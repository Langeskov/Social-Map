"""Phase 4 adapters against real migrated databases, with caller-owned transactions."""
import ast
import inspect
from dataclasses import fields, replace
from datetime import timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.entities import Person, Relationship, RelationshipHistory, Workspace
from app.domain.errors import DomainError, NotFoundError
from app.services.relationship_repositories import sql_relationship_repositories
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage import relationship_repositories as adapters
from app.storage.database import DatabaseError
from app.storage.models import RelationshipHistoryModel, RelationshipModel


@pytest.fixture
def relationship_records(runtime, now):
    workspace = Workspace(name="Relationships", created_at=now, updated_at=now)
    other = Workspace(name="Other relationships", created_at=now, updated_at=now)
    a = Person(id=UUID(int=1), workspace_id=workspace.id, display_name="A", created_at=now, updated_at=now)
    b = replace(a, id=UUID(int=2), display_name="B")
    c = replace(a, id=UUID(int=3), display_name="C")
    relationship = Relationship(workspace_id=workspace.id, person_a_id=a.id, person_b_id=b.id,
                                relationship_type="Friend", created_at=now, updated_at=now)
    history = RelationshipHistory(workspace_id=workspace.id, relationship_id=relationship.id,
                                  state="ACTIVE", effective_at=now, created_at=now, note="fixture note")
    with transaction(runtime.sessions) as session:
        base = sql_repositories(session)
        for entity in (workspace, other):
            base.workspaces.save(entity)
        for entity in (a, b, c):
            base.people.save(entity)
        repositories = sql_relationship_repositories(session)
        repositories.relationships.save(relationship)
        repositories.histories.add(history)
    return workspace, other, a, b, c, relationship, history


def test_mapping_detached_snapshots_and_lifecycle(runtime, relationship_records, now):
    ws, _, a, b, _, relation, history = relationship_records
    with transaction(runtime.sessions) as session:
        repositories = sql_relationship_repositories(session)
        loaded = repositories.relationships.get(ws.id, relation.id)
        assert loaded == relation and loaded is not relation
        updated = replace(loaded, updated_at=now + timedelta(days=1), deleted_at=now + timedelta(days=1))
        assert repositories.relationships.update(updated) == updated
        assert repositories.relationships.get(ws.id, relation.id) is None
        assert repositories.relationships.get(ws.id, relation.id, include_deleted=True) == updated
        restored = replace(updated, deleted_at=None, updated_at=now + timedelta(days=2))
        assert repositories.relationships.save(restored) == restored
        entries = repositories.histories.list_by_relationship(ws.id, relation.id)
    assert entries == [history]
    assert type(loaded) is Relationship and type(entries[0]) is RelationshipHistory
    for entity in (loaded, entries[0]):
        for field in fields(entity):
            value = getattr(entity, field.name)
            if hasattr(value, "tzinfo"):
                assert value.tzinfo == timezone.utc
    assert (loaded.person_a_id, loaded.person_b_id) == (a.id, b.id)
    with transaction(runtime.sessions) as session:
        assert sql_relationship_repositories(session).relationships.get(ws.id, relation.id) == restored


@pytest.mark.parametrize("method", ["save", "update"])
@pytest.mark.parametrize("field", ["workspace_id", "person_a_id", "person_b_id", "relationship_type", "directionality", "created_at"])
def test_same_id_replaced_identity_rejected_without_persisting(runtime, relationship_records, now, method, field):
    ws, other, a, b, c, relation, _ = relationship_records
    changes = {
        "workspace_id": other.id,
        "person_a_id": UUID(int=0),
        "person_b_id": c.id,
        "relationship_type": "Different",
        "directionality": "DIRECTED",
        "created_at": now + timedelta(seconds=1),
    }
    replacement = replace(relation, **{field: changes[field]})
    assert replacement.id == relation.id
    with pytest.raises(DomainError, match="身份"):
        with transaction(runtime.sessions) as session:
            getattr(sql_relationship_repositories(session).relationships, method)(replacement)
    with transaction(runtime.sessions) as session:
        assert sql_relationship_repositories(session).relationships.get(ws.id, relation.id) == relation


@pytest.mark.parametrize("inactive", ["relationship", "person_a", "person_b", "workspace"])
def test_deleted_auditing_is_explicit_and_workspace_scoped(runtime, relationship_records, now, inactive):
    ws, other, a, b, _, relation, history = relationship_records
    with transaction(runtime.sessions) as session:
        base = sql_repositories(session)
        repositories = sql_relationship_repositories(session)
        if inactive == "relationship":
            repositories.relationships.soft_delete(ws.id, relation.id, now)
        elif inactive == "workspace":
            base.workspaces.save(replace(ws, deleted_at=now))
        else:
            base.people.save(replace(a if inactive == "person_a" else b, deleted_at=now))
        repo = repositories.relationships
        assert repo.get(ws.id, relation.id) is None
        assert repo.list_by_person(ws.id, a.id) == []
        assert repo.list_by_person(ws.id, b.id) == []
        assert repo.list_by_workspace(ws.id) == []
        audited = repo.get(ws.id, relation.id, include_deleted=True)
        assert audited.id == relation.id
        assert repo.list_by_person(ws.id, a.id, include_deleted=True) == [audited]
        assert repo.list_by_person(ws.id, b.id, include_deleted=True) == [audited]
        assert repo.list_by_workspace(ws.id, include_deleted=True) == [audited]
        identity = (ws.id, a.id, b.id, relation.relationship_type, relation.directionality)
        assert repo.get_by_identity(*identity) == audited
        assert repo.get_by_identity(*identity, include_deleted=False) is None
        assert repo.get(other.id, relation.id, include_deleted=True) is None
        assert repo.list_by_workspace(other.id, include_deleted=True) == []
        assert repo.list_by_person(other.id, a.id, include_deleted=True) == []
        assert repo.get_by_identity(other.id, *identity[1:]) is None
        assert repositories.histories.list_by_relationship(other.id, relation.id) == []
        assert repositories.histories.get_latest(other.id, relation.id) is None
        assert repositories.histories.list_by_relationship(ws.id, relation.id) == [history]
        with pytest.raises(NotFoundError):
            repo.soft_delete(other.id, relation.id, now)


@pytest.mark.parametrize("deleted", [False, True])
def test_full_natural_key_is_unique_even_deleted(runtime, relationship_records, now, deleted):
    ws, _, a, b, _, relation, _ = relationship_records
    with transaction(runtime.sessions) as session:
        repo = sql_relationship_repositories(session).relationships
        if deleted:
            repo.soft_delete(ws.id, relation.id, now)
        distinct = [replace(relation, id=uuid4(), relationship_type="friend"),
                    replace(relation, id=uuid4(), relationship_type="Colleague"),
                    replace(relation, id=uuid4(), directionality="DIRECTED"),
                    replace(relation, id=uuid4(), person_a_id=b.id, person_b_id=a.id, directionality="DIRECTED")]
        for entity in distinct:
            assert repo.save(entity) == entity
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_relationship_repositories(session).relationships.save(replace(relation, id=uuid4()))
    assert isinstance(error.value.__cause__, IntegrityError)
    with transaction(runtime.sessions) as session:
        assert len(sql_relationship_repositories(session).relationships.list_by_workspace(ws.id, include_deleted=True)) == 5


def test_history_order_effective_created_uuid_and_as_of(runtime, relationship_records, now):
    ws, _, _, _, _, relation, original = relationship_records
    later = now + timedelta(days=1)
    entries = [replace(original, id=UUID(int=20), state="FUTURE_STATE", effective_at=later, created_at=now),
               replace(original, id=UUID(int=10), state="PAUSED", effective_at=later, created_at=now),
               replace(original, id=UUID(int=5), state="ENDED", effective_at=later, created_at=later),
               replace(original, id=UUID(int=30), effective_at=now - timedelta(days=1), created_at=later)]
    with transaction(runtime.sessions) as session:
        repo = sql_relationship_repositories(session).histories
        for entry in entries:
            assert repo.add(entry) == entry
        expected = sorted([original, *entries], key=lambda item: (item.effective_at, item.created_at, item.id.int))
        assert repo.list_by_relationship(ws.id, relation.id) == expected
        assert repo.get_latest(ws.id, relation.id) == entries[2]
        assert repo.get_latest(ws.id, relation.id, as_of=now) == original
        assert repo.get_latest(ws.id, relation.id, as_of=later) == entries[2]
        assert repo.get_latest(ws.id, relation.id, as_of=now - timedelta(days=2)) is None
        for forbidden in ("update", "save", "delete", "remove"):
            assert not hasattr(repo, forbidden)


@pytest.mark.parametrize("mutation", ["history_update", "history_delete", "relationship_id", "relationship_created_at"])
def test_orm_guards_preserve_identity_and_append_only_history(runtime, relationship_records, now, mutation):
    ws, _, _, _, _, relation, history = relationship_records
    with pytest.raises(DomainError):
        with transaction(runtime.sessions) as session:
            if mutation.startswith("history"):
                row = session.get(RelationshipHistoryModel, history.id)
                if mutation == "history_delete":
                    session.delete(row)
                else:
                    row.state = "ENDED"
            else:
                row = session.get(RelationshipModel, relation.id)
                if mutation == "relationship_id":
                    row.id = uuid4()
                else:
                    row.created_at = now + timedelta(days=1)
            session.flush()
    with transaction(runtime.sessions) as session:
        repositories = sql_relationship_repositories(session)
        assert repositories.relationships.get(ws.id, relation.id) == relation
        assert repositories.histories.list_by_relationship(ws.id, relation.id) == [history]


def test_history_add_existing_id_cannot_overwrite_prior_state(runtime, relationship_records):
    ws, _, _, _, _, relation, history = relationship_records
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_relationship_repositories(session).histories.add(replace(history, state="ENDED"))
    assert isinstance(error.value.__cause__, IntegrityError)
    with transaction(runtime.sessions) as session:
        assert sql_relationship_repositories(session).histories.list_by_relationship(ws.id, relation.id) == [history]


def test_repository_writes_remain_rollbackable_by_caller(runtime, relationship_records, now):
    ws, _, _, _, _, relation, history = relationship_records
    with pytest.raises(RuntimeError, match="caller rollback"):
        with transaction(runtime.sessions) as session:
            repositories = sql_relationship_repositories(session)
            repositories.relationships.update(replace(relation, updated_at=now + timedelta(days=1)))
            repositories.histories.add(replace(history, id=uuid4(), state="ENDED"))
            raise RuntimeError("caller rollback")
    with transaction(runtime.sessions) as session:
        repositories = sql_relationship_repositories(session)
        assert repositories.relationships.get(ws.id, relation.id) == relation
        assert repositories.histories.list_by_relationship(ws.id, relation.id) == [history]


def test_adapters_only_map_and_flush_never_own_session_engine_or_transaction(runtime, relationship_records, now, monkeypatch):
    ws, _, _, _, _, relation, history = relationship_records
    tree = ast.parse(inspect.getsource(adapters))
    forbidden = {"commit", "rollback", "close", "begin", "create_engine", "create_sqlite_engine", "sessionmaker", "Session", "initialize_database", "create_session_factory"}
    calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))}
    assert not calls.intersection(forbidden)
    with runtime.sessions() as session:
        with session.begin():
            def forbidden_call(*args, **kwargs):
                raise AssertionError("Repository took transaction ownership")
            with monkeypatch.context() as patch:
                patch.setattr(session, "commit", forbidden_call)
                patch.setattr(session, "rollback", forbidden_call)
                patch.setattr(session, "close", forbidden_call)
                repositories = sql_relationship_repositories(session)
                changed = replace(relation, updated_at=now + timedelta(seconds=1))
                assert repositories.relationships.update(changed) == changed
                assert repositories.relationships.save(changed) == changed
                assert repositories.relationships.get(ws.id, relation.id) == changed
                assert repositories.histories.add(replace(history, id=uuid4(), state="PAUSED")).state == "PAUSED"
                assert len(repositories.histories.list_by_relationship(ws.id, relation.id)) == 2
        # The successful caller-owned transaction committed without repository help.
    with transaction(runtime.sessions) as session:
        assert sql_relationship_repositories(session).relationships.get(ws.id, relation.id) == changed
