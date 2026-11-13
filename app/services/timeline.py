"""Read-only Timeline projection over Events and RelationshipHistory.

The Timeline is a projection, never a fact: this module writes nothing, creates
no table, caches no statistic, and every call runs in one read transaction that
returns detached Domain values. Evidence is deliberately absent: evidence
belongs to the Event detail, not to the timeline.

Frozen ordering rule
    ``timestamp DESC``, then ``kind`` (EVENT before RELATIONSHIP_CHANGE at the
    same instant), then ``entity_id`` as a string descending. The last key makes
    every result reproducible, like the ``id DESC`` tie-break of the Event and
    History adapters.
    - EVENT sorts by ``started_at``, or by that DATE's 00:00 UTC. The Event's own
      ``occurred_on`` semantics are never rewritten.
    - RELATIONSHIP_CHANGE sorts by ``effective_at``.
    Future timestamps are never filtered out: a planned Event or a planned state
    change is part of the timeline exactly like a backdated one.

Frozen visibility rule
    Only a row's own lifecycle hides it. Events and Relationships follow their
    own ``deleted_at`` (``include_deleted=True`` widens the projection).
    RelationshipHistory rows have no ``deleted_at`` and an ENDED state is not a
    deletion, so recorded history always stays visible; archiving an endpoint
    person never erases the history of a relationship.

Cost
    Each public method issues a fixed number of queries and never queries once
    per row: the person's Events, every History row, every Relationship row, one
    batched EventParticipant read and one Person read for display names.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from uuid import UUID

from app.domain.entities import Event, Relationship, RelationshipHistory
from app.domain.errors import NotFoundError
from app.domain.validation import utc_now, validate_nonnegative_int, validate_uuid
from app.services.rules import workspace_required
from app.services.timeline_repositories import sql_timeline_repositories
from app.services.unit_of_work import transaction

EVENT_KIND = "EVENT"
RELATIONSHIP_CHANGE_KIND = "RELATIONSHIP_CHANGE"

# One instant, two kinds: the rank is read with reverse=True, so the larger value
# is listed first and EVENT precedes RELATIONSHIP_CHANGE.
_KIND_RANK = {RELATIONSHIP_CHANGE_KIND: 0, EVENT_KIND: 1}


@dataclass(frozen=True)
class TimelineItem:
    timestamp: datetime
    kind: str
    title: str
    summary: str
    entity_id: UUID
    person_ids: tuple[UUID, ...]
    relationship_id: UUID | None
    event_id: UUID | None


def event_instant(event: Event) -> datetime:
    """The Timeline instant of an Event: ``started_at``, else that DATE's 00:00 UTC.

    A DATE Event is a day, not an instant. The day is projected onto the UTC
    timeline for ordering only; the Event keeps ``occurred_on`` unchanged.
    """
    if event.started_at is not None:
        return event.started_at
    return datetime.combine(event.occurred_on, time.min, tzinfo=timezone.utc)


def _collapse(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _event_summary(event: Event, person_ids: tuple[UUID, ...], names: dict[UUID, str]) -> str:
    parts = [event.event_type, event.record_kind]
    if event.location:
        parts.append(f"地点：{event.location}")
    labels = [names[person_id] for person_id in person_ids if person_id in names]
    if labels:
        parts.append("参与者：" + "、".join(labels))
    description = _collapse(event.description)
    if description:
        parts.append(description)
    return " · ".join(parts)


def _history_summary(history: RelationshipHistory, relationship: Relationship,
                     names: dict[UUID, str]) -> str:
    parts = []
    endpoints = [names.get(relationship.person_a_id), names.get(relationship.person_b_id)]
    if all(endpoints):
        parts.append("、".join(endpoints))
    parts.append(f"关系类型：{relationship.relationship_type}")
    parts.append(f"状态：{history.state}")
    note = _collapse(history.note)
    if note:
        parts.append(note)
    return " · ".join(parts)


def _sort_key(item: TimelineItem):
    # reverse=True reverses every field, so the rank above puts EVENT first and
    # the id breaks remaining ties exactly like the adapters (id DESC).
    return (item.timestamp, _KIND_RANK[item.kind], str(item.entity_id))


class TimelineService:
    def __init__(self, sessions, repositories=sql_timeline_repositories, *, clock=utc_now):
        # clock is accepted for interface symmetry with the other services. The
        # Timeline never filters on it: future timestamps are always displayed.
        self._sessions, self._repositories, self._clock = sessions, repositories, clock

    @staticmethod
    def _limit(value):
        if value is not None:
            validate_nonnegative_int(value, "limit")
        return value

    @staticmethod
    def _relationships(repositories, workspace_id, include_deleted):
        """Relationships visible to the projection.

        The query deliberately reads the unfiltered list and applies the frozen
        visibility rule here: only the Relationship's own ``deleted_at`` hides a
        history row, not the archived state of one of its endpoint persons.
        """
        rows = repositories.relationships.list_by_workspace(workspace_id, include_deleted=True)
        return {entity.id: entity for entity in rows if include_deleted or entity.deleted_at is None}

    def _project(self, repositories, workspace_id, events, histories, relationships, limit):
        # One batched participant read and one Person read cover every row below.
        participants = repositories.participants.list_by_events(workspace_id, [event.id for event in events])
        names = {person.id: person.display_name
                 for person in repositories.people.list(workspace_id, include_deleted=True)}
        by_event: dict[UUID, list[UUID]] = {}
        for entry in participants:
            by_event.setdefault(entry.event_id, []).append(entry.person_id)
        items = []
        for event in events:
            person_ids = tuple(by_event.get(event.id, ()))
            items.append(TimelineItem(timestamp=event_instant(event), kind=EVENT_KIND, title=event.title,
                                      summary=_event_summary(event, person_ids, names),
                                      entity_id=event.id, person_ids=person_ids,
                                      relationship_id=None, event_id=event.id))
        for history in histories:
            relationship = relationships[history.relationship_id]
            items.append(TimelineItem(timestamp=history.effective_at, kind=RELATIONSHIP_CHANGE_KIND,
                                      title=f"{relationship.relationship_type} 关系状态变更",
                                      summary=_history_summary(history, relationship, names),
                                      entity_id=history.id,
                                      person_ids=(relationship.person_a_id, relationship.person_b_id),
                                      relationship_id=relationship.id, event_id=history.event_id))
        items.sort(key=_sort_key, reverse=True)
        return items if limit is None else items[:limit]

    def person_timeline(self, workspace_id, person_id, *, include_deleted=False, limit=None) -> list[TimelineItem]:
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(person_id, "person_id")
        self._limit(limit)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            person = repositories.people.get(workspace_id, person_id, include_deleted=True)
            if person is None or (person.deleted_at is not None and not include_deleted):
                raise NotFoundError("人物不属于当前 Workspace、已归档或不存在。")
            events = repositories.events.list_by_person(workspace_id, person_id, include_deleted=include_deleted)
            relationships = self._relationships(repositories, workspace_id, include_deleted)
            related = {key for key, entity in relationships.items()
                       if person_id in (entity.person_a_id, entity.person_b_id)}
            histories = [row for row in repositories.histories.list_by_workspace(workspace_id)
                         if row.relationship_id in related]
            return self._project(repositories, workspace_id, events, histories, relationships, limit)

    def workspace_timeline(self, workspace_id, *, include_deleted=False, limit=None) -> list[TimelineItem]:
        validate_uuid(workspace_id, "workspace_id")
        self._limit(limit)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            events = repositories.events.list_by_workspace(workspace_id, include_deleted=include_deleted)
            relationships = self._relationships(repositories, workspace_id, include_deleted)
            histories = [row for row in repositories.histories.list_by_workspace(workspace_id)
                         if row.relationship_id in relationships]
            return self._project(repositories, workspace_id, events, histories, relationships, limit)
