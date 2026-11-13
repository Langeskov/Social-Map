"""Session lifetimes and thread ownership through the public factory."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident

import pytest
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError

from app.config.settings import AppConfig
from app.services.unit_of_work import transaction
from app.storage.database import create_sqlite_engine
from app.storage.session import ThreadBoundSession, create_session_factory


@pytest.fixture
def session_engine(tmp_path):
    engine = create_sqlite_engine(AppConfig(data_dir=tmp_path))
    try:
        yield engine, create_session_factory(engine)
    finally:
        engine.dispose()


def test_session_factory_creates_independent_permanently_closed_sessions(session_engine):
    engine, factory = session_engine
    first, second = factory(), factory()
    try:
        assert isinstance(first, ThreadBoundSession)
        assert isinstance(second, ThreadBoundSession)
        assert first is not second
        assert first.scalar(select(1)) == second.scalar(select(1)) == 1
        assert engine.pool.checkedout() == 2
        first.close()
        assert engine.pool.checkedout() == 1
        with pytest.raises(InvalidRequestError, match="permanently closed"):
            first.execute(select(1))
        assert second.scalar(select(2)) == 2
    finally:
        first.close()
        second.close()
    assert engine.pool.checkedout() == 0


def test_parallel_threads_create_their_own_sessions(session_engine):
    engine, factory = session_engine
    barrier = Barrier(2)

    def worker():
        with transaction(factory) as session:
            connection = session.connection().connection.driver_connection
            barrier.wait(timeout=10)
            value = session.scalar(select(42))
            owner = get_ident()
        with pytest.raises(InvalidRequestError, match="permanently closed"):
            session.scalar(select(1))
        return session, connection, owner, value

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        first, second = [future.result(timeout=20) for future in futures]
    assert first[0] is not second[0]
    assert first[1] is not second[1]
    assert first[2] != second[2]
    assert first[2] != get_ident()
    assert first[3] == second[3] == 42
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("operation", [
    "execute", "scalar", "scalars", "connection", "begin", "begin_nested",
    "commit", "rollback", "flush", "close", "add", "add_all", "delete", "get", "merge",
])
def test_session_rejects_cross_thread_operations(session_engine, operation):
    engine, factory = session_engine
    session = factory()
    calls = {
        "execute": lambda: session.execute(select(1)),
        "scalar": lambda: session.scalar(select(1)),
        "scalars": lambda: session.scalars(select(1)),
        "connection": session.connection,
        "begin": session.begin,
        "begin_nested": session.begin_nested,
        "commit": session.commit,
        "rollback": session.rollback,
        "flush": session.flush,
        "close": session.close,
        "add": lambda: session.add(None),
        "add_all": lambda: session.add_all([]),
        "delete": lambda: session.delete(None),
        "get": lambda: session.get(None, 1),
        "merge": lambda: session.merge(None),
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match="不得跨线程"):
                executor.submit(calls[operation]).result(timeout=10)
        # A rejected foreign-thread call must not poison the owner's Session.
        assert session.scalar(select(7)) == 7
    finally:
        session.close()
    assert engine.pool.checkedout() == 0
