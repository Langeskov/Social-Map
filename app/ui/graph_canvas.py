"""Shared static graph drawing: the rendering half of the Relationship graph.

This module is *rendering only*. It owns no service, stores nothing and never
infers anything: callers pass plain people and ``RelationshipView`` facts and get
items back. The same items draw the global Graph page and the person-centred Ego
Network, so there is exactly one implementation of nodes, edges, arrows and
state styling.

Invariant (same as the historic GraphPage): no animation, no timer, no
auto-layout transition, no score. The ring layouts are deterministic, so the
same facts render the same way.

Click handling lives on the *view*, not on the items: rebuilding clears the
scene (destroying items and their connections) while the view outlives every
rebuild. The dispatch target is an explicit ``click_handler`` exposing
``open_person`` / ``open_relationship``; without one the view falls back to
walking the parent chain for a page that provides them (the Graph page does).
"""
import logging
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainterPath, QPainterPathStroker, QPen, QPixmap,
    QPolygonF, QTransform)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPathItem, QGraphicsPolygonItem,
    QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView)

from app.ui.design.tokens import COLORS

logger = logging.getLogger(__name__)

NODE_DIAMETER = 120.0
RING_RADIUS = 260.0
ARROW_LENGTH = 16.0
ARROW_WIDTH = 7.0
EDGE_LABEL_OFFSET = 22.0
AVATAR_SIZE = 34
MIN_SCALE, MAX_SCALE = 0.25, 4.0

# Edge identity comes from the Design System: ACTIVE uses the neutral emphasis,
# PAUSED the single warning colour, ENDED / unknown the archived colour. Only the
# line style distinguishes deleted edges, so colour stays meaningful.
STATE_PENS = {
    "ACTIVE": (COLORS["accent"], Qt.PenStyle.SolidLine),
    "PAUSED": (COLORS["warning"], Qt.PenStyle.DashLine),
    "ENDED": (COLORS["archived"], Qt.PenStyle.DotLine),
}
UNKNOWN_STATE_PEN = (COLORS["archived"], Qt.PenStyle.DashDotLine)
DIRECTED_COLOUR = COLORS["accent"]
DELETED_COLOUR = COLORS["archived"]


# ------------------------------------------------------------------ geometry

def circular_positions(count, ring_radius=RING_RADIUS):
    """Static ring layout: deterministic, so the same facts render the same way."""
    if count <= 0:
        return []
    if count == 1:
        return [QPointF(0.0, 0.0)]
    step = 2.0 * math.pi / count
    return [QPointF(ring_radius * math.cos(index * step), ring_radius * math.sin(index * step))
            for index in range(count)]


def ego_positions(neighbor_count, ring_radius=RING_RADIUS):
    """Ring positions for the neighbours of a centred person; never ``(0, 0)``.

    The centre node of an Ego Network occupies the origin, so neighbours use a
    ring that starts at the top and never collides with the centre.
    """
    if neighbor_count <= 0:
        return []
    if neighbor_count == 1:
        return [QPointF(ring_radius, 0.0)]
    step = 2.0 * math.pi / neighbor_count
    return [QPointF(ring_radius * math.cos(index * step - math.pi / 2),
                    ring_radius * math.sin(index * step - math.pi / 2))
            for index in range(neighbor_count)]


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


# ---------------------------------------------------------------------- pens

def state_label(state):
    return state if state else "尚未生效"


def relationship_pen(current_state, deleted=False, directed=False):
    if deleted:
        colour, style = DELETED_COLOUR, Qt.PenStyle.DashLine
    else:
        colour, style = UNKNOWN_STATE_PEN if current_state is None else STATE_PENS.get(
            current_state, UNKNOWN_STATE_PEN)
    return QPen(QColor(colour), 3 if directed else 2, style)


# --------------------------------------------------------------------- items

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
        # Node identity uses the Entity layer colour; archived/deleted falls back to
        # the single archived colour so a deleted node never looks current.
        body = QColor(COLORS["raised"] if not self.deleted else COLORS["surface"])
        border = QColor(COLORS["archived"] if self.deleted else COLORS["entity"])
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(border, 3 if self.isSelected() else 1))
        painter.drawEllipse(QRectF(-self.radius, -self.radius, self.diameter, self.diameter))
        top = -12
        if self.avatar is not None and not self.avatar.isNull():
            scaled = self.avatar.scaled(AVATAR_SIZE, AVATAR_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(int(-scaled.width() / 2), int(-self.radius + 10), scaled)
            top = 8
        painter.setPen(QPen(QColor(COLORS["text"])))
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
            self.label.setBrush(QBrush(QColor(COLORS["muted"])))
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
        self.setPen(QPen(QColor(COLORS["text"]), 3))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setPen(relationship_pen(self.current_state, self.deleted, self.directed))
        super().hoverLeaveEvent(event)

    def click(self, event=None):
        """Click through the owning view (the view outlives every rebuild)."""
        return self.scene().views()[0].click_item(self, event) if self.scene() else None


# ---------------------------------------------------------------------- view

def graph_page_of(widget):
    """The page owning ``widget`` and offering ``open_person``, found via parents."""
    while widget is not None and not hasattr(widget, "open_person"):
        widget = widget.parentWidget()
    return widget


class GraphClicks:
    """Explicit click dispatch target for ``GraphView``.

    The Graph page passes itself; an embedded canvas (the Ego Network) passes
    this adapter so the drawing layer never needs to know its host.
    """

    def __init__(self, open_person, open_relationship):
        self._open_person = open_person
        self._open_relationship = open_relationship

    def open_person(self, item, event=None):
        return self._open_person(item, event)

    def open_relationship(self, item, event=None):
        return self._open_relationship(item, event)


class GraphView(QGraphicsView):
    """Plain view: left-drag pans, the wheel scales, clicks dispatch to the handler.

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
        #: Optional explicit dispatch target (``open_person`` / ``open_relationship``).
        self.click_handler = None

    def graph_page(self):
        return self.click_handler if self.click_handler is not None else graph_page_of(self)

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
        """Dispatch one item click to the handler; returns the opened detail, if any."""
        handler = self.graph_page()
        if handler is None or item is None:
            return None
        if isinstance(item, PersonNode):
            return handler.open_person(item, event)
        if isinstance(item, RelationshipEdge):
            return handler.open_relationship(item, event)
        return None


# -------------------------------------------------------------------- layout

def build_graph_items(people, counts, views, positions, *, avatar_for=None):
    """Compose nodes and edges from plain facts. Nothing is persisted.

    ``people``/``positions`` pair by index, ``counts`` maps person id to the number
    of drawn edges, and parallel relationships between one pair share the endpoint
    positions with a spread offset so every edge stays clickable.
    """
    layout = {}
    nodes = []
    for person, position in zip(people, positions):
        node = PersonNode(person, counts.get(person.id, 0),
                          avatar=avatar_for(person) if avatar_for is not None else None,
                          deleted=person.deleted_at is not None)
        node.setPos(position)
        nodes.append(node)
        layout[person.id] = position
    grouped = {}
    for view in views:
        key = tuple(sorted((str(view.person_a.id), str(view.person_b.id))))
        grouped.setdefault(key, []).append(view)
    edges = []
    for entries in grouped.values():
        for position, view in enumerate(entries):
            first, second = layout[view.person_a.id], layout[view.person_b.id]
            offset = 0.0 if len(entries) == 1 else (position - (len(entries) - 1) / 2) * 0.35
            edges.append(RelationshipEdge(view, first, second, offset=offset))
    return nodes, edges, layout


__all__ = ["ARROW_LENGTH", "ARROW_WIDTH", "AVATAR_SIZE", "DELETED_COLOUR", "DIRECTED_COLOUR",
           "EDGE_LABEL_OFFSET", "GraphClicks", "GraphView", "MAX_SCALE", "MIN_SCALE",
           "NODE_DIAMETER", "PersonNode", "QGraphicsScene", "RING_RADIUS", "RelationshipEdge",
           "STATE_PENS", "UNKNOWN_STATE_PEN", "arc_path", "arrow_polygon", "build_graph_items",
           "circular_positions", "edge_label_position", "ego_positions", "graph_page_of",
           "relationship_pen", "state_label"]
