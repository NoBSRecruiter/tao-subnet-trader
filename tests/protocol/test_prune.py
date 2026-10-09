"""protocol.prune: ladder and live target (golden), immunity calendar, registration cost and cost ratio vectors, the
section 3.3 hazard model (reference P(reg within 24 h)), the as-of refit, recovery ratios and the moving-bottom grid."""
from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core.fixed import DEC
from taotrader.core.units import RAO_PER_TAO, AlphaRao, Block, NetUid, Rao, SubnetKey
from taotrader.protocol.ema import ema_alpha, t_star_blocks
from taotrader.protocol.prune import (
    HazardModel,
    RegistrationRow,
    apply_hot_market,
    blocks_to_ratio,
    cost_ratio,
    default_lambda_floor,
    first_floor_block,
    fit_hazard,
    hazard_from_table,
    immunity_end,
    interp_cdf,
    is_immune,
    ladder,
    lock_reduction_interval_eff,
    model_cdf,
    p_registration,
    prune_possible,
    prune_rank,
    prune_target,
    recovery_ratio,
    registration_cost,
    staker_base,
    time_to_target,
    window_open_block,
)

TAO = RAO_PER_TAO
CDF_R = [Decimal(x) for x in ("1.75", "1.462", "1.253", "1.132", "1.045", "0.958", "0.872", "0.767", "0.266")]
CDF_F = [Decimal(x) for x in ("0.0625", "0.09", "0.19", "0.31", "0.50", "0.72", "0.84", "0.94", "1.0")]


def _frozen_model(**kw) -> HazardModel:
    """The preregistered section 3.3 table (config/preregistration.toml [prune.hazard])."""
    return hazard_from_table(CDF_R, CDF_F, 32, n0=4, rate_limit_blocks=14_400, i_eff_blocks=57_600,
                             prior_scale_blocks=43_200, **kw)


def _runtime_target(gs, dec) -> int | None:
    raw = gs.runtime("SubnetInfoRuntimeApi_get_subnet_to_prune")
    b = bytes.fromhex(raw[2:])
    return int.from_bytes(b[1:3], "little") if b[0] == 1 else None


# ------------------------------------------------------------------------------------------------- ladder (golden)
@pytest.mark.parametrize("name", ["sn92_9240388", "globals_9240878"])
def test_live_prune_target_is_sn92(gsnap, dec, name: str) -> None:
    """Brief 4.3/4.4: the local rule picks SN92, as SubnetInfoRuntimeApi_get_subnet_to_prune does; ranks 2-3 are SN47
    and SN72."""
    gs = gsnap(name, 0)
    snap = dec.build_snapshot(gs)
    lad = ladder(snap)
    assert [int(k.netuid) for k in lad[:3]] == [92, 47, 72]
    assert _runtime_target(gs, dec) == 92
    target = prune_target(snap)
    assert target is not None and int(target.netuid) == 92
    assert prune_rank(snap, lad[1]) == 2
    assert all(int(k.netuid) != 0 for k in lad)
    s92 = snap.get(lad[0])
    assert s92 is not None and abs(s92.moving_price / Decimal("0.0013565") - 1) < Decimal("0.002")   # brief: 0.0013565


IMMUNITY_CALENDAR = {16: 9_324_646, 99: 9_436_056, 86: 9_557_284, 103: 9_626_380, 70: 9_689_571, 59: 9_802_771,
                     82: 10_019_260}


def test_immunity_calendar_from_globals(gsnap, dec) -> None:
    """Brief 4.9 (from 9,240,878): immunity runs from NetworkRegisteredAt for NetworkImmunityPeriod blocks."""
    snap = dec.build_snapshot(gsnap("globals_9240878", 0))
    assert snap.glob.immunity_period == 864_000
    lad = set(ladder(snap))
    for netuid, end in IMMUNITY_CALENDAR.items():
        s = snap.by_netuid(netuid)
        assert s is not None
        assert immunity_end(s, snap.glob) == end
        assert is_immune(s, snap.glob, snap.block) and s.key not in lad
        assert prune_rank(snap, s.key) is None
    s86 = snap.by_netuid(86)
    assert s86 is not None and s86.moving_price == 0 and s86.first_emission_block is None   # never started: EMA 0


def test_ladder_order_ties_and_immunity(make_subnet, make_snapshot) -> None:
    blk = 9_000_000
    subs = [
        make_subnet(1, reg_at=5_000_000, moving_price=Decimal("0.002")),
        make_subnet(2, reg_at=6_000_000, moving_price=Decimal(0)),               # EMA-0 tie: the older wins
        make_subnet(3, reg_at=5_500_000, moving_price=Decimal(0)),
        make_subnet(4, reg_at=blk - 864_000 + 1, moving_price=Decimal(0)),      # immune for one more block
        make_subnet(5, reg_at=blk - 864_000, moving_price=Decimal("0.001")),    # immunity ends exactly at blk
    ]
    snap = make_snapshot(blk, subs)
    assert [int(k.netuid) for k in ladder(snap)] == [3, 2, 5, 1]
    later = make_snapshot(blk + 1, subs)
    assert [int(k.netuid) for k in ladder(later)] == [3, 2, 4, 5, 1]
    assert prune_target(make_snapshot(blk, [subs[3]])) is None


def test_prune_possible(make_globals) -> None:
    assert prune_possible(make_globals())                                       # 128 + 0 >= 128
    assert not prune_possible(make_globals(n_nonroot_networks=127))
    assert prune_possible(make_globals(n_nonroot_networks=127, cleanup_queue_len=1))


# ------------------------------------------------------------------------------------------------- registration cost
def test_registration_cost_matches_runtime_api(gsnap, dec) -> None:
    """962.89 TAO at 9,240,878 (L = 653.02, last 9,210,610, I_eff 57,600), equal to the runtime API to the rao; the
    same at the SN92 fixture block."""
    for name, block_tao in (("globals_9240878", "962.89"), ("sn92_9240388", "968.44")):
        gs = gsnap(name, 0)
        glob = dec.build_globals(gs)
        runtime = dec.le(gs.runtime("SubnetRegistrationRuntimeApi_get_network_registration_cost"))
        assert registration_cost(glob, Block(gs.block)) == runtime
        assert round(Decimal(runtime) / TAO, 2) == Decimal(block_tao)
    glob = dec.build_globals(gsnap("globals_9240878", 0))
    assert glob.last_reg_block == 9_210_610 and round(Decimal(glob.last_lock_cost) / TAO, 2) == Decimal("653.02")
    assert lock_reduction_interval_eff(glob) == 57_600
    # brief 4.1 live values along the same line: 969.38 (~9,240,300) -> 965.70 (~9,240,630); the cost falls
    # L // 57,600 = 0.0113 TAO per block, so "~block" allows ~9 blocks (0.1 TAO)
    assert abs(Decimal(registration_cost(glob, Block(9_240_300))) / TAO - Decimal("969.38")) < Decimal("0.1")
    assert abs(Decimal(registration_cost(glob, Block(9_240_630))) / TAO - Decimal("965.70")) < Decimal("0.1")


def test_registration_cost_of_the_8618670_registration(make_globals) -> None:
    """1,003.01 TAO for the registration at 8,618,670: L = 842.35, previous registration 8,572,056 (delta 46,614)."""
    glob = make_globals(last_lock_cost=Rao(842_350_000_000), last_reg_block=Block(8_572_056))
    assert round(Decimal(registration_cost(glob, Block(8_618_670))) / TAO, 2) == Decimal("1003.01")


def test_registration_cost_shape(make_globals) -> None:
    glob = make_globals(last_reg_block=Block(9_000_000))
    L = glob.last_lock_cost
    opening = window_open_block(glob)
    assert opening == 9_014_400
    assert abs(cost_ratio(glob, opening) - Decimal("1.75")) < Decimal("1e-6")      # 1.75 L when the rate limit expires
    assert abs(cost_ratio(glob, Block(9_057_600)) - 1) < Decimal("1e-6")           # back to L after 8 days
    assert registration_cost(glob, Block(9_115_200)) == glob.min_lock_cost        # 1-TAO floor after ~16 days
    assert registration_cost(glob, Block(9_000_000)) == 2 * L
    assert registration_cost(glob, Block(8_999_000)) == 2 * L                     # elapsed clamps at 0
    first = make_globals(last_reg_block=Block(0))
    assert registration_cost(first, Block(5)) == first.last_lock_cost - (first.last_lock_cost // 57_600) * 5   # mult 1
    assert cost_ratio(make_globals(last_lock_cost=Rao(0)), Block(1)) == 0


@pytest.mark.parametrize(("delta", "r"), [(31_000, "1.462"), (43_000, "1.253"), (50_000, "1.132"), (55_000, "1.045"),
                                          (65_000, "0.872")])
def test_cost_ratio_points(make_globals, delta: int, r: str) -> None:
    """r = 2 - delta/57,600 (section 10.1 cost ratio -> CDF)."""
    glob = make_globals(last_reg_block=Block(9_000_000))
    assert round(cost_ratio(glob, Block(9_000_000 + delta)), 3) == Decimal(r)
    d = blocks_to_ratio(glob, Decimal(r))
    assert d is not None and abs(d - delta) <= 60
    assert cost_ratio(glob, Block(9_000_000 + d)) <= Decimal(r) < cost_ratio(glob, Block(9_000_000 + d - 1))


def test_blocks_to_ratio_and_floor_block(make_globals) -> None:
    glob = make_globals(last_reg_block=Block(9_000_000))
    assert blocks_to_ratio(glob, Decimal(2)) == 0
    assert blocks_to_ratio(glob, Decimal("0.0001")) is None                       # the 1-TAO floor is above it
    tf = first_floor_block(glob)
    assert tf is not None
    assert cost_ratio(glob, tf) <= Decimal("1.045") < cost_ratio(glob, Block(tf - 1))
    assert abs((tf - 9_000_000) - 55_008) <= 2


# ------------------------------------------------------------------------------------------------- hazard model
def test_frozen_table_model_points() -> None:
    m = _frozen_model()
    assert m.valid and m.n0 == 4 and m.lambda_floor_per_block == default_lambda_floor()
    assert abs(m.p_open - DEC.divide(Decimal(2), Decimal(36))) < Decimal("1e-55")   # the table's 0.0625 smoothed: 0.0556
    pts = dict(m.cdf_by_r)
    assert set(CDF_R) <= set(pts)
    rs = [r for r, _ in m.cdf_by_r]
    assert rs == sorted(rs, reverse=True)
    fs = [f for _, f in m.cdf_by_r]
    assert fs == sorted(fs) and fs[-1] <= 1
    # F = (32*F_emp + 4*F_prior)/36 at a table point: r = 1.045 <-> delta = 55,008, F_prior = 1 - exp(-40,608/43,200)
    prior = DEC.subtract(Decimal(1), DEC.exp(DEC.minus(DEC.divide(Decimal(40_608), Decimal(43_200)))))
    want = DEC.divide(DEC.add(Decimal(16), DEC.multiply(Decimal(4), prior)), Decimal(36))
    assert abs(pts[Decimal("1.045")] - want) < Decimal("1e-55")
    assert interp_cdf(m.cdf_by_r, Decimal(3)) == m.cdf_by_r[0][1]
    assert interp_cdf(m.cdf_by_r, Decimal(0)) == m.cdf_by_r[-1][1]


REFERENCE_P24 = [(14_399, "0.083"), (31_000, "0.073"), (43_000, "0.157"), (50_000, "0.396"), (55_000, "0.51"),
                 (65_000, "0.55")]


@pytest.mark.parametrize(("delta", "p"), REFERENCE_P24)
def test_reference_p_registration_within_24h(make_globals, delta: int, p: str) -> None:
    """Section 3.3 reference P(reg within 24 h): 8.3% at opening, 7.3% at delta 31k, 15.7% at 43k, 39.6% at 50k,
    51% at 55k, 55% at 65k. The printed references exclude the tail floor (reproduced to < 0.6 pp with the floor off);
    the floor (hazard >= ln2/7,200 once r <= 1.045) can only raise them."""
    glob = make_globals(last_reg_block=Block(9_000_000))
    blk = Block(9_000_000 + delta)
    no_floor = p_registration(glob, _frozen_model(lambda_floor_per_block=Decimal(0)), blk, 7_200)
    assert abs(no_floor - Decimal(p)) < Decimal("0.006")
    floored = p_registration(glob, _frozen_model(), blk, 7_200)
    assert floored >= no_floor
    if delta >= 55_008:                                                          # whole day inside the floor region
        assert floored >= 1 - DEC.exp(-default_lambda_floor() * 7_200) - Decimal("1e-30")


def test_p_registration_zero_inside_rate_limit_and_without_prune(make_globals) -> None:
    glob = make_globals(last_reg_block=Block(9_000_000))
    m = _frozen_model()
    assert p_registration(glob, m, Block(9_000_000), 14_399) == 0                # window opens at +14,400
    assert abs(p_registration(glob, m, Block(9_000_000), 14_400) - m.p_open) < Decimal("1e-55")   # the opening mass
    assert p_registration(glob, m, Block(9_010_000), 0) == 0
    assert p_registration(replace(glob, n_nonroot_networks=100), m, Block(9_050_000), 7_200) == 0
    p1 = p_registration(glob, m, Block(9_050_000), 3_600)
    p2 = p_registration(glob, m, Block(9_050_000), 7_200)
    assert 0 < p1 < p2 < 1


def test_invalid_model_uses_window_rule_and_constant_floor(make_globals) -> None:
    glob = make_globals(last_reg_block=Block(9_000_000))
    m = replace(_frozen_model(), valid=False)
    assert p_registration(glob, m, Block(9_000_000), 7_000) == 0
    half = p_registration(glob, m, Block(9_014_399), 7_200)                       # one day from the opening block
    assert abs(half - Decimal("0.5")) < Decimal("1e-30")
    assert abs(p_registration(glob, m, Block(9_100_000), 7_200) - Decimal("0.5")) < Decimal("1e-30")


def test_hot_market_raises_the_opening_mass(make_globals) -> None:
    glob = make_globals(last_reg_block=Block(9_000_000))
    base = _frozen_model()
    hot = apply_hot_market(base, [Decimal("1.2"), Decimal("1.41"), Decimal("1.5")])
    assert hot.p_open == Decimal("0.5")
    assert model_cdf(hot, Decimal("1.75")) == Decimal("0.5")
    assert p_registration(glob, hot, Block(9_014_399), 7_200) > Decimal("0.5")
    cold = apply_hot_market(hot, [Decimal("1.5"), Decimal("1.39")])
    assert cold.p_open == base.p_open
    assert apply_hot_market(base, [Decimal("1.5")]).p_open == base.p_open        # needs the last 2


def _rows(n: int, start: int = 7_000_000, seed: int = 7) -> list[RegistrationRow]:
    rng = random.Random(seed)
    out, blk = [], start
    for _ in range(n):
        gap = rng.randint(14_401, 90_000)
        blk += gap
        out.append(RegistrationRow(queued_block=Block(blk), victim_netuid=NetUid(rng.randint(1, 128)),
                                   cost_ratio=Decimal(2) - Decimal(gap) / Decimal(57_600), blocks_since_prev=gap))
    return out


def test_fit_hazard_uses_only_rows_before_asof() -> None:
    prior = _frozen_model()
    rows = _rows(20)
    asof = Block(rows[11].queued_block)                                           # rows 0..10 are before asof
    fitted = fit_hazard(rows, asof, prior)
    perturbed = rows[:11] + [replace(r, cost_ratio=Decimal("0.3")) for r in rows[11:]] + _rows(5, start=rows[-1].queued_block)
    assert fit_hazard(perturbed, asof, prior) == fitted                          # the future cannot leak in
    assert fit_hazard(rows[:11], asof, prior) == fitted                          # deleting it changes nothing either
    assert fit_hazard(list(reversed(rows)), asof, prior) == fitted               # order-independent, deterministic
    assert fitted != prior and fitted.valid


def test_fit_hazard_needs_eight_rows_and_smooths_toward_the_prior() -> None:
    prior = _frozen_model()
    rows = _rows(12)
    assert fit_hazard(rows[:7], Block(rows[-1].queued_block + 1), prior) is prior
    fitted = fit_hazard(rows, Block(rows[-1].queued_block + 1), prior)
    n, n0 = 12, prior.n0
    ratios = [r.cost_ratio for r in rows]
    for r in ratios:
        emp = DEC.divide(Decimal(sum(1 for x in ratios if x >= r)), Decimal(n))
        want = DEC.divide(DEC.add(DEC.multiply(Decimal(n), emp), DEC.multiply(Decimal(n0), interp_cdf(prior.cdf_by_r, r))),
                          Decimal(n + n0))
        got = dict(fitted.cdf_by_r)[r]
        assert got >= DEC.subtract(want, Decimal("1e-50"))                       # monotone repair can only raise it
    fs = [f for _, f in fitted.cdf_by_r]
    assert fs == sorted(fs)
    hot_rows = rows[:10] + [replace(rows[10], cost_ratio=Decimal("1.5")), replace(rows[11], cost_ratio=Decimal("1.45"))]
    assert fit_hazard(hot_rows, Block(rows[-1].queued_block + 1), prior).p_open == Decimal("0.5")


# ------------------------------------------------------------------------------------------------- recovery
def test_recovery_ratio_sn92_golden(gsnap, dec) -> None:
    """SN92 at 9,240,388 with the brief's escrow E = 49,063 alpha: ~0.368 (0.41 before the basket sale)."""
    gs = gsnap("sn92_9240388", 0)
    glob = dec.build_globals(gs)
    s = dec.build_subnet(gs, 92, glob)
    assert s.key.reg_at > glob.tao_in_refund_block                                  # new-rule subnet (protocol term)
    assert s.total_alpha_staked is not None and s.total_alpha_staked < s.alpha_out - s.protocol_alpha
    assert staker_base(s) == s.alpha_out - s.protocol_alpha                         # fail-closed: the larger base
    after = recovery_ratio(replace(s, escrow_alpha=AlphaRao(49_063 * TAO)), glob, Decimal("0.35"))
    before = recovery_ratio(replace(s, escrow_alpha=None), glob, Decimal("0.35"))
    assert abs(after - Decimal("0.368")) < Decimal("0.003")
    assert abs(before - Decimal("0.41")) < Decimal("0.005")
    # brief 4.5 cross-check: SubnetTAO / (AlphaOut + AlphaIn) / spot before basket sales
    approx = DEC.divide(DEC.divide(Decimal(s.pool.tao), Decimal(s.alpha_out + s.pool.alpha)), s.pool.spot())
    assert abs(before - approx) < Decimal("1e-50")


def test_recovery_ratio_sn47_legacy(make_subnet, make_globals, make_pool) -> None:
    """Brief 4.5 SN47 (legacy, registered before TaoInRefundDeploymentBlock): 983.9 TAO pot, AlphaOut 1,576,160,
    spot 0.0017, escrow 25,516 alpha -> ~0.36."""
    glob = make_globals()
    x = 983_900_000_000 * 10_000 // 17                                             # alpha reserve at spot 0.0017
    s = make_subnet(47, reg_at=7_340_400, pool=make_pool(983_900_000_000, x), alpha_out=AlphaRao(1_576_160 * TAO),
                    protocol_alpha=AlphaRao(40_000 * TAO), total_alpha_staked=None, escrow_alpha=AlphaRao(25_516 * TAO))
    r = recovery_ratio(s, glob, Decimal("0.35"))
    assert abs(r - Decimal("0.36")) < Decimal("0.005")
    assert abs(recovery_ratio(replace(s, escrow_alpha=None), glob, Decimal("0.35")) - Decimal("0.367")) < Decimal("0.002")


def test_recovery_ratio_rules(make_subnet, make_globals, make_pool) -> None:
    glob = make_globals()
    s = make_subnet(9, reg_at=9_000_000, pool=make_pool(600 * TAO, 450_000 * TAO), alpha_out=AlphaRao(600_000 * TAO),
                    protocol_alpha=AlphaRao(40_000 * TAO), total_alpha_staked=AlphaRao(300_000 * TAO))
    new_rule = recovery_ratio(s, glob, Decimal("0.35"))
    legacy = recovery_ratio(replace(s, key=SubnetKey(NetUid(9), Block(8_000_000))), glob, Decimal("0.35"))
    assert legacy > new_rule                                                          # no AlphaIn + E term in the denominator
    assert recovery_ratio(replace(s, pool=make_pool(0, 450_000 * TAO)), glob, Decimal("0.35")) == 0   # empty pot
    assert recovery_ratio(replace(s, pool=make_pool(600 * TAO, 0)), glob, Decimal("0.35")) == Decimal("0.35")
    tiny = replace(s, alpha_out=AlphaRao(1), protocol_alpha=AlphaRao(0), total_alpha_staked=AlphaRao(1),
                   key=SubnetKey(NetUid(9), Block(8_000_000)))
    assert recovery_ratio(tiny, glob, Decimal("0.35")) == 1                           # clamped to [0, 1]
    # staker base: the larger of TotalAlphaStaked and AlphaOut - ProtocolAlpha
    assert staker_base(s) == 560_000 * TAO
    assert staker_base(replace(s, total_alpha_staked=AlphaRao(700_000 * TAO))) == 700_000 * TAO
    assert staker_base(replace(s, total_alpha_staked=None)) == 560_000 * TAO
    # reservoirs are folded into the reserves (dissolution phase 0)
    assert recovery_ratio(replace(s, reservoir_tao=Rao(60 * TAO)), glob, Decimal("0.35")) > new_rule


# ------------------------------------------------------------------------------------------------- moving-bottom grid
def _flat(make_subnet, make_pool, netuid: int, ema: str, reg_at: int = 4_000_000, spot: str | None = None, **kw):
    """A mature subnet whose pool spot equals `spot` (default: its EMA) exactly."""
    price = Decimal(spot if spot is not None else ema)
    tao = 600 * TAO
    alpha = int(Decimal(tao) / price)
    return make_subnet(netuid, reg_at=reg_at, moving_price=Decimal(ema), pool=make_pool(tao, alpha),
                       first_emission_block=Block(reg_at + 100), **kw)


def test_time_to_target_matches_t_star_on_a_flat_bottom(make_subnet, make_snapshot, make_pool) -> None:
    bottom = _flat(make_subnet, make_pool, 1, "0.001")
    k = _flat(make_subnet, make_pool, 2, "0.0011")
    other = _flat(make_subnet, make_pool, 3, "0.003")
    snap = make_snapshot(9_000_000, [bottom, k, other])
    a = ema_alpha(snap.glob, k, snap.block)
    t_star = t_star_blocks(k.moving_price, bottom.moving_price, Decimal(0), a)
    assert t_star is not None and 300 < t_star < 400
    got = time_to_target(snap, k.key, Decimal(0), 2_000)
    assert got is not None and t_star - 2 <= got <= t_star + 1     # exact block (closed form vs discrete), not the grid
    assert time_to_target(snap, bottom.key, Decimal("0.001"), 2_000) == 0         # already the target
    assert time_to_target(snap, k.key, Decimal("0.0012"), 5_000) is None          # spot above the bottom: never
    assert time_to_target(snap, k.key, Decimal(0), 300) is None                   # beyond the horizon
    assert time_to_target(snap, SubnetKey(NetUid(9), Block(1)), Decimal(0), 2_000) is None


def test_time_to_target_with_a_falling_bottom_takes_longer(make_subnet, make_snapshot, make_pool) -> None:
    k = _flat(make_subnet, make_pool, 2, "0.0011")
    flat = make_snapshot(9_000_000, [_flat(make_subnet, make_pool, 1, "0.001"), k])
    falling = make_snapshot(9_000_000, [_flat(make_subnet, make_pool, 1, "0.001", spot="0.0007"), k])
    t_flat = time_to_target(flat, k.key, Decimal(0), 3_000)
    t_fall = time_to_target(falling, k.key, Decimal(0), 3_000)
    assert t_flat is not None and t_fall is not None and t_fall > t_flat


def test_time_to_target_admits_subnets_at_immunity_expiry(make_subnet, make_snapshot, make_pool) -> None:
    blk = 9_000_000
    bottom = _flat(make_subnet, make_pool, 1, "0.001")
    k = _flat(make_subnet, make_pool, 2, "0.0011")
    young = _flat(make_subnet, make_pool, 3, "0.0005", reg_at=blk - 864_000 + 100)   # admitted at blk + 100
    snap = make_snapshot(blk, [bottom, k, young])
    assert young.key not in ladder(snap)
    assert time_to_target(snap, k.key, Decimal(0), 2_000) is None                  # young is the target from blk+100
    assert time_to_target(snap, young.key, Decimal("0.0005"), 2_000) == 100
    late_k = _flat(make_subnet, make_pool, 4, "0.0002", reg_at=blk - 864_000 + 500)  # lowest EMA, immune until +500
    snap2 = make_snapshot(blk, [bottom, late_k])
    assert time_to_target(snap2, late_k.key, Decimal("0.0002"), 2_000) == 500
    assert time_to_target(snap2, late_k.key, Decimal("0.0002"), 400) is None       # still immune at the horizon
    with pytest.raises(ValueError):
        time_to_target(snap2, late_k.key, Decimal(0), 100, step_blocks=0)


# ------------------------------------------------------------------------------------------------- review regressions
def _first_target_block(snap, key, spot_k: Decimal, horizon: int) -> int | None:
    """Brute force: the first offset t <= horizon at which `key` is ladder()[0] under the flat-spot EMA paths."""
    from taotrader.protocol.ema import project_ema
    from taotrader.protocol.prune import _pool_spot

    glob = snap.glob
    sk = snap.get(key)
    for t in range(horizon + 1):
        blk = snap.block + t
        if blk < immunity_end(sk, glob):
            continue
        mine = (project_ema(glob, sk, snap.block, t, spot_k), sk.key.reg_at, sk.key.netuid)
        if not any(blk >= immunity_end(s, glob)
                   and (project_ema(glob, s, snap.block, t, _pool_spot(s)), s.key.reg_at, s.key.netuid) < mine
                   for s in snap.subnets if s.key != key):
            return t
    return None


def test_time_to_target_is_the_first_block_not_the_next_grid_point(make_subnet, make_snapshot, make_pool) -> None:
    """A Tier A trigger compares t* with U + M_A: rounding the crossing UP to the 10-block grid delays the exit."""
    bottom = _flat(make_subnet, make_pool, 1, "0.001")
    k = _flat(make_subnet, make_pool, 2, "0.0011")
    snap = make_snapshot(9_000_000, [bottom, k, _flat(make_subnet, make_pool, 3, "0.003")])
    exact = _first_target_block(snap, k.key, Decimal(0), 2_000)
    assert exact is not None and exact % 10 != 0
    assert time_to_target(snap, k.key, Decimal(0), 2_000) == exact


def test_time_to_target_checks_the_horizon_block_itself(make_subnet, make_snapshot, make_pool) -> None:
    """Horizon = U + M_A = 315 is not on the grid: a crossing at 311..315 must still be reported (not None)."""
    bottom = _flat(make_subnet, make_pool, 1, "0.001")
    k = _flat(make_subnet, make_pool, 2, "0.0011")
    snap = make_snapshot(9_000_000, [bottom, k])
    exact = _first_target_block(snap, k.key, Decimal(0), 2_000)
    assert exact is not None
    assert time_to_target(snap, k.key, Decimal(0), exact) == exact
    assert time_to_target(snap, k.key, Decimal(0), exact - 1) is None


def test_time_to_target_finds_a_target_window_between_grid_points(make_subnet, make_snapshot, make_pool) -> None:
    """k leaves immunity at +303 as the lowest EMA; a lower rival is admitted at +308. k is the target for blocks
    303..307 only, a window that falls between the grid points 300 and 310."""
    blk = 9_000_000
    bottom = _flat(make_subnet, make_pool, 1, "0.001")
    k = _flat(make_subnet, make_pool, 2, "0.0005", reg_at=blk - 864_000 + 303)
    rival = _flat(make_subnet, make_pool, 3, "0.0002", reg_at=blk - 864_000 + 308)
    snap = make_snapshot(blk, [bottom, k, rival])
    assert _first_target_block(snap, k.key, Decimal("0.0005"), 2_000) == 303
    assert time_to_target(snap, k.key, Decimal("0.0005"), 2_000) == 303


def _brute_force_floored_p(glob, model: HazardModel, t0: int, horizon: int) -> Decimal:
    """Section 3.3 rule taken literally: per-block survival min(S(t+1)/S(t), exp(-lambda)) for every block step whose
    end has r <= 1.045, the model's own S(t+1)/S(t) before that."""
    from taotrader.protocol.prune import _cdf_at_block

    tf = first_floor_block(glob)
    floor_step = DEC.exp(DEC.minus(model.lambda_floor_per_block))
    surv = [DEC.subtract(Decimal(1), _cdf_at_block(glob, model, t)) for t in range(t0, t0 + horizon + 1)]
    total = Decimal(1)
    for i in range(horizon):
        step = DEC.divide(surv[i + 1], surv[i]) if surv[i] > 0 else Decimal(0)
        if tf is not None and t0 + i + 1 >= tf:
            step = min(step, floor_step)
        total = DEC.multiply(total, step)
    return DEC.subtract(Decimal(1), total)


@pytest.mark.parametrize(("delta", "horizon"), [(53_000, 7_200), (55_000, 300), (60_000, 1_000), (64_500, 7_200),
                                                (70_500, 1_000), (72_000, 7_200), (80_000, 2_000)])
def test_tail_floor_is_applied_per_block(make_globals, delta: int, horizon: int) -> None:
    """The floor is a per-block rule. Applying it to the model's survival over 300-block chunks lets a chunk where the
    model hazard is above the floor mask a stretch where it is below, which understates P(reg) (up to 0.4 pp)."""
    glob = make_globals(last_reg_block=Block(9_000_000))
    m = _frozen_model()
    t0 = 9_000_000 + delta
    want = _brute_force_floored_p(glob, m, t0, horizon)
    assert abs(p_registration(glob, m, Block(t0), horizon) - want) < Decimal("1e-12")


def test_tail_floor_is_path_independent(make_globals) -> None:
    """P(t0, t2) = 1 - (1 - P(t0, t1)) * (1 - P(t1, t2)): the floored hazard does not depend on the query start."""
    glob = make_globals(last_reg_block=Block(9_000_000))
    m = _frozen_model()
    for d0, h1, h2 in [(60_000, 7, 300), (56_123, 301, 777), (70_000, 13, 7_200)]:
        t0 = Block(9_000_000 + d0)
        p01 = p_registration(glob, m, t0, h1)
        p12 = p_registration(glob, m, Block(t0 + h1), h2)
        p02 = p_registration(glob, m, t0, h1 + h2)
        chained = DEC.subtract(Decimal(1), DEC.multiply(DEC.subtract(Decimal(1), p01), DEC.subtract(Decimal(1), p12)))
        assert abs(chained - p02) < Decimal("1e-12")


@pytest.mark.parametrize("case", ["hot_market", "min_lock_kink", "floor_from_opening", "fitted"])
def test_tail_floor_per_block_on_edge_shapes(make_globals, case: str) -> None:
    """The exact per-block floor relies on S(t) being linear between knots (CDF points, the min-lock kink, the window
    opening). Check it against brute force where each kind of knot falls inside the horizon."""
    glob = make_globals(last_reg_block=Block(9_000_000))
    m = _frozen_model()
    starts = [53_000, 61_000, 75_000]
    if case == "hot_market":
        m = apply_hot_market(m, [Decimal("1.5"), Decimal("1.6")])
    elif case == "min_lock_kink":
        glob = replace(glob, last_lock_cost=Rao(3 * glob.min_lock_cost))        # cost floors at r = 1/3 within ~95k
        starts = [90_000, 93_500]
    elif case == "floor_from_opening":
        glob = replace(glob, network_rate_limit=60_000)                          # r(opening) < 1.045
        starts = [59_000, 59_999, 64_000]
    else:
        m = fit_hazard(_rows(15), Block(_rows(15)[-1].queued_block + 1), m)
    for d0 in starts:
        for horizon in (1, 299, 3_000):
            t0 = 9_000_000 + d0
            want = _brute_force_floored_p(glob, m, t0, horizon)
            assert abs(p_registration(glob, m, Block(t0), horizon) - want) < Decimal("1e-12"), (case, d0, horizon)
