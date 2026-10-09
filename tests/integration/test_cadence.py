"""Section 10.3 item 8: cadence invariance - slow strategies at stride 60 vs 300 agree within sampling error.

The EW baselines rebalance daily (absolute 7,200-block buckets, so both strides decide at the same blocks). Every WP9
strategy declares min_cadence_blocks = 60, which the Runner's data contract refuses on a 300-block source; the test
therefore wraps the SAME baseline implementation in a declaration-only wrapper (min_cadence_blocks = 300, on_tick
delegated unchanged) and runs it at stride 300 over the whole mini-lake window; the stride-60 side is the golden
replay of the same books (the wrapper changes nothing but the declaration, so its stride-60 decisions are the golden
ones).

Asserted: the target names agree at the common daily decision blocks (>= 90% of them identical), the daily return paths
correlate (>= 0.8), and the total returns differ by less than 1 percentage point.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from taotrader.backtest.runner import run_pass
from taotrader.core.config import SleeveCfg
from taotrader.core.events import DecisionTrace
from taotrader.core.protocols import Strategy, TickContext
from taotrader.core.signals import SignalKind, StrategyOutput
from taotrader.core.units import PPM, Ppm, Stage, StrategyId
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.strategies.base import build_strategy

BOOKS = ("base-ew-total", "base-ew-price")


class Slow:
    """Declaration-only wrapper: the same strategy, declared evaluable on 300-block data."""

    def __init__(self, inner: Strategy, min_cadence: int) -> None:
        self.inner = inner
        self.id = inner.id
        self.decide_every_blocks = inner.decide_every_blocks
        self.wake_on = inner.wake_on
        self.min_cadence_blocks = min_cadence
        self.valid_from_block = inner.valid_from_block
        self.declares_dilution = inner.declares_dilution

    def initial_memory(self) -> object:
        return self.inner.initial_memory()

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        return self.inner.on_tick(ctx, memory)


def _wrapped(plan: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for b in BOOKS:
        (sl,) = plan.book(b).sleeves
        out[b] = [Slow(build_strategy(SleeveCfg(StrategyId(sl.strategy), Stage.RESEARCH, Ppm(PPM), sl.params)), 300)]
    return out


def _targets(j: SqliteJournal, book: str) -> dict[int, tuple[int, ...]]:
    out: dict[int, tuple[int, ...]] = {}
    for rec in j.read(1):
        if rec.kind == "decision_trace" and rec.book == book:
            ev = decode_record(rec)
            assert isinstance(ev, DecisionTrace)
            if ev.strategies_run:
                out[int(ev.block)] = tuple(sorted(int(s.key.netuid) for s in ev.signals if s.kind is SignalKind.TARGET))
    return out


def test_stride_60_and_300_agree(itx: Any, golden: Any, golden_plan: Any, lake: Lake) -> None:
    r60, _, path = golden                                   # stride 60: the golden replay (same implementations)
    j60 = SqliteJournal(str(path), readonly=True)
    j300 = SqliteJournal(":memory:", durable=False)
    r300 = run_pass(golden_plan, lake, books=list(BOOKS), stride=300, journal=j300, extra_strategies=_wrapped(golden_plan))
    assert r60.ticks > 4 * r300.ticks > 0
    for book in BOOKS:
        t60, t300 = _targets(j60, book), _targets(j300, book)
        common = sorted(set(t60) & set(t300))
        assert len(common) >= 5, (book, sorted(t60)[:5], sorted(t300)[:5])
        same = sum(1 for b in common if t60[b] == t300[b])
        assert same >= 0.9 * len(common), (book, same, len(common))
        m60, m300 = r60.books[book].metrics, r300.books[book].metrics
        assert abs(m60.total_return_pct - m300.total_return_pct) < 1.0, (book, m60.total_return_pct, m300.total_return_pct)
        a, b = np.asarray(m60.daily_returns), np.asarray(m300.daily_returns)
        n = min(len(a), len(b))
        assert n >= 5
        assert float(np.corrcoef(a[:n], b[:n])[0, 1]) >= 0.8
    j60.close()
