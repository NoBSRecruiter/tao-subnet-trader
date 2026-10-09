"""WP10 backtest.studies: S0-S7 on the short real pass, panel mechanics on synthetic rows, coverage labelling."""
from __future__ import annotations

import math
from dataclasses import replace

from taotrader.backtest import studies as sd
from taotrader.backtest.runner import PassResult
from taotrader.core.units import Block, NetUid, SubnetKey

VERDICTS = {sd.PASS, sd.FAIL, sd.KILL, sd.REPORTED, sd.NOT_EVALUABLE}
DAY = 7_200


def _row(block: int, netuid: int, spot: float, index: float = 1.0, **kw: object) -> sd.PanelRow:
    base = sd.PanelRow(block=block, ts_ms=block * 12_000, key=SubnetKey(NetUid(netuid), Block(1)), spot=spot, pool_tao=1_000.0,
                       hotkey="h", index=index, ret_1d=None, ret_7d=None, flow_1d=None, flow_z_1d=None, yield_net_day=None,
                       yield_cf_day=0.0, sell_push_day=0.0, cb_push_day=0.0, eligible=True, enabled=True, started=True,
                       prune_rank=None, immune=False, obs_emis_day=0.0, emis_day=0.0, model_ok=True, rt_cost_ppm=1_000)
    return replace(base, **kw)  # type: ignore[arg-type]


def test_coverage_label_and_gating() -> None:
    assert sd.coverage_label(8_466_531) == sd.TOTAL_RETURN
    assert sd.coverage_label(8_000_000) == sd.PRICE_ONLY
    assert sd.coverage_label(8_000_000, panel_from=4_920_351) == sd.TOTAL_RETURN
    t = [sd.TestResult("S3", "T1", sd.PASS, "v", "thr"), sd.TestResult("S3", "T0", sd.REPORTED, "v", "thr")]
    gated = sd._gate_coverage(t, sd.PRICE_ONLY)
    assert [g.verdict for g in gated] == [sd.NOT_EVALUABLE, sd.REPORTED]
    assert "ineligible (PASS)" in gated[0].detail
    assert sd._gate_coverage(t, sd.TOTAL_RETURN) == t


def test_panel_forward_returns_are_generation_and_hotkey_exact() -> None:
    rows = [_row(0, 1, 1.0, 1.0), _row(DAY, 1, 2.0, 1.1), _row(0, 2, 1.0, 1.0), _row(DAY, 2, 1.0, 1.2, hotkey="other")]
    p = sd._Panel(rows)
    assert p.fwd(rows[0], DAY) == math.log(2.0) + math.log(1.1)
    assert p.fwd(rows[0], DAY, total=False) == math.log(2.0)
    assert p.fwd(rows[2], DAY) is None                           # the index hotkey changed: no P x I return
    assert p.fwd(rows[2], DAY, total=False) == 0.0


def test_s1_ranks_a_planted_momentum_factor() -> None:
    rows: list[sd.PanelRow] = []
    for d in range(12):
        for n in range(1, 10):
            past = 0.01 * n
            spot = math.exp(0.002 * n * d)
            rows.append(_row(d * DAY, n, spot, ret_7d=past, ret_1d=0.0, pool_tao=100.0 * n))
    res = sd.s1(rows, window=(8_765_684, 8_830_000))
    _, table = res.tables["factors"]
    wml7 = next(r for r in table if r[0] == "WML7" and r[1] == "1d" and r[2] == 0)
    assert float(wml7[4]) > 0.9                                  # mean IC: the planted ordering
    assert res.coverage == sd.TOTAL_RETURN and res.tests[0].verdict == sd.REPORTED


def test_s5_power_floor_and_shock_detection() -> None:
    rows = [_row(0, 1, 1.0), _row(60, 1, 0.9), _row(120, 1, 0.92), _row(60 + 3_600, 1, 0.95)]
    res = sd.s5(rows, window=(0, 4_000))
    (t,) = res.tests
    assert t.verdict == sd.NOT_EVALUABLE and "shocks=1" in t.value and "power floor" in t.detail


def test_short_pass_runs_every_study(short_pass: tuple[PassResult, sd.PanelRecorder]) -> None:
    res, rec = short_pass
    assert rec.rows and rec.universe_counts
    window = (8_765_400, int(res.last_block or 0))
    out = sd.run_all(res, rec, window=window)
    assert [s.study for s in out] == ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    for s in out:
        assert s.tests, s.study
        for t in s.tests:
            assert t.verdict in VERDICTS, (s.study, t.test, t.verdict)
    s0 = out[0]
    assert s0.verdicts()["T0"] == sd.REPORTED
    _, rows = s0.tables["benchmark_books"]
    assert {r[0] for r in rows} >= {"base-ew-total", "base-ew-price", "base-cash", "carry"}
    assert "ew_index_gross" in s0.tables
    # the short window is far below every pre-registered power floor: nothing passes by accident
    s4 = out[4]
    assert s4.verdicts()["F1"] == sd.NOT_EVALUABLE


def test_router_forward_excess_is_out_of_sample() -> None:
    a = _row(0, 1, 1.0, candidates=(("best", 900.0, 1.0), ("mid", 500.0, 1.0), ("low", 100.0, 1.0)))
    z = _row(DAY, 1, 1.0, candidates=(("mid", 600.0, 1.02), ("best", 800.0, 1.01), ("low", 50.0, 1.0)))
    out = sd.router_forward_excess(sd._Panel([a, z]))
    assert len(out) == 1
    b, n, exd, ok = out[0]
    assert (b, n) == (0, 1)
    assert abs(exd - (math.log(1.01) - math.log(1.01))) < 1e-12 and ok        # median of {1.01, 1.02, 1.0} growth
    z2 = _row(DAY, 1, 1.0, candidates=(("best", 800.0, 1.0), ("mid", 600.0, 1.05), ("low", 50.0, 1.03)))
    (_, _, exd2, ok2), = sd.router_forward_excess(sd._Panel([a, z2]))
    assert exd2 < 0 and not ok2                                                 # the past winner lagged forward


def test_panel_recorder_diffs_against_its_own_previous_snapshot(short_pass: tuple[PassResult, sd.PanelRecorder]) -> None:
    _, rec = short_pass
    assert rec.blocks == sorted(rec.blocks) and len(set(rec.blocks)) == len(rec.blocks)
    for b, k in rec.dereg_blocks + rec.emission_off:
        assert b in rec.blocks and k is not None
