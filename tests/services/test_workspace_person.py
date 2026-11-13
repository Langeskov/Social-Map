from dataclasses import replace
from datetime import date, timezone
import pytest
from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError, NotFoundError
from app.services.database import initialize_database
from app.services.workspace import WorkspaceService
from app.services.person import PersonService
from app.services.unit_of_work import transaction
from app.services.repositories import sql_repositories

@pytest.fixture
def basic_services(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    runtime = initialize_database(config)
    workspaces = WorkspaceService(runtime.sessions)
    people = PersonService(runtime.sessions)
    yield runtime, workspaces, people
    runtime.close()


def test_default_workspace_idempotent_and_no_people(basic_services):
    runtime, workspaces, people = basic_services
    first = workspaces.ensure_default()
    assert workspaces.ensure_default().id == first.id
    assert len(workspaces.list()) == 1
    assert people.list(first.id) == []


def test_default_is_created_when_another_scope_exists(basic_services):
    runtime, workspaces, people = basic_services
    custom = workspaces.create("Custom")
    default = workspaces.ensure_default()
    assert default.name == "Default Workspace" and default.id != custom.id
    assert workspaces.ensure_default().id == default.id
    workspaces.soft_delete(default.id)
    assert workspaces.ensure_default().id == custom.id
    assert people.list(custom.id) == []


def test_workspace_crud_and_isolation(basic_services):
    runtime, workspaces, people = basic_services
    a, b = workspaces.create("A"), workspaces.create("B")
    assert workspaces.update(a.id, name="Updated", description="description").name == "Updated"
    with pytest.raises(ConflictError):
        workspaces.create("Updated")
    with pytest.raises(ConflictError):
        workspaces.update(b.id, name="Updated")
    person = people.create(a.id, "duplicate", real_name="shared", birthday=date(2000, 1, 2))
    assert people.list(b.id) == []
    with pytest.raises(NotFoundError):
        people.get(b.id, person.id)
    with pytest.raises(DomainError):
        workspaces.soft_delete(a.id)
    workspaces.soft_delete(b.id)
    assert {item.id for item in workspaces.list()} == {a.id}


def test_person_alias_search_update_delete_and_utc(basic_services):
    runtime, workspaces, people = basic_services
    workspace = workspaces.create("A")
    first = people.create(workspace.id, "Same", real_name="original", notes="private")
    second = people.create(workspace.id, "Same", real_name="original")
    alias = people.add_alias(workspace.id, first.id, "literal%_alias", "CUSTOM_TYPE")
    people.add_alias(workspace.id, second.id, "other")
    assert [item.id for item in people.list(workspace.id, "%_")] == [first.id]
    assert len(people.list(workspace.id, "original")) == 2
    assert people.detail(workspace.id, first.id).aliases == (alias,)
    changed = people.update(workspace.id, first.id, display_name="new", location="local", notes=None)
    assert first.display_name == "Same" and changed.display_name == "new"
    assert changed.updated_at >= first.updated_at
    assert changed.updated_at.tzinfo == timezone.utc
    with pytest.raises(DomainError):
        people.update(workspace.id, first.id, id=first.id)
    with pytest.raises(NotFoundError):
        people.remove_alias(workspace.id, second.id, alias.id)
    people.remove_alias(workspace.id, first.id, alias.id)
    assert not people.detail(workspace.id, first.id).aliases
    people.soft_delete(workspace.id, first.id)
    assert [item.id for item in people.list(workspace.id)] == [second.id]
    with pytest.raises(NotFoundError):
        people.get(workspace.id, first.id)
    with transaction(runtime.sessions) as session:
        archived = sql_repositories(session).people.get(workspace.id, first.id, include_deleted=True)
        assert archived.deleted_at.tzinfo == timezone.utc
        assert archived.updated_at == archived.deleted_at


def test_service_snapshots_survive_restart(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    runtime = initialize_database(config)
    workspace = WorkspaceService(runtime.sessions).create("A")
    person = PersonService(runtime.sessions).create(workspace.id, "Saved")
    runtime.close()
    reopened = initialize_database(config)
    try:
        assert WorkspaceService(reopened.sessions).get(workspace.id) == workspace
        assert PersonService(reopened.sessions).get(workspace.id, person.id) == person
    finally:
        reopened.close()
