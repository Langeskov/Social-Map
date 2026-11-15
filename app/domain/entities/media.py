"""Media metadata domain entity; no filesystem I/O occurs here."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import MediaValidationError
from app.domain.validation import (
    normalize_datetime, utc_now, validate_dimensions, validate_nonnegative_int,
    validate_sha256, validate_storage_path, validate_text, validate_uuid,
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Media:
    workspace_id: UUID
    sha256: str
    mime_type: str
    size_bytes: int
    storage_path: str
    id: UUID = field(default_factory=uuid4)
    width: int | None = None
    height: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_uuid(self.id, "id")
        validate_uuid(self.workspace_id, "workspace_id")
        object.__setattr__(self, "sha256", validate_sha256(self.sha256))
        validate_text(self.mime_type, "mime_type", max_length=200, error_type=MediaValidationError)
        validate_nonnegative_int(self.size_bytes, "size_bytes", error_type=MediaValidationError)
        validate_dimensions(self.width, self.height)
        object.__setattr__(self, "storage_path", validate_storage_path(self.storage_path))
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", normalize_datetime(self.deleted_at, "deleted_at", optional=True))
