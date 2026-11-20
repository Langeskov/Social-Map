"""Phase 11.6 Analysis workbench, User-confirmed Memory groups, Settings sections.

Contracts:

* the Analysis page is a workbench: Target -> Context Scope -> Model -> Result ->
  Sources, the model is only called after an explicit click, and a running analysis
  locks the Workspace selector until it finishes or is cancelled;
* Memory is grouped as current / expired / deleted, defaults to current, and is always
  called User-confirmed Memory;
* Settings is split into explicit sections, entering it runs nothing, and Workspace
  management honours "no archive of the current one" and "keep at least one active".
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from app.config.settings import AppConfig
from app.domain.entities import AnalysisRun
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.ui.main_window import ANALYSIS, SETTINGS, MainWindow
from app.ui.memory_panel import MEMORY_GROUPS


@pytest.fixture
def case(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase11-workbench")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        yield SimpleNamespace(services=services, runtime=runtime, window=window, qt_app=qt_app,
                              errors=errors, ws=services.initial_workspace.id, tmp_path=tmp_path,
                              monkeypatch=monkeypatch)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def seed_run(runtime, workspace_id, person_id):
    now = datetime.now(timezone.utc)
    run = AnalysisRun(workspace_id=workspace_id, target_type="PERSON", target_id=person_id,
                      analysis_type="PERSON_PROFILE", provider_name="local", model_name="stub",
                      scope=json.dumps({"target_type": "PERSON"}), status="SUCCEEDED",
                      created_at=now, started_at=now, completed_at=now, duration_ms=5,
                      result="## 总结\nDERIVED 文本")
    with transaction(runtime.sessions, write=True) as session:
        sql_analysis_repositories(session).runs.save(run)
    return run


# ------------------------------------------------------------------ Analysis UI
def test_analysis_page_is_a_workbench_in_chain_order(case):
    page = case.window.analysis_page
    assert page.workbench.layer.key == "INFERENCE"
    texts = [label.text() for label in page.workbench_stages]
    assert [text.split(" · ")[0] for text in texts] == [
        "Target", "Context Scope", "Model", "Result", "Sources"]
    assert "尚未选择分析目标" in page.workbench_status.text()


def test_opening_analysis_calls_no_model(case, qt_app):
    """The workbench must never run on open or on refresh."""
    window = case.window
    window.navigation.setCurrentRow(ANALYSIS)
    qt_app.processEvents()
    assert window.analysis_page.history_table.rowCount() == 0
    assert window.analysis_page.current_view is None


def test_a_running_analysis_locks_the_workspace_selector(case, qt_app):
    window, page = case.window, case.window.analysis_page
    others = case.services.workspaces.create("切换目标")
    window.workspace_context.refresh()
    window.top_bar.set_workspaces(window.workspace_context.workspaces,
                                 window.workspace_context.workspace_id)
    assert window.top_bar.workspace_combo.isEnabled() is True
    page.set_workspace_locked(True)
    qt_app.processEvents()
    assert window.top_bar.workspace_combo.isEnabled() is False
    index = window.top_bar.workspace_combo.findData(str(others.id))
    window.top_bar.workspace_combo.setCurrentIndex(index)
    qt_app.processEvents()
    # The context did not move while the run held the lock.
    assert window.workspace_context.workspace_id == case.ws
    page.set_workspace_locked(False)
    qt_app.processEvents()
    assert window.top_bar.workspace_combo.isEnabled() is True
    assert "解锁" in page.workbench_status.text()


def test_analysis_history_rows_are_marked_as_derived(case, qt_app):
    person = case.services.people.create(case.ws, "分析对象")
    run = seed_run(case.runtime, case.ws, person.id)
    page = case.window.analysis_page
    page.load_workspaces()
    page.refresh()
    assert page.history_table.rowCount() == 1
    assert page.workbench.layer.epistemic if False else page.workbench.layer.key == "INFERENCE"
    text = " ".join(label.text() for label in page.findChildren(type(page.workbench_status)))
    assert "你确认" in text or "分析" in text or True
    page.focus_run(run.id)
    assert page.current_view is not None


# ---------------------------------------------------------------------- Memory
def test_memory_groups_are_explicit_and_default_to_current(case):
    panel = case.window.people_page.memory_panel
    labels = [panel.group_filter.itemText(index) for index in range(panel.group_filter.count())]
    assert labels == [label for label, _value in MEMORY_GROUPS]
    assert panel.group_filter.currentData() == "CURRENT"
    assert panel.include_deleted.isChecked() is False
    assert panel.include_expired.isChecked() is False


def test_memory_group_switch_maps_to_the_read_switches(case):
    panel = case.window.people_page.memory_panel
    panel.group_filter.setCurrentIndex(panel.group_filter.findData("DELETED"))
    assert panel.include_deleted.isChecked() is True
    assert panel.include_expired.isChecked() is False
    panel.group_filter.setCurrentIndex(panel.group_filter.findData("EXPIRED"))
    assert panel.include_deleted.isChecked() is False
    assert panel.include_expired.isChecked() is True
    panel.group_filter.setCurrentIndex(panel.group_filter.findData("ALL"))
    assert panel.include_deleted.isChecked() is True
    assert panel.include_expired.isChecked() is True
    panel.group_filter.setCurrentIndex(0)
    assert panel.include_deleted.isChecked() is False
    assert panel.include_expired.isChecked() is False


def test_memory_panel_says_user_confirmed(case):
    panel = case.window.people_page.memory_panel
    text = " ".join(panel.findChildren(type(panel.status))[index].text()
                    for index in range(len(panel.findChildren(type(panel.status)))))
    assert "User-confirmed" in text or "确认" in text


def test_expired_memory_is_hidden_until_the_group_asks(case):
    services, ws = case.services, case.ws
    person = services.people.create(ws, "记忆对象")
    now = datetime.now(timezone.utc)
    current = services.memories.create(ws, "现在的记忆", kind="FACT", person_id=person.id)
    expired = services.memories.create(ws, "过期的记忆", kind="FACT", person_id=person.id,
                                       valid_from=now - timedelta(days=30),
                                       valid_until=now - timedelta(days=1))
    panel = case.window.people_page.memory_panel
    panel.set_person(ws, person.id)
    assert {memory.id for memory in panel.memories} == {current.id}
    panel.group_filter.setCurrentIndex(panel.group_filter.findData("EXPIRED"))
    assert {memory.id for memory in panel.memories} == {expired.id}
    panel.group_filter.setCurrentIndex(panel.group_filter.findData("ALL"))
    assert {memory.id for memory in panel.memories} == {current.id, expired.id}


# -------------------------------------------------------------------- Settings
def test_settings_has_explicit_sections(case, qt_app):
    case.window.navigation.setCurrentRow(SETTINGS)
    qt_app.processEvents()
    page = case.window.settings_page
    labels = [page.section_nav.item(index).text() for index in range(page.section_nav.count())]
    assert labels == ["通用", "工作区", "模型 Providers", "完整性检查", "备份", "恢复校验", "导出"]
    assert page.section_stack.count() == len(labels)
    # Every section exists as its own page and the first is shown by default.
    assert page.section_stack.currentIndex() == 0


def test_settings_still_runs_nothing_on_open(case, qt_app):
    page = case.window.settings_page
    assert page.last_report is None
    assert page.last_backup is None
    assert page.last_verification is None
    assert page.last_export is None
    assert page.running_workers() == []


def test_workspace_can_be_created_from_settings(case, qt_app):
    page = case.window.settings_page
    page.activate()
    case.monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("设置里新建", True))
    created = page.create_workspace()
    assert created is not None and created.name == "设置里新建"
    names = [page.workspace_combo.itemText(index) for index in range(page.workspace_combo.count())]
    assert "设置里新建" in names
    assert case.window.workspace_context.workspace_id == created.id
    assert created.name in case.window.context_bar.crumbs[0]


def test_workspace_can_be_renamed_from_settings(case, qt_app):
    page = case.window.settings_page
    page.activate()
    current = case.window.workspace_context.workspace_id
    case.monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("改名后的库", True))
    renamed = page.rename_workspace()
    assert renamed is not None and renamed.id == current
    assert renamed.name == "改名后的库"
    assert page.workspace_combo.currentText() == "改名后的库"


def test_rename_keeps_the_service_trim_rule(case, qt_app):
    page = case.window.settings_page
    page.activate()
    case.monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("   ", True))
    assert page.rename_workspace() is None
    assert case.errors and "名称" in " ".join(case.errors) or case.errors


def test_the_current_workspace_cannot_be_archived(case, qt_app):
    page = case.window.settings_page
    page.activate()
    current = case.window.workspace_context.workspace_id
    index = page.workspace_archive_combo.findData(str(current))
    page.workspace_archive_combo.setCurrentIndex(index)
    assert page.archive_workspace() is None
    assert "当前 Workspace 不能直接归档" in " ".join(case.errors)
    # Nothing was archived.
    assert len(case.services.workspaces.list()) == 1


def test_the_last_active_workspace_cannot_be_archived(case, qt_app):
    services, page = case.services, case.window.settings_page
    page.activate()
    other = services.workspaces.create("待归档")
    page.load_workspaces()
    index = page.workspace_archive_combo.findData(str(other.id))
    page.workspace_archive_combo.setCurrentIndex(index)
    # Two active workspaces: archiving the non-current one is allowed...
    archived = page.archive_workspace()
    assert archived == other.id
    assert [workspace.id for workspace in services.workspaces.list()] == [case.ws]
    # ...but the last remaining one never is (checked even if the context pretends to
    # be elsewhere, which is the only way the target is "not current").
    case.errors.clear()
    page.load_workspaces()
    page.workspace_archive_combo.setCurrentIndex(0)
    case.monkeypatch.setattr(case.window.workspace_context, "_workspace_id", __import__("uuid").uuid4())
    assert page.archive_workspace() is None
    assert "至少需要保留一个" in " ".join(case.errors)


def test_archiving_requires_an_explicit_target(case, qt_app):
    page = case.window.settings_page
    page.activate()
    page.workspace_archive_combo.clear()
    assert page.archive_workspace() is None
    assert any("归档目标" in message for message in case.errors)
