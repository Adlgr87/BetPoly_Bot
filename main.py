"""BetPoly_Bot entry point.

Runs three cooperating components in a single asyncio loop:

1. the Telegram bot (polling),
2. the Mini App / HTTP API (aiohttp),
3. the read-only escrow indexer.

Only (1) and (2) are strictly required; the indexer degrades gracefully when
the TON API is unreachable (it retries with backoff and never blocks the bot).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timezone

from telegram import BotCommand, MenuButtonWebApp, Update, WebAppInfo
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bot import database, notifications
from bot.api import start_server
from bot.chain.indexer import EscrowIndexer
from bot.chain.toncenter import TonCenterClient
from bot.formatting import esc, fmt_ts
from bot.handlers.admin import (
    admin_chain_callback,
    admin_chain_cancel_callback,
    admin_chain_cancel_force_callback,
    admin_chain_market_callback,
    admin_chain_options_callback,
    admin_chain_win_callback,
    admin_confirm_callback,
    admin_contract_callback,
    admin_contract_ok_callback,
    admin_contract_pause_callback,
    admin_handler,
    admin_panel_callback,
    admin_predictions_page_callback,
    admin_resolve_callback,
    admin_winner_callback,
    build_admin_contract_conversation,
)
from bot.handlers.common import menu_for, notify_limiter, safe_answer
from bot.handlers.help import help_callback, help_handler, how_it_works_callback
from bot.handlers.markets import (
    list_markets_callback,
    market_view_callback,
    mercados_handler,
)
from bot.handlers.predictions import (
    bet_amount_message,
    bet_confirm_callback,
    bet_option_callback,
    bet_select_callback,
    build_crear_conversation,
    cancel_bet_handler,
    list_predictions_callback,
    predicciones_handler,
)
from bot.handlers.start import menu_callback, start_handler
from bot.handlers.stats import (
    misbets_handler,
    my_bets_callback,
    ranking_callback,
    ranking_handler,
)
from bot.handlers.wallet import (
    wallet_handler,
    wallet_info_callback,
    wallet_unlink_callback,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_close_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        closed = await database.close_expired_predictions()
    except Exception:  # noqa: BLE001
        logger.exception("close_expired_predictions failed")
        return
    if closed:
        logger.info("Closed %d expired predictions: %s", len(closed), closed)


async def job_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send at most one reminder per (prediction, user) — ever."""
    try:
        rows = await database.claim_reminders("closing")
    except Exception:  # noqa: BLE001
        logger.exception("claim_reminders failed")
        return
    for row in rows:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "⏰ <b>Recordatorio</b>\n\n"
                    f"La predicción <b>{esc(row['title'])}</b> cierra "
                    f"{esc(fmt_ts(_epoch(row['closes_at'])))}.\n\n"
                    "Aún estás a tiempo de apostar con /predicciones"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not remind user %s: %s", row["user_id"], exc)


def _epoch(value) -> int:
    try:
        if isinstance(value, (int, float)):
            return int(value)
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return 0


async def job_dispatch_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía los avisos pendientes (F-04) sin bloquear nada más.

    Lotes de 20 cada 30 s: suficiente para un host de 512 MB y fácil de razonar.
    Un fallo de envío no se pierde: la fila reaparece con backoff exponencial.
    """
    try:
        rows = await database.due_notifications(20)
    except Exception:  # noqa: BLE001
        logger.exception("due_notifications failed")
        return
    for row in rows:
        allowed, retry_after = notify_limiter.allow(f"notify:{row['user_id']}")
        if not allowed:
            await database.reschedule_notification(row["id"], int(retry_after) or 60)
            continue
        text = notifications.render(row)
        if text is None:
            await database.mark_notification_failed(row["id"], "tipo desconocido")
            continue
        try:
            await context.bot.send_message(
                chat_id=int(row["user_id"]), text=text, parse_mode="HTML"
            )
        except Exception as exc:  # noqa: BLE001 - Telegram puede fallar por mil motivos
            logger.debug("No se pudo enviar el aviso %s: %s", row["id"], exc)
            await database.mark_notification_failed(row["id"], str(exc))
            continue
        await database.mark_notification_sent(row["id"])


async def job_housekeeping(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await database.purge_nonces(86_400)
        await database.purge_pending_actions(86_400)
    except Exception:  # noqa: BLE001
        logger.exception("housekeeping failed")


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and, when possible, tell the user something useful."""
    error = context.error
    logger.error("Unhandled error while processing update: %s", error, exc_info=error)

    if isinstance(error, (BadRequest, Forbidden)):
        return
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(Exception):
            await update.effective_message.reply_text(
                "⚠️ Algo salió mal procesando tu solicitud. Inténtalo de nuevo en unos segundos.",
                reply_markup=menu_for(update),
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _commands(settings: config.Settings) -> list[BotCommand]:
    commands = [BotCommand("start", "Menú principal")]
    if settings.escrow_enabled:
        commands += [
            BotCommand("wallet", "Vincular tu wallet TON"),
            BotCommand("mercados", "Mercados on-chain (solo lectura)"),
        ]
    if settings.points_enabled:
        commands += [
            BotCommand("crear", "Crear una predicción de puntos"),
            BotCommand("predicciones", "Ver predicciones abiertas"),
        ]
    commands += [
        BotCommand("misbets", "Perfil y apuestas"),
        BotCommand("ranking", "Top 10 por puntos"),
        BotCommand("help", "Ayuda"),
    ]
    if settings.admin_ids:
        commands.append(BotCommand("admin", "Panel de administrador"))
    return commands


async def post_init(application: Application) -> None:
    settings = config.get_settings()
    settings.validate()
    await database.init_db()

    # --- Mini App menu button ------------------------------------------
    if settings.escrow_enabled and settings.web_enabled:
        if settings.webapp_url.startswith("https://"):
            try:
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="⚡ Apostar TON", web_app=WebAppInfo(url=settings.webapp_url)
                    )
                )
            except Exception as exc:  # noqa: BLE001 - non fatal
                logger.warning("Could not set the Mini App menu button: %s", exc)
        else:
            logger.warning(
                "PUBLIC_BASE_URL is not https; Telegram will reject the Mini App button."
            )

    try:
        await application.bot.set_my_commands(_commands(settings))
    except Exception as exc:  # noqa: BLE001 - non fatal (network)
        logger.warning("Could not register bot commands: %s", exc)

    # --- HTTP API + Mini App -------------------------------------------
    application.bot_data["ton_client"] = None
    application.bot_data["web_runner"] = None
    application.bot_data["indexer"] = None

    ton_client = None
    if settings.escrow_enabled:
        ton_client = TonCenterClient()
        application.bot_data["ton_client"] = ton_client

    if settings.web_enabled:
        try:
            runner = await start_server(ton_client=ton_client)
            application.bot_data["web_runner"] = runner
        except OSError as exc:
            logger.error("Could not start the HTTP server: %s", exc)

    if settings.escrow_enabled and settings.indexer_enabled and ton_client is not None:
        indexer = EscrowIndexer(
            client=ton_client,
            escrow_address=settings.escrow_address,
            poll_interval=settings.indexer_poll_interval,
            batch=settings.indexer_batch,
            min_stake_nano=settings.min_stake_nano,
        )
        application.bot_data["indexer"] = indexer
        indexer.start()

    logger.info(
        "BetPoly ready · mode=%s · points=%s · escrow=%s · web=%s",
        settings.mode,
        settings.points_enabled,
        settings.escrow_address or "off",
        settings.public_base_url if settings.web_enabled else "off",
    )


async def post_shutdown(application: Application) -> None:
    indexer: EscrowIndexer | None = application.bot_data.get("indexer")
    if indexer is not None:
        await indexer.stop()
    runner = application.bot_data.get("web_runner")
    if runner is not None:
        await runner.cleanup()
    client = application.bot_data.get("ton_client")
    if client is not None:
        await client.close()
    await database.close_db()
    logger.info("BetPoly stopped cleanly")


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(config.require_bot_token())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- Conversations first: they must win over the generic text handler.
    application.add_handler(build_crear_conversation())
    application.add_handler(build_admin_contract_conversation())

    # --- Commands --------------------------------------------------------
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler(["wallet", "billetera"], wallet_handler))
    application.add_handler(CommandHandler("mercados", mercados_handler))
    application.add_handler(CommandHandler("predicciones", predicciones_handler))
    application.add_handler(CommandHandler("misbets", misbets_handler))
    application.add_handler(CommandHandler("ranking", ranking_handler))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(
        CommandHandler(["cancelar", "cancel"], cancel_bet_handler)
    )

    # --- Inline callbacks ------------------------------------------------
    callbacks = [
        ("^main_menu$", menu_callback),
        ("^noop$", noop_callback),
        ("^help$", help_callback),
        ("^how_it_works$", how_it_works_callback),
        (r"^list_predictions:\d+$", list_predictions_callback),
        (r"^bet_select:\d+$", bet_select_callback),
        (r"^bet_option:\d+:\d+$", bet_option_callback),
        (r"^bet_confirm:\d+:\d+:\d+$", bet_confirm_callback),
        ("^ranking$", ranking_callback),
        ("^my_bets$", my_bets_callback),
        ("^wallet_info$", wallet_info_callback),
        ("^wallet_unlink$", wallet_unlink_callback),
        (r"^list_markets:\d+$", list_markets_callback),
        (r"^market_view:\d+$", market_view_callback),
        ("^admin_panel$", admin_panel_callback),
        (r"^admin_preds:\d+$", admin_predictions_page_callback),
        (r"^admin_resolve:\d+$", admin_resolve_callback),
        (r"^admin_winner:\d+:\d+$", admin_winner_callback),
        (r"^admin_confirm:\d+:\d+$", admin_confirm_callback),
        (r"^admin_chain:\d+$", admin_chain_callback),
        (r"^admin_chain_market:\d+$", admin_chain_market_callback),
        (r"^admin_chain_options:\d+$", admin_chain_options_callback),
        (r"^admin_chain_win:\d+:\d+$", admin_chain_win_callback),
        (r"^admin_chain_cancel:\d+$", admin_chain_cancel_callback),
        (r"^admin_chain_cancel_force:\d+$", admin_chain_cancel_force_callback),
        ("^admin_contract$", admin_contract_callback),
        (r"^admin_contract_pause:\d+$", admin_contract_pause_callback),
        (r"^admin_contract_ok:[a-z0-9_]+$", admin_contract_ok_callback),
    ]
    for pattern, handler in callbacks:
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    # --- Plain text: bet amount input (last, lowest priority) ------------
    # En privado cualquier número sirve; en grupo el handler exige que sea una
    # respuesta al mensaje del bot (F-02), así que no hace falta filtrar por chat.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bet_amount_message),
        group=1,
    )

    # --- Jobs ------------------------------------------------------------
    assert application.job_queue is not None, "python-telegram-bot[job-queue] is required"
    application.job_queue.run_repeating(job_close_expired, interval=60, first=10)
    application.job_queue.run_repeating(job_reminders, interval=600, first=60)
    application.job_queue.run_repeating(job_dispatch_notifications, interval=30, first=20)
    application.job_queue.run_repeating(job_housekeeping, interval=3600, first=300)

    application.add_error_handler(error_handler)
    return application


def run() -> None:
    settings = config.get_settings()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    logger.info("Starting BetPoly_Bot…")
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


async def check() -> None:
    """Validate configuration and database without touching the network."""
    settings = config.get_settings()
    settings.validate()
    await database.init_db()
    await database.close_db()
    print("✅ Configuración válida")
    print(f"   mode={settings.mode} points={settings.points_enabled} escrow={settings.escrow_address}")
    print(f"   web={settings.public_base_url} proof_domain={settings.proof_domain}")
    print(f"   admins={len(settings.admin_ids)} db={database.database_path()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BetPoly_Bot")
    parser.add_argument(
        "--check", action="store_true", help="Valida la configuración y sale sin conectar a Telegram"
    )
    args = parser.parse_args()
    if args.check:
        try:
            asyncio.run(check())
        except config.ConfigError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        return
    run()


if __name__ == "__main__":
    main()
