"""Explicit relationship adapters. Identity is immutable; history is append-only."""
from dataclasses import fields, replace
from datetime import datetime
from uuid import UUID
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased
from app.domain.entities import Relationship, RelationshipHistory
from app.domain.errors import DomainError, NotFoundError
from app.storage.mapping import to_domain as snapshot, to_values as values
from app.storage.models import RelationshipModel, RelationshipHistoryModel, PersonModel, WorkspaceModel

IDENTITY = ("workspace_id", "person_a_id", "person_b_id", "relationship_type", "directionality", "created_at")


class SQLRelationshipRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted=False):
        query = select(RelationshipModel).where(RelationshipModel.workspace_id == workspace_id)
        if not include_deleted:
            a, b = aliased(PersonModel), aliased(PersonModel)
            query = query.join(WorkspaceModel, WorkspaceModel.id == RelationshipModel.workspace_id).join(
                a, and_(a.workspace_id == RelationshipModel.workspace_id, a.id == RelationshipModel.person_a_id)).join(
                b, and_(b.workspace_id == RelationshipModel.workspace_id, b.id == RelationshipModel.person_b_id)).where(
                WorkspaceModel.deleted_at.is_(None), RelationshipModel.deleted_at.is_(None),
                a.deleted_at.is_(None), b.deleted_at.is_(None))
        return query

    def get(self, workspace_id: UUID, id: UUID, *, include_deleted=False):
        return snapshot(Relationship, self.session.scalar(self._query(workspace_id, include_deleted).where(RelationshipModel.id == id)))

    def get_by_identity(self, workspace_id, person_a_id, person_b_id, relationship_type, directionality, *, include_deleted=True):
        query = self._query(workspace_id, include_deleted).where(
            RelationshipModel.person_a_id == person_a_id, RelationshipModel.person_b_id == person_b_id,
            RelationshipModel.relationship_type == relationship_type, RelationshipModel.directionality == directionality)
        return snapshot(Relationship, self.session.scalar(query))

    def list_by_person(self, workspace_id, person_id, *, include_deleted=False):
        query = self._query(workspace_id, include_deleted).where(or_(
            RelationshipModel.person_a_id == person_id, RelationshipModel.person_b_id == person_id))
        return [snapshot(Relationship, model) for model in self.session.scalars(query.order_by(RelationshipModel.created_at, RelationshipModel.id))]

    def list_by_workspace(self, workspace_id, *, include_deleted=False):
        query = self._query(workspace_id, include_deleted).order_by(RelationshipModel.created_at, RelationshipModel.id)
        return [snapshot(Relationship, model) for model in self.session.scalars(query)]

    def save(self, entity: Relationship):
        model = self.session.get(RelationshipModel, entity.id)
        if model is None:
            model = RelationshipModel(**values(entity))
            self.session.add(model)
        else:
            self._apply_lifecycle(model, entity)
        self.session.flush()
        return snapshot(Relationship, model)

    def _apply_lifecycle(self, model, entity):
        if any(getattr(model, field) != getattr(entity, field) for field in IDENTITY):
            raise DomainError("Phase 4 不允许修改关系身份；请删除错误记录后创建正确关系。")
        model.updated_at, model.deleted_at = entity.updated_at, entity.deleted_at

    def update(self, entity: Relationship):
        model = self.session.get(RelationshipModel, entity.id)
        if model is None:
            raise NotFoundError("关系记录不存在。")
        self._apply_lifecycle(model, entity)
        self.session.flush()
        return snapshot(Relationship, model)

    def soft_delete(self, workspace_id, id, at: datetime):
        entity = self.get(workspace_id, id, include_deleted=True)
        if entity is None:
            raise NotFoundError("关系不属于当前 Workspace。")
        return self.update(replace(entity, deleted_at=at, updated_at=at))

class SQLRelationshipHistoryRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, relationship_id):
        return select(RelationshipHistoryModel).join(RelationshipModel, and_(
            RelationshipModel.id == RelationshipHistoryModel.relationship_id,
            RelationshipModel.workspace_id == RelationshipHistoryModel.workspace_id)).where(
            RelationshipHistoryModel.workspace_id == workspace_id,
            RelationshipHistoryModel.relationship_id == relationship_id)

    def list_by_relationship(self, workspace_id, relationship_id):
        query = self._query(workspace_id, relationship_id).order_by(
            RelationshipHistoryModel.effective_at, RelationshipHistoryModel.created_at, RelationshipHistoryModel.id)
        return [snapshot(RelationshipHistory, model) for model in self.session.scalars(query)]

    def list_by_workspace(self, workspace_id):
        """One query for every history row: timeline/graph/dashboard must not be N+1."""
        query = select(RelationshipHistoryModel).join(RelationshipModel, and_(
            RelationshipModel.id == RelationshipHistoryModel.relationship_id,
            RelationshipModel.workspace_id == RelationshipHistoryModel.workspace_id)).where(
            RelationshipHistoryModel.workspace_id == workspace_id).order_by(
            RelationshipHistoryModel.effective_at, RelationshipHistoryModel.created_at, RelationshipHistoryModel.id)
        return [snapshot(RelationshipHistory, model) for model in self.session.scalars(query)]

    def add(self, entity: RelationshipHistory):
        model = RelationshipHistoryModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        return snapshot(RelationshipHistory, model)

    def get(self, workspace_id, id):
        query = select(RelationshipHistoryModel).where(
            RelationshipHistoryModel.workspace_id == workspace_id, RelationshipHistoryModel.id == id)
        return snapshot(RelationshipHistory, self.session.scalar(query))

    def list_by_event(self, workspace_id, event_id):
        query = select(RelationshipHistoryModel).where(
            RelationshipHistoryModel.workspace_id == workspace_id, RelationshipHistoryModel.event_id == event_id
        ).order_by(RelationshipHistoryModel.effective_at, RelationshipHistoryModel.created_at, RelationshipHistoryModel.id)
        return [snapshot(RelationshipHistory, model) for model in self.session.scalars(query)]

    def attach_event(self, workspace_id, id, event_id):
        # Flush pre-existing changes before issuing a narrowly scoped capability.
        # State history has no general-purpose update API.
        self.session.flush()
        model = self.session.scalar(select(RelationshipHistoryModel).where(
            RelationshipHistoryModel.workspace_id == workspace_id, RelationshipHistoryModel.id == id))
        if model is None:
            raise NotFoundError("History 不属于当前 Workspace。")
        old = model.event_id
        if old == event_id:
            return snapshot(RelationshipHistory, model)
        key = "ris_history_event_attachment"
        if key in self.session.info:
            raise DomainError("不允许嵌套历史关联授权。")
        self.session.info[key] = (model.id, old, event_id)
        try:
            model.event_id = event_id
            self.session.flush()
            return snapshot(RelationshipHistory, model)
        finally:
            self.session.info.pop(key, None)

    def get_latest(self, workspace_id, relationship_id, *, as_of=None):
        query = self._query(workspace_id, relationship_id)
        if as_of is not None:
            query = query.where(RelationshipHistoryModel.effective_at <= as_of)
        query = query.order_by(RelationshipHistoryModel.effective_at.desc(),
                               RelationshipHistoryModel.created_at.desc(), RelationshipHistoryModel.id.desc()).limit(1)
        return snapshot(RelationshipHistory, self.session.scalar(query))
