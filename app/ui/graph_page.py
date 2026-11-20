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

The drawing primitives (``PersonNode``, ``RelationshipEdge``, ``GraphView``,
geometry and state pens) live in :mod:`app.ui.graph_canvas` so the Ego Network on
the People workspace draws with exactly the same implementation. The layout is a
static ring and the arrow heads are static polygons: no animation, no particle,
no auto-layout transition and no timer exists in this module.

Click handling lives on the *view*, not on the items: rebuilding the graph clears
the scene, which destroys the items (and with them any signal connection they own),
while the view and the page outlive every rebuild. This page registers itself as
the explicit click handler.
"""
import logging
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGraphicsScene, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget)

from app.domain.entities import Person
from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.context import ContextPage
from app.ui.design.tokens import COLORS
from app.ui.event_detail import plain_label
from app.ui.graph_canvas import (PersonNode, RelationshipEdge, GraphView, build_graph_items,
    circular_positions)
from app.ui.person_dialog import PersonDialog
from app.ui.timeline_panel import RelationshipDetailDialog

logger = logging.getLogger(__name__)

ALL_TYPES = "全部"


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Graph UI", title="关系图操作未完成")


class GraphPage(ContextPage, QWidget):
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
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("graphRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)
        # Filters live in a side panel so the canvas keeps the room (Phase 11).
        body = QHBoxLayout()
        self.filters_panel = QFrame()
        self.filters_panel.setObjectName("graphFilters")
        filters = QVBoxLayout(self.filters_panel)
        filters.addWidget(plain_label("Filters · 过滤"))
        filters.addWidget(QLabel("关系类型"))
        self.type_filter = QComboBox()
        self.type_filter.setObjectName("graphTypeFilter")
        self.type_filter.addItem(ALL_TYPES, None)
        self.type_filter.currentIndexChanged.connect(lambda _: self.rebuild_graph())
        filters.addWidget(self.type_filter)
        filters.addWidget(QLabel("关系状态"))
        self.state_filter = QComboBox()
        self.state_filter.setObjectName("graphStateFilter")
        self.state_filter.addItem("全部状态", None)
        for state in ("ACTIVE", "PAUSED", "ENDED"):
            self.state_filter.addItem(state, state)
        self.state_filter.currentIndexChanged.connect(lambda _: self.refresh())
        filters.addWidget(self.state_filter)
        self.show_archived = QCheckBox("显示已归档人物")
        self.show_archived.setObjectName("graphShowArchived")
        self.show_archived.toggled.connect(lambda _: self.refresh())
        filters.addWidget(self.show_archived)
        self.show_deleted = QCheckBox("显示已删除关系")
        self.show_deleted.setObjectName("graphShowDeleted")
        self.show_deleted.toggled.connect(lambda _: self.refresh())
        filters.addWidget(self.show_deleted)
        filters.addStretch(1)
        self.filters_panel.setFixedWidth(max(180, self.filters_panel.fontMetrics().horizontalAdvance(
            "显示已删除关系") + 48))
        body.addWidget(self.filters_panel)
        self.scene = QGraphicsScene(self)
        self.view = GraphView(self.scene, self)
        self.view.click_handler = self
        body.addWidget(self.view, 1)
        layout.addLayout(body, 1)
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
            if self.workspace_context is not None:
                self.workspace_id = self.workspace_context.workspace_id
            self.workspace_combo.blockSignals(True)
            try:
                self.workspace_combo.clear()
                for workspace in workspaces:
                    self.workspace_combo.addItem(workspace.name, str(workspace.id))
                index = self.workspace_combo.findData(str(self.workspace_id))
                self.workspace_combo.setCurrentIndex(index if index >= 0 else 0)
                data = self.workspace_combo.currentData()
                if self.workspace_context is None:
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
        self.refresh()

    # ------------------------------------------------- Phase 11: context protocol
    def clear_context(self):
        """Empty the scene: no node or edge from the previous Workspace stays drawn."""
        self.scene.clear()
        self.nodes = []
        self.edges = []
        self.layout = {}
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
        views = [view for view in views if show_deleted or view.relationship.deleted_at is None]
        # Phase 11 side filter: show only the requested current state. It filters the
        # rows already read, so it never issues another query.
        state = self.state_filter.currentData() if getattr(self, "state_filter", None) else None
        if state is not None:
            views = [view for view in views if view.current_state == state]
        return views

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
            nodes, edges, layout = build_graph_items(people, counts, views, positions,
                                                     avatar_for=self.avatar_for)
            for node in nodes:
                self.scene.addItem(node)
            for edge in edges:
                self.scene.addItem(edge)
            self.nodes, self.edges, self.layout = nodes, edges, layout
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


__all__ = ["ALL_TYPES", "GraphPage", "GraphView", "PersonNode", "RelationshipEdge",
           "run_operation"]
