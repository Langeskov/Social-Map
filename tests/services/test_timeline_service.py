"""Phase 7 Timeline projection: real SQLite, real migrations, read-only.

Covers the frozen Timeline requirements: person and workspace timelines, DATE
and datetime Events, future and backdated timestamps, one reproducible order for
equal instants, soft delete, workspace isolation, limit, and the "no N+1" /
"writes nothing" guarantees.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.domain.errors import DomainError, NotFoundError
from app.services.timeline import (EVENT_KIND, RELATIONSHIP_CHANGE_KIND, TimelineItem, event_instant)
from projection_fixtures import (DAY, NOW, build_env, count_selects, database_state, make_event,
                                 make_evidence, make_history, make_relationship)


@pytest.fixture
def env(tmp_path):
    built = build_env(tmp_path, "timeline-data")
    try:
        yield built
    finally:
        built.runtime.close()


def ids(items):
    return [item.entity_id for item in items]


def kinds(items):
    return [item.kind for item in items]


def test_person_timeline_holds_participation_and_relationship_history(env):
    event = make_event(env, title="共同活动", participants=(env.a.id, env.b.id))
    only_b = make_event(env, title="只有 Bo", participants=(env.b.id,))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    paused = make_history(env, relationship, "PAUSED", note="互动减少", effective_at=NOW - timedelta(days=1))

    items = env.timeline.person_timeline(env.ws, env.a.id)
    assert all(type(item) is TimelineItem for item in items)
    assert {item.entity_id for item in items} == {event.id, initial.id, paused.id}
    assert only_b.id not in ids(items)

    event_item = next(item for item in items if item.kind == EVENT_KIND)
    assert event_item.entity_id == event.id and event_item.event_id == event.id
    assert event_item.relationship_id is None and event_item.title == "共同活动"
    assert set(event_item.person_ids) == {env.a.id, env.b.id}

    change = next(item for item in items if item.entity_id == paused.id)
    assert change.kind == RELATIONSHIP_CHANGE_KIND and change.entity_id == paused.id
    assert change.relationship_id == relationship.id and change.event_id is None
    assert set(change.person_ids) == {env.a.id, env.b.id}
    assert "PAUSED" in change.summary and "Friend" in change.summary
    assert "Friend" in change.title

    # The other endpoint gets its own timeline, including the Event it alone attended.
    assert {item.entity_id for item in env.timeline.person_timeline(env.ws, env.b.id)} == {
        event.id, only_b.id, initial.id, paused.id}


def test_workspace_timeline_covers_every_event_and_history_without_evidence(env):
    event = make_event(env, title="有证据的事件", participants=(env.a.id,))
    second = make_event(env, title="第二个事件", occurred_on=DAY - timedelta(days=5))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    ended = make_history(env, relationship, "ENDED", effective_at=NOW + timedelta(days=3))

    before = env.timeline.workspace_timeline(env.ws)
    assert set(kinds(before)) == {EVENT_KIND, RELATIONSHIP_CHANGE_KIND}
    # One merged order: the planned change, the initial state, then both Events.
    assert ids(before) == [ended.id, initial.id, event.id, second.id]
    assert [item.timestamp for item in before] == sorted((item.timestamp for item in before), reverse=True)
    # ENDED is a recorded change, never a deletion.
    assert ended.id in ids(before)

    make_evidence(env, event, title="聊天记录")
    assert env.timeline.workspace_timeline(env.ws) == before


def test_date_event_sorts_by_utc_midnight_without_rewriting_occurred_on(env):
    event = make_event(env, title="日期事件", occurred_on=DAY)
    item = env.timeline.workspace_timeline(env.ws)[0]
    assert item.kind == EVENT_KIND and item.entity_id == event.id
    assert item.timestamp == datetime(DAY.year, DAY.month, DAY.day, tzinfo=timezone.utc)
    assert item.timestamp.utcoffset() == timedelta(0)

    stored = env.events.get(env.ws, event.id)
    assert stored == event
    assert stored.occurred_on == DAY and stored.started_at is None and stored.ended_at is None
    assert event_instant(stored) == item.timestamp


def test_datetime_event_uses_started_at_normalized_to_utc(env):
    zone = timezone(timedelta(hours=-7))
    moment = NOW + timedelta(hours=3)
    event = make_event(env, title="时间事件", occurred_on=None, started_at=moment.astimezone(zone),
                       ended_at=(moment + timedelta(hours=2)).astimezone(zone), participants=(env.a.id,))
    item = env.timeline.person_timeline(env.ws, env.a.id)[0]
    assert item.entity_id == event.id and item.timestamp == moment
    assert item.timestamp.tzinfo is not None and item.timestamp.utcoffset() == timedelta(0)
    assert env.events.get(env.ws, event.id).started_at == moment


def test_future_timestamps_are_displayed_not_filtered(env):
    future_event = make_event(env, title="未来事件", occurred_on=None,
                              started_at=NOW + timedelta(days=10), participants=(env.a.id,))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    planned = make_history(env, relationship, "ENDED", effective_at=NOW + timedelta(days=20))

    for items in (env.timeline.workspace_timeline(env.ws),
                  env.timeline.person_timeline(env.ws, env.a.id)):
        assert ids(items) == [planned.id, future_event.id, initial.id]
        assert items[0].timestamp == NOW + timedelta(days=20)
        assert items[1].timestamp == NOW + timedelta(days=10)

    newest = env.timeline.person_timeline(env.ws, env.a.id, limit=1)
    assert [item.entity_id for item in newest] == [planned.id]


def test_backdated_event_sorts_last_and_keeps_its_own_date(env):
    recent = make_event(env, title="今天", occurred_on=DAY, participants=(env.a.id,))
    backdated = make_event(env, title="补录", occurred_on=DAY - timedelta(days=100), participants=(env.a.id,))
    items = env.timeline.person_timeline(env.ws, env.a.id)
    assert ids(items) == [recent.id, backdated.id]
    assert items[-1].timestamp == datetime(DAY.year, DAY.month, DAY.day, tzinfo=timezone.utc) - timedelta(days=100)
    assert env.events.get(env.ws, backdated.id).occurred_on == DAY - timedelta(days=100)


def test_equal_instants_use_one_fixed_reproducible_order(env):
    same = NOW.replace(microsecond=0)
    events = [make_event(env, title=f"事件{index}", occurred_on=None, started_at=same,
                         participants=(env.a.id,)) for index in range(3)]
    relationship = make_relationship(env, "Friend", effective_at=same)
    histories = [env.relationships.history(env.ws, relationship.id)[0]]
    histories.append(make_history(env, relationship, "PAUSED", effective_at=same))

    first = env.timeline.workspace_timeline(env.ws)
    second = env.timeline.workspace_timeline(env.ws)
    assert first == second
    assert kinds(first) == [EVENT_KIND] * 3 + [RELATIONSHIP_CHANGE_KIND] * 2
    assert ids(first)[:3] == sorted((event.id for event in events), key=str, reverse=True)
    assert ids(first)[3:] == sorted((history.id for history in histories), key=str, reverse=True)
    # The person timeline merges the same sources with the same frozen rule.
    assert env.timeline.person_timeline(env.ws, env.a.id) == first


def test_soft_delete_hides_only_the_deleted_row(env):
    event = make_event(env, title="将删除的事件", participants=(env.a.id,))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    ended = make_history(env, relationship, "ENDED", effective_at=NOW + timedelta(days=2))

    env.events.soft_delete_event(env.ws, event.id)
    assert ids(env.timeline.workspace_timeline(env.ws)) == [ended.id, initial.id]
    assert ids(env.timeline.person_timeline(env.ws, env.a.id)) == [ended.id, initial.id]
    assert {item.entity_id for item in env.timeline.workspace_timeline(env.ws, include_deleted=True)} == {
        event.id, initial.id, ended.id}

    env.relationships.soft_delete(env.ws, relationship.id)
    assert env.timeline.workspace_timeline(env.ws) == []
    assert {item.entity_id for item in
            env.timeline.workspace_timeline(env.ws, include_deleted=True)} == {event.id, initial.id, ended.id}
    # The append-only history of a deleted Relationship still exists.
    assert len(env.relationships.history(env.ws, relationship.id, include_deleted=True)) == 2
    with pytest.raises(NotFoundError):
        env.relationships.history(env.ws, relationship.id)


def test_archived_person_hides_their_timeline_but_never_recorded_history(env):
    event = make_event(env, title="参与事件", participants=(env.a.id, env.b.id))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    env.people.soft_delete(env.ws, env.a.id)

    with pytest.raises(NotFoundError):
        env.timeline.person_timeline(env.ws, env.a.id)
    archived = env.timeline.person_timeline(env.ws, env.a.id, include_deleted=True)
    assert {item.entity_id for item in archived} == {event.id, initial.id}
    event_item = next(item for item in archived if item.kind == EVENT_KIND)
    assert set(event_item.person_ids) == {env.a.id, env.b.id}
    assert "Ada" in event_item.summary and "Bo" in event_item.summary

    # Archiving an endpoint person is a Person lifecycle fact: the Relationship is
    # not deleted and its recorded history stays in the workspace timeline.
    assert env.relationships.get(env.ws, relationship.id, include_deleted=True).deleted_at is None
    expected = {event.id, initial.id}
    assert {item.entity_id for item in env.timeline.workspace_timeline(env.ws)} == expected
    assert {item.entity_id for item in env.timeline.person_timeline(env.ws, env.b.id)} == expected


def test_workspace_isolation_and_invalid_arguments(env):
    event = make_event(env, title="本工作区事件", participants=(env.a.id,))
    foreign_event = env.events.create_event(env.foreign, "外部事件", "Meeting", occurred_on=DAY)

    assert ids(env.timeline.workspace_timeline(env.foreign)) == [foreign_event.id]
    assert event.id not in ids(env.timeline.workspace_timeline(env.foreign))

    for call in (lambda: env.timeline.workspace_timeline(uuid4()),
                 lambda: env.timeline.person_timeline(env.ws, env.c.id),
                 lambda: env.timeline.person_timeline(env.ws, uuid4()),
                 lambda: env.timeline.person_timeline(env.foreign, env.a.id),
                 lambda: env.timeline.person_timeline(uuid4(), env.a.id),
                 lambda: env.timeline.person_timeline(uuid4(), env.a.id, include_deleted=True)):
        with pytest.raises(NotFoundError):
            call()
    with pytest.raises(DomainError):
        env.timeline.person_timeline("not-a-uuid", env.a.id)
    with pytest.raises(DomainError):
        env.timeline.workspace_timeline(env.ws, limit=-1)
    with pytest.raises(DomainError):
        env.timeline.person_timeline(env.ws, env.a.id, limit="2")


def test_limit_applies_after_the_merged_sort(env):
    events = [make_event(env, title=f"事件{index}", occurred_on=None,
                         started_at=NOW - timedelta(days=index + 1), participants=(env.a.id,))
              for index in range(3)]
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    ended = make_history(env, relationship, "ENDED", effective_at=NOW + timedelta(days=1))

    full = env.timeline.workspace_timeline(env.ws)
    assert ids(full) == [ended.id, initial.id, events[0].id, events[1].id, events[2].id]
    assert env.timeline.workspace_timeline(env.ws, limit=None) == full
    assert env.timeline.workspace_timeline(env.ws, limit=len(full)) == full
    assert env.timeline.workspace_timeline(env.ws, limit=2) == full[:2]
    assert env.timeline.workspace_timeline(env.ws, limit=0) == []
    assert env.timeline.person_timeline(env.ws, env.a.id, limit=3) == full[:3]
    assert env.timeline.person_timeline(env.ws, env.a.id, limit=0) == []


def test_titles_and_summaries_are_readable(env):
    event = make_event(env, title="共同活动", event_type="Meeting", location="茶馆",
                       description="  第一行\n第二行  ", participants=(env.a.id, env.b.id))
    relationship = make_relationship(env, "Friend")
    items = env.timeline.workspace_timeline(env.ws)

    event_item = next(item for item in items if item.kind == EVENT_KIND)
    assert event_item.entity_id == event.id
    assert event_item.title == "共同活动"
    assert "Meeting" in event_item.summary and "FACT" in event_item.summary
    assert "地点：茶馆" in event_item.summary
    assert "参与者：" in event_item.summary
    assert "Ada" in event_item.summary and "Bo" in event_item.summary
    assert "第一行 第二行" in event_item.summary

    change_item = next(item for item in items if item.kind == RELATIONSHIP_CHANGE_KIND)
    assert change_item.relationship_id == relationship.id
    assert change_item.title == "Friend 关系状态变更"
    assert "关系类型：Friend" in change_item.summary
    assert "状态：ACTIVE" in change_item.summary
    assert "Ada" in change_item.summary and "Bo" in change_item.summary


def test_history_event_link_is_exposed_on_the_change_item(env):
    event = make_event(env, title="状态变更事件", participants=(env.a.id,))
    relationship = make_relationship(env, "Friend")
    initial = env.relationships.history(env.ws, relationship.id)[0]
    linked = env.events.attach_event_to_history(env.ws, initial.id, event.id)

    change = next(item for item in env.timeline.workspace_timeline(env.ws)
                  if item.kind == RELATIONSHIP_CHANGE_KIND)
    assert change.entity_id == initial.id and change.relationship_id == relationship.id
    assert change.event_id == event.id
    assert change.timestamp == linked.effective_at == initial.effective_at


def test_items_expose_domain_and_stdlib_values_only(env):
    make_event(env, participants=(env.a.id,))
    make_relationship(env, "Friend")
    make_history(env, make_relationship(env, "Colleague"), "PAUSED")
    for item in env.timeline.workspace_timeline(env.ws, include_deleted=True):
        assert list(item.__dataclass_fields__) == ["timestamp", "kind", "title", "summary",
                                                   "entity_id", "person_ids", "relationship_id", "event_id"]
        assert isinstance(item.timestamp, datetime) and item.timestamp.utcoffset() == timedelta(0)
        assert item.kind in (EVENT_KIND, RELATIONSHIP_CHANGE_KIND)
        assert isinstance(item.entity_id, UUID) and isinstance(item.person_ids, tuple)
        assert isinstance(item.title, str) and isinstance(item.summary, str)
        assert item.relationship_id is None or isinstance(item.relationship_id, UUID)
        assert item.event_id is None or isinstance(item.event_id, UUID)


def test_query_count_is_constant_and_never_per_row(env):
    make_event(env, participants=(env.a.id, env.b.id))
    make_relationship(env, "Friend")
    with count_selects(env) as sparse_person:
        env.timeline.person_timeline(env.ws, env.a.id)
    with count_selects(env) as sparse_workspace:
        env.timeline.workspace_timeline(env.ws)

    for index in range(6):
        make_event(env, title=f"事件{index}", occurred_on=None, started_at=NOW - timedelta(days=index),
                   participants=(env.a.id, env.b.id))
        relationship = make_relationship(env, f"Kind{index}")
        make_history(env, relationship, "PAUSED")
        make_history(env, relationship, "ENDED")
    with count_selects(env) as dense_person:
        env.timeline.person_timeline(env.ws, env.a.id)
    with count_selects(env) as dense_workspace:
        env.timeline.workspace_timeline(env.ws)

    assert len(dense_person) == len(sparse_person) <= 8
    assert len(dense_workspace) == len(sparse_workspace) <= 8


def test_timeline_projection_writes_nothing(env):
    event = make_event(env, title="有证据的事件", participants=(env.a.id, env.b.id))
    relationship = make_relationship(env, "Friend")
    make_history(env, relationship, "PAUSED")
    make_evidence(env, event)

    before = database_state(env)
    env.timeline.workspace_timeline(env.ws)
    env.timeline.workspace_timeline(env.ws, include_deleted=True, limit=2)
    env.timeline.person_timeline(env.ws, env.a.id, include_deleted=True)
    env.timeline.person_timeline(env.ws, env.b.id, limit=1)
    assert database_state(env) == before
