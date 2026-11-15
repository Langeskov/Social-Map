"""Phase 7 Graph page: real services, real migrations, plain static Qt graphics.

No screenshot or pixel assertion is used anywhere: the tests read the same facts
the page draws (node payloads, edge metadata, arrow items, layout points) and the
scene items' own ``click`` handlers, exactly like the other UI tests call buttons.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui import graph_page as module
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import RelationshipDetailDialog

PERSON_IDS = ["ALPHA", "BETA", "GAMMA", "DELTA"]


@pytest.fixture
def graph_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "graph-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        people = {name: services.people.create(ws, name) for name in PERSON_IDS}
        friend = services.relationships.create(ws, people["ALPHA"].id, people["BETA"].id, "Friend")
        colleague = services.relationships.create(ws, people["ALPHA"].id, people["GAMMA"].id, "Colleague",
                                                  directionality="DIRECTED")
        services.relationships.create(ws, people["BETA"].id, people["DELTA"].id, "Classmate")
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(3)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws, people=people,
                              friend=friend, colleague=colleague, window=window,
                              page=window.pages.widget(3), errors=errors, qt_app=qt_app)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def edge_for(context, relationship):
    return next(edge for edge in context.page.edges if edge.entity.id == relationship.id)


def snapshot_rows(runtime):
    """Row count of every object that is *not* the derived search projection.

    ``SearchService`` rebuilds its FTS5 projection on demand, and a search widget
    refresh can therefore legitimately insert projection rows. Graph building must
    not touch any of them either, so the projection is excluded from the comparison
    and the eleven fact tables plus the six other objects are compared exactly.
    """
    inspector = inspect(runtime.engine)
    names = [name for name in inspector.get_table_names() if not name.startswith("search_document")]
    with runtime.engine.connect() as connection:
        return {name: connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
                for name in sorted(names)}


# --------------------------------------------------------------------- discovery

def test_initial_graph_has_person_nodes_and_relationship_edges(graph_ui):
    c = graph_ui
    page = c.page
    assert page.objectName() == "graph"
    assert page.workspace_id == c.ws
    # Four current people, three relationships, no user data invented.
    assert sorted(node.display_name for node in page.nodes) == sorted(PERSON_IDS)
    assert len(page.edges) == 3
    assert sorted(edge.entity.relationship_type for edge in page.edges) == ["Classmate", "Colleague", "Friend"]
    assert page.status.textFormat() == Qt.TextFormat.PlainText
    assert "3 条关系" in page.status.text()
    # Relationship count per node is the number of drawn edges of that person.
    assert page.node_for_person(c.people["ALPHA"].id).count == 2
    assert page.node_for_person(c.people["DELTA"].id).count == 1
    assert c.errors == []


def test_node_carries_no_inference_or_score(graph_ui):
    c = graph_ui
    node = c.page.node_for_person(c.people["ALPHA"].id)
    forbidden = ("好感", "强度", "危险", "评分", "score", "affinity", "推断", "性格")
    texts = [node.toolTip(), node.label_text(), c.page.status.text()]
    texts += [edge.toolTip() for edge in c.page.edges]
    texts += [item.text() for item in c.page.scene.items() if hasattr(item, "text")]
    for text in texts:
        assert not any(word in text for word in forbidden)
    # The node payload is the real Domain value, not a copy with invented fields.
    assert node.person.id == c.people["ALPHA"].id
    assert node.person.display_name == "ALPHA"
    assert not hasattr(node, "score")
    assert c.errors == []


def test_directed_edge_has_arrow_and_undirected_edge_has_none(graph_ui):
    c = graph_ui
    directed = edge_for(c, c.colleague)
    undirected = edge_for(c, c.friend)
    assert directed.entity.directionality == "DIRECTED"
    assert directed.arrow is not None
    assert directed.arrow.sceneBoundingRect().isValid()
    assert directed.arrow.polygon().count() == 3
    assert "有向" in directed.legend_text()
    assert undirected.entity.directionality == "UNDIRECTED"
    assert undirected.arrow is None
    assert "无向" in undirected.legend_text()
    assert c.errors == []


def test_edge_label_and_state_are_rendered_as_text(graph_ui):
    c = graph_ui
    edge = edge_for(c, c.friend)
    assert edge.label is not None
    assert edge.label.text() == "Friend · 无向 · ACTIVE"
    # A PAUSED current state and a planned future state are shown as text, not animation.
    c.services.relationships.add_history(c.ws, c.friend.id, "PAUSED")
    c.page.refresh()
    refreshed = edge_for(c, c.friend)
    assert refreshed.current_state == "PAUSED"
    assert refreshed.label.text() == "Friend · 无向 · PAUSED"
    future = datetime.now(timezone.utc) + timedelta(days=60)
    c.services.relationships.add_history(c.ws, c.friend.id, "ENDED", effective_at=future)
    c.page.refresh()
    planned = edge_for(c, c.friend)
    # The current state stays PAUSED: a planned change is not today's state.
    assert planned.current_state == "PAUSED"
    assert planned.label.text() == "Friend · 无向 · PAUSED"
    assert c.services.relationships.current(c.ws, c.friend.id).current_state == "PAUSED"
    assert c.errors == []


def test_relationship_type_filter_limits_edges(graph_ui):
    c = graph_ui
    page = c.page
    assert {page.type_filter.itemText(index) for index in range(page.type_filter.count())} == {
        module.ALL_TYPES, "Friend", "Colleague", "Classmate"}
    assert page.type_filter.currentData() is None
    page.type_filter.setCurrentIndex(page.type_filter.findData("Friend"))
    assert [edge.entity.relationship_type for edge in page.edges] == ["Friend"]
    assert len(page.nodes) == 4                       # every person stays visible
    assert page.node_for_person(c.people["DELTA"].id).count == 0
    page.type_filter.setCurrentIndex(page.type_filter.findData("Colleague"))
    assert [edge.entity.relationship_type for edge in page.edges] == ["Colleague"]
    page.type_filter.setCurrentIndex(0)
    assert len(page.edges) == 3
    assert c.errors == []


def test_archived_people_hidden_by_default_and_shown_on_request(graph_ui):
    c = graph_ui
    page = c.page
    assert page.show_archived.isChecked() is False
    c.services.people.soft_delete(c.ws, c.people["GAMMA"].id)
    page.refresh()
    assert page.node_for_person(c.people["GAMMA"].id) is None
    assert len(page.nodes) == 3
    assert c.colleague.id not in {edge.entity.id for edge in page.edges}
    page.show_archived.setChecked(True)
    node = page.node_for_person(c.people["GAMMA"].id)
    assert node is not None and node.deleted is True
    assert len(page.nodes) == 4
    assert c.colleague.id in {edge.entity.id for edge in page.edges}
    assert "已归档" in page.status.text()
    page.show_archived.setChecked(False)
    assert page.node_for_person(c.people["GAMMA"].id) is None
    assert c.errors == []


def test_deleted_relationships_hidden_by_default_and_shown_on_request(graph_ui):
    c = graph_ui
    page = c.page
    c.services.relationships.soft_delete(c.ws, c.friend.id)
    page.refresh()
    assert c.friend.id not in {edge.entity.id for edge in page.edges}
    assert len(page.edges) == 2
    page.show_deleted.setChecked(True)
    edge = edge_for(c, c.friend)
    assert edge.deleted is True
    assert "已删除" in edge.toolTip()
    assert edge.arrow is None
    page.show_deleted.setChecked(False)
    assert c.friend.id not in {edge.entity.id for edge in page.edges}
    assert c.errors == []


# ------------------------------------------------------------------ interaction

def test_click_node_opens_person_and_requests_navigation(graph_ui):
    c = graph_ui
    page = c.page
    requests = []
    page.navigate_requested.connect(lambda target, entity_id: requests.append((target, entity_id)))
    node = page.node_for_person(c.people["BETA"].id)
    dialog = node.click(SimpleNamespace(pos=lambda: QPointF(0, 0)))
    try:
        assert requests == [("person", c.people["BETA"].id)]
        assert isinstance(dialog, module.PersonDialog)
        assert dialog in page._dialogs
        assert dialog.windowTitle()
        # The shell navigated to People and selected the same person.
        assert c.window.pages.currentIndex() == 1
        assert c.window.people_page.current_detail.person.id == c.people["BETA"].id
    finally:
        dialog.close()
        qt_app_events(c)
    assert page._dialogs == []
    assert c.errors == []


def test_click_edge_opens_relationship_detail(graph_ui):
    c = graph_ui
    page = c.page
    requests = []
    page.navigate_requested.connect(lambda target, entity_id: requests.append((target, entity_id)))
    edge = edge_for(c, c.colleague)
    dialog = page.view.click_item(edge)
    try:
        assert requests == [("relationship", c.colleague.id)]
        assert isinstance(dialog, RelationshipDetailDialog)
        assert dialog.fields["relationship_type"].text() == "Colleague"
        assert "DIRECTED" in dialog.fields["directionality"].text()
        assert dialog.fields["current_state"].text() == "ACTIVE · 正常"
        assert dialog.history_table.rowCount() == 1
        for label in dialog.fields.values():
            assert label.textFormat() == Qt.TextFormat.PlainText
    finally:
        dialog.close()
        qt_app_events(c)
    assert page._dialogs == []
    assert c.errors == []


def test_edge_selection_exposes_the_underlying_view(graph_ui):
    c = graph_ui
    page = c.page
    edge = edge_for(c, c.friend)
    page.scene.clearSelection()
    edge.setSelected(True)
    assert page.selected_edge() is edge
    assert page.selected_node() is None
    assert edge.shape().boundingRect().width() >= edge.path.boundingRect().width()
    assert page.focus_person(c.people["ALPHA"].id) is not None
    assert page.selected_node().person_id == c.people["ALPHA"].id
    assert {item.entity.id for item in page.scene.selectedItems()
            if isinstance(item, module.RelationshipEdge)} == {c.friend.id, c.colleague.id}
    assert c.errors == []


def test_node_is_movable_and_view_zooms(graph_ui):
    c = graph_ui
    page = c.page
    node = page.node_for_person(c.people["ALPHA"].id)
    assert node.flags() & module.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    before = node.pos()
    node.setPos(before + QPointF(40, -25))
    assert node.pos() == before + QPointF(40, -25)
    assert page.view.dragMode() == module.QGraphicsView.DragMode.ScrollHandDrag
    assert page.view.zoom_by(1.15) > 1.0
    assert page.view.reset_zoom() == 1.0
    assert page.view.zoom_by(100.0) == 1.0        # clamped, never lost
    assert page.view.zoom_by(0.001) == 1.0
    assert c.errors == []


def test_mouse_press_on_scene_items_reaches_the_click_handlers(graph_ui):
    """A real mouse press/release on the canvas must reach the same handlers.

    The items are movable, so the press is delivered through the scene (which is
    also what proves the node's ItemIsMovable flag is live), and the release is
    delivered to the view, which owns the click dispatch.
    """
    c = graph_ui
    page = c.page
    page.navigate_requested.disconnect()
    requests = []
    page.navigate_requested.connect(lambda target, entity_id: requests.append((target, entity_id)))
    node = page.node_for_person(c.people["ALPHA"].id)
    center = QPointF(page.view.mapFromScene(node.pos()))
    c.qt_app.processEvents()
    press = QMouseEvent(QEvent.Type.MouseButtonPress, center, page.view.viewport().mapToGlobal(center),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    page.scene.sendEvent(node, press)
    assert node.flags() & module.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, center, page.view.viewport().mapToGlobal(center),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    page.view.mouseReleaseEvent(release)
    assert requests == [("person", c.people["ALPHA"].id)]
    dialog = page._dialogs[-1]
    try:
        assert isinstance(dialog, module.PersonDialog)
    finally:
        dialog.close()
        qt_app_events(c)
    # The same path works for an edge: the widened shape makes the arc clickable.
    requests.clear()
    edge = edge_for(c, c.friend)
    assert page.view.itemAt(page.view.mapFromScene(edge.label.pos())) is not None
    page.view.click_item(edge)
    assert requests == [("relationship", c.friend.id)]
    page._dialogs[-1].close()
    qt_app_events(c)
    assert page._dialogs == []
    assert c.errors == []


def test_empty_workspace_renders_no_node_and_no_fake_data(graph_ui):
    c = graph_ui
    page = c.page
    empty = c.services.workspaces.create("Empty")
    page.activate()
    page.workspace_combo.setCurrentIndex(page.workspace_combo.findData(str(empty.id)))
    assert page.workspace_id == empty.id
    assert page.nodes == [] and page.edges == []
    assert page.scene.items() == []
    assert "没有可显示的人物" in page.status.text()
    assert c.errors == []


# ------------------------------------------------------------------ persistence

def test_archived_person_without_any_relationship_is_still_drawable(graph_ui):
    """Show Archived uses the management view, so isolated archived people appear."""
    c = graph_ui
    page = c.page
    lonely = c.services.people.create(c.ws, "无关系的已归档人物")
    c.services.people.soft_delete(c.ws, lonely.id)
    page.refresh()
    assert page.node_for_person(lonely.id) is None
    page.show_archived.setChecked(True)
    node = page.node_for_person(lonely.id)
    assert node is not None and node.deleted is True
    assert node.display_name == "无关系的已归档人物"
    assert page.edges_for_person(lonely.id) == []
    page.show_archived.setChecked(False)
    assert page.node_for_person(lonely.id) is None
    assert c.errors == []


def test_building_the_graph_persists_nothing(graph_ui):
    c = graph_ui
    page = c.page
    before = snapshot_rows(c.runtime)
    page.rebuild_graph()
    page.type_filter.setCurrentIndex(page.type_filter.findData("Friend"))
    page.show_archived.setChecked(True)
    page.show_deleted.setChecked(True)
    page.refresh_button.click()
    page.show_archived.setChecked(False)
    page.show_deleted.setChecked(False)
    page.focus_person(c.people["ALPHA"].id)
    after = snapshot_rows(c.runtime)
    assert after == before
    assert set(after) >= {"workspace", "person", "person_alias", "media", "person_media", "relationship",
                          "relationship_history", "event", "event_participant", "evidence",
                          "evidence_media", "alembic_version"}
    assert "search_document" not in after
    # Nothing was added to the graph page's own state either: nodes/edges only.
    assert set(vars(page)) >= {"nodes", "edges", "layout", "scene"}
    assert c.errors == []


def qt_app_events(context):
    context.qt_app.processEvents()
