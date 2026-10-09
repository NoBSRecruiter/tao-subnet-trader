"""WP10 reports: static HTML (inline SVG, no JS) + CSV with the mandatory contents (section 11 WP10)."""
from __future__ import annotations

import csv
from pathlib import Path

from taotrader.backtest import studies as sd
from taotrader.backtest.books import BacktestPlan
from taotrader.backtest.runner import PassResult
from taotrader.reports import html as rh
from taotrader.reports import tables as tb


def test_report_contents_and_order(short_pass: tuple[PassResult, sd.PanelRecorder], short_plan: BacktestPlan,
                                   tmp_path: Path) -> None:
    res, rec = short_pass
    window = (8_765_400, int(res.last_block or 0))
    studies = [sd.s0(res, rec.rows, window=window), sd.s2(rec, window=window)]
    page = rh.write_report(tmp_path, res, studies, plan=short_plan, trial_count=17, universe=rec.universe_counts)
    text = page.read_text(encoding="utf-8")
    assert "<script" not in text.lower() and "http://" not in text and "https://" not in text
    assert rh.NO_ADVICE in text
    assert "Trial count: 17" in text
    assert "probability of backtest overfitting" in text and "deflated SR" in text
    # the attribution chart is the FIRST chart of the page and carries price / yield / fees / dereg
    first_svg = text.index("<svg")
    assert text.index("Attribution: price / yield / fees / dereg") < first_svg < text.index("NAV_liq by book")
    head = text[first_svg:text.index("</svg>", first_svg)]
    for word in ("price", "yield", "fees", "dereg"):
        assert word in head
    for section in ("Impact bounds", "Regime table", "Universe counts", "Fills and limit failures by exact_block",
                    "S0: EW total-return benchmark"):
        assert section in text
    for name in ("books.csv", "attribution.csv", "regimes.csv", "universe.csv", "failures.csv", "nav_daily.csv",
                 "studies.csv", "impact.csv", "s0_benchmark_books.csv", "s2_daily.csv"):
        assert (tmp_path / name).is_file(), name
    with (tmp_path / "studies.csv").open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["study", "test", "verdict", "value", "threshold", "detail"]
    assert any(r[1] == "T0" and r[2] == "REPORTED" for r in rows[1:])


def test_price_only_label_is_printed(short_pass: tuple[PassResult, sd.PanelRecorder]) -> None:
    res, rec = short_pass
    s = sd.s0(res, rec.rows, window=(8_000_000, 8_100_000))
    assert s.coverage == sd.PRICE_ONLY
    text = rh.render_html(res, [s])
    assert sd.PRICE_ONLY in text


def test_tables_shapes(short_pass: tuple[PassResult, sd.PanelRecorder], short_plan: BacktestPlan) -> None:
    res, _ = short_pass
    hdr, rows = tb.book_table(res, dict(short_plan.variants))
    assert len(rows) == len(res.books) and len(hdr) == len(rows[0])
    hdr, rows = tb.attribution_table(res)
    assert hdr[1:5] == ("price", "yield", "fees", "dereg")
    hdr, rows = tb.failure_table(res)
    assert "limit fail rate exact" in hdr and "limit fail rate stride" in hdr
    hdr, rows = tb.impact_table(res, dict(short_plan.variants))
    assert rows and hdr[1].startswith("temporary")
    assert tb.regime_table(res)[1]


def test_svg_helpers_handle_empty_and_negative_values() -> None:
    assert "no data" in rh.svg_bars([], [], [])
    assert "no data" in rh.svg_lines({})
    svg = rh.svg_bars(["a", "b"], ["x", "y"], [[1.0, -2.0], [0.0, 0.5]])
    assert svg.startswith("<svg") and svg.count("<rect") >= 4
    line = rh.svg_lines({"s": [(1.0, 2.0), (2.0, 2.0)]})
    assert "<path" in line


def test_pbo_of_pass_shapes(short_pass: tuple[PassResult, sd.PanelRecorder]) -> None:
    res, _ = short_pass
    pbo, days, books = tb.pbo_of_pass(res)
    assert books == len(res.books) and days >= 1
    assert pbo is None or 0.0 <= pbo <= 1.0
