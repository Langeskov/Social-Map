"""Phase 14.1 ChangeLog repository: append-only persistence and workspace isolation."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domain.entities import (ChangeLogEntry, Workspace, canonical_json,
                                 decode_snapshot)
from app.domain.errors import DomainError
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage.change_log_repositories import SQLChangeLogRepository
from app.storage.database import DatabaseError
from app.storage.models import ChangeLogModel

NOW = datetime(2026, 11, 22, 8, 0, 0, 123456, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def make_entry(workspace_id, *, entity_type="PERSON", entity_id=None, action="UPDATE",
               revision_no=1, operation_id=None, before=None, after=None,
               parent=None, metadata=None, changed_fields=("display_name",),
               recorded_at=NOW, payload_version="1"):
    entity_id = entity_id or uuid4()
    if before is None and action not in ("CREATE", "IMPORT"):
        before = {"display_name": "Old"}
    if after is None:
        after = {"display_name": "New"}
    parent_type, parent_id = (None, None) if parent is None else parent
    return ChangeLogEntry(
        workspace_id=workspace_id, entity_type=entity_type, entity_id=entity_id,
        action=action, operation_id=operation_id or uuid4(), revision_no=revision_no,
        changed_fields=tuple(changed_fields), payload_version=payload_version,
        recorded_at=recorded_at, before_snapshot=before, after_snapshot=after,
        parent_entity_type=parent_type, parent_entity_id=parent_id, metadata=metadata)


@pytest.fixture
def workspace(runtime, now):
    entity = Workspace(id=uuid4(), name=f"WS {uuid4().hex[:8]}", description="d",
                       created_at=now, updated_at=now)
    with transaction(runtime.sessions) as session:
        assert sql_repositories(session).workspaces.save(entity) == entity
    return entity


def test_add_and_get_roundtrip_all_fields(runtime, workspace):
    entry = make_entry(workspace.id, before={"display_name": "Old", "notes": None},
                       after={"display_name": "New", "notes": "n"},
                       changed_fields=("display_name", "notes"),
                       metadata={"source_revision_no": 1})
    with transaction(runtime.sessions) as session:
        saved = SQLChangeLogRepository(session).add(entry)
        assert saved == entry
        assert SQLChangeLogRepository(session).get(workspace.id, entry.id) == entry
        raw = session.scalar(select(ChangeLogModel).where(ChangeLogModel.id == entry.id))
        assert raw.changed_fields == canonical_json(["display_name", "notes"])
        assert raw.before_snapshot == canonical_json(entry.before_snapshot)
        assert raw.after_snapshot == canonical_json(entry.after_snapshot)
        assert raw.metadata_json == canonical_json({"source_revision_no": 1})
        assert raw.payload_version == "1"


def test_entry_requires_contract_fields():
    workspace_id = uuid4()
    with pytest.raises(DomainError):
        make_entry(workspace_id, entity_type="UNKNOWN")
    with pytest.raises(DomainError):
        make_entry(workspace_id, action="BOGUS")
    with pytest.raises(DomainError):
        make_entry(workspace_id, entity_type="MEDIA", action="CREATE")  # not allowed
    with pytest.raises(DomainError):
        make_entry(workspace_id, revision_no=0)
    with pytest.raises(DomainError):
        make_entry(workspace_id, payload_version="")
    with pytest.raises(DomainError):
        make_entry(workspace_id, changed_fields=("",))
    with pytest.raises(DomainError):
        make_entry(workspace_id, parent=("PERSON", None))       # half a parent
    with pytest.raises(DomainError):
        make_entry(workspace_id, action="CREATE", before={"x": 1})


def test_entity_workspace_and_operation_queries_are_scoped(runtime, workspace):
    other = Workspace(id=uuid4(), name=f"Other {uuid4().hex[:8]}", created_at=NOW,
                      updated_at=NOW)
    person = uuid4()
    operation = uuid4()
    with transaction(runtime.sessions) as session:
        assert sql_repositories(session).workspaces.save(other) == other
        repository = SQLChangeLogRepository(session)
        first = repository.add(make_entry(workspace.id, entity_id=person,
                                          operation_id=operation, revision_no=1,
                                          recorded_at=NOW, after={"display_name": "A"}))
        second = repository.add(make_entry(workspace.id, entity_id=person,
                                           operation_id=operation, revision_no=2,
                                           recorded_at=LATER, after={"display_name": "B"}))
        repository.add(make_entry(other.id, entity_id=person, revision_no=1))
        repository.add(make_entry(workspace.id, entity_id=uuid4(), revision_no=1))
        history = repository.list_by_entity(workspace.id, "PERSON", person)
        assert [row.id for row in history] == [first.id, second.id]
        assert repository.list_by_entity(other.id, "PERSON", person)[0].workspace_id == other.id
        assert repository.get(workspace.id, first.id) == first
        assert repository.get(other.id, first.id) is None
        operation_rows = repository.list_by_operation(workspace.id, operation)
        assert [row.revision_no for row in operation_rows] == [1, 2]
        assert repository.list_by_operation(other.id, operation) == []
        assert repository.latest_revision(workspace.id, "PERSON", person) == 2
        assert repository.latest_revision(workspace.id, "PERSON", uuid4()) is None


def test_pagination_and_parent_queries(runtime, workspace):
    relationship = uuid4()
    history_id = uuid4()
    with transaction(runtime.sessions) as session:
        repository = SQLChangeLogRepository(session)
        for revision in range(1, 4):
            repository.add(make_entry(workspace.id, entity_type="EVENT",
                                      entity_id=relationship, revision_no=revision,
                                      action="CREATE" if revision == 1 else "UPDATE",
                                      before=None if revision == 1 else {"a": revision - 1},
                                      after={"a": revision}))
        repository.add(make_entry(
            workspace.id, entity_type="RELATIONSHIP_HISTORY", entity_id=history_id,
            action="APPEND_HISTORY", changed_fields=(),
            before={"state": "OLD"}, after={"state": "NEW"},
            parent=("RELATIONSHIP", relationship)))
        assert [row.revision_no for row in repository.list_by_entity(
            workspace.id, "EVENT", relationship, limit=2, offset=1)] == [2, 3]
        parent_rows = repository.list_by_parent(workspace.id, "RELATIONSHIP", relationship)
        assert [row.entity_id for row in parent_rows] == [history_id]
        assert repository.get_revision(workspace.id, "EVENT", relationship, 2) \
            .revision_no == 2
        assert repository.get_revision(workspace.id, "EVENT", relationship, 9) is None


def test_unique_revision_is_enforced_by_the_database(runtime, workspace):
    entity_id = uuid4()
    with transaction(runtime.sessions) as session:
        repository = SQLChangeLogRepository(session)
        repository.add(make_entry(workspace.id, entity_id=entity_id, revision_no=1))
    with pytest.raises(DatabaseError):
        with transaction(runtime.sessions) as session:
            SQLChangeLogRepository(session).add(
                make_entry(workspace.id, entity_id=entity_id, revision_no=1))


def test_database_check_constraints_reject_invalid_rows(runtime, workspace):
    entity_id = uuid4()
    base = ("INSERT INTO change_log (id, workspace_id, entity_type, entity_id, action, "
            "operation_id, revision_no, changed_fields, payload_version, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '1', ?)")
    values = dict(id=uuid4().hex, workspace_id=workspace.id.hex, entity_type="PERSON",
                  entity_id=entity_id.hex, action="UPDATE", operation_id=uuid4().hex,
                  revision_no=1, recorded_at="2026-11-22 08:00:00.000000")
    for field, value in (("entity_type", "BOGUS"), ("action", "BOGUS"),
                         ("revision_no", 0)):
        broken = dict(values, **{field: value})
        with pytest.raises(DatabaseError):
            with transaction(runtime.sessions) as session:
                session.connection().exec_driver_sql(
                    base, (broken["id"], broken["workspace_id"], broken["entity_type"],
                           broken["entity_id"], broken["action"], broken["operation_id"],
                           broken["revision_no"], broken["recorded_at"]))
    # Parent must be both set or both NULL.
    with pytest.raises(Exception):
        with transaction(runtime.sessions) as session:
            session.connection().exec_driver_sql(
                "INSERT INTO change_log (id, workspace_id, entity_type, entity_id, "
                "parent_entity_type, action, operation_id, revision_no, changed_fields, "
                "payload_version, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, '[]', '1', ?)",
                (uuid4().hex, workspace.id.hex, "PERSON", entity_id.hex, "PERSON",
                 "UPDATE", uuid4().hex, "2026-11-22 08:00:00.000000"))
    # Workspace foreign key is real.
    with pytest.raises(Exception):
        with transaction(runtime.sessions) as session:
            session.connection().exec_driver_sql(
                base, (uuid4().hex, uuid4().hex, "PERSON", entity_id.hex, "UPDATE",
                       uuid4().hex, 1, "2026-11-22 08:00:00.000000"))


def test_repository_is_append_only_and_orm_guards_reject_mutation(runtime, workspace):
    with transaction(runtime.sessions) as session:
        repository = SQLChangeLogRepository(session)
        assert not hasattr(repository, "update")
        assert not hasattr(repository, "delete")
        entry = repository.add(make_entry(workspace.id))
    with pytest.raises(DomainError):
        with transaction(runtime.sessions) as session:
            model = session.get(ChangeLogModel, entry.id)
            model.action = "ARCHIVE"
    with pytest.raises(DomainError):
        with transaction(runtime.sessions) as session:
            session.delete(session.get(ChangeLogModel, entry.id))
    with transaction(runtime.sessions) as session:
        assert SQLChangeLogRepository(session).get(workspace.id, entry.id) == entry


def test_snapshot_decoding_roundtrips_through_the_domain_contract(runtime, workspace):
    entry = make_entry(workspace.id, before={"display_name": "旧", "notes": None},
                       after={"display_name": "新", "notes": "备注"})
    with transaction(runtime.sessions) as session:
        SQLChangeLogRepository(session).add(entry)
        loaded = SQLChangeLogRepository(session).get(workspace.id, entry.id)
        assert loaded.before_snapshot == {"display_name": "旧", "notes": None}
        assert loaded.after_snapshot == {"display_name": "新", "notes": "备注"}
        assert decode_snapshot("1", canonical_json(loaded.after_snapshot)) == \
            loaded.after_snapshot
