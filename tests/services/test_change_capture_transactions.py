"""Phase 14.2 atomicity: business rows and audit rows commit or roll back together."""
from dataclasses import dataclass
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.person import PersonService
from app.services.unit_of_work import transaction
from app.storage.change_log_repositories import SQLChangeLogRepository
from app.storage.models import ChangeLogModel, PersonModel


@dataclass
class Env:
    config: AppConfig
    runtime: object
    services: object
    ws: object


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "atomic-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    yield Env(config, runtime, services, services.initial_workspace)
    runtime.close()


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "avatar.png"
    Image.new("RGB", (12, 10), (20, 30, 40)).save(path, "PNG")
    return path


def audit_count(env):
    with transaction(env.runtime.sessions) as session:
        return session.scalar(select(func.count()).select_from(ChangeLogModel))


def person_rows(env):
    with transaction(env.runtime.sessions) as session:
        return session.scalar(select(func.count()).select_from(PersonModel))


def history(env, entity_type, entity_id):
    with transaction(env.runtime.sessions) as session:
        return tuple(SQLChangeLogRepository(session).list_by_entity(
            env.ws.id, entity_type, entity_id))


def test_business_and_audit_commit_together(env):
    person = env.services.people.create(env.ws.id, "Alice")
    assert person_rows(env) == 1
    assert audit_count(env) == 1
    assert history(env, "PERSON", person.id)[0].action == "CREATE"


def test_snapshot_failure_rolls_back_the_business_write(env, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("snapshot failure")

    monkeypatch.setattr("app.services.person.person_state", explode)
    with pytest.raises(RuntimeError):
        env.services.people.create(env.ws.id, "Alice")
    assert person_rows(env) == 0
    assert audit_count(env) == 0


def test_changelog_write_failure_rolls_back_the_business_write(env, monkeypatch):
    def explode(self, entry):
        raise RuntimeError("audit write failure")

    monkeypatch.setattr(SQLChangeLogRepository, "add", explode)
    with pytest.raises(RuntimeError):
        env.services.people.create(env.ws.id, "Alice")
    assert person_rows(env) == 0
    assert audit_count(env) == 0


def test_event_snapshot_failure_rolls_back_participants(env, monkeypatch):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")

    def explode(*_args, **_kwargs):
        raise RuntimeError("snapshot failure")

    monkeypatch.setattr("app.services.event.event_state", explode)
    from datetime import date
    with pytest.raises(RuntimeError):
        env.services.events.create_event(env.ws.id, "Retro", "MEETING",
                                         occurred_on=date(2026, 11, 20),
                                         participants=(alice.id, bob.id))
    with transaction(env.runtime.sessions) as session:
        from app.storage.models import EventModel, EventParticipantModel
        assert session.scalar(select(func.count()).select_from(EventModel)) == 0
        assert session.scalar(select(func.count()).select_from(EventParticipantModel)) == 0


def test_failed_validation_writes_no_history(env):
    before = audit_count(env)
    with pytest.raises(Exception):
        env.services.people.create(env.ws.id, "   ")
    assert audit_count(env) == before


def test_noop_update_creates_no_revision(env):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    rows = history(env, "PERSON", person.id)
    assert [row.revision_no for row in rows] == [1]


def test_revisions_increment_per_entity_and_start_at_one(env):
    first = env.services.people.create(env.ws.id, "Alice")
    second = env.services.people.create(env.ws.id, "Bob")
    env.services.people.update(env.ws.id, first.id, occupation="Engineer")
    env.services.people.update(env.ws.id, first.id, occupation="Designer")
    assert [row.revision_no for row in history(env, "PERSON", first.id)] == [1, 2, 3]
    assert [row.revision_no for row in history(env, "PERSON", second.id)] == [1]


def test_person_avatar_operation_groups_media_and_person_rows(env, image):
    person = env.services.people.create(env.ws.id, "With Avatar", avatar_path=image)
    person_rows = history(env, "PERSON", person.id)
    assert len(person_rows) == 1
    with transaction(env.runtime.sessions) as session:
        rows = SQLChangeLogRepository(session).list_by_operation(
            env.ws.id, person_rows[0].operation_id)
    assert {(row.entity_type, row.action) for row in rows} == {
        ("PERSON", "CREATE"), ("MEDIA", "IMPORT")}


def test_read_operations_never_write_audit_rows(env):
    person = env.services.people.create(env.ws.id, "Alice")
    before = audit_count(env)
    env.services.people.list(env.ws.id)
    env.services.people.detail(env.ws.id, person.id)
    env.services.people.get(env.ws.id, person.id)
    env.services.dashboard.snapshot(env.ws.id)
    env.services.memories.list_all(env.ws.id)
    assert audit_count(env) == before


def test_second_workspace_audit_rows_stay_scoped(env):
    other = env.services.workspaces.create("Second")
    person = env.services.people.create(other.id, "Other Alice")
    with transaction(env.runtime.sessions) as session:
        rows = SQLChangeLogRepository(session).list_by_entity(
            other.id, "PERSON", person.id)
        assert len(rows) == 1 and rows[0].workspace_id == other.id
        assert SQLChangeLogRepository(session).list_by_entity(
            env.ws.id, "PERSON", person.id) == []


def test_application_composition_shares_one_writer(env):
    services = env.services
    writer = services.people._changes
    assert writer is not None
    for service in (services.people, services.media, services.events,
                    services.evidence, services.relationships, services.memories,
                    services.workspaces):
        assert service._changes is writer


def test_service_without_writer_does_not_capture(tmp_path):
    config = AppConfig(data_dir=tmp_path / "no-writer")
    runtime = initialize_database(config)
    try:
        services = create_application_services(config, runtime.sessions)
        plain = PersonService(runtime.sessions, media_service=None)
        workspace = services.initial_workspace
        plain.create(workspace.id, "Unrecorded")
        with transaction(runtime.sessions) as session:
            # ensure_default() is system bootstrap and the plain service has no
            # writer: no audit rows at all.
            assert session.scalar(select(func.count()).select_from(ChangeLogModel)) == 0
    finally:
        runtime.close()
