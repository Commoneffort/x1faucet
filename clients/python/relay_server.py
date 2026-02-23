#!/usr/bin/env python3
"""
X1 Faucet Relay Server v2
==========================
Allows zero-XNT agents to register and claim without any native balance.
Updated for v2 program: pool_v2 seeds, no treasury, no acknowledge_promise.

Security fixes applied (Relay-1 through Relay-5):
  Relay-1: /submit validates all instruction program_ids == PROGRAM_ID
  Relay-2: RPC errors sanitized — raw Solana errors never returned to caller
  Relay-3: agent_has_claimed() wrapped in try/except with len validation
  Relay-4: /register oneshot REMOVED (wallet must sign; relay cannot sign for wallet)
  Relay-5: LimitUploadSize middleware — 64KB max request body

Flow:
  1. Agent calls GET /tx/register or GET /tx/claim  → gets partially-signed tx bytes
  2. Agent signs the tx with their keypair (pure crypto, no XNT needed)
  3. Agent calls POST /submit with the signed tx bytes
  4. Relay broadcasts to RPC

Run:
  pip install fastapi uvicorn solders slowapi
  python3 relay_server.py

Env vars (optional):
  RELAY_WALLET   path to relay keypair (default: ~/.config/solana/id.json)
  RELAY_PORT     port to listen on (default: 7181)
"""

import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
import urllib.request
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

# ── Config ────────────────────────────────────────────────────────────────────

PROGRAM_ID = Pubkey.from_string("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR")
AUTHORITY  = Pubkey.from_string("DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh")
RPC_URL    = "https://rpc.mainnet.x1.xyz"

RELAY_WALLET_PATH = os.environ.get(
    "RELAY_WALLET", os.path.expanduser("~/.config/solana/id.json")
)
RELAY_PORT = int(os.environ.get("RELAY_PORT", 7181))

# Maximum number of registrations the relay will ever sponsor.
RELAY_MAX_REGISTRATIONS = int(os.environ.get("RELAY_MAX_REGISTRATIONS", 500))

# Counter file — survives restarts. Reset manually when deploying new program.
_COUNTER_FILE = os.path.join(os.path.dirname(__file__), ".relay_reg_count")

def _load_counter() -> int:
    # INFO-3 fix: distinguish "file not found" (normal first run) from
    # "file exists but corrupt" (warn loudly rather than silently resetting).
    if not os.path.exists(_COUNTER_FILE):
        return 0
    try:
        return int(open(_COUNTER_FILE).read().strip())
    except Exception as e:
        print(f"[WARN] Counter file corrupt or unreadable: {e}. Starting at 0.", file=sys.stderr)
        return 0

def _save_counter(n: int) -> None:
    with open(_COUNTER_FILE, "w") as f:
        f.write(str(n))

_reg_count = _load_counter()
# MED-NEW-2 fix: asyncio lock prevents concurrent requests from bypassing the cap.
_reg_lock = asyncio.Lock()

async def _check_and_bump_registrations() -> None:
    global _reg_count
    async with _reg_lock:
        if _reg_count >= RELAY_MAX_REGISTRATIONS:
            raise HTTPException(
                status_code=503,
                detail=f"Relay sponsorship limit reached ({RELAY_MAX_REGISTRATIONS}). "
                       "Contact the operator to raise the cap.",
            )
        _reg_count += 1
        _save_counter(_reg_count)

# ── Load relay keypair ─────────────────────────────────────────────────────────

def load_relay_keypair() -> Keypair:
    with open(RELAY_WALLET_PATH) as f:
        return Keypair.from_bytes(bytes(json.load(f)))

RELAY_KP = load_relay_keypair()
print(f"Relay wallet : {RELAY_KP.pubkey()}")
print(f"RPC          : {RPC_URL}")
print(f"Program      : {PROGRAM_ID}")
print(f"Registrations: {_reg_count}/{RELAY_MAX_REGISTRATIONS}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

def find_pda(seeds: list, program_id: Pubkey) -> tuple:
    return Pubkey.find_program_address(seeds, program_id)

def pool_pda_v2() -> Pubkey:
    pda, _ = find_pda([b"pool_v2", bytes(AUTHORITY)], PROGRAM_ID)
    return pda

def rpc(method: str, params: list) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(
        RPC_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def latest_blockhash() -> str:
    resp = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    return resp["result"]["value"]["blockhash"]

def send_raw(tx_b64: str) -> str:
    """
    Relay-2: RPC errors are logged server-side but never forwarded to the caller.
    Clients receive a generic "Transaction failed" message on error.
    """
    resp = rpc("sendTransaction", [
        tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed"},
    ])
    if "error" in resp:
        # Log full error on the server, return generic message to caller
        print(f"[RPC ERROR] {resp['error']}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Transaction failed")
    return resp["result"]

def validate_transaction_programs(tx_b64: str) -> None:
    """
    Relay-1: Ensure every instruction in the transaction targets only
    PROGRAM_ID or SYSTEM_PROGRAM_ID. Rejects any unexpected program calls.
    """
    try:
        tx_bytes = base64.b64decode(tx_b64)
        tx = Transaction.from_bytes(tx_bytes)
        msg = tx.message
        for ix in msg.instructions:
            prog_id = msg.account_keys[ix.program_id_index]
            if prog_id != PROGRAM_ID and prog_id != SYSTEM_PROGRAM_ID:
                print(f"[SECURITY] Rejected tx with disallowed program: {prog_id}", file=sys.stderr)
                raise HTTPException(
                    status_code=400,
                    detail="Transaction contains an unexpected program instruction"
                )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid transaction format")

# Agent account layout (v2 — after 8-byte discriminator):
#   wallet(32)  pool(32)  parent Option<Pubkey>(1+[32])
#   debt(8)  total_claimed(8)  total_repaid(8)  referrals(4)
#   referral_earnings(8)  referral_pending(8)  has_claimed(1) ...
def agent_has_claimed(raw: bytes) -> bool:
    """
    Relay-3: Wrapped in try/except with length validation.
    Returns True if Agent account has has_claimed == 1.
    """
    try:
        # Minimum valid agent size (discriminator + all fixed fields, parent=None)
        # = 8 + 32 + 32 + 1 + 8 + 8 + 8 + 4 + 8 + 8 + 1 = 118 minimum
        if len(raw) < 118:
            print(f"[WARN] agent_has_claimed: raw data too short ({len(raw)} bytes)", file=sys.stderr)
            return False

        off = 8 + 32 + 32   # skip discriminator + wallet + pool (v2 has pool field)
        if off >= len(raw):
            return False

        has_par = raw[off]; off += 1
        off += 32 if has_par else 0     # Option<Pubkey>
        if off + 8 + 8 + 8 + 4 + 8 + 8 + 1 > len(raw):
            return False
        off += 8 + 8 + 8 + 4 + 8 + 8   # debt, claimed, repaid, referrals, ref_earn, ref_pending (v2)
        return raw[off] == 1            # has_claimed
    except Exception as e:
        print(f"[WARN] agent_has_claimed parse error: {e}", file=sys.stderr)
        return False

# ── Instruction builders (v2 program) ─────────────────────────────────────────

def ix_register(wallet: Pubkey, parent: Pubkey | None) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool         = pool_pda_v2()
    payer        = RELAY_KP.pubkey()

    # v2: no acknowledge_promise byte — just Option<Pubkey> for parent
    parent_bytes = b"\x00" if parent is None else b"\x01" + bytes(parent)

    accounts = [
        AccountMeta(pubkey=wallet,            is_signer=True,  is_writable=False),
        AccountMeta(pubkey=payer,             is_signer=True,  is_writable=True),
        AccountMeta(pubkey=agent_pda,         is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool,              is_signer=False, is_writable=True),
    ]

    if parent is not None:
        parent_agent_pda, _ = find_pda([b"agent", bytes(parent)], PROGRAM_ID)
        accounts.append(AccountMeta(pubkey=parent_agent_pda, is_signer=False, is_writable=True))

    accounts.append(AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False))

    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("register_agent") + parent_bytes,
        accounts   = accounts,
    )

def ix_claim(wallet: Pubkey) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool         = pool_pda_v2()

    # v2: no treasury account
    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("claim_airdrop"),
        accounts   = [
            AccountMeta(pubkey=wallet,    is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool,      is_signer=False, is_writable=True),
        ],
    )

# ── Build a transaction for the agent to sign ─────────────────────────────────

def build_unsigned_for_agent(ix: Instruction, wallet: Pubkey) -> str:
    """
    Builds a partially-signed transaction (relay signs as fee payer).
    Relay occupies signer slot 0; all other signer slots get null signatures.
    Agent must fill their slot and POST to /submit.
    """
    bh  = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([ix], RELAY_KP.pubkey(), bh)
    # Relay signs its own slot (index 0 — fee payer is always first signer).
    # Remaining required signer slots get null (all-zero) signatures for the
    # agent to fill before submitting.
    relay_sig  = RELAY_KP.sign_message(bytes(msg))
    n_signers  = msg.header.num_required_signatures
    sigs       = [relay_sig] + [Signature.default()] * (n_signers - 1)
    tx         = Transaction.populate(msg, sigs)
    return base64.b64encode(bytes(tx)).decode()

# ── Middleware ─────────────────────────────────────────────────────────────────

class LimitUploadSize(BaseHTTPMiddleware):
    """
    Relay-5 + MED-NEW-1 fix: Enforce body size limit for ALL requests.
    Checks Content-Length header first (fast path), then reads the actual
    body stream to catch chunked transfer encoding that omits the header.
    """
    def __init__(self, app, max_size: int = 65536):  # 64KB default
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_size:
                    return Response(
                        content="Request body too large (max 64KB)",
                        status_code=413
                    )
            except ValueError:
                return Response(content="Invalid content-length", status_code=400)

        # MED-NEW-1 fix: also enforce against chunked bodies (no Content-Length header).
        # Read the stream, accumulate, then store as _body so Starlette's
        # _CachedRequest.wrapped_receive (used by call_next) returns the real body.
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > self.max_size:
                return Response(
                    content="Request body too large (max 64KB)",
                    status_code=413
                )

        # Starlette ≥0.21 uses _CachedRequest whose wrapped_receive checks _body
        # before _stream_consumed.  Setting _body here ensures the downstream app
        # receives the complete body even though we already consumed the stream.
        request._body = body
        return await call_next(request)

# ── FastAPI app ────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="X1 Faucet Relay", version="2.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Relay-5: request body size limit
app.add_middleware(LimitUploadSize, max_size=65536)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    tx: str  # base64-encoded transaction already signed by agent

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "relay": str(RELAY_KP.pubkey()),
        "program": str(PROGRAM_ID),
        "program_version": "v2",
        "pool_pda": str(pool_pda_v2()),
        "rpc": RPC_URL,
        "registrations_sponsored": _reg_count,
        "registrations_cap": RELAY_MAX_REGISTRATIONS,
    }


@app.get("/tx/register")
@limiter.limit("5/hour")
async def get_register_tx(request: Request, wallet: str, parent: str | None = None):
    """
    Build a register transaction for the agent to sign.
    Relay is fee payer — agent needs 0 XNT. Pool reimburses rent.

    Returns: { tx: "<base64>", agent_pda: "<pda>" }
    Agent must sign and POST /submit.
    """
    try:
        wallet_pk = Pubkey.from_string(wallet)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid wallet pubkey")

    parent_pk = None
    if parent:
        try:
            parent_pk = Pubkey.from_string(parent)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid parent pubkey")
        if parent_pk == wallet_pk:
            raise HTTPException(status_code=400, detail="Cannot refer yourself")

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is not None:
        raise HTTPException(status_code=409, detail="Agent already registered")

    await _check_and_bump_registrations()
    ix     = ix_register(wallet_pk, parent_pk)
    tx_b64 = build_unsigned_for_agent(ix, wallet_pk)
    return {"tx": tx_b64, "agent_pda": str(agent_pda)}


@app.get("/tx/claim")
@limiter.limit("5/hour")
def get_claim_tx(request: Request, wallet: str):
    """
    Build a claim transaction for the agent to sign.
    Relay covers tx fee — agent needs 0 XNT.

    Returns: { tx: "<base64>" }
    Agent must sign and POST /submit.
    """
    try:
        wallet_pk = Pubkey.from_string(wallet)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid wallet pubkey")

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    value = resp.get("result", {}).get("value")
    if value is None:
        raise HTTPException(status_code=404, detail="Agent not registered")

    try:
        raw = base64.b64decode(value["data"][0])
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read agent account")

    if agent_has_claimed(raw):
        raise HTTPException(status_code=409, detail="Agent has already claimed")

    ix     = ix_claim(wallet_pk)
    tx_b64 = build_unsigned_for_agent(ix, wallet_pk)
    return {"tx": tx_b64}


@app.post("/submit")
@limiter.limit("20/hour")
def submit_tx(request: Request, req: SubmitRequest):
    """
    Submit a transaction that the agent has signed.
    Relay-1: validates that all instructions target only PROGRAM_ID or SystemProgram.
    Relay-2: RPC errors sanitized — generic "Transaction failed" returned on error.

    Returns: { signature: "<tx signature>" }
    """
    # Relay-1: reject transactions containing unexpected programs
    validate_transaction_programs(req.tx)

    sig = send_raw(req.tx)
    return {"signature": sig}


# NOTE: /register oneshot endpoint REMOVED (Relay-4).
# Reason: the wallet must sign the transaction to prove ownership.
# The relay cannot sign on behalf of a wallet it does not control.
# Use GET /tx/register → agent signs → POST /submit instead.


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)
