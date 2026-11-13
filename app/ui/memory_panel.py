"""Memory UI: a reusable list panel plus its two small editors.

Phase 9 rules encoded here:

* **nothing is generated.** There is no ``suggest``/``extract``/``refresh`` button:
  every Memory in this module is typed by the user or explicitly confirmed from a
  finished Analysis run, and a Memory never rewrites a Person, Relationship, Event
  or Evidence.
* **the panel is scope-only.** ``set_person(workspace_id, person_id)`` shows that
  person's memories; ``set_person(workspace_id, None)`` shows the Workspace-level
  ones (``person_id IS NULL``). Both the Person Detail panel and the Workspace
  dialog use this one widget, so the listing rules stay in one place.
* **content is data.** Memory text is only ever put into ``QPlainTextEdit`` /
  ``QTableWidgetItem``; it is never logged, never a window title and never a
  ``QMessageBox`` argument.
* **two display switches, both off by default**: an expired memory
  (``valid_from`` in the future or ``valid_until`` in the past) and a soft-deleted
  one are hidden until the matching checkbox is ticked.
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.analysis_dialog import parse_optional_datetime, plain_label, time_text

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole

#: The frozen order of the three kinds; the service validates the values again.
MEMORY_KINDS = (("FACT", "FACT · 事实"), ("PREFERENCE", "PREFERENCE · 偏好"),
                ("CONTEXT", "CONTEXT · 背景"))
KIND_LABELS = dict(MEMORY_KINDS)
MEMORY_HEADERS = ["内容", "类型", "来源", "有效期（UTC）", "确认时间（UTC）"]
SOURCE_LABELS = {"MANUAL": "MANUAL · 手工录入", "ANALYSIS": "ANALYSIS · 分析确认"}
VALIDITY_HINT = ("有效期必须带时区，例如 2026-10-03T18:00:00+08:00；留空表示不设边界。")


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Memory UI", title="Memory 操作未完成")


def kind_text(kind) -> str:
    return KIND_LABELS.get(kind, str(kind or "—"))


def source_text(memory) -> str:
    label = SOURCE_LABELS.get(memory.source_type, str(memory.source_type or "—"))
    if memory.source_type == "ANALYSIS" and memory.source_analysis_id is not None:
        label += f" · {str(memory.source_analysis_id)[:8]}"
    return label


def validity_text(memory) -> str:
    parts = []
    if memory.valid_from is not None:
        parts.append("起 " + time_text(memory.valid_from))
    if memory.valid_until is not None:
        parts.append("止 " + time_text(memory.valid_until))
    return " · ".join(parts) if parts else "不限"


def current_switch_text(now=None) -> str:
    return "只显示当前有效的 Memory；已过期与已删除的默认隐藏。"


class MemoryEditDialog(QDialog):
    """Create or edit one Memory.

    The dialog writes nothing: it validates and returns the keyword arguments the
    caller passes to ``MemoryService.create`` / ``update``. ``person_id`` is only
    offered when ``allow_person`` is true, and an empty choice means
    ``person_id IS NULL`` (Workspace level).
    """

    def __init__(self, parent=None, *, memory=None, allow_person=True, person_id=None, people=(),
                 valid_from=None, valid_until=None, title=None):
        super().__init__(parent)
        self.setWindowTitle(title or ("编辑 Memory" if memory is not None else "新增 Memory"))
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        note = plain_label(
            "Memory 是你确认过的内容，不是模型自动生成的事实；保存与修改都不会改变人物、"
            "关系、事件或证据。" + ("" if memory is None else "来源（MANUAL/ANALYSIS）永不改变，"
                                                     "只删除或恢复，不重写来源。"))
        note.setObjectName("subtitle")
        layout.addWidget(note)
        form = QFormLayout()
        self.content = QPlainTextEdit()
        self.content.setObjectName("memoryEditContent")
        self.content.setPlaceholderText("内容（纯文本，不会写入日志）")
        self.content.setMinimumHeight(170)
        self.content.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        form.addRow("内容", self.content)
        self.kind = QComboBox()
        self.kind.setObjectName("memoryEditKind")
        for value, label in MEMORY_KINDS:
            self.kind.addItem(label, value)
        form.addRow("类型", self.kind)
        self.person = QComboBox()
        self.person.setObjectName("memoryEditPerson")
        self.person.addItem("（空 = Workspace 级记忆）", None)
        for person in people or ():
            self.person.addItem(f"{person.display_name} · {str(person.id)[:8]}", person.id)
        if allow_person:
            form.addRow("绑定人物", self.person)
        else:
            self.person.hide()
        self.valid_from = QPlainTextEdit()
        self.valid_from.setObjectName("memoryEditValidFrom")
        self.valid_from.setPlaceholderText("可留空；例如 2026-10-03T18:00:00+08:00")
        self.valid_from.setMaximumHeight(46)
        self.valid_until = QPlainTextEdit()
        self.valid_until.setObjectName("memoryEditValidUntil")
        self.valid_until.setPlaceholderText("可留空；必须晚于起点")
        self.valid_until.setMaximumHeight(46)
        if memory is not None or valid_from is not None or valid_until is not None:
            form.addRow("valid_from", self.valid_from)
            form.addRow("valid_until", self.valid_until)
        else:
            self.valid_from.hide()
            self.valid_until.hide()
        layout.addLayout(form)
        layout.addWidget(plain_label(VALIDITY_HINT))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self._prefill(memory=memory, person_id=person_id, valid_from=valid_from,
                      valid_until=valid_until)

    def _prefill(self, *, memory, person_id, valid_from, valid_until):
        if memory is not None:
            self.content.setPlainText(memory.content)
            self.select_kind(memory.kind)
            person_id = memory.person_id
            valid_from = memory.valid_from
            valid_until = memory.valid_until
        self.select_person(person_id)
        self.valid_from.setPlainText("" if valid_from is None else valid_from.isoformat())
        self.valid_until.setPlainText("" if valid_until is None else valid_until.isoformat())

    def select_person(self, person_id):
        """Choose the binding by comparing the stored value itself (never ``findData``)."""
        for index in range(self.person.count()):
            if self.person.itemData(index) == person_id:
                self.person.setCurrentIndex(index)
                return index
        self.person.setCurrentIndex(0)
        return -1

    def select_kind(self, kind):
        for index in range(self.kind.count()):
            if self.kind.itemData(index) == kind:
                self.kind.setCurrentIndex(index)
                return index
        return -1

    # -------------------------------------------------------------------- values
    def current_kind(self):
        return self.kind.currentData()

    def boundaries(self):
        """``(valid_from, valid_until)`` with naive input refused and order checked."""
        start = parse_optional_datetime(self.valid_from.toPlainText(), field="valid_from")
        end = parse_optional_datetime(self.valid_until.toPlainText(), field="valid_until")
        if start is not None and end is not None and end < start:
            raise DomainError("valid_until 不能早于 valid_from。")
        return start, end

    def values(self) -> dict:
        content = self.content.toPlainText().strip()
        if not content:
            raise DomainError("Memory 内容不能为空。")
        start, end = self.boundaries()
        return {"content": content, "kind": self.current_kind(),
                "person_id": self.person.currentData(), "valid_from": start, "valid_until": end}

    def accept(self):
        try:
            self.values()
        except (DomainError, ValueError) as exc:
            failure = exc if isinstance(exc, DomainError) else DomainError(str(exc))
            QMessageBox.warning(self, "请检查 Memory 内容", str(failure))
            return None
        return super().accept()


class MemoryPanel(QWidget):
    """List / add / edit / soft-delete / restore the memories of one scope.

    ``set_person(workspace_id, person_id)`` binds the panel to a person,
    ``set_person(workspace_id, None)`` to the Workspace-level memories. The panel
    itself performs no automatic generation and never writes a fact table.
    """

    def __init__(self, services, parent=None, *, title="Memory · 记忆（只有你确认的内容）",
                 allow_person=True, name="memoryPanel"):
        super().__init__(parent)
        self.setObjectName(name)
        self.services = services
        self.service = getattr(services, "memories", None)
        self.allow_person = allow_person
        self.workspace_id = None
        self.person_id = None
        self.memories = []
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(plain_label(title))
        switches = QHBoxLayout()
        self.include_expired = QCheckBox("显示过期")
        self.include_expired.setObjectName("memoryIncludeExpired")
        self.include_expired.toggled.connect(lambda _: self.refresh())
        self.include_deleted = QCheckBox("显示已删除")
        self.include_deleted.setObjectName("memoryIncludeDeleted")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        switches.addWidget(self.include_expired)
        switches.addWidget(self.include_deleted)
        switches.addStretch()
        layout.addLayout(switches)
        self.table = QTableWidget(0, len(MEMORY_HEADERS))
        self.table.setObjectName("memoryList")
        self.table.setHorizontalHeaderLabels(MEMORY_HEADERS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(170)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _: self.edit_memory())
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.add_button = self.button(row, "新增 Memory", self.add_memory)
        self.edit_button = self.button(row, "编辑", self.edit_memory)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.delete_button = self.button(row, "删除", self.delete_memory)
        self.restore_button = self.button(row, "恢复", self.restore_memory)
        self.refresh_button = self.button(row, "刷新", self.refresh)
        layout.addLayout(row)
        self.status = plain_label()
        self.status.setObjectName("memoryStatus")
        layout.addWidget(self.status)
        self.update_buttons()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        return run_operation(self, callback)

    # ------------------------------------------------------------------- scope
    def set_person(self, workspace_id, person_id):
        """Bind the panel to one scope; ``person_id=None`` means Workspace level."""
        self.workspace_id = workspace_id
        self.person_id = person_id
        return self.refresh()

    def activate(self):
        """Page activation: re-read the facts (never generates a Memory)."""
        return self.refresh()

    def scope_text(self):
        if self.person_id is None:
            return "Workspace 级记忆（person_id 为空）"
        return f"人物记忆 · {str(self.person_id)[:8]}"

    # ------------------------------------------------------------------ listing
    def selected_memory(self):
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return item.data(ROLE) if item is not None else None

    def update_buttons(self):
        available = self.service is not None and self.workspace_id is not None
        memory = self.selected_memory()
        deleted = memory is not None and memory.deleted_at is not None
        self.add_button.setEnabled(bool(available))
        self.refresh_button.setEnabled(bool(available))
        self.edit_button.setEnabled(bool(available and memory is not None and not deleted))
        self.delete_button.setEnabled(bool(available and memory is not None and not deleted))
        self.restore_button.setEnabled(bool(available and memory is not None and deleted))

    def refresh(self, selected_id=None):
        if isinstance(selected_id, bool):
            selected_id = None
        if selected_id is None:
            memory = self.selected_memory()
            selected_id = memory.id if memory is not None else None
        self.memories = []
        self.table.setRowCount(0)
        self.update_buttons()
        if self.service is None:
            self.status.setText("Memory 服务尚未装配：记忆列表不可用。")
            return []
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace。")
            return []

        def load():
            self.memories = list(self.service.list(
                self.workspace_id, person_id=self.person_id,
                include_deleted=self.include_deleted.isChecked(),
                include_expired=self.include_expired.isChecked()))
            self.fill_table(selected_id)
            return self.memories
        return self.run_operation(load)

    def fill_table(self, selected_id=None):
        """Render the last read; the table stays read-only (Memory is never edited here)."""
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for memory in self.memories:
                row = self.table.rowCount()
                self.table.insertRow(row)
                values = [memory.content, kind_text(memory.kind), source_text(memory),
                          validity_text(memory), time_text(memory.confirmed_at)]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if memory.deleted_at is not None:
                        item.setText("已删除 · " + item.text())
                    if column == 0:
                        item.setData(ROLE, memory)
                    self.table.setItem(row, column, item)
            if self.memories:
                index = next((row for row in range(self.table.rowCount())
                              if self.table.item(row, 0).data(ROLE).id == selected_id), 0)
                self.table.selectRow(index)
        finally:
            self.table.blockSignals(False)
        self.update_buttons()
        self.status.setText(self.status_text())
        return self.memories

    def status_text(self):
        deleted = sum(1 for memory in self.memories if memory.deleted_at is not None)
        return (f"{self.scope_text()} · {len(self.memories)} 条"
                + (f"（含已删除 {deleted}）" if deleted else "")
                + (" · 含过期／未生效" if self.include_expired.isChecked() else "")
                + " · 本页只读取你确认过的 Memory，不生成、不自动修改事实。")

    # ------------------------------------------------------------------- rows
    def row_values(self, memory):
        """The five displayed cells (used by the tests and by ``fill_table``)."""
        return [memory.content, kind_text(memory.kind), source_text(memory), validity_text(memory),
                time_text(memory.confirmed_at)]

    # ---------------------------------------------------------------- operations
    def list_people(self):
        """The bindable people of the current Workspace and nothing else."""
        if self.workspace_id is None:
            return []
        lister = getattr(self.services, "people", None)
        if lister is None:
            return []
        return list(lister.list(self.workspace_id))

    def create_memory(self, values):
        """One write path shared by the panel, the dialog and the tests (MANUAL)."""
        def create():
            memory = self.service.create(self.workspace_id, values["content"], kind=values["kind"],
                                         person_id=values.get("person_id") or self.person_id,
                                         valid_from=values.get("valid_from"),
                                         valid_until=values.get("valid_until"))
            self.refresh(memory.id)
            return memory
        return self.run_operation(create)

    def update_memory(self, memory, values):
        def update():
            saved = self.service.update(self.workspace_id, memory.id, content=values["content"],
                                        kind=values["kind"], person_id=values.get("person_id"),
                                        valid_from=values.get("valid_from"),
                                        valid_until=values.get("valid_until"),
                                        clear_valid_from=values.get("valid_from") is None,
                                        clear_valid_until=values.get("valid_until") is None)
            self.refresh(saved.id)
            return saved
        return self.run_operation(update)

    def add_memory(self):
        if self.service is None or self.workspace_id is None:
            return None
        def create():
            dialog = MemoryEditDialog(self, allow_person=self.allow_person, people=self.list_people(),
                                      person_id=self.person_id, valid_from=None, valid_until=None)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                return self.create_memory(dialog.values())
            return None
        return self.run_operation(create)

    def edit_memory(self):
        memory = self.selected_memory()
        if self.service is None or self.workspace_id is None or memory is None:
            return None
        if memory.deleted_at is not None:
            self.status.setText("已删除的 Memory 只能恢复，不能编辑。")
            return None
        def edit():
            dialog = MemoryEditDialog(self, memory=memory, allow_person=self.allow_person,
                                      people=self.list_people())
            if dialog.exec() == QDialog.DialogCode.Accepted:
                return self.update_memory(memory, dialog.values())
            return None
        return self.run_operation(edit)

    def delete_memory(self):
        memory = self.selected_memory()
        if self.service is None or self.workspace_id is None or memory is None:
            return None
        if QMessageBox.question(self, "删除 Memory",
                                "将软删除这条 Memory（内容保留，可恢复）；"
                                "人物、关系、事件与证据都不会被修改。是否继续？"
                                ) != QMessageBox.StandardButton.Yes:
            return None
        def remove():
            saved = self.service.soft_delete(self.workspace_id, memory.id)
            self.refresh(memory.id)
            return saved
        return self.run_operation(remove)

    def restore_memory(self):
        memory = self.selected_memory()
        if self.service is None or self.workspace_id is None or memory is None:
            return None
        def restore():
            saved = self.service.restore(self.workspace_id, memory.id)
            self.refresh(memory.id)
            return saved
        return self.run_operation(restore)


class MemoryWorkspaceDialog(QDialog):
    """Workspace-level memories (``person_id IS NULL``) in their own window.

    The Person page opens this: a Person panel can only bind a Memory to its own
    person, so the Workspace scope gets a separate, clearly labelled list with the
    same panel (and therefore the same display rules).
    """

    def __init__(self, services, workspace_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workspace 记忆 · 不与人物绑定")
        self.resize(900, 620)
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            "这些 Memory 的 person_id 为空，属于整个 Workspace；它们不会自动进入任何分析，"
            "只有你在分析选项里明确要求时才会被读取。"))
        self.panel = MemoryPanel(services, self, allow_person=False, name="workspaceMemoryPanel",
                                 title="Workspace 级 Memory（person_id 为空）")
        layout.addWidget(self.panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self.panel.set_person(workspace_id, None)

    def activate(self):
        return self.panel.refresh()


__all__ = ["KIND_LABELS", "MEMORY_HEADERS", "MEMORY_KINDS", "MemoryEditDialog", "MemoryPanel",
           "MemoryWorkspaceDialog", "SOURCE_LABELS", "VALIDITY_HINT", "current_switch_text",
           "kind_text", "run_operation", "source_text", "validity_text"]
