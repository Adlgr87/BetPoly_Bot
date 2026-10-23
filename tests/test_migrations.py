"""Migraciones versionadas con `PRAGMA user_version` (D-04).

La regla es simple y por eso se puede probar: cada versión se aplica una sola
vez, en orden, y una base de datos más nueva que el código se rechaza en vez de
corromperse. Antes las «migraciones» eran tres bloques sueltos en `init_db` y un
`UPDATE ... WHERE points = 1000` que se repetía en cada arranque.
"""
from __future__ import annotations

import aiosqlite
import pytest

import config
from bot import database as db_module

LEGACY_DDL = """
CREATE TABLE users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    points      INTEGER NOT NULL DEFAULT 1000,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    closes_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    winner_opt  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE TABLE bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    prediction_id INTEGER NOT NULL,
    option_id     INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    placed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    result        TEXT,
    UNIQUE (user_id, prediction_id)
);
CREATE TABLE wallet_links (
    user_id     INTEGER PRIMARY KEY,
    address     TEXT NOT NULL UNIQUE,
    network     TEXT NOT NULL DEFAULT 'ton',
    public_key  TEXT,
    verified_at TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
"""


async def _legacy_db(path) -> None:
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(LEGACY_DDL)
        # Usuario «virgen» con el valor histórico y otro con historial real.
        await conn.execute(
            "INSERT INTO users (user_id, username, full_name, points)"
            " VALUES (7, 'viejo', 'Viejo', 1000)"
        )
        await conn.execute(
            "INSERT INTO users (user_id, username, full_name, created_at, updated_at)"
            " VALUES (8, 'activo', 'Activo', '2026-01-01', '2026-02-01')"
        )
        await conn.execute(
            "INSERT INTO wallet_links VALUES (7, '0:deadbeef', 'ton', NULL,"
            " '2026-01-01T00:00:00+00:00', 0)"
        )
        await conn.commit()


async def _open(path):
    db_module.configure_database(str(path))
    await db_module.init_db()
    return db_module


async def _version() -> int:
    async with db_module.connection() as conn:
        async with conn.execute("PRAGMA user_version") as cur:
            return int((await cur.fetchone())[0])


class TestSchemaVersion:
    async def test_fresh_database_is_stamped(self, db):
        assert await _version() == db_module.SCHEMA_VERSION

    async def test_initialising_twice_is_a_no_op(self, db):
        before = await _version()
        await db.init_db()
        assert await _version() == before
        assert await db.get_user(1) is None  # nada duplicado ni perdido

    async def test_a_future_schema_is_rejected(self, db):
        async with db.connection() as conn:
            await conn.execute(f"PRAGMA user_version = {db_module.SCHEMA_VERSION + 1}")
            await conn.commit()
        with pytest.raises(db.DatabaseError, match="versión"):
            await db.init_db()

    async def test_every_migration_has_a_destination(self):
        assert sorted(db_module._MIGRATIONS) == list(
            range(1, db_module.SCHEMA_VERSION + 1)
        )


class TestLegacyUpgrade:
    async def test_legacy_database_keeps_its_data(self, tmp_path):
        path = tmp_path / "legacy.db"
        await _legacy_db(path)
        await _open(path)
        try:
            assert await _version() == db_module.SCHEMA_VERSION
            user = await db_module.get_user(7)
            assert user is not None and user["username"] == "viejo"
            async with db_module.connection() as conn:
                async with conn.execute("PRAGMA table_info(wallet_links)") as cur:
                    columns = [row["name"] for row in await cur.fetchall()]
                assert "id" in columns
                async with conn.execute(
                    "SELECT COUNT(*) AS n FROM wallet_links"
                ) as cur:
                    assert (await cur.fetchone())["n"] == 1
        finally:
            await db_module.close_db()

    async def test_legacy_points_are_corrected_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STARTING_POINTS", "5000")
        config.reset_settings()
        path = tmp_path / "legacy.db"
        await _legacy_db(path)
        try:
            await _open(path)
            virgin = await db_module.get_user(7)
            active = await db_module.get_user(8)
            assert virgin["points"] == 5000, "el usuario virgen toma el valor configurado"
            assert active["points"] == 1000, "un usuario con historial no se toca"
        finally:
            await db_module.close_db()
            monkeypatch.undo()
            config.reset_settings()

    async def test_the_correction_does_not_repeat(self, tmp_path, monkeypatch):
        path = tmp_path / "legacy.db"
        await _legacy_db(path)
        try:
            await _open(path)
            monkeypatch.setenv("STARTING_POINTS", "9999")
            config.reset_settings()
            await db_module.init_db()  # misma versión: no vuelve a migrar
            assert (await db_module.get_user(7))["points"] == 1000
        finally:
            await db_module.close_db()
            monkeypatch.undo()
            config.reset_settings()


class TestStartingPoints:
    async def test_new_user_gets_the_configured_amount(self, db, monkeypatch):
        """D-04: antes el saldo real de un usuario nuevo seguía siendo 1000."""
        monkeypatch.setenv("STARTING_POINTS", "4321")
        config.reset_settings()
        try:
            await db.upsert_user(101, "nuevo", "Nuevo")
            assert (await db.get_user(101))["points"] == 4321
        finally:
            monkeypatch.undo()
            config.reset_settings()

    async def test_returning_user_keeps_their_balance(self, db):
        await db.upsert_user(102, "veterano", "Veterano")
        await db.adjust_points(102, 250)
        await db.upsert_user(102, "veterano", "Veterano")
        assert (await db.get_user(102))["points"] == 1250


class TestUsernamePreservation:
    """D-05: Telegram puede mandar ``None`` y no debe borrar el @usuario."""

    async def test_none_keeps_the_previous_username(self, db):
        await db.upsert_user(103, "alice", "Alice")
        await db.upsert_user(103, None, "Alice Actualizada")
        user = await db.get_user(103)
        assert user["username"] == "alice"
        assert user["full_name"] == "Alice Actualizada"

    async def test_a_new_username_replaces_the_old_one(self, db):
        await db.upsert_user(104, "vieja", "X")
        await db.upsert_user(104, "nueva", "X")
        assert (await db.get_user(104))["username"] == "nueva"
