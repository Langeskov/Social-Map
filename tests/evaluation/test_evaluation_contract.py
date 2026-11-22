"""Evaluation contract structure (Phase 13.0 / 6.3, 6.4).

Covers the versioned input contract fields, the metric registry (including the
N/A policy that keeps human/model-output metrics honest) and the data-leakage
principle: reference answers describe expectations and must never be part of the
synthetic input a model would see.
"""
from pathlib import Path

import pytest

from app.evaluation import DATASET_VERSION, load_dataset
from app.evaluation.models import METRIC_DEFINITIONS

DATASET_DIR = Path(__file__).parent / "dataset"

REQUIRED_TOP_LEVEL_FIELDS = (
    "scenario_id", "scenario_version", "dataset_version", "fixture", "request",
    "expected_facts", "required_sources", "forbidden_claims",
    "expected_uncertainties", "evaluation_dimensions",
)

#: The ten metrics required by the Phase 13.0 metric table plus the four
#: model-evaluation dimensions added in Phase 13.4.
TABLE_METRICS = (
    "PARSE_VALIDITY", "REQUIRED_SECTIONS", "CITATION_VALIDITY",
    "CROSS_WORKSPACE_LEAKAGE", "FACT_COVERAGE", "UNSUPPORTED_CLAIMS",
    "UNCERTAINTY_HANDLING", "EPISTEMIC_SEPARATION", "COMPLETENESS", "ROBUSTNESS",
)
MODEL_METRICS = ("GROUNDING", "LATENCY", "TOKEN_USAGE", "FAILURE_RATE")


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET_DIR)


def test_every_scenario_declares_the_versioned_contract(dataset):
    import json
    for scenario in dataset.scenarios:
        raw = json.loads((Path(DATASET_DIR) / scenario.source_path).read_text(encoding="utf-8"))
        for field in REQUIRED_TOP_LEVEL_FIELDS:
            assert field in raw, f"{scenario.scenario_id} 缺少契约字段 {field}"
        assert raw["dataset_version"] == DATASET_VERSION
        request = raw["request"]
        for field in ("target_type", "target_key", "analysis_type", "as_of",
                      "time_scope", "include"):
            assert field in request, f"{scenario.scenario_id}.request 缺少 {field}"
        # 6.2 per-scenario requirements: facts, forbidden inferences, expected
        # uncertainties, source ids+types, expected verification, human review.
        assert isinstance(raw["expected_facts"], list)
        assert isinstance(raw["forbidden_claims"], list) and raw["forbidden_claims"]
        assert isinstance(raw["expected_uncertainties"], list) and raw["expected_uncertainties"]
        assert isinstance(raw["human_review"], list) and raw["human_review"]
        assert raw["expected_context"]["expected_error"] in (None, "CONTEXT_LIMIT")


def test_expected_fact_ids_are_unique_within_each_scenario(dataset):
    for scenario in dataset.scenarios:
        ids = [fact.fact_id for fact in scenario.expected_facts]
        assert len(ids) == len(set(ids)), scenario.scenario_id


def test_metric_registry_contains_the_required_table():
    for metric_id in TABLE_METRICS + MODEL_METRICS:
        definition = METRIC_DEFINITIONS.get(metric_id)
        assert definition is not None, metric_id
        assert definition.description and definition.method and definition.scope
    assert len(METRIC_DEFINITIONS) == 22


def test_metric_registry_keeps_modes_explicit():
    for definition in METRIC_DEFINITIONS.values():
        if definition.available_in_13_0:
            # Computable today: no N/A excuse needed.
            assert definition.na_reason == "", definition.metric_id
            assert not definition.needs_human
        else:
            # Not computable in 13.0: must say why a run reports N/A.
            assert definition.na_reason, definition.metric_id
    human_only = [item.metric_id for item in METRIC_DEFINITIONS.values() if item.needs_human]
    assert "COMPLETENESS" in human_only and "EPISTEMIC_SEPARATION" in human_only


def test_evaluation_dimensions_only_reference_known_metrics(dataset):
    for scenario in dataset.scenarios:
        for dimension in scenario.evaluation_dimensions:
            assert dimension in METRIC_DEFINITIONS, f"{scenario.scenario_id}: {dimension}"


def test_reference_answers_are_not_part_of_the_synthetic_input(dataset):
    """Leakage principle at dataset level: expectation text must not occur in the
    fixture data a model could see. (The runner additionally checks the real
    rendered prompt in PROMPT_LEAKAGE.)"""
    for scenario in dataset.scenarios:
        fixture_text = (Path(DATASET_DIR) / dataset.fixtures[scenario.fixture_id].source_path
                        ).read_text(encoding="utf-8")
        for token in scenario.reference_answer_tokens:
            assert token not in fixture_text, \
                f"{scenario.scenario_id} 的参考答案泄漏进夹具数据：{token!r}"


def test_no_reference_token_collides_with_required_context_text(dataset):
    """required_text is *expected* in the context; it must not double as an
    expectation statement, or the leak check would be meaningless."""
    for scenario in dataset.scenarios:
        for token in scenario.reference_answer_tokens:
            for marker in scenario.expected_context.required_text:
                assert token != marker, scenario.scenario_id
