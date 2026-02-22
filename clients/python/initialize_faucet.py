#!/usr/bin/env python3
"""
Initialize the agent_faucet program on X1 mainnet.
Creates the faucet_pool and treasury PDAs on-chain.
Run once after deployment.
"""
import hashlib
import struct
import json
import sys
import base58

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.rpc.config import RpcSendTransactionConfig
from solders.commitment_config import CommitmentLevel
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

PROGRAM_ID   = Pubkey.from_string("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR")
RPC_URL      = "https://rpc.mainnet.x1.xyz"
WALLET_PATH  = "/home/owlx1/.config/solana/id.json"
CLAIM_AMOUNT = 210_000_000  # 0.21 XNT in lamports

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_keypair(path: str) -> Keypair:
    with open(path) as f:
        data = json.load(f)
    return Keypair.from_bytes(bytes(data))

def find_pda(seeds: list, program_id: Pubkey) -> tuple:
    # Use solders' built-in which correctly checks off-curve validity
    return Pubkey.find_program_address(seeds, program_id)

def anchor_discriminator(name: str) -> bytes:
    """Anchor instruction discriminator: first 8 bytes of SHA256('global:<name>')"""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

def rpc_call(method: str, params: list) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    authority = load_keypair(WALLET_PATH)
    auth_pubkey = authority.pubkey()
    print(f"Authority : {auth_pubkey}")

    # Derive PDAs
    pool_pda, pool_bump = find_pda([b"faucet_pool", bytes(auth_pubkey)], PROGRAM_ID)
    treasury_pda, _     = find_pda([b"treasury",    bytes(auth_pubkey)], PROGRAM_ID)
    print(f"Faucet Pool PDA : {pool_pda}")
    print(f"Treasury PDA    : {treasury_pda}")
    print(f"Claim amount    : {CLAIM_AMOUNT} lamports (0.21 XNT)")

    # Check if already initialized
    resp = rpc_call("getAccountInfo", [str(pool_pda), {"encoding": "base64"}])
    if resp.get("result", {}).get("value") is not None:
        print("\n[!] Faucet pool already initialized. Nothing to do.")
        sys.exit(0)

    # Get recent blockhash
    bh_resp = rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = bh_resp["result"]["value"]["blockhash"]
    print(f"\nBlockhash : {blockhash}")

    # Build initialize instruction
    # Accounts: authority, faucet_pool, treasury, system_program
    disc = anchor_discriminator("initialize")
    args = struct.pack("<Q", CLAIM_AMOUNT)  # u64 little-endian
    ix_data = disc + args

    ix = Instruction(
        program_id=PROGRAM_ID,
        data=bytes(ix_data),
        accounts=[
            AccountMeta(pubkey=auth_pubkey,    is_signer=True,  is_writable=True),
            AccountMeta(pubkey=pool_pda,       is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_pda,   is_signer=False, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )

    # Build and sign transaction
    from solders.message import Message
    from solders.hash import Hash
    msg = Message.new_with_blockhash(
        [ix],
        auth_pubkey,
        Hash.from_string(blockhash),
    )
    tx = Transaction([authority], msg, Hash.from_string(blockhash))

    # Send
    print("\nSending initialize transaction...")
    tx_bytes = bytes(tx)
    import base64
    tx_b64 = base64.b64encode(tx_bytes).decode()

    send_resp = rpc_call("sendTransaction", [
        tx_b64,
        {"encoding": "base64", "preflightCommitment": "confirmed"},
    ])

    if "error" in send_resp:
        print(f"\n[ERROR] {send_resp['error']}")
        sys.exit(1)

    sig = send_resp["result"]
    print(f"\n[OK] Initialize transaction sent!")
    print(f"Signature       : {sig}")
    print(f"Faucet Pool PDA : {pool_pda}  ← send XNT here via fund_faucet")
    print(f"Treasury PDA    : {treasury_pda}")

if __name__ == "__main__":
    main()
