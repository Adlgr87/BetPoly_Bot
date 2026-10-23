"""Shared pytest fixtures.

The suite is fully offline: no Telegram, no TON API, no network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "BOT_TOKEN": "123456789:TEST-TOKEN-for-unit-tests",
        "SECRET_KEY": "test-secret-key-do-not-use-in-production",
        "ENVIRONMENT": "test",
        "BET_MODE": "hybrid",
        "ESCROW_ADDRESS": "0:" + "ab" * 32,
        "ESCROW_ADMIN_ADDRESS": "0:" + "cd" * 32,
        "PUBLIC_BASE_URL": "https://betpoly.test",
        "TON_PROOF_DOMAIN": "betpoly.test",
        "ADMIN_IDS": "1001,1002",
        "ALLOW_DEV_SESSION": "1",
        "WEB_ENABLED": "0",
        "INDEXER_ENABLED": "0",
        "STARTING_POINTS": "1000",
        "MIN_BET_POINTS": "10",
        "HOUSE_RAKE_BPS": "0",
        "TIMEZONE": "UTC",
    }
)

import config  # noqa: E402
from bot import database  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_settings():
    config.reset_settings()
    yield
    config.reset_settings()


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    from bot import api

    for limiter in (api._session_limiter, api._prepare_limiter, api._verify_limiter):
        limiter.reset()
    yield
    for limiter in (api._session_limiter, api._prepare_limiter, api._verify_limiter):
        limiter.reset()


@pytest_asyncio.fixture
async def db(tmp_path):
    """Isolated SQLite database per test."""
    database.configure_database(str(tmp_path / "betpoly-test.db"))
    await database.init_db()
    try:
        yield database
    finally:
        await database.close_db()
        database.configure_database(":memory:")


@pytest_asyncio.fixture
async def users(db):
    await db.upsert_user(1, "alice", "Alice")
    await db.upsert_user(2, "bob", "Bob")
    await db.upsert_user(3, "carol", "Carol")
    return db


@pytest.fixture
def admin_id() -> int:
    return 1001


@pytest.fixture
def normal_id() -> int:
    return 1
