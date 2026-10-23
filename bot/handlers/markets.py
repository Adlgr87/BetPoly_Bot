"""Read-only browsing of on-chain escrow markets from the chat."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import database, economics
from bot.formatting import bold, code, esc, fmt_ts, relative, ton
from bot.handlers.common import edit_or_reply, register_user, safe_answer
from bot.keyboards import market_detail_keyboard, markets_list_keyboard

PAGE_SIZE = 4


async def mercados_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await _show_markets(update, context, page=0)


async def list_markets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    data = update.callback_query.data or ""
    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _show_markets(update, context, page=page)


async def _show_markets(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    settings = config.get_settings()
    if not settings.escrow_enabled:
        await edit_or_reply(
            update,
            "ℹ️ Los mercados on-chain no están habilitados en esta instancia "
            f"(BET_MODE={esc(settings.mode)}).\n\n"
            "Usa /predicciones para el modo puntos.",
        )
        return

    markets_list = await database.list_markets(status="open", limit=100)
    if not markets_list:
        await edit_or_reply(
            update,
            "📭 Todavía no hay mercados on-chain abiertos.\n\n"
            "Crea el primero desde la Mini App o consulta los resueltos con /mercados.",
        )
        return

    total_pages = max(1, (len(markets_list) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = markets_list[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"🔒 <b>Mercados TON</b> ({page + 1}/{total_pages})", ""]
    for market in chunk:
        lines.append(
            f"<b>{esc(market['title'])}</b>\n"
            f"    🪙 pozo {bold(ton(int(market.get('pool') or 0)))} TON · "
            f"👥 {int(market.get('bets') or 0)}\n"
            f"    ⏰ {esc(fmt_ts(int(market['closes_at'])))} "
            f"({esc(relative(int(market['closes_at'])))})"
        )
    lines += [
        "",
        "Los fondos van a un contrato escrow: el bot no los custodia.",
        "Abre la Mini App para apostar y reclamar pagos.",
    ]
    await edit_or_reply(
        update,
        "\n".join(lines),
        reply_markup=markets_list_keyboard(page, total_pages, chunk),
    )


async def market_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    settings = config.get_settings()
    if not settings.escrow_enabled:
        await edit_or_reply(update, "ℹ️ Los mercados on-chain no están habilitados.")
        return
    try:
        market_id = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return

    market = await database.get_market(market_id)
    if not market:
        await edit_or_reply(
            update,
            "⚠️ Ese mercado todavía no está indexado. Espera unos segundos y reintenta.",
        )
        return

    options = {int(o["idx"]): o["label"] for o in market.get("options", [])}
    pools = {int(k): v for k, v in (market.get("option_pools") or {}).items()}
    total = int(market.get("pool") or 0)

    lines = [
        f"🔒 <b>{esc(market['title'])}</b>",
        f"🆔 Mercado #{int(market['market_id'])} · estado: <b>{esc(market['status'])}</b>",
        f"🪙 Pozo: {bold(ton(total))} TON · 👥 {int(market.get('bets') or 0)} apuestas",
        f"⏰ Cierra: {esc(fmt_ts(int(market['closes_at'])))}\n",
        "<b>Opciones</b>",
    ]
    fee_bps = int(market.get("fee_bps") or 0)
    for idx, label in sorted(options.items()):
        pool = pools.get(idx, 0)
        mult = economics.multiplier(total, pool, fee_bps)
        multiplier = f"x{mult:.2f}" if mult else "—"
        lines.append(f"• {esc(label)} — {bold(ton(pool))} TON ({esc(multiplier)})")

    if market["status"] == "resolved" and market.get("winning_option") is not None:
        winner = options.get(int(market["winning_option"]), f"Opción {int(market['winning_option']) + 1}")
        lines += ["", f"🏁 Ganador: <b>{esc(winner)}</b>"]
    elif market["status"] == "cancelled":
        lines += ["", "🛑 Mercado cancelado: cada wallet puede reclamar su reembolso."]

    wallet = await database.get_wallet(update.effective_user.id)
    if wallet:
        bet = await database.get_onchain_bet(market_id, wallet["address"])
        if bet:
            lines += [
                "",
                f"👤 Tu apuesta: <b>{esc(options.get(int(bet['option']), '?'))}</b> · "
                f"{esc(ton(int(bet['stake'])))} TON"
                + (" · <b>reclamada</b>" if bet["claimed"] else ""),
            ]

    lines += [
        "",
        f"Contrato: {code(settings.escrow_address)}",
        "<i>El bot solo indexa la blockchain: no puede mover ni retener tus fondos.</i>",
    ]
    await edit_or_reply(update, "\n".join(lines), reply_markup=market_detail_keyboard(market))
