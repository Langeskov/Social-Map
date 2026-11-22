"""Change capture: one operation context, one writer, same transaction as the write.

The writer never opens its own Session and never commits: it receives the caller's
session inside ``write_operation`` so the business rows and their audit rows are
one atomic unit. It validates the entity/parent references (workspace-scoped
existence), derives the next revision number, serializes nothing itself (callers
pass whitelisted snapshots from ``change_snapshots``) and appends exactly one row
per recorded entity change.

No-op rule: UPDATE/ATTACH/DETACH with identical before/after snapshots record
nothing; lifecycle actions (ARCHIVE/RESTORE) and creations always record when the
service actually changed state.
"""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from app.domain.entities import (ALLOWED_ACTIONS, ENTITY_TYPES, PAYLOAD_VERSION,
                                 ChangeLogEntry)
from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.services.change_log_repositories import sql_change_log_repositories
from app.services.change_serialization import ChangeSnapshotSerializer, snapshot_field_names
from app.storage.models import (AnalysisRunModel, ChangeLogModel, EvidenceModel,
                                EventModel, MediaModel, MemoryModel, PersonModel,
                                RelationshipHistoryModel, RelationshipModel,
                                WorkspaceModel)

#: entity_type -> mapped model used only for reference validation.
_ENTITY_MODELS = {
    "WORKSPACE": WorkspaceModel,
    "PERSON": PersonModel,
    "RELATIONSHIP": RelationshipModel,
    "RELATIONSHIP_HISTORY": RelationshipHistoryModel,
    "EVENT": EventModel,
    "EVIDENCE": EvidenceModel,
    "MEDIA": MediaModel,
    "MEMORY": MemoryModel,
}
_CREATE_LIKE = ("CREATE", "IMPORT", "APPEND_HISTORY")
_NOOP_SKIP_ACTIONS = ("UPDATE", "ATTACH_EVENT", "DETACH_EVENT")

SERIALIZER = ChangeSnapshotSerializer()


@dataclass(frozen=True)
class ChangeOperationContext:
    """Identity of one user operation; all audit rows of the operation share it.

    ``action_override``/``metadata`` are used only by the controlled revert
    (Phase 14.4): the underlying Service still performs a normal domain update, but
    the audit row is labelled REVERT and carries its source revision.
    """

    workspace_id: UUID
    operation_id: UUID
    recorded_at: datetime
    action_override: str | None = None
    metadata: dict | None = None


def _ensure_reference(session, workspace_id, entity_type, entity_id, *, label):
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise DomainError(f"未知的实体类型：{entity_type!r}。")
    validate_uuid(entity_id, "entity_id")
    if entity_type == "WORKSPACE":
        row = session.get(WorkspaceModel, entity_id)
        if row is None:
            raise DomainError(f"{label} Workspace 不存在。")
        return
    found = session.scalar(select(model).where(model.workspace_id == workspace_id,
                                               model.id == entity_id))
    if found is None:
        raise DomainError(f"{label} 不属于当前 Workspace 或不存在。")


class ChangeLogWriter:
    """Append audit rows inside the caller's write transaction."""

    def __init__(self, *, repositories=sql_change_log_repositories, clock=utc_now):
        self._repositories = repositories
        self._clock = clock

    def begin(self, workspace_id: UUID) -> ChangeOperationContext:
        validate_uuid(workspace_id, "workspace_id")
        return ChangeOperationContext(workspace_id=workspace_id, operation_id=uuid4(),
                                      recorded_at=normalize_datetime(
                                          self._clock(), "clock"))

    def record(self, session, context: ChangeOperationContext, *, entity_type: str,
               action: str, entity, before: dict | None = None,
               after: dict | None = None, parent=None,
               metadata: dict | None = None) -> ChangeLogEntry | None:
        """Append one ChangeLog row; return None for a genuine no-op update."""
        if session is None:
            raise DomainError("ChangeLogWriter 必须在业务写事务内使用。")
        if not isinstance(context, ChangeOperationContext):
            raise DomainError("ChangeLogWriter 需要显式 ChangeOperationContext。")
        if entity_type not in ENTITY_TYPES:
            raise DomainError(f"未知的实体类型：{entity_type!r}。")
        effective_action = action
        if context.action_override and action == "UPDATE":
            effective_action = context.action_override
        if effective_action not in ALLOWED_ACTIONS[entity_type]:
            raise DomainError(f"{entity_type} 不允许操作 {effective_action}。")
        if effective_action in _NOOP_SKIP_ACTIONS and before is not None and after is not None \
                and before == after:
            return None
        entity_id = getattr(entity, "id", None)
        validate_uuid(entity_id, "entity_id")
        entity_workspace = getattr(entity, "workspace_id", None)
        if entity_workspace is not None and entity_workspace != context.workspace_id:
            raise DomainError("实体不属于当前 Workspace，拒绝记录。")
        if entity_type == "WORKSPACE" and entity_id != context.workspace_id:
            raise DomainError("Workspace 审计行必须使用该 Workspace 自身作为作用域。")
        if action in ("CREATE", "IMPORT") and before is not None:
            raise DomainError(f"{action} 不应有修改前快照。")
        if action in ("CREATE", "IMPORT", "APPEND_HISTORY") and after is None:
            raise DomainError(f"{action} 必须有修改后快照。")
        if effective_action not in _CREATE_LIKE and (before is None or after is None):
            raise DomainError(f"{effective_action} 必须同时记录修改前后快照。")
        _ensure_reference(session, context.workspace_id, entity_type, entity_id,
                          label="实体")
        if parent is not None:
            parent_type, parent_id = parent
            if parent_type not in ENTITY_TYPES:
                raise DomainError(f"未知的父实体类型：{parent_type!r}。")
            _ensure_reference(session, context.workspace_id, parent_type, parent_id,
                              label="父实体")
        if effective_action in _CREATE_LIKE:
            fields = tuple(after or ())
        else:
            fields = SERIALIZER.changed_fields(entity_type, before, after)
        repositories = self._repositories(session)
        last = repositories.change_log.latest_revision(
            context.workspace_id, entity_type, entity_id)
        revision_no = 1 if last is None else last + 1
        parent_type, parent_id = (None, None) if parent is None else parent
        merged_metadata = dict(context.metadata or {})
        merged_metadata.update(metadata or {})
        entry = ChangeLogEntry(
            workspace_id=context.workspace_id, entity_type=entity_type,
            entity_id=entity_id, action=effective_action,
            operation_id=context.operation_id,
            revision_no=revision_no, changed_fields=fields,
            before_snapshot=None if effective_action in _CREATE_LIKE else before,
            after_snapshot=after, payload_version=PAYLOAD_VERSION,
            recorded_at=context.recorded_at,
            parent_entity_type=parent_type, parent_entity_id=parent_id,
            metadata=merged_metadata or None)
        return repositories.change_log.add(entry)
