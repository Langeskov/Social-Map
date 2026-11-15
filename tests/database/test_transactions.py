"""Application transactions against independent, disposable test-only tables."""
import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError, SQLAlchemyError

from app.services.unit_of_work import transaction
from app.storage.database import DatabaseError


def assert_closed(engine, session):
    assert not session.in_transaction()
    assert engine.pool.checkedout() == 0
    with pytest.raises(InvalidRequestError, match="permanently closed"):
        session.execute(select(1))


def counts(factory, table_a, table_b):
    with factory() as reader:
        return tuple(reader.scalar(select(func.count()).select_from(table)) for table in (table_a, table_b))


def test_successful_a_and_b_are_committed_atomically(database_case):
    engine, factory, table_a, table_b = database_case
    with transaction(factory) as session:
        session.execute(table_a.insert().values(id=1, value="A"))
        session.execute(table_b.insert().values(id=1, a_id=1, value="B"))
        # A second Session cannot see either uncommitted write.
        assert counts(factory, table_a, table_b) == (0, 0)
    assert_closed(engine, session)
    assert counts(factory, table_a, table_b) == (1, 1)
    with factory() as first, factory() as second:
        assert first is not second
        assert first.scalar(select(table_a.c.value)) == "A"
        assert second.scalar(select(table_b.c.value)) == "B"
        assert first.connection().connection.driver_connection is not second.connection().connection.driver_connection


def test_normal_exception_rolls_back_both_writes_and_is_reraised_unchanged(database_case):
    engine, factory, table_a, table_b = database_case
    original = ValueError("test operation failed")
    with pytest.raises(ValueError) as failure:
        with transaction(factory) as session:
            session.execute(table_a.insert().values(id=1, value="A"))
            session.execute(table_b.insert().values(id=1, a_id=1, value="B"))
            raise original
    assert failure.value is original
    assert counts(factory, table_a, table_b) == (0, 0)
    assert_closed(engine, session)


def test_foreign_key_failure_is_translated_and_rolls_back_earlier_write(database_case):
    engine, factory, table_a, table_b = database_case
    with pytest.raises(DatabaseError, match="已回滚") as failure:
        with transaction(factory) as session:
            session.execute(table_a.insert().values(id=1, value="A"))
            session.execute(table_b.insert().values(id=1, a_id=999, value="B"))
    assert isinstance(failure.value.__cause__, IntegrityError)
    assert counts(factory, table_a, table_b) == (0, 0)
    assert_closed(engine, session)


def test_sqlalchemy_error_from_application_context_is_translated(database_case):
    engine, factory, table_a, table_b = database_case
    original = SQLAlchemyError("synthetic application-context failure")
    with pytest.raises(DatabaseError) as failure:
        with transaction(factory) as session:
            session.execute(table_a.insert().values(id=1, value="A"))
            raise original
    assert failure.value.__cause__ is original
    assert counts(factory, table_a, table_b) == (0, 0)
    assert_closed(engine, session)


def test_real_constraint_failure_never_logs_sql_or_sensitive_parameters(database_case, caplog):
    engine, factory, table_a, table_b = database_case
    secret = "sensitive-private-person-token-12345"
    with caplog.at_level(logging.ERROR, logger="app.storage.database"):
        with pytest.raises(DatabaseError) as failure:
            with transaction(factory) as session:
                session.execute(table_a.insert().values(id=1, value=secret))
                session.execute(table_a.insert().values(id=2, value=secret))
    assert isinstance(failure.value.__cause__, IntegrityError)
    assert secret not in caplog.text
    assert "INSERT INTO" not in caplog.text
    assert "test_atomic_a" not in caplog.text
    assert secret not in str(failure.value)
    assert "IntegrityError" in caplog.text
    assert "SQLITE_CONSTRAINT_UNIQUE" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert counts(factory, table_a, table_b) == (0, 0)
    assert_closed(engine, session)
