"""taotrader/risk/hazard_mc.py - the seeded prune Monte Carlo behind Tier B and the MC entry rule (WP8; DESIGN.md 3.3).

P_prune(k, H) = share of N_PATHS simulated paths in which generation k is pruned within H blocks. Only consulted when
RiskCfg.tier_b_enabled (phase 2, after FT1 and FT2 pass): Tier B uses H = H_B (7,200 blocks), the entry floor
P_prune_7d * (1 - R) <= 1% uses H = 50,400.

Per path, every `step_blocks` (60) blocks, for every non-root generation j of the snapshot:
- ln s_j += sigma_j * sqrt(60/7200) * (sqrt(rho) Z_c + sqrt(1 - rho) Z_j) + J_j, sigma_j = max(6%/day, realised
  3-day volatility), rho = 0.3, J_j = ln(1 - 0.5) with probability p_jump/day * 60/7200 (Calibration.tier_b_jump_p_day
  and tier_b_jump_size as of the decision block; priors 0.03 and ln 0.5);
- the EMA follows the chain recursion over the step, E += (1 - (1 - a_j)^60) (min(s_j, 1) - E), with a_j =
  SubnetMovingAlpha * b/(b + EMAPriceHalvingBlocks) at the step start (frozen EMAs: a = 0);
- a registration lands with the conditional per-step probability of the hazard model (protocol.prune.p_registration,
  which already returns 0 inside NetworkRateLimit and when not prune_possible). It prunes the path's target, the
  argmin of (EMA, reg_at, netuid) over generations alive and no longer immune at the step end (subnets are admitted
  at their immunity expiry). The window then closes for NetworkRateLimit and the hazard restarts from the new
  registration (the schedule depends only on blocks since the registration; the cost ratio r = c/L is invariant to L).

Seeding (section 3.3): numpy.random.Generator(PCG64(int.from_bytes(blake2b(f"{run_seed}|{block_hash}",
digest_size=8), "little"))). Draw order per step is fixed (Z_c, Z, jump uniforms, registration uniforms), so a run is
reproducible bit for bit on one platform; floats are feature math here (cross-OS results agree to tolerance).
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import pairwise
from typing import Final

import numpy as np

from ..core.errors import LookaheadError
from ..core.fixed import DEC, ONE, to_ppm
from ..core.protocols import SnapshotStore
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import BLOCKS_PER_DAY, PPM, Block, Ppm, SubnetKey
from ..core.views import Feat
from ..protocol.calibration import Calibration
from ..protocol.ema import blocks_since_start
from ..protocol.emission import emit_eligible
from ..protocol.prune import HazardModel, immunity_end, p_registration, prune_possible, window_open_block

__all__ = [
    "H_7D_BLOCKS",
    "McParams",
    "McResult",
    "QCache",
    "mc_seed",
    "realised_sigma_3d",
    "registration_schedule",
    "run_mc",
    "sigma_inputs",
]

H_7D_BLOCKS: Final[int] = 7 * BLOCKS_PER_DAY
SIGMA_GRID_BLOCKS: Final[int] = 300
SIGMA_LOOKBACK_BLOCKS: Final[int] = 3 * BLOCKS_PER_DAY
SIGMA_MIN_RETURNS: Final[int] = 10
_TINY: Final[float] = 1e-300


@dataclass(frozen=True, slots=True)
class McParams:
    paths: int = 2_000
    step_blocks: int = 60
    sigma_floor_ppm_day: int = 60_000          # 6 %/day
    rho_ppm: int = 300_000                     # common-factor correlation 0.3
    jump_p_day: Decimal = Decimal("0.03")
    jump_log_size: Decimal = DEC.ln(Decimal("0.5"))

    @staticmethod
    def from_calibration(cal: Calibration | None, paths: int = 2_000) -> McParams:
        if cal is None:
            return McParams(paths=paths)
        return McParams(paths=paths, jump_p_day=cal.tier_b_jump_p_day, jump_log_size=cal.tier_b_jump_size)


@dataclass(frozen=True, slots=True)
class McResult:
    block: Block
    horizon_blocks: int
    paths: int
    seed: int
    p_prune_ppm: tuple[tuple[SubnetKey, Ppm], ...]   # sorted by key
    p_registration_ppm: Ppm                          # share of paths with at least one registration

    def get(self, key: SubnetKey) -> Ppm:
        for k, p in self.p_prune_ppm:
            if k == key:
                return p
        return Ppm(0)


def mc_seed(run_seed: int, block_hash: str) -> int:
    """int.from_bytes(blake2b(f"{run_seed}|{block_hash}".encode(), digest_size=8).digest(), "little")."""
    return int.from_bytes(hashlib.blake2b(f"{run_seed}|{block_hash}".encode(), digest_size=8).digest(), "little")


QCache = dict[tuple[object, ...], float]


def _hazard_key(glob: ChainGlobals) -> tuple[int, ...]:
    """Every ChainGlobals field protocol.prune.p_registration reads (the rest of the globals does not matter)."""
    return (int(glob.last_reg_block), int(glob.last_lock_cost), int(glob.min_lock_cost), glob.lock_reduction_interval,
            int(glob.block_emission), glob.network_rate_limit, glob.n_nonroot_networks, glob.cleanup_queue_len,
            glob.subnet_limit)


def _step_q(glob: ChainGlobals, model: HazardModel, t: int, h: int, cache: QCache | None) -> float:
    """P(a registration in (t, t + h] | none since last_reg_block): p_registration is exactly this conditional and
    composes across steps (P(t0, t2) = 1 - (1 - P(t0, t1))(1 - P(t1, t2))), so it depends only on the absolute block
    and is memoised by it."""
    if t + h < int(window_open_block(glob)):
        return 0.0
    ck: tuple[object, ...] = (_hazard_key(glob), model, t, h)
    if cache is not None:
        hit = cache.get(ck)
        if hit is not None:
            return hit
    q = float(min(max(p_registration(glob, model, Block(t), h), Decimal(0)), ONE))
    if cache is not None:
        if len(cache) > 200_000:
            cache.clear()
        cache[ck] = q
    return q


def registration_schedule(snap: ChainSnapshot, model: HazardModel, horizon_blocks: int, step_blocks: int,
                          cache: QCache | None = None) -> tuple[list[float], list[float]]:
    """(q_base, q_after): conditional per-step registration probabilities from the snapshot's state, and `d` steps
    after a path's own registration (q_after; empty when the horizon cannot reach the next window).

    q_after is computed once per lock cost on a canonical registration at block 1 with NetworkLastLockCost = the
    current L: the hazard depends on the cost ratio r = c/L', which after a registration follows 2 - Delta/I_eff
    whatever L' is (exact up to the integer rounding of L'/I_eff and the NetworkMinLockCost floor)."""
    glob = snap.glob
    n = -(-horizon_blocks // step_blocks)
    if n <= 0 or not prune_possible(glob):
        return [0.0] * max(n, 0), []
    b = int(snap.block)
    base = [_step_q(glob, model, b + i * step_blocks, min(step_blocks, horizon_blocks - i * step_blocks), cache)
            for i in range(n)]
    if horizon_blocks <= glob.network_rate_limit:
        return base, []
    rel = replace(glob, last_reg_block=Block(1))
    after = [_step_q(rel, model, 1 + d * step_blocks, step_blocks, cache) for d in range(n)]
    return base, after


def run_mc(snap: ChainSnapshot, model: HazardModel, horizon_blocks: int, params: McParams, seed: int,
           sigma_day: Mapping[SubnetKey, float], cache: QCache | None = None) -> McResult:
    """P_prune for every non-root generation of `snap` over `horizon_blocks` (see the module docstring)."""
    glob = snap.glob
    block = int(snap.block)
    subs: list[SubnetState] = sorted((s for s in snap.subnets if int(s.key.netuid) != 0),
                                     key=lambda s: (int(s.key.reg_at), int(s.key.netuid)))
    keys = [s.key for s in subs]
    n = len(subs)
    step = max(int(params.step_blocks), 1)
    n_steps = -(-max(horizon_blocks, 0) // step)
    paths = max(int(params.paths), 1)
    if n == 0 or n_steps == 0 or not prune_possible(glob):
        return McResult(Block(block), horizon_blocks, paths, seed, tuple(sorted((k, Ppm(0)) for k in keys)), Ppm(0))
    q_base, q_after = registration_schedule(snap, model, horizon_blocks, step, cache)

    e0 = np.array([float(s.moving_price) for s in subs], dtype=np.float64)
    spot0 = np.array([float(s.pool.spot()) if s.pool.px_tao > 0 and s.pool.px_alpha > 0 else 0.0 for s in subs],
                     dtype=np.float64)
    log0 = np.log(np.maximum(spot0, _TINY))
    imm_end = np.array([int(immunity_end(s, glob)) for s in subs], dtype=np.int64)
    frozen = np.array([not emit_eligible(s) for s in subs], dtype=bool)
    b0 = np.array([float(blocks_since_start(s, snap.block) or 0) for s in subs], dtype=np.float64)
    halving = np.array([float(max(s.ema_halving_blocks, 1)) for s in subs], dtype=np.float64)
    ma = float(glob.moving_alpha)
    floor = params.sigma_floor_ppm_day / PPM
    sig = np.array([max(floor, float(sigma_day.get(k, floor))) for k in keys], dtype=np.float64)
    rho = params.rho_ppm / PPM
    sr, sc = math.sqrt(rho), math.sqrt(1.0 - rho)
    jump = float(params.jump_log_size)
    p_jump_day = float(params.jump_p_day)

    rng = np.random.Generator(np.random.PCG64(seed))
    ema = np.tile(e0, (paths, 1))
    logs = np.tile(log0, (paths, 1))
    alive = np.ones((paths, n), dtype=bool)
    pruned = np.zeros((paths, n), dtype=bool)
    last_reg = np.full(paths, -1, dtype=np.int64)
    any_reg = np.zeros(paths, dtype=bool)
    rows = np.arange(paths)
    q_after_arr = np.array(q_after if q_after else [0.0], dtype=np.float64)

    for i in range(n_steps):
        h = min(step, horizon_blocks - i * step)
        t_end = block + i * step + h
        zc = rng.standard_normal(paths)
        z = rng.standard_normal((paths, n))
        uj = rng.random((paths, n))
        ur = rng.random(paths)
        sd = sig * math.sqrt(h / BLOCKS_PER_DAY)
        logs += sd * (sr * zc[:, None] + sc * z)
        logs += np.where(uj < min(1.0, p_jump_day * h / BLOCKS_PER_DAY), jump, 0.0)
        b = b0 + i * step
        a = np.where(frozen | (b <= 0), 0.0, ma * b / (b + halving))
        decay = 1.0 - np.power(1.0 - a, h)
        ema += decay * (np.minimum(np.exp(logs), 1.0) - ema)
        if q_after:
            d = np.clip(i - last_reg - 1, 0, len(q_after) - 1)
            q = np.where(last_reg < 0, q_base[i], q_after_arr[d])
        else:
            q = np.where(last_reg < 0, q_base[i], 0.0)
        reg = ur < q
        if not reg.any():
            continue
        admitted = alive & (t_end >= imm_end)[None, :]
        masked = np.where(admitted, ema, np.inf)
        tgt = np.argmin(masked, axis=1)
        has = np.isfinite(masked[rows, tgt])
        hit = reg & has
        pruned[rows[hit], tgt[hit]] = True
        alive[rows[hit], tgt[hit]] = False
        last_reg[reg] = i
        any_reg |= reg

    probs = pruned.mean(axis=0)
    out = tuple(sorted((keys[j], to_ppm(float(probs[j]))) for j in range(n)))
    return McResult(Block(block), horizon_blocks, paths, seed, out, to_ppm(float(any_reg.mean())))


def realised_sigma_3d(store: SnapshotStore | None, raw: ChainSnapshot,
                      keys: Sequence[SubnetKey] | None = None) -> dict[SubnetKey, float]:
    """Realised daily volatility over 3 days from ln spot on the absolute 300-block grid of the store (>= 10
    returns of the same generation), sqrt(24) x SD of the 300-block log returns."""
    if store is None:
        return {}
    b = int(raw.block)
    snaps: dict[int, ChainSnapshot] = {}
    g = (b // SIGMA_GRID_BLOCKS) * SIGMA_GRID_BLOCKS
    while g > b - SIGMA_LOOKBACK_BLOCKS:
        try:
            snap = store.at_or_before(Block(g))
        except (KeyError, LookaheadError, ValueError):
            break
        if int(snap.block) <= b - SIGMA_LOOKBACK_BLOCKS:
            break
        snaps[int(snap.block)] = snap
        g -= SIGMA_GRID_BLOCKS
    snaps[b] = raw
    series = [snaps[k] for k in sorted(snaps)]
    want = sorted(keys) if keys is not None else sorted(s.key for s in raw.subnets)
    out: dict[SubnetKey, float] = {}
    scale = math.sqrt(BLOCKS_PER_DAY / SIGMA_GRID_BLOCKS)
    for key in want:
        lp: list[float] = []
        for snap in series:
            s = snap.get(key)
            if s is not None and s.pool.px_tao > 0 and s.pool.px_alpha > 0:
                p = float(s.pool.spot())
                if p > 0:
                    lp.append(math.log(p))
        rets = [y - x for x, y in pairwise(lp)]
        if len(rets) < SIGMA_MIN_RETURNS:
            continue
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out[key] = math.sqrt(var) * scale
    return out


def sigma_inputs(store: SnapshotStore | None, raw: ChainSnapshot, feats: Mapping[SubnetKey, Feat]) -> dict[SubnetKey, float]:
    """sigma_j per generation: realised 3-day volatility, else Feat.sigma_d (14 d), else absent (the MC floor)."""
    out = realised_sigma_3d(store, raw)
    for s in raw.subnets:
        if s.key not in out:
            f = feats.get(s.key)
            if f is not None and f.sigma_d is not None and math.isfinite(f.sigma_d):
                out[s.key] = f.sigma_d
    return out
