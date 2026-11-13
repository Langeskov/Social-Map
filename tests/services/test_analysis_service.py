"""AnalysisService + gateway: recorded runs, retries, cancellation and privacy."""
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError, NotFoundError
from app.services.analysis import AnalysisService
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import (CancelToken, FakeTransport, HttpResponse, LLMConfigError,
                              LLMConfigStore, LLMGateway, LLMProviderConfig)
from app.services.unit_of_work import transaction
from app.storage.models import (AnalysisEventSourceModel, AnalysisEvidenceSourceModel,
                                AnalysisPersonSourceModel, AnalysisRelationshipSourceModel,
                                AnalysisRunModel, EventModel, EvidenceModel, PersonModel,
                                RelationshipHistoryModel, RelationshipModel)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ANSWER = """## Facts（事实）
根据记录，2026-04-01 有一场摄影活动。

## Observations（观察）
用户记录：她当时更主动。

## Interpretations（推断）
根据记录，两人可能保持联系。

## Uncertainties（不确定与缺失）
证据不足以确认关系强度。"""


def chat_response(content=ANSWER, status=200):
    if status != 200:
        return HttpResponse(status, b'{"error":"private server detail"}', {}, "http://x/v1/chat/completions")
    payload = {"id": "chatcmpl-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content}}],
               "usage": {"total_tokens": 42}}
    return HttpResponse(status, json.dumps(payload).encode(), {}, "http://x/v1/chat/completions")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_TEST_KEY", "sk-test-secret-value")
    config = AppConfig(data_dir=tmp_path / "analysis-data")
    runtime = initialize_database(config)
    transport = FakeTransport(default_response=chat_response())
    store = LLMConfigStore.for_data_dir(config.storage_paths.data_dir)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake", base_url="http://127.0.0.1:9/v1",
                                            model="fake-model", credential_ref="env:RIS_TEST_KEY",
                                            temperature=0.1, timeout=5))
    try:
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        workspace = services.initial_workspace
        person = services.people.create(workspace.id, "分析对象")
        other = services.people.create(workspace.id, "另一个人")
        relationship = services.relationships.create(workspace.id, person.id, other.id, "Friend")
        event = services.events.create_event(workspace.id, "摄影活动", "Meeting", "FACT",
                                             occurred_on=datetime.now(timezone.utc).date(),
                                             participants=(person.id,))
        evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "聊天记录",
                                                     content_text="私人聊天原文", source_type="CHAT")
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              person=person, other=other, relationship=relationship, event=event,
                              evidence=evidence, transport=transport, gateway=gateway, store=store,
                              tmp_path=tmp_path)
    finally:
        runtime.close()


def request_for(env, target_type="PERSON", **overrides):
    targets = {"PERSON": env.person.id, "RELATIONSHIP": env.relationship.id, "EVENT": env.event.id}
    types = {"PERSON": "PERSON_PROFILE", "RELATIONSHIP": "RELATIONSHIP_ANALYSIS",
             "EVENT": "EVENT_ANALYSIS"}
    values = dict(workspace_id=env.ws, target_type=target_type,
                  target_id=targets.get(target_type, uuid4()),
                  analysis_type=types.get(target_type, "PERSON_PROFILE"),
                  as_of=NOW, time_scope=ALL_TIME)
    values.update(overrides)
    return AnalysisRequest(**values)


def fact_counts(env):
    with transaction(env.runtime.sessions) as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in
                     (PersonModel, RelationshipModel, RelationshipHistoryModel, EventModel,
                      EvidenceModel, AnalysisRunModel, AnalysisPersonSourceModel,
                      AnalysisRelationshipSourceModel, AnalysisEventSourceModel,
                      AnalysisEvidenceSourceModel))


def test_run_creation_success_and_result(env):
    before = fact_counts(env)
    run = env.services.analysis.create_run(request_for(env))
    assert run.status == "SUCCEEDED"
    assert run.result == ANSWER and run.error_message is None
    assert run.provider_name == "fake" and run.model_name == "fake-model"
    assert run.completed_at is not None and run.duration_ms is not None
    assert run.analysis_type == "PERSON_PROFILE"
    after = fact_counts(env)
    # One run row was added; every fact table stayed identical.
    assert after[:5] == before[:5] and after[5] == before[5] + 1
    assert env.services.people.get(env.ws, env.person.id).display_name == "分析对象"


def test_snapshot_is_persisted_and_replayable(env):
    run = env.services.analysis.create_run(request_for(env))
    view = env.services.analysis.get(env.ws, run.id)
    snapshot = view.snapshot
    assert snapshot is not None
    assert "分析对象" in snapshot.rendered_context
    assert snapshot.analysis_run_id == run.id
    assert snapshot.estimated_tokens > 0
    raw = json.loads(run.context_snapshot)
    assert raw["scope"]["workspace_id"] == str(env.ws)
    assert raw["rendered_context"] == snapshot.rendered_context
    # The snapshot is what the transport actually received (JSON escapes CJK, so
    # compare the decoded message payload).
    sent = json.loads(env.transport.requests[0]["body"].decode("utf-8"))
    assert snapshot.rendered_context in sent["messages"][1]["content"]


def test_sources_are_persisted_per_fact_table(env):
    run = env.services.analysis.create_run(request_for(env))
    view = env.services.analysis.get(env.ws, run.id)
    assert {source.target_id for source in view.sources.people} == {env.person.id}
    assert {source.target_id for source in view.sources.events} == {env.event.id}
    assert {source.target_id for source in view.sources.evidence} == {env.evidence.id}
    assert view.sources.total() >= 3
    with transaction(env.runtime.sessions) as session:
        rows = session.scalars(select(AnalysisPersonSourceModel)).all()
        assert [row.workspace_id for row in rows] == [env.ws]
        assert session.scalar(select(func.count()).select_from(AnalysisRunModel)) == 1


def test_relationship_and_event_targets_work(env):
    relationship_run = env.services.analysis.create_run(request_for(env, "RELATIONSHIP"))
    assert relationship_run.status == "SUCCEEDED"
    view = env.services.analysis.get(env.ws, relationship_run.id)
    assert {source.target_id for source in view.sources.relationships} == {env.relationship.id}
    event_run = env.services.analysis.create_run(request_for(env, "EVENT"))
    assert event_run.status == "SUCCEEDED"
    assert "摄影活动" in env.services.analysis.get(env.ws, event_run.id).snapshot.rendered_context


@pytest.mark.parametrize("status,expected", [(401, "凭据"), (403, "凭据"), (404, "不存在"),
                                             (429, "限流"), (500, "服务器错误")])
def test_provider_failures_are_recorded_as_failed_runs(env, status, expected):
    env.transport.push(chat_response(status=status))
    env.transport.push(chat_response(status=status))          # retry attempt
    run = env.services.analysis.create_run(request_for(env))
    assert run.status == "FAILED"
    assert run.result is None
    assert expected in run.error_message
    assert "private server detail" not in run.error_message
    assert "sk-test-secret-value" not in run.error_message
    assert fact_counts(env)[:5] == fact_counts(env)[:5]


def test_retry_policy_retries_transient_failures_only(env):
    env.transport.push(chat_response(status=500))
    env.transport.push(chat_response())                       # second attempt succeeds
    run = env.services.analysis.create_run(request_for(env))
    assert run.status == "SUCCEEDED"
    assert len(env.transport.requests) == 2

    env.transport.push(chat_response(status=401))
    run = env.services.analysis.create_run(request_for(env))
    assert run.status == "FAILED"
    # 401 is not retried: exactly one request per run.
    assert len(env.transport.requests) == 3


def test_context_guard_failure_is_recorded_safely(env):
    run = env.services.analysis.create_run(request_for(env, max_context_tokens=1))
    assert run.status == "FAILED"
    assert "Selected context exceeds model context limit." in run.error_message
    assert env.transport.requests == []                      # nothing was sent


def test_cancellation_before_send_marks_cancelled(env):
    token = CancelToken()
    token.cancel()
    run = env.services.analysis.create_run(request_for(env), cancel=token)
    assert run.status == "CANCELLED"
    assert run.result is None
    assert env.transport.requests == []
    assert "取消" in run.error_message


def test_cancel_run_marks_a_pending_run(env):
    with transaction(env.runtime.sessions) as session:
        repositories = sql_analysis_repositories(session)
        from app.domain.entities import AnalysisRun
        pending = repositories.runs.save(AnalysisRun(
            workspace_id=env.ws, target_type="PERSON", target_id=env.person.id,
            analysis_type="PERSON_PROFILE", provider_name="fake", model_name="fake-model",
            scope="{}", status="PENDING", created_at=NOW, started_at=NOW))
    cancelled = env.services.analysis.cancel_run(env.ws, pending.id)
    assert cancelled.status == "CANCELLED" and cancelled.completed_at is not None
    with pytest.raises(ConflictError):
        env.services.analysis.cancel_run(env.ws, cancelled.id)


def test_history_lists_runs_newest_first_and_never_overwrites(env):
    first = env.services.analysis.create_run(request_for(env))
    env.transport.push(chat_response("第二次结果"))
    second = env.services.analysis.create_run(request_for(env))
    history = env.services.analysis.history(env.ws)
    assert [view.run.id for view in history] == [second.id, first.id]
    assert history[1].run.result == ANSWER
    assert history[0].run.result == "第二次结果"
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRunModel)) == 2
    filtered = env.services.analysis.history(env.ws, target_type="EVENT")
    assert filtered == []


def test_workspace_isolation_and_target_validation(env):
    other = env.services.workspaces.create("另一个 Workspace")
    run = env.services.analysis.create_run(request_for(env))
    with pytest.raises(NotFoundError):
        env.services.analysis.get(other.id, run.id)
    with pytest.raises(NotFoundError):
        env.services.analysis.create_run(request_for(env, target_id=uuid4()))
    with pytest.raises(NotFoundError):
        env.services.analysis.create_run(request_for(env, workspace_id=uuid4()))
    with pytest.raises(DomainError):
        env.services.analysis.create_run(request_for(env, target_type="MEDIA", target_id=uuid4(),
                                                     analysis_type="PERSON_PROFILE"))
    assert env.services.analysis.history(other.id) == []


def test_delete_run_removes_only_analysis_rows(env):
    run = env.services.analysis.create_run(request_for(env))
    before = fact_counts(env)
    env.services.analysis.delete_run(env.ws, run.id)
    after = fact_counts(env)
    assert after[:5] == before[:5]
    assert after[5] == 0 and after[6:] == (0, 0, 0, 0)
    with pytest.raises(NotFoundError):
        env.services.analysis.get(env.ws, run.id)


def test_facts_are_never_mutated_by_an_analysis(env):
    def fact_rows():
        with transaction(env.runtime.sessions) as session:
            # Core select on the Table yields raw column values, not ORM instances.
            return {model.__tablename__: [tuple(row) for row in
                                          session.execute(select(model.__table__)).all()]
                    for model in (PersonModel, RelationshipModel, RelationshipHistoryModel, EventModel,
                                  EvidenceModel)}
    before = fact_rows()
    env.services.analysis.create_run(request_for(env))
    env.services.analysis.create_run(request_for(env, "RELATIONSHIP"))
    env.services.analysis.create_run(request_for(env, "EVENT"))
    assert fact_rows() == before


def test_prompt_separates_facts_observations_interpretations_and_uncertainties(env):
    env.services.analysis.create_run(request_for(env))
    body = json.loads(env.transport.requests[0]["body"].decode("utf-8"))
    system = body["messages"][0]["content"]
    user = body["messages"][-1]["content"]
    context = body["messages"][1]["content"]
    assert body["messages"][0]["role"] == "system"
    for phrase in ("不能修改", "Observation", "Inference", "不得编造", "不确定性", "好感度"):
        assert phrase in system
    for heading in ("Facts", "Observations", "Interpretations", "Uncertainties"):
        assert heading in user
    assert "事实上下文" in context and "OBSERVATION" in system


def test_api_key_is_never_logged_or_stored(env, caplog):
    with caplog.at_level(logging.DEBUG):
        run = env.services.analysis.create_run(request_for(env))
    assert run.status == "SUCCEEDED"
    assert "sk-test-secret-value" not in caplog.text
    assert env.transport.requests[0]["headers"]["Authorization"] == "Bearer sk-test-secret-value"
    # The database stores a reference, never the key.
    with transaction(env.runtime.sessions) as session:
        for row in session.scalars(select(AnalysisRunModel)).all():
            assert "sk-test-secret-value" not in (row.scope or "")
            assert "sk-test-secret-value" not in (row.context_snapshot or "")
            assert "sk-test-secret-value" not in (row.result or "")
            assert "env:RIS_TEST_KEY" in (row.scope or "")
    assert env.gateway.credential_reference("fake") == "env:RIS_TEST_KEY"


def test_private_context_and_result_are_not_logged(env, caplog):
    env.services.evidence.update_metadata(env.ws, env.evidence.id, annotation="极其私密的注释")
    with caplog.at_level(logging.DEBUG):
        env.services.analysis.create_run(request_for(env, include_evidence=True))
        env.transport.push(chat_response(status=500))
        env.transport.push(chat_response(status=500))
        env.services.analysis.create_run(request_for(env, include_evidence=True))
    assert "极其私密的注释" not in caplog.text
    assert "私人聊天原文" not in caplog.text
    assert ANSWER.splitlines()[1] not in caplog.text
    assert "Analysis run" in caplog.text                      # the safe, class-only line


def test_config_store_round_trip_keeps_secrets_out_of_the_file(env):
    store = env.store
    config = store.load()
    assert config.names() == ("fake",)
    assert config.default_provider == "fake"
    text = store.path.read_text(encoding="utf-8")
    assert "env:RIS_TEST_KEY" in text
    assert "sk-test-secret-value" not in text
    store.save(config.with_provider(LLMProviderConfig(provider_name="second",
                                                      base_url="https://api.example.invalid/v1",
                                                      model="m2")))
    assert store.load().names() == ("fake", "second")
    assert store.load().default_provider == "fake"
    store.save(store.load().with_default("second"))
    assert store.load().default_provider == "second"
    store.save(store.load().without_provider("second"))
    assert store.load().names() == ("fake",)
    assert store.load().default_provider == "fake"
    with pytest.raises(LLMConfigError):
        LLMProviderConfig(provider_name="bad", base_url="ftp://x", model="m")
    with pytest.raises(LLMConfigError):
        LLMProviderConfig(provider_name="bad", base_url="http://x", model="m",
                          extra_headers={"Authorization": "Bearer secret"})


def test_missing_credential_fails_the_run_without_sending(env):
    env.gateway.save_provider(LLMProviderConfig(provider_name="nokey", base_url="http://127.0.0.1:9/v1",
                                                model="m", credential_ref="env:RIS_ABSENT_KEY"))
    env.gateway.set_default("nokey")
    run = env.services.analysis.create_run(request_for(env))
    assert run.status == "FAILED"
    assert "RIS_ABSENT_KEY" in run.error_message
    assert env.transport.requests == []


def test_unconfigured_provider_is_reported_clearly(env):
    empty = LLMGateway(LLMConfigStore(env.tmp_path / "missing.toml"), transport=FakeTransport([]))
    service = AnalysisService(env.runtime.sessions, empty)
    with pytest.raises(LLMConfigError):
        service.create_run(request_for(env))
