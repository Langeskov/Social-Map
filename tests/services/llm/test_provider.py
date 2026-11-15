"""OpenAI-compatible provider over a fake transport: no test reaches the network."""
import json
from datetime import datetime, timezone

import pytest

from app.services.llm import (ChatMessage, ChatRequest, FakeTransport, HttpResponse, LLMAuthError,
                              LLMConfigError, LLMConnectionError, LLMContextTooLargeError,
                              LLMCredentialError, LLMNotFoundError, LLMRateLimitError,
                              LLMResponseError, LLMServerError, LLMTimeoutError,
                              OpenAICompatibleProvider)
from app.services.llm.credentials import CredentialResolver, EnvironmentCredentialStore, InMemoryCredentialStore


def response(status=200, payload=None, raw=None):
    body = raw if raw is not None else json.dumps(payload if payload is not None else {}).encode("utf-8")
    return HttpResponse(status, body, {}, "http://example.invalid/v1/chat/completions")


def chat_payload(content="分析结果", model="test-model"):
    return {"id": "chatcmpl-1", "model": model, "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def make_provider(transport, **overrides):
    values = dict(name="local", base_url="http://127.0.0.1:9/v1", model="test-model",
                  credential=None, transport=transport, timeout=5.0)
    values.update(overrides)
    return OpenAICompatibleProvider(**values)


def request(text="hello", **overrides):
    values = dict(model="test-model", messages=(ChatMessage("system", "s"), ChatMessage("user", text)))
    values.update(overrides)
    return ChatRequest(**values)


def test_base_url_is_normalised_and_validated():
    transport = FakeTransport([response(payload=chat_payload())])
    provider = make_provider(transport, base_url="  http://127.0.0.1:9/v1/  ")
    assert provider.base_url == "http://127.0.0.1:9/v1"
    provider.chat(request())
    assert transport.requests[0]["url"] == "http://127.0.0.1:9/v1/chat/completions"
    for bad in ("", "   ", "127.0.0.1:9/v1", "ftp://host/v1", None, 42):
        with pytest.raises(LLMConfigError):
            make_provider(transport, base_url=bad)
    with pytest.raises(LLMConfigError):
        make_provider(transport, model="  ")
    with pytest.raises(LLMConfigError):
        make_provider(transport, name="")


def test_api_key_reference_controls_the_authorization_header():
    transport = FakeTransport([response(payload=chat_payload())])
    provider = make_provider(transport, credential="sk-secret-value")
    provider.chat(request())
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer sk-secret-value"
    # The key never appears in the URL or the body.
    assert "sk-secret-value" not in transport.requests[0]["url"]
    assert "sk-secret-value" not in transport.requests[0]["body"].decode("utf-8")

    transport = FakeTransport([response(payload=chat_payload())])
    make_provider(transport, credential=None).chat(request())
    assert "Authorization" not in transport.requests[0]["headers"]


def test_credential_resolution_from_environment_and_os_store(monkeypatch):
    monkeypatch.setenv("RIS_TEST_KEY", "sk-from-env")
    resolver = CredentialResolver(os_store=InMemoryCredentialStore({"api": "sk-from-os"}))
    assert resolver.resolve("env:RIS_TEST_KEY") == "sk-from-env"
    assert resolver.resolve("os:api") == "sk-from-os"
    assert resolver.resolve(None) is None
    assert resolver.resolve("none") is None
    assert CredentialResolver.describe("env:RIS_TEST_KEY") == "env:RIS_TEST_KEY"
    assert CredentialResolver.describe(None) == "（无凭据）"
    with pytest.raises(LLMCredentialError):
        resolver.resolve("env:RIS_MISSING_KEY")
    with pytest.raises(LLMCredentialError):
        resolver.resolve("os:missing")
    with pytest.raises(LLMCredentialError):
        resolver.resolve("plain-secret-name")
    with pytest.raises(LLMCredentialError):
        resolver.resolve("env:")
    monkeypatch.delenv("RIS_TEST_KEY", raising=False)
    assert EnvironmentCredentialStore().get("RIS_TEST_KEY") is None


def test_success_parses_content_model_usage():
    transport = FakeTransport([response(payload=chat_payload("四段结果", model="gpt-x"))])
    result = make_provider(transport).chat(request("请分析"))
    assert result.content == "四段结果" and result.model == "gpt-x"
    assert result.finish_reason == "stop" and result.usage["total_tokens"] == 15
    body = json.loads(transport.requests[0]["body"].decode("utf-8"))
    assert body["model"] == "test-model"
    assert [item["role"] for item in body["messages"]] == ["system", "user"]
    assert "temperature" not in body and "max_tokens" not in body


def test_temperature_and_extra_headers_are_sent_when_configured():
    transport = FakeTransport([response(payload=chat_payload())])
    provider = make_provider(transport, temperature=0.25, extra_headers={"X-Tenant": "me"})
    provider.chat(request())
    body = json.loads(transport.requests[0]["body"].decode("utf-8"))
    assert body["temperature"] == 0.25
    assert transport.requests[0]["headers"]["X-Tenant"] == "me"


@pytest.mark.parametrize("status,expected", [(401, LLMAuthError), (403, LLMAuthError),
                                             (404, LLMNotFoundError), (429, LLMRateLimitError),
                                             (500, LLMServerError), (503, LLMServerError)])
def test_http_status_is_mapped_to_a_typed_safe_error(status, expected):
    transport = FakeTransport([response(status=status, raw=b'{"error":"private detail"}')])
    with pytest.raises(expected) as failure:
        make_provider(transport).chat(request())
    assert str(failure.value)
    assert "private detail" not in str(failure.value)
    assert failure.value.status == status


def test_context_too_large_error_is_recognised():
    raw = json.dumps({"error": {"message": "This model's maximum context length is 8192 tokens"}}).encode()
    transport = FakeTransport([response(status=400, raw=raw)])
    with pytest.raises(LLMContextTooLargeError):
        make_provider(transport).chat(request())
    assert "context limit" in str(LLMContextTooLargeError())


@pytest.mark.parametrize("error", [LLMTimeoutError("模型请求超时（>5s）。"),
                                   LLMConnectionError("无法连接模型服务，请检查 base_url 与网络。")])
def test_transport_failures_surface_as_typed_errors(error):
    transport = FakeTransport([], error=error)
    with pytest.raises(type(error)):
        make_provider(transport).chat(request())


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b'{"choices": []}',
                                 b'{"choices": [{}]}', b'{"choices": [{"message": {}}]}',
                                 b'{"choices": [{"message": {"content": "   "}}]}'])
def test_malformed_responses_are_rejected(raw):
    transport = FakeTransport([response(raw=raw)])
    with pytest.raises(LLMResponseError):
        make_provider(transport).chat(request())


def test_response_text_fallback_and_non_int_usage_are_tolerated():
    raw = json.dumps({"model": "m", "choices": [{"text": "fallback text"}],
                      "usage": {"total_tokens": "many"}}).encode()
    result = make_provider(FakeTransport([response(raw=raw)])).chat(request())
    assert result.content == "fallback text"
    assert result.usage == {}


def test_test_connection_and_list_models():
    models = {"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}]}
    transport = FakeTransport([response(payload=models), response(payload=models)])
    provider = make_provider(transport)
    assert provider.list_models() == ("a", "b")
    status = provider.test_connection()
    assert status.ok is True and "2" in status.message and status.models == ("a", "b")

    failing = make_provider(FakeTransport([response(status=401)], error=None))
    failure = failing.test_connection()
    assert failure.ok is False and "凭据" in failure.message

    assert make_provider(FakeTransport([response(status=401)])).test_connection().ok is False


def test_missing_models_endpoint_is_not_a_connection_failure():
    transport = FakeTransport([response(status=404, payload={})])
    provider = make_provider(transport)
    assert provider.list_models() == ()
