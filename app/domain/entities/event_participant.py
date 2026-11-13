"""A single person's participation in a real recorded Event."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class EventParticipant:
    workspace_id: UUID
    event_id: UUID
    person_id: UUID
    id: UUID = field(default_factory=uuid4)
    role: str | None = None
    note: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        for name in ("id", "workspace_id", "event_id", "person_id"):
            validate_uuid(getattr(self, name), name)
        validate_text(self.role, "role", max_length=200, optional=True, nonempty=False)
        validate_text(self.note, "note", optional=True, nonempty=False)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
