"""Immutable, content-addressed media files; no database or UI dependencies.

The caller holds ``content_lock`` throughout publish, its database transaction,
and any compensation. Physical deletion is only failed-import compensation,
never soft deletion. The data directory must be application-owned: checks
reject symlinks/junctions, but are not a sandbox against hostile directory
renames by another OS user between path checks and filesystem operations.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from app.domain.errors import MediaValidationError
from app.domain.validation import validate_sha256, validate_uuid
from app.storage.file_lock import file_lock

logger = logging.getLogger(__name__)

MAX_SIZE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
CHUNK_SIZE = 1024 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
_ERROR = "媒体文件无效、不可读取或超出安全限制。"


@dataclass(frozen=True)
class StagedMedia:
    path: Path
    sha256: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None


@dataclass(frozen=True)
class PublishedMedia:
    storage_path: str
    created_new: bool
    sha256: str
    size_bytes: int


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    # O_NOFOLLOW adds a leaf-level guard on platforms that support it.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise MediaValidationError(_ERROR)
        while chunk := stream.read(CHUNK_SIZE):
            size += len(chunk)
            if size > MAX_SIZE_BYTES:
                raise MediaValidationError(_ERROR)
            digest.update(chunk)
    return digest.hexdigest(), size


def _image_like(prefix: bytes) -> bool:
    return (prefix.startswith((b"\x89PNG", b"\xff\xd8", b"GIF87a", b"GIF89a", b"BM",
                               b"II*\0", b"MM\0*", b"\0\0\1\0", b"8BPS"))
            or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"))


class MediaStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.media_dir = self.data_dir / "media"
        self.staging_dir = self.media_dir / ".staging"
        self.locks_dir = self.media_dir / ".locks"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for directory in (self.media_dir, self.staging_dir, self.locks_dir):
                self._guard(directory)
                directory.mkdir(exist_ok=True)
                self._guard(directory)
        except (OSError, ValueError):
            raise MediaValidationError("媒体存储目录不可用。") from None

    def _guard(self, path: Path) -> Path:
        """Reject every link component, including links into this same tree."""
        try:
            parts = path.relative_to(self.data_dir).parts
            current = self.data_dir
            if _is_link(current):
                raise MediaValidationError(_ERROR)
            for part in parts:
                current = current / part
                if _is_link(current):
                    raise MediaValidationError(_ERROR)
            if path.resolve() != path:
                raise MediaValidationError(_ERROR)
            return path
        except (OSError, ValueError, RuntimeError):
            raise MediaValidationError(_ERROR) from None

    def _staged_path(self, staged: StagedMedia) -> Path:
        if not isinstance(staged, StagedMedia) or not isinstance(staged.path, Path):
            raise MediaValidationError(_ERROR)
        path = staged.path
        try:
            if path.parent != self.staging_dir or str(UUID(path.name)) != path.name:
                raise MediaValidationError(_ERROR)
        except (ValueError, AttributeError):
            raise MediaValidationError(_ERROR) from None
        return self._guard(path)

    def _storage_path(self, workspace_id: UUID, sha256: str) -> str:
        validate_uuid(workspace_id, "workspace_id")
        sha256 = validate_sha256(sha256)
        return f"media/{workspace_id}/{sha256[:2]}/{sha256}"

    def _inspect(self, path: Path, extension: str) -> tuple[str, int | None, int | None]:
        Image.init()
        extensions = set(Image.registered_extensions()) | {".svg", ".heic", ".heif", ".avif"}
        with path.open("rb") as stream:
            prefix = stream.read(16)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    if width * height > MAX_IMAGE_PIXELS:
                        raise MediaValidationError(_ERROR)
                    mime = Image.MIME.get(image.format or "")
                    if not mime or not mime.startswith("image/"):
                        raise MediaValidationError(_ERROR)
                    image.verify()
            except UnidentifiedImageError:
                if extension in extensions or _image_like(prefix):
                    raise MediaValidationError(_ERROR) from None
                return "application/octet-stream", None, None
            # verify() does not decode the pixel data; reopen and load every
            # frame, bounding cumulative decoded pixels for animated images.
            with Image.open(path) as image:
                total_pixels = 0
                frame = 0
                while True:
                    total_pixels += image.width * image.height
                    if total_pixels > MAX_IMAGE_PIXELS:
                        raise MediaValidationError(_ERROR)
                    image.load()
                    frame += 1
                    try:
                        image.seek(frame)
                    except EOFError:
                        break
            return mime, width, height

    def stage(self, source: Path) -> StagedMedia:
        """Copy without altering the original; validate only our owned snapshot."""
        path = self.staging_dir / str(uuid4())
        created = False
        try:
            self._guard(path)
            digest = hashlib.sha256()
            size = 0
            with Path(source).open("rb") as original:
                if not stat.S_ISREG(os.fstat(original.fileno()).st_mode):
                    raise MediaValidationError(_ERROR)
                with path.open("xb") as target:
                    created = True
                    while chunk := original.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > MAX_SIZE_BYTES:
                            raise MediaValidationError(_ERROR)
                        target.write(chunk)
                        digest.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            if not size:
                raise MediaValidationError(_ERROR)
            mime, width, height = self._inspect(path, Path(source).suffix.lower())
            return StagedMedia(path, digest.hexdigest(), mime, size, width, height)
        except BaseException as exc:
            if created:
                try:
                    self._guard(path).unlink(missing_ok=True)
                except (OSError, MediaValidationError) as cleanup_error:
                    logger.error("Media staging rollback cleanup failed: type=%s", type(cleanup_error).__name__)
            if isinstance(exc, Exception):
                raise MediaValidationError(_ERROR) from None
            raise

    def discard(self, staged: StagedMedia) -> None:
        try:
            self._staged_path(staged).unlink(missing_ok=True)
        except OSError:
            raise MediaValidationError("临时媒体清理失败。") from None

    @contextmanager
    def content_lock(self, workspace_id: UUID, sha256: str):
        storage_path = self._storage_path(workspace_id, sha256)
        key = storage_path.split("/")
        directory = self.locks_dir / key[1]
        try:
            self._guard(directory)
            directory.mkdir(exist_ok=True)
            path = self._guard(directory / (key[3] + ".lock"))
            with file_lock(path, timeout=LOCK_TIMEOUT_SECONDS):
                yield
        except OSError:
            raise MediaValidationError("媒体内容锁不可用。") from None

    def path_for(self, storage_path: str) -> Path:
        """Accept only the exact canonical content path, never staging/locks."""
        if not isinstance(storage_path, str):
            raise MediaValidationError("媒体存储路径无效。")
        match = re.fullmatch(
            r"media/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/([0-9a-f]{2})/([0-9a-f]{64})",
            storage_path,
        )
        if match is None or match[2] != match[3][:2]:
            raise MediaValidationError("媒体存储路径无效。")
        return self._guard(self.data_dir.joinpath(*storage_path.split("/")))

    def publish(self, workspace_id: UUID, staged: StagedMedia) -> PublishedMedia:
        """Atomically create a same-volume hardlink; never replace old content.

        Staging remains until service finally calls discard. An existing file
        must match both digest and size, even when it has the expected name.
        """
        try:
            source = self._staged_path(staged)
            sha256 = validate_sha256(staged.sha256)
            if sha256 != staged.sha256 or _hash_file(source) != (sha256, staged.size_bytes):
                raise MediaValidationError(_ERROR)
            storage_path = self._storage_path(workspace_id, sha256)
            final = self.path_for(storage_path)
            final.parent.mkdir(parents=True, exist_ok=True)
            final = self.path_for(storage_path)
            try:
                os.link(source, final, follow_symlinks=False)
                created_new = True
            except FileExistsError:
                if _hash_file(self.path_for(storage_path)) != (sha256, staged.size_bytes):
                    raise MediaValidationError("已有媒体文件校验失败，未覆盖原文件。") from None
                created_new = False
            return PublishedMedia(storage_path, created_new, sha256, staged.size_bytes)
        except OSError:
            raise MediaValidationError("媒体发布失败，未覆盖已有文件。") from None

    def verify(self, storage_path: str, sha256: str | None = None) -> bool:
        try:
            path = self.path_for(storage_path)
            expected = path.name
            if sha256 is not None and validate_sha256(sha256) != expected:
                return False
            digest, size = _hash_file(path)
            return size > 0 and digest == expected
        except (OSError, ValueError):
            return False

    def delete_if_unreferenced(self, receipt: PublishedMedia, *,
                               media_exists: bool, references_exist: bool) -> bool:
        """Conservative failed-import compensation; caller holds content_lock.

        Only literal False is proof; None, 0 and other unknown values retain
        the content. Database state is exclusively the caller's responsibility.
        """
        if (not isinstance(receipt, PublishedMedia) or receipt.created_new is not True
                or media_exists is not False or references_exist is not False):
            return False
        try:
            path = self.path_for(receipt.storage_path)
            if path.name != receipt.sha256:
                return False
            if _hash_file(path) != (receipt.sha256, receipt.size_bytes):
                return False
            self.path_for(receipt.storage_path).unlink()
            return True
        except (OSError, ValueError):
            return False
