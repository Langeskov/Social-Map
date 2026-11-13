"""Schema initialization migrates formal tables without creating a DefaultWorkspace."""
from sqlalchemy import func, inspect, select

from app.config.settings import AppConfig
from app.services.database import initialize_database
from app.services.unit_of_work import transaction
from app.storage.database import Base, load_models
from app.storage.migrations import current_heads
from app.storage.session import ThreadBoundSession


PHASE3_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
PHASE5_TABLES = PHASE3_TABLES | {"relationship", "relationship_history", "event", "event_participant"}
EVIDENCE_TABLES = {"evidence", "evidence_media"}
# Eleven business fact tables; migrations 0006, 0007 and 0008 create none and change
# none.
FACT_TABLES = PHASE5_TABLES | EVIDENCE_TABLES
# Phase 7 projection infrastructure: one ordinary projection table plus the FTS5
# virtual table. SQLite also materializes five shadow tables for the FTS5 index
# (search_document_fts_data/_idx/_content/_docsize/_config); they belong to the
# projection and must never be counted as business tables.
PROJECTION_TABLES = {"search_document", "search_document_fts"}
FTS_SHADOW_TABLES = {"search_document_fts_data", "search_document_fts_idx",
                     "search_document_fts_content", "search_document_fts_docsize",
                     "search_document_fts_config"}
# Phase 8 analysis ledger: one run table plus four provenance tables. They are
# mapped models created by migration 0007 and are never business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 memory table: user-confirmed facts, preferences and context. A mapped model
# created by migration 0008, never a business fact table.
MEMORY_TABLES = {"memory"}
ORM_TABLES = FACT_TABLES | {"search_document"} | ANALYSIS_TABLES | MEMORY_TABLES
HEAD_TABLES = ORM_TABLES | PROJECTION_TABLES | FTS_SHADOW_TABLES | {"alembic_version"}
FORMAL_HEAD = ("0008_memory",)


def test_application_initialization_migrates_schema_without_default_workspace(tmp_path, monkeypatch):
    load_models()
    metadata_tables = set(Base.metadata.tables)
    assert metadata_tables == ORM_TABLES
    # The FTS5 index stays infrastructure: it is never a mapped model.
    assert "search_document_fts" not in metadata_tables

    def forbid_create_all(*args, **kwargs):
        raise AssertionError("Formal application tables must be initialized through Alembic")

    monkeypatch.setattr(Base.metadata, "create_all", forbid_create_all)
    config = AppConfig(data_dir=tmp_path)
    runtime = initialize_database(config)
    try:
        assert set(inspect(runtime.engine).get_table_names()) == HEAD_TABLES
        assert current_heads(runtime.engine) == FORMAL_HEAD
        assert set(Base.metadata.tables) == metadata_tables
        with transaction(runtime.sessions) as session:
            assert isinstance(session, ThreadBoundSession)
            assert session.scalar(select(1)) == 1
            # WorkspaceService at the entry point owns business initialization.
            for table in Base.metadata.sorted_tables:
                assert session.execute(table.select()).all() == []
        assert runtime.engine.pool.checkedout() == 0
        original_pool = runtime.engine.pool
    finally:
        runtime.close()
    assert runtime.engine.pool is not original_pool

    reopened = initialize_database(config)
    try:
        assert set(inspect(reopened.engine).get_table_names()) == HEAD_TABLES
        assert current_heads(reopened.engine) == FORMAL_HEAD
        assert set(Base.metadata.tables) == metadata_tables
        with reopened.engine.connect() as connection:
            for table in Base.metadata.sorted_tables:
                assert connection.execute(table.select()).all() == []
            # The projection starts empty; the migration inserts no derived rows.
            assert connection.scalar(select(func.count()).select_from(
                Base.metadata.tables["search_document"])) == 0
    finally:
        reopened.close()
