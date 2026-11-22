"""ChangeLog Service wiring: workspace validation plus the append-only ledger."""
from dataclasses import dataclass

from app.repositories.change_log_interfaces import ChangeLogRepository
from app.repositories.interfaces import WorkspaceRepository


@dataclass(frozen=True)
class ChangeLogRepositories:
    workspaces: WorkspaceRepository
    change_log: ChangeLogRepository


def sql_change_log_repositories(session) -> ChangeLogRepositories:
    from app.storage.change_log_repositories import SQLChangeLogRepository
    from app.storage.repositories import SQLWorkspaceRepository
    return ChangeLogRepositories(SQLWorkspaceRepository(session),
                                 SQLChangeLogRepository(session))
