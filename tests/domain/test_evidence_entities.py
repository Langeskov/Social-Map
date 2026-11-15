"""Pure Phase 6 domain rules: create-once material, separated annotation, UTC."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from app.domain.entities import Evidence, EvidenceMedia
from app.domain.entities.evidence import KINDS, SOURCE_TYPES
from app.domain.errors import DomainError

NOW = datetime(2026, 10, 4, 9, 30, 0, 123456, tzinfo=timezone(timedelta(hours=8)))


def evidence(**changes):
    values = dict(workspace_id=uuid4(), event_id=uuid4(), kind="TEXT", title="  聊天记录  ",
                  content_text="原始聊天文字……", source_type="CHAT")
    values.update(changes)
    return Evidence(**values)


def test_exact_fields_defaults_and_trim():
    entity = evidence()
    assert entity.title == "聊天记录" and entity.source_type == "CHAT" and entity.kind == "TEXT"
    assert entity.captured_at is None and entity.deleted_at is None and entity.annotation is None
    assert {f.name for f in fields(Evidence)} == {
        "id", "workspace_id", "event_id", "kind", "title", "content_text", "annotation", "source_type",
        "source_label", "source_reference", "captured_at", "created_at", "updated_at", "deleted_at"}
    assert {f.name for f in fields(EvidenceMedia)} == {
        "id", "workspace_id", "evidence_id", "media_id", "role", "created_at"}
    with pytest.raises(AttributeError):
        entity.title = "changed"


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_is_representable(kind):
    assert evidence(kind=kind).kind == kind


@pytest.mark.parametrize("kind", ["INFERENCE", "SUMMARY", "text", " TEXT ", "", None, 42])
def test_unknown_kind_rejected(kind):
    with pytest.raises(DomainError):
        evidence(kind=kind)


@pytest.mark.parametrize("source", SOURCE_TYPES)
def test_every_source_type_is_representable(source):
    assert evidence(source_type=source).source_type == source


@pytest.mark.parametrize("source", ["AI", "chat", "WECHAT", "", None, 1])
def test_unknown_source_type_rejected(source):
    with pytest.raises(DomainError):
        evidence(source_type=source)


@pytest.mark.parametrize("value", ["", "   ", "x" * 201, None, 3])
def test_title_rules(value):
    with pytest.raises(DomainError):
        evidence(title=value)


def test_source_label_normalisation_and_boundaries():
    assert evidence(source_label="  WeChat 群  ").source_label == "WeChat 群"
    assert evidence(source_label="   ").source_label is None
    assert evidence(source_label="x" * 300).source_label == "x" * 300
    with pytest.raises(DomainError):
        evidence(source_label="x" * 301)


def test_original_content_and_annotation_are_separate():
    entity = evidence(content_text="原文", annotation="我的说明")
    assert entity.content_text == "原文" and entity.annotation == "我的说明"
    assert replace(entity, annotation=None).content_text == "原文"
    for name in ("content_text", "annotation", "source_reference"):
        with pytest.raises(DomainError):
            evidence(**{name: 12})


def test_content_text_is_stored_verbatim():
    raw = "  保留   原始空白\n第二行  "
    assert evidence(content_text=raw).content_text == raw


@pytest.mark.parametrize("name", ["id", "workspace_id", "event_id"])
@pytest.mark.parametrize("value", [None, True, "uuid", 7])
def test_evidence_uuid_fields(name, value):
    with pytest.raises(DomainError):
        evidence(**{name: value})


def test_captured_at_is_utc_and_naive_rejected():
    assert evidence(captured_at=NOW).captured_at == NOW.astimezone(timezone.utc)
    assert evidence(captured_at=NOW).captured_at.tzinfo is timezone.utc
    for name in ("captured_at", "created_at", "updated_at", "deleted_at"):
        with pytest.raises(DomainError):
            evidence(**{name: NOW.replace(tzinfo=None)})


def test_evidence_media_rules():
    link = EvidenceMedia(workspace_id=uuid4(), evidence_id=uuid4(), media_id=uuid4(),
                         role="  截图  ", created_at=NOW)
    assert link.role == "截图" and link.created_at.tzinfo is timezone.utc
    assert replace(link, role="   ").role is None
    with pytest.raises(DomainError):
        replace(link, role="x" * 101)
    for name in ("id", "workspace_id", "evidence_id", "media_id"):
        values = dict(workspace_id=uuid4(), evidence_id=uuid4(), media_id=uuid4())
        values[name] = None
        with pytest.raises(DomainError):
            EvidenceMedia(**values)
    with pytest.raises(DomainError):
        replace(link, created_at=NOW.replace(tzinfo=None))
