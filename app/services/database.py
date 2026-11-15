"""Application lifecycle coordinates engine, migrations and session factory."""
from dataclasses import dataclass
import logging
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from app.config.settings import AppConfig
from app.storage.database import DatabaseError, create_sqlite_engine, log_database_error, load_models
from app.storage.migrations import alembic_config, current_heads, expected_heads
from app.storage.session import ThreadBoundSession, create_session_factory

logger = logging.getLogger(__name__)

@dataclass
class DatabaseRuntime:
    engine: Engine
    sessions: sessionmaker[ThreadBoundSession]

    def close(self):
        self.engine.dispose()


def initialize_database(config: AppConfig) -> DatabaseRuntime:
    """Fresh empty databases migrate; non-empty/unmatched schemas require CLI.

    No silent upgrading of an existing version. No metadata.create_all().
    """
    load_models()
    engine = create_sqlite_engine(config)
    try:
        heads = current_heads(engine)
        expected = expected_heads(config)
        if len(expected) != 1:
            raise DatabaseError("迁移脚本缺失或存在多个 head。请检查安装文件与迁移历史，程序不会初始化数据库。")
        if not heads:
            tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
            if tables:
                raise DatabaseError("数据库含有未纳入迁移管理的表。请先备份并检查数据库，程序不会自动修改它。")
            migration = alembic_config(config)
            with engine.begin() as connection:
                migration.attributes["connection"] = connection
                command.upgrade(migration, "head")
            logger.info("Empty database initialized through Alembic")
        if current_heads(engine) != expected:
            raise DatabaseError("数据库版本与程序不匹配。请先备份，再使用相同配置执行 Alembic upgrade head。")
        logger.info("Database schema version verified")
        return DatabaseRuntime(engine, create_session_factory(engine))
    except Exception as exc:
        engine.dispose()
        log_database_error("schema initialization", exc)
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError("数据库迁移初始化失败。请检查配置和错误日志；程序未继续启动。") from exc
