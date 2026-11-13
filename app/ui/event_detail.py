"""Detached event detail rendering and shared UI operation boundary."""
import logging
from datetime import timezone
from PySide6.QtCore import Qt, Signal
# Re-exported so the modules that already import it from here keep one boundary.
from app.ui.operations import UNKNOWN_MESSAGE, run_operation  # noqa: F401
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHeaderView,
    QLabel, QMessageBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import DomainError
from app.services.errors import DatabaseError

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole


def plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def time_text(value):
    if value is None:
        return "—"
    if hasattr(value, "tzinfo"):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value.isoformat()


def event_time(event):
    if event.occurred_on is not None:
        return event.occurred_on.isoformat() + " · 日期"
    return time_text(event.started_at) + (" → " + time_text(event.ended_at) if event.ended_at else "")


def participant_text(view):
    return view.person.display_name + (" · Archived" if view.person.deleted_at else "")


def history_text(link):
    return (f"{link.relationship.relationship_type} · {link.history.state} · "
            f"{time_text(link.history.effective_at)} · {link.history.id}")


def table(headers, name):
    result = QTableWidget(0, len(headers))
    result.setObjectName(name)
    result.setHorizontalHeaderLabels(headers)
    result.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    result.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    result.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return result


class EventDetailView(QWidget):
    # Lets the Evidence panel follow whatever this view is currently displaying.
    detail_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_detail = None
        layout = QVBoxLayout(self)
        self.fields = {}
        form = QFormLayout()
        for key, label in [("title", "标题"), ("event_type", "类型"), ("record_kind", "性质"),
                           ("time", "发生时间（精确时间为 UTC）"), ("location", "地点"),
                           ("created_at", "录入时间 UTC"), ("updated_at", "更新时间 UTC"),
                           ("lifecycle", "记录情况")]:
            self.fields[key] = plain_label("—")
            form.addRow(label, self.fields[key])
        layout.addLayout(form)
        self.description = QTextEdit()
        self.description.setReadOnly(True)
        self.description.setMaximumHeight(120)
        layout.addWidget(QLabel("描述"))
        layout.addWidget(self.description)
        layout.addWidget(QLabel("Participants · 参与者"))
        self.participants = table(["人物", "角色", "备注", "人物 UUID"], "eventParticipants")
        self.participants.setMinimumHeight(130)
        layout.addWidget(self.participants)
        layout.addWidget(QLabel("已明确关联的 RelationshipHistory（仅引用，不自动改变关系）"))
        self.histories = table(["关系类型", "状态", "生效时间 UTC", "历史备注", "History UUID"], "eventHistories")
        self.histories.setMinimumHeight(130)
        layout.addWidget(self.histories)

    def set_detail(self, detail):
        self.current_detail = detail
        for label in self.fields.values():
            label.setText("—")
        self.description.clear()
        self.participants.setRowCount(0)
        self.histories.setRowCount(0)
        if detail is None:
            self.detail_changed.emit(None)
            return
        event = detail.event
        for key in ("title", "event_type", "record_kind", "location"):
            self.fields[key].setText(getattr(event, key) or "—")
        self.fields["time"].setText(event_time(event))
        self.fields["created_at"].setText(time_text(event.created_at))
        self.fields["updated_at"].setText(time_text(event.updated_at))
        self.fields["lifecycle"].setText("已删除" if event.deleted_at else "有效记录")
        self.description.setPlainText(event.description or "")
        for view in detail.participants:
            row = self.participants.rowCount()
            self.participants.insertRow(row)
            for column, value in enumerate([participant_text(view), view.participant.role or "",
                                            view.participant.note or "", str(view.person.id)]):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ROLE, view)
                self.participants.setItem(row, column, item)
        for link in detail.histories:
            row = self.histories.rowCount()
            self.histories.insertRow(row)
            for column, value in enumerate([link.relationship.relationship_type, link.history.state,
                                            time_text(link.history.effective_at), link.history.note or "",
                                            str(link.history.id)]):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ROLE, link)
                self.histories.setItem(row, column, item)
        self.detail_changed.emit(detail)


class EventDetailDialog(QDialog):
    events_changed = Signal(object)

    def __init__(self, detail, parent=None, *, services=None):
        super().__init__(parent)
        self.setWindowTitle("Event Detail · 事件详情")
        self.resize(920, 760)
        layout = QVBoxLayout(self)
        self.controller = None
        if services is not None:
            # Late import prevents the EventsPage -> rendering -> dialog cycle.
            from app.ui.events_page import EventsPage
            self.controller = EventsPage(services, self, detail=detail)
            self.controller.events_changed.connect(self.events_changed.emit)
            self.detail_view = self.controller.detail_view
            layout.addWidget(self.controller)
        else:
            self.detail_view = EventDetailView(self)
            self.detail_view.set_detail(detail)
            layout.addWidget(self.detail_view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
