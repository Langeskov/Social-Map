"""Scoped Evidence adapter behaviour against a real migrated database.

Database-level constraint coverage (CHECK/FK/UNIQUE/partial unique index and the
ORM guards) lives in tests/migrations/test_evidence_schema.py, which exercises the
same real schema. This module keeps only what the adapter itself is responsible
for: mapping, scoping, metadata-only updates, caller-owned transactions and the
combined media reference check.
"""
import ast
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy.exc import StatementError
from app.config.settings import AppConfig
from app.domain.entities import Event, Evidence, EvidenceMedia, Media, Workspace
from app.domain.errors import DomainError, NotFoundError
from app.services.database import initialize_database
from app.services.evidence_repositories import sql_evidence_repositories
from app.services.unit_of_work import transaction
from app.storage.models import EvidenceModel

NOW = datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc)


def evidence_row(workspace_id, event_id, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, event_id=event_id, kind="TEXT", title="evidence",
                  content_text="原文", annotation=None, source_type="CHAT", source_label=None,
                  source_reference=None, captured_at=None, created_at=NOW, updated_at=NOW, deleted_at=None)
    values.update(overrides)
    return values


@pytest.fixture
def env(tmp_path):
    runtime = initialize_database(AppConfig(data_dir=tmp_path / "evidence-repository"))
    try:
        with transaction(runtime.sessions, write=True) as session:
            repositories = sql_evidence_repositories(session)
            workspace = repositories.workspaces.save(Workspace(name="evidence ws"))
            foreign = repositories.workspaces.save(Workspace(name="foreign ws"))
            event = repositories.events.save(Event(workspace_id=workspace.id, title="event",
                                                   event_type="Meeting", occurred_on=date(2026, 10, 1)))
            foreign_event = repositories.events.save(Event(workspace_id=foreign.id, title="foreign",
                                                           event_type="Meeting", occurred_on=date(2026, 10, 1)))
            media = repositories.media.save(Media(workspace_id=workspace.id, sha256="a" * 64,
                                                  mime_type="image/png", size_bytes=10, width=2, height=3,
                                                  storage_path="media/one.png", created_at=NOW, updated_at=NOW))
            foreign_media = repositories.media.save(Media(workspace_id=foreign.id, sha256="b" * 64,
                                                          mime_type="image/png", size_bytes=10, width=2, height=3,
                                                          storage_path="media/two.png", created_at=NOW, updated_at=NOW))
        yield SimpleNamespace(runtime=runtime, ws=workspace.id, foreign=foreign.id, event=event,
                              foreign_event=foreign_event, media=media, foreign_media=foreign_media)
    finally:
        runtime.close()


def test_crud_scoped_lists_and_detached_snapshots(env):
    with transaction(env.runtime.sessions, write=True) as session:
        repositories = sql_evidence_repositories(session)
        entity = repositories.evidence.save(Evidence(workspace_id=env.ws, event_id=env.event.id,
                                                     kind="TEXT", title="evidence", content_text="原文", source_type="CHAT"))
        link = repositories.evidence_media.add(EvidenceMedia(workspace_id=env.ws, evidence_id=entity.id,
                                                            media_id=env.media.id, role="截图", created_at=NOW))
        assert repositories.evidence.get(env.foreign, entity.id) is None
        assert repositories.evidence.get(env.ws, entity.id, include_deleted=True) == entity
        assert repositories.evidence.list_by_event(env.ws, env.event.id) == [entity]
        assert repositories.evidence.list_by_event(env.ws, env.event.id, kind="IMAGE") == []
        assert repositories.evidence.list_by_workspace(env.foreign) == []
        assert repositories.evidence_media.list_by_evidence(env.ws, entity.id) == [link]
        assert repositories.evidence_media.list_by_media(env.ws, env.media.id) == [link]
        assert repositories.evidence_media.list_by_evidence(env.foreign, entity.id) == []
        revised = repositories.evidence.update_metadata(replace(entity, title="改名", annotation="说明", updated_at=NOW))
        assert revised.title == "改名" and revised.annotation == "说明"
        assert revised.content_text == entity.content_text and revised.kind == entity.kind
        deleted = repositories.evidence.soft_delete(env.ws, entity.id, NOW)
        assert repositories.evidence.get(env.ws, entity.id) is None
        assert repositories.evidence.get(env.ws, entity.id, include_deleted=True) == deleted
        assert repositories.evidence_media.list_by_evidence(env.ws, entity.id) == [link]
        assert repositories.evidence.restore(env.ws, entity.id, NOW).deleted_at is None
        assert repositories.evidence_media.remove(env.foreign, entity.id, env.media.id) == 0
        assert repositories.evidence_media.remove(env.ws, entity.id, env.media.id) == 1
        assert repositories.evidence_media.list_by_evidence(env.ws, entity.id) == []
    assert revised.title == "改名" and link.role == "截图"


def test_caller_owned_transactions_roll_back(env):
    with pytest.raises(RuntimeError):
        with transaction(env.runtime.sessions, write=True) as session:
            repositories = sql_evidence_repositories(session)
            entity = repositories.evidence.save(Evidence(workspace_id=env.ws, event_id=env.event.id,
                                                         kind="TEXT", title="evidence", content_text="原文"))
            repositories.evidence_media.add(EvidenceMedia(workspace_id=env.ws, evidence_id=entity.id,
                                                         media_id=env.media.id, created_at=NOW))
            raise RuntimeError("synthetic failure after flush")
    with transaction(env.runtime.sessions) as session:
        assert sql_evidence_repositories(session).evidence.list_by_workspace(env.ws, include_deleted=True) == []


def test_metadata_update_never_touches_original_material(env):
    with transaction(env.runtime.sessions, write=True) as session:
        repositories = sql_evidence_repositories(session)
        entity = repositories.evidence.save(Evidence(workspace_id=env.ws, event_id=env.event.id, kind="MANUAL",
                                                     title="evidence", content_text="原文", source_type="CHAT",
                                                     source_reference="原始引用", captured_at=NOW))
        revised = repositories.evidence.update_metadata(
            replace(entity, title="新标题", annotation="新注释", updated_at=NOW))
        assert (revised.content_text, revised.kind, revised.source_type) == ("原文", "MANUAL", "CHAT")
        assert (revised.source_reference, revised.captured_at) == ("原始引用", NOW)
        assert (revised.event_id, revised.workspace_id, revised.created_at) == (
            entity.event_id, entity.workspace_id, entity.created_at)


def test_naive_or_non_datetime_capture_is_rejected_before_sql(env):
    row = evidence_row(env.ws, env.event.id, captured_at="not-a-datetime")
    with pytest.raises(StatementError):
        with env.runtime.sessions() as session, session.begin():
            session.add(EvidenceModel(**row))
            session.flush()
    naive = evidence_row(env.ws, env.event.id, captured_at=datetime(2026, 10, 4, 9, 30))
    with pytest.raises(StatementError):
        with env.runtime.sessions() as session, session.begin():
            session.add(EvidenceModel(**naive))
            session.flush()


def test_save_never_overwrites_existing_evidence(env):
    with transaction(env.runtime.sessions, write=True) as session:
        repositories = sql_evidence_repositories(session)
        entity = repositories.evidence.save(Evidence(workspace_id=env.ws, event_id=env.event.id,
                                                     kind="TEXT", title="evidence", content_text="原文"))
        with pytest.raises(DomainError):
            repositories.evidence.save(replace(entity, content_text="改写"))
        with pytest.raises(NotFoundError):
            repositories.evidence.update_metadata(replace(entity, id=uuid4()))


def test_media_reference_check_covers_evidence_bindings(env):
    with transaction(env.runtime.sessions, write=True) as session:
        repositories = sql_evidence_repositories(session)
        assert repositories.media.has_references(env.ws, env.media.id) is False
        entity = repositories.evidence.save(Evidence(workspace_id=env.ws, event_id=env.event.id,
                                                     kind="IMAGE", title="photo", source_type="IMAGE"))
        repositories.evidence_media.add(EvidenceMedia(workspace_id=env.ws, evidence_id=entity.id,
                                                     media_id=env.media.id, created_at=NOW))
        assert repositories.media.has_references(env.ws, env.media.id) is True
        assert repositories.media.has_references(env.foreign, env.media.id) is False
        # Phase 7 rule: soft deleting the Evidence keeps its binding, so the media
        # is still referenced; only removing the binding releases it.
        repositories.evidence.soft_delete(env.ws, entity.id, NOW)
        assert repositories.media.has_references(env.ws, env.media.id) is True
        assert repositories.media.has_references(env.ws, env.media.id, active_only=False) is True
        repositories.evidence_media.remove(env.ws, entity.id, env.media.id)
        assert repositories.media.has_references(env.ws, env.media.id) is False


def test_adapters_never_own_session_engine_or_transaction():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "app/storage/evidence_repositories.py").read_text(encoding="utf-8"))
    forbidden = {"commit", "rollback", "create_engine", "create_session_factory", "sessionmaker"}
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Attribute) and node.func.attr in forbidden or
        isinstance(node.func, ast.Name) and node.func.id in forbidden)]
