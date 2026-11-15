"""Pure relationship data; services normalize undirected endpoints and text."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid


@dataclass(frozen=True, kw_only=True, slots=True)
class Relationship:
    workspace_id: UUID
    person_a_id: UUID
    person_b_id: UUID
    relationship_type: str
    directionality: str = "UNDIRECTED"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "person_a_id", "person_b_id"):
            validate_uuid(getattr(self, name), name)
        if self.person_a_id == self.person_b_id:
            raise DomainError("person_a_id and person_b_id must be distinct")
        validate_text(self.directionality, "directionality")
        if self.directionality not in ("UNDIRECTED", "DIRECTED"):
            raise DomainError("directionality must be UNDIRECTED or DIRECTED")
        if self.directionality == "UNDIRECTED" and self.person_a_id.int >= self.person_b_id.int:
            raise DomainError("UNDIRECTED endpoints must satisfy person_a_id.int < person_b_id.int")
        validate_text(self.relationship_type, "relationship_type", max_length=200)
        if self.relationship_type != self.relationship_type.strip():
            raise DomainError("relationship_type must not have leading or trailing whitespace")
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", normalize_datetime(self.deleted_at, "deleted_at", optional=True))
