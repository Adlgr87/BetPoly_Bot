"""Teclados: datos de callback y paginación (D-06).

`bot/keyboards.py` es donde vive la navegación del bot, y hasta la Fase 7 era el
módulo con más condicionales y menos pruebas. Estos tests fijan lo que de verdad
importa: qué `callback_data` sale en cada botón y que la paginación no ofrezca
«atrás» en la primera página ni «adelante» en la última.
"""
from __future__ import annotations

from bot import keyboards


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


class TestMainMenu:
    def test_admin_row_only_for_admins(self):
        assert "admin_panel" in callbacks(keyboards.main_menu_keyboard(is_admin=True))
        assert "admin_panel" not in callbacks(keyboards.main_menu_keyboard())

    def test_wallet_row_follows_the_mode_and_shows_the_link_state(self):
        """El modo híbrido (por defecto en los tests) sí muestra la fila."""
        linked = keyboards.main_menu_keyboard(wallet={"address": "0:" + "ab" * 32})
        plain = keyboards.main_menu_keyboard()
        assert callbacks(linked) == callbacks(plain)
        assert "👛 Wallet vinculada" in labels(linked)
        assert "👛 Vincular wallet" in labels(plain)

    def test_every_row_is_a_real_button_list(self):
        for row in keyboards.main_menu_keyboard(is_admin=True).inline_keyboard:
            assert row and all(button.text for button in row)


class TestBackToMenu:
    def test_admin_keeps_the_way_back_to_the_panel(self):
        markup = keyboards.back_to_menu_keyboard(is_admin=True)
        assert {"main_menu", "admin_panel"} <= set(callbacks(markup))

    def test_a_regular_user_only_goes_home(self):
        assert "admin_panel" not in callbacks(keyboards.back_to_menu_keyboard())
        assert "main_menu" in callbacks(keyboards.back_to_menu_keyboard())


class TestPagination:
    PREDICTIONS = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    def test_first_page_never_offers_back(self):
        markup = keyboards.predictions_list_keyboard(0, 3, self.PREDICTIONS)
        data = callbacks(markup)
        assert "bet_select:1" in data
        assert not any(item == "list_predictions:-1" for item in data)
        assert "list_predictions:1" in data
        assert "1/3" in labels(markup)

    def test_middle_page_offers_both_directions(self):
        data = callbacks(keyboards.predictions_list_keyboard(1, 3, self.PREDICTIONS))
        assert "list_predictions:0" in data and "list_predictions:2" in data

    def test_last_page_never_offers_forward(self):
        data = callbacks(keyboards.predictions_list_keyboard(2, 3, self.PREDICTIONS))
        assert "list_predictions:1" in data
        assert "list_predictions:3" not in data

    def test_a_single_page_has_no_navigation(self):
        data = callbacks(keyboards.predictions_list_keyboard(0, 1, self.PREDICTIONS))
        assert not any(item.startswith("list_predictions:") for item in data)
        assert data[-1] == "main_menu"

    def test_markets_pagination(self):
        markets = [{"market_id": 5, "title": "M"}]
        first = callbacks(keyboards.markets_list_keyboard(0, 2, markets))
        last = callbacks(keyboards.markets_list_keyboard(1, 2, markets))
        assert "market_view:5" in first
        assert "list_markets:1" in first and "list_markets:0" in last
        assert "list_markets:-1" not in first and "list_markets:2" not in last

    def test_admin_chain_pagination(self):
        markets = [{"market_id": 9, "title": "On-chain"}]
        first = callbacks(keyboards.admin_chain_markets_keyboard(0, 2, markets))
        last = callbacks(keyboards.admin_chain_markets_keyboard(1, 2, markets))
        assert "admin_chain_market:9" in first
        assert "admin_chain:1" in first and "admin_chain:0" in last
        assert "admin_chain:-1" not in first and "admin_chain:2" not in last
        assert last[-1] == "admin_panel"


class TestActions:
    def test_bet_confirmation_carries_the_three_numbers(self):
        data = callbacks(keyboards.confirm_bet_keyboard(4, 2, 250))
        assert "bet_confirm:4:2:250" in data

    def test_options_map_to_their_prediction(self):
        data = callbacks(
            keyboards.options_keyboard(7, [{"id": 1, "label": "Sí"}, {"id": 2, "label": "No"}])
        )
        assert data[:2] == ["bet_option:7:1", "bet_option:7:2"]

    def test_resolution_confirmation_is_explicit(self):
        data = callbacks(keyboards.confirm_resolve_keyboard(3, 1))
        assert "admin_confirm:3:1" in data
        assert "admin_resolve:3" in data, "cancelar vuelve a elegir la opción"

    def test_long_labels_are_truncated(self):
        markup = keyboards.options_keyboard(1, [{"id": 1, "label": "x" * 120}])
        assert len(labels(markup)[0]) <= 40
