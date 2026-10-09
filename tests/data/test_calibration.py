"""WP4 calibration provider tests (DESIGN.md sections 3.3, 8.10, 11 WP4): the as-of rule (asof(t) is unchanged when
registration rows - and prunes and dissolutions - after t are perturbed or deleted), the hazard / kappa_p / Tier B /
FT10 fits, refit-point caching, the hazard invalidation path, and FrozenCalibration (preregistered values, paper and
live only)."""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.core.state import ChainSnapshot, SubnetState
from taotrader.core.units import Block, NetUid, RunMode
from taotrader.data import calibration as cal
from taotrader.data.lake import Lake
from taotrader.data.refine import write_dimension
from taotrader.protocol.calibration import calibration_digest, check_asof
from taotrader.protocol.prune import RegistrationRow, apply_hot_market, fit_hazard
from taotrader.protocol.sellload import SellLoadParams

PRE = cal.PreregCalib.load()


def reg(b: int, r: str, d: int = 50_000, victim: int | None = 7) -> RegistrationRow:
    return RegistrationRow(Block(b), None if victim is None else NetUid(victim), Decimal(r), d)


REGS = tuple(reg(8_000_000 + 50_000 * i, r) for i, r in enumerate(
    ["1.31", "1.05", "0.99", "1.12", "0.87", "1.21", "1.02", "0.95", "1.44", "1.47", "1.10", "0.91", "1.00", "1.30"]))
PRUNES = tuple(cal.PruneObs(8_000_000 + 50_000 * i, (Decimal(1), Decimal("1.05"), Decimal("1.2"), Decimal("1.6")),
                            Decimal(1) if i % 4 else Decimal("1.2"), 1 if i % 5 else 6) for i in range(14))
PARAMS = (cal.ParamPoint(7_000_000, 28_800, 115_200), cal.ParamPoint(7_200_000, 28_800, 57_600),
          cal.ParamPoint(7_400_000, 14_401, 57_600))


def provider(regs: tuple[RegistrationRow, ...] = REGS, prunes: tuple[cal.PruneObs, ...] = PRUNES,
             diss: tuple[cal.DissolutionObs, ...] = (), **kw: Any) -> cal.LakeCalibrationProvider:
    return cal.LakeCalibrationProvider(cal.CalibrationInputs(regs, PARAMS, prunes, diss), prereg=PRE, **kw)


# ------------------------------------------------------------------------------------------------ preregistered values
def test_prereg_values_are_the_section_3_3_table() -> None:
    assert PRE.cdf_r[0] == Decimal("1.75") and PRE.cdf_f[0] == Decimal("0.0625") and PRE.n_registrations == 32
    assert (PRE.n0, PRE.refit_min_rows, PRE.prior_scale_blocks) == (4, 8, 43_200)
    assert PRE.kappa_p == 4 and PRE.kappa_p_range == (Decimal(2), Decimal(8)) and PRE.r_default == Decimal("0.35")
    assert PRE.tier_b_jump_p_day == Decimal("0.03") and PRE.tier_b_prior_until_prunes == 10
    assert abs(PRE.tier_b_jump_size - Decimal("-0.6931471805599453")) < Decimal("1e-15")
    assert PRE.phi == SellLoadParams()                                      # the sellload priors
    with pytest.raises(ValueError):
        cal._ln_one_minus("log(0.5)")


def test_frozen_calibration_paper_live_only_and_hot_market() -> None:
    with pytest.raises(ValueError, match="paper and live"):
        cal.FrozenCalibration(RunMode.BACKTEST)
    f = cal.FrozenCalibration(RunMode.PAPER)
    c = f.asof(Block(9_300_000))
    check_asof(c, Block(9_300_000))
    assert c.hazard == cal.frozen_hazard(PRE) and c.hazard.valid
    assert abs(c.hazard.p_open - Decimal("0.0625") * 32 / 36) < Decimal("1e-12")       # the opening mass, smoothed
    assert (c.kappa_p, c.r_default, c.r_cap_formula, c.tier_b_jump_p_day) == (Decimal(4), Decimal("0.35"), True, Decimal("0.03"))
    assert c.digest == calibration_digest(c) and f.asof(Block(9_300_000)).digest == c.digest
    hot = cal.FrozenCalibration(RunMode.LIVE, recent_cost_ratios=lambda _b: [Decimal("1.5"), Decimal("1.41")], ft10_passed=True)
    ch = hot.asof(Block(9_300_000))
    assert ch.hazard == apply_hot_market(cal.frozen_hazard(PRE), [Decimal("1.5"), Decimal("1.41")])
    assert ch.hazard.p_open == Decimal("0.5") and ch.r_cap_formula is False


# ------------------------------------------------------------------------------------------------ as-of rule
@pytest.mark.parametrize("t", [7_100_000, 8_160_000, 8_375_001, 8_700_000])
def test_asof_unchanged_when_future_rows_are_perturbed(t: int) -> None:
    """Section 11 WP4 acceptance / section 8.10 bias test."""
    base = provider().asof(Block(t))
    regs = tuple(r if r.queued_block < t else RegistrationRow(r.queued_block, None, r.cost_ratio * 2, 1) for r in REGS)
    regs += (reg(t, "1.7"), reg(t + 5, "0.5"))                                  # new rows at and after t
    prunes = tuple(p if p.block < t else cal.PruneObs(p.block, p.rhos, Decimal("1.6"), 9) for p in PRUNES)
    diss = (cal.DissolutionObs(t, Decimal("0.3"), Decimal("0.9")),)
    for variant in (provider(regs=regs), provider(prunes=prunes), provider(diss=diss),
                    provider(regs=tuple(r for r in REGS if r.queued_block < t), prunes=tuple(p for p in PRUNES if p.block < t))):
        got = variant.asof(Block(t))
        assert got == base and got.digest == base.digest
    check_asof(base, Block(t))


def test_refit_points_cache_and_asof_field() -> None:
    p = provider()
    a = p.asof(Block(8_120_000))
    b = p.asof(Block(8_140_000))
    assert a is b and a.asof == 8_100_001                                     # last event (8,100,000) + 1
    c = p.asof(Block(8_150_001))
    assert c.asof == 8_150_001 and c.digest != a.digest
    assert p.asof(Block(10)).asof == 0                                        # before any event


def test_hazard_fit_only_on_rows_before_asof() -> None:
    p = provider()
    early = p.asof(Block(8_300_000))                                          # 6 registrations before: the prior
    prior = cal.prior_hazard(14_401, 57_600, PRE.prior_scale_blocks, PRE.n0)
    assert early.hazard == prior
    late = p.asof(Block(8_500_000))                                           # 10 rows: refit
    rows = [r for r in REGS if r.queued_block < 8_450_001]
    assert late.hazard == fit_hazard(rows, Block(8_450_001), prior) and late.hazard != prior
    # the prior follows the rate limit / I_eff in force before asof
    assert provider().asof(Block(7_100_000)).hazard == cal.prior_hazard(28_800, 115_200, PRE.prior_scale_blocks, PRE.n0)


def test_hazard_invalidation_path() -> None:
    p = provider(hazard_invalid_from=8_200_000)
    assert p.asof(Block(8_150_000)).hazard.valid                              # before the invalidation
    mid = p.asof(Block(8_500_000))
    assert not mid.hazard.valid                                                # 6 post-invalidation rows < 8
    assert p.asof(Block(8_700_000)).hazard.valid                              # 10 post-invalidation rows: refit
    regs_after = [r for r in REGS if 8_200_000 <= r.queued_block < 8_650_001]
    prior = cal.prior_hazard(14_401, 57_600, PRE.prior_scale_blocks, PRE.n0)
    assert p.asof(Block(8_700_000)).hazard == fit_hazard(regs_after, Block(8_650_001), prior)


# ------------------------------------------------------------------------------------------------ component fits
def test_kappa_fit_prior_until_enough_prunes_then_mle() -> None:
    assert cal.fit_kappa(PRUNES[:9], Decimal(4), (Decimal(2), Decimal(8)), 10) == 4
    always_bottom = [cal.PruneObs(i, (Decimal(1), Decimal("1.5"), Decimal(2)), Decimal(1), 1) for i in range(12)]
    k = cal.fit_kappa(always_bottom, Decimal(4), (Decimal(2), Decimal(8)), 10)
    assert k == (Decimal(12) * 8 + 4 * 4) / 16                                 # MLE at the upper bound, shrunk
    random_victims = [cal.PruneObs(i, (Decimal(1), Decimal("1.05"), Decimal("1.1")), Decimal("1.05") + Decimal(i % 3 - 1) / 20, 1)
                      for i in range(12)]
    k2 = cal.fit_kappa(random_victims, Decimal(4), (Decimal(2), Decimal(8)), 10)
    assert Decimal(2) <= k2 < k and k2 == cal.fit_kappa(random_victims, Decimal(4), (Decimal(2), Decimal(8)), 10)
    mixed = cal.fit_kappa(list(PRUNES), Decimal(4), (Decimal(2), Decimal(8)), 10)
    assert Decimal(2) < mixed < Decimal(8) and mixed == mixed.quantize(Decimal("0.001"))
    unusable = [cal.PruneObs(i, (Decimal(1),), None, None) for i in range(20)]
    assert cal.fit_kappa(unusable, Decimal(4), (Decimal(2), Decimal(8)), 10) == 4


def test_tier_b_jump_and_ft10() -> None:
    ps = [cal.PruneObs(i, (), None, 5 if i < 3 else 1) for i in range(12)]
    assert cal.fit_tier_b_jump(ps[:9], Decimal("0.03"), 10) == Decimal("0.03")
    assert cal.fit_tier_b_jump(ps, Decimal("0.03"), 10) == Decimal("0.1438")    # -ln(1 - 3/12) / 2
    assert cal.fit_tier_b_jump([cal.PruneObs(i, (), None, 9) for i in range(10)], Decimal("0.03"), 10) == Decimal("0.2")
    assert cal.ft10_cap([]) is True
    ok = [cal.DissolutionObs(1, Decimal("0.36"), Decimal("0.40")), cal.DissolutionObs(2, Decimal("0.3"), Decimal("0.31"))]
    assert cal.ft10_cap(ok) is False
    assert cal.ft10_cap(ok + [cal.DissolutionObs(3, Decimal("0.3"), Decimal("0.45"))]) is True
    p = provider(diss=tuple(ok))
    assert p.asof(Block(1)).r_cap_formula is True and p.asof(Block(3)).r_cap_formula is False


def test_phi_measurements_as_of() -> None:
    m = SellLoadParams(phi_owner_ppm=500_000)                                    # type: ignore[arg-type]
    p = cal.LakeCalibrationProvider(cal.CalibrationInputs(REGS, PARAMS, PRUNES, (), ((8_400_000, m),)), prereg=PRE)
    assert p.asof(Block(8_400_000)).phi == PRE.phi and p.asof(Block(8_400_001)).phi == m


# ------------------------------------------------------------------------------------------------ from the lake
def _lake_with_history(root: Path, make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot],
                       regs: list[dict[str, Any]]) -> Lake:
    lake = Lake(root)
    snaps = []
    for b in range(8_900_000, 9_000_001, 3_600):
        subs = [make_subnet(n, reg_at=7_000_000 + n, moving_price=Decimal(n) / Decimal(1000)) for n in (1, 2, 3, 4)]
        snaps.append(make_snapshot(b, subs, network_rate_limit=14_401))
    lake.write_snapshots(snaps)
    write_dimension(lake, "registration", regs, "queued_block")
    gens = [{"netuid": 1, "reg_at": 7_000_001, "queued_block": None, "added_block": 7_000_001, "start_call_block": None,
             "first_seen": 8_900_000, "last_seen": 8_960_000, "end_block": 8_970_000, "end_kind": "pruned",
             "end_refined": False, "lock_amount": None, "seed_price_rao": None, "seed_anomaly": None, "pre_end_tao": None,
             "pre_end_alpha_in": None, "pre_end_alpha_out": None, "pre_end_protocol": None, "pre_end_escrow": None,
             "pre_end_total_staked": None, "observed_payout_ratio": 0.4}]
    write_dimension(lake, "generation", gens, "reg_at")
    return lake


def test_from_lake_asof_unchanged_when_lake_rows_after_t_change(make_subnet: Callable[..., SubnetState],
                                                                make_snapshot: Callable[..., ChainSnapshot],
                                                                tmp_path: Path) -> None:
    regs: list[dict[str, Any]] = [{"queued_block": 8_000_000 + 40_000 * i, "victim_netuid": 5, "victim_reg_at": 1,
                                   "new_reg_at": 8_000_020 + 40_000 * i,
             "cost_ratio": 0.9 + i / 50, "lock_amount": 10**12, "blocks_since_prev": 40_000, "shielded": None} for i in range(25)]
    t = 8_600_000
    with _lake_with_history(tmp_path / "a" / "lake", make_subnet, make_snapshot, regs) as lake:
        inp = cal.CalibrationInputs.from_lake(lake, r_default=PRE.r_default)
        assert len(inp.registrations) == 25 and inp.registrations[3].cost_ratio == Decimal("0.96")
        assert inp.params == (cal.ParamPoint(8_900_000, 14_401, 57_600),)
        assert len(inp.prunes) == 1 and inp.prunes[0].victim_rho == 1 and inp.prunes[0].rank_2d == 1
        assert inp.prunes[0].rhos == tuple(Decimal(x) for x in (1, 2, 3, 4))
        assert len(inp.dissolutions) == 1 and inp.dissolutions[0].observed == Decimal("0.4")
        a = cal.LakeCalibrationProvider(inp, prereg=PRE).asof(Block(t))
    perturbed = [r if r["queued_block"] < t else {**r, "cost_ratio": 2.5, "blocks_since_prev": 1} for r in regs]
    perturbed = [r for r in perturbed if r["queued_block"] != 8_960_000]
    with _lake_with_history(tmp_path / "b" / "lake", make_subnet, make_snapshot, perturbed) as lake:
        b = cal.LakeCalibrationProvider.from_lake(lake, prereg=PRE).asof(Block(t))
    assert a == b and a.digest == b.digest and a.hazard.valid
