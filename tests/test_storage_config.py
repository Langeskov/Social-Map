from pathlib import Path
import pytest
from platformdirs import user_data_path
from app.config.paths import APP_NAME
from app.config.settings import AppConfig, ConfigError, load_config


def test_default_platformdirs_without_io():
    config = AppConfig()
    expected = user_data_path(APP_NAME, appauthor=False).resolve()
    assert config.data_dir == expected
    assert config.storage_paths.database == expected / "database.sqlite3"
    assert config.log_dir == expected / "logs"


def test_config_relative_paths_are_not_relative_to_cwd(tmp_path, monkeypatch):
    folder = tmp_path / "configuration"
    folder.mkdir()
    path = folder / "custom.toml"
    path.write_text('[storage]\ndata_dir="../local data"\ndatabase="数据%库.sqlite3"\nbusy_timeout_ms=1200\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = load_config(path)
    assert config.data_dir == (tmp_path / "local data").resolve()
    assert config.storage_paths.database == config.data_dir / "数据%库.sqlite3"
    assert config.log_dir == config.data_dir / "logs"
    assert config.busy_timeout_ms == 1200
    assert not config.data_dir.exists()  # Resolver must not create directories.


def test_absolute_data_root(tmp_path):
    path = tmp_path / "absolute.toml"
    destination = tmp_path / "other location"
    path.write_text(f'[storage]\ndata_dir="{destination.as_posix()}"\n', encoding="utf-8")
    assert load_config(path).data_dir == destination


def test_empty_storage_uses_os_defaults(tmp_path):
    path = tmp_path / "empty.toml"
    path.write_text('[storage]\ndata_dir=""\n', encoding="utf-8")
    assert load_config(path).data_dir == AppConfig().data_dir


def test_existing_explicit_logging_path_still_works(tmp_path):
    path = tmp_path / "logging.toml"
    path.write_text('[logging]\ndirectory="explicit-logs"\n[storage]\ndata_dir="data"\n', encoding="utf-8")
    config = load_config(path)
    assert config.log_dir == tmp_path / "explicit-logs"
    assert config.storage_paths.logs == tmp_path / "data" / "logs"


@pytest.mark.parametrize("content", [
    '[storage]\ndata_dir=3', '[storage]\ndatabase="../escape.sqlite3"',
    '[storage]\ndatabase="/absolute.sqlite3"', '[storage]\ndatabase="C:\\\\absolute.sqlite3"',
    '[storage]\ndatabase=""', '[storage]\ndatabase="."', '[storage]\ndatabase=".."',
    '[storage]\ndatabase=1', '[storage]\ndatabase=":memory:"',
    '[storage]\nbusy_timeout_ms=-1', '[storage]\nbusy_timeout_ms=true',
    '[storage]\nbusy_timeout_ms=60001', '[storage]\nunknown=1', 'storage=1',
])
def test_invalid_storage_config(tmp_path, content):
    path = tmp_path / "invalid.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
