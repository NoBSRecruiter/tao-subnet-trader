"""risk.hazard_mc: seeding, determinism under a fixed seed and block hash, and model sanity (DESIGN.md 3.3 Tier B)."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from types import ModuleType

import pytest

from taotrader.core.units import Block
from taotrader.protocol.prune import hazard_from_table
from taotrader.risk.hazard_mc import McParams, mc_seed, realised_sigma_3d, registration_schedule, run_mc, sigma_inputs

B = 9_240_388
CDF_R = [Decimal(x) for x in ("1.75", "1.462", "1.253", "1.132", "1.045", "0.958", "0.872", "0.767", "0.266")]
CDF_F = [Decimal(x) for x in ("0.0625", "0.09", "0.19", "0.31", "0.50", "0.72", "0.84", "0.94", "1.0")]


@pytest.fixture(scope="module")
def model():
    return hazard_from_table(CDF_R, CDF_F, 32, n0=4, rate_limit_blocks=14_400, i_eff_blocks=57_600,
                             prior_scale_blocks=43_200)


def test_seed_formula() -> None:
    raw = int.from_bytes(hashlib.blake2b(b"7|0xabc", digest_size=8).digest(), "little")
    assert mc_seed(7, "0xabc") == raw
    assert mc_seed(7, "0xabc") != mc_seed(8, "0xabc") != mc_seed(7, "0xabd")


def _hot(kit: ModuleType):
    """Window open long ago (Delta 55,000: r ~ 1.045, hazard ~ 50%/day)."""
    return kit.snapshot(last_reg_block=Block(B - 55_000))


def test_determinism_under_a_fixed_seed_and_block_hash(kit: ModuleType, model) -> None:
    snap = _hot(kit)
    params = McParams(paths=400)
    seed = mc_seed(0, str(snap.block_hash))
    a = run_mc(snap, model, 7_200, params, seed, {})
    b = run_mc(snap, model, 7_200, params, seed, {})
    assert a == b
    other = run_mc(snap, model, 7_200, params, mc_seed(0, "0x" + "f" * 64), {})
    assert other.p_prune_ppm != a.p_prune_ppm


def test_target_is_most_exposed_and_probabilities_are_bounded(kit: ModuleType, model) -> None:
    snap = _hot(kit)
    res = run_mc(snap, model, 7_200, McParams(paths=1_000), 1234, {})
    p = dict(res.p_prune_ppm)
    assert set(p) == {s.key for s in snap.subnets}
    assert all(0 <= v <= 1_000_000 for v in p.values())
    assert p[kit.key(1)] > 300_000                       # the bottom is pruned whenever a registration lands
    assert p[kit.key(1)] > p[kit.key(2)] >= p[kit.key(12)]
    assert sum(p.values()) <= res.p_registration_ppm + 1  # at most one prune per path inside 24 h
    assert 300_000 < res.p_registration_ppm < 800_000


def test_no_prune_when_not_possible_or_inside_the_rate_limit(kit: ModuleType, model) -> None:
    res = run_mc(kit.snapshot(n_nonroot_networks=11), model, 7_200, McParams(paths=100), 1, {})
    assert all(v == 0 for _, v in res.p_prune_ppm) and res.p_registration_ppm == 0
    closed = kit.snapshot(last_reg_block=Block(B - 100))     # window opens in 14,300 blocks
    res2 = run_mc(closed, model, 7_200, McParams(paths=100), 1, {})
    assert all(v == 0 for _, v in res2.p_prune_ppm)
    q_base, q_after = registration_schedule(closed, model, 7_200, 60)
    assert len(q_base) == 120 and max(q_base) == 0.0 and q_after == []


def test_seven_day_horizon_uses_the_post_registration_schedule(kit: ModuleType, model) -> None:
    snap = _hot(kit)
    q_base, q_after = registration_schedule(snap, model, 50_400, 60)
    assert len(q_base) == len(q_after) == 840
    assert all(q == 0.0 for q in q_after[:239])              # NetworkRateLimit after the path's registration
    assert max(q_after[240:]) > 0
    res = run_mc(snap, model, 50_400, McParams(paths=200), 99, {})
    assert res.horizon_blocks == 50_400 and dict(res.p_prune_ppm)[kit.key(1)] > 900_000


def test_jumps_raise_prune_risk_of_a_near_bottom_name(kit: ModuleType, model) -> None:
    subs = [kit.subnet(1, price=Decimal("0.010")), kit.subnet(2, price=Decimal("0.0105")),
            kit.subnet(3, price=Decimal("0.05"))]
    snap = kit.snapshot(subnets=subs, last_reg_block=Block(B - 55_000), subnet_limit=3, n_nonroot_networks=3)
    calm = run_mc(snap, model, 7_200, McParams(paths=1_000, jump_p_day=Decimal(0)), 5, {})
    jumpy = run_mc(snap, model, 7_200, McParams(paths=1_000, jump_p_day=Decimal(2)), 5, {})
    assert jumpy.get(kit.key(2)) > calm.get(kit.key(2))


def test_realised_sigma_from_the_store(kit: ModuleType) -> None:
    snaps = []
    for i in range(80):
        blk = (B // 300) * 300 - 300 * (79 - i)
        price = Decimal("0.01") * (Decimal("1.01") if i % 2 else Decimal(1))
        snaps.append(kit.snapshot(blk, subnets=[kit.subnet(1, price=price)]))
    raw = kit.snapshot(B, subnets=[kit.subnet(1, price=Decimal("0.01"))])
    store = kit.MemStore(snaps + [raw], clock=B)
    sig = realised_sigma_3d(store, raw)
    assert kit.key(1) in sig and 0.03 < sig[kit.key(1)] < 0.06     # +-1% per 300 blocks -> ~4.9 %/day
    fr = kit.frame(raw)
    assert sigma_inputs(None, raw, fr.feats)[kit.key(1)] == 0.05   # falls back to Feat.sigma_d
