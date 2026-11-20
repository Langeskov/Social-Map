"""Phase 11.5 Dashboard, global search and Graph information hierarchy.

Contracts:

* the Dashboard is summary + recent records + entry points only: the duplicated search
  box and Workspace Timeline are no longer displayed, the five "recent" lists carry
  their layer identity, and the analysis list is marked DERIVED;
* every row opens the real detail (person / event / relationship / evidence / run);
* unified search lives in the top bar, is reachable with Ctrl+F, is scoped to the
  Current Workspace and never keeps another Workspace's results;
* the Graph has a side filter panel with type, state, archived and deleted.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.domain.entities import AnalysisRun
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.ui.design.widgets import LayerBadge
from app.ui.main_window import ANALYSIS, EVENTS, PEOPLE, MainWindow


@pytest.fixture
def case(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    config = AppConfig(data_dir=tmp_path / "phase11-shell")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        yield SimpleNamespace(services=services, runtime=runtime, window=window, qt_app=qt_app,
                              errors=errors, ws=services.initial_workspace.id, tmp_path=tmp_path)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def seed_run(runtime, workspace_id, person_id, *, status="SUCCEEDED", provider="local"):
    """Record one analysis run without any network call (the ledger is a fact)."""
    now = datetime.now(timezone.utc)
    run = AnalysisRun(workspace_id=workspace_id, target_type="PERSON", target_id=person_id,
                      analysis_type="PERSON_PROFILE", provider_name=provider, model_name="stub-model",
                      scope=json.dumps({"target_type": "PERSON"}), status=status,
                      created_at=now, started_at=now,
                      result="DERIVED 文本" if status == "SUCCEEDED" else None,
                      completed_at=now if status == "SUCCEEDED" else None,
                      duration_ms=12 if status == "SUCCEEDED" else None)
    with transaction(runtime.sessions, write=True) as session:
        sql_analysis_repositories(session).runs.save(run)
    return run


# --------------------------------------------------------------------- Dashboard
def test_dashboard_shows_five_layered_recent_lists(case):
    page = case.window.dashboard_page
    assert page.dashboardRecentPeople.objectName() == "dashboardRecentPeople"
    expected = {"dashboardRecentPeople": "ENTITY", "dashboardRecentEvents": "OCCURRENCE",
                "dashboardRecentChanges": "LINK", "dashboardRecentEvidence": "PROOF",
                "dashboardRecentAnalysis": "INFERENCE"}
    for name, layer in expected.items():
        widget = page.findChild(type(page.dashboardRecentPeople), name)
        assert widget is not None
    headings = [badge for badge in page.findChildren(LayerBadge)]
    assert {badge.layer.key for badge in headings} == {"ENTITY", "OCCURRENCE", "LINK", "PROOF", "INFERENCE"}


def test_dashboard_no_longer_embeds_search_or_the_full_timeline(case):
    page = case.window.dashboard_page
    assert page.search_panel.isVisible() is False
    assert page.workspace_timeline.isVisible() is False


def test_dashboard_lists_recent_analysis_as_derived(case):
    services, page = case.services, case.window.dashboard_page
    person = services.people.create(case.ws, "被分析者")
    run = seed_run(case.runtime, case.ws, person.id)
    page.refresh()
    assert page.dashboardRecentAnalysis.count() == 1
    text = page.dashboardRecentAnalysis.item(0).text()
    assert "DERIVED" in text and "PERSON_PROFILE" in text and run.status in text
    assert "分析 1" in page.status.text()


def test_dashboard_analysis_row_opens_the_analysis_page(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "跳转分析")
    run = seed_run(case.runtime, case.ws, person.id)
    page = window.dashboard_page
    page.refresh()
    page.dashboardRecentAnalysis.setCurrentRow(0)
    page.open_selected()
    case.qt_app.processEvents()
    assert window.pages.currentIndex() == ANALYSIS
    assert window.analysis_page.current_view is not None
    assert window.analysis_page.current_view.run.id == run.id


def test_dashboard_rows_still_open_their_own_detail(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "最近人物")
    event = services.events.create_event(case.ws, "最近事件", "Meeting",
                                         started_at=datetime.now(timezone.utc) - timedelta(hours=1))
    page = window.dashboard_page
    page.refresh()
    page.dashboardRecentPeople.setCurrentRow(0)
    page.open_selected()
    case.qt_app.processEvents()
    assert window.pages.currentIndex() == PEOPLE
    assert window.people_page.current_detail.person.id == person.id
    page.dashboardRecentEvents.setCurrentRow(0)
    page.open_entry(None, entry=page.dashboardRecentEvents.item(0).data(0x0100))
    case.qt_app.processEvents()
    assert window.pages.currentIndex() == EVENTS
    assert window.events_page.current_detail.event.id == event.id


# ---------------------------------------------------------------- global search
def test_global_search_lives_in_the_top_bar(case):
    window = case.window
    assert window.global_search is not None
    assert window.global_search.parent() is window.top_bar
    assert window.global_search.isVisible()
    assert window.global_search.objectName() == "globalSearch"


def test_ctrl_f_focuses_the_global_search(case):
    window = case.window
    window.navigation.setCurrentRow(EVENTS)
    case.qt_app.processEvents()
    target = window.focus_search()
    assert target is window.global_search.input
    assert window.find_shortcut is not None
    assert "F" in window.find_shortcut.key().toString()


def test_global_search_is_scoped_to_the_current_workspace(case):
    services, window = case.services, case.window
    services.people.create(case.ws, "全局命中")
    panel = window.global_search
    panel.set_workspace(case.ws)
    panel.input.setText("全局命中")
    results = panel.refresh()
    assert [result.title for result in results] == ["全局命中"]
    assert all(result.workspace_id == case.ws for result in results)
    other = services.workspaces.create("搜索隔离")
    window.workspace_context.refresh()
    window.top_bar.set_workspaces(window.workspace_context.workspaces, window.workspace_context.workspace_id)
    window.top_bar.workspace_combo.setCurrentIndex(
        window.top_bar.workspace_combo.findData(str(other.id)))
    case.qt_app.processEvents()
    assert panel.workspace_id == other.id
    assert panel.result_list.count() == 0
    assert panel.query_text() == ""


def test_global_search_result_opens_the_owning_detail(case):
    services, window = case.services, case.window
    person = services.people.create(case.ws, "搜索跳转人物")
    panel = window.global_search
    panel.input.setText("搜索跳转人物")
    results = panel.refresh()
    assert results
    panel.result_list.setCurrentRow(0)
    assert panel.open_selected() == results[0]
    case.qt_app.processEvents()
    assert window.pages.currentIndex() == PEOPLE
    assert window.people_page.current_detail.person.id == person.id


def test_global_search_can_jump_to_another_workspace(case):
    """A hit always carries its Workspace, so the jump switches the context first."""
    services, window = case.services, case.window
    other = services.workspaces.create("另一个库")
    person = services.people.create(other.id, "别库人物")
    panel = window.global_search
    panel.set_workspace(other.id)
    panel.input.setText("别库人物")
    results = panel.refresh()
    assert results
    panel.result_list.setCurrentRow(0)
    panel.open_selected()
    case.qt_app.processEvents()
    assert window.workspace_context.workspace_id == other.id
    assert window.people_page.current_detail.person.id == person.id


# ------------------------------------------------------------------------ Graph
def test_graph_has_side_filters(case, qt_app):
    window = case.window
    window.navigation.setCurrentRow(3)
    qt_app.processEvents()
    page = window.graph_page
    assert page.filters_panel.isVisible()
    assert page.type_filter.parent() is page.filters_panel
    assert page.show_archived.parent() is page.filters_panel
    assert page.show_deleted.parent() is page.filters_panel
    options = [page.state_filter.itemData(index) for index in range(page.state_filter.count())]
    assert options == [None, "ACTIVE", "PAUSED", "ENDED"]


def test_graph_state_filter_filters_without_extra_queries(case, qt_app):
    services, window = case.services, case.window
    a = services.people.create(case.ws, "图A")
    b = services.people.create(case.ws, "图B")
    friend = services.relationships.create(case.ws, a.id, b.id, "Friend")
    services.relationships.add_history(case.ws, friend.id, "PAUSED")
    page = window.graph_page
    page.load_workspaces()
    page.refresh()
    assert len(page.graph_state()["edges"]) == 1
    page.state_filter.setCurrentIndex(page.state_filter.findData("ACTIVE"))
    assert page.graph_state()["edges"] == []
    page.state_filter.setCurrentIndex(page.state_filter.findData("PAUSED"))
    assert len(page.graph_state()["edges"]) == 1
    page.state_filter.setCurrentIndex(0)
    assert len(page.graph_state()["edges"]) == 1


def test_graph_edge_colours_come_from_tokens(case, qt_app):
    from app.ui.design.tokens import COLORS
    from app.ui.graph_canvas import DELETED_COLOUR, DIRECTED_COLOUR, STATE_PENS, UNKNOWN_STATE_PEN
    allowed = set(COLORS.values())
    assert DIRECTED_COLOUR in allowed
    assert DELETED_COLOUR in allowed
    assert UNKNOWN_STATE_PEN[0] in allowed
    for colour, _style in STATE_PENS.values():
        assert colour in allowed
