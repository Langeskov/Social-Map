"""Phase 7 shell contracts: six pages, Graph navigation and cross-page jumps.

Additional Phase 7 coverage appended here: the unified search UI (shared by
Dashboard and People) and the complete Timeline widget (Events *and* relationship
state changes, future ones included).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.event_detail import ROLE
from app.ui.main_window import MainWindow
from app.ui.pages import PlaceholderPage
from app.ui.search_panel import SearchPanel
from app.ui.timeline_panel import MODE_PERSON, MODE_WORKSPACE, RelationshipDetailDialog, TimelineRow

PAGE_ORDER = ["dashboard", "people", "events", "graph", "analysis", "settings"]


@pytest.fixture
def shell(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    config = AppConfig(data_dir=tmp_path / "navigation-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        workspace = services.initial_workspace
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              window=window, qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def test_six_pages_in_frozen_order(shell):
    window = shell.window
    assert window.pages.count() == 6
    assert [window.pages.widget(index).objectName() for index in range(6)] == PAGE_ORDER
    assert window.navigation.count() == 6
    assert "Graph" in window.navigation.item(3).text()


def test_every_page_is_reachable_by_navigation(shell):
    window = shell.window
    for index in range(6):
        window.navigation.setCurrentRow(index)
        assert window.pages.currentIndex() == index
        assert window.pages.currentWidget().objectName() == PAGE_ORDER[index]


def test_analysis_page_is_real_and_shows_no_fabricated_data(shell):
    """Phase 8 replaced the placeholder with the analysis history page."""
    window = shell.window
    window.navigation.setCurrentRow(4)
    analysis = window.pages.currentWidget()
    assert analysis.objectName() == "analysis"
    assert hasattr(analysis, "history_table") and hasattr(analysis, "provider_button")
    # An empty workspace shows an honest empty history, not invented runs.
    assert analysis.history_table.rowCount() == 0
    texts = " ".join(label.text() for label in analysis.findChildren(QLabel))
    assert "未" in texts or "没有" in texts
    assert "demo" not in texts.lower()


def test_graph_page_reads_services_without_persisting(shell):
    services, ws = shell.services, shell.ws
    a = services.people.create(ws, "导航甲")
    b = services.people.create(ws, "导航乙")
    services.relationships.create(ws, a.id, b.id, "Friend")
    graph = shell.window.pages.widget(3)
    assert graph.rebuild_graph() is not None
    state = graph.graph_state()
    assert sorted(state["labels"]) == sorted(["导航甲", "导航乙"])
    assert len(state["edges"]) == 1
    assert shell.errors == []


def test_cross_page_jump_selects_person_and_event(shell):
    services, ws, window = shell.services, shell.ws, shell.window
    person = services.people.create(ws, "跳转人物")
    event = services.events.create_event(ws, "跳转事件", "Meeting", occurred_on=None,
                                         started_at=__import__("datetime").datetime(
                                             2026, 5, 1, 10, tzinfo=__import__("datetime").timezone.utc))
    window.open_target("person", person.id)
    assert window.pages.currentIndex() == 1
    assert window.people_page.current_detail is not None
    assert window.people_page.current_detail.person.id == person.id
    window.open_target("event", event.id)
    assert window.pages.currentIndex() == 2
    assert window.events_page.current_detail is not None
    assert window.events_page.current_detail.event.id == event.id
    assert shell.errors == []


def test_cross_page_jump_ignores_unknown_targets(shell):
    window = shell.window
    window.navigation.setCurrentRow(0)
    window.open_target("nonsense", uuid4())
    window.open_target("person", "not-a-uuid")
    window.open_target("event", None)
    assert window.pages.currentIndex() == 0
    # A well-formed id for a target that has no such row still only switches page
    # and must not raise.
    window.open_target("person", uuid4())
    assert window.pages.currentIndex() == 1
    assert shell.errors == []


# ------------------------------------------------------------------ search UI

@pytest.fixture
def search_case(shell):
    """The shell plus one Event / Evidence / Alias to search for."""
    services, ws = shell.services, shell.ws
    alpha = services.people.create(ws, "ALPHA")
    beta = services.people.create(ws, "BETA")
    services.people.add_alias(ws, alpha.id, "ALIAS-ONE", "NICKNAME")
    now = datetime.now(timezone.utc)
    event = services.events.create_event(ws, "Chat evening", "Message", record_kind="FACT",
                                         started_at=now - timedelta(days=1), participants=(alpha.id,))
    future_event = services.events.create_event(ws, "Planned trip", "Activity", record_kind="FACT",
                                                started_at=now + timedelta(days=45),
                                                participants=(alpha.id,))
    evidence = services.evidence.create_evidence(ws, event.id, kind="TEXT", title="chat notes",
                                                 source_type="CHAT", content_text="chat transcript")
    shell.alpha, shell.beta, shell.event, shell.future_event, shell.evidence = (
        alpha, beta, event, future_event, evidence)
    return shell


def test_unified_search_entry_exists_on_dashboard_and_people(shell):
    people_panel = shell.window.people_page.search_panel
    dashboard_panel = shell.window.dashboard_page.search_panel
    assert isinstance(people_panel, SearchPanel)
    assert isinstance(dashboard_panel, SearchPanel)
    assert people_panel.objectName() == "peopleSearch"
    assert dashboard_panel.objectName() == "dashboardSearch"
    assert people_panel.workspace_id == shell.ws
    assert dashboard_panel.workspace_id == shell.ws
    assert people_panel.input.placeholderText()
    assert people_panel.rebuild_button.text() == "重建搜索索引"
    assert shell.errors == []


@pytest.mark.parametrize("query", ["ALPHA", "ALPHA*", "chat", '"chat transcript"', "-ALPHA",
                                   "ALPHA AND BETA", "ALPHA OR BETA", "NOT ALPHA", "*", '"', "-",
                                   "ALPHA* -BETA", " AND ", '""'])
def test_search_accepts_every_query_shape_without_crashing(search_case, query):
    panel = search_case.window.people_page.search_panel
    panel.input.setText(query)
    results = panel.refresh()
    assert results is None or isinstance(results, list)
    assert panel.result_list.count() == len(results or [])
    # The raw text reached the service unchanged: the UI never rewrites FTS syntax.
    assert panel.query_text() == query
    assert search_case.errors == []


def test_search_finds_person_alias_and_event_and_evidence(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("ALPHA")
    assert "PERSON" in {result.entity_type for result in panel.refresh()}
    panel.input.setText("ALIAS-ONE")
    alias_results = panel.refresh()
    assert [result.entity_type for result in alias_results] == ["ALIAS"]
    assert alias_results[0].metadata["person_id"] == search_case.alpha.id
    panel.input.setText("Planned trip")
    assert "EVENT" in {result.entity_type for result in panel.refresh()}
    panel.input.setText("chat transcript")
    evidence_results = panel.refresh()
    assert "EVIDENCE" in {result.entity_type for result in evidence_results}
    assert evidence_results[0].metadata["event_id"] == search_case.event.id
    assert search_case.errors == []


def test_search_result_rows_show_type_title_time_and_summary(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("Chat evening")
    panel.refresh()
    text = panel.result_list.item(0).text()
    assert "事件" in text
    assert "Chat evening" in text
    assert "+00:00" in text
    assert search_case.errors == []


def test_clicking_person_result_selects_that_person(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("BETA")
    panel.refresh()
    panel.result_list.setCurrentRow(0)
    panel.open_item(panel.result_list.item(0))          # exactly what a click does
    assert search_case.window.people_page.current_detail.person.id == search_case.beta.id
    assert search_case.window.people_page.people_list.currentItem().data(ROLE) == search_case.beta.id
    assert search_case.errors == []


def test_clicking_alias_result_opens_its_person(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("ALIAS-ONE")
    panel.refresh()
    panel.result_list.setCurrentRow(0)
    panel.open_item(panel.result_list.item(0))
    assert search_case.window.people_page.current_detail.person.id == search_case.alpha.id
    assert search_case.errors == []


def test_clicking_event_result_opens_the_events_page(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("Planned trip")
    panel.refresh()
    result = panel.results[0]
    assert result.entity_type == "EVENT"
    panel.activate_result(result)
    assert search_case.window.pages.currentIndex() == 2
    assert search_case.window.events_page.current_detail.event.id == search_case.future_event.id
    assert search_case.window.events_page.detail_view.fields["title"].text() == "Planned trip"
    assert search_case.errors == []


def test_clicking_evidence_result_opens_its_event(search_case):
    panel = search_case.window.people_page.search_panel
    panel.input.setText("chat notes")
    panel.refresh()
    result = next(result for result in panel.results if result.entity_type == "EVIDENCE")
    panel.activate_result(result)
    assert search_case.window.pages.currentIndex() == 2
    events = search_case.window.events_page
    assert events.current_detail.event.id == search_case.event.id
    assert events.evidence_panel.evidence_list.rowCount() == 1
    assert search_case.errors == []


def test_people_page_search_routes_events_through_the_shell(search_case):
    """The People page owns Person/Alias jumps and forwards EVENT to the shell."""
    people_page = search_case.window.people_page
    people_page.search_panel.input.setText("Chat evening")
    people_page.search_panel.refresh()
    result = people_page.search_panel.results[0]
    assert result.entity_type == "EVENT"
    people_page.open_search_result(result)
    assert search_case.window.pages.currentIndex() == 2
    assert search_case.window.events_page.current_detail.event.id == search_case.event.id
    assert search_case.errors == []


def test_rebuild_index_button_reindexes_and_reports(search_case):
    panel = search_case.window.people_page.search_panel
    assert panel.rebuild_button.isEnabled()
    panel.rebuild_button.click()
    assert "搜索索引已重建" in panel.status.text()
    panel.input.setText("ALPHA")
    assert "PERSON" in {result.entity_type for result in panel.refresh()}
    assert search_case.errors == []


def test_include_deleted_widens_the_search(search_case):
    panel = search_case.window.people_page.search_panel
    search_case.services.events.soft_delete_event(search_case.ws, search_case.event.id)
    panel.input.setText("Chat evening")
    assert panel.refresh() == []
    panel.include_deleted.setChecked(True)
    results = panel.refresh()
    assert [result.entity_type for result in results] == ["EVENT"]
    assert results[0].metadata["deleted"] is True
    assert "已删除" in panel.result_list.item(0).text()
    panel.include_deleted.setChecked(False)
    assert panel.refresh() == []
    assert search_case.errors == []


def test_missing_search_service_is_explained_not_crashed(qt_app):
    panel = SearchPanel(SimpleNamespace(search=None, initial_workspace=None), None)
    assert panel.refresh() == []
    assert "尚未装配" in panel.status.text()
    assert not panel.input.isEnabled()
    assert panel.rebuild_index() is None
    panel.input.setText("anything")
    panel.refresh()
    assert panel.result_list.count() == 0
    assert "尚未装配" in panel.status.text()
    panel.close()


# ---------------------------------------------------------------- Timeline UI

def test_person_timeline_shows_events_and_relationship_changes(search_case):
    services, ws = search_case.services, search_case.ws
    services.relationships.create(ws, search_case.alpha.id, search_case.beta.id, "Friend")
    panel = search_case.window.people_page.full_timeline
    assert panel.mode == MODE_PERSON
    panel.set_person(ws, search_case.alpha.id)
    rows = [panel.table.item(row, 0).data(ROLE) for row in range(panel.table.rowCount())]
    assert {row.item.kind for row in rows} == {"EVENT", "RELATIONSHIP_CHANGE"}
    assert {row.item.title for row in rows} >= {"Chat evening", "Planned trip", "Friend 关系状态变更"}
    index = next(index for index, row in enumerate(rows) if row.item.kind == "RELATIONSHIP_CHANGE")
    assert panel.table.item(index, 1).text() == "关系变更"
    assert panel.table.item(index, 5).text() == "ACTIVE · 正常"
    assert "ALPHA" in panel.table.item(index, 4).text()
    assert search_case.errors == []


def test_person_timeline_keeps_future_events_visible(search_case):
    panel = search_case.window.people_page.full_timeline
    panel.set_person(search_case.ws, search_case.alpha.id)
    rows = [panel.table.item(row, 0).data(ROLE) for row in range(panel.table.rowCount())]
    future_row = next(row for row in rows if row.item.entity_id == search_case.future_event.id)
    # The UI adds no "now" filter: the planned Event is listed, and it is first.
    assert future_row.item.timestamp == search_case.future_event.started_at
    assert rows.index(future_row) == 0
    assert panel.table.item(0, 2).text() == "Planned trip"
    assert search_case.errors == []


def test_person_timeline_clears_when_nobody_is_selected(search_case):
    panel = search_case.window.people_page.full_timeline
    unrelated = search_case.services.people.create(search_case.ws, "OUTSIDER")
    panel.set_person(search_case.ws, unrelated.id)
    assert panel.table.rowCount() == 0
    assert "0 条" in panel.status.text()
    panel.set_person(None, None)
    assert panel.person_id is None
    assert panel.table.rowCount() == 0
    assert search_case.errors == []


def test_workspace_timeline_mode_shows_every_person_event(search_case):
    services, ws = search_case.services, search_case.ws
    panel = search_case.window.people_page.full_timeline
    panel.set_person(ws, search_case.alpha.id)
    personal = panel.table.rowCount()
    outsider = services.people.create(ws, "OUTSIDER")
    services.events.create_event(ws, "Other talk", "Call", record_kind="FACT",
                                 started_at=datetime.now(timezone.utc) - timedelta(days=3),
                                 participants=(outsider.id,))
    panel.set_person(ws, search_case.alpha.id)
    assert panel.table.rowCount() == personal          # the other Event is not this person's
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(MODE_WORKSPACE))
    assert panel.mode == MODE_WORKSPACE
    assert panel.table.rowCount() == personal + 1
    # Ordering is the service's (timestamp DESC): the planned trip is still first and
    # the new Event is placed by its own timestamp, not by insertion order.
    titles = [panel.table.item(row, 2).text() for row in range(panel.table.rowCount())]
    assert titles == ["Planned trip", "Chat evening", "Other talk"]
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(MODE_PERSON))
    assert panel.mode == MODE_PERSON
    assert panel.table.rowCount() == personal
    assert search_case.errors == []


def test_clicking_timeline_event_opens_the_event_detail(search_case):
    panel = search_case.window.people_page.full_timeline
    panel.set_person(search_case.ws, search_case.alpha.id)
    rows = [panel.table.item(row, 0).data(ROLE) for row in range(panel.table.rowCount())]
    index = next(index for index, row in enumerate(rows) if row.item.entity_id == search_case.event.id)
    panel.table.selectRow(index)
    dialog = panel.open_selected()
    try:
        assert dialog is not None
        assert dialog.controller.workspace_id == search_case.ws
        assert dialog.controller.current_detail.event.id == search_case.event.id
        assert dialog.detail_view.fields["title"].text() == "Chat evening"
        # The single-Event window keeps its Evidence panel, exactly like the Events page.
        assert dialog.controller.isAncestorOf(dialog.controller.evidence_panel)
        assert dialog.controller.evidence_panel.evidence_list.rowCount() == 1
    finally:
        dialog.close()
        search_case.qt_app.processEvents()
    assert panel._dialogs == []
    assert search_case.errors == []


def test_clicking_timeline_relationship_change_opens_relationship_detail(search_case):
    relationship = search_case.services.relationships.create(
        search_case.ws, search_case.alpha.id, search_case.beta.id, "Friend")
    panel = search_case.window.people_page.full_timeline
    panel.set_person(search_case.ws, search_case.alpha.id)
    rows = [panel.table.item(row, 0).data(ROLE) for row in range(panel.table.rowCount())]
    index = next(index for index, row in enumerate(rows) if row.item.kind == "RELATIONSHIP_CHANGE")
    panel.table.selectRow(index)
    dialog = panel.open_selected()
    try:
        assert isinstance(dialog, RelationshipDetailDialog)
        assert dialog.relationship_id == relationship.id
        assert dialog.fields["relationship_type"].text() == "Friend"
        assert dialog.fields["current_state"].text() == "ACTIVE · 正常"
        assert dialog.history_table.rowCount() == 1
    finally:
        dialog.close()
        search_case.qt_app.processEvents()
    assert panel._dialogs == []
    assert search_case.errors == []


def test_full_timeline_coexists_with_the_historic_event_timeline(search_case):
    services, ws = search_case.services, search_case.ws
    people_page = search_case.window.people_page
    panel = people_page.full_timeline
    assert panel is not people_page.event_timeline
    assert people_page.event_timeline.table.objectName() == "personTimeline"
    services.relationships.create(ws, search_case.alpha.id, search_case.beta.id, "Friend")
    people_page.refresh_people(search_case.alpha.id)
    # The complete Timeline carries the relationship change; the Event-only one does not.
    assert panel.table.rowCount() == people_page.event_timeline.table.rowCount() + 1
    assert {panel.table.item(row, 1).text() for row in range(panel.table.rowCount())} == {"事件", "关系变更"}
    assert people_page.event_timeline.table.horizontalHeaderItem(4).text() == "简述"
    services.events.soft_delete_event(ws, search_case.event.id)
    panel.include_deleted.setChecked(True)
    assert panel.table.rowCount() == people_page.event_timeline.table.rowCount() + 1
    assert search_case.errors == []


def test_timeline_rows_carry_domain_values_only(search_case):
    panel = search_case.window.people_page.full_timeline
    panel.set_person(search_case.ws, search_case.alpha.id)
    for row in range(panel.table.rowCount()):
        payload = panel.table.item(row, 0).data(ROLE)
        assert isinstance(payload, TimelineRow)
        assert payload.item.kind in {"EVENT", "RELATIONSHIP_CHANGE"}
        assert payload.item.person_ids
        if payload.item.kind == "EVENT":
            assert payload.event is not None and payload.event.event.id == payload.item.entity_id
    assert panel.table.item(0, 3).toolTip().startswith("<qt>")
    assert search_case.errors == []
