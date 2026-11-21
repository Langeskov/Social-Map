"""Reference verification statuses (Phase 13.2 / 8.5, 8.8).

Every deterministic status is exercised against the real services and SQLite:
VALID_FULL, VALID_LIMITED, INVALID_FORMAT, UNKNOWN_SOURCE, NOT_IN_RUN,
WORKSPACE_MISMATCH, TYPE_MISMATCH, DELETED_SOURCE, SOURCE_NOT_EXPOSED.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config.settings import AppConfig
from app.domain.entities.reference_validation import (DELETED_SOURCE, INVALID_FORMAT,
                                                      NOT_IN_RUN, SOURCE_NOT_EXPOSED,
                                                      TYPE_MISMATCH, UNKNOWN_SOURCE,
                                                      VALID_FULL, VALID_LIMITED,
                                                      WORKSPACE_MISMATCH,
                                                      ValidatedReference)
from app.services.analysis_result_parser import parse_analysis_result
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest
from app.services.database import initialize_database
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.services.reference_verification import ReferenceVerifier
from app.services.unit_of_work import transaction
from app.storage.models import AnalysisRunModel

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
ANSWER = "## Facts（事实）\n有一场活动。"


def chat_response(content=ANSWER):
    payload = {"id": "chatcmpl-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content}}]}
    return HttpResponse(200, json.dumps(payload).encode(), {}, "http://x/v1/chat/completions")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_TEST_KEY", "sk-test-secret-value")
    config = AppConfig(data_dir=tmp_path / "verification-data")
    runtime = initialize_database(config)
    transport = FakeTransport(default_response=chat_response())
    store = LLMConfigStore.for_data_dir(config.storage_paths.data_dir)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None)
    gateway.save_provider(LLMProviderConfig(provider_name="fake", base_url="http://127.0.0.1:9/v1",
                                            model="fake-model", credential_ref="env:RIS_TEST_KEY"))
    try:
        services = create_application_services(config, runtime.sessions, llm_gateway=gateway)
        workspace = services.initial_workspace
        person = services.people.create(workspace.id, "爱丽丝", occupation="摄影师")
        other = services.people.create(workspace.id, "鲍勃")
        relationship = services.relationships.create(workspace.id, person.id, other.id, "协作伙伴")
        event = services.events.create_event(workspace.id, "深夜通话", "Call", "FACT",
                                             started_at=NOW, participants=(person.id,))
        evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "聊天记录",
                                                     content_text="私人聊天原文", source_type="CHAT")
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              person=person, other=other, relationship=relationship,
                              event=event, evidence=evidence, transport=transport)
    finally:
        runtime.close()


def analyze(env, **overrides):
    values = dict(workspace_id=env.ws, target_type="PERSON", target_id=env.person.id,
                  analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=ALL_TIME)
    values.update(overrides)
    return env.services.analysis.create_run(AnalysisRequest(**values))


def verifier_for(env, run):
    """Open a read-only session and build the shared verifier for one run."""
    session_cm = transaction(env.runtime.sessions)
    session = session_cm.__enter__()
    repositories = sql_analysis_repositories(session)
    stored = repositories.runs.get(env.ws, run.id)
    sources = repositories.sources.list_by_run(env.ws, run.id)
    verifier = ReferenceVerifier.for_run(repositories, stored, sources)
    return verifier, session_cm


# ------------------------------------------------------------------- statuses
def test_valid_full_and_limited_follow_the_exposure_level(env):
    run = analyze(env)
    verifier, cm = verifier_for(env, run)
    try:
        assert verifier.verify("PERSON", env.person.id).status == VALID_FULL      # PROFILE
        endpoint = verifier.verify("PERSON", env.other.id)
        assert endpoint.status == VALID_LIMITED                                  # IDENTITY_ONLY
        assert "IDENTITY_ONLY" in endpoint.exposure
        assert verifier.verify("EVENT", env.event.id).status == VALID_FULL       # EVENT_DETAIL
        metadata_only = verifier.verify("EVIDENCE", env.evidence.id)
        assert metadata_only.status == VALID_LIMITED                             # METADATA_ONLY
    finally:
        cm.__exit__(None, None, None)


def test_invalid_format_unknown_and_type_mismatch(env):
    run = analyze(env)
    verifier, cm = verifier_for(env, run)
    try:
        bad = verifier.verify_markers("[EVENT:not-a-uuid]")
        assert [item.status for item in bad] == [INVALID_FORMAT]
        assert bad[0].entity_id is None and bad[0].reason
        unknown = verifier.verify("EVENT", uuid4())
        assert unknown.status == UNKNOWN_SOURCE and unknown.reason
        forged_type = verifier.verify("EVENT", env.person.id)   # Person UUID cited as EVENT
        assert forged_type.status == TYPE_MISMATCH
        assert "PERSON" in forged_type.reason
        memory = verifier.verify("MEMORY", env.person.id)
        assert memory.status == TYPE_MISMATCH
    finally:
        cm.__exit__(None, None, None)


def test_workspace_mismatch_is_not_unknown_source(env):
    other_workspace = env.services.workspaces.create("外部工作区")
    outsider = env.services.people.create(other_workspace.id, "外部人物")
    run = analyze(env)
    verifier, cm = verifier_for(env, run)
    try:
        result = verifier.verify("PERSON", outsider.id)
        assert result.status == WORKSPACE_MISMATCH
        assert "Workspace" in result.reason
    finally:
        cm.__exit__(None, None, None)


def test_deleted_source_is_distinct_from_unknown(env):
    run = analyze(env)
    env.services.events.soft_delete_event(env.ws, env.event.id)
    verifier, cm = verifier_for(env, run)
    try:
        result = verifier.verify("EVENT", env.event.id)
        assert result.status == DELETED_SOURCE
        assert "删除" in result.reason
    finally:
        cm.__exit__(None, None, None)


def test_source_not_exposed_when_manifest_lacks_the_entity(env):
    # No participant: this event is never rendered into the person-target context.
    hidden = env.services.events.create_event(env.ws, "未曝光事件", "Meeting", "FACT",
                                              started_at=NOW, participants=())
    run = analyze(env)
    verifier, cm = verifier_for(env, run)
    try:
        # A real object of this workspace that was never part of the rendered context.
        assert verifier.verify("EVENT", hidden.id).status == SOURCE_NOT_EXPOSED
    finally:
        cm.__exit__(None, None, None)


def test_not_in_run_on_legacy_snapshot_and_no_full_upgrade(env):
    run = analyze(env)
    with transaction(env.runtime.sessions) as session:
        repositories = sql_analysis_repositories(session)
        stored = repositories.runs.get(env.ws, run.id)
        # Legacy verifier: no manifest, empty recorded sources.
        legacy = ReferenceVerifier(repositories, workspace_id=env.ws, manifest=None,
                                   recorded={})
        alive = legacy.verify("EVENT", env.event.id)
        assert alive.status == NOT_IN_RUN
        assert "旧快照" in alive.reason
        # Recorded membership without a manifest is only VALID_LIMITED, never FULL.
        recorded = ReferenceVerifier(repositories, workspace_id=env.ws, manifest=None,
                                     recorded={"EVENT": {str(env.event.id)}})
        limited = recorded.verify("EVENT", env.event.id)
        assert limited.status == VALID_LIMITED
        assert "未确认曝光等级" in limited.reason
        assert stored is not None
    # The stored snapshot is untouched by verification (no backfill).
    with transaction(env.runtime.sessions) as session:
        raw = session.scalar(select(AnalysisRunModel.context_snapshot).where(
            AnalysisRunModel.id == run.id))
    assert "entity_manifest" in (raw or "")


def test_parsed_result_references_are_checked_per_entry(env):
    answer = json.dumps({
        "schema_version": "1",
        "facts": [{"content": "活动已发生",
                   "references": [{"type": "EVENT", "id": str(env.event.id)},
                                  {"type": "PERSON", "id": str(env.person.id)}]}],
        "observations": [], "interpretations": [{"content": "可能仍在联系"}],
        "uncertainties": [],
    }, ensure_ascii=False)
    env.transport.set_default(chat_response(answer))
    run = analyze(env)
    checks = env.services.analysis.reference_checks(env.ws, run.id)
    assert len(checks) == 2
    assert {item.reference.status for item in checks} == {VALID_FULL}
    assert all(item.section == "facts" and item.entry_index == 0 for item in checks)
    # An inference without a structural reference is not forced to cite anything:
    # no fabricated check is produced for it (human review decides support).
    assert all(item.section == "facts" for item in checks)


def test_verification_is_read_only_and_statuses_cannot_be_bypassed(env):
    run = analyze(env)
    before = env.services.analysis.get(env.ws, run.id).run.result
    checks = env.services.analysis.reference_checks(env.ws, run.id)
    assert checks == ()
    memory_records = env.services.memories.list_all(env.ws)
    assert memory_records == []
    assert env.services.analysis.get(env.ws, run.id).run.result == before
    # A caller cannot mint a "valid" status: only the documented vocabulary exists.
    with pytest.raises(ValueError):
        ValidatedReference(raw="x", entity_type="EVENT", entity_id=uuid4(),
                           status="ACCEPTED")


def test_candidate_service_uses_the_same_verifier(env):
    answer = ANSWER + f"\n引用 [EVENT:{env.event.id}] [PERSON:{env.other.id}]"
    env.transport.set_default(chat_response(answer))
    run = analyze(env)
    candidates = env.services.memories.candidate_references(env.ws, run.id)
    by_type = {item.entity_type: item for item in candidates}
    verifier, cm = verifier_for(env, run)
    try:
        assert by_type["EVENT"].status == verifier.verify("EVENT", env.event.id).status
        assert by_type["PERSON"].status == verifier.verify("PERSON", env.other.id).status
    finally:
        cm.__exit__(None, None, None)
