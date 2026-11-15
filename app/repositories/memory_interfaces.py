"""Memory persistence contract."""
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.entities import Memory


class MemoryRepository(Protocol):
    def get(self, workspace_id: UUID, id: UUID, *, include_deleted: bool = True) -> Memory | None: ...

    def list_by_workspace(self, workspace_id: UUID, *, person_id: UUID | None = None,
                          workspace_scoped_only: bool = False, kind: str | None = None,
                          include_deleted: bool = False, now: datetime | None = None,
                          include_expired: bool = False, limit: int | None = None) -> list[Memory]: ...

    def save(self, entity: Memory) -> Memory: ...

    def update(self, entity: Memory) -> Memory: ...
