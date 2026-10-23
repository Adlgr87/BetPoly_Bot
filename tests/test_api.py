"""HTTP API: Telegram auth, ton_proof linking, market preparation, hardening."""
from __future__ import annotations

import time

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from test_security import signed_init_data
from test_ton_proof import make_proof, make_wallet

from bot import api
from bot.chain import bodies

USER = {"id": 555001, "first_name": "Ada", "last_name": "Lovelace", "username": "ada"}
ADMIN = {"id": 1001, "first_name": "Root", "username": "root"}


@pytest_asyncio.fixture
async def client(db):
    api._session_limiter.reset()
    api._prepare_limiter.reset()
    api._verify_limiter.reset()
    app = api.create_app()
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def session_for(client, user, wallet=None, header=True):
    """Create a session and (optionally) a verified wallet."""
    init_data = signed_init_data(user)
    response = await client.post("/api/session", json={"init_data": init_data})
    assert response.status == 200, await response.text()
    payload = (await response.json())["data"]
    headers = {"X-Session": payload["session"]} if header else {}

    if wallet is not None:
        wallet_data = make_wallet()
        nonce_response = await client.post("/api/wallet/nonce", headers=headers)
        nonce = (await nonce_response.json())["data"]["nonce"]
        proof = make_proof(wallet_data, payload=nonce)
        response = await client.post(
            "/api/wallet/verify",
            headers=headers,
            json={
                "address": wallet_data["address"],
                "network": "-239",
                "state_init": wallet_data["state_init_b64"],
                "proof": proof,
            },
        )
        assert response.status == 200, await response.text()
        payload["wallet"] = (await response.json())["data"]["wallet"]
        payload["wallet_data"] = wallet_data
    return payload, headers


class TestPublic:
    async def test_health(self, client):
        response = await client.get("/api/health")
        assert response.status == 200
        body = await response.json()
        assert body["ok"] is True
        assert body["data"]["mode"] == "hybrid"

    async def test_health_survives_a_missing_database(self, client):
        # No ``init_db`` here: the endpoint must still answer 200.
        response = await client.get("/api/health")
        assert response.status == 200
        assert (await response.json())["data"]["status"] == "ok"

    async def test_config_does_not_leak_secrets(self, client):
        response = await client.get("/api/config")
        data = (await response.json())["data"]
        assert data["escrow_enabled"] is True
        assert data["min_stake_ton"] == "0.1"
        assert "secret" not in str(data).lower()
        assert "test-secret" not in str(data)

    async def test_manifest(self, client):
        response = await client.get("/tonconnect-manifest.json")
        data = await response.json()
        assert data["url"] == "https://betpoly.test"
        assert data["name"]
        assert data["iconUrl"].endswith("/static/icon.png")

    async def test_mini_app_is_served(self, client):
        response = await client.get("/")
        assert response.status == 200
        text = await response.text()
        assert "BetPoly" in text
        assert "Content-Security-Policy" in response.headers
        assert "frame-ancestors" in response.headers["Content-Security-Policy"]

    async def test_static_assets_are_served(self, client):
        for path, content_type in (
            ("/static/app.js", "javascript"),
            ("/static/styles.css", "css"),
            ("/static/icon.png", "image/png"),
        ):
            response = await client.get(path)
            assert response.status == 200, path
            assert content_type in response.headers["Content-Type"], path

    async def test_manifest_icon_exists(self, client):
        import config

        icon = api.WEB_DIR / "static" / "icon.png"
        assert icon.is_file(), "el manifiesto apunta a /static/icon.png"
        assert icon.stat().st_size > 1024
        assert config.get_settings().manifest_url.endswith("/tonconnect-manifest.json")

    async def test_legal_pages(self, client):
        for path in ("/terms", "/privacy"):
            response = await client.get(path)
            assert response.status == 200
            assert "BetPoly" in await response.text()


class TestAuth:
    async def test_session_with_valid_init_data(self, client):
        response = await client.post("/api/session", json={"init_data": signed_init_data(USER)})
        assert response.status == 200
        data = (await response.json())["data"]
        assert data["user_id"] == USER["id"]
        assert data["session"] and data["nonce"]
        assert data["wallet"] is None
        assert data["config"]["escrow_enabled"] is True

    async def test_session_with_invalid_init_data(self, client):
        response = await client.post(
            "/api/session", json={"init_data": signed_init_data(USER).replace("Ada", "Eve")}
        )
        assert response.status == 401
        assert (await response.json())["error"]["code"] == "bad_init_data"

    async def test_session_without_init_data_is_rejected_when_dev_off(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("ALLOW_DEV_SESSION", "0")
        import config

        config.reset_settings()
        response = await client.post("/api/session", json={})
        assert response.status == 401

    async def test_dev_session_allowed_in_local_mode(self, client):
        response = await client.post("/api/session", json={})
        assert response.status == 200
        assert (await response.json())["data"]["dev_mode"] is True

    async def test_me_requires_session(self, client):
        assert (await client.get("/api/me")).status == 401
        assert (await client.get("/api/me", headers={"X-Session": "nope"})).status == 401

    async def test_me_with_session(self, client):
        payload, headers = await session_for(client, USER)
        response = await client.get("/api/me", headers=headers)
        assert response.status == 200
        data = (await response.json())["data"]
        assert data["user"]["user_id"] == USER["id"]
        assert data["user"]["points"] == 1000


class TestWalletVerification:
    async def test_valid_ton_proof_links_wallet(self, client):
        payload, headers = await session_for(client, USER, wallet=True)
        assert payload["wallet"]["address"].startswith("0:")
        response = await client.get("/api/me", headers=headers)
        data = (await response.json())["data"]
        assert data["wallet"]["address"] == payload["wallet"]["address"]

    async def test_same_proof_cannot_be_replayed(self, client):
        payload, headers = await session_for(client, USER)
        nonce = (await (await client.post("/api/wallet/nonce", headers=headers)).json())["data"]["nonce"]
        wallet = make_wallet()
        body = {
            "address": wallet["address"],
            "network": "-239",
            "state_init": wallet["state_init_b64"],
            "proof": make_proof(wallet, payload=nonce),
        }
        first = await client.post("/api/wallet/verify", headers=headers, json=body)
        assert first.status == 200
        second = await client.post("/api/wallet/verify", headers=headers, json=body)
        assert second.status == 409
        assert (await second.json())["error"]["code"] == "nonce_reused"

    async def test_wrong_domain_is_rejected(self, client):
        payload, headers = await session_for(client, USER)
        nonce = (await (await client.post("/api/wallet/nonce", headers=headers)).json())["data"]["nonce"]
        wallet = make_wallet()
        response = await client.post(
            "/api/wallet/verify",
            headers=headers,
            json={
                "address": wallet["address"],
                "state_init": wallet["state_init_b64"],
                "proof": make_proof(wallet, payload=nonce, domain="evil.example"),
            },
        )
        assert response.status == 401
        assert (await response.json())["error"]["code"] == "bad_domain"

    async def test_wallet_cannot_be_linked_to_two_accounts(self, client, monkeypatch):
        wallet = make_wallet()
        first_payload, first_headers = await session_for(client, USER, wallet=wallet)
        address = first_payload["wallet"]["address"]

        other = {"id": 555002, "first_name": "Bob"}
        _, other_headers = await session_for(client, other)
        nonce = (await (await client.post("/api/wallet/nonce", headers=other_headers)).json())["data"]["nonce"]
        response = await client.post(
            "/api/wallet/verify",
            headers=other_headers,
            json={
                "address": wallet["address"],
                "state_init": wallet["state_init_b64"],
                "proof": make_proof(wallet, payload=nonce),
            },
        )
        assert response.status == 409
        assert (await response.json())["error"]["code"] == "wallet_taken"
        assert address == wallet["address"]

    async def test_unlink(self, client):
        _, headers = await session_for(client, USER, wallet=True)
        response = await client.delete("/api/wallet", headers=headers)
        assert response.status == 200
        data = (await (await client.get("/api/me", headers=headers)).json())["data"]
        assert data["wallet"] is None


class TestMarketsApi:
    async def _ready_market(self, db, market_id=1, closes_in=3600):
        await db.enrich_market_from_event(market_id, "0:" + "11" * 32, "¿Gana México?", 2,
                               int(time.time()) + closes_in, fee_bps=200)
        await db.set_market_options(market_id, ["Sí", "No"])

    async def test_list_and_detail(self, client, db):
        await self._ready_market(db)
        response = await client.get("/api/markets")
        markets = (await response.json())["data"]["markets"]
        assert len(markets) == 1
        assert markets[0]["title"] == "¿Gana México?"
        assert markets[0]["options"][0]["label"] == "Sí"

        response = await client.get("/api/markets/1")
        assert response.status == 200
        assert (await response.json())["data"]["market_id"] == 1

        assert (await client.get("/api/markets/99")).status == 404

    async def test_prepare_create_requires_wallet(self, client):
        payload, headers = await session_for(client, USER)
        response = await client.post(
            "/api/markets/prepare",
            headers=headers,
            json={"title": "¿Lloverá mañana?", "options": ["Sí", "No"],
                  "closes_at": int(time.time()) + 7200},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "wallet_required"

    async def test_prepare_create_returns_unsigned_transaction(self, client):
        _, headers = await session_for(client, USER, wallet=True)
        response = await client.post(
            "/api/markets/prepare",
            headers=headers,
            json={"title": "¿Lloverá mañana?", "options": ["Sí", "No"],
                  "closes_at": int(time.time()) + 7200},
        )
        assert response.status == 200, await response.text()
        tx = (await response.json())["data"]
        parsed = bodies.parse_message_body(tx["payload"])
        assert parsed.op == bodies.OP_CREATE_MARKET
        assert tx["amount"] == str(api.markets.CREATE_GAS_NANO)
        assert tx["to"].startswith("EQ")

    async def test_prepare_bet(self, client, db):
        _, headers = await session_for(client, USER, wallet=True)
        await self._ready_market(db)
        response = await client.post(
            "/api/markets/1/bets/prepare",
            headers=headers,
            json={"option": 1, "stake_ton": "2.5"},
        )
        assert response.status == 200, await response.text()
        tx = (await response.json())["data"]
        parsed = bodies.parse_message_body(tx["payload"])
        assert parsed.stake == 2_500_000_000
        assert tx["amount"] == str(2_500_000_000 + api.markets.BET_GAS_NANO)

    async def test_prepare_bet_rejects_bad_amounts(self, client, db):
        _, headers = await session_for(client, USER, wallet=True)
        await self._ready_market(db)
        for stake in ("abc", "-1", "0", "1e9"):
            response = await client.post(
                "/api/markets/1/bets/prepare",
                headers=headers,
                json={"option": 0, "stake_ton": stake},
            )
            assert response.status == 400, stake

    async def test_claim_flow(self, client, db):
        payload, headers = await session_for(client, USER, wallet=True)
        await db.enrich_market_from_event(2, "0:" + "11" * 32, "M", 2, int(time.time()) - 60, fee_bps=200)
        await db.set_market_options(2, ["Sí", "No"])
        await db.record_onchain_bet(2, payload["wallet"]["address"], 0, 1_000_000_000, "tx", 1)
        await db.update_market_settled(2, 0, 6_000_000_000, 4_000_000_000)
        response = await client.post("/api/markets/2/claim/prepare", headers=headers)
        assert response.status == 200, await response.text()
        tx = (await response.json())["data"]
        assert tx["details"]["estimate"] == "1470000000"

    async def test_admin_resolution_is_admin_only(self, client, db):
        await self._ready_market(db, closes_in=-60)
        _, headers = await session_for(client, USER, wallet=True)
        response = await client.post(
            "/api/admin/markets/1/resolve/prepare", headers=headers, json={"winning_option": 0}
        )
        assert response.status == 403

        _, admin_headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/markets/1/resolve/prepare", headers=admin_headers,
            json={"winning_option": 0},
        )
        assert response.status == 200, await response.text()
        tx = (await response.json())["data"]
        assert bodies.parse_message_body(tx["payload"]).winning_option == 0

    async def test_admin_cannot_resolve_open_market(self, client, db):
        await self._ready_market(db, closes_in=3600)
        _, admin_headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/markets/1/resolve/prepare", headers=admin_headers,
            json={"winning_option": 0},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "market_still_open"

    async def test_indexer_status(self, client):
        _, headers = await session_for(client, ADMIN)
        response = await client.get("/api/indexer", headers=headers)
        assert response.status == 200
        data = (await response.json())["data"]
        assert data["is_admin"] is True
        assert "indexer" in data


class TestAdminContract:
    """Fase 2: endpoints del contrato (el backend prepara, la wallet firma)."""

    @staticmethod
    def _tx(kind="set_fee"):
        from bot.markets import PreparedTx

        return PreparedTx(
            to="EQ" + "A" * 46,
            amount=50_000_000,
            payload="te6ccgEBAQEAAgAAAA==",
            kind=kind,
            valid_until=int(time.time()) + 300,
            summary="Resumen",
            deeplink="https://app.tonkeeper.com/transfer/EQ",
            details={"ok": True},
        )

    @staticmethod
    def _state():
        from bot.chain.escrow_state import EscrowState

        return EscrowState(
            address="0:" + "ab" * 32,
            fee_bps=250,
            min_stake_nano=100_000_000,
            paused=False,
            total_locked_nano=1000,
            fees_nano=400,
            market_count=3,
            balance_nano=1400,
            admin="0:" + "cd" * 32,
        )

    async def test_state_is_admin_only(self, client, db, monkeypatch):
        monkeypatch.setattr(api.markets, "escrow_state", lambda **kw: _async(self._state()))
        _, headers = await session_for(client, USER)
        response = await client.get("/api/admin/contract", headers=headers)
        assert response.status == 403

    async def test_state_returns_the_snapshot(self, client, db, monkeypatch):
        monkeypatch.setattr(api.markets, "escrow_state", lambda **kw: _async(self._state()))
        _, headers = await session_for(client, ADMIN)
        response = await client.get("/api/admin/contract", headers=headers)
        assert response.status == 200, await response.text()
        data = (await response.json())["data"]
        assert data["fee_bps"] == 250
        assert data["fee_percent"] == "2.50"
        assert data["max_withdrawable_nano"] == "400"
        assert data["is_paused"] is False
        assert data["admin_matches_wallet"] is None  # admin sin wallet vinculada

    async def test_withdraw_prepares_the_amount(self, client, db, monkeypatch):
        captured = {}

        async def fake(user_id, amount):
            captured.update({"user_id": user_id, "amount": amount})
            return self._tx("withdraw_fees")

        monkeypatch.setattr(api.markets, "prepare_withdraw_fees", fake)
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/withdraw/prepare", headers=headers, json={"amount": "1.5"}
        )
        assert response.status == 200, await response.text()
        assert captured == {"user_id": ADMIN["id"], "amount": 1_500_000_000}
        assert (await response.json())["data"]["kind"] == "withdraw_fees"

    async def test_withdraw_rejects_a_bad_amount(self, client, db):
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/withdraw/prepare", headers=headers, json={"amount": "nada"}
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "bad_amount"

    async def test_withdraw_is_admin_only(self, client, db):
        _, headers = await session_for(client, USER)
        response = await client.post(
            "/api/admin/contract/withdraw/prepare", headers=headers, json={"amount": "1"}
        )
        assert response.status == 403

    async def test_set_fee_accepts_percent(self, client, db, monkeypatch):
        captured = {}

        async def fake(user_id, fee_bps):
            captured["fee_bps"] = fee_bps
            return self._tx("set_fee")

        monkeypatch.setattr(api.markets, "prepare_set_fee", fake)
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/set-fee/prepare", headers=headers, json={"percent": "2,5"}
        )
        assert response.status == 200, await response.text()
        assert captured["fee_bps"] == 250

        response = await client.post(
            "/api/admin/contract/set-fee/prepare", headers=headers, json={"fee_bps": 400}
        )
        assert response.status == 200
        assert captured["fee_bps"] == 400

    async def test_set_fee_rejects_an_invalid_percent(self, client, db):
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/set-fee/prepare", headers=headers, json={"percent": "25"}
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "bad_fee"

    async def test_set_paused_requires_a_boolean(self, client, db):
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/set-paused/prepare", headers=headers, json={"paused": "sí"}
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "bad_flag"

    async def test_set_paused_prepares_the_flag(self, client, db, monkeypatch):
        captured = {}

        async def fake(user_id, paused):
            captured["paused"] = paused
            return self._tx("set_paused")

        monkeypatch.setattr(api.markets, "prepare_set_paused", fake)
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/set-paused/prepare", headers=headers, json={"paused": True}
        )
        assert response.status == 200, await response.text()
        assert captured["paused"] is True

    async def test_change_admin_passes_the_address(self, client, db, monkeypatch):
        captured = {}

        async def fake(user_id, address):
            captured["address"] = address
            return self._tx("change_admin")

        monkeypatch.setattr(api.markets, "prepare_change_admin", fake)
        _, headers = await session_for(client, ADMIN)
        address = "0:" + "ee" * 32
        response = await client.post(
            "/api/admin/contract/change-admin/prepare", headers=headers, json={"address": address}
        )
        assert response.status == 200, await response.text()
        assert captured["address"] == address

    async def test_domain_errors_map_to_400_and_403(self, client, db, monkeypatch):
        from bot.markets import MarketError

        async def forbidden(user_id, paused):
            raise MarketError("forbidden", "Solo un administrador.")

        async def invalid(user_id, amount):
            raise MarketError("amount_too_high", "Solo hay 1 TON.")

        monkeypatch.setattr(api.markets, "prepare_set_paused", forbidden)
        monkeypatch.setattr(api.markets, "prepare_withdraw_fees", invalid)
        _, headers = await session_for(client, ADMIN)

        response = await client.post(
            "/api/admin/contract/set-paused/prepare", headers=headers, json={"paused": True}
        )
        assert response.status == 403

        response = await client.post(
            "/api/admin/contract/withdraw/prepare", headers=headers, json={"amount": "5"}
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "amount_too_high"

    async def test_no_endpoint_returns_a_signed_transaction(self, client, db, monkeypatch):
        """El backend prepara; firmar es siempre de la wallet (invariante P1)."""

        async def fake(user_id, amount):
            return self._tx("withdraw_fees")

        monkeypatch.setattr(api.markets, "prepare_withdraw_fees", fake)
        _, headers = await session_for(client, ADMIN)
        response = await client.post(
            "/api/admin/contract/withdraw/prepare", headers=headers, json={"amount": "1"}
        )
        assert response.status == 200
        data = (await response.json())["data"]
        assert set(data) == {
            "to", "amount", "amount_ton", "payload", "kind", "valid_until",
            "summary", "deeplink", "details",
        }
        serialized = str(data).lower()
        for forbidden in ("private_key", "secret", "seed", "signed_boc", "signature"):
            assert forbidden not in serialized

    async def test_chain_failure_is_reported_as_unavailable(self, client, db, monkeypatch):
        from bot.markets import MarketError

        async def down(**kwargs):
            raise MarketError("chain_unavailable", "La cadena no respondió.")

        monkeypatch.setattr(api.markets, "escrow_state", down)
        _, headers = await session_for(client, ADMIN)
        response = await client.get("/api/admin/contract", headers=headers)
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "chain_unavailable"


async def _async(value):
    return value


class TestHardening:
    async def test_invalid_json(self, client):
        response = await client.post(
            "/api/session", data="not json", headers={"Content-Type": "application/json"}
        )
        assert response.status == 400

    async def test_rate_limited_sessions(self, client):
        api._session_limiter.reset()
        statuses = []
        for _ in range(35):
            response = await client.post("/api/session", json={})
            statuses.append(response.status)
        assert 429 in statuses

    async def test_session_tampering(self, client):
        payload, _ = await session_for(client, USER)
        token = payload["session"]
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        response = await client.get("/api/me", headers={"X-Session": tampered})
        assert response.status == 401

    async def test_error_payloads_are_structured(self, client):
        response = await client.get("/api/me")
        body = await response.json()
        assert body["ok"] is False
        assert body["error"]["code"]
        assert body["error"]["message"]
