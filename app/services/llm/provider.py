"""Provider contract and the single OpenAI-compatible implementation.

`Fact` rows never reach this layer: a provider receives a finished `ChatRequest`
and returns text. Vendor-specific behaviour is limited to the (configurable)
base_url, model name and extra headers.
"""
import json
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urljoin

from app.services.llm.errors import (LLMAuthError, LLMConfigError, LLMContextTooLargeError,
                                     LLMNotFoundError, LLMRateLimitError, LLMResponseError,
                                     LLMServerError)
from app.services.llm.transport import HttpResponse, HttpTransport

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self):
        if self.role not in {ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT}:
            raise LLMConfigError("消息角色必须是 system、user 或 assistant。")
        if not isinstance(self.content, str) or not self.content.strip():
            raise LLMConfigError("消息内容不能为空。")


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    temperature: float | None = None
    timeout: float = 30.0
    max_tokens: int | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMConfigError("model 不能为空。")
        if not self.messages:
            raise LLMConfigError("至少需要一条消息。")
        if self.temperature is not None and not 0 <= float(self.temperature) <= 2:
            raise LLMConfigError("temperature 必须在 0 到 2 之间。")
        if self.timeout is None or float(self.timeout) <= 0:
            raise LLMConfigError("timeout 必须为正数。")

    def payload(self):
        body = {"model": self.model, "messages": [{"role": message.role, "content": message.content}
                                                  for message in self.messages]}
        if self.temperature is not None:
            body["temperature"] = float(self.temperature)
        if self.max_tokens is not None:
            body["max_tokens"] = int(self.max_tokens)
        return body


@dataclass(frozen=True)
class ChatResponse:
    content: str
    model: str
    finish_reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    raw_id: str | None = None


@dataclass(frozen=True)
class ProviderStatus:
    ok: bool
    message: str
    models: tuple[str, ...] = ()


class LLMProvider(Protocol):
    """The only way the analysis layer talks to a model."""

    name: str

    def chat(self, request: ChatRequest) -> ChatResponse: ...

    def test_connection(self) -> ProviderStatus: ...

    def list_models(self) -> tuple[str, ...]: ...


def normalise_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise LLMConfigError("base_url 不能为空。")
    cleaned = base_url.strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        raise LLMConfigError("base_url 必须以 http:// 或 https:// 开头。")
    return cleaned


class OpenAICompatibleProvider:
    """OpenAI-compatible `/chat/completions` over an injectable transport."""

    def __init__(self, *, name: str, base_url: str, model: str, credential: str | None = None,
                 transport: HttpTransport | None = None, temperature: float | None = None,
                 timeout: float = 30.0, extra_headers: Mapping[str, str] | None = None):
        from app.services.llm.transport import UrllibTransport
        if not isinstance(name, str) or not name.strip():
            raise LLMConfigError("provider_name 不能为空。")
        self.name = name.strip()
        self.base_url = normalise_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise LLMConfigError("model 不能为空。")
        self.model = model.strip()
        self.credential = credential
        self.temperature = temperature
        self.timeout = float(timeout)
        self.extra_headers = dict(extra_headers or {})
        self._transport = transport or UrllibTransport()

    # ------------------------------------------------------------------ helpers
    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        headers.update(self.extra_headers)
        return headers

    @staticmethod
    def _classify(response: HttpResponse):
        """Map an HTTP status to a typed, body-free error (or return None)."""
        if response.status < 400:
            return None
        if response.status in (401, 403):
            return LLMAuthError(f"模型服务拒绝了凭据（HTTP {response.status}）。"
                                "请检查 API Key 是否有效。", status=response.status)
        if response.status == 404:
            return LLMNotFoundError("模型或接口不存在（HTTP 404）。请检查 base_url 与模型名称。",
                                    status=404)
        if response.status == 429:
            return LLMRateLimitError("模型服务限流（HTTP 429）。请稍后重试。", status=429)
        if response.status >= 500:
            return LLMServerError(status=response.status)
        if "context" in _safe_text(response.body).lower() and "length" in _safe_text(response.body).lower():
            return LLMContextTooLargeError()
        return LLMResponseError(f"模型服务返回 HTTP {response.status}。", status=response.status)

    def _post(self, path: str, payload: dict, timeout: float) -> HttpResponse:
        body = json.dumps(payload).encode("utf-8")
        response = self._transport.post(self._url(path), headers=self._headers(), body=body,
                                        timeout=float(timeout))
        error = self._classify(response)
        if error is not None:
            raise error
        return response

    # ------------------------------------------------------------------- contract
    def chat(self, request: ChatRequest) -> ChatResponse:
        payload = request.payload()
        # The provider's configured temperature applies when the caller expressed no
        # preference; an explicit request value always wins.
        if request.temperature is None and self.temperature is not None:
            payload["temperature"] = float(self.temperature)
        headers = self._headers()
        headers.update(request.extra_headers or {})
        body = json.dumps(payload).encode("utf-8")
        response = self._transport.post(self._url("chat/completions"), headers=headers, body=body,
                                        timeout=float(request.timeout))
        error = self._classify(response)
        if error is not None:
            raise error
        return self._parse_chat(response)

    @staticmethod
    def _parse_chat(response: HttpResponse) -> ChatResponse:
        data = response.json()
        if not isinstance(data, dict):
            raise LLMResponseError("模型响应的顶层结构不是对象。")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("模型响应缺少 choices。")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("模型响应的 choices[0] 不是对象。")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            # Some gateways return the text under `text`; accept it only when message is absent.
            fallback = first.get("text")
            if isinstance(fallback, str) and fallback.strip():
                content = fallback
            else:
                raise LLMResponseError("模型响应缺少 content。")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        model = data.get("model") if isinstance(data.get("model"), str) else ""
        return ChatResponse(content=content, model=model, finish_reason=first.get("finish_reason"),
                            usage={key: value for key, value in usage.items() if isinstance(value, int)},
                            raw_id=data.get("id") if isinstance(data.get("id"), str) else None)

    def test_connection(self) -> ProviderStatus:
        try:
            models = self.list_models()
        except Exception as exc:                      # classified errors carry safe text
            from app.services.llm.errors import LLMError
            if isinstance(exc, LLMError):
                return ProviderStatus(False, str(exc))
            return ProviderStatus(False, "Provider 测试失败，请检查配置与网络。")
        if models:
            return ProviderStatus(True, f"连接成功，模型列表返回 {len(models)} 项。", models)
        return ProviderStatus(True, "连接成功。")

    def list_models(self) -> tuple[str, ...]:
        response = self._transport.get(self._url("models"), headers=self._headers(),
                                       timeout=self.timeout)
        if response.status == 404:
            # `/models` is optional in the compatible API: not every gateway has it,
            # and its absence is not a connection failure.
            return ()
        error = self._classify(response)
        if error is not None:
            raise error
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            # A gateway may not implement /models; that is not a connection failure.
            return ()
        names = []
        for entry in data["data"]:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                names.append(entry["id"])
        return tuple(names)


def _safe_text(body: bytes, limit: int = 400) -> str:
    try:
        return body.decode("utf-8", errors="replace")[:limit]
    except Exception:
        return ""
