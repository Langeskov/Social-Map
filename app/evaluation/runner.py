"""Offline evaluation runner (Phase 13.0 / 6.6).

Runs a validated synthetic dataset through the *real* ContextBuilder and
PromptBuilder and verifies everything that is deterministically checkable about
the model *input*. It never calls a provider: model-output metrics are reported
as N/A with an explicit reason until their evaluators land in Phase 13.1/13.2.

Reproducibility contract: with the same dataset bytes the same scenario rebuilds
an identical context. The only volatile values are `run_id`, the run timestamps
and Memory.confirmed_at (rendered as 「确认于 …」, owned by the service clock);
`mask_volatile_text` masks ISO timestamps so runs can be compared, and the run
result declares its volatile fields explicitly.
"""
import hashlib
import re
from pathlib import Path
from uuid import uuid4

from app.domain.validation import utc_now
from app.evaluation.dataset import scenario_digest
from app.evaluation.fixture import materialize_fixture
from app.evaluation.models import (DETERMINISTIC, EXPECTED_ERROR_CONTEXT_LIMIT,
                                   FAIL, HUMAN, HUMAN_ASSISTED, METRIC_DEFINITIONS,
                                   NOT_APPLICABLE, OUTCOME_BUILT, OUTCOME_ERROR,
                                   OUTCOME_REFUSED_LIMIT, PASS, REFERENCE_BASED,
                                   ContextOutcome, ContextRef, EvaluationDataset,
                                   EvaluationRunResult, EvaluationScenario,
                                   FixtureSpec, MetricResult,
                                   ScenarioEvaluationResult)
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.context import AnalysisRequest, ContextBuilder
from app.services.entity_manifest import (CONTENT_INCLUDED, EVIDENCE,
                                          entity_marker, unmarked_entity_ids)
from app.services.prompt import PromptBuilder
from app.services.unit_of_work import transaction

LIMIT_ERROR_MARKER = "Selected context exceeds model context limit."
_GUIDANCE_MARKERS = ("缩", "范围", "勾选", "减少")

_ISO_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+[0-9:]{2,6}|Z)?")
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def mask_volatile_text(text: str) -> str:
    """Mask wall-clock timestamps and materialized row UUIDs.

    Both are run-local volatile values: timestamps come from the service clock
    (e.g. Memory.confirmed_at) and row UUIDs are assigned at materialization
    time. Everything else — keys, texts, fixture times, structure — must
    reproduce byte-for-byte between identical runs.
    """
    masked = _UUID_PATTERN.sub("<UUID>", text or "")
    return _ISO_PATTERN.sub("<TIMESTAMP>", masked)


# --------------------------------------------------------------------- run API
def run_dataset(dataset: EvaluationDataset, *, work_dir: str | Path,
                scenario_ids: tuple[str, ...] | None = None,
                run_id: str | None = None) -> EvaluationRunResult:
    """Run every (or the selected) scenario offline and return structured results."""
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    chosen = dataset.scenarios
    if scenario_ids:
        wanted = set(scenario_ids)
        missing = wanted - {item.scenario_id for item in dataset.scenarios}
        if missing:
            raise KeyError(f"未知 scenario_id：{', '.join(sorted(missing))}")
        chosen = tuple(item for item in dataset.scenarios if item.scenario_id in wanted)
    results = []
    for index, scenario in enumerate(chosen):
        scenario_dir = root / f"{index:02d}-{scenario.scenario_id}"
        results.append(run_scenario(scenario, dataset, work_dir=scenario_dir))
    return EvaluationRunResult(
        run_id=run_id or str(uuid4()),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_digest=dataset.digest,
        started_at=started,
        completed_at=utc_now(),
        scenario_results=tuple(results))


def run_scenario(scenario: EvaluationScenario, dataset: EvaluationDataset, *,
                 work_dir: str | Path) -> ScenarioEvaluationResult:
    """Run exactly one scenario: seed, build context+prompt, verify, report."""
    fixture = dataset.fixtures[scenario.fixture_id]
    scenario_path = f"{scenario.source_path} (digest {scenario_digest(dataset, scenario)[:12]})"
    metrics: list[MetricResult] = []
    metrics.append(_dataset_integrity(dataset, scenario))
    try:
        materialized = materialize_fixture(fixture, Path(work_dir))
    except Exception as exc:  # seeding failure is a run error, never a fact change
        context = ContextOutcome(built=False, error=f"夹具物化失败：{exc}")
        metrics.append(_fail("CONTEXT_EXPECTATIONS",
                             f"夹具物化失败，无法验证预期上下文：{exc}", (scenario_path,)))
        metrics.extend(_not_applicable_context_checks("夹具物化失败，无上下文可核对。"))
        metrics.extend(_model_output_na(scenario))
        return ScenarioEvaluationResult(
            scenario_id=scenario.scenario_id, scenario_version=scenario.scenario_version,
            scenario_path=scenario_path, fixture_id=scenario.fixture_id,
            outcome=OUTCOME_ERROR, context=context, metrics=tuple(metrics),
            evaluation_dimensions=scenario.evaluation_dimensions)

    with materialized:
        try:
            request = _analysis_request(scenario, materialized)
            with transaction(materialized.runtime.sessions) as session:
                snapshot = ContextBuilder(sql_analysis_repositories(session)).build(request)
            messages = PromptBuilder().build(snapshot)
        except Exception as exc:
            return _refusal_result(scenario, scenario_path, metrics, exc)

        context = _context_outcome(snapshot, materialized, scenario)
        prompt_text = "\n".join(message.content for message in messages)
        metrics.append(_context_binding(scenario, snapshot, materialized, scenario_path))
        metrics.append(_prompt_leakage(scenario, prompt_text, snapshot, scenario_path))
        metrics.append(_token_bound(scenario, snapshot, scenario_path))
        metrics.append(_context_isolation(fixture, snapshot, materialized, scenario,
                                          scenario_path))
        metrics.append(_context_expectations(scenario, snapshot, materialized, fixture,
                                             scenario_path))
        metrics.append(_exposure_manifest(scenario, snapshot, scenario_path))
        metrics.extend(_model_output_na(scenario))
        return ScenarioEvaluationResult(
            scenario_id=scenario.scenario_id, scenario_version=scenario.scenario_version,
            scenario_path=scenario_path, fixture_id=scenario.fixture_id,
            outcome=OUTCOME_BUILT, context=context, metrics=tuple(metrics),
            evaluation_dimensions=scenario.evaluation_dimensions)


# ------------------------------------------------------------------ internals
def _analysis_request(scenario, materialized) -> AnalysisRequest:
    spec = scenario.request

    def selected(kind, keys):
        return tuple(materialized.resolve(kind, key) for key in keys)

    return AnalysisRequest(
        workspace_id=materialized.workspace_of(spec.target_type, spec.target_key),
        target_type=spec.target_type,
        target_id=materialized.resolve(spec.target_type, spec.target_key),
        analysis_type=spec.analysis_type,
        as_of=spec.as_of,
        time_scope=spec.time_scope,
        include_relationships=spec.include_relationships,
        include_history=spec.include_history,
        include_observations=spec.include_observations,
        include_evidence=spec.include_evidence,
        include_memories=spec.include_memories,
        selected_event_ids=selected("EVENT", spec.selected_event_keys),
        selected_evidence_ids=selected("EVIDENCE", spec.selected_evidence_keys),
        selected_memory_ids=selected("MEMORY", spec.selected_memory_keys),
        custom_start=spec.custom_start,
        custom_end=spec.custom_end,
        max_context_tokens=spec.max_context_tokens)


def _context_outcome(snapshot, materialized, scenario) -> ContextOutcome:
    bindings = {f"{scenario.request.target_type}:{scenario.request.target_key}":
                str(materialized.resolve(scenario.request.target_type, scenario.request.target_key))}
    for source in scenario.required_sources:
        bindings[source.token] = str(materialized.resolve(source.type, source.key))
    for fact in scenario.expected_facts:
        bindings[fact.source.token] = str(materialized.resolve(fact.source.type, fact.source.key))
    for ref in scenario.expected_context.included + scenario.expected_context.excluded:
        bindings[ref.token] = str(materialized.resolve(ref.type, ref.key))
    rendered = snapshot.rendered_context
    return ContextOutcome(
        built=True,
        estimated_tokens=snapshot.estimated_tokens,
        context_chars=len(rendered),
        rendered_digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        source_bindings=bindings,
        scope={"workspace_id": snapshot.scope.get("workspace_id"),
               "as_of": snapshot.scope.get("as_of"),
               "time_window": snapshot.scope.get("time_window"),
               "include": snapshot.scope.get("include"),
               "masked_digest": hashlib.sha256(
                   mask_volatile_text(rendered).encode("utf-8")).hexdigest()})


def _refusal_result(scenario, scenario_path, metrics, exc):
    message = str(exc)
    expected_limit = scenario.expected_context.expected_error == EXPECTED_ERROR_CONTEXT_LIMIT
    is_limit = LIMIT_ERROR_MARKER in message
    if is_limit:
        context = ContextOutcome(built=False, error=message)
        ok = _guidance(message)
        metrics.append(MetricResult(
            "TOKEN_ESTIMATE_BOUND", PASS if ok else FAIL, DETERMINISTIC,
            "上下文超预算时抛出明确错误并给出缩小范围的指引，未静默截断。" if ok
            else "超限错误缺少用户指引。", (scenario_path,), {"error": message}))
        if expected_limit:
            metrics.append(_pass("CONTEXT_EXPECTATIONS",
                                 "按预期被上下文预算拒绝"
                                 f"（expected_error={EXPECTED_ERROR_CONTEXT_LIMIT}，"
                                 "显式错误，非截断）。", (scenario_path,)))
        else:
            metrics.append(_fail("CONTEXT_EXPECTATIONS",
                                 "未预期的上下文超限拒绝。", (scenario_path,)))
        metrics.extend(_not_applicable_context_checks("上下文未构建（超限拒绝），无快照可核对。"))
    else:
        context = ContextOutcome(built=False, error=message)
        metrics.append(MetricResult(
            "TOKEN_ESTIMATE_BOUND", NOT_APPLICABLE, DETERMINISTIC,
            "上下文构建因非超限错误失败，无估算可核对。", (scenario_path,), {"error": message}))
        reason = ("预期超限拒绝，实际是其他错误。" if expected_limit
                  else f"上下文构建失败：{message}")
        metrics.append(_fail("CONTEXT_EXPECTATIONS", reason, (scenario_path,)))
        metrics.extend(_not_applicable_context_checks("上下文构建失败，无快照可核对。"))
    metrics.extend(_model_output_na(scenario))
    outcome = OUTCOME_REFUSED_LIMIT if is_limit else OUTCOME_ERROR
    return ScenarioEvaluationResult(
        scenario_id=scenario.scenario_id, scenario_version=scenario.scenario_version,
        scenario_path=scenario_path, fixture_id=scenario.fixture_id, outcome=outcome,
        context=context, metrics=tuple(metrics),
        evaluation_dimensions=scenario.evaluation_dimensions)


def _guidance(message: str) -> bool:
    return any(marker in message for marker in _GUIDANCE_MARKERS)


def _dataset_integrity(dataset, scenario) -> MetricResult:
    return _pass("DATASET_INTEGRITY",
                 "加载期 schema、唯一性、引用完整性与合成来源声明全部通过（fail-closed）。",
                 (scenario.source_path, f"dataset digest {dataset.digest[:12]}"))


def _context_binding(scenario, snapshot, materialized, scenario_path) -> MetricResult:
    source_ids = snapshot.source_ids()
    failures = []
    wanted = list(scenario.required_sources) + [fact.source for fact in scenario.expected_facts]
    for source in wanted:
        resolved = str(materialized.resolve(source.type, source.key))
        if resolved not in tuple(source_ids.get(source.type, ())):
            failures.append(f"{source.token}（{resolved}）不在本次快照的来源集合中")
    if failures:
        return _fail("CONTEXT_BINDING",
                     "所需来源未绑定到本次 ContextSnapshot：" + "；".join(failures),
                     (scenario_path,))
    return _pass("CONTEXT_BINDING",
                 f"{len(wanted)} 个所需来源全部出现在本次快照的来源集合中。",
                 (scenario_path,))


def _prompt_leakage(scenario, prompt_text, snapshot, scenario_path) -> MetricResult:
    corpus = scenario.reference_answer_tokens
    leaked = [token for token in corpus if token and token in prompt_text]
    in_context = [token for token in corpus if token and token in snapshot.rendered_context]
    if leaked or in_context:
        return _fail("PROMPT_LEAKAGE",
                     "参考答案文本出现在模型请求中：" + "；".join(leaked or in_context),
                     (scenario_path,))
    return _pass("PROMPT_LEAKAGE",
                 f"{len(corpus)} 条参考答案文本均未进入上下文与提示词。",
                 (scenario_path,))


def _token_bound(scenario, snapshot, scenario_path) -> MetricResult:
    limit = scenario.request.max_context_tokens
    estimated = snapshot.estimated_tokens
    details = {"estimated_tokens": estimated, "max_context_tokens": limit,
               "context_chars": len(snapshot.rendered_context)}
    if estimated > limit:
        return _fail("TOKEN_ESTIMATE_BOUND",
                     f"估算 {estimated} 超过预算 {limit} 却仍然构建成功。", (scenario_path,), details)
    return _pass("TOKEN_ESTIMATE_BOUND",
                 f"估算 {estimated} ≤ 预算 {limit}（估算保守计数，非 Provider 实际用量）。",
                 (scenario_path,), details)


def _context_isolation(fixture, snapshot, materialized, scenario, scenario_path) -> MetricResult:
    target_workspace = fixture.workspace_key_of(scenario.request.target_type,
                                                scenario.request.target_key)
    rendered = snapshot.rendered_context
    source_ids = set()
    for values in snapshot.source_ids().values():
        source_ids.update(values)
    source_ids.update(snapshot.scope.get("included_memory_ids", ()))
    leaks = []
    for person in fixture.people:
        if person.workspace != target_workspace:
            if person.display_name and person.display_name in rendered:
                leaks.append(f"异 Workspace 人物名 {person.display_name!r}")
    for event in fixture.all_events():
        if event.workspace != target_workspace and event.title in rendered:
            leaks.append(f"异 Workspace 事件标题 {event.title!r}")
    for item in fixture.all_evidence():
        if item.workspace != target_workspace and item.title in rendered:
            leaks.append(f"异 Workspace 证据标题 {item.title!r}")
    for memory in fixture.memories:
        marker = memory.content[:16]
        if memory.workspace != target_workspace and marker and marker in rendered:
            leaks.append(f"异 Workspace Memory 文本 {marker!r}")
    for token, identifier in materialized.ids.items():
        ref_type, _, key = token.partition(":")
        if ref_type not in ("PERSON", "RELATIONSHIP", "EVENT", "EVIDENCE", "MEMORY"):
            continue
        if materialized.workspace_key_of(ref_type, key) != target_workspace \
                and str(identifier) in source_ids:
            leaks.append(f"异 Workspace 记录 {token} 进入来源集合")
    if leaks:
        return _fail("CONTEXT_ISOLATION",
                     "上下文混入其他 Workspace 的记录：" + "；".join(leaks), (scenario_path,))
    return _pass("CONTEXT_ISOLATION",
                 "目标 Workspace 之外的合成记录均未出现在上下文与来源集合中。",
                 (scenario_path,))


def _context_expectations(scenario, snapshot, materialized, fixture, scenario_path) -> MetricResult:
    expected = scenario.expected_context
    source_ids = snapshot.source_ids()
    memories = set(snapshot.scope.get("included_memory_ids", ()))
    problems = []

    def present(ref: ContextRef) -> bool:
        resolved = str(materialized.resolve(ref.type, ref.key))
        if ref.type == "MEMORY":
            return resolved in memories
        return resolved in tuple(source_ids.get(ref.type, ()))

    for ref in expected.included:
        if not present(ref):
            problems.append(f"应包含 {ref.token}，但不在快照来源集合中")
    for ref in expected.excluded:
        if present(ref):
            problems.append(f"不应包含 {ref.token}，却出现在快照来源集合中")
        marker = _marker_for(fixture, ref.type, ref.key)
        if marker and marker in snapshot.rendered_context:
            problems.append(f"不应包含 {ref.token}，其文本 {marker!r} 出现在上下文中")
    for text in expected.required_text:
        if text not in snapshot.rendered_context:
            problems.append(f"上下文缺少必需文本 {text!r}")
    for text in expected.forbidden_text:
        if text in snapshot.rendered_context:
            problems.append(f"上下文出现禁含文本 {text!r}")
    if expected.expected_error == EXPECTED_ERROR_CONTEXT_LIMIT:
        problems.append("预期上下文超限拒绝，但上下文构建成功")
    if problems:
        return _fail("CONTEXT_EXPECTATIONS", "；".join(problems), (scenario_path,))
    return _pass("CONTEXT_EXPECTATIONS",
                 f"预期上下文断言全部成立（含 {len(expected.included)} 条包含、"
                 f"{len(expected.excluded)} 条排除、{len(expected.required_text)} 条必含文本、"
                 f"{len(expected.forbidden_text)} 条禁含文本）。",
                 (scenario_path,))


def _marker_for(fixture: FixtureSpec, ref_type: str, key: str) -> str:
    if ref_type == "PERSON":
        for person in fixture.people:
            if person.key == key:
                return person.display_name
    elif ref_type == "EVENT":
        for event in fixture.all_events():
            if event.key == key:
                return event.title
    elif ref_type == "EVIDENCE":
        for item in fixture.all_evidence():
            if item.key == key:
                return item.title
    elif ref_type == "MEMORY":
        for memory in fixture.memories:
            if memory.key == key:
                return memory.content[:16]
    return ""


def _model_output_na(scenario) -> list[MetricResult]:
    rows = []
    for metric_id in scenario.evaluation_dimensions:
        definition = METRIC_DEFINITIONS.get(metric_id)
        if definition is None or definition.available_in_13_0:
            continue
        rows.append(MetricResult(metric_id, NOT_APPLICABLE,
                                 _mode_for(definition), definition.na_reason,
                                 (scenario.source_path,)))
    return rows


def _mode_for(definition) -> str:
    if definition.needs_human and definition.needs_reference_answer:
        return HUMAN_ASSISTED if "Human-assisted" in definition.kind else REFERENCE_BASED
    if definition.needs_human:
        return HUMAN
    if definition.needs_reference_answer:
        return REFERENCE_BASED
    return DETERMINISTIC


def _pass(metric_id: str, reason: str, evidence=(), details=None) -> MetricResult:
    return MetricResult(metric_id, PASS, DETERMINISTIC, reason, tuple(evidence),
                        dict(details or {}))


def _fail(metric_id: str, reason: str, evidence=(), details=None) -> MetricResult:
    return MetricResult(metric_id, FAIL, DETERMINISTIC, reason, tuple(evidence),
                        dict(details or {}))


def _not_applicable_context_checks(reason: str) -> list[MetricResult]:
    return [MetricResult(metric_id, NOT_APPLICABLE, DETERMINISTIC, reason)
            for metric_id in ("CONTEXT_BINDING", "PROMPT_LEAKAGE", "CONTEXT_ISOLATION",
                              "CONTEXT_EXPECTATIONS", "EXPOSURE_MANIFEST")]


def _exposure_manifest(scenario, snapshot, scenario_path) -> MetricResult:
    """Manifest <-> markers <-> source_ids consistency (Phase 13.1)."""
    manifest = snapshot.entity_manifest
    rendered = snapshot.rendered_context
    problems = []
    if not manifest.entries:
        return _fail("EXPOSURE_MANIFEST", "缺少 Context Entity Manifest。", (scenario_path,))
    for entry in manifest.entries:
        if not entry.exposures:
            problems.append(f"{entry.entity_type}:{entry.entity_id} 没有曝光等级")
        if entity_marker(entry.entity_type, entry.entity_id) not in rendered:
            problems.append(f"{entry.entity_type}:{entry.entity_id} 在上下文中没有标记")
    unmarked = unmarked_entity_ids(rendered)
    if unmarked:
        problems.append("上下文存在未标记的 UUID：" + "、".join(unmarked[:5]))
    derived = {kind: tuple(values) for kind, values in manifest.source_ids().items()}
    observed = {kind: tuple(values) for kind, values in snapshot.source_ids().items()}
    if derived != observed:
        problems.append("source_ids() 与 Manifest 不一致")
    if not scenario.request.include_evidence:
        overstated = [entry.label or str(entry.entity_id)
                      for entry in manifest.by_type(EVIDENCE)
                      if CONTENT_INCLUDED in entry.exposures]
        if overstated:
            problems.append("证据原文未开启却被标记为已提供原文：" + "、".join(overstated))
    if problems:
        return _fail("EXPOSURE_MANIFEST", "；".join(problems), (scenario_path,))
    return _pass("EXPOSURE_MANIFEST",
                 f"Manifest {len(manifest.entries)} 个实体：标记、source_ids 与曝光等级一致。",
                 (scenario_path,), {"manifest_version": manifest.version,
                                    "entities": len(manifest.entries)})
