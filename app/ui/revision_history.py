"""Read-only Revision History & Diff Viewer (Phase 14.3).

Entry points in Person / Event / Evidence / Relationship / Memory details open this
dialog. It never writes, never shows raw JSON or internal field names, degrades
gracefully for unknown snapshot versions, paginates newest-first and makes the
"Phase 14 has no backfill" boundary explicit. Restoring fields belongs to 14.4 and
is deliberately absent here.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.services.change_history import ENTITY_LABELS, LONG_TEXT_LIMIT, RevisionView
from app.ui.analysis_dialog import plain_label, read_only_text, utc_text
from app.ui.operations import run_operation


class LongTextDialog(QDialog):
    """Full text of one field change, opened only on demand."""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("revisionLongTextDialog")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        self.text = read_only_text()
        self.text.setObjectName("revisionLongText")
        self.text.setPlainText(text)
        layout.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class OperationDialog(QDialog):
    """All audit rows that belong to one user operation (grouped view)."""

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.setWindowTitle("同一操作的全部变更")
        self.setObjectName("revisionOperationDialog")
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(f"操作时间（本地时区）：{utc_text(operation.recorded_at)}"))
        self.list = QListWidget()
        self.list.setObjectName("revisionOperationRows")
        for row in operation.rows:
            item = QListWidgetItem(
                f"{row.entity_label} · {row.action_label} · 版本 #{row.revision_no}"
                f" · {str(row.entity_id)[:8]}")
            item.setToolTip(str(row.entity_id))
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class RevertPreviewDialog(QDialog):
    """Confirmation dialog for a controlled field revert (never a silent write)."""

    def __init__(self, preview, parent=None):
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("恢复字段预览")
        self.setObjectName("revertPreviewDialog")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            f"将把以下字段恢复为版本 #{preview.source_revision_no} 的值。"
            "这会作为一次新的业务操作记录，历史版本不会被修改。"))
        self.fields = QListWidget()
        self.fields.setObjectName("revertFieldList")
        for item in preview.fields:
            self.fields.addItem(QListWidgetItem(
                f"{item.label}：{item.current_text} → {item.target_text}"))
        layout.addWidget(self.fields, 1)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        confirm = buttons.addButton("确认恢复", QDialogButtonBox.ButtonRole.AcceptRole)
        confirm.setObjectName("revertConfirmButton")
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        layout.addWidget(buttons)


class RevisionHistoryDialog(QDialog):
    """Newest-first revision list plus a formatted field diff for the selection."""

    def __init__(self, services, workspace_id, entity_type, entity_id, *,
                 title=None, parent=None):
        super().__init__(parent)
        self.setObjectName("revisionHistoryDialog")
        self.services = services
        self.workspace_id = workspace_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.history = services.change_history
        self._views: list[RevisionView] = []
        self._related_views: list[RevisionView] = []
        self._offset = 0
        self._related_offset = 0
        self._operation_dialogs = []
        entity_label = ENTITY_LABELS.get(entity_type, entity_type)
        self.setWindowTitle(title or f"{entity_label} · 变更历史")
        self.resize(980, 700)
        layout = QVBoxLayout(self)
        self.header = plain_label(
            f"{entity_label} · {str(entity_id)[:8]} · 只读历史（事实层与原记录不会被修改）")
        self.header.setObjectName("revisionHeader")
        layout.addWidget(self.header)
        self.notice = plain_label("")
        self.notice.setObjectName("revisionNotice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("版本（新 → 旧）"))
        self.list = QListWidget()
        self.list.setObjectName("revisionList")
        self.list.itemSelectionChanged.connect(self._on_select)
        left_layout.addWidget(self.list, 1)
        self.load_more = QPushButton("加载更早的版本")
        self.load_more.setObjectName("revisionLoadMore")
        self.load_more.setEnabled(False)
        self.load_more.clicked.connect(self._load_more)
        left_layout.addWidget(self.load_more)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("字段差异（旧 → 新）"))
        self.diff = read_only_text()
        self.diff.setObjectName("revisionDiff")
        right_layout.addWidget(self.diff, 1)
        actions = QHBoxLayout()
        self.operation_button = QPushButton("查看同一操作的其它变更")
        self.operation_button.setObjectName("revisionOperationButton")
        self.operation_button.setEnabled(False)
        self.operation_button.clicked.connect(self._show_operation)
        actions.addWidget(self.operation_button)
        self.full_text_button = QPushButton("查看完整长文本")
        self.full_text_button.setObjectName("revisionFullTextButton")
        self.full_text_button.setEnabled(False)
        self.full_text_button.clicked.connect(self._show_full_text)
        actions.addWidget(self.full_text_button)
        self.revert_button = QPushButton("恢复此版本字段…")
        self.revert_button.setObjectName("revisionRevertButton")
        self.revert_button.setEnabled(False)
        self.revert_button.clicked.connect(self._start_revert)
        actions.addWidget(self.revert_button)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([360, 620])
        layout.addWidget(splitter, 1)

        # A relationship's appended state history rows have the relationship as their
        # aggregate parent; show them as their own read-only section.
        self.related_block = QWidget()
        related_layout = QVBoxLayout(self.related_block)
        related_layout.setContentsMargins(0, 0, 0, 0)
        related_layout.addWidget(QLabel("关系状态历史（追加记录）"))
        self.related_list = QListWidget()
        self.related_list.setObjectName("revisionRelatedList")
        self.related_list.itemSelectionChanged.connect(self._on_related_select)
        related_layout.addWidget(self.related_list, 1)
        self.related_load_more = QPushButton("加载更早的状态历史")
        self.related_load_more.setObjectName("revisionRelatedLoadMore")
        self.related_load_more.setEnabled(False)
        self.related_load_more.clicked.connect(self._load_more_related)
        related_layout.addWidget(self.related_load_more)
        layout.addWidget(self.related_block)
        self.related_block.setVisible(entity_type == "RELATIONSHIP")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    # ------------------------------------------------------------------ loading
    def refresh(self):
        page = run_operation(
            self,
            lambda: self.history.entity_history(self.workspace_id, self.entity_type,
                                                self.entity_id, limit=50, offset=0),
            context="revision history")
        if page is None:
            return None
        self._offset = 0
        self._views = []
        self.list.clear()
        self._append_page(page)
        self._offset = len(page.revisions)
        if page.legacy_hint:
            self.notice.setText(page.legacy_hint)
        elif not page.has_history:
            self.notice.setText("此记录没有可追溯的历史。Phase 14 之前创建的数据不会补录，"
                                "之后的修改才会出现在这里。")
        else:
            self.notice.setText("")
        if self.entity_type == "RELATIONSHIP":
            related = run_operation(
                self,
                lambda: self.history.parent_history(self.workspace_id, "RELATIONSHIP",
                                                    self.entity_id, limit=50, offset=0),
                context="relationship history")
            if related is not None:
                self._related_offset = 0
                self._related_views = []
                self.related_list.clear()
                self._append_related_page(related)
                self._related_offset = len(related.revisions)
        return page

    def _append_page(self, page):
        self._views.extend(page.revisions)
        for view in page.revisions:
            item = QListWidgetItem(
                f"#{view.revision_no} · {utc_text(view.entry.recorded_at)} · "
                f"{view.action_label} · {view.summary}")
            item.setToolTip(view.summary)
            self.list.addItem(item)
        self.load_more.setEnabled(page.has_more)
        if self._views and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def _append_related_page(self, page):
        self._related_views.extend(page.revisions)
        for view in page.revisions:
            item = QListWidgetItem(
                f"#{view.revision_no} · {utc_text(view.entry.recorded_at)} · "
                f"{view.action_label} · {view.summary}")
            self.related_list.addItem(item)
        self.related_load_more.setEnabled(page.has_more)
        if self._related_views and self.related_list.currentRow() < 0:
            self.related_list.setCurrentRow(0)

    def _load_more(self):
        page = run_operation(
            self,
            lambda: self.history.entity_history(self.workspace_id, self.entity_type,
                                                self.entity_id, limit=50,
                                                offset=self._offset),
            context="revision history page")
        if page is None:
            return None
        self._append_page(page)
        self._offset += len(page.revisions)
        return page

    def _load_more_related(self):
        page = run_operation(
            self,
            lambda: self.history.parent_history(self.workspace_id, "RELATIONSHIP",
                                                self.entity_id, limit=50,
                                                offset=self._related_offset),
            context="relationship history page")
        if page is None:
            return None
        self._append_related_page(page)
        self._related_offset += len(page.revisions)
        return page

    # ------------------------------------------------------------- selection
    def _current_view(self) -> RevisionView | None:
        row = self.list.currentRow()
        if 0 <= row < len(self._views):
            return self._views[row]
        return None

    def current_view(self) -> RevisionView | None:
        return self._current_view()

    def _on_select(self):
        view = self._current_view()
        self._render(view)

    def _on_related_select(self):
        row = self.related_list.currentRow()
        if 0 <= row < len(self._related_views):
            self._render(self._related_views[row])

    def _render(self, view: RevisionView | None):
        self.full_text_button.setEnabled(False)
        self.operation_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self._revert_preview = None
        if view is None:
            self.diff.setPlainText("选择左侧版本查看字段差异。")
            return
        entry = view.entry
        lines = [
            f"版本 #{view.revision_no} · {view.action_label}",
            f"记录时间（本地时区）：{utc_text(entry.recorded_at)}",
            f"实体：{ENTITY_LABELS.get(entry.entity_type, entry.entity_type)}",
        ]
        if view.snapshot_unavailable:
            lines.append("")
            lines.append("此版本的快照无法解析（旧版本或损坏数据）；仅显示审计元数据，"
                         "不能生成差异。")
        elif view.changes:
            lines.append("")
            lines.append("字段差异（旧 → 新）：")
            long_found = False
            for change in view.changes:
                left = _truncate(change.before_text)
                right = _truncate(change.after_text)
                if len(change.before_text) > LONG_TEXT_LIMIT or \
                        len(change.after_text) > LONG_TEXT_LIMIT:
                    long_found = True
                lines.append(f"• {change.label}")
                lines.append(f"    旧：{left}")
                lines.append(f"    新：{right}")
            self.full_text_button.setEnabled(long_found)
        else:
            lines.append("")
            lines.append("该次操作没有业务字段差异（例如生命周期动作）。")
        self.diff.setPlainText("\n".join(lines))
        self.operation_button.setEnabled(True)
        self._refresh_revert_availability(view)

    def _refresh_revert_availability(self, view: RevisionView):
        """Enable the revert button only for a whitelisted, currently valid target."""
        service = getattr(self.services, "change_revert", None)
        if service is None:
            return
        try:
            preview = service.preview(self.workspace_id, view.entry.entity_type,
                                      view.entry.entity_id, view.revision_no)
        except DomainError as exc:
            self.revert_button.setToolTip(str(exc))
            return
        self._revert_preview = preview
        if preview.can_revert and preview.has_changes:
            self.revert_button.setEnabled(True)
            self.revert_button.setToolTip("")
        else:
            self.revert_button.setToolTip(preview.reason)

    def _start_revert(self):
        preview = getattr(self, "_revert_preview", None)
        if preview is None or not preview.can_revert or not preview.has_changes:
            return None
        dialog = RevertPreviewDialog(preview, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        result = run_operation(
            self,
            lambda: self.services.change_revert.revert(
                self.workspace_id, preview.entity_type, preview.entity_id,
                preview.source_revision_no, confirmed=True),
            context="controlled revert")
        if result is None:
            return None
        self.refresh()
        self.notice.setText(result.message)
        return result

    def _show_operation(self):
        view = self._current_view()
        if view is None:
            return None
        operation = run_operation(
            self,
            lambda: self.history.operation(self.workspace_id, view.entry.operation_id),
            context="operation group")
        if operation is None:
            return None
        dialog = OperationDialog(operation, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._operation_dialogs.append(dialog)
        dialog.finished.connect(lambda _: self._operation_dialogs.remove(dialog)
                                if dialog in self._operation_dialogs else None)
        dialog.show()
        return dialog

    def _show_full_text(self):
        view = self._current_view()
        if view is None:
            return None
        chunks = []
        for change in view.changes:
            chunks.append(f"【{change.label}】\n旧：{change.before_text}\n新：{change.after_text}")
        text = "\n\n".join(chunks) or "没有可用文本。"
        dialog = LongTextDialog(f"长文本 · 版本 #{view.revision_no}", text, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog


def _truncate(text: str) -> str:
    if len(text) <= LONG_TEXT_LIMIT:
        return text
    return text[:LONG_TEXT_LIMIT] + f"…（共 {len(text)} 字符，点击“查看完整长文本”）"


def open_revision_history(parent, services, workspace_id, entity_type, entity_id, *,
                          title=None):
    """Open one non-modal read-only history dialog and keep a reference on parent."""
    dialog = RevisionHistoryDialog(services, workspace_id, entity_type, entity_id,
                                   title=title, parent=parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    refs = getattr(parent, "_revision_history_dialogs", None)
    if refs is None:
        refs = []
        parent._revision_history_dialogs = refs
    refs.append(dialog)
    dialog.finished.connect(lambda _: refs.remove(dialog) if dialog in refs else None)
    dialog.show()
    return dialog


__all__ = ["RevisionHistoryDialog", "OperationDialog", "LongTextDialog",
           "RevertPreviewDialog", "open_revision_history"]
