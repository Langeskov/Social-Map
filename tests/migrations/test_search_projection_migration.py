"""Phase 7 search-projection migration: real SQLite, offline SQL and PostgreSQL DDL.

Revision ``0006_search_projection`` adds projection infrastructure only: the
ordinary table ``search_document`` plus the FTS5 virtual table
``search_document_fts``. No business fact table is created, changed or dropped,
so every fact-table assertion below is paired with an explicit projection
assertion instead of being widened.

PostgreSQL is verified by DDL compilation only: FTS5 is a SQLite feature and is
never claimed to work on PostgreSQL. No PostgreSQL server is started here.
"""
import ast
from io import StringIO
from pathlib import Path
from uuid import uuid4

from alembic import command
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.config.settings import AppConfig
from app.storage.database import Base, create_sqlite_engine, load_models
from app.storage.migrations import alembic_config, current_heads
from app.storage.models import (
    EvidenceMediaModel, EvidenceModel, EventModel, EventParticipantModel, MediaModel,
    PersonAliasModel, PersonMediaModel, PersonModel, RelationshipHistoryModel,
    RelationshipModel, SearchDocumentModel, WorkspaceModel,
)
from tests.migrations.schema_facts import (
    ANALYSIS_TABLES, EVIDENCE_TABLES, FACT_TABLES, MEMORY_TABLES, NOW, PROJECTION_OBJECTS,
    PROJECTION_TABLES, SEARCH_DOCUMENT, SEARCH_DOCUMENT_FTS, fts_shadow_tables,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0006_search_projection.py"
FTS_CREATE = f"CREATE VIRTUAL TABLE {SEARCH_DOCUMENT_FTS} USING fts5("
FTS_COLUMNS_DDL = FTS_CREATE + "title_tokens, body_tokens)"
FTS_DROP = f"DROP TABLE IF EXISTS {SEARCH_DOCUMENT_FTS}"
ENTITY_TYPES = ("PERSON", "ALIAS", "EVENT", "EVIDENCE")


def migrate(engine, config, revision, *, down=False):
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        try:
            (command.downgrade if down else command.upgrade)(config, revision)
        finally:
            config.attributes.pop("connection", None)


def open_database(tmp_path, monkeypatch):
    """A real temporary SQLite file; only migrations may create formal tables."""
    monkeypatch.setattr(Base.metadata, "create_all",
                        lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path / "phase7 search projection 数据库")
    return create_sqlite_engine(config), alembic_config(config)


def insert(connection, model, **values):
    connection.execute(model.__table__.insert().values(**values))


def seed_facts(connection):
    """Insert one complete row in each of the eleven business fact tables."""
    scope, other = uuid4(), uuid4()
    for identifier in (scope, other):
        insert(connection, WorkspaceModel, id=identifier, name=str(identifier),
               description="保留工作区", created_at=NOW, updated_at=NOW)
    person_a, person_b, foreign_person = sorted((uuid4(), uuid4(), uuid4()))
    for identifier, workspace, name in ((person_a, scope, "保留人物 A"),
                                        (person_b, scope, "保留人物 B"),
                                        (foreign_person, other, "其他工作区人物")):
        insert(connection, PersonModel, id=identifier, workspace_id=workspace, display_name=name,
               real_name=None, notes="保留说明", created_at=NOW, updated_at=NOW)
    media, foreign_media = uuid4(), uuid4()
    insert(connection, MediaModel, id=media, workspace_id=scope, sha256="a" * 64,
           mime_type="image/png", size_bytes=42, width=2, height=3,
           storage_path=f"media/{media}.png", created_at=NOW, updated_at=NOW)
    insert(connection, MediaModel, id=foreign_media, workspace_id=other, sha256="b" * 64,
           mime_type="image/png", size_bytes=7, width=1, height=1,
           storage_path=f"media/{foreign_media}.png", created_at=NOW, updated_at=NOW)
    insert(connection, PersonAliasModel, id=uuid4(), workspace_id=scope, person_id=person_a,
           alias="保留别名", alias_type="NICKNAME", note="保留", created_at=NOW, updated_at=NOW)
    insert(connection, PersonMediaModel, id=uuid4(), workspace_id=scope, person_id=person_a,
           media_id=media, role="GALLERY", sort_order=2, created_at=NOW)
    relationship = uuid4()
    insert(connection, RelationshipModel, id=relationship, workspace_id=scope,
           person_a_id=person_a, person_b_id=person_b, relationship_type="Friend",
           directionality="UNDIRECTED", created_at=NOW, updated_at=NOW)
    insert(connection, RelationshipHistoryModel, id=uuid4(), workspace_id=scope,
           relationship_id=relationship, event_id=None, state="ACTIVE",
           effective_at=NOW, note="保留历史", created_at=NOW)
    event = uuid4()
    insert(connection, EventModel, id=event, workspace_id=scope, title="保留事件",
           event_type="Meeting", record_kind="FACT", occurred_on=NOW.date(),
           started_at=None, ended_at=None, created_at=NOW, updated_at=NOW)
    insert(connection, EventParticipantModel, id=uuid4(), workspace_id=scope, event_id=event,
           person_id=person_a, role="参与者", note="保留参与记录", created_at=NOW)
    evidence = uuid4()
    insert(connection, EvidenceModel, id=evidence, workspace_id=scope, event_id=event, kind="TEXT",
           title="保留证据", content_text="原始正文", annotation=None, source_type="MANUAL",
           source_label=None, source_reference="原始引用", captured_at=None,
           created_at=NOW, updated_at=NOW, deleted_at=None)
    insert(connection, EvidenceMediaModel, id=uuid4(), workspace_id=scope, evidence_id=evidence,
           media_id=media, role="ATTACHMENT", created_at=NOW)
    return {"scope": scope, "person_a": person_a, "event": event, "evidence": evidence,
            "media": media}


def fact_rows(engine):
    """Canonical rows of all eleven fact tables, keyed by table name."""
    snapshot = {}
    with engine.connect() as connection:
        for name in sorted(FACT_TABLES):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            rows = connection.execute(sa.select(table).order_by(table.c.id)).all()
            # Coerce driver values (UUID, datetime) to stable text so the
            # comparison never depends on reflected Python type identity.
            snapshot[name] = [tuple(str(value) for value in row) for row in rows]
    return snapshot


def fact_schema(engine):
    """Every named constraint and index of all eleven fact tables."""
    snapshot = {}
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        for name in sorted(FACT_TABLES):
            snapshot[name] = (
                [dict(column) for column in inspector.get_columns(name)],
                inspector.get_pk_constraint(name), inspector.get_check_constraints(name),
                inspector.get_unique_constraints(name), inspector.get_foreign_keys(name),
                inspector.get_indexes(name),
            )
    # Reflection allocates fresh type and SQL-expression objects on every call.
    # Normalize their representations to strings while preserving every other
    # property, exactly like the Phase 6 evidence snapshot does.
    for schema in snapshot.values():
        for column in schema[0]:
            column["type"] = str(column["type"])
        for index in schema[5]:
            index["dialect_options"] = {
                key: str(value) if isinstance(value, sa.sql.elements.ClauseElement) else value
                for key, value in index.get("dialect_options", {}).items()
            }
    return snapshot


def fact_state(engine):
    return fact_rows(engine), fact_schema(engine)


def raw_table_names(engine):
    with engine.connect() as connection:
        return {row[0] for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


def sqlite_master_names(engine):
    with engine.connect() as connection:
        return {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master")}


def projection_objects(engine):
    """Projection objects actually present, including the FTS5 shadow tables."""
    return PROJECTION_OBJECTS & sqlite_master_names(engine) | fts_shadow_tables(raw_table_names(engine))


def analysis_objects(engine):
    """Phase 8 analysis objects actually present; empty at 0006 and below."""
    return ANALYSIS_TABLES & sqlite_master_names(engine)


def memory_objects(engine):
    """Phase 9 memory table actually present; empty at 0007 and below."""
    return MEMORY_TABLES & sqlite_master_names(engine)


def projection_head_tables(engine):
    """0006 table set: eleven fact tables, two projection objects, no shadow tables.

    A database migrated to 0006 alone carries no analysis or memory table, so the set
    at that revision is asserted exactly without them.
    """
    return FACT_TABLES | PROJECTION_TABLES | {"alembic_version"}


def expected_head_tables(engine):
    """Current-head table set: facts, projection objects, analysis ledger, memory."""
    return projection_head_tables(engine) | analysis_objects(engine) | memory_objects(engine)


def fact_table_names(engine):
    """Only the business fact tables: every later group is filtered out."""
    names = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
    return (names - PROJECTION_OBJECTS - fts_shadow_tables(names) - ANALYSIS_TABLES
            - MEMORY_TABLES)


def fts_query(connection, term):
    return connection.exec_driver_sql(
        f"SELECT rowid, bm25({SEARCH_DOCUMENT_FTS}) FROM {SEARCH_DOCUMENT_FTS} "
        f"WHERE {SEARCH_DOCUMENT_FTS} MATCH ? ORDER BY rowid", (term,)).all()


def add_search_document(connection, workspace_id, **overrides):
    values = dict(id=1, workspace_id=workspace_id, entity_type="PERSON", entity_id=uuid4(),
                  parent_id=None, title="保留投影标题", body="保留投影正文",
                  title_tokens="alpha beta", body_tokens="gamma delta",
                  occurred_at=None, deleted_at=None, updated_at=NOW)
    values.update(overrides)
    connection.execute(SearchDocumentModel.__table__.insert().values(**values))
    return values["id"]


def assert_fact_schema_matches_orm(engine, names=FACT_TABLES):
    """The migrated fact schema still matches the frozen ORM metadata exactly.

    ``names`` narrows the comparison to table names that really exist on both
    sides (``schema_facts.FACT_TABLES`` is the ORM-mapped fact set).
    """
    inspector = sa.inspect(engine)
    for name in sorted(names):
        table = Base.metadata.tables[name]
        assert {column["name"] for column in inspector.get_columns(name)} == set(table.c.keys())
        assert inspector.get_pk_constraint(name)["name"] == table.primary_key.name
        for kind, reflected in [(sa.CheckConstraint, inspector.get_check_constraints(name)),
                                (sa.ForeignKeyConstraint, inspector.get_foreign_keys(name)),
                                (sa.UniqueConstraint, inspector.get_unique_constraints(name))]:
            assert {item["name"] for item in reflected} == {
                item.name for item in table.constraints if isinstance(item, kind)}
        assert {index["name"] for index in inspector.get_indexes(name)} == {
            index.name for index in table.indexes}


def test_0005_to_0006_adds_only_projection_objects_and_survives_reopen(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0005_evidence")
        assert current_heads(engine) == ("0005_evidence",)
        # Before 0006 the schema carries no %search_document% object at all.
        assert {name for name in sqlite_master_names(engine) if "search_document" in name} == set()
        assert fact_table_names(engine) == FACT_TABLES
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)

        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        assert fact_state(engine) == before
        # The eleven business fact tables are unchanged: no new fact table, and
        # no projection object is ever counted as one.
        assert fact_table_names(engine) == FACT_TABLES
        assert len(fact_table_names(engine)) == 11
        load_models()
        # The ORM also maps the Phase 8 analysis ledger and the Phase 9 memory table,
        # which this revision does not create yet: the complete 0006 schema is
        # asserted as fact + projection only.
        assert set(Base.metadata.tables) == (FACT_TABLES | {SEARCH_DOCUMENT} | ANALYSIS_TABLES
                                             | MEMORY_TABLES)
        assert SEARCH_DOCUMENT_FTS not in Base.metadata.tables
        shadow = fts_shadow_tables(raw_table_names(engine))
        assert len(shadow) == 5
        assert projection_objects(engine) == PROJECTION_OBJECTS | shadow
        assert analysis_objects(engine) == set()
        # The complete schema is the fact tables plus the projection objects
        # (the FTS5 virtual table and the shadow tables SQLite creates for it).
        assert set(sa.inspect(engine).get_table_names()) == (
            projection_head_tables(engine) | shadow)
        with engine.connect() as connection:
            kinds = dict(connection.exec_driver_sql(
                "SELECT name, type FROM sqlite_master WHERE name IN (?, ?)",
                (SEARCH_DOCUMENT, SEARCH_DOCUMENT_FTS)).all())
            assert kinds == {SEARCH_DOCUMENT: "table", SEARCH_DOCUMENT_FTS: "table"}
            assert connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = ?", (SEARCH_DOCUMENT_FTS,)
            ).scalar() == FTS_COLUMNS_DDL
            # FTS5 columns are declared exactly as the migration's frozen tuple.
            assert [row[1] for row in connection.exec_driver_sql(
                f"PRAGMA table_info({SEARCH_DOCUMENT_FTS})").all()] == ["title_tokens", "body_tokens"]
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        # A fresh process must reflect the same projection schema from disk.
        engine.dispose()
        reopened = create_sqlite_engine(AppConfig(data_dir=tmp_path / "phase7 search projection 数据库"))
        try:
            assert current_heads(reopened) == ("0006_search_projection",)
            assert fact_state(reopened) == before
            assert fact_table_names(reopened) == FACT_TABLES
            assert projection_objects(reopened) == projection_objects(engine)
            assert analysis_objects(reopened) == set()
            assert set(sa.inspect(reopened).get_table_names()) == (
                projection_head_tables(reopened) | shadow)
            assert_fact_schema_matches_orm(reopened)
        finally:
            reopened.dispose()
    finally:
        engine.dispose()


def test_search_document_schema_matches_orm_metadata_and_applies_its_constraints(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        # The Phase 7 head: 0007 exists later in the chain and is deliberately not
        # applied, so the projection is asserted at the revision that introduced it.
        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        load_models()
        table = SearchDocumentModel.__table__
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint(SEARCH_DOCUMENT) == {
            "constrained_columns": ["id"], "name": table.primary_key.name}
        assert table.primary_key.name == "pk_search_document"
        assert {column["name"] for column in inspector.get_columns(SEARCH_DOCUMENT)} == set(table.c.keys())
        assert {column["name"]: column["nullable"] for column in inspector.get_columns(SEARCH_DOCUMENT)} == {
            column.name: column.nullable for column in table.columns}
        assert inspector.get_unique_constraints(SEARCH_DOCUMENT) == [
            {"name": "uq_search_document_workspace_entity",
             "column_names": ["workspace_id", "entity_type", "entity_id"]}]
        assert {constraint.name for constraint in table.constraints
                if isinstance(constraint, sa.UniqueConstraint)} == {"uq_search_document_workspace_entity"}
        checks = inspector.get_check_constraints(SEARCH_DOCUMENT)
        assert [check["name"] for check in checks] == ["ck_search_document_entity_type_valid"]
        assert checks[0]["sqltext"] == "entity_type IN ('PERSON','ALIAS','EVENT','EVIDENCE')"
        assert {constraint.name for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)} == {"ck_search_document_entity_type_valid"}
        assert {index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(SEARCH_DOCUMENT)} == {
            "ix_search_document_workspace_type": ("workspace_id", "entity_type"),
            "ix_search_document_workspace_deleted": ("workspace_id", "deleted_at")}
        assert {index.name: tuple(column.name for column in index.columns)
                for index in table.indexes} == {
            "ix_search_document_workspace_type": ("workspace_id", "entity_type"),
            "ix_search_document_workspace_deleted": ("workspace_id", "deleted_at")}
        # The FTS5 virtual table is infrastructure, never a mapped entity.
        assert SEARCH_DOCUMENT_FTS not in Base.metadata.tables

        with engine.begin() as connection:
            workspace = uuid4()
            insert(connection, WorkspaceModel, id=workspace, name=str(workspace),
                   description=None, created_at=NOW, updated_at=NOW)
            entity_id = uuid4()
            for position, entity_type in enumerate(ENTITY_TYPES):
                add_search_document(connection, workspace, id=position + 1,
                                    entity_type=entity_type, entity_id=entity_id)
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(SearchDocumentModel)) == 4

        def expect_integrity(code, callback):
            with pytest.raises(IntegrityError) as error:
                with engine.begin() as connection:
                    callback(connection)
            assert error.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_" + code

        # CHECK(entity_type IN ...) rejects every value outside the frozen set.
        for entity_type in ("UNKNOWN", "person", "Person", "", " EVENT"):
            expect_integrity("CHECK", lambda c, value=entity_type: add_search_document(
                c, uuid4(), id=99, entity_type=value))
        # UNIQUE(workspace_id, entity_type, entity_id) rejects duplicate projections.
        with engine.connect() as connection:
            existing = connection.execute(sa.select(
                SearchDocumentModel.workspace_id, SearchDocumentModel.entity_type,
                SearchDocumentModel.entity_id)).first()
        expect_integrity("UNIQUE", lambda c: add_search_document(
            c, existing.workspace_id, id=98, entity_type=existing.entity_type,
            entity_id=existing.entity_id))
        # The same entity in another workspace is a different projection row.
        with engine.begin() as connection:
            add_search_document(connection, uuid4(), id=97, entity_type=existing.entity_type,
                                entity_id=existing.entity_id)
        # Nullable columns stay nullable while required columns stay NOT NULL.
        with engine.begin() as connection:
            add_search_document(connection, existing.workspace_id, id=50, entity_type="EVENT",
                                entity_id=uuid4(), body=None, body_tokens=None, parent_id=None,
                                occurred_at=NOW, deleted_at=NOW)
        for missing in ("workspace_id", "entity_id", "title", "title_tokens", "updated_at"):
            values = dict(id=51, workspace_id=uuid4(), entity_type="PERSON", entity_id=uuid4(),
                          parent_id=None, title="标题", body=None, title_tokens="alpha",
                          body_tokens=None, occurred_at=None, deleted_at=None, updated_at=NOW)
            values[missing] = None
            expect_integrity("NOTNULL", lambda c, row=values: c.execute(
                SearchDocumentModel.__table__.insert().values(**row)))
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(SearchDocumentModel)) == 6
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE ?",
                (SEARCH_DOCUMENT_FTS + "_%",)).scalar() == 5
    finally:
        engine.dispose()


def test_fts5_virtual_table_accepts_insert_match_and_bm25(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_DOCUMENT_FTS}(rowid, title_tokens, body_tokens) "
                "VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                (1, "alpha beta", "gamma delta", 2, "beta gamma", "epsilon zeta",
                 3, "eta theta", "iota kappa"))
        with engine.connect() as connection:
            matched = fts_query(connection, "beta")
            assert [row[0] for row in matched] == [1, 2]
            assert all(isinstance(row[1], float) for row in matched)
            assert [row[0] for row in fts_query(connection, "kappa")] == [3]
            assert fts_query(connection, "nonexistentterm") == []
            # MATCH addresses a real FTS5 index, which SQLite backs with five
            # shadow tables that are projection objects, never fact tables.
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE ?",
                (SEARCH_DOCUMENT_FTS + "_%",)).scalar() == 5
            # A plain table count covers the eleven fact tables, the two
            # projection objects, alembic_version and the five shadow tables.
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = ?", ("table",)
            ).scalar() == len(expected_head_tables(engine)) + 5
        # The virtual table stores no business truth: the projection stays empty.
        assert projection_objects(engine) == PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
        assert fact_table_names(engine) == FACT_TABLES
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(SearchDocumentModel)) == 0
    finally:
        engine.dispose()


def test_0006_to_0005_downgrade_drops_projection_and_keeps_every_fact_row(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0005_evidence")
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        migrate(engine, migration, "0006_search_projection")
        with engine.begin() as connection:
            connection.execute(SearchDocumentModel.__table__.insert().values(
                id=1, workspace_id=uuid4(), entity_type="PERSON", entity_id=uuid4(),
                parent_id=None, title="投影行", body=None, title_tokens="alpha",
                body_tokens=None, occurred_at=None, deleted_at=None, updated_at=NOW))
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_DOCUMENT_FTS}(title_tokens, body_tokens) VALUES ('alpha', 'beta')")
        assert fact_state(engine) == before

        migrate(engine, migration, "0005_evidence", down=True)
        assert current_heads(engine) == ("0005_evidence",)
        assert {name for name in sqlite_master_names(engine) if "search_document" in name} == set()
        assert projection_objects(engine) == set()
        assert raw_table_names(engine) & {SEARCH_DOCUMENT, SEARCH_DOCUMENT_FTS} == set()
        assert fact_table_names(engine) == FACT_TABLES
        assert set(sa.inspect(engine).get_table_names()) == FACT_TABLES | {"alembic_version"}
        assert fact_rows(engine) == before[0]
        assert fact_schema(engine) == before[1]
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        for name in (SEARCH_DOCUMENT, SEARCH_DOCUMENT_FTS):
            with pytest.raises(sa.exc.NoSuchTableError):
                sa.inspect(engine).get_indexes(name)
        # The projection is rebuildable: dropping and re-running recreates an
        # empty index that accepts new content.
        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        assert fact_state(engine) == before
        assert projection_objects(engine) == PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(SearchDocumentModel)) == 0
            assert connection.exec_driver_sql(
                f"SELECT count(*) FROM {SEARCH_DOCUMENT_FTS}").scalar() == 0
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_DOCUMENT_FTS}(title_tokens, body_tokens) VALUES (?, ?)",
                ("rebuilt tokens", "rebuilt body"))
        with engine.connect() as connection:
            assert [row[0] for row in fts_query(connection, "rebuilt")] == [1]
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_0005_0006_0005_0006_roundtrip_keeps_facts_and_leaves_projection_empty(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0005_evidence")
        with engine.begin() as connection:
            seed_facts(connection)
        facts, schema = fact_rows(engine), fact_schema(engine)
        for revision, down in [("0006_search_projection", False), ("0005_evidence", True),
                               ("0006_search_projection", False)]:
            migrate(engine, migration, revision, down=down)
            assert current_heads(engine) == (revision,)
            # Fact rows and fact schema are identical in every segment of the
            # roundtrip: 0005 -> 0006 -> 0005 -> 0006.
            assert fact_rows(engine) == facts
            assert fact_schema(engine) == schema
            assert fact_table_names(engine) == FACT_TABLES
            assert_fact_schema_matches_orm(engine)
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            if down:
                assert projection_objects(engine) == set()
                assert set(sa.inspect(engine).get_table_names()) == FACT_TABLES | {"alembic_version"}
            else:
                assert projection_objects(engine) == (
                    PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine)))
                # The migration seeds nothing: the projection starts empty.
                with engine.connect() as connection:
                    assert connection.scalar(
                        sa.select(sa.func.count()).select_from(SearchDocumentModel)) == 0
                    assert connection.exec_driver_sql(
                        f"SELECT count(*) FROM {SEARCH_DOCUMENT_FTS}").scalar() == 0
        assert current_heads(engine) == ("0006_search_projection",)
        assert fact_rows(engine) == facts and fact_schema(engine) == schema
    finally:
        engine.dispose()


def test_failed_0006_after_fts_ddl_rolls_back_ddl_data_and_version(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0005_evidence")
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        tables_before = sqlite_master_names(engine)

        # Inject after the FTS5 virtual table DDL has been emitted, so the
        # failure lands mid-migration with a partially created projection.
        # op.execute() reaches the dialect implementation, which is the final
        # execution point for every migration statement in both online and
        # offline mode.
        from alembic import util
        from alembic.ddl.impl import DefaultImpl
        original_exec = DefaultImpl._exec
        observed = []

        def failing_exec(self, construct, execution_options=None, multiparams=None, params=None):
            result = original_exec(self, construct, execution_options, multiparams,
                                   util.immutabledict() if params is None else params)
            if FTS_CREATE in str(construct):
                observed.append(str(construct))
                raise RuntimeError("controlled failure after FTS5 DDL")
            return result

        monkeypatch.setattr(DefaultImpl, "_exec", failing_exec)
        with pytest.raises(RuntimeError, match="controlled failure after FTS5 DDL"):
            migrate(engine, migration, "0006_search_projection")
        monkeypatch.setattr(DefaultImpl, "_exec", original_exec)

        assert observed == [FTS_COLUMNS_DDL]
        assert current_heads(engine) == ("0005_evidence",)
        assert {name for name in sqlite_master_names(engine) if "search_document" in name} == set()
        assert projection_objects(engine) == set()
        assert sqlite_master_names(engine) == tables_before
        assert fact_state(engine) == before
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        # The identical migration then succeeds unchanged.
        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        assert fact_state(engine) == before
        assert projection_objects(engine) == PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
        assert fact_table_names(engine) == FACT_TABLES
    finally:
        engine.dispose()


def test_offline_sql_renders_fts5_ddl_and_executes_on_real_sqlite(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)

    def offline_script(revision, *, down=False):
        output = StringIO()
        migration.output_buffer = output
        (command.downgrade if down else command.upgrade)(migration, revision, sql=True)
        return output.getvalue()

    def execute_script(sql):
        connection = engine.raw_connection()
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            connection.executescript(sql)
        finally:
            connection.close()

    try:
        upgrade_sql = offline_script("head")
        # Offline rendering never connects to or creates a database file.
        assert sa.inspect(engine).get_table_names() == []
        assert FTS_CREATE in upgrade_sql
        assert "CREATE VIRTUAL TABLE search_document_fts USING fts5(title_tokens, body_tokens)" in upgrade_sql
        assert f"CREATE TABLE {SEARCH_DOCUMENT} " in upgrade_sql
        assert "CONSTRAINT uq_search_document_workspace_entity UNIQUE " \
               "(workspace_id, entity_type, entity_id)" in upgrade_sql
        assert "CREATE INDEX ix_search_document_workspace_type" in upgrade_sql
        assert "CREATE INDEX ix_search_document_workspace_deleted" in upgrade_sql

        execute_script(upgrade_sql)
        # The script renders the whole chain, so this is the current head: the
        # projection plus the analysis ledger that 0007 adds and the memory table
        # that 0008 adds on top of it.
        assert current_heads(engine) == ("0008_memory",)
        assert fact_table_names(engine) == FACT_TABLES
        assert projection_objects(engine) == PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
        assert analysis_objects(engine) == ANALYSIS_TABLES
        assert memory_objects(engine) == MEMORY_TABLES
        assert "CREATE TABLE analysis_run " in upgrade_sql
        assert "CREATE TABLE memory " in upgrade_sql
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_DOCUMENT_FTS}(title_tokens, body_tokens) VALUES ('alpha', 'beta')")

        # The database is at 0008 now, so the offline range starts there and the
        # script drops the memory table, then the analysis ledger, then the
        # projection objects.
        downgrade_sql = offline_script("0008_memory:0005_evidence", down=True)
        assert FTS_DROP in downgrade_sql
        assert "DROP INDEX ix_search_document_workspace_deleted" in downgrade_sql
        assert f"DROP TABLE {SEARCH_DOCUMENT}" in downgrade_sql
        for name in sorted(MEMORY_TABLES):
            assert f"DROP TABLE {name}" in downgrade_sql
        for name in sorted(ANALYSIS_TABLES):
            assert f"DROP TABLE {name}" in downgrade_sql
        assert downgrade_sql.index("DROP TABLE memory") < \
            downgrade_sql.index("DROP TABLE analysis_run")
        execute_script(downgrade_sql)
        assert current_heads(engine) == ("0005_evidence",)
        assert {name for name in sqlite_master_names(engine) if "search_document" in name} == set()
        assert sqlite_master_names(engine) & ANALYSIS_TABLES == set()
        assert sqlite_master_names(engine) & MEMORY_TABLES == set()
        assert fact_table_names(engine) == FACT_TABLES
        assert fact_state(engine) == before

        reupgrade_sql = offline_script("0005_evidence:0006_search_projection")
        assert FTS_CREATE in reupgrade_sql
        execute_script(reupgrade_sql)
        assert current_heads(engine) == ("0006_search_projection",)
        assert fact_state(engine) == before
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_DOCUMENT_FTS}(title_tokens, body_tokens) VALUES ('rebuilt', 'index')")
        with engine.connect() as connection:
            assert [row[0] for row in fts_query(connection, "rebuilt")] == [1]
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_postgresql_compiles_projection_orm_and_migration_is_sqlite_only():
    load_models()
    search_document = Base.metadata.tables[SEARCH_DOCUMENT]
    ddl = str(CreateTable(search_document).compile(dialect=postgresql.dialect()))
    assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT pk_search_document PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT uq_search_document_workspace_entity UNIQUE " \
           "(workspace_id, entity_type, entity_id)" in ddl
    assert "CONSTRAINT ck_search_document_entity_type_valid CHECK " \
           "(entity_type IN ('PERSON','ALIAS','EVENT','EVIDENCE'))" in ddl
    for index in search_document.indexes:
        assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    # Every pre-existing fact model still compiles for PostgreSQL.
    for name in sorted(FACT_TABLES):
        table = Base.metadata.tables[name]
        assert f"CREATE TABLE {name} " in str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    # FTS5 is SQLite-only infrastructure and is NOT portable to PostgreSQL:
    # PostgreSQL has no FTS5. The migration states it, and the ORM metadata
    # proves it by modelling the projection as one ordinary table.
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    module = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
                   for node in ast.walk(module))
    assert "UTCDateTime" not in source
    assert "create_all" not in source
    namespace = {}
    exec(compile(module, str(MIGRATION_PATH), "exec"), namespace)
    assert namespace["revision"] == "0006_search_projection"
    assert namespace["down_revision"] == "0005_evidence"
    assert namespace["branch_labels"] is None and namespace["depends_on"] is None
    assert namespace["FTS_COLUMNS"] == ("title_tokens", "body_tokens")
    # The SQLite-only DDL exists exactly once, as a raw SQL string, and the
    # migration module declares no ORM/SQLAlchemy FTS5 construct at all.
    assert source.count("CREATE VIRTUAL TABLE") == 1
    assert source.count("DROP TABLE IF EXISTS") == 1
    assert "search_document_fts" in source and "fts5" in source
    assert "Index(" not in source and "Table(" not in source
    # FTS5 DDL goes through op.execute(), which both the online and the offline
    # path can render; get_bind().exec_driver_sql() would break offline --sql.
    # The only raw-driver call left is the PRAGMA compile check, which never
    # runs offline.
    assert 'exec_driver_sql("PRAGMA compile_options")' in source
    # Statement-level view: exactly one PRAGMA check plus the two FTS5 statements
    # use raw execution, and no other statement bypasses the migration dialect.
    raw_calls = [node for node in ast.walk(module)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in ("exec_driver_sql", "execute")]
    assert {node.func.attr for node in raw_calls} == {"exec_driver_sql", "execute"}
    assert sum(node.func.attr == "exec_driver_sql" for node in raw_calls) == 1
    assert sum(node.func.attr == "execute" for node in raw_calls) == 2
    emitted = []
    for node in raw_calls:
        if node.func.attr != "execute":
            continue
        argument = node.args[0]
        assert isinstance(argument, (ast.Constant, ast.JoinedStr))
        if isinstance(argument, ast.Constant):
            emitted.append(argument.value)
        else:
            # Rebuild the f-string with the frozen column names substituted.
            emitted.append("".join(
                part.value if isinstance(part, ast.Constant) else "<columns>"
                for part in argument.values).replace(
                    "<columns>", ", ".join(namespace["FTS_COLUMNS"])))
    assert emitted == [FTS_COLUMNS_DDL, FTS_DROP]
    assert {
        constraint.name for constraint in Base.metadata.tables[SEARCH_DOCUMENT].constraints
    } <= {"pk_search_document", "uq_search_document_workspace_entity",
          "ck_search_document_entity_type_valid"}

    # Offline rendering for a PostgreSQL URL is not exercised here: migration
    # statements are text, and the FTS5 DDL is emitted verbatim by
    # ``op.execute`` for every dialect (online or offline). The portability
    # claim is therefore made by the ORM/metadata layer, which carries the FTS5
    # index as infrastructure - the two assertions below prove that the mapped
    # projection is one ordinary table and that no ORM construct can ever put
    # FTS5 into generated PostgreSQL DDL.
    assert f"CREATE VIRTUAL TABLE {SEARCH_DOCUMENT_FTS}" not in ddl
    assert all("fts5" not in str(CreateIndex(index).compile(dialect=postgresql.dialect()))
               for index in search_document.indexes)
    assert SEARCH_DOCUMENT_FTS not in Base.metadata.tables
    for name in sorted(FACT_TABLES):
        assert f"CREATE TABLE {name} " in str(
            CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
