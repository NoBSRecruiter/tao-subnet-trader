"""engine.engine: Engine.decide phases, cadence, pipeline order, mode filters, fail-closed behaviour (DESIGN.md 4.4)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.config import SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import (
    CapitalChanged,
    ChainEventKind,
    ChainEventObserved,
    DecisionTrace,
    DeregSettled,
    FillReported,
    HealthObs,
    ModeChanged,
    OperatorCommand,
    OrderCancelled,
    OrderIntended,
    SleeveTransfer,
    SnapshotObserved,
    SubmitStarted,
    VenueAck,
    YieldAccrued,
)
from taotrader.core.orders import OrderKind, OrderState, Urgency
from taotrader.core.protocols import RouterState
from taotrader.core.signals import RiskAction, RiskDecision, SleeveXfer, TargetBook
from taotrader.core.units import Block, BookId, LogicalTime, Mode, Phase, Ppm, PriceRao, Rao, RunMode, Stage, StrategyId
from taotrader.engine.engine import parse_dereg_model
from taotrader.engine.reducer import (
    ENGINE_FORCED_EXIT,
    ENGINE_ORDER_SPOT,
    ROUTER_MEMORY_ID,
    EngineState,
    check_state,
    fold_batch,
    value_at,
)
from taotrader.protocol.calibration import Calibration, sealed
from taotrader.protocol.derive import derive_events
from taotrader.protocol.prune import HazardModel, recovery_ratio
from taotrader.protocol.sellload import SellLoadParams

TAO = 10**9
CARRY = StrategyId("carry")


class Harness:
    """Runner stand-in without venues: builds the tick inputs, calls decide, folds inputs + outputs."""

    def __init__(self, fx: Any, engine: Any, store: Any) -> None:
        self.fx, self.engine, self.store = fx, engine, store
        self.state: EngineState = engine.initial_state()
        self.prev: Any = None
        self.features = fx.FakeFeatures()
        self.batches: list[list[Any]] = []

    def step(self, snap: Any, *, extra: tuple[Any, ...] = (), health: HealthObs | None = None, warm: bool = True) -> list[Any]:
        b = snap.block
        self.store.add(snap)
        self.store.clock = b
        h = health if health is not None else HealthObs.nominal()
        events = derive_events(self.prev, snap)
        frame = self.features.update(snap, events)
        frame = replace(frame, warm=warm)
        run = [SnapshotObserved(b, snap.block_hash, snap.digest, snap.plan, 0, h)] + [ChainEventObserved(e) for e in events]
        run += list(extra)
        items = [(LogicalTime(b, Phase.INGEST, i), BookId(""), e) for i, e in enumerate(run)]
        if not self.state.funded:
            cfg = self.engine.cfg
            items.append((LogicalTime(b, Phase.INGEST, 0), self.engine.book,
                          CapitalChanged(self.engine.book, b, cfg.capital_rao, cfg.fee_float_rao, "initial")))
        outs = self.engine.decide(self.state, snap, self.prev, events, frame, snap, h, inputs=items, store=self.store)
        self.state = fold_batch(self.state, [e for _, _, e in items + outs])
        self.batches.append(outs)
        self.prev = snap
        return outs

    def fold(self, *evs: Any) -> None:
        self.state = fold_batch(self.state, list(evs))


def events_of(outs: list[Any], cls: type) -> list[Any]:
    return [e for _, _, e in outs if isinstance(e, cls)]


def trace_of(outs: list[Any]) -> DecisionTrace:
    (t,) = events_of(outs, DecisionTrace)
    return t


def hold(h: Harness, fx: Any, block: int, key: Any = None, alpha: int = 1_000 * TAO) -> None:
    """Fold a filled buy of `alpha` (shares at index 1) on `key` into the harness state."""
    key = key if key is not None else fx.K7
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, block, key, tao_in=10 * TAO)
    h.fold(OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim0", None, Block(block + 12)),
           VenueAck(i.book, i.order_id, 0, Block(block + 3), Block(block + 5), "", ""),
           FillReported(fx.mk_fill(i, block, tao=10 * TAO, alpha=alpha)))


@pytest.fixture()
def mk(fx, market, make_subnet, make_snapshot):
    def build(strategies=None, **kw):
        strategies = strategies if strategies is not None else [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})])]
        cfg = kw.pop("cfg", None) or fx.book_cfg(dereg_model=kw.pop("dereg_model", "formula"))
        eng = fx.make_engine(cfg, strategies, **kw)
        return Harness(fx, eng, fx.FakeStore())
    return build


# ------------------------------------------------------------------------------------------------- ACCOUNT
def test_yield_accrues_in_account_before_the_decision(fx, market, mk) -> None:
    snaps = market(8)                                            # START .. START+420; drain at START+360
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])])
    h.step(snaps[0])
    hold(h, fx, fx.START + 5)
    for s in snaps[1:6]:
        assert events_of(h.step(s), YieldAccrued) == []          # flat index: nothing to accrue
    outs = h.step(snaps[6])                                      # START+360: the index grew 0.03%
    (y,) = events_of(outs, YieldAccrued)
    t0 = outs[0][0]
    assert t0.phase is Phase.ACCOUNT and outs[0][2] is y
    idx = fx.hk_idx(fx.HK_A, fx.START + 360).index()
    assert y.delta_alpha == value_at(Decimal(1_000 * TAO), idx) - 1_000 * TAO == 300_000_000
    assert y.index_before == Decimal(1) and y.index_after == idx
    assert fx.position_alpha(h.state, fx.K7, fx.HK_A) == 1_000_300_000_000 and check_state(h.state) == []
    # NAV_liq of the same tick already values the accrued yield
    assert trace_of(outs).nav_liq > h.state.portfolio.cash


def test_dereg_settled_from_the_last_good_snapshot_outside_live(fx, market, mk) -> None:
    gone = fx.START + 120
    snaps = market(4, mutate=lambda b, subs: subs.pop() and None if b >= gone else None)
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])])
    h.step(snaps[0])
    hold(h, fx, fx.START + 5, key=fx.K9, alpha=500 * TAO)
    h.step(snaps[1])
    outs = h.step(snaps[2])                                      # K9 vanished (DEREGISTERED at gone)
    (d,) = events_of(outs, DeregSettled)
    s_prev = snaps[1].get(fx.K9)
    r = recovery_ratio(s_prev, snaps[1].glob, Decimal("0.35"))
    assert d.alpha_value == 500 * TAO and d.model == "formula"
    assert d.payout_tao == int(Decimal(500 * TAO) * s_prev.pool.spot() * r)
    assert h.state.portfolio.position(fx.K9) is None and h.state.dissolving == () and check_state(h.state) == []


def test_fixed_dereg_model_and_live_mode_never_settles(fx, market, mk) -> None:
    gone = fx.START + 120
    snaps = market(4, mutate=lambda b, subs: subs.pop() and None if b >= gone else None)
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])], dereg_model="fixed:350000")
    h.step(snaps[0])
    hold(h, fx, fx.START + 5, key=fx.K9, alpha=500 * TAO)
    h.step(snaps[1])
    (d,) = events_of(h.step(snaps[2]), DeregSettled)
    assert d.model == "fixed:0.35" and d.payout_tao == int(Decimal(500 * TAO) * snaps[1].get(fx.K9).pool.spot() * Decimal("0.35"))

    live = mk(strategies=[fx.TargetStrategy(CARRY, [])], mode=RunMode.LIVE)
    live.step(snaps[0])
    hold(live, fx, fx.START + 5, key=fx.K9, alpha=500 * TAO)
    live.step(snaps[1])
    outs = live.step(snaps[2])
    assert events_of(outs, DeregSettled) == []                   # reconciliation journals the observed payout
    assert live.state.dissolving == (fx.K9,) and live.state.portfolio.position(fx.K9) is not None


def test_parse_dereg_model() -> None:
    assert parse_dereg_model("formula") == ("formula", None)
    assert parse_dereg_model("fixed:650000") == ("fixed:0.65", Decimal("0.65"))
    with pytest.raises(ValueError):
        parse_dereg_model("fixed:0.35")


# ------------------------------------------------------------------------------------------------- cadence
def test_strategy_cadence_uses_absolute_block_buckets_and_wake_events(fx, market, mk) -> None:
    every300 = fx.TargetStrategy(CARRY, [], decide_every_blocks=300)
    woken = fx.TargetStrategy(StrategyId("lcw"), [], decide_every_blocks=7_200, wake_on=frozenset({ChainEventKind.EPOCH_DRAIN}))
    cfg = fx.book_cfg(sleeves=[SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(StrategyId("lcw"), Stage.PAPER, Ppm(1))])
    h = mk(strategies=[every300, woken], cfg=cfg)
    for s in market(14):
        h.step(s)
    assert every300.calls == [fx.START, fx.START + 300, fx.START + 600]
    assert woken.calls == [fx.START, fx.START + 360, fx.START + 720]   # first tick, then each EPOCH_DRAIN
    runs = [list(trace_of(o).strategies_run) for o in h.batches]
    assert runs[0] == [CARRY, StrategyId("lcw")] and runs[1] == []


def test_valid_from_and_warm_gate_strategies_but_not_the_overlay(fx, market, mk) -> None:
    late = fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})], valid_from_block=Block(fx.START + 120))
    overlay = fx.FakeOverlay()
    h = mk(strategies=[late], overlay=overlay)
    snaps = market(4)
    h.step(snaps[0])
    h.step(snaps[1], warm=False)
    assert late.calls == [] and len(overlay.contexts) == 2       # risk review runs every tick regardless
    h.step(snaps[2], warm=False)
    h.step(snaps[3])
    assert late.calls == [fx.START + 180]


# ------------------------------------------------------------------------------------------------- pipeline order
@dataclass
class SpyAllocator:
    seen: list[RouterState] = field(default_factory=list)
    transfers: tuple[SleeveXfer, ...] = ()

    def __call__(self, signals: Any, ctx: Any, caps: Any) -> TargetBook:
        self.seen.append(ctx.book_view.router)
        return TargetBook(asof=ctx.block, items=(), transfers=self.transfers)


def test_router_result_replaces_book_view_router_and_is_journaled_when_changed(fx, market, mk) -> None:
    spy = SpyAllocator()
    h = mk(allocator=spy, planner=lambda *a: ())
    snaps = market(2)
    t0 = trace_of(h.step(snaps[0]))
    assert spy.seen[0].hotkey(fx.K7) == fx.HK_A                  # the allocator saw the router's fresh choice
    assert dict(t0.memories)[ROUTER_MEMORY_ID] == codec.canonical_bytes(spy.seen[0])
    assert h.state.router == spy.seen[0]
    t1 = trace_of(h.step(snaps[1]))
    assert ROUTER_MEMORY_ID not in dict(t1.memories)             # unchanged: not re-journaled


def test_transfers_are_journaled_once_each(fx, market, mk) -> None:
    x = SleeveXfer(fx.K7, CARRY, StrategyId("momentum"), Decimal(5), Rao(7), PriceRao(10**7))
    spy = SpyAllocator(transfers=(x, x))
    h = mk(allocator=spy, planner=lambda *a: ())
    outs = h.step(market(1)[0])
    (t,) = events_of(outs, SleeveTransfer)
    assert (t.key, t.from_strategy, t.to_strategy, t.shares, t.tao) == (fx.K7, CARRY, StrategyId("momentum"), Decimal(5), 7)
    assert [o[0].phase for o in outs if isinstance(o[2], SleeveTransfer)] == [Phase.EMIT]


@dataclass
class RaisingOverlay:
    def review(self, proposal: Any, ctx: Any) -> Any:
        raised = replace(proposal, items=tuple(replace(t, value_rao=Rao(t.value_rao + 1)) for t in proposal.items))
        return RiskDecision(targets=raised, actions=(), mode=ctx.tick.mode)


def test_the_overlay_cannot_raise_a_target(fx, market, mk) -> None:
    seen: list[Any] = []
    h = mk(overlay=RaisingOverlay(), planner=lambda d, *a: seen.append(d) or ())
    t = trace_of(h.step(market(1)[0]))
    clamp = [a for a in t.actions if a.rule == "engine.monotone_clamp"]
    assert clamp and seen[0].targets.items[0].value_rao == fx.book_cfg().capital_rao * 800_000 // 10**6 * 500_000 // 10**6


# ------------------------------------------------------------------------------------------------- modes and filters
def test_caution_drops_buys(fx, market, mk) -> None:
    h = mk(overlay=fx.FakeOverlay(mode=Mode.CAUTION))
    outs = h.step(market(1)[0])
    assert events_of(outs, OrderIntended) == []
    t = trace_of(outs)
    assert t.mode is Mode.CAUTION and any(a.rule == "engine.drop_intent" and "mode:CAUTION" in a.detail for a in t.actions)
    assert events_of(outs, ModeChanged)[0].mode is Mode.CAUTION


def test_exits_only_keeps_forced_sells_and_frozen_keeps_emergency_sells(fx, market, mk) -> None:
    for mode, urgency, kept in [(Mode.EXITS_ONLY, Urgency.URGENT, True), (Mode.FROZEN, Urgency.URGENT, False),
                                (Mode.FROZEN, Urgency.EMERGENCY, True)]:
        overlay = fx.FakeOverlay(forced={fx.K7: (0, urgency, "prune_A")}, mode=mode)
        h = mk(strategies=[fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000, fx.K9: 200_000})])], overlay=overlay)
        h.step(market(1)[0])
        hold(h, fx, fx.START + 5)
        outs = h.step(market(2)[1])
        intents = [e.intent for e in events_of(outs, OrderIntended)]
        assert all(i.kind is OrderKind.REMOVE_STAKE_LIMIT for i in intents)
        assert bool(intents) is kept, (mode, urgency)
        t = trace_of(outs)
        assert any(a.rule == ENGINE_FORCED_EXIT and a.key == fx.K7 and f"urgency={int(urgency)}" in a.detail
                   for a in t.actions)


def test_operator_halt_skips_the_planner_and_cancels_intended_orders(fx, market, mk) -> None:
    planner = fx.FakePlanner()
    h = mk(planner=planner)
    snaps = market(2)
    outs = h.step(snaps[0])
    (i,) = [e.intent for e in events_of(outs, OrderIntended)]    # never submitted (no venue here)
    calls = planner.calls
    halt = OperatorCommand(snaps[1].block, "halt", "kill file", "n1")
    outs = h.step(snaps[1], extra=(halt,))
    assert planner.calls == calls and events_of(outs, OrderIntended) == []
    (c,) = events_of(outs, OrderCancelled)
    assert (c.order_id, c.reason) == (i.order_id, "operator_halt")
    assert events_of(outs, ModeChanged)[0].mode is Mode.FROZEN
    assert next(o for o in h.state.orders if o.intent.order_id == i.order_id).state is OrderState.CANCELLED


def test_stale_intended_orders_are_cancelled_by_ttl_and_pool_gone(fx, market, mk) -> None:
    h = mk()
    snaps = market(3, mutate=lambda b, subs: subs.pop(0) and None if b >= fx.START + 120 else None)
    outs = h.step(snaps[0])
    (i,) = [e.intent for e in events_of(outs, OrderIntended)]
    assert i.key == fx.K7
    outs = h.step(snaps[1])
    assert [c.reason for c in events_of(outs, OrderCancelled)] == ["ttl_expired"]
    h2 = mk()
    h2.step(snaps[0])
    outs = h2.step(snaps[2])                                     # K7 removed from the snapshot
    assert [c.reason for c in events_of(outs, OrderCancelled)] == ["pool_gone"]


def test_orphans_halt_entries(fx, market, mk) -> None:
    h = mk()
    snaps = market(2)
    h.step(snaps[0])
    ghost = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, fx.START, fx.K9, tao_in=TAO)
    h.fold(FillReported(fx.mk_fill(ghost, fx.START + 5, tao=TAO, alpha=TAO)))
    assert h.state.orphans == 1
    h2 = mk()                                                     # same state but a fresh strategy schedule
    h2.state, h2.prev = h.state, h.prev
    h2.state = replace(h2.state, orders=())
    outs = h2.step(snaps[1])
    assert events_of(outs, OrderIntended) == []
    assert any("entries_halted" in a.detail for a in trace_of(outs).actions)


def test_health_and_safe_mode_floors(fx, market, mk) -> None:
    snap = market(1)[0]
    cases = [(HealthObs(3, 50, 2, 0, 0), Mode.CAUTION), (HealthObs(3, 200, 2, 0, 0), Mode.EXITS_ONLY),
             (HealthObs(3, 12, 0, 0, 0), Mode.FROZEN), (HealthObs(40, 12, 2, 0, 0), Mode.CAUTION),
             (HealthObs(3, 12, 1, 0, 0), Mode.CAUTION)]
    for health, mode in cases:
        h = mk()
        assert trace_of(h.step(snap, health=health)).mode is mode
    safe = replace(snap, glob=replace(snap.glob, safe_mode_until=Block(snap.block + 10)))
    h = mk()
    t = trace_of(h.step(safe))
    assert t.mode is Mode.FROZEN and any(a.rule == "engine.mode_floor" and "safe_mode" in a.detail for a in t.actions)


def test_operator_flatten_forces_an_urgent_exit(fx, market, mk) -> None:
    h = mk(strategies=[fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})])])
    snaps = market(2)
    h.step(snaps[0])
    hold(h, fx, fx.START + 5)
    h.state = replace(h.state, orders=())
    outs = h.step(snaps[1], extra=(OperatorCommand(snaps[1].block, "flatten:7", "ops", "f1"),))
    (i,) = [e.intent for e in events_of(outs, OrderIntended)]
    assert i.kind is OrderKind.REMOVE_STAKE_LIMIT and i.full_position and i.urgency is Urgency.URGENT
    assert any(a.rule == ENGINE_FORCED_EXIT and "rule=operator" in a.detail for a in trace_of(outs).actions)


def test_buy_intents_carry_their_decision_spot(fx, market, mk) -> None:
    h = mk()
    outs = h.step(market(1)[0])
    (i,) = [e.intent for e in events_of(outs, OrderIntended)]
    (a,) = [a for a in trace_of(outs).actions if a.rule == ENGINE_ORDER_SPOT]
    assert a.key == i.key and f"spot_rao={market(1)[0].get(fx.K7).pool.spot_rao()}" in a.detail
    assert h.state.chase[0].spot == market(1)[0].get(fx.K7).pool.spot_rao()


def test_duplicate_ids_and_inflight_netuids_are_dropped(fx, market, mk) -> None:
    snap = market(1)[0]
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, int(snap.block), fx.K7, tao_in=TAO)
    j = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, int(snap.block), fx.K7, fx.HK_B, tao_in=TAO)
    h = mk(planner=lambda *a: (i, i, j))
    outs = h.step(snap)
    assert [e.intent.order_id for e in events_of(outs, OrderIntended)] == [i.order_id]
    whys = sorted(a.detail.split("why=")[1] for a in trace_of(outs).actions if a.rule == "engine.drop_intent")
    assert whys == ["duplicate_order_id", "netuid_inflight"]


# ------------------------------------------------------------------------------------------------- fail-closed
class Boom:
    def __call__(self, *a: Any) -> Any:
        raise RuntimeError("bug")

    def review(self, *a: Any) -> Any:
        raise RuntimeError("bug")


@dataclass
class BadStrategy:
    id: StrategyId = CARRY
    decide_every_blocks: int = 60
    wake_on: frozenset[ChainEventKind] = frozenset()
    min_cadence_blocks: int = 60
    valid_from_block: Block = Block(0)
    declares_dilution: bool = False

    def initial_memory(self) -> object:
        return None

    def on_tick(self, ctx: Any, memory: object) -> Any:
        raise ZeroDivisionError("strategy bug")


def test_pipeline_failures_fail_closed_and_are_journaled(fx, market, mk) -> None:
    snap = market(1)[0]
    t = trace_of(mk(overlay=Boom()).step(snap))
    assert t.n_intents == 0 and t.mode >= Mode.CAUTION
    assert any(a.rule == "engine.error" and a.detail == "stage=overlay;exc=RuntimeError" for a in t.actions)
    t = trace_of(mk(planner=Boom()).step(snap))
    assert t.n_intents == 0 and any(a.detail == "stage=planner;exc=RuntimeError" for a in t.actions)
    t = trace_of(mk(allocator=Boom()).step(snap))
    assert any(a.detail == "stage=allocator;exc=RuntimeError" for a in t.actions)
    t = trace_of(mk(router=Boom()).step(snap))
    assert any(a.detail == "stage=router;exc=RuntimeError" for a in t.actions)
    t = trace_of(mk(strategies=[BadStrategy()]).step(snap))
    assert t.strategies_run == () and any(a.rule == "engine.strategy_error" and "ZeroDivisionError" in a.detail
                                          for a in t.actions)


# ------------------------------------------------------------------------------------------------- calibration, purity
@dataclass
class Provider:
    shift: int = 0

    def asof(self, block: Block) -> Calibration:
        hz = HazardModel(cdf_by_r=((Decimal(2), Decimal(1)),), p_open=Decimal("0.0625"), n0=4,
                         lambda_floor_per_block=Decimal("0.0001"), valid=True)
        return sealed(Calibration(asof=Block(block + self.shift), hazard=hz, kappa_p=Decimal(4), r_default=Decimal("0.35"),
                                  r_cap_formula=True, tier_b_jump_p_day=Decimal("0.03"), tier_b_jump_size=Decimal("-0.69"),
                                  phi=SellLoadParams(), digest=""))


def test_calibration_digest_is_journaled_and_lookahead_raises(fx, market, mk) -> None:
    h = mk(calibration=Provider())
    t = trace_of(h.step(market(1)[0]))
    assert t.calib_digest == Provider().asof(Block(fx.START)).digest and t.calib_digest
    with pytest.raises(LookaheadError):
        mk(calibration=Provider(shift=1)).step(market(1)[0])


def test_decide_is_pure_and_needs_the_tick_snapshot(fx, market, mk) -> None:
    snap = market(1)[0]
    a, b = mk(), mk()
    assert a.step(snap) == b.step(snap)
    h = mk()
    with pytest.raises(ValueError, match="SnapshotObserved"):
        h.engine.decide(h.state, snap, None, (), h.features.update(snap, ()), snap, HealthObs.nominal(), inputs=(),
                        store=h.store)


def test_engine_rejects_a_sleeve_without_a_strategy(fx) -> None:
    with pytest.raises(ValueError, match="no strategy"):
        fx.make_engine(fx.book_cfg(), [])
    assert RiskAction  # imported for type completeness
