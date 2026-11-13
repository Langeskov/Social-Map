"""Timeline Service wiring plus the one batched read the projection needs.

Style follows app/services/event_repositories.py: one frozen dataclass of Domain
contracts and one ``sql_*`` factory that binds it to the caller-owned Session.
Transactions stay owned by the service.

Phase 7 freezes app/storage/**, so the read that must cover many Events at once
(the participants of a whole timeline page) lives here instead. It is a plain
read-only select built with the conventions of app/storage/*_repositories.py and
it is never called once per timeline row.
"""
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Protocol
from uuid import UUID

from app.domain.entities import EventParticipant
from app.repositories.event_interfaces import EventRepository
from app.repositories.interfaces import PersonRepository, WorkspaceRepository
from app.repositories.relationship_interfaces import RelationshipHistoryRepository, RelationshipRepository


def _snapshot(kind, model):
    """Copy scalar model columns into a detached Domain value (no ORM leakage)."""
    return None if model is None else kind(**{field.name: getattr(model, field.name) for field in fields(kind)})


class TimelineParticipantRepository(Protocol):
    """The Event contract plus the batched read a Timeline page requires."""

    def list_by_events(self, workspace_id: UUID, event_ids: Sequence[UUID]) -> list[EventParticipant]: ...


class SQLTimelineParticipantRepository:
    """Every EventParticipant row of many Events in exactly one query."""

    def __init__(self, session):
        self.session = session

    def list_by_events(self, workspace_id, event_ids):
        from sqlalchemy import select

        from app.storage.models import EventParticipantModel
        unique = tuple(dict.fromkeys(event_ids))
        if not unique:
            return []
        query = select(EventParticipantModel).where(
            EventParticipantModel.workspace_id == workspace_id,
            EventParticipantModel.event_id.in_(unique)).order_by(
            EventParticipantModel.created_at, EventParticipantModel.id)
        return [_snapshot(EventParticipant, row) for row in self.session.scalars(query)]


@dataclass(frozen=True)
class TimelineRepositories:
    workspaces: WorkspaceRepository
    people: PersonRepository
    events: EventRepository
    participants: TimelineParticipantRepository
    relationships: RelationshipRepository
    histories: RelationshipHistoryRepository


def sql_timeline_repositories(session) -> TimelineRepositories:
    from app.storage.event_repositories import SQLEventRepository
    from app.storage.relationship_repositories import SQLRelationshipHistoryRepository, SQLRelationshipRepository
    from app.storage.repositories import SQLPersonRepository, SQLWorkspaceRepository
    return TimelineRepositories(SQLWorkspaceRepository(session), SQLPersonRepository(session),
        SQLEventRepository(session), SQLTimelineParticipantRepository(session),
        SQLRelationshipRepository(session), SQLRelationshipHistoryRepository(session))
