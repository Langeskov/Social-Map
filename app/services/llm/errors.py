"""Typed LLM failures.

Every error carries a *safe* message: it never contains the API key, an
Authorization header, a full HTTP body or private evidence text. The analysis
service stores exactly this message on the run, and the UI shows it verbatim.
"""
from app.domain.errors import DomainError


class LLMError(DomainError):
    """Base class for every provider-side failure."""

    #: Safe, user-facing text; subclasses refine it.
    default_message = "模型调用失败。"

    def __init__(self, message: str | None = None, *, status: int | None = None):
        self.status = status
        super().__init__(message or self.default_message)


class LLMConfigError(LLMError):
    default_message = "LLM 配置无效或不完整。"


class LLMCredentialError(LLMError):
    default_message = "无法获取该 Provider 的 API Key，请检查 credential_ref 或系统凭据存储。"


class LLMAuthError(LLMError):
    default_message = "模型服务拒绝了凭据（401/403）。请检查 API Key 是否有效。"


class LLMNotFoundError(LLMError):
    default_message = "模型或接口不存在（404）。请检查 base_url 与模型名称。"


class LLMRateLimitError(LLMError):
    default_message = "模型服务限流（429）。请稍后重试。"


class LLMServerError(LLMError):
    default_message = "模型服务返回服务器错误（5xx）。"

    def __init__(self, message: str | None = None, *, status: int | None = None):
        super().__init__(message or f"模型服务返回服务器错误（{status}）。", status=status)


class LLMTimeoutError(LLMError):
    default_message = "模型请求超时。"


class LLMConnectionError(LLMError):
    default_message = "无法连接模型服务，请检查 base_url 与网络。"


class LLMResponseError(LLMError):
    default_message = "模型返回了无法解析的响应。"


class LLMContextTooLargeError(LLMError):
    default_message = "Selected context exceeds model context limit."


class LLMCancelledError(LLMError):
    default_message = "分析已取消。"
