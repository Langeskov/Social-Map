"""Explicit wiring boundary: Services use Domain contracts, adapters own mapping."""
from dataclasses import dataclass
from collections.abc import Callable
from sqlalchemy.orm import Session
from app.repositories.interfaces import WorkspaceRepository, PersonRepository, PersonAliasRepository, MediaRepository, PersonMediaRepository

@dataclass(frozen=True)
class Repositories:
    workspaces: WorkspaceRepository
    people: PersonRepository
    aliases: PersonAliasRepository
    media: MediaRepository
    person_media: PersonMediaRepository


def sql_repositories(session: Session) -> Repositories:
    from app.storage.repositories import SQLWorkspaceRepository, SQLPersonRepository, SQLPersonAliasRepository, SQLMediaRepository, SQLPersonMediaRepository
    return Repositories(SQLWorkspaceRepository(session), SQLPersonRepository(session),
                        SQLPersonAliasRepository(session), SQLMediaRepository(session), SQLPersonMediaRepository(session))

RepositoryFactory = Callable[[Session], Repositories]
