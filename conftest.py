"""Repository-level pytest configuration.

The confined Windows workspace cannot write to the user temp directory, so
``tempfile.gettempdir()`` silently falls back to the current working directory and
pytest would create ``pytest-of-<user>`` inside the repository — hundreds of
megabytes of throwaway databases per run.

This hook keeps every test temporary in one predictable, git-ignored directory
(``.test-runs/``) whenever the operating-system temp directory is not really
usable. On a normal machine nothing changes: the OS temp directory is proven
writable with a real file and is used as before.

The same proven-writable directory is also handed to SQLite through
``SQLITE_TMPDIR``/``TMPDIR``/``TEMP``/``TMP``: SQLite needs a temporary file to
drop the FTS5 virtual table added by migration ``0006_search_projection``, and a
denied temp directory turns that real DDL into ``SQLITE_CANTOPEN``.
"""
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_TEMP_ROOT = ROOT / ".test-runs"


def os_temp_is_usable() -> bool:
    """Return True only when a temp candidate accepts a real file.

    ``os.access`` is unreliable on Windows, so the check writes and removes one
    probe file instead of trusting the permission bits.
    """
    for name in ("TMPDIR", "TEMP", "TMP"):
        candidate = os.environ.get(name)
        if not candidate or not os.path.isdir(candidate):
            continue
        probe = os.path.join(candidate, f".temp-write-probe-{os.getpid()}")
        try:
            with open(probe, "w", encoding="utf-8"):
                pass
        except OSError:
            continue
        try:
            os.remove(probe)
        except OSError:
            pass
        return True
    return False


def redirect_temp_root() -> Path | None:
    current = Path(tempfile.gettempdir()).resolve()
    if os_temp_is_usable() and ROOT not in current.parents and current != ROOT:
        return None
    TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(TEST_TEMP_ROOT)
    return TEST_TEMP_ROOT


def enable_sqlite_temporary_files() -> Path:
    """Point SQLite's temporary files at a directory that really accepts writes.

    SQLite resolves its temporary directory from ``SQLITE_TMPDIR``/``TMPDIR``/
    ``TEMP``/``TMP`` and caches the result on first use, and it answers
    ``SQLITE_CANTOPEN`` ("unable to open database file") when that directory is
    unusable. A confined Windows host can deny the OS temp directory, and SQLite
    then refuses DDL that needs a spill file - notably
    ``DROP TABLE search_document_fts`` in migration ``0006_search_projection``.
    The tests must observe the real migration behaviour, so the environment is
    pointed at the proven-writable directory before the first SQLite call.
    """
    directory = Path(tempfile.gettempdir())
    for name in ("SQLITE_TMPDIR", "TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(directory)
    return directory


redirect_temp_root()
enable_sqlite_temporary_files()

