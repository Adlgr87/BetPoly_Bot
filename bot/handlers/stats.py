"""Handlers for user stats, leaderboard and on-chain portfolio — F005, F006."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import database
from bot.formatting import bold, code, esc, ton
from bot.formatting import points as fmt_points
from bot.handlers.common import edit_or_reply, register_user, safe_answer
from bot.keyboards import back_to_menu_keyboard, refresh_ranking_keyboard

MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


# ---------------------------------------------------------------------------
# F006 — leaderboard
# ---------------------------------------------------------------------------

async def ranking_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await _send_ranking(update, context)


async def ranking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    await _send_ranking(update, context)


async def _send_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    leaderboard = await database.get_leaderboard(10)
    user_id = update.effective_user.id if update.effective_user else 0

    if not leaderboard:
        text = "🏆 El ranking está vacío todavía. ¡Sé el primero en apostar!"
    else:
        lines = ["🏆 <b>Top 10 apostadores (puntos)</b>", ""]
        for index, entry in enumerate(leaderboard):
            medal = MEDALS.get(index, f"{index + 1}.")
            name = entry.get("username") or entry.get("full_name") or "Anónimo"
            you = " ← <b>tú</b>" if int(entry["user_id"]) == int(user_id) else ""
            lines.append(
                f"{medal} {bold(name)} — {bold(fmt_points(entry['points']))} pts{you}"
            )
        text = "\n".join(lines)
        if all(int(entry["user_id"]) != int(user_id) for entry in leaderboard):
            me = await database.get_user(user_id)
            if me:
                text += f"\n\n📍 Tu posición: {bold(fmt_points(me['points']))} puntos"
        text += "\n\n<i>El ranking premia el modo puntos (sandbox). Las apuestas TON se liquidan en la blockchain.</i>"

    await edit_or_reply(update, text, reply_markup=refresh_ranking_keyboard())


# ---------------------------------------------------------------------------
# F005 — profile
# ---------------------------------------------------------------------------

async def misbets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await _send_profile(update, context, update.effective_user.id)


async def my_bets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    await _send_profile(update, context, update.callback_query.from_user.id)


async def _send_profile(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    settings = config.get_settings()
    user_data = await database.get_user(user_id)
    if not user_data:
        await edit_or_reply(
            update, "❌ No se encontró tu perfil. Usa /start primero."
        )
        return

    bets = await database.get_user_bets(user_id, limit=5)
    all_bets = await database.get_user_bets(user_id, limit=500)
    wins = sum(1 for b in all_bets if b["result"] == "win")
    losses = sum(1 for b in all_bets if b["result"] == "lose")
    refunds = sum(1 for b in all_bets if b["result"] == "refund")
    pending = sum(1 for b in all_bets if b["result"] is None)
    accuracy = f"{wins / (wins + losses) * 100:.0f}%" if (wins + losses) else "—"
    profit = sum(int(b["payout"] or 0) - int(b["amount"]) for b in all_bets if b["result"])

    lines = [
        "📊 <b>Tu perfil</b>",
        "",
        f"🪙 Saldo: {bold(fmt_points(user_data['points']))} puntos",
        f"🎲 Apuestas: {bold(len(all_bets))} · ✅ {wins} · ❌ {losses} · ↩️ {refunds} · ⏳ {pending}",
        f"🎯 Acierto: {bold(accuracy)}",
        f"📈 Resultado neto: {bold(fmt_points(profit) if profit >= 0 else '-' + fmt_points(abs(profit)))} pts",
    ]

    if bets:
        lines += ["", "📜 <b>Últimas apuestas</b>"]
        for bet in bets:
            icon = {"win": "✅", "lose": "❌", "refund": "↩️"}.get(bet["result"], "⏳")
            payout = f" → +{fmt_points(bet['payout'])}" if bet.get("payout") else ""
            lines.append(
                f"{icon} {esc(bet['pred_title'][:34])} → <b>{esc(bet['option_label'])}</b> "
                f"({fmt_points(bet['amount'])} pts){payout}"
            )

    if settings.escrow_enabled:
        wallet = await database.get_wallet(user_id)
        if wallet:
            onchain = await database.list_onchain_bets(wallet["address"], limit=100)
            claimable = [
                b for b in onchain
                if b["status"] in ("resolved", "cancelled") and not b["claimed"]
            ]
            staked = sum(int(b["stake"]) for b in onchain)
            lines += [
                "",
                "🔒 <b>Portfolio TON</b>",
                f"👛 {code(wallet['address'])}",
                f"🎲 Apuestas on-chain: {bold(len(onchain))} · total: {bold(ton(staked))} TON",
            ]
            if claimable:
                lines.append(
                    f"💰 <b>{len(claimable)}</b> mercado(s) con pago pendiente. "
                    "Reclama en la Mini App."
                )
        else:
            lines += [
                "",
                "👛 No tienes wallet vinculada.",
                f"Vincula una en la Mini App: {esc(settings.webapp_url)}",
            ]

    await edit_or_reply(update, "\n".join(lines), reply_markup=back_to_menu_keyboard())
