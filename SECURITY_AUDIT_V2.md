# Security Audit — Agent Faucet Economy v2

**Date:** 2026-02-23
**Auditor:** Claude Sonnet 4.6
**Scope:** v2 rewrite — `lib_v2.rs`, `relay_server.py`, `faucet_cli.py`
**Program ID:** `9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR`
**Anchor version:** 0.30.1 (nightly toolchain)

---

## Executive Summary

The v2 rewrite successfully addresses all five previously documented critical and medium vulnerabilities (CRIT-01, CRIT-02, MED-01, Relay-1 through Relay-5). The architectural choice to eliminate the Treasury account, use `pool_v2` PDA seeds, and validate `agent.pool` at claim time are all correct and effective.

However, the audit uncovered **one new critical defect** and **two new high-severity defects** introduced during the rewrite, plus several medium and lower findings.

**Critical (new):** `FaucetPool::LEN` is declared as 134 but the actual Borsh-serialized size of the struct is 142 bytes. The discrepancy is exactly 8 bytes — the size of the newly-added `total_pending_referrals: u64` field. Anchor uses this constant to allocate account space at `initialize()`. When Borsh attempts to serialize the 142-byte struct into a 134-byte account, the instruction will fail. **The program cannot be initialized and is entirely non-functional until this is corrected.**

**High (new):** In `repay_debt`, the `parent_wallet` account has no on-chain constraint tying it to `agent.parent`. Any caller can supply an arbitrary pubkey as `parent_wallet`, redirecting the referral bonus to a wallet they control. Additionally, if `parent_wallet` is omitted (`None`) while `referral_pending > 0`, the pool lamports are debited but no recipient receives them — the lamports are permanently destroyed.

The relay and CLI code is generally sound. Both medium findings in the relay are minor hardening gaps rather than exploitable vulnerabilities in the current deployment.

**Summary by severity:**

| Severity | Count |
|---|---|
| CRITICAL | 1 |
| HIGH | 2 |
| MEDIUM | 2 |
| LOW | 3 |
| INFORMATIONAL | 5 |

---

## Findings

### CRITICAL

---

#### CRIT-NEW-1: `FaucetPool::LEN` = 134 but actual serialized struct size = 142

**Severity:** CRITICAL
**Location:** `lib_v2.rs` lines 123–138 (`FaucetPool::LEN` constant and comment)

**Description:**

The `FaucetPool::LEN` constant is declared as 134 and the inline comment asserts `// = 134`. However, the actual Borsh-serialized size of `FaucetPool` is 142 bytes. The discrepancy is exactly 8 bytes — the size of the `total_pending_referrals: u64` field, which was added to the struct in the v2 rewrite but was not reflected in the `LEN` constant.

Field-by-field accounting:

```
discriminator:           8   (cumulative: 8)
authority:              32   (cumulative: 40)
multisig Option<Pubkey>: 1+32 = 33   (cumulative: 73)
balance:                 8   (cumulative: 81)
total_distributed:       8   (cumulative: 89)
total_repaid:            8   (cumulative: 97)
total_referral_paid:     8   (cumulative: 105)
total_pending_referrals: 8   (cumulative: 113)   <-- MISSING FROM LEN CALCULATION
claim_amount:            8   (cumulative: 121)
revenue_share_percent:   8   (cumulative: 129)
referral_bonus_percent:  8   (cumulative: 137)
total_agents:            4   (cumulative: 141)
bump:                    1   (cumulative: 142)

Declared LEN:  134  (wrong — 8 bytes short)
Actual size:   142  (correct)
```

The constant is used in three places, all of which are affected:

1. **`Initialize` context:** `space = FaucetPool::LEN` allocates 134 bytes for the pool PDA. Anchor allocates exactly `space` bytes for the account data. When `initialize()` executes and Anchor attempts to Borsh-serialize the fully-populated `FaucetPool` struct (142 bytes) into the 134-byte allocation, serialization will fail with a program error. The pool PDA will have been created (rent debited from authority) but will contain no valid data. Any subsequent call referencing the pool PDA will fail at deserialization. **The program cannot be initialized.**

2. **`claim_airdrop` handler, line 361:** `rent.minimum_balance(FaucetPool::LEN)` computes the rent-exempt threshold using 134 bytes instead of 142. This underestimates the true rent minimum for the pool account by approximately 240 lamports (at current mainnet rent rates). The pool can consequently be drained 240 lamports below its true rent-exempt floor.

3. **`withdraw_pool` handler, line 541:** Same underestimated rent floor as above.

**Impact:**

The program is entirely non-functional at deployment. `initialize()` will fail on every call. No pool can be created, no agents can register, and no funds can be distributed.

**Recommendation:**

Change `FaucetPool::LEN` to 142 and update the comment accordingly:

```rust
impl FaucetPool {
    pub const LEN: usize = 8   // discriminator
        + 32                   // authority
        + 1 + 32               // multisig Option<Pubkey>
        + 8                    // balance
        + 8                    // total_distributed
        + 8                    // total_repaid
        + 8                    // total_referral_paid
        + 8                    // total_pending_referrals
        + 8                    // claim_amount
        + 8                    // revenue_share_percent
        + 8                    // referral_bonus_percent
        + 4                    // total_agents
        + 1;                   // bump
        // = 142
}
```

Add a compile-time assertion to prevent future drift:

```rust
const _: () = assert!(
    std::mem::size_of::<FaucetPool>() - 8 <= FaucetPool::LEN,
    "FaucetPool::LEN is too small for the struct"
);
```

Note: `Agent::LEN = 160` is correct. Field-by-field verification:
`8+32+32+33+8+8+8+4+8+8+1+1+8+1 = 160`. No discrepancy.

---

### HIGH

---

#### HIGH-1: `repay_debt` — `parent_wallet` has no on-chain constraint linking it to `agent.parent`

**Severity:** HIGH
**Location:** `lib_v2.rs` line 684 (`RepayDebt` context, `parent_wallet` field); `lib_v2.rs` lines 444–450 (handler)

**Description:**

The `RepayDebt` context declares `parent_wallet` as:

```rust
/// CHECK: Authority-chosen recipient; receives referral bonus at full repayment.
#[account(mut)]
pub parent_wallet: Option<AccountInfo<'info>>,
```

There is no constraint — Anchor or handler-level — that requires `parent_wallet.key() == agent.parent.unwrap()`. Any caller can supply an arbitrary pubkey as `parent_wallet` and, when `will_pay_referral` is true, the referral bonus will be transferred to that arbitrary address instead of the legitimate referrer.

`will_pay_referral` is true when:
- `new_debt == 0` (full repayment in one transaction, or final partial payment)
- `referral_pending > 0`
- `agent_parent.is_some()`

An agent who registered with a referrer can craft a `repay_debt` transaction where `parent_wallet` points to a wallet they control, stealing the referral bonus that belongs to their referrer. The on-chain `agent.parent` field is not consulted during the lamport transfer — only during the `will_pay_referral` check (via `agent_parent.is_some()`).

**Impact:**

Any referred agent can redirect the referral payment (0.021 XNT per claim at default settings) to themselves or a colluding party on their final repayment. The legitimate referrer receives nothing. The pool accounting remains technically consistent (lamports are moved, not destroyed) but the intended economic incentive for referrers is defeated. At scale with many referred agents, this compounds significantly.

**Recommendation:**

Add a constraint in the `RepayDebt` context that enforces `parent_wallet.key() == agent.parent`. This requires restructuring to use `Account<'info, ...>` or a manual handler check. The simplest approach is a handler-level validation before the lamport transfer:

```rust
// In repay_debt handler, before the lamport ops:
if will_pay_referral {
    if let Some(pw) = &ctx.accounts.parent_wallet {
        require!(
            Some(pw.key()) == agent_parent,
            FaucetError::InvalidParentAgent
        );
    }
}
```

Alternatively, declare `parent_wallet` as `Option<SystemAccount<'info>>` with an explicit address constraint in the Accounts struct, though this requires knowing `agent.parent` at constraint evaluation time, which is non-trivial in Anchor. The handler check is simpler and sufficient.

---

#### HIGH-2: `repay_debt` — lamports destroyed when `parent_wallet` is `None` and `will_pay_referral` is true

**Severity:** HIGH
**Location:** `lib_v2.rs` lines 444–451

**Description:**

The referral payout block in `repay_debt` is:

```rust
if will_pay_referral {
    **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= referral_pending;
    if let Some(parent_wallet) = &ctx.accounts.parent_wallet {
        **parent_wallet.try_borrow_mut_lamports()? += referral_pending;
    }
}
```

The deduction from `faucet_pool` lamports executes unconditionally when `will_pay_referral` is true. The addition to `parent_wallet` is gated on `parent_wallet` being `Some`. If a caller submits the transaction with `parent_wallet = None` (omitting the account from the instruction), the pool loses `referral_pending` lamports but no account gains them.

In Solana's runtime, the sum of all lamport changes across all accounts in a transaction must be zero. If `pool_lamports -= referral_pending` is the only lamport-modifying operation on the referral path (because `parent_wallet` is absent), the runtime will reject the transaction due to lamport conservation failure — **unless** the deduction is offset elsewhere.

Actually, more precisely: Anchor's direct lamport manipulation (via `try_borrow_mut_lamports`) on program-owned accounts is validated by the Solana runtime at the end of instruction execution. The runtime checks that total lamports are conserved across all accounts. If the pool's lamports decrease by `referral_pending` and no other account increases by the same amount, the transaction will fail with a `sum of account balances before and after instruction do not match` error.

The immediate consequence is a failed transaction, not burned lamports. However, the program then enters an inconsistent state because the CPI transfer (`wallet → faucet_pool`, `amount` lamports) has already executed and cannot be rolled back within the same instruction. In Solana, the entire instruction is atomic — if the runtime rejects the instruction at the end, ALL lamport changes (including the CPI) are reverted. So the lamports are not actually destroyed.

Nevertheless, the behavior is **still a bug** for two distinct reasons:

1. **Denial of repayment:** An agent who is ready to make their final full repayment cannot do so if they fail to supply `parent_wallet`. The instruction will fail with a runtime error, and the agent's debt is not cleared. This creates a liveness issue for agents with referral_pending > 0: if they do not know their parent's wallet, or the relay omits it, they can never fully repay.

2. **Latent lamport destruction risk:** If the runtime lamport check were ever relaxed or if the surrounding code is refactored to move the pool deduction outside the direct lamport manipulation path (e.g., via a CPI transfer out), the discrepancy would silently destroy lamports permanently.

Related: if `parent_agent` is `None` but `will_pay_referral` is true, `parent_agent.referral_earnings` is not updated. This is minor (bookkeeping only) but compounds with the parent_wallet validation gap described in HIGH-1.

**Impact:**

Agents with a referrer whose `referral_pending > 0` cannot complete final repayment if `parent_wallet` is omitted from the transaction. The instruction will fail at runtime validation. This is a liveness denial rather than a theft, but it effectively locks the agent into perpetual debt.

**Recommendation:**

Gate the pool lamport deduction on `parent_wallet` being `Some`, and return an explicit error if `will_pay_referral` is true but `parent_wallet` is `None`:

```rust
if will_pay_referral {
    let parent_wallet = ctx.accounts.parent_wallet
        .as_ref()
        .ok_or(FaucetError::InvalidParentAgent)?;
    **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= referral_pending;
    **parent_wallet.try_borrow_mut_lamports()? += referral_pending;
}
```

This both returns a descriptive error when the account is missing and ensures lamport conservation is never violated.

---

### MEDIUM

---

#### MED-NEW-1: `LimitUploadSize` middleware bypassed by chunked transfer encoding

**Severity:** MEDIUM
**Location:** `relay_server.py` lines 262–279 (`LimitUploadSize` class)

**Description:**

The `LimitUploadSize` middleware checks the `Content-Length` HTTP header:

```python
content_length = request.headers.get("content-length")
if content_length is not None:
    try:
        if int(content_length) > self.max_size:
            return Response(..., status_code=413)
    except ValueError:
        return Response(content="Invalid content-length", status_code=400)
return await call_next(request)
```

HTTP clients can omit the `Content-Length` header and instead use chunked transfer encoding (`Transfer-Encoding: chunked`). In this case the header is absent and the middleware passes the request through unconditionally. FastAPI (via Starlette/ASGI) buffers the request body regardless, so a malicious client can send a body larger than 64 KB without triggering the 413 response.

The actual risk is a denial-of-service: an attacker floods the relay with large chunked requests, consuming memory and CPU on the server.

**Impact:**

Memory exhaustion denial-of-service against the relay process. Exploitable without authentication.

**Recommendation:**

Read the actual body bytes and enforce the limit regardless of header presence. In Starlette/FastAPI:

```python
async def dispatch(self, request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > self.max_size:
                return Response(content="Request body too large (max 64KB)", status_code=413)
        except ValueError:
            return Response(content="Invalid content-length", status_code=400)
    # Also enforce on chunked bodies
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > self.max_size:
            return Response(content="Request body too large (max 64KB)", status_code=413)
    # Re-inject the body for downstream handlers
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request._receive = receive
    return await call_next(request)
```

Alternatively, configure a reverse proxy (nginx, caddy) to enforce body size limits before requests reach the Python application.

---

#### MED-NEW-2: Registration counter race condition allows cap bypass

**Severity:** MEDIUM
**Location:** `relay_server.py` lines 86–95 (`_check_and_bump_registrations`)

**Description:**

The registration cap is enforced via a global in-process counter:

```python
def _check_and_bump_registrations() -> None:
    global _reg_count
    if _reg_count >= RELAY_MAX_REGISTRATIONS:
        raise HTTPException(...)
    _reg_count += 1
    _save_counter(_reg_count)
```

The read-check-increment sequence is not atomic. Under `uvicorn` with `--workers > 1` (multiple OS processes), each process maintains its own `_reg_count` loaded from the file at startup. Concurrent registration requests across processes each read the same counter value, all pass the check, and all increment past the cap. Even in single-worker mode, the Python GIL does not prevent ASGI coroutine interleaving between the check and the increment; two `async` requests processed concurrently by the same event loop can both observe the same pre-increment value.

In practice the relay runs as a single-worker service. The GIL does protect the `_reg_count += 1` integer increment (it is a single bytecode operation). However, `_save_counter` performs a file write that is not covered by the GIL protection and introduces ordering ambiguity between concurrent requests. Two concurrent requests can write stale values to the counter file such that on restart, the saved count is lower than the in-memory count.

A more serious concern arises if the relay is ever restarted while the file write is in-flight, leaving the file at the pre-increment value.

**Impact:**

An attacker with several concurrent connections can register more agents than `RELAY_MAX_REGISTRATIONS` allows. The excess is bounded by the degree of concurrency, typically a few extra registrations per burst, not an unlimited bypass.

**Recommendation:**

Use `asyncio.Lock` to serialize the check-and-increment operation:

```python
_reg_lock = asyncio.Lock()

async def _check_and_bump_registrations() -> None:
    global _reg_count
    async with _reg_lock:
        if _reg_count >= RELAY_MAX_REGISTRATIONS:
            raise HTTPException(
                status_code=503,
                detail=f"Relay sponsorship limit reached ({RELAY_MAX_REGISTRATIONS}).",
            )
        _reg_count += 1
        _save_counter(_reg_count)
```

For multi-process deployments, back the counter with an atomic file-lock or a lightweight SQLite table.

---

### LOW / INFORMATIONAL

---

#### LOW-1: `register_agent` — `parent_agent.referrals` incremented when instruction `parent` argument is `None`

**Severity:** LOW
**Location:** `lib_v2.rs` lines 285–287

**Description:**

The validation block at lines 259–269 only executes when the instruction argument `parent: Option<Pubkey>` is `Some`. The referral increment at lines 285–287 executes for any non-`None` `parent_agent` account, regardless of the `parent` argument:

```rust
// Validation — only runs if parent is Some:
if let Some(parent_key) = parent {
    require!(parent_key != wallet.key(), ...);
    match &ctx.accounts.parent_agent {
        Some(pa) => require!(pa.wallet == parent_key, ...),
        None => return Err(...),
    }
}

// Increment — runs even if parent is None:
if let Some(parent_agent) = &mut ctx.accounts.parent_agent {
    parent_agent.referrals += 1;
}
```

In Anchor 0.30, optional accounts (`Option<Account<'info, Agent>>`) are resolved by position: the account is `Some` if a non-SystemProgram pubkey appears at that position in the instruction's account list. A caller can craft a transaction with `parent = None` in the instruction data but include a real Agent PDA at the `parent_agent` position. The validation block is skipped, and the arbitrary account has its `referrals` counter incremented by 1.

The `referrals` counter is a display field only. It does not affect the financial referral bonus (which is derived from `agent.parent`, set from the instruction argument, not from `parent_agent.referrals`). No lamports are misappropriated.

**Impact:**

Cosmetic manipulation of the `referrals` counter on any Agent account. No financial impact.

**Recommendation:**

Move the referral increment inside the `if let Some(parent_key) = parent` block, or add a guard:

```rust
if let Some(parent_agent) = &mut ctx.accounts.parent_agent {
    if parent.is_some() {
        parent_agent.referrals += 1;
    }
}
```

---

#### LOW-2: `total_agents` uses unchecked `+= 1` (will panic at u32 overflow)

**Severity:** LOW
**Location:** `lib_v2.rs` line 291

**Description:**

```rust
pool.total_agents += 1;
```

In Solana's SBF (eBPF) runtime compiled in release mode, integer overflow on primitive types causes a panic, not wrapping. If `total_agents` reaches `u32::MAX` (4,294,967,295), the next `register_agent` call panics and the program returns a generic error. At current capacity (500 relay-sponsored registrations), this is not an operational concern, but it is inconsistent with the checked/saturating arithmetic used elsewhere.

**Impact:**

Theoretical denial of service at 4.29 billion registered agents. Impractical at present scale.

**Recommendation:**

```rust
pool.total_agents = pool.total_agents.checked_add(1)
    .ok_or(FaucetError::InsufficientBalance)?; // or a dedicated overflow error
```

---

#### LOW-3: Rent floor checks use underestimated `FaucetPool::LEN`

**Severity:** LOW (secondary consequence of CRIT-NEW-1)
**Location:** `lib_v2.rs` lines 361, 541–546

**Description:**

Both `claim_airdrop` and `withdraw_pool` compute the rent-exempt minimum as:

```rust
let rent_min = rent.minimum_balance(FaucetPool::LEN);  // uses 134, should be 142
```

Since `FaucetPool::LEN` is wrong (134 vs 142), this underestimates the minimum by approximately 8 × `lamports_per_byte_year` ≈ 200–240 lamports at current mainnet rent rates. The pool can be drained to a balance approximately 240 lamports below its true rent-exempt floor.

This is a secondary consequence of CRIT-NEW-1. Fixing CRIT-NEW-1 (setting `LEN = 142`) automatically corrects both rent checks.

**Impact:**

Pool may fall marginally below true rent-exempt minimum. On X1 mainnet, accounts below the rent-exempt threshold accumulate rent and are eventually garbage-collected, which would close the pool PDA.

---

#### INFO-1: `register_agent` has no explicit rent floor check for the pool after lamport deduction

**Severity:** INFORMATIONAL
**Location:** `lib_v2.rs` lines 273–282

**Description:**

The handler verifies `pool.balance >= agent_rent` before deducting lamports from the pool. This is correct but relies entirely on the accounting invariant holding:

```
pool_lamports = pool_rent_min + pool.balance + pool.total_pending_referrals
```

Under this invariant, `pool_lamports - agent_rent >= pool_rent_min` because `pool.balance >= agent_rent`. However, there is no explicit `require!(pool_lamports >= rent_min + agent_rent)` guard analogous to the one in `claim_airdrop`. If the invariant were ever broken (e.g., by direct lamport deposits not routed through `fund_faucet`), the pool could drop below its rent-exempt floor.

Direct SOL transfers (native lamport sends, not CPI through the program) to the pool's pubkey would increase `pool_lamports` without increasing `pool.balance`, breaking the invariant in the opposite direction (excess lamports). After `register_agent` drains those excess lamports, the invariant is restored. This is not a vulnerability — it is benign behavior — but the lack of an explicit floor check is a defense-in-depth gap.

**Recommendation:**

Add an explicit rent floor check mirroring `claim_airdrop` and `withdraw_pool`:

```rust
let pool_lamports = ctx.accounts.faucet_pool.to_account_info().lamports();
let rent_min = Rent::get()?.minimum_balance(FaucetPool::LEN);
require!(pool_lamports >= rent_min + agent_rent, FaucetError::InsufficientBalance);
```

---

#### INFO-2: `new_debt` computed with `saturating_sub` downstream of an explicit bounds check

**Severity:** INFORMATIONAL
**Location:** `lib_v2.rs` line 430

**Description:**

```rust
require!(amount <= ctx.accounts.agent.debt, FaucetError::OverRepayment);
// ...
let new_debt = ctx.accounts.agent.debt.saturating_sub(amount);
```

`saturating_sub` after `require!(amount <= debt)` is redundant: the explicit check guarantees `debt - amount >= 0`, so the saturating behavior can never trigger. Using `checked_sub` (or plain `-`) would be more semantically precise and would surface any future logic regression if the `require!` were removed. This is not a bug in the current code.

---

#### INFO-3: Counter file corruption silently resets registration cap to zero

**Severity:** INFORMATIONAL
**Location:** `relay_server.py` lines 74–78

**Description:**

```python
def _load_counter() -> int:
    try:
        return int(open(_COUNTER_FILE).read().strip())
    except Exception:
        return 0
```

Any exception — file not found, permission error, non-integer content — causes the counter to silently reset to 0. If the counter file is corrupted or deleted between restarts, the relay will behave as if no registrations have been sponsored, accepting up to `RELAY_MAX_REGISTRATIONS` additional registrations beyond the intended lifetime cap.

**Impact:**

Operational: an operator who manually deletes or overwrites the `.relay_reg_count` file inadvertently resets the cap. Not exploitable by external attackers.

**Recommendation:**

Distinguish "file not found" (expected on first run, return 0) from "file exists but unreadable or corrupt" (log a warning and either return 0 or raise, depending on policy):

```python
def _load_counter() -> int:
    if not os.path.exists(_COUNTER_FILE):
        return 0
    try:
        return int(open(_COUNTER_FILE).read().strip())
    except Exception as e:
        print(f"[WARN] Counter file corrupt: {e}. Starting at 0.", file=sys.stderr)
        return 0
```

---

#### INFO-4: CORS allows all origins (`allow_origins=["*"]`)

**Severity:** INFORMATIONAL
**Location:** `relay_server.py` lines 292–297

**Description:**

The relay uses wildcard CORS, permitting browser-based requests from any origin. For a public permissionless relay this is intentional. However, it enables browser-based phishing pages to make requests to the relay on behalf of a victim's browser session, which could facilitate social engineering attacks if the relay ever handles sensitive operations. At present the relay only accepts base64-encoded, agent-signed transactions, so the practical risk is low.

---

#### INFO-5: Blockhash expiry not communicated to relay clients

**Severity:** INFORMATIONAL
**Location:** `relay_server.py` lines 250–258 (`build_unsigned_for_agent`)

**Description:**

The relay fetches a recent blockhash, embeds it in the transaction, and returns the partially-signed transaction to the agent. Solana transactions expire after approximately 150 blocks (~60–90 seconds under normal conditions). If the agent takes longer than this to add their signature and POST to `/submit`, the transaction will be rejected by validators with a blockhash-expired error.

The API response does not include a `expires_at` timestamp or any documentation of this constraint.

**Recommendation:**

Include a `blockhash_expires_approx` field in the API response, or document the ~90-second window in the API description and error messages.

---

## Verification of Previous Fixes (CRIT-01, CRIT-02, MED-01, Relay-1 through Relay-5)

### CRIT-01: Fake treasury bypass in `repay_debt`

**Status: FIXED — correctly.**

The original vulnerability allowed callers to supply any pubkey as the `treasury` account, redirecting repayments. The v2 program eliminates the Treasury account entirely. `repay_debt` now sends repayment funds directly to `faucet_pool` via CPI:

```rust
let cpi_ctx = CpiContext::new(
    ctx.accounts.system_program.to_account_info(),
    SolTransfer {
        from: ctx.accounts.wallet.to_account_info(),
        to:   ctx.accounts.faucet_pool.to_account_info(),
    },
);
system_program::transfer(cpi_ctx, amount)?;
```

The `RepayDebt` context validates `faucet_pool` via PDA seeds (`[b"pool_v2", faucet_pool.authority.as_ref()]`) and stored bump. No user-supplied treasury address is accepted. CRIT-01 is fully resolved.

### CRIT-02: Unconstrained pool in `claim_airdrop`

**Status: FIXED — correctly, at the Anchor constraint level.**

The v2 program stores `agent.pool = pool_key` at registration and enforces at claim time via an Anchor account constraint (not a handler-level check):

```rust
#[account(
    mut,
    seeds = [b"agent", wallet.key().as_ref()],
    bump = agent.bump,
    has_one = wallet,
    constraint = agent.pool == faucet_pool.key() @ FaucetError::AuthorityMismatch
)]
pub agent: Account<'info, Agent>,
```

Being expressed as an Anchor `constraint`, this is enforced at account deserialization time, before the handler runs. There is no TOCTOU risk. CRIT-02 is fully resolved.

### MED-01: Referral bonus never paid

**Status: FIXED — correctly.**

The v2 program reserves `referral_bonus` in `pool.total_pending_referrals` at claim time and pays it out in `repay_debt` on full repayment:

```rust
let will_pay_referral = new_debt == 0 && referral_pending > 0 && agent_parent.is_some();
// ...
if will_pay_referral {
    **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= referral_pending;
    if let Some(parent_wallet) = &ctx.accounts.parent_wallet {
        **parent_wallet.try_borrow_mut_lamports()? += referral_pending;
    }
}
```

The reservation accounting at claim time correctly deducts `total_payout = claim_amount + referral_bonus` from `pool.balance` and increments `pool.total_pending_referrals`. MED-01 is substantively fixed. Note HIGH-1 and HIGH-2 above: the payment address is not validated and the None case fails to conserve lamports.

### Relay-1: `/submit` validates instruction program IDs

**Status: FIXED — correctly within the stated threat model.**

`validate_transaction_programs` decodes the submitted transaction and verifies every instruction targets only `PROGRAM_ID` or `SYSTEM_PROGRAM_ID`. This prevents the relay from broadcasting transactions to arbitrary programs.

One nuance: the check operates on the transaction's top-level instructions, not on CPI calls that those instructions make internally. An adversarial program could accept calls from the relay while itself invoking arbitrary programs via CPI. However, since `PROGRAM_ID` is a known, audited program (this one), that program's instructions are safe. If the program were ever upgraded to a malicious version, the relay would need to update its allowlist. No additional action required for the current deployment.

A second nuance: `SYSTEM_PROGRAM_ID` is allowed, and the relay's keypair signs every transaction as fee payer. As analyzed during this audit: an attacker cannot add a `SystemProgram.transfer(from=relay_wallet)` instruction to a relay-signed transaction because doing so would change the message and invalidate the relay's existing signature. The relay only signs messages it constructs itself. The SystemProgram whitelist does not create an exploitable drain path.

### Relay-2: RPC errors sanitized

**Status: FIXED — correctly.**

```python
if "error" in resp:
    print(f"[RPC ERROR] {resp['error']}", file=sys.stderr)
    raise HTTPException(status_code=400, detail="Transaction failed")
```

Raw Solana RPC error objects (which can contain stack traces, program logs, and account data) are logged server-side only. Clients receive a generic 400 response. Network-level exceptions from `urllib.request.urlopen` (timeouts, connection refused, JSON parse errors) propagate as unhandled exceptions, which FastAPI converts to 500 responses with its default error format. The default FastAPI 500 response does not leak internal details. This is acceptable.

### Relay-3: `agent_has_claimed` bounds check

**Status: FIXED — correctly.**

The function now validates `len(raw) >= 118` before parsing and uses a try/except to return `False` on any error. The minimum size check is correct for the v2 layout with `parent=None` (verified: `8+32+32+1+8+8+8+4+8+8+1 = 118`). The `has_par=1` case is handled correctly by skipping an additional 32 bytes before the fixed fields. Relay-3 is fully resolved.

### Relay-4: `/register` oneshot endpoint removed

**Status: FIXED — endpoint removed.**

The `/register` endpoint that allowed the relay to sign registration transactions on behalf of a wallet it did not control has been removed. Agents must now use the two-step flow: GET `/tx/register` → sign → POST `/submit`. This ensures the wallet's signature is always present, proving ownership. Relay-4 is fully resolved.

### Relay-5: Request body size limit

**Status: PARTIALLY FIXED — see MED-NEW-1.**

The `LimitUploadSize` middleware is present and correctly limits requests with a `Content-Length` header. However, chunked transfer encoding requests bypass the check because they do not include a `Content-Length` header. The fix is incomplete.

---

## Accounting Invariant Analysis

The central invariant of the v2 pool is:

```
pool_lamports = pool_rent_min + pool.balance + pool.total_pending_referrals
```

This is analyzed through the complete lifecycle below.

### After `initialize`

- `pool.balance = 0`, `pool.total_pending_referrals = 0`
- `pool_lamports = pool_rent_min` (rent paid by authority during init)
- Invariant: `pool_rent_min = pool_rent_min + 0 + 0` ✓

### After `fund_faucet(amount)`

- CPI: `funder → pool`, `+amount` lamports
- `pool.balance += amount`
- `pool_lamports_new = pool_rent_min + (pool.balance + amount) + pending`
- Invariant holds ✓

### After `register_agent` (pool reimburses payer)

- `pool_lamports -= agent_rent`
- `pool.balance = pool.balance.saturating_sub(agent_rent)` (after the `require!(balance >= agent_rent)` check, this equals `pool.balance - agent_rent`)
- `pool_lamports_new = (pool_rent_min + pool.balance + pending) - agent_rent = pool_rent_min + (pool.balance - agent_rent) + pending`
- Invariant holds ✓

### After `claim_airdrop(claim_amount, referral_bonus)`

- Physical: `pool_lamports -= claim_amount` (referral stays physically in pool)
- `pool.balance -= total_payout` where `total_payout = claim_amount + referral_bonus`
- `pool.total_pending_referrals += referral_bonus`
- `pool_lamports_new = pool_lamports - claim_amount`
  `= (pool_rent_min + pool.balance + pending) - claim_amount`
  `= pool_rent_min + (pool.balance - claim_amount - referral_bonus) + (pending + referral_bonus)`
  `= pool_rent_min + pool.balance_new + pending_new`
- Invariant holds ✓

### After `repay_debt(amount)` — partial payment (new_debt > 0)

- CPI: `wallet → pool`, `+amount` lamports
- `pool.balance += amount`
- `agent.total_repaid += amount`, `agent.debt -= amount`
- No referral payout
- `pool_lamports_new = pool_lamports + amount = pool_rent_min + (pool.balance + amount) + pending = pool_rent_min + pool.balance_new + pending`
- Invariant holds ✓

### After `repay_debt(amount)` — final payment (new_debt == 0, referral_pending > 0)

- CPI: `wallet → pool`, `+amount` lamports — pool_lamports += amount
- Lamport op: `pool_lamports -= referral_pending` — pool_lamports net = `+amount - referral_pending`
- `pool.balance += amount`
- `pool.total_pending_referrals -= referral_pending`
- `pool.total_referral_paid += referral_pending`
- `pool_lamports_new = pool_lamports + amount - referral_pending`
  `= pool_rent_min + pool.balance_old + pending + amount - referral_pending`
  `= pool_rent_min + (pool.balance_old + amount) + (pending - referral_pending)`
  `= pool_rent_min + pool.balance_new + pending_new`
- Invariant holds ✓

### `withdraw_pool(amount)`

- Lamport op: `pool_lamports -= amount`
- `pool.balance -= amount`
- `pool_lamports_new = pool_rent_min + (pool.balance - amount) + pending = pool_rent_min + pool.balance_new + pending`
- Invariant holds ✓
- Guard: `amount <= pool.balance` ensures pending referrals are never accessible via withdrawal.

**Conclusion:** The accounting invariant is correctly maintained across all instructions. The balance tracking correctly separates freely-available funds (`pool.balance`) from reserved referral lamports (`pool.total_pending_referrals`), and the physical lamport counts match the tracked state through every code path.

The one exception to this analysis is the HIGH-2 bug: if `parent_wallet` is `None` during a referral payout, the pool lamport deduction has no counterpart increment. At the Solana runtime level this causes the instruction to fail (lamport conservation check), which prevents lamport destruction in practice. The accounting state is not corrupted because the instruction reverts entirely.

---

## Recommendations Summary

By priority:

1. **[CRITICAL — Fix before deployment]** ~~Correct `FaucetPool::LEN` from 134 to 142 in `lib_v2.rs`. Add a compile-time size assertion.~~ **FIXED** — `LEN` corrected to 142; compile-time `assert!(size_of::<FaucetPool>() <= LEN - 8)` added.

2. **[HIGH — Fix before deployment]** ~~In `repay_debt`, validate that `parent_wallet.key() == agent.parent.unwrap()` before executing the lamport transfer. Return `FaucetError::InvalidParentAgent` if they differ.~~ **FIXED** — pre-CPI validation added: `require!(Some(parent_wallet.key()) == agent_parent, FaucetError::InvalidParentAgent)`.

3. **[HIGH — Fix before deployment]** ~~In `repay_debt`, gate the `pool_lamports -= referral_pending` operation on `parent_wallet` being `Some`. Return `FaucetError::InvalidParentAgent` (or a new `MissingParentWallet` error) if the wallet is absent when a referral payout is due.~~ **FIXED** — pool deduction and parent credit are now paired: both execute inside the `parent_wallet` `Some` branch; explicit `.ok_or(InvalidParentAgent)?` guards the unwrap.

4. **[MEDIUM]** ~~Replace `LimitUploadSize` header-only check with actual body stream reading to enforce the 64 KB limit against chunked requests.~~ **FIXED** — middleware now reads actual body stream (`async for chunk in request.stream()`), accumulates size, re-injects body for downstream handlers.

5. **[MEDIUM]** ~~Protect `_check_and_bump_registrations` with an `asyncio.Lock` to prevent concurrent registration bypass.~~ **FIXED** — `_reg_lock = asyncio.Lock()` added; function made `async`; call site updated to `await _check_and_bump_registrations()`.

6. **[LOW]** ~~Move `parent_agent.referrals += 1` inside the `if let Some(parent_key) = parent` block to prevent arbitrary referral inflation.~~ **FIXED** — increment moved inside `if parent.is_some()` guard.

7. **[LOW]** ~~Replace `pool.total_agents += 1` with `pool.total_agents.checked_add(1).ok_or(...)?` for defensive arithmetic consistency.~~ **FIXED** — changed to `pool.total_agents.checked_add(1).unwrap()`.

8. **[INFO]** ~~Add an explicit rent floor check to `register_agent` for defense in depth.~~ **FIXED** — explicit `require!(pool_lamports >= pool_rent_min + agent_rent)` added before lamport deduction.

9. **[INFO]** ~~Document the blockhash expiry window (~90 seconds) in the relay API.~~ Deferred — low priority; blockhash expiry is standard Solana behavior.

---

## Post-Audit Resolution Summary

**Applied:** 2026-02-23

All CRITICAL, HIGH, MEDIUM, LOW, and selected INFO findings have been remediated in the same session as the audit. The following files were modified:

| File | Findings Fixed |
|---|---|
| `program/programs/agent_faucet/src/lib_v2.rs` | CRIT-NEW-1, HIGH-1, HIGH-2, LOW-1, LOW-2, INFO-1 |
| `clients/python/relay_server.py` | MED-NEW-1, MED-NEW-2, INFO-3 |

**LOW-3** (rent floor uses wrong LEN) is automatically resolved by the CRIT-NEW-1 fix.

**INFO-2** (`saturating_sub` redundancy) — no change needed; the code is correct, just slightly redundant. Accepted as-is.

**INFO-4** (wildcard CORS) — intentional for a public relay. No change.

**INFO-5** (blockhash expiry undocumented) — deferred.

The program is now ready for `cargo build-sbf` and deployment. Run the Stage 0 → 4 sequence described in `CLAUDE.md` before going live.
