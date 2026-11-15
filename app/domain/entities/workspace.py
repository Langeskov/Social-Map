"""Workspace domain entity."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class Workspace:
    name: str
    id: UUID = field(default_factory=uuid4)
    description: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_uuid(self.id, "id")
        validate_text(self.name, "name", max_length=200)
        validate_text(self.description, "description", optional=True, nonempty=False)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", normalize_datetime(self.deleted_at, "deleted_at", optional=True))
