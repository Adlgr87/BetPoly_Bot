"""Admin panel handler — F008."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.database import (
    get_bettors_for_prediction,
    get_options,
    get_predictions_to_resolve,
    resolve_prediction,
)
from bot.keyboards import (
    admin_options_keyboard,
    admin_predictions_keyboard,
    back_to_menu_keyboard,
    confirm_resolve_keyboard,
)
from config import ADMIN_IDS

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return
    await _show_admin_panel(update, context, edit=False)


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.answer("🚫 Sin permisos.", show_alert=True)
        return
    await _show_admin_panel(update, context, edit=True)


async def _show_admin_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool
) -> None:
    predictions = await get_predictions_to_resolve()
    if not predictions:
        text = "✅ No hay predicciones pendientes de resolución."
        kb = back_to_menu_keyboard()
    else:
        text = "🔧 *Panel de Administrador*\n\nSelecciona una predicción para resolver:"
        kb = admin_predictions_keyboard(predictions)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def admin_resolve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options for a prediction. callback_data: admin_resolve:<pred_id>"""
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.answer("🚫 Sin permisos.", show_alert=True)
        return

    pred_id = int(query.data.split(":")[1])
    options = await get_options(pred_id)

    await query.edit_message_text(
        "✅ *Selecciona la opción ganadora:*",
        parse_mode="Markdown",
        reply_markup=admin_options_keyboard(pred_id, options),
    )


async def admin_winner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show confirm dialog. callback_data: admin_winner:<pred_id>:<opt_id>"""
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.answer("🚫 Sin permisos.", show_alert=True)
        return

    _, pred_id_str, opt_id_str = query.data.split(":")
    pred_id, opt_id = int(pred_id_str), int(opt_id_str)

    options = await get_options(pred_id)
    opt_label = next((o["label"] for o in options if o["id"] == opt_id), "?")

    await query.edit_message_text(
        f"⚠️ ¿Confirmas que la opción ganadora es *{opt_label}*?",
        parse_mode="Markdown",
        reply_markup=confirm_resolve_keyboard(pred_id, opt_id),
    )


async def admin_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve prediction and notify bettors. callback_data: admin_confirm:<pred_id>:<opt_id>"""
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.answer("🚫 Sin permisos.", show_alert=True)
        return

    _, pred_id_str, opt_id_str = query.data.split(":")
    pred_id, opt_id = int(pred_id_str), int(opt_id_str)

    winning_label = await resolve_prediction(pred_id, opt_id)

    await query.edit_message_text(
        f"✅ Predicción resuelta. Opción ganadora: *{winning_label}*\n\n"
        "Los participantes han sido notificados automáticamente.",
        parse_mode="Markdown",
        reply_markup=back_to_menu_keyboard(),
    )

    # Notify each bettor
    bettors = await get_bettors_for_prediction(pred_id)
    for bet in bettors:
        won = bet["option_id"] == opt_id
        result_text = (
            f"🎉 ¡Has ganado tu apuesta!\n"
            f"La opción *{winning_label}* era la correcta.\n"
            f"💰 Has recuperado *{bet['amount'] * 2} puntos*."
            if won else
            f"😢 Has perdido tu apuesta.\n"
            f"La opción ganadora era *{winning_label}*.\n"
            f"💰 Has perdido *{bet['amount']} puntos*."
        )
        try:
            await context.bot.send_message(
                chat_id=bet["user_id"],
                text=result_text,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Could not notify user %s: %s", bet["user_id"], exc)
