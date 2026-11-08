"""In-memory, short-lived secret references.

Plaintext passwords never enter task records, manifests, logs, URLs or
localStorage.  The web UI posts a password once to `secrets.create`, receives a
short-lived `secretRef`, and passes that reference to an encrypt/decrypt job.
The value is kept only in this process memory and is erased on expiry/revoke or
when the server exits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
import threading

MAX_TTL_SECONDS = 3600
DEFAULT_TTL_SECONDS = 600
MAX_SECRET_BYTES = 4096


class SecretError(ValueError):
    pass


class SecretStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        if not 30 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise SecretError("secretRef 有效期必须在 30 到 3600 秒之间。")
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._values: dict[str, tuple[bytes, datetime, str]] = {}

    def create(self, value: str | bytes, purpose: str = "encryption") -> dict:
        if purpose not in {"encryption", "remote_auth"}:
            raise SecretError("不支持的 secretRef 用途。")
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if not raw or len(raw) > MAX_SECRET_BYTES:
            raise SecretError("密码长度不合法。")
        reference = "secret-" + secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl)
        with self._lock:
            self._purge_locked()
            self._values[reference] = (raw, expires, purpose)
        return {"secretRef": reference, "expiresAt": expires.isoformat()}

    def get(self, reference: str, purpose: str = "encryption") -> bytes:
        with self._lock:
            self._purge_locked()
            found = self._values.get(reference)
        if not found:
            raise SecretError("secretRef 不存在、已过期或已被销毁，请重新输入密码。")
        value, _expires, stored_purpose = found
        if purpose and stored_purpose != purpose:
            raise SecretError("secretRef 的用途不匹配。")
        return value

    def revoke(self, reference: str) -> bool:
        with self._lock:
            return self._values.pop(reference, None) is not None

    def purge(self) -> None:
        with self._lock:
            self._purge_locked()

    def _purge_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for key in [key for key, (_value, expires, _purpose) in self._values.items() if expires <= now]:
            self._values.pop(key, None)
