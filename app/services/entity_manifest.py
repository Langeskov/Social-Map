"""Context Entity Manifest (Phase 13.1): what the model actually saw, entity by entity.

The manifest is the **single source of truth** for the source exposure contract:

- it records, per database entity, the context roles it played and the *actual*
  exposure levels reached (e.g. a relationship endpoint shown only by name is
  ``IDENTITY_ONLY``, not ``PROFILE``);
- it drives ``ContextSnapshot.source_ids()`` -> ``analysis_*_source`` rows, the
  ``[TYPE:<uuid>]`` markers rendered into the context, and (Phase 13.2) reference
  validation;
- it is versioned; snapshots written before Phase 13.1 have no manifest and fall
  back to the legacy scope-based source set.

AnalysisSource membership means "this entity was explicitly provided or
represented in the context". It does **not** mean its full profile was provided,
and it does **not** mean any model conclusion about it is grounded.
"""
from dataclasses import dataclass
from uuid import UUID

MANIFEST_VERSION = "1"

# ------------------------------------------------------------- entity types
PERSON = "PERSON"
RELATIONSHIP = "RELATIONSHIP"
EVENT = "EVENT"
EVIDENCE = "EVIDENCE"
MEMORY = "MEMORY"
ENTITY_TYPES = (PERSON, RELATIONSHIP, EVENT, EVIDENCE, MEMORY)
#: AnalysisSource kinds: only fact-layer entities may be cited by a model answer.
SOURCE_TYPES = (PERSON, RELATIONSHIP, EVENT, EVIDENCE)

# --------------------------------------------------------- exposure vocabulary
#: Per entity type — a single universal "exposed" level would be meaningless.
PROFILE = "PROFILE"                          # PERSON: full profile line provided
IDENTITY_ONLY = "IDENTITY_ONLY"              # PERSON: only the identity/name shown
PARTICIPANT_CONTEXT = "PARTICIPANT_CONTEXT"  # PERSON: event participant line shown
RELATIONSHIP_FACT = "RELATIONSHIP_FACT"      # RELATIONSHIP: type/direction provided
EVENT_DETAIL = "EVENT_DETAIL"                # EVENT: event detail line provided
EVENT_REFERENCE = "EVENT_REFERENCE"          # EVENT: only the link/uuid was shown
METADATA_ONLY = "METADATA_ONLY"              # EVIDENCE: metadata only, no raw text
CONTENT_INCLUDED = "CONTENT_INCLUDED"        # EVIDENCE: raw text / annotation provided
USER_CONFIRMED_CONTENT = "USER_CONFIRMED_CONTENT"  # MEMORY: confirmed content provided

EXPOSURES_BY_TYPE = {
    PERSON: (PROFILE, IDENTITY_ONLY, PARTICIPANT_CONTEXT),
    RELATIONSHIP: (RELATIONSHIP_FACT,),
    EVENT: (EVENT_DETAIL, EVENT_REFERENCE),
    EVIDENCE: (METADATA_ONLY, CONTENT_INCLUDED),
    MEMORY: (USER_CONFIRMED_CONTENT,),
}

# ----------------------------------------------------------------- context roles
ROLE_TARGET = "TARGET"
ROLE_RELATIONSHIP_ENDPOINT = "RELATIONSHIP_ENDPOINT"
ROLE_EVENT_PARTICIPANT = "EVENT_PARTICIPANT"
ROLE_PERSON_RELATIONSHIP = "PERSON_RELATIONSHIP"
ROLE_LINKED_EVENT = "LINKED_EVENT"
ROLE_EVENT_HISTORY = "EVENT_HISTORY"
ROLE_EVENT_EVIDENCE = "EVENT_EVIDENCE"
ROLE_WORKSPACE_MEMORY = "WORKSPACE_MEMORY"
ROLE_PERSON_MEMORY = "PERSON_MEMORY"
ROLES = (ROLE_TARGET, ROLE_RELATIONSHIP_ENDPOINT, ROLE_EVENT_PARTICIPANT,
         ROLE_PERSON_RELATIONSHIP, ROLE_LINKED_EVENT, ROLE_EVENT_HISTORY,
         ROLE_EVENT_EVIDENCE, ROLE_WORKSPACE_MEMORY, ROLE_PERSON_MEMORY)


def entity_marker(entity_type: str, entity_id: UUID) -> str:
    """The exact marker rendered in the context for one entity."""
    return f"[{entity_type}:{entity_id}]"


def person_exposure_note(exposures) -> str:
    """What the model must be told about a person's limited exposure."""
    values = set(exposures or ())
    if PROFILE in values:
        return ""
    if IDENTITY_ONLY in values and PARTICIPANT_CONTEXT in values:
        return "（仅身份与参与信息，未提供完整档案）"
    if IDENTITY_ONLY in values:
        return "（仅身份信息，未提供完整档案）"
    return "（参与人信息，未提供完整档案）"


def _ordered(values, order) -> tuple:
    wanted = set(values)
    return tuple(item for item in order if item in wanted)


@dataclass(frozen=True)
class EntityExposure:
    """One entity in the manifest: type, id, label, roles and exposures."""

    entity_type: str
    entity_id: UUID
    label: str = ""
    roles: tuple[str, ...] = ()
    exposures: tuple[str, ...] = ()

    def __post_init__(self):
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(f"未知实体类型：{self.entity_type!r}")
        if not isinstance(self.entity_id, UUID):
            raise ValueError("entity_id 必须是 UUID")
        object.__setattr__(self, "label", (self.label or "")[:200])
        object.__setattr__(self, "roles", _ordered(self.roles or (), ROLES))
        unknown = set(self.exposures or ()) - set(EXPOSURES_BY_TYPE[self.entity_type])
        if unknown:
            raise ValueError(f"{self.entity_type} 不支持曝光等级：{sorted(unknown)}")
        object.__setattr__(self, "exposures",
                           _ordered(self.exposures or (), EXPOSURES_BY_TYPE[self.entity_type]))

    def to_mapping(self) -> dict:
        return {"entity_type": self.entity_type, "entity_id": str(self.entity_id),
                "label": self.label, "roles": list(self.roles),
                "exposures": list(self.exposures)}


@dataclass(frozen=True)
class ContextEntityManifest:
    """The versioned manifest of one rendered context.

    Entry order is the context build order (target first, then relationships,
    endpoints, events, evidence, memories) — deterministic for the same input and
    meaningful for the UI source lists (no random-UUID ordering).
    """

    version: str = MANIFEST_VERSION
    entries: tuple[EntityExposure, ...] = ()

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("manifest.version 不能为空")
        object.__setattr__(self, "entries", tuple(self.entries))

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, entity_type: str, entity_id: UUID) -> EntityExposure | None:
        for entry in self.entries:
            if entry.entity_type == entity_type and entry.entity_id == entity_id:
                return entry
        return None

    def by_type(self, entity_type: str) -> tuple[EntityExposure, ...]:
        return tuple(item for item in self.entries if item.entity_type == entity_type)

    def source_ids(self) -> dict:
        """Fact-entity ids grouped by AnalysisSource kind (manifest-driven)."""
        return {kind: tuple(str(item.entity_id) for item in self.by_type(kind))
                for kind in SOURCE_TYPES}

    def memory_ids(self) -> tuple[str, ...]:
        return tuple(str(item.entity_id) for item in self.by_type(MEMORY))

    def to_mapping(self) -> dict:
        return {"version": self.version,
                "entries": [item.to_mapping() for item in self.entries]}

    @classmethod
    def from_mapping(cls, data) -> "ContextEntityManifest | None":
        """Rebuild a stored manifest; ``None`` when absent or malformed.

        Old snapshots (before Phase 13.1) have no manifest and keep the legacy
        scope-based source rules; a malformed manifest is never guessed at.
        """
        if not isinstance(data, dict):
            return None
        try:
            entries = []
            for item in data.get("entries", ()):
                entries.append(EntityExposure(
                    entity_type=item["entity_type"],
                    entity_id=UUID(item["entity_id"]),
                    label=item.get("label", ""),
                    roles=tuple(item.get("roles", ())),
                    exposures=tuple(item.get("exposures", ()))))
            return cls(version=str(data.get("version") or MANIFEST_VERSION),
                       entries=tuple(entries))
        except (KeyError, TypeError, ValueError):
            return None


class ManifestBuilder:
    """Mutable collector used by ContextBuilder; merges repeated entity sightings."""

    def __init__(self):
        self._entries: dict[tuple[str, UUID], dict] = {}

    def add(self, entity_type: str, entity_id: UUID, *, label: str = "",
            role: str | None = None, exposure: str | None = None) -> None:
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"未知实体类型：{entity_type!r}")
        if exposure is not None and exposure not in EXPOSURES_BY_TYPE[entity_type]:
            raise ValueError(f"{entity_type} 不支持曝光等级：{exposure!r}")
        key = (entity_type, entity_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = {"label": "", "roles": set(), "exposures": set()}
        if label and not entry["label"]:
            entry["label"] = label
        if role:
            entry["roles"].add(role)
        if exposure:
            entry["exposures"].add(exposure)

    def build(self) -> ContextEntityManifest:
        entries = tuple(
            EntityExposure(entity_type=entity_type, entity_id=entity_id,
                           label=data["label"], roles=tuple(data["roles"]),
                           exposures=tuple(data["exposures"]))
            for (entity_type, entity_id), data in self._entries.items())
        return ContextEntityManifest(entries=entries)


def unmarked_entity_ids(rendered_context: str) -> tuple[str, ...]:
    """UUIDs that appear in the context without a ``[TYPE:<uuid>]`` marker.

    Used by the evaluation runner and tests: every citable entity must be
    identifiable from the text itself, not only from the database.
    """
    import re
    text = rendered_context or ""
    markers = set()
    for match in re.finditer(
            r"\[(PERSON|RELATIONSHIP|EVENT|EVIDENCE|MEMORY):"
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]",
            text):
        markers.add(match.group(2).lower())
    found = {match.group(0).lower() for match in re.finditer(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)}
    return tuple(sorted(found - markers))
