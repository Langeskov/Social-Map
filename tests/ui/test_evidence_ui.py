"""Phase 6 Evidence UI integration uses the real application services and migrations."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import logging
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QGroupBox, QLineEdit, QMessageBox, QTextEdit
from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui.event_detail import ROLE
from app.ui.evidence_dialog import (EvidenceCreateDialog, EvidenceMetadataDialog, MediaAttachDialog,
    parse_captured)
from app.ui import evidence_dialog as dialog_module
from app.ui import evidence_panel as module


@pytest.fixture
def evidence_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "evidence-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        person = services.people.create(ws, "Subject")
        event = services.events.create_event(ws, title="Chat", event_type="Contact", record_kind="FACT",
                                            occurred_on=date(2026, 9, 28), participants=(person.id,))
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(2)
        qt_app.processEvents()
        files = []
        for name, colour in [("chat.png", "green"), ("second.png", "blue"), ("third.png", "black")]:
            path = tmp_path / name
            Image.new("RGB", (24, 16), colour).save(path)
            files.append(path)
        media = [services.media.import_file(ws, path) for path in files]
        page = window.pages.widget(2)
        page.refresh_events(event.id)
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws, person=person,
                              event=event, window=window, page=page, panel=page.evidence_panel,
                              people=window.pages.widget(1), files=files, media=media[0],
                              second_media=media[1], third_media=media[2], errors=errors,
                              qt_app=qt_app, tmp_path=tmp_path)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def stub_dialog(monkeypatch, name, values, accepted=True):
    payload = values
    monkeypatch.setattr(module, name, lambda *args, **kwargs: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected,
        values=lambda: payload))


def make_evidence(c, **changes):
    values = dict(kind="TEXT", title="聊天文本", source_type="CHAT", source_label="WeChat",
                  source_reference="thread-1", captured_at=None, content_text="原始内容",
                  annotation=None, media=())
    values.update(changes)
    evidence = c.services.evidence.create_evidence(c.ws, c.event.id, **values)
    c.panel.refresh(evidence.id)
    return evidence


def row_for(c, title):
    for row in range(c.panel.evidence_list.rowCount()):
        if c.panel.evidence_list.item(row, 0).data(ROLE).evidence.title == title:
            return row
    raise AssertionError(f"Evidence {title!r} is not listed")


def select_row(c, title):
    row = row_for(c, title)
    c.panel.evidence_list.selectRow(row)
    return row


def test_empty_panel_state_and_missing_service(qt_app):
    panel = module.EvidencePanel(SimpleNamespace(evidence=None), None)
    assert panel.objectName() == "evidencePanel"
    assert panel.evidence_list.columnCount() == 6
    assert panel.status.textFormat() == Qt.TextFormat.PlainText
    assert panel.set_event(None) is None
    assert panel.evidence_list.rowCount() == 0
    assert not panel.create_button.isEnabled()
    event = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), deleted_at=None)
    panel.set_event(event)
    assert "尚未装配" in panel.status.text()
    assert panel.evidence_list.rowCount() == 0
    for button in (panel.create_button, panel.edit_button, panel.detail_button, panel.attach_button,
                   panel.detach_button, panel.delete_button, panel.restore_button):
        assert not button.isEnabled()
    assert not panel.include_deleted.isEnabled()
    assert panel.create_evidence() is None
    assert panel.show_detail() is None
    panel.close()


def test_empty_evidence_for_selected_event(evidence_ui):
    c = evidence_ui
    assert c.page.evidence_panel is c.panel
    assert c.panel.isVisible()
    layout = c.page.detail_view.parent().layout()
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    assert widgets.index(c.panel) == widgets.index(c.page.detail_view) + 1
    assert c.panel.current_event.id == c.event.id
    assert c.panel.workspace_id == c.ws
    assert c.panel.evidence_list.rowCount() == 0
    assert c.panel.attachments.rowCount() == 0
    assert c.panel.create_button.isEnabled()
    for button in (c.panel.edit_button, c.panel.detail_button, c.panel.attach_button,
                   c.panel.detach_button, c.panel.delete_button, c.panel.restore_button):
        assert not button.isEnabled()
    assert "0 条" in c.panel.status.text()
    c.panel.refresh_button.click()                   # a real click must not lose the context
    assert c.panel.evidence_list.rowCount() == 0
    assert "0 条" in c.panel.status.text()
    c.panel.set_event(None)
    assert c.panel.evidence_list.rowCount() == 0
    assert not c.panel.create_button.isEnabled()
    assert "未选择事件" in c.panel.status.text()
    assert c.errors == []


def test_workspace_switch_clears_evidence_panel(evidence_ui):
    c = evidence_ui
    make_evidence(c, title="A")
    assert c.panel.evidence_list.rowCount() == 1
    other = c.services.workspaces.create("Other")
    c.page.activate()
    c.page.workspace_combo.setCurrentIndex(c.page.workspace_combo.findData(str(other.id)))
    assert c.page.current_detail is None
    assert c.panel.evidence_list.rowCount() == 0
    assert not c.panel.create_button.isEnabled()
    assert "未选择事件" in c.panel.status.text()
    assert c.errors == []


def test_create_text_and_image_evidence(evidence_ui, monkeypatch):
    c = evidence_ui
    captured = datetime(2026, 9, 28, 21, 14, tzinfo=timezone.utc)
    long_annotation = "用户注释 " * 40
    stub_dialog(monkeypatch, "EvidenceCreateDialog", dict(
        kind="TEXT", title="聊天文本", source_type="CHAT", source_label="WeChat",
        source_reference="thread-1", captured_at=captured, content_text="<b>原始聊天文本</b>",
        annotation=long_annotation, media=()))
    c.panel.create_evidence()
    assert c.panel.evidence_list.rowCount() == 1
    row = row_for(c, "聊天文本")
    assert c.panel.evidence_list.item(row, 0).text() == "聊天文本"
    assert c.panel.evidence_list.item(row, 1).text() == "TEXT"
    assert c.panel.evidence_list.item(row, 2).text() == "CHAT · WeChat"
    assert c.panel.evidence_list.item(row, 3).text() == "2026-09-28T21:14:00.000000+00:00"
    assert c.panel.evidence_list.item(row, 4).text() == "0"
    summary = c.panel.evidence_list.item(row, 5).text()
    assert summary.endswith("…") and len(summary) == module.SUMMARY_LIMIT + 1
    text_detail = c.panel.evidence_list.item(row, 0).data(ROLE)
    assert text_detail.evidence.content_text == "<b>原始聊天文本</b>"
    assert text_detail.evidence.annotation == long_annotation
    # A cancelled dialog writes nothing.
    stub_dialog(monkeypatch, "EvidenceCreateDialog", dict(
        kind="TEXT", title="不应写入", source_type="MANUAL", source_label=None, source_reference=None,
        captured_at=None, content_text=None, annotation=None, media=()), accepted=False)
    c.panel.create_evidence()
    assert c.panel.evidence_list.rowCount() == 1
    # IMAGE evidence attached to an existing Workspace media.
    stub_dialog(monkeypatch, "EvidenceCreateDialog", dict(
        kind="IMAGE", title="聊天截图", source_type="IMAGE", source_label=None, source_reference=None,
        captured_at=None, content_text=None, annotation=None, media=(c.media.id,)))
    c.panel.create_evidence()
    assert c.panel.evidence_list.rowCount() == 2
    row = select_row(c, "聊天截图")
    assert c.panel.evidence_list.item(row, 3).text() == "—"       # captured_at is NULL
    assert c.panel.evidence_list.item(row, 4).text() == "1"
    assert c.panel.evidence_list.item(row, 5).text() == "—"       # annotation is NULL
    assert c.panel.attachments.rowCount() == 1
    assert c.panel.attachments.item(0, 0).text() == c.media.storage_path
    assert c.services.evidence.detail(c.ws, c.panel.evidence_list.item(row, 0).data(ROLE).evidence.id).media[0].media.id == c.media.id
    assert c.errors == []


def test_link_without_source_reference_is_refused(evidence_ui, monkeypatch):
    c = evidence_ui
    with pytest.raises(DomainError) as info:
        c.services.evidence.create_evidence(c.ws, c.event.id, "LINK", "链接证据", source_type="WEB")
    message = str(info.value)
    stub_dialog(monkeypatch, "EvidenceCreateDialog", dict(
        kind="LINK", title="链接证据", source_type="WEB", source_label=None, source_reference=None,
        captured_at=None, content_text=None, annotation=None, media=()))
    c.panel.create_evidence()
    assert c.panel.evidence_list.rowCount() == 0
    assert c.errors == [message]                     # the service text is shown unchanged
    assert c.services.evidence.list_evidence(c.ws, c.event.id, include_deleted=True) == []


@pytest.mark.parametrize("text", ["2026-09-28", "2026-09-28T21:14:00", "not-a-time"])
def test_captured_at_requires_timezone(text):
    with pytest.raises(DomainError):
        parse_captured(text)


def test_captured_at_blank_is_null_and_offsets_normalize():
    assert parse_captured("   ") is None
    assert parse_captured("2026-09-28T21:14:00+08:00") == datetime(2026, 9, 28, 13, 14, tzinfo=timezone.utc)
    assert parse_captured("2026-09-28T13:14:00Z") == datetime(2026, 9, 28, 13, 14, tzinfo=timezone.utc)


def test_create_dialog_fields_media_selection_and_validation(evidence_ui):
    c = evidence_ui
    dialog = EvidenceCreateDialog(c.services.evidence, c.panel.list_media)
    try:
        assert {dialog.kind.itemData(index) for index in range(dialog.kind.count())} == set(c.services.evidence.KINDS)
        assert {dialog.source_type.itemData(index) for index in range(dialog.source_type.count())} == set(c.services.evidence.SOURCE_TYPES)
        assert dialog.source_type.currentData() == "MANUAL"
        assert dialog.media.rowCount() == 3
        dialog.title.setText("Title")
        dialog.captured_at.setText("2026-09-28T21:14:00")
        with pytest.raises(DomainError):
            dialog.values()
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert c.errors
        c.errors.clear()
        dialog.captured_at.setText("2026-09-28T21:14:00+08:00")
        dialog.media.item(0, 0).setCheckState(Qt.CheckState.Checked)
        dialog.media.item(0, 1).setText("SCREENSHOT")
        values = dialog.values()
        assert values["captured_at"] == datetime(2026, 9, 28, 13, 14, tzinfo=timezone.utc)
        assert values["kind"] == "TEXT" and values["source_type"] == "MANUAL"
        assert values["content_text"] is None and values["annotation"] is None
        assert len(values["media"]) == 1
        assert values["media"][0].media_id == dialog.media.item(0, 0).data(ROLE)
        assert values["media"][0].role == "SCREENSHOT"
        dialog.media.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.captured_at.clear()
        assert dialog.values()["captured_at"] is None and dialog.values()["media"] == ()
    finally:
        dialog.close()


def test_detail_dialog_separates_raw_content_from_annotation(evidence_ui):
    c = evidence_ui
    captured = datetime(2026, 9, 28, 13, 14, 0, 123456, tzinfo=timezone.utc)
    evidence = make_evidence(c, title="原始标题", annotation="旧注释",
                             content_text="<b>原始内容</b>\n第二行", captured_at=captured,
                             source_type="CHAT", source_label="WeChat", source_reference="thread-1",
                             media=(c.media.id,))
    select_row(c, "原始标题")
    dialog = c.panel.show_detail()
    assert dialog is not None and dialog.isVisible()
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert dialog in c.panel._dialogs
    assert dialog.content.isReadOnly()
    assert dialog.content.toPlainText() == "<b>原始内容</b>\n第二行"
    assert not dialog.annotation.isReadOnly()
    assert dialog.annotation.toPlainText() == "旧注释"
    assert dialog.fields["kind"].text() == "TEXT"
    assert dialog.fields["source_type"].text() == "CHAT"
    assert dialog.fields["source_label"].text() == "WeChat"
    assert dialog.fields["source_reference"].text() == "thread-1"
    assert dialog.fields["captured_at"].text() == "2026-09-28T13:14:00.123456+00:00"
    assert dialog.fields["created_at"].text().endswith("+00:00")
    assert dialog.fields["lifecycle"].text() == "有效记录"
    for label in dialog.fields.values():
        assert label.textFormat() == Qt.TextFormat.PlainText
    # No editor exists for content_text / kind / source_* / captured_at.
    assert [widget.objectName() for widget in dialog.findChildren(QTextEdit)] == [
        "evidenceDetailContent", "evidenceDetailAnnotation"]
    assert [widget.objectName() for widget in dialog.findChildren(QTextEdit) if not widget.isReadOnly()] == [
        "evidenceDetailAnnotation"]
    assert dialog.findChildren(QLineEdit) == []
    raw = dialog.findChild(QGroupBox, "evidenceRawGroup")
    annotation_group = dialog.findChild(QGroupBox, "evidenceAnnotationGroup")
    assert raw is not None and annotation_group is not None
    assert raw.findChildren(QTextEdit) == [dialog.content]
    assert [widget for widget in raw.findChildren(QTextEdit) if not widget.isReadOnly()] == []
    assert dialog.content.parent() is not annotation_group
    assert annotation_group.findChildren(QTextEdit) == [dialog.annotation]
    assert dialog.attachments.rowCount() == 1
    assert dialog.attachments.item(0, 0).text() == c.media.storage_path
    dialog.annotation.setPlainText("新注释")
    dialog.save_annotation()
    updated = c.services.evidence.get(c.ws, evidence.id, include_deleted=True)
    assert (updated.title, updated.annotation) == ("原始标题", "新注释")
    assert updated.content_text == "<b>原始内容</b>\n第二行"
    assert updated.kind == "TEXT" and updated.source_type == "CHAT"
    assert dialog.annotation.toPlainText() == "新注释"
    dialog.close()
    c.qt_app.processEvents()
    assert c.panel._dialogs == []
    assert c.panel.evidence_list.item(row_for(c, "原始标题"), 5).text() == "新注释"
    assert c.errors == []


def test_metadata_dialog_only_offers_title_and_annotation(evidence_ui, monkeypatch):
    c = evidence_ui
    evidence = make_evidence(c, title="原标题", content_text="原始内容", annotation="旧注释")
    detail = c.services.evidence.detail(c.ws, evidence.id)
    dialog = EvidenceMetadataDialog(detail)
    try:
        assert set(dialog.values()) == {"title", "annotation"}
        assert dialog.content.isReadOnly()
        assert dialog.content.toPlainText() == "原始内容"
        assert [widget for widget in dialog.findChildren(QLineEdit)] == [dialog.title]
        assert not dialog.annotation.isReadOnly()
        dialog.title.setText("新标题")
        dialog.annotation.setPlainText("新注释")
        assert dialog.values() == {"title": "新标题", "annotation": "新注释"}
        dialog.title.setText("")
        with pytest.raises(DomainError):
            dialog.values()
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert c.errors
        c.errors.clear()
    finally:
        dialog.close()
    select_row(c, "原标题")
    stub_dialog(monkeypatch, "EvidenceMetadataDialog", {"title": "新标题", "annotation": "新注释"})
    c.panel.edit_metadata()
    updated = c.services.evidence.get(c.ws, evidence.id, include_deleted=True)
    assert (updated.title, updated.annotation) == ("新标题", "新注释")
    assert (updated.kind, updated.source_type) == ("TEXT", "CHAT")
    assert (updated.source_reference, updated.content_text) == ("thread-1", "原始内容")
    assert c.panel.evidence_list.item(row_for(c, "新标题"), 5).text() == "新注释"
    # The service refuses any field the UI must never send.
    stub_dialog(monkeypatch, "EvidenceMetadataDialog", {"title": "改名", "annotation": None, "kind": "OTHER"})
    c.panel.edit_metadata()
    assert c.errors
    c.errors.clear()
    assert c.services.evidence.get(c.ws, evidence.id, include_deleted=True).title == "新标题"


def test_attach_remove_media_reference_and_import_file(evidence_ui, monkeypatch):
    c = evidence_ui
    evidence = make_evidence(c, kind="MANUAL", title="手工记录", source_type="MANUAL")
    select_row(c, "手工记录")
    assert c.panel.attachments.rowCount() == 0
    assert not c.panel.detach_button.isEnabled()
    stub_dialog(monkeypatch, "MediaAttachDialog", (c.media.id, "SCREENSHOT"))
    c.panel.add_media()
    assert c.panel.attachments.rowCount() == 1
    assert c.panel.attachments.item(0, 4).text() == "SCREENSHOT"
    assert c.panel.attachments.item(0, 6).text() == str(c.media.id)
    second_path = c.services.media.original_path(c.ws, c.second_media.id)
    second_bytes = second_path.read_bytes()
    c.panel.attachments.selectRow(0)
    assert c.panel.detach_button.isEnabled()
    c.panel.remove_media()
    assert c.panel.attachments.rowCount() == 0
    assert len(c.services.evidence.detail(c.ws, evidence.id).media) == 0
    assert second_path.exists() and second_path.read_bytes() == second_bytes      # bytes survive
    assert c.services.media.get(c.ws, c.second_media.id).id == c.second_media.id  # media survives
    # Importing a new file reuses the existing media service and then attaches it.
    new_file = c.tmp_path / "imported-evidence.txt"
    new_file.write_text("private imported attachment", encoding="utf-8")
    monkeypatch.setattr(dialog_module.QFileDialog, "getOpenFileName",
                        lambda *args, **kwargs: (str(new_file), "所有文件 (*)"))
    dialog = MediaAttachDialog(c.panel.list_media, c.panel.import_media)
    try:
        assert dialog.picker.media_list.rowCount() == 3
        imported = dialog.picker.import_file()
        assert imported is not None and imported.size_bytes == len(new_file.read_bytes())
        assert dialog.picker.media_list.rowCount() == 4
        assert dialog.picker.selected_id() == imported.id
        assert dialog.values() == (imported.id, None)
    finally:
        dialog.close()
    stub_dialog(monkeypatch, "MediaAttachDialog", (imported.id, None))
    c.panel.add_media()
    assert c.panel.attachments.rowCount() == 1
    assert c.services.evidence.detail(c.ws, evidence.id).media[0].media.id == imported.id
    assert c.errors == []


def test_stale_attachment_snapshot_conflict_changes_nothing(evidence_ui, monkeypatch):
    c = evidence_ui
    evidence = make_evidence(c, kind="IMAGE", title="截图", source_type="IMAGE", media=(c.media.id,))
    select_row(c, "截图")
    assert c.panel.attachments.rowCount() == 1
    # Another window attaches a second media after this panel loaded.
    c.services.evidence.add_media(c.ws, evidence.id, c.second_media.id, "EXTRA")
    assert len(c.services.evidence.detail(c.ws, evidence.id).media) == 2
    assert c.panel.attachments.rowCount() == 1           # the panel still shows its snapshot
    c.panel.attachments.selectRow(0)
    c.panel.remove_media()
    assert len(c.errors) == 1
    assert c.panel.attachments.rowCount() == 2           # conflict refreshed the panel
    assert len(c.services.evidence.detail(c.ws, evidence.id).media) == 2
    # Adding with a stale snapshot is refused the same way.
    c.errors.clear()
    fourth = c.tmp_path / "fourth.png"
    Image.new("RGB", (8, 8), "white").save(fourth)
    fourth_media = c.services.media.import_file(c.ws, fourth)
    c.services.evidence.add_media(c.ws, evidence.id, c.third_media.id, "EXTRA")
    assert len(c.services.evidence.detail(c.ws, evidence.id).media) == 3
    assert c.panel.attachments.rowCount() == 2           # the panel still shows its own snapshot
    stub_dialog(monkeypatch, "MediaAttachDialog", (fourth_media.id, None))
    c.panel.add_media()
    assert len(c.errors) == 1
    detail = c.services.evidence.detail(c.ws, evidence.id)
    assert len(detail.media) == 3
    assert fourth_media.id not in {view.media.id for view in detail.media}
    assert c.panel.attachments.rowCount() == 3


def test_soft_delete_and_restore_evidence(evidence_ui):
    c = evidence_ui
    evidence = make_evidence(c, title="待删除", content_text="不可变的原始内容")
    assert c.panel.evidence_list.rowCount() == 1
    c.panel.delete_evidence()
    assert c.panel.evidence_list.rowCount() == 0
    deleted = c.services.evidence.get(c.ws, evidence.id, include_deleted=True)
    assert deleted.deleted_at is not None
    assert deleted.content_text == "不可变的原始内容"
    c.panel.include_deleted.setChecked(True)
    assert c.panel.evidence_list.rowCount() == 1
    assert c.panel.evidence_list.item(0, 0).text() == "已删除 · 待删除"
    assert c.panel.restore_button.isEnabled()
    assert not c.panel.edit_button.isEnabled() and not c.panel.delete_button.isEnabled()
    select_row(c, "待删除")
    dialog = c.panel.show_detail()                       # deleted raw content stays readable
    assert dialog is not None
    assert dialog.fields["lifecycle"].text() == "已删除"
    assert dialog.content.toPlainText() == "不可变的原始内容"
    dialog.close()
    c.qt_app.processEvents()
    c.panel.restore_evidence()
    restored = c.services.evidence.get(c.ws, evidence.id, include_deleted=True)
    assert restored.deleted_at is None
    assert restored.content_text == "不可变的原始内容"
    assert c.panel.evidence_list.item(row_for(c, "待删除"), 0).text() == "待删除"
    assert not c.panel.restore_button.isEnabled()
    c.panel.include_deleted.setChecked(False)
    assert c.panel.evidence_list.rowCount() == 1
    assert c.errors == []


def test_deleted_event_hides_evidence_until_deleted_records_requested(evidence_ui):
    c = evidence_ui
    evidence = make_evidence(c, title="事件证据")
    assert c.panel.evidence_list.rowCount() == 1
    c.page.delete_event()
    assert c.services.evidence.get(c.ws, evidence.id, include_deleted=True).deleted_at is None
    assert c.panel.evidence_list.rowCount() == 0
    assert not c.panel.create_button.isEnabled()
    c.page.include_deleted.setChecked(True)
    assert c.page.event_list.count() == 1
    assert c.panel.evidence_list.rowCount() == 1
    assert not c.panel.create_button.isEnabled()         # a deleted Event allows no Evidence write
    assert not c.panel.edit_button.isEnabled() and not c.panel.delete_button.isEnabled()
    assert c.panel.detail_button.isEnabled()
    # The panel's own checkbox is what reveals content for a soft-deleted Event.
    deleted_event = c.services.events.get(c.ws, c.event.id, include_deleted=True)
    c.panel.set_event(deleted_event)
    assert c.panel.evidence_list.rowCount() == 0
    assert "隐藏" in c.panel.status.text()
    c.panel.include_deleted.setChecked(True)
    assert c.panel.evidence_list.rowCount() == 1
    c.panel.include_deleted.setChecked(False)
    assert c.panel.evidence_list.rowCount() == 0
    c.page.restore_event()
    assert c.page.current_detail.event.deleted_at is None
    assert c.panel.evidence_list.rowCount() == 1         # visible again without any Evidence change
    c.page.include_deleted.setChecked(False)
    assert c.panel.evidence_list.rowCount() == 1
    assert c.panel.create_button.isEnabled()
    assert c.services.evidence.get(c.ws, evidence.id, include_deleted=True).deleted_at is None
    assert c.errors == []


def test_timeline_single_event_detail_shows_evidence_panel(evidence_ui):
    c = evidence_ui
    make_evidence(c, title="时间线证据", annotation="时间线注释")
    c.people.refresh_people(c.person.id)
    dialog = c.people.event_timeline.show_detail()
    try:
        assert dialog is not None and dialog.isVisible()
        controller = dialog.controller
        assert controller.workspace_id == c.ws
        assert controller.isAncestorOf(controller.evidence_panel)
        assert controller.evidence_panel.evidence_list.rowCount() == 1
        assert controller.evidence_panel.evidence_list.item(0, 0).data(ROLE).evidence.title == "时间线证据"
    finally:
        dialog.close()
        c.qt_app.processEvents()
    assert c.errors == []


def test_private_evidence_text_is_not_logged(evidence_ui, caplog):
    c = evidence_ui
    private = "private-evidence-content-must-not-be-logged"
    def fail():
        raise DomainError(private)
    with caplog.at_level(logging.WARNING, logger="app.ui.evidence_panel"):
        c.panel.run_operation(fail)
    assert "DomainError" in caplog.text
    assert private not in caplog.text
    assert c.errors == [private]
    c.errors.clear()
    with caplog.at_level(logging.WARNING, logger="app"):
        make_evidence(c, title="私密标题", content_text=private, annotation=private, source_label=private)
    assert private not in caplog.text
    assert c.panel.evidence_list.rowCount() == 1
    assert c.errors == []


def test_unknown_evidence_failure_shows_generic_message(evidence_ui, caplog):
    c = evidence_ui
    def fail():
        raise RuntimeError("private-technical-detail")
    with caplog.at_level(logging.ERROR, logger="app.ui.evidence_panel"):
        c.panel.run_operation(fail)
    assert "RuntimeError" in caplog.text
    assert "private-technical-detail" not in caplog.text
    assert c.errors == ["发生技术错误，请查看本地错误日志。"]
