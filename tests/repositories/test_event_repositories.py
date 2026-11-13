"""Scoped adapters and row-specific history attachment authorization.

Database-level UNIQUE/FK coverage for event participants lives in
tests/migrations/test_event_schema.py against the same real schema.
"""
import ast
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.config.settings import AppConfig
from app.domain.entities import Event, EventParticipant
from app.domain.errors import DomainError, NotFoundError
from app.services.database import initialize_database
from app.services.event import EventService
from app.services.event_repositories import sql_event_repositories
from app.services.person import PersonService
from app.services.relationship import RelationshipService
from app.services.unit_of_work import transaction
from app.services.workspace import WorkspaceService
from app.storage.models import RelationshipHistoryModel

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    runtime = initialize_database(AppConfig(data_dir=tmp_path / "repository-events"))
    try:
        workspaces, people = WorkspaceService(runtime.sessions), PersonService(runtime.sessions)
        ws, foreign = workspaces.create("one"), workspaces.create("two")
        a, b = people.create(ws.id, "a"), people.create(ws.id, "b")
        entity = Event(workspace_id=ws.id, title="event", event_type="type", occurred_on=date(2026, 9, 30))
        service = EventService(runtime.sessions)
        saved = service.create_event(ws.id, "event", "type", occurred_on=entity.occurred_on, participants=(a.id,))
        relationships = RelationshipService(runtime.sessions)
        rel = relationships.create(ws.id, a.id, b.id, "Friend")
        history = relationships.history(ws.id, rel.id)[0]
        yield SimpleNamespace(runtime=runtime, ws=ws.id, foreign=foreign.id, a=a, b=b,
            entity=entity, saved=saved, history=history, service=service)
    finally:
        runtime.close()


def test_repository_crud_scoped_lists_and_detached(env):
    with transaction(env.runtime.sessions, write=True) as session:
        repo = sql_event_repositories(session)
        entity = repo.events.save(env.entity)
        participant = repo.participants.add(EventParticipant(workspace_id=env.ws,
            event_id=entity.id, person_id=env.b.id, role="guest"))
        assert repo.events.get(env.foreign, entity.id, include_deleted=True) is None
        assert repo.participants.list_by_event(env.foreign, entity.id) == []
        assert repo.participants.list_by_person(env.foreign, env.b.id) == []
        assert repo.participants.list_by_person(env.ws, env.b.id) == [participant]
        assert [x.id for x in repo.events.list_by_person(env.ws, env.b.id)] == [entity.id]
        revised = repo.events.update(replace(entity, title="updated", updated_at=NOW))
        assert revised.title == "updated"
        deleted = repo.events.soft_delete(env.ws, entity.id, NOW)
        assert repo.events.get(env.ws, entity.id) is None
        assert repo.events.get(env.ws, entity.id, include_deleted=True) == deleted
        assert repo.participants.list_by_event(env.ws, entity.id) == [participant]
        assert repo.events.restore(env.ws, entity.id, NOW).deleted_at is None
        repo.participants.remove(env.foreign, entity.id, env.b.id)
        assert repo.participants.list_by_event(env.ws, entity.id) == [participant]
        repo.participants.remove(env.ws, entity.id, env.b.id)
        assert repo.participants.list_by_event(env.ws, entity.id) == []
    assert revised.title == "updated" and participant.role == "guest"


def test_repository_writes_rollback_under_caller(env):
    with pytest.raises(RuntimeError):
        with transaction(env.runtime.sessions, write=True) as session:
            repo = sql_event_repositories(session)
            repo.events.save(env.entity)
            repo.participants.add(EventParticipant(workspace_id=env.ws, event_id=env.entity.id, person_id=env.b.id))
            raise RuntimeError("synthetic failure after flush")
    with transaction(env.runtime.sessions) as session:
        repo = sql_event_repositories(session)
        assert repo.events.get(env.ws, env.entity.id, include_deleted=True) is None
        assert repo.participants.list_by_person(env.ws, env.b.id) == []


@pytest.mark.parametrize("change", ["event_id", "state", "note", "effective_at", "created_at", "workspace_id", "relationship_id", "id", "delete"])
def test_unauthorized_orm_history_changes_rejected(env, change):
    with pytest.raises(DomainError):
        with transaction(env.runtime.sessions, write=True) as session:
            model = session.get(RelationshipHistoryModel, env.history.id)
            if change == "delete": session.delete(model)
            else:
                value = env.saved.id if change == "event_id" else (
                    uuid4() if change.endswith("id") else NOW if change.endswith("at") else "changed")
                setattr(model, change, value)
            session.flush()
    assert env.service.list_attachable_histories(env.ws)[0].history == env.history


@pytest.mark.parametrize("wrong", ["row", "old", "new", "mixed", "delete"])
def test_history_capability_row_value_and_field_specific(env, wrong):
    with pytest.raises(DomainError):
        with transaction(env.runtime.sessions, write=True) as session:
            model = session.get(RelationshipHistoryModel, env.history.id)
            session.info["ris_history_event_attachment"] = (
                uuid4() if wrong == "row" else model.id,
                uuid4() if wrong == "old" else None,
                uuid4() if wrong == "new" else env.saved.id)
            model.event_id = env.saved.id
            if wrong == "mixed": model.state = "PAUSED"
            if wrong == "delete": session.delete(model)
            session.flush()


def test_authorization_removed_after_success_and_failure(env, monkeypatch):
    with transaction(env.runtime.sessions, write=True) as session:
        repo = sql_event_repositories(session)
        assert repo.histories.attach_event(env.ws, env.history.id, env.saved.id).event_id == env.saved.id
        assert "ris_history_event_attachment" not in session.info
        assert repo.histories.attach_event(env.ws, env.history.id, None) == env.history
        assert "ris_history_event_attachment" not in session.info
    with env.runtime.sessions() as session:
        session.begin()
        repo = sql_event_repositories(session)
        original_flush = session.flush
        def fail_authorized(*args, **kwargs):
            if "ris_history_event_attachment" in session.info:
                raise RuntimeError("synthetic authorized flush failure")
            return original_flush(*args, **kwargs)
        monkeypatch.setattr(session, "flush", fail_authorized)
        with pytest.raises(RuntimeError):
            repo.histories.attach_event(env.ws, env.history.id, env.saved.id)
        assert "ris_history_event_attachment" not in session.info
        session.rollback()
    assert env.service.list_attachable_histories(env.ws)[0].history == env.history


def test_event_identity_workspace_created_time_protected(env):
    with transaction(env.runtime.sessions) as session:
        repo = sql_event_repositories(session)
        for changes in (dict(workspace_id=env.foreign), dict(created_at=NOW)):
            with pytest.raises(DomainError):
                repo.events.update(replace(env.saved, **changes))
        with pytest.raises(NotFoundError):
            repo.events.update(replace(env.saved, id=uuid4()))


def test_adapters_never_own_session_engine_or_transaction():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "app/storage/event_repositories.py").read_text(encoding="utf-8"))
    forbidden = {"commit", "rollback", "create_engine", "create_session_factory", "sessionmaker"}
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Attribute) and node.func.attr in forbidden or
        isinstance(node.func, ast.Name) and node.func.id in forbidden)]
