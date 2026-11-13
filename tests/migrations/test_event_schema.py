"""Phase 5 contracts on real SQLite migrations; PostgreSQL coverage is compilation only."""
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
    EvidenceMediaModel, EvidenceModel, EventModel, EventParticipantModel, MediaModel, PersonAliasModel,
    PersonMediaModel, PersonModel, RelationshipHistoryModel, RelationshipModel, WorkspaceModel,
)

NOW = datetime(2026, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc)
OLD = {"workspace", "person", "person_alias", "media", "person_media", "relationship", "relationship_history"}
PHASE5_TABLES = OLD | {"event", "event_participant"}
# Eleven business fact tables; migration 0006 adds none and changes none.
ALL = PHASE5_TABLES | {"evidence", "evidence_media"}
# Phase 7 projection infrastructure: the mapped search_document table, the FTS5
# virtual table and the five shadow tables SQLite creates for the FTS5 index.
PROJECTION_TABLES = {"search_document", "search_document_fts"}
FTS_SHADOW_TABLES = {"search_document_fts_data", "search_document_fts_idx",
                     "search_document_fts_content", "search_document_fts_docsize",
                     "search_document_fts_config"}
# Phase 8 analysis ledger: the run table plus four provenance tables. It is part of
# the complete head schema and never one of the eleven business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 memory table: user-confirmed facts, preferences and context. Part of the
# complete head schema, never a business fact table.
MEMORY_TABLES = {"memory"}
HEAD_TABLES = (ALL | PROJECTION_TABLES | FTS_SHADOW_TABLES | ANALYSIS_TABLES | MEMORY_TABLES
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
    config = AppConfig(data_dir=tmp_path / "event migration 数据库")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        # Current head, so metadata comparisons include every later table.
        migrate(engine, migration, "head")
        yield engine, migration
    finally:
        engine.dispose()


def seed_original(connection):
    scope, other = uuid4(), uuid4()
    for identifier in (scope, other):
        connection.execute(WorkspaceModel.__table__.insert().values(
            id=identifier, name=str(identifier), description="保留工作区", created_at=NOW, updated_at=NOW))
    a, b, foreign = sorted((uuid4(), uuid4())) + [uuid4()]
    for identifier, workspace in ((a, scope), (b, scope), (foreign, other)):
        connection.execute(PersonModel.__table__.insert().values(
            id=identifier, workspace_id=workspace, display_name="sentinel", notes="保留人物",
            created_at=NOW, updated_at=NOW))
    media, relationship, history = uuid4(), uuid4(), uuid4()
    connection.execute(MediaModel.__table__.insert().values(
        id=media, workspace_id=scope, sha256="a" * 64, mime_type="image/png", size_bytes=9,
        width=1, height=1, storage_path="media/sentinel.png", created_at=NOW, updated_at=NOW))
    connection.execute(PersonAliasModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, person_id=a, alias="原名", alias_type="OTHER",
        created_at=NOW, updated_at=NOW))
    connection.execute(PersonMediaModel.__table__.insert().values(
        id=uuid4(), workspace_id=scope, person_id=a, media_id=media, role="AVATAR", sort_order=0, created_at=NOW))
    connection.execute(RelationshipModel.__table__.insert().values(
        id=relationship, workspace_id=scope, person_a_id=a, person_b_id=b,
        relationship_type="Friend", directionality="UNDIRECTED", created_at=NOW, updated_at=NOW))
    # Explicitly list 0003 columns: this helper also seeds the old schema.
    connection.execute(RelationshipHistoryModel.__table__.insert().values(
        id=history, workspace_id=scope, relationship_id=relationship, state="ACTIVE",
        effective_at=NOW, created_at=NOW, note="保留历史"))
    return scope, other, a, b, foreign, relationship, history


def add_event(connection, scope, **overrides):
    values = dict(id=uuid4(), workspace_id=scope, title="Meeting", event_type="Meeting",
                  record_kind="FACT", occurred_on=date(2026, 3, 4), started_at=None,
                  ended_at=None, created_at=NOW, updated_at=NOW)
    values.update(overrides)
    connection.execute(EventModel.__table__.insert().values(**values))
    return values["id"]


def add_participant(connection, scope, event_id, person_id, **overrides):
    values = dict(id=uuid4(), workspace_id=scope, event_id=event_id,
                  person_id=person_id, role=None, note=None, created_at=NOW)
    values.update(overrides)
    connection.execute(EventParticipantModel.__table__.insert().values(**values))
    return values["id"]


def expect_integrity(engine, code, callback):
    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            callback(connection)
    assert error.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_" + code


def old_snapshot(engine):
    """Compare original rows and all old constraints, ignoring the intentional new attachment."""
    rows, schemas = {}, {}
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        for name in sorted(OLD):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            columns = [column for column in table.c if column.name != "event_id"]
            rows[name] = connection.execute(sa.select(*columns).order_by(table.c.id)).all()
            schemas[name] = (
                [c for c in inspector.get_columns(name) if c["name"] != "event_id"],
                inspector.get_pk_constraint(name), inspector.get_check_constraints(name),
                inspector.get_unique_constraints(name),
                [c for c in inspector.get_foreign_keys(name) if c["name"] != "fk_relationship_history_workspace_event"],
                [i for i in inspector.get_indexes(name) if i["name"] != "ix_relationship_history_workspace_event"],
            )
    # Reflection allocates fresh type and SQL-expression objects on each call.
    # Normalize their representations, preserving every column property and index predicate.
    for schema in schemas.values():
        for column in schema[0]:
            column["type"] = str(column["type"])
        for index in schema[5]:
            index["dialect_options"] = {
                key: str(value) if isinstance(value, sa.sql.elements.ClauseElement) else value
                for key, value in index.get("dialect_options", {}).items()
            }
    return rows, schemas


def test_empty_database_explicit_1_2_3_4(tmp_path, monkeypatch):
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        for revision, count in [("0001_empty_initial", 0), ("0002_workspace_person_media", 5),
                                ("0003_relationship", 7), ("0004_event", 9)]:
            migrate(engine, migration, revision)
            assert current_heads(engine) == (revision,)
            names = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
            assert len(names) == count
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                for name in names:
                    table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                    assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
    finally:
        engine.dispose()


def test_seeded_3_4_3_4_retains_all_original_rows_and_constraints(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        migrate(engine, migration, "0003_relationship")
        with engine.begin() as connection:
            scope, other, a, b, foreign, rel, hist = seed_original(connection)
        before = old_snapshot(engine)
        for revision, down in [("0004_event", False), ("0003_relationship", True), ("0004_event", False)]:
            migrate(engine, migration, revision, down=down)
            assert current_heads(engine) == (revision,)
            assert old_snapshot(engine) == before
            assert set(sa.inspect(engine).get_table_names()) == (PHASE5_TABLES if not down else OLD) | {"alembic_version"}
            with engine.begin() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                if not down:
                    assert connection.scalar(sa.select(RelationshipHistoryModel.event_id)) is None
                    event_id = add_event(connection, scope)
                    add_participant(connection, scope, event_id, a, role="普通字符串角色", note="保留")
                    connection.execute(RelationshipHistoryModel.__table__.update().where(
                        RelationshipHistoryModel.id == hist).values(event_id=event_id))
            # Downgrade follows a real non-NULL attachment, dropping only the new column.
            expect_integrity(engine, "FOREIGNKEY", lambda c: c.execute(PersonAliasModel.__table__.insert().values(
                id=uuid4(), workspace_id=scope, person_id=foreign, alias="bad", alias_type="OTHER",
                created_at=NOW, updated_at=NOW)))
            expect_integrity(engine, "CHECK", lambda c: c.execute(RelationshipHistoryModel.__table__.insert().values(
                id=uuid4(), workspace_id=scope, relationship_id=rel, state=" bad", effective_at=NOW, created_at=NOW)))
    finally:
        engine.dispose()


def test_failed_0004_migration_rolls_back_ddl_data_and_version(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        migrate(engine, migration, "0003_relationship")
        with engine.begin() as connection:
            seed_original(connection)
        before = old_snapshot(engine)
        # BatchOperations queues first, so inject when executing the rebuilt table's indexes.
        from alembic.ddl.sqlite import SQLiteImpl
        original_impl = SQLiteImpl.create_index
        def fail_impl(self, index, **kwargs):
            if index.name == "ix_relationship_history_workspace_event":
                raise RuntimeError("controlled migration failure after history rebuild")
            return original_impl(self, index, **kwargs)
        monkeypatch.setattr(SQLiteImpl, "create_index", fail_impl)
        with pytest.raises(RuntimeError, match="controlled migration failure"):
            migrate(engine, migration, "0004_event")
        assert current_heads(engine) == ("0003_relationship",)
        assert set(sa.inspect(engine).get_table_names()) == OLD | {"alembic_version"}
        assert old_snapshot(engine) == before
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        monkeypatch.setattr(SQLiteImpl, "create_index", original_impl)
        migrate(engine, migration, "0004_event")
        assert current_heads(engine) == ("0004_event",)
    finally:
        engine.dispose()


@pytest.mark.parametrize("overrides", [
    {"title": ""}, {"title": " Meeting"}, {"title": "Meeting "}, {"title": "x" * 201},
    {"event_type": ""}, {"event_type": " Meeting"}, {"event_type": "Meeting "}, {"event_type": "x" * 201},
    {"record_kind": "fact"}, {"record_kind": "UNKNOWN"},
    {"occurred_on": None}, {"started_at": NOW}, {"ended_at": NOW},
    {"occurred_on": None, "ended_at": NOW},
    {"occurred_on": None, "started_at": NOW, "ended_at": NOW - timedelta(microseconds=1)},
])
def test_event_strict_checks(database, overrides):
    engine, _ = database
    with engine.begin() as connection:
        scope, *_ = seed_original(connection)
    expect_integrity(engine, "CHECK", lambda c: add_event(c, scope, **overrides))


def test_date_timestamp_modes_case_sensitive_values_and_utc_reload(database):
    engine, _ = database
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    with engine.begin() as connection:
        scope, *_ = seed_original(connection)
        identifiers = [add_event(connection, scope), add_event(connection, scope, title="meeting", event_type="meeting"),
                       add_event(connection, scope, title="x" * 200, event_type="x" * 200, record_kind="OBSERVATION"),
                       add_event(connection, scope, occurred_on=None, started_at=local),
                       add_event(connection, scope, occurred_on=None, started_at=local, ended_at=local,
                                 created_at=local, updated_at=local, deleted_at=local,
                                 location="x" * 10000, description="私人说明")]
        assert connection.scalar(sa.select(sa.func.count()).select_from(EventModel).where(EventModel.event_type == "Meeting")) == 3
        assert connection.scalar(sa.select(sa.func.count()).select_from(EventModel).where(EventModel.event_type == "meeting")) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(EventModel).where(EventModel.title == "Meeting")) == 3
        assert connection.scalar(sa.select(sa.func.count()).select_from(EventModel).where(EventModel.title == "meeting")) == 1
    engine.dispose()
    with Session(engine) as session:
        date_row = session.get(EventModel, identifiers[0])
        assert date_row.occurred_on == date(2026, 3, 4)
        assert date_row.started_at is date_row.ended_at is None
        time_row = session.get(EventModel, identifiers[-1])
        assert time_row.occurred_on is None
        for name in ("started_at", "ended_at", "created_at", "updated_at", "deleted_at"):
            value = getattr(time_row, name)
            assert value == NOW and value.tzinfo is timezone.utc


def test_event_workspace_participant_and_history_scope_foreign_keys_and_unique(database):
    engine, _ = database
    with engine.begin() as connection:
        scope, other, a, b, foreign, rel, hist = seed_original(connection)
        first, second = add_event(connection, scope), add_event(connection, other)
        add_participant(connection, scope, first, a, role="custom role")
        add_participant(connection, scope, first, b, role=None)
        connection.execute(RelationshipHistoryModel.__table__.update().where(
            RelationshipHistoryModel.id == hist).values(event_id=first))
    expect_integrity(engine, "FOREIGNKEY", lambda c: add_event(c, uuid4()))
    expect_integrity(engine, "UNIQUE", lambda c: add_participant(c, scope, first, a, role="different role"))
    for ws, event_id, person_id in [(scope, second, b), (scope, first, foreign),
                                   (other, first, foreign), (scope, uuid4(), b), (scope, first, uuid4())]:
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_participant(c, ws, event_id, person_id))
    for bad_event in (second, uuid4()):
        expect_integrity(engine, "FOREIGNKEY", lambda c: c.execute(RelationshipHistoryModel.__table__.update().where(
            RelationshipHistoryModel.id == hist).values(event_id=bad_event)))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_head_metadata_matches_named_schema_and_alembic_check(database):
    engine, migration = database
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
    # The projection objects exist on top of those fact tables.
    assert set(inspector.get_table_names()) == HEAD_TABLES
    assert {name for name in inspector.get_table_names() if "search_document" in name} == (
        PROJECTION_TABLES | FTS_SHADOW_TABLES)
    assert current_heads(engine) == HEAD
    assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("event_participant")} == {
        ("workspace_id", "event_id"), ("workspace_id", "person_id")}
    assert {tuple(i["column_names"]) for i in inspector.get_indexes("event")} == {
        ("workspace_id", "started_at"), ("workspace_id", "occurred_on"),
        ("workspace_id", "deleted_at"), ("workspace_id", "event_type")}
    with engine.connect() as connection:
        migration.attributes["connection"] = connection
        try:
            command.check(migration)
        finally:
            migration.attributes.pop("connection", None)


def test_offline_sql_roundtrip_executes_frozen_history_without_schema_or_data_loss(tmp_path):
    from io import StringIO

    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)

    def offline_script(revision, *, down=False, history_rebuild=True):
        """Render one offline script from the script directory, not the database.

        Offline rendering always walks the whole chain, and a downgrade range is
        resolved from the script head, so a downgrade names its start revision
        explicitly: the engine reaches 0007 with Phase 8 and a bare
        ``command.downgrade`` would only cover the already-current tail.
        """
        output = StringIO()
        migration.output_buffer = output
        if down:
            command.downgrade(migration, f"{current_heads(engine)[0]}:{revision}", sql=True)
        else:
            command.upgrade(migration, revision, sql=True)
        sql = output.getvalue()
        if history_rebuild:
            assert "_alembic_tmp_relationship_history" in sql
            assert "ck_relationship_history_state_trimmed_length" in sql
            assert "ck_relationship_history_ck_" not in sql
        else:
            # A 0004/0005/0006-only range must not rebuild the history table again.
            assert "_alembic_tmp_relationship_history" not in sql
        return sql

    def execute_script(sql):
        # Generated SQLite offline DDL is executed by the real DBAPI, with FK ON.
        connection = engine.raw_connection()
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            connection.executescript(sql)
        finally:
            connection.close()

    def fresh_script(revision, *, history_rebuild=False):
        """Execute a full offline script on a brand-new file of the same config.

        An upgrade script always starts at the base revision: alembic has no
        offline notion of the database's current revision, so only a fresh
        database can execute one. This is the offline "clean install" run.
        """
        script = offline_script(revision, history_rebuild=history_rebuild)
        database = config.storage_paths.database
        # Windows keeps the file locked while the pool still holds it open.
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        execute_script(script)
        assert current_heads(engine) == (revision,)
        return script

    try:
        sql = offline_script("head")
        assert sa.inspect(engine).get_table_names() == []  # Rendering cannot connect/create schema.
        # 0006 renders its projection DDL offline, including the FTS5 virtual table.
        assert "CREATE VIRTUAL TABLE search_document_fts USING fts5(" in sql
        assert "CREATE TABLE search_document " in sql
        execute_script(sql)
        assert current_heads(engine) == HEAD
        assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
        with engine.begin() as connection:
            scope, _, a, _, _, _, hist = seed_original(connection)
            event_id = add_event(connection, scope)
            add_participant(connection, scope, event_id, a)
            connection.execute(RelationshipHistoryModel.__table__.update().where(
                RelationshipHistoryModel.id == hist).values(event_id=event_id))
        before = old_snapshot(engine)
        # 0006 -> 0004 removes the projection and both Phase 5/6 tables in one script.
        execute_script(offline_script("0004_event", down=True, history_rebuild=False))
        assert current_heads(engine) == ("0004_event",)
        assert set(sa.inspect(engine).get_table_names()) == PHASE5_TABLES | {"alembic_version"}
        assert old_snapshot(engine) == before
        # The non-NULL attachment survives that downgrade and only disappears
        # with the 0003 schema, which has no relationship_history.event_id.
        with engine.connect() as connection:
            assert connection.scalar(sa.select(RelationshipHistoryModel.event_id)) is not None
        execute_script(offline_script("0003_relationship", down=True))
        assert current_heads(engine) == ("0003_relationship",)
        assert set(sa.inspect(engine).get_table_names()) == OLD | {"alembic_version"}
        assert old_snapshot(engine) == before
        with pytest.raises(sa.exc.OperationalError):
            with engine.connect() as connection:
                connection.scalar(sa.select(RelationshipHistoryModel.event_id))
        # The 0004 SQLite batch rebuild is part of every full-chain render.
        rebuild_sql = offline_script("0004_event")
        assert "_alembic_tmp_relationship_history" in rebuild_sql
        evidence_sql = offline_script("0005_evidence")
        assert "CREATE TABLE evidence " in evidence_sql and "CREATE TABLE evidence_media " in evidence_sql
        assert "role IS NULL" in evidence_sql  # The partial unique index is part of the script.
        projection_sql = offline_script("0006_search_projection")
        assert "CREATE VIRTUAL TABLE search_document_fts USING fts5(" in projection_sql
        assert "CREATE TABLE search_document " in projection_sql
        analysis_sql = offline_script("0007_analysis")
        assert "CREATE TABLE analysis_run " in analysis_sql
        assert "CREATE TABLE memory " not in analysis_sql
        # The 0006 contribution is projection DDL only: cut both later tails away and
        # exactly that statement block is left, with no further history rebuild.
        delta = projection_sql[len(evidence_sql):].replace(analysis_sql[len(evidence_sql):], "")
        assert "search_document" in delta
        assert "_alembic_tmp_relationship_history" not in delta
        assert "CREATE TABLE evidence " not in delta
        assert "analysis_run" not in delta
        # Neither rendering nor the two downgrades touched a single fact row.
        assert old_snapshot(engine) == before
        # Every revision still executes offline on a brand-new SQLite file,
        # including the 0006 FTS5 DDL.
        fresh_script("0003_relationship")
        fresh_script("0004_event", history_rebuild=True)
        fresh_script("0005_evidence", history_rebuild=True)
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceModel)) == 0
            assert connection.scalar(sa.select(sa.func.count()).select_from(EvidenceMediaModel)) == 0
        fresh_script("0006_search_projection", history_rebuild=True)
        fresh_script("0007_analysis", history_rebuild=True)
        # The last revision of the chain executes offline too and leaves the head.
        fresh_script("0008_memory", history_rebuild=True)
        assert current_heads(engine) == HEAD
        assert set(sa.inspect(engine).get_table_names()) == HEAD_TABLES
        test_head_metadata_matches_named_schema_and_alembic_check((engine, migration))
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_postgresql_orm_and_frozen_migration_compile_only():
    # The eleven fact tables plus the Phase 7 search_document projection table.
    for name in ALL | {"search_document"}:
        table = Base.metadata.tables[name]
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    path = ROOT / "alembic" / "versions" / "0004_event.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app") for node in ast.walk(module))
    assert "UTCDateTime" not in source
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    assert namespace["down_revision"] == "0003_relationship"
    statements = []
    engine = sa.create_mock_engine("postgresql://", lambda sql, *a, **k: statements.append(
        str(sql.compile(dialect=postgresql.dialect()))))
    with Operations.context(MigrationContext.configure(engine.connect())):
        namespace["upgrade"]()
        namespace["downgrade"]()
    assert sum("CREATE TABLE" in sql for sql in statements) == 2
    assert sum("DROP TABLE" in sql for sql in statements) == 2
    assert any("ALTER TABLE relationship_history ADD COLUMN event_id UUID" in sql for sql in statements)
    assert any("DROP COLUMN event_id" in sql for sql in statements)
    assert "TIMESTAMP WITH TIME ZONE" in "\n".join(statements)
    assert all("INSERT" not in sql for sql in statements)


@pytest.mark.parametrize("expired", [False, True])
def test_row_specific_attachment_authorization_allows_only_event_id_and_detach(database, expired):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, _, _, _, _, identifier = seed_original(connection)
        event_id = add_event(connection, scope)
    with Session(engine) as session:
        row = session.get(RelationshipHistoryModel, identifier)
        if expired:
            session.expire(row, ["event_id"])
        row.event_id = event_id
        session.info["ris_history_event_attachment"] = (identifier, None, event_id)
        try:
            session.flush()
        finally:
            session.info.pop("ris_history_event_attachment", None)
        session.commit()
        assert session.get(RelationshipHistoryModel, identifier).event_id == event_id
        row.event_id = None
        session.info["ris_history_event_attachment"] = (identifier, event_id, None)
        try:
            session.flush()
        finally:
            session.info.pop("ris_history_event_attachment", None)
        session.commit()
        assert session.get(RelationshipHistoryModel, identifier).event_id is None


@pytest.mark.parametrize("case", ["plain", "wrong-row", "wrong-old", "wrong-new", "extra-field", "delete"])
def test_attachment_exception_never_authorizes_other_history_changes(database, case):
    engine, _ = database
    with engine.begin() as connection:
        scope, _, _, _, _, _, identifier = seed_original(connection)
        event_id = add_event(connection, scope)
    with Session(engine) as session:
        row = session.get(RelationshipHistoryModel, identifier)
        row.event_id = event_id
        flag = (identifier, None, event_id)
        if case == "wrong-row":
            flag = (uuid4(), None, event_id)
        elif case == "wrong-old":
            flag = (identifier, uuid4(), event_id)
        elif case == "wrong-new":
            flag = (identifier, None, uuid4())
        elif case == "extra-field":
            row.note = "forbidden rewrite"
        elif case == "delete":
            session.delete(row)
        if case != "plain":
            session.info["ris_history_event_attachment"] = flag
        try:
            with pytest.raises(DomainError, match="追加"):
                session.flush()
        finally:
            session.info.pop("ris_history_event_attachment", None)
            session.rollback()
        original = session.get(RelationshipHistoryModel, identifier)
        assert original.event_id is None and original.note == "保留历史"
