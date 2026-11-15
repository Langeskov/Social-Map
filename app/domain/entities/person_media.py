"""Association between a person and media in the same workspace."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_nonnegative_int, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class PersonMedia:
    workspace_id: UUID
    person_id: UUID
    media_id: UUID
    id: UUID = field(default_factory=uuid4)
    role: str = "GALLERY"
    sort_order: int = 0
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_uuid(self.id, "id")
        validate_uuid(self.workspace_id, "workspace_id")
        validate_uuid(self.person_id, "person_id")
        validate_uuid(self.media_id, "media_id")
        if not isinstance(self.role, str) or self.role not in ("AVATAR", "GALLERY"):
            raise DomainError("role must be AVATAR or GALLERY")
        validate_nonnegative_int(self.sort_order, "sort_order")
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
