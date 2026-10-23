"""El preview local debe sembrar datos reales sobre el esquema actual.

Cubre `scripts/dev_server.py`, que hasta ahora no ejecutaba ningún test y por
eso seguía llamando a una función de base de datos ya retirada.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dev_server():
    spec = importlib.util.spec_from_file_location("dev_server", ROOT / "scripts" / "dev_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def test_seed_creates_markets_with_pools(db):
    dev_server = _load_dev_server()
    await dev_server.seed()

    market = await db.get_market(1)
    assert market is not None
    assert market["fee_bps"] == 200
    assert [o["label"] for o in market["options"]] == ["Sí", "No"]

    async with db.connection() as conn:
        async with conn.execute("SELECT COUNT(*) AS n FROM onchain_bets WHERE market_id = 1") as cur:
            assert (await cur.fetchone())["n"] == 2


async def test_seed_is_idempotent(db):
    dev_server = _load_dev_server()
    await dev_server.seed()
    await dev_server.seed()
    async with db.connection() as conn:
        async with conn.execute("SELECT COUNT(*) AS n FROM markets") as cur:
            assert (await cur.fetchone())["n"] == len(dev_server.DEMO_MARKETS)
