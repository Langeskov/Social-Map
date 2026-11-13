from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import logging
import pytest
from PySide6.QtWidgets import QDialog, QMessageBox
from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui import relationship_panel as module

@pytest.fixture
def relationship_ui(tmp_path, qt_app, monkeypatch):
    config = AppConfig(data_dir=tmp_path)
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    ws = services.initial_workspace.id
    a = services.people.create(ws, "A")
    b = services.people.create(ws, "B")
    window = MainWindow(config, services)
    window.show()
    window.navigation.setCurrentRow(1)
    page = window.pages.widget(1)
    page.refresh_people(a.id)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    qt_app.processEvents()
    yield SimpleNamespace(config=config, runtime=runtime, services=services, a=a, b=b,
                          ws=ws, window=window, page=page, panel=page.relationship_panel, errors=errors)
    window.close()
    qt_app.processEvents()
    runtime.close()


def add_values(other, kind="Friend", orientation="UNDIRECTED"):
    return dict(other_id=other, relationship_type=kind, orientation=orientation,
                initial_state="ACTIVE", effective_at=None, note=None)


def test_add_dialog_normalizes_and_identity_not_editable(relationship_ui, monkeypatch):
    c = relationship_ui
    monkeypatch.setattr(module, "RelationshipDialog", lambda *args: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted, values=lambda: add_values(c.b.id)))
    c.panel.add_relationship()
    assert c.panel.table.rowCount() == 1
    selected = c.panel.selected()
    assert selected.relationship.relationship_type == "Friend"
    assert selected.relationship.person_a_id.int < selected.relationship.person_b_id.int
    assert selected.current_state == "ACTIVE"
    assert not hasattr(c.panel, "edit_button")
    assert c.panel.table.editTriggers() == module.QTableWidget.EditTrigger.NoEditTriggers
    assert not hasattr(c.services.relationships, "update")
    assert c.errors == []


def test_directed_orientation_and_multiple_types_display(relationship_ui):
    c = relationship_ui
    outgoing = c.panel.add_from_values(add_values(c.b.id, "Colleague", "OUTGOING"))
    incoming = c.panel.add_from_values(add_values(c.b.id, "Colleague", "INCOMING"))
    c.panel.add_from_values(add_values(c.b.id, "Friend"))
    assert outgoing.person_a_id == c.a.id and incoming.person_a_id == c.b.id
    assert c.panel.table.rowCount() == 3
    assert {c.panel.table.item(row, 2).text() for row in range(3)} == {"无向", "本人 → 对方", "对方 → 本人"}
    c.page.refresh_people(c.b.id)
    assert c.panel.table.rowCount() == 3
    assert c.errors == []


def test_state_history_end_reactivate_and_deleted_restore(relationship_ui, monkeypatch):
    c = relationship_ui
    entity = c.panel.add_from_values(add_values(c.b.id))
    for state in ["PAUSED", "ACTIVE", "ENDED"]:
        monkeypatch.setattr(module, "StateDialog", lambda *args, state=state: SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted,
            values=lambda: dict(state=state, effective_at=None, note="state note")))
        c.panel.change_state()
        assert c.panel.selected().current_state == state
    assert c.services.relationships.get(c.ws, entity.id).deleted_at is None
    history_dialog = c.panel.show_history()
    assert history_dialog.isVisible()
    assert history_dialog.table.rowCount() == 4
    assert history_dialog.table.item(3, 1).text() == "ENDED"
    history_dialog.close()
    c.panel.delete_relationship()
    assert c.panel.table.rowCount() == 0
    c.panel.include_deleted.setChecked(True)
    assert c.panel.table.rowCount() == 1
    assert not c.panel.state_button.isEnabled()
    assert c.panel.restore_button.isEnabled()
    c.panel.restore_relationship()
    assert c.panel.selected().relationship.id == entity.id
    assert c.panel.selected().current_state == "ENDED"
    assert len(c.services.relationships.history(c.ws, entity.id)) == 4
    c.services.relationships.add_history(c.ws, entity.id, "ACTIVE")
    c.panel.refresh()
    assert c.panel.selected().current_state == "ACTIVE"
    assert len(c.services.relationships.history(c.ws, entity.id)) == 5
    assert c.errors == []


def test_archived_endpoint_history_visible_but_not_restorable(relationship_ui):
    c = relationship_ui
    entity = c.panel.add_from_values(add_values(c.b.id))
    c.services.relationships.soft_delete(c.ws, entity.id)
    c.services.people.soft_delete(c.ws, c.b.id)
    c.panel.refresh()
    assert c.panel.table.rowCount() == 0
    c.panel.include_deleted.setChecked(True)
    assert c.panel.table.rowCount() == 1
    assert not c.panel.state_button.isEnabled()
    assert not c.panel.restore_button.isEnabled()
    dialog = c.panel.show_history()
    assert dialog.table.rowCount() == 1
    dialog.close()
    assert c.errors == []


def test_workspace_switch_and_person_clear_do_not_leak_relationships(relationship_ui):
    c = relationship_ui
    c.panel.add_from_values(add_values(c.b.id))
    other = c.page.create_workspace_named("Other")
    assert c.panel.person_id is None
    assert c.panel.table.rowCount() == 0
    assert not c.panel.add_button.isEnabled()
    person = c.services.people.create(other.id, "Other person")
    c.page.refresh_people(person.id)
    assert c.panel.workspace_id == other.id and c.panel.table.rowCount() == 0
    assert c.errors == []


@pytest.mark.parametrize("value", ["2026-10-03T18:00:00", "2026-10-03", "wrong"])
def test_effective_time_naive_rejected(value):
    with pytest.raises(DomainError):
        module.parse_effective(value)


def test_time_and_dialog_type_validation(qt_app):
    assert module.parse_effective("2026-10-03T18:00:00+08:00") == datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
    assert module.parse_effective("") is None
    dialog = module.RelationshipDialog([])
    with pytest.raises(DomainError):
        dialog.values()
    dialog.close()


def test_future_state_not_displayed_as_current(relationship_ui):
    c = relationship_ui
    entity = c.panel.add_from_values(add_values(c.b.id))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    c.services.relationships.add_history(c.ws, entity.id, "ENDED", effective_at=future)
    c.panel.refresh()
    assert c.panel.selected().current_state == "ACTIVE"
    dialog = c.panel.show_history()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(1, 1).text() == "ENDED"
    dialog.close()
    assert c.errors == []


def test_ui_error_logging_has_no_private_history_note(relationship_ui, caplog):
    private = "private-history-note-never-log"
    def fail():
        raise DomainError(private)
    with caplog.at_level(logging.WARNING, logger="app.ui.relationship_panel"):
        relationship_ui.panel.run_operation(fail)
    assert "DomainError" in caplog.text and private not in caplog.text
    assert relationship_ui.errors == [private]
