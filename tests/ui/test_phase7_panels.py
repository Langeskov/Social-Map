"""Phase 7 panels inside People: Person Timeline and the unified search entry."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import MODE_PERSON, MODE_WORKSPACE

NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=45)          # relative to the real clock, not a frozen constant


@pytest.fixture
def panels(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    config = AppConfig(data_dir=tmp_path / "panels-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        person = services.people.create(ws, "面板甲")
        other = services.people.create(ws, "面板乙")
        relationship = services.relationships.create(ws, person.id, other.id, "Friend")
        past = services.events.create_event(ws, "过去活动", "Meeting", "FACT",
                                            occurred_on=date(2026, 4, 1), participants=(person.id,))
        future = services.events.create_event(ws, "未来活动", "Meeting", "FACT",
                                              started_at=FUTURE, participants=(person.id,))
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(1)
        qt_app.processEvents()
        window.people_page.refresh_people(person.id)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws,
                              person=person, other=other, relationship=relationship,
                              past=past, future=future, window=window,
                              page=window.pages.widget(1), qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def row_items(panel):
    """The projection rows the panel rendered, in display order."""
    return [row.item for row in panel._rows]


def test_person_timeline_lists_events_and_relationship_changes(panels):
    c = panels
    timeline = c.page.full_timeline
    timeline.mode = MODE_PERSON
    timeline.set_person(c.ws, c.person.id)
    items = row_items(timeline)
    kinds = {item.kind for item in items}
    assert kinds == {"EVENT", "RELATIONSHIP_CHANGE"}
    event_ids = {item.entity_id for item in items if item.kind == "EVENT"}
    assert event_ids == {c.past.id, c.future.id}
    change = next(item for item in items if item.kind == "RELATIONSHIP_CHANGE")
    assert change.relationship_id == c.relationship.id
    assert set(change.person_ids) == {c.person.id, c.other.id}
    assert c.errors == []


def test_person_timeline_keeps_future_events_visible_and_first(panels):
    c = panels
    timeline = c.page.full_timeline
    timeline.mode = MODE_PERSON
    timeline.set_person(c.ws, c.person.id)
    items = row_items(timeline)
    event_items = [item for item in items if item.kind == "EVENT"]
    identifiers = [item.entity_id for item in event_items]
    assert c.future.id in identifiers and c.past.id in identifiers
    # Newest first: the future event precedes the backdated one, and a future
    # timestamp is never hidden just because it is in the future.
    assert identifiers.index(c.future.id) < identifiers.index(c.past.id)
    future_item = next(item for item in event_items if item.entity_id == c.future.id)
    assert future_item.timestamp > datetime.now(timezone.utc)
    assert [item.timestamp for item in event_items] == sorted(
        (item.timestamp for item in event_items), reverse=True)


def test_event_only_timeline_still_works(panels):
    """The historic Phase 5 widget keeps its contract: events of one person."""
    c = panels
    event_timeline = c.page.event_timeline
    event_timeline.set_person(c.ws, c.person.id)
    assert event_timeline.table.rowCount() == 2
    assert c.errors == []


def test_people_search_entry_finds_records_and_navigates(panels):
    c = panels
    panel = c.page.search_panel
    panel.set_workspace(c.ws)
    panel.input.setText("面板乙")
    results = panel.refresh()
    assert [result.entity_type for result in results] == ["PERSON"]
    panel.open_item(panel.result_list.item(0))
    assert c.window.pages.currentIndex() == 1
    assert c.page.current_detail.person.id == c.other.id
    panel.input.setText("未来活动")
    results = panel.refresh()
    event_result = next(result for result in results if result.entity_type == "EVENT")
    assert event_result.entity_id == c.future.id
    panel.open_item(panel.result_list.item(0))
    # An event hit opens the Events page on that event.
    assert c.window.pages.currentIndex() == 2
    assert c.window.pages.widget(2).current_detail.event.id == c.future.id
    assert c.errors == []


@pytest.mark.parametrize("query", ["*", '"', "AND", "OR NOT", "a-b", "面板 AND 甲", "-", "("])
def test_search_entry_survives_operator_text(panels, query):
    c = panels
    panel = c.page.search_panel
    panel.set_workspace(c.ws)
    panel.input.setText(query)
    results = panel.refresh()
    assert isinstance(results, list)
    assert c.errors == []


def test_timeline_row_opens_event_and_relationship_details(panels, monkeypatch):
    c = panels
    timeline = c.page.full_timeline
    timeline.mode = MODE_PERSON
    timeline.set_person(c.ws, c.person.id)
    items = row_items(timeline)
    event_item = next(item for item in items if item.kind == "EVENT")
    row = next(index for index, item in enumerate(items) if item.entity_id == event_item.entity_id)
    timeline.table.selectRow(row)
    dialog = timeline.open_selected()
    assert dialog is not None
    try:
        assert dialog.detail_view.fields["title"].text() in {"过去活动", "未来活动"}
    finally:
        dialog.close()
    change_item = next(item for item in items if item.kind == "RELATIONSHIP_CHANGE")
    change_row = next(index for index, item in enumerate(items)
                      if item.entity_id == change_item.entity_id)
    timeline.table.selectRow(change_row)
    relationship_dialog = timeline.open_selected()
    assert relationship_dialog is not None
    try:
        assert relationship_dialog.view.relationship.id == c.relationship.id
        assert relationship_dialog.view.relationship.relationship_type == "Friend"
    finally:
        relationship_dialog.close()
    assert c.errors == []


def test_workspace_mode_ignores_person_selection(panels):
    c = panels
    timeline = c.page.full_timeline
    timeline.mode = MODE_WORKSPACE
    timeline.set_person(c.ws, None)
    items = row_items(timeline)
    assert {item.kind for item in items} == {"EVENT", "RELATIONSHIP_CHANGE"}
    assert c.errors == []


def test_timeline_names_an_archived_participant(panels):
    """A recorded participant keeps its name even after the person is archived."""
    c = panels
    c.services.people.soft_delete(c.ws, c.other.id)
    timeline = c.page.full_timeline
    timeline.mode = MODE_PERSON
    timeline.set_person(c.ws, c.person.id)
    column = [timeline.table.item(row, 4).text() for row in range(timeline.table.rowCount())]
    assert any("面板乙" in text for text in column), column
    assert all(str(c.other.id)[:8] not in text for text in column)
    assert c.errors == []
