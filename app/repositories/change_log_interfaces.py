"""ChangeLog persistence contract: append-only, workspace-scoped, no mutation."""
from typing import Protocol
from uuid import UUID

from app.domain.entities import ChangeLogEntry


class ChangeLogRepository(Protocol):
    """Append + read only. There is deliberately no update/delete method."""

    def add(self, entry: ChangeLogEntry) -> ChangeLogEntry: ...

    def get(self, workspace_id: UUID, entry_id: UUID) -> ChangeLogEntry | None: ...

    def list_by_entity(self, workspace_id: UUID, entity_type: str, entity_id: UUID, *,
                       limit: int | None = None, offset: int = 0,
                       newest_first: bool = False) -> list[ChangeLogEntry]: ...

    def list_by_parent(self, workspace_id: UUID, parent_type: str, parent_id: UUID, *,
                       limit: int | None = None, offset: int = 0) -> list[ChangeLogEntry]: ...

    def list_by_operation(self, workspace_id: UUID, operation_id: UUID) -> list[ChangeLogEntry]: ...

    def get_revision(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                     revision_no: int) -> ChangeLogEntry | None: ...

    def latest_revision(self, workspace_id: UUID, entity_type: str,
                        entity_id: UUID) -> int | None: ...
