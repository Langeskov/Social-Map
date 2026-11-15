"""Plain Qt controls for immutable relationship identities and state history."""
from datetime import datetime, timezone
import logging
from uuid import UUID
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.analysis_dialog import ANALYZE_LABELS, analysis_center
from app.ui.operations import run_operation as _run_operation

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole


def utc_text(value):
    return "—" if value is None else value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_effective(text):
    if not text.strip():
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except ValueError:
        raise DomainError("生效时间必须包含时区，例如 2026-10-03T18:00:00+08:00；留空表示现在。") from None


def make_state_combo(state="ACTIVE"):
    combo = QComboBox()
    for key, label in [("ACTIVE", "正常"), ("PAUSED", "暂停"), ("ENDED", "结束")]:
        combo.addItem(f"{key} · {label}", key)
    index = combo.findData(state)
    combo.setCurrentIndex(max(0, index))
    return combo

class StateDialog(QDialog):
    def __init__(self, state="ACTIVE", parent=None):
        super().__init__(parent)
        self.setWindowTitle("追加关系状态（不修改关系身份）")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.state = make_state_combo(state)
        self.effective = QLineEdit()
        self.effective.setPlaceholderText("留空=现在；ISO 时间须含 +08:00 或 Z")
        self.note = QTextEdit()
        form.addRow("状态", self.state)
        form.addRow("实际生效时间", self.effective)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        layout.addWidget(QLabel("状态写入新 History。ENDED 不会删除关系；未来生效记录不提前改变当前状态。"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self):
        return dict(state=self.state.currentData(), effective_at=parse_effective(self.effective.text()),
                    note=self.note.toPlainText().strip() or None)

    def accept(self):
        try:
            self.values()
        except DomainError as exc:
            QMessageBox.warning(self, "请检查生效时间", str(exc))
            return
        super().accept()

class RelationshipDialog(QDialog):
    def __init__(self, people, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加关系")
        self.resize(520, 450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.other = QComboBox()
        for person in people:
            self.other.addItem(f"{person.display_name} · {str(person.id)[:8]}", str(person.id))
        self.kind = QLineEdit()
        self.kind.setPlaceholderText("自定义类型；创建后不可改，大小写敏感")
        self.direction = QComboBox()
        self.direction.addItem("无向", "UNDIRECTED")
        self.direction.addItem("本人 → 对方", "OUTGOING")
        self.direction.addItem("对方 → 本人", "INCOMING")
        self.state = make_state_combo()
        self.effective = QLineEdit()
        self.effective.setPlaceholderText("留空=现在；ISO 时间须含时区")
        self.note = QTextEdit()
        form.addRow("对方人物", self.other)
        form.addRow("关系类型", self.kind)
        form.addRow("方向", self.direction)
        form.addRow("初始状态", self.state)
        form.addRow("实际生效时间", self.effective)
        form.addRow("历史备注", self.note)
        layout.addLayout(form)
        layout.addWidget(QLabel("类型、方向和端点创建后不可改。录入错误请删除错误记录，再创建正确关系。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if not people:
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            layout.addWidget(QLabel("请先在当前 Workspace 创建另一位人物。"))
        layout.addWidget(buttons)

    def values(self):
        if self.other.currentData() is None:
            raise DomainError("请选择当前 Workspace 的另一位人物。")
        kind = self.kind.text().strip()
        if not 1 <= len(kind) <= 200:
            raise DomainError("关系类型去除首尾空格后长度必须为 1–200。")
        return dict(other_id=UUID(self.other.currentData()), relationship_type=kind,
                    orientation=self.direction.currentData(), initial_state=self.state.currentData(),
                    effective_at=parse_effective(self.effective.text()), note=self.note.toPlainText().strip() or None)

    def accept(self):
        try:
            self.values()
        except DomainError as exc:
            QMessageBox.warning(self, "请检查关系信息", str(exc))
            return
        super().accept()

class HistoryDialog(QDialog):
    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关系状态历史 · 生效时间 / 写入时间分开显示")
        self.resize(1000, 450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("顺序：effective_at → created_at → UUID。历史只追加；晚于当前时间的记录尚未生效。"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["生效时间 UTC", "状态", "备注", "写入时间 UTC", "UUID（同时间决胜）"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate([utc_text(entry.effective_at), entry.state, entry.note or "",
                                            utc_text(entry.created_at), str(entry.id)]):
                self.table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(self.table)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

class RelationshipPanel(QWidget):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self.service = getattr(services, "relationships", None)
        self.workspace_id = self.person_id = None
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(QLabel("Relationships · 关系（身份不可编辑）"))
        self.include_deleted = QCheckBox("显示已删除记录 / 已归档端点")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        layout.addWidget(self.include_deleted)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("relationshipList")
        self.table.setHorizontalHeaderLabels(["对方", "类型", "方向", "当前状态", "记录建立 UTC", "状态生效 UTC", "记录情况"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(200)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.add_button = self.button(row, "添加关系", self.add_relationship)
        self.state_button = self.button(row, "修改状态", self.change_state)
        self.history_button = self.button(row, "查看 History", self.show_history)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.delete_button = self.button(row, "删除错误记录", self.delete_relationship)
        self.restore_button = self.button(row, "恢复记录（保持状态）", self.restore_relationship)
        self.refresh_button = self.button(row, "刷新状态", self.refresh)
        layout.addLayout(row)
        row = QHBoxLayout()
        # Phase 8: the model is called only when this button is clicked; selecting a
        # relationship never triggers anything.
        self.analyze_button = self.button(row, ANALYZE_LABELS["RELATIONSHIP"], self.analyze_relationship)
        layout.addLayout(row)
        self.update_buttons()

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        """Delegates to the shared UI operation boundary (``app.ui.operations``).

        Consolidated in Phase 10: this panel previously kept its own copy that also
        omitted OSError from the known-failure tuple and used a slightly different
        unknown-error text. Both now match every other screen.
        """
        return _run_operation(self, callback, context="Relationship UI", title="关系操作未完成")

    def set_person(self, workspace_id, person_id):
        self.workspace_id, self.person_id = workspace_id, person_id
        self.table.clearContents()
        self.table.setRowCount(0)
        self.refresh()

    def selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(ROLE) if item is not None else None

    def update_buttons(self):
        available = self.service is not None and self.person_id is not None
        view = self.selected()
        self.add_button.setEnabled(available)
        self.refresh_button.setEnabled(available)
        self.state_button.setEnabled(bool(view and view.relationship.deleted_at is None and view.endpoints_active))
        self.history_button.setEnabled(view is not None)
        self.delete_button.setEnabled(bool(view and view.relationship.deleted_at is None))
        self.restore_button.setEnabled(bool(view and view.relationship.deleted_at is not None and view.endpoints_active))
        self.analyze_button.setEnabled(view is not None)

    def analyze_relationship(self):
        """Phase 8 entry: options dialog only; the run itself starts on confirm."""
        view = self.selected()
        if view is None:
            return None
        return analysis_center(self).analyze_target(
            "RELATIONSHIP", view.relationship.id, workspace_id=self.workspace_id,
            target_label=f"{view.relationship.relationship_type} · {view.other(self.person_id).display_name}")

    def refresh(self):
        if self.service is None or self.person_id is None:
            self.table.setRowCount(0)
            self.update_buttons()
            return
        previous = self.selected()
        selected_id = previous.relationship.id if previous else None
        def load():
            views = self.service.list_by_person(self.workspace_id, self.person_id, include_deleted=self.include_deleted.isChecked())
            self.table.setRowCount(0)
            target = 0
            for view in views:
                entity = view.relationship
                direction = "无向" if entity.directionality == "UNDIRECTED" else (
                    "本人 → 对方" if entity.person_a_id == self.person_id else "对方 → 本人")
                lifecycle = "已删除" if entity.deleted_at is not None else "有效记录"
                if not view.endpoints_active:
                    lifecycle += " · 端点已归档"
                values = [view.other(self.person_id).display_name, entity.relationship_type, direction,
                          view.current_state or "尚未生效", utc_text(entity.created_at),
                          utc_text(view.current_history.effective_at if view.current_history else None), lifecycle]
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(ROLE, view)
                    self.table.setItem(row, column, item)
                if entity.id == selected_id:
                    target = row
            if views:
                self.table.selectRow(target)
            self.update_buttons()
        self.run_operation(load)

    def add_from_values(self, values):
        values = dict(values)
        other = values.pop("other_id")
        orientation = values.pop("orientation")
        if orientation not in {"UNDIRECTED", "OUTGOING", "INCOMING"}:
            raise DomainError("请选择明确的关系方向。")
        a, b = (other, self.person_id) if orientation == "INCOMING" else (self.person_id, other)
        direction = "UNDIRECTED" if orientation == "UNDIRECTED" else "DIRECTED"
        entity = self.service.create(self.workspace_id, a, b, directionality=direction, **values)
        self.refresh()
        return entity

    def add_relationship(self):
        if self.person_id is None or self.service is None:
            return
        def add():
            people = [person for person in self.services.people.list(self.workspace_id) if person.id != self.person_id]
            dialog = RelationshipDialog(people, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.add_from_values(dialog.values())
        self.run_operation(add)

    def change_state(self):
        view = self.selected()
        if view is not None:
            def change():
                dialog = StateDialog(view.current_state or "ACTIVE", self)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    self.service.add_history(self.workspace_id, view.relationship.id, **dialog.values())
                    self.refresh()
            self.run_operation(change)

    def delete_relationship(self):
        view = self.selected()
        if view is None:
            return
        if QMessageBox.question(self, "删除错误关系", "这是删除错误记录，不是结束关系；History 会保留。结束关系请改为 ENDED。继续？") != QMessageBox.StandardButton.Yes:
            return
        def delete():
            self.service.soft_delete(self.workspace_id, view.relationship.id)
            self.refresh()
        self.run_operation(delete)

    def restore_relationship(self):
        view = self.selected()
        if view is not None:
            def restore():
                self.service.restore(self.workspace_id, view.relationship.id)
                self.refresh()
            self.run_operation(restore)

    def show_history(self):
        view = self.selected()
        if view is None:
            return None
        def show():
            entries = self.service.history(self.workspace_id, view.relationship.id, include_deleted=True)
            dialog = HistoryDialog(entries, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)
