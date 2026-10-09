"""risk.regime_throttle: the ONE market-regime throttle (DESIGN.md 3.10 step 7), MONITOR until FT-R1."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

from taotrader.core.config import RiskCfg
from taotrader.core.units import BLOCKS_PER_DAY, PPM, AlphaRao, Rao
from taotrader.risk.regime_throttle import RegimeThrottle, daily_median_residual, m_regime_ppm

DAY0 = (9_240_388 // BLOCKS_PER_DAY) * BLOCKS_PER_DAY


def test_multiplier_clip() -> None:
    assert m_regime_ppm(None) == PPM
    assert m_regime_ppm(0) == PPM and m_regime_ppm(1_000) == PPM
    assert m_regime_ppm(-2_500) == 500_000                  # 1 + (-0.25%) / 0.5%
    assert m_regime_ppm(-5_000) == 0 and m_regime_ppm(-9_000) == 0


def _days(kit: ModuleType, daily_ret: Decimal, n: int = 10):
    out = []
    for j in range(n):
        f = (1 + daily_ret) ** j                      # oldest first
        # tiny emissions keep the modelled drift (chain buy, sell load) negligible next to the price path
        out.append(kit.snapshot(DAY0 - BLOCKS_PER_DAY * (n - 1 - j), block_emission=Rao(1_000),
                                subnets=[kit.subnet(i, price=Decimal("0.01") * i * f, alpha_out_emission=AlphaRao(1_000))
                                         for i in range(1, 6)]))
    return out


def test_falling_market_throttles(kit: ModuleType) -> None:
    snaps = _days(kit, Decimal("-0.02"))
    store = kit.MemStore(snaps, clock=DAY0)
    rt = RegimeThrottle()
    r = rt.reading(store, snaps[-1], RiskCfg())
    assert r.n_days == 9 and r.alpha0_ppm_day is not None
    assert r.alpha0_ppm_day < -15_000 and r.m_regime_ppm == 0
    med = daily_median_residual(snaps[0], snaps[1], RiskCfg())
    assert med is not None and -0.025 < med < -0.015
    assert rt.reading(store, snaps[-1], RiskCfg()) is r          # memoised


def test_flat_market_does_not_throttle_and_no_history_is_neutral(kit: ModuleType) -> None:
    snaps = _days(kit, Decimal(0))
    r = RegimeThrottle().reading(kit.MemStore(snaps, clock=DAY0), snaps[-1], RiskCfg())
    assert r.alpha0_ppm_day is not None and abs(r.alpha0_ppm_day) < 2_000
    lone = RegimeThrottle().reading(kit.MemStore([snaps[-1]], clock=DAY0), snaps[-1], RiskCfg())
    assert lone.alpha0_ppm_day is None and lone.m_regime_ppm == PPM
    assert RegimeThrottle().reading(None, snaps[-1], RiskCfg()).m_regime_ppm == PPM
