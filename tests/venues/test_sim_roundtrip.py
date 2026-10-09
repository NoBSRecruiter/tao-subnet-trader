"""Acceptance (DESIGN.md 8.3, WP6): a 10-TAO round trip in a 1,000-TAO pool loses ~0.208 TAO under TEMPORARY impact and
~0.012 TAO under PERSISTENT impact (fees included), and the venue's loss equals protocol.amm.round_trip_cost_ppm."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported
from taotrader.core.units import AlphaRao, Rao
from taotrader.protocol.amm import ImpactBound, liq_value, marginal_after_sell, quote_buy, round_trip_cost_ppm
from taotrader.venues.sim import SimVenue

TAO = 10**9
BUY_BLOCK, SELL_BLOCK = 10_000, 10_060


def _round_trip(consts, harness, snap, buy, sell, half_life: int | None, sell_block: int = SELL_BLOCK) -> tuple[int, int]:
    """(loss in rao incl. tx fees, alpha bought)."""
    c = consts
    cfg = ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=half_life)
    h = harness(SimVenue(c["BOOK"], cfg, seed=0))
    h.capital(100 * TAO, fee_float=TAO)
    h.place(buy(BUY_BLOCK, 10 * TAO), snap(BUY_BLOCK))
    (fb,) = h.tick(snap(BUY_BLOCK + 5))
    assert isinstance(fb, FillReported)
    view = h.venue.mark_to(snap(sell_block))             # what the planner sizes the exit on
    alpha = fb.fill.alpha
    limit = marginal_after_sell(view.get(c["KEY"]).pool, AlphaRao(alpha)) * 98 // 100
    h.place(sell(sell_block, limit=limit, full=True), snap(sell_block))
    (fs,) = h.tick(snap(sell_block + 5))
    assert isinstance(fs, FillReported) and fs.fill.complete and fs.fill.alpha == alpha
    h.check_ledger()
    loss = (100 * TAO - h.venue.cash) + (TAO - h.bal("fee_float"))
    return loss, alpha


@pytest.mark.parametrize(("half_life", "bound", "approx_tao"), [
    (0, ImpactBound.TEMPORARY, Decimal("0.208")),
    (None, ImpactBound.PERSISTENT, Decimal("0.012")),
])
def test_ten_tao_round_trip_bracket(consts, harness, snap, buy, sell, half_life, bound, approx_tao) -> None:
    loss, alpha = _round_trip(consts, harness, snap, buy, sell, half_life)
    assert abs(Decimal(loss) / TAO - approx_tao) < Decimal("0.0005"), loss
    # exactly the protocol's round-trip bound: buy on the pool, sell on the healed (TEMPORARY) or shifted pool
    p = snap(BUY_BLOCK).get(consts["KEY"]).pool
    fees = ExecCfg().buy_tx_fee_rao + ExecCfg().sell_tx_fee_rao
    qb = quote_buy(p, Rao(10 * TAO))
    back = liq_value(p if bound is ImpactBound.TEMPORARY else p.shifted(qb.d_tao, qb.d_alpha), AlphaRao(alpha))
    assert loss == 10 * TAO + fees - back
    rt = round_trip_cost_ppm(p, Rao(10 * TAO), bound, fees)
    assert abs(loss * 1_000_000 // (10 * TAO) - rt) <= 1


def test_temporary_is_about_17x_persistent(consts, harness, snap, buy, sell) -> None:
    temp, _ = _round_trip(consts, harness, snap, buy, sell, 0)
    pers, _ = _round_trip(consts, harness, snap, buy, sell, None)
    assert 15 < temp / pers < 19


def test_half_life_middle_case_lies_inside_the_bracket(consts, harness, snap, buy, sell) -> None:
    temp, _ = _round_trip(consts, harness, snap, buy, sell, 0)
    pers, _ = _round_trip(consts, harness, snap, buy, sell, None)
    hl = 14_400
    one_half_life, _ = _round_trip(consts, harness, snap, buy, sell, hl, sell_block=BUY_BLOCK + 5 + hl - 5)
    immediate, _ = _round_trip(consts, harness, snap, buy, sell, hl)
    assert pers < immediate < one_half_life < temp
    # half of the displacement is gone after one half-life: the impact part of the loss is roughly the midpoint
    assert abs(Decimal(one_half_life - pers) / Decimal(temp - pers) - Decimal("0.5")) < Decimal("0.03")
