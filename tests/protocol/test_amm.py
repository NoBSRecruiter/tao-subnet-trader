"""protocol.amm: section 10.1 verified vectors (golden fixtures) and unit tests."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taotrader.core.orders import FailReason
from taotrader.core.state import PoolKind, PoolState
from taotrader.core.units import PPM, RAO_PER_TAO, AlphaRao, PriceRao, Rao
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
    spot_rao_exact,
    v_max,
    v_star,
)

TAO = RAO_PER_TAO


def _golden_pool(gsnap, dec, name: str, netuid: int) -> PoolState:
    gs = gsnap(name, 0)
    return dec.build_subnet(gs, netuid, dec.build_globals(gs)).pool


# ------------------------------------------------------------------------------------------------- golden vectors
def test_sn92_10_tao_buy_matches_sim_swap(gsnap, dec, sim_fields) -> None:
    gs = gsnap("sn92_9240388", 0)
    pool = _golden_pool(gsnap, dec, "sn92_9240388", 92)
    q = quote_buy(pool, Rao(10 * TAO))
    sim = sim_fields(gs.runtime("SwapRuntimeApi_sim_swap_tao_for_alpha", netuid=92, tao_rao=10 * TAO))
    assert q.fee == 5_035_477                                   # floor(10e9 * 33 / 65535)
    assert q.amount_in - q.fee == 9_994_964_523 == sim[0]       # net tao_amount
    assert sim[1] == 7_289_425_629_146                          # 7,289.425629 alpha
    assert abs(q.amount_out - sim[1]) <= 3                      # integer floors within a few rao
    assert abs(Decimal(q.amount_out - sim[1]) / Decimal(sim[1])) <= Decimal("1e-7")
    assert q.amount_out == sim[1]                               # exact to the rao in practice
    assert sim[2] == q.fee
    assert pool.spot_rao() == dec.le(gs.runtime("SwapRuntimeApi_current_alpha_price", netuid=92)) == 1_348_210
    assert q.d_tao == q.amount_in - q.fee and q.d_alpha == -q.amount_out and q.author_fee_tao == 0


def test_sn92_100_tao_buy_vector(gsnap, dec, sim_fields) -> None:
    gs = gsnap("sn92_9240388", 0)
    pool = _golden_pool(gsnap, dec, "sn92_9240388", 92)
    q = quote_buy(pool, Rao(100 * TAO))
    sim = sim_fields(gs.runtime("SwapRuntimeApi_sim_swap_tao_for_alpha", netuid=92, tao_rao=100 * TAO))
    assert q.amount_out == sim[1] == 63_351_665_005_924
    assert round(Decimal(q.amount_out) / TAO, 2) == Decimal("63351.67")
    impact = spot_free = Decimal(q.amount_in) * TAO / spot_rao_exact(pool) - q.amount_out   # gross/spot - out
    assert abs(impact - sim[5]) <= 2
    assert round(spot_free / TAO, 2) == Decimal("10820.70")
    assert round(impact / (Decimal(q.amount_in) * TAO / spot_rao_exact(pool)) * 100, 1) == Decimal("14.6")
    assert abs(Decimal(587_199_047_950) / TAO - Decimal("587.2")) < Decimal("0.01")   # the 587.2-TAO pool


def test_sn92_1000_alpha_sell_matches_sim_swap(gsnap, dec, sim_fields) -> None:
    gs = gsnap("sn92_9240388", 0)
    pool = _golden_pool(gsnap, dec, "sn92_9240388", 92)
    q = quote_sell(pool, AlphaRao(1_000 * TAO))
    sim = sim_fields(gs.runtime("SwapRuntimeApi_sim_swap_alpha_for_tao", netuid=92, alpha_rao=1_000 * TAO))
    assert q.amount_out == sim[0] == 1_344_446_749
    assert q.fee == sim[3] and q.amount_in - q.fee == sim[1]
    assert q.d_alpha == 1_000 * TAO and q.d_tao == -(q.amount_out + q.author_fee_tao)
    assert 0 < q.author_fee_tao < q.fee * pool.spot_rao() // TAO + 2


def test_sn1_one_tao_quote(gsnap, dec, sim_fields) -> None:
    """Brief 2.8 / DESIGN 10.1 SN1 vector. The design's alpha_out 152,285,961,807 is unreproducible (WP0 scan of
    [9,238,000, 9,242,700]); at 9,240,388 the brief's other values hold exactly and sim_swap gives 152,290,647,774."""
    gs = gsnap("sn1_quote_9240388", 0)
    pool = _golden_pool(gsnap, dec, "sn1_quote_9240388", 1)
    q = quote_buy(pool, Rao(TAO))
    sim = sim_fields(gs.runtime("SwapRuntimeApi_sim_swap_tao_for_alpha", netuid=1, tao_rao=TAO))
    assert pool.spot_rao() == 6_562_800
    assert q.fee == 503_547 and q.amount_in - q.fee == 999_496_453 == sim[0]
    assert q.amount_out == sim[1] == 152_290_647_774
    assert round(Decimal(q.amount_out) / TAO, 2) == Decimal("152.29")


@pytest.mark.parametrize("netuid", [1, 19, 64])
def test_era_b_virtual_reserves_match_sim_swap(gsnap, dec, sim_fields, netuid: int) -> None:
    gs = gsnap("erab_7000020", 0)
    pool = _golden_pool(gsnap, dec, "erab_7000020", netuid)
    assert pool.kind is PoolKind.CP_V3_VIRTUAL and pool.w_quote_e18 == 5 * 10**17
    price = dec.le(gs.runtime("SwapRuntimeApi_current_alpha_price", netuid=netuid))
    assert abs(pool.spot_rao() - price) <= 1
    n = 0
    for r in gs.raw["runtime_api"]:
        if r["args"].get("netuid") != netuid or not r["method"].startswith("SwapRuntimeApi_sim_swap"):
            continue
        sim = sim_fields(r["result"])
        assert len(sim) == 4                                    # 32-byte SimSwapResult at spec 348
        if r["method"].endswith("tao_for_alpha"):
            q = quote_buy(pool, Rao(r["args"]["tao_rao"]))
            assert q.amount_in - q.fee == sim[0] and q.fee == sim[2]
            out, ref = q.amount_out, sim[1]
        else:
            q = quote_sell(pool, AlphaRao(r["args"]["alpha_rao"]))
            assert q.amount_in - q.fee == sim[1] and q.fee == sim[3]
            out, ref = q.amount_out, sim[0]
        assert abs(Decimal(out - ref) / Decimal(ref)) <= Decimal("3e-7")
        n += 1
    assert n == 6


# ------------------------------------------------------------------------------------------------- impact tables
SLIPPAGE_TABLE = {
    300: ("0.33", "3.23", "25.0"), 500: ("0.20", "1.96", "16.7"), 1_000: ("0.10", "0.99", "9.09"),
    2_000: ("0.05", "0.50", "4.76"), 3_000: ("0.03", "0.33", "3.23"), 6_700: ("0.015", "0.15", "1.47"),
}


@pytest.mark.parametrize("pool_tao", sorted(SLIPPAGE_TABLE))
def test_slippage_table(balancer_pool, pool_tao: int) -> None:
    """Shortfall against spot of the post-fee amount: D'/(T+D'), D' = D(1 - 0.000504) (brief 2.5). The printed table
    is rounded from D/(T+D) in places (T=300, D=10: 3.2258 -> 3.23 while D'/(T+D') = 3.2242), so the tolerance is half
    a unit of the printed digit plus the fee's relative effect (0.1% of the value)."""
    pool = balancer_pool(pool_tao * TAO, pool_tao * TAO * 700)
    for delta, expected in zip((1, 10, 100), SLIPPAGE_TABLE[pool_tao], strict=True):
        q = quote_buy(pool, Rao(delta * TAO))
        dy = Decimal(q.amount_in - q.fee)
        impact_pct = (1 - Decimal(q.amount_out) * spot_rao_exact(pool) / TAO / dy) * 100
        exp = Decimal(expected)
        half_ulp = Decimal(1).scaleb(exp.as_tuple().exponent) / 2   # type: ignore[arg-type]
        assert abs(impact_pct - exp) <= half_ulp + exp / 1_000, (pool_tao, delta, impact_pct)
        dprime = Decimal(delta) * (1 - Decimal("0.000504"))
        assert abs(impact_pct - dprime / (pool_tao + dprime) * 100) < Decimal("0.001")


@pytest.mark.parametrize(("pool_tao", "move_pct"), [(300, 78), (1_000, 21)])
def test_post_trade_spot_after_100_tao_buy(balancer_pool, pool_tao: int, move_pct: int) -> None:
    pool = balancer_pool(pool_tao * TAO, pool_tao * TAO * 500)
    q = quote_buy(pool, Rao(100 * TAO))
    after = pool.shifted(q.d_tao, q.d_alpha)
    move = (Decimal(after.spot_rao()) / Decimal(pool.spot_rao()) - 1) * 100
    assert round(move) == move_pct
    assert abs(q.marginal_after - after.spot_rao()) <= 1
    assert abs(marginal_after_buy(pool, Rao(100 * TAO)) - q.marginal_after) <= 1


@pytest.mark.parametrize(("slip_ppm", "frac"), [(10_000, "1.0101"), (20_000, "2.0408"), (50_000, "5.263"),
                                                (100_000, "11.11")])
def test_v_max(slip_ppm: int, frac: str) -> None:
    t = 3_000 * TAO
    got = Decimal(v_max(Rao(t), slip_ppm)) / t * 100
    exp = Decimal(frac)
    assert abs(got - exp) <= Decimal(1).scaleb(exp.as_tuple().exponent) / 2   # type: ignore[arg-type]
    with pytest.raises(ValueError):
        v_max(Rao(t), PPM)


def test_v11_default_five_percent_bound(balancer_pool) -> None:
    """Max buy under a 5% marginal bound = y*(sqrt(1.05) - 1) = 2.47% of the reserve; SN70 (241 TAO) -> ~5.9 TAO."""
    pool = balancer_pool(241 * TAO, 241 * TAO * 900)
    limit = PriceRao(int((spot_rao_exact(pool) * Decimal("1.05")).to_integral_value()))
    gross = max_buy_to_limit(pool, limit)
    net = gross - gross * pool.fee_rate // 65_535
    assert abs(Decimal(net) / pool.tao - (Decimal("1.05").sqrt() - 1)) < Decimal("1e-6")
    assert round(Decimal(net) / pool.tao * 100, 2) == Decimal("2.47")
    assert Decimal("5.9") <= Decimal(gross) / TAO < Decimal("6.0")
    assert marginal_after_buy(pool, gross) <= limit < marginal_after_buy(pool, Rao(gross + 10_000_000))


@pytest.mark.parametrize(("frac_bp", "expected_pct"), [(25, "0.62"), (50, "1.11"), (100, "2.09"), (200, "4.0")])
def test_round_trip_temporary_bound_at_3000_tao(balancer_pool, frac_bp: int, expected_pct: str) -> None:
    """Section 2.0: RT(V) ~ 2f + 2V/(T+V) + 0.001865/V at T = 3,000 TAO. The printed values come from that
    approximation (reproduced exactly below); the exact two-leg quote is slightly cheaper (2V/(T+2V) impact), within
    2% of the printed value."""
    pool = balancer_pool(3_000 * TAO, 3_000 * TAO * 400)
    size = Rao(3_000 * TAO * frac_bp // 10_000)
    exp = Decimal(expected_pct)
    v, t, f = Decimal(size) / TAO, Decimal(3_000), Decimal(33) / 65_535
    approx = (2 * f + 2 * v / (t + v) + Decimal("0.001865") / v) * 100
    assert abs(approx - exp) <= Decimal(1).scaleb(exp.as_tuple().exponent) / 2 + Decimal("0.0001")  # type: ignore[arg-type]
    got = Decimal(round_trip_cost_ppm(pool, size, ImpactBound.TEMPORARY, 1_865_000)) / 10_000
    assert abs(got - exp) / exp <= Decimal("0.02")
    assert got <= approx
    exact = (1 - Decimal(liq_value(pool, AlphaRao(quote_buy(pool, size).amount_out)) - 1_865_000) / size) * 100
    assert abs(exact - got) <= Decimal("0.0001")
    persistent = Decimal(round_trip_cost_ppm(pool, size, ImpactBound.PERSISTENT, 1_865_000)) / 10_000
    f2 = Decimal(2 * 33) / 65_535 * 100
    assert abs(persistent - (f2 + Decimal(1_865_000) / size * 100)) < Decimal("0.01")
    assert persistent < got


def test_prototype_round_trip_temporary_vs_persistent(balancer_pool) -> None:
    """Section 8.3: a 10-TAO position in a 1,000-TAO pool loses 0.208 TAO TEMPORARY vs 0.012 PERSISTENT (the 0.208 is
    the 2f + 2V/(T+V) approximation; the exact quote gives 0.206)."""
    pool = balancer_pool(1_000 * TAO, 1_000 * TAO * 300)
    size = Rao(10 * TAO)
    temp = Decimal(round_trip_cost_ppm(pool, size, ImpactBound.TEMPORARY, 0)) * 10 / PPM
    pers = Decimal(round_trip_cost_ppm(pool, size, ImpactBound.PERSISTENT, 1_865_000)) * 10 / PPM
    assert abs(temp - Decimal("0.208")) <= Decimal("0.003")
    assert round(pers, 3) == Decimal("0.012")
    assert temp / pers > 15                              # the ~17x bracket the reports show


def test_v_star() -> None:
    pool = PoolState(kind=PoolKind.BALANCER, tao=Rao(1_000 * TAO), alpha=AlphaRao(10**15), px_tao=1_000 * TAO,
                     px_alpha=10**15, w_quote_e18=5 * 10**17, fee_rate=33)
    got = v_star(pool, 20_000)                         # alpha_h 2% -> T*(0.02 - 2*33/65535)/4
    exact = Decimal(1_000 * TAO) * (Decimal("0.02") - Decimal(66) / 65_535) / 4
    assert abs(got - exact) <= 1
    assert v_star(pool, 20_000, 500_000) == got * 500_000 // PPM or abs(v_star(pool, 20_000, 500_000) - got // 2) <= 1
    assert v_star(pool, 1_000) == 0                    # edge below 2f -> no trade


# ------------------------------------------------------------------------------------------------- guards
def test_buy_guards_raise_chain_error_names(balancer_pool) -> None:
    pool = balancer_pool(500 * TAO, 500 * TAO * 1_000)
    cases = [
        (Rao(2_000_000), FailReason.AMOUNT_TOO_LOW),             # gross = min stake, post-fee below it
        (Rao(0), FailReason.AMOUNT_TOO_LOW),
        (Rao(500 * TAO * 1_000 + 1), FailReason.INSUFFICIENT_LIQUIDITY),
    ]
    for amount, reason in cases:
        with pytest.raises(SwapError) as ei:
            quote_buy(pool, amount)
        assert ei.value.reason is reason and str(ei.value) == reason.value
    assert quote_buy(pool, Rao(2_001_100)).amount_out > 0           # just above the minimum after the fee
    thin = balancer_pool(500 * TAO, 999_999)
    with pytest.raises(SwapError) as ei:
        quote_buy(thin, Rao(TAO))
    assert ei.value.reason is FailReason.RESERVES_TOO_LOW


def test_swap_input_too_large_guard() -> None:
    """Net input > 1000 x input-side reserve with the gross pre-check still passing (fee rate high)."""
    pool = PoolState(kind=PoolKind.BALANCER, tao=Rao(10**7), alpha=AlphaRao(10**15), px_tao=10**7, px_alpha=10**15,
                     w_quote_e18=5 * 10**17, fee_rate=0)
    with pytest.raises(SwapError) as ei:
        quote_buy(pool, Rao(10**10 + 1))
    assert ei.value.reason is FailReason.INSUFFICIENT_LIQUIDITY
    sell_pool = PoolState(kind=PoolKind.BALANCER, tao=Rao(10**12), alpha=AlphaRao(10**6), px_tao=10**12, px_alpha=10**6,
                          w_quote_e18=5 * 10**17, fee_rate=0)
    with pytest.raises(SwapError) as ei:
        quote_sell(sell_pool, AlphaRao(10**9 + 1))
    assert ei.value.reason is FailReason.INSUFFICIENT_LIQUIDITY


def test_sell_guards_raise_chain_error_names(balancer_pool) -> None:
    pool = balancer_pool(500 * TAO, 500 * TAO * 1_000)
    with pytest.raises(SwapError) as ei:
        quote_sell(pool, AlphaRao(0))
    assert ei.value.reason is FailReason.AMOUNT_TOO_LOW
    small = AlphaRao(1_000 * 1_000_000)                  # ~1,000 alpha rao per rao: ~0.001 TAO out
    assert quote_sell(pool, small).amount_out < 2_000_000
    with pytest.raises(SwapError) as ei:
        quote_sell(pool, small, partial_remaining=True)
    assert ei.value.reason is FailReason.AMOUNT_TOO_LOW  # partial sells need >= 0.002 TAO out
    dry = balancer_pool(999_999, 10**15)
    with pytest.raises(SwapError) as ei:
        quote_sell(dry, AlphaRao(10**12))
    assert ei.value.reason is FailReason.RESERVES_TOO_LOW
    with pytest.raises(SwapError) as ei:
        quote_sell(pool, AlphaRao(1))                    # rounds to 0 TAO out
    assert ei.value.reason is FailReason.INSUFFICIENT_LIQUIDITY
    assert liq_value(pool, AlphaRao(1)) == 0 and liq_value(pool, AlphaRao(0)) == 0


def test_limit_strictness(balancer_pool) -> None:
    pool = balancer_pool(800 * TAO, 800 * TAO * 600)
    spot = pool.spot_rao()
    for lim in (spot, spot - 1):                         # spot_rao is floored, so spot itself is still <= exact spot
        with pytest.raises(SwapError) as ei:
            max_buy_to_limit(pool, PriceRao(lim))
        assert ei.value.reason is FailReason.PRICE_LIMIT_EXCEEDED
    with pytest.raises(SwapError) as ei:
        max_sell_to_limit(pool, PriceRao(spot + 1))
    assert ei.value.reason is FailReason.PRICE_LIMIT_EXCEEDED
    assert max_buy_to_limit(pool, PriceRao(spot + 1)) >= 0
    assert max_sell_to_limit(pool, PriceRao(spot)) >= 0
    with pytest.raises(ValueError):
        max_sell_to_limit(pool, PriceRao(0))


def test_limits_bound_the_marginal_price(balancer_pool) -> None:
    pool = balancer_pool(1_200 * TAO, 1_200 * TAO * 750, w_quote_e18=499_999_964_641_764_870)
    up = PriceRao(pool.spot_rao() * 103 // 100)
    gross = max_buy_to_limit(pool, up)
    assert marginal_after_buy(pool, gross) <= up < marginal_after_buy(pool, Rao(gross + 1_000_000))
    q = quote_buy(pool, gross)
    assert q.marginal_after <= up + 1
    down = PriceRao(pool.spot_rao() * 97 // 100)
    a = max_sell_to_limit(pool, down)
    assert marginal_after_sell(pool, a) >= down > marginal_after_sell(pool, AlphaRao(a + 10**9))
    assert quote_sell(pool, a).marginal_after >= down - 1


def test_degenerate_pools() -> None:
    bad = PoolState(kind=PoolKind.BALANCER, tao=Rao(10), alpha=AlphaRao(10), px_tao=10, px_alpha=10,
                    w_quote_e18=0, fee_rate=33)
    with pytest.raises(ValueError):
        quote_buy(bad, Rao(TAO))
    empty = PoolState(kind=PoolKind.BALANCER, tao=Rao(0), alpha=AlphaRao(0), px_tao=0, px_alpha=0,
                      w_quote_e18=5 * 10**17, fee_rate=33)
    assert liq_value(empty, AlphaRao(TAO)) == 0
    with pytest.raises(SwapError):
        quote_buy(empty, Rao(TAO))
    with pytest.raises(ValueError):
        round_trip_cost_ppm(empty, Rao(0), ImpactBound.TEMPORARY, 0)


def test_era_a_cp_real_and_exact_half_weight_path() -> None:
    """CP kinds and Balancer pools at exactly 0.5 take the exact integer path x*dy//(y+dy)."""
    p = PoolState(kind=PoolKind.CP_REAL, tao=Rao(5 * 10**12), alpha=AlphaRao(7 * 10**15), px_tao=5 * 10**12,
                  px_alpha=7 * 10**15, w_quote_e18=5 * 10**17, fee_rate=196)
    q = quote_buy(p, Rao(3 * TAO))
    fee = 3 * TAO * 196 // 65_535
    dy = 3 * TAO - fee
    assert q.fee == fee and q.amount_out == p.px_alpha * dy // (p.px_tao + dy)
    s = quote_sell(p, AlphaRao(10**12))
    fa = 10**12 * 196 // 65_535
    assert s.amount_out == p.px_tao * (10**12 - fa) // (p.px_alpha + 10**12 - fa)


def test_quotes_are_deterministic(gsnap, dec) -> None:
    pool = _golden_pool(gsnap, dec, "sn92_9240388", 92)
    a = [quote_buy(pool, Rao(k * 137_000_001)) for k in range(1, 20)]
    b = [quote_buy(pool, Rao(k * 137_000_001)) for k in range(1, 20)]
    assert a == b
    assert [marginal_after_sell(pool, AlphaRao(k * 10**12)) for k in range(1, 5)] == [
        marginal_after_sell(pool, AlphaRao(k * 10**12)) for k in range(1, 5)]
