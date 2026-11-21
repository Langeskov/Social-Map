"""Analysis page: the history of user-triggered model runs, and nothing else.

The page is deliberately a *ledger viewer*:

* it lists what ``AnalysisService.history`` returns - nothing is generated here and
  no model is touched by opening, refreshing or selecting a row;
* the only write actions are ``cancel_run`` (an unfinished run) and ``delete_run``
  (a history record); neither can change a Person, Relationship, Event or Evidence;
* a run's target is resolved through the ordinary read services purely for display;
* the Provider list is configuration, not a call: connection tests happen only when
  the user presses "测试连接" inside the settings dialog.

Phase 9 adds two read-mostly blocks to the detail area:

* **Sources** - the four grouped reference lists of the selected run, each row
  showing only a name/short id, and each row jumping to the fact it references;
* **候选引用 / Save as Memory** - ``MemoryService.candidate_references`` may be
  displayed, always labelled as a candidate and never written by this page, and a
  finished answer can be turned into a Memory *after* the user edits it inside
  ``MemoryConfirmDialog`` (opening that dialog writes nothing at all).
"""
import logging
from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.services.prompt import split_result_sections
from app.ui.analysis_dialog import (MEMORY_KINDS, RESULT_SECTIONS, SOURCE_KINDS, SOURCE_LABELS,
    TARGET_LABELS, AnalysisResultDialog, AnalysisSourceRows, ContextSnapshotDialog,
    MemoryConfirmDialog, ProviderSettingsDialog, duration_text, plain_label,
    suggested_memory_content, time_text, track_dialog, utc_text)
from app.ui.analysis_dialog import run_analysis
from app.ui.context import ContextPage
from app.ui.design.tokens import SPACE
from app.ui.design.widgets import LayerCard, plain_text_label

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole

#: The fixed order of the Analysis workbench (Phase 11 §4.10).
WORKBENCH_STAGES = [("Target", "目标"), ("Context Scope", "上下文范围"), ("Model", "模型"),
                    ("Result", "结果"), ("Sources", "来源")]

FILTER_ALL = "全部目标"
UNFINISHED = {"PENDING", "RUNNING"}
TARGET_FILTERS = ((FILTER_ALL, None),) + tuple((f"{key} · {label}", key)
                                               for key, label in TARGET_LABELS.items())
HISTORY_HEADERS = ["创建时间", "目标类型", "目标", "分析类型", "Provider", "模型", "状态", "耗时"]
#: Candidate references: entity type, target, the service's verdict and its reason.
CANDIDATE_HEADERS = ["实体类型", "候选对象", "是否可作为 Memory 引用", "Service 判定"]
CANDIDATE_ACCEPTED = "已被 Service 接受（可跳转）"
CANDIDATE_REJECTED = "候选无效（不属于本次来源，已拒绝）"


def source_label_text(kind) -> str:
    return SOURCE_LABELS.get(kind, str(kind))


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Analysis UI", title="分析操作未完成")


class AnalysisPage(ContextPage, QWidget):
    """Analysis History for one Workspace, with the Provider settings entry."""

    navigate_requested = Signal(str, object)
    # Decision 6: while a run is in flight the Workspace selector is locked.
    workspace_lock_changed = Signal(bool)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("analysis")
        self.services = services
        self.analysis = getattr(services, "analysis", None)
        self.gateway = getattr(services, "llm", None)
        self.workspace_id = getattr(getattr(services, "initial_workspace", None), "id", None)
        self.runs = []
        self.current_view = None
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = plain_label("Analysis · 分析（只有你点击后才会调用模型）")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        top = QHBoxLayout()
        top.addWidget(QLabel("当前 Workspace"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("analysisWorkspaceSelector")
        self.workspace_combo.setMinimumWidth(160)
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        top.addWidget(self.workspace_combo, 1)
        top.addWidget(QLabel("目标过滤"))
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("analysisTargetFilter")
        for label, value in TARGET_FILTERS:
            self.filter_combo.addItem(label, value)
        self.filter_combo.currentIndexChanged.connect(lambda _: self.refresh())
        top.addWidget(self.filter_combo)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("analysisRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        self.provider_button = QPushButton("Provider 设置")
        self.provider_button.setObjectName("analysisProviderSettings")
        self.provider_button.clicked.connect(self.open_provider_settings)
        top.addWidget(self.provider_button)
        layout.addLayout(top)
        # The workbench reads left to right, in the order the chain runs:
        # Analysis Target -> Context Scope -> Model -> Result -> Sources.
        self.workbench = LayerCard("INFERENCE", "Analysis Workbench · 分析工作台（只有你点击后才会调用模型）",
                                   self)
        self.workbench.setObjectName("analysisWorkbench")
        stages = QHBoxLayout()
        stages.setSpacing(SPACE["sm"])
        self.workbench_stages = []
        for index, (name, caption) in enumerate(WORKBENCH_STAGES):
            if index:
                arrow = plain_text_label("→", object_name="workbenchArrow", wrap=False)
                stages.addWidget(arrow)
            label = plain_text_label(f"{name} · {caption}", object_name="workbenchStage", wrap=False)
            self.workbench_stages.append(label)
            stages.addWidget(label)
        stages.addStretch(1)
        self.workbench.body.addLayout(stages)
        self.workbench_status = plain_text_label("尚未选择分析目标：结果只在模型返回后显示。",
                                                 object_name="workbenchStatus")
        self.workbench.body.addWidget(self.workbench_status)
        layout.addWidget(self.workbench)
        splitter = QSplitter()
        left = QWidget()
        column = QVBoxLayout(left)
        column.addWidget(plain_label("Analysis History（每个阶段都会创建新的分析记录，不会覆盖旧记录）"))
        self.history_table = QTableWidget(0, len(HISTORY_HEADERS))
        self.history_table.setObjectName("analysisHistory")
        self.history_table.setHorizontalHeaderLabels(HISTORY_HEADERS)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.itemSelectionChanged.connect(self.select_run)
        self.history_table.itemDoubleClicked.connect(lambda _: self.show_result())
        column.addWidget(self.history_table)
        row = QHBoxLayout()
        self.result_button = self.button(row, "查看结果", self.show_result)
        self.snapshot_button = self.button(row, "查看上下文快照", self.show_snapshot)
        self.cancel_button = self.button(row, "取消运行", self.cancel_run)
        self.delete_button = self.button(row, "删除记录", self.delete_run)
        column.addLayout(row)
        splitter.addWidget(left)
        right = QWidget()
        detail = QVBoxLayout(right)
        self.detail_fields = {}
        form = QFormLayout()
        for key, label in [("target", "目标"), ("analysis_type", "分析类型"), ("provider", "Provider"),
                           ("model", "模型"), ("status", "状态"), ("created", "创建时间"),
                           ("duration", "耗时"), ("sources", "来源数量"), ("scope", "范围")]:
            self.detail_fields[key] = plain_label("—")
            form.addRow(label, self.detail_fields[key])
        detail.addLayout(form)
        self.message = plain_label()
        self.message.setObjectName("analysisMessage")
        detail.addWidget(self.message)
        detail.addWidget(plain_label("结果摘要（模型输出，不是数据库事实）"))
        self.summary = QPlainTextEdit()
        self.summary.setObjectName("analysisSummary")
        self.summary.setReadOnly(True)
        self.summary.setMinimumHeight(240)
        self.summary.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        detail.addWidget(self.summary, 1)
        # Phase 9: the reference lists live below the summary, in their own scroll
        # area, so a run with many sources never squeezes the answer off screen.
        references = QScrollArea()
        references.setObjectName("analysisReferences")
        references.setWidgetResizable(True)
        references.setMinimumHeight(220)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        self.sources_group = QGroupBox("Sources · 本次运行的来源（只显示名称/短 id，可双击跳转）")
        self.sources_group.setObjectName("analysisSourcesGroup")
        sources_layout = QVBoxLayout(self.sources_group)
        self.source_rows = {}
        self.source_counts = {}
        self.source_resolvers = {kind: self.source_resolver(kind) for kind, _ in SOURCE_KINDS}
        for kind, label in SOURCE_KINDS:
            row = QHBoxLayout()
            count = plain_label(f"{label}（0）")
            count.setObjectName(f"analysisSourceCount_{kind}")
            count.setMinimumWidth(150)
            row.addWidget(count)
            rows = AnalysisSourceRows(kind, self)
            rows.activated.connect(self.open_source)
            row.addWidget(rows, 1)
            sources_layout.addLayout(row)
            self.source_rows[kind] = rows
            self.source_counts[kind] = count
        panel_layout.addWidget(self.sources_group)
        panel_layout.addWidget(plain_label(
            "候选引用（模型输出里出现、并已与本次来源核对的引用；只有被 Service 接受的候选可跳转，"
            "本页不会据此写入任何数据）"))
        self.candidates = QTableWidget(0, len(CANDIDATE_HEADERS))
        self.candidates.setObjectName("analysisCandidates")
        self.candidates.setHorizontalHeaderLabels(CANDIDATE_HEADERS)
        self.candidates.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Candidates are read-only rows; a rejected one is never an action.
        self.candidates.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.candidates.setMinimumHeight(110)
        self.candidates.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.candidates.itemDoubleClicked.connect(lambda _: self.open_candidate())
        panel_layout.addWidget(self.candidates)
        self.candidate_note = plain_label(
            "尚未读取候选引用：选择一条分析记录后显示（只读，不写库）。")
        self.candidate_note.setObjectName("analysisCandidateNote")
        panel_layout.addWidget(self.candidate_note)
        references.setWidget(panel)
        detail.addWidget(references)
        actions = QHBoxLayout()
        self.source_open_button = self.button(actions, "跳转到所选来源", self.open_selected_source)
        self.memory_button = self.button(actions, "Save as Memory · 保存为记忆", self.save_memory)
        detail.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([620, 420])
        layout.addWidget(splitter, 1)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.load_workspaces()
        self.refresh()

    def run_operation(self, callback):
        return run_operation(self, callback)

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

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
            # A running analysis locks the context (decision 6): never switch the
            # Workspace under a live run.
            if self.analysis_locked():
                self.sync_workspace_combo()
                return
            self.workspace_context.set_workspace(workspace_id)
            return
        self.workspace_id = workspace_id
        self.refresh()

    def analysis_locked(self) -> bool:
        """True while a run is in flight for the current Workspace."""
        return bool(getattr(self, "workspace_locked", False))

    def set_workspace_locked(self, locked: bool) -> bool:
        """Lock the Workspace selector for the duration of one run (decision 6)."""
        locked = bool(locked)
        if getattr(self, "workspace_locked", False) == locked:
            return locked
        self.workspace_locked = locked
        if locked:
            self.workbench_status.setText("分析运行中：Workspace 已锁定。你可以取消本次分析。")
        else:
            self.workbench_status.setText(
                "分析已结束：Workspace 已解锁；结果不会自动变成事实，需要你确认。")
        self.workspace_lock_changed.emit(locked)
        return locked

    def _lock_for_options(self, options) -> None:
        """Lock once the worker started; unlock only when the QThread ends.

        The lock spans the actual Worker lifecycle (Phase 12.3): a cancel
        request and a closed dialog never unlock early, success, failure and
        cancellation all unlock through ``QThread.finished``, and no thread is
        ever killed to cancel a model request. The connection is explicitly
        queued to this page (GUI thread), because ``finished`` is emitted from
        the worker thread.
        """
        progress = getattr(options, "progress", None)
        worker = getattr(progress, "worker", None) if progress is not None else None
        if worker is None:
            return
        self.set_workspace_locked(True)
        worker.finished.connect(self._unlock_after_worker, Qt.ConnectionType.QueuedConnection)

    def _unlock_after_worker(self) -> None:
        """The QThread has truly ended: release the Workspace lock."""
        self.set_workspace_locked(False)

    # ------------------------------------------------- Phase 11: context protocol
    def context_object(self):
        """Breadcrumb object segment: the selected analysis run id."""
        view = self.current_view
        if view is None:
            return ""
        return f"Run {str(view.run.id)[:8]}"

    def clear_context(self):
        """Forget the selected run and its result: no analysis UI context survives."""
        self.runs = []
        self.current_view = None
        self.history_table.setRowCount(0)
        self.set_detail(None)
        self.status.setText("")

    def load_workspace(self, workspace_id):
        self.load_workspaces()
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
        return self.refresh()

    def current_filter(self):
        return self.filter_combo.currentData()

    def refresh(self, selected_id=None):
        """Re-read the history; this never creates a run and never calls a model."""
        self.history_table.setRowCount(0)
        self.current_view = None
        self.set_detail(None)
        if self.analysis is None or not hasattr(self.analysis, "history"):
            self.status.setText("分析服务尚未装配：Analysis 不可用。")
            self.update_buttons()
            return []
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace。")
            self.update_buttons()
            return []

        def load():
            self.runs = list(self.analysis.history(self.workspace_id,
                                                   target_type=self.current_filter(), limit=100))
            target = None
            for view in self.runs:
                row = self.history_table.rowCount()
                self.history_table.insertRow(row)
                values = [utc_text(view.run.created_at),
                          TARGET_LABELS.get(view.run.target_type, view.run.target_type),
                          self.target_text(view.run), view.run.analysis_type, view.run.provider_name,
                          view.run.model_name, view.run.status, duration_text(view.run.duration_ms)]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(ROLE, view)
                    self.history_table.setItem(row, column, item)
                if view.run.id == selected_id:
                    target = row
            if self.runs:
                self.history_table.selectRow(0 if target is None else target)
            self.status.setText(self.status_text())
            self.update_buttons()
            return self.runs
        return self.run_operation(load)

    def status_text(self):
        providers = 0 if self.gateway is None else len(self.gateway.config().providers)
        hint = ("" if providers else " · 尚未配置 Provider：请点击「Provider 设置」添加后才能发起分析")
        return (f"分析记录 {len(self.runs)} 条（本页只读取分析记录；不自动调用模型）"
                f" · Provider {providers} 个{hint}")

    def target_text(self, run):
        label = self.target_name(run.target_type, run.target_id)
        short = str(run.target_id)[:8]
        return f"{label or '（已不可见）'} · {short}" if label else short

    def target_name(self, target_type, target_id):
        """Read-only display lookup; a missing/archived target shows no name."""
        def lookup():
            if target_type == "PERSON":
                detail = self.services.people.detail(self.workspace_id, target_id)
                return detail.person.display_name + ("（已归档）" if detail.person.deleted_at else "")
            if target_type == "EVENT":
                detail = self.services.events.detail(self.workspace_id, target_id, include_deleted=True)
                return detail.event.title
            if target_type == "RELATIONSHIP":
                service = getattr(self.services, "relationships", None)
                found = service.get(self.workspace_id, target_id, include_deleted=True) \
                    if service is not None else None
                return found.relationship_type if found is not None else None
            return None
        try:
            return lookup()
        except (DomainError, DatabaseError, OSError, ValueError, AttributeError) as exc:
            logger.warning("Analysis target lookup failed: type=%s", type(exc).__name__)
            return None

    def source_name(self, workspace_id, kind, target_id):
        """A reference-only label for one Source row (never a fact body).

        Evidence belongs to an Event and has no page of its own, so its row shows
        the evidence title; the jump resolves the owning Event through
        ``EvidenceService.get``. A deleted fact still resolves, because a recorded
        reference must stay visible in the ledger.
        """
        workspace_id = workspace_id or self.workspace_id
        def lookup():
            if kind == "PERSON":
                return self.services.people.detail(workspace_id, target_id).person.display_name
            if kind == "EVENT":
                return self.services.events.detail(workspace_id, target_id,
                                                   include_deleted=True).event.title
            if kind == "RELATIONSHIP":
                service = getattr(self.services, "relationships", None)
                found = service.get(workspace_id, target_id, include_deleted=True) \
                    if service is not None else None
                return found.relationship_type if found is not None else None
            if kind == "EVIDENCE":
                evidence = self.services.evidence.get(workspace_id, target_id, include_deleted=True)
                return evidence.title
            return None
        try:
            return lookup()
        except (DomainError, DatabaseError, OSError, ValueError, AttributeError) as exc:
            logger.warning("Analysis source lookup failed: kind=%s type=%s", kind,
                           type(exc).__name__)
            return None

    def source_resolver(self, kind):
        return lambda workspace_id, target_id: self.source_name(workspace_id, kind, target_id)

    # ------------------------------------------------------------------- detail
    def selected_view(self):
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        return item.data(ROLE) if item is not None else None

    def select_run(self):
        self.set_detail(self.selected_view())

    def set_detail(self, view):
        self.current_view = view
        self.notify_context()
        for label in self.detail_fields.values():
            label.setText("—")
        self.summary.clear()
        self.message.clear()
        self.clear_references()
        if view is None:
            self.update_buttons()
            return None
        run = view.run
        self.detail_fields["target"].setText(
            f"{TARGET_LABELS.get(run.target_type, run.target_type)} · {self.target_text(run)}")
        self.detail_fields["analysis_type"].setText(run.analysis_type)
        self.detail_fields["provider"].setText(run.provider_name)
        self.detail_fields["model"].setText(run.model_name)
        self.detail_fields["status"].setText(run.status)
        self.detail_fields["created"].setText(utc_text(run.created_at))
        self.detail_fields["duration"].setText(duration_text(run.duration_ms))
        sources = view.sources
        self.detail_fields["sources"].setText(
            f"合计 {sources.total()}（人物 {len(sources.people)} · 关系 {len(sources.relationships)}"
            f" · 事件 {len(sources.events)} · 证据 {len(sources.evidence)}）")
        self.detail_fields["scope"].setText(self.scope_text(view))
        self.show_sources(view)
        self.show_candidates(view)
        if run.status == "SUCCEEDED":
            self.summary.setPlainText(run.result or "")
            self.message.setText("状态 SUCCEEDED · 下面是模型输出，不是数据库事实；事实层未被修改。"
                                 f"识别到的小节：{self.section_summary(run)}")
        else:
            self.message.setText(f"状态 {run.status} · {run.error_message or '没有可显示的结果正文。'}")
        self.update_buttons()
        return view

    # ------------------------------------------------------------------ sources
    def clear_references(self):
        """Empty every reference view; called on every detail change and refresh."""
        for kind, _ in SOURCE_KINDS:
            self.source_rows[kind].setRowCount(0)
            self.source_counts[kind].setText(f"{source_label_text(kind)}（0）")
        self.candidates.setRowCount(0)
        self.candidate_note.setText("尚未读取候选引用：选择一条分析记录后显示（只读，不写库）。")
        return None

    def show_sources(self, view):
        """Fill the four groups with reference-only rows (no fact body is copied).

        A source id that no longer resolves is still listed, with the short id and
        the reason - the run's recorded reference is never hidden or rewritten.
        """
        workspace_id = view.run.workspace_id
        groups = {"PERSON": view.sources.people, "RELATIONSHIP": view.sources.relationships,
                  "EVENT": view.sources.events, "EVIDENCE": view.sources.evidence}
        for kind, label in SOURCE_KINDS:
            targets = [source.target_id for source in groups[kind]]
            count = self.source_rows[kind].fill(workspace_id, targets, self.source_resolvers)
            self.source_counts[kind].setText(f"{label}（{count}）")
        return self.source_rows

    def selected_source(self):
        for kind, _ in SOURCE_KINDS:
            target_id = self.source_rows[kind].current_target()
            if target_id is not None:
                return kind, target_id
        return None, None

    def open_selected_source(self):
        kind, target_id = self.selected_source()
        if kind is None:
            self.status.setText("请先在 Sources 里选择一条来源（双击也可跳转）。")
            return None
        return self.open_source(kind, target_id)

    def open_source(self, kind, target_id):
        """Jump to the referenced fact; the Target page renders it, not this page."""
        return self.open_target(kind, target_id)

    # --------------------------------------------------------------- candidates
    def candidate_references(self, view):
        """Read-only candidate list; without the service nothing is invented."""
        service = getattr(self.services, "memories", None)
        if service is None or not hasattr(service, "candidate_references"):
            return None
        return list(service.candidate_references(view.run.workspace_id, view.run.id))

    def show_candidates(self, view):
        """Candidates from the model output, always labelled as candidates.

        Only a reference the Service accepted may be activated here; a rejected or
        non-matching candidate is displayed as invalid and is never writable. This
        method never writes anything, including never creating a Memory.
        """
        candidates = self.run_operation(lambda: self.candidate_references(view))
        self.candidates.setRowCount(0)
        if candidates is None:
            self.candidate_note.setText(
                "Memory 服务尚未装配：不显示候选引用（本页不会据此写入任何数据）。")
            return []
        accepted = rejected = 0
        for candidate in candidates:
            is_accepted = bool(getattr(candidate, "accepted", False))
            row = self.candidates.rowCount()
            self.candidates.insertRow(row)
            if is_accepted:
                accepted += 1
                verdict = CANDIDATE_ACCEPTED
            else:
                rejected += 1
                verdict = CANDIDATE_REJECTED
            values = [str(getattr(candidate, "entity_type", "") or "—"),
                      self.candidate_target_text(candidate), verdict,
                      str(getattr(candidate, "reason", "") or "—")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setData(ROLE, candidate if is_accepted else None)
                self.candidates.setItem(row, column, item)
        self.candidate_note.setText(
            f"候选引用 {len(candidates)} 条（被接受 {accepted} · 无效 {rejected}）。"
            "候选来自模型输出，只是候选：只有被 Service 接受的那些能跳转，"
            "本页不会自动创建 Memory，也不会修改任何事实。")
        return candidates

    def candidate_target_text(self, candidate):
        """A reference-only label for one candidate: name, or its short id."""
        entity_type = str(getattr(candidate, "entity_type", "") or "")
        entity_id = getattr(candidate, "entity_id", None)
        if entity_id is None:
            return "—"
        short = str(entity_id)[:8]
        if entity_type not in SOURCE_LABELS:
            return f"{entity_type} · {short}"
        resolver = self.source_resolvers.get(entity_type)
        try:
            name = resolver(getattr(candidate, "workspace_id", None) or self.workspace_id,
                            entity_id) if resolver is not None else None
        except Exception as exc:
            logger.debug("Candidate lookup failed: type=%s", type(exc).__name__)
            name = None
        return f"{name} · {short}" if name else short

    def open_candidate(self):
        """Double-click on an accepted candidate only; a rejected row has no target."""
        row = self.candidates.currentRow()
        item = self.candidates.item(row, 1) if row >= 0 else None
        candidate = item.data(ROLE) if item is not None else None
        if candidate is None:
            return None
        return self.open_target(str(candidate.entity_type), candidate.entity_id)

    @staticmethod
    def scope_text(view):
        scope = view.scope
        include = scope.get("include", {})
        parts = [f"时间范围 {scope.get('time_scope', '—')}"]
        if include:
            parts.append("包含 " + "、".join(f"{key}={'是' if value else '否'}"
                                             for key, value in include.items()))
        snapshot = view.snapshot
        if snapshot is not None:
            parts.append(f"快照估算 {snapshot.estimated_tokens} tokens")
        return " · ".join(parts)

    @staticmethod
    def section_summary(run):
        """Which of the four result sections the stored answer actually contains."""
        sections = split_result_sections(run.result or "")
        found = [title for key, title in RESULT_SECTIONS if sections.get(key)]
        return " · ".join(found) if found else "未识别到小节标题"

    def update_buttons(self):
        available = self.analysis is not None and self.workspace_id is not None
        run = None if self.current_view is None else self.current_view.run
        self.refresh_button.setEnabled(available)
        self.provider_button.setEnabled(self.gateway is not None)
        self.result_button.setEnabled(bool(run is not None and run.status == "SUCCEEDED"))
        self.snapshot_button.setEnabled(bool(self.current_view is not None
                                            and self.current_view.snapshot is not None))
        self.cancel_button.setEnabled(bool(run is not None and run.status in UNFINISHED))
        self.delete_button.setEnabled(bool(run is not None))
        # Save as Memory is offered for a recorded answer only; the Memory service
        # itself is checked when the button is pressed, so a missing service cannot
        # turn a read-only page into a crash.
        self.memory_button.setEnabled(bool(available and run is not None
                                           and run.status == "SUCCEEDED"))
        self.source_open_button.setEnabled(available)

    # ------------------------------------------------------------------ actions
    def show_result(self):
        view = self.current_view
        if view is None:
            return None
        dialog = AnalysisResultDialog(view, parent=self)
        track_dialog(self, dialog)
        dialog.show()
        return dialog

    def show_snapshot(self):
        view = self.current_view
        if view is None or view.snapshot is None:
            return None
        dialog = ContextSnapshotDialog(view.snapshot, parent=self)
        track_dialog(self, dialog)
        dialog.show()
        return dialog

    # ---------------------------------------------------------- save as memory
    def memory_service(self):
        return getattr(self.services, "memories", None)

    def suggested_memory(self):
        """The editable default content offered by the confirmation dialog."""
        return suggested_memory_content(self)

    def save_memory(self):
        """Open the Memory confirmation dialog for the selected answer.

        Opening the dialog (or cancelling it) writes nothing. Only the dialog's
        ``confirmed`` signal - the user pressing "确认保存" - calls
        ``MemoryService.create_from_analysis(..., confirmed=True)``.
        """
        view = self.current_view
        service = self.memory_service()
        if view is None or view.run.status != "SUCCEEDED":
            return None
        if service is None or not hasattr(service, "create_from_analysis"):
            QMessageBox.warning(self, "Memory 不可用", "Memory 服务尚未装配：无法保存为记忆。")
            return None
        dialog = MemoryConfirmDialog(self.services, view.run.workspace_id, view.run,
                                     self.suggested_memory(), parent=self)
        dialog.confirmed.connect(lambda values: self.create_memory_from_analysis(dialog, values))
        track_dialog(self, dialog)
        dialog.show()
        return dialog

    def create_memory_from_analysis(self, dialog, values):
        """The one write path: called only from ``MemoryConfirmDialog.confirmed``."""
        service = self.memory_service()
        run = dialog.run
        def create():
            memory = service.create_from_analysis(
                run.workspace_id, run.id, values["content"], kind=values["kind"],
                person_id=values.get("person_id"), valid_from=values.get("valid_from"),
                valid_until=values.get("valid_until"), confirmed=True)
            dialog.created_memory = memory
            self.status.setText(
                f"已保存 Memory {str(memory.id)[:8]}（{memory.kind} · {memory.source_type}）"
                "；本次分析记录与所有事实数据均未被修改。")
            return memory
        return self.run_operation(create)

    def cancel_run(self):
        view = self.current_view
        if view is None or self.analysis is None or not hasattr(self.analysis, "cancel_run"):
            return None
        def cancel():
            run = self.analysis.cancel_run(self.workspace_id, view.run.id)
            self.refresh(run.id)
            return run
        return self.run_operation(cancel)

    def delete_run(self):
        view = self.current_view
        if view is None or self.analysis is None or not hasattr(self.analysis, "delete_run"):
            return None
        if QMessageBox.question(self, "删除分析记录",
                                "只删除这条分析记录（含它的来源引用与快照），"
                                "人物、关系、事件、证据都不会被修改。是否继续？"
                                ) != QMessageBox.StandardButton.Yes:
            return None
        def remove():
            self.analysis.delete_run(self.workspace_id, view.run.id)
            self.refresh()
        return self.run_operation(remove)

    def open_provider_settings(self):
        if self.gateway is None:
            QMessageBox.warning(self, "Provider 不可用", "LLM 网关尚未装配：无法配置 Provider。")
            return None
        dialog = ProviderSettingsDialog(self.services, self)
        track_dialog(self, dialog)
        dialog.finished.connect(lambda _: self.refresh())
        dialog.show()
        return dialog

    def analyze_target(self, target_type, target_id, *, target_label=None, workspace_id=None):
        """Entry point used by People / Relationship / Event; user click only.

        ``workspace_id`` is the Workspace the *caller* is showing: the run is
        recorded there and this page is pointed at it, so the new record is always
        visible in the list the user is about to see.
        """
        if self.analysis is None:
            QMessageBox.warning(self, "分析不可用", "分析服务尚未装配：无法发起分析。")
            return None
        if self.gateway is not None and not self.gateway.config().providers:
            QMessageBox.warning(self, "尚未配置 Provider",
                                "请先在 Analysis 页的「Provider 设置」中添加一个 Provider。")
            return None
        if workspace_id is not None and workspace_id != self.workspace_id:
            self.workspace_id = workspace_id
            index = self.workspace_combo.findData(str(workspace_id))
            self.workspace_combo.blockSignals(True)
            try:
                if index >= 0:
                    self.workspace_combo.setCurrentIndex(index)
            finally:
                self.workspace_combo.blockSignals(False)
            self.refresh()          # the ledger shown must belong to that Workspace
        if self.workspace_id is None:
            self.load_workspaces()
        options = run_analysis(self.services, self.workspace_id, target_type, target_id,
                               target_label=target_label, parent=self, owner=self,
                               on_result=self.after_run, on_failed=self.refresh)
        if options is not None and hasattr(options, "confirmed"):
            # Runs after the dialog's own handler, i.e. once ``options.progress`` exists.
            options.confirmed.connect(lambda: self._lock_for_options(options))
        return options

    def after_run(self, run):
        """A finished run is recorded history: reload the ledger and select it."""
        self.refresh(run.id)
        view = next((item for item in self.runs if item.run.id == run.id), None)
        if view is not None:
            self.set_detail(view)
        return view

    # --------------------------------------------------------------- navigation
    def open_selected_target(self):
        view = self.current_view
        if view is None:
            return None
        return self.open_target(view.run.target_type, view.run.target_id)

    def open_target(self, target_type, target_id):
        """Ask the shell to show the analysed fact (Analysis only records it).

        EVIDENCE has no page of its own: it resolves to the Event that owns the
        evidence and the shell selects that very evidence there.
        """
        kind = {"PERSON": "person", "EVENT": "event", "RELATIONSHIP": "relationship",
                "EVIDENCE": "evidence"}.get(target_type)
        if kind is None:
            return None
        self.navigate_requested.emit(kind, target_id)
        return target_id

    def focus_run(self, run_id):
        """Select one recorded run (used by cross-page navigation)."""
        if self.analysis is None:
            return None
        self.refresh(run_id)
        return self.current_view


__all__ = ["AnalysisPage", "CANDIDATE_ACCEPTED", "CANDIDATE_HEADERS", "CANDIDATE_REJECTED",
           "FILTER_ALL", "HISTORY_HEADERS", "MEMORY_KINDS", "RESULT_SECTIONS", "SOURCE_KINDS",
           "SOURCE_LABELS", "TARGET_FILTERS", "run_operation", "source_label_text", "time_text"]
