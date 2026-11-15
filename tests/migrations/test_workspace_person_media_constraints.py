"""Database-enforced Phase 3 contracts against the real Alembic-created schema."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import CheckConstraint, func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.config.settings import AppConfig
from app.storage.database import Base, create_sqlite_engine
from app.storage.migrations import alembic_config, current_heads
from app.storage.models import (
    MediaModel, PersonAliasModel, PersonMediaModel, PersonModel, WorkspaceModel,
)
from app.storage.session import create_session_factory


PHASE3_TABLES = {"workspace", "person", "person_alias", "media", "person_media"}
PHASE5_TABLES = PHASE3_TABLES | {"relationship", "relationship_history", "event", "event_participant"}
FORMAL_TABLES = PHASE5_TABLES | {"evidence", "evidence_media"}
# Phase 7 projection infrastructure: search_document is mapped, the FTS5 virtual
# table plus the five shadow tables SQLite creates for it are not.
SEARCH_PROJECTION_TABLES = {"search_document"}
PROJECTION_OBJECTS = {"search_document", "search_document_fts",
                      "search_document_fts_data", "search_document_fts_idx",
                      "search_document_fts_content", "search_document_fts_docsize",
                      "search_document_fts_config"}
# Phase 8 analysis ledger: the run table plus four provenance tables. They are part
# of the complete head schema and never one of the eleven business fact tables.
ANALYSIS_TABLES = {"analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source"}
# Phase 9 memory table: user-confirmed facts, preferences and context. Part of the
# complete head schema, never a business fact table.
MEMORY_TABLES = {"memory"}
NOW = datetime(2026, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc)


@pytest.fixture
def migrated_database(tmp_path, monkeypatch):
    """Only migration scripts may create formal tables, never create_all()."""
    def forbid_create_all(*args, **kwargs):
        raise AssertionError("Formal schema must be created through Alembic migrations")

    monkeypatch.setattr(Base.metadata, "create_all", forbid_create_all)
    metadata_tables = set(Base.metadata.tables)
    assert metadata_tables == (FORMAL_TABLES | SEARCH_PROJECTION_TABLES | ANALYSIS_TABLES
                               | MEMORY_TABLES)
    config = AppConfig(data_dir=tmp_path / "正式 migration with spaces")
    engine = create_sqlite_engine(config)
    migration = alembic_config(config)
    try:
        assert inspect(engine).get_table_names() == []
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "0002_workspace_person_media")
        migration.attributes.pop("connection")
        assert current_heads(engine) == ("0002_workspace_person_media",)
        assert set(inspect(engine).get_table_names()) == PHASE3_TABLES | {"alembic_version"}
        yield engine, migration
    finally:
        engine.dispose()
        assert set(Base.metadata.tables) == metadata_tables


def insert_workspace(connection, *, name=None, timestamp=NOW):
    identifier = uuid4()
    connection.execute(WorkspaceModel.__table__.insert().values(
        id=identifier, name=name or f"Workspace {identifier}",
        created_at=timestamp, updated_at=timestamp,
    ))
    return identifier


def insert_person(connection, workspace_id, *, name="Same name", timestamp=NOW):
    identifier = uuid4()
    connection.execute(PersonModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, display_name=name, real_name=name,
        created_at=timestamp, updated_at=timestamp,
    ))
    return identifier


def insert_media(connection, workspace_id, *, sha256="a" * 64, timestamp=NOW):
    identifier = uuid4()
    connection.execute(MediaModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, sha256=sha256,
        mime_type="image/png", size_bytes=42, width=2, height=3,
        storage_path=f"media/{identifier}.png", created_at=timestamp, updated_at=timestamp,
    ))
    return identifier


def insert_alias(connection, workspace_id, person_id, *, timestamp=NOW):
    identifier = uuid4()
    connection.execute(PersonAliasModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, person_id=person_id,
        alias="Shared alias", alias_type="NICKNAME", created_at=timestamp, updated_at=timestamp,
    ))
    return identifier


def insert_link(connection, workspace_id, person_id, media_id, *, role="GALLERY", timestamp=NOW):
    identifier = uuid4()
    connection.execute(PersonMediaModel.__table__.insert().values(
        id=identifier, workspace_id=workspace_id, person_id=person_id, media_id=media_id,
        role=role, sort_order=0, created_at=timestamp,
    ))
    return identifier


@pytest.mark.parametrize("reference", ["alias_person", "link_person", "link_media"])
def test_composite_foreign_keys_reject_cross_workspace_rows(migrated_database, reference):
    engine, _ = migrated_database
    with engine.begin() as connection:
        first_workspace = insert_workspace(connection)
        second_workspace = insert_workspace(connection)
        first_person = insert_person(connection, first_workspace)
        second_person = insert_person(connection, second_workspace)
        first_media = insert_media(connection, first_workspace)
        second_media = insert_media(connection, second_workspace)
        # Positive controls: every referenced ID exists and same-workspace rows work.
        insert_alias(connection, first_workspace, first_person)
        insert_link(connection, first_workspace, first_person, first_media)

    with pytest.raises(IntegrityError) as failure:
        with engine.begin() as connection:
            if reference == "alias_person":
                insert_alias(connection, second_workspace, first_person)
            elif reference == "link_person":
                insert_link(connection, first_workspace, second_person, first_media)
            else:
                insert_link(connection, first_workspace, first_person, second_media)
    assert failure.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(PersonAliasModel)) == 1
        assert connection.scalar(select(func.count()).select_from(PersonMediaModel)) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_partial_avatar_unique_allows_multiple_gallery_and_other_person_avatar(migrated_database):
    engine, _ = migrated_database
    with engine.begin() as connection:
        workspace = insert_workspace(connection)
        person = insert_person(connection, workspace)
        other_person = insert_person(connection, workspace)
        first_media = insert_media(connection, workspace)
        second_media = insert_media(connection, workspace, sha256="b" * 64)
        insert_link(connection, workspace, person, first_media)
        insert_link(connection, workspace, person, second_media)
        insert_link(connection, workspace, person, first_media, role="AVATAR")
        insert_link(connection, workspace, other_person, first_media, role="AVATAR")

    with pytest.raises(IntegrityError) as failure:
        with engine.begin() as connection:
            insert_link(connection, workspace, person, second_media, role="AVATAR")
    assert failure.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    with engine.connect() as connection:
        rows = connection.execute(select(PersonMediaModel.__table__)).all()
        assert len(rows) == 4
        assert sum(row.role == "AVATAR" for row in rows) == 2
        assert sum(row.role == "GALLERY" for row in rows) == 2
    avatar_index = next(
        index for index in inspect(engine).get_indexes("person_media")
        if index["name"] == "uq_person_media_avatar"
    )
    assert avatar_index["unique"]
    assert avatar_index["column_names"] == ["person_id"]
    assert str(avatar_index["dialect_options"]["sqlite_where"]) == "role = 'AVATAR'"


def test_person_display_and_real_names_are_not_unique(migrated_database):
    engine, _ = migrated_database
    with engine.begin() as connection:
        first_workspace = insert_workspace(connection)
        second_workspace = insert_workspace(connection)
        identifiers = {
            insert_person(connection, first_workspace),
            insert_person(connection, first_workspace),
            insert_person(connection, second_workspace),
        }
    assert len(identifiers) == 3
    with engine.connect() as connection:
        rows = connection.execute(select(PersonModel.__table__)).all()
        assert len(rows) == 3
        assert {row.display_name for row in rows} == {"Same name"}
        assert {row.real_name for row in rows} == {"Same name"}


def test_alias_is_not_globally_or_workspace_unique(migrated_database):
    engine, _ = migrated_database
    with engine.begin() as connection:
        first_workspace = insert_workspace(connection)
        second_workspace = insert_workspace(connection)
        first_person = insert_person(connection, first_workspace)
        other_person = insert_person(connection, first_workspace)
        second_person = insert_person(connection, second_workspace)
        insert_alias(connection, first_workspace, first_person)
        insert_alias(connection, first_workspace, other_person)
        insert_alias(connection, second_workspace, second_person)
    with engine.connect() as connection:
        rows = connection.execute(select(PersonAliasModel.__table__)).all()
        assert len(rows) == 3
        assert {row.alias for row in rows} == {"Shared alias"}
        assert len({row.workspace_id for row in rows}) == 2


def test_media_hash_is_unique_only_within_workspace(migrated_database):
    engine, _ = migrated_database
    with engine.begin() as connection:
        first_workspace = insert_workspace(connection)
        second_workspace = insert_workspace(connection)
        insert_media(connection, first_workspace)
        insert_media(connection, second_workspace)
    with pytest.raises(IntegrityError) as failure:
        with engine.begin() as connection:
            insert_media(connection, first_workspace)
    assert failure.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    with engine.connect() as connection:
        rows = connection.execute(select(MediaModel.__table__)).all()
        assert len(rows) == 2
        assert {row.sha256 for row in rows} == {"a" * 64}
        assert {row.workspace_id for row in rows} == {first_workspace, second_workspace}


@pytest.mark.parametrize("model", [
    WorkspaceModel, PersonModel, PersonAliasModel, MediaModel, PersonMediaModel,
])
def test_utc_datetime_reload_from_migrated_schema(migrated_database, model):
    engine, _ = migrated_database
    local_time = datetime(2026, 3, 4, 20, 34, 56, 123456,
                          tzinfo=timezone(timedelta(hours=8)))
    with engine.begin() as connection:
        workspace = insert_workspace(connection, timestamp=local_time)
        person = insert_person(connection, workspace, timestamp=local_time)
        media = insert_media(connection, workspace, timestamp=local_time)
        alias = insert_alias(connection, workspace, person, timestamp=local_time)
        link = insert_link(connection, workspace, person, media, timestamp=local_time)
        identifiers = {
            WorkspaceModel: workspace, PersonModel: person, MediaModel: media,
            PersonAliasModel: alias, PersonMediaModel: link,
        }
        # Exercise nullable UTC fields as well, where the model has them.
        for mapped_model in (WorkspaceModel, PersonModel, MediaModel):
            table = mapped_model.__table__
            connection.execute(table.update().where(table.c.id == identifiers[mapped_model])
                               .values(deleted_at=local_time))
    # Use fresh DBAPI connections and ORM sessions, not the inserted object values.
    engine.dispose()
    sessions = create_session_factory(engine)
    with sessions() as session:
        reloaded = session.get(model, identifiers[model])
        assert reloaded is not None
        assert reloaded.id == identifiers[model]
        for field in ("created_at", "updated_at", "deleted_at"):
            if hasattr(reloaded, field):
                value = getattr(reloaded, field)
                assert value == NOW
                assert value.tzinfo is timezone.utc
                assert value.utcoffset() == timedelta(0)
    # Missing nullable timestamps must still reload as None, not a made-up time.
    with engine.begin() as connection:
        live_workspace = insert_workspace(connection)
    with sessions() as session:
        assert session.get(WorkspaceModel, live_workspace).deleted_at is None


def test_formal_metadata_matches_migration_and_alembic_check(migrated_database):
    engine, migration = migrated_database
    metadata_tables = set(Base.metadata.tables)
    # The eleven fact tables plus the mapped Phase 7 search_document projection, the
    # Phase 8 analysis ledger and the Phase 9 memory table.
    assert metadata_tables == (FORMAL_TABLES | SEARCH_PROJECTION_TABLES | ANALYSIS_TABLES
                               | MEMORY_TABLES)
    assert len(FORMAL_TABLES) == 11
    assert FORMAL_TABLES.isdisjoint(ANALYSIS_TABLES)
    assert FORMAL_TABLES.isdisjoint(MEMORY_TABLES)
    assert "search_document_fts" not in metadata_tables

    def assert_reflected_schema_matches(table_names):
        inspector = inspect(engine)
        # Projection, analysis and memory objects are present from 0006/0007/0008
        # onwards; the fact tables must match exactly whether or not those later
        # objects exist yet.
        reflected = set(inspector.get_table_names())
        assert reflected >= table_names | {"alembic_version"}
        assert reflected - table_names - {"alembic_version"} <= (
            PROJECTION_OBJECTS | ANALYSIS_TABLES | MEMORY_TABLES)
        for table in Base.metadata.sorted_tables:
            if table.name not in table_names:
                continue
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.c.keys())
            assert inspector.get_pk_constraint(table.name)["name"] == table.primary_key.name
            actual_checks = {constraint["name"] for constraint in inspector.get_check_constraints(table.name)}
            expected_checks = {constraint.name for constraint in table.constraints
                               if isinstance(constraint, CheckConstraint)}
            assert actual_checks == expected_checks
            actual_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
            assert actual_indexes == {index.name for index in table.indexes}

    # Revision 0002 must still match every original Phase 3 model field/constraint.
    assert current_heads(engine) == ("0002_workspace_person_media",)
    assert_reflected_schema_matches(PHASE3_TABLES)
    # This explicit step stays frozen at 0004; only the later head can match all metadata.
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            command.upgrade(migration, "0004_event")
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0004_event",)
    assert_reflected_schema_matches(PHASE5_TABLES)
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            command.upgrade(migration, "0005_evidence")
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0005_evidence",)
    assert_reflected_schema_matches(FORMAL_TABLES)
    assert set(inspect(engine).get_table_names()) == FORMAL_TABLES | {"alembic_version"}
    # The 0006 step adds projection infrastructure only.
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            command.upgrade(migration, "0006_search_projection")
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0006_search_projection",)
    assert_reflected_schema_matches(FORMAL_TABLES)
    assert set(inspect(engine).get_table_names()) == (
        FORMAL_TABLES | PROJECTION_OBJECTS | {"alembic_version"})
    # The 0007 step adds the analysis ledger only.
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            command.upgrade(migration, "0007_analysis")
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0007_analysis",)
    assert_reflected_schema_matches(FORMAL_TABLES)
    assert set(inspect(engine).get_table_names()) == (
        FORMAL_TABLES | PROJECTION_OBJECTS | ANALYSIS_TABLES | {"alembic_version"})
    # The 0008 step adds the memory table only.
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            command.upgrade(migration, "0008_memory")
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0008_memory",)
    assert_reflected_schema_matches(FORMAL_TABLES)
    assert set(inspect(engine).get_table_names()) == (
        FORMAL_TABLES | PROJECTION_OBJECTS | ANALYSIS_TABLES | MEMORY_TABLES
        | {"alembic_version"})
    with engine.connect() as connection:
        migration.attributes["connection"] = connection
        try:
            command.check(migration)
        finally:
            migration.attributes.pop("connection")
    assert current_heads(engine) == ("0008_memory",)
    assert set(Base.metadata.tables) == metadata_tables
