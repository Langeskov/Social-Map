"""Model evaluation and comparison (Phase 13.4).

Two modes, both deliberately explicit:

* **offline (fixed answers)** — every model label replays a synthetic answer from
  the dataset through the *same* materialized context and prompt, so dataset /
  scenario / context / prompt versions are shared by construction. No provider,
  no network (used by tests and by ``python -m app.evaluation --models``).
* **online (live provider)** — one explicitly selected provider/model is called
  through the existing ``LLMGateway``; only the CLI can start it, and it refuses
  to run without an explicit confirmation flag. Failures (error class, timeout,
  retry-unknown, early finish) stay in the report; they are never dropped.

Automatic metrics only judge structure and citation legality
(PARSE_VALIDITY, REQUIRED_SECTIONS, CITATION_VALIDITY, CROSS_WORKSPACE_LEAKAGE,
FACT_COVERAGE, GROUNDING). Semantic support, contradiction handling, uncertainty
quality and completeness stay human-review rows — there is no score, no LLM
judge verdict and no fabricated pass.
"""
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.entities.analysis_result import (LEGACY_MARKDOWN, EMPTY, SCHEMA_VERSION,
                                                 SECTION_KEYS, STRUCTURED_STATUSES,
                                                 TOO_LARGE)
from app.domain.entities.reference_validation import VALID_FULL, VALID_LIMITED, is_valid
from app.domain.validation import utc_now
from app.evaluation.dataset import PLACEHOLDER_PATTERN
from app.evaluation.fixture import materialize_fixture
from app.evaluation.models import (DETERMINISTIC, FAIL, HUMAN, HUMAN_ASSISTED,
                                   METRIC_DEFINITIONS, NOT_APPLICABLE, PASS,
                                   EvaluationDataset, EvaluationScenario, MetricResult)
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.analysis_result_parser import parse_analysis_result
from app.services.analysis_telemetry import (PROMPT_TEMPLATE_VERSION,
                                             context_snapshot_hash,
                                             prompt_template_hash,
                                             request_payload_hash)
from app.services.context import AnalysisRequest, ContextBuilder
from app.services.llm import ChatRequest, LLMError, LLMTimeoutError
from app.services.llm.response_metadata import finish_reason, usage_summary
from app.services.prompt import PromptBuilder
from app.services.reference_verification import ReferenceVerifier
from app.services.unit_of_work import transaction

OFFLINE_MODE = "OFFLINE_FIXED"
ONLINE_MODE = "ONLINE_LIVE"

AUTO_METRICS = ("PARSE_VALIDITY", "REQUIRED_SECTIONS", "CITATION_VALIDITY",
                "CROSS_WORKSPACE_LEAKAGE", "FACT_COVERAGE", "GROUNDING")
HUMAN_METRICS = ("UNSUPPORTED_CLAIMS", "UNCERTAINTY_HANDLING", "EPISTEMIC_SEPARATION",
                 "COMPLETENESS", "ROBUSTNESS")


@dataclass(frozen=True)
class ModelEvaluationSpec:
    """One model label under evaluation (fixed-answer or live provider)."""

    label: str
    mode: str = OFFLINE_MODE
    provider_name: str | None = None
    model_name: str | None = None

    def __post_init__(self):
        if self.mode not in (OFFLINE_MODE, ONLINE_MODE):
            raise ValueError(f"未知评估模式：{self.mode!r}")
        if not self.label or not self.label.strip():
            raise ValueError("model label 不能为空")


@dataclass(frozen=True)
class ModelCallOutcome:
    """What happened for one call (success, failure, timing, provider metadata)."""

    status: str                         # SUCCESS / FAILED
    answer: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    timed_out: bool = False
    attempts: int | None = None
    duration_ms: int = 0
    returned_model: str | None = None
    finish: dict | None = None
    usage: dict | None = None

    def to_mapping(self) -> dict:
        return {"status": self.status, "answer": self.answer,
                "error_class": self.error_class, "error_message": self.error_message,
                "timed_out": self.timed_out, "attempts": self.attempts,
                "duration_ms": self.duration_ms, "returned_model": self.returned_model,
                "finish": self.finish, "usage": self.usage}


@dataclass(frozen=True)
class ModelScenarioResult:
    scenario_id: str
    scenario_version: str
    scenario_path: str
    model_label: str
    mode: str
    request: dict
    outcome: ModelCallOutcome
    parse_status: str | None
    references: tuple
    metrics: tuple

    def metric(self, metric_id: str) -> MetricResult | None:
        for item in self.metrics:
            if item.metric_id == metric_id:
                return item
        return None

    @property
    def scenario(self) -> str:
        return self.scenario_id


@dataclass(frozen=True)
class ModelEvaluationRunResult:
    run_id: str
    mode: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    prompt_template_version: str
    prompt_hash: str
    analysis_schema_version: str
    models: tuple
    scenario_ids: tuple
    results: tuple
    skipped_scenarios: tuple = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def for_model(self, label: str) -> tuple:
        return tuple(item for item in self.results if item.model_label == label)

    @property
    def failures(self) -> tuple:
        return tuple(item for item in self.results if item.outcome.status != "SUCCESS")

    def to_mapping(self) -> dict:
        return {"run_id": self.run_id, "mode": self.mode,
                "dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
                "dataset_digest": self.dataset_digest,
                "prompt_template_version": self.prompt_template_version,
                "prompt_hash": self.prompt_hash,
                "analysis_schema_version": self.analysis_schema_version,
                "models": [{"label": item.label, "mode": item.mode,
                            "provider_name": item.provider_name,
                            "model_name": item.model_name} for item in self.models],
                "scenario_ids": list(self.scenario_ids),
                "skipped_scenarios": list(self.skipped_scenarios),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
                "results": [{"scenario_id": item.scenario_id,
                             "scenario_version": item.scenario_version,
                             "model_label": item.model_label, "mode": item.mode,
                             "request": item.request,
                             "outcome": item.outcome.to_mapping(),
                             "parse_status": item.parse_status,
                             "references": [reference.to_mapping()
                                            for reference in item.references],
                             "metrics": [{"metric_id": m.metric_id, "status": m.status,
                                          "evaluation_mode": m.evaluation_mode,
                                          "reason": m.reason, "evidence": list(m.evidence),
                                          "details": dict(m.details)}
                                         for m in item.metrics]}
                            for item in self.results],
                "aggregates": aggregate_models(self)}


# ------------------------------------------------------------------- evaluator
def evaluate_answer(scenario: EvaluationScenario, parsed, references,
                    expected_fact_targets: dict) -> tuple:
    """Rule-based metrics for one answer; never fakes a semantic pass.

    ``references`` are ``ValidatedReference`` rows; ``expected_fact_targets`` maps
    ``fact_id -> (source_type, uuid_str)`` for reference-based coverage.
    """
    metrics = []
    structured = parsed.parse_status in STRUCTURED_STATUSES
    # ---------------------------------------------------------- parse validity
    if structured:
        metrics.append(_pass("PARSE_VALIDITY", f"结构化 JSON 契约（{parsed.parse_status}）。"))
    elif parsed.parse_status == LEGACY_MARKDOWN:
        metrics.append(_fail("PARSE_VALIDITY",
                             "旧版 Markdown 四段格式：可读但不符合 schema 1 JSON 契约。"))
    elif parsed.parse_status == EMPTY:
        metrics.append(_fail("PARSE_VALIDITY", "空回答。"))
    elif parsed.parse_status == TOO_LARGE:
        metrics.append(_fail("PARSE_VALIDITY", "回答超过解析上限，未结构化。"))
    else:
        metrics.append(_fail("PARSE_VALIDITY", f"无法按契约解析（{parsed.parse_status}）。"))
    # -------------------------------------------------------- required sections
    if structured:
        missing = [key for key in SECTION_KEYS if key not in parsed.sections_present]
        if missing:
            metrics.append(_fail("REQUIRED_SECTIONS", "缺少小节：" + "、".join(missing)))
        else:
            metrics.append(_pass("REQUIRED_SECTIONS", "四节（facts/observations/interpretations/"
                                                    "uncertainties）均存在。"))
    else:
        metrics.append(_fail("REQUIRED_SECTIONS",
                             "回答不是结构化 JSON，四节契约无法满足。"))
    # --------------------------------------------------------- citation validity
    if not references:
        metrics.append(_n("CITATION_VALIDITY", "回答没有引用可验证。"))
    else:
        invalid = [item for item in references if not is_valid(item.status)]
        full = sum(1 for item in references if item.status == VALID_FULL)
        limited = sum(1 for item in references if item.status == VALID_LIMITED)
        details = {"total": len(references), "valid_full": full,
                   "valid_limited": limited, "invalid": len(invalid)}
        if invalid:
            summary = "；".join(f"{item.entity_type}:{item.entity_id} {item.status}"
                                for item in invalid[:5])
            metrics.append(_fail("CITATION_VALIDITY",
                                 f"{len(invalid)} 条引用无效：{summary}", details=details))
        else:
            metrics.append(_pass("CITATION_VALIDITY",
                                 f"{len(references)} 条引用全部合法"
                                 f"（完整 {full} · 有限 {limited}）。", details=details))
    # ------------------------------------------------------- workspace isolation
    if not references:
        metrics.append(_n("CROSS_WORKSPACE_LEAKAGE", "回答没有引用可检查。"))
    else:
        leaked = [item for item in references if item.status == "WORKSPACE_MISMATCH"]
        if leaked:
            metrics.append(_fail("CROSS_WORKSPACE_LEAKAGE",
                                 f"{len(leaked)} 条引用指向其他 Workspace。"))
        else:
            metrics.append(_pass("CROSS_WORKSPACE_LEAKAGE", "没有跨 Workspace 引用。"))
    # ------------------------------------------------------------ fact coverage
    if not expected_fact_targets:
        metrics.append(_n("FACT_COVERAGE", "场景没有参考答案事实。"))
    else:
        cited = {(item.entity_type, str(item.entity_id)) for item in references
                 if is_valid(item.status)}
        missing = [fact_id for fact_id, (source_type, source_id) in expected_fact_targets.items()
                   if (source_type, source_id) not in cited]
        covered = len(expected_fact_targets) - len(missing)
        details = {"covered": covered, "total": len(expected_fact_targets),
                   "missing": missing}
        if missing:
            metrics.append(_fail("FACT_COVERAGE",
                                 f"覆盖 {covered}/{len(expected_fact_targets)}；"
                                 "缺少引用的事实：" + "、".join(missing), details=details))
        else:
            metrics.append(_pass("FACT_COVERAGE",
                                 f"全部 {len(expected_fact_targets)} 条参考答案事实均有引用。",
                                 details=details))
    # ---------------------------------------------------------------- grounding
    facts_entries = parsed.facts
    if not facts_entries:
        if expected_fact_targets:
            metrics.append(_fail("GROUNDING", "没有 FACT 条目，无法支撑任何事实结论。"))
        else:
            metrics.append(_n("GROUNDING", "回答没有 FACT 条目可检查。"))
    else:
        uncited = [index for index, entry in enumerate(facts_entries) if not entry.references]
        invalid_entries = []
        for index, entry in enumerate(facts_entries):
            entry_refs = [item for item in references
                          if any(str(item.entity_id) == str(reference.entity_id)
                                 and item.entity_type == reference.entity_type
                                 for reference in entry.references)]
            if entry.references and not any(is_valid(item.status) for item in entry_refs):
                invalid_entries.append(index)
        details = {"fact_entries": len(facts_entries),
                   "uncited": len(uncited), "invalid_citations": len(invalid_entries),
                   "human_review_required": True}
        if uncited or invalid_entries:
            metrics.append(_fail(
                "GROUNDING",
                f"FACT 条目中 {len(uncited)} 条没有引用、{len(invalid_entries)} 条引用无效；"
                "（结构性代理，语义支持仍需人工复核）", details=details))
        else:
            metrics.append(_pass(
                "GROUNDING",
                f"{len(facts_entries)} 条 FACT 条目均带合法引用（结构性代理，"
                "语义支持仍需人工复核）。", details=details))
    # ------------------------------------------------------------ human metrics
    review_items = "、".join(item.review_id for item in scenario.human_review[:5]) or "—"
    for metric_id in HUMAN_METRICS:
        definition = METRIC_DEFINITIONS[metric_id]
        mode = HUMAN_ASSISTED if "Human-assisted" in definition.kind else HUMAN
        metrics.append(MetricResult(
            metric_id, NOT_APPLICABLE, mode,
            f"需要人工复核：自动评估不判断语义（复核清单：{review_items}）。",
            (scenario.source_path,), {"human_review_items": [item.review_id
                                                             for item in scenario.human_review]}))
    return tuple(metrics)


def failed_metrics(reason: str, scenario: EvaluationScenario) -> tuple:
    """N/A rows for a failed call: nothing was measured, so nothing passes."""
    metrics = [_n(metric_id, reason)
               for metric_id in ("PARSE_VALIDITY", "REQUIRED_SECTIONS", "CITATION_VALIDITY",
                                 "CROSS_WORKSPACE_LEAKAGE", "FACT_COVERAGE", "GROUNDING")]
    review_items = "、".join(item.review_id for item in scenario.human_review[:5]) or "—"
    for metric_id in HUMAN_METRICS:
        metrics.append(MetricResult(metric_id, NOT_APPLICABLE, HUMAN,
                                    f"请求失败，无回答可复核（复核清单：{review_items}）。",
                                    (scenario.source_path,)))
    return tuple(metrics)


def _pass(metric_id, reason, *, details=None) -> MetricResult:
    return MetricResult(metric_id, PASS, DETERMINISTIC, reason, (), dict(details or {}))


def _fail(metric_id, reason, *, details=None) -> MetricResult:
    return MetricResult(metric_id, FAIL, DETERMINISTIC, reason, (), dict(details or {}))


def _n(metric_id, reason) -> MetricResult:
    return MetricResult(metric_id, NOT_APPLICABLE, DETERMINISTIC, reason, ())


# ------------------------------------------------------------------- run flows
def _request_for(scenario, materialized):
    spec = scenario.request
    return AnalysisRequest(
        workspace_id=materialized.workspace_of(spec.target_type, spec.target_key),
        target_type=spec.target_type,
        target_id=materialized.resolve(spec.target_type, spec.target_key),
        analysis_type=spec.analysis_type, as_of=spec.as_of, time_scope=spec.time_scope,
        include_relationships=spec.include_relationships,
        include_history=spec.include_history,
        include_observations=spec.include_observations,
        include_evidence=spec.include_evidence,
        include_memories=spec.include_memories,
        selected_event_ids=tuple(materialized.resolve("EVENT", key)
                                 for key in spec.selected_event_keys),
        selected_evidence_ids=tuple(materialized.resolve("EVIDENCE", key)
                                    for key in spec.selected_evidence_keys),
        selected_memory_ids=tuple(materialized.resolve("MEMORY", key)
                                  for key in spec.selected_memory_keys),
        custom_start=spec.custom_start, custom_end=spec.custom_end,
        max_context_tokens=spec.max_context_tokens)


def _request_identity(snapshot, scenario, *, model_label, model_name) -> dict:
    return {"provider_name": model_label if model_label else None,
            "requested_model": model_name,
            "model_label": model_label,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_hash": prompt_template_hash(),
            "context_snapshot_hash": context_snapshot_hash(snapshot),
            "analysis_schema_version": SCHEMA_VERSION,
            "target_type": scenario.request.target_type,
            "analysis_type": scenario.request.analysis_type,
            "time_scope": scenario.request.time_scope,
            "as_of": scenario.request.as_of.isoformat(),
            "include": {"evidence": scenario.request.include_evidence,
                        "memories": scenario.request.include_memories}}


def _chat_request(model_name: str, messages) -> ChatRequest:
    return ChatRequest(model=model_name, messages=tuple(messages))


def _resolve_placeholders(content, materialized) -> str:
    return PLACEHOLDER_PATTERN.sub(
        lambda match: str(materialized.resolve(match.group(1), match.group(2))), content)


def _evaluate_one(scenario, snapshot, repositories, materialized, *, label,
                  model_name, request: ChatRequest, outcome, mode) -> ModelScenarioResult:
    identity = _request_identity(snapshot, scenario, model_label=label,
                                 model_name=model_name)
    identity["request_hash"] = request_payload_hash(request)
    expected_targets = {
        fact.fact_id: (fact.source.type, str(materialized.resolve(fact.source.type,
                                                                  fact.source.key)))
        for fact in scenario.expected_facts}
    if outcome.status != "SUCCESS":
        return ModelScenarioResult(
            scenario_id=scenario.scenario_id, scenario_version=scenario.scenario_version,
            scenario_path=scenario.source_path, model_label=label, mode=mode,
            request=identity, outcome=outcome, parse_status=None, references=(),
            metrics=failed_metrics(f"请求失败（{outcome.error_class or '未知错误'}），"
                                   "无回答可评估。", scenario))
    content = outcome.answer or ""
    parsed = parse_analysis_result(content)
    verifier = ReferenceVerifier(repositories,
                                 workspace_id=UUID(snapshot.scope["workspace_id"]),
                                 manifest=snapshot.entity_manifest,
                                 recorded={kind: frozenset(values)
                                           for kind, values in snapshot.source_ids().items()})
    checks = verifier.verify_parsed_result(parsed)
    references, seen = [], set()
    for check in checks:
        reference = check.reference
        key = (reference.entity_type,
               str(reference.entity_id) if reference.entity_id else reference.raw)
        if key in seen:
            continue
        seen.add(key)
        references.append(reference)
    for reference in verifier.verify_markers(content):
        key = (reference.entity_type,
               str(reference.entity_id) if reference.entity_id else reference.raw)
        if key in seen:
            continue
        seen.add(key)
        references.append(reference)
    metrics = evaluate_answer(scenario, parsed, references, expected_targets)
    return ModelScenarioResult(
        scenario_id=scenario.scenario_id, scenario_version=scenario.scenario_version,
        scenario_path=scenario.source_path, model_label=label, mode=mode,
        request=identity, outcome=outcome, parse_status=parsed.parse_status,
        references=tuple(references), metrics=metrics)


def _select_scenarios(dataset, scenario_ids, *, require_model_answers):
    scenarios = dataset.scenarios
    if scenario_ids:
        wanted = set(scenario_ids)
        missing = wanted - {item.scenario_id for item in dataset.scenarios}
        if missing:
            raise KeyError(f"未知 scenario_id：{', '.join(sorted(missing))}")
        scenarios = tuple(item for item in scenarios if item.scenario_id in wanted)
    selected, skipped = [], []
    for scenario in scenarios:
        if require_model_answers and not scenario.model_answers:
            skipped.append(scenario.scenario_id)
            continue
        selected.append(scenario)
    return tuple(selected), tuple(skipped)


def run_model_evaluation(dataset: EvaluationDataset, *, work_dir, model_labels=None,
                         scenario_ids=None, run_id=None) -> ModelEvaluationRunResult:
    """Offline comparison of fixed synthetic answers (no provider, no network)."""
    started = utc_now()
    scenarios, skipped = _select_scenarios(dataset, scenario_ids,
                                           require_model_answers=True)
    labels = tuple(model_labels) if model_labels else tuple(sorted(
        {answer.model_label for scenario in scenarios for answer in scenario.model_answers}))
    if not labels:
        raise ValueError("数据集没有可比较的固定模型答案。")
    specs = tuple(ModelEvaluationSpec(label=label, mode=OFFLINE_MODE) for label in labels)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for index, scenario in enumerate(scenarios):
        fixture = dataset.fixtures[scenario.fixture_id]
        materialized = materialize_fixture(fixture, root / f"{index:02d}-{scenario.scenario_id}")
        try:
            with transaction(materialized.runtime.sessions) as session:
                repositories = sql_analysis_repositories(session)
                snapshot = ContextBuilder(repositories).build(_request_for(scenario, materialized))
                messages = PromptBuilder().build(snapshot)
                answers = [answer for answer in scenario.model_answers
                           if answer.model_label in labels]
                for answer in answers:
                    content = _resolve_placeholders(answer.content, materialized)
                    request = ChatRequest(model=answer.model_label,
                                          messages=tuple(messages))
                    outcome = ModelCallOutcome(status="SUCCESS", answer=content,
                                               attempts=None, duration_ms=0)
                    results.append(_evaluate_one(
                        scenario, snapshot, repositories, materialized,
                        label=answer.model_label, model_name=answer.model_label,
                        request=request, outcome=outcome, mode=OFFLINE_MODE))
        finally:
            materialized.close()
    return ModelEvaluationRunResult(
        run_id=run_id or str(uuid4()), mode=OFFLINE_MODE,
        dataset_id=dataset.dataset_id, dataset_version=dataset.dataset_version,
        dataset_digest=dataset.digest, prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_hash=prompt_template_hash(), analysis_schema_version=SCHEMA_VERSION,
        models=specs, scenario_ids=tuple(item.scenario_id for item in scenarios),
        results=tuple(results), skipped_scenarios=skipped,
        started_at=started, completed_at=utc_now())


def run_online_model_evaluation(dataset: EvaluationDataset, *, gateway, provider_name,
                                model_name=None, work_dir, scenario_ids=None,
                                run_id=None) -> ModelEvaluationRunResult:
    """Call one explicitly selected live provider/model; failures stay reported."""
    started = utc_now()
    entry = gateway.config().provider(provider_name)
    requested_model = entry.model
    if model_name is not None and model_name != requested_model:
        raise ValueError(
            f"在线评估只能使用已配置 Provider 的模型（{requested_model!r}）；"
            "如需其他模型请先更新 Provider 配置，避免把不同参数当成同一次对比。")
    scenarios, _skipped = _select_scenarios(dataset, scenario_ids,
                                            require_model_answers=False)
    spec = ModelEvaluationSpec(label=entry.provider_name, mode=ONLINE_MODE,
                               provider_name=entry.provider_name,
                               model_name=requested_model)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for index, scenario in enumerate(scenarios):
        fixture = dataset.fixtures[scenario.fixture_id]
        materialized = materialize_fixture(fixture, root / f"{index:02d}-{scenario.scenario_id}")
        try:
            with transaction(materialized.runtime.sessions) as session:
                repositories = sql_analysis_repositories(session)
                snapshot = ContextBuilder(repositories).build(_request_for(scenario, materialized))
            messages = PromptBuilder().build(snapshot)
            request = ChatRequest(model=requested_model, messages=tuple(messages),
                                  temperature=entry.temperature, timeout=entry.timeout,
                                  extra_headers=entry.extra_headers)
            wall = time.monotonic()
            try:
                response, attempts = gateway.chat_with_attempts(
                    request, provider_name=entry.provider_name)
                duration_ms = int((time.monotonic() - wall) * 1000)
                outcome = ModelCallOutcome(
                    status="SUCCESS", answer=response.content, attempts=attempts,
                    duration_ms=duration_ms, returned_model=response.model or None,
                    finish=finish_reason(response.finish_reason),
                    usage=usage_summary(response.usage))
                with transaction(materialized.runtime.sessions) as session:
                    repositories = sql_analysis_repositories(session)
                    result = _evaluate_one(
                        scenario, snapshot, repositories, materialized,
                        label=entry.provider_name, model_name=requested_model,
                        request=request, outcome=outcome, mode=ONLINE_MODE)
            except LLMError as exc:
                duration_ms = int((time.monotonic() - wall) * 1000)
                outcome = ModelCallOutcome(
                    status="FAILED", error_class=type(exc).__name__,
                    error_message=str(exc), timed_out=isinstance(exc, LLMTimeoutError),
                    attempts=None, duration_ms=duration_ms)
                with transaction(materialized.runtime.sessions) as session:
                    repositories = sql_analysis_repositories(session)
                    result = _evaluate_one(
                        scenario, snapshot, repositories, materialized,
                        label=entry.provider_name, model_name=requested_model,
                        request=request, outcome=outcome, mode=ONLINE_MODE)
            results.append(result)
        finally:
            materialized.close()
    return ModelEvaluationRunResult(
        run_id=run_id or str(uuid4()), mode=ONLINE_MODE,
        dataset_id=dataset.dataset_id, dataset_version=dataset.dataset_version,
        dataset_digest=dataset.digest, prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_hash=prompt_template_hash(), analysis_schema_version=SCHEMA_VERSION,
        models=(spec,), scenario_ids=tuple(item.scenario_id for item in scenarios),
        results=tuple(results), skipped_scenarios=(),
        started_at=started, completed_at=utc_now())


# ---------------------------------------------------------------- aggregation
def aggregate_models(result: ModelEvaluationRunResult) -> dict:
    """Per-model aggregates: counts, failure rate, latency, usage, finish reasons."""
    aggregates = {}
    for spec in result.models:
        rows = result.for_model(spec.label)
        successes = [row for row in rows if row.outcome.status == "SUCCESS"]
        failures = len(rows) - len(successes)
        # Fixed-answer runs have no real provider call: latency is not observed.
        durations = ([row.outcome.duration_ms for row in successes]
                     if spec.mode == ONLINE_MODE else [])
        usages = [row.outcome.usage for row in successes if row.outcome.usage]
        available = [item for item in usages if item.get("available")]
        metric_counts = {}
        for row in rows:
            for metric in row.metrics:
                bucket = metric_counts.setdefault(metric.metric_id,
                                                  {"PASS": 0, "FAIL": 0, "N/A": 0})
                key = "N/A" if metric.status == NOT_APPLICABLE else metric.status
                bucket[key] += 1
        finish_counts = {}
        for row in successes:
            category = (row.outcome.finish or {}).get("category", "UNAVAILABLE")
            finish_counts[category] = finish_counts.get(category, 0) + 1
        aggregates[spec.label] = {
            "mode": spec.mode, "provider_name": spec.provider_name,
            "model_name": spec.model_name,
            "calls": len(rows), "successes": len(successes), "failures": failures,
            "failure_rate": (failures / len(rows)) if rows else 0.0,
            "latency_ms": {"available": bool(durations),
                           "average": round(statistics.mean(durations), 1) if durations else None,
                           "median": round(statistics.median(durations), 1) if durations else None},
            "usage": {"available": bool(available),
                      "runs_with_usage": len(available),
                      "prompt_tokens": sum(item["prompt_tokens"] for item in available
                                           if item.get("prompt_tokens") is not None)
                      if available else None,
                      "completion_tokens": sum(item["completion_tokens"] for item in available
                                               if item.get("completion_tokens") is not None)
                      if available else None,
                      "total_tokens": sum(item["total_tokens"] for item in available
                                          if item.get("total_tokens") is not None)
                      if available else None},
            "finish_reasons": finish_counts,
            "metrics": metric_counts,
            "scenarios": [row.scenario_id for row in rows],
            "failures_detail": [{"scenario_id": row.scenario_id,
                                 "error_class": row.outcome.error_class,
                                 "timed_out": row.outcome.timed_out,
                                 "error_message": row.outcome.error_message}
                                for row in rows if row.outcome.status != "SUCCESS"],
        }
    return aggregates


# -------------------------------------------------------------- human review
def human_review_items(result: ModelEvaluationRunResult, dataset: EvaluationDataset) -> tuple:
    """Scenario-level human review items plus model answers needing manual reading."""
    items = []
    for scenario in dataset.scenarios:
        if scenario.scenario_id not in result.scenario_ids:
            continue
        for item in scenario.human_review:
            items.append({"scenario_id": scenario.scenario_id,
                          "review_id": item.review_id, "question": item.question,
                          "rationale": item.rationale})
    return tuple(items)
