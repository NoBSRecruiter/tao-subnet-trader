"""taotrader/reports/html.py - static HTML report (inline SVG, no JavaScript) plus CSV (WP10; DESIGN.md 8.9, 11 WP10).

`write_report(out_dir, res, studies, ...)` writes `index.html` and one CSV per table into out_dir. The page carries,
in this order: the no-advice statement; run identity (code / config / preregistration / data manifest hashes, seed)
and the TRIAL COUNT of the registry (deflated Sharpe ratios are computed against it, and the CSCV probability of
backtest overfitting over the pass's books is printed with the book table); the price / yield / fee / dereg
attribution as the FIRST chart; NAV_liq by book; both impact bounds side by side (TEMPORARY = headline and gating,
PERSISTENT = optimistic); the regime table; the universe counts; limit-failure rates split by exact_block; the study
verdicts and tables. Windows that start before
the hotkey panel (8,466,531) carry the "price-only + closed-form yield proxy" label (section 8.11).

Colours are CSS custom properties with a prefers-color-scheme dark override; the page has no script and no external
resource, so it renders identically offline.
"""
from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ..backtest.metrics import daily_points
from ..backtest.runner import PassResult
from ..core.units import RAO_PER_TAO
from .tables import (
    ATTRIBUTION,
    Table,
    attribution,
    attribution_table,
    book_table,
    failure_table,
    impact_table,
    nav_table,
    pbo_of_pass,
    regime_table,
    study_tests_table,
    universe_table,
    write_csv,
)

__all__ = ["NO_ADVICE", "render_html", "svg_bars", "svg_lines", "write_report"]

NO_ADVICE: Final[str] = ("Research output only. This report is not investment, financial or trading advice, and no "
                         "result here is a recommendation to buy, sell or hold any asset. Backtests are simulations "
                         "with stated assumptions; impact is bracketed and gated at the conservative bound.")
PALETTE: Final[tuple[str, ...]] = ("var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)",
                                   "var(--c7)", "var(--c8)")
CSS: Final[str] = """
:root { --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --grid:#d1d9e0; --card:#f6f8fa;
  --c1:#2f6feb; --c2:#1a7f37; --c3:#cf222e; --c4:#9a6700; --c5:#8250df; --c6:#0a7d8c; --c7:#bc4c00; --c8:#57606a; }
@media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --grid:#30363d; --card:#161b22;
  --c1:#4c8dff; --c2:#3fb950; --c3:#f85149; --c4:#d29922; --c5:#a371f7; --c6:#39c5cf; --c7:#f0883e; --c8:#8b949e; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
  margin:0 auto; max-width:1180px; padding:16px; }
h1 { font-size:22px; margin:8px 0 4px; } h2 { font-size:17px; margin:28px 0 8px; border-bottom:1px solid var(--grid); }
.note { background:var(--card); border-left:4px solid var(--c4); padding:8px 12px; margin:12px 0; }
.muted { color:var(--muted); } table { border-collapse:collapse; font-size:12.5px; margin:6px 0 14px; }
th, td { border:1px solid var(--grid); padding:3px 7px; text-align:right; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; } th { background:var(--card); }
.scroll { overflow-x:auto; max-width:100%; } svg text { fill:var(--fg); font-size:11px; }
svg .axis { stroke:var(--grid); } .PASS { color:var(--c2); font-weight:600; } .FAIL, .KILL { color:var(--c3); font-weight:600; }
.NOT_EVALUABLE { color:var(--muted); } .REPORTED { color:var(--c1); }
"""


def _e(x: Any) -> str:
    return html.escape(str(x))


def _table(t: Table, *, verdict_col: int | None = None) -> str:
    hdr, rows = t
    out = ["<div class='scroll'><table><thead><tr>", *(f"<th>{_e(h)}</th>" for h in hdr), "</tr></thead><tbody>"]
    for r in rows:
        cells = []
        for i, v in enumerate(r):
            cls = f" class='{_e(v)}'" if verdict_col is not None and i == verdict_col else ""
            cells.append(f"<td{cls}>{_e(v)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def svg_bars(groups: Sequence[str], series: Sequence[str], values: Sequence[Sequence[float]], *, width: int = 1100,
             height: int = 300, unit: str = "TAO") -> str:
    """Grouped bar chart (one group per book, one bar per series), zero baseline, labelled axis."""
    if not groups:
        return "<p class='muted'>no data</p>"
    vmax = max([abs(v) for row in values for v in row] + [1e-12])
    left, right, top, bottom = 60, 10, 24, 80
    pw, ph = width - left - right, height - top - bottom
    zero = top + ph / 2
    gw = pw / len(groups)
    bw = max(2.0, gw * 0.8 / max(len(series), 1))
    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='attribution by book'>",
             f"<line class='axis' x1='{left}' y1='{zero:.1f}' x2='{width - right}' y2='{zero:.1f}'/>",
             f"<text x='4' y='{top - 8}'>{_e(unit)} (+/- {vmax:.4g})</text>"]
    for gi, g in enumerate(groups):
        x0 = left + gi * gw + gw * 0.1
        for si, _ in enumerate(series):
            v = values[gi][si]
            h = abs(v) / vmax * (ph / 2)
            y = zero - h if v >= 0 else zero
            parts.append(f"<rect x='{x0 + si * bw:.1f}' y='{y:.1f}' width='{bw - 1:.1f}' height='{max(h, 0.5):.1f}' "
                         f"fill='{PALETTE[si % len(PALETTE)]}'><title>{_e(g)} {_e(series[si])}: {v:.6g}</title></rect>")
        parts.append(f"<text x='{left + gi * gw + gw / 2:.1f}' y='{height - bottom + 14}' text-anchor='end' "
                     f"transform='rotate(-35 {left + gi * gw + gw / 2:.1f} {height - bottom + 14})'>{_e(g)}</text>")
    for si, s in enumerate(series):
        parts.append(f"<rect x='{left + si * 110}' y='4' width='10' height='10' fill='{PALETTE[si % len(PALETTE)]}'/>"
                     f"<text x='{left + si * 110 + 14}' y='13'>{_e(s)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def svg_lines(series: Mapping[str, Sequence[tuple[float, float]]], *, width: int = 1100, height: int = 280,
              ylabel: str = "") -> str:
    """Line chart: series name -> [(x, y)], shared axes, min/max labels."""
    pts = [p for s in series.values() for p in s]
    if not pts:
        return "<p class='muted'>no data</p>"
    xmin, xmax = min(p[0] for p in pts), max(p[0] for p in pts)
    ymin, ymax = min(p[1] for p in pts), max(p[1] for p in pts)
    if xmax == xmin:
        xmax = xmin + 1
    if ymax == ymin:
        ymax = ymin + 1
    left, right, top, bottom = 70, 150, 16, 28
    pw, ph = width - left - right, height - top - bottom

    def sx(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * pw

    def sy(y: float) -> float:
        return top + (1 - (y - ymin) / (ymax - ymin)) * ph

    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='{_e(ylabel)}'>",
             f"<line class='axis' x1='{left}' y1='{top + ph}' x2='{left + pw}' y2='{top + ph}'/>",
             f"<line class='axis' x1='{left}' y1='{top}' x2='{left}' y2='{top + ph}'/>",
             f"<text x='4' y='{top + 8}'>{ymax:.5g}</text><text x='4' y='{top + ph}'>{ymin:.5g}</text>",
             f"<text x='{left}' y='{height - 6}'>{xmin:.0f}</text>",
             f"<text x='{left + pw}' y='{height - 6}' text-anchor='end'>{xmax:.0f}</text>"]
    for i, (name, s) in enumerate(sorted(series.items())):
        if not s:
            continue
        c = PALETTE[i % len(PALETTE)]
        d = " ".join(f"{'M' if j == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for j, (x, y) in enumerate(s))
        parts.append(f"<path d='{d}' fill='none' stroke='{c}' stroke-width='1.6'><title>{_e(name)}</title></path>")
        parts.append(f"<text x='{left + pw + 6}' y='{top + 12 + 13 * i}' fill='{c}'>{_e(name)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def render_html(res: PassResult, studies: Sequence[Any], *, plan: Any = None, trial_count: int = 0,
                universe: Sequence[tuple[int, int, int]] = (), title: str = "Backtest report") -> str:
    variants = dict(plan.variants) if plan is not None else {}
    books = sorted(res.books)
    attr_series = [a for a, _ in ATTRIBUTION]
    attr_vals = [[attribution(res.books[b].metrics)[a] for a in attr_series] for b in books]
    nav_series = {b: [(float(d.block), d.nav / RAO_PER_TAO) for d in daily_points(res.books[b].points)] for b in books}
    uni_series = {"eligible A-G": [(float(b), float(n)) for b, _, n in universe]}
    ident = res.identity
    window = f"{res.first_block} .. {res.last_block}" if res.first_block is not None else "empty"
    labels = sorted({s.coverage for s in studies}) if studies else []
    pbo, pbo_days, pbo_books = pbo_of_pass(res)
    pbo_text = "n/a (too few days or books)" if pbo is None else f"{pbo:.3f} ({pbo_books} books x {pbo_days} days)"
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body>",
        f"<h1>{_e(title)}</h1>",
        f"<div class='note'><strong>No advice.</strong> {_e(NO_ADVICE)}</div>",
        "<p>",
        f"Window <strong>{_e(window)}</strong> ({res.ticks} ticks). <strong>Trial count: {trial_count}</strong> "
        "(every evaluation is registered; read the statistics against it). ",
        f"Coverage: {_e(', '.join(labels) or 'n/a')}.</p>",
        "<p class='muted'>Run identity: code " + _e(ident.code_hash[:16]) + " · config " + _e(ident.config_hash[:16])
        + " · prereg " + _e(ident.prereg_hash[:16]) + " · data manifest " + _e(ident.manifest_hash[:16])
        + f" · seed {ident.seed} · run key {_e(ident.run_key())}</p>",
        "<h2>Attribution: price / yield / fees / dereg (TAO)</h2>",
        svg_bars(books, attr_series, attr_vals),
        _table(attribution_table(res)),
        "<h2>NAV_liq by book (TAO, incl. fee float; UTC daily)</h2>",
        svg_lines(nav_series, ylabel="NAV_liq TAO"),
        "<h2>Books</h2>",
        f"<p>CSCV probability of backtest overfitting over these books: <strong>{_e(pbo_text)}</strong>; deflated "
        f"Sharpe ratios are computed against the trial count ({trial_count}).</p>",
        _table(book_table(res, variants, seed=ident.seed, n_trials=trial_count)),
        "<h2>Impact bounds (TEMPORARY = headline and gating; PERSISTENT = optimistic)</h2>",
        _table(impact_table(res, variants)) if variants else "<p class='muted'>no variants</p>",
        "<h2>Regime table</h2>",
        _table(regime_table(res)),
        "<h2>Universe counts</h2>",
        svg_lines(uni_series, height=200, ylabel="eligible subnets"),
        _table(universe_table(universe)),
        "<h2>Fills and limit failures by exact_block</h2>",
        "<p class='muted'>Stride replays evaluate fills and failures on the next stride snapshot (exact_block = false); "
        "they are reported separately and never feed the failure counters (section 3.12).</p>",
        _table(failure_table(res)),
        "<h2>Studies</h2>",
        _table(study_tests_table(studies), verdict_col=2),
    ]
    for s in studies:
        parts.append(f"<h2>{_e(s.study)}: {_e(s.title)}</h2><p class='muted'>window {s.window[0]} .. {s.window[1]}; "
                     f"{_e(s.coverage)}</p>")
        for n in s.notes:
            parts.append(f"<p class='muted'>{_e(n)}</p>")
        for name, t in s.tables.items():
            parts.append(f"<h3>{_e(name)}</h3>{_table(t)}")
    if res.alerts:
        parts.append("<h2>Alerts</h2>" + _table((("kind", "message"), tuple(res.alerts[:200]))))
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(out_dir: str | Path, res: PassResult, studies: Sequence[Any], *, plan: Any = None, trial_count: int = 0,
                 universe: Sequence[tuple[int, int, int]] = (), title: str = "Backtest report") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    variants = dict(plan.variants) if plan is not None else {}
    csvs: dict[str, Table] = {
        "books.csv": book_table(res, variants, seed=res.identity.seed, n_trials=trial_count),
        "attribution.csv": attribution_table(res),
        "regimes.csv": regime_table(res), "universe.csv": universe_table(universe), "failures.csv": failure_table(res),
        "nav_daily.csv": nav_table(res), "studies.csv": study_tests_table(studies)}
    if variants:
        csvs["impact.csv"] = impact_table(res, variants)
    for s in studies:
        for name, t in s.tables.items():
            csvs[f"{s.study.lower()}_{name}.csv"] = t
    for name, (hdr, rows) in csvs.items():
        write_csv(out / name, hdr, rows)
    page = out / "index.html"
    page.write_text(render_html(res, studies, plan=plan, trial_count=trial_count, universe=universe, title=title),
                    encoding="utf-8")
    return page
