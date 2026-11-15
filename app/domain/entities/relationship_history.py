"""Immutable relationship history data, intended for append-only persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class RelationshipHistory:
    workspace_id: UUID
    relationship_id: UUID
    state: str
    effective_at: datetime
    id: UUID = field(default_factory=uuid4)
    note: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    event_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "relationship_id"):
            validate_uuid(getattr(self, name), name)
        if self.event_id is not None:
            validate_uuid(self.event_id, "event_id")
        validate_text(self.state, "state", max_length=50)
        if self.state != self.state.strip():
            raise DomainError("state must not have leading or trailing whitespace")
        validate_text(self.note, "note", optional=True, nonempty=False)
        object.__setattr__(self, "effective_at", normalize_datetime(self.effective_at, "effective_at"))
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
