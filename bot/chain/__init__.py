"""Blockchain adapters (TON).

Everything in this package is **read-only** with respect to user funds: the
bot builds unsigned message bodies that the user's own wallet signs, and reads
the escrow contract from a public API.  No private key ever enters the
process.
"""

from bot.chain.addresses import (  # noqa: F401
    AddressError,
    is_valid_address,
    normalize_address,
    parse_address,
    same_address,
    to_friendly,
)
from bot.chain.bodies import (  # noqa: F401
    EVENT_MARKET_CANCELLED,
    EVENT_MARKET_CREATED,
    EVENT_MARKET_RESOLVED,
    EVENT_OPS,
    OP_CANCEL_MARKET,
    OP_CLAIM_PAYOUT,
    OP_CREATE_MARKET,
    OP_PLACE_BET,
    OP_RESOLVE_MARKET,
    EscrowEvent,
    MessageBody,
    build_cancel_market,
    build_claim_payout,
    build_create_market,
    build_place_bet,
    build_resolve_market,
    parse_event,
    parse_message_body,
    title_to_hash,
)
