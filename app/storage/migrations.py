"""Shared Alembic configuration and schema-version inspection."""
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine
from app.config.settings import AppConfig, PROJECT_ROOT


def alembic_config(config: AppConfig) -> Config:
    migration = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(PROJECT_ROOT / "alembic").replace("%", "%%"))
    migration.attributes["app_config"] = config
    return migration


def current_heads(engine: Engine) -> tuple[str, ...]:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_heads()


def expected_heads(config: AppConfig) -> tuple[str, ...]:
    return tuple(ScriptDirectory.from_config(alembic_config(config)).get_heads())
