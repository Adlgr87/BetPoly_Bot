"""Async SQLite persistence layer for BetPoly_Bot.

Two clearly separated sources of truth live in this file:

1. **Off-chain sandbox economy** (``users.points``, ``predictions``, ``bets``)
   — virtual points, no real money, no custody.
2. **On-chain mirror** (``markets``, ``onchain_bets``, ``chain_*``) — a
   read-only projection of the escrow contract.  Balances are *never* derived
   from this database: the blockchain is the only judge.

Concurrency: a single shared connection guarded by an asyncio lock, with
``BEGIN IMMEDIATE`` for read-modify-write sections, keeps the check-then-act
races that existed in the previous implementation from producing negative
balances or double payouts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Tuple

import aiosqlite

import config
from bot import economics
from bot.formatting import iso_now, iso_utc

logger = logging.getLogger(__name__)

#: Tipos de aviso. El texto vive en :mod:`bot.notifications`; la persistencia,
#: aquí. Coinciden a propósito (una sola lista de tipos).
NOTIFICATION_PREDICTION_RESULT = "prediction_result"
NOTIFICATION_MARKET_CLAIM = "market_claim"
NOTIFICATION_MARKET_REFUND = "market_refund"

__all__ = [
    "DatabaseError", "DuplicateBet", "InsufficientFunds", "WalletConflict",
    "SettlementResult", "BetSettlement",
    "configure_database", "close_db", "init_db", "connection", "transaction",
    "upsert_user", "get_user", "get_leaderboard", "adjust_points",
    "create_prediction", "get_prediction", "get_open_predictions",
    "get_predictions_to_resolve", "close_expired_predictions",
    "settle_prediction", "resolve_prediction", "get_options",
    "place_bet", "get_user_bets", "get_bettors_for_prediction",
    "get_prediction_pools", "get_user_bet", "get_user_stats", "ranking_position",
    "claim_reminders", "get_reminder_targets",
    "link_wallet", "get_wallet", "get_wallet_owner", "unlink_wallet",
    "create_nonce", "consume_nonce", "purge_nonces",
    "insert_market_placeholder", "enrich_market_from_event",
    "update_market_settled", "update_market_cancelled",
    "get_market", "list_markets", "set_market_options",
    "record_onchain_bet", "mark_onchain_bet_claimed", "list_onchain_bets",
    "get_onchain_bet", "get_cursor", "set_cursor", "mark_tx_processed",
    "unmark_tx_processed", "market_pool",
    "record_indexer_run", "get_indexer_status",
    "create_pending_action", "find_pending_action", "consume_pending_action",
    "purge_pending_actions",
]


class DatabaseError(RuntimeError):
    """Base class for domain errors raised by the persistence layer."""


class InsufficientFunds(DatabaseError):
    pass


class DuplicateBet(DatabaseError):
    pass


class WalletConflict(DatabaseError):
    pass


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
_conn: Optional[aiosqlite.Connection] = None
_conn_path: Optional[str] = None
_lock = asyncio.Lock()

_override_path: Optional[str] = None


def configure_database(path: str) -> None:
    """Point the layer at another database file (tests, migrations)."""
    global _override_path, _conn, _conn_path
    _override_path = path
    _conn = None
    _conn_path = None


def database_path() -> str:
    if _override_path:
        return _override_path
    return config.get_settings().database_path


async def _get_conn() -> aiosqlite.Connection:
    global _conn, _conn_path
    path = database_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            await _conn.close()
        finally:
            _conn = None
            _conn_path = None
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    if path != ":memory:":
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
    _conn, _conn_path = conn, path
    return conn


async def close_db() -> None:
    global _conn, _conn_path
    async with _lock:
        if _conn is not None:
            await _conn.close()
        _conn = None
        _conn_path = None


@asynccontextmanager
async def connection() -> AsyncIterator[aiosqlite.Connection]:
    """Yield the shared connection while holding the write lock."""
    async with _lock:
        yield await _get_conn()


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """Serialized ``BEGIN IMMEDIATE`` transaction with rollback on error."""
    async with _lock:
        conn = await _get_conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# ``wallet_links`` needs a surrogate key: a Telegram account can unlink and
# later re-link, and the address must become available to other accounts once it
# is inactive.  A global UNIQUE(address) made that impossible (S-07), so the
# table is rebuilt with partial unique indexes instead.
_WALLET_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS wallet_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    address     TEXT NOT NULL,
    network     TEXT NOT NULL DEFAULT 'ton',
    public_key  TEXT,
    verified_at TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_active_addr
    ON wallet_links(address) WHERE is_active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_active_user
    ON wallet_links(user_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_wallet_history ON wallet_links(user_id, verified_at DESC);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    points      INTEGER NOT NULL DEFAULT 1000,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    closes_at   TEXT NOT NULL,
    resolved_at TEXT,
    winner_opt  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    mode        TEXT NOT NULL DEFAULT 'points',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS options (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    label         TEXT NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    prediction_id INTEGER NOT NULL,
    option_id     INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    payout        INTEGER NOT NULL DEFAULT 0,
    result        TEXT,
    placed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(id) ON DELETE CASCADE,
    FOREIGN KEY (option_id) REFERENCES options(id),
    UNIQUE (user_id, prediction_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    prediction_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (prediction_id, user_id, kind)
);

-- ---------------------------------------------------------------- on-chain
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    ref_id     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at    TEXT,
    last_error TEXT,
    UNIQUE (user_id, kind, ref_id)
);

CREATE TABLE IF NOT EXISTS auth_nonces (
    nonce      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'ton_proof',
    created_at INTEGER NOT NULL,
    used_at    INTEGER
);

CREATE TABLE IF NOT EXISTS markets (
    market_id      INTEGER PRIMARY KEY,
    creator_address TEXT,
    title          TEXT NOT NULL,
    title_hash     TEXT,
    option_count   INTEGER NOT NULL,
    closes_at      INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    pending_resolve_at INTEGER,
    winning_option INTEGER,
    total_pool     INTEGER NOT NULL DEFAULT 0,
    winning_pool   INTEGER NOT NULL DEFAULT 0,
    fee_bps        INTEGER,
    created_lt     INTEGER,
    created_tx     TEXT,
    metadata_source TEXT NOT NULL DEFAULT 'event',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_options (
    market_id INTEGER NOT NULL,
    idx       INTEGER NOT NULL,
    label     TEXT NOT NULL,
    PRIMARY KEY (market_id, idx)
);

CREATE TABLE IF NOT EXISTS onchain_bets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    bettor    TEXT NOT NULL,
    option    INTEGER NOT NULL,
    stake     INTEGER NOT NULL,
    tx_hash   TEXT NOT NULL,
    tx_lt     INTEGER NOT NULL,
    claimed   INTEGER NOT NULL DEFAULT 0,
    payout    INTEGER,
    placed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (market_id, bettor)
);

CREATE TABLE IF NOT EXISTS chain_cursors (
    name       TEXT PRIMARY KEY,
    lt         INTEGER NOT NULL DEFAULT 0,
    last_hash  TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chain_txs (
    tx_hash      TEXT NOT NULL,
    lt           INTEGER NOT NULL,
    account      TEXT,
    op           INTEGER,
    processed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tx_hash, lt)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    match_a     TEXT,
    match_b     TEXT,
    payload     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS chain_tx_failures (
    tx_hash       TEXT NOT NULL,
    lt            INTEGER NOT NULL,
    op            INTEGER,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    next_retry_at TEXT,
    PRIMARY KEY (tx_hash, lt)
);

CREATE TABLE IF NOT EXISTS indexer_status (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_run        TEXT,
    last_error      TEXT,
    last_lt         INTEGER,
    processed_total INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_points      ON users(points DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status, closes_at);
CREATE INDEX IF NOT EXISTS idx_predictions_chat   ON predictions(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_options_pred       ON options(prediction_id);
CREATE INDEX IF NOT EXISTS idx_bets_pred          ON bets(prediction_id, result);
CREATE INDEX IF NOT EXISTS idx_bets_user          ON bets(user_id, placed_at DESC);
-- R-05: el listado ordena por (closes_at, market_id DESC); sin la tercera
-- columna SQLite monta un B-TREE temporal para desempatar. Sustituye al antiguo
-- idx_markets_status(status, closes_at), que era su prefijo exacto.
DROP INDEX IF EXISTS idx_markets_status;
CREATE INDEX IF NOT EXISTS idx_markets_status_close ON markets(status, closes_at, market_id DESC);
CREATE INDEX IF NOT EXISTS idx_onchain_bets_mkt   ON onchain_bets(market_id, bettor);
-- R-04: «mis apuestas on-chain» filtra por bettor y ordena por id descendente.
CREATE INDEX IF NOT EXISTS idx_onchain_bets_bettor ON onchain_bets(bettor, id DESC);
-- R-01: agregar pozos por opción de una página de mercados sin B-TREE temporal.
CREATE INDEX IF NOT EXISTS idx_onchain_bets_mkt_opt ON onchain_bets(market_id, option);
CREATE INDEX IF NOT EXISTS idx_pending_match      ON pending_actions(kind, match_a, match_b, consumed_at);
CREATE INDEX IF NOT EXISTS idx_nonces_user        ON auth_nonces(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tx_failures_retry  ON chain_tx_failures(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_notifications_due  ON notifications(status, next_at);
"""

# Columns added after the first public release.  ``init_db`` adds them only
# when missing so that an existing betpoly.db keeps working.
_LEGACY_COLUMNS: Sequence[Tuple[str, str, str]] = (
    ("users", "updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("predictions", "mode", "TEXT NOT NULL DEFAULT 'points'"),
    ("bets", "payout", "INTEGER NOT NULL DEFAULT 0"),
    ("markets", "metadata_source", "TEXT NOT NULL DEFAULT 'event'"),
    ("markets", "pending_resolve_at", "INTEGER"),
)


async def _table_columns(conn: aiosqlite.Connection, table: str) -> List[str]:
    try:
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            return [row["name"] for row in await cur.fetchall()]
    except aiosqlite.Error:  # pragma: no cover - defensive
        return []


async def _migrate_wallet_links(db: aiosqlite.Connection) -> None:
    """Rebuild the legacy ``wallet_links`` table (user_id PK, address UNIQUE)."""
    columns = await _table_columns(db, "wallet_links")
    if not columns:
        await db.executescript(_WALLET_LINKS_DDL)
        return
    if "id" in columns:
        return
    logger.info("Migrating wallet_links: removing global UNIQUE(address)")
    await db.execute("ALTER TABLE wallet_links RENAME TO wallet_links_legacy")
    await db.executescript(_WALLET_LINKS_DDL)
    await db.execute(
        """
        INSERT INTO wallet_links (user_id, address, network, public_key, verified_at, is_active)
        SELECT user_id, address, network, public_key, verified_at, is_active
        FROM wallet_links_legacy
        """
    )
    await db.execute("DROP TABLE wallet_links_legacy")


async def init_db() -> None:
    """Create/upgrade the schema. Safe to call repeatedly."""
    settings = config.get_settings()
    async with transaction() as db:
        await db.executescript(_SCHEMA)
        await _migrate_wallet_links(db)
        for table, column, ddl in _LEGACY_COLUMNS:
            columns = await _table_columns(db, table)
            if columns and column not in columns:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                logger.info("Migrated %s: added column %s", table, column)
        # New databases start with the configured amount of points.
        await db.execute(
            "UPDATE users SET points = ? WHERE points = 1000 AND created_at = updated_at "
            "AND ? != 1000 AND NOT EXISTS (SELECT 1 FROM bets WHERE bets.user_id = users.user_id)",
            (settings.starting_points, settings.starting_points),
        )
        await db.commit()
    logger.info("Database ready at %s (mode=%s)", database_path(), settings.mode)


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass
class BetSettlement:
    bet_id: int
    user_id: int
    option_id: int
    stake: int
    payout: int
    result: str  # win | lose | refund


@dataclass
class SettlementResult:
    prediction_id: int
    title: str
    winning_label: str
    total_pool: int
    winning_pool: int
    rake: int = 0
    bets: List[BetSettlement] = field(default_factory=list)

    @property
    def winners(self) -> List[BetSettlement]:
        return [b for b in self.bets if b.result in ("win", "refund")]

    @property
    def losers(self) -> List[BetSettlement]:
        return [b for b in self.bets if b.result == "lose"]

    @property
    def refunded(self) -> bool:
        return bool(self.bets) and all(b.result == "refund" for b in self.bets)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: Optional[str], full_name: Optional[str]) -> None:
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                full_name  = excluded.full_name,
                updated_at = ?
            """,
            (user_id, username, full_name or "", iso_now()),
        )


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_leaderboard(limit: int = 10) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    async with connection() as db:
        async with db.execute(
            """
            SELECT user_id, username, full_name, points
            FROM users
            ORDER BY points DESC, user_id ASC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def adjust_points(user_id: int, delta: int) -> int:
    """Add ``delta`` points (may be negative) and return the new balance."""
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET points = MAX(0, points + ?), updated_at = ? WHERE user_id = ?",
            (int(delta), iso_now(), user_id),
        )
        async with db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row["points"]) if row else 0


# ---------------------------------------------------------------------------
# Predictions (off-chain sandbox mode)
# ---------------------------------------------------------------------------

async def create_prediction(
    creator_id: int,
    chat_id: int,
    title: str,
    options: List[str],
    closes_at: datetime,
    mode: str = "points",
) -> int:
    settings = config.get_settings()
    if not title.strip():
        raise DatabaseError("El título no puede estar vacío.")
    if not 2 <= len(options) <= settings.max_options:
        raise DatabaseError(f"Necesitas entre 2 y {settings.max_options} opciones.")
    async with transaction() as db:
        cur = await db.execute(
            "INSERT INTO predictions (creator_id, chat_id, title, closes_at, mode) VALUES (?, ?, ?, ?, ?)",
            (creator_id, chat_id, title.strip()[:200], iso_utc(closes_at), mode),
        )
        pred_id = int(cur.lastrowid)
        await db.executemany(
            "INSERT INTO options (prediction_id, label) VALUES (?, ?)",
            [(pred_id, opt.strip()[:80]) for opt in options],
        )
    return pred_id


async def get_prediction(pred_id: int) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute("SELECT * FROM predictions WHERE id = ?", (pred_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_open_predictions(chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Predicciones abiertas **con su pozo**, en una sola consulta (R-01).

    Antes la lista pedía una consulta por predicción para el pozo: con 8 filas
    eran 8 idas a SQLite por cada refresco del mensaje.
    """
    now = iso_now()
    sql = """
        SELECT p.*, COALESCE(SUM(b.amount), 0) AS total_pool
        FROM predictions p
        LEFT JOIN bets b ON b.prediction_id = p.id
        WHERE p.status = 'open' AND p.closes_at > ?
    """
    params: List[Any] = [now]
    if chat_id is not None:
        sql += " AND p.chat_id = ?"
        params.append(chat_id)
    sql += " GROUP BY p.id ORDER BY p.closes_at ASC LIMIT 50"
    async with connection() as db:
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_predictions_to_resolve() -> List[Dict[str, Any]]:
    """Closed predictions that still need a winner (admin panel)."""
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM predictions WHERE status = 'closed' ORDER BY closes_at ASC LIMIT 25"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def close_expired_predictions() -> List[int]:
    """Mark predictions whose ``closes_at`` has passed as ``closed``."""
    now = iso_now()
    async with transaction() as db:
        async with db.execute(
            "SELECT id, title FROM predictions WHERE status = 'open' AND closes_at <= ?",
            (now,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE predictions SET status = 'closed' WHERE id IN ({placeholders})",
            ids,
        )
    return ids


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------

async def place_bet(user_id: int, pred_id: int, option_id: int, amount: int) -> int:
    """Debit points and store the bet atomically. Returns the new balance."""
    settings = config.get_settings()
    amount = int(amount)
    if amount < settings.min_bet_points:
        raise DatabaseError(f"La apuesta mínima es de {settings.min_bet_points} puntos.")
    if amount > settings.max_bet_points:
        raise DatabaseError(f"La apuesta máxima es de {settings.max_bet_points:,} puntos.")

    async with transaction() as db:
        async with db.execute(
            "SELECT status, closes_at, title FROM predictions WHERE id = ?", (pred_id,)
        ) as cur:
            pred = await cur.fetchone()
        if pred is None:
            raise DatabaseError("Predicción no encontrada.")
        if pred["status"] != "open":
            raise DatabaseError("Esta predicción ya no está abierta.")
        if pred["closes_at"] <= iso_now():
            raise DatabaseError("El plazo de esta predicción ya cerró.")

        async with db.execute(
            "SELECT id FROM options WHERE id = ? AND prediction_id = ?", (option_id, pred_id)
        ) as cur:
            if await cur.fetchone() is None:
                raise DatabaseError("La opción no pertenece a esta predicción.")

        cur = await db.execute(
            """
            UPDATE users SET points = points - ?, updated_at = ?
            WHERE user_id = ? AND points >= ?
            """,
            (amount, iso_now(), user_id, amount),
        )
        if cur.rowcount == 0:
            raise InsufficientFunds("Saldo insuficiente.")

        try:
            await db.execute(
                "INSERT INTO bets (user_id, prediction_id, option_id, amount, placed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, pred_id, option_id, amount, iso_now()),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateBet("Ya has apostado en esta predicción.") from exc

        async with db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row["points"]) if row else 0


def _split_payouts(
    winners: List[Dict[str, Any]], distributable_pool: int, winning_pool: int
) -> Dict[int, int]:
    """Reparto pari-mutuel con redondeo determinista.

    Cada ganador recibe ``payout(stake, distributable, winning_pool)`` (la única
    fórmula del proyecto, en :mod:`bot.economics`) redondeada hacia abajo; el
    sobrante se reparte de uno en uno entre las apuestas mayores para que la
    suma sea *exactamente* el pozo distribuible (no se crean puntos).
    """
    if winning_pool <= 0 or distributable_pool <= 0:
        return {b["id"]: 0 for b in winners}
    payouts = {
        b["id"]: economics.payout(int(b["amount"]), distributable_pool, winning_pool)
        for b in winners
    }
    remainder = distributable_pool - sum(payouts.values())
    if remainder > 0:
        order = sorted(winners, key=lambda b: (-int(b["amount"]), b["id"]))
        for i in range(remainder):
            payouts[order[i % len(order)]["id"]] += 1
    return payouts


async def settle_prediction(pred_id: int, winning_option_id: int) -> SettlementResult:
    """Resolve a prediction and distribute the pool pari-mutuel style.

    * Winners share the whole pool proportionally to their stake.
    * If nobody picked the winning option (or there are no bets) every stake is
      refunded instead of silently burning points.
    * Idempotent: resolving twice raises instead of paying twice.
    """
    settings = config.get_settings()
    async with transaction() as db:
        async with db.execute("SELECT * FROM predictions WHERE id = ?", (pred_id,)) as cur:
            pred = await cur.fetchone()
        if pred is None:
            raise DatabaseError("Predicción no encontrada.")
        if pred["status"] == "resolved":
            raise DatabaseError("Esta predicción ya fue resuelta.")
        if pred["status"] not in ("open", "closed"):
            raise DatabaseError("Esta predicción no se puede resolver.")

        async with db.execute(
            "SELECT id, label FROM options WHERE id = ? AND prediction_id = ?",
            (winning_option_id, pred_id),
        ) as cur:
            option = await cur.fetchone()
        if option is None:
            raise DatabaseError("La opción ganadora no pertenece a esta predicción.")
        winning_label = option["label"]

        async with db.execute(
            "SELECT id, user_id, option_id, amount FROM bets WHERE prediction_id = ? ORDER BY id",
            (pred_id,),
        ) as cur:
            bets = [dict(r) for r in await cur.fetchall()]

        total_pool = sum(int(b["amount"]) for b in bets)
        winners = [b for b in bets if int(b["option_id"]) == int(winning_option_id)]
        winning_pool = sum(int(b["amount"]) for b in winners)

        settlements: List[BetSettlement] = []
        rake = 0

        if not bets:
            payouts: Dict[int, int] = {}
        elif winning_pool == 0:
            # Nobody was right: full refund, no rake.
            payouts = {b["id"]: int(b["amount"]) for b in bets}
        else:
            distributable = economics.distributable(total_pool, settings.house_rake_bps)
            rake = total_pool - distributable
            payouts = _split_payouts(winners, distributable, winning_pool)

        now = iso_now()
        for bet in bets:
            payout = int(payouts.get(bet["id"], 0))
            if not bets or winning_pool == 0:
                result = "refund"
            elif int(bet["option_id"]) == int(winning_option_id):
                result = "win"
            else:
                result = "lose"
            await db.execute(
                "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
                (result, payout, bet["id"]),
            )
            if payout:
                await db.execute(
                    "UPDATE users SET points = points + ?, updated_at = ? WHERE user_id = ?",
                    (payout, now, bet["user_id"]),
                )
            settlements.append(
                BetSettlement(
                    bet_id=int(bet["id"]),
                    user_id=int(bet["user_id"]),
                    option_id=int(bet["option_id"]),
                    stake=int(bet["amount"]),
                    payout=payout,
                    result=result,
                )
            )

        await db.execute(
            "UPDATE predictions SET status = 'resolved', winner_opt = ?, resolved_at = ? WHERE id = ?",
            (winning_label, now, pred_id),
        )

        # F-04: quien ganó o recuperó puntos merece saberlo. Se escribe en la
        # misma transacción, así que no puede haber liquidación sin aviso ni
        # aviso de una liquidación que no ocurrió. `queue` deduplica.
        for settlement in settlements:
            if settlement.result not in ("win", "refund"):
                continue
            await _queue_notification(
                db,
                settlement.user_id,
                NOTIFICATION_PREDICTION_RESULT,
                pred_id,
                {
                    "prediction_id": pred_id,
                    "title": pred["title"],
                    "winning_label": winning_label,
                    "result": settlement.result,
                    "stake": settlement.stake,
                    "payout": settlement.payout,
                },
            )

    return SettlementResult(
        prediction_id=pred_id,
        title=pred["title"],
        winning_label=winning_label,
        total_pool=total_pool,
        winning_pool=winning_pool,
        rake=rake,
        bets=settlements,
    )


async def resolve_prediction(pred_id: int, winning_option_id: int) -> str:
    """Backwards compatible wrapper: resolve and return the winning label."""
    result = await settle_prediction(pred_id, winning_option_id)
    return result.winning_label


# ---------------------------------------------------------------------------
# Options / history
# ---------------------------------------------------------------------------

async def get_options(pred_id: int) -> List[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM options WHERE prediction_id = ? ORDER BY id", (pred_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_user_bets(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            """
            SELECT b.id, b.amount, b.payout, b.result, b.placed_at,
                   p.id AS pred_id, p.title AS pred_title, p.status AS pred_status,
                   p.winner_opt AS pred_winner,
                   o.label AS option_label
            FROM bets b
            JOIN predictions p ON b.prediction_id = p.id
            JOIN options o     ON b.option_id = o.id
            WHERE b.user_id = ?
            ORDER BY b.placed_at DESC, b.id DESC
            LIMIT ?
            """,
            (user_id, max(1, min(int(limit), 500))),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_user_stats(user_id: int) -> Dict[str, Any]:
    """Resumen del perfil calculado en SQL (R-02).

    Antes se traían hasta 500 apuestas al proceso para contar ganadas, perdidas,
    reembolsos y neto. Los mismos números salen con un ``SUM``/``COUNT``.
    """
    async with connection() as db:
        async with db.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(amount), 0) AS staked,
                   COALESCE(SUM(payout), 0) AS payouts,
                   COALESCE(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN result = 'lose' THEN 1 ELSE 0 END), 0) AS losses,
                   COALESCE(SUM(CASE WHEN result = 'refund' THEN 1 ELSE 0 END), 0) AS refunds,
                   COALESCE(SUM(CASE WHEN result IS NULL THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN result IS NOT NULL THEN payout - amount ELSE 0 END), 0)
                       AS net
            FROM bets WHERE user_id = ?
            """,
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
    stats = dict(row) if row else {}
    for key in ("total", "staked", "payouts", "wins", "losses", "refunds", "pending", "net"):
        stats[key] = int(stats.get(key) or 0)
    decided = stats["wins"] + stats["losses"]
    stats["accuracy"] = (stats["wins"] / decided * 100) if decided else None
    return stats


async def ranking_position(user_id: int) -> Optional[int]:
    """Puesto en el ranking por puntos, contado en SQL (R-03)."""
    async with connection() as db:
        async with db.execute("SELECT points FROM users WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        async with db.execute(
            "SELECT COUNT(*) AS better FROM users"
            " WHERE points > ? OR (points = ? AND user_id < ?)",
            (int(row["points"]), int(row["points"]), int(user_id)),
        ) as cur:
            better = await cur.fetchone()
    return int(better["better"]) + 1


async def get_bettors_for_prediction(pred_id: int) -> List[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            """
            SELECT b.user_id, b.option_id, b.amount, b.payout, b.result,
                   u.username, u.full_name
            FROM bets b LEFT JOIN users u ON u.user_id = b.user_id
            WHERE b.prediction_id = ?
            ORDER BY b.id
            """,
            (pred_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_prediction_pools(pred_id: int) -> Dict[int, int]:
    """Return ``{option_id: staked_points}`` plus a ``-1`` key with the total."""
    async with connection() as db:
        async with db.execute(
            "SELECT option_id, COALESCE(SUM(amount),0) AS pool FROM bets "
            "WHERE prediction_id = ? GROUP BY option_id",
            (int(pred_id),),
        ) as cur:
            pools = {int(r["option_id"]): int(r["pool"]) for r in await cur.fetchall()}
    pools[-1] = sum(pools.values())
    return pools


async def get_user_bet(pred_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM bets WHERE prediction_id = ? AND user_id = ?",
            (int(pred_id), int(user_id)),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Reminders (deduplicated push notifications)
# ---------------------------------------------------------------------------

async def claim_reminders(kind: str = "closing") -> List[Dict[str, Any]]:
    """Atomically claim due reminders so a job repeat cannot spam users."""
    settings = config.get_settings()
    if settings.reminder_hours_before <= 0:
        return []
    threshold = iso_utc(
        datetime.now(timezone.utc) + timedelta(hours=settings.reminder_hours_before)
    )
    now = iso_now()
    async with transaction() as db:
        async with db.execute(
            """
            SELECT p.id AS pred_id, p.title, p.closes_at, b.user_id
            FROM predictions p
            JOIN bets b ON b.prediction_id = p.id
            WHERE p.status = 'open' AND p.closes_at > ? AND p.closes_at <= ?
            ORDER BY p.closes_at ASC
            LIMIT 500
            """,
            (now, threshold),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        claimed: List[Dict[str, Any]] = []
        for row in rows:
            cur = await db.execute(
                "INSERT OR IGNORE INTO reminders (prediction_id, user_id, kind, sent_at) VALUES (?, ?, ?, ?)",
                (row["pred_id"], row["user_id"], kind, now),
            )
            if cur.rowcount:
                claimed.append(row)
        return claimed


async def get_reminder_targets(pred_id: int) -> List[int]:
    async with connection() as db:
        async with db.execute(
            "SELECT user_id FROM bets WHERE prediction_id = ?", (pred_id,)
        ) as cur:
            return [int(r["user_id"]) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Outbox de notificaciones (F-04)
# ---------------------------------------------------------------------------
# Los productores (liquidación de puntos e indexador) escriben aquí *dentro* de
# su propia transacción; el repartidor de `main.py` las envía y reintenta. Así
# un fallo de Telegram no puede perder el aviso ni bloquear la cadena.

#: Intentos antes de dar el aviso por muerto (usuario que bloqueó el bot, etc.).
MAX_NOTIFICATION_ATTEMPTS = 5
#: Backoff exponencial: 60 s, 120 s, 240 s… con techo de una hora.
NOTIFICATION_MAX_BACKOFF = 3600


async def _queue_notification(
    db: aiosqlite.Connection,
    user_id: int,
    kind: str,
    ref_id: Any,
    payload: Dict[str, Any],
    *,
    now: Optional[int] = None,
    delay: int = 0,
) -> None:
    """Inserta un aviso pendiente; el UNIQUE evita duplicados (dedupe)."""
    await db.execute(
        """
        INSERT OR IGNORE INTO notifications (user_id, kind, ref_id, payload, next_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            kind,
            str(ref_id),
            json.dumps(payload, ensure_ascii=False),
            int(now if now is not None else time.time()) + int(delay),
        ),
    )


async def queue_notification(
    user_id: int, kind: str, ref_id: Any, payload: Dict[str, Any], *, delay: int = 0
) -> bool:
    """Encola un aviso fuera de una transacción; devuelve si era nuevo."""
    async with transaction() as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO notifications (user_id, kind, ref_id, payload, next_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                kind,
                str(ref_id),
                json.dumps(payload, ensure_ascii=False),
                int(time.time()) + int(delay),
            ),
        )
        return bool(cur.rowcount)


async def due_notifications(limit: int = 20, *, now: Optional[int] = None) -> List[Dict[str, Any]]:
    """Avisos pendientes cuya hora de reintento ya pasó."""
    async with connection() as db:
        async with db.execute(
            """
            SELECT * FROM notifications
            WHERE status = 'pending' AND next_at <= ?
            ORDER BY next_at, id
            LIMIT ?
            """,
            (int(now if now is not None else time.time()), max(1, min(int(limit), 200))),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def mark_notification_sent(notification_id: int) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE notifications SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
            (iso_now(), int(notification_id)),
        )


async def mark_notification_failed(notification_id: int, error: str = "") -> None:
    """Suma un intento y programa el siguiente con backoff exponencial."""
    async with transaction() as db:
        async with db.execute(
            "SELECT attempts FROM notifications WHERE id = ?", (int(notification_id),)
        ) as cur:
            row = await cur.fetchone()
        attempts = int(row["attempts"]) + 1 if row else MAX_NOTIFICATION_ATTEMPTS
        status = "failed" if attempts >= MAX_NOTIFICATION_ATTEMPTS else "pending"
        delay = min(NOTIFICATION_MAX_BACKOFF, 60 * 2 ** max(0, attempts - 1))
        await db.execute(
            "UPDATE notifications SET attempts = ?, status = ?, next_at = ?, last_error = ?"
            " WHERE id = ?",
            (
                attempts,
                status,
                int(time.time()) + delay,
                (error or "")[:300],
                int(notification_id),
            ),
        )


async def reschedule_notification(notification_id: int, delay: int) -> None:
    """Aplaza un aviso sin gastar un intento (p. ej. limitador de tasa)."""
    async with transaction() as db:
        await db.execute(
            "UPDATE notifications SET next_at = ? WHERE id = ?",
            (int(time.time()) + max(1, int(delay)), int(notification_id)),
        )


async def notifications_for(user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Avisos de un usuario; útil para `/misbets` y para los tests."""
    query = "SELECT * FROM notifications WHERE user_id = ?"
    params: List[Any] = [int(user_id)]
    if status:
        query += " AND status = ?"
        params.append(status)
    async with connection() as db:
        async with db.execute(query + " ORDER BY id", params) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ---------------------------------------------------------------------------
# Wallet links
# ---------------------------------------------------------------------------

async def link_wallet(
    user_id: int, address: str, network: str = "ton", public_key: Optional[str] = None
) -> None:
    address = address.strip()
    if not address:
        raise WalletConflict("Dirección vacía.")
    async with transaction() as db:
        async with db.execute(
            "SELECT user_id FROM wallet_links WHERE address = ? AND is_active = 1", (address,)
        ) as cur:
            owner = await cur.fetchone()
        if owner and int(owner["user_id"]) != int(user_id):
            raise WalletConflict("Esta wallet ya está vinculada a otra cuenta de Telegram.")
        # One active wallet per account: deactivate the previous one first so the
        # partial unique index stays satisfied and the history is preserved.
        await db.execute(
            "UPDATE wallet_links SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (int(user_id),),
        )
        try:
            await db.execute(
                """
                INSERT INTO wallet_links (user_id, address, network, public_key, verified_at, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (int(user_id), address, network, public_key, iso_now()),
            )
        except aiosqlite.IntegrityError as exc:
            # Two concurrent verifications of the same address: exactly one wins.
            raise WalletConflict(
                "Esta wallet ya está vinculada a otra cuenta de Telegram."
            ) from exc


async def get_wallet(user_id: int) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM wallet_links WHERE user_id = ? AND is_active = 1", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_wallet_owner(address: str) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM wallet_links WHERE address = ? AND is_active = 1", (address,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def unlink_wallet(user_id: int) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE wallet_links SET is_active = 0 WHERE user_id = ?", (user_id,)
        )


# ---------------------------------------------------------------------------
# Nonces (anti replay for ton_proof)
# ---------------------------------------------------------------------------

async def create_nonce(user_id: int, purpose: str, ttl_seconds: int = 600) -> str:
    import secrets as _s

    nonce = _s.token_hex(16)
    now = int(datetime.now(timezone.utc).timestamp())
    async with transaction() as db:
        await db.execute(
            "INSERT INTO auth_nonces (nonce, user_id, purpose, created_at) VALUES (?, ?, ?, ?)",
            (nonce, user_id, purpose, now),
        )
        await db.execute(
            "DELETE FROM auth_nonces WHERE created_at < ?", (now - max(ttl_seconds, 3600),)
        )
    return nonce


async def consume_nonce(nonce: str, user_id: int, purpose: str, ttl_seconds: int = 600) -> bool:
    """Single-use nonce validation. Returns True only the first time."""
    if not nonce or ttl_seconds <= 0:
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    async with transaction() as db:
        cur = await db.execute(
            """
            UPDATE auth_nonces SET used_at = ?
            WHERE nonce = ? AND user_id = ? AND purpose = ?
              AND used_at IS NULL AND created_at >= ?
            """,
            (now, nonce, user_id, purpose, now - ttl_seconds),
        )
        return cur.rowcount == 1


async def purge_nonces(older_than_seconds: int = 86_400) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    async with transaction() as db:
        await db.execute("DELETE FROM auth_nonces WHERE created_at < ?", (now - older_than_seconds,))


# ---------------------------------------------------------------------------
# On-chain mirror: markets
# ---------------------------------------------------------------------------

async def insert_market_placeholder(
    market_id: int,
    creator_address: Optional[str],
    option_count: int,
    created_lt: Optional[int] = None,
    created_tx: Optional[str] = None,
) -> bool:
    """Create a minimal row for a market we only know through a bet.

    The row is explicitly marked ``metadata_source='placeholder'`` so the real
    metadata can replace it later without ever trusting a placeholder over an
    on-chain event (S-02).
    """
    async with transaction() as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO markets
                (market_id, creator_address, title, option_count, closes_at,
                 created_lt, created_tx, metadata_source, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, 'placeholder', ?)
            """,
            (
                int(market_id), creator_address, f"Mercado #{int(market_id)}",
                int(option_count), created_lt, created_tx, iso_now(),
            ),
        )
        return cur.rowcount == 1


def _is_placeholder_title(title: Optional[str], market_id: int) -> bool:
    return not title or title.strip() == f"Mercado #{int(market_id)}"


async def enrich_market_from_event(
    market_id: int,
    creator_address: Optional[str],
    title: str,
    option_count: int,
    closes_at: int,
    title_hash: Optional[str] = None,
    created_lt: Optional[int] = None,
    created_tx: Optional[str] = None,
    fee_bps: Optional[int] = None,
) -> Tuple[bool, bool]:
    """Apply the authoritative metadata of a ``market_created`` event.

    Returns ``(created, enriched)``. Only empty or placeholder fields are
    filled: a market that already carries real metadata is never overwritten.
    """
    async with transaction() as db:
        async with db.execute(
            "SELECT * FROM markets WHERE market_id = ?", (int(market_id),)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            await db.execute(
                """
                INSERT INTO markets
                    (market_id, creator_address, title, title_hash, option_count, closes_at,
                     created_lt, created_tx, fee_bps, metadata_source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'event', ?)
                """,
                (
                    int(market_id), creator_address, title[:200], title_hash,
                    int(option_count), int(closes_at), created_lt, created_tx,
                    int(fee_bps) if fee_bps is not None else None, iso_now(),
                ),
            )
            return True, False

        current = dict(row)
        if current.get("metadata_source") == "event":
            return False, False

        updates: Dict[str, Any] = {}
        if title and _is_placeholder_title(current.get("title"), market_id):
            updates["title"] = title[:200]
        if int(current.get("closes_at") or 0) == 0 and int(closes_at or 0) > 0:
            updates["closes_at"] = int(closes_at)
        if int(option_count or 0) > int(current.get("option_count") or 0):
            updates["option_count"] = int(option_count)
        for column, value in (
            ("creator_address", creator_address),
            ("title_hash", title_hash),
            ("created_lt", created_lt),
            ("created_tx", created_tx),
            ("fee_bps", int(fee_bps) if fee_bps is not None else None),
        ):
            if value is not None and current.get(column) in (None, ""):
                updates[column] = value
        updates["metadata_source"] = "event"
        updates["updated_at"] = iso_now()

        assignments = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE markets SET {assignments} WHERE market_id = ?",  # noqa: S608 - column names are literals
            (*updates.values(), int(market_id)),
        )
        return False, True


async def set_market_pending_resolve(market_id: int, timestamp: int) -> None:
    """Mark that a resolution/cancellation was prepared (blocks new bets, F-06)."""
    async with transaction() as db:
        await db.execute(
            "UPDATE markets SET pending_resolve_at = ?, updated_at = ? WHERE market_id = ?",
            (int(timestamp), iso_now(), int(market_id)),
        )


async def update_market_settled(
    market_id: int, winning_option: int, total_pool: int, winning_pool: int, status: str = "resolved"
) -> None:
    async with transaction() as db:
        await db.execute(
            """
            UPDATE markets
            SET status = ?, winning_option = ?, total_pool = ?, winning_pool = ?,
                pending_resolve_at = NULL, updated_at = ?
            WHERE market_id = ?
            """,
            (status, int(winning_option), int(total_pool), int(winning_pool), iso_now(), int(market_id)),
        )
        if status == "resolved":
            await _queue_market_notifications(db, int(market_id), cancelled=False)


async def _queue_market_notifications(
    db: aiosqlite.Connection, market_id: int, *, cancelled: bool
) -> None:
    """Avisa a quien puede reclamar: ganadores y, si se cancela, a todos."""
    async with db.execute(
        "SELECT title, winning_option FROM markets WHERE market_id = ?", (int(market_id),)
    ) as cur:
        market = await cur.fetchone()
    if market is None:
        return
    query = """
        SELECT wl.user_id, ob.option
        FROM onchain_bets ob
        JOIN wallet_links wl
          ON wl.address = ob.bettor AND wl.is_active = 1
        WHERE ob.market_id = ?
    """
    async with db.execute(query, (int(market_id),)) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for row in rows:
        if not cancelled and int(row["option"]) != int(market["winning_option"] or 0):
            continue
        await _queue_notification(
            db,
            row["user_id"],
            NOTIFICATION_MARKET_REFUND if cancelled else NOTIFICATION_MARKET_CLAIM,
            market_id,
            {"market_id": market_id, "title": market["title"]},
        )


async def update_market_cancelled(market_id: int) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE markets SET status = 'cancelled', pending_resolve_at = NULL,"
            " updated_at = ? WHERE market_id = ?",
            (iso_now(), int(market_id)),
        )
        await _queue_market_notifications(db, int(market_id), cancelled=True)


async def get_market(market_id: int) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute("SELECT * FROM markets WHERE market_id = ?", (market_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        market = dict(row)
        market["options"] = await _market_options(db, int(market_id))
        return market


async def _market_options(db: aiosqlite.Connection, market_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT idx, label FROM market_options WHERE market_id = ? ORDER BY idx", (market_id,)
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    if rows:
        return rows
    # Markets created outside the Mini App have no off-chain labels.
    async with db.execute("SELECT option_count FROM markets WHERE market_id = ?", (market_id,)) as cur:
        row = await cur.fetchone()
    count = int(row["option_count"]) if row else 0
    return _default_options(count)


def _default_options(count: int) -> List[Dict[str, Any]]:
    """Etiquetas de relleno para mercados creados fuera de la Mini App."""
    return [{"idx": i, "label": f"Opción {i + 1}"} for i in range(max(0, int(count)))]


async def list_markets(
    status: Optional[str] = "open", limit: int = 25, offset: int = 0
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    sql = "SELECT * FROM markets"
    params: List[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY closes_at ASC, market_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with connection() as db:
        async with db.execute(sql, params) as cur:
            markets = [dict(r) for r in await cur.fetchall()]
        if not markets:
            return []
        # R-01: opciones y pozos de toda la página en dos consultas, no tres por
        # mercado. `/api/markets` se sondea cada pocos segundos, así que la
        # diferencia se nota.
        ids = [int(m["market_id"]) for m in markets]
        placeholders = ",".join("?" * len(ids))
        labels = await _labels_for(db, ids, placeholders)
        pools, totals, counts = await _pools_for(db, ids, placeholders)
        for market in markets:
            market_id = int(market["market_id"])
            market["options"] = labels.get(market_id) or _default_options(
                int(market.get("option_count") or 0)
            )
            market["option_pools"] = pools.get(market_id, {})
            market["pool"] = totals.get(market_id, 0)
            market["bets"] = counts.get(market_id, 0)
        return markets


async def _labels_for(
    db: aiosqlite.Connection, ids: List[int], placeholders: str
) -> Dict[int, List[Dict[str, Any]]]:
    async with db.execute(
        "SELECT market_id, idx, label FROM market_options"
        f" WHERE market_id IN ({placeholders}) ORDER BY market_id, idx",
        ids,
    ) as cur:
        rows = await cur.fetchall()
    labels: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        labels.setdefault(int(row["market_id"]), []).append(
            {"idx": int(row["idx"]), "label": row["label"]}
        )
    return labels


async def _pools_for(
    db: aiosqlite.Connection, ids: List[int], placeholders: str
) -> Tuple[Dict[int, Dict[str, int]], Dict[int, int], Dict[int, int]]:
    async with db.execute(
        "SELECT market_id, option, COALESCE(SUM(stake),0) AS pool, COUNT(*) AS bets"
        f" FROM onchain_bets WHERE market_id IN ({placeholders})"
        " GROUP BY market_id, option",
        ids,
    ) as cur:
        rows = await cur.fetchall()
    pools: Dict[int, Dict[str, int]] = {}
    totals: Dict[int, int] = {}
    counts: Dict[int, int] = {}
    for row in rows:
        market_id = int(row["market_id"])
        stake = int(row["pool"] or 0)
        pools.setdefault(market_id, {})[str(int(row["option"]))] = stake
        totals[market_id] = totals.get(market_id, 0) + stake
        counts[market_id] = counts.get(market_id, 0) + int(row["bets"] or 0)
    return pools, totals, counts


async def _option_pools(db: aiosqlite.Connection, market_id: int) -> Dict[str, int]:
    async with db.execute(
        "SELECT option, COALESCE(SUM(stake),0) AS pool FROM onchain_bets WHERE market_id = ? GROUP BY option",
        (market_id,),
    ) as cur:
        return {str(int(r["option"])): int(r["pool"]) for r in await cur.fetchall()}


async def set_market_options(market_id: int, labels: Iterable[str]) -> None:
    async with transaction() as db:
        await db.execute("DELETE FROM market_options WHERE market_id = ?", (int(market_id),))
        await db.executemany(
            "INSERT INTO market_options (market_id, idx, label) VALUES (?, ?, ?)",
            [(int(market_id), i, str(label)[:80]) for i, label in enumerate(labels)],
        )


# ---------------------------------------------------------------------------
# On-chain mirror: bets
# ---------------------------------------------------------------------------

async def record_onchain_bet(
    market_id: int,
    bettor: str,
    option: int,
    stake: int,
    tx_hash: str,
    tx_lt: int,
) -> bool:
    """Insert a bet observed on-chain. Returns False when already known."""
    async with transaction() as db:
        try:
            cur = await db.execute(
                """
                INSERT INTO onchain_bets (market_id, bettor, option, stake, tx_hash, tx_lt, placed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (int(market_id), bettor, int(option), int(stake), tx_hash, int(tx_lt), iso_now()),
            )
            return cur.rowcount == 1
        except aiosqlite.IntegrityError:
            return False


async def mark_onchain_bet_claimed(market_id: int, bettor: str, payout: Optional[int] = None) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE onchain_bets SET claimed = 1, payout = COALESCE(?, payout) "
            "WHERE market_id = ? AND bettor = ?",
            (payout, int(market_id), bettor),
        )


async def get_onchain_bet(market_id: int, bettor: str) -> Optional[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM onchain_bets WHERE market_id = ? AND bettor = ?",
            (int(market_id), bettor),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_onchain_bets(bettor: str, limit: int = 50) -> List[Dict[str, Any]]:
    async with connection() as db:
        async with db.execute(
            """
            SELECT ob.*, m.title, m.status, m.winning_option, m.closes_at,
                   m.option_count, m.total_pool, m.winning_pool
            FROM onchain_bets ob
            JOIN markets m ON m.market_id = ob.market_id
            WHERE ob.bettor = ?
            ORDER BY ob.id DESC LIMIT ?
            """,
            (bettor, max(1, min(int(limit), 200))),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def market_pool(market_id: int) -> int:
    async with connection() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(stake),0) AS pool FROM onchain_bets WHERE market_id = ?",
            (int(market_id),),
        ) as cur:
            row = await cur.fetchone()
            return int(row["pool"] or 0)


# ---------------------------------------------------------------------------
# Indexer bookkeeping
# ---------------------------------------------------------------------------

async def get_cursor(name: str) -> Tuple[int, Optional[str]]:
    async with connection() as db:
        async with db.execute(
            "SELECT lt, last_hash FROM chain_cursors WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return (int(row["lt"]), row["last_hash"]) if row else (0, None)


async def set_cursor(name: str, lt: int, last_hash: Optional[str]) -> None:
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO chain_cursors (name, lt, last_hash, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET lt = excluded.lt, last_hash = excluded.last_hash,
                                            updated_at = excluded.updated_at
            """,
            (name, int(lt), last_hash, iso_now()),
        )


async def mark_tx_processed(tx_hash: str, lt: int, account: Optional[str], op: Optional[int]) -> bool:
    async with transaction() as db:
        try:
            cur = await db.execute(
                "INSERT INTO chain_txs (tx_hash, lt, account, op, processed_at) VALUES (?, ?, ?, ?, ?)",
                (tx_hash, int(lt), account, op, iso_now()),
            )
            return cur.rowcount == 1
        except aiosqlite.IntegrityError:
            return False


async def unmark_tx_processed(tx_hash: str, lt: int) -> None:
    """Release a transaction so the next poll retries it after a failure."""
    async with transaction() as db:
        await db.execute("DELETE FROM chain_txs WHERE tx_hash = ? AND lt = ?", (tx_hash, int(lt)))


async def register_chain_tx_failure(
    tx_hash: str, lt: int, op: Optional[int], error: str
) -> int:
    """Record a failed transaction and return how many times it has failed.

    The row is the *dead-letter queue* of the indexer: it survives restarts and
    is retried with backoff after the cursor has moved past it.
    """
    message = (error or "")[:500]
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO chain_tx_failures (tx_hash, lt, op, error, attempts, first_seen, last_seen)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(tx_hash, lt) DO UPDATE SET
                attempts  = chain_tx_failures.attempts + 1,
                error     = excluded.error,
                last_seen = excluded.last_seen
            """,
            (tx_hash, int(lt), op, message, iso_now(), iso_now()),
        )
        async with db.execute(
            "SELECT attempts FROM chain_tx_failures WHERE tx_hash = ? AND lt = ?",
            (tx_hash, int(lt)),
        ) as cur:
            row = await cur.fetchone()
        return int(row["attempts"]) if row else 1


async def schedule_chain_tx_retry(tx_hash: str, lt: int, delay_seconds: int) -> None:
    """Push the next retry attempt into the future (exponential backoff)."""
    retry_at = iso_now(offset_seconds=max(0, int(delay_seconds)))
    async with transaction() as db:
        await db.execute(
            "UPDATE chain_tx_failures SET next_retry_at = ? WHERE tx_hash = ? AND lt = ?",
            (retry_at, tx_hash, int(lt)),
        )


async def list_chain_tx_failures(limit: int = 100, *, due_only: bool = False) -> List[Dict[str, Any]]:
    """Failed transactions, oldest first. ``due_only`` filters by backoff."""
    sql = "SELECT * FROM chain_tx_failures"
    params: List[Any] = []
    if due_only:
        sql += " WHERE next_retry_at IS NULL OR next_retry_at <= ?"
        params.append(iso_now())
    sql += " ORDER BY lt ASC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    async with connection() as db:
        async with db.execute(sql, params) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def count_chain_tx_failures() -> int:
    async with connection() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM chain_tx_failures") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0


async def clear_chain_tx_failure(tx_hash: str, lt: int) -> None:
    async with transaction() as db:
        await db.execute(
            "DELETE FROM chain_tx_failures WHERE tx_hash = ? AND lt = ?", (tx_hash, int(lt))
        )


async def record_indexer_run(
    last_lt: Optional[int], error: Optional[str], processed: int = 0
) -> None:
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO indexer_status (id, last_run, last_error, last_lt, processed_total)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_run = excluded.last_run,
                last_error = excluded.last_error,
                last_lt = excluded.last_lt,
                processed_total = indexer_status.processed_total + ?
            """,
            (iso_now(), error, last_lt, processed, processed),
        )


async def get_indexer_status() -> Dict[str, Any]:
    async with connection() as db:
        async with db.execute("SELECT * FROM indexer_status WHERE id = 1") as cur:
            row = await cur.fetchone()
        status = (
            {"last_run": None, "last_error": None, "last_lt": None, "processed_total": 0}
            if row is None
            else dict(row)
        )
        async with db.execute("SELECT COUNT(*) AS n FROM chain_tx_failures") as cur:
            failures = await cur.fetchone()
        status["failures"] = int(failures["n"]) if failures else 0
        return status


# ---------------------------------------------------------------------------
# Pending actions (Mini App intents waiting for a chain confirmation)
# ---------------------------------------------------------------------------

async def create_pending_action(
    user_id: int,
    kind: str,
    match_a: Optional[str] = None,
    match_b: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    async with transaction() as db:
        cur = await db.execute(
            """
            INSERT INTO pending_actions (user_id, kind, match_a, match_b, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, kind, match_a, match_b, json.dumps(payload or {}, ensure_ascii=False), iso_now()),
        )
        return int(cur.lastrowid)


async def find_pending_action(
    kind: str, match_a: Optional[str] = None, match_b: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    sql = "SELECT * FROM pending_actions WHERE kind = ? AND consumed_at IS NULL"
    params: List[Any] = [kind]
    if match_a is not None:
        sql += " AND match_a = ?"
        params.append(match_a)
    if match_b is not None:
        sql += " AND match_b = ?"
        params.append(match_b)
    sql += " ORDER BY id ASC LIMIT 1"
    async with connection() as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        action = dict(row)
        action["payload"] = json.loads(action["payload"] or "{}")
        return action


async def consume_pending_action(action_id: int) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE pending_actions SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (iso_now(), int(action_id)),
        )


async def purge_pending_actions(max_age_seconds: int = 86_400) -> None:
    cutoff = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds))
    async with transaction() as db:
        await db.execute(
            "DELETE FROM pending_actions WHERE created_at < ? AND consumed_at IS NOT NULL",
            (cutoff,),
        )
