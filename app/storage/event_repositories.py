"""Session-injected Event adapters: explicit SQL ordering, no transaction ownership."""
from dataclasses import replace
from sqlalchemy import DateTime, String, and_, cast, func, or_, select
from app.domain.entities import Event, EventParticipant, Evidence
from app.domain.errors import DomainError, NotFoundError
from app.storage import search_projection
from app.storage.models import EventModel, EventParticipantModel, EvidenceModel, WorkspaceModel
from app.storage.mapping import to_domain as snapshot, to_values as values


class SQLEventRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted=False):
        query = select(EventModel).where(EventModel.workspace_id == workspace_id)
        if not include_deleted:
            query = query.join(WorkspaceModel, WorkspaceModel.id == EventModel.workspace_id).where(
                EventModel.deleted_at.is_(None), WorkspaceModel.deleted_at.is_(None))
        return query

    def _ordered(self, query):
        # SQLite CAST(date AS DATETIME) produces an integer year, not a timestamp.
        # Its datetime() formats both sources consistently; PG needs a typed cast.
        if self.session.get_bind().dialect.name == "sqlite":
            instant = func.coalesce(cast(EventModel.started_at, String),
                cast(EventModel.occurred_on, String) + " 00:00:00.000000")
        else:
            instant = func.coalesce(EventModel.started_at,
                func.timezone("UTC", cast(EventModel.occurred_on, DateTime(timezone=False))))
        return query.order_by(instant.desc(), EventModel.id.desc())

    def get(self, workspace_id, id, *, include_deleted=False):
        return snapshot(Event, self.session.scalar(self._query(workspace_id, include_deleted).where(EventModel.id == id)))

    def list_by_workspace(self, workspace_id, search="", *, include_deleted=False):
        query = self._query(workspace_id, include_deleted)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            query = query.where(or_(*(column.ilike(pattern, escape="\\") for column in
                                    (EventModel.title, EventModel.description, EventModel.event_type))))
        return [snapshot(Event, row) for row in self.session.scalars(self._ordered(query))]

    def list_by_person(self, workspace_id, person_id, *, include_deleted=False):
        query = self._query(workspace_id, include_deleted).join(EventParticipantModel, and_(
            EventParticipantModel.workspace_id == EventModel.workspace_id,
            EventParticipantModel.event_id == EventModel.id)).where(EventParticipantModel.person_id == person_id)
        return [snapshot(Event, row) for row in self.session.scalars(self._ordered(query))]

    def save(self, entity):
        if self.session.get(EventModel, entity.id) is not None:
            return self.update(entity)
        model = EventModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        saved = snapshot(Event, model)
        search_projection.sync_event(self.session, saved)
        return saved

    def update(self, entity):
        model = self.session.get(EventModel, entity.id)
        if model is None:
            raise NotFoundError("Event 不存在。")
        if model.workspace_id != entity.workspace_id or model.created_at != entity.created_at:
            raise DomainError("不能修改 Event 的 Workspace 或创建时间。")
        for key, value in values(entity).items():
            if key not in {"id", "workspace_id", "created_at"}:
                setattr(model, key, value)
        self.session.flush()
        saved = snapshot(Event, model)
        # Phase 7: the search projection follows the fact inside the same transaction.
        search_projection.sync_event(self.session, saved)
        # Evidence inherits its Event's visibility (deleted OR restored) in the index.
        for evidence in self.session.scalars(select(EvidenceModel).where(
                EvidenceModel.workspace_id == saved.workspace_id,
                EvidenceModel.event_id == saved.id)):
            search_projection.sync_evidence(self.session, snapshot(Evidence, evidence))
        return saved

    def soft_delete(self, workspace_id, id, at):
        entity = self.get(workspace_id, id, include_deleted=True)
        if entity is None:
            raise NotFoundError("Event 不属于当前 Workspace。")
        return self.update(replace(entity, deleted_at=at, updated_at=at))

    def restore(self, workspace_id, id, at):
        entity = self.get(workspace_id, id, include_deleted=True)
        if entity is None:
            raise NotFoundError("Event 不属于当前 Workspace。")
        return self.update(replace(entity, deleted_at=None, updated_at=at))


class SQLEventParticipantRepository:
    def __init__(self, session):
        self.session = session

    def list_by_event(self, workspace_id, event_id):
        query = select(EventParticipantModel).where(EventParticipantModel.workspace_id == workspace_id,
            EventParticipantModel.event_id == event_id).order_by(EventParticipantModel.created_at, EventParticipantModel.id)
        return [snapshot(EventParticipant, row) for row in self.session.scalars(query)]

    def list_by_person(self, workspace_id, person_id):
        query = select(EventParticipantModel).where(EventParticipantModel.workspace_id == workspace_id,
            EventParticipantModel.person_id == person_id).order_by(EventParticipantModel.created_at, EventParticipantModel.id)
        return [snapshot(EventParticipant, row) for row in self.session.scalars(query)]

    def add(self, entity):
        model = EventParticipantModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        return snapshot(EventParticipant, model)

    def remove(self, workspace_id, event_id, person_id):
        model = self.session.scalar(select(EventParticipantModel).where(
            EventParticipantModel.workspace_id == workspace_id, EventParticipantModel.event_id == event_id,
            EventParticipantModel.person_id == person_id))
        if model is not None:
            self.session.delete(model)
            self.session.flush()

    def update_metadata(self, entity):
        model = self.session.get(EventParticipantModel, entity.id)
        if model is None or (model.workspace_id, model.event_id, model.person_id) != (
                entity.workspace_id, entity.event_id, entity.person_id):
            raise NotFoundError("Participant 不属于当前 Event。")
        model.role, model.note = entity.role, entity.note
        self.session.flush()
        return snapshot(EventParticipant, model)
