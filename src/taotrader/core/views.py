"""taotrader/core/views.py - shared per-snapshot features. Computed ONCE per snapshot by features.engine and
shared by every book and strategy. Floats are allowed here (feature math); decisions quantize via to_ppm.

None means "not enough same-generation history" - consumers must treat None as ineligible, never as zero.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .units import Block, Hotkey, Ppm, SubnetKey


@dataclass(frozen=True, slots=True)
class RouterCandidate:
    """Book-independent YieldRouter inputs for one tracked hotkey (WP5, section 3.8). The per-book choice
    (Q_MAX against own shares, hysteresis, MOVE_STAKE) is WP8 risk/router.py."""
    hotkey: Hotkey
    score_ppm_day: int                   # EWMA_{hl 20 epochs}(d ln I per epoch) * 7200/Tempo, net of take (to_ppm)
    take_u16: int                        # Delegates[h]
    childkey_take_u16: int               # ChildkeyTake(h, n)
    member_frac_ppm: Ppm                 # share of the last 20 epochs with h in AlphaDividendsPerSubnet(n, .)
    member_last2: bool                   # recipient in each of the last 2 epochs
    permit_rank: int | None              # rank by TotalHotkeyAlpha among dividend recipients (1 = largest)
    ratio_ok: bool                       # realised / closed-form net yield in [0.65, 1.35] (False while T6 is re-validated)
    take_increase_recent: bool           # take raised within 216,000 blocks (Delegates diffs); unknown -> False, flagged
    eligible: bool                       # every book-independent filter of section 3.8 passes


@dataclass(frozen=True, slots=True)
class Feat:
    key: SubnetKey
    # --- price, depth, returns (log, from the median-of-3 60-block price; generation-truncated)
    spot: float                          # TAO/alpha (era-correct)
    pool_tao: float                      # SubnetTAO in TAO
    k_w: float                           # 1 / w_base (2.0 at 0.5/0.5)
    ret_1h: float | None
    ret_1d: float | None
    ret_7d: float | None
    sigma_d: float | None                # 14-day realised SD of daily ln P
    fast_ema_gap: float                  # ln(min(spot,1) / local 600-block-half-life EMA of spot)
    ema_gap: float                       # ln(min(spot,1) / SubnetMovingPrice)
    # --- flows (dSubnetTaoFlow / SubnetTAO; None before 8,466,531 or across a generation change)
    flow_1h: float | None
    flow_1d: float | None
    flow_7d: float | None
    flow_z_1d: float | None              # robust z vs trailing 30 d of this generation
    # --- emission and structure (shared protocol replicas; read-live parameters)
    emis_tao_day: float                  # modelled E_i
    chain_buy_day: float                 # modelled chain buy (TAO/day); 0 when E <= rp*alpha_em*spot*7200
    obs_emis_tao_day: float              # 7200*(SubnetTaoInEmission + SubnetExcessTao + reservoir delta)
    gate_keep: float                     # g/b = 1/(1+(theta/b)^h)
    burn_adj_rank: int | None            # 1 = largest burn-adjusted share among eligible
    ema_rank_desc: int | None            # 1 = largest SubnetMovingPrice
    rp: float                            # root proportion
    sell_push_day: float                 # k_w * S / y from protocol.sellload (fraction of price per day, >= 0)
    cb_push_day: float                   # k_w * CB / y
    escrow_frac: float | None            # E / x
    # --- yield (book-independent YieldRouter inputs; the per-book choice is risk/router.py, section 3.8)
    a_earn_alpha: float                  # sum TotalHotkeyAlpha over dividend recipients (tracked approximation flagged)
    yield_cf_gross_day: float            # AE*(1-c_o)*0.5*(1-rp)/A_earn
    router_candidates: tuple[RouterCandidate, ...]   # tracked hotkeys, best first (eligible, score, take, stake, hotkey)
    best_candidate: Hotkey | None        # first eligible candidate; NOT a book's choice (books differ by Q_MAX, hysteresis)
    yield_net_day: float | None          # realised EWMA d ln I of best_candidate, net of take
    a_earn_growth_day: float             # deterministic A_earn growth (escrow + compounding) as a fraction/day
    # --- lifecycle and prune
    age_reg_blocks: int                  # block - NetworkRegisteredAt
    since_start_blocks: int | None       # block - (FirstEmissionBlockNumber - 1)
    immune: bool
    immune_until: Block
    prune_rank: int | None               # 1 = current target among non-immune; None if immune
    rho: float | None                    # SubnetMovingPrice / bottom non-immune EMA
    t_star_stress_blocks: float | None   # time-to-target with spot -> D_STRESS * spot; inf -> None
    launch_flags: frozenset[str]         # Gatekeeper mechanical flags (section 3.7)
    # --- execution microstructure
    beta_entry_ppm: Ppm                  # q95 |ln p_t - ln p_{t-h}| over 1,800 blocks (30 stride points), own-fill blocks
                                         # excluded; h = finality_lag + latency (per-block data) or stride (section 3.12)
    beta_exit_ppm: Ppm                   # q99 of the same sample, for sells
    # --- owner and holder concentration (section 3.6; brief risk #7)
    owner_sold_6h_frac: float | None     # owner net alpha sold over 1,800 blocks / pool alpha
    owner_liquid_frac: float | None      # owner_alpha * (1 - autolock) / SubnetAlphaIn
    top_holder_frac: float | None        # (owner_alpha + top-5 tracked TotalHotkeyAlpha) / (AlphaOut - ProtocolAlpha)


@dataclass(frozen=True, slots=True)
class PruneView:
    prune_possible: bool                 # n_nonroot + cleanup_queue_len >= SubnetLimit
    target: SubnetKey | None             # local rule; == runtime target or a data alarm is raised
    runtime_agrees: bool
    ladder: tuple[SubnetKey, ...]        # non-immune, ascending (moving_price, reg_at)
    bottom_ema: float
    blocks_since_reg: int
    window_open: bool                    # blocks_since_reg >= NetworkRateLimit
    blocks_to_window: int                # 0 if open
    cost_ratio: float                    # r = registration cost / NetworkLastLockCost
    p_reg_ppm: tuple[tuple[int, Ppm], ...]   # (horizon_blocks, P(a registration lands within horizon))
    hazard_valid: bool                   # False after a registration-economics change (spec-475 PoW scope etc.)
    immunity_calendar: tuple[tuple[Block, SubnetKey], ...]   # upcoming expiries, ascending


@dataclass(frozen=True, slots=True)
class EmissionView:
    theta: float
    gate_rank: int
    sum_ema: float                       # sum SubnetMovingPrice over eligible; root sell flag = sum_ema > 1
    root_flag: bool
    parity_err_max_tao_day: float        # max |E_model - E_obs|
    model_ok: bool                       # protocol.emission.parity_ok: median |rel err| < 1% AND >= 90% within 5%, over
                                         # enabled subnets with E > 1 TAO/day, trailing 300-block observed emission (= T2a)


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    block: Block
    warm: bool                           # >= 30 days of history ingested; no strategy runs before
    feats: Mapping[SubnetKey, Feat]
    prune: PruneView
    emission: EmissionView
    regime_id: str                       # protocol.regimes label (reports and valid_from guards; strategies may not branch on it)
    universe_eligible: int               # overlay-floor sections A-G count (book-independent; published every snapshot)
    beta_horizon_blocks: int             # h used for beta at this snapshot (journaled via digest; reports split by it)
    digest: str                          # canonical digest, journaled in DecisionTrace
