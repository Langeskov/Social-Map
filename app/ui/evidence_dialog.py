"""Evidence dialogs. Raw evidence is only ever displayed read-only.

The dialogs perform no persistence themselves: listing media and saving the
annotation are callbacks owned by the Evidence panel, so one operation boundary
(and one log category) covers every Evidence write. Only these fields are
editable anywhere in this module: ``title`` and ``annotation``. ``content_text``,
``kind``, ``source_type``, ``source_label``, ``source_reference`` and
``captured_at`` are rendered with plain read-only controls and have no editor.
"""
from datetime import datetime, timezone
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHeaderView, QHBoxLayout, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import DomainError
from app.ui.event_detail import ROLE, plain_label, time_text

ATTACHMENT_HEADERS = ["文件（存储路径）", "MIME", "大小 (bytes)", "尺寸", "角色", "引用建立 UTC", "媒体 UUID"]

CREATE_MEDIA_HEADERS = ["附加已有媒体（勾选）", "角色", "媒体 UUID"]


def parse_captured(text):
    """ISO 8601 that must carry an explicit timezone; blank means NULL."""
    if not text.strip():
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except ValueError:
        raise DomainError("captured_at 必须是包含时区的 ISO 时间，例如 2026-09-28T21:14:00+08:00；"
                          "留空表示未知（NULL）。") from None


def dimensions_text(media):
    if media.width is None or media.height is None:
        return "—"
    return f"{media.width} × {media.height}"


def attachment_values(view):
    """One attachment row; never renders file bytes, only stored metadata."""
    media, link = view.media, view.link
    return [media.storage_path, media.mime_type, str(media.size_bytes), dimensions_text(media),
            link.role or "—", time_text(link.created_at), str(media.id)]


def media_choice_text(media):
    return f"{media.mime_type} · {media.size_bytes} bytes · {media.storage_path} · {str(media.id)[:8]}"


def fill_attachment_table(target, views):
    target.setRowCount(0)
    for view in views:
        row = target.rowCount()
        target.insertRow(row)
        for column, value in enumerate(attachment_values(view)):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(ROLE, view)
            target.setItem(row, column, item)


def make_attachment_table(name):
    result = QTableWidget(0, len(ATTACHMENT_HEADERS))
    result.setObjectName(name)
    result.setHorizontalHeaderLabels(ATTACHMENT_HEADERS)
    result.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    result.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    result.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return result


class MediaPicker(QWidget):
    """Shared "pick an existing Workspace media" table with search and import."""

    def __init__(self, list_media, import_media, parent=None):
        super().__init__(parent)
        self.list_media, self.import_media = list_media, import_media
        self.on_change = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索当前 Workspace 的媒体（SHA256 / MIME / 路径）")
        self.search.returnPressed.connect(self.reload)
        row.addWidget(self.search, 1)
        self.search_button = QPushButton("搜索")
        self.search_button.clicked.connect(self.reload)
        row.addWidget(self.search_button)
        self.import_button = QPushButton("导入新文件…")
        self.import_button.clicked.connect(self.import_file)
        row.addWidget(self.import_button)
        layout.addLayout(row)
        self.media_list = QTableWidget(0, 5)
        self.media_list.setObjectName("evidenceMediaPicker")
        self.media_list.setHorizontalHeaderLabels(["媒体", "大小 (bytes)", "尺寸", "记录情况", "媒体 UUID"])
        self.media_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.media_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.media_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.media_list.setMinimumHeight(150)
        self.media_list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.media_list.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.media_list)
        self.note = plain_label("")
        layout.addWidget(self.note)

    def _selection_changed(self):
        if self.on_change is not None:
            self.on_change()

    def selected_id(self):
        item = self.media_list.item(self.media_list.currentRow(), 0)
        return item.data(ROLE) if item is not None else None

    def reload(self, selected_id=None):
        # Qt clicked can pass a bool; only preserve a genuine Media id.
        if isinstance(selected_id, bool):
            selected_id = None
        self.media_list.setRowCount(0)
        media = self.list_media(self.search.text().strip())
        if media is None:
            self.note.setText("媒体列表不可用；未修改任何数据。")
            self._selection_changed()
            return None
        target = 0
        for index, entry in enumerate(media):
            row = self.media_list.rowCount()
            self.media_list.insertRow(row)
            values = [f"{entry.mime_type} · {entry.storage_path}", str(entry.size_bytes),
                      dimensions_text(entry), "已删除" if entry.deleted_at else "可引用", str(entry.id)]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ROLE, entry.id)
                self.media_list.setItem(row, column, item)
            if entry.id == selected_id:
                target = index
        if media:
            self.media_list.selectRow(target)
        self.note.setText(f"当前 Workspace 可引用媒体 · {len(media)} 条（导入新文件会先写入媒体库再附加引用）")
        self._selection_changed()
        return media

    def import_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入新媒体文件", "", "所有文件 (*)")
        if not filename:
            return None
        media = self.import_media(Path(filename))
        if media is not None:
            self.reload(selected_id=media.id)
        return media

    def selected_role(self):
        return None

    def values(self):
        if self.selected_id() is None:
            raise DomainError("请选择一个媒体，或先导入新文件。")
        return self.selected_id(), None


class MediaAttachDialog(QDialog):
    def __init__(self, list_media, import_media, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加媒体引用（不会复制或删除文件）")
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label("只建立 Evidence 与已有 Media 的引用；移除引用不会删除文件。"))
        self.picker = MediaPicker(list_media, import_media, self)
        layout.addWidget(self.picker)
        form = QFormLayout()
        self.role = QLineEdit()
        self.role.setPlaceholderText("引用角色（可留空）")
        form.addRow("角色", self.role)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.picker.on_change = self.update_buttons
        self.picker.reload()
        self.update_buttons()

    def update_buttons(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self.picker.selected_id() is not None)

    def values(self):
        media_id, _ = self.picker.values()
        return media_id, self.role.text().strip() or None


class EvidenceCreateDialog(QDialog):
    """Create dialog. Only title/annotation are free text the user may edit later."""

    def __init__(self, service, list_media, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建证据（原始内容创建后不可修改）")
        self.resize(880, 760)
        self.service, self.list_media = service, list_media
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for kind in service.KINDS:
            self.kind.addItem(kind, kind)
        self.title = QLineEdit()
        self.source_type = QComboBox()
        for source in service.SOURCE_TYPES:
            self.source_type.addItem(source, source)
        index = self.source_type.findData("MANUAL")
        self.source_type.setCurrentIndex(max(0, index))
        self.source_label = QLineEdit()
        self.source_reference = QLineEdit()
        self.source_reference.setPlaceholderText("来源定位信息；LINK 类型必须填写")
        self.captured_at = QLineEdit()
        self.captured_at.setPlaceholderText("留空=NULL；ISO 时间须含时区，例如 2026-09-28T21:14:00+08:00")
        form.addRow("类型 kind", self.kind)
        form.addRow("标题", self.title)
        form.addRow("来源类型 source_type", self.source_type)
        form.addRow("来源标签 source_label", self.source_label)
        form.addRow("来源引用 source_reference", self.source_reference)
        form.addRow("captured_at", self.captured_at)
        layout.addLayout(form)
        layout.addWidget(plain_label("原始内容 content_text（创建后只读，不会被任何界面修改）"))
        self.content = QTextEdit()
        self.content.setObjectName("evidenceContentInput")
        self.content.setMinimumHeight(120)
        layout.addWidget(self.content)
        layout.addWidget(plain_label("用户注释 annotation（与原始内容分开保存，可随时编辑）"))
        self.annotation = QTextEdit()
        self.annotation.setObjectName("evidenceAnnotationInput")
        self.annotation.setMaximumHeight(100)
        layout.addWidget(self.annotation)
        layout.addWidget(plain_label("附加已有媒体（可勾选多个；角色可留空）"))
        self.media = QTableWidget(0, len(CREATE_MEDIA_HEADERS))
        self.media.setObjectName("evidenceMediaSelector")
        self.media.setHorizontalHeaderLabels(CREATE_MEDIA_HEADERS)
        self.media.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.media.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.media.setMinimumHeight(150)
        layout.addWidget(self.media)
        row = QHBoxLayout()
        self.reload_button = QPushButton("刷新媒体列表")
        self.reload_button.clicked.connect(self.reload_media)
        row.addWidget(self.reload_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.reload_media()

    def reload_media(self):
        self.media.setRowCount(0)
        entries = self.list_media("")
        if entries is None:
            return
        for entry in entries:
            row = self.media.rowCount()
            self.media.insertRow(row)
            item = QTableWidgetItem(media_choice_text(entry))
            item.setData(ROLE, entry.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.media.setItem(row, 0, item)
            self.media.setItem(row, 1, QTableWidgetItem(""))
            identity = QTableWidgetItem(str(entry.id))
            identity.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.media.setItem(row, 2, identity)

    def values(self):
        # Imported lazily: the UI module must load even while the service is unassembled.
        from app.services.evidence import MediaInput
        title = self.title.text().strip()
        if not 1 <= len(title) <= 200:
            raise DomainError("标题去除首尾空格后长度必须为 1–200。")
        media = []
        for row in range(self.media.rowCount()):
            item = self.media.item(row, 0)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            role_item = self.media.item(row, 1)
            media.append(MediaInput(media_id=item.data(ROLE),
                                    role=(role_item.text().strip() or None) if role_item is not None else None))
        return dict(kind=self.kind.currentData(), title=title, source_type=self.source_type.currentData(),
                    source_label=self.source_label.text().strip() or None,
                    source_reference=self.source_reference.text().strip() or None,
                    captured_at=parse_captured(self.captured_at.text()),
                    content_text=self.content.toPlainText().strip() or None,
                    annotation=self.annotation.toPlainText().strip() or None, media=tuple(media))

    def accept(self):
        try:
            self.values()
        except (DomainError, ValueError) as exc:
            QMessageBox.warning(self, "请检查证据信息", str(exc))
            return
        super().accept()


class EvidenceMetadataDialog(QDialog):
    """Only title and annotation are editable; every other field is read-only."""

    def __init__(self, detail, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑证据元数据（仅标题与注释）")
        self.resize(720, 620)
        self.detail = detail
        evidence = detail.evidence
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit(evidence.title)
        form.addRow("标题（可编辑）", self.title)
        for label, value in [("类型 kind（只读）", evidence.kind),
                             ("来源类型 source_type（只读）", evidence.source_type),
                             ("来源标签 source_label（只读）", evidence.source_label or "—"),
                             ("来源引用 source_reference（只读）", evidence.source_reference or "—"),
                             ("captured_at（只读）", time_text(evidence.captured_at)),
                             ("created_at（只读）", time_text(evidence.created_at))]:
            form.addRow(label, plain_label(value))
        layout.addLayout(form)
        layout.addWidget(plain_label("原始内容 content_text（只读，不可修改）"))
        self.content = QTextEdit()
        self.content.setObjectName("evidenceMetadataContent")
        self.content.setReadOnly(True)
        self.content.setMaximumHeight(140)
        self.content.setPlainText(evidence.content_text or "")
        layout.addWidget(self.content)
        layout.addWidget(plain_label("用户注释 annotation（可编辑）"))
        self.annotation = QTextEdit()
        self.annotation.setObjectName("evidenceMetadataAnnotation")
        self.annotation.setPlainText(evidence.annotation or "")
        layout.addWidget(self.annotation)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self):
        title = self.title.text().strip()
        if not 1 <= len(title) <= 200:
            raise DomainError("标题去除首尾空格后长度必须为 1–200。")
        return dict(title=title, annotation=self.annotation.toPlainText().strip() or None)

    def accept(self):
        try:
            self.values()
        except (DomainError, ValueError) as exc:
            QMessageBox.warning(self, "请检查证据信息", str(exc))
            return
        super().accept()


class EvidenceDetailDialog(QDialog):
    """Raw evidence (read-only) is visually separated from the user annotation."""

    def __init__(self, detail, save_annotation=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Evidence Detail · 证据详情（原始内容只读）")
        self.resize(900, 780)
        self.detail = detail
        self.save_callback = save_annotation
        layout = QVBoxLayout(self)
        # Two visually separated sections: raw evidence is read-only, only the
        # user annotation below it can be edited.
        raw = QGroupBox("一、原始证据 · 只读（content_text、来源、captured_at、created_at、附件不可修改）")
        raw.setObjectName("evidenceRawGroup")
        raw_layout = QVBoxLayout(raw)
        form = QFormLayout()
        self.fields = {}
        for key, label in [("kind", "类型 kind"), ("source_type", "来源类型 source_type"),
                           ("source_label", "来源标签 source_label"),
                           ("source_reference", "来源引用 source_reference"),
                           ("captured_at", "captured_at UTC"), ("created_at", "created_at UTC"),
                           ("updated_at", "updated_at UTC"), ("lifecycle", "记录情况")]:
            self.fields[key] = plain_label("—")
            form.addRow(label, self.fields[key])
        raw_layout.addLayout(form)
        raw_layout.addWidget(plain_label("原始内容 content_text（只读）"))
        self.content = QTextEdit()
        self.content.setObjectName("evidenceDetailContent")
        self.content.setReadOnly(True)
        self.content.setMinimumHeight(130)
        raw_layout.addWidget(self.content)
        raw_layout.addWidget(plain_label("附件引用（只读；添加或移除请在 Evidence 面板中操作）"))
        self.attachments = make_attachment_table("evidenceDetailAttachments")
        self.attachments.setMinimumHeight(140)
        raw_layout.addWidget(self.attachments)
        layout.addWidget(raw)
        note = QGroupBox("二、用户注释 annotation · 可编辑（保存只更新 annotation，原始证据保持不变）")
        note.setObjectName("evidenceAnnotationGroup")
        note_layout = QVBoxLayout(note)
        self.annotation = QTextEdit()
        self.annotation.setObjectName("evidenceDetailAnnotation")
        self.annotation.setMinimumHeight(110)
        note_layout.addWidget(self.annotation)
        row = QHBoxLayout()
        self.save_button = QPushButton("保存注释")
        self.save_button.clicked.connect(self.save_annotation)
        self.save_button.setEnabled(save_annotation is not None)
        row.addWidget(self.save_button)
        row.addStretch(1)
        note_layout.addLayout(row)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.set_detail(detail)

    def set_detail(self, detail):
        self.detail = detail
        evidence = detail.evidence
        for label in self.fields.values():
            label.setText("—")
        self.fields["kind"].setText(evidence.kind)
        self.fields["source_type"].setText(evidence.source_type)
        self.fields["source_label"].setText(evidence.source_label or "—")
        self.fields["source_reference"].setText(evidence.source_reference or "—")
        self.fields["captured_at"].setText(time_text(evidence.captured_at))
        self.fields["created_at"].setText(time_text(evidence.created_at))
        self.fields["updated_at"].setText(time_text(evidence.updated_at))
        self.fields["lifecycle"].setText("已删除" if evidence.deleted_at else "有效记录")
        self.content.setPlainText(evidence.content_text or "")
        self.annotation.setPlainText(evidence.annotation or "")
        fill_attachment_table(self.attachments, detail.media)

    def save_annotation(self):
        if self.save_callback is None:
            return None
        updated = self.save_callback(self.annotation.toPlainText())
        if updated is not None:
            self.set_detail(updated)
        return updated
