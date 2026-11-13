"""Phase 10 Settings → Data Management UI: real migrations, services and directories.

Like every other UI test in this repository the module runs against the **real**
SQLite database, the real migrations and the real services, plus real temporary
directories for backup and export; only the HTTP transport is fake, so no test can
reach the network. What is under test is what a user can observe:

* the Settings page is a real Data Management page with four manual actions;
* **opening or switching to it runs none of them** - the four service calls are
  counted from before ``MainWindow`` is even constructed;
* a check, a backup and a verification return through the worker thread that owns
  them, with every button disabled and the status line naming the operation while
  it runs;
* a damaged media file is reported as ``MEDIA_FILE_MISSING`` and a tampered backup
  fails its ``media_files`` check - an existing file is never mistaken for a good
  backup;
* an export contains the analysis result only when the user ticked that box, and a
  refused destination is shown as the service's own message, not as a crash;
* no API key, no ``credential_ref``, no Memory/Evidence body text ever reaches a
  widget or the log.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtWidgets import (QFileDialog, QLabel, QMessageBox, QPlainTextEdit, QTableWidget)
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import FakeTransport, LLMConfigStore, LLMGateway, LLMProviderConfig
from app.storage.models import MediaModel
from app.ui.main_window import MainWindow
from app.ui.settings_page import (CHECK_FAILED, CHECK_PASSED, ERROR_COLOR, ERROR_SEVERITY,
                                  INTEGRITY_BUSY, SettingsPage, human_size)
from tests.ui.test_analysis_ui import FOUR_SECTIONS, chat_response

SECRET = "sk-phase10-settings-secret-must-never-be-shown"
CREDENTIAL_REF = "env:RIS_SETTINGS_KEY"
PRIVATE_MEMORY = "private-memory-body-must-not-be-rendered"
EVIDENCE_BODY = "secret log"
BUTTON_LABELS = ("Run Integrity Check", "Create Backup", "Verify Backup", "Export Workspace")


# ---------------------------------------------------------------------- helpers

def pump(qt_app, predicate, timeout=30.0):
    """Run the Qt event loop until ``predicate()`` holds (or time out)."""
    deadline = timeout
    while deadline > 0:
        qt_app.processEvents()
        if predicate():
            return True
        qt_app.processEvents()
        qt_app.thread().msleep(10)
        deadline -= 0.01
    return bool(predicate())


def action(case, trigger, attribute, timeout=30.0):
    """Trigger one UI action and wait until its own result has been rendered."""
    before = getattr(case.page, attribute)
    trigger()
    landed = pump(case.qt_app, lambda: case.page.busy is False
                  and getattr(case.page, attribute) is not before, timeout)
    assert landed, f"{attribute} was never rendered"
    return getattr(case.page, attribute)


def run_analysis(case):
    """One real analysis run through the service with the fake transport."""
    case.transport.push(chat_response())
    request = AnalysisRequest(workspace_id=case.ws, target_type="PERSON", target_id=case.person.id,
                              analysis_type=case.services.analysis.analysis_type_for("PERSON"),
                              as_of=datetime.now(timezone.utc), time_scope=ALL_TIME)
    return case.services.analysis.create_run(request)


def chooser(monkeypatch, *paths):
    """Answer directory choices in order; the returned list can be extended."""
    answers = [Path(path) for path in paths]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *args, **kwargs:
                                     str(answers.pop(0)) if answers else ""))
    return answers


def find_row(table, column, value):
    for row in range(table.rowCount()):
        if table.item(row, column).text() == value:
            return row
    return -1


def page_text(page):
    """Every string the page shows in a text widget, including table cells."""
    parts = [widget.text() for widget in page.findChildren(QLabel)]
    parts += [widget.toPlainText() for widget in page.findChildren(QPlainTextEdit)]
    for table in page.findChildren(QTableWidget):
        parts += [table.item(row, column).text() for row in range(table.rowCount())
                  for column in range(table.columnCount()) if table.item(row, column) is not None]
    return "\n".join(parts)


def instrument(services):
    """Count every service call the page could make, without changing behaviour.

    The wrappers are installed **before** ``MainWindow`` exists, so a call made
    while the page is constructed or activated would be counted as well.
    """
    counts = {"integrity": 0, "backup": 0, "verify": 0, "export": 0, "workspaces": 0,
              "threads": []}
    # (counter key, attribute of ApplicationServices, method on that service)
    for key, owner, attribute in (("integrity", "integrity", "check"),
                                  ("backup", "backups", "backup"),
                                  ("verify", "backup_verification", "verify"),
                                  ("export", "exports", "export"),
                                  ("workspaces", "workspaces", "list")):
        service = getattr(services, owner, None)
        if service is None or not hasattr(service, attribute):
            continue
        original = getattr(service, attribute)

        def counted(*args, __original=original, __key=key, **kwargs):
            counts[__key] += 1
            counts["threads"].append(threading.current_thread().name)
            return __original(*args, **kwargs)

        setattr(service, attribute, counted)
    return counts


@pytest.fixture
def settings_ui(tmp_path, qt_app, monkeypatch):
    """Real database + real services + FakeTransport; UI errors are captured."""
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setenv("RIS_SETTINGS_KEY", SECRET)
    config = AppConfig(data_dir=tmp_path / "phase10-settings-data")
    runtime = initialize_database(config)
    window = None
    try:
        transport = FakeTransport([])
        gateway = LLMGateway(LLMConfigStore.for_data_dir(config.storage_paths.data_dir),
                             transport=transport)
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        ws = services.initial_workspace.id
        person = services.people.create(ws, "Tester")
        other = services.people.create(ws, "Other")
        services.relationships.create(ws, person.id, other.id, "Friend")
        event = services.events.create_event(
            ws, "Meeting", "Call", "FACT",
            started_at=datetime.now(timezone.utc) - timedelta(days=3), participants=(person.id,))
        evidence = services.evidence.create_evidence(ws, event.id, kind="TEXT", title="Chat log",
                                                     source_type="CHAT", content_text=EVIDENCE_BODY)
        services.memories.create(ws, PRIVATE_MEMORY, kind="FACT", person_id=person.id)
        gateway.save_provider(LLMProviderConfig(provider_name="fake",
                                                base_url="http://127.0.0.1:9/v1",
                                                model="fake-model", credential_ref=CREDENTIAL_REF))
        originals = tmp_path / "originals"
        originals.mkdir()
        avatar, gallery = originals / "avatar.png", originals / "gallery.png"
        Image.new("RGB", (17, 11), (9, 8, 7)).save(avatar, "PNG")
        Image.new("RGB", (13, 19), (7, 8, 9)).save(gallery, "PNG")
        media = services.media.import_file(ws, avatar, person_id=person.id, role="AVATAR")
        second = services.media.import_file(ws, gallery, person_id=person.id)
        counts = instrument(services)
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(5)                 # Settings, then activate()
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, gateway=gateway,
                              transport=transport, ws=ws, person=person, other=other, event=event,
                              evidence=evidence, media=media, second_media=second,
                              window=window, page=window.settings_page, counts=counts,
                              tmp_path=tmp_path, qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


# ------------------------------------------------------------- 1. the real page

def test_settings_page_is_real_and_shows_four_manual_buttons(settings_ui):
    c = settings_ui
    page = c.page
    assert isinstance(page, SettingsPage)
    assert page.objectName() == "settings"
    assert c.window.pages.widget(5) is page
    assert [button.text() for button in (page.integrity_button, page.backup_button,
                                        page.verify_button, page.export_button)] == list(BUTTON_LABELS)
    for button in (page.integrity_button, page.backup_button, page.verify_button,
                   page.export_button):
        assert button.isEnabled() is True
    assert page.service_note.isVisible() is False
    assert "Data Management" in page_text(page)
    assert page.workspace_combo.count() == len(c.services.workspaces.list()) == 1
    assert page.workspace_combo.currentData() == str(c.ws)
    assert str(c.ws) in page.export_workspace_label.text()
    # Every operation is opt-in, and both private-text switches start off.
    assert page.database_only.isChecked() is False
    assert page.current_workspace_only.isChecked() is False
    assert page.include_results.isChecked() is False
    assert page.include_snapshots.isChecked() is False
    assert c.errors == []


def test_opening_and_switching_to_settings_runs_nothing(settings_ui):
    c = settings_ui
    # The counters were installed before MainWindow was built, so this covers
    # construction *and* activation of the page.
    assert c.counts["workspaces"] >= 1                     # activation reads the list only
    assert (c.counts["integrity"], c.counts["backup"], c.counts["verify"],
            c.counts["export"]) == (0, 0, 0, 0)
    for index in (0, 3, 5, 4, 5):
        c.window.navigation.setCurrentRow(index)
        c.qt_app.processEvents()
    assert (c.counts["integrity"], c.counts["backup"], c.counts["verify"],
            c.counts["export"]) == (0, 0, 0, 0)
    assert c.page.last_report is None and c.page.last_backup is None
    assert c.page.last_verification is None and c.page.last_export is None
    assert c.page.integrity_table.rowCount() == 0 and c.page.verify_table.rowCount() == 0
    assert c.page.integrity_summary.toPlainText() == "" and c.page.verify_summary.toPlainText() == ""
    assert "尚未" in c.page.integrity_status.text()
    assert "尚未" in c.page.backup_status.text()
    assert "尚未" in c.page.verify_status.text()
    assert "尚未" in c.page.export_status.text()
    assert c.page.busy is False
    assert c.transport.requests == []
    assert c.errors == []


# ------------------------------------------------------------ 2. integrity check

def test_integrity_check_runs_on_click_and_reports_ok(settings_ui):
    c = settings_ui
    assert c.counts["integrity"] == 0
    report = action(c, c.page.integrity_button.click, "last_report")
    assert report is c.page.last_report
    assert report.ok is True and report.errors == ()
    # Defaults really are "whole database" and "full check including media hashes".
    assert report.counts["workspace_filtered"] is False
    assert report.counts["media_hashes_checked"] == 2
    summary = c.page.integrity_summary.toPlainText()
    assert summary.startswith(report.summary())
    assert "OK" in summary and "0 error" in summary and "0 warning" in summary
    assert str(report.checked_at.astimezone().isoformat(timespec="seconds")) in summary
    assert c.page.integrity_status.text().startswith("OK")
    assert c.page.integrity_table.rowCount() == len(report.warnings)
    assert c.page.issue_rows(severity=ERROR_SEVERITY) == []
    assert c.counts["integrity"] == 1
    assert c.transport.requests == []
    assert c.errors == []

    # The two scope switches are honoured, one at a time.
    c.page.database_only.setChecked(True)
    database_only = action(c, c.page.integrity_button.click, "last_report")
    assert database_only.counts["media_hashes_checked"] == 0
    assert database_only.counts["media_files_skipped"] == 2
    assert "跳过 2 个" in c.page.integrity_summary.toPlainText()
    c.page.database_only.setChecked(False)
    c.page.current_workspace_only.setChecked(True)
    scoped = action(c, c.page.integrity_button.click, "last_report")
    assert scoped.counts["workspace_filtered"] is True
    assert "仅当前 Workspace" in c.page.integrity_summary.toPlainText()
    assert c.counts["integrity"] == 3
    assert c.errors == []


def test_slow_operations_leave_the_gui_thread_and_disable_the_buttons(settings_ui):
    c = settings_ui
    timeline = []
    original_set_busy = c.page.set_busy

    def spy_set_busy(busy, message=""):
        result = original_set_busy(busy, message)
        timeline.append(("busy", busy, message, c.page.integrity_button.isEnabled(),
                         c.page.backup_button.isEnabled(), c.page.verify_button.isEnabled(),
                         c.page.export_button.isEnabled()))
        return result

    c.page.set_busy = spy_set_busy
    counted_check = c.services.integrity.check

    def recording_check(*args, **kwargs):
        timeline.append(("call", threading.current_thread().name))
        return counted_check(*args, **kwargs)

    c.services.integrity.check = recording_check
    action(c, c.page.integrity_button.click, "last_report")

    assert [entry[0] for entry in timeline] == ["busy", "call", "busy"]
    busy_on, call, busy_off = timeline
    assert busy_on[1] is True and busy_on[2].startswith(INTEGRITY_BUSY)
    assert (busy_on[3], busy_on[4], busy_on[5], busy_on[6]) == (False, False, False, False)
    assert call[1] != threading.main_thread().name          # the audit ran off the GUI thread
    assert busy_off[1] is False and busy_off[2] == ""
    assert c.page.busy is False and c.page.busy_label.text() == ""
    for button in (c.page.integrity_button, c.page.backup_button, c.page.verify_button,
                   c.page.export_button):
        assert button.isEnabled() is True
    assert c.errors == []


def test_a_stale_worker_signal_cannot_unlock_a_running_operation(settings_ui, monkeypatch):
    """The previous worker's queued signal must not re-enable a running operation."""
    c = settings_ui
    parent = c.tmp_path / "backup-parent"
    parent.mkdir()
    chooser(monkeypatch, parent)
    action(c, c.page.integrity_button.click, "last_report")
    finished_worker = c.page.worker
    assert finished_worker.isFinished() and c.page.busy is False

    counted = c.services.backups.backup

    def slow_backup(*args, **kwargs):
        time.sleep(0.3)
        return counted(*args, **kwargs)

    c.services.backups.backup = slow_backup
    c.page.backup_button.click()
    assert c.page.busy is True
    finished_worker.finished.emit()                     # exactly what a queued signal does
    assert c.page.busy is True                          # the running backup keeps its lock
    for button in (c.page.integrity_button, c.page.backup_button, c.page.verify_button,
                   c.page.export_button):
        assert button.isEnabled() is False
    assert c.page.backup_button.toolTip() == ""
    result = action(c, lambda: None, "last_backup")
    assert result.ok is True
    assert c.page.busy is False and c.page.busy_label.text() == ""
    for button in (c.page.integrity_button, c.page.backup_button, c.page.verify_button,
                   c.page.export_button):
        assert button.isEnabled() is True
    assert c.errors == []


def test_integrity_check_reports_a_deleted_media_file_as_an_error(settings_ui):
    c = settings_ui
    victim = c.services.media.store.path_for(c.media.storage_path)
    assert victim.is_file()
    victim.unlink()
    report = action(c, c.page.integrity_button.click, "last_report")

    assert report.ok is False
    assert len(report.errors) == 1
    assert "发现问题" in c.page.integrity_status.text()
    assert c.page.integrity_summary.toPlainText().startswith("发现问题")
    assert "1 项 error" in c.page.integrity_summary.toPlainText()
    rows = c.page.issue_rows(code="MEDIA_FILE_MISSING")
    assert len(rows) == 1
    assert rows[0][0] == ERROR_SEVERITY
    assert rows[0][3].startswith("media ·")
    row = find_row(c.page.integrity_table, 1, "MEDIA_FILE_MISSING")
    assert row >= 0
    assert c.page.integrity_table.item(row, 0).foreground().color().name() == ERROR_COLOR.name()
    assert c.page.issue_rows(severity=ERROR_SEVERITY) == rows
    assert c.counts["integrity"] == 1
    assert c.errors == []


def test_a_raising_service_is_reported_through_the_operation_boundary(settings_ui, caplog):
    c = settings_ui
    message = "磁盘不可用（测试注入）"

    def boom(*args, **kwargs):
        raise OSError(message)

    c.services.integrity.check = boom
    with caplog.at_level(logging.DEBUG, logger="app"):
        c.page.integrity_button.click()
        assert pump(c.qt_app, lambda: c.page.busy is False and c.errors)
    assert c.errors[-1] == message                    # the readable message reaches the user
    assert message not in caplog.text                 # ... and never the log
    assert "OSError" in caplog.text                   # only the class name does
    assert c.page.last_report is None
    assert c.page.busy is False and c.page.busy_label.text() == ""
    for button in (c.page.integrity_button, c.page.backup_button, c.page.verify_button,
                   c.page.export_button):
        assert button.isEnabled() is True


# --------------------------------------------------------------- 3. create backup

def test_create_backup_writes_a_real_directory_and_shows_the_manifest(settings_ui, monkeypatch):
    c = settings_ui
    parent = c.tmp_path / "backup-parent"
    parent.mkdir()
    chooser(monkeypatch, parent)
    result = action(c, c.page.backup_button.click, "last_backup")

    assert result.ok is True and result.error is None
    assert result.directory.is_dir() and result.directory.parent == parent
    assert result.directory.name.startswith("backup-")
    assert result.database_path.is_file() and result.manifest_path.is_file()
    manifest = result.manifest
    with c.runtime.sessions() as session:
        stored_media = session.scalar(select(func.count()).select_from(MediaModel.__table__))
    assert stored_media == 2
    assert manifest.media_file_count == stored_media == 2
    assert manifest.media_total_bytes == sum(entry.size for entry in manifest.media)
    for entry in manifest.media:
        assert result.media_dir.joinpath(*entry.relative_path.split("/")).is_file()

    details = c.page.backup_details.toPlainText()
    assert str(result.directory) in details
    assert c.page.backup_status.text().startswith("备份完成")
    assert f"媒体文件：{manifest.media_file_count} 个" in details
    assert human_size(manifest.media_total_bytes) in details
    assert f"{manifest.media_total_bytes:,} 字节" in details
    assert manifest.schema_revision in details
    assert f"{manifest.database_sha256[:12]}…" in details
    assert "耗时：" in details
    assert c.counts["backup"] == 1
    assert c.transport.requests == []
    assert c.errors == []


def test_backup_inside_the_data_directory_is_refused_readably(settings_ui, monkeypatch):
    c = settings_ui
    chooser(monkeypatch, c.config.storage_paths.data_dir)
    result = action(c, c.page.backup_button.click, "last_backup")

    assert result.ok is False
    assert c.page.backup_status.text().startswith("备份失败")
    details = c.page.backup_details.toPlainText()
    assert "数据目录" in result.error and result.error in details
    assert c.page.busy is False
    assert c.errors == []                             # a refusal is data, not a crash


# --------------------------------------------------------------- 4. verify backup

def test_verify_backup_checks_every_named_step_and_detects_tampering(settings_ui, monkeypatch):
    c = settings_ui
    parent = c.tmp_path / "backup-parent"
    parent.mkdir()
    answers = chooser(monkeypatch, parent)
    backup = action(c, c.page.backup_button.click, "last_backup")
    assert backup.ok is True

    answers.append(backup.directory)
    verification = action(c, c.page.verify_button.click, "last_verification")
    assert verification.ok is True
    assert c.page.verify_status.text().startswith("备份验证通过")
    summary = c.page.verify_summary.toPlainText()
    assert summary.startswith("备份验证通过") and "检查项：" in summary
    rows = c.page.check_rows()
    assert len(rows) == len(verification.checks) >= 10
    assert c.counts["threads"][-1] != threading.main_thread().name   # verified off the GUI thread
    assert {row[0] for row in rows} >= {"manifest", "database_sha256", "copy", "schema_revision",
                                        "integrity_check", "foreign_key_check", "media_files",
                                        "media_extra_files", "application_open",
                                        "integrity_audit"}
    assert {row[1] for row in rows} == {CHECK_PASSED}
    assert verification.schema_revision is not None

    # An existing directory of files is not a good backup: tampering must be caught.
    entry = backup.manifest.media[0]
    victim = backup.media_dir.joinpath(*entry.relative_path.split("/"))
    victim.write_bytes(victim.read_bytes() + b"tampered")
    answers.append(backup.directory)
    broken = action(c, c.page.verify_button.click, "last_verification")
    assert broken.ok is False
    assert c.page.verify_status.text().startswith("备份验证失败")
    failed = [row[0] for row in c.page.check_rows() if row[1] == CHECK_FAILED]
    assert failed[0] == "media_files"
    row = find_row(c.page.verify_table, 0, "media_files")
    assert row >= 0
    assert c.page.verify_table.item(row, 1).foreground().color().name() == ERROR_COLOR.name()
    assert "媒体校验失败" in c.page.check_rows()[row][2]
    assert c.counts["verify"] == 2
    assert c.errors == []


def test_a_directory_that_merely_exists_is_not_a_verified_backup(settings_ui, monkeypatch):
    c = settings_ui
    fake = c.tmp_path / "looks-like-a-backup"
    fake.mkdir()
    (fake / "database.sqlite3").write_bytes(b"not really a database")
    chooser(monkeypatch, fake)
    verification = action(c, c.page.verify_button.click, "last_verification")

    assert verification.ok is False
    assert c.page.verify_status.text().startswith("备份验证失败")
    rows = c.page.check_rows()
    assert [row[0] for row in rows] == ["manifest"]    # nothing else is even attempted
    assert rows[0][1] == CHECK_FAILED and "manifest.json" in rows[0][2]
    assert c.counts["verify"] == 1
    assert c.errors == []


# ------------------------------------------------------------------- 5. export

def test_export_writes_workspace_json_and_respects_the_privacy_switches(settings_ui, monkeypatch):
    c = settings_ui
    run = run_analysis(c)
    assert run.status == "SUCCEEDED"
    assert len(c.transport.requests) == 1

    plain = c.tmp_path / "export-plain"
    plain.mkdir()
    chooser(monkeypatch, plain)
    result = action(c, c.page.export_button.click, "last_export")

    assert result.ok is True
    assert result.json_path == plain / "workspace.json" and result.json_path.is_file()
    details = c.page.export_details.toPlainText()
    assert c.page.export_status.text().startswith("导出完成")
    assert "workspace.json" in details
    assert "person=2" in details and "event=1" in details and "evidence=1" in details
    assert "memory=1" in details and "analysis=1" in details and "media=2" in details
    assert f"媒体文件：{result.media_file_count} 个" in details
    assert human_size(result.media_total_bytes) in details
    assert result.summary() in details
    assert (plain / "README.txt").is_file() and result.media_dir.is_dir()

    document = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert document["is_backup"] is False
    assert document["privacy"] == {"credentials_included": False,
                                   "analysis_results_included": False,
                                   "analysis_snapshots_included": False}
    assert len(document["person"]) == 2 and len(document["media"]) == 2
    assert len(document["memory"]) == 1
    record = document["analysis"][0]
    assert record["id"] == str(run.id)
    # Default: metadata only. The raw answer and the snapshot stay out.
    assert record["result_included"] is False and "result" not in record
    assert record["context_snapshot_included"] is False and "context_snapshot" not in record
    exported_text = result.json_path.read_text(encoding="utf-8")
    assert FOUR_SECTIONS not in exported_text
    # The provider configuration (and therefore any credential) is not exported at all.
    assert SECRET not in exported_text
    assert "llm-providers" not in exported_text

    # Opt in, and the very same document carries the raw answer and the snapshot.
    c.page.include_results.setChecked(True)
    c.page.include_snapshots.setChecked(True)
    exposed = c.tmp_path / "export-exposed"
    exposed.mkdir()
    chooser(monkeypatch, exposed)
    result = action(c, c.page.export_button.click, "last_export")
    assert result.ok is True
    document = json.loads(result.json_path.read_text(encoding="utf-8"))
    record = document["analysis"][0]
    assert document["privacy"]["analysis_results_included"] is True
    assert record["result_included"] is True and FOUR_SECTIONS in record["result"]
    assert record["context_snapshot_included"] is True and record["context_snapshot"]
    assert c.counts["export"] == 2
    assert len(c.transport.requests) == 1
    assert c.errors == []


def test_export_refuses_a_non_empty_directory_without_crashing(settings_ui, monkeypatch):
    c = settings_ui
    occupied = c.tmp_path / "occupied"
    occupied.mkdir()
    keeper = occupied / "keep.txt"
    keeper.write_text("keep me", encoding="utf-8")
    chooser(monkeypatch, occupied)
    result = action(c, c.page.export_button.click, "last_export")

    assert result.ok is False
    assert c.page.export_status.text().startswith("导出失败")
    details = c.page.export_details.toPlainText()
    assert result.error in details and "非空" in result.error
    assert keeper.read_text(encoding="utf-8") == "keep me"
    assert not (occupied / "workspace.json").exists()
    assert c.page.busy is False
    assert c.errors == []


def test_cancelling_the_directory_chooser_starts_nothing(settings_ui, monkeypatch):
    c = settings_ui
    chooser(monkeypatch)                              # an empty answer = "cancel"
    assert c.page.create_backup() is None
    assert c.page.verify_backup() is None
    assert c.page.export_workspace() is None
    assert (c.counts["backup"], c.counts["verify"], c.counts["export"]) == (0, 0, 0)
    assert "已取消" in c.page.backup_status.text()
    assert "已取消" in c.page.verify_status.text()
    assert "已取消" in c.page.export_status.text()
    assert c.page.busy is False
    assert c.errors == []


# ------------------------------------------------------- 6. privacy and no-service

def test_no_secret_and_no_body_text_reach_the_page_or_the_log(settings_ui, monkeypatch, caplog):
    c = settings_ui
    parent = c.tmp_path / "backup-parent"
    parent.mkdir()
    exported = c.tmp_path / "export-privacy"
    exported.mkdir()
    answers = chooser(monkeypatch, parent)
    run = run_analysis(c)                              # a real run with a real answer
    assert run.status == "SUCCEEDED"
    c.page.include_results.setChecked(True)
    c.page.include_snapshots.setChecked(True)
    with caplog.at_level(logging.DEBUG, logger="app"):
        report = action(c, c.page.integrity_button.click, "last_report")
        backup = action(c, c.page.backup_button.click, "last_backup")
        answers.append(backup.directory)
        verification = action(c, c.page.verify_button.click, "last_verification")
        answers.append(exported)
        export = action(c, c.page.export_button.click, "last_export")
        c.window.navigation.setCurrentRow(0)
        c.qt_app.processEvents()
        c.window.navigation.setCurrentRow(5)
        c.qt_app.processEvents()

    assert report.ok is True and backup.ok is True
    assert verification.ok is True and export.ok is True
    # Everything ran exactly once: nothing was repeated behind the user's back.
    assert (c.counts["integrity"], c.counts["backup"], c.counts["verify"],
            c.counts["export"]) == (1, 1, 1, 1)
    text = page_text(c.page)
    assert SECRET not in text                          # no API key, ever
    assert CREDENTIAL_REF not in text                  # no credential reference either
    assert PRIVATE_MEMORY not in text                   # no Memory body text
    assert EVIDENCE_BODY not in text                    # no Evidence body text
    assert SECRET not in caplog.text
    assert PRIVATE_MEMORY not in caplog.text
    assert EVIDENCE_BODY not in caplog.text
    # The explicitly requested export does contain the answer - in its file only.
    assert export.json_path.is_file()
    document = json.loads(export.json_path.read_text(encoding="utf-8"))
    assert document["privacy"]["analysis_results_included"] is True
    assert FOUR_SECTIONS in document["analysis"][0]["result"]
    assert document["analysis"][0]["context_snapshot"]
    assert c.errors == []


def test_a_page_without_services_explains_itself(qt_app, tmp_path):
    page = SettingsPage(AppConfig(data_dir=tmp_path / "no-services"), None)
    try:
        assert page.objectName() == "settings"
        assert page.activate() == []
        for button in (page.integrity_button, page.backup_button, page.verify_button,
                       page.export_button):
            assert button.isEnabled() is False
        assert "尚未装配" in page.service_note.text()
        assert page.run_integrity_check() is None
        assert page.create_backup() is None
        assert page.verify_backup() is None
        assert page.export_workspace() is None
        assert page.busy is False and page.busy_label.text() == ""
    finally:
        page.close()
        qt_app.processEvents()
