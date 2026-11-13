import pytest
from sqlalchemy import event, select
from app.config.settings import AppConfig
from app.storage.database import create_sqlite_engine
from app.storage.session import create_session_factory
from app.services.unit_of_work import transaction
from app.services.operations import write_operation


def test_read_and_write_begin_modes_and_no_pool_option_leak(tmp_path):
    engine = create_sqlite_engine(AppConfig(data_dir=tmp_path))
    statements = []
    @event.listens_for(engine, "before_cursor_execute")
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.startswith("BEGIN"):
            statements.append(statement)
    factory = create_session_factory(engine)
    try:
        with transaction(factory) as session:
            assert session.info["ris_write"] is False
            assert session.scalar(select(1)) == 1
        with write_operation(factory) as session:
            assert session.info["ris_write"] is True
            assert session.scalar(select(2)) == 2
        with transaction(factory) as session:
            assert session.scalar(select(3)) == 3
        assert statements == ["BEGIN", "BEGIN IMMEDIATE", "BEGIN"]
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_reserved_write_transaction_rolls_back_and_releases_lock(tmp_path):
    engine = create_sqlite_engine(AppConfig(data_dir=tmp_path))
    factory = create_session_factory(engine)
    try:
        with pytest.raises(ValueError):
            with write_operation(factory) as session:
                session.scalar(select(1))
                raise ValueError("technical test failure")
        with write_operation(factory) as session:
            assert session.scalar(select(2)) == 2
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
