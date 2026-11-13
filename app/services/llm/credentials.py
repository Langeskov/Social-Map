"""Credential resolution: the database stores a reference, never a secret.

Supported `credential_ref` schemes:

* ``env:NAME``      environment variable (portable, CI/desktop friendly)
* ``os:NAME``       operating-system secure store (Windows Credential Manager, or
                    the optional ``keyring`` package where available)
* empty/``none``    no credential: local endpoints frequently need none

Logging never receives the value: `describe()` returns the reference only.
"""
import os
from typing import Protocol

from app.services.llm.errors import LLMCredentialError

ENV_PREFIX = "env:"
OS_PREFIX = "os:"
NO_CREDENTIAL = {"", "none", "null", "no-auth"}


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, secret: str) -> None: ...

    def delete(self, name: str) -> None: ...


class EnvironmentCredentialStore:
    """Read secrets from the process environment."""

    def get(self, name):
        value = os.environ.get(name)
        return value or None

    def set(self, name, secret):
        raise LLMCredentialError("环境变量凭据由外部管理，程序不会写入环境变量。")

    def delete(self, name):
        raise LLMCredentialError("环境变量凭据由外部管理，程序不会删除环境变量。")


class InMemoryCredentialStore:
    """Test/DI stand-in for the operating-system store."""

    def __init__(self, secrets=None):
        self._secrets = dict(secrets or {})

    def get(self, name):
        return self._secrets.get(name)

    def set(self, name, secret):
        self._secrets[name] = secret

    def delete(self, name):
        self._secrets.pop(name, None)


class WindowsCredentialStore:
    """OS secure store on Windows, implemented with ctypes only.

    Nothing is written unless the caller explicitly calls ``set``; the analysis
    service never does, so a run can only *read* an existing credential.
    """

    def __init__(self, target_prefix: str = "RelationshipIntelligence/"):
        self.target_prefix = target_prefix

    def _advapi(self):
        import ctypes
        from ctypes import wintypes
        if os.name != "nt":
            raise LLMCredentialError("当前平台没有可用的操作系统凭据存储。")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(ctypes.c_void_p)]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        return ctypes, wintypes, advapi

    def get(self, name):
        try:
            ctypes, wintypes, advapi = self._advapi()
        except LLMCredentialError:
            raise
        except Exception as exc:
            raise LLMCredentialError("无法加载操作系统凭据接口。") from exc

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        pointer = ctypes.c_void_p()
        target = self.target_prefix + name
        if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            return None
        try:
            credential = ctypes.cast(pointer, ctypes.POINTER(CREDENTIAL)).contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return blob.decode("utf-16-le", errors="replace") or None
        finally:
            advapi.CredFree(pointer)

    def set(self, name, secret):
        raise LLMCredentialError(
            "程序不会自动写入操作系统凭据存储；请用系统工具或 keyring 预先创建该凭据。")

    def delete(self, name):
        raise LLMCredentialError("程序不会删除操作系统凭据。")


class CredentialResolver:
    """Turn a `credential_ref` into a secret (or None for unauthenticated endpoints)."""

    def __init__(self, *, environment=None, os_store=None):
        self._environment = environment or EnvironmentCredentialStore()
        self._os_store = os_store

    @property
    def os_store(self):
        if self._os_store is None:
            self._os_store = WindowsCredentialStore()
        return self._os_store

    @staticmethod
    def describe(credential_ref: str | None) -> str:
        """Safe, loggable description: never the secret."""
        if not credential_ref or credential_ref.strip().lower() in NO_CREDENTIAL:
            return "（无凭据）"
        return credential_ref.strip()

    def resolve(self, credential_ref: str | None) -> str | None:
        reference = (credential_ref or "").strip()
        if reference.lower() in NO_CREDENTIAL:
            return None
        if reference.startswith(ENV_PREFIX):
            name = reference[len(ENV_PREFIX):].strip()
            if not name:
                raise LLMCredentialError("credential_ref 的 env: 后必须跟环境变量名。")
            value = self._environment.get(name)
            if not value:
                raise LLMCredentialError(f"环境变量 {name} 未设置或为空。")
            return value
        if reference.startswith(OS_PREFIX):
            name = reference[len(OS_PREFIX):].strip()
            if not name:
                raise LLMCredentialError("credential_ref 的 os: 后必须跟凭据名称。")
            value = self.os_store.get(name)
            if not value:
                raise LLMCredentialError(f"操作系统凭据存储中没有名为 {name} 的凭据。")
            return value
        raise LLMCredentialError(
            "credential_ref 必须使用 env:NAME 或 os:NAME（或留空表示无凭据）。")
