"""Main entry point for BetPoly_Bot."""
from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from bot.database import close_expired_predictions, get_open_predictions, init_db
from bot.handlers.admin import (
    admin_confirm_callback,
    admin_handler,
    admin_panel_callback,
    admin_resolve_callback,
    admin_winner_callback,
)
from bot.handlers.help import help_handler, how_it_works_callback
from bot.handlers.predictions import (
    bet_amount_message,
    bet_confirm_callback,
    bet_option_callback,
    bet_select_callback,
    build_crear_conversation,
    cmd_crear_callback,
    list_predictions_callback,
    predicciones_handler,
)
from bot.handlers.start import start_handler
from bot.handlers.stats import (
    misbets_handler,
    my_bets_callback,
    ranking_callback,
    ranking_handler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F007 — Scheduled jobs (alerts)
# ---------------------------------------------------------------------------

async def job_close_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark expired predictions as closed."""
    closed = await close_expired_predictions()
    if closed:
        logger.info("Closed %d expired predictions: %s", len(closed), closed)


async def job_remind_closing_soon(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminders for predictions closing within REMINDER_HOURS_BEFORE hours."""
    from datetime import datetime, timedelta, timezone
    import aiosqlite

    threshold = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=config.REMINDER_HOURS_BEFORE)
    threshold_str = threshold.replace(microsecond=0).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.id, p.title, p.closes_at, b.user_id
            FROM predictions p
            JOIN bets b ON b.prediction_id = p.id
            WHERE p.status = 'open' AND p.closes_at <= ?
            """,
            (threshold_str,),
        ) as cursor:
            rows = await cursor.fetchall()

    seen_pairs: set[tuple[int, int]] = set()
    for row in rows:
        key = (row["id"], row["user_id"])
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"⏰ Recordatorio: La predicción\n*{row['title']}*\n"
                    f"cierra pronto (`{row['closes_at'][:16]}`).\n\n"
                    "¡Aún estás a tiempo de apostar! Usa /predicciones"
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Could not remind user %s: %s", row["user_id"], exc)


# ---------------------------------------------------------------------------
# Main menu callback dispatcher
# ---------------------------------------------------------------------------

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.keyboards import main_menu_keyboard
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏠 *Menú Principal*\n\nElige una opción:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    await init_db()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Menú principal y registro"),
            BotCommand("crear", "Crear una nueva predicción"),
            BotCommand("predicciones", "Ver predicciones abiertas"),
            BotCommand("misbets", "Tus apuestas y estadísticas"),
            BotCommand("ranking", "Top 10 apostadores"),
            BotCommand("admin", "Panel de administrador"),
            BotCommand("help", "Ayuda y comandos"),
        ]
    )
    logger.info("Bot commands registered.")


def main() -> None:
    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # F002 — Create prediction (conversation)
    application.add_handler(build_crear_conversation())

    # Commands
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("predicciones", predicciones_handler))
    application.add_handler(CommandHandler("misbets", misbets_handler))
    application.add_handler(CommandHandler("ranking", ranking_handler))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(CommandHandler("help", help_handler))

    # Inline button callbacks
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(list_predictions_callback, pattern=r"^list_predictions:\d+$"))
    application.add_handler(CallbackQueryHandler(cmd_crear_callback, pattern="^cmd_crear$"))
    application.add_handler(CallbackQueryHandler(bet_select_callback, pattern=r"^bet_select:\d+$"))
    application.add_handler(CallbackQueryHandler(bet_option_callback, pattern=r"^bet_option:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(bet_confirm_callback, pattern=r"^bet_confirm:\d+:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(ranking_callback, pattern="^ranking$"))
    application.add_handler(CallbackQueryHandler(my_bets_callback, pattern="^my_bets$"))
    application.add_handler(CallbackQueryHandler(how_it_works_callback, pattern="^how_it_works$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_resolve_callback, pattern=r"^admin_resolve:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_winner_callback, pattern=r"^admin_winner:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern=r"^admin_confirm:\d+:\d+$"))

    # Fallback: plain text message for bet amount input
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bet_amount_message)
    )

    # F007 — Scheduled jobs
    job_queue = application.job_queue
    job_queue.run_repeating(job_close_expired, interval=300, first=10)
    job_queue.run_repeating(job_remind_closing_soon, interval=3600, first=60)

    logger.info("Starting BetPoly_Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
