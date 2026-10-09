"""protocol.ema: section 10.1 EMA vectors (half-life table, warm-up, t* worst case, Tier A coverage) and unit tests."""
from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core.fixed import DEC
from taotrader.core.state import ChainSnapshot
from taotrader.core.units import Block
from taotrader.protocol.ema import (
    blocks_since_start,
    ema_alpha,
    ema_forecast,
    ema_step,
    ema_warmup_fraction,
    half_life_blocks,
    project_ema,
    t_star_blocks,
)
from taotrader.protocol.prune import ladder

ALPHA = Decimal("0.0003")
H = 201_600
DAY = 7_200
BLOCKS_PER_HOUR = 300


def _a(b: int, alpha: Decimal = ALPHA, halving: int = H) -> Decimal:
    return DEC.divide(DEC.multiply(alpha, Decimal(b)), Decimal(b + halving))


def _hours(blocks: Decimal | int) -> Decimal:
    return Decimal(blocks) / BLOCKS_PER_HOUR


def _eligible(snap: ChainSnapshot) -> ChainSnapshot:
    """The bulk fixture carries only NetworksAdded / NetworkRegisteredAt / SubnetMovingPrice / FirstEmissionBlockNumber
    for most netuids; every started subnet of the ladder is live (SubtokenEnabled, registration allowed) on chain."""
    subs = tuple(replace(s, subtoken_enabled=True, reg_allowed=True) if s.first_emission_block is not None else s
                 for s in snap.subnets)
    return ChainSnapshot(block=snap.block, block_hash=snap.block_hash, timestamp_ms=snap.timestamp_ms, plan=snap.plan,
                         glob=snap.glob, subnets=subs)


# ------------------------------------------------------------------------------------------------- half-life table
@pytest.mark.parametrize(("b", "lo", "hi"), [
    (4_300_000, Decimal("7.7"), Decimal("8.4")),          # mature, genesis-era: about 7.7-8.4 h
    (30 * DAY, Decimal("14.85"), Decimal("14.95")),       # 30 d after start_call: 14.9 h
    (7 * DAY, Decimal("1.55") * 24, Decimal("1.65") * 24),  # 7 d: 1.6 d
    (1 * DAY, Decimal("9.25") * 24, Decimal("9.35") * 24),  # 1 d: 9.3 d
])
def test_half_life_table(b: int, lo: Decimal, hi: Decimal) -> None:
    hl = half_life_blocks(_a(b))
    assert hl is not None
    assert lo <= _hours(hl) <= hi


def test_mature_half_life_bounds() -> None:
    """a -> 0.0003 as b grows: the half-life falls toward ln2/0.0003 = 2,310 blocks = 7.70 h; b = 2.2M gives 8.4 h."""
    floor_h = _hours(DEC.divide(DEC.ln(Decimal(2)), ALPHA))
    assert round(floor_h, 2) == Decimal("7.70")
    hl = half_life_blocks(_a(2_220_000))
    assert hl is not None and round(_hours(hl), 1) == Decimal("8.4")


def test_sn92_half_life_from_fixture(gsnap, dec) -> None:
    """SN92 at 9,240,388: b = block - (FirstEmissionBlockNumber - 1); half-life 9.4 h (brief 3.3)."""
    gs = gsnap("sn92_9240388", 0)
    glob = dec.build_globals(gs)
    s = dec.build_subnet(gs, 92, glob)
    assert glob.moving_alpha == Decimal(1_288_490) / Decimal(2**32)          # 0.0003 (I96F32 raw 1,288,490)
    b = blocks_since_start(s, Block(gs.block))
    assert s.first_emission_block is not None and b == gs.block - (s.first_emission_block - 1)
    a = ema_alpha(glob, s, Block(gs.block))
    assert a == DEC.divide(DEC.multiply(glob.moving_alpha, Decimal(b)), Decimal(b + s.ema_halving_blocks))
    hl = half_life_blocks(a)
    assert hl is not None and abs(_hours(hl) - Decimal("9.4")) < Decimal("0.1")


def test_ema_alpha_zero_when_frozen(make_subnet, make_globals) -> None:
    glob = make_globals()
    s = make_subnet(5, reg_at=8_000_000)
    blk = Block(9_000_000)
    assert ema_alpha(glob, s, blk) > 0
    assert ema_alpha(glob, replace(s, reg_allowed=False), blk) == 0           # NetworkRegistrationAllowed false freezes
    assert ema_alpha(glob, replace(s, subtoken_enabled=False), blk) == 0
    assert ema_alpha(glob, replace(s, first_emission_block=None), blk) == 0   # no start_call
    assert ema_alpha(glob, replace(s, emission_enabled=False), blk) > 0        # emission off does NOT freeze
    assert ema_alpha(glob, s, Block(s.first_emission_block - 1)) == 0          # b = 0 before the first update
    assert half_life_blocks(Decimal(0)) is None


# ------------------------------------------------------------------------------------------------- warm-up
@pytest.mark.parametrize(("days", "lo", "hi"), [
    (1, Decimal("3.6"), Decimal("4.0")), (3, Decimal("27.5"), Decimal("28.5")), (7, Decimal("79.5"), Decimal("80.5")),
    (10, Decimal("95.55"), Decimal("95.65")), (14, Decimal("99.65"), Decimal("99.75")),
])
def test_warm_up_closed_form(days: int, lo: Decimal, hi: Decimal) -> None:
    """EMA/spot = 1 - exp(-0.0003*(b - 201,600*ln(1 + b/201,600))): ~3.7-4% at 1 d, ~28% at 3 d, ~80% at 7 d,
    95.6% at 10 d, 99.7% at 14 d."""
    b = days * DAY
    got = ema_warmup_fraction(b, ALPHA, H) * 100
    assert lo <= got <= hi
    explicit = 1 - DEC.exp(-ALPHA * (Decimal(b) - H * DEC.ln(1 + Decimal(b) / H)))   # 28-digit default context
    assert abs(got - explicit * 100) < Decimal("1e-20")


@pytest.mark.parametrize("days", [1, 3, 7, 10, 14])
def test_warm_up_closed_form_matches_per_block_recursion(days: int) -> None:
    """The chain steps EMA' = a*min(spot,1) + (1-a)*EMA with a = 0.0003*b/(b+H), b = 1, 2, ... after start_call."""
    b = days * DAY
    ema = 0.0
    for i in range(1, b + 1):
        a = 0.0003 * i / (i + H)
        ema = a + (1 - a) * ema
    assert abs(float(ema_warmup_fraction(b, ALPHA, H)) - ema) < 1e-4


def test_ema_forecast_matches_recursion_from_a_nonzero_state() -> None:
    e0, spot, b0, dn = Decimal("0.004"), Decimal("0.0015"), 900_000, 3_000
    ema = e0
    for i in range(1, dn + 1):
        ema = ema_step(ema, spot, _a(b0 + i))
    cf = ema_forecast(e0, spot, b0, dn, ALPHA, H)
    assert abs(cf - ema) / ema < Decimal("1e-4")         # continuous approximation of prod(1 - a_i): section 10.1
    assert ema_forecast(e0, Decimal(3), b0, dn, ALPHA, H) == ema_forecast(e0, Decimal(1), b0, dn, ALPHA, H)  # spot cap 1
    assert ema_forecast(e0, spot, b0, 0, ALPHA, H) == e0
    assert ema_step(Decimal("0.5"), Decimal(2), Decimal("0.1")) == Decimal("0.55")


def test_project_ema(make_subnet, make_globals, make_pool) -> None:
    glob = make_globals()
    s = make_subnet(7, reg_at=8_000_000, moving_price=Decimal("0.002"), pool=make_pool(600 * 10**9, 500_000 * 10**9))
    blk = Block(9_000_000)
    b0 = blocks_since_start(s, blk)
    assert b0 is not None
    got = project_ema(glob, s, blk, 1_000)
    assert got == ema_forecast(s.moving_price, s.pool.spot(), b0, 1_000, glob.moving_alpha, s.ema_halving_blocks)
    assert s.pool.spot() < got < s.moving_price                               # falls toward a spot of 0.0012
    assert project_ema(glob, s, blk, 1_000, Decimal(0)) < got
    assert project_ema(glob, replace(s, reg_allowed=False), blk, 1_000) == s.moving_price   # frozen
    assert project_ema(glob, s, blk, 0) == s.moving_price


# ------------------------------------------------------------------------------------------------- t*
def test_t_star_worst_case_ladder_ranks_2_to_4(gsnap, dec) -> None:
    """Brief 4.4 at 9,240,388: ranks 2-4 at 1.255x / 1.508x / 1.53x of the bottom EMA -> 2.8 h / 4.8 h / 4.9-5.0 h
    with spot -> 0 (each subnet's own a); ranks 4-15 at 1.53-1.70x -> 4.9-6.3 h."""
    snap = _eligible(dec.build_snapshot(gsnap("sn92_9240388", 0)))
    lad = ladder(snap)
    assert int(lad[0].netuid) == 92
    bottom = snap.get(lad[0])
    assert bottom is not None
    rows = []
    for k in lad[1:15]:
        s = snap.get(k)
        assert s is not None
        a = ema_alpha(snap.glob, s, snap.block)
        t = t_star_blocks(s.moving_price, bottom.moving_price, Decimal(0), a)
        assert t is not None
        rows.append((s.moving_price / bottom.moving_price, _hours(t)))
    (r2, h2), (r3, h3), (r4, h4) = rows[:3]
    # the brief's ratios were read a few blocks apart from the fixture block: 1.255 / 1.508 vs 1.2534 / 1.5085 here
    assert abs(r2 - Decimal("1.255")) <= Decimal("0.002") and round(h2, 1) == Decimal("2.8")
    assert abs(r3 - Decimal("1.508")) <= Decimal("0.002") and round(h3, 1) == Decimal("4.8")
    assert round(r4, 2) == Decimal("1.52") and Decimal("4.85") <= h4 <= Decimal("5.05")
    assert all(Decimal("1.52") <= r <= Decimal("1.70") for r, _ in rows[2:])
    assert all(Decimal("4.85") <= h <= Decimal("6.35") for _, h in rows[2:])


def test_t_star_is_half_life_times_log2_ratio_at_zero_spot() -> None:
    a = _a(4_011_706)
    hl = half_life_blocks(a)
    assert hl is not None
    for ratio in (Decimal("1.255"), Decimal("1.508"), Decimal("2")):
        t = t_star_blocks(ratio * Decimal("0.001"), Decimal("0.001"), Decimal(0), a)
        assert t is not None
        assert abs(Decimal(t) - hl * DEC.ln(ratio) / DEC.ln(Decimal(2))) <= 1


@pytest.mark.parametrize(("ratio", "spot_frac"), [("1.255", "0"), ("1.508", "0.5"), ("1.05", "0.9"), ("1.3", "0.2")])
def test_t_star_matches_brute_force_ema_stepping(ratio: str, spot_frac: str) -> None:
    """Closed form = brute-force stepping of the chain recursion with k's spot held at s and the bottom EMA flat."""
    e1 = Decimal("0.0013565")
    ek = e1 * Decimal(ratio)
    s = ek * Decimal(spot_frac)
    a = _a(1_849_481)
    t = t_star_blocks(ek, e1, s, a)
    assert t is not None
    ema, n = ek, 0
    while ema > e1:
        ema = ema_step(ema, s, a)
        n += 1
    assert t == n


def test_t_star_edge_cases() -> None:
    a = _a(1_000_000)
    assert t_star_blocks(Decimal("0.001"), Decimal("0.002"), Decimal(0), a) == 0         # already at/below
    assert t_star_blocks(Decimal("0.002"), Decimal("0.002"), Decimal(0), a) == 0
    assert t_star_blocks(Decimal("0.003"), Decimal("0.002"), Decimal("0.002"), a) is None   # spot >= E_1: never
    assert t_star_blocks(Decimal("0.003"), Decimal("0.002"), Decimal("0.0025"), a) is None
    assert t_star_blocks(Decimal("0.003"), Decimal("0.002"), Decimal(0), Decimal(0)) is None   # frozen EMA
    assert t_star_blocks(Decimal("1.2"), Decimal("0.9"), Decimal(5), a) is None          # spot capped at 1 > E_1


TIER_A_COVERAGE = [  # (U + M_A, gap covered at a 50% spot crash, gap covered at spot -> 0); a = 0.000286 (section 3.3)
    (165, "2.4", "4.8"), (315, "4.5", "9.4"), (615, "8.8", "19.2"), (1_215, "17.2", "41.6"),
]


def _max_gap(horizon: int, crash_to: Decimal, a: Decimal) -> Decimal:
    """Largest EMA gap g (E_k = (1+g)*E_1, spot_k = EMA_k before the crash) that t* still clears within horizon."""
    lo, hi = Decimal(0), Decimal(1)
    e1 = Decimal("0.001")
    for _ in range(60):
        mid = (lo + hi) / 2
        ek = e1 * (1 + mid)
        t = t_star_blocks(ek, e1, ek * crash_to, a)
        if t is not None and t <= horizon:
            lo = mid
        else:
            hi = mid
    return lo


@pytest.mark.parametrize(("horizon", "crash", "zero"), TIER_A_COVERAGE)
def test_tier_a_coverage_table(horizon: int, crash: str, zero: str) -> None:
    a = Decimal("0.000286")
    assert round(_max_gap(horizon, Decimal("0.5"), a) * 100, 1) == Decimal(crash)
    assert round(_max_gap(horizon, Decimal(0), a) * 100, 1) == Decimal(zero)


def test_t_star_against_float_reference() -> None:
    """Sanity: the Decimal closed form equals the float formula to the block."""
    e1, ek, s, a = 0.0013565, 0.0013565 * 1.508, 0.0, 0.0002377
    ref = math.ceil(math.log((ek - s) / (e1 - s)) / -math.log(1 - a))
    got = t_star_blocks(Decimal(repr(ek)), Decimal(repr(e1)), Decimal(0), Decimal(repr(a)))
    assert got == ref
