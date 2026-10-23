"""Minimal, resilient HTTP client for public TON APIs.

Only *public read* endpoints are used.  The bot never signs, never broadcasts
and never needs an API secret to operate; an API key is optional and only
raises rate limits.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

import config
from bot.chain.addresses import normalize_address

logger = logging.getLogger(__name__)


class TonApiError(RuntimeError):
    """Raised when the public API cannot be reached or returns garbage."""


@dataclass
class ChainTransaction:
    """Normalized view of a toncenter v2 transaction."""

    tx_hash: str          # hex
    lt: int
    utime: int
    success: bool
    aborted: bool
    inbound_source: Optional[str] = None
    inbound_value: int = 0
    inbound_body: Optional[str] = None      # base64 BoC
    inbound_body_hash: Optional[str] = None
    out_messages: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_internal_inbound(self) -> bool:
        return bool(self.inbound_source)


def _hash_b64_to_hex(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value).hex()
    except (binascii.Error, ValueError):
        return value


def _msg_body(msg: Dict[str, Any]) -> Optional[str]:
    data = msg.get("msg_data") or {}
    body = data.get("body")
    if isinstance(body, str) and body:
        return body
    return None


def _msg_value(msg: Dict[str, Any]) -> int:
    try:
        return int(str(msg.get("value") or 0))
    except ValueError:
        return 0


def parse_transaction(raw: Dict[str, Any]) -> ChainTransaction:
    tx_id = raw.get("transaction_id") or {}
    description = raw.get("description") or {}
    compute = description.get("compute_ph") or {}
    in_msg = raw.get("in_msg") or {}
    out_msgs = raw.get("out_msgs") or []
    source = in_msg.get("source") or None
    try:
        lt = int(str(tx_id.get("lt") or 0))
    except ValueError:
        lt = 0
    return ChainTransaction(
        tx_hash=_hash_b64_to_hex(tx_id.get("hash")),
        lt=lt,
        utime=int(raw.get("utime") or 0),
        success=bool(compute.get("success")) and not bool(description.get("aborted")),
        aborted=bool(description.get("aborted")),
        inbound_source=normalize_address(source) if source else None,
        inbound_value=_msg_value(in_msg),
        inbound_body=_msg_body(in_msg),
        out_messages=out_msgs,
        raw=raw,
    )


class TonCenterClient:
    """Thin async wrapper around toncenter v2 (and optional tonapi)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        public_key_api: Optional[str] = None,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        settings = config.get_settings()
        self.base_url = (base_url or settings.ton_api_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.ton_api_key
        self.public_key_api = (public_key_api or settings.ton_public_key_api or "").rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self._session = session
        self._owns_session = session is None

    # -- lifecycle ---------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "BetPolyBot/1.0 (+non-custodial indexer)"},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- request helper ----------------------------------------------------
    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        session = await self._get_session()
        params = dict(kwargs.pop("params", {}) or {})
        if self.api_key and "toncenter" in self.base_url:
            params.setdefault("api_key", self.api_key)
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                async with session.request(method, url, params=params, **kwargs) as resp:
                    if resp.status == 429:
                        raise TonApiError("rate limited by the TON API")
                    if resp.status >= 500:
                        raise TonApiError(f"TON API error {resp.status}")
                    if resp.status >= 400:
                        text = await resp.text()
                        raise TonApiError(f"TON API {resp.status}: {text[:200]}")
                    payload = await resp.json(content_type=None)
                    if isinstance(payload, dict) and payload.get("ok") is False:
                        raise TonApiError(str(payload.get("error") or "TON API returned ok=false"))
                    return payload
            except (TimeoutError, aiohttp.ClientError, TonApiError) as exc:
                last_error = exc
                if attempt == self.retries - 1:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
        raise TonApiError(str(last_error) if last_error else "unknown TON API failure")

    # -- endpoints ---------------------------------------------------------
    async def get_transactions(
        self, address: str, *, lt: Optional[int] = None, limit: int = 50
    ) -> List[ChainTransaction]:
        """Return transactions for ``address``, newest first."""
        params: Dict[str, Any] = {"address": address, "limit": max(1, min(limit, 100))}
        if lt:
            params["lt"] = int(lt)
        payload = await self._request("GET", f"{self.base_url}/getTransactions", params=params)
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list):
            raise TonApiError("Unexpected /getTransactions payload")
        return [parse_transaction(tx) for tx in result]

    async def get_wallet_information(self, address: str) -> Dict[str, Any]:
        payload = await self._request(
            "GET", f"{self.base_url}/getWalletInformation", params={"address": address}
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}

    async def get_public_key(self, address: str) -> Optional[str]:
        """Best-effort public key lookup for already deployed wallets."""
        try:
            info = await self.get_wallet_information(address)
            key = info.get("public_key")
            if isinstance(key, str) and len(key) == 64:
                return key.lower()
        except TonApiError as exc:
            logger.debug("getWalletInformation failed for %s: %s", address, exc)

        if self.public_key_api:
            url = f"{self.public_key_api}/v2/accounts/{normalize_address(address)}"
            try:
                payload = await self._request("GET", url)
                key = (payload or {}).get("public_key") if isinstance(payload, dict) else None
                if isinstance(key, str) and len(key) == 64:
                    return key.lower()
            except TonApiError as exc:
                logger.debug("public key API failed for %s: %s", address, exc)
        return None

    async def get_account_state(self, address: str) -> Dict[str, Any]:
        payload = await self._request(
            "GET", f"{self.base_url}/getAddressInformation", params={"address": address}
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}
