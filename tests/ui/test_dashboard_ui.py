"""Phase 7 Dashboard: real DashboardService values and real navigation targets.

The page must show recorded facts only: six counts and four bounded recent lists,
each row jumping to the detail that owns it. No strength/affinity/danger figure and
no Analysis placeholder is allowed to appear.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.event_detail import ROLE
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import RelationshipDetailDialog

FACT_NAMES = ["ALPHA", "BETA", "GAMMA"]


@pytest.fixture
def dashboard_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "dashboard-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        people = {name: services.people.create(ws, name) for name in FACT_NAMES}
        friend = services.relationships.create(ws, people["ALPHA"].id, people["BETA"].id, "Friend")
        now = datetime.now(timezone.utc)
        recent_event = services.events.create_event(
            ws, title="Recent call", event_type="Call", record_kind="FACT",
            started_at=now - timedelta(days=1), ended_at=None, occurred_on=None, location=None,
            description="recent", participants=(people["ALPHA"].id,))
        services.events.create_event(
            ws, title="Old meeting", event_type="Meeting", record_kind="OBSERVATION",
            started_at=now - timedelta(days=45), ended_at=None, occurred_on=None, location=None,
            description="old", participants=(people["BETA"].id,))
        recent_evidence = services.evidence.create_evidence(
            ws, recent_event.id, kind="TEXT", title="Chat log", source_type="CHAT",
            content_text="hello", captured_at=now - timedelta(days=1))
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(0)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws, people=people,
                              friend=friend, recent_event=recent_event, recent_evidence=recent_evidence,
                              window=window, page=window.pages.widget(0), errors=errors, qt_app=qt_app,
                              now=now)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def payloads(widget):
    return [widget.item(row).data(ROLE) for row in range(widget.count())]


def test_dashboard_shows_real_counts_and_recent_lists(dashboard_ui):
    c = dashboard_ui
    page = c.page
    assert page.objectName() == "dashboard"
    assert page.workspace_id == c.ws
    counts = {key: label.text() for key, label in page.count_labels.items()}
    assert counts == {"people": "3", "active_relationships": "1", "events_7d": "1", "events_30d": "1",
                      "evidence_7d": "1", "evidence_30d": "1"}
    snapshot = page.snapshot
    assert snapshot is not None
    assert (snapshot.counts.people, snapshot.counts.events_30d) == (3, 1)
    assert [person.display_name for person in snapshot.recent_people] == list(reversed(FACT_NAMES))
    assert [event.title for event in payloads(page.dashboardRecentEvents)] == ["Recent call", "Old meeting"]
    assert payloads(page.dashboardRecentChanges)[0].relationship.id == c.friend.id
    assert payloads(page.dashboardRecentEvidence) == [c.recent_evidence]
    # The lists are real rows, not described rows: each item belongs to a Domain value.
    assert page.dashboardRecentPeople.item(0).text().startswith("GAMMA")
    assert page.dashboardRecentEvents.item(0).text().startswith("Recent call")
    assert c.errors == []


def test_dashboard_never_shows_inference_figures(dashboard_ui):
    c = dashboard_ui
    forbidden = ("关系强度", "好感", "危险", "评分", "score", "affinity", "推断", "性格", "AI 分析")
    widget = c.page
    texts = [widget.status.text()] + [label.text() for label in widget.count_labels.values()]
    for name in ("dashboardRecentPeople", "dashboardRecentEvents", "dashboardRecentChanges",
                 "dashboardRecentEvidence"):
        listing = getattr(widget, name)
        texts.append(listing.objectName())
        texts += [listing.item(row).text() for row in range(listing.count())]
    texts += [child.text() for child in widget.findChildren(type(widget.status))]
    for text in texts:
        assert not any(word in text for word in forbidden)
    assert c.page.status.textFormat() == Qt.TextFormat.PlainText
    assert c.errors == []


def test_click_recent_person_selects_it_on_the_people_page(dashboard_ui):
    c = dashboard_ui
    page = c.page
    target = payloads(page.dashboardRecentPeople)[1]
    assert target.display_name == "BETA"
    page.open_entry(None, entry=target)
    assert c.window.pages.currentIndex() == 1
    people = c.window.people_page
    assert people.workspace_id == c.ws
    assert people.current_detail.person.id == c.people["BETA"].id
    assert people.people_list.currentItem().data(ROLE) == c.people["BETA"].id
    assert c.errors == []


def test_click_recent_event_opens_that_event_detail(dashboard_ui):
    c = dashboard_ui
    page = c.page
    page.open_entry(None, entry=payloads(page.dashboardRecentEvents)[0])
    assert c.window.pages.currentIndex() == 2
    events = c.window.events_page
    assert events.current_detail.event.id == c.recent_event.id
    assert events.detail_view.fields["title"].text() == "Recent call"
    assert events.evidence_panel.current_event.id == c.recent_event.id
    assert events.evidence_panel.evidence_list.rowCount() == 1
    assert c.errors == []


def test_click_recent_evidence_opens_its_event_with_evidence(dashboard_ui):
    c = dashboard_ui
    page = c.page
    page.open_entry(None, entry=payloads(page.dashboardRecentEvidence)[0])
    assert c.window.pages.currentIndex() == 2
    events = c.window.events_page
    assert events.current_detail.event.id == c.recent_event.id
    assert events.evidence_panel.evidence_list.item(0, 0).data(ROLE).evidence.id == c.recent_evidence.id
    assert c.errors == []


def test_click_recent_relationship_opens_relationship_detail(dashboard_ui):
    c = dashboard_ui
    page = c.page
    change = payloads(page.dashboardRecentChanges)[0]
    assert change.history.state == "ACTIVE"
    page.open_entry(None, entry=change)
    assert c.window.pages.currentIndex() == 1
    dialog = next((item for item in c.window.people_page._dialogs
                   if isinstance(item, RelationshipDetailDialog)), None)
    try:
        assert dialog is not None
        assert dialog.relationship_id == c.friend.id
        assert dialog.fields["relationship_type"].text() == "Friend"
        assert dialog.fields["current_state"].text() == "ACTIVE · 正常"
        assert dialog.history_table.rowCount() == 1
    finally:
        if dialog is not None:
            dialog.close()
            c.qt_app.processEvents()
    assert c.errors == []


def test_dashboard_counts_follow_deleted_toggle(dashboard_ui):
    c = dashboard_ui
    page = c.page
    c.services.events.soft_delete_event(c.ws, c.recent_event.id)
    page.refresh()
    assert page.count_labels["events_7d"].text() == "0"
    assert payloads(page.dashboardRecentEvents) and all(
        event.deleted_at is None for event in payloads(page.dashboardRecentEvents))
    page.include_deleted.setChecked(True)
    assert page.count_labels["events_7d"].text() == "1"
    assert c.recent_event.id in {event.id for event in payloads(page.dashboardRecentEvents)}
    page.include_deleted.setChecked(False)
    assert page.count_labels["events_7d"].text() == "0"
    assert c.errors == []


def test_dashboard_workspace_switch_is_isolated(dashboard_ui):
    c = dashboard_ui
    page = c.page
    other = c.services.workspaces.create("Other")
    other_person = c.services.people.create(other.id, "OUTSIDER")
    page.activate()
    page.workspace_combo.setCurrentIndex(page.workspace_combo.findData(str(other.id)))
    assert page.workspace_id == other.id
    assert page.count_labels["people"].text() == "1"
    assert [person.display_name for person in payloads(page.dashboardRecentPeople)] == ["OUTSIDER"]
    assert payloads(page.dashboardRecentEvents) == []
    assert payloads(page.dashboardRecentEvidence) == []
    assert page.search_panel.workspace_id == other.id
    assert page.workspace_timeline.workspace_id == other.id
    # The first Workspace is untouched and comes back unchanged.
    page.workspace_combo.setCurrentIndex(page.workspace_combo.findData(str(c.ws)))
    assert page.count_labels["people"].text() == "3"
    assert {person.id for person in payloads(page.dashboardRecentPeople)} == {person.id
                                                                             for person in c.people.values()}
    assert other_person.id not in {person.id for person in payloads(page.dashboardRecentPeople)}
    assert c.errors == []


def test_dashboard_workspace_timeline_shows_events_and_relationship_changes(dashboard_ui):
    c = dashboard_ui
    page = c.page
    timeline = page.workspace_timeline
    assert timeline.workspace_id == c.ws
    kinds = {timeline.table.item(row, 1).text() for row in range(timeline.table.rowCount())}
    assert kinds == {"事件", "关系变更"}
    titles = {timeline.table.item(row, 2).text() for row in range(timeline.table.rowCount())}
    assert {"Recent call", "Old meeting", "Friend 关系状态变更"} <= titles
    assert c.errors == []


def test_dashboard_search_jumps_to_person_and_event(dashboard_ui):
    c = dashboard_ui
    page = c.page
    panel = page.search_panel
    assert panel.workspace_id == c.ws
    panel.input.setText("BETA")
    results = panel.refresh()
    assert [result.entity_type for result in results] == ["PERSON"]
    assert panel.result_list.count() == 1
    panel.result_list.setCurrentRow(0)
    panel.open_item(panel.result_list.item(0))          # what a click on the row does
    assert c.window.pages.currentIndex() == 1
    assert c.window.people_page.current_detail.person.id == c.people["BETA"].id
    page.activate()
    panel.input.setText("Recent")
    results = panel.refresh()
    assert "EVENT" in {result.entity_type for result in results}
    event_result = next(result for result in results if result.entity_type == "EVENT")
    page.open_search_result(event_result)
    assert c.window.pages.currentIndex() == 2
    assert c.window.events_page.current_detail.event.id == c.recent_event.id
    assert c.errors == []


def test_dashboard_search_row_click_opens_the_person(dashboard_ui):
    c = dashboard_ui
    page = c.page
    panel = page.search_panel
    panel.input.setText("ALPHA")
    assert [result.entity_type for result in panel.refresh()] == ["PERSON"]
    panel.result_list.setCurrentRow(0)
    panel.open_item(panel.result_list.item(0))          # what a click on the row does
    assert c.window.pages.currentIndex() == 1
    assert c.window.people_page.current_detail.person.id == c.people["ALPHA"].id
    assert c.errors == []


def test_dashboard_empty_workspace_is_honest(dashboard_ui):
    c = dashboard_ui
    page = c.page
    empty = c.services.workspaces.create("Nothing here")
    page.activate()
    page.workspace_combo.setCurrentIndex(page.workspace_combo.findData(str(empty.id)))
    assert {label.text() for label in page.count_labels.values()} == {"0"}
    for name in ("dashboardRecentPeople", "dashboardRecentEvents", "dashboardRecentChanges",
                 "dashboardRecentEvidence"):
        assert getattr(page, name).count() == 0
    assert page.workspace_timeline.table.rowCount() == 0
    assert "0" in page.status.text()
    assert c.errors == []
