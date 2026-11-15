"""Workspace export: a portable, readable dump — explicitly *not* a backup.

An export exists to look at the data elsewhere or to move it between tools. It is a
JSON document plus copies of the referenced media, and it is deliberately **not
restorable over a live database**: `BackupService` owns recovery, with a consistent
snapshot and a manifest that can be verified.

Privacy rules baked into the format:

* credential material cannot appear: the exported records have no credential fields,
  and the LLM provider configuration (which *does* hold `credential_ref`) is not
  exported at all;
* an analysis contributes metadata only — provider, model, status, timings, scope and
  the ids of the facts it read. The raw answer and the context snapshot (both may
  contain private text) are included **only** when the caller explicitly asks.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from uuid import UUID

from app.domain.validation import utc_now
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "relationship-intelligence-workspace-export"
EXPORT_VERSION = 1
README_NAME = "README.txt"
JSON_NAME = "workspace.json"
MEDIA_DIR = "media"
README_TEXT = """Relationship Intelligence System — Workspace Export
=====================================================

这是一个用于查看与搬运的导出，不是备份，也不能直接覆盖生产数据库。
要完整恢复，请使用 Backup（database.sqlite3 + media + manifest.json）并运行恢复校验。

- workspace.json : 本工作区的人物、别名、关系、关系历史、事件、参与人、证据、
                   证据附件、Memory 与分析元数据。
- media/         : 被引用的媒体文件副本。

隐私：导出中不包含任何 API Key 或凭据内容。分析的原始回答与上下文快照默认不导出，
只有导出时显式选择才会包含。
"""


@dataclass(frozen=True, slots=True)
class ExportResult:
    ok: bool
    directory: Path
    json_path: Path | None = None
    media_dir: Path | None = None
    workspace_id: UUID | None = None
    counts: dict = field(default_factory=dict)
    media_file_count: int = 0
    media_total_bytes: int = 0
    error: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"导出失败：{self.error}"
        return (f"已导出 {self.counts.get('person', 0)} 人物 / {self.counts.get('event', 0)} 事件 / "
                f"{self.counts.get('evidence', 0)} 证据 / {self.counts.get('memory', 0)} Memory；"
                f"媒体 {self.media_file_count} 个文件（{self.media_total_bytes} 字节）")


def _jsonable(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _record(entity, *, skip=()) -> dict:
    data = {name: _jsonable(getattr(entity, name)) for name in entity.__dataclass_fields__
            if name not in skip}
    return data


class WorkspaceExportService:
    """Read-only export of one workspace (facts + memories + analysis metadata)."""

    def __init__(self, config, sessions, *, clock=utc_now):
        self._config = config
        self._sessions = sessions
        self._clock = clock

    # ------------------------------------------------------------------ helpers
    def _store(self):
        from app.storage.media_store import MediaStore
        return MediaStore(self._config.storage_paths.data_dir)

    def _collect(self, repositories, workspace_id):
        people = repositories.people.list(workspace_id, include_deleted=True)
        aliases = [alias for person in people
                   for alias in repositories.aliases.list(workspace_id, person.id)]
        relationships = repositories.relationships.list_by_workspace(workspace_id,
                                                                    include_deleted=True)
        histories = repositories.histories.list_by_workspace(workspace_id)
        events = repositories.events.list_by_workspace(workspace_id, include_deleted=True)
        participants = [item for event in events
                        for item in repositories.participants.list_by_event(workspace_id, event.id)]
        evidence = [item for event in events
                    for item in repositories.evidence.list_by_event(workspace_id, event.id,
                                                                    include_deleted=True)]
        links = [link for item in evidence
                 for link in repositories.evidence_media.list_by_evidence(workspace_id, item.id)]
        media = repositories.media.list(workspace_id, include_deleted=True)
        memories = repositories.memories.list_by_workspace(workspace_id, workspace_scoped_only=False,
                                                          include_deleted=True, include_expired=True)
        runs = repositories.runs.list_by_workspace(workspace_id)
        sources = {run.id: repositories.sources.list_by_run(workspace_id, run.id) for run in runs}
        return {"people": people, "aliases": aliases, "relationships": relationships,
                "histories": histories, "events": events, "participants": participants,
                "evidence": evidence, "evidence_media": links, "media": media,
                "memories": memories, "runs": runs, "sources": sources}

    @staticmethod
    def _analysis_record(run, sources, *, include_result, include_snapshot):
        record = {"id": _jsonable(run.id), "target_type": run.target_type,
                  "target_id": _jsonable(run.target_id), "analysis_type": run.analysis_type,
                  "provider_name": run.provider_name, "model_name": run.model_name,
                  "status": run.status, "created_at": _jsonable(run.created_at),
                  "started_at": _jsonable(run.started_at), "completed_at": _jsonable(run.completed_at),
                  "duration_ms": run.duration_ms, "error_message": run.error_message,
                  "scope": json.loads(run.scope) if run.scope else {},
                  "sources": {"person": [_jsonable(item.target_id) for item in sources.people],
                              "relationship": [_jsonable(item.target_id)
                                               for item in sources.relationships],
                              "event": [_jsonable(item.target_id) for item in sources.events],
                              "evidence": [_jsonable(item.target_id) for item in sources.evidence]},
                  "result_included": bool(include_result and run.result),
                  "context_snapshot_included": bool(include_snapshot and run.context_snapshot)}
        # The raw answer and the context snapshot may quote private text; they are
        # exported only when the caller explicitly asks for them.
        if include_result and run.result:
            record["result"] = run.result
        if include_snapshot and run.context_snapshot:
            record["context_snapshot"] = run.context_snapshot
        return record

    @staticmethod
    def _write_media(files: Iterable, media_dir: Path, store) -> tuple[int, int, list]:
        """Copy referenced media; a file-level problem is recorded, never fatal."""
        media_dir.mkdir(parents=True, exist_ok=True)
        count = total = 0
        entries = []
        for model in files:
            try:
                source = store.path_for(model.storage_path)
                if not source.is_file():
                    entries.append({"storage_path": model.storage_path, "copied": False,
                                    "reason": "源文件不存在"})
                    continue
                data = source.read_bytes()
            except Exception as exc:            # invalid path, unreadable file, ...
                entries.append({"storage_path": model.storage_path, "copied": False,
                                "reason": f"无法读取（{type(exc).__name__}）"})
                continue
            target = media_dir / Path(model.storage_path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            count += 1
            total += len(data)
            entries.append({"storage_path": model.storage_path, "relative_path": target.name,
                            "size": len(data), "sha256": model.sha256, "copied": True})
        return count, total, entries

    # ------------------------------------------------------------------- export
    def export(self, workspace_id, destination: Path, *, include_analysis_results: bool = False,
               include_analysis_snapshots: bool = False) -> ExportResult:
        target = Path(destination)
        try:
            if target.exists() and any(target.iterdir()):
                return ExportResult(False, target, error="导出目录已存在且非空，请换一个空目录。")
            with transaction(self._sessions) as session:
                repositories = sql_analysis_repositories(session)
                workspace = workspace_required(repositories, workspace_id)
                data = self._collect(repositories, workspace_id)
                document = {
                    "format": EXPORT_FORMAT,
                    "format_version": EXPORT_VERSION,
                    "exported_at": _jsonable(self._clock()),
                    "is_backup": False,
                    "workspace": _record(workspace),
                    "person": [_record(item) for item in data["people"]],
                    "person_alias": [_record(item) for item in data["aliases"]],
                    "relationship": [_record(item) for item in data["relationships"]],
                    "relationship_history": [_record(item) for item in data["histories"]],
                    "event": [_record(item) for item in data["events"]],
                    "event_participant": [_record(item) for item in data["participants"]],
                    "evidence": [_record(item) for item in data["evidence"]],
                    "evidence_media": [_record(item) for item in data["evidence_media"]],
                    "media": [_record(item) for item in data["media"]],
                    "memory": [_record(item) for item in data["memories"]],
                    "analysis": [self._analysis_record(item, data["sources"][item.id],
                                                       include_result=include_analysis_results,
                                                       include_snapshot=include_analysis_snapshots)
                                 for item in data["runs"]],
                    "privacy": {"credentials_included": False,
                                "analysis_results_included": bool(include_analysis_results),
                                "analysis_snapshots_included": bool(include_analysis_snapshots)},
                }
                media_files = list(data["media"])
            target.mkdir(parents=True, exist_ok=True)
            media_dir = target / MEDIA_DIR
            count, total, media_entries = self._write_media(media_files, media_dir, self._store())
            document["media_files"] = media_entries
            json_path = target / JSON_NAME
            json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            (target / README_NAME).write_text(README_TEXT, encoding="utf-8")
            counts = {name: len(document[key]) for name, key in
                      (("person", "person"), ("alias", "person_alias"),
                       ("relationship", "relationship"), ("history", "relationship_history"),
                       ("event", "event"), ("participant", "event_participant"),
                       ("evidence", "evidence"), ("evidence_media", "evidence_media"),
                       ("media", "media"), ("memory", "memory"), ("analysis", "analysis"))}
            return ExportResult(True, target, json_path=json_path, media_dir=media_dir,
                                workspace_id=workspace_id, counts=counts, media_file_count=count,
                                media_total_bytes=total)
        except Exception as exc:
            logger.warning("Workspace export failed: type=%s", type(exc).__name__)
            from app.domain.errors import DomainError
            message = (str(exc) if isinstance(exc, (DomainError, ValueError, OSError))
                       else "导出失败，请查看本地日志。")
            return ExportResult(False, target, error=message)
