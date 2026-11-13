"""Unified search box + result list, shared by Dashboard, People and any page.

The widget is a pure view over ``SearchService``:

* the raw query text is forwarded unchanged - the service owns FTS5 safety, so
  operators such as ``*``, ``"``, ``-``, ``AND``/``OR``/``NOT`` are never
  rewritten (or escaped) here;
* every row shows type / title / time / snippet, and clicking one asks the page
  above to navigate to the real detail (Person, Event or the parent Event of an
  Alias / Evidence);
* a rebuild button calls ``rebuild_index`` and a checkbox widens the query with
  ``include_deleted=True``;
* a missing Search service or an unavailable FTS5 index produces a readable
  status line, never a crash.
"""
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.event_detail import plain_label, time_text, ROLE

logger = logging.getLogger(__name__)

ENTITY_LABELS = {"PERSON": "人物", "ALIAS": "别称", "EVENT": "事件", "EVIDENCE": "证据"}
DEBOUNCE_MS = 200
RESULT_LIMIT = 25


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Search UI", title="搜索未完成")


def entity_label(entity_type):
    return ENTITY_LABELS.get(entity_type, entity_type or "—")


def result_title(result):
    suffix = " · 已删除" if result.metadata.get("deleted") else ""
    return entity_label(result.entity_type) + " · " + (result.title or "—") + suffix


def result_time(result):
    """Occurred time of a hit; ``—`` when the projection carries none."""
    occurred = result.metadata.get("occurred_at")
    if occurred is None:
        return "—"
    if hasattr(occurred, "tzinfo"):
        return time_text(occurred)
    if hasattr(occurred, "isoformat"):
        return occurred.isoformat() + " · 日期"
    return str(occurred)


def result_snippet(result):
    text = " ".join((result.snippet or "").split())
    return text or "—"


class SearchPanel(QWidget):
    """One input box, one result list, one navigation signal."""

    navigate_requested = Signal(object)

    def __init__(self, services, parent=None, *, placeholder="统一搜索：人物 / 别称 / 事件 / 证据",
                 name="unifiedSearch", compact=False):
        super().__init__(parent)
        self.setObjectName(name)
        self.services = services
        self.service = getattr(services, "search", None)
        self.workspace_id = getattr(getattr(services, "initial_workspace", None), "id", None)
        self.results = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = plain_label("Search · 统一搜索（人物 / 别称 / 事件 / 证据）")
        layout.addWidget(self.heading)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setObjectName("searchInput")
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DEBOUNCE_MS)
        self.timer.timeout.connect(self.refresh)
        self.input.textChanged.connect(lambda _: self.timer.start())
        row.addWidget(self.input, 1)
        self.button = QPushButton("搜索")
        self.button.setObjectName("searchButton")
        self.button.clicked.connect(self.refresh)
        row.addWidget(self.button)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.include_deleted = QCheckBox("显示已删除")
        self.include_deleted.setObjectName("searchIncludeDeleted")
        self.include_deleted.toggled.connect(lambda _: self.refresh())
        controls.addWidget(self.include_deleted)
        self.rebuild_button = QPushButton("重建搜索索引")
        self.rebuild_button.setObjectName("searchRebuild")
        self.rebuild_button.clicked.connect(self.rebuild_index)
        controls.addWidget(self.rebuild_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.result_list = QListWidget()
        self.result_list.setObjectName("searchResults")
        self.result_list.setMinimumHeight(90 if compact else 160)
        self.result_list.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.result_list.itemClicked.connect(self.open_item)
        layout.addWidget(self.result_list)
        row = QHBoxLayout()
        self.open_button = QPushButton("打开所选结果")
        self.open_button.setObjectName("searchOpen")
        self.open_button.clicked.connect(self.open_selected)
        row.addWidget(self.open_button)
        row.addStretch()
        layout.addLayout(row)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.update_controls()
        self.report_state()

    # ------------------------------------------------------------------ helpers

    def run_operation(self, callback):
        return run_operation(self, callback)

    def set_workspace(self, workspace_id):
        """Follow the page's current Workspace; results are never mixed."""
        if workspace_id == self.workspace_id:
            return
        self.workspace_id = workspace_id
        self.results = []
        self.result_list.clear()
        self.report_state()

    def query_text(self):
        return self.input.text()

    def selected_result(self):
        item = self.result_list.currentItem()
        return item.data(ROLE) if item is not None else None

    def update_controls(self):
        available = self.service is not None and self.workspace_id is not None
        self.input.setEnabled(available)
        self.button.setEnabled(available)
        self.rebuild_button.setEnabled(self.service is not None)
        self.include_deleted.setEnabled(available)
        self.open_button.setEnabled(self.selected_result() is not None)

    def report_state(self, message=""):
        if self.service is None:
            self.status.setText("搜索服务尚未装配：统一搜索不可用。")
            return
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace，再使用统一搜索。")
            return
        if message:
            self.status.setText(message)
            return
        try:
            available = self.service.index_available()
        except (DomainError, DatabaseError, ValueError) as exc:
            logger.warning("Search index state unavailable: type=%s", type(exc).__name__)
            available = False
        if available:
            self.status.setText("输入关键词后回车搜索；支持 * \" - AND OR NOT，语法由搜索服务处理。")
        else:
            self.status.setText("FTS5 搜索索引不可用：可尝试“重建搜索索引”，或检查数据库迁移。")

    # ------------------------------------------------------------------ actions

    def refresh(self):
        self.timer.stop()
        self.results = []
        self.result_list.blockSignals(True)
        self.result_list.clear()
        self.result_list.blockSignals(False)
        self.update_controls()
        if self.service is None or self.workspace_id is None:
            self.report_state()
            return []
        query = self.query_text()
        if not query.strip():
            self.report_state("搜索词为空：未执行搜索。")
            return []

        def load():
            results = list(self.service.search(self.workspace_id, query,
                                              include_deleted=self.include_deleted.isChecked(),
                                              limit=RESULT_LIMIT))
            self.results = results
            self.result_list.blockSignals(True)
            try:
                self.result_list.clear()
                for result in results:
                    item = QListWidgetItem(f"{result_title(result)}\n"
                                           f"{result_time(result)} · {result_snippet(result)}")
                    item.setData(ROLE, result)
                    self.result_list.addItem(item)
            finally:
                self.result_list.blockSignals(False)
            self.update_controls()
            scope = "（含已删除）" if self.include_deleted.isChecked() else ""
            self.status.setText(f"搜索结果 · {len(results)} 条{scope}"
                                if results else "没有匹配结果。搜索语法由搜索服务处理，未做本地过滤。")
            return results
        return self.run_operation(load)

    def rebuild_index(self):
        if self.service is None:
            self.report_state()
            return None

        def rebuild():
            count = self.service.rebuild_index(self.workspace_id)
            # Clear the query first: a pending refresh must not overwrite the report.
            self.input.blockSignals(True)
            self.input.clear()
            self.input.blockSignals(False)
            self.refresh()
            self.report_state(f"搜索索引已重建 · {count} 篇文档。")
            return count
        return self.run_operation(rebuild)

    def open_selected(self):
        result = self.selected_result()
        return None if result is None else self.activate_result(result)

    def open_item(self, item):
        result = item.data(ROLE) if item is not None else None
        return None if result is None else self.activate_result(result)

    def activate_result(self, result):
        """Emit the navigation request; the page above owns page switching."""
        self.navigate_requested.emit(result)
        return result

    def focus(self):
        self.input.setFocus()
        return self.input
