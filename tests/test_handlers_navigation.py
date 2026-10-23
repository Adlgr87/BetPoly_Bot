"""F-01/F-14: el botón «🔧 Admin» aparece siempre… y solo para administradores.

El bug original: ``main.py`` construía el menú sin rol y ``back_to_menu_keyboard``
ignoraba su parámetro ``is_admin``, así que el admin perdía el botón en cuanto
navegaba una pantalla. Esta tabla recorre todas las pantallas de menú.
"""
from __future__ import annotations

import pytest

from bot.handlers.admin import admin_resolve_callback
from bot.handlers.help import (
    build_help_text,
    help_callback,
    help_handler,
    how_it_works_callback,
)
from bot.handlers.predictions import (
    bet_option_callback,
    cancel_bet_handler,
    example_close,
    list_predictions_callback,
    predicciones_handler,
)
from bot.handlers.start import menu_callback, start_handler
from bot.handlers.stats import (
    misbets_handler,
    my_bets_callback,
    ranking_callback,
    ranking_handler,
)
from bot.keyboards import back_to_menu_keyboard
from tests.telegram_fakes import make_update

ADMIN = 1001  # ADMIN_IDS del conftest
MORTAL = 1


# (nombre, handler, tipo de update)
SCREENS = [
    ("start", start_handler, "text"),
    ("menu", menu_callback, "callback:main_menu"),
    ("help", help_handler, "text"),
    ("help_callback", help_callback, "callback:help"),
    ("how_it_works", how_it_works_callback, "callback:how_it_works"),
    ("ranking", ranking_handler, "text"),
    ("ranking_callback", ranking_callback, "callback:ranking"),
    ("misbets", misbets_handler, "text"),
    ("my_bets", my_bets_callback, "callback:my_bets"),
    ("predicciones", predicciones_handler, "text"),
    ("list_predictions", list_predictions_callback, "callback:list_predictions:0"),
    ("cancel_bet", cancel_bet_handler, "text"),
    ("admin_resolve_sin_opciones", admin_resolve_callback, "callback:admin_resolve:99"),
]


async def _run(db, handler, kind: str, user_id: int):
    await db.upsert_user(user_id, f"u{user_id}", f"User {user_id}")
    data = kind.split(":", 1)[1] if kind.startswith("callback:") else None
    update, context, recorder = make_update(user_id=user_id, data=data, text="/x")
    await handler(update, context)
    return recorder


@pytest.mark.parametrize("name,handler,kind", SCREENS, ids=[s[0] for s in SCREENS])
class TestAdminRowEverywhere:
    async def test_admin_keeps_the_admin_button(self, db, users, name, handler, kind):
        recorder = await _run(db, handler, kind, ADMIN)
        assert "admin_panel" in recorder.callbacks(), (
            f"{name} perdió la fila Admin para un administrador"
        )

    async def test_normal_user_never_sees_it(self, db, users, name, handler, kind):
        recorder = await _run(db, handler, kind, MORTAL)
        assert "admin_panel" not in recorder.callbacks(), (
            f"{name} ofrece el panel de admin a un usuario normal"
        )


class TestKeyboardHelper:
    def test_back_keyboard_includes_the_row_only_for_admins(self):
        admin = back_to_menu_keyboard(is_admin=True).inline_keyboard
        mortal = back_to_menu_keyboard(is_admin=False).inline_keyboard
        assert any(b.callback_data == "admin_panel" for row in admin for b in row)
        assert not any(b.callback_data == "admin_panel" for row in mortal for b in row)


class TestHelpPerRole:
    """U-06: /help solo promete lo que ese usuario puede usar."""

    def test_admin_sees_admin_command(self):
        assert "/admin" in build_help_text(is_admin=True)

    def test_normal_user_does_not(self):
        assert "/admin" not in build_help_text(is_admin=False)

    def test_both_see_the_public_commands(self):
        for is_admin in (True, False):
            text = build_help_text(is_admin=is_admin)
            assert "/start" in text and "/help" in text and "/ranking" in text


class TestDynamicDates:
    """U-02: el ejemplo de fecha de cierre es futuro, no una fecha de 2025."""

    def test_example_close_is_in_the_future(self):
        from datetime import datetime

        from bot.formatting import parse_local_datetime

        parsed = parse_local_datetime(example_close())
        assert parsed > datetime.now(parsed.tzinfo)

    async def test_creation_form_uses_the_dynamic_example(self, db, users):
        from bot.handlers.predictions import ask_options_handler

        update, context, recorder = make_update(user_id=1, text="Sí, No")
        context.user_data["new_pred_title"] = "¿Llueve mañana en Monterrey?"
        await ask_options_handler(update, context)
        assert example_close() in recorder.last["text"]
        assert "25/12/2025" not in recorder.last["text"]


class TestEmptyStates:
    """U-03: el estado vacío no remite al mismo comando que se acaba de usar."""

    async def test_markets_empty_state_points_elsewhere(self, db, users):
        from bot.handlers.markets import mercados_handler

        update, context, recorder = make_update(user_id=1, text="/mercados")
        await mercados_handler(update, context)
        text = recorder.last["text"]
        assert "/predicciones" in text
        assert "consulta los resueltos con /mercados" not in text

    async def test_bet_without_context_explains_the_way_out(self, db, users):
        update, context, recorder = make_update(user_id=1, text="10")
        context.user_data["awaiting_bet_amount"] = True
        from bot.handlers.predictions import bet_amount_message

        await bet_amount_message(update, context)
        assert "perdió el contexto" in recorder.last["text"]


class TestUnknownOption:
    async def test_bad_option_returns_to_the_menu(self, db, users):
        update, context, recorder = make_update(user_id=1, data="bet_option:1:999")
        await bet_option_callback(update, context)
        assert "admin_panel" not in recorder.callbacks()
        assert recorder.last["text"]
