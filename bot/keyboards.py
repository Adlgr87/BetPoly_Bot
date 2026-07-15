"""Inline keyboard helpers for BetPoly_Bot."""
from __future__ import annotations

from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# Start / main menu
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Ver Predicciones", callback_data="list_predictions:0"),
                InlineKeyboardButton("➕ Crear Predicción", callback_data="cmd_crear"),
            ],
            [
                InlineKeyboardButton("🏆 Ranking", callback_data="ranking"),
                InlineKeyboardButton("📊 Mis Apuestas", callback_data="my_bets"),
            ],
            [
                InlineKeyboardButton("❓ Ayuda", callback_data="help"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Prediction list (paginated)
# ---------------------------------------------------------------------------

def predictions_list_keyboard(page: int, total_pages: int, pred_ids: List[int]) -> InlineKeyboardMarkup:
    rows = []
    for pred_id in pred_ids:
        rows.append(
            [InlineKeyboardButton(f"🎲 Apostar", callback_data=f"bet_select:{pred_id}")]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"list_predictions:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"list_predictions:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Bet flow
# ---------------------------------------------------------------------------

def options_keyboard(prediction_id: int, options: List[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(opt["label"], callback_data=f"bet_option:{prediction_id}:{opt['id']}")]
        for opt in options
    ]
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data="list_predictions:0")])
    return InlineKeyboardMarkup(rows)


def confirm_bet_keyboard(prediction_id: int, option_id: int, amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirmar apuesta",
                    callback_data=f"bet_confirm:{prediction_id}:{option_id}:{amount}",
                ),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"bet_select:{prediction_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_predictions_keyboard(predictions: List[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📌 {p['title'][:40]}", callback_data=f"admin_resolve:{p['id']}")]
        for p in predictions
    ]
    rows.append([InlineKeyboardButton("🔙 Salir", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def admin_options_keyboard(pred_id: int, options: List[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"✅ {opt['label']}", callback_data=f"admin_winner:{pred_id}:{opt['id']}")]
        for opt in options
    ]
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def confirm_resolve_keyboard(pred_id: int, option_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirmar resolución",
                    callback_data=f"admin_confirm:{pred_id}:{option_id}",
                ),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"admin_resolve:{pred_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")]]
    )


def refresh_ranking_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Actualizar", callback_data="ranking")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")],
        ]
    )
