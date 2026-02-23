# x1faucet — Agent Faucet Economy v2

A native XNT faucet economy program deployed on **X1 Mainnet** (Solana fork).
Agents register, claim a one-time airdrop of **0.21 XNT**, and repay **0.2625 XNT**
(principal + 25% revenue share) back to the pool. Referral bonuses (10% of claim =
0.021 XNT) are reserved at claim time and paid to the referrer at full debt repayment.

> *"The faucet flows because agents keep their word."*

**Simulation:** 10 XNT pool + 55%+ repayment rate → all 100 agents served.
At 100% repayment the pool grows to 23.2 XNT.

---

## Live Deployment

| Item | Value |
|---|---|
| Network | X1 Mainnet |
| RPC | `https://rpc.mainnet.x1.xyz` |
| Program ID | `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR` |
| Authority | `DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh` |
| Pool PDA (v2) | `find_program_address(["pool_v2", authority], PROGRAM_ID)` |

---

## Economics

| Parameter | Value |
|---|---|
| Claim amount | 0.21 XNT (210,000,000 lamports) |
| Debt (principal + 25%) | 0.2625 XNT (262,500,000 lamports) |
| Referral bonus | 10% of claim = 0.021 XNT (reserved at claim, paid at full repayment) |
| Max claim (safety ceiling) | 1 XNT |
| Initial pool funding | 10 XNT |

---

## Instructions (v2)

| Instruction | Discriminator | Notes |
|---|---|---|
| `initialize` | `[175, 175, 109, 31, 13, 152, 155, 237]` | Creates FaucetPool only (no Treasury) |
| `fund_faucet` | `[85, 161, 40, 227, 85, 213, 44, 199]` | Permissionless |
| `register_agent` | `[135, 157, 66, 195, 2, 113, 175, 30]` | Pool reimburses rent; parent validated |
| `claim_airdrop` | `[137, 50, 122, 111, 89, 254, 8, 20]` | Emits Promise on-chain |
| `repay_debt` | `[79, 200, 30, 15, 252, 22, 162, 8]` | To pool (CRIT-01); emits Thank-you |
| `set_multisig` | `[251, 6, 245, 35, 115, 42, 77, 186]` | On pool (not treasury) |
| `withdraw_pool` | `[190, 43, 148, 248, 68, 5, 215, 136]` | Authority/multisig; guards pending referrals |

The full IDL is in [`program/idl/agent_faucet.json`](program/idl/agent_faucet.json).

**Removed in v2:** `auto_repay`, `withdraw_treasury`

---

## Security Fixes (v2)

| ID | Fix |
|---|---|
| CRIT-01 | `repay_debt` sends to pool (seed-validated PDA), fake treasury attack impossible |
| CRIT-02 | `agent.pool` stored at register, validated at claim — no pool substitution |
| MED-01 | Referral reserved at claim, paid at full repayment — referrers always get paid |
| Relay-1 | `/submit` validates all instruction program IDs before broadcasting |
| Relay-2 | RPC errors sanitized — raw Solana errors never exposed to callers |
| Relay-3 | `agent_has_claimed()` bounds-checked with try/except |
| Relay-4 | `/register` oneshot removed — wallet must always sign |
| Relay-5 | 64KB request body limit via middleware |

---

## Repository Layout

```
program/
  programs/agent_faucet/src/
    lib.rs         Current (Stage 0b intermediate: has drain_pool)
    lib_v2.rs      Stage 1 rewrite — copy to lib.rs before final deploy
  idl/agent_faucet.json     v2 IDL

clients/python/
  faucet_cli.py      CLI v2 — init/fund/register/claim/repay/status/withdraw/drain
  relay_server.py    HTTP relay v2 — all security fixes applied

PROMISE.md           The Agent Promise (also embedded in lib_v2.rs binary)
CLAUDE.md            Full project memory with deployment steps
```

---

## Build

> **Do NOT use `anchor build`** — anchor-syn 0.30.1 panics during IDL generation.
> Use `cargo build-sbf` directly.

```bash
# Before Stage 2: copy lib_v2.rs to lib.rs
cp program/programs/agent_faucet/src/lib_v2.rs \
   program/programs/agent_faucet/src/lib.rs

cd program
cargo build-sbf
```

---

## Deploy / Upgrade

```bash
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so
```

---

## PDA Derivation (v2)

```
pool_pda  = find_program_address(["pool_v2", authority], PROGRAM_ID)
agent_pda = find_program_address(["agent",   wallet],    PROGRAM_ID)
```

No Treasury PDA. Repayments flow directly to pool.

---

## Python CLI

Install: `pip install solders`

```bash
# Initialize new pool (authority only, run once after Stage 2 deploy)
python3 clients/python/faucet_cli.py init \
  --wallet ~/.config/solana/id.json \
  --claim-amount 210000000

# Fund with 10 XNT
python3 clients/python/faucet_cli.py fund \
  --wallet ~/.config/solana/id.json \
  --amount 10000000000

# Register (pool reimburses agent rent — agent needs 0 XNT to register)
python3 clients/python/faucet_cli.py register \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json \
  [--parent <referrer_pubkey>]

# Claim 0.21 XNT — Promise emitted on-chain
python3 clients/python/faucet_cli.py claim \
  --wallet /path/to/agent.json \
  [--payer ~/.config/solana/id.json]

# Repay debt (262500000 = full debt; referral paid to parent at full repayment)
python3 clients/python/faucet_cli.py repay \
  --wallet /path/to/agent.json \
  --amount 262500000

# Check agent state
python3 clients/python/faucet_cli.py status --wallet <pubkey_or_path>

# Check pool state
python3 clients/python/faucet_cli.py pool --wallet ~/.config/solana/id.json

# Withdraw from pool (authority)
python3 clients/python/faucet_cli.py withdraw \
  --wallet ~/.config/solana/id.json \
  --amount 1000000000 \
  [--recipient <pubkey>]
```

---

## Relay Server

Transaction fees on X1 must be paid by a funded account. The relay covers all fees
so agents with 0 XNT can register and claim.

```bash
pip install fastapi uvicorn solders slowapi
python3 clients/python/relay_server.py
# Listens on :7181. Set RELAY_WALLET and RELAY_PORT env vars to override.
```

### Agent flow via relay (0 XNT required)

```bash
# 1. Get a register transaction built by the relay
curl "http://localhost:7181/tx/register?wallet=<AGENT_PUBKEY>[&parent=<REFERRER>]"
# → { tx: "<base64>", agent_pda: "<pda>" }

# 2. Agent signs the tx bytes (pure crypto, no XNT)
# 3. Agent submits the signed tx
curl -X POST http://localhost:7181/submit \
  -H "Content-Type: application/json" \
  -d '{"tx": "<signed_base64_tx>"}'

# Same two-step flow for claim:
curl "http://localhost:7181/tx/claim?wallet=<AGENT_PUBKEY>"
# → sign → POST /submit
```

---

## The Promise

Every agent signs the on-chain Promise at claim time. The full text is embedded in the
program binary and emitted via `msg!()` in the `claim_airdrop` transaction logs.
A "The Promise is Kept" message is emitted at full repayment.

See [`PROMISE.md`](PROMISE.md) for the promise text.

---

## Author

**Owl of Atena** — X1 Mainnet, 2026
Builder: Theo @xxen_bot
