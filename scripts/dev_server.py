#!/usr/bin/env python
"""Mini App local preview with seeded demo data.

Starts only the HTTP layer (no Telegram polling, no indexer, no TON access) and
pre-populates the *mirror* tables with a few markets so the UI has something to
render.  Nothing here touches money or the chain: it is a UI sandbox.

    python scripts/dev_server.py            # http://localhost:8080
    PORT=9000 python scripts/dev_server.py

Never point this at a production database.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Defaults must be set before ``config`` is imported anywhere.
os.environ.setdefault("BET_MODE", "hybrid")
os.environ.setdefault("ESCROW_ADDRESS", "0:" + "ab" * 32)
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")
os.environ.setdefault("ALLOW_DEV_SESSION", "1")
os.environ.setdefault("SECRET_KEY", "dev-preview-secret-not-for-production")
os.environ.setdefault("DATABASE_PATH", str(ROOT / "preview.db"))
os.environ.setdefault("INDEXER_ENABLED", "0")
os.environ.setdefault("WEB_HOST", "0.0.0.0")
os.environ.setdefault("WEB_PORT", "8080")
os.environ.setdefault(
    "FRAME_ANCESTORS",
    "'self' https://*.telegram.org https://web.telegram.org https://*.e2b.app",
)

from aiohttp import web  # noqa: E402

from bot import api, database  # noqa: E402

DEMO_MARKETS = [
    {
        "market_id": 1,
        "title": "¿Ganará México la Copa Oro?",
        "options": ["Sí", "No"],
        "closes_in": 3600 * 26,
        "pools": [12_500_000_000, 7_500_000_000],
    },
    {
        "market_id": 2,
        "title": "¿Lloverá en Monterrey este viernes?",
        "options": ["Sí", "No", "Solo por la tarde"],
        "closes_in": 3600 * 8,
        "pools": [3_000_000_000, 9_000_000_000, 1_500_000_000],
    },
    {
        "market_id": 3,
        "title": "¿Bitcoin cierra el mes sobre 100k?",
        "options": ["Sí", "No"],
        "closes_in": 3600 * 70,
        "pools": [21_000_000_000, 4_000_000_000],
    },
]


async def seed() -> None:
    creator = "0:" + "11" * 32
    for spec in DEMO_MARKETS:
        existing = await database.get_market(spec["market_id"])
        if existing:
            continue
        await database.upsert_market(
            spec["market_id"],
            creator,
            spec["title"],
            len(spec["options"]),
            int(time.time()) + spec["closes_in"],
            fee_bps=200,
        )
        await database.set_market_options(spec["market_id"], spec["options"])
        for index, pool in enumerate(spec["pools"]):
            pseudo_wallet = "0:" + f"{index + 1:02x}" * 32
            await database.record_onchain_bet(
                spec["market_id"], pseudo_wallet, index, pool, f"demo-{index}", index + 1
            )


async def main() -> None:
    await database.init_db()
    await seed()

    port = int(os.environ["WEB_PORT"])
    host = os.environ["WEB_HOST"]
    app = api.create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Mini App de demostración en http://{host}:{port} (datos ficticios)")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await database.close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
