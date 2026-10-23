"""Telegram initData validation, session tokens, nonces and rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from bot import security

BOT_TOKEN = "123456789:TEST-TOKEN-for-unit-tests"


def signed_init_data(user: dict, auth_date: int | None = None, token: str = BOT_TOKEN) -> str:
    data = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    data["hash"] = digest
    return urlencode(data)


class TestInitData:
    def test_valid_payload(self):
        raw = signed_init_data({"id": 42, "first_name": "Ada", "last_name": "Lovelace", "username": "ada"})
        user = security.validate_init_data(raw)
        assert user.user_id == 42
        assert user.full_name == "Ada Lovelace"
        assert user.username == "ada"

    def test_tampered_user_is_rejected(self):
        raw = signed_init_data({"id": 42, "first_name": "Ada"})
        tampered = raw.replace("Ada", "Eve")
        with pytest.raises(security.AuthError) as exc:
            security.validate_init_data(tampered)
        assert exc.value.code == "bad_init_data"

    def test_wrong_bot_token_is_rejected(self):
        raw = signed_init_data({"id": 42, "first_name": "Ada"}, token="999:other")
        with pytest.raises(security.AuthError):
            security.validate_init_data(raw)

    def test_expired_auth_date(self):
        raw = signed_init_data({"id": 42, "first_name": "Ada"}, auth_date=time.time() - 90_000)
        with pytest.raises(security.AuthError) as exc:
            security.validate_init_data(raw)
        assert exc.value.code == "expired_init_data"

    def test_missing_hash(self):
        with pytest.raises(security.AuthError):
            security.validate_init_data("user=%7B%7D")

    def test_missing_user(self):
        data = {"auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(security.AuthError):
            security.validate_init_data(urlencode(data))


class TestSessions:
    def test_roundtrip(self):
        token = security.issue_session(77)
        assert security.verify_session(token) == 77

    def test_tampered_token(self):
        token = security.issue_session(77)
        head, expires, signature = token.split(".")
        with pytest.raises(security.AuthError):
            security.verify_session(f"{head}.{expires}.{signature[:-2]}AA")

    def test_cannot_escalate_user_id(self):
        token = security.issue_session(77)
        _, expires, signature = token.split(".")
        with pytest.raises(security.AuthError):
            security.verify_session(f"1.{expires}.{signature}")

    def test_expired(self):
        token = security.issue_session(77, ttl=60, now=int(time.time()) - 120)
        with pytest.raises(security.AuthError) as exc:
            security.verify_session(token)
        assert exc.value.code == "expired_session"

    def test_malformed(self):
        with pytest.raises(security.AuthError):
            security.verify_session("garbage")

    def test_dev_session_requires_opt_in(self, monkeypatch):
        assert security.dev_session(None) is not None  # ALLOW_DEV_SESSION=1 in tests
        monkeypatch.setenv("ALLOW_DEV_SESSION", "0")
        import config

        config.reset_settings()
        assert security.dev_session(None) is None


class TestNonces:
    async def test_single_use(self, db):
        nonce = await db.create_nonce(5, "ton_proof")
        assert await db.consume_nonce(nonce, 5, "ton_proof") is True
        assert await db.consume_nonce(nonce, 5, "ton_proof") is False

    async def test_bound_to_user_and_purpose(self, db):
        nonce = await db.create_nonce(5, "ton_proof")
        assert await db.consume_nonce(nonce, 6, "ton_proof") is False
        assert await db.consume_nonce(nonce, 5, "other") is False
        assert await db.consume_nonce(nonce, 5, "ton_proof") is True

    async def test_unknown_nonce(self, db):
        assert await db.consume_nonce("deadbeef", 5, "ton_proof") is False

    async def test_expired_nonce(self, db):
        nonce = await db.create_nonce(5, "ton_proof")
        assert await db.consume_nonce(nonce, 5, "ton_proof", ttl_seconds=0) is False


class TestRateLimiter:
    def test_allows_under_limit(self):
        limiter = security.RateLimiter(limit=3, window_seconds=60)
        assert all(limiter.allow("u")[0] for _ in range(3))
        allowed, retry = limiter.allow("u")
        assert not allowed and retry > 0

    def test_window_slides(self):
        limiter = security.RateLimiter(limit=2, window_seconds=10)
        assert limiter.allow("u", now=1000)[0]
        assert limiter.allow("u", now=1005)[0]
        assert not limiter.allow("u", now=1009)[0]
        assert limiter.allow("u", now=1011)[0]

    def test_keys_are_independent(self):
        limiter = security.RateLimiter(limit=1, window_seconds=60)
        assert limiter.allow("a")[0]
        assert limiter.allow("b")[0]
        assert not limiter.allow("a")[0]
