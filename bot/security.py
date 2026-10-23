"""Authentication and abuse protection for the Mini App API.

Two independent proofs are required before a wallet can be linked:

1. **Telegram ``initData``** — HMAC signed by Telegram with the bot token.
   This proves *who* is using the Mini App.
2. **TON Connect ``ton_proof``** — Ed25519 signature by the wallet.
   This proves *which address* belongs to that user (see
   :mod:`bot.chain.ton_proof`).

The backend then issues a short lived, HMAC signed session token so that the
mini app does not have to resend ``initData`` on every request.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Dict, Deque, Optional, Tuple
from urllib.parse import parse_qsl

import config

from collections import deque

SESSION_TTL = 60 * 60  # 1 hour
MAX_INIT_DATA_AGE = 24 * 60 * 60


class AuthError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TelegramUser:
    user_id: int
    username: Optional[str]
    full_name: str
    language_code: Optional[str] = None
    is_premium: bool = False


# ---------------------------------------------------------------------------
# Telegram initData
# ---------------------------------------------------------------------------

def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def validate_init_data(
    init_data: str,
    bot_token: Optional[str] = None,
    *,
    max_age: int = MAX_INIT_DATA_AGE,
    now: Optional[int] = None,
) -> TelegramUser:
    """Validate Telegram WebApp ``initData`` and return the user."""
    token = bot_token or config.require_bot_token()
    if not init_data or "hash=" not in init_data:
        raise AuthError("missing_init_data", "Falta initData de Telegram.")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:  # pragma: no cover - defensive
        raise AuthError("bad_init_data", "initData mal formado.") from exc

    data = dict(pairs)
    received_hash = data.pop("hash", "")
    data.pop("signature", None)  # third-party signature, not part of the HMAC

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = _hmac(b"WebAppData", token.encode("utf-8"))
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not received_hash or not hmac.compare_digest(expected, received_hash):
        raise AuthError("bad_init_data", "La firma de initData no es válida.")

    current = int(now if now is not None else time.time())
    try:
        auth_date = int(data.get("auth_date") or 0)
    except ValueError as exc:
        raise AuthError("bad_init_data", "auth_date inválido.") from exc
    if auth_date <= 0 or current - auth_date > max_age:
        raise AuthError("expired_init_data", "La sesión de Telegram expiró; reabre la Mini App.")
    if auth_date - current > 300:
        raise AuthError("bad_init_data", "auth_date está en el futuro.")

    try:
        user = json.loads(data.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise AuthError("bad_init_data", "El campo user no es JSON.") from exc
    if not isinstance(user, dict) or not user.get("id"):
        raise AuthError("bad_init_data", "initData no incluye un usuario válido.")

    full_name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ).strip() or (user.get("username") or "Usuario")
    return TelegramUser(
        user_id=int(user["id"]),
        username=user.get("username"),
        full_name=full_name,
        language_code=user.get("language_code"),
        is_premium=bool(user.get("is_premium")),
    )


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def issue_session(user_id: int, *, ttl: int = SESSION_TTL, now: Optional[int] = None) -> str:
    settings = config.get_settings()
    issued = int(now if now is not None else time.time())
    expires = issued + max(60, ttl)
    body = f"{int(user_id)}.{expires}"
    signature = _hmac(settings.secret_key.encode("utf-8"), body.encode("utf-8"))
    return f"{body}.{_b64(signature)}"


def verify_session(token: str, *, now: Optional[int] = None) -> int:
    settings = config.get_settings()
    if not token:
        raise AuthError("missing_session", "Falta el token de sesión.")
    try:
        user_id_raw, expires_raw, signature_raw = token.split(".", 2)
        user_id = int(user_id_raw)
        expires = int(expires_raw)
        signature = _unb64(signature_raw)
    except (ValueError, binascii.Error) as exc:
        raise AuthError("bad_session", "Token de sesión mal formado.") from exc

    body = f"{user_id}.{expires}"
    expected = _hmac(settings.secret_key.encode("utf-8"), body.encode("utf-8"))
    if not hmac.compare_digest(expected, signature):
        raise AuthError("bad_session", "Token de sesión inválido.")
    if int(now if now is not None else time.time()) > expires:
        raise AuthError("expired_session", "La sesión expiró; vuelve a abrir la Mini App.")
    return user_id


def dev_session(init_data: Optional[str]) -> Optional[str]:
    """Explicit, opt-in bypass used only when the Mini App runs outside Telegram.

    Disabled by default and rejected in production by ``Settings.validate()``.
    """
    settings = config.get_settings()
    if not settings.allow_dev_session or settings.is_production:
        return None
    if init_data:
        return None
    return issue_session(0)


# ---------------------------------------------------------------------------
# Simple in-process rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding window limiter (per key) for API and chat actions."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str, *, now: Optional[float] = None) -> Tuple[bool, float]:
        current = float(now if now is not None else time.time())
        bucket = self._hits.setdefault(key, deque())
        while bucket and current - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(0.0, self.window - (current - bucket[0]))
            return False, retry_after
        bucket.append(current)
        if len(self._hits) > 10_000:  # bound memory on long running bots
            for stale in [k for k, v in self._hits.items() if not v]:
                self._hits.pop(stale, None)
        return True, 0.0

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


__all__ = [
    "AuthError", "TelegramUser", "validate_init_data", "issue_session",
    "verify_session", "dev_session", "RateLimiter", "SESSION_TTL",
]
