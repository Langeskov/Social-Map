"""Fixtures deliberately keep all test tables out of application Base.metadata."""
import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table

from app.config.settings import AppConfig
from app.storage.database import Base, create_sqlite_engine, load_models
from app.storage.session import create_session_factory


@pytest.fixture
def database_case(tmp_path, request):
    load_models()
    formal_table_names = set(Base.metadata.tables)
    timeout = getattr(request, "param", 5000)
    engine = create_sqlite_engine(AppConfig(data_dir=tmp_path, busy_timeout_ms=timeout))
    metadata = MetaData()
    table_a = Table(
        "test_atomic_a", metadata,
        Column("id", Integer, primary_key=True),
        Column("value", String, nullable=False, unique=True),
    )
    table_b = Table(
        "test_atomic_b", metadata,
        Column("id", Integer, primary_key=True),
        Column("a_id", Integer, ForeignKey("test_atomic_a.id"), nullable=False),
        Column("value", String, nullable=False),
    )
    assert metadata is not Base.metadata
    assert set(Base.metadata.tables) == formal_table_names
    assert set(metadata.tables).isdisjoint(formal_table_names)
    try:
        metadata.create_all(engine)
        yield engine, create_session_factory(engine), table_a, table_b
    finally:
        engine.dispose()
        assert set(Base.metadata.tables) == formal_table_names
