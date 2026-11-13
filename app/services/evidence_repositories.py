"""Evidence Service wiring: Phase 5 event contracts plus Phase 3 media contracts."""
from dataclasses import dataclass
from app.repositories.interfaces import MediaRepository, WorkspaceRepository
from app.repositories.event_interfaces import EventRepository
from app.repositories.evidence_interfaces import EvidenceRepository, EvidenceMediaRepository


@dataclass(frozen=True)
class EvidenceRepositories:
    workspaces: WorkspaceRepository
    events: EventRepository
    media: MediaRepository
    evidence: EvidenceRepository
    evidence_media: EvidenceMediaRepository


def sql_evidence_repositories(session):
    from app.storage.repositories import SQLWorkspaceRepository, SQLMediaRepository
    from app.storage.event_repositories import SQLEventRepository
    from app.storage.evidence_repositories import SQLEvidenceRepository, SQLEvidenceMediaRepository
    return EvidenceRepositories(SQLWorkspaceRepository(session), SQLEventRepository(session),
                                SQLMediaRepository(session), SQLEvidenceRepository(session),
                                SQLEvidenceMediaRepository(session))
