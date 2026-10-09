"""WP9 strategies inside the WP7 Engine (DESIGN.md sections 4.4, 5.10, 5.13 note 4): the Engine runs them on its cadence
and wake rules, journals their memories in DecisionTrace and decodes them back with
codec.decode_bytes(type(initial_memory()), raw) on the next run, with no strategy errors. The router, caps,
allocator, overlay and planner are pass-through fakes (WP8 is tested separately)."""
from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from taotrader.core import codec
from taotrader.core.config import BookCfg, ExecCfg, RiskCfg, SleeveCfg
from taotrader.core.events import CapitalChanged, ChainEventObserved, DecisionTrace, HealthObs, SnapshotObserved
from taotrader.core.orders import OrderIntent
from taotrader.core.protocols import RiskContext, RouterState, TickContext
from taotrader.core.signals import RiskDecision, StrategyOutput, TargetBook
from taotrader.core.units import BookId, LogicalTime, Phase, Ppm, Rao, RunMode, Stage, StrategyId, SubnetKey
from taotrader.engine.engine import Engine
from taotrader.engine.reducer import fold_batch
from taotrader.protocol.derive import derive_events
from taotrader.strategies.base import build_strategy

TAO = 10**9


def _router(ctx: TickContext, risk: RiskCfg) -> RouterState:
    return ctx.book_view.router


def _caps(ctx: TickContext, risk: RiskCfg) -> dict[SubnetKey, Rao]:
    return {}


def _allocator(signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext, caps: dict[SubnetKey, Rao]) -> TargetBook:
    return TargetBook(asof=ctx.block, items=())


class _Overlay:
    def review(self, proposal: TargetBook, ctx: RiskContext) -> RiskDecision:
        return RiskDecision(targets=proposal, actions=(), mode=ctx.tick.mode)


def _planner(decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey], run_id: str,
             book: BookId) -> tuple[OrderIntent, ...]:
    return ()


def test_strategies_run_and_their_memories_round_trip_through_the_engine(sx: SimpleNamespace) -> None:
    specs = [sx.Spec(10 + i, reg_at=8_000_000 + i, feat={"ret_1d": 0.001 * (i + 1), "ret_7d": 0.01 * (i + 1),
                                                          "flow_1d": 0.001 * (i + 1)}) for i in range(6)]
    m = sx.market(specs)
    sleeves = tuple(SleeveCfg(StrategyId(sid), Stage.RESEARCH, Ppm(200_000))
                    for sid in ("baseline.ew_total", "baseline.random_entry", "carry", "lcw", "momentum"))
    cfg = BookCfg(book=BookId("b1"), capital_rao=Rao(1_000 * TAO), fee_float_rao=Rao(TAO), sleeves=sleeves)
    strategies = [build_strategy(s, exec_cfg=ExecCfg(), risk=RiskCfg()) for s in sleeves]
    engine = Engine(run_id="wp9", cfg=cfg, mode=RunMode.BACKTEST, strategies=strategies, router=_router, caps=_caps,
                    allocator=_allocator, overlay=_Overlay(), planner=_planner)
    state = engine.initial_state()
    store = m.store(sx.B - 60, 8_400)
    prev: Any = None
    traces: list[DecisionTrace] = []
    for b in (sx.B, sx.B + 60, sx.B + 300, sx.B + 360):
        snap = m.snap(b)
        store.add(snap)
        store.clock = snap.block
        events = derive_events(prev, snap)
        frame = m.frame(b)
        h = HealthObs.nominal()
        run: list[Any] = [SnapshotObserved(snap.block, snap.block_hash, snap.digest, snap.plan, 0, h)]
        run += [ChainEventObserved(e) for e in events]
        items = [(LogicalTime(snap.block, Phase.INGEST, i), BookId(""), e) for i, e in enumerate(run)]
        if not state.funded:
            items.append((LogicalTime(snap.block, Phase.INGEST, 0), engine.book,
                          CapitalChanged(engine.book, snap.block, cfg.capital_rao, cfg.fee_float_rao, "initial")))
        outs = engine.decide(state, snap, prev, events, frame, snap, h, inputs=items, store=store)
        state = fold_batch(state, [e for _, _, e in items + outs])
        traces += [e for _, _, e in outs if isinstance(e, DecisionTrace)]
        prev = snap
    assert [int(t.block) for t in traces] == [sx.B, sx.B + 60, sx.B + 300, sx.B + 360]
    errors = [a for t in traces for a in t.actions if a.rule.startswith("engine.strategy")]
    assert errors == [], errors
    first = traces[0]
    assert set(first.strategies_run) == {s.strategy for s in sleeves}
    assert {s.strategy for s in first.signals} >= {StrategyId("baseline.ew_total"), StrategyId("carry")}
    # the next scheduled run decodes every journaled memory and runs without an error
    third = traces[2]
    assert StrategyId("carry") in third.strategies_run and StrategyId("momentum") in third.strategies_run
    for strat in strategies:
        for sid, raw in third.memories:
            if sid == strat.id:
                assert codec.canonical_bytes(codec.decode_bytes(type(strat.initial_memory()), raw)) == raw
