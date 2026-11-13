"""Complete Timeline widget: Events *and* Relationship state changes.

Two modes share one widget:

* ``MODE_PERSON``    - one person's participation timeline (participating Events
  plus every state change of a Relationship that person is an endpoint of).
* ``MODE_WORKSPACE`` - the whole Workspace timeline.

The panel renders exactly what ``TimelineService`` returns and never applies its
own time window: a planned Event or a planned state change in the future is part
of the timeline and stays visible (the service already ordered it).

Visibility is likewise delegated: ``include_deleted`` is forwarded verbatim, so
the widget never invents a second soft-delete rule.

Fact / observation separation is preserved: an Event opens the real Event detail
(with its Evidence panel); a state change opens a read-only Relationship detail.
"""
import logging
from dataclasses import dataclass
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.event_detail import EventDetailDialog, plain_label, table, time_text, ROLE
from app.ui.relationship_panel import HistoryDialog

logger = logging.getLogger(__name__)

MODE_PERSON = "PERSON"
MODE_WORKSPACE = "WORKSPACE"

KIND_LABELS = {"EVENT": "事件", "RELATIONSHIP_CHANGE": "关系变更"}
HEADERS = ["时间", "类型", "标题", "摘要", "相关人物", "关系状态"]
STATE_LABELS = {"ACTIVE": "ACTIVE · 正常", "PAUSED": "PAUSED · 暂停", "ENDED": "ENDED · 结束"}


def state_text(state):
    if not state:
        return "尚未生效"
    return STATE_LABELS.get(state, state)


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``).

    Only the exception class name is logged there: timeline summaries may quote
    private user text, so no exception message and no row text ever reaches the log.
    """
    return _run_operation(parent, callback, context="Timeline UI", title="时间线操作未完成")


@dataclass(frozen=True)
class TimelineRow:
    """One rendered Timeline row: the projection value plus its Event detail.

    ``event`` mirrors the historic EventTimeline contract (``row.event.id``) and
    is ``None`` for a relationship change.
    """
    item: object
    event: object = None


def summary_text(value, limit=120):
    text = " ".join((value or "").split())
    if not text:
        return "—"
    return text if len(text) <= limit else text[:limit] + "…"


def person_names(person_ids, names):
    labels = [names.get(person_id) or str(person_id)[:8] for person_id in person_ids]
    return "、".join(labels) if labels else "—"


class RelationshipDetailDialog(QDialog):
    """Read-only Relationship detail: identity, current state and full history.

    It writes nothing, shows no inferred value, and is the shared target of every
    "open this relationship" jump in the UI (Timeline, Graph, Dashboard, Search).
    """

    def __init__(self, services, workspace_id, relationship_id, parent=None, *, history=None):
        super().__init__(parent)
        self.setWindowTitle("Relationship Detail · 关系详情")
        self.resize(880, 560)
        self.services = services
        self.workspace_id = workspace_id
        self.relationship_id = relationship_id
        self.entries = tuple(history or ())
        self.view = None
        self.fields = {}
        layout = QVBoxLayout(self)
        header = plain_label("关系身份不可编辑；状态历史只追加。本页不显示任何推断或评分。")
        layout.addWidget(header)
        form = QFormLayout()
        for key, label in [("relationship_type", "关系类型"), ("directionality", "方向"),
                           ("endpoint_a", "端点 A"), ("endpoint_b", "端点 B"),
                           ("current_state", "当前状态"), ("created_at", "记录建立 UTC"),
                           ("effective_at", "当前状态生效 UTC"), ("lifecycle", "记录情况")]:
            self.fields[key] = plain_label("—")
            form.addRow(label, self.fields[key])
        layout.addLayout(form)
        layout.addWidget(QLabel("Relationship History · 状态历史（生效时间 → 写入时间 → UUID）"))
        self.history_table = table(["生效时间 UTC", "状态", "备注", "写入时间 UTC", "History UUID"],
                                   "relationshipHistory")
        self.history_table.setMinimumHeight(190)
        layout.addWidget(self.history_table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def load(self, workspace_id, relationship_id):
        """Reload from the services; every displayed value comes from a DTO."""
        def load_values():
            view = self.services.relationships.current(workspace_id, relationship_id, include_deleted=True)
            entries = self.services.relationships.history(workspace_id, relationship_id, include_deleted=True)
            self.set_view(view, entries)
            return view
        return run_operation(self, load_values)

    def set_view(self, view, entries):
        self.view = view
        self.entries = tuple(entries or ())
        entity = view.relationship
        self.workspace_id = entity.workspace_id
        self.relationship_id = entity.id
        self.fields["relationship_type"].setText(entity.relationship_type)
        self.fields["directionality"].setText(
            "DIRECTED · 有向（A → B）" if entity.directionality == "DIRECTED" else "UNDIRECTED · 无向")
        self.fields["endpoint_a"].setText(f"{view.person_a.display_name} · {view.person_a.id}")
        self.fields["endpoint_b"].setText(f"{view.person_b.display_name} · {view.person_b.id}")
        self.fields["current_state"].setText(state_text(view.current_state))
        self.fields["created_at"].setText(time_text(entity.created_at))
        self.fields["effective_at"].setText(
            time_text(view.current_history.effective_at) if view.current_history is not None else "—")
        lifecycle = "已删除" if entity.deleted_at is not None else "有效记录"
        if not view.endpoints_active:
            lifecycle += " · 端点已归档"
        self.fields["lifecycle"].setText(lifecycle)
        self.history_table.setRowCount(0)
        for entry in self.entries:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for column, value in enumerate([time_text(entry.effective_at), state_text(entry.state),
                                            entry.note or "", time_text(entry.created_at), str(entry.id)]):
                self.history_table.setItem(row, column, QTableWidgetItem(value))
        return view


class TimelinePanel(QWidget):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self.service = getattr(services, "timeline", None)
        self.workspace_id = self.person_id = None
        self.mode = MODE_WORKSPACE
        self._rows = []
        self._people = {}
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        self.heading = plain_label("Timeline · 完整时间线（Event + 关系状态变更，事实 / 观察）")
        layout.addWidget(self.heading)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("范围"))
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("timelineMode")
        self.mode_combo.addItem("Person Timeline · 当前人物", MODE_PERSON)
        self.mode_combo.addItem("Workspace Timeline · 当前 Workspace", MODE_WORKSPACE)
        self.mode_combo.currentIndexChanged.connect(self.change_mode)
        controls.addWidget(self.mode_combo)
        controls.addStretch()
        layout.addLayout(controls)
        self.include_deleted = QCheckBox("显示已删除记录")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        layout.addWidget(self.include_deleted)
        self.table = table(HEADERS, "personTimeline")
        self.table.setMinimumHeight(240)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(self.open_selected)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.detail_button = QPushButton("打开所选条目")
        self.detail_button.clicked.connect(self.open_selected)
        self.refresh_button = QPushButton("刷新 Timeline")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.detail_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.update_buttons()

    # ------------------------------------------------------------- small helpers

    @staticmethod
    def kind_label(kind):
        return KIND_LABELS.get(kind, kind or "—")

    def run_operation(self, callback):
        return run_operation(self, callback)

    def set_person(self, workspace_id, person_id):
        self.workspace_id, self.person_id = workspace_id, person_id
        self.table.setRowCount(0)
        self._rows = []
        self.refresh()

    def change_mode(self, index):
        selected = self.mode_combo.itemData(index)
        if selected is None or selected == self.mode:
            return
        self.mode = selected
        self.refresh()

    def selected_row(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def selected(self):
        """Historic EventTimeline contract: the selected row, or ``None``."""
        return self.selected_row()

    def update_buttons(self):
        available = self.service is not None and (
            self.workspace_id is not None and (self.mode == MODE_WORKSPACE or self.person_id is not None))
        self.refresh_button.setEnabled(available)
        self.detail_button.setEnabled(self.selected_row() is not None)
        self.mode_combo.setEnabled(available)

    # ------------------------------------------------------------------ loading

    def load_items(self):
        if self.service is None or self.workspace_id is None:
            return []
        include_deleted = self.include_deleted.isChecked()
        if self.mode == MODE_WORKSPACE:
            return list(self.service.workspace_timeline(self.workspace_id, include_deleted=include_deleted))
        if self.person_id is None:
            return []
        return list(self.service.person_timeline(self.workspace_id, self.person_id,
                                                 include_deleted=include_deleted))

    def refresh(self):
        selected = self.selected_row()
        selected_id = selected.item.entity_id if selected is not None else None
        self._rows = []
        self.table.setRowCount(0)
        self._people = {}
        self.update_buttons()
        if self.service is None:
            self.status.setText("Timeline 服务尚未装配：完整时间线不可用。")
            return
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace：时间线已清空。")
            return
        if self.mode == MODE_PERSON and self.person_id is None:
            self.status.setText("未选择人物：Person Timeline 已清空。")
            return

        def load():
            items = self.load_items()
            # Archived participants are resolved through the management view so a
            # recorded name is shown instead of a bare id.
            self._people = {person.id: person.display_name
                            for person in self.services.people.list(self.workspace_id,
                                                                    include_deleted=True)}
            self.table.blockSignals(True)
            try:
                self.table.setRowCount(0)
                target = -1
                for index, item in enumerate(items):
                    event = self._event_detail(item)
                    self._rows.append(TimelineRow(item=item, event=event))
                    values = [time_text(item.timestamp), self.kind_label(item.kind), item.title,
                              summary_text(item.summary), person_names(item.person_ids, self._people),
                              self._state_column(item)]
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    payload = self._rows[-1]
                    for column, value in enumerate(values):
                        entry = QTableWidgetItem(value)
                        if column == 0:
                            # The row payload carries both the projection item and its
                            # Event detail, so a selection can open the real detail.
                            entry.setData(ROLE, payload)
                        if column == 3 and item.summary:
                            # Qt tooltips auto-detect HTML: escape user text first.
                            entry.setToolTip("<qt>" + escape(item.summary).replace("\n", "<br>") + "</qt>")
                        self.table.setItem(row, column, entry)
                    if item.entity_id == selected_id:
                        target = index
                if items:
                    self.table.selectRow(target if target >= 0 else 0)
            finally:
                self.table.blockSignals(False)
            scope = "Workspace" if self.mode == MODE_WORKSPACE else "人物"
            self.status.setText(f"{scope}时间线 · {len(items)} 条"
                                + ("（含已删除记录）" if self.include_deleted.isChecked() else ""))
            self.update_buttons()
        self.run_operation(load)

    def _event_detail(self, item):
        if item.event_id is None or getattr(self.services, "events", None) is None:
            return None
        return self.services.events.detail(self.workspace_id, item.event_id, include_deleted=True)

    def _state_column(self, item):
        if item.kind == "RELATIONSHIP_CHANGE":
            if item.relationship_id is None:
                return "—"
            try:
                view = self.services.relationships.current(self.workspace_id, item.relationship_id,
                                                           include_deleted=True)
            except (DomainError, DatabaseError, ValueError) as exc:
                logger.warning("Timeline relationship state unavailable: type=%s", type(exc).__name__)
                return "—"
            return state_text(view.current_state)
        linked = " · 已关联关系历史" if item.relationship_id is not None else ""
        return "事件" + linked

    # ---------------------------------------------------------------- navigation

    def open_selected(self, *_args):
        row = self.selected_row()
        return None if row is None else self.open_row(row)

    def open_row(self, row):
        if row.item.kind == "RELATIONSHIP_CHANGE":
            if row.item.relationship_id is None:
                return None
            return self.show_relationship(row.item.relationship_id)
        return self.show_event(row)

    def show_event(self, row):
        """Open the real Event detail (with its Evidence panel) for one row."""
        if row is None or row.item.event_id is None or getattr(self.services, "events", None) is None:
            return None

        def show():
            detail = self.services.events.detail(self.workspace_id, row.item.event_id, include_deleted=True)
            dialog = EventDetailDialog(detail, self, services=self.services)
            dialog.events_changed.connect(lambda _: self.refresh())
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)

    def show_history_dialog(self, workspace_id, relationship_id):
        """Append-only history view of one Relationship (no write, no inference)."""
        def show():
            entries = self.services.relationships.history(workspace_id, relationship_id, include_deleted=True)
            dialog = HistoryDialog(entries, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)

    def show_relationship(self, relationship_id, workspace_id=None):
        """Open the read-only Relationship detail for one Relationship."""
        def show():
            dialog = RelationshipDetailDialog(self.services, workspace_id or self.workspace_id,
                                              relationship_id, self)
            dialog.load(dialog.workspace_id, dialog.relationship_id)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)


__all__ = ["MODE_PERSON", "MODE_WORKSPACE", "HEADERS", "TimelineRow", "TimelinePanel",
           "RelationshipDetailDialog", "run_operation", "state_text", "summary_text", "person_names"]
