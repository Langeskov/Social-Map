"""Token estimation boundaries (Phase 13.0 / 6.5): the estimate must stay
conservative for CJK and mixed text, and the context guard must refuse instead
of silently truncating. See docs/phase13-evaluation.md for the review notes.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config.settings import AppConfig
from app.domain.errors import DomainError
from app.services.application import create_application_services
from app.services.context import (ALL_TIME, CHARS_PER_TOKEN, CJK_CHARS_PER_TOKEN,
                                  AnalysisRequest, ContextBuilder, estimate_tokens)
from app.services.database import initialize_database
from app.services.analysis_repositories import sql_analysis_repositories
from app.services.unit_of_work import transaction

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- pure estimates
def test_documented_ratios_are_the_policy_values():
    # These constants *are* the documented policy; a silent change must fail here
    # and force a review of docs/phase13-evaluation.md.
    assert CHARS_PER_TOKEN == 2
    assert CJK_CHARS_PER_TOKEN == 1


def test_empty_and_single_characters():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1          # ceil(1/2)
    assert estimate_tokens("ab") == 1
    assert estimate_tokens("中") == 1         # wide char = 1 token
    assert estimate_tokens("中中") == 2


def test_ascii_estimate_is_conservative():
    text = "The quick brown fox jumps over the lazy dog, 0123456789, https://example.invalid/a?b=c."
    # Policy rate: ceil(len/2)…
    assert estimate_tokens(text) == (len(text) + 1) // 2
    # …and that is well above the practical ~4 chars/token reality.
    assert estimate_tokens(text) >= (len(text) + 3) // 4


def test_cjk_estimate_is_conservative_not_len_over_two():
    text = "海岚市光影工作室的摄影师爱丽丝正在筹备一场摄影展。" * 10
    assert estimate_tokens(text) == len(text)
    # The key regression: the old len/2 rule undercounted CJK by ~25% against the
    # 1.5-characters-per-token reality it claimed to bound.
    old_estimate = (len(text) + 1) // 2
    assert estimate_tokens(text) > old_estimate
    assert estimate_tokens(text) >= int(len(text) / 1.5)


def test_mixed_text_counts_wide_and_narrow_separately():
    text = "爱丽丝 (Alice) 于 2026-06-12 参与 call #42。"   # mixed CJK/ASCII/digits
    wide = sum(1 for ch in text if _wide(ch))
    narrow = len(text) - wide
    expected = wide + (narrow + 1) // 2
    assert estimate_tokens(text) == expected
    # Wide characters make the mixed estimate exceed the naive len/2 rule.
    assert estimate_tokens(text) > (len(text) + 1) // 2


def _wide(ch: str) -> bool:
    import unicodedata
    return unicodedata.east_asian_width(ch) in ("W", "F")


def test_fullwidth_forms_and_kana_count_as_wide():
    assert estimate_tokens("ＡＢ") == 2          # fullwidth ASCII
    assert estimate_tokens("カタカナ") == 4      # kana


# --------------------------------------------------- guard boundary (real build)
@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "token-data")
    runtime = initialize_database(config)
    try:
        services = create_application_services(config, runtime.sessions)
        yield SimpleNamespace(config=config, runtime=runtime, services=services,
                              ws=services.initial_workspace.id)
    finally:
        runtime.close()


def _build(env, **overrides):
    values = dict(workspace_id=env.ws, target_type="PERSON", target_id=None,
                  analysis_type="PERSON_PROFILE", as_of=NOW, time_scope=ALL_TIME)
    values.update(overrides)
    request = AnalysisRequest(**values)
    with transaction(env.runtime.sessions) as session:
        return ContextBuilder(sql_analysis_repositories(session), clock=lambda: NOW).build(request)


def test_context_guard_boundary_refuses_exactly_over_budget(env):
    person = env.services.people.create(env.ws, "爱丽丝", occupation="摄影师")
    env.services.events.create_event(env.ws, "深夜通话", "Call", "FACT",
                                     started_at=NOW - timedelta(days=3),
                                     participants=(person.id,),
                                     description="通话讨论展板尺寸与排期")
    baseline = _build(env, target_id=person.id)
    exact = baseline.estimated_tokens
    assert exact > 0

    # Exactly at the budget: accepted, nothing truncated.
    at_limit = _build(env, target_id=person.id, max_context_tokens=exact)
    assert at_limit.rendered_context == baseline.rendered_context
    assert at_limit.estimated_tokens == exact

    # One token below: explicit refusal with user guidance, never a silent cut.
    with pytest.raises(DomainError) as failure:
        _build(env, target_id=person.id, max_context_tokens=exact - 1)
    message = str(failure.value)
    assert "Selected context exceeds model context limit." in message
    assert "缩小" in message


def test_cjk_context_estimate_uses_conservative_counting(env):
    person = env.services.people.create(env.ws, "陈默", occupation="摄影师")
    env.services.events.create_event(
        env.ws, "长描述事件", "Meeting", "FACT",
        occurred_on=date(2026, 6, 10), participants=(person.id,),
        description="这是一段足够长的中文描述，用来验证中文字符在 token 估算中按字符计数。")
    snapshot = _build(env, target_id=person.id)
    text = snapshot.rendered_context
    wide = sum(1 for ch in text if _wide(ch))
    assert snapshot.estimated_tokens >= wide
