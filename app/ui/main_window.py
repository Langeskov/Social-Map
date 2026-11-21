"""Main window shell: top bar, stable navigation, read-only context bar, pages.

Layout (Phase 11)::

    ┌──────────────────────────────────────────────────────────┐
    │ TopBar: brand │ workspace │ global search │ actions │ status │
    ├────────────┬─────────────────────────────────────────────┤
    │ Navigation │ ContextBar (breadcrumb: Workspace › page)   │
    │            ├─────────────────────────────────────────────┤
    │            │                   Page                      │
    └────────────┴─────────────────────────────────────────────┘

The navigation order is frozen and the sidebar keeps a computed fixed width, so no
page can compress it. Pages are constructed with the same ``services`` and every
cross-page jump goes through ``open_target``.
"""
import logging
from uuid import UUID

from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from app.config.settings import AppConfig
from app.ui.context import ContextPage, DetailWindowTracker, WorkspaceContext
from app.ui.design import theme
from app.ui.pages import create_pages
from app.ui.shell import ContextBar, Sidebar, TopBar

# Frozen page order: dashboard(0) people(1) events(2) graph(3) analysis(4) settings(5).
PAGE_TITLES = ["Dashboard · 工作台", "People · 人物", "Events · 事件",
               "Graph · 关系图", "Analysis · 分析", "Settings · 设置"]
PAGE_KEYS = ["dashboard", "people", "events", "graph", "analysis", "settings"]
DASHBOARD, PEOPLE, EVENTS, GRAPH, ANALYSIS, SETTINGS = range(6)

# The MainWindow minimum size is a layout floor, not a promise for every DPI and
# resolution combination: at extreme scale the page itself scrolls (decision 9).
MINIMUM_WIDTH = 800
MINIMUM_HEIGHT = 560
#: Longest object name shown in the breadcrumb before it is elided.
CRUMB_LIMIT = 40
BRAND_TEXT = "SOCIAL MAP"
BRAND_SUBTITLE = "RELATIONSHIP INTELLIGENCE"


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, services=None):
        super().__init__()
        theme.apply_design()
        self.config = config
        self.services = services
        self.setWindowTitle(config.title)
        self.resize(config.width, config.height)
        self.setMinimumSize(MINIMUM_WIDTH, MINIMUM_HEIGHT)
        root = QWidget()
        root.setObjectName("shellRoot")
        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.top_bar = TopBar(BRAND_TEXT, root)
        self.top_bar.setToolTip(f"{BRAND_SUBTITLE}")
        column.addWidget(self.top_bar)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar(PAGE_TITLES, root)
        self.navigation = self.sidebar.navigation
        body.addWidget(self.sidebar)
        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(0)
        self.context_bar = ContextBar(right)
        right_column.addWidget(self.context_bar)
        self.pages = QStackedWidget()
        for page in create_pages(config, services):
            self.pages.addWidget(page)
        right_column.addWidget(self.pages, 1)
        body.addWidget(right, 1)
        column.addLayout(body, 1)
        self.setCentralWidget(root)
        self.people_page = self.pages.widget(PEOPLE)
        self.events_page = self.pages.widget(EVENTS)
        self.graph_page = self.pages.widget(GRAPH)
        self.dashboard_page = self.pages.widget(DASHBOARD)
        self.analysis_page = self.pages.widget(ANALYSIS)
        # The Data Management page owns the four manual operations; the shell only
        # keeps a reference so a shutdown can wait for a running worker.
        self.settings_page = self.pages.widget(SETTINGS)
        self.workspace_context = self._build_context(services)
        self._build_global_search(services)
        self._bind_pages()
        self._connect_pages()
        self._build_shortcuts()
        self.navigation.currentRowChanged.connect(self.select_page)
        self.navigation.setCurrentRow(DASHBOARD)

    # ------------------------------------------------------------ global search
    def _build_global_search(self, services) -> None:
        """One search, in the top bar (Ctrl+F); pages no longer carry their own."""
        if services is None or getattr(services, "search", None) is None:
            self.global_search = None
            return
        from app.ui.search_panel import SearchPanel
        self.global_search = SearchPanel(services, self.top_bar, name="globalSearch", compact=True,
                                         toolbar=True,
                                         placeholder="全局搜索：人物 / 别称 / 事件 / 证据（Ctrl+F）")
        self.global_search.navigate_requested.connect(self.open_search_result)
        self.top_bar.search_slot.addWidget(self.global_search)
        if self.workspace_context is not None:
            self.global_search.set_workspace(self.workspace_context.workspace_id)

    def _build_shortcuts(self) -> None:
        from PySide6.QtGui import QKeySequence, QShortcut
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self.focus_search)

    def focus_search(self):
        """Ctrl+F focuses the global search, never a page-local duplicate."""
        if self.global_search is not None:
            return self.global_search.focus()
        page = self.people_page
        if hasattr(page, "search") and page.search is not None:
            page.search.setFocus()
            return page.search
        return None

    def open_search_result(self, result) -> None:
        """Route a global hit to the page that owns it (workspace first)."""
        workspace_id = getattr(result, "workspace_id", None)
        if workspace_id is not None and self.workspace_context is not None:
            self.workspace_context.set_workspace(workspace_id)
        entity_type = getattr(result, "entity_type", None)
        metadata = getattr(result, "metadata", {}) or {}
        if entity_type == "PERSON":
            self.open_target("person", result.entity_id)
        elif entity_type == "ALIAS":
            self.open_target("person", metadata.get("person_id"))
        elif entity_type == "EVENT":
            self.open_target("event", result.entity_id)
        elif entity_type == "EVIDENCE":
            self.open_target("event", metadata.get("event_id"))
        return result

    # ------------------------------------------------------- workspace context
    def _build_context(self, services) -> WorkspaceContext | None:
        """The one Workspace context, created only when services are wired."""
        if services is None or getattr(services, "workspaces", None) is None:
            return None
        context = WorkspaceContext(services.workspaces,
                                   getattr(services, "initial_workspace", None), self)
        context.refresh()
        self.top_bar.set_workspaces(context.workspaces, context.workspace_id)
        self.top_bar.workspace_combo.currentIndexChanged.connect(self._workspace_selected)
        context.changed.connect(self._context_changed)
        context.about_to_change.connect(lambda _old: self._close_detail_windows())
        return context

    def _bind_pages(self) -> None:
        """Give every page the shared context and its own window tracker."""
        if self.workspace_context is None:
            return
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            if not isinstance(page, ContextPage):
                continue
            tracker = DetailWindowTracker(page, getattr(page, "_dialogs", None),
                                          getattr(page, "_viewers", None))
            page.bind_workspace_context(self.workspace_context, tracker)

    def _workspace_selected(self, index: int) -> None:
        if self.workspace_context is None or index < 0:
            return
        if hasattr(self.analysis_page, "analysis_locked") and self.analysis_page.analysis_locked():
            return                                  # never switch under a live run
        data = self.top_bar.workspace_combo.itemData(index)
        if not data:
            return
        self.workspace_context.set_workspace(UUID(str(data)))

    def _context_changed(self, workspace_id) -> None:
        self.top_bar.show_workspace(workspace_id)
        if self.global_search is not None:
            self.global_search.clear()
            self.global_search.set_workspace(workspace_id)
        self._refresh_crumbs()

    def _close_detail_windows(self) -> None:
        """Close every non-modal window before the Workspace changes underneath it."""
        for index in range(self.pages.count()):
            tracker = getattr(self.pages.widget(index), "detail_windows", None)
            if tracker is not None:
                tracker.close_all()
        page = getattr(self, "settings_page", None)
        if page is not None and hasattr(page, "reset_context"):
            page.reset_context()

    # ------------------------------------------------------------------- shell
    def set_workspace_name(self, name: str) -> None:
        """Override the breadcrumb's Workspace segment (used when no context exists)."""
        self._workspace_name = name
        self._refresh_crumbs()

    def current_workspace_name(self) -> str:
        if self.workspace_context is not None:
            return self.workspace_context.current_name()
        return getattr(self, "_workspace_name", "")

    def refresh_crumbs(self) -> None:
        """Public: re-render the breadcrumb (pages call this after a selection)."""
        return self._refresh_crumbs()

    def _refresh_crumbs(self) -> None:
        index = self.pages.currentIndex()
        page = self.pages.currentWidget()
        crumbs = []
        workspace = self.current_workspace_name()
        if workspace:
            crumbs.append(workspace)
        if 0 <= index < len(PAGE_TITLES):
            crumbs.append(PAGE_TITLES[index].split(" · ")[0])
        elif page is not None:
            crumbs.append(page.objectName())
        # Object segment: only what the page actually has selected (Phase 11 §9).
        label = ""
        if page is not None and hasattr(page, "context_object"):
            try:
                label = page.context_object() or ""
            except Exception:                      # noqa: BLE001 - a crumb must not break nav
                label = ""
        if label:
            text = " ".join(str(label).split())
            crumbs.append(text if len(text) <= CRUMB_LIMIT else text[:CRUMB_LIMIT] + "…")
        self.context_bar.set_crumbs(crumbs)

    # ------------------------------------------------------------------ wiring
    def _connect_pages(self) -> None:
        events_page = self.events_page
        if hasattr(events_page, "events_changed"):
            # One Timeline: the People page's complete timeline (Events + relationship
            # state changes) is the only view that needs refreshing on an Event change.
            events_page.events_changed.connect(lambda workspace_id: (
                self.people_page.full_timeline.refresh()
                if self.people_page.workspace_id == workspace_id else None))
        for page in (self.dashboard_page, self.graph_page, self.people_page, self.analysis_page):
            if hasattr(page, "navigate_requested"):
                page.navigate_requested.connect(self.open_target)
        if hasattr(self.analysis_page, "workspace_lock_changed"):
            self.analysis_page.workspace_lock_changed.connect(self._workspace_lock_changed)

    def _workspace_lock_changed(self, locked: bool) -> None:
        """A running analysis locks the global Workspace selector (decision 6).

        The message is a ``lock``-level status: page switches cannot overwrite
        it, and unlocking restores the page-level status text.
        """
        self.top_bar.workspace_combo.setEnabled(not locked)
        self.top_bar.set_status("分析运行中：Workspace 已锁定" if locked else "", level="lock")

    # ------------------------------------------------------------------- shell
    def select_page(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            self.pages.setCurrentIndex(index)
            page = self.pages.currentWidget()
            if hasattr(page, "activate"):
                page.activate()
            name = page.objectName()
            self._refresh_crumbs()
            self.top_bar.set_status(f"当前页面 · {name}")
            logging.getLogger(__name__).debug("Navigation changed: %s", name)

    def closeEvent(self, event):
        """Let a running Data Management worker finish before the window goes away.

        A ``QThread`` destroyed while it is still running is undefined behaviour, and
        the backup / integrity / export workers are user-triggered and never
        cancelled: closing the window waits for them (bounded) instead of killing
        them mid-write.
        """
        page = getattr(self, "settings_page", None)
        if page is not None and hasattr(page, "wait_for_worker"):
            page.wait_for_worker()
        return super().closeEvent(event)

    def show_page(self, index: int) -> None:
        """Navigate like a user click (activation included)."""
        self.navigation.setCurrentRow(index)

    def open_target(self, target: str, entity_id) -> None:
        """Cross-page jump requested by Dashboard, Graph, People or Analysis.

        The target page owns how a detail is opened; the shell only switches page.
        ``evidence`` has no page of its own: the Analysis Sources list resolves the
        owning Event on the Events page and selects that evidence there.
        """
        entity_id = self._as_uuid(entity_id)
        if target == "person" and entity_id is not None:
            self.show_page(PEOPLE)
            self.people_page.focus_person(entity_id)
        elif target == "event" and entity_id is not None:
            self.show_page(EVENTS)
            self.events_page.focus_event(entity_id)
        elif target == "relationship" and entity_id is not None:
            self.show_page(PEOPLE)
            self.people_page.focus_relationship(entity_id)
        elif target == "evidence" and entity_id is not None:
            self.show_page(EVENTS)
            if hasattr(self.events_page, "focus_evidence"):
                self.events_page.focus_evidence(entity_id)
        elif target == "analysis_run" and entity_id is not None:
            self.show_page(ANALYSIS)
            if hasattr(self.analysis_page, "focus_run"):
                self.analysis_page.focus_run(entity_id)

    @staticmethod
    def _as_uuid(value):
        from uuid import UUID
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None
