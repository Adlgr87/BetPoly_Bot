"""Configuration loader for BetPoly_Bot."""
from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Required environment variable '{key}' is not set.")
    return value


BOT_TOKEN: str = _require("BOT_TOKEN")

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = (
    [int(uid.strip()) for uid in _raw_admins.split(",") if uid.strip()]
    if _raw_admins
    else []
)

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "betpoly.db")
REMINDER_HOURS_BEFORE: int = int(os.getenv("REMINDER_HOURS_BEFORE", "24"))

# Points awarded / deducted on win or loss
POINTS_ON_WIN_MULTIPLIER: float = 2.0
STARTING_POINTS: int = 1000
