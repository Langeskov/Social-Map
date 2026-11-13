"""Phase 6 evidence contracts on real SQLite migrations.

PostgreSQL verification is DDL compilation plus a mock-engine run of the frozen
migration module only: no PostgreSQL server is started or claimed.
"""
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.storage.database import Base, create_sqlite_engine
from app.storage.migrations import alembic_config, current_heads
from app.storage.models import (
    EvidenceMediaModel, EvidenceModel, EventModel, EventParticipantModel, MediaModel,
    PersonAliasModel, PersonMediaModel, PersonModel, RelationshipHistoryModel,
    RelationshipModel, WorkspaceModel,
)

NOW = datetime(2026, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc)
KINDS = ("TEXT", "IMAGE", "FILE", "LINK", "MANUAL", "OTHER")
SOURCE_TYPES = ("CHAT", "EMAIL", "DOCUMENT", "IMAGE", "FILE", "WEB", "MANUAL", "OTHER")
PHASE5_TABLES = {"workspace", "person", "person_alias", "media", "person_media",
                 "relationship", "relationship_history", "event", "event_participant"}
EVIDENCE_TABLES = {"evidence", "evidence_media"}
# The eleven business fact tables: migration 0006 adds none and changes none.
ALL = PHASE5_TABLES | EVIDENCE_TABLES
# Phase 7 projection infrastructure, kept strictly apart from the fact tables:
# the mapped search_document table, the FTS5 virtual table and the five shadow
# tables SQLite creates for the FTS5 index.
PROJECTION_TABLES = {"search_document", "search_document_fts"}
FTS_SHADOW_TABLES = {"search_document_fts_data", "search_document_fts_idx",
                     "search_document_fts_content", "search_document_fts_docsize",
                     "search_document_fts_config"}
# Phase 8 analysis ledger: the run table plus four provenance tables. It is part of
# the complete head schema and never one of the eleven business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 memory table: user-confirmed facts, preferences and context. Like the
# analysis ledger it is part of the complete head schema and never a fact table.
MEMORY_TABLES = {"memory"}
HEAD_TABLES = (ALL | PROJECTION_TABLES | ANALYSIS_TABLES | MEMORY_TABLES | FTS_SHADOW_TABLES
               | {"alembic_version"})
HEAD = ("0008_memory",)
ROOT = Path(__file__).resolve().parents[2]


def migrate(engine, config, revision, *, down=False):
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        try:
            (command.downgrade if down else command.upgrade)(config, revision)
        finally:
            config.attributes.pop("connection", None)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path / "evidence migration 数据库")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        # Current head, so the metadata comparison below covers every later table.
        migrate(engine, migration, "head")
        yield engine, migration
    finally:
        engine.dispose()


def add_workspace(connection, identifier=None):
    identifier = identifier or uuid4()
    connection.execute(WorkspaceModel.__table__.insert().values(
        id=identifier, name=str(identifier), description="保留工作区",
        created_at=NOW, updated_at=NOW))
    return identifier


def add_person(connection, workspace_id, identifier=None, name="保留人物"):
    identifier = identifier or uuid4()
    connection.execute(PersonModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, display_name=name, notes="保留说明",
        created_at=NOW, updated_at=NOW))
    return identifier


def add_media(connection, workspace_id, sha256="a" * 64):
    identifier = uuid4()
    connection.execute(MediaModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, sha256=sha256, mime_type="image/png",
        size_bytes=42, width=2, height=3, storage_path=f"media/{identifier}.png",
        created_at=NOW, updated_at=NOW))
    return identifier


def add_event(connection, workspace_id, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, title="Event", event_type="Meeting",
                  record_kind="FACT", occurred_on=date(2026, 3, 4), started_at=None,
                  ended_at=None, created_at=NOW, updated_at=NOW)
    values.update(overrides)
    connection.execute(EventModel.__table__.insert().values(**values))
    return values["id"]


def add_evidence(connection, workspace_id, event_id, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, event_id=event_id, kind="TEXT",
                  title="保留证据", content_text="原始正文", annotation=None, source_type="MANUAL",
                  source_label=None, source_reference="原始引用", captured_at=None,
                  created_at=NOW, updated_at=NOW, deleted_at=None)
    values.update(overrides)
    connection.execute(EvidenceModel.__table__.insert().values(**values))
    return values["id"]


def add_binding(connection, workspace_id, evidence_id, media_id, role=None, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, evidence_id=evidence_id,
                  media_id=media_id, role=role, created_at=NOW)
    values.update(overrides)
    connection.execute(EvidenceMediaModel.__table__.insert().values(**values))
    return values["id"]


def seed_scope(connection):
    """One workspace with a usable event and media plus one unrelated workspace."""
    scope, other = add_workspace(connection), add_workspace(connection)
    return scope, other, add_event(connection, scope), add_media(connection, scope)


def seed_phase5(connection):
    """Rows in every pre-existing table, so a Phase 6 downgrade must not lose any of them."""
    scope, other = add_workspace(connection), add_workspace(connection)
    a, b = sorted((uuid4(), uuid4()))
    add_person(connection, scope, a, name="保留人物 A")
    add_person(connection, scope, b, name="保留人物 B")
    add_person(connection, other, name="其他工作区人物")
    media = add_media(connection, scope)
    connection.execute(PersonAliasModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, person_id=a, alias="保留别名", alias_type="NICKNAME",
        note="保留", created_at=NOW, updated_at=NOW))
    connection.execute(PersonMediaModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, person_id=a, media_id=media, role="AVATAR",
        sort_order=2, created_at=NOW))
    relationship = uuid4()
    connection.execute(RelationshipModel.__table__.insert().values(
        id=relationship, workspace_id=scope, person_a_id=a, person_b_id=b,
        relationship_type="Friend", directionality="UNDIRECTED", created_at=NOW, updated_at=NOW))
    connection.execute(RelationshipHistoryModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, relationship_id=relationship, state="ACTIVE",
        effective_at=NOW, created_at=NOW, note="保留历史"))
    event = add_event(connection, scope)
    connection.execute(EventParticipantModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, event_id=event, person_id=a, role="参与者",
        note="保留参与记录", created_at=NOW))
    return {"scope": scope, "other": other, "person_a": a, "person_b": b, "media": media,
            "relationship": relationship, "event": event}


def phase5_snapshot(engine):
    """Rows plus every named constraint and index of the nine pre-existing tables."""
    rows, schemas = {}, {}
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        for name in sorted(PHASE5_TABLES):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            rows[name] = connection.execute(sa.select(table).order_by(table.c.id)).all()
            schemas[name] = (
                [dict(column) for column in inspector.get_columns(name)],
                inspector.get_pk_constraint(name), inspector.get_check_constraints(name),
                inspector.get_unique_constraints(name), inspector.get_foreign_keys(name),
                inspector.get_indexes(name),
            )
    # Reflection allocates fresh type and SQL-expression objects on each call.
    # Normalize their representations while preserving every other property.
    for schema in schemas.values():
        for column in schema[0]:
            column["type"] = str(column["type"])
        for index in schema[5]:
            index["dialect_options"] = {
                key: str(value) if isinstance(value, sa.sql.elements.ClauseElement) else value
                for key, value in index.get("dialect_options", {}).items()
            }
    return rows, schemas


def expect_integrity(engine, code, callback):
    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            callback(connection)
    assert error.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_" + code


def test_empty_database_explicit_1_2_3_4_5_6_7(tmp_path, monkeypatch):
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        # 0001..0005 create business fact tables only; 0006 adds the projection
        # infrastructure, 0007 the analysis ledger and 0008 the memory table on top,
        # and all three still leave exactly eleven fact tables.
        for revision, fact_count, total_count in [
            ("0001_empty_initial", 0, 0), ("0002_workspace_person_media", 5, 5),
            ("0003_relationship", 7, 7), ("0004_event", 9, 9), ("0005_evidence", 11, 11),
            ("0006_search_projection", 11, 11 + len(PROJECTION_TABLES) + len(FTS_SHADOW_TABLES)),
            ("0007_analysis", 11, 11 + len(PROJECTION_TABLES) + len(FTS_SHADOW_TABLES)
             + len(ANALYSIS_TABLES)),
            ("0008_memory", 11, 11 + len(PROJECTION_TABLES) + len(FTS_SHADOW_TABLES)
             + len(ANALYSIS_TABLES) + len(MEMORY_TABLES)),
        ]:
            migrate(engine, migration, revision)
            assert current_heads(engine) == (revision,)
            names = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
            assert len(names) == total_count
            fact_names = names - PROJECTION_TABLES - FTS_SHADOW_TABLES - ANALYSIS_TABLES - MEMORY_TABLES
            assert len(fact_names) == fact_count
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                # No migration inserts illustrative business rows. The FTS5
                # shadow tables are excluded here: SQLite keeps its own index
                # bookkeeping rows in them (fts5 initializes its config table
                # even for an empty index), so they are not business data.
                for name in names - FTS_SHADOW_TABLES:
                    table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                    assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
            if revision == "0006_search_projection":
                # The projection rows are derived data and start empty; the FTS5
                # index holds no document either.
                with engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM search_document").scalar() == 0
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM search_document_fts").scalar() == 0
            if revision == "0007_analysis":
                # 0007 seeds nothing either: the ledger starts empty.
                with engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM analysis_run").scalar() == 0
                    for table in sorted(ANALYSIS_TABLES - {"analysis_run"}):
                        assert connection.exec_driver_sql(
                            f"SELECT count(*) FROM {table}").scalar() == 0
            if revision == "0008_memory":
                # 0008 seeds nothing either: the memory table starts empty.
                with engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM memory").scalar() == 0
        assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
        # 0008 -> 0006 removes the memory table and the analysis ledger in one step.
        migrate(engine, migration, "0006_search_projection", down=True)
        assert current_heads(engine) == ("0006_search_projection",)
        assert set(sa.inspect(engine).get_table_names()) == (
            ALL | PROJECTION_TABLES | FTS_SHADOW_TABLES | {"alembic_version"})
        assert {name for name in sa.inspect(engine).get_table_names()
                if "analysis" in name} == set()
        assert {name for name in sa.inspect(engine).get_table_names()
                if "memory" in name} == set()
        # 0006 -> 0005 removes the projection objects too.
        migrate(engine, migration, "0005_evidence", down=True)
        assert current_heads(engine) == ("0005_evidence",)
        assert set(sa.inspect(engine).get_table_names()) == ALL | {"alembic_version"}
        assert {name for name in sa.inspect(engine).get_table_names()
                if "search_document" in name} == set()
        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        assert set(sa.inspect(engine).get_table_names()) == (
            ALL | PROJECTION_TABLES | FTS_SHADOW_TABLES | {"alembic_version"})
        migrate(engine, migration, "0007_analysis")
        assert current_heads(engine) == ("0007_analysis",)
        assert set(sa.inspect(engine).get_table_names()) == (
            ALL | PROJECTION_TABLES | FTS_SHADOW_TABLES | ANALYSIS_TABLES | {"alembic_version"})
        migrate(engine, migration, "0008_memory")
        assert current_heads(engine) == HEAD
        assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
    finally:
        engine.dispose()


def test_seeded_0004_0005_0004_0005_retains_nine_tables_and_new_tables_stay_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path / "证据 migration 保留")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        migrate(engine, migration, "0004_event")
        with engine.begin() as connection:
            seeded = seed_phase5(connection)
        before = phase5_snapshot(engine)
        for revision, down in [("0005_evidence", False), ("0004_event", True), ("0005_evidence", False)]:
            migrate(engine, migration, revision, down=down)
            assert current_heads(engine) == (revision,)
            assert phase5_snapshot(engine) == before
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            if down:
                assert set(sa.inspect(engine).get_table_names()) == PHASE5_TABLES | {"alembic_version"}
                with engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name LIKE '%evidence%'").all() == []
                for name in EVIDENCE_TABLES:
                    with pytest.raises(sa.exc.NoSuchTableError):
                        sa.inspect(engine).get_indexes(name)
            else:
                assert set(sa.inspect(engine).get_table_names()) == ALL | {"alembic_version"}
                with engine.begin() as connection:
                    assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel)) == 0
                    assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 0
                    evidence = add_evidence(connection, seeded["scope"], seeded["event"])
                    add_binding(connection, seeded["scope"], evidence, seeded["media"], role="ATTACHMENT")
        with engine.begin() as connection:
            assert connection.scalar(sa.select(EvidenceModel.content_text)) == "原始正文"
            assert connection.scalar(sa.select(EvidenceMediaModel.role)) == "ATTACHMENT"
        assert current_heads(engine) == ("0005_evidence",)
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_evidence(c, uuid4(), seeded["event"]))
    finally:
        engine.dispose()


@pytest.mark.parametrize("overrides", [
    {"kind": ""}, {"kind": "text"}, {"kind": "TEXT "}, {"kind": "UNKNOWN"},
    {"source_type": ""}, {"source_type": "chat"}, {"source_type": "UNKNOWN"},
    {"title": ""}, {"title": " Evidence"}, {"title": "Evidence "}, {"title": " "},
    {"title": "x" * 201},
    {"source_label": ""}, {"source_label": "x" * 301},
])
def test_evidence_strict_checks(database, overrides):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, _ = seed_scope(connection)
    expect_integrity(engine, "CHECK", lambda c: add_evidence(c, scope, event_id, **overrides))


def test_evidence_accepts_declared_enumerations_and_boundary_lengths(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, _ = seed_scope(connection)
        identifiers = [add_evidence(connection, scope, event_id, kind=kind, source_type=source_type,
                                    title="x", source_label="y")
                       for kind, source_type in zip(KINDS, SOURCE_TYPES)]
        identifiers.append(add_evidence(connection, scope, event_id, source_label=None))
        identifiers.append(add_evidence(connection, scope, event_id, title="x" * 200,
                                        source_label="y" * 300, content_text="", annotation=None,
                                        captured_at=NOW, deleted_at=NOW))
        assert len(set(identifiers)) == len(KINDS) + 2
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel)) == len(identifiers)
        for kind, source_type in zip(KINDS, SOURCE_TYPES):
            assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel).where(
                EvidenceModel.kind == kind, EvidenceModel.source_type == source_type)) == 1


def test_evidence_workspace_event_scope_foreign_keys(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, other, event_id, _ = seed_scope(connection)
        foreign_event = add_event(connection, other)
        add_evidence(connection, scope, event_id)
        add_evidence(connection, other, foreign_event, kind="OTHER", source_type="OTHER")
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_evidence(c, scope, foreign_event))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_evidence(c, other, event_id))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_evidence(c, scope, uuid4()))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_evidence(c, uuid4(), event_id))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel)) == 2


def test_evidence_media_rejects_cross_workspace_and_missing_references(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, other, event_id, media = seed_scope(connection)
        foreign_event = add_event(connection, other)
        foreign_media = add_media(connection, other, sha256="b" * 64)
        evidence = add_evidence(connection, scope, event_id)
        foreign_evidence = add_evidence(connection, other, foreign_event)
        # A named role keeps these rejections distinct from the NULL-role unique index.
        add_binding(connection, scope, evidence, media, role="ATTACHMENT")
    # Both referenced IDs exist, but never inside the binding's own workspace.
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, scope, foreign_evidence, media))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, other, evidence, media))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, scope, evidence, foreign_media))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, other, evidence, foreign_media))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, scope, uuid4(), media))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_binding(c, scope, evidence, uuid4()))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 1


def test_evidence_media_unique_role_combination_rejects_duplicates(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, media = seed_scope(connection)
        second_media = add_media(connection, scope, sha256="b" * 64)
        evidence = add_evidence(connection, scope, event_id)
        add_binding(connection, scope, evidence, media, role="ATTACHMENT")
        # Same evidence/media pair with another role, and another media with the same role.
        add_binding(connection, scope, evidence, media, role="THUMBNAIL")
        add_binding(connection, scope, evidence, second_media, role="ATTACHMENT")
    expect_integrity(engine, "UNIQUE", lambda c: add_binding(c, scope, evidence, media, role="ATTACHMENT"))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 3


def test_evidence_media_null_role_duplicates_are_rejected_by_partial_unique_index(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, media = seed_scope(connection)
        second_media = add_media(connection, scope, sha256="b" * 64)
        evidence = add_evidence(connection, scope, event_id)
        # The plain UNIQUE(evidence_id, media_id, role) treats NULL roles as distinct.
        add_binding(connection, scope, evidence, media, role=None)
        add_binding(connection, scope, evidence, media, role="ATTACHMENT")
        add_binding(connection, scope, evidence, second_media, role=None)
    index = next(index for index in sa.inspect(engine).get_indexes("evidence_media")
                 if index["name"] == "uq_evidence_media_evidence_media_norole")
    assert index["unique"] in (True, 1)
    assert index["column_names"] == ["evidence_id", "media_id"]
    assert str(index["dialect_options"]["sqlite_where"]) == "role IS NULL"
    unique = next(constraint for constraint in sa.inspect(engine).get_unique_constraints("evidence_media")
                  if constraint["name"] == "uq_evidence_media_evidence_media_role")
    assert unique["column_names"] == ["evidence_id", "media_id", "role"]
    expect_integrity(engine, "UNIQUE", lambda c: add_binding(c, scope, evidence, media, role=None))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 3


def test_evidence_media_reloads_utc_timestamps_and_null_optional_columns(database):
    engine, _ = database
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    with engine.begin() as connection:
        scope, _, event_id, media = seed_scope(connection)
        identifier = add_evidence(connection, scope, event_id, captured_at=local, created_at=local,
                                  updated_at=local, deleted_at=local, content_text=None,
                                  annotation=None, source_reference=None, source_label=None)
        binding = add_binding(connection, scope, identifier, media, created_at=local)
    engine.dispose()
    with Session(engine) as session:
        row = session.get(EvidenceModel, identifier)
        assert row.captured_at == NOW and row.captured_at.tzinfo is timezone.utc
        for name in ("created_at", "updated_at", "deleted_at"):
            value = getattr(row, name)
            assert value == NOW and value.tzinfo is timezone.utc
        assert (row.content_text, row.annotation, row.source_reference, row.source_label) == (None,) * 4
        assert session.get(EvidenceMediaModel, binding).created_at == NOW


@pytest.mark.parametrize("field,value", [
    ("id", uuid4()), ("workspace_id", uuid4()), ("event_id", uuid4()),
    ("kind", "IMAGE"), ("source_type", "WEB"), ("source_reference", "改写引用"),
    ("content_text", "改写正文"), ("captured_at", NOW), ("created_at", NOW + timedelta(days=1)),
])
def test_orm_evidence_original_fields_are_immutable(database, field, value):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, _ = seed_scope(connection)
        identifier = add_evidence(connection, scope, event_id)
    with Session(engine) as session:
        row = session.get(EvidenceModel, identifier)
        original = getattr(row, field)
        setattr(row, field, value)
        with pytest.raises(DomainError, match="原始证据内容不可修改，请软删除后新建"):
            session.flush()
        session.rollback()
        assert getattr(session.get(EvidenceModel, identifier), field) == original


def test_orm_evidence_allows_title_annotation_and_soft_delete_only(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, _ = seed_scope(connection)
        identifier = add_evidence(connection, scope, event_id)
    with Session(engine) as session:
        row = session.get(EvidenceModel, identifier)
        row.title = "修订标题"
        row.annotation = "新增批注"
        row.updated_at = NOW + timedelta(days=1)
        row.deleted_at = NOW + timedelta(days=1)
        session.commit()
    with Session(engine) as session:
        row = session.get(EvidenceModel, identifier)
        assert row.title == "修订标题" and row.annotation == "新增批注"
        assert row.deleted_at == NOW + timedelta(days=1)
        assert row.content_text == "原始正文" and row.source_reference == "原始引用"
        assert row.kind == "TEXT" and row.source_type == "MANUAL"
        # Soft deletion never unlocks the captured original fields.
        row.content_text = "软删除后改写"
        with pytest.raises(DomainError, match="原始证据内容不可修改，请软删除后新建"):
            session.flush()
        session.rollback()
    with engine.connect() as connection:
        assert connection.scalar(sa.select(EvidenceModel.content_text)) == "原始正文"
        assert connection.scalar(sa.select(EvidenceModel.title)) == "修订标题"
        assert connection.scalar(sa.select(EvidenceModel.deleted_at)) == NOW + timedelta(days=1)


def test_orm_evidence_delete_is_rejected(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, _ = seed_scope(connection)
        identifier = add_evidence(connection, scope, event_id)
    with Session(engine) as session:
        row = session.get(EvidenceModel, identifier)
        session.delete(row)
        with pytest.raises(DomainError, match="原始证据内容不可修改，请软删除后新建"):
            session.flush()
        session.rollback()
        assert session.get(EvidenceModel, identifier) is not None
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel)) == 1


def test_orm_evidence_media_bindings_remain_mutable_and_deletable(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, event_id, media = seed_scope(connection)
        evidence = add_evidence(connection, scope, event_id)
        identifier = add_binding(connection, scope, evidence, media, role="ATTACHMENT")
    with Session(engine) as session:
        row = session.get(EvidenceMediaModel, identifier)
        row.role = "THUMBNAIL"
        session.commit()
        assert session.get(EvidenceMediaModel, identifier).role == "THUMBNAIL"
        session.delete(session.get(EvidenceMediaModel, identifier))
        session.commit()
        assert session.get(EvidenceMediaModel, identifier) is None
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 0


def test_head_metadata_matches_named_schema_and_alembic_check(database):
    engine, migration = database
    assert current_heads(engine) == HEAD
    inspector = sa.inspect(engine)
    # At the head the ORM maps the eleven fact tables, the search projection, the
    # analysis ledger and the memory table: 0006, 0007 and 0008 add no business fact.
    assert set(Base.metadata.tables) == ALL | {"search_document"} | ANALYSIS_TABLES | MEMORY_TABLES
    assert len(ALL) == 11
    assert ALL.isdisjoint(ANALYSIS_TABLES)
    assert ALL.isdisjoint(MEMORY_TABLES)
    assert "search_document_fts" not in Base.metadata.tables
    for name in ALL:
        table = Base.metadata.tables[name]
        assert {c["name"] for c in inspector.get_columns(name)} == set(table.c.keys())
        assert inspector.get_pk_constraint(name)["name"] == table.primary_key.name
        for kind, reflected in [(sa.CheckConstraint, inspector.get_check_constraints(name)),
                                (sa.ForeignKeyConstraint, inspector.get_foreign_keys(name)),
                                (sa.UniqueConstraint, inspector.get_unique_constraints(name))]:
            assert {c["name"] for c in reflected} == {c.name for c in table.constraints if isinstance(c, kind)}
        assert {i["name"] for i in inspector.get_indexes(name)} == {i.name for i in table.indexes}
    assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("evidence")} == {
        ("workspace_id", "event_id")}
    assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("evidence_media")} == {
        ("workspace_id", "evidence_id"), ("workspace_id", "media_id")}
    assert {tuple(i["column_names"]) for i in inspector.get_indexes("evidence")} == {
        ("workspace_id", "event_id"), ("workspace_id", "kind"),
        ("workspace_id", "deleted_at"), ("workspace_id", "created_at")}
    # The projection objects sit on top of the fact tables, never inside them.
    assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
    assert {name for name in sa.inspect(engine).get_table_names()
            if "search_document" in name} == PROJECTION_TABLES | FTS_SHADOW_TABLES
    assert ALL.isdisjoint(PROJECTION_TABLES)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        migration.attributes["connection"] = connection
        try:
            command.check(migration)
        finally:
            migration.attributes.pop("connection", None)


def test_postgresql_orm_and_frozen_migration_compile_only():
    evidence = Base.metadata.tables["evidence"]
    media = Base.metadata.tables["evidence_media"]
    for table in (evidence, media):
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    evidence_ddl = str(CreateTable(evidence).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_evidence PRIMARY KEY (id)" in evidence_ddl
    assert "CONSTRAINT fk_evidence_workspace_event FOREIGN KEY(workspace_id, event_id) " \
           "REFERENCES event (workspace_id, id)" in evidence_ddl
    assert "CONSTRAINT uq_evidence_workspace_id_id UNIQUE (workspace_id, id)" in evidence_ddl
    assert "CONSTRAINT ck_evidence_kind_valid CHECK (kind IN " \
           "('TEXT','IMAGE','FILE','LINK','MANUAL','OTHER'))" in evidence_ddl
    assert "CONSTRAINT ck_evidence_source_type_valid CHECK (source_type IN " \
           "('CHAT','EMAIL','DOCUMENT','IMAGE','FILE','WEB','MANUAL','OTHER'))" in evidence_ddl
    assert "CONSTRAINT ck_evidence_title_trimmed_length CHECK " \
           "(title = trim(title) AND length(title) BETWEEN 1 AND 200)" in evidence_ddl
    assert "CONSTRAINT ck_evidence_source_label_length CHECK " \
           "(source_label IS NULL OR length(source_label) BETWEEN 1 AND 300)" in evidence_ddl
    media_ddl = str(CreateTable(media).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_evidence_media PRIMARY KEY (id)" in media_ddl
    assert "CONSTRAINT fk_evidence_media_workspace_evidence FOREIGN KEY(workspace_id, evidence_id) " \
           "REFERENCES evidence (workspace_id, id)" in media_ddl
    assert "CONSTRAINT fk_evidence_media_workspace_media FOREIGN KEY(workspace_id, media_id) " \
           "REFERENCES media (workspace_id, id)" in media_ddl
    assert "CONSTRAINT uq_evidence_media_evidence_media_role UNIQUE (evidence_id, media_id, role)" in media_ddl
    partial = next(index for index in media.indexes
                   if index.name == "uq_evidence_media_evidence_media_norole")
    assert str(CreateIndex(partial).compile(dialect=postgresql.dialect())) == (
        "CREATE UNIQUE INDEX uq_evidence_media_evidence_media_norole ON evidence_media "
        "(evidence_id, media_id) WHERE role IS NULL")

    # The frozen migration module must not import application code or reuse UTCDateTime.
    path = ROOT / "alembic" / "versions" / "0005_evidence.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
                   for node in ast.walk(module))
    assert "UTCDateTime" not in source
    assert "create_all" not in source
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    assert namespace["revision"] == "0005_evidence"
    assert namespace["down_revision"] == "0004_event"
    statements = []
    engine = sa.create_mock_engine("postgresql://", lambda sql, *a, **k: statements.append(
        str(sql.compile(dialect=postgresql.dialect()))))
    with Operations.context(MigrationContext.configure(engine.connect())):
        namespace["upgrade"]()
        namespace["downgrade"]()
    assert sum("CREATE TABLE" in sql for sql in statements) == 2
    assert sum("DROP TABLE" in sql for sql in statements) == 2
    assert all("INSERT" not in sql for sql in statements)
    joined = "\n".join(statements)
    assert "TIMESTAMP WITH TIME ZONE" in joined
    assert "CREATE TABLE evidence " in joined and "CREATE TABLE evidence_media " in joined
    assert "DROP TABLE evidence_media" in joined and "DROP TABLE evidence" in joined
    assert "FOREIGN KEY(workspace_id, event_id) REFERENCES event (workspace_id, id)" in joined
    assert "FOREIGN KEY(workspace_id, evidence_id) REFERENCES evidence (workspace_id, id)" in joined
    assert "FOREIGN KEY(workspace_id, media_id) REFERENCES media (workspace_id, id)" in joined
    assert "UNIQUE (evidence_id, media_id, role)" in joined
    assert "UNIQUE INDEX uq_evidence_media_evidence_media_norole ON evidence_media " \
           "(evidence_id, media_id) WHERE role IS NULL" in joined
    assert "kind IN ('TEXT','IMAGE','FILE','LINK','MANUAL','OTHER')" in joined
