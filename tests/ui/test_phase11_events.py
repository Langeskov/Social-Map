"""Phase 11.4 Event / Evidence / Timeline: hierarchy, marking and filters.

Contracts:

* the Event detail reads Header -> Time -> Participants -> Description -> Evidence ->
  Related history, and Evidence can never be mixed into the description;
* ORIGINAL EVIDENCE and USER ANNOTATION are marked FACT / USER and stay separate;
* a removed last IMAGE/FILE attachment is refused with the Service's own message;
* the Timeline separates UPCOMING from PAST and filters by kind / period / person
  without extra queries;
* nothing here writes: the timeline and the detail are read-only projections.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.evidence_dialog import EvidenceDetailDialog
from app.ui.main_window import MainWindow
from app.ui.timeline_panel import KIND_FILTERS, PAST, PERIOD_FILTERS, UPCOMING


@pytest.fixture
def case(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    config = AppConfig(data_dir=tmp_path / "phase11-events")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        window.navigation.setCurrentRow(2)
        qt_app.processEvents()
        yield SimpleNamespace(services=services, window=window, page=window.events_page,
                              events=window.events_page, qt_app=qt_app, errors=errors,
                              ws=services.initial_workspace.id, tmp_path=tmp_path)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def layout_widgets(widget):
    layout = widget.layout()
    return [layout.itemAt(index).widget() for index in range(layout.count())]


# ------------------------------------------------------------------ Event order
def test_event_detail_follows_the_reading_order(case):
    page = case.page
    view = page.detail_view
    view_widgets = layout_widgets(view)
    assert view_widgets.index(view.participants) < view_widgets.index(view.description)
    # Evidence has its own slot after the description, never inside it.
    assert view_widgets.index(view.evidence_container) > view_widgets.index(view.description)
    assert view.histories not in view_widgets
    # Page order: detail, Evidence, Related history.
    page_widgets = layout_widgets(view.parent())
    assert page_widgets.index(page.evidence_panel) == page_widgets.index(view) + 1
    assert page_widgets.index(view.histories_block) == page_widgets.index(page.evidence_panel) + 1


def test_event_detail_marks_layer_and_lifecycle(case):
    page, services, ws = case.page, case.services, case.ws
    event = services.events.create_event(ws, "带标记的事件", "Meeting", occurred_on=date(2026, 4, 1))
    page.focus_event(event.id)
    view = page.detail_view
    assert view.badge.layer.key == "OCCURRENCE"
    assert view.lifecycle_pill.isVisible()
    assert view.lifecycle_pill.status == "ACTIVE"
    services.events.soft_delete_event(ws, event.id)
    page.include_deleted.setChecked(True)
    page.focus_event(event.id)
    assert view.lifecycle_pill.status == "DELETED"


def test_empty_event_detail_clears_every_block(case):
    page = case.page
    page.detail_view.set_detail(None)
    assert page.detail_view.lifecycle_pill.isVisible() is False
    assert page.detail_view.participants.rowCount() == 0
    assert page.detail_view.histories.rowCount() == 0
    assert page.detail_view.description.toPlainText() == ""


# -------------------------------------------------------------- Evidence marking
def test_evidence_detail_marks_original_and_annotation(case):
    services, ws = case.services, case.ws
    event = services.events.create_event(ws, "证据事件", "Meeting", occurred_on=date(2026, 4, 2))
    evidence = services.evidence.create_evidence(ws, event.id, "TEXT", "原始", content_text="原文",
                                                 annotation="我的注释")
    detail = services.evidence.detail(ws, evidence.id)
    dialog = EvidenceDetailDialog(detail)
    assert dialog.content.isReadOnly()
    assert dialog.annotation.isReadOnly() is False
    assert dialog.content.toPlainText() == "原文"
    assert dialog.annotation.toPlainText() == "我的注释"
    dialog.close()


def test_last_image_attachment_is_refused_with_the_service_message(case):
    services, ws, tmp_path = case.services, case.ws, case.tmp_path
    source = tmp_path / "only.png"
    Image.new("RGB", (8, 8), "white").save(source)
    media = services.media.import_file(ws, source)
    event = services.events.create_event(ws, "图片证据", "Meeting", occurred_on=date(2026, 4, 3))
    evidence = services.evidence.create_evidence(ws, event.id, "IMAGE", "唯一图片", source_type="IMAGE",
                                                 media=(media.id,))
    case.page.focus_event(event.id)
    panel = case.page.evidence_panel
    panel.include_deleted.setChecked(True)
    panel.refresh()
    row = next(index for index in range(panel.evidence_list.rowCount())
               if panel.evidence_list.item(index, 0).text() == "唯一图片")
    panel.evidence_list.selectRow(row)
    panel.attachments.selectRow(0)
    panel.remove_media()
    assert len(case.errors) == 1
    assert "至少保留一个附件" in case.errors[0]
    assert len(services.evidence.detail(ws, evidence.id).media) == 1
    # The refusal is explicitly a rule conflict, never a stale-snapshot claim.
    assert panel.conflict_banner.message_label.text() == case.errors[0]


# ------------------------------------------------------------------ Timeline
def test_timeline_separates_upcoming_from_past(case, qt_app):
    services, ws = case.services, case.ws
    now = datetime.now(timezone.utc)
    services.events.create_event(ws, "过去的事件", "Meeting",
                                 started_at=now - timedelta(days=3))
    services.events.create_event(ws, "未来的事件", "Meeting",
                                 started_at=now + timedelta(days=30))
    case.window.navigation.setCurrentRow(0)
    qt_app.processEvents()
    panel = case.window.events_page.workspace_timeline
    panel.set_person(ws, None)
    periods = {panel.table.item(row, 2).text(): panel.table.item(row, 6).text()
               for row in range(panel.table.rowCount())}
    assert periods["过去的事件"] == PAST
    assert periods["未来的事件"] == UPCOMING


def test_timeline_filters_by_period_kind_and_person(case, qt_app):
    services, ws = case.services, case.ws
    now = datetime.now(timezone.utc)
    person = services.people.create(ws, "时间线人物")
    other = services.people.create(ws, "另一个人")
    past = services.events.create_event(ws, "已发生", "Meeting", started_at=now - timedelta(days=2))
    future = services.events.create_event(ws, "计划中", "Meeting", started_at=now + timedelta(days=5))
    services.events.add_participant(ws, past.id, person.id)
    services.events.add_participant(ws, future.id, other.id)
    case.window.navigation.setCurrentRow(0)
    qt_app.processEvents()
    panel = case.window.events_page.workspace_timeline
    panel.set_person(ws, None)
    assert panel.table.rowCount() == 2

    panel.period_filter.setCurrentIndex(panel.period_filter.findData(UPCOMING))
    assert [panel.table.item(row, 2).text() for row in range(panel.table.rowCount())] == ["计划中"]

    panel.period_filter.setCurrentIndex(panel.period_filter.findData(None))
    assert panel.table.rowCount() == 2

    panel.person_filter.setCurrentIndex(panel.person_filter.findData(str(person.id)))
    assert [panel.table.item(row, 2).text() for row in range(panel.table.rowCount())] == ["已发生"]

    panel.person_filter.setCurrentIndex(0)
    panel.kind_filter.setCurrentIndex(panel.kind_filter.findData("RELATIONSHIP_CHANGE"))
    assert panel.table.rowCount() == 0
    panel.kind_filter.setCurrentIndex(0)
    assert panel.table.rowCount() == 2


def test_timeline_filter_options_are_explicit():
    assert [label for label, _value in PERIOD_FILTERS] == ["全部时段", "UPCOMING · 未来", "PAST · 过去"]
    assert [value for _label, value in KIND_FILTERS] == [None, "EVENT", "RELATIONSHIP_CHANGE"]


def test_timeline_rows_keep_their_payload_and_columns(case, qt_app):
    services, ws = case.services, case.ws
    event = services.events.create_event(ws, "有效载荷", "Meeting", occurred_on=date(2026, 5, 5))
    case.window.navigation.setCurrentRow(0)
    qt_app.processEvents()
    panel = case.window.events_page.workspace_timeline
    panel.set_person(ws, None)
    assert panel.table.columnCount() == 7
    assert panel.table.horizontalHeaderItem(1).text() == "类型"
    assert panel.table.horizontalHeaderItem(6).text() == "时段"
    row = 0
    payload = panel.table.item(row, 0).data(0x0100)
    assert payload.item.entity_id == event.id
    assert case.errors == []
