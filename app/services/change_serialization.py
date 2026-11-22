"""Whitelisted, versioned snapshot serialization for ChangeLog.

Phase 14.0/14.1 own the data contract: only the fields named below may ever enter
a snapshot; collection members are recorded as ordered lists inside the aggregate
snapshot (Alias/PersonMedia/EventParticipant/EvidenceMedia are not separate
entities). Encoding is canonical JSON so two snapshots can be compared byte-wise
and diffed deterministically.

This module does not read the database and does not decide when to record: the
writer (Phase 14.2) passes domain objects and collections in.
"""
from collections.abc import Mapping

from app.domain.entities import (ENTITY_TYPES, PAYLOAD_VERSION, canonical_json,
                                 decode_snapshot, normalize_snapshot)
from app.domain.errors import DomainError

#: Ordered business field whitelist per entity type. Order is the diff/display order.
SNAPSHOT_FIELDS = {
    "WORKSPACE": ("name", "description"),
    "PERSON": ("display_name", "real_name", "gender", "birthday", "occupation",
               "organization", "location", "notes"),
    "RELATIONSHIP": ("person_a_id", "person_b_id", "relationship_type",
                     "directionality"),
    "RELATIONSHIP_HISTORY": ("relationship_id", "state", "effective_at", "note",
                             "event_id"),
    "EVENT": ("title", "event_type", "record_kind", "occurred_on", "started_at",
              "ended_at", "location", "description"),
    "EVIDENCE": ("title", "annotation"),
    "MEDIA": ("sha256", "mime_type", "size_bytes", "width", "height"),
    "MEMORY": ("person_id", "kind", "content", "source_type", "source_analysis_id",
               "valid_from", "valid_until"),
}

#: Collection snapshots allowed per entity type (empty when none).
COLLECTION_FIELDS = {
    "PERSON": ("aliases", "images"),
    "EVENT": ("participants",),
    "EVIDENCE": ("media",),
}

#: Fields captured for each collection item, in canonical order. The tuple is
#: also the deterministic sort key (see design §9).
COLLECTION_ITEM_FIELDS = {
    "aliases": ("alias", "alias_type", "note"),
    "images": ("role", "sort_order", "media_id"),
    "participants": ("person_id", "role", "note"),
    "media": ("media_id", "role"),
}


def snapshot_field_names(entity_type: str) -> tuple[str, ...]:
    """All snapshot keys (business fields then collections) for one entity type."""
    if entity_type not in SNAPSHOT_FIELDS:
        raise DomainError(f"未知的实体类型：{entity_type!r}。")
    return SNAPSHOT_FIELDS[entity_type] + COLLECTION_FIELDS.get(entity_type, ())


def _attribute(entity, name: str):
    try:
        return getattr(entity, name)
    except AttributeError as exc:
        raise DomainError(
            f"{type(entity).__name__} 缺少快照字段 {name!r}。") from exc


def _sorted_collection(collection: str, items) -> list[dict]:
    fields = COLLECTION_ITEM_FIELDS[collection]
    normalized = [
        normalize_snapshot({field: _attribute(item, field) for field in fields})
        for item in items
    ]

    def key(item):
        # None sorts first per field; values within one field share one type.
        return tuple((item[field] is None, "" if item[field] is None else item[field])
                     for field in fields)

    return sorted(normalized, key=key)


class ChangeSnapshotSerializer:
    """Build/compare canonical snapshots; no persistence side effects."""

    def snapshot(self, entity_type: str, entity, *, aliases=(), images=(),
                 participants=(), media=()) -> dict:
        if entity_type not in SNAPSHOT_FIELDS:
            raise DomainError(f"未知的实体类型：{entity_type!r}。")
        supplied = {"aliases": tuple(aliases or ()), "images": tuple(images or ()),
                    "participants": tuple(participants or ()), "media": tuple(media or ())}
        supported = COLLECTION_FIELDS.get(entity_type, ())
        for name, items in supplied.items():
            if name not in supported and items:
                raise DomainError(f"{entity_type} 快照不支持集合 {name}。")
        snapshot = {field: _attribute(entity, field) for field in SNAPSHOT_FIELDS[entity_type]}
        for name in supported:
            snapshot[name] = _sorted_collection(name, supplied[name])
        # normalize_snapshot validates the JSON-ready types (datetime/date/UUID/None).
        return normalize_snapshot(snapshot)

    def changed_fields(self, entity_type: str, before: Mapping,
                       after: Mapping) -> tuple[str, ...]:
        names = snapshot_field_names(entity_type)
        for label, value in (("修改前", before), ("修改后", after)):
            self._require_complete(entity_type, value, label)
        return tuple(name for name in names if before.get(name) != after.get(name))

    @staticmethod
    def _require_complete(entity_type: str, snapshot, label: str) -> None:
        if not isinstance(snapshot, Mapping):
            raise DomainError(f"{label}快照必须是 JSON 对象。")
        expected = set(snapshot_field_names(entity_type))
        actual = set(snapshot)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise DomainError(
                f"{label}快照字段与 {entity_type} 契约不一致；"
                f"缺少 {missing}，未知 {unknown}。")

    @staticmethod
    def encode(value) -> str:
        return canonical_json(normalize_snapshot(value))

    def decode(self, payload_version: str, text: str) -> dict | None:
        return decode_snapshot(payload_version, text)

    @staticmethod
    def payload_version() -> str:
        return PAYLOAD_VERSION
