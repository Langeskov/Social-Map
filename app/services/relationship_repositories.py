"""Relationship Service wiring reuses Phase 3 contracts without changing media wiring."""
from dataclasses import dataclass
from app.repositories.interfaces import WorkspaceRepository, PersonRepository
from app.repositories.relationship_interfaces import RelationshipRepository, RelationshipHistoryRepository

@dataclass(frozen=True)
class RelationshipRepositories:
    workspaces: WorkspaceRepository
    people: PersonRepository
    relationships: RelationshipRepository
    histories: RelationshipHistoryRepository


def sql_relationship_repositories(session) -> RelationshipRepositories:
    from app.storage.repositories import SQLWorkspaceRepository, SQLPersonRepository
    from app.storage.relationship_repositories import SQLRelationshipRepository, SQLRelationshipHistoryRepository
    return RelationshipRepositories(SQLWorkspaceRepository(session), SQLPersonRepository(session),
                                    SQLRelationshipRepository(session), SQLRelationshipHistoryRepository(session))
