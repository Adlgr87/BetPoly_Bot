"""Guards that keep the contract artifact and the Python wire format in sync.

`contracts/escrow.fc` is the source of truth for opcodes, events and exit
codes.  `bot/chain/bodies.py` re-implements the same numbers in Python, so a
rename or renumbering in one place silently breaks the other: these tests fail
fast when that happens.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pytoniq_core import Cell

from bot.chain import bodies

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "escrow.fc"
ARTIFACT = ROOT / "contracts" / "build" / "escrow.code.boc"

#: Hash of the compiled code cell.  Update it together with the artifact when
#: the contract changes on purpose (and redeploy).
CODE_HASH = "a29c5c51fe93597967c6d37c976a27e989d13662117165dec1d899fe3292db9b"


def _contract_constants() -> dict[str, int]:
    text = CONTRACT.read_text()
    return {
        name: int(value, 16)
        for name, value in re.findall(r"const int (\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", text)
    }


def _exit_codes() -> dict[str, int]:
    text = CONTRACT.read_text()
    return {
        name: int(value)
        for name, value in re.findall(r"const int (err_\w+)\s*=\s*(\d+)\s*;", text)
    }


@pytest.mark.skipif(not CONTRACT.exists(), reason="contrato no incluido")
class TestOpcodes:
    def test_python_opcodes_match_contract(self):
        constants = _contract_constants()
        pairs = [
            ("op_create_market", bodies.OP_CREATE_MARKET),
            ("op_place_bet", bodies.OP_PLACE_BET),
            ("op_resolve_market", bodies.OP_RESOLVE_MARKET),
            ("op_cancel_market", bodies.OP_CANCEL_MARKET),
            ("op_claim_payout", bodies.OP_CLAIM_PAYOUT),
            ("op_set_paused", bodies.OP_SET_PAUSED),
            ("op_set_fee", bodies.OP_SET_FEE),
            ("op_change_admin", bodies.OP_CHANGE_ADMIN),
            ("op_withdraw_fees", bodies.OP_WITHDRAW_FEES),
        ]
        for name, python_value in pairs:
            assert constants[name] == python_value, name

    def test_event_opcodes_match_contract(self):
        constants = _contract_constants()
        assert constants["ev_market_created"] == bodies.EVENT_MARKET_CREATED
        assert constants["ev_market_resolved"] == bodies.EVENT_MARKET_RESOLVED
        assert constants["ev_market_cancelled"] == bodies.EVENT_MARKET_CANCELLED

    def test_opcodes_are_unique(self):
        values = [
            bodies.OP_CREATE_MARKET, bodies.OP_PLACE_BET, bodies.OP_RESOLVE_MARKET,
            bodies.OP_CANCEL_MARKET, bodies.OP_CLAIM_PAYOUT, bodies.OP_SET_PAUSED,
            bodies.OP_SET_FEE, bodies.OP_CHANGE_ADMIN, bodies.OP_WITHDRAW_FEES,
        ]
        assert len(set(values)) == len(values)

    def test_exit_codes_are_contiguous_and_unique(self):
        codes = sorted(_exit_codes().values())
        assert len(set(codes)) == len(codes)
        assert codes == list(range(codes[0], codes[0] + len(codes)))
        assert codes[0] == 401


@pytest.mark.skipif(not ARTIFACT.exists(), reason="ejecuta `npm run build` en contracts/")
class TestArtifact:
    def test_code_hash_is_pinned(self):
        cell = Cell.from_boc(ARTIFACT.read_bytes())[0]
        assert cell.hash.hex() == CODE_HASH

    def test_code_cell_is_small(self):
        cell = Cell.from_boc(ARTIFACT.read_bytes())[0]
        # Sanity check: the contract fits the 64 KiB cell budget with room to spare.
        assert len(cell.bits.tobytes()) <= 64


class TestLimits:
    def test_fee_cap(self):
        text = CONTRACT.read_text()
        assert re.search(r"const int max_fee_bps\s*=\s*1000;", text)

    def test_max_options_respected_by_config(self):
        text = CONTRACT.read_text()
        match = re.search(r"const int max_options\s*=\s*(\d+);", text)
        assert match, "el contrato debe declarar max_options"
        contract_max = int(match.group(1))

        import config

        assert config.get_settings().max_options <= contract_max

    def test_min_stake_is_at_least_one_cent(self):
        text = CONTRACT.read_text()
        assert re.search(r"const int min_bet_value\s*=\s*10000000;", text)
