"""LLM provider configuration: non-sensitive fields in a TOML file.

The main application config keeps rejecting unknown tables, so LLM settings live in
their own file inside the data directory (``llm-providers.toml``). Only references
are stored: `credential_ref` names where the secret lives, never the secret.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path
import tomllib

from app.services.llm.errors import LLMConfigError
from app.services.llm.provider import normalise_base_url

PROVIDER_FIELDS = {"provider_name", "base_url", "model", "credential_ref", "temperature",
                   "timeout", "extra_headers"}
FILE_NAME = "llm-providers.toml"


@dataclass(frozen=True)
class LLMProviderConfig:
    provider_name: str
    base_url: str
    model: str
    credential_ref: str | None = None
    temperature: float | None = None
    timeout: float = 30.0
    extra_headers: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.provider_name, str) or not self.provider_name.strip():
            raise LLMConfigError("provider_name 不能为空。")
        object.__setattr__(self, "provider_name", self.provider_name.strip())
        object.__setattr__(self, "base_url", normalise_base_url(self.base_url))
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMConfigError("model 不能为空。")
        object.__setattr__(self, "model", self.model.strip())
        reference = self.credential_ref
        if reference is not None:
            if not isinstance(reference, str):
                raise LLMConfigError("credential_ref 必须是字符串。")
            reference = reference.strip() or None
        object.__setattr__(self, "credential_ref", reference)
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)) \
                    or not 0 <= float(self.temperature) <= 2:
                raise LLMConfigError("temperature 必须在 0 到 2 之间。")
            object.__setattr__(self, "temperature", float(self.temperature))
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) \
                or not 0 < float(self.timeout) <= 600:
            raise LLMConfigError("timeout 必须是 0 到 600 秒之间的数值。")
        object.__setattr__(self, "timeout", float(self.timeout))
        headers = self.extra_headers or {}
        if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                for key, value in headers.items()):
            raise LLMConfigError("extra_headers 必须是字符串到字符串的表。")
        lowered = {key.lower() for key in headers}
        if "authorization" in lowered:
            raise LLMConfigError("不要把 Authorization 写进 extra_headers；请使用 credential_ref。")
        object.__setattr__(self, "extra_headers", dict(headers))

    @classmethod
    def from_mapping(cls, data) -> "LLMProviderConfig":
        if not isinstance(data, dict):
            raise LLMConfigError("每个 Provider 必须是一个 TOML 表。")
        unknown = set(data) - PROVIDER_FIELDS
        if unknown:
            raise LLMConfigError(f"Provider 配置包含不支持的字段：{', '.join(sorted(unknown))}")
        if "provider_name" not in data or "base_url" not in data or "model" not in data:
            raise LLMConfigError("Provider 必须包含 provider_name、base_url 和 model。")
        return cls(provider_name=data["provider_name"], base_url=data["base_url"], model=data["model"],
                   credential_ref=data.get("credential_ref"),
                   temperature=data.get("temperature"), timeout=data.get("timeout", 30.0),
                   extra_headers=data.get("extra_headers", {}))

    def to_mapping(self) -> dict:
        data = {"provider_name": self.provider_name, "base_url": self.base_url, "model": self.model,
                "timeout": self.timeout}
        if self.credential_ref:
            data["credential_ref"] = self.credential_ref
        if self.temperature is not None:
            data["temperature"] = self.temperature
        if self.extra_headers:
            data["extra_headers"] = dict(self.extra_headers)
        return data


@dataclass(frozen=True)
class LLMConfig:
    default_provider: str | None = None
    providers: tuple[LLMProviderConfig, ...] = ()

    def provider(self, name: str | None = None) -> LLMProviderConfig:
        if not self.providers:
            raise LLMConfigError("尚未配置任何 LLM Provider，请先在 Settings 中添加。")
        wanted = (name or self.default_provider or self.providers[0].provider_name).strip()
        for entry in self.providers:
            if entry.provider_name == wanted:
                return entry
        raise LLMConfigError(f"找不到名为 {wanted} 的 Provider。")

    def names(self) -> tuple[str, ...]:
        return tuple(entry.provider_name for entry in self.providers)

    def with_provider(self, entry: LLMProviderConfig) -> "LLMConfig":
        others = tuple(item for item in self.providers if item.provider_name != entry.provider_name)
        default = self.default_provider or entry.provider_name
        return LLMConfig(default_provider=default, providers=others + (entry,))

    def without_provider(self, name: str) -> "LLMConfig":
        remaining = tuple(item for item in self.providers if item.provider_name != name)
        default = self.default_provider if self.default_provider != name else (
            remaining[0].provider_name if remaining else None)
        return LLMConfig(default_provider=default, providers=remaining)

    def with_default(self, name: str) -> "LLMConfig":
        if name not in self.names():
            raise LLMConfigError(f"找不到名为 {name} 的 Provider。")
        return replace(self, default_provider=name)


class LLMConfigStore:
    """Reads and writes `llm-providers.toml` next to the application database."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "LLMConfigStore":
        return cls(Path(data_dir) / FILE_NAME)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> LLMConfig:
        if not self.path.exists():
            return LLMConfig()
        try:
            with self.path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise LLMConfigError(f"无法读取 LLM 配置：{self.path.name}") from exc
        unknown_tables = set(data) - {"llm"}
        if unknown_tables:
            raise LLMConfigError(f"LLM 配置包含不支持的字段：{', '.join(sorted(unknown_tables))}")
        table = data.get("llm", {})
        if not isinstance(table, dict):
            raise LLMConfigError("[llm] 必须是一个 TOML 表。")
        unknown = set(table) - {"default_provider", "providers"}
        if unknown:
            raise LLMConfigError(f"LLM 配置包含不支持的字段：{', '.join(sorted(unknown))}")
        raw = table.get("providers", [])
        if not isinstance(raw, list):
            raise LLMConfigError("providers 必须是 Provider 表的数组（[[llm.providers]]）。")
        entries = tuple(LLMProviderConfig.from_mapping(item) for item in raw)
        names = [entry.provider_name for entry in entries]
        if len(set(names)) != len(names):
            raise LLMConfigError("Provider 名称不能重复。")
        default = table.get("default_provider")
        if default is not None and default not in names:
            raise LLMConfigError("default_provider 必须是已配置的 Provider。")
        return LLMConfig(default_provider=default, providers=entries)

    def save(self, config: LLMConfig) -> LLMConfig:
        parts = ["# LLM Providers（只有非敏感字段；API Key 由 credential_ref 指向系统凭据或环境变量）。"]
        header = ["[llm]"]
        if config.default_provider:
            header.append(f"default_provider = {_toml_value(config.default_provider)}")
        parts.append("\n".join(header))
        for entry in config.providers:
            block = ["[[llm.providers]]"]
            block.extend(f"{key} = {_toml_value(value)}" for key, value in entry.to_mapping().items())
            parts.append("\n".join(block))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        return config


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_value(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + inner + " }"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
