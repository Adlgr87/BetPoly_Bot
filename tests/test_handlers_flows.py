"""Flujos de la capa Telegram: apuestas de puntos, mercados, perfil y admin.

Todo corre sobre dobles (``tests/telegram_fakes.py``) y la base SQLite de
``tmp_path``; la cadena se sustituye por objetos :class:`EscrowState` y
``PreparedTx``. Se comprueba lo que el usuario *ve* y lo que queda en la base.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from telegram import Chat

from bot import markets
from bot.chain.addresses import to_friendly
from bot.chain.escrow_state import EscrowState
from bot.handlers import (
    admin as admin_handler,
)
from bot.handlers import (
    help as help_handler,
)
from bot.handlers import (
    markets as markets_handler,
)
from bot.handlers import (
    predictions as pred_handler,
)
from bot.handlers import (
    stats as stats_handler,
)
from bot.handlers import (
    wallet as wallet_handler,
)
from bot.handlers.common import edit_or_reply, rate_limited, safe_answer
from bot.markets import MarketError
from tests.telegram_fakes import FailingCallbackQuery, FakeMessage, make_update

WALLET = "0:" + "11" * 32
OTHER = "0:" + "22" * 32
ADMIN = 1001


async def _user(db, user_id=1, points=1000):
    await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
    if points != 1000:
        async with db.connection() as conn:
            await conn.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, user_id))
            await conn.commit()


async def _prediction(db, *, options=("Sí", "No"), closes_in=3600, title="¿Llueve mañana?"):
    return await db.create_prediction(
        creator_id=1,
        chat_id=1,
        title=title,
        options=list(options),
        closes_at=datetime.now(timezone.utc) + timedelta(seconds=closes_in),
    )


async def _bet(db, pred_id, user_id=1, amount=100, option_index=0):
    await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
    options = await db.get_options(pred_id)
    return await db.place_bet(user_id, pred_id, options[option_index]["id"], amount)


def _tx(kind="set_fee", summary="Cambiar la comisión a 2.00 %"):
    return markets.PreparedTx(
        to="EQ" + "A" * 46,
        amount=50_000_000,
        payload="te6cc",
        kind=kind,
        valid_until=int(time.time()) + 300,
        summary=summary,
        deeplink="https://app.tonkeeper.com/transfer/EQ?bin=te6cc",
        details={"market_title": "M", "winning_label": "Sí"},
    )


# ---------------------------------------------------------------------------
# Apuestas de puntos
# ---------------------------------------------------------------------------

class TestBetFlow:
    async def test_select_shows_options_and_pools(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        update, context, recorder = make_update(user_id=1, data=f"bet_select:{pred_id}")
        await pred_handler.bet_select_callback(update, context)
        assert "Pozo total" in recorder.last["text"]
        assert f"bet_option:{pred_id}" in " ".join(recorder.callbacks())

    async def test_select_rejects_a_closed_prediction(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        await db.update_prediction_status(pred_id, "closed") if hasattr(
            db, "update_prediction_status"
        ) else None
        update, context, recorder = make_update(user_id=1, data=f"bet_select:{pred_id}")
        await pred_handler.bet_select_callback(update, context)
        assert recorder.last["text"]

    async def test_option_asks_for_the_amount(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=1, data=f"bet_option:{pred_id}:{options[0]['id']}"
        )
        await pred_handler.bet_option_callback(update, context)
        assert "cuántos puntos" in recorder.last["text"]
        assert context.user_data["awaiting_bet_amount"] is True

    async def test_option_rejects_unknown_option(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        update, context, recorder = make_update(user_id=1, data=f"bet_option:{pred_id}:999")
        await pred_handler.bet_option_callback(update, context)
        assert "no válida" in recorder.last["text"]

    async def test_amount_shows_preview_from_the_backend_formula(self, db, monkeypatch):
        pred_id = await _prediction(db)
        await _bet(db, pred_id, user_id=2, amount=300, option_index=1)
        await _user(db)
        options = await db.get_options(pred_id)
        context = None
        update, context, recorder = make_update(user_id=1, text="100")
        context.user_data["awaiting_bet_amount"] = True
        context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": options[0]["id"]}
        await pred_handler.bet_amount_message(update, context)
        text = recorder.last["text"]
        assert "Resumen de tu apuesta" in text
        # 400 puntos en el pozo, 100 en la opción → 100 * 400 / 100 = 400
        assert "400" in text
        assert f"bet_confirm:{pred_id}:{options[0]['id']}:100" in recorder.callbacks()

    async def test_amount_rejects_decimals_without_truncating(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(user_id=1, text="10.5")
        context.user_data["awaiting_bet_amount"] = True
        context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": options[0]["id"]}
        await pred_handler.bet_amount_message(update, context)
        assert "puntos son enteros" in recorder.last["text"]
        assert "si querías 10" in recorder.last["text"]

    async def test_amount_above_balance_is_refused(self, db):
        pred_id = await _prediction(db)
        await _user(db, points=100)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(user_id=1, text="5000")
        context.user_data["awaiting_bet_amount"] = True
        context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": options[0]["id"]}
        await pred_handler.bet_amount_message(update, context)
        assert "máximo es 100 puntos" in recorder.last["text"]

    async def test_amount_below_minimum_is_refused(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(user_id=1, text="5")
        context.user_data["awaiting_bet_amount"] = True
        context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": options[0]["id"]}
        await pred_handler.bet_amount_message(update, context)
        assert "mínima" in recorder.last["text"]

    async def test_confirm_places_the_bet(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=1, data=f"bet_confirm:{pred_id}:{options[0]['id']}:100"
        )
        await pred_handler.bet_confirm_callback(update, context)
        assert "¡Apuesta registrada!" in recorder.last["text"]
        assert await db.get_user_bet(pred_id, 1) is not None

    async def test_confirm_without_funds_explains_it(self, db):
        pred_id = await _prediction(db)
        await _user(db, points=10)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=1, data=f"bet_confirm:{pred_id}:{options[0]['id']}:5000"
        )
        await pred_handler.bet_confirm_callback(update, context)
        assert "❌" in recorder.last["text"]


class TestGroupMode:
    """F-02: en grupo solo se acepta la respuesta al mensaje del bot."""

    async def _prompt(self, db):
        pred_id = await _prediction(db)
        await _user(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=1,
            data=f"bet_option:{pred_id}:{options[0]['id']}",
            chat_type=Chat.GROUP,
            chat_id=-100,
        )
        await pred_handler.bet_option_callback(update, context)
        return pred_id, context, recorder, options

    async def test_prompt_stores_the_message_id(self, db):
        _, context, recorder, _ = await self._prompt(db)
        assert context.user_data["amount_prompt_id"] is not None
        assert "responde a este mensaje" in recorder.last["text"].lower()

    async def test_reply_in_group_is_accepted(self, db):
        pred_id, context, recorder, options = await self._prompt(db)
        prompt = FakeMessage(recorder, chat=None)
        prompt.message_id = context.user_data["amount_prompt_id"]
        update, _, recorder2 = make_update(
            user_id=1,
            text="100",
            chat_type=Chat.GROUP,
            chat_id=-100,
            reply_to=prompt,
            recorder=recorder,
        )
        await pred_handler.bet_amount_message(update, context)
        assert "Resumen de tu apuesta" in recorder2.last["text"]

    async def test_loose_group_message_is_ignored(self, db):
        pred_id, context, recorder, options = await self._prompt(db)
        update, _, recorder2 = make_update(
            user_id=1, text="100", chat_type=Chat.GROUP, chat_id=-100, recorder=recorder
        )
        before = len(recorder.events)
        await pred_handler.bet_amount_message(update, context)
        assert len(recorder.events) == before  # no responde a nada: silencio

    async def test_reply_to_another_message_is_ignored(self, db):
        pred_id, context, recorder, options = await self._prompt(db)
        other = FakeMessage(recorder, chat=None)
        update, _, recorder2 = make_update(
            user_id=1,
            text="100",
            chat_type=Chat.GROUP,
            chat_id=-100,
            reply_to=other,
            recorder=recorder,
        )
        before = len(recorder.events)
        await pred_handler.bet_amount_message(update, context)
        assert len(recorder.events) == before


class TestCreateConversation:
    async def test_full_happy_path(self, db, monkeypatch):
        await _user(db)
        update, context, recorder = make_update(
            user_id=1, data="cmd_crear", text="/crear"
        )
        state = await pred_handler.crear_handler(update, context)
        assert state == pred_handler.ASK_TITLE

        update, _, recorder = make_update(user_id=1, text="¿Gana el América?")
        state = await pred_handler.ask_title_handler(update, context)
        assert state == pred_handler.ASK_OPTIONS

        update, _, recorder = make_update(user_id=1, text="Sí, No")
        state = await pred_handler.ask_options_handler(update, context)
        assert state == pred_handler.ASK_CLOSES_AT

        from bot.formatting import fmt_local

        closes = fmt_local(datetime.now(timezone.utc) + timedelta(days=2))
        update, _, recorder = make_update(user_id=1, text=closes)
        state = await pred_handler.ask_closes_at_handler(update, context)
        assert state == pred_handler.CONFIRM_CREATE

        update, _, recorder = make_update(user_id=1, data="create_confirm", recorder=recorder)
        state = await pred_handler.confirm_create_callback(update, context)
        assert state == pred_handler.ConversationHandler.END
        assert "¡Predicción creada!" in recorder.last["text"]

    async def test_title_validation(self, db):
        context = _ctx()
        update, _, recorder = make_update(user_id=1, text="corto")
        assert await pred_handler.ask_title_handler(update, context) == pred_handler.ASK_TITLE
        assert "al menos 6" in recorder.last["text"]
        update, _, recorder = make_update(user_id=1, text="x" * 200)
        assert await pred_handler.ask_title_handler(update, context) == pred_handler.ASK_TITLE
        assert "no puede superar" in recorder.last["text"]

    async def test_options_validation(self, db):
        context = _ctx()
        for raw, expected in [
            ("Solo una", "entre 2"),
            ("Sí, sí", "distintas"),
            ("a" * 70 + ", No", "60 caracteres"),
        ]:
            update, _, recorder = make_update(user_id=1, text=raw)
            assert await pred_handler.ask_options_handler(update, context) == pred_handler.ASK_OPTIONS
            assert expected in recorder.last["text"]

    async def test_close_date_validation(self, db):
        from bot.formatting import fmt_local

        context = _ctx()
        update, _, recorder = make_update(user_id=1, text="no es una fecha")
        state = await pred_handler.ask_closes_at_handler(update, context)
        assert state == pred_handler.ASK_CLOSES_AT and "Formato inválido" in recorder.last["text"]

        soon = fmt_local(datetime.now(timezone.utc) + timedelta(minutes=2))
        update, _, recorder = make_update(user_id=1, text=soon)
        state = await pred_handler.ask_closes_at_handler(update, context)
        assert state == pred_handler.ASK_CLOSES_AT and "al menos 10 minutos" in recorder.last["text"]

        far = fmt_local(datetime.now(timezone.utc) + timedelta(days=400))
        update, _, recorder = make_update(user_id=1, text=far)
        state = await pred_handler.ask_closes_at_handler(update, context)
        assert state == pred_handler.ASK_CLOSES_AT and "más de un año" in recorder.last["text"]

    async def test_confirm_without_context(self, db):
        update, context, recorder = make_update(user_id=1, data="create_confirm")
        state = await pred_handler.confirm_create_callback(update, context)
        assert state == pred_handler.ConversationHandler.END
        assert "Se perdió el contexto" in recorder.last["text"]

    async def test_cancel_and_timeout(self, db):
        update, context, recorder = make_update(user_id=1, data="create_cancel")
        assert await pred_handler.confirm_create_callback(update, context) == (
            pred_handler.ConversationHandler.END
        )
        assert "cancelada" in recorder.last["text"]

        update, context, recorder = make_update(user_id=1, text="/cancelar")
        assert await pred_handler.cancel_conv_handler(update, context) == (
            pred_handler.ConversationHandler.END
        )
        update, context, recorder = make_update(user_id=1, text="")
        assert await pred_handler.timeout_handler(update, context) == (
            pred_handler.ConversationHandler.END
        )
        assert "expiró" in recorder.last["text"]

    async def test_points_disabled_blocks_creation(self, db, monkeypatch):
        monkeypatch.setenv("BET_MODE", "escrow")
        _reload_settings(monkeypatch)
        update, context, recorder = make_update(user_id=1, text="/crear")
        assert await pred_handler.crear_handler(update, context) == (
            pred_handler.ConversationHandler.END
        )
        assert "Mini App" in recorder.last["text"]


def _ctx():
    from tests.telegram_fakes import FakeContext, Recorder

    return FakeContext(Recorder())


def _reload_settings(monkeypatch):
    import config

    config.reset_settings()


# ---------------------------------------------------------------------------
# Mercados on-chain
# ---------------------------------------------------------------------------

class TestMarketsView:
    async def _market(self, db):
        await db.enrich_market_from_event(1, WALLET, "¿Gana México?", 2, int(time.time()) + 600)
        await db.set_market_options(1, ["Sí", "No"])
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "t1", 1)

    async def test_list_shows_open_markets(self, db):
        await self._market(db)
        update, context, recorder = make_update(user_id=1, text="/mercados")
        await markets_handler.mercados_handler(update, context)
        assert "Mercados TON" in recorder.last["text"]
        assert "market_view:1" in recorder.callbacks()

    async def test_detail_shows_net_multiplier(self, db):
        await self._market(db)
        update, context, recorder = make_update(user_id=1, data="market_view:1")
        await markets_handler.market_view_callback(update, context)
        assert "¿Gana México?" in recorder.last["text"]
        assert "x1.00" in recorder.last["text"] or "x" in recorder.last["text"]

    async def test_detail_of_missing_market(self, db):
        update, context, recorder = make_update(user_id=1, data="market_view:999")
        await markets_handler.market_view_callback(update, context)
        assert "no" in recorder.last["text"].lower()


# ---------------------------------------------------------------------------
# Perfil, ranking y wallet
# ---------------------------------------------------------------------------

class TestProfile:
    async def test_profile_with_bets_and_wallet(self, db):
        pred_id = await _prediction(db)
        await _bet(db, pred_id, user_id=1, amount=100)
        options = await db.get_options(pred_id)
        await db.settle_prediction(pred_id, options[0]["id"])
        await db.link_wallet(1, WALLET)
        await db.enrich_market_from_event(5, WALLET, "M", 2, int(time.time()) + 600)
        await db.record_onchain_bet(5, WALLET, 0, 1_000_000_000, "t", 1)
        await db.update_market_settled(5, 0, 1_000_000_000, 1_000_000_000)

        update, context, recorder = make_update(user_id=1, text="/misbets")
        await stats_handler.misbets_handler(update, context)
        text = recorder.last["text"]
        assert "Tu perfil" in text and "Portfolio TON" in text
        assert "pago pendiente" in text

    async def test_profile_without_wallet(self, db):
        await _user(db)
        update, context, recorder = make_update(user_id=1, text="/misbets")
        await stats_handler.misbets_handler(update, context)
        assert "No tienes wallet vinculada" in recorder.last["text"]

    async def test_profile_requires_start(self, db):
        update, context, recorder = make_update(user_id=77, text="/misbets")
        await stats_handler.my_bets_callback.__wrapped__(update, context) if hasattr(
            stats_handler.my_bets_callback, "__wrapped__"
        ) else None
        update, context, recorder = make_update(user_id=77, data="my_bets")
        await stats_handler.my_bets_callback(update, context)
        assert "no se encontró tu perfil" in recorder.last["text"].lower()

    async def test_ranking_with_entries_and_own_position(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        update, context, recorder = make_update(user_id=1, data="ranking")
        await stats_handler.ranking_callback(update, context)
        text = recorder.last["text"]
        assert "Top 10" in text and "tú" in text
        assert "ranking" in recorder.callbacks()

    async def test_ranking_when_user_is_not_in_the_top(self, db):
        # Diez rivales por delante: el usuario queda fuera del Top 10 y el
        # mensaje muestra su puesto exacto (R-03).
        for user_id in range(2, 12):
            await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
            await db.adjust_points(user_id, 1000)
        await _user(db, user_id=1)
        update, context, recorder = make_update(user_id=1, text="/ranking")
        await stats_handler.ranking_handler(update, context)
        text = recorder.last["text"]
        assert "Tu posición" in text
        assert "(puesto 11)" in text, text

    async def test_ranking_empty(self, db):
        update, context, recorder = make_update(user_id=1, text="/ranking")
        await stats_handler._send_ranking(update, context)
        assert "vacío" in recorder.last["text"]


class TestWallet:
    async def test_wallet_linked_shows_friendly_address(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.link_wallet(1, WALLET)
        update, context, recorder = make_update(user_id=1, text="/wallet")
        await wallet_handler.wallet_handler(update, context)
        assert "EQ" in recorder.last["text"]

    async def test_wallet_not_linked_explains_non_custody(self, db):
        await db.upsert_user(1, "alice", "Alice")
        update, context, recorder = make_update(user_id=1, text="/wallet")
        await wallet_handler.wallet_handler(update, context)
        assert "nunca" in recorder.last["text"].lower()

    async def test_unlink(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.link_wallet(1, WALLET)
        update, context, recorder = make_update(user_id=1, data="wallet_unlink")
        await wallet_handler.wallet_unlink_callback(update, context)
        assert "desvinculada" in recorder.last["text"]
        assert await db.get_wallet(1) is None

    async def test_points_only_mode(self, db, monkeypatch):
        monkeypatch.setenv("BET_MODE", "points")
        monkeypatch.delenv("ESCROW_ADDRESS", raising=False)
        _reload_settings(monkeypatch)
        await db.upsert_user(1, "alice", "Alice")
        update, context, recorder = make_update(user_id=1, text="/wallet")
        await wallet_handler.wallet_handler(update, context)
        assert "modo puntos" in recorder.last["text"]


# ---------------------------------------------------------------------------
# Administración
# ---------------------------------------------------------------------------

class TestAdminPanel:
    async def test_panel_lists_every_prediction_the_keyboard_offers(self, db):
        """U-04: el texto y el teclado muestran lo mismo."""
        for index in range(10):
            pred_id = await _prediction(db, title=f"Predicción número {index}")
            await _bet(db, pred_id, user_id=2, amount=50)
        async with db.connection() as conn:
            await conn.execute("UPDATE predictions SET status = 'closed'")
            await conn.commit()
        update, context, recorder = make_update(user_id=ADMIN, text="/admin")
        await admin_handler.admin_handler(update, context)
        text = recorder.last["text"]
        buttons = [b for b in recorder.callbacks() if b.startswith("admin_resolve:")]
        assert len(buttons) == 10
        assert text.count("Predicción número") == 10

    async def test_panel_without_predictions(self, db):
        update, context, recorder = make_update(user_id=ADMIN, text="/admin")
        await admin_handler.admin_handler(update, context)
        assert "No hay predicciones" in recorder.last["text"]
        assert "admin_chain:0" in recorder.callbacks()

    async def test_non_admin_is_refused(self, db):
        await _user(db)
        update, context, recorder = make_update(user_id=1, text="/admin")
        await admin_handler.admin_handler(update, context)
        assert "permisos" in recorder.last["text"]
        update, context, recorder = make_update(user_id=1, data="admin_panel")
        await admin_handler.admin_panel_callback(update, context)
        assert update.callback_query.answers[-1]["alert"] is True
        assert recorder.events == []  # no abrió nada


class TestAdminResolvePoints:
    async def _ready(self, db):
        pred_id = await _prediction(db)
        await _bet(db, pred_id, user_id=1, amount=400, option_index=0)
        await _bet(db, pred_id, user_id=2, amount=100, option_index=1)
        return pred_id

    async def test_resolve_flow_ends_in_a_settlement(self, db):
        pred_id = await self._ready(db)
        options = await db.get_options(pred_id)

        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_resolve:{pred_id}"
        )
        await admin_handler.admin_resolve_callback(update, context)
        assert "opción ganadora" in recorder.last["text"]

        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_winner:{pred_id}:{options[0]['id']}"
        )
        await admin_handler.admin_winner_callback(update, context)
        assert "¿Confirmas" in recorder.last["text"]
        assert "puntos por punto apostado" in recorder.last["text"]

        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_confirm:{pred_id}:{options[0]['id']}"
        )
        await admin_handler.admin_confirm_callback(update, context)
        assert any("Predicción resuelta" in e["text"] for e in recorder.events)
        # Los avisos salen **solo** por el outbox (F-04): un `send` directo aquí
        # duplicaría el mensaje que el job envía 30 s después.
        assert not any(e["kind"] == "send" for e in recorder.events)
        assert len(await db.notifications_for(1)) == 1
        assert await db.notifications_for(2) == []

    async def test_winner_without_pool_says_refund(self, db):
        pred_id = await _prediction(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_winner:{pred_id}:{options[1]['id']}"
        )
        await admin_handler.admin_winner_callback(update, context)
        assert "se reembolsa" in recorder.last["text"]

    async def test_winner_of_another_prediction_is_rejected(self, db):
        pred_id = await _prediction(db)
        other = await _prediction(db, title="Otra pregunta distinta")
        options = await db.get_options(other)
        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_winner:{pred_id}:{options[0]['id']}"
        )
        await admin_handler.admin_winner_callback(update, context)
        assert "no pertenece" in recorder.last["text"]

    async def test_settlement_errors_are_shown(self, db):
        update, context, recorder = make_update(
            user_id=ADMIN, data="admin_confirm:999:1"
        )
        await admin_handler.admin_confirm_callback(update, context)
        assert "❌" in recorder.last["text"]

    async def test_bet_notification_failure_does_not_break_resolution(self, db):
        """Un Telegram caído no puede frenar una liquidación ya escrita."""
        pred_id = await self._ready(db)
        options = await db.get_options(pred_id)
        update, context, recorder = make_update(
            user_id=ADMIN, data=f"admin_confirm:{pred_id}:{options[0]['id']}"
        )
        context.bot.error = RuntimeError("usuario bloqueó el bot")
        await admin_handler.admin_confirm_callback(update, context)
        assert any("Predicción resuelta" in e["text"] for e in recorder.events)
        # El aviso queda encolado y el job lo reintenta; nada se perdió.
        assert len(await db.notifications_for(1)) == 1


class TestAdminChain:
    async def _market(self, db, *, closes_in=600):
        await db.enrich_market_from_event(1, WALLET, "¿Gana México?", 2, int(time.time()) + closes_in)
        await db.set_market_options(1, ["Sí", "No"])
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000_000, "t1", 1)

    async def test_empty_list(self, db):
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain:0")
        await admin_handler.admin_chain_callback(update, context)
        assert "No hay mercados" in recorder.last["text"]

    async def test_list_and_detail(self, db):
        await self._market(db)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain:0")
        await admin_handler.admin_chain_callback(update, context)
        assert "Mercados on-chain" in recorder.last["text"]

        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_market:1")
        await admin_handler.admin_chain_market_callback(update, context)
        assert "¿Gana México?" in recorder.last["text"]

        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_options:1")
        await admin_handler.admin_chain_options_callback(update, context)
        assert "¿Qué opción ganó" in recorder.last["text"]

        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_market:404")
        await admin_handler.admin_chain_market_callback(update, context)
        assert "no indexado" in recorder.last["text"]

    async def test_prepare_resolve_is_signed_in_the_wallet(self, db, monkeypatch):
        await self._market(db, closes_in=-60)
        async def fake(*args, **kwargs):
            return _tx("resolve_market", "Resolver #1")
        monkeypatch.setattr(markets, "prepare_resolve", fake)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_win:1:0")
        await admin_handler.admin_chain_win_callback(update, context)
        assert "sin firmar" in recorder.last["text"]
        urls = [b.url for b in recorder.buttons() if getattr(b, "url", None)]
        assert urls and "tonkeeper" in urls[0]

    async def test_prepare_resolve_failure(self, db, monkeypatch):
        async def fake(*args, **kwargs):
            raise MarketError("not_closed", "Aún no cierra.")
        monkeypatch.setattr(markets, "prepare_resolve", fake)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_win:1:0")
        await admin_handler.admin_chain_win_callback(update, context)
        assert "Aún no cierra" in recorder.last["text"]

    async def test_cancel_and_emergency_cancel(self, db, monkeypatch):
        calls = []

        async def fake(user_id, market_id, force=False):
            calls.append(force)
            if not force:
                raise MarketError("cancel_needs_force", "Necesita confirmación.")
            return _tx("cancel_market", "Cancelar")

        monkeypatch.setattr(markets, "prepare_cancel", fake)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_cancel:1")
        await admin_handler.admin_chain_cancel_callback(update, context)
        assert "Cancelación de emergencia" in recorder.last["text"]

        update, context, recorder = make_update(
            user_id=ADMIN, data="admin_chain_cancel_force:1"
        )
        await admin_handler.admin_chain_cancel_force_callback(update, context)
        assert "Cancelación de emergencia preparada" in recorder.last["text"]
        assert calls == [False, True]

    async def test_cancel_happy_path(self, db, monkeypatch):
        async def fake(user_id, market_id, force=False):
            return _tx("cancel_market", "Cancelar")

        monkeypatch.setattr(markets, "prepare_cancel", fake)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_cancel:1")
        await admin_handler.admin_chain_cancel_callback(update, context)
        assert "Cancelación preparada" in recorder.last["text"]

    async def test_cancel_failure(self, db, monkeypatch):
        async def fake(user_id, market_id, force=False):
            raise MarketError("market_not_found", "No existe.")

        monkeypatch.setattr(markets, "prepare_cancel", fake)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_chain_cancel:1")
        await admin_handler.admin_chain_cancel_callback(update, context)
        assert "No existe" in recorder.last["text"]


class TestAdminContract:
    def _state(self, **kwargs):
        base = dict(
            address=WALLET,
            fee_bps=200,
            min_stake_nano=100_000_000,
            paused=False,
            total_locked_nano=1_000_000_000,
            fees_nano=500_000_000,
            market_count=3,
            balance_nano=1_500_000_000,
            admin=None,
            read_at=0,
        )
        base.update(kwargs)
        return EscrowState(**base)

    async def test_panel_shows_state(self, db, monkeypatch):
        await db.upsert_user(ADMIN, "admin", "Admin")
        await db.link_wallet(ADMIN, WALLET)

        async def fake_state():
            return self._state(admin=WALLET, stale=True, error="sin conexión")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract")
        await admin_handler.admin_contract_callback(update, context)
        text = recorder.last["text"]
        assert "Comisiones acumuladas" in text
        assert "administradora" in text
        assert "sin conexión" in text
        assert "admin_contract_withdraw" in recorder.callbacks()

    async def test_panel_warns_when_wallet_is_not_admin(self, db, monkeypatch):
        await db.upsert_user(ADMIN, "admin", "Admin")
        await db.link_wallet(ADMIN, OTHER)

        async def fake_state():
            return self._state(admin=WALLET)

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract")
        await admin_handler.admin_contract_callback(update, context)
        assert "no es la" in recorder.last["text"]

    async def test_panel_without_admin_address(self, db, monkeypatch):
        await db.upsert_user(ADMIN, "admin", "Admin")

        async def fake_state():
            return self._state(admin=None)

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract")
        await admin_handler.admin_contract_callback(update, context)
        assert "ESCROW_ADMIN_ADDRESS" in recorder.last["text"]

    async def test_panel_when_the_chain_fails(self, db, monkeypatch):
        async def fake_state():
            raise MarketError("chain_unavailable", "Sin conexión con TON.")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract")
        await admin_handler.admin_contract_callback(update, context)
        assert "No pude leer el contrato" in recorder.last["text"]
        assert "admin_contract" in recorder.callbacks()

    async def test_pause_needs_confirmation(self, db, monkeypatch):
        async def fake_state():
            return self._state()

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_pause:1")
        await admin_handler.admin_contract_pause_callback(update, context)
        assert "¿Pausar" in recorder.last["text"]
        assert "admin_contract_ok:pause" in recorder.callbacks()
        assert context.user_data["contract_pending"]["value"] is True

    async def test_confirm_builds_the_unsigned_transaction(self, db, monkeypatch):
        async def fake_state():
            return self._state()

        async def fake_pause(user_id, paused):
            return _tx("set_paused", "Pausar el contrato")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        monkeypatch.setattr(markets, "prepare_set_paused", fake_pause)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_pause:1")
        await admin_handler.admin_contract_pause_callback(update, context)

        update, _, recorder = make_update(
            user_id=ADMIN, data="admin_contract_ok:pause", recorder=recorder
        )
        await admin_handler.admin_contract_ok_callback(update, context)
        assert "sin firmar" in recorder.last["text"]

    async def test_confirm_without_pending_reshows_the_panel(self, db, monkeypatch):
        async def fake_state():
            return self._state()

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_ok:pause")
        await admin_handler.admin_contract_ok_callback(update, context)
        assert "La acción expiró" in recorder.last["text"]

    async def test_confirm_reports_domain_errors(self, db, monkeypatch):
        async def fake_state():
            return self._state()

        async def fake_pause(user_id, paused):
            raise MarketError("wallet_not_admin", "Tu wallet no es la administradora.")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        monkeypatch.setattr(markets, "prepare_set_paused", fake_pause)
        context = _ctx()
        context.user_data["contract_pending"] = {"action": "pause", "value": True}
        update, _, recorder = make_update(user_id=ADMIN, data="admin_contract_ok:pause")
        update.callback_query.from_user.id = ADMIN
        await admin_handler.admin_contract_ok_callback(update, context)
        assert "no es la administradora" in recorder.last["text"]

    async def test_withdraw_flow(self, db, monkeypatch):
        async def fake_state():
            return self._state()

        async def fake_withdraw(user_id, amount):
            assert amount == 1_500_000_000
            return _tx("withdraw_fees", "Retirar 1.5 TON")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        monkeypatch.setattr(markets, "prepare_withdraw_fees", fake_withdraw)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_withdraw")
        state = await admin_handler.contract_withdraw_entry(update, context)
        assert state == admin_handler.CONTRACT_WITHDRAW
        assert "Disponible" in recorder.last["text"]

        update, context, recorder = make_update(user_id=ADMIN, text="1.5", recorder=recorder)
        state = await admin_handler.contract_withdraw_input(update, context)
        assert state == admin_handler.ConversationHandler.END
        assert "Retirar 1.5 TON" in recorder.last["text"]

    async def test_withdraw_rejects_bad_input(self, db, monkeypatch):
        async def fake_state():
            raise MarketError("chain_unavailable", "sin red")

        monkeypatch.setattr(markets, "escrow_state", fake_state)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_withdraw")
        await admin_handler.contract_withdraw_entry(update, context)
        assert "No pude consultar" in recorder.last["text"]

        update, context, recorder = make_update(user_id=ADMIN, text="mucho")
        state = await admin_handler.contract_withdraw_input(update, context)
        assert state == admin_handler.CONTRACT_WITHDRAW
        assert "❌" in recorder.last["text"]

    async def test_fee_flow(self, db, monkeypatch):
        async def fake_fee(user_id, bps):
            assert bps == 250
            return _tx("set_fee", "Comisión 2.50 %")

        monkeypatch.setattr(markets, "prepare_set_fee", fake_fee)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_setfee")
        assert await admin_handler.contract_fee_entry(update, context) == admin_handler.CONTRACT_FEE

        update, context, recorder = make_update(user_id=ADMIN, text="2,5 %")
        assert await admin_handler.contract_fee_input(update, context) == (
            admin_handler.ConversationHandler.END
        )
        assert "Comisión 2.50" in recorder.last["text"]

    async def test_fee_rejects_bad_input(self, db):
        update, context, recorder = make_update(user_id=ADMIN, text="20")
        assert await admin_handler.contract_fee_input(update, context) == (
            admin_handler.CONTRACT_FEE
        )
        assert "10 %" in recorder.last["text"]

    async def test_change_admin_needs_a_second_confirmation(self, db, monkeypatch):
        async def fake_admin(user_id, address):
            return _tx("change_admin", "Transferir la administración")

        monkeypatch.setattr(markets, "prepare_change_admin", fake_admin)
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract_changeadmin")
        assert await admin_handler.contract_admin_entry(update, context) == (
            admin_handler.CONTRACT_ADMIN
        )

        friendly = to_friendly("0:" + "ee" * 32)
        update, context, recorder = make_update(user_id=ADMIN, text=friendly)
        assert await admin_handler.contract_admin_input(update, context) == (
            admin_handler.ConversationHandler.END
        )
        assert "Confirma el cambio" in recorder.last["text"]

        update, _, recorder = make_update(
            user_id=ADMIN, data="admin_contract_ok:admin", recorder=recorder
        )
        await admin_handler.admin_contract_ok_callback(update, context)
        assert "Transferir la administración" in recorder.last["text"]

    async def test_change_admin_rejects_a_bad_address(self, db):
        update, context, recorder = make_update(user_id=ADMIN, text="no-es-una-direccion")
        assert await admin_handler.contract_admin_input(update, context) == (
            admin_handler.CONTRACT_ADMIN
        )
        assert "❌" in recorder.last["text"]

    async def test_cancel_and_timeout(self, db):
        update, context, recorder = make_update(user_id=ADMIN, data="admin_contract")
        assert await admin_handler.contract_cancel(update, context) == (
            admin_handler.ConversationHandler.END
        )
        update, context, recorder = make_update(user_id=ADMIN, text="/cancelar")
        await admin_handler.contract_cancel(update, context)
        assert "cancelada" in recorder.last["text"]
        update, context, recorder = make_update(user_id=ADMIN, text="")
        await admin_handler.contract_timeout(update, context)
        assert "expiró" in recorder.last["text"]

    async def test_inputs_refuse_a_non_admin(self, db):
        await _user(db)
        for handler in (
            admin_handler.contract_withdraw_input,
            admin_handler.contract_fee_input,
            admin_handler.contract_admin_input,
        ):
            update, context, recorder = make_update(user_id=1, text="1")
            assert await handler(update, context) == admin_handler.ConversationHandler.END


# ---------------------------------------------------------------------------
# Utilidades compartidas
# ---------------------------------------------------------------------------

class TestCommonHelpers:
    async def test_safe_answer_swallows_a_stale_query(self):
        from tests.telegram_fakes import Recorder

        recorder = Recorder()
        update, context, _ = make_update(user_id=1, data="noop")
        query = FailingCallbackQuery(recorder, "noop", update.effective_user, FakeMessage(recorder))
        await safe_answer(query)  # no debe lanzar

    async def test_edit_or_reply_falls_back_to_a_reply(self):
        from telegram.error import BadRequest

        from tests.telegram_fakes import Recorder

        class BrokenQuery:
            def __init__(self):
                self.message = object()

            async def edit_message_text(self, *args, **kwargs):
                raise BadRequest("no text in the message")

        recorder = Recorder()
        update, context, _ = make_update(user_id=1, data="x", recorder=recorder)
        update.callback_query = BrokenQuery()
        await edit_or_reply(update, "hola")
        assert recorder.last["text"] == "hola"

    async def test_edit_or_reply_ignores_not_modified(self):
        from telegram.error import BadRequest

        from tests.telegram_fakes import Recorder

        class SameQuery:
            def __init__(self):
                self.message = object()

            async def edit_message_text(self, *args, **kwargs):
                raise BadRequest("Message is not modified")

        recorder = Recorder()
        update, context, _ = make_update(user_id=1, data="x", recorder=recorder)
        update.callback_query = SameQuery()
        await edit_or_reply(update, "hola")
        assert recorder.events == []

    async def test_rate_limited_blocks_after_the_limit(self):
        from bot.handlers.common import bet_limiter

        bet_limiter.reset()
        update, context, _ = make_update(user_id=1, text="1")
        results = [rate_limited(update, bet_limiter, "unit") for _ in range(11)]
        assert results[:10] == [False] * 10
        assert results[10] is True
        bet_limiter.reset()

    def test_help_and_error_helper(self):
        assert help_handler.build_help_text(is_admin=True)
