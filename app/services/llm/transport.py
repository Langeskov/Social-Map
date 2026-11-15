"""HTTP transport boundary: the provider never talks to a socket directly.

Tests inject their own transport, so no test can reach the network by accident.
`UrllibTransport` uses only the standard library: the project adds no HTTP client
dependency, and the wire format stays the OpenAI-compatible JSON API.
"""
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from app.services.llm.errors import LLMConnectionError, LLMTimeoutError


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict
    url: str = ""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _malformed(exc) from exc


def _malformed(exc):
    from app.services.llm.errors import LLMResponseError
    return LLMResponseError(f"模型返回的不是合法 JSON（{type(exc).__name__}）。")


class HttpTransport(Protocol):
    """One POST, no retries, no logging of bodies or headers."""

    def post(self, url: str, *, headers: dict, body: bytes, timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTP POST with explicit timeout and error mapping."""

    def post(self, url, *, headers, body, timeout):
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(), dict(response.headers), url)
        except urllib.error.HTTPError as exc:
            # HTTP statuses are data here, not exceptions: the provider classifies them.
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return HttpResponse(exc.code, payload, dict(exc.headers or {}), url)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeoutError(f"模型请求超时（>{timeout:g}s）。") from exc
            raise LLMConnectionError("无法连接模型服务，请检查 base_url 与网络。") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LLMTimeoutError(f"模型请求超时（>{timeout:g}s）。") from exc
        except OSError as exc:
            raise LLMConnectionError("无法连接模型服务，请检查 base_url 与网络。") from exc

    def get(self, url, *, headers, timeout):
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(), dict(response.headers), url)
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return HttpResponse(exc.code, payload, dict(exc.headers or {}), url)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeoutError(f"模型请求超时（>{timeout:g}s）。") from exc
            raise LLMConnectionError("无法连接模型服务，请检查 base_url 与网络。") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LLMTimeoutError(f"模型请求超时（>{timeout:g}s）。") from exc
        except OSError as exc:
            raise LLMConnectionError("无法连接模型服务，请检查 base_url 与网络。") from exc


class FakeTransport:
    """Deterministic transport for tests: queued responses, no network.

    A queued response is consumed first (FIFO), so a test can push the exact
    failures it wants to exercise; `default_response` answers everything else.
    """

    def __init__(self, responses=(), *, error=None, default_response=None):
        self.requests = []
        self._responses = list(responses)
        self._error = error
        self._default = default_response

    def push(self, response):
        self._responses.append(response)

    def set_error(self, error):
        self._error = error

    def set_default(self, response):
        self._default = response

    def _next(self):
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError("FakeTransport has no queued response")

    def post(self, url, *, headers, body, timeout):
        self.requests.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return self._next()

    def get(self, url, *, headers, timeout):
        self.requests.append({"url": url, "headers": dict(headers), "body": None, "timeout": timeout})
        return self._next()
