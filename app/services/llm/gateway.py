"""LLM gateway: configuration, credentials, retry policy and cancellation.

The gateway is the only object the analysis service talks to. It owns:

* building a provider from configuration and resolving its credential;
* the (deliberately small) retry policy: 429/5xx/timeout/connection only, a bounded
  number of attempts, never a 401/403/4xx;
* cooperative cancellation: the token is checked before every attempt and after the
  response, so a cancelled run stops sending requests.
"""
import time
from typing import Protocol

from app.services.llm.config import LLMConfig, LLMConfigStore, LLMProviderConfig
from app.services.llm.credentials import CredentialResolver
from app.services.llm.errors import (LLMCancelledError, LLMConnectionError, LLMError,
                                     LLMRateLimitError, LLMServerError, LLMTimeoutError)
from app.services.llm.provider import ChatRequest, ChatResponse, OpenAICompatibleProvider, ProviderStatus
from app.services.llm.transport import HttpTransport

RETRYABLE = (LLMRateLimitError, LLMServerError, LLMTimeoutError, LLMConnectionError)
DEFAULT_ATTEMPTS = 2
BACKOFF_SECONDS = 0.5


class Cancellable(Protocol):
    @property
    def cancelled(self) -> bool: ...


class CancelToken:
    """Cooperative cancellation; the request in flight finishes on its own timeout."""

    def __init__(self):
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise LLMCancelledError("分析已取消；未修改任何事实数据。")


class LLMGateway:
    def __init__(self, store: LLMConfigStore, *, resolver: CredentialResolver | None = None,
                 transport: HttpTransport | None = None, sleeper=time.sleep,
                 attempts: int = DEFAULT_ATTEMPTS):
        self._store = store
        self._resolver = resolver or CredentialResolver()
        self._transport = transport
        self._sleeper = sleeper
        self.attempts = max(1, int(attempts))
        self._config_cache: LLMConfig | None = None

    # ------------------------------------------------------------------ config
    def config(self) -> LLMConfig:
        if self._config_cache is None:
            self._config_cache = self._store.load()
        return self._config_cache

    def reload(self) -> LLMConfig:
        self._config_cache = None
        return self.config()

    def save_provider(self, entry: LLMProviderConfig) -> LLMConfig:
        config = self.config().with_provider(entry)
        self._store.save(config)
        return self.reload()

    def set_default(self, name: str) -> LLMConfig:
        config = self.config().with_default(name)
        self._store.save(config)
        return self.reload()

    def remove_provider(self, name: str) -> LLMConfig:
        config = self.config().without_provider(name)
        self._store.save(config)
        return self.reload()

    def credential_reference(self, name: str | None = None) -> str:
        """Loggable credential description: the reference, never the secret."""
        return CredentialResolver.describe(self.config().provider(name).credential_ref)

    # ---------------------------------------------------------------- provider
    def provider(self, name: str | None = None) -> OpenAICompatibleProvider:
        entry = self.config().provider(name)
        secret = self._resolver.resolve(entry.credential_ref)
        return OpenAICompatibleProvider(name=entry.provider_name, base_url=entry.base_url,
                                        model=entry.model, credential=secret,
                                        transport=self._transport, temperature=entry.temperature,
                                        timeout=entry.timeout, extra_headers=entry.extra_headers)

    def chat(self, request: ChatRequest, *, provider_name: str | None = None,
             cancel: Cancellable | None = None) -> ChatResponse:
        provider = self.provider(provider_name)
        last: LLMError | None = None
        for attempt in range(1, self.attempts + 1):
            if cancel is not None:
                cancel.raise_if_cancelled()
            try:
                response = provider.chat(request)
            except LLMError as exc:
                last = exc
                if not isinstance(exc, RETRYABLE) or attempt >= self.attempts:
                    raise
                self._sleeper(BACKOFF_SECONDS * attempt)
                continue
            if cancel is not None:
                cancel.raise_if_cancelled()
            return response
        raise last or LLMError("模型调用失败。")

    def test_provider(self, name: str | None = None) -> ProviderStatus:
        try:
            return self.provider(name).test_connection()
        except LLMError as exc:
            return ProviderStatus(False, str(exc))

    def list_models(self, name: str | None = None) -> tuple[str, ...]:
        return self.provider(name).list_models()
