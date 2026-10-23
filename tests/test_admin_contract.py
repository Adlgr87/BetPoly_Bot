"""Fase 2: administración del contrato escrow (S-03 / D-03).

Cubre las tres piezas nuevas, cada una con su prueba:

* ``runGetMethod`` y el parseo del stack de TON Center,
* ``EscrowState`` + caché con TTL y comportamiento *stale-while-error*,
* los cuatro constructores de payload de administración expuestos por
  ``bot.markets`` (el backend prepara, la wallet firma: nunca al revés).
"""
from __future__ import annotations

import pytest

import config
from bot import markets
from bot.chain import bodies
from bot.chain import escrow_state as escrow_state_module
from bot.chain.addresses import to_friendly
from bot.chain.escrow_state import EscrowState, EscrowStateCache, read_escrow_state
from bot.chain.toncenter import TonApiError, TonCenterClient, _parse_stack_entry
from bot.parsing import parse_fee_bps, parse_nano

ADMIN_ID = 1001
OTHER_ID = 1
#: ``ESCROW_ADMIN_ADDRESS`` del conftest: la única wallet que puede firmar.
ADMIN_WALLET = config.get_settings().escrow_admin_address
OTHER_WALLET = "0:" + "11" * 32


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

class FakeChain:
    """Cliente mínimo que responde como el contrato."""

    def __init__(self, values=None, *, fail=None, balance=5_000_000_000):
        self.values = values or {
            "get_fee_bps": [250],
            "get_min_stake": [100_000_000],
            "get_paused": [0],
            "get_total_locked": [7_000_000_000],
            "get_fees": [2_500_000_000],
            "get_market_count": [4],
        }
        self.fail = fail
        self.balance = balance
        self.calls = 0

    async def run_get_method(self, address, method, stack=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.values[method]

    async def get_account_state(self, address):
        return {"balance": str(self.balance)}


async def _link(db, user_id, address):
    await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
    await db.link_wallet(user_id, address, public_key="ab" * 32)


async def _admin_ready(db):
    await _link(db, ADMIN_ID, ADMIN_WALLET)


# ---------------------------------------------------------------------------
# parseo de importes y comisiones
# ---------------------------------------------------------------------------

class TestParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1.5", 1_500_000_000), ("1,5", 1_500_000_000), ("0.000000001", 1), ("2", 2_000_000_000)],
    )
    def test_parse_nano_accepts_ton_text(self, value, expected):
        assert parse_nano(value) == expected

    def test_parse_nano_passes_integers_through(self):
        assert parse_nano(1_500_000_000) == 1_500_000_000

    @pytest.mark.parametrize("value", ["", "abc", "-1", "1.1234567891", None, True])
    def test_parse_nano_rejects_garbage(self, value):
        with pytest.raises(markets.MarketError) as exc:
            parse_nano(value)
        assert exc.value.code == "bad_amount"

    @pytest.mark.parametrize(
        ("value", "expected"), [("2", 200), ("2,5", 250), ("10", 1000), ("0", 0), ("1.25 %", 125)]
    )
    def test_parse_fee_bps(self, value, expected):
        assert parse_fee_bps(value) == expected

    @pytest.mark.parametrize("value", ["", "abc", "10.5", "-1", "12"])
    def test_parse_fee_bps_rejects_out_of_range(self, value):
        with pytest.raises(markets.MarketError) as exc:
            parse_fee_bps(value)
        assert exc.value.code == "bad_fee"


# ---------------------------------------------------------------------------
# runGetMethod
# ---------------------------------------------------------------------------

class TestRunGetMethod:
    def test_stack_entries_are_normalised(self):
        assert _parse_stack_entry(["num", "0x2a"]) == 42
        assert _parse_stack_entry(["num", "17"]) == 17
        assert _parse_stack_entry(["cell", "te6..."]) == {"type": "cell", "value": "te6..."}

    async def test_posts_address_method_and_stack(self, monkeypatch):
        client = TonCenterClient(base_url="https://ton.test", api_key="k")
        captured = {}

        async def fake_request(method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return {"result": {"exit_code": 0, "stack": [["num", "0x0a"]]}}

        monkeypatch.setattr(client, "_request", fake_request)
        stack = await client.run_get_method("0:" + "ab" * 32, "get_fees", [["num", "0x1"]])
        assert stack == [10]
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/runGetMethod")
        assert captured["json"] == {
            "address": "0:" + "ab" * 32,
            "method": "get_fees",
            "stack": [["num", "0x1"]],
        }

    async def test_raises_on_non_zero_exit_code(self, monkeypatch):
        client = TonCenterClient(base_url="https://ton.test", api_key="k")

        async def fake_request(method, url, **kwargs):
            return {"result": {"exit_code": 11, "stack": []}}

        monkeypatch.setattr(client, "_request", fake_request)
        with pytest.raises(TonApiError):
            await client.run_get_method("0:" + "ab" * 32, "get_fees")


# ---------------------------------------------------------------------------
# EscrowState + caché
# ---------------------------------------------------------------------------

class TestEscrowState:
    async def test_reads_every_getter(self):
        state = await read_escrow_state(
            FakeChain(), "0:" + "ab" * 32, admin="0:" + "cd" * 32, now=1000
        )
        assert state.fee_bps == 250
        assert state.min_stake_nano == 100_000_000
        assert state.paused is False
        assert state.total_locked_nano == 7_000_000_000
        assert state.fees_nano == 2_500_000_000
        assert state.market_count == 4
        assert state.balance_nano == 5_000_000_000
        assert state.admin == "0:" + "cd" * 32
        assert state.read_at == 1000
        assert state.max_withdrawable_nano == 2_500_000_000
        assert state.stale is False

    async def test_paused_flag_is_boolean(self):
        chain = FakeChain(values={
            "get_fee_bps": [0], "get_min_stake": [0], "get_paused": [1],
            "get_total_locked": [0], "get_fees": [0], "get_market_count": [0],
        })
        state = await read_escrow_state(chain, "0:" + "ab" * 32)
        assert state.is_paused is True

    async def test_balance_failure_does_not_break_the_snapshot(self):
        class NoBalance(FakeChain):
            async def get_account_state(self, address):
                raise TonApiError("boom")

        state = await read_escrow_state(NoBalance(), "0:" + "ab" * 32)
        assert state.balance_nano is None
        assert state.fee_bps == 250

    async def test_as_dict_exposes_formatted_fee(self):
        state = EscrowState(address="0:" + "ab" * 32, fee_bps=250, fees_nano=10)
        payload = state.as_dict()
        assert payload["fee_percent"] == "2.50"
        assert payload["max_withdrawable_nano"] == "10"

    async def test_cache_hits_within_ttl(self):
        chain = FakeChain()
        cache = EscrowStateCache(ttl_seconds=30)
        await cache.get(chain, "0:" + "ab" * 32, now=100)
        await cache.get(chain, "0:" + "ab" * 32, now=120)
        assert chain.calls == 6  # seis getters, una sola lectura

    async def test_cache_refreshes_after_ttl_and_with_force(self):
        chain = FakeChain()
        cache = EscrowStateCache(ttl_seconds=30)
        await cache.get(chain, "0:" + "ab" * 32, now=100)
        await cache.get(chain, "0:" + "ab" * 32, now=131)
        assert chain.calls == 12
        await cache.get(chain, "0:" + "ab" * 32, now=131, force=True)
        assert chain.calls == 18

    async def test_error_without_previous_snapshot_raises(self):
        cache = EscrowStateCache()
        with pytest.raises(TonApiError):
            await cache.get(FakeChain(fail=TonApiError("down")), "0:" + "ab" * 32)

    async def test_error_keeps_serving_the_last_snapshot(self):
        cache = EscrowStateCache(ttl_seconds=30)
        address = "0:" + "ab" * 32
        await cache.get(FakeChain(), address, now=100)
        stale = await cache.get(FakeChain(fail=TonApiError("down")), address, now=200)
        assert stale.stale is True
        assert stale.error
        assert stale.fee_bps == 250

    async def test_reset_forgets_everything(self):
        cache = EscrowStateCache()
        chain = FakeChain()
        await cache.get(chain, "0:" + "ab" * 32, now=100)
        cache.reset()
        await cache.get(chain, "0:" + "ab" * 32, now=101)
        assert chain.calls == 12


# ---------------------------------------------------------------------------
# servicios de administración (bot.markets)
# ---------------------------------------------------------------------------

class TestAdminGuard:
    async def test_non_admin_is_rejected(self, db):
        await _link(db, OTHER_ID, ADMIN_WALLET)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_set_fee(OTHER_ID, 100)
        assert exc.value.code == "forbidden"

    async def test_admin_without_wallet_is_rejected(self, db):
        await db.upsert_user(ADMIN_ID, "root", "Root")
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_set_paused(ADMIN_ID, True)
        assert exc.value.code == "wallet_required"

    async def test_wallet_that_is_not_the_configured_admin_is_rejected(self, db):
        await _link(db, ADMIN_ID, OTHER_WALLET)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_set_paused(ADMIN_ID, True)
        assert exc.value.code == "wallet_not_admin"

    async def test_check_is_skipped_without_escrow_admin_address(self, db, monkeypatch):
        await _link(db, ADMIN_ID, OTHER_WALLET)
        monkeypatch.setenv("ESCROW_ADMIN_ADDRESS", "")
        config.reset_settings()
        tx = await markets.prepare_set_paused(ADMIN_ID, True)
        assert tx.kind == "set_paused"


class TestPrepareWithdraw:
    async def test_builds_unsigned_withdraw(self, db):
        await _admin_ready(db)
        state = await read_escrow_state(FakeChain(), "0:" + "ab" * 32)
        tx = await markets.prepare_withdraw_fees(ADMIN_ID, 1_000_000_000, state=state)
        parsed = bodies.parse_message_body(tx.payload)
        assert parsed.op == bodies.OP_WITHDRAW_FEES
        assert parsed.amount == 1_000_000_000
        assert tx.kind == "withdraw_fees"
        assert tx.amount == config.get_settings().admin_gas_nano
        assert tx.details["available_ton"] == "2.5"
        assert tx.to.startswith("EQ")

    async def test_cannot_withdraw_more_than_the_fees(self, db):
        await _admin_ready(db)
        state = await read_escrow_state(FakeChain(), "0:" + "ab" * 32)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_withdraw_fees(ADMIN_ID, 2_500_000_001, state=state)
        assert exc.value.code == "amount_too_high"

    async def test_rejects_zero(self, db):
        await _admin_ready(db)
        state = await read_escrow_state(FakeChain(), "0:" + "ab" * 32)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_withdraw_fees(ADMIN_ID, 0, state=state)
        assert exc.value.code == "bad_amount"

    async def test_never_looks_like_it_can_touch_user_funds(self, db):
        """``max_withdrawable_nano`` == fees: los fondos bloqueados no salen."""
        state = await read_escrow_state(FakeChain(), "0:" + "ab" * 32)
        assert state.max_withdrawable_nano == state.fees_nano
        assert state.max_withdrawable_nano < state.total_locked_nano + state.fees_nano


class TestPrepareFee:
    async def test_builds_set_fee(self, db):
        await _admin_ready(db)
        tx = await markets.prepare_set_fee(ADMIN_ID, 250)
        parsed = bodies.parse_message_body(tx.payload)
        assert parsed.op == bodies.OP_SET_FEE
        assert parsed.admin_fee_bps == 250
        assert tx.details == {"fee_bps": 250, "fee_percent": 2.5}

    @pytest.mark.parametrize("bps", [-1, 1001])
    async def test_rejects_out_of_range(self, db, bps):
        await _admin_ready(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_set_fee(ADMIN_ID, bps)
        assert exc.value.code == "bad_fee"

    async def test_accepts_the_maximum(self, db):
        await _admin_ready(db)
        tx = await markets.prepare_set_fee(ADMIN_ID, markets.MAX_FEE_BPS)
        assert bodies.parse_message_body(tx.payload).admin_fee_bps == markets.MAX_FEE_BPS


class TestPreparePaused:
    async def test_pause_and_resume(self, db):
        await _admin_ready(db)
        paused = await markets.prepare_set_paused(ADMIN_ID, True)
        resumed = await markets.prepare_set_paused(ADMIN_ID, False)
        assert bodies.parse_message_body(paused.payload).paused_flag is True
        assert bodies.parse_message_body(resumed.payload).paused_flag is False
        assert paused.summary == "Pausar el contrato"
        assert resumed.summary == "Reanudar el contrato"


class TestPrepareChangeAdmin:
    async def test_builds_change_admin(self, db):
        await _admin_ready(db)
        target = to_friendly("0:" + "ee" * 32)
        tx = await markets.prepare_change_admin(ADMIN_ID, target)
        parsed = bodies.parse_message_body(tx.payload)
        assert parsed.op == bodies.OP_CHANGE_ADMIN
        assert parsed.address == markets.normalize_admin_address(target)
        assert tx.kind == "change_admin"
        assert tx.details["new_admin_raw"] == parsed.address

    async def test_rejects_the_current_admin(self, db):
        await _admin_ready(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_change_admin(ADMIN_ID, ADMIN_WALLET)
        assert exc.value.code == "same_admin"

    @pytest.mark.parametrize("address", ["", "no-es-una-direccion", "0:" + "00" * 32])
    async def test_rejects_invalid_or_zero_address(self, db, address):
        await _admin_ready(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_change_admin(ADMIN_ID, address)
        assert exc.value.code == "bad_address"


class TestEscrowStateService:
    async def test_wraps_chain_errors_as_a_domain_error(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise TonApiError("down")

        monkeypatch.setattr(escrow_state_module, "read_escrow_state", boom)
        markets.escrow_state_cache().reset()
        with pytest.raises(markets.MarketError) as exc:
            await markets.escrow_state(client=FakeChain())
        assert exc.value.code == "chain_unavailable"
        markets.escrow_state_cache().reset()

    async def test_rejects_when_escrow_is_disabled(self, monkeypatch):
        monkeypatch.setenv("BET_MODE", "points")
        monkeypatch.setenv("ESCROW_ADDRESS", "")
        config.reset_settings()
        with pytest.raises(markets.MarketError) as exc:
            await markets.escrow_state(client=FakeChain())
        assert exc.value.code == "escrow_disabled"
