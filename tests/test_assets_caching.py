"""Fase 6: activos y caché (R-06, R-08, R-09).

Tres propiedades que se rompen solas si nadie las vigila:

* el JavaScript y el CSS salen comprimidos y con caché inmutable cuando la URL
  lleva la huella del contenido;
* ``/api/markets`` contesta ``304`` a un cliente que ya tiene esa versión;
* ningún activo del arranque se pasa del presupuesto de 350 KB comprimidos.
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from bot import api
from scripts import build_icon

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"

#: Presupuesto del plan (§9): lo que el móvil descarga antes de pintar nada.
STARTUP_BUDGET_BYTES = 350 * 1024
#: R-06: el icono es el activo más pesado y tiene tope propio.
ICON_BUDGET_BYTES = 80 * 1024
#: F-12: bundle de terceros vendorizado (minificado, sin recompilar). Se mide
#: comprimido, como lo descarga el móvil, no como ocupa en el repositorio.
VENDORED = {"tonconnect-ui.min.js"}


@pytest_asyncio.fixture
async def client(db):
    api.reset_asset_cache()
    async with TestClient(TestServer(api.create_app())) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def raw_client(db):
    """Cliente que **no** descomprime: así se ve la cabecera real que enviamos."""
    api.reset_asset_cache()
    async with TestClient(TestServer(api.create_app()), auto_decompress=False) as test_client:
        yield test_client


class TestVersionedAssets:
    """R-09: versión en la URL → caché de un año; gzip en memoria."""

    async def test_index_versions_every_static_reference(self, client):
        response = await client.get("/")
        assert response.status == 200
        assert "no-store" in response.headers["Cache-Control"]
        html = await response.text()
        assert "/static/app.js?v=" in html
        assert "/static/styles.css?v=" in html
        assert '"/static/app.js"' not in html

    async def test_versioned_asset_is_immutable_and_compressed(self, raw_client):
        html = await (await raw_client.get("/")).text()
        version = html.split("/static/app.js?v=")[1].split('"')[0]
        response = await raw_client.get(
            f"/static/app.js?v={version}", headers={"Accept-Encoding": "gzip"}
        )
        assert response.status == 200
        assert "immutable" in response.headers["Cache-Control"]
        assert "max-age=31536000" in response.headers["Cache-Control"]
        assert response.headers["Content-Encoding"] == "gzip"
        body = gzip.decompress(await response.read()).decode("utf-8")
        assert body == (STATIC / "app.js").read_text(encoding="utf-8")

    async def test_unversioned_asset_is_revalidated(self, client):
        response = await client.get("/static/styles.css")
        assert response.status == 200
        assert "immutable" not in response.headers["Cache-Control"]
        assert "no-cache" in response.headers["Cache-Control"]

    async def test_second_request_hits_the_etag_and_gets_304(self, client):
        first = await client.get("/static/app.js")
        etag = first.headers["ETag"]
        second = await client.get("/static/app.js", headers={"If-None-Match": etag})
        assert second.status == 304
        assert await second.read() == b""

    async def test_images_are_not_gzipped(self, raw_client):
        response = await raw_client.get("/static/icon.png", headers={"Accept-Encoding": "gzip"})
        assert response.status == 200
        assert "Content-Encoding" not in response.headers
        assert response.headers["Content-Type"] == "image/png"

    async def test_unknown_and_traversal_paths_are_rejected(self, client):
        assert (await client.get("/static/nope.js")).status == 404
        assert (await client.get("/static/../index.html")).status == 404
        assert (await client.get("/static/%2e%2e%2findex.html")).status == 404
        assert (await client.get("/static/README.md")).status == 404


class TestMarketsEtag:
    """R-08: el sondeo se abarata a ``304`` cuando nada cambió."""

    async def test_second_get_returns_304(self, client):
        first = await client.get("/api/markets", params={"status": "open"})
        assert first.status == 200
        etag = first.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')
        assert "no-cache" in first.headers["Cache-Control"]

        second = await client.get(
            "/api/markets", params={"status": "open"}, headers={"If-None-Match": etag}
        )
        assert second.status == 304
        assert second.headers["ETag"] == etag
        assert await second.read() == b""

    async def test_etag_changes_when_the_payload_changes(self, client, db):
        first = await client.get("/api/markets", params={"status": "open"})
        etag = first.headers["ETag"]
        await db.enrich_market_from_event(
            1, "0:" + "22" * 32, "Nuevo mercado", 2, 4_000_000_000, fee_bps=200
        )
        second = await client.get(
            "/api/markets", params={"status": "open"}, headers={"If-None-Match": etag}
        )
        assert second.status == 200
        assert second.headers["ETag"] != etag

    async def test_other_queries_are_separate_entries(self, client, db):
        await db.enrich_market_from_event(
            1, "0:" + "33" * 32, "Abierto", 2, 4_000_000_000, fee_bps=200
        )
        opened = await client.get("/api/markets", params={"status": "open"})
        assert opened.status == 200
        etag = opened.headers["ETag"]
        other = await client.get(
            "/api/markets", params={"status": "resolved"}, headers={"If-None-Match": etag}
        )
        assert other.status == 200
        assert other.headers["ETag"] != etag


class TestAssetBudget:
    """R-06: el icono y el arranque entero caben en el presupuesto."""

    def test_icon_fits_its_budget(self):
        icon = STATIC / "icon.png"
        assert icon.stat().st_size <= ICON_BUDGET_BYTES, (
            f"icon.png pesa {icon.stat().st_size} B; el tope es {ICON_BUDGET_BYTES}. "
            "Regenéralo con `python scripts/build_icon.py`."
        )

    def test_no_first_party_asset_is_absurdly_large(self):
        for path in STATIC.iterdir():
            if path.is_file() and path.name not in VENDORED:
                assert path.stat().st_size <= STARTUP_BUDGET_BYTES, path.name

    def test_startup_payload_is_under_budget(self):
        files = [
            ROOT / "web" / "index.html",
            STATIC / "styles.css",
            STATIC / "app.js",
            STATIC / "icon.png",
            STATIC / "tonconnect-ui.min.js",  # 528 KB → ~155 KB gzip
        ]
        total = sum(len(gzip.compress(path.read_bytes(), 9)) for path in files)
        assert total <= STARTUP_BUDGET_BYTES, (
            f"el arranque comprimido pesa {total / 1024:.1f} KB; el tope es "
            f"{STARTUP_BUDGET_BYTES / 1024:.0f} KB"
        )

    def test_icon_builder_refuses_a_bigger_target(self, tmp_path, monkeypatch):
        """El script falla si el resultado se pasa del presupuesto."""

        class FakeImage:
            LANCZOS = 1
            MEDIANCUT = 0

            def open(self, path):
                return self

            def convert(self, mode):
                return self

            def resize(self, size, method):
                assert size == (build_icon.SIZE, build_icon.SIZE)
                return self

            def quantize(self, colors, method):
                assert colors == build_icon.COLORS
                return self

            def save(self, target, optimize=True):
                target.write_bytes(b"x" * (build_icon.BUDGET_BYTES + 1))

        source = tmp_path / "original.png"
        source.write_bytes(b"png")
        target = tmp_path / "icon.png"
        code = build_icon.build(source, target, image_module=FakeImage())
        assert code == 1
        assert target.stat().st_size > build_icon.BUDGET_BYTES

    def test_icon_builder_reports_a_missing_source(self, tmp_path):
        class NeverUsed:
            LANCZOS = 1
            MEDIANCUT = 0

        assert build_icon.build(
            tmp_path / "nada.png", tmp_path / "out.png", image_module=NeverUsed()
        ) == 2

    def test_icon_builder_never_crashes_without_a_source(self, tmp_path):
        """Sin fuente (o sin Pillow) sale con código 2 y un mensaje, no con traza."""
        assert build_icon.build(tmp_path / "x.png", tmp_path / "y.png") == 2

    def test_app_js_has_no_fixed_interval_polling(self):
        """R-08: el sondeo es adaptativo; un ``setInterval`` fijo vuelve a gastar."""
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "setInterval" not in app_js
        delays = re.findall(r"\[15000, 30000, 60000\]", app_js)
        assert delays, "se esperaban los tres escalones 15/30/60 s del backoff"
        assert "visibilitychange" in app_js
