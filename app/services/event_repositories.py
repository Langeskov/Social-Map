"""Event Service's explicit adapters; no changes to the earlier composition contracts."""
from dataclasses import dataclass
from app.repositories.interfaces import WorkspaceRepository, PersonRepository
from app.repositories.event_interfaces import EventRepository, EventParticipantRepository
from app.repositories.relationship_interfaces import RelationshipRepository, RelationshipHistoryRepository


@dataclass(frozen=True)
class EventRepositories:
    workspaces: WorkspaceRepository
    people: PersonRepository
    events: EventRepository
    participants: EventParticipantRepository
    relationships: RelationshipRepository
    histories: RelationshipHistoryRepository


def sql_event_repositories(session):
    from app.storage.repositories import SQLWorkspaceRepository, SQLPersonRepository
    from app.storage.event_repositories import SQLEventRepository, SQLEventParticipantRepository
    from app.storage.relationship_repositories import SQLRelationshipRepository, SQLRelationshipHistoryRepository
    return EventRepositories(SQLWorkspaceRepository(session), SQLPersonRepository(session),
        SQLEventRepository(session), SQLEventParticipantRepository(session),
        SQLRelationshipRepository(session), SQLRelationshipHistoryRepository(session))
