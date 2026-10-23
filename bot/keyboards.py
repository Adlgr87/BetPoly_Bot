"""Inline keyboard helpers for BetPoly_Bot.

Telegram limits ``callback_data`` to 64 bytes, so callback payloads stay short
and always carry the numeric ids only.
"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import config


def _webapp(url: str, label: str = "🚀 Abrir Mini App") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))


def _esc_label(text: str, size: int = 34) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


# ---------------------------------------------------------------------------
# Start / main menu
# ---------------------------------------------------------------------------

def main_menu_keyboard(*, is_admin: bool = False, wallet=None) -> InlineKeyboardMarkup:
    settings = config.get_settings()
    rows: list[list[InlineKeyboardButton]] = []

    if settings.escrow_enabled:
        rows.append([_webapp(settings.webapp_url, "⚡ Apostar TON (Mini App)")])

    points_row: list[InlineKeyboardButton] = []
    if settings.points_enabled:
        points_row.append(
            InlineKeyboardButton("📋 Predicciones", callback_data="list_predictions:0")
        )
        points_row.append(InlineKeyboardButton("➕ Crear", callback_data="cmd_crear"))
    if points_row:
        rows.append(points_row)

    rows.append(
        [
            InlineKeyboardButton("🏆 Ranking", callback_data="ranking"),
            InlineKeyboardButton("📊 Perfil", callback_data="my_bets"),
        ]
    )
    if settings.escrow_enabled:
        wallet_label = "👛 Wallet vinculada" if wallet else "👛 Vincular wallet"
        rows.append(
            [
                InlineKeyboardButton(wallet_label, callback_data="wallet_info"),
                InlineKeyboardButton("🌐 Mercados", callback_data="list_markets:0"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("❓ Ayuda", callback_data="help"),
            InlineKeyboardButton("🔒 Cómo es no-custodial", callback_data="how_it_works"),
        ]
    )
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Off-chain predictions (virtual points)
# ---------------------------------------------------------------------------

def predictions_list_keyboard(
    page: int, total_pages: int, predictions: list[dict[str, Any]]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pred in predictions:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🎲 {_esc_label(pred['title'])}", callback_data=f"bet_select:{pred['id']}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️", callback_data=f"list_predictions:{page - 1}")
        )
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton("➡️", callback_data=f"list_predictions:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menú", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def options_keyboard(prediction_id: int, options: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                _esc_label(opt["label"], 40),
                callback_data=f"bet_option:{prediction_id}:{opt['id']}",
            )
        ]
        for opt in options
    ]
    rows.append(
        [InlineKeyboardButton("🔙 Volver", callback_data="list_predictions:0")]
    )
    return InlineKeyboardMarkup(rows)


def confirm_bet_keyboard(prediction_id: int, option_id: int, amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirmar",
                    callback_data=f"bet_confirm:{prediction_id}:{option_id}:{amount}",
                ),
                InlineKeyboardButton(
                    "❌ Cancelar", callback_data=f"bet_select:{prediction_id}"
                ),
            ]
        ]
    )


def cancel_conversation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancelar", callback_data="create_cancel")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")],
        ]
    )


def confirm_create_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data="create_confirm"),
                InlineKeyboardButton("❌ Cancelar", callback_data="create_cancel"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# On-chain markets
# ---------------------------------------------------------------------------

def markets_list_keyboard(
    page: int, total_pages: int, markets_list: list[dict[str, Any]]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for market in markets_list:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔒 {_esc_label(market['title'])}",
                    callback_data=f"market_view:{market['market_id']}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"list_markets:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list_markets:{page + 1}"))
    if nav:
        rows.append(nav)
    settings = config.get_settings()
    if settings.escrow_enabled:
        rows.append([_webapp(settings.webapp_url, "🚀 Abrir Mini App para apostar")])
    rows.append([InlineKeyboardButton("🏠 Menú", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def market_detail_keyboard(market: dict[str, Any]) -> InlineKeyboardMarkup:
    settings = config.get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if settings.escrow_enabled:
        rows.append([_webapp(settings.webapp_url, "🎲 Apostar en la Mini App")])
    if market.get("status") == "open":
        rows.append(
            [InlineKeyboardButton("🔄 Actualizar", callback_data=f"market_view:{market['market_id']}")]
        )
    rows.append([InlineKeyboardButton("🔙 Mercados", callback_data="list_markets:0")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_predictions_keyboard(
    page: int, total_pages: int, predictions: list[dict], *, onchain: bool = True
) -> InlineKeyboardMarkup:
    """Predicciones pendientes de una página (F-08).

    ``predictions`` es ya el trozo de la página: el llamador pagina y este
    teclado solo añade la navegación, igual que ``predictions_list_keyboard``.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"📌 {_esc_label(p['title'], 36)}", callback_data=f"admin_resolve:{p['id']}"
            )
        ]
        for p in predictions
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_preds:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_preds:{page + 1}"))
    if nav:
        rows.append(nav)
    if onchain:
        rows.append([InlineKeyboardButton("🔒 Mercados on-chain", callback_data="admin_chain:0")])
    rows.append([InlineKeyboardButton("🔙 Salir", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def admin_options_keyboard(pred_id: int, options: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"✅ {_esc_label(opt['label'], 36)}",
                callback_data=f"admin_winner:{pred_id}:{opt['id']}",
            )
        ]
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


def admin_chain_markets_keyboard(
    page: int, total_pages: int, markets_list: list[dict[str, Any]]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"🔒 {_esc_label(m['title'], 34)}",
                callback_data=f"admin_chain_market:{m['market_id']}",
            )
        ]
        for m in markets_list
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_chain:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_chain:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def admin_chain_options_keyboard(market_id: int, options: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"🏁 {_esc_label(opt['label'], 34)}",
                callback_data=f"admin_chain_win:{market_id}:{opt['idx']}",
            )
        ]
        for opt in options
    ]
    rows.append(
        [InlineKeyboardButton("🔙 Volver", callback_data=f"admin_chain_market:{market_id}")]
    )
    return InlineKeyboardMarkup(rows)


def admin_chain_market_keyboard(market_id: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "open":
        rows.append(
            [InlineKeyboardButton("🏁 Resolver", callback_data=f"admin_chain_options:{market_id}")]
        )
        rows.append(
            [InlineKeyboardButton("🛑 Cancelar mercado", callback_data=f"admin_chain_cancel:{market_id}")]
        )
    rows.append([InlineKeyboardButton("🔄 Actualizar", callback_data=f"admin_chain_market:{market_id}")])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data="admin_chain:0")])
    return InlineKeyboardMarkup(rows)


def admin_contract_keyboard(state: Any) -> InlineKeyboardMarkup:
    """Actions for the escrow contract itself (fees, pause, admin)."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "💸 Retirar comisiones", callback_data="admin_contract_withdraw"
            )
        ],
        [
            InlineKeyboardButton("⚖️ Cambiar comisión", callback_data="admin_contract_setfee"),
        ],
        [
            InlineKeyboardButton(
                "▶️ Reanudar contrato" if state.is_paused else "⏸ Pausar contrato",
                callback_data=f"admin_contract_pause:{0 if state.is_paused else 1}",
            )
        ],
        [InlineKeyboardButton("👤 Cambiar administrador", callback_data="admin_contract_changeadmin")],
        [InlineKeyboardButton("🔄 Actualizar estado", callback_data="admin_contract")],
        [InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")],
    ]
    return InlineKeyboardMarkup(rows)


def confirm_contract_action_keyboard(action: str) -> InlineKeyboardMarkup:
    """Explicit confirmation for irreversible contract actions.

    ``callback_data`` only carries the action: a TON address would blow past the
    64-byte limit, so the value waits in the conversation state.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirmar", callback_data=f"admin_contract_ok:{action}"
                )
            ],
            [InlineKeyboardButton("❌ Cancelar", callback_data="admin_contract")],
        ]
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def back_to_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Teclado de vuelta al menú, con la fila de administración cuando toca.

    Antes el parámetro ``is_admin`` se aceptaba y se ignoraba, así que el botón
    «🔧 Admin» desaparecía en cuanto el admin navegaba una pantalla (F-01).
    """
    rows: list[list[InlineKeyboardButton]] = []
    settings = config.get_settings()
    if settings.escrow_enabled:
        rows.append([_webapp(settings.webapp_url, "🚀 Mini App")])
    rows.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")])
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def refresh_ranking_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔄 Actualizar", callback_data="ranking")],
        [InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def help_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📖 ¿Cómo funciona?", callback_data="how_it_works")]]
    settings = config.get_settings()
    if settings.escrow_enabled:
        rows.insert(0, [_webapp(settings.webapp_url, "🚀 Abrir Mini App")])
    rows.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")])
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def wallet_keyboard(wallet: dict[str, Any] | None) -> InlineKeyboardMarkup:
    settings = config.get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if settings.escrow_enabled:
        rows.append([_webapp(settings.webapp_url, "👛 Vincular wallet en la Mini App")])
    if wallet:
        rows.append(
            [InlineKeyboardButton("🔓 Desvincular", callback_data="wallet_unlink")]
        )
    rows.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)
