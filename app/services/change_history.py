"""Read-side revision history: pages, field diffs and operation groups (Phase 14.3).

This module never writes. It formats snapshots for a human reader:
- labels come from the audit matrix (no raw JSON/field names shown to the user);
- values are formatted deterministically (None/bool/date/datetime/UUID/collections/
  long text), while the full formatted text stays available for a detail viewer;
- rows whose payload version is unknown/corrupted are marked unavailable instead of
  being guessed, diffed or reverted;
- pagination is newest-first so the UI never scans every revision.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID

from app.domain.entities import ChangeLogEntry, ENTITY_TYPES
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import validate_uuid
from app.services.change_log_repositories import sql_change_log_repositories
from app.services.change_serialization import COLLECTION_FIELDS, SNAPSHOT_FIELDS
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
LONG_TEXT_LIMIT = 120

ENTITY_LABELS = {
    "WORKSPACE": "Workspace",
    "PERSON": "人物",
    "RELATIONSHIP": "关系",
    "RELATIONSHIP_HISTORY": "关系状态历史",
    "EVENT": "事件",
    "EVIDENCE": "证据",
    "MEDIA": "媒体",
    "MEMORY": "Memory",
}
ACTION_LABELS = {
    "CREATE": "创建",
    "UPDATE": "更新",
    "ARCHIVE": "归档",
    "RESTORE": "恢复",
    "IMPORT": "导入",
    "APPEND_HISTORY": "追加状态历史",
    "ATTACH_EVENT": "关联事件",
    "DETACH_EVENT": "解除事件关联",
    "REVERT": "恢复字段",
}
CREATE_LIKE = ("CREATE", "IMPORT", "APPEND_HISTORY")

FIELD_LABELS = {
    "WORKSPACE": {"name": "名称", "description": "描述"},
    "PERSON": {
        "display_name": "显示名称", "real_name": "真实姓名", "gender": "性别",
        "birthday": "生日", "occupation": "职业", "organization": "组织",
        "location": "所在地", "notes": "备注", "aliases": "别称", "images": "图片引用",
    },
    "RELATIONSHIP": {
        "person_a_id": "端点 A", "person_b_id": "端点 B",
        "relationship_type": "关系类型", "directionality": "方向",
    },
    "RELATIONSHIP_HISTORY": {
        "relationship_id": "所属关系", "state": "状态", "effective_at": "生效时间",
        "note": "备注", "event_id": "关联事件",
    },
    "EVENT": {
        "title": "标题", "event_type": "事件类型", "record_kind": "记录性质",
        "occurred_on": "发生日期", "started_at": "开始时间", "ended_at": "结束时间",
        "location": "地点", "description": "描述", "participants": "参与人",
    },
    "EVIDENCE": {"title": "标题", "annotation": "注释", "media": "附件"},
    "MEDIA": {"sha256": "内容指纹", "mime_type": "类型", "size_bytes": "大小（字节）",
              "width": "宽度", "height": "高度"},
    "MEMORY": {"person_id": "所属人物", "kind": "类型", "content": "内容",
               "source_type": "来源类型", "source_analysis_id": "来源分析",
               "valid_from": "生效起", "valid_until": "生效止"},
}


@dataclass(frozen=True)
class FieldChange:
    field: str
    label: str
    before_text: str
    after_text: str


@dataclass(frozen=True)
class RevisionView:
    entry: ChangeLogEntry
    action_label: str
    changes: tuple[FieldChange, ...]
    summary: str

    @property
    def snapshot_unavailable(self) -> bool:
        return self.entry.snapshot_error is not None

    @property
    def revision_no(self) -> int:
        return self.entry.revision_no


@dataclass(frozen=True)
class HistoryPage:
    entity_type: str
    entity_id: UUID
    revisions: tuple[RevisionView, ...]
    has_more: bool
    offset: int

    @property
    def has_history(self) -> bool:
        return bool(self.revisions)

    @property
    def legacy_hint(self) -> str | None:
        """Shown when the oldest visible revision is not a creation: history began
        after the entity was created (Phase 14 has no backfill)."""
        if not self.revisions or self.offset:
            return None
        oldest = self.revisions[-1].entry
        if oldest.action not in CREATE_LIKE:
            return ("该记录在 Phase 14 之前创建，早期历史不可恢复；"
                    "以下为启用审计之后的变化。")
        return None


@dataclass(frozen=True)
class OperationRow:
    entity_type: str
    entity_label: str
    entity_id: UUID
    action_label: str
    revision_no: int


@dataclass(frozen=True)
class OperationView:
    operation_id: UUID
    recorded_at: datetime
    rows: tuple[OperationRow, ...]


def field_label(entity_type: str, field: str) -> str:
    return FIELD_LABELS.get(entity_type, {}).get(field, f"未识别字段（{field}）")


def format_value(entity_type: str, field: str, value) -> str:
    """Deterministic human text; never a raw JSON dump."""
    if value is None:
        return "（空）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value if value.strip() else "（空文本）"
    if isinstance(value, (list, tuple)):
        return _format_collection(field, value)
    if isinstance(value, dict):
        return _format_collection(field, [value])
    return str(value)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _short_uuid(value) -> str:
    return str(value)[:8]


def _format_collection(field: str, items) -> str:
    if not items:
        return "（无）"
    rendered = []
    for item in items:
        if not isinstance(item, dict):
            rendered.append(str(item))
            continue
        if field == "aliases":
            alias = item.get("alias") or "（未命名别称）"
            kind = item.get("alias_type")
            note = item.get("note")
            text = f"{alias}（{kind}）" if kind else str(alias)
            if note:
                text += f"｜{note}"
            rendered.append(text)
        elif field == "images":
            rendered.append(f"{_short_uuid(item.get('media_id'))}"
                            f"（{item.get('role')}）")
        elif field == "participants":
            role = item.get("role")
            note = item.get("note")
            text = _short_uuid(item.get("person_id"))
            if role:
                text += f"（{role}）"
            if note:
                text += f"｜{note}"
            rendered.append(text)
        elif field == "media":
            rendered.append(f"{_short_uuid(item.get('media_id'))}"
                            f"（{item.get('role') or '无角色'}）")
        else:
            rendered.append("、".join(f"{key}={value}" for key, value in item.items()))
    return "；".join(rendered)


def _field_value(snapshot, field):
    return None if snapshot is None else snapshot.get(field)


def _changes_from_entry(entity_type: str, entry: ChangeLogEntry) -> tuple[FieldChange, ...]:
    if entry.snapshot_error is not None:
        return ()
    before, after = entry.before_snapshot, entry.after_snapshot
    if before is None:
        if after is None:
            return ()
        return tuple(FieldChange(field, field_label(entity_type, field),
                                 "（新增）", format_value(entity_type, field, value))
                     for field, value in after.items())
    changes = []
    for field in entry.changed_fields:
        changes.append(FieldChange(field, field_label(entity_type, field),
                                   format_value(entity_type, field,
                                                _field_value(before, field)),
                                   format_value(entity_type, field,
                                                _field_value(after, field))))
    return tuple(changes)


def _view(entry: ChangeLogEntry) -> RevisionView:
    changes = _changes_from_entry(entry.entity_type, entry)
    action_label = ACTION_LABELS.get(entry.action, entry.action)
    if entry.snapshot_error is not None:
        summary = f"{action_label}（快照不可用）"
    elif changes:
        labels = "、".join(change.label for change in changes)
        summary = f"{action_label}：{labels}"
    else:
        summary = action_label
    return RevisionView(entry=entry, action_label=action_label, changes=changes,
                        summary=summary)


def _check_entity(entity_type: str, entity_id: UUID) -> None:
    if entity_type not in ENTITY_TYPES:
        raise DomainError(f"未知的实体类型：{entity_type!r}。")
    validate_uuid(entity_id, "entity_id")


def _page(limit: int | None, offset: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DomainError("offset 必须是 >= 0 的整数。")
    if limit is None:
        limit = DEFAULT_PAGE_SIZE
    if isinstance(limit, bool) or not isinstance(limit, int) or \
            not 1 <= limit <= MAX_PAGE_SIZE:
        raise DomainError(f"limit 必须是 1..{MAX_PAGE_SIZE} 的整数。")
    return limit, offset


class ChangeHistoryService:
    """Read-only history views for one Workspace (archived entities included)."""

    def __init__(self, sessions, *, repositories=sql_change_log_repositories):
        self._sessions = sessions
        self._repositories = repositories

    def entity_history(self, workspace_id: UUID, entity_type: str, entity_id: UUID, *,
                       limit: int | None = DEFAULT_PAGE_SIZE,
                       offset: int = 0) -> HistoryPage:
        validate_uuid(workspace_id, "workspace_id")
        _check_entity(entity_type, entity_id)
        limit, offset = _page(limit, offset)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            rows = repositories.change_log.list_by_entity(
                workspace_id, entity_type, entity_id, limit=limit + 1, offset=offset,
                newest_first=True)
        has_more = len(rows) > limit
        return HistoryPage(entity_type=entity_type, entity_id=entity_id,
                           revisions=tuple(_view(row) for row in rows[:limit]),
                           has_more=has_more, offset=offset)

    def parent_history(self, workspace_id: UUID, parent_type: str, parent_id: UUID, *,
                       limit: int | None = DEFAULT_PAGE_SIZE,
                       offset: int = 0) -> HistoryPage:
        validate_uuid(workspace_id, "workspace_id")
        _check_entity(parent_type, parent_id)
        limit, offset = _page(limit, offset)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            rows = repositories.change_log.list_by_parent(
                workspace_id, parent_type, parent_id, limit=limit + 1, offset=offset,
                newest_first=True)
        has_more = len(rows) > limit
        return HistoryPage(entity_type=parent_type, entity_id=parent_id,
                           revisions=tuple(_view(row) for row in rows[:limit]),
                           has_more=has_more, offset=offset)

    def revision(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                 revision_no: int) -> RevisionView:
        validate_uuid(workspace_id, "workspace_id")
        _check_entity(entity_type, entity_id)
        if isinstance(revision_no, bool) or not isinstance(revision_no, int) \
                or revision_no < 1:
            raise DomainError("revision_no 必须是 >= 1 的整数。")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            entry = repositories.change_log.get_revision(
                workspace_id, entity_type, entity_id, revision_no)
            if entry is None:
                raise NotFoundError("该实体没有指定的历史版本。")
            return _view(entry)

    def operation(self, workspace_id: UUID, operation_id: UUID) -> OperationView:
        validate_uuid(workspace_id, "workspace_id")
        validate_uuid(operation_id, "operation_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            entries = repositories.change_log.list_by_operation(workspace_id, operation_id)
        if not entries:
            raise NotFoundError("该操作没有可读取的审计记录。")
        rows = tuple(OperationRow(
            entity_type=entry.entity_type,
            entity_label=ENTITY_LABELS.get(entry.entity_type, entry.entity_type),
            entity_id=entry.entity_id,
            action_label=ACTION_LABELS.get(entry.action, entry.action),
            revision_no=entry.revision_no) for entry in entries)
        return OperationView(operation_id=operation_id,
                             recorded_at=entries[0].recorded_at, rows=rows)

    def diff(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
             from_revision: int, to_revision: int) -> tuple[FieldChange, ...]:
        self._check_revisions(from_revision, to_revision)
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            before = repositories.change_log.get_revision(
                workspace_id, entity_type, entity_id, from_revision)
            after = repositories.change_log.get_revision(
                workspace_id, entity_type, entity_id, to_revision)
        if before is None or after is None:
            raise NotFoundError("比较的历史版本不存在。")
        if before.snapshot_error is not None or after.snapshot_error is not None:
            raise DomainError("历史快照无法解析，不能生成差异。")
        before_values = before.after_snapshot or {}
        after_values = after.after_snapshot or {}
        changes = []
        fields = list(SNAPSHOT_FIELDS.get(entity_type, ()))
        for field in COLLECTION_FIELDS.get(entity_type, ()):
            if field not in fields:
                fields.append(field)
        extra = [field for field in after_values if field not in fields]
        for field in fields + extra:
            left = before_values.get(field) if field in before_values else None
            right = after_values.get(field) if field in after_values else None
            if left != right:
                changes.append(FieldChange(field, field_label(entity_type, field),
                                           format_value(entity_type, field, left),
                                           format_value(entity_type, field, right)))
        return tuple(changes)

    def adjacent_diff(self, workspace_id: UUID, entity_type: str, entity_id: UUID,
                      revision_no: int) -> tuple[FieldChange, ...]:
        if isinstance(revision_no, bool) or not isinstance(revision_no, int) \
                or revision_no < 2:
            raise DomainError("第一个版本没有相邻的前一个版本。")
        return self.diff(workspace_id, entity_type, entity_id, revision_no - 1,
                         revision_no)

    @staticmethod
    def _check_revisions(from_revision: int, to_revision: int) -> None:
        for value in (from_revision, to_revision):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DomainError("revision_no 必须是 >= 1 的整数。")
