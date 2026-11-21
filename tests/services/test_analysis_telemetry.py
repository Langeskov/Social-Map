"""Run telemetry hashes and payload merging (Phase 13.3 / 9.4).

Hashes are identification aids only: deterministic for identical input, changing
with content, with a documented canonical form (sorted keys, UTF-8, None != "",
volatile run values excluded, array order preserved).
"""
import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from app.services.analysis_telemetry import (PROMPT_TEMPLATE_VERSION, TELEMETRY_VERSION,
                                            build_partial_request_telemetry,
                                            build_request_telemetry, canonical_json,
                                            complete_telemetry, context_snapshot_hash,
                                            fail_telemetry, prompt_template_hash,
                                            request_payload_hash, scope_with_telemetry,
                                            sha256_hex, telemetry_from_scope)
from app.services.context import ContextSection, ContextSnapshot
from app.services.llm import ChatMessage, ChatRequest, ChatResponse

RUN_ID = uuid4()
WS_ID = uuid4()
TARGET_ID = uuid4()
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def snapshot(*, run_id=RUN_ID, rendered="上下文正文", lines=("事件 A", "事件 B")):
    return ContextSnapshot(analysis_run_id=run_id, generated_at=NOW,
                           scope={"as_of": NOW.isoformat(),
                                  "time_window": {"scope": "ALL", "start": None},
                                  "analysis_run_id": str(run_id), "workspace_id": str(WS_ID)},
                           rendered_context=rendered,
                           sections=(ContextSection(key="events", title="事件（FACT）", kind="FACT",
                                                    lines=tuple(lines)),),
                           target_type="PERSON", target_id=TARGET_ID,
                           analysis_type="PERSON_PROFILE")


def request(content="system 内容", model="fake-model"):
    return ChatRequest(model=model, messages=(ChatMessage("system", content),
                                              ChatMessage("user", "上下文")))


# ------------------------------------------------------------------ canonical
def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    assert canonical_json({"a": "中文"}) == '{"a":"中文"}'      # ensure_ascii=False
    assert canonical_json({"a": None}) != canonical_json({"a": ""})   # None != ""
    assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


# ------------------------------------------------------------------ prompts
def test_prompt_template_hash_is_stable_and_content_sensitive(monkeypatch):
    first = prompt_template_hash()
    assert len(first) == 64 and prompt_template_hash() == first
    assert PROMPT_TEMPLATE_VERSION in ("1", "2", "3")
    import app.services.analysis_telemetry as telemetry_module
    monkeypatch.setattr(telemetry_module, "OUTPUT_TEMPLATE", "changed")
    assert prompt_template_hash() != first


# ------------------------------------------------------------------- context
def test_context_hash_ignores_volatile_run_values():
    first = context_snapshot_hash(snapshot(run_id=uuid4()))
    second = context_snapshot_hash(snapshot(run_id=uuid4()))
    assert first == second                       # analysis_run_id is excluded
    assert len(first) == 64


def test_context_hash_changes_with_content_and_keeps_array_order():
    assert context_snapshot_hash(snapshot(rendered="别的正文")) != context_snapshot_hash(snapshot())
    assert context_snapshot_hash(snapshot(lines=("事件 A", "事件 B"))) != \
           context_snapshot_hash(snapshot(lines=("事件 B", "事件 A")))


# ------------------------------------------------------------------- request
def test_request_hash_is_stable_and_content_sensitive():
    assert request_payload_hash(request()) == request_payload_hash(request())
    assert request_payload_hash(request(content="改了")) != request_payload_hash(request())
    assert request_payload_hash(request(model="other-model")) != request_payload_hash(request())
    with_temp = ChatRequest(model="fake-model", temperature=0.5,
                            messages=(ChatMessage("system", "system 内容"),
                                      ChatMessage("user", "上下文")))
    assert request_payload_hash(with_temp) != request_payload_hash(request())


# ------------------------------------------------------------- merge / storage
def _response(**overrides):
    values = dict(content="回答", model="fake-model", finish_reason="stop",
                  usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                  raw_id="chatcmpl-1")
    values.update(overrides)
    return ChatResponse(**values)


def test_request_telemetry_records_identity_and_estimate():
    telemetry = build_request_telemetry(request(), provider_name="fake",
                                        requested_model="fake-model", snapshot=snapshot())
    assert telemetry["version"] == TELEMETRY_VERSION
    identity = telemetry["request"]
    assert identity["provider_name"] == "fake"
    assert identity["requested_model"] == "fake-model"
    assert identity["prompt_template_version"] == PROMPT_TEMPLATE_VERSION
    assert len(identity["prompt_hash"]) == 64
    assert len(identity["context_snapshot_hash"]) == 64
    assert len(identity["request_hash"]) == 64
    assert identity["analysis_schema_version"] == "1"
    assert identity["estimated_context_tokens"] > 0
    assert "response" not in telemetry and "execution" not in telemetry


def test_partial_telemetry_has_no_fake_hashes():
    telemetry = build_partial_request_telemetry(provider_name="fake", requested_model="fake-model")
    assert telemetry["request"]["provider_name"] == "fake"
    assert "context_snapshot_hash" not in telemetry["request"]
    assert "request_hash" not in telemetry["request"]


def test_complete_telemetry_merges_response_metadata():
    telemetry = build_request_telemetry(request(), provider_name="fake",
                                        requested_model="fake-model", snapshot=snapshot())
    completed = complete_telemetry(telemetry, _response(model="gateway-model"),
                                   attempts=2, duration_ms=123)
    assert completed["response"]["returned_model"] == "gateway-model"
    assert completed["response"]["finish_reason"] == {"raw": "stop", "category": "STOP"}
    assert completed["response"]["usage"]["available"] is True
    assert completed["response"]["provider_request_id"] == "chatcmpl-1"
    assert completed["execution"] == {"status": "SUCCEEDED", "duration_ms": 123,
                                      "retry_count": 1}
    assert completed["version"] == TELEMETRY_VERSION


def test_missing_usage_stays_unavailable_in_telemetry():
    completed = complete_telemetry(build_partial_request_telemetry(
        provider_name="fake", requested_model="fake-model"),
        _response(usage={}), attempts=1, duration_ms=1)
    assert completed["response"]["usage"] == {"available": False, "prompt_tokens": None,
                                              "completion_tokens": None, "total_tokens": None}


def test_fail_telemetry_records_execution_only():
    failed = fail_telemetry(build_partial_request_telemetry(
        provider_name="fake", requested_model="fake-model"),
        status="FAILED", duration_ms=42, error_class="LLMAuthError")
    assert failed["execution"]["status"] == "FAILED"
    assert failed["execution"]["error_class"] == "LLMAuthError"
    assert failed["execution"]["retry_count"] == 0
    assert "response" not in failed


def test_scope_telemetry_roundtrip_and_malformed_records():
    telemetry = complete_telemetry(build_partial_request_telemetry(
        provider_name="fake", requested_model="m"), _response(), attempts=1, duration_ms=5)
    scope = scope_with_telemetry({"a": 1}, telemetry)
    assert scope["a"] == 1 and telemetry_from_scope(scope) == telemetry
    assert telemetry_from_scope({}) is None
    assert telemetry_from_scope(None) is None
    assert telemetry_from_scope({"telemetry": {"version": "1"}}) == {"version": "1"}
    assert telemetry_from_scope({"telemetry": "not-a-dict"}) is None
    assert telemetry_from_scope({"telemetry": {"version": ""}}) is None
