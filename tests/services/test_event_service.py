"""Real SQLite service acceptance, atomic failures, retained facts and explicit links."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import event as sa_event, func, select
from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.services.database import initialize_database
from app.services.event import EventService, ParticipantInput
from app.services.event_repositories import sql_event_repositories
from app.services.person import PersonService
from app.services.relationship import RelationshipService
from app.services.unit_of_work import transaction
from app.services.workspace import WorkspaceService
from app.storage.models import EventModel, EventParticipantModel, RelationshipHistoryModel, RelationshipModel
from app.storage.session import ThreadBoundSession

NOW = datetime(2026, 9, 30, 12, 0, 0, 123456, tzinfo=timezone.utc)
DAY = date(2026, 9, 30)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "event-data")
    runtime = initialize_database(config)
    try:
        workspaces, people = WorkspaceService(runtime.sessions), PersonService(runtime.sessions)
        workspace, foreign = workspaces.create("event fixture"), workspaces.create("other fixture")
        a, b = people.create(workspace.id, "同名"), people.create(workspace.id, "同名")
        c = people.create(foreign.id, "foreign")
        clock = SimpleNamespace(now=NOW)
        service = EventService(runtime.sessions, clock=lambda: clock.now)
        relationships = RelationshipService(runtime.sessions, clock=lambda: clock.now)
        yield SimpleNamespace(config=config, runtime=runtime, workspaces=workspaces, people=people,
            service=service, relationships=relationships, ws=workspace.id, foreign=foreign.id,
            a=a, b=b, c=c, clock=clock)
    finally:
        runtime.close()


def create(env, **changes):
    values = dict(title="  共同活动  ", event_type="  Meeting  ", occurred_on=DAY,
                  participants=(ParticipantInput(env.a.id, "host", "原始参与"), env.b.id))
    values.update(changes)
    return env.service.create_event(env.ws, **values)


def counts(env):
    with transaction(env.runtime.sessions) as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in
                     (EventModel, EventParticipantModel, RelationshipModel, RelationshipHistoryModel))


def test_crud_detached_dtos_and_no_automatic_relationship(env):
    entity = create(env)
    assert entity.title == "共同活动" and entity.event_type == "Meeting"
    detail = env.service.detail(env.ws, entity.id)
    assert {p.person.id for p in detail.participants} == {env.a.id, env.b.id}
    assert detail.histories == () and counts(env) == (1, 2, 0, 0)
    revised = env.service.update_event(env.ws, entity.id, title=" Edited ", record_kind="OBSERVATION",
        location="地点", description="我感觉当时更主动", event_type="Observation",
        occurred_on=None, started_at=NOW, ended_at=NOW+timedelta(hours=2))
    assert revised.title == "Edited" and revised.record_kind == "OBSERVATION"
    assert revised.updated_at > entity.updated_at and revised.created_at == entity.created_at
    assert env.service.get(env.ws, entity.id) == revised
    assert detail.event == entity  # Detached immutable snapshots do not change.
    assert counts(env) == (1, 2, 0, 0)


@pytest.mark.parametrize("changes", [dict(title=""), dict(title="x"*201), dict(event_type=" "),
    dict(event_type="x"*201), dict(record_kind="INFERENCE"), dict(occurred_on=None),
    dict(started_at=NOW), dict(ended_at=NOW),
    dict(occurred_on=None, started_at=NOW, ended_at=NOW-timedelta(microseconds=1)),
    dict(occurred_on=None, started_at=NOW.replace(tzinfo=None)),
    dict(occurred_on=NOW), dict(participants=("not UUID",)), dict(participants=None)])
def test_invalid_create_writes_nothing(env, changes):
    with pytest.raises(DomainError):
        create(env, **changes)
    assert counts(env) == (0, 0, 0, 0)


@pytest.mark.parametrize("kind", ["FACT", "OBSERVATION"])
def test_kinds_and_date_only(env, kind):
    entity = create(env, record_kind=kind)
    assert entity.record_kind == kind and entity.occurred_on == DAY
    assert entity.started_at is None and entity.ended_at is None


@pytest.mark.parametrize("method", ["create", "add", "update"])
def test_cross_workspace_participant_service_validation(env, method):
    entity = create(env, participants=()) if method != "create" else None
    before = counts(env)
    with pytest.raises(WorkspaceMismatchError):
        if method == "create":
            create(env, participants=(env.a.id, env.c.id))
        elif method == "add":
            env.service.add_participant(env.ws, entity.id, env.c.id)
        else:
            env.service.update_event(env.ws, entity.id, participants=(env.a.id, env.c.id))
    assert counts(env) == before


@pytest.mark.parametrize("method", ["create", "add", "update"])
def test_archived_person_cannot_be_new_participant(env, method):
    entity = create(env, participants=()) if method != "create" else None
    env.people.soft_delete(env.ws, env.a.id)
    before = counts(env)
    with pytest.raises(DomainError):
        if method == "create":
            create(env)
        elif method == "add":
            env.service.add_participant(env.ws, entity.id, env.a.id)
        else:
            env.service.update_event(env.ws, entity.id, participants=(env.a.id,))
    assert counts(env) == before


def test_archived_historical_participant_retained_through_edit_delete_restore(env):
    entity = create(env)
    original = env.service.detail(env.ws, entity.id)
    env.people.soft_delete(env.ws, env.a.id)
    archived = env.service.detail(env.ws, entity.id)
    assert archived.event == entity
    assert tuple(p.participant for p in archived.participants) == tuple(p.participant for p in original.participants)
    assert next(p for p in archived.participants if p.person.id == env.a.id).person.deleted_at is not None
    revised = env.service.update_event(env.ws, entity.id, title="历史编辑",
        participants=(ParticipantInput(env.a.id, "host", "原始参与"), env.b.id))
    assert tuple(p.participant for p in env.service.detail(env.ws, entity.id).participants) == tuple(p.participant for p in original.participants)
    env.service.soft_delete_event(env.ws, entity.id)
    assert env.service.list_events(env.ws) == []
    assert env.service.list_events_by_person(env.ws, env.a.id) == []
    assert len(env.service.list_events_by_person(env.ws, env.a.id, include_deleted=True)) == 1
    restored = env.service.restore_event(env.ws, entity.id)
    assert restored.title == revised.title and restored.deleted_at is None
    assert counts(env) == (1, 2, 0, 0)
    assert len(env.service.list_events_by_person(env.ws, env.a.id)) == 1


def test_participant_add_remove_update_and_unique(env):
    entity = create(env, participants=())
    entry = env.service.add_participant(env.ws, entity.id, env.a.id, role="host", note="hello")
    with pytest.raises(ConflictError):
        env.service.add_participant(env.ws, entity.id, env.a.id, role="guest")
    with pytest.raises(ConflictError):
        create(env, participants=(env.a.id, env.a.id))
    updated = env.service.update_event(env.ws, entity.id,
        participants=(ParticipantInput(env.a.id, "guest", "changed"), env.b.id))
    detail = env.service.detail(env.ws, entity.id)
    retained = next(p.participant for p in detail.participants if p.person.id == env.a.id)
    assert retained.id == entry.id and retained.created_at == entry.created_at
    assert retained.role == "guest" and retained.note == "changed"
    env.service.remove_participant(env.ws, entity.id, env.a.id)
    assert len(env.service.detail(env.ws, entity.id).participants) == 1
    assert env.service.get(env.ws, entity.id).updated_at > updated.updated_at
    with pytest.raises(NotFoundError):
        env.service.remove_participant(env.ws, entity.id, env.a.id)


def test_soft_delete_restore_idempotent_and_default_hidden(env):
    entity = create(env)
    deleted = env.service.soft_delete_event(env.ws, entity.id)
    assert deleted.deleted_at is not None and deleted.updated_at > entity.updated_at
    assert env.service.soft_delete_event(env.ws, entity.id) == deleted
    with pytest.raises(NotFoundError):
        env.service.get(env.ws, entity.id)
    assert env.service.get(env.ws, entity.id, include_deleted=True) == deleted
    assert len(env.service.list_events(env.ws, include_deleted=True)) == 1
    restored = env.service.restore_event(env.ws, entity.id)
    assert restored.updated_at > deleted.updated_at and restored.deleted_at is None
    assert env.service.restore_event(env.ws, entity.id) == restored
    assert counts(env) == (1, 2, 0, 0)


@pytest.mark.parametrize("changes", [dict(workspace_id=uuid4()), dict(created_at=NOW), dict(id=uuid4()),
    dict(deleted_at=NOW), dict(title=""), dict(ended_at=NOW)])
def test_invalid_update_is_atomic(env, changes):
    entity = create(env)
    old = env.service.detail(env.ws, entity.id)
    with pytest.raises((DomainError, TypeError)):
        env.service.update_event(env.ws, entity.id, **changes)
    assert env.service.detail(env.ws, entity.id) == old


def test_future_past_backdated_and_mixed_date_time_sql_sort(env):
    late = create(env, title="future", occurred_on=None, started_at=NOW+timedelta(days=10), participants=())
    early = create(env, title="backdated", occurred_on=DAY-timedelta(days=100), participants=())
    middle = create(env, title="date", participants=())
    timed = create(env, title="time", occurred_on=None, started_at=NOW, participants=())
    micro = create(env, title="micro", occurred_on=None, started_at=NOW+timedelta(microseconds=1), participants=())
    midnight = create(env, title="midnight", occurred_on=None, started_at=NOW.replace(hour=0, minute=0, second=0, microsecond=0), participants=())
    rows = env.service.list_events(env.ws)
    assert [r.event.id for r in rows[:3]] == [late.id, micro.id, timed.id]
    assert {r.event.id for r in rows[3:5]} == {middle.id, midnight.id}
    assert rows[3].event.id.int > rows[4].event.id.int
    assert rows[-1].event.id == early.id
    # Backdating changes occurrence sorting, never relationship state.
    env.service.update_event(env.ws, late.id, started_at=NOW-timedelta(days=200))
    assert env.service.list_events(env.ws)[-1].event.id == late.id


@pytest.mark.parametrize("token", ["%", "_", "\\", "a%_\\b"])
@pytest.mark.parametrize("field", ["title", "description", "event_type"])
def test_literal_basic_search(env, token, field):
    one = create(env, participants=(), **{field: f"find {token} exactly"})
    create(env, title="other", event_type="other", description="other", participants=())
    assert [row.event.id for row in env.service.list_events(env.ws, token)] == [one.id]
    assert env.service.list_events(env.foreign, token) == []


def test_list_by_person_workspace_and_participant_filter(env):
    one = create(env, participants=(env.a.id,))
    create(env, title="only b", participants=(env.b.id,))
    assert [row.event.id for row in env.service.list_events_by_person(env.ws, env.a.id)] == [one.id]
    with pytest.raises(NotFoundError):
        env.service.list_events_by_person(env.foreign, env.a.id)
    for method in (env.service.get, env.service.detail, env.service.soft_delete_event, env.service.restore_event):
        with pytest.raises(NotFoundError):
            method(env.foreign, one.id)
    with pytest.raises(NotFoundError):
        env.service.create_event(uuid4(), "event", "type", occurred_on=DAY)


@pytest.mark.parametrize("operation", ["create", "update"])
def test_participant_write_failure_rolls_back_whole_event(env, monkeypatch, operation):
    from app.storage.event_repositories import SQLEventParticipantRepository
    original = SQLEventParticipantRepository.add
    state = {"count": 0}
    def failing(self, entry):
        result = original(self, entry)
        state["count"] += 1
        if state["count"] == 2:
            raise RuntimeError("synthetic second participant write failure")
        return result
    existing = create(env, participants=()) if operation == "update" else None
    monkeypatch.setattr(SQLEventParticipantRepository, "add", failing)
    before = counts(env)
    with pytest.raises(RuntimeError):
        if operation == "create":
            create(env)
        else:
            env.service.update_event(env.ws, existing.id, title="must rollback", participants=(env.a.id, env.b.id))
    assert counts(env) == before
    if existing:
        assert env.service.get(env.ws, existing.id) == existing


@contextmanager
def fail_commit(env):
    def before_commit(session):
        if session.get_bind() is env.runtime.engine and session.info.get("ris_write"):
            raise RuntimeError("synthetic commit failure")
    sa_event.listen(ThreadBoundSession, "before_commit", before_commit)
    try:
        yield
    finally:
        sa_event.remove(ThreadBoundSession, "before_commit", before_commit)


@pytest.mark.parametrize("operation", ["create", "update", "delete", "restore", "add", "remove", "attach", "detach"])
def test_commit_failure_rolls_back_operation(env, operation):
    entity = create(env, participants=(env.a.id,))
    relationship = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    history = env.relationships.history(env.ws, relationship.id)[0]
    if operation == "restore":
        env.service.soft_delete_event(env.ws, entity.id)
    if operation == "detach":
        env.service.attach_event_to_history(env.ws, history.id, entity.id)
    before = env.service.detail(env.ws, entity.id, include_deleted=True)
    before_counts = counts(env)
    with fail_commit(env), pytest.raises(RuntimeError):
        if operation == "create": create(env)
        elif operation == "update": env.service.update_event(env.ws, entity.id, title="changed")
        elif operation == "delete": env.service.soft_delete_event(env.ws, entity.id)
        elif operation == "restore": env.service.restore_event(env.ws, entity.id)
        elif operation == "add": env.service.add_participant(env.ws, entity.id, env.b.id)
        elif operation == "remove": env.service.remove_participant(env.ws, entity.id, env.a.id)
        else: env.service.attach_event_to_history(env.ws, history.id, None if operation == "detach" else entity.id)
    assert env.service.detail(env.ws, entity.id, include_deleted=True) == before
    assert counts(env) == before_counts


def test_attach_detach_many_histories_keeps_append_only_facts_and_state(env):
    entity = create(env)
    rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    second_rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Colleague")
    first = env.relationships.history(env.ws, rel.id)[0]
    second = env.relationships.history(env.ws, second_rel.id)[0]
    future = env.relationships.add_history(env.ws, rel.id, "PAUSED", effective_at=NOW+timedelta(days=1))
    baseline = env.relationships.current(env.ws, rel.id)
    linked = env.service.attach_event_to_history(env.ws, first.id, entity.id)
    assert linked == replace(first, event_id=entity.id)
    assert env.service.attach_event_to_history(env.ws, first.id, entity.id) == linked
    env.service.attach_relationship_history(env.ws, second.id, entity.id)
    env.service.attach_event_to_history(env.ws, future.id, entity.id)
    assert len(env.service.detail(env.ws, entity.id).histories) == 3
    assert counts(env) == (1, 2, 2, 3)
    assert env.relationships.current(env.ws, rel.id).relationship == baseline.relationship
    assert env.relationships.current(env.ws, rel.id).current_state == "ACTIVE"
    env.service.soft_delete_event(env.ws, entity.id)
    assert len(env.service.detail(env.ws, entity.id, include_deleted=True).histories) == 3
    env.service.restore_event(env.ws, entity.id)
    assert env.relationships.current(env.ws, rel.id).current_state == "ACTIVE"
    detached = env.service.attach_event_to_history(env.ws, first.id, None)
    assert detached == first
    assert counts(env) == (1, 2, 2, 3)
    assert len(env.service.detail(env.ws, entity.id).histories) == 2
    assert env.relationships.current(env.ws, rel.id).relationship == baseline.relationship


@pytest.mark.parametrize("case", ["foreign_event", "foreign_history", "missing_event", "missing_history"])
def test_attach_rejects_wrong_workspace_or_missing_objects(env, case):
    entity = create(env)
    rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    history = env.relationships.history(env.ws, rel.id)[0]
    foreign_event = env.service.create_event(env.foreign, "foreign", "type", occurred_on=DAY)
    history_id, event_id, ws = history.id, entity.id, env.ws
    if case == "foreign_event": event_id = foreign_event.id
    elif case == "foreign_history": ws, event_id = env.foreign, foreign_event.id
    elif case == "missing_event": event_id = uuid4()
    else: history_id = uuid4()
    with pytest.raises(WorkspaceMismatchError):
        env.service.attach_event_to_history(ws, history_id, event_id)
    assert env.relationships.history(env.ws, rel.id) == (history,)


def test_restart_utc_roundtrip_participants_and_history_links(env):
    zone = timezone(timedelta(hours=-7))
    start = NOW.astimezone(zone)
    entity = create(env, occurred_on=None, started_at=start, ended_at=start+timedelta(hours=2))
    rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    history = env.relationships.history(env.ws, rel.id)[0]
    env.service.attach_event_to_history(env.ws, history.id, entity.id)
    original = env.service.detail(env.ws, entity.id)
    env.runtime.close()
    runtime = initialize_database(env.config)
    try:
        reloaded = EventService(runtime.sessions).detail(env.ws, entity.id)
        assert reloaded == original
        for instant in (reloaded.event.started_at, reloaded.event.ended_at, reloaded.event.created_at,
                        reloaded.event.updated_at, reloaded.participants[0].participant.created_at):
            assert instant.tzinfo is timezone.utc
        assert reloaded.event.started_at == NOW
        assert reloaded.histories[0].history.event_id == entity.id
    finally:
        runtime.close()


def test_service_private_text_not_logged(env, caplog):
    secret = "private-secret-observation"
    with caplog.at_level("ERROR", logger="app.storage.database"):
        with pytest.raises(DomainError):
            create(env, title=secret, description=secret, record_kind="INFERENCE")
    assert secret not in caplog.text


def test_stale_detach_cannot_clear_another_events_current_reference(env):
    one = create(env)
    two = create(env, title="second")
    rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    history = env.relationships.history(env.ws, rel.id)[0]
    env.service.attach_event_to_history(env.ws, history.id, one.id)
    env.service.attach_event_to_history(env.ws, history.id, two.id)
    with pytest.raises(ConflictError):
        env.service.attach_event_to_history(env.ws, history.id, None, expected_event_id=one.id)
    assert env.relationships.history(env.ws, rel.id)[0].event_id == two.id
    assert len(env.service.detail(env.ws, two.id).histories) == 1
    assert env.service.detail(env.ws, one.id).histories == ()
    # A stale detail that still believes H points at one must not re-point it.
    with pytest.raises(ConflictError):
        env.service.attach_event_to_history(env.ws, history.id, one.id, expected_event_id=one.id)
    assert env.relationships.history(env.ws, rel.id)[0].event_id == two.id
    env.service.attach_relationship_history(env.ws, history.id, None, expected_event_id=two.id)
    assert env.relationships.history(env.ws, rel.id) == (history,)


def test_archived_relationship_endpoints_still_allow_explicit_history_links(env):
    entity = create(env)
    rel = env.relationships.create(env.ws, env.a.id, env.b.id, "Friend")
    history = env.relationships.history(env.ws, rel.id)[0]
    env.relationships.soft_delete(env.ws, rel.id)
    env.people.soft_delete(env.ws, env.b.id)
    linked = env.service.attach_event_to_history(env.ws, history.id, entity.id)
    assert linked == replace(history, event_id=entity.id)
    detail = env.service.detail(env.ws, entity.id)
    assert detail.histories[0].relationship.deleted_at is not None
    assert env.service.attach_event_to_history(env.ws, history.id, None) == history
    assert counts(env) == (1, 2, 1, 1)


def test_archived_retained_participant_metadata_edit_preserves_identity(env):
    entity = create(env)
    entry = next(p.participant for p in env.service.detail(env.ws, entity.id).participants if p.person.id == env.a.id)
    env.people.soft_delete(env.ws, env.a.id)
    env.service.update_event(env.ws, entity.id, participants=(ParticipantInput(env.a.id, "guest", "updated note"), env.b.id))
    revised = next(p.participant for p in env.service.detail(env.ws, entity.id).participants if p.person.id == env.a.id)
    assert revised == replace(entry, role="guest", note="updated note")
    assert revised.id == entry.id and revised.created_at == entry.created_at

