"""Relationship Graph page: a plain static QGraphicsView over the fact layer.

The graph is a *rendering* of the database, never a second truth:

* nodes come from ``services.people.list`` and edges from
  ``services.relationships.list_by_workspace``; nothing is persisted, no table
  and no cache is created, and every reload re-reads the services;
* archived people are hidden unless the toggle asks for them;
* soft-deleted relationships are hidden unless the deleted toggle asks for them;
* the state of an edge is the *current* state (``RelationshipView``), so a planned
  future state is never presented as today's state;
* a node shows only facts - display name, avatar reference and the number of
  relationships drawn. No inference, personality, affinity or score ever appears.

The layout is a static ring and the arrow heads are static polygons: no animation,
no particle, no auto-layout transition and no timer exists in this module.

Click handling lives on the *view*, not on the items: rebuilding the graph clears
the scene, which destroys the items (and with them any signal connection they own),
while the view and the page outlive every rebuild.
"""
import logging
import math
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QPainterPath, QPainterPathStroker, QPen, QPixmap,
    QPolygonF, QTransform)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGraphicsItem, QGraphicsPathItem,
    QGraphicsPolygonItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget)

from app.domain.entities import Person
from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.event_detail import plain_label
from app.ui.person_dialog import PersonDialog
from app.ui.timeline_panel import RelationshipDetailDialog

logger = logging.getLogger(__name__)

ALL_TYPES = "全部"
NODE_DIAMETER = 120.0
RING_RADIUS = 260.0
ARROW_LENGTH = 16.0
ARROW_WIDTH = 7.0
EDGE_LABEL_OFFSET = 22.0
AVATAR_SIZE = 34
MIN_SCALE, MAX_SCALE = 0.25, 4.0

STATE_PENS = {
    "ACTIVE": ("#67d6a8", Qt.PenStyle.SolidLine),
    "PAUSED": ("#e0b464", Qt.PenStyle.DashLine),
    "ENDED": ("#7d8b99", Qt.PenStyle.DotLine),
}
UNKNOWN_STATE_PEN = ("#7d8b99", Qt.PenStyle.DashDotLine)
DIRECTED_COLOUR = "#8ed3e8"
DELETED_COLOUR = "#b06a76"


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Graph UI", title="关系图操作未完成")


def circular_positions(count, ring_radius=RING_RADIUS):
    """Static ring layout: deterministic, so the same facts render the same way."""
    if count <= 0:
        return []
    if count == 1:
        return [QPointF(0.0, 0.0)]
    step = 2.0 * math.pi / count
    return [QPointF(ring_radius * math.cos(index * step), ring_radius * math.sin(index * step))
            for index in range(count)]


def arc_path(start, end, offset=0.35):
    """A quadratic arc: parallel relationships between one pair stay clickable."""
    path = QPainterPath(start)
    if offset == 0.0:
        path.lineTo(end)
        return path
    control = QPointF((start.x() + end.x()) / 2 + (end.y() - start.y()) * offset,
                      (start.y() + end.y()) / 2 - (end.x() - start.x()) * offset)
    path.quadTo(control, end)
    return path


def edge_label_position(start, end, offset=EDGE_LABEL_OFFSET):
    middle = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
    length = math.hypot(end.x() - start.x(), end.y() - start.y())
    if length == 0:
        return middle
    return QPointF(middle.x() - (end.y() - start.y()) / length * offset,
                   middle.y() + (end.x() - start.x()) / length * offset)


def arrow_polygon(tip, direction):
    """Static arrow head at ``tip`` pointing along the unit vector ``direction``."""
    base = QPointF(tip.x() - direction.x() * ARROW_LENGTH, tip.y() - direction.y() * ARROW_LENGTH)
    normal = QPointF(-direction.y(), direction.x())
    return QPolygonF([tip,
                      QPointF(base.x() + normal.x() * ARROW_WIDTH, base.y() + normal.y() * ARROW_WIDTH),
                      QPointF(base.x() - normal.x() * ARROW_WIDTH, base.y() - normal.y() * ARROW_WIDTH)])


def state_label(state):
    return state if state else "尚未生效"


def relationship_pen(current_state, deleted=False, directed=False):
    if deleted:
        colour, style = DELETED_COLOUR, Qt.PenStyle.DashLine
    else:
        colour, style = UNKNOWN_STATE_PEN if current_state is None else STATE_PENS.get(
            current_state, UNKNOWN_STATE_PEN)
    return QPen(QColor(colour), 3 if directed else 2, style)


class GraphView(QGraphicsView):
    """Plain view: left-drag pans, the wheel scales, clicks dispatch to the page.

    No animation and no timer: the wheel handler scales once per event, and a click
    is resolved synchronously against the item under the cursor.
    """

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(self.renderHints().Antialiasing, True)
        self.setRenderHint(self.renderHints().TextAntialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumHeight(320)
        self.setObjectName("graphCanvas")

    def graph_page(self):
        return graph_page_of(self)

    def wheelEvent(self, event):
        self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)

    def zoom_by(self, factor):
        """Deterministic scale step; clamped so the graph stays usable."""
        target = self.transform().m11() * factor
        if not MIN_SCALE <= target <= MAX_SCALE:
            return self.transform().m11()
        self.scale(factor, factor)
        return self.transform().m11()

    def reset_zoom(self):
        self.setTransform(QTransform())
        return self.transform().m11()

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        super().mouseReleaseEvent(event)
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self.click_item(item, event)

    def click_item(self, item, event=None):
        """Dispatch one item click to the page; returns the opened detail, if any."""
        page = self.graph_page()
        if page is None or item is None:
            return None
        if isinstance(item, PersonNode):
            return page.open_person(item, event)
        if isinstance(item, RelationshipEdge):
            return page.open_relationship(item, event)
        return None


def graph_page_of(widget):
    """The GraphPage owning ``widget``, found by walking the parent chain."""
    while widget is not None and not hasattr(widget, "open_person"):
        widget = widget.parentWidget()
    return widget


class PersonNode(QGraphicsItem):
    """One Person = one static node: name, avatar (if any) and relationship count."""

    def __init__(self, person, count, *, avatar=None, deleted=False, diameter=NODE_DIAMETER):
        super().__init__()
        self.person = person
        self.person_id = person.id
        self.display_name = person.display_name
        self.count = int(count)
        self.deleted = bool(deleted)
        self.avatar = avatar
        self.diameter = float(diameter)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(2)
        self.setToolTip(f"{self.display_name} · 关系 {self.count}" + (" · 已归档" if self.deleted else ""))

    @property
    def radius(self):
        return self.diameter / 2

    def boundingRect(self):
        return QRectF(-self.radius - 4, -self.radius - 4, self.diameter + 8, self.diameter + 8)

    def label_text(self):
        return self.display_name if len(self.display_name) <= 10 else self.display_name[:10] + "…"

    def paint(self, painter, option=None, widget=None):
        # ``painter is None`` keeps the label/count composition callable in tests.
        if painter is None:
            return None
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        body = QColor("#392b2f") if self.deleted else QColor("#2b4152")
        border = QColor("#a3727c") if self.deleted else QColor("#8ed3e8")
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(border, 3 if self.isSelected() else 1))
        painter.drawEllipse(QRectF(-self.radius, -self.radius, self.diameter, self.diameter))
        top = -12
        if self.avatar is not None and not self.avatar.isNull():
            scaled = self.avatar.scaled(AVATAR_SIZE, AVATAR_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(int(-scaled.width() / 2), int(-self.radius + 10), scaled)
            top = 8
        painter.setPen(QPen(QColor("#e5ecf3")))
        font = QFont()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(-self.radius + 4, top, self.diameter - 8, 30),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), self.label_text())
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(QRectF(-self.radius + 4, self.radius - 30, self.diameter - 8, 22),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                         f"关系 {self.count}")
        if self.deleted:
            painter.drawText(QRectF(-self.radius + 4, self.radius - 52, self.diameter - 8, 20),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom), "已归档")
        return None

    def click(self, event=None):
        """Click through the owning view (the view outlives every rebuild)."""
        return self.scene().views()[0].click_item(self, event) if self.scene() else None


class RelationshipEdge(QGraphicsPathItem):
    """One Relationship = one edge: type + current state, arrow when DIRECTED."""

    def __init__(self, view, start, end, *, offset=0.35):
        super().__init__()
        self.view = view
        self.entity = view.relationship
        self.person_a_id = self.entity.person_a_id
        self.person_b_id = self.entity.person_b_id
        self.directed = self.entity.directionality == "DIRECTED"
        self.deleted = self.entity.deleted_at is not None
        self.current_state = view.current_state
        self.path = arc_path(start, end, offset)
        self.setPath(self.path)
        self.setZValue(1)
        self.arrow = None
        self.label = None
        self.setPen(relationship_pen(self.current_state, self.deleted, self.directed))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setToolTip(self.legend_text())
        if self.directed and start != end:
            length = math.hypot(end.x() - start.x(), end.y() - start.y())
            direction = QPointF((end.x() - start.x()) / length, (end.y() - start.y()) / length)
            self.arrow = QGraphicsPolygonItem(arrow_polygon(end, direction), self)
            self.arrow.setBrush(QBrush(QColor(DIRECTED_COLOUR)))
            self.arrow.setPen(QPen(QColor(DIRECTED_COLOUR)))
            self.arrow.setZValue(1)
        if start != end:
            self.label = QGraphicsSimpleTextItem(self.legend_text(), self)
            self.label.setBrush(QBrush(QColor("#dbe6f0")))
            self.label.setPos(edge_label_position(start, end))

    def legend_text(self):
        parts = [self.entity.relationship_type,
                 "有向" if self.directed else "无向",
                 state_label(self.current_state)]
        if self.deleted:
            parts.append("已删除")
        return " · ".join(parts)

    # A thin arc is hard to hit: the clickable shape is widened, never the drawing.
    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(16)
        return stroker.createStroke(self.path)

    def connector_ids(self):
        return (self.person_a_id, self.person_b_id)

    def hoverEnterEvent(self, event):
        self.setPen(QPen(QColor("#f2f7fb"), 3))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setPen(relationship_pen(self.current_state, self.deleted, self.directed))
        super().hoverLeaveEvent(event)

    def click(self, event=None):
        """Click through the owning view (the view outlives every rebuild)."""
        return self.scene().views()[0].click_item(self, event) if self.scene() else None


class GraphPage(QWidget):
    navigate_requested = Signal(str, object)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("graph")
        self.services = services
        self.relationship_service = getattr(services, "relationships", None)
        self.workspace_id = services.initial_workspace.id
        self._dialogs = []
        self.nodes = []
        self.edges = []
        self.layout = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = plain_label("Graph · 关系图（数据库事实的可视化，不是第二套数据）")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("当前 Workspace"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("graphWorkspaceSelector")
        self.workspace_combo.setMinimumWidth(160)
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        controls.addWidget(self.workspace_combo, 1)
        controls.addWidget(QLabel("关系类型"))
        self.type_filter = QComboBox()
        self.type_filter.setObjectName("graphTypeFilter")
        self.type_filter.addItem(ALL_TYPES, None)
        self.type_filter.currentIndexChanged.connect(lambda _: self.rebuild_graph())
        controls.addWidget(self.type_filter)
        self.show_archived = QCheckBox("显示已归档人物")
        self.show_archived.setObjectName("graphShowArchived")
        self.show_archived.toggled.connect(lambda _: self.refresh())
        controls.addWidget(self.show_archived)
        self.show_deleted = QCheckBox("显示已删除关系")
        self.show_deleted.setObjectName("graphShowDeleted")
        self.show_deleted.toggled.connect(lambda _: self.refresh())
        controls.addWidget(self.show_deleted)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("graphRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)
        self.scene = QGraphicsScene(self)
        self.view = GraphView(self.scene, self)
        layout.addWidget(self.view, 1)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.load_workspaces()
        self.refresh()

    # ------------------------------------------------------------------ service

    def run_operation(self, callback):
        return run_operation(self, callback)

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
        self.workspace_id = UUID(data) if data else None
        self.refresh()

    def activate(self):
        self.load_workspaces()
        self.refresh()

    def people_for_graph(self):
        """Facts only: current people, plus archived people when the toggle asks.

        ``PersonService.list(include_deleted=True)`` is the management/audit view, so
        an archived person is drawable even when no relationship references it. The
        endpoint fallback below only covers people an archived-endpoint relationship
        mentions, which is a subset of that list.
        """
        if self.show_archived.isChecked():
            people = list(self.services.people.list(self.workspace_id, include_deleted=True))
        else:
            people = [person for person in self.services.people.list(self.workspace_id)
                      if person.deleted_at is None]
        known = {person.id: person for person in people}
        for view in self.relationships_for_graph():
            for endpoint in (view.person_a, view.person_b):
                if endpoint.deleted_at is None or endpoint.id in known:
                    continue
                known[endpoint.id] = Person(workspace_id=self.workspace_id,
                                            display_name=endpoint.display_name,
                                            id=endpoint.id, deleted_at=endpoint.deleted_at)
        return list(known.values())

    def relationships_for_graph(self):
        """The Relationship facts to draw, honouring both toggles.

        ``RelationshipService.list_by_workspace`` treats an archived *endpoint* as an
        invisible Relationship, so the archived toggle has to widen the service read
        too. That widening never re-admits a row whose own ``deleted_at`` is set: a
        soft-deleted Relationship still needs the deleted toggle.
        """
        if self.relationship_service is None:
            return []
        show_archived = self.show_archived.isChecked()
        show_deleted = self.show_deleted.isChecked()
        views = self.relationship_service.list_by_workspace(
            self.workspace_id, include_deleted=show_deleted or show_archived)
        return [view for view in views if show_deleted or view.relationship.deleted_at is None]

    def avatar_for(self, person):
        # An archived person has no readable detail any more: no avatar, no crash.
        if person.deleted_at is not None:
            return None
        try:
            detail = self.services.people.detail(self.workspace_id, person.id)
        except (DomainError, DatabaseError, OSError, ValueError) as exc:
            logger.warning("Graph avatar unavailable: type=%s", type(exc).__name__)
            return None
        for image in getattr(detail, "images", ()):
            if image.link.role != "AVATAR":
                continue
            try:
                pixmap = QPixmap(str(self.services.media.original_path(self.workspace_id, image.media.id)))
            except (DomainError, DatabaseError, OSError, ValueError) as exc:
                logger.warning("Graph avatar file unavailable: type=%s", type(exc).__name__)
                return None
            return None if pixmap.isNull() else pixmap
        return None

    # -------------------------------------------------------------------- graph

    def refresh(self):
        """Refill the type filter from the facts, then redraw."""
        selected_type = self.type_filter.currentData()
        self.type_filter.blockSignals(True)
        try:
            self.type_filter.clear()
            self.type_filter.addItem(ALL_TYPES, None)
            for kind in sorted({view.relationship.relationship_type
                                for view in self.relationships_for_graph()}):
                self.type_filter.addItem(kind, kind)
            index = self.type_filter.findData(selected_type)
            self.type_filter.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.type_filter.blockSignals(False)
        return self.rebuild_graph()

    def rebuild_graph(self):
        """Draw the current facts; the previous drawing is dropped, nothing is saved."""
        self.nodes, self.edges, self.layout = [], [], {}
        self.scene.clear()
        if self.workspace_id is None:
            self.status.setText("请先选择 Workspace。")
            return []
        if self.relationship_service is None:
            self.status.setText("关系服务尚未装配：关系图不可用。")
            return []

        def build():
            people = self.people_for_graph()
            visible_ids = {person.id for person in people}
            kind = self.type_filter.currentData()
            views = [view for view in self.relationships_for_graph()
                     if (kind is None or view.relationship.relationship_type == kind)
                     and view.person_a.id in visible_ids and view.person_b.id in visible_ids]
            positions = circular_positions(len(people))
            counts = {person.id: 0 for person in people}
            for view in views:
                counts[view.person_a.id] = counts.get(view.person_a.id, 0) + 1
                counts[view.person_b.id] = counts.get(view.person_b.id, 0) + 1
            for person, position in zip(people, positions):
                node = PersonNode(person, counts.get(person.id, 0), avatar=self.avatar_for(person),
                                  deleted=person.deleted_at is not None)
                node.setPos(position)
                self.scene.addItem(node)
                self.nodes.append(node)
                self.layout[person.id] = position
            grouped = {}
            for view in views:
                key = tuple(sorted((str(view.person_a.id), str(view.person_b.id))))
                grouped.setdefault(key, []).append(view)
            for entries in grouped.values():
                for position, view in enumerate(entries):
                    first, second = self.layout[view.person_a.id], self.layout[view.person_b.id]
                    offset = 0.0 if len(entries) == 1 else (position - (len(entries) - 1) / 2) * 0.35
                    edge = RelationshipEdge(view, first, second, offset=offset)
                    self.scene.addItem(edge)
                    self.edges.append(edge)
            if not people:
                self.status.setText("当前 Workspace 没有可显示的人物：请先创建人物。未生成演示数据。")
            else:
                widened = "（含已归档人物 / 已删除关系）" if (self.show_archived.isChecked()
                                                          or self.show_deleted.isChecked()) else ""
                self.status.setText(f"关系图 · {len(self.nodes)} 个节点 · {len(self.edges)} 条关系"
                                    f"{widened} · 数据来自人物与关系服务，未新增任何表")
            self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40))
            return self.edges
        return self.run_operation(build)

    def graph_state(self):
        """A comparable snapshot of what is currently drawn (facts only)."""
        return {
            "nodes": sorted(str(node.person_id) for node in self.nodes),
            "labels": sorted(node.display_name for node in self.nodes),
            "edges": sorted((str(edge.entity.id), edge.entity.relationship_type, edge.current_state,
                             edge.entity.directionality) for edge in self.edges),
            "arrows": sum(1 for edge in self.edges if edge.arrow is not None),
        }

    # --------------------------------------------------------------- navigation

    def selected_node(self):
        for item in self.scene.selectedItems():
            if isinstance(item, PersonNode):
                return item
        return None

    def selected_edge(self):
        for item in self.scene.selectedItems():
            if isinstance(item, RelationshipEdge):
                return item
        return None

    def node_for_person(self, person_id):
        return next((node for node in self.nodes if node.person_id == person_id), None)

    def edges_for_person(self, person_id):
        return [edge for edge in self.edges if person_id in edge.connector_ids()]

    def track_dialog(self, dialog):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialogs.append(dialog)
        dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
        return dialog

    def show_person_dialog(self, person):
        return self.track_dialog(PersonDialog(person, self))

    def open_person(self, node_or_person, event=None):
        """Clicking a node: ask the shell to select the person and show the detail."""
        person = getattr(node_or_person, "person", node_or_person)
        self.navigate_requested.emit("person", getattr(person, "id", person))
        dialog = self.show_person_dialog(person)
        dialog.show()
        return dialog

    def open_relationship(self, edge_or_view, event=None):
        """Clicking an edge: open the read-only Relationship detail."""
        view = getattr(edge_or_view, "view", edge_or_view)
        self.navigate_requested.emit("relationship", view.relationship.id)

        def show():
            dialog = RelationshipDetailDialog(self.services, self.workspace_id, view.relationship.id, self)
            dialog.load(self.workspace_id, view.relationship.id)
            self.track_dialog(dialog)
            dialog.show()
            return dialog
        return self.run_operation(show)

    def focus_person(self, person_id):
        """Select one person's node and its edges without creating a second source."""
        node = self.node_for_person(person_id)
        if node is None:
            return None
        self.scene.clearSelection()
        node.setSelected(True)
        for edge in self.edges_for_person(person_id):
            edge.setSelected(True)
        self.view.centerOn(node)
        return node


__all__ = ["GraphPage", "GraphView", "PersonNode", "RelationshipEdge", "ALL_TYPES", "arc_path",
           "arrow_polygon", "circular_positions", "edge_label_position", "graph_page_of",
           "relationship_pen", "state_label", "run_operation"]
