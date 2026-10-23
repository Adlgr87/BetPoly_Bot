"""S-06: el importe que se confirma es el importe que se apuesta.

El handler antiguo hacía ``text.replace(".", "").replace(",", "")``, así que
``1.5`` se convertía en ``15``: diez veces lo escrito. Estas tablas fijan el
comportamiento nuevo.
"""
from __future__ import annotations

import pytest

from bot import parsing
from bot.markets import MarketError


def _code(call) -> str:
    with pytest.raises(MarketError) as exc:
        call()
    return exc.value.code


class TestParsePoints:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("250", 250),
            (" 250 ", 250),
            ("1.000", 1000),
            ("1,000", 1000),
            ("1 000", 1000),
            ("1 234 567", 1_234_567),
            ("1.234.567", 1_234_567),
            ("10\u00a0000", 10_000),
            (42, 42),
        ],
    )
    def test_accepts_integers_and_thousands(self, raw, expected):
        assert parsing.parse_points(raw) == expected

    @pytest.mark.parametrize("raw,suggested", [("10.5", 10), ("1,5", 1), ("250.90", 250)])
    def test_decimals_are_explicit_errors_not_truncation(self, raw, suggested):
        with pytest.raises(MarketError) as exc:
            parsing.parse_points(raw)
        assert exc.value.code == "bad_points_decimal"
        assert f"si querías {suggested}, escribe {suggested}" in str(exc.value)

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "-5", "1.23.4", "10 puntos", "1e3"])
    def test_rejects_junk(self, raw):
        assert _code(lambda: parsing.parse_points(raw)) == "bad_amount"

    @pytest.mark.parametrize("raw", ["0", 0])
    def test_rejects_zero(self, raw):
        assert _code(lambda: parsing.parse_points(raw)) == "bad_amount"

    def test_rejects_none_and_bool(self):
        assert _code(lambda: parsing.parse_points(None)) == "bad_amount"
        assert _code(lambda: parsing.parse_points(True)) == "bad_amount"

    def test_respects_the_maximum(self):
        assert parsing.parse_points("1000", maximum=1000) == 1000
        assert _code(lambda: parsing.parse_points("1001", maximum=1000)) == "stake_too_high"
        with pytest.raises(MarketError) as exc:
            parsing.parse_points("5000", maximum=1000)
        assert "1000 puntos" in str(exc.value)


class TestParseNanoAndFee:
    def test_nano_accepts_ton_and_nanoton(self):
        assert parsing.parse_nano("1.5") == 1_500_000_000
        assert parsing.parse_nano("1,5") == 1_500_000_000
        assert parsing.parse_nano(1_500_000_000) == 1_500_000_000

    def test_nano_rejects_text(self):
        assert _code(lambda: parsing.parse_nano("dos ton")) == "bad_amount"

    def test_fee_percent_to_bps(self):
        assert parsing.parse_fee_bps("2") == 200
        assert parsing.parse_fee_bps("2,5 %") == 250

    def test_fee_caps_at_ten_percent(self):
        assert _code(lambda: parsing.parse_fee_bps("10.01")) == "bad_fee"
        assert _code(lambda: parsing.parse_fee_bps("mucho")) == "bad_fee"
