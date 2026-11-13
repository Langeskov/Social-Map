from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable, CreateIndex
from app.storage.database import Base, load_models
from app.storage.types import UTCDateTime


PHASE3_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
PHASE5_TABLES = PHASE3_TABLES | {"relationship", "relationship_history", "event", "event_participant"}
EVIDENCE_TABLES = {"evidence", "evidence_media"}
# The eleven business fact tables; unchanged by Phase 7 and by Phase 8.
FACT_TABLES = PHASE5_TABLES | EVIDENCE_TABLES
# Phase 7 adds the search projection as a mapped table. The FTS5 virtual table
# search_document_fts and the shadow tables SQLite creates for it are storage
# infrastructure, so they are deliberately absent from the ORM metadata.
SEARCH_PROJECTION_TABLES = {"search_document"}
# Phase 8 adds the analysis ledger: one run table plus four provenance tables.
# They record how an answer was produced and are never business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 adds the memory table: user-confirmed facts, preferences and context about
# one person or a whole workspace. Also never a business fact table.
MEMORY_TABLES = {"memory"}
ALLOWED_TABLES = FACT_TABLES | SEARCH_PROJECTION_TABLES | ANALYSIS_TABLES | MEMORY_TABLES


def test_five_models_have_named_portable_constraints():
    load_models()
    metadata_tables = set(Base.metadata.tables)
    assert PHASE3_TABLES < metadata_tables
    # The projection table, the analysis ledger and the memory table are mapped on
    # top of the eleven fact tables.
    assert metadata_tables == ALLOWED_TABLES
    assert len(FACT_TABLES) == 11
    assert SEARCH_PROJECTION_TABLES.isdisjoint(FACT_TABLES)
    assert ANALYSIS_TABLES.isdisjoint(FACT_TABLES)
    assert MEMORY_TABLES.isdisjoint(FACT_TABLES)
    assert "search_document_fts" not in metadata_tables
    for table in Base.metadata.sorted_tables:
        assert all(constraint.name for constraint in table.constraints)
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            assert str(CreateTable(table).compile(dialect=dialect))
            for index in table.indexes:
                assert str(CreateIndex(index).compile(dialect=dialect))
    association = Base.metadata.tables["person_media"]
    assert {tuple(fk.column_keys) for fk in association.foreign_key_constraints} == {
        ("workspace_id", "person_id"), ("workspace_id", "media_id")}
    index = next(index for index in association.indexes if index.name == "uq_person_media_avatar")
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert "WHERE role = 'AVATAR'" in str(CreateIndex(index).compile(dialect=dialect))
    evidence_media = Base.metadata.tables["evidence_media"]
    assert {tuple(fk.column_keys) for fk in evidence_media.foreign_key_constraints} == {
        ("workspace_id", "evidence_id"), ("workspace_id", "media_id")}
    norole = next(index for index in evidence_media.indexes
                  if index.name == "uq_evidence_media_evidence_media_norole")
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert "WHERE role IS NULL" in str(CreateIndex(norole).compile(dialect=dialect))


def test_utc_type_roundtrip_and_naive_rejection():
    kind = UTCDateTime()
    instant = datetime(2025, 1, 2, 10, tzinfo=timezone(timedelta(hours=8)))
    bound = kind.process_bind_param(instant, sqlite.dialect())
    assert bound == datetime(2025, 1, 2, 2)
    assert kind.process_result_value(bound, sqlite.dialect()) == instant
    assert kind.process_bind_param(instant, postgresql.dialect()).tzinfo == timezone.utc
    with pytest.raises(ValueError):
        kind.process_bind_param(datetime(2025, 1, 2), sqlite.dialect())
