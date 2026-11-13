"""Main desktop shell with keyboard-accessible navigation."""
import logging
from uuid import UUID
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget, QVBoxLayout, QWidget
from app.config.settings import AppConfig
from app.ui.pages import create_pages

# Frozen page order: dashboard(0) people(1) events(2) graph(3) analysis(4) settings(5).
PAGE_TITLES = ["Dashboard · 工作台", "People · 人物", "Events · 事件",
               "Graph · 关系图", "Analysis · 分析", "Settings · 设置"]
DASHBOARD, PEOPLE, EVENTS, GRAPH, ANALYSIS, SETTINGS = range(6)

STYLE = """
QWidget { background: #16212d; color: #e5ecf3; font-size: 14px; }
QWidget#sidebar { background: #111b26; }
QLabel#brand { font-size: 20px; font-weight: 600; color: #8ed3e8; }
QLabel#pageTitle { font-size: 27px; font-weight: 600; }
QLabel#subtitle { color: #aebdce; }
QListWidget { background: #111b26; border: none; outline: none; }
QListWidget::item { padding: 15px 12px; margin: 3px 0; border-radius: 5px; }
QListWidget::item:selected { background: #264457; color: #b9edff; }
QListWidget::item:hover { background: #203445; }
QListWidget::item:focus { border: 1px solid #8ed3e8; }
QFrame#emptyPanel { background: #1c2c3b; border: 1px solid #354858; border-radius: 8px; }
QFrame#emptyPanel QLabel { background: transparent; }
QGroupBox { border: 1px solid #354858; border-radius: 8px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #aebdce; }
QGraphicsView#graphCanvas { background: #101a24; border: 1px solid #354858; border-radius: 8px; }
QStatusBar { color: #aebdce; }
"""

class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, services=None):
        super().__init__()
        self.setWindowTitle(config.title)
        self.resize(config.width, config.height)
        self.setMinimumSize(800, 560)
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(18, 28, 18, 20)
        brand = QLabel("RELATIONSHIP\nINTELLIGENCE")
        brand.setObjectName("brand")
        column.addWidget(brand)
        phase = QLabel("LOCAL WORKSPACE · PHASE 10")
        phase.setObjectName("subtitle")
        column.addWidget(phase)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("主导航")
        self.navigation.addItems(PAGE_TITLES)
        column.addWidget(self.navigation, 1)
        footer = QLabel("事实 ≠ 观察 ≠ 推断")
        footer.setObjectName("subtitle")
        column.addWidget(footer)
        self.pages = QStackedWidget()
        for page in create_pages(config, services):
            self.pages.addWidget(page)
        row.addWidget(sidebar)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)
        self.people_page = self.pages.widget(PEOPLE)
        self.events_page = self.pages.widget(EVENTS)
        self.graph_page = self.pages.widget(GRAPH)
        self.dashboard_page = self.pages.widget(DASHBOARD)
        self.analysis_page = self.pages.widget(ANALYSIS)
        # Phase 10: the Data Management page owns four manual operations; the shell
        # only keeps a reference so a shutdown can wait for a running worker.
        self.settings_page = self.pages.widget(SETTINGS)
        if hasattr(self.events_page, "events_changed"):
            self.events_page.events_changed.connect(lambda workspace_id: (
                self.people_page.event_timeline.refresh()
                if self.people_page.workspace_id == workspace_id else None))
        dashboard = self.dashboard_page
        if hasattr(dashboard, "navigate_requested"):
            dashboard.navigate_requested.connect(self.open_target)
        graph = self.graph_page
        if hasattr(graph, "navigate_requested"):
            graph.navigate_requested.connect(self.open_target)
        people = self.people_page
        if hasattr(people, "navigate_requested"):
            people.navigate_requested.connect(self.open_target)
        analysis = self.analysis_page
        if hasattr(analysis, "navigate_requested"):
            analysis.navigate_requested.connect(self.open_target)
        self.navigation.currentRowChanged.connect(self.select_page)
        self.navigation.setCurrentRow(DASHBOARD)

    # ------------------------------------------------------------------- shell

    def select_page(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            self.pages.setCurrentIndex(index)
            page = self.pages.currentWidget()
            if hasattr(page, "activate"):
                page.activate()
            name = page.objectName()
            self.statusBar().showMessage(
                f"Phase 10 · {name} · 工作台 / 人物与记忆 / 媒体 / 关系历史 / 事件与时间线 / 证据 / 统一搜索 / 关系图 / 分析复核（仅在你点击后调用模型）/ 数据管理（完整性检查 / 备份 / 校验 / 导出，全部手动触发）")
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
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None
