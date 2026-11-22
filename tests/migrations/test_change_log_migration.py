"""Phase 14.1 migration 0009_change_log: schema, downgrade, data preservation.

Exercises the real Alembic chain on a temporary SQLite file; no PostgreSQL server,
no create_all(). The ChangeLog table is the only object 0009 creates.
"""
from datetime import datetime, timezone
from io import StringIO
from uuid import uuid4

from alembic import command
import pytest
import sqlalchemy as sa

from app.config.settings import AppConfig
from app.storage.database import create_sqlite_engine
from app.storage.migrations import alembic_config, current_heads
from app.storage.models import ChangeLogModel, PersonModel, WorkspaceModel

HEAD = ("0009_change_log",)
NOW = datetime(2026, 11, 22, 9, 0, 0, 123456, tzinfo=timezone.utc)
COLUMNS = ["id", "workspace_id", "entity_type", "entity_id", "parent_entity_type",
           "parent_entity_id", "action", "operation_id", "revision_no",
           "changed_fields", "before_snapshot", "after_snapshot", "payload_version",
           "metadata", "recorded_at"]
NULLABLE = {"parent_entity_type", "parent_entity_id", "before_snapshot",
            "after_snapshot", "metadata"}
CHECKS = {
    "ck_change_log_action_valid", "ck_change_log_changed_fields_present",
    "ck_change_log_entity_type_valid", "ck_change_log_parent_pair_valid",
    "ck_change_log_parent_type_valid", "ck_change_log_payload_version_nonempty",
    "ck_change_log_revision_positive",
}
INDEXES = {
    "ix_change_log_workspace_entity":
        ["workspace_id", "entity_type", "entity_id", "revision_no"],
    "ix_change_log_workspace_operation": ["workspace_id", "operation_id"],
    "ix_change_log_workspace_recorded": ["workspace_id", "recorded_at", "id"],
}
UNIQUES = {
    ("workspace_id", "id"),
    ("workspace_id", "entity_type", "entity_id", "revision_no"),
}


def open_database(tmp_path, monkeypatch):
    monkeypatch.setattr(sa.MetaData, "create_all",
                        lambda *a, **k: pytest.fail("create_all forbidden"))
    config = AppConfig(data_dir=tmp_path / "phase14 change log db")
    return config, create_sqlite_engine(config), alembic_config(config)


def migrate(engine, migration, revision, *, down=False):
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        try:
            (command.downgrade if down else command.upgrade)(migration, revision)
        finally:
            migration.attributes.pop("connection", None)


def test_migration_creates_only_the_change_log_table(tmp_path, monkeypatch):
    config, engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0008_memory")
        assert current_heads(engine) == ("0008_memory",)
        before = set(sa.inspect(engine).get_table_names())
        assert "change_log" not in before

        migrate(engine, migration, "head")
        assert current_heads(engine) == HEAD
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())
        assert tables - before == {"change_log"}
        assert before < tables

        assert [column["name"] for column in inspector.get_columns("change_log")] == COLUMNS
        assert {column["name"] for column in inspector.get_columns("change_log")
                if column["nullable"]} == NULLABLE
        assert inspector.get_pk_constraint("change_log") == {
            "constrained_columns": ["id"], "name": "pk_change_log"}
        fks = inspector.get_foreign_keys("change_log")
        assert [(fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"],
                 tuple(fk["referred_columns"])) for fk in fks] == [
            ("fk_change_log_workspace_id_workspace", ("workspace_id",), "workspace",
             ("id",))]
        assert {tuple(unique["column_names"])
                for unique in inspector.get_unique_constraints("change_log")} == UNIQUES
        assert {check["name"] for check in inspector.get_check_constraints("change_log")} \
            == CHECKS
        assert {index["name"]: list(index["column_names"])
                for index in inspector.get_indexes("change_log")
                if index["name"].startswith("ix_change_log_")} == INDEXES
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        # Downgrade removes the ChangeLog only; every earlier object stays.
        migrate(engine, migration, "0008_memory", down=True)
        assert current_heads(engine) == ("0008_memory",)
        assert set(sa.inspect(engine).get_table_names()) == before
        assert {name for name in sa.inspect(engine).get_table_names()
                if "change_log" in name} == set()

        migrate(engine, migration, "head")
        assert current_heads(engine) == HEAD
        assert "change_log" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_upgrade_preserves_existing_rows_and_seeds_nothing(tmp_path, monkeypatch):
    config, engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "0008_memory")
        workspace_id, person_id = uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(WorkspaceModel.__table__.insert().values(
                id=workspace_id, name="保留工作区", created_at=NOW, updated_at=NOW))
            connection.execute(PersonModel.__table__.insert().values(
                id=person_id, workspace_id=workspace_id, display_name="保留人物",
                created_at=NOW, updated_at=NOW))
            before_workspace = connection.execute(
                sa.text("SELECT name FROM workspace WHERE id = :id"),
                {"id": workspace_id.hex}).scalar_one()
            before_person = connection.execute(
                sa.text("SELECT display_name FROM person WHERE id = :id"),
                {"id": person_id.hex}).scalar_one()

        migrate(engine, migration, "head")
        with engine.begin() as connection:
            assert connection.execute(
                sa.text("SELECT name FROM workspace WHERE id = :id"),
                {"id": workspace_id.hex}).scalar_one() == before_workspace
            assert connection.execute(
                sa.text("SELECT display_name FROM person WHERE id = :id"),
                {"id": person_id.hex}).scalar_one() == before_person
            assert connection.exec_driver_sql("SELECT count(*) FROM change_log").scalar() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        # Downgrading 0009 keeps the business rows untouched.
        migrate(engine, migration, "0008_memory", down=True)
        with engine.begin() as connection:
            assert connection.execute(
                sa.text("SELECT display_name FROM person WHERE id = :id"),
                {"id": person_id.hex}).scalar_one() == before_person
    finally:
        engine.dispose()


def test_constraints_reject_invalid_rows_through_real_sql(tmp_path, monkeypatch):
    config, engine, migration = open_database(tmp_path, monkeypatch)
    try:
        migrate(engine, migration, "head")
        workspace_id, entity_id, operation_id = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(WorkspaceModel.__table__.insert().values(
                id=workspace_id, name="约束工作区", created_at=NOW, updated_at=NOW))

        def insert_override(connection, **overrides):
            values = dict(id=uuid4(), workspace_id=workspace_id, entity_type="PERSON",
                          entity_id=entity_id, action="UPDATE",
                          operation_id=operation_id, revision_no=1,
                          changed_fields="[]", payload_version="1", recorded_at=NOW)
            values.update(overrides)
            connection.execute(ChangeLogModel.__table__.insert().values(**values))

        for overrides in (
            {"entity_type": "BOGUS"},
            {"action": "BOGUS"},
            {"action": None},                          # NOT NULL
            {"revision_no": 0},
            {"payload_version": ""},
            {"changed_fields": "   "},                 # CHECK non-empty
            {"parent_entity_type": "PERSON"},          # parent id missing
            {"parent_entity_id": uuid4()},             # parent type missing
            {"workspace_id": uuid4()},                 # workspace FK violation
        ):
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as connection:
                    insert_override(connection, **overrides)

        with engine.begin() as connection:
            insert_override(connection, revision_no=1)
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as connection:
                insert_override(connection, revision_no=1)   # duplicate revision
        with engine.begin() as connection:
            insert_override(connection, revision_no=2)       # next revision is fine
    finally:
        engine.dispose()


def test_offline_sql_renders_and_executes_the_change_log(tmp_path, monkeypatch):
    config, engine, migration = open_database(tmp_path, monkeypatch)
    try:
        output = StringIO()
        migration.output_buffer = output
        command.upgrade(migration, "head", sql=True)
        upgrade_sql = output.getvalue()
        assert "CREATE TABLE change_log " in upgrade_sql
        assert "CONSTRAINT pk_change_log PRIMARY KEY (id)" in upgrade_sql
        assert "CONSTRAINT fk_change_log_workspace_id_workspace FOREIGN KEY" \
               "(workspace_id) REFERENCES workspace (id)" in upgrade_sql
        assert "CONSTRAINT uq_change_log_workspace_entity_revision UNIQUE " \
               "(workspace_id, entity_type, entity_id, revision_no)" in upgrade_sql
        assert "CONSTRAINT uq_change_log_workspace_id_id UNIQUE (workspace_id, id)" \
               in upgrade_sql
        for name in sorted(INDEXES):
            assert f"CREATE INDEX {name} ON change_log " in upgrade_sql
        for check in sorted(CHECKS):
            assert f"CONSTRAINT {check} CHECK" in upgrade_sql
        assert "entity_type IN ('WORKSPACE', 'PERSON', 'RELATIONSHIP', " \
               "'RELATIONSHIP_HISTORY', 'EVENT', 'EVIDENCE', 'MEDIA', 'MEMORY')" \
               in upgrade_sql
        assert "action IN ('CREATE', 'UPDATE', 'ARCHIVE', 'RESTORE', 'IMPORT', " \
               "'APPEND_HISTORY', 'ATTACH_EVENT', 'DETACH_EVENT', 'REVERT')" in upgrade_sql
        assert "CONSTRAINT ck_change_log_parent_pair_valid CHECK " \
               "((parent_entity_type IS NULL) = (parent_entity_id IS NULL))" in upgrade_sql

        # Executing the whole rendered chain on a fresh database reaches the head.
        fresh_config = AppConfig(data_dir=tmp_path / "offline head install")
        fresh_engine = create_sqlite_engine(fresh_config)
        try:
            connection = fresh_engine.raw_connection()
            try:
                connection.executescript(upgrade_sql)
            finally:
                connection.close()
            assert current_heads(fresh_engine) == HEAD
            assert "change_log" in sa.inspect(fresh_engine).get_table_names()
        finally:
            fresh_engine.dispose()

        output = StringIO()
        migration.output_buffer = output
        command.downgrade(migration, "head:0008_memory", sql=True)
        downgrade_sql = output.getvalue()
        assert "DROP TABLE change_log" in downgrade_sql
        for name in INDEXES:
            assert f"DROP INDEX {name}" in downgrade_sql
            assert downgrade_sql.index(f"DROP INDEX {name}") < \
                downgrade_sql.index("DROP TABLE change_log")
        assert "DROP TABLE memory" not in downgrade_sql
        assert "DROP TABLE person" not in downgrade_sql
    finally:
        engine.dispose()
