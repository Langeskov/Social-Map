"""Offline analysis-evaluation framework (Phase 13.0).

Public API:

- `load_dataset` — load + validate a versioned synthetic dataset (no DB, no net);
- `run_dataset` / `run_scenario` — offline evaluation through the real
  ContextBuilder / PromptBuilder (no provider, no network);
- `write_report` / `render_markdown` — machine- and human-readable results.

Evaluation never writes facts, never saves Memories and never calls a model.
"""
from app.evaluation.dataset import load_dataset, scenario_digest
from app.evaluation.model_evaluation import (ModelEvaluationRunResult,
                                             evaluate_answer,
                                             run_model_evaluation,
                                             run_online_model_evaluation)
from app.evaluation.models import (DATASET_VERSION, FAIL, METRIC_DEFINITIONS,
                                   NOT_APPLICABLE, PASS, SCHEMA_VERSION,
                                   DatasetError, DatasetIssue, EvaluationDataset,
                                   EvaluationRunResult, EvaluationScenario,
                                   ExpectedReferenceOutcome, MetricResult,
                                   ModelAnswerSpec, SampleOutput,
                                   ScenarioEvaluationResult)
from app.evaluation.report import (render_markdown, render_model_markdown,
                                   write_model_report, write_report)
from app.evaluation.runner import mask_volatile_text, run_dataset, run_scenario

__all__ = [
    "DATASET_VERSION", "SCHEMA_VERSION", "METRIC_DEFINITIONS",
    "PASS", "FAIL", "NOT_APPLICABLE",
    "DatasetError", "DatasetIssue", "EvaluationDataset", "EvaluationRunResult",
    "EvaluationScenario", "ExpectedReferenceOutcome", "MetricResult",
    "ModelAnswerSpec", "SampleOutput", "ScenarioEvaluationResult",
    "ModelEvaluationRunResult", "evaluate_answer",
    "load_dataset", "scenario_digest", "run_dataset", "run_scenario",
    "run_model_evaluation", "run_online_model_evaluation",
    "render_markdown", "render_model_markdown",
    "write_report", "write_model_report", "mask_volatile_text",
]
