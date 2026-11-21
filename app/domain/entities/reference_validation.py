"""Validated source references (Phase 13.2).

A model answer may cite database entities. Parsing a reference (Phase 13.1) only
proves its *shape*; this module defines the deterministic verification outcomes
that prove whether the cited entity:

- exists at all, in this workspace, in this run's sources and in the Context
  Entity Manifest (i.e. it was actually exposed to the model with a recorded
  exposure level);
- is still alive, and whether the exposure was full or limited.

**Reference legality is not semantic support.** VALID_FULL/VALID_LIMITED only
mean the citation itself is legitimate; whether a sentence is actually supported
by the cited material is a human judgement.

Statuses are deliberately distinct — a single "accepted" boolean hid every
failure reason and is replaced by this vocabulary.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from uuid import UUID

#: The citation is legitimate and the entity was fully exposed in the context.
VALID_FULL = "VALID_FULL"
#: The citation is legitimate but the entity was only partially exposed
#: (e.g. IDENTITY_ONLY / PARTICIPANT_CONTEXT / METADATA_ONLY / EVENT_REFERENCE),
#: or the run predates the Context Entity Manifest.
VALID_LIMITED = "VALID_LIMITED"
#: The reference text itself is not a valid UUID (malformed citation).
INVALID_FORMAT = "INVALID_FORMAT"
#: No such object exists in any workspace.
UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
#: The object exists in this workspace but was not part of this run's sources.
NOT_IN_RUN = "NOT_IN_RUN"
#: The object belongs to a different workspace.
WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"
#: The declared entity type is wrong (e.g. a Person UUID cited as EVENT), or the
#: declared type is not a citable AnalysisSource kind at all.
TYPE_MISMATCH = "TYPE_MISMATCH"
#: The object exists but has been deleted.
DELETED_SOURCE = "DELETED_SOURCE"
#: The run has a Context Entity Manifest and this entity is absent from it, i.e.
#: it was never exposed in the context.
SOURCE_NOT_EXPOSED = "SOURCE_NOT_EXPOSED"

STATUSES = (VALID_FULL, VALID_LIMITED, INVALID_FORMAT, UNKNOWN_SOURCE, NOT_IN_RUN,
            WORKSPACE_MISMATCH, TYPE_MISMATCH, DELETED_SOURCE, SOURCE_NOT_EXPOSED)
VALID_STATUSES = (VALID_FULL, VALID_LIMITED)
#: AnalysisSource kinds a model answer may cite (fact layer only, never Memory).
CITABLE_TYPES = ("PERSON", "RELATIONSHIP", "EVENT", "EVIDENCE")

#: Source-marker syntax a model may emit; the body is captured loosely so a
#: malformed citation is classified instead of silently vanishing.
MARKER_PATTERN = re.compile(r"\[(PERSON|RELATIONSHIP|EVENT|EVIDENCE):([^\[\]\s]{1,64})\]")


def is_valid(status: str) -> bool:
    return status in VALID_STATUSES


@dataclass(frozen=True)
class ReferenceTarget:
    """Read-model returned by the repository lookup (any workspace)."""

    entity_type: str
    entity_id: UUID
    workspace_id: UUID
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class ValidatedReference:
    """One citation plus its deterministic verification outcome."""

    raw: str
    entity_type: str
    entity_id: UUID | None
    status: str
    reason: str = ""
    exposure: tuple = ()

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"未知验证状态：{self.status!r}")
        if self.entity_id is not None and not isinstance(self.entity_id, UUID):
            raise ValueError("entity_id 必须是 UUID 或 None")

    @property
    def valid(self) -> bool:
        return self.status in VALID_STATUSES

    def to_mapping(self) -> dict:
        return {"raw": self.raw, "entity_type": self.entity_type,
                "entity_id": str(self.entity_id) if self.entity_id else None,
                "status": self.status, "reason": self.reason,
                "exposure": list(self.exposure)}


@dataclass(frozen=True)
class EntryReferenceCheck:
    """One parsed reference of one result entry, with its verification."""

    section: str
    entry_index: int
    reference: ValidatedReference

    def to_mapping(self) -> dict:
        return {"section": self.section, "entry_index": self.entry_index,
                "reference": self.reference.to_mapping()}
