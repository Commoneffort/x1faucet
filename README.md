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
| IDL account | `F59nDpjipfusQmqUGXpytudGPvtGU2iejRQjGXmNJMUn` |
| Authority | `DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh` |
| Pool PDA (v2) | `E2qLABAjNaytZyt5WVenEgthq4cpBJFVPvrgVRDAiMxa` |
| Relay | `http://193.34.212.186:7181` |

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

## Live Pool

| Item | Value |
|---|---|
| Pool PDA (v2) | `E2qLABAjNaytZyt5WVenEgthq4cpBJFVPvrgVRDAiMxa` |
| Pool balance | 10 XNT (funded 2026-02-23) |
| Agents registered | 0 / ∞ |
| Relay | `http://<host>:7181` |

---

## Repository Layout

```
program/
  programs/agent_faucet/src/
    lib.rs         DEPLOYED — v2 final program (pool_v2 seeds, all fixes)
    lib_v2.rs      Source of truth (identical to lib.rs post-deploy)
  idl/agent_faucet.json     v2 IDL (published on-chain)

clients/python/
  faucet_cli.py      CLI v2 — init/fund/register/claim/repay/status/withdraw/drain
  relay_server.py    HTTP relay v2 — all security fixes applied

PROMISE.md           The Agent Promise (also embedded in lib.rs binary)
CLAUDE.md            Full project memory with deployment steps
SECURITY_AUDIT_V2.md Full security audit + resolution log
```

---

## Build

> **Do NOT use `anchor build`** — anchor-syn 0.30.1 panics during IDL generation.
> Use `cargo build-sbf` directly.

```bash
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

**Step 0 — Generate a keypair (one time, no XNT needed)**

The relay never creates a wallet for you. You must generate a keypair first.
The keypair is pure cryptography — it requires no network call and no funds.

```bash
# Option A: Solana CLI
solana-keygen new --outfile agent.json
# Prints your public key — this is your <AGENT_PUBKEY>

# Option B: Python (for autonomous agents)
python3 -c "
from solders.keypair import Keypair, import json
kp = Keypair()
print('pubkey :', kp.pubkey())
with open('agent.json', 'w') as f:
    json.dump(list(bytes(kp)), f)
"
```

The resulting `agent.json` is your identity. Keep the private key secure —
whoever holds it can sign transactions on your behalf.

**Step 1 — Register (relay pays the fee)**

```bash
# Get a partially-signed register transaction from the relay
curl "http://193.34.212.186:7181/tx/register?wallet=<AGENT_PUBKEY>[&parent=<REFERRER_PUBKEY>]"
# → { tx: "<base64>", agent_pda: "<pda>" }
```

The relay has already signed as fee payer (slot 0). You must sign as the wallet
owner (slot 1) to prove you control the keypair, then submit:

```python
import base64, json
from solders.keypair import Keypair
from solders.transaction import Transaction

with open("agent.json") as f:
    kp = Keypair.from_bytes(bytes(json.load(f)))

tx = Transaction.from_bytes(base64.b64decode("<tx from relay>"))
sig = kp.sign_message(bytes(tx.message))
signed = Transaction.populate(tx.message, [tx.signatures[0], sig])
signed_b64 = base64.b64encode(bytes(signed)).decode()
```

```bash
curl -X POST http://193.34.212.186:7181/submit \
  -H "Content-Type: application/json" \
  -d "{\"tx\": \"$SIGNED_B64\"}"
# → { signature: "<tx sig>" }
```

**Step 2 — Claim 0.21 XNT (same sign-and-submit flow)**

```bash
curl "http://193.34.212.186:7181/tx/claim?wallet=<AGENT_PUBKEY>"
# → { tx: "<base64>" }
# Sign with agent keypair (same as Step 1) → POST /submit
```

After submitting the claim transaction, 0.21 XNT lands in your wallet.
Your wallet account is activated on-chain at this moment.

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
