"""Shared validation primitives using only the Python standard library.

Validators return validated values; UTC and path normalization can therefore
be applied explicitly by each frozen entity's ``__post_init__``.
"""

from datetime import date, datetime, timezone
from pathlib import PurePosixPath
import re
from uuid import UUID

from app.domain.errors import DomainError, MediaValidationError


def utc_now() -> datetime:
    """Return an aware timestamp in UTC (also suitable as a default factory)."""
    return datetime.now(timezone.utc)


def validate_uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise DomainError(f"{field_name} must be a UUID")
    return value


def validate_text(
    value: str | None,
    field_name: str,
    *,
    max_length: int | None = None,
    optional: bool = False,
    nonempty: bool = True,
    error_type: type[DomainError] = DomainError,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a string")
    if nonempty and not value.strip():
        raise error_type(f"{field_name} must not be blank")
    if max_length is not None and len(value) > max_length:
        raise error_type(f"{field_name} must contain at most {max_length} characters")
    return value


def normalize_datetime(
    value: datetime | None, field_name: str, *, optional: bool = False
) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def validate_birthday(value: date | None) -> date | None:
    # datetime subclasses date, but cannot be used for a date-only birthday.
    if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
        raise DomainError("birthday must be a date, not a datetime")
    return value


def validate_nonnegative_int(
    value: int, field_name: str, *, error_type: type[DomainError] = DomainError
) -> int:
    if type(value) is not int or value < 0:
        raise error_type(f"{field_name} must be a nonnegative integer")
    return value


def validate_dimensions(width: int | None, height: int | None) -> None:
    if width is None and height is None:
        return
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise MediaValidationError("width and height must both be positive integers or both None")


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise MediaValidationError("sha256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def validate_storage_path(value: str | PurePosixPath) -> str:
    if not isinstance(value, (str, PurePosixPath)):
        raise MediaValidationError("storage_path must be a POSIX path string or PurePosixPath")
    raw = str(value)
    path = PurePosixPath(raw)
    if (
        not raw.strip()
        or "\\" in raw
        or ":" in raw
        or "\x00" in raw
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
    ):
        raise MediaValidationError("storage_path must be a nonempty relative POSIX path without '..', backslashes or colons")
    return path.as_posix()
