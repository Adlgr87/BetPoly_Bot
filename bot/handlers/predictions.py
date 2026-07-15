"""Handlers for predictions — F002, F003, F004."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.database import (
    create_prediction,
    get_open_predictions,
    get_options,
    get_prediction,
    get_user,
    place_bet,
    upsert_user,
)
from bot.keyboards import (
    back_to_menu_keyboard,
    confirm_bet_keyboard,
    main_menu_keyboard,
    options_keyboard,
    predictions_list_keyboard,
)

logger = logging.getLogger(__name__)

# ConversationHandler states
ASK_TITLE, ASK_OPTIONS, ASK_CLOSES_AT, CONFIRM_CREATE = range(4)

# Number of predictions shown per page
PAGE_SIZE = 3


# ---------------------------------------------------------------------------
# F003 — List open predictions
# ---------------------------------------------------------------------------

async def predicciones_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point via /predicciones command."""
    user = update.effective_user
    await upsert_user(user.id, user.username, user.full_name)
    await _show_predictions_page(update, context, page=0, edit=False)


async def list_predictions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles list_predictions:<page> callback."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    await _show_predictions_page(update, context, page=page, edit=True)


async def _show_predictions_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    edit: bool,
) -> None:
    predictions = await get_open_predictions()
    if not predictions:
        text = "📭 No hay predicciones abiertas ahora mismo.\n\n¿Quieres crear la primera?"
        kb = main_menu_keyboard()
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb)
        return

    total_pages = max(1, (len(predictions) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = predictions[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"📋 *Predicciones Abiertas* (página {page + 1}/{total_pages})\n"]
    pred_ids = []
    for i, p in enumerate(chunk, start=page * PAGE_SIZE + 1):
        closes = p["closes_at"][:16].replace("T", " ")
        lines.append(f"{i}\\. *{p['title']}*\n    ⏰ Cierra: `{closes}`\n")
        pred_ids.append(p["id"])

    text = "\n".join(lines)
    keyboard = predictions_list_keyboard(page, total_pages, pred_ids)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="MarkdownV2", reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="MarkdownV2", reply_markup=keyboard
        )


# ---------------------------------------------------------------------------
# F004 — Bet on a prediction
# ---------------------------------------------------------------------------

async def bet_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options for a prediction. callback_data: bet_select:<pred_id>"""
    query = update.callback_query
    await query.answer()
    pred_id = int(query.data.split(":")[1])

    prediction = await get_prediction(pred_id)
    if not prediction or prediction["status"] != "open":
        await query.edit_message_text(
            "⚠️ Esta predicción ya no está abierta.", reply_markup=back_to_menu_keyboard()
        )
        return

    options = await get_options(pred_id)
    closes = prediction["closes_at"][:16].replace("T", " ")
    text = (
        f"🎲 *{prediction['title']}*\n"
        f"⏰ Cierra: `{closes}`\n\n"
        "Selecciona la opción en la que quieres apostar:"
    )
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=options_keyboard(pred_id, options),
    )


async def bet_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User selected an option; ask for amount. callback_data: bet_option:<pred_id>:<opt_id>"""
    query = update.callback_query
    await query.answer()
    _, pred_id_str, opt_id_str = query.data.split(":")
    pred_id, opt_id = int(pred_id_str), int(opt_id_str)

    # Store in context for next step
    context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": opt_id}

    user = await get_user(query.from_user.id)
    balance = user["points"] if user else 0

    await query.edit_message_text(
        f"💰 Tu saldo actual: *{balance} puntos*\n\n"
        "¿Cuántos puntos quieres apostar? (mínimo 10)\n"
        "_Responde con un número en el chat:_",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )
    context.user_data["awaiting_bet_amount"] = True


async def bet_amount_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive the bet amount from a plain text message."""
    if not context.user_data.get("awaiting_bet_amount"):
        return

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Por favor, introduce un número entero.")
        return

    amount = int(text)
    if amount < 10:
        await update.message.reply_text("❌ La apuesta mínima es de 10 puntos.")
        return

    pending = context.user_data.get("pending_bet", {})
    pred_id = pending.get("pred_id")
    opt_id = pending.get("opt_id")

    if not pred_id or not opt_id:
        await update.message.reply_text("❌ Error interno. Vuelve a intentarlo con /predicciones.")
        context.user_data.pop("awaiting_bet_amount", None)
        return

    prediction = await get_prediction(pred_id)
    options = await get_options(pred_id)
    opt_label = next((o["label"] for o in options if o["id"] == opt_id), "?")

    text_confirm = (
        f"📝 *Resumen de tu apuesta:*\n\n"
        f"🎯 Predicción: *{prediction['title']}*\n"
        f"✅ Opción elegida: *{opt_label}*\n"
        f"💰 Puntos apostados: *{amount}*\n\n"
        "¿Confirmas la apuesta?"
    )
    await update.message.reply_text(
        text_confirm,
        parse_mode="Markdown",
        reply_markup=confirm_bet_keyboard(pred_id, opt_id, amount),
    )
    context.user_data["awaiting_bet_amount"] = False


async def bet_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm and place the bet. callback_data: bet_confirm:<pred_id>:<opt_id>:<amount>"""
    query = update.callback_query
    await query.answer()
    _, pred_id_str, opt_id_str, amount_str = query.data.split(":")
    pred_id, opt_id, amount = int(pred_id_str), int(opt_id_str), int(amount_str)

    user = query.from_user
    await upsert_user(user.id, user.username, user.full_name)

    try:
        await place_bet(user.id, pred_id, opt_id, amount)
    except ValueError as exc:
        await query.edit_message_text(
            f"❌ {exc}", reply_markup=back_to_menu_keyboard()
        )
        return

    new_user = await get_user(user.id)
    balance = new_user["points"] if new_user else "?"

    await query.edit_message_text(
        f"🎉 ¡Apuesta registrada!\n\n"
        f"💰 Saldo restante: *{balance} puntos*\n\n"
        "¡Mucha suerte! 🤞",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )
    context.user_data.pop("pending_bet", None)


# ---------------------------------------------------------------------------
# F002 — Create prediction (ConversationHandler)
# ---------------------------------------------------------------------------

async def crear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /crear command or cmd_crear callback."""
    user = update.effective_user
    await upsert_user(user.id, user.username, user.full_name)
    text = (
        "➕ *Crear Predicción — Paso 1/3*\n\n"
        "Escribe el *título* o pregunta de tu predicción:\n"
        "_(Ej: ¿Ganará el Real Madrid el próximo partido?)_"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")
    return ASK_TITLE


async def ask_title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_pred_title"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Título guardado.\n\n"
        "➕ *Crear Predicción — Paso 2/3*\n\n"
        "Escribe las *opciones* separadas por comas:\n"
        "_(Ej: Sí gana, No gana, Empate)_",
        parse_mode="Markdown",
    )
    return ASK_OPTIONS


async def ask_options_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    options = [o.strip() for o in raw.split(",") if o.strip()]
    if len(options) < 2:
        await update.message.reply_text("❌ Necesitas al menos 2 opciones separadas por comas.")
        return ASK_OPTIONS
    context.user_data["new_pred_options"] = options
    await update.message.reply_text(
        "✅ Opciones guardadas.\n\n"
        "➕ *Crear Predicción — Paso 3/3*\n\n"
        "Escribe la fecha y hora de cierre en formato `DD/MM/YYYY HH:MM`:\n"
        "_(Ej: 25/12/2025 20:00)_",
        parse_mode="Markdown",
    )
    return ASK_CLOSES_AT


async def ask_closes_at_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        closes_at = datetime.strptime(raw, "%d/%m/%Y %H:%M")
        if closes_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            await update.message.reply_text("❌ La fecha debe ser futura. Inténtalo de nuevo.")
            return ASK_CLOSES_AT
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Usa `DD/MM/YYYY HH:MM`.", parse_mode="Markdown"
        )
        return ASK_CLOSES_AT

    context.user_data["new_pred_closes_at"] = closes_at
    title = context.user_data["new_pred_title"]
    options = context.user_data["new_pred_options"]

    opts_text = "\n".join(f"   • {o}" for o in options)
    await update.message.reply_text(
        f"📝 *Resumen de la predicción:*\n\n"
        f"🎯 *{title}*\n"
        f"📌 Opciones:\n{opts_text}\n"
        f"⏰ Cierre: `{raw}`\n\n"
        "¿Confirmas la creación?",
        parse_mode="Markdown",
        reply_markup=_confirm_create_keyboard(),
    )
    return CONFIRM_CREATE


def _confirm_create_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data="create_confirm"),
                InlineKeyboardButton("❌ Cancelar", callback_data="create_cancel"),
            ]
        ]
    )


async def confirm_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "create_cancel":
        await query.edit_message_text(
            "❌ Predicción cancelada.", reply_markup=back_to_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    user = query.from_user
    title = context.user_data["new_pred_title"]
    options = context.user_data["new_pred_options"]
    closes_at = context.user_data["new_pred_closes_at"]
    chat_id = update.effective_chat.id

    pred_id = await create_prediction(user.id, chat_id, title, options, closes_at)

    await query.edit_message_text(
        f"🎉 *¡Predicción creada!*\n\n"
        f"📌 ID: `{pred_id}`\n"
        f"🎯 *{title}*\n\n"
        "Los usuarios ya pueden apostar con /predicciones",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Operación cancelada.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


def build_crear_conversation() -> ConversationHandler:
    """Build and return the ConversationHandler for /crear."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("crear", crear_handler),
        ],
        states={
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_title_handler)],
            ASK_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_options_handler)],
            ASK_CLOSES_AT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_closes_at_handler)],
            CONFIRM_CREATE: [
                CallbackQueryHandler(confirm_create_callback, pattern="^create_confirm$"),
                CallbackQueryHandler(confirm_create_callback, pattern="^create_cancel$"),
            ],
        },
        fallbacks=[MessageHandler(filters.COMMAND, cancel_conv_handler)],
        per_user=True,
        per_chat=False,
    )


# ---------------------------------------------------------------------------
# Callback: cmd_crear (from inline button)
# ---------------------------------------------------------------------------

async def cmd_crear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger crear flow from the main menu inline button."""
    await crear_handler(update, context)
