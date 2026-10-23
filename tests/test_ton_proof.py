"""TON Connect `ton_proof` verification (Ed25519 + state-init address binding)."""
from __future__ import annotations

import base64
import time

import pytest
from nacl.signing import SigningKey
from pytoniq_core import begin_cell

from bot.chain.addresses import normalize_address
from bot.chain.ton_proof import (
    ProofError,
    build_proof_digest,
    parse_state_init,
    public_keys_from_state_init,
    state_init_data_cell,
    verify_ton_proof,
)

DOMAIN = "betpoly.test"
NONCE = "0123456789abcdef0123456789abcdef"


def make_wallet(seed: bytes = b"\x01" * 32, seqno: int = 0):
    """Build a synthetic wallet StateInit whose data cell holds the public key."""
    signing_key = SigningKey(seed)
    public_key = bytes(signing_key.verify_key)
    data = (
        begin_cell()
        .store_uint(seqno, 32)      # wallet seqno
        .store_uint(0, 32)          # subwallet id
        .store_bytes(public_key)    # 256 bit key
        .store_dict(None)           # plugins / extensions
        .end_cell()
    )
    code = begin_cell().store_uint(0xDEADBEEF, 32).end_cell()
    state_init = (
        begin_cell()
        .store_bit(0)      # split_depth: none
        .store_bit(0)      # special: none
        .store_bit(1)      # either bit: code + data
        .store_ref(code)
        .store_ref(data)
        .end_cell()
    )
    address_hash = state_init.hash
    return {
        "signing_key": signing_key,
        "public_key": public_key,
        "data": data,
        "state_init": state_init,
        "state_init_b64": base64.b64encode(state_init.to_boc()).decode(),
        "address": "0:" + address_hash.hex(),
    }


def make_proof(wallet, *, domain=DOMAIN, payload=NONCE, timestamp=None, tamper=False,
               signing_key=None):
    timestamp = int(timestamp if timestamp is not None else time.time())
    digest = build_proof_digest(
        workchain=0,
        address_hash=bytes.fromhex(wallet["address"].split(":")[1]),
        domain=domain,
        timestamp=timestamp,
        payload=payload,
    )
    signature = (signing_key or wallet["signing_key"]).sign(digest).signature
    if tamper:
        signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
    return {
        "timestamp": timestamp,
        "domain": {"lengthBytes": len(domain.encode()), "value": domain},
        "signature": base64.b64encode(signature).decode(),
        "payload": payload,
    }


def verify(wallet, proof, **overrides):
    params = dict(
        address=wallet["address"],
        network="-239",
        proof=proof,
        expected_domain=DOMAIN,
        expected_nonce=NONCE,
        state_init=wallet["state_init_b64"],
        max_age_seconds=600,
    )
    params.update(overrides)
    return verify_ton_proof(**params)


class TestStateInit:
    def test_extracts_public_key(self):
        wallet = make_wallet()
        keys = list(public_keys_from_state_init(wallet["state_init_b64"]))
        assert wallet["public_key"] in keys

    def test_data_cell_is_the_second_ref(self):
        wallet = make_wallet()
        data = state_init_data_cell(parse_state_init(wallet["state_init_b64"]))
        assert data.hash == wallet["data"].hash

    def test_rejects_garbage(self):
        with pytest.raises(ProofError):
            parse_state_init("not base64 !!")


class TestVerification:
    def test_valid_proof(self):
        wallet = make_wallet()
        result = verify(wallet, make_proof(wallet))
        assert result.address == normalize_address(wallet["address"])
        assert result.public_key == wallet["public_key"].hex()
        assert result.workchain == 0

    def test_wrong_domain(self):
        wallet = make_wallet()
        proof = make_proof(wallet, domain="evil.example")
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "bad_domain"

    def test_domain_length_must_match(self):
        wallet = make_wallet()
        proof = make_proof(wallet)
        proof["domain"]["lengthBytes"] = 999
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "bad_domain"

    def test_expired_proof(self):
        wallet = make_wallet()
        proof = make_proof(wallet, timestamp=time.time() - 3600)
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "expired_proof"

    def test_future_proof(self):
        wallet = make_wallet()
        proof = make_proof(wallet, timestamp=time.time() + 3600)
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "expired_proof"

    def test_wrong_nonce(self):
        wallet = make_wallet()
        proof = make_proof(wallet, payload="another-nonce")
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "bad_payload"

    def test_tampered_signature(self):
        wallet = make_wallet()
        proof = make_proof(wallet, tamper=True)
        with pytest.raises(ProofError) as exc:
            verify(wallet, proof)
        assert exc.value.code == "bad_signature"

    def test_signature_from_another_key_is_rejected(self):
        victim = make_wallet(seed=b"\x02" * 32)
        attacker = make_wallet(seed=b"\x03" * 32)
        # attacker signs but claims the victim's address + state init
        proof = make_proof(attacker)
        with pytest.raises(ProofError) as exc:
            verify(victim, proof)
        assert exc.value.code == "bad_signature"

    def test_state_init_must_match_address(self):
        wallet = make_wallet()
        other = make_wallet(seed=b"\x04" * 32)
        with pytest.raises(ProofError) as exc:
            verify(wallet, make_proof(wallet), state_init=other["state_init_b64"])
        assert exc.value.code == "state_init_mismatch"

    def test_masterchain_addresses_rejected(self):
        wallet = make_wallet()
        proof = make_proof(wallet)
        with pytest.raises(ProofError) as exc:
            verify_ton_proof(
                address="-1:" + wallet["address"].split(":")[1],
                network="-239",
                proof=proof,
                expected_domain=DOMAIN,
                expected_nonce=NONCE,
                state_init=None,
                onchain_public_key=wallet["public_key"].hex(),
            )
        assert exc.value.code == "bad_address"

    def test_unsupported_network(self):
        wallet = make_wallet()
        with pytest.raises(ProofError) as exc:
            verify(wallet, make_proof(wallet), network="-1")
        assert exc.value.code == "bad_network"

    def test_onchain_key_fallback(self):
        wallet = make_wallet()
        result = verify_ton_proof(
            address=wallet["address"],
            network="-239",
            proof=make_proof(wallet),
            expected_domain=DOMAIN,
            expected_nonce=NONCE,
            state_init=None,
            onchain_public_key=wallet["public_key"].hex(),
        )
        assert result.public_key == wallet["public_key"].hex()

    def test_missing_key_fails_closed(self):
        wallet = make_wallet()
        with pytest.raises(ProofError) as exc:
            verify_ton_proof(
                address=wallet["address"],
                network="-239",
                proof=make_proof(wallet),
                expected_domain=DOMAIN,
                expected_nonce=NONCE,
                state_init=None,
                onchain_public_key=None,
            )
        assert exc.value.code == "bad_signature"

    def test_reported_public_key_must_match(self):
        wallet = make_wallet()
        other = make_wallet(seed=b"\x05" * 32)
        with pytest.raises(ProofError) as exc:
            verify(wallet, make_proof(wallet), public_key=other["public_key"].hex())
        assert exc.value.code == "key_mismatch"

    def test_deployed_wallet_with_rotated_key(self):
        """State init with a stale key can still verify through the onchain key."""
        wallet = make_wallet(seed=b"\x06" * 32)
        rotated = make_wallet(seed=b"\x07" * 32)
        # signature made by the rotated (live) key over THIS wallet address
        proof = make_proof(wallet, signing_key=rotated["signing_key"])
        result = verify_ton_proof(
            address=wallet["address"],
            network="-239",
            proof=proof,
            expected_domain=DOMAIN,
            expected_nonce=NONCE,
            state_init=wallet["state_init_b64"],
            onchain_public_key=rotated["public_key"].hex(),
        )
        assert result.public_key == rotated["public_key"].hex()
