"""Phase 6 Evidence service: kind rules, immutability, atomicity and conflicts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import pytest
from PIL import Image
from sqlalchemy import event, func, select
from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.evidence import EvidenceService, MediaInput
from app.services.repositories import sql_repositories
from app.storage.models import EvidenceMediaModel, EvidenceModel
from app.storage.session import ThreadBoundSession

NOW = datetime(2026, 10, 4, 9, 30, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "evidence-data")
    runtime = initialize_database(config)
    try:
        services = create_application_services(config, runtime.sessions)
        workspace = services.initial_workspace
        foreign = services.workspaces.create("Other workspace")
        event = services.events.create_event(workspace.id, "共同活动", "Meeting", "FACT", occurred_on=date(2026, 10, 4))
        observation = services.events.create_event(workspace.id, "我的感受", "Observation", "OBSERVATION",
                                                   occurred_on=date(2026, 10, 5))
        foreign_event = services.events.create_event(foreign.id, "外部事件", "Meeting", occurred_on=date(2026, 10, 4))
        yield SimpleEnv(config, runtime, services, workspace.id, foreign.id, event, observation, foreign_event, tmp_path)
    finally:
        runtime.close()


class SimpleEnv:
    def __init__(self, config, runtime, services, ws, foreign, event, observation, foreign_event, tmp_path):
        self.config, self.runtime, self.services = config, runtime, services
        self.ws, self.foreign = ws, foreign
        self.event, self.observation, self.foreign_event = event, observation, foreign_event
        self.tmp_path = tmp_path

    @property
    def evidence(self) -> EvidenceService:
        return self.services.evidence


def make_images(env, count=2):
    return [env.services.media.import_file(env.ws, _write_image(env, index)) for index in range(count)]


def _write_image(env, index=0):
    originals = env.tmp_path / "originals"
    originals.mkdir(exist_ok=True)
    path = originals / f"evidence-{uuid4().hex}.png"
    Image.new("RGB", (6 + index, 5 + index), (10 + index, 40, 90)).save(path, "PNG")
    return path


def make_file(env, name="record.txt", content=b"raw file payload"):
    path = env.tmp_path / name
    path.write_bytes(content)
    return env.services.media.import_file(env.ws, path), path


def text_evidence(env, **changes):
    values = dict(kind="TEXT", title="聊天记录", content_text="原始聊天文字……", source_type="CHAT")
    values.update(changes)
    return env.evidence.create_evidence(env.ws, values.pop("event_id", env.event.id), **values)


def counts(env):
    with env.runtime.sessions() as session, session.begin():
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (EvidenceModel, EvidenceMediaModel))


def test_crud_and_detached_snapshots(env):
    entity = text_evidence(env)
    assert entity.kind == "TEXT" and entity.title == "聊天记录" and entity.source_type == "CHAT"
    detail = env.evidence.detail(env.ws, entity.id)
    assert detail.evidence == entity and detail.event == env.event and detail.media == ()
    assert env.evidence.get(env.ws, entity.id) == entity
    assert [item.evidence.id for item in env.evidence.list_evidence(env.ws, env.event.id)] == [entity.id]
    assert counts(env) == (1, 0)
    # Original content survives a fresh read.
    assert env.evidence.get(env.ws, entity.id).content_text == "原始聊天文字……"


@pytest.mark.parametrize("kind,extra", [("TEXT", dict(content_text="原文")),
                                        ("MANUAL", dict(content_text="手工记录")),
                                        ("OTHER", dict(content_text=None))])
def test_text_like_kinds(env, kind, extra):
    entity = text_evidence(env, kind=kind, **extra)
    assert entity.kind == kind


def test_image_and_file_kinds_require_media(env):
    images = make_images(env, 2)
    image_evidence = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "现场照片",
        source_type="IMAGE", media=(images[0].id,))
    assert image_evidence.kind == "IMAGE"
    file_media, _ = make_file(env)
    file_evidence = env.evidence.create_evidence(env.ws, env.event.id, "FILE", "聊天导出",
        source_type="FILE", media=(MediaInput(file_media.id, "导出文件"),))
    detail = env.evidence.detail(env.ws, file_evidence.id)
    assert detail.media[0].link.role == "导出文件" and detail.media[0].media.id == file_media.id
    with pytest.raises(DomainError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "没有附件")
    with pytest.raises(DomainError):
        env.evidence.create_evidence(env.ws, env.event.id, "FILE", "没有附件")
    assert counts(env) == (2, 2)


def test_link_kind_requires_source_reference(env):
    entity = env.evidence.create_evidence(env.ws, env.event.id, "LINK", "相关网页",
        source_type="WEB", source_reference="https://example.invalid/thread/1", source_label="论坛")
    assert entity.source_reference.startswith("https://")
    with pytest.raises(DomainError):
        env.evidence.create_evidence(env.ws, env.event.id, "LINK", "缺少引用", source_type="WEB")
    with pytest.raises(DomainError):
        env.evidence.create_evidence(env.ws, env.event.id, "LINK", "空白引用", source_type="WEB",
                                     source_reference="   ")
    assert counts(env) == (1, 0)


def test_text_kind_requires_content(env):
    for value in (None, "", "   "):
        with pytest.raises(DomainError):
            env.evidence.create_evidence(env.ws, env.event.id, "TEXT", "缺少原文", content_text=value)
    assert counts(env) == (0, 0)


def test_fact_and_observation_events_both_accept_evidence(env):
    fact = text_evidence(env)
    observation = env.evidence.create_evidence(env.ws, env.observation.id, "MANUAL", "我的观察记录",
        content_text="我感觉对方更主动", annotation="活动当天的感受")
    assert env.evidence.list_evidence(env.ws, env.observation.id)[0].event.record_kind == "OBSERVATION"
    assert env.evidence.get(env.ws, observation.id).event_id == env.observation.id
    assert len(env.evidence.list_evidence(env.ws, env.event.id)) == 1
    assert env.evidence.get(env.ws, fact.id).event_id == env.event.id


def test_cross_workspace_objects_are_rejected_by_service(env):
    foreign_media = env.services.media.import_file(env.foreign, _write_image(env))
    with pytest.raises(WorkspaceMismatchError):
        env.evidence.create_evidence(env.foreign, env.event.id, "TEXT", "错域", content_text="原文")
    with pytest.raises(WorkspaceMismatchError):
        env.evidence.create_evidence(env.ws, env.foreign_event.id, "TEXT", "错域", content_text="原文")
    with pytest.raises(WorkspaceMismatchError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "错域媒体", media=(foreign_media.id,))
    entity = text_evidence(env)
    with pytest.raises(WorkspaceMismatchError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "不存在媒体", media=(uuid4(),))
    with pytest.raises(NotFoundError):
        env.evidence.get(env.foreign, entity.id)
    assert counts(env) == (1, 0)


def test_deleted_event_blocks_new_evidence_but_keeps_existing(env):
    entity = text_evidence(env)
    env.services.events.soft_delete_event(env.ws, env.event.id)
    with pytest.raises(DomainError):
        env.evidence.create_evidence(env.ws, env.event.id, "TEXT", "新证据", content_text="原文")
    # Existing evidence stays fully auditable and its metadata remains editable.
    assert env.evidence.get(env.ws, entity.id) == entity
    assert env.evidence.list_evidence(env.ws, env.event.id) == []
    assert len(env.evidence.list_evidence(env.ws, env.event.id, include_deleted=True)) == 1
    updated = env.evidence.update_metadata(env.ws, entity.id, annotation="事件删除后补充的说明")
    assert updated.content_text == entity.content_text
    env.services.events.restore_event(env.ws, env.event.id)
    visible = env.evidence.list_evidence(env.ws, env.event.id)
    assert len(visible) == 1 and visible[0].evidence.annotation == "事件删除后补充的说明"
    assert env.evidence.get(env.ws, entity.id).content_text == "原始聊天文字……"


def test_soft_delete_and_restore_evidence_only(env):
    images = make_images(env, 1)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(images[0].id,))
    deleted = env.evidence.soft_delete_evidence(env.ws, entity.id)
    assert deleted.deleted_at is not None and deleted.updated_at > entity.updated_at
    assert env.evidence.soft_delete_evidence(env.ws, entity.id) == deleted
    with pytest.raises(NotFoundError):
        env.evidence.get(env.ws, entity.id)
    detail = env.evidence.detail(env.ws, entity.id, include_deleted=True)
    assert detail.evidence == deleted and len(detail.media) == 1
    assert env.services.media.get(env.ws, images[0].id).deleted_at is None
    restored = env.evidence.restore_evidence(env.ws, entity.id)
    assert restored.deleted_at is None and restored.updated_at > deleted.updated_at
    assert restored.content_text == entity.content_text and restored.kind == entity.kind
    assert env.evidence.restore_evidence(env.ws, entity.id) == restored
    assert env.services.events.get(env.ws, env.event.id) == env.event
    assert counts(env) == (1, 1)


def test_delete_evidence_never_deletes_media_file(env):
    media, path = make_file(env)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "FILE", "文件", source_type="FILE",
                                          media=(media.id,))
    original = env.services.media.original_path(env.ws, media.id)
    before = original.read_bytes()
    env.evidence.soft_delete_evidence(env.ws, entity.id)
    assert original.exists() and original.read_bytes() == before
    assert env.services.media.get(env.ws, media.id).deleted_at is None
    restored = env.evidence.restore_evidence(env.ws, entity.id)
    assert env.evidence.detail(env.ws, restored.id).media[0].media.id == media.id
    assert path.read_bytes() == b"raw file payload"


def test_media_soft_delete_requires_no_binding_at_all(env):
    """Phase 7 rule: evidence soft delete keeps its bindings, so it keeps the media."""
    media, _ = make_file(env)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "TEXT", "文字", content_text="原文",
                                          media=(media.id,))
    with pytest.raises(ConflictError):
        env.services.media.soft_delete(env.ws, media.id)
    # Soft deleting the Evidence deliberately keeps EvidenceMedia, so the media is
    # still referenced and cannot be deleted.
    env.evidence.soft_delete_evidence(env.ws, entity.id)
    with pytest.raises(ConflictError):
        env.services.media.soft_delete(env.ws, media.id)
    # Only removing the binding itself releases the media.
    env.evidence.restore_evidence(env.ws, entity.id)
    env.evidence.remove_media(env.ws, entity.id, media.id)
    env.services.media.soft_delete(env.ws, media.id)
    with env.runtime.sessions() as session, session.begin():
        stored = sql_repositories(session).media.get(env.ws, media.id, include_deleted=True)
        assert stored is not None and stored.deleted_at is not None
    # A deleted media cannot be attached to another evidence afterwards.
    with pytest.raises(WorkspaceMismatchError):
        env.evidence.add_media(env.ws, entity.id, media.id)


def test_multiple_evidence_and_multiple_media_with_reuse(env):
    media = make_images(env, 3)
    first = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "第一张",
        source_type="IMAGE", media=(media[0].id, media[1].id))
    second = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "复用的照片",
        source_type="IMAGE", media=(MediaInput(media[1].id, "副本"),))
    third = env.evidence.create_evidence(env.ws, env.event.id, "FILE", "文件",
        source_type="FILE", media=(media[2].id,))
    assert len(env.evidence.detail(env.ws, first.id).media) == 2
    assert len(env.evidence.list_evidence(env.ws, env.event.id)) == 3
    assert env.evidence.detail(env.ws, second.id).media[0].media.id == media[1].id
    assert counts(env) == (3, 4)
    assert len(env.evidence.list_attachable_media(env.ws)) == 3
    assert [item.id for item in env.evidence.list_attachable_media(env.ws, search="image/png")] != []
    assert env.evidence.list_attachable_media(env.ws, search="no-such-mime") == []
    assert env.evidence.get(env.ws, third.id).kind == "FILE"


def test_duplicate_media_binding_is_rejected(env):
    media = make_images(env, 1)[0]
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media.id,))
    with pytest.raises(ConflictError):
        env.evidence.add_media(env.ws, entity.id, media.id)
    with pytest.raises(ConflictError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "重复", source_type="IMAGE",
                                     media=(media.id, media.id))
    # A different role is a distinct binding and stays allowed.
    env.evidence.add_media(env.ws, entity.id, media.id, "高清")
    assert len(env.evidence.detail(env.ws, entity.id).media) == 2


def test_metadata_update_and_original_content_protection(env):
    entity = text_evidence(env, annotation="原始注释")
    updated = env.evidence.update_metadata(env.ws, entity.id, title=" 新标题 ", annotation="补充说明")
    assert updated.title == "新标题" and updated.annotation == "补充说明"
    assert updated.content_text == entity.content_text and updated.kind == entity.kind
    assert updated.source_type == entity.source_type and updated.created_at == entity.created_at
    assert updated.updated_at > entity.updated_at
    for field in ("content_text", "kind", "source_type", "source_reference", "captured_at",
                  "event_id", "workspace_id", "id", "created_at", "deleted_at"):
        with pytest.raises((DomainError, TypeError)):
            env.evidence.update_metadata(env.ws, entity.id, **{field: "改写"})
    stored = env.evidence.get(env.ws, entity.id)
    assert stored.content_text == "原始聊天文字……" and stored.kind == "TEXT"
    assert not hasattr(env.evidence, "update_content")
    assert not hasattr(env.evidence, "update_source")


def test_metadata_update_keeps_annotation_separate_from_content(env):
    entity = text_evidence(env, content_text="原始聊天文字", annotation=None)
    env.evidence.update_metadata(env.ws, entity.id, annotation="这是活动前一天的聊天记录。")
    stored = env.evidence.get(env.ws, entity.id)
    assert stored.content_text == "原始聊天文字"
    assert stored.annotation == "这是活动前一天的聊天记录。"
    assert stored.annotation not in stored.content_text


def test_stale_attachment_snapshot_is_rejected(env):
    media = make_images(env, 2)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id,))
    stale = env.evidence.attachment_ids(env.evidence.detail(env.ws, entity.id))
    # Another window attaches a second media.
    env.evidence.add_media(env.ws, entity.id, media[1].id, "第二张")
    with pytest.raises(ConflictError):
        env.evidence.remove_media(env.ws, entity.id, media[0].id, expected_attachment_ids=stale)
    with pytest.raises(ConflictError):
        env.evidence.add_media(env.ws, entity.id, media[0].id, "重复", expected_attachment_ids=stale)
    assert len(env.evidence.detail(env.ws, entity.id).media) == 2
    fresh = env.evidence.attachment_ids(env.evidence.detail(env.ws, entity.id))
    env.evidence.remove_media(env.ws, entity.id, media[1].id, expected_attachment_ids=fresh)
    assert len(env.evidence.detail(env.ws, entity.id).media) == 1


def test_removing_last_attachment_of_media_kind_is_rejected(env):
    media = make_images(env, 2)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id, media[1].id))
    env.evidence.remove_media(env.ws, entity.id, media[0].id)
    with pytest.raises(ConflictError):
        env.evidence.remove_media(env.ws, entity.id, media[1].id)
    assert len(env.evidence.detail(env.ws, entity.id).media) == 1
    # TEXT evidence may lose all attachments because its rule does not require one.
    text = env.evidence.create_evidence(env.ws, env.event.id, "TEXT", "文字", content_text="原文",
                                        media=(media[0].id,))
    env.evidence.remove_media(env.ws, text.id, media[0].id)
    assert env.evidence.detail(env.ws, text.id).media == ()


def test_remove_media_rejects_unknown_binding(env):
    media = make_images(env, 1)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id,))
    with pytest.raises(NotFoundError):
        env.evidence.remove_media(env.ws, entity.id, uuid4())
    assert counts(env) == (1, 1)


def test_concurrent_attachment_keeps_one_winner(env):
    media = make_images(env, 2)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id,))
    snapshot = env.evidence.attachment_ids(env.evidence.detail(env.ws, entity.id))
    barrier = Barrier(2)
    def attach(media_id):
        barrier.wait(timeout=10)
        try:
            env.evidence.add_media(env.ws, entity.id, media_id, expected_attachment_ids=snapshot)
            return "ok"
        except ConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Both writers target the same new media with the same snapshot: exactly one wins.
        results = sorted(pool.map(attach, [media[1].id, media[1].id]))
    assert results == ["conflict", "ok"]
    assert env.evidence.attachment_ids(env.evidence.detail(env.ws, entity.id)) == {media[0].id, media[1].id}
    assert counts(env) == (1, 2)


def test_removed_media_can_be_reattached(env):
    media = make_images(env, 2)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id,))
    with pytest.raises(ConflictError):
        env.evidence.remove_media(env.ws, entity.id, media[0].id)
    env.evidence.add_media(env.ws, entity.id, media[1].id)
    env.evidence.remove_media(env.ws, entity.id, media[0].id)
    assert [view.media.id for view in env.evidence.detail(env.ws, entity.id).media] == [media[1].id]
    env.evidence.add_media(env.ws, entity.id, media[0].id)
    assert len(env.evidence.detail(env.ws, entity.id).media) == 2


def test_attachment_requires_active_event(env):
    media = make_images(env, 2)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "照片", source_type="IMAGE",
                                          media=(media[0].id,))
    env.services.events.soft_delete_event(env.ws, env.event.id)
    # Link-only attachment edits stay available while the Event is deleted.
    env.evidence.add_media(env.ws, entity.id, media[1].id, "补充")
    env.evidence.remove_media(env.ws, entity.id, media[1].id)
    assert counts(env) == (1, 1)
    assert env.services.events.get(env.ws, env.event.id, include_deleted=True).deleted_at is not None


def test_captured_at_and_created_at_are_utc_round_trip(env):
    zone = timezone(timedelta(hours=-7))
    captured = NOW.astimezone(zone)
    entity = text_evidence(env, captured_at=captured)
    assert entity.captured_at == NOW and entity.captured_at.tzinfo is timezone.utc
    assert entity.created_at.tzinfo is timezone.utc
    with pytest.raises(DomainError):
        text_evidence(env, captured_at=captured.replace(tzinfo=None))
    env.runtime.close()
    runtime = initialize_database(env.config)
    try:
        reloaded = EvidenceService(runtime.sessions).detail(env.ws, entity.id)
        assert reloaded.evidence == entity
        assert reloaded.evidence.captured_at == NOW
        assert reloaded.evidence.created_at.tzinfo is timezone.utc
        assert reloaded.evidence.updated_at.tzinfo is timezone.utc
    finally:
        runtime.close()


def test_captured_at_may_be_absent_but_created_at_always_exists(env):
    entity = env.evidence.create_evidence(env.ws, env.event.id, "MANUAL", "手工记录")
    assert entity.captured_at is None
    assert entity.created_at is not None and entity.updated_at is not None


@contextmanager
def fail_commit(env):
    def before_commit(session):
        if session.get_bind() is env.runtime.engine and session.info.get("ris_write"):
            raise RuntimeError("synthetic commit failure")
    event.listen(ThreadBoundSession, "before_commit", before_commit)
    try:
        yield
    finally:
        event.remove(ThreadBoundSession, "before_commit", before_commit)


def test_evidence_and_bindings_commit_atomically(env):
    media = make_images(env, 2)
    with fail_commit(env), pytest.raises(RuntimeError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "原子照片", source_type="IMAGE",
                                     media=(media[0].id, media[1].id))
    assert counts(env) == (0, 0)
    entity = env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "原子照片", source_type="IMAGE",
                                          media=(media[0].id, media[1].id))
    assert counts(env) == (1, 2)
    with fail_commit(env), pytest.raises(RuntimeError):
        env.evidence.add_media(env.ws, entity.id, media[0].id, "第三个角色")
    assert counts(env) == (1, 2)
    with fail_commit(env), pytest.raises(RuntimeError):
        env.evidence.update_metadata(env.ws, entity.id, title="不应写入")
    assert env.evidence.get(env.ws, entity.id).title == "原子照片"


def test_binding_failure_rolls_back_new_evidence(env, monkeypatch):
    from app.storage.evidence_repositories import SQLEvidenceMediaRepository
    media = make_images(env, 2)
    original = SQLEvidenceMediaRepository.add
    calls = {"count": 0}
    def failing(self, entity):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("synthetic second binding failure")
        return original(self, entity)
    monkeypatch.setattr(SQLEvidenceMediaRepository, "add", failing)
    with pytest.raises(RuntimeError):
        env.evidence.create_evidence(env.ws, env.event.id, "IMAGE", "半写入", source_type="IMAGE",
                                     media=(media[0].id, media[1].id))
    assert counts(env) == (0, 0)


def test_service_never_converts_evidence_into_events_or_relationships(env):
    entity = text_evidence(env)
    assert env.services.relationships.list_by_workspace(env.ws) == []
    assert counts(env) == (1, 0)
    assert not hasattr(env.evidence, "create_event")
    assert not hasattr(env.evidence, "create_relationship")
    assert not hasattr(env.evidence, "analyze")
    env.evidence.update_metadata(env.ws, entity.id, annotation="只是注释")
    assert counts(env) == (1, 0)
    assert len(env.services.events.list_events(env.ws)) == 2  # fixture events only


def test_private_text_not_logged(env, caplog):
    secret = "private-evidence-payload"
    with caplog.at_level("ERROR", logger="app.storage.database"):
        with pytest.raises(DomainError):
            env.evidence.create_evidence(env.ws, env.event.id, "TEXT", secret, content_text=None)
    assert secret not in caplog.text
    assert counts(env) == (0, 0)
