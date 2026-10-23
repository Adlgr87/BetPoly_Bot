"""Market (escrow) service layer.

This module turns a user intent — *"quiero apostar 2 TON a la opción 1"* — into
an **unsigned** transaction payload that the user's own wallet signs.  The
backend validates every rule it can validate off-chain (mirror state, limits,
deadlines) but never moves funds itself.

Money always travels:

    user wallet  ──signed tx──▶  escrow contract  ──claim──▶  user wallet

The bot only *reads* the result.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import config
from bot import database
from bot.chain.addresses import AddressError, normalize_address, to_friendly
from bot.chain.bodies import (
    build_cancel_market,
    build_claim_payout,
    build_create_market,
    build_place_bet,
    build_resolve_market,
    payload_b64,
    payload_b64url,
    title_to_hash,
)
from bot.formatting import ton

logger = logging.getLogger(__name__)

NANO = 1_000_000_000

#: Gas the contract needs on top of the stake (forward fees + storage).
BET_GAS_NANO = 30_000_000          # 0.03 TON
CREATE_GAS_NANO = 50_000_000       # 0.05 TON
ADMIN_GAS_NANO = 50_000_000        # 0.05 TON (funds the lifecycle event)
CLAIM_GAS_NANO = 20_000_000        # 0.02 TON

MIN_MARKET_DURATION = 15 * 60      # 15 minutes
MAX_MARKET_DURATION = 365 * 24 * 3600
TX_TTL = 300                       # 5 minutes to sign


class MarketError(ValueError):
    """Business rule violation, safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PreparedTx:
    """Everything the wallet needs to sign, plus UI hints."""

    to: str
    amount: int                 # nanoTON attached value
    payload: str                # base64 BoC body
    kind: str
    valid_until: int
    summary: str
    deeplink: str
    details: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "to": self.to,
            "amount": str(self.amount),
            "amount_ton": ton(self.amount),
            "payload": self.payload,
            "kind": self.kind,
            "valid_until": self.valid_until,
            "summary": self.summary,
            "deeplink": self.deeplink,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def escrow_address() -> str:
    settings = config.get_settings()
    if not settings.escrow_enabled or not settings.escrow_address:
        raise MarketError(
            "escrow_disabled",
            "Los mercados on-chain no están habilitados en esta instancia del bot.",
        )
    try:
        return normalize_address(settings.escrow_address)
    except AddressError as exc:  # pragma: no cover - config validated at boot
        raise MarketError("bad_escrow", "ESCROW_ADDRESS no es una dirección válida.") from exc


def friendly_escrow() -> str:
    return to_friendly(escrow_address())


def tonkeeper_link(address: str, amount: int, body_b64url: str, text: Optional[str] = None) -> str:
    """Deep link that opens a wallet with a pre-filled transaction."""
    params = {"amount": str(max(0, int(amount)))}
    if text:
        params["text"] = text[:120]
    params["bin"] = body_b64url
    return f"https://app.tonkeeper.com/transfer/{to_friendly(address)}?{urlencode(params)}"


def _prepared(
    body_cell, *, amount: int, kind: str, summary: str, details: Dict[str, Any]
) -> PreparedTx:
    escrow = escrow_address()
    valid_until = int(time.time()) + TX_TTL
    return PreparedTx(
        to=to_friendly(escrow),
        amount=int(amount),
        payload=payload_b64(body_cell),
        kind=kind,
        valid_until=valid_until,
        summary=summary,
        deeplink=tonkeeper_link(escrow, amount, payload_b64url(body_cell), text=summary),
        details=details,
    )


async def require_linked_wallet(user_id: int) -> Dict[str, Any]:
    wallet = await database.get_wallet(user_id)
    if not wallet:
        raise MarketError(
            "wallet_required",
            "Vincula tu wallet TON antes de operar con mercados on-chain.",
        )
    return wallet


def _assert_open(market: Dict[str, Any], now: Optional[int] = None) -> None:
    now = int(now if now is not None else time.time())
    if market["status"] != "open":
        raise MarketError("market_closed", "Este mercado ya no acepta apuestas.")
    closes_at = int(market.get("closes_at") or 0)
    if closes_at and closes_at <= now:
        raise MarketError("market_expired", "El mercado ya cerró.")


# ---------------------------------------------------------------------------
# create market
# ---------------------------------------------------------------------------

async def prepare_create_market(
    user_id: int, title: str, options: List[str], closes_at: int
) -> PreparedTx:
    settings = config.get_settings()
    wallet = await require_linked_wallet(user_id)
    title = " ".join((title or "").split())
    if len(title) < 6:
        raise MarketError("bad_title", "El título debe tener al menos 6 caracteres.")
    if len(title) > 140:
        raise MarketError("bad_title", "El título no puede superar 140 caracteres.")
    cleaned = [" ".join(o.split()) for o in options if o and o.strip()]
    if not 2 <= len(cleaned) <= settings.max_options:
        raise MarketError(
            "bad_options", f"Necesitas entre 2 y {settings.max_options} opciones."
        )
    if len({o.lower() for o in cleaned}) != len(cleaned):
        raise MarketError("bad_options", "Las opciones deben ser distintas.")
    if any(len(o) > 60 for o in cleaned):
        raise MarketError("bad_options", "Cada opción puede tener hasta 60 caracteres.")

    now = int(time.time())
    if closes_at < now + MIN_MARKET_DURATION:
        raise MarketError(
            "bad_close_date",
            f"El cierre debe ser al menos {MIN_MARKET_DURATION // 60} minutos en el futuro.",
        )
    if closes_at > now + MAX_MARKET_DURATION:
        raise MarketError("bad_close_date", "El cierre no puede estar a más de un año.")

    title_hash = title_to_hash(title)
    tx = _prepared(
        build_create_market(title_hash, closes_at, len(cleaned)),
        amount=CREATE_GAS_NANO,
        kind="create_market",
        summary=f"Crear mercado: {title[:60]}",
        details={
            "market_id": None,
            "title": title,
            "title_hash": title_hash,
            "options": cleaned,
            "closes_at": closes_at,
            "creator": wallet["address"],
        },
    )
    await database.create_pending_action(
        user_id=user_id,
        kind="create_market",
        match_a=normalize_address(wallet["address"]),
        match_b=title_hash,
        payload={
            "title": title,
            "options": cleaned,
            "closes_at": closes_at,
            "title_hash": title_hash,
        },
    )
    return tx


# ---------------------------------------------------------------------------
# place bet
# ---------------------------------------------------------------------------

async def prepare_bet(user_id: int, market_id: int, option: int, stake: int) -> PreparedTx:
    settings = config.get_settings()
    wallet = await require_linked_wallet(user_id)
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe todavía en la cadena.")
    _assert_open(market)
    if not 0 <= int(option) < int(market["option_count"]):
        raise MarketError("bad_option", "Opción fuera de rango.")
    if int(stake) < settings.min_stake_nano:
        raise MarketError(
            "stake_too_low",
            f"La apuesta mínima es de {ton(settings.min_stake_nano)} TON.",
        )
    if int(stake) > 5_000 * NANO:
        raise MarketError("stake_too_high", "La apuesta máxima es de 5000 TON.")

    existing = await database.get_onchain_bet(int(market_id), normalize_address(wallet["address"]))
    if existing:
        raise MarketError(
            "already_bet", "Ya tienes una apuesta en este mercado (una por wallet)."
        )

    options = {int(o["idx"]): o["label"] for o in market.get("options", [])}
    label = options.get(int(option), f"Opción {int(option) + 1}")
    attached = int(stake) + BET_GAS_NANO
    tx = _prepared(
        build_place_bet(int(market_id), int(option), int(stake)),
        amount=attached,
        kind="place_bet",
        summary=f"Apostar {ton(int(stake))} TON a «{label}»",
        details={
            "market_id": int(market_id),
            "option": int(option),
            "option_label": label,
            "stake": str(int(stake)),
            "stake_ton": ton(int(stake)),
            "gas": str(BET_GAS_NANO),
            "market_title": market["title"],
        },
    )
    await database.create_pending_action(
        user_id=user_id,
        kind="bet",
        match_a=normalize_address(wallet["address"]),
        match_b=str(int(market_id)),
        payload={"option": int(option), "stake": int(stake)},
    )
    return tx


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

async def prepare_claim(user_id: int, market_id: int) -> PreparedTx:
    wallet = await require_linked_wallet(user_id)
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe.")
    if market["status"] not in ("resolved", "cancelled"):
        raise MarketError("market_open", "El mercado todavía no está resuelto.")
    address = normalize_address(wallet["address"])
    bet = await database.get_onchain_bet(int(market_id), address)
    if not bet:
        raise MarketError("no_bet", "No tienes una apuesta en este mercado.")
    if int(bet.get("claimed") or 0):
        raise MarketError("already_claimed", "Ya reclamaste este mercado.")

    estimate = estimate_payout(bet, market)
    tx = _prepared(
        build_claim_payout(int(market_id)),
        amount=CLAIM_GAS_NANO,
        kind="claim_payout",
        summary=f"Reclamar pago del mercado #{int(market_id)}",
        details={
            "market_id": int(market_id),
            "estimate": str(estimate),
            "estimate_ton": ton(estimate),
            "status": market["status"],
        },
    )
    return tx


def estimate_payout(bet: Dict[str, Any], market: Dict[str, Any]) -> int:
    """Off-chain estimate of a claim; the contract has the final word."""
    stake = int(bet.get("stake") or 0)
    if market.get("status") == "cancelled":
        return stake
    total = int(market.get("total_pool") or 0)
    winning_pool = int(market.get("winning_pool") or 0)
    if market.get("status") != "resolved" or winning_pool <= 0:
        return 0
    bet_option = bet.get("option")
    winning_option = market.get("winning_option")
    if bet_option is None or winning_option is None:
        return 0
    if int(bet_option) != int(winning_option):
        return 0
    fee_bps = int(market.get("fee_bps") or 0)
    distributable = total - (total * fee_bps) // 10_000
    return (stake * distributable) // winning_pool


# ---------------------------------------------------------------------------
# admin actions (the operator signs from their own wallet)
# ---------------------------------------------------------------------------

async def prepare_resolve(
    user_id: int, market_id: int, winning_option: int, *, now: Optional[int] = None
) -> PreparedTx:
    settings = config.get_settings()
    if user_id not in settings.admin_ids:
        raise MarketError("forbidden", "Solo un administrador puede resolver mercados.")
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe.")
    if market["status"] != "open":
        raise MarketError("market_closed", "El mercado ya no está abierto.")
    now = int(now if now is not None else time.time())
    if int(market.get("closes_at") or 0) > now:
        raise MarketError(
            "market_still_open",
            "El mercado aún no cierra: espera a la fecha de cierre para resolverlo.",
        )
    if not 0 <= int(winning_option) < int(market["option_count"]):
        raise MarketError("bad_option", "Opción ganadora fuera de rango.")

    options = {int(o["idx"]): o["label"] for o in market.get("options", [])}
    label = options.get(int(winning_option), f"Opción {int(winning_option) + 1}")
    return _prepared(
        build_resolve_market(int(market_id), int(winning_option)),
        amount=ADMIN_GAS_NANO,
        kind="resolve_market",
        summary=f"Resolver #{int(market_id)} → «{label}»",
        details={
            "market_id": int(market_id),
            "winning_option": int(winning_option),
            "winning_label": label,
            "market_title": market["title"],
        },
    )


async def prepare_cancel(user_id: int, market_id: int) -> PreparedTx:
    settings = config.get_settings()
    if user_id not in settings.admin_ids:
        raise MarketError("forbidden", "Solo un administrador puede cancelar mercados.")
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe.")
    if market["status"] != "open":
        raise MarketError("market_closed", "El mercado ya no está abierto.")
    return _prepared(
        build_cancel_market(int(market_id)),
        amount=ADMIN_GAS_NANO,
        kind="cancel_market",
        summary=f"Cancelar mercado #{int(market_id)} y habilitar reembolsos",
        details={"market_id": int(market_id), "market_title": market["title"]},
    )


# ---------------------------------------------------------------------------
# read helpers for the API
# ---------------------------------------------------------------------------

def market_view(market: Dict[str, Any], *, wallet: Optional[str] = None,
                user_id: Optional[int] = None) -> Dict[str, Any]:
    """Serialize a mirrored market with odds, deadlines and the caller's bet."""
    now = int(time.time())
    total_pool = int(market.get("pool") or market.get("total_pool") or 0)
    pools = market.get("option_pools") or {}
    options_out: List[Dict[str, Any]] = []
    for option in market.get("options", []):
        idx = int(option["idx"])
        pool = int(pools.get(str(idx), 0))
        multiplier = round(total_pool / pool, 4) if pool > 0 and total_pool > 0 else None
        options_out.append(
            {
                "index": idx,
                "label": option["label"],
                "pool": str(pool),
                "pool_ton": ton(pool),
                "multiplier": multiplier,
            }
        )

    closes_at = int(market.get("closes_at") or 0)
    view = {
        "market_id": int(market["market_id"]),
        "title": market.get("title"),
        "option_count": int(market.get("option_count") or 0),
        "closes_at": closes_at,
        "status": market.get("status"),
        "winning_option": market.get("winning_option"),
        "pool": str(total_pool),
        "pool_ton": ton(total_pool),
        "bets": int(market.get("bets") or 0),
        "fee_bps": int(market.get("fee_bps") or 0),
        "options": options_out,
        "seconds_left": max(0, closes_at - now) if closes_at else None,
        "is_open": market.get("status") == "open" and (not closes_at or closes_at > now),
        "creator": market.get("creator_address"),
    }
    if wallet:
        view["contract"] = friendly_escrow() if config.get_settings().escrow_enabled else None
    return view


async def market_detail(market_id: int, *, user_id: Optional[int] = None) -> Dict[str, Any]:
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe todavía en la cadena.")
    pools: Dict[str, int] = {}
    total = 0
    async with database.connection() as db:
        async with db.execute(
            "SELECT option, COALESCE(SUM(stake),0) AS pool FROM onchain_bets "
            "WHERE market_id = ? GROUP BY option",
            (int(market_id),),
        ) as cur:
            for row in await cur.fetchall():
                pools[str(int(row["option"]))] = int(row["pool"])
                total += int(row["pool"])
        async with db.execute(
            "SELECT COUNT(*) AS n FROM onchain_bets WHERE market_id = ?", (int(market_id),)
        ) as cur:
            row = await cur.fetchone()
            bets = int(row["n"]) if row else 0
    market["option_pools"] = pools
    market["pool"] = total
    market["bets"] = bets

    view = market_view(market)
    if user_id:
        wallet = await database.get_wallet(user_id)
        if wallet:
            bet = await database.get_onchain_bet(int(market_id), wallet["address"])
            if bet:
                view["my_bet"] = {
                    "option": int(bet["option"]),
                    "stake": str(int(bet["stake"])),
                    "stake_ton": ton(int(bet["stake"])),
                    "claimed": bool(bet["claimed"]),
                    "payout": str(bet["payout"]) if bet.get("payout") is not None else None,
                    "tx_hash": bet.get("tx_hash"),
                }
                view["my_estimate"] = str(estimate_payout(bet, market))
    return view


async def prepare_for_body(kind: str, *args, **kwargs) -> PreparedTx:
    """Dispatch helper used by API tests."""
    handlers = {
        "create_market": prepare_create_market,
        "place_bet": prepare_bet,
        "claim_payout": prepare_claim,
        "resolve_market": prepare_resolve,
        "cancel_market": prepare_cancel,
    }
    if kind not in handlers:
        raise MarketError("unknown_kind", f"Tipo de transacción desconocido: {kind}")
    return await handlers[kind](*args, **kwargs)
