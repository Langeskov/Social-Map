"""Phase 14.3 Revision History & Diff Viewer (offscreen Qt, real services)."""
from datetime import date
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QPushButton

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.change_history import LONG_TEXT_LIMIT
from app.services.person import PersonService
from app.services.unit_of_work import transaction
from app.storage.models import ChangeLogModel
from app.ui.revision_history import (LongTextDialog, OperationDialog,
                                     RevisionHistoryDialog)
from app.ui.evidence_panel import EvidencePanel
from app.ui.memory_panel import MemoryPanel
from app.ui.relationship_panel import RelationshipPanel


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "history-ui")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    try:
        yield type("Env", (), {"config": config, "runtime": runtime,
                               "services": services, "ws": services.initial_workspace})()
    finally:
        runtime.close()


def make_person_with_two_versions(env, name="Alice"):
    person = env.services.people.create(env.ws.id, name, occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    return person


def dialog_for(env, entity_type, entity_id):
    return RevisionHistoryDialog(env.services, env.ws.id, entity_type, entity_id)


def button_texts(widget):
    return [button.text() for button in widget.findChildren(QPushButton)]


def test_dialog_lists_revisions_and_formatted_field_diff(env, qt_app):
    person = make_person_with_two_versions(env)
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 2
        assert "创建" in dialog.list.item(1).text()
        assert "更新" in dialog.list.item(0).text()
        dialog.list.setCurrentRow(0)
        qt_app.processEvents()
        diff = dialog.diff.toPlainText()
        assert "职业" in diff and "Engineer" in diff and "Designer" in diff
        assert '{"' not in diff and "before_snapshot" not in diff
        assert "恢复字段" not in button_texts(dialog)
        assert "撤销" not in "".join(button_texts(dialog))
        assert dialog.operation_button.isEnabled() is True
    finally:
        dialog.close()


def test_operation_group_dialog_lists_every_row(env, qt_app):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    dialog = dialog_for(env, "RELATIONSHIP", relationship.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 1
        dialog.list.setCurrentRow(0)
        operation_dialog = dialog._show_operation()
        qt_app.processEvents()
        assert isinstance(operation_dialog, OperationDialog)
        assert operation_dialog.list.count() == 2
        texts = [operation_dialog.list.item(i).text()
                 for i in range(operation_dialog.list.count())]
        assert any("关系" in text for text in texts)
        assert any("追加状态历史" in text for text in texts)
    finally:
        dialog.close()


def test_relationship_related_history_section(env, qt_app):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    env.services.relationships.add_history(env.ws.id, relationship.id, "PAUSED")
    dialog = dialog_for(env, "RELATIONSHIP", relationship.id)
    qt_app.processEvents()
    try:
        assert dialog.related_block.isVisibleTo(dialog) is True
        assert dialog.related_list.count() == 2
    finally:
        dialog.close()


def test_legacy_and_empty_history_are_explicit(env, qt_app):
    plain = PersonService(env.runtime.sessions, media_service=None)
    legacy = plain.create(env.ws.id, "Legacy")
    env.services.people.update(env.ws.id, legacy.id, occupation="Engineer")
    dialog = dialog_for(env, "PERSON", legacy.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 1
        assert "Phase 14 之前" in dialog.notice.text()
    finally:
        dialog.close()

    untracked = plain.create(env.ws.id, "Untracked")
    empty = dialog_for(env, "PERSON", untracked.id)
    qt_app.processEvents()
    try:
        assert empty.list.count() == 0
        assert "没有可追溯的历史" in empty.notice.text()
    finally:
        empty.close()


def test_unknown_snapshot_degrades_without_crash(env, qt_app):
    person = env.services.people.create(env.ws.id, "Alice")
    with transaction(env.runtime.sessions) as session:
        session.add(ChangeLogModel(
            id=uuid4(), workspace_id=env.ws.id, entity_type="PERSON",
            entity_id=person.id, action="UPDATE", operation_id=uuid4(),
            revision_no=2, changed_fields='["display_name"]',
            before_snapshot='{"display_name":"Old"}',
            after_snapshot='{"display_name":"New"}', payload_version="999",
            recorded_at=person.updated_at))
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 2
        dialog.list.setCurrentRow(0)
        qt_app.processEvents()
        assert "无法解析" in dialog.diff.toPlainText()
        assert dialog.full_text_button.isEnabled() is False
    finally:
        dialog.close()


def test_long_text_opens_a_read_only_full_view(env, qt_app):
    long_text = "长" * (LONG_TEXT_LIMIT + 40)
    person = env.services.people.create(env.ws.id, "Alice", notes="short")
    env.services.people.update(env.ws.id, person.id, notes=long_text)
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        dialog.list.setCurrentRow(0)
        qt_app.processEvents()
        assert dialog.full_text_button.isEnabled() is True
        full = dialog._show_full_text()
        qt_app.processEvents()
        assert isinstance(full, LongTextDialog)
        assert long_text in full.text.toPlainText()
    finally:
        dialog.close()


def test_archived_person_history_remains_readable(env, qt_app):
    person = make_person_with_two_versions(env)
    env.services.people.soft_delete(env.ws.id, person.id)
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 3
        assert "归档" in dialog.list.item(0).text()
    finally:
        dialog.close()


def test_panel_entry_points_expose_read_only_history(env, qt_app):
    assert hasattr(EvidencePanel(env.services), "history_button")
    relationship_panel = RelationshipPanel(env.services)
    assert hasattr(relationship_panel, "change_history_button")
    memory_panel = MemoryPanel(env.services)
    assert hasattr(memory_panel, "history_button")
    for panel in (relationship_panel, memory_panel):
        panel.deleteLater()
    qt_app.processEvents()


def test_revert_button_is_enabled_only_for_a_valid_target(env, qt_app):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        dialog.list.setCurrentRow(0)     # newest UPDATE: target equals current
        qt_app.processEvents()
        assert dialog.revert_button.isEnabled() is False
        dialog.list.setCurrentRow(1)     # CREATE revision: revert to Engineer
        qt_app.processEvents()
        assert dialog.revert_button.isEnabled() is True
    finally:
        dialog.close()

    alice = env.services.people.create(env.ws.id, "A")
    bob = env.services.people.create(env.ws.id, "B")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id, "COLLEAGUE")
    relationship_dialog = dialog_for(env, "RELATIONSHIP", relationship.id)
    qt_app.processEvents()
    try:
        relationship_dialog.list.setCurrentRow(0)
        qt_app.processEvents()
        assert relationship_dialog.revert_button.isEnabled() is False
        assert "不支持" in relationship_dialog.revert_button.toolTip()
    finally:
        relationship_dialog.close()


def test_archived_person_blocks_revert(env, qt_app):
    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    env.services.people.soft_delete(env.ws.id, person.id)
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        dialog.list.setCurrentRow(2)     # CREATE row (ARCHIVE/UPDATE are newer)
        qt_app.processEvents()
        assert dialog.revert_button.isEnabled() is False
        assert "归档" in dialog.revert_button.toolTip()
    finally:
        dialog.close()


def test_revert_executes_as_a_new_revision(env, qt_app, monkeypatch):
    import app.ui.revision_history as module
    from PySide6.QtWidgets import QDialog

    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        dialog.list.setCurrentRow(1)     # revert to the CREATE snapshot
        qt_app.processEvents()
        monkeypatch.setattr(module, "RevertPreviewDialog",
                            lambda preview, parent=None: type(
                                "FakeDialog", (), {"exec": lambda self: QDialog.DialogCode.Accepted})())
        result = dialog._start_revert()
        qt_app.processEvents()
        assert result is not None and result.applied is True
        assert env.services.people.get(env.ws.id, person.id).occupation == "Engineer"
        assert dialog.list.count() == 3
        assert "恢复字段" in dialog.list.item(0).text()
        assert "已恢复" in dialog.notice.text()
    finally:
        dialog.close()


def test_confirm_dialog_lists_exact_field_changes(env, qt_app):
    from app.ui.revision_history import RevertPreviewDialog

    person = env.services.people.create(env.ws.id, "Alice", occupation="Engineer")
    env.services.people.update(env.ws.id, person.id, occupation="Designer")
    preview = env.services.change_revert.preview(env.ws.id, "PERSON", person.id, 1)
    dialog = RevertPreviewDialog(preview)
    qt_app.processEvents()
    try:
        assert dialog.fields.count() == 1
        text = dialog.fields.item(0).text()
        assert "职业" in text and "Designer" in text and "Engineer" in text
    finally:
        dialog.close()


def test_people_page_history_button_opens_the_dialog(env, qt_app):
    from app.ui.main_window import MainWindow

    window = MainWindow(env.config, env.services)
    window.show()
    window.navigation.setCurrentRow(1)
    qt_app.processEvents()
    try:
        page = window.pages.widget(1)
        person = env.services.people.create(page.workspace_id, "Alice")
        env.services.people.update(page.workspace_id, person.id, occupation="Engineer")
        page.refresh_people(person.id)
        qt_app.processEvents()
        assert page.current_detail is not None
        assert page.history_button.isEnabled() is True
        page.show_person_history()
        qt_app.processEvents()
        dialogs = getattr(page, "_revision_history_dialogs", [])
        assert len(dialogs) == 1
        assert dialogs[0].list.count() == 2
    finally:
        for dialog in getattr(page, "_revision_history_dialogs", []):
            dialog.close()
        window.close()
        qt_app.processEvents()


def test_event_detail_dialog_exposes_history_button(env, qt_app):
    from app.ui.event_detail import EventDetailDialog

    person = env.services.people.create(env.ws.id, "Alice")
    event = env.services.events.create_event(env.ws.id, "Retro", "MEETING",
                                             occurred_on=date(2026, 11, 20),
                                             participants=(person.id,))
    detail = env.services.events.detail(env.ws.id, event.id)
    dialog = EventDetailDialog(detail, services=env.services)
    qt_app.processEvents()
    try:
        assert dialog.history_button is not None
        opened = dialog.show_history()
        qt_app.processEvents()
        assert opened is not None and opened.list.count() == 1
    finally:
        window = dialog
        window.close()
        qt_app.processEvents()


PERSON_SNAPSHOT = {
    "display_name": "Alice", "real_name": None, "gender": None, "birthday": None,
    "occupation": None, "organization": None, "location": None, "notes": None,
    "aliases": [], "images": [],
}


def _insert_history_rows(env, *, entity_type, entity_id, count, parent=None,
                         action="UPDATE", start_revision=2):
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from app.storage.models import ChangeLogModel

    base = datetime(2026, 11, 20, tzinfo=timezone.utc)
    with transaction(env.runtime.sessions) as session:
        for index in range(count):
            after = dict(PERSON_SNAPSHOT, display_name=f"Alice {index}")
            session.add(ChangeLogModel(
                id=uuid4(), workspace_id=env.ws.id, entity_type=entity_type,
                entity_id=entity_id,
                parent_entity_type=None if parent is None else "RELATIONSHIP",
                parent_entity_id=parent,
                action=action, operation_id=uuid4(),
                revision_no=start_revision + index,
                changed_fields='["display_name"]' if action == "UPDATE" else "[]",
                before_snapshot=None if action == "APPEND_HISTORY" else
                json.dumps(PERSON_SNAPSHOT),
                after_snapshot=json.dumps(after),
                payload_version="1", recorded_at=base + timedelta(seconds=index)))


def _revision_numbers(widget):
    import re
    return [int(re.search(r"#(\d+)", widget.item(i).text()).group(1))
            for i in range(widget.count())]


def test_history_pagination_loads_more_without_duplicates(env, qt_app):
    from app.storage.models import ChangeLogModel
    from app.services.unit_of_work import transaction

    person = env.services.people.create(env.ws.id, "Alice")
    _insert_history_rows(env, entity_type="PERSON", entity_id=person.id, count=55)
    dialog = dialog_for(env, "PERSON", person.id)
    qt_app.processEvents()
    try:
        assert dialog.list.count() == 50          # newest-first first page
        assert _revision_numbers(dialog.list)[0] == 56
        dialog._load_more()
        qt_app.processEvents()
        numbers = _revision_numbers(dialog.list)
        assert dialog.list.count() == 56
        assert len(numbers) == len(set(numbers))   # no duplicated page
        assert sorted(numbers) == list(range(1, 57))
        assert numbers == sorted(numbers, reverse=True)
    finally:
        dialog.close()


def test_relationship_related_pagination_loads_more_without_duplicates(env, qt_app):
    from app.storage.models import ChangeLogModel
    from app.services.unit_of_work import transaction
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id,
                                                     "COLLEAGUE")
    base = datetime(2026, 11, 20, tzinfo=timezone.utc)
    with transaction(env.runtime.sessions) as session:
        for index in range(55):
            session.add(ChangeLogModel(
                id=uuid4(), workspace_id=env.ws.id,
                entity_type="RELATIONSHIP_HISTORY", entity_id=uuid4(),
                parent_entity_type="RELATIONSHIP", parent_entity_id=relationship.id,
                action="APPEND_HISTORY", operation_id=uuid4(), revision_no=1,
                changed_fields='["event_id","note","relationship_id","state"]',
                before_snapshot=None,
                after_snapshot=json.dumps({"event_id": None, "note": None,
                                           "relationship_id": str(relationship.id),
                                           "state": "ACTIVE"}),
                payload_version="1", recorded_at=base + timedelta(seconds=index)))
    dialog = dialog_for(env, "RELATIONSHIP", relationship.id)
    qt_app.processEvents()
    try:
        assert dialog.related_list.count() == 50
        dialog._load_more_related()
        qt_app.processEvents()
        # 55 injected + the initial APPEND_HISTORY; a repeated page would
        # produce ~100 rows, so count and text uniqueness catch the bug.
        assert dialog.related_list.count() == 56
        texts = [dialog.related_list.item(i).text()
                 for i in range(dialog.related_list.count())]
        assert len(texts) == len(set(texts))
    finally:
        dialog.close()
