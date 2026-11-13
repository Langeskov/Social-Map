"""Person's participation timeline, backed only by the event service."""
from html import escape
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QTableWidgetItem, QVBoxLayout, QWidget
from app.ui.event_detail import EventDetailDialog, event_time, participant_text, run_operation, table, ROLE


class EventTimeline(QWidget):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self.service = getattr(services, "events", None)
        self.workspace_id = self.person_id = None
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(QLabel("Person Timeline · 人物参与事件（事实 / 观察）"))
        self.include_deleted = QCheckBox("显示已删除事件")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        layout.addWidget(self.include_deleted)
        self.table = table(["发生时间", "标题", "类型", "性质", "简述", "参与者", "记录情况"], "personTimeline")
        self.table.setMinimumHeight(200)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _: self.show_detail())
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.detail_button = QPushButton("查看事件详情")
        self.detail_button.clicked.connect(self.show_detail)
        self.refresh_button = QPushButton("刷新 Timeline")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.detail_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.update_buttons()

    def run_operation(self, callback):
        return run_operation(self, callback)

    def set_person(self, workspace_id, person_id):
        self.workspace_id, self.person_id = workspace_id, person_id
        self.table.setRowCount(0)
        self.refresh()

    def selected(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(ROLE) if item else None

    def update_buttons(self):
        available = self.service is not None and self.person_id is not None
        self.refresh_button.setEnabled(available)
        self.detail_button.setEnabled(available and self.selected() is not None)

    def refresh(self):
        previous = self.selected()
        selected_id = previous.event.id if previous else None
        self.table.setRowCount(0)
        self.update_buttons()
        if self.service is None or self.person_id is None:
            return
        def load():
            details = self.service.list_events_by_person(self.workspace_id, self.person_id,
                                                         include_deleted=self.include_deleted.isChecked())
            target = 0
            for detail in details:
                event = detail.event
                row = self.table.rowCount()
                self.table.insertRow(row)
                description = event.description or ""
                summary = " ".join(description.split())
                if len(summary) > 120:
                    summary = summary[:120] + "…"
                values = [event_time(event), event.title, event.event_type, event.record_kind,
                          summary or "—", "、".join(participant_text(view) for view in detail.participants),
                          "已删除" if event.deleted_at else "有效记录"]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(ROLE, detail)
                    if column == 4 and description:
                        # Qt tooltips auto-detect HTML: escape user text before display.
                        item.setToolTip("<qt>" + escape(description).replace("\n", "<br>") + "</qt>")
                    self.table.setItem(row, column, item)
                if event.id == selected_id:
                    target = row
            if details:
                self.table.selectRow(target)
            self.update_buttons()
        self.run_operation(load)

    def show_detail(self):
        selected = self.selected()
        if selected is None:
            return None
        def show():
            detail = self.service.detail(self.workspace_id, selected.event.id, include_deleted=True)
            dialog = EventDetailDialog(detail, self, services=self.services)
            dialog.events_changed.connect(lambda _: self.refresh())
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)
