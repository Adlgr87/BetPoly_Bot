"""Handlers for user stats and leaderboard — F005, F006."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.database import get_leaderboard, get_user, get_user_bets, upsert_user
from bot.keyboards import back_to_menu_keyboard, refresh_ranking_keyboard

MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


# ---------------------------------------------------------------------------
# F006 — Leaderboard
# ---------------------------------------------------------------------------

async def ranking_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username, user.full_name)
    await _send_ranking(update, context, edit=False)


async def ranking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _send_ranking(update, context, edit=True)


async def _send_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> None:
    leaderboard = await get_leaderboard(10)

    if not leaderboard:
        text = "🏆 El ranking está vacío todavía. ¡Sé el primero en apostar!"
    else:
        lines = ["🏆 *Top 10 Apostadores*\n"]
        for i, entry in enumerate(leaderboard):
            medal = MEDALS.get(i, f"{i + 1}.")
            name = entry.get("username") or entry.get("full_name") or "Anónimo"
            lines.append(f"{medal} *{name}* — {entry['points']} pts")
        text = "\n".join(lines)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=refresh_ranking_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=refresh_ranking_keyboard()
        )


# ---------------------------------------------------------------------------
# F005 — My Bets / Profile
# ---------------------------------------------------------------------------

async def misbets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username, user.full_name)
    await _send_profile(update, context, user.id, edit=False)


async def my_bets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _send_profile(update, context, query.from_user.id, edit=True)


async def _send_profile(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit: bool
) -> None:
    user_data = await get_user(user_id)
    if not user_data:
        text = "❌ No se encontró tu perfil. Usa /start primero."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return

    bets = await get_user_bets(user_id)
    total = len(bets)
    wins = sum(1 for b in bets if b["result"] == "win")
    losses = sum(1 for b in bets if b["result"] == "lose")
    pending = sum(1 for b in bets if b["result"] is None)
    accuracy = f"{wins / (wins + losses) * 100:.0f}%" if (wins + losses) > 0 else "—"

    text = (
        f"📊 *Tu Perfil*\n\n"
        f"💰 Saldo: *{user_data['points']} puntos*\n"
        f"🎲 Total apuestas: *{total}*\n"
        f"✅ Ganadas: *{wins}* | ❌ Perdidas: *{losses}* | ⏳ Pendientes: *{pending}*\n"
        f"🎯 Tasa de acierto: *{accuracy}*\n"
    )

    if bets:
        text += "\n📜 *Últimas 5 apuestas:*\n"
        for bet in bets[:5]:
            icon = {"win": "✅", "lose": "❌"}.get(bet["result"], "⏳")
            text += (
                f"{icon} {bet['pred_title'][:30]} → *{bet['option_label']}* "
                f"({bet['amount']} pts)\n"
            )

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=back_to_menu_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=back_to_menu_keyboard()
        )
