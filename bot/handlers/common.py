"""Shared helpers for Telegram handlers."""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest

import config
from bot import database, security

logger = logging.getLogger(__name__)

bet_limiter = security.RateLimiter(limit=10, window_seconds=60)
create_limiter = security.RateLimiter(limit=5, window_seconds=3600)
notify_limiter = security.RateLimiter(limit=20, window_seconds=60)


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and int(user_id) in config.get_settings().admin_ids


def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def register_user(update: Update) -> None:
    user = update.effective_user
    if user is None:
        return
    await database.upsert_user(user.id, user.username, user.full_name)


async def safe_answer(query, text: Optional[str] = None, alert: bool = False) -> None:
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
) -> None:
    """Edit the callback message when possible, otherwise reply."""
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            if "no text in the message" not in str(exc).lower():
                logger.debug("edit failed, falling back to reply: %s", exc)
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )


def rate_limited(update: Update, limiter: security.RateLimiter, bucket: str) -> bool:
    user = update.effective_user
    key = f"{bucket}:{user.id if user else 'anon'}"
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        logger.info("Rate limited %s (retry in %.0fs)", key, retry_after)
    return not allowed
