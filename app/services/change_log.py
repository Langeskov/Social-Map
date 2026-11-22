"""Read-only ChangeLog Service: stable, workspace-scoped history queries.

Phase 14.1 provides the query foundation only. It does not record changes (Phase
14.2), does not format diffs for the UI (Phase 14.3) and does not revert (Phase
14.4). No method here writes to the database.
"""
from uuid import UUID

from app.domain.entities import ENTITY_TYPES, ChangeLogEntry
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import validate_uuid
from app.services.change_log_repositories import sql_change_log_repositories
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


def _page_args(limit: int | None, offset: int) -> tuple[int | None, int]:
    if offset is None:
        offset = 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DomainError("offset 必须是 >= 0 的整数。")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or \
                not 1 <= limit <= MAX_PAGE_SIZE:
            raise DomainError(f"limit 必须是 1..{MAX_PAGE_SIZE} 的整数。")
    return limit, offset


def _entity_args(entity_type: str, entity_id: UUID) -> None:
    if entity_type not in ENTITY_TYPES:
        raise DomainError(f"未知的实体类型：{entity_type!r}。")
    validate_uuid(entity_id, "entity_id")


class ChangeLogService:
    """Append-only ledger reads; the writer and UI layers are later phases."""

    def __init__(self, sessions, *, repositories=sql_change_log_repositories):
        self._sessions = sessions
        self._repositories = repositories

    def history(self, workspace_id: UUID, entity_type: str, entity_id: UUID, *,
                limit: int | None = DEFAULT_PAGE_SIZE, offset: int = 0
                ) -> tuple[ChangeLogEntry, ...]:
        """Entity history in revision order (oldest first); paginated by offset."""
        validate_uuid(workspace_id, "workspace_id")
        _entity_args(entity_type, entity_id)
        limit, offset = _page_args(limit, offset)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return tuple(repositories.change_log.list_by_entity(
                workspace_id, entity_type, entity_id, limit=limit, offset=offset))

    def parent_history(self, workspace_id: UUID, parent_type: str, parent_id: UUID, *,
                       limit: int | None = DEFAULT_PAGE_SIZE, offset: int = 0
                       ) -> tuple[ChangeLogEntry, ...]:
        """Rows whose aggregate parent is the given entity (e.g. a relationship's
        history entries)."""
        validate_uuid(workspace_id, "workspace_id")
        _entity_args(parent_type, parent_id)
        limit, offset = _page_args(limit, offset)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return tuple(repositories.change_log.list_by_parent(
                workspace_id, parent_type, parent_id, limit=limit, offset=offset))

    def operation(self, workspace_id: UUID, operation_id: UUID) -> tuple[ChangeLogEntry, ...]:
        """Every row produced by one user operation, in recorded order."""
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(operation_id, "operation_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return tuple(repositories.change_log.list_by_operation(
                workspace_id, operation_id))

    def revision(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                 revision_no: int) -> ChangeLogEntry:
        """One specific historical revision of an entity."""
        validate_uuid(workspace_id, "workspace_id")
        _entity_args(entity_type, entity_id)
        if isinstance(revision_no, bool) or not isinstance(revision_no, int) \
                or revision_no < 1:
            raise DomainError("revision_no 必须是 >= 1 的整数。")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            entry = repositories.change_log.get_revision(
                workspace_id, entity_type, entity_id, revision_no)
            if entry is None:
                raise NotFoundError("该实体没有指定的历史版本。")
            return entry

    def latest_revision(self, workspace_id: UUID, entity_type: str,
                        entity_id: UUID) -> int | None:
        validate_uuid(workspace_id, "workspace_id")
        _entity_args(entity_type, entity_id)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return repositories.change_log.latest_revision(
                workspace_id, entity_type, entity_id)
