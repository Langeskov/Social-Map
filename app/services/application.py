"""Composition root: only services and immutable Domain values reach Qt."""
from dataclasses import dataclass
from app.domain.entities import Workspace
from app.services.workspace import WorkspaceService
from app.services.person import PersonService
from app.services.media import MediaService
from app.services.relationship import RelationshipService
from app.services.event import EventService
from app.services.evidence import EvidenceService
from app.services.search import SearchService
from app.services.timeline import TimelineService
from app.services.dashboard import DashboardService
from app.services.analysis import AnalysisService
from app.services.memory import MemoryService
from app.services.backup import BackupService, BackupVerificationService
from app.services.change_capture import ChangeLogWriter
from app.services.change_history import ChangeHistoryService
from app.services.export import WorkspaceExportService
from app.services.integrity import DataIntegrityService
from app.services.llm import LLMConfigStore, LLMGateway
from app.config.settings import AppConfig
from app.storage.media_store import MediaStore

@dataclass(frozen=True)
class ApplicationServices:
    workspaces: WorkspaceService
    people: PersonService
    media: MediaService
    initial_workspace: Workspace
    relationships: RelationshipService | None = None
    events: EventService | None = None
    evidence: EvidenceService | None = None
    search: SearchService | None = None
    timeline: TimelineService | None = None
    dashboard: DashboardService | None = None
    llm: LLMGateway | None = None
    analysis: AnalysisService | None = None
    memories: MemoryService | None = None
    change_history: ChangeHistoryService | None = None
    integrity: DataIntegrityService | None = None
    backups: BackupService | None = None
    backup_verification: BackupVerificationService | None = None
    exports: WorkspaceExportService | None = None


def create_application_services(config: AppConfig, sessions, *, llm_gateway=None) -> ApplicationServices:
    # Change capture is composed here, once: every business service shares one
    # writer, so the application always records audited writes in the same
    # transaction as the business rows.
    changes = ChangeLogWriter()
    workspaces = WorkspaceService(sessions, changes=changes)
    store = MediaStore(config.storage_paths.data_dir)
    media = MediaService(sessions, store, changes=changes)
    people = PersonService(sessions, media, changes=changes)
    gateway = llm_gateway or LLMGateway(LLMConfigStore.for_data_dir(config.storage_paths.data_dir))
    return ApplicationServices(workspaces=workspaces, people=people, media=media,
        initial_workspace=workspaces.ensure_default(), relationships=RelationshipService(sessions, changes=changes),
        events=EventService(sessions, changes=changes), evidence=EvidenceService(sessions, changes=changes),
        search=SearchService(sessions),
        timeline=TimelineService(sessions), dashboard=DashboardService(sessions),
        llm=gateway, analysis=AnalysisService(sessions, gateway),
        memories=MemoryService(sessions, changes=changes),
        change_history=ChangeHistoryService(sessions),
        integrity=DataIntegrityService(sessions, media_store=store),
        backups=BackupService(config), backup_verification=BackupVerificationService(config),
        exports=WorkspaceExportService(config, sessions))
