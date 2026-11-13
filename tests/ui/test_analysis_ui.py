"""Phase 8 Analysis UI: real migrations, real services, fake HTTP transport.

Every test in this module runs against the real SQLite database and the real
services; only the HTTP transport is fake, so **no test can reach the network**.
The rules under test are the ones a user can observe:

* opening People / Events / an Event detail or selecting a row never calls a model;
* a run exists only after the user confirms the options dialog;
* the blocking model call happens on a worker thread, with a Cancel button wired to
  the very ``CancelToken`` the service received;
* a FAILED / CANCELLED run shows safe text - never a secret, never result text;
* the history is append-only, and deleting a record does not touch a fact;
* only ``credential_ref`` is configured, stored or displayed, never an API key.
"""
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.context import ALL_TIME, CUSTOM, LAST_7_DAYS, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.services.llm.gateway import CancelToken
from app.ui import analysis_dialog as dialog_module
from app.ui.analysis_dialog import AnalysisOptionsDialog, ProviderSettingsDialog
from app.ui.event_detail import ROLE
from app.ui.main_window import MainWindow
from app.ui.people_page import PeoplePage

API_KEY = "sk-phase8-super-secret-key-should-never-appear"
FOUR_SECTIONS = """## Facts（事实）
- Tester 在 2026-05-01 参加 Meeting（来源：event 记录）。

## Observations（观察）
- 用户记录了“通话持续 10 分钟”。

## Interpretations（推断）
- 根据记录，双方关系可能仍在维持。

## Uncertainties（不确定与缺失）
- 通话内容没有证据支持。"""
CHAT_URL = "http://127.0.0.1:9/v1/chat/completions"


def chat_response(content=FOUR_SECTIONS, model="fake-model", status=200):
    if status != 200:
        return HttpResponse(status, b'{"error":"unauthorized"}', {}, CHAT_URL)
    body = json.dumps({"id": "chat-1", "model": model,
                       "choices": [{"message": {"role": "assistant", "content": content},
                                    "finish_reason": "stop"}]}).encode("utf-8")
    return HttpResponse(status, body, {}, CHAT_URL)


def models_response(names=("fake-model",)):
    body = json.dumps({"data": [{"id": name} for name in names]}).encode("utf-8")
    return HttpResponse(200, body, {}, "http://127.0.0.1:9/v1/models")


@pytest.fixture
def analysis_ui(tmp_path, qt_app, monkeypatch):
    """Real database + real services + a FakeTransport; UI errors are captured."""
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase8-analysis-data")
    runtime = initialize_database(config)
    window = None
    try:
        transport = FakeTransport([])
        gateway = LLMGateway(LLMConfigStore.for_data_dir(config.storage_paths.data_dir),
                             transport=transport)
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        ws = services.initial_workspace.id
        person = services.people.create(ws, "Tester")
        other = services.people.create(ws, "Other")
        relationship = services.relationships.create(ws, person.id, other.id, "Friend")
        event = services.events.create_event(ws, "Meeting", "Call", "FACT",
                                             started_at=datetime.now(timezone.utc) - timedelta(days=3),
                                             participants=(person.id,))
        gateway.save_provider(LLMProviderConfig(provider_name="fake",
                                               base_url="http://127.0.0.1:9/v1", model="fake-model"))
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(1)
        qt_app.processEvents()
        window.people_page.refresh_people(person.id)
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, gateway=gateway,
                              transport=transport, ws=ws, person=person, other=other,
                              relationship=relationship, event=event, window=window,
                              people=window.people_page, events=window.events_page,
                              page=window.analysis_page, qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


# ---------------------------------------------------------------------- helpers

def pump(qt_app, predicate, timeout=15.0):
    """Run the Qt event loop until ``predicate()`` holds (or time out)."""
    deadline = timeout
    while deadline > 0:
        qt_app.processEvents()
        if predicate():
            return True
        qt_app.processEvents()
        qt_app.thread().msleep(10)
        deadline -= 0.01
    return bool(predicate())


def history_rows(page):
    return page.history_table.rowCount()


def run_ids(page):
    return [page.history_table.item(row, 0).data(ROLE).run.id
            for row in range(page.history_table.rowCount())]


def select_run(page, run_id):
    for row in range(page.history_table.rowCount()):
        if page.history_table.item(row, 0).data(ROLE).run.id == run_id:
            page.history_table.selectRow(row)
            return page.history_table.item(row, 0).data(ROLE)
    raise AssertionError(f"run {run_id} is not listed")


def open_options(case, target_type, target_id, *, label=None):
    """The exact call the three Analyze buttons make; returns the options dialog."""
    dialog = case.page.analyze_target(target_type, target_id, target_label=label)
    case.qt_app.processEvents()
    return dialog


def options_for(case, target_type, target_id, **kwargs):
    dialog = AnalysisOptionsDialog(case.services, case.ws, target_type, target_id, **kwargs)
    case.qt_app.processEvents()
    return dialog


def run_to_completion(case, options, *, before=None):
    """Confirm the options dialog and let the worker the real code started finish.

    ``options`` must come from ``analyze_target`` (``page_call``), i.e. the wired
    entry point: confirming it starts the worker through the production path.
    """
    options.accept()
    case.qt_app.processEvents()
    progress = options.progress
    assert progress is not None, "confirming the options dialog must start a worker"
    if before is not None:
        before(progress)
    pump(case.qt_app, lambda: progress.worker.isFinished())
    case.qt_app.processEvents()
    pump(case.qt_app, lambda: history_rows(case.page) > 0)
    return progress.final_run, progress


def perform_run(case, target_type, target_id, *, response=None, before=None, page_call=None):
    """One complete UI run: the page entry point -> confirm -> worker -> history."""
    case.transport.push(response if response is not None else chat_response())
    options = (page_call or case.page.analyze_target)(target_type, target_id)
    case.qt_app.processEvents()
    assert isinstance(options, AnalysisOptionsDialog)
    run, progress = run_to_completion(case, options, before=before)
    return run, options, progress


# ------------------------------------------------- 1. entries exist, no auto call

def test_analyze_entries_exist_and_opening_pages_calls_no_model(analysis_ui):
    c = analysis_ui
    assert c.people.analyze_button.text().startswith("Analyze Person")
    assert c.people.relationship_panel.analyze_button.text().startswith("Analyze Relationship")
    assert c.events.analyze_button.text().startswith("Analyze Event")
    # Walk the whole shell: nothing may reach the transport without a user click.
    for index in range(6):
        c.window.navigation.setCurrentRow(index)
        c.qt_app.processEvents()
    c.people.refresh_people(c.person.id)
    c.people.relationship_panel.refresh()
    c.people.relationship_panel.table.selectRow(0)
    c.events.refresh_events(c.event.id)
    c.qt_app.processEvents()
    assert c.transport.requests == []
    assert c.page.history_table.rowCount() == 0
    assert c.errors == []


def test_each_analyze_button_opens_the_options_dialog_only(analysis_ui):
    c = analysis_ui
    person_dialog = c.people.analyze_person()
    c.qt_app.processEvents()
    assert isinstance(person_dialog, AnalysisOptionsDialog)
    assert person_dialog.isVisible()
    assert person_dialog.analysis_type == "PERSON_PROFILE"
    assert person_dialog.provider.currentText() == "fake"
    person_dialog.reject()
    c.qt_app.processEvents()

    c.people.relationship_panel.table.selectRow(0)
    relationship_dialog = c.people.relationship_panel.analyze_relationship()
    c.qt_app.processEvents()
    assert relationship_dialog.analysis_type == "RELATIONSHIP_ANALYSIS"
    relationship_dialog.reject()
    c.qt_app.processEvents()

    event_dialog = c.events.analyze_event()
    c.qt_app.processEvents()
    assert event_dialog.analysis_type == "EVENT_ANALYSIS"
    event_dialog.reject()
    c.qt_app.processEvents()
    # A dialog that was opened and dismissed sends nothing and records nothing.
    assert c.transport.requests == []
    assert c.page.history_table.rowCount() == 0
    assert c.errors == []


def test_buttons_are_disabled_without_a_selection(analysis_ui):
    c = analysis_ui
    c.events.refresh_events()                 # a refresh auto-selects the first row
    assert c.events.analyze_button.isEnabled() is True
    c.events.select_event(None)               # no Event detail at all
    assert c.events.analyze_button.isEnabled() is False
    c.people.relationship_panel.set_person(None, None)
    assert c.people.relationship_panel.analyze_button.isEnabled() is False
    c.people.people_list.setCurrentItem(None)
    c.people.select_person(None)
    assert c.people.analyze_button.isEnabled() is False
    assert c.transport.requests == []


# ------------------------------------------------------- 2. options dialog rules

def test_custom_time_scope_requires_a_range_and_rejects_naive_times(analysis_ui):
    c = analysis_ui
    dialog = options_for(c, "PERSON", c.person.id)
    try:
        assert dialog.current_scope() == "LAST_30_DAYS"
        assert not dialog.custom_start.isEnabled()
        dialog.time_scope.setCurrentIndex(dialog.time_scope.findData(CUSTOM))
        assert dialog.update_custom_state() and dialog.custom_start.isEnabled()

        dialog.custom_start.setText("")
        dialog.custom_end.setText("")
        dialog.accept()
        assert dialog.result() != 1                      # 1 == QDialog.DialogCode.Accepted
        assert "至少需要起点或终点" in c.errors[-1]

        dialog.custom_end.setText("2026-05-01T10:00:00")  # no timezone
        dialog.accept()
        assert dialog.result() != 1
        assert "必须包含时区" in c.errors[-1]

        dialog.custom_start.setText("2026-05-02T10:00:00+08:00")
        dialog.custom_end.setText("2026-05-01T10:00:00+08:00")
        with pytest.raises(DomainError):
            dialog.values()
        dialog.accept()
        assert dialog.result() != 1

        dialog.custom_start.setText("2026-05-01T10:00:00+08:00")
        dialog.custom_end.setText("2026-05-02T10:00:00+08:00")
        values = dialog.values()
        assert values["time_scope"] == CUSTOM
        assert values["custom_start"] == datetime(2026, 5, 1, 2, tzinfo=timezone.utc)
        assert values["custom_end"] == datetime(2026, 5, 2, 2, tzinfo=timezone.utc)
        assert dialog.values()["provider_name"] == "fake"
        dialog.accept()
        assert dialog.result() == 1
    finally:
        dialog.close()
    assert c.transport.requests == []


def test_context_preview_shows_the_text_without_calling_a_model(analysis_ui):
    c = analysis_ui
    dialog = options_for(c, "PERSON", c.person.id, target_label=c.person.display_name)
    try:
        dialog.time_scope.setCurrentIndex(dialog.time_scope.findData(ALL_TIME))
        snapshot = dialog.preview_button.click() or dialog.preview_context()
        c.qt_app.processEvents()
        assert snapshot is not None
        text = dialog.preview.toPlainText()
        assert text == snapshot.rendered_context
        assert "Tester" in text
        assert "事实上下文" in text
        assert snapshot.estimated_tokens > 0
        assert "不调用模型" in dialog.preview_button.text()
    finally:
        dialog.close()
    assert c.transport.requests == []
    assert c.page.history_table.rowCount() == 0
    assert c.errors == []


def test_evidence_text_is_excluded_until_the_user_checks_it(analysis_ui):
    c = analysis_ui
    c.services.evidence.create_evidence(c.ws, c.event.id, kind="TEXT", title="Chat log",
                                        source_type="CHAT", content_text="private-transcript-marker")
    dialog = options_for(c, "EVENT", c.event.id)
    try:
        assert dialog.include_evidence.isChecked() is False
        first = dialog.preview_context()
        assert "private-transcript-marker" not in first.rendered_context
        dialog.include_evidence.setChecked(True)
        second = dialog.preview_context()
        assert "private-transcript-marker" in second.rendered_context
        assert second.estimated_tokens > first.estimated_tokens
    finally:
        dialog.close()
    assert c.transport.requests == []


# --------------------------------------------------------- 3. successful analysis

def test_confirming_runs_the_analysis_and_shows_four_sections(analysis_ui):
    c = analysis_ui
    run, options, progress = perform_run(c, "PERSON", c.person.id)
    assert run is not None
    assert run.status == "SUCCEEDED"
    assert run.provider_name == "fake"
    assert len(c.transport.requests) == 1
    assert c.transport.requests[0]["url"] == CHAT_URL
    body = json.loads(c.transport.requests[0]["body"].decode("utf-8"))
    assert body["model"] == "fake-model"
    assert "Tester" in body["messages"][1]["content"]
    assert progress.worker.isFinished()
    assert progress.timer.isActive() is False

    view = select_run(c.page, run.id)
    assert view.sources.total() >= 1
    dialog = c.page.show_result()
    c.qt_app.processEvents()
    assert dialog is not None
    assert dialog.run.status == "SUCCEEDED"
    # The four headings are separate views; an empty "other" body carries no text.
    assert sorted(dialog.section_views) == ["facts", "interpretations", "observations", "other",
                                            "uncertainties"]
    assert dialog.section_views["other"].toPlainText() == "（本段没有内容）"
    assert dialog.sections["other"] == ""
    assert "Tester 在 2026-05-01 参加 Meeting" in dialog.section_views["facts"].toPlainText()
    assert "通话持续 10 分钟" in dialog.section_views["observations"].toPlainText()
    assert "关系可能仍在维持" in dialog.section_views["interpretations"].toPlainText()
    assert "没有证据支持" in dialog.section_views["uncertainties"].toPlainText()
    assert dialog.fields["状态"].text() == "SUCCEEDED"
    assert dialog.fields["Provider"].text() == "fake"
    assert dialog.fields["模型"].text() == "fake-model"
    assert dialog.fields["耗时"].text() != "—"
    assert "SUCCEEDED" in dialog.status.text()
    assert dialog.referenced_counts()["total"] == view.sources.total()

    snapshot = dialog.show_snapshot()
    c.qt_app.processEvents()
    assert snapshot is not None
    assert snapshot.text.toPlainText() == view.snapshot.rendered_context
    assert "Tester" in snapshot.text.toPlainText()
    snapshot.close()
    dialog.close()
    c.qt_app.processEvents()
    assert c.errors == []


def test_unstructured_answer_lands_in_the_other_section(analysis_ui):
    c = analysis_ui
    run, _, _ = perform_run(c, "PERSON", c.person.id,
                            response=chat_response("no headings at all, just prose"))
    dialog = c.page.show_result()
    try:
        assert dialog.section_views["facts"].toPlainText() == "（本段没有内容）"
        assert "no headings at all" in dialog.section_views["other"].toPlainText()
        assert dialog.sections["facts"] == ""
    finally:
        dialog.close()
    assert run.status == "SUCCEEDED"


# ----------------------------------------------------------------- 4. failure path

def test_auth_failure_keeps_safe_text_and_hides_the_result(analysis_ui):
    c = analysis_ui
    run, options, progress = perform_run(c, "EVENT", c.event.id, response=chat_response(status=401))
    assert run.status == "FAILED"
    assert "401" in run.error_message
    assert c.errors == []                       # a recorded FAILED run is not a UI error
    view = select_run(c.page, run.id)
    assert view.run.status == "FAILED"
    assert c.page.result_button.isEnabled() is False
    assert c.page.snapshot_button.isEnabled() is True     # the snapshot of a failed run exists
    assert "FAILED" in c.page.message.text()
    assert API_KEY not in c.page.message.text()

    dialog = c.page.show_result()
    c.qt_app.processEvents()
    try:
        assert dialog.section_views == {}                 # no result body for a FAILED run
        assert "不显示模型输出正文" in " ".join(
            label.text() for label in dialog.findChildren(type(dialog.status)))
        assert "401" in dialog.status.text()
        assert API_KEY not in dialog.status.text()
    finally:
        dialog.close()
    assert c.transport.requests[0]["headers"].get("Authorization") is None


def test_failed_run_is_listed_and_can_be_deleted_without_touching_facts(analysis_ui):
    c = analysis_ui
    run, _, _ = perform_run(c, "EVENT", c.event.id, response=chat_response(status=401))
    assert history_rows(c.page) == 1
    select_run(c.page, run.id)
    c.page.delete_run()
    assert history_rows(c.page) == 0
    assert c.services.analysis.history(c.ws) == []
    # The fact layer is untouched: the analysed Event is still there.
    assert c.services.events.detail(c.ws, c.event.id).event.id == c.event.id
    assert c.errors == []


# ---------------------------------------------------------------- 5. cancel path

def test_cancel_button_flips_the_token_the_service_received(analysis_ui):
    c = analysis_ui
    c.transport.push(chat_response())
    options = c.page.analyze_target("PERSON", c.person.id)
    c.qt_app.processEvents()
    options.accept()
    c.qt_app.processEvents()
    progress = options.progress
    pump(c.qt_app, lambda: progress.worker.isFinished())
    c.qt_app.processEvents()
    token = progress.worker.token
    assert isinstance(token, CancelToken)
    assert token.cancelled is False
    progress.cancel_button.click()
    assert token.cancelled is True
    assert progress.cancel_button.isEnabled() is False
    assert "已请求取消" in progress.status.text()
    assert progress.final_run.status == "SUCCEEDED"
    assert c.page.history_table.rowCount() == 1


def test_worker_cancellation_is_recorded_as_cancelled_and_shows_no_result(analysis_ui):
    c = analysis_ui
    run, _, _ = perform_run(c, "PERSON", c.person.id,
                            before=lambda progress: progress.cancel_requested())
    assert run.status == "CANCELLED"
    assert run.result is None
    view = select_run(c.page, run.id)
    assert view.run.status == "CANCELLED"
    assert c.page.result_button.isEnabled() is False
    assert "CANCELLED" in c.page.message.text()

    dialog = c.page.show_result()
    c.qt_app.processEvents()
    try:
        assert dialog.section_views == {}
        assert "CANCELLED" in dialog.status.text()
        assert "取消" in dialog.status.text()
    finally:
        dialog.close()
    # A cancelled run is finished: the pages no longer offer to cancel it.
    with pytest.raises(DomainError):
        c.services.analysis.cancel_run(c.ws, run.id)
    assert c.page.cancel_button.isEnabled() is False


def test_page_cancel_action_cancels_an_unfinished_run(analysis_ui):
    c = analysis_ui
    request = AnalysisRequest(workspace_id=c.ws, target_type="PERSON", target_id=c.person.id,
                              analysis_type="PERSON_PROFILE", as_of=datetime.now(timezone.utc),
                              time_scope=LAST_7_DAYS)
    token = CancelToken()
    token.cancel()
    run = c.services.analysis.create_run(request, cancel=token)
    assert run.status == "CANCELLED"
    c.page.refresh(run.id)
    view = select_run(c.page, run.id)
    assert view.run.status == "CANCELLED"
    assert c.page.message.text().startswith("状态 CANCELLED")
    assert "取消" in c.page.message.text()
    assert c.page.cancel_button.isEnabled() is False
    assert c.page.delete_button.isEnabled() is True
    c.page.delete_run()
    assert c.services.analysis.history(c.ws) == []
    assert c.errors == []


# --------------------------------------------------------------- 6. history page

def test_history_lists_every_run_and_never_overwrites_one(analysis_ui):
    c = analysis_ui
    first, _, _ = perform_run(c, "PERSON", c.person.id)
    second, _, _ = perform_run(c, "EVENT", c.event.id)
    assert first.id != second.id
    assert len({first.id, second.id}) == 2
    assert history_rows(c.page) == 2
    assert {row.run.id for row in c.page.runs} == {first.id, second.id}
    assert {row.run.target_type for row in c.page.runs} == {"PERSON", "EVENT"}
    # Newest first: the ledger is ordered by creation time, never rewritten.
    assert run_ids(c.page)[0] == second.id
    assert [row.run.status for row in c.page.runs] == ["SUCCEEDED", "SUCCEEDED"]
    assert len(c.services.analysis.history(c.ws)) == 2
    assert len({row.run.id for row in c.services.analysis.history(c.ws)}) == 2
    assert "分析记录 2 条" in c.page.status.text()
    assert c.errors == []


def test_history_filter_refresh_and_detail_summary(analysis_ui):
    c = analysis_ui
    person_run, _, _ = perform_run(c, "PERSON", c.person.id)
    event_run, _, _ = perform_run(c, "EVENT", c.event.id)
    c.page.filter_combo.setCurrentIndex(c.page.filter_combo.findData("PERSON"))
    assert history_rows(c.page) == 1
    assert run_ids(c.page) == [person_run.id]
    c.page.filter_combo.setCurrentIndex(c.page.filter_combo.findData("EVENT"))
    assert run_ids(c.page) == [event_run.id]
    c.page.filter_combo.setCurrentIndex(0)
    c.page.refresh_button.click()
    assert history_rows(c.page) == 2

    view = select_run(c.page, person_run.id)
    assert c.page.detail_fields["analysis_type"].text() == "PERSON_PROFILE"
    assert c.page.detail_fields["provider"].text() == "fake"
    assert c.page.detail_fields["model"].text() == "fake-model"
    assert "合计" in c.page.detail_fields["sources"].text()
    assert "Tester" in c.page.detail_fields["target"].text()
    assert view.run.status == "SUCCEEDED"
    assert "Facts" in c.page.section_summary(view.run)
    assert "Tester 在 2026-05-01 参加 Meeting" in c.page.summary.toPlainText()
    assert c.page.result_button.isEnabled() is True
    assert c.page.snapshot_button.isEnabled() is True
    assert c.errors == []


def test_analysis_history_survives_a_restart_with_the_same_database(analysis_ui):
    c = analysis_ui
    run, _, _ = perform_run(c, "PERSON", c.person.id)
    c.window.navigation.setCurrentRow(4)                    # reactivating reloads the ledger
    assert history_rows(c.page) == 1
    assert run_ids(c.page) == [run.id]
    c.page.activate()
    assert history_rows(c.page) == 1
    assert c.page.history_table.item(0, 1).text() == "人物"
    assert c.page.history_table.item(0, 6).text() == "SUCCEEDED"
    assert c.transport.requests and len(c.transport.requests) == 1


def test_analysis_row_opens_the_analysed_fact_on_its_own_page(analysis_ui):
    c = analysis_ui
    run, _, _ = perform_run(c, "PERSON", c.person.id)
    select_run(c.page, run.id)
    c.page.open_selected_target()
    c.qt_app.processEvents()
    assert c.window.pages.currentIndex() == 1
    assert c.people.current_detail.person.id == c.person.id
    assert c.window.analysis_page is c.page
    assert c.window.open_target("analysis_run", run.id) is None
    assert c.window.pages.currentIndex() == 4
    assert c.page.current_view.run.id == run.id
    assert c.errors == []


# ------------------------------------------------------------ 7. provider dialog

def test_provider_page_handles_missing_configuration_gracefully(analysis_ui):
    c = analysis_ui
    c.gateway.remove_provider("fake")
    assert c.gateway.config().names() == ()
    c.page.activate()
    assert "尚未配置 Provider" in c.page.status.text()
    assert c.page.provider_button.isEnabled() is True
    # The entry point explains itself instead of crashing.
    assert open_options(c, "PERSON", c.person.id) is None
    assert "Provider" in c.errors[-1]
    assert c.transport.requests == []
    dialog = options_for(c, "PERSON", c.person.id)
    try:
        assert dialog.provider.count() == 0
        assert dialog.update_provider_state() is False
        assert "尚未配置任何 LLM Provider" in dialog.provider_note.text()
    finally:
        dialog.close()
    assert c.transport.requests == []


def test_provider_panel_adds_edits_defaults_and_removes_providers(analysis_ui):
    c = analysis_ui
    panel = ProviderSettingsDialog(c.services, c.page)
    try:
        assert panel.table.rowCount() == 1
        assert panel.table.item(0, 0).text() == "fake"
        panel.save_provider(LLMProviderConfig(provider_name="second",
                                              base_url="http://127.0.0.1:9/v1", model="m2",
                                              temperature=0.2, timeout=12.0))
        assert panel.table.rowCount() == 2
        assert c.gateway.config().names() == ("fake", "second")
        panel.select("second")
        panel.set_default()
        assert c.gateway.config().default_provider == "second"
        assert panel.table.item(panel.select("second"), 6).text() == "★ 默认"
        panel.delete_provider()
        assert c.gateway.config().names() == ("fake",)
        assert panel.table.rowCount() == 1
    finally:
        panel.close()
    assert c.transport.requests == []
    assert c.errors == []


def test_provider_dialog_writes_no_secret_to_disk_or_log(analysis_ui, monkeypatch, caplog):
    c = analysis_ui
    stored = LLMProviderConfig(provider_name="with-ref",
                               base_url="http://127.0.0.1:9/v1", model="m",
                               credential_ref="env:TEST_KEY")
    stub = SimpleNamespace(exec=lambda: 1, values=lambda: stored)
    monkeypatch.setattr(dialog_module, "ProviderDialog", lambda *args, **kwargs: stub)
    panel = ProviderSettingsDialog(c.services, c.page)
    try:
        with caplog.at_level(logging.DEBUG, logger="app"):
            panel.add_provider()
        assert panel.selected_name() == "with-ref"
        assert panel.table.item(panel.table.currentRow(), 3).text() == "env:TEST_KEY"
        assert "env:TEST_KEY" in c.gateway.credential_reference("with-ref")
    finally:
        panel.close()
    path = c.config.storage_paths.data_dir / "llm-providers.toml"
    text = path.read_text(encoding="utf-8")
    assert "provider_name = \"with-ref\"" in text
    assert "credential_ref" in text and "env:TEST_KEY" in text
    assert API_KEY not in text and "sk-" not in text
    assert API_KEY not in caplog.text
    assert c.errors == []


def test_credential_reference_is_used_but_never_shown_or_logged(analysis_ui, monkeypatch, caplog):
    c = analysis_ui
    monkeypatch.setenv("TEST_KEY", API_KEY)
    c.gateway.save_provider(LLMProviderConfig(provider_name="with-ref",
                                              base_url="http://127.0.0.1:9/v1", model="m",
                                              credential_ref="env:TEST_KEY"))
    c.gateway.set_default("with-ref")            # the analysis runs through this Provider
    run, _, _ = perform_run(c, "PERSON", c.person.id,
                            response=HttpResponse(401, b'{"error":"unauthorized"}', {}, CHAT_URL))
    header = c.transport.requests[0]["headers"].get("Authorization")
    assert header == f"Bearer {API_KEY}"          # the reference resolved to the real key
    view = select_run(c.page, run.id)
    assert run.status == "FAILED"
    dialog = c.page.show_result()
    c.qt_app.processEvents()
    try:
        texts = [dialog.status.text()] + [label.text() for label in dialog.fields.values()]
        assert API_KEY not in " ".join(texts)
        assert "sk-" not in " ".join(texts)
    finally:
        dialog.close()
    assert "env:TEST_KEY" in json.dumps(view.scope)
    assert API_KEY not in json.dumps(view.scope)
    assert API_KEY not in caplog.text


def test_provider_test_button_reports_status_and_model_count(analysis_ui):
    c = analysis_ui
    panel = ProviderSettingsDialog(c.services, c.page)
    try:
        panel.select("fake")
        c.transport.push(models_response(("fake-model",)))
        status = panel.test_provider()
        assert status.ok is True
        assert panel.status.text().startswith("连接成功")
        assert "模型 1 个" in panel.status.text()
        assert "credential_ref" in panel.status.text() or "凭据引用" in panel.status.text()
        assert len(c.transport.requests) == 1
        c.transport.push(HttpResponse(503, b'{"error":"down"}', {}, "http://127.0.0.1:9/v1/models"))
        failed = panel.test_provider()
        assert failed.ok is False
        assert panel.status.text().startswith("连接失败")
        assert "模型服务" in panel.status.text()          # the provider's own safe text
        assert "503" in panel.status.text()
        assert c.errors == []
    finally:
        panel.close()


def test_secret_like_input_is_rejected_without_echoing_it(analysis_ui):
    c = analysis_ui
    panel = ProviderSettingsDialog(c.services, c.page)
    try:
        # A raw key is not a reference: it is refused, and the refusal never echoes it.
        monkeypatch_dialog = dialog_module.ProviderDialog(panel)
        try:
            monkeypatch_dialog.name.setText("bad")
            monkeypatch_dialog.base_url.setText("http://127.0.0.1:9/v1")
            monkeypatch_dialog.model.setText("m")
            monkeypatch_dialog.credential_ref.setText(API_KEY)
            with pytest.raises(Exception) as failure:
                monkeypatch_dialog.values()
            assert "credential_ref" in str(failure.value)
            assert API_KEY not in str(failure.value)

            monkeypatch_dialog.extra_headers.setPlainText("Authorization: Bearer " + API_KEY)
            monkeypatch_dialog.credential_ref.setText("")
            with pytest.raises(Exception) as failure:
                monkeypatch_dialog.values()
            assert "extra_headers" in str(failure.value)
            assert API_KEY not in str(failure.value)
        finally:
            monkeypatch_dialog.close()
    finally:
        panel.close()
    assert c.errors == []


# ---------------------------------------------------------------- 8. log privacy

def test_analysis_logs_never_contain_evidence_text_or_keys(analysis_ui, caplog):
    c = analysis_ui
    private = "private-evidence-text-must-not-be-logged"
    c.services.evidence.create_evidence(c.ws, c.event.id, kind="TEXT", title="Chat log",
                                        source_type="CHAT", content_text=private)
    with caplog.at_level(logging.DEBUG, logger="app"):
        options = c.page.analyze_target("EVENT", c.event.id)
        c.qt_app.processEvents()
        options.include_evidence.setChecked(True)      # the user asks for raw text
        c.transport.push(chat_response())
        run, _ = run_to_completion(c, options)
    assert run.status == "SUCCEEDED"
    context = c.services.analysis.get(c.ws, run.id).snapshot.rendered_context
    assert private in context                          # sent to the model ...
    assert private not in caplog.text                  # ... but never written to a log
    assert API_KEY not in caplog.text
    assert "evidence_content" not in caplog.text
    assert FOUR_SECTIONS.splitlines()[0] not in caplog.text


def test_unexpected_worker_failure_is_generic_in_ui_and_log(analysis_ui, monkeypatch, caplog):
    c = analysis_ui
    secret = "worker-internal-detail"

    class Exploding:
        """A service stub that fails with a private, unexpected error."""

        analysis_type_for = staticmethod(lambda target_type: {
            "PERSON": "PERSON_PROFILE", "RELATIONSHIP": "RELATIONSHIP_ANALYSIS",
            "EVENT": "EVENT_ANALYSIS"}[target_type])

        def create_run(self, request, *, provider_name=None, cancel=None):
            raise RuntimeError(secret)

    broken = replace(c.services, analysis=Exploding())
    monkeypatch.setattr(c.page, "services", broken)
    monkeypatch.setattr(c.page, "analysis", broken.analysis)
    c.page.analyze_target("PERSON", c.person.id)
    c.qt_app.processEvents()
    options = c.page._dialogs[-1]
    with caplog.at_level(logging.DEBUG, logger="app"):
        options.accept()
        c.qt_app.processEvents()
        progress = options.progress
        pump(c.qt_app, lambda: progress.worker.isFinished())
        c.qt_app.processEvents()
    assert progress.final_error is not None
    assert secret not in progress.final_error.message
    assert "技术错误" in progress.final_error.message
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
    assert c.transport.requests == []


def test_missing_analysis_service_is_explained_not_crashed(analysis_ui):
    c = analysis_ui
    page = c.page
    original = page.analysis
    page.analysis = None
    try:
        assert page.analyze_target("PERSON", c.person.id) is None
        assert "尚未装配" in c.errors[-1]
        page.refresh()
        assert "尚未装配" in page.status.text()
        assert page.history_table.rowCount() == 0
        assert page.cancel_run() is None
        assert page.delete_run() is None
        assert page.show_result() is None
        assert page.show_snapshot() is None
    finally:
        page.analysis = original
    assert c.transport.requests == []


def test_options_dialog_reuses_the_open_instance_instead_of_starting_twice(analysis_ui):
    c = analysis_ui
    first = open_options(c, "PERSON", c.person.id)
    second = dialog_module.run_analysis(c.services, c.ws, "PERSON", c.person.id,
                                        parent=c.page, owner=c.page, dialog=first)
    assert second is first
    assert c.transport.requests == []
    first.close()
    c.qt_app.processEvents()
    assert run_ids(c.page) == []


def test_buttons_outside_the_shell_keep_a_hidden_run_holder(analysis_ui):
    """Without the shell there is no page: People still works, on a hidden holder."""
    c = analysis_ui
    page = PeoplePage(c.services)
    try:
        page.show()
        c.qt_app.processEvents()
        page.refresh_people(c.person.id)
        c.qt_app.processEvents()
        dialog = page.analyze_person()
        c.qt_app.processEvents()
        holder = page._analysis_page
        assert holder is not None and holder.analysis is not None
        assert holder.isVisible() is False           # never rendered inside People
        assert holder.parent() is page
        assert isinstance(dialog, AnalysisOptionsDialog)
        assert dialog.workspace_id == c.ws
        dialog.close()
    finally:
        page.close()
        c.qt_app.processEvents()
    assert c.transport.requests == []
    assert c.errors == []
