"""Evidence domain entity: raw material supporting a recorded Event.

Evidence belongs to the fact layer. It is not analysis, inference, summary or a
personality/relationship judgement, and it never depends on Qt or SQLAlchemy.
"""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4
from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid

KINDS = ("TEXT", "IMAGE", "FILE", "LINK", "MANUAL", "OTHER")
SOURCE_TYPES = ("CHAT", "EMAIL", "DOCUMENT", "IMAGE", "FILE", "WEB", "MANUAL", "OTHER")


@dataclass(frozen=True, kw_only=True, slots=True)
class Evidence:
    workspace_id: UUID
    event_id: UUID
    kind: str
    title: str
    id: UUID = field(default_factory=uuid4)
    content_text: str | None = None
    annotation: str | None = None
    source_type: str = "MANUAL"
    source_label: str | None = None
    source_reference: str | None = None
    captured_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self):
        for name in ("id", "workspace_id", "event_id"):
            validate_uuid(getattr(self, name), name)
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise DomainError(f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(self.source_type, str) or self.source_type not in SOURCE_TYPES:
            raise DomainError(f"source_type must be one of {', '.join(SOURCE_TYPES)}")
        title = self.title
        if not isinstance(title, str):
            raise DomainError("title must be a string")
        title = title.strip()
        validate_text(title, "title", max_length=200)
        object.__setattr__(self, "title", title)
        # Original content is stored verbatim: only presence/type is validated here,
        # the kind-specific rules live in EvidenceService.
        validate_text(self.content_text, "content_text", optional=True, nonempty=False)
        validate_text(self.annotation, "annotation", optional=True, nonempty=False)
        validate_text(self.source_reference, "source_reference", optional=True, nonempty=False)
        label = self.source_label
        if label is not None:
            if not isinstance(label, str):
                raise DomainError("source_label must be a string")
            label = label.strip() or None
            validate_text(label, "source_label", max_length=300, optional=True)
        object.__setattr__(self, "source_label", label)
        object.__setattr__(self, "captured_at", normalize_datetime(self.captured_at, "captured_at", optional=True))
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "deleted_at", normalize_datetime(self.deleted_at, "deleted_at", optional=True))

    @property
    def has_original_content(self):
        return bool(self.content_text and self.content_text.strip())
