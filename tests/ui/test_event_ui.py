"""Phase 5 UI integration uses the real application services and migrations."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
import logging
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox
from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui.event_dialog import EventDialog, parse_datetime
from app.ui.event_detail import ROLE
from app.ui import events_page as module


@pytest.fixture
def event_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "event-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        a = services.people.create(ws, "Same name")
        b = services.people.create(ws, "Same name")
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(2)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws, a=a, b=b,
                              window=window, page=window.pages.widget(2), people=window.pages.widget(1),
                              errors=errors, qt_app=qt_app)
    finally:
        if window:
            window.close()
            qt_app.processEvents()
        runtime.close()


def values(title="Meeting", participants=(), **changes):
    result = dict(title=title, event_type="Contact", record_kind="FACT", occurred_on=date(2025, 4, 1),
                  started_at=None, ended_at=None, location=None, description=None, participants=participants)
    result.update(changes)
    return result


def stub_dialog(monkeypatch, data, accepted=True):
    monkeypatch.setattr(module, "EventDialog", lambda *args, **kwargs: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected,
        values=lambda: dict(data)))


def test_empty_events_and_timeline(event_ui):
    c = event_ui
    # Phase 7 added the Graph page: six pages, Events still at index 2.
    assert c.window.pages.count() == 6
    assert [c.window.pages.widget(index).objectName() for index in range(6)] == [
        "dashboard", "people", "events", "graph", "analysis", "settings"]
    assert c.page.objectName() == "events"
    assert c.page.event_list.count() == 0
    assert not c.page.edit_button.isEnabled()
    c.people.refresh_people(c.a.id)
    assert c.people.event_timeline.table.rowCount() == 0
    assert not c.people.event_timeline.detail_button.isEnabled()
    assert c.errors == []


def test_create_edit_timeline_detail_and_record_kinds(event_ui, monkeypatch):
    c = event_ui
    c.people.refresh_people(c.a.id)
    stub_dialog(monkeypatch, values(participants=(c.a.id, c.b.id)))
    c.page.create_event()
    assert c.page.event_list.count() == 1
    id = c.page.current_detail.event.id
    assert c.page.detail_view.participants.rowCount() == 2
    assert c.people.event_timeline.table.rowCount() == 1  # signal refreshes hidden People
    dialog = c.people.event_timeline.show_detail()
    assert dialog is not None and dialog.isVisible()
    assert dialog.detail_view.fields["title"].text() == "Meeting"
    dialog.close()
    started = datetime(2025, 4, 2, 10, tzinfo=timezone.utc)
    stub_dialog(monkeypatch, values("Observation", (c.a.id,), record_kind="OBSERVATION", occurred_on=None,
                                   started_at=started, ended_at=started, description="<b>plain private description</b>"))
    c.page.edit_event()
    assert c.page.current_detail.event.id == id
    assert c.page.detail_view.fields["record_kind"].text() == "OBSERVATION"
    assert "+00:00" in c.page.detail_view.fields["time"].text()
    assert c.page.detail_view.description.toPlainText() == "<b>plain private description</b>"
    assert c.people.event_timeline.selected().event.title == "Observation"
    assert c.errors == []


def test_timeline_detail_has_edit_participants_history_delete_restore(event_ui, monkeypatch):
    c = event_ui
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    relationship = c.services.relationships.create(c.ws, c.a.id, c.b.id, "Friend")
    history = c.services.relationships.history(c.ws, relationship.id)[0]
    c.people.refresh_people(c.a.id)
    dialog = c.people.event_timeline.show_detail()
    assert dialog is not None
    controller = dialog.controller
    assert controller.workspace_id == c.ws
    assert controller.current_detail.event.id == event.id
    try:
        stub_dialog(monkeypatch, values("Edited from timeline", (c.a.id,)))
        controller.edit_button.click()
        assert dialog.detail_view.fields["title"].text() == "Edited from timeline"
        assert c.people.event_timeline.selected().event.title == "Edited from timeline"
        monkeypatch.setattr(module, "ParticipantDialog", lambda *args: SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted,
            values=lambda: dict(person_id=c.b.id, role="Witness", note=None)))
        controller.add_participant_button.click()
        table = dialog.detail_view.participants
        row = next(row for row in range(table.rowCount()) if table.item(row, 0).data(ROLE).person.id == c.b.id)
        table.selectRow(row)
        controller.remove_participant_button.click()
        assert table.rowCount() == 1
        monkeypatch.setattr(module, "HistoryAttachDialog", lambda *args: SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted, values=lambda: history.id))
        controller.attach_button.click()
        assert dialog.detail_view.histories.rowCount() == 1
        dialog.detail_view.histories.selectRow(0)
        controller.detach_button.click()
        assert dialog.detail_view.histories.rowCount() == 0
        controller.delete_button.click()
        assert dialog.detail_view.fields["lifecycle"].text() == "已删除"
        assert c.people.event_timeline.table.rowCount() == 0
        assert controller.restore_button.isEnabled()
        controller.restore_button.click()
        assert dialog.detail_view.fields["lifecycle"].text() == "有效记录"
        assert c.people.event_timeline.table.rowCount() == 1
        assert len(c.services.relationships.history(c.ws, relationship.id)) == 1
    finally:
        dialog.close()
    assert c.errors == []


def test_participant_add_remove_uses_uuid_not_name(event_ui, monkeypatch):
    c = event_ui
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    c.page.refresh_events(event.id)
    monkeypatch.setattr(module, "ParticipantDialog", lambda *args: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted,
        values=lambda: dict(person_id=c.b.id, role="Witness", note="participant note")))
    c.page.add_participant()
    assert {view.person.id for view in c.page.current_detail.participants} == {c.a.id, c.b.id}
    table = c.page.detail_view.participants
    row = next(row for row in range(table.rowCount()) if table.item(row, 0).data(ROLE).person.id == c.b.id)
    table.selectRow(row)
    c.page.remove_participant()
    assert [view.person.id for view in c.page.current_detail.participants] == [c.a.id]
    assert c.errors == []


def test_timeline_summary_and_event_list_metadata(event_ui):
    c = event_ui
    description = "<b>private literal markup</b>\n" + "Long description " * 20
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id, c.b.id), description=description))
    c.page.refresh_events(event.id)
    text = c.page.event_list.currentItem().text()
    assert "Contact" in text
    assert "2 参与者" in text
    assert "有效记录" in text
    c.people.refresh_people(c.a.id)
    table = c.people.event_timeline.table
    assert table.horizontalHeaderItem(4).text() == "简述"
    item = table.item(0, 4)
    assert item.text().startswith("<b>private literal markup</b>")
    assert item.text().endswith("…")
    assert len(item.text()) == 121
    assert "&lt;b&gt;private literal markup&lt;/b&gt;" in item.toolTip()
    assert "Long description " * 20 in item.toolTip()
    assert c.errors == []


def test_search_and_timeline_latest_first(event_ui):
    c = event_ui
    old = c.services.events.create_event(c.ws, **values("Older", (c.a.id,), occurred_on=date(2025, 4, 1)))
    latest = c.services.events.create_event(c.ws, **values("Newest", (c.a.id,), occurred_on=date(2025, 4, 3)))
    c.people.refresh_people(c.a.id)
    assert c.people.event_timeline.table.item(0, 0).data(ROLE).event.id == latest.id
    c.page.search.setText("Older")
    c.page.refresh_events()
    assert c.page.event_list.count() == 1
    assert c.page.current_detail.event.id == old.id
    c.page.search.setText("no matching event")
    c.page.refresh_events()
    assert c.page.current_detail is None
    assert not c.page.edit_button.isEnabled()
    assert c.errors == []


def test_delete_restore_and_timeline_include_deleted(event_ui):
    c = event_ui
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    c.people.refresh_people(c.a.id)
    c.page.refresh_events(event.id)
    c.page.delete_event()
    assert c.page.event_list.count() == 0
    assert c.people.event_timeline.table.rowCount() == 0
    c.page.include_deleted.setChecked(True)
    c.people.event_timeline.include_deleted.setChecked(True)
    assert c.page.event_list.count() == c.people.event_timeline.table.rowCount() == 1
    assert not c.page.edit_button.isEnabled()
    assert c.page.restore_button.isEnabled()
    c.page.restore_event()
    assert c.page.current_detail.event.deleted_at is None
    assert c.page.edit_button.isEnabled()
    assert c.errors == []


def test_workspace_and_person_clear_are_isolated(event_ui):
    c = event_ui
    c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    c.people.refresh_people(c.a.id)
    assert c.people.event_timeline.table.rowCount() == 1
    other = c.people.create_workspace_named("Other")
    assert c.people.event_timeline.person_id is None
    assert c.people.event_timeline.table.rowCount() == 0
    c.page.activate()
    c.page.workspace_combo.setCurrentIndex(c.page.workspace_combo.findData(str(other.id)))
    assert c.page.current_detail is None
    assert c.page.event_list.count() == 0
    assert not c.page.edit_button.isEnabled()
    c.window.navigation.setCurrentRow(1)
    assert c.people.event_timeline.table.rowCount() == 0
    assert c.errors == []


def test_page_activation_reloads_external_service_changes(event_ui):
    c = event_ui
    c.window.navigation.setCurrentRow(1)
    c.people.refresh_people(c.a.id)
    c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    c.window.navigation.setCurrentRow(2)
    assert c.page.event_list.count() == 1
    c.window.navigation.setCurrentRow(1)
    assert c.people.event_timeline.table.rowCount() == 1
    assert c.errors == []


def test_archived_participant_display_and_editor_preservation(event_ui):
    c = event_ui
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id, c.b.id)))
    c.services.people.soft_delete(c.ws, c.b.id)
    c.page.refresh_events(event.id)
    table = c.page.detail_view.participants
    assert any("Archived" in table.item(row, 0).text() for row in range(table.rowCount()))
    dialog = EventDialog(c.services.people.list(c.ws), c.page.current_detail)
    try:
        ids = {participant.person_id for participant in dialog.values()["participants"]}
        assert ids == {c.a.id, c.b.id}
        dialog.title.setText("Edited with archived participant")
        c.services.events.update_event(c.ws, event.id, **dialog.values())
        assert {view.person.id for view in c.services.events.detail(c.ws, event.id).participants} == {c.a.id, c.b.id}
        assert any("Archived" in dialog.participants.item(row, 0).text()
                   for row in range(dialog.participants.rowCount()))
    finally:
        dialog.close()
    assert c.errors == []


def test_explicit_history_attach_detach_does_not_create_history(event_ui, monkeypatch):
    c = event_ui
    event = c.services.events.create_event(c.ws, **values(participants=(c.a.id,)))
    relationship = c.services.relationships.create(c.ws, c.a.id, c.b.id, "Friend")
    history = c.services.relationships.history(c.ws, relationship.id)[0]
    c.page.refresh_events(event.id)
    assert c.page.detail_view.histories.rowCount() == 0
    monkeypatch.setattr(module, "HistoryAttachDialog", lambda *args: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted, values=lambda: history.id))
    c.page.attach_history()
    assert c.page.detail_view.histories.rowCount() == 1
    assert len(c.services.relationships.history(c.ws, relationship.id)) == 1
    c.page.detail_view.histories.selectRow(0)
    c.page.detach_history()
    assert c.page.detail_view.histories.rowCount() == 0
    assert len(c.services.relationships.history(c.ws, relationship.id)) == 1
    c.page.delete_event()
    c.page.include_deleted.setChecked(True)
    assert c.page.attach_button.isEnabled()
    assert not c.page.edit_button.isEnabled()
    c.page.attach_history()
    assert c.page.detail_view.histories.rowCount() == 1
    assert c.page.current_detail.event.deleted_at is not None
    assert len(c.services.relationships.history(c.ws, relationship.id)) == 1
    assert c.errors == []


def test_dialog_cancel_and_invalid_time_do_not_write(event_ui, monkeypatch):
    c = event_ui
    stub_dialog(monkeypatch, values(), accepted=False)
    c.page.create_event()
    assert c.page.event_list.count() == 0
    dialog = EventDialog([c.a, c.b])
    try:
        dialog.title.setText("Title")
        dialog.event_type.setText("Type")
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert c.errors
        c.errors.clear()
        dialog.started_at.setText("2025-04-01T10:00:00")
        with pytest.raises(DomainError):
            dialog.values()
        dialog.started_at.setText("2025-04-01T18:00:00+08:00")
        assert dialog.values()["started_at"] == datetime(2025, 4, 1, 10, tzinfo=timezone.utc)
        dialog.occurred_on.setText("2025-04-01")
        with pytest.raises(DomainError):
            dialog.values()
        dialog.started_at.clear()
        assert dialog.values()["occurred_on"] == date(2025, 4, 1)
        assert {dialog.record_kind.itemData(index) for index in range(dialog.record_kind.count())} == {"FACT", "OBSERVATION"}
    finally:
        dialog.close()
    assert c.errors == []


@pytest.mark.parametrize("text", ["wrong", "2025-04-01", "2025-04-01T10:00:00"])
def test_naive_event_times_rejected(text):
    with pytest.raises(DomainError):
        parse_datetime(text)


def test_private_description_not_logged(event_ui, caplog):
    private = "private-description-must-not-be-logged"
    def fail():
        raise DomainError(private)
    with caplog.at_level(logging.WARNING, logger="app.ui.event_detail"):
        event_ui.page.run_operation(fail)
    assert "DomainError" in caplog.text
    assert private not in caplog.text
    assert event_ui.errors == [private]


def test_two_details_stale_detach_cannot_remove_other_event_link(event_ui, monkeypatch):
    c = event_ui
    a_event = c.services.events.create_event(c.ws, **values("A", (c.a.id,)))
    b_event = c.services.events.create_event(c.ws, **values("B", (c.a.id,)))
    rel = c.services.relationships.create(c.ws, c.a.id, c.b.id, "Friend")
    history = c.services.relationships.history(c.ws, rel.id)[0]
    c.services.events.attach_event_to_history(c.ws, history.id, a_event.id)
    c.people.refresh_people(c.a.id)
    timeline = c.people.event_timeline
    def open_for(event_id):
        row = next(row for row in range(timeline.table.rowCount())
                   if timeline.table.item(row, 0).data(ROLE).event.id == event_id)
        timeline.table.selectRow(row)
        return timeline.show_detail()
    a_dialog, b_dialog = open_for(a_event.id), open_for(b_event.id)
    try:
        monkeypatch.setattr(module, "HistoryAttachDialog", lambda *args: SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted, values=lambda: history.id))
        b_dialog.controller.attach_history()
        assert a_dialog.detail_view.histories.rowCount() == 1  # cached A snapshot
        a_dialog.detail_view.histories.selectRow(0)
        a_dialog.controller.detach_history()
        assert len(c.errors) == 1 and "关联已改变" in c.errors[0]
        assert a_dialog.detail_view.histories.rowCount() == 0  # conflict refreshes A
        assert c.services.relationships.history(c.ws, rel.id)[0].event_id == b_event.id
        assert len(c.services.events.detail(c.ws, b_event.id).histories) == 1
        assert len(c.services.relationships.history(c.ws, rel.id)) == 1
    finally:
        a_dialog.close()
        b_dialog.close()

