"""Shared helpers for Telegram handlers."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest

import config
from bot import database, security
from bot.keyboards import back_to_menu_keyboard, main_menu_keyboard

logger = logging.getLogger(__name__)

bet_limiter = security.RateLimiter(limit=10, window_seconds=60)
create_limiter = security.RateLimiter(limit=5, window_seconds=3600)
notify_limiter = security.RateLimiter(limit=20, window_seconds=60)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and int(user_id) in config.get_settings().admin_ids


def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def register_user(update: Update) -> None:
    user = update.effective_user
    if user is None:
        return
    await database.upsert_user(user.id, user.username, user.full_name)


def menu_for(update: Update, wallet=None):
    """Menú principal resuelto con el rol de quien mira (F-01/F-14).

    Todas las pantallas pasan por aquí: un admin nunca pierde el botón «🔧 Admin»
    y un usuario normal nunca lo ve.
    """
    return main_menu_keyboard(is_admin=is_admin(_user_id(update)), wallet=wallet)


def back_keyboard(update: Update):
    """Teclado «volver al menú» con la misma regla de rol que :func:`menu_for`."""
    return back_to_menu_keyboard(is_admin=is_admin(_user_id(update)))


def _user_id(update: Update):
    user = getattr(update, "effective_user", None)
    return getattr(user, "id", None)


async def safe_answer(query, text: str | None = None, alert: bool = False) -> None:
    """Answer a callback query, ignoring 'already answered' races."""
    try:
        await query.answer(text=text, show_alert=alert)
    except BadRequest as exc:  # pragma: no cover - Telegram race
        logger.debug("callback answer ignored: %s", exc)


async def edit_or_reply(
    update: Update,
    text: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = True,
):
    """Edit the callback message when possible, otherwise reply.

    Devuelve el mensaje resultante: el modo grupo lo necesita para exigir que
    la respuesta del usuario sea a *ese* mensaje (F-02).
    """
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            return await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return query.message
            if "no text in the message" not in str(exc).lower():
                logger.debug("edit failed, falling back to reply: %s", exc)
    message = update.effective_message
    if message is not None:
        return await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    return None


def rate_limited(update: Update, limiter: security.RateLimiter, bucket: str) -> bool:
    user = update.effective_user
    key = f"{bucket}:{user.id if user else 'anon'}"
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        logger.info("Rate limited %s (retry in %.0fs)", key, retry_after)
    return not allowed
