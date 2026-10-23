"""Parsing of money and user input.

One module for every "turn this text into a number" rule, so the bot, the API
and the tests cannot disagree (P6 del plan). All money is returned as integers:

* TON amounts -> nanoTON (1 TON = 1e9 nanoTON)
* percentages -> basis points (1 % = 100 bps)
* points -> integer points, accepting thousands separators

Errors are always :class:`MarketError` with a user-facing message, because every
caller shows that text directly.
"""
from __future__ import annotations

import re
from typing import Any

from bot.markets import MAX_FEE_BPS, MarketError

#: ``1``, ``1.5``, ``1,5``, ``0.000000001`` — up to 9 decimals, no signs, no exponents.
NANO_RE = re.compile(r"^\d+(?:[.,]\d{1,9})?$")
#: Percentage with up to two decimals: ``2``, ``2.5``, ``2,75``.
PERCENT_RE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")

NANO_PER_TON = 1_000_000_000

#: ``1000`` — plain integer.
POINTS_RE = re.compile(r"^\d+$")
#: ``1.000`` / ``1,000`` / ``1 000`` / ``1 234 567`` — thousands groups of three.
THOUSANDS_RE = re.compile(r"^\d{1,3}(?:([ .,])\d{3})(?:\1\d{3})*$")
#: ``10.5`` / ``1,5`` — a decimal the user probably meant as an integer.
DECIMAL_RE = re.compile(r"^\d+[.,]\d+$")


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


def parse_points(value: Any, *, maximum: int | None = None) -> int:
    """Parse a points stake, refusing silent losses (S-06).

    * ``250`` -> 250
    * ``1.000`` / ``1,000`` / ``1 000`` -> 1000 (thousands, not decimals)
    * ``10.5`` / ``1,5`` -> explicit error: points are integers
    * ``0``, negatives or more than ``maximum`` -> error naming the limit

    The old handler stripped every dot and comma, so ``1.5`` silently became
    ``15`` — ten times the stake the user wrote. Here an ambiguous number is
    rejected instead of guessed.
    """
    if isinstance(value, bool) or value is None:
        raise MarketError("bad_amount", "Escribe cuántos puntos quieres apostar.")
    if isinstance(value, int):
        amount = value
    else:
        raw = " ".join(str(value).replace("\u00a0", " ").split())
        if not raw:
            raise MarketError("bad_amount", "Escribe cuántos puntos quieres apostar.")
        if POINTS_RE.match(raw):
            amount = int(raw)
        else:
            match = THOUSANDS_RE.match(raw)
            if match:
                amount = int(raw.replace(match.group(1), ""))
            elif DECIMAL_RE.match(raw):
                suggested = raw.replace(",", ".")
                integer = int(float(suggested))
                raise MarketError(
                    "bad_points_decimal",
                    f"Los puntos son enteros: si querías {integer}, escribe {integer}.",
                )
            else:
                raise MarketError(
                    "bad_amount", "Escribe un número entero de puntos, por ejemplo 250."
                )
    if amount <= 0:
        raise MarketError("bad_amount", "La apuesta debe ser mayor que cero.")
    if maximum is not None and amount > int(maximum):
        raise MarketError(
            "stake_too_high",
            f"El máximo es {int(maximum)} puntos: no puedes apostar más que eso.",
        )
    return amount


def parse_fee_bps(value: Any) -> int:
    """Parse ``"2"`` / ``"2.5"`` (percent) into basis points (200 / 250)."""
    raw = str(value if value is not None else "").strip().replace(",", ".").rstrip(" %")
    if not PERCENT_RE.match(raw):
        raise MarketError(
            "bad_fee", "Escribe el porcentaje con hasta dos decimales, por ejemplo 2.5"
        )
    percent = float(raw)
    bps = int(round(percent * 100))
    if bps > MAX_FEE_BPS:
        raise MarketError("bad_fee", "La comisión máxima es del 10 %.")
    return bps
