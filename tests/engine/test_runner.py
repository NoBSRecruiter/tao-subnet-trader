"""engine.runner: the side-effect loop with the real SimVenue and SqliteJournal and fake decision components
(DESIGN.md 4.4, 4.5, 4.7, 9.8 #14)."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core.errors import DataContractError
from taotrader.core.events import (
    CapitalChanged,
    ConfigApplied,
    DecisionTrace,
    FillReported,
    HealthObs,
    OperatorCommand,
    OrderFailed,
    OrderIntended,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
    YieldAccrued,
)
from taotrader.core.orders import FailReason, OrderKind, OrderState, Resolution
from taotrader.core.units import Block, BookId, Phase, RunMode, StrategyId
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.engine.control import ControlWatcher, write_command
from taotrader.engine.reducer import check_state, money_digest
from taotrader.engine.runner import Runner, RunnerPoisoned

TAO = 10**9
CARRY = StrategyId("carry")


def make_runner(fx: Any, snaps: list[Any], *, journal: Any = None, strategies: Any = None, venue: Any = None,
                stride: int = 60, books: tuple[str, ...] = ("b1",), features: Any = None, **kw: Any) -> Runner:
    rts = []
    for name in books:
        strat = strategies(name) if callable(strategies) else (strategies or [
            fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000, fx.K9: 300_000}), (fx.START + 900, {fx.K9: 200_000})])])
        eng = fx.make_engine(fx.book_cfg(name), strat)
        rts.append(fx.make_runtime(eng, venue(name) if callable(venue) else venue))
    return Runner(run_id=fx.RUN, mode=RunMode.BACKTEST, source=fx.FakeSource(snaps, cadence_blocks=stride),
                  journal=journal if journal is not None else SqliteJournal(":memory:"),
                  features=features if features is not None else fx.FakeFeatures(), books=rts, **kw)


def records(j: SqliteJournal) -> list[tuple[Any, Any]]:
    return [(r, decode_record(r)) for r in j.read(1)]


def test_a_backtest_run_trades_accrues_and_keeps_the_ledger_balanced(fx, market) -> None:
    r = make_runner(fx, market(30))
    s = fx.arun(r.run())
    assert s.ticks == 30 and s.last_block == fx.START + 29 * 60
    kinds = Counter(rec.kind for rec in r.journal.read(1))
    assert kinds["snapshot_observed"] == 30 and kinds["decision_trace"] == 30 and kinds["config_applied"] == 1
    assert kinds["capital_changed"] == 1 and kinds["fill_reported"] >= 3 and kinds["yield_accrued"] >= 2
    st = r.book("b1").state
    assert check_state(st) == [] and st.breaches == () and st.orphans == 0
    assert r.journal.verify_chain(deep=True) == sum(kinds.values())
    venue = r.book("b1").venue
    assert venue.cash == st.portfolio.cash                       # the venue's journal fold agrees with the reducer
    for p in st.portfolio.positions:
        assert venue.shares(p.key, p.hotkey) == p.shares


def test_phase_ordering_yield_accrues_before_same_block_fills(fx, market) -> None:
    # buy at START (fills at START+60); exit decided at START+300 fills at START+360, the drain block
    strat = [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000}), (fx.START + 300, {})])]
    r = make_runner(fx, market(8), strategies=strat)
    fx.arun(r.run())
    recs = [(rec, ev) for rec, ev in records(r.journal) if int(rec.time.block) == fx.START + 360]
    kinds = [rec.kind for rec, _ in recs]
    y = kinds.index("yield_accrued")
    f = kinds.index("fill_reported")
    assert y < f and recs[y][0].time.phase is Phase.ACCOUNT and recs[f][0].time.phase is Phase.VENUE
    yev, fev = recs[y][1], recs[f][1]
    assert isinstance(yev, YieldAccrued) and isinstance(fev, FillReported)
    assert fev.fill.kind is OrderKind.REMOVE_STAKE_LIMIT and fev.fill.shares > 0
    # the yield was computed on the pre-sale share count (the whole position earned the drain)
    expected = (fev.fill.shares * Decimal("0.0003")).to_integral_value()
    assert abs(yev.delta_alpha - int(expected)) <= 2
    st = r.book("b1").state
    assert st.portfolio.positions == () and check_state(st) == []


def test_two_runs_give_identical_hash_chains(fx, market) -> None:
    heads = []
    for _ in range(2):
        r = make_runner(fx, market(25), books=("b1", "b2"))
        fx.arun(r.run())
        heads.append(r.journal.head())
    assert heads[0] == heads[1] and heads[0][0] > 100


def test_cadence_alignment_is_identical_at_stride_1_and_60(fx, market) -> None:
    runs = {}
    for stride in (1, 60):
        n = 1_201 if stride == 1 else 21
        r = make_runner(fx, market(n, stride=stride), stride=stride)
        fx.arun(r.run())
        runs[stride] = [int(ev.block) for _, ev in records(r.journal)
                        if isinstance(ev, DecisionTrace) and ev.strategies_run]
    assert runs[1] == runs[60] == [fx.START + k * 300 for k in range(5)]


def test_own_fill_blocks_reach_the_feature_engine(fx, market) -> None:
    feats = fx.FakeFeatures()
    r = make_runner(fx, market(6), features=feats)
    fx.arun(r.run())
    fill_blocks = sorted({int(ev.fill.block) for _, ev in records(r.journal) if isinstance(ev, FillReported)})
    assert fill_blocks
    seen = dict(feats.calls)
    first = fill_blocks[0]
    assert all(first not in seen[b] for b in seen if b <= first)  # a fill is known only after it is journaled
    assert all(first in seen[b] for b in seen if b > first)


def test_data_contract_refuses_a_strategy_finer_than_the_source(fx, market) -> None:
    with pytest.raises(DataContractError):
        make_runner(fx, market(2), strategies=[fx.TargetStrategy(CARRY, [], min_cadence_blocks=1)])


def test_an_orphan_from_the_venue_quarantines_without_raising(fx, market) -> None:
    ghost = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, fx.START, fx.K9, tao_in=TAO)
    venues: dict[str, Any] = {}

    def venue(name: str) -> Any:
        v = fx.ScriptedVenue(BookId(name), queued=[FillReported(fx.mk_fill(ghost, fx.START + 5, tao=TAO, alpha=TAO))])
        venues[name] = v
        return v

    alerts: list[tuple[str, str]] = []
    r = make_runner(fx, market(3), venue=venue, on_alert=lambda k, m: alerts.append((k, m)))
    fx.arun(r.run())
    st = r.book("b1").state
    assert st.orphans == 1 and alerts and alerts[0][0] == "orphan"
    assert st.portfolio.cash == 100 * TAO - sum(int(ev.fill.tao) for _, ev in records(r.journal)
                                                  if isinstance(ev, FillReported) and ev.fill.order_id != ghost.order_id)
    later = [ev for rec, ev in records(r.journal) if isinstance(ev, DecisionTrace) and int(ev.block) > fx.START]
    assert all(ev.n_intents == 0 for ev in later)                 # entries halted while quarantined


def test_submit_raising_journals_submit_unknown_and_resolve_lands_it(fx, market) -> None:
    holder: dict[str, Any] = {}

    def venue(name: str) -> Any:
        holder[name] = fx.ScriptedVenue(BookId(name), raise_on_submit=True)
        return holder[name]

    snaps = market(4)
    j = SqliteJournal(":memory:")
    r = make_runner(fx, snaps[:2], venue=venue, journal=j)
    fx.arun(r.run())
    unknown = [ev for _, ev in records(j) if isinstance(ev, SubmitUnknown)]
    assert unknown and "ConnectionError" in unknown[0].detail
    oid = unknown[0].order_id
    st = r.book("b1").state
    assert next(o for o in st.orders if o.intent.order_id == oid).state is OrderState.UNKNOWN
    assert not any(isinstance(ev, OrderFailed) for _, ev in records(j))       # never failed blindly, never re-sent
    assert holder["b1"].submits.count(oid) == 1
    intent = next(o.intent for o in st.orders if o.intent.order_id == oid)
    fill = fx.mk_fill(intent, fx.START + 5, tao=intent.tao_in, alpha=intent.tao_in * 99)
    holder["b1"].resolution = (Resolution.LANDED, (FillReported(fill),))
    r.source = fx.FakeSource(snaps, cadence_blocks=60)
    fx.arun(r.run())
    st = r.book("b1").state
    assert next(o for o in st.orders if o.intent.order_id == oid).state is OrderState.FILLED
    assert st.orphans == 0 and check_state(st) == []


def test_operator_control_files_halt_and_resume(fx, market, tmp_path) -> None:
    ctl = tmp_path / "control"
    write_command(ctl, "halt", "maintenance", nonce="h1")
    r = make_runner(fx, market(3), control=ControlWatcher(ctl))
    fx.arun(r.run(max_ticks=1))
    ops = [ev for _, ev in records(r.journal) if isinstance(ev, OperatorCommand)]
    assert [(o.command, o.nonce) for o in ops] == [("halt", "h1")]
    assert r.book("b1").state.halted and not any(isinstance(ev, OrderIntended) for _, ev in records(r.journal))
    assert (ctl / "done" / "cmd-h1.json").exists()
    write_command(ctl, "resume", "done", nonce="r1")
    fx.arun(r.run(max_ticks=1))
    assert not r.book("b1").state.halted
    assert any(isinstance(ev, OrderIntended) for _, ev in records(r.journal))


def test_the_kill_file_keeps_every_book_halted(fx, market, tmp_path) -> None:
    ctl = tmp_path / "control"
    ctl.mkdir()
    (ctl / "KILL").write_text("stop", encoding="utf-8")
    r = make_runner(fx, market(4), control=ControlWatcher(ctl), books=("b1", "b2"))
    fx.arun(r.run(max_ticks=1))
    assert all(rt.state.halted for rt in r.books)
    write_command(ctl, "resume", "try", nonce="r1")
    fx.arun(r.run(max_ticks=2))                                  # resume, overridden by the kill file in the same batch
    halts = [ev for _, ev in records(r.journal) if isinstance(ev, OperatorCommand) and ev.command == "halt"]
    assert len(halts) == 2 and halts[0].nonce != halts[1].nonce
    assert all(rt.state.halted for rt in r.books)
    assert not any(isinstance(ev, SubmitStarted) for _, ev in records(r.journal))


def test_queued_capital_is_journaled_once(fx, market) -> None:
    r = make_runner(fx, market(3))
    fx.arun(r.run(max_ticks=1))
    r.queue_capital("b1", 5 * TAO, 0, "topup-1")
    with pytest.raises(ValueError):
        r.queue_capital("b1", 5 * TAO, 0, "topup-1")
    fx.arun(r.run())
    caps = [ev for _, ev in records(r.journal) if isinstance(ev, CapitalChanged)]
    assert [(c.memo, c.cash_delta) for c in caps] == [("initial", 100 * TAO), ("topup-1", 5 * TAO)]
    with pytest.raises(ValueError):
        r.queue_capital("b1", TAO, 0, "topup-1")


def test_commit_in_thread_and_health_are_journaled(fx, market) -> None:
    health = lambda b: HealthObs(3, 12 + (b % 7), 2, 0, 0)           # noqa: E731
    r = make_runner(fx, market(5), commit_in_thread=True)
    r.source = fx.FakeSource(market(5), cadence_blocks=60, health=health)
    fx.arun(r.run())
    obs = [ev for _, ev in records(r.journal) if isinstance(ev, SnapshotObserved)]
    assert [o.health.secs_since_block for o in obs] == [12 + (int(o.block) % 7) for o in obs]
    assert r._executor is not None and r._executor._max_workers == 1          # one dedicated commit thread
    r.close()
    assert r._executor is None


def test_a_failed_commit_poisons_the_runner(fx, market) -> None:
    class Disk(Exception):
        pass

    def fault(point: str) -> None:
        if point == "pre_commit":
            raise Disk("disk full")

    r = make_runner(fx, market(3), fault=fault)
    with pytest.raises(Disk):
        fx.arun(r.run())
    with pytest.raises(RunnerPoisoned):
        fx.arun(r.run())


def test_mode_mismatch_and_duplicate_books_are_refused(fx, market) -> None:
    eng = fx.make_engine(fx.book_cfg(), [fx.TargetStrategy(CARRY, [])])
    rt = fx.make_runtime(eng)
    with pytest.raises(ValueError, match="mode"):
        Runner(run_id=fx.RUN, mode=RunMode.PAPER, source=fx.FakeSource(market(1)), journal=SqliteJournal(":memory:"),
               features=fx.FakeFeatures(), books=[rt])
    with pytest.raises(ValueError, match="duplicate"):
        Runner(run_id=fx.RUN, mode=RunMode.BACKTEST, source=fx.FakeSource(market(1)), journal=SqliteJournal(":memory:"),
               features=fx.FakeFeatures(), books=[rt, rt])


def test_money_digest_differs_when_the_journal_differs(fx, market) -> None:
    a = make_runner(fx, market(10))
    b = make_runner(fx, market(10), strategies=[fx.TargetStrategy(CARRY, [(0, {fx.K7: 100_000})])])
    fx.arun(a.run())
    fx.arun(b.run())
    assert money_digest(a.book("b1").state) != money_digest(b.book("b1").state)
    assert ConfigApplied and VenueAck and Block and FailReason and Resolution   # imports used by sibling tests


def test_live_dissolution_is_settled_once_by_reconciliation(fx, market) -> None:
    from taotrader.core.events import DeregSettled
    from taotrader.core.units import Rao

    gone = fx.START + 240
    snaps = market(8, mutate=lambda b, subs: subs.pop() and None if b >= gone else None)   # K9 removed at `gone`
    strat = [fx.TargetStrategy(CARRY, [(0, {fx.K9: 500_000})])]
    eng = fx.make_engine(fx.book_cfg(), strat, mode=RunMode.LIVE)
    rt = fx.make_runtime(eng)
    calls: list[int] = []

    async def reconcile(book_rt: Any, snap: Any) -> list[Any]:
        calls.append(int(snap.block))
        out = []
        for key in book_rt.state.dissolving:
            pos = book_rt.state.portfolio.position(key)
            out.append(DeregSettled(book_rt.book, key, pos.hotkey, snap.block, 0, Rao(7 * TAO), "observed"))
        if not out and int(snap.block) > gone:                   # replay the settlement: must be skipped, never paid twice
            out.append(DeregSettled(book_rt.book, fx.K9, fx.HK_A, Block(gone), 0, Rao(7 * TAO), "observed"))
        return out

    alerts: list[tuple[str, str]] = []
    r = Runner(run_id=fx.RUN, mode=RunMode.LIVE, source=fx.FakeSource(snaps), journal=SqliteJournal(":memory:"),
               features=fx.FakeFeatures(), books=[rt], reconcile=reconcile, on_alert=lambda k, m: alerts.append((k, m)))
    fx.arun(r.run())
    settles = [ev for _, ev in records(r.journal) if isinstance(ev, DeregSettled)]
    assert len(settles) == 1 and settles[0].model == "observed" and int(settles[0].block) == gone
    st = r.book("b1").state
    assert st.portfolio.position(fx.K9) is None and st.dissolving == () and st.orphans == 0 and check_state(st) == []
    assert any("repeated journaled event dereg:b1:9:1000" in m for _, m in alerts)
    assert calls and calls[0] == fx.START
