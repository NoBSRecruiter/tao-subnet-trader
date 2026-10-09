"""taotrader/reports/tables.py - report tables and CSV export (WP10; DESIGN.md sections 8.9, 8.11, 11 WP10).

Every table is (header, rows) of plain values, written as CSV next to the HTML report. Tables:
- `book_table`: per book - variant labels, days, mean daily net, NW t, 95% stationary-bootstrap CI, total return,
  max drawdown, turnover, fee drag, hit rate, average hold, time in cash, orphans and invariant breaches.
- `attribution_table`: per book - price / yield / fees / shortfall / dereg / liquidity attribution in TAO.
- `impact_table`: every base book at each impact bound present in the pass (TEMPORARY headline and PERSISTENT
  optimistic bound side by side).
- `regime_table`: per book and protocol.regimes regime - days and mean daily net.
- `failure_table`: fills and failures split by exact_block (per-block outcomes vs stride-evaluated), limit-failure
  (PriceLimitExceeded + SlippageTooHigh) rates per split (section 3.12 / FT7).
- `subnet_table`, `universe_table`, `nav_table`, `study_tests_table`.
"""
from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..backtest.metrics import COMPONENTS, BookMetrics, daily_points
from ..backtest.runner import PassResult
from ..backtest.stats import cscv_pbo, deflated_sharpe, newey_west_t, stationary_bootstrap_ci
from ..core.units import RAO_PER_TAO, Block
from ..protocol.regimes import regime_at

__all__ = [
    "Table", "attribution_table", "book_table", "failure_table", "impact_table", "nav_table", "regime_table",
    "study_tests_table", "subnet_table", "universe_table", "write_csv",
]

Table = tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]


def _f(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def write_csv(path: str | Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return p


def book_table(res: PassResult, variants: Mapping[str, Any] | None = None, *, seed: int = 0, n_trials: int = 1) -> Table:
    """Per-book statistics; the deflated Sharpe ratio is computed against `n_trials` (the registry's trial count)."""
    hdr = ("book", "base", "impact", "dereg", "days", "mean %/day", "NW t", "95% CI %/day", "total %", "max DD %",
           "turnover %/day", "fee drag %", "hit rate %", "avg hold d", "time in cash %", "deflated SR", "orphans",
           "breaches")
    rows: list[tuple[Any, ...]] = []
    for bk in sorted(res.books):
        o = res.books[bk]
        m = o.metrics
        v = (variants or {}).get(bk)
        ci = stationary_bootstrap_ci(m.daily_returns, mean_block=7, n_boot=1_000, seed=seed, label=bk)
        rows.append((bk, getattr(v, "base", bk), getattr(v, "impact", ""), getattr(v, "dereg", ""), m.days,
                     _f(m.mean_daily_net_pct), _f(newey_west_t(m.daily_returns, 5), 2),
                     "n/a" if ci is None else f"[{100 * ci.lo:.4f}, {100 * ci.hi:.4f}]", _f(m.total_return_pct, 3),
                     _f(m.max_drawdown_pct, 3), _f(m.turnover_per_day_pct, 3), _f(m.fee_drag_pct), _f(m.hit_rate_pct, 1),
                     _f(m.avg_hold_days, 2), _f(m.time_in_cash_pct, 1),
                     _f(deflated_sharpe(m.daily_returns, max(n_trials, 1)), 3), o.orphans, len(o.breaches)))
    return hdr, tuple(rows)


ATTRIBUTION: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price", ("price",)), ("yield", ("yield_",)), ("fees", ("swap_fee", "tx_fee", "failed")), ("dereg", ("dereg",)),
    ("shortfall", ("shortfall",)), ("liquidity", ("liquidity",)))


def attribution(m: BookMetrics) -> dict[str, float]:
    return {name: sum(m.components_tao.get(c, 0.0) for c in comps) for name, comps in ATTRIBUTION}


def attribution_table(res: PassResult) -> Table:
    hdr = ("book", *(a for a, _ in ATTRIBUTION), "net (TAO)", "capital (TAO)", "max identity error (rao)")
    rows: list[tuple[Any, ...]] = []
    for bk in sorted(res.books):
        m = res.books[bk].metrics
        at = attribution(m)
        rows.append((bk, *(_f(at[a], 6) for a, _ in ATTRIBUTION), _f(sum(at.values()), 6),
                     _f(m.components_tao.get("capital", 0.0), 3), _f(m.max_identity_error_rao, 3)))
    return hdr, tuple(rows)


def impact_table(res: PassResult, variants: Mapping[str, Any]) -> Table:
    by_base: dict[str, dict[str, BookMetrics]] = {}
    for bk, o in res.books.items():
        v = variants.get(bk)
        if v is None or v.dereg != variants.get(v.base, v).dereg:
            continue
        by_base.setdefault(str(v.base), {})[str(v.impact)] = o.metrics
    impacts = sorted({i for d in by_base.values() for i in d}, key=lambda s: (s != "temporary", s != "persistent", s))
    hdr = ("base book", *(f"{i} mean %/day" for i in impacts), *(f"{i} total %" for i in impacts))
    rows = []
    for base in sorted(by_base):
        d = by_base[base]
        rows.append((base, *(_f(d[i].mean_daily_net_pct) if i in d else "-" for i in impacts),
                     *(_f(d[i].total_return_pct, 3) if i in d else "-" for i in impacts)))
    return hdr, tuple(rows)


def regime_table(res: PassResult) -> Table:
    hdr = ("book", "regime", "days", "mean %/day")
    rows: list[tuple[Any, ...]] = []
    for bk in sorted(res.books):
        groups: dict[str, list[float]] = {}
        prev = 0
        for d in daily_points(res.books[bk].points):
            base = prev + d.flows
            if base > 0:
                groups.setdefault(regime_at(Block(d.block)).regime_id, []).append((d.nav - prev - d.flows) / base)
            prev = d.nav
        for rid, rs in groups.items():
            rows.append((bk, rid, len(rs), _f(100 * sum(rs) / len(rs))))
    return hdr, tuple(rows)


def failure_table(res: PassResult) -> Table:
    hdr = ("book", "fills exact", "fills stride", "failures exact", "failures stride", "limit fail rate exact",
           "limit fail rate stride")
    rows: list[tuple[Any, ...]] = []
    for bk in sorted(res.books):
        t = res.books[bk].metrics.trades
        ex = t.fills_exact + t.failures_exact
        st = t.fills_stride + t.failures_stride
        rows.append((bk, t.fills_exact, t.fills_stride, t.failures_exact, t.failures_stride,
                     _f(t.limit_failures_exact / ex if ex else None), _f(t.limit_failures_stride / st if st else None)))
    return hdr, tuple(rows)


def subnet_table(res: PassResult, book: str) -> Table:
    m = res.books[book].metrics
    rows = tuple((n, _f(v, 6)) for n, v in sorted(m.per_netuid_tao.items(), key=lambda kv: -kv[1]))
    return ("netuid", "contribution (TAO)"), rows


def universe_table(counts: Sequence[tuple[int, int, int]]) -> Table:
    seen: dict[int, tuple[int, int, int]] = {}
    for b, ts, n in counts:
        seen.setdefault(ts // 86_400_000, (b, ts, n))
    return ("block", "utc day", "eligible A-G"), tuple((b, ts // 86_400_000, n) for b, ts, n in seen.values())


def nav_table(res: PassResult) -> Table:
    hdr = ("book", "utc day", "block", "NAV_liq (TAO)", "flows (TAO)")
    rows = []
    for bk in sorted(res.books):
        for d in daily_points(res.books[bk].points):
            rows.append((bk, d.day, d.block, _f(d.nav / RAO_PER_TAO, 6), _f(d.flows / RAO_PER_TAO, 6)))
    return hdr, tuple(rows)


def study_tests_table(studies: Sequence[Any]) -> Table:
    rows = tuple((t.study, t.test, t.verdict, t.value, t.threshold, t.detail) for s in studies for t in s.tests)
    return ("study", "test", "verdict", "value", "threshold", "detail"), rows


COMPONENT_NAMES = COMPONENTS


def pbo_of_pass(res: PassResult, n_splits: int = 16) -> tuple[float | None, int, int]:
    """CSCV probability of backtest overfitting over the pass's books (columns) and their common UTC days (rows):
    (PBO, days, books). None when there are fewer days than splits or fewer than two books."""
    rets = {bk: list(o.metrics.daily_returns) for bk, o in sorted(res.books.items()) if o.metrics.daily_returns}
    if len(rets) < 2:
        return None, 0, len(rets)
    t = min(len(v) for v in rets.values())
    matrix = [[rets[bk][i] for bk in rets] for i in range(t)]
    splits = min(n_splits, t - (t % 2))
    return (cscv_pbo(matrix, n_splits=splits) if splits >= 2 else None), t, len(rets)
