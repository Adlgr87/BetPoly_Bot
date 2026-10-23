"""Handler for /help and the non-custodial explainer — F009."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot.formatting import bold, code, ton
from bot.handlers.common import safe_answer
from bot.keyboards import help_keyboard


def build_help_text() -> str:
    settings = config.get_settings()
    lines = [
        "❓ <b>Comandos disponibles</b>",
        "",
        "🚀 /start — Menú principal",
    ]
    if settings.escrow_enabled:
        lines += [
            "👛 /wallet — Vincular o revisar tu wallet TON",
            "🌐 /mercados — Mercados on-chain (solo lectura)",
        ]
    if settings.points_enabled:
        lines += [
            "➕ /crear — Crear una predicción de puntos",
            "📋 /predicciones — Ver predicciones abiertas",
        ]
    lines += [
        "📊 /misbets — Perfil, apuestas y portfolio",
        "🏆 /ranking — Top 10 por puntos",
        "🔧 /admin — Panel de administrador",
        "❓ /help — Este mensaje",
    ]
    return "\n".join(lines)


def build_how_it_works() -> str:
    settings = config.get_settings()
    lines = ["🛡️ <b>Cómo funciona y por qué es no-custodial</b>", ""]

    if settings.escrow_enabled:
        lines += [
            "🔒 <b>Mercados TON (dinero real)</b>",
            "1. Conectas tu wallet con TON Connect dentro de la Mini App.",
            "2. Eliges mercado, opción e importe. Tu wallet firma la transacción.",
            "3. El TON viaja de tu wallet al <b>contrato escrow</b>: nunca a una cuenta del bot.",
            "4. Cuando el mercado cierra, el administrador firma la resolución on-chain.",
            "5. Cada ganador reclama su pago; el contrato lo envía a su wallet.",
            "",
            "El bot solo tiene una dirección pública del contrato y la lee. "
            "No tiene claves, no puede firmar por ti, no puede congelar retiros. "
            "Si el servidor desaparece, tus fondos siguen en el contrato y puedes "
            "reclamarlos con cualquier wallet compatible.",
            "",
            f"Contrato: {code(settings.escrow_address or '—')}",
            f"Apuesta mínima: {bold(ton(settings.min_stake_nano))} TON "
            f"+ {bold(ton(30_000_000))} TON de gas.",
            "",
        ]

    if settings.points_enabled:
        lines += [
            "🪙 <b>Modo puntos (sandbox, sin dinero)</b>",
            f"1. Empiezas con {bold(settings.starting_points)} puntos virtuales.",
            "2. Apuestas a una opción; los puntos se descuentan al confirmar.",
            "3. Al cerrar, un admin declara la opción ganadora.",
            "4. El pozo se reparte <b>pari-mutuel</b>: los ganadores se llevan todo "
            "el pozo en proporción a lo apostado.",
            "Si nadie acierta, se reembolsa a todos. No hay creación de puntos de la nada.",
            "",
        ]

    lines += [
        "🔑 <b>Seguridad</b>",
        "• El bot jamás te pedirá tu frase semilla ni tu clave privada.",
        "• Firmar un mensaje de vinculación no cuesta gas ni mueve fondos.",
        "• Cualquiera que te pida la semilla «para verificar tu wallet» es un estafador.",
        "",
        "ℹ️ Los puntos son virtuales y no son canjeables por dinero.",
    ]
    return "\n".join(lines)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        build_help_text(), parse_mode="HTML", reply_markup=help_keyboard()
    )


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    from bot.handlers.common import edit_or_reply

    await edit_or_reply(update, build_help_text(), reply_markup=help_keyboard())


async def how_it_works_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    from bot.handlers.common import edit_or_reply
    from bot.keyboards import back_to_menu_keyboard

    await edit_or_reply(update, build_how_it_works(), reply_markup=back_to_menu_keyboard())
