"""ChangeLog domain contract: append-only audit records for business writes.

The table is the single source of truth for recorded changes; a "revision" is
just the ordered view of one entity's entries. Snapshots are whitelisted JSON
documents (never ORM objects, binaries or media files) whose canonical encoding
is owned here so storage, serializer and future writers agree on one format.

No service writes here yet in Phase 14.1: the domain/repository/serializer only
establish the persistence foundation.
"""
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, validate_uuid

#: Every entity type the audit matrix can name. Alias/media links/participants are
#: collections inside their aggregate snapshot, not separate types.
ENTITY_TYPES = ("WORKSPACE", "PERSON", "RELATIONSHIP", "RELATIONSHIP_HISTORY",
                "EVENT", "EVIDENCE", "MEDIA", "MEMORY")

#: append-only action vocabulary. REVERT is written only by the Phase 14.4
#: controlled-revert path (a revert is a new business operation, never an edit).
ACTIONS = ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "IMPORT", "APPEND_HISTORY",
           "ATTACH_EVENT", "DETACH_EVENT", "REVERT")

PAYLOAD_VERSION = "1"
SUPPORTED_PAYLOAD_VERSIONS = (PAYLOAD_VERSION,)

#: Which actions each entity type may use. Kept next to the contract so the writer
#: (Phase 14.2) and the tests share one authoritative rule.
ALLOWED_ACTIONS = {
    "WORKSPACE": ("CREATE", "UPDATE", "ARCHIVE", "REVERT"),
    "PERSON": ("CREATE", "UPDATE", "ARCHIVE", "REVERT"),
    "RELATIONSHIP": ("CREATE", "ARCHIVE", "RESTORE"),
    "RELATIONSHIP_HISTORY": ("APPEND_HISTORY", "ATTACH_EVENT", "DETACH_EVENT"),
    "EVENT": ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "REVERT"),
    "EVIDENCE": ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "REVERT"),
    "MEDIA": ("IMPORT", "ARCHIVE"),
    "MEMORY": ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "REVERT"),
}

#: Actions whose before-state does not exist (nothing was there before).
_CREATE_LIKE = ("CREATE", "IMPORT")


def canonical_json(value) -> str:
    """Canonical JSON for snapshots/metadata: stable byte order for diffing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_changed_fields(fields) -> str:
    return canonical_json(list(fields))


def decode_json_object(text: str, *, where: str) -> Mapping:
    """Decode one stored JSON object; malformed payloads are explicit errors."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{where} 不是合法 JSON。") from exc
    if not isinstance(value, dict):
        raise DomainError(f"{where} 必须是 JSON 对象。")
    return value


def decode_snapshot(payload_version: str, text: str) -> Mapping | None:
    """Decode a stored snapshot for a supported payload version."""
    if text is None:
        return None
    if payload_version not in SUPPORTED_PAYLOAD_VERSIONS:
        raise DomainError(f"未知的快照版本：{payload_version!r}。")
    return decode_json_object(text, where="快照")


def _normalize_value(value):
    """Coerce one snapshot value into its stable JSON representation."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    raise DomainError(f"快照包含不支持的类型：{type(value).__name__}。")


def normalize_snapshot(value) -> dict | None:
    """Return a JSON-ready copy of one snapshot (or ``None``)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DomainError("快照必须是 JSON 对象。")
    return {str(key): _normalize_value(item) for key, item in value.items()}


@dataclass(frozen=True, kw_only=True, slots=True)
class ChangeLogEntry:
    """One append-only audit row; never mutated after insert.

    ``before_snapshot`` is ``None`` for CREATE/IMPORT; every other action records
    both sides so a reader never has to reconstruct a state by replay.
    """

    workspace_id: UUID
    entity_type: str
    entity_id: UUID
    action: str
    operation_id: UUID
    revision_no: int
    changed_fields: tuple[str, ...]
    payload_version: str
    recorded_at: datetime
    id: UUID = field(default_factory=uuid4)
    before_snapshot: Mapping | None = None
    after_snapshot: Mapping | None = None
    parent_entity_type: str | None = None
    parent_entity_id: UUID | None = None
    metadata: Mapping | None = None

    def __post_init__(self):
        for name in ("id", "workspace_id", "entity_id", "operation_id"):
            validate_uuid(getattr(self, name), name)
        if self.entity_type not in ENTITY_TYPES:
            raise DomainError(f"未知的实体类型：{self.entity_type!r}。")
        if self.action not in ACTIONS:
            raise DomainError(f"未知的操作类型：{self.action!r}。")
        if self.action not in ALLOWED_ACTIONS[self.entity_type]:
            raise DomainError(
                f"{self.entity_type} 不允许操作 {self.action}。")
        if isinstance(self.revision_no, bool) or not isinstance(self.revision_no, int) \
                or self.revision_no < 1:
            raise DomainError("revision_no 必须是 >= 1 的整数。")
        if not isinstance(self.payload_version, str) or not self.payload_version.strip():
            raise DomainError("payload_version 不能为空。")
        fields = tuple(self.changed_fields)
        if any(not isinstance(item, str) or not item.strip() for item in fields):
            raise DomainError("changed_fields 只能包含非空字段名。")
        object.__setattr__(self, "changed_fields", fields)
        before = normalize_snapshot(self.before_snapshot)
        after = normalize_snapshot(self.after_snapshot)
        object.__setattr__(self, "before_snapshot", before)
        object.__setattr__(self, "after_snapshot", after)
        if self.action in _CREATE_LIKE:
            if before is not None:
                raise DomainError(f"{self.action} 不应有修改前快照。")
            if after is None:
                raise DomainError(f"{self.action} 必须有修改后快照。")
        else:
            if before is None or after is None:
                raise DomainError(f"{self.action} 必须同时记录修改前后快照。")
        parent_type = self.parent_entity_type
        parent_id = self.parent_entity_id
        if (parent_type is None) != (parent_id is None):
            raise DomainError("parent_entity_type 与 parent_entity_id 必须同时为空或同时存在。")
        if parent_type is not None:
            if parent_type not in ENTITY_TYPES:
                raise DomainError(f"未知的父实体类型：{parent_type!r}。")
            validate_uuid(parent_id, "parent_entity_id")
        if self.metadata is not None:
            if not isinstance(self.metadata, Mapping):
                raise DomainError("metadata 必须是 JSON 对象。")
            object.__setattr__(self, "metadata",
                               {str(key): _normalize_value(item)
                                for key, item in self.metadata.items()})
        object.__setattr__(self, "recorded_at",
                           normalize_datetime(self.recorded_at, "recorded_at"))

    @property
    def created(self) -> bool:
        return self.action in _CREATE_LIKE

    def encode_before(self) -> str | None:
        return None if self.before_snapshot is None else canonical_json(self.before_snapshot)

    def encode_after(self) -> str | None:
        return None if self.after_snapshot is None else canonical_json(self.after_snapshot)

    def encode_metadata(self) -> str | None:
        return None if self.metadata is None else canonical_json(self.metadata)
