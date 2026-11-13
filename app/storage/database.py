"""Concentrated SQLite adapter; no business metadata or automatic create_all."""
import logging
from pathlib import Path
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase
from app.config.settings import AppConfig

logger = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

class Base(DeclarativeBase):
    """Persistence metadata only; ORM objects never leave repositories."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def load_models() -> None:
    from app.storage import models  # Register mappings for app and Alembic.


class DatabaseError(RuntimeError):
    """User-facing infrastructure failure; SQL and parameters stay out of logs."""


def log_database_error(operation: str, exc: Exception) -> None:
    original = getattr(exc, "orig", exc)
    logger.error("Database %s failed: type=%s driver_type=%s driver_code=%s; SQL/parameters omitted",
                 operation, type(exc).__name__, type(original).__name__,
                 getattr(original, "sqlite_errorname", getattr(original, "errno", "unknown")))


def database_url(config: AppConfig) -> URL:
    # URL.create safely handles spaces, Unicode, percent and Windows drive letters.
    return URL.create("sqlite+pysqlite", database=str(config.storage_paths.database))


def create_sqlite_engine(config: AppConfig) -> Engine:
    """Create one engine per application lifecycle; Sessions are never shared.

    isolation_level=None + explicit BEGIN avoids sqlite's legacy implicit-DDL
    commit behavior on supported Python 3.11+ (including transactional migrations).
    """
    paths = config.storage_paths
    try:
        for directory in (paths.data_dir, paths.media, paths.logs, paths.cache, paths.backups):
            directory.mkdir(parents=True, exist_ok=True)
        if paths.database.exists():
            # Windows file readonly attributes must fail explicitly, not silently
            # produce a read-only connection that fails only on the first save.
            with paths.database.open("r+b"):
                pass
        engine = create_engine(database_url(config), hide_parameters=True, echo=False,
                               connect_args={"timeout": config.busy_timeout_ms / 1000})
    except (OSError, ValueError) as exc:
        log_database_error("engine creation", exc)
        raise DatabaseError("无法创建或写入数据库目录/文件，请检查路径和文件权限。") from exc

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={config.busy_timeout_ms}")
            mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise DatabaseError("数据库无法启用 WAL，请使用支持 SQLite 本地锁的文件系统。")
            if cursor.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise DatabaseError("数据库无法启用外键约束。")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def explicit_begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.get_execution_options().get("ris_write") else "BEGIN")

    try:
        # Verify a write transaction is possible without adding any schema/data.
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("ROLLBACK")
            finally:
                cursor.close()
        finally:
            connection.close()
    except Exception as exc:
        engine.dispose()
        log_database_error("connection initialization", exc)
        raise DatabaseError("无法打开可写数据库。请检查文件权限、磁盘、文件格式或其他程序占用。") from exc
    logger.info("SQLite engine ready: foreign_keys=ON, WAL, busy_timeout=%s ms", config.busy_timeout_ms)
    return engine
