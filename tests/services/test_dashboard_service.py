"""Phase 7 Dashboard projection: real SQLite, real migrations, read-only.

Covers the frozen Dashboard requirements: recent people/events/relationship
changes/evidence with explicit SQL ordering and LIMIT, the counts (including
"active" meaning the current state is ACTIVE), soft delete, workspace isolation,
and the "no N+1", "no cached statistics" and "writes nothing" guarantees.
"""
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.entities import Event, Evidence, Person
from app.domain.errors import DomainError, NotFoundError
from app.services.dashboard import DashboardCounts, DashboardSnapshot, RelationshipChange
from projection_fixtures import (DAY, NOW, build_env, count_selects, database_state, make_event,
                                 make_evidence, make_history, make_relationship)


@pytest.fixture
def env(tmp_path):
    built = build_env(tmp_path, "dashboard-data")
    try:
        yield built
    finally:
        built.runtime.close()


def test_snapshot_returns_domain_values_and_matching_counts(env):
    make_event(env, participants=(env.a.id,))
    make_relationship(env, "Friend")
    snapshot = env.dashboard.snapshot(env.ws)
    assert type(snapshot) is DashboardSnapshot and snapshot.workspace_id == env.ws
    assert type(snapshot.counts) is DashboardCounts
    assert all(type(person) is Person for person in snapshot.recent_people)
    assert all(type(event) is Event for event in snapshot.recent_events)
    assert all(type(change) is RelationshipChange for change in snapshot.recent_relationship_changes)
    assert all(type(item) is Evidence for item in snapshot.recent_evidence)
    assert all(isinstance(value, tuple) for value in
               (snapshot.recent_people, snapshot.recent_events, snapshot.recent_relationship_changes,
                snapshot.recent_evidence))
    assert snapshot.counts == env.dashboard.counts(env.ws)


def test_recent_people_are_the_newest_five_on_the_default_path(env):
    created = [env.people.create(env.ws, f"人物{index}") for index in range(6)]
    snapshot = env.dashboard.snapshot(env.ws)
    assert [person.id for person in snapshot.recent_people] == [person.id for person in reversed(created[1:])]
    assert [person.created_at for person in snapshot.recent_people] == sorted(
        (person.created_at for person in snapshot.recent_people), reverse=True)

    env.people.soft_delete(env.ws, created[-1].id)
    assert [person.id for person in env.dashboard.snapshot(env.ws).recent_people] == [
        person.id for person in reversed(created[:5])]
    assert created[-1].id in [person.id for person in
                              env.dashboard.snapshot(env.ws, include_deleted=True).recent_people]


def test_recent_events_follow_the_frozen_event_occurrence_order(env):
    make_event(env, title="未来", occurred_on=None, started_at=NOW + timedelta(days=2))
    make_event(env, title="今天时间", occurred_on=None, started_at=NOW - timedelta(hours=1))
    make_event(env, title="今天日期", occurred_on=DAY)
    make_event(env, title="昨天日期", occurred_on=DAY - timedelta(days=1))
    make_event(env, title="补录", occurred_on=DAY - timedelta(days=90))
    for index in range(3):
        make_event(env, title=f"更早{index}", occurred_on=None, started_at=NOW - timedelta(days=10 + index))

    snapshot = env.dashboard.snapshot(env.ws)
    # The Dashboard order is the Events page order: the Events page slices its own
    # explicit SQL ordering, so both must agree.
    canonical = [detail.event.id for detail in env.events.list_events(env.ws)]
    assert [event.id for event in snapshot.recent_events] == canonical[:5]
    assert [event.title for event in snapshot.recent_events] == ["未来", "今天时间", "今天日期", "昨天日期", "更早0"]

    env.events.soft_delete_event(env.ws, snapshot.recent_events[0].id)
    widened = env.dashboard.snapshot(env.ws, include_deleted=True).recent_events
    assert snapshot.recent_events[0].id in [event.id for event in widened]
    assert snapshot.recent_events[0].id not in [event.id for event in env.dashboard.snapshot(env.ws).recent_events]


def test_recent_relationship_changes_are_newest_first_and_limited(env):
    relationship = make_relationship(env, "Friend", effective_at=NOW - timedelta(days=30))
    rows = [env.relationships.history(env.ws, relationship.id)[0]]
    for index, state in enumerate(("PAUSED", "ACTIVE", "ENDED", "PAUSED", "ACTIVE")):
        rows.append(make_history(env, relationship, state, effective_at=NOW - timedelta(days=20 - index)))
    planned = make_history(env, relationship, "ACTIVE", effective_at=NOW + timedelta(days=5))

    snapshot = env.dashboard.snapshot(env.ws)
    changes = snapshot.recent_relationship_changes
    expected = sorted((*rows, planned), key=lambda row: row.effective_at, reverse=True)[:5]
    assert [change.history.id for change in changes] == [row.id for row in expected]
    assert changes[0].history.id == planned.id          # a planned change is displayed
    assert all(change.relationship.id == relationship.id for change in changes)
    assert changes[0].relationship == env.relationships.get(env.ws, relationship.id)

    env.relationships.soft_delete(env.ws, relationship.id)
    assert env.dashboard.snapshot(env.ws).recent_relationship_changes == ()
    assert len(env.dashboard.snapshot(env.ws, include_deleted=True).recent_relationship_changes) == 5


def test_recent_evidence_is_newest_first_and_limited(env):
    event = make_event(env, title="证据宿主", occurred_on=DAY - timedelta(days=200))
    created = []
    for index in range(6):
        created.append(make_evidence(env, event, title=f"证据{index}"))
        env.advance(minutes=1)

    snapshot = env.dashboard.snapshot(env.ws)
    assert [item.id for item in snapshot.recent_evidence] == [item.id for item in reversed(created[1:])]
    assert [item.created_at for item in snapshot.recent_evidence] == sorted(
        (item.created_at for item in snapshot.recent_evidence), reverse=True)

    env.evidence.soft_delete_evidence(env.ws, created[-1].id)
    assert created[-1].id not in [item.id for item in env.dashboard.snapshot(env.ws).recent_evidence]
    assert created[-1].id in [item.id for item in
                              env.dashboard.snapshot(env.ws, include_deleted=True).recent_evidence]


def test_counts_people_and_current_relationship_state(env):
    assert env.dashboard.counts(env.ws).people == 2
    assert env.dashboard.counts(env.foreign).people == 1

    friend = make_relationship(env, "Friend")
    colleague = make_relationship(env, "Colleague")
    family = make_relationship(env, "Family")
    assert env.dashboard.counts(env.ws).active_relationships == 3

    # The current state is the newest state history, so the clock moves on first.
    env.advance(minutes=1)
    make_history(env, friend, "ENDED")
    make_history(env, colleague, "PAUSED")
    assert env.dashboard.counts(env.ws).active_relationships == 1

    # A planned future state is not the current state.
    make_history(env, colleague, "ACTIVE", effective_at=NOW + timedelta(days=30))
    assert env.dashboard.counts(env.ws).active_relationships == 1

    env.relationships.soft_delete(env.ws, family.id)
    assert env.dashboard.counts(env.ws).active_relationships == 0
    assert env.dashboard.counts(env.ws, include_deleted=True).active_relationships == 1

    env.people.soft_delete(env.ws, env.b.id)
    assert env.dashboard.counts(env.ws).people == 1
    assert env.dashboard.counts(env.ws, include_deleted=True).people == 2


def test_count_windows_use_the_event_instant_and_the_evidence_creation(env):
    host = make_event(env, title="证据宿主", occurred_on=DAY - timedelta(days=200))
    make_event(env, title="3天前", occurred_on=None, started_at=NOW - timedelta(days=3))
    make_event(env, title="10天前", occurred_on=None, started_at=NOW - timedelta(days=10))
    make_event(env, title="40天前", occurred_on=None, started_at=NOW - timedelta(days=40))
    make_event(env, title="未来", occurred_on=None, started_at=NOW + timedelta(days=1))
    make_event(env, title="2天前日期", occurred_on=(NOW - timedelta(days=2)).date())
    make_event(env, title="20天前日期", occurred_on=(NOW - timedelta(days=20)).date())

    counts = env.dashboard.counts(env.ws)
    assert (counts.events_7d, counts.events_30d) == (2, 4)

    make_evidence(env, host, title="刚刚")
    env.advance(days=-10)
    old = make_evidence(env, host, title="十天前证据")
    env.advance(days=10)
    counts = env.dashboard.counts(env.ws)
    assert (counts.evidence_7d, counts.evidence_30d) == (1, 2)

    env.evidence.soft_delete_evidence(env.ws, old.id)
    assert env.dashboard.counts(env.ws).evidence_30d == 1
    assert env.dashboard.counts(env.ws, include_deleted=True).evidence_30d == 2


def test_workspace_isolation_for_every_list_and_count(env):
    event = make_event(env, title="本工作区事件", participants=(env.a.id,))
    make_evidence(env, event)
    make_relationship(env, "Friend")

    other = env.people.create(env.foreign, "Dee")
    foreign_event = env.events.create_event(env.foreign, "外部事件", "Meeting", occurred_on=DAY)
    foreign_relationship = env.relationships.create(env.foreign, env.c.id, other.id, "Friend")
    foreign_history = env.relationships.history(env.foreign, foreign_relationship.id)[0]

    snapshot = env.dashboard.snapshot(env.foreign)
    assert [item.id for item in snapshot.recent_events] == [foreign_event.id]
    assert [item.id for item in snapshot.recent_evidence] == []
    assert {person.id for person in snapshot.recent_people} == {env.c.id, other.id}
    assert [change.history.id for change in snapshot.recent_relationship_changes] == [foreign_history.id]
    assert snapshot.counts == DashboardCounts(people=2, active_relationships=1, events_7d=1,
                                              events_30d=1, evidence_7d=0, evidence_30d=0)

    mine = env.dashboard.snapshot(env.ws)
    assert event.id in [item.id for item in mine.recent_events]
    assert foreign_event.id not in [item.id for item in mine.recent_events]

    for call in (lambda: env.dashboard.snapshot(uuid4()), lambda: env.dashboard.counts(uuid4())):
        with pytest.raises(NotFoundError):
            call()
    with pytest.raises(DomainError):
        env.dashboard.counts("not-a-uuid")
    with pytest.raises(DomainError):
        env.dashboard.snapshot("not-a-uuid")


def test_dashboard_recomputes_instead_of_caching_and_writes_nothing(env):
    event = make_event(env, title="事件", participants=(env.a.id,))
    make_relationship(env, "Friend")
    make_evidence(env, event)

    before = database_state(env)
    first = env.dashboard.snapshot(env.ws)
    assert env.dashboard.snapshot(env.ws) == first
    assert env.dashboard.counts(env.ws) == first.counts
    assert database_state(env) == before

    env.advance(minutes=1)
    make_event(env, title="新事件", occurred_on=None, started_at=NOW, participants=(env.a.id,))
    refreshed = env.dashboard.counts(env.ws)
    assert refreshed.events_7d == first.counts.events_7d + 1
    assert refreshed.events_30d == first.counts.events_30d + 1
    assert refreshed.people == first.counts.people
    assert env.dashboard.snapshot(env.ws).recent_events[0].title == "新事件"


def test_query_count_is_constant_and_never_per_row(env):
    event = make_event(env, participants=(env.a.id,))
    make_relationship(env, "Friend")
    make_evidence(env, event)
    with count_selects(env) as sparse_counts:
        env.dashboard.counts(env.ws)
    with count_selects(env) as sparse_snapshot:
        env.dashboard.snapshot(env.ws)

    for index in range(8):
        created = make_event(env, title=f"事件{index}", occurred_on=None,
                             started_at=NOW - timedelta(days=index), participants=(env.a.id, env.b.id))
        make_evidence(env, created, title=f"证据{index}")
        relationship = make_relationship(env, f"Kind{index}")
        make_history(env, relationship, "PAUSED")
        make_history(env, relationship, "ENDED")
    with count_selects(env) as dense_counts:
        env.dashboard.counts(env.ws)
    with count_selects(env) as dense_snapshot:
        env.dashboard.snapshot(env.ws)

    # Six COUNT aggregates plus one query per list, whatever the row count is.
    assert len(dense_counts) == len(sparse_counts) <= 8
    assert len(dense_snapshot) == len(sparse_snapshot) <= 12
