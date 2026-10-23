"""Escrow contract message bodies (build + parse).

The layouts below are the *wire contract* with ``contracts/escrow.fc``.  They
are the single place where opcodes live so the Python backend, the Mini App
and the FunC contract can be kept in sync (``tests/test_bodies.py`` guards it).

Body layouts
------------
create_market  : uint32 op | uint256 title_hash | uint32 closes_at | uint8 option_count
place_bet      : uint32 op | uint32 market_id  | uint8 option | coins stake
resolve_market : uint32 op | uint32 market_id  | uint8 winning_option
cancel_market  : uint32 op | uint32 market_id
claim_payout   : uint32 op | uint32 market_id
set_paused     : uint32 op | uint8 paused
set_fee        : uint32 op | uint16 fee_bps
change_admin   : uint32 op | MsgAddress
withdraw_fees  : uint32 op | coins amount
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from pytoniq_core import Cell, begin_cell

from bot.chain.addresses import AddressError, normalize_address, parse_address

OP_CREATE_MARKET = 0x1A0A0001
OP_PLACE_BET = 0x1A0A0002
OP_RESOLVE_MARKET = 0x1A0A0003
OP_CANCEL_MARKET = 0x1A0A0004
OP_CLAIM_PAYOUT = 0x1A0A0005
OP_SET_PAUSED = 0x1A0A0101
# --- contract -> indexer events (lifecycle only, funded by the actor's gas) --
EVENT_MARKET_CREATED = 0xE0A00001
EVENT_MARKET_RESOLVED = 0xE0A00002
EVENT_MARKET_CANCELLED = 0xE0A00003

EVENT_OPS = {EVENT_MARKET_CREATED, EVENT_MARKET_RESOLVED, EVENT_MARKET_CANCELLED}

OP_SET_FEE = 0x1A0A0102
OP_CHANGE_ADMIN = 0x1A0A0103
OP_WITHDRAW_FEES = 0x1A0A0104

KNOWN_OPS = {
    OP_CREATE_MARKET,
    OP_PLACE_BET,
    OP_RESOLVE_MARKET,
    OP_CANCEL_MARKET,
    OP_CLAIM_PAYOUT,
    OP_SET_PAUSED,
    OP_SET_FEE,
    OP_CHANGE_ADMIN,
    OP_WITHDRAW_FEES,
}

OP_NAMES = {
    EVENT_MARKET_CREATED: "event_market_created",
    EVENT_MARKET_RESOLVED: "event_market_resolved",
    EVENT_MARKET_CANCELLED: "event_market_cancelled",
    OP_CREATE_MARKET: "create_market",
    OP_PLACE_BET: "place_bet",
    OP_RESOLVE_MARKET: "resolve_market",
    OP_CANCEL_MARKET: "cancel_market",
    OP_CLAIM_PAYOUT: "claim_payout",
    OP_SET_PAUSED: "set_paused",
    OP_SET_FEE: "set_fee",
    OP_CHANGE_ADMIN: "change_admin",
    OP_WITHDRAW_FEES: "withdraw_fees",
}


class BodyError(ValueError):
    """Raised when a message body cannot be built or parsed."""


@dataclass
class MessageBody:
    op: int
    market_id: Optional[int] = None
    option: Optional[int] = None
    stake: Optional[int] = None
    winning_option: Optional[int] = None
    title_hash: Optional[str] = None
    closes_at: Optional[int] = None
    option_count: Optional[int] = None
    address: Optional[str] = None
    amount: Optional[int] = None
    flag: Optional[int] = None
    raw_op: Optional[int] = None

    @property
    def name(self) -> str:
        return OP_NAMES.get(self.op, f"0x{self.op:08x}")

    @property
    def admin_fee_bps(self) -> Optional[int]:
        """``set_fee`` payload: the contract stores the fee as ``uint16`` bps."""
        return self.amount if self.op == OP_SET_FEE else None

    @property
    def paused_flag(self) -> Optional[bool]:
        """``set_paused`` payload: ``uint8`` 1/0."""
        return None if self.flag is None else bool(self.flag)


def title_to_hash(title: str) -> str:
    """Deterministic 256-bit commitment of a market title."""
    return hashlib.sha256(" ".join(title.split()).encode("utf-8")).hexdigest()


def _b64(cell: Cell) -> str:
    return base64.b64encode(cell.to_boc()).decode("ascii")


def _b64url(cell: Cell) -> str:
    return base64.urlsafe_b64encode(cell.to_boc()).decode("ascii")


def build_create_market(title_hash: str, closes_at: int, option_count: int) -> Cell:
    if len(title_hash) != 64:
        raise BodyError("title_hash must be a 32 byte hex string")
    if option_count < 2 or option_count > 255:
        raise BodyError("option_count out of range")
    return (
        begin_cell()
        .store_uint(OP_CREATE_MARKET, 32)
        .store_uint(int(title_hash, 16), 256)
        .store_uint(int(closes_at), 32)
        .store_uint(int(option_count), 8)
        .end_cell()
    )


def build_place_bet(market_id: int, option: int, stake: int) -> Cell:
    if market_id < 0 or market_id > 0xFFFFFFFF:
        raise BodyError("market_id out of range")
    if not 0 <= option <= 255:
        raise BodyError("option out of range")
    if stake <= 0:
        raise BodyError("stake must be positive")
    return (
        begin_cell()
        .store_uint(OP_PLACE_BET, 32)
        .store_uint(int(market_id), 32)
        .store_uint(int(option), 8)
        .store_coins(int(stake))
        .end_cell()
    )


def build_resolve_market(market_id: int, winning_option: int) -> Cell:
    return (
        begin_cell()
        .store_uint(OP_RESOLVE_MARKET, 32)
        .store_uint(int(market_id), 32)
        .store_uint(int(winning_option), 8)
        .end_cell()
    )


def build_cancel_market(market_id: int) -> Cell:
    return begin_cell().store_uint(OP_CANCEL_MARKET, 32).store_uint(int(market_id), 32).end_cell()


def build_claim_payout(market_id: int) -> Cell:
    return begin_cell().store_uint(OP_CLAIM_PAYOUT, 32).store_uint(int(market_id), 32).end_cell()


def build_set_paused(paused: bool) -> Cell:
    return begin_cell().store_uint(OP_SET_PAUSED, 32).store_uint(1 if paused else 0, 8).end_cell()


def build_set_fee(fee_bps: int) -> Cell:
    if not 0 <= fee_bps <= 1000:
        raise BodyError("fee_bps must be within 0..1000 (0%..10%)")
    return begin_cell().store_uint(OP_SET_FEE, 32).store_uint(int(fee_bps), 16).end_cell()


def build_change_admin(address: str) -> Cell:
    return (
        begin_cell()
        .store_uint(OP_CHANGE_ADMIN, 32)
        .store_address(parse_address(address))
        .end_cell()
    )


def build_withdraw_fees(amount: int) -> Cell:
    if amount <= 0:
        raise BodyError("amount must be positive")
    return begin_cell().store_uint(OP_WITHDRAW_FEES, 32).store_coins(int(amount)).end_cell()


def payload_b64(cell: Cell) -> str:
    """Base64 body as expected by TON Connect ``sendTransaction.messages[].payload``."""
    return _b64(cell)


def payload_b64url(cell: Cell) -> str:
    """base64url body as expected by Tonkeeper deep links (``bin`` parameter)."""
    return _b64url(cell)


def _unwrap(cell: Cell) -> Cell:
    """Descend through empty carrier cells that only hold a single ref.

    Message bodies built by the contract (and by wallets) often live in a ref
    because the header does not leave room for them inline.
    """
    seen = 0
    while seen < 8:
        try:
            empty = cell.begin_parse().remaining_bits == 0
            refs = cell.refs or []
        except Exception:  # noqa: BLE001 - defensive
            return cell
        if empty and len(refs) == 1:
            cell = refs[0]
            seen += 1
            continue
        return cell
    return cell


def _to_cell(body: Any) -> Cell:
    if isinstance(body, Cell):
        return _unwrap(body)
    if isinstance(body, (bytes, bytearray)):
        return _unwrap(Cell.one_from_boc(bytes(body)))
    if isinstance(body, str):
        raw = body.strip()
        if not raw:
            raise BodyError("empty body")
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise BodyError("body is not valid base64") from exc
        return _unwrap(Cell.one_from_boc(data))
    raise BodyError(f"unsupported body type: {type(body)!r}")


def parse_message_body(body: Any) -> MessageBody:
    """Parse an inbound escrow message.

    Unknown or admin-only opcodes are returned with only ``op`` populated so the
    caller can decide what to do (the indexer ignores them).
    """
    cell = _to_cell(body)
    slice_ = cell.begin_parse()
    if slice_.remaining_bits < 32:
        return MessageBody(op=0)
    op = slice_.load_uint(32)
    parsed = MessageBody(op=op, raw_op=op)
    try:
        if op == OP_CREATE_MARKET:
            parsed.title_hash = f"{slice_.load_uint(256):064x}"
            parsed.closes_at = slice_.load_uint(32)
            parsed.option_count = slice_.load_uint(8)
        elif op == OP_PLACE_BET:
            parsed.market_id = slice_.load_uint(32)
            parsed.option = slice_.load_uint(8)
            parsed.stake = slice_.load_coins()
        elif op in (OP_RESOLVE_MARKET,):
            parsed.market_id = slice_.load_uint(32)
            parsed.winning_option = slice_.load_uint(8)
        elif op in (OP_CANCEL_MARKET, OP_CLAIM_PAYOUT):
            parsed.market_id = slice_.load_uint(32)
        elif op == OP_SET_PAUSED:
            parsed.flag = slice_.load_uint(8)
        elif op == OP_SET_FEE:
            parsed.amount = slice_.load_uint(16)
        elif op == OP_CHANGE_ADMIN:
            parsed.address = normalize_address(slice_.load_address().to_str())
        elif op == OP_WITHDRAW_FEES:
            parsed.amount = slice_.load_coins()
    except (AddressError, Exception) as exc:  # noqa: BLE001 - truncated body
        if isinstance(exc, BodyError):
            raise
        raise BodyError(f"malformed body for {parsed.name}: {exc}") from exc
    return parsed


def prepare_transaction_payload(
    *, kind: str, market_id: Optional[int] = None, option: Optional[int] = None,
    stake: Optional[int] = None, winning_option: Optional[int] = None,
    title_hash: Optional[str] = None, closes_at: Optional[int] = None,
    option_count: Optional[int] = None,
) -> str:
    """Build the base64 payload for a Mini App ``sendTransaction`` call."""
    if kind == "create_market":
        if title_hash is None or closes_at is None or option_count is None:
            raise BodyError("create_market requires title_hash, closes_at and option_count")
        return payload_b64(build_create_market(title_hash, closes_at, option_count))
    if kind == "place_bet":
        if market_id is None or option is None or stake is None:
            raise BodyError("place_bet requires market_id, option and stake")
        return payload_b64(build_place_bet(market_id, option, stake))
    if kind == "resolve_market":
        if market_id is None or winning_option is None:
            raise BodyError("resolve_market requires market_id and winning_option")
        return payload_b64(build_resolve_market(market_id, winning_option))
    if kind == "cancel_market":
        if market_id is None:
            raise BodyError("cancel_market requires market_id")
        return payload_b64(build_cancel_market(market_id))
    if kind == "claim_payout":
        if market_id is None:
            raise BodyError("claim_payout requires market_id")
        return payload_b64(build_claim_payout(market_id))
    raise BodyError(f"unknown payload kind {kind!r}")


# ---------------------------------------------------------------------------
# Lifecycle events emitted by the escrow contract to the indexer address
# ---------------------------------------------------------------------------
@dataclass
class EscrowEvent:
    op: int
    market_id: int
    creator: Optional[str] = None
    title_hash: Optional[str] = None
    closes_at: Optional[int] = None
    option_count: Optional[int] = None
    fee_bps: Optional[int] = None
    winning_option: Optional[int] = None
    total_pool: Optional[int] = None
    winning_pool: Optional[int] = None

    @property
    def name(self) -> str:
        return OP_NAMES.get(self.op, f"0x{self.op:08x}")


def parse_event(body: Any) -> Optional[EscrowEvent]:
    """Parse a contract event. Returns ``None`` for non-event bodies."""
    try:
        cell = _to_cell(body)
        s = cell.begin_parse()
        if s.remaining_bits < 64:
            return None
        op = s.load_uint(32)
        if op not in EVENT_OPS:
            return None
        event = EscrowEvent(op=op, market_id=s.load_uint(32))
        if op == EVENT_MARKET_CREATED:
            event.creator = normalize_address(s.load_address().to_str())
            event.title_hash = f"{s.load_uint(256):064x}"
            event.closes_at = s.load_uint(32)
            event.option_count = s.load_uint(8)
            if s.remaining_bits >= 16:
                event.fee_bps = s.load_uint(16)
        elif op == EVENT_MARKET_RESOLVED:
            event.winning_option = s.load_uint(8)
            event.total_pool = s.load_coins()
            event.winning_pool = s.load_coins()
        return event
    except Exception:  # noqa: BLE001 - unknown/foreign message bodies
        return None
