"""Fase 4: una sola economía pari-mutuel (F-03, H-*).

* La fórmula vive en `bot/economics.py` y se prueba aquí.
* `contracts/vectors/economics.json` fija los mismos vectores que consume el
  test en JavaScript: si una de las dos implementaciones se desvía, CI falla.
* Un guardarraíl impide que vuelvan los literales de dinero por el árbol.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bot import economics

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "contracts" / "vectors" / "economics.json"


def _vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


class TestDistributable:
    def test_keeps_the_pool_when_there_is_no_fee(self):
        assert economics.distributable(10_000, 0) == 10_000

    def test_subtracts_the_fee_rounding_down(self):
        assert economics.distributable(999, 1_000) == 900
        assert economics.fee_of(999, 1_000) == 99

    def test_clamps_a_fee_above_the_denominator(self):
        assert economics.distributable(1_000, 50_000) == 0

    def test_empty_or_negative_pool(self):
        assert economics.distributable(0, 200) == 0
        assert economics.distributable(-5, 200) == 0
        assert economics.fee_of(0, 200) == 0


class TestPayout:
    def test_shares_the_pool_proportionally(self):
        assert economics.payout(500, 1_000, 1_000) == 500

    def test_rounds_down_and_never_returns_negatives(self):
        assert economics.payout(3, 7, 7) == 3
        assert economics.payout(0, 10, 10) == 0
        assert economics.payout(10, 0, 10) == 0
        assert economics.payout(10, 10, 0) == 0
        assert economics.payout(-10, 10, 10) == 0

    def test_multiplier_is_net_of_the_fee(self):
        # 4 TON de pozo, 2 % de comisión → 3.92 por TON apostado, no 4.
        assert economics.multiplier(4_000_000_000, 1_000_000_000, 200) == 3.92
        assert economics.multiplier(4_000_000_000, 1_000_000_000, 0) == 4.0

    def test_multiplier_needs_a_pool(self):
        assert economics.multiplier(0, 0, 200) is None
        assert economics.multiplier(1_000, 0, 200) is None

    def test_preview_includes_the_new_stake(self):
        assert economics.preview_payout(1_000_000_000, 0, 0, 200) == 980_000_000
        assert economics.preview_payout(0, 0, 0, 200) == 0


class TestSharedVectors:
    """Los mismos números que valida el sandbox del contrato (JS)."""

    @pytest.mark.parametrize("case", _vectors()["payouts"])
    def test_payout_matches_the_contract(self, case):
        pool = economics.distributable(case["total"], case["fee_bps"])
        assert pool == case["distributable"]
        assert economics.payout(case["stake"], pool, case["winning_pool"]) == case["payout"]

    @pytest.mark.parametrize("case", _vectors()["previews"])
    def test_preview_matches_the_contract(self, case):
        assert economics.preview_payout(
            case["stake"], case["option_pool"], case["total"], case["fee_bps"]
        ) == case["payout"]


class TestNoMoneyLiterals:
    """H-*: los importes configuran el sistema, no se copian por el código.

    El barrido ignora `config.py` (donde viven los valores por defecto), los
    tests y el contrato (que tiene su propia unidad).
    """

    FORBIDDEN = (r"\b30_000_000\b", r"\b50_000_000\b", r"\b20_000_000\b",
                 r"5_000\s*\*\s*NANO", r"\b0\.05\b")
    ROOTS = ("bot", "web", "scripts", "main.py")

    def _files(self):
        for name in self.ROOTS:
            path = ROOT / name
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.suffix in (".py", ".js", ".html") and "node_modules" not in child.parts:
                        yield child
            elif path.is_file():
                yield path

    def test_no_hardcoded_money_amounts(self):
        offenders = []
        for path in self._files():
            text = path.read_text(encoding="utf-8")
            for pattern in self.FORBIDDEN:
                for match in re.findall(pattern, text):
                    offenders.append(f"{path.relative_to(ROOT)}: {match}")
        assert not offenders, (
            "importes de dinero incrustados fuera de config.py: "
            f"{offenders}. Añádelos a Settings y léelos desde ahí."
        )
