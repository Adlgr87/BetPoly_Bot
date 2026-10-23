"""Vinculación de wallets: liberar direcciones y migrar el esquema antiguo (S-07)."""
from __future__ import annotations

import aiosqlite
import pytest

from bot import database as db_module

LEGACY_DDL = """
CREATE TABLE wallet_links (
    user_id     INTEGER PRIMARY KEY,
    address     TEXT NOT NULL UNIQUE,
    network     TEXT NOT NULL DEFAULT 'ton',
    public_key  TEXT,
    verified_at TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
"""


class TestWalletRelink:
    async def test_unlink_releases_the_address(self, db):
        addr = "0:" + "aa" * 32
        await db.link_wallet(1, addr, public_key="k1")
        assert await db.get_wallet_owner(addr) is not None

        await db.unlink_wallet(1)
        assert await db.get_wallet(1) is None
        assert await db.get_wallet_owner(addr) is None, "la dirección debe quedar libre"

        await db.link_wallet(2, addr, public_key="k2")
        owner = await db.get_wallet_owner(addr)
        assert owner is not None and int(owner["user_id"]) == 2

    async def test_history_is_kept_after_relinking(self, db):
        first, second = "0:" + "a1" * 32, "0:" + "a2" * 32
        await db.link_wallet(1, first)
        await db.link_wallet(1, second)
        async with db.connection() as conn:
            async with conn.execute(
                "SELECT address, is_active FROM wallet_links WHERE user_id = 1 ORDER BY id"
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        assert [(r["address"], r["is_active"]) for r in rows] == [(first, 0), (second, 1)]
        current = await db.get_wallet(1)
        assert current is not None and current["address"] == second

    async def test_one_active_wallet_per_account(self, db):
        await db.link_wallet(1, "0:" + "a1" * 32)
        await db.link_wallet(1, "0:" + "a2" * 32)
        async with db.connection() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM wallet_links WHERE user_id = 1 AND is_active = 1"
            ) as cur:
                assert (await cur.fetchone())["n"] == 1

    async def test_an_active_address_cannot_belong_to_two_accounts(self, db):
        addr = "0:" + "bb" * 32
        await db.link_wallet(1, addr)
        with pytest.raises(db.WalletConflict):
            await db.link_wallet(2, addr)
        owner = await db.get_wallet_owner(addr)
        assert owner is not None and int(owner["user_id"]) == 1


class TestLegacyMigration:
    async def test_legacy_schema_is_migrated_without_data_loss(self, tmp_path):
        path = tmp_path / "legacy.db"
        async with aiosqlite.connect(path) as conn:
            await conn.executescript(LEGACY_DDL)
            await conn.execute(
                "INSERT INTO wallet_links VALUES (7, '0:deadbeef', 'ton', NULL,"
                " '2026-01-01T00:00:00+00:00', 0)"
            )
            await conn.commit()

        db_module.configure_database(str(path))
        await db_module.init_db()
        try:
            async with db_module.connection() as conn:
                async with conn.execute("PRAGMA table_info(wallet_links)") as cur:
                    columns = [row["name"] for row in await cur.fetchall()]
                assert "id" in columns, "la migración debe añadir la clave subrogada"
                async with conn.execute("SELECT COUNT(*) AS n FROM wallet_links") as cur:
                    assert (await cur.fetchone())["n"] == 1, "no se pierde el historial"
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='wallet_links_legacy'"
                ) as cur:
                    assert await cur.fetchone() is None, "la tabla temporal debe eliminarse"

            await db_module.link_wallet(8, "0:deadbeef")
            owner = await db_module.get_wallet_owner("0:deadbeef")
            assert owner is not None and int(owner["user_id"]) == 8
        finally:
            await db_module.close_db()
            db_module.configure_database(":memory:")
