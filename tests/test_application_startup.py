from pathlib import Path
import os
import subprocess
import sys
from sqlalchemy import inspect
from app.config.settings import load_config
from app.services.database import initialize_database
from app.storage.migrations import current_heads


# Eleven business fact tables, unchanged by the Phase 7 projection migration, by the
# Phase 8 analysis migration and by the Phase 9 memory migration.
FACT_TABLES = {
    "workspace", "person", "person_alias", "media", "person_media",
    "relationship", "relationship_history", "event", "event_participant",
    "evidence", "evidence_media",
}
# Phase 7 projection infrastructure: the projection table, the FTS5 virtual
# table and the five shadow tables SQLite materializes for the FTS5 index.
PROJECTION_OBJECTS = {
    "search_document", "search_document_fts", "search_document_fts_data",
    "search_document_fts_idx", "search_document_fts_content",
    "search_document_fts_docsize", "search_document_fts_config",
}
# Phase 8 analysis ledger: the run table and its four provenance tables. It records
# how an answer was produced and is never a business fact table.
ANALYSIS_TABLES = {
    "analysis_run", "analysis_person_source", "analysis_relationship_source",
    "analysis_event_source", "analysis_evidence_source",
}
# Phase 9 memory table: what the user confirmed about a person or a whole workspace.
# Part of the complete schema, never a business fact table.
MEMORY_TABLES = {"memory"}


def run_app(config_path: Path, cwd: Path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-s", "-m", "app.main", "--config", str(config_path), "--smoke-test"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=30)


def test_application_process_restart(tmp_path):
    from app.config.settings import PROJECT_ROOT
    config_path = tmp_path / "application.toml"
    config_path.write_text('[storage]\ndata_dir="runtime"\n', encoding="utf-8")
    for _ in range(2):
        process = run_app(config_path, PROJECT_ROOT)
        assert process.returncode == 0, process.stdout + process.stderr
    config = load_config(config_path)
    assert config.storage_paths.database.exists()
    runtime = initialize_database(config)
    try:
        table_names = set(inspect(runtime.engine).get_table_names())
        assert table_names == FACT_TABLES | PROJECTION_OBJECTS | ANALYSIS_TABLES | MEMORY_TABLES | {
            "alembic_version"}
        assert len(FACT_TABLES) == 11
        assert FACT_TABLES.isdisjoint(PROJECTION_OBJECTS)
        assert FACT_TABLES.isdisjoint(ANALYSIS_TABLES)
        assert FACT_TABLES.isdisjoint(MEMORY_TABLES)
        assert ANALYSIS_TABLES.isdisjoint(PROJECTION_OBJECTS)
        assert MEMORY_TABLES.isdisjoint(PROJECTION_OBJECTS)
        assert MEMORY_TABLES.isdisjoint(ANALYSIS_TABLES)
        assert current_heads(runtime.engine) == ("0008_memory",)
    finally:
        runtime.close()
    log = (config.log_dir / "application.log").read_text(encoding="utf-8")
    assert log.count("GUI smoke test passed") == 2


def test_application_connection_failure_logged(tmp_path):
    from app.config.settings import PROJECT_ROOT
    config_path = tmp_path / "broken.toml"
    config_path.write_text('[storage]\ndata_dir="runtime"\n', encoding="utf-8")
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "database.sqlite3").write_bytes(b"Not a SQLite database")
    process = run_app(config_path, PROJECT_ROOT)
    assert process.returncode == 1
    errors = (root / "logs" / "error.log").read_text(encoding="utf-8")
    assert "connection initialization" in errors
    assert "SQLITE_NOTADB" in errors
    assert "Not a SQLite database" not in errors
