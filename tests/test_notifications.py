"""F-04: avisos al usuario — outbox, dedupe, reintentos y textos.

El envío real (Telegram) se sustituye por un doble; lo que se prueba es la
garantía importante: un aviso nace en la misma transacción que el resultado, no
se duplica y sobrevive a un fallo de envío.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import database, notifications
from main import job_dispatch_notifications

WALLET = "0:" + "11" * 32
OTHER = "0:" + "22" * 32


class FakeBot:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


def _context(bot: FakeBot) -> SimpleNamespace:
    return SimpleNamespace(bot=bot)


async def _prediction(db) -> int:
    await db.upsert_user(1, "alice", "Alice")
    await db.upsert_user(2, "bob", "Bob")
    pred_id = await db.create_prediction(
        creator_id=1,
        chat_id=1,
        title="¿Llueve mañana?",
        options=["Sí", "No", "Quizá"],
        closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    options = await db.get_options(pred_id)
    await db.place_bet(1, pred_id, options[0]["id"], 100)
    await db.place_bet(2, pred_id, options[1]["id"], 300)
    return pred_id


class TestSettlementNotifications:
    async def test_winner_gets_one_notification(self, db):
        pred_id = await _prediction(db)
        options = await db.get_options(pred_id)
        result = await db.settle_prediction(pred_id, options[0]["id"])

        rows = await db.notifications_for(1)
        assert len(rows) == 1
        assert rows[0]["kind"] == notifications.KIND_PREDICTION_RESULT
        assert rows[0]["ref_id"] == str(pred_id)
        assert rows[0]["status"] == "pending"
        payload = json.loads(rows[0]["payload"])
        assert payload["result"] == "win"
        assert payload["payout"] == result.bets[0].payout
        # Quien perdió no recibe ruido.
        assert await db.notifications_for(2) == []

    async def test_the_other_bettor_gets_nothing(self, db):
        pred_id = await _prediction(db)
        options = await db.get_options(pred_id)
        await db.settle_prediction(pred_id, options[0]["id"])
        assert len(await db.notifications_for(2)) == 0

    async def test_refund_flow_notifies_every_bettor(self, db):
        pred_id = await _prediction(db)
        options = await db.get_options(pred_id)
        # Nadie apostó por la tercera opción: reembolso completo para todos.
        await db.settle_prediction(pred_id, options[2]["id"])
        alice = await db.notifications_for(1)
        bob = await db.notifications_for(2)
        assert len(alice) == 1 and len(bob) == 1
        payload = json.loads(alice[0]["payload"])
        assert payload["result"] == "refund"
        assert payload["payout"] == 100
        assert json.loads(bob[0]["payload"])["payout"] == 300

    async def test_settling_twice_does_not_duplicate(self, db):
        pred_id = await _prediction(db)
        options = await db.get_options(pred_id)
        await db.settle_prediction(pred_id, options[0]["id"])
        with pytest.raises(database.DatabaseError):
            await db.settle_prediction(pred_id, options[0]["id"])
        assert len(await db.notifications_for(1)) == 1

    async def test_queue_is_idempotent_by_kind_and_ref(self, db):
        await db.upsert_user(1, "alice", "Alice")
        assert await db.queue_notification(1, "x", 7, {"a": 1}) is True
        assert await db.queue_notification(1, "x", 7, {"a": 2}) is False
        rows = await db.notifications_for(1)
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"a": 1}


class TestMarketNotifications:
    async def _market(self, db, *, status: str = "open", winning: int = 0):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        await db.link_wallet(1, WALLET)
        await db.link_wallet(2, OTHER)
        await db.enrich_market_from_event(9, WALLET, "¿Gana México?", 2, int(time.time()) + 600)
        await db.set_market_options(9, ["Sí", "No"])
        await db.record_onchain_bet(9, WALLET, 0, 1_000_000_000, "t1", 1)
        await db.record_onchain_bet(9, OTHER, 1, 2_000_000_000, "t2", 2)
        if status != "open":
            await db.update_market_settled(9, winning, 3_000_000_000, 1_000_000_000)

    async def test_resolution_notifies_only_the_winner(self, db):
        await self._market(db, status="resolved", winning=0)
        alice = await db.notifications_for(1)
        assert len(alice) == 1
        assert alice[0]["kind"] == notifications.KIND_MARKET_CLAIM
        assert alice[0]["ref_id"] == "9"
        assert await db.notifications_for(2) == []

    async def test_cancellation_notifies_every_bettor(self, db):
        await self._market(db)
        await db.update_market_cancelled(9)
        assert len(await db.notifications_for(1)) == 1
        assert len(await db.notifications_for(2)) == 1
        assert (await db.notifications_for(1))[0]["kind"] == notifications.KIND_MARKET_REFUND

    async def test_unlinked_bet_tor_is_skipped(self, db):
        await db.enrich_market_from_event(10, WALLET, "Suelto", 2, int(time.time()) + 600)
        await db.record_onchain_bet(10, "0:" + "33" * 32, 0, 10, "t9", 9)
        await db.update_market_settled(10, 0, 10, 10)
        assert await db.due_notifications() == []


class TestRender:
    def _row(self, kind, payload):
        return {"kind": kind, "payload": json.dumps(payload)}

    def test_win(self):
        text = notifications.render(
            self._row(
                notifications.KIND_PREDICTION_RESULT,
                {
                    "title": "¿Llueve?",
                    "winning_label": "Sí",
                    "result": "win",
                    "stake": 100,
                    "payout": 400,
                },
            )
        )
        assert "¡Ganaste!" in text
        assert "400" in text
        assert "Sí" in text

    def test_refund(self):
        text = notifications.render(
            self._row(
                notifications.KIND_PREDICTION_RESULT,
                {"title": "X", "result": "refund", "stake": 50, "payout": 50},
            )
        )
        assert "Reembolso" in text and "50" in text

    def test_market_claim_and_cancel(self):
        claim = notifications.render(
            self._row(notifications.KIND_MARKET_CLAIM, {"title": "M", "market_id": 3})
        )
        refund = notifications.render(
            self._row(notifications.KIND_MARKET_REFUND, {"title": "M", "market_id": 3})
        )
        assert "Mercado resuelto" in claim and "#3" in claim
        assert "Mercado cancelado" in refund

    def test_unknown_kind_and_broken_payload(self):
        assert notifications.render({"kind": "nope", "payload": "{}"}) is None
        assert notifications.render({"kind": "nope", "payload": "no-json"}) is None
        assert notifications.render({"kind": "nope", "payload": "[1, 2]"}) is None

    def test_user_text_is_escaped(self):
        text = notifications.render(
            self._row(
                notifications.KIND_PREDICTION_RESULT,
                {"title": "<b>hack</b>", "result": "refund", "stake": 1, "payout": 1},
            )
        )
        assert "&lt;b&gt;hack" in text


class TestDispatch:
    async def _pending(self, db, user_id=1, payload=None):
        await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
        await db.queue_notification(
            user_id,
            notifications.KIND_PREDICTION_RESULT,
            1,
            payload
            or {
                "title": "T",
                "winning_label": "Sí",
                "result": "win",
                "stake": 10,
                "payout": 20,
            },
        )

    async def test_sends_and_marks_as_sent(self, db):
        await self._pending(db)
        bot = FakeBot()
        await job_dispatch_notifications(_context(bot))
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == 1
        assert (await db.notifications_for(1))[0]["status"] == "sent"

    async def test_failure_is_retried_with_backoff(self, db):
        await self._pending(db)
        bot = FakeBot(error=RuntimeError("telegram caído"))
        before = int(time.time())
        await job_dispatch_notifications(_context(bot))
        row = (await db.notifications_for(1))[0]
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert int(row["next_at"]) >= before + 60
        assert "caído" in row["last_error"]
        # Y el siguiente intento (sin esperar) ya no la envía: aún no toca.
        bot2 = FakeBot()
        await job_dispatch_notifications(_context(bot2))
        assert bot2.sent == []

    async def test_gives_up_after_max_attempts(self, db):
        await self._pending(db)
        for _ in range(database.MAX_NOTIFICATION_ATTEMPTS):
            await db.mark_notification_failed(1, "x")
        row = (await db.notifications_for(1))[0]
        assert row["status"] == "failed"
        assert row["attempts"] == database.MAX_NOTIFICATION_ATTEMPTS
        bot = FakeBot()
        await job_dispatch_notifications(_context(bot))
        assert bot.sent == []

    async def test_unrenderable_row_does_not_stop_the_batch(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        await db.queue_notification(1, "nope", 1, {})
        await db.queue_notification(
            2,
            notifications.KIND_PREDICTION_RESULT,
            1,
            {"title": "T", "result": "refund", "stake": 1, "payout": 1},
        )
        bot = FakeBot()
        await job_dispatch_notifications(_context(bot))
        assert len(bot.sent) == 1
        row = (await db.notifications_for(1))[0]
        assert row["status"] == "pending" and row["attempts"] == 1
        assert "desconocido" in row["last_error"]

    async def test_indexer_keeps_working_when_telegram_fails(self, db):
        """La cadena no se bloquea porque Telegram esté caído (F-04)."""
        await db.upsert_user(1, "alice", "Alice")
        await db.link_wallet(1, WALLET)
        await db.enrich_market_from_event(11, WALLET, "M", 2, int(time.time()) + 600)
        await db.record_onchain_bet(11, WALLET, 0, 5, "t", 1)
        await db.update_market_settled(11, 0, 5, 5)
        await job_dispatch_notifications(_context(FakeBot(error=RuntimeError("boom"))))
        # El estado on-chain quedó aplicado y el aviso sigue pendiente para luego.
        assert (await db.get_market(11))["status"] == "resolved"
        assert (await db.notifications_for(1))[0]["status"] == "pending"

    async def test_limiter_defers_without_spending_an_attempt(self, db):
        await self._pending(db)
        from bot.handlers.common import notify_limiter

        notify_limiter.reset()
        for _ in range(20):
            notify_limiter.allow("notify:1")
        bot = FakeBot()
        await job_dispatch_notifications(_context(bot))
        row = (await db.notifications_for(1))[0]
        assert bot.sent == []
        assert row["attempts"] == 0
        assert int(row["next_at"]) > int(time.time())
        notify_limiter.reset()
