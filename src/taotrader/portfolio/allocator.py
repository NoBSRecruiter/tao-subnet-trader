"""taotrader/portfolio/allocator.py - StandardAllocator: sleeves -> per-subnet aggregate targets (WP8; DESIGN.md 3.10).

`StandardAllocator(book_cfg, run_mode=..., live_sleeves=...)(signals, ctx, caps) -> TargetBook` (core.protocols.Allocator).
All values are executable (one-shot sim_sell) TAO in rao on ctx.view.

1. Budgets by evidence stage, never by P&L: B_s = G_MAX_EFF * NAV_liq * budget_s * m_stage * m_kill, where G_MAX_EFF
   carries the DD30 governor (and m_regime when regime_throttle_active; risk.modes.g_max_eff_ppm).
   - m_stage: backtest books 1 for every stage; paper books RESEARCH/SHADOW 0, PAPER/LIVE_ELIGIBLE 1; live and
     live-dry books 1 only for LIVE_ELIGIBLE sleeves listed in [live].sleeves (`live_sleeves`), else 0.
   - Outside backtests a PAPER-or-later sleeve budget is capped at B_MAX = 40%; an LCW sleeve at 5% everywhere.
   - m_kill: sleeve kill state from BookView.sleeve_stats (ACTIVE 1, REDUCED 0.5, SUSPENDED 0 -> unwound).
   - The post-spec burn-in m_burnin (0.5) is applied by the overlay, the only stage that sees
     RiskContext.burn_in_until (ADR request: expose it in TickContext/BookView).
   - Inverse-vol budgets (after 60 paper days AND FT8) are not enabled: fixed budgets.
2. Sleeve targets: value_{s,k} = min(B_s * weight, max_size_rao, V*(alpha_h) if alpha_h > 0), V* on the view's pool;
   x m_gate * m_trd when gate_haircuts_active and the signal does not declare dilution. EXIT zeroes the sleeve's
   share, AVOID forbids its increase, signals past asof + horizon_blocks (> 0) are ignored, a held (sleeve, key) with
   no live TARGET signal gets 0.
3. Sum-then-cap per subnet with the CapsFn's V_cap: over the cap, existing sleeve holdings keep priority (pro rata to
   their current value, i.e. shares), then higher stage, then pro rata to the requested increase.
4. Portfolio constraints: N_eff (drop the names with the lowest sum of edge_ppm_day * value), then the section I
   buckets and the gross cap (risk.liquidity.apply_aggregates, increases cut first); the cut is pushed back to the
   sleeves, increasing sleeves first. A sleeve's increase is also limited to its own sleeve cash.
5. Netting: sleeves that shrink sell to sleeves that grow at the decision spot of the view, inside the virtual ledger
   (TargetBook.transfers, sorted by (key, from, to); shares exact, TAO = floor(value_of(shares) * spot / 1e9)); only
   the net reaches the pool.
6. Output: one TargetPosition per generation with a positive target or a holding, with the book's router hotkey
   (BookView.router after the router step; else the position's hotkey; for a new name risk.router.entry_hotkey, and no
   entry without one), the max signal urgency, the attribution of the net change (buyers for a net increase, sellers
   for a net decrease, holders otherwise) and the reason "alpha_h_ppm=<n>" (value-weighted alpha_h of the increasing
   sleeves, when all of them supplied one) for the planner's benefit check.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from ..core.config import BookCfg, SleeveCfg
from ..core.fixed import DEC
from ..core.orders import Urgency
from ..core.protocols import TickContext
from ..core.signals import Signal, SignalKind, SleeveXfer, StrategyOutput, TargetBook, TargetPosition
from ..core.units import PPM, RAO_PER_TAO, Hotkey, PriceRao, Rao, RunMode, Stage, StrategyId, SubnetKey
from ..protocol.amm import v_star
from ..risk.liquidity import (
    Holding,
    aggregate_limits,
    apply_aggregates,
    attribution_from_weights,
    holdings,
    is_lcw,
    monitored,
    pool_spot_rao,
    sleeve_values,
)
from ..risk.modes import g_max_eff_ppm, sleeve_budget_mult_ppm
from ..risk.regime_throttle import RegimeThrottle
from ..risk.router import entry_hotkey

__all__ = ["B_MAX_PPM", "LCW_BUDGET_MAX_PPM", "StandardAllocator"]

B_MAX_PPM: Final[int] = 400_000
LCW_BUDGET_MAX_PPM: Final[int] = 50_000
ALPHA_H_REASON: Final[str] = "alpha_h_ppm="


@dataclass(frozen=True, slots=True)
class _Want:
    sid: StrategyId
    key: SubnetKey
    value: int
    edge_ppm_day: int
    alpha_h_ppm: int
    urgency: Urgency
    stage: Stage


def _split_pro_rata(total: int, weights: Sequence[tuple[StrategyId, int]]) -> dict[StrategyId, int]:
    """Split `total` by non-negative weights (floored, remainder to the largest weight, ties lowest id)."""
    ws = [(s, w) for s, w in sorted(weights) if w > 0]
    out = {s: 0 for s, _ in sorted(weights)}
    tw = sum(w for _, w in ws)
    if total <= 0 or tw <= 0:
        return out
    for s, w in ws:
        out[s] = total * w // tw
    rest = total - sum(out.values())
    if rest:
        top = min(ws, key=lambda x: (-x[1], x[0]))[0]
        out[top] += rest
    return out


class StandardAllocator:
    """The Allocator of section 3.10 (pure; constructed per book)."""

    def __init__(self, book: BookCfg, *, run_mode: RunMode = RunMode.BACKTEST, live_sleeves: Sequence[StrategyId] = (),
                 regime: RegimeThrottle | None = None) -> None:
        self.book = book
        self.risk = book.risk
        self.run_mode = run_mode
        self.live_sleeves = tuple(sorted(live_sleeves))
        self.regime = regime if regime is not None else RegimeThrottle()

    # ------------------------------------------------------------------ budgets
    def m_stage_ppm(self, sl: SleeveCfg) -> int:
        if self.run_mode is RunMode.BACKTEST:
            return PPM
        if self.run_mode is RunMode.PAPER:
            return PPM if sl.stage >= Stage.PAPER else 0
        return PPM if sl.stage is Stage.LIVE_ELIGIBLE and sl.strategy in self.live_sleeves else 0

    def budget_ppm(self, sl: SleeveCfg) -> int:
        b = int(sl.budget_ppm)
        if self.run_mode is not RunMode.BACKTEST and sl.stage >= Stage.PAPER:
            b = min(b, B_MAX_PPM)
        if is_lcw(sl.strategy):
            b = min(b, LCW_BUDGET_MAX_PPM)
        return max(b, 0)

    def budget_rao(self, sl: SleeveCfg, ctx: TickContext, g_eff_ppm: int) -> int:
        kill = next((sleeve_budget_mult_ppm(st.state) for st in ctx.book_view.sleeve_stats if st.strategy == sl.strategy), PPM)
        nav = max(int(ctx.nav_liq), 0)
        return nav * g_eff_ppm // PPM * self.budget_ppm(sl) // PPM * self.m_stage_ppm(sl) // PPM * kill // PPM

    # ------------------------------------------------------------------ the allocator
    def __call__(self, signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext,
                 caps: dict[SubnetKey, Rao]) -> TargetBook:
        cfg = self.risk
        b = int(ctx.block)
        view = ctx.view
        regime_ppm = self.regime.reading(ctx.store, ctx.raw, cfg).m_regime_ppm if cfg.regime_throttle_active else None
        g_eff = g_max_eff_ppm(cfg, ctx.book_view, int(ctx.nav_liq), b, regime_ppm)
        hold = holdings(ctx)
        cur_sv = sleeve_values(ctx.portfolio, hold)
        stages: dict[StrategyId, Stage] = {}
        wants: dict[tuple[StrategyId, SubnetKey], _Want] = {}

        for sl, out in sorted(signals, key=lambda x: x[0].strategy):
            sid = sl.strategy
            stages[sid] = sl.stage
            budget = self.budget_rao(sl, ctx, g_eff)
            by_key: dict[SubnetKey, list[Signal]] = {}
            for sg in out.signals:
                if sg.strategy != sid or (sg.horizon_blocks > 0 and int(sg.asof) + sg.horizon_blocks <= b):
                    continue
                by_key.setdefault(sg.key, []).append(sg)
            keys = set(by_key) | {k for (s, k) in cur_sv if s == sid}
            for key in sorted(keys):
                wants[(sid, key)] = self._sleeve_target(sid, sl.stage, key, by_key.get(key, []), budget,
                                                        cur_sv.get((sid, key), 0), ctx)
        for (sid, key) in sorted(cur_sv):                  # holdings of sleeves without a signal list (e.g. "book")
            if (sid, key) not in wants:
                wants[(sid, key)] = _Want(sid, key, 0, 0, 0, Urgency.NORMAL, stages.get(sid, Stage.RESEARCH))

        keys_all = sorted({k for (_, k) in wants} | set(hold))
        alloc: dict[tuple[StrategyId, SubnetKey], int] = {}
        for key in keys_all:
            rows = [w for (s, k), w in sorted(wants.items(), key=lambda kv: kv[0]) if k == key]
            alloc.update(self._cap_key(key, rows, cur_sv, int(caps.get(key, Rao(0)))))
        self._cash_limit(alloc, cur_sv, ctx)
        self._no_validator(alloc, hold, ctx)

        totals = {k: sum(v for (s, kk), v in alloc.items() if kk == k) for k in keys_all}
        current = {k: int(h.value) for k, h in hold.items()}
        limits = aggregate_limits(int(ctx.nav_liq), g_eff, cfg)
        self._n_eff(alloc, totals, wants, limits.n_eff)
        lcw = {k: sum(v for (s, kk), v in alloc.items() if kk == k and is_lcw(s)) * PPM // max(totals[k], 1)
               for k in keys_all}
        new_totals, _ = apply_aggregates(totals, current, view, ctx.raw, cfg, limits, lcw)
        for k in keys_all:
            if new_totals.get(k, 0) < totals[k]:
                self._shrink(alloc, cur_sv, k, new_totals.get(k, 0))
                totals[k] = new_totals.get(k, 0)

        transfers: list[SleeveXfer] = []
        items: list[TargetPosition] = []
        for key in keys_all:
            per_sleeve = {sid: v for (sid, k), v in alloc.items() if k == key}
            total = sum(per_sleeve.values())
            h = hold.get(key)
            if total <= 0 and h is None:
                continue
            xf, weights, alpha_h = self._net(key, per_sleeve, cur_sv, wants, h, ctx)
            transfers.extend(xf)
            hotkey = self._hotkey(key, h, ctx)
            if hotkey is None:
                continue
            urg = max((wants[(sid, key)].urgency for sid in per_sleeve if (sid, key) in wants), default=Urgency.NORMAL)
            reasons: tuple[str, ...] = ("alloc",)
            if alpha_h > 0:
                reasons += (f"{ALPHA_H_REASON}{alpha_h}",)
            items.append(TargetPosition(key=key, hotkey=hotkey, value_rao=Rao(max(total, 0)), urgency=urg,
                                        attribution=attribution_from_weights(weights), reasons=reasons))
        transfers.sort(key=lambda x: (x.key, x.from_strategy, x.to_strategy))
        return TargetBook(asof=ctx.block, items=tuple(items), transfers=tuple(transfers))

    # ------------------------------------------------------------------ steps
    def _sleeve_target(self, sid: StrategyId, stage: Stage, key: SubnetKey, sigs: Sequence[Signal], budget: int,
                       cur: int, ctx: TickContext) -> _Want:
        s = ctx.view.get(key)
        tgt = max((g for g in sigs if g.kind is SignalKind.TARGET), key=lambda g: (int(g.asof), g.weight_ppm),
                  default=None)
        exit_sig = [g for g in sigs if g.kind is SignalKind.EXIT]
        avoid = any(g.kind is SignalKind.AVOID for g in sigs)
        urgency = max((g.urgency for g in sigs), default=Urgency.NORMAL)
        if exit_sig or s is None or tgt is None:
            return _Want(sid, key, 0, 0, 0, urgency, stage)
        v = budget * max(int(tgt.weight_ppm), 0) // PPM
        if tgt.max_size_rao is not None:
            v = min(v, int(tgt.max_size_rao))
        if tgt.alpha_h_ppm > 0:
            v = min(v, int(v_star(s.pool, int(tgt.alpha_h_ppm))))
        if self.risk.gate_haircuts_active and not tgt.declares_dilution:
            mon = monitored(ctx.frame.feats.get(key), self.risk)
            v = v * mon.m_gate_ppm // PPM * mon.m_trd_ppm // PPM
        if avoid:
            v = min(v, cur)
        return _Want(sid, key, max(v, 0), int(tgt.edge_ppm_day), int(tgt.alpha_h_ppm), urgency, stage)

    @staticmethod
    def _cap_key(key: SubnetKey, rows: Sequence[_Want], cur_sv: Mapping[tuple[StrategyId, SubnetKey], int],
                 cap: int) -> dict[tuple[StrategyId, SubnetKey], int]:
        """Sum-then-cap: holdings first (pro rata to current value), then higher stage, then pro rata."""
        total = sum(w.value for w in rows)
        if total <= max(cap, 0):
            return {(w.sid, key): w.value for w in rows}
        cap = max(cap, 0)
        keep = [(w.sid, min(w.value, cur_sv.get((w.sid, key), 0))) for w in rows]
        keep_total = sum(v for _, v in keep)
        if keep_total >= cap:
            part = _split_pro_rata(cap, keep)
            return {(w.sid, key): min(part.get(w.sid, 0), dict(keep)[w.sid]) for w in rows}
        out = {(w.sid, key): v for (w, (_, v)) in zip(rows, keep, strict=True)}
        room = cap - keep_total
        for stage in sorted({w.stage for w in rows}, reverse=True):
            incs = [(w.sid, w.value - out[(w.sid, key)]) for w in rows if w.stage == stage and w.value > out[(w.sid, key)]]
            need = sum(v for _, v in incs)
            if need <= 0:
                continue
            if need <= room:
                for sid, v in incs:
                    out[(sid, key)] += v
                room -= need
            else:
                part = _split_pro_rata(room, incs)
                for sid, v in incs:
                    out[(sid, key)] += min(part.get(sid, 0), v)
                room = 0
            if room <= 0:
                break
        return out

    @staticmethod
    def _cash_limit(alloc: dict[tuple[StrategyId, SubnetKey], int], cur_sv: Mapping[tuple[StrategyId, SubnetKey], int],
                    ctx: TickContext) -> None:
        """A sleeve's total increase may not exceed its own sleeve cash (keys in order)."""
        cash = {sid: max(int(c), 0) for sid, c in ctx.portfolio.sleeve_cash}
        if not cash:
            return
        for (sid, key) in sorted(alloc):
            inc = alloc[(sid, key)] - cur_sv.get((sid, key), 0)
            if inc <= 0:
                continue
            avail = cash.get(sid, 0)
            take = min(inc, avail)
            alloc[(sid, key)] = cur_sv.get((sid, key), 0) + take
            cash[sid] = avail - take

    def _no_validator(self, alloc: dict[tuple[StrategyId, SubnetKey], int], hold: Mapping[SubnetKey, Holding],
                      ctx: TickContext) -> None:
        """No entry without a router hotkey (section 3.2 G): new names without one get 0."""
        for (sid, key) in sorted(alloc):
            if key not in hold and alloc[(sid, key)] > 0 and self._hotkey(key, None, ctx) is None:
                alloc[(sid, key)] = 0

    @staticmethod
    def _n_eff(alloc: dict[tuple[StrategyId, SubnetKey], int], totals: dict[SubnetKey, int],
               wants: Mapping[tuple[StrategyId, SubnetKey], _Want], n_eff: int) -> None:
        names = [k for k in sorted(totals) if totals[k] > 0]
        if len(names) <= n_eff:
            return

        def score(k: SubnetKey) -> int:
            return sum(wants[(s, kk)].edge_ppm_day * v for (s, kk), v in alloc.items() if kk == k and (s, kk) in wants)

        for k in sorted(names, key=lambda k: (score(k), totals[k], k))[:len(names) - max(n_eff, 0)]:
            for sk in [x for x in sorted(alloc) if x[1] == k]:
                alloc[sk] = 0
            totals[k] = 0

    @staticmethod
    def _shrink(alloc: dict[tuple[StrategyId, SubnetKey], int], cur_sv: Mapping[tuple[StrategyId, SubnetKey], int],
                key: SubnetKey, new_total: int) -> None:
        """Lower the sleeves' values on `key` to sum to new_total: increases first (pro rata), then holdings."""
        rows = sorted(sk for sk in alloc if sk[1] == key)
        excess = sum(alloc[sk] for sk in rows) - max(new_total, 0)
        if excess <= 0:
            return
        incs = [(sk[0], alloc[sk] - min(alloc[sk], cur_sv.get(sk, 0))) for sk in rows]
        total_inc = sum(v for _, v in incs)
        cut = _split_pro_rata(min(excess, total_inc), incs)
        for sk in rows:
            alloc[sk] -= cut.get(sk[0], 0)
        excess -= sum(cut.values())
        if excess > 0:
            cut2 = _split_pro_rata(excess, [(sk[0], alloc[sk]) for sk in rows])
            for sk in rows:
                alloc[sk] -= min(cut2.get(sk[0], 0), alloc[sk])

    def _hotkey(self, key: SubnetKey, h: Holding | None, ctx: TickContext) -> Hotkey | None:
        chosen = ctx.book_view.router.hotkey(key)
        if chosen is not None:
            return chosen
        if h is not None:
            return h.hotkey
        return entry_hotkey(ctx, key, self.risk)

    def _net(self, key: SubnetKey, rows: Mapping[StrategyId, int], cur_sv: Mapping[tuple[StrategyId, SubnetKey], int],
             wants: Mapping[tuple[StrategyId, SubnetKey], _Want], h: Holding | None,
             ctx: TickContext) -> tuple[list[SleeveXfer], list[tuple[StrategyId, int]], int]:
        """Netting transfers on `key`, the attribution weights of the net change, and the alpha_h of the increase."""
        sids = sorted(set(rows) | {s for (s, k) in cur_sv if k == key})
        give = [(s, cur_sv.get((s, key), 0) - rows.get(s, 0)) for s in sids if cur_sv.get((s, key), 0) > rows.get(s, 0)]
        take = [(s, rows.get(s, 0) - cur_sv.get((s, key), 0)) for s in sids if rows.get(s, 0) > cur_sv.get((s, key), 0)]
        xfers: list[SleeveXfer] = []
        s_view = ctx.view.get(key)
        idx = s_view.hotkey(h.hotkey) if s_view is not None and h is not None else None
        spot = pool_spot_rao(s_view.pool) if s_view is not None else 0
        sleeve_shares = {sh.strategy: sh.shares for sh in ctx.portfolio.sleeves if sh.key == key}
        gi, ti = list(give), list(take)
        i = j = 0
        matched_give: dict[StrategyId, int] = {}
        matched_take: dict[StrategyId, int] = {}
        if h is not None and idx is not None and int(h.value) > 0 and h.shares > 0 and spot > 0:
            while i < len(gi) and j < len(ti):
                (fs, fv), (ts, tv) = gi[i], ti[j]
                v = min(fv, tv)
                shares = DEC.divide(DEC.multiply(Decimal(v), h.shares), Decimal(int(h.value)))
                if v == fv and rows.get(fs, 0) <= 0:          # an exiting seller hands over all its remaining shares
                    shares = sleeve_shares.get(fs, Decimal(0))
                shares = min(shares, sleeve_shares.get(fs, Decimal(0)))
                if shares > 0:
                    alpha = int(idx.value_of(shares))
                    tao = alpha * spot // RAO_PER_TAO
                    if tao > 0:
                        xfers.append(SleeveXfer(key=key, from_strategy=fs, to_strategy=ts, shares=shares, tao=Rao(tao),
                                                price=PriceRao(spot)))
                        sleeve_shares[fs] = DEC.subtract(sleeve_shares.get(fs, Decimal(0)), shares)
                        matched_give[fs] = matched_give.get(fs, 0) + v
                        matched_take[ts] = matched_take.get(ts, 0) + v
                gi[i] = (fs, fv - v)
                ti[j] = (ts, tv - v)
                if gi[i][1] <= 0:
                    i += 1
                if ti[j][1] <= 0:
                    j += 1
        net = sum(rows.values()) - sum(cur_sv.get((s, key), 0) for s in sids)
        if net > 0:
            weights = [(s, v - matched_take.get(s, 0)) for s, v in take]
        elif net < 0:
            weights = [(s, v - matched_give.get(s, 0)) for s, v in give]
        else:
            weights = [(s, rows.get(s, 0)) for s in sids]
        if sum(max(w, 0) for _, w in weights) <= 0:
            weights = [(s, rows.get(s, 0) or cur_sv.get((s, key), 0)) for s in sids]
        incs = [(s, v) for s, v in take if v > 0]
        alpha_h = 0
        if incs and all(wants.get((s, key)) is not None and wants[(s, key)].alpha_h_ppm > 0 for s, _ in incs):
            tot = sum(v for _, v in incs)
            alpha_h = sum(wants[(s, key)].alpha_h_ppm * v for s, v in incs) // tot
        return xfers, [(s, max(w, 0)) for s, w in weights], alpha_h


if TYPE_CHECKING:
    from ..core.protocols import Allocator

    def _conforms(a: StandardAllocator) -> Allocator:      # mypy: structural conformance to core.protocols.Allocator
        return a
