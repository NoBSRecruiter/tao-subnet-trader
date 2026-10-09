"""taotrader/risk/regime_throttle.py - the ONE market-regime throttle (WP8; DESIGN.md 3.10 step 7). MONITOR by default.

Every 7,200 blocks (at absolute day boundaries d * 7,200, so stride 1 and stride 60 agree):
    alpha_0 = EWMA (half-life 7 d) of the cross-sectional MEDIAN residual daily P x I return of the eligible universe,
    residual_i = ln(P_d I_d / (P_{d-1} I_{d-1})) - (cb_push_i - sell_push_i) * days,
    m_regime = clip(1 + alpha_0 / 0.5%, 0, 1).
P is the era-correct spot, I the share-price index of the subnet's largest earning tracked hotkey (price only when no
earner is tracked on both days), and the modelled drift (cb_push - sell_push, fractions of price per day) comes from the
shared emission and sell-load replicas (protocol.emission.emission_vector + protocol.sellload with the frozen priors)
on the day-start snapshot. Eligible on the day-start snapshot: started, emission-enabled, SubnetTAO >= t_min_pool_rao,
quote weight within [0.30, 0.70], same generation on both days. History: the last 35 day boundaries of the bounded
SnapshotStore (missing days are skipped).

m_regime multiplies G_MAX_EFF only when RiskCfg.regime_throttle_active (FT-R1 pass); otherwise it is journaled as a
RiskAction(action="MONITOR"). Floats are feature math here; the result crosses into decisions as ppm.
`RegimeThrottle` memoises per-day medians keyed by the two snapshots' digests (a pure function of the store).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from ..core.config import RiskCfg
from ..core.errors import LookaheadError
from ..core.fixed import DEC, to_ppm
from ..core.protocols import SnapshotStore
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import BLOCKS_PER_DAY, PERQUINTILL, PPM, Block
from ..protocol.emission import emission_vector, sum_ema
from ..protocol.sellload import SellLoadParams, sell_load

__all__ = [
    "HALF_LIFE_DAYS",
    "HISTORY_DAYS",
    "SCALE_PPM_DAY",
    "RegimeReading",
    "RegimeThrottle",
    "daily_median_residual",
    "m_regime_ppm",
]

HISTORY_DAYS: Final[int] = 35
HALF_LIFE_DAYS: Final[int] = 7
SCALE_PPM_DAY: Final[int] = 5_000          # 0.5 %/day
QUOTE_MIN_E18: Final[int] = 3 * PERQUINTILL // 10
QUOTE_MAX_E18: Final[int] = 7 * PERQUINTILL // 10


@dataclass(frozen=True, slots=True)
class RegimeReading:
    day_block: Block                       # the day boundary the reading was computed at
    alpha0_ppm_day: int | None             # None without at least one day of history
    m_regime_ppm: int
    n_days: int


def m_regime_ppm(alpha0_ppm_day: int | None) -> int:
    """clip(1 + alpha_0 / 0.5%, 0, 1) in ppm; 1 without a reading."""
    if alpha0_ppm_day is None:
        return PPM
    return max(0, min(PPM, PPM + alpha0_ppm_day * PPM // SCALE_PPM_DAY))


def _eligible(s: SubnetState, cfg: RiskCfg) -> bool:
    return (s.first_emission_block is not None and s.emission_enabled and s.pool.tao >= cfg.t_min_pool_rao
            and QUOTE_MIN_E18 <= s.pool.w_quote_e18 <= QUOTE_MAX_E18 and s.pool.px_alpha > 0 and s.pool.px_tao > 0)


def _log_pxi(s0: SubnetState, s1: SubnetState) -> float | None:
    p0, p1 = float(s0.pool.spot()), float(s1.pool.spot())
    if p0 <= 0 or p1 <= 0:
        return None
    r = math.log(p1 / p0)
    earners = sorted((h for h in s0.hotkeys if h.earns), key=lambda h: (-int(h.total_alpha), h.hotkey))
    for h0 in earners:
        h1 = s1.hotkey(h0.hotkey)
        if h1 is None:
            continue
        i0, i1 = float(h0.index()), float(h1.index())
        if i0 > 0 and i1 > 0:
            return r + math.log(i1 / i0)
    return r


def daily_median_residual(day0: ChainSnapshot, day1: ChainSnapshot, cfg: RiskCfg) -> float | None:
    """Cross-sectional median residual return between two day-boundary snapshots (fraction), None if empty."""
    days = (int(day1.block) - int(day0.block)) / BLOCKS_PER_DAY
    if days <= 0:
        return None
    shares = emission_vector(day0)
    root_flag = sum_ema(day0) > 1
    params = SellLoadParams()
    vals: list[float] = []
    for s0 in day0.subnets:
        if not _eligible(s0, cfg):
            continue
        s1 = day1.get(s0.key)
        share = shares.get(s0.key)
        if s1 is None or share is None:
            continue
        ret = _log_pxi(s0, s1)
        if ret is None:
            continue
        sl = sell_load(s0, day0.glob, share, params, day0.block, root_flag)
        drift = float(DEC.subtract(sl.cb_push_day, sl.sell_push_day))
        vals.append(ret - drift * days)
    if not vals:
        return None
    vals.sort()
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 == 1 else (vals[m - 1] + vals[m]) / 2


class RegimeThrottle:
    """Memoised reader of the regime throttle (deterministic: the memo only caches a pure function of the store)."""

    def __init__(self) -> None:
        self._medians: dict[tuple[str, str], float | None] = {}
        self._readings: dict[tuple[int, str], RegimeReading] = {}

    def reading(self, store: SnapshotStore | None, raw: ChainSnapshot, cfg: RiskCfg) -> RegimeReading:
        day_block = (int(raw.block) // BLOCKS_PER_DAY) * BLOCKS_PER_DAY
        if store is None:
            return RegimeReading(Block(day_block), None, PPM, 0)
        snaps: list[ChainSnapshot] = []
        for j in range(HISTORY_DAYS + 1):
            g = day_block - j * BLOCKS_PER_DAY
            if g <= 0:
                break
            try:
                snap = raw if g == int(raw.block) else store.at_or_before(Block(g))
            except (KeyError, LookaheadError, ValueError):
                break
            if snaps and int(snap.block) >= int(snaps[-1].block):
                continue
            snaps.append(snap)
        snaps.reverse()
        memo_key = (day_block, "|".join(f"{int(s.block)}:{s.digest}" for s in snaps))
        hit = self._readings.get(memo_key)
        if hit is not None:
            return hit
        residuals: list[float] = []
        for a, b in pairwise(snaps):
            k = (f"{int(a.block)}:{a.digest}", f"{int(b.block)}:{b.digest}")
            if k not in self._medians or not a.digest or not b.digest:
                self._medians[k] = daily_median_residual(a, b, cfg)
            v = self._medians[k]
            if v is not None and math.isfinite(v):
                residuals.append(v)
        if not residuals:
            reading = RegimeReading(Block(day_block), None, PPM, 0)
        else:
            num = den = 0.0
            for age, r in enumerate(reversed(residuals)):
                w = 0.5 ** (age / HALF_LIFE_DAYS)
                num += w * r
                den += w
            alpha0 = int(to_ppm(num / den))
            reading = RegimeReading(Block(day_block), alpha0, m_regime_ppm(alpha0), len(residuals))
        if len(self._readings) > 64:
            self._readings.clear()
        if len(self._medians) > 4_096:
            self._medians.clear()
        self._readings[memo_key] = reading
        return reading
