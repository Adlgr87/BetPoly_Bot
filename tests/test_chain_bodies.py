"""Market bodies, addresses and event parsing (wire protocol with escrow.fc)."""
from __future__ import annotations

import base64

import pytest
from pytoniq_core import Cell, begin_cell

from bot.chain import bodies
from bot.chain.addresses import (
    AddressError,
    is_valid_address,
    normalize_address,
    parse_address,
    same_address,
    to_friendly,
)

VALID = "0:" + "ab" * 32


class TestAddresses:
    def test_normalizes_raw_and_friendly_forms(self):
        friendly = to_friendly(VALID)
        assert friendly.startswith("EQ")
        assert normalize_address(friendly) == VALID
        assert normalize_address(VALID.upper().replace(":", ":")) == VALID

    def test_invalid_inputs(self):
        for value in ("", "not-an-address", "0:xyz", "EQ", None):
            assert not is_valid_address(value)
        with pytest.raises(AddressError):
            parse_address("nope")

    def test_same_address_across_encodings(self):
        assert same_address(VALID, to_friendly(VALID, bounceable=False))
        assert not same_address(VALID, "0:" + "cd" * 32)


class TestBodies:
    def test_create_market_roundtrip(self):
        title_hash = bodies.title_to_hash("  ¿Ganará   México?  ")
        cell = bodies.build_create_market(title_hash, 1_900_000_000, 3)
        parsed = bodies.parse_message_body(cell)
        assert parsed.op == bodies.OP_CREATE_MARKET
        assert parsed.title_hash == title_hash
        assert parsed.closes_at == 1_900_000_000
        assert parsed.option_count == 3

    def test_place_bet_roundtrip(self):
        cell = bodies.build_place_bet(42, 2, 1_500_000_000)
        parsed = bodies.parse_message_body(cell)
        assert (parsed.market_id, parsed.option, parsed.stake) == (42, 2, 1_500_000_000)

    def test_resolve_cancel_claim_roundtrip(self):
        resolve = bodies.parse_message_body(bodies.build_resolve_market(7, 1))
        assert (resolve.market_id, resolve.winning_option) == (7, 1)
        assert bodies.parse_message_body(bodies.build_cancel_market(9)).market_id == 9
        assert bodies.parse_message_body(bodies.build_claim_payout(11)).market_id == 11

    def test_payload_is_base64_boc(self):
        cell = bodies.build_place_bet(1, 0, 1_000_000_000)
        payload = bodies.payload_b64(cell)
        raw = base64.b64decode(payload)
        assert raw[:4] == bytes.fromhex("b5ee9c72")
        parsed = bodies.parse_message_body(base64.b64encode(raw).decode())
        assert parsed.market_id == 1

    def test_b64url_payload_roundtrip(self):
        cell = bodies.build_claim_payout(5)
        url = bodies.payload_b64url(cell)
        assert "+" not in url and "/" not in url
        assert bodies.parse_message_body(url).market_id == 5

    def test_validation_errors(self):
        with pytest.raises(bodies.BodyError):
            bodies.build_create_market("short", 1, 2)
        with pytest.raises(bodies.BodyError):
            bodies.build_place_bet(1, 1, 0)
        with pytest.raises(bodies.BodyError):
            bodies.build_set_fee(5000)

    def test_unknown_op_is_reported(self):
        cell = begin_cell().store_uint(0xDEADBEEF, 32).end_cell()
        parsed = bodies.parse_message_body(cell)
        assert parsed.op == 0xDEADBEEF
        assert parsed.name == "0xdeadbeef"

    def test_title_hash_is_normalised(self):
        assert bodies.title_to_hash("Hola   mundo") == bodies.title_to_hash(" Hola mundo ")


def _created_event(market_id: int, creator: str, title_hash: str, closes_at: int, count: int) -> Cell:
    return (
        begin_cell()
        .store_uint(bodies.EVENT_MARKET_CREATED, 32)
        .store_uint(market_id, 32)
        .store_address(parse_address(creator))
        .store_uint(int(title_hash, 16), 256)
        .store_uint(closes_at, 32)
        .store_uint(count, 8)
        .store_uint(200, 16)
        .end_cell()
    )


class TestEvents:
    def test_parse_market_created(self):
        title_hash = "ab" * 32
        event = bodies.parse_event(_created_event(3, VALID, title_hash, 1_800_000_000, 4))
        assert event is not None
        assert event.op == bodies.EVENT_MARKET_CREATED
        assert event.market_id == 3
        assert event.creator == VALID
        assert event.title_hash == title_hash
        assert event.closes_at == 1_800_000_000
        assert event.option_count == 4
        assert event.fee_bps == 200

    def test_parse_market_resolved(self):
        cell = (
            begin_cell()
            .store_uint(bodies.EVENT_MARKET_RESOLVED, 32)
            .store_uint(9, 32)
            .store_uint(2, 8)
            .store_coins(6_000_000_000)
            .store_coins(4_000_000_000)
            .end_cell()
        )
        event = bodies.parse_event(cell)
        assert event.market_id == 9
        assert event.winning_option == 2
        assert event.total_pool == 6_000_000_000
        assert event.winning_pool == 4_000_000_000

    def test_event_nested_in_ref_is_unwrapped(self):
        """The contract cannot fit the event inline, so it travels in a ref."""
        inner = _created_event(1, VALID, "cd" * 32, 1_800_000_000, 2)
        carrier = begin_cell().store_ref(inner).end_cell()
        assert carrier.begin_parse().remaining_bits == 0
        event = bodies.parse_event(carrier)
        assert event is not None and event.market_id == 1

    def test_non_events_return_none(self):
        assert bodies.parse_event(begin_cell().store_uint(0x12345678, 32).store_uint(1, 32).end_cell()) is None
        assert bodies.parse_event(begin_cell().store_uint(1, 8).end_cell()) is None
