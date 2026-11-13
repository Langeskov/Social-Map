from datetime import date
from pathlib import Path
from types import SimpleNamespace
import hashlib
import logging
import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QMessageBox
from app.config.settings import AppConfig
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui.people_page import ROLE
from app.ui.person_dialog import PersonDialog, AliasDialog
from app.ui import people_page as module

@pytest.fixture
def ui_case(tmp_path, qt_app, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    window = MainWindow(config, services)
    window.show()
    window.navigation.setCurrentRow(1)
    qt_app.processEvents()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    image = tmp_path / "fixture.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (20, 12), "red").save(image)
    Image.new("RGB", (30, 16), "blue").save(second)
    page = window.pages.widget(1)
    yield SimpleNamespace(config=config, runtime=runtime, services=services, window=window,
                          page=page, image=image, second=second, errors=errors, qt_app=qt_app)
    window.close()
    qt_app.processEvents()
    runtime.close()


def stub_person_dialog(monkeypatch, values, avatar=None):
    def dialog(*args, **kwargs):
        return SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                               values=lambda: dict(values), avatar_path=avatar)
    monkeypatch.setattr(module, "PersonDialog", dialog)


def test_production_people_page_empty_and_default_workspace(ui_case):
    c = ui_case
    # Phase 7 added the Graph page: six pages, People still at index 1.
    assert c.window.pages.count() == 6
    assert [c.window.pages.widget(index).objectName() for index in range(6)] == [
        "dashboard", "people", "events", "graph", "analysis", "settings"]
    assert c.page.objectName() == "people"
    assert c.page.workspace_combo.currentText() == "Default Workspace"
    assert c.page.people_list.count() == 0
    assert not c.page.edit_button.isEnabled()
    assert c.services.workspaces.ensure_default().id == c.page.workspace_id
    assert c.errors == []


def test_create_edit_alias_search_and_archive_ui(ui_case, monkeypatch):
    c, page = ui_case, ui_case.page
    stub_person_dialog(monkeypatch, dict(display_name="Person", real_name="Real", gender="Other",
                                        birthday=date(2000, 1, 2), notes="private"), c.image)
    page.create_person()
    assert page.people_list.count() == 1
    person_id = page.current_detail.person.id
    assert page.current_detail.person.birthday == date(2000, 1, 2)
    assert len(page.current_detail.images) == 1
    assert not page.avatar.pixmap().isNull()
    stub_person_dialog(monkeypatch, dict(display_name="Edited", occupation="Work", notes="new"))
    page.edit_person()
    assert page.current_detail.person.display_name == "Edited"
    monkeypatch.setattr(module, "AliasDialog", lambda *args: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted,
        values=lambda: dict(alias="online%id", alias_type="ONLINE_ID", note="note")))
    page.add_alias()
    assert page.alias_list.count() == 1
    page.search.setText("online%id")
    page.refresh_people()
    assert page.people_list.count() == 1
    page.alias_list.setCurrentRow(0)
    page.remove_alias()
    assert page.people_list.count() == 0  # Alias was the search match.
    page.search.clear()
    page.refresh_people()
    page.archive_person()
    assert page.people_list.count() == 0
    with pytest.raises(NotFoundError):
        c.services.people.get(page.workspace_id, person_id)
    assert c.errors == []


def test_workspace_switch_clears_old_selection(ui_case, monkeypatch):
    c, page = ui_case, ui_case.page
    old_scope = page.workspace_id
    c.services.people.create(old_scope, "Only in A")
    page.refresh_people()
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *args: ("B", True))
    page.create_workspace()
    new_scope = page.workspace_id
    assert old_scope != new_scope
    assert page.people_list.count() == 0
    assert page.current_detail is None
    assert not page.edit_button.isEnabled()
    index = page.workspace_combo.findData(str(old_scope))
    page.workspace_combo.setCurrentIndex(index)
    assert page.people_list.count() == 1
    assert page.current_detail.person.display_name == "Only in A"
    assert c.errors == []


def test_gallery_shared_media_reference_removal_and_original_zoom(ui_case, monkeypatch):
    c, page = ui_case, ui_case.page
    first = c.services.people.create(page.workspace_id, "A")
    second = c.services.people.create(page.workspace_id, "B")
    page.refresh_people(first.id)
    monkeypatch.setattr(module.QFileDialog, "getOpenFileNames", lambda *args: ([str(c.image), str(c.second)], ""))
    page.choose_images("GALLERY")
    assert len(page.current_detail.images) == 2
    first_media = page.current_detail.images[0].media
    c.services.people.add_gallery(page.workspace_id, second.id, first_media.id)
    page.image_list.setCurrentRow(0)
    page.promote_image()
    assert sum(image.link.role == "AVATAR" for image in page.current_detail.images) == 1
    gallery_index = next(index for index in range(page.image_list.count())
                         if page.image_list.item(index).data(ROLE).link.role == "GALLERY")
    page.image_list.setCurrentRow(gallery_index)
    selected = page.image_list.currentItem().data(ROLE)
    path = c.services.media.original_path(page.workspace_id, selected.media.id)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    viewer = page.open_image()
    assert viewer is not None and viewer.isVisible()
    viewer.view.resetTransform()
    viewer.view.zoom(1.2)
    assert viewer.view.transform().m11() > 1
    viewer.view.zoom(1 / 1.2)
    assert viewer.view.transform().m11() == pytest.approx(1)
    viewer.close()
    page.remove_image()
    assert path.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert c.services.people.detail(page.workspace_id, second.id).images
    assert c.errors == []


def test_dialog_fields_and_birthday_validation(qt_app):
    dialog = PersonDialog()
    dialog.fields["display_name"].setText("Name")
    dialog.fields["birthday"].setText("2000-01-31")
    assert dialog.values()["birthday"] == date(2000, 1, 31)
    dialog.fields["birthday"].setText("not-a-date")
    with pytest.raises(ValueError):
        dialog.values()
    alias = AliasDialog()
    alias.alias.setText("Alias")
    alias.kind.setEditText("CUSTOM")
    assert alias.values()["alias_type"] == "CUSTOM"
    dialog.close()
    alias.close()


def test_ui_errors_log_only_class(ui_case, caplog):
    private = "private-notes-should-never-be-logged"
    def fail():
        raise DomainError(private)
    with caplog.at_level(logging.WARNING, logger="app.ui.people_page"):
        ui_case.page.run_operation(fail)
    assert "DomainError" in caplog.text
    assert private not in caplog.text
    assert ui_case.errors == [private]
