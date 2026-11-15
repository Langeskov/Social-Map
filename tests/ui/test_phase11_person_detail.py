"""Phase 11.3 Person Detail: the layer chain, the identity card and the tabs.

The contracts under test:

* the detail follows the layer chain top to bottom — Identity (Entity) ->
  Relationships (Link) -> Timeline (Occurrence) -> Evidence (Proof) / Memory (user
  confirmed) / Analysis (Inference) in tabs;
* the identity card shows only fields that have a value (no empty boxes), names the
  Current Workspace segment through the shell, and renders a short display UUID that
  is *presentation only* (no schema field);
* an archived person is marked ARCHIVED while its history stays readable;
* the People list has an archived filter and never invents rows;
* Evidence and Analysis tabs read the fact layer: nothing is written by looking.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.ui.design.widgets import LayerCard
from app.ui.main_window import MainWindow


@pytest.fixture
def case(tmp_path, qt_app, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    # Destructive confirmations are answered "no" unless a test says otherwise.
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: QMessageBox.StandardButton.No)
    config = AppConfig(data_dir=tmp_path / "person-detail")
    runtime = initialize_database(config)
    window = None
    try:
        services = create_application_services(config, runtime.sessions)
        window = MainWindow(config, services)
        window.show()
        qt_app.processEvents()
        window.navigation.setCurrentRow(1)
        qt_app.processEvents()
        yield SimpleNamespace(services=services, window=window, page=window.people_page,
                              qt_app=qt_app, errors=errors, ws=services.initial_workspace.id,
                              tmp_path=tmp_path)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


def test_detail_follows_the_layer_chain(case):
    page = case.page
    assert isinstance(page.identity_card, LayerCard)
    assert page.identity_card.layer.key == "ENTITY"
    assert page.relationships_card.layer.key == "LINK"
    assert page.timeline_card.layer.key == "OCCURRENCE"
    assert page.analysis_layer.layer.key == "INFERENCE"
    # Tabs keep the agreed order: Evidence, Memory, Analysis.
    titles = [page.detail_tabs.tabText(index) for index in range(page.detail_tabs.count())]
    assert [title.split(" · ")[0] for title in titles] == ["Evidence", "Memory", "Analysis"]
    # One Timeline only: the duplicate Event-only widget is gone (option c).
    assert not hasattr(page, "event_timeline")


def test_identity_card_hides_empty_fields(case):
    page = case.page
    person = case.services.people.create(case.ws, "只有名字")
    page.refresh_people(person.id)
    assert page.current_detail.person.id == person.id
    assert page.identity_name.text() == "只有名字"
    for key, label in page.field_labels.items():
        assert label.text() == "—"
        assert page.fields_form.isRowVisible(page.field_rows[key]) is False


def test_identity_card_shows_present_fields_only(case):
    page = case.page
    person = case.services.people.create(case.ws, "完整人物", occupation="工程师",
                                         organization="某公司", location="上海", gender="FEMALE")
    page.refresh_people(person.id)
    assert page.field_labels["occupation"].text() == "工程师"
    assert page.field_labels["organization"].text() == "某公司"
    assert page.field_labels["location"].text() == "上海"
    assert page.fields_form.isRowVisible(page.field_rows["occupation"]) is True
    assert page.fields_form.isRowVisible(page.field_rows["location"]) is True


def test_short_uuid_is_display_only(case):
    page = case.page
    person = case.services.people.create(case.ws, "UUID 人物")
    page.refresh_people(person.id)
    text = page.identity_uuid.text()
    assert text.startswith("ID · ")
    assert "…" in text
    assert len(text) < len(str(person.id)) + 8
    assert page.identity_uuid.toolTip() == str(person.id)
    assert page.short_uuid(person.id).startswith(str(person.id)[:8])


def test_active_person_shows_active_and_archived_shows_archived(case):
    page = case.page
    person = case.services.people.create(case.ws, "会归档的人物")
    page.refresh_people(person.id)
    assert page.status_pill.isVisible()
    assert page.status_pill.status == "ACTIVE"
    assert page.archive_button.isEnabled() is True
    case.services.people.soft_delete(case.ws, person.id)
    page.include_archived.setChecked(True)
    page.refresh_people(person.id)
    assert page.status_pill.status == "ARCHIVED"
    assert page.archive_button.isEnabled() is False
    # History of an archived person stays readable.
    assert page.current_detail is not None
    assert page.current_detail.person.deleted_at is not None


def test_archived_filter_is_off_by_default(case):
    page, services, ws = case.page, case.services, case.ws
    services.people.create(ws, "有效人物")
    archived = services.people.create(ws, "已归档人物")
    services.people.soft_delete(ws, archived.id)
    page.include_archived.setChecked(False)
    page.refresh_people()
    names = [page.people_list.item(index).text() for index in range(page.people_list.count())]
    assert names == ["有效人物"]
    page.include_archived.setChecked(True)
    page.refresh_people()
    names = [page.people_list.item(index).text() for index in range(page.people_list.count())]
    assert "已归档人物 · ARCHIVED" in names


def test_evidence_tab_lists_evidence_of_the_person_events(case):
    page, services, ws = case.page, case.services, case.ws
    person = services.people.create(ws, "有证据的人物")
    event = services.events.create_event(ws, "相关事件", "Meeting", occurred_on=date(2026, 5, 1))
    services.events.add_participant(ws, event.id, person.id)
    evidence = services.evidence.create_evidence(ws, event.id, "TEXT", "聊天记录",
                                                 content_text="原文")
    page.refresh_people(person.id)
    assert page.evidence_list.count() == 1
    assert page.evidence_list.item(0).data(0x0100) == (event.id, evidence.id)
    assert "1 条证据" in page.evidence_status.text()
    # Opening it navigates to the owning Event (Evidence has no page of its own).
    page.evidence_list.setCurrentRow(0)
    assert page.open_person_evidence() == event.id


def test_evidence_tab_is_empty_and_honest_without_evidence(case):
    page, services, ws = case.page, case.services, case.ws
    person = services.people.create(ws, "没有证据")
    page.refresh_people(person.id)
    assert page.evidence_list.count() == 0
    assert "没有关联证据" in page.evidence_status.text()


def test_analysis_tab_reads_history_without_creating_a_run(case):
    page, services, ws = case.page, case.services, case.ws
    person = services.people.create(ws, "会被分析的人物")
    page.refresh_people(person.id)
    assert page.analysis_list.count() == 0
    assert "尚无关于该人物的分析记录" in page.analysis_status.text()
    assert services.analysis.history(ws, target_type="PERSON", target_id=person.id) == []
    assert page.analysis_layer.layer.key == "INFERENCE"


def test_person_detail_is_one_page_not_extra_windows(case):
    page, services, ws = case.page, case.services, case.ws
    person = services.people.create(ws, "一个人物")
    page.refresh_people(person.id)
    # Selecting a person opens no window at all.
    assert page._dialogs == []
    assert page._viewers == []


def test_selecting_another_person_replaces_the_previous_detail(case):
    page, services, ws = case.page, case.services, case.ws
    first = services.people.create(ws, "第一")
    second = services.people.create(ws, "第二")
    page.refresh_people(first.id)
    assert page.current_detail.person.id == first.id
    page.refresh_people(second.id)
    assert page.current_detail.person.id == second.id
    assert page.identity_name.text() == "第二"
    assert case.errors == []
