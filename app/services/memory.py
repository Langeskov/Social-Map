"""MemoryService: user-confirmed memory, never an automatic side effect.

Rules enforced here (and mirrored by database constraints):

* a Memory is created only with an explicit confirmation — ``confirmed=True`` is a
  required keyword for the analysis path, and the manual path stamps
  ``confirmed_at`` at creation time;
* the content always comes from the caller: the raw analysis result is **never**
  copied automatically;
* provenance is write-once: editing a memory cannot change ``source_type`` or
  ``source_analysis_id``;
* nothing in this module writes to a fact table.
"""
import logging
import re
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from app.domain.entities import CandidateReference, Memory
from app.domain.errors import DomainError, NotFoundError
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.memory_repositories import sql_memory_repositories
from app.services.operations import write_operation
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

logger = logging.getLogger(__name__)

#: Conservative source markers a model *may* emit. The body is captured loosely so a
#: malformed reference is reported as a rejected candidate instead of vanishing; the
#: uuid is always validated afterwards and never trusted.
MARKER_PATTERN = re.compile(r"\[(PERSON|RELATIONSHIP|EVENT|EVIDENCE):([^\[\]\s]{1,64})\]")
SOURCE_KIND_FOR_TYPE = {"PERSON": "people", "RELATIONSHIP": "relationships", "EVENT": "events",
                        "EVIDENCE": "evidence"}


class MemoryService:
    def __init__(self, sessions, *, analysis_repositories=sql_analysis_repositories,
                 repositories=sql_memory_repositories, clock=utc_now):
        self._sessions = sessions
        self._analysis = analysis_repositories
        self._repositories = repositories
        self._clock = clock

    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    # ------------------------------------------------------------------- reads
    def get(self, workspace_id, memory_id) -> Memory:
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            memory = repositories.memories.get(workspace_id, memory_id)
            if memory is None:
                raise NotFoundError("Memory 不属于当前 Workspace。")
            return memory

    def list(self, workspace_id, *, person_id=None, kind=None, include_deleted=False,
             include_expired=False, now=None, limit=None) -> list[Memory]:
        """Memories of one person, or the workspace-level ones when person_id is None."""
        return self._list(workspace_id, kind=kind, include_deleted=include_deleted,
                          include_expired=include_expired, now=now, limit=limit,
                          person_id=person_id, workspace_scoped_only=person_id is None)

    def list_all(self, workspace_id, *, kind=None, include_deleted=False, include_expired=False,
                 now=None, limit=None) -> list[Memory]:
        """Every memory of the workspace, person-specific and workspace-level alike."""
        return self._list(workspace_id, kind=kind, include_deleted=include_deleted,
                          include_expired=include_expired, now=now, limit=limit)

    def _list(self, workspace_id, *, kind=None, include_deleted=False, include_expired=False,
              now=None, limit=None, person_id=None, workspace_scoped_only=False):
        validate_uuid(workspace_id, "workspace_id")
        if kind is not None and kind not in ("FACT", "PREFERENCE", "CONTEXT"):
            raise DomainError("kind 必须是 FACT、PREFERENCE 或 CONTEXT。")
        moment = normalize_datetime(now, "now") if now is not None else self._now()
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            return repositories.memories.list_by_workspace(
                workspace_id, person_id=person_id, workspace_scoped_only=workspace_scoped_only,
                kind=kind, include_deleted=include_deleted, now=moment,
                include_expired=include_expired, limit=limit)

    @staticmethod
    def is_current(memory: Memory, *, now: datetime | None = None) -> bool:
        return memory.is_current(now=now)

    # ------------------------------------------------------------------ writes
    def create(self, workspace_id, content, *, kind, person_id=None, valid_from=None,
               valid_until=None) -> Memory:
        """Manual memory: `MANUAL`, no analysis reference, confirmed now."""
        now = self._now()
        entity = Memory(workspace_id=workspace_id, content=content, kind=kind, source_type="MANUAL",
                        confirmed_at=now, person_id=person_id,
                        valid_from=normalize_datetime(valid_from, "valid_from", optional=True),
                        valid_until=normalize_datetime(valid_until, "valid_until", optional=True),
                        created_at=now, updated_at=now)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            self._require_person(repositories, workspace_id, entity.person_id)
            return repositories.memories.save(entity)

    def create_from_analysis(self, workspace_id, analysis_run_id, content, *, kind, confirmed,
                             person_id=None, valid_from=None, valid_until=None) -> Memory:
        """Create a memory from a specific analysis after explicit user confirmation.

        `confirmed` must be literally True: opening a confirmation dialog is not a
        confirmation, so an unconfirmed call is refused instead of silently
        recording the analysis as long-term memory.
        """
        if confirmed is not True:
            raise DomainError("创建 Memory 需要用户明确确认（confirmed=True）。")
        validate_uuid(analysis_run_id, "analysis_run_id")
        now = self._now()
        entity = Memory(workspace_id=workspace_id, content=content, kind=kind,
                        source_type="ANALYSIS", source_analysis_id=analysis_run_id,
                        confirmed_at=now, person_id=person_id,
                        valid_from=normalize_datetime(valid_from, "valid_from", optional=True),
                        valid_until=normalize_datetime(valid_until, "valid_until", optional=True),
                        created_at=now, updated_at=now)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            run = self._analysis(session).runs.get(workspace_id, analysis_run_id)
            if run is None:
                raise NotFoundError("AnalysisRun 不属于当前 Workspace。")
            if run.status != "SUCCEEDED":
                raise DomainError("只有成功的分析才能保存为 Memory。")
            self._require_person(repositories, workspace_id, entity.person_id)
            return repositories.memories.save(entity)

    def update(self, workspace_id, memory_id, *, content=None, kind=None, person_id=None,
               clear_person=False, valid_from=None, valid_until=None, clear_valid_from=False,
               clear_valid_until=False) -> Memory:
        """Edit content/kind/person/validity. Provenance and source stay untouched."""
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            current = repositories.memories.get(workspace_id, memory_id)
            if current is None:
                raise NotFoundError("Memory 不属于当前 Workspace。")
            if current.deleted_at is not None:
                raise DomainError("已删除的 Memory 需要先恢复才能编辑。")
            changes = {"updated_at": self._now()}
            if content is not None:
                changes["content"] = content
            if kind is not None:
                changes["kind"] = kind
            if clear_person:
                changes["person_id"] = None
            elif person_id is not None:
                self._require_person(repositories, workspace_id, person_id)
                changes["person_id"] = person_id
            if clear_valid_from:
                changes["valid_from"] = None
            elif valid_from is not None:
                changes["valid_from"] = normalize_datetime(valid_from, "valid_from")
            if clear_valid_until:
                changes["valid_until"] = None
            elif valid_until is not None:
                changes["valid_until"] = normalize_datetime(valid_until, "valid_until")
            updated = replace(current, **changes)
            return repositories.memories.update(updated)

    def soft_delete(self, workspace_id, memory_id) -> Memory:
        """Soft delete only: the row, its provenance and the analysis run survive."""
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            current = repositories.memories.get(workspace_id, memory_id)
            if current is None:
                raise NotFoundError("Memory 不属于当前 Workspace。")
            if current.deleted_at is not None:
                raise DomainError("Memory 已经删除。")
            now = self._now()
            return repositories.memories.update(replace(current, deleted_at=now, updated_at=now))

    def restore(self, workspace_id, memory_id) -> Memory:
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            current = repositories.memories.get(workspace_id, memory_id)
            if current is None:
                raise NotFoundError("Memory 不属于当前 Workspace。")
            if current.deleted_at is None:
                raise DomainError("Memory 没有被删除。")
            return repositories.memories.update(replace(current, deleted_at=None,
                                                         updated_at=self._now()))

    # -------------------------------------------------------------- candidates
    def candidate_references(self, workspace_id, analysis_run_id) -> list[CandidateReference]:
        """Source markers found in the raw answer, validated against this run's sources.

        Nothing is written: a marker is *accepted* only when the object exists in the
        same workspace **and** was part of the recorded sources of this very run.
        """
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._analysis(session)
            workspace_required(repositories, workspace_id)
            run = repositories.runs.get(workspace_id, analysis_run_id)
            if run is None:
                raise NotFoundError("AnalysisRun 不属于当前 Workspace。")
            recorded = repositories.sources.list_by_run(workspace_id, analysis_run_id)
            by_kind = {"PERSON": {source.target_id for source in recorded.people},
                       "RELATIONSHIP": {source.target_id for source in recorded.relationships},
                       "EVENT": {source.target_id for source in recorded.events},
                       "EVIDENCE": {source.target_id for source in recorded.evidence}}
            results, seen = [], set()
            for match in MARKER_PATTERN.finditer(run.result or ""):
                raw, entity_type, identifier = match.group(0), match.group(1), match.group(2)
                if raw in seen:
                    continue
                seen.add(raw)
                try:
                    entity_id = UUID(identifier)
                except ValueError:
                    results.append(CandidateReference(raw=raw, entity_type=entity_type,
                                                      reason="不是合法的 UUID"))
                    continue
                if entity_id not in by_kind[entity_type]:
                    results.append(CandidateReference(raw=raw, entity_type=entity_type,
                                                      entity_id=entity_id,
                                                      reason="该对象不在本次分析的来源中"))
                    continue
                if not self._exists(repositories, workspace_id, entity_type, entity_id):
                    results.append(CandidateReference(raw=raw, entity_type=entity_type,
                                                      entity_id=entity_id,
                                                      reason="该对象已不存在或不属于当前 Workspace"))
                    continue
                results.append(CandidateReference(raw=raw, entity_type=entity_type,
                                                  entity_id=entity_id, accepted=True,
                                                  reason="属于本次分析的来源"))
            return results

    # --------------------------------------------------------------- internals
    @staticmethod
    def _require_person(repositories, workspace_id, person_id):
        if person_id is None:
            return
        if repositories.people.get(workspace_id, person_id, include_deleted=True) is None:
            raise NotFoundError("人物不属于当前 Workspace。")

    @staticmethod
    def _exists(repositories, workspace_id, entity_type, entity_id):
        if entity_type == "PERSON":
            return repositories.people.get(workspace_id, entity_id, include_deleted=True) is not None
        if entity_type == "RELATIONSHIP":
            return repositories.relationships.get(workspace_id, entity_id,
                                                  include_deleted=True) is not None
        if entity_type == "EVENT":
            return repositories.events.get(workspace_id, entity_id, include_deleted=True) is not None
        return repositories.evidence.get(workspace_id, entity_id, include_deleted=True) is not None
