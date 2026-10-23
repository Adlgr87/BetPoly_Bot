"""Admin panel — F008.

Two different jobs live here:

* **Points mode** (sandbox): the admin picks the winning option and the backend
  distributes the pari-mutuel pool among the winners.
* **On-chain mode** (real TON): the admin never gets a backend key. The bot
  builds an *unsigned* transaction and hands the admin a wallet deep link; the
  admin signs it in their own wallet. The indexer then confirms it on-chain.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from bot import database, markets
from bot.formatting import bold, code, esc, fmt_ts, points as fmt_points, ton
from bot.handlers.common import edit_or_reply, is_admin, safe_answer
from bot.keyboards import (
    admin_chain_market_keyboard,
    admin_chain_markets_keyboard,
    admin_chain_options_keyboard,
    admin_options_keyboard,
    admin_predictions_keyboard,
    back_to_menu_keyboard,
    confirm_resolve_keyboard,
)

logger = logging.getLogger(__name__)

CHAIN_PAGE_SIZE = 5


async def _deny(update: Update) -> bool:
    user = update.effective_user
    if is_admin(user.id if user else None):
        return False
    if update.callback_query:
        await safe_answer(update.callback_query, "🚫 Sin permisos.", alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("🚫 No tienes permisos para usar este comando.")
    return True


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny(update):
        return
    await _show_admin_panel(update, context)


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    await _show_admin_panel(update, context)


async def _show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = config.get_settings()
    predictions = await database.get_predictions_to_resolve()
    lines = ["🔧 <b>Panel de administrador</b>", ""]

    if predictions:
        lines.append(
            f"🪙 <b>{len(predictions)}</b> predicción(es) de puntos esperando resolución:"
        )
        for pred in predictions[:8]:
            lines.append(f"   • {esc(pred['title'][:48])}")
    else:
        lines.append("🪙 No hay predicciones de puntos pendientes.")

    keyboard_rows = []
    if predictions:
        keyboard_rows = admin_predictions_keyboard(predictions).inline_keyboard

    if settings.escrow_enabled:
        status = await database.get_indexer_status()
        onchain = await database.list_markets(status="open", limit=100)
        ready = [m for m in onchain if int(m.get("closes_at") or 0) <= _now()]
        lines += [
            "",
            f"🔒 <b>Mercados on-chain</b>: {len(onchain)} abiertos · {len(ready)} listos para resolver",
            f"👁 Indexer: último bloque leído "
            f"{esc(str(status.get('last_lt') or '—'))}"
            + (f" · ⚠️ {esc(status['last_error'][:60])}" if status.get("last_error") else ""),
            "",
            "<i>Al resolver, el bot genera una transacción sin firmar: tú la firmas "
            "desde tu propia wallet. El backend no tiene llaves.</i>",
        ]
        keyboard_rows = [
            [InlineKeyboardButton(f"📌 {_short(p['title'])}", callback_data=f"admin_resolve:{p['id']}")]
            for p in predictions
        ]
        keyboard_rows.append(
            [InlineKeyboardButton("🔒 Mercados on-chain", callback_data="admin_chain:0")]
        )
        keyboard_rows.append([InlineKeyboardButton("🔙 Salir", callback_data="main_menu")])
    else:
        keyboard_rows = keyboard_rows or [
            [InlineKeyboardButton("🔙 Salir", callback_data="main_menu")]
        ]

    await edit_or_reply(
        update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows)
    )


def _short(text: str, size: int = 36) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


def _now() -> int:
    import time

    return int(time.time())


# ---------------------------------------------------------------------------
# Points mode resolution
# ---------------------------------------------------------------------------

async def admin_resolve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    try:
        pred_id = int((update.callback_query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return

    options = await database.get_options(pred_id)
    if not options:
        await edit_or_reply(update, "⚠️ Predicción sin opciones.", reply_markup=back_to_menu_keyboard())
        return
    pools = await database.get_prediction_pools(pred_id)
    total = pools.pop(-1, 0)
    await edit_or_reply(
        update,
        f"✅ <b>Selecciona la opción ganadora</b>\n\n"
        f"🪙 Pozo total: {bold(fmt_points(total))} puntos",
        reply_markup=admin_options_keyboard(pred_id, options),
    )


async def admin_winner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    try:
        _, pred_id_raw, opt_id_raw = (update.callback_query.data or "").split(":")
        pred_id, opt_id = int(pred_id_raw), int(opt_id_raw)
    except (ValueError, TypeError):
        return

    options = await database.get_options(pred_id)
    label = next((o["label"] for o in options if o["id"] == opt_id), None)
    if label is None:
        await edit_or_reply(update, "⚠️ Esa opción no pertenece a la predicción.")
        return

    pools = await database.get_prediction_pools(pred_id)
    total = pools.pop(-1, 0)
    winning_pool = pools.get(opt_id, 0)
    settings = config.get_settings()
    distributable = total - (total * settings.house_rake_bps) // 10_000
    multiplier = f"x{distributable / winning_pool:.2f}" if winning_pool else "sin ganadores (reembolso)"

    await edit_or_reply(
        update,
        f"⚠️ ¿Confirmas que la opción ganadora es <b>{esc(label)}</b>?\n\n"
        f"🪙 Pozo: {bold(fmt_points(total))} · en esa opción: {bold(fmt_points(winning_pool))}\n"
        f"📈 Pago por punto apostado: {bold(esc(multiplier))}\n\n"
        "<i>Esta acción es irreversible y reparte los puntos una sola vez.</i>",
        reply_markup=confirm_resolve_keyboard(pred_id, opt_id),
    )


async def admin_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    try:
        _, pred_id_raw, opt_id_raw = (query.data or "").split(":")
        pred_id, opt_id = int(pred_id_raw), int(opt_id_raw)
    except (ValueError, TypeError):
        return

    try:
        result = await database.settle_prediction(pred_id, opt_id)
    except database.DatabaseError as exc:
        await edit_or_reply(
            update, f"❌ {esc(exc)}", reply_markup=back_to_menu_keyboard()
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("Settlement failed for prediction %s", pred_id)
        await edit_or_reply(
            update, "❌ Error interno al liquidar la predicción.", reply_markup=back_to_menu_keyboard()
        )
        return

    summary = (
        "✅ <b>Predicción resuelta</b>\n\n"
        f"🎯 {esc(result.title)}\n"
        f"🏁 Ganadora: <b>{esc(result.winning_label)}</b>\n"
        f"🪙 Pozo: {bold(fmt_points(result.total_pool))} · "
        f"en la ganadora: {bold(fmt_points(result.winning_pool))}\n"
        f"✅ Ganadores: {bold(len(result.winners))} · ❌ Perdedores: {bold(len(result.losers))}"
    )
    if result.refunded and result.bets:
        summary += "\n↩️ Nadie acertó: se reembolsó a todos."
    if result.rake:
        summary += f"\n🏦 Comisión de la casa: {bold(fmt_points(result.rake))} pts"

    await edit_or_reply(update, summary, reply_markup=back_to_menu_keyboard())
    await _notify_bettors(context, result)


async def _notify_bettors(context: ContextTypes.DEFAULT_TYPE, result: database.SettlementResult) -> None:
    for index, bet in enumerate(result.bets, start=1):
        if bet.result == "win":
            text = (
                "🎉 <b>¡Ganaste!</b>\n\n"
                f"🎯 {esc(result.title)}\n"
                f"🏁 Opción: <b>{esc(result.winning_label)}</b>\n"
                f"🪙 Apostaste {bold(fmt_points(bet.stake))} → recibes "
                f"<b>{bold(fmt_points(bet.payout))}</b> puntos."
            )
        elif bet.result == "refund":
            text = (
                "↩️ <b>Reembolso</b>\n\n"
                f"🎯 {esc(result.title)}\n"
                f"🪙 Te devolvemos <b>{fmt_points(bet.payout)}</b> puntos."
            )
        else:
            text = (
                "😢 <b>Esta vez no fue</b>\n\n"
                f"🎯 {esc(result.title)}\n"
                f"🏁 Ganó: <b>{esc(result.winning_label)}</b>\n"
                f"🪙 Perdiste {bold(fmt_points(bet.stake))} puntos."
            )
        try:
            await context.bot.send_message(chat_id=bet.user_id, text=text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 - user blocked the bot
            logger.debug("Could not notify user %s: %s", bet.user_id, exc)
        if index % 20 == 0:
            await asyncio.sleep(1)  # stay under Telegram's flood limit


# ---------------------------------------------------------------------------
# On-chain resolution (sign with your own wallet)
# ---------------------------------------------------------------------------

async def admin_chain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    try:
        page = int((update.callback_query.data or "").split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _show_chain_markets(update, context, page)


async def _show_chain_markets(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    settings = config.get_settings()
    if not settings.escrow_enabled:
        await edit_or_reply(
            update, "ℹ️ Mercados on-chain deshabilitados en esta instancia.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    open_markets = await database.list_markets(status="open", limit=100)
    if not open_markets:
        await edit_or_reply(
            update, "✅ No hay mercados on-chain abiertos.", reply_markup=back_to_menu_keyboard()
        )
        return

    now = _now()
    open_markets.sort(key=lambda m: (int(m.get("closes_at") or 0) > now, int(m.get("closes_at") or 0)))
    total_pages = max(1, (len(open_markets) + CHAIN_PAGE_SIZE - 1) // CHAIN_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = open_markets[page * CHAIN_PAGE_SIZE : (page + 1) * CHAIN_PAGE_SIZE]

    lines = [f"🔒 <b>Mercados on-chain</b> ({page + 1}/{total_pages})", ""]
    for market in chunk:
        close_ts = int(market.get("closes_at") or 0)
        state = "🟢 abierto" if close_ts > now else "🟡 cerrado, listo para resolver"
        lines.append(
            f"<b>#{int(market['market_id'])} {esc(market['title'][:44])}</b>\n"
            f"    {state} · cierre {esc(fmt_ts(close_ts))} · "
            f"pozo {bold(ton(int(market.get('pool') or 0)))} TON"
        )
    await edit_or_reply(
        update,
        "\n".join(lines),
        reply_markup=admin_chain_markets_keyboard(page, total_pages, chunk),
    )


async def admin_chain_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    try:
        market_id = int((update.callback_query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return
    await _show_chain_market(update, market_id)


async def _show_chain_market(update: Update, market_id: int) -> None:
    market = await database.get_market(market_id)
    if not market:
        await edit_or_reply(update, "⚠️ Mercado no indexado todavía.")
        return
    now = _now()
    close_ts = int(market.get("closes_at") or 0)
    options = {int(o["idx"]): o["label"] for o in market.get("options", [])}
    pools = {int(k): v for k, v in (market.get("option_pools") or {}).items()}
    total = int(market.get("pool") or 0)

    lines = [
        f"🔒 <b>#{market_id} {esc(market['title'])}</b>",
        f"Estado: <b>{esc(market['status'])}</b> · cierre {esc(fmt_ts(close_ts))}",
        f"Pozo: {bold(ton(total))} TON · {int(market.get('bets') or 0)} apuestas",
        "",
    ]
    for idx, label in sorted(options.items()):
        lines.append(f"• {esc(label)} — {bold(ton(pools.get(idx, 0)))} TON")
    lines += [
        "",
        (
            "✅ Listo: puedes generar la transacción de resolución."
            if close_ts <= now
            else "⏳ Aún no cierra: la resolución on-chain fallará hasta la fecha de cierre."
        ),
        "<i>Firmarás con tu wallet: el bot solo construye el mensaje.</i>",
    ]
    await edit_or_reply(
        update,
        "\n".join(lines),
        reply_markup=admin_chain_market_keyboard(market_id, str(market["status"])),
    )


async def admin_chain_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    try:
        market_id = int((update.callback_query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return
    market = await database.get_market(market_id)
    if not market:
        await edit_or_reply(update, "⚠️ Mercado no indexado todavía.")
        return
    await edit_or_reply(
        update,
        f"🏁 <b>¿Qué opción ganó en #{market_id}?</b>\n"
        "Toca la ganadora para generar la transacción sin firmar.",
        reply_markup=admin_chain_options_keyboard(market_id, market.get("options", [])),
    )


async def admin_chain_win_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    try:
        _, market_id_raw, option_raw = (query.data or "").split(":")
        market_id, option = int(market_id_raw), int(option_raw)
    except (ValueError, TypeError):
        return

    try:
        tx = await markets.prepare_resolve(query.from_user.id, market_id, option)
    except markets.MarketError as exc:
        await edit_or_reply(
            update,
            f"❌ {esc(exc)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Volver", callback_data=f"admin_chain_market:{market_id}")]]
            ),
        )
        return

    await edit_or_reply(
        update,
        "🧾 <b>Resolución preparada (sin firmar)</b>\n\n"
        f"Mercado: <b>#{market_id} {esc(tx.details.get('market_title', ''))}</b>\n"
        f"Ganadora: <b>{esc(tx.details.get('winning_label', ''))}</b>\n"
        f"Valor adjunto (gas): {bold(ton(tx.amount))} TON\n\n"
        "Abre el botón y firma en tu wallet. Cuando la transacción se confirme, "
        "el indexador marcará el mercado como resuelto y avisará a los apostadores.\n\n"
        f"{code(tx.deeplink)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Firmar en la wallet", url=tx.deeplink)],
                [InlineKeyboardButton("🔙 Volver", callback_data=f"admin_chain_market:{market_id}")],
            ]
        ),
    )


async def admin_chain_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    try:
        market_id = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return
    try:
        tx = await markets.prepare_cancel(query.from_user.id, market_id)
    except markets.MarketError as exc:
        await edit_or_reply(update, f"❌ {esc(exc)}")
        return

    await edit_or_reply(
        update,
        "🧾 <b>Cancelación preparada (sin firmar)</b>\n\n"
        f"Mercado: <b>#{market_id} {esc(tx.details.get('market_title', ''))}</b>\n\n"
        "Al firmarla, todas las wallets podrán reclamar su reembolso completo.\n\n"
        f"{code(tx.deeplink)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Firmar en la wallet", url=tx.deeplink)],
                [InlineKeyboardButton("🔙 Volver", callback_data=f"admin_chain_market:{market_id}")],
            ]
        ),
    )
