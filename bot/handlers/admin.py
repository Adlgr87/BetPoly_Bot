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
from typing import Any, Dict, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from bot import database, economics, markets
from bot.chain.addresses import same_address, to_friendly
from bot.formatting import bold, code, esc, fmt_ts, ton
from bot.formatting import points as fmt_points
from bot.handlers.common import edit_or_reply, is_admin, safe_answer
from bot.keyboards import (
    admin_chain_market_keyboard,
    admin_chain_markets_keyboard,
    admin_chain_options_keyboard,
    admin_contract_keyboard,
    admin_options_keyboard,
    admin_predictions_keyboard,
    back_to_menu_keyboard,
    confirm_contract_action_keyboard,
    confirm_resolve_keyboard,
)
from bot.parsing import parse_fee_bps, parse_nano

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
            *(
                [f"🚨 <b>{status['failures']}</b> transacción(es) en la cola de reintentos"]
                if int(status.get("failures") or 0)
                else []
            ),
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
        keyboard_rows.append(
            [InlineKeyboardButton("⚙️ Contrato escrow", callback_data="admin_contract")]
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
    distributable = economics.distributable(total, settings.house_rake_bps)
    if winning_pool:
        # Puntos, no TON: «por punto apostado» se expresa con puntos, no con «x».
        per_point = distributable / winning_pool
        payout_hint = f"{per_point:.2f} puntos por punto apostado"
    else:
        payout_hint = "sin ganadores (se reembolsa todo)"

    await edit_or_reply(
        update,
        f"⚠️ ¿Confirmas que la opción ganadora es <b>{esc(label)}</b>?\n\n"
        f"🪙 Pozo: {bold(fmt_points(total))} · en esa opción: {bold(fmt_points(winning_pool))}\n"
        f"📈 Pago: {bold(esc(payout_hint))}\n\n"
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


async def _show_cancel_prepared(
    update: Update, market_id: int, tx: markets.PreparedTx, *, emergency: bool = False
) -> None:
    """One renderer for both cancellation paths (normal and emergency)."""
    title = "Cancelación de emergencia preparada" if emergency else "Cancelación preparada"
    await edit_or_reply(
        update,
        f"🧾 <b>{title} (sin firmar)</b>\n\n"
        f"Mercado: <b>#{market_id} {esc(tx.details.get('market_title', ''))}</b>\n\n"
        "Al firmarla, todas las wallets podrán reclamar su reembolso completo.\n\n"
        f"{code(tx.deeplink)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Firmar en la wallet", url=tx.deeplink)],
                [
                    InlineKeyboardButton(
                        "🔙 Volver", callback_data=f"admin_chain_market:{market_id}"
                    )
                ],
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
        if getattr(exc, "code", "") == "cancel_needs_force":
            # Emergency cancellation of a live market that already holds bets.
            await edit_or_reply(
                update,
                "⚠️ <b>Cancelación de emergencia</b>\n\n"
                f"Mercado: <b>#{market_id}</b>\n\n"
                "El mercado <b>sigue abierto y ya tiene apuestas</b>. Si lo cancelas, "
                "todos los apostadores recuperan su importe y nadie gana.\n\n"
                "<i>Esta acción no se puede deshacer.</i>",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⚠️ Confirmar cancelación",
                                callback_data=f"admin_chain_cancel_force:{market_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Volver",
                                callback_data=f"admin_chain_market:{market_id}",
                            )
                        ],
                    ]
                ),
            )
            return
        await edit_or_reply(update, f"❌ {esc(exc)}")
        return

    await _show_cancel_prepared(update, market_id, tx)


async def admin_chain_cancel_force_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Second step of the emergency cancellation (explicit confirmation)."""
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    try:
        market_id = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return
    try:
        tx = await markets.prepare_cancel(query.from_user.id, market_id, force=True)
    except markets.MarketError as exc:
        await edit_or_reply(update, f"❌ {esc(exc)}")
        return

    await _show_cancel_prepared(update, market_id, tx, emergency=True)


# ===========================================================================
# Escrow contract administration (S-03)
#
# The contract has four admin operations that no interface exposed before.
# They keep the project's invariant: the backend *prepares* an unsigned
# transaction and the human signs it in their own wallet.
# ===========================================================================

CONTRACT_WITHDRAW, CONTRACT_FEE, CONTRACT_ADMIN = range(3)

_PENDING_KEY = "contract_pending"


def _pending(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.user_data.setdefault(_PENDING_KEY, {})


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


async def _contract_state_text(user_id: int) -> Tuple[str, Any]:
    """Render the on-chain state plus how it relates to the operator's wallet."""
    state = await markets.escrow_state()
    wallet = await database.get_wallet(user_id)
    lines = [
        "⚙️ <b>Contrato escrow</b>",
        "",
        f"📍 Dirección: {code(markets.friendly_escrow())}",
        f"🔌 Estado: {bold('⏸ pausado' if state.is_paused else '▶️ activo')}",
        f"⚖️ Comisión actual: {bold(f'{state.fee_bps / 100:.2f} %')}",
        f"🪙 Apuesta mínima: {bold(ton(state.min_stake_nano))} TON",
        f"💸 Comisiones acumuladas: {bold(ton(state.fees_nano))} TON",
        f"🔒 Fondos de usuarios bloqueados: {bold(ton(state.total_locked_nano))} TON",
        f"🏷 Mercados creados: {bold(str(state.market_count))}",
    ]
    if state.balance_nano is not None:
        lines.append(f"🏦 Balance del contrato: {bold(ton(state.balance_nano))} TON")

    lines.append("")
    if state.admin:
        lines.append(f"👤 Admin configurado: {code(state.admin)}")
        if wallet and same_address(wallet["address"], state.admin):
            lines.append("✅ Tu wallet vinculada es la administradora.")
        elif wallet:
            lines.append(
                f"⚠️ Tu wallet ({code(to_friendly(wallet['address']))}) no es la "
                "administradora: la firma sería rechazada por el contrato."
            )
        else:
            lines.append("ℹ️ No tienes wallet vinculada en el bot.")
    else:
        lines.append(
            "ℹ️ <code>ESCROW_ADMIN_ADDRESS</code> no está configurada: no puedo "
            "comprobar que tu wallet sea la administradora."
        )
    lines += [
        "",
        "<i>Las comisiones son lo único que el contrato permite retirar; los "
        "fondos de los usuarios nunca salen por esta vía. El backend no tiene "
        "llaves: cada acción se firma en tu wallet.</i>",
    ]
    if state.stale:
        lines.append(f"⚠️ Estado sin actualizar: {esc(state.error or 'sin conexión')}")
    return "\n".join(lines), state


async def admin_contract_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return
    await _show_contract_panel(update, context)


async def _show_contract_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, error: Optional[str] = None
) -> None:
    if not config.get_settings().escrow_enabled:
        await edit_or_reply(
            update,
            "ℹ️ Esta instancia no tiene contrato escrow configurado.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    try:
        text, state = await _contract_state_text(update.effective_user.id)
    except markets.MarketError as exc:
        await edit_or_reply(
            update,
            f"⚠️ No pude leer el contrato: {esc(str(exc))}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Reintentar", callback_data="admin_contract")],
                    [InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")],
                ]
            ),
        )
        return
    if error:
        text = f"❌ {esc(error)}\n\n{text}"
    await edit_or_reply(update, text, reply_markup=admin_contract_keyboard(state))


async def _show_prepared(update: Update, tx: markets.PreparedTx) -> None:
    """Render an unsigned transaction with the wallet deeplink."""
    await edit_or_reply(
        update,
        f"🧾 <b>{esc(tx.summary)}</b> (sin firmar)\n\n"
        f"Contrato: {code(tx.to)}\n"
        f"Gas adjunto: {bold(ton(tx.amount))} TON\n\n"
        "Abre el botón y firma en tu wallet de administrador. El backend no "
        "tiene llaves y no puede firmar por ti.\n\n"
        f"{code(tx.deeplink)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔐 Firmar en la wallet", url=tx.deeplink)],
                [InlineKeyboardButton("⚙️ Volver al contrato", callback_data="admin_contract")],
            ]
        ),
    )


# -- pause ------------------------------------------------------------------

async def admin_contract_pause_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    try:
        flag = bool(int((query.data or "").split(":")[1]))
    except (IndexError, ValueError):
        return
    _pending(context)["action"] = "pause"
    _pending(context)["value"] = flag
    await edit_or_reply(
        update,
        (
            "⏸ <b>¿Pausar el contrato?</b>\n\n"
            "Mientras esté pausado, nadie podrá crear mercados ni apostar. "
            "Resolver, cancelar y reclamar pagos siguen funcionando."
            if flag
            else "▶️ <b>¿Reanudar el contrato?</b>\n\nSe volverán a aceptar "
            "mercados y apuestas."
        ),
        reply_markup=confirm_contract_action_keyboard("pause"),
    )


# -- confirmación -----------------------------------------------------------

async def admin_contract_ok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    if await _deny(update):
        return
    action = (query.data or "").split(":")[1] if ":" in (query.data or "") else ""
    pending = dict(_pending(context))
    _clear_pending(context)
    try:
        if action == "pause" and "value" in pending:
            tx = await markets.prepare_set_paused(query.from_user.id, bool(pending["value"]))
        elif action == "admin" and pending.get("address"):
            tx = await markets.prepare_change_admin(query.from_user.id, pending["address"])
        else:
            await _show_contract_panel(update, context, error="La acción expiró, inténtalo de nuevo.")
            return
    except markets.MarketError as exc:
        await _show_contract_panel(update, context, error=str(exc))
        return
    await _show_prepared(update, tx)


# -- conversación de entrada de datos ---------------------------------------

async def _contract_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, action: str, prompt: str
) -> int:
    await safe_answer(update.callback_query)
    if await _deny(update):
        return ConversationHandler.END
    _clear_pending(context)
    _pending(context)["action"] = action
    await edit_or_reply(
        update,
        prompt,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_contract")]]
        ),
    )
    return {
        "withdraw": CONTRACT_WITHDRAW,
        "fee": CONTRACT_FEE,
        "admin": CONTRACT_ADMIN,
    }[action]


async def contract_withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        state = await markets.escrow_state()
        available = ton(state.max_withdrawable_nano)
        detail = f"Disponible: <b>{available} TON</b>."
    except markets.MarketError:
        detail = "No pude consultar el saldo de comisiones."
    return await _contract_entry(
        update,
        context,
        action="withdraw",
        prompt=(
            "💸 <b>Retirar comisiones</b>\n\n"
            f"{detail}\n\n"
            "Escribe el importe en TON (por ejemplo <code>1.5</code>). "
            "Solo se pueden retirar comisiones, nunca los fondos de los usuarios."
        ),
    )


async def contract_fee_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _contract_entry(
        update,
        context,
        action="fee",
        prompt=(
            "⚖️ <b>Cambiar la comisión</b>\n\n"
            "Escribe el porcentaje que se queda la plataforma (0–10), "
            "por ejemplo <code>2</code> o <code>2.5</code>.\n"
            "Solo afecta a los mercados creados a partir de ahora."
        ),
    )


async def contract_admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _contract_entry(
        update,
        context,
        action="admin",
        prompt=(
            "👤 <b>Cambiar administrador</b>\n\n"
            "Escribe la dirección TON que pasará a controlar el contrato "
            "(<code>EQ…</code> o <code>0:hex</code>).\n\n"
            "⚠️ Es <b>irreversible</b>: si pierdes esa wallet, pierdes el control."
        ),
    )


async def contract_withdraw_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny(update):
        return ConversationHandler.END
    try:
        amount = parse_nano(update.effective_message.text or "", field="importe")
        tx = await markets.prepare_withdraw_fees(update.effective_user.id, amount)
    except (ValueError, markets.MarketError) as exc:
        await update.effective_message.reply_text(f"❌ {esc(str(exc))}")
        return CONTRACT_WITHDRAW
    _clear_pending(context)
    await _show_prepared(update, tx)
    return ConversationHandler.END


async def contract_fee_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny(update):
        return ConversationHandler.END
    try:
        bps = parse_fee_bps(update.effective_message.text or "")
        tx = await markets.prepare_set_fee(update.effective_user.id, bps)
    except (ValueError, markets.MarketError) as exc:
        await update.effective_message.reply_text(f"❌ {esc(str(exc))}")
        return CONTRACT_FEE
    _clear_pending(context)
    await _show_prepared(update, tx)
    return ConversationHandler.END


async def contract_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _deny(update):
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    try:
        target = markets.normalize_admin_address(raw)
    except (ValueError, markets.MarketError) as exc:
        await update.effective_message.reply_text(f"❌ {esc(str(exc))}")
        return CONTRACT_ADMIN
    _pending(context)["address"] = target
    await update.effective_message.reply_text(
        "⚠️ <b>Confirma el cambio de administrador</b>\n\n"
        f"Nueva dirección: {code(to_friendly(target))}\n\n"
        "A partir de ese momento solo esa wallet podrá resolver mercados, "
        "cambiar la comisión o retirar comisiones. <b>No se puede deshacer.</b>",
        parse_mode="HTML",
        reply_markup=confirm_contract_action_keyboard("admin"),
    )
    return ConversationHandler.END


async def contract_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_pending(context)
    if update.callback_query is not None:
        await safe_answer(update.callback_query)
        await _show_contract_panel(update, context)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            "❌ Operación cancelada.", reply_markup=back_to_menu_keyboard()
        )
    return ConversationHandler.END


async def contract_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_pending(context)
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "⌛ La operación expiró por inactividad. Vuelve a abrir /admin → ⚙️ Contrato escrow."
        )
    return ConversationHandler.END


def build_admin_contract_conversation() -> ConversationHandler:
    """Text input flow for withdraw / fee / change-admin."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(contract_withdraw_entry, pattern=r"^admin_contract_withdraw$"),
            CallbackQueryHandler(contract_fee_entry, pattern=r"^admin_contract_setfee$"),
            CallbackQueryHandler(contract_admin_entry, pattern=r"^admin_contract_changeadmin$"),
        ],
        states={
            CONTRACT_WITHDRAW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contract_withdraw_input)
            ],
            CONTRACT_FEE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contract_fee_input)
            ],
            CONTRACT_ADMIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, contract_admin_input)
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, contract_timeout),
                CallbackQueryHandler(contract_timeout),
            ],
        },
        fallbacks=[
            CommandHandler(["cancelar", "cancel"], contract_cancel),
            CallbackQueryHandler(contract_cancel, pattern=r"^admin_contract$"),
        ],
        allow_reentry=True,
        conversation_timeout=300,
        per_user=True,
        per_chat=False,
    )
