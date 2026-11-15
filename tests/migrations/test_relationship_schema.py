"""Phase 4 schema contracts exercised against actual, temporary Alembic databases."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4
import ast

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
    MediaModel, PersonAliasModel, PersonMediaModel, PersonModel,
    RelationshipHistoryModel, RelationshipModel, WorkspaceModel,
)

OLD_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
ALL_TABLES = OLD_TABLES | {"relationship", "relationship_history"}
# Phase 7 adds projection infrastructure on top of the business fact tables:
# search_document plus the FTS5 virtual table and the five shadow tables SQLite
# creates for it. The metadata set is the fact tables plus the mapped projection.
FACT_TABLES = ALL_TABLES | {"event", "event_participant", "evidence", "evidence_media"}
PROJECTION_TABLES = {"search_document", "search_document_fts"}
FTS_SHADOW_TABLES = {"search_document_fts_data", "search_document_fts_idx",
                     "search_document_fts_content", "search_document_fts_docsize",
                     "search_document_fts_config"}
# Phase 8 adds the analysis ledger: one run table plus four provenance tables. It is
# part of the complete head schema and never one of the eleven business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 adds the memory table: user-confirmed facts, preferences and context. Also
# part of the complete head schema and never a business fact table.
MEMORY_TABLES = {"memory"}
HEAD_TABLES = (FACT_TABLES | PROJECTION_TABLES | ANALYSIS_TABLES | MEMORY_TABLES
               | FTS_SHADOW_TABLES | {"alembic_version"})
NOW = datetime(2026, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def migrate(engine, migration, revision, *, downgrade=False):
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            (command.downgrade if downgrade else command.upgrade)(migration, revision)
        finally:
            migration.attributes.pop("connection", None)


@pytest.fixture
def database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Schema must be created by migrations, not create_all")
    monkeypatch.setattr(Base.metadata, "create_all", forbidden)
    config = AppConfig(data_dir=tmp_path / "关系 migration database")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        migrate(engine, migration, "0003_relationship")
        assert set(sa.inspect(engine).get_table_names()) == ALL_TABLES | {"alembic_version"}
        with engine.connect() as connection:
            # Migrations must never insert illustrative business rows.
            for name in sorted(ALL_TABLES):
                table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        yield engine, migration
    finally:
        engine.dispose()


def workspace(connection, name=None):
    identifier = uuid4()
    connection.execute(WorkspaceModel.__table__.insert().values(
        id=identifier, name=name or str(identifier), created_at=NOW, updated_at=NOW))
    return identifier


def person(connection, scope, identifier=None):
    identifier = identifier or uuid4()
    connection.execute(PersonModel.__table__.insert().values(
        id=identifier, workspace_id=scope, display_name="sentinel person",
        notes="原始数据", created_at=NOW, updated_at=NOW))
    return identifier


def relationship(connection, scope, a, b, **overrides):
    values = dict(id=uuid4(), workspace_id=scope, person_a_id=a, person_b_id=b,
                  relationship_type="Friend", directionality="UNDIRECTED",
                  created_at=NOW, updated_at=NOW, deleted_at=None)
    values.update(overrides)
    connection.execute(RelationshipModel.__table__.insert().values(**values))
    return values["id"]


def history(connection, scope, rel, **overrides):
    values = dict(id=uuid4(), workspace_id=scope, relationship_id=rel,
                  state="自定义状态", effective_at=NOW, created_at=NOW, note=None)
    values.update(overrides)
    connection.execute(RelationshipHistoryModel.__table__.insert().values(**values))
    return values["id"]


def seeded(engine):
    with engine.begin() as connection:
        scope = workspace(connection)
        other_scope = workspace(connection)
        a, b = sorted((person(connection, scope), person(connection, scope)))
        foreign = person(connection, other_scope)
    return scope, other_scope, a, b, foreign


def expect_integrity(engine, code, callback):
    with pytest.raises(IntegrityError) as failure:
        with engine.begin() as connection:
            callback(connection)
    assert failure.value.orig.sqlite_errorname == code


def old_schema_snapshot(engine):
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        ddl = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name IN ('workspace','person','person_alias','media','person_media') "
            "ORDER BY type, name").all()
        rows = {name: connection.execute(sa.select(Base.metadata.tables[name])).all()
                for name in sorted(OLD_TABLES)}
    return ddl, rows, {name: inspector.get_foreign_keys(name) for name in OLD_TABLES}


def test_full_migration_cycle_preserves_old_schema_rows_and_foreign_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path / "cycle")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        migrate(engine, migration, "0001_empty_initial")
        assert sa.inspect(engine).get_table_names() == ["alembic_version"]
        migrate(engine, migration, "0002_workspace_person_media")
        assert set(sa.inspect(engine).get_table_names()) == OLD_TABLES | {"alembic_version"}
        with engine.begin() as connection:
            scope, foreign_scope = workspace(connection), workspace(connection)
            p, foreign_p = person(connection, scope), person(connection, foreign_scope)
            m, alias, link = uuid4(), uuid4(), uuid4()
            connection.execute(MediaModel.__table__.insert().values(
                id=m, workspace_id=scope, sha256="a" * 64, mime_type="image/png",
                size_bytes=7, width=1, height=1, storage_path="media/sentinel.png",
                created_at=NOW, updated_at=NOW))
            connection.execute(PersonAliasModel.__table__.insert().values(
                id=alias, workspace_id=scope, person_id=p, alias="sentinel alias",
                alias_type="NICKNAME", note="保留", created_at=NOW, updated_at=NOW))
            connection.execute(PersonMediaModel.__table__.insert().values(
                id=link, workspace_id=scope, person_id=p, media_id=m,
                role="AVATAR", sort_order=2, created_at=NOW))
        before = old_schema_snapshot(engine)
        for revision, down, tables in [
            ("0003_relationship", False, ALL_TABLES),
            ("0002_workspace_person_media", True, OLD_TABLES),
            ("0003_relationship", False, ALL_TABLES),
        ]:
            migrate(engine, migration, revision, downgrade=down)
            assert current_heads(engine) == (revision,)
            assert set(sa.inspect(engine).get_table_names()) == tables | {"alembic_version"}
            assert old_schema_snapshot(engine) == before
            if revision == "0003_relationship":
                with engine.begin() as connection:
                    assert connection.scalar(sa.select(sa.func.count()).select_from(RelationshipModel)) == 0
                    assert connection.scalar(sa.select(sa.func.count()).select_from(RelationshipHistoryModel)) == 0
                    second_person = person(connection, scope)
                    rel = relationship(connection, scope, p, second_person, directionality="DIRECTED")
                    history(connection, scope, rel)
                # Additional person is a controlled sentinel carried through the next transitions.
                before = old_schema_snapshot(engine)
            expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
                c.execute(PersonMediaModel.__table__.insert().values(
                    id=uuid4(), workspace_id=scope, person_id=foreign_p,
                    media_id=m, role="GALLERY", sort_order=0, created_at=NOW)))
            expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
                c.execute(PersonAliasModel.__table__.insert().values(
                    id=uuid4(), workspace_id=scope, person_id=foreign_p, alias="bad",
                    alias_type="OTHER", created_at=NOW, updated_at=NOW)))
            expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
                c.execute(PersonMediaModel.__table__.insert().values(
                    id=uuid4(), workspace_id=foreign_scope, person_id=foreign_p,
                    media_id=m, role="GALLERY", sort_order=0, created_at=NOW)))
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("endpoint", ["person_a_id", "person_b_id"])
def test_both_relationship_endpoints_are_scope_foreign_keys(database, endpoint):
    engine, _ = database
    scope, _, a, b, foreign = seeded(engine)
    with engine.begin() as connection:
        relationship(connection, scope, a, b)
    expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
        relationship(c, scope, a, b, directionality="DIRECTED", **{endpoint: foreign}))


def test_relationship_workspace_foreign_key_is_explicit(database):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
        relationship(c, uuid4(), a, b))
    names = {fk["name"] for fk in sa.inspect(engine).get_foreign_keys("relationship")}
    assert "fk_relationship_workspace_id_workspace" in names


@pytest.mark.parametrize("overrides", [
    {"same": True}, {"reverse": True}, {"directionality": "undirected"},
    {"directionality": "UNKNOWN"}, {"relationship_type": ""},
    {"relationship_type": " "}, {"relationship_type": " Friend"},
    {"relationship_type": "Friend "}, {"relationship_type": "x" * 201},
])
def test_relationship_checks_reject_invalid_direct_database_values(database, overrides):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    overrides = dict(overrides)
    if overrides.pop("same", False):
        b = a
    if overrides.pop("reverse", False):
        a, b = b, a
    expect_integrity(engine, "SQLITE_CONSTRAINT_CHECK", lambda c:
        relationship(c, scope, a, b, **overrides))


@pytest.mark.parametrize("deleted", [False, True])
def test_natural_key_uniqueness_includes_soft_deleted_relationships(database, deleted):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        relationship(connection, scope, a, b, deleted_at=NOW if deleted else None)
    expect_integrity(engine, "SQLITE_CONSTRAINT_UNIQUE", lambda c:
        relationship(c, scope, a, b, deleted_at=None if deleted else NOW))


def test_type_case_direction_and_directed_endpoint_order_are_distinct(database):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        identifiers = {
            relationship(connection, scope, a, b),
            relationship(connection, scope, a, b, relationship_type="friend"),
            relationship(connection, scope, a, b, relationship_type="Colleague"),
            relationship(connection, scope, a, b, directionality="DIRECTED"),
            relationship(connection, scope, b, a, directionality="DIRECTED"),
            relationship(connection, scope, a, b, relationship_type="x" * 200),
        }
    assert len(identifiers) == 6


def test_history_composite_foreign_key_rejects_wrong_scope_and_missing_relationship(database):
    engine, _ = database
    scope, other_scope, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b)
        history(connection, scope, rel)
    for bad_scope, bad_rel in [(other_scope, rel), (scope, uuid4())]:
        expect_integrity(engine, "SQLITE_CONSTRAINT_FOREIGNKEY", lambda c:
            history(c, bad_scope, bad_rel))


@pytest.mark.parametrize("state", ["", " ", " pending", "pending ", "x" * 51])
def test_history_state_trimmed_length_database_check(database, state):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b)
    expect_integrity(engine, "SQLITE_CONSTRAINT_CHECK", lambda c:
        history(c, scope, rel, state=state))


def test_history_states_are_open_and_ties_have_stable_uuid_order(database):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b)
        for identifier, state in [(UUID(int=3), "新状态"), (UUID(int=1), "x" * 50),
                                  (UUID(int=2), "custom-state")]:
            history(connection, scope, rel, id=identifier, state=state)
    with engine.connect() as connection:
        table = sa.Table("relationship_history", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(table).order_by(
            table.c.effective_at, table.c.created_at, table.c.id)).all()
    assert [UUID(row.id) for row in rows] == [UUID(int=1), UUID(int=2), UUID(int=3)]
    assert rows[0].note is None


def test_metadata_constraint_names_indexes_and_latest_alembic_check(database):
    engine, migration = database
    inspector = sa.inspect(engine)
    # Eleven business fact tables plus the mapped search_document projection, the
    # Phase 8 analysis ledger and the Phase 9 memory table: none of those migrations
    # adds a business fact table.
    assert set(Base.metadata.tables) == (FACT_TABLES | {"search_document"} | ANALYSIS_TABLES
                                        | MEMORY_TABLES)
    assert len(FACT_TABLES) == 11
    assert FACT_TABLES.isdisjoint(ANALYSIS_TABLES)
    assert FACT_TABLES.isdisjoint(MEMORY_TABLES)
    assert "search_document_fts" not in Base.metadata.tables
    assert current_heads(engine) == ("0003_relationship",)
    expected_names = {
        "relationship": {
            "pk_relationship", "fk_relationship_workspace_id_workspace",
            "fk_relationship_workspace_person_a", "fk_relationship_workspace_person_b",
            "uq_relationship_workspace_id_id", "uq_relationship_workspace_pair_type_directionality",
            "ck_relationship_people_distinct", "ck_relationship_directionality_valid",
            "ck_relationship_undirected_sorted", "ck_relationship_type_trimmed_length",
        },
        "relationship_history": {
            "pk_relationship_history", "fk_relationship_history_workspace_relationship",
            "ck_relationship_history_state_trimmed_length",
        },
    }
    for name, names in expected_names.items():
        actual = {c.name for c in Base.metadata.tables[name].constraints}
        if name == "relationship_history":
            actual.remove("fk_relationship_history_workspace_event")
        assert actual == names
    for name in sorted(ALL_TABLES):
        table = Base.metadata.tables[name]
        assert inspector.get_pk_constraint(name)["name"] == table.primary_key.name
        columns = set(table.c.keys()) - ({"event_id"} if name == "relationship_history" else set())
        assert {c["name"] for c in inspector.get_columns(name)} == columns
        for kind, reflected in [
            (sa.CheckConstraint, inspector.get_check_constraints(name)),
            (sa.ForeignKeyConstraint, inspector.get_foreign_keys(name)),
            (sa.UniqueConstraint, inspector.get_unique_constraints(name)),
        ]:
            assert {c["name"] for c in reflected} == {
                c.name for c in table.constraints if isinstance(c, kind)
                and c.name != "fk_relationship_history_workspace_event"}
        assert {i["name"] for i in inspector.get_indexes(name)} == {
            i.name for i in table.indexes if i.name != "ix_relationship_history_workspace_event"}
        assert all(c.name for c in table.constraints)
    assert {tuple(i["column_names"]) for i in inspector.get_indexes("relationship")} == {
        ("workspace_id", "person_a_id"), ("workspace_id", "person_b_id"),
        ("workspace_id", "relationship_type"), ("workspace_id", "deleted_at"),
    }
    assert inspector.get_indexes("relationship_history")[0]["column_names"] == [
        "workspace_id", "relationship_id", "effective_at", "created_at", "id"]
    migrate(engine, migration, "0004_event")
    assert current_heads(engine) == ("0004_event",)
    migrate(engine, migration, "0005_evidence")
    assert current_heads(engine) == ("0005_evidence",)
    # The Phase 4 assertions above stay frozen at 0004; the projection arrives only
    # in 0006, the analysis ledger only in 0007 and the memory table only in 0008, so
    # the database has to reach the current head before command.check().
    migrate(engine, migration, "0006_search_projection")
    assert current_heads(engine) == ("0006_search_projection",)
    assert set(sa.inspect(engine).get_table_names()) == (
        FACT_TABLES | PROJECTION_TABLES | FTS_SHADOW_TABLES | {"alembic_version"})
    migrate(engine, migration, "0007_analysis")
    assert current_heads(engine) == ("0007_analysis",)
    assert set(sa.inspect(engine).get_table_names()) == (
        FACT_TABLES | PROJECTION_TABLES | ANALYSIS_TABLES | FTS_SHADOW_TABLES
        | {"alembic_version"})
    migrate(engine, migration, "0008_memory")
    assert current_heads(engine) == ("0008_memory",)
    assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
    with engine.connect() as connection:
        migration.attributes["connection"] = connection
        try:
            command.check(migration)
        finally:
            migration.attributes.pop("connection", None)


def test_postgresql_model_and_frozen_migration_ddl_compile():
    # For every fact table and the Phase 7 projection table.
    for name in FACT_TABLES | {"search_document"}:
        table = Base.metadata.tables[name]
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
        for index in table.indexes:
            assert "CREATE" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    path = ROOT / "alembic" / "versions" / "0003_relationship.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
                   for node in ast.walk(module))
    assert "UTCDateTime" not in source
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    assert namespace["down_revision"] == "0002_workspace_person_media"
    statements = []
    engine = sa.create_mock_engine("postgresql://", lambda statement, *args, **kwargs:
        statements.append(str(statement.compile(dialect=postgresql.dialect()))))
    context = MigrationContext.configure(engine.connect())
    with Operations.context(context):
        namespace["upgrade"]()
        namespace["downgrade"]()
    assert sum("CREATE TABLE" in sql for sql in statements) == 2
    assert sum("DROP TABLE" in sql for sql in statements) == 2
    assert all("INSERT" not in sql for sql in statements)
    assert "TIMESTAMP WITH TIME ZONE" in "\n".join(statements)


def test_relationship_and_history_utc_reload(database):
    engine, migration = database
    migrate(engine, migration, "0004_event")
    scope, _, a, b, _ = seeded(engine)
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b, created_at=local,
                           updated_at=local, deleted_at=local)
        hist = history(connection, scope, rel, effective_at=local, created_at=local)
    engine.dispose()
    with Session(engine) as session:
        row = session.get(RelationshipModel, rel)
        entry = session.get(RelationshipHistoryModel, hist)
        for value in [row.created_at, row.updated_at, row.deleted_at,
                      entry.effective_at, entry.created_at]:
            assert value == NOW and value.tzinfo is timezone.utc


@pytest.mark.parametrize("field,value", [
    ("id", UUID(int=999)), ("workspace_id", UUID(int=999)),
    ("person_a_id", UUID(int=999)), ("person_b_id", UUID(int=999)),
    ("relationship_type", "Other"), ("directionality", "DIRECTED"),
    ("created_at", NOW + timedelta(days=1)),
])
def test_orm_relationship_identity_changes_fail_and_rollback_restores(database, field, value):
    engine, _ = database
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b)
    with Session(engine) as session:
        row = session.get(RelationshipModel, rel)
        original = getattr(row, field)
        setattr(row, field, value)
        with pytest.raises(DomainError, match="身份"):
            session.flush()
        session.rollback()
        assert getattr(session.get(RelationshipModel, rel), field) == original
        row = session.get(RelationshipModel, rel)
        row.deleted_at = NOW
        row.updated_at = NOW + timedelta(days=1)
        session.commit()
        assert session.get(RelationshipModel, rel).deleted_at == NOW


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_orm_history_is_append_only_and_rollback_restores(database, operation):
    engine, migration = database
    migrate(engine, migration, "0004_event")
    scope, _, a, b, _ = seeded(engine)
    with engine.begin() as connection:
        rel = relationship(connection, scope, a, b)
        identifier = history(connection, scope, rel, note="原注释")
    with Session(engine) as session:
        row = session.get(RelationshipHistoryModel, identifier)
        if operation == "update":
            row.note = "改写"
        else:
            session.delete(row)
        with pytest.raises(DomainError, match="追加"):
            session.flush()
        session.rollback()
        assert session.get(RelationshipHistoryModel, identifier).note == "原注释"
