"""Workspace export (Phase 10): portable JSON + media, never a backup."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.config.settings import AppConfig
from app.domain.errors import NotFoundError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.export import EXPORT_FORMAT, JSON_NAME, MEDIA_DIR, README_NAME, WorkspaceExportService
from app.services.llm import FakeTransport, LLMConfigStore, LLMGateway, LLMProviderConfig
from app.services.unit_of_work import transaction
from app.storage.models import (EventModel, EvidenceModel, MemoryModel, PersonModel,
                                RelationshipModel)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_EXPORT_KEY", "sk-export-secret-value")
    config = AppConfig(data_dir=tmp_path / "export-data")
    runtime = initialize_database(config)
    store = LLMConfigStore.for_data_dir(config.storage_paths.data_dir)
    gateway = LLMGateway(store, transport=FakeTransport([]), sleeper=lambda _s: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake", base_url="http://127.0.0.1:9/v1",
                                            model="fake-model", credential_ref="env:RIS_EXPORT_KEY"))
    try:
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        workspace = services.initial_workspace
        other = services.workspaces.create("另一个 Workspace")
        person = services.people.create(workspace.id, "导出人物", occupation="摄影师")
        services.people.add_alias(workspace.id, person.id, "小导", alias_type="NICKNAME")
        partner = services.people.create(workspace.id, "导出伙伴")
        relationship = services.relationships.create(workspace.id, person.id, partner.id, "Friend")
        services.relationships.add_history(workspace.id, relationship.id, "PAUSED",
                                           effective_at=NOW - timedelta(days=3))
        event = services.events.create_event(workspace.id, "导出事件", "Meeting", "FACT",
                                             occurred_on=NOW.date(), participants=(person.id,))
        evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "导出证据",
                                                     content_text="导出的证据原文",
                                                     source_type="CHAT")
        image_path = tmp_path / "export.png"
        Image.new("RGB", (4, 4), (1, 2, 3)).save(image_path, "PNG")
        media = services.media.import_file(workspace.id, image_path)
        services.evidence.add_media(workspace.id, evidence.id, media.id)
        memory = services.memories.create(workspace.id, "用户确认的导出记忆", kind="PREFERENCE",
                                          person_id=person.id)
        # Another workspace keeps its own data, which must never leak into the export.
        outsider = services.people.create(other.id, "外部人物")
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              other=other.id, person=person, partner=partner, event=event,
                              evidence=evidence, media=media, memory=memory, outsider=outsider,
                              tmp_path=tmp_path)
    finally:
        runtime.close()


def export(env, name="export-out", **overrides):
    service = WorkspaceExportService(env.config, env.runtime.sessions)
    return service.export(env.ws, env.tmp_path / name, **overrides)


def document(result):
    return json.loads(Path(result.json_path).read_text(encoding="utf-8"))


def test_workspace_export_writes_json_media_and_readme(env):
    result = export(env)
    assert result.ok is True and result.error is None
    assert Path(result.json_path).name == JSON_NAME
    assert (Path(result.directory) / README_NAME).is_file()
    assert Path(result.media_dir).name == MEDIA_DIR
    assert result.counts["person"] == 2 and result.counts["alias"] == 1
    # Creating a relationship records its initial state, so there are two rows.
    assert result.counts["relationship"] == 1 and result.counts["history"] == 2
    assert result.counts["event"] == 1 and result.counts["participant"] == 1
    assert result.counts["evidence"] == 1 and result.counts["memory"] == 1
    assert result.media_file_count == 1 and result.media_total_bytes > 0
    copied = list(Path(result.media_dir).iterdir())
    assert len(copied) == 1 and copied[0].stat().st_size == env.media.size_bytes
    assert "导出" in result.summary()


def test_exported_json_is_parseable_and_self_describing(env):
    result = export(env)
    data = document(result)
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data
    assert data["format"] == EXPORT_FORMAT and data["is_backup"] is False
    assert data["format_version"] == 1
    assert data["workspace"]["id"] == str(env.ws)
    assert {item["display_name"] for item in data["person"]} == {"导出人物", "导出伙伴"}
    assert data["person_alias"][0]["alias"] == "小导"
    assert data["relationship"][0]["relationship_type"] == "Friend"
    assert data["relationship_history"][0]["state"] == "PAUSED"
    assert data["event"][0]["title"] == "导出事件"
    assert data["event_participant"][0]["person_id"] == str(env.person.id)
    assert data["evidence"][0]["title"] == "导出证据"
    assert data["evidence_media"][0]["evidence_id"] == str(env.evidence.id)
    assert data["memory"][0]["content"] == "用户确认的导出记忆"
    assert data["media"][0]["id"] == str(env.media.id)
    # Media entries describe what was actually copied.
    assert data["media_files"][0]["copied"] is True
    assert data["media_files"][0]["sha256"] == env.media.sha256


def test_export_never_includes_another_workspace(env):
    result = export(env)
    data = document(result)
    text = json.dumps(data, ensure_ascii=False)
    assert "外部人物" not in text
    assert str(env.other) not in text
    assert str(env.outsider.id) not in text
    assert all(item["workspace_id"] == str(env.ws) for item in data["person"])
    assert all(item["workspace_id"] == str(env.ws) for item in data["memory"])


def test_export_excludes_every_secret(env):
    result = export(env)
    files = [path for path in Path(result.directory).rglob("*") if path.is_file()]
    assert files
    for path in files:
        payload = path.read_bytes()
        assert b"sk-export-secret-value" not in payload
        assert b"RIS_EXPORT_KEY" not in payload          # not even the reference
        assert b"Authorization" not in payload
    data = document(result)
    assert data["privacy"]["credentials_included"] is False
    assert "credential" not in json.dumps(data).lower().replace("credentials_included", "")
    assert "llm" not in {key.lower() for key in data}


def test_analysis_metadata_is_exported_but_results_need_opt_in(env):
    from app.services.context import ALL_TIME, AnalysisRequest
    from app.services.llm import HttpResponse
    answer = "## Facts（事实）\n私有回答正文。"
    env.services.llm._transport.set_default(HttpResponse(200, json.dumps({
        "model": "fake-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": answer}}]}).encode(), {}, ""))
    request = AnalysisRequest(workspace_id=env.ws, target_type="PERSON", target_id=env.person.id,
                              analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=ALL_TIME)
    run = env.services.analysis.create_run(request)
    assert run.status == "SUCCEEDED"

    default = export(env, "export-default")
    data = document(default)
    assert len(data["analysis"]) == 1
    record = data["analysis"][0]
    assert record["provider_name"] == "fake" and record["model_name"] == "fake-model"
    assert record["status"] == "SUCCEEDED" and record["analysis_type"] == "PERSON_PROFILE"
    assert record["sources"]["person"] == [str(env.person.id)]
    assert "result" not in record and "context_snapshot" not in record
    assert record["result_included"] is False and record["context_snapshot_included"] is False
    assert "私有回答正文" not in json.dumps(data, ensure_ascii=False)

    explicit = export(env, "export-explicit", include_analysis_results=True,
                      include_analysis_snapshots=True)
    explicit_data = document(explicit)
    explicit_record = explicit_data["analysis"][0]
    assert explicit_record["result"] == answer
    assert "context_snapshot" in explicit_record
    assert explicit_data["privacy"]["analysis_results_included"] is True
    assert explicit_data["privacy"]["analysis_snapshots_included"] is True


def test_export_does_not_modify_the_source(env):
    def snapshot():
        with transaction(env.runtime.sessions) as session:
            rows = {model.__tablename__: [tuple(row) for row in
                                          session.execute(select(model.__table__)).all()]
                    for model in (PersonModel, RelationshipModel, EventModel, EvidenceModel,
                                  MemoryModel)}
        media_root = env.config.storage_paths.data_dir / "media"
        files = sorted((path.relative_to(media_root).as_posix(), path.stat().st_size)
                       for path in media_root.rglob("*") if path.is_file())
        return rows, files

    before = snapshot()
    assert export(env).ok is True
    assert snapshot() == before
    assert env.services.memories.get(env.ws, env.memory.id).content == "用户确认的导出记忆"


def test_export_refuses_a_non_empty_destination_and_unknown_workspace(env):
    target = env.tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    service = WorkspaceExportService(env.config, env.runtime.sessions)
    refused = service.export(env.ws, target)
    assert refused.ok is False and "非空" in refused.error
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    # An unknown workspace is reported through the result, not by raising into the UI.
    missing = service.export(uuid4(), env.tmp_path / "nowhere")
    assert missing.ok is False and missing.error
    assert "Workspace" in missing.error
    assert not (env.tmp_path / "nowhere").exists()


def test_export_records_media_it_cannot_copy(env):
    media_root = env.config.storage_paths.data_dir
    stored = next(path for path in (media_root / "media").rglob("*") if path.is_file())
    stored.unlink()                                  # the file disappeared after import
    result = export(env, "export-missing-file")
    assert result.ok is True                         # the facts are still exported
    assert result.media_file_count == 0
    entry = document(result)["media_files"][0]
    assert entry["copied"] is False and entry["reason"]
    assert document(result)["media"][0]["id"] == str(env.media.id)
    # A path the media store rejects (traversal, absolute, backslash) cannot even be
    # stored — app.domain.validation refuses it — so the per-file guard in
    # `_write_media` only has to cover "exists but unreadable", which is exercised by
    # the missing-file case above through the same code path.
    with pytest.raises(ValueError):
        from app.storage.repositories import SQLMediaRepository
        import dataclasses
        with transaction(env.runtime.sessions, write=True) as session:
            repository = SQLMediaRepository(session)
            model = repository.get(env.ws, env.media.id, include_deleted=True)
            repository.save(dataclasses.replace(model, storage_path="media/outside/../../etc/passwd"))


def test_export_of_an_empty_workspace_is_valid(env):
    empty = env.services.workspaces.create("空 Workspace")
    service = WorkspaceExportService(env.config, env.runtime.sessions)
    result = service.export(empty.id, env.tmp_path / "export-empty")
    assert result.ok is True
    data = document(result)
    assert data["person"] == [] and data["memory"] == [] and data["analysis"] == []
    assert data["workspace"]["name"] == "空 Workspace"
    assert result.counts["person"] == 0
