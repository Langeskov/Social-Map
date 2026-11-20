"""Phase 12.1 Person Investigation Workspace: layout, Ego Network, selection state.

Real services, real migrations, plain static Qt graphics. No screenshot or pixel
assertions: the tests read the same facts the workspace draws (node payloads,
edge metadata, dialog fields, layout parents) and call the same handlers a user
click reaches. Nothing here checks scores or inferences because none exist.
"""
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QMessageBox, QSplitter

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import RelationshipDetailDialog


@pytest.fixture
def workspace_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase12-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        a = services.people.create(ws, "甲")
        b = services.people.create(ws, "乙")
        c = services.people.create(ws, "丙")
        d = services.people.create(ws, "丁")
        friend = services.relationships.create(ws, a.id, b.id, "Friend")
        colleague = services.relationships.create(ws, a.id, c.id, "Colleague",
                                                  directionality="DIRECTED")
        classmate = services.relationships.create(ws, b.id, c.id, "Classmate")
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(1)
        qt_app.processEvents()
        page = window.people_page
        page.refresh_people(a.id)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws,
                              a=a, b=b, c=c, d=d, friend=friend, colleague=colleague,
                              classmate=classmate, window=window, page=page,
                              qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def ego_edge_ids(page):
    return {edge.entity.id for edge in page.ego_edges}


def snapshot_rows(runtime):
    """Row counts of the fact tables (the derived search projection excluded)."""
    inspector = inspect(runtime.engine)
    names = [name for name in inspector.get_table_names() if not name.startswith("search_document")]
    with runtime.engine.connect() as connection:
        return {name: connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
                for name in sorted(names)}


# ------------------------------------------------------------------ layout

def test_workspace_keeps_all_regions_and_inspector_tabs(workspace_ui):
    c = workspace_ui
    page = c.page
    assert page.objectName() == "people"
    assert page.inspector_mode in {"side", "bottom"}
    # People Index keeps every management capability of the old page.
    assert page.search is not None and page.include_archived is not None
    assert page.people_list.objectName() == "peopleList"
    assert page.create_person is not None and page.edit_button is not None
    # Dossier keeps the identity surface.
    assert page.identity_card.layer.key == "ENTITY"
    assert page.identity_name is not None and page.identity_uuid is not None
    assert page.status_pill is not None and page.notes is not None
    assert page.alias_list is not None and page.image_list is not None
    # Ego Network sits in the centre column under the dossier.
    assert page.ego_card.layer.key == "LINK"
    assert page.ego_view is not None and page.ego_status is not None
    # Inspector tabs keep the layer chain Link -> Occurrence -> Proof -> User -> Inference.
    titles = [page.detail_tabs.tabText(index) for index in range(page.detail_tabs.count())]
    assert [title.split(" · ")[0] for title in titles] == [
        "关系", "Activity", "Evidence", "Memory", "Analysis"]
    assert page.timeline_card.layer.key == "OCCURRENCE"
    assert page.analysis_layer.layer.key == "INFERENCE"
    assert c.errors == []


def test_inspector_mode_switches_between_side_and_bottom(workspace_ui):
    c = workspace_ui
    page = c.page
    page.set_inspector_mode("side")
    assert page.inspector_mode == "side"
    assert page.detail_tabs.parent() is page.inner_split
    assert page.inner_split.indexOf(page.detail_tabs) == 2
    page.set_inspector_mode("bottom")
    assert page.inspector_mode == "bottom"
    assert page.detail_tabs.parent() is page.outer_split
    assert page.outer_split.indexOf(page.detail_tabs) == 1
    # The tab widget itself survives the move with its panels intact.
    assert page.detail_tabs.count() == 5
    assert page.full_timeline.parent() is not None
    page.set_inspector_mode("side")
    assert page.detail_tabs.parent() is page.inner_split
    assert c.errors == []


def test_manual_mode_choice_stops_the_auto_switch(workspace_ui):
    c = workspace_ui
    page = c.page
    page.resize(1200, 700)
    c.qt_app.processEvents()
    page.toggle_inspector_mode()
    chosen = page.inspector_mode
    page._auto_inspector_mode()
    assert page.inspector_mode == chosen
    assert c.errors == []


def test_auto_switch_moves_the_inspector_by_width(workspace_ui):
    c = workspace_ui
    page = c.page
    page._inspector_mode_locked = False
    page.resize(1800, 800)
    page._auto_inspector_mode()
    assert page.inspector_mode == "side"
    page.resize(1100, 700)
    page._auto_inspector_mode()
    assert page.inspector_mode == "bottom"
    # Compact mode keeps every core control inside the page bounds.
    for widget in (page.people_list, page.edit_button, page.inspector_mode_button,
                   page.ego_view, page.detail_tabs):
        assert widget.isVisible() or widget.parent() is not None
        assert widget.width() > 0
    assert c.errors == []


# ------------------------------------------------------------- ego network

def test_ego_network_draws_one_hop_facts_only(workspace_ui):
    c = workspace_ui
    page = c.page
    assert {node.display_name for node in page.ego_nodes} == {"甲", "乙", "丙"}
    # "丁" is not a neighbour of 甲 and the 乙-丙 edge is not 甲's relationship.
    assert page.ego_node_for_person(c.d.id) is None
    assert ego_edge_ids(page) == {c.friend.id, c.colleague.id}
    assert len(page.ego_edges) == 2
    center = page.ego_node_for_person(c.a.id)
    assert center is not None and center.current is True
    assert "当前人物" in center.toolTip()
    assert page.ego_node_for_person(c.b.id).current is False
    assert "一层" in page.ego_status.text()
    assert c.errors == []


def test_ego_edges_keep_direction_and_state_facts(workspace_ui):
    c = workspace_ui
    page = c.page
    directed = next(edge for edge in page.ego_edges if edge.entity.id == c.colleague.id)
    undirected = next(edge for edge in page.ego_edges if edge.entity.id == c.friend.id)
    assert directed.directed is True and directed.arrow is not None
    assert undirected.directed is False and undirected.arrow is None
    assert "有向" in directed.legend_text() and "无向" in undirected.legend_text()
    assert "ACTIVE" in directed.legend_text()
    assert c.errors == []


def test_ego_hides_deleted_relationships_by_default_like_graph_page(workspace_ui):
    c = workspace_ui
    page = c.page
    assert page.ego_show_deleted.isChecked() is False
    assert page.ego_show_archived.isChecked() is False
    c.services.relationships.soft_delete(c.ws, c.friend.id)
    page.rebuild_ego()
    assert ego_edge_ids(page) == {c.colleague.id}
    page.ego_show_deleted.setChecked(True)
    edge = next(edge for edge in page.ego_edges if edge.entity.id == c.friend.id)
    assert edge.deleted is True
    assert "已删除" in edge.toolTip()
    page.ego_show_deleted.setChecked(False)
    assert ego_edge_ids(page) == {c.colleague.id}
    assert c.errors == []


def test_ego_hides_archived_endpoint_by_default_and_shows_on_request(workspace_ui):
    c = workspace_ui
    page = c.page
    c.services.people.soft_delete(c.ws, c.c.id)
    page.rebuild_ego()
    assert page.ego_node_for_person(c.c.id) is None
    assert ego_edge_ids(page) == {c.friend.id}
    page.ego_show_archived.setChecked(True)
    node = page.ego_node_for_person(c.c.id)
    assert node is not None and node.deleted is True
    assert ego_edge_ids(page) == {c.friend.id, c.colleague.id}
    assert "已归档" in page.ego_status.text()
    assert c.errors == []


def test_ego_empty_state_is_explicit(workspace_ui):
    c = workspace_ui
    page = c.page
    lonely = c.services.people.create(c.ws, "无关系人物")
    page.refresh_people(lonely.id)
    assert page.ego_status.text() == "该人物还没有关系记录。"
    assert page.ego_nodes == [] or [node.person_id for node in page.ego_nodes] == [lonely.id]
    assert page.ego_edges == []
    assert c.errors == []


def test_ego_node_click_opens_that_person(workspace_ui):
    c = workspace_ui
    page = c.page
    node = page.ego_node_for_person(c.b.id)
    node.click(SimpleNamespace(pos=lambda: QPointF(0, 0)))
    c.qt_app.processEvents()
    assert page.current_detail.person.id == c.b.id
    assert page.identity_name.text() == "乙"
    # The network re-centres on the opened person: 乙's relationships are
    # Friend (with 甲) and Classmate (with 丙); 甲's Colleague edge is not 乙's.
    center = page.ego_node_for_person(c.b.id)
    assert center is not None and center.current is True
    assert ego_edge_ids(page) == {c.friend.id, c.classmate.id}
    assert c.errors == []


def test_ego_edge_click_opens_the_real_relationship_detail(workspace_ui):
    c = workspace_ui
    page = c.page
    edge = next(edge for edge in page.ego_edges if edge.entity.id == c.colleague.id)
    dialog = page.ego_view.click_item(edge)
    try:
        assert isinstance(dialog, RelationshipDetailDialog)
        assert dialog.fields["relationship_type"].text() == "Colleague"
        assert "DIRECTED" in dialog.fields["directionality"].text()
        assert dialog in page._dialogs
    finally:
        dialog.close()
        c.qt_app.processEvents()
    assert page._dialogs == []
    assert c.errors == []


def test_ego_of_an_archived_person_is_degraded_not_invented(workspace_ui):
    c = workspace_ui
    page = c.page
    c.services.people.soft_delete(c.ws, c.a.id)
    page.include_archived.setChecked(True)
    page.refresh_people(c.a.id)
    assert page.status_pill.status == "ARCHIVED"
    assert page.ego_nodes == [] and page.ego_edges == []
    assert "已归档" in page.ego_status.text()
    assert "不可用" in page.ego_status.text()
    # Known Service gap (read contract approved for Phase 12.3): the relationship
    # and timeline *panels* still refuse archived people with the Service message;
    # the workspace itself invents nothing and shows the honest degraded state.
    assert c.errors != []
    assert all("归档" in message for message in c.errors), c.errors


def test_ego_persists_nothing(workspace_ui):
    c = workspace_ui
    page = c.page
    before = snapshot_rows(c.runtime)
    page.rebuild_ego()
    page.ego_show_deleted.setChecked(True)
    page.ego_show_archived.setChecked(True)
    page.ego_show_deleted.setChecked(False)
    page.ego_show_archived.setChecked(False)
    assert snapshot_rows(c.runtime) == before
    assert c.errors == []


# ------------------------------------------------------- interaction state

def test_selecting_another_person_replaces_the_whole_context(workspace_ui):
    c = workspace_ui
    page = c.page
    assert page.current_detail.person.id == c.a.id
    assert page.ego_node_for_person(c.a.id) is not None
    page.detail_tabs.setCurrentIndex(2)
    page.refresh_people(c.d.id)
    c.qt_app.processEvents()
    assert page.current_detail.person.id == c.d.id
    assert page.identity_name.text() == "丁"
    # No selection or drawing of the previous person survives.
    assert page.ego_node_for_person(c.a.id) is None
    assert page.ego_edges == []
    assert page.ego_status.text() == "该人物还没有关系记录。"
    assert page.evidence_list.count() == 0
    assert page.analysis_list.count() == 0
    assert page.detail_tabs.currentIndex() == 0
    assert c.errors == []


def test_workspace_switch_clears_dossier_ego_and_inspector(workspace_ui):
    c = workspace_ui
    page = c.page
    other = c.services.workspaces.create("另一个 Workspace")
    c.window.workspace_context.refresh()
    c.window.top_bar.set_workspaces(c.window.workspace_context.workspaces,
                                    c.window.workspace_context.workspace_id)
    assert page.ego_nodes != []
    c.window.workspace_context.set_workspace(other.id)
    c.qt_app.processEvents()
    assert page.current_detail is None
    assert page.ego_nodes == [] and page.ego_edges == []
    assert page.ego_status.text() == "尚未选择人物。"
    assert page.people_list.count() == 0
    assert page.evidence_list.count() == 0
    assert page.analysis_list.count() == 0
    assert page._dialogs == [] and page._viewers == []
    assert c.errors == []
