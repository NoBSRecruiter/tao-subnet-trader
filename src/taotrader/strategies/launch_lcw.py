"""taotrader/strategies/launch_lcw.py - the launch carry window (LCW) sub-sleeve of (d) new-subnet launches (WP9;
DESIGN.md sections 2.4 and 3.4). Verdict EXPERIMENTAL, paper-only, behind `enabled = False` (config [lcw].enabled).
It may enter PAPER only after the offline FT1 AND FT2 pass, and it is never promoted on fewer than 20 paper trades.
With enabled = False it emits nothing, except an EXIT for anything it still holds.

Every gate failure is a machine-readable reason code. Each WATCH-window generation that fails a gate gets an AVOID
signal carrying the codes (journaled in DecisionTrace):
- lcw.U1.watch: since_start in [720, 72,000] and age_reg <= 100 d. lcw.U1.gatekeeper.<FLAG>: a Gatekeeper veto-all flag
  (UNSTARTED, SEED_ANOMALY, BURNING). EMA_WARMING and EMISSION_OFF are the LCW(-paper) exceptions;
- lcw.U2.pool: 600 <= T <= 6,000 TAO;
- lcw.U3.spot_ratio: spot / spot0 in [0.8, 1.6]. spot0 is the generation's first stored spot (ctx.store); when it is
  unknown the code is lcw.U3.spot0_unknown;
- lcw.U4.burn: the 2-epoch mean of MinerBurned <= 0.35;
- lcw.U5.*: >= 8 miner UIDs from >= 5 coldkeys, top-1 coldkey share <= 0.5, >= 2 permit coldkeys, from
  SubnetState.metagraph. lcw.U5.metagraph_missing (fail closed; section 13 Q13);
- lcw.U6.owner: autolock on, OR (owner sold over 7 d <= 50 % of the owner-cut accrual AND owner TAO out over 24 h
  <= 0.5 % of T). lcw.U6.owner_unknown (fail closed);
- lcw.U7.*: a yield hotkey (book router, else best candidate) with >= 2 epochs of positive realised d ln I and a
  marginal net yield Y_real * A0 / (A0 + a_me) >= 0.6 %/day;
- lcw.U8.*: phi_ewma >= +0.4 % of T per day and phi_6h >= 0 (net user flow per day, fractions of T);
- lcw.U9.*: mu_hat >= 0.8 %/day and G - 2f >= 3 %;
- lcw.U10.positions: at most 2 launch positions;
- lcw.book.*: book cooldowns, entry halts, dissolving keys; lcw.size: no positive size.

Yield forecast: y(tau) = AE (1 - c_o) 0.5 (1 - rp)(1 - take)(1 - ck) / (A0 + G_mech tau + kappa_e max(phi_ewma, 0) T
tau / p + a_me), averaged over tau = 1..10 d. G_mech = Feat.a_earn_growth_day * A0 is the deterministic A_earn
growth. A metagraph with zero miners is flagged lcw.miner_half_upside, an upside case that is not added to the forecast.
Score: mu_hat = 2 lam phi_ewma + y_avg - headwind - RT_TEMPORARY(V) / h, with lam = 0.5 and h = 10 d. The headwind is
the shared sell-load push (protocol.sellload; 2 p dS_alpha / T at k_w = 2). G = (2 lam phi_ewma + y_avg - headwind) * h
is the gross edge over the hold.
Sizing: V = min(T (G - 2f) / 4 (protocol.amm.v_star), V_max at s = 1.5 %, 1 % of T, 0.5 x daily organic buying
(max(phi_ewma, 0) T), 2.5 % of NAV_liq). It goes in two tranches 360 blocks apart (V/2, then V).

Exits (sleeve level; the overlay owns URGENT emission-off and the section 3.4 hard stop):
- HIGH: X1 emission disabled after it was enabled; X2 owner dump (24 h sales >= 3x accrual or >= 1 % of T, an owner
  change, autolock off); X4 stop -8 % from cost, or trailing 12 % on executable value per sleeve share.
- NORMAL: X3 MinerBurned > 0.60 for 2 epochs, or < 4 miners; X5 phi_6h <= -1 % of T per day, or phi_ewma < 0 for 2
  epochs after day 5; X6 realised yield < 0.25 %/day while mu_hat < 0; X7 hold >= 14 d or age_reg >=
  NetworkImmunityPeriod - 144,000 (read live). Also the section 3.4 rule: not emission-enabled within 21 d of start_call.

phi_ewma is the EWMA of the per-day net user flow with a half-life of phi_half_life_blocks (7,200), sampled every
epoch-sized step (360 blocks) over 3 half-lives. The design leaves this half-life open; it is a frozen choice here.
Cadence: decide every 360 blocks (one epoch). Wake on EPOCH_DRAIN, EMISSION_TOGGLED, OWNER_CHANGED, AUTOLOCK_TOGGLED,
LARGE_FLOW, OWNER_POSITION_CHANGED and DEREGISTERED. A wake run concerning neither a holding nor a WATCH-window
generation returns the last signals. valid_from_block = 8,466,531 (flows valid); min_cadence_blocks = 60.
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
from ..core.signals import Signal, StrategyOutput
from ..core.state import PoolState, SubnetState
from ..core.units import BLOCKS_PER_DAY, FEE_DEN, PPM, RAO_PER_TAO, Block, Hotkey, Rao, StrategyId, SubnetKey
from ..features.gatekeeper import VETO_ALL
from ..protocol.amm import ImpactBound, SwapError, round_trip_cost_ppm, v_max, v_star
from ..protocol.calibration import Calibration, CalibrationProvider
from ..protocol.emission import EmissionShare
from ..protocol.regimes import VALIDATOR_SHARE, regime
from ..protocol.sellload import SellLoadParams, sell_load
from ..protocol.yield_model import owner_cut_frac
from .base import (
    History,
    SleevePos,
    StrategyBase,
    avoid_signal,
    calibration_at,
    candidate,
    days_to_blocks,
    entry_blocks,
    events_by_key,
    exit_signal,
    frac_to_ppm,
    parse_params,
    relevant_wake,
    sleeve_positions,
    sort_signals,
    tao_to_rao,
    target_signal,
    yield_hotkey,
)

LCW_ID: Final[StrategyId] = StrategyId("lcw")
K = ChainEventKind
WAKE_ON: Final[frozenset[ChainEventKind]] = frozenset({
    K.EPOCH_DRAIN, K.EMISSION_TOGGLED, K.OWNER_CHANGED, K.AUTOLOCK_TOGGLED, K.LARGE_FLOW, K.OWNER_POSITION_CHANGED,
    K.DEREGISTERED})
MIN_CADENCE_BLOCKS: Final[int] = 60
EPOCH_STEP_BLOCKS: Final[int] = 360
SPOT0_SEARCH_STEPS: Final[int] = 10


def valid_from() -> Block:
    return regime("price_ema_rp").first_block


@dataclass(frozen=True, slots=True)
class LcwParams:
    """Section 2.4 LCW parameters (config/preregistration.toml [lcw])."""
    enabled: bool = False
    since_start_min_blocks: int = 720
    since_start_max_blocks: int = 72_000
    age_reg_max_days: float = 100.0
    t_min_tao: float = 600.0
    t_max_tao: float = 6_000.0
    spot_ratio_lo: float = 0.8
    spot_ratio_hi: float = 1.6
    miner_burned_2epoch_max: float = 0.35
    miner_uids_min: int = 8
    miner_coldkeys_min: int = 5
    top1_coldkey_share_max: float = 0.5
    permit_coldkeys_min: int = 2
    owner_sold_7d_max_of_accrual: float = 0.5
    owner_tao_out_24h_max_frac: float = 0.005
    router_positive_epochs_min: int = 2
    router_marginal_net_yield_min_pct_day: float = 0.6
    phi_ewma_min_frac_day: float = 0.004
    phi_6h_min: float = 0.0
    mu_hat_min_pct_day: float = 0.8
    g_minus_2f_min: float = 0.03
    positions_max: int = 2
    lam: float = 0.5
    h_days: float = 10.0
    yield_avg_days_lo: int = 1
    yield_avg_days_hi: int = 10
    kappa_e: float = 0.7
    tranche_count: int = 2
    tranche_spacing_blocks: int = 360
    size_slip_frac: float = 0.015
    pool_frac_max: float = 0.01
    organic_frac: float = 0.5
    nav_frac_max: float = 0.025
    phi_half_life_blocks: int = BLOCKS_PER_DAY
    phi_step_blocks: int = EPOCH_STEP_BLOCKS
    x2_owner_dump_accrual_mult: float = 3.0
    x2_owner_dump_t_frac_24h: float = 0.01
    x3_miner_burned: float = 0.60
    x3_burn_epochs: int = 2
    x3_min_miners: int = 4
    x4_stop_from_cost: float = -0.08
    x4_trailing: float = 0.12
    x5_phi_6h_frac_day: float = -0.01
    x5_after_days: float = 5.0
    x5_neg_epochs: int = 2
    x6_realised_yield_min_pct_day: float = 0.25
    x7_hold_days: float = 14.0
    x7_before_immunity_end_blocks: int = 144_000
    emission_enable_deadline_days: float = 21.0
    router_epochs: int = 10
    decide_every_blocks: int = EPOCH_STEP_BLOCKS

    def problems(self) -> list[str]:
        out: list[str] = []
        if not 0 <= self.since_start_min_blocks <= self.since_start_max_blocks:
            out.append("need 0 <= since_start_min_blocks <= since_start_max_blocks")
        if not 0 < self.t_min_tao <= self.t_max_tao:
            out.append("need 0 < t_min_tao <= t_max_tao")
        if not 0 < self.spot_ratio_lo <= self.spot_ratio_hi:
            out.append("need 0 < spot_ratio_lo <= spot_ratio_hi")
        if self.h_days <= 0 or self.lam < 0 or not 1 <= self.yield_avg_days_lo <= self.yield_avg_days_hi:
            out.append("need h_days > 0, lam >= 0 and 1 <= yield_avg_days_lo <= yield_avg_days_hi")
        if self.tranche_count < 1 or self.positions_max < 0 or self.decide_every_blocks < 1:
            out.append("tranche_count >= 1, positions_max >= 0, decide_every_blocks >= 1")
        if self.phi_half_life_blocks < 1 or self.phi_step_blocks < 1 or self.router_epochs < 1:
            out.append("phi_half_life_blocks, phi_step_blocks and router_epochs must be >= 1")
        for name in ("size_slip_frac", "pool_frac_max", "nav_frac_max", "x4_trailing"):
            if not 0 <= getattr(self, name) < 1:
                out.append(f"{name} must be in [0, 1)")
        return out


@dataclass(frozen=True, slots=True)
class LcwKeyState:
    key: SubnetKey
    entry_block: int | None = None
    tranche1_block: int | None = None       # first tranche proposed; the full size follows tranche_spacing_blocks later
    peak_e12: int = 0                       # peak executable value per sleeve share * 1e12
    burn_epochs: int = 0                    # consecutive epochs with MinerBurned > x3_miner_burned
    phi_neg_epochs: int = 0                 # consecutive epochs with phi_ewma < 0
    last_epoch_seen: int = 0
    was_enabled: bool = False               # emission seen enabled while tracked (X1)
    exiting: int = 0


@dataclass(frozen=True, slots=True)
class LcwMemory:
    keys: tuple[LcwKeyState, ...] = ()
    last_sched: int | None = None
    last_signals: tuple[Signal, ...] = ()


@dataclass(frozen=True, slots=True)
class LcwRow:
    key: SubnetKey
    hotkey: Hotkey | None
    phi_ewma: float | None
    phi_6h: float | None
    y_real: float
    y_avg: float
    headwind: float
    g_gross: float
    mu_hat: float
    v_rao: int
    rt_ppm: int | None
    failed: tuple[str, ...]
    flags: tuple[str, ...] = ()

    @property
    def qualifies(self) -> bool:
        return not self.failed


@dataclass(frozen=True, slots=True)
class LcwEval:
    block: Block
    rows: Mapping[SubnetKey, LcwRow] = field(default_factory=dict)
    signals: tuple[Signal, ...] = ()
    memory: LcwMemory = LcwMemory()


def _pct(x: float) -> float:
    return x / 100.0


class LcwStrategy(StrategyBase):
    """Section 2.4 LCW sub-sleeve (paper-only, flag-gated). Pure; state in LcwMemory."""

    def __init__(self, params: LcwParams | Mapping[str, object] | None = None, *, strategy_id: StrategyId = LCW_ID,
                 exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None,
                 calibration: CalibrationProvider | None = None) -> None:
        p = params if isinstance(params, LcwParams) else parse_params(LcwParams, params, where=str(strategy_id))
        super().__init__(strategy_id=strategy_id, decide_every_blocks=p.decide_every_blocks, wake_on=WAKE_ON,
                         min_cadence_blocks=MIN_CADENCE_BLOCKS, valid_from_block=valid_from(), declares_dilution=True)
        self.params = p
        self.exec_cfg = exec_cfg if exec_cfg is not None else ExecCfg()
        self.risk = risk if risk is not None else RiskCfg()
        self.calibration = calibration

    def initial_memory(self) -> LcwMemory:
        return LcwMemory()

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        mem = memory if isinstance(memory, LcwMemory) else self.initial_memory()
        held = sleeve_positions(ctx, self.id)
        if not self.params.enabled:
            sigs = sort_signals(exit_signal(self.id, k, ctx.block, Urgency.NORMAL, ("lcw.disabled",)) for k in sorted(held))
            return StrategyOutput(sigs, replace(mem, last_signals=sigs))
        scheduled = self.scheduled(int(ctx.block), mem.last_sched)
        watch = [k for k in sorted(ctx.frame.feats) if self._in_watch(ctx, k)]
        if not scheduled and not relevant_wake(ctx.events, list(held) + watch, self.wake_on):
            return StrategyOutput(mem.last_signals, mem)
        ev = self.evaluate(ctx, mem, held=held, scheduled=scheduled)
        return StrategyOutput(ev.signals, ev.memory)

    # ---------------------------------------------------------------------------------------------- evaluation
    def _in_watch(self, ctx: TickContext, key: SubnetKey) -> bool:
        p = self.params
        f = ctx.frame.feats[key]
        ss = f.since_start_blocks
        return ss is not None and p.since_start_min_blocks <= ss <= p.since_start_max_blocks \
            and f.age_reg_blocks <= days_to_blocks(p.age_reg_max_days)

    def evaluate(self, ctx: TickContext, memory: LcwMemory | None = None, *,
                 held: Mapping[SubnetKey, SleevePos] | None = None, scheduled: bool = True) -> LcwEval:
        p = self.params
        mem = memory if memory is not None else self.initial_memory()
        b = int(ctx.block)
        hold = dict(held) if held is not None else sleeve_positions(ctx, self.id)
        cal = calibration_at(self.calibration, ctx.block)
        hist = History(ctx)
        states = {s.key: s for s in mem.keys}
        ev_keys = events_by_key(ctx.events)
        rows: dict[SubnetKey, LcwRow] = {}
        cands = [k for k in sorted(ctx.frame.feats) if ctx.raw.get(k) is not None
                 and (k in hold or self._in_watch(ctx, k))]
        for k in cands:
            pos = hold.get(k)
            rows[k] = self.gates(ctx, k, hist, cal, n_positions=len(hold), a_me_alpha=int(pos.alpha) if pos else 0,
                                 held=pos is not None)
        signals: list[Signal] = []
        h_blocks = days_to_blocks(p.h_days)

        # holdings
        kept = 0
        for key in sorted(hold):
            pos = hold[key]
            row = rows.get(key)
            s = ctx.raw.get(key)
            st = states.get(key) or LcwKeyState(key=key)
            if row is None or s is None:
                states[key] = st
                continue
            if st.entry_block is None:
                st = replace(st, entry_block=int(pos.opened_block))
            st, high, normal = self._exits(ctx, hist, row, s, pos, st, ev_keys.get(key, ()))
            if high or normal or st.exiting:
                urg = Urgency.HIGH if high else Urgency.NORMAL
                if st.exiting:
                    urg = max(urg, Urgency(st.exiting))
                st = replace(st, exiting=int(urg))
                signals.append(exit_signal(self.id, key, ctx.block, urg,
                                           ("lcw.exit",) + tuple(high + normal or ["lcw.exit_in_progress"])))
            else:
                kept += 1
                target = int(pos.value_rao)
                if row.v_rao > 0 and st.tranche1_block is not None and b >= st.tranche1_block + p.tranche_spacing_blocks:
                    target = max(target, row.v_rao)
                    st = replace(st, tranche1_block=None)
                signals.append(target_signal(self.id, key, ctx.block, value_rao=target, budget_rao=self._budget(ctx),
                                             edge_day=row.mu_hat, alpha_h=row.g_gross, horizon_blocks=h_blocks,
                                             declares_dilution=True, reasons=("lcw.hold",) + row.flags,
                                             hotkey=row.hotkey))
            states[key] = st

        # entries (first tranche) and AVOIDs with the failing gate codes
        new = 0
        for k, row in sorted(rows.items()):
            if k in hold:
                continue
            if row.qualifies and kept + new < p.positions_max:
                first = row.v_rao // p.tranche_count if p.tranche_count > 1 else row.v_rao
                signals.append(target_signal(self.id, k, ctx.block, value_rao=first, budget_rao=self._budget(ctx),
                                             edge_day=row.mu_hat, alpha_h=row.g_gross, horizon_blocks=h_blocks,
                                             declares_dilution=True, reasons=("lcw.entry", "lcw.tranche1") + row.flags,
                                             hotkey=row.hotkey))
                prior = states.get(k) or LcwKeyState(key=k)
                t1 = (prior.tranche1_block if prior.tranche1_block is not None else b) if p.tranche_count > 1 else None
                states[k] = replace(prior, tranche1_block=t1)       # spacing counts from the FIRST proposal
                new += 1
            else:
                why = row.failed if row.failed else ("lcw.U10.positions",)
                signals.append(avoid_signal(self.id, k, ctx.block, ("lcw.reject",) + why))

        # X1 needs "emission seen enabled" for every tracked key; then trim (an unfilled first tranche is forgotten
        # after a day once the generation is no longer a candidate)
        for k in sorted(set(states) | set(rows)):
            s = ctx.raw.get(k)
            if s is None:
                states.pop(k, None)
                continue
            st = states.get(k) or LcwKeyState(key=k)
            if s.emission_enabled and not st.was_enabled:
                st = replace(st, was_enabled=True)
            states[k] = st
        keep = {k: v for k, v in sorted(states.items()) if k in hold or k in rows or (
            v.tranche1_block is not None and b - v.tranche1_block <= BLOCKS_PER_DAY)}
        sigs = sort_signals(signals)
        new_mem = LcwMemory(keys=tuple(keep[k] for k in sorted(keep)), last_sched=b if scheduled else mem.last_sched,
                            last_signals=sigs)
        return LcwEval(block=ctx.block, rows=rows, signals=sigs, memory=new_mem)

    def _budget(self, ctx: TickContext) -> int:
        """Nominal LCW sleeve budget: NAV_liq * budget_ppm (the LCW <= 5 % NAV cap is the sleeve's budget_ppm)."""
        return int(ctx.nav_liq) * int(ctx.sleeve.budget_ppm) // PPM

    # ---------------------------------------------------------------------------------------------- gates
    def gates(self, ctx: TickContext, key: SubnetKey, hist: History, cal: Calibration | None, *, n_positions: int = 0,
              a_me_alpha: int = 0, held: bool = False) -> LcwRow:
        """Every section 2.4 LCW gate for `key` with its reason codes (empty `failed` = qualifies)."""
        p = self.params
        raw = ctx.raw
        f = ctx.frame.feats[key]
        s = raw.get(key)
        sv = ctx.view.get(key) or s
        failed: list[str] = []
        flags: list[str] = []
        if s is None or sv is None:
            return LcwRow(key, None, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0, None, ("lcw.missing",))
        # U1 WATCH window + Gatekeeper veto-all flags
        if not self._in_watch(ctx, key):
            failed.append("lcw.U1.watch")
        failed.extend(f"lcw.U1.gatekeeper.{fl}" for fl in sorted(f.launch_flags & VETO_ALL))
        # U2 pool
        tao = int(s.pool.tao)
        if tao < tao_to_rao(p.t_min_tao) or tao > tao_to_rao(p.t_max_tao):
            failed.append("lcw.U2.pool")
        # U3 spot / spot0
        spot0 = self._spot0(hist, s)
        if spot0 is None or spot0 <= 0:
            failed.append("lcw.U3.spot0_unknown")
        elif not p.spot_ratio_lo <= f.spot / spot0 <= p.spot_ratio_hi:
            failed.append("lcw.U3.spot_ratio")
        # U4 burn (2-epoch mean)
        prev = hist.subnet_ago(key, max(s.tempo, 1) + 1, max_stale=max(s.tempo, 1))
        mb_prev = float(prev[1].miner_burned) if prev is not None else float(s.miner_burned)
        if (float(s.miner_burned) + mb_prev) / 2.0 > p.miner_burned_2epoch_max:
            failed.append("lcw.U4.burn")
        # U5 metagraph
        mg = s.metagraph
        if mg is None:
            failed.append("lcw.U5.metagraph_missing")
        else:
            if mg.n_miners < p.miner_uids_min:
                failed.append("lcw.U5.miners")
            if mg.n_miner_coldkeys < p.miner_coldkeys_min:
                failed.append("lcw.U5.coldkeys")
            if mg.top1_coldkey_share_ppm > frac_to_ppm(p.top1_coldkey_share_max):
                failed.append("lcw.U5.top1")
            if mg.n_permit_coldkeys < p.permit_coldkeys_min:
                failed.append("lcw.U5.permits")
            if mg.n_miners == 0:
                flags.append("lcw.miner_half_upside")
        # U6 owner
        failed.extend(self._owner_gate(hist, s))
        # flows
        phi = hist.flow_ewma_per_day(key, p.phi_half_life_blocks, p.phi_step_blocks)
        f6 = hist.flow_frac(key, 1_800)
        phi_6h = f6 * 4.0 if f6 is not None else None
        if phi is None or phi < p.phi_ewma_min_frac_day:
            failed.append("lcw.U8.phi_ewma")
        if phi_6h is None or phi_6h < p.phi_6h_min:
            failed.append("lcw.U8.phi_6h")
        # U7 router hotkey
        h = yield_hotkey(ctx, f)
        c = candidate(f, h)
        idx = s.hotkey(h) if h is not None else None
        y_real = c.score_ppm_day / PPM if c is not None else 0.0
        if c is None or idx is None or h is None:
            failed.append("lcw.U7.hotkey")
        else:
            rets = hist.epoch_index_returns(key, h, p.router_epochs)
            if sum(1 for r in rets if r > 0) < p.router_positive_epochs_min:
                failed.append("lcw.U7.positive_epochs")
        # forecast, score and size
        a0 = f.a_earn_alpha
        spot = f.spot
        t_tao = f.pool_tao
        phi_pos = max(phi or 0.0, 0.0)
        g_mech = f.a_earn_growth_day * a0
        ae = BLOCKS_PER_DAY * int(s.alpha_out_emission) / RAO_PER_TAO
        c_o = float(owner_cut_frac(s, raw.glob))
        take = idx.take_u16 / FEE_DEN if idx is not None else 1.0
        ck = idx.childkey_take_u16 / FEE_DEN if idx is not None else 1.0
        num = ae * (1.0 - c_o) * float(VALIDATOR_SHARE) * (1.0 - f.rp) * (1.0 - take) * (1.0 - ck)

        def y_avg(a_me: float) -> float:
            vals = []
            for tau in range(p.yield_avg_days_lo, p.yield_avg_days_hi + 1):
                den = a0 + g_mech * tau + (p.kappa_e * phi_pos * t_tao * tau / spot if spot > 0 else 0.0) + a_me
                vals.append(num / den if den > 0 else 0.0)
            return sum(vals) / len(vals)

        phi_cal = cal.phi if cal is not None else SellLoadParams()
        share = EmissionShare(key=key, b=Decimal(0), keep=Decimal(0), final=Decimal(0), tao_per_block=Rao(0),
                              tao_in_per_block=Rao(0), chain_buy_per_block=Rao(0))
        headwind = float(sell_load(s, raw.glob, share, phi_cal, raw.block, ctx.frame.emission.root_flag).sell_push_day)
        a_me = a_me_alpha / RAO_PER_TAO
        pool = sv.pool
        f_fee = pool.fee_rate / FEE_DEN
        rt: int | None = None
        ya = y_avg(a_me)
        if a_me_alpha == 0 and spot > 0:         # provisional size without our own stake, then one iteration with it
            v0 = self._size(ctx, pool, (2.0 * p.lam * (phi or 0.0) + ya - headwind) * p.h_days, phi_pos, t_tao)
            ya = y_avg(v0 / RAO_PER_TAO / spot)
        g_gross = (2.0 * p.lam * (phi or 0.0) + ya - headwind) * p.h_days
        v = self._size(ctx, pool, g_gross, phi_pos, t_tao)
        tx = int(self.exec_cfg.buy_tx_fee_rao) + int(self.exec_cfg.sell_tx_fee_rao)
        if v > 0:
            try:
                rt = round_trip_cost_ppm(pool, Rao(v), ImpactBound.TEMPORARY, tx)
            except SwapError:
                rt = None
        mu_hat = 2.0 * p.lam * (phi or 0.0) + ya - headwind - ((rt / PPM) / p.h_days if rt is not None else 1.0)
        if c is not None and a0 > 0:
            mine = a_me if a_me > 0 else (v / RAO_PER_TAO / spot if spot > 0 else 0.0)
            if y_real * a0 / (a0 + mine) < _pct(p.router_marginal_net_yield_min_pct_day):
                failed.append("lcw.U7.marginal_yield")
        elif c is not None:
            failed.append("lcw.U7.marginal_yield")
        if mu_hat < _pct(p.mu_hat_min_pct_day):
            failed.append("lcw.U9.mu_hat")
        if g_gross - 2.0 * f_fee < p.g_minus_2f_min:
            failed.append("lcw.U9.g_minus_2f")
        if not held and n_positions >= p.positions_max:
            failed.append("lcw.U10.positions")
        failed.extend(f"lcw.book.{r}" for r in entry_blocks(ctx, key))
        if v <= 0 or rt is None:
            failed.append("lcw.size")
        return LcwRow(key=key, hotkey=h, phi_ewma=phi, phi_6h=phi_6h, y_real=y_real, y_avg=ya, headwind=headwind,
                      g_gross=g_gross, mu_hat=mu_hat, v_rao=v, rt_ppm=rt, failed=tuple(failed), flags=tuple(flags))

    def _size(self, ctx: TickContext, pool: PoolState, g_gross: float, phi_pos: float, t_tao: float) -> int:
        p = self.params
        caps = [int(v_star(pool, to_ppm(g_gross))), int(v_max(pool.tao, frac_to_ppm(p.size_slip_frac))),
                int(pool.tao) * frac_to_ppm(p.pool_frac_max) // PPM,
                to_ppm(p.organic_frac * phi_pos * t_tao) * (RAO_PER_TAO // PPM),
                int(ctx.nav_liq) * frac_to_ppm(p.nav_frac_max) // PPM]
        return max(min(caps), 0)

    def _spot0(self, hist: History, s: SubnetState) -> float | None:
        """Spot of the generation's first stored snapshot (searched on the 60-block grid after NetworkRegisteredAt)."""
        reg = int(s.key.reg_at)
        for i in range(1, SPOT0_SEARCH_STEPS + 1):
            snap = hist.at_or_before(reg + i * hist.grid_blocks)
            if snap is None or int(snap.block) < reg:
                continue
            st = snap.get(s.key)
            if st is not None and st.pool.px_alpha > 0 and st.pool.px_tao > 0:
                return float(st.pool.spot())
        return None

    def _owner_gate(self, hist: History, s: SubnetState) -> list[str]:
        p = self.params
        if s.owner_cut_autolock is True:
            return []
        week = 7 * BLOCKS_PER_DAY
        sold_7d = hist.owner_sold_alpha(s.key, week, EPOCH_STEP_BLOCKS)
        sold_1d = hist.owner_sold_alpha(s.key, BLOCKS_PER_DAY)
        if sold_7d is None or sold_1d is None or s.pool.px_alpha <= 0 or s.pool.tao <= 0:
            return ["lcw.U6.owner_unknown"]
        accrual = float(DEC.multiply(owner_cut_frac(s, hist.now.glob), Decimal(int(s.alpha_out_emission) * week)))
        spot = float(s.pool.spot())
        out_frac = sold_1d * spot / int(s.pool.tao)
        if sold_7d > p.owner_sold_7d_max_of_accrual * accrual or out_frac > p.owner_tao_out_24h_max_frac:
            return ["lcw.U6.owner"]
        return []

    # ---------------------------------------------------------------------------------------------- exits
    def _exits(self, ctx: TickContext, hist: History, row: LcwRow, s: SubnetState, pos: SleevePos, st: LcwKeyState,
               events: tuple[ChainEvent, ...]) -> tuple[LcwKeyState, list[str], list[str]]:
        p = self.params
        b = int(ctx.block)
        f = ctx.frame.feats[row.key]
        high: list[str] = []
        normal: list[str] = []
        # X1 emission disabled after enabled
        if (st.was_enabled and not s.emission_enabled) or any(
                e.kind is K.EMISSION_TOGGLED and e.flag is False for e in events):
            high.append("lcw.X1.emission_off")
        # X2 owner dump
        sold_1d = hist.owner_sold_alpha(row.key, BLOCKS_PER_DAY)
        accrual_1d = float(DEC.multiply(owner_cut_frac(s, ctx.raw.glob), Decimal(int(s.alpha_out_emission) * BLOCKS_PER_DAY)))
        if sold_1d is not None and s.pool.tao > 0 and s.pool.px_alpha > 0 and (
                (accrual_1d > 0 and sold_1d >= p.x2_owner_dump_accrual_mult * accrual_1d)
                or sold_1d * float(s.pool.spot()) >= p.x2_owner_dump_t_frac_24h * int(s.pool.tao)):
            high.append("lcw.X2.owner_dump")
        if any(e.kind is K.OWNER_CHANGED for e in events):
            high.append("lcw.X2.owner_changed")
        if any(e.kind is K.AUTOLOCK_TOGGLED and e.flag is False for e in events):
            high.append("lcw.X2.autolock_off")
        # X4 stops on executable value
        vps = int(DEC.divide(DEC.multiply(Decimal(int(pos.value_rao)), Decimal(10**12)), pos.shares)) if pos.shares > 0 else 0
        st = replace(st, peak_e12=max(st.peak_e12, vps))
        if pos.cost_rao > 0 and pos.value_rao * PPM <= pos.cost_rao * (PPM + frac_to_ppm(p.x4_stop_from_cost)):
            high.append("lcw.X4.stop_from_cost")
        if st.peak_e12 > 0 and vps * PPM <= st.peak_e12 * (PPM - frac_to_ppm(p.x4_trailing)):
            high.append("lcw.X4.trailing")
        # epoch counters (X3 burn, X5 slow flow)
        ss = f.since_start_blocks or 0
        if int(s.last_epoch_block) != st.last_epoch_seen:
            burn_hot = float(s.miner_burned) > p.x3_miner_burned
            phi_neg = row.phi_ewma is not None and row.phi_ewma < 0 and ss >= days_to_blocks(p.x5_after_days)
            st = replace(st, last_epoch_seen=int(s.last_epoch_block), burn_epochs=st.burn_epochs + 1 if burn_hot else 0,
                         phi_neg_epochs=st.phi_neg_epochs + 1 if phi_neg else 0)
        if st.burn_epochs >= p.x3_burn_epochs:
            normal.append("lcw.X3.burn")
        if s.metagraph is not None and s.metagraph.n_miners < p.x3_min_miners:
            normal.append("lcw.X3.miners")
        if row.phi_6h is not None and row.phi_6h <= p.x5_phi_6h_frac_day:
            normal.append("lcw.X5.phi_6h")
        if st.phi_neg_epochs >= p.x5_neg_epochs:
            normal.append("lcw.X5.phi_ewma")
        if row.y_real < _pct(p.x6_realised_yield_min_pct_day) and row.mu_hat < 0:
            normal.append("lcw.X6.yield")
        held_blocks = b - (st.entry_block if st.entry_block is not None else int(pos.opened_block))
        if held_blocks >= days_to_blocks(p.x7_hold_days) or \
                f.age_reg_blocks >= ctx.raw.glob.immunity_period - p.x7_before_immunity_end_blocks:
            normal.append("lcw.X7.time")
        if not s.emission_enabled and ss >= days_to_blocks(p.emission_enable_deadline_days):
            normal.append("lcw.emission_deadline")
        return st, high, normal

