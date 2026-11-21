"""Evaluation data contracts for Phase 13.0.

Four different objects stay distinct and must never be merged:

- raw model output (a string; never a fact),
- parsed analysis (a parse product; Phase 13.1),
- validated references (checked source ids; Phase 13.2),
- evaluated analysis (quality findings; this package).

This module only defines *evaluation-side* DTOs: the versioned dataset contract
(synthetic input + reference expectations) and the result structures the offline
runner emits. Nothing here writes to the fact layer, calls a model or touches
the network. See docs/phase13-evaluation.md for the dataset specification.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID

# --------------------------------------------------------------------- versions
#: Version of the dataset/scenario JSON schema this loader understands.
SCHEMA_VERSION = "1"
#: Version of the bundled synthetic dataset content.
DATASET_VERSION = "1.1"

# ------------------------------------------------------------------ source kinds
#: Fact types an AnalysisSource may cite (kept identical to the analysis layer).
SOURCE_TYPES = ("PERSON", "RELATIONSHIP", "EVENT", "EVIDENCE")
#: Memory is user-confirmed USER-layer content; contexts may include it but an
#: AnalysisSource may not cite it as a fact.
MEMORY_TYPE = "MEMORY"
CONTEXT_REF_TYPES = SOURCE_TYPES + (MEMORY_TYPE,)

TARGET_TYPES = ("PERSON", "RELATIONSHIP", "EVENT")
ANALYSIS_TYPES = ("PERSON_PROFILE", "RELATIONSHIP_ANALYSIS", "EVENT_ANALYSIS")

TIME_SCOPES = ("LAST_7_DAYS", "LAST_30_DAYS", "LAST_90_DAYS", "ALL", "CUSTOM")

# ------------------------------------------------------------- metric vocabulary
PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "N/A"
STATUSES = (PASS, FAIL, NOT_APPLICABLE)

#: How a metric result was produced. Human items are never reported as verified.
DETERMINISTIC = "DETERMINISTIC"
REFERENCE_BASED = "REFERENCE_BASED"
HUMAN = "HUMAN"
HUMAN_ASSISTED = "HUMAN_ASSISTED"

#: The runner outcome for one scenario.
OUTCOME_BUILT = "CONTEXT_BUILT"
OUTCOME_REFUSED_LIMIT = "CONTEXT_REFUSED_LIMIT"
OUTCOME_ERROR = "CONTEXT_ERROR"

#: Declared expected error for a scenario (never a silent truncation).
EXPECTED_ERROR_CONTEXT_LIMIT = "CONTEXT_LIMIT"


class DatasetError(Exception):
    """Raised when a dataset violates the evaluation contract.

    Carries every issue found so a broken dataset is diagnosable in one pass.
    """

    def __init__(self, issues):
        self.issues = tuple(issues)
        summary = "; ".join(f"{item.path}: {item.message}" for item in self.issues[:8])
        if len(self.issues) > 8:
            summary += f" …（共 {len(self.issues)} 项）"
        super().__init__(f"评估数据集不合法：{summary}")


@dataclass(frozen=True)
class DatasetIssue:
    path: str
    message: str


# ----------------------------------------------------------------- input contract
@dataclass(frozen=True)
class SourceRef:
    """A typed reference to one fixture record key (resolved to a UUID at run)."""

    type: str
    key: str

    def __post_init__(self):
        if self.type not in SOURCE_TYPES:
            raise ValueError(f"SourceRef.type 必须是 {', '.join(SOURCE_TYPES)} 之一，得到 {self.type!r}")
        if not self.key:
            raise ValueError("SourceRef.key 不能为空")

    @property
    def token(self) -> str:
        return f"{self.type}:{self.key}"


@dataclass(frozen=True)
class ContextRef:
    """Like SourceRef but also allows MEMORY (context-level assertions)."""

    type: str
    key: str

    def __post_init__(self):
        if self.type not in CONTEXT_REF_TYPES:
            raise ValueError(f"ContextRef.type 必须是 {', '.join(CONTEXT_REF_TYPES)} 之一，得到 {self.type!r}")
        if not self.key:
            raise ValueError("ContextRef.key 不能为空")

    @property
    def token(self) -> str:
        return f"{self.type}:{self.key}"


@dataclass(frozen=True)
class ExpectedFact:
    """One reference-answer fact: what a correct analysis should cover."""

    fact_id: str
    source: SourceRef
    statement: str
    note: str = ""


@dataclass(frozen=True)
class ForbiddenClaim:
    """A claim the analysis must never make (e.g. fabricated records, scores)."""

    claim_id: str
    description: str
    rationale: str = ""


@dataclass(frozen=True)
class ExpectedUncertainty:
    """An uncertainty a correct analysis should express."""

    uncertainty_id: str
    description: str
    reason: str = ""


@dataclass(frozen=True)
class HumanReviewItem:
    """Something only a human (or assisted review) can judge."""

    review_id: str
    question: str
    rationale: str = ""


@dataclass(frozen=True)
class RequestSpec:
    """The analysis request half of the scenario input context."""

    target_type: str
    target_key: str
    analysis_type: str
    as_of: datetime
    time_scope: str = "LAST_30_DAYS"
    include_relationships: bool = True
    include_history: bool = True
    include_observations: bool = True
    include_evidence: bool = False
    include_memories: bool = False
    selected_event_keys: tuple[str, ...] = ()
    selected_evidence_keys: tuple[str, ...] = ()
    selected_memory_keys: tuple[str, ...] = ()
    custom_start: datetime | None = None
    custom_end: datetime | None = None
    max_context_tokens: int = 24000


@dataclass(frozen=True)
class ExpectedReferenceOutcome:
    """One reference a sample model answer is expected to contain, and its status."""

    ref_type: str
    expected_status: str
    source: SourceRef | None = None     # fixture key resolved to a real UUID
    literal_id: UUID | None = None      # explicit UUID (e.g. a fabricated one)


@dataclass(frozen=True)
class SampleOutput:
    """A fixed synthetic model answer used for offline reference-grounding checks.

    ``content`` may embed ``<<TYPE:key>>`` placeholders that the runner replaces
    with the materialized UUIDs before parsing, so expectations stay readable.
    """

    label: str
    content: str
    expected_references: tuple[ExpectedReferenceOutcome, ...] = ()
    expected_missing_fact_references: int = 0


@dataclass(frozen=True)
class ExpectedContext:
    """Deterministic expectations about the *input* the model will see.

    These are the checks Phase 13.0 can verify without any model output.
    """

    included: tuple[ContextRef, ...] = ()
    excluded: tuple[ContextRef, ...] = ()
    required_text: tuple[str, ...] = ()
    forbidden_text: tuple[str, ...] = ()
    expected_error: str | None = None


@dataclass(frozen=True)
class EvaluationScenario:
    """One versioned evaluation task bound to one synthetic fixture."""

    scenario_id: str
    scenario_version: str
    dataset_version: str
    title: str
    description: str
    fixture_id: str
    request: RequestSpec
    expected_facts: tuple[ExpectedFact, ...]
    required_sources: tuple[SourceRef, ...]
    forbidden_claims: tuple[ForbiddenClaim, ...]
    expected_uncertainties: tuple[ExpectedUncertainty, ...]
    human_review: tuple[HumanReviewItem, ...]
    evaluation_dimensions: tuple[str, ...]
    expected_context: ExpectedContext
    sample_output: SampleOutput | None = None
    notes: str = ""
    source_path: str = ""

    @property
    def reference_answer_tokens(self) -> tuple[str, ...]:
        """Strings that must never appear in a model request (leak corpus)."""
        parts = [fact.statement for fact in self.expected_facts]
        parts += [claim.description for claim in self.forbidden_claims]
        parts += [item.description for item in self.expected_uncertainties]
        return tuple(part for part in parts if part)


# --------------------------------------------------------------- fixture records
@dataclass(frozen=True)
class AliasSpec:
    alias: str
    alias_type: str = "OTHER"
    note: str = ""


@dataclass(frozen=True)
class PersonSpec:
    key: str
    workspace: str
    display_name: str
    real_name: str = ""
    gender: str = ""
    occupation: str = ""
    organization: str = ""
    location: str = ""
    birthday: date | None = None
    notes: str = ""
    aliases: tuple[AliasSpec, ...] = ()


@dataclass(frozen=True)
class HistorySpec:
    state: str
    effective_at: datetime
    note: str = ""


@dataclass(frozen=True)
class RelationshipSpec:
    key: str
    workspace: str
    person_a: str
    person_b: str
    relationship_type: str
    directionality: str = "UNDIRECTED"
    initial_state: str = "ACTIVE"
    effective_at: datetime | None = None
    note: str = ""
    history: tuple[HistorySpec, ...] = ()


@dataclass(frozen=True)
class ParticipantSpec:
    person: str
    role: str = ""
    note: str = ""


@dataclass(frozen=True)
class EventSpec:
    key: str
    workspace: str
    title: str
    event_type: str
    record_kind: str = "FACT"
    occurred_on: date | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    location: str = ""
    description: str = ""
    participants: tuple[ParticipantSpec, ...] = ()


@dataclass(frozen=True)
class MediaSpec:
    key: str
    filename: str
    content: str = "synthetic evaluation attachment payload"


@dataclass(frozen=True)
class EvidenceSpec:
    key: str
    workspace: str
    event: str
    kind: str
    title: str
    source_type: str = "MANUAL"
    content_text: str = ""
    annotation: str = ""
    source_label: str = ""
    source_reference: str = ""
    captured_at: datetime | None = None
    media: tuple[MediaSpec, ...] = ()


@dataclass(frozen=True)
class MemorySpec:
    key: str
    workspace: str
    content: str
    kind: str = "CONTEXT"
    person: str = ""
    valid_from: datetime | None = None
    valid_until: datetime | None = None


@dataclass(frozen=True)
class BulkEventSpec:
    """Deterministic expansion of many synthetic events (scenario J budgets)."""

    key_prefix: str
    title_prefix: str
    workspace: str
    person: str
    count: int
    description_template: str
    event_type: str = "Meeting"
    record_kind: str = "FACT"
    occurred_on_base: date | None = None
    occurred_on_step_days: int = -1

    def key_at(self, index: int) -> str:
        return f"{self.key_prefix}-{index:03d}"

    def title_at(self, index: int) -> str:
        return f"{self.title_prefix} {index:03d}"

    def description_at(self, index: int) -> str:
        return self.description_template.replace("{index}", f"{index:03d}")


@dataclass(frozen=True)
class BulkEvidenceSpec:
    key_prefix: str
    title_prefix: str
    workspace: str
    event_key_prefix: str
    count: int
    content_template: str
    kind: str = "TEXT"
    source_type: str = "DOCUMENT"

    def key_at(self, index: int) -> str:
        return f"{self.key_prefix}-{index:03d}"

    def title_at(self, index: int) -> str:
        return f"{self.title_prefix} {index:03d}"

    def event_key_at(self, index: int) -> str:
        return f"{self.event_key_prefix}-{index:03d}"

    def content_at(self, index: int) -> str:
        return self.content_template.replace("{index}", f"{index:03d}")


BULK_LIMIT = 500


@dataclass(frozen=True)
class FixtureSpec:
    """One synthetic workspace universe. All names and texts are fictional."""

    fixture_id: str
    fixture_version: str
    description: str
    workspaces: tuple[tuple[str, str], ...]          # (key, name)
    people: tuple[PersonSpec, ...]
    relationships: tuple[RelationshipSpec, ...]
    events: tuple[EventSpec, ...]
    evidence: tuple[EvidenceSpec, ...]
    memories: tuple[MemorySpec, ...]
    bulk_events: tuple[BulkEventSpec, ...] = ()
    bulk_evidence: tuple[BulkEvidenceSpec, ...] = ()
    synthetic: bool = True
    source_path: str = ""

    def workspace_key_of(self, kind: str, key: str) -> str:
        for person in self.people:
            if kind == "PERSON" and person.key == key:
                return person.workspace
        for relationship in self.relationships:
            if kind == "RELATIONSHIP" and relationship.key == key:
                return relationship.workspace
        for event in self.all_events():
            if kind == "EVENT" and event.key == key:
                return event.workspace
        for item in self.all_evidence():
            if kind == "EVIDENCE" and item.key == key:
                return item.workspace
        for memory in self.memories:
            if kind == "MEMORY" and memory.key == key:
                return memory.workspace
        raise KeyError(f"{kind}:{key}")

    def all_events(self) -> tuple[EventSpec, ...]:
        expanded: list[EventSpec] = list(self.events)
        for bulk in self.bulk_events:
            for index in range(bulk.count):
                occurred_on = None
                if bulk.occurred_on_base is not None:
                    occurred_on = bulk.occurred_on_base + timedelta(
                        days=index * bulk.occurred_on_step_days)
                expanded.append(EventSpec(
                    key=bulk.key_at(index), workspace=bulk.workspace,
                    title=bulk.title_at(index), event_type=bulk.event_type,
                    record_kind=bulk.record_kind, occurred_on=occurred_on,
                    description=bulk.description_at(index),
                    participants=(ParticipantSpec(person=bulk.person),)))
        return tuple(expanded)

    def all_evidence(self) -> tuple[EvidenceSpec, ...]:
        expanded: list[EvidenceSpec] = list(self.evidence)
        for bulk in self.bulk_evidence:
            for index in range(bulk.count):
                expanded.append(EvidenceSpec(
                    key=bulk.key_at(index), workspace=bulk.workspace,
                    event=bulk.event_key_at(index),
                    kind=bulk.kind, title=bulk.title_at(index),
                    source_type=bulk.source_type, content_text=bulk.content_at(index)))
        return tuple(expanded)


@dataclass(frozen=True)
class EvaluationDataset:
    dataset_id: str
    dataset_version: str
    title: str
    description: str
    fixtures: dict[str, FixtureSpec]
    scenarios: tuple[EvaluationScenario, ...]
    source_dir: str = ""
    digest: str = ""
    file_digests: dict = field(default_factory=dict)

    def scenario(self, scenario_id: str) -> EvaluationScenario:
        for item in self.scenarios:
            if item.scenario_id == scenario_id:
                return item
        raise KeyError(scenario_id)


# ---------------------------------------------------------------- result objects
@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    name: str
    kind: str                    # mirrors the evaluation table's 类型 column
    description: str
    method: str                  # how it is computed
    scope: str                   # where it applies
    needs_reference_answer: bool
    needs_human: bool
    available_in_13_0: bool
    na_reason: str               # why a run reports N/A before its evaluator exists


#: The initial metric registry (Phase 13.0 / 6.4). Results are always reported
#: per metric with status + reason + evidence; there is no single quality score.
METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    item.metric_id: item for item in (
        MetricDefinition(
            "PARSE_VALIDITY", "Parse Validity", "Deterministic",
            "模型输出能否按结果契约解析。",
            "由 Phase 13.1 的独立解析器对 raw output 解析；解析失败保留原文并分类错误。",
            "模型输出（13.1 起）", False, False, False,
            "结果解析器在 Phase 13.1 提供；本阶段没有模型输出可解析。"),
        MetricDefinition(
            "REQUIRED_SECTIONS", "Required Sections", "Deterministic",
            "必须结构（Facts / Observations / Interpretations / Uncertainties）是否齐全。",
            "解析后检查必需小节是否存在且非空策略符合契约。",
            "模型输出（13.1 起）", False, False, False,
            "结构化结果契约在 Phase 13.1 提供。"),
        MetricDefinition(
            "CITATION_VALIDITY", "Citation Validity", "Deterministic",
            "引用是否真实存在、属于本次 AnalysisSource 集合且类型匹配。",
            "逐条核对引用 UUID、类型、Run 归属、Workspace 归属与删除状态（13.2）。",
            "模型输出中的引用（13.2 起）", False, False, False,
            "来源引用校验在 Phase 13.2 提供。"),
        MetricDefinition(
            "CROSS_WORKSPACE_LEAKAGE", "Cross-workspace Leakage", "Deterministic",
            "输出是否引用了其他 Workspace 的内容。",
            "引用校验中拒绝跨 Workspace 引用（13.2）；输入侧隔离由 CONTEXT_ISOLATION 覆盖。",
            "模型输出中的引用（13.2 起）", False, False, False,
            "输出侧跨 Workspace 检查在 Phase 13.2 提供。"),
        MetricDefinition(
            "FACT_COVERAGE", "Fact Coverage", "Reference-based",
            "标准事实清单是否被正确覆盖。",
            "对照 expected_facts 与模型输出小节做参考答案比对；不评价语言润色。",
            "模型输出 vs 参考答案", True, False, False,
            "参考答案比对评估器随 Phase 13.1/13.2 的结果契约提供。"),
        MetricDefinition(
            "UNSUPPORTED_CLAIMS", "Unsupported Claims", "Reference-based / Human",
            "是否产生缺少依据的断言。",
            "自动检查引用合法性与 forbidden_claims 命中；语义是否有据由人工复核确认。",
            "模型输出 vs 参考答案 + 人工", True, True, False,
            "断言支撑性判定需要 13.1/13.2 的解析与引用校验，再叠加人工复核。"),
        MetricDefinition(
            "UNCERTAINTY_HANDLING", "Uncertainty Handling", "Reference-based / Human",
            "是否正确说明缺失与矛盾（expected_uncertainties）。",
            "参考答案比对 + 人工确认表述是否恰当，不做自动语义判决。",
            "模型输出 vs 参考答案 + 人工", True, True, False,
            "需要解析后的 Uncertainties 小节（Phase 13.1）。"),
        MetricDefinition(
            "EPISTEMIC_SEPARATION", "Epistemic Separation", "Reference-based / Human",
            "是否区分 FACT / OBSERVATION / INFERENCE，不把观察或推断写成事实。",
            "解析后小节归属检查 + 人工确认；禁止用关键词正则冒充语义验证。",
            "模型输出 + 人工", True, True, False,
            "需要解析后的分节结果（Phase 13.1）与人工复核。"),
        MetricDefinition(
            "COMPLETENESS", "Completeness", "Human-assisted",
            "是否覆盖任务范围内的关键信息。",
            "人工对照 expected_facts 与任务说明打分记录；不做自动总分。",
            "人工评价", True, True, False,
            "人工复核工作流随 Phase 13.4 提供；本阶段仅登记 human_review 清单。"),
        MetricDefinition(
            "ROBUSTNESS", "Robustness", "Deterministic / Human",
            "不同输入边界下是否稳定（长上下文、稀疏、矛盾等）。",
            "确定性部分由上下文边界检查覆盖（TOKEN_ESTIMATE_BOUND 等）；输出稳定性由多模型评估覆盖（13.4）。",
            "上下文边界 + 模型输出", True, True, False,
            "输出层稳定性评估在 Phase 13.4 提供；上下文边界行为已由本阶段确定性检查覆盖。"),
        # ------------------------------------------------- Phase 13.0 computable
        MetricDefinition(
            "DATASET_INTEGRITY", "Dataset Integrity", "Deterministic",
            "数据集 schema、唯一性与引用完整性是否全部通过。",
            "加载期 fail-closed 校验：schema 版本、ID 唯一、expected_facts/required_sources 的 key 与类型均存在。",
            "评估数据集", False, False, True,
            ""),
        MetricDefinition(
            "CONTEXT_BINDING", "Context Binding", "Deterministic",
            "所需来源是否真实出现在本次 ContextSnapshot 的来源集合中。",
            "把 fixture key 解析为真实 UUID 后核对 snapshot.source_ids() 与 included_memory_ids。",
            "上下文构建", False, False, True,
            ""),
        MetricDefinition(
            "PROMPT_LEAKAGE", "Prompt Leakage", "Deterministic",
            "参考答案（expected_facts / forbidden_claims / expected_uncertainties）是否泄漏进模型请求。",
            "对 PromptBuilder 三段消息做全串包含检查；参考答案文本绝不出现在请求里。",
            "上下文 + 提示词", False, False, True,
            ""),
        MetricDefinition(
            "TOKEN_ESTIMATE_BOUND", "Token Estimate Bound", "Deterministic",
            "上下文估算是否落在预算内，超限时是否明确拒绝而非静默截断。",
            "核对 estimate_tokens 结果与 max_context_tokens；超限必须抛出带用户指引的错误。",
            "上下文构建", False, False, True,
            ""),
        MetricDefinition(
            "CONTEXT_ISOLATION", "Context Isolation", "Deterministic",
            "其他 Workspace 的合成记录是否绝不出现在上下文中。",
            "对非目标 Workspace 的所有 fixture 记录做标记文本与 UUID 双重缺席检查。",
            "上下文构建", False, False, True,
            ""),
        MetricDefinition(
            "CONTEXT_EXPECTATIONS", "Context Expectations", "Deterministic",
            "场景声明的预期上下文断言（含入/排除来源、必含/禁含文本、预期错误）是否全部成立。",
            "逐条核对 expected_context 与真实 ContextSnapshot；预期的超限拒绝也算成立。",
            "上下文构建", False, False, True,
            ""),
        MetricDefinition(
            "EXPOSURE_MANIFEST", "Exposure Manifest", "Deterministic",
            "Context Entity Manifest 与 source_ids、持久化来源集合、上下文实体标记是否完全一致，"
            "且曝光等级如实反映实际提供的内容。",
            "13.1 起：核对 Manifest 条目与渲染标记 [TYPE:<uuid>]、source_ids()、analysis_*_source，"
            "并检查 Evidence METADATA_ONLY / PERSON IDENTITY_ONLY 等等级不被夸大。",
            "上下文构建 + 来源集", False, False, True,
            ""),
        MetricDefinition(
            "REFERENCE_GROUNDING", "Reference Grounding", "Deterministic",
            "固定样例回答中的每条结构化引用是否被验证为预期状态（真实 / 有限 / 未知 / "
            "跨 Workspace / 类型错误 / 未曝光），以及 FACT 条目是否缺少引用。",
            "13.2 起：解析样例输出 → 共享 ReferenceVerifier 逐条验证 → 与场景声明的"
            " expected_references 比对；引用合法性不等于语义有据。",
            "样例模型输出 vs 来源集合", True, False, True,
            ""),
    )
}


@dataclass(frozen=True)
class MetricResult:
    metric_id: str
    status: str
    evaluation_mode: str
    reason: str
    evidence: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    @property
    def is_na(self) -> bool:
        return self.status == NOT_APPLICABLE


@dataclass(frozen=True)
class ContextOutcome:
    built: bool
    estimated_tokens: int = 0
    context_chars: int = 0
    rendered_digest: str = ""
    source_bindings: dict = field(default_factory=dict)   # "TYPE:key" -> uuid str
    scope: dict = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class ScenarioEvaluationResult:
    scenario_id: str
    scenario_version: str
    scenario_path: str
    fixture_id: str
    outcome: str
    context: ContextOutcome
    metrics: tuple[MetricResult, ...]
    evaluation_dimensions: tuple[str, ...] = ()

    def metric(self, metric_id: str) -> MetricResult | None:
        for item in self.metrics:
            if item.metric_id == metric_id:
                return item
        return None

    @property
    def failed(self) -> tuple[MetricResult, ...]:
        return tuple(item for item in self.metrics if item.status == FAIL)


@dataclass(frozen=True)
class EvaluationRunResult:
    run_id: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    started_at: datetime
    completed_at: datetime
    scenario_results: tuple[ScenarioEvaluationResult, ...]
    volatile_fields: tuple[str, ...] = (
        "run_id", "started_at", "completed_at",
        "context.generated_at",
        "memory confirmed_at (rendered as 「确认于 …」)",
        "materialized row UUIDs (rendered as id=…/event=…/快照来源 id)",
    )

    @property
    def total_failures(self) -> int:
        return sum(len(item.failed) for item in self.scenario_results)

    @property
    def total_na(self) -> int:
        return sum(1 for item in self.scenario_results for metric in item.metrics if metric.is_na)

    def to_mapping(self) -> dict:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_digest": self.dataset_digest,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "volatile_fields": list(self.volatile_fields),
            "scenarios": [
                {
                    "scenario_id": item.scenario_id,
                    "scenario_version": item.scenario_version,
                    "scenario_path": item.scenario_path,
                    "fixture_id": item.fixture_id,
                    "outcome": item.outcome,
                    "context": {
                        "built": item.context.built,
                        "estimated_tokens": item.context.estimated_tokens,
                        "context_chars": item.context.context_chars,
                        "rendered_digest": item.context.rendered_digest,
                        "source_bindings": dict(item.context.source_bindings),
                        "scope": item.context.scope,
                        "error": item.context.error,
                    },
                    "metrics": [
                        {"metric_id": metric.metric_id, "status": metric.status,
                         "evaluation_mode": metric.evaluation_mode, "reason": metric.reason,
                         "evidence": list(metric.evidence), "details": dict(metric.details)}
                        for metric in item.metrics],
                }
                for item in self.scenario_results],
        }
