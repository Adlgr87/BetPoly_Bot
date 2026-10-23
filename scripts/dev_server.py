#!/usr/bin/env python
"""Mini App local preview with seeded demo data.

Starts only the HTTP layer (no Telegram polling, no indexer, no TON access) and
pre-populates the *mirror* tables with a few markets so the UI has something to
render.  Nothing here touches money or the chain: it is a UI sandbox.

    python scripts/dev_server.py                       # http://localhost:8080
    python scripts/dev_server.py --port 9000 --no-seed
    python scripts/dev_server.py --public-base-url https://abc.e2b.app

TON Connect exige HTTPS: en local el manifest es de mentira, pero con
`--public-base-url` puedes apuntarlo al túnel que estés usando y probar con
wallets reales. Las variables `WEB_HOST`, `WEB_PORT`, `DATABASE_PATH` y
`PUBLIC_BASE_URL` siguen funcionando como valores por defecto.

Never point this at a production database.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Sequence

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

import config  # noqa: E402
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
        await database.enrich_market_from_event(
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Opciones de línea de comandos (F-09: antes todo iba por variables de entorno)."""
    parser = argparse.ArgumentParser(
        prog="dev_server.py",
        description="Mini App de demostración: solo HTTP, con datos ficticios.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("WEB_HOST", "0.0.0.0"),
        help="interfaz donde escuchar (por defecto 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEB_PORT", "8080")),
        help="puerto HTTP (por defecto 8080 o WEB_PORT)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="ruta de la base de datos de demostración (por defecto DATABASE_PATH)",
    )
    parser.add_argument(
        "--public-base-url",
        default=None,
        help="URL pública para el manifest de TON Connect (debe ser https en producción)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="no siembra mercados de ejemplo (usa los que ya haya)",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def apply_args(args: argparse.Namespace) -> None:
    """Vuelca las opciones al entorno **antes** de crear la app."""
    if args.db:
        os.environ["DATABASE_PATH"] = args.db
        database.configure_database(args.db)
    if args.public_base_url:
        os.environ["PUBLIC_BASE_URL"] = args.public_base_url
        config.reset_settings()


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    apply_args(args)
    await database.init_db()
    if not args.no_seed:
        await seed()

    port = args.port
    host = args.host
    app = api.create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    base = config.get_settings().public_base_url.rstrip("/")
    print(f"Mini App de demostración en {base} (escuchando en {host}:{port}, datos ficticios)")
    if not base.startswith("https://"):
        print("ℹ️  Manifest http://: TON Connect solo lo acepta por HTTPS en wallets reales.")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await database.close_db()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(sys.argv[1:]))
