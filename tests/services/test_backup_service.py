"""Phase 10 backup and restore-verification contracts.

Real migrations, real temporary directories and real media files only. The
suite proves the two properties that make a backup worth having:

* the snapshot is *consistent* - it is taken from the running WAL database
  through SQLite's online backup API, so a transaction that only exists in the
  un-checkpointed WAL is still in the snapshot;
* the snapshot is *verified* - every file, hash, key and schema revision is
  re-checked on a temporary copy, and none of that touches the production
  database or media files.
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.backup import (BackupManifest, BackupService, BackupVerificationService,
                                 MediaManifestEntry)
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.services.unit_of_work import transaction
from app.storage.database import Base
from app.storage.models import (AnalysisRunModel, EventModel, EvidenceModel, MediaModel,
                                MemoryModel, PersonModel, RelationshipModel, WorkspaceModel)

NOW = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
SECRET = "sk-secret-backup-value"
ANSWER = "## Facts（事实）\n根据记录，2026-04-01 有一场摄影活动。\n"


def chat_response(content=ANSWER):
    payload = {"id": "chatcmpl-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content}}],
               "usage": {"total_tokens": 12}}
    return HttpResponse(200, json.dumps(payload).encode(), {}, "http://x/v1/chat/completions")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_BACKUP_KEY", SECRET)
    config = AppConfig(data_dir=tmp_path / "backup-data")
    runtime = initialize_database(config)
    transport = FakeTransport(default_response=chat_response())
    store = LLMConfigStore.for_data_dir(config.storage_paths.data_dir)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake", base_url="http://127.0.0.1:9/v1",
                                            model="fake-model",
                                            credential_ref="env:RIS_BACKUP_KEY"))
    services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
    workspace = services.initial_workspace
    person = services.people.create(workspace.id, "备份甲")
    other = services.people.create(workspace.id, "备份乙")
    relationship = services.relationships.create(workspace.id, person.id, other.id, "Friend")
    event = services.events.create_event(workspace.id, "摄影活动", "Meeting", "FACT",
                                         occurred_on=date(2026, 4, 1),
                                         participants=(person.id, other.id))
    evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "聊天记录",
                                                 content_text="私人聊天原文", source_type="CHAT")
    originals = tmp_path / "originals"
    originals.mkdir()
    avatar, gallery = originals / "avatar.png", originals / "gallery.png"
    Image.new("RGB", (17, 11), (9, 8, 7)).save(avatar, "PNG")
    Image.new("RGB", (13, 19), (7, 8, 9)).save(gallery, "PNG")
    media = services.media.import_file(workspace.id, avatar, person_id=person.id, role="AVATAR")
    second = services.media.import_file(workspace.id, gallery)
    services.people.add_gallery(workspace.id, person.id, second.id)
    memory = services.memories.create(workspace.id, "已确认的记忆内容", kind="FACT",
                                      person_id=person.id)
    run = services.analysis.create_run(AnalysisRequest(
        workspace_id=workspace.id, target_type="PERSON", target_id=person.id,
        analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=ALL_TIME))
    assert run.status == "SUCCEEDED"
    try:
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace,
                              person=person, other=other, relationship=relationship, event=event,
                              evidence=evidence, media=media, second_media=second, memory=memory,
                              run=run, gateway=gateway, transport=transport, tmp_path=tmp_path,
                              now=NOW)
    finally:
        runtime.close()


# ------------------------------------------------------------------ utilities

def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def backup(env, name="snapshot"):
    destination = env.tmp_path / "backups" / name
    return BackupService(env.config, clock=lambda: env.now).backup(destination)


def verify(env, result):
    return BackupService(env.config, clock=lambda: env.now).verify(result.directory)


def media_path(root, relative_path):
    return Path(root).joinpath(*relative_path.split("/"))


def partial_leftovers(destination):
    """Sibling temporary trees of one destination, e.g. ``.name.partial-ab12``."""
    if not destination.parent.is_dir():
        return []
    return sorted(path.name for path in destination.parent.iterdir()
                  if ".partial-" in path.name)


def media_inventory(config):
    """Every real source media file: relative POSIX path -> sha256."""
    media_dir = config.storage_paths.media
    return {path.relative_to(media_dir).as_posix(): sha256_of(path)
            for path in sorted(media_dir.rglob("*"))
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(media_dir).parts)}


def files_under(root):
    return sorted(path.relative_to(root).as_posix() for path in Path(root).rglob("*")
                  if path.is_file())


def table_rows(env):
    """Row-level snapshot of every table of the source database, order independent.

    Enumerated from ``sqlite_master`` rather than from the ORM metadata, so the
    comparison also covers ``alembic_version`` and the Phase 7 FTS5 projection.
    """
    with transaction(env.runtime.sessions) as session:
        connection = session.connection()
        names = [row[0] for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert set(Base.metadata.tables) <= set(names)
        return {name: sorted(repr(tuple(row)) for row in
                             connection.exec_driver_sql(f'SELECT * FROM "{name}"'))
                for name in names}


def snapshot_query(database, sql, parameters=()):
    connection = sqlite3.connect(str(database))
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


# ------------------------------------------------- 11/12: consistent WAL snapshot

def test_snapshot_contains_a_transaction_that_only_lives_in_the_wal(env):
    """A live writer holds the WAL open; the snapshot must still be complete."""
    database = env.config.storage_paths.database
    wal = database.with_name(database.name + "-wal")
    marker = "只存在于 WAL 的已确认记忆"
    holder = env.runtime.engine.raw_connection()
    try:
        cursor = holder.cursor()
        cursor.execute("BEGIN")
        cursor.execute("SELECT COUNT(*) FROM person").fetchone()     # hold a read snapshot
        committed = env.services.memories.create(env.ws.id, marker, kind="FACT")
        assert wal.is_file() and wal.stat().st_size > 0, "WAL 未被保留，测试前提不成立"
        # The committed row is only in the WAL: a plain file copy of the main
        # database alone could not contain it, so this is a real torn-snapshot trap.
        assert marker.encode("utf-8") not in database.read_bytes()

        result = backup(env, "wal-snapshot")
        assert result.ok is True and result.error is None
        assert partial_leftovers(result.directory) == []
    finally:
        holder.rollback()
        holder.close()

    snapshot = result.directory / "database.sqlite3"
    assert snapshot_query(snapshot, "PRAGMA integrity_check") == [("ok",)]
    contents = [row[0] for row in snapshot_query(snapshot, "SELECT content FROM memory")]
    assert sorted(contents) == sorted(["已确认的记忆内容", marker])
    assert snapshot_query(snapshot, "SELECT COUNT(*) FROM person") == [(2,)]
    assert committed.id is not None
    assert result.manifest.workspace_count == 1
    # Reading the live database left the committed transaction visible to the app.
    assert marker in [item.content for item in env.services.memories.list_all(env.ws.id)]


def test_snapshot_is_a_self_contained_single_file(env):
    result = backup(env, "single-file")
    assert result.ok is True
    assert sorted(path.name for path in result.directory.iterdir()) == [
        "database.sqlite3", "manifest.json", "media"]
    assert not (result.directory / "database.sqlite3-wal").exists()
    assert not (result.directory / "database.sqlite3-shm").exists()
    assert snapshot_query(result.directory / "database.sqlite3",
                          "SELECT version_num FROM alembic_version") == [("0008_memory",)]


# ------------------------------------------------------ 13: media files copied

def test_media_files_are_copied_byte_for_byte(env):
    before = media_inventory(env.config)
    result = backup(env, "media-copy")
    assert result.ok is True
    assert len(before) == 2 and result.manifest.media_file_count == len(before)
    for entry in result.manifest.media:
        source = media_path(env.config.storage_paths.media, entry.relative_path)
        target = media_path(result.media_dir, entry.relative_path)
        assert target.is_file()
        assert target.read_bytes() == source.read_bytes()
        assert sha256_of(target) == entry.sha256 == source.name == before[entry.relative_path]
        assert not Path(entry.relative_path).is_absolute()


# ------------------------------------------------------------ 14: manifest JSON

def test_manifest_is_complete_json_and_counts_match_the_files(env):
    result = backup(env, "manifest")
    raw = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert set(raw) == {"application_version", "schema_revision", "created_at", "workspace_count",
                        "database_sha256", "media_file_count", "media_total_bytes", "media"}
    assert raw["application_version"] == "Phase 10"
    assert raw["schema_revision"] == "0008_memory"
    assert raw["created_at"] == env.now.isoformat()
    assert raw["workspace_count"] == 1
    assert raw["database_sha256"] == sha256_of(result.database_path)
    assert len(raw["media"]) == raw["media_file_count"] == len(files_under(result.media_dir))
    assert raw["media_total_bytes"] == sum(item["size"] for item in raw["media"])
    assert raw["media_total_bytes"] == sum(
        (result.media_dir / item["relative_path"]).stat().st_size for item in raw["media"])
    assert BackupManifest.from_mapping(raw) == result.manifest


def test_manifest_mapping_round_trip_and_rejection():
    entry = MediaManifestEntry(relative_path="ws/ab/" + "a" * 64, size=12, sha256="a" * 64)
    manifest = BackupManifest(application_version="Phase 10", schema_revision="0008_memory",
                              created_at=NOW, workspace_count=2, database_sha256="b" * 64,
                              media_file_count=1, media_total_bytes=12, media=(entry,))
    mapping = manifest.to_mapping()
    assert json.loads(json.dumps(mapping)) == mapping
    assert BackupManifest.from_mapping(mapping) == manifest
    for broken in ({**mapping, "created_at": "2026-05-06T07:08:09"},          # naive time
                   {key: value for key, value in mapping.items() if key != "media"},
                   {**mapping, "database_sha256": "short"},
                   {**mapping, "workspace_count": -1},
                   {**mapping, "media": [{**mapping["media"][0], "relative_path": "/etc/passwd"}]},
                   {**mapping, "media": [{**mapping["media"][0], "relative_path": "../escape"}]},
                   {**mapping, "media": [{**mapping["media"][0], "size": "12"}]},
                   {**mapping, "media_file_count": 7},
                   ["not", "an", "object"]):
        with pytest.raises(ValueError):
            BackupManifest.from_mapping(broken)


# ------------------------------------------------------- 15: hashes are correct

def test_every_hash_matches_recomputed_file_and_database_row(env):
    result = backup(env, "hashes")
    entries = {entry.relative_path: entry for entry in result.manifest.media}
    with transaction(env.runtime.sessions) as session:
        recorded = {row.storage_path[len("media/"):]: (row.sha256, row.size_bytes) for row in
                    session.execute(select(MediaModel.storage_path, MediaModel.sha256,
                                           MediaModel.size_bytes))}
    assert set(entries) == set(recorded) == set(media_inventory(env.config))
    for relative, (sha256, size) in recorded.items():
        copied = media_path(result.media_dir, relative)
        assert entries[relative].sha256 == sha256
        assert entries[relative].size == size == copied.stat().st_size
        assert sha256_of(copied) == sha256
        assert result.manifest.database_sha256 == sha256_of(result.database_path)


# ------------------------------------------- 16: a failed backup changes nothing

def test_refuses_a_nonempty_destination_and_keeps_everything_untouched(env):
    destination = env.tmp_path / "backups" / "taken"
    destination.mkdir(parents=True)
    keep = destination / "existing-backup.txt"
    keep.write_text("先前备份", encoding="utf-8")
    rows, media = table_rows(env), media_inventory(env.config)

    result = BackupService(env.config, clock=lambda: env.now).backup(destination)

    assert result.ok is False
    assert result.error and "非空" in result.error
    assert result.manifest is None and result.database_path is None
    assert result.media_dir is None and result.manifest_path is None
    assert result.duration_ms >= 0 and result.directory == destination
    assert [path.name for path in destination.iterdir()] == ["existing-backup.txt"]
    assert keep.read_text(encoding="utf-8") == "先前备份"
    assert partial_leftovers(destination) == []
    assert table_rows(env) == rows
    assert media_inventory(env.config) == media


def test_an_empty_existing_destination_is_accepted(env):
    destination = env.tmp_path / "backups" / "empty"
    destination.mkdir(parents=True)
    result = BackupService(env.config, clock=lambda: env.now).backup(destination)
    assert result.ok is True
    assert sorted(path.name for path in destination.iterdir()) == [
        "database.sqlite3", "manifest.json", "media"]


def test_missing_source_media_fails_readably_and_cleans_up(env):
    victim = media_path(env.config.storage_paths.media,
                        env.media.storage_path[len("media/"):])
    assert victim.is_file()
    victim.unlink()
    destination = env.tmp_path / "backups" / "missing-media"
    rows, media = table_rows(env), media_inventory(env.config)

    result = BackupService(env.config, clock=lambda: env.now).backup(destination)

    assert result.ok is False
    assert result.error is not None
    assert "1 个媒体文件在源媒体目录中缺失" in result.error
    assert str(env.config.storage_paths.data_dir) not in result.error
    assert str(victim) not in result.error
    assert destination.exists() is False
    assert partial_leftovers(destination) == []
    assert table_rows(env) == rows
    assert media_inventory(env.config) == media


def test_destination_inside_the_media_directory_is_refused(env):
    destination = env.config.storage_paths.media / "in-place-backup"
    result = BackupService(env.config, clock=lambda: env.now).backup(destination)
    assert result.ok is False
    assert result.error and "不能位于" in result.error
    assert destination.exists() is False


# --------------------------------------------- 17: the source is never modified

def test_backup_does_not_modify_a_single_source_row_or_media_file(env):
    rows, media = table_rows(env), media_inventory(env.config)
    assert len(rows) > len(Base.metadata.tables) and len(media) == 2

    result = backup(env, "read-only")

    assert result.ok is True
    assert table_rows(env) == rows
    assert media_inventory(env.config) == media


# ------------------------------- 18/19/20/21/22: verification on a temp copy

def test_verification_works_on_a_temporary_copy_and_cleans_it_up(env, monkeypatch):
    result = backup(env, "verify-temp")
    database = env.config.storage_paths.database
    before_file = (database.stat().st_mtime_ns, sha256_of(database))
    before_media = media_inventory(env.config)
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr("app.services.backup.tempfile.mkdtemp", recording_mkdtemp)
    report = verify(env, result)

    assert report.ok is True and report.error is None
    assert created and all(not path.exists() for path in created)
    assert report.check("copy").ok is True
    assert "临时目录" in report.check("copy").message
    assert (database.stat().st_mtime_ns, sha256_of(database)) == before_file
    assert media_inventory(env.config) == before_media
    assert files_under(result.directory) == sorted(
        ["database.sqlite3", "manifest.json"] +
        ["media/" + entry.relative_path for entry in result.manifest.media])


def test_verification_checks_agree_with_the_manifest(env):
    result = backup(env, "verify-schema")
    report = verify(env, result)
    assert report.ok is True
    # The dedicated service is the single implementation; BackupService delegates.
    direct = BackupVerificationService(env.config, clock=lambda: env.now).verify(result.directory)
    assert direct.ok is True and direct.checks == report.checks
    assert report.schema_revision == result.manifest.schema_revision == "0008_memory"
    schema = report.check("schema_revision")
    assert schema.ok is True and "0008_memory" in schema.message
    assert "应用 head" in schema.message
    manifest_check = report.check("manifest")
    assert manifest_check.ok is True and "0008_memory" in manifest_check.message
    assert {check.name for check in report.checks} >= {
        "manifest", "database_sha256", "copy", "schema_revision", "integrity_check",
        "foreign_key_check", "media_files", "media_extra_files", "application_open",
        "integrity_audit"}
    assert all(check.message for check in report.checks)


def test_verification_proves_clean_foreign_keys_media_hashes_and_app_open(env):
    result = backup(env, "verify-health")
    report = verify(env, result)
    assert report.ok is True
    assert report.check("integrity_check").ok is True
    assert report.check("foreign_key_check").ok is True
    assert "无违规" in report.check("foreign_key_check").message
    media_check = report.check("media_files")
    assert media_check.ok is True
    assert str(result.manifest.media_file_count) in media_check.message
    assert report.check("media_extra_files").ok is True
    assert report.check("application_open").ok is True
    # The audit check either really ran (and passed) or says why it was skipped.
    audit = report.check("integrity_audit")
    assert audit.ok is True and audit.message
    if "已跳过" not in audit.message:
        assert report.report is not None and getattr(report.report, "ok", False) is True


def test_the_restored_copy_can_be_opened_and_used_by_the_application(env):
    result = backup(env, "restore")
    restored = env.tmp_path / "restored"
    shutil.copytree(result.directory, restored)

    runtime = initialize_database(AppConfig(data_dir=restored))
    try:
        services = create_application_services(AppConfig(data_dir=restored), runtime.sessions)
        workspace = services.initial_workspace
        assert {person.display_name for person in services.people.list(workspace.id)} == {
            "备份甲", "备份乙"}
        assert [item.content for item in services.memories.list_all(workspace.id)] == [
            "已确认的记忆内容"]
    finally:
        runtime.close()


# --------------------------------------------------- 23: snapshot data is equal

def test_snapshot_row_counts_equal_the_source_database(env):
    result = backup(env, "counts")
    models = {"workspace": WorkspaceModel, "person": PersonModel, "media": MediaModel,
              "relationship": RelationshipModel, "event": EventModel, "evidence": EvidenceModel,
              "analysis_run": AnalysisRunModel, "memory": MemoryModel}
    snapshot = {name: snapshot_query(result.database_path,
                                     f"SELECT COUNT(*) FROM {name}")[0][0] for name in models}
    with transaction(env.runtime.sessions) as session:
        source = {name: session.scalar(select(func.count()).select_from(model))
                  for name, model in models.items()}
    assert snapshot == source
    assert all(count > 0 for count in source.values())
    assert snapshot_query(result.database_path, "PRAGMA foreign_key_check") == []


# ------------------------------------------------------------ no secrets at all

def test_no_backup_file_contains_the_provider_secret(env):
    data_dir = env.config.storage_paths.data_dir
    provider_file = data_dir / "llm-providers.toml"
    assert provider_file.is_file() and "env:RIS_BACKUP_KEY" in provider_file.read_text("utf-8")

    result = backup(env, "secrets")
    assert result.ok is True
    copied = files_under(result.directory)
    assert copied                                        # the check is not vacuous
    for relative in copied:
        payload = (result.directory / relative).read_bytes()
        assert SECRET.encode("utf-8") not in payload, relative
        assert b"Authorization" not in payload, relative
    assert SECRET.encode("utf-8") not in result.manifest_path.read_bytes()
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRunModel)) == 1


# --------------------------------------------------------------- summary text

def test_summary_is_readable_for_a_passing_and_a_failing_verification(env):
    result = backup(env, "summary")
    good = verify(env, result)
    text = good.summary()
    assert "备份验证通过" in text and "未通过" not in text
    assert f"{len(good.checks)}/{len(good.checks)} 通过" in text
    assert "0008_memory" in text and str(result.directory) in text
    assert all(check.name in text for check in good.checks)

    victim = media_path(result.media_dir, result.manifest.media[0].relative_path)
    victim.write_bytes(victim.read_bytes() + b"tampered")
    bad = verify(env, result)
    bad_text = bad.summary()
    assert bad.ok is False and "备份验证未通过" in bad_text
    assert "media_files" in bad_text and "错误：" in bad_text
    assert len(bad_text.splitlines()) == len(bad.checks) + 3


# ---------------------------------------------------------- tampering detection

def test_a_tampered_media_file_fails_verification(env):
    result = backup(env, "tampered-media")
    entry = result.manifest.media[0]
    victim = media_path(result.media_dir, entry.relative_path)
    victim.write_bytes(victim.read_bytes() + b"tampered")

    report = verify(env, result)

    assert report.ok is False
    media_check = report.check("media_files")
    assert media_check is not None and media_check.ok is False
    assert "1" in media_check.message
    assert report.error == media_check.message
    assert report.check("manifest").ok is True
    assert report.check("database_sha256").ok is True
    failed = {check.name for check in report.failed}
    assert "media_files" in failed and failed <= {"media_files", "integrity_audit"}
    audit = report.check("integrity_audit")
    if "已跳过" not in audit.message:
        # Two independent mechanisms notice the tampered bytes.
        assert audit.ok is False
    # Verification reports; it never repairs or deletes the backup it inspected.
    assert victim.is_file() and result.manifest_path.is_file()


def test_a_tampered_manifest_is_rejected(env):
    media_sha = backup(env, "tamper-media-sha")
    payload = json.loads(media_sha.manifest_path.read_text(encoding="utf-8"))
    payload["media"][0]["sha256"] = "0" * 64
    media_sha.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(env, media_sha)
    assert report.ok is False and report.check("media_files").ok is False
    assert report.check("manifest").ok is True

    database_sha = backup(env, "tamper-database-sha")
    payload = json.loads(database_sha.manifest_path.read_text(encoding="utf-8"))
    payload["database_sha256"] = "1" * 64
    database_sha.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(env, database_sha)
    assert report.ok is False and report.check("database_sha256").ok is False

    counts = backup(env, "tamper-count")
    payload = json.loads(counts.manifest_path.read_text(encoding="utf-8"))
    payload["media_file_count"] = 7
    counts.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(env, counts)
    assert report.ok is False
    assert [check.name for check in report.checks] == ["manifest"]
    assert "media_file_count" in report.error

    broken = backup(env, "tamper-json")
    broken.manifest_path.write_text("{ not json", encoding="utf-8")
    report = verify(env, broken)
    assert report.ok is False and report.check("manifest").ok is False
    assert "无法解析" in report.error


def test_missing_manifest_or_database_fails_without_being_silently_accepted(env):
    missing_dir = env.tmp_path / "backups" / "absent"
    report = BackupService(env.config, clock=lambda: env.now).verify(missing_dir)
    assert report.ok is False
    assert [check.name for check in report.checks] == ["manifest"]
    assert report.error and "不存在" in report.error
    assert report.schema_revision is None

    result = backup(env, "damaged")
    (result.directory / "database.sqlite3").unlink()
    report = verify(env, result)
    assert report.ok is False
    assert report.check("database_sha256").ok is False
    assert report.check("copy").ok is False
    assert all(check.ok is False for check in report.checks[3:])
    assert sum(1 for check in report.checks if check.ok) == 1        # only the manifest
    assert files_under(result.directory) == sorted(
        ["manifest.json"] +
        ["media/" + entry.relative_path for entry in result.manifest.media])
