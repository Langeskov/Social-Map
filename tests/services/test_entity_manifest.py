"""Context Entity Manifest — Source Exposure Contract tests (Phase 13.1).

These pin the four required matrices of the 13.0 supplement decision: which
entities enter the source set, with which exposure level, how markers match the
manifest, and how the same manifest drives persisted ``analysis_*_source`` rows.
The candidate-reference "limited status" test belongs to Phase 13.2's validator.
"""
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.application import create_application_services
from app.services.context import ALL_TIME, AnalysisRequest, ContextBuilder, snapshot_from_mapping
from app.services.database import initialize_database
from app.services.entity_manifest import (CONTENT_INCLUDED, EVENT, EVIDENCE,
                                          IDENTITY_ONLY, MANIFEST_VERSION,
                                          METADATA_ONLY, PARTICIPANT_CONTEXT,
                                          PERSON, PROFILE, RELATIONSHIP,
                                          RELATIONSHIP_FACT, ROLE_EVENT_PARTICIPANT,
                                          ROLE_RELATIONSHIP_ENDPOINT, ROLE_TARGET,
                                          ManifestBuilder, entity_marker,
                                          unmarked_entity_ids)
from app.services.llm import (FakeTransport, HttpResponse, LLMConfigStore, LLMGateway,
                              LLMProviderConfig)
from app.services.unit_of_work import transaction
from app.storage.models import (EventModel, EvidenceModel, PersonModel,
                                RelationshipHistoryModel, RelationshipModel)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
ANSWER = "## Facts（事实）\n有一场活动。"


def chat_response():
    payload = {"id": "chatcmpl-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": ANSWER}}],
               "usage": {"total_tokens": 42}}
    return HttpResponse(200, json.dumps(payload).encode(), {}, "http://x/v1/chat/completions")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIS_TEST_KEY", "sk-test-secret-value")
    config = AppConfig(data_dir=tmp_path / "manifest-data")
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
        person = services.people.create(workspace.id, "爱丽丝", occupation="摄影师")
        other = services.people.create(workspace.id, "鲍勃")
        relationship = services.relationships.create(workspace.id, person.id, other.id, "协作伙伴")
        event = services.events.create_event(workspace.id, "深夜通话", "Call", "FACT",
                                             started_at=NOW,
                                             participants=(person.id,))
        evidence = services.evidence.create_evidence(workspace.id, event.id, "TEXT", "聊天记录",
                                                     content_text="私人聊天原文",
                                                     annotation="用户注释内容", source_type="CHAT")
        yield SimpleNamespace(config=config, runtime=runtime, services=services, ws=workspace.id,
                              person=person, other=other, relationship=relationship,
                              event=event, evidence=evidence, transport=transport)
    finally:
        runtime.close()


def request_for(env, target_type="PERSON", **overrides):
    targets = {"PERSON": env.person.id, "RELATIONSHIP": env.relationship.id,
               "EVENT": env.event.id}
    types = {"PERSON": "PERSON_PROFILE", "RELATIONSHIP": "RELATIONSHIP_ANALYSIS",
             "EVENT": "EVENT_ANALYSIS"}
    values = dict(workspace_id=env.ws, target_type=target_type,
                  target_id=targets[target_type],
                  analysis_type=types[target_type], as_of=NOW, time_scope=ALL_TIME)
    values.update(overrides)
    return AnalysisRequest(**values)


def build(env, request):
    with transaction(env.runtime.sessions) as session:
        return ContextBuilder(sql_analysis_repositories(session)).build(request)


MARKER_RE = re.compile(r"\[(PERSON|RELATIONSHIP|EVENT|EVIDENCE|MEMORY):"
                       r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                       r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]")


# ----------------------------------------------------------- exposure matrices
def test_person_target_has_profile_self_and_identity_only_endpoints(env):
    snapshot = build(env, request_for(env))
    manifest = snapshot.entity_manifest
    target = manifest.get(PERSON, env.person.id)
    endpoint = manifest.get(PERSON, env.other.id)
    assert target is not None and PROFILE in target.exposures
    assert ROLE_TARGET in target.roles
    assert endpoint is not None and IDENTITY_ONLY in endpoint.exposures
    assert ROLE_RELATIONSHIP_ENDPOINT in endpoint.roles
    # Limited exposure is stated in the context itself.
    assert entity_marker(PERSON, env.other.id) in snapshot.rendered_context
    assert "（仅身份信息，未提供完整档案）" in snapshot.rendered_context
    # Exactly one limited person (the target is PROFILE and gets no caveat line).
    assert snapshot.rendered_context.count("仅身份信息，未提供完整档案") == 1
    # Legacy scope lists stay unchanged while source_ids() grows.
    assert snapshot.scope["person_ids"] == [str(env.person.id)]
    assert {str(env.person.id), str(env.other.id)} == set(snapshot.source_ids()["PERSON"])


def test_relationship_target_has_identity_markers_for_both_endpoints(env):
    snapshot = build(env, request_for(env, "RELATIONSHIP"))
    manifest = snapshot.entity_manifest
    relationship = manifest.get(RELATIONSHIP, env.relationship.id)
    assert relationship is not None and RELATIONSHIP_FACT in relationship.exposures
    for person in (env.person, env.other):
        entry = manifest.get(PERSON, person.id)
        assert entry is not None and IDENTITY_ONLY in entry.exposures
        assert entity_marker(PERSON, person.id) in snapshot.rendered_context
    endpoint_lines = [line for line in snapshot.rendered_context.splitlines()
                      if line.startswith(("- A=", "- B="))]
    assert len(endpoint_lines) == 2
    for line in endpoint_lines:
        assert MARKER_RE.search(line) is not None


def test_event_target_has_participant_identity_and_role_markers(env):
    snapshot = build(env, request_for(env, "EVENT"))
    manifest = snapshot.entity_manifest
    event = manifest.get(EVENT, env.event.id)
    assert event is not None and "EVENT_DETAIL" in event.exposures
    participant = manifest.get(PERSON, env.person.id)
    assert participant is not None and PARTICIPANT_CONTEXT in participant.exposures
    assert ROLE_EVENT_PARTICIPANT in participant.roles
    assert "参与人：" in snapshot.rendered_context
    assert "（参与人信息，未提供完整档案）" in snapshot.rendered_context
    assert set(snapshot.source_ids()["PERSON"]) == {str(env.person.id)}


def test_other_workspace_persons_never_enter_the_source_set(env):
    other_workspace = env.services.workspaces.create("外部工作区")
    outsider = env.services.people.create(other_workspace.id, "外部人物")
    snapshot = build(env, request_for(env))
    assert snapshot.entity_manifest.get(PERSON, outsider.id) is None
    assert str(outsider.id) not in snapshot.source_ids()["PERSON"]
    assert "外部人物" not in snapshot.rendered_context


# ------------------------------------------------- manifest <-> markers <-> rows
def test_manifest_ids_equal_the_rendered_markers(env):
    snapshot = build(env, request_for(env))
    rendered = snapshot.rendered_context
    marked = {(match.group(1), match.group(2).lower()) for match in MARKER_RE.finditer(rendered)}
    entries = {(entry.entity_type, str(entry.entity_id).lower())
               for entry in snapshot.entity_manifest.entries}
    assert marked == entries
    assert unmarked_entity_ids(rendered) == ()


def test_same_person_multiple_roles_are_merged_into_one_entry(env):
    # Two relationships to the same person: the endpoint is sighted twice.
    env.services.relationships.create(env.ws, env.person.id, env.other.id, "项目同伴")
    snapshot = build(env, request_for(env))
    entries = [item for item in snapshot.entity_manifest.by_type(PERSON)
               if item.entity_id == env.other.id]
    assert len(entries) == 1
    assert entries[0].roles == (ROLE_RELATIONSHIP_ENDPOINT,)
    assert entries[0].exposures == (IDENTITY_ONLY,)
    assert snapshot.source_ids()["PERSON"].count(str(env.other.id)) == 1
    # The merge logic itself unions roles and exposures deterministically.
    builder = ManifestBuilder()
    builder.add(PERSON, env.other.id, label="鲍勃", role=ROLE_RELATIONSHIP_ENDPOINT,
                exposure=IDENTITY_ONLY)
    builder.add(PERSON, env.other.id, label="鲍勃", role=ROLE_EVENT_PARTICIPANT,
                exposure=PARTICIPANT_CONTEXT)
    merged = builder.build().get(PERSON, env.other.id)
    assert set(merged.roles) == {ROLE_RELATIONSHIP_ENDPOINT, ROLE_EVENT_PARTICIPANT}
    assert set(merged.exposures) == {IDENTITY_ONLY, PARTICIPANT_CONTEXT}


def test_evidence_metadata_only_is_not_marked_as_content_included(env):
    with_content = build(env, request_for(env, include_evidence=True))
    entry = with_content.entity_manifest.get(EVIDENCE, env.evidence.id)
    assert entry is not None and CONTENT_INCLUDED in entry.exposures
    assert "私人聊天原文" in with_content.rendered_context
    metadata_only = build(env, request_for(env, include_evidence=False))
    entry = metadata_only.entity_manifest.get(EVIDENCE, env.evidence.id)
    assert entry is not None and entry.exposures == (METADATA_ONLY,)
    assert "私人聊天原文" not in metadata_only.rendered_context
    assert "用户注释内容" not in metadata_only.rendered_context


def test_manifest_matches_persisted_sources_exactly(env):
    run = env.services.analysis.create_run(request_for(env, include_evidence=True))
    view = env.services.analysis.get(env.ws, run.id)
    stored = snapshot_from_mapping(json.loads(run.context_snapshot))
    assert stored.entity_manifest.entries
    persisted = {"PERSON": {str(item.target_id) for item in view.sources.people},
                 "RELATIONSHIP": {str(item.target_id) for item in view.sources.relationships},
                 "EVENT": {str(item.target_id) for item in view.sources.events},
                 "EVIDENCE": {str(item.target_id) for item in view.sources.evidence}}
    derived = {kind: set(values) for kind, values in stored.source_ids().items()}
    assert persisted == derived
    # And the run did not touch any fact row.
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(PersonModel)) == 2
        assert session.scalar(select(func.count()).select_from(RelationshipModel)) == 1
        assert session.scalar(select(func.count()).select_from(EventModel)) == 1
        assert session.scalar(select(func.count()).select_from(EvidenceModel)) == 1
        assert session.scalar(select(func.count()).select_from(RelationshipHistoryModel)) >= 1


# --------------------------------------------------------- history / versioning
def test_manifest_is_versioned_and_roundtrips(env):
    snapshot = build(env, request_for(env))
    mapping = snapshot.to_mapping()
    assert mapping["entity_manifest"]["version"] == MANIFEST_VERSION
    rebuilt = snapshot_from_mapping(mapping)
    assert rebuilt.entity_manifest == snapshot.entity_manifest
    assert rebuilt.source_ids() == snapshot.source_ids()


def test_legacy_snapshot_without_manifest_keeps_legacy_source_rules(env):
    snapshot = build(env, request_for(env))
    mapping = snapshot.to_mapping()
    del mapping["entity_manifest"]                       # a pre-13.1 stored run
    before = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    legacy = snapshot_from_mapping(mapping)
    assert legacy.entity_manifest.entries == ()
    assert legacy.included_memory_ids == ()
    assert legacy.source_ids() == {
        "PERSON": (str(env.person.id),),
        "RELATIONSHIP": (str(env.relationship.id),),
        "EVENT": (str(env.event.id),),
        "EVIDENCE": (str(env.evidence.id),)}
    # Historical runs are never rewritten or upgraded by reading them back.
    assert json.dumps(mapping, ensure_ascii=False, sort_keys=True) == before
