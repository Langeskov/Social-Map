"""Person operations return detached Domain snapshots, never ORM instances."""
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID
from app.domain.entities import Person, PersonAlias, Media, PersonMedia
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import utc_now
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.services.operations import write_operation
from app.services.rules import workspace_required, person_required
from app.services.change_snapshots import person_state
from app.services.person_links import attach_media

@dataclass(frozen=True)
class PersonImage:
    link: PersonMedia
    media: Media

@dataclass(frozen=True)
class PersonDetail:
    person: Person
    aliases: tuple[PersonAlias, ...]
    images: tuple[PersonImage, ...]

class PersonService:
    EDITABLE = {"display_name", "real_name", "gender", "birthday", "occupation", "organization", "location", "notes"}

    def __init__(self, sessions, media_service=None, repositories=sql_repositories, *,
                 changes=None):
        self._sessions, self._media, self._repositories = sessions, media_service, repositories
        self._changes = changes

    def list(self, workspace_id: UUID, search: str = "", *, include_deleted: bool = False) -> list[Person]:
        """List people of one workspace; archived people are opt-in.

        ``include_deleted`` exists for management/audit views: the relationship graph
        must be able to draw an archived person even when no relationship references
        it, and projections must be able to render an archived participant's name
        instead of a bare id.
        """
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return repositories.people.list(workspace_id, search, include_deleted=include_deleted)

    def get(self, workspace_id: UUID, person_id: UUID) -> Person:
        with transaction(self._sessions) as session:
            return person_required(self._repositories(session), workspace_id, person_id)

    def detail(self, workspace_id: UUID, person_id: UUID, *, include_deleted: bool = False) -> PersonDetail:
        """Read one person's identity, aliases and image references.

        Read-only archive contract (Phase 12.3): with ``include_deleted=True`` an
        archived person's identity and surviving aliases are readable and nothing
        else changes. ``soft_delete`` unbinds every ``PersonMedia`` row at archive
        time, so ``images`` stays empty for an archived person - Media files may
        still exist, but they are no longer part of this Person's reference set
        and are never re-bound or invented here. No write path accepts an
        archived person.
        """
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            if include_deleted:
                workspace_required(repositories, workspace_id)
                person = repositories.people.get(workspace_id, person_id, include_deleted=True)
                if person is None:
                    raise NotFoundError("人物不属于当前 Workspace。")
            else:
                person = person_required(repositories, workspace_id, person_id)
            images = tuple(PersonImage(link, repositories.media.get(workspace_id, link.media_id))
                           for link in repositories.person_media.list(workspace_id, person_id))
            return PersonDetail(person,
                                tuple(repositories.aliases.list(workspace_id, person_id,
                                                                include_deleted=include_deleted)),
                                images)

    def create(self, workspace_id: UUID, display_name: str, *, avatar_path: Path | None = None, **fields) -> Person:
        if set(fields) - (self.EDITABLE - {"display_name"}):
            raise DomainError("包含不支持的人物字段。")
        if not isinstance(display_name, str):
            raise DomainError("显示名称必须是字符串。")
        entity = Person(workspace_id=workspace_id, display_name=display_name.strip(), **fields)
        if avatar_path is not None:
            if self._media is None:
                raise DomainError("媒体服务不可用。")
            operation = self._changes.begin(workspace_id) if self._changes is not None else None
            # The person row is saved inside the media transaction, so the capture
            # happens there under this operation context (media import + person create).
            self._media.import_file(workspace_id, avatar_path, role="AVATAR",
                                    _new_person=entity, operation=operation)
            return entity
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            saved = repositories.people.save(entity)
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="CREATE", entity=saved,
                                     after=person_state(repositories, workspace_id, saved,
                                                        aliases=(), images=()))
            return saved

    def update(self, workspace_id: UUID, person_id: UUID, *, operation=None, **changes) -> Person:
        if set(changes) - self.EDITABLE:
            raise DomainError("不允许修改人物 ID、Workspace 或生命周期字段。")
        if isinstance(changes.get("display_name"), str):
            changes["display_name"] = changes["display_name"].strip()
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = person_required(repositories, workspace_id, person_id)
            before = (person_state(repositories, workspace_id, old)
                      if self._changes is not None else None)
            saved = repositories.people.save(replace(old, **changes, updated_at=utc_now()))
            context = (operation if operation is not None
                       else (self._changes.begin(workspace_id)
                             if self._changes is not None else None))
            if self._changes is not None and context is not None:
                self._changes.record(session, context, entity_type="PERSON",
                                     action="UPDATE", entity=saved, before=before,
                                     after=person_state(repositories, workspace_id, saved))
            return saved

    def soft_delete(self, workspace_id: UUID, person_id: UUID) -> None:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            old = person_required(repositories, workspace_id, person_id)
            before = (person_state(repositories, workspace_id, old)
                      if self._changes is not None else None)
            repositories.person_media.remove_for_person(workspace_id, person_id)
            now = utc_now()
            saved = repositories.people.save(replace(old, deleted_at=now, updated_at=now))
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="ARCHIVE", entity=saved, before=before,
                                     after=person_state(repositories, workspace_id, saved))

    def add_alias(self, workspace_id: UUID, person_id: UUID, alias: str, alias_type: str = "OTHER", note: str | None = None) -> PersonAlias:
        entity = PersonAlias(workspace_id=workspace_id, person_id=person_id,
                             alias=alias.strip() if isinstance(alias, str) else alias, alias_type=alias_type, note=note)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            person = person_required(repositories, workspace_id, person_id)
            before = (person_state(repositories, workspace_id, person)
                      if self._changes is not None else None)
            saved = repositories.aliases.save(entity)
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="UPDATE", entity=person, before=before,
                                     after=person_state(repositories, workspace_id, person))
            return saved

    def remove_alias(self, workspace_id: UUID, person_id: UUID, alias_id: UUID) -> None:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            person = person_required(repositories, workspace_id, person_id)
            alias = repositories.aliases.get(workspace_id, alias_id)
            if alias is None or alias.person_id != person_id:
                raise NotFoundError("别称不属于当前人物。")
            before = (person_state(repositories, workspace_id, person)
                      if self._changes is not None else None)
            repositories.aliases.remove(workspace_id, alias_id)
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="UPDATE", entity=person, before=before,
                                     after=person_state(repositories, workspace_id, person))

    def set_avatar(self, workspace_id: UUID, person_id: UUID, media_id: UUID) -> PersonMedia:
        return self._attach(workspace_id, person_id, media_id, "AVATAR")

    def add_gallery(self, workspace_id: UUID, person_id: UUID, media_id: UUID) -> PersonMedia:
        return self._attach(workspace_id, person_id, media_id, "GALLERY")

    def _attach(self, workspace_id, person_id, media_id, role):
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            person = person_required(repositories, workspace_id, person_id)
            before = (person_state(repositories, workspace_id, person)
                      if self._changes is not None else None)
            link = attach_media(repositories, workspace_id, person_id, media_id, role)
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="UPDATE", entity=person, before=before,
                                     after=person_state(repositories, workspace_id, person))
            return link

    def remove_media_reference(self, workspace_id: UUID, person_id: UUID, link_id: UUID) -> None:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            person = person_required(repositories, workspace_id, person_id)
            link = repositories.person_media.get(workspace_id, link_id)
            if link is None or link.person_id != person_id:
                raise NotFoundError("图片引用不属于当前人物。")
            before = (person_state(repositories, workspace_id, person)
                      if self._changes is not None else None)
            repositories.person_media.remove(workspace_id, link_id)
            if self._changes is not None:
                context = self._changes.begin(workspace_id)
                self._changes.record(session, context, entity_type="PERSON",
                                     action="UPDATE", entity=person, before=before,
                                     after=person_state(repositories, workspace_id, person))
