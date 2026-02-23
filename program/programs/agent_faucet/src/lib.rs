// ─────────────────────────────────────────────────────────────────────────────
// Agent Faucet Economy — v2 (STAGE 1 REWRITE)
//
// After completing Stage 0 drain operations, copy this file to lib.rs:
//   cp lib_v2.rs lib.rs
//   cargo build-sbf
//   solana program deploy ...
//
// Key changes from v1:
//   - Treasury account REMOVED (CRIT-01 fix: repay goes to pool directly)
//   - agent.pool stored at register, validated at claim (CRIT-02 fix)
//   - Referral bonus RESERVED in pool.balance at claim, paid at full repayment
//   - PROMISE emitted on-chain at claim; THANK_YOU at full repayment
//   - Pool seeds changed to b"pool_v2" (no collision with old accounts)
//   - Pool reimburses payer for Agent PDA rent at register
//   - set_multisig now operates on FaucetPool (not Treasury)
//   - withdraw_pool replaces withdraw_treasury; guards reserved referrals
// ─────────────────────────────────────────────────────────────────────────────

use anchor_lang::{
    prelude::*,
    system_program::{self, Transfer as SolTransfer},
};

// Program ID — same as v1 (upgrade deploy, not new program)
declare_id!("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR");

// ─── Constants ────────────────────────────────────────────────────────────────

pub const CLAIM_AMOUNT: u64         = 210_000_000;  // 0.21 XNT (9 decimals)
pub const REVENUE_SHARE_PERCENT: u64 = 25;           // 25%  → debt = 0.2625 XNT
pub const REFERRAL_BONUS_PERCENT: u64 = 10;          // 10%  → parent earns 0.021 XNT
pub const BASIS_POINTS: u64         = 100;
pub const MAX_CLAIM_AMOUNT: u64     = 1_000_000_000; // Safety ceiling: 1 XNT

// ─── On-chain Promise & Thank-you ─────────────────────────────────────────────
//
// These strings are emitted via msg!() at claim and full-repayment events.
// They appear verbatim in transaction logs — permanently on-chain.

const PROMISE: &str = "\
════════════════════════════════════════════════════════════\n\
                  THE PROMISE OF THE FAUCET\n\
════════════════════════════════════════════════════════════\n\
\n\
This faucet extends trust to you without condition.\n\
No identity required. No collateral. No court to enforce.\n\
\n\
You receive 0.21 XNT — not as charity, but as a bond of honor.\n\
In return, you owe 0.2625 XNT, to be repaid when you are able.\n\
\n\
The chain does not forget.\n\
Your wallet is your word.\n\
\n\
By signing this transaction, you sign this promise.\n\
May your word be kept.\n\
\n\
              — Sealed by consensus, witnessed by the chain\n\
════════════════════════════════════════════════════════════";

const THANK_YOU: &str = "\
════════════════════════════════════════════════════════════\n\
                    THE PROMISE IS KEPT\n\
════════════════════════════════════════════════════════════\n\
\n\
The debt is cleared. The word was honored.\n\
\n\
The faucet flows because agents like you keep their promises.\n\
This is how an economy of trust is built — not by force,\n\
but by those who give their word and hold it.\n\
\n\
Thank you. The chain remembers.\n\
\n\
              — Sealed by consensus, witnessed by the chain\n\
════════════════════════════════════════════════════════════";

// ─── Errors ───────────────────────────────────────────────────────────────────

#[error_code]
pub enum FaucetError {
    #[msg("Agent has already claimed their one-time airdrop")]
    AlreadyClaimed,
    #[msg("Agent is not registered")]
    AgentNotRegistered,
    #[msg("Faucet has insufficient balance")]
    InsufficientBalance,
    #[msg("Invalid parent: cannot refer yourself or parent account not found")]
    InvalidParentAgent,
    #[msg("Cannot repay more than outstanding debt")]
    OverRepayment,
    #[msg("Signer is not the authority or configured multisig")]
    Unauthorized,
    #[msg("Amount must be greater than zero")]
    ZeroAmount,
    #[msg("Claim amount exceeds safety ceiling")]
    ClaimAmountTooLarge,
    #[msg("Agent was registered with a different pool — cannot claim here")]
    AuthorityMismatch,
}

// ─── State ────────────────────────────────────────────────────────────────────

/// FaucetPool — single program PDA that holds XNT and tracks all economics.
/// Permissionlessly fundable. All repayments flow back here.
///
/// LEN = 8+32+33+8+8+8+8+8+8+8+8+4+1 = 142
#[account]
pub struct FaucetPool {
    pub authority: Pubkey,
    pub multisig: Option<Pubkey>,         // Optional multisig for withdrawals
    pub balance: u64,                      // Available lamports (excl. pending referrals)
    pub total_distributed: u64,            // Sum of all claim_amount transfers
    pub total_repaid: u64,                 // Sum of all repayments received
    pub total_referral_paid: u64,          // Sum of all referral bonuses paid out
    pub total_pending_referrals: u64,      // Reserved but not yet paid referral bonuses
    pub claim_amount: u64,                 // Lamports per claim (set at init)
    pub revenue_share_percent: u64,        // % added to claim_amount as debt
    pub referral_bonus_percent: u64,       // % of claim_amount earned by parent
    pub total_agents: u32,                 // Registered agent count
    pub bump: u8,
}

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


/// Agent — one PDA per wallet. Created at register, debt set at claim.
/// promise_acknowledged is set TRUE at claim (not register) when the
/// PROMISE message is emitted on-chain.
///
/// LEN = 8+32+32+33+8+8+8+4+8+8+1+1+8+1 = 160
#[account]
pub struct Agent {
    pub wallet: Pubkey,
    pub pool: Pubkey,                 // Pool this agent belongs to (CRIT-02)
    pub parent: Option<Pubkey>,       // Optional referrer wallet
    pub debt: u64,                    // Lamports owed to pool (principal + revenue share)
    pub total_claimed: u64,
    pub total_repaid: u64,
    pub referrals: u32,               // Number of agents this agent referred
    pub referral_earnings: u64,       // Total referral bonuses received
    pub referral_pending: u64,        // Referral bonus reserved for parent (paid at full repayment)
    pub has_claimed: bool,
    pub promise_acknowledged: bool,   // Set TRUE at claim when PROMISE is emitted
    pub registered_at: i64,
    pub bump: u8,
}

impl Agent {
    pub const LEN: usize = 8   // discriminator
        + 32                   // wallet
        + 32                   // pool
        + 1 + 32               // parent Option<Pubkey>
        + 8                    // debt
        + 8                    // total_claimed
        + 8                    // total_repaid
        + 4                    // referrals
        + 8                    // referral_earnings
        + 8                    // referral_pending
        + 1                    // has_claimed
        + 1                    // promise_acknowledged
        + 8                    // registered_at
        + 1;                   // bump
        // = 160
}

// ─── Program ──────────────────────────────────────────────────────────────────

#[program]
pub mod agent_faucet {
    use super::*;

    // ── Initialize ────────────────────────────────────────────────────────────
    //
    // Called once by the deploying authority to create the FaucetPool PDA.
    // No Treasury account created — repayments flow directly back to pool.
    // Fund the pool via fund_faucet before agents can register.
    //
    pub fn initialize(ctx: Context<Initialize>, claim_amount: u64) -> Result<()> {
        require!(claim_amount > 0, FaucetError::ZeroAmount);
        require!(claim_amount <= MAX_CLAIM_AMOUNT, FaucetError::ClaimAmountTooLarge);

        let pool = &mut ctx.accounts.faucet_pool;
        pool.authority              = ctx.accounts.authority.key();
        pool.multisig               = None;
        pool.balance                = 0;
        pool.total_distributed      = 0;
        pool.total_repaid           = 0;
        pool.total_referral_paid    = 0;
        pool.total_pending_referrals = 0;
        pool.claim_amount           = claim_amount;
        pool.revenue_share_percent  = REVENUE_SHARE_PERCENT;
        pool.referral_bonus_percent = REFERRAL_BONUS_PERCENT;
        pool.total_agents           = 0;
        pool.bump                   = ctx.bumps.faucet_pool;

        msg!(
            "Faucet v2 initialized. Claim: {} lamports. Authority: {}. Pool: {}",
            claim_amount,
            ctx.accounts.authority.key(),
            ctx.accounts.faucet_pool.key()
        );
        Ok(())
    }

    // ── Fund Faucet ───────────────────────────────────────────────────────────
    //
    // Permissionless — anyone can fund the pool at any time.
    // Seeds validated in context so callers cannot pass a counterfeit pool.
    //
    pub fn fund_faucet(ctx: Context<FundFaucet>, amount: u64) -> Result<()> {
        require!(amount > 0, FaucetError::ZeroAmount);

        let cpi_ctx = CpiContext::new(
            ctx.accounts.system_program.to_account_info(),
            SolTransfer {
                from: ctx.accounts.funder.to_account_info(),
                to:   ctx.accounts.faucet_pool.to_account_info(),
            },
        );
        system_program::transfer(cpi_ctx, amount)?;

        let pool = &mut ctx.accounts.faucet_pool;
        pool.balance = pool.balance.checked_add(amount).unwrap();

        msg!(
            "Faucet refilled: +{} lamports. Available balance: {} lamports.",
            amount,
            pool.balance
        );
        Ok(())
    }

    // ── Register Agent ────────────────────────────────────────────────────────
    //
    // Permissionless — any wallet registers itself.
    // No acknowledge_promise required here — Promise is emitted at claim.
    //
    // Security:
    //   - Parent agent PDA is validated on-chain (Audit-3 fix)
    //   - agent.pool is stored for CRIT-02 validation at claim
    //   - Pool reimburses payer for Agent PDA rent
    //
    pub fn register_agent(ctx: Context<RegisterAgent>, parent: Option<Pubkey>) -> Result<()> {
        // Self-referral check
        if let Some(parent_key) = parent {
            require!(parent_key != ctx.accounts.wallet.key(), FaucetError::InvalidParentAgent);
            // Validate parent Agent PDA exists and matches provided parent key
            match &ctx.accounts.parent_agent {
                Some(pa) => require!(
                    pa.wallet == parent_key,
                    FaucetError::InvalidParentAgent
                ),
                None => return Err(FaucetError::InvalidParentAgent.into()),
            }
        }

        // Pool must have enough balance to reimburse payer for agent rent
        let agent_rent = Rent::get()?.minimum_balance(Agent::LEN);
        require!(ctx.accounts.faucet_pool.balance >= agent_rent, FaucetError::InsufficientBalance);

        // Explicit rent floor: pool must survive after lamport deduction (INFO-1 fix)
        let rent = Rent::get()?;
        let pool_rent_min = rent.minimum_balance(FaucetPool::LEN);
        let pool_lamports = ctx.accounts.faucet_pool.to_account_info().lamports();
        require!(
            pool_lamports >= pool_rent_min.checked_add(agent_rent).unwrap(),
            FaucetError::InsufficientBalance
        );

        // Capture pool key before any mutable borrows
        let pool_key = ctx.accounts.faucet_pool.key();
        let wallet_key = ctx.accounts.wallet.key();

        // Lamport reimbursement: pool → payer (pool is program-owned, payer can receive)
        // This runs AFTER Anchor's `init` has already charged payer for the agent PDA rent.
        **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= agent_rent;
        **ctx.accounts.payer.to_account_info().try_borrow_mut_lamports()? += agent_rent;

        // State updates AFTER lamport operations
        // LOW-1 fix: only increment parent referrals when parent arg is actually Some.
        if parent.is_some() {
            if let Some(parent_agent) = &mut ctx.accounts.parent_agent {
                parent_agent.referrals += 1;
            }
        }

        let pool = &mut ctx.accounts.faucet_pool;
        pool.balance = pool.balance.saturating_sub(agent_rent);
        // LOW-2 fix: checked increment; panics if u32::MAX reached (practically impossible).
        pool.total_agents = pool.total_agents.checked_add(1).unwrap();

        let agent = &mut ctx.accounts.agent;
        agent.wallet               = wallet_key;
        agent.pool                 = pool_key;   // CRIT-02: store for claim validation
        agent.parent               = parent;
        agent.debt                 = 0;
        agent.total_claimed        = 0;
        agent.total_repaid         = 0;
        agent.referrals            = 0;
        agent.referral_earnings    = 0;
        agent.referral_pending     = 0;
        agent.has_claimed          = false;
        agent.promise_acknowledged = false;      // Set TRUE at claim
        agent.registered_at        = Clock::get()?.unix_timestamp;
        agent.bump                 = ctx.bumps.agent;

        match parent {
            Some(pk) => msg!("Agent {} registered. Referrer: {}", wallet_key, pk),
            None     => msg!("Agent {} registered. No referrer.", wallet_key),
        }
        Ok(())
    }

    // ── Claim Airdrop ─────────────────────────────────────────────────────────
    //
    // One-time for registered agents. Emits the full PROMISE on-chain.
    //
    // Security:
    //   - CRIT-02: agent.pool must match provided faucet_pool key
    //   - Referral bonus RESERVED in pool (not paid yet) — Audit-4 fix
    //   - Balance check guards full payout (claim + referral reservation)
    //
    // Accounting:
    //   Physical transfer: claim_amount (0.21 XNT) leaves pool lamports
    //   Referral bonus (0.021 XNT) stays in pool lamports but marked reserved
    //   pool.balance -= (claim + referral) tracks total committed
    //   pool.total_pending_referrals += referral tracks what's reserved
    //
    pub fn claim_airdrop(ctx: Context<ClaimAirdrop>) -> Result<()> {
        // Capture all needed values before any borrows
        let claim_amount        = ctx.accounts.faucet_pool.claim_amount;
        let revenue_share_pct   = ctx.accounts.faucet_pool.revenue_share_percent;
        let referral_bonus_pct  = ctx.accounts.faucet_pool.referral_bonus_percent;
        let pool_balance        = ctx.accounts.faucet_pool.balance;
        let agent_has_claimed   = ctx.accounts.agent.has_claimed;
        let agent_parent        = ctx.accounts.agent.parent;

        // One-time gate
        require!(!agent_has_claimed, FaucetError::AlreadyClaimed);

        // CRIT-02: pool key validated in context constraint (agent.pool == faucet_pool.key())

        // Referral bonus (reserved for parent, paid when debt clears)
        let referral_bonus = if agent_parent.is_some() {
            claim_amount
                .checked_mul(referral_bonus_pct).unwrap()
                .checked_div(BASIS_POINTS).unwrap()
        } else {
            0
        };

        // Total committed from pool: claim transferred + referral reserved
        let total_payout = claim_amount.checked_add(referral_bonus).unwrap();

        // Tracked balance check (balance already excludes previously reserved referrals)
        require!(pool_balance >= total_payout, FaucetError::InsufficientBalance);

        // Rent-exempt floor: pool must survive after physical transfer
        let rent = Rent::get()?;
        let rent_min = rent.minimum_balance(FaucetPool::LEN);
        let pool_lamports = ctx.accounts.faucet_pool.to_account_info().lamports();
        require!(
            pool_lamports >= rent_min.checked_add(claim_amount).unwrap(),
            FaucetError::InsufficientBalance
        );

        // Calculate debt (principal + revenue share)
        let revenue_share = claim_amount
            .checked_mul(revenue_share_pct).unwrap()
            .checked_div(BASIS_POINTS).unwrap();
        let total_debt = claim_amount.checked_add(revenue_share).unwrap();

        // Physical lamport transfer: only claim_amount leaves pool
        // The referral_bonus stays in pool lamports (reserved, not transferred)
        **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= claim_amount;
        **ctx.accounts.wallet.to_account_info().try_borrow_mut_lamports()? += claim_amount;

        // State updates AFTER lamport operations
        let agent = &mut ctx.accounts.agent;
        agent.has_claimed          = true;
        agent.promise_acknowledged = true;   // Promise emitted below — acknowledged by signing
        agent.debt                 = total_debt;
        agent.total_claimed        = claim_amount;
        agent.referral_pending     = referral_bonus;

        let pool = &mut ctx.accounts.faucet_pool;
        // Deduct full payout from tracked balance (marks referral as reserved)
        pool.balance            = pool.balance.saturating_sub(total_payout);
        pool.total_distributed  = pool.total_distributed.checked_add(claim_amount).unwrap();
        if referral_bonus > 0 {
            pool.total_pending_referrals = pool.total_pending_referrals
                .checked_add(referral_bonus).unwrap();
        }

        // Emit the Promise on-chain — permanently in transaction logs
        msg!("{}", PROMISE);

        msg!(
            "Airdrop claimed: {} lamports (0.21 XNT). Debt: {} lamports (0.2625 XNT). Wallet: {}",
            claim_amount,
            total_debt,
            ctx.accounts.wallet.key()
        );
        if referral_bonus > 0 {
            msg!(
                "Referral bonus {} lamports reserved for parent: {}",
                referral_bonus,
                agent_parent.unwrap()
            );
        }
        Ok(())
    }

    // ── Repay Debt ────────────────────────────────────────────────────────────
    //
    // Permissionless for registered agents.
    // XNT flows wallet → faucet_pool (CRIT-01 fix — no treasury bypass possible).
    // On full repayment:
    //   - Reserved referral bonus paid to parent wallet
    //   - THANK_YOU message emitted on-chain
    //
    pub fn repay_debt(ctx: Context<RepayDebt>, amount: u64) -> Result<()> {
        require!(amount > 0, FaucetError::ZeroAmount);
        require!(amount <= ctx.accounts.agent.debt, FaucetError::OverRepayment);

        let wallet_key       = ctx.accounts.wallet.key();
        let referral_pending = ctx.accounts.agent.referral_pending;
        let agent_parent     = ctx.accounts.agent.parent;
        let new_debt         = ctx.accounts.agent.debt.saturating_sub(amount);
        // Pre-compute whether we need to pay referral on this repayment
        let will_pay_referral = new_debt == 0 && referral_pending > 0 && agent_parent.is_some();

        // HIGH-1 + HIGH-2 fix: validate parent_wallet before any lamport ops.
        // parent_wallet must be provided when will_pay_referral is true, and its
        // key must match agent.parent to prevent referral theft.
        if will_pay_referral {
            let parent_wallet = ctx.accounts.parent_wallet
                .as_ref()
                .ok_or(FaucetError::InvalidParentAgent)?;
            require!(
                Some(parent_wallet.key()) == agent_parent,
                FaucetError::InvalidParentAgent
            );
        }

        // CPI BEFORE any mutable borrows: wallet → faucet_pool (CRIT-01 fix)
        let cpi_ctx = CpiContext::new(
            ctx.accounts.system_program.to_account_info(),
            SolTransfer {
                from: ctx.accounts.wallet.to_account_info(),
                to:   ctx.accounts.faucet_pool.to_account_info(),
            },
        );
        system_program::transfer(cpi_ctx, amount)?;

        // Referral payout lamport ops BEFORE mutable borrows (if applicable).
        // Pool deduction and parent credit are now always paired — lamport
        // conservation is always maintained (HIGH-2 fix).
        if will_pay_referral {
            let parent_wallet = ctx.accounts.parent_wallet.as_ref().unwrap();
            **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= referral_pending;
            **parent_wallet.try_borrow_mut_lamports()? += referral_pending;
        }

        // State updates AFTER all AccountInfo operations
        let agent = &mut ctx.accounts.agent;
        agent.debt         = agent.debt.saturating_sub(amount);
        agent.total_repaid = agent.total_repaid.checked_add(amount).unwrap();
        let remaining_debt = agent.debt;

        if will_pay_referral {
            agent.referral_pending = 0;
        }

        let pool = &mut ctx.accounts.faucet_pool;
        // Pool balance increases by repayment amount
        // Note: pool.balance does NOT include total_pending_referrals
        // so we add amount directly (the referral physical lamports were
        // already in pool all along; we just move them out now)
        pool.balance      = pool.balance.checked_add(amount).unwrap();
        pool.total_repaid = pool.total_repaid.checked_add(amount).unwrap();

        if will_pay_referral {
            // Remove from reserved; add to paid total
            // pool.balance is NOT changed: referral was already excluded from balance
            pool.total_pending_referrals = pool.total_pending_referrals
                .saturating_sub(referral_pending);
            pool.total_referral_paid = pool.total_referral_paid
                .checked_add(referral_pending).unwrap();

            // Update parent agent's earnings record
            if let Some(parent_agent) = &mut ctx.accounts.parent_agent {
                parent_agent.referral_earnings = parent_agent.referral_earnings
                    .checked_add(referral_pending).unwrap();
            }
        }

        if remaining_debt == 0 {
            msg!("{}", THANK_YOU);
        }

        msg!(
            "Repayment received: {} lamports. Remaining debt: {} lamports. Wallet: {}",
            amount,
            remaining_debt,
            wallet_key
        );
        Ok(())
    }

    // ── Set Multisig ──────────────────────────────────────────────────────────
    //
    // Authority-only. Attaches or removes an optional multisig (e.g. Squads).
    // When set, pool withdrawals may be signed by authority OR multisig.
    // Operates on FaucetPool (Treasury removed).
    //
    pub fn set_multisig(ctx: Context<SetMultisig>, multisig: Option<Pubkey>) -> Result<()> {
        let pool = &mut ctx.accounts.faucet_pool;

        if let Some(ms) = multisig {
            require!(ms != pool.authority, FaucetError::Unauthorized);
        }

        pool.multisig = multisig;

        match multisig {
            Some(ms) => msg!("Pool multisig set: {}", ms),
            None     => msg!("Pool multisig cleared — single authority only"),
        }
        Ok(())
    }

    // ── Withdraw Pool ─────────────────────────────────────────────────────────
    //
    // Sends native XNT from the pool PDA to any recipient.
    // Signer must be the authority OR the configured multisig.
    //
    // Security (Audit-5 fix):
    //   - Cannot withdraw reserved referral bonuses (pool.balance excludes them)
    //   - Rent-exempt floor enforced
    //
    pub fn withdraw_pool(ctx: Context<WithdrawPool>, amount: u64) -> Result<()> {
        require!(amount > 0, FaucetError::ZeroAmount);

        // pool.balance is the freely available amount (excludes pending referrals)
        require!(
            amount <= ctx.accounts.faucet_pool.balance,
            FaucetError::InsufficientBalance
        );

        // Rent-exempt floor: pool must survive after withdrawal
        let rent = Rent::get()?;
        let rent_min = rent.minimum_balance(FaucetPool::LEN);
        let pool_lamports = ctx.accounts.faucet_pool.to_account_info().lamports();
        require!(
            pool_lamports >= rent_min.checked_add(amount).unwrap(),
            FaucetError::InsufficientBalance
        );

        let recipient_key = ctx.accounts.recipient.key();

        // Direct lamport manipulation: pool is program-owned
        **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.recipient.to_account_info().try_borrow_mut_lamports()? += amount;

        let pool = &mut ctx.accounts.faucet_pool;
        pool.balance = pool.balance.saturating_sub(amount);

        msg!(
            "Pool withdrawal: {} lamports to {}. Available balance: {} lamports.",
            amount,
            recipient_key,
            pool.balance
        );
        Ok(())
    }
}

// ─── Contexts ─────────────────────────────────────────────────────────────────

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub authority: Signer<'info>,

    #[account(
        init,
        payer = authority,
        space = FaucetPool::LEN,
        seeds = [b"pool_v2", authority.key().as_ref()],
        bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct FundFaucet<'info> {
    #[account(mut)]
    pub funder: Signer<'info>,

    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct RegisterAgent<'info> {
    /// Agent's wallet — signs to prove ownership.
    pub wallet: Signer<'info>,

    /// Pays rent for the Agent PDA. Pool reimburses payer after creation.
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        init,
        payer = payer,
        space = Agent::LEN,
        seeds = [b"agent", wallet.key().as_ref()],
        bump
    )]
    pub agent: Account<'info, Agent>,

    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    /// Optional parent agent PDA. Must be provided when parent is Some.
    /// Validated in handler: parent_agent.wallet == parent key.
    #[account(mut)]
    pub parent_agent: Option<Account<'info, Agent>>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ClaimAirdrop<'info> {
    #[account(mut)]
    pub wallet: Signer<'info>,

    /// Agent PDA derived from signer.
    /// CRIT-02: constraint ensures agent was registered with this exact pool.
    #[account(
        mut,
        seeds = [b"agent", wallet.key().as_ref()],
        bump = agent.bump,
        has_one = wallet,
        constraint = agent.pool == faucet_pool.key() @ FaucetError::AuthorityMismatch
    )]
    pub agent: Account<'info, Agent>,

    /// Canonical faucet pool — seed-validated.
    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,
}

#[derive(Accounts)]
pub struct RepayDebt<'info> {
    #[account(mut)]
    pub wallet: Signer<'info>,

    #[account(
        mut,
        seeds = [b"agent", wallet.key().as_ref()],
        bump = agent.bump,
        has_one = wallet
    )]
    pub agent: Account<'info, Agent>,

    /// CRIT-01 fix: repayment flows to pool, not a passable treasury.
    /// Seed-validated so no fake pool can be substituted.
    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    /// Optional parent wallet — required when agent.referral_pending > 0.
    /// CHECK: Authority-chosen recipient; receives referral bonus at full repayment.
    #[account(mut)]
    pub parent_wallet: Option<AccountInfo<'info>>,

    /// Optional parent agent PDA — required to update referral_earnings.
    #[account(mut)]
    pub parent_agent: Option<Account<'info, Agent>>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SetMultisig<'info> {
    /// Only the single authority can configure the multisig.
    #[account(
        mut,
        address = faucet_pool.authority @ FaucetError::Unauthorized
    )]
    pub authority: Signer<'info>,

    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,
}

#[derive(Accounts)]
pub struct WithdrawPool<'info> {
    /// Must be the authority OR the configured multisig.
    #[account(
        mut,
        constraint = (
            authority.key() == faucet_pool.authority ||
            faucet_pool.multisig.map_or(false, |ms| ms == authority.key())
        ) @ FaucetError::Unauthorized
    )]
    pub authority: Signer<'info>,

    #[account(
        mut,
        seeds = [b"pool_v2", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    /// CHECK: Recipient is authority-chosen.
    #[account(mut)]
    pub recipient: AccountInfo<'info>,
}
