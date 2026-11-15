"""Pure domain tests: no database, service, Qt fixture or filesystem setup."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path, PurePosixPath
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from app.domain.entities import Media, Person, PersonAlias, PersonMedia, Workspace
from app.domain.errors import (
    ConflictError, DomainError, MediaValidationError, NotFoundError, WorkspaceMismatchError,
)
from app.domain.validation import utc_now


ENTITY_TYPES = (Workspace, Person, PersonAlias, Media, PersonMedia)


def make_entity(entity_type, **overrides):
    values = {
        Workspace: {"name": "Example workspace"},
        Person: {"workspace_id": uuid4(), "display_name": "Example person"},
        PersonAlias: {"workspace_id": uuid4(), "person_id": uuid4(), "alias": "An alias"},
        Media: {"workspace_id": uuid4(), "sha256": "a" * 64, "mime_type": "image/png", "size_bytes": 0, "storage_path": "images/photo.png"},
        PersonMedia: {"workspace_id": uuid4(), "person_id": uuid4(), "media_id": uuid4()},
    }[entity_type]
    values.update(overrides)
    return entity_type(**values)


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_defaults_and_keyword_only_frozen_slots(entity_type):
    before = utc_now()
    entity = make_entity(entity_type)
    after = utc_now()
    assert isinstance(entity.id, UUID)
    assert not hasattr(entity, "__dict__")
    assert before <= entity.created_at <= after
    assert entity.created_at.tzinfo is timezone.utc
    if hasattr(entity, "updated_at"):
        assert before <= entity.updated_at <= after
        assert entity.updated_at.tzinfo is timezone.utc
    if hasattr(entity, "deleted_at"):
        assert entity.deleted_at is None
    with pytest.raises(FrozenInstanceError):
        entity.id = uuid4()
    with pytest.raises(TypeError):
        entity_type(uuid4())
    another = make_entity(entity_type)
    assert entity.id != another.id


def test_optional_defaults_match_model_api():
    person = make_entity(Person)
    assert all(getattr(person, name) is None for name in (
        "real_name", "gender", "birthday", "occupation", "organization", "location", "notes"
    ))
    alias = make_entity(PersonAlias)
    assert alias.alias_type == "OTHER" and alias.note is None
    media = make_entity(Media)
    assert media.width is None and media.height is None
    link = make_entity(PersonMedia)
    assert link.role == "GALLERY" and link.sort_order == 0
    # Keep the entity congruent with the association model, which has no updated_at.
    assert not hasattr(link, "updated_at")


UUID_FIELDS = [(cls, item.name) for cls in ENTITY_TYPES for item in fields(cls)
               if item.name == "id" or item.name.endswith("_id")]
TIME_FIELDS = [(cls, item.name) for cls in ENTITY_TYPES for item in fields(cls)
               if item.name.endswith("_at")]


@pytest.mark.parametrize("entity_type,field_name", UUID_FIELDS)
@pytest.mark.parametrize("bad", [None, 123, "not-a-uuid", str(uuid4())])
def test_all_identifier_fields_require_uuid_objects(entity_type, field_name, bad):
    with pytest.raises(DomainError, match=field_name):
        make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("entity_type,field_name", TIME_FIELDS)
def test_all_timestamps_normalize_to_utc(entity_type, field_name):
    local = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    entity = make_entity(entity_type, **{field_name: local})
    normalized = getattr(entity, field_name)
    assert normalized == local
    assert normalized.tzinfo is timezone.utc
    assert normalized.hour == 19 and normalized.day == 1


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("entity_type,field_name", TIME_FIELDS)
@pytest.mark.parametrize("bad", [datetime(2025, 1, 2), datetime(2025, 1, 2, tzinfo=NoOffset()), "2025-01-01", date(2025, 1, 1)])
def test_all_timestamps_reject_naive_or_non_datetime(entity_type, field_name, bad):
    with pytest.raises(DomainError, match=field_name):
        make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_required_created_timestamp_rejects_none(entity_type):
    with pytest.raises(DomainError):
        make_entity(entity_type, created_at=None)


@pytest.mark.parametrize("entity_type,field_name,limit", [
    (Workspace, "name", 200), (Person, "display_name", 200),
    (PersonAlias, "alias", 300), (PersonAlias, "alias_type", 100),
    (Media, "mime_type", 200),
])
def test_required_text_and_maximum_lengths(entity_type, field_name, limit):
    assert getattr(make_entity(entity_type, **{field_name: "x" * limit}), field_name) == "x" * limit
    for bad in ("", " \t\n", None, 1, "x" * (limit + 1)):
        with pytest.raises(DomainError):
            make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("field_name,limit", [
    ("real_name", 200), ("gender", 100), ("occupation", 300),
    ("organization", 300), ("location", 300),
])
def test_optional_person_strings_validate_type_and_orm_limit(field_name, limit):
    assert getattr(make_entity(Person, **{field_name: "x" * limit}), field_name) == "x" * limit
    assert getattr(make_entity(Person, **{field_name: ""}), field_name) == ""
    for bad in (False, 42, "x" * (limit + 1)):
        with pytest.raises(DomainError):
            make_entity(Person, **{field_name: bad})


@pytest.mark.parametrize("entity_type,field_name", [(Workspace, "description"), (Person, "notes"), (PersonAlias, "note")])
def test_optional_free_text(entity_type, field_name):
    assert getattr(make_entity(entity_type, **{field_name: ""}), field_name) == ""
    with pytest.raises(DomainError):
        make_entity(entity_type, **{field_name: []})


def test_birthday_is_date_not_datetime():
    birthday = date(2000, 1, 2)
    assert make_entity(Person, birthday=birthday).birthday == birthday
    for bad in (datetime(2000, 1, 2), utc_now(), "2000-01-02", 42):
        with pytest.raises(DomainError, match="birthday"):
            make_entity(Person, birthday=bad)


@pytest.mark.parametrize("path", [
    "", " ", ".", "./", "..", "../photo.png", "images/../photo.png",
    "/photo.png", "//host/photo.png", r"images\photo.png", r"C:\photo.png",
    "C:photo.png", "images/a:b.png", "images/\x00.png", None, 123,
])
def test_media_rejects_unsafe_paths(path):
    with pytest.raises(MediaValidationError):
        make_entity(Media, storage_path=path)


@pytest.mark.parametrize("path,expected", [
    ("photo.png", "photo.png"), ("images/photo.png", "images/photo.png"),
    ("./images//photo.png", "images/photo.png"),
    (PurePosixPath("images/photo.png"), "images/photo.png"),
    ("images/a..b.png", "images/a..b.png"),
])
def test_media_normalizes_relative_posix_path_to_string(path, expected):
    media = make_entity(Media, storage_path=path)
    assert media.storage_path == expected
    assert isinstance(media.storage_path, str)


@pytest.mark.parametrize("sha256", ["", "a" * 63, "a" * 65, "g" * 64, "a" * 63 + "\n", None, 42])
def test_sha256_requires_64_hex_characters(sha256):
    with pytest.raises(MediaValidationError):
        make_entity(Media, sha256=sha256)


@pytest.mark.parametrize("sha256", ["aB09" * 16, "AB09" * 16, "ab09" * 16])
def test_sha256_accepts_hex_and_normalizes_to_lowercase(sha256):
    media = make_entity(Media, sha256=sha256)
    assert media.sha256 == sha256.lower()
    assert replace(media, sha256=sha256.upper()).sha256 == sha256.lower()


def test_sha256_validator_returns_lowercase_digest():
    from app.domain.validation import validate_sha256

    assert validate_sha256("AB09" * 16) == "ab09" * 16


@pytest.mark.parametrize("value", [-1, True, 1.0, "0", None])
def test_size_requires_nonnegative_integer(value):
    with pytest.raises(MediaValidationError):
        make_entity(Media, size_bytes=value)


@pytest.mark.parametrize("width,height", [(None, 1), (1, None), (0, 1), (1, 0), (-1, 1), (1, -1), (True, 1), (1, False), (1.0, 1), (1, "1")])
def test_dimensions_require_paired_positive_integers(width, height):
    with pytest.raises(MediaValidationError):
        make_entity(Media, width=width, height=height)


def test_valid_dimensions():
    media = make_entity(Media, width=640, height=480)
    assert (media.width, media.height) == (640, 480)


@pytest.mark.parametrize("role", ["avatar", "gallery", "OTHER", "", None, []])
def test_person_media_role_is_strict(role):
    with pytest.raises(DomainError):
        make_entity(PersonMedia, role=role)


@pytest.mark.parametrize("role", ["AVATAR", "GALLERY"])
def test_valid_roles(role):
    assert make_entity(PersonMedia, role=role).role == role


@pytest.mark.parametrize("value", [-1, True, 1.0, "0", None])
def test_sort_order_requires_nonnegative_integer(value):
    with pytest.raises(DomainError):
        make_entity(PersonMedia, sort_order=value)


def test_replace_revalidates_without_mutating_original():
    original = Workspace(name="Original")
    changed = replace(original, name="Changed")
    assert original.name == "Original" and changed.name == "Changed"
    assert original.id == changed.id
    with pytest.raises(DomainError):
        replace(original, name=" ")
    local = datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=-5)))
    changed = replace(original, updated_at=local)
    assert changed.updated_at.tzinfo is timezone.utc


@pytest.mark.parametrize("error_type", [NotFoundError, WorkspaceMismatchError, ConflictError, MediaValidationError])
def test_public_errors_inherit_domain_value_error(error_type):
    assert issubclass(error_type, DomainError)
    assert issubclass(error_type, ValueError)


def test_domain_import_does_not_load_framework_or_storage_dependencies():
    # A fresh interpreter proves this even though the project's root conftest
    # imports Qt for unrelated UI tests.
    script = '''
import importlib.abc
import sys
class RejectInfrastructure(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'PySide6', 'sqlite3', 'alembic'} or fullname.startswith(('app.storage', 'app.services')):
            raise AssertionError('Forbidden domain dependency: ' + fullname)
sys.meta_path.insert(0, RejectInfrastructure())
from app.domain.entities import Workspace, Person, PersonAlias, Media, PersonMedia
from app.domain.errors import DomainError
from app.domain.validation import utc_now
assert not any(isinstance(value, (Workspace, Person, PersonAlias, Media, PersonMedia)) for module_name, module in tuple(sys.modules.items()) if module_name.startswith('app.domain') for value in vars(module).values())
'''
    result = subprocess.run([sys.executable, "-s", "-c", script], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
