"""Analysis service wiring: fact repositories (read-only) plus analysis storage."""
from dataclasses import dataclass
from app.repositories.analysis_interfaces import AnalysisRunRepository, AnalysisSourceRepository
from app.repositories.event_interfaces import EventParticipantRepository, EventRepository
from app.repositories.evidence_interfaces import EvidenceMediaRepository, EvidenceRepository
from app.repositories.interfaces import (MediaRepository, PersonAliasRepository, PersonRepository,
                                         WorkspaceRepository)
from app.repositories.relationship_interfaces import (RelationshipHistoryRepository,
                                                      RelationshipRepository)
from app.repositories.memory_interfaces import MemoryRepository


@dataclass(frozen=True)
class AnalysisRepositories:
    workspaces: WorkspaceRepository
    people: PersonRepository
    aliases: PersonAliasRepository
    relationships: RelationshipRepository
    histories: RelationshipHistoryRepository
    events: EventRepository
    participants: EventParticipantRepository
    evidence: EvidenceRepository
    evidence_media: EvidenceMediaRepository
    media: MediaRepository
    runs: AnalysisRunRepository
    sources: AnalysisSourceRepository
    memories: MemoryRepository


def sql_analysis_repositories(session) -> AnalysisRepositories:
    from app.storage.analysis_repositories import (SQLAnalysisRunRepository,
                                                   SQLAnalysisSourceRepository)
    from app.storage.event_repositories import SQLEventParticipantRepository, SQLEventRepository
    from app.storage.evidence_repositories import (SQLEvidenceMediaRepository,
                                                   SQLEvidenceRepository)
    from app.storage.memory_repositories import SQLMemoryRepository
    from app.storage.repositories import (SQLMediaRepository, SQLPersonAliasRepository,
                                          SQLPersonRepository, SQLWorkspaceRepository)
    from app.storage.relationship_repositories import (SQLRelationshipHistoryRepository,
                                                       SQLRelationshipRepository)
    return AnalysisRepositories(SQLWorkspaceRepository(session), SQLPersonRepository(session),
                                SQLPersonAliasRepository(session), SQLRelationshipRepository(session),
                                SQLRelationshipHistoryRepository(session), SQLEventRepository(session),
                                SQLEventParticipantRepository(session), SQLEvidenceRepository(session),
                                SQLEvidenceMediaRepository(session), SQLMediaRepository(session),
                                SQLAnalysisRunRepository(session), SQLAnalysisSourceRepository(session),
                                SQLMemoryRepository(session))
