"""Binding between one Evidence record and one existing Phase 3 Media."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4
from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class EvidenceMedia:
    workspace_id: UUID
    evidence_id: UUID
    media_id: UUID
    id: UUID = field(default_factory=uuid4)
    role: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        for name in ("id", "workspace_id", "evidence_id", "media_id"):
            validate_uuid(getattr(self, name), name)
        role = self.role
        if role is not None:
            if not isinstance(role, str):
                raise DomainError("role must be a string")
            role = role.strip() or None
            validate_text(role, "role", max_length=100, optional=True)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
