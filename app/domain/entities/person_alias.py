"""Person alias domain entity."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class PersonAlias:
    workspace_id: UUID
    person_id: UUID
    alias: str
    id: UUID = field(default_factory=uuid4)
    alias_type: str = "OTHER"
    note: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_uuid(self.id, "id")
        validate_uuid(self.workspace_id, "workspace_id")
        validate_uuid(self.person_id, "person_id")
        validate_text(self.alias, "alias", max_length=300)
        validate_text(self.alias_type, "alias_type", max_length=100)
        validate_text(self.note, "note", optional=True, nonempty=False)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
