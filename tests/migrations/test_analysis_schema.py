"""Phase 8 analysis ledger: real SQLite, offline SQL and PostgreSQL DDL.

Revision ``0007_analysis`` adds a run ledger (``analysis_run``) plus four
provenance tables (``analysis_*_source``). It adds no business fact table: the
eleven fact tables of 0001-0005 keep their exact rows and constraints, and 0006's
search projection is untouched. Every assertion below therefore separates three
groups:

* the eleven business FACT tables, frozen;
* the Phase 7 search PROJECTION, frozen;
* the Phase 8 ANALYSIS ledger, added (and removable) on top of both.

Phase 9 (revision ``0008_memory``) adds the ``memory`` table at the head, so the
head/table-set assertions below name ``0008_memory`` and the complete-schema sets
add the memory table; every 0007-specific assertion stays exactly as it was.

PostgreSQL is verified by DDL compilation only. No PostgreSQL server is started
and no PostgreSQL deployment is claimed.
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
from app.storage.database import Base, create_sqlite_engine
from app.storage.migrations import alembic_config, current_heads
from app.storage.models import (
    AnalysisEventSourceModel, AnalysisEvidenceSourceModel, AnalysisPersonSourceModel,
    AnalysisRelationshipSourceModel, AnalysisRunModel, EvidenceModel, EventModel,
    PersonModel, RelationshipModel, WorkspaceModel,
)
from tests.migrations.schema_facts import (
    ANALYSIS_RUN, ANALYSIS_SOURCE_TABLES, ANALYSIS_SOURCE_TARGETS, ANALYSIS_TABLES,
    FACT_TABLES, MEMORY_TABLES, NOW, PROJECTION_OBJECTS, PROJECTION_TABLES, SEARCH_DOCUMENT,
    SEARCH_DOCUMENT_FTS, expected_head_tables, fts_shadow_tables,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0007_analysis.py"
# The four analysis tables carry no ON DELETE clause, i.e. NO ACTION: a source row
# always blocks deleting the run (and the fact) it points at.
SOURCE_MODELS = (AnalysisPersonSourceModel, AnalysisRelationshipSourceModel,
                 AnalysisEventSourceModel, AnalysisEvidenceSourceModel)
TARGET_TYPES = ("PERSON", "RELATIONSHIP", "EVENT")
ANALYSIS_TYPES = ("PERSON_PROFILE", "RELATIONSHIP_ANALYSIS", "EVENT_ANALYSIS")
STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")


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
    config = AppConfig(data_dir=tmp_path / "phase8 analysis 数据库")
    return create_sqlite_engine(config), alembic_config(config)


def insert(connection, model, **values):
    connection.execute(model.__table__.insert().values(**values))


def seed_facts(connection):
    """One complete row in each of the eleven business fact tables."""
    scope, other = uuid4(), uuid4()
    for identifier in (scope, other):
        insert(connection, WorkspaceModel, id=identifier, name=str(identifier),
               description="保留工作区", created_at=NOW, updated_at=NOW)
    person_a, person_b = sorted((uuid4(), uuid4()))
    for identifier, name in ((person_a, "保留人物 A"), (person_b, "保留人物 B")):
        insert(connection, PersonModel, id=identifier, workspace_id=scope, display_name=name,
               real_name=None, notes="保留说明", created_at=NOW, updated_at=NOW)
    relationship = uuid4()
    insert(connection, RelationshipModel, id=relationship, workspace_id=scope,
           person_a_id=person_a, person_b_id=person_b, relationship_type="Friend",
           directionality="UNDIRECTED", created_at=NOW, updated_at=NOW)
    event = uuid4()
    insert(connection, EventModel, id=event, workspace_id=scope, title="保留事件",
           event_type="Meeting", record_kind="FACT", occurred_on=NOW.date(),
           started_at=None, ended_at=None, created_at=NOW, updated_at=NOW)
    evidence = uuid4()
    insert(connection, EvidenceModel, id=evidence, workspace_id=scope, event_id=event, kind="TEXT",
           title="保留证据", content_text="原始正文", annotation=None, source_type="MANUAL",
           source_label=None, source_reference="原始引用", captured_at=None,
           created_at=NOW, updated_at=NOW, deleted_at=None)
    return {"scope": scope, "other": other, "person_a": person_a, "person_b": person_b,
            "relationship": relationship, "event": event, "evidence": evidence}


def analysis_row(workspace_id, **overrides):
    """A fully valid analysis_run row; overrides make exactly one field invalid."""
    values = dict(id=uuid4(), workspace_id=workspace_id, target_type="PERSON",
                  target_id=uuid4(), analysis_type="PERSON_PROFILE", provider_name="deepseek",
                  model_name="deepseek-flash", scope='{"as_of": null, "include": {"events": true}}',
                  status="PENDING", context_snapshot=None, result=None, error_message=None,
                  created_at=NOW, started_at=NOW, completed_at=None, duration_ms=None)
    values.update(overrides)
    return values


def add_run(connection, workspace_id, **overrides):
    values = analysis_row(workspace_id, **overrides)
    insert(connection, AnalysisRunModel, **values)
    return values


def add_source(connection, model, workspace_id, run_id, target_id, **overrides):
    values = dict(id=uuid4(), analysis_run_id=run_id, workspace_id=workspace_id,
                  target_id=target_id, created_at=NOW)
    values.update(overrides)
    insert(connection, model, **values)
    return values


def expect_integrity(engine, code, callback):
    with pytest.raises(IntegrityError) as error:
        with engine.begin() as connection:
            callback(connection)
    assert error.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_" + code


def fact_rows(engine):
    """Canonical rows of all eleven fact tables, keyed by table name."""
    snapshot = {}
    with engine.connect() as connection:
        for name in sorted(FACT_TABLES):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            rows = connection.execute(sa.select(table).order_by(table.c.id)).all()
            # Driver values (UUID, datetime) are coerced to stable text so the
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
    # Reflection allocates fresh type and SQL-expression objects on every call;
    # their representations are normalized to strings, exactly like the Phase 6
    # evidence and Phase 7 projection snapshots.
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


def master_snapshot(engine):
    """Every sqlite_master object with its kind and DDL, ignoring FTS5 internals.

    The FTS5 shadow tables carry engine-managed bookkeeping rows and a
    non-deterministic ``_idx`` blob, so the Phase 7 projection is compared through
    ``projection_objects()`` instead of through this raw snapshot.
    """
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").all()
    return [tuple(row) for row in rows if not str(row[1]).startswith(SEARCH_DOCUMENT_FTS)]


def projection_objects(engine):
    """Projection objects actually present, including the FTS5 shadow tables."""
    return (PROJECTION_OBJECTS & sqlite_master_names(engine)
            | fts_shadow_tables(raw_table_names(engine)))


def analysis_objects(engine):
    """Every sqlite_master object whose name belongs to the Phase 8 analysis ledger.

    Both the five tables and the seven indexes created by the migration carry an
    ``analysis`` prefix; SQLite's own ``sqlite_autoindex_...`` objects do not.
    """
    return {name for name in sqlite_master_names(engine) if name.startswith("analysis")}


def analysis_tables(engine):
    """Only the five analysis tables, without their secondary objects."""
    return raw_table_names(engine) & ANALYSIS_TABLES


def fact_table_names(engine):
    """Only the business fact tables: projection and analysis objects filtered out."""
    names = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
    return (names - PROJECTION_OBJECTS - fts_shadow_tables(names) - ANALYSIS_TABLES
            - MEMORY_TABLES)


def assert_schema_matches_orm(engine, names):
    """The migrated schema of ``names`` matches the frozen ORM metadata exactly."""
    Base.metadata.tables  # Metadata is registered by the caller's load_models()/import.
    inspector = sa.inspect(engine)
    for name in sorted(names):
        table = Base.metadata.tables[name]
        assert {column["name"] for column in inspector.get_columns(name)} == set(table.c.keys())
        assert {column["name"]: column["nullable"]
                for column in inspector.get_columns(name)} == {
            column.name: column.nullable for column in table.columns}
        assert inspector.get_pk_constraint(name)["name"] == table.primary_key.name
        for kind, reflected in [(sa.CheckConstraint, inspector.get_check_constraints(name)),
                                (sa.ForeignKeyConstraint, inspector.get_foreign_keys(name)),
                                (sa.UniqueConstraint, inspector.get_unique_constraints(name))]:
            assert {item["name"] for item in reflected} == {
                item.name for item in table.constraints if isinstance(item, kind)}
        assert {index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(name)} == {
            index.name: tuple(column.name for column in index.columns)
            for index in table.indexes}


def test_0006_to_0007_adds_the_analysis_ledger_and_keeps_every_fact_table(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0006_search_projection")
        assert current_heads(engine) == ("0006_search_projection",)
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        assert analysis_objects(engine) == set()
        assert fact_table_names(engine) == FACT_TABLES

        migrate(engine, migration, "0007_analysis")
        assert current_heads(engine) == ("0007_analysis",)
        # The eleven business fact tables keep their rows and their whole schema.
        assert fact_state(engine) == before
        assert fact_table_names(engine) == FACT_TABLES
        assert len(fact_table_names(engine)) == 11
        # The analysis ledger is a third group: never a fact table, never a projection.
        assert ANALYSIS_TABLES.isdisjoint(FACT_TABLES)
        assert ANALYSIS_TABLES.isdisjoint(PROJECTION_TABLES)
        assert analysis_tables(engine) == ANALYSIS_TABLES
        assert projection_objects(engine) == (
            PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine)))
        # The complete schema is the fact tables plus the projection objects (the
        # FTS5 virtual table and the five shadow tables SQLite creates for it) plus
        # the five analysis tables. Revision 0007 predates 0008's memory table, so
        # that one is excluded here and asserted at the head further down.
        assert set(sa.inspect(engine).get_table_names()) == expected_head_tables(
            raw_table_names(engine)) - MEMORY_TABLES
        assert raw_table_names(engine) & MEMORY_TABLES == set()
        assert_schema_matches_orm(engine, FACT_TABLES | ANALYSIS_TABLES)

        # A fresh process must reflect the same analysis ledger from disk.
        engine.dispose()
        reopened = create_sqlite_engine(AppConfig(data_dir=tmp_path / "phase8 analysis 数据库"))
        try:
            assert current_heads(reopened) == ("0007_analysis",)
            assert fact_state(reopened) == before
            assert fact_table_names(reopened) == FACT_TABLES
            assert analysis_tables(reopened) == ANALYSIS_TABLES
            assert projection_objects(reopened) == projection_objects(engine)
            assert set(sa.inspect(reopened).get_table_names()) == expected_head_tables(
                raw_table_names(reopened)) - MEMORY_TABLES
            assert_schema_matches_orm(reopened, FACT_TABLES | ANALYSIS_TABLES)
        finally:
            reopened.dispose()
    finally:
        engine.dispose()


def test_analysis_run_schema_matches_orm_and_applies_its_constraints(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        assert current_heads(engine) == ("0008_memory",)
        table = AnalysisRunModel.__table__
        assert table.primary_key.name == "pk_analysis_run"
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint(ANALYSIS_RUN) == {
            "constrained_columns": ["id"], "name": "pk_analysis_run"}
        assert {column["name"] for column in inspector.get_columns(ANALYSIS_RUN)} == set(table.c.keys())
        assert [check["name"] for check in inspector.get_check_constraints(ANALYSIS_RUN)] == [
            "ck_analysis_run_analysis_type_valid", "ck_analysis_run_duration_nonnegative",
            "ck_analysis_run_status_valid", "ck_analysis_run_target_type_valid"]
        assert inspector.get_unique_constraints(ANALYSIS_RUN) == [
            {"name": "uq_analysis_run_workspace_id_id", "column_names": ["workspace_id", "id"]}]
        assert inspector.get_foreign_keys(ANALYSIS_RUN) == [
            {"name": "fk_analysis_run_workspace_id_workspace",
             "constrained_columns": ["workspace_id"],
             "referred_table": "workspace", "referred_columns": ["id"],
             "referred_schema": None, "options": {}}]
        assert {index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(ANALYSIS_RUN)} == {
            "ix_analysis_run_workspace_created": ("workspace_id", "created_at"),
            "ix_analysis_run_workspace_status": ("workspace_id", "status"),
            "ix_analysis_run_workspace_target": ("workspace_id", "target_type", "target_id")}
        # The ledger persists no credential of any kind.
        forbidden = ("key", "token", "secret", "password", "credential")
        assert not [name for name in table.c.keys()
                    if any(part in name.lower() for part in forbidden)]

        with engine.begin() as connection:
            scope = seed_facts(connection)["scope"]
            for target_type in TARGET_TYPES:
                for analysis_type in ANALYSIS_TYPES:
                    add_run(connection, scope, id=uuid4(), target_type=target_type,
                            analysis_type=analysis_type)
            for status in STATUSES:
                add_run(connection, scope, id=uuid4(), status=status)
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(AnalysisRunModel)) == len(TARGET_TYPES) * len(
                ANALYSIS_TYPES) + len(STATUSES)

        # CHECK(target_type IN ...) rejects every value outside the frozen set.
        for target_type in ("UNKNOWN", "person", "Person", "", " PERSON", "PEOPLE"):
            expect_integrity(engine, "CHECK", lambda c, value=target_type: add_run(
                c, uuid4(), target_type=value))
        for analysis_type in ("SUMMARY", "person_profile", "", "PROFILE"):
            expect_integrity(engine, "CHECK", lambda c, value=analysis_type: add_run(
                c, uuid4(), analysis_type=value))
        for status in ("DONE", "pending", "", "RUNNING "):
            expect_integrity(engine, "CHECK", lambda c, value=status: add_run(
                c, uuid4(), status=value))
        # CHECK(duration_ms IS NULL OR duration_ms >= 0)
        for duration in (-1, -1000):
            expect_integrity(engine, "CHECK", lambda c, value=duration: add_run(
                c, uuid4(), duration_ms=value))
        # NULL and zero durations are legal: an unfinished or instant run.
        with engine.begin() as connection:
            scope = connection.scalar(sa.select(AnalysisRunModel.workspace_id).limit(1))
            add_run(connection, scope, duration_ms=None, completed_at=None,
                    context_snapshot=None, result=None, error_message=None)
            add_run(connection, scope, duration_ms=0, completed_at=NOW,
                    context_snapshot='{"sent": true}', result="原始回答",
                    error_message=None)
        # The run belongs to an existing workspace; a cross-workspace or unknown
        # workspace_id is rejected by the foreign key.
        scope, other = sorted((uuid4(), uuid4()))
        with engine.begin() as connection:
            for identifier in (scope, other):
                insert(connection, WorkspaceModel, id=identifier, name=str(identifier),
                       description=None, created_at=NOW, updated_at=NOW)
            add_run(connection, scope)
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_run(c, uuid4()))
        # Every required column refuses NULL.
        for missing in ("workspace_id", "target_type", "target_id", "analysis_type",
                        "provider_name", "model_name", "scope", "status",
                        "created_at", "started_at"):
            values = analysis_row(scope)
            values[missing] = None
            expect_integrity(engine, "NOTNULL", lambda c, row=values: c.execute(
                AnalysisRunModel.__table__.insert().values(**row)))
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_every_source_table_has_the_same_shape_over_its_own_fact_target(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        inspector = sa.inspect(engine)
        for model in SOURCE_MODELS:
            name = model.__tablename__
            target = ANALYSIS_SOURCE_TARGETS[name]
            table = model.__table__
            assert {column["name"] for column in inspector.get_columns(name)} == set(table.c.keys())
            assert inspector.get_pk_constraint(name) == {
                "constrained_columns": ["id"], "name": f"pk_{name}"}
            assert {constraint["name"] for constraint in inspector.get_check_constraints(name)} == set()
            assert inspector.get_unique_constraints(name) == [
                {"name": f"uq_{name}_run_target",
                 "column_names": ["analysis_run_id", "target_id"]}]
            assert {constraint["name"]: (tuple(constraint["constrained_columns"]),
                                        constraint["referred_table"],
                                        tuple(constraint["referred_columns"]))
                    for constraint in inspector.get_foreign_keys(name)} == {
                f"fk_{name}_workspace_run": (("workspace_id", "analysis_run_id"),
                                             ANALYSIS_RUN, ("workspace_id", "id")),
                f"fk_{name}_workspace_{target}": (("workspace_id", "target_id"),
                                                  target, ("workspace_id", "id"))}
            assert {index["name"]: tuple(index["column_names"])
                    for index in inspector.get_indexes(name)} == {
                f"ix_{name}_workspace_run": ("workspace_id", "analysis_run_id"),
                f"ix_{name}_workspace_target": ("workspace_id", "target_id")}
            # Every composite foreign key target must really be UNIQUE(workspace_id, id).
            assert ("workspace_id", "id") in {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(target)}
        assert_schema_matches_orm(engine, ANALYSIS_SOURCE_TABLES)
        # The four provenance tables are structurally identical apart from the
        # fact table they point at.
        shapes = {name: (tuple(column["name"] for column in inspector.get_columns(name)),
                         tuple(sorted(index["name"] for index in inspector.get_indexes(name))))
                  for name in sorted(ANALYSIS_SOURCE_TABLES)}
        assert len({columns for columns, _ in shapes.values()}) == 1
        assert len(set(shapes)) == 4
    finally:
        engine.dispose()


def test_source_rows_reject_unknown_cross_workspace_and_duplicate_targets(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        with engine.begin() as connection:
            facts = seed_facts(connection)
            run = add_run(connection, facts["scope"])["id"]
            add_source(connection, AnalysisPersonSourceModel, facts["scope"], run,
                       facts["person_a"])
            add_source(connection, AnalysisRelationshipSourceModel, facts["scope"], run,
                       facts["relationship"])
            add_source(connection, AnalysisEventSourceModel, facts["scope"], run, facts["event"])
            add_source(connection, AnalysisEvidenceSourceModel, facts["scope"], run,
                       facts["evidence"])
        scope, other = facts["scope"], facts["other"]
        assert scope != other

        # An unknown target id is rejected for every source table.
        for model in SOURCE_MODELS:
            expect_integrity(engine, "FOREIGNKEY", lambda c, m=model: add_source(
                c, m, scope, run, uuid4()))
        # A target that exists but belongs to another workspace must be rejected for
        # every source table: the composite key includes workspace_id.
        with engine.begin() as connection:
            foreign_person = uuid4()
            insert(connection, PersonModel, id=foreign_person, workspace_id=other,
                   display_name="其他工作区人物", real_name=None, notes=None,
                   created_at=NOW, updated_at=NOW)
            foreign_event = uuid4()
            insert(connection, EventModel, id=foreign_event, workspace_id=other, title="其他事件",
                   event_type="Meeting", record_kind="FACT", occurred_on=NOW.date(),
                   started_at=None, ended_at=None, created_at=NOW, updated_at=NOW)
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_source(
            c, AnalysisPersonSourceModel, scope, run, foreign_person))
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_source(
            c, AnalysisEventSourceModel, scope, run, foreign_event))
        # The run must exist, and it must belong to the same workspace as the source
        # row. These cases isolate the two composite keys instead of tripping UNIQUE
        # first:
        #  * a run that lives in another workspace is not reachable from this one;
        #  * an unknown run id is not reachable at all.
        with engine.begin() as connection:
            other_run = add_run(connection, other)["id"]
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_source(
            c, AnalysisPersonSourceModel, other, run, foreign_person))
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_source(
            c, AnalysisPersonSourceModel, scope, other_run, facts["person_a"]))
        expect_integrity(engine, "FOREIGNKEY", lambda c: add_source(
            c, AnalysisPersonSourceModel, scope, uuid4(), facts["person_a"]))
        # UNIQUE(analysis_run_id, target_id) rejects the same fact twice in one run,
        # while another run may legitimately consume the very same fact.
        expect_integrity(engine, "UNIQUE", lambda c: add_source(
            c, AnalysisPersonSourceModel, scope, run, facts["person_a"]))
        with engine.begin() as connection:
            second = add_run(connection, scope)["id"]
            add_source(connection, AnalysisPersonSourceModel, scope, second, facts["person_a"])
            # The same fact id may feed another source table of the same run: the
            # unique constraint is per run and per fact, not global.
            add_source(connection, AnalysisPersonSourceModel, scope, second, facts["person_b"])
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_deleting_a_consumed_fact_or_its_run_is_blocked_without_cascade(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        with engine.begin() as connection:
            facts = seed_facts(connection)
            run = add_run(connection, facts["scope"])["id"]
            add_source(connection, AnalysisPersonSourceModel, facts["scope"], run,
                       facts["person_a"])
            add_source(connection, AnalysisEventSourceModel, facts["scope"], run, facts["event"])
        # An unrelated fact row and an unrelated run that nothing references.
        unused = uuid4()
        with engine.begin() as connection:
            insert(connection, PersonModel, id=unused, workspace_id=facts["scope"],
                   display_name="未被引用人物", real_name=None, notes=None,
                   created_at=NOW, updated_at=NOW)
            idle_run = add_run(connection, facts["scope"])["id"]
        # The run has no ON DELETE clause on its source tables (NO ACTION), so the
        # database -- not only the application -- refuses to delete it.
        expect_integrity(engine, "FOREIGNKEY", lambda c: c.exec_driver_sql(
            "DELETE FROM analysis_run WHERE id = ?", (run.hex,)))
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                AnalysisRunModel).where(AnalysisRunModel.id == run)) == 1
        # A fact a run consumed is protected the same way.
        expect_integrity(engine, "FOREIGNKEY", lambda c: c.exec_driver_sql(
            "DELETE FROM person WHERE id = ?", (facts["person_a"].hex,)))
        expect_integrity(engine, "FOREIGNKEY", lambda c: c.exec_driver_sql(
            "DELETE FROM event WHERE id = ?", (facts["event"].hex,)))
        # The block comes from the source row, not from the table: the very same
        # deletes succeed for a fact and a run that no source row references.
        with engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM person WHERE id = ?", (unused.hex,))
            connection.exec_driver_sql("DELETE FROM analysis_run WHERE id = ?", (idle_run.hex,))
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                AnalysisRunModel).where(AnalysisRunModel.id == idle_run)) == 0
            # The referenced run and its two source rows survive untouched.
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                AnalysisRunModel).where(AnalysisRunModel.id == run)) == 1
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                AnalysisPersonSourceModel).where(
                AnalysisPersonSourceModel.analysis_run_id == run)) == 1
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                AnalysisEventSourceModel).where(
                AnalysisEventSourceModel.analysis_run_id == run)) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        # Dropping the ledger tables themselves never rewrites a fact row.
        with engine.begin() as connection:
            facts_before = connection.execute(sa.select(
                PersonModel.id, PersonModel.display_name).where(
                PersonModel.workspace_id == facts["scope"]).order_by(PersonModel.id)).all()
        migrate(engine, migration, "0006_search_projection", down=True)
        assert analysis_objects(engine) == set()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        migrate(engine, migration, "0007_analysis")
        with engine.connect() as connection:
            assert connection.execute(sa.select(
                PersonModel.id, PersonModel.display_name).where(
                PersonModel.workspace_id == facts["scope"]).order_by(PersonModel.id)).all() == \
                facts_before
    finally:
        engine.dispose()


def test_0007_to_0006_drops_only_the_analysis_ledger_and_keeps_every_fact_row(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0006_search_projection")
        with engine.begin() as connection:
            facts = seed_facts(connection)
        before = fact_state(engine)
        projection_before = projection_objects(engine)
        assert analysis_objects(engine) == set()

        migrate(engine, migration, "0007_analysis")
        with engine.begin() as connection:
            run = add_run(connection, facts["scope"])["id"]
            add_source(connection, AnalysisPersonSourceModel, facts["scope"], run, facts["person_a"])
            add_source(connection, AnalysisEventSourceModel, facts["scope"], run, facts["event"])
        assert fact_state(engine) == before
        assert analysis_objects(engine) == ANALYSIS_TABLES
        # 0006's projection, including the FTS5 index, is not touched by 0007.
        assert projection_objects(engine) == projection_before

        migrate(engine, migration, "0006_search_projection", down=True)
        assert current_heads(engine) == ("0006_search_projection",)
        # Not one %analysis% object is left, in any form.
        assert analysis_objects(engine) == set()
        assert {name for name in sqlite_master_names(engine) if "analysis" in name} == set()
        assert raw_table_names(engine) & ANALYSIS_TABLES == set()
        for name in sorted(ANALYSIS_TABLES):
            with pytest.raises(sa.exc.NoSuchTableError):
                sa.inspect(engine).get_indexes(name)
        # The eleven fact tables keep every row and their whole named schema.
        assert fact_table_names(engine) == FACT_TABLES
        assert set(sa.inspect(engine).get_table_names()) == (
            FACT_TABLES | PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
            | {"alembic_version"})
        assert fact_rows(engine) == before[0]
        assert fact_schema(engine) == before[1]
        assert projection_objects(engine) == projection_before
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM search_document").scalar() == 0
    finally:
        engine.dispose()


def test_0006_0007_0006_0007_roundtrip_keeps_facts_and_leaves_the_ledger_empty(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0006_search_projection")
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        for revision, down in [("0007_analysis", False), ("0006_search_projection", True),
                               ("0007_analysis", False)]:
            migrate(engine, migration, revision, down=down)
            assert current_heads(engine) == (revision,)
            # Fact rows and fact schema are identical in every segment of the
            # roundtrip: 0006 -> 0007 -> 0006 -> 0007.
            assert fact_rows(engine) == before[0]
            assert fact_schema(engine) == before[1]
            assert fact_table_names(engine) == FACT_TABLES
            assert_schema_matches_orm(engine, FACT_TABLES)
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            if down:
                assert analysis_objects(engine) == set()
            else:
                assert analysis_objects(engine) == ANALYSIS_TABLES
                # The migration seeds nothing: the ledger starts empty.
                with engine.connect() as connection:
                    for model in (AnalysisRunModel, *SOURCE_MODELS):
                        assert connection.scalar(
                            sa.select(sa.func.count()).select_from(model)) == 0
        assert current_heads(engine) == ("0007_analysis",)
        assert fact_rows(engine) == before[0] and fact_schema(engine) == before[1]
    finally:
        engine.dispose()


def test_failed_0007_after_the_last_source_table_rolls_back_ddl_data_and_version(tmp_path, monkeypatch):
    engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0006_search_projection")
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)
        master_before = master_snapshot(engine)
        projection_before = projection_objects(engine)

        # The failure lands after all five tables exist and their indexes are being
        # built, i.e. after the last source table has been created.
        from alembic import util
        from alembic.ddl.impl import DefaultImpl
        original_exec = DefaultImpl._exec
        observed = []
        trigger = ("CREATE INDEX ix_analysis_person_source_workspace_target "
                   "ON analysis_person_source (workspace_id, target_id)")

        def failing_exec(self, construct, execution_options=None, multiparams=None, params=None):
            if trigger in str(construct):
                observed.append(str(construct))
                raise RuntimeError("controlled failure after the last analysis table")
            return original_exec(self, construct, execution_options, multiparams,
                                 util.immutabledict() if params is None else params)

        monkeypatch.setattr(DefaultImpl, "_exec", failing_exec)
        with pytest.raises(RuntimeError, match="controlled failure after the last analysis table"):
            migrate(engine, migration, "0007_analysis")
        monkeypatch.setattr(DefaultImpl, "_exec", original_exec)

        assert observed == [trigger]
        assert current_heads(engine) == ("0006_search_projection",)
        assert analysis_objects(engine) == set()
        assert analysis_tables(engine) == set()
        # Not one residual object: every non-FTS5 sqlite_master row (table, index or
        # anything else) is byte-identical to the pre-migration snapshot.
        assert master_snapshot(engine) == master_before
        assert projection_objects(engine) == projection_before
        assert fact_state(engine) == before
        assert set(sa.inspect(engine).get_table_names()) == (
            FACT_TABLES | PROJECTION_OBJECTS | fts_shadow_tables(raw_table_names(engine))
            | {"alembic_version"})
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        # The identical migration then succeeds unchanged.
        migrate(engine, migration, "0007_analysis")
        assert current_heads(engine) == ("0007_analysis",)
        assert analysis_objects(engine) == ANALYSIS_TABLES
        assert fact_state(engine) == before
        assert fact_table_names(engine) == FACT_TABLES
    finally:
        engine.dispose()


def test_offline_sql_renders_the_analysis_ddl_and_executes_on_real_sqlite(tmp_path, monkeypatch):
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
        upgrade_sql = offline_script("0006_search_projection:0007_analysis")
        # Offline rendering never connects to or creates a database file.
        assert sa.inspect(engine).get_table_names() == []
        assert f"CREATE TABLE {ANALYSIS_RUN} " in upgrade_sql
        for name in sorted(ANALYSIS_SOURCE_TABLES):
            assert f"CREATE TABLE {name} " in upgrade_sql
            assert f"CONSTRAINT fk_{name}_workspace_run FOREIGN KEY" \
                   "(workspace_id, analysis_run_id) REFERENCES analysis_run " \
                   "(workspace_id, id)" in upgrade_sql
            assert f"CONSTRAINT uq_{name}_run_target UNIQUE (analysis_run_id, target_id)" \
                   in upgrade_sql
            assert f"CREATE INDEX ix_{name}_workspace_run" in upgrade_sql
            assert f"CREATE INDEX ix_{name}_workspace_target" in upgrade_sql
        assert "CONSTRAINT uq_analysis_run_workspace_id_id UNIQUE (workspace_id, id)" in upgrade_sql
        assert "CONSTRAINT ck_analysis_run_target_type_valid CHECK " \
               "(target_type IN ('PERSON','RELATIONSHIP','EVENT'))" in upgrade_sql
        assert "CONSTRAINT ck_analysis_run_analysis_type_valid CHECK " \
               "(analysis_type IN ('PERSON_PROFILE','RELATIONSHIP_ANALYSIS','EVENT_ANALYSIS'))" \
               in upgrade_sql
        assert "CONSTRAINT ck_analysis_run_status_valid CHECK " \
               "(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED'))" in upgrade_sql
        assert "CONSTRAINT ck_analysis_run_duration_nonnegative CHECK " \
               "(duration_ms IS NULL OR duration_ms >= 0)" in upgrade_sql
        assert "CREATE INDEX ix_analysis_run_workspace_created" in upgrade_sql
        assert "CREATE INDEX ix_analysis_run_workspace_target" in upgrade_sql
        assert "CREATE INDEX ix_analysis_run_workspace_status" in upgrade_sql

        # The whole chain is rendered from base, so the offline script is a clean
        # install; executing it produces exactly the head schema, memory included.
        execute_script(offline_script("head"))
        assert current_heads(engine) == ("0008_memory",)
        assert analysis_tables(engine) == ANALYSIS_TABLES
        assert fact_table_names(engine) == FACT_TABLES
        assert set(sa.inspect(engine).get_table_names()) == expected_head_tables(
            raw_table_names(engine))
        assert raw_table_names(engine) & MEMORY_TABLES == MEMORY_TABLES
        with engine.begin() as connection:
            seed_facts(connection)
        before = fact_state(engine)

        # The database is at 0008 now, so the offline range starts there: the memory
        # table goes first, then the analysis ledger.
        downgrade_sql = offline_script("0008_memory:0006_search_projection", down=True)
        for name in sorted(MEMORY_TABLES):
            assert f"DROP TABLE {name}" in downgrade_sql
        for name in sorted(ANALYSIS_TABLES):
            assert f"DROP TABLE {name}" in downgrade_sql
        assert downgrade_sql.index("DROP TABLE memory") < \
               downgrade_sql.index("DROP TABLE analysis_person_source")
        assert downgrade_sql.index("DROP TABLE analysis_person_source") < \
               downgrade_sql.index("DROP TABLE analysis_run")
        execute_script(downgrade_sql)
        assert current_heads(engine) == ("0006_search_projection",)
        assert analysis_objects(engine) == set()
        assert {name for name in sqlite_master_names(engine) if "memory" in name} == set()
        assert fact_table_names(engine) == FACT_TABLES
        assert fact_state(engine) == before

        reupgrade_sql = offline_script("0006_search_projection:0008_memory")
        assert f"CREATE TABLE {ANALYSIS_RUN} " in reupgrade_sql
        assert "CREATE TABLE memory " in reupgrade_sql
        execute_script(reupgrade_sql)
        assert current_heads(engine) == ("0008_memory",)
        assert analysis_objects(engine) == ANALYSIS_TABLES
        assert raw_table_names(engine) & MEMORY_TABLES == MEMORY_TABLES
        assert fact_state(engine) == before
        with engine.connect() as connection:
            for model in (AnalysisRunModel, *SOURCE_MODELS):
                assert connection.scalar(sa.select(sa.func.count()).select_from(model)) == 0
            # The migration seeds nothing: the re-created memory table is empty too.
            assert connection.exec_driver_sql("SELECT count(*) FROM memory").scalar() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_postgresql_compiles_every_analysis_construct_without_claiming_a_deployment():
    run = AnalysisRunModel.__table__
    composed = sa.MetaData()
    sa.Table("workspace", composed, sa.Column("id", sa.Uuid(), primary_key=True))
    for name, target in sorted(ANALYSIS_SOURCE_TARGETS.items()):
        source = AnalysisRunModel.metadata.tables[name]
        sa.Table(target, composed, sa.Column("id", sa.Uuid(), primary_key=True),
                 sa.Column("workspace_id", sa.Uuid(), nullable=False))
        ddl = str(CreateTable(source).compile(dialect=postgresql.dialect()))
        assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
        assert f"CONSTRAINT pk_{name} PRIMARY KEY (id)" in ddl
        assert f"CONSTRAINT fk_{name}_workspace_run FOREIGN KEY(workspace_id, analysis_run_id) " \
               f"REFERENCES analysis_run (workspace_id, id)" in ddl
        assert f"CONSTRAINT fk_{name}_workspace_{target} FOREIGN KEY(workspace_id, target_id) " \
               f"REFERENCES {target} (workspace_id, id)" in ddl
        assert f"CONSTRAINT uq_{name}_run_target UNIQUE (analysis_run_id, target_id)" in ddl
        assert source.primary_key.name == f"pk_{name}"
        for index in source.indexes:
            assert "CREATE" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        # The source-to-target column really is a foreign key column, not a bare UUID.
        assert ("workspace_id", "target_id") in {
            tuple(constraint.column_keys) for constraint in source.foreign_key_constraints}

    ddl = str(CreateTable(run).compile(dialect=postgresql.dialect()))
    assert "UUID" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT pk_analysis_run PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT fk_analysis_run_workspace_id_workspace FOREIGN KEY(workspace_id) " \
           "REFERENCES workspace (id)" in ddl
    assert "CONSTRAINT uq_analysis_run_workspace_id_id UNIQUE (workspace_id, id)" in ddl
    assert "CONSTRAINT ck_analysis_run_target_type_valid CHECK " \
           "(target_type IN ('PERSON','RELATIONSHIP','EVENT'))" in ddl
    assert "CONSTRAINT ck_analysis_run_analysis_type_valid CHECK " \
           "(analysis_type IN ('PERSON_PROFILE','RELATIONSHIP_ANALYSIS','EVENT_ANALYSIS'))" in ddl
    assert "CONSTRAINT ck_analysis_run_status_valid CHECK " \
           "(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED'))" in ddl
    assert "CONSTRAINT ck_analysis_run_duration_nonnegative CHECK " \
           "(duration_ms IS NULL OR duration_ms >= 0)" in ddl
    # The ledger persists no credential column on either dialect.
    forbidden = ("key", "token", "secret", "password", "credential")
    assert not [name for name in run.c.keys()
                if any(part in name.lower() for part in forbidden)]
    for index in run.indexes:
        assert "CREATE" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    # The migration is frozen SQLAlchemy DDL: it imports no application module, uses
    # the SQLite-independent ``sa`` type spellings, never calls create_all, and
    # declares the revision chain exactly once.
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    module = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
                   for node in ast.walk(module))
    assert "UTCDateTime" not in source
    assert "create_all" not in source
    assert "fts5" not in source
    assert "Index(" not in source and "Table(" not in source
    namespace = {}
    exec(compile(module, str(MIGRATION_PATH), "exec"), namespace)
    assert namespace["revision"] == "0007_analysis"
    assert namespace["down_revision"] == "0006_search_projection"
    assert namespace["branch_labels"] is None and namespace["depends_on"] is None
    assert namespace["TARGET_TYPES"] == TARGET_TYPES
    assert namespace["ANALYSIS_TYPES"] == ANALYSIS_TYPES
    assert namespace["STATUSES"] == STATUSES
    # The frozen table list covers exactly the four provenance tables, each with the
    # fact table its composite foreign key points at.
    assert dict(namespace["SOURCE_TABLES"]) == ANALYSIS_SOURCE_TARGETS
    assert len(namespace["SOURCE_TABLES"]) == 4
    # No data is ever written by the migration: no INSERT/UPDATE/DELETE, and no
    # migration here may bypass the dialect with a raw statement.
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in ("execute", "exec_driver_sql")
                   for node in ast.walk(module))
    upper = source.upper()
    for statement in ("INSERT ", "UPDATE ", "DELETE ", "CREATE VIRTUAL TABLE"):
        assert statement not in upper
