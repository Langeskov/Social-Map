"""Report rendering for offline evaluation runs.

Both a machine-readable form (manifest.json / raw-results.json / metrics.json)
and a human-readable report.md. The report always separates what was verified
from what was not: FAIL and N/A are different states and there is no single
composite quality score.
"""
import json
from pathlib import Path

from app.evaluation.models import (FAIL, METRIC_DEFINITIONS, NOT_APPLICABLE,
                                   EvaluationRunResult)

LIMITATIONS = (
    "本报告只验证列明的检查项。它不验证：模型是否真的理解事实、推断是否合理、"
    "语言是否夸大原始材料、两份材料是否构成语义矛盾——这些需要人工复核或后续阶段的评估器。",
    "Token 数为离线保守估算，不是 Provider 实际用量；实际用量以 Phase 13.3 记录的 usage 元数据为准。",
    "N/A 表示本次运行没有能力评估该项（例如尚无模型输出），绝不表示通过，也不以 0 分代替。",
    "评估数据集全部为虚构合成数据；结果不包含任何真实个人资料。",
)


def render_markdown(result: EvaluationRunResult) -> str:
    lines = [f"# Evaluation Report — {result.run_id}", ""]
    lines += [
        f"- Dataset: `{result.dataset_id}` version {result.dataset_version}",
        f"- Dataset digest: `{result.dataset_digest}`",
        f"- Started: {result.started_at.isoformat()} · Completed: {result.completed_at.isoformat()}",
        f"- Volatile fields (excluded from reproducibility): {', '.join(result.volatile_fields)}",
        "",
        "## 本次验证了什么",
        "",
        "- DATASET_INTEGRITY：数据集 schema、唯一性、引用完整性（fail-closed）。",
        "- CONTEXT_BINDING：所需来源真实绑定到本次 ContextSnapshot。",
        "- PROMPT_LEAKAGE：参考答案未泄漏进模型请求。",
        "- TOKEN_ESTIMATE_BOUND：上下文估算不超预算，超限明确拒绝而非静默截断。",
        "- CONTEXT_ISOLATION：其他 Workspace 的记录不进入上下文。",
        "- CONTEXT_EXPECTATIONS：场景声明的预期上下文断言（含入/排除与文本）。",
        "- EXPOSURE_MANIFEST：Manifest ↔ source_ids ↔ 上下文实体标记一致，曝光等级不夸大（13.1 起）。",
        "",
        "## 本次没有验证什么",
        "",
    ]
    lines += [f"- {item}" for item in LIMITATIONS]
    lines += ["", "## 汇总", "",
              "| Scenario | Outcome | Est. tokens | FAIL | N/A |", "|---|---|---|---|---|"]
    for item in result.scenario_results:
        fails = len(item.failed)
        na = sum(1 for metric in item.metrics if metric.is_na)
        lines.append(f"| {item.scenario_id} | {item.outcome} | "
                     f"{item.context.estimated_tokens} | {fails} | {na} |")
    lines += ["", "## 分项结果", ""]
    for item in result.scenario_results:
        lines += [f"### {item.scenario_id}（{item.outcome}）", "",
                  f"- 文件：{item.scenario_path}",
                  f"- 上下文：{'已构建' if item.context.built else '未构建'}"
                  f" · 估算 {item.context.estimated_tokens} tokens"
                  f" · {item.context.context_chars} 字符"]
        if item.context.error:
            lines.append(f"- 错误：{item.context.error}")
        lines.append("")
        lines.append("| 指标 | 状态 | 模式 | 原因 | 证据 |")
        lines.append("|---|---|---|---|---|")
        for metric in item.metrics:
            evidence = "、".join(metric.evidence) or "—"
            reason = metric.reason.replace("|", "\\|")
            lines.append(f"| {metric.metric_id} | {metric.status} | "
                         f"{metric.evaluation_mode} | {reason} | {evidence} |")
        lines.append("")
    failures = [(item, metric) for item in result.scenario_results
                for metric in item.metrics if metric.status == FAIL]
    lines += ["## 失败样本", ""]
    if not failures:
        lines.append("（无）")
    for item, metric in failures:
        lines += [f"- **{item.scenario_id} / {metric.metric_id}**：{metric.reason}",
                  f"  - 样本：{item.scenario_path}"]
    na_rows = [(item, metric) for item in result.scenario_results
               for metric in item.metrics if metric.status == NOT_APPLICABLE]
    lines += ["", "## N/A（未评估，不等于通过）", ""]
    if not na_rows:
        lines.append("（无）")
    for item, metric in na_rows:
        lines.append(f"- {item.scenario_id} / {metric.metric_id}：{metric.reason}")
    lines += ["", "## 指标定义", "",
              "| 指标 | 类型 | 计算方法 | 参考答案 | 人工 | 13.0 可用 |", "|---|---|---|---|---|---|"]
    for definition in METRIC_DEFINITIONS.values():
        lines.append(f"| {definition.metric_id} | {definition.kind} | {definition.method} | "
                     f"{'是' if definition.needs_reference_answer else '否'} | "
                     f"{'是' if definition.needs_human else '否'} | "
                     f"{'是' if definition.available_in_13_0 else '否'} |")
    lines.append("")
    return "\n".join(lines)


def write_report(result: EvaluationRunResult, output_dir: str | Path) -> Path:
    """Write manifest.json / raw-results.json / metrics.json / report.md."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps({
        "run_id": result.run_id,
        "dataset_id": result.dataset_id,
        "dataset_version": result.dataset_version,
        "dataset_digest": result.dataset_digest,
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
        "volatile_fields": list(result.volatile_fields),
        "scenario_count": len(result.scenario_results),
        "failure_count": result.total_failures,
        "na_count": result.total_na,
        "metric_ids": sorted(METRIC_DEFINITIONS),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "raw-results.json").write_text(
        json.dumps(result.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    rows = [{"scenario_id": item.scenario_id, "metric_id": metric.metric_id,
             "status": metric.status, "evaluation_mode": metric.evaluation_mode,
             "reason": metric.reason, "evidence": list(metric.evidence)}
            for item in result.scenario_results for metric in item.metrics]
    (target / "metrics.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "report.md").write_text(render_markdown(result), encoding="utf-8")
    return target
