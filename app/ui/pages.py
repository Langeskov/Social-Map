"""Honest empty states, not simulated business data."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget
from app.config.settings import AppConfig


class PlaceholderPage(QWidget):
    def __init__(self, key: str, title: str, description: str, parent=None):
        super().__init__(parent)
        self.setObjectName(key)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 32, 36, 32)
        layout.setSpacing(18)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        subtitle = QLabel(description)
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        panel = QFrame()
        panel.setObjectName("emptyPanel")
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(28, 36, 28, 36)
        label = QLabel("本页尚未实现业务功能")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(label)
        note = QLabel("当前为 Phase 10；本页功能尚未实现。没有加载或生成演示数据。")
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setObjectName("subtitle")
        inner.addWidget(note)
        layout.addWidget(panel)
        self.body_layout = layout
        layout.addStretch()


def create_pages(config: AppConfig, services=None) -> list[QWidget]:
    """The frozen page order: dashboard(0) people(1) events(2) graph(3) analysis(4) settings(5).

    Each page keeps its own ``objectName`` and is reached by the navigation list in
    ``MainWindow``. Analysis is a real page from Phase 8: it lists recorded runs and
    never calls a model on its own; without the analysis service it stays an honest
    placeholder instead of pretending to be wired.

    Settings is a real page from Phase 10: it owns the four manual data operations
    (integrity check, backup, restore verification, export) and runs none of them on
    open - it is constructed with the same ``services`` the other pages receive, and
    without them it explains which buttons are unavailable instead of failing.
    """
    from app.ui.people_page import PeoplePage
    from app.ui.events_page import EventsPage
    from app.ui.graph_page import GraphPage
    from app.ui.dashboard_page import DashboardPage
    dashboard = (DashboardPage(services)
                 if services is not None and getattr(services, "dashboard", None) is not None
                 else PlaceholderPage("dashboard", "Dashboard · 工作台",
                                      "工作台汇总尚未实现；人物、媒体和关系管理请进入 People。"))
    people = (PeoplePage(services) if services is not None
              else PlaceholderPage("people", "People · 人物", "人物业务服务尚未装配。"))
    events = (EventsPage(services)
              if services is not None and getattr(services, "events", None) is not None
              else PlaceholderPage("events", "Events · 事件", "事件业务服务尚未装配。"))
    graph = (GraphPage(services) if services is not None
             else PlaceholderPage("graph", "Graph · 关系图", "关系服务尚未装配。"))
    if services is not None and getattr(services, "analysis", None) is not None:
        from app.ui.analysis_page import AnalysisPage
        analysis = AnalysisPage(services)
    else:
        analysis = PlaceholderPage("analysis", "Analysis · 分析",
                                   "分析服务尚未装配：分析历史、结果查看与 Provider 设置不可用。")
    from app.ui.settings_page import SettingsPage
    return [
        dashboard,
        people,
        events,
        graph,
        analysis,
        SettingsPage(config, services),
    ]
