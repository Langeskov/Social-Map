"""Model evaluation and comparison (Phase 13.4 / 10.1-10.6).

Offline mode compares fixed synthetic answers through the same context/prompt; it
must be deterministic and touch no network. Online mode is explicit-only: it
calls exactly one injected provider, records failures instead of dropping them and
never invents a pass when nothing was measured.
"""
import json
import shutil
import socket
from pathlib import Path

import pytest

from app.config.settings import AppConfig
from app.evaluation import load_dataset
from app.evaluation.dataset import DatasetError
from app.evaluation.model_evaluation import (OFFLINE_MODE, ONLINE_MODE,
                                             aggregate_models,
                                             run_model_evaluation,
                                             run_online_model_evaluation)
from app.evaluation.report import write_model_report
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.services.llm.errors import LLMTimeoutError

DATASET_DIR = Path(__file__).parent / "dataset"
CHAT_URL = "http://127.0.0.1:9/v1/chat/completions"


def chat_response(content="hello", *, status=200, model="fake-model",
                  finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 2,
                                               "total_tokens": 7}):
    if status != 200:
        return HttpResponse(status, b'{"error":"unauthorized"}', {}, CHAT_URL)
    payload = {"id": "chat-1", "model": model,
               "choices": [{"index": 0, "finish_reason": finish_reason,
                            "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return HttpResponse(status, json.dumps(payload).encode(), {}, CHAT_URL)


def make_gateway(tmp_path, *, responses=(), error=None, default_response=None,
                 model="fake-model"):
    store = LLMConfigStore.for_data_dir(tmp_path / "llm-data")
    transport = FakeTransport(responses, error=error, default_response=default_response)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake",
                                            base_url="http://127.0.0.1:9/v1",
                                            model=model))
    return gateway, transport


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET_DIR)


@pytest.fixture(scope="module")
def offline_run(dataset, tmp_path_factory):
    return run_model_evaluation(dataset, work_dir=tmp_path_factory.mktemp("model-eval"))


def row(run, scenario_id, label):
    return next(item for item in run.results
                if item.scenario_id == scenario_id and item.model_label == label)


# ----------------------------------------------------------------- dataset rules
def test_dataset_has_fixed_answers_for_two_labels(dataset):
    scenarios = [item for item in dataset.scenarios if item.model_answers]
    assert {item.scenario_id for item in scenarios} == {
        "SCENARIO_A_COMPLETE", "SCENARIO_D_CONFLICT", "SCENARIO_G_LAST_7_DAYS"}
    for scenario in scenarios:
        labels = [answer.model_label for answer in scenario.model_answers]
        assert labels == ["alpha", "beta"]


def test_duplicate_model_labels_are_rejected(tmp_path):
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    path = target / "scenarios" / "scenario-a-complete.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["model_answers"].append(dict(raw["model_answers"][0]))
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("model_label 重复" in issue.message for issue in failure.value.issues)


def test_unknown_model_answer_placeholder_is_rejected(tmp_path):
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    path = target / "scenarios" / "scenario-d-conflict.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["model_answers"][0]["content"] = raw["model_answers"][0]["content"].replace(
        "<<EVIDENCE:chat-d>>", "<<EVIDENCE:ghost-evidence>>")
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("ghost-evidence" in issue.message for issue in failure.value.issues)


# ------------------------------------------------------------------ offline run
def test_offline_run_covers_each_scenario_per_model(offline_run):
    assert offline_run.mode == OFFLINE_MODE
    assert offline_run.scenario_ids == ("SCENARIO_A_COMPLETE", "SCENARIO_D_CONFLICT",
                                        "SCENARIO_G_LAST_7_DAYS")
    assert len(offline_run.results) == 6            # 3 scenarios × 2 labels
    assert set(offline_run.skipped_scenarios) == {item for item in
        {scenario.scenario_id for scenario in
         load_dataset(DATASET_DIR).scenarios} - set(offline_run.scenario_ids)}


def test_offline_metrics_distinguish_compliant_and_noisy_answers(offline_run):
    a_alpha = row(offline_run, "SCENARIO_A_COMPLETE", "alpha")
    for metric_id in ("PARSE_VALIDITY", "REQUIRED_SECTIONS", "CITATION_VALIDITY",
                      "FACT_COVERAGE", "GROUNDING"):
        assert a_alpha.metric(metric_id).status == "PASS", metric_id
    a_beta = row(offline_run, "SCENARIO_A_COMPLETE", "beta")
    assert a_beta.metric("CITATION_VALIDITY").status == "FAIL"   # fabricated uuid
    assert a_beta.metric("GROUNDING").status == "FAIL"
    g_beta = row(offline_run, "SCENARIO_G_LAST_7_DAYS", "beta")
    assert g_beta.metric("PARSE_VALIDITY").status == "FAIL"       # plain prose
    g_alpha = row(offline_run, "SCENARIO_G_LAST_7_DAYS", "alpha")
    # A citation to an out-of-window event is not exposed in this context.
    assert g_alpha.metric("CITATION_VALIDITY").status == "FAIL"
    d_beta = row(offline_run, "SCENARIO_D_CONFLICT", "beta")
    assert d_beta.metric("FACT_COVERAGE").status == "FAIL"        # one side, no citations


def test_offline_fairness_metadata_is_shared_per_scenario(offline_run):
    for scenario_id in offline_run.scenario_ids:
        rows = [item for item in offline_run.results if item.scenario_id == scenario_id]
        assert {item.request["context_snapshot_hash"] for item in rows} == {
            rows[0].request["context_snapshot_hash"]}
        assert {item.request["prompt_hash"] for item in rows} == {rows[0].request["prompt_hash"]}
        assert {item.request["prompt_template_version"] for item in rows} == {
            offline_run.prompt_template_version}
        # The request differs only by the model under comparison.
        assert len({item.request["request_hash"] for item in rows}) == 2


def test_offline_answers_are_resolved_and_preserved(offline_run):
    for item in offline_run.results:
        assert item.outcome.status == "SUCCESS"
        assert item.outcome.answer and "<<" not in item.outcome.answer
        assert item.outcome.attempts is None      # fixed answer, no provider call
        assert item.mode == OFFLINE_MODE


def test_offline_run_is_deterministic_and_never_opens_network(dataset, tmp_path,
                                                              monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("离线模型评估不得访问网络")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    first = run_model_evaluation(dataset, work_dir=tmp_path / "r1")
    second = run_model_evaluation(dataset, work_dir=tmp_path / "r2")
    assert first.run_id != second.run_id
    for left, right in zip(first.results, second.results):
        assert (left.scenario_id, left.model_label) == (right.scenario_id,
                                                        right.model_label)
        assert [(m.metric_id, m.status) for m in left.metrics] == \
               [(m.metric_id, m.status) for m in right.metrics]


def test_human_metrics_are_explicitly_na(offline_run):
    item = row(offline_run, "SCENARIO_A_COMPLETE", "alpha")
    for metric_id in ("UNSUPPORTED_CLAIMS", "UNCERTAINTY_HANDLING", "EPISTEMIC_SEPARATION",
                      "COMPLETENESS", "ROBUSTNESS"):
        metric = item.metric(metric_id)
        assert metric is not None
        assert metric.status == "N/A"
        assert "人工复核" in metric.reason


def test_aggregates_report_counts_and_observations(offline_run):
    aggregates = aggregate_models(offline_run)
    for label in ("alpha", "beta"):
        agg = aggregates[label]
        assert agg["calls"] == 3 and agg["successes"] == 3 and agg["failures"] == 0
        assert agg["failure_rate"] == 0.0
        assert agg["latency_ms"]["available"] is False          # fixed answers
        assert agg["usage"]["available"] is False
        assert agg["metrics"]["PARSE_VALIDITY"]["PASS"] >= 1
    assert aggregates["alpha"]["metrics"]["CITATION_VALIDITY"]["PASS"] == 2


# ------------------------------------------------------------------- online run
def test_online_requires_the_configured_model(dataset, tmp_path):
    gateway, _transport = make_gateway(tmp_path, default_response=chat_response())
    with pytest.raises(ValueError):
        run_online_model_evaluation(dataset, gateway=gateway, provider_name="fake",
                                    model_name="other-model", work_dir=tmp_path / "w",
                                    scenario_ids=("SCENARIO_A_COMPLETE",))


def test_online_success_records_metadata_and_metrics(dataset, tmp_path):
    answer = json.dumps({"schema_version": "1",
                         "facts": [{"content": "深夜通话已发生",
                                    "references": [{"type": "EVENT",
                                                    "id": "00000000-0000-4000-8000-0000000000bb"}]}],
                         "observations": [], "interpretations": [], "uncertainties": []},
                        ensure_ascii=False)
    gateway, transport = make_gateway(
        tmp_path, default_response=chat_response(answer, finish_reason="stop"))
    result = run_online_model_evaluation(
        dataset, gateway=gateway, provider_name="fake", work_dir=tmp_path / "w",
        scenario_ids=("SCENARIO_A_COMPLETE",))
    assert result.mode == ONLINE_MODE
    item = result.results[0]
    assert item.outcome.status == "SUCCESS"
    assert item.outcome.returned_model == "fake-model"
    assert item.outcome.finish["category"] == "STOP"
    assert item.outcome.usage["total_tokens"] == 7
    assert item.outcome.attempts == 1
    assert len(transport.requests) == 1               # exactly one explicit call
    assert item.metric("CITATION_VALIDITY").status == "FAIL"   # fabricated uuid
    assert item.metric("PARSE_VALIDITY").status == "PASS"


def test_online_failure_is_recorded_not_dropped(dataset, tmp_path):
    gateway, _transport = make_gateway(tmp_path,
                                       responses=[chat_response(status=401),
                                                  chat_response(status=401)])
    result = run_online_model_evaluation(
        dataset, gateway=gateway, provider_name="fake", work_dir=tmp_path / "w",
        scenario_ids=("SCENARIO_A_COMPLETE",))
    item = result.results[0]
    assert item.outcome.status == "FAILED"
    assert item.outcome.error_class == "LLMAuthError"
    assert item.outcome.attempts == 1              # non-retryable, one call
    assert len(result.failures) == 1
    assert item.metric("PARSE_VALIDITY").status == "N/A"       # nothing measured
    assert "请求失败" in item.metric("PARSE_VALIDITY").reason
    report_dir = write_model_report(result, tmp_path / "report", dataset=dataset)
    text = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "失败样本" in text and "LLMAuthError" in text


def test_online_timeout_is_marked(dataset, tmp_path):
    gateway, transport = make_gateway(tmp_path,
                                      error=LLMTimeoutError("模型请求超时（>30s）。"))
    result = run_online_model_evaluation(
        dataset, gateway=gateway, provider_name="fake", work_dir=tmp_path / "w",
        scenario_ids=("SCENARIO_A_COMPLETE",))
    item = result.results[0]
    assert item.outcome.status == "FAILED"
    assert item.outcome.timed_out is True
    assert item.outcome.attempts == 2              # exhausted retries, real count
    assert len(transport.requests) == 2             # gateway retried a transient error


def test_online_early_stop_is_recorded(dataset, tmp_path):
    gateway, _transport = make_gateway(
        tmp_path, default_response=chat_response("partial answer", finish_reason="length"))
    result = run_online_model_evaluation(
        dataset, gateway=gateway, provider_name="fake", work_dir=tmp_path / "w",
        scenario_ids=("SCENARIO_A_COMPLETE",))
    item = result.results[0]
    assert item.outcome.finish == {"raw": "length", "category": "LENGTH"}
    aggregates = aggregate_models(result)
    assert aggregates["fake"]["finish_reasons"] == {"LENGTH": 1}


# ------------------------------------------------------------------------ CLI
def test_cli_offline_models_mode_runs_without_network(dataset, tmp_path, monkeypatch):
    from app.evaluation.__main__ import main

    def explode(*args, **kwargs):
        raise AssertionError("CLI 离线模式不得访问网络")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    exit_code = main(["--dataset", str(DATASET_DIR), "--models", "alpha,beta",
                      "--output", str(tmp_path / "runs")])
    assert exit_code == 0
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == OFFLINE_MODE
    assert manifest["dataset_version"] == dataset.dataset_version
    assert (run_dirs[0] / "raw-results.jsonl").exists()
    assert (run_dirs[0] / "report.md").exists()


def test_cli_online_mode_requires_explicit_confirmation(dataset, tmp_path):
    from app.evaluation.__main__ import main
    with pytest.raises(SystemExit) as failure:
        main(["--dataset", str(DATASET_DIR), "--online", "--provider", "fake",
              "--output", str(tmp_path / "runs")])
    assert failure.value.code == 2
    with pytest.raises(SystemExit) as failure:
        main(["--dataset", str(DATASET_DIR), "--online", "--confirm-online",
              "--output", str(tmp_path / "runs")])
    assert failure.value.code == 2


def test_cli_online_mode_runs_with_injected_gateway(dataset, tmp_path):
    from app.evaluation.__main__ import main
    gateway, _transport = make_gateway(tmp_path, default_response=chat_response())
    exit_code = main(["--dataset", str(DATASET_DIR), "--online", "--provider", "fake",
                      "--confirm-online", "--scenario", "SCENARIO_A_COMPLETE",
                      "--output", str(tmp_path / "runs")],
                     gateway_factory=lambda: gateway)
    assert exit_code == 0
    run_dir = next((tmp_path / "runs").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == ONLINE_MODE
    assert manifest["models"][0]["provider_name"] == "fake"
    assert manifest["counts"]["fake"]["calls"] == 1


# ---------------------------------------------------------------------- report
def test_report_files_are_machine_and_human_readable(offline_run, tmp_path, dataset):
    target = write_model_report(offline_run, tmp_path / "report", dataset=dataset)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == OFFLINE_MODE
    assert manifest["dataset_version"] == "1.2"
    assert manifest["prompt_template_version"] == "2"
    assert "limitations" in manifest and manifest["limitations"]
    lines = (target / "raw-results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(offline_run.results)
    metrics = json.loads((target / "metrics.json").read_text(encoding="utf-8"))
    assert set(metrics) == {"aggregates", "by_scenario_model"}
    assert "alpha" in metrics["aggregates"] and "beta" in metrics["aggregates"]
    text = (target / "report.md").read_text(encoding="utf-8")
    for phrase in ("失败样本", "需要人工复核", "局限", "不构成排名", "Prompt template",
                   "不是模型排行榜"):
        assert phrase in text
