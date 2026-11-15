"""Person domain entity."""

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID, uuid4

from app.domain.validation import normalize_datetime, utc_now, validate_birthday, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class Person:
    workspace_id: UUID
    display_name: str
    id: UUID = field(default_factory=uuid4)
    real_name: str | None = None
    gender: str | None = None
    birthday: date | None = None
    occupation: str | None = None
    organization: str | None = None
    location: str | None = None
    notes: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_uuid(self.id, "id")
        validate_uuid(self.workspace_id, "workspace_id")
        validate_text(self.display_name, "display_name", max_length=200)
        for name, maximum in (("real_name", 200), ("gender", 100), ("occupation", 300), ("organization", 300), ("location", 300)):
            validate_text(getattr(self, name), name, max_length=maximum, optional=True, nonempty=False)
        validate_text(self.notes, "notes", optional=True, nonempty=False)
        validate_birthday(self.birthday)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", normalize_datetime(self.deleted_at, "deleted_at", optional=True))
