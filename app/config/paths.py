"""One path policy shared by application and migration tooling (no IO)."""
from dataclasses import dataclass
from pathlib import Path
from platformdirs import user_data_path

APP_NAME = "Relationship Intelligence System"

@dataclass(frozen=True)
class StoragePaths:
    data_dir: Path
    database: Path
    media: Path
    logs: Path
    cache: Path
    backups: Path


def resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def resolve_storage_paths(data_dir: str | Path | None, database: str, *, base: Path) -> StoragePaths:
    root = resolve_path(data_dir, base=base) if data_dir else user_data_path(APP_NAME, appauthor=False).resolve()
    return StoragePaths(root, root / database, root / "media", root / "logs", root / "cache", root / "backups")
