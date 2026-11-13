"""Workspace is an isolation boundary, not an account or tenant system."""
from dataclasses import replace
from uuid import UUID
from app.domain.entities import Workspace
from app.domain.errors import ConflictError, DomainError
from app.domain.validation import utc_now
from app.services.unit_of_work import transaction
from app.services.operations import write_operation
from app.services.repositories import sql_repositories
from app.services.rules import workspace_required

class WorkspaceService:
    def __init__(self, sessions, repositories=sql_repositories):
        self._sessions, self._repositories = sessions, repositories

    def list(self) -> list[Workspace]:
        with transaction(self._sessions) as session:
            return self._repositories(session).workspaces.list()

    def get(self, workspace_id: UUID) -> Workspace:
        with transaction(self._sessions) as session:
            return workspace_required(self._repositories(session), workspace_id)

    def ensure_default(self) -> Workspace:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = repositories.workspaces.get_by_name("Default Workspace", include_deleted=True)
            if old is not None and old.deleted_at is None:
                return old
            active = repositories.workspaces.list()
            if old is not None and active:
                # Respect an explicitly archived default while other scopes exist.
                return active[0]
            return repositories.workspaces.save(
                replace(old, deleted_at=None, updated_at=utc_now()) if old else Workspace(name="Default Workspace"))

    def create(self, name: str, description: str | None = None) -> Workspace:
        entity = Workspace(name=name.strip(), description=description)
        with write_operation(self._sessions) as session:
            repository = self._repositories(session).workspaces
            if repository.get_by_name(entity.name, include_deleted=True):
                raise ConflictError("Workspace 名称已存在（包括已归档 Workspace）。")
            return repository.save(entity)

    def update(self, workspace_id: UUID, *, name: str, description: str | None = None) -> Workspace:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = workspace_required(repositories, workspace_id)
            collision = repositories.workspaces.get_by_name(name.strip(), include_deleted=True)
            if collision and collision.id != old.id:
                raise ConflictError("Workspace 名称已存在。")
            return repositories.workspaces.save(replace(old, name=name.strip(), description=description, updated_at=utc_now()))

    def soft_delete(self, workspace_id: UUID) -> None:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = workspace_required(repositories, workspace_id)
            if repositories.people.list(workspace_id) or repositories.media.list(workspace_id):
                raise DomainError("Workspace 中仍有有效人物或媒体，不能归档。")
            now = utc_now()
            repositories.workspaces.save(replace(old, deleted_at=now, updated_at=now))
