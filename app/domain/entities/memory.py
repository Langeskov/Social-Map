"""Memory domain entity: user-confirmed long-term semantics, never a fact.

A `Memory` exists only because a user explicitly confirmed it. It can be created
by hand (`MANUAL`) or accepted from a specific analysis (`ANALYSIS`, keeping the
run it came from), and it never modifies Person, Relationship, Event or Evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import (normalize_datetime, utc_now, validate_text,
                                   validate_uuid)

KINDS = ("FACT", "PREFERENCE", "CONTEXT")
SOURCE_TYPES = ("MANUAL", "ANALYSIS")
CONTENT_MAX_LENGTH = 4000


@dataclass(frozen=True, kw_only=True, slots=True)
class Memory:
    workspace_id: UUID
    content: str
    kind: str
    source_type: str
    confirmed_at: datetime
    id: UUID = field(default_factory=uuid4)
    person_id: UUID | None = None
    source_analysis_id: UUID | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self):
        validate_uuid(self.id, "id")
        validate_uuid(self.workspace_id, "workspace_id")
        if self.kind not in KINDS:
            raise DomainError(f"kind 必须是 {', '.join(KINDS)} 之一。")
        if self.source_type not in SOURCE_TYPES:
            raise DomainError(f"source_type 必须是 {', '.join(SOURCE_TYPES)} 之一。")
        validate_text(self.content, "content", max_length=CONTENT_MAX_LENGTH)
        text = self.content.strip()
        if not text:
            raise DomainError("Memory content 不能为空。")
        object.__setattr__(self, "content", text)
        if self.person_id is not None:
            validate_uuid(self.person_id, "person_id")
        if self.source_analysis_id is not None:
            validate_uuid(self.source_analysis_id, "source_analysis_id")
        if self.source_type == "MANUAL" and self.source_analysis_id is not None:
            raise DomainError("手工 Memory 不能引用 AnalysisRun。")
        if self.source_type == "ANALYSIS" and self.source_analysis_id is None:
            raise DomainError("来自分析的 Memory 必须记录 source_analysis_id。")
        for name in ("confirmed_at", "created_at", "updated_at"):
            object.__setattr__(self, name, normalize_datetime(getattr(self, name), name))
        for name in ("valid_from", "valid_until", "deleted_at"):
            object.__setattr__(self, name,
                               normalize_datetime(getattr(self, name), name, optional=True))
        if self.valid_from is not None and self.valid_until is not None \
                and self.valid_until < self.valid_from:
            raise DomainError("valid_until 不能早于 valid_from。")

    @property
    def workspace_scoped(self) -> bool:
        return self.person_id is None

    @property
    def from_analysis(self) -> bool:
        return self.source_type == "ANALYSIS"

    def is_current(self, *, now: datetime | None = None) -> bool:
        """Deleted, not-yet-valid and expired memories are not *current*."""
        moment = normalize_datetime(now, "now") if now is not None else utc_now()
        if self.deleted_at is not None:
            return False
        if self.valid_from is not None and self.valid_from > moment:
            return False
        if self.valid_until is not None and self.valid_until <= moment:
            return False
        return True


@dataclass(frozen=True, slots=True)
class CandidateReference:
    """A source marker found in a model answer.

    Candidates are *never* trusted: `accepted` is only true when the referenced
    object exists, belongs to the same workspace and was part of this run's
    recorded sources.
    """

    raw: str
    entity_type: str
    entity_id: UUID | None = None
    accepted: bool = False
    reason: str = ""
