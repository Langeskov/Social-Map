"""AnalysisService: user-triggered model calls, recorded, never touching facts.

Flow of one run:

1. `PENDING` run is created with the request scope (one write transaction).
2. `ContextBuilder` renders the snapshot from the fact layer (read-only).
3. `PromptBuilder` turns the snapshot into messages and the provider answers.
4. The run is updated to `SUCCEEDED` / `FAILED` / `CANCELLED` together with the
   snapshot and the referenced fact ids (`analysis_*_source`).

No method in this module writes to a fact table, and no method is called
automatically: every run starts from an explicit user action.
"""
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

from app.domain.entities import (ANALYSIS_TYPE_FOR_TARGET, ANALYSIS_TYPES, AnalysisRun,
                                 AnalysisSourceSet)
from app.domain.errors import ConflictError, DomainError, NotFoundError
from app.domain.validation import normalize_datetime, utc_now, validate_uuid
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.context import (ALL_TIME, DEFAULT_MAX_CONTEXT_TOKENS, LAST_30_DAYS,
                                  AnalysisRequest, ContextBuilder, ContextSnapshot,
                                  snapshot_from_mapping)
from app.services.operations import write_operation
from app.services.prompt import PromptBuilder
from app.services.rules import workspace_required
from app.services.unit_of_work import transaction

logger = logging.getLogger(__name__)
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class AnalysisRunView:
    run: AnalysisRun
    sources: AnalysisSourceSet

    @property
    def snapshot(self) -> ContextSnapshot | None:
        if not self.run.context_snapshot:
            return None
        try:
            return snapshot_from_mapping(json.loads(self.run.context_snapshot))
        except (ValueError, KeyError, TypeError):
            return None

    @property
    def scope(self) -> dict:
        try:
            return json.loads(self.run.scope)
        except (ValueError, TypeError):
            return {}


class AnalysisService:
    """The analysis layer's public API; the UI never sees a provider directly."""

    def __init__(self, sessions, gateway, *, repositories=sql_analysis_repositories,
                 prompt_builder=None, clock=utc_now, sleeper=time.sleep):
        self._sessions = sessions
        self._gateway = gateway
        self._repositories = repositories
        self._prompt = prompt_builder or PromptBuilder()
        self._clock = clock
        self._sleep = sleeper

    # ------------------------------------------------------------------ helpers
    def _now(self):
        return normalize_datetime(self._clock(), "clock")

    def _require(self, repositories, workspace_id, run_id, *, with_sources=False):
        workspace_required(repositories, workspace_id)
        run = repositories.runs.get(workspace_id, run_id)
        if run is None:
            raise NotFoundError("AnalysisRun 不属于当前 Workspace。")
        if not with_sources:
            return run
        return AnalysisRunView(run, repositories.sources.list_by_run(workspace_id, run_id))

    @staticmethod
    def analysis_type_for(target_type: str) -> str:
        try:
            return ANALYSIS_TYPE_FOR_TARGET[target_type]
        except KeyError as exc:
            raise DomainError("target_type 必须是 PERSON、RELATIONSHIP 或 EVENT。") from exc

    def _validate_target(self, repositories, workspace_id, target_type, target_id):
        validate_uuid(target_id, "target_id")
        if target_type == "PERSON":
            if repositories.people.get(workspace_id, target_id) is None:
                raise NotFoundError("人物不属于当前 Workspace，或已经归档。")
        elif target_type == "RELATIONSHIP":
            if repositories.relationships.get(workspace_id, target_id) is None:
                raise NotFoundError("关系不属于当前 Workspace，或已删除。")
        elif target_type == "EVENT":
            if repositories.events.get(workspace_id, target_id) is None:
                raise NotFoundError("Event 不属于当前 Workspace，或已删除。")
        else:
            raise DomainError("target_type 必须是 PERSON、RELATIONSHIP 或 EVENT。")

    # -------------------------------------------------------------------- reads
    def get(self, workspace_id, run_id, *, with_sources=True):
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            return self._require(repositories, workspace_id, run_id, with_sources=with_sources)

    def history(self, workspace_id, *, target_type=None, target_id=None, limit=DEFAULT_LIMIT):
        validate_uuid(workspace_id, "workspace_id")
        with transaction(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, workspace_id)
            runs = repositories.runs.list_by_workspace(workspace_id, target_type=target_type,
                                                       target_id=target_id, limit=limit)
            return [AnalysisRunView(run, repositories.sources.list_by_run(workspace_id, run.id))
                    for run in runs]

    def build_snapshot(self, request: AnalysisRequest, *, analysis_run_id=None):
        """Render a context without calling a model (used by the UI preview and tests)."""
        with transaction(self._sessions) as session:
            builder = ContextBuilder(self._repositories(session), clock=self._clock)
            return builder.build(request, analysis_run_id=analysis_run_id)

    # ------------------------------------------------------------------- writes
    def create_run(self, request: AnalysisRequest, *, provider_name: str | None = None,
                   cancel=None) -> AnalysisRun:
        """Create, execute and record one analysis run (never writes fact tables)."""
        if not isinstance(request, AnalysisRequest):
            raise DomainError("request 必须是 AnalysisRequest。")
        if request.analysis_type != self.analysis_type_for(request.target_type):
            raise DomainError("analysis_type 与 target_type 不匹配。")
        started_wall = time.monotonic()
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            workspace_required(repositories, request.workspace_id)
            self._validate_target(repositories, request.workspace_id, request.target_type,
                                  request.target_id)
            entry = self._gateway.config().provider(provider_name)
            now = self._now()
            scope = {"analysis_type": request.analysis_type, "target_type": request.target_type,
                     "target_id": str(request.target_id), "time_scope": request.time_scope,
                     "as_of": request.as_of.isoformat(),
                     "include_relationships": request.include_relationships,
                     "include_history": request.include_history,
                     "include_observations": request.include_observations,
                     "include_evidence": request.include_evidence,
                     "selected_event_ids": [str(value) for value in request.selected_event_ids],
                     "selected_evidence_ids": [str(value) for value in request.selected_evidence_ids],
                     "credential_ref": self._gateway.credential_reference(entry.provider_name)}
            run = repositories.runs.save(AnalysisRun(
                workspace_id=request.workspace_id, target_type=request.target_type,
                target_id=request.target_id, analysis_type=request.analysis_type,
                provider_name=entry.provider_name, model_name=entry.model, scope=json.dumps(scope),
                status="PENDING", created_at=now, started_at=now))
            run_id = run.id

        # Outside the creation transaction: render the context from the fact layer.
        try:
            with transaction(self._sessions) as session:
                builder = ContextBuilder(self._repositories(session), clock=self._clock)
                snapshot = builder.build(request, analysis_run_id=run_id)
        except Exception as exc:
            return self._fail(run_id, request.workspace_id, exc, started_wall)

        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            current = repositories.runs.get(request.workspace_id, run_id)
            running = replace(current, status="RUNNING",
                              context_snapshot=json.dumps(snapshot.to_mapping(), ensure_ascii=False),
                              started_at=self._now())
            repositories.runs.update(running)
            self._persist_sources(repositories, snapshot)

        messages = self._prompt.build(snapshot)
        request_payload = _chat_request(entry, messages)
        try:
            if cancel is not None:
                cancel.raise_if_cancelled()
            response = self._gateway.chat(request_payload, provider_name=entry.provider_name,
                                          cancel=cancel)
        except Exception as exc:
            return self._fail(run_id, request.workspace_id, exc, started_wall)

        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            current = repositories.runs.get(request.workspace_id, run_id)
            finished = self._complete(current, status="SUCCEEDED", result=response.content,
                                      started_wall=started_wall)
            return repositories.runs.update(finished)

    def cancel_run(self, workspace_id, run_id) -> AnalysisRun:
        """Mark a run cancelled; the HTTP request itself ends on its own timeout."""
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            run = self._require(repositories, workspace_id, run_id)
            if run.finished:
                raise ConflictError("该分析已经结束，无法取消。")
            return repositories.runs.update(replace(
                run, status="CANCELLED", error_message="分析已取消；未修改任何事实数据。",
                completed_at=self._now(), duration_ms=0))

    def delete_run(self, workspace_id, run_id) -> None:
        """Remove one run and its source rows; fact tables are untouched."""
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            self._require(repositories, workspace_id, run_id)
            repositories.sources.remove_for_run(workspace_id, run_id)
            repositories.runs.delete(workspace_id, run_id)

    # ------------------------------------------------------------------ internals
    def _persist_sources(self, repositories, snapshot: ContextSnapshot) -> None:
        ids = snapshot.source_ids()
        workspace_id = UUID(snapshot.scope["workspace_id"])   # scope is JSON, so it is text
        for kind, values in ids.items():
            if not values:
                continue
            repositories.sources.add_many(kind, workspace_id, snapshot.analysis_run_id,
                                          [UUID(value) for value in values])

    def _complete(self, run, *, status, result, started_wall, error_message=None):
        finished_at = self._now()
        duration = int((time.monotonic() - started_wall) * 1000)
        return replace(run, status=status, result=result, error_message=error_message,
                       completed_at=finished_at, duration_ms=max(0, duration))

    def _fail(self, run_id, workspace_id, exc, started_wall) -> AnalysisRun:
        from app.services.llm import LLMCancelledError, LLMError
        status = "CANCELLED" if isinstance(exc, LLMCancelledError) else "FAILED"
        message = str(exc) if isinstance(exc, (LLMError, DomainError)) else "分析失败，请查看本地日志。"
        # Only the safe message and the exception class are logged; never context/result.
        logger.warning("Analysis run %s %s: type=%s", run_id, status, type(exc).__name__)
        with write_operation(self._sessions) as session:
            repositories = self._repositories(session)
            current = repositories.runs.get(workspace_id, run_id)
            if current is None:
                raise NotFoundError("AnalysisRun 已被删除。")
            return repositories.runs.update(self._complete(current, status=status, result=None,
                                                           started_wall=started_wall,
                                                           error_message=message))


def _chat_request(entry, messages):
    from app.services.llm import ChatRequest
    return ChatRequest(model=entry.model, messages=tuple(messages), temperature=entry.temperature,
                       timeout=entry.timeout, extra_headers=entry.extra_headers)
