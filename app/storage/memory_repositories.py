"""Memory storage adapter: soft delete, validity window, workspace isolation."""
from dataclasses import fields
from datetime import datetime
from sqlalchemy import select

from app.domain.entities import Memory
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import utc_now
from app.storage.models import MemoryModel, WorkspaceModel


from app.storage.mapping import to_domain, to_values as _values


def _snapshot(model):
    """One Memory row → domain entity (the entity type is fixed here)."""
    return to_domain(Memory, model)


class SQLMemoryRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id):
        return select(MemoryModel).join(
            WorkspaceModel, WorkspaceModel.id == MemoryModel.workspace_id).where(
            MemoryModel.workspace_id == workspace_id, WorkspaceModel.deleted_at.is_(None))

    def get(self, workspace_id, id, *, include_deleted=True):
        query = self._query(workspace_id).where(MemoryModel.id == id)
        if not include_deleted:
            query = query.where(MemoryModel.deleted_at.is_(None))
        return _snapshot(self.session.scalar(query))

    def list_by_workspace(self, workspace_id, *, person_id=None, workspace_scoped_only=False,
                          kind=None, include_deleted=False, now=None, include_expired=False,
                          limit=None):
        query = self._query(workspace_id)
        if workspace_scoped_only:
            query = query.where(MemoryModel.person_id.is_(None))
        elif person_id is not None:
            query = query.where(MemoryModel.person_id == person_id)
        if kind is not None:
            query = query.where(MemoryModel.kind == kind)
        if not include_deleted:
            query = query.where(MemoryModel.deleted_at.is_(None))
        if not include_expired:
            # "Current" at `now`: started already, not yet expired.
            moment = now or utc_now()
            query = query.where((MemoryModel.valid_from.is_(None)) | (MemoryModel.valid_from <= moment),
                                (MemoryModel.valid_until.is_(None)) | (MemoryModel.valid_until > moment))
        query = query.order_by(MemoryModel.created_at.desc(), MemoryModel.id.desc())
        if limit is not None:
            query = query.limit(int(limit))
        return [_snapshot(model) for model in self.session.scalars(query)]

    def save(self, entity):
        if self.session.get(MemoryModel, entity.id) is not None:
            raise DomainError("Memory 已存在。")
        model = MemoryModel(**_values(entity))
        self.session.add(model)
        self.session.flush()
        return _snapshot(model)

    def update(self, entity):
        model = self.session.get(MemoryModel, entity.id)
        if model is None or model.workspace_id != entity.workspace_id:
            raise NotFoundError("Memory 不属于当前 Workspace。")
        # Provenance and identity are write-once: editing a memory never rewrites
        # where it came from.
        for name in ("id", "workspace_id", "source_type", "source_analysis_id", "confirmed_at",
                     "created_at"):
            if getattr(model, name) != getattr(entity, name):
                raise DomainError(f"Memory 的 {name} 不可修改。删除请使用软删除。")
        for field_ in fields(entity):
            if field_.name in {"id", "workspace_id", "source_type", "source_analysis_id",
                               "confirmed_at", "created_at"}:
                continue
            setattr(model, field_.name, getattr(entity, field_.name))
        self.session.flush()
        return _snapshot(model)
