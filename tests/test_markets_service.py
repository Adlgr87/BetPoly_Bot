"""Off-chain validation of on-chain transactions (backend never signs)."""
from __future__ import annotations

import time

import pytest

import config
from bot import markets
from bot.chain import bodies

WALLET = "0:" + "11" * 32
OTHER_WALLET = "0:" + "22" * 32


async def _linked(db, user_id=1, address=WALLET):
    await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
    await db.link_wallet(user_id, address, public_key="ab" * 32)


async def _market(db, market_id=1, *, status="open", closes_in=3600, options=("Sí", "No"),
                  winning=None, total=0, winning_pool=0, fee_bps=200):
    await db.upsert_market(
        market_id, WALLET, "¿Pasa algo?", len(options), int(time.time()) + closes_in,
        fee_bps=fee_bps,
    )
    await db.set_market_options(market_id, options)
    if status != "open":
        await db.update_market_settled(market_id, winning or 0, total, winning_pool, status=status)


class TestPrepareCreate:
    async def test_builds_unsigned_transaction(self, db):
        await _linked(db)
        close = int(time.time()) + 3600
        tx = await markets.prepare_create_market(1, "  ¿Ganará   México?  ", ["Sí", "No"], close)
        parsed = bodies.parse_message_body(tx.payload)
        assert parsed.op == bodies.OP_CREATE_MARKET
        assert parsed.title_hash == bodies.title_to_hash("¿Ganará México?")
        assert parsed.closes_at == close
        assert parsed.option_count == 2
        assert tx.amount == markets.CREATE_GAS_NANO
        assert tx.to.startswith("EQ")
        assert "app.tonkeeper.com/transfer/" in tx.deeplink
        assert "bin=" in tx.deeplink

        pending = await db.find_pending_action(
            "create_market", match_a=WALLET, match_b=parsed.title_hash
        )
        assert pending is not None
        assert pending["payload"]["options"] == ["Sí", "No"]

    async def test_requires_wallet(self, users):
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_create_market(
                1, "Titulo valido", ["A", "B"], int(time.time()) + 3600
            )
        assert exc.value.code == "wallet_required"

    @pytest.mark.parametrize(
        "title,options,delta,code",
        [
            ("corto", ["A", "B"], 3600, "bad_title"),
            ("Título suficientemente largo", ["A"], 3600, "bad_options"),
            ("Título suficientemente largo", ["A", "A"], 3600, "bad_options"),
            ("Título suficientemente largo", ["A", "B"], 60, "bad_close_date"),
            ("Título suficientemente largo", ["A", "B"], 400 * 24 * 3600, "bad_close_date"),
        ],
    )
    async def test_validation(self, db, title, options, delta, code):
        await _linked(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_create_market(1, title, options, int(time.time()) + delta)
        assert exc.value.code == code

    async def test_option_count_respects_config(self, db):
        await _linked(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_create_market(
                1, "Título suficientemente largo",
                [f"O{i}" for i in range(config.get_settings().max_options + 1)],
                int(time.time()) + 3600,
            )
        assert exc.value.code == "bad_options"


class TestPrepareBet:
    async def test_valid_bet(self, db):
        await _linked(db)
        await _market(db)
        tx = await markets.prepare_bet(1, 1, 0, 2_000_000_000)
        parsed = bodies.parse_message_body(tx.payload)
        assert (parsed.market_id, parsed.option, parsed.stake) == (1, 0, 2_000_000_000)
        assert tx.amount == 2_000_000_000 + markets.BET_GAS_NANO
        assert tx.details["stake_ton"] == "2"

    async def test_wallet_required(self, users):
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 0, 2_000_000_000)
        assert exc.value.code == "wallet_required"

    async def test_unknown_market(self, db):
        await _linked(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 999, 0, 2_000_000_000)
        assert exc.value.code == "market_not_found"

    async def test_expired_market(self, db):
        await _linked(db)
        await _market(db, closes_in=-60)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 0, 2_000_000_000)
        assert exc.value.code == "market_expired"

    async def test_resolved_market(self, db):
        await _linked(db)
        await _market(db, status="resolved", closes_in=-60, winning=0, total=10, winning_pool=10)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 0, 2_000_000_000)
        assert exc.value.code == "market_closed"

    async def test_option_out_of_range(self, db):
        await _linked(db)
        await _market(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 5, 2_000_000_000)
        assert exc.value.code == "bad_option"

    async def test_minimum_stake(self, db):
        await _linked(db)
        await _market(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 0, 1_000)
        assert exc.value.code == "stake_too_low"

    async def test_one_bet_per_wallet(self, db):
        await _linked(db)
        await _market(db)
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "tx", 1)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_bet(1, 1, 1, 2_000_000_000)
        assert exc.value.code == "already_bet"


class TestClaims:
    async def test_estimate_math(self):
        market = {"status": "resolved", "total_pool": 6000, "winning_pool": 4000,
                  "winning_option": 0, "fee_bps": 200}
        bet = {"option": 0, "stake": 1000}
        # distributable = 6000 - 120 = 5880 -> 1000 * 5880 / 4000
        assert markets.estimate_payout(bet, market) == 1470
        assert markets.estimate_payout({"option": 1, "stake": 1000}, market) == 0
        cancelled = dict(market, status="cancelled")
        assert markets.estimate_payout(bet, cancelled) == 1000
        assert markets.estimate_payout(bet, {"status": "open"}) == 0

    async def test_claim_requires_resolution(self, db):
        await _linked(db)
        await _market(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_claim(1, 1)
        assert exc.value.code == "market_open"

    async def test_claim_without_bet(self, db):
        await _linked(db)
        await _market(db, status="resolved", closes_in=-60, winning=0, total=100, winning_pool=100)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_claim(1, 1)
        assert exc.value.code == "no_bet"

    async def test_valid_claim(self, db):
        await _linked(db)
        await _market(db, status="resolved", closes_in=-60, winning=0,
                      total=6_000_000_000, winning_pool=4_000_000_000)
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "tx", 1)
        tx = await markets.prepare_claim(1, 1)
        assert bodies.parse_message_body(tx.payload).market_id == 1
        assert tx.details["estimate"] == "1470000000"

    async def test_double_claim_blocked(self, db):
        await _linked(db)
        await _market(db, status="resolved", closes_in=-60, winning=0,
                      total=6_000_000_000, winning_pool=4_000_000_000)
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "tx", 1)
        await db.mark_onchain_bet_claimed(1, WALLET, 1_470_000_000)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_claim(1, 1)
        assert exc.value.code == "already_claimed"


class TestAdminActions:
    async def test_only_admin_can_resolve(self, db):
        await _linked(db)
        await _market(db, closes_in=-60)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_resolve(1, 1, 0)
        assert exc.value.code == "forbidden"

    async def test_cannot_resolve_before_close(self, db):
        await _linked(db, admin_id := 1001)
        await _market(db, closes_in=3600)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_resolve(admin_id, 1, 0)
        assert exc.value.code == "market_still_open"

    async def test_resolution_payload(self, db):
        await _linked(db, 1001)
        await _market(db, closes_in=-60, options=("A", "B", "C"))
        tx = await markets.prepare_resolve(1001, 1, 2)
        parsed = bodies.parse_message_body(tx.payload)
        assert (parsed.market_id, parsed.winning_option) == (1, 2)
        assert tx.amount == markets.ADMIN_GAS_NANO

    async def test_invalid_winner(self, db):
        await _linked(db, 1001)
        await _market(db, closes_in=-60)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_resolve(1001, 1, 9)
        assert exc.value.code == "bad_option"

    async def test_cancel_payload(self, db):
        await _linked(db, 1001)
        await _market(db)
        tx = await markets.prepare_cancel(1001, 1)
        assert bodies.parse_message_body(tx.payload).op == bodies.OP_CANCEL_MARKET

    async def test_non_admin_cannot_cancel(self, db):
        await _linked(db)
        await _market(db)
        with pytest.raises(markets.MarketError) as exc:
            await markets.prepare_cancel(1, 1)
        assert exc.value.code == "forbidden"


class TestViews:
    async def test_market_view_odds(self, db):
        await _linked(db)
        await _market(db, options=("A", "B"))
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "t1", 1)
        await db.record_onchain_bet(1, OTHER_WALLET, 1, 3_000_000_000, "t2", 2)
        detail = await markets.market_detail(1, user_id=1)
        assert detail["pool"] == "4000000000"
        assert detail["pool_ton"] == "4"
        assert detail["options"][0]["multiplier"] == 4.0
        assert detail["options"][1]["multiplier"] == 1.3333
        assert detail["my_bet"]["stake"] == "1000000000"
        assert detail["seconds_left"] > 0

    async def test_market_detail_missing(self, db):
        with pytest.raises(markets.MarketError):
            await markets.market_detail(1234)

    async def test_market_view_for_open_market_has_no_multiplier_without_pool(self, db):
        await _market(db, options=("A", "B"))
        detail = await markets.market_detail(1)
        assert detail["options"][0]["multiplier"] is None
        assert detail["pool"] == "0"
