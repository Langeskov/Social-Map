"""Phase 11.4 hardening: ContextBar object segment and Ctrl+S (save and close).

Contracts:

* the breadcrumb is ``Workspace › Page › Object`` and the object segment only ever
  shows what the page actually has selected (or nothing);
* the object segment disappears when the selection is cleared, when the Workspace
  changes, and when the selected record is archived/deleted;
* long object names are elided, and every label stays PlainText;
* Ctrl+S runs the same path as the dialog's Accepted button ("保存并关闭"), so no
  dialog gets a second, parallel write path.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.event_dialog import EventDialog
from app.ui.evidence_dialog import EvidenceMetadataDialog
from app.ui.main_window import ANALYSIS, EVENTS, GRAPH, PEOPLE, SETTINGS, MainWindow
from app.ui.person_dialog import PersonDialog


@pytest.fixture
def case(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase11-interaction")
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


def crumbs(window):
    return list(window.context_bar.crumbs)


# ------------------------------------------------------------- ContextBar object
def test_person_breadcrumb_shows_the_selected_person(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "面包屑人物")
    window.open_target("person", person.id)
    case.qt_app.processEvents()
    assert crumbs(window) == [services.initial_workspace.name, "People", "面包屑人物"]
    assert window.pages.currentIndex() == PEOPLE


def test_event_breadcrumb_shows_the_selected_event(case):
    services, window = case.services, case.window
    event = services.events.create_event(case.ws, "面包屑事件", "Meeting", occurred_on=date(2026, 6, 1))
    window.open_target("event", event.id)
    case.qt_app.processEvents()
    assert crumbs(window) == [services.initial_workspace.name, "Events", "面包屑事件"]
    assert window.pages.currentIndex() == EVENTS


def test_analysis_breadcrumb_shows_the_run_id(case):
    from app.domain.entities import AnalysisRun
    from app.services.analysis_repositories import sql_analysis_repositories
    from app.services.unit_of_work import transaction
    services, window = case.services, case.window
    person = services.people.create(case.ws, "分析面包屑")
    now = datetime.now(timezone.utc)
    run = AnalysisRun(workspace_id=case.ws, target_type="PERSON", target_id=person.id,
                      analysis_type="PERSON_PROFILE", provider_name="local", model_name="stub",
                      scope='{"target_type": "PERSON"}', status="SUCCEEDED",
                      created_at=now, started_at=now, completed_at=now, duration_ms=3,
                      result="## 总结\n内容")
    with transaction(case.runtime.sessions, write=True) as session:
        sql_analysis_repositories(session).runs.save(run)
    window.open_target("analysis_run", run.id)
    case.qt_app.processEvents()
    assert window.pages.currentIndex() == ANALYSIS
    assert crumbs(window)[:2] == [services.initial_workspace.name, "Analysis"]
    assert crumbs(window)[2] == f"Run {str(run.id)[:8]}"


def test_pages_without_an_object_stop_at_the_page_segment(case):
    window = case.window
    for index in (GRAPH, SETTINGS):
        window.navigation.setCurrentRow(index)
        case.qt_app.processEvents()
        assert len(crumbs(window)) == 2
        assert crumbs(window)[0] == case.services.initial_workspace.name


def test_breadcrumb_object_disappears_when_the_selection_is_cleared(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "会被清空")
    window.open_target("person", person.id)
    case.qt_app.processEvents()
    assert len(crumbs(window)) == 3
    window.people_page.people_list.setCurrentItem(None)
    case.qt_app.processEvents()
    assert crumbs(window) == [services.initial_workspace.name, "People"]


def test_breadcrumb_object_is_cleared_by_a_workspace_switch(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "切换前人物")
    window.open_target("person", person.id)
    case.qt_app.processEvents()
    assert crumbs(window)[-1] == "切换前人物"
    other = services.workspaces.create("另一个库")
    window.workspace_context.refresh()
    window.top_bar.set_workspaces(window.workspace_context.workspaces,
                                  window.workspace_context.workspace_id)
    window.top_bar.workspace_combo.setCurrentIndex(
        window.top_bar.workspace_combo.findData(str(other.id)))
    case.qt_app.processEvents()
    assert crumbs(window) == [other.name, "People"]


def test_archiving_the_selected_person_clears_the_object_segment(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "将被归档")
    window.open_target("person", person.id)
    case.qt_app.processEvents()
    window.people_page.archive_person()
    case.qt_app.processEvents()
    assert crumbs(window) == [services.initial_workspace.name, "People"]


def test_long_object_names_are_elided_and_plain_text(case):
    services, window = case.services, case.window
    long_name = "很长的" * 40
    person = services.people.create(case.ws, long_name)
    window.open_target("person", person.id)
    case.qt_app.processEvents()
    label = crumbs(window)[-1]
    assert len(label) == 41
    assert label.endswith("…")
    from PySide6.QtWidgets import QLabel
    for widget in window.context_bar.findChildren(QLabel):
        assert widget.textFormat() == Qt.TextFormat.PlainText


# --------------------------------------------------------------------- Ctrl+S
def test_person_dialog_ctrl_s_saves_and_closes(qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    dialog = PersonDialog()
    dialog.show()
    qt_app.processEvents()
    assert dialog.save_shortcut is not None
    dialog.fields["display_name"].setText("快捷键人物")
    QTest.keyClick(dialog, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qt_app.processEvents()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.values()["display_name"] == "快捷键人物"
    assert errors == []


def test_event_dialog_ctrl_s_saves_and_closes(qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    dialog = EventDialog([])
    dialog.show()
    qt_app.processEvents()
    dialog.title.setText("快捷键事件")
    dialog.event_type.setText("Meeting")
    dialog.occurred_on.setText("2026-06-01")
    QTest.keyClick(dialog, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qt_app.processEvents()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.values()["title"] == "快捷键事件"
    assert errors == []


def test_event_dialog_ctrl_s_still_validates(qt_app, monkeypatch):
    """Save-and-close reuses the Accepted path, so validation still refuses."""
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    dialog = EventDialog([])
    dialog.show()
    qt_app.processEvents()
    dialog.title.setText("缺少时间")
    dialog.event_type.setText("Meeting")
    QTest.keyClick(dialog, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qt_app.processEvents()
    assert dialog.result() != dialog.DialogCode.Accepted
    assert errors


def test_evidence_metadata_dialog_ctrl_s_saves_and_closes(case, qt_app):
    services, ws = case.services, case.ws
    event = services.events.create_event(ws, "证据事件", "Meeting", occurred_on=date(2026, 6, 2))
    evidence = services.evidence.create_evidence(ws, event.id, "TEXT", "原标题", content_text="原文")
    detail = services.evidence.detail(ws, evidence.id)
    dialog = EvidenceMetadataDialog(detail)
    dialog.show()
    qt_app.processEvents()
    dialog.title.setText("新标题")
    QTest.keyClick(dialog, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qt_app.processEvents()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.values()["title"] == "新标题"


def test_every_editing_dialog_exposes_the_save_shortcut(case, qt_app):
    """The shortcut is installed once per editing dialog, not per page."""
    from app.ui.analysis_dialog import ProviderDialog
    from app.ui.memory_panel import MemoryEditDialog
    from app.ui.relationship_panel import RelationshipDialog, StateDialog
    from app.ui.evidence_dialog import EvidenceCreateDialog

    services = case.services
    dialogs = [
        PersonDialog(),
        EventDialog([]),
        MemoryEditDialog(),
        StateDialog(),
        RelationshipDialog([]),
        ProviderDialog(),
        EvidenceCreateDialog(services.evidence, lambda search=None: []),
    ]
    try:
        for dialog in dialogs:
            assert dialog.save_shortcut is not None, type(dialog).__name__
            assert dialog.save_shortcut.key().toString().upper().endswith("+S") or \
                "S" in dialog.save_shortcut.key().toString()
    finally:
        for dialog in dialogs:
            dialog.close()
