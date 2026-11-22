"""Gateway attempt accounting (Phase 13.5).

The retry count recorded on a run must be the *real* number of provider calls,
including runs that end in FAILED after exhausted retries. When a path cannot
prove the count (e.g. cancellation raised outside the retry loop), the count is
unknown — never fabricated as 0.
"""
import json

import pytest

from app.services.llm import (ATTEMPTS_ATTRIBUTE, CancelToken, ChatMessage, ChatRequest,
                              FakeTransport, HttpResponse, LLMAuthError,
                              LLMCancelledError, LLMConfigStore, LLMGateway,
                              LLMProviderConfig, LLMServerError, LLMTimeoutError,
                              attempts_from_error)


def response(status=200, content="ok", finish_reason="stop"):
    if status != 200:
        return HttpResponse(status, b'{"error":"private server detail"}', {},
                            "http://127.0.0.1:9/v1/chat/completions")
    payload = {"id": "chat-1", "model": "fake-model",
               "choices": [{"index": 0, "finish_reason": finish_reason,
                            "message": {"role": "assistant", "content": content}}],
               "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
    return HttpResponse(status, json.dumps(payload).encode(), {},
                        "http://127.0.0.1:9/v1/chat/completions")


def make_gateway(tmp_path, *, attempts=2, responses=(), default_response=None, error=None):
    store = LLMConfigStore.for_data_dir(tmp_path / "llm-data")
    transport = FakeTransport(responses, error=error, default_response=default_response)
    gateway = LLMGateway(store, transport=transport, sleeper=lambda _seconds: None,
                         attempts=attempts)
    gateway.save_provider(LLMProviderConfig(provider_name="fake",
                                            base_url="http://127.0.0.1:9/v1",
                                            model="fake-model"))
    return gateway, transport


def request():
    return ChatRequest(model="fake-model", messages=(ChatMessage("user", "hi"),))


def test_success_reports_the_first_attempt(tmp_path):
    gateway, transport = make_gateway(tmp_path, default_response=response())
    _answer, attempts = gateway.chat_with_attempts(request(), provider_name="fake")
    assert attempts == 1
    assert len(transport.requests) == 1


def test_success_after_retry_reports_the_real_count(tmp_path):
    gateway, transport = make_gateway(tmp_path,
                                      responses=[response(status=429), response()])
    _answer, attempts = gateway.chat_with_attempts(request(), provider_name="fake")
    assert attempts == 2
    assert len(transport.requests) == 2


def test_exhausted_retries_keep_the_real_count_on_the_error(tmp_path):
    gateway, transport = make_gateway(tmp_path, attempts=2,
                                      default_response=response(status=500))
    with pytest.raises(LLMServerError) as failure:
        gateway.chat_with_attempts(request(), provider_name="fake")
    assert len(transport.requests) == 2
    assert attempts_from_error(failure.value) == 2
    assert getattr(failure.value, ATTEMPTS_ATTRIBUTE) == 2


def test_non_retryable_failure_records_exactly_one_attempt(tmp_path):
    gateway, transport = make_gateway(tmp_path, default_response=response(status=401))
    with pytest.raises(LLMAuthError) as failure:
        gateway.chat_with_attempts(request(), provider_name="fake")
    assert len(transport.requests) == 1
    assert attempts_from_error(failure.value) == 1


def test_custom_attempt_budget_is_reflected(tmp_path):
    gateway, transport = make_gateway(tmp_path, attempts=3,
                                      default_response=response(status=503))
    with pytest.raises(LLMServerError) as failure:
        gateway.chat_with_attempts(request(), provider_name="fake")
    assert len(transport.requests) == 3
    assert attempts_from_error(failure.value) == 3


def test_cancellation_outside_the_loop_has_no_attempt_count(tmp_path):
    gateway, transport = make_gateway(tmp_path, default_response=response())
    token = CancelToken()
    token.cancel()
    with pytest.raises(LLMCancelledError) as failure:
        gateway.chat_with_attempts(request(), provider_name="fake", cancel=token)
    assert transport.requests == []
    assert attempts_from_error(failure.value) is None     # unavailable, not 0


def test_attempts_from_error_rejects_unprovable_values():
    class ForeignError(Exception):
        pass

    assert attempts_from_error(ForeignError("x")) is None
    error = LLMTimeoutError("timeout")
    error.attempts = 0
    assert attempts_from_error(error) is None
    error.attempts = True
    assert attempts_from_error(error) is None
    error.attempts = "2"
    assert attempts_from_error(error) is None
    error.attempts = 2
    assert attempts_from_error(error) == 2
