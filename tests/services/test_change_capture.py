"""Phase 14.2 change capture: writer contract and per-entity audit rows.

Uses the real application composition (so the production writer wiring is the one
under test) and real migrated temporary databases. Only the HTTP/model layer is
never touched here: these are explicit, user-triggered business operations.
"""
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.domain.entities import Person, Workspace
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.change_capture import ChangeLogWriter
from app.services.change_snapshots import person_state
from app.services.database import initialize_database
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage.change_log_repositories import SQLChangeLogRepository
from app.storage.models import ChangeLogModel


@dataclass
class Env:
    config: AppConfig
    runtime: object
    services: object
    ws: Workspace


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "capture-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    yield Env(config, runtime, services, services.initial_workspace)
    runtime.close()


@pytest.fixture
def images(tmp_path):
    originals = tmp_path / "originals"
    originals.mkdir()
    result = []
    for index, color in enumerate(((10, 20, 30), (40, 50, 60), (70, 80, 90))):
        path = originals / f"image-{index}.png"
        Image.new("RGB", (12 + index, 10 + index), color).save(path, "PNG")
        result.append(path)
    return tuple(result)


def history(env, entity_type, entity_id, *, workspace_id=None):
    scope = env.ws.id if workspace_id is None else workspace_id
    with transaction(env.runtime.sessions) as session:
        return tuple(SQLChangeLogRepository(session).list_by_entity(
            scope, entity_type, entity_id))


def all_rows(env):
    with transaction(env.runtime.sessions) as session:
        return tuple(session.scalars(
            select(ChangeLogModel).order_by(ChangeLogModel.recorded_at,
                                            ChangeLogModel.id)))


def count_rows(env):
    with transaction(env.runtime.sessions) as session:
        return session.scalar(select(func.count()).select_from(ChangeLogModel))


def make_people(env, *names):
    return tuple(env.services.people.create(env.ws.id, name) for name in names)


def make_event(env, participants=()):
    return env.services.events.create_event(env.ws.id, "Retro", "MEETING",
                                            occurred_on=date(2026, 11, 20),
                                            participants=participants)


# ------------------------------------------------------------------ writer core
def test_writer_begin_is_a_distinct_operation_with_one_recorded_time(env):
    writer = env.services.people._changes
    first = writer.begin(env.ws.id)
    second = writer.begin(env.ws.id)
    assert first.operation_id != second.operation_id
    assert first.workspace_id == second.workspace_id == env.ws.id
    assert first.recorded_at.tzinfo is not None
    assert second.recorded_at.tzinfo is not None


def test_writer_validates_entity_workspace_and_existence(env):
    writer: ChangeLogWriter = env.services.people._changes
    with transaction(env.runtime.sessions) as session:
        repositories = sql_repositories(session)
        other = Workspace(name=f"Other {uuid4().hex[:8]}")
        repositories.workspaces.save(other)
        person = Person(workspace_id=env.ws.id, display_name="Alice")
        repositories.people.save(person)
        context = writer.begin(env.ws.id)
        before = person_state(repositories, env.ws.id, person)
        after = dict(before, display_name="Alicia")
        entry = writer.record(session, context, entity_type="PERSON", action="UPDATE",
                              entity=person, before=before, after=after)
        assert entry is not None and entry.changed_fields == ("display_name",)
        assert entry.recorded_at == context.recorded_at
        assert entry.operation_id == context.operation_id
        # No-op update records nothing.
        assert writer.record(session, context, entity_type="PERSON", action="UPDATE",
                             entity=person, before=before, after=before) is None
        # Lifecycle with identical business snapshot is still recorded.
        archived = writer.record(session, context, entity_type="PERSON",
                                 action="ARCHIVE", entity=person,
                                 before=before, after=before)
        assert archived is not None and archived.changed_fields == ()
        # Cross-workspace entity/context is refused.
        with pytest.raises(DomainError):
            writer.record(session, writer.begin(other.id), entity_type="PERSON",
                          action="UPDATE", entity=person, before=before, after=after)
        # Missing entity is refused.
        ghost = SimpleNamespace(id=uuid4(), workspace_id=env.ws.id)
        with pytest.raises(DomainError):
            writer.record(session, context, entity_type="PERSON", action="UPDATE",
                          entity=ghost, before=before, after=after)
        # Invalid/absent parent is refused.
        with pytest.raises(DomainError):
            writer.record(session, context, entity_type="RELATIONSHIP_HISTORY",
                          action="APPEND_HISTORY", entity=ghost,
                          after={"relationship_id": str(uuid4()), "state": "ACTIVE",
                                 "effective_at": None, "note": None, "event_id": None},
                          parent=("RELATIONSHIP", uuid4()))
        # Unknown action for the entity type is refused.
        with pytest.raises(DomainError):
            writer.record(session, context, entity_type="MEDIA", action="CREATE",
                          entity=ghost, after={})


def test_writer_requires_session_and_context(env):
    writer: ChangeLogWriter = env.services.people._changes
    with transaction(env.runtime.sessions) as session:
        with pytest.raises(DomainError):
            writer.record(None, writer.begin(env.ws.id), entity_type="PERSON",
                          action="CREATE", entity=SimpleNamespace(id=uuid4()),
                          after={})
        with pytest.raises(DomainError):
            writer.record(session, {"operation_id": uuid4()}, entity_type="PERSON",
                          action="CREATE", entity=SimpleNamespace(id=uuid4()),
                          after={})


# ------------------------------------------------------------------- per entity
def test_person_create_update_archive_restore_are_captured(env):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    rows = history(env, "PERSON", person.id)
    assert [row.action for row in rows] == ["CREATE"]
    assert rows[0].revision_no == 1
    assert rows[0].before_snapshot is None
    assert rows[0].after_snapshot["display_name"] == "Alice"
    assert "occupation" in rows[0].changed_fields
    assert rows[0].payload_version == "1"

    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    rows = history(env, "PERSON", person.id)
    assert [row.revision_no for row in rows] == [1, 2]
    assert rows[1].action == "UPDATE"
    assert rows[1].changed_fields == ("occupation",)
    assert rows[1].before_snapshot["occupation"] == "Engineer"
    assert rows[1].after_snapshot["occupation"] == "Designer"

    env.services.people.soft_delete(env.ws.id, person.id)
    rows = history(env, "PERSON", person.id)
    assert rows[-1].action == "ARCHIVE" and rows[-1].revision_no == 3


def test_alias_and_media_link_changes_update_the_person_snapshot(env, images):
    person = env.services.people.create(env.ws.id, "Alice")
    media = env.services.media.import_file(env.ws.id, images[0])
    alias = env.services.people.add_alias(env.ws.id, person.id, "A", "NICKNAME")
    rows = history(env, "PERSON", person.id)
    assert rows[-1].changed_fields == ("aliases",)
    assert [item["alias"] for item in rows[-1].after_snapshot["aliases"]] == ["A"]

    env.services.people.remove_alias(env.ws.id, person.id, alias.id)
    assert history(env, "PERSON", person.id)[-1].after_snapshot["aliases"] == []

    env.services.people.add_gallery(env.ws.id, person.id, media.id)
    rows = history(env, "PERSON", person.id)
    assert rows[-1].changed_fields == ("images",)
    assert [item["media_id"] for item in rows[-1].after_snapshot["images"]] == [str(media.id)]

    link = env.services.people.detail(env.ws.id, person.id).images[0].link
    env.services.people.remove_media_reference(env.ws.id, person.id, link.id)
    rows = history(env, "PERSON", person.id)
    assert rows[-1].changed_fields == ("images",)
    assert rows[-1].after_snapshot["images"] == []


def test_person_archive_records_the_media_unbinding(env, images):
    person = env.services.people.create(env.ws.id, "Alice")
    media = env.services.media.import_file(env.ws.id, images[0])
    env.services.people.add_gallery(env.ws.id, person.id, media.id)
    env.services.people.soft_delete(env.ws.id, person.id)
    rows = history(env, "PERSON", person.id)
    assert rows[-1].action == "ARCHIVE"
    assert rows[-1].before_snapshot["images"] != []
    assert rows[-1].after_snapshot["images"] == []


def test_event_create_update_participants_and_lifecycle(env):
    alice, bob = make_people(env, "Alice", "Bob")
    event = env.services.events.create_event(env.ws.id, "Retro", "MEETING",
                                             occurred_on=date(2026, 11, 20),
                                             participants=(alice.id,))
    rows = history(env, "EVENT", event.id)
    assert rows[0].action == "CREATE"
    assert [item["person_id"] for item in rows[0].after_snapshot["participants"]] == \
        [str(alice.id)]

    env.services.events.update_event(env.ws.id, event.id, title="Retro 2")
    rows = history(env, "EVENT", event.id)
    assert rows[-1].changed_fields == ("title",)

    env.services.events.add_participant(env.ws.id, event.id, bob.id, role="LEAD")
    rows = history(env, "EVENT", event.id)
    assert rows[-1].changed_fields == ("participants",)
    assert len(rows[-1].after_snapshot["participants"]) == 2

    env.services.events.remove_participant(env.ws.id, event.id, alice.id)
    assert len(history(env, "EVENT", event.id)[-1].after_snapshot["participants"]) == 1

    env.services.events.soft_delete_event(env.ws.id, event.id)
    env.services.events.restore_event(env.ws.id, event.id)
    rows = history(env, "EVENT", event.id)
    assert [row.action for row in rows[-2:]] == ["ARCHIVE", "RESTORE"]


def test_relationship_create_and_history_append_share_one_operation(env):
    alice, bob = make_people(env, "Alice", "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id,
                                                     "COLLEAGUE")
    relationship_rows = history(env, "RELATIONSHIP", relationship.id)
    assert relationship_rows[0].action == "CREATE"
    operation = relationship_rows[0].operation_id

    with transaction(env.runtime.sessions) as session:
        operation_rows = SQLChangeLogRepository(session).list_by_operation(
            env.ws.id, operation)
    assert {row.entity_type for row in operation_rows} == {"RELATIONSHIP",
                                                          "RELATIONSHIP_HISTORY"}
    history_row = next(row for row in operation_rows
                       if row.entity_type == "RELATIONSHIP_HISTORY")
    assert history_row is not None and history_row.action == "APPEND_HISTORY"
    assert history_row.parent_entity_type == "RELATIONSHIP"
    assert history_row.parent_entity_id == relationship.id
    assert history_row.after_snapshot["state"] == "ACTIVE"

    added = env.services.relationships.add_history(env.ws.id, relationship.id, "PAUSED")
    appended = history(env, "RELATIONSHIP_HISTORY", added.id)
    assert len(appended) == 1 and appended[0].action == "APPEND_HISTORY"

    env.services.relationships.soft_delete(env.ws.id, relationship.id)
    env.services.relationships.restore(env.ws.id, relationship.id)
    actions = [row.action for row in history(env, "RELATIONSHIP", relationship.id)]
    assert actions[-2:] == ["ARCHIVE", "RESTORE"]


def test_history_event_link_is_captured_as_attach_or_detach(env):
    alice, bob = make_people(env, "Alice", "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id,
                                                     "COLLEAGUE")
    history_row = env.services.relationships.history(env.ws.id, relationship.id)[0]
    event = make_event(env)
    env.services.events.attach_event_to_history(env.ws.id, history_row.id, event.id)
    rows = history(env, "RELATIONSHIP_HISTORY", history_row.id)
    assert rows[-1].action == "ATTACH_EVENT"
    assert rows[-1].before_snapshot["event_id"] is None
    assert rows[-1].after_snapshot["event_id"] == str(event.id)

    env.services.events.attach_event_to_history(env.ws.id, history_row.id, None)
    rows = history(env, "RELATIONSHIP_HISTORY", history_row.id)
    assert rows[-1].action == "DETACH_EVENT"
    assert rows[-1].after_snapshot["event_id"] is None


def test_evidence_metadata_media_and_lifecycle(env, images):
    alice = make_people(env, "Alice")[0]
    event = make_event(env, participants=(alice.id,))
    first = env.services.media.import_file(env.ws.id, images[0])
    second = env.services.media.import_file(env.ws.id, images[1])
    evidence = env.services.evidence.create_evidence(
        env.ws.id, event.id, "IMAGE", "Screenshot", media=(first.id,))
    rows = history(env, "EVIDENCE", evidence.id)
    assert rows[0].action == "CREATE"
    assert [item["media_id"] for item in rows[0].after_snapshot["media"]] == [str(first.id)]
    assert "content_text" not in rows[0].after_snapshot

    env.services.evidence.update_metadata(env.ws.id, evidence.id, title="New title")
    assert history(env, "EVIDENCE", evidence.id)[-1].changed_fields == ("title",)

    env.services.evidence.add_media(env.ws.id, evidence.id, second.id)
    assert history(env, "EVIDENCE", evidence.id)[-1].changed_fields == ("media",)
    env.services.evidence.remove_media(env.ws.id, evidence.id, second.id)
    assert len(history(env, "EVIDENCE", evidence.id)[-1].after_snapshot["media"]) == 1

    env.services.evidence.soft_delete_evidence(env.ws.id, evidence.id)
    env.services.evidence.restore_evidence(env.ws.id, evidence.id)
    actions = [row.action for row in history(env, "EVIDENCE", evidence.id)]
    assert actions[-2:] == ["ARCHIVE", "RESTORE"]


def test_memory_lifecycle_is_captured(env):
    person = make_people(env, "Alice")[0]
    memory = env.services.memories.create(env.ws.id, "Likes tea", kind="PREFERENCE",
                                          person_id=person.id)
    rows = history(env, "MEMORY", memory.id)
    assert rows[0].action == "CREATE"
    assert rows[0].after_snapshot["content"] == "Likes tea"
    assert rows[0].after_snapshot["source_type"] == "MANUAL"

    env.services.memories.update(env.ws.id, memory.id, content="Likes coffee")
    assert history(env, "MEMORY", memory.id)[-1].changed_fields == ("content",)

    env.services.memories.soft_delete(env.ws.id, memory.id)
    env.services.memories.restore(env.ws.id, memory.id)
    actions = [row.action for row in history(env, "MEMORY", memory.id)]
    assert actions[-2:] == ["ARCHIVE", "RESTORE"]


def test_workspace_lifecycle_is_captured(env):
    workspace = env.services.workspaces.create("Second", "description")
    rows = history(env, "WORKSPACE", workspace.id, workspace_id=workspace.id)
    assert rows[0].action == "CREATE" and rows[0].after_snapshot["name"] == "Second"
    env.services.workspaces.update(workspace.id, name="Renamed", description="d2")
    assert history(env, "WORKSPACE", workspace.id,
                   workspace_id=workspace.id)[-1].changed_fields == (
        "name", "description")
    env.services.workspaces.soft_delete(workspace.id)
    assert history(env, "WORKSPACE", workspace.id,
                   workspace_id=workspace.id)[-1].action == "ARCHIVE"


def test_media_import_and_archive_are_captured(env, images):
    media = env.services.media.import_file(env.ws.id, images[0])
    rows = history(env, "MEDIA", media.id)
    assert rows[0].action == "IMPORT"
    assert rows[0].before_snapshot is None
    assert rows[0].after_snapshot["mime_type"] == "image/png"
    env.services.media.soft_delete(env.ws.id, media.id)
    rows = history(env, "MEDIA", media.id)
    assert rows[-1].action == "ARCHIVE"


def test_person_with_avatar_is_one_operation_without_duplicate_person_rows(env, images):
    person = env.services.people.create(env.ws.id, "With Avatar", avatar_path=images[0])
    with transaction(env.runtime.sessions) as session:
        rows = SQLChangeLogRepository(session).list_by_operation(
            env.ws.id, history(env, "PERSON", person.id)[0].operation_id)
    assert {(row.entity_type, row.action) for row in rows} == {
        ("PERSON", "CREATE"), ("MEDIA", "IMPORT")}
    person_rows = history(env, "PERSON", person.id)
    assert len(person_rows) == 1
    assert len(person_rows[0].after_snapshot["images"]) == 1


def test_media_reimport_after_archive_is_restore_not_import(env, images):
    media = env.services.media.import_file(env.ws.id, images[0])
    env.services.media.soft_delete(env.ws.id, media.id)
    again = env.services.media.import_file(env.ws.id, images[0])
    assert again.id == media.id
    rows = history(env, "MEDIA", media.id)
    assert [row.action for row in rows] == ["IMPORT", "ARCHIVE", "RESTORE"]
    assert rows[-1].before_snapshot is not None and rows[-1].after_snapshot is not None
    assert rows[-1].before_snapshot == rows[-1].after_snapshot  # content unchanged
    # Re-importing an already active media must not fabricate history.
    before = len(rows)
    duplicate = env.services.media.import_file(env.ws.id, images[0])
    assert duplicate.id == media.id
    assert len(history(env, "MEDIA", media.id)) == before
