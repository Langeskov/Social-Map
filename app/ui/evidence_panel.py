"""Evidence panel for the selected Event.

Raw evidence is never edited here: only the user annotation (and title) may be
written, ``content_text``/``kind``/``source_type``/``source_reference``/
``captured_at`` have no editor in this UI. Attachments are references to
Workspace media; removing one only detaches it and never deletes the file.

Every mutating call passes the attachment snapshot taken from the EvidenceDetail
the panel is currently displaying, so a stale panel cannot detach an attachment
another window just added.
"""
import logging
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QMessageBox, QPushButton,
    QTableWidgetItem, QVBoxLayout, QWidget)
from app.domain.errors import ConflictError, DomainError
from app.services.errors import DatabaseError
from app.ui.event_detail import ROLE, plain_label, table, time_text
from app.ui.evidence_dialog import (EvidenceCreateDialog, EvidenceDetailDialog, EvidenceMetadataDialog,
    MediaAttachDialog, attachment_values, make_attachment_table)

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole
#: Extra role on the first column: the Evidence id, independent of the displayed text.
EVIDENCE_ID_ROLE = ROLE + 1
SUMMARY_LIMIT = 80

EVIDENCE_HEADERS = ["标题", "类型", "来源", "captured_at UTC", "附件", "注释摘要"]


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Evidence UI", title="证据操作未完成")


def summary_text(value, limit=SUMMARY_LIMIT):
    if not value:
        return "—"
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit] + "…"


def source_text(evidence):
    label = evidence.source_label
    return evidence.source_type + (f" · {label}" if label else "")


def attachment_snapshot(detail):
    """The attachment set the panel is currently displaying for ``detail``."""
    from app.services.evidence import EvidenceService
    return EvidenceService.attachment_ids(detail)


class EvidencePanel(QWidget):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("evidencePanel")
        self.services = services
        self.service = getattr(services, "evidence", None)
        self.current_event = None
        self.workspace_id = None
        self.include_deleted_event = False
        self._details = {}
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(plain_label("Evidence · 原始证据（原始内容只读，用户注释单独保存）"))
        self.include_deleted = QCheckBox("显示已删除证据 / 已删除事件下的证据")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        layout.addWidget(self.include_deleted)
        self.evidence_list = table(EVIDENCE_HEADERS, "evidenceList")
        self.evidence_list.setMinimumHeight(170)
        self.evidence_list.itemSelectionChanged.connect(self.select_evidence)
        layout.addWidget(self.evidence_list)
        row = QHBoxLayout()
        self.create_button = self.button(row, "新建证据", self.create_evidence)
        self.edit_button = self.button(row, "编辑元数据（标题/注释）", self.edit_metadata)
        self.detail_button = self.button(row, "查看原始内容与附件", self.show_detail)
        self.refresh_button = self.button(row, "刷新证据", self.refresh)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.attach_button = self.button(row, "添加媒体", self.add_media)
        self.detach_button = self.button(row, "移除所选媒体引用", self.remove_media)
        self.delete_button = self.button(row, "删除证据", self.delete_evidence)
        self.restore_button = self.button(row, "恢复证据", self.restore_evidence)
        layout.addLayout(row)
        layout.addWidget(plain_label("所显示证据的附件（移除只解绑引用，不删除文件）"))
        self.attachments = make_attachment_table("evidenceAttachments")
        self.attachments.setMinimumHeight(130)
        self.attachments.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.attachments)
        self.status = plain_label("未选择事件：证据面板已清空。")
        layout.addWidget(self.status)
        self.update_buttons()

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        return run_operation(self, callback)

    # ---------------------------------------------------------------- context

    def set_event(self, event, *, include_deleted_event=False):
        self.current_event = event
        self.workspace_id = getattr(event, "workspace_id", None)
        self.include_deleted_event = bool(include_deleted_event)
        self.refresh()

    def visible(self):
        """A soft-deleted Event hides its Evidence until deleted records are asked for."""
        if self.service is None or self.current_event is None:
            return False
        if getattr(self.current_event, "deleted_at", None) is None:
            return True
        return self.include_deleted.isChecked() or self.include_deleted_event

    def listing_include_deleted(self):
        """Deleted Evidence is listed when asked for, and always for a deleted Event."""
        if self.current_event is None:
            return False
        return self.include_deleted.isChecked() or getattr(self.current_event, "deleted_at", None) is not None

    def selected_detail(self):
        item = self.evidence_list.item(self.evidence_list.currentRow(), 0)
        return item.data(ROLE) if item is not None else None

    def selected_attachment(self):
        item = self.attachments.item(self.attachments.currentRow(), 0)
        return item.data(ROLE) if item is not None else None

    def can_write(self, detail):
        return (self.service is not None and self.current_event is not None
                and self.workspace_id is not None
                and getattr(self.current_event, "deleted_at", None) is None
                and detail is not None and detail.evidence.deleted_at is None)

    # ------------------------------------------------------------------ state

    def update_buttons(self):
        available = self.service is not None and self.current_event is not None
        shown = self.visible()
        active = shown and getattr(self.current_event, "deleted_at", None) is None and self.workspace_id is not None
        detail = self.selected_detail()
        evidence = detail.evidence if detail is not None else None
        writable = active and evidence is not None and evidence.deleted_at is None
        self.include_deleted.setEnabled(available)
        self.create_button.setEnabled(bool(active))
        self.edit_button.setEnabled(bool(writable))
        self.detail_button.setEnabled(detail is not None)
        self.attach_button.setEnabled(bool(writable))
        self.detach_button.setEnabled(bool(writable and self.selected_attachment() is not None))
        self.delete_button.setEnabled(bool(writable))
        self.restore_button.setEnabled(bool(active and evidence is not None and evidence.deleted_at is not None))
        self.refresh_button.setEnabled(bool(available))

    def refresh(self, selected_id=None):
        # Qt clicked can pass a bool; only preserve a genuine Evidence id.
        if isinstance(selected_id, bool):
            selected_id = None
        if selected_id is None:
            current = self.selected_detail()
            selected_id = current.evidence.id if current is not None else None
        self._details = {}
        self.evidence_list.setRowCount(0)
        self.attachments.setRowCount(0)
        self.update_buttons()
        if self.service is None:
            self.status.setText("证据服务尚未装配：证据面板不可用。")
            return
        if self.current_event is None:
            self.status.setText("未选择事件：证据面板已清空。")
            return
        if not self.visible():
            self.status.setText("事件已删除：证据默认隐藏。勾选“显示已删除证据”后可见；"
                                "恢复事件后自动恢复显示。未修改任何 Evidence。")
            return

        def load():
            include_deleted = self.listing_include_deleted()
            details = self.service.list_evidence(self.workspace_id, self.current_event.id,
                                                 include_deleted=include_deleted)
            self._details = {}
            self.evidence_list.blockSignals(True)
            try:
                self.evidence_list.setRowCount(0)
                target = 0
                for index, detail in enumerate(details):
                    evidence = detail.evidence
                    self._details[evidence.id] = detail
                    row = self.evidence_list.rowCount()
                    self.evidence_list.insertRow(row)
                    values = [("已删除 · " if evidence.deleted_at else "") + evidence.title, evidence.kind,
                              source_text(evidence), time_text(evidence.captured_at),
                              str(len(detail.media)), summary_text(evidence.annotation)]
                    for column, value in enumerate(values):
                        item = QTableWidgetItem(value)
                        if column == 0:
                            item.setData(ROLE, detail)
                            # The row keeps its id in the extra role as well, so a
                            # cross-page jump can select one evidence without relying
                            # on the displayed text.
                            item.setData(EVIDENCE_ID_ROLE, evidence.id)
                        self.evidence_list.setItem(row, column, item)
                    if evidence.id == selected_id:
                        target = index
                if details:
                    self.evidence_list.selectRow(target)
            finally:
                self.evidence_list.blockSignals(False)
            self.status.setText(f"事件证据 · {len(details)} 条"
                                + ("（含已删除）" if include_deleted else ""))
            self.select_evidence()
        self.run_operation(load)

    def evidence_ids(self):
        """Every listed Evidence id, in the displayed order."""
        return [self.evidence_list.item(row, 0).data(EVIDENCE_ID_ROLE)
                for row in range(self.evidence_list.rowCount())]

    def focus_evidence(self, evidence_id):
        """Select one listed Evidence and show its attachments (read-only jump).

        The refresh above always asks for deleted Evidence, so a soft-deleted row
        is selectable too; nothing is written by selecting a row.
        """
        for row in range(self.evidence_list.rowCount()):
            item = self.evidence_list.item(row, 0)
            if item is not None and item.data(EVIDENCE_ID_ROLE) == evidence_id:
                self.evidence_list.selectRow(row)
                self.select_evidence()
                return self.selected_detail()
        return None

    def select_evidence(self):
        detail = self.selected_detail()
        self.attachments.setRowCount(0)
        if detail is not None:
            for view in detail.media:
                row = self.attachments.rowCount()
                self.attachments.insertRow(row)
                for column, value in enumerate(attachment_values(view)):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(ROLE, view)
                    self.attachments.setItem(row, column, item)
        self.update_buttons()

    # --------------------------------------------------------- service bridge

    def list_media(self, search=""):
        """Callback used by the dialogs; returns None when the list is unavailable."""
        def load():
            if self.service is None or self.workspace_id is None:
                raise DomainError("证据服务尚未装配或未选择事件，无法列出媒体。")
            return list(self.service.list_attachable_media(self.workspace_id, search=search))
        return self.run_operation(load)

    def import_media(self, path):
        """Import a new file through the existing media service, then attach it."""
        def load():
            if self.workspace_id is None:
                raise DomainError("请先选择事件，再导入新媒体文件。")
            return self.services.media.import_file(self.workspace_id, Path(path))
        return self.run_operation(load)

    def save_annotation(self, detail, text):
        def save():
            self.service.update_metadata(self.workspace_id, detail.evidence.id, annotation=text.strip() or None)
            self.refresh(detail.evidence.id)
            return self.service.detail(self.workspace_id, detail.evidence.id, include_deleted=True)
        return self.run_operation(save)

    # ------------------------------------------------------------- operations

    def create_evidence(self):
        if (self.service is None or self.current_event is None
                or getattr(self.current_event, "deleted_at", None) is not None):
            return None

        def create():
            dialog = EvidenceCreateDialog(self.service, self.list_media, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                evidence = self.service.create_evidence(self.workspace_id, self.current_event.id,
                                                        **dialog.values())
                self.refresh(evidence.id)
                return evidence
            return None
        return self.run_operation(create)

    def edit_metadata(self):
        detail = self.selected_detail()
        if not self.can_write(detail):
            return None

        def edit():
            dialog = EvidenceMetadataDialog(detail, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.service.update_metadata(self.workspace_id, detail.evidence.id, **dialog.values())
                self.refresh(detail.evidence.id)
        return self.run_operation(edit)

    def show_detail(self):
        detail = self.selected_detail()
        if detail is None:
            return None

        def show():
            current = self.service.detail(self.workspace_id, detail.evidence.id, include_deleted=True)
            dialog = EvidenceDetailDialog(current, lambda text: self.save_annotation(current, text), self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)

    def add_media(self):
        detail = self.selected_detail()
        if not self.can_write(detail):
            return None

        def add():
            dialog = MediaAttachDialog(self.list_media, self.import_media, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            media_id, role = dialog.values()
            try:
                link = self.service.add_media(self.workspace_id, detail.evidence.id, media_id, role,
                                              expected_attachment_ids=attachment_snapshot(detail))
            except ConflictError:
                self.refresh(detail.evidence.id)
                raise
            self.refresh(detail.evidence.id)
            return link
        return self.run_operation(add)

    def remove_media(self):
        detail = self.selected_detail()
        view = self.selected_attachment()
        if view is None or not self.can_write(detail):
            return None

        def remove():
            try:
                self.service.remove_media(self.workspace_id, detail.evidence.id, view.media.id,
                                          expected_attachment_ids=attachment_snapshot(detail))
            except ConflictError:
                self.refresh(detail.evidence.id)
                raise
            self.refresh(detail.evidence.id)
        return self.run_operation(remove)

    def delete_evidence(self):
        detail = self.selected_detail()
        if not self.can_write(detail):
            return None
        if QMessageBox.question(self, "删除证据",
                                "将软删除该 Evidence；原始内容与附件引用保留，可恢复。是否继续？") != QMessageBox.StandardButton.Yes:
            return None

        def delete():
            self.service.soft_delete_evidence(self.workspace_id, detail.evidence.id)
            self.refresh(detail.evidence.id)
        return self.run_operation(delete)

    def restore_evidence(self):
        detail = self.selected_detail()
        if (detail is None or self.service is None or self.workspace_id is None
                or getattr(self.current_event, "deleted_at", None) is not None
                or detail.evidence.deleted_at is None):
            return None

        def restore():
            self.service.restore_evidence(self.workspace_id, detail.evidence.id)
            self.refresh(detail.evidence.id)
        return self.run_operation(restore)
