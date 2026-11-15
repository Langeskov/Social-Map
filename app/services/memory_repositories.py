"""Memory service wiring: memory rows plus the fact lookups they validate against."""
from dataclasses import dataclass
from app.repositories.interfaces import PersonRepository, WorkspaceRepository
from app.repositories.memory_interfaces import MemoryRepository


@dataclass(frozen=True)
class MemoryRepositories:
    memories: MemoryRepository
    people: PersonRepository
    workspaces: WorkspaceRepository


def sql_memory_repositories(session) -> MemoryRepositories:
    from app.storage.memory_repositories import SQLMemoryRepository
    from app.storage.repositories import SQLPersonRepository, SQLWorkspaceRepository
    return MemoryRepositories(SQLMemoryRepository(session), SQLPersonRepository(session),
                              SQLWorkspaceRepository(session))
