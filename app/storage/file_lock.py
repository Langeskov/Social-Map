"""Persistent advisory lock files, combining process and thread exclusion."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time

from app.domain.errors import MediaValidationError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock(path: Path) -> threading.Lock:
    with _REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(os.path.normcase(str(path)), threading.Lock())


@contextmanager
def file_lock(path: Path, *, timeout: float = 10.0):
    """Never unlink lock files: all contenders must continue using one inode."""
    deadline = time.monotonic() + timeout
    local = _thread_lock(path)
    if not local.acquire(timeout=max(0.0, timeout)):
        raise MediaValidationError("媒体内容正在使用，请稍后重试。")
    stream = None
    acquired = False
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        # Locking a byte beyond EOF is supported by Windows/POSIX. Do not
        # initialize it outside the lock: another process may already own
        # that byte, making even the initialization write fail on Windows.
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaValidationError("媒体内容正在使用，请稍后重试。") from None
                time.sleep(min(0.025, remaining))
        if os.fstat(stream.fileno()).st_size == 0:
            stream.seek(0)
            stream.write(b"\0")
        yield
    except OSError:
        raise MediaValidationError("媒体内容锁不可用。") from None
    finally:
        try:
            if stream is not None:
                try:
                    if acquired:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            local.release()
