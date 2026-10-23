"""Activos web: CSP real y cero manejadores inline (Fase 3.2, S-05).

La CSP que publica el servidor no permite ``unsafe-inline`` para scripts, así
que cualquier ``onclick="..."`` es un botón muerto. Estas pruebas son un
guardarraíl estático (no hay navegador en CI) más una comprobación de que la
cabecera que se envía de verdad coincide con esa política.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from bot import api

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
APP_JS = ROOT / "web" / "static" / "app.js"

#: ``onclick=``, ``onerror=``… pero no ``data-on...`` ni ``addEventListener``.
INLINE_HANDLER = re.compile(r"(?<![\w-])on[a-z]+\s*=", re.IGNORECASE)


@pytest_asyncio.fixture
async def web_client(db):  # ``db`` aísla y cierra la conexión compartida
    async with TestClient(TestServer(api.create_app())) as client:
        yield client


class TestNoInlineHandlers:
    def test_index_has_no_inline_handlers(self):
        assert not INLINE_HANDLER.findall(INDEX.read_text(encoding="utf-8"))

    def test_app_js_has_no_inline_handlers(self):
        offenders = INLINE_HANDLER.findall(APP_JS.read_text(encoding="utf-8"))
        assert not offenders, f"manejadores inline bloqueados por la CSP: {offenders}"

    def test_every_script_tag_loads_a_file(self):
        html = INDEX.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, f"script inline bloqueado por la CSP: {tag}"

    def test_the_retry_button_uses_event_delegation(self):
        source = APP_JS.read_text(encoding="utf-8")
        assert 'data-reload="1"' in source
        assert "closest('[data-reload]')" in source


class TestCspHeader:
    async def test_csp_forbids_inline_scripts(self, web_client):
        response = await web_client.get("/")
        assert response.status == 200
        csp = response.headers["Content-Security-Policy"]
        script_src = next(
            part.strip() for part in csp.split(";") if part.strip().startswith("script-src")
        )
        assert "'unsafe-inline'" not in script_src
        assert "'self'" in script_src
        assert "frame-ancestors" in csp
        assert "default-src 'self'" in csp

    async def test_security_headers_are_present(self, web_client):
        response = await web_client.get("/")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "same-origin"

    async def test_api_responses_carry_the_same_csp(self, web_client):
        response = await web_client.get("/api/health")
        assert response.status == 200
        assert "script-src" in response.headers["Content-Security-Policy"]

    async def test_static_js_is_served(self, web_client):
        response = await web_client.get("/static/app.js")
        assert response.status == 200
        assert "data-reload" in await response.text()
