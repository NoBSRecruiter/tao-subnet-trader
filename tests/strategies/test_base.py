"""WP9 shared plumbing (strategies/base.py): params parsing, sleeve holdings on the view, the History store helpers
(generation safety, no lookahead), the floor rows, p_reg fallbacks and the Signal builders."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from taotrader.core.config import RiskCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.orders import Urgency
from taotrader.core.signals import SignalKind
from taotrader.core.units import PPM, Block, NetUid, Ppm, StrategyId, SubnetKey
from taotrader.protocol.amm import liq_value
from taotrader.strategies.base import (
    History,
    ParamsError,
    days_to_blocks,
    entry_blocks,
    exit_signal,
    floor_rows,
    p_reg_horizon,
    parse_params,
    robust_sd,
    sleeve_budget_rao,
    sleeve_positions,
    tao_to_rao,
    target_signal,
    terciles,
)
from taotrader.strategies.carry import CarryStrategy


@dataclass(frozen=True, slots=True)
class _P:
    a: int = 1
    b: float = 0.5
    c: bool = False
    d: str = "x"

    def problems(self) -> list[str]:
        return ["a must be >= 0"] if self.a < 0 else []


def test_parse_params_is_strict() -> None:
    assert parse_params(_P, None) == _P()
    assert parse_params(_P, {"a": 3, "b": 2, "c": True, "d": "y"}) == _P(3, 2.0, True, "y")
    for bad, msg in (({"z": 1}, "unknown"), ({"a": 1.5}, "expected int"), ({"a": True}, "expected int"),
                     ({"b": "1"}, "finite number"), ({"b": math.inf}, "finite number"), ({"c": 0}, "expected bool"),
                     ({"d": 1}, "expected str"), ({"a": -1}, "a must be")):
        with pytest.raises(ParamsError, match=msg):
            parse_params(_P, bad)


def test_unit_helpers() -> None:
    assert tao_to_rao(1.5) == 1_500_000_000 and tao_to_rao(0.1) == 100_000_000
    assert days_to_blocks(2.5) == 18_000
    assert robust_sd([1.0, 2.0, 3.0, 4.0, 100.0]) == pytest.approx(1.4826)
    t = terciles({SubnetKey(NetUid(i), Block(0)): float(v) for i, v in enumerate([5, 1, 9, 3, 7, 2])})
    assert [t[SubnetKey(NetUid(i), Block(0))] for i in range(6)] == [1, 0, 2, 1, 2, 0]


def test_sleeve_positions_value_on_the_view(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92)])
    k = sx.Spec(92).key
    port = sx.holding("carry", k, alpha_tao=1_000)
    ctx = m.ctx(portfolio=port)
    pos = sleeve_positions(ctx, StrategyId("carry"))[k]
    s = ctx.view.get(k)
    assert s is not None
    idx = s.hotkey(sx.HK1)
    assert idx is not None
    assert pos.alpha == idx.value_of(Decimal(1_000 * sx.TAO))
    assert pos.value_rao == liq_value(s.pool, pos.alpha)
    assert sleeve_positions(ctx, StrategyId("momentum")) == {}
    # the view (own footprint) is what sizing and marks read: a shifted view pool changes both
    shifted = replace(ctx.raw, subnets=tuple(replace(x, pool=x.pool.shifted(-100 * sx.TAO, 10_000 * sx.TAO))
                                             if x.key == k else x for x in ctx.raw.subnets))
    vctx = replace(ctx, view=shifted)
    assert sleeve_positions(vctx, StrategyId("carry"))[k].value_rao < pos.value_rao
    row_raw = CarryStrategy().evaluate(ctx).rows[k]
    row_view = CarryStrategy().evaluate(vctx).rows[k]
    assert row_view.v_star_rao < row_raw.v_star_rao
    assert sleeve_budget_rao(ctx, 800_000) == 800 * sx.TAO


def test_history_is_generation_safe_and_never_looks_ahead(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92, flow_frac_day=0.02)])
    k = sx.Spec(92).key
    ctx = m.ctx()
    h = History(ctx)
    assert h.flow_frac(k, 1_800) == pytest.approx(0.02 * 1_800 / 7_200, rel=1e-6)
    assert h.at_or_before(sx.B + 10_000) is ctx.raw
    rets = h.epoch_index_returns(k, sx.HK1, 20)
    assert len(rets) == 20 and all(r == pytest.approx(math.log(1.0003), rel=1e-3) for r in rets)
    # a re-registered netuid (new reg_at) has no same-generation history
    k2 = SubnetKey(NetUid(92), Block(sx.B - 1_000))
    m2 = sx.market([sx.Spec(92, reg_at=sx.B - 1_000)])
    ctx2 = m2.ctx(store=m.store(sx.B - 60, 8_400))
    h2 = History(ctx2)
    assert h2.flow_frac(k2, 1_800) is None
    assert h2.epoch_index_returns(k2, sx.HK1, 20) == ()
    # the store refuses anything after its clock
    store = m.store(sx.B - 60, 600)
    store.clock = Block(sx.B - 60)
    with pytest.raises(LookaheadError):
        store.at_or_before(Block(sx.B))


def test_owner_sold_from_store_deltas(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92, owner_alpha_tao=1_000, owner_growth_per_block=-10**6)])
    k = sx.Spec(92).key
    h = History(m.ctx())
    sold = h.owner_sold_alpha(k, 7_200)
    c_o = Decimal(11_796) / Decimal(65_535)
    # sold = blocks x (1e6 rao/block of sales + c_o x 1e7 rao/block of owner-cut accrual that never arrived)
    want = int(7_200 * (10**6 + c_o * 10**7))
    assert sold is not None and abs(sold - want) <= 7_200 // 60 + 2
    assert History(sx.market([sx.Spec(92)]).ctx()).owner_sold_alpha(k, 7_200) is None


def test_floor_rows_and_entry_blocks(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92), sx.Spec(93, reg_at=8_000_001, feat={"prune_rank": 4})])
    k92, k93 = sx.Spec(92).key, sx.Spec(93, reg_at=8_000_001).key
    bv = sx.book_view(cooldowns=((k92, "owner", Block(sx.B + 10)), (k92, "fail_burst", Block(sx.B - 1))),
                      entries_halted_until=Block(sx.B + 1), dissolving=(k93,))
    ctx = m.ctx(book_view=bv)
    rows = floor_rows(ctx, RiskCfg())
    assert rows[k92].eligible and "D" in rows[k93].failed and "D" in rows[sx.BOTTOM].failed
    assert floor_rows(ctx, RiskCfg(), skip_prune=True)[k93].eligible
    assert entry_blocks(ctx, k92) == ("cooldown.owner", "entries_halted")
    assert entry_blocks(ctx, k93) == ("entries_halted", "dissolving")


def test_p_reg_horizon_fallbacks(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92)])
    ctx = m.ctx()
    assert p_reg_horizon(ctx, None, 36_000) == pytest.approx(0.1)
    assert p_reg_horizon(ctx, None, 20_000) == pytest.approx(0.1)            # next longer published horizon
    assert p_reg_horizon(ctx, None, 100_800) == pytest.approx(1 - (1 - 0.13) ** 2)
    assert p_reg_horizon(m.ctx(prune={"p_reg_ppm": ()}), None, 36_000) == 1.0   # nothing known: fail closed
    assert p_reg_horizon(m.ctx(prune={"prune_possible": False}), None, 36_000) == 0.0


def test_signal_builders(sx: SimpleNamespace) -> None:
    k = sx.Spec(92).key
    s = target_signal(StrategyId("carry"), k, Block(1), value_rao=5 * sx.TAO, budget_rao=100 * sx.TAO, edge_day=0.001234,
                      alpha_h=0.01, horizon_blocks=10, declares_dilution=True, reasons=("r",))
    assert s.kind is SignalKind.TARGET and s.weight_ppm == 50_000 and s.max_size_rao == 5 * sx.TAO
    assert s.edge_ppm_day == 1_234 and s.alpha_h_ppm == 10_000
    capped = target_signal(StrategyId("carry"), k, Block(1), value_rao=500 * sx.TAO, budget_rao=100 * sx.TAO,
                           edge_day=0.0, alpha_h=0.0, horizon_blocks=0, declares_dilution=False, reasons=())
    assert capped.weight_ppm == PPM
    e = exit_signal(StrategyId("carry"), k, Block(1), Urgency.EMERGENCY, ("x",))
    assert e.kind is SignalKind.EXIT and e.urgency is Urgency.HIGH          # sleeves never exceed HIGH
    assert Ppm(0) == 0
