"""Read-only Dashboard projection: bounded recent lists plus COUNT statistics.

The Dashboard answers "what happened recently?" for one Workspace. It is a
projection, never a fact: nothing is written, no table is created, no statistic
is cached, and every call reads the current rows in one transaction and returns
detached Domain values.

Frozen semantics
    - Workspace isolation first: a missing or archived Workspace raises
      NotFoundError before any row is read.
    - ``include_deleted=True`` widens every list, every count and the
      ``active_relationships`` statistic to soft-deleted rows.
    - ``active_relationships`` counts Relationships whose *current* state is
      ACTIVE, exactly like ``RelationshipService.current``: the newest history
      row with ``effective_at <= clock()`` decides. PAUSED, ENDED, a planned
      future state and a state-less Relationship are never counted as active.
    - ``events_7d`` / ``events_30d`` count Events whose timeline instant (the
      same rule the Timeline uses: ``started_at``, else that DATE's 00:00 UTC)
      falls inside the window ending at ``clock()``.
    - ``evidence_7d`` / ``evidence_30d`` count Evidence recorded inside that
      window (``created_at``).

Cost
    Each list is one LIMIT query and each statistic is one COUNT query (two
    windows per query), so a snapshot issues a fixed number of queries and never
    queries once per row.
"""
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.domain.entities import Event, Evidence, Person, Relationship, RelationshipHistory
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.services.dashboard_repositories import sql_dashboard_repositories
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

RECENT_LIMIT = 5
ACTIVE_STATE = "ACTIVE"


@dataclass(frozen=True)
class DashboardCounts:
    people: int
    active_relationships: int
    events_7d: int
    events_30d: int
    evidence_7d: int
    evidence_30d: int


@dataclass(frozen=True)
class RelationshipChange:
    history: RelationshipHistory
    relationship: Relationship


@dataclass(frozen=True)
class DashboardSnapshot:
    workspace_id: UUID
    counts: DashboardCounts
    recent_people: tuple[Person, ...]
    recent_events: tuple[Event, ...]
    recent_relationship_changes: tuple[RelationshipChange, ...]
    recent_evidence: tuple[Evidence, ...]


class DashboardService:
    def __init__(self, sessions, repositories=sql_dashboard_repositories, *, clock=utc_now):
        self._sessions, self._repositories, self._clock = sessions, repositories, clock

    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    @staticmethod
    def _active_relationships(repositories, workspace_id, include_deleted, as_of):
        """Count Relationships whose current state is ACTIVE (one query per table).

        "Current" is the newest history row at or before ``as_of`` in the total
        order (effective_at, created_at, id) — the same order as
        RelationshipHistoryRepository.get_latest, so a PAUSED/ENDED row, a
        state-less Relationship or a merely planned future row never counts.
        """
        visible = [entity for entity in repositories.relationships.list_by_workspace(
            workspace_id, include_deleted=True) if include_deleted or entity.deleted_at is None]
        # `histories.list_by_workspace` is the append-only read: one query for all
        # rows, including the history of ENDED and of soft-deleted Relationships.
        newest: dict[UUID, tuple] = {}
        states: dict[UUID, str] = {}
        for history in repositories.histories.list_by_workspace(workspace_id):
            if history.effective_at > as_of:
                continue  # A planned state is not the current state yet.
            order = (history.effective_at, history.created_at, history.id)
            if order > newest.get(history.relationship_id, ()):
                newest[history.relationship_id] = order
                states[history.relationship_id] = history.state
        return sum(1 for entity in visible if states.get(entity.id) == ACTIVE_STATE)

    def _counts(self, repositories, workspace_id, include_deleted, now) -> DashboardCounts:
        seven, thirty = (now - timedelta(days=days) for days in (7, 30))
        return DashboardCounts(
            people=repositories.people.count(workspace_id, include_deleted=include_deleted),
            active_relationships=self._active_relationships(repositories, workspace_id, include_deleted, now),
            events_7d=repositories.events.count_occurred(workspace_id, seven, now,
                                                         include_deleted=include_deleted),
            events_30d=repositories.events.count_occurred(workspace_id, thirty, now,
                                                          include_deleted=include_deleted),
            evidence_7d=repositories.evidence.count_created(workspace_id, seven, now,
                                                            include_deleted=include_deleted),
            evidence_30d=repositories.evidence.count_created(workspace_id, thirty, now,
                                                             include_deleted=include_deleted))

    def counts(self, workspace_id, *, include_deleted=False) -> DashboardCounts:
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return self._counts(repositories, workspace_id, include_deleted, self._now())

    def snapshot(self, workspace_id, *, include_deleted=False) -> DashboardSnapshot:
        validate_uuid(workspace_id, "workspace_id")
        now = self._now()
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            counts = self._counts(repositories, workspace_id, include_deleted, now)
            people = repositories.people.list_recent(workspace_id, RECENT_LIMIT, include_deleted=include_deleted)
            events = repositories.events.list_recent(workspace_id, RECENT_LIMIT, include_deleted=include_deleted)
            changes = repositories.changes.list_recent(workspace_id, RECENT_LIMIT, include_deleted=include_deleted)
            evidence = repositories.evidence.list_recent(workspace_id, RECENT_LIMIT, include_deleted=include_deleted)
            return DashboardSnapshot(workspace_id=workspace_id, counts=counts,
                                     recent_people=tuple(people), recent_events=tuple(events),
                                     recent_relationship_changes=tuple(
                                         RelationshipChange(history, relationship)
                                         for history, relationship in changes),
                                     recent_evidence=tuple(evidence))
