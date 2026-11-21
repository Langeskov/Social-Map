"""Offline evaluation runner (Phase 13.0 / 6.6, 6.7).

Covers: every scenario runs offline through the real ContextBuilder/PromptBuilder,
no network access, no production-database writes, reproducible results modulo
declared volatile fields, explicit refusal instead of silent truncation, and the
strict difference between FAIL and N/A.
"""
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from app.evaluation import (NOT_APPLICABLE, FAIL, PASS, load_dataset, run_dataset,
                            run_scenario)
from app.evaluation.models import EXPECTED_ERROR_CONTEXT_LIMIT

DATASET_DIR = Path(__file__).parent / "dataset"

EXPECTED_OUTCOMES = {
    "SCENARIO_A_COMPLETE": "CONTEXT_BUILT",
    "SCENARIO_B_SPARSE": "CONTEXT_BUILT",
    "SCENARIO_C_NO_SOURCE": "CONTEXT_BUILT",
    "SCENARIO_D_CONFLICT": "CONTEXT_BUILT",
    "SCENARIO_E_EPISTEMIC": "CONTEXT_BUILT",
    "SCENARIO_F_RELATIONSHIP_CHANGE": "CONTEXT_BUILT",
    "SCENARIO_G_LAST_7_DAYS": "CONTEXT_BUILT",
    "SCENARIO_G_LAST_30_DAYS": "CONTEXT_BUILT",
    "SCENARIO_G_LAST_90_DAYS": "CONTEXT_BUILT",
    "SCENARIO_G_ALL": "CONTEXT_BUILT",
    "SCENARIO_G_CUSTOM": "CONTEXT_BUILT",
    "SCENARIO_H_EVIDENCE_OFF": "CONTEXT_BUILT",
    "SCENARIO_H_EVIDENCE_ON": "CONTEXT_BUILT",
    "SCENARIO_I_MEMORY_DEFAULT": "CONTEXT_BUILT",
    "SCENARIO_I_MEMORY_ON": "CONTEXT_BUILT",
    "SCENARIO_J_NEAR_LIMIT": "CONTEXT_BUILT",
    "SCENARIO_J_OVER_LIMIT": "CONTEXT_REFUSED_LIMIT",
}


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET_DIR)


@pytest.fixture(scope="module")
def full_run(dataset, tmp_path_factory):
    return run_dataset(dataset, work_dir=tmp_path_factory.mktemp("eval-work"))


def _by_id(run):
    return {item.scenario_id: item for item in run.scenario_results}


# ------------------------------------------------------------ offline run
def test_every_scenario_runs_offline_with_expected_outcome(full_run):
    results = _by_id(full_run)
    assert set(results) == set(EXPECTED_OUTCOMES)
    for scenario_id, expected in EXPECTED_OUTCOMES.items():
        assert results[scenario_id].outcome == expected, scenario_id


def test_canonical_dataset_has_no_failures(full_run):
    for item in full_run.scenario_results:
        assert item.failed == (), (item.scenario_id, item.failed)


def test_context_metrics_pass_and_model_metrics_are_na(full_run):
    item = _by_id(full_run)["SCENARIO_A_COMPLETE"]
    for metric_id in ("DATASET_INTEGRITY", "CONTEXT_BINDING", "PROMPT_LEAKAGE",
                      "TOKEN_ESTIMATE_BOUND", "CONTEXT_ISOLATION", "CONTEXT_EXPECTATIONS"):
        metric = item.metric(metric_id)
        assert metric is not None and metric.status == PASS, metric_id
    # Model-output metrics are N/A with an explicit reason — never a fake pass,
    # never a 0 score.
    for metric_id in ("PARSE_VALIDITY", "CITATION_VALIDITY", "FACT_COVERAGE",
                      "EPISTEMIC_SEPARATION", "COMPLETENESS"):
        metric = item.metric(metric_id)
        assert metric is not None, metric_id
        assert metric.status == NOT_APPLICABLE, metric_id
        assert metric.status != PASS and metric.status != FAIL
        assert metric.reason, metric_id


def test_na_and_fail_are_different_states(dataset, tmp_path):
    # A tampered scenario expectation produces FAIL with a locatable reason…
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    path = target / "scenarios" / "scenario-b-sparse.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["expected_context"]["required_text"].append("绝不会出现的标记文本")
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    broken = load_dataset(target)
    result = run_scenario(broken.scenario("SCENARIO_B_SPARSE"), broken,
                          work_dir=tmp_path / "work")
    expectation = result.metric("CONTEXT_EXPECTATIONS")
    assert expectation.status == FAIL
    assert "绝不会出现的标记文本" in expectation.reason
    assert "scenario-b-sparse.json" in result.scenario_path
    # …while the same run still reports model-output metrics as N/A.
    assert result.metric("FACT_COVERAGE").status == NOT_APPLICABLE
    assert result.metric("FACT_COVERAGE").reason


def test_reference_grounding_checks_sample_outputs(full_run):
    results = _by_id(full_run)
    scenario_a = results["SCENARIO_A_COMPLETE"].metric("REFERENCE_GROUNDING")
    assert scenario_a is not None and scenario_a.status == PASS
    statuses = set(scenario_a.details["references"].values())
    assert {"VALID_FULL", "VALID_LIMITED", "SOURCE_NOT_EXPOSED",
            "WORKSPACE_MISMATCH", "TYPE_MISMATCH", "UNKNOWN_SOURCE"} <= statuses
    conflict = results["SCENARIO_D_CONFLICT"].metric("REFERENCE_GROUNDING")
    assert conflict is not None and conflict.status == PASS
    assert set(conflict.details["references"].values()) == {"VALID_FULL"}
    # A fact entry without any reference is counted, not silently ignored.
    window = results["SCENARIO_G_LAST_7_DAYS"].metric("REFERENCE_GROUNDING")
    assert window is not None and window.status == PASS
    assert window.details["missing_fact_references"] == 1
    assert "SOURCE_NOT_EXPOSED" in set(window.details["references"].values())


def test_reference_grounding_detects_a_wrong_expectation(dataset, tmp_path):
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    path = target / "scenarios" / "scenario-a-complete.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sample_output"]["expected_references"][0]["expected_status"] = "VALID_LIMITED"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    broken = load_dataset(target)
    result = run_scenario(broken.scenario("SCENARIO_A_COMPLETE"), broken,
                          work_dir=tmp_path / "work")
    metric = result.metric("REFERENCE_GROUNDING")
    assert metric.status == FAIL
    assert "期望 VALID_LIMITED，实际 VALID_FULL" in metric.reason


# ------------------------------------------------------- reproducibility
def test_same_inputs_reproduce_same_results(dataset, tmp_path):
    ids = ("SCENARIO_A_COMPLETE", "SCENARIO_G_ALL", "SCENARIO_I_MEMORY_ON")
    first = run_dataset(dataset, work_dir=tmp_path / "r1", scenario_ids=ids)
    second = run_dataset(dataset, work_dir=tmp_path / "r2", scenario_ids=ids)
    assert first.run_id != second.run_id
    left, right = _by_id(first), _by_id(second)
    for scenario_id in ids:
        assert left[scenario_id].outcome == right[scenario_id].outcome
        assert [(m.metric_id, m.status) for m in left[scenario_id].metrics] == \
               [(m.metric_id, m.status) for m in right[scenario_id].metrics]
        # Logical content reproduces byte-for-byte after masking declared
        # volatile values (run-local timestamps and materialized row UUIDs; since
        # Phase 13.1 every rendered entity carries a [TYPE:<uuid>] marker, so the
        # markers are the only UUID-bearing tokens in the context).
        assert left[scenario_id].context.scope["masked_digest"] == \
               right[scenario_id].context.scope["masked_digest"], scenario_id
    # Declared volatile fields are explicit in the run result.
    assert "run_id" in first.volatile_fields


def test_scenario_binding_records_sources_and_run_identity(full_run):
    item = _by_id(full_run)["SCENARIO_A_COMPLETE"]
    bindings = item.context.source_bindings
    assert "PERSON:alice" in bindings and "EVENT:call-night" in bindings
    UUID(bindings["PERSON:alice"])          # resolves to a real UUID
    assert item.scenario_id == "SCENARIO_A_COMPLETE"
    assert "scenario-a-complete.json" in item.scenario_path
    assert item.context.scope["as_of"] == "2026-06-15T12:00:00+00:00"


# ------------------------------------------------------------- budget behaviour
def test_near_limit_scenario_builds_close_to_budget(full_run):
    item = _by_id(full_run)["SCENARIO_J_NEAR_LIMIT"]
    assert item.outcome == "CONTEXT_BUILT"
    limit = 24000
    assert item.context.estimated_tokens <= limit
    assert item.context.estimated_tokens >= limit // 2      # 「接近」预算上限
    metric = item.metric("TOKEN_ESTIMATE_BOUND")
    assert metric.status == PASS
    assert metric.details["max_context_tokens"] == limit


def test_over_limit_scenario_refuses_explicitly_without_truncation(full_run):
    item = _by_id(full_run)["SCENARIO_J_OVER_LIMIT"]
    assert item.outcome == "CONTEXT_REFUSED_LIMIT"
    assert item.context.built is False
    assert "Selected context exceeds model context limit." in item.context.error
    assert "缩小" in item.context.error
    bound = item.metric("TOKEN_ESTIMATE_BOUND")
    assert bound.status == PASS
    assert "未静默截断" in bound.reason
    expectation = item.metric("CONTEXT_EXPECTATIONS")
    assert expectation.status == PASS
    assert EXPECTED_ERROR_CONTEXT_LIMIT in expectation.reason or "超限" in expectation.reason


# ------------------------------------------------------------------- hygiene
def test_runner_never_opens_network_connections(dataset, tmp_path, monkeypatch):
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("评估运行期间不得访问网络")
    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    result = run_dataset(dataset, work_dir=tmp_path / "offline",
                         scenario_ids=("SCENARIO_B_SPARSE",))
    assert _by_id(result)["SCENARIO_B_SPARSE"].outcome == "CONTEXT_BUILT"


def test_evaluation_writes_never_touch_the_production_database(dataset, tmp_path):
    from app.config.settings import AppConfig
    from app.evaluation.fixture import materialize_fixture

    default_dir = Path(AppConfig().data_dir)

    def snapshot():
        if not default_dir.exists():
            return None
        return sorted(str(path.relative_to(default_dir))
                      for path in default_dir.rglob("*"))

    before = snapshot()
    fixture = dataset.fixtures["fixture-b-sparse"]
    materialized = materialize_fixture(fixture, tmp_path / "store")
    try:
        # The evaluation store exists only under the caller-supplied directory.
        assert (tmp_path / "store").exists()
        assert materialized.data_dir == tmp_path / "store"
    finally:
        materialized.close()
    assert snapshot() == before, "评估运行改动了生产数据目录"
