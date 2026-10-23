"""TON address helpers.

Internally addresses are always stored in the canonical *raw* form
``<workchain>:<64 hex chars>`` so comparisons are stable.  Users see the
friendly, URL-safe, bounceable form.
"""
from __future__ import annotations

from pytoniq_core import Address


class AddressError(ValueError):
    """Raised when a string is not a usable TON address."""


def parse_address(value: str) -> Address:
    if not isinstance(value, str) or not value.strip():
        raise AddressError("Dirección vacía.")
    raw = value.strip()
    try:
        address = Address(raw)
    except Exception as exc:  # noqa: BLE001 - library raises many types
        raise AddressError(f"Dirección TON inválida: {value!r}") from exc
    if address is None:  # pragma: no cover - defensive
        raise AddressError(f"Dirección TON inválida: {value!r}")
    return address


def raw_parts(value: str) -> tuple[int, bytes]:
    """Return ``(workchain, hash_bytes)`` for an address."""
    address = parse_address(value)
    raw = address.to_str(
        is_user_friendly=False, is_url_safe=True, is_bounceable=False, is_test_only=False
    )
    wc_str, _, hash_hex = raw.partition(":")
    if not hash_hex:
        raise AddressError(f"Dirección TON inválida: {value!r}")
    try:
        return int(wc_str), bytes.fromhex(hash_hex)
    except ValueError as exc:  # pragma: no cover - defensive
        raise AddressError(f"Dirección TON inválida: {value!r}") from exc


def normalize_address(value: str) -> str:
    """Canonical ``workchain:hex`` representation used across the codebase."""
    wc, hash_bytes = raw_parts(value)
    return f"{wc}:{hash_bytes.hex()}"


def to_friendly(value: str, *, bounceable: bool = True, testnet: bool = False) -> str:
    address = parse_address(value)
    return address.to_str(
        is_user_friendly=True,
        is_url_safe=True,
        is_bounceable=bounceable,
        is_test_only=testnet,
    )


def is_valid_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        parse_address(value)
        return True
    except AddressError:
        return False


def same_address(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return normalize_address(a) == normalize_address(b)
    except AddressError:
        return False


def address_hash(value: str) -> bytes:
    _, hash_bytes = raw_parts(value)
    return hash_bytes
