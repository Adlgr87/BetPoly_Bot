"""Off-chain points economy: atomicity, pari-mutuel settlement, refunds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot import database


async def _prediction(db, options=("Sí", "No"), closes_in=3600, creator=1, chat=10) -> int:
    return await db.create_prediction(
        creator_id=creator,
        chat_id=chat,
        title="¿Ganará el equipo?",
        options=list(options),
        closes_at=datetime.now(timezone.utc) + timedelta(seconds=closes_in),
    )


class TestBets:
    async def test_bet_debits_points(self, users):
        pred_id = await _prediction(users)
        options = await users.get_options(pred_id)
        balance = await users.place_bet(1, pred_id, options[0]["id"], 250)
        assert balance == 750
        assert (await users.get_user(1))["points"] == 750

    async def test_insufficient_funds(self, users):
        pred_id = await _prediction(users)
        options = await users.get_options(pred_id)
        with pytest.raises(database.InsufficientFunds):
            await users.place_bet(1, pred_id, options[0]["id"], 5000)
        assert (await users.get_user(1))["points"] == 1000

    async def test_duplicate_bet_is_rejected_without_debiting(self, users):
        pred_id = await _prediction(users)
        options = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, options[0]["id"], 100)
        with pytest.raises(database.DuplicateBet):
            await users.place_bet(1, pred_id, options[1]["id"], 100)
        assert (await users.get_user(1))["points"] == 900

    async def test_option_from_another_prediction_is_rejected(self, users):
        pred_id = await _prediction(users)
        other = await _prediction(users)
        foreign = (await users.get_options(other))[0]["id"]
        with pytest.raises(database.DatabaseError):
            await users.place_bet(1, pred_id, foreign, 100)

    async def test_bet_after_deadline_is_rejected(self, users):
        pred_id = await _prediction(users, closes_in=-60)
        options = await users.get_options(pred_id)
        with pytest.raises(database.DatabaseError):
            await users.place_bet(1, pred_id, options[0]["id"], 100)

    async def test_min_and_max_limits(self, users):
        pred_id = await _prediction(users)
        options = await users.get_options(pred_id)
        with pytest.raises(database.DatabaseError):
            await users.place_bet(1, pred_id, options[0]["id"], 1)
        with pytest.raises(database.DatabaseError):
            await users.place_bet(1, pred_id, options[0]["id"], 2_000_000)

    async def test_concurrent_bets_cannot_overdraw(self, users):
        import asyncio

        pred_id = await _prediction(users)
        options = await users.get_options(pred_id)

        async def attempt(user_id):
            try:
                await users.place_bet(user_id, pred_id, options[0]["id"], 600)
                return "ok"
            except database.DatabaseError:
                return "rejected"

        results = await asyncio.gather(attempt(1), attempt(1), attempt(2))
        assert results.count("ok") == 2
        assert (await users.get_user(1))["points"] == 400


class TestSettlement:
    async def test_pari_mutuel_split(self, users):
        pred_id = await _prediction(users)
        yes, no = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 100)   # winner
        await users.place_bet(2, pred_id, no["id"], 300)    # loser
        await users.place_bet(3, pred_id, yes["id"], 300)   # winner

        result = await users.settle_prediction(pred_id, yes["id"])
        assert result.total_pool == 700
        assert result.winning_pool == 400
        assert result.rake == 0
        # winners share the whole pool proportionally: 700 * stake / 400
        assert {b.user_id: b.payout for b in result.bets} == {1: 175, 2: 0, 3: 525}
        assert sum(b.payout for b in result.bets) == 700
        assert (await users.get_user(1))["points"] == 900 + 175
        assert (await users.get_user(2))["points"] == 700
        assert (await users.get_user(3))["points"] == 700 + 525

    async def test_rounding_never_creates_points(self, users):
        pred_id = await _prediction(users)
        yes, no = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 33)
        await users.place_bet(2, pred_id, yes["id"], 33)
        await users.place_bet(3, pred_id, no["id"], 34)

        result = await users.settle_prediction(pred_id, yes["id"])
        assert sum(b.payout for b in result.bets) == result.total_pool == 100

    async def test_nobody_wins_refunds_everyone(self, users):
        pred_id = await _prediction(users)
        yes, no = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 200)
        await users.place_bet(2, pred_id, no["id"], 300)

        result = await users.settle_prediction(pred_id, no["id"])
        assert result.refunded is False  # someone picked "no"
        assert result.winning_pool == 300

        pred2 = await _prediction(users, options=("A", "B", "C"))
        opts = await users.get_options(pred2)
        await users.place_bet(1, pred2, opts[0]["id"], 100)
        await users.place_bet(2, pred2, opts[1]["id"], 100)
        result2 = await users.settle_prediction(pred2, opts[2]["id"])
        assert result2.winning_pool == 0
        assert result2.refunded is True
        assert {b.user_id: b.payout for b in result2.bets} == {1: 100, 2: 100}
        assert sum(b.payout for b in result2.bets) == result2.total_pool

    async def test_resolution_is_idempotent(self, users):
        pred_id = await _prediction(users)
        yes, _ = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 100)
        await users.settle_prediction(pred_id, yes["id"])
        balance = (await users.get_user(1))["points"]
        with pytest.raises(database.DatabaseError):
            await users.settle_prediction(pred_id, yes["id"])
        assert (await users.get_user(1))["points"] == balance

    async def test_option_must_belong_to_prediction(self, users):
        pred_id = await _prediction(users)
        other = await _prediction(users)
        foreign = (await users.get_options(other))[0]["id"]
        with pytest.raises(database.DatabaseError):
            await users.settle_prediction(pred_id, foreign)

    async def test_rake_is_taken_from_the_pool(self, users, monkeypatch):
        monkeypatch.setenv("HOUSE_RAKE_BPS", "500")
        import config

        config.reset_settings()
        pred_id = await _prediction(users)
        yes, no = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 1000)
        await users.place_bet(2, pred_id, no["id"], 10)

        result = await users.settle_prediction(pred_id, yes["id"])
        assert result.rake == (1010 * 500) // 10_000
        assert sum(b.payout for b in result.bets) == result.total_pool - result.rake

    async def test_history_and_leaderboard(self, users):
        pred_id = await _prediction(users)
        yes, _ = await users.get_options(pred_id)
        await users.place_bet(1, pred_id, yes["id"], 400)
        await users.settle_prediction(pred_id, yes["id"])
        bets = await users.get_user_bets(1)
        assert bets and bets[0]["result"] == "win"
        assert bets[0]["payout"] == 400
        board = await users.get_leaderboard(10)
        assert board[0]["user_id"] == 1
        assert board[0]["points"] == 1000


class TestLifecycle:
    async def test_close_expired_predictions(self, users):
        expired = await _prediction(users, closes_in=-5)
        active = await _prediction(users, closes_in=600)
        closed = await users.close_expired_predictions()
        assert closed == [expired]
        assert (await users.get_prediction(expired))["status"] == "closed"
        assert (await users.get_prediction(active))["status"] == "open"

    async def test_reminders_are_deduplicated(self, users):
        pred_id = await _prediction(users, closes_in=3600)
        option = (await users.get_options(pred_id))[0]["id"]
        await users.place_bet(1, pred_id, option, 10)

        first = await users.claim_reminders("closing")
        assert len(first) == 1 and first[0]["pred_id"] == pred_id
        second = await users.claim_reminders("closing")
        assert second == []

    async def test_reminders_ignore_far_future(self, users):
        pred_id = await _prediction(users, closes_in=48 * 3600)
        option = (await users.get_options(pred_id))[0]["id"]
        await users.place_bet(1, pred_id, option, 10)
        assert await users.claim_reminders("closing") == []

    async def test_predictions_are_scoped_per_group(self, users):
        await _prediction(users, chat=111)
        await _prediction(users, chat=222)
        assert len(await users.get_open_predictions(chat_id=111)) == 1
        assert len(await users.get_open_predictions()) == 2
