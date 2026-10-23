"""Handlers for off-chain predictions — F002, F003, F004.

Everything here is the *sandbox* economy: virtual points, no money, no custody.
The on-chain markets live in :mod:`bot.handlers.markets`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from bot import database, economics, parsing
from bot.formatting import bold, code, esc, fmt_local, parse_local_datetime
from bot.formatting import points as fmt_points
from bot.handlers.common import (
    back_keyboard,
    bet_limiter,
    create_limiter,
    edit_or_reply,
    is_group,
    menu_for,
    rate_limited,
    register_user,
    safe_answer,
)
from bot.keyboards import (
    cancel_conversation_keyboard,
    confirm_bet_keyboard,
    confirm_create_keyboard,
    options_keyboard,
    predictions_list_keyboard,
)
from bot.markets import MIN_TITLE_LENGTH, MarketError

logger = logging.getLogger(__name__)

ASK_TITLE, ASK_OPTIONS, ASK_CLOSES_AT, CONFIRM_CREATE = range(4)
PAGE_SIZE = 4
MAX_TITLE = 160
MAX_OPTION = 60
MAX_DURATION = timedelta(days=365)


def example_close(*, days: int = 7) -> str:
    """Fecha de ejemplo para el formulario: nunca una fecha ya pasada (U-02)."""
    return fmt_local(datetime.now(timezone.utc) + timedelta(days=days))


# ---------------------------------------------------------------------------
# F003 — list open predictions
# ---------------------------------------------------------------------------

async def predicciones_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await _show_predictions_page(update, context, page=0)


async def list_predictions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    try:
        page = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _show_predictions_page(update, context, page=page)


def _scope_chat_id(update: Update):
    return update.effective_chat.id if is_group(update) else None


async def _show_predictions_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    settings = config.get_settings()
    if not settings.points_enabled:
        await edit_or_reply(
            update,
            "ℹ️ Este bot funciona solo con mercados on-chain. Abre la Mini App para apostar TON.",
            reply_markup=menu_for(update),
        )
        return

    predictions = await database.get_open_predictions(chat_id=_scope_chat_id(update))
    if not predictions:
        await edit_or_reply(
            update,
            "📭 No hay predicciones abiertas ahora mismo.\n\n"
            "Crea la primera con /crear o apuesta TON en la Mini App.",
            reply_markup=menu_for(update),
        )
        return

    total_pages = max(1, (len(predictions) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = predictions[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"📋 <b>Predicciones abiertas</b> ({page + 1}/{total_pages})", ""]
    for index, pred in enumerate(chunk, start=page * PAGE_SIZE + 1):
        closes = fmt_local(datetime.fromisoformat(pred["closes_at"]))
        # El pozo viene en la misma fila (R-01): cero consultas por predicción.
        lines.append(
            f"{index}. <b>{esc(pred['title'])}</b>\n"
            f"    ⏰ Cierra: {code(closes)} · 🪙 pozo: {bold(fmt_points(pred['total_pool']))}"
        )
    lines += ["", "Toca una predicción para apostar tus puntos."]

    await edit_or_reply(
        update,
        "\n".join(lines),
        reply_markup=predictions_list_keyboard(page, total_pages, chunk),
    )


# ---------------------------------------------------------------------------
# F004 — bet
# ---------------------------------------------------------------------------

async def bet_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    try:
        pred_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        return

    prediction = await database.get_prediction(pred_id)
    if not prediction or prediction["status"] != "open":
        await edit_or_reply(
            update,
            "⚠️ Esta predicción ya no está abierta.",
            reply_markup=back_keyboard(update),
        )
        return

    existing = await database.get_user_bet(pred_id, query.from_user.id)
    options = await database.get_options(pred_id)
    pools = await database.get_prediction_pools(pred_id)
    total_pool = pools.pop(-1, 0)
    lines = [
        f"🎲 <b>{esc(prediction['title'])}</b>",
        f"⏰ Cierra: {code(fmt_local(datetime.fromisoformat(prediction['closes_at'])))}",
        f"🪙 Pozo total: {bold(fmt_points(total_pool))} puntos",
        "",
    ]
    if existing:
        lines.append(
            f"ℹ️ Ya apostaste <b>{fmt_points(existing['amount'])}</b> puntos aquí "
            f"(una apuesta por predicción)."
        )
    lines.append("Elige la opción por la que quieres apostar:")
    await edit_or_reply(update, "\n".join(lines), reply_markup=options_keyboard(pred_id, options))


async def bet_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    try:
        _, pred_id_raw, opt_id_raw = query.data.split(":")
        pred_id, opt_id = int(pred_id_raw), int(opt_id_raw)
    except (ValueError, TypeError):
        return

    settings = config.get_settings()
    prediction = await database.get_prediction(pred_id)
    options = await database.get_options(pred_id)
    if not prediction or not any(o["id"] == opt_id for o in options):
        await edit_or_reply(update, "⚠️ Opción no válida.", reply_markup=back_keyboard(update))
        return
    if prediction["status"] != "open":
        await edit_or_reply(update, "⚠️ La predicción ya cerró.", reply_markup=back_keyboard(update))
        return

    user = await database.get_user(query.from_user.id)
    balance = int(user["points"]) if user else 0
    context.user_data["pending_bet"] = {"pred_id": pred_id, "opt_id": opt_id}
    context.user_data["awaiting_bet_amount"] = True

    hint = ""
    if is_group(update):
        hint = "\n\n<i>En grupo, responde a este mensaje con el importe.</i>"
    sent = await edit_or_reply(
        update,
        f"💰 Saldo disponible: <b>{fmt_points(balance)} puntos</b>\n"
        f"🎯 Predicción: {esc(prediction['title'])}\n"
        f"✅ Opción: <b>{esc(next(o['label'] for o in options if o['id'] == opt_id))}</b>\n\n"
        f"Escribe cuántos puntos quieres apostar "
        f"(mínimo {bold(settings.min_bet_points)}, máximo {bold(fmt_points(balance))}).\n"
        f"Envía /cancelar para salir.{hint}",
        reply_markup=back_keyboard(update),
    )
    # En grupo, solo se acepta una respuesta a *este* mensaje (F-02).
    context.user_data.pop("amount_prompt_id", None)
    if is_group(update) and sent is not None:
        context.user_data["amount_prompt_id"] = getattr(sent, "message_id", None)


def _amount_is_for_us(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """En grupo, el importe solo cuenta si responde al mensaje del bot (F-02)."""
    if not is_group(update):
        return True
    prompt_id = context.user_data.get("amount_prompt_id")
    reply_to = getattr(update.effective_message, "reply_to_message", None)
    return bool(prompt_id) and reply_to is not None and getattr(
        reply_to, "message_id", None
    ) == prompt_id


async def bet_amount_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_bet_amount"):
        return  # ignoramos texto suelto sin flujo activo
    if not _amount_is_for_us(update, context):
        return  # en un grupo, no es una respuesta a nuestro prompt
    if rate_limited(update, bet_limiter, "bet_amount"):
        await update.effective_message.reply_text("⏳ Vas muy rápido. Espera unos segundos.")
        return

    pending = context.user_data.get("pending_bet") or {}
    pred_id, opt_id = pending.get("pred_id"), pending.get("opt_id")
    if not pred_id or opt_id is None:
        context.user_data["awaiting_bet_amount"] = False
        context.user_data.pop("amount_prompt_id", None)
        await update.effective_message.reply_text(
            "❌ Se perdió el contexto. Vuelve a empezar con /predicciones."
        )
        return

    prediction = await database.get_prediction(pred_id)
    user = await database.get_user(update.effective_user.id)
    if not prediction or not user:
        await update.effective_message.reply_text("❌ Predicción no encontrada.")
        context.user_data["awaiting_bet_amount"] = False
        return

    settings = config.get_settings()
    balance = int(user["points"])
    try:
        amount = parsing.parse_points(update.effective_message.text, maximum=balance)
    except MarketError as exc:
        await update.effective_message.reply_text(f"❌ {esc(exc)}")
        return
    if amount < settings.min_bet_points:
        await update.effective_message.reply_text(
            f"❌ La apuesta mínima es de {settings.min_bet_points} puntos."
        )
        return

    options = await database.get_options(pred_id)
    label = next((o["label"] for o in options if o["id"] == opt_id), "?")
    pools = await database.get_prediction_pools(pred_id)
    option_pool = pools.get(opt_id, 0)
    total_pool = pools.get(-1, 0)

    # Una sola economía (F-03): el pago que se promete es el que se liquidará.
    potential = economics.preview_payout(
        amount, option_pool, total_pool, settings.house_rake_bps
    )
    multiplier = economics.multiplier(
        total_pool + amount, option_pool + amount, settings.house_rake_bps
    )
    potential_line = (
        f"\n📈 Si ganas: <b>{fmt_points(potential)}</b> puntos"
        + (f" (x{multiplier:.2f})" if multiplier else "")
        + "\n"
    )

    context.user_data["awaiting_bet_amount"] = False
    context.user_data.pop("amount_prompt_id", None)
    await update.effective_message.reply_text(
        "📝 <b>Resumen de tu apuesta</b>\n\n"
        f"🎯 {esc(prediction['title'])}\n"
        f"✅ Opción: <b>{esc(label)}</b>\n"
        f"🪙 Puntos: <b>{fmt_points(amount)}</b>\n"
        f"{potential_line}\n"
        "El pago es pari-mutuel: los ganadores se reparten todo el pozo "
        "en proporción a lo apostado.",
        parse_mode="HTML",
        reply_markup=confirm_bet_keyboard(pred_id, opt_id, amount),
    )


async def bet_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    try:
        _, pred_id_raw, opt_id_raw, amount_raw = query.data.split(":")
        pred_id, opt_id, amount = int(pred_id_raw), int(opt_id_raw), int(amount_raw)
    except (ValueError, TypeError):
        return

    if rate_limited(update, bet_limiter, "bet_confirm"):
        await edit_or_reply(update, "⏳ Espera un momento antes de apostar otra vez.")
        return

    user = query.from_user
    await register_user(update)
    try:
        balance = await database.place_bet(user.id, pred_id, opt_id, amount)
    except database.InsufficientFunds as exc:
        await edit_or_reply(update, f"❌ {esc(exc)}", reply_markup=back_keyboard(update))
        return
    except (database.DuplicateBet, database.DatabaseError) as exc:
        await edit_or_reply(update, f"❌ {esc(exc)}", reply_markup=back_keyboard(update))
        return

    context.user_data.pop("pending_bet", None)
    context.user_data["awaiting_bet_amount"] = False
    await edit_or_reply(
        update,
        "🎉 <b>¡Apuesta registrada!</b>\n\n"
        f"🪙 Apostado: <b>{fmt_points(amount)}</b>\n"
        f"💰 Saldo restante: <b>{fmt_points(balance)}</b>\n\n"
        "¡Mucha suerte! 🤞",
        reply_markup=back_keyboard(update),
    )


async def cancel_bet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_bet", None)
    context.user_data["awaiting_bet_amount"] = False
    await update.effective_message.reply_text(
        "❌ Apuesta cancelada.", reply_markup=menu_for(update)
    )


# ---------------------------------------------------------------------------
# F002 — create prediction (conversation)
# ---------------------------------------------------------------------------

async def crear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = config.get_settings()
    if not settings.points_enabled:
        await edit_or_reply(
            update,
            "ℹ️ En este bot las predicciones se crean on-chain desde la Mini App.",
            reply_markup=menu_for(update),
        )
        return ConversationHandler.END

    if rate_limited(update, create_limiter, "create"):
        await edit_or_reply(update, "⏳ Has creado demasiadas predicciones. Prueba más tarde.")
        return ConversationHandler.END

    await register_user(update)
    context.user_data.clear()
    text = (
        "➕ <b>Crear predicción — Paso 1/3</b>\n\n"
        "Escribe el <b>título</b> o pregunta:\n"
        "<i>Ej: ¿Ganará el Real Madrid el próximo partido?</i>"
    )
    if update.callback_query:
        await safe_answer(update.callback_query)
        await edit_or_reply(update, text, reply_markup=cancel_conversation_keyboard())
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=cancel_conversation_keyboard()
        )
    return ASK_TITLE


async def ask_title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = " ".join((update.effective_message.text or "").split())
    if len(title) < MIN_TITLE_LENGTH:
        await update.effective_message.reply_text(
            "❌ El título debe tener al menos 6 caracteres. Inténtalo de nuevo."
        )
        return ASK_TITLE
    if len(title) > MAX_TITLE:
        await update.effective_message.reply_text(
            f"❌ El título no puede superar {MAX_TITLE} caracteres."
        )
        return ASK_TITLE
    context.user_data["new_pred_title"] = title
    await update.effective_message.reply_text(
        "✅ Título guardado.\n\n"
        "➕ <b>Paso 2/3</b>\n\n"
        "Escribe las <b>opciones</b> separadas por comas:\n"
        "<i>Ej: Sí, No, Empate</i>",
        parse_mode="HTML",
        reply_markup=cancel_conversation_keyboard(),
    )
    return ASK_OPTIONS


async def ask_options_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = config.get_settings()
    raw = update.effective_message.text or ""
    options: list[str] = []
    for chunk in raw.split(","):
        label = " ".join(chunk.split())
        if label:
            options.append(label)

    if not database.MIN_OPTIONS <= len(options) <= settings.max_options:
        await update.effective_message.reply_text(
            f"❌ Necesitas entre 2 y {settings.max_options} opciones separadas por comas."
        )
        return ASK_OPTIONS
    if len({o.lower() for o in options}) != len(options):
        await update.effective_message.reply_text("❌ Las opciones deben ser distintas.")
        return ASK_OPTIONS
    if any(len(o) > MAX_OPTION for o in options):
        await update.effective_message.reply_text(
            f"❌ Cada opción puede tener hasta {MAX_OPTION} caracteres."
        )
        return ASK_OPTIONS

    context.user_data["new_pred_options"] = options
    now_local = fmt_local(datetime.now(timezone.utc))
    await update.effective_message.reply_text(
        "✅ Opciones guardadas.\n\n"
        "➕ <b>Paso 3/3</b>\n\n"
        "Escribe la fecha y hora de cierre en formato "
        f"{code('DD/MM/YYYY HH:MM')} (zona {code(config.get_settings().timezone)}):\n"
        f"<i>Ej: {esc(example_close())} — ahora son las {esc(now_local)}</i>",
        parse_mode="HTML",
        reply_markup=cancel_conversation_keyboard(),
    )
    return ASK_CLOSES_AT


async def ask_closes_at_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    try:
        closes_at = parse_local_datetime(raw)
    except ValueError:
        await update.effective_message.reply_text(
            f"❌ Formato inválido. Usa DD/MM/YYYY HH:MM, por ejemplo {example_close()}."
        )
        return ASK_CLOSES_AT

    now = datetime.now(timezone.utc)
    if closes_at <= now + timedelta(minutes=10):
        await update.effective_message.reply_text(
            "❌ La fecha de cierre debe ser al menos 10 minutos en el futuro."
        )
        return ASK_CLOSES_AT
    if closes_at > now + MAX_DURATION:
        await update.effective_message.reply_text("❌ El cierre no puede estar a más de un año.")
        return ASK_CLOSES_AT

    context.user_data["new_pred_closes_at"] = closes_at.isoformat()
    title = context.user_data.get("new_pred_title", "")
    options = context.user_data.get("new_pred_options", [])
    opts_text = "\n".join(f"   • {esc(o)}" for o in options)
    await update.effective_message.reply_text(
        "📝 <b>Resumen de la predicción</b>\n\n"
        f"🎯 <b>{esc(title)}</b>\n"
        f"📌 Opciones:\n{opts_text}\n"
        f"⏰ Cierre: {code(fmt_local(closes_at))}\n\n"
        "¿Confirmas la creación?",
        parse_mode="HTML",
        reply_markup=confirm_create_keyboard(),
    )
    return CONFIRM_CREATE


async def confirm_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer(query)

    if query.data == "create_cancel":
        context.user_data.clear()
        await edit_or_reply(
            update, "❌ Predicción cancelada.", reply_markup=menu_for(update)
        )
        return ConversationHandler.END

    title = context.user_data.get("new_pred_title")
    options = context.user_data.get("new_pred_options")
    closes_raw = context.user_data.get("new_pred_closes_at")
    if not title or not options or not closes_raw:
        await edit_or_reply(
            update, "❌ Se perdió el contexto. Empieza de nuevo con /crear."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await register_user(update)
    try:
        pred_id = await database.create_prediction(
            creator_id=query.from_user.id,
            chat_id=update.effective_chat.id,
            title=title,
            options=options,
            closes_at=datetime.fromisoformat(closes_raw),
        )
    except database.DatabaseError as exc:
        await edit_or_reply(update, f"❌ {esc(exc)}")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()
    await edit_or_reply(
        update,
        "🎉 <b>¡Predicción creada!</b>\n\n"
        f"🆔 {code(pred_id)}\n"
        f"🎯 <b>{esc(title)}</b>\n\n"
        "Los demás ya pueden apostar con /predicciones.",
        reply_markup=back_keyboard(update),
    )
    return ConversationHandler.END


async def cancel_conv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "❌ Operación cancelada.", reply_markup=menu_for(update)
    )
    return ConversationHandler.END


async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "⌛ El formulario expiró por inactividad. Empieza de nuevo con /crear."
    )
    return ConversationHandler.END


def build_crear_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("crear", crear_handler),
            # The inline menu button must enter the conversation, otherwise the
            # next plain-text message would be consumed by the global handler.
            CallbackQueryHandler(crear_handler, pattern=r"^cmd_crear$"),
        ],
        states={
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_title_handler)],
            ASK_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_options_handler)],
            ASK_CLOSES_AT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_closes_at_handler)],
            CONFIRM_CREATE: [
                CallbackQueryHandler(confirm_create_callback, pattern=r"^create_(confirm|cancel)$")
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, timeout_handler),
                CallbackQueryHandler(timeout_handler),
            ],
        },
        fallbacks=[
            CommandHandler(["cancelar", "cancel"], cancel_conv_handler),
            CallbackQueryHandler(
                confirm_create_callback, pattern=r"^create_cancel$"
            ),
        ],
        allow_reentry=True,
        conversation_timeout=900,
        per_user=True,
        per_chat=True,
    )


# Kept as a public alias for backwards compatibility with older imports.
cmd_crear_callback = crear_handler
