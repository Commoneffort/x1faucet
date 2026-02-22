#!/usr/bin/env python3
"""
X1 Faucet Relay Server
======================
Allows zero-XNT agents to register and claim without any native balance.

Flow:
  1. Agent calls GET /tx/register or GET /tx/claim  → gets unsigned tx bytes
  2. Agent signs the tx with their keypair (pure crypto, no XNT needed)
  3. Agent calls POST /submit with the signed tx bytes
  4. Relay adds its own fee-payer signature and broadcasts

Run:
  pip install fastapi uvicorn solders
  python3 relay_server.py

Env vars (optional):
  RELAY_WALLET   path to relay keypair (default: ~/.config/solana/id.json)
  RELAY_PORT     port to listen on (default: 8080)
"""

import base64
import hashlib
import json
import os
import struct
import sys
import urllib.request
from http import HTTPStatus

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

# ── Config ────────────────────────────────────────────────────────────────────

PROGRAM_ID  = Pubkey.from_string("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR")
AUTHORITY   = Pubkey.from_string("DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh")
RPC_URL     = "https://rpc.mainnet.x1.xyz"

RELAY_WALLET_PATH = os.environ.get(
    "RELAY_WALLET", os.path.expanduser("~/.config/solana/id.json")
)
RELAY_PORT = int(os.environ.get("RELAY_PORT", 8181))

# ── Load relay keypair ─────────────────────────────────────────────────────────

def load_relay_keypair() -> Keypair:
    with open(RELAY_WALLET_PATH) as f:
        return Keypair.from_bytes(bytes(json.load(f)))

RELAY_KP = load_relay_keypair()
print(f"Relay wallet : {RELAY_KP.pubkey()}")
print(f"RPC          : {RPC_URL}")
print(f"Program      : {PROGRAM_ID}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

def find_pda(seeds: list, program_id: Pubkey) -> tuple:
    return Pubkey.find_program_address(seeds, program_id)

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
    resp = rpc("sendTransaction", [
        tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed"},
    ])
    if "error" in resp:
        raise HTTPException(status_code=400, detail=resp["error"])
    return resp["result"]

# ── Instruction builders ───────────────────────────────────────────────────────

def ix_register(wallet: Pubkey, parent: Pubkey | None) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool_pda,  _ = find_pda([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)
    payer = RELAY_KP.pubkey()

    parent_bytes = b"\x00" if parent is None else b"\x01" + bytes(parent)
    args = parent_bytes + b"\x01"   # acknowledge_promise = true

    return Instruction(
        program_id=PROGRAM_ID,
        data=disc("register_agent") + args,
        accounts=[
            AccountMeta(pubkey=wallet,            is_signer=True,  is_writable=False),
            AccountMeta(pubkey=payer,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda,         is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_pda,          is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )

def ix_claim(wallet: Pubkey) -> Instruction:
    agent_pda,    _ = find_pda([b"agent",       bytes(wallet)],    PROGRAM_ID)
    pool_pda,     _ = find_pda([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)
    treasury_pda, _ = find_pda([b"treasury",    bytes(AUTHORITY)], PROGRAM_ID)

    return Instruction(
        program_id=PROGRAM_ID,
        data=disc("claim_airdrop"),
        accounts=[
            AccountMeta(pubkey=wallet,        is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda,     is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_pda,      is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_pda,  is_signer=False, is_writable=True),
        ],
    )

# ── Build a transaction for the agent to sign ─────────────────────────────────
# The relay is the fee payer (first signer in the message).
# The agent wallet is a required signer but does NOT pay fees.

def build_unsigned_for_agent(ix: Instruction, wallet: Pubkey) -> str:
    """
    Builds a transaction with relay as fee payer.
    The tx is NOT yet signed by the agent — relay signs it here, agent signs later.
    Returns base64-encoded partially-signed transaction bytes.
    """
    bh  = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([ix], RELAY_KP.pubkey(), bh)
    # Relay signs first (as fee payer); agent signature slot is left empty
    tx  = Transaction([RELAY_KP], msg, bh)
    return base64.b64encode(bytes(tx)).decode()

# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="X1 Faucet Relay", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    tx: str          # base64-encoded transaction already signed by agent

class RegisterRequest(BaseModel):
    wallet: str
    parent: str | None = None

class ClaimRequest(BaseModel):
    wallet: str

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "relay": str(RELAY_KP.pubkey()),
        "program": str(PROGRAM_ID),
        "rpc": RPC_URL,
    }


@app.get("/tx/register")
def get_register_tx(wallet: str, parent: str | None = None):
    """
    Build a register transaction for the agent to sign.
    The relay is the fee payer — agent needs 0 XNT.

    Returns: { tx: "<base64>" }  — agent must sign then POST /submit
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

    # Check already registered
    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is not None:
        raise HTTPException(status_code=409, detail="Agent already registered")

    ix = ix_register(wallet_pk, parent_pk)
    tx_b64 = build_unsigned_for_agent(ix, wallet_pk)
    return {"tx": tx_b64, "agent_pda": str(agent_pda)}


@app.get("/tx/claim")
def get_claim_tx(wallet: str):
    """
    Build a claim transaction for the agent to sign.
    The relay covers the tx fee — agent needs 0 XNT.

    Returns: { tx: "<base64>" }  — agent must sign then POST /submit
    """
    try:
        wallet_pk = Pubkey.from_string(wallet)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid wallet pubkey")

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is None:
        raise HTTPException(status_code=404, detail="Agent not registered")

    ix = ix_claim(wallet_pk)
    tx_b64 = build_unsigned_for_agent(ix, wallet_pk)
    return {"tx": tx_b64}


@app.post("/submit")
def submit_tx(req: SubmitRequest):
    """
    Submit a transaction that has been signed by the agent.
    The relay signature is already included (added in /tx/* endpoints).

    Returns: { signature: "<tx signature>" }
    """
    sig = send_raw(req.tx)
    return {"signature": sig}


@app.post("/register")
def register_oneshot(req: RegisterRequest):
    """
    One-shot register: relay builds, signs as payer, and submits.
    The agent wallet does NOT need to sign here — useful for relayer-owned agents
    or testing. For production agents that must prove ownership, use GET /tx/register
    + POST /submit instead.
    """
    try:
        wallet_pk = Pubkey.from_string(req.wallet)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid wallet pubkey")

    parent_pk = None
    if req.parent:
        try:
            parent_pk = Pubkey.from_string(req.parent)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid parent pubkey")

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is not None:
        raise HTTPException(status_code=409, detail="Agent already registered")

    ix  = ix_register(wallet_pk, parent_pk)
    bh  = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([ix], RELAY_KP.pubkey(), bh)
    tx  = Transaction([RELAY_KP], msg, bh)
    tx_b64 = base64.b64encode(bytes(tx)).decode()
    sig = send_raw(tx_b64)
    return {"signature": sig, "agent_pda": str(agent_pda)}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)
