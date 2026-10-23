"""Parsing of money and user input.

One module for every "turn this text into a number" rule, so the bot, the API
and the tests cannot disagree (P6 del plan). All money is returned as integers:

* TON amounts -> nanoTON (1 TON = 1e9 nanoTON)
* percentages -> basis points (1 % = 100 bps)

Errors are always :class:`MarketError` with a user-facing message, because every
caller shows that text directly.
"""
from __future__ import annotations

import re
from typing import Any

from bot.markets import MarketError

#: ``1``, ``1.5``, ``1,5``, ``0.000000001`` — up to 9 decimals, no signs, no exponents.
NANO_RE = re.compile(r"^\d+(?:[.,]\d{1,9})?$")
#: Percentage with up to two decimals: ``2``, ``2.5``, ``2,75``.
PERCENT_RE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")

NANO_PER_TON = 1_000_000_000


def parse_nano(value: Any, *, field: str = "importe") -> int:
    """Parse ``"1.5"`` (TON) or ``1500000000`` (nanoTON) into nanoTON."""
    if isinstance(value, bool) or value is None:
        raise MarketError("bad_amount", f"El {field} no es válido.")
    if isinstance(value, int):
        return value
    raw = str(value).strip().replace(",", ".")
    if not NANO_RE.match(raw):
        raise MarketError(
            "bad_amount", f"Escribe un {field} válido en TON, por ejemplo 1.5"
        )
    whole, _, frac = raw.partition(".")
    return int(whole) * NANO_PER_TON + int(frac.ljust(9, "0") or 0)


def parse_fee_bps(value: Any) -> int:
    """Parse ``"2"`` / ``"2.5"`` (percent) into basis points (200 / 250)."""
    raw = str(value if value is not None else "").strip().replace(",", ".").rstrip(" %")
    if not PERCENT_RE.match(raw):
        raise MarketError(
            "bad_fee", "Escribe el porcentaje con hasta dos decimales, por ejemplo 2.5"
        )
    percent = float(raw)
    bps = int(round(percent * 100))
    if bps > 1000:
        raise MarketError("bad_fee", "La comisión máxima es del 10 %.")
    return bps
