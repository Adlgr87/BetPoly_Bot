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
from typing import Any
from urllib.parse import urlencode

import config
from bot import database, economics
from bot.chain.addresses import AddressError, normalize_address, to_friendly
from bot.chain.bodies import (
    build_cancel_market,
    build_change_admin,
    build_claim_payout,
    build_create_market,
    build_place_bet,
    build_resolve_market,
    build_set_fee,
    build_set_paused,
    build_withdraw_fees,
    payload_b64,
    payload_b64url,
    title_to_hash,
)
from bot.chain.escrow_state import EscrowState, EscrowStateCache
from bot.formatting import ton

logger = logging.getLogger(__name__)

NANO = 1_000_000_000

#: Límites de validación de mercados, alineados con los del contrato.
MIN_TITLE_LENGTH = 6
MAX_TITLE_LENGTH = 140
MAX_OPTION_LENGTH = 60

#: Ventana durante la que se rechazan apuestas mientras hay una resolución
#: preparada: el contrato las rechazaría y el usuario pagaría gas para nada.
RESOLVE_WINDOW_SECONDS = 15 * 60

#: The contract caps the fee at 10 %; mirrored here to fail before signing.
MAX_FEE_BPS = 1000


# -- dinero configurable ----------------------------------------------------
# Los importes viven en ``config.Settings`` (una sola fuente, H-*); estas
# funciones existen para que nadie tenga que recordar el nombre del campo y para
# que el gas se lea siempre en el momento de preparar la transacción.

def bet_gas_nano() -> int:
    """Gas que acompaña a una apuesta, además del importe."""
    return config.get_settings().bet_gas_nano


def create_gas_nano() -> int:
    return config.get_settings().create_gas_nano


def admin_gas_nano() -> int:
    """Gas de cualquier operación de administración (funde el evento)."""
    return config.get_settings().admin_gas_nano


def claim_gas_nano() -> int:
    return config.get_settings().claim_gas_nano


def tx_ttl_seconds() -> int:
    return config.get_settings().tx_ttl_seconds

_escrow_state_cache = EscrowStateCache()


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
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
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


def tonkeeper_link(address: str, amount: int, body_b64url: str, text: str | None = None) -> str:
    """Deep link that opens a wallet with a pre-filled transaction."""
    params = {"amount": str(max(0, int(amount)))}
    if text:
        params["text"] = text[:120]
    params["bin"] = body_b64url
    return f"https://app.tonkeeper.com/transfer/{to_friendly(address)}?{urlencode(params)}"


def _prepared(
    body_cell, *, amount: int, kind: str, summary: str, details: dict[str, Any]
) -> PreparedTx:
    escrow = escrow_address()
    valid_until = int(time.time()) + tx_ttl_seconds()
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


async def require_linked_wallet(user_id: int) -> dict[str, Any]:
    wallet = await database.get_wallet(user_id)
    if not wallet:
        raise MarketError(
            "wallet_required",
            "Vincula tu wallet TON antes de operar con mercados on-chain.",
        )
    return wallet


def _assert_open(market: dict[str, Any], now: int | None = None) -> None:
    now = int(now if now is not None else time.time())
    if market["status"] != "open":
        raise MarketError("market_closed", "Este mercado ya no acepta apuestas.")
    pending_at = int(market.get("pending_resolve_at") or 0)
    if pending_at and pending_at + RESOLVE_WINDOW_SECONDS > now:
        raise MarketError(
            "market_resolving",
            "El administrador está resolviendo este mercado. Espera unos minutos.",
        )
    closes_at = int(market.get("closes_at") or 0)
    if closes_at and closes_at <= now:
        raise MarketError("market_expired", "El mercado ya cerró.")


# ---------------------------------------------------------------------------
# create market
# ---------------------------------------------------------------------------

async def prepare_create_market(
    user_id: int, title: str, options: list[str], closes_at: int
) -> PreparedTx:
    settings = config.get_settings()
    wallet = await require_linked_wallet(user_id)
    title = " ".join((title or "").split())
    if len(title) < MIN_TITLE_LENGTH:
        raise MarketError("bad_title", "El título debe tener al menos 6 caracteres.")
    if len(title) > MAX_TITLE_LENGTH:
        raise MarketError("bad_title", "El título no puede superar 140 caracteres.")
    cleaned = [" ".join(o.split()) for o in options if o and o.strip()]
    if not database.MIN_OPTIONS <= len(cleaned) <= settings.max_options:
        raise MarketError(
            "bad_options", f"Necesitas entre 2 y {settings.max_options} opciones."
        )
    if len({o.lower() for o in cleaned}) != len(cleaned):
        raise MarketError("bad_options", "Las opciones deben ser distintas.")
    if any(len(o) > MAX_OPTION_LENGTH for o in cleaned):
        raise MarketError("bad_options", "Cada opción puede tener hasta 60 caracteres.")

    now = int(time.time())
    min_minutes = settings.min_market_duration_min
    max_days = settings.max_market_duration_days
    if closes_at < now + min_minutes * 60:
        raise MarketError(
            "bad_close_date",
            f"El cierre debe ser al menos {min_minutes} minutos en el futuro.",
        )
    if closes_at > now + max_days * 86_400:
        raise MarketError(
            "bad_close_date", f"El cierre no puede estar a más de {max_days} días."
        )

    title_hash = title_to_hash(title)
    tx = _prepared(
        build_create_market(title_hash, closes_at, len(cleaned)),
        amount=create_gas_nano(),
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
    max_stake = settings.max_stake_nano
    if int(stake) > max_stake:
        raise MarketError(
            "stake_too_high", f"La apuesta máxima es de {ton(max_stake)} TON."
        )

    existing = await database.get_onchain_bet(int(market_id), normalize_address(wallet["address"]))
    if existing:
        raise MarketError(
            "already_bet", "Ya tienes una apuesta en este mercado (una por wallet)."
        )

    options = {int(o["idx"]): o["label"] for o in market.get("options", [])}
    label = options.get(int(option), f"Opción {int(option) + 1}")
    gas = bet_gas_nano()
    attached = int(stake) + gas
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
            "gas": str(gas),
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
        amount=claim_gas_nano(),
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


def estimate_payout(bet: dict[str, Any], market: dict[str, Any]) -> int:
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
    return economics.payout(stake, economics.distributable(total, fee_bps), winning_pool)


async def quote_payout(market_id: int, option: int, stake: int) -> dict[str, Any]:
    """Pago estimado de una apuesta nueva, calculado **solo** aquí (F-03).

    La Mini App no reimplementa la fórmula pari-mutuel: pide este *quote* y se
    limita a formatear lo que devuelve el backend, que es la misma función que
    usa la liquidación de puntos y que el contrato.
    """
    settings = config.get_settings()
    stake = int(stake)
    if stake <= 0:
        raise MarketError("bad_amount", "Escribe un importe mayor que cero.")
    if stake < settings.min_stake_nano:
        raise MarketError(
            "stake_too_low", f"La apuesta mínima es de {ton(settings.min_stake_nano)} TON."
        )
    if stake > settings.max_stake_nano:
        raise MarketError(
            "stake_too_high", f"La apuesta máxima es de {ton(settings.max_stake_nano)} TON."
        )
    detail = await market_detail(int(market_id))
    if not detail.get("is_open"):
        raise MarketError("market_closed", "Este mercado ya no acepta apuestas.")
    if detail.get("resolving"):
        raise MarketError(
            "market_resolving", "Hay una resolución en curso: espera unos minutos."
        )
    pools = {int(item["index"]): int(item["pool"]) for item in detail["options"]}
    if int(option) not in pools:
        raise MarketError("bad_option", "Esa opción no existe en este mercado.")
    total = int(detail["pool"])
    estimated = economics.preview_payout(
        stake, pools[int(option)], total, int(detail.get("fee_bps") or 0)
    )
    return {
        "market_id": int(market_id),
        "option": int(option),
        "stake": str(stake),
        "stake_ton": ton(stake),
        "payout": str(estimated),
        "payout_ton": ton(estimated),
        "multiplier": economics.multiplier(total + stake, pools[int(option)] + stake,
                                           int(detail.get("fee_bps") or 0)),
    }


# ---------------------------------------------------------------------------
# admin actions (the operator signs from their own wallet)
# ---------------------------------------------------------------------------

async def prepare_resolve(
    user_id: int, market_id: int, winning_option: int, *, now: int | None = None
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
    await database.set_market_pending_resolve(int(market_id), now)
    return _prepared(
        build_resolve_market(int(market_id), int(winning_option)),
        amount=admin_gas_nano(),
        kind="resolve_market",
        summary=f"Resolver #{int(market_id)} → «{label}»",
        details={
            "market_id": int(market_id),
            "winning_option": int(winning_option),
            "winning_label": label,
            "market_title": market["title"],
        },
    )


async def prepare_cancel(
    user_id: int, market_id: int, *, force: bool = False, now: int | None = None
) -> PreparedTx:
    """Cancel a market and enable refunds.

    Cancelling while the market is still open **and** already holds bets needs an
    explicit ``force``: the Telegram panel asks for confirmation, the HTTP API
    never sends it.  Otherwise a single misclick would refund a live market.
    """
    settings = config.get_settings()
    if user_id not in settings.admin_ids:
        raise MarketError("forbidden", "Solo un administrador puede cancelar mercados.")
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe.")
    if market["status"] != "open":
        raise MarketError("market_closed", "El mercado ya no está abierto.")
    now = int(now if now is not None else time.time())
    closes_at = int(market.get("closes_at") or 0)
    has_bets = await database.market_pool(int(market_id)) > 0
    if not force and closes_at > now and has_bets:
        raise MarketError(
            "cancel_needs_force",
            "El mercado sigue abierto y ya tiene apuestas. Cancela solo si es una emergencia.",
        )
    await database.set_market_pending_resolve(int(market_id), now)
    return _prepared(
        build_cancel_market(int(market_id)),
        amount=admin_gas_nano(),
        kind="cancel_market",
        summary=f"Cancelar mercado #{int(market_id)} y habilitar reembolsos",
        details={"market_id": int(market_id), "market_title": market["title"]},
    )


# ---------------------------------------------------------------------------
# escrow administration (the backend never signs: it only prepares)
# ---------------------------------------------------------------------------

def _require_admin(user_id: int) -> None:
    if int(user_id) not in config.get_settings().admin_ids:
        raise MarketError("forbidden", "Solo un administrador puede operar el contrato.")


def _configured_admin() -> str | None:
    """Admin advertised by configuration (the contract has no getter for it)."""
    return config.get_settings().escrow_admin_address or None


def _assert_signing_wallet_is_admin(wallet: dict[str, Any] | None) -> None:
    """Fail *before* signing when the operator's wallet is not the contract admin.

    Only enforced when ``ESCROW_ADMIN_ADDRESS`` is configured: without it there
    is nothing to compare against, and guessing would block legitimate operators.
    """
    expected = _configured_admin()
    if not expected or not wallet:
        return
    if normalize_address(wallet["address"]) != normalize_address(expected):
        raise MarketError(
            "wallet_not_admin",
            "Tu wallet vinculada no es la administradora del contrato. "
            "Firmar esta transacción fallaría en la cadena.",
        )


async def _admin_wallet(user_id: int) -> dict[str, Any]:
    _require_admin(user_id)
    wallet = await require_linked_wallet(user_id)
    _assert_signing_wallet_is_admin(wallet)
    return wallet


def escrow_state_cache() -> EscrowStateCache:
    """Exposed so tests and the bot can force a refresh."""
    return _escrow_state_cache


async def escrow_state(
    client: Any | None = None, *, force: bool = False
) -> EscrowState:
    """Current contract state, served from a 30 s cache (see escrow_state.py)."""
    if not config.get_settings().escrow_enabled:
        raise MarketError(
            "escrow_disabled",
            "El contrato escrow no está habilitado en esta instancia.",
        )
    from bot.chain.toncenter import TonCenterClient  # noqa: PLC0415 - grafo de imports plano

    address = escrow_address()
    admin = _configured_admin()
    if client is not None:
        return await _read_state(client, address, admin, force)
    owned = TonCenterClient()
    try:
        return await _read_state(owned, address, admin, force)
    finally:
        await owned.close()


async def _read_state(
    client: Any, address: str, admin: str | None, force: bool
) -> EscrowState:
    """Translate chain failures into one domain error (handlers never see the chain)."""
    from bot.chain.toncenter import TonApiError  # noqa: PLC0415 - solo al leer la cadena

    try:
        return await _escrow_state_cache.get(
            client, address, admin=admin, force=force
        )
    except TonApiError as exc:
        raise MarketError(
            "chain_unavailable",
            "La cadena no respondió. Inténtalo de nuevo en unos segundos.",
        ) from exc


async def prepare_withdraw_fees(
    user_id: int, amount_nano: int, *, state: EscrowState | None = None
) -> PreparedTx:
    """Withdraw accrued fees. Never touches user funds: the contract enforces it."""
    wallet = await _admin_wallet(user_id)
    state = state or await escrow_state()
    amount = int(amount_nano)
    if amount <= 0:
        raise MarketError("bad_amount", "El importe debe ser mayor que cero.")
    available = state.max_withdrawable_nano
    if amount > available:
        raise MarketError(
            "amount_too_high",
            f"Solo hay {ton(available)} TON de comisiones acumuladas.",
        )
    return _prepared(
        build_withdraw_fees(amount),
        amount=admin_gas_nano(),
        kind="withdraw_fees",
        summary=f"Retirar {ton(amount)} TON de comisiones",
        details={
            "amount": str(amount),
            "amount_ton": ton(amount),
            "available_ton": ton(available),
            "admin_wallet": wallet["address"],
        },
    )


async def prepare_set_fee(user_id: int, fee_bps: int) -> PreparedTx:
    """Change the platform fee. Applies to markets created afterwards."""
    await _admin_wallet(user_id)
    bps = int(fee_bps)
    if not 0 <= bps <= MAX_FEE_BPS:
        raise MarketError(
            "bad_fee", f"La comisión debe estar entre 0 y {MAX_FEE_BPS} puntos base (0–10 %)."
        )
    return _prepared(
        build_set_fee(bps),
        amount=admin_gas_nano(),
        kind="set_fee",
        summary=f"Cambiar la comisión a {bps / 100:.2f} %",
        details={"fee_bps": bps, "fee_percent": round(bps / 100, 2)},
    )


async def prepare_set_paused(user_id: int, paused: bool) -> PreparedTx:
    """Pause/unpause the contract. Resolutions and claims keep working when paused."""
    await _admin_wallet(user_id)
    flag = bool(paused)
    return _prepared(
        build_set_paused(flag),
        amount=admin_gas_nano(),
        kind="set_paused",
        summary="Pausar el contrato" if flag else "Reanudar el contrato",
        details={"paused": flag},
    )


def normalize_admin_address(value: str) -> str:
    """Validate a TON address for ``change_admin`` and return it raw (``0:hex``)."""
    try:
        target = normalize_address(str(value or "").strip())
    except (AddressError, ValueError) as exc:
        raise MarketError("bad_address", "La dirección no es válida.") from exc
    if int(target.split(":")[1], 16) == 0:
        raise MarketError("bad_address", "No se puede usar la dirección cero.")
    return target


async def prepare_change_admin(user_id: int, new_address: str) -> PreparedTx:
    """Transfer contract administration. Irreversible: double-check the address."""
    await _admin_wallet(user_id)
    target = normalize_admin_address(new_address)
    current = _configured_admin()
    if current and target == normalize_address(current):
        raise MarketError("same_admin", "Esa ya es la dirección administradora.")
    return _prepared(
        build_change_admin(target),
        amount=admin_gas_nano(),
        kind="change_admin",
        summary=f"Transferir la administración a {to_friendly(target)}",
        details={"new_admin": to_friendly(target), "new_admin_raw": target},
    )


# ---------------------------------------------------------------------------
# read helpers for the API
# ---------------------------------------------------------------------------

def _is_resolving(market: dict[str, Any], now: int) -> bool:
    pending_at = int(market.get("pending_resolve_at") or 0)
    return bool(pending_at) and pending_at + RESOLVE_WINDOW_SECONDS > now


def _option_views(
    market: dict[str, Any], total_pool: int, fee_bps: int
) -> list[dict[str, Any]]:
    """Una entrada por opción, con su multiplicador ya calculado (F-03)."""
    pools = market.get("option_pools") or {}
    views: list[dict[str, Any]] = []
    for option in market.get("options", []):
        idx = int(option["idx"])
        pool = int(pools.get(str(idx), 0))
        views.append(
            {
                "index": idx,
                "label": option["label"],
                "pool": str(pool),
                "pool_ton": ton(pool),
                "multiplier": economics.multiplier(total_pool, pool, fee_bps),
            }
        )
    return views


def _open_now(market: dict[str, Any], closes_at: int, now: int) -> bool:
    """Abierto = estado `open`, sin cierre pasado y sin resolución preparada."""
    if market.get("status") != "open":
        return False
    if closes_at and closes_at <= now:
        return False
    return not _is_resolving(market, now)


def market_view(market: dict[str, Any], *, wallet: str | None = None) -> dict[str, Any]:
    """Serialize a mirrored market with odds, deadlines and the caller's bet."""
    now = int(time.time())
    total_pool = int(market.get("pool") or market.get("total_pool") or 0)
    fee_bps = int(market.get("fee_bps") or 0)
    options_out = _option_views(market, total_pool, fee_bps)
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
        "fee_bps": fee_bps,
        "fee_percent": f"{fee_bps / 100:.2f}",
        "options": options_out,
        "seconds_left": max(0, closes_at - now) if closes_at else None,
        "is_open": _open_now(market, closes_at, now),
        "resolving": _is_resolving(market, now),
        "creator": market.get("creator_address"),
    }
    if wallet:
        view["contract"] = friendly_escrow() if config.get_settings().escrow_enabled else None
    return view


async def market_detail(market_id: int, *, user_id: int | None = None) -> dict[str, Any]:
    market = await database.get_market(int(market_id))
    if not market:
        raise MarketError("market_not_found", "Ese mercado no existe todavía en la cadena.")
    pools: dict[str, int] = {}
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
