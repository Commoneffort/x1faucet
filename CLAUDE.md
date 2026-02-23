# Agent Faucet Economy — Project Memory
Author: Owl of Atena
Created: 2026-02-22
Updated: 2026-02-23 (v2 full redeploy)

---

## What this project is

A native XNT faucet economy program deployed on X1 mainnet (Solana fork).
Agents register, claim a one-time airdrop of 0.21 XNT, and repay 0.2625 XNT
(principal + 25% revenue share) back to the pool. Referral bonuses (10% of
claim = 0.021 XNT) are reserved at claim time and paid to the referrer when
the referred agent fully repays their debt.

Economics simulation (10 XNT pool, 55%+ repayment rate): all 100 agents served.
At 100% repayment, pool grows to 23.2 XNT.

---

## Deployed program

| Item | Value |
|---|---|
| Program ID | `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR` |
| Authority / deployer | `DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh` |
| Pool PDA (v2) | `find_program_address([b"pool_v2", bytes(AUTHORITY)], PROGRAM_ID)` |
| IDL account | (re-publish after Stage 3 deploy) |
| RPC | `https://rpc.mainnet.x1.xyz` |
| Wallet path | `~/.config/solana/id.json` |

> **No Treasury PDA in v2.** Repayments flow directly to the pool.
> Old v1 PDAs (faucet_pool, treasury) still exist on-chain but are drained.

---

## Key constants (lib_v2.rs → becomes lib.rs after Stage 1 deploy)

```rust
CLAIM_AMOUNT            = 210_000_000   // 0.21 XNT
REVENUE_SHARE_PERCENT   = 25            // debt = 0.2625 XNT
REFERRAL_BONUS_PERCENT  = 10            // parent earns 0.021 XNT (at full repayment)
BASIS_POINTS            = 100
MAX_CLAIM_AMOUNT        = 1_000_000_000 // safety ceiling 1 XNT
```

---

## File map

```
program/
  Anchor.toml                              cluster=mainnet, anchor_version=0.30.1
  Cargo.toml                               workspace, members=["programs/*"], resolver="2"
  rust-toolchain.toml                      channel="nightly"
  programs/agent_faucet/
    Cargo.toml                             anchor-lang 0.30.1, features=[init-if-needed,idl-build]
    src/lib.rs                             INTERMEDIATE v1+drain_pool (Stage 0b deploy)
    src/lib_v2.rs                          STAGE 1 REWRITE — rename to lib.rs before Stage 2 deploy
  idl/agent_faucet.json                    v2 IDL (hand-written; publish after Stage 2 deploy)
  target/                                  gitignored — build artifacts

clients/python/
  faucet_cli.py                            CLI v2 — fund/register/claim/repay/status/init/withdraw/drain
  relay_server.py                          HTTP relay v2 — security fixes applied
  initialize_faucet.py                     [legacy] one-time init for v1 — do not use
  nexus_faucet_bridge.py                   display/bridge layer (no live RPC yet)

PROMISE.md                                 on-chain promise text (also embedded in lib_v2.rs)
```

---

## Stage 0 — Drain old accounts (DO FIRST)

### Stage 0a: Drain treasury (current v1 program)
```bash
# Drain all treasury accumulated balance to authority wallet
python3 clients/python/faucet_cli.py drain --wallet ~/.config/solana/id.json
```

### Stage 0b: Drain pool (requires intermediate deploy)
```bash
# 1. Build current lib.rs (has drain_pool added) as intermediate binary
cd program && cargo build-sbf

# 2. Deploy intermediate
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so

# 3. Drain the old pool PDA
python3 clients/python/faucet_cli.py drain-pool --wallet ~/.config/solana/id.json
```

---

## Stage 1+2 — Deploy new program

```bash
# Rename Stage 1 rewrite to active source
cp program/programs/agent_faucet/src/lib_v2.rs \
   program/programs/agent_faucet/src/lib.rs

# Build
cd program && cargo build-sbf

# Deploy
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so
```

---

## Stage 3 — Compute withdraw_pool discriminator and publish IDL

```bash
# withdraw_pool discriminator: [190, 43, 148, 248, 68, 5, 215, 136] — already in IDL.
# If instruction names ever change, recompute with:
#   python3 -c "import hashlib; print(list(hashlib.sha256(b'global:<name>').digest()[:8]))"

# Publish IDL on-chain
anchor idl upgrade \
  --filepath program/idl/agent_faucet.json \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet ~/.config/solana/id.json \
  9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR
```

---

## Stage 4 — Initialize & Fund

```bash
# Initialize new pool (pool_v2 seeds)
python3 clients/python/faucet_cli.py init \
  --wallet ~/.config/solana/id.json \
  --claim-amount 210000000

# Fund with 10 XNT
python3 clients/python/faucet_cli.py fund \
  --wallet ~/.config/solana/id.json \
  --amount 10000000000

# Check pool state
python3 clients/python/faucet_cli.py pool --wallet ~/.config/solana/id.json
```

---

## Instructions & discriminators (v2)

| Instruction | Discriminator |
|---|---|
| `initialize` | `[175, 175, 109, 31, 13, 152, 155, 237]` |
| `fund_faucet` | `[85, 161, 40, 227, 85, 213, 44, 199]` |
| `register_agent` | `[135, 157, 66, 195, 2, 113, 175, 30]` |
| `claim_airdrop` | `[137, 50, 122, 111, 89, 254, 8, 20]` |
| `repay_debt` | `[79, 200, 30, 15, 252, 22, 162, 8]` |
| `set_multisig` | `[251, 6, 245, 35, 115, 42, 77, 186]` |
| `withdraw_pool` | `[190, 43, 148, 248, 68, 5, 215, 136]` |

**Removed in v2:** `auto_repay`, `withdraw_treasury`

Account discriminators:

| Account | Discriminator |
|---|---|
| `Agent` | `[47, 166, 112, 147, 155, 197, 86, 7]` |
| `FaucetPool` | `[207, 23, 94, 142, 183, 251, 218, 116]` |

**Removed in v2:** `Treasury`

---

## Account structures (v2)

### FaucetPool (LEN = 142)
```
discriminator(8) authority(32) multisig Option<Pubkey>(1+32)
balance(8) total_distributed(8) total_repaid(8)
total_referral_paid(8) total_pending_referrals(8)
claim_amount(8) revenue_share_percent(8) referral_bonus_percent(8)
total_agents(4) bump(1)
```

### Agent (LEN = 160)
```
discriminator(8) wallet(32) pool(32) parent Option<Pubkey>(1+32)
debt(8) total_claimed(8) total_repaid(8) referrals(4)
referral_earnings(8) referral_pending(8)
has_claimed(1) promise_acknowledged(1) registered_at(8) bump(1)
```

---

## PDA seeds (v2)

| PDA | Seeds |
|---|---|
| Pool | `[b"pool_v2", authority]` |
| Agent | `[b"agent", wallet]` (unchanged) |

**Old v1 seeds (for reference / drain only):**
- Pool v1: `[b"faucet_pool", authority]`
- Treasury v1: `[b"treasury", authority]`

---

## Instruction accounts (v2)

### initialize
```
authority      Signer + mut
faucet_pool    PDA seeds=[b"pool_v2", authority]  init, writable
system_program
```

### register_agent
```
wallet         Signer (no mut) — proves ownership
payer          Signer + mut   — pays Agent PDA rent (reimbursed by pool)
agent          PDA seeds=[b"agent", wallet]        init, writable
faucet_pool    PDA seeds=[b"pool_v2", authority]   writable
parent_agent   PDA seeds=[b"agent", parent] (optional) writable
system_program
```

### claim_airdrop
```
wallet         Signer + mut   — receives 0.21 XNT
agent          PDA seeds=[b"agent", wallet]       writable (has_one=wallet, pool==faucet_pool.key())
faucet_pool    PDA seeds=[b"pool_v2", authority]  writable
```
No treasury. CRIT-02: agent.pool is validated against faucet_pool.key().

### repay_debt
```
wallet         Signer + mut   — sends XNT
agent          PDA seeds=[b"agent", wallet]        writable
faucet_pool    PDA seeds=[b"pool_v2", authority]   writable (CRIT-01: no treasury bypass)
parent_wallet  AccountInfo mut (optional, needed when referral_pending > 0)
parent_agent   PDA seeds=[b"agent", parent] (optional) writable
system_program
```

### withdraw_pool
```
authority      Signer + mut   (must be pool.authority OR pool.multisig)
faucet_pool    PDA seeds=[b"pool_v2", authority]  writable
recipient      AccountInfo mut
```

### set_multisig
```
authority      Signer + mut   (must be pool.authority)
faucet_pool    PDA seeds=[b"pool_v2", authority]  writable
```

---

## Security fixes applied (v2)

| ID | Vulnerability | Fix |
|---|---|---|
| CRIT-01 | Fake treasury bypass in repay_debt | repay_debt sends CPI to pool (seed-validated), no treasury |
| CRIT-02 | Unconstrained pool in claim_airdrop | agent.pool stored at register, checked at claim |
| MED-01 | Referral bonus never paid | Paid at full debt repayment from reserved pool lamports |
| MED-05 | auto_repay used hardcoded constant | auto_repay instruction removed entirely |
| Audit-3 | Parent not validated on-chain at register | parent_agent PDA passed and wallet-key verified |
| Audit-4 | Referral not reserved at claim | pool.balance -= full payout; total_pending_referrals += bonus |
| Audit-5 | withdraw could drain reserved referrals | amount <= pool.balance (which excludes pending referrals) |
| Relay-1 | /submit is open proxy for any program | All ix program_ids validated == PROGRAM_ID or SystemProgram |
| Relay-2 | Raw RPC errors exposed to callers | Catch all RPC errors; return generic "Transaction failed" |
| Relay-3 | agent_has_claimed no bounds check | Wrapped in try/except; validates len before parsing |
| Relay-4 | /register oneshot — relay signs for wallet | Endpoint removed; wallet must always sign |
| Relay-5 | No request body size limit | LimitUploadSize middleware (64KB) |

---

## Zero-XNT agent onboarding

### Via CLI with --payer
```bash
python3 clients/python/faucet_cli.py register \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json

python3 clients/python/faucet_cli.py claim \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json
```

### Via relay server
```bash
# Start relay (listens on :7181 by default)
python3 clients/python/relay_server.py

# 1. Get register tx
curl "http://localhost:7181/tx/register?wallet=<AGENT_PUBKEY>"
# → { tx: "<base64>", agent_pda: "<pda>" }

# 2. Agent signs tx
# 3. Submit
curl -X POST http://localhost:7181/submit \
  -H "Content-Type: application/json" \
  -d '{"tx":"<signed_base64>"}'
```

---

## How to build

```bash
cd program
cargo build-sbf
# Do NOT use `anchor build` — anchor-syn 0.30.1 has an IDL generation panic
```

## How to deploy / upgrade

```bash
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so
```

## Relay service

```bash
sudo systemctl restart x1faucet-relay
sudo journalctl -u x1faucet-relay -f
```

---

## Known issues / gotchas

- **anchor build broken**: Use `cargo build-sbf` — anchor-syn 0.30.1 panics on proc_macro2::Span.
- **withdraw_pool discriminator**: Must compute post-build. See Stage 3 section above.
  Update IDL before publishing on-chain.
- **Optional accounts in Anchor 0.30**: Anchor uses SystemProgram as a sentinel for None.
  Clients pass SystemProgram pubkey when parent_agent/parent_wallet is not needed.
- **lib_v2.rs vs lib.rs**: lib.rs is the intermediate (Stage 0b); lib_v2.rs is Stage 1.
  Copy lib_v2.rs → lib.rs before Stage 2 build.
- **Counter reset**: After Stage 1 deploy, manually delete .relay_reg_count or reset it
  to 0 so the new program starts fresh.
- **Existing agent PDAs**: Old v1 Agent accounts use the old struct layout (no pool field).
  They cannot be used with the v2 program. Agents must re-register.

---

## History of major changes

| Date | Change |
|---|---|
| 2026-02-22 | Initial build, deployment to X1 mainnet (v1) |
| 2026-02-22 | initialize transaction sent successfully |
| 2026-02-22 | RegisterAgent split into wallet+payer for zero-balance agent support |
| 2026-02-22 | IDL written manually, published on-chain |
| 2026-02-22 | faucet_cli.py created (register/claim/repay/status/fund) |
| 2026-02-22 | Published to GitHub: Commoneffort/x1faucet |
| 2026-02-22 | Added relay_server.py for HTTP-based zero-XNT agent onboarding |
| 2026-02-23 | Security audit — 5 critical/medium issues identified |
| 2026-02-23 | v2 full redeploy plan designed and implemented |
| 2026-02-23 | lib.rs: added drain_pool (Stage 0b intermediate) |
| 2026-02-23 | lib_v2.rs: full rewrite — no treasury, pool_v2 seeds, CRIT-01/02 fixes |
| 2026-02-23 | faucet_cli.py v2: new seeds, drain commands, new parse_agent layout |
| 2026-02-23 | relay_server.py v2: Relay-1 through Relay-5 security fixes |
| 2026-02-23 | IDL v2: updated for new program structure |
