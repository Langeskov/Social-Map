"""ContextBuilder: explicit scope, fact-only rendering and privacy defaults."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from app.config.settings import AppConfig
from app.domain.errors import DomainError, NotFoundError
from app.services.application import create_application_services
from app.services.context import (ALL_TIME, CUSTOM, LAST_7_DAYS, LAST_30_DAYS, AnalysisRequest,
                                  ContextBuilder, event_instant, resolve_window)
from app.services.database import initialize_database
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.unit_of_work import transaction

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "context-data")
    runtime = initialize_database(config)
    try:
        services = create_application_services(config, runtime.sessions)
        workspace = services.initial_workspace
        yield SimpleEnv(config, runtime, services, workspace.id, tmp_path)
    finally:
        runtime.close()


class SimpleEnv:
    def __init__(self, config, runtime, services, ws, tmp_path):
        self.config, self.runtime, self.services, self.ws, self.tmp_path = (
            config, runtime, services, ws, tmp_path)


def seed(env):
    services = env.services
    alice = services.people.create(env.ws, "爱丽丝", real_name="Alice", occupation="摄影师")
    services.people.add_alias(env.ws, alice.id, "小爱", alias_type="NICKNAME")
    bob = services.people.create(env.ws, "鲍勃")
    relationship = services.relationships.create(env.ws, alice.id, bob.id, "Friend",
                                                 effective_at=NOW - timedelta(days=10))
    services.relationships.add_history(env.ws, relationship.id, "PAUSED",
                                       effective_at=NOW - timedelta(days=5))
    services.relationships.add_history(env.ws, relationship.id, "ACTIVE",
                                       effective_at=NOW + timedelta(days=5))
    past = services.events.create_event(env.ws, "摄影活动", "Meeting", "FACT",
                                        occurred_on=date(2026, 4, 1), participants=(alice.id, bob.id),
                                        description="一起拍照")
    recent = services.events.create_event(env.ws, "近期通话", "Call", "FACT",
                                          started_at=NOW - timedelta(days=2), participants=(alice.id,))
    future = services.events.create_event(env.ws, "未来会议", "Meeting", "FACT",
                                          started_at=NOW + timedelta(days=10), participants=(alice.id,))
    observation = services.events.create_event(env.ws, "我的观察", "Observation", "OBSERVATION",
                                               occurred_on=date(2026, 4, 2), participants=(alice.id,),
                                               description="她当时更主动")
    text_evidence = services.evidence.create_evidence(
        env.ws, recent.id, "TEXT", "聊天记录", content_text="原始聊天文字：明天一起拍照",
        annotation="活动前一天的记录", source_type="CHAT", source_label="微信群",
        captured_at=NOW - timedelta(days=2))
    image_path = env.tmp_path / "photo.png"
    Image.new("RGB", (5, 4), (10, 20, 30)).save(image_path, "PNG")
    media = services.media.import_file(env.ws, image_path)
    image_evidence = services.evidence.create_evidence(env.ws, past.id, "IMAGE", "现场照片",
                                                       source_type="IMAGE", media=(media.id,))
    other_workspace = services.workspaces.create("另一个 Workspace")
    outsider = services.people.create(other_workspace.id, "外部人物")
    return SimpleNamespace(alice=alice, bob=bob, relationship=relationship, past=past, recent=recent,
                           future=future, observation=observation, text_evidence=text_evidence,
                           image_evidence=image_evidence, media=media, other=other_workspace,
                           outsider=outsider)


def person_request(env, **overrides):
    values = dict(workspace_id=env.ws, target_type="PERSON", target_id=None,
                  analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=LAST_30_DAYS)
    values.update(overrides)
    return AnalysisRequest(**values)


def build(env, request):
    with transaction(env.runtime.sessions) as session:
        return ContextBuilder(sql_analysis_repositories(session), clock=lambda: NOW).build(request)


def test_person_context_contains_facts_relationships_history_and_events(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id))
    text = snapshot.rendered_context
    assert "爱丽丝" in text and "real_name=Alice" in text
    assert "小爱" in text                                     # alias
    assert data.relationship.relationship_type in text        # current relationship
    assert "PAUSED" in text and "ACTIVE" in text              # history rows
    assert "近期通话" in text
    assert snapshot.scope["person_ids"] == [str(data.alice.id)]
    assert str(data.relationship.id) in snapshot.scope["relationship_ids"]
    assert snapshot.target_type == "PERSON"


def test_relationship_context_covers_both_endpoints_and_history(env):
    data = seed(env)
    snapshot = build(env, person_request(
        env, target_type="RELATIONSHIP", target_id=data.relationship.id,
        analysis_type="RELATIONSHIP_ANALYSIS"))
    text = snapshot.rendered_context
    # UNDIRECTED relationships are stored with endpoints sorted by UUID, so A/B
    # assignment is not fixed: assert the names appear as endpoints either way.
    endpoint_lines = [line for line in text.splitlines() if line.startswith(("- A=", "- B="))]
    assert len(endpoint_lines) == 2
    assert any("爱丽丝" in line for line in endpoint_lines)
    assert any("鲍勃" in line for line in endpoint_lines)
    assert "关系历史" in text and "PAUSED" in text
    assert set(snapshot.scope["person_ids"]) == {str(data.alice.id), str(data.bob.id)}


def test_event_context_includes_participants_and_evidence(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_type="EVENT", target_id=data.recent.id,
                                         analysis_type="EVENT_ANALYSIS"))
    text = snapshot.rendered_context
    assert "近期通话" in text and "爱丽丝" in text
    assert "聊天记录" in text
    assert snapshot.scope["event_ids"] == [str(data.recent.id)]
    assert str(data.text_evidence.id) in snapshot.scope["evidence_ids"]


def test_workspace_isolation_is_enforced(env):
    data = seed(env)
    with pytest.raises(NotFoundError):
        build(env, person_request(env, target_id=data.outsider.id))
    with pytest.raises(NotFoundError):
        build(env, person_request(env, target_id=uuid4()))
    with pytest.raises(NotFoundError):
        build(env, person_request(env, workspace_id=uuid4(), target_id=data.alice.id))
    with pytest.raises(NotFoundError):
        build(env, person_request(env, target_type="RELATIONSHIP", target_id=uuid4(),
                                  analysis_type="RELATIONSHIP_ANALYSIS"))
    assert "外部人物" not in build(env, person_request(env, target_id=data.alice.id)).rendered_context


def test_deleted_facts_are_excluded_and_archived_targets_refused(env):
    data = seed(env)
    env.services.events.soft_delete_event(env.ws, data.recent.id)
    env.services.people.soft_delete(env.ws, data.bob.id)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME))
    text = snapshot.rendered_context
    assert "近期通话" not in text                      # deleted event
    assert str(data.text_evidence.id) not in snapshot.scope["evidence_ids"]
    with pytest.raises(NotFoundError):
        build(env, person_request(env, target_id=data.bob.id))


def test_future_history_is_marked_and_never_current(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME))
    assert "计划中" in snapshot.rendered_context
    # The current state is derived only from rows effective at or before as_of.
    current = [line for line in snapshot.rendered_context.splitlines()
               if line.startswith("- ") and "计划中" not in line and " · ACTIVE" in line]
    assert current and "PAUSED" in snapshot.rendered_context


def test_as_of_and_time_window_control_the_event_set(env):
    data = seed(env)
    recent_window = build(env, person_request(env, target_id=data.alice.id, time_scope=LAST_7_DAYS))
    assert "近期通话" in recent_window.rendered_context
    assert "摄影活动" not in recent_window.rendered_context      # older than 7 days
    assert "未来会议" not in recent_window.rendered_context      # after as_of
    all_window = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME))
    assert "摄影活动" in all_window.rendered_context             # ALL has no lower bound
    # as_of is the upper bound of every scope: a future record is not yet a fact.
    # The rendered header states the cutoff, so the model can see it.
    assert "未来会议" not in all_window.rendered_context
    assert all_window.scope["time_window"]["end"] == NOW.isoformat()
    assert "as_of=" in all_window.rendered_context


def test_resolve_window_supports_every_scope_and_custom_validation():
    week = resolve_window(LAST_7_DAYS, as_of=NOW)
    assert week.start == NOW - timedelta(days=7) and week.end == NOW
    assert resolve_window(ALL_TIME, as_of=NOW).start is None
    custom = resolve_window(CUSTOM, as_of=NOW, custom_start=NOW - timedelta(days=3))
    assert custom.start == NOW - timedelta(days=3) and custom.end == NOW
    with pytest.raises(DomainError):
        resolve_window(CUSTOM, as_of=NOW)
    with pytest.raises(DomainError):
        resolve_window(CUSTOM, as_of=NOW, custom_start=NOW, custom_end=NOW - timedelta(days=1))
    with pytest.raises(DomainError):
        resolve_window(CUSTOM, as_of=NOW, custom_start=datetime(2026, 1, 1))
    with pytest.raises(DomainError):
        resolve_window("LAST_YEAR", as_of=NOW)
    with pytest.raises(DomainError):
        AnalysisRequest(workspace_id=uuid4(), target_type="PERSON", target_id=uuid4(),
                        analysis_type="PERSON_PROFILE", as_of=datetime(2026, 1, 1))


def test_selected_events_are_included_even_outside_the_window(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=LAST_7_DAYS,
                                         selected_event_ids=(data.future.id, data.observation.id)))
    text = snapshot.rendered_context
    assert "未来会议" in text and "我的观察" in text
    assert str(data.future.id) in snapshot.scope["event_ids"]


def test_selected_evidence_restricts_the_evidence_set(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME,
                                         selected_evidence_ids=(data.image_evidence.id,)))
    assert str(data.image_evidence.id) in snapshot.scope["evidence_ids"]
    assert str(data.text_evidence.id) not in snapshot.scope["evidence_ids"]


def test_observations_toggle_and_section(env):
    data = seed(env)
    with_observations = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME,
                                                  include_observations=True))
    assert "我的观察" in with_observations.rendered_context
    assert any(section.kind == "OBSERVATION" for section in with_observations.sections)
    without = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME,
                                        include_observations=False))
    assert "我的观察" not in without.rendered_context
    kinds = {section.kind for section in without.sections}
    assert "OBSERVATION" not in kinds


def test_evidence_is_metadata_only_by_default(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME))
    text = snapshot.rendered_context
    assert "聊天记录" in text and "captured_at=" in text and "source=CHAT" in text
    assert "微信群" in text                                    # source label is metadata
    assert "原始聊天文字" not in text                           # raw content withheld
    assert "活动前一天的记录" not in text                        # annotation withheld too
    assert "仅元数据" in text


def test_evidence_content_is_sent_only_when_explicitly_requested(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME,
                                         include_evidence=True))
    text = snapshot.rendered_context
    assert "原始聊天文字" in text and "活动前一天的记录" in text
    assert "含用户明确选择的原文" in text


def test_image_evidence_contributes_metadata_but_no_binary(env):
    data = seed(env)
    snapshot = build(env, person_request(env, target_id=data.alice.id, time_scope=ALL_TIME,
                                         include_evidence=True))
    text = snapshot.rendered_context
    assert "现场照片" in text and "image/png" in text            # mime + size, from Media metadata
    assert "base64" not in text.lower()
    # No binary payload can appear: only text lines are rendered.
    assert all(isinstance(line, str) for section in snapshot.sections for line in section.lines)


def test_context_size_guard_refuses_instead_of_truncating(env):
    data = seed(env)
    request = person_request(env, target_id=data.alice.id, time_scope=ALL_TIME, max_context_tokens=5)
    with pytest.raises(DomainError) as failure:
        build(env, request)
    assert "Selected context exceeds model context limit." in str(failure.value)
    assert "缩" in str(failure.value) or "缩小" in str(failure.value)


def test_unsupported_target_type_is_rejected(env):
    data = seed(env)
    request = person_request(env, target_id=data.alice.id)
    object.__setattr__(request, "target_type", "WORKSPACE")
    with pytest.raises(DomainError):
        build(env, request)


def test_event_instant_handles_both_event_kinds():
    from app.domain.entities import Event
    started = datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc)
    assert event_instant(Event(workspace_id=uuid4(), title="t", event_type="k",
                               started_at=started)) == started
    assert event_instant(Event(workspace_id=uuid4(), title="t", event_type="k",
                               occurred_on=date(2026, 5, 1))) == datetime(2026, 5, 1, tzinfo=timezone.utc)
