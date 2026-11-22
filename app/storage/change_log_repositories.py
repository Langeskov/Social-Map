"""Append-only ChangeLog storage adapter.

One Session owned by the caller; no commit/rollback here. Snapshot columns are
canonical JSON text, decoded through the domain contract so the adapter never
invents a format. No update/delete method exists: the ORM guards would reject
such a write anyway.
"""
import json
from uuid import UUID

from sqlalchemy import func, select

from app.domain.entities import (ChangeLogEntry, decode_json_object, decode_snapshot,
                               encode_changed_fields)
from app.domain.errors import DomainError
from app.storage.models import ChangeLogModel

__all__ = ["SQLChangeLogRepository"]


def _decode_changed_fields(text: str) -> tuple[str, ...]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DomainError("changed_fields 不是合法 JSON。") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DomainError("changed_fields 必须是字符串数组。")
    return tuple(value)


def _to_domain(model) -> ChangeLogEntry | None:
    if model is None:
        return None
    try:
        changed_fields = _decode_changed_fields(model.changed_fields)
        before = decode_snapshot(model.payload_version, model.before_snapshot)
        after = decode_snapshot(model.payload_version, model.after_snapshot)
        metadata = (None if model.metadata_json is None else
                    decode_json_object(model.metadata_json, where="metadata"))
        snapshot_error = None
    except DomainError:
        # A row written by a future/incompatible payload version (or a corrupted
        # JSON blob) stays readable as history metadata; the UI must not crash and
        # must not pretend it can diff/revert such a snapshot.
        changed_fields, before, after, metadata = (), None, None, None
        snapshot_error = "无法解析此版本快照"
    return ChangeLogEntry(
        id=model.id,
        workspace_id=model.workspace_id,
        entity_type=model.entity_type,
        entity_id=model.entity_id,
        parent_entity_type=model.parent_entity_type,
        parent_entity_id=model.parent_entity_id,
        action=model.action,
        operation_id=model.operation_id,
        revision_no=model.revision_no,
        changed_fields=changed_fields,
        before_snapshot=before,
        after_snapshot=after,
        payload_version=model.payload_version,
        metadata=metadata,
        recorded_at=model.recorded_at,
        snapshot_error=snapshot_error,
    )


def _values(entry: ChangeLogEntry) -> dict:
    return {"id": entry.id, "workspace_id": entry.workspace_id,
            "entity_type": entry.entity_type, "entity_id": entry.entity_id,
            "parent_entity_type": entry.parent_entity_type,
            "parent_entity_id": entry.parent_entity_id,
            "action": entry.action, "operation_id": entry.operation_id,
            "revision_no": entry.revision_no,
            "changed_fields": encode_changed_fields(entry.changed_fields),
            "before_snapshot": entry.encode_before(),
            "after_snapshot": entry.encode_after(),
            "payload_version": entry.payload_version,
            "metadata_json": entry.encode_metadata(),
            "recorded_at": entry.recorded_at}


class SQLChangeLogRepository:
    def __init__(self, session):
        self.session = session

    def _query(self, workspace_id: UUID):
        return select(ChangeLogModel).where(ChangeLogModel.workspace_id == workspace_id)

    def add(self, entry: ChangeLogEntry) -> ChangeLogEntry:
        model = ChangeLogModel(**_values(entry))
        self.session.add(model)
        self.session.flush()
        return _to_domain(model)

    def get(self, workspace_id: UUID, entry_id: UUID) -> ChangeLogEntry | None:
        return _to_domain(self.session.scalar(
            self._query(workspace_id).where(ChangeLogModel.id == entry_id)))

    def list_by_entity(self, workspace_id: UUID, entity_type: str, entity_id: UUID, *,
                       limit: int | None = None, offset: int = 0,
                       newest_first: bool = False) -> list[ChangeLogEntry]:
        query = self._query(workspace_id).where(
            ChangeLogModel.entity_type == entity_type,
            ChangeLogModel.entity_id == entity_id)
        if newest_first:
            query = query.order_by(ChangeLogModel.revision_no.desc(),
                                   ChangeLogModel.id.desc())
        else:
            query = query.order_by(ChangeLogModel.revision_no, ChangeLogModel.id)
        return self._list(query, limit=limit, offset=offset)

    def list_by_parent(self, workspace_id: UUID, parent_type: str, parent_id: UUID, *,
                       limit: int | None = None, offset: int = 0) -> list[ChangeLogEntry]:
        query = self._query(workspace_id).where(
            ChangeLogModel.parent_entity_type == parent_type,
            ChangeLogModel.parent_entity_id == parent_id)
        return self._list(query.order_by(ChangeLogModel.recorded_at,
                                         ChangeLogModel.revision_no, ChangeLogModel.id),
                          limit=limit, offset=offset)

    def list_by_operation(self, workspace_id: UUID,
                          operation_id: UUID) -> list[ChangeLogEntry]:
        query = self._query(workspace_id).where(
            ChangeLogModel.operation_id == operation_id).order_by(
            ChangeLogModel.recorded_at, ChangeLogModel.revision_no, ChangeLogModel.id)
        return [_to_domain(model) for model in self.session.scalars(query)]

    def get_revision(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                     revision_no: int) -> ChangeLogEntry | None:
        return _to_domain(self.session.scalar(
            self._query(workspace_id).where(
                ChangeLogModel.entity_type == entity_type,
                ChangeLogModel.entity_id == entity_id,
                ChangeLogModel.revision_no == revision_no)))

    def latest_revision(self, workspace_id: UUID, entity_type: str,
                        entity_id: UUID) -> int | None:
        value = self.session.scalar(
            select(func.max(ChangeLogModel.revision_no)).where(
                ChangeLogModel.workspace_id == workspace_id,
                ChangeLogModel.entity_type == entity_type,
                ChangeLogModel.entity_id == entity_id))
        return None if value is None else int(value)

    def _list(self, query, *, limit, offset) -> list[ChangeLogEntry]:
        if offset:
            query = query.offset(int(offset))
        if limit is not None:
            query = query.limit(int(limit))
        return [_to_domain(model) for model in self.session.scalars(query)]
