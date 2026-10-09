"""protocol.amm: section 10.2 property tests (hypothesis, derandomized profile)."""
from __future__ import annotations

from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from taotrader.core.fixed import DEC
from taotrader.core.orders import FailReason
from taotrader.core.state import PoolKind, PoolState
from taotrader.core.units import PERQUINTILL, AlphaRao, PriceRao, Rao
from taotrader.protocol.amm import (
    ImpactBound,
    SwapError,
    liq_value,
    marginal_after_buy,
    marginal_after_sell,
    max_buy_to_limit,
    max_sell_to_limit,
    quote_buy,
    quote_sell,
    round_trip_cost_ppm,
)

TAO = 10**9
GUARD_REASONS = {FailReason.AMOUNT_TOO_LOW, FailReason.INSUFFICIENT_LIQUIDITY, FailReason.RESERVES_TOO_LOW,
                 FailReason.SWAP_INPUT_TOO_LARGE}


@st.composite
def pools(draw: st.DrawFn, *, skewed: bool = True) -> PoolState:
    tao = draw(st.integers(min_value=50 * TAO, max_value=50_000 * TAO))
    price_rao = draw(st.integers(min_value=100_000, max_value=500_000_000))      # 0.0001 .. 0.5 TAO per alpha
    if skewed:
        w_q = draw(st.one_of(st.just(PERQUINTILL // 2),
                             st.integers(min_value=3 * PERQUINTILL // 10, max_value=7 * PERQUINTILL // 10)))
    else:
        w_q = PERQUINTILL // 2
    alpha = max(tao * (PERQUINTILL - w_q) * TAO // (w_q * price_rao), 10**7)
    fee = draw(st.sampled_from([0, 33, 196, 330]))
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao, px_alpha=alpha,
                     w_quote_e18=w_q, fee_rate=fee)


def _ln_invariant(p: PoolState, x: int, y: int) -> Decimal:
    w_b = DEC.divide(Decimal(p.w_base_e18), Decimal(PERQUINTILL))
    w_q = DEC.divide(Decimal(p.w_quote_e18), Decimal(PERQUINTILL))
    return DEC.add(DEC.multiply(w_b, DEC.ln(Decimal(x))), DEC.multiply(w_q, DEC.ln(Decimal(y))))


@given(pools(), st.integers(min_value=3_000_000, max_value=2_000 * TAO))
def test_round_trip_on_unchanged_pool_is_never_profitable(p: PoolState, size: int) -> None:
    try:
        qb = quote_buy(p, Rao(size))
    except SwapError:
        return
    after = p.shifted(qb.d_tao, qb.d_alpha)
    assert liq_value(after, AlphaRao(qb.amount_out)) <= size              # PERSISTENT leg
    assert liq_value(p, AlphaRao(qb.amount_out)) <= size                  # TEMPORARY leg (healed pool)
    assert round_trip_cost_ppm(p, Rao(size), ImpactBound.PERSISTENT, 0) >= 0
    assert round_trip_cost_ppm(p, Rao(size), ImpactBound.TEMPORARY, 0) >= round_trip_cost_ppm(
        p, Rao(size), ImpactBound.PERSISTENT, 0) - 1


@given(pools(), st.integers(min_value=10**9, max_value=10**15), st.integers(min_value=1, max_value=999))
def test_split_sell_matches_single_sell_within_fee_rounding(p: PoolState, a: int, cut_permille: int) -> None:
    a1 = a * cut_permille // 1_000
    assume(0 < a1 < a)
    try:
        single = quote_sell(p, AlphaRao(a)).amount_out
        q1 = quote_sell(p, AlphaRao(a1))
        q2 = quote_sell(p.shifted(q1.d_tao, q1.d_alpha), AlphaRao(a - a1))
    except SwapError:
        return
    split = q1.amount_out + q2.amount_out
    assert split <= single + 2                                  # never better than one sell beyond rounding
    assert single - split <= single * p.fee_rate // 65_535 + 3  # only the first leg's fee-alpha sale differs


@given(pools(), st.integers(min_value=3_000_000, max_value=5_000 * TAO))
def test_weighted_invariant_non_decreasing_for_buys(p: PoolState, size: int) -> None:
    try:
        q = quote_buy(p, Rao(size))
    except SwapError:
        return
    before = _ln_invariant(p, p.px_alpha, p.px_tao)
    after = _ln_invariant(p, p.px_alpha + q.d_alpha, p.px_tao + q.d_tao)
    assert after >= DEC.subtract(before, Decimal("1e-45"))   # DEC: the default context has only 28 digits


@given(pools(), st.integers(min_value=1, max_value=400_000))
def test_max_buy_to_limit_respects_the_limit(p: PoolState, up_ppm: int) -> None:
    spot = p.spot_rao()
    limit = PriceRao(spot + max(spot * up_ppm // 1_000_000, 1) + 1)
    gross = max_buy_to_limit(p, limit)
    assert gross >= 0
    assert marginal_after_buy(p, gross) <= limit


@given(pools(), st.integers(min_value=1, max_value=400_000))
def test_max_sell_to_limit_respects_the_limit(p: PoolState, down_ppm: int) -> None:
    spot = p.spot_rao()
    limit = PriceRao(spot - max(spot * down_ppm // 1_000_000, 1))
    assume(limit > 0)
    a = max_sell_to_limit(p, limit)
    assert a >= 0
    assert marginal_after_sell(p, a) >= limit


@given(pools(), st.integers(min_value=-10, max_value=10**17), st.booleans())
def test_every_guard_raises_a_chain_error_name(p: PoolState, amount: int, buy: bool) -> None:
    try:
        q = quote_buy(p, Rao(amount)) if buy else quote_sell(p, AlphaRao(amount), partial_remaining=amount % 2 == 0)
    except SwapError as e:
        assert e.reason in GUARD_REASONS
        assert str(e) == e.reason.value
        return
    assert q.amount_out > 0 and q.fee >= 0
    assert q.amount_out <= (p.alpha if buy else p.tao)


@given(pools(skewed=False), st.integers(min_value=3_000_000, max_value=10**13))
def test_results_are_identical_on_repeated_runs(p: PoolState, size: int) -> None:
    try:
        a, b = quote_buy(p, Rao(size)), quote_buy(p, Rao(size))
    except SwapError:
        return
    assert a == b
    assert marginal_after_buy(p, Rao(size)) == marginal_after_buy(p, Rao(size))


@given(pools(), st.integers(min_value=3_000_000, max_value=3_000 * TAO), st.integers(min_value=0, max_value=20_000))
def test_planner_buy_limit_construction_holds(p: PoolState, tao_in: int, beta_ppm: int) -> None:
    """Section 3.12: limit = ceil(marginal_after_buy(tao_in) * (1 + beta)) > spot  =>  tao_in <= max_buy_to_limit."""
    try:
        quote_buy(p, Rao(tao_in))
    except SwapError:
        return
    m = marginal_after_buy(p, Rao(tao_in))
    limit = PriceRao(-((-m * (1_000_000 + beta_ppm)) // 1_000_000))
    assume(Decimal(limit) > DEC.multiply(p.spot(), Decimal(TAO)))
    assert tao_in <= max_buy_to_limit(p, limit)


@given(pools(), st.integers(min_value=10**9, max_value=10**15), st.integers(min_value=0, max_value=50_000))
def test_planner_sell_limit_construction_holds(p: PoolState, alpha: int, beta_ppm: int) -> None:
    """Section 3.12: limit = floor(marginal_after_sell(alpha) * (1 - beta)) < spot  =>  alpha <= max_sell_to_limit."""
    try:
        quote_sell(p, AlphaRao(alpha))
    except SwapError:
        return
    m = marginal_after_sell(p, AlphaRao(alpha))
    limit = PriceRao(m * (1_000_000 - beta_ppm) // 1_000_000)
    assume(limit > 0 and Decimal(limit) < DEC.multiply(p.spot(), Decimal(TAO)))
    assert alpha <= max_sell_to_limit(p, limit)
