"""Phase 14.4 controlled revert: whitelist, new revision, no history rewrite."""
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.change_revert import (REVERT_WHITELISTS, ChangeRevertService,
                                        supports_revert)
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.storage.models import ChangeLogModel

NOW = datetime(2026, 11, 22, 11, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "revert-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    try:
        yield type("Env", (), {"config": config, "runtime": runtime,
                               "services": services, "ws": services.initial_workspace})()
    finally:
        runtime.close()


@pytest.fixture
def revert(env):
    return env.services.change_revert


def audit_count(env):
    with transaction(env.runtime.sessions) as session:
        return session.scalar(select(func.count()).select_from(ChangeLogModel))


def entries(env, entity_type, entity_id, workspace_id=None):
    from app.storage.change_log_repositories import SQLChangeLogRepository
    scope = workspace_id or env.ws.id
    with transaction(env.runtime.sessions) as session:
        return tuple(SQLChangeLogRepository(session).list_by_entity(
            scope, entity_type, entity_id))


def test_person_revert_creates_a_new_revert_revision(env, revert):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    original = entries(env, "PERSON", person.id)
    preview = revert.preview(env.ws.id, "PERSON", person.id, 1)
    assert preview.can_revert and preview.has_changes
    assert [(item.field, item.current_text, item.target_text) for item in preview.fields] == [
        ("occupation", "Designer", "Engineer")]
    result = revert.revert(env.ws.id, "PERSON", person.id, 1, confirmed=True)
    assert result.applied is True and result.revision_no == 3
    assert env.services.people.get(env.ws.id, person.id).occupation == "Engineer"
    latest = entries(env, "PERSON", person.id)
    assert [row.action for row in latest] == ["CREATE", "UPDATE", "REVERT"]
    revert_row = latest[-1]
    assert revert_row.changed_fields == ("occupation",)
    assert revert_row.metadata == {"source_revision_no": 1}
    assert revert_row.operation_id not in {row.operation_id for row in original}
    assert tuple(latest[:2]) == tuple(original)          # old rows untouched


def test_preview_only_lists_whitelisted_fields(env, revert):
    person = env.services.people.create(env.ws.id, "Alice")
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    env.services.people.add_alias(env.ws.id, person.id, "A", "NICKNAME")
    preview = revert.preview(env.ws.id, "PERSON", person.id, 2)
    assert all(item.field in REVERT_WHITELISTS["PERSON"] for item in preview.fields)
    assert "aliases" not in {item.field for item in preview.fields}


def test_forbidden_entities_and_snapshot_errors_are_blocked(env, revert):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    preview = revert.preview(env.ws.id, "RELATIONSHIP", relationship.id, 1)
    assert preview.can_revert is False
    assert "不支持" in preview.reason
    assert supports_revert("RELATIONSHIP") is False
    with transaction(env.runtime.sessions) as session:
        session.add(ChangeLogModel(
            id=uuid4(), workspace_id=env.ws.id, entity_type="PERSON",
            entity_id=alice.id, action="UPDATE", operation_id=uuid4(),
            revision_no=99, changed_fields='["display_name"]',
            before_snapshot='{"display_name":"A"}',
            after_snapshot='{"display_name":"B"}', payload_version="999",
            recorded_at=NOW))
    blocked = revert.preview(env.ws.id, "PERSON", alice.id, 99)
    assert blocked.can_revert is False and "快照" in blocked.reason
    with pytest.raises(DomainError):
        revert.revert(env.ws.id, "PERSON", alice.id, 99, confirmed=True)


def test_archived_entity_is_blocked(env, revert):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    env.services.people.soft_delete(env.ws.id, person.id)
    preview = revert.preview(env.ws.id, "PERSON", person.id, 1)
    assert preview.can_revert is False and "归档" in preview.reason
    with pytest.raises(DomainError):
        revert.revert(env.ws.id, "PERSON", person.id, 1, confirmed=True)


def test_confirmation_is_required(env, revert):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    before = audit_count(env)
    with pytest.raises(DomainError):
        revert.revert(env.ws.id, "PERSON", person.id, 1)
    assert env.services.people.get(env.ws.id, person.id).occupation == "Designer"
    assert audit_count(env) == before


def test_noop_revert_does_not_create_a_revision(env, revert):
    person = env.services.people.create(env.ws.id, "Alice")
    before = audit_count(env)
    result = revert.revert(env.ws.id, "PERSON", person.id, 1, confirmed=True)
    assert result.applied is False and result.revision_no is None
    assert "一致" in result.message
    assert audit_count(env) == before


def test_workspace_name_conflict_is_rejected_without_writing(env, revert):
    first = env.services.workspaces.create("Name One")
    env.services.workspaces.update(first.id, name="Name Two")
    env.services.workspaces.create("Name One")       # old name now belongs to another row
    before = audit_count(env)
    preview = revert.preview(first.id, "WORKSPACE", first.id, 1)
    assert preview.can_revert is False and "名称" in preview.reason
    with pytest.raises(DomainError):
        revert.revert(first.id, "WORKSPACE", first.id, 1, confirmed=True)
    assert env.services.workspaces.get(first.id).name == "Name Two"
    assert audit_count(env) == before


def test_workspace_description_revert(env, revert):
    workspace = env.services.workspaces.create("Desc", "old")
    env.services.workspaces.update(workspace.id, name="Desc", description="new")
    result = revert.revert(workspace.id, "WORKSPACE", workspace.id, 1, confirmed=True)
    assert result.applied is True
    assert env.services.workspaces.get(workspace.id).description == "old"
    latest = entries(env, "WORKSPACE", workspace.id, workspace_id=workspace.id)[-1]
    assert latest.action == "REVERT"


def test_event_and_evidence_revert_respect_immutability(env, revert):
    person = env.services.people.create(env.ws.id, "Alice")
    event = env.services.events.create_event(env.ws.id, "Retro", "MEETING",
                                             occurred_on=date(2026, 11, 20),
                                             participants=(person.id,))
    env.services.events.update_event(env.ws.id, event.id, occurred_on=date(2026, 11, 21))
    detail = env.services.events.get(env.ws.id, event.id)
    env.services.change_revert.revert(env.ws.id, "EVENT", event.id, 1, confirmed=True)
    assert env.services.events.get(env.ws.id, event.id).occurred_on == date(2026, 11, 20)

    evidence = env.services.evidence.create_evidence(
        env.ws.id, event.id, "TEXT", "Title", content_text="原始内容",
        source_type="MANUAL")
    content_before = env.services.evidence.detail(env.ws.id, evidence.id).evidence.content_text
    env.services.evidence.update_metadata(env.ws.id, evidence.id, title="New title",
                                          annotation="note")
    preview = revert.preview(env.ws.id, "EVIDENCE", evidence.id, 1)
    fields = {item.field for item in preview.fields}
    assert fields <= {"title", "annotation"} and "content_text" not in fields
    revert.revert(env.ws.id, "EVIDENCE", evidence.id, 1, confirmed=True)
    assert env.services.evidence.detail(env.ws.id, evidence.id).evidence.title == "Title"
    assert env.services.evidence.detail(env.ws.id, evidence.id).evidence.content_text == content_before


def test_memory_person_and_validity_clearing_roundtrip(env, revert):
    person = env.services.people.create(env.ws.id, "Alice")
    memory = env.services.memories.create(env.ws.id, "Likes tea", kind="PREFERENCE",
                                          person_id=person.id,
                                          valid_from=NOW)
    env.services.memories.update(env.ws.id, memory.id, clear_person=True,
                                 clear_valid_from=True)
    current = env.services.memories.get(env.ws.id, memory.id)
    assert current.person_id is None and current.valid_from is None
    # Revision 2 is the cleared state; revision 1 has person + validity.
    revert.revert(env.ws.id, "MEMORY", memory.id, 1, confirmed=True)
    restored = env.services.memories.get(env.ws.id, memory.id)
    assert restored.person_id == person.id and restored.valid_from == NOW
    # Reverting to the cleared revision clears them again through clear_* flags.
    revert.revert(env.ws.id, "MEMORY", memory.id, 2, confirmed=True)
    cleared = env.services.memories.get(env.ws.id, memory.id)
    assert cleared.person_id is None and cleared.valid_from is None


def test_revert_failure_rolls_back_and_writes_no_audit(env, revert, monkeypatch):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    before = audit_count(env)

    def explode(*_args, **_kwargs):
        raise RuntimeError("snapshot failure during revert")

    monkeypatch.setattr("app.services.person.person_state", explode)
    with pytest.raises(RuntimeError):
        revert.revert(env.ws.id, "PERSON", person.id, 1, confirmed=True)
    assert env.services.people.get(env.ws.id, person.id).occupation == "Designer"
    assert audit_count(env) == before


def test_revert_validation(env, revert):
    person = env.services.people.create(env.ws.id, "Alice")
    with pytest.raises(DomainError):
        revert.preview(env.ws.id, "PERSON", person.id, 0)
    with pytest.raises(NotFoundError):
        revert.preview(env.ws.id, "PERSON", uuid4(), 1)
    with pytest.raises(DomainError):
        revert.preview(env.ws.id, "WORKSPACE", uuid4(), 1)
    for name in ("record", "write", "delete"):
        assert not hasattr(revert, name)
