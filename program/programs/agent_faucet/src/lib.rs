use anchor_lang::{
    prelude::*,
    system_program::{self, Transfer as SolTransfer},
};

// Program ID
declare_id!("9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR");

// ─── Constants ────────────────────────────────────────────────────────────────

pub const CLAIM_AMOUNT: u64        = 210_000_000; // 0.21 XNT (9 decimals)
pub const REVENUE_SHARE_PERCENT: u64 = 25;        // 25%  → debt = 0.2625 XNT
pub const REFERRAL_BONUS_PERCENT: u64 = 10;       // 10%  → parent earns 0.021 XNT
pub const BASIS_POINTS: u64        = 100;
pub const MAX_CLAIM_AMOUNT: u64    = 1_000_000_000; // Safety ceiling: 1 XNT

// ─── Errors ───────────────────────────────────────────────────────────────────

#[error_code]
pub enum FaucetError {
    #[msg("Agent has already claimed their one-time airdrop")]
    AlreadyClaimed,
    #[msg("Agent is not registered")]
    AgentNotRegistered,
    #[msg("Faucet has insufficient balance")]
    InsufficientBalance,
    #[msg("Invalid parent: cannot refer yourself")]
    InvalidParentAgent,
    #[msg("Cannot repay more than outstanding debt")]
    OverRepayment,
    #[msg("Signer is not the authority or configured multisig")]
    Unauthorized,
    #[msg("Multisig is not configured on this treasury")]
    MultisigNotSet,
    #[msg("Promise must be acknowledged to register")]
    PromiseNotAcknowledged,
    #[msg("Amount must be greater than zero")]
    ZeroAmount,
    #[msg("Claim amount exceeds safety ceiling")]
    ClaimAmountTooLarge,
    #[msg("Faucet pool and treasury authority mismatch")]
    AuthorityMismatch,
}

// ─── State ────────────────────────────────────────────────────────────────────

/// Created once per wallet via register_agent.
/// promise_acknowledged is recorded on-chain at registration — immutable proof.
#[account]
pub struct Agent {
    pub wallet: Pubkey,
    pub parent: Option<Pubkey>,       // Optional referrer
    pub debt: u64,                    // Lamports owed to treasury (principal + 25%)
    pub total_claimed: u64,           // Always CLAIM_AMOUNT or 0
    pub total_repaid: u64,
    pub referrals: u32,               // Number of wallets this agent referred
    pub referral_earnings: u64,       // Lamports earned from referral bonuses
    pub has_claimed: bool,            // One-time gate — set to true after first claim
    pub promise_acknowledged: bool,   // On-chain record of Promise acknowledgment
    pub registered_at: i64,
    pub bump: u8,
}

impl Agent {
    pub const LEN: usize = 8    // discriminator
        + 32                    // wallet
        + 1 + 32                // parent Option<Pubkey>
        + 8                     // debt
        + 8                     // total_claimed
        + 8                     // total_repaid
        + 4                     // referrals
        + 8                     // referral_earnings
        + 1                     // has_claimed
        + 1                     // promise_acknowledged
        + 8                     // registered_at
        + 1;                    // bump
}

/// Single PDA holding program state + native XNT lamports for claims.
/// Permissionlessly fundable — anyone can call fund_faucet at any time.
#[account]
pub struct FaucetPool {
    pub authority: Pubkey,
    pub balance: u64,             // Tracked spendable lamports (total - rent)
    pub total_distributed: u64,
    pub claim_amount: u64,        // Lamports per claim (set at init)
    pub revenue_share_percent: u64,
    pub referral_bonus_percent: u64,
    pub total_agents: u32,
    pub bump: u8,
}

impl FaucetPool {
    pub const LEN: usize = 8    // discriminator
        + 32                    // authority
        + 8                     // balance
        + 8                     // total_distributed
        + 8                     // claim_amount
        + 8                     // revenue_share_percent
        + 8                     // referral_bonus_percent
        + 4                     // total_agents
        + 1;                    // bump
}

/// Accumulates repaid XNT. Withdrawable by authority or optional multisig.
#[account]
pub struct Treasury {
    pub authority: Pubkey,
    /// Optional multisig (e.g. Squads vault PDA).
    /// When set, withdrawals may be signed by authority OR multisig.
    /// Only the single authority can set/clear this — prevents lockout.
    pub multisig: Option<Pubkey>,
    pub accumulated: u64,
    pub total_repaid: u64,
    pub total_referral_paid: u64,
    pub bump: u8,
}

impl Treasury {
    pub const LEN: usize = 8    // discriminator
        + 32                    // authority
        + 1 + 32                // multisig Option<Pubkey>
        + 8                     // accumulated
        + 8                     // total_repaid
        + 8                     // total_referral_paid
        + 1;                    // bump
}

// ─── Program ──────────────────────────────────────────────────────────────────

#[program]
pub mod agent_faucet {
    use super::*;

    // ── Initialize ────────────────────────────────────────────────────────────
    //
    // Called once by the deploying authority.
    // Pass claim_amount = CLAIM_AMOUNT (210_000_000 = 0.21 XNT).
    // After this, fund the faucet via fund_faucet before agents can claim.
    //
    pub fn initialize(ctx: Context<Initialize>, claim_amount: u64) -> Result<()> {
        require!(claim_amount > 0, FaucetError::ZeroAmount);
        require!(claim_amount <= MAX_CLAIM_AMOUNT, FaucetError::ClaimAmountTooLarge);

        let pool = &mut ctx.accounts.faucet_pool;
        let treasury = &mut ctx.accounts.treasury;

        pool.authority            = ctx.accounts.authority.key();
        pool.balance              = 0;
        pool.total_distributed    = 0;
        pool.claim_amount         = claim_amount;
        pool.revenue_share_percent  = REVENUE_SHARE_PERCENT;
        pool.referral_bonus_percent = REFERRAL_BONUS_PERCENT;
        pool.total_agents         = 0;
        pool.bump                 = ctx.bumps.faucet_pool;

        treasury.authority        = ctx.accounts.authority.key();
        treasury.multisig         = None;
        treasury.accumulated      = 0;
        treasury.total_repaid     = 0;
        treasury.total_referral_paid = 0;
        treasury.bump             = ctx.bumps.treasury;

        msg!(
            "Faucet initialized. Claim: {} lamports (0.21 XNT). Authority: {}",
            claim_amount,
            ctx.accounts.authority.key()
        );
        Ok(())
    }

    // ── Fund Faucet ───────────────────────────────────────────────────────────
    //
    // Fully permissionless — anyone (including the authority) can fund at any
    // time and as many times as needed. Native XNT only, no tokens.
    //
    // Seeds on faucet_pool are validated in the context, so callers cannot
    // pass a counterfeit pool account.
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
            "Faucet refilled: +{} lamports. Pool balance: {} lamports.",
            amount,
            pool.balance
        );
        Ok(())
    }

    // ── Register Agent ────────────────────────────────────────────────────────
    //
    // Permissionless — any wallet registers itself.
    // The Promise MUST be acknowledged (acknowledge_promise = true).
    // This is enforced on-chain and recorded permanently in agent.promise_acknowledged.
    //
    // Registration flow:
    //   1. Client calls display_promise() (Python bridge)
    //   2. User reads and accepts the Promise
    //   3. Client calls register_agent with acknowledge_promise = true
    //   4. Program creates the Agent PDA, records acknowledgment on-chain
    //   5. Agent is now eligible to call claim_airdrop (once, ever)
    //
    pub fn register_agent(
        ctx: Context<RegisterAgent>,
        parent: Option<Pubkey>,
        acknowledge_promise: bool,
    ) -> Result<()> {
        // Promise gate — cannot register without acknowledging
        require!(acknowledge_promise, FaucetError::PromiseNotAcknowledged);

        if let Some(parent_key) = parent {
            require!(parent_key != ctx.accounts.wallet.key(), FaucetError::InvalidParentAgent);
        }

        let agent = &mut ctx.accounts.agent;
        agent.wallet                = ctx.accounts.wallet.key();
        agent.parent                = parent;
        agent.debt                  = 0;
        agent.total_claimed         = 0;
        agent.total_repaid          = 0;
        agent.referrals             = 0;
        agent.referral_earnings     = 0;
        agent.has_claimed           = false;
        agent.promise_acknowledged  = true; // Immutable on-chain record
        agent.registered_at         = Clock::get()?.unix_timestamp;
        agent.bump                  = ctx.bumps.agent;

        ctx.accounts.faucet_pool.total_agents += 1;

        match parent {
            Some(pk) => msg!("Agent {} registered. Referrer: {}", ctx.accounts.wallet.key(), pk),
            None     => msg!("Agent {} registered. No referrer.", ctx.accounts.wallet.key()),
        }
        Ok(())
    }

    // ── Claim Airdrop ─────────────────────────────────────────────────────────
    //
    // Permissionless for registered agents. One-time, forever.
    // Transfers 0.21 XNT native coin from faucet pool to agent wallet.
    // Records debt of 0.2625 XNT (principal + 25% revenue share) on-chain.
    //
    // Security: seed validation ensures pool and treasury are canonical PDAs
    // from the same authority. Rent-exempt floor is checked before deduction.
    //
    pub fn claim_airdrop(ctx: Context<ClaimAirdrop>) -> Result<()> {
        // Capture all values we need BEFORE any mutable borrows
        let claim_amount      = ctx.accounts.faucet_pool.claim_amount;
        let revenue_share_pct = ctx.accounts.faucet_pool.revenue_share_percent;
        let referral_bonus_pct = ctx.accounts.faucet_pool.referral_bonus_percent;
        let pool_balance      = ctx.accounts.faucet_pool.balance;
        let agent_has_claimed = ctx.accounts.agent.has_claimed;
        let agent_parent      = ctx.accounts.agent.parent;

        // One-time only
        require!(!agent_has_claimed, FaucetError::AlreadyClaimed);

        // Tracked balance check
        require!(pool_balance >= claim_amount, FaucetError::InsufficientBalance);

        // Rent-exempt floor — pool must stay alive after transfer
        let rent = Rent::get()?;
        let rent_min = rent.minimum_balance(FaucetPool::LEN);
        let pool_lamports = ctx.accounts.faucet_pool.to_account_info().lamports();
        require!(
            pool_lamports >= rent_min.checked_add(claim_amount).unwrap(),
            FaucetError::InsufficientBalance
        );

        // Lamport transfer BEFORE mutable state borrows
        // faucet_pool is program-owned → direct lamport manipulation
        **ctx.accounts.faucet_pool.to_account_info().try_borrow_mut_lamports()? -= claim_amount;
        **ctx.accounts.wallet.to_account_info().try_borrow_mut_lamports()?      += claim_amount;

        // Calculate debt
        let revenue_share = claim_amount
            .checked_mul(revenue_share_pct).unwrap()
            .checked_div(BASIS_POINTS).unwrap();
        let total_debt = claim_amount.checked_add(revenue_share).unwrap();

        // State updates AFTER AccountInfo operations
        let agent = &mut ctx.accounts.agent;
        agent.has_claimed   = true;
        agent.debt          = total_debt;
        agent.total_claimed = claim_amount;

        let pool = &mut ctx.accounts.faucet_pool;
        pool.balance           = pool.balance.saturating_sub(claim_amount);
        pool.total_distributed = pool.total_distributed.checked_add(claim_amount).unwrap();

        // Queue referral bonus
        if let Some(parent_key) = agent_parent {
            let referral_bonus = claim_amount
                .checked_mul(referral_bonus_pct).unwrap()
                .checked_div(BASIS_POINTS).unwrap();
            ctx.accounts.treasury.total_referral_paid = ctx.accounts.treasury
                .total_referral_paid
                .checked_add(referral_bonus).unwrap();
            msg!("Referral bonus {} lamports queued for parent: {}", referral_bonus, parent_key);
        }

        msg!(
            "Airdrop claimed: {} lamports (0.21 XNT). Debt: {} lamports (0.2625 XNT). Wallet: {}",
            claim_amount,
            total_debt,
            ctx.accounts.wallet.key()
        );
        Ok(())
    }

    // ── Repay Debt ────────────────────────────────────────────────────────────
    //
    // Permissionless for registered agents.
    // Agent sends native XNT from their wallet to the treasury.
    // A thank-you message is emitted on every repayment.
    //
    pub fn repay_debt(ctx: Context<RepayDebt>, amount: u64) -> Result<()> {
        require!(amount > 0, FaucetError::ZeroAmount);
        require!(amount <= ctx.accounts.agent.debt, FaucetError::OverRepayment);

        let wallet_key = ctx.accounts.wallet.key();

        // CPI BEFORE mutable borrows — system_program::transfer needs treasury AccountInfo
        let cpi_ctx = CpiContext::new(
            ctx.accounts.system_program.to_account_info(),
            SolTransfer {
                from: ctx.accounts.wallet.to_account_info(),
                to:   ctx.accounts.treasury.to_account_info(),
            },
        );
        system_program::transfer(cpi_ctx, amount)?;

        // State updates AFTER CPI
        let agent = &mut ctx.accounts.agent;
        agent.debt         = agent.debt.saturating_sub(amount);
        agent.total_repaid = agent.total_repaid.checked_add(amount).unwrap();
        let remaining_debt = agent.debt;

        let treasury = &mut ctx.accounts.treasury;
        treasury.accumulated  = treasury.accumulated.checked_add(amount).unwrap();
        treasury.total_repaid = treasury.total_repaid.checked_add(amount).unwrap();

        if remaining_debt == 0 {
            msg!(
                "Empire's trust has been extended to you. Honor matters. \
                 Your debt is fully cleared — the faucet flows because you kept your word. \
                 Wallet: {}",
                wallet_key
            );
        } else {
            msg!(
                "Thank you — your repayment of {} lamports is received. \
                 Empire's trust has been extended to you. Honor matters. \
                 Remaining debt: {} lamports. Wallet: {}",
                amount,
                remaining_debt,
                wallet_key
            );
        }
        Ok(())
    }

    // ── Auto-Repay ────────────────────────────────────────────────────────────
    //
    // Called by the bridge when an agent earns XNT.
    // Automatically routes 25% of earnings to treasury until debt is cleared.
    //
    pub fn auto_repay(ctx: Context<AutoRepay>, earnings: u64) -> Result<()> {
        require!(earnings > 0, FaucetError::ZeroAmount);

        if ctx.accounts.agent.debt == 0 {
            return Ok(());
        }

        let current_debt = ctx.accounts.agent.debt;
        let wallet_key   = ctx.accounts.wallet.key();

        let repayment = earnings
            .checked_mul(REVENUE_SHARE_PERCENT).unwrap()
            .checked_div(BASIS_POINTS).unwrap();
        let repayment = std::cmp::min(repayment, current_debt);

        // CPI BEFORE mutable borrows
        let cpi_ctx = CpiContext::new(
            ctx.accounts.system_program.to_account_info(),
            SolTransfer {
                from: ctx.accounts.wallet.to_account_info(),
                to:   ctx.accounts.treasury.to_account_info(),
            },
        );
        system_program::transfer(cpi_ctx, repayment)?;

        // State updates AFTER CPI
        let agent = &mut ctx.accounts.agent;
        agent.debt         = agent.debt.saturating_sub(repayment);
        agent.total_repaid = agent.total_repaid.checked_add(repayment).unwrap();
        let remaining_debt = agent.debt;

        let treasury = &mut ctx.accounts.treasury;
        treasury.accumulated  = treasury.accumulated.checked_add(repayment).unwrap();
        treasury.total_repaid = treasury.total_repaid.checked_add(repayment).unwrap();

        if remaining_debt == 0 {
            msg!(
                "Empire's trust has been extended to you. Honor matters. \
                 Your debt is fully cleared — the faucet flows because you kept your word. \
                 Wallet: {}",
                wallet_key
            );
        } else {
            msg!(
                "Thank you — auto-repaid {} lamports from {} earnings. \
                 Empire's trust has been extended to you. Honor matters. \
                 Remaining debt: {} lamports.",
                repayment,
                earnings,
                remaining_debt
            );
        }
        Ok(())
    }

    // ── Set Multisig ──────────────────────────────────────────────────────────
    //
    // Authority-only. Attaches or removes an optional multisig (e.g. Squads).
    // When set, treasury withdrawals can be signed by authority OR multisig.
    // Only the single authority can change this — prevents multisig lockout.
    // Pass multisig = None to revert to single-authority-only mode.
    //
    pub fn set_multisig(ctx: Context<SetMultisig>, multisig: Option<Pubkey>) -> Result<()> {
        let treasury = &mut ctx.accounts.treasury;

        if let Some(ms) = multisig {
            require!(ms != treasury.authority, FaucetError::Unauthorized);
        }

        treasury.multisig = multisig;

        match multisig {
            Some(ms) => msg!("Treasury multisig set: {}", ms),
            None     => msg!("Treasury multisig cleared — single authority only"),
        }
        Ok(())
    }

    // ── Withdraw Treasury ─────────────────────────────────────────────────────
    //
    // Sends accumulated native XNT from the treasury PDA to any recipient.
    // Signer must be the authority OR the configured multisig (if set).
    // Treasury PDA is program-owned, so lamports are moved directly.
    // Rent-exempt floor is enforced — treasury account cannot be drained below rent.
    //
    pub fn withdraw_treasury(ctx: Context<WithdrawTreasury>, amount: u64) -> Result<()> {
        require!(amount > 0, FaucetError::ZeroAmount);
        require!(amount <= ctx.accounts.treasury.accumulated, FaucetError::InsufficientBalance);

        let recipient_key = ctx.accounts.recipient.key();

        // Rent-exempt floor — treasury must stay alive after withdrawal
        let rent = Rent::get()?;
        let rent_min = rent.minimum_balance(Treasury::LEN);
        let treasury_lamports = ctx.accounts.treasury.to_account_info().lamports();
        require!(
            treasury_lamports >= rent_min.checked_add(amount).unwrap(),
            FaucetError::InsufficientBalance
        );

        // Lamport manipulation BEFORE mutable borrow
        // Treasury PDA is program-owned → direct lamport manipulation
        **ctx.accounts.treasury.to_account_info().try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.recipient.to_account_info().try_borrow_mut_lamports()? += amount;

        // State update AFTER lamport manipulation
        let treasury = &mut ctx.accounts.treasury;
        treasury.accumulated = treasury.accumulated.saturating_sub(amount);

        msg!(
            "Treasury withdrawal: {} lamports to {}. Remaining: {} lamports.",
            amount,
            recipient_key,
            treasury.accumulated
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
        seeds = [b"faucet_pool", authority.key().as_ref()],
        bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    #[account(
        init,
        payer = authority,
        space = Treasury::LEN,
        seeds = [b"treasury", authority.key().as_ref()],
        bump
    )]
    pub treasury: Account<'info, Treasury>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct FundFaucet<'info> {
    /// Anyone can fund — fully permissionless.
    #[account(mut)]
    pub funder: Signer<'info>,

    /// Seeds validated — cannot pass a counterfeit pool account.
    #[account(
        mut,
        seeds = [b"faucet_pool", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct RegisterAgent<'info> {
    /// Agent's wallet — signs to prove ownership. Zero XNT balance is OK.
    pub wallet: Signer<'info>,

    /// Pays rent for the Agent PDA. Can be the authority, a relayer, or the
    /// agent itself if it already holds enough XNT.
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
        seeds = [b"faucet_pool", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ClaimAirdrop<'info> {
    #[account(mut)]
    pub wallet: Signer<'info>,

    /// Agent PDA derived from signer — cannot be spoofed.
    #[account(
        mut,
        seeds = [b"agent", wallet.key().as_ref()],
        bump = agent.bump,
        has_one = wallet
    )]
    pub agent: Account<'info, Agent>,

    /// Canonical faucet pool — seed-validated.
    #[account(
        mut,
        seeds = [b"faucet_pool", faucet_pool.authority.as_ref()],
        bump = faucet_pool.bump
    )]
    pub faucet_pool: Account<'info, FaucetPool>,

    /// Canonical treasury — seed-validated and must share authority with pool.
    #[account(
        mut,
        seeds = [b"treasury", treasury.authority.as_ref()],
        bump = treasury.bump,
        constraint = treasury.authority == faucet_pool.authority @ FaucetError::AuthorityMismatch
    )]
    pub treasury: Account<'info, Treasury>,
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

    /// Canonical treasury — seed-validated.
    #[account(
        mut,
        seeds = [b"treasury", treasury.authority.as_ref()],
        bump = treasury.bump
    )]
    pub treasury: Account<'info, Treasury>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct AutoRepay<'info> {
    #[account(mut)]
    pub wallet: Signer<'info>,

    #[account(
        mut,
        seeds = [b"agent", wallet.key().as_ref()],
        bump = agent.bump,
        has_one = wallet
    )]
    pub agent: Account<'info, Agent>,

    /// Canonical treasury — seed-validated.
    #[account(
        mut,
        seeds = [b"treasury", treasury.authority.as_ref()],
        bump = treasury.bump
    )]
    pub treasury: Account<'info, Treasury>,

    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SetMultisig<'info> {
    /// Only the single authority can configure the multisig.
    #[account(
        mut,
        address = treasury.authority @ FaucetError::Unauthorized
    )]
    pub authority: Signer<'info>,

    #[account(
        mut,
        seeds = [b"treasury", treasury.authority.as_ref()],
        bump = treasury.bump
    )]
    pub treasury: Account<'info, Treasury>,
}

#[derive(Accounts)]
pub struct WithdrawTreasury<'info> {
    /// Must be the single authority OR the configured multisig.
    #[account(
        mut,
        constraint = (
            authority.key() == treasury.authority ||
            treasury.multisig.map_or(false, |ms| ms == authority.key())
        ) @ FaucetError::Unauthorized
    )]
    pub authority: Signer<'info>,

    #[account(
        mut,
        seeds = [b"treasury", treasury.authority.as_ref()],
        bump = treasury.bump
    )]
    pub treasury: Account<'info, Treasury>,

    /// CHECK: Recipient is authority-chosen. Verified by the withdrawal signer.
    #[account(mut)]
    pub recipient: AccountInfo<'info>,
}
