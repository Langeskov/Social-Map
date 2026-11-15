"""Pure Phase 5 entities: time alternatives and facts/observations only."""
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import pytest
from app.domain.entities import Event, EventParticipant, RelationshipHistory
from app.domain.errors import DomainError

NOW = datetime(2026, 9, 30, 19, 30, 0, 123456, tzinfo=timezone(timedelta(hours=8)))


def event(**changes):
    values = dict(workspace_id=uuid4(), title="  见面  ", event_type="  Meeting  ", occurred_on=date(2026, 9, 30))
    values.update(changes)
    return Event(**values)


def test_exact_fields_frozen_and_trim():
    entity = event()
    assert entity.title == "见面" and entity.event_type == "Meeting" and entity.record_kind == "FACT"
    assert {f.name for f in fields(Event)} == {"id", "workspace_id", "title", "event_type", "record_kind",
        "occurred_on", "started_at", "ended_at", "location", "description", "created_at", "updated_at", "deleted_at"}
    assert {f.name for f in fields(EventParticipant)} == {"id", "workspace_id", "event_id", "person_id", "role", "note", "created_at"}
    with pytest.raises(AttributeError):
        entity.title = "changed"
    assert event(event_type="meeting").event_type != entity.event_type


@pytest.mark.parametrize("name", ["title", "event_type"])
@pytest.mark.parametrize("value", ["", "  ", "x" * 201, None, 1, True])
def test_invalid_text(name, value):
    with pytest.raises(DomainError):
        event(**{name: value})


@pytest.mark.parametrize("name", ["title", "event_type"])
@pytest.mark.parametrize("value", ["x", "x" * 200, "  x  "])
def test_text_boundaries(name, value):
    assert getattr(event(**{name: value}), name) == value.strip()


@pytest.mark.parametrize("kind", ["FACT", "OBSERVATION"])
def test_record_kinds(kind):
    assert event(record_kind=kind).record_kind == kind


@pytest.mark.parametrize("kind", ["INFERENCE", "fact", " FACT ", "", None, 42])
def test_reject_inference_and_unknown_kind(kind):
    with pytest.raises(DomainError):
        event(record_kind=kind)


def test_date_not_datetime_or_timezone_converted():
    day = date(1600, 1, 1)
    assert event(occurred_on=day).occurred_on is day
    for invalid in (NOW, "2026-09-30", 42):
        with pytest.raises(DomainError):
            event(occurred_on=invalid)


@pytest.mark.parametrize("changes", [
    dict(occurred_on=None), dict(started_at=NOW), dict(ended_at=NOW),
    dict(occurred_on=None, ended_at=NOW),
    dict(occurred_on=None, started_at=NOW, ended_at=NOW-timedelta(microseconds=1)),
])
def test_invalid_time_alternatives(changes):
    with pytest.raises(DomainError):
        event(**changes)


@pytest.mark.parametrize("end", [None, NOW, NOW+timedelta(hours=2)])
def test_time_event_utc(end):
    entity = event(occurred_on=None, started_at=NOW, ended_at=end)
    assert entity.started_at == NOW.astimezone(timezone.utc)
    assert entity.started_at.tzinfo is timezone.utc
    assert entity.ended_at == (end.astimezone(timezone.utc) if end else None)


@pytest.mark.parametrize("name", ["started_at", "ended_at", "created_at", "updated_at", "deleted_at"])
def test_naive_time_rejected(name):
    changes = dict(occurred_on=None, started_at=NOW)
    changes[name] = NOW.replace(tzinfo=None)
    with pytest.raises(DomainError):
        event(**changes)


@pytest.mark.parametrize("name", ["id", "workspace_id"])
@pytest.mark.parametrize("value", [None, True, "uuid", 1])
def test_event_uuid(name, value):
    with pytest.raises(DomainError):
        event(**{name: value})


@pytest.mark.parametrize("name", ["id", "workspace_id", "event_id", "person_id"])
@pytest.mark.parametrize("value", [None, True, "uuid", 1])
def test_participant_uuid(name, value):
    values = dict(workspace_id=uuid4(), event_id=uuid4(), person_id=uuid4())
    values[name] = value
    with pytest.raises(DomainError):
        EventParticipant(**values)


def test_participant_optional_metadata_and_utc():
    entity = EventParticipant(workspace_id=uuid4(), event_id=uuid4(), person_id=uuid4(), role="host", note="观察", created_at=NOW)
    assert entity.created_at.tzinfo is timezone.utc
    assert replace(entity, role=None).note == "观察"
    with pytest.raises(DomainError):
        replace(entity, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(DomainError):
        replace(entity, role="x"*201)


def test_history_optional_event_reference():
    history = RelationshipHistory(workspace_id=uuid4(), relationship_id=uuid4(), state="ACTIVE", effective_at=NOW)
    assert history.event_id is None
    id = uuid4()
    assert replace(history, event_id=id).event_id == id
    for invalid in (True, 1, str(id)):
        with pytest.raises(DomainError):
            replace(history, event_id=invalid)
