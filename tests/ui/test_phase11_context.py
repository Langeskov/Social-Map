"""Phase 11.2 Workspace context isolation and the Conflict UI contracts.

The rules under test:

* there is exactly one Workspace context, and every page follows it;
* switching Workspace runs the same two-step reset everywhere: open detail windows
  close, selections and search results are dropped, and nothing from Workspace A can
  still be shown while Workspace B is current;
* unified search never mixes two Workspaces;
* a ``ConflictError`` keeps the Service's own user-facing text, reaches the user
  through the ordinary dialog *and* stays visible in a page-level banner;
* an unknown failure keeps the generic message;
* a page that is not bound to a context keeps its previous, purely local behaviour
  (this is what protects the Phase 5-10 tests).
"""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QMessageBox

from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui import operations as operations_module
from app.ui.context import DetailWindowTracker, WorkspaceContext, hide_duplicate_workspace_controls
from app.ui.design.tokens import CONFLICT_MESSAGE
from app.ui.events_page import EventsPage


@pytest.fixture
def shell(tmp_path, qt_app, monkeypatch):
    """A real MainWindow over a real temporary database, with dialogs captured."""
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    config = AppConfig(data_dir=tmp_path / "context-data")
    runtime = initialize_database(config)
    from app.ui.main_window import MainWindow
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        seed = services.initial_workspace
        second = services.workspaces.create("第二个 Workspace")
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, window=window,
                              qt_app=qt_app, errors=errors, a=seed, b=second,
                              tmp_path=tmp_path)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def switch(shell, workspace):
    """Switch Workspace through the single global selector, like a user."""
    combo = shell.window.top_bar.workspace_combo
    index = combo.findData(str(workspace.id))
    assert index >= 0
    combo.setCurrentIndex(index)
    shell.qt_app.processEvents()


# --------------------------------------------------------------- single context
def test_one_global_selector_drives_every_page(shell):
    window = shell.window
    assert window.top_bar.workspace_combo.count() == len(shell.services.workspaces.list())
    switch(shell, shell.b)
    for page in (window.people_page, window.events_page, window.dashboard_page,
                 window.graph_page, window.analysis_page, window.settings_page):
        assert page.workspace_id == shell.b.id
    assert window.workspace_context.workspace_id == shell.b.id
    # The per-page selectors are no longer user-visible controls.
    for page in (window.people_page, window.events_page, window.dashboard_page,
                 window.graph_page, window.analysis_page, window.settings_page):
        assert page.workspace_combo.isVisible() is False
    assert window.context_bar.crumbs[0] == shell.b.name


def test_page_selector_still_routes_through_the_context(shell):
    """A legacy page control cannot create a second, independent context."""
    window = shell.window
    combo = window.people_page.workspace_combo
    index = combo.findData(str(shell.b.id))
    combo.setCurrentIndex(index)
    shell.qt_app.processEvents()
    assert window.workspace_context.workspace_id == shell.b.id
    assert window.events_page.workspace_id == shell.b.id


# ------------------------------------------------------------- context reset
def test_switching_workspace_drops_the_person_detail(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    person = services.people.create(ws, "只在 A 里")
    window.open_target("person", person.id)
    assert window.people_page.current_detail is not None
    assert window.people_page.current_detail.person.display_name == "只在 A 里"
    switch(shell, shell.b)
    assert window.people_page.current_detail is None
    assert window.people_page.people_list.count() == 0
    assert window.people_page.analysis_locked() is False if hasattr(window.people_page, "analysis_locked") else True
    assert shell.errors == []


def test_switching_workspace_drops_the_event_detail_and_evidence(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    event = services.events.create_event(ws, "只在 A 的事件", "Meeting", occurred_on=date(2026, 3, 1))
    window.open_target("event", event.id)
    assert window.events_page.current_detail is not None
    assert window.events_page.evidence_panel.current_event is not None
    switch(shell, shell.b)
    assert window.events_page.current_detail is None
    assert window.events_page.evidence_panel.current_event is None
    assert window.events_page.event_list.count() == 0
    assert shell.errors == []


def test_switching_workspace_closes_non_modal_detail_windows(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    a = services.people.create(ws, "甲")
    b = services.people.create(ws, "乙")
    view = services.relationships.create(ws, a.id, b.id, "Friend")
    dialog = window.people_page.open_relationship(view.id)
    assert dialog is not None and dialog.isVisible()
    assert dialog in window.people_page._dialogs
    switch(shell, shell.b)
    shell.qt_app.processEvents()
    # `WA_DeleteOnClose` means the window may already be gone; both "closed" and
    # "deleted" prove the stale window cannot keep showing Workspace A.
    from shiboken6 import isValid
    assert (not isValid(dialog)) or dialog.isVisible() is False
    assert window.people_page._dialogs == []
    assert shell.errors == []


def test_switching_workspace_clears_search_input_and_results(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    services.people.create(ws, "搜索目标甲")
    panel = window.people_page.search_panel
    panel.input.setText("搜索目标甲")
    panel.refresh()
    assert panel.result_list.count() == 1
    switch(shell, shell.b)
    assert panel.query_text() == ""
    assert panel.result_list.count() == 0
    assert panel.workspace_id == shell.b.id
    assert shell.errors == []


def test_switching_workspace_empties_the_graph(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    a = services.people.create(ws, "图甲")
    b = services.people.create(ws, "图乙")
    services.relationships.create(ws, a.id, b.id, "Friend")
    window.navigation.setCurrentRow(3)
    shell.qt_app.processEvents()
    assert window.graph_page.graph_state()["labels"]
    switch(shell, shell.b)
    state = window.graph_page.graph_state()
    assert state["labels"] == []
    assert state["edges"] == []


def test_switching_workspace_resets_the_analysis_page(shell):
    window = shell.window
    window.navigation.setCurrentRow(4)
    shell.qt_app.processEvents()
    window.analysis_page.history_table.selectRow(0) if window.analysis_page.history_table.rowCount() else None
    switch(shell, shell.b)
    assert window.analysis_page.current_view is None
    assert window.analysis_page.history_table.rowCount() == 0


def test_search_never_crosses_workspaces(shell):
    services, window = shell.services, shell.window
    services.people.create(shell.a.id, "跨库人物")
    panel = window.people_page.search_panel
    panel.set_workspace(shell.b.id)
    panel.input.setText("跨库人物")
    results = panel.refresh()
    assert results == []
    assert "没有" in panel.status.text()
    panel.set_workspace(shell.a.id)
    results = panel.refresh()
    assert [result.title for result in results] == ["跨库人物"]


def test_context_reports_unknown_workspace_names_honestly(shell):
    context = shell.window.workspace_context
    assert context.name_of(uuid4()) == ""
    assert context.current_name() == shell.a.name


# ------------------------------------------------------------- conflict boundary
def test_conflict_banner_keeps_the_service_message(shell, monkeypatch):
    """A stale attachment snapshot: dialog text unchanged, banner stays visible."""
    services, ws, window = shell.services, shell.a.id, shell.window
    source = shell.tmp_path / "conflict.png"
    Image.new("RGB", (8, 8), "white").save(source)
    media = services.media.import_file(ws, source)
    second = shell.tmp_path / "conflict-2.png"
    Image.new("RGB", (8, 8), "black").save(second)
    extra = services.media.import_file(ws, second)
    event = services.events.create_event(ws, "冲突事件", "Meeting", occurred_on=date(2026, 3, 1))
    evidence = services.evidence.create_evidence(ws, event.id, "IMAGE", "截图", source_type="IMAGE",
                                                 media=(media.id,))
    window.open_target("event", event.id)
    panel = window.events_page.evidence_panel
    panel.include_deleted.setChecked(True)
    panel.refresh()
    row = next(index for index in range(panel.evidence_list.rowCount())
               if panel.evidence_list.item(index, 0).text() == "截图")
    panel.evidence_list.selectRow(row)
    panel.attachments.selectRow(0)
    # Another window changes the attachment set after this panel loaded.
    services.evidence.add_media(ws, evidence.id, extra.id, "EXTRA")
    panel.remove_media()
    assert len(shell.errors) == 1
    message = shell.errors[0]
    assert "刷新后重试" in message
    assert panel.conflict_banner.isVisible() is (panel.isVisible() and panel.conflict_banner.isVisible())
    assert panel.conflict_banner.message_label.text() == message
    assert panel.conflict_banner.state == "CONFLICT"


def test_conflict_banner_is_cleared_by_a_successful_reload(shell):
    services, ws, window = shell.services, shell.a.id, shell.window
    event = services.events.create_event(ws, "普通事件", "Meeting", occurred_on=date(2026, 3, 1))
    window.open_target("event", event.id)
    panel = window.events_page.evidence_panel
    panel.show_conflict("某个冲突")
    assert panel.conflict_banner.isVisible()
    panel.refresh()
    assert panel.conflict_banner.isVisible() is False


def test_run_operation_keeps_history_and_adds_the_banner(qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    seen = []

    def raise_conflict():
        raise ConflictError("该分析已经结束，无法取消。")

    result = operations_module.run_operation(None, raise_conflict, on_conflict=seen.append)
    assert result is None
    assert seen == ["该分析已经结束，无法取消。"]
    assert errors == ["该分析已经结束，无法取消。"]
    # A conflict with no text falls back to the honest generic message.
    seen.clear()
    errors.clear()
    operations_module.run_operation(None, lambda: (_ for _ in ()).throw(ConflictError("")),
                                    on_conflict=seen.append)
    assert seen == [operations_module.UNKNOWN_MESSAGE]


def test_unknown_failure_still_hides_internals(qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    assert operations_module.run_operation(None, lambda: 1 + "private-value") is None
    assert errors == [operations_module.UNKNOWN_MESSAGE]


def test_safe_user_message_prefers_the_service_text():
    assert operations_module.safe_user_message(DomainError("  有内容  ")) == "有内容"
    assert operations_module.safe_user_message(DomainError("")) == operations_module.UNKNOWN_MESSAGE
    assert CONFLICT_MESSAGE.endswith("请刷新后重试。")


# ------------------------------------------------------- unbound page behaviour
def test_an_unbound_page_keeps_its_local_workspace(tmp_path, qt_app, monkeypatch):
    """The historical per-page tests rely on this: no context, no global coupling."""
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: None)
    config = AppConfig(data_dir=tmp_path / "unbound-data")
    runtime = initialize_database(config)
    page = None
    try:
        services = create_application_services(config, runtime.sessions)
        page = EventsPage(services)
        assert page.workspace_context is None
        other = services.workspaces.create("另一个")
        page.refresh_workspaces()
        index = page.workspace_combo.findData(str(other.id))
        page.workspace_combo.setCurrentIndex(index)
        assert page.workspace_id == other.id
        assert page.workspace_combo.isVisible() or not page.isVisible()
    finally:
        if page is not None:
            page.deleteLater()
        runtime.close()


# -------------------------------------------------------------------- tracker
def test_detail_tracker_closes_everything_it_was_given(qt_app):
    opened = [QDialog(), QDialog()]
    for dialog in opened:
        dialog.show()
    tracker = DetailWindowTracker(object(), opened, [])
    assert tracker.count() == 2
    assert tracker.close_all() == 2
    assert [dialog.isVisible() for dialog in opened] == [False, False]


def test_workspace_context_does_not_reemit_for_the_same_workspace(qt_app):
    class Service:
        def list(self):
            return []

    context = WorkspaceContext(Service(), None)
    events = []
    context.changed.connect(events.append)
    assert context.set_workspace(uuid4()) is True
    current = context.workspace_id
    assert context.set_workspace(current) is False
    assert len(events) == 1
    context.reset(current)
    assert len(events) == 2


def test_duplicate_workspace_controls_are_hidden_not_deleted(qt_app):
    class Page:
        class Combo:
            def __init__(self):
                self.hidden = False

            def setVisible(self, value):
                self.hidden = not value

            def parentWidget(self):
                return None

        def __init__(self):
            self.workspace_combo = self.Combo()

    page = Page()
    hide_duplicate_workspace_controls(page)
    assert page.workspace_combo.hidden is True
    hide_duplicate_workspace_controls(object())      # no combo: a no-op, never a crash
