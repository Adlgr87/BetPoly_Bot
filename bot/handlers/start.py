"""Handler for /start — F001 onboarding + non-custodial explanation."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import database
from bot.formatting import bold, esc, points, ton
from bot.handlers.common import is_admin, register_user
from bot.keyboards import main_menu_keyboard


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await register_user(update)
    context.user_data.pop("awaiting_bet_amount", None)
    context.user_data.pop("pending_bet", None)

    settings = config.get_settings()
    record = await database.get_user(user.id)
    wallet = await database.get_wallet(user.id) if settings.escrow_enabled else None

    lines = [
        f"👋 ¡Hola, {bold(user.first_name)}!",
        "",
        f"Bienvenido a {bold(settings.app_title)}, predicciones y mercados entre amigos.",
        "",
        f"🪙 <b>Modo puntos</b>: empiezas con {bold(points(record['points'] if record else settings.starting_points))} puntos virtuales. "
        "No hay dinero real aquí: es el modo sandbox para jugar y competir en el ranking.",
    ]

    if settings.escrow_enabled:
        lines += [
            "",
            "🔒 <b>Modo TON (no-custodial)</b>",
            "Las apuestas con TON van de tu wallet directamente al contrato escrow. "
            "El bot <b>nunca</b> ve tu clave privada ni tu semilla, no puede mover tus fondos "
            "y no puede congelarlos: el contrato es el único juez.",
            "",
            "• Firma con tu wallet en la Mini App (TON Connect).",
            "• Una apuesta = una transacción que tú autorizas.",
            "• Resolución y pagos se ejecutan on-chain.",
            "",
            (
                f"👛 Wallet vinculada: {bold(wallet['address'])}"
                if wallet
                else "👛 Aún no has vinculado una wallet. Usa /wallet o la Mini App."
            ),
        ]

    if settings.points_enabled:
        lines += [
            "",
            f"🎲 Apuesta mínima en puntos: {bold(settings.min_bet_points)} · "
            f"comisión de la casa: {bold(f'{settings.house_rake_bps / 100:g}%')}",
        ]

    lines += ["", "Elige una opción para empezar 👇"]

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_admin=is_admin(user.id), wallet=wallet),
        disable_web_page_preview=True,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.handlers.common import edit_or_reply, safe_answer

    query = update.callback_query
    await safe_answer(query)
    user = query.from_user
    await register_user(update)
    settings = config.get_settings()
    wallet = await database.get_wallet(user.id) if settings.escrow_enabled else None
    record = await database.get_user(user.id)
    text = (
        "🏠 <b>Menú principal</b>\n\n"
        f"🪙 Saldo: {bold(points(record['points'] if record else 0))} puntos\n"
        + (
            f"👛 Wallet: {esc(wallet['address'])}\n" if wallet else "👛 Wallet: no vinculada\n"
        )
        + (
            f"🔒 Escrow TON: {bold(ton(settings.min_stake_nano))} TON mínimo por apuesta\n"
            if settings.escrow_enabled
            else ""
        )
        + "\nElige una opción:"
    )
    await edit_or_reply(
        update, text, reply_markup=main_menu_keyboard(is_admin=is_admin(user.id), wallet=wallet)
    )
