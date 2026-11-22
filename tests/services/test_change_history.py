"""Phase 14.3 read-side history: pages, diffs, operation groups, graceful legacy."""
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config.settings import AppConfig
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.change_history import (ACTION_LABELS, ChangeHistoryService, ENTITY_LABELS,
                                         LONG_TEXT_LIMIT)
from app.services.database import initialize_database
from app.services.person import PersonService
from app.services.unit_of_work import transaction
from app.storage.models import ChangeLogModel

NOW = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "history-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    try:
        yield type("Env", (), {"config": config, "runtime": runtime,
                               "services": services, "ws": services.initial_workspace})()
    finally:
        runtime.close()


@pytest.fixture
def history(env):
    return ChangeHistoryService(env.runtime.sessions)


def test_entity_history_is_newest_first_and_paginated(env, history):
    person = env.services.people.create(env.ws.id, "Alice")
    for index in range(3):
        env.services.people.update(env.ws.id, person.id, occupation=f"Role {index}")
    page = history.entity_history(env.ws.id, "PERSON", person.id, limit=2, offset=0)
    assert [view.revision_no for view in page.revisions] == [4, 3]
    assert page.has_more is True
    assert page.offset == 0
    older = history.entity_history(env.ws.id, "PERSON", person.id, limit=2, offset=2)
    assert [view.revision_no for view in older.revisions] == [2, 1]
    assert older.has_more is False
    assert older.legacy_hint is None


def test_revision_details_use_human_labels_and_values(env, history):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer",
                                        birthday=date(1990, 5, 12))
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    view = history.revision(env.ws.id, "PERSON", person.id, 2)
    assert view.action_label == "更新"
    assert view.summary.startswith("更新：")
    change = next(item for item in view.changes if item.field == "occupation")
    assert change.label == "职业"
    assert change.before_text == "Engineer" and change.after_text == "Designer"
    created = history.revision(env.ws.id, "PERSON", person.id, 1)
    birthday = next(item for item in created.changes if item.field == "birthday")
    assert birthday.before_text == "（新增）" and birthday.after_text == "1990-05-12"
    assert all("{" not in item.label for item in created.changes)


def test_diff_between_revisions_and_adjacent(env, history):
    person = env.services.people.create(env.ws.id, "Alice", occupation="A")
    env.services.people.update(env.ws.id, person.id, occupation="B")
    env.services.people.update(env.ws.id, person.id, occupation="C")
    changes = history.diff(env.ws.id, "PERSON", person.id, 1, 3)
    assert [(item.label, item.before_text, item.after_text) for item in changes] == [
        ("职业", "A", "C")]
    adjacent = history.adjacent_diff(env.ws.id, "PERSON", person.id, 3)
    assert [(item.before_text, item.after_text) for item in adjacent] == [("B", "C")]
    with pytest.raises(NotFoundError):
        history.diff(env.ws.id, "PERSON", person.id, 1, 9)
    with pytest.raises(DomainError):
        history.adjacent_diff(env.ws.id, "PERSON", person.id, 1)


def test_long_text_is_formatted_but_available_in_full(env, history):
    long_note = "长" * (LONG_TEXT_LIMIT + 30)
    person = env.services.people.create(env.ws.id, "Alice", notes=long_note)
    view = history.revision(env.ws.id, "PERSON", person.id, 1)
    note = next(item for item in view.changes if item.field == "notes")
    assert len(note.after_text) > LONG_TEXT_LIMIT
    assert note.after_text.startswith("长" * 20)


def test_operation_group_lists_all_rows(env, history):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    page = history.entity_history(env.ws.id, "RELATIONSHIP", relationship.id)
    operation = history.operation(env.ws.id, page.revisions[0].entry.operation_id)
    assert operation.recorded_at is not None
    assert {(row.entity_type, row.action_label) for row in operation.rows} == {
        ("RELATIONSHIP", "创建"), ("RELATIONSHIP_HISTORY", "追加状态历史")}
    assert all(row.entity_label for row in operation.rows)


def test_relationship_parent_history_is_available(env, history):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    env.services.relationships.add_history(env.ws.id, relationship.id, "PAUSED")
    page = history.parent_history(env.ws.id, "RELATIONSHIP", relationship.id, limit=50)
    assert [view.action_label for view in page.revisions] == ["追加状态历史",
                                                              "追加状态历史"]
    assert all(view.entry.parent_entity_id == relationship.id for view in page.revisions)


def test_archived_entity_history_stays_readable(env, history):
    person = env.services.people.create(env.ws.id, "Alice")
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    env.services.people.soft_delete(env.ws.id, person.id)
    page = history.entity_history(env.ws.id, "PERSON", person.id)
    assert [view.action_label for view in page.revisions] == ["归档", "更新", "创建"]


def test_legacy_entity_hint_is_explicit_and_not_fabricated(tmp_path, history, env):
    plain = PersonService(env.runtime.sessions, media_service=None)
    person = plain.create(env.ws.id, "Old Person")
    # Phase 14 starts auditing later: the first recorded change is an UPDATE, not a
    # fabricated CREATE.
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    page = history.entity_history(env.ws.id, "PERSON", person.id)
    assert [view.revision_no for view in page.revisions] == [1]
    assert page.revisions[0].action_label == "更新"
    assert "Phase 14 之前" in (page.legacy_hint or "")
    empty = history.entity_history(env.ws.id, "PERSON", uuid4())
    assert empty.has_history is False and empty.legacy_hint is None


def test_unknown_payload_version_degrades_instead_of_crashing(env, history):
    person = env.services.people.create(env.ws.id, "Alice")
    with transaction(env.runtime.sessions) as session:
        session.add(ChangeLogModel(
            id=uuid4(), workspace_id=env.ws.id, entity_type="PERSON",
            entity_id=person.id, action="UPDATE", operation_id=uuid4(),
            revision_no=2, changed_fields='["display_name"]',
            before_snapshot='{"display_name":"Old"}',
            after_snapshot='{"display_name":"New"}', payload_version="999",
            recorded_at=NOW))
    page = history.entity_history(env.ws.id, "PERSON", person.id)
    broken = next(view for view in page.revisions if view.revision_no == 2)
    assert broken.snapshot_unavailable is True
    assert "快照不可用" in broken.summary
    with pytest.raises(DomainError):
        history.diff(env.ws.id, "PERSON", person.id, 1, 2)


def test_workspace_isolation_for_history(env, history):
    other = env.services.workspaces.create("Other")
    person = env.services.people.create(other.id, "Other Alice")
    assert history.entity_history(other.id, "PERSON", person.id).has_history is True
    assert history.entity_history(env.ws.id, "PERSON", person.id).has_history is False
    with pytest.raises(NotFoundError):
        history.entity_history(uuid4(), "PERSON", person.id)


def test_validation_and_read_only_api(history):
    with pytest.raises(DomainError):
        history.entity_history(uuid4(), "UNKNOWN", uuid4())
    with pytest.raises(DomainError):
        history.entity_history(uuid4(), "PERSON", uuid4(), limit=0)
    with pytest.raises(DomainError):
        history.revision(uuid4(), "PERSON", uuid4(), 0)
    for forbidden in ("record", "write", "revert", "update", "delete"):
        assert not hasattr(history, forbidden)
