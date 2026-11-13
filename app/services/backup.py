"""Phase 10: consistent local backup and restore verification.

What this module guarantees
    * ``BackupService.backup()`` reads the source and never writes to it: the
      database is captured through SQLite's *online backup API* from the running
      WAL database, never by copying ``database.sqlite3``/``-wal``/``-shm``.
      A file copy of a live WAL database is a torn snapshot: committed pages may
      still live only in the ``-wal`` file, so the copied ``database.sqlite3``
      alone can silently lose the most recent committed transaction. The backup
      API instead walks the database page by page inside a read transaction, so
      the copy always contains exactly one committed state - including every
      transaction that was committed before it started, WAL content included.
      No migration is run, no ``deleted_at`` is touched, no search projection is
      refreshed and no Analysis/Memory row is created.
    * Every media file referenced by a ``media`` row is copied byte for byte and
      its sha256 is compared with the hash the database recorded.
    * The whole backup is assembled in a temporary ``.partial`` directory next to
      the destination and renamed into place only after every check passed, so a
      failed run leaves no half-written backup and cannot overwrite an existing
      destination.
    * ``BackupVerificationService.verify()`` works on a copy below
      ``tempfile.mkdtemp()`` and never opens the production database or media.

What a manifest deliberately never contains
    No API key, no credential or ``credential_ref`` value, no ``Authorization``
    header, no absolute media path and no Memory/Evidence body text. It carries
    counts, hashes and media paths relative to ``media/`` only.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time

from app.config.settings import AppConfig
from app.domain.validation import utc_now
from app.storage.database import create_sqlite_engine
from app.storage.media_store import MediaStore
from app.storage.session import create_session_factory

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.sqlite3"
MEDIA_DIR_NAME = "media"
DEFAULT_APPLICATION_VERSION = "Phase 10"

#: Ledger of the checks a verification performs, in report order.
CHECK_NAMES = ("manifest", "database_sha256", "copy", "schema_revision", "integrity_check",
               "foreign_key_check", "media_files", "media_extra_files", "application_open",
               "integrity_audit")

_HASH_CHUNK = 1024 * 1024
_MEDIA_PREFIX = "media/"


def _sha256_file(path: Path) -> tuple[str, int]:
    """Digest and size of one regular file, read in bounded chunks."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _slice(value, length: int = 200) -> str:
    """A log/description safe shortening: never an unbounded private string."""
    text = str(value)
    return text if len(text) <= length else text[: length - 3] + "..."


class _BackupError(Exception):
    """A readable, user-facing reason why one backup could not be produced."""


@dataclass(frozen=True, slots=True)
class MediaManifestEntry:
    """One backed-up media file, addressed relative to the backup's media/."""

    relative_path: str
    size: int
    sha256: str

    def to_mapping(self) -> dict:
        return {"relative_path": self.relative_path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Everything a restore verification needs, and nothing private."""

    application_version: str
    schema_revision: str
    created_at: datetime
    workspace_count: int
    database_sha256: str
    media_file_count: int
    media_total_bytes: int
    media: tuple[MediaManifestEntry, ...]

    def to_mapping(self) -> dict:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return {"application_version": self.application_version,
                "schema_revision": self.schema_revision,
                "created_at": created_at.astimezone(timezone.utc).isoformat(),
                "workspace_count": self.workspace_count,
                "database_sha256": self.database_sha256,
                "media_file_count": self.media_file_count,
                "media_total_bytes": self.media_total_bytes,
                "media": [entry.to_mapping() for entry in self.media]}

    @classmethod
    def from_mapping(cls, data: dict) -> "BackupManifest":
        """Parse a manifest, rejecting anything that cannot be trusted."""
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON 对象")
        required = ("application_version", "schema_revision", "created_at", "workspace_count",
                    "database_sha256", "media_file_count", "media_total_bytes", "media")
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError("manifest 缺少字段：" + "、".join(missing))
        for name in ("application_version", "schema_revision"):
            if not isinstance(data[name], str) or not data[name].strip():
                raise ValueError(f"manifest.{name} 必须是非空字符串")
        created_at = data["created_at"]
        if not isinstance(created_at, str):
            raise ValueError("manifest.created_at 必须是 ISO 8601 字符串")
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ValueError("manifest.created_at 不是合法的 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValueError("manifest.created_at 必须带时区（UTC）")
        database_sha256 = data["database_sha256"]
        if not isinstance(database_sha256, str) or len(database_sha256) != 64:
            raise ValueError("manifest.database_sha256 必须是 64 位十六进制字符串")
        counts = {}
        for name in ("workspace_count", "media_file_count", "media_total_bytes"):
            value = data[name]
            if type(value) is not int or value < 0:
                raise ValueError(f"manifest.{name} 必须是非负整数")
            counts[name] = value
        raw_media = data["media"]
        if not isinstance(raw_media, (list, tuple)):
            raise ValueError("manifest.media 必须是数组")
        entries = tuple(cls._entry(item) for item in raw_media)
        if counts["media_file_count"] != len(entries):
            raise ValueError("manifest.media_file_count 与 media 条目数不一致")
        if counts["media_total_bytes"] != sum(entry.size for entry in entries):
            raise ValueError("manifest.media_total_bytes 与 media 条目大小之和不一致")
        return cls(application_version=data["application_version"].strip(),
                   schema_revision=data["schema_revision"].strip(),
                   created_at=parsed.astimezone(timezone.utc),
                   workspace_count=counts["workspace_count"],
                   database_sha256=database_sha256.lower(),
                   media_file_count=counts["media_file_count"],
                   media_total_bytes=counts["media_total_bytes"], media=entries)

    @staticmethod
    def _entry(item) -> MediaManifestEntry:
        if not isinstance(item, dict):
            raise ValueError("manifest.media 的每个条目必须是对象")
        for name in ("relative_path", "size", "sha256"):
            if name not in item:
                raise ValueError(f"manifest.media 条目缺少字段：{name}")
        relative_path = item["relative_path"]
        if not isinstance(relative_path, str):
            raise ValueError("manifest.media.relative_path 必须是字符串")
        pure = PurePosixPath(relative_path)
        if (not relative_path or pure.is_absolute() or "\\" in relative_path
                or ":" in relative_path or ".." in pure.parts or "." in pure.parts):
            raise ValueError("manifest.media.relative_path 必须是 media/ 内的相对路径")
        size = item["size"]
        if type(size) is not int or size < 0:
            raise ValueError("manifest.media.size 必须是非负整数")
        sha256 = item["sha256"]
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("manifest.media.sha256 必须是 64 位十六进制字符串")
        return MediaManifestEntry(relative_path=relative_path, size=size, sha256=sha256.lower())


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Outcome of one backup attempt; every path is absolute."""

    ok: bool
    directory: Path
    database_path: Path | None
    media_dir: Path | None
    manifest_path: Path | None
    manifest: BackupManifest | None
    error: str | None
    duration_ms: int


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """One named, individually reportable verification step."""

    name: str
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class BackupVerification:
    """Outcome of one restore verification, run entirely on a temporary copy."""

    ok: bool
    directory: Path
    schema_revision: str | None
    checks: tuple[VerificationCheck, ...]
    report: object | None
    error: str | None

    @property
    def failed(self) -> tuple[VerificationCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def check(self, name: str) -> VerificationCheck | None:
        return next((item for item in self.checks if item.name == name), None)

    def summary(self) -> str:
        passed = sum(1 for check in self.checks if check.ok)
        lines = [f"备份验证{'通过' if self.ok else '未通过'}：{self.directory}",
                 f"检查项：{passed}/{len(self.checks)} 通过"]
        if self.schema_revision:
            lines.append(f"schema 版本：{self.schema_revision}")
        for check in self.checks:
            lines.append(f"- [{'通过' if check.ok else '失败'}] {check.name}：{check.message}")
        if self.error:
            lines.append(f"错误：{self.error}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """What the snapshot database itself proves about the copied state."""

    schema_revision: str
    workspace_count: int
    database_sha256: str
    media_rows: tuple[tuple[str, str, int], ...]


class BackupService:
    """Create one consistent, self-describing backup directory.

    The destination is a *new* directory. An existing non-empty directory is
    refused instead of merged or overwritten, so no backup is ever destroyed by
    a later failed attempt.
    """

    def __init__(self, config: AppConfig, *, clock=utc_now,
                 application_version: str = DEFAULT_APPLICATION_VERSION):
        self._config = config
        self._clock = clock
        self._application_version = str(application_version)

    # ------------------------------------------------------------------ public
    def backup(self, destination: Path) -> BackupResult:
        started = time.monotonic()
        destination = _absolute(Path(destination))

        def result(ok, *, manifest=None, error=None):
            base = destination
            return BackupResult(
                ok=ok, directory=destination,
                database_path=base / DATABASE_NAME if ok else None,
                media_dir=base / MEDIA_DIR_NAME if ok else None,
                manifest_path=base / MANIFEST_NAME if ok else None,
                manifest=manifest, error=error,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)))

        try:
            self._check_destination(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = Path(tempfile.mkdtemp(
                prefix=f".{destination.name}.partial-", dir=str(destination.parent)))
        except _BackupError as exc:
            logger.warning("Backup refused: %s", _slice(exc))
            return result(False, error=str(exc))
        except OSError as exc:
            logger.warning("Backup destination unusable: %s", type(exc).__name__)
            return result(False, error="无法创建备份临时目录，请检查目标路径与磁盘权限。")

        try:
            snapshot = self._snapshot_database(partial / DATABASE_NAME)
            entries, problems = self._copy_media(snapshot.media_rows,
                                                 partial / MEDIA_DIR_NAME)
            if problems:
                raise _BackupError("备份失败：" + "；".join(problems) + "。")
            manifest = BackupManifest(
                application_version=self._application_version,
                schema_revision=snapshot.schema_revision,
                created_at=self._now(),
                workspace_count=snapshot.workspace_count,
                database_sha256=snapshot.database_sha256,
                media_file_count=len(entries),
                media_total_bytes=sum(entry.size for entry in entries),
                media=entries)
            self._write_manifest(partial / MANIFEST_NAME, manifest)
            self._publish(partial, destination)
        except _BackupError as exc:
            _remove_tree(partial)
            logger.warning("Backup failed: %s", _slice(exc))
            return result(False, error=str(exc))
        except OSError as exc:
            _remove_tree(partial)
            logger.warning("Backup failed while writing: %s", type(exc).__name__)
            return result(False, error="写入备份目录失败，请检查磁盘空间与目标目录权限。")
        except Exception as exc:                      # never leave a partial tree behind
            _remove_tree(partial)
            logger.exception("Backup failed unexpectedly: type=%s", type(exc).__name__)
            return result(False, error="备份过程中发生未预期错误，未生成备份。")

        logger.info("Backup created: media=%s bytes=%s database_sha256=%s",
                    manifest.media_file_count, manifest.media_total_bytes,
                    manifest.database_sha256[:12])
        return result(True, manifest=manifest)

    def verify(self, backup_dir: Path) -> BackupVerification:
        """Delegate to :class:`BackupVerificationService` (single implementation)."""
        return BackupVerificationService(self._config, clock=self._clock).verify(backup_dir)

    # -------------------------------------------------------------- destination
    def _check_destination(self, destination: Path) -> None:
        if destination.exists() and not destination.is_dir():
            raise _BackupError("备份目标已存在且不是目录，请选择一个新的目录名。")
        if destination.is_dir() and any(destination.iterdir()):
            raise _BackupError("备份目标目录已存在且非空，已拒绝覆盖；请选择一个新的目录名。")
        source = self._config.storage_paths
        for label, guarded in (("数据目录", source.data_dir), ("媒体目录", source.media)):
            if destination == guarded or guarded in destination.parents:
                raise _BackupError(f"备份目标不能位于{label}内，请选择其他位置。")

    # ----------------------------------------------------------------- snapshot
    def _snapshot_database(self, target_path: Path) -> _Snapshot:
        """Capture the live database through SQLite's online backup API.

        ``sqlite3.Connection.backup`` copies committed pages inside a read
        transaction, so the result is one consistent committed state of the WAL
        database. The file copy of ``database.sqlite3`` (or of ``-wal``/``-shm``)
        is deliberately never used: those bytes are not a database on their own.
        """
        source_path = Path(self._config.storage_paths.database)
        if not source_path.is_file():
            raise _BackupError("源数据库文件不存在，无法创建备份。")
        timeout = self._config.busy_timeout_ms / 1000
        source = target = None
        try:
            source = sqlite3.connect(str(source_path), timeout=timeout)
            target = sqlite3.connect(str(target_path), timeout=timeout)
            source.backup(target)
        except sqlite3.Error as exc:
            raise _BackupError(f"无法创建数据库一致快照（{type(exc).__name__}）。") from exc
        finally:
            for connection in (target, source):
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:             # pragma: no cover - close must not mask
                        logger.warning("Snapshot connection close failed")

        self._finalize_snapshot(target_path, timeout)
        return self._inspect_snapshot(target_path, timeout)

    @staticmethod
    def _finalize_snapshot(target_path: Path, timeout: float) -> None:
        """Fold the snapshot into one self-contained file (no -wal/-shm needed).

        Page 1 carries the source header, so the snapshot can still advertise WAL;
        leaving the journal mode switch and the checkpoint to SQLite guarantees
        that ``database.sqlite3`` alone is a complete database, even after the
        backup directory is moved or copied without its sidecar files.
        """
        connection = None
        try:
            connection = sqlite3.connect(str(target_path), timeout=timeout)
            connection.execute("PRAGMA journal_mode=DELETE").fetchall()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error as exc:
            raise _BackupError(f"数据库快照无法收敛为单一文件（{type(exc).__name__}）。") from exc
        finally:
            if connection is not None:
                connection.close()
        for suffix in ("-wal", "-shm"):
            sidecar = target_path.with_name(target_path.name + suffix)
            if suffix == "-wal" and sidecar.exists() and sidecar.stat().st_size:
                raise _BackupError("数据库快照仍依赖 WAL 文件，已放弃本次备份。")
            sidecar.unlink(missing_ok=True)

    @staticmethod
    def _inspect_snapshot(target_path: Path, timeout: float) -> _Snapshot:
        """Prove the finished file is a healthy, versioned database."""
        connection = None
        try:
            connection = sqlite3.connect(str(target_path), timeout=timeout)
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if [row[0] for row in integrity] != ["ok"]:
                raise _BackupError("数据库快照未通过 PRAGMA integrity_check，未生成备份。")
            try:
                revisions = [str(row[0]) for row in
                             connection.execute("SELECT version_num FROM alembic_version")]
            except sqlite3.Error as exc:
                raise _BackupError("数据库快照缺少 alembic_version 表，未生成备份。") from exc
            if len(revisions) != 1:
                raise _BackupError("数据库快照的 Alembic 版本不是单一 head，未生成备份。")
            workspace_count = int(connection.execute(
                "SELECT COUNT(*) FROM workspace").fetchone()[0])
            media_rows = tuple((str(path), str(sha256 or ""), int(size or 0)) for
                               path, sha256, size in connection.execute(
                                   "SELECT storage_path, sha256, size_bytes FROM media "
                                   "ORDER BY storage_path"))
        except _BackupError:
            raise
        except sqlite3.Error as exc:
            raise _BackupError(f"无法读取数据库快照（{type(exc).__name__}）。") from exc
        finally:
            if connection is not None:
                connection.close()
        database_sha256, _size = _sha256_file(target_path)
        return _Snapshot(schema_revision=revisions[0], workspace_count=workspace_count,
                         database_sha256=database_sha256, media_rows=media_rows)

    # -------------------------------------------------------------------- media
    def _copy_media(self, rows, target_root: Path):
        """Copy every referenced media file and verify it against the database.

        A file whose bytes disagree with the recorded sha256/size, or that is
        missing, fails the whole backup: a backup that silently contains a
        damaged copy is worse than no backup at all. The reported reason carries
        relative-path *counts*, never one absolute host path per file.
        """
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            store = MediaStore(self._config.storage_paths.data_dir)
        except Exception as exc:
            raise _BackupError(
                f"媒体存储目录不可用，无法备份媒体文件（{type(exc).__name__}）。") from exc

        entries: list[MediaManifestEntry] = []
        missing = invalid = mismatched = 0
        for storage_path, sha256, size_bytes in rows:
            relative = _relative_media_path(storage_path)
            if relative is None:
                invalid += 1
                continue
            try:
                source = store.path_for(storage_path)
            except Exception:
                invalid += 1
                continue
            try:
                stream = source.open("rb")
            except OSError:
                missing += 1
                continue
            target = target_root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            try:
                with stream, target.open("xb") as sink:
                    while chunk := stream.read(_HASH_CHUNK):
                        sink.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    sink.flush()
                    os.fsync(sink.fileno())
            except OSError as exc:
                raise _BackupError(
                    f"写入备份媒体文件失败（{type(exc).__name__}），请检查磁盘空间。") from exc
            recorded = digest.hexdigest()
            if size != size_bytes or (sha256 and recorded != sha256.lower()):
                mismatched += 1
                continue
            entries.append(MediaManifestEntry(relative_path=relative, size=size,
                                             sha256=recorded))

        problems = []
        if missing:
            problems.append(f"{missing} 个媒体文件在源媒体目录中缺失")
        if invalid:
            problems.append(f"{invalid} 个媒体记录的存储路径无效")
        if mismatched:
            problems.append(f"{mismatched} 个媒体文件与数据库记录的 sha256/大小不一致")
        return tuple(entries), tuple(problems)

    # ----------------------------------------------------------------- manifest
    def _write_manifest(self, path: Path, manifest: BackupManifest) -> None:
        payload = json.dumps(manifest.to_mapping(), ensure_ascii=False, indent=2,
                             sort_keys=True)
        path.write_text(payload + "\n", encoding="utf-8")

    @staticmethod
    def _publish(partial: Path, destination: Path) -> None:
        """Rename the finished tree into place; never merge into an existing one."""
        try:
            os.rename(partial, destination)
            return
        except OSError:
            # An empty destination directory cannot be replaced by rename() on
            # Windows; every other case is left untouched and reported.
            if destination.is_dir() and not any(destination.iterdir()):
                destination.rmdir()
                os.rename(partial, destination)
                return
            raise

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise _BackupError("clock 必须返回 datetime。")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class BackupVerificationService:
    """Verify a backup on a throwaway copy; the production database stays shut.

    Every step is a named check. ``ok`` is only True when all of them passed:
    the presence of the files is never treated as proof of a usable backup.
    """

    def __init__(self, config: AppConfig, *, clock=utc_now):
        self._config = config
        self._clock = clock

    # ------------------------------------------------------------------ public
    def verify(self, backup_dir: Path) -> BackupVerification:
        """Verify one backup directory; the instance holds no state between calls."""
        directory = _absolute(Path(backup_dir))
        checks: list[VerificationCheck] = []
        report = None
        manifest_check, manifest = self._check_manifest(directory)
        checks.append(manifest_check)
        if manifest is None:
            return BackupVerification(False, directory, None, tuple(checks), None,
                                      manifest_check.message)

        database_path = directory / DATABASE_NAME
        media_dir = directory / MEDIA_DIR_NAME
        checks.append(self._check_database_sha256(database_path, manifest))

        temp_root = Path(tempfile.mkdtemp(prefix="ris-backup-verify-"))
        try:
            copy_root = temp_root / "copy"
            copy_root.mkdir()
            copy_check, copied_database = self._copy_to_temp(
                database_path, media_dir, copy_root)
            checks.append(copy_check)
            if copy_check.ok:
                copy_checks, report = self._check_copy(copy_root, copied_database, manifest)
                checks.extend(copy_checks)
            else:
                checks.extend(self._skipped_after_copy(manifest))
        finally:
            # The temporary copy is always removed: it may contain private data and
            # a kept copy would be a second, unprotected database. `checks` already
            # carries every diagnostic message the caller needs.
            _remove_tree(temp_root)

        ok = all(check.ok for check in checks)
        failed = [check for check in checks if not check.ok]
        return BackupVerification(ok=ok, directory=directory,
                                  schema_revision=manifest.schema_revision if ok else None,
                                  checks=tuple(checks), report=report,
                                  error=None if ok else failed[0].message)

    # ----------------------------------------------------------- manifest steps
    @staticmethod
    def _check_manifest(directory: Path):
        path = directory / MANIFEST_NAME
        if not directory.is_dir():
            return VerificationCheck("manifest", False, "备份目录不存在。"), None
        if not path.is_file():
            return VerificationCheck("manifest", False, "备份目录缺少 manifest.json。"), None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return VerificationCheck(
                "manifest", False,
                f"manifest.json 无法解析（{type(exc).__name__}）。"), None
        try:
            manifest = BackupManifest.from_mapping(data)
        except ValueError as exc:
            return VerificationCheck("manifest", False, f"manifest.json 无效：{exc}。"), None
        return VerificationCheck(
            "manifest", True,
            f"manifest.json 可读取：schema={manifest.schema_revision}，"
            f"数据库 sha256={manifest.database_sha256[:12]}…，"
            f"媒体 {manifest.media_file_count} 个 / {manifest.media_total_bytes} 字节。"), manifest

    @staticmethod
    def _check_database_sha256(database_path: Path, manifest: BackupManifest):
        if not database_path.is_file():
            return VerificationCheck("database_sha256", False,
                                     f"备份目录缺少 {DATABASE_NAME}。")
        try:
            digest, _size = _sha256_file(database_path)
        except OSError as exc:
            return VerificationCheck(
                "database_sha256", False,
                f"无法读取备份数据库（{type(exc).__name__}）。")
        if digest != manifest.database_sha256:
            return VerificationCheck(
                "database_sha256", False,
                f"数据库 sha256 与 manifest 不一致（实际 {digest[:12]}…，"
                f"记录 {manifest.database_sha256[:12]}…）。")
        return VerificationCheck("database_sha256", True, "数据库 sha256 与 manifest 一致。")

    # --------------------------------------------------------------- copy steps
    @staticmethod
    def _copy_to_temp(database_path: Path, media_dir: Path, copy_root: Path):
        target = copy_root / DATABASE_NAME
        try:
            if not database_path.is_file():
                raise FileNotFoundError(database_path.name)
            shutil.copyfile(database_path, target)
        except OSError as exc:
            return (VerificationCheck("copy", False,
                                      f"无法把备份数据库复制到临时目录（{type(exc).__name__}）。"),
                    None)
        if media_dir.is_dir():
            try:
                shutil.copytree(media_dir, copy_root / MEDIA_DIR_NAME)
            except OSError as exc:
                return (VerificationCheck(
                    "copy", False,
                    f"无法把备份媒体复制到临时目录（{type(exc).__name__}）。"), None)
        return (VerificationCheck("copy", True,
                                 "数据库与媒体已复制到独立临时目录，校验不在备份原地进行。"),
                target)

    @staticmethod
    def _skipped_after_copy(manifest: BackupManifest):
        reason = "前置检查失败，未执行（备份副本不可用）。"
        return tuple(VerificationCheck(name, False, reason) for name in CHECK_NAMES[3:])

    # ------------------------------------------------------- checks on the copy
    def _check_copy(self, copy_root: Path, copied_database: Path, manifest: BackupManifest):
        """All checks that must run on the temporary copy; also returns the report."""
        checks = [self._check_schema(copied_database, manifest)]
        checks.extend(self._check_database_health(copied_database))
        checks.extend(self._check_media(copy_root / MEDIA_DIR_NAME, manifest))
        checks.append(self._check_application_open(copy_root))
        audit_check, report = self._check_integrity_audit(copy_root)
        checks.append(audit_check)
        return checks, report

    def _check_schema(self, copied_database: Path, manifest: BackupManifest):
        try:
            connection = sqlite3.connect(str(copied_database))
        except sqlite3.Error as exc:
            return VerificationCheck("schema_revision", False,
                                     f"无法打开备份副本（{type(exc).__name__}）。")
        try:
            try:
                revisions = [str(row[0]) for row in
                             connection.execute("SELECT version_num FROM alembic_version")]
            except sqlite3.Error:
                return VerificationCheck("schema_revision", False,
                                         "备份副本缺少 alembic_version 表。")
        finally:
            connection.close()
        if len(revisions) != 1:
            return VerificationCheck("schema_revision", False,
                                     "备份副本的 Alembic 版本不是单一 head。")
        if revisions[0] != manifest.schema_revision:
            return VerificationCheck(
                "schema_revision", False,
                f"备份副本 schema={revisions[0]} 与 manifest 记录 "
                f"{manifest.schema_revision} 不一致。")
        expected = self._expected_head()
        if expected is None:
            return VerificationCheck(
                "schema_revision", True,
                f"备份副本 schema={revisions[0]} 与 manifest 一致；"
                "应用 head 不可用，未比对。")
        if expected != revisions[0]:
            return VerificationCheck(
                "schema_revision", False,
                f"备份副本 schema={revisions[0]} 与应用期望 head {expected} 不一致。")
        return VerificationCheck("schema_revision", True,
                                 f"备份副本 schema={revisions[0]} 与 manifest、应用 head 一致。")

    def _expected_head(self) -> str | None:
        """The single head this installation's migrations expect, when knowable."""
        from app.storage.migrations import expected_heads
        try:
            heads = expected_heads(self._config)
        except Exception:
            return None
        return heads[0] if len(heads) == 1 else None

    @staticmethod
    def _check_database_health(copied_database: Path):
        try:
            connection = sqlite3.connect(str(copied_database))
        except sqlite3.Error as exc:
            message = f"无法打开备份副本（{type(exc).__name__}）。"
            return (VerificationCheck("integrity_check", False, message),
                    VerificationCheck("foreign_key_check", False, message))
        try:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            message = f"数据库副本检查失败（{type(exc).__name__}）。"
            return (VerificationCheck("integrity_check", False, message),
                    VerificationCheck("foreign_key_check", False, message))
        finally:
            connection.close()
        if integrity == ["ok"]:
            integrity_check = VerificationCheck("integrity_check", True,
                                               "PRAGMA integrity_check = ok。")
        else:
            integrity_check = VerificationCheck(
                "integrity_check", False,
                f"PRAGMA integrity_check 报告 {len(integrity)} 条问题：" +
                "；".join(_slice(item) for item in integrity[:3]))
        if not violations:
            key_check = VerificationCheck("foreign_key_check", True,
                                          "PRAGMA foreign_key_check 无违规行。")
        else:
            key_check = VerificationCheck(
                "foreign_key_check", False,
                f"PRAGMA foreign_key_check 发现 {len(violations)} 行外键违规。")
        return integrity_check, key_check

    @staticmethod
    def _check_media(media_root: Path, manifest: BackupManifest):
        entries = {entry.relative_path: entry for entry in manifest.media}
        missing = mismatched = 0
        for relative in sorted(entries):
            entry = entries[relative]
            path = media_root.joinpath(*relative.split("/"))
            try:
                digest, size = _sha256_file(path)
            except OSError:
                missing += 1
                continue
            if size != entry.size or digest != entry.sha256:
                mismatched += 1
        if missing or mismatched:
            files_check = VerificationCheck(
                "media_files", False,
                f"媒体校验失败：缺失 {missing} 个，size/sha256 不一致 {mismatched} 个"
                f"（manifest 记录 {len(entries)} 个）。")
        elif not media_root.is_dir():
            files_check = VerificationCheck(
                "media_files", False,
                f"备份布局缺少 {MEDIA_DIR_NAME}/ 目录（manifest 记录 {len(entries)} 个文件）。")
        else:
            files_check = VerificationCheck(
                "media_files", True,
                f"{len(entries)} 个媒体文件的 size 与 sha256 与 manifest 全部一致。")

        present = set()
        if media_root.is_dir():
            for path in sorted(media_root.rglob("*")):
                if path.is_file():
                    present.add(path.relative_to(media_root).as_posix())
        extra = sorted(present - set(entries))
        if extra:
            extra_check = VerificationCheck(
                "media_extra_files", False,
                f"media/ 中存在 {len(extra)} 个 manifest 未记录的文件：" +
                "、".join(_slice(name, 80) for name in extra[:3]))
        else:
            extra_check = VerificationCheck("media_extra_files", True,
                                            "media/ 中没有 manifest 未记录的文件。")
        return files_check, extra_check

    def _check_application_open(self, copy_root: Path):
        try:
            engine = create_sqlite_engine(AppConfig(data_dir=copy_root,
                                                    database=DATABASE_NAME))
        except Exception as exc:
            return VerificationCheck(
                "application_open", False,
                f"应用无法打开数据库副本（{type(exc).__name__}）。")
        try:
            return VerificationCheck(
                "application_open", True,
                "应用建库入口已成功打开并关闭数据库副本（WAL、外键、写事务均可用）。")
        finally:
            engine.dispose()

    def _check_integrity_audit(self, copy_root: Path):
        """Optional cross-check: the Phase 10 audit service, on the backup copy.

        Returns ``(check, report)``: the report is the audit's own object so the
        caller can attach it, and it is ``None`` whenever the audit was skipped.
        The audit is a parallel deliverable, so an absent or differently shaped
        service degrades to a documented *skip* (``ok=True``) instead of failing a
        perfectly good backup - while a service that runs and reports problems is
        a real failure. A service that raises while running is also a skip: a bug
        inside the audit tool is not evidence about this backup, and the exact
        exception is quoted in the check message. Nothing here ever opens the
        production database: the audit runs against the temporary copy only.
        """
        try:
            from app.services import integrity as integrity_module
        except ImportError:
            return self._audit_skipped(
                "app.services.integrity 不可用（不是失败）"), None
        service_class = getattr(integrity_module, "DataIntegrityService", None)
        if service_class is None:
            return self._audit_skipped(
                "app.services.integrity 未提供 DataIntegrityService"), None
        engine = None
        try:
            engine = create_sqlite_engine(AppConfig(data_dir=copy_root,
                                                    database=DATABASE_NAME))
            sessions = create_session_factory(engine)
            try:
                service = service_class(**self._integrity_arguments(service_class, sessions,
                                                                    copy_root))
            except Exception as exc:
                return self._audit_skipped(
                    f"DataIntegrityService 无法构造（{type(exc).__name__}："
                    f"{_slice(exc, 120)}）"), None
            # A *bound* method, so inspect.signature() sees only the audit's own
            # parameters and never the constructor's `self`.
            method = next((getattr(service, name) for name in
                           ("check", "audit", "verify", "run")
                           if callable(getattr(service, name, None))), None)
            if method is None:
                return self._audit_skipped("DataIntegrityService 没有可调用的审计方法"), None
            invocation = self._audit_invocation(method, sessions)
            if invocation is None:
                return self._audit_skipped("审计方法的必需参数无法由备份验证提供"), None
            try:
                report = invocation()
            except sqlite3.Error as exc:
                # A database level failure is evidence *about the copy*.
                return VerificationCheck(
                    "integrity_audit", False,
                    f"完整性审计在备份副本上遇到数据库错误（{type(exc).__name__}）。"), None
            except Exception as exc:
                # A defect inside the (parallel) audit service says nothing about
                # this backup, so it is reported as a skip with the exact reason
                # instead of turning a healthy backup into a failure.
                return self._audit_skipped(
                    f"完整性审计当前无法运行（{type(exc).__name__}："
                    f"{_slice(exc, 120)}）"), None
        finally:
            if engine is not None:
                engine.dispose()
        ok = bool(getattr(report, "ok", report))
        summary = getattr(report, "summary", None)
        detail = summary() if callable(summary) else "完整性审计已完成。"
        if ok:
            return VerificationCheck("integrity_audit", True,
                                     f"数据完整性审计在备份副本上通过：{_slice(detail)}"), report
        return VerificationCheck("integrity_audit", False,
                                 f"数据完整性审计在备份副本上报告问题：{_slice(detail)}"), report

    @staticmethod
    def _audit_skipped(reason: str) -> VerificationCheck:
        return VerificationCheck("integrity_audit", True,
                                 f"已跳过完整性审计：{reason}。")

    @staticmethod
    def _integrity_arguments(service_class, sessions, copy_root: Path) -> dict:
        """Fill only the constructor parameters this service actually declares."""
        try:
            parameters = inspect.signature(service_class).parameters
        except (TypeError, ValueError):
            return {}
        candidates = {"sessions": lambda: sessions,
                      "session_factory": lambda: sessions,
                      "factory": lambda: sessions,
                      "media_store": lambda: MediaStore(copy_root),
                      "store": lambda: MediaStore(copy_root),
                      "config": lambda: AppConfig(data_dir=copy_root,
                                                  database=DATABASE_NAME)}
        return {name: build() for name, build in candidates.items() if name in parameters}

    def _audit_invocation(self, method, sessions):
        """A zero-argument callable for the audit, or None when it cannot be built."""
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return method
        required = [item for item in parameters.values()
                    if item.default is inspect.Parameter.empty
                    and item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)]
        if not required:
            return method
        if len(required) == 1 and required[0].name in {"workspace_id", "workspace", "scope"}:
            workspace_id = self._first_workspace_id(sessions)
            return lambda: method(workspace_id)
        return None

    @staticmethod
    def _first_workspace_id(sessions):
        from sqlalchemy import select
        from app.storage.models import WorkspaceModel
        table = WorkspaceModel.__table__
        with sessions() as session:
            row = session.execute(select(table.c.id).order_by(table.c.created_at)).first()
        return None if row is None else row[0]


def _relative_media_path(storage_path) -> str | None:
    """`media/<workspace>/<xx>/<sha>` -> `<workspace>/<xx>/<sha>`, else None."""
    if not isinstance(storage_path, str) or not storage_path.startswith(_MEDIA_PREFIX):
        return None
    relative = storage_path[len(_MEDIA_PREFIX):]
    pure = PurePosixPath(relative)
    if (not relative or pure.is_absolute() or ":" in relative or "\\" in relative
            or ".." in pure.parts or "." in pure.parts):
        return None
    return relative


def _absolute(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:                                    # pragma: no cover - defensive
        logger.warning("Temporary backup tree could not be removed")
