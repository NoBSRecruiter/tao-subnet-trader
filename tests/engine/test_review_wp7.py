"""Adversarial-review regressions for WP7 (engine, reducer, runner, recovery, control).

Each test pins one defect found in review; the docstring names the design rule it enforces.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core.events import (
    CapitalChanged,
    ChainEventObserved,
    DecisionTrace,
    DeregSettled,
    FillReported,
    HealthObs,
    OperatorCommand,
    OrderCancelled,
    OrderIntended,
    SnapshotObserved,
    SubmitStarted,
    VenueAck,
)
from taotrader.core.orders import TERMINAL, OrderIntent, OrderKind, Urgency
from taotrader.core.protocols import TickContext
from taotrader.core.signals import RiskDecision
from taotrader.core.units import Block, BookId, Hotkey, LogicalTime, Mode, Phase, RunMode, StrategyId, SubnetKey
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.engine.control import ControlWatcher, write_command
from taotrader.engine.reducer import EngineState, check_state, fold_batch, initial_state, ledger_balance, reduce
from taotrader.engine.runner import Runner
from taotrader.protocol.derive import derive_events
from taotrader.protocol.prune import recovery_ratio

TAO = 10**9
CARRY = StrategyId("carry")


class Crash(BaseException):
    """A simulated process death (BaseException: no `except Exception` in the code under test swallows it)."""


class Harness:
    """Runner stand-in without venues: builds the tick inputs, calls decide, folds inputs + outputs."""

    def __init__(self, fx: Any, engine: Any, store: Any) -> None:
        self.fx, self.engine, self.store = fx, engine, store
        self.state: EngineState = engine.initial_state()
        self.prev: Any = None
        self.features = fx.FakeFeatures()

    def step(self, snap: Any, *, extra: tuple[Any, ...] = (), health: HealthObs | None = None) -> list[Any]:
        b = snap.block
        self.store.add(snap)
        self.store.clock = b
        h = health if health is not None else HealthObs.nominal()
        events = derive_events(self.prev, snap)
        frame = replace(self.features.update(snap, events), warm=True)
        run = [SnapshotObserved(b, snap.block_hash, snap.digest, snap.plan, 0, h)] + [ChainEventObserved(e) for e in events]
        run += list(extra)
        items = [(LogicalTime(b, Phase.INGEST, i), BookId(""), e) for i, e in enumerate(run)]
        if not self.state.funded:
            cfg = self.engine.cfg
            items.append((LogicalTime(b, Phase.INGEST, 0), self.engine.book,
                          CapitalChanged(self.engine.book, b, cfg.capital_rao, cfg.fee_float_rao, "initial")))
        outs = self.engine.decide(self.state, snap, self.prev, events, frame, snap, h, inputs=items, store=self.store)
        self.state = fold_batch(self.state, [e for _, _, e in items + outs])
        self.prev = snap
        return outs

    def fold(self, *evs: Any) -> None:
        self.state = fold_batch(self.state, list(evs))


def events_of(outs: list[Any], cls: type) -> list[Any]:
    return [e for _, _, e in outs if isinstance(e, cls)]


def trace_of(outs: list[Any]) -> DecisionTrace:
    (t,) = events_of(outs, DecisionTrace)
    return t


def hold(h: Harness, fx: Any, block: int, key: Any = None, alpha: int = 1_000 * TAO, hotkey: Hotkey | None = None) -> None:
    """Fold a filled buy of `alpha` (shares at index 1) on (key, hotkey) into the harness state."""
    key = key if key is not None else fx.K7
    hk = hotkey if hotkey is not None else fx.HK_A
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, block, key, hk, tao_in=10 * TAO)
    h.fold(OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim0", None, Block(block + 12)),
           VenueAck(i.book, i.order_id, 0, Block(block + 3), Block(block + 5), "", ""),
           FillReported(fx.mk_fill(i, block, tao=10 * TAO, alpha=alpha)))


@pytest.fixture()
def mk(fx):
    def build(strategies: Any = None, **kw: Any) -> Harness:
        strategies = strategies if strategies is not None else [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})])]
        cfg = kw.pop("cfg", None) or fx.book_cfg(dereg_model=kw.pop("dereg_model", "formula"))
        return Harness(fx, fx.make_engine(cfg, strategies, **kw), fx.FakeStore())
    return build


# ------------------------------------------------------------------------------------------------- 1. FROZEN exception
def test_safe_mode_frozen_submits_no_emergency_exit(fx, market, mk) -> None:
    """Section 9.8 #15 / 3.11: during SafeMode no staking call is whitelisted, so risk exits are attempted only when the
    chain accepts staking calls. The allow_emergency_exits_when_frozen exception belongs to the key-alarm FROZEN row
    only; an EMERGENCY sell submitted in SafeMode fails on chain and burns its tx fee every re-quote."""
    safe_from = fx.START + 60
    snaps = market(2, mutate=lambda b, subs: {"safe_mode_until": Block(b + 100)} if b >= safe_from else None)
    overlay = fx.FakeOverlay(forced={fx.K7: (0, Urgency.EMERGENCY, "prune_A")})
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])], overlay=overlay)
    h.step(snaps[0])
    hold(h, fx, fx.START + 5)
    outs = h.step(snaps[1])
    t = trace_of(outs)
    assert t.mode is Mode.FROZEN
    assert events_of(outs, OrderIntended) == [], "an EMERGENCY sell was emitted while SafeMode freezes staking calls"
    assert any(a.rule == "engine.drop_intent" and "mode:FROZEN" in a.detail for a in t.actions)


def test_no_healthy_endpoint_frozen_submits_no_emergency_exit(fx, market, mk) -> None:
    """Section 3.11: 'no healthy head endpoint -> FROZEN' has no emergency exception (nothing can reach the chain)."""
    snaps = market(2)
    overlay = fx.FakeOverlay(forced={fx.K7: (0, Urgency.EMERGENCY, "prune_A")})
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])], overlay=overlay)
    h.step(snaps[0])
    hold(h, fx, fx.START + 5)
    outs = h.step(snaps[1], health=HealthObs(3, 12, 0, 0, 0))
    assert trace_of(outs).mode is Mode.FROZEN
    assert events_of(outs, OrderIntended) == []


def test_key_alarm_frozen_still_allows_the_emergency_exit(fx, market, mk) -> None:
    """Contrast: a FROZEN raised by the overlay (live key alarm) keeps the configured EMERGENCY exception."""
    snaps = market(2)
    overlay = fx.FakeOverlay(forced={fx.K7: (0, Urgency.EMERGENCY, "prune_A")}, mode=Mode.FROZEN)
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])], overlay=overlay)
    h.step(snaps[0])
    hold(h, fx, fx.START + 5)
    outs = h.step(snaps[1])
    (i,) = [e.intent for e in events_of(outs, OrderIntended)]
    assert i.kind is OrderKind.REMOVE_STAKE_LIMIT and i.urgency is Urgency.EMERGENCY


# ------------------------------------------------------------------------------------------------- 2. one settlement per generation
def test_a_generation_held_on_two_hotkeys_settles_in_one_journal_record(fx, market, mk) -> None:
    """DeregSettled's idempotency key is dereg:{book}:{netuid}:{reg_at} (one per held generation). Two positions on the
    same generation (an invariant-5 anomaly: a partial move, a live ReconAdjusted) made the Engine emit two records
    with that key, so the journal rejected the whole tick batch and the Runner crash-looped on every restart."""
    gone = fx.START + 120
    snaps = market(3, mutate=lambda b, subs: subs.pop() and None if b >= gone else None)   # K9 removed at `gone`
    h = mk(strategies=[fx.TargetStrategy(CARRY, [])])
    h.step(snaps[0])
    hold(h, fx, fx.START + 5, key=fx.K9, alpha=500 * TAO)
    hold(h, fx, fx.START + 6, key=fx.K9, alpha=200 * TAO, hotkey=fx.HK_B)
    h.step(snaps[1])
    assert len([p for p in h.state.portfolio.positions if p.key == fx.K9]) == 2
    outs = h.step(snaps[2])
    settles = events_of(outs, DeregSettled)
    assert len(settles) == 1, [s.hotkey for s in settles]
    SqliteJournal(":memory:").append_batch(outs)                  # one idempotency key: the batch commits
    (d,) = settles
    s_prev = snaps[1].get(fx.K9)
    r = recovery_ratio(s_prev, snaps[1].glob, Decimal("0.35"))
    assert d.alpha_value == 700 * TAO
    assert d.payout_tao == int(Decimal(700 * TAO) * s_prev.pool.spot() * r)
    st = h.state
    assert all(p.key != fx.K9 for p in st.portfolio.positions) and st.dissolving == ()
    for hk in (fx.HK_A, fx.HK_B):
        assert ledger_balance(st, f"pos:9:1000:{hk}", "A:9:1000") == 0
    assert all(sh.key != fx.K9 for sh in st.portfolio.sleeves)
    assert check_state(st) == [] and st.orphans == 0


def test_an_observed_settlement_closes_every_hotkey_of_the_generation(fx) -> None:
    """Live reconciliation journals one observed DeregSettled per generation; the payout is the coldkey's whole
    free-TAO credit, so it must close every position on that generation, not leave a zombie on the other hotkey."""
    eng = fx.make_engine(fx.book_cfg(), [fx.TargetStrategy(CARRY, [])], mode=RunMode.LIVE)
    st = initial_state(eng.spec)
    st = reduce(st, CapitalChanged(BookId("b1"), Block(fx.START), 100 * TAO, TAO, "initial"))
    for blk, hk, alpha in ((fx.START + 5, fx.HK_A, 500 * TAO), (fx.START + 6, fx.HK_B, 200 * TAO)):
        i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, blk, fx.K9, hk, tao_in=10 * TAO)
        for ev in (OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim0", None, Block(blk + 12)),
                   VenueAck(i.book, i.order_id, 0, Block(blk + 3), Block(blk + 5), "", ""),
                   FillReported(fx.mk_fill(i, blk, tao=10 * TAO, alpha=alpha))):
            st = reduce(st, ev)
    cash0 = st.portfolio.cash
    st = fold_batch(st, [DeregSettled(BookId("b1"), fx.K9, fx.HK_A, Block(fx.START + 300), 0, 7 * TAO, "observed")])
    assert st.portfolio.cash == cash0 + 7 * TAO
    assert all(p.key != fx.K9 for p in st.portfolio.positions)
    assert check_state(st) == [] and st.orphans == 0


# ------------------------------------------------------------------------------------------------- 6. reducer totality
def _funded_with_position(fx: Any) -> EngineState:
    eng = fx.make_engine(fx.book_cfg(), [fx.TargetStrategy(CARRY, [])])
    st = reduce(initial_state(eng.spec), CapitalChanged(BookId("b1"), Block(fx.START), 100 * TAO, TAO, "initial"))
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, fx.START + 5, fx.K7, tao_in=10 * TAO)
    for ev in (OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim0", None, Block(fx.START + 17)),
               VenueAck(i.book, i.order_id, 0, Block(fx.START + 8), Block(fx.START + 10), "", "")):
        st = reduce(st, ev)
    return fold_batch(st, [FillReported(fx.mk_fill(i, fx.START + 10, tao=10 * TAO, alpha=1_000 * TAO))])


def _bracket(st: EngineState, i: OrderIntent) -> EngineState:
    for ev in (OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim1", None, Block(i.created_block + 12)),
               VenueAck(i.book, i.order_id, 0, Block(i.created_block + 3), Block(i.created_block + 5), "", "")):
        st = reduce(st, ev)
    return st


def test_a_move_fill_without_destination_is_quarantined_not_raised(fx) -> None:
    """reduce() must be total on journaled facts (section 4.5): a committed MOVE_STAKE fill without dest_hotkey raised
    AssertionError from core.portfolio.fill_txn, so every recovery replay re-raised and the run could never restart."""
    st = _funded_with_position(fx)
    m = fx.mk_intent(OrderKind.MOVE_STAKE, fx.START + 20, fx.K7, full=True, dest_hotkey=fx.HK_B)
    st = _bracket(st, m)
    bad = replace(fx.mk_fill(m, fx.START + 25, alpha=1_000 * TAO), dest_hotkey=None, dest_key=None)
    before = st.portfolio
    st2 = fold_batch(st, [FillReported(bad)])                     # must not raise
    assert st2.orphans == st.orphans + 1 and st2.portfolio == before and check_state(st2) == []


@pytest.mark.parametrize("field_name, value", [("shares", Decimal(-1_000 * TAO)), ("tao", -5), ("tx_fee", -1)])
def test_a_signed_or_negative_sell_fill_is_quarantined(fx, field_name: str, value: Any) -> None:
    """Fill amounts are unsigned magnitudes. A signed share delta (negative shares on a sell) grew the position and
    the ledger together, so no invariant fired; a negative fee minted fee float. Such facts must be quarantined."""
    st = _funded_with_position(fx)
    s = fx.mk_intent(OrderKind.REMOVE_STAKE_LIMIT, fx.START + 20, fx.K7, full=True)
    st = _bracket(st, s)
    f = replace(fx.mk_fill(s, fx.START + 25, tao=12 * TAO, alpha=1_000 * TAO), **{field_name: value})
    before = (st.portfolio, st.ledger)
    st2 = fold_batch(st, [FillReported(f)])
    assert st2.orphans == st.orphans + 1 and (st2.portfolio, st2.ledger) == before


def test_negative_fees_and_payouts_are_quarantined(fx) -> None:
    from taotrader.core.events import CarrierFeeSettled, OrderFailed
    from taotrader.core.orders import FailReason
    st = _funded_with_position(fx)
    s = fx.mk_intent(OrderKind.REMOVE_STAKE_LIMIT, fx.START + 20, fx.K7, full=True)
    st = _bracket(st, s)
    ff = st.portfolio.fee_float
    st2 = fold_batch(st, [OrderFailed(BookId("b1"), s.order_id, 0, Block(fx.START + 25), FailReason.OTHER, -3)])
    assert st2.orphans == st.orphans + 1 and st2.portfolio.fee_float == ff
    st3 = fold_batch(st, [OrderFailed(BookId("b1"), s.order_id, 0, Block(fx.START + 25), FailReason.SHIELD_MISSED, 0,
                                      expired=True),
                          CarrierFeeSettled(BookId("b1"), s.order_id, 0, Block(fx.START + 40), -7, "carrier_only")])
    assert st3.orphans == st.orphans + 1 and st3.portfolio.fee_float == ff
    st4 = fold_batch(st, [DeregSettled(BookId("b1"), fx.K7, fx.HK_A, Block(fx.START + 30), 0, -9, "observed")])
    assert st4.orphans == st.orphans + 1 and st4.portfolio == st.portfolio


# ------------------------------------------------------------------------------------------------- 7. sleeve cost ratio
@pytest.mark.parametrize("drift_ppm", [20_000, -20_000, 0])
def test_cost_ratio_measures_the_cost_model_not_the_price_drift_to_the_fill(fx, drift_ppm: int) -> None:
    """Section 3.11 sleeve kill: REDUCED at a 20-trade realised/modelled cost ratio > 1.5, SUSPENDED > 2.5. The
    intent's expected_out is modelled at the DECISION spot, but the modelled cost was evaluated at the fill's
    spot_before; a stride fill lands ~60 blocks later, so a 2% move flipped the modelled cost negative, the sum was
    clamped to 1 rao-ppm and the ratio exploded (momentum buying into strength was always SUSPENDED)."""
    from taotrader.core.signals import RiskAction
    from taotrader.engine.reducer import ENGINE_ORDER_SPOT, sleeve_stats
    st = reduce(initial_state(fx.make_engine(fx.book_cfg(), [fx.TargetStrategy(CARRY, [])]).spec),
                CapitalChanged(BookId("b1"), Block(fx.START), 100 * TAO, TAO, "initial"))
    spot_d = 10_000_000                                            # 0.01 TAO per alpha at the decision
    cost_ppm = 3_000                                               # fee + impact, as modelled and as realised
    b = fx.START + 60
    tao_in = 10 * TAO
    expected = tao_in * 10**9 // spot_d * (1_000_000 - cost_ppm) // 1_000_000
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, b, fx.K7, tao_in=tao_in, expected_out=expected)
    trace = DecisionTrace(book=BookId("b1"), block=Block(b), strategies_run=(), signals=(),
                          actions=(RiskAction(ENGINE_ORDER_SPOT, fx.K7, "MONITOR", f"spot_rao={spot_d};order_id={i.order_id}"),),
                          mode=Mode.NORMAL, memories=(), features_digest="", n_intents=1)
    st = fold_batch(st, [SnapshotObserved(Block(b), "0x" + "1" * 64, "d", 2, 0, HealthObs.nominal()), trace,
                         OrderIntended(i)])
    st = _bracket(st, i)
    spot_f = spot_d * (1_000_000 + drift_ppm) // 1_000_000          # the stride fill sees a moved price
    alpha = tao_in * 10**9 // spot_f * (1_000_000 - cost_ppm) // 1_000_000
    fill = fx.mk_fill(i, b + 60, tao=tao_in, alpha=alpha, spot_before=spot_f, shortfall_ppm=cost_ppm, exact=False)
    st = fold_batch(st, [FillReported(fill)])
    (stats,) = [s for s in sleeve_stats(st) if s.strategy == CARRY]
    assert abs(stats.cost_ratio_20_ppm - 1_000_000) <= 1_000, stats.cost_ratio_20_ppm


# ------------------------------------------------------------------------------------------------- 8. duplicate venue facts
def test_a_venue_repeating_a_fact_within_one_call_is_skipped_not_a_crash_loop(fx, market) -> None:
    """Venue facts already in the journal are skipped with an alert; the same fact twice in ONE resolve() answer was
    not, so the batch hit the UNIQUE idempotency key, poisoned the Runner and did the same on every restart."""
    from taotrader.core.orders import OrderState, Resolution
    holder: dict[str, Any] = {}

    def venue(name: str) -> Any:
        holder[name] = fx.ScriptedVenue(BookId(name), raise_on_submit=True)
        return holder[name]

    snaps = market(4)
    j = SqliteJournal(":memory:")
    alerts: list[tuple[str, str]] = []
    eng = fx.make_engine(fx.book_cfg("b1"), [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})])])
    rt = fx.make_runtime(eng, venue("b1"))
    r = Runner(run_id=fx.RUN, mode=RunMode.BACKTEST, source=fx.FakeSource(snaps[:2]), journal=j,
               features=fx.FakeFeatures(), books=[rt], on_alert=lambda k, m: alerts.append((k, m)))
    fx.arun(r.run())
    (o,) = [o for o in rt.state.orders if o.state is OrderState.UNKNOWN]
    fill = fx.mk_fill(o.intent, fx.START + 5, tao=o.intent.tao_in, alpha=o.intent.tao_in * 99)
    holder["b1"].resolution = (Resolution.LANDED, (FillReported(fill), FillReported(fill)))
    r.source = fx.FakeSource(snaps, cadence_blocks=60)
    fx.arun(r.run())                                              # must not raise DuplicateIdempotencyKey
    fills = [e for e in (decode_record(x) for x in j.read(1)) if isinstance(e, FillReported)]
    assert [f.fill.fill_id for f in fills].count(fill.fill_id) == 1
    assert any(k == "venue" and f"fill:{fill.fill_id}" in m for k, m in alerts)
    assert rt.state.orphans == 0 and check_state(rt.state) == []


# ------------------------------------------------------------------------------------------------- 3. control-file order
def _effective(cmds: list[OperatorCommand], halted: bool, exits_only: bool = False) -> tuple[bool, bool]:
    for c in cmds:
        if c.command == "halt":
            halted = True
        elif c.command == "resume":
            halted, exits_only = False, False
        elif c.command == "exits_only":
            exits_only = True
    return halted, exits_only


def test_a_halt_polled_together_with_a_resume_is_never_undone(tmp_path) -> None:
    """Section 9.8 #14: `taotrader halt` stops all new submissions within one block. Files were applied in file-name
    (random nonce) order, so a resume written just BEFORE a halt could be journaled after it and silently un-halt."""
    b = Block(9_000_000)
    for first, second in (("zz", "aa"), ("aa", "zz")):
        ctl = tmp_path / f"ctl-{first}"
        write_command(ctl, "resume", "operator: resume", nonce=first)
        write_command(ctl, "halt", "operator: halt", nonce=second)
        cmds = ControlWatcher(ctl).poll(b, halted=True, resume_count=0)
        assert sorted(c.command for c in cmds) == ["halt", "resume"]
        assert _effective(cmds, halted=True) == (True, False), [c.command for c in cmds]


def test_exits_only_polled_together_with_a_resume_wins(tmp_path) -> None:
    """The restrictive command wins within one poll whatever the nonce order (fail-closed operator semantics)."""
    b = Block(9_000_000)
    for first, second in (("zz", "aa"), ("aa", "zz")):
        ctl = tmp_path / f"ctl-{first}"
        write_command(ctl, "resume", "r", nonce=first)
        write_command(ctl, "exits_only", "x", nonce=second)
        cmds = ControlWatcher(ctl).poll(b, halted=False, resume_count=0)
        assert _effective(cmds, halted=False) == (False, True), [c.command for c in cmds]


# ------------------------------------------------------------------------------------------------- 4. planner view
@dataclass
class BusyAwarePlanner:
    """The WP8 StandardPlanner rule: a netuid is busy when it is in `inflight` OR has a non-terminal order in
    ctx.book_view.orders. Delegates the rest to the conftest FakePlanner."""
    inner: Any
    seen_open: list[int] = field(default_factory=list)

    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey], run_id: str,
                 book: BookId) -> tuple[OrderIntent, ...]:
        busy = set(inflight) | {o.intent.key for o in ctx.book_view.orders if o.state not in TERMINAL}
        self.seen_open.append(len(busy))
        return tuple(self.inner(decision, ctx, frozenset(busy), run_id, book))


def test_orders_cancelled_this_tick_are_closed_in_the_planner_book_view(fx, market, mk) -> None:
    """Section 4.4 DECIDE step 9 + the planner contract: the Engine cancels INTENDED orders the new decision invalidates
    BEFORE planning, so their netuid is free again in the same tick. The planner reads book history from
    ctx.book_view, which still showed the cancelled orders as INTENDED, so a re-decision waited a whole tick (60 blocks
    in a stride backtest; an EMERGENCY exit whose reserve failed was delayed)."""
    planner = BusyAwarePlanner(fx.FakePlanner())
    h = mk(planner=planner)
    snaps = market(2)
    (first,) = [e.intent for e in events_of(h.step(snaps[0]), OrderIntended)]   # never submitted (no venue here)
    outs = h.step(snaps[1])
    (c,) = events_of(outs, OrderCancelled)
    assert (c.order_id, c.reason) == (first.order_id, "ttl_expired")
    again = [e.intent for e in events_of(outs, OrderIntended)]
    assert [i.key for i in again] == [fx.K7], "the cancelled order still blocked its netuid in the planner's view"


# ------------------------------------------------------------------------------------------------- 5. kill file at restart
def test_a_kill_file_present_at_restart_stops_the_recovery_outbox(fx, market, tmp_path) -> None:
    """Section 9.8 #14: a kill file stops all new submissions. Recovery re-drove the INTENDED orders of the last tick
    (crash after the commit, before the outbox) before the control directory was ever read, so a KILL dropped after
    the crash did not stop them."""
    snaps = market(3)
    path = tmp_path / "j.sqlite"

    def build(journal: Any, **kw: Any) -> Runner:
        eng = fx.make_engine(fx.book_cfg("b1"), [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000})])])
        return Runner(run_id=fx.RUN, mode=RunMode.BACKTEST, source=fx.FakeSource(snaps), journal=journal,
                      features=fx.FakeFeatures(), books=[fx.make_runtime(eng)], **kw)

    def fault(point: str) -> None:
        if point == "post_commit":
            raise Crash(point)

    j = SqliteJournal(path, durable=False)
    with pytest.raises(Crash):
        fx.arun(build(j, fault=fault).run())
    j.close()
    ctl = tmp_path / "control"
    ctl.mkdir()
    (ctl / "KILL").write_text("stop", encoding="utf-8")
    j2 = SqliteJournal(path, durable=False)
    r2 = build(j2, control=ControlWatcher(ctl))
    evs = [decode_record(rec) for rec in j2.read(1)]
    assert any(isinstance(e, OrderIntended) for e in evs) and not any(isinstance(e, SubmitStarted) for e in evs)
    fx.arun(r2.recover())
    evs = [decode_record(rec) for rec in j2.read(1)]
    assert r2.book("b1").state.halted
    assert not any(isinstance(e, SubmitStarted) for e in evs), "the outbox submitted while the KILL file existed"
    fx.arun(r2.run())                                             # the next ticks cancel the stale intent and stay halted
    evs = [decode_record(rec) for rec in j2.read(1)]
    assert not any(isinstance(e, SubmitStarted) for e in evs) and r2.book("b1").state.halted
    j2.close()
