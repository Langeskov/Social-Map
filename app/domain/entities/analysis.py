"""Analysis domain entities: model output lives here, never in the fact layer.

An `AnalysisRun` is an immutable record of one user-triggered model call: what was
asked, which facts were shown to the model (snapshot) and what came back. Nothing
in this module can mutate a Person, Relationship, Event or Evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid

STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
TARGET_TYPES = ("PERSON", "RELATIONSHIP", "EVENT")
ANALYSIS_TYPES = ("PERSON_PROFILE", "RELATIONSHIP_ANALYSIS", "EVENT_ANALYSIS")
#: Which analysis type each target type accepts.
ANALYSIS_TYPE_FOR_TARGET = {"PERSON": "PERSON_PROFILE", "RELATIONSHIP": "RELATIONSHIP_ANALYSIS",
                            "EVENT": "EVENT_ANALYSIS"}


@dataclass(frozen=True, kw_only=True, slots=True)
class AnalysisRun:
    workspace_id: UUID
    target_type: str
    target_id: UUID
    analysis_type: str
    provider_name: str
    model_name: str
    scope: str
    id: UUID = field(default_factory=uuid4)
    status: str = "PENDING"
    context_snapshot: str | None = None
    result: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None

    def __post_init__(self):
        for name in ("id", "workspace_id", "target_id"):
            validate_uuid(getattr(self, name), name)
        if self.target_type not in TARGET_TYPES:
            raise DomainError(f"target_type 必须是 {', '.join(TARGET_TYPES)} 之一。")
        if self.analysis_type not in ANALYSIS_TYPES:
            raise DomainError(f"analysis_type 必须是 {', '.join(ANALYSIS_TYPES)} 之一。")
        if self.analysis_type != ANALYSIS_TYPE_FOR_TARGET[self.target_type]:
            raise DomainError("analysis_type 与 target_type 不匹配。")
        if self.status not in STATUSES:
            raise DomainError(f"status 必须是 {', '.join(STATUSES)} 之一。")
        for name in ("provider_name", "model_name"):
            value = getattr(self, name)
            validate_text(value, name, max_length=200)
            object.__setattr__(self, name, value.strip())
        validate_text(self.scope, "scope", max_length=20000)
        for name in ("context_snapshot", "result", "error_message"):
            validate_text(getattr(self, name), name, optional=True, nonempty=False)
        for name in ("created_at", "started_at"):
            object.__setattr__(self, name, normalize_datetime(getattr(self, name), name))
        object.__setattr__(self, "completed_at",
                           normalize_datetime(self.completed_at, "completed_at", optional=True))
        if self.duration_ms is not None and (type(self.duration_ms) is not int or self.duration_ms < 0):
            raise DomainError("duration_ms 必须是非负整数或 None。")
        if self.error_message is not None and self.status not in {"FAILED", "CANCELLED"}:
            raise DomainError("error_message 只允许出现在 FAILED 或 CANCELLED 运行中。")

    @property
    def finished(self) -> bool:
        return self.status in {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True, kw_only=True, slots=True)
class AnalysisSource:
    """One fact object referenced by an analysis run (one row in a typed link table)."""

    workspace_id: UUID
    analysis_run_id: UUID
    target_id: UUID
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        for name in ("id", "workspace_id", "analysis_run_id", "target_id"):
            validate_uuid(getattr(self, name), name)
        object.__setattr__(self, "created_at", normalize_datetime(self.created_at, "created_at"))


@dataclass(frozen=True)
class AnalysisSourceSet:
    """Sources grouped by the fact table they point at."""

    people: tuple[AnalysisSource, ...] = ()
    relationships: tuple[AnalysisSource, ...] = ()
    events: tuple[AnalysisSource, ...] = ()
    evidence: tuple[AnalysisSource, ...] = ()

    def total(self) -> int:
        return len(self.people) + len(self.relationships) + len(self.events) + len(self.evidence)

    def all(self) -> tuple[AnalysisSource, ...]:
        return self.people + self.relationships + self.events + self.evidence
