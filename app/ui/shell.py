"""Main window shell widgets: top bar, read-only context bar, sidebar.

The shell is layout only. Slots in the top bar (workspace, search, actions) are
filled by later sub-phases; until then they stay empty instead of showing disabled
fake controls. The Context Bar is a read-only breadcrumb (no Back / Forward history,
decision 1 of Phase 11).
"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSizePolicy,
                               QVBoxLayout, QWidget)

from app.ui.design import icons
from app.ui.design.tokens import COLORS as C, EPISTEMICS, SPACE
from app.ui.design.widgets import EpistemicTag, icon_side

CRUMB_SEPARATOR = "›"


class TopBar(QFrame):
    """Workspace │ global search │ actions │ status, in one fixed row."""

    def __init__(self, brand: str, parent=None):
        super().__init__(parent)
        self.setObjectName("topBar")
        row = QHBoxLayout(self)
        margin = SPACE["lg"]
        row.setContentsMargins(margin, SPACE["sm"], margin, SPACE["sm"])
        row.setSpacing(SPACE["lg"])
        self.brand = QLabel(brand)
        self.brand.setObjectName("brand")
        row.addWidget(self.brand)
        self.workspace_slot = QHBoxLayout()
        self.workspace_slot.setSpacing(SPACE["sm"])
        row.addLayout(self.workspace_slot)
        self.search_slot = QHBoxLayout()
        self.search_slot.setSpacing(SPACE["sm"])
        row.addLayout(self.search_slot, 1)
        self.actions_slot = QHBoxLayout()
        self.actions_slot.setSpacing(SPACE["sm"])
        row.addLayout(self.actions_slot)
        self.status = QLabel("")
        self.status.setObjectName("topStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.status)

    def set_status(self, text: str) -> None:
        self.status.setText(text)


class ContextBar(QFrame):
    """Read-only breadcrumb ``Workspace › page › object``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextBar")
        self.row = QHBoxLayout(self)
        margin = SPACE["lg"]
        self.row.setContentsMargins(margin, SPACE["sm"], margin, SPACE["sm"])
        self.row.setSpacing(SPACE["sm"])
        self.crumbs: list[str] = []
        self.set_crumbs([])

    def set_crumbs(self, crumbs) -> None:
        self.crumbs = [str(item) for item in crumbs if str(item)]
        while self.row.count():
            item = self.row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        last = len(self.crumbs) - 1
        for index, text in enumerate(self.crumbs):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setObjectName("crumbCurrent" if index == last else "crumb")
            self.row.addWidget(label)
            if index != last:
                separator = QLabel(CRUMB_SEPARATOR)
                separator.setObjectName("crumb")
                self.row.addWidget(separator)
        self.row.addStretch(1)

    def text(self) -> str:
        return f" {CRUMB_SEPARATOR} ".join(self.crumbs)


NAV_ICONS = ("dashboard", "people", "events", "graph", "analysis", "settings")


class Sidebar(QFrame):
    """Navigation list + FACT / USER / DERIVED legend.

    The width is computed from the longest label (``QFontMetrics``) plus the icon and
    padding, then fixed, so navigation is never compressed by a narrow window and
    never wastes space at high DPI.
    """

    def __init__(self, titles, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        column = QVBoxLayout(self)
        column.setContentsMargins(SPACE["sm"], SPACE["md"], SPACE["sm"], SPACE["md"])
        column.setSpacing(SPACE["md"])
        self.navigation = QListWidget()
        self.navigation.setObjectName("mainNavigation")
        self.navigation.setAccessibleName("主导航")
        self.navigation.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side = icon_side(self.navigation, 1.3)
        self.navigation.setIconSize(QSize(side, side))
        for index, title in enumerate(titles):
            item = QListWidgetItem(icons.icon(NAV_ICONS[index % len(NAV_ICONS)], C["muted"],
                                              selected_color=C["accent"]), title)
            self.navigation.addItem(item)
        column.addWidget(self.navigation, 1)
        legend = QWidget()
        legend_column = QVBoxLayout(legend)
        legend_column.setContentsMargins(SPACE["sm"], 0, SPACE["sm"], 0)
        legend_column.setSpacing(SPACE["xs"])
        for item in EPISTEMICS:
            row = QHBoxLayout()
            row.setSpacing(SPACE["sm"])
            row.addWidget(EpistemicTag(item.key, legend))
            row.addStretch(1)
            legend_column.addLayout(row)
        column.addWidget(legend)
        metrics = self.navigation.fontMetrics()
        widest = max((metrics.horizontalAdvance(title) for title in titles), default=0)
        width = widest + side + SPACE["xl"] * 3
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
