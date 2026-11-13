"""Storage adapter tests use only temporary files and independent metadata."""
import logging
import os
import sqlite3
import stat
import sys
from contextlib import ExitStack

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import StatementError

from app.config.settings import AppConfig
from app.storage.database import (
    Base,
    DatabaseError,
    create_sqlite_engine,
    log_database_error,
    load_models,
)


def test_engine_creates_required_directories_without_schema(tmp_path):
    load_models()
    formal_table_names = set(Base.metadata.tables)
    config = AppConfig(data_dir=tmp_path / "fresh" / "data")
    engine = create_sqlite_engine(config)
    try:
        paths = config.storage_paths
        for directory in (paths.data_dir, paths.media, paths.logs, paths.cache, paths.backups):
            assert directory.is_dir()
        assert paths.database.is_file()
        assert inspect(engine).get_table_names() == []
        assert set(Base.metadata.tables) == formal_table_names
        assert engine.hide_parameters is True
        assert engine.echo is False
    finally:
        engine.dispose()


@pytest.mark.parametrize("timeout", [0, 1234, 5000])
def test_every_pooled_connection_has_required_pragmas(tmp_path, timeout):
    engine = create_sqlite_engine(AppConfig(data_dir=tmp_path, busy_timeout_ms=timeout))
    try:
        # Concurrent checkouts force distinct DBAPI connections, not repeated
        # assertions against one pooled connection; reuse is checked as well.
        for _ in range(2):
            with ExitStack() as stack:
                connections = [stack.enter_context(engine.connect()) for _ in range(4)]
                assert len({id(c.connection.driver_connection) for c in connections}) == 4
                for connection in connections:
                    assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
                    assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                    assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == timeout
    finally:
        engine.dispose()


def test_invalid_database_file_raises_clear_database_error(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    config.storage_paths.database.write_bytes(b"not a sqlite database")
    with pytest.raises(DatabaseError, match="无法打开可写数据库") as failure:
        create_sqlite_engine(config)
    assert failure.value.__cause__ is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows readonly attribute behavior")
def test_windows_readonly_database_has_explicit_error_and_restores_attribute(tmp_path):
    config = AppConfig(data_dir=tmp_path)
    engine = create_sqlite_engine(config)
    engine.dispose()
    database = config.storage_paths.database
    original_mode = database.stat().st_mode
    try:
        os.chmod(database, stat.S_IREAD)
        assert not database.stat().st_mode & stat.S_IWRITE
        with pytest.raises(DatabaseError, match="文件权限") as failure:
            create_sqlite_engine(config)
        assert isinstance(failure.value.__cause__, PermissionError)
    finally:
        os.chmod(database, original_mode | stat.S_IWRITE)
    # The test's own temporary database is writable again even after failure.
    restored = create_sqlite_engine(config)
    restored.dispose()


def test_log_database_error_records_only_error_types_and_code(caplog):
    secret = "private-person-token-DO-NOT-LOG"
    statement = "INSERT INTO sensitive_records(secret) VALUES (:secret)"
    original = sqlite3.IntegrityError(secret)
    original.sqlite_errorname = "SQLITE_CONSTRAINT_FOREIGNKEY"
    original.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
    error = StatementError(secret, statement, {"secret": secret}, original)
    with caplog.at_level(logging.ERROR, logger="app.storage.database"):
        log_database_error("test operation", error)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    message = record.getMessage()
    assert "type=StatementError" in message
    assert "driver_type=IntegrityError" in message
    assert "driver_code=SQLITE_CONSTRAINT_FOREIGNKEY" in message
    assert secret not in caplog.text
    assert statement not in caplog.text
    assert "sensitive_records" not in caplog.text
    assert record.exc_info is None
    assert record.stack_info is None
