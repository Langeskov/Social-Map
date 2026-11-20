"""Dashboard: the real "what happened recently?" workbench for one Workspace.

Every value on this page comes from ``DashboardService`` (counts + four bounded
recent lists) and every row opens the real detail elsewhere:

* recent Person        → People page, that person selected;
* recent Event         → Events page, that Event detail;
* recent Evidence      → Events page, the Event that owns it;
* recent relationship  → People page, the Relationship detail.

Nothing is inferred: there is no relationship strength, no affinity, no "danger"
score and no Analysis placeholder data. The unified search entry sits on top so
the page can also answer "where is X?".
"""
import logging
from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QVBoxLayout, QWidget)

from app.domain.entities import AnalysisRun, Event, Evidence, Person
from app.services.dashboard import RelationshipChange
from app.ui.context import ContextPage
from app.ui.design.widgets import SectionHeader
from app.ui.event_detail import plain_label, time_text, ROLE
from app.ui.search_panel import SearchPanel
from app.ui.timeline_panel import MODE_WORKSPACE, TimelinePanel

logger = logging.getLogger(__name__)

COUNT_LABELS = [("people", "人物"), ("active_relationships", "当前活跃关系"),
                ("events_7d", "最近 7 天事件"), ("events_30d", "最近 30 天事件"),
                ("evidence_7d", "最近 7 天证据"), ("evidence_30d", "最近 30 天证据")]


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Dashboard UI", title="工作台操作未完成")


def join_parts(*values):
    return " · ".join(str(value) for value in values if value)


class DashboardPage(ContextPage, QWidget):
    navigate_requested = Signal(str, object)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("dashboard")
        self.services = services
        self.service = getattr(services, "dashboard", None)
        self.workspace_id = services.initial_workspace.id
        self.snapshot = None
        self.count_labels = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = plain_label("Dashboard · 工作台：最近发生了什么")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        top = QHBoxLayout()
        top.addWidget(QLabel("当前 Workspace"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("dashboardWorkspaceSelector")
        self.workspace_combo.setMinimumWidth(160)
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        top.addWidget(self.workspace_combo, 1)
        self.include_deleted = QCheckBox("显示已删除记录")
        self.include_deleted.setObjectName("dashboardIncludeDeleted")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        top.addWidget(self.include_deleted)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("dashboardRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        self.search_panel = SearchPanel(services, self, name="dashboardSearch", compact=True)
        self.search_panel.navigate_requested.connect(self.open_search_result)
        # Phase 11 decision 4: the Dashboard no longer embeds a second search box or a
        # full Workspace Timeline. The widgets stay constructed (their contracts are
        # addressed by the Phase 7 tests) but are not part of the page layout: search
        # lives in the TopBar and the complete Timeline lives on People / Events.
        self.search_panel.hide()
        counts = QGroupBox("统计（只统计已记录的事实，不包含推断）")
        counts.setObjectName("dashboardCounts")
        counts_row = QHBoxLayout(counts)
        for key, label in COUNT_LABELS:
            column = QVBoxLayout()
            caption = QLabel(label)
            caption.setObjectName("subtitle")
            value = plain_label("—")
            value.setObjectName(f"dashboardCount_{key}")
            self.count_labels[key] = value
            column.addWidget(caption)
            column.addWidget(value)
            counts_row.addLayout(column)
        layout.addWidget(counts)
        splitter = QSplitter()
        self.people_list = self.section(splitter, "最近人物", "dashboardRecentPeople", layer_key="ENTITY")
        self.events_list = self.section(splitter, "最近事件", "dashboardRecentEvents", layer_key="OCCURRENCE")
        self.changes_list = self.section(splitter, "最近关系变化", "dashboardRecentChanges", layer_key="LINK")
        self.evidence_list = self.section(splitter, "最近证据", "dashboardRecentEvidence", layer_key="PROOF")
        self.analysis_list = self.section(splitter, "最近分析（DERIVED，非事实）", "dashboardRecentAnalysis",
                                          layer_key="INFERENCE")
        self.recent_people, self.recent_events = self.people_list, self.events_list
        self.recent_changes, self.recent_evidence = self.changes_list, self.evidence_list
        self.recent_analysis = self.analysis_list
        splitter.setSizes([240, 240, 260, 240, 240])
        layout.addWidget(splitter, 1)
        # The complete Workspace Timeline is no longer duplicated here (decision 4).
        self.workspace_timeline = TimelinePanel(services, self)
        self.workspace_timeline.mode = MODE_WORKSPACE
        self.workspace_timeline.set_person(self.workspace_id, None)
        self.workspace_timeline.mode_combo.setCurrentIndex(
            max(0, self.workspace_timeline.mode_combo.findData(MODE_WORKSPACE)))
        self.workspace_timeline.mode_combo.hide()
        self.workspace_timeline.hide()
        self.status = plain_label()
        layout.addWidget(self.status)
        self.load_workspaces()
        self.refresh()

    # ------------------------------------------------------------------ helpers

    def section(self, splitter, title, name, *, layer_key=None):
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        if layer_key:
            header = SectionHeader(title, panel, layer_key=layer_key)
            column.addWidget(header)
        else:
            heading = plain_label(title)
            column.addWidget(heading)
        widget = QListWidget()
        widget.setObjectName(name)
        widget.itemClicked.connect(self.open_entry)
        widget.itemDoubleClicked.connect(lambda _: self.open_selected())
        column.addWidget(widget)
        splitter.addWidget(panel)
        setattr(self, name, widget)
        return widget

    def run_operation(self, callback):
        return run_operation(self, callback)

    def list_widgets(self):
        return (self.dashboardRecentPeople, self.dashboardRecentEvents,
                self.dashboardRecentChanges, self.dashboardRecentEvidence,
                self.dashboardRecentAnalysis)

    def selected_entry(self):
        current = self.dashboardRecentPeople.currentItem()
        if current is not None:
            return current.data(ROLE)
        for widget in self.list_widgets():
            item = widget.currentItem()
            if item is not None:
                return item.data(ROLE)
        return None

    # ------------------------------------------------------------------ loading

    def load_workspaces(self):
        def load():
            workspaces = self.services.workspaces.list()
            self.workspace_combo.blockSignals(True)
            try:
                self.workspace_combo.clear()
                for workspace in workspaces:
                    self.workspace_combo.addItem(workspace.name, str(workspace.id))
                index = self.workspace_combo.findData(str(self.workspace_id))
                self.workspace_combo.setCurrentIndex(index if index >= 0 else 0)
                data = self.workspace_combo.currentData()
                self.workspace_id = UUID(data) if data else None
            finally:
                self.workspace_combo.blockSignals(False)
            return self.workspace_id
        return self.run_operation(load)

    def change_workspace(self, index):
        if index < 0:
            return
        data = self.workspace_combo.itemData(index)
        workspace_id = UUID(data) if data else None
        if self.workspace_context is not None:
            self.workspace_context.set_workspace(workspace_id)
            return
        self.workspace_id = workspace_id
        self.search_panel.set_workspace(self.workspace_id)
        self.refresh()

    # ------------------------------------------------- Phase 11: context protocol
    def clear_context(self):
        """Forget the Workspace's snapshot: no count, row or search result survives."""
        self.snapshot = None
        for widget in self.list_widgets():
            widget.blockSignals(True)
            widget.clear()
            widget.blockSignals(False)
        for label in self.count_labels.values():
            label.setText("—")
        self.search_panel.input.blockSignals(True)
        self.search_panel.input.clear()
        self.search_panel.input.blockSignals(False)
        self.search_panel.set_workspace(None)
        self.status.setText("")

    def load_workspace(self, workspace_id):
        self.load_workspaces()
        self.search_panel.set_workspace(workspace_id)
        self.refresh()

    def sync_workspace_combo(self):
        index = self.workspace_combo.findData(str(self.workspace_id))
        if index < 0:
            return
        self.workspace_combo.blockSignals(True)
        try:
            self.workspace_combo.setCurrentIndex(index)
        finally:
            self.workspace_combo.blockSignals(False)

    def activate(self):
        self.load_workspaces()
        self.search_panel.set_workspace(self.workspace_id)
        self.refresh()

    def refresh(self):
        self.snapshot = None
        for widget in self.list_widgets():
            widget.blockSignals(True)
            widget.clear()
            widget.blockSignals(False)
        for label in self.count_labels.values():
            label.setText("—")
        if self.service is None:
            self.status.setText("工作台服务尚未装配：Dashboard 不可用。")
            return self.snapshot
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace。")
            return self.snapshot
        self.search_panel.set_workspace(self.workspace_id)
        self.workspace_timeline.set_person(self.workspace_id, None)

        def load():
            snapshot = self.service.snapshot(self.workspace_id,
                                             include_deleted=self.include_deleted.isChecked())
            self.snapshot = snapshot
            # DERIVED list: read through the Dashboard service only, on its own
            # bounded query so the snapshot's frozen query count is unchanged.
            reader = getattr(self.service, "recent_analysis", None)
            runs = list(reader(self.workspace_id)) if reader is not None else []
            self.set_snapshot(snapshot, runs)
            return snapshot
        return self.run_operation(load)

    def person_entry(self, person):
        return (person, join_parts(person.display_name,
                                   "已归档" if person.deleted_at is not None else None,
                                   person.organization, person.occupation),
                time_text(person.created_at))

    def event_entry(self, event):
        return (event, join_parts(event.title, event.event_type, event.record_kind,
                                  "已删除" if event.deleted_at is not None else None),
                time_text(event.started_at or event.occurred_on))

    def change_entry(self, change):
        return (change, join_parts(change.relationship.relationship_type, change.history.state,
                                   "已删除" if change.relationship.deleted_at is not None else None),
                time_text(change.history.effective_at))

    def evidence_entry(self, evidence):
        return (evidence, join_parts(evidence.title, evidence.kind, evidence.source_type,
                                     "已删除" if evidence.deleted_at is not None else None),
                time_text(evidence.captured_at or evidence.created_at))

    def analysis_entry(self, run):
        """DERIVED rows: the status and target are shown, the answer is not a fact."""
        return (run, join_parts("DERIVED", run.analysis_type, run.status,
                                run.provider_name, run.model_name),
                time_text(run.created_at))

    def set_snapshot(self, snapshot, runs=()):
        self.snapshot = snapshot
        self.recent_runs = list(runs)
        for key, label in self.count_labels.items():
            label.setText(str(getattr(snapshot.counts, key)))
        self.fill(self.dashboardRecentPeople,
                  [self.person_entry(person) for person in snapshot.recent_people])
        self.fill(self.dashboardRecentEvents,
                  [self.event_entry(event) for event in snapshot.recent_events])
        self.fill(self.dashboardRecentChanges,
                  [self.change_entry(change) for change in snapshot.recent_relationship_changes])
        self.fill(self.dashboardRecentEvidence,
                  [self.evidence_entry(evidence) for evidence in snapshot.recent_evidence])
        self.fill(self.dashboardRecentAnalysis,
                  [self.analysis_entry(run) for run in self.recent_runs])
        scope = "（含已删除记录）" if self.include_deleted.isChecked() else ""
        self.status.setText(
            f"最近记录 {scope}· 人物 {len(snapshot.recent_people)} · 事件 {len(snapshot.recent_events)}"
            f" · 关系变化 {len(snapshot.recent_relationship_changes)}"
            f" · 证据 {len(snapshot.recent_evidence)}"
            f" · 分析 {len(self.recent_runs)}。点击条目打开对应详情。")
        return snapshot

    @staticmethod
    def fill(widget, entries):
        """``entries`` is ``(payload, text, moment)``: payload rides in UserRole."""
        widget.blockSignals(True)
        try:
            widget.clear()
            for payload, text, moment in entries:
                item = QListWidgetItem(f"{text}\n{moment}")
                item.setData(ROLE, payload)
                widget.addItem(item)
        finally:
            widget.blockSignals(False)

    # --------------------------------------------------------------- navigation

    def open_selected(self):
        entry = self.selected_entry()
        return None if entry is None else self.open_entry(None, entry=entry)

    def open_entry(self, item, entry=None):
        entry = entry if entry is not None else (item.data(ROLE) if item is not None else None)
        if entry is None:
            return None
        kind = type(entry)
        if kind is Person:
            self.navigate_requested.emit("person", entry.id)
        elif kind is Event:
            self.navigate_requested.emit("event", entry.id)
        elif kind is Evidence:
            self.navigate_requested.emit("event", entry.event_id)
        elif kind is RelationshipChange:
            self.navigate_requested.emit("relationship", entry.relationship.id)
        elif kind is AnalysisRun:
            self.navigate_requested.emit("analysis_run", entry.id)
        return entry

    def open_search_result(self, result):
        """Alias / Evidence results carry the parent id the navigation needs."""
        if result.entity_type == "PERSON":
            self.navigate_requested.emit("person", result.entity_id)
        elif result.entity_type == "ALIAS":
            person_id = result.metadata.get("person_id")
            if person_id is not None:
                self.navigate_requested.emit("person", person_id)
        elif result.entity_type == "EVIDENCE":
            event_id = result.metadata.get("event_id")
            if event_id is not None:
                self.navigate_requested.emit("event", event_id)
        elif result.entity_type == "EVENT":
            self.navigate_requested.emit("event", result.entity_id)
        return result


__all__ = ["DashboardPage", "COUNT_LABELS", "join_parts", "run_operation"]
