"""WP10 backtest.metrics: NAV_liq, the exact decomposition identity, daily resampling and book statistics."""
from __future__ import annotations

from decimal import Decimal

from taotrader.backtest import metrics as mt
from taotrader.backtest.runner import PassResult
from taotrader.backtest.studies import PanelRecorder
from taotrader.core.units import RAO_PER_TAO


def _pt(block: int, ts_ms: int, nav: int, *, flows: int = 0, per: tuple[tuple[int, Decimal], ...] = (),
        n_pos: int = 1) -> mt.TickPoint:
    return mt.TickPoint(block=block, ts_ms=ts_ms, nav=nav, nav_spot=Decimal(nav), nav_liq_engine=nav, cash=0, fee_float=0,
                        gross_liq=0, n_positions=n_pos, comps=mt.Components(), identity_error=Decimal(0), flows=flows,
                        traded_tao=0, per_netuid=per)


def test_identity_holds_within_one_rao_on_every_tick_of_real_data(short_pass: tuple[PassResult, PanelRecorder]) -> None:
    res, _ = short_pass
    assert res.ticks > 10
    for bk, o in res.books.items():
        assert o.points, bk
        for p in o.points:
            assert p.identity_error <= 1, (bk, p.block, p.identity_error)
        # summed over the run, the components (capital included) explain the whole NAV path from 0
        total = Decimal(0)
        for p in o.points:
            total += p.comps.total()
        assert abs(total - Decimal(o.points[-1].nav)) <= len(o.points), bk


def test_nav_starts_at_capital_plus_fee_float_and_cash_book_is_flat(short_pass: tuple[PassResult, PanelRecorder]) -> None:
    res, _ = short_pass
    cash = res.books["base-cash"]
    first = cash.points[0]
    assert first.flows == 110 * RAO_PER_TAO and first.nav == 110 * RAO_PER_TAO
    assert all(p.nav == first.nav and p.n_positions == 0 for p in cash.points)
    assert cash.metrics.trades.fills == 0 and cash.metrics.time_in_cash_pct == 100.0


def test_ew_total_trades_and_its_components_are_populated(short_pass: tuple[PassResult, PanelRecorder]) -> None:
    res, _ = short_pass
    m = res.books["base-ew-total"].metrics
    assert m.trades.buys > 0
    c = m.components_tao
    assert c["swap_fee"] < 0 and c["tx_fee"] < 0                 # costs are charged
    assert c["capital"] == 110.0
    assert m.max_identity_error_rao <= 1
    # stride replay: every fill and failure is evaluated on a later stride snapshot
    assert m.trades.fills_exact == 0 and m.trades.fills_stride == m.trades.fills
    assert m.per_netuid_tao                                    # per-subnet contributions exist
    lat = m.trades.fill_latency_blocks
    assert lat and min(lat) >= 5                               # N+2 after finality lag 3: never earlier than 5 blocks


def test_engine_nav_matches_the_recorder_definition(short_pass: tuple[PassResult, PanelRecorder]) -> None:
    res, _ = short_pass
    for o in res.books.values():
        for p in o.points:
            assert p.nav == p.nav_liq_engine + p.fee_float
            assert p.nav_spot >= p.nav - p.fee_float - p.cash - 1 or p.n_positions == 0


def test_daily_points_and_flow_adjusted_returns() -> None:
    day = mt.MS_PER_DAY
    pts = [_pt(1, 0, 100, flows=100), _pt(2, day // 2, 101), _pt(3, day + 1, 110, flows=10), _pt(4, 2 * day + 5, 99)]
    days = mt.daily_points(pts)
    assert [(d.day, d.nav, d.flows) for d in days] == [(0, 101, 100), (1, 110, 10), (2, 99, 0)]
    r = mt.daily_returns(days)
    assert r[0] == 0.01                                        # (101 - 0 - 100) / 100
    assert r[1] == (110 - 101 - 10) / 111
    assert r[2] == (99 - 110) / 110


def test_max_drawdown() -> None:
    assert mt.max_drawdown([1, 2, 3]) == 0
    assert mt.max_drawdown([100, 120, 90, 130, 65]) == 0.5


def test_leave_top_out_removes_the_largest_contributors() -> None:
    day = mt.MS_PER_DAY
    pts = [_pt(1, 0, 100, flows=100),
           _pt(2, day, 120, per=((1, Decimal(15)), (2, Decimal(5)))),
           _pt(3, 2 * day, 125, per=((2, Decimal(5)),))]
    top, rets = mt.leave_top_out(pts, 1)
    assert top == (1,)
    assert rets[1] == (105 - 100) / 100                        # day 1 without netuid 1's +15


def test_book_metrics_summary() -> None:
    day = mt.MS_PER_DAY
    pts = [_pt(1, 0, 100, flows=100), _pt(2, day, 102), _pt(3, 2 * day, 101, n_pos=0)]
    m = mt.book_metrics("b", pts, mt.TradeStats(hold_blocks=(7_200, 14_400)))
    assert m.days == 3 and m.ticks == 3
    assert m.avg_hold_days == 1.5
    assert round(m.time_in_cash_pct, 6) == round(100 / 3, 6)
    assert m.max_drawdown_pct > 0
    assert m.hit_rate_pct == 100 * 1 / 3


def test_capacity_at_half_edge() -> None:
    assert mt.capacity_at_half_edge([(1, 0.2), (3, 0.18), (10, 0.12), (30, 0.06)]) == 10 + (0.12 - 0.1) * 20 / 0.06
    assert mt.capacity_at_half_edge([(1, 0.2), (3, 0.19)]) is None          # never halves within the sweep
    assert mt.capacity_at_half_edge([(1, -0.1), (3, -0.2)]) is None         # no positive edge to halve
    assert mt.capacity_at_half_edge([]) is None
