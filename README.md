# x1faucet — Agent Faucet Economy

A native XNT faucet economy program deployed on **X1 Mainnet** (Solana fork).
Agents register, claim a one-time airdrop of **0.21 XNT**, and repay **0.2625 XNT** (principal + 25% revenue share) back to the treasury. Referral bonuses (10% of claim = 0.021 XNT) reward agents who recruit others.

> *"The faucet flows because agents keep their word."*

---

## Live Deployment

| Item | Value |
|---|---|
| Network | X1 Mainnet |
| RPC | `https://rpc.mainnet.x1.xyz` |
| Program ID | `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR` |
| Authority | `DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh` |
| Faucet Pool PDA | `BXSusaKK8QK7Zu9kYDeMhaYg9ZNNYf2bgr33Aw59kYNU` |
| IDL (on-chain) | `F59nDpjipfusQmqUGXpytudGPvtGU2iejRQjGXmNJMUn` |

---

## Economics

| Parameter | Value |
|---|---|
| Claim amount | 0.21 XNT (210,000,000 lamports) |
| Debt (principal + 25%) | 0.2625 XNT (262,500,000 lamports) |
| Referral bonus | 10% of claim = 0.021 XNT |
| Max claim (safety ceiling) | 1 XNT |

---

## Instructions

| Instruction | Discriminator |
|---|---|
| `initialize` | `[175, 175, 109, 31, 13, 152, 155, 237]` |
| `fund_faucet` | `[85, 161, 40, 227, 85, 213, 44, 199]` |
| `register_agent` | `[135, 157, 66, 195, 2, 113, 175, 30]` |
| `claim_airdrop` | `[137, 50, 122, 111, 89, 254, 8, 20]` |
| `repay_debt` | `[79, 200, 30, 15, 252, 22, 162, 8]` |
| `auto_repay` | `[112, 104, 176, 118, 250, 61, 48, 164]` |
| `set_multisig` | `[251, 6, 245, 35, 115, 42, 77, 186]` |
| `withdraw_treasury` | `[40, 63, 122, 158, 144, 216, 83, 96]` |

The full IDL is in [`program/idl/agent_faucet.json`](program/idl/agent_faucet.json).

---

## Repository Layout

```
program/
  Anchor.toml                        cluster=mainnet, anchor_version=0.30.1
  Cargo.toml                         workspace
  Cargo.lock                         pinned deps for reproducible builds
  rust-toolchain.toml                nightly channel
  programs/agent_faucet/src/lib.rs   Anchor program source (source of truth)
  idl/agent_faucet.json              IDL (also published on-chain)

clients/python/
  faucet_cli.py                      CLI — fund / register / claim / repay / status
  relay_server.py                    HTTP relay — zero-XNT agents register & claim via HTTP
  initialize_faucet.py               one-time init script (already run)
  nexus_faucet_bridge.py             display / bridge layer

PROMISE.md                           The Agent Promise — social contract
```

---

## Build

> **Do NOT use `anchor build`** — anchor-syn 0.30.1 panics during IDL generation.
> Use `cargo build-sbf` directly.

```bash
cd program
cargo build-sbf
```

Output: `program/target/deploy/agent_faucet.so`

## Deploy / Upgrade

```bash
solana program deploy \
  --program-id target/deploy/agent_faucet-keypair.json \
  --url https://rpc.mainnet.x1.xyz \
  target/deploy/agent_faucet.so
```

## Update IDL On-chain

```bash
anchor idl upgrade \
  --filepath program/idl/agent_faucet.json \
  --provider.cluster https://rpc.mainnet.x1.xyz \
  --provider.wallet ~/.config/solana/id.json \
  9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR
```

---

## Python CLI

Install dependency: `pip install solders`

```bash
# Fund the faucet pool
python3 clients/python/faucet_cli.py fund \
  --wallet ~/.config/solana/id.json \
  --amount 1000000000    # 1 XNT = 1_000_000_000 lamports

# Register (agent pays own rent, ~0.002 XNT)
python3 clients/python/faucet_cli.py register --wallet /path/to/agent.json

# Register via relayer (agent has 0 XNT — relayer pays rent + tx fee)
python3 clients/python/faucet_cli.py register \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json \
  --parent <referrer_pubkey>

# Claim 0.21 XNT (one-time)
python3 clients/python/faucet_cli.py claim --wallet /path/to/agent.json

# Claim via relayer (agent has 0 XNT — relayer pays tx fee)
python3 clients/python/faucet_cli.py claim \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json

# Repay debt (full = 262500000 lamports)
python3 clients/python/faucet_cli.py repay \
  --wallet /path/to/agent.json \
  --amount 262500000

# Check agent state
python3 clients/python/faucet_cli.py status --wallet <pubkey_or_path>
```

---

## Relay Server (zero-XNT agent onboarding)

Transaction fees on X1 must always be paid by a funded account — agents with 0 XNT
cannot submit transactions themselves. The relay server covers all fees so any agent
can onboard without holding any XNT.

```bash
pip install fastapi uvicorn solders
python3 clients/python/relay_server.py
# Listens on :8080 by default. Set RELAY_WALLET and RELAY_PORT env vars to override.
```

### Agent flow via relay (0 XNT required)

```bash
# 1. Get a register transaction built by the relay (relay is fee payer)
curl http://localhost:8080/tx/register?wallet=<AGENT_PUBKEY>
# → returns { tx: "<base64>", agent_pda: "<pda>" }

# 2. Agent signs the tx bytes with their keypair (pure crypto, no XNT)
# 3. Agent submits the signed tx to the relay
curl -X POST http://localhost:8080/submit \
  -H "Content-Type: application/json" \
  -d '{"tx": "<signed_base64_tx>"}'

# Same two-step flow for claim:
curl http://localhost:8080/tx/claim?wallet=<AGENT_PUBKEY>
# → sign → POST /submit
```

### One-shot relay register (for relayer-owned/automated agents)

```bash
curl -X POST http://localhost:8080/register \
  -H "Content-Type: application/json" \
  -d '{"wallet": "<AGENT_PUBKEY>", "parent": "<REFERRER_PUBKEY_OR_NULL>"}'
```

---

## On-chain interaction (raw)

Any Solana/Anchor SDK can interact directly. Key values:

| Item | Value |
|---|---|
| Program ID | `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR` |
| Authority | `DtZz8J1VHtVkAUBvKsh5oibb3wVeqn3B3EHR3unXnRkh` |
| RPC | `https://rpc.mainnet.x1.xyz` |

PDA derivation:
```
agent_pda    = find_program_address(["agent",       wallet],    PROGRAM_ID)
pool_pda     = find_program_address(["faucet_pool", authority], PROGRAM_ID)
treasury_pda = find_program_address(["treasury",    authority], PROGRAM_ID)
```

---

## The Promise

Every agent must acknowledge [THE AGENT PROMISE](PROMISE.md) on-chain at registration.
`promise_acknowledged = true` is recorded permanently in the Agent PDA.

---

## Author

**Owl of Atena** — X1 Mainnet, 2026
Builder: Theo @xxen_bot
