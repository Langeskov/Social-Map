"""DataIntegrityService: a read-only audit of stored facts, provenance and files.

This module answers one question - "is what is stored still consistent?" - and it
never answers it by changing anything. ``check()`` opens exactly one read
transaction, issues SELECT/PRAGMA statements only, and returns a frozen report:

* no INSERT/UPDATE/DELETE and no DDL;
* no migration is run and no search projection is refreshed;
* no Analysis, Memory or other fact is created as a side effect;
* no orphan file is deleted, moved or rewritten - ``ORPHAN_MEDIA_FILE`` is a
  report, never a cleanup.

Frozen codes
    ERROR   : FOREIGN_KEY_VIOLATION, PERSON_WORKSPACE_MISMATCH,
              RELATIONSHIP_WORKSPACE_MISMATCH, RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH,
              EVENT_PARTICIPANT_WORKSPACE_MISMATCH, EVIDENCE_WORKSPACE_MISMATCH,
              EVIDENCE_MEDIA_WORKSPACE_MISMATCH, PERSON_MEDIA_WORKSPACE_MISMATCH,
              MEDIA_WORKSPACE_MISMATCH, PERSON_MEDIA_DANGLING,
              EVIDENCE_MEDIA_DANGLING, MEDIA_FILE_MISSING, MEDIA_FILE_SIZE_MISMATCH,
              MEDIA_FILE_HASH_MISMATCH, ANALYSIS_SOURCE_DANGLING,
              MEMORY_SOURCE_INVALID, MEMORY_STATE_INVALID
    WARNING : MEDIA_UNREFERENCED, ORPHAN_MEDIA_FILE, MEMORY_EXPIRED,
              MEDIA_STORE_UNAVAILABLE, ANALYSIS_RUN_STATE_INVALID

    Beyond the frozen list, two families are audited the same way because a wrong
    workspace there would leak a fact across a workspace boundary rather than only
    misplace a row: ``ANALYSIS_{PERSON,RELATIONSHIP,EVENT,EVIDENCE}_SOURCE_WORKSPACE_MISMATCH``
    (Phase 8 provenance against its run) and
    ``SEARCH_{PERSON,ALIAS,EVENT,EVIDENCE}_WORKSPACE_MISMATCH`` (Phase 7 projection
    against the fact it projects). Both are ERRORs and both are expected to be
    empty; they are reported, never repaired.

Privacy
    A message or detail never carries Memory/Evidence body text, a credential or
    anything but an identifier, a relative storage path and a count. The audit is
    safe to print, log and attach to a backup manifest.

Why some checks are structurally empty
    The schema declares composite foreign keys ``(workspace_id, parent_id) ->
    parent(workspace_id, id)`` on every workspace-scoped child table, and SQLite
    enforces them once ``PRAGMA foreign_keys=ON`` (which the engine sets on every
    connection). A row whose ``workspace_id`` disagrees with an *existing* parent
    row therefore cannot be inserted, so the eight ``*_WORKSPACE_MISMATCH`` queries
    are real self-joins that are expected to return nothing on a database only ever
    written through these constraints. They are not assertions of that fact: the
    parent is looked up by identifier alone, so a raw connection with
    ``foreign_keys=OFF`` that inserted a mismatched child - or a parent row that
    never existed - is reported. Two of the eight are weaker than the rest and are
    documented in ``_check_workspace_mismatches``: a database whose constraints were
    never bypassed can make ``PERSON_WORKSPACE_MISMATCH`` and
    ``MEDIA_WORKSPACE_MISMATCH`` fire only when the parent Workspace row itself is
    missing, because ``workspace`` is keyed by the very id the child copies.
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection, Engine

from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.storage.models import (
    AnalysisEventSourceModel, AnalysisEvidenceSourceModel, AnalysisPersonSourceModel,
    AnalysisRelationshipSourceModel, AnalysisRunModel, EventModel, EventParticipantModel,
    EvidenceMediaModel, EvidenceModel, MediaModel, MemoryModel, PersonAliasModel,
    PersonMediaModel, PersonModel, RelationshipHistoryModel, RelationshipModel,
    SearchDocumentModel, WorkspaceModel,
)

ERROR = "ERROR"
WARNING = "WARNING"

#: The eleven business fact tables, in the order the schema introduced them.
FACT_TABLES = ("workspace", "person", "person_alias", "media", "person_media",
               "relationship", "relationship_history", "event", "event_participant",
               "evidence", "evidence_media")

#: Phase 7 projection, Phase 8 analysis ledger and Phase 9 memory: counted too,
#: but deliberately never counted as one of the eleven fact tables.
PROJECTION_TABLES = ("search_document",)
ANALYSIS_TABLES = ("analysis_run", "analysis_person_source", "analysis_relationship_source",
                   "analysis_event_source", "analysis_evidence_source")
MEMORY_TABLES = ("memory",)

TABLE_MODELS = {
    "workspace": WorkspaceModel, "person": PersonModel, "person_alias": PersonAliasModel,
    "media": MediaModel, "person_media": PersonMediaModel,
    "relationship": RelationshipModel, "relationship_history": RelationshipHistoryModel,
    "event": EventModel, "event_participant": EventParticipantModel,
    "evidence": EvidenceModel, "evidence_media": EvidenceMediaModel,
    "search_document": SearchDocumentModel, "analysis_run": AnalysisRunModel,
    "analysis_person_source": AnalysisPersonSourceModel,
    "analysis_relationship_source": AnalysisRelationshipSourceModel,
    "analysis_event_source": AnalysisEventSourceModel,
    "analysis_evidence_source": AnalysisEvidenceSourceModel, "memory": MemoryModel,
}

#: code -> (child table, child parent column, parent table)
#:
#: ``MEDIA_WORKSPACE_MISMATCH`` covers both the media row against its Workspace and
#: every binding that points at it. The eight codes are the frozen set; the check
#: that a workspace row "belongs to itself" is added by the audit itself as an
#: unconditional-null guard, because a Workspace has no parent workspace row.
WORKSPACE_MISMATCHES = (
    ("PERSON_WORKSPACE_MISMATCH", PersonModel, PersonModel.workspace_id, WorkspaceModel),
    ("RELATIONSHIP_WORKSPACE_MISMATCH", RelationshipModel,
     RelationshipModel.workspace_id, WorkspaceModel),
    ("RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH", RelationshipHistoryModel,
     RelationshipHistoryModel.relationship_id, RelationshipModel),
    ("EVENT_PARTICIPANT_WORKSPACE_MISMATCH", EventParticipantModel,
     EventParticipantModel.event_id, EventModel),
    ("EVIDENCE_WORKSPACE_MISMATCH", EvidenceModel, EvidenceModel.event_id, EventModel),
    ("EVIDENCE_MEDIA_WORKSPACE_MISMATCH", EvidenceMediaModel,
     EvidenceMediaModel.evidence_id, EvidenceModel),
    ("PERSON_MEDIA_WORKSPACE_MISMATCH", PersonMediaModel,
     PersonMediaModel.person_id, PersonModel),
    ("MEDIA_WORKSPACE_MISMATCH", MediaModel, MediaModel.workspace_id, WorkspaceModel),
)

#: code -> (from table, from column, to table, to column)
DANGLING_REFERENCES = (
    ("PERSON_MEDIA_DANGLING", PersonMediaModel, PersonMediaModel.media_id,
     MediaModel, MediaModel.id),
    ("EVIDENCE_MEDIA_DANGLING", EvidenceMediaModel, EvidenceMediaModel.media_id,
     MediaModel, MediaModel.id),
)

#: (label, source table, fact table) for the four Phase 8 provenance tables.
ANALYSIS_SOURCES = (
    ("analysis_person_source", AnalysisPersonSourceModel, PersonModel),
    ("analysis_relationship_source", AnalysisRelationshipSourceModel, RelationshipModel),
    ("analysis_event_source", AnalysisEventSourceModel, EventModel),
    ("analysis_evidence_source", AnalysisEvidenceSourceModel, EvidenceModel),
)

#: Phase 8 provenance rows whose workspace_id disagrees with their AnalysisRun.
#: ``analysis_*_source`` also carries ``(workspace_id, analysis_run_id) ->
#: analysis_run(workspace_id, id)``, so it is empty for the same reason as the
#: eight frozen checks; it is audited because provenance is the one place where a
#: wrong workspace would silently misattribute a fact to another person's data.
ANALYSIS_SOURCE_WORKSPACES = tuple(
    (f"{label.upper()}_WORKSPACE_MISMATCH", model) for label, model, _target in ANALYSIS_SOURCES)

#: The Phase 7 projection is derived data, but a row whose workspace_id disagrees
#: with the fact it projects would leak a fact across a workspace boundary in
#: search results, so it is audited as an ERROR as well.
PROJECTION_WORKSPACES = (
    ("SEARCH_PERSON_WORKSPACE_MISMATCH", "PERSON", PersonModel),
    ("SEARCH_ALIAS_WORKSPACE_MISMATCH", "ALIAS", PersonAliasModel),
    ("SEARCH_EVENT_WORKSPACE_MISMATCH", "EVENT", EventModel),
    ("SEARCH_EVIDENCE_WORKSPACE_MISMATCH", "EVIDENCE", EvidenceModel),
)

#: Guardrails: one report stays readable even on a badly damaged database, and one
#: verification pass never hashes an unbounded number of bytes per file.
MAX_ISSUES_PER_CODE = 200
_HASH_CHUNK = 1024 * 1024
_ACTIONABLE_STATUSES = ("SUCCEEDED", "FAILED")


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    """One finding: a stable machine code plus a privacy-safe description."""

    code: str
    severity: str
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    detail: str | None = None

    def to_mapping(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "entity_type": self.entity_type, "entity_id": self.entity_id,
                "detail": self.detail}


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """A frozen audit result; only ``errors`` decide ``ok`` (warnings do not)."""

    ok: bool
    checked_at: datetime
    errors: tuple[IntegrityIssue, ...]
    warnings: tuple[IntegrityIssue, ...]
    counts: dict

    @property
    def issues(self) -> tuple[IntegrityIssue, ...]:
        return self.errors + self.warnings

    def codes(self, severity: str | None = None) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues
                     if severity is None or issue.severity == severity)

    def summary(self) -> str:
        state = "OK" if self.ok else "发现问题"
        return (f"{state}：{len(FACT_TABLES)} 张表 / "
                f"{len(self.errors)} error / {len(self.warnings)} warning")

    def to_mapping(self) -> dict:
        """JSON-serializable form for the UI and the backup manifest."""
        return {"ok": self.ok,
                "checked_at": self.checked_at.astimezone(timezone.utc).isoformat(),
                "summary": self.summary(),
                "counts": dict(self.counts),
                "errors": [issue.to_mapping() for issue in self.errors],
                "warnings": [issue.to_mapping() for issue in self.warnings]}


@dataclass(frozen=True, slots=True)
class _MediaFile:
    """One media row whose canonical file must be reconciled with disk."""

    media_id: UUID
    workspace_id: UUID
    storage_path: str
    sha256: str
    size_bytes: int


class DataIntegrityService:
    """Read-only audit over the migrated schema, optionally including files."""

    def __init__(self, sessions, *, media_store=None, clock=utc_now):
        self._sessions = sessions
        self._store = media_store
        self._clock = clock

    # ------------------------------------------------------------------ public
    def check(self, workspace_id: UUID | None = None, *,
              verify_media_files: bool = True) -> IntegrityReport:
        """Audit the stored rows of one Workspace, or of all of them.

        ``verify_media_files=False`` skips every filesystem read (missing file,
        size, hash and orphan-file checks) and performs database checks only. The
        media store is not called at all in that mode. Nothing is written, deleted
        or refreshed in either mode.
        """
        if workspace_id is not None:
            validate_uuid(workspace_id, "workspace_id")
        checked_at = normalize_datetime(self._clock(), "clock")
        errors: list[IntegrityIssue] = []
        warnings: list[IntegrityIssue] = []
        counts: dict[str, Any] = {"workspace_filtered": workspace_id is not None,
                                  # PRAGMA foreign_key_check reports on the whole
                                  # database, because referential integrity is a
                                  # property of the file rather than of one
                                  # Workspace. Every other check honours the filter.
                                  "foreign_key_check_scope": "database"}

        with self._engine().connect() as connection:
            self._check_foreign_keys(connection, errors)
            self._count_rows(connection, workspace_id, counts)
            self._check_workspace_mismatches(connection, workspace_id, errors)
            self._check_analysis_source_workspaces(connection, workspace_id, errors)
            self._check_projection_workspaces(connection, workspace_id, errors)
            self._check_dangling_bindings(connection, workspace_id, errors)
            self._check_analysis_sources(connection, workspace_id, errors)
            self._check_memory_sources(connection, workspace_id, errors)
            self._check_memory_state(connection, workspace_id, errors)
            self._check_expired_memories(connection, workspace_id, checked_at, warnings)
            self._check_analysis_runs(connection, workspace_id, warnings)
            counts["media_unreferenced"] = self._check_unreferenced_media(
                connection, workspace_id, warnings)
            rows = self._media_rows(connection, workspace_id, counts)

        # Path resolution and every file read live behind this branch: a database
        # only audit never calls the media store at all.
        if verify_media_files and self._store is not None:
            files = self._resolve_media_files(rows, counts, errors)
            self._check_media_files(files, counts, errors)
            self._check_orphan_files(files, workspace_id=workspace_id, counts=counts,
                                     warnings=warnings)
        else:
            # No filesystem read happened: either the caller asked for a database
            # only audit, or no media store was injected at all.
            counts["media_rows_unresolved"] = 0
            counts["media_files_checked"] = 0
            counts["media_hashes_checked"] = 0
            counts["media_files_skipped"] = len(rows)
            counts["orphan_files"] = 0
        if self._store is None:
            warnings.append(IntegrityIssue(
                code="MEDIA_STORE_UNAVAILABLE", severity=WARNING,
                message="未提供媒体存储，文件层面的检查（缺失、大小、哈希、孤儿文件）已跳过。",
                entity_type="media", detail="先配置媒体存储目录后重新审计。"))
        counts["error_count"] = len(errors)
        counts["warning_count"] = len(warnings)
        return IntegrityReport(ok=not errors, checked_at=checked_at,
                               errors=tuple(errors), warnings=tuple(warnings), counts=counts)

    # ----------------------------------------------------------------- plumbing
    def _engine(self) -> Engine:
        """The Engine behind the injected session factory; never a new Engine."""
        engine = getattr(self._sessions, "bind", None)
        if engine is None:
            engine = getattr(self._sessions, "kw", {}).get("bind")
        if not isinstance(engine, Engine):
            raise TypeError("DataIntegrityService 需要绑定 Engine 的 sessionmaker。")
        return engine

    def _path_for(self, storage_path) -> Path:
        """Canonical absolute path of one storage_path, resolved by the media store.

        The store is the only component that knows the path policy, so the audit
        reuses it instead of re-implementing (and possibly weakening) validation.
        """
        if self._store is None:
            raise ValueError("media store unavailable")
        return Path(self._store.path_for(storage_path))

    def _walk_files(self, root):
        """Every real file below ``root``, staging and lock directories excluded.

        The media root is ``data_dir/media``; files are addressed by paths relative
        to it, so a report never carries an absolute host path.
        """
        media_dir = Path(self._store.media_dir)
        excluded = {media_dir / ".staging", media_dir / ".locks"}
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or any(parent in excluded for parent in path.parents):
                continue
            try:
                relative = path.relative_to(media_dir).as_posix()
            except ValueError:
                relative = path.name
            yield path, relative

    @staticmethod
    def _scoped(statement, column, workspace_id):
        return statement if workspace_id is None else statement.where(column == workspace_id)

    @staticmethod
    def _rows(connection: Connection, statement) -> list:
        return connection.execute(statement).all()

    @staticmethod
    def _digest(path: Path) -> str | None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(_HASH_CHUNK):
                    digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    # ------------------------------------------------------------------- checks
    def _check_foreign_keys(self, connection, errors):
        for table, rowid, parent, _fkid in connection.exec_driver_sql(
                "PRAGMA foreign_key_check").all():
            errors.append(IntegrityIssue(
                code="FOREIGN_KEY_VIOLATION", severity=ERROR,
                message=f"表 {table} 的第 {rowid} 行违反指向 {parent} 的外键约束。",
                entity_type=str(table), entity_id=None,
                detail=f"table={table} rowid={rowid} parent={parent}"))

    def _count_rows(self, connection, workspace_id, counts):
        for name in FACT_TABLES + PROJECTION_TABLES + ANALYSIS_TABLES + MEMORY_TABLES:
            table = TABLE_MODELS[name].__table__
            statement = select(func.count()).select_from(table)
            if workspace_id is not None and "workspace_id" in table.c:
                statement = statement.where(table.c.workspace_id == workspace_id)
            counts[f"table_{name}"] = int(connection.scalar(statement) or 0)
        counts["fact_tables"] = len(FACT_TABLES)

    def _check_workspace_mismatches(self, connection, workspace_id, errors):
        """Child rows whose workspace_id is not the workspace of the parent row.

        The join matches on the identifier only, so this is a real self-join and
        not a restatement of the composite foreign key. Because the parent row is
        looked up by identifier alone, the query also reports the two shapes that
        an inner join would silently drop:

        * the parent row exists but names a different workspace (its own
          workspace_id is reported), and
        * no parent row exists at all (reported as ``-``), which is a dangling
          reference and therefore also a foreign key violation.

        Each frozen check reports on its own, so a row whose parent is missing is
        named by both this check and the matching dangling-reference check: the two
        answers - "this reference is broken" and "this workspace cannot be
        reconciled with a parent" - are both true statements about the row.

        One of the eight has no independent content. ``PERSON_WORKSPACE_MISMATCH``
        and ``MEDIA_WORKSPACE_MISMATCH`` name a child whose parent is the
        ``workspace`` row itself; that row is identified by the very id the child
        copies, so its "own workspace" is only ever itself. The comparison still
        runs and is still a real self-join - it reports the row whenever no
        workspace with that id and that workspace_id exists, which is exactly the
        dangling case the foreign key check also reports. The guard below only
        recognises that ``workspace`` has no ``workspace_id`` column of its own and
        compares its primary key instead of inventing a value.
        """
        for code, child_model, child_key, parent_model in WORKSPACE_MISMATCHES:
            child, parent = child_model.__table__, parent_model.__table__
            # ``workspace`` is its own world: its identity is the id a child copies,
            # so the parent's "workspace" is that id rather than a separate column.
            parent_workspace_column = (parent.c.workspace_id if "workspace_id" in parent.c
                                       else parent.c.id)
            statement = (
                select(child.c.id, child.c.workspace_id,
                       select(parent_workspace_column)
                       .where(child_key == parent.c.id)
                       .scalar_subquery())
                .where(~select(parent.c.id)
                       .where(child_key == parent.c.id,
                              parent_workspace_column == child.c.workspace_id)
                       .exists()))
            for row_id, child_workspace, parent_workspace in self._rows(
                    connection, self._scoped(statement, child.c.workspace_id, workspace_id)):
                errors.append(IntegrityIssue(
                    code=code, severity=ERROR,
                    message=f"{child.name} 行的 workspace_id 不是其父行 {parent.name} 所在的 Workspace。",
                    entity_type=child.name, entity_id=str(row_id),
                    detail=(f"{child.name}.workspace_id={child_workspace} "
                            f"{parent.name}.workspace_id={parent_workspace or '-'}")))

    def _check_analysis_source_workspaces(self, connection, workspace_id, errors):
        """Provenance rows whose workspace_id disagrees with their AnalysisRun."""
        run = AnalysisRunModel.__table__
        for code, source_model in ANALYSIS_SOURCE_WORKSPACES:
            source = source_model.__table__
            statement = (select(source.c.id, source.c.workspace_id, run.c.workspace_id)
                         .join(run, source.c.analysis_run_id == run.c.id)
                         .where(source.c.workspace_id != run.c.workspace_id))
            for row_id, source_workspace, run_workspace in self._rows(
                    connection, self._scoped(statement, source.c.workspace_id, workspace_id)):
                errors.append(IntegrityIssue(
                    code=code, severity=ERROR,
                    message=f"{source.name} 的 workspace_id 与其 AnalysisRun 不一致。",
                    entity_type=source.name, entity_id=str(row_id),
                    detail=(f"{source.name}.workspace_id={source_workspace} != "
                            f"analysis_run.workspace_id={run_workspace}")))

    def _check_projection_workspaces(self, connection, workspace_id, errors):
        """Projection rows whose workspace_id disagrees with the projected fact."""
        projection = SearchDocumentModel.__table__
        for code, entity_type, fact_model in PROJECTION_WORKSPACES:
            fact = fact_model.__table__
            statement = (select(projection.c.id, projection.c.entity_id,
                                projection.c.workspace_id, fact.c.workspace_id)
                         .join(fact, projection.c.entity_id == fact.c.id)
                         .where(projection.c.entity_type == entity_type,
                                projection.c.workspace_id != fact.c.workspace_id))
            for row_id, entity_id, projection_workspace, fact_workspace in self._rows(
                    connection, self._scoped(statement, projection.c.workspace_id, workspace_id)):
                errors.append(IntegrityIssue(
                    code=code, severity=ERROR,
                    message=f"搜索投影行的 workspace_id 与 {fact.name} 事实不一致。",
                    entity_type="search_document", entity_id=str(entity_id),
                    detail=(f"search_document.id={row_id} "
                            f"projection={projection_workspace} fact={fact_workspace}")))

    def _check_dangling_bindings(self, connection, workspace_id, errors):
        for code, from_model, from_column, to_model, to_column in DANGLING_REFERENCES:
            source, target = from_model.__table__, to_model.__table__
            statement = (select(source.c.id, source.c.workspace_id, from_column)
                         .outerjoin(target, from_column == to_column)
                         .where(to_column.is_(None)))
            for row_id, row_workspace, missing in self._rows(
                    connection, self._scoped(statement, source.c.workspace_id, workspace_id)):
                errors.append(IntegrityIssue(
                    code=code, severity=ERROR,
                    message=f"{source.name} 行指向不存在的 {target.name}。",
                    entity_type=source.name, entity_id=str(row_id),
                    detail=(f"workspace_id={row_workspace} 缺失的 "
                            f"{target.name}.id={missing}")))

    def _check_analysis_sources(self, connection, workspace_id, errors):
        for _label, source_model, target_model in ANALYSIS_SOURCES:
            source, target = source_model.__table__, target_model.__table__
            statement = (select(source.c.id, source.c.analysis_run_id, source.c.target_id)
                         .outerjoin(target, source.c.target_id == target.c.id)
                         .where(target.c.id.is_(None)))
            for row_id, run_id, target_id in self._rows(
                    connection, self._scoped(statement, source.c.workspace_id, workspace_id)):
                errors.append(IntegrityIssue(
                    code="ANALYSIS_SOURCE_DANGLING", severity=ERROR,
                    message=f"{source.name} 指向不存在的 {target.name} 事实对象。",
                    entity_type=source.name, entity_id=str(row_id),
                    detail=(f"analysis_run_id={run_id} 缺失的 "
                            f"{target.name}.id={target_id}")))

    def _check_memory_sources(self, connection, workspace_id, errors):
        """Memory provenance: the source_type/source_analysis_id pair must hold.

        Four shapes are invalid, and one query answers all of them: the referenced
        run does not exist, the run exists in ANOTHER workspace (the database's own
        ``foreign_key_check`` reports that too, because the foreign key is the
        composite ``(workspace_id, source_analysis_id)``), a MANUAL row still
        carries an id, or an ANALYSIS row carries none. The run is looked up by id
        alone and its ``workspace_id`` is compared explicitly, so a run from
        another workspace is named instead of being mistaken for a missing one.
        """
        table, run = MemoryModel.__table__, AnalysisRunModel.__table__
        statement = (select(table.c.id, table.c.source_type, table.c.source_analysis_id,
                            run.c.workspace_id)
                     .outerjoin(run, table.c.source_analysis_id == run.c.id)
                     .where(or_(and_(table.c.source_type == "MANUAL",
                                     table.c.source_analysis_id.is_not(None)),
                                and_(table.c.source_type == "ANALYSIS",
                                     table.c.source_analysis_id.is_(None)),
                                and_(table.c.source_analysis_id.is_not(None),
                                     run.c.id.is_(None)),
                                and_(run.c.id.is_not(None),
                                     run.c.workspace_id != table.c.workspace_id))))
        for memory_id, source_type, run_id, run_workspace in self._rows(
                connection, self._scoped(statement, table.c.workspace_id, workspace_id)):
            if source_type == "ANALYSIS" and run_id is None:
                reason = "source_type=ANALYSIS 但没有 source_analysis_id"
            elif source_type == "MANUAL":
                reason = "source_type=MANUAL 却携带 source_analysis_id"
            elif run_workspace is not None:
                reason = "source_analysis_id 指向的 AnalysisRun 属于其他 Workspace"
            else:
                reason = "source_analysis_id 指向不存在的 AnalysisRun"
            errors.append(IntegrityIssue(
                code="MEMORY_SOURCE_INVALID", severity=ERROR,
                message=f"Memory 来源非法：{reason}。",
                entity_type="memory", entity_id=str(memory_id),
                detail=f"source_type={source_type} source_analysis_id={run_id}"))

    def _check_memory_state(self, connection, workspace_id, errors):
        table = MemoryModel.__table__
        # The three invalid shapes: a backwards validity window, a missing
        # confirmation, or content that is empty, over-long or not its own trimmed
        # form. Each predicate is evaluated by SQLite, NULL semantics included.
        statement = select(table.c.id, table.c.confirmed_at, table.c.valid_from,
                           table.c.valid_until, table.c.content).where(or_(
                               and_(table.c.valid_from.is_not(None),
                                    table.c.valid_until.is_not(None),
                                    table.c.valid_until < table.c.valid_from),
                               table.c.confirmed_at.is_(None),
                               func.length(func.trim(table.c.content)) == 0,
                               func.length(table.c.content) > 4000,
                               table.c.content != func.trim(table.c.content)))
        for memory_id, confirmed_at, valid_from, valid_until, content in self._rows(
                connection, self._scoped(statement, table.c.workspace_id, workspace_id)):
            if valid_from is not None and valid_until is not None and valid_until < valid_from:
                reason = "valid_until 早于 valid_from"
            elif confirmed_at is None:
                reason = "confirmed_at 为空（Memory 必须经用户确认）"
            else:
                reason = "content 为空、超长或与 trim 后不同"
            errors.append(IntegrityIssue(
                code="MEMORY_STATE_INVALID", severity=ERROR,
                message=f"Memory 状态非法：{reason}。",
                entity_type="memory", entity_id=str(memory_id),
                # A length is a fact about the row; the content itself is never copied.
                detail=f"content_length={len(content or '')}"))

    def _check_expired_memories(self, connection, workspace_id, checked_at, warnings):
        table = MemoryModel.__table__
        statement = select(table.c.id, table.c.valid_until).where(
            table.c.deleted_at.is_(None), table.c.valid_until.is_not(None),
            table.c.valid_until <= checked_at)
        for memory_id, valid_until in self._rows(
                connection, self._scoped(statement, table.c.workspace_id, workspace_id)):
            warnings.append(IntegrityIssue(
                code="MEMORY_EXPIRED", severity=WARNING,
                message="Memory 的有效期已过；行仍然保留，只是不再作为当前事实。",
                entity_type="memory", entity_id=str(memory_id),
                detail=f"valid_until={valid_until}"))

    def _check_analysis_runs(self, connection, workspace_id, warnings):
        table = AnalysisRunModel.__table__
        statement = select(table.c.id, table.c.status, table.c.completed_at,
                           table.c.duration_ms).where(
            or_(and_(table.c.status.in_(_ACTIONABLE_STATUSES),
                     table.c.completed_at.is_(None)),
                and_(table.c.status.in_(("PENDING", "RUNNING")),
                     table.c.completed_at.is_not(None))))
        for run_id, status, completed_at, duration_ms in self._rows(
                connection, self._scoped(statement, table.c.workspace_id, workspace_id)):
            warnings.append(IntegrityIssue(
                code="ANALYSIS_RUN_STATE_INVALID", severity=WARNING,
                message=f"AnalysisRun 的状态 {status} 与 completed_at 不一致；仅提示，不修改记录。",
                entity_type="analysis_run", entity_id=str(run_id),
                detail=f"status={status} completed_at={completed_at} duration_ms={duration_ms}"))

    def _check_unreferenced_media(self, connection, workspace_id, warnings):
        """Media that neither a PersonMedia nor an EvidenceMedia row references.

        Phase 7 rule: an EvidenceMedia row counts even when its Evidence is soft
        deleted, so soft deleting Evidence never releases the bytes. Symmetrically,
        a soft-deleted Person or Media row does not change the verdict either - the
        binding row is what counts, exactly like ``SQLMediaRepository.has_references``.
        """
        media = MediaModel.__table__
        person_media = select(PersonMediaModel.__table__.c.id).where(
            PersonMediaModel.__table__.c.media_id == media.c.id).exists()
        evidence_media = select(EvidenceMediaModel.__table__.c.id).where(
            EvidenceMediaModel.__table__.c.media_id == media.c.id).exists()
        statement = select(media.c.id, media.c.workspace_id, media.c.storage_path).where(
            ~person_media, ~evidence_media)
        found = self._rows(connection, self._scoped(statement, media.c.workspace_id,
                                                  workspace_id))
        for media_id, row_workspace, storage_path in found[:MAX_ISSUES_PER_CODE]:
            warnings.append(IntegrityIssue(
                code="MEDIA_UNREFERENCED", severity=WARNING,
                message="Media 没有被任何 PersonMedia 或 EvidenceMedia 引用；行与文件都保留。",
                entity_type="media", entity_id=str(media_id),
                detail=f"workspace_id={row_workspace} {self._short(storage_path)}"))
        return len(found)

    def _media_rows(self, connection, workspace_id, counts):
        table = MediaModel.__table__
        statement = select(table.c.id, table.c.workspace_id, table.c.storage_path,
                           table.c.sha256, table.c.size_bytes)
        rows = self._rows(connection, self._scoped(statement, table.c.workspace_id,
                                                   workspace_id))
        counts["media_rows"] = len(rows)
        return rows

    def _resolve_media_files(self, rows, counts, errors):
        """Map media rows to canonical paths; an unusable path is a missing file.

        Without a media store there is no path policy to resolve against, so no
        row is called unresolvable: "we cannot look" is reported once as
        ``MEDIA_STORE_UNAVAILABLE`` instead of once per media row.
        """
        files, unresolved = [], 0
        for media_id, row_workspace, storage_path, sha256, size_bytes in rows:
            if self._store is not None:
                try:
                    self._path_for(storage_path)
                except Exception:
                    unresolved += 1
                    errors.append(IntegrityIssue(
                        code="MEDIA_FILE_MISSING", severity=ERROR,
                        message="Media 的 storage_path 不是可解析的内容寻址路径，文件无法定位。",
                        entity_type="media", entity_id=str(media_id),
                        detail=self._short(storage_path)))
                    continue
            files.append(_MediaFile(media_id, row_workspace, storage_path, sha256, size_bytes))
        counts["media_rows_unresolved"] = unresolved
        return files

    def _check_media_files(self, files: Iterable[_MediaFile], counts, errors):
        checked = hashes = 0
        for item in files:
            path = self._path_for(item.storage_path)
            try:
                size = path.stat().st_size
            except OSError:
                errors.append(IntegrityIssue(
                    code="MEDIA_FILE_MISSING", severity=ERROR,
                    message="Media 记录的文件不存在或不可读取。",
                    entity_type="media", entity_id=str(item.media_id),
                    detail=item.storage_path))
                continue
            checked += 1
            if size != item.size_bytes:
                errors.append(IntegrityIssue(
                    code="MEDIA_FILE_SIZE_MISMATCH", severity=ERROR,
                    message="文件大小与 Media 记录的 size_bytes 不一致。",
                    entity_type="media", entity_id=str(item.media_id),
                    detail=f"{item.storage_path} 实际={size} 记录={item.size_bytes}"))
            digest = self._digest(path)
            if digest is None:
                errors.append(IntegrityIssue(
                    code="MEDIA_FILE_MISSING", severity=ERROR,
                    message="Media 文件在读取过程中消失或不可读取。",
                    entity_type="media", entity_id=str(item.media_id),
                    detail=item.storage_path))
                continue
            hashes += 1
            if digest != item.sha256.lower():
                errors.append(IntegrityIssue(
                    code="MEDIA_FILE_HASH_MISMATCH", severity=ERROR,
                    message="文件的 sha256 与 Media 记录的 sha256 不一致。",
                    entity_type="media", entity_id=str(item.media_id),
                    detail=f"{item.storage_path} 实际={digest[:12]}… 记录={item.sha256[:12]}…"))
        counts["media_files_checked"] = checked
        counts["media_hashes_checked"] = hashes

    def _check_orphan_files(self, files, *, workspace_id, counts, warnings):
        """Files under the media root no Media row claims. Reported, never deleted."""
        media_dir = Path(self._store.media_dir)
        root = media_dir if workspace_id is None else media_dir / str(workspace_id)
        # Every media row of the audit decides what is "known", so a file belongs to
        # another Workspace is never called an orphan by a single-Workspace audit.
        known = set()
        for item in files:
            try:
                known.add(self._path_for(item.storage_path))
            except Exception:
                continue
        orphans = []
        try:
            for path, relative in self._walk_files(root):
                if path not in known:
                    orphans.append(relative)
        except OSError:
            counts["orphan_files"] = 0
            counts["orphan_scan_failed"] = True
            return
        counts["orphan_files"] = len(orphans)
        for relative in orphans[:MAX_ISSUES_PER_CODE]:
            warnings.append(IntegrityIssue(
                code="ORPHAN_MEDIA_FILE", severity=WARNING,
                message="媒体根目录下存在没有对应 Media 行的文件；仅报告，不删除。",
                entity_type="media", entity_id=None, detail=relative))

    @staticmethod
    def _short(value) -> str:
        text = str(value)
        return text if len(text) <= 200 else text[:197] + "..."
