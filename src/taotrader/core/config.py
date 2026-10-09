"""taotrader/core/config.py - frozen config dataclasses. TOML is parsed and validated by ops.config_load (WP0);
the core only sees these. Units are in the field names. Defaults == section 3 of DESIGN.md.
Cross-field rule checked by config_load: RiskCfg.unwind_exec_blocks == ExecCfg.finality_lag_blocks + latency_blocks.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .units import BookId, Ppm, Rao, RunMode, Stage, StrategyId


@dataclass(frozen=True, slots=True)
class ExecCfg:
    """Shared cost and fill model (venues.sim, strategies' cost gates, planner)."""
    latency_blocks: int = 2                    # shielded inclusion N+2
    finality_lag_blocks: int = 3               # engine acts on finalized heads; added to fill timing in sim
    shield_miss_ppm: int = 11_000              # 1.1% non-decrypt (swept 0.5%-3%)
    buy_tx_fee_rao: int = 1_028_000            # Proxy(add_stake_limit) 933,081 + carrier ~94,560
    sell_tx_fee_rao: int = 837_000             # Proxy(remove_stake_limit) 742,666 + carrier
    move_tx_fee_rao: int = 1_000_000           # same-subnet move_stake (UNMEASURED; verify on test.finney)
    rotate_tx_fee_rao: int = 1_250_000         # Proxy(swap_stake_limit) measured; move_stake_limit UNMEASURED
    carrier_fee_rao: int = 98_000              # charged on a miss only if the carrier was included; sim books it on every
                                               # injected miss (the measured 1.1% were included-but-undecrypted carriers)
    impact_half_life_blocks: int | None = 0    # 0 = healed/temporary impact (HEADLINE, gating); None = permanent (optimistic)
    fail_inject_ppm: int = 0                   # extra random inner-call failures (stress)
    n_delegates: int = 3                       # delegates modelled in sim/paper; must equal the live delegate count


@dataclass(frozen=True, slots=True)
class RiskCfg:
    # --- prune engine
    unwind_exec_blocks: int = 5                # L_exec per attempt = finality lag 3 + N+2 latency 2 (outcome final once
                                               # N+2 is finalized); U = L_exec * (1 + retries) = 15
    unwind_retries: int = 2
    margin_a_blocks: int = 300                 # M_A (FT1-calibrated; range 60-1,200)
    d_stress_ppm: Ppm = Ppm(500_000)           # Tier A stressed spot = (1 - 0.5) * spot
    k_bottom: int = 3                          # backstop rank
    r_backstop_ppm: Ppm = Ppm(1_200_000)       # backstop when cost/L <= 1.2
    tier_b_enabled: bool = False               # phase 2: after FT1 + FT2 pass
    h_b_blocks: int = 7_200
    pi_b_ppm: Ppm = Ppm(7_500)                 # Tier B: P_prune_24h * (1 - R) >= 0.75% of position
    mc_paths: int = 2_000
    entry_min_rank: int = 6                    # non-immune entry floor (prune_rank >= 6)
    entry_min_rho_ppm: Ppm = Ppm(1_500_000)    # EMA >= 1.5 x target EMA
    pi_entry_7d_ppm: Ppm = Ppm(10_000)         # once MC is enabled
    r_default_ppm: Ppm = Ppm(350_000)
    # --- emission, burn, launch age (single source for all sleeves)
    emission_ban_blocks: int = 100_800         # 14 d after a disable
    reenable_wait_blocks: int = 360
    burn_entry_max_ppm: Ppm = Ppm(500_000)
    burn_exit_ppm: Ppm = Ppm(900_000)          # NORMAL exit when MinerBurned >= 0.9 for 2 epochs
    min_since_start_blocks: int = 100_800      # 14 d (EMA 99.7% warm)
    min_age_reg_blocks: int = 216_000          # 30 d (launch phase belongs to LCW only)
    launch_stop_before_immunity_end: int = 144_000   # LCW hard stop at NetworkImmunityPeriod - this (read live)
    # --- liquidity and size
    s_exit_entry_ppm: Ppm = Ppm(15_000)        # V_cap = T_st * s / (1 - s)
    s_exit_hold_max_ppm: Ppm = Ppm(25_000)
    d_t_ppm: Ppm = Ppm(200_000)                # pool stress haircut
    s_urgent_ppm: Ppm = Ppm(30_000)
    nu_max_ppm: Ppm = Ppm(150_000)             # per-subnet share of NAV_liq
    g_max_ppm: Ppm = Ppm(800_000)              # gross alpha <= 80% of NAV_liq
    n_max: int = 12
    v_min_rao: Rao = Rao(500_000_000)          # 0.5 TAO min order
    remainder_min_rao: Rao = Rao(500_000_000)  # max(0.05 TAO, V_MIN)
    band_ppm: Ppm = Ppm(200_000)               # no-trade band
    ladder_bucket_ppm: Ppm = Ppm(150_000)      # sum over prune_rank <= 15
    owner_cluster_ppm: Ppm = Ppm(200_000)
    young_bucket_ppm: Ppm = Ppm(100_000)       # since_start < 30 d
    exit_budget_ppm: Ppm = Ppm(20_000)         # sum ES*V <= 2% NAV_liq
    escrow_max_ppm: Ppm = Ppm(500_000)         # E/x
    t_min_pool_rao: Rao = Rao(200 * 10**9)
    fee_rate_max: int = 330
    gate_haircuts_active: bool = False         # m_gate / m_trd monitoring-only until FT4 passes
    min_free_real_rao: Rao = Rao(50_000_000)   # MIN_FREE_REAL: 0.05 TAO never spent on buys; must be >= existential deposit
    # --- owner
    owner_cooldown_blocks: int = 7_200
    owner_unstake_frac_ppm: Ppm = Ppm(20_000)  # owner coldkey unstake > 2% of SubnetTAO
    owner_liquid_max_ppm: Ppm = Ppm(100_000)   # m_owner = 0.5 on V_cap when owner_liquid_frac >= 10%
    owner_haircut_active: bool = False         # m_owner monitoring-only until an FT5-style ablation passes
    # --- validator router
    take_max_ppm: Ppm = Ppm(50_000)
    q_max_ppm: Ppm = Ppm(50_000)               # our stake <= 5% of TotalHotkeyAlpha(h, n)
    permit_rank_frac_ppm: Ppm = Ppm(800_000)
    k_epochs: int = 20
    switch_min_gain_ppm_day: int = 200         # 0.02 %/day
    # --- planner
    beta_entry_floor_ppm: Ppm = Ppm(1_000)
    beta_entry_cap_ppm: Ppm = Ppm(20_000)
    beta_exit_floor_ppm: Ppm = Ppm(2_500)
    beta_exit_cap_ppm: Ppm = Ppm(50_000)
    k_chase: int = 2
    chi_max_ppm: Ppm = Ppm(15_000)
    finality_shield_pause_blocks: int = 5
    # --- modes and kill switches (DD/daily thresholds are re-derived by bootstrap: <= 5% false CAUTION days)
    stall_warn_s: int = 36
    stall_halt_s: int = 120
    finality_caution_blocks: int = 30
    stale_prune_blocks: int = 25
    dd_soft_ppm: Ppm = Ppm(150_000)
    dd_hard_ppm: Ppm = Ppm(250_000)
    daily_loss_ppm: Ppm = Ppm(80_000)
    fail_burst_netuid: int = 3                 # per 600 blocks -> 7,200-block subnet cooldown (per-block outcomes only)
    fail_burst_global: int = 5                 # per 600 blocks -> 300-block CAUTION (per-block outcomes only)
    fee_float_alert_rao: Rao = Rao(250_000_000)     # live fee float < 0.25 TAO -> alert
    fee_float_caution_rao: Rao = Rao(150_000_000)   # < 0.15 TAO -> CAUTION
    fee_float_exits_rao: Rao = Rao(50_000_000)      # < 0.05 TAO -> EXITS_ONLY (alpha-fee trap)
    spec_burn_in_blocks: int = 100_800         # halve budgets 14 d after a spec with regimes.touches_econ, or after a
                                               # post-spec parity breach (section 3.10 step 2)
    allow_emergency_exits_when_frozen: bool = True   # Tier A exits with tight fill-or-kill limits even on key alarm
    regime_throttle_active: bool = False       # the ONE market-regime throttle: monitoring-only until FT-R1 passes


@dataclass(frozen=True, slots=True)
class SleeveCfg:
    strategy: StrategyId
    stage: Stage
    budget_ppm: Ppm                            # share of G_MAX_EFF * NAV_liq; funded by stage (section 3.10)
    params: Mapping[str, object] = field(default_factory=dict)   # validated into the strategy's own Params


@dataclass(frozen=True, slots=True)
class BookCfg:
    book: BookId
    capital_rao: Rao                           # REQUIRED explicit value; capital is unknown and configurable
    fee_float_rao: Rao
    sleeves: tuple[SleeveCfg, ...]
    risk: RiskCfg = RiskCfg()
    exec: ExecCfg = ExecCfg()
    dereg_model: str = "formula"               # "formula" | "fixed:350000" | "fixed:650000" (ppm of spot)


@dataclass(frozen=True, slots=True)
class RpcCfg:
    head_endpoints: tuple[str, ...]            # wss://, in priority order
    archive_endpoints: tuple[str, ...]         # https:// JSON-RPC
    rate_per_s: float = 3.0
    burst: int = 3
    max_concurrency: int = 3
    keys_per_call: int = 2_000
    timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class LiveCfg:
    enabled: bool = False                      # lock 1 of 4
    mode: str = "plan_only"                    # "plan_only" | "submit"  (lock 2: config confirmation)
    network: str = "test"                      # "test" | "finney"
    real_coldkey_ss58: str = ""
    delegate_wallets: tuple[str, ...] = ()     # Staking-proxy delegates (2-3)
    sleeves: tuple[StrategyId, ...] = ()       # only LIVE_ELIGIBLE sleeves the user lists here
    allowed_netuids: tuple[int, ...] = ()      # empty = overlay universe; buys only. Sell/move policies use
                                               # allowed_netuids + held netuids, so an edit never strands a position
    # Caps below bound BUYS ONLY (section 9.6). Sells are bounded only by the held position and same-subnet moves by
    # the position on the origin hotkey; a risk exit (urgency >= URGENT) is never VENUE_REJECTed for a cap.
    max_order_tao: float = 1.0                 # per buy; also the buy Policy's max_spend_tao
    max_daily_turnover_tao: float = 5.0        # buy TAO per 7,200 blocks (sells and moves not counted)
    max_position_tao: float = 5.0              # a buy may not take a position's executable value above this
    max_fee_tao: float = 0.005
    min_fee_float_tao: float = 0.15            # preflight floor (== RiskCfg.fee_float_caution_rao)
    max_ops_balance_tao: float = 0.5           # preflight: each delegate's free balance <= this (a fee buffer only)
    accepted_specs: tuple[int, ...] = ()       # submit refuses on any other spec_version (UNARMED, section 9.3)
    risk_exits_when_unarmed: bool = False      # user sets it explicitly in live.example.toml: while UNARMED (arm token
                                               # expired or spec not accepted), allow EMERGENCY/URGENT full sells only,
                                               # and only if V2, V3, V6 pass on the current spec (section 9.3)


@dataclass(frozen=True, slots=True)
class RunCfg:
    run_id: str
    mode: RunMode
    books: tuple[BookCfg, ...]
    rpc: RpcCfg
    live: LiveCfg = LiveCfg()
    data_dir: str = "data"
    cadence_blocks: int = 60                   # backtest stride / paper FULL-plan cadence
    seed: int = 0                              # stochastic pieces are seeded from (seed, block_hash)
