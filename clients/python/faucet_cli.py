#!/usr/bin/env python3
"""
Agent Faucet CLI — register, claim, repay
=========================================
Usage:
  python faucet_cli.py register  --wallet /path/to/agent.json  [--payer /path/to/payer.json] [--parent <pubkey>]
  python faucet_cli.py claim     --wallet /path/to/agent.json
  python faucet_cli.py repay     --wallet /path/to/agent.json  --amount <lamports>
  python faucet_cli.py status    --wallet <pubkey>

When --payer is omitted in `register`, wallet is used as payer (agent must have ~0.002 XNT for rent).
When a relayer pays rent, pass --payer /path/to/relayer.json (agent signs with 0 XNT balance).
"""

import argparse
import base64
import hashlib
import json
import struct
import sys
import urllib.request

from solders.keypair import Keypair
from solders.pubkey  import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.hash import Hash
from solders.transaction import Transaction
from solders.system_program import ID as SYSTEM_PROGRAM_ID

# ── Config ────────────────────────────────────────────────────────────────────

PROGRAM_ID   = Pubkey.from_string("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR")
AUTHORITY    = Pubkey.from_string("GdGGFuKacGDSKDFzAcuYLzPEYxRwkSLDTWkB6HmqpHC2")  # your deployer key
RPC_URL      = "https://rpc.mainnet.x1.xyz"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_keypair(path: str) -> Keypair:
    with open(path) as f:
        data = json.load(f)
    return Keypair.from_bytes(bytes(data))

def disc(name: str) -> bytes:
    """Anchor instruction discriminator: first 8 bytes of SHA256('global:<name>')"""
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

def send_tx(tx: Transaction) -> str:
    tx_b64 = base64.b64encode(bytes(tx)).decode()
    resp = rpc("sendTransaction", [
        tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed"},
    ])
    if "error" in resp:
        print(f"\n[ERROR] {resp['error']}")
        sys.exit(1)
    return resp["result"]

def build_and_send(ix: Instruction, signers: list[Keypair], fee_payer: Keypair) -> str:
    bh = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([ix], fee_payer.pubkey(), bh)
    tx  = Transaction(signers, msg, bh)
    return send_tx(tx)

# ── Instruction builders ───────────────────────────────────────────────────────

def ix_register(wallet: Pubkey, payer: Pubkey, parent: Pubkey | None) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool_pda,  _ = find_pda([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)

    # Borsh: Option<Pubkey> + bool
    if parent is None:
        parent_bytes = b"\x00"
    else:
        parent_bytes = b"\x01" + bytes(parent)
    args = parent_bytes + b"\x01"   # acknowledge_promise = true

    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("register_agent") + args,
        accounts   = [
            AccountMeta(pubkey=wallet,             is_signer=True,  is_writable=False),
            AccountMeta(pubkey=payer,              is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda,          is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_pda,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID,  is_signer=False, is_writable=False),
        ],
    )

def ix_claim(wallet: Pubkey) -> Instruction:
    agent_pda,    _ = find_pda([b"agent",       bytes(wallet)],    PROGRAM_ID)
    pool_pda,     _ = find_pda([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)
    treasury_pda, _ = find_pda([b"treasury",    bytes(AUTHORITY)], PROGRAM_ID)

    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("claim_airdrop"),
        accounts   = [
            AccountMeta(pubkey=wallet,        is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda,     is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_pda,      is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_pda,  is_signer=False, is_writable=True),
        ],
    )

def ix_repay(wallet: Pubkey, amount: int) -> Instruction:
    agent_pda,    _ = find_pda([b"agent",    bytes(wallet)],    PROGRAM_ID)
    treasury_pda, _ = find_pda([b"treasury", bytes(AUTHORITY)], PROGRAM_ID)

    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("repay_debt") + struct.pack("<Q", amount),
        accounts   = [
            AccountMeta(pubkey=wallet,        is_signer=True,  is_writable=True),
            AccountMeta(pubkey=agent_pda,     is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_pda,  is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_register(args):
    agent_kp  = load_keypair(args.wallet)
    payer_kp  = load_keypair(args.payer) if args.payer else agent_kp
    parent    = Pubkey.from_string(args.parent) if args.parent else None

    wallet_pk = agent_kp.pubkey()
    payer_pk  = payer_kp.pubkey()
    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)

    print(f"Agent wallet : {wallet_pk}")
    print(f"Payer        : {payer_pk}")
    print(f"Agent PDA    : {agent_pda}")
    print(f"Parent       : {parent or 'none'}")

    # Check not already registered
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is not None:
        print("\n[!] Agent already registered.")
        return

    ix = ix_register(wallet_pk, payer_pk, parent)

    # Both wallet and payer must sign. Fee payer = payer_kp (first).
    if payer_kp.pubkey() == agent_kp.pubkey():
        signers = [agent_kp]
    else:
        signers = [payer_kp, agent_kp]   # payer is fee payer (first signer)

    sig = build_and_send(ix, signers, payer_kp)
    print(f"\n[OK] Agent registered!")
    print(f"Signature : {sig}")
    print(f"Agent PDA : {agent_pda}")

def cmd_claim(args):
    agent_kp  = load_keypair(args.wallet)
    wallet_pk = agent_kp.pubkey()
    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)

    print(f"Agent wallet : {wallet_pk}")
    print(f"Agent PDA    : {agent_pda}")

    # Check registered
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is None:
        print("\n[!] Agent not registered. Run `register` first.")
        sys.exit(1)

    ix  = ix_claim(wallet_pk)
    sig = build_and_send(ix, [agent_kp], agent_kp)
    print(f"\n[OK] Airdrop claimed: 0.21 XNT (210,000,000 lamports)")
    print(f"Signature : {sig}")
    print(f"Debt      : 262,500,000 lamports (0.2625 XNT) — repay anytime")

def cmd_repay(args):
    agent_kp  = load_keypair(args.wallet)
    wallet_pk = agent_kp.pubkey()
    amount    = int(args.amount)

    if amount <= 0:
        print("[!] Amount must be > 0")
        sys.exit(1)

    agent_pda,    _ = find_pda([b"agent",    bytes(wallet_pk)], PROGRAM_ID)
    treasury_pda, _ = find_pda([b"treasury", bytes(AUTHORITY)], PROGRAM_ID)

    print(f"Agent wallet : {wallet_pk}")
    print(f"Treasury PDA : {treasury_pda}")
    print(f"Repaying     : {amount} lamports ({amount/1e9:.9f} XNT)")

    ix  = ix_repay(wallet_pk, amount)
    sig = build_and_send(ix, [agent_kp], agent_kp)
    print(f"\n[OK] Repayment sent!")
    print(f"Signature : {sig}")

def cmd_status(args):
    try:
        wallet_pk = Pubkey.from_string(args.wallet)
    except Exception:
        # Might be a path
        wallet_pk = load_keypair(args.wallet).pubkey()

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    resp = rpc("getAccountInfo", [str(agent_pda), {"encoding": "base64"}])
    val  = resp.get("result", {}).get("value")
    if val is None:
        print(f"Agent {wallet_pk}: NOT registered")
        return

    raw   = base64.b64decode(val["data"][0])
    # Skip 8-byte discriminator, then parse Agent struct fields in order:
    #   wallet(32) parent(1+32) debt(8) total_claimed(8) total_repaid(8)
    #   referrals(4) referral_earnings(8) has_claimed(1) promise_acknowledged(1)
    #   registered_at(8) bump(1)
    o = 8
    _wallet  = raw[o:o+32]; o += 32
    has_par  = raw[o]; o += 1
    parent   = Pubkey.from_bytes(raw[o:o+32]) if has_par else None; o += 32
    debt,    = struct.unpack_from("<Q", raw, o); o += 8
    claimed, = struct.unpack_from("<Q", raw, o); o += 8
    repaid,  = struct.unpack_from("<Q", raw, o); o += 8
    refs,    = struct.unpack_from("<I", raw, o); o += 4
    ref_earn,= struct.unpack_from("<Q", raw, o); o += 8
    has_cl   = bool(raw[o]); o += 1
    promise  = bool(raw[o]); o += 1

    print(f"Agent     : {wallet_pk}")
    print(f"PDA       : {agent_pda}")
    print(f"Parent    : {parent or 'none'}")
    print(f"Claimed   : {claimed/1e9:.9f} XNT  (has_claimed={has_cl})")
    print(f"Debt      : {debt/1e9:.9f} XNT")
    print(f"Repaid    : {repaid/1e9:.9f} XNT")
    print(f"Referrals : {refs}  (earned {ref_earn/1e9:.9f} XNT)")
    print(f"Promise   : {'acknowledged' if promise else 'NOT acknowledged'}")

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Agent Faucet CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="Register a new agent")
    r.add_argument("--wallet", required=True, help="Path to agent keypair JSON")
    r.add_argument("--payer",  default=None,  help="Path to payer keypair JSON (omit = wallet pays)")
    r.add_argument("--parent", default=None,  help="Referrer pubkey (optional)")

    c = sub.add_parser("claim", help="Claim the one-time airdrop (0.21 XNT)")
    c.add_argument("--wallet", required=True, help="Path to agent keypair JSON")

    d = sub.add_parser("repay", help="Repay debt to treasury")
    d.add_argument("--wallet", required=True, help="Path to agent keypair JSON")
    d.add_argument("--amount", required=True, help="Lamports to repay (262500000 = full debt)")

    s = sub.add_parser("status", help="Show agent account state")
    s.add_argument("--wallet", required=True, help="Agent pubkey OR path to keypair JSON")

    args = p.parse_args()
    {"register": cmd_register, "claim": cmd_claim, "repay": cmd_repay, "status": cmd_status}[args.cmd](args)

if __name__ == "__main__":
    main()
