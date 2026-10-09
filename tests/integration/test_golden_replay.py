"""Section 10.3 item 1: golden replay of a known period, plus the S0 report end-to-end (section 11 WP10 acceptance).

Blocks 8,765,400 -> 8,830,000 (gate rank-32, the committed mini-lake) with every baseline book and the carry MVP:
- expected run digest per book (tests/integration/golden_minilake_digests.json; updated only with an ADR - regenerate
  with TAOTRADER_REGEN_GOLDEN=1 and record the reason in the ADR);
- the decomposition identity holds on every tick (<= 1 rao, preregistration [metrics].decomposition_identity_tolerance_rao);
- the EW-price baseline is negative over the window (the brief's sign);
- no orphan, no invariant breach;
- S0 (and S1-S7) run end-to-end on the replay and the static HTML + CSV report is written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from taotrader.backtest import studies as sd
from taotrader.backtest.runner import PassResult, TrialRegistry, register_pass
from taotrader.ops.config_load import load_preregistration
from taotrader.reports.html import NO_ADVICE, write_report

Golden = tuple[PassResult, sd.PanelRecorder, Path]


def test_golden_replay_covers_the_window(golden: Golden, itx: Any) -> None:
    res, rec, _ = golden
    assert res.first_block == itx.GOLDEN_START
    assert res.last_block is not None and res.last_block >= itx.GOLDEN_END - 60
    assert res.ticks >= 1_000                                    # ~1,073 snapshots (60-block stride + membership points)
    assert set(res.books) == set(itx.GOLDEN_BOOKS)
    assert len(rec.blocks) == res.ticks


def test_expected_run_digest_per_book(golden: Golden, itx: Any) -> None:
    res, _, _ = golden
    got = {"window": [itx.GOLDEN_START, itx.GOLDEN_END], "feature_warm_blocks": itx.GOLDEN_WARM,
           "manifest_hash": res.identity.manifest_hash, "books": res.digests(),
           "money": {k: v.money_digest for k, v in sorted(res.books.items())}}
    path: Path = itx.GOLDEN_DIGESTS
    if os.environ.get("TAOTRADER_REGEN_GOLDEN") == "1" or not path.exists():
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if os.environ.get("TAOTRADER_REGEN_GOLDEN") != "1":
            pytest.fail(f"wrote the initial golden digests to {path}; commit them (later changes need an ADR)")
    want = json.loads(path.read_text(encoding="utf-8"))
    assert want["manifest_hash"] == got["manifest_hash"], "the mini-lake changed: regenerate the digests with an ADR"
    assert got["books"] == want["books"]
    assert got["money"] == want["money"]


def test_decomposition_identity_on_every_tick(golden: Golden) -> None:
    res, _, _ = golden
    tol = int(load_preregistration()["metrics"]["decomposition_identity_tolerance_rao"])
    for bk, o in res.books.items():
        worst = max(p.identity_error for p in o.points)
        assert worst <= tol, (bk, worst)


def test_ew_price_baseline_is_negative_over_the_window(golden: Golden) -> None:
    res, _, _ = golden
    m = res.books["base-ew-price"].metrics
    assert m.trades.buys > 0
    assert m.components_tao["price"] < 0, m.components_tao       # price-only P&L of the EW universe


def test_no_orphans_breaches_or_alerts(golden: Golden) -> None:
    res, _, _ = golden
    for bk, o in res.books.items():
        assert o.orphans == 0 and not o.breaches, (bk, o.breaches)
    assert not [a for a in res.alerts if a[0] in ("orphan", "invariant")]


def test_s0_report_runs_end_to_end(golden: Golden, golden_plan: Any, tmp_path: Path) -> None:
    res, rec, _ = golden
    window = (int(res.first_block or 0), int(res.last_block or 0))
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    n = register_pass(reg, res, "study:s0")
    reg.close()
    studies = sd.run_all(res, rec, window=window)
    s0 = studies[0]
    assert s0.study == "S0" and s0.coverage == sd.TOTAL_RETURN
    assert s0.verdicts()["T0"] == sd.REPORTED
    bench = {r[0]: r for r in s0.tables["benchmark_books"][1]}
    assert {"base-ew-total", "base-ew-price", "carry"} <= set(bench)
    page = write_report(tmp_path / "s0", res, studies, plan=golden_plan, trial_count=n, universe=rec.universe_counts)
    text = page.read_text(encoding="utf-8")
    assert NO_ADVICE in text and f"Trial count: {n}" in text and "<script" not in text.lower()
    assert (tmp_path / "s0" / "s0_benchmark_books.csv").is_file()
    for s in studies:
        for t in s.tests:
            assert t.verdict in (sd.PASS, sd.FAIL, sd.KILL, sd.REPORTED, sd.NOT_EVALUABLE)
