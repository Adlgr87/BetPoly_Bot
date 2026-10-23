"""TON Connect ``ton_proof`` verification.

A TON wallet proves ownership of an address by signing::

    message = "ton-proof-item-v2/"
              + workchain:int32be + address_hash:32 bytes
              + len(domain):uint32le + domain:utf8
              + timestamp:uint64le
              + payload:utf8
    digest  = sha256(0xFF 0xFF + "ton-connect" + sha256(message))
    proof   = ed25519_sign(private_key, digest)

The backend must recover the wallet public key from the *state init* that TON
Connect sends along with the address.  Rather than hard-coding wallet code
hashes (which break with every new wallet version), this module accepts a
candidate key only if:

1. ``hash(state_init_cell) == address_hash`` — the state init really defines
   that address, and
2. the candidate key verifies the signature over a message that commits to the
   address, domain, timestamp and single-use nonce.

That combination is self-authenticating and wallet-version agnostic.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Iterable

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pytoniq_core import Cell

from bot.chain.addresses import (
    AddressError,
    normalize_address,
    parse_address,
    raw_parts,
    to_friendly,
)

TON_PROOF_PREFIX = b"ton-proof-item-v2/"
TON_CONNECT_PREFIX = b"ton-connect"


class ProofError(ValueError):
    """Verification failed. ``code`` is a stable, machine readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerifiedWallet:
    address: str          # canonical "0:hex"
    friendly_address: str
    public_key: str       # hex
    workchain: int
    timestamp: int


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def build_proof_message(
    *, workchain: int, address_hash: bytes, domain: str, timestamp: int, payload: str
) -> bytes:
    if len(address_hash) != 32:
        raise ProofError("bad_address", "El hash de la dirección debe tener 32 bytes.")
    domain_bytes = domain.encode("utf-8")
    return b"".join(
        (
            TON_PROOF_PREFIX,
            workchain.to_bytes(4, "big", signed=True),
            address_hash,
            len(domain_bytes).to_bytes(4, "little", signed=False),
            domain_bytes,
            int(timestamp).to_bytes(8, "little", signed=False),
            payload.encode("utf-8"),
        )
    )


def build_proof_digest(**kwargs) -> bytes:
    message = build_proof_message(**kwargs)
    return _sha256(b"\xff\xff" + TON_CONNECT_PREFIX + _sha256(message))


# ---------------------------------------------------------------------------
# state init handling
# ---------------------------------------------------------------------------

def parse_state_init(raw: str) -> Cell:
    """Decode a base64 ``walletStateInit`` BoC into a cell."""
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ProofError("bad_state_init", "walletStateInit no es base64 válido.") from exc
    try:
        return Cell.one_from_boc(data)
    except Exception as exc:  # noqa: BLE001 - library raises generic errors
        raise ProofError("bad_state_init", "walletStateInit no es un BoC válido.") from exc


def state_init_data_cell(state_init: Cell) -> Cell:
    """Return the ``data`` cell of a ``StateInit``."""
    s = state_init.begin_parse()
    try:
        if s.remaining_bits >= 1:
            s.load_bit()  # split_depth (Maybe)
        if s.remaining_bits >= 1:
            s.load_bit()  # special (Maybe)
        if s.remaining_bits < 1:
            raise ProofError("bad_state_init", "StateInit truncado.")
        if s.load_bit():  # either bit
            s.load_ref()  # code
            data = s.load_ref()
        else:
            if s.load_bit():
                s.load_ref()  # code
            data = s.load_ref() if s.load_bit() else None
    except ProofError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProofError("bad_state_init", "StateInit mal formado.") from exc
    if data is None:
        raise ProofError("bad_state_init", "StateInit sin celda de datos.")
    return data


def _candidate_keys(data_cell: Cell, max_depth: int = 1) -> list[bytes]:
    """Scan 256-bit windows of the data cell (and one level of refs)."""
    cells: list[Cell] = [data_cell]
    for _ in range(max_depth):
        extra: list[Cell] = []
        for cell in cells[-1:]:
            try:
                refs = getattr(cell, "refs", None) or []
            except Exception:  # pragma: no cover - defensive
                refs = []
            for ref in refs:
                extra.append(ref)
        cells.extend(extra)

    candidates: list[bytes] = []
    seen = set()
    for cell in cells:
        try:
            s = cell.begin_parse()
            total = s.remaining_bits
            if total < 256:
                continue
            raw = s.load_bits(total).tobytes()
        except Exception:  # pragma: no cover - defensive
            continue
        # Wallet data cells are byte aligned: walk every 256-bit window and let
        # the signature tell us which one is the real public key.
        for start in range(0, len(raw) - 32 + 1):
            chunk = raw[start:start + 32]
            if chunk in seen:
                continue
            seen.add(chunk)
            candidates.append(chunk)
    return candidates


def _verify(digest: bytes, signature: bytes, public_key: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        VerifyKey(public_key).verify(digest, signature)
        return True
    except (BadSignatureError, ValueError):
        return False


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def verify_ton_proof(
    *,
    address: str,
    network: str = "-239",
    proof: dict,
    expected_domain: str,
    expected_nonce: str,
    max_age_seconds: int = 600,
    state_init: str | None = None,
    public_key: str | None = None,
    onchain_public_key: str | None = None,
    now: int | None = None,
) -> VerifiedWallet:
    """Validate a ``ton_proof`` payload.

    The body is a checklist of small steps; each one raises ``ProofError`` with
    its own code, so the API can answer with a precise message and this function
    stays readable (R-07).

    Parameters
    ----------
    proof:
        ``{"timestamp", "domain": {"lengthBytes", "value"}, "signature", "payload"}``.
    state_init:
        Base64 BoC from ``account.walletStateInit`` (preferred, works offline).
    public_key:
        Optional key reported by the SDK; when present it must match the
        recovered key.
    onchain_public_key:
        Fallback key fetched from a public API when the wallet is already
        deployed and no state init was provided.
    """
    if not isinstance(proof, dict):
        raise ProofError("bad_proof", "Falta el objeto proof.")

    domain_value = _check_domain(proof, expected_domain)
    timestamp = _check_timestamp(proof, max_age_seconds, now)
    payload = _check_payload(proof, expected_nonce)
    signature = _decode_signature(proof)
    parsed, workchain, address_hash = _check_address(address, network)

    digest = build_proof_digest(
        workchain=workchain,
        address_hash=address_hash,
        domain=domain_value,
        timestamp=timestamp,
        payload=payload,
    )

    state_init_cell = _check_state_init(state_init, address_hash)
    resolved_key = _resolve_public_key(digest, signature, state_init_cell, onchain_public_key)
    _check_declared_key(public_key, resolved_key)

    raw = normalize_address(parsed.to_str())
    return VerifiedWallet(
        address=raw,
        friendly_address=to_friendly(raw),
        public_key=resolved_key.hex(),
        workchain=workchain,
        timestamp=timestamp,
    )


def _check_domain(proof: dict, expected_domain: str) -> str:
    domain = proof.get("domain") or {}
    value = domain.get("value")
    length_bytes = domain.get("lengthBytes")
    if not isinstance(value, str) or not value:
        raise ProofError("bad_domain", "El proof no incluye dominio.")
    if value != expected_domain:
        raise ProofError("bad_domain", "El dominio del proof no coincide con el de la app.")
    if not isinstance(length_bytes, int) or length_bytes != len(value.encode("utf-8")):
        raise ProofError("bad_domain", "lengthBytes no coincide con el dominio.")
    return value


def _check_timestamp(proof: dict, max_age_seconds: int, now: int | None) -> int:
    try:
        timestamp = int(proof.get("timestamp"))
    except (TypeError, ValueError) as exc:
        raise ProofError("bad_timestamp", "timestamp inválido.") from exc
    current = int(now if now is not None else time.time())
    if abs(current - timestamp) > int(max_age_seconds):
        raise ProofError("expired_proof", "El proof expiró; vuelve a conectar la wallet.")
    return timestamp


def _check_payload(proof: dict, expected_nonce: str) -> str:
    payload = proof.get("payload") or ""
    if not expected_nonce or not hmac.compare_digest(str(payload), str(expected_nonce)):
        raise ProofError("bad_payload", "El payload del proof no corresponde a esta sesión.")
    return str(payload)


def _decode_signature(proof: dict) -> bytes:
    try:
        signature = base64.b64decode(proof.get("signature") or "", validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ProofError("bad_signature", "Firma no es base64 válido.") from exc
    if len(signature) != 64:
        raise ProofError("bad_signature", "La firma debe tener 64 bytes.")
    return signature


def _check_address(address: str, network: str) -> tuple[Any, int, bytes]:
    try:
        parsed = parse_address(address)
    except AddressError as exc:
        raise ProofError("bad_address", str(exc)) from exc
    workchain, address_hash = raw_parts(address)
    if workchain != 0:
        raise ProofError("bad_address", "Solo se aceptan wallets de la basechain.")
    if network not in ("-239", "-3", 0, "0"):
        raise ProofError("bad_network", "Red no soportada (usa mainnet o testnet).")
    return parsed, workchain, address_hash


def _check_state_init(state_init: str | None, address_hash: bytes) -> Cell | None:
    """El state init debe definir exactamente la dirección declarada."""
    if not state_init:
        return None
    cell = parse_state_init(state_init)
    if cell.hash != address_hash:
        raise ProofError(
            "state_init_mismatch",
            "walletStateInit no corresponde a la dirección declarada.",
        )
    return cell


def _resolve_public_key(
    digest: bytes,
    signature: bytes,
    state_init_cell: Cell | None,
    onchain_public_key: str | None,
) -> bytes:
    """Prueba las claves candidatas; solo la firma decide cuál es la buena.

    Primero el ``state_init`` (funciona sin red y cubre cualquier versión de
    wallet); si no hay o no basta, la clave publicada on-chain.
    """
    if state_init_cell is not None:
        for candidate in _candidate_keys(state_init_data_cell(state_init_cell)):
            if _verify(digest, signature, candidate):
                return candidate
    if onchain_public_key:
        key = _hex_key(onchain_public_key)
        if key and _verify(digest, signature, key):
            return key
    raise ProofError(
        "bad_signature",
        "No se pudo verificar la firma de la wallet (clave pública desconocida).",
    )


def _check_declared_key(public_key: str | None, resolved_key: bytes) -> None:
    """Si el SDK declaró una clave, tiene que ser la que firmó."""
    if not public_key:
        return
    declared = _hex_key(public_key)
    if declared is not None and not hmac.compare_digest(declared, resolved_key):
        raise ProofError("key_mismatch", "La clave pública no coincide con la firma.")


def _hex_key(value: str) -> bytes | None:
    try:
        key = bytes.fromhex(value.strip())
    except (ValueError, AttributeError):
        return None
    return key if len(key) == 32 else None


def public_keys_from_state_init(state_init_b64: str) -> Iterable[bytes]:
    """Expose candidate keys (used by tests and debugging tools)."""
    return _candidate_keys(state_init_data_cell(parse_state_init(state_init_b64)))
