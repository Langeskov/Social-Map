"""Phase 12.2 Activity & Evidence Inspector: selection, reader, jump path.

Real services, real migrations. The tests drive the same selection handlers a
user click reaches and assert on the rendered facts (dialog fields, reader
content, navigation requests) - never on scores or inferences, because the data
model has none.
"""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox, QTextEdit

from app.config.settings import AppConfig
from app.domain.entities import AnalysisRun
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.unit_of_work import transaction
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import RelationshipDetailDialog


def seed_run(runtime, workspace_id, person_id):
    """Record one analysis run without any network call (the ledger is a fact)."""
    now = datetime.now(timezone.utc)
    run = AnalysisRun(workspace_id=workspace_id, target_type="PERSON", target_id=person_id,
                      analysis_type="PERSON_PROFILE", provider_name="local", model_name="stub-model",
                      scope=json.dumps({"target_type": "PERSON"}), status="SUCCEEDED",
                      created_at=now, started_at=now, result="DERIVED 文本",
                      completed_at=now, duration_ms=12)
    with transaction(runtime.sessions, write=True) as session:
        sql_analysis_repositories(session).runs.save(run)
    return run


@pytest.fixture
def inspector_ui(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase12-inspector-data")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        ws = services.initial_workspace.id
        a = services.people.create(ws, "甲")
        b = services.people.create(ws, "乙")
        lonely = services.people.create(ws, "独行者")
        services.relationships.create(ws, a.id, b.id, "Friend")
        event = services.events.create_event(ws, "过去会议", "Meeting", "FACT",
                                             occurred_on=date(2026, 4, 1), participants=(a.id,))
        evidence = services.evidence.create_evidence(ws, event.id, "TEXT", "聊天记录",
                                                     content_text="原始内容ABC", annotation="旧注释")
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(1)
        qt_app.processEvents()
        page = window.people_page
        page.refresh_people(a.id)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=ws,
                              a=a, b=b, lonely=lonely, event=event, evidence=evidence,
                              window=window, page=page, qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def select_activity_row(c, row):
    """Select a timeline row the way a user click does.

    ``refresh`` auto-selects the first row signal-blocked, so re-selecting it
    programmatically emits nothing; a real click always emits ``itemClicked``,
    which is what the panel forwards to the inspector.
    """
    timeline = c.page.full_timeline
    index = timeline.table_index(row)
    timeline.table.selectRow(index)
    timeline.table.itemClicked.emit(timeline.table.item(index, 0))
    c.qt_app.processEvents()
    return row


# ------------------------------------------------------ activity -> inspectors

def test_event_row_selection_opens_the_event_inspector(inspector_ui):
    c = inspector_ui
    page = c.page
    rows = page.full_timeline.event_rows()
    assert [row.item.entity_id for row in rows] == [c.event.id]
    select_activity_row(c, rows[0])
    assert page.activity_inspector.currentWidget() is page.event_inspector_page
    assert page.event_inspector.fields["title"].text() == "过去会议"
    assert page.event_inspector.fields["record_kind"].text() == "FACT"
    assert page.event_inspector.participants.rowCount() == 1
    assert "甲" in page.event_inspector.participants.item(0, 0).text()
    assert c.errors == []


def test_relationship_change_row_shows_relationship_detail(inspector_ui):
    c = inspector_ui
    page = c.page
    rows = page.full_timeline.relationship_rows()
    assert rows, "人物的关系状态变更必须出现在 Activity 中"
    select_activity_row(c, rows[0])
    assert page.activity_inspector.currentWidget() is page.relationship_inspector_page
    assert page.rel_fields["relationship_type"].text() == "Friend"
    assert "有向" not in page.rel_fields["directionality"].text()
    assert "ACTIVE" in page.rel_fields["current_state"].text()
    assert page.rel_history.rowCount() >= 1
    # The full shared detail dialog opens from the inspector.
    dialog = page._open_inspected_relationship()
    try:
        assert isinstance(dialog, RelationshipDetailDialog)
        assert dialog.fields["relationship_type"].text() == "Friend"
        assert dialog in page._dialogs
    finally:
        dialog.close()
        c.qt_app.processEvents()
    assert page._dialogs == []
    assert c.errors == []


def test_event_inspector_jumps_to_the_event_evidence(inspector_ui):
    c = inspector_ui
    page = c.page
    select_activity_row(c, page.full_timeline.event_rows()[0])
    page.event_evidence_button.click()
    c.qt_app.processEvents()
    assert page.detail_tabs.currentWidget() is page.evidence_tab
    item = page.evidence_list.currentItem()
    assert item is not None
    assert item.data(0x0100) == (c.event.id, c.evidence.id)
    assert page.evidence_reader.content.toPlainText() == "原始内容ABC"
    assert c.errors == []


def test_open_full_event_dialog_from_the_inspector(inspector_ui):
    c = inspector_ui
    page = c.page
    select_activity_row(c, page.full_timeline.event_rows()[0])
    dialog = page._open_inspected_event()
    try:
        assert dialog is not None and dialog.isVisible()
        assert dialog in page._dialogs          # tracked for Workspace reset
    finally:
        dialog.close()
        c.qt_app.processEvents()
    assert page._dialogs == []
    assert c.errors == []


# ---------------------------------------------------------- evidence reader

def test_evidence_reader_separates_raw_content_from_annotation(inspector_ui):
    c = inspector_ui
    page = c.page
    page.evidence_list.setCurrentRow(0)
    c.qt_app.processEvents()
    reader = page.evidence_reader
    assert reader.fields["title"].text() == "聊天记录"
    assert reader.fields["kind"].text() == "TEXT"
    assert reader.fields["source_type"].text() == "MANUAL"
    assert reader.content.isReadOnly()
    assert reader.content.toPlainText() == "原始内容ABC"
    assert not reader.annotation.isReadOnly()
    assert reader.annotation.toPlainText() == "旧注释"
    # FACT / USER separation is explicit and the raw block has no editor.
    from PySide6.QtWidgets import QGroupBox
    raw = reader.findChild(QGroupBox, "evidenceRawGroup")
    annotation_group = reader.findChild(QGroupBox, "evidenceAnnotationGroup")
    assert raw is not None and annotation_group is not None
    assert [w for w in raw.findChildren(QTextEdit) if not w.isReadOnly()] == []
    assert annotation_group.findChildren(QTextEdit) == [reader.annotation]
    # The owning Event stays visible (Evidence exists only through its Event).
    assert "过去会议" in reader.owner_label.text()
    assert c.errors == []


def test_reader_annotation_save_updates_only_the_annotation(inspector_ui):
    c = inspector_ui
    page = c.page
    page.evidence_list.setCurrentRow(0)
    c.qt_app.processEvents()
    page.evidence_reader.annotation.setPlainText("新注释")
    page.evidence_reader.save_annotation()
    stored = c.services.evidence.get(c.ws, c.evidence.id, include_deleted=True)
    assert stored.annotation == "新注释"
    assert stored.content_text == "原始内容ABC"
    assert stored.kind == "TEXT" and stored.title == "聊天记录"
    assert c.errors == []


def test_reader_owner_button_navigates_to_the_owning_event(inspector_ui):
    c = inspector_ui
    page = c.page
    requests = []
    page.navigate_requested.connect(lambda target, entity_id: requests.append((target, entity_id)))
    page.evidence_list.setCurrentRow(0)
    c.qt_app.processEvents()
    page._open_reader_owner()
    assert requests == [("event", c.event.id)]
    # Double-click keeps its historic jump to the owning Event as well.
    page.open_person_evidence()
    assert requests[-1] == ("event", c.event.id)
    assert c.errors == []


# --------------------------------------------------------- jump path + state

def test_full_jump_path_person_event_evidence_analysis(inspector_ui):
    c = inspector_ui
    page = c.page
    run = seed_run(c.runtime, c.ws, c.a.id)
    page.reload_person_analysis(c.a.id)
    requests = []
    page.navigate_requested.connect(lambda target, entity_id: requests.append((target, entity_id)))
    # Person is selected: Activity -> Event Inspector -> Evidence -> owner jump.
    assert page.current_detail.person.id == c.a.id
    select_activity_row(c, page.full_timeline.event_rows()[0])
    assert page.event_inspector.fields["title"].text() == "过去会议"
    page._show_inspected_event_evidence()
    assert page.evidence_reader.content.toPlainText() == "原始内容ABC"
    page._open_reader_owner()
    # Analysis Sources are reached through the run detail on the Analysis page.
    assert page.analysis_list.count() == 1
    page.analysis_list.setCurrentRow(0)
    assert page.open_person_analysis() == run.id
    assert requests == [("event", c.event.id), ("analysis_run", run.id)]
    assert c.errors == []


def test_activity_rows_carry_the_record_status(inspector_ui):
    c = inspector_ui
    page = c.page
    timeline = page.full_timeline
    # A soft-deleted Event stays hidden until asked for, then its row is marked.
    c.services.events.soft_delete_event(c.ws, c.event.id)
    timeline.refresh()
    titles = [timeline.table.item(row, 2).text() for row in range(timeline.table.rowCount())]
    assert "已删除 · 过去会议" not in titles
    timeline.include_deleted.setChecked(True)
    c.qt_app.processEvents()
    titles = [timeline.table.item(row, 2).text() for row in range(timeline.table.rowCount())]
    assert "已删除 · 过去会议" in titles
    # A live record keeps its plain title.
    assert "Friend 关系状态变更" in titles
    assert c.errors == []


def test_person_switch_clears_inspector_and_reader(inspector_ui):
    c = inspector_ui
    page = c.page
    select_activity_row(c, page.full_timeline.event_rows()[0])
    page.evidence_list.setCurrentRow(0)
    c.qt_app.processEvents()
    assert page.evidence_reader.content.toPlainText() == "原始内容ABC"
    page.refresh_people(c.lonely.id)
    c.qt_app.processEvents()
    assert page.activity_inspector.currentWidget() is page.activity_inspector_empty
    assert page.evidence_reader.annotation.toPlainText() == ""
    assert page.evidence_reader.content.toPlainText() == ""
    assert page.rel_fields["relationship_type"].text() == "—"
    assert page.evidence_list.count() == 0
    assert c.errors == []


def test_workspace_switch_closes_inspector_dialogs(inspector_ui):
    c = inspector_ui
    page = c.page
    select_activity_row(c, page.full_timeline.event_rows()[0])
    dialog = page._open_inspected_event()
    assert dialog.isVisible()
    other = c.services.workspaces.create("另一个")
    c.window.workspace_context.refresh()
    c.window.top_bar.set_workspaces(c.window.workspace_context.workspaces,
                                    c.window.workspace_context.workspace_id)
    c.window.workspace_context.set_workspace(other.id)
    c.qt_app.processEvents()
    assert page._dialogs == []
    assert page.activity_inspector.currentWidget() is page.activity_inspector_empty
    assert c.errors == []


# ------------------------------------------------------------------ batching

def test_timeline_refresh_is_batched_not_per_row(inspector_ui, monkeypatch):
    """Refresh reads events and relationship states in batches, never per row."""
    c = inspector_ui
    page = c.page
    counters = {"events_detail": 0, "relationships_current": 0,
                "events_list": 0, "relationships_list": 0}
    events, relationships = c.services.events, c.services.relationships
    real_detail, real_current = events.detail, relationships.current
    real_list_events, real_list_by_workspace = events.list_events, relationships.list_by_workspace

    def counting_detail(*args, **kwargs):
        counters["events_detail"] += 1
        return real_detail(*args, **kwargs)

    def counting_current(*args, **kwargs):
        counters["relationships_current"] += 1
        return real_current(*args, **kwargs)

    def counting_list_events(*args, **kwargs):
        counters["events_list"] += 1
        return real_list_events(*args, **kwargs)

    def counting_list_by_workspace(*args, **kwargs):
        counters["relationships_list"] += 1
        return real_list_by_workspace(*args, **kwargs)

    monkeypatch.setattr(events, "detail", counting_detail)
    monkeypatch.setattr(relationships, "current", counting_current)
    monkeypatch.setattr(events, "list_events", counting_list_events)
    monkeypatch.setattr(relationships, "list_by_workspace", counting_list_by_workspace)
    page.full_timeline.refresh()
    assert counters["events_detail"] == 0
    assert counters["relationships_current"] == 0
    assert counters["events_list"] == 1
    assert counters["relationships_list"] == 1
    assert page.full_timeline.event_rows(), "batch 读取后行数据仍完整"
    assert page.full_timeline.selected_row() is not None
    # Opening a row loads a fresh detail on demand - the dialog path stays real.
    row = page.full_timeline.event_rows()[0]
    dialog = page.full_timeline.show_event(row)
    try:
        # The dialog's embedded event controller re-reads fresh data as well,
        # so at least one on-demand detail read happened after the batch.
        assert counters["events_detail"] >= 1
    finally:
        dialog.close()
        c.qt_app.processEvents()
    assert c.errors == []
