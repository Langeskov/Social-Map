"""SQL adapters: caller-owned Session, explicit mappings, pure Domain results.

Each save flushes before returning so consecutive parent/child saves respect
foreign keys without ORM relationships. Transaction ownership stays in services.
"""
from dataclasses import fields
from uuid import UUID

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app.domain.entities import Media, Person, PersonAlias, PersonMedia, Workspace
from app.storage import search_projection
from app.storage.mapping import (apply_update as _update, to_domain as _domain, to_values as _values)
from app.storage.models import (
    EvidenceMediaModel, EvidenceModel, MediaModel, PersonAliasModel, PersonMediaModel,
    PersonModel, WorkspaceModel,
)

__all__ = [
    "SQLWorkspaceRepository", "SQLPersonRepository", "SQLPersonAliasRepository",
    "SQLMediaRepository", "SQLPersonMediaRepository",
]


def _literal_pattern(search: str) -> str:
    # Escape the escape character first. Percent/underscore are user text.
    return "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SQLWorkspaceRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, id: UUID, *, include_deleted: bool = False) -> Workspace | None:
        query = select(WorkspaceModel).where(WorkspaceModel.id == id)
        if not include_deleted:
            query = query.where(WorkspaceModel.deleted_at.is_(None))
        return _domain(Workspace, self.session.scalar(query))

    def get_by_name(self, name: str, *, include_deleted: bool = False) -> Workspace | None:
        query = select(WorkspaceModel).where(WorkspaceModel.name == name)
        if not include_deleted:
            query = query.where(WorkspaceModel.deleted_at.is_(None))
        return _domain(Workspace, self.session.scalar(query))

    def list(self, *, include_deleted: bool = False) -> list[Workspace]:
        query = select(WorkspaceModel)
        if not include_deleted:
            query = query.where(WorkspaceModel.deleted_at.is_(None))
        return [_domain(Workspace, model) for model in self.session.scalars(
            query.order_by(WorkspaceModel.name, WorkspaceModel.id))]

    def save(self, entity: Workspace) -> Workspace:
        model = self.session.get(WorkspaceModel, entity.id)
        if model is None:
            model = WorkspaceModel(**_values(entity))
            self.session.add(model)
        else:
            _update(model, entity)
        self.session.flush()
        return _domain(Workspace, model)


class SQLPersonRepository:
    def __init__(self, session: Session):
        self.session = session

    def _query(self, workspace_id: UUID, include_deleted: bool):
        query = select(PersonModel).where(PersonModel.workspace_id == workspace_id)
        if not include_deleted:
            query = query.join(WorkspaceModel, WorkspaceModel.id == PersonModel.workspace_id).where(
                PersonModel.deleted_at.is_(None), WorkspaceModel.deleted_at.is_(None))
        return query

    def get(self, workspace_id: UUID, id: UUID, *, include_deleted: bool = False) -> Person | None:
        return _domain(Person, self.session.scalar(
            self._query(workspace_id, include_deleted).where(PersonModel.id == id)))

    def list(self, workspace_id: UUID, search: str = "", *, include_deleted: bool = False) -> list[Person]:
        query = self._query(workspace_id, include_deleted)
        if search:
            pattern = _literal_pattern(search)
            alias_match = exists(select(PersonAliasModel.id).where(
                PersonAliasModel.workspace_id == workspace_id,
                PersonAliasModel.person_id == PersonModel.id,
                PersonAliasModel.alias.like(pattern, escape="\\")))
            query = query.where(or_(PersonModel.display_name.like(pattern, escape="\\"),
                                    PersonModel.real_name.like(pattern, escape="\\"), alias_match))
        return [_domain(Person, model) for model in self.session.scalars(
            query.order_by(PersonModel.display_name, PersonModel.id))]

    def save(self, entity: Person) -> Person:
        model = self.session.get(PersonModel, entity.id)
        if model is None:
            model = PersonModel(**_values(entity))
            self.session.add(model)
        else:
            _update(model, entity)
        self.session.flush()
        saved = _domain(Person, model)
        # Phase 7: keep the derived search projection in the same transaction.
        search_projection.sync_person(self.session, saved)
        return saved


class SQLPersonAliasRepository:
    def __init__(self, session: Session):
        self.session = session

    def _query(self, workspace_id: UUID):
        return (select(PersonAliasModel)
                .join(PersonModel, (PersonModel.id == PersonAliasModel.person_id)
                      & (PersonModel.workspace_id == PersonAliasModel.workspace_id))
                .join(WorkspaceModel, WorkspaceModel.id == PersonModel.workspace_id)
                .where(PersonAliasModel.workspace_id == workspace_id,
                       PersonModel.deleted_at.is_(None), WorkspaceModel.deleted_at.is_(None)))

    def get(self, workspace_id: UUID, id: UUID) -> PersonAlias | None:
        return _domain(PersonAlias, self.session.scalar(
            self._query(workspace_id).where(PersonAliasModel.id == id)))

    def list(self, workspace_id: UUID, person_id: UUID) -> list[PersonAlias]:
        query = self._query(workspace_id).where(PersonAliasModel.person_id == person_id)
        return [_domain(PersonAlias, model) for model in self.session.scalars(
            query.order_by(PersonAliasModel.alias, PersonAliasModel.id))]

    def save(self, entity: PersonAlias) -> PersonAlias:
        model = self.session.get(PersonAliasModel, entity.id)
        if model is None:
            model = PersonAliasModel(**_values(entity))
            self.session.add(model)
        else:
            _update(model, entity)
        self.session.flush()
        saved = _domain(PersonAlias, model)
        search_projection.sync_alias_row(self.session, saved)
        return saved

    def remove(self, workspace_id: UUID, id: UUID) -> None:
        # Scope-only deletion must also work for inactive parents (compensation).
        self.session.execute(delete(PersonAliasModel).where(
            PersonAliasModel.workspace_id == workspace_id, PersonAliasModel.id == id))
        self.session.flush()
        search_projection.remove_alias(self.session, workspace_id, id)


class SQLMediaRepository:
    def __init__(self, session: Session):
        self.session = session

    def _query(self, workspace_id: UUID, include_deleted: bool):
        query = select(MediaModel).where(MediaModel.workspace_id == workspace_id)
        if not include_deleted:
            query = query.join(WorkspaceModel, WorkspaceModel.id == MediaModel.workspace_id).where(
                MediaModel.deleted_at.is_(None), WorkspaceModel.deleted_at.is_(None))
        return query

    def get(self, workspace_id: UUID, id: UUID, *, include_deleted: bool = False) -> Media | None:
        return _domain(Media, self.session.scalar(
            self._query(workspace_id, include_deleted).where(MediaModel.id == id)))

    def get_by_hash(self, workspace_id: UUID, sha256: str, *, include_deleted: bool = False) -> Media | None:
        return _domain(Media, self.session.scalar(
            self._query(workspace_id, include_deleted).where(MediaModel.sha256 == sha256)))

    def list(self, workspace_id: UUID, *, include_deleted: bool = False) -> list[Media]:
        return [_domain(Media, model) for model in self.session.scalars(
            self._query(workspace_id, include_deleted).order_by(MediaModel.created_at, MediaModel.id))]

    def has_references(self, workspace_id: UUID, media_id: UUID, *, active_only: bool = True) -> bool:
        """Media is referenced while any binding row still exists.

        Phase 7 rule: an EvidenceMedia row counts even when its Evidence is soft
        deleted, because soft deleting Evidence deliberately keeps its bindings —
        so Evidence soft delete does NOT release the Media. Only removing the
        binding itself (or deleting an attached person link) frees the media.
        """
        person = select(PersonMediaModel.id).join(PersonModel, (
            PersonModel.id == PersonMediaModel.person_id)
            & (PersonModel.workspace_id == PersonMediaModel.workspace_id)).join(MediaModel, (
                MediaModel.id == PersonMediaModel.media_id)
                & (MediaModel.workspace_id == PersonMediaModel.workspace_id)).where(
            PersonMediaModel.workspace_id == workspace_id, PersonMediaModel.media_id == media_id)
        if active_only:
            person = person.where(PersonModel.deleted_at.is_(None), MediaModel.deleted_at.is_(None))
        if self.session.scalar(select(person.exists())):
            return True
        # Evidence bindings always count, whatever the Evidence lifecycle is.
        evidence = select(EvidenceMediaModel.id).where(
            EvidenceMediaModel.workspace_id == workspace_id, EvidenceMediaModel.media_id == media_id)
        return bool(self.session.scalar(select(evidence.exists())))

    def save(self, entity: Media) -> Media:
        model = self.session.get(MediaModel, entity.id)
        if model is None:
            model = MediaModel(**_values(entity))
            self.session.add(model)
        else:
            _update(model, entity)
        self.session.flush()
        return _domain(Media, model)


class SQLPersonMediaRepository:
    def __init__(self, session: Session):
        self.session = session

    def _query(self, workspace_id: UUID):
        return (select(PersonMediaModel)
                .join(PersonModel, (PersonModel.id == PersonMediaModel.person_id)
                      & (PersonModel.workspace_id == PersonMediaModel.workspace_id))
                .join(MediaModel, (MediaModel.id == PersonMediaModel.media_id)
                      & (MediaModel.workspace_id == PersonMediaModel.workspace_id))
                .join(WorkspaceModel, WorkspaceModel.id == PersonMediaModel.workspace_id)
                .where(PersonMediaModel.workspace_id == workspace_id,
                       PersonModel.deleted_at.is_(None), MediaModel.deleted_at.is_(None),
                       WorkspaceModel.deleted_at.is_(None)))

    def get(self, workspace_id: UUID, id: UUID) -> PersonMedia | None:
        return _domain(PersonMedia, self.session.scalar(
            self._query(workspace_id).where(PersonMediaModel.id == id)))

    def list(self, workspace_id: UUID, person_id: UUID) -> list[PersonMedia]:
        query = self._query(workspace_id).where(PersonMediaModel.person_id == person_id)
        return [_domain(PersonMedia, model) for model in self.session.scalars(
            query.order_by(PersonMediaModel.sort_order, PersonMediaModel.id))]

    def save(self, entity: PersonMedia) -> PersonMedia:
        model = self.session.get(PersonMediaModel, entity.id)
        if model is None:
            model = PersonMediaModel(**_values(entity))
            self.session.add(model)
        else:
            _update(model, entity)
        self.session.flush()
        return _domain(PersonMedia, model)

    def remove(self, workspace_id: UUID, id: UUID) -> None:
        self.session.execute(delete(PersonMediaModel).where(
            PersonMediaModel.workspace_id == workspace_id, PersonMediaModel.id == id))
        self.session.flush()

    def remove_for_person(self, workspace_id: UUID, person_id: UUID, role: str | None = None) -> None:
        query = delete(PersonMediaModel).where(
            PersonMediaModel.workspace_id == workspace_id, PersonMediaModel.person_id == person_id)
        if role is not None:
            query = query.where(PersonMediaModel.role == role)
        self.session.execute(query)
        self.session.flush()

    def has_references(self, workspace_id: UUID, media_id: UUID, *, active_only: bool = True) -> bool:
        if active_only:
            query = self._query(workspace_id).where(PersonMediaModel.media_id == media_id)
        else:
            query = select(PersonMediaModel.id).where(
                PersonMediaModel.workspace_id == workspace_id, PersonMediaModel.media_id == media_id)
        return bool(self.session.scalar(select(query.exists())))
