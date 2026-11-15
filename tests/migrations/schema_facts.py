"""Frozen Phase 1-6 schema facts shared by migration and initialization tests.

Phase 7 (revision ``0006_search_projection``) keeps every business fact table
unchanged and adds PROJECTION infrastructure on top of it:

* ``search_document`` - an ordinary projection table (``SearchDocumentModel``);
* ``search_document_fts`` - an FTS5 virtual table that SQLite implements through
  five shadow tables (``..._data``, ``..._idx``, ``..._content``, ``..._docsize``,
  ``..._config``). The virtual table is deliberately NOT a mapped model.

Phase 8 (revision ``0007_analysis``) likewise adds no business fact table: it
records analysis *runs* and the fact rows they consumed. ``analysis_run`` plus the
four ``analysis_*_source`` provenance tables are therefore a third group:

* they belong to the "complete schema" of the current head;
* they must never be counted as one of the eleven business fact tables;
* the fact tables they reference keep their exact shape and data.

Phase 9 (revision ``0008_memory``) again adds no business fact table: ``memory``
stores what the user has confirmed about a person or about a whole workspace. It is
a fourth group with the same two rules - part of the complete schema at the head,
never one of the eleven fact tables.

The groups must therefore be asserted separately: a projection, analysis or memory
object must never be counted as a business fact table, and the fact-table
assertions stay exactly as strict as they were before Phases 7, 8 and 9.
"""
from datetime import datetime, timezone

from app.storage.database import load_models

NOW = datetime(2026, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc)

PHASE3_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
PHASE5_TABLES = PHASE3_TABLES | {"relationship", "relationship_history",
                                 "event", "event_participant"}
EVIDENCE_TABLES = {"evidence", "evidence_media"}
# The eleven business fact tables: 0 at 0001, 5 at 0002, 7 at 0003, 9 at 0004,
# 11 at 0005 and still exactly 11 at 0006 and at 0007.
FACT_TABLES = PHASE5_TABLES | EVIDENCE_TABLES
SEARCH_DOCUMENT = "search_document"
SEARCH_DOCUMENT_FTS = "search_document_fts"
PROJECTION_TABLES = {SEARCH_DOCUMENT, SEARCH_DOCUMENT_FTS}
# Revision 0006 adds exactly these two objects itself; project_objects() also has
# to report the shadow tables SQLite materializes for the FTS5 index.
PROJECTION_OBJECTS = PROJECTION_TABLES

# Revision 0007 records analysis runs and the facts they consumed. Both are
# mapped models created by the migration itself, so they are part of the "complete
# schema" of the head - and never part of FACT_TABLES.
ANALYSIS_RUN = "analysis_run"
ANALYSIS_RUN_TABLES = {ANALYSIS_RUN}
ANALYSIS_SOURCE_TABLES = {
    "analysis_person_source", "analysis_relationship_source",
    "analysis_event_source", "analysis_evidence_source",
}
# Maps each provenance table to the fact table its (workspace_id, target_id)
# composite foreign key points at.
ANALYSIS_SOURCE_TARGETS = {
    "analysis_person_source": "person",
    "analysis_relationship_source": "relationship",
    "analysis_event_source": "event",
    "analysis_evidence_source": "evidence",
}
ANALYSIS_TABLES = ANALYSIS_RUN_TABLES | ANALYSIS_SOURCE_TABLES
ANALYSIS_OBJECTS = ANALYSIS_TABLES

# Revision 0008 stores user-confirmed memories. ``memory`` is a mapped model created
# by the migration itself, so it belongs to the "complete schema" of the head - and
# never to FACT_TABLES, which stays exactly eleven tables wide.
MEMORY = "memory"
MEMORY_TABLES = {MEMORY}
# The five indexes 0008 creates. They are asserted by name because the memory
# object set must be matched exactly: no other schema object carries "memory".
MEMORY_INDEXES = {
    "ix_memory_workspace_person": ("workspace_id", "person_id"),
    "ix_memory_workspace_kind": ("workspace_id", "kind"),
    "ix_memory_workspace_deleted": ("workspace_id", "deleted_at"),
    "ix_memory_workspace_valid": ("workspace_id", "valid_from", "valid_until"),
    "ix_memory_workspace_analysis": ("workspace_id", "source_analysis_id"),
}


def fts_shadow_tables(table_names) -> set[str]:
    """Select the FTS5 shadow tables SQLite creates for search_document_fts.

    They live in ``sqlite_master`` next to the virtual table, so a "complete
    schema" assertion has to name them; a "business fact tables" assertion must
    always filter them out.
    """
    return {name for name in table_names
            if name.startswith(SEARCH_DOCUMENT_FTS + "_") and name != SEARCH_DOCUMENT_FTS}


def expected_head_tables(table_names=None) -> set[str]:
    """Full current-head table set: facts, projection, analysis, memory, version.

    ``table_names`` is an existing ``sqlite_master`` table listing when the caller
    wants the five FTS5 shadow tables included; without it the FTS5 virtual table
    alone stands for the index, exactly like ``PROJECTION_TABLES``.
    """
    names = (FACT_TABLES | PROJECTION_TABLES | ANALYSIS_TABLES | MEMORY_TABLES
             | {"alembic_version"})
    if table_names is None:
        return names
    return names | fts_shadow_tables(table_names)


