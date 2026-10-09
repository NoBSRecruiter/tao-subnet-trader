"""WP10 backtest.bias: lookahead canary, oracle canary, shuffled and delayed placebos, future truncation (section 8.10).

All runs use the production engine on real mini-lake snapshots (short window)."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from taotrader.backtest import bias
from taotrader.backtest.books import BacktestPlan
from taotrader.backtest.runner import run_pass
from taotrader.core.config import BookCfg, SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.signals import SignalKind
from taotrader.core.units import PPM, Block, BookId, Ppm, Stage, StrategyId
from taotrader.data.journal import SqliteJournal
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore
from taotrader.strategies.base import build_strategy


def _book(plan: BacktestPlan, book: str, strategy: str) -> BookCfg:
    base = plan.book("base-cash")
    return replace(base, book=BookId(book), sleeves=(SleeveCfg(StrategyId(strategy), Stage.RESEARCH, Ppm(PPM)),))


def _plan_with(plan: BacktestPlan, *books: BookCfg) -> BacktestPlan:
    from types import MappingProxyType

    from taotrader.backtest.books import BookVariant
    vs = dict(plan.variants)
    for b in books:
        vs[b.book] = BookVariant(b.book, b.book, "temporary", b.dereg_model, tuple(s.strategy for s in b.sleeves))
    return replace(plan, run=replace(plan.run, books=plan.run.books + tuple(books)), variants=MappingProxyType(vs))


def test_peeking_canary_raises_on_the_store(open_lake: Lake) -> None:
    store = LakeSnapshotStore(open_lake, clock=8_766_000)
    canary = bias.PeekingCanary()

    class Ctx:
        block = Block(8_766_000)

    ctx: Any = Ctx()
    ctx.store = store
    with pytest.raises(LookaheadError):
        canary.on_tick(ctx, 0)


def test_peeking_canary_inside_the_engine_is_journaled_as_a_lookahead_error(short_plan_factory: Any,
                                                                            open_lake: Lake) -> None:
    early = short_plan_factory("backtest.feature_warm_blocks=600")            # frames warm (strategies run) early
    plan = _plan_with(early, _book(early, "canary-peek", "canary.peek"))
    canary = bias.PeekingCanary()
    jr = SqliteJournal(":memory:", durable=False)
    res = run_pass(plan, open_lake, books=["canary-peek"], end=8_766_800, journal=jr,
                   extra_strategies={"canary-peek": [canary]})
    errs = bias.peek_errors(jr)
    assert canary.peeks > 0 and len(errs) == canary.peeks
    assert all("LookaheadError" in d for _, d in errs)
    assert res.books["canary-peek"].metrics.trades.fills == 0


def test_oracle_canary_shows_an_implausible_edge_and_delay_degrades_it(short_plan: BacktestPlan, open_lake: Lake) -> None:
    table = bias.oracle_table(open_lake, short_plan.start_block, short_plan.end_block, 1_200)
    assert table
    books = [_book(short_plan, f"oracle-d{d}", f"canary.oracle.d{d}") for d in (0, 600)]
    plan = _plan_with(short_plan, *books)
    o0 = bias.OracleCanary(table, strategy_id="canary.oracle.d0", every_blocks=300, top_n=3)
    inner = bias.OracleCanary(table, strategy_id="canary.oracle.inner", every_blocks=300, top_n=3)
    o600 = bias.DelayedSignals(inner, 600, strategy_id="canary.oracle.d600")
    res = run_pass(plan, open_lake, books=["oracle-d0", "oracle-d600", "base-ew-total"],
                   extra_strategies={"oracle-d0": [o0], "oracle-d600": [o600]})
    r0 = res.books["oracle-d0"].metrics
    r600 = res.books["oracle-d600"].metrics
    ew = res.books["base-ew-total"].metrics
    assert r0.trades.buys > 0
    # the harness can see an edge: the oracle beats the EW benchmark (price component, before costs) ...
    assert r0.components_tao["price"] > ew.components_tao["price"]
    # ... and delaying the same information degrades it
    assert r0.components_tao["price"] >= r600.components_tao["price"]


def test_shuffled_signals_keep_the_book_shape_and_change_the_names(short_plan: BacktestPlan, open_lake: Lake) -> None:
    sl = SleeveCfg(StrategyId("baseline.ew_total"), Stage.RESEARCH, Ppm(PPM))
    inner = build_strategy(sl)
    shuffled = bias.ShuffledSignals(inner, seed=1, strategy_id="placebo.shuffled")
    plan = _plan_with(short_plan, _book(short_plan, "placebo-shuffled", "placebo.shuffled"))
    jr = SqliteJournal(":memory:", durable=False)
    res = run_pass(plan, open_lake, books=["placebo-shuffled", "base-ew-total"], journal=jr,
                   extra_strategies={"placebo-shuffled": [shuffled]})
    from taotrader.core.events import DecisionTrace
    from taotrader.data.journal import decode_record
    sh: list[Any] = []
    ew: list[Any] = []
    for rec in jr.read(1):
        if rec.kind == "decision_trace":
            ev = decode_record(rec)
            assert isinstance(ev, DecisionTrace)
            tg = [s for s in ev.signals if s.kind is SignalKind.TARGET]
            (sh if ev.book == "placebo-shuffled" else ew).append((ev.block, sorted(int(s.key.netuid) for s in tg)))
    paired = [(a, b) for a, b in zip(sh, ew, strict=False) if a[1] or b[1]]
    assert paired
    assert all(len(a[1]) == len(b[1]) for a, b in paired)       # same number of targets as the wrapped strategy
    assert any(a[1] != b[1] for a, b in paired)                  # but on different names
    assert res.books["placebo-shuffled"].metrics.max_identity_error_rao <= 1


def test_future_truncation_agrees_before_k_minus_latency(short_plan: BacktestPlan, open_lake: Lake) -> None:
    k, m = 8_775_000, 600
    ja, jb = SqliteJournal(":memory:", durable=False), SqliteJournal(":memory:", durable=False)
    books = ["base-ew-total", "carry"]
    run_pass(short_plan, open_lake, books=books, end=k, journal=ja, observe=False)
    run_pass(short_plan, open_lake, books=books, end=k + m, journal=jb, observe=False)
    latency = short_plan.book("carry").exec.finality_lag_blocks + short_plan.book("carry").exec.latency_blocks
    ok, n = bias.truncation_agrees(ja, jb, k, latency)
    assert ok and n > 0
    assert bias.decision_records(jb, before=None) != bias.decision_records(ja, before=None)   # b saw more blocks
