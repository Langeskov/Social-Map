"""Preserve the empty initial revision and Phase 3 schema at the Phase 6 head."""
import subprocess
import sys

from alembic import command
import pytest
from sqlalchemy import inspect

from app.config.settings import AppConfig, PROJECT_ROOT, load_config
from app.services.database import DatabaseRuntime, initialize_database
from app.storage.database import Base, DatabaseError, create_sqlite_engine
from app.storage.migrations import alembic_config, current_heads, expected_heads


INITIAL_HEAD = ("0001_empty_initial",)
PHASE3_HEAD = ("0002_workspace_person_media",)
PROJECTION_HEAD = ("0006_search_projection",)
FORMAL_HEAD = ("0008_memory",)
PHASE3_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
PHASE5_TABLES = PHASE3_TABLES | {"relationship", "relationship_history", "event", "event_participant"}
EVIDENCE_TABLES = {"evidence", "evidence_media"}
# Revision 0006 and 0007 still have exactly these eleven business fact tables:
# neither adds a fact table and neither changes one.
FACT_TABLES = PHASE5_TABLES | EVIDENCE_TABLES
# Phase 7 projection infrastructure added on top of the fact tables: the ordinary
# projection table, the FTS5 virtual table and the five shadow tables SQLite
# materializes for the FTS5 index. These are never counted as business tables.
PROJECTION_TABLES = {"search_document", "search_document_fts"}
FTS_SHADOW_TABLES = {"search_document_fts_data", "search_document_fts_idx",
                     "search_document_fts_content", "search_document_fts_docsize",
                     "search_document_fts_config"}
# Phase 8 analysis ledger on top of both: the run table and four provenance tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 memory table on top of all of them: user-confirmed facts, preferences and
# context. Part of the complete head schema, never one of the eleven fact tables.
MEMORY_TABLES = {"memory"}
# 0006 introduces the projection, so all earlier revisions stay projection-free;
# 0007 introduces the analysis ledger, so 0006 stays analysis-free; 0008 introduces
# the memory table, so 0007 stays memory-free.
PRE_0006_TABLES = FACT_TABLES
PRE_0007_TABLES = FACT_TABLES | PROJECTION_TABLES | FTS_SHADOW_TABLES | {"alembic_version"}
PRE_0008_TABLES = PRE_0007_TABLES | ANALYSIS_TABLES
HEAD_TABLES = PRE_0008_TABLES | MEMORY_TABLES


@pytest.mark.parametrize("directory", ["data", "空库 with spaces 100%"])
def test_empty_database_upgrade_downgrade_upgrade(tmp_path, directory):
    config = AppConfig(data_dir=tmp_path / directory)
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        assert current_heads(engine) == ()
        assert expected_heads(config) == FORMAL_HEAD
        assert inspect(engine).get_table_names() == []

        command.upgrade(migration, INITIAL_HEAD[0])
        assert current_heads(engine) == INITIAL_HEAD
        assert inspect(engine).get_table_names() == ["alembic_version"]

        command.upgrade(migration, PHASE3_HEAD[0])
        assert current_heads(engine) == PHASE3_HEAD
        assert set(inspect(engine).get_table_names()) == PHASE3_TABLES | {"alembic_version"}

        command.upgrade(migration, "head")
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES

        # 0008 -> 0006 drops the memory table and the analysis ledger, and leaves the
        # projection in place.
        command.downgrade(migration, "0006_search_projection")
        assert current_heads(engine) == PROJECTION_HEAD
        assert set(inspect(engine).get_table_names()) == PRE_0007_TABLES
        assert {name for name in inspect(engine).get_table_names() if "memory" in name} == set()

        # 0006 -> 0005 drops both projection objects and every FTS5 shadow table.
        command.downgrade(migration, "0005_evidence")
        assert current_heads(engine) == ("0005_evidence",)
        assert set(inspect(engine).get_table_names()) == PRE_0006_TABLES | {"alembic_version"}

        command.upgrade(migration, "head")
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES

        command.downgrade(migration, PHASE3_HEAD[0])
        assert current_heads(engine) == PHASE3_HEAD
        assert set(inspect(engine).get_table_names()) == PHASE3_TABLES | {"alembic_version"}

        command.upgrade(migration, "head")
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES

        command.downgrade(migration, INITIAL_HEAD[0])
        assert current_heads(engine) == INITIAL_HEAD
        assert inspect(engine).get_table_names() == ["alembic_version"]

        command.upgrade(migration, "head")
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES

        command.downgrade(migration, "base")
        assert current_heads(engine) == ()
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

        command.upgrade(migration, "head")
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES
    finally:
        engine.dispose()


def test_formal_metadata_matches_migrated_schema_without_seed_data(tmp_path):
    runtime = initialize_database(AppConfig(data_dir=tmp_path))
    try:
        metadata_tables = set(Base.metadata.tables)
        # The ORM maps the eleven fact tables plus the search_document projection, the
        # analysis ledger and the memory table; search_document_fts stays
        # infrastructure, not a model.
        assert metadata_tables == (FACT_TABLES | {"search_document"} | ANALYSIS_TABLES
                                   | MEMORY_TABLES)
        assert FACT_TABLES.isdisjoint(ANALYSIS_TABLES)
        assert FACT_TABLES.isdisjoint(MEMORY_TABLES)
        assert set(inspect(runtime.engine).get_table_names()) == HEAD_TABLES
        with runtime.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalars().all() == list(FORMAL_HEAD)
            for table in Base.metadata.sorted_tables:
                assert connection.scalar(table.select().limit(1)) is None
        assert set(Base.metadata.tables) == metadata_tables
    finally:
        runtime.close()


def test_initialize_database_twice_across_restart(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    first = initialize_database(config)
    try:
        assert isinstance(first, DatabaseRuntime)
        assert current_heads(first.engine) == FORMAL_HEAD
        assert set(inspect(first.engine).get_table_names()) == HEAD_TABLES
        with first.sessions() as session:
            assert session.get_bind() is first.engine
    finally:
        first.close()

    second = initialize_database(config)
    try:
        assert second.engine is not first.engine
        assert second.sessions is not first.sessions
        assert current_heads(second.engine) == FORMAL_HEAD
        assert set(inspect(second.engine).get_table_names()) == HEAD_TABLES
        with second.sessions() as session:
            assert session.get_bind() is second.engine
    finally:
        second.close()


@pytest.mark.parametrize("empty_version_table", [False, True])
def test_unversioned_nonempty_database_is_rejected_without_schema_changes(
    tmp_path, empty_version_table
):
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    try:
        with engine.begin() as connection:
            # A disposable sentinel is not an application model or real user data.
            connection.exec_driver_sql("CREATE TABLE migration_test_sentinel (value TEXT)")
            connection.exec_driver_sql(
                "INSERT INTO migration_test_sentinel VALUES ('preserve-me')"
            )
            if empty_version_table:
                connection.exec_driver_sql(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
        before_tables = inspect(engine).get_table_names()
        with pytest.raises(DatabaseError, match="未纳入迁移管理"):
            initialize_database(config)
        assert inspect(engine).get_table_names() == before_tables
        assert current_heads(engine) == ()
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT value FROM migration_test_sentinel"
            ).scalars().all() == ["preserve-me"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "stored_heads", [("unrecognized_revision",), ("0001_empty_initial", "another_revision")]
)
def test_mismatched_version_is_rejected_and_not_rewritten(tmp_path, stored_heads):
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            for revision in stored_heads:
                connection.exec_driver_sql(
                    "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
                )
        with pytest.raises(DatabaseError, match="版本与程序不匹配"):
            initialize_database(config)
        assert set(current_heads(engine)) == set(stored_heads)
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("independent_cwd", [False, True])
@pytest.mark.parametrize("subcommand", ["current", "history"])
def test_alembic_cli_uses_explicit_config_from_any_cwd(tmp_path, independent_cwd, subcommand):
    config_directory = tmp_path / "配置 with spaces"
    config_directory.mkdir()
    config_path = config_directory / "settings.toml"
    config_path.write_text(
        '[storage]\ndata_dir = "relative data"\ndatabase = "cli.sqlite3"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.storage_paths.database == config_directory / "relative data" / "cli.sqlite3"
    runtime = initialize_database(config)
    runtime.close()
    cwd = tmp_path / "unrelated cwd" if independent_cwd else PROJECT_ROOT
    if independent_cwd:
        cwd.mkdir()

    result = subprocess.run(
        [
            sys.executable, "-s", "-m", "alembic",
            "-c", str(PROJECT_ROOT / "alembic.ini"),
            "-x", f"config={config_path}", subcommand,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head" in result.stdout
    if subcommand == "current":
        # The database was initialized at the Phase 8 head.
        assert FORMAL_HEAD[0] in result.stdout
    else:
        assert "0008_memory" in result.stdout
        assert "0007_analysis" in result.stdout
        assert "0006_search_projection" in result.stdout
        assert "0005_evidence" in result.stdout
        assert "0004_event" in result.stdout
        assert "0003_relationship" in result.stdout
        assert "0002_workspace_person_media" in result.stdout
        assert "0001_empty_initial" in result.stdout

    engine = create_sqlite_engine(config)
    try:
        assert current_heads(engine) == FORMAL_HEAD
        assert set(inspect(engine).get_table_names()) == HEAD_TABLES
    finally:
        engine.dispose()
    if independent_cwd:
        assert not list(cwd.rglob("*.sqlite3"))
