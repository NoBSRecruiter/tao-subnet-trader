"""taotrader/backtest/studies.py - offline studies S0-S7 with pre-registered thresholds (WP10; DESIGN.md 8.11, 10.4).

Every study reads ONE backtest pass (backtest.runner.arun_pass over the lake, with the production engine) plus the
generation-keyed panel recorded during that pass (`PanelRecorder`, hooked into the pass via on_tick): per tick and
generation the Feat fields, the universe A-G verdict and the P x I index of a sticky tracked hotkey. Thresholds come
from config/preregistration.toml (`[carry.tests]`, `[momentum.tests]`, `[mean_reversion_study]`, `[lcw.tests]`,
`[falsification]`, `[metrics]`), never from code literals. Each test returns a `TestResult` with a verdict:

    PASS | FAIL | KILL (the pre-registered kill condition holds) | REPORTED (no threshold, e.g. T0) |
    NOT_EVALUABLE (the lake/pass cannot evaluate it: too few events for the pre-registered power floor, a data series
    the lake does not hold - per-block windows, paper fills, observed payouts - with the reason in `detail`).

The default outcome of a weak sample is "not promoted" (section 8.9): NOT_EVALUABLE never counts as a pass.

Panel coverage rule (S0, S1; section 8.11): P x I needs the hotkey panel, collected per epoch from 8,466,531. A window
starting earlier is labelled "price-only + closed-form yield proxy" unless `panel_from` (the lake's collector_meta)
covers it; such windows are ineligible for promotion or kill decisions (their verdicts become NOT_EVALUABLE).

Studies:
- S0: EW total return (P x I, router hotkey, overlay guards, TEMPORARY costs) - the base-ew-total / base-ew-price books
  of the pass - plus the gross EW P x I index of each sleeve's eligible universe from the panel. T0: REPORTED.
- S1: factor re-run on the P x I panel: SMB (pool size), WML7, WML30, REV (1 d), 1 d / 7 d rank IC, entry lags
  {0, 300, 1,200} blocks, long-only and long-short, net of TEMPORARY round-trip costs at a realistic size.
- S2: universe counts (A-G eligible, carry-eligible, momentum-eligible) daily and at >= 10 dates.
- S3: carry T1 (realised vs closed-form yield), T2a (emission parity, model_ok share), T3 (price drift vs structural
  sell push), T4 (carry score spread / IC), T5 (cost stress from the pass's sensitivity books), T6 (router excess).
- S4: momentum F1 (IC at entry lags), F2 (net edge), F3 (day 2-3 continuation), F9 (sub-period signs, subnet share),
  F10 (beat random entry), F11 (flow match), F12 (capacity), F14 (impulses), F15 (decomposition identity).
- S5: mean-reversion event study E-MR (shock <= -8% log move in one stride, quiet filter, forward 1/6/12 h).
- S6: launches FT0 (Queued->Added lag, netuid = victim, seed), FT1-FT4, FT6, FT10.
- S7: overlay FT1-FT6, FT9, FT10 (section 10.4).

CLI (S0 on the full lake; the mini-lake command is in tests/integration/README):
  python -m taotrader.backtest.studies s0 --lake data/lake --out reports/s0 [--start N --end N] [--books a,b]
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ..core.state import ChainSnapshot
from ..core.units import BLOCKS_PER_DAY, RAO_PER_TAO, Hotkey, Rao, SubnetKey
from ..data.lake import Lake
from ..engine.runner import Runner
from ..ops.config_load import load_preregistration
from ..protocol.amm import ImpactBound, round_trip_cost_ppm
from .metrics import BookMetrics, capacity_at_half_edge
from .runner import PassResult
from .stats import newey_west_t, series_stats, spearman, stationary_bootstrap_ci, wilson_ci

__all__ = [
    "PANEL_FROM_BLOCK", "PanelRecorder", "PanelRow", "StudyResult", "TestResult", "coverage_label", "run_all", "s0",
    "s1", "s2", "s3", "s4", "s5", "s6", "s7",
]

PANEL_FROM_BLOCK: Final[int] = 8_466_531          # post-June hotkey panel (WP4 collector default)
PASS, FAIL, KILL, REPORTED, NOT_EVALUABLE = "PASS", "FAIL", "KILL", "REPORTED", "NOT_EVALUABLE"
TOTAL_RETURN: Final[str] = "total-return (P x I)"
PRICE_ONLY: Final[str] = "price-only + closed-form yield proxy"
HOUR_BLOCKS: Final[int] = 300


@dataclass(frozen=True, slots=True)
class TestResult:
    study: str
    test: str
    verdict: str
    value: str
    threshold: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StudyResult:
    study: str
    title: str
    window: tuple[int, int]
    coverage: str
    tests: tuple[TestResult, ...]
    tables: Mapping[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def verdicts(self) -> dict[str, str]:
        return {t.test: t.verdict for t in self.tests}


def coverage_label(start: int, panel_from: int = PANEL_FROM_BLOCK) -> str:
    return TOTAL_RETURN if start >= panel_from else PRICE_ONLY


# ------------------------------------------------------------------------------------------------ the panel
@dataclass(frozen=True, slots=True)
class PanelRow:
    block: int
    ts_ms: int
    key: SubnetKey
    spot: float
    pool_tao: float
    hotkey: str | None              # the sticky tracked hotkey of this generation (P x I index source)
    index: float | None
    ret_1d: float | None
    ret_7d: float | None
    flow_1d: float | None
    flow_z_1d: float | None
    yield_net_day: float | None
    yield_cf_day: float
    sell_push_day: float
    cb_push_day: float
    eligible: bool                  # universe sections A-G
    enabled: bool
    started: bool
    prune_rank: int | None
    immune: bool
    obs_emis_day: float
    emis_day: float
    model_ok: bool
    rt_cost_ppm: int | None         # TEMPORARY round trip at the study size (raw pool)
    candidates: tuple[tuple[str, float, float | None], ...] = ()   # eligible router candidates, best first:
                                                                   # (hotkey, score_ppm_day, index at this block)


class PanelRecorder:
    """on_tick hook for backtest.runner.arun_pass: records the generation-keyed panel from the shared FeatureEngine."""

    def __init__(self, *, study_size_tao: float = 1.0, tx_fees_rao: int = 1_028_000 + 837_000) -> None:
        self.rows: list[PanelRow] = []
        self.size = Rao(int(study_size_tao * RAO_PER_TAO))
        self.tx = tx_fees_rao
        self._sticky: dict[SubnetKey, str] = {}
        self.universe_counts: list[tuple[int, int, int]] = []    # (block, ts_ms, eligible count)
        self.launches: list[Any] = []
        self.dereg_blocks: list[tuple[int, SubnetKey]] = []
        self.emission_off: list[tuple[int, SubnetKey]] = []
        self.blocks: list[int] = []
        self._prev: ChainSnapshot | None = None

    def __call__(self, runner: Runner, snap: ChainSnapshot) -> None:
        fe: Any = runner.features
        frame = getattr(fe, "last_frame", None)
        if frame is None or frame.block != snap.block:
            return
        uni = {r.key: r for r in getattr(fe, "last_universe", ())}
        self.blocks.append(int(snap.block))
        self.universe_counts.append((int(snap.block), int(snap.timestamp_ms), int(frame.universe_eligible)))
        prev = self._prev
        self._prev = snap
        for k in sorted(frame.feats, key=lambda x: (x.netuid, x.reg_at)):
            f = frame.feats[k]
            s = snap.get(k)
            if s is None:
                continue
            hk = self._sticky.get(k)
            idx: float | None = None
            if hk is None or s.hotkey(Hotkey(hk)) is None:
                cand = f.best_candidate or (max(s.hotkeys, key=lambda h: (h.total_alpha, h.hotkey)).hotkey
                                            if s.hotkeys else None)
                if cand is not None:
                    hk = self._sticky[k] = str(cand)
            if hk is not None:
                h = s.hotkey(Hotkey(hk))
                if h is not None and h.total_shares > 0:
                    idx = float(h.index())
            u = uni.get(k)
            try:
                rt: int | None = int(round_trip_cost_ppm(s.pool, self.size, ImpactBound.TEMPORARY, self.tx))
            except Exception:
                rt = None
            self.rows.append(PanelRow(
                block=int(snap.block), ts_ms=int(snap.timestamp_ms), key=k, spot=float(f.spot), pool_tao=float(f.pool_tao),
                hotkey=hk, index=idx, ret_1d=f.ret_1d, ret_7d=f.ret_7d, flow_1d=f.flow_1d, flow_z_1d=f.flow_z_1d,
                yield_net_day=f.yield_net_day, yield_cf_day=float(f.yield_cf_gross_day), sell_push_day=float(f.sell_push_day),
                cb_push_day=float(f.cb_push_day), eligible=bool(u is not None and u.eligible),
                enabled=bool(s.emission_enabled), started=s.first_emission_block is not None, prune_rank=f.prune_rank,
                immune=bool(f.immune), obs_emis_day=float(f.obs_emis_tao_day), emis_day=float(f.emis_tao_day),
                model_ok=bool(frame.emission.model_ok), rt_cost_ppm=rt,
                candidates=tuple((str(c.hotkey), float(c.score_ppm_day), _index_of(s, c.hotkey))
                                 for c in f.router_candidates if c.eligible)))
            if prev is not None:
                ps = prev.get(k)
                if ps is not None and ps.emission_enabled and not s.emission_enabled:
                    self.emission_off.append((int(snap.block), k))
        if prev is not None:
            gone = {s.key for s in prev.subnets} - {s.key for s in snap.subnets}
            for k in sorted(gone, key=lambda x: (x.netuid, x.reg_at)):
                self.dereg_blocks.append((int(snap.block), k))
        gk = getattr(fe, "gatekeeper", None)
        if gk is not None:
            self.launches = list(gk.records())


def _index_of(s: Any, hotkey: Any) -> float | None:
    h = s.hotkey(hotkey)
    return float(h.index()) if h is not None and h.total_shares > 0 else None


def router_forward_excess(panel: _Panel, h: int = BLOCKS_PER_DAY) -> list[tuple[int, int, float, bool]]:
    """Out-of-sample router check (T6 / FT9): at each block and generation with >= 2 eligible candidates, the forward
    index growth ln(I_{t+h} / I_t) of the router's choice (the best candidate at t) against the median of the eligible
    candidates' forward growth. Returns (block, netuid, excess per day, chosen >= median)."""
    out: list[tuple[int, int, float, bool]] = []
    for r in panel.rows:
        if len(r.candidates) < 2:
            continue
        b1 = panel.block_at_or_after(r.block + h)
        if b1 is None or b1 - r.block > h + 60:
            continue
        z = panel.at.get((b1, r.key))
        if z is None:
            continue
        later = {hk: ix for hk, _, ix in z.candidates}
        growth: list[tuple[str, float]] = []
        for hk, _, ix in r.candidates:
            iz = later.get(hk)
            if ix is not None and iz is not None and ix > 0 and iz > 0:
                growth.append((hk, math.log(iz / ix)))
        if len(growth) < 2 or growth[0][0] != r.candidates[0][0]:
            continue
        vals = sorted(g for _, g in growth)
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        days = (b1 - r.block) / BLOCKS_PER_DAY
        out.append((r.block, int(r.key.netuid), (growth[0][1] - med) / days, growth[0][1] >= med))
    return out


class _Panel:
    """Lookups over the recorded rows: by (block, key), sorted blocks, forward P x I and price returns."""

    def __init__(self, rows: Sequence[PanelRow]) -> None:
        self.rows = list(rows)
        self.at: dict[tuple[int, SubnetKey], PanelRow] = {(r.block, r.key): r for r in rows}
        self.blocks = sorted({r.block for r in rows})
        self.by_block: dict[int, list[PanelRow]] = {}
        for r in rows:
            self.by_block.setdefault(r.block, []).append(r)

    def block_at_or_after(self, b: int) -> int | None:
        import bisect
        i = bisect.bisect_left(self.blocks, b)
        return self.blocks[i] if i < len(self.blocks) else None

    def fwd(self, r: PanelRow, h: int, *, total: bool = True, lag: int = 0) -> float | None:
        """log P x I (or price) return from block r.block + lag to r.block + lag + h on the same generation and hotkey."""
        b0 = self.block_at_or_after(r.block + lag) if lag else r.block
        if b0 is None:
            return None
        b1 = self.block_at_or_after(b0 + h)
        if b1 is None or b1 - b0 > h + 60:
            return None
        a, z = self.at.get((b0, r.key)), self.at.get((b1, r.key))
        if a is None or z is None or a.spot <= 0 or z.spot <= 0:
            return None
        ret = math.log(z.spot / a.spot)
        if total:
            if a.index is None or z.index is None or a.hotkey != z.hotkey or a.index <= 0:
                return None
            ret += math.log(z.index / a.index)
        return ret


# ------------------------------------------------------------------------------------------------ helpers
def _prereg() -> dict[str, Any]:
    return load_preregistration()


def _fmt(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


def _daily_cross_section(panel: _Panel, score: Callable[[PanelRow], float | None], h: int, *, lag: int = 0,
                         total: bool = True, every: int = BLOCKS_PER_DAY, eligible_only: bool = True,
                         net_cost: bool = False) -> tuple[list[float], list[float], list[float]]:
    """Per decision date (every `every` blocks): rank IC of score vs forward return, top-minus-bottom tercile spread
    and the long-only top-tercile excess over the EW cross-section (net of round-trip cost when net_cost)."""
    ics: list[float] = []
    ls: list[float] = []
    lo: list[float] = []
    last = -10**18
    for b in panel.blocks:
        if b - last < every:
            continue
        last = b
        xs: list[tuple[float, float, PanelRow]] = []
        for r in panel.by_block.get(b, []):
            if eligible_only and not r.eligible:
                continue
            sc = score(r)
            fr = panel.fwd(r, h, total=total, lag=lag)
            if sc is None or fr is None:
                continue
            if net_cost and r.rt_cost_ppm is not None:
                fr -= r.rt_cost_ppm / 1e6
            xs.append((sc, fr, r))
        if len(xs) < 6:
            continue
        ic = spearman([x[0] for x in xs], [x[1] for x in xs])
        if ic is not None:
            ics.append(ic)
        xs.sort(key=lambda t: t[0])
        n3 = len(xs) // 3
        bot, top = xs[:n3], xs[-n3:]
        mean_all = sum(x[1] for x in xs) / len(xs)
        ls.append(sum(x[1] for x in top) / n3 - sum(x[1] for x in bot) / n3)
        lo.append(sum(x[1] for x in top) / n3 - mean_all)
    return ics, ls, lo


def _t_verdict(study: str, test: str, xs: Sequence[float], *, mean_min: float, t_min: float, min_n: int = 10,
               scale: float = 1.0, unit: str = "", kill_if: Callable[[float, float | None], bool] | None = None) -> TestResult:
    if len(xs) < min_n:
        return TestResult(study, test, NOT_EVALUABLE, f"n={len(xs)}", f"n>={min_n}", "too few observations")
    m = sum(xs) / len(xs) * scale
    t = newey_west_t(xs, 5)
    val = f"mean={m:.4f}{unit} t={_fmt(t, 2)} n={len(xs)}"
    thr = f"mean>={mean_min}{unit} t>={t_min}"
    if kill_if is not None and kill_if(m, t):
        return TestResult(study, test, KILL, val, thr)
    ok = m >= mean_min and t is not None and t >= t_min
    return TestResult(study, test, PASS if ok else FAIL, val, thr)


def _book(res: PassResult, book: str) -> BookMetrics | None:
    o = res.books.get(book)
    return None if o is None else o.metrics


def _gate_coverage(results: list[TestResult], coverage: str) -> list[TestResult]:
    """Price-only windows are ineligible for promotion or kill decisions (section 8.11)."""
    if coverage == TOTAL_RETURN:
        return results
    return [TestResult(t.study, t.test, NOT_EVALUABLE if t.verdict in (PASS, FAIL, KILL) else t.verdict, t.value,
                       t.threshold, f"{PRICE_ONLY} window; ineligible ({t.verdict}) {t.detail}".strip()) for t in results]


def _metrics_row(name: str, m: BookMetrics, *, seed: int = 0) -> tuple[Any, ...]:
    st = series_stats(m.daily_returns, seed=seed, label=name, n_boot=1_000)
    ci = st.ci
    return (name, m.days, f"{m.mean_daily_net_pct:.4f}", _fmt(st.nw_t, 2),
            "n/a" if ci is None else f"[{100 * ci.lo:.4f}, {100 * ci.hi:.4f}]", f"{m.total_return_pct:.3f}",
            f"{m.max_drawdown_pct:.3f}", f"{m.turnover_per_day_pct:.3f}", f"{m.fee_drag_pct:.4f}",
            f"{m.time_in_cash_pct:.1f}")


METRICS_HEADER: Final[tuple[str, ...]] = ("book", "days", "mean %/day", "NW t", "95% CI %/day", "total %", "max DD %",
                                          "turnover %/day", "fee drag %", "time in cash %")


# ------------------------------------------------------------------------------------------------ S0
def s0(res: PassResult, panel_rows: Sequence[PanelRow], *, window: tuple[int, int], panel_from: int = PANEL_FROM_BLOCK,
       sleeves: Sequence[str] = ("carry", "momentum", "blend")) -> StudyResult:
    """The benchmark every module must beat: EW total return of eligible universes (books) + panel EW index."""
    cov = coverage_label(window[0], panel_from)
    panel = _Panel(panel_rows)
    rows: list[tuple[Any, ...]] = []
    for name in ("base-ew-total", "base-ew-price", "base-cash", "base-random", *sleeves):
        m = _book(res, name)
        if m is not None:
            rows.append(_metrics_row(name, m, seed=int(res.identity.seed)))
    # gross EW P x I index of the eligible universe (and the price-only twin), daily
    idx_rows: list[tuple[Any, ...]] = []
    for label, pred in (("eligible A-G", lambda r: r.eligible),
                        ("carry-like (eligible, yield known)", lambda r: r.eligible and r.yield_net_day is not None),
                        ("all started", lambda r: r.started)):
        tr, pr = _ew_index_returns(panel, pred)
        st = series_stats(tr, seed=int(res.identity.seed), label=f"s0-{label}", n_boot=1_000)
        idx_rows.append((label, len(tr), f"{100 * st.mean:.4f}", _fmt(st.nw_t, 2),
                         f"{100 * (sum(pr) / len(pr) if pr else 0.0):.4f}"))
    ewp = _book(res, "base-ew-price")
    ewt = _book(res, "base-ew-total")
    tests = [TestResult("S0", "T0", REPORTED, "see benchmark table", "reported, no threshold",
                        "every module must beat it out of sample")]
    if ewp is not None:
        po = _price_only_pct(ewp)
        tests.append(TestResult("S0", "ew_price_sign", REPORTED, f"price-only={po:.3f}% (total {ewp.total_return_pct:.3f}%)",
                                "brief: negative over the gate rank-32 window", "negative" if po < 0 else "non-negative"))
    if ewt is not None:
        y = 100.0 * ewt.components_tao.get("yield_", 0.0) / ewt.start_nav_tao if ewt.start_nav_tao else 0.0
        tests.append(TestResult("S0", "yield_minus_price", REPORTED,
                                f"yield={y:.3f}% of NAV; P x I total {ewt.total_return_pct:.3f}% vs price-only "
                                f"{_price_only_pct(ewt):.3f}%", "reported"))
    return StudyResult("S0", "EW total-return benchmark of eligible universes", window, cov, tuple(tests),
                       {"benchmark_books": (METRICS_HEADER, tuple(rows)),
                        "ew_index_gross": (("universe", "days", "mean P x I %/day", "NW t", "mean price %/day"),
                                           tuple(idx_rows))},
                       (f"coverage: {cov}", "books: TEMPORARY impact, overlay guards, router hotkey (production engine)",
                        "ew_index_gross: daily-rebalanced EW over the universe, no costs (panel)"))


def _price_only_pct(m: BookMetrics) -> float:
    """The price component of a book's P&L as % of its starting NAV (the price-only baseline view)."""
    return 100.0 * m.components_tao.get("price", 0.0) / m.start_nav_tao if m.start_nav_tao else 0.0


def _ew_index_returns(panel: _Panel, pred: Callable[[PanelRow], bool]) -> tuple[list[float], list[float]]:
    tr: list[float] = []
    pr: list[float] = []
    last = -10**18
    for b in panel.blocks:
        if b - last < BLOCKS_PER_DAY:
            continue
        last = b
        ts, ps = [], []
        for r in panel.by_block.get(b, []):
            if not pred(r):
                continue
            t = panel.fwd(r, BLOCKS_PER_DAY, total=True)
            p = panel.fwd(r, BLOCKS_PER_DAY, total=False)
            if t is not None:
                ts.append(math.expm1(t))
            if p is not None:
                ps.append(math.expm1(p))
        if ts:
            tr.append(sum(ts) / len(ts))
        if ps:
            pr.append(sum(ps) / len(ps))
    return tr, pr


# ------------------------------------------------------------------------------------------------ S1
def s1(panel_rows: Sequence[PanelRow], *, window: tuple[int, int], panel_from: int = PANEL_FROM_BLOCK) -> StudyResult:
    """Factor re-run on the generation-keyed, payout-inclusive P x I panel (resets the carry / momentum priors)."""
    cov = coverage_label(window[0], panel_from)
    panel = _Panel(panel_rows)
    pre = _prereg()
    lags = [int(x) for x in pre["momentum"]["tests"]["F1"]["entry_lags_blocks"]]
    factors: dict[str, Callable[[PanelRow], float | None]] = {
        "SMB": lambda r: -r.pool_tao,
        "WML7": lambda r: r.ret_7d,
        "WML30": lambda r: _ret_back(panel, r, 30 * BLOCKS_PER_DAY),
        "REV": lambda r: None if r.ret_1d is None else -r.ret_1d,
    }
    table: list[tuple[Any, ...]] = []
    for name, sc in factors.items():
        for h_days in (1, 7):
            for lag in lags:
                ics, ls, lo = _daily_cross_section(panel, sc, h_days * BLOCKS_PER_DAY, lag=lag, net_cost=False)
                _, ls_n, lo_n = _daily_cross_section(panel, sc, h_days * BLOCKS_PER_DAY, lag=lag, net_cost=True)
                table.append((name, f"{h_days}d", lag, len(ics), _fmt(_mean(ics)), _fmt(newey_west_t(ics, 5), 2),
                              _fmt(_pct(_mean(ls))), _fmt(_pct(_mean(ls_n))), _fmt(_pct(_mean(lo))), _fmt(_pct(_mean(lo_n)))))
    tests = [TestResult("S1", "factor_table", REPORTED, f"{len(table)} rows", "reported; resets the F0 priors")]
    return StudyResult("S1", "Factor re-run on the P x I panel", window, cov, tuple(tests),
                       {"factors": (("factor", "horizon", "entry lag", "dates", "mean IC", "IC NW t", "L/S gross %",
                                     "L/S net %", "long gross excess %", "long net excess %"), tuple(table))},
                       (f"coverage: {cov}", "net = minus the TEMPORARY round trip at the study size (1 TAO)"))


def _ret_back(panel: _Panel, r: PanelRow, h: int) -> float | None:
    import bisect
    i = bisect.bisect_left(panel.blocks, r.block - h)
    if i >= len(panel.blocks):
        return None
    b0 = panel.blocks[i]
    if r.block - b0 < h - 60:
        return None
    a = panel.at.get((b0, r.key))
    if a is None or a.spot <= 0 or r.spot <= 0:
        return None
    return math.log(r.spot / a.spot)


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pct(x: float | None) -> float | None:
    return None if x is None else 100.0 * x


# ------------------------------------------------------------------------------------------------ S2
def s2(rec: PanelRecorder, *, window: tuple[int, int], dates: int = 10) -> StudyResult:
    """Universe counts per sleeve at >= 10 dates and daily (capacity statements)."""
    panel = _Panel(rec.rows)
    daily: list[tuple[Any, ...]] = []
    last_day = None
    for b in panel.blocks:
        rows = panel.by_block[b]
        day = rows[0].ts_ms // 86_400_000 if rows else 0
        if day == last_day:
            continue
        last_day = day
        elig = sum(1 for r in rows if r.eligible)
        carry = sum(1 for r in rows if r.eligible and r.yield_net_day is not None and r.yield_net_day > 0)
        mom = sum(1 for r in rows if r.eligible and r.ret_7d is not None)
        daily.append((b, day, len(rows), elig, carry, mom))
    step = max(1, len(daily) // max(dates, 1))
    sampled = daily[::step] if len(daily) > dates else daily
    n_ok = len(daily) >= dates
    tests = [TestResult("S2", "dates", PASS if n_ok else NOT_EVALUABLE, f"{len(daily)} daily dates", f">= {dates} dates",
                        "" if n_ok else "window shorter than the pre-registered date count")]
    hdr = ("block", "utc day", "subnets", "eligible A-G", "carry-eligible", "momentum-eligible")
    return StudyResult("S2", "Universe counts", window, TOTAL_RETURN, tuple(tests),
                       {"daily": (hdr, tuple(daily)), "sampled": (hdr, tuple(sampled))})


# ------------------------------------------------------------------------------------------------ S3 carry
def s3(res: PassResult, panel_rows: Sequence[PanelRow], *, window: tuple[int, int],
       sensitivity: Mapping[str, BookMetrics] | None = None, panel_from: int = PANEL_FROM_BLOCK) -> StudyResult:
    cov = coverage_label(window[0], panel_from)
    pre = _prereg()["carry"]["tests"]
    panel = _Panel(panel_rows)
    out: list[TestResult] = []
    # T1: realised daily index growth vs closed-form gross yield (OLS slope through the origin and r2)
    xs, ys = [], []
    for r in panel.rows:
        if r.index is None or r.yield_cf_day <= 0 or not r.eligible:
            continue
        z = panel.at.get((_next_day(panel, r.block), r.key))
        if z is None or z.index is None or z.hotkey != r.hotkey or r.index <= 0:
            continue
        flow = abs(r.flow_1d) if r.flow_1d is not None else 0.0
        if flow > float(pre["T1"]["flow_filter_frac"]) * 100:
            continue
        xs.append(r.yield_cf_day)
        ys.append(math.log(z.index / r.index))
    if len(xs) >= 30:
        sxx = sum(x * x for x in xs)
        coef = sum(x * y for x, y in zip(xs, ys, strict=True)) / sxx if sxx > 0 else 0.0
        my = sum(ys) / len(ys)
        ss_tot = sum((y - my) ** 2 for y in ys)
        ss_res = sum((y - coef * x) ** 2 for x, y in zip(xs, ys, strict=True))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        lo, hi = pre["T1"]["coef_band"]
        ok = lo <= coef <= hi and r2 >= float(pre["T1"]["r2_min"])
        out.append(TestResult("S3", "T1", PASS if ok else FAIL, f"coef={coef:.3f} r2={r2:.3f} n={len(xs)}",
                              f"coef in [{lo}, {hi}], r2 >= {pre['T1']['r2_min']}"))
    else:
        out.append(TestResult("S3", "T1", NOT_EVALUABLE, f"n={len(xs)}", "n >= 30", "too few index pairs"))
    # T2a: emission parity share (EmissionView.model_ok, the single parity gate)
    oks = [r.model_ok for r in panel.rows]
    blocks_ok = {r.block: r.model_ok for r in panel.rows}
    share = sum(1 for v in blocks_ok.values() if v) / len(blocks_ok) if blocks_ok else 0.0
    out.append(TestResult("S3", "T2a", PASS if blocks_ok and share >= 0.9 else (FAIL if blocks_ok else NOT_EVALUABLE),
                          f"model_ok share={share:.3f} over {len(blocks_ok)} ticks",
                          f"median rel err <= {pre['T2a']['median_rel_err_max']}, >= {pre['T2a']['within_5pct_frac_min']} "
                          "within 5% (protocol.emission.parity_ok per tick)",
                          "" if oks else "no ticks"))
    # T3: forward price drift vs structural sell push (pooled slope and t)
    ds, ps = [], []
    for r in panel.rows:
        if not r.eligible or r.sell_push_day <= 0:
            continue
        f = panel.fwd(r, BLOCKS_PER_DAY, total=False)
        if f is not None:
            ds.append(f)
            ps.append(-r.sell_push_day)
    if len(ds) >= 100:
        mx, my = sum(ps) / len(ps), sum(ds) / len(ds)
        sxx = sum((x - mx) ** 2 for x in ps)
        beta = sum((x - mx) * (y - my) for x, y in zip(ps, ds, strict=True)) / sxx if sxx > 0 else 0.0
        resid = [y - my - beta * (x - mx) for x, y in zip(ps, ds, strict=True)]
        se = math.sqrt(sum(e * e for e in resid) / max(len(ds) - 2, 1) / sxx) if sxx > 0 else float("inf")
        t = beta / se if se > 0 else 0.0
        lo, hi = pre["T3"]["phi_band"]
        ok = lo <= beta <= hi and abs(t) >= float(pre["T3"]["combined_t_min"])
        out.append(TestResult("S3", "T3", PASS if ok else FAIL, f"phi_hat={beta:.3f} t={t:.2f} n={len(ds)}",
                              f"phi in [{lo}, {hi}], |t| >= {pre['T3']['combined_t_min']}"))
    else:
        out.append(TestResult("S3", "T3", NOT_EVALUABLE, f"n={len(ds)}", "n >= 100", "too few sell-push observations"))
    # T4a: carry score (realised net yield - sell push) spread and IC over 14-day holds
    ics, ls, _ = _daily_cross_section(panel, lambda r: None if r.yield_net_day is None else r.yield_net_day - r.sell_push_day,
                                      14 * BLOCKS_PER_DAY, every=7 * BLOCKS_PER_DAY)
    t4 = pre["T4a"]
    if len(ls) >= 4:
        sp = _mean(ls) or 0.0
        t4t = newey_west_t(ls, 5)
        if sp <= 0 or (t4t is not None and t4t < 1):
            verdict = KILL
        else:
            verdict = PASS if (100 * sp / 14 >= float(t4["spread_min_pct_day"]) and (t4t or 0) >= float(t4["spread_t_min"])
                               and (_mean(ics) or 0) >= float(t4["ic_min"])) else FAIL
        out.append(TestResult("S3", "T4a", verdict, f"spread={100 * sp / 14:.4f}%/day t={_fmt(t4t, 2)} IC={_fmt(_mean(ics))}",
                              f"spread >= {t4['spread_min_pct_day']}%/day, t >= {t4['spread_t_min']}, IC >= {t4['ic_min']}"))
    else:
        out.append(TestResult("S3", "T4a", NOT_EVALUABLE, f"{len(ls)} weekly dates", ">= 4 weekly dates (fit 45 d + trade 14 d)",
                              "window too short for the walk-forward"))
    # T5: cost stress (sensitivity books of the pass: fees x2 and latency x2 must stay >= 0; kill if < 0 at x1.5)
    out.append(_t5(sensitivity, "carry"))
    # T6: router - forward (out-of-sample) index growth of the chosen candidate minus the median eligible candidate,
    # averaged per block (subnets of one block are not independent), NW t over the block series
    per_block: dict[int, list[float]] = {}
    for b, _n, exd, _ok in router_forward_excess(panel):
        per_block.setdefault(b, []).append(100.0 * exd)
    ex = [sum(v) / len(v) for _, v in sorted(per_block.items())]
    t6 = pre["T6"]
    out.append(_t_verdict("S3", "T6", ex, mean_min=float(t6["excess_min_pct_day"]), t_min=float(t6["t_min"]), unit="%/day"))
    return StudyResult("S3", "Carry tests T1-T6", window, cov, tuple(_gate_coverage(out, cov)))


def _next_day(panel: _Panel, b: int) -> int:
    nb = panel.block_at_or_after(b + BLOCKS_PER_DAY)
    return -1 if nb is None else nb


def _t5(sensitivity: Mapping[str, BookMetrics] | None, base: str) -> TestResult:
    if not sensitivity:
        return TestResult("S3", "T5", NOT_EVALUABLE, "no sensitivity books", "net >= 0 at fees x2 and latency x2",
                          "run the sensitivity grid (backtest.runner.sensitivity_books)")
    worst = [(k, m.mean_daily_net_pct) for k, m in sensitivity.items() if k.startswith(base) and "-f2" in k]
    if not worst:
        return TestResult("S3", "T5", NOT_EVALUABLE, "no fees x2 variants", "net >= 0 at fees x2 and latency x2")
    k, v = min(worst, key=lambda kv: kv[1])
    return TestResult("S3", "T5", PASS if v >= 0 else KILL, f"worst {k}: {v:.4f}%/day", "net >= 0 at fees x2 / latency x2")


# ------------------------------------------------------------------------------------------------ S4 momentum
def s4(res: PassResult, panel_rows: Sequence[PanelRow], *, window: tuple[int, int],
       panel_from: int = PANEL_FROM_BLOCK, capacity: Mapping[int, BookMetrics] | None = None) -> StudyResult:
    """Momentum tests. `capacity`: the momentum book's metrics per capital (TAO) from the capacity sweep (F12)."""
    cov = coverage_label(window[0], panel_from)
    pre = _prereg()["momentum"]["tests"]
    panel = _Panel(panel_rows)
    out: list[TestResult] = []
    ic_by_lag: dict[int, list[float]] = {}
    for lag in [int(x) for x in pre["F1"]["entry_lags_blocks"]]:
        ic_by_lag[lag], _, _ = _daily_cross_section(panel, lambda r: r.ret_7d, BLOCKS_PER_DAY, lag=lag)
    ic300 = ic_by_lag.get(300, [])
    ic0 = ic_by_lag.get(0, [])
    if len(ic300) >= 10:
        m300 = _mean(ic300) or 0.0
        ci = stationary_bootstrap_ci(ic300, mean_block=7, n_boot=1_000, label="F1")
        kill = m300 < 0.01 or (ci is not None and ci.lo <= 0 <= ci.hi)
        ratio = m300 / (_mean(ic0) or float("nan")) if ic0 and (_mean(ic0) or 0) != 0 else float("nan")
        ok = m300 >= float(pre["F1"]["ic_300_min"]) and ratio >= float(pre["F1"]["ic_ratio_300_over_0_min"])
        out.append(TestResult("S4", "F1", KILL if kill else (PASS if ok else FAIL),
                              f"IC300={m300:.4f} IC0={_fmt(_mean(ic0))} ratio={_fmt(ratio, 2)} n={len(ic300)}",
                              f"IC300 >= {pre['F1']['ic_300_min']}, ratio >= {pre['F1']['ic_ratio_300_over_0_min']}"))
    else:
        out.append(TestResult("S4", "F1", NOT_EVALUABLE, f"n={len(ic300)}", ">= 10 daily IC dates", "window too short"))
    mom = _book(res, "momentum")
    if mom is not None and len(mom.daily_returns) >= 10:
        ci = stationary_bootstrap_ci(mom.daily_returns, mean_block=7, n_boot=1_000, label="F2")
        point = mom.mean_daily_net_pct / 100
        lb = ci.lo if ci is not None else float("-inf")
        verdict = KILL if point <= 0 else (PASS if point >= float(pre["F2"]["point_min"]) / 100
                                           and lb >= float(pre["F2"]["lower_bound_min"]) / 100 else FAIL)
        out.append(TestResult("S4", "F2", verdict, f"net={100 * point:.4f}%/day lb={100 * lb:.4f}",
                              f"point >= {pre['F2']['point_min']}%/day, lb >= {pre['F2']['lower_bound_min']}"))
    else:
        out.append(TestResult("S4", "F2", NOT_EVALUABLE, "momentum book days < 10", ">= 10 days"))
    # F3: day 2-3 continuation of the top tercile (excess over EW)
    _, _, lo1 = _daily_cross_section(panel, lambda r: r.ret_7d, BLOCKS_PER_DAY, lag=BLOCKS_PER_DAY)
    _, _, lo2 = _daily_cross_section(panel, lambda r: r.ret_7d, BLOCKS_PER_DAY, lag=2 * BLOCKS_PER_DAY)
    d23 = [a + b for a, b in zip(lo1, lo2, strict=False)]
    if len(d23) >= 10:
        v = 100 * (_mean(d23) or 0) / 2
        out.append(TestResult("S4", "F3", PASS if v >= float(pre["F3"]["day2_3_cum_excess_min_pct_day"]) else FAIL,
                              f"{v:.4f}%/day", f">= {pre['F3']['day2_3_cum_excess_min_pct_day']}%/day"))
    else:
        out.append(TestResult("S4", "F3", NOT_EVALUABLE, f"n={len(d23)}", ">= 10 dates"))
    # F9: sub-period signs (3 equal sub-periods of the momentum book) and the largest subnet's gross share
    if mom is not None and len(mom.daily_returns) >= 9:
        dr = list(mom.daily_returns)
        k = len(dr) // 3
        subs = [sum(dr[i * k:(i + 1) * k]) for i in range(3)]
        neg = sum(1 for s in subs if s < 0)
        gross = {n: abs(v) for n, v in mom.per_netuid_tao.items()}
        share = max(gross.values()) / sum(gross.values()) if gross and sum(gross.values()) > 0 else 0.0
        ok = neg <= 1 and share <= float(pre["F9"]["subnet_gross_share_max"])
        out.append(TestResult("S4", "F9", PASS if ok else FAIL, f"negative sub-periods={neg}/3 max subnet share={share:.2f}",
                              f"<= 1 of 3 negative, share <= {pre['F9']['subnet_gross_share_max']}"))
    else:
        out.append(TestResult("S4", "F9", NOT_EVALUABLE, "too few days", ">= 9 days"))
    rnd = _book(res, "base-random")
    if mom is not None and rnd is not None and mom.days >= 10:
        d = mom.mean_daily_net_pct - rnd.mean_daily_net_pct
        out.append(TestResult("S4", "F10", PASS if d >= float(pre["F10"]["beat_random_entry_min_pct_day"]) else FAIL,
                              f"momentum - random = {d:.4f}%/day", f">= {pre['F10']['beat_random_entry_min_pct_day']}%/day",
                              "random-entry placebo book of the same pass (single seed; the permutation test needs the grid)"))
    else:
        out.append(TestResult("S4", "F10", NOT_EVALUABLE, "books missing", "momentum and base-random books"))
    out.append(TestResult("S4", "F11", NOT_EVALUABLE, "-", f"flow match within {pre['F11']['flow_match_tolerance']}",
                          "needs >= 30 subnet-days of observed own flow (paper)"))
    out.append(_f12(capacity, pre["F12"]))
    # F14: impulses (|1-stride log move| >= 3%) - continuation from +60 blocks to +1 d vs the one-way cost
    imp: list[float] = []
    for r in panel.rows:
        prev = panel.at.get((r.block - 60, r.key))
        if prev is None or prev.spot <= 0 or r.spot <= 0 or not r.eligible:
            continue
        mv = math.log(r.spot / prev.spot)
        if abs(mv) < 0.03:
            continue
        f = panel.fwd(r, BLOCKS_PER_DAY - 60, total=False, lag=60)
        if f is None:
            continue
        cost = (r.rt_cost_ppm or 0) / 2e6
        imp.append(math.copysign(1.0, mv) * f - cost)
    need = int(pre["F14"]["impulses"])
    if len(imp) >= need:
        m = _mean(imp) or 0.0
        out.append(TestResult("S4", "F14", PASS if m > 0 else FAIL, f"mean net continuation={100 * m:.3f}% n={len(imp)}",
                              "t+60 to +1 d exceeds one-way cost"))
    else:
        out.append(TestResult("S4", "F14", NOT_EVALUABLE, f"impulses={len(imp)}", f">= {need}", "power floor"))
    # F15: decomposition identity residual (metrics) on the momentum book
    if mom is not None:
        out.append(TestResult("S4", "F15", PASS if mom.max_identity_error_rao <= 1 else FAIL,
                              f"max identity error {mom.max_identity_error_rao:.3g} rao", "<= 1 rao per tick"))
    return StudyResult("S4", "Momentum tests F1-F15", window, cov, tuple(_gate_coverage(out, cov)))


def _f12(capacity: Mapping[int, BookMetrics] | None, thr: Mapping[str, Any]) -> TestResult:
    need, net_min = float(thr["capacity_min_tao"]), float(thr["capacity_net_min_pct_day"])
    if not capacity or len(capacity) < 2:
        return TestResult("S4", "F12", NOT_EVALUABLE, "-", f"capacity >= {need} TAO",
                          "run the capacity sweep (backtest.runner.capacity_books)")
    pts = [(float(c), m.mean_daily_net_pct) for c, m in sorted(capacity.items())]
    half = capacity_at_half_edge(pts)
    at_need = [e for c, e in pts if c >= need]
    ok = (half is None or half >= need) and bool(at_need) and at_need[0] >= net_min
    return TestResult("S4", "F12", PASS if ok else FAIL,
                      f"edge halves at {_fmt(half, 1)} TAO; net at >= {need:.0f} TAO = {_fmt(at_need[0] if at_need else None)}",
                      f"capacity >= {need} TAO with net >= {net_min}%/day")


# ------------------------------------------------------------------------------------------------ S5 E-MR
def s5(panel_rows: Sequence[PanelRow], *, window: tuple[int, int], panel_from: int = PANEL_FROM_BLOCK) -> StudyResult:
    cov = coverage_label(window[0], panel_from)
    pre = _prereg()["mean_reversion_study"]
    panel = _Panel(panel_rows)
    shocks: list[tuple[PanelRow, float]] = []
    quiet: list[tuple[PanelRow, float]] = []
    for r in panel.rows:
        p = panel.at.get((r.block - 60, r.key))
        if p is None or p.spot <= 0 or r.spot <= 0:
            continue
        mv = math.log(r.spot / p.spot)
        if mv > float(pre["shock_log_move"]):
            continue
        shocks.append((r, mv))
        drops = 0
        for k in range(2, int(pre["quiet_prior_window_blocks"]) // 60 + 1):
            a, z = panel.at.get((r.block - 60 * k, r.key)), panel.at.get((r.block - 60 * (k - 1), r.key))
            if a is not None and z is not None and a.spot > 0 and math.log(z.spot / a.spot) <= -float(pre["quiet_prior_drop_frac"]):
                drops += 1
        r1 = r.ret_1d if r.ret_1d is not None else 0.0
        r7 = r.ret_7d if r.ret_7d is not None else 0.0
        if drops <= int(pre["quiet_prior_drops_max"]) and r1 >= float(pre["quiet_24h_min"]) and r7 >= float(pre["quiet_7d_min"]):
            quiet.append((r, mv))
    table: list[tuple[Any, ...]] = []
    for h in [int(x) for x in pre["F1"]["horizons_h"]]:
        rs = [panel.fwd(r, h * HOUR_BLOCKS, total=False) for r, _ in quiet]
        vals = [x for x in rs if x is not None]
        rho = [x / -mv for (r, mv), x in zip(quiet, rs, strict=True) if x is not None and mv < 0]
        table.append((f"{h}h", len(vals), _fmt(_pct(_mean(vals))), _fmt(_mean(rho), 3)))
    n_floor, n_after = int(pre["power_floor_shocks"]), int(pre["power_floor_after_filters"])
    if len(shocks) < n_floor or len(quiet) < n_after:
        verdict, detail = NOT_EVALUABLE, f"power floor: shocks {len(shocks)} < {n_floor} or filtered {len(quiet)} < {n_after}"
    else:
        rhos = [float(row[3]) for row in table if row[3] != "n/a"]
        hits = sum(1 for v in rhos if v >= float(pre["F1"]["rho_star_at_8pct"]) + float(pre["F1"]["rho_margin"]))
        verdict = PASS if hits >= int(pre["F1"]["min_horizons"]) else FAIL
        detail = "PASS here only re-opens the proposal by ADR (FILTER-ONLY verdict stands)"
    tests = [TestResult("S5", "E-MR", verdict, f"shocks={len(shocks)} filtered={len(quiet)}",
                        f"rho_lcb >= rho* + {pre['F1']['rho_margin']} on >= {pre['F1']['min_horizons']} horizons", detail)]
    return StudyResult("S5", "Mean-reversion event study E-MR", window, cov, tuple(tests),
                       {"horizons": (("horizon", "events", "mean fwd %", "mean rho"), tuple(table))})


# ------------------------------------------------------------------------------------------------ S6 launches
def s6(rec: PanelRecorder, res: PassResult, *, window: tuple[int, int]) -> StudyResult:
    pre = _prereg()["lcw"]["tests"]
    lo_lag, hi_lag = (int(x) for x in pre["FT0"]["lag_blocks"])
    rows: list[tuple[Any, ...]] = []
    lag_ok: list[bool] = []
    for lr in rec.launches:
        lag = lr.lag_blocks
        ok = lag is not None and lo_lag <= lag <= hi_lag and lr.netuid_ok is not False and not lr.seed_anomaly
        if lag is not None:
            lag_ok.append(ok)
        rows.append((int(lr.key.netuid), int(lr.key.reg_at), lag, lr.netuid_ok, lr.seed_anomaly))
    out = [TestResult("S6", "FT0", (PASS if all(lag_ok) else FAIL) if lag_ok else NOT_EVALUABLE,
                      f"{sum(lag_ok)}/{len(lag_ok)} launches with lag in [{lo_lag}, {hi_lag}], netuid = victim, seed ok",
                      "all", "" if lag_ok else "no launch with an exact queued block in the window")]
    n_min = int(pre["FT4"]["min_trades"])
    for t in ("FT1", "FT2", "FT3", "FT4", "FT6"):
        out.append(TestResult("S6", t, NOT_EVALUABLE, f"launches={len(rec.launches)}", f">= {n_min} launch trades",
                              "LCW is paper-only and disabled ([lcw].enabled = false); the window holds too few launches"))
    dereg = []
    for o in res.books.values():
        for p in o.points:
            if p.comps.dereg != 0:
                dereg.append((o.book, p.block, float(p.comps.dereg) / RAO_PER_TAO))
    out.append(TestResult("S6", "FT10", NOT_EVALUABLE, f"modelled settlements={len(dereg)}", "|R_pred - R_obs| <= 0.05",
                          "observed payouts need the refined dissolution windows (generation.observed_payout_ratio)"))
    return StudyResult("S6", "Launch study FT0-FT10", window, TOTAL_RETURN, tuple(out),
                       {"launches": (("netuid", "reg_at", "lag", "netuid_ok", "seed_anomaly"), tuple(rows)),
                        "dereg_settlements": (("book", "block", "payout - value at last spot (TAO)"), tuple(dereg))})


# ------------------------------------------------------------------------------------------------ S7 overlay
def s7(rec: PanelRecorder, res: PassResult, *, window: tuple[int, int]) -> StudyResult:
    pre = _prereg()["falsification"]
    panel = _Panel(rec.rows)
    out: list[TestResult] = []
    # FT1a: every prune in the window - was the victim at prune rank 1 within U + M_A before removal?
    prunes = rec.dereg_blocks
    hits = 0
    for b, k in prunes:
        pb = [x for x in panel.blocks if x < b]
        last = pb[-1] if pb else None
        r = panel.at.get((last, k)) if last is not None else None
        if r is not None and r.prune_rank is not None and r.prune_rank <= 3:
            hits += 1
    if prunes:
        lo, hi = wilson_ci(hits, len(prunes))
        out.append(TestResult("S7", "FT1a", NOT_EVALUABLE if len(prunes) < 26 else (PASS if hits >= 25 else FAIL),
                              f"{hits}/{len(prunes)} victims in the bottom 3 at the last stride (95% CI {lo:.2f}-{hi:.2f})",
                              str(pre["FT1a"]["pass_min"]), "out-of-sample prunes 27-52 need the full lake"
                              if len(prunes) < 26 else ""))
    else:
        out.append(TestResult("S7", "FT1a", NOT_EVALUABLE, "0 prunes", str(pre["FT1a"]["pass_min"]), "no prune in the window"))
    out.append(TestResult("S7", "FT1b", NOT_EVALUABLE, "-", f"EMA rebuild err < {pre['FT1b']['ema_rebuild_err_max']}",
                          "needs the per-block refinement windows (refine windows)"))
    out.append(TestResult("S7", "FT1c", NOT_EVALUABLE, "-", f">= {pre['FT1c']['avoided_over_false_alarm_min']}",
                          "needs FT1a recall over the out-of-sample prunes"))
    out.append(TestResult("S7", "FT2", NOT_EVALUABLE, "-", f"Brier skill >= {pre['FT2']['brier_skill_min']}",
                          "walk-forward hazard fit needs the full registration history"))
    # FT3: emission-off events - exit at event + latency vs hold, 7 d (price, panel)
    gains: list[float] = []
    for b, k in rec.emission_off:
        r = panel.at.get((b, k))
        if r is None:
            continue
        f = panel.fwd(r, 7 * BLOCKS_PER_DAY, total=True, lag=60)
        if f is not None:
            gains.append(-f - (r.rt_cost_ppm or 0) / 2e6)
    if len(gains) >= 10:
        med = sorted(gains)[len(gains) // 2]
        frac = sum(1 for g in gains if g > 0) / len(gains)
        ok = med >= float(pre["FT3"]["median_gain_min"]) and frac >= float(pre["FT3"]["frac_events_min"])
        out.append(TestResult("S7", "FT3", PASS if ok else FAIL, f"median={med:.4f} frac={frac:.2f} n={len(gains)}",
                              f"median >= {pre['FT3']['median_gain_min']}, frac >= {pre['FT3']['frac_events_min']}"))
    else:
        out.append(TestResult("S7", "FT3", NOT_EVALUABLE, f"events={len(gains)}", ">= 10 emission-off events"))
    out.append(TestResult("S7", "FT4", NOT_EVALUABLE, "-", f"weekly IC >= {pre['FT4']['weekly_ic_min']}",
                          "needs >= 26 weekly dates"))
    out.append(TestResult("S7", "FT5", NOT_EVALUABLE, "-", "overlay on/off per family", "needs an overlay-off book pair"))
    out.append(TestResult("S7", "FT6", NOT_EVALUABLE, "-", "exits within S_EXIT_HOLD_MAX", "needs replayed next-block flows"))
    # FT9: router - the chosen candidate's FORWARD index growth >= the median eligible candidate's, per subnet-day
    # (the design's subnet-months need a longer lake; one observation per subnet and UTC day here)
    seen: dict[tuple[int, int], bool] = {}
    for b, n, _exd, ok in router_forward_excess(panel):
        seen.setdefault((b // BLOCKS_PER_DAY, n), ok)
    total, good = len(seen), sum(1 for v in seen.values() if v)
    if total >= 30:
        frac = good / total
        out.append(TestResult("S7", "FT9", PASS if frac >= float(pre["FT9"]["chosen_ge_median_frac"]) else FAIL,
                              f"{frac:.2f} of {total} subnet-days", f">= {pre['FT9']['chosen_ge_median_frac']}"))
    else:
        out.append(TestResult("S7", "FT9", NOT_EVALUABLE, f"n={total}", ">= 30 subnet-days"))
    out.append(TestResult("S7", "FT10", NOT_EVALUABLE, "-", f"|R_pred - R_obs| <= {pre['FT10']['abs_err_max']}",
                          "observed payouts need the refined dissolution windows"))
    return StudyResult("S7", "Overlay falsification FT1-FT10", window, TOTAL_RETURN, tuple(out),
                       {"prunes_in_window": (("block", "netuid", "reg_at"),
                                             tuple((b, int(k.netuid), int(k.reg_at)) for b, k in prunes))})


def run_all(res: PassResult, rec: PanelRecorder, *, window: tuple[int, int], panel_from: int = PANEL_FROM_BLOCK,
            sensitivity: Mapping[str, BookMetrics] | None = None,
            capacity: Mapping[int, BookMetrics] | None = None) -> list[StudyResult]:
    return [s0(res, rec.rows, window=window, panel_from=panel_from), s1(rec.rows, window=window, panel_from=panel_from),
            s2(rec, window=window), s3(res, rec.rows, window=window, sensitivity=sensitivity, panel_from=panel_from),
            s4(res, rec.rows, window=window, panel_from=panel_from, capacity=capacity),
            s5(rec.rows, window=window, panel_from=panel_from),
            s6(rec, res, window=window), s7(rec, res, window=window)]


# ------------------------------------------------------------------------------------------------ CLI
def lake_panel_from(lake: Lake) -> int:
    """The collector's pinned panel_from (collector_meta), else the default post-June panel block."""
    import sqlite3
    try:
        con = sqlite3.connect(str(lake.state_db))
        try:
            row = con.execute("SELECT value FROM collector_meta WHERE key = 'panel_from'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return PANEL_FROM_BLOCK
    return int(row[0]) if row is not None else PANEL_FROM_BLOCK


def main(argv: Sequence[str] | None = None) -> int:
    from ..reports.html import write_report
    from .books import load_backtest_plan
    from .runner import TrialRegistry, arun_pass, register_pass

    ap = argparse.ArgumentParser(prog="python -m taotrader.backtest.studies")
    ap.add_argument("study", choices=["s0", "all"])
    ap.add_argument("--lake", default=None, help="lake root (default: [backtest].lake)")
    ap.add_argument("--config", action="append", default=None, help="config files (default: default + books.backtest)")
    ap.add_argument("--set", action="append", default=[], help="override path=value")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--warmup-blocks", type=int, default=None)
    ap.add_argument("--books", default=None, help="comma-separated subset of books")
    ap.add_argument("--out", default="reports/s0")
    ap.add_argument("--trials", default=None, help="trial registry SQLite (default: <out>/trials.sqlite)")
    a = ap.parse_args(argv)
    from ..ops.config_load import DEFAULT_CONFIG
    from .books import BOOKS_BACKTEST
    paths = a.config or [str(DEFAULT_CONFIG), str(BOOKS_BACKTEST)]
    plan = load_backtest_plan(paths, cli=a.set)
    lake = Lake(a.lake or plan.lake)
    rec = PanelRecorder()
    books = a.books.split(",") if a.books else None
    try:
        res = asyncio.run(arun_pass(plan, lake, books=books, start=a.start, end=a.end, warmup_blocks=a.warmup_blocks,
                                    on_tick=rec))
        pf = lake_panel_from(lake)
    finally:
        lake.close()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    reg = TrialRegistry(a.trials or out / "trials.sqlite")
    try:
        n_trials = register_pass(reg, res, f"study:{a.study}")
    finally:
        reg.close()
    window = (a.start or plan.start_block, int(res.last_block or 0))
    studies = [s0(res, rec.rows, window=window, panel_from=pf)] if a.study == "s0" else run_all(res, rec, window=window,
                                                                                                panel_from=pf)
    path = write_report(out, res, studies, plan=plan, trial_count=n_trials, universe=rec.universe_counts)
    print(f"report: {path}")
    for st in studies:
        for t in st.tests:
            print(f"{t.study} {t.test}: {t.verdict} ({t.value})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
