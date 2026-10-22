"""Handler for /help command — F009: Contextual Help."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.keyboards import back_to_menu_keyboard

HELP_TEXT = (
    "❓ *Comandos disponibles*\n\n"
    "🚀 /start — Menú principal y registro\n"
    "➕ /crear — Crear una nueva predicción\n"
    "📋 /predicciones — Ver predicciones abiertas\n"
    "📊 /misbets — Tus apuestas y estadísticas\n"
    "🏆 /ranking — Top 10 apostadores\n"
    "🔧 /admin — Panel de administrador _(solo admins)_\n"
    "❓ /help — Este mensaje\n"
)

HOW_IT_WORKS = (
    "📖 *¿Cómo funciona BetPoly Bot?*\n\n"
    "1️⃣ Alguien crea una predicción con varias opciones y una fecha de cierre.\n"
    "2️⃣ Los demás usuarios apuestan sus puntos a la opción que creen correcta.\n"
    "3️⃣ Cuando la predicción cierra, un administrador declara el resultado.\n"
    "4️⃣ Los ganadores recuperan el doble de sus puntos apostados. 🎉\n"
    "5️⃣ Los perdedores pierden los puntos apostados. 😢\n\n"
    "¡El que más puntos acumule gana el ranking!"
)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📖 ¿Cómo funciona?", callback_data="how_it_works")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")],
        ]
    )
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=keyboard)


async def how_it_works_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        HOW_IT_WORKS,
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )
