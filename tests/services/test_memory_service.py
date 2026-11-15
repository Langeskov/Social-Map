"""MemoryService + Analysis Review traceability (Phase 9)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.domain.entities import Memory
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import FakeTransport, HttpResponse, LLMConfigStore, LLMGateway, LLMProviderConfig
from app.services.unit_of_work import transaction
from app.storage.models import (AnalysisRunModel, EventModel, EvidenceModel, MemoryModel, PersonModel,
                                RelationshipHistoryModel, RelationshipModel)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ANSWER = """## Facts（事实）
根据记录，2026-04-01 有一场摄影活动。

## Interpretations（推断）
根据记录，两人可能保持联系。
"""


def chat_response(content=ANSWER):
    payload = {"id": "chatcmpl-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content}}],
               "usage": {"total_tokens": 12}}
    return HttpResponse(200, json.dumps(payload).encode(), {}, "http://x/v1/chat/completions")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_TEST_KEY", "sk-test-secret-value")
    config = AppConfig(data_dir=tmp_path / "memory-data")
    runtime = initialize_database(config)
    transport = FakeTransport(default_response=chat_response())
    store = LLMConfigStore.for_data_dir(config.storage_paths.data_dir)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake", base_url="http://127.0.0.1:9/v1",
                                            model="fake-model", credential_ref="env:RIS_TEST_KEY"))
    try:
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        workspace = services.initial_workspace
        person = services.people.create(workspace.id, "记忆对象")
        other = services.people.create(workspace.id, "另一个人")
        relationship = services.relationships.create(workspace.id, person.id, other.id, "Friend")
        event = services.events.create_event(workspace.id, "摄影活动", "Meeting", "FACT",
                                             occurred_on=datetime.now(timezone.utc).date(),
                                             participants=(person.id,))
        evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "聊天记录",
                                                     content_text="私人聊天原文", source_type="CHAT")
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              person=person, other=other, relationship=relationship, event=event,
                              evidence=evidence, transport=transport, gateway=gateway,
                              tmp_path=tmp_path)
    finally:
        runtime.close()


def analyze(env, target_type="PERSON"):
    target = {"PERSON": env.person.id, "RELATIONSHIP": env.relationship.id, "EVENT": env.event.id}
    analysis_type = {"PERSON": "PERSON_PROFILE", "RELATIONSHIP": "RELATIONSHIP_ANALYSIS",
                     "EVENT": "EVENT_ANALYSIS"}
    request = AnalysisRequest(workspace_id=env.ws, target_type=target_type,
                              target_id=target[target_type], analysis_type=analysis_type[target_type],
                              as_of=NOW, time_scope=ALL_TIME)
    return env.services.analysis.create_run(request)


def fact_rows(env):
    with transaction(env.runtime.sessions) as session:
        return {model.__tablename__: [tuple(row) for row in session.execute(select(model.__table__)).all()]
                for model in (PersonModel, RelationshipModel, RelationshipHistoryModel, EventModel,
                              EvidenceModel, AnalysisRunModel)}


# --------------------------------------------------------------------- review

def test_analysis_sources_can_locate_real_facts(env):
    run = analyze(env)
    view = env.services.analysis.get(env.ws, run.id)
    assert {source.target_id for source in view.sources.people} == {env.person.id}
    assert {source.target_id for source in view.sources.events} == {env.event.id}
    assert {source.target_id for source in view.sources.evidence} == {env.evidence.id}
    # A source is a reference: the fact is still reachable through its own service.
    assert env.services.people.get(env.ws, env.person.id).display_name == "记忆对象"
    assert env.services.events.get(env.ws, env.event.id).title == "摄影活动"


def test_sources_stay_workspace_isolated_without_duplicating_facts(env):
    run = analyze(env)
    other = env.services.workspaces.create("另一个 Workspace")
    with pytest.raises(NotFoundError):
        env.services.analysis.get(other.id, run.id)
    assert env.services.memories.list_all(other.id) == []
    # Source rows only reference fact ids; the snapshot is the only text stored, and
    # it never duplicates a fact's private payload beyond what was rendered.
    with transaction(env.runtime.sessions) as session:
        snapshot_text = session.scalar(select(AnalysisRunModel.context_snapshot).where(
            AnalysisRunModel.id == run.id))
        assert "私人聊天原文" not in (snapshot_text or "")       # evidence content stayed out
        assert session.scalar(select(func.count()).select_from(AnalysisRunModel)) == 1


def test_candidate_references_are_validated_before_being_trusted(env):
    run = analyze(env)
    forged = f"[EVIDENCE:{uuid4()}]"
    outside = f"[EVENT:{uuid4()}]"
    text = ANSWER + f"\n引用： [EVENT:{env.event.id}] [PERSON:{env.person.id}] {forged} {outside}"
    env.transport.set_default(chat_response(text))
    second = analyze(env)
    candidates = env.services.memories.candidate_references(env.ws, second.id)
    accepted = {item.raw for item in candidates if item.accepted}
    rejected = {item.raw: item.reason for item in candidates if not item.accepted}
    assert f"[EVENT:{env.event.id}]" in accepted
    assert f"[PERSON:{env.person.id}]" in accepted
    assert forged in rejected and outside in rejected
    assert all("来源" in reason for reason in rejected.values())
    # Parsing never writes anything and never becomes a source.
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(MemoryModel)) == 0
    view = env.services.analysis.get(env.ws, second.id)
    assert view.sources.total() >= 1
    # The forged/outside markers did not become sources of the run.
    recorded = {str(source.target_id) for source in view.sources.all()}
    assert str(uuid4()) not in recorded
    assert run.id != second.id


def test_candidate_references_ignore_unparsable_markers(env):
    env.transport.set_default(chat_response("引用 [EVENT:not-a-uuid] 与 [UNKNOWN:abc] 结束"))
    run = analyze(env)
    candidates = env.services.memories.candidate_references(env.ws, run.id)
    assert [item.accepted for item in candidates] == [False]
    assert candidates[0].entity_type == "EVENT"
    assert candidates[0].entity_id is None


def test_candidate_references_require_a_known_run(env):
    with pytest.raises(NotFoundError):
        env.services.memories.candidate_references(env.ws, uuid4())
    with pytest.raises(NotFoundError):
        env.services.memories.candidate_references(uuid4(), analyze(env).id)


# --------------------------------------------------------------------- memory

def test_manual_memory_creation(env):
    memory = env.services.memories.create(env.ws, "  张三更喜欢小型活动  ", kind="PREFERENCE",
                                          person_id=env.person.id)
    assert memory.content == "张三更喜欢小型活动"          # trimmed
    assert memory.source_type == "MANUAL" and memory.source_analysis_id is None
    assert memory.confirmed_at is not None and memory.deleted_at is None
    assert memory.workspace_id == env.ws and memory.person_id == env.person.id
    assert env.services.memories.get(env.ws, memory.id).content == memory.content


def test_workspace_level_memory_has_no_person(env):
    memory = env.services.memories.create(env.ws, "整体上我更看重面对面交流", kind="CONTEXT")
    assert memory.person_id is None and memory.workspace_scoped
    assert [item.id for item in env.services.memories.list(env.ws)] == [memory.id]
    assert env.services.memories.list(env.ws, person_id=env.person.id) == []
    assert [item.id for item in env.services.memories.list_all(env.ws)] == [memory.id]


def test_memory_from_analysis_requires_explicit_confirmation(env):
    run = analyze(env)
    with pytest.raises(DomainError):
        env.services.memories.create_from_analysis(env.ws, run.id, "内容", kind="FACT",
                                                   confirmed=False)
    with pytest.raises(DomainError):
        env.services.memories.create_from_analysis(env.ws, run.id, "内容", kind="FACT",
                                                   confirmed=None)
    assert env.services.memories.list_all(env.ws, include_deleted=True) == []
    memory = env.services.memories.create_from_analysis(
        env.ws, run.id, "用户可以编辑后的文本", kind="FACT", person_id=env.person.id, confirmed=True)
    assert memory.source_type == "ANALYSIS"
    assert memory.source_analysis_id == run.id
    assert memory.confirmed_at is not None
    assert memory.content == "用户可以编辑后的文本"      # not the raw result
    assert memory.content != run.result


def test_memory_from_analysis_validates_run_and_person(env):
    other = env.services.workspaces.create("另一个 Workspace")
    run = analyze(env)
    with pytest.raises(NotFoundError):
        env.services.memories.create_from_analysis(other.id, run.id, "内容", kind="FACT", confirmed=True)
    with pytest.raises(NotFoundError):
        env.services.memories.create_from_analysis(env.ws, uuid4(), "内容", kind="FACT", confirmed=True)
    with pytest.raises(NotFoundError):
        env.services.memories.create_from_analysis(env.ws, run.id, "内容", kind="FACT",
                                                   person_id=uuid4(), confirmed=True)
    assert env.services.memories.list_all(env.ws, include_deleted=True) == []


def test_content_is_editable_and_provenance_is_not(env):
    run = analyze(env)
    memory = env.services.memories.create_from_analysis(env.ws, run.id, "初始内容", kind="FACT",
                                                        confirmed=True)
    updated = env.services.memories.update(env.ws, memory.id, content="修改后的内容", kind="CONTEXT")
    assert updated.content == "修改后的内容" and updated.kind == "CONTEXT"
    assert updated.updated_at >= memory.updated_at
    assert updated.source_type == "ANALYSIS" and updated.source_analysis_id == run.id
    assert updated.confirmed_at == memory.confirmed_at
    with pytest.raises(DomainError):
        env.services.memories.update(env.ws, memory.id, content="   ")
    with pytest.raises(DomainError):
        env.services.memories.update(env.ws, memory.id, kind="PERSONALITY")


def test_source_analysis_is_not_modified_by_memory(env):
    run = analyze(env)
    before = env.services.analysis.get(env.ws, run.id)
    env.services.memories.create_from_analysis(env.ws, run.id, "长期记住这一点", kind="FACT",
                                               confirmed=True)
    after = env.services.analysis.get(env.ws, run.id)
    assert after.run == before.run
    assert after.run.result == ANSWER
    assert after.sources.total() == before.sources.total()


def test_memory_never_modifies_any_fact(env):
    run = analyze(env)
    before = fact_rows(env)
    manual = env.services.memories.create(env.ws, "事实不应改变", kind="FACT", person_id=env.person.id)
    from_analysis = env.services.memories.create_from_analysis(env.ws, run.id, "也不应改变", kind="PREFERENCE",
                                                               confirmed=True)
    env.services.memories.update(env.ws, manual.id, content="编辑后也不应改变")
    env.services.memories.soft_delete(env.ws, from_analysis.id)
    env.services.memories.restore(env.ws, from_analysis.id)
    assert fact_rows(env) == before


def test_soft_delete_and_restore(env):
    memory = env.services.memories.create(env.ws, "可以删除的记忆", kind="FACT")
    deleted = env.services.memories.soft_delete(env.ws, memory.id)
    assert deleted.deleted_at is not None
    assert env.services.memories.list_all(env.ws) == []
    assert [item.id for item in env.services.memories.list_all(env.ws, include_deleted=True)] == [memory.id]
    with pytest.raises(DomainError):
        env.services.memories.soft_delete(env.ws, memory.id)
    restored = env.services.memories.restore(env.ws, memory.id)
    assert restored.deleted_at is None
    assert restored.source_type == memory.source_type
    with pytest.raises(DomainError):
        env.services.memories.restore(env.ws, memory.id)
    assert [item.id for item in env.services.memories.list_all(env.ws)] == [memory.id]


def test_validity_window_filters_expired_and_future_memories(env):
    current = env.services.memories.create(env.ws, "当前有效", kind="FACT",
                                           valid_from=NOW - timedelta(days=1))
    future = env.services.memories.create(env.ws, "尚未生效", kind="FACT",
                                          valid_from=NOW + timedelta(days=1))
    expired = env.services.memories.create(env.ws, "已经过期", kind="FACT",
                                           valid_until=NOW - timedelta(days=1))
    exact = env.services.memories.create(env.ws, "边界：valid_until 等于 now", kind="FACT",
                                         valid_from=NOW - timedelta(days=2), valid_until=NOW)
    now_listed = {item.id for item in env.services.memories.list_all(env.ws, now=NOW)}
    assert current.id in now_listed
    assert future.id not in now_listed and expired.id not in now_listed
    assert exact.id not in now_listed          # valid_until is exclusive
    everything = {item.id for item in env.services.memories.list_all(env.ws, include_expired=True,
                                                                    now=NOW)}
    assert {current.id, future.id, expired.id, exact.id} <= everything
    assert env.services.memories.is_current(current, now=NOW) is True
    assert env.services.memories.is_current(future, now=NOW) is False
    assert env.services.memories.is_current(expired, now=NOW) is False


def test_validity_rules_are_enforced(env):
    with pytest.raises(DomainError):
        env.services.memories.create(env.ws, "顺序错误", kind="FACT",
                                     valid_from=NOW, valid_until=NOW - timedelta(days=1))
    with pytest.raises(DomainError):
        env.services.memories.create(env.ws, "naive 时间", kind="FACT",
                                     valid_from=datetime(2026, 1, 1))
    memory = env.services.memories.create(env.ws, "先有有效期", kind="FACT", valid_until=NOW)
    assert env.services.memories.is_current(memory, now=NOW) is False
    cleared = env.services.memories.update(env.ws, memory.id, clear_valid_until=True)
    assert cleared.valid_until is None and env.services.memories.is_current(cleared, now=NOW) is True


def test_kind_and_content_validation(env):
    for kind in ("FACT", "PREFERENCE", "CONTEXT"):
        assert env.services.memories.create(env.ws, f"{kind} 内容", kind=kind).kind == kind
    for bad in ("PERSONALITY", "PSYCHOLOGY", "ATTACHMENT_STYLE", "TRUST_SCORE", "LIKE_ME",
                "DANGER_SCORE", "fact", ""):
        with pytest.raises(DomainError):
            env.services.memories.create(env.ws, "内容", kind=bad)
    with pytest.raises(DomainError):
        env.services.memories.create(env.ws, "   ", kind="FACT")
    with pytest.raises(DomainError):
        env.services.memories.create(env.ws, "x" * 4001, kind="FACT")
    assert env.services.memories.create(env.ws, "x" * 4000, kind="FACT").content == "x" * 4000


def test_person_and_workspace_isolation(env):
    other = env.services.workspaces.create("另一个 Workspace")
    outsider = env.services.people.create(other.id, "外部人物")
    memory = env.services.memories.create(env.ws, "本空间记忆", kind="FACT", person_id=env.person.id)
    with pytest.raises(NotFoundError):
        env.services.memories.create(env.ws, "跨空间", kind="FACT", person_id=outsider.id)
    with pytest.raises(NotFoundError):
        env.services.memories.get(other.id, memory.id)
    with pytest.raises(NotFoundError):
        env.services.memories.update(other.id, memory.id, content="改")
    with pytest.raises(NotFoundError):
        env.services.memories.soft_delete(other.id, memory.id)
    with pytest.raises(NotFoundError):
        env.services.memories.list(uuid4())
    assert env.services.memories.list_all(other.id) == []


def test_duplicate_looking_content_is_allowed(env):
    first = env.services.memories.create(env.ws, "张三喜欢摄影", kind="PREFERENCE",
                                         person_id=env.person.id)
    second = env.services.memories.create(env.ws, "张三喜欢摄影活动", kind="PREFERENCE",
                                          person_id=env.person.id)
    third = env.services.memories.create(env.ws, "张三喜欢摄影", kind="PREFERENCE",
                                         person_id=env.person.id)
    assert len({first.id, second.id, third.id}) == 3
    assert len(env.services.memories.list(env.ws, person_id=env.person.id)) == 3


def test_person_memory_requires_an_existing_person(env):
    archived = env.services.people.create(env.ws, "会被归档")
    env.services.people.soft_delete(env.ws, archived.id)
    # An archived person still owns their recorded memories, so the reference holds.
    assert env.services.memories.create(env.ws, "归档人物记忆", kind="CONTEXT",
                                        person_id=archived.id).person_id == archived.id
    with pytest.raises(NotFoundError):
        env.services.memories.create(env.ws, "不存在的人物", kind="FACT", person_id=uuid4())


def test_kind_filter_and_limits(env):
    env.services.memories.create(env.ws, "A", kind="FACT")
    env.services.memories.create(env.ws, "B", kind="PREFERENCE")
    env.services.memories.create(env.ws, "C", kind="CONTEXT")
    assert [item.kind for item in env.services.memories.list_all(env.ws, kind="FACT")] == ["FACT"]
    assert len(env.services.memories.list_all(env.ws, limit=2)) == 2
    with pytest.raises(DomainError):
        env.services.memories.list_all(env.ws, kind="PERSONALITY")


# -------------------------------------------------------------------- context

def context_request(env, **overrides):
    values = dict(workspace_id=env.ws, target_type="PERSON", target_id=env.person.id,
                  analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=ALL_TIME)
    values.update(overrides)
    return AnalysisRequest(**values)


def test_memories_are_absent_from_context_by_default(env):
    env.services.memories.create(env.ws, "默认不应出现的记忆", kind="FACT", person_id=env.person.id)
    snapshot = env.services.analysis.build_snapshot(context_request(env))
    assert "默认不应出现的记忆" not in snapshot.rendered_context
    assert snapshot.scope["included_memory_ids"] == []
    assert snapshot.scope["include"]["memories"] is False


def test_include_memories_adds_current_memories_and_records_ids(env):
    person_memory = env.services.memories.create(env.ws, "人物级记忆内容", kind="PREFERENCE",
                                                 person_id=env.person.id)
    workspace_memory = env.services.memories.create(env.ws, "Workspace 级记忆内容", kind="CONTEXT")
    snapshot = env.services.analysis.build_snapshot(context_request(env, include_memories=True))
    assert "人物级记忆内容" in snapshot.rendered_context
    assert "Workspace 级记忆内容" in snapshot.rendered_context
    assert set(snapshot.scope["included_memory_ids"]) == {str(person_memory.id), str(workspace_memory.id)}
    assert snapshot.scope["include"]["memories"] is True
    section = next(item for item in snapshot.sections if item.key == "memories")
    assert "用户确认" in section.note and section.kind == "FACT"


def test_deleted_and_expired_memories_never_enter_context(env):
    deleted = env.services.memories.create(env.ws, "已删除记忆内容", kind="FACT",
                                           person_id=env.person.id)
    env.services.memories.soft_delete(env.ws, deleted.id)
    expired = env.services.memories.create(env.ws, "已过期记忆内容", kind="FACT",
                                           person_id=env.person.id, valid_until=NOW - timedelta(days=1))
    future = env.services.memories.create(env.ws, "尚未生效记忆内容", kind="FACT",
                                          person_id=env.person.id, valid_from=NOW + timedelta(days=1))
    keep = env.services.memories.create(env.ws, "仍然有效的记忆内容", kind="FACT",
                                        person_id=env.person.id)
    snapshot = env.services.analysis.build_snapshot(context_request(env, include_memories=True))
    text = snapshot.rendered_context
    assert "仍然有效的记忆内容" in text
    assert "已删除记忆内容" not in text
    assert "已过期记忆内容" not in text
    assert "尚未生效记忆内容" not in text
    assert snapshot.scope["included_memory_ids"] == [str(keep.id)]
    # Explicit selection cannot resurrect a deleted or expired memory either.
    selected = env.services.analysis.build_snapshot(context_request(
        env, include_memories=False, selected_memory_ids=(deleted.id, expired.id, future.id, keep.id)))
    assert selected.scope["included_memory_ids"] == [str(keep.id)]
    assert "已删除记忆内容" not in selected.rendered_context


def test_memories_of_other_people_are_not_included(env):
    env.services.memories.create(env.ws, "别人的人物记忆", kind="FACT", person_id=env.other.id)
    env.services.memories.create(env.ws, "我的记忆", kind="FACT", person_id=env.person.id)
    snapshot = env.services.analysis.build_snapshot(context_request(env, include_memories=True))
    assert "我的记忆" in snapshot.rendered_context
    assert "别人的人物记忆" not in snapshot.rendered_context


def test_relationship_and_event_contexts_include_involved_people_memories(env):
    env.services.memories.create(env.ws, "甲的记忆", kind="FACT", person_id=env.person.id)
    env.services.memories.create(env.ws, "乙的记忆", kind="FACT", person_id=env.other.id)
    relationship_snapshot = env.services.analysis.build_snapshot(context_request(
        env, target_type="RELATIONSHIP", target_id=env.relationship.id,
        analysis_type="RELATIONSHIP_ANALYSIS", include_memories=True))
    assert "甲的记忆" in relationship_snapshot.rendered_context
    assert "乙的记忆" in relationship_snapshot.rendered_context
    event_snapshot = env.services.analysis.build_snapshot(context_request(
        env, target_type="EVENT", target_id=env.event.id, analysis_type="EVENT_ANALYSIS",
        include_memories=True))
    assert "甲的记忆" in event_snapshot.rendered_context
    assert "乙的记忆" not in event_snapshot.rendered_context


def test_analysis_with_memories_sends_them_and_records_them(env):
    memory = env.services.memories.create(env.ws, "进入请求的记忆内容", kind="PREFERENCE",
                                          person_id=env.person.id)
    request = context_request(env, include_memories=True)
    run = env.services.analysis.create_run(request)
    assert run.status == "SUCCEEDED"
    sent = json.loads(env.transport.requests[0]["body"].decode("utf-8"))
    assert "进入请求的记忆内容" in sent["messages"][1]["content"]
    view = env.services.analysis.get(env.ws, run.id)
    assert view.snapshot.scope["included_memory_ids"] == [str(memory.id)]
    assert "进入请求的记忆内容" in view.snapshot.rendered_context
    assert "进入请求的记忆内容" not in (run.result or "")


def test_memory_content_is_not_logged(env, caplog):
    with caplog.at_level(logging.DEBUG):
        memory = env.services.memories.create(env.ws, "极其私密的长期记忆", kind="FACT",
                                              person_id=env.person.id)
        env.services.memories.update(env.ws, memory.id, content="编辑后的私密长期记忆")
        env.services.memories.soft_delete(env.ws, memory.id)
        env.services.memories.restore(env.ws, memory.id)
        with pytest.raises(DomainError):
            env.services.memories.create(env.ws, "私密内容但 kind 非法", kind="PERSONALITY")
    assert "极其私密的长期记忆" not in caplog.text
    assert "编辑后的私密长期记忆" not in caplog.text
    assert "私密内容但 kind 非法" not in caplog.text


def test_memory_entity_enforces_invariants_directly():
    person = uuid4()
    base = dict(workspace_id=uuid4(), content="内容", kind="FACT", source_type="MANUAL",
                confirmed_at=NOW)
    assert Memory(**base).person_id is None
    assert Memory(**base, person_id=person).person_id == person
    with pytest.raises(DomainError):
        Memory(**{**base, "source_type": "ANALYSIS"})
    with pytest.raises(DomainError):
        Memory(**{**base, "source_analysis_id": uuid4()})
    with pytest.raises(DomainError):
        Memory(**{**base, "valid_from": NOW, "valid_until": NOW - timedelta(seconds=1)})
    with pytest.raises(DomainError):
        Memory(**{**base, "kind": "DANGER_SCORE"})
    with pytest.raises(DomainError):
        Memory(**{**base, "content": "  "})
