"""taotrader/strategies/carry.py - sleeve (a) carry: hotkey-optimised nominator-yield carry, screened by structural
sell load (WP9; DESIGN.md section 2.1). Verdict CORE; MVP scope.

Score per generation k, per day (fractions; feature math in float, quantised once at the Signal boundary):
- Y_real: the realised EWMA d ln I of the yield hotkey h, net of take (RouterCandidate.score_ppm_day, WP5). h is the
  book's router choice (book_view.router), else Feat.best_candidate.
- Y_f = Y_real * A0 / (A0 + G * H/2) * (1 - yield_haircut). A0 = A_earn (alpha). G (alpha/day) = the deterministic
  A_earn growth (Feat.a_earn_growth_day * A0) + kappa_e * max(net inflow alpha/day, 0), with net inflow = flow_1d * y
  / spot. When the trailing 14-day A_earn growth exceeds G, the trailing rate is used, clipped at 3 %/day.
- D = cb_push - sell_push. cb_push is the observed trailing-300-block chain buy (Feat.cb_push_day). sell_push comes
  from protocol.sellload with phi = the as-of Calibration.phi (Params phi without a provider); sell_load_on = False is
  the T3 kill (phi = 0).
- lambda = p_reg(H_eval) * exp(-kappa_p * (rho - 1)) / H_eval (P_bottom capped at 1; rho unknown -> 1). It is 0 when no
  prune is possible or the generation stays immune through H_eval. R = protocol.prune.recovery_ratio through
  calibration.effective_recovery. Expected prune loss = lambda * (1 - R).
- mu = Y_f + D - lambda (1 - R) [+ deferred-module terms]; mu_lcb = mu - unc_z * sigma_model. sigma_model is the robust
  SD (1.4826 MAD) of realised - predicted per-day 5-day total return (ln P*I of the yield hotkey), pooled by pool-size
  tercile over a rolling 30 days. The prior 0.30 %/day applies until a tercile has 30 days of residuals.
- V* = y * max(mu_lcb * H - 2f - tx/V, 0) / 4 with one fixed-point iteration (protocol.amm.v_star on the view pool;
  tx = buy + sell tx fee). V = min(V*, v_cap * y, y * s_max/(1 - s_max), w_max * sleeve budget), halved in the
  research burn bucket. mu_net = mu_lcb - RT_TEMPORARY(V) / H (protocol.amm.round_trip_cost_ppm).

Entry (each scheduled evaluation, every rebalance_blocks): overlay floor A-G, C-U1..C-U11, no book cooldown/halt,
mu_net >= mu_in, V >= V_min, V/y <= v_cap. Qualifiers are ranked by mu_net / max(sigma_d, 3 %); at most N_max names are
held. Holdings need only mu_net >= mu_out (hysteresis) and are re-sized each rebalance. A trade is proposed only if
|target - held| >= max(V_min, 25 % of target). Signal: TARGET(weight_ppm = V / sleeve budget, max_size_rao = V,
edge_ppm_day = mu_lcb, alpha_h_ppm = mu_lcb * H, horizon = H, declares_dilution = True, hotkey_pref = h).

Exits (sleeve level, sticky until the sleeve is flat; the overlay's forced exits are separate and authoritative):
C-X1 owner distribution, C-X2 flow crash, C-X4 thesis stop and C-X5 stricter prune are HIGH. C-X3 hotkey loss, C-S1
decay, C-S2 rank and C-S3 max hold (re-underwritten through the entry path) are NORMAL.

Deferred modules (present, OFF by default; each turns on only after its ablation passes):
- forward_cb_sim: a 2-day forward chain-buy projection (flat-spot EMA forecasts through protocol.emission);
- gate_tilt: a bonus inside the band [EmissionBarRank - 10, EmissionBarRank + 13] of burn-adjusted rank;
- flow terms a1 * flow_1d + a2 * flow_7d / 7 (a1 = a2 = 0);
- emission_reenable: a bonus for a window after an EMISSION_TOGGLED -> true;
- the market intercept moved to the overlay throttle; drain-timing exits are the planner's.

Cadence: decide every 300 blocks. Wake on EPOCH_DRAIN, EMISSION_TOGGLED, DEREGISTERED, LARGE_FLOW,
OWNER_POSITION_CHANGED, OWNER_CHANGED, AUTOLOCK_TOGGLED, TAKE_CHANGED, DIVIDEND_MEMBERSHIP and PRUNE_TARGET_CHANGED.
A wake run whose events do not concern a held generation (the "(held)" qualifiers) only books the events and returns
the last signals. valid_from_block = 8,765,684 (rank-32 gate); min_cadence_blocks = 60.

Approximations (documented deviations):
- C-X2 uses Feat.flow_z_1d as the robust z of the flow crash (no 6-hour z is published); flow_6h is read from the store.
- C-U6 "positive d ln I in >= 90 % of the last 20 epochs" is computed from the store's FULL snapshots (the epoch's last
  observation). Unobserved epochs count as non-positive (fail closed).
- C-U7 "owner_sold over 7,200 blocks" sums protocol.derive.owner_position_delta over the 60-block grid.
- C-U11: an unread escrow (None) counts as E = 0, as in the overlay floor, and the signal carries "C-U11.escrow_unknown".
- w_max applies to the nominal sleeve budget G_MAX * NAV_liq * budget_ppm (TickContext carries no capital figure;
  G_MAX is the book's RiskCfg.g_max_ppm).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final

from ..core.config import ExecCfg, RiskCfg
from ..core.events import ChainEvent, ChainEventKind
from ..core.fixed import DEC, to_ppm
from ..core.orders import Urgency
from ..core.protocols import TickContext
from ..core.signals import Signal, SignalKind, StrategyOutput
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import (
    BLOCKS_PER_DAY,
    FEE_DEN,
    PERQUINTILL,
    PPM,
    RAO_PER_TAO,
    Block,
    Hotkey,
    Mode,
    NetUid,
    Ppm,
    Rao,
    StrategyId,
    SubnetKey,
)
from ..core.views import Feat, RouterCandidate
from ..features.universe import UniverseRow
from ..protocol.amm import ImpactBound, SwapError, round_trip_cost_ppm, v_max, v_star
from ..protocol.calibration import Calibration, CalibrationProvider, effective_recovery
from ..protocol.ema import project_ema
from ..protocol.emission import EmissionShare, emission_vector
from ..protocol.prune import immunity_end, recovery_ratio
from ..protocol.regimes import regime
from ..protocol.sellload import SellLoadParams, sell_load
from ..protocol.yield_model import a_earn
from .base import (
    History,
    SleevePos,
    StrategyBase,
    calibration_at,
    candidate,
    days_to_blocks,
    entry_blocks,
    events_by_key,
    exit_signal,
    floor_rows,
    frac_to_ppm,
    hk_prefix,
    hotkey_by_prefix,
    p_reg_horizon,
    parse_params,
    recent_forced_exit,
    relevant_wake,
    robust_sd,
    sleeve_budget_rao,
    sleeve_positions,
    sort_signals,
    tao_to_rao,
    target_signal,
    terciles,
    yield_hotkey,
)

CARRY_ID: Final[StrategyId] = StrategyId("carry")
K = ChainEventKind
WAKE_ON: Final[frozenset[ChainEventKind]] = frozenset({
    K.EPOCH_DRAIN, K.EMISSION_TOGGLED, K.DEREGISTERED, K.LARGE_FLOW, K.OWNER_POSITION_CHANGED, K.OWNER_CHANGED,
    K.AUTOLOCK_TOGGLED, K.TAKE_CHANGED, K.DIVIDEND_MEMBERSHIP, K.PRUNE_TARGET_CHANGED})
MIN_CADENCE_BLOCKS: Final[int] = 60
FLOW_6H_BLOCKS: Final[int] = 1_800
OWNER_WINDOW_BLOCKS: Final[int] = BLOCKS_PER_DAY
SKETCH_POINTS: Final[int] = 9                      # residual quantile sketch per (day, tercile)
PRED_MAX_NAMES: Final[int] = 48                    # predictions recorded per day (qualifiers and holdings)
SIGMA_MIN_POINTS: Final[int] = 10

PredRow = tuple[int, int, int, int, int, int, int]   # (block, netuid, reg_at, hotkey48, tercile, mu_ppm_day, lnv_e9)
ResidRow = tuple[int, int, tuple[int, ...]]          # (day_block, tercile, residual sketch in ppm/day)


def valid_from() -> Block:
    return regime("gate_rank32").first_block


# ------------------------------------------------------------------------------------------------- params
@dataclass(frozen=True, slots=True)
class CarryParams:
    """Section 2.1 parameters (names follow config/preregistration.toml [carry.params] / [carry.universe] /
    [carry.exits] / [carry.model]). P&L-tuned (budget 3): h_eval_days, mu_in_pct_day, unc_z."""
    h_eval_days: float = 5.0
    mu_in_pct_day: float = 0.10
    unc_z: float = 0.5
    mu_out_pct_day: float = -0.05
    phi_owner: float = 0.8
    phi_miner: float = 0.6
    kappa_basket_per_day: float = 0.02
    kappa_e: float = 0.7
    yield_haircut: float = 0.10
    kappa_p: float = 4.0
    r_default: float = 0.35
    min_age_start_days: float = 45.0
    t_min_tao: float = 500.0
    t_max_tao: float = 8_000.0
    micro_only: bool = False
    micro_only_t_max_tao: float = 3_000.0
    v_cap_frac: float = 0.010
    s_max_frac: float = 0.010
    w_max_frac: float = 0.12
    n_max: int = 12
    v_min_tao: float = 1.0
    rank_in: int = 8
    rho_in: float = 2.0
    rank_exit: int = 3
    rho_exit: float = 1.3
    burn_max: float = 0.20
    allow_burn_bucket: bool = False
    burn_bucket_max: float = 0.5
    owner_exit_frac_per_1800_blocks: float = 0.005
    owner_sold_max_frac_7200_blocks: float = 0.005
    owner_quiet_days: float = 7.0
    flow_exit_6h_frac: float = -0.02
    flow_exit_z: float = -3.5
    large_flow_outflow_frac: float = 0.02
    flow_1d_min_frac: float = -0.005
    flow_z_1d_min: float = -1.0
    escrow_frac_max: float = 0.25
    dd_stop_frac: float = 0.20
    min_hold_days: float = 2.0
    max_hold_days: float = 21.0
    rebalance_blocks: int = 300
    router_take_max: float = 0.05
    router_membership_min: float = 0.90
    router_positive_dlni_frac_min: float = 0.90
    router_realised_net_yield_min_pct_day: float = 0.10
    router_epochs: int = 20
    risk_exit_cooldown_days: float = 3.0
    hotkey_loss_epochs: int = 2
    prune_window_lead_blocks: int = 1_800
    decay_evaluations: int = 2
    rank_buffer: int = 3
    trade_band_frac: float = 0.25
    sigma_d_floor: float = 0.03
    sigma_model_prior_pct_day: float = 0.30
    sigma_model_window_days: float = 30.0
    sigma_model_min_days: float = 30.0
    residual_horizon_days: float = 5.0
    a_earn_trailing_growth_days: float = 14.0
    a_earn_growth_clip_pct_day: float = 3.0
    sell_load_on: bool = True
    # --- deferred modules: OFF until T2b + the T9 ablation (forward CB, gate tilt), T3 (flow terms), T7a (re-enable)
    forward_cb_sim: bool = False
    forward_cb_blocks: int = 2 * BLOCKS_PER_DAY
    forward_cb_step_blocks: int = 1_800
    gate_tilt: bool = False
    gate_tilt_below: int = 10
    gate_tilt_above: int = 13
    gate_tilt_pct_day: float = 0.05
    flow_a1: float = 0.0
    flow_a2: float = 0.0
    emission_reenable: bool = False
    reenable_window_days: float = 7.0
    reenable_bonus_pct_day: float = 0.05

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.h_eval_days <= 0 or self.residual_horizon_days <= 0:
            out.append("h_eval_days and residual_horizon_days must be > 0")
        if not 0 < self.t_min_tao <= self.t_max_tao:
            out.append("need 0 < t_min_tao <= t_max_tao")
        for name in ("v_cap_frac", "s_max_frac", "w_max_frac", "yield_haircut", "trade_band_frac"):
            v = getattr(self, name)
            if not 0 <= v < 1:
                out.append(f"{name} must be in [0, 1)")
        if self.n_max < 1 or self.rebalance_blocks < 1 or self.router_epochs < 1:
            out.append("n_max, rebalance_blocks and router_epochs must be >= 1")
        if self.mu_out_pct_day > self.mu_in_pct_day:
            out.append("mu_out_pct_day must be <= mu_in_pct_day (hysteresis)")
        if self.forward_cb_step_blocks < 1 or self.forward_cb_blocks < self.forward_cb_step_blocks:
            out.append("forward_cb_blocks must be >= forward_cb_step_blocks >= 1")
        if self.v_min_tao < 0 or self.sigma_d_floor <= 0:
            out.append("v_min_tao must be >= 0 and sigma_d_floor > 0")
        return out


# ------------------------------------------------------------------------------------------------- memory
@dataclass(frozen=True, slots=True)
class CarryKeyState:
    """Per-generation memory (only kept while it carries information)."""
    key: SubnetKey
    entry_block: int | None = None          # sleeve entry or last re-underwrite (C-S3)
    decay_count: int = 0                    # C-S1: consecutive scheduled evaluations with mu_net < mu_out
    rank_out_count: int = 0                 # C-S2: consecutive scheduled evaluations out of top N_max + 3
    hk_loss_epochs: int = 0                 # C-X3: consecutive epochs without an eligible hotkey
    last_epoch_seen: int = 0
    exiting: int = 0                        # urgency of a sticky sleeve exit in progress (0 = none)
    risk_exit_block: int | None = None      # last HIGH sleeve exit (C-U9)
    owner_event_block: int | None = None    # last OWNER_CHANGED / autolock true -> false (C-U7)
    reenable_block: int | None = None       # last EMISSION_TOGGLED -> true (deferred re-enable module)


@dataclass(frozen=True, slots=True)
class CarryMemory:
    keys: tuple[CarryKeyState, ...] = ()
    preds: tuple[PredRow, ...] = ()
    resid: tuple[ResidRow, ...] = ()
    resid_first_day: tuple[int, int, int] = (-1, -1, -1)   # first residual day per size tercile (-1 = none yet)
    last_sched: int | None = None
    last_pred_day: int | None = None
    last_signals: tuple[Signal, ...] = ()


# ------------------------------------------------------------------------------------------------- evaluation rows
@dataclass(frozen=True, slots=True)
class CarryRow:
    """One generation's carry evaluation (diagnostics; not journaled)."""
    key: SubnetKey
    hotkey: Hotkey | None
    y_real: float
    y_f: float
    growth_alpha_day: float
    cb_push: float
    sell_push: float
    struct: float
    p_reg: float
    p_bottom: float
    hazard_day: float
    recovery: float
    prune_loss_day: float
    extra_day: float
    mu: float
    sigma_model: float
    mu_lcb: float
    alpha_h_ppm: int
    v_star_rao: int
    v_rao: int
    rt_ppm: int | None
    mu_net: float
    rank_score: float | None
    tercile: int
    failed: tuple[str, ...]
    flags: tuple[str, ...] = ()

    @property
    def qualifies(self) -> bool:
        return not self.failed


@dataclass(frozen=True, slots=True)
class CarryEval:
    block: Block
    rows: Mapping[SubnetKey, CarryRow] = field(default_factory=dict)
    signals: tuple[Signal, ...] = ()
    memory: CarryMemory = CarryMemory()


def _pct(x: float) -> float:
    return x / 100.0


# ------------------------------------------------------------------------------------------------- strategy
class CarryStrategy(StrategyBase):
    """Section 2.1 carry MVP. Pure; state in CarryMemory."""

    def __init__(self, params: CarryParams | Mapping[str, object] | None = None, *,
                 strategy_id: StrategyId = CARRY_ID, exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None,
                 calibration: CalibrationProvider | None = None) -> None:
        p = params if isinstance(params, CarryParams) else parse_params(CarryParams, params, where=str(strategy_id))
        super().__init__(strategy_id=strategy_id, decide_every_blocks=p.rebalance_blocks, wake_on=WAKE_ON,
                         min_cadence_blocks=MIN_CADENCE_BLOCKS, valid_from_block=valid_from(), declares_dilution=True)
        self.params = p
        self.exec_cfg = exec_cfg if exec_cfg is not None else ExecCfg()
        self.risk = risk if risk is not None else RiskCfg()
        self.calibration = calibration

    def initial_memory(self) -> CarryMemory:
        return CarryMemory()

    # ---------------------------------------------------------------------------------------------- entry point
    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        mem = memory if isinstance(memory, CarryMemory) else self.initial_memory()
        held = sleeve_positions(ctx, self.id)
        scheduled = self.scheduled(int(ctx.block), mem.last_sched)
        if not scheduled and not relevant_wake(ctx.events, held, self.wake_on):
            states = self._book_events(ctx, {s.key: s for s in mem.keys})
            return StrategyOutput(mem.last_signals, replace(mem, keys=_pack(states)))
        ev = self.evaluate(ctx, mem, held=held, scheduled=scheduled)
        return StrategyOutput(ev.signals, ev.memory)

    # ---------------------------------------------------------------------------------------------- evaluation
    def evaluate(self, ctx: TickContext, memory: CarryMemory | None = None, *,
                 held: Mapping[SubnetKey, SleevePos] | None = None, scheduled: bool = True) -> CarryEval:
        """Full evaluation: every row, the decisions, the signals and the next memory."""
        p = self.params
        mem = memory if memory is not None else self.initial_memory()
        b = int(ctx.block)
        hold = dict(held) if held is not None else sleeve_positions(ctx, self.id)
        cal = calibration_at(self.calibration, ctx.block)
        hist = History(ctx)
        states = self._book_events(ctx, {s.key: s for s in mem.keys})
        rows = self._rows(ctx, mem, hist, cal, hold, states)
        budget = sleeve_budget_rao(ctx, int(self.risk.g_max_ppm))
        h_blocks = days_to_blocks(p.h_eval_days)
        v_min = tao_to_rao(p.v_min_tao)
        ev_keys = events_by_key(ctx.events)
        signals: list[Signal] = []

        # ranking over qualifiers and holdings (C-S2, C-S3, entries)
        pool = [r for r in rows.values() if r.rank_score is not None and (r.qualifies or r.key in hold)]
        pool.sort(key=lambda r: (-(r.rank_score or 0.0), r.key))
        rank_of = {r.key: i + 1 for i, r in enumerate(pool)}

        kept: list[SubnetKey] = []
        for key in sorted(hold):
            pos = hold[key]
            st = states.get(key) or CarryKeyState(key=key)
            if st.entry_block is None:
                st = replace(st, entry_block=int(pos.opened_block))
            row = rows.get(key)
            if row is None:            # generation gone or no features: the overlay owns dissolution
                states[key] = st
                continue
            st, sig = self._hold_decision(ctx, hist, row, pos, st, rank_of, ev_keys.get(key, ()), scheduled, budget,
                                          h_blocks, v_min)
            states[key] = st
            signals.append(sig)
            if st.exiting == 0:
                kept.append(key)

        # entries: top qualifiers by rank score while slots remain (scheduled evaluations only; a wake run keeps the
        # entry targets of the last scheduled evaluation that have not filled yet)
        slots = p.n_max - len(kept)
        if not scheduled:
            carried = [s for s in mem.last_signals if s.key not in hold and s.kind is SignalKind.TARGET]
            signals.extend(carried[:max(slots, 0)])
            slots = 0
        for r in pool:
            if slots <= 0:
                break
            if r.key in hold or not r.qualifies or rank_of[r.key] > p.n_max:
                continue
            signals.append(target_signal(self.id, r.key, ctx.block, value_rao=r.v_rao, budget_rao=budget, edge_day=r.mu_lcb,
                                         alpha_h=r.mu_lcb * p.h_eval_days, horizon_blocks=h_blocks, declares_dilution=True,
                                         reasons=("carry.entry",) + r.flags, hotkey=r.hotkey))
            slots -= 1

        # memory: drop flat keys' position state; keep event / exit history while it matters
        for key in sorted(states):
            st = states[key]
            if key not in hold:
                st = replace(st, entry_block=None, decay_count=0, rank_out_count=0, hk_loss_epochs=0, exiting=0)
            states[key] = st
        new_mem = replace(mem, keys=_pack(self._trim(states, b)))
        new_mem = self._track_residuals(ctx, new_mem, rows, hold, hist)
        sigs = sort_signals(signals)
        new_mem = replace(new_mem, last_signals=sigs, last_sched=b if scheduled else mem.last_sched)
        return CarryEval(block=ctx.block, rows=rows, signals=sigs, memory=new_mem)

    # ---------------------------------------------------------------------------------------------- per-key rows
    def _rows(self, ctx: TickContext, mem: CarryMemory, hist: History, cal: Calibration | None,
              hold: Mapping[SubnetKey, SleevePos], states: Mapping[SubnetKey, CarryKeyState]) -> dict[SubnetKey, CarryRow]:
        p = self.params
        raw, frame = ctx.raw, ctx.frame
        b = int(ctx.block)
        glob = raw.glob
        keys = sorted(k for k in frame.feats if raw.get(k) is not None)
        floor = floor_rows(ctx, self.risk)
        h_blocks = days_to_blocks(p.h_eval_days)
        p_reg = p_reg_horizon(ctx, cal, h_blocks)
        kappa_p = float(cal.kappa_p) if cal is not None else p.kappa_p
        r_def = cal.r_default if cal is not None else Decimal(repr(p.r_default))
        phi = cal.phi if cal is not None else SellLoadParams(
            Ppm(frac_to_ppm(p.phi_owner)), Ppm(frac_to_ppm(p.phi_miner)), Ppm(frac_to_ppm(p.kappa_basket_per_day)))
        fwd = self._forward_cb(raw) if p.forward_cb_sim else {}
        trail_days = p.a_earn_trailing_growth_days
        trail_snap = hist.at_or_before(b - days_to_blocks(trail_days))
        size_terc = terciles({k: frame.feats[k].pool_tao for k in keys})
        sigma_by_t = {t: self._sigma_model(mem, t, b) for t in (0, 1, 2)}
        budget = sleeve_budget_rao(ctx, int(self.risk.g_max_ppm))
        tx = int(self.exec_cfg.buy_tx_fee_rao) + int(self.exec_cfg.sell_tx_fee_rao)
        v_min = tao_to_rao(p.v_min_tao)
        out: dict[SubnetKey, CarryRow] = {}
        for k in keys:
            f = frame.feats[k]
            s = raw.get(k)
            sv = ctx.view.get(k) or s
            if s is None or sv is None:
                continue
            h = yield_hotkey(ctx, f)
            cand = candidate(f, h)
            y_real = cand.score_ppm_day / PPM if cand is not None else 0.0

            # yield forecast
            a0 = f.a_earn_alpha
            g = f.a_earn_growth_day * a0
            if f.flow_1d is not None and f.spot > 0:
                g += p.kappa_e * max(f.flow_1d * f.pool_tao / f.spot, 0.0)
            if trail_snap is not None and a0 > 0:
                old = trail_snap.get(k)
                a_old = int(a_earn(old)) / RAO_PER_TAO if old is not None else 0.0
                days = (b - int(trail_snap.block)) / BLOCKS_PER_DAY
                if a_old > 0 and days > 0:
                    g_tr = (a0 / a_old - 1.0) / days
                    if g_tr * a0 > g:
                        g = min(g_tr, _pct(p.a_earn_growth_clip_pct_day)) * a0
            denom = a0 + g * p.h_eval_days / 2.0
            y_f = (y_real * a0 / denom if denom > 0 else 0.0) * (1.0 - p.yield_haircut)

            # structure
            cb_push = fwd.get(k, f.cb_push_day) if p.forward_cb_sim else f.cb_push_day
            sell_push = 0.0
            if p.sell_load_on:
                share = EmissionShare(key=k, b=Decimal(0), keep=Decimal(0), final=Decimal(0), tao_per_block=Rao(0),
                                      tao_in_per_block=Rao(0), chain_buy_per_block=Rao(0))
                sell_push = float(sell_load(s, glob, share, phi, raw.block, frame.emission.root_flag).sell_push_day)
            struct = cb_push - sell_push

            # prune hazard and recovery
            p_bottom = 0.0
            if frame.prune.prune_possible and not (f.immune and int(f.immune_until) > b + h_blocks):
                p_bottom = 1.0 if f.rho is None else min(1.0, _exp(-kappa_p * (f.rho - 1.0)))
            hazard = p_reg * p_bottom / p.h_eval_days
            r_formula = recovery_ratio(s, glob, r_def)
            rec = float(effective_recovery(r_formula, cal)) if cal is not None else float(r_formula)
            loss = hazard * (1.0 - rec)

            st = states.get(k)
            extra = self._extra_terms(f, raw, st, b)
            mu = y_f + struct - loss + extra
            terc = size_terc.get(k, 0)
            sigma = sigma_by_t[terc]
            mu_lcb = mu - p.unc_z * sigma

            # sizing on the view pool
            pool = sv.pool
            alpha_h_ppm = to_ppm(mu_lcb * p.h_eval_days)
            v0 = int(v_star(pool, alpha_h_ppm))
            vs = int(v_star(pool, alpha_h_ppm - (-(-tx * PPM // v0)))) if v0 > 0 else 0
            caps = [vs, int(pool.tao) * frac_to_ppm(p.v_cap_frac) // PPM, int(v_max(pool.tao, frac_to_ppm(p.s_max_frac))),
                    budget * frac_to_ppm(p.w_max_frac) // PPM]
            v = max(min(caps), 0)
            flags: list[str] = []
            burn = float(s.miner_burned)
            if p.allow_burn_bucket and p.burn_max <= burn <= p.burn_bucket_max:
                v //= 2
                flags.append("C-U5.burn_bucket_half")
            pos = hold.get(k)
            rt_size = v if v > 0 else (int(pos.value_rao) if pos is not None and pos.value_rao > 0 else v_min)
            rt: int | None
            try:
                rt = round_trip_cost_ppm(pool, Rao(rt_size), ImpactBound.TEMPORARY, tx) if rt_size > 0 else None
            except SwapError:
                rt = None
            mu_net = mu_lcb - (rt / PPM / p.h_eval_days if rt is not None else 0.0)
            rank_score = mu_net / max(f.sigma_d, p.sigma_d_floor) if f.sigma_d is not None else None

            failed = self._entry_failures(ctx, f, s, k, cand, h, hist, floor.get(k), st, v, mu_net, rt, v_min, flags)
            out[k] = CarryRow(key=k, hotkey=h, y_real=y_real, y_f=y_f, growth_alpha_day=g, cb_push=cb_push,
                              sell_push=sell_push, struct=struct, p_reg=p_reg, p_bottom=p_bottom, hazard_day=hazard,
                              recovery=rec, prune_loss_day=loss, extra_day=extra, mu=mu, sigma_model=sigma, mu_lcb=mu_lcb,
                              alpha_h_ppm=alpha_h_ppm, v_star_rao=vs, v_rao=v, rt_ppm=rt, mu_net=mu_net,
                              rank_score=rank_score, tercile=terc, failed=failed, flags=tuple(flags))
        return out

    def _entry_failures(self, ctx: TickContext, f: Feat, s: SubnetState, k: SubnetKey, c: RouterCandidate | None,
                        h: Hotkey | None, hist: History, floor: UniverseRow | None, st: CarryKeyState | None, v: int,
                        mu_net: float, rt: int | None, v_min: int, flags: list[str]) -> tuple[str, ...]:
        """C-U1..C-U11, the overlay floor, book cooldowns and the size / hurdle rules. Store-backed checks run only
        when every cheap rule passed."""
        p = self.params
        b = int(ctx.block)
        frame = ctx.frame
        out: list[str] = []
        if floor is None:
            out.append("floor.missing")
        elif floor.failed:
            out.append("floor." + "".join(floor.failed))
        out.extend(entry_blocks(ctx, k))
        # C-U1
        if s.first_emission_block is None or not s.subtoken_enabled or not s.reg_allowed or not s.emission_enabled:
            out.append("C-U1")
        # C-U2
        if f.since_start_blocks is None or f.since_start_blocks < days_to_blocks(p.min_age_start_days):
            out.append("C-U2")
        # C-U3
        y = int(s.pool.tao)
        t_max = min(p.t_max_tao, p.micro_only_t_max_tao) if p.micro_only else p.t_max_tao
        if y < tao_to_rao(p.t_min_tao) or y > tao_to_rao(t_max) or (p.micro_only and y >= tao_to_rao(t_max)):
            out.append("C-U3.pool")
        if rt is None:
            out.append("C-U3.unquotable")
        if v * PPM > frac_to_ppm(p.v_cap_frac) * y:
            out.append("C-U3.v_cap")
        # C-U4 (cheap part)
        if k == frame.prune.target:
            out.append("C-U4.target")
        elif f.prune_rank is not None and (f.prune_rank < p.rank_in or f.rho is None or f.rho < p.rho_in):
            out.append("C-U4.ladder")
        # C-U5
        burn = float(s.miner_burned)
        if burn >= p.burn_max and not (p.allow_burn_bucket and burn <= p.burn_bucket_max):
            out.append("C-U5")
        # C-U6 (cheap part)
        if c is None:
            out.append("C-U6.no_hotkey")
        else:
            if not c.eligible:
                out.append("C-U6.ineligible")
            if c.take_u16 * PPM > frac_to_ppm(p.router_take_max) * FEE_DEN:
                out.append("C-U6.take")
            if c.member_frac_ppm < frac_to_ppm(p.router_membership_min):
                out.append("C-U6.membership")
            if c.score_ppm_day < frac_to_ppm(_pct(p.router_realised_net_yield_min_pct_day)):
                out.append("C-U6.realised_yield")
            if not c.ratio_ok:
                out.append("C-U6.ratio")
        # C-U7 (cheap part)
        quiet = days_to_blocks(p.owner_quiet_days)
        if st is not None and st.owner_event_block is not None and st.owner_event_block > b - quiet:
            out.append("C-U7.owner_event")
        if any(kk == k and "owner" in rule and int(until) > b for kk, rule, until in ctx.book_view.cooldowns):
            out.append("C-U7.owner_cooldown")
        # C-U8
        if ctx.mode is not Mode.NORMAL or not frame.emission.model_ok:
            out.append("C-U8")
        # C-U9
        window = days_to_blocks(p.risk_exit_cooldown_days)
        if recent_forced_exit(ctx, k, window) or (st is not None and st.risk_exit_block is not None
                                                   and st.risk_exit_block > b - window):
            out.append("C-U9")
        # C-U10
        if f.flow_1d is None or f.flow_1d < p.flow_1d_min_frac or f.flow_z_1d is None or f.flow_z_1d < p.flow_z_1d_min:
            out.append("C-U10")
        # C-U11
        if f.escrow_frac is None:
            flags.append("C-U11.escrow_unknown")
        elif f.escrow_frac > p.escrow_frac_max:
            out.append("C-U11")
        # ranking and size
        if f.sigma_d is None:
            out.append("rank.sigma_d")
        if mu_net < _pct(p.mu_in_pct_day):
            out.append("mu_net<mu_in")
        if v < v_min or v <= 0:
            out.append("V<V_min")
        if out:
            return tuple(out)
        # ---- store-backed checks (only for otherwise-qualifying names)
        if c is not None and h is not None:
            rets = hist.epoch_index_returns(k, h, p.router_epochs)
            pos_frac = sum(1 for r in rets if r > 0) / p.router_epochs
            if pos_frac < p.router_positive_dlni_frac_min:
                out.append("C-U6.positive_dlni")
        sold = hist.owner_sold_alpha(k, OWNER_WINDOW_BLOCKS)
        if sold is not None and s.pool.alpha > 0 and s.pool.w_quote_e18 > 0:
            tao_eq = sold / int(s.pool.alpha) * (s.pool.w_base_e18 / s.pool.w_quote_e18)
            if tao_eq > p.owner_sold_max_frac_7200_blocks:
                out.append("C-U7.owner_sold")
        old = hist.subnet_ago(k, quiet, max_stale=quiet)
        if old is not None:
            o = old[1]
            if (o.owner_coldkey, o.owner_hotkey) != (s.owner_coldkey, s.owner_hotkey) or (
                    o.owner_cut_autolock is True and s.owner_cut_autolock is False):
                out.append("C-U7.owner_change_7d")
        if f.immune and int(f.immune_until) <= b + days_to_blocks(p.h_eval_days + 3.0):
            rank, rho = _projected_at_expiry(ctx.raw, s)
            if rank is None or rank < p.rank_in or rho is None or rho < p.rho_in:
                out.append("C-U4.immune_expiry")
        return tuple(out)

    # ---------------------------------------------------------------------------------------------- holdings
    def _hold_decision(self, ctx: TickContext, hist: History, row: CarryRow, pos: SleevePos, st: CarryKeyState,
                       rank_of: Mapping[SubnetKey, int], evs: tuple[ChainEvent, ...], scheduled: bool, budget: int,
                       h_blocks: int, v_min: int) -> tuple[CarryKeyState, Signal]:
        p = self.params
        b = int(ctx.block)
        f = ctx.frame.feats[row.key]
        s = ctx.raw.get(row.key)
        high: list[str] = []
        normal: list[str] = []
        # C-X1 owner distribution
        owner_tao = (f.owner_sold_6h_frac * (s.pool.w_base_e18 / s.pool.w_quote_e18)
                     if f.owner_sold_6h_frac is not None and s is not None and s.pool.w_quote_e18 > 0 else None)
        if owner_tao is not None and owner_tao > p.owner_exit_frac_per_1800_blocks:
            high.append("C-X1.owner_sold")
        if any(e.kind is K.OWNER_CHANGED for e in evs):
            high.append("C-X1.owner_changed")
        if any(e.kind is K.AUTOLOCK_TOGGLED and e.flag is False for e in evs):
            high.append("C-X1.autolock_off")
        # C-X2 flow crash
        f6 = hist.flow_frac(row.key, FLOW_6H_BLOCKS)
        if f6 is not None and f6 < p.flow_exit_6h_frac and f.flow_z_1d is not None and f.flow_z_1d <= p.flow_exit_z:
            high.append("C-X2.flow_crash")
        lf_ppm = frac_to_ppm(p.large_flow_outflow_frac)
        if any(e.kind is K.LARGE_FLOW and (e.amount or 0) < 0 and abs(e.frac_ppm or 0) >= lf_ppm for e in evs):
            high.append("C-X2.large_outflow")
        # C-X4 thesis stop (executable value vs cost basis)
        if pos.cost_rao > 0 and pos.value_rao * PPM <= pos.cost_rao * (PPM - frac_to_ppm(p.dd_stop_frac)):
            high.append("C-X4.dd_stop")
        # C-X5 stricter prune
        pv = ctx.frame.prune
        window = pv.window_open or pv.blocks_to_window <= p.prune_window_lead_blocks
        if window and f.prune_rank is not None and (
                f.prune_rank <= p.rank_exit or (f.rho is not None and f.rho <= p.rho_exit)):
            high.append("C-X5.prune")
        # C-X3 hotkey loss (counted per epoch)
        if s is not None and int(s.last_epoch_block) != st.last_epoch_seen:
            rh = ctx.book_view.router.hotkey(row.key)
            rc = candidate(f, rh)
            ok = (rc is not None and rc.eligible) or f.best_candidate is not None
            st = replace(st, last_epoch_seen=int(s.last_epoch_block), hk_loss_epochs=0 if ok else st.hk_loss_epochs + 1)
        if st.hk_loss_epochs >= p.hotkey_loss_epochs:
            normal.append("C-X3.hotkey_loss")
        # C-S1 / C-S2 counters (scheduled evaluations only)
        if scheduled:
            st = replace(st, decay_count=st.decay_count + 1 if row.mu_net < _pct(p.mu_out_pct_day) else 0)
            rank = rank_of.get(row.key)
            out_of_rank = (rank is None or rank > p.n_max + p.rank_buffer) and row.mu_net < _pct(p.mu_in_pct_day)
            st = replace(st, rank_out_count=st.rank_out_count + 1 if out_of_rank else 0)
        held_blocks = b - (st.entry_block if st.entry_block is not None else int(pos.opened_block))
        if st.decay_count >= p.decay_evaluations and held_blocks >= days_to_blocks(p.min_hold_days):
            normal.append("C-S1.decay")
        if st.rank_out_count >= p.decay_evaluations:
            normal.append("C-S2.rank")
        reasons: tuple[str, ...] = ()
        if held_blocks >= days_to_blocks(p.max_hold_days) and not high and not normal and st.exiting == 0:
            if row.qualifies and rank_of.get(row.key, p.n_max + 1) <= p.n_max:
                st = replace(st, entry_block=b)
                reasons = ("C-S3.reunderwritten",)
            else:
                normal.append("C-S3.max_hold")

        if high or normal or st.exiting:
            urg = Urgency.HIGH if high else Urgency.NORMAL
            if st.exiting:
                urg = max(urg, Urgency(st.exiting))
            if high:
                st = replace(st, risk_exit_block=b)
            why = tuple(high + normal) or ("carry.exit_in_progress",)
            st = replace(st, exiting=int(urg))
            return st, exit_signal(self.id, row.key, ctx.block, urg, ("carry.exit",) + why)

        # hold / re-size
        held_v = int(pos.value_rao)
        if row.qualifies and row.v_rao > 0:
            target = row.v_rao
        elif row.v_rao > 0:
            target = min(row.v_rao, held_v)
        else:
            target = held_v
        band = max(v_min, int(target * p.trade_band_frac))
        if abs(target - held_v) < band:
            target = held_v
        return st, target_signal(self.id, row.key, ctx.block, value_rao=target, budget_rao=budget, edge_day=row.mu_lcb,
                                 alpha_h=row.mu_lcb * p.h_eval_days, horizon_blocks=h_blocks, declares_dilution=True,
                                 reasons=("carry.hold",) + reasons + row.flags, hotkey=row.hotkey)

    # ---------------------------------------------------------------------------------------------- memory helpers
    def _book_events(self, ctx: TickContext, states: dict[SubnetKey, CarryKeyState]) -> dict[SubnetKey, CarryKeyState]:
        """Owner events (C-U7) and re-enables (deferred module) are booked on every run, light or full."""
        b = int(ctx.block)
        out = dict(states)
        for e in ctx.events:
            if e.key is None:
                continue
            st = out.get(e.key) or CarryKeyState(key=e.key)
            if e.kind is K.OWNER_CHANGED or (e.kind is K.AUTOLOCK_TOGGLED and e.flag is False):
                out[e.key] = replace(st, owner_event_block=b)
            elif e.kind is K.EMISSION_TOGGLED and e.flag is True and self.params.emission_reenable:
                out[e.key] = replace(st, reenable_block=b)
        return out

    def _trim(self, states: Mapping[SubnetKey, CarryKeyState], b: int) -> dict[SubnetKey, CarryKeyState]:
        p = self.params
        keep_risk = days_to_blocks(p.risk_exit_cooldown_days)
        keep_owner = days_to_blocks(p.owner_quiet_days)
        keep_reenable = days_to_blocks(p.reenable_window_days)
        out: dict[SubnetKey, CarryKeyState] = {}
        for k in sorted(states):
            st = states[k]
            if st.risk_exit_block is not None and st.risk_exit_block <= b - keep_risk:
                st = replace(st, risk_exit_block=None)
            if st.owner_event_block is not None and st.owner_event_block <= b - keep_owner:
                st = replace(st, owner_event_block=None)
            if st.reenable_block is not None and st.reenable_block <= b - keep_reenable:
                st = replace(st, reenable_block=None)
            if st == CarryKeyState(key=k, last_epoch_seen=st.last_epoch_seen) and st.entry_block is None:
                continue
            out[k] = st
        return out

    # ---------------------------------------------------------------------------------------------- sigma_model
    def _sigma_model(self, mem: CarryMemory, tercile: int, b: int) -> float:
        p = self.params
        prior = _pct(p.sigma_model_prior_pct_day)
        first = mem.resid_first_day[tercile]
        if first < 0 or b - first < days_to_blocks(p.sigma_model_min_days):
            return prior
        lo = b - days_to_blocks(p.sigma_model_window_days)
        pts = [x for day, t, sk in mem.resid if t == tercile and day > lo for x in sk]
        if len(pts) < SIGMA_MIN_POINTS:
            return prior
        return robust_sd([x / PPM for x in pts])

    def _track_residuals(self, ctx: TickContext, mem: CarryMemory, rows: Mapping[SubnetKey, CarryRow],
                         hold: Mapping[SubnetKey, SleevePos], hist: History) -> CarryMemory:
        """Mature predictions older than the residual horizon into per-(day, tercile) residual sketches; record one
        prediction per day for every qualifier and holding (mu of the evaluation, ln P*I of the yield hotkey)."""
        p = self.params
        raw = ctx.raw
        b = int(ctx.block)
        horizon = days_to_blocks(p.residual_horizon_days)
        pending: list[PredRow] = []
        groups: dict[tuple[int, int], list[int]] = {}
        for pr in mem.preds:
            pb, netuid, reg_at, hk48, terc, mu_ppm, lnv0 = pr
            if b - pb < horizon:
                pending.append(pr)
                continue
            s = raw.get(SubnetKey(NetUid(netuid), Block(reg_at)))
            lnv1 = _lnv(s, hk48)
            if lnv1 is None:
                continue
            days = (b - pb) / BLOCKS_PER_DAY
            realised = (lnv1 - lnv0) / 1e9 / days
            groups.setdefault((pb // BLOCKS_PER_DAY * BLOCKS_PER_DAY, terc), []).append(
                round((realised - mu_ppm / PPM) * PPM))
        resid = list(mem.resid)
        first = list(mem.resid_first_day)
        for (day, terc), vals in sorted(groups.items()):
            old = [r for r in resid if r[0] == day and r[1] == terc]
            merged = sorted(vals + [x for r in old for x in r[2]])
            resid = [r for r in resid if not (r[0] == day and r[1] == terc)] + [(day, terc, _sketch(merged))]
            if first[terc] < 0:
                first[terc] = day
        lo = b - days_to_blocks(p.sigma_model_window_days)
        resid = sorted(r for r in resid if r[0] > lo)
        day_idx = b // BLOCKS_PER_DAY
        last_day = mem.last_pred_day
        if last_day is None or day_idx > last_day:
            chosen = sorted((r for r in rows.values() if r.qualifies or r.key in hold), key=lambda r: r.key)[:PRED_MAX_NAMES]
            for r in chosen:
                s = raw.get(r.key)
                if r.hotkey is None or s is None:
                    continue
                hk48 = hk_prefix(r.hotkey)
                lnv = _lnv(s, hk48)
                if lnv is None:
                    continue
                pending.append((b, int(r.key.netuid), int(r.key.reg_at), hk48, r.tercile, to_ppm(r.mu), lnv))
            last_day = day_idx
        return replace(mem, preds=tuple(sorted(pending)), resid=tuple(resid),
                       resid_first_day=(first[0], first[1], first[2]), last_pred_day=last_day)

    # ---------------------------------------------------------------------------------------------- deferred modules
    def _extra_terms(self, f: Feat, raw: ChainSnapshot, st: CarryKeyState | None, b: int) -> float:
        p = self.params
        extra = 0.0
        if p.gate_tilt and f.burn_adj_rank is not None:
            n = raw.glob.gate_rank
            if n - p.gate_tilt_below <= f.burn_adj_rank <= n + p.gate_tilt_above:
                extra += _pct(p.gate_tilt_pct_day)
        if p.flow_a1 != 0.0 and f.flow_1d is not None:
            extra += p.flow_a1 * f.flow_1d
        if p.flow_a2 != 0.0 and f.flow_7d is not None:
            extra += p.flow_a2 * f.flow_7d / 7.0
        if p.emission_reenable and st is not None and st.reenable_block is not None and \
                b - st.reenable_block <= days_to_blocks(p.reenable_window_days):
            extra += _pct(p.reenable_bonus_pct_day)
        return extra

    def _forward_cb(self, raw: ChainSnapshot) -> dict[SubnetKey, float]:
        """Forward chain-buy push: mean modelled chain buy over the next forward_cb_blocks with every EMA following its
        flat-spot forecast (protocol.ema.project_ema), as k_w * CB/day / y."""
        p = self.params
        glob = raw.glob
        totals: dict[SubnetKey, int] = {s.key: 0 for s in raw.subnets}
        n = 0
        for dn in range(p.forward_cb_step_blocks, p.forward_cb_blocks + 1, p.forward_cb_step_blocks):
            override = {s.key: project_ema(glob, s, raw.block, dn) for s in raw.subnets}
            for key, sh in sorted(emission_vector(raw, ema_override=override).items()):
                totals[key] = totals.get(key, 0) + int(sh.chain_buy_per_block)
            n += 1
        out: dict[SubnetKey, float] = {}
        for s in raw.subnets:
            if n == 0 or s.pool.px_tao <= 0 or s.pool.w_base_e18 <= 0:
                continue
            k_w = PERQUINTILL / s.pool.w_base_e18
            out[s.key] = k_w * (totals[s.key] / n) * BLOCKS_PER_DAY / int(s.pool.px_tao)
        return out


# ------------------------------------------------------------------------------------------------- helpers
def _exp(x: float) -> float:
    """exp through the fixed Decimal context (bit-identical across OSes, unlike libm)."""
    return float(DEC.exp(Decimal(repr(x))))


def _pack(states: Mapping[SubnetKey, CarryKeyState]) -> tuple[CarryKeyState, ...]:
    return tuple(states[k] for k in sorted(states))


def _sketch(sorted_vals: list[int]) -> tuple[int, ...]:
    n = len(sorted_vals)
    if n <= SKETCH_POINTS:
        return tuple(sorted_vals)
    return tuple(sorted_vals[round(i * (n - 1) / (SKETCH_POINTS - 1))] for i in range(SKETCH_POINTS))


def _lnv(s: SubnetState | None, hk48: int) -> int | None:
    """ln(spot * I_h) * 1e9 for the tracked hotkey with this 48-bit prefix; None if absent."""
    if s is None or s.pool.px_tao <= 0 or s.pool.px_alpha <= 0:
        return None
    h = hotkey_by_prefix(s, hk48)
    idx = s.hotkey(h) if h is not None else None
    if idx is None or idx.total_shares <= 0 or idx.total_alpha <= 0:
        return None
    v = DEC.multiply(s.pool.spot(), idx.index())
    if v <= 0:
        return None
    return round(float(DEC.ln(v)) * 1e9)


def _projected_at_expiry(snap: ChainSnapshot, s: SubnetState) -> tuple[int | None, float | None]:
    """Ladder rank and rho = EMA / bottom EMA of `s` at its immunity expiry under flat-spot EMA forecasts of every
    subnet (others admitted at their own expiry). (None, None) if nothing is prunable then."""
    glob = snap.glob
    at = int(immunity_end(s, glob))
    dn = max(at - int(snap.block), 0)
    paths: list[tuple[Decimal, int, int, SubnetKey]] = []
    for o in snap.subnets:
        if int(o.key.netuid) == 0 or at < int(immunity_end(o, glob)):
            continue
        paths.append((project_ema(glob, o, snap.block, dn), int(o.key.reg_at), int(o.key.netuid), o.key))
    paths.sort()
    keys = [x[3] for x in paths]
    if s.key not in keys or not paths:
        return None, None
    bottom = paths[0][0]
    mine = paths[keys.index(s.key)][0]
    rho = float(DEC.divide(mine, bottom)) if bottom > 0 else None
    return keys.index(s.key) + 1, rho
