"""Handler for /start command — F001: Onboarding."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.database import upsert_user
from bot.keyboards import main_menu_keyboard


WELCOME_TEXT = (
    "👋 ¡Bienvenido a *BetPoly Bot*!\n\n"
    "Aquí puedes crear predicciones, apostar puntos y competir con tus amigos.\n\n"
    "💰 Empiezas con *1.000 puntos*. ¡Úsalos sabiamente!\n\n"
    "Elige una opción para comenzar:"
)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
