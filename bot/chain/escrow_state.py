"""Read-only view of the escrow contract storage.

The contract exposes seven getters (``get_fee_bps``, ``get_paused``,
``get_fees``, ``get_total_locked``, ``get_market_count``, ``get_min_stake`` and
``get_market_data``). This module turns them into one snapshot so the admin
panels can show the *current* on-chain state instead of trusting the mirror.

Two deliberate decisions:

* **The admin address is not read from the chain.** The contract does not expose
  a ``get_admin`` getter, and adding one would change the code hash and force a
  redeploy. It comes from ``ESCROW_ADMIN_ADDRESS`` (which until now was parsed
  and never used) and is only used to *warn*, never to grant permissions.
* **Everything is cached with a short TTL.** Opening the admin panel must not
  fire seven RPC calls per tap, but a stale pause flag is dangerous, so the TTL
  is short and a failed refresh keeps serving the last good snapshot with
  ``stale=True`` instead of hiding the state.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from bot.chain.toncenter import TonCenterClient

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30

#: Getters read on every refresh, in this order.
_GETTERS = (
    ("fee_bps", "get_fee_bps"),
    ("min_stake_nano", "get_min_stake"),
    ("paused", "get_paused"),
    ("total_locked_nano", "get_total_locked"),
    ("fees_nano", "get_fees"),
    ("market_count", "get_market_count"),
)


@dataclass
class EscrowState:
    """Snapshot of the contract storage that matters to an operator."""

    address: str
    fee_bps: int = 0
    min_stake_nano: int = 0
    paused: bool = False
    total_locked_nano: int = 0
    fees_nano: int = 0
    market_count: int = 0
    balance_nano: int | None = None
    admin: str | None = None
    read_at: int = 0
    stale: bool = False
    error: str | None = None

    @property
    def is_paused(self) -> bool:
        return bool(self.paused)

    @property
    def max_withdrawable_nano(self) -> int:
        """Fees are the only funds the admin may ever take out."""
        return max(0, int(self.fees_nano))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_paused"] = self.is_paused
        data["max_withdrawable_nano"] = str(self.max_withdrawable_nano)
        data["fee_percent"] = f"{self.fee_bps / 100:.2f}"
        return data


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


async def read_escrow_state(
    client: TonCenterClient,
    address: str,
    *,
    admin: str | None = None,
    now: int | None = None,
) -> EscrowState:
    """Read every getter. Raises :class:`TonApiError` if the contract fails."""
    state = EscrowState(
        address=address,
        admin=admin,
        read_at=int(now if now is not None else time.time()),
    )
    for attribute, method in _GETTERS:
        stack = await client.run_get_method(address, method)
        value = _as_int(stack[0] if stack else 0)
        if attribute == "paused":
            state.paused = bool(value)
        else:
            setattr(state, attribute, value)

    # Balance is informational; never let it invalidate the whole snapshot.
    try:
        info = await client.get_account_state(address)
        balance = info.get("balance")
        if balance is not None:
            state.balance_nano = _as_int(balance)
    except Exception as exc:  # noqa: BLE001 - balance is best effort
        logger.debug("escrow balance unavailable: %s", exc)
    return state


@dataclass
class _Entry:
    state: EscrowState | None = None
    fetched_at: float = 0.0


class EscrowStateCache:
    """Tiny TTL cache with *stale-while-error* semantics."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = max(1, int(ttl_seconds))
        self._entries: dict[str, _Entry] = {}

    def reset(self) -> None:
        self._entries.clear()

    async def get(
        self,
        client: TonCenterClient,
        address: str,
        *,
        admin: str | None = None,
        force: bool = False,
        now: float | None = None,
    ) -> EscrowState:
        moment = float(now if now is not None else time.time())
        entry = self._entries.get(address)
        fresh = entry is not None and (moment - entry.fetched_at) < self.ttl
        if fresh and not force and entry is not None and entry.state is not None:
            return entry.state

        previous = entry.state if entry is not None else None
        try:
            state = await read_escrow_state(
                client, address, admin=admin, now=int(moment)
            )
        except Exception as exc:  # noqa: BLE001 - degrade to the last snapshot
            logger.warning("Could not refresh escrow state for %s: %s", address, exc)
            if previous is None:
                raise
            stale = EscrowState(**{**asdict(previous), "stale": True, "error": str(exc)})
            self._entries[address] = _Entry(state=stale, fetched_at=entry.fetched_at)
            return stale

        self._entries[address] = _Entry(state=state, fetched_at=moment)
        return state
