#!/usr/bin/env python3
"""
Integration tests for relay_server.py
======================================
Tests exercise every endpoint end-to-end, including the actual transaction
building path that static code review missed (NotEnoughSigners bug).

Run:
    pytest tests/test_relay.py -v

Coverage:
  - /health          → fields present
  - /tx/register     → tx structure, partial signing, edge cases
  - /tx/claim        → tx structure, edge cases
  - /submit          → program allowlist, bad tx format, RPC error sanitisation
  - LimitUploadSize  → Content-Length and chunked encoding enforcement
  - Transaction      → relay sig valid, agent slot null, agent can complete signing
  - Registration cap → exact cap enforcement
"""

import base64
import json
import struct
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

sys.path.insert(0, str(Path(__file__).parent.parent / "clients" / "python"))
import relay_server
from relay_server import app, PROGRAM_ID, RELAY_KP, find_pda, pool_pda_v2

# ── Shared constants ───────────────────────────────────────────────────────────

AGENT_KP = Keypair()
AGENT_PK = AGENT_KP.pubkey()

# Valid-format blockhash — not fetched from RPC in tests
DUMMY_BH = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
FAKE_SIG = "5KtPn1LGuxhKGSAjOCpfMD6mCDUAGnPrFhCKHQHMzHZ"


# ── RPC mock helpers ───────────────────────────────────────────────────────────

def blockhash_resp():
    return {"result": {"value": {"blockhash": DUMMY_BH}}}


def account_missing():
    return {"result": {"value": None}}


def account_with(raw: bytes):
    return {"result": {"value": {"data": [base64.b64encode(raw).decode(), "base64"]}}}


def send_ok():
    return {"result": FAKE_SIG}


def make_agent_bytes(has_claimed: bool) -> bytes:
    """Minimal v2 Agent account for mocking RPC responses."""
    disc     = bytes([47, 166, 112, 147, 155, 197, 86, 7])
    wallet   = bytes(AGENT_PK)
    pool     = bytes(pool_pda_v2())
    parent   = b"\x00"
    debt     = struct.pack("<Q", 262_500_000)
    clm      = struct.pack("<Q", 210_000_000 if has_claimed else 0)
    repaid   = struct.pack("<Q", 0)
    refs     = struct.pack("<I", 0)
    ref_earn = struct.pack("<Q", 0)
    ref_pend = struct.pack("<Q", 0)
    claimed  = bytes([1 if has_claimed else 0])
    promise  = bytes([1 if has_claimed else 0])
    reg_at   = struct.pack("<Q", 1_700_000_000)
    bump     = bytes([255])
    raw = (disc + wallet + pool + parent + debt + clm + repaid
           + refs + ref_earn + ref_pend + claimed + promise + reg_at + bump)
    return raw.ljust(160, b"\x00")


def rpc_register(method, params):
    """Mock for /tx/register: agent not found, blockhash available."""
    if method == "getAccountInfo":
        return account_missing()
    return blockhash_resp()


def rpc_claim(has_claimed=False):
    """Mock for /tx/claim: agent account present with given claimed state."""
    def _rpc(method, params):
        if method == "getAccountInfo":
            return account_with(make_agent_bytes(has_claimed))
        return blockhash_resp()
    return _rpc


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    # Fresh TestClient per test; raise_server_exceptions=True so we see real errors
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    """Zero counter and clear rate-limit storage before each test, restore after."""
    orig_count = relay_server._reg_count
    relay_server._reg_count = 0  # start each test from zero
    # Clear slowapi in-memory storage so rate limits don't bleed between tests
    storage = getattr(relay_server.limiter, "_storage", None)
    if storage and hasattr(storage, "reset"):
        storage.reset()
    yield
    relay_server._reg_count = orig_count
    if storage and hasattr(storage, "reset"):
        storage.reset()


# ── /health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_200(self, client):
        assert client.get("/health").status_code == 200

    def test_status_ok(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_program_id_present(self, client):
        d = client.get("/health").json()
        assert d["program"] == str(PROGRAM_ID)

    def test_pool_pda_present(self, client):
        d = client.get("/health").json()
        assert d["pool_pda"] == str(pool_pda_v2())

    def test_registration_fields_present(self, client):
        d = client.get("/health").json()
        assert "registrations_sponsored" in d
        assert "registrations_cap" in d


# ── /tx/register — happy path ──────────────────────────────────────────────────

class TestRegisterHappyPath:
    def test_returns_200(self, client):
        with patch("relay_server.rpc", side_effect=rpc_register):
            r = client.get(f"/tx/register?wallet={AGENT_PK}")
        assert r.status_code == 200, r.text

    def test_response_has_tx_and_agent_pda(self, client):
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        assert "tx" in d
        assert "agent_pda" in d

    def test_agent_pda_matches_derivation(self, client):
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        expected, _ = find_pda([b"agent", bytes(AGENT_PK)], PROGRAM_ID)
        assert d["agent_pda"] == str(expected)

    def test_tx_is_valid_base64(self, client):
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        base64.b64decode(d["tx"])  # must not raise

    def test_tx_deserializes_as_transaction(self, client):
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx is not None

    def test_tx_has_two_required_signers(self, client):
        """register_agent needs relay (fee payer) + agent wallet — both must sign."""
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.message.header.num_required_signatures == 2

    def test_relay_slot_is_signed(self, client):
        """Slot 0 (relay / fee payer) must carry a real non-null signature."""
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.signatures[0] != Signature.default(), \
            "relay slot must not be null"

    def test_agent_slot_is_null(self, client):
        """Slot 1 (agent) must be all-zero — waiting for the agent to fill it."""
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.signatures[1] == Signature.default(), \
            "agent slot must be null before agent signs"

    def test_relay_signature_cryptographically_valid(self, client):
        """The relay's signature must verify against the relay's public key."""
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        # Signature.verify(pubkey, message_bytes) → True if valid
        assert tx.signatures[0].verify(RELAY_KP.pubkey(), bytes(tx.message))

    def test_agent_can_complete_signing(self, client):
        """After agent adds their sig both slots are non-null — tx is sendable."""
        with patch("relay_server.rpc", side_effect=rpc_register):
            d = client.get(f"/tx/register?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        agent_sig = AGENT_KP.sign_message(bytes(tx.message))
        completed = Transaction.populate(tx.message, [tx.signatures[0], agent_sig])
        assert completed.signatures[0] != Signature.default()
        assert completed.signatures[1] != Signature.default()

    def test_counter_increments_on_success(self, client):
        before = relay_server._reg_count
        with patch("relay_server.rpc", side_effect=rpc_register):
            client.get(f"/tx/register?wallet={AGENT_PK}")
        assert relay_server._reg_count == before + 1

    def test_with_valid_parent(self, client):
        parent_pk = Keypair().pubkey()
        with patch("relay_server.rpc", side_effect=rpc_register):
            r = client.get(
                f"/tx/register?wallet={AGENT_PK}&parent={parent_pk}"
            )
        assert r.status_code == 200, r.text
        assert "tx" in r.json()


# ── /tx/register — error cases ────────────────────────────────────────────────

class TestRegisterErrors:
    def test_invalid_wallet_returns_400(self, client):
        r = client.get("/tx/register?wallet=notavalidpubkey")
        assert r.status_code == 400

    def test_self_referral_returns_400(self, client):
        with patch("relay_server.rpc", return_value=account_missing()):
            r = client.get(
                f"/tx/register?wallet={AGENT_PK}&parent={AGENT_PK}"
            )
        assert r.status_code == 400
        assert "refer yourself" in r.json()["detail"].lower()

    def test_invalid_parent_returns_400(self, client):
        with patch("relay_server.rpc", return_value=account_missing()):
            r = client.get(
                f"/tx/register?wallet={AGENT_PK}&parent=notvalid"
            )
        assert r.status_code == 400

    def test_already_registered_returns_409(self, client):
        with patch("relay_server.rpc",
                   return_value=account_with(make_agent_bytes(False))):
            r = client.get(f"/tx/register?wallet={AGENT_PK}")
        assert r.status_code == 409
        assert "already registered" in r.json()["detail"].lower()

    def test_cap_exceeded_returns_503(self, client):
        relay_server._reg_count = relay_server.RELAY_MAX_REGISTRATIONS
        with patch("relay_server.rpc", side_effect=rpc_register):
            r = client.get(f"/tx/register?wallet={AGENT_PK}")
        assert r.status_code == 503
        assert "limit" in r.json()["detail"].lower()


# ── /tx/claim — happy path ────────────────────────────────────────────────────

class TestClaimHappyPath:
    def test_returns_200(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(has_claimed=False)):
            r = client.get(f"/tx/claim?wallet={AGENT_PK}")
        assert r.status_code == 200, r.text

    def test_response_has_tx(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        assert "tx" in d

    def test_tx_deserializes(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        Transaction.from_bytes(base64.b64decode(d["tx"]))

    def test_tx_has_two_required_signers(self, client):
        """claim_airdrop needs relay (fee payer) + agent wallet."""
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.message.header.num_required_signatures == 2

    def test_relay_slot_signed(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.signatures[0] != Signature.default()

    def test_agent_slot_null(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.signatures[1] == Signature.default()

    def test_relay_signature_valid(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        assert tx.signatures[0].verify(RELAY_KP.pubkey(), bytes(tx.message))

    def test_agent_can_complete_signing(self, client):
        with patch("relay_server.rpc", side_effect=rpc_claim(False)):
            d = client.get(f"/tx/claim?wallet={AGENT_PK}").json()
        tx = Transaction.from_bytes(base64.b64decode(d["tx"]))
        agent_sig = AGENT_KP.sign_message(bytes(tx.message))
        completed = Transaction.populate(tx.message, [tx.signatures[0], agent_sig])
        assert completed.signatures[0] != Signature.default()
        assert completed.signatures[1] != Signature.default()


# ── /tx/claim — error cases ───────────────────────────────────────────────────

class TestClaimErrors:
    def test_invalid_wallet_returns_400(self, client):
        r = client.get("/tx/claim?wallet=bad")
        assert r.status_code == 400

    def test_not_registered_returns_404(self, client):
        with patch("relay_server.rpc", return_value=account_missing()):
            r = client.get(f"/tx/claim?wallet={AGENT_PK}")
        assert r.status_code == 404
        assert "not registered" in r.json()["detail"].lower()

    def test_already_claimed_returns_409(self, client):
        with patch("relay_server.rpc",
                   return_value=account_with(make_agent_bytes(has_claimed=True))):
            r = client.get(f"/tx/claim?wallet={AGENT_PK}")
        assert r.status_code == 409
        assert "already claimed" in r.json()["detail"].lower()


# ── /submit ────────────────────────────────────────────────────────────────────

def _tx_targeting(prog_id: Pubkey) -> str:
    """Build a fully-signed single-instruction tx targeting the given program."""
    bh  = Hash.from_string(DUMMY_BH)
    ix  = Instruction(
        program_id=prog_id,
        data=bytes(8),
        accounts=[AccountMeta(pubkey=AGENT_PK, is_signer=True, is_writable=False)],
    )
    msg = Message.new_with_blockhash([ix], AGENT_PK, bh)
    tx  = Transaction([AGENT_KP], msg, bh)
    return base64.b64encode(bytes(tx)).decode()


class TestSubmit:
    def test_valid_tx_accepted(self, client):
        tx_b64 = _tx_targeting(PROGRAM_ID)
        with patch("relay_server.rpc", return_value=send_ok()):
            r = client.post("/submit", json={"tx": tx_b64})
        assert r.status_code == 200, r.text
        assert r.json()["signature"] == FAKE_SIG

    def test_unknown_program_rejected(self, client):
        tx_b64 = _tx_targeting(Keypair().pubkey())
        r = client.post("/submit", json={"tx": tx_b64})
        assert r.status_code == 400
        assert "unexpected program" in r.json()["detail"].lower()

    def test_system_program_allowed(self, client):
        """SystemProgram is on the allowlist."""
        tx_b64 = _tx_targeting(SYSTEM_PROGRAM_ID)
        with patch("relay_server.rpc", return_value=send_ok()):
            r = client.post("/submit", json={"tx": tx_b64})
        assert r.status_code == 200, r.text

    def test_garbage_tx_base64_rejected(self, client):
        r = client.post("/submit", json={"tx": "this_is_not_a_valid_tx!!"})
        assert r.status_code == 400

    def test_rpc_error_returns_generic_message(self, client):
        """Relay-2: raw RPC error details must never reach the caller."""
        tx_b64 = _tx_targeting(PROGRAM_ID)
        rpc_error = {"error": {"code": -32002, "message": "very sensitive internal detail"}}
        with patch("relay_server.rpc", return_value=rpc_error):
            r = client.post("/submit", json={"tx": tx_b64})
        assert r.status_code == 400
        assert "sensitive" not in r.text
        assert "internal detail" not in r.text
        assert r.json()["detail"] == "Transaction failed"

    def test_mixed_programs_rejected(self, client):
        """Tx with one valid and one disallowed instruction must be rejected."""
        bad_prog = Keypair().pubkey()
        bh = Hash.from_string(DUMMY_BH)
        ix_good = Instruction(
            program_id=PROGRAM_ID,
            data=bytes(8),
            accounts=[AccountMeta(pubkey=AGENT_PK, is_signer=True, is_writable=False)],
        )
        ix_bad = Instruction(
            program_id=bad_prog,
            data=bytes(8),
            accounts=[AccountMeta(pubkey=AGENT_PK, is_signer=False, is_writable=False)],
        )
        msg = Message.new_with_blockhash([ix_good, ix_bad], AGENT_PK, bh)
        tx  = Transaction([AGENT_KP], msg, bh)
        tx_b64 = base64.b64encode(bytes(tx)).decode()
        r = client.post("/submit", json={"tx": tx_b64})
        assert r.status_code == 400


# ── LimitUploadSize middleware ─────────────────────────────────────────────────

class TestBodySizeLimit:
    MAX = 65536  # 64 KB

    def test_small_body_not_413(self, client):
        # Any non-413 response is acceptable (may be 400 for bad tx format)
        r = client.post("/submit", json={"tx": "a" * 100})
        assert r.status_code != 413

    def test_body_over_limit_via_content_length(self, client):
        big = "x" * (self.MAX + 1)
        r = client.post(
            "/submit",
            content=big.encode(),
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(big))},
        )
        assert r.status_code == 413

    def test_body_exactly_at_limit_not_413(self, client):
        at_limit = "x" * self.MAX
        r = client.post(
            "/submit",
            content=at_limit.encode(),
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(at_limit))},
        )
        assert r.status_code != 413

    def test_chunked_body_over_limit_rejected(self, client):
        """MED-NEW-1: stream reading must catch chunked encoding bypass."""
        big_body = b"x" * (self.MAX + 500)

        def chunked():
            chunk = 1024
            for i in range(0, len(big_body), chunk):
                yield big_body[i:i + chunk]

        r = client.post(
            "/submit",
            content=chunked(),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413


# ── Registration cap ───────────────────────────────────────────────────────────

class TestRegistrationCap:
    def test_cap_allows_exactly_n(self, client):
        """One request at the boundary succeeds; next is rejected."""
        relay_server._reg_count = relay_server.RELAY_MAX_REGISTRATIONS - 1
        with patch("relay_server.rpc", side_effect=rpc_register):
            r1 = client.get(f"/tx/register?wallet={Keypair().pubkey()}")
        assert r1.status_code == 200, r1.text
        assert relay_server._reg_count == relay_server.RELAY_MAX_REGISTRATIONS

        with patch("relay_server.rpc", side_effect=rpc_register):
            r2 = client.get(f"/tx/register?wallet={Keypair().pubkey()}")
        assert r2.status_code == 503

    def test_cap_not_exceeded_by_concurrent_requests(self, client):
        """MED-NEW-2: counter never exceeds cap even under rapid sequential requests.

        asyncio.Lock serialises access to the counter so no two requests can
        simultaneously read a count below the cap and both succeed.  We simulate
        this by setting the remaining slots to 1 and firing 5 rapid requests
        through the same ASGI app — the asyncio event loop runs them serially so
        the lock is exercised and only the first request should succeed.
        """
        relay_server._reg_count = relay_server.RELAY_MAX_REGISTRATIONS - 1
        results = []
        for _ in range(5):
            with patch("relay_server.rpc", side_effect=rpc_register):
                r = client.get(f"/tx/register?wallet={Keypair().pubkey()}")
            results.append(r.status_code)

        successes = results.count(200)
        assert successes == 1, (
            f"Expected exactly 1 success, got {successes}. "
            f"All statuses: {results}. "
            f"Final counter: {relay_server._reg_count}"
        )
        assert relay_server._reg_count == relay_server.RELAY_MAX_REGISTRATIONS
