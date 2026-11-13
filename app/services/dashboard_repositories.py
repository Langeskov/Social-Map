"""Dashboard Service wiring plus the bounded reads and COUNT aggregates it needs.

Style follows app/services/event_repositories.py: one frozen dataclass of Domain
contracts and one ``sql_*`` factory bound to the caller-owned Session, while the
service keeps transaction ownership.

Phase 7 freezes app/storage/**, so the Dashboard's own read shapes live here:
every recent list is exactly one query with LIMIT and every statistic is one
COUNT (two windows per statistic), which is what keeps the snapshot free of N+1
queries. The recent-Event ordering mirrors the Event storage adapter: a DATE
Event sorts by that day's 00:00 UTC without rewriting ``occurred_on``, and both
dialects need their own form of that instant.
"""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import DateTime, String, and_, cast, func, select

from app.domain.entities import Event, Evidence, Person, Relationship, RelationshipHistory
from app.repositories.interfaces import WorkspaceRepository
from app.repositories.relationship_interfaces import RelationshipHistoryRepository, RelationshipRepository

# The Event instant is stored as "%Y-%m-%d %H:%M:%S.%f" text in SQLite, so the
# window boundary must be bound in exactly that form to compare as text.
_SQLITE_INSTANT = "%Y-%m-%d %H:%M:%S.%f"


def _snapshot(kind, model):
    """Copy scalar model columns into a detached Domain value (no ORM leakage)."""
    return None if model is None else kind(**{field.name: getattr(model, field.name) for field in fields(kind)})


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_SQLITE_INSTANT)


def _instant(session, started_at, occurred_on):
    """The occurrence ordering key shared with the Events page (never rewriting DATE)."""
    if session.get_bind().dialect.name == "sqlite":
        # SQLite CAST(date AS DATETIME) produces an integer year, not a timestamp.
        return func.coalesce(cast(started_at, String),
                             cast(occurred_on, String) + " 00:00:00.000000")
    return func.coalesce(started_at, func.timezone("UTC", cast(occurred_on, DateTime(timezone=False))))


def _within(session, instant, start: datetime, end: datetime):
    if session.get_bind().dialect.name == "sqlite":
        return and_(instant >= _stamp(start), instant <= _stamp(end))
    return and_(instant >= start, instant <= end)


class DashboardPersonRepository(Protocol):
    def count(self, workspace_id: UUID, *, include_deleted: bool = False) -> int: ...
    def list_recent(self, workspace_id: UUID, limit: int, *, include_deleted: bool = False) -> list[Person]: ...


class DashboardEventRepository(Protocol):
    def count_occurred(self, workspace_id: UUID, start: datetime, end: datetime, *,
                       include_deleted: bool = False) -> int: ...
    def list_recent(self, workspace_id: UUID, limit: int, *, include_deleted: bool = False) -> list[Event]: ...


class DashboardEvidenceRepository(Protocol):
    def count_created(self, workspace_id: UUID, start: datetime, end: datetime, *,
                      include_deleted: bool = False) -> int: ...
    def list_recent(self, workspace_id: UUID, limit: int, *, include_deleted: bool = False) -> list[Evidence]: ...


class DashboardChangeRepository(Protocol):
    def list_recent(self, workspace_id: UUID, limit: int, *, include_deleted: bool = False
                    ) -> list[tuple[RelationshipHistory, Relationship]]: ...


class SQLDashboardPersonRepository:
    """Newest people first, plus the workspace people count."""

    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted):
        from app.storage.models import PersonModel
        query = select(PersonModel).where(PersonModel.workspace_id == workspace_id)
        return query if include_deleted else query.where(PersonModel.deleted_at.is_(None))

    def count(self, workspace_id, *, include_deleted=False):
        from app.storage.models import PersonModel
        query = select(func.count()).select_from(PersonModel).where(PersonModel.workspace_id == workspace_id)
        if not include_deleted:
            query = query.where(PersonModel.deleted_at.is_(None))
        return int(self.session.scalar(query) or 0)

    def list_recent(self, workspace_id, limit, *, include_deleted=False):
        from app.storage.models import PersonModel
        query = self._query(workspace_id, include_deleted).order_by(
            PersonModel.created_at.desc(), PersonModel.id.desc()).limit(limit)
        return [_snapshot(Person, row) for row in self.session.scalars(query)]


class SQLDashboardEventRepository:
    """Newest occurrences first, plus the 7/30 day occurrence counts."""

    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted):
        from app.storage.models import EventModel
        query = select(EventModel).where(EventModel.workspace_id == workspace_id)
        return query if include_deleted else query.where(EventModel.deleted_at.is_(None))

    def count_occurred(self, workspace_id, start, end, *, include_deleted=False):
        from app.storage.models import EventModel
        instant = _instant(self.session, EventModel.started_at, EventModel.occurred_on)
        query = select(func.count()).select_from(EventModel).where(
            EventModel.workspace_id == workspace_id, _within(self.session, instant, start, end))
        if not include_deleted:
            query = query.where(EventModel.deleted_at.is_(None))
        return int(self.session.scalar(query) or 0)

    def list_recent(self, workspace_id, limit, *, include_deleted=False):
        from app.storage.models import EventModel
        instant = _instant(self.session, EventModel.started_at, EventModel.occurred_on)
        query = self._query(workspace_id, include_deleted).order_by(instant.desc(), EventModel.id.desc()).limit(limit)
        return [_snapshot(Event, row) for row in self.session.scalars(query)]


class SQLDashboardEvidenceRepository:
    """Newest evidence first, plus the 7/30 day capture counts."""

    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted):
        from app.storage.models import EvidenceModel
        query = select(EvidenceModel).where(EvidenceModel.workspace_id == workspace_id)
        return query if include_deleted else query.where(EvidenceModel.deleted_at.is_(None))

    def count_created(self, workspace_id, start, end, *, include_deleted=False):
        from app.storage.models import EvidenceModel
        query = select(func.count()).select_from(EvidenceModel).where(
            EvidenceModel.workspace_id == workspace_id,
            EvidenceModel.created_at >= start, EvidenceModel.created_at <= end)
        if not include_deleted:
            query = query.where(EvidenceModel.deleted_at.is_(None))
        return int(self.session.scalar(query) or 0)

    def list_recent(self, workspace_id, limit, *, include_deleted=False):
        from app.storage.models import EvidenceModel
        query = self._query(workspace_id, include_deleted).order_by(
            EvidenceModel.created_at.desc(), EvidenceModel.id.desc()).limit(limit)
        return [_snapshot(Evidence, row) for row in self.session.scalars(query)]


class SQLDashboardChangeRepository:
    """Newest relationship state changes with their Relationship in one query."""

    def __init__(self, session):
        self.session = session

    def list_recent(self, workspace_id, limit, *, include_deleted=False):
        from app.storage.models import RelationshipHistoryModel, RelationshipModel
        query = select(RelationshipHistoryModel, RelationshipModel).join(RelationshipModel, and_(
            RelationshipModel.id == RelationshipHistoryModel.relationship_id,
            RelationshipModel.workspace_id == RelationshipHistoryModel.workspace_id)).where(
            RelationshipHistoryModel.workspace_id == workspace_id)
        if not include_deleted:
            # Only the Relationship's own lifecycle hides a change; an ENDED state
            # is a recorded change and stays visible.
            query = query.where(RelationshipModel.deleted_at.is_(None))
        query = query.order_by(RelationshipHistoryModel.effective_at.desc(),
                               RelationshipHistoryModel.created_at.desc(),
                               RelationshipHistoryModel.id.desc()).limit(limit)
        return [(_snapshot(RelationshipHistory, history), _snapshot(Relationship, relationship))
                for history, relationship in self.session.execute(query)]


@dataclass(frozen=True)
class DashboardRepositories:
    workspaces: WorkspaceRepository
    people: DashboardPersonRepository
    events: DashboardEventRepository
    evidence: DashboardEvidenceRepository
    relationships: RelationshipRepository
    histories: RelationshipHistoryRepository
    changes: DashboardChangeRepository


def sql_dashboard_repositories(session) -> DashboardRepositories:
    from app.storage.relationship_repositories import SQLRelationshipHistoryRepository, SQLRelationshipRepository
    from app.storage.repositories import SQLWorkspaceRepository
    return DashboardRepositories(SQLWorkspaceRepository(session), SQLDashboardPersonRepository(session),
        SQLDashboardEventRepository(session), SQLDashboardEvidenceRepository(session),
        SQLRelationshipRepository(session), SQLRelationshipHistoryRepository(session),
        SQLDashboardChangeRepository(session))
