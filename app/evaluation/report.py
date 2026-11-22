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

MODEL_LIMITATIONS = (
    "自动指标只判断结构（解析、必需小节、引用合法性、引用覆盖、跨 Workspace 隔离）与"
    "结构性来源指向（GROUNDING 代理）；它们不证明结论语义上被材料支持。",
    "语义质量（禁止断言、矛盾处理、不确定性表述、完整性、稳健性）一律为人工复核项，"
    "报告列出复核清单；本报告不是模型排行榜，也不给出单一总分。",
    "离线固定答案模式没有真实 Provider：latency 恒为 0、usage/finish_reason 不可用；"
    "在线模式每个运行只调用一个明确选择的 Provider/模型，失败样本保留在报告中。",
    "不同运行之间只有在 Dataset 版本、Scenario 版本、ContextSnapshot、Prompt 版本、"
    "输出契约与请求参数一致时才可比较；跨运行比较前请先核对 manifest.json。",
    "LLM Judge 不是裁判：任何 Judge 结论只能作为实验记录，不能写回事实层或自动决定 Memory。",
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


# ------------------------------------------------------ model evaluation report
def render_model_markdown(result, *, dataset=None) -> str:
    """Human-readable model-evaluation report (no leaderboard, no single score)."""
    from app.evaluation.model_evaluation import aggregate_models, human_review_items
    aggregates = aggregate_models(result)
    lines = [f"# Model Evaluation Report — {result.run_id}", "",
             f"- Mode: `{result.mode}`",
             f"- Dataset: `{result.dataset_id}` version {result.dataset_version}",
             f"- Dataset digest: `{result.dataset_digest}`",
             f"- Prompt template: v{result.prompt_template_version} · hash `{result.prompt_hash[:16]}…`",
             f"- Output schema: {result.analysis_schema_version}",
             f"- Started: {result.started_at.isoformat() if result.started_at else '—'}"
             f" · Completed: {result.completed_at.isoformat() if result.completed_at else '—'}",
             f"- Scenarios: {', '.join(result.scenario_ids) or '—'}",
             "", "## 模型与调用", "",
             "| Label | Mode | Provider | Model | Calls | Success | Failures | Failure rate |",
             "|---|---|---|---|---|---|---|---|"]
    for spec in result.models:
        agg = aggregates[spec.label]
        lines.append(f"| {spec.label} | {spec.mode} | {spec.provider_name or '—'} | "
                     f"{spec.model_name or '—'} | {agg['calls']} | {agg['successes']} | "
                     f"{agg['failures']} | {agg['failure_rate']:.2f} |")
    lines += ["", "## 自动指标（按模型计数）", "",
              "| Metric | " + " | ".join(spec.label for spec in result.models) + " |",
              "|---|" + "---|" * len(result.models)]
    metric_ids = sorted({metric.metric_id for row in result.results for metric in row.metrics})
    for metric_id in metric_ids:
        cells = []
        for spec in result.models:
            bucket = aggregates[spec.label]["metrics"].get(metric_id, {})
            cells.append(f"PASS {bucket.get('PASS', 0)} · FAIL {bucket.get('FAIL', 0)} · "
                         f"N/A {bucket.get('N/A', 0)}")
        lines.append(f"| {metric_id} | " + " | ".join(cells) + " |")
    lines += ["", "## 观测值（无通过阈值，仅报告）", "",
              "| Label | Latency avg/median (ms) | Usage total | Finish reasons |",
              "|---|---|---|---|"]
    for spec in result.models:
        agg = aggregates[spec.label]
        latency = agg["latency_ms"]
        usage = agg["usage"]
        latency_text = (f"{latency['average']} / {latency['median']}"
                        if latency["available"] else "N/A（固定答案无真实调用）")
        usage_text = (str(usage["total_tokens"]) if usage["available"]
                      else "unavailable（Provider 未返回 usage）")
        finishes = "、".join(f"{key}={value}" for key, value in
                             sorted(agg["finish_reasons"].items())) or "N/A"
        lines.append(f"| {spec.label} | {latency_text} | {usage_text} | {finishes} |")
    # ------------------------------------------------ cross-model differences
    by_scenario = {}
    for row in result.results:
        by_scenario.setdefault(row.scenario_id, {})[row.model_label] = row
    labels = [spec.label for spec in result.models]
    lines += ["", "## 同一 Scenario 的差异（仅状态差异，不构成排名）", ""]
    differences = []
    for scenario_id, rows in sorted(by_scenario.items()):
        if len(rows) < 2:
            continue
        metric_ids_for_scenario = sorted({metric.metric_id for row in rows.values()
                                          for metric in row.metrics})
        for metric_id in metric_ids_for_scenario:
            statuses = {label: (rows[label].metric(metric_id).status
                                if rows[label] and rows[label].metric(metric_id) else "N/A")
                        for label in labels if label in rows}
            if len(set(statuses.values())) > 1:
                differences.append((scenario_id, metric_id,
                                    " · ".join(f"{label}={status}"
                                               for label, status in statuses.items())))
    if differences:
        for scenario_id, metric_id, detail in differences:
            lines.append(f"- {scenario_id} / {metric_id}：{detail}")
    else:
        lines.append("（没有状态差异）")
    # ------------------------------------------------------- failure samples
    lines += ["", "## 失败样本（不得静默移除）", ""]
    failures = [row for row in result.results if row.outcome.status != "SUCCESS"]
    if not failures:
        lines.append("（无请求失败）")
    for row in failures:
        lines.append(f"- {row.model_label} / {row.scenario_id}："
                     f"{row.outcome.error_class} · timeout={row.outcome.timed_out} · "
                     f"{row.outcome.error_message or ''}")
    # --------------------------------------------------- human review needed
    lines += ["", "## 需要人工复核", ""]
    review = human_review_items(result, dataset) if dataset is not None else ()
    if not review:
        lines.append("（数据集未提供复核清单或未传入 dataset）")
    for item in review:
        lines.append(f"- {item['scenario_id']} / {item['review_id']}：{item['question']}")
    lines += ["", "## 局限", ""] + [f"- {item}" for item in MODEL_LIMITATIONS]
    lines.append("")
    return "\n".join(lines)


def write_model_report(result, output_dir, *, dataset=None) -> Path:
    """Write manifest.json / raw-results.jsonl / metrics.json / report.md."""
    from app.evaluation.model_evaluation import aggregate_models
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    aggregates = aggregate_models(result)
    manifest = {
        "run_id": result.run_id, "mode": result.mode,
        "dataset_id": result.dataset_id, "dataset_version": result.dataset_version,
        "dataset_digest": result.dataset_digest,
        "prompt_template_version": result.prompt_template_version,
        "prompt_hash": result.prompt_hash,
        "analysis_schema_version": result.analysis_schema_version,
        "models": [{"label": spec.label, "mode": spec.mode,
                    "provider_name": spec.provider_name, "model_name": spec.model_name}
                   for spec in result.models],
        "scenario_ids": list(result.scenario_ids),
        "skipped_scenarios": list(result.skipped_scenarios),
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "counts": {label: {"calls": agg["calls"], "successes": agg["successes"],
                           "failures": agg["failures"],
                           "failure_rate": agg["failure_rate"]}
                   for label, agg in aggregates.items()},
        "volatile_fields": ["run_id", "started_at", "completed_at",
                            "duration_ms (offline fixed answers = 0)"],
        "limitations": list(MODEL_LIMITATIONS),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (target / "raw-results.jsonl").open("w", encoding="utf-8") as handle:
        for row in result.results:
            handle.write(json.dumps({
                "scenario_id": row.scenario_id,
                "scenario_version": row.scenario_version,
                "model_label": row.model_label, "mode": row.mode,
                "request": row.request,
                "outcome": row.outcome.to_mapping(),
                "parse_status": row.parse_status,
                "references": [reference.to_mapping() for reference in row.references],
                "metrics": [{"metric_id": metric.metric_id, "status": metric.status,
                             "evaluation_mode": metric.evaluation_mode,
                             "reason": metric.reason,
                             "details": dict(metric.details)}
                            for metric in row.metrics],
            }, ensure_ascii=False) + "\n")
    metrics = {"aggregates": aggregates,
               "by_scenario_model": [
                   {"scenario_id": row.scenario_id, "model_label": row.model_label,
                    "mode": row.mode, "outcome": row.outcome.status,
                    "parse_status": row.parse_status,
                    "metrics": [{"metric_id": metric.metric_id, "status": metric.status,
                                 "evaluation_mode": metric.evaluation_mode,
                                 "reason": metric.reason,
                                 "details": dict(metric.details)}
                                for metric in row.metrics]}
                   for row in result.results]}
    (target / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "report.md").write_text(
        render_model_markdown(result, dataset=dataset), encoding="utf-8")
    return target
