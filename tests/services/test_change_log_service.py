"""Phase 14.1 read-only ChangeLogService: validation, pagination, isolation.

The service exposes history reads only; recording (14.2), UI formatting (14.3)
and revert (14.4) must not leak into this stage.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.config.settings import AppConfig
from app.domain.entities import ChangeLogEntry, Workspace
from app.domain.errors import DomainError, NotFoundError
from app.services.change_log import ChangeLogService, MAX_PAGE_SIZE
from app.services.database import initialize_database
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage.change_log_repositories import SQLChangeLogRepository

NOW = datetime(2026, 11, 22, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def runtime(tmp_path):
    database = initialize_database(AppConfig(data_dir=tmp_path))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(runtime):
    return ChangeLogService(runtime.sessions)


@pytest.fixture
def workspaces(runtime):
    first = Workspace(id=uuid4(), name=f"A {uuid4().hex[:8]}", created_at=NOW,
                      updated_at=NOW)
    second = Workspace(id=uuid4(), name=f"B {uuid4().hex[:8]}", created_at=NOW,
                       updated_at=NOW)
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        assert repositories.workspaces.save(first) == first
        assert repositories.workspaces.save(second) == second
    return first, second


def entry(workspace_id, *, entity_type="PERSON", entity_id=None, revision_no=1,
          operation_id=None, recorded_at=NOW, action="UPDATE", parent=None):
    before = {"display_name": "Old"} if action not in ("CREATE", "IMPORT") else None
    after = {"display_name": "New"}
    parent_type, parent_id = (None, None) if parent is None else parent
    return ChangeLogEntry(
        workspace_id=workspace_id, entity_type=entity_type,
        entity_id=entity_id or uuid4(), action=action,
        operation_id=operation_id or uuid4(), revision_no=revision_no,
        changed_fields=("display_name",) if before else (), payload_version="1",
        recorded_at=recorded_at, before_snapshot=before, after_snapshot=after,
        parent_entity_type=parent_type, parent_entity_id=parent_id)


def add(runtime, workspace, *entries):
    with transaction(runtime.sessions) as session:
        repository = SQLChangeLogRepository(session)
        saved = [repository.add(item) for item in entries]
    return saved


def test_history_is_ordered_paginated_and_scoped(runtime, service, workspaces):
    first_ws, second_ws = workspaces
    person = uuid4()
    rows = add(runtime, first_ws,
               entry(first_ws.id, entity_id=person, revision_no=1,
                     recorded_at=NOW),
               entry(first_ws.id, entity_id=person, revision_no=2, recorded_at=NOW),
               entry(first_ws.id, entity_id=person, revision_no=3, recorded_at=NOW))
    add(runtime, second_ws, entry(second_ws.id, entity_id=person, revision_no=1))
    assert [row.id for row in service.history(first_ws.id, "PERSON", person)] == \
        [row.id for row in rows]
    assert [row.revision_no for row in service.history(
        first_ws.id, "PERSON", person, limit=1, offset=1)] == [2]
    assert service.latest_revision(first_ws.id, "PERSON", person) == 3
    assert service.latest_revision(second_ws.id, "PERSON", person) == 1
    assert len(service.history(second_ws.id, "PERSON", person)) == 1
    assert service.revision(first_ws.id, "PERSON", person, 2).revision_no == 2
    with pytest.raises(NotFoundError):
        service.revision(first_ws.id, "PERSON", person, 9)
    with pytest.raises(NotFoundError):
        service.revision(second_ws.id, "PERSON", person, 2)


def test_operation_and_parent_queries(runtime, service, workspaces):
    ws, _other = workspaces
    operation_id = uuid4()
    relationship = uuid4()
    history_id = uuid4()
    add(runtime, ws,
        entry(ws.id, entity_type="RELATIONSHIP", entity_id=relationship,
              revision_no=1, operation_id=operation_id, action="CREATE"),
        entry(ws.id, entity_type="RELATIONSHIP_HISTORY", entity_id=history_id,
              revision_no=1, operation_id=operation_id, action="APPEND_HISTORY",
              recorded_at=NOW + timedelta(minutes=1),
              parent=("RELATIONSHIP", relationship)))
    rows = service.operation(ws.id, operation_id)
    assert [row.entity_type for row in rows] == ["RELATIONSHIP", "RELATIONSHIP_HISTORY"]
    assert service.operation(ws.id, uuid4()) == ()
    parent_rows = service.parent_history(ws.id, "RELATIONSHIP", relationship)
    assert [row.entity_id for row in parent_rows] == [history_id]


def test_validation_errors_are_explicit(service, workspaces, runtime):
    ws, other = workspaces
    person = uuid4()
    with pytest.raises(DomainError):
        service.history(ws.id, "UNKNOWN_TYPE", person)
    with pytest.raises(DomainError):
        service.history(ws.id, "PERSON", person, limit=0)
    with pytest.raises(DomainError):
        service.history(ws.id, "PERSON", person, limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(DomainError):
        service.history(ws.id, "PERSON", person, offset=-1)
    with pytest.raises(DomainError):
        service.revision(ws.id, "PERSON", person, 0)
    with pytest.raises(NotFoundError):
        service.history(uuid4(), "PERSON", person)      # unknown workspace
    with pytest.raises(NotFoundError):
        service.operation(uuid4(), uuid4())
    add(runtime, ws, entry(ws.id, entity_id=person, revision_no=1))
    assert service.history(other.id, "PERSON", person) == ()


def test_service_has_no_write_api(service):
    for forbidden in ("record", "add", "update", "delete", "revert", "capture"):
        assert not hasattr(service, forbidden)
