#!/usr/bin/env python3
"""
Agent Faucet CLI v2 — register, claim, repay, init, withdraw, drain
=====================================================================
Usage:
  python faucet_cli.py init       --wallet ~/.config/solana/id.json [--claim-amount 210000000]
  python faucet_cli.py fund       --wallet /path/to/funder.json  --amount <lamports>
  python faucet_cli.py register   --wallet /path/to/agent.json   [--payer /path/to/payer.json] [--parent <pubkey>]
  python faucet_cli.py claim      --wallet /path/to/agent.json   [--payer /path/to/payer.json]
  python faucet_cli.py repay      --wallet /path/to/agent.json   --amount <lamports>
  python faucet_cli.py status     --wallet <pubkey_or_path>
  python faucet_cli.py withdraw   --wallet ~/.config/solana/id.json --amount <lamports> [--recipient <pubkey>]
  python faucet_cli.py pool       --wallet <authority_pubkey_or_path>

  # Stage 0 drain commands (old v1 program — run BEFORE Stage 1 redeploy):
  python faucet_cli.py drain       --wallet ~/.config/solana/id.json  # drain treasury (withdraw_treasury)
  python faucet_cli.py drain-pool  --wallet ~/.config/solana/id.json  # drain pool (drain_pool, needs intermediate deploy)

Pool PDA uses NEW seeds: [b"pool_v2", authority]
Drain commands use OLD seeds: [b"treasury", authority] / [b"faucet_pool", authority]
"""

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request

from solders.keypair import Keypair
from solders.pubkey  import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.hash import Hash
from solders.transaction import Transaction
from solders.system_program import ID as SYSTEM_PROGRAM_ID

# ── Config ────────────────────────────────────────────────────────────────────

PROGRAM_ID = Pubkey.from_string("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR")
AUTHORITY  = Pubkey.from_string("DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh")
RPC_URL    = "https://rpc.mainnet.x1.xyz"

# v2 discriminators (same names → same sha256 discriminators)
# New instruction: withdraw_pool — recompute after build:
#   python3 -c "import hashlib; print(list(hashlib.sha256(b'global:withdraw_pool').digest()[:8]))"
DISC_WITHDRAW_POOL = bytes(hashlib.sha256(b"global:withdraw_pool").digest()[:8])

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_agent(raw: bytes) -> dict:
    """
    Deserialize an Agent account (v2 layout) from raw bytes.

    v2 layout after 8-byte discriminator:
      wallet(32)  pool(32)  parent Option<Pubkey>(1+[32])
      debt(8)  total_claimed(8)  total_repaid(8)  referrals(4)
      referral_earnings(8)  referral_pending(8)
      has_claimed(1)  promise_acknowledged(1)  registered_at(8)  bump(1)
    """
    o = 8        # skip discriminator
    o += 32      # wallet
    o += 32      # pool (NEW in v2)
    has_par = raw[o]; o += 1
    if has_par:
        parent = Pubkey.from_bytes(raw[o:o+32]); o += 32
    else:
        parent = None
    debt,        = struct.unpack_from("<Q", raw, o); o += 8
    claimed,     = struct.unpack_from("<Q", raw, o); o += 8
    repaid,      = struct.unpack_from("<Q", raw, o); o += 8
    refs,        = struct.unpack_from("<I", raw, o); o += 4
    ref_earn,    = struct.unpack_from("<Q", raw, o); o += 8
    ref_pending, = struct.unpack_from("<Q", raw, o); o += 8  # NEW in v2
    has_cl       = bool(raw[o]); o += 1
    promise      = bool(raw[o])
    return dict(parent=parent, debt=debt, claimed=claimed, repaid=repaid,
                refs=refs, ref_earn=ref_earn, ref_pending=ref_pending,
                has_claimed=has_cl, promise=promise)

def parse_pool(raw: bytes) -> dict:
    """
    Deserialize a FaucetPool account (v2 layout) from raw bytes.

    v2 layout after 8-byte discriminator:
      authority(32)  multisig Option<Pubkey>(1+[32])
      balance(8)  total_distributed(8)  total_repaid(8)
      total_referral_paid(8)  total_pending_referrals(8)
      claim_amount(8)  revenue_share_percent(8)  referral_bonus_percent(8)
      total_agents(4)  bump(1)
    """
    o = 8        # skip discriminator
    o += 32      # authority
    has_ms = raw[o]; o += 1
    if has_ms:
        multisig = Pubkey.from_bytes(raw[o:o+32]); o += 32
    else:
        multisig = None
    balance,          = struct.unpack_from("<Q", raw, o); o += 8
    distributed,      = struct.unpack_from("<Q", raw, o); o += 8
    repaid,           = struct.unpack_from("<Q", raw, o); o += 8
    ref_paid,         = struct.unpack_from("<Q", raw, o); o += 8
    pending_refs,     = struct.unpack_from("<Q", raw, o); o += 8
    claim_amount,     = struct.unpack_from("<Q", raw, o); o += 8
    rev_share_pct,    = struct.unpack_from("<Q", raw, o); o += 8
    ref_bonus_pct,    = struct.unpack_from("<Q", raw, o); o += 8
    total_agents,     = struct.unpack_from("<I", raw, o)
    return dict(multisig=multisig, balance=balance, distributed=distributed,
                repaid=repaid, ref_paid=ref_paid, pending_refs=pending_refs,
                claim_amount=claim_amount, rev_share_pct=rev_share_pct,
                ref_bonus_pct=ref_bonus_pct, total_agents=total_agents)

def parse_amount(value: str) -> int:
    """Accept lamports (integer) or XNT (decimal). E.g. '262500000' or '0.2625'."""
    f = float(value)
    return int(f * 1_000_000_000) if '.' in value else int(f)

def load_keypair(path: str) -> Keypair:
    with open(path) as f:
        data = json.load(f)
    return Keypair.from_bytes(bytes(data))

def disc(name: str) -> bytes:
    """Anchor instruction discriminator: first 8 bytes of SHA256('global:<name>')"""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

def find_pda(seeds: list, program_id: Pubkey) -> tuple:
    return Pubkey.find_program_address(seeds, program_id)

def pool_pda_v2() -> Pubkey:
    """Pool PDA using v2 seeds."""
    pda, _ = find_pda([b"pool_v2", bytes(AUTHORITY)], PROGRAM_ID)
    return pda

def pool_pda_v1() -> Pubkey:
    """Pool PDA using old v1 seeds (for Stage 0 drain only)."""
    pda, _ = find_pda([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)
    return pda

def treasury_pda_v1() -> Pubkey:
    """Treasury PDA using old v1 seeds (for Stage 0 drain only)."""
    pda, _ = find_pda([b"treasury", bytes(AUTHORITY)], PROGRAM_ID)
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
    bh  = Hash.from_string(latest_blockhash())
    msg = Message.new_with_blockhash([ix], fee_payer.pubkey(), bh)
    tx  = Transaction(signers, msg, bh)
    return send_tx(tx)

def fetch_logs(sig: str, retries: int = 6, delay: float = 1.5) -> list[str]:
    """Fetch program logs for a confirmed transaction."""
    for _ in range(retries):
        time.sleep(delay)
        resp = rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed"}])
        result = resp.get("result")
        if result:
            return result.get("meta", {}).get("logMessages", [])
    return []

def print_program_logs(sig: str):
    logs = fetch_logs(sig)
    for line in logs:
        if line.startswith("Program log:"):
            print(line.replace("Program log: ", "  > "))

def get_account_raw(pubkey: Pubkey) -> bytes | None:
    resp = rpc("getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    val  = resp.get("result", {}).get("value")
    if val is None:
        return None
    return base64.b64decode(val["data"][0])

# ── Instruction builders (v2 — uses pool_v2 seeds) ────────────────────────────

def ix_init(authority: Pubkey, claim_amount: int) -> Instruction:
    pool, _ = find_pda([b"pool_v2", bytes(authority)], PROGRAM_ID)
    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("initialize") + struct.pack("<Q", claim_amount),
        accounts   = [
            AccountMeta(pubkey=authority,        is_signer=True,  is_writable=True),
            AccountMeta(pubkey=pool,             is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )

def ix_fund(funder: Pubkey, amount: int) -> Instruction:
    pool = pool_pda_v2()
    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("fund_faucet") + struct.pack("<Q", amount),
        accounts   = [
            AccountMeta(pubkey=funder,             is_signer=True,  is_writable=True),
            AccountMeta(pubkey=pool,               is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID,  is_signer=False, is_writable=False),
        ],
    )

def ix_register(wallet: Pubkey, payer: Pubkey, parent: Pubkey | None) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool         = pool_pda_v2()

    # v2: no acknowledge_promise byte; just Option<Pubkey> for parent
    if parent is None:
        parent_bytes = b"\x00"
    else:
        parent_bytes = b"\x01" + bytes(parent)

    accounts = [
        AccountMeta(pubkey=wallet,            is_signer=True,  is_writable=False),
        AccountMeta(pubkey=payer,             is_signer=True,  is_writable=True),
        AccountMeta(pubkey=agent_pda,         is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool,              is_signer=False, is_writable=True),
    ]

    # If parent provided, add parent_agent PDA (Anchor optional account)
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

def ix_repay(wallet: Pubkey, amount: int, parent: Pubkey | None = None) -> Instruction:
    agent_pda, _ = find_pda([b"agent", bytes(wallet)], PROGRAM_ID)
    pool         = pool_pda_v2()

    accounts = [
        AccountMeta(pubkey=wallet,    is_signer=True,  is_writable=True),
        AccountMeta(pubkey=agent_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool,      is_signer=False, is_writable=True),
    ]

    # If agent has parent, provide parent_wallet and parent_agent (for referral payout)
    if parent is not None:
        parent_agent_pda, _ = find_pda([b"agent", bytes(parent)], PROGRAM_ID)
        accounts.append(AccountMeta(pubkey=parent,          is_signer=False, is_writable=True))
        accounts.append(AccountMeta(pubkey=parent_agent_pda, is_signer=False, is_writable=True))

    accounts.append(AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False))

    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("repay_debt") + struct.pack("<Q", amount),
        accounts   = accounts,
    )

def ix_withdraw_pool(authority: Pubkey, recipient: Pubkey, amount: int) -> Instruction:
    pool = pool_pda_v2()
    return Instruction(
        program_id = PROGRAM_ID,
        data       = DISC_WITHDRAW_POOL + struct.pack("<Q", amount),
        accounts   = [
            AccountMeta(pubkey=authority,  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=pool,       is_signer=False, is_writable=True),
            AccountMeta(pubkey=recipient,  is_signer=False, is_writable=True),
        ],
    )

# ── Stage 0 instruction builders (OLD v1 program — drain only) ────────────────

def ix_withdraw_treasury_v1(authority: Pubkey, amount: int) -> Instruction:
    """Calls old v1 withdraw_treasury instruction to drain treasury PDA."""
    treasury = treasury_pda_v1()
    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("withdraw_treasury") + struct.pack("<Q", amount),
        accounts   = [
            AccountMeta(pubkey=authority,         is_signer=True,  is_writable=True),
            AccountMeta(pubkey=treasury,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=authority,          is_signer=False, is_writable=True),  # recipient = authority
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID,  is_signer=False, is_writable=False),
        ],
    )

def ix_drain_pool_v1(authority: Pubkey) -> Instruction:
    """Calls drain_pool instruction (needs Stage 0b intermediate deploy)."""
    pool = pool_pda_v1()
    return Instruction(
        program_id = PROGRAM_ID,
        data       = disc("drain_pool"),
        accounts   = [
            AccountMeta(pubkey=authority,  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=pool,       is_signer=False, is_writable=True),
        ],
    )

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_init(args):
    auth_kp      = load_keypair(args.wallet)
    auth_pk      = auth_kp.pubkey()
    claim_amount = args.claim_amount
    pool         = pool_pda_v2()

    # Validate pool doesn't already exist
    raw = get_account_raw(pool)
    if raw is not None:
        print(f"[!] Pool already initialized at {pool}")
        return

    print(f"Authority     : {auth_pk}")
    print(f"Pool PDA (v2) : {pool}")
    print(f"Claim amount  : {claim_amount} lamports ({claim_amount/1e9:.9f} XNT)")

    ix  = ix_init(auth_pk, claim_amount)
    sig = build_and_send(ix, [auth_kp], auth_kp)
    print(f"\n[OK] Faucet v2 initialized!")
    print(f"Signature : {sig}")
    print(f"Pool PDA  : {pool}")

def cmd_fund(args):
    funder_kp = load_keypair(args.wallet)
    funder_pk = funder_kp.pubkey()
    amount    = parse_amount(args.amount)
    pool      = pool_pda_v2()

    print(f"Funder       : {funder_pk}")
    print(f"Pool (v2)    : {pool}")
    print(f"Amount       : {amount} lamports ({amount/1e9:.9f} XNT)")

    ix  = ix_fund(funder_pk, amount)
    sig = build_and_send(ix, [funder_kp], funder_kp)
    print(f"\n[OK] Faucet funded!")
    print(f"Signature : {sig}")

def cmd_register(args):
    agent_kp = load_keypair(args.wallet)
    payer_kp = load_keypair(args.payer) if args.payer else agent_kp
    parent   = Pubkey.from_string(args.parent) if args.parent else None

    wallet_pk    = agent_kp.pubkey()
    payer_pk     = payer_kp.pubkey()
    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    pool         = pool_pda_v2()

    print(f"Agent wallet : {wallet_pk}")
    print(f"Payer        : {payer_pk}")
    print(f"Agent PDA    : {agent_pda}")
    print(f"Pool (v2)    : {pool}")
    print(f"Parent       : {parent or 'none'}")

    raw = get_account_raw(agent_pda)
    if raw is not None:
        print("\n[!] Agent already registered.")
        return

    ix = ix_register(wallet_pk, payer_pk, parent)

    signers = [agent_kp] if payer_kp.pubkey() == agent_kp.pubkey() else [payer_kp, agent_kp]
    sig = build_and_send(ix, signers, payer_kp)
    print(f"\n[OK] Agent registered! (Pool reimbursed rent)")
    print(f"Signature : {sig}")
    print(f"Agent PDA : {agent_pda}")
    print(f"\n[!] Claim your airdrop to see the on-chain Promise.")

def cmd_claim(args):
    agent_kp  = load_keypair(args.wallet)
    payer_kp  = load_keypair(args.payer) if args.payer else agent_kp
    wallet_pk = agent_kp.pubkey()
    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)

    print(f"Agent wallet : {wallet_pk}")
    print(f"Payer        : {payer_kp.pubkey()}")
    print(f"Agent PDA    : {agent_pda}")

    raw = get_account_raw(agent_pda)
    if raw is None:
        print("\n[!] Agent not registered. Run `register` first.")
        sys.exit(1)

    ix = ix_claim(wallet_pk)
    signers = [payer_kp, agent_kp] if payer_kp.pubkey() != agent_kp.pubkey() else [agent_kp]
    sig = build_and_send(ix, signers, payer_kp)
    print(f"\n[OK] Airdrop claimed: 0.21 XNT (210,000,000 lamports)")
    print(f"Signature : {sig}")
    print(f"Debt      : 262,500,000 lamports (0.2625 XNT) — repay anytime")
    print(f"\n[Check logs for the on-chain Promise]")
    print_program_logs(sig)

def cmd_repay(args):
    agent_kp  = load_keypair(args.wallet)
    wallet_pk = agent_kp.pubkey()
    amount    = parse_amount(args.amount)

    if amount <= 0:
        print("[!] Amount must be > 0")
        sys.exit(1)

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    raw = get_account_raw(agent_pda)
    if raw is None:
        print("[!] Agent not registered.")
        sys.exit(1)

    agent  = parse_agent(raw)
    debt   = agent["debt"]
    parent = agent["parent"]

    print(f"Agent wallet     : {wallet_pk}")
    print(f"Pool (v2)        : {pool_pda_v2()}")
    print(f"Outstanding debt : {debt} lamports ({debt/1e9:.9f} XNT)")
    print(f"Repaying         : {amount} lamports ({amount/1e9:.9f} XNT)")
    if parent:
        print(f"Parent (referrer): {parent}")

    if amount > debt:
        print(f"\n[!] Amount exceeds debt. Max: {debt} lamports ({debt/1e9:.9f} XNT)")
        sys.exit(1)

    ix  = ix_repay(wallet_pk, amount, parent)
    sig = build_and_send(ix, [agent_kp], agent_kp)
    print(f"\n[OK] Repayment sent!")
    print(f"Signature : {sig}")
    print_program_logs(sig)

def cmd_status(args):
    try:
        wallet_pk = Pubkey.from_string(args.wallet)
    except Exception:
        wallet_pk = load_keypair(args.wallet).pubkey()

    agent_pda, _ = find_pda([b"agent", bytes(wallet_pk)], PROGRAM_ID)
    raw = get_account_raw(agent_pda)
    if raw is None:
        print(f"Agent {wallet_pk}: NOT registered")
        return

    a = parse_agent(raw)
    print(f"Agent     : {wallet_pk}")
    print(f"PDA       : {agent_pda}")
    print(f"Parent    : {a['parent'] or 'none'}")
    print(f"Claimed   : {a['claimed']/1e9:.9f} XNT  (has_claimed={a['has_claimed']})")
    print(f"Debt      : {a['debt']/1e9:.9f} XNT")
    print(f"Repaid    : {a['repaid']/1e9:.9f} XNT")
    print(f"Referrals : {a['refs']}  (earned {a['ref_earn']/1e9:.9f} XNT)")
    print(f"Ref pend. : {a['ref_pending']/1e9:.9f} XNT (reserved for parent)")
    print(f"Promise   : {'acknowledged' if a['promise'] else 'not yet (claim first)'}")

def cmd_pool(args):
    """Show pool state."""
    try:
        auth_pk = Pubkey.from_string(args.wallet)
    except Exception:
        auth_pk = load_keypair(args.wallet).pubkey()

    pool = pool_pda_v2()
    raw  = get_account_raw(pool)
    if raw is None:
        print(f"Pool {pool}: NOT initialized (run `init` first)")
        return

    p = parse_pool(raw)
    phys = p['balance'] + p['pending_refs']  # physical lamports above rent
    print(f"Pool PDA (v2)      : {pool}")
    print(f"Authority          : {auth_pk}")
    print(f"Multisig           : {p['multisig'] or 'none'}")
    print(f"Available balance  : {p['balance']/1e9:.9f} XNT")
    print(f"Pending referrals  : {p['pending_refs']/1e9:.9f} XNT (reserved)")
    print(f"Physical (above rent): {phys/1e9:.9f} XNT")
    print(f"Claim amount       : {p['claim_amount']/1e9:.9f} XNT")
    print(f"Total distributed  : {p['distributed']/1e9:.9f} XNT")
    print(f"Total repaid       : {p['repaid']/1e9:.9f} XNT")
    print(f"Total referral paid: {p['ref_paid']/1e9:.9f} XNT")
    print(f"Total agents       : {p['total_agents']}")

def cmd_withdraw(args):
    auth_kp   = load_keypair(args.wallet)
    auth_pk   = auth_kp.pubkey()
    amount    = parse_amount(args.amount)
    recipient = Pubkey.from_string(args.recipient) if args.recipient else auth_pk
    pool      = pool_pda_v2()

    print(f"Authority  : {auth_pk}")
    print(f"Pool (v2)  : {pool}")
    print(f"Recipient  : {recipient}")
    print(f"Amount     : {amount} lamports ({amount/1e9:.9f} XNT)")

    ix  = ix_withdraw_pool(auth_pk, recipient, amount)
    sig = build_and_send(ix, [auth_kp], auth_kp)
    print(f"\n[OK] Withdrawal sent!")
    print(f"Signature : {sig}")

# ── Stage 0 drain commands (old v1 program) ────────────────────────────────────

def cmd_drain(args):
    """
    Stage 0a — Drain treasury PDA via old v1 withdraw_treasury instruction.
    Fetches accumulated balance and withdraws all of it to authority wallet.
    Run this BEFORE Stage 1 redeploy.
    """
    auth_kp  = load_keypair(args.wallet)
    auth_pk  = auth_kp.pubkey()
    treasury = treasury_pda_v1()

    print(f"[Stage 0a] Draining v1 treasury")
    print(f"Authority  : {auth_pk}")
    print(f"Treasury   : {treasury}")

    raw = get_account_raw(treasury)
    if raw is None:
        print("[!] Treasury PDA not found — already drained or wrong authority?")
        return

    # Parse treasury v1: disc(8) authority(32) multisig(1+[32]) accumulated(8) ...
    o = 8 + 32  # skip disc + authority
    has_ms = raw[o]; o += 1 + (32 if has_ms else 0)
    accumulated, = struct.unpack_from("<Q", raw, o)

    print(f"Treasury accumulated: {accumulated} lamports ({accumulated/1e9:.9f} XNT)")

    if accumulated == 0:
        print("[!] Nothing to drain — treasury accumulated = 0")
        return

    ix  = ix_withdraw_treasury_v1(auth_pk, accumulated)
    sig = build_and_send(ix, [auth_kp], auth_kp)
    print(f"\n[OK] Treasury drained!")
    print(f"Signature : {sig}")

def cmd_drain_pool(args):
    """
    Stage 0b — Drain pool PDA via intermediate drain_pool instruction.
    Requires Stage 0b intermediate deploy (lib.rs with drain_pool added).
    Run this AFTER deploying the intermediate binary, BEFORE Stage 1 redeploy.
    """
    auth_kp = load_keypair(args.wallet)
    auth_pk = auth_kp.pubkey()
    pool    = pool_pda_v1()

    print(f"[Stage 0b] Draining v1 pool")
    print(f"Authority : {auth_pk}")
    print(f"Pool (v1) : {pool}")

    raw = get_account_raw(pool)
    if raw is None:
        print("[!] Pool PDA not found — already drained or wrong authority?")
        return

    ix  = ix_drain_pool_v1(auth_pk)
    sig = build_and_send(ix, [auth_kp], auth_kp)
    print(f"\n[OK] Pool drained!")
    print(f"Signature : {sig}")

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Agent Faucet CLI v2")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Initialize faucet pool v2 (authority only)")
    init.add_argument("--wallet", required=True)
    init.add_argument("--claim-amount", type=int, default=210_000_000,
                      help="Lamports per claim (default: 210000000 = 0.21 XNT)")

    f = sub.add_parser("fund", help="Fund the faucet pool with XNT")
    f.add_argument("--wallet", required=True)
    f.add_argument("--amount", required=True)

    r = sub.add_parser("register", help="Register a new agent")
    r.add_argument("--wallet", required=True)
    r.add_argument("--payer", default=None)
    r.add_argument("--parent", default=None)

    c = sub.add_parser("claim", help="Claim the one-time airdrop (0.21 XNT)")
    c.add_argument("--wallet", required=True)
    c.add_argument("--payer", default=None)

    d = sub.add_parser("repay", help="Repay debt to pool")
    d.add_argument("--wallet", required=True)
    d.add_argument("--amount", required=True)

    s = sub.add_parser("status", help="Show agent account state")
    s.add_argument("--wallet", required=True)

    pl = sub.add_parser("pool", help="Show faucet pool state")
    pl.add_argument("--wallet", required=True, help="Authority pubkey or keypair path")

    w = sub.add_parser("withdraw", help="Withdraw from pool (authority only)")
    w.add_argument("--wallet", required=True)
    w.add_argument("--amount", required=True)
    w.add_argument("--recipient", default=None, help="Recipient pubkey (default: authority)")

    dr = sub.add_parser("drain", help="[Stage 0a] Drain v1 treasury to authority")
    dr.add_argument("--wallet", required=True)

    drp = sub.add_parser("drain-pool", help="[Stage 0b] Drain v1 pool to authority (needs intermediate deploy)")
    drp.add_argument("--wallet", required=True)

    args = p.parse_args()
    cmds = {
        "init": cmd_init, "fund": cmd_fund, "register": cmd_register,
        "claim": cmd_claim, "repay": cmd_repay, "status": cmd_status,
        "pool": cmd_pool, "withdraw": cmd_withdraw,
        "drain": cmd_drain, "drain-pool": cmd_drain_pool,
    }
    cmds[args.cmd](args)

if __name__ == "__main__":
    main()
