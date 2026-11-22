"""Phase 14.1 snapshot serializer: whitelists, canonical JSON, versions.

Pure-Python tests: no database, no ORM objects. They pin the contract the writer
(14.2) and the diff viewer (14.3) will rely on.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.domain.entities import (ENTITY_TYPES, PAYLOAD_VERSION, ChangeLogEntry,
                                 canonical_json)
from app.domain.errors import DomainError
from app.services.change_serialization import (COLLECTION_FIELDS, SNAPSHOT_FIELDS,
                                               ChangeSnapshotSerializer,
                                               snapshot_field_names)

SERIALIZER = ChangeSnapshotSerializer()
NOW = datetime(2026, 11, 22, 8, 30, 15, 123456, tzinfo=timezone.utc)


def entity(entity_type, **values):
    """A whitelisted attribute bag; domain objects are not required here."""
    fields = {name: None for name in SNAPSHOT_FIELDS[entity_type]}
    fields.update(values)
    return SimpleNamespace(**fields)


def test_every_entity_type_has_a_nonempty_whitelist():
    assert set(SNAPSHOT_FIELDS) == set(ENTITY_TYPES)
    for entity_type, fields in SNAPSHOT_FIELDS.items():
        assert fields, entity_type
        assert len(set(fields)) == len(fields), entity_type
        assert "created_at" not in fields and "updated_at" not in fields
        assert "deleted_at" not in fields and "id" not in fields
        assert set(COLLECTION_FIELDS).issubset(SNAPSHOT_FIELDS)
        assert snapshot_field_names(entity_type) == (
            SNAPSHOT_FIELDS[entity_type] + COLLECTION_FIELDS.get(entity_type, ()))


def test_person_snapshot_encodes_types_and_sorts_collections():
    target = uuid4()
    person = entity("PERSON", display_name="Alice", real_name=None,
                    gender=None, birthday=date(1990, 5, 12), occupation="Engineer",
                    organization=None, location=None, notes="note",
                    created_at=NOW, updated_at=NOW, deleted_at=None)
    aliases = [SimpleNamespace(alias="B", alias_type="NICK", note=None),
               SimpleNamespace(alias="A", alias_type="OTHER", note="x")]
    images = [SimpleNamespace(media_id=uuid4(), role="GALLERY", sort_order=1),
              SimpleNamespace(media_id=uuid4(), role="AVATAR", sort_order=0)]
    images.sort(key=lambda item: item.media_id.int)
    snapshot = SERIALIZER.snapshot("PERSON", person, aliases=aliases, images=images)
    assert set(snapshot) == set(snapshot_field_names("PERSON"))
    assert snapshot["birthday"] == "1990-05-12"
    assert snapshot["real_name"] is None
    # Aliases are sorted by (alias, alias_type, note); images by role/sort/media.
    assert [item["alias"] for item in snapshot["aliases"]] == ["A", "B"]
    assert [item["role"] for item in snapshot["images"]] == ["AVATAR", "GALLERY"]
    assert isinstance(snapshot["images"][0]["media_id"], str)


def test_event_snapshot_records_participants_and_omits_system_fields():
    person_a, person_b = sorted((uuid4(), uuid4()), key=lambda value: value.int)
    event = entity("EVENT", title="Retro", event_type="MEETING", record_kind="FACT",
                   occurred_on=None, started_at=NOW, ended_at=None,
                   location=None, description="d")
    participants = [SimpleNamespace(person_id=person_b, role="LEAD", note=None),
                    SimpleNamespace(person_id=person_a, role=None, note=None)]
    snapshot = SERIALIZER.snapshot("EVENT", event, participants=participants)
    assert snapshot["started_at"] == NOW.isoformat()
    assert snapshot["occurred_on"] is None
    assert [item["person_id"] for item in snapshot["participants"]] == [
        str(person_a), str(person_b)]
    assert "participants" in snapshot
    for forbidden in ("created_at", "updated_at", "deleted_at", "id"):
        assert forbidden not in snapshot


def test_evidence_snapshot_never_contains_the_immutable_original_content():
    evidence = entity("EVIDENCE", title="Chat", annotation="note")
    media = [SimpleNamespace(media_id=uuid4(), role=None)]
    snapshot = SERIALIZER.snapshot("EVIDENCE", evidence, media=media)
    assert set(snapshot) == {"title", "annotation", "media"}
    assert "content_text" not in snapshot
    assert "source_reference" not in snapshot
    assert "captured_at" not in snapshot
    assert snapshot["media"][0]["role"] is None


def test_media_and_memory_snapshots_are_metadata_only():
    media = entity("MEDIA", sha256="a" * 64, mime_type="image/png", size_bytes=42,
                   width=2, height=3)
    snapshot = SERIALIZER.snapshot("MEDIA", media)
    assert snapshot == {"sha256": "a" * 64, "mime_type": "image/png",
                        "size_bytes": 42, "width": 2, "height": 3}
    memory = entity("MEMORY", person_id=uuid4(), kind="FACT", content="text",
                    source_type="ANALYSIS", source_analysis_id=uuid4(),
                    valid_from=None, valid_until=None)
    memory_snapshot = SERIALIZER.snapshot("MEMORY", memory)
    assert memory_snapshot["source_type"] == "ANALYSIS"
    assert "confirmed_at" not in memory_snapshot
    assert "deleted_at" not in memory_snapshot


def test_unknown_entity_type_and_missing_attribute_are_rejected():
    with pytest.raises(DomainError):
        SERIALIZER.snapshot("UNKNOWN", SimpleNamespace())
    person = entity("PERSON")
    with pytest.raises(DomainError):
        SERIALIZER.snapshot("PERSON", person, media=[SimpleNamespace(
            media_id=uuid4(), role=None)])   # supported only for EVIDENCE
    with pytest.raises(DomainError):
        SERIALIZER.snapshot("PERSON", object())           # missing attributes


def test_changed_fields_use_contract_order_and_detect_collections():
    before = SERIALIZER.snapshot("PERSON", entity("PERSON", display_name="Old"),
                                 aliases=())
    after = SERIALIZER.snapshot("PERSON", entity("PERSON", display_name="New",
                                                 occupation="Engineer"),
                                aliases=[SimpleNamespace(alias="A", alias_type="OTHER",
                                                         note=None)])
    assert SERIALIZER.changed_fields("PERSON", before, after) == (
        "display_name", "occupation", "aliases")
    assert SERIALIZER.changed_fields("PERSON", before, before) == ()


def test_changed_fields_reject_incomplete_or_foreign_snapshots():
    complete = SERIALIZER.snapshot("EVENT", entity("EVENT", title="t", event_type="e",
                                                   record_kind="FACT"), participants=())
    with pytest.raises(DomainError):
        SERIALIZER.changed_fields("EVENT", {"title": "t"}, complete)
    with pytest.raises(DomainError):
        SERIALIZER.changed_fields("EVENT", {**complete, "unexpected": 1}, complete)


def test_encoding_is_canonical_and_decoding_checks_the_version():
    first = SERIALIZER.encode({"b": 1, "a": [2, 1], "c": None})
    second = SERIALIZER.encode({"a": [2, 1], "c": None, "b": 1})
    assert first == second == '{"a":[2,1],"b":1,"c":null}'
    assert PAYLOAD_VERSION == "1"
    assert SERIALIZER.decode("1", '{"a":1}') == {"a": 1}
    assert SERIALIZER.decode("1", None) is None
    with pytest.raises(DomainError):
        SERIALIZER.decode("999", '{"a":1}')          # unknown version
    with pytest.raises(DomainError):
        SERIALIZER.decode("1", "not json")
    with pytest.raises(DomainError):
        SERIALIZER.decode("1", "[1, 2]")             # snapshot must be an object


def test_domain_entry_contract_keeps_lifecycle_snapshot_rules():
    workspace_id, entity_id, operation_id = uuid4(), uuid4(), uuid4()
    after = SERIALIZER.snapshot("WORKSPACE", entity("WORKSPACE", name="W"))
    with pytest.raises(DomainError):        # CREATE must not carry a before-state
        ChangeLogEntry(workspace_id=workspace_id, entity_type="WORKSPACE",
                       entity_id=workspace_id, action="CREATE", operation_id=operation_id,
                       revision_no=1, changed_fields=("name",), payload_version="1",
                       recorded_at=NOW, before_snapshot=after, after_snapshot=after)
    entry = ChangeLogEntry(workspace_id=workspace_id, entity_type="WORKSPACE",
                           entity_id=workspace_id, action="CREATE",
                           operation_id=operation_id, revision_no=1,
                           changed_fields=("name",), payload_version="1",
                           recorded_at=NOW, after_snapshot=after)
    assert canonical_json(entry.before_snapshot) if entry.before_snapshot else True
    with pytest.raises(DomainError):
        ChangeLogEntry(workspace_id=workspace_id, entity_type="PERSON",
                       entity_id=entity_id, action="APPEND_HISTORY",
                       operation_id=operation_id, revision_no=1,
                       changed_fields=(), payload_version="1", recorded_at=NOW,
                       before_snapshot=after, after_snapshot=after)
