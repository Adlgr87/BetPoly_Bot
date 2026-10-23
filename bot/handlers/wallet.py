"""Wallet linking guidance — the bot never asks for keys or seed phrases."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import database
from bot.chain.addresses import to_friendly
from bot.formatting import bold, code, esc
from bot.handlers.common import edit_or_reply, register_user, safe_answer
from bot.keyboards import wallet_keyboard

WALLET_EXPLAINER = (
    "🔐 <b>Vinculación de wallet (no-custodial)</b>\n\n"
    "El bot <b>nunca</b> te pedirá tu frase semilla ni tu clave privada. "
    "Si alguien te los pide en nombre de este bot, es un fraude.\n\n"
    "<b>Cómo se vincula</b>\n"
    "1. Abre la Mini App desde el botón de abajo.\n"
    "2. Pulsa «Conectar wallet» y elige Tonkeeper, MyTonWallet, Telegram Wallet…\n"
    "3. Tu wallet firma un mensaje de prueba (<code>ton_proof</code>). "
    "Firmar un mensaje <b>no</b> cuesta gas y <b>no</b> mueve fondos.\n"
    "4. El bot guarda solo tu dirección pública.\n\n"
    "<b>Qué puede y qué no puede hacer el bot</b>\n"
    "✅ Leer la blockchain y mostrarte mercados y saldos.\n"
    "✅ Preparar transacciones para que tú las firmes.\n"
    "❌ Mover tus fondos, bloquear retiros o cambiar tu apuesta.\n"
    "❌ Ver tu clave privada: jamás sale de tu wallet."
)


async def wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await _send_wallet(update, context)


async def wallet_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_answer(update.callback_query)
    await _send_wallet(update, context)


async def _send_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = config.get_settings()
    user_id = update.effective_user.id
    wallet = await database.get_wallet(user_id)

    if not settings.escrow_enabled:
        await edit_or_reply(
            update,
            "ℹ️ Esta instancia funciona en <b>modo puntos</b> "
            f"(BET_MODE={esc(settings.mode)}). No hay fondos reales ni wallets que vincular.\n\n"
            "Para activar mercados TON configura BET_MODE=hybrid y ESCROW_ADDRESS.",
            reply_markup=wallet_keyboard(None),
        )
        return

    if wallet:
        try:
            friendly = to_friendly(wallet["address"])
        except Exception:  # noqa: BLE001 - defensive
            friendly = wallet["address"]
        text = (
            "👛 <b>Tu wallet vinculada</b>\n\n"
            f"Dirección: {code(friendly)}\n"
            f"Red: {bold(wallet.get('network', 'ton'))}\n"
            f"Verificada: {esc(wallet.get('verified_at', '—'))}\n\n"
            f"Contrato escrow: {code(settings.escrow_address)}\n\n"
            "Puedes desvincularla cuando quieras: tus fondos siguen en la blockchain "
            "y podrás reclamarlos directamente desde el contrato."
        )
    else:
        text = WALLET_EXPLAINER

    await edit_or_reply(update, text, reply_markup=wallet_keyboard(wallet))


async def wallet_unlink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query, "Wallet desvinculada")
    await database.unlink_wallet(query.from_user.id)
    await edit_or_reply(
        update,
        "🔓 Wallet desvinculada de esta cuenta de Telegram.\n\n"
        "<b>Importante:</b> esto no toca tus fondos. Si tienes apuestas abiertas, "
        "podrás reclamarlas conectando la misma wallet de nuevo o llamando al contrato "
        "desde cualquier wallet compatible.",
        reply_markup=wallet_keyboard(None),
    )
