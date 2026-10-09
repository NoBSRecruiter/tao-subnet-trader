"""taotrader/strategies/momentum.py - sleeve (b) momentum / rotation (WP9; DESIGN.md section 2.2).
Verdict EXPERIMENTAL (research-gated): only the MVP below can reach PAPER; deferred hypotheses sit behind flags (OFF).

Universe: the overlay floor A-G, plus 600 <= T <= 10,000 TAO (micro class < 3,000); age_reg >= 30 d AND
(age_reg >= 90 d OR EMA rank <= 40); MinerBurned <= 0.5; quote weight in [0.45, 0.55]; yield hotkey take <= 2 % and
ChildkeyTake = 0; >= 7 d of same-generation price and flow history (Feat.ret_7d and Feat.flow_7d present, so no
SubnetTaoFlow reset); non-immune prune_rank >= 7.

Signals (Feat, from the median-of-3 60-block price; z = rank-gauss over the eligible set U):
- r_24h = Feat.ret_1d, r_7d = Feat.ret_7d, nf_24h = Feat.flow_1d; S = z(z(r_24h) + z(r_7d) + z(nf_24h)).
- m_u = clip(0.5 * mean over U of the 14-day mean daily d ln(P*I), -0.5 %, +0.3 %). P is the pool spot and I the
  index of Feat.best_candidate, 14 days ago (ctx.store) and now; names lacking either reading are left out.
- y_n = min(closed-form net yield of the yield hotkey, its realised EWMA d ln I).
- EH = H_e * (m_u + y_n) + b_S * S * alpha_days(H_e), with alpha_days(H) = min(H, 1) + alpha_tail * max(H - 1, 0).
- V = min(T * max(0, EH - phi) / (4 lam), rho * T, T * s_exit / (1 - s_exit), w_max * sleeve NAV), with
  phi = 2f + 2 lat, and rho = 0.30 % (micro) or 0.50 % (near-micro). "Sleeve NAV" is the nominal sleeve budget
  G_MAX * NAV_liq * budget_ppm (RiskCfg.g_max_ppm of the book; TickContext carries no sleeve capital figure).
- net_edge = EH - RT_TEMPORARY(V) - 2 lat. This is the cost gate. The original candidate() sold into the post-buy
  pool, i.e. the optimistic PERSISTENT bound, which over-admits by 0.2-0.6 %; that bound is never used here.

Entry (each scheduled evaluation, every 300 blocks): rank by S within K + K_buf; S >= S_in; r_24h > 0;
net_edge >= net_margin (0.25 %); V >= 1.5 TAO; at most 2 new entries per cycle; trailing-24h one-way (buy) turnover,
plus the new entries, <= 30 % of sleeve NAV. The turnover comes from FILLED buys in book_view.orders, attributed to this
sleeve. The adaptive min hold clip(RT / daily edge, 1, 4) d is fixed when a holding is first seen.

Exits: trailing stop on executable value per sleeve share, clip(k_stop * sigma_d, 10 %, 25 %) from peak (HIGH;
sigma_d unknown -> 10 %, the tight end); flow reversal nf_1h <= -2.5 % or nf_4h <= -5 % of the pool (HIGH;
nf_4h from ctx.store). Decay (S < S_out, or rank > K + K_buf, on 2 cycles, after the min hold), universe failure on
2 cycles, and the time exit (3 d micro / 5 d near-micro, re-underwritten through the entry rules at most twice) are
NORMAL. Overlay forced exits are separate and authoritative.

Deferred hypotheses (code present, flags OFF; each has its own test, section 2.2):
- p1_impulse (F14): a LARGE_FLOW inflow >= impulse_frac on an eligible name admits it outside the top K + K_buf,
  at HIGH urgency. It must still pass the cost gate.
- ema_gap_struct (F4): S adds w_ema_gap * z(ema_gap) + w_struct_level * z(cb_push - sell_push) + w_struct_lag *
  z(1-day change of that level; the past level comes from the store through protocol.sellload).
- rotation (F5c): an entry decided in the same cycle as an exit is annotated "rotation_from:<netuid>:<reg_at>" and
  gated with the rotation cost: the move pays the swap fee once (on the origin leg) and ExecCfg.rotate_tx_fee instead
  of a sell plus a buy, so the entry's net edge gains one swap fee and (buy + sell - rotate) tx / V. The planner's
  MOVE_STAKE_LIMIT path stays disabled until FT-M5c.
- breadth_gate (F8): no new entries while the share of U with r_24h > 0 is below breadth_min.
- weekly_reversal_guard (F8): no entry in a micro name whose r_7d exceeds weekly_reversal_max.
- blowoff_trim (F9): a holding whose fast_ema_gap >= blowoff_gap is trimmed to blowoff_keep_frac.

Cadence: decide every 300 blocks; wake on LARGE_FLOW (held), EMISSION_TOGGLED and DEREGISTERED. A wake run without a
held-generation event only returns the last signals. valid_from_block = 8,466,531 (SubnetTaoFlow valid);
min_cadence_blocks = 60. declares_dilution = False.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final

from ..core.config import ExecCfg, RiskCfg
from ..core.events import ChainEventKind
from ..core.fixed import DEC
from ..core.orders import OrderKind, OrderState, Urgency
from ..core.protocols import TickContext
from ..core.signals import Signal, SignalKind, StrategyOutput
from ..core.state import PoolState
from ..core.units import BLOCKS_PER_DAY, FEE_DEN, PERQUINTILL, PPM, Block, Hotkey, Rao, StrategyId, SubnetKey
from ..protocol.amm import ImpactBound, SwapError, round_trip_cost_ppm, v_max
from ..protocol.emission import EmissionShare
from ..protocol.regimes import regime
from ..protocol.sellload import SellLoadParams, sell_load
from ..protocol.yield_model import closed_form_yield_net
from .base import (
    History,
    SleevePos,
    StrategyBase,
    candidate,
    days_to_blocks,
    entry_blocks,
    exit_signal,
    floor_rows,
    frac_to_ppm,
    parse_params,
    rank_gauss,
    relevant_wake,
    sleeve_budget_rao,
    sleeve_positions,
    sort_signals,
    tao_to_rao,
    target_signal,
    yield_hotkey,
)

MOMENTUM_ID: Final[StrategyId] = StrategyId("momentum")
K = ChainEventKind
WAKE_ON: Final[frozenset[ChainEventKind]] = frozenset({K.LARGE_FLOW, K.EMISSION_TOGGLED, K.DEREGISTERED})
MIN_CADENCE_BLOCKS: Final[int] = 60
NF_4H_BLOCKS: Final[int] = 1_200


def valid_from() -> Block:
    return regime("price_ema_rp").first_block


# ------------------------------------------------------------------------------------------------- params
@dataclass(frozen=True, slots=True)
class MomentumParams:
    """Section 2.2 parameters (config/preregistration.toml [momentum.params] / [momentum.universe] / [momentum.exits]).
    P&L-tuned (budget 4): s_in, rho_micro_frac / rho_near_frac, h_e_days, k_stop."""
    s_in: float = 1.0
    s_out: float = -0.25
    rho_micro_frac: float = 0.0030
    rho_near_frac: float = 0.0050
    h_e_days: float = 2.0
    k_stop: float = 2.5
    b_s_per_z_day: float = 0.0010
    alpha_tail: float = 0.25
    k: int = 5
    k_buf: int = 2
    lam: float = 1.5
    s_exit_frac: float = 0.01
    w_max_frac: float = 0.30
    net_margin_frac: float = 0.0025
    lat_frac: float = 0.0005
    lat_stress_frac: float = 0.003
    limit_eps_frac: float = 0.01                 # planner input (its beta overrides when larger); carried for the record
    turnover_cap_nav_per_day: float = 0.30
    t_min_tao: float = 600.0
    t_max_tao: float = 10_000.0
    micro_cut_tao: float = 3_000.0
    take_max: float = 0.02
    childkey_take_max: float = 0.0
    age_reg_min_days: float = 30.0
    age_reg_full_days: float = 90.0
    ema_rank_max: int = 40
    miner_burned_max: float = 0.5
    quote_lo: float = 0.45
    quote_hi: float = 0.55
    prune_rank_min: int = 7
    max_new_entries: int = 2
    v_min_tao: float = 1.5
    m_u_lo: float = -0.005
    m_u_hi: float = 0.003
    m_u_days: float = 14.0
    stop_clip_lo: float = 0.10
    stop_clip_hi: float = 0.25
    flow_rev_1h_frac: float = -0.025
    flow_rev_4h_frac: float = -0.05
    decay_cycles: int = 2
    universe_fail_cycles: int = 2
    time_exit_micro_days: float = 3.0
    time_exit_near_days: float = 5.0
    underwrite_max: int = 2
    min_hold_lo_days: float = 1.0
    min_hold_hi_days: float = 4.0
    rebalance_blocks: int = 300
    # --- deferred hypotheses (OFF)
    p1_impulse: bool = False
    impulse_frac: float = 0.02
    ema_gap_struct: bool = False
    w_ema_gap: float = 1.0
    w_struct_level: float = 1.0
    w_struct_lag: float = 1.0
    rotation: bool = False
    breadth_gate: bool = False
    breadth_min: float = 0.5
    weekly_reversal_guard: bool = False
    weekly_reversal_max: float = 0.30
    blowoff_trim: bool = False
    blowoff_gap: float = 0.06
    blowoff_keep_frac: float = 0.5

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.h_e_days <= 0 or self.lam <= 0:
            out.append("h_e_days and lam must be > 0")
        if self.k < 1 or self.k_buf < 0 or self.max_new_entries < 0 or self.rebalance_blocks < 1:
            out.append("k >= 1, k_buf >= 0, max_new_entries >= 0, rebalance_blocks >= 1")
        if self.s_out > self.s_in:
            out.append("s_out must be <= s_in")
        if not 0 < self.t_min_tao <= self.t_max_tao:
            out.append("need 0 < t_min_tao <= t_max_tao")
        if not 0 <= self.quote_lo <= self.quote_hi <= 1:
            out.append("need 0 <= quote_lo <= quote_hi <= 1")
        if not 0 < self.stop_clip_lo <= self.stop_clip_hi < 1:
            out.append("need 0 < stop_clip_lo <= stop_clip_hi < 1")
        for name in ("s_exit_frac", "w_max_frac", "rho_micro_frac", "rho_near_frac", "blowoff_keep_frac"):
            if not 0 <= getattr(self, name) < 1:
                out.append(f"{name} must be in [0, 1)")
        return out


# ------------------------------------------------------------------------------------------------- memory
@dataclass(frozen=True, slots=True)
class MomKeyState:
    key: SubnetKey
    entry_block: int | None = None
    min_hold_blocks: int = 0
    underwrites: int = 0
    peak_e12: int = 0                      # peak executable value per sleeve share, * 1e12 (trailing stop)
    decay_count: int = 0
    universe_fail_count: int = 0
    exiting: int = 0                       # urgency of a sticky sleeve exit in progress


@dataclass(frozen=True, slots=True)
class MomentumMemory:
    keys: tuple[MomKeyState, ...] = ()
    last_sched: int | None = None
    last_signals: tuple[Signal, ...] = ()


@dataclass(frozen=True, slots=True)
class MomRow:
    key: SubnetKey
    hotkey: Hotkey | None
    universe_failed: tuple[str, ...]
    s_score: float | None
    rank: int | None
    r_24h: float | None
    y_n: float
    eh: float
    v_rao: int
    rt_ppm: int | None
    net_edge: float | None
    micro: bool

    @property
    def in_universe(self) -> bool:
        return not self.universe_failed


@dataclass(frozen=True, slots=True)
class MomentumEval:
    block: Block
    m_u: float
    breadth: float
    rows: Mapping[SubnetKey, MomRow] = field(default_factory=dict)
    signals: tuple[Signal, ...] = ()
    memory: MomentumMemory = MomentumMemory()


def alpha_days(h_days: float, alpha_tail: float) -> float:
    """Measured decay of the continuation: min(H, 1) + alpha_tail * max(H - 1, 0)."""
    return min(h_days, 1.0) + alpha_tail * max(h_days - 1.0, 0.0)


def net_edge(eh: float, pool: PoolState, v_rao: int, bound: ImpactBound, tx_fees_rao: int, lat: float) -> float | None:
    """EH - RT_bound(V) - 2 lat (None when V cannot be quoted). Gating uses ImpactBound.TEMPORARY only."""
    if v_rao <= 0:
        return None
    try:
        rt = round_trip_cost_ppm(pool, Rao(v_rao), bound, tx_fees_rao)
    except SwapError:
        return None
    return eh - rt / PPM - 2.0 * lat


# ------------------------------------------------------------------------------------------------- strategy
class MomentumStrategy(StrategyBase):
    """Section 2.2 momentum MVP. Pure; state in MomentumMemory."""

    def __init__(self, params: MomentumParams | Mapping[str, object] | None = None, *,
                 strategy_id: StrategyId = MOMENTUM_ID, exec_cfg: ExecCfg | None = None,
                 risk: RiskCfg | None = None) -> None:
        p = params if isinstance(params, MomentumParams) else parse_params(MomentumParams, params, where=str(strategy_id))
        super().__init__(strategy_id=strategy_id, decide_every_blocks=p.rebalance_blocks, wake_on=WAKE_ON,
                         min_cadence_blocks=MIN_CADENCE_BLOCKS, valid_from_block=valid_from(), declares_dilution=False)
        self.params = p
        self.exec_cfg = exec_cfg if exec_cfg is not None else ExecCfg()
        self.risk = risk if risk is not None else RiskCfg()

    def initial_memory(self) -> MomentumMemory:
        return MomentumMemory()

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        mem = memory if isinstance(memory, MomentumMemory) else self.initial_memory()
        held = sleeve_positions(ctx, self.id)
        scheduled = self.scheduled(int(ctx.block), mem.last_sched)
        impulse = self.params.p1_impulse and any(
            e.kind is K.LARGE_FLOW and (e.amount or 0) > 0 for e in ctx.events)
        if not scheduled and not impulse and not relevant_wake(ctx.events, held, self.wake_on):
            return StrategyOutput(mem.last_signals, mem)
        ev = self.evaluate(ctx, mem, held=held, scheduled=scheduled)
        return StrategyOutput(ev.signals, ev.memory)

    # ---------------------------------------------------------------------------------------------- evaluation
    def evaluate(self, ctx: TickContext, memory: MomentumMemory | None = None, *,
                 held: Mapping[SubnetKey, SleevePos] | None = None, scheduled: bool = True) -> MomentumEval:
        p = self.params
        mem = memory if memory is not None else self.initial_memory()
        b = int(ctx.block)
        hold = dict(held) if held is not None else sleeve_positions(ctx, self.id)
        hist = History(ctx)
        budget = sleeve_budget_rao(ctx, int(self.risk.g_max_ppm))
        rows, m_u, breadth = self._rows(ctx, hist, budget, hold)
        states = {s.key: s for s in mem.keys}
        h_blocks = days_to_blocks(p.h_e_days)
        v_min = tao_to_rao(p.v_min_tao)
        top = p.k + p.k_buf
        signals: list[Signal] = []
        exits: list[SubnetKey] = []

        for key in sorted(hold):
            pos = hold[key]
            row = rows.get(key)
            st = states.get(key) or MomKeyState(key=key)
            if row is None:
                states[key] = st
                continue
            if st.entry_block is None:
                st = replace(st, entry_block=int(pos.opened_block), min_hold_blocks=self._min_hold(row))
            f = ctx.frame.feats[key]
            # trailing stop on executable value per sleeve share
            vps = int(DEC.divide(DEC.multiply(Decimal(int(pos.value_rao)), Decimal(10**12)), pos.shares)) \
                if pos.shares > 0 else 0
            st = replace(st, peak_e12=max(st.peak_e12, vps))
            sd = f.sigma_d
            stop = p.stop_clip_lo if sd is None else min(max(p.k_stop * sd, p.stop_clip_lo), p.stop_clip_hi)
            high: list[str] = []
            normal: list[str] = []
            if st.peak_e12 > 0 and vps * PPM <= st.peak_e12 * (PPM - frac_to_ppm(stop)):
                high.append("stop.trailing")
            nf4 = hist.flow_frac(key, NF_4H_BLOCKS)
            if (f.flow_1h is not None and f.flow_1h <= p.flow_rev_1h_frac) or (nf4 is not None and nf4 <= p.flow_rev_4h_frac):
                high.append("exit.flow_reversal")
            if scheduled:
                weak = (row.s_score is None or row.s_score < p.s_out or row.rank is None or row.rank > top)
                st = replace(st, decay_count=st.decay_count + 1 if weak else 0,
                             universe_fail_count=0 if row.in_universe else st.universe_fail_count + 1)
            held_blocks = b - (st.entry_block if st.entry_block is not None else int(pos.opened_block))
            if st.decay_count >= p.decay_cycles and held_blocks >= st.min_hold_blocks:
                normal.append("exit.decay")
            if st.universe_fail_count >= p.universe_fail_cycles:
                normal.append("exit.universe")
            reasons: tuple[str, ...] = ()
            limit = days_to_blocks(p.time_exit_micro_days if row.micro else p.time_exit_near_days)
            if held_blocks >= limit and not high and not normal and st.exiting == 0:
                ok = row.rank is not None and row.rank <= top and self._entry_ok(row, p.s_in) == ()
                if st.underwrites < p.underwrite_max and ok:
                    st = replace(st, entry_block=b, underwrites=st.underwrites + 1)
                    reasons = ("exit.time.reunderwritten",)
                else:
                    normal.append("exit.time")
            if high or normal or st.exiting:
                urg = Urgency.HIGH if high else Urgency.NORMAL
                if st.exiting:
                    urg = max(urg, Urgency(st.exiting))
                states[key] = replace(st, exiting=int(urg))
                signals.append(exit_signal(self.id, key, ctx.block, urg,
                                           ("momentum.exit",) + tuple(high + normal or ["momentum.exit_in_progress"])))
                exits.append(key)
                continue
            held_v = int(pos.value_rao)
            target = row.v_rao if (row.v_rao > 0 and self._entry_ok(row, p.s_out) == ()) else held_v
            if p.blowoff_trim and f.fast_ema_gap >= p.blowoff_gap:
                target = min(target, held_v * frac_to_ppm(p.blowoff_keep_frac) // PPM)
                reasons += ("blowoff_trim",)
            states[key] = st
            signals.append(self._target(ctx, row, target, budget, h_blocks, ("momentum.hold",) + reasons))

        # entries
        turnover = self._turnover_24h(ctx)
        cap = budget * frac_to_ppm(p.turnover_cap_nav_per_day) // PPM
        impulse_keys = {e.key for e in ctx.events if p.p1_impulse and e.kind is K.LARGE_FLOW and e.key is not None
                        and (e.amount or 0) > 0 and abs(e.frac_ppm or 0) >= frac_to_ppm(p.impulse_frac)}
        new = 0
        kept = len(hold) - len(exits)
        if not scheduled:            # a wake run keeps the unfilled entry targets of the last scheduled evaluation
            carried = [s for s in mem.last_signals if s.key not in hold and s.kind is SignalKind.TARGET]
            signals.extend(carried)
            new = len(carried)
        order = sorted((r for r in rows.values() if r.s_score is not None and r.rank is not None),
                       key=lambda r: (r.rank, r.key))
        breadth_ok = not p.breadth_gate or breadth >= p.breadth_min
        emitted = {s.key for s in signals}
        for r in order:
            if new >= p.max_new_entries or kept + new >= top:
                break
            if r.key in hold or r.key in emitted or not breadth_ok:
                continue
            impulse = r.key in impulse_keys
            if not (scheduled or impulse):
                continue
            if (r.rank or top + 1) > top and not impulse:
                continue
            paired = p.rotation and bool(exits)
            if entry_blocks(ctx, r.key) or self._entry_ok(self._rotated(r, ctx) if paired else r,
                                                          0.0 if impulse else p.s_in) != ():
                continue
            if r.v_rao < v_min or turnover + r.v_rao > cap:
                continue
            if p.weekly_reversal_guard and r.micro:
                f = ctx.frame.feats[r.key]
                if f.ret_7d is not None and f.ret_7d > p.weekly_reversal_max:
                    continue
            why: tuple[str, ...] = ("momentum.entry",)
            if impulse:
                why += ("p1_impulse",)
            if paired:
                src = exits[min(new, len(exits) - 1)]
                why += (f"rotation_from:{int(src.netuid)}:{int(src.reg_at)}",)
            signals.append(self._target(ctx, r, r.v_rao, budget, h_blocks, why,
                                        urgency=Urgency.HIGH if impulse else Urgency.NORMAL))
            turnover += r.v_rao
            new += 1

        for key in sorted(states):
            if key not in hold:
                states.pop(key)
        sigs = sort_signals(signals)
        new_mem = MomentumMemory(keys=tuple(states[k] for k in sorted(states)),
                                 last_sched=b if scheduled else mem.last_sched, last_signals=sigs)
        return MomentumEval(block=ctx.block, m_u=m_u, breadth=breadth, rows=rows, signals=sigs, memory=new_mem)

    # ---------------------------------------------------------------------------------------------- pieces
    def _entry_ok(self, r: MomRow, s_min: float) -> tuple[str, ...]:
        """Signal-level entry rules (rank and size are checked by the caller): universe, S, r_24h > 0, cost gate."""
        p = self.params
        out: list[str] = list(r.universe_failed)
        if r.s_score is None or r.s_score < s_min:
            out.append("S<S_in")
        if r.r_24h is None or r.r_24h <= 0:
            out.append("r_24h<=0")
        if r.net_edge is None or r.net_edge < p.net_margin_frac:
            out.append("net_edge<margin")
        return tuple(out)

    def _rotated(self, r: MomRow, ctx: TickContext) -> MomRow:
        """The row with the rotation cost (deferred F5c flag): one swap fee and (buy + sell - rotate) tx / V saved."""
        s = ctx.view.get(r.key)
        if r.net_edge is None or r.v_rao <= 0 or s is None:
            return r
        ex = self.exec_cfg
        saved_tx = int(ex.buy_tx_fee_rao) + int(ex.sell_tx_fee_rao) - int(ex.rotate_tx_fee_rao)
        return replace(r, net_edge=r.net_edge + s.pool.fee_rate / FEE_DEN + saved_tx / r.v_rao)

    def _min_hold(self, r: MomRow) -> int:
        p = self.params
        daily = r.eh / p.h_e_days
        rt = (r.rt_ppm or 0) / PPM
        days = p.min_hold_hi_days if daily <= 0 else min(max(rt / daily, p.min_hold_lo_days), p.min_hold_hi_days)
        return days_to_blocks(days)

    def _target(self, ctx: TickContext, r: MomRow, value: int, budget: int, h_blocks: int, reasons: tuple[str, ...],
                urgency: Urgency = Urgency.NORMAL) -> Signal:
        p = self.params
        return target_signal(self.id, r.key, ctx.block, value_rao=value, budget_rao=budget, edge_day=r.eh / p.h_e_days,
                             alpha_h=r.eh, horizon_blocks=h_blocks, declares_dilution=False, reasons=reasons,
                             hotkey=r.hotkey, urgency=urgency)

    def _turnover_24h(self, ctx: TickContext) -> int:
        """One-way (buy) turnover of this sleeve over the last 7,200 blocks: FILLED buys in book_view.orders, by the
        sleeve's attribution share."""
        b = int(ctx.block)
        total = 0
        for o in ctx.book_view.orders:
            it = o.intent
            if o.state is not OrderState.FILLED or it.kind is not OrderKind.ADD_STAKE_LIMIT:
                continue
            if int(it.created_block) <= b - BLOCKS_PER_DAY:
                continue
            share = sum(int(ppm) for sid, ppm in it.attribution if sid == self.id)
            total += int(it.tao_in) * share // PPM
        return total

    def _universe(self, ctx: TickContext, floor_failed: Sequence[str] | None, key: SubnetKey) -> tuple[str, ...]:
        p = self.params
        f = ctx.frame.feats[key]
        s = ctx.raw.get(key)
        out: list[str] = []
        if floor_failed is None:
            out.append("floor.missing")
        elif floor_failed:
            out.append("floor." + "".join(floor_failed))
        if s is None:
            return (*out, "U.missing")
        tao = int(s.pool.tao)
        if tao < tao_to_rao(p.t_min_tao) or tao > tao_to_rao(p.t_max_tao):
            out.append("U.pool")
        age = f.age_reg_blocks
        if age < days_to_blocks(p.age_reg_min_days) or (
                age < days_to_blocks(p.age_reg_full_days) and (f.ema_rank_desc is None or f.ema_rank_desc > p.ema_rank_max)):
            out.append("U.age")
        if float(s.miner_burned) > p.miner_burned_max:
            out.append("U.burn")
        wq = s.pool.w_quote_e18
        if wq * PPM < frac_to_ppm(p.quote_lo) * PERQUINTILL or wq * PPM > frac_to_ppm(p.quote_hi) * PERQUINTILL:
            out.append("U.quote")
        c = candidate(f, yield_hotkey(ctx, f))
        if c is None:
            out.append("U.hotkey")
        else:
            if c.take_u16 * PPM > frac_to_ppm(p.take_max) * FEE_DEN:
                out.append("U.take")
            if c.childkey_take_u16 * PPM > frac_to_ppm(p.childkey_take_max) * FEE_DEN:
                out.append("U.childkey_take")
        if f.ret_1d is None or f.ret_7d is None or f.flow_1d is None or f.flow_7d is None:
            out.append("U.history")
        if f.prune_rank is not None and f.prune_rank < p.prune_rank_min:
            out.append("U.prune")
        return tuple(out)

    def _m_u(self, ctx: TickContext, hist: History, keys: Sequence[SubnetKey]) -> float:
        p = self.params
        span = days_to_blocks(p.m_u_days)
        old_snap = hist.at_or_before(int(ctx.block) - span)
        vals: list[float] = []
        if old_snap is not None and int(old_snap.block) < int(ctx.block):
            days = (int(ctx.block) - int(old_snap.block)) / BLOCKS_PER_DAY
            for k in keys:
                s1, s0 = ctx.raw.get(k), old_snap.get(k)
                h = ctx.frame.feats[k].best_candidate
                if s0 is None or s1 is None or h is None:
                    continue
                i0, i1 = s0.hotkey(h), s1.hotkey(h)
                if i0 is None or i1 is None or min(i0.total_shares, i1.total_shares) <= 0:
                    continue
                if min(i0.total_alpha, i1.total_alpha) <= 0 or min(s0.pool.px_alpha, s1.pool.px_alpha) <= 0:
                    continue
                v0 = DEC.multiply(s0.pool.spot(), i0.index())
                v1 = DEC.multiply(s1.pool.spot(), i1.index())
                if v0 <= 0 or v1 <= 0:
                    continue
                vals.append(float(DEC.ln(DEC.divide(v1, v0))) / days)
        mean = sum(vals) / len(vals) if vals else 0.0
        return min(max(0.5 * mean, p.m_u_lo), p.m_u_hi)

    def _struct_terms(self, ctx: TickContext, hist: History, keys: Sequence[SubnetKey]) -> tuple[list[float], list[float]]:
        """(cb_push - sell_push now, its change over 1 day). The past level uses the store snapshot 1 day ago:
        protocol.sellload with the frozen priors, and that snapshot's SubnetExcessTao as the chain buy."""
        level = [ctx.frame.feats[k].cb_push_day - ctx.frame.feats[k].sell_push_day for k in keys]
        old_snap = hist.at_or_before(int(ctx.block) - BLOCKS_PER_DAY)
        lag: list[float] = []
        for k, lv in zip(keys, level, strict=True):
            s0 = old_snap.get(k) if old_snap is not None else None
            if s0 is None or s0.pool.px_tao <= 0 or s0.pool.w_base_e18 <= 0:
                lag.append(0.0)
                continue
            share = EmissionShare(key=k, b=Decimal(0), keep=Decimal(0), final=Decimal(0), tao_per_block=Rao(0),
                                  tao_in_per_block=Rao(0), chain_buy_per_block=Rao(0))
            assert old_snap is not None
            sell0 = float(sell_load(s0, old_snap.glob, share, SellLoadParams(), old_snap.block,
                                    ctx.frame.emission.root_flag).sell_push_day)
            cb0 = PERQUINTILL / s0.pool.w_base_e18 * int(s0.excess_tao) * BLOCKS_PER_DAY / int(s0.pool.px_tao)
            lag.append(lv - (cb0 - sell0))
        return level, lag

    def _rows(self, ctx: TickContext, hist: History, budget: int, hold: Mapping[SubnetKey, SleevePos]
              ) -> tuple[dict[SubnetKey, MomRow], float, float]:
        p = self.params
        raw, frame = ctx.raw, ctx.frame
        keys = sorted(k for k in frame.feats if raw.get(k) is not None)
        floor = floor_rows(ctx, self.risk)
        uni = {k: self._universe(ctx, floor[k].failed if k in floor else None, k) for k in keys}
        u_keys = [k for k in keys if not uni[k]]
        r1 = [float(frame.feats[k].ret_1d or 0.0) for k in u_keys]
        r7 = [float(frame.feats[k].ret_7d or 0.0) for k in u_keys]
        nf = [float(frame.feats[k].flow_1d or 0.0) for k in u_keys]
        combined = [a + b + c for a, b, c in zip(rank_gauss(r1), rank_gauss(r7), rank_gauss(nf), strict=True)]
        if p.ema_gap_struct and u_keys:
            gaps = rank_gauss([frame.feats[k].ema_gap for k in u_keys])
            level, lag = self._struct_terms(ctx, hist, u_keys)
            zl, zg = rank_gauss(level), rank_gauss(lag)
            combined = [c + p.w_ema_gap * g + p.w_struct_level * lv + p.w_struct_lag * lg
                        for c, g, lv, lg in zip(combined, gaps, zl, zg, strict=True)]
        s_vals = rank_gauss(combined)
        s_of = dict(zip(u_keys, s_vals, strict=True))
        ranked = sorted(u_keys, key=lambda k: (-s_of[k], k))
        rank_of = {k: i + 1 for i, k in enumerate(ranked)}
        m_u = self._m_u(ctx, hist, u_keys)
        breadth = sum(1 for x in r1 if x > 0) / len(r1) if r1 else 0.0
        ad = alpha_days(p.h_e_days, p.alpha_tail)
        lat = p.lat_frac
        tx_rt = int(self.exec_cfg.buy_tx_fee_rao) + int(self.exec_cfg.sell_tx_fee_rao)
        out: dict[SubnetKey, MomRow] = {}
        for k in keys:
            f = frame.feats[k]
            s = raw.get(k)
            sv = ctx.view.get(k) or s
            if s is None or sv is None:
                continue
            h = yield_hotkey(ctx, f)
            c = candidate(f, h)
            idx = s.hotkey(h) if h is not None else None
            y_n = 0.0
            if c is not None and idx is not None:
                y_n = min(float(closed_form_yield_net(s, raw.glob, idx)), c.score_ppm_day / PPM)
            s_score = s_of.get(k)
            micro = int(s.pool.tao) < tao_to_rao(p.micro_cut_tao)
            eh = p.h_e_days * (m_u + y_n) + p.b_s_per_z_day * (s_score or 0.0) * ad
            pool = sv.pool
            f_fee = pool.fee_rate / FEE_DEN
            phi = 2.0 * f_fee + 2.0 * lat
            edge_ppm = max(frac_to_ppm(eh - phi), 0)
            lam_ppm = frac_to_ppm(p.lam)
            v_edge = int(pool.px_tao) * edge_ppm // (4 * lam_ppm) if lam_ppm > 0 else 0
            rho = p.rho_micro_frac if micro else p.rho_near_frac
            v = min(v_edge, int(pool.tao) * frac_to_ppm(rho) // PPM, int(v_max(pool.tao, frac_to_ppm(p.s_exit_frac))),
                    budget * frac_to_ppm(p.w_max_frac) // PPM)
            v = max(v, 0)
            pos = hold.get(k)
            rt_size = v if v > 0 else (int(pos.value_rao) if pos is not None else 0)
            rt: int | None = None
            ne: float | None = None
            if rt_size > 0:
                try:
                    rt = round_trip_cost_ppm(pool, Rao(rt_size), ImpactBound.TEMPORARY, tx_rt)
                    ne = eh - rt / PPM - 2.0 * lat
                except SwapError:
                    rt, ne = None, None
            if v <= 0:
                ne = None
            out[k] = MomRow(key=k, hotkey=h, universe_failed=uni[k], s_score=s_score, rank=rank_of.get(k),
                            r_24h=f.ret_1d, y_n=y_n, eh=eh, v_rao=v, rt_ppm=rt, net_edge=ne, micro=micro)
        return out, m_u, breadth

