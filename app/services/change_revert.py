"""Controlled field revert (Phase 14.4).

A revert is a **new business operation**: it reads a target revision's whitelisted
snapshot, validates the current entity and the field whitelist, previews the changes,
requires explicit confirmation and then calls the existing Service update method.
That method records an audit row labelled ``REVERT`` (with the source revision) in the
same transaction, so old ChangeLog rows are never modified or deleted.

Identity, Workspace ownership, system timestamps, Relationship identity/history,
Evidence original content, Memory provenance and media binaries/links are never
reverted here. Lifecycle restore stays in the existing restore Services.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from app.domain.entities import ENTITY_TYPES, ChangeLogEntry
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import validate_uuid
from app.services.change_history import field_label, format_value
from app.services.change_log_repositories import sql_change_log_repositories
from app.services.repositories import sql_repositories
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

#: The only fields a revert may write, per the reviewed 14.0 contract.
REVERT_WHITELISTS = {
    "PERSON": ("display_name", "real_name", "gender", "birthday", "occupation",
               "organization", "location", "notes"),
    "EVENT": ("title", "event_type", "record_kind", "occurred_on", "started_at",
              "ended_at", "location", "description"),
    "EVIDENCE": ("title", "annotation"),
    "MEMORY": ("content", "kind", "person_id", "valid_from", "valid_until"),
    "WORKSPACE": ("name", "description"),
}
_REQUIRED_TEXT = {
    ("PERSON", "display_name"), ("EVENT", "title"), ("EVIDENCE", "title"),
    ("WORKSPACE", "name"), ("MEMORY", "content"),
}
_DATE_FIELDS = {("PERSON", "birthday"), ("EVENT", "occurred_on")}
_DATETIME_FIELDS = {("EVENT", "started_at"), ("EVENT", "ended_at"),
                    ("MEMORY", "valid_from"), ("MEMORY", "valid_until")}
_UUID_FIELDS = {("MEMORY", "person_id")}


@dataclass(frozen=True)
class RevertField:
    field: str
    label: str
    current_value: object
    target_value: object
    current_text: str
    target_text: str


@dataclass(frozen=True)
class RevertPreview:
    entity_type: str
    entity_id: UUID
    source_revision_no: int
    fields: tuple[RevertField, ...]
    blocked_reason: str | None = None

    @property
    def can_revert(self) -> bool:
        return self.blocked_reason is None

    @property
    def has_changes(self) -> bool:
        return bool(self.fields)

    @property
    def reason(self) -> str:
        if self.blocked_reason:
            return self.blocked_reason
        if not self.fields:
            return "目标版本与当前值一致，没有需要恢复的字段。"
        return ""


@dataclass(frozen=True)
class RevertResult:
    applied: bool
    revision_no: int | None
    entry: ChangeLogEntry | None
    preview: RevertPreview
    message: str


def supports_revert(entity_type: str) -> bool:
    return entity_type in REVERT_WHITELISTS


def _coerce(entity_type: str, field: str, value):
    if value is None:
        return None
    key = (entity_type, field)
    try:
        if key in _DATE_FIELDS:
            return date.fromisoformat(value) if isinstance(value, str) else value
        if key in _DATETIME_FIELDS:
            return datetime.fromisoformat(value) if isinstance(value, str) else value
        if key in _UUID_FIELDS:
            return UUID(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise DomainError(f"目标版本的字段 {field!r} 无法解析。") from exc
    return value


def _required_invalid(entity_type: str, field: str, value) -> bool:
    if (entity_type, field) not in _REQUIRED_TEXT:
        return False
    return not isinstance(value, str) or not value.strip()


class ChangeRevertService:
    """Preview and execute whitelisted field reverts through the existing Services."""

    def __init__(self, sessions, *, people=None, events=None, evidence=None, memories=None,
                 workspaces=None, history=None, changes=None,
                 repositories=sql_change_log_repositories):
        self._sessions = sessions
        self._people = people
        self._events = events
        self._evidence = evidence
        self._memories = memories
        self._workspaces = workspaces
        self._history = history
        self._changes = changes
        self._repositories = repositories

    # ------------------------------------------------------------------ preview
    def preview(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                revision_no: int) -> RevertPreview:
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(entity_id, "entity_id")
        if entity_type not in ENTITY_TYPES:
            raise DomainError(f"未知的实体类型：{entity_type!r}。")
        if isinstance(revision_no, bool) or not isinstance(revision_no, int) \
                or revision_no < 1:
            raise DomainError("revision_no 必须是 >= 1 的整数。")
        if entity_type == "WORKSPACE" and workspace_id != entity_id:
            raise DomainError("Workspace 恢复必须使用该 Workspace 自身作为作用域。")
        if not supports_revert(entity_type):
            return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                 source_revision_no=revision_no, fields=(),
                                 blocked_reason="该实体不支持受控字段恢复。")
        entry = self._entry(workspace_id, entity_type, entity_id, revision_no)
        if entry is None:
            raise NotFoundError("目标历史版本不存在。")
        if entry.snapshot_error is not None:
            return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                 source_revision_no=revision_no, fields=(),
                                 blocked_reason="目标版本的快照无法解析，不能恢复。")
        snapshot = entry.after_snapshot
        if not isinstance(snapshot, dict):
            return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                 source_revision_no=revision_no, fields=(),
                                 blocked_reason="目标版本没有可恢复的字段快照。")
        current, blocked = self._current(workspace_id, entity_type, entity_id)
        if blocked is not None:
            return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                 source_revision_no=revision_no, fields=(),
                                 blocked_reason=blocked)
        fields = []
        for name in REVERT_WHITELISTS[entity_type]:
            if name not in snapshot:
                return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                     source_revision_no=revision_no, fields=(),
                                     blocked_reason="目标版本缺少受控字段快照，不能恢复。")
            target = snapshot.get(name)
            if _required_invalid(entity_type, name, target):
                return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                     source_revision_no=revision_no, fields=(),
                                     blocked_reason=f"目标版本的 {field_label(entity_type, name)} "
                                                    "不是有效值，不能恢复。")
            current_value = getattr(current, name)
            if current_value == target:
                continue
            fields.append(RevertField(
                field=name, label=field_label(entity_type, name),
                current_value=current_value, target_value=target,
                current_text=format_value(entity_type, name, current_value),
                target_text=format_value(entity_type, name, target)))
        if entity_type == "WORKSPACE" and any(item.field == "name" for item in fields):
            target_name = snapshot.get("name")
            if self._workspace_name_taken(target_name, entity_id):
                return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                                     source_revision_no=revision_no, fields=(),
                                     blocked_reason="目标名称已被其他 Workspace 使用，不能恢复。")
        return RevertPreview(entity_type=entity_type, entity_id=entity_id,
                             source_revision_no=revision_no, fields=tuple(fields))

    # ------------------------------------------------------------------- revert
    def revert(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
               revision_no: int, *, confirmed: bool = False) -> RevertResult:
        if confirmed is not True:
            raise DomainError("恢复字段需要用户明确确认。")
        preview = self.preview(workspace_id, entity_type, entity_id, revision_no)
        if preview.blocked_reason is not None:
            raise DomainError(preview.blocked_reason)
        if not preview.fields:
            return RevertResult(applied=False, revision_no=None, entry=None,
                                preview=preview, message=preview.reason)
        if self._changes is None:
            raise DomainError("当前装配没有启用变更审计，无法执行受控恢复。")
        context = self._changes.begin(workspace_id)
        context = replace(context, action_override="REVERT",
                          metadata={"source_revision_no": revision_no})
        self._apply(workspace_id, entity_type, entity_id, preview, context)
        latest = self._latest_revision(workspace_id, entity_type, entity_id)
        entry = self._entry(workspace_id, entity_type, entity_id, latest) \
            if latest is not None else None
        return RevertResult(applied=True, revision_no=latest, entry=entry,
                            preview=preview,
                            message=f"已恢复 {len(preview.fields)} 个字段，"
                                    f"并记录为新的版本 #{latest}。")

    # ---------------------------------------------------------------- internals
    def _entry(self, workspace_id, entity_type, entity_id, revision_no):
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return repositories.change_log.get_revision(
                workspace_id, entity_type, entity_id, revision_no)

    def _latest_revision(self, workspace_id, entity_type, entity_id):
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return repositories.change_log.latest_revision(
                workspace_id, entity_type, entity_id)

    def _workspace_name_taken(self, target_name, entity_id) -> bool:
        with transaction(self._sessions) as session:
            existing = sql_repositories(session).workspaces.get_by_name(
                str(target_name).strip(), include_deleted=True)
            return existing is not None and existing.id != entity_id

    def _current(self, workspace_id, entity_type, entity_id):
        """Return the active current entity or a user-facing blocked reason."""
        try:
            if entity_type == "PERSON":
                entity = self._people.get(workspace_id, entity_id)
            elif entity_type == "EVENT":
                entity = self._events.get(workspace_id, entity_id)
            elif entity_type == "EVIDENCE":
                entity = self._evidence.detail(workspace_id, entity_id).evidence
            elif entity_type == "MEMORY":
                entity = self._memories.get(workspace_id, entity_id)
            elif entity_type == "WORKSPACE":
                entity = self._workspaces.get(workspace_id)
            else:  # pragma: no cover - guarded by supports_revert
                return None, "该实体不支持受控字段恢复。"
        except (NotFoundError, DomainError) as exc:
            return None, f"实体当前不可恢复（{exc}）。"
        if getattr(entity, "deleted_at", None) is not None:
            return None, "实体已归档或删除，请先通过既有恢复功能恢复后再恢复字段。"
        return entity, None

    def _apply(self, workspace_id, entity_type, entity_id, preview, context):
        values = {item.field: _coerce(entity_type, item.field, item.target_value)
                  for item in preview.fields}
        if entity_type == "PERSON":
            self._people.update(workspace_id, entity_id, operation=context, **values)
        elif entity_type == "EVENT":
            self._events.update_event(workspace_id, entity_id, operation=context,
                                      **values)
        elif entity_type == "EVIDENCE":
            self._evidence.update_metadata(workspace_id, entity_id,
                                           operation=context, **values)
        elif entity_type == "MEMORY":
            kwargs = {}
            if "content" in values:
                kwargs["content"] = values["content"]
            if "kind" in values:
                kwargs["kind"] = values["kind"]
            if "person_id" in values:
                if values["person_id"] is None:
                    kwargs["clear_person"] = True
                else:
                    kwargs["person_id"] = values["person_id"]
            if "valid_from" in values:
                if values["valid_from"] is None:
                    kwargs["clear_valid_from"] = True
                else:
                    kwargs["valid_from"] = values["valid_from"]
            if "valid_until" in values:
                if values["valid_until"] is None:
                    kwargs["clear_valid_until"] = True
                else:
                    kwargs["valid_until"] = values["valid_until"]
            self._memories.update(workspace_id, entity_id, operation=context, **kwargs)
        elif entity_type == "WORKSPACE":
            current = self._workspaces.get(workspace_id)
            name = values.get("name", current.name)
            description = values.get("description", current.description)
            self._workspaces.update(workspace_id, name=name, description=description,
                                    operation=context)
        else:  # pragma: no cover
            raise DomainError("该实体不支持受控字段恢复。")
