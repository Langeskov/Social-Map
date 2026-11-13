"""Analysis adapters: run records and the four typed source link tables.

The fact tables are opened read-only here (the builder reads them elsewhere); this
module only ever writes `analysis_run` and `analysis_*_source` rows.
"""
from dataclasses import fields
from sqlalchemy import delete, select

from app.domain.entities import AnalysisRun, AnalysisSource, AnalysisSourceSet
from app.domain.errors import DomainError, NotFoundError
from app.storage.models import (AnalysisEventSourceModel, AnalysisEvidenceSourceModel,
                                AnalysisPersonSourceModel, AnalysisRelationshipSourceModel,
                                AnalysisRunModel, WorkspaceModel)

SOURCE_KINDS = {
    "PERSON": ("people", AnalysisPersonSourceModel),
    "RELATIONSHIP": ("relationships", AnalysisRelationshipSourceModel),
    "EVENT": ("events", AnalysisEventSourceModel),
    "EVIDENCE": ("evidence", AnalysisEvidenceSourceModel),
}
IDENTITY = ("id", "workspace_id", "target_type", "target_id", "analysis_type", "created_at")


from app.storage.mapping import to_domain as snapshot, to_values as values
class SQLAnalysisRunRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id):
        return select(AnalysisRunModel).join(
            WorkspaceModel, WorkspaceModel.id == AnalysisRunModel.workspace_id).where(
            AnalysisRunModel.workspace_id == workspace_id, WorkspaceModel.deleted_at.is_(None))

    def get(self, workspace_id, id):
        return snapshot(AnalysisRun, self.session.scalar(self._query(workspace_id).where(
            AnalysisRunModel.id == id)))

    def list_by_workspace(self, workspace_id, *, target_type=None, target_id=None, limit=None):
        query = self._query(workspace_id)
        if target_type is not None:
            query = query.where(AnalysisRunModel.target_type == target_type)
        if target_id is not None:
            query = query.where(AnalysisRunModel.target_id == target_id)
        query = query.order_by(AnalysisRunModel.created_at.desc(), AnalysisRunModel.id.desc())
        if limit is not None:
            query = query.limit(int(limit))
        return [snapshot(AnalysisRun, model) for model in self.session.scalars(query)]

    def save(self, entity):
        if self.session.get(AnalysisRunModel, entity.id) is not None:
            raise DomainError("AnalysisRun 已存在；历史记录只追加，不覆盖。")
        model = AnalysisRunModel(**values(entity))
        self.session.add(model)
        self.session.flush()
        return snapshot(AnalysisRun, model)

    def update(self, entity):
        model = self.session.get(AnalysisRunModel, entity.id)
        if model is None or model.workspace_id != entity.workspace_id:
            raise NotFoundError("AnalysisRun 不属于当前 Workspace。")
        if model.created_at != entity.created_at:
            raise DomainError("AnalysisRun 的创建时间不可修改。")
        for field_ in fields(entity):
            if field_.name in {"id", "workspace_id", "target_type", "target_id", "analysis_type",
                               "provider_name", "model_name", "scope", "created_at"}:
                # Identity and scope are write-once: history is appended, not rewritten.
                continue
            setattr(model, field_.name, getattr(entity, field_.name))
        self.session.flush()
        return snapshot(AnalysisRun, model)

    def delete(self, workspace_id, id):
        model = self.session.get(AnalysisRunModel, id)
        if model is None or model.workspace_id != workspace_id:
            raise NotFoundError("AnalysisRun 不属于当前 Workspace。")
        self.session.delete(model)
        self.session.flush()


class SQLAnalysisSourceRepository:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def _model(kind):
        try:
            return SOURCE_KINDS[kind][1]
        except KeyError as exc:
            raise DomainError(f"未知的 AnalysisSource 类型：{kind}") from exc

    def list_by_run(self, workspace_id, analysis_run_id):
        groups = {}
        for kind, (attribute, model) in SOURCE_KINDS.items():
            query = select(model).where(model.workspace_id == workspace_id,
                                        model.analysis_run_id == analysis_run_id).order_by(
                model.created_at, model.target_id)
            groups[attribute] = tuple(snapshot(AnalysisSource, row) for row in self.session.scalars(query))
        return AnalysisSourceSet(**groups)

    def add_many(self, kind, workspace_id, analysis_run_id, target_ids):
        model = self._model(kind)
        existing = {row.target_id for row in self.session.scalars(select(model).where(
            model.workspace_id == workspace_id, model.analysis_run_id == analysis_run_id))}
        created = []
        for target_id in target_ids:
            if target_id in existing:
                continue
            entity = AnalysisSource(workspace_id=workspace_id, analysis_run_id=analysis_run_id,
                                    target_id=target_id)
            self.session.add(model(**values(entity)))
            existing.add(target_id)
            created.append(entity)
        self.session.flush()
        return tuple(created)

    def remove_for_run(self, workspace_id, analysis_run_id):
        for _attribute, model in SOURCE_KINDS.values():
            self.session.execute(delete(model).where(model.workspace_id == workspace_id,
                                                     model.analysis_run_id == analysis_run_id))
        self.session.flush()
