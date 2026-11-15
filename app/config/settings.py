"""Validated local TOML configuration; no business or credential settings."""
from dataclasses import dataclass
from pathlib import Path
import tomllib
from app.config.paths import StoragePaths, resolve_path, resolve_storage_paths

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class ConfigError(ValueError):
    """Configuration cannot be loaded or validated."""

@dataclass(frozen=True)
class AppConfig:
    title: str = "Relationship Intelligence System"
    width: int = 1120
    height: int = 760
    log_level: str = "INFO"
    log_dir: Path | None = None
    data_dir: Path | None = None
    database: str = "database.sqlite3"
    busy_timeout_ms: int = 5000

    def __post_init__(self):
        # Direct construction uses the project root as relative-path base.
        # load_config resolves explicit paths against its configuration file first.
        if not isinstance(self.database, str) or not self.database.strip() or self.database in {".", "..", ":memory:"} or any(char in self.database for char in '/\\\\:'):
            raise ConfigError("storage.database 必须为数据根目录内的普通文件名")
        if type(self.busy_timeout_ms) is not int or not 0 <= self.busy_timeout_ms <= 60000:
            raise ConfigError("storage.busy_timeout_ms 必须为 0 至 60000 的整数")
        paths = resolve_storage_paths(self.data_dir, self.database, base=PROJECT_ROOT)
        object.__setattr__(self, "data_dir", paths.data_dir)
        object.__setattr__(self, "log_dir", resolve_path(self.log_dir, base=PROJECT_ROOT) if self.log_dir is not None else paths.logs)

    @property
    def storage_paths(self) -> StoragePaths:
        return resolve_storage_paths(self.data_dir, self.database, base=PROJECT_ROOT)


def load_config(path: Path | None = None) -> AppConfig:
    source = Path(path) if path is not None else PROJECT_ROOT / "config.toml"
    if not source.exists():
        if path is not None:
            raise ConfigError(f"配置文件不存在：{source}")
        return AppConfig()
    try:
        with source.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置：{source} ({exc})") from exc
    if set(data) - {"app", "logging", "storage"}:
        raise ConfigError("配置只支持 [app]、[logging] 和 [storage]")
    app, logs, storage = data.get("app", {}), data.get("logging", {}), data.get("storage", {})
    if not all(isinstance(table, dict) for table in (app, logs, storage)):
        raise ConfigError("app、logging 和 storage 必须是 TOML 表")
    if set(app) - {"title", "width", "height"} or set(logs) - {"level", "directory"} or set(storage) - {"data_dir", "database", "busy_timeout_ms"}:
        raise ConfigError("配置包含不支持的字段")
    title = app.get("title", AppConfig.title)
    width, height = app.get("width", 1120), app.get("height", 760)
    level, directory = logs.get("level", "INFO"), logs.get("directory")
    data_dir = storage.get("data_dir", "")
    if not isinstance(title, str) or not title.strip():
        raise ConfigError("app.title 必须是非空字符串")
    if type(width) is not int or type(height) is not int or width < 800 or height < 560:
        raise ConfigError("窗口尺寸必须为整数，且不小于 800 × 560")
    if not isinstance(level, str) or level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("logging.level 无效")
    if directory is not None and (not isinstance(directory, str) or not directory.strip()):
        raise ConfigError("logging.directory 必须是非空路径")
    if not isinstance(data_dir, str):
        raise ConfigError("storage.data_dir 必须是字符串路径，空字符串表示系统默认目录")
    base = source.resolve().parent
    return AppConfig(
        title=title.strip(), width=width, height=height, log_level=level.upper(),
        log_dir=resolve_path(directory, base=base) if directory is not None else None,
        data_dir=resolve_path(data_dir, base=base) if data_dir else None,
        database=storage.get("database", "database.sqlite3"),
        busy_timeout_ms=storage.get("busy_timeout_ms", 5000),
    )
