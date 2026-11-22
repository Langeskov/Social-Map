"""Phase 14.4 integrity: ChangeLog references, snapshots, revisions, operations.

The audit is read-only. Broken rows are injected directly (bypassing the writer) to
prove the compensating control sees them; nothing is repaired.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.storage.models import ChangeLogModel, PersonModel

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "integrity-changelog")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    try:
        yield type("Env", (), {"config": config, "runtime": runtime,
                               "services": services, "ws": services.initial_workspace})()
    finally:
        runtime.close()


def insert_row(runtime, **overrides):
    values = dict(id=uuid4(), workspace_id=None, entity_type="PERSON",
                  entity_id=uuid4(), action="UPDATE", operation_id=uuid4(),
                  revision_no=2, changed_fields='["display_name"]',
                  before_snapshot='{"display_name":"Old"}',
                  after_snapshot='{"display_name":"New"}', payload_version="1",
                  recorded_at=NOW)
    values.update(overrides)
    with transaction(runtime.sessions) as session:
        session.add(ChangeLogModel(**values))


def codes(report):
    return {issue.code for issue in report.errors}


def change_codes(env, workspace_id=None):
    report = env.services.integrity.check(workspace_id, verify_media_files=False)
    return {code for code in codes(report) if code.startswith("CHANGE_LOG_")}


def test_clean_audit_reports_no_change_log_problems(env):
    person = env.services.people.create(env.ws.id, "Alice")
    env.services.people.update(env.ws.id, person.id, occupation="Engineer")
    report = env.services.integrity.check(env.ws.id, verify_media_files=False)
    assert report.ok is True
    assert not {code for code in codes(report) if code.startswith("CHANGE_LOG_")}
    assert report.counts["table_change_log"] == 2
    assert report.counts["change_log_rows_checked"] == 2


def test_orphan_entity_is_reported(env):
    person = env.services.people.create(env.ws.id, "Alice")
    insert_row(env.runtime, workspace_id=env.ws.id, entity_id=uuid4(), revision_no=2)
    assert "CHANGE_LOG_ORPHAN_ENTITY" in change_codes(env, env.ws.id)
    assert person is not None


def test_workspace_mismatch_is_reported(env):
    other = env.services.workspaces.create("Other")
    foreign_person = env.services.people.create(other.id, "Foreign")
    insert_row(env.runtime, workspace_id=env.ws.id, entity_id=foreign_person.id,
               revision_no=2)
    assert "CHANGE_LOG_WORKSPACE_MISMATCH" in change_codes(env, env.ws.id)


def test_revision_gap_and_snapshot_errors_are_reported(env):
    person = env.services.people.create(env.ws.id, "Alice")
    insert_row(env.runtime, workspace_id=env.ws.id, entity_id=person.id,
               revision_no=3)
    insert_row(env.runtime, workspace_id=env.ws.id, entity_id=person.id,
               revision_no=4, payload_version="999")
    found = change_codes(env, env.ws.id)
    assert "CHANGE_LOG_REVISION_GAP" in found
    assert "CHANGE_LOG_SNAPSHOT_INVALID" in found


def test_invalid_parent_is_reported(env):
    alice = env.services.people.create(env.ws.id, "Alice")
    bob = env.services.people.create(env.ws.id, "Bob")
    relationship = env.services.relationships.create(env.ws.id, alice.id, bob.id,
                                                     "COLLEAGUE")
    history = env.services.relationships.history(env.ws.id, relationship.id)[0]
    insert_row(env.runtime, workspace_id=env.ws.id,
               entity_type="RELATIONSHIP_HISTORY", entity_id=history.id,
               action="APPEND_HISTORY", revision_no=2, changed_fields="[]",
               before_snapshot=None, after_snapshot='{"relationship_id": null}',
               parent_entity_type="RELATIONSHIP", parent_entity_id=uuid4())
    assert "CHANGE_LOG_PARENT_INVALID" in change_codes(env, env.ws.id)


def test_cross_workspace_operation_is_reported(env):
    other = env.services.workspaces.create("Other")
    first = env.services.people.create(env.ws.id, "First")
    second = env.services.people.create(other.id, "Second")
    operation = uuid4()
    insert_row(env.runtime, workspace_id=env.ws.id, entity_id=first.id,
               revision_no=2, operation_id=operation)
    insert_row(env.runtime, workspace_id=other.id, entity_id=second.id,
               revision_no=2, operation_id=operation)
    found = change_codes(env)
    assert "CHANGE_LOG_OPERATION_INCONSISTENT" in found


def test_audit_workspace_filter_limits_change_log_checks(env):
    other = env.services.workspaces.create("Other")
    person = env.services.people.create(env.ws.id, "Alice")
    insert_row(env.runtime, workspace_id=other.id, entity_id=uuid4(), revision_no=2)
    assert "CHANGE_LOG_ORPHAN_ENTITY" not in change_codes(env, env.ws.id)
    assert "CHANGE_LOG_ORPHAN_ENTITY" in change_codes(env, other.id)
    assert person is not None
