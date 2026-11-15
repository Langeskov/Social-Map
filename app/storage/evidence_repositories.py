"""Session-injected Evidence adapters. Original material is write-once.

`update_metadata` only ever assigns title/annotation; the ORM guard in
app.storage.models is the second line of defence against rewriting original
content, kind, source, captured_at or the owning Event.
"""
from dataclasses import fields
from sqlalchemy import and_, delete, select
from app.domain.entities import Evidence, EvidenceMedia
from app.domain.errors import DomainError, NotFoundError
from app.storage import search_projection
from app.storage.models import EvidenceModel, EvidenceMediaModel, WorkspaceModel

METADATA_FIELDS = ("title", "annotation", "updated_at", "deleted_at")


from app.storage.mapping import to_domain as snapshot, to_values as values
class SQLEvidenceRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id, include_deleted=False):
        query = select(EvidenceModel).join(WorkspaceModel, WorkspaceModel.id == EvidenceModel.workspace_id).where(
            EvidenceModel.workspace_id == workspace_id, WorkspaceModel.deleted_at.is_(None))
        if not include_deleted:
            query = query.where(EvidenceModel.deleted_at.is_(None))
        return query

    def get(self, workspace_id, id, *, include_deleted=False):
        return snapshot(Evidence, self.session.scalar(
            self._query(workspace_id, include_deleted).where(EvidenceModel.id == id)))

    def _list(self, workspace_id, *, include_deleted=False, kind=None, event_id=None):
        query = self._query(workspace_id, include_deleted)
        if event_id is not None:
            query = query.where(EvidenceModel.event_id == event_id)
        if kind is not None:
            query = query.where(EvidenceModel.kind == kind)
        # Explicit SQL ordering: newest capture first, then newest write, then UUID.
        query = query.order_by(EvidenceModel.created_at.desc(), EvidenceModel.id.desc())
        return [snapshot(Evidence, model) for model in self.session.scalars(query)]

    def list_by_event(self, workspace_id, event_id, *, include_deleted=False, kind=None):
        return self._list(workspace_id, include_deleted=include_deleted, kind=kind, event_id=event_id)

    def list_by_workspace(self, workspace_id, *, include_deleted=False, kind=None):
        return self._list(workspace_id, include_deleted=include_deleted, kind=kind)

    def save(self, entity):
        if self.session.get(EvidenceModel, entity.id) is not None:
            raise DomainError("证据已存在；原始证据不可覆盖，请软删除后新建。")
        model = EvidenceModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        saved = snapshot(Evidence, model)
        search_projection.sync_evidence(self.session, saved)
        return saved

    def update_metadata(self, entity):
        model = self.session.get(EvidenceModel, entity.id)
        if model is None or model.workspace_id != entity.workspace_id:
            raise NotFoundError("证据不属于当前 Workspace。")
        for name in METADATA_FIELDS:
            setattr(model, name, getattr(entity, name))
        self.session.flush()
        saved = snapshot(Evidence, model)
        # Title/annotation are searchable, so the projection follows the fact here.
        search_projection.sync_evidence(self.session, saved)
        return saved

    def soft_delete(self, workspace_id, id, at):
        entity = self.get(workspace_id, id, include_deleted=True)
        if entity is None:
            raise NotFoundError("证据不属于当前 Workspace。")
        from dataclasses import replace
        return self.update_metadata(replace(entity, deleted_at=at, updated_at=at))

    def restore(self, workspace_id, id, at):
        entity = self.get(workspace_id, id, include_deleted=True)
        if entity is None:
            raise NotFoundError("证据不属于当前 Workspace。")
        from dataclasses import replace
        return self.update_metadata(replace(entity, deleted_at=None, updated_at=at))


class SQLEvidenceMediaRepository:
    def __init__(self, session):
        self.session = session

    def get(self, workspace_id, id):
        return snapshot(EvidenceMedia, self.session.scalar(select(EvidenceMediaModel).where(
            EvidenceMediaModel.workspace_id == workspace_id, EvidenceMediaModel.id == id)))

    def _list(self, workspace_id, column, identifier):
        query = select(EvidenceMediaModel).where(
            EvidenceMediaModel.workspace_id == workspace_id, column == identifier).order_by(
            EvidenceMediaModel.created_at, EvidenceMediaModel.id)
        return [snapshot(EvidenceMedia, model) for model in self.session.scalars(query)]

    def list_by_evidence(self, workspace_id, evidence_id):
        return self._list(workspace_id, EvidenceMediaModel.evidence_id, evidence_id)

    def list_by_media(self, workspace_id, media_id):
        return self._list(workspace_id, EvidenceMediaModel.media_id, media_id)

    def add(self, entity):
        model = EvidenceMediaModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        return snapshot(EvidenceMedia, model)

    def remove(self, workspace_id, evidence_id, media_id, role=None):
        query = delete(EvidenceMediaModel).where(
            EvidenceMediaModel.workspace_id == workspace_id,
            EvidenceMediaModel.evidence_id == evidence_id,
            EvidenceMediaModel.media_id == media_id)
        if role is not None:
            query = query.where(EvidenceMediaModel.role == role)
        result = self.session.execute(query)
        self.session.flush()
        return result.rowcount or 0

    def has_references(self, workspace_id, media_id, *, active_only=True):
        query = select(EvidenceMediaModel.id).join(EvidenceModel, and_(
            EvidenceModel.id == EvidenceMediaModel.evidence_id,
            EvidenceModel.workspace_id == EvidenceMediaModel.workspace_id)).where(
            EvidenceMediaModel.workspace_id == workspace_id,
            EvidenceMediaModel.media_id == media_id)
        if active_only:
            query = query.where(EvidenceModel.deleted_at.is_(None))
        return bool(self.session.scalar(select(query.exists())))
