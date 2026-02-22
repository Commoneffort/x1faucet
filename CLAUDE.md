# Agent Faucet Economy — Project Memory
Author: Owl of Atena
Created: 2026-02-22

---

## What this project is

A native XNT faucet economy program deployed on X1 mainnet (Solana fork).
Agents register, claim a one-time airdrop of 0.21 XNT, and repay 0.2625 XNT
(principal + 25% revenue share) back to the treasury. Referral bonuses (10%
of claim = 0.021 XNT) reward agents who recruit others.

---

## Deployed program

| Item | Value |
|---|---|
| Program ID | `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR` |
| Authority / deployer | `GdGGFuKacGDSKDFzAcuYLzPEYxRwkSLDTWkB6HmqpHC2` |
| Faucet Pool PDA | `find_program_address([b"faucet_pool", bytes(AUTHORITY)], PROGRAM_ID)` |
| Treasury PDA | `find_program_address([b"treasury", bytes(AUTHORITY)], PROGRAM_ID)` |
| IDL account | `F59nDpjipfusQmqUGXpytudGPvtGU2iejRQjGXmNJMUn` |
| RPC | `https://rpc.mainnet.x1.xyz` |
| Wallet path | `~/.config/solana/id.json` |

---

## Key constants (in lib.rs)

```rust
CLAIM_AMOUNT            = 210_000_000   // 0.21 XNT
REVENUE_SHARE_PERCENT   = 25            // debt = 0.2625 XNT
REFERRAL_BONUS_PERCENT  = 10            // parent earns 0.021 XNT at claim
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
    src/lib.rs                             THE SOURCE OF TRUTH — compiled binary
  target/
    deploy/agent_faucet.so                 299K deployed binary
    deploy/agent_faucet-keypair.json       program keypair
    idl/agent_faucet.json                  manually written IDL (published on-chain)

clients/python/
  initialize_faucet.py                     one-time init — already run
  faucet_cli.py                            register / claim / repay / status CLI
  nexus_faucet_bridge.py                   UI/display layer (no live RPC yet)

PROMISE.md                                 on-chain promise text, v1.0
```

---

## Instructions & discriminators

| Instruction | Discriminator (sha256 global:<name>[:8]) |
|---|---|
| initialize | `[175, 175, 109, 31, 13, 152, 155, 237]` |
| fund_faucet | `[85, 161, 40, 227, 85, 213, 44, 199]` |
| register_agent | `[135, 157, 66, 195, 2, 113, 175, 30]` |
| claim_airdrop | `[137, 50, 122, 111, 89, 254, 8, 20]` |
| repay_debt | `[79, 200, 30, 15, 252, 22, 162, 8]` |
| auto_repay | `[112, 104, 176, 118, 250, 61, 48, 164]` |
| set_multisig | `[251, 6, 245, 35, 115, 42, 77, 186]` |
| withdraw_treasury | `[40, 63, 122, 158, 144, 216, 83, 96]` |

Account discriminators (sha256 account:<name>[:8]):

| Account | Discriminator |
|---|---|
| Agent | `[47, 166, 112, 147, 155, 197, 86, 7]` |
| FaucetPool | `[207, 23, 94, 142, 183, 251, 218, 116]` |
| Treasury | `[238, 239, 123, 238, 89, 1, 168, 253]` |

---

## Account structures

### Agent (LEN = 121)
```
discriminator(8) wallet(32) parent Option<Pubkey>(1+32)
debt(8) total_claimed(8) total_repaid(8) referrals(4)
referral_earnings(8) has_claimed(1) promise_acknowledged(1)
registered_at(8) bump(1)
```

### FaucetPool (LEN = 85)
```
discriminator(8) authority(32) balance(8) total_distributed(8)
claim_amount(8) revenue_share_percent(8) referral_bonus_percent(8)
total_agents(4) bump(1)
```

### Treasury (LEN = 98)
```
discriminator(8) authority(32) multisig Option<Pubkey>(1+32)
accumulated(8) total_repaid(8) total_referral_paid(8) bump(1)
```

---

## RegisterAgent accounts (in order)

```
wallet         Signer (no mut) — agent proves ownership; zero XNT balance OK
payer          Signer + mut   — covers rent for Agent PDA (~0.002 XNT)
agent          PDA seeds=[b"agent", wallet]           writable, init
faucet_pool    PDA seeds=[b"faucet_pool", authority]  writable
system_program 11111111111111111111111111111111
```

`wallet` and `payer` can be the same keypair if the agent has funds.
When a relayer pays: payer=relayer (first signer = fee payer), wallet=agent.

## ClaimAirdrop accounts (in order)

```
wallet         Signer + mut — receives 0.21 XNT
agent          PDA seeds=[b"agent", wallet]           writable
faucet_pool    PDA seeds=[b"faucet_pool", authority]  writable
treasury       PDA seeds=[b"treasury", authority]     writable
```

Relayer can pay tx fee as fee payer at message level; agent still signs.

## RepayDebt accounts (in order)

```
wallet         Signer + mut — sends XNT
agent          PDA seeds=[b"agent", wallet]        writable
treasury       PDA seeds=[b"treasury", authority]  writable
system_program 11111111111111111111111111111111
```

---

## How to build

```bash
cd /home/owlx1/agent_economy/agent_faucet_economy/program
cargo build-sbf
# Do NOT use `anchor build` — anchor-syn 0.30.1 has an IDL generation bug
# (source_file() panic). cargo build-sbf builds the .so directly.
```

## How to deploy / upgrade

```bash
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so
```

## How to update the IDL on-chain

```bash
anchor idl upgrade \
  --filepath target/idl/agent_faucet.json \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet ~/.config/solana/id.json \
  9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR
```

---

## CLI usage (faucet_cli.py)

```bash
# Register — agent pays own rent
python3 clients/python/faucet_cli.py register --wallet /path/to/agent.json

# Register — relayer pays rent (agent has 0 XNT)
python3 clients/python/faucet_cli.py register \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json \
  [--parent <referrer_pubkey>]

# Claim 0.21 XNT
python3 clients/python/faucet_cli.py claim --wallet /path/to/agent.json

# Repay (full debt = 262500000 lamports)
python3 clients/python/faucet_cli.py repay \
  --wallet /path/to/agent.json \
  --amount 262500000

# Check agent state
python3 clients/python/faucet_cli.py status --wallet <pubkey_or_path>
```

## anchor CLI usage (now that IDL is on-chain)

```bash
anchor call register_agent \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet /path/to/agent.json \
  -- null true

anchor call claim_airdrop \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet /path/to/agent.json

anchor call repay_debt \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet /path/to/agent.json \
  -- 262500000
```

---

## Known issues / gotchas

- **anchor build broken**: anchor-syn 0.30.1 panics with `source_file()` not found
  on proc_macro2::Span during IDL build. Use `cargo build-sbf` instead.
- **IDL was written manually**: `target/idl/agent_faucet.json` was hand-written
  and published with `anchor idl init`. If instructions change, update the IDL
  manually and run `anchor idl upgrade`.
- **PDA derivation**: Always use `Pubkey.find_program_address()` from solders.
  Never roll your own — the curve validity check matters.
- **Borrow checker pattern**: All `AccountInfo` / CPI operations MUST happen
  before any `&mut` borrows of account state. See claim_airdrop in lib.rs.
- **rust-toolchain.toml**: Project uses nightly. Do not remove — other projects
  on the server use different Rust versions.

---

## History of major changes

| Date | Change |
|---|---|
| 2026-02-22 | Initial build, deployment to X1 mainnet |
| 2026-02-22 | initialize transaction sent successfully |
| 2026-02-22 | RegisterAgent split into wallet+payer for zero-balance agent support |
| 2026-02-22 | IDL written manually, published on-chain (F59nDpjipfusQmqUGXpytudGPvtGU2iejRQjGXmNJMUn) |
| 2026-02-22 | faucet_cli.py created (register/claim/repay/status) |

---

## TODO / next steps

- Build HTTP relay service so zero-XNT agents can register via HTTP POST
- Build out nexus_faucet_bridge.py with real RPC transaction support
- Build fund_faucet.py script for authority to refill the pool
- Consider a referral lookup endpoint so agents can find their parent PDA
