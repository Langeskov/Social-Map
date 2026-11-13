"""Relationship data invariants without services, persistence or UI dependencies."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from app.domain import entities
from app.domain.entities import Relationship, RelationshipHistory
from app.domain.errors import DomainError
from app.domain.validation import utc_now


ENTITY_TYPES = (Relationship, RelationshipHistory)


def make_entity(entity_type, **overrides):
    if entity_type is Relationship:
        values = {
            "workspace_id": uuid4(),
            "person_a_id": UUID(int=1),
            "person_b_id": UUID(int=2),
            "relationship_type": "FRIEND",
        }
    else:
        values = {
            "workspace_id": uuid4(),
            "relationship_id": uuid4(),
            "state": "ACTIVE",
            "effective_at": utc_now(),
        }
    values.update(overrides)
    return entity_type(**values)


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_defaults_keyword_only_frozen_slots_and_public_export(entity_type):
    before = utc_now()
    entity = make_entity(entity_type)
    after = utc_now()
    assert entity_type.__name__ in entities.__all__
    assert entity.id.version == 4
    assert entity.id != make_entity(entity_type).id
    assert before <= entity.created_at <= after
    assert entity.created_at.tzinfo is timezone.utc
    assert not hasattr(entity, "__dict__")
    assert entity_type.__bases__ == (object,)
    with pytest.raises(FrozenInstanceError):
        entity.id = uuid4()
    with pytest.raises(TypeError):
        entity_type(uuid4())


def test_exact_field_api_and_defaults():
    relationship = make_entity(Relationship)
    assert {item.name for item in fields(Relationship)} == {
        "workspace_id", "person_a_id", "person_b_id", "relationship_type",
        "directionality", "id", "created_at", "updated_at", "deleted_at",
    }
    assert relationship.directionality == "UNDIRECTED"
    assert relationship.deleted_at is None
    assert relationship.updated_at.tzinfo is timezone.utc
    assert {item.name for item in fields(RelationshipHistory)} == {
        "workspace_id", "relationship_id", "state", "effective_at", "id", "note", "created_at", "event_id",
    }
    assert make_entity(RelationshipHistory).note is None
    assert make_entity(RelationshipHistory).event_id is None


@pytest.mark.parametrize("entity_type,required_fields", [
    (Relationship, ("workspace_id", "person_a_id", "person_b_id", "relationship_type")),
    (RelationshipHistory, ("workspace_id", "relationship_id", "state", "effective_at")),
])
def test_required_fields_have_no_defaults(entity_type, required_fields):
    from dataclasses import MISSING

    for item in fields(entity_type):
        if item.name in required_fields:
            assert item.default is MISSING and item.default_factory is MISSING
    with pytest.raises(TypeError):
        entity_type()


UUID_FIELDS = [(cls, item.name) for cls in ENTITY_TYPES for item in fields(cls)
               if (item.name == "id" or item.name.endswith("_id")) and item.default is not None]
TIME_FIELDS = [(cls, item.name) for cls in ENTITY_TYPES for item in fields(cls)
               if item.name.endswith("_at")]


@pytest.mark.parametrize("entity_type,field_name", UUID_FIELDS)
@pytest.mark.parametrize("bad", [None, True, 123, "not-a-uuid", str(uuid4())])
def test_identifiers_require_real_uuid_objects(entity_type, field_name, bad):
    with pytest.raises(DomainError, match=field_name):
        make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_supplied_uuid_is_preserved(entity_type):
    identifier = uuid4()
    assert make_entity(entity_type, id=identifier).id is identifier


@pytest.mark.parametrize("entity_type,field_name", TIME_FIELDS)
@pytest.mark.parametrize("offset", [8, -5])
def test_timestamps_normalize_to_utc_including_date_rollover(entity_type, field_name, offset):
    local = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=offset)))
    normalized = getattr(make_entity(entity_type, **{field_name: local}), field_name)
    assert normalized == local
    assert normalized.tzinfo is timezone.utc
    assert normalized == local.astimezone(timezone.utc)
    if offset == 8:
        assert normalized.date() == date(2025, 1, 1)


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("entity_type,field_name", TIME_FIELDS)
@pytest.mark.parametrize("bad", [
    datetime(2025, 1, 2), datetime(2025, 1, 2, tzinfo=NoOffset()),
    "2025-01-02", date(2025, 1, 2), 42,
])
def test_timestamps_reject_naive_date_and_non_datetime(entity_type, field_name, bad):
    with pytest.raises(DomainError, match=field_name):
        make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("entity_type,field_name", TIME_FIELDS)
def test_only_deleted_timestamp_accepts_none(entity_type, field_name):
    if field_name == "deleted_at":
        assert make_entity(entity_type, **{field_name: None}).deleted_at is None
    else:
        with pytest.raises(DomainError, match=field_name):
            make_entity(entity_type, **{field_name: None})


@pytest.mark.parametrize("directionality", ["UNDIRECTED", "DIRECTED"])
def test_same_endpoints_are_rejected(directionality):
    with pytest.raises(DomainError, match="distinct"):
        make_entity(Relationship, person_a_id=UUID(int=2), directionality=directionality)


def test_undirected_requires_canonical_order_without_swapping():
    with pytest.raises(DomainError, match="person_a_id.int < person_b_id.int"):
        make_entity(Relationship, person_a_id=UUID(int=2), person_b_id=UUID(int=1))
    relationship = make_entity(Relationship)
    assert relationship.person_a_id.int < relationship.person_b_id.int


@pytest.mark.parametrize("a,b", [(1, 2), (2, 1)])
def test_directed_preserves_endpoint_direction(a, b):
    relationship = make_entity(Relationship, person_a_id=UUID(int=a), person_b_id=UUID(int=b), directionality="DIRECTED")
    assert (relationship.person_a_id.int, relationship.person_b_id.int) == (a, b)


@pytest.mark.parametrize("bad", [None, [], {}, True, 42, "", "directed", "undirected", " DIRECTED", "DIRECTED ", "OTHER"])
def test_directionality_accepts_only_two_exact_strings(bad):
    with pytest.raises(DomainError, match="directionality"):
        make_entity(Relationship, directionality=bad)


@pytest.mark.parametrize("entity_type,field_name,limit", [
    (Relationship, "relationship_type", 200), (RelationshipHistory, "state", 50),
])
def test_required_text_limits_and_already_trimmed_contract(entity_type, field_name, limit):
    for valid in ("x", "x" * limit, "WITH INTERNAL SPACE", "内部文本"):
        assert getattr(make_entity(entity_type, **{field_name: valid}), field_name) == valid
    for bad in (None, True, 42, [], {}, "", " \t\n", "x" * (limit + 1), " x", "x ", "\tx", "x\n", "\u3000x", "x\u00a0"):
        with pytest.raises(DomainError, match=field_name):
            make_entity(entity_type, **{field_name: bad})


@pytest.mark.parametrize("state", ["ACTIVE", "PAUSED", "ENDED", "FUTURE_STATE", "future state"])
def test_history_allows_future_string_states_without_enum(state):
    history = make_entity(RelationshipHistory, state=state)
    assert type(history.state) is str
    assert history.state == state


@pytest.mark.parametrize("note", [None, "", " \t\n", "私人备注", "x" * 10000])
def test_note_is_optional_unrestricted_text(note):
    assert make_entity(RelationshipHistory, note=note).note == note


@pytest.mark.parametrize("bad", [["PRIVATE_NOTE"], {"PRIVATE_NOTE": 1}, 42, True])
def test_note_type_errors_do_not_expose_private_values(bad):
    with pytest.raises(DomainError, match="note") as error:
        make_entity(RelationshipHistory, note=bad)
    assert str(error.value) == "note must be a string"
    assert "PRIVATE_NOTE" not in str(error.value)


@pytest.mark.parametrize("entity_type,field_name", [(Relationship, "relationship_type"), (RelationshipHistory, "state")])
def test_text_errors_do_not_expose_private_values(entity_type, field_name):
    with pytest.raises(DomainError) as error:
        make_entity(entity_type, **{field_name: " PRIVATE_VALUE "})
    assert "PRIVATE_VALUE" not in str(error.value)


def test_replace_revalidates_without_mutating_entities():
    relationship = make_entity(Relationship)
    with pytest.raises(DomainError):
        replace(relationship, person_a_id=relationship.person_b_id)
    with pytest.raises(DomainError):
        replace(relationship, relationship_type=" FRIEND ")
    assert relationship.relationship_type == "FRIEND"
    history = make_entity(RelationshipHistory)
    with pytest.raises(FrozenInstanceError):
        history.state = "ENDED"
    with pytest.raises(DomainError):
        replace(history, state=" ACTIVE ")
    assert history.state == "ACTIVE"


def test_import_is_stdlib_only_and_creates_no_startup_instances():
    script = '''
import importlib.abc
import sys
class RejectInfrastructure(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'PySide6', 'sqlite3', 'alembic'} or fullname.startswith(('app.storage', 'app.services', 'app.repositories', 'app.ui')):
            raise AssertionError('Forbidden domain dependency: ' + fullname)
sys.meta_path.insert(0, RejectInfrastructure())
from app.domain.entities import Relationship, RelationshipHistory
for module_name, module in tuple(sys.modules.items()):
    if module_name.startswith('app.domain'):
        assert not any(isinstance(value, (Relationship, RelationshipHistory)) for value in vars(module).values())
assert not any(name.split('.')[0] in {'sqlalchemy', 'PySide6', 'sqlite3', 'alembic'} for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-s", "-c", script], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
