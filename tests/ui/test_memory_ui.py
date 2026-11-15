"""Phase 9 Memory UI: Analysis Sources / Save as Memory / Memory management.

Like every other UI test in this repository the module runs against the **real**
SQLite database, the real migrations and the real services; only the HTTP
transport is fake, so no test can reach the network. What is under test is what a
user can observe:

* the Analysis detail expands four Sources groups, every row jumps to the fact it
  references (an Evidence row lands on its Event with that evidence selected), and
  opening the page calls no model;
* candidate references from a model answer are labelled as candidates, only the
  ones the Service accepted can be activated, and nothing is written by showing
  them;
* "Save as Memory" writes only after the user presses 纭淇濆瓨: opening the dialog
  and cancelling create nothing, and the stored content is the *edited* text;
* the Person Memory panel adds MANUAL memories, edits content/kind without ever
  changing the source, soft-deletes, restores, and hides expired/deleted rows
  until the matching switch is ticked;
* the default analysis context contains no Memory unless the user opts in;
* Memory content never reaches the log.
"""
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.ui.analysis_dialog import MemoryConfirmDialog
from app.ui.analysis_page import CANDIDATE_ACCEPTED, CANDIDATE_REJECTED
from app.ui.main_window import MainWindow
from app.ui.memory_panel import MemoryEditDialog, MemoryPanel, MemoryWorkspaceDialog
from tests.ui.test_analysis_ui import FOUR_SECTIONS, chat_response

ROLE = Qt.ItemDataRole.UserRole
MARKER_EVENT = "[EVENT:{event}]"
MARKER_STRAY_EVIDENCE = "[EVIDENCE:{evidence}]"
PRIVATE_CONTENT = "private-memory-content-must-not-be-logged"
SOURCE_KINDS = ("PERSON", "RELATIONSHIP", "EVENT", "EVIDENCE")


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


@pytest.fixture
def memory_ui(tmp_path, qt_app, monkeypatch):
    """Real database + real services + FakeTransport; UI errors are captured."""
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = AppConfig(data_dir=tmp_path / "phase9-memory-data")
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
        event = services.events.create_event(
            ws, "Meeting", "Call", "FACT",
            started_at=datetime.now(timezone.utc) - timedelta(days=3), participants=(person.id,))
        evidence = services.evidence.create_evidence(ws, event.id, kind="TEXT", title="Chat log",
                                                     source_type="CHAT", content_text="secret log")
        gateway.save_provider(LLMProviderConfig(provider_name="fake",
                                                base_url="http://127.0.0.1:9/v1",
                                                model="fake-model"))
        window = MainWindow(config, services)
        window.show()
        window.navigation.setCurrentRow(4)                 # Analysis page, then activate
        qt_app.processEvents()
        yield SimpleNamespace(config=config, runtime=runtime, services=services, gateway=gateway,
                              transport=transport, ws=ws, person=person, other=other,
                              relationship=relationship, event=event, evidence=evidence,
                              window=window, people=window.people_page, events=window.events_page,
                              page=window.analysis_page, memories=services.memories,
                              qt_app=qt_app, errors=errors)
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        runtime.close()


# ---------------------------------------------------------------------- helpers

def run_analysis(case, target_type, target_id, *, response=None, expect="SUCCEEDED"):
    """One real analysis run through the service with the fake transport."""
    case.transport.push(response if response is not None else chat_response())
    request = AnalysisRequest(workspace_id=case.ws, target_type=target_type, target_id=target_id,
                              analysis_type=case.services.analysis.analysis_type_for(target_type),
                              as_of=datetime.now(timezone.utc), time_scope=ALL_TIME)
    run = case.services.analysis.create_run(request)
    assert run.status == expect
    return run


def patched_result(case, run, result):
    """Store an explicit answer (the UI only ever reads what the service holds)."""
    from app.services.unit_of_work import transaction
    from app.storage.analysis_repositories import SQLAnalysisRunRepository
    with transaction(case.runtime.sessions, write=True) as session:
        repository = SQLAnalysisRunRepository(session)
        stored = repository.get(case.ws, run.id)
        values = {name: getattr(stored, name) for name in stored.__dataclass_fields__}
        values["result"] = result
        return repository.update(type(stored)(**values))


def show_run(case, run):
    case.page.refresh(run.id)
    case.qt_app.processEvents()
    return case.page.current_view


def source_rows(case, kind):
    return case.page.source_rows[kind]


def open_person(case, person_id=None):
    """Open the People page and select one person (so its panels are scoped)."""
    case.window.navigation.setCurrentRow(1)
    case.qt_app.processEvents()
    case.people.refresh_people(person_id or case.person.id)
    case.qt_app.processEvents()
    return case.people.current_detail


# ------------------------------------------------- 1. Sources list and jumping

def test_sources_detail_lists_four_groups_and_never_calls_a_model(memory_ui):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    assert len(c.transport.requests) == 1                 # the run the user asked for
    show_run(c, run)

    counts = {kind: source_rows(c, kind).rowCount() for kind in SOURCE_KINDS}
    for kind in SOURCE_KINDS:
        assert counts[kind] >= 1, f"{kind} must be listed as a source"
        assert f"（{counts[kind]}）" in c.page.source_counts[kind].text()
    assert source_rows(c, "PERSON").item(0, 0).text() == "Tester"
    assert source_rows(c, "EVENT").item(0, 0).text() == "Meeting"
    assert "可跳转" in source_rows(c, "PERSON").item(0, 2).text()
    assert source_rows(c, "EVIDENCE").item(0, 0).text() == "Chat log"

    # Only reference information is copied into the list - never a fact body.
    for kind in SOURCE_KINDS:
        table = source_rows(c, kind)
        for row in range(table.rowCount()):
            text = " ".join(table.item(row, column).text() for column in range(3))
            assert "secret log" not in text

    # Re-reading the detail must not touch the transport again.
    c.page.refresh(run.id)
    assert len(c.transport.requests) == 1
    assert c.errors == []


def test_clicking_a_source_row_really_jumps_to_that_fact(memory_ui):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    show_run(c, run)
    c.window.navigation.setCurrentRow(4)                   # stay on Analysis
    c.qt_app.processEvents()

    person_rows = source_rows(c, "PERSON")
    assert person_rows.current_target() is None
    person_rows.selectRow(0)
    person_rows.activate_current()
    c.qt_app.processEvents()
    assert c.window.pages.currentIndex() == 1              # People page
    assert c.people.current_detail.person.id == c.person.id

    c.page.refresh(run.id)
    relation_rows = source_rows(c, "RELATIONSHIP")
    assert relation_rows.current_target() is None
    relation_rows.selectRow(0)
    assert relation_rows.current_target() == c.relationship.id
    relation_rows.activate_current()
    c.qt_app.processEvents()
    assert c.window.pages.currentIndex() == 1
    assert c.people.current_detail is not None
    assert len(c.transport.requests) == 1
    assert c.errors == []


def test_an_evidence_source_lands_on_its_event_with_that_evidence_selected(memory_ui):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    show_run(c, run)
    rows = source_rows(c, "EVIDENCE")
    target = next((row for row in range(rows.rowCount())
                   if rows.item(row, 0).data(ROLE) == c.evidence.id), None)
    assert target is not None, "the analysed evidence must be listed as a source"
    rows.selectRow(target)
    rows.activate_current()
    c.qt_app.processEvents()

    assert c.window.pages.currentIndex() == 2              # Events page
    assert c.events.current_detail is not None
    assert c.events.current_detail.event.id == c.event.id
    selected = c.events.evidence_panel.selected_detail()
    assert selected is not None
    assert selected.evidence.id == c.evidence.id
    assert len(c.transport.requests) == 1
    assert c.errors == []


def test_the_shell_routes_the_evidence_branch_to_the_owning_event(memory_ui):
    c = memory_ui
    assert c.window.open_target("evidence", c.evidence.id) is None
    c.qt_app.processEvents()
    assert c.window.pages.currentIndex() == 2
    assert c.events.current_detail.event.id == c.event.id
    assert c.events.evidence_panel.selected_detail().evidence.id == c.evidence.id
    assert c.transport.requests == []


# ------------------------------------------------------- 2. candidate references

def test_candidates_are_labelled_and_only_accepted_ones_are_actionable(memory_ui):
    c = memory_ui
    original = run_analysis(c, "EVENT", c.event.id)
    # One marker points at a real source of this run, one at an evidence that is not.
    result = (FOUR_SECTIONS + "\n\n## 引用\n- 会议事件 " + MARKER_EVENT.format(event=c.event.id)
              + "\n- 另一条证据 " + MARKER_STRAY_EVIDENCE.format(evidence=uuid4()))
    run = patched_result(c, original, result)
    show_run(c, run)

    candidates = c.services.memories.candidate_references(c.ws, run.id)
    assert len(candidates) == 2
    accepted = [item for item in candidates if item.accepted]
    rejected = [item for item in candidates if not item.accepted]
    assert [item.entity_type for item in accepted] == ["EVENT"]
    assert accepted[0].entity_id == c.event.id
    assert [item.entity_type for item in rejected] == ["EVIDENCE"]

    assert c.page.candidates.rowCount() == 2
    verdicts = [c.page.candidates.item(row, 2).text() for row in range(2)]
    assert verdicts.count(CANDIDATE_ACCEPTED) == 1
    assert verdicts.count(CANDIDATE_REJECTED) == 1
    assert "候选" in c.page.candidate_note.text()
    assert "被接受 1" in c.page.candidate_note.text()
    assert "无效 1" in c.page.candidate_note.text()
    assert "不会自动创建 Memory" in c.page.candidate_note.text()
    # Only the accepted row carries a jump target; the rejected row has none.
    targets = [c.page.candidates.item(row, 1).data(ROLE) for row in range(2)]
    assert sum(1 for item in targets if item is not None) == 1
    accepted_row = targets.index(next(item for item in targets if item is not None))
    assert c.page.candidates.item(accepted_row, 2).text() == CANDIDATE_ACCEPTED
    assert c.page.candidates.item(accepted_row, 1).text().startswith("Meeting")
    # Showing candidates writes nothing: no Memory, no fact change.
    assert c.memories.list_all(c.ws) == []
    assert c.services.events.detail(c.ws, c.event.id).event.id == c.event.id
    assert c.errors == []


def test_an_accepted_candidate_jumps_and_a_rejected_one_cannot(memory_ui):
    c = memory_ui
    original = run_analysis(c, "EVENT", c.event.id)
    result = "## 寮曠敤\n- " + MARKER_EVENT.format(event=c.event.id) + "\n- " \
             + MARKER_STRAY_EVIDENCE.format(evidence=uuid4())
    run = patched_result(c, original, result)
    show_run(c, run)
    assert c.page.candidates.rowCount() == 2
    assert c.page.candidates.currentRow() < 0              # no row is selected as an action
    assert c.page.open_candidate() is None                 # nothing to activate
    for row in range(c.page.candidates.rowCount()):
        if c.page.candidates.item(row, 2).text() == CANDIDATE_ACCEPTED:
            c.page.candidates.selectRow(row)
            break
    c.page.open_candidate()
    c.qt_app.processEvents()
    assert c.window.pages.currentIndex() == 2
    assert c.events.current_detail.event.id == c.event.id
    assert c.memories.list_all(c.ws) == []
    assert c.errors == []


# -------------------------------------------------------- 3. Save as Memory flow

def test_save_as_memory_requires_an_explicit_confirmation(memory_ui):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    show_run(c, run)
    assert c.page.memory_button.isEnabled() is True

    dialog = c.page.save_memory()
    c.qt_app.processEvents()
    assert isinstance(dialog, MemoryConfirmDialog)
    assert dialog.isVisible() is True
    # Opening the dialog is not a confirmation: nothing exists yet.
    assert c.memories.list_all(c.ws) == []
    assert dialog.created_memory is None
    assert dialog.content.toPlainText() == c.page.suggested_memory()
    assert dialog.fields["analysis_run_id"].text() == str(run.id)
    assert dialog.fields["model"].text() == run.model_name
    assert dialog.kind.currentData() == "FACT"
    assert dialog.person.currentData() is None             # default = Workspace level
    assert dialog.valid_from.text() == "" and dialog.valid_until.text() == ""

    # Cancelling writes nothing either.
    dialog.cancel_button.click()
    c.qt_app.processEvents()
    assert dialog.created_memory is None
    assert c.memories.list_all(c.ws) == []

    # A naive timestamp and a reversed window are refused before any write.
    dialog = c.page.save_memory()
    c.qt_app.processEvents()
    assert dialog.created_memory is None
    dialog.valid_from.setText("2026-10-03T18:00:00")
    dialog.confirm()
    assert "时区" in c.errors[-1]
    assert c.memories.list_all(c.ws) == []
    dialog.valid_from.setText("2026-10-03T18:00:00+08:00")
    dialog.valid_until.setText("2026-10-01T18:00:00+08:00")
    dialog.confirm()
    assert "不能早于" in c.errors[-1]
    assert dialog.parsed is None
    assert c.memories.list_all(c.ws) == []
    dialog.valid_until.setText("")

    # Only 确认保存 creates the Memory, and it stores the edited text.
    edited = "用户编辑后的记忆内容：Tester 与 Other 的关系仍在维持。"
    assert edited != dialog.content.toPlainText()
    dialog.content.setPlainText(edited)
    dialog.select_kind("PREFERENCE")
    dialog.select_person(c.person.id)
    dialog.confirm()
    c.qt_app.processEvents()
    created = dialog.created_memory
    assert created is not None
    assert created.source_type == "ANALYSIS"
    assert created.source_analysis_id == run.id
    assert created.confirmed_at is not None
    assert created.content == edited
    assert created.kind == "PREFERENCE"
    assert created.person_id == c.person.id
    stored = c.memories.list_all(c.ws)
    assert [item.id for item in stored] == [created.id]
    assert stored[0].content == edited
    assert len(c.transport.requests) == 1
    # Only the two deliberate invalid inputs were surfaced; nothing else failed.
    assert len(c.errors) == 2
    assert "时区" in c.errors[0] and "不能早于" in c.errors[1]


def test_save_as_memory_is_offered_only_for_a_finished_answer(memory_ui):
    c = memory_ui
    run_analysis(c, "PERSON", c.person.id, expect="FAILED",
                 response=HttpResponse(401, b"{}", {}, "http://127.0.0.1:9/v1/chat/completions"))
    failed = c.services.analysis.history(c.ws)[0].run
    assert failed.status == "FAILED"
    show_run(c, failed)
    assert c.page.memory_button.isEnabled() is False
    assert c.page.save_memory() is None
    assert c.memories.list_all(c.ws) == []
    assert c.errors == []


def test_the_suggested_text_is_editable_and_can_be_replaced(memory_ui):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    show_run(c, run)
    suggestion = c.page.suggested_memory()
    assert "Facts" in suggestion                           # the stored answer is suggested
    dialog = c.page.save_memory()
    c.qt_app.processEvents()
    assert dialog.windowTitle().startswith("保存为 Memory")
    dialog.content.setPlainText("完全由用户写下的内容")
    dialog.confirm()
    c.qt_app.processEvents()
    assert dialog.created_memory.content == "完全由用户写下的内容"
    assert c.memories.list_all(c.ws)[0].content == "完全由用户写下的内容"


# --------------------------------------------------------- 4. Person memories

def test_person_panel_adds_edits_deletes_and_restores_a_manual_memory(memory_ui):
    c = memory_ui
    open_person(c, c.person.id)
    panel = c.people.memory_panel
    assert panel.person_id == c.person.id
    assert panel.table.rowCount() == 0

    manual = panel.create_memory({"content": PRIVATE_CONTENT, "kind": "FACT"})
    c.qt_app.processEvents()
    assert manual is not None
    assert manual.source_type == "MANUAL"
    assert manual.source_analysis_id is None
    assert manual.confirmed_at is not None
    assert manual.person_id == c.person.id
    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 0).text() == PRIVATE_CONTENT
    assert panel.table.item(0, 1).text().startswith("FACT")
    assert panel.table.item(0, 2).text().startswith("MANUAL")

    edited = panel.update_memory(manual, {"content": "编辑后的内容", "kind": "CONTEXT",
                                          "person_id": c.person.id})
    c.qt_app.processEvents()
    assert edited.content == "编辑后的内容"
    assert edited.kind == "CONTEXT"
    assert edited.updated_at > manual.updated_at
    assert edited.source_type == "MANUAL" and edited.source_analysis_id is None
    assert edited.confirmed_at == manual.confirmed_at
    assert panel.table.item(0, 0).text() == "编辑后的内容"

    removed = panel.delete_memory()
    c.qt_app.processEvents()
    assert removed.deleted_at is not None
    assert panel.table.rowCount() == 0                     # hidden by default
    assert c.memories.list(c.ws, person_id=c.person.id) == []

    panel.include_deleted.setChecked(True)
    c.qt_app.processEvents()
    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 0).text().startswith("已删除 · ")
    assert panel.restore_button.isEnabled() is True

    panel.restore_memory()
    c.qt_app.processEvents()
    assert c.memories.list(c.ws, person_id=c.person.id)[0].deleted_at is None
    panel.include_deleted.setChecked(False)
    assert panel.table.rowCount() == 1
    assert c.transport.requests == []
    assert c.errors == []


def test_workspace_level_memories_live_outside_the_person_list(memory_ui):
    c = memory_ui
    open_person(c, c.person.id)
    person_panel = c.people.memory_panel
    person_panel.create_memory({"content": "人物记忆", "kind": "FACT"})
    c.qt_app.processEvents()
    assert len(c.memories.list(c.ws, person_id=c.person.id)) == 1
    assert c.memories.list(c.ws) == []                     # Workspace level is empty

    dialog = c.people.open_workspace_memories()
    c.qt_app.processEvents()
    assert isinstance(dialog, MemoryWorkspaceDialog)
    panel = dialog.panel
    assert panel.person_id is None
    assert panel.table.rowCount() == 0
    manual = panel.create_memory({"content": "Workspace 记忆", "kind": "CONTEXT"})
    c.qt_app.processEvents()
    assert manual.person_id is None
    assert manual.source_type == "MANUAL"
    assert panel.table.rowCount() == 1
    # The person panel does not list it, the Workspace list does.
    assert person_panel.table.rowCount() == 1
    assert person_panel.table.item(0, 0).text() == "人物记忆"
    assert [item.content for item in c.memories.list(c.ws)] == ["Workspace 记忆"]
    assert [item.content for item in c.memories.list(c.ws, person_id=c.person.id)] == ["人物记忆"]
    dialog.close()
    c.qt_app.processEvents()
    assert c.transport.requests == []
    assert c.errors == []


# ------------------------------------------------------------ 5. validity window

def test_expired_and_future_memories_hide_until_the_switch_is_on(memory_ui):
    c = memory_ui
    open_person(c, c.person.id)
    panel = MemoryPanel(c.services, c.people)
    panel.set_person(c.ws, None)
    try:
        future = c.memories.create(c.ws, "未来才生效", kind="FACT",
                                   valid_from=datetime.now(timezone.utc) + timedelta(days=30))
        expired = c.memories.create(c.ws, "已经过期", kind="FACT",
                                    valid_until=datetime.now(timezone.utc) - timedelta(days=1))
        current = c.memories.create(c.ws, "当前有效", kind="FACT")
        assert c.memories.is_current(future) is False
        assert c.memories.is_current(expired) is False
        assert c.memories.is_current(current) is True

        panel.refresh()
        c.qt_app.processEvents()
        assert panel.table.rowCount() == 1
        assert panel.table.item(0, 0).text() == "当前有效"

        panel.include_expired.setChecked(True)
        c.qt_app.processEvents()
        assert panel.table.rowCount() == 3
        shown = {panel.table.item(row, 0).text() for row in range(3)}
        assert shown == {"当前有效", "未来才生效", "已经过期"}
        assert "含过期／未生效" in panel.status.text()
        panel.include_expired.setChecked(False)
        c.qt_app.processEvents()
        assert panel.table.rowCount() == 1

        # The service refuses a reversed window; the editor refuses it earlier.
        with pytest.raises(DomainError):
            c.memories.create(c.ws, "坏窗口", kind="FACT",
                              valid_from=datetime(2026, 5, 2, tzinfo=timezone.utc),
                              valid_until=datetime(2026, 5, 1, tzinfo=timezone.utc))
        editor = MemoryEditDialog(panel, allow_person=False)
        editor.valid_from.setPlainText("2026-10-03T18:00:00")
        with pytest.raises(DomainError):
            editor.boundaries()
        editor.valid_from.setPlainText("2026-10-03T18:00:00+08:00")
        editor.valid_until.setPlainText("2026-10-01T18:00:00+08:00")
        with pytest.raises(DomainError):
            editor.boundaries()
        editor.valid_from.setPlainText("")
        editor.valid_until.setPlainText("")
        assert editor.boundaries() == (None, None)
        with pytest.raises(DomainError):
            panel.service.create(c.ws, "无时区", kind="FACT",
                                 valid_from=datetime(2026, 5, 1))
        editor.content.setPlainText("")
        with pytest.raises(DomainError):
            editor.values()
        editor.close()
    finally:
        panel.close()
        c.qt_app.processEvents()
    assert c.transport.requests == []
    assert c.errors == []


def test_the_memory_table_stays_read_only(memory_ui):
    c = memory_ui
    open_person(c, c.person.id)
    panel = c.people.memory_panel
    panel.create_memory({"content": "不可就地编辑", "kind": "FACT"})
    c.qt_app.processEvents()
    from PySide6.QtWidgets import QTableWidget
    assert panel.table.editTriggers() == QTableWidget.EditTrigger.NoEditTriggers
    assert panel.table.selectionBehavior() == QTableWidget.SelectionBehavior.SelectRows
    # Nothing in the list widget is a content editor: a Memory changes only through
    # the edit dialog, which writes through the service boundary.
    assert not hasattr(panel, "content")
    assert c.memories.list(c.ws, person_id=c.person.id)[0].content == "不可就地编辑"
    assert c.transport.requests == []


# ----------------------------------------------------------------- 6. context

def test_memory_is_absent_from_the_default_context_and_opt_in_includes_it(memory_ui):
    c = memory_ui
    memory = c.memories.create(c.ws, PRIVATE_CONTENT, kind="FACT", person_id=c.person.id)
    request = AnalysisRequest(workspace_id=c.ws, target_type="PERSON", target_id=c.person.id,
                              analysis_type="PERSON_PROFILE", as_of=datetime.now(timezone.utc),
                              time_scope=ALL_TIME)
    default = c.services.analysis.build_snapshot(request)
    assert default.scope["included_memory_ids"] == []
    assert PRIVATE_CONTENT not in default.rendered_context
    assert "用户确认的长期记忆" not in default.rendered_context

    opted_in = c.services.analysis.build_snapshot(replace(request, include_memories=True))
    assert opted_in.scope["included_memory_ids"] == [str(memory.id)]
    assert PRIVATE_CONTENT in opted_in.rendered_context
    assert opted_in.estimated_tokens > default.estimated_tokens
    assert c.transport.requests == []


def test_the_analysis_options_default_to_excluding_memory(memory_ui):
    c = memory_ui
    open_person(c, c.person.id)
    c.memories.create(c.ws, PRIVATE_CONTENT, kind="FACT", person_id=c.person.id)
    c.window.navigation.setCurrentRow(1)
    c.qt_app.processEvents()
    options = c.people.analyze_person()                    # opens the options dialog only
    c.qt_app.processEvents()
    try:
        assert options.include_memories.isChecked() is False
        first = options.preview_context()
        assert PRIVATE_CONTENT not in first.rendered_context
        options.include_memories.setChecked(True)
        second = options.preview_context()
        assert PRIVATE_CONTENT in second.rendered_context
        assert options.build_request().include_memories is True
    finally:
        options.close()
        c.qt_app.processEvents()
    assert c.transport.requests == []
    assert c.errors == []


# ---------------------------------------------------------------- 7. log privacy

def test_memory_content_never_reaches_the_log(memory_ui, caplog):
    c = memory_ui
    run = run_analysis(c, "PERSON", c.person.id)
    open_person(c, c.person.id)
    panel = c.people.memory_panel
    with caplog.at_level(logging.DEBUG, logger="app"):
        show_run(c, run)
        dialog = c.page.save_memory()
        c.qt_app.processEvents()
        # The title is captured while the dialog is alive (it is deleted on close).
        title = dialog.windowTitle()
        dialog.content.setPlainText(PRIVATE_CONTENT)
        dialog.select_person(c.person.id)
        dialog.confirm()
        c.qt_app.processEvents()
        panel.refresh()
        panel.table.selectRow(0)
        c.qt_app.processEvents()
    assert dialog.created_memory is not None
    assert dialog.created_memory.content == PRIVATE_CONTENT
    assert panel.table.item(0, 0).text() == PRIVATE_CONTENT
    assert PRIVATE_CONTENT not in caplog.text
    assert PRIVATE_CONTENT not in title
    assert c.errors == []

