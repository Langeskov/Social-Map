"""Migration environment: never maintain a separate development database URL."""
from alembic import context
from app.config.settings import load_config
from app.logging_setup import configure_logging
from app.storage.database import Base, create_sqlite_engine, database_url, load_models
from pathlib import Path

config = context.config
app_config = config.attributes.get("app_config")
if app_config is None:
    explicit = context.get_x_argument(as_dictionary=True).get("config")
    app_config = load_config(Path(explicit) if explicit else None)
    configure_logging(app_config)
from app.storage.types import UTCDateTime

load_models()
target_metadata = Base.metadata


def render_item(kind, value, context):
    if kind == "type" and isinstance(value, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


PROJECTION_TABLE = "search_document_fts"
PROJECTION_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def include_object(_obj, name, type_, _reflected, _compare_to):
    """Keep SQLite's own FTS5 objects out of autogenerate.

    The Phase 7 search index is infrastructure the SQLite engine materialises from
    migration 0006 (plus the five shadow tables it creates), not ORM metadata. It is
    deliberately unmapped, so an unfiltered ``alembic check``/``--autogenerate`` would
    propose dropping the whole search index. ``search_document`` itself is a mapped
    projection table and stays comparable.
    """
    if type_ != "table":
        return True
    if name == PROJECTION_TABLE:
        return False
    return not (name.startswith(PROJECTION_TABLE + "_") and name.endswith(PROJECTION_SHADOW_SUFFIXES))


def run(connection):
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, render_as_batch=True, render_item=render_item,
                      transaction_per_migration=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    context.configure(url=database_url(app_config), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"},
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = config.attributes.get("connection")
    if connection is not None:
        run(connection)
    else:
        engine = create_sqlite_engine(app_config)
        try:
            with engine.connect() as connection:
                run(connection)
        finally:
            engine.dispose()
