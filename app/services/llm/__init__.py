"""LLM layer: provider contract, configuration, credentials and gateway."""
from app.services.llm.config import LLMConfig, LLMConfigStore, LLMProviderConfig
from app.services.llm.credentials import CredentialResolver, InMemoryCredentialStore
from app.services.llm.errors import (LLMAuthError, LLMCancelledError, LLMConfigError,
                                     LLMConnectionError, LLMContextTooLargeError, LLMCredentialError,
                                     LLMError, LLMNotFoundError, LLMRateLimitError, LLMResponseError,
                                     LLMServerError, LLMTimeoutError)
from app.services.llm.gateway import CancelToken, LLMGateway
from app.services.llm.provider import (ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_USER, ChatMessage,
                                       ChatRequest, ChatResponse, LLMProvider,
                                       OpenAICompatibleProvider, ProviderStatus)
from app.services.llm.transport import FakeTransport, HttpResponse, UrllibTransport

__all__ = [
    "CancelToken", "ChatMessage", "ChatRequest", "ChatResponse", "CredentialResolver",
    "FakeTransport", "HttpResponse", "InMemoryCredentialStore", "LLMAuthError", "LLMCancelledError",
    "LLMConfig", "LLMConfigError", "LLMConfigStore", "LLMConnectionError", "LLMContextTooLargeError",
    "LLMCredentialError", "LLMError", "LLMGateway", "LLMNotFoundError", "LLMProvider",
    "LLMProviderConfig", "LLMRateLimitError", "LLMResponseError", "LLMServerError", "LLMTimeoutError",
    "OpenAICompatibleProvider", "ProviderStatus", "UrllibTransport",
    "ROLE_ASSISTANT", "ROLE_SYSTEM", "ROLE_USER",
]
