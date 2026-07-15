"""Async SQLite persistence layer for BetPoly_Bot."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from config import DATABASE_PATH, STARTING_POINTS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    points      INTEGER NOT NULL DEFAULT {starting_points},
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    closes_at   TEXT    NOT NULL,
    resolved_at TEXT,
    winner_opt  TEXT,
    status      TEXT    NOT NULL DEFAULT 'open',  -- open | closed | resolved
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (creator_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS options (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id  INTEGER NOT NULL,
    label          TEXT    NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS bets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    prediction_id  INTEGER NOT NULL,
    option_id      INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    result         TEXT,  -- win | lose | pending
    placed_at      TEXT   NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id)        REFERENCES users(user_id),
    FOREIGN KEY (prediction_id)  REFERENCES predictions(id),
    FOREIGN KEY (option_id)      REFERENCES options(id),
    UNIQUE (user_id, prediction_id)
);
""".format(starting_points=STARTING_POINTS)


async def init_db() -> None:
    """Create tables if they do not exist."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
    logger.info("Database initialised at %s", DATABASE_PATH)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: Optional[str], full_name: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, username, full_name),
        )
        await db.commit()


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_leaderboard(limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, username, full_name, points FROM users ORDER BY points DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def adjust_points(user_id: int, delta: int) -> int:
    """Add *delta* points (can be negative) to a user. Returns new balance."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?",
            (delta, user_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Prediction helpers
# ---------------------------------------------------------------------------

async def create_prediction(
    creator_id: int,
    chat_id: int,
    title: str,
    options: List[str],
    closes_at: datetime,
) -> int:
    """Insert a new prediction and its options. Returns prediction id."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO predictions (creator_id, chat_id, title, closes_at) VALUES (?, ?, ?, ?)",
            (creator_id, chat_id, title, closes_at.isoformat()),
        )
        pred_id = cursor.lastrowid
        await db.executemany(
            "INSERT INTO options (prediction_id, label) VALUES (?, ?)",
            [(pred_id, opt) for opt in options],
        )
        await db.commit()
    return pred_id


async def get_prediction(pred_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM predictions WHERE id = ?", (pred_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_open_predictions(chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if chat_id is not None:
            async with db.execute(
                "SELECT * FROM predictions WHERE status = 'open' AND chat_id = ? ORDER BY closes_at",
                (chat_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM predictions WHERE status = 'open' ORDER BY closes_at"
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_predictions_to_resolve() -> List[Dict[str, Any]]:
    """Return closed (not yet resolved) predictions for the admin panel."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM predictions WHERE status IN ('open', 'closed') ORDER BY closes_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def close_expired_predictions() -> List[int]:
    """Mark predictions past their closes_at as 'closed'. Returns affected ids."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT id FROM predictions WHERE status = 'open' AND closes_at <= ?",
            (now,),
        ) as cursor:
            ids = [row[0] for row in await cursor.fetchall()]
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            await db.execute(
                f"UPDATE predictions SET status = 'closed' WHERE id IN ({placeholders})",
                ids,
            )
            await db.commit()
    return ids


async def resolve_prediction(pred_id: int, winning_option_id: int) -> str:
    """Resolve a prediction: mark winner, update bets, adjust points. Returns winning label."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Get winning label
        async with db.execute(
            "SELECT label FROM options WHERE id = ?", (winning_option_id,)
        ) as cursor:
            opt_row = await cursor.fetchone()
        if not opt_row:
            raise ValueError("Option not found")
        winning_label = opt_row["label"]

        # Mark prediction resolved
        await db.execute(
            "UPDATE predictions SET status = 'resolved', winner_opt = ?, resolved_at = datetime('now') WHERE id = ?",
            (winning_label, pred_id),
        )

        # Fetch all bets for this prediction
        async with db.execute(
            "SELECT b.id, b.user_id, b.amount, b.option_id FROM bets b WHERE b.prediction_id = ?",
            (pred_id,),
        ) as cursor:
            bets = [dict(r) for r in await cursor.fetchall()]

        for bet in bets:
            if bet["option_id"] == winning_option_id:
                result = "win"
                await db.execute(
                    "UPDATE users SET points = points + ? WHERE user_id = ?",
                    (bet["amount"] * 2, bet["user_id"]),
                )
            else:
                result = "lose"
            await db.execute(
                "UPDATE bets SET result = ? WHERE id = ?", (result, bet["id"])
            )

        await db.commit()
    return winning_label


# ---------------------------------------------------------------------------
# Option helpers
# ---------------------------------------------------------------------------

async def get_options(pred_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM options WHERE prediction_id = ? ORDER BY id",
            (pred_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Bet helpers
# ---------------------------------------------------------------------------

async def place_bet(
    user_id: int, pred_id: int, option_id: int, amount: int
) -> None:
    """Deduct points and record the bet. Raises ValueError on duplicate or insufficient funds."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Check points
        async with db.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or row["points"] < amount:
            raise ValueError("Saldo insuficiente.")

        # Check prediction still open
        async with db.execute(
            "SELECT status FROM predictions WHERE id = ?", (pred_id,)
        ) as cursor:
            prow = await cursor.fetchone()
        if not prow or prow["status"] != "open":
            raise ValueError("Esta predicción ya no está abierta.")

        # Insert bet (UNIQUE constraint prevents double betting)
        try:
            await db.execute(
                "INSERT INTO bets (user_id, prediction_id, option_id, amount) VALUES (?, ?, ?, ?)",
                (user_id, pred_id, option_id, amount),
            )
        except aiosqlite.IntegrityError:
            raise ValueError("Ya has apostado en esta predicción.")

        # Deduct points
        await db.execute(
            "UPDATE users SET points = points - ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()


async def get_user_bets(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT b.id, b.amount, b.result, b.placed_at,
                   p.title AS pred_title, p.status AS pred_status,
                   o.label AS option_label
            FROM bets b
            JOIN predictions p ON b.prediction_id = p.id
            JOIN options o     ON b.option_id      = o.id
            WHERE b.user_id = ?
            ORDER BY b.placed_at DESC
            """,
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_bettors_for_prediction(pred_id: int) -> List[Dict[str, Any]]:
    """Return user_id and option_id for all bets on a prediction."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, option_id, amount FROM bets WHERE prediction_id = ?",
            (pred_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
