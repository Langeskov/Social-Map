"""SQLite concurrency checks use only independent temporary test tables."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError, OperationalError

from app.services.unit_of_work import transaction
from app.storage.database import DatabaseError


def test_wal_reader_keeps_committed_snapshot_during_writer(database_case):
    engine, factory, table_a, table_b = database_case
    with transaction(factory) as seed:
        seed.execute(table_a.insert().values(id=1, value="committed-A"))
        seed.execute(table_b.insert().values(id=1, a_id=1, value="committed-B"))
    with factory() as reader:
        with transaction(factory) as writer:
            writer.execute(table_a.update().values(value="new-A"))
            writer.execute(table_b.update().values(value="new-B"))
            # A distinct DBAPI connection reads the committed database while
            # the writer retains its lock, not its uncommitted new values.
            assert reader.scalar(select(table_a.c.value)) == "committed-A"
            assert reader.scalar(select(table_b.c.value)) == "committed-B"
            assert reader.connection().connection.driver_connection is not writer.connection().connection.driver_connection
        # WAL readers retain the snapshot across the writer's successful commit.
        assert reader.scalar(select(table_a.c.value)) == "committed-A"
        assert reader.scalar(select(table_b.c.value)) == "committed-B"
    with factory() as fresh:
        assert fresh.scalar(select(table_a.c.value)) == "new-A"
        assert fresh.scalar(select(table_b.c.value)) == "new-B"
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("database_case", [75], indirect=True)
def test_busy_timeout_competing_writer_rolls_back_and_closes(database_case):
    engine, factory, table_a, table_b = database_case
    completed_sessions = []

    def competing_writer():
        try:
            with transaction(factory) as contender:
                completed_sessions.append(contender)
                contender.execute(table_a.insert().values(id=2, value="losing-A"))
                contender.execute(table_b.insert().values(id=2, a_id=2, value="losing-B"))
        finally:
            with pytest.raises(InvalidRequestError, match="permanently closed"):
                completed_sessions[-1].scalar(select(1))

    with transaction(factory) as writer:
        writer.execute(table_a.insert().values(id=1, value="winning-A"))
        writer.execute(table_b.insert().values(id=1, a_id=1, value="winning-B"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(DatabaseError, match="已回滚") as failure:
                executor.submit(competing_writer).result(timeout=10)
        cause = failure.value.__cause__
        assert isinstance(cause, OperationalError)
        assert cause.orig.sqlite_errorname == "SQLITE_BUSY"
        # Only the original writer remains checked out after the failed UoW.
        assert engine.pool.checkedout() == 1
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(table_a)) == 1
        assert reader.scalar(select(func.count()).select_from(table_b)) == 1
        assert reader.scalar(select(table_a.c.value)) == "winning-A"
        assert reader.scalar(select(table_b.c.value)) == "winning-B"
    # A new operation succeeds after the original lock is released.
    with transaction(factory) as retry:
        retry.execute(table_a.insert().values(id=2, value="retry-A"))
        retry.execute(table_b.insert().values(id=2, a_id=2, value="retry-B"))
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(table_a)) == 2
        assert reader.scalar(select(func.count()).select_from(table_b)) == 2
    assert engine.pool.checkedout() == 0
