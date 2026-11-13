"""User-recorded facts and observations; no inference or relationship side effects."""
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID, uuid4
from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class Event:
    workspace_id: UUID
    title: str
    event_type: str
    record_kind: str = "FACT"
    id: UUID = field(default_factory=uuid4)
    occurred_on: date | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    location: str | None = None
    description: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self):
        for name in ("id", "workspace_id"):
            validate_uuid(getattr(self, name), name)
        for name in ("title", "event_type"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise DomainError(f"{name} must be a string")
            value = value.strip()
            validate_text(value, name, max_length=200)
            object.__setattr__(self, name, value)
        if not isinstance(self.record_kind, str) or self.record_kind not in {"FACT", "OBSERVATION"}:
            raise DomainError("record_kind must be FACT or OBSERVATION; INFERENCE is not an Event")
        if self.occurred_on is not None and (not isinstance(self.occurred_on, date) or isinstance(self.occurred_on, datetime)):
            raise DomainError("occurred_on must be a date, not a datetime")
        for name in ("started_at", "ended_at", "deleted_at"):
            object.__setattr__(self, name, normalize_datetime(getattr(self, name), name, optional=True))
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, normalize_datetime(getattr(self, name), name))
        if self.occurred_on is not None:
            if self.started_at is not None or self.ended_at is not None:
                raise DomainError("date events cannot also have start or end times")
        elif self.started_at is None:
            raise DomainError("an Event requires occurred_on or started_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise DomainError("ended_at must not precede started_at")
        for name in ("location", "description"):
            validate_text(getattr(self, name), name, optional=True, nonempty=False)
