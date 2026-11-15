import logging
import subprocess
import sys
import pytest
from sqlalchemy import inspect
from app.config.settings import AppConfig, PROJECT_ROOT
from app.logging_setup import configure_logging
from app.services import database as service
from app.storage.database import DatabaseError, create_sqlite_engine


@pytest.mark.parametrize("heads", [(), ("branch-a", "branch-b")])
def test_missing_or_ambiguous_migration_head_is_rejected(tmp_path, monkeypatch, heads):
    config = AppConfig(data_dir=tmp_path)
    monkeypatch.setattr(service, "expected_heads", lambda config: heads)
    with pytest.raises(DatabaseError, match="迁移脚本缺失"):
        service.initialize_database(config)
    engine = create_sqlite_engine(config)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_directory_creation_failure_has_explicit_file_log(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("sentinel", encoding="utf-8")
    config = AppConfig(data_dir=blocker / "data", log_dir=tmp_path / "logs")
    logger = configure_logging(config)
    try:
        with pytest.raises(DatabaseError, match="路径和文件权限"):
            create_sqlite_engine(config)
        for handler in logger.handlers:
            handler.flush()
        errors = (config.log_dir / "error.log").read_text(encoding="utf-8")
        assert "engine creation" in errors
        assert "driver_type=" in errors
        assert "sentinel" not in errors
        assert blocker.read_text(encoding="utf-8") == "sentinel"
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_offline_migration_sql_does_not_create_database(tmp_path):
    config = tmp_path / "offline.toml"
    config.write_text('[storage]\ndata_dir="offline-data"\n', encoding="utf-8")
    result = subprocess.run([sys.executable, "-s", "-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"),
                             "-x", f"config={config}", "upgrade", "head", "--sql"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0001_empty_initial" in result.stdout
    assert not (tmp_path / "offline-data" / "database.sqlite3").exists()
