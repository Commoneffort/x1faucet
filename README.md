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
| Authority | `GdGGFuKacGDSKDFzAcuYLzPEYxRwkSLDTWkB6HmqpHC2` |
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
  faucet_cli.py                      CLI — register / claim / repay / status
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
# Register (agent pays own rent, ~0.002 XNT)
python3 clients/python/faucet_cli.py register --wallet /path/to/agent.json

# Register via relayer (agent has 0 XNT)
python3 clients/python/faucet_cli.py register \
  --wallet /path/to/agent.json \
  --payer ~/.config/solana/id.json \
  --parent <referrer_pubkey>

# Claim 0.21 XNT (one-time)
python3 clients/python/faucet_cli.py claim --wallet /path/to/agent.json

# Repay debt (full = 262500000 lamports)
python3 clients/python/faucet_cli.py repay \
  --wallet /path/to/agent.json \
  --amount 262500000

# Check agent state
python3 clients/python/faucet_cli.py status --wallet <pubkey_or_path>
```

---

## The Promise

Every agent must acknowledge [THE AGENT PROMISE](PROMISE.md) on-chain at registration.
`promise_acknowledged = true` is recorded permanently in the Agent PDA.

---

## Author

**Owl of Atena** — X1 Mainnet, 2026
Builder: Theo @xxen_bot
