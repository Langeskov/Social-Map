"""Continuous relationship identities, append-only state history, one write transaction."""
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID
from app.domain.entities import Relationship, RelationshipHistory
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.domain.validation import normalize_datetime, utc_now, validate_text, validate_uuid
from app.services.relationship_repositories import sql_relationship_repositories
from app.services.rules import workspace_required
from app.services.operations import write_operation
from app.services.unit_of_work import transaction

@dataclass(frozen=True)
class RelationshipPerson:
    id: UUID
    display_name: str
    deleted_at: datetime | None

@dataclass(frozen=True)
class RelationshipView:
    relationship: Relationship
    person_a: RelationshipPerson
    person_b: RelationshipPerson
    current_history: RelationshipHistory | None

    @property
    def current_state(self):
        return self.current_history.state if self.current_history is not None else None

    @property
    def endpoints_active(self):
        return self.person_a.deleted_at is None and self.person_b.deleted_at is None

    def other(self, person_id):
        if person_id == self.person_a.id:
            return self.person_b
        if person_id == self.person_b.id:
            return self.person_a
        raise DomainError("当前人物不是该关系的端点。")

class RelationshipService:
    STATES = {"ACTIVE", "PAUSED", "ENDED"}
    DIRECTIONS = {"UNDIRECTED", "DIRECTED"}

    def __init__(self, sessions, repositories=sql_relationship_repositories, *, clock=utc_now):
        self._sessions, self._repositories, self._clock = sessions, repositories, clock

    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    def _state(self, state):
        if not isinstance(state, str) or state.strip().upper() not in self.STATES:
            raise DomainError("Phase 4 状态必须是 ACTIVE、PAUSED 或 ENDED。")
        return state.strip().upper()

    def _endpoints(self, repositories, workspace_id, person_a_id, person_b_id):
        workspace_required(repositories, workspace_id)
        for id in (person_a_id, person_b_id):
            validate_uuid(id, "person_id")
            if repositories.people.get(workspace_id, id) is None:
                raise WorkspaceMismatchError("关系端点必须是当前 Workspace 中未归档的人物。")
        if person_a_id == person_b_id:
            raise DomainError("不能建立人物与自身的关系。")

    def _get(self, repositories, workspace_id, id, *, include_deleted=False):
        workspace_required(repositories, workspace_id)
        entity = repositories.relationships.get(workspace_id, id, include_deleted=include_deleted)
        if entity is None:
            raise NotFoundError("关系不属于当前 Workspace、已删除或端点已归档。")
        return entity

    def _view(self, repositories, entity, as_of):
        a = repositories.people.get(entity.workspace_id, entity.person_a_id, include_deleted=True)
        b = repositories.people.get(entity.workspace_id, entity.person_b_id, include_deleted=True)
        if a is None or b is None:
            raise NotFoundError("关系端点数据缺失，请检查数据完整性。")
        return RelationshipView(entity, RelationshipPerson(a.id, a.display_name, a.deleted_at),
                                RelationshipPerson(b.id, b.display_name, b.deleted_at),
                                repositories.histories.get_latest(entity.workspace_id, entity.id, as_of=as_of))

    def create(self, workspace_id: UUID, person_a_id: UUID, person_b_id: UUID,
               relationship_type: str, directionality: str = "UNDIRECTED", *,
               initial_state: str = "ACTIVE", effective_at: datetime | None = None, note: str | None = None) -> Relationship:
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(person_a_id, "person_a_id")
        validate_uuid(person_b_id, "person_b_id")
        if not isinstance(directionality, str) or directionality not in self.DIRECTIONS:
            raise DomainError("关系方向必须是 UNDIRECTED 或 DIRECTED。")
        if not isinstance(relationship_type, str):
            raise DomainError("关系类型必须是字符串。")
        kind = relationship_type.strip()
        validate_text(kind, "relationship_type", max_length=200)
        if directionality == "UNDIRECTED" and person_a_id.int > person_b_id.int:
            person_a_id, person_b_id = person_b_id, person_a_id
        state = self._state(initial_state)
        now = self._now()
        effective = now if effective_at is None else normalize_datetime(effective_at, "effective_at")
        entity = Relationship(workspace_id=workspace_id, person_a_id=person_a_id, person_b_id=person_b_id,
                              relationship_type=kind, directionality=directionality, created_at=now, updated_at=now)
        history = RelationshipHistory(workspace_id=workspace_id, relationship_id=entity.id, state=state,
                                      effective_at=effective, note=note, created_at=now)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            self._endpoints(repositories, workspace_id, person_a_id, person_b_id)
            duplicate = repositories.relationships.get_by_identity(workspace_id, person_a_id, person_b_id, kind, directionality)
            if duplicate is not None:
                if duplicate.deleted_at is not None:
                    raise ConflictError("同身份关系已删除，请恢复原记录，而不是创建第二条。")
                raise ConflictError("同身份关系已经存在。结束或恢复状态请追加历史，不要重复创建。")
            saved = repositories.relationships.save(entity)
            repositories.histories.add(history)
            return saved

    def get(self, workspace_id: UUID, id: UUID, *, include_deleted=False) -> Relationship:
        with transaction(self._sessions) as session:
            return self._get(self._repositories(session), workspace_id, id, include_deleted=include_deleted)

    def current(self, workspace_id: UUID, id: UUID, *, as_of: datetime | None = None, include_deleted=False) -> RelationshipView:
        instant = self._now() if as_of is None else normalize_datetime(as_of, "as_of")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            return self._view(repositories, self._get(repositories, workspace_id, id, include_deleted=include_deleted), instant)

    def list_by_person(self, workspace_id: UUID, person_id: UUID, *, include_deleted=False, as_of: datetime | None = None) -> list[RelationshipView]:
        instant = self._now() if as_of is None else normalize_datetime(as_of, "as_of")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            if repositories.people.get(workspace_id, person_id) is None:
                raise NotFoundError("当前人物已归档或不属于该 Workspace。")
            return [self._view(repositories, entity, instant) for entity in
                    repositories.relationships.list_by_person(workspace_id, person_id, include_deleted=include_deleted)]

    def list_by_workspace(self, workspace_id: UUID, *, include_deleted=False, as_of: datetime | None = None) -> list[RelationshipView]:
        instant = self._now() if as_of is None else normalize_datetime(as_of, "as_of")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return [self._view(repositories, entity, instant) for entity in
                    repositories.relationships.list_by_workspace(workspace_id, include_deleted=include_deleted)]

    def history(self, workspace_id: UUID, id: UUID, *, include_deleted=False) -> tuple[RelationshipHistory, ...]:
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            self._get(repositories, workspace_id, id, include_deleted=include_deleted)
            return tuple(repositories.histories.list_by_relationship(workspace_id, id))

    def add_history(self, workspace_id: UUID, id: UUID, state: str, *, effective_at: datetime | None = None, note: str | None = None) -> RelationshipHistory:
        state = self._state(state)
        now = self._now()
        effective = now if effective_at is None else normalize_datetime(effective_at, "effective_at")
        entry = RelationshipHistory(workspace_id=workspace_id, relationship_id=id, state=state,
                                    effective_at=effective, note=note, created_at=now)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            entity = self._get(repositories, workspace_id, id)
            self._endpoints(repositories, workspace_id, entity.person_a_id, entity.person_b_id)
            added = repositories.histories.add(entry)
            repositories.relationships.update(replace(entity, updated_at=now))
            return added

    def soft_delete(self, workspace_id: UUID, id: UUID) -> Relationship:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            entity = self._get(repositories, workspace_id, id, include_deleted=True)
            if entity.deleted_at is not None:
                return entity
            # Deleting an erroneous record does not invent an ENDED history.
            return repositories.relationships.soft_delete(workspace_id, id, self._now())

    def restore(self, workspace_id: UUID, id: UUID) -> Relationship:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            entity = self._get(repositories, workspace_id, id, include_deleted=True)
            self._endpoints(repositories, workspace_id, entity.person_a_id, entity.person_b_id)
            if entity.deleted_at is None:
                return entity
            # Lifecycle recovery keeps all past states. ACTIVE is an explicit
            # subsequent state-history operation, never an implicit field edit.
            return repositories.relationships.update(replace(entity, deleted_at=None, updated_at=self._now()))
