"""Event forms: factual/observed records, explicit time, UUID participants."""
from datetime import date, datetime, timezone
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHeaderView, QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout)
from app.domain.errors import DomainError

ROLE = Qt.ItemDataRole.UserRole


def parse_datetime(text):
    if not text.strip():
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except ValueError:
        raise DomainError("时间须为包含时区的 ISO 时间，例如 2026-10-03T18:00:00+08:00。") from None


class EventDialog(QDialog):
    def __init__(self, people, detail=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑事件" if detail else "新建事件")
        self.resize(660, 680)
        event = detail.event if detail else None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit(getattr(event, "title", ""))
        self.event_type = QLineEdit(getattr(event, "event_type", ""))
        self.record_kind = QComboBox()
        self.record_kind.addItem("FACT · 事实", "FACT")
        self.record_kind.addItem("OBSERVATION · 观察", "OBSERVATION")
        if event:
            self.record_kind.setCurrentIndex(self.record_kind.findData(event.record_kind))
        self.occurred_on = QLineEdit()
        self.started_at = QLineEdit()
        self.ended_at = QLineEdit()
        self.occurred_on.setPlaceholderText("YYYY-MM-DD；与开始时间严格二选一")
        self.started_at.setPlaceholderText("ISO 时间须含时区；与日期严格二选一")
        self.ended_at.setPlaceholderText("可选；须含时区且不早于开始时间")
        for name in ("occurred_on", "started_at", "ended_at"):
            value = getattr(event, name, None)
            if value is not None:
                getattr(self, name).setText(value.isoformat())
        self.location = QLineEdit(getattr(event, "location", None) or "")
        self.description = QTextEdit()
        self.description.setPlainText(getattr(event, "description", None) or "")
        self.description.setMaximumHeight(100)
        for label, widget in [("标题", self.title), ("类型（自定义）", self.event_type),
                              ("记录性质", self.record_kind), ("发生日期", self.occurred_on),
                              ("开始时间", self.started_at), ("结束时间", self.ended_at),
                              ("地点", self.location), ("描述", self.description)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        self.participants = QTableWidget(0, 3)
        self.participants.setObjectName("eventParticipantEditor")
        self.participants.setHorizontalHeaderLabels(["参与人物（勾选）", "角色", "备注"])
        self.participants.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        existing = {view.person.id: view for view in detail.participants} if detail else {}
        candidates = {person.id: person for person in people}
        candidates.update({id: view.person for id, view in existing.items()})
        for person in candidates.values():
            row = self.participants.rowCount()
            self.participants.insertRow(row)
            item = QTableWidgetItem(person.display_name + (" · Archived" if person.deleted_at else ""))
            item.setData(ROLE, person.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if person.id in existing else Qt.CheckState.Unchecked)
            self.participants.setItem(row, 0, item)
            view = existing.get(person.id)
            for column, name in [(1, "role"), (2, "note")]:
                value = getattr(view.participant, name, None) if view else None
                self.participants.setItem(row, column, QTableWidgetItem(value or ""))
        layout.addWidget(self.participants)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self):
        from app.services.event import ParticipantInput
        title, kind = self.title.text().strip(), self.event_type.text().strip()
        if not 1 <= len(title) <= 200 or not 1 <= len(kind) <= 200:
            raise DomainError("标题和类型去除首尾空格后长度须为 1–200。")
        try:
            occurred = date.fromisoformat(self.occurred_on.text().strip()) if self.occurred_on.text().strip() else None
        except ValueError:
            raise DomainError("发生日期须为有效 YYYY-MM-DD 日期。") from None
        started, ended = parse_datetime(self.started_at.text()), parse_datetime(self.ended_at.text())
        if (occurred is None) == (started is None):
            raise DomainError("发生日期与开始时间必须严格二选一；不支持未知时间。")
        if ended is not None and (started is None or ended < started):
            raise DomainError("结束时间须有开始时间且不早于开始时间。")
        participants = []
        for row in range(self.participants.rowCount()):
            item = self.participants.item(row, 0)
            if item.checkState() == Qt.CheckState.Checked:
                participants.append(ParticipantInput(person_id=item.data(ROLE),
                    role=self.participants.item(row, 1).text().strip() or None,
                    note=self.participants.item(row, 2).text().strip() or None))
        return dict(title=title, event_type=kind, record_kind=self.record_kind.currentData(),
                    occurred_on=occurred, started_at=started, ended_at=ended,
                    location=self.location.text().strip() or None,
                    description=self.description.toPlainText().strip() or None, participants=tuple(participants))

    def accept(self):
        try:
            self.values()
        except (DomainError, ValueError) as exc:
            QMessageBox.warning(self, "请检查事件信息", str(exc))
            return
        super().accept()
