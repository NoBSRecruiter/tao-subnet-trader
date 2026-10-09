"""taotrader/protocol/prune.py - prune ladder and target, registration cost, the registration hazard model and its
as-of refit, and the dissolution recovery ratio (WP2; DESIGN.md sections 3.3, 5.12, 8.8, 8.10; brief 4.1-4.5).

Prune rule (picked the actual victim in 52 of 52 prunes): target = argmin over non-immune, non-root, added subnets
of (SubnetMovingPrice, NetworkRegisteredAt); pruning happens only when n_nonroot + len(DissolveCleanupQueue) >=
SubnetLimit.

Registration cost (root.rs::get_network_lock_cost; reproduces the runtime API to the rao at 9,240,878):
    cost = max(NetworkMinLockCost, mult*L - (L // I_eff) * (block - last_reg_block)),
    I_eff = NetworkLockReductionInterval * block_emission // 1e9, mult = 2 (1 if no lock was ever recorded).

Hazard model (section 3.3). `HazardModel.cdf_by_r` holds the FINAL piecewise-linear CDF in the cost ratio
r = cost / NetworkLastLockCost, r descending: F(r) = P(the next registration has happened by the time the ratio has
fallen to r). Smoothing toward a prior is applied when the model is BUILT (hazard_from_table for the frozen
section-3.3 table with its parametric Delta-prior; fit_hazard for as-of refits), never inside p_registration.
`p_open` raises the mass at window opening above the CDF's first point (hot market); `lambda_floor_per_block`
bounds the per-block hazard from below once r <= TAIL_FLOOR_R; `valid = False` falls back to the window rule plus
the constant floor hazard. The section 3.3 reference P(reg within 24 h) values (8.3 / 7.3 / 15.7 / 39.6 / 51 / 55%)
are reproduced to < 0.6 pp by the frozen-table model WITHOUT the tail floor; with the (exact, per-block) floor they
rise by 0.8 / 1.9 / 3.0 pp at delta 50k / 55k / 65k (the floor can only raise a probability).

Verified on the archive (tests/protocol/test_network.py): ladder(snapshot at P-1)[0] is the victim in 52 of 52
logged prunes, and SN58 is the target at 8,500,160. time_to_target() is the section 3.3 moving-bottom search (exact first block).
Recovery: see recovery_ratio / staker_base for the fail-closed staker base (pending FT10).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from typing import Final

from ..core.fixed import DEC, ONE, floor_int
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import BLOCKS_PER_DAY, RAO_PER_TAO, Block, NetUid, Rao, SubnetKey
from .ema import project_ema
from .regimes import LOCK_COST_MULT

ZERO: Final[Decimal] = Decimal(0)
TAIL_FLOOR_R: Final[Decimal] = Decimal("1.045")      # section 3.3 / prereg [prune.hazard].tail_floor_r (frozen)
REFIT_MIN_ROWS: Final[int] = 8                       # fewer rows -> the prior is returned unchanged
HOT_MARKET_LAST_N: Final[int] = 2                    # hot market: the last 2 registrations paid r >= 1.4 ...
HOT_MARKET_R_MIN: Final[Decimal] = Decimal("1.4")
HOT_MARKET_P_OPEN: Final[Decimal] = Decimal("0.5")   # ... -> P_OPEN = 0.5
DEFAULT_GRID_STEP: Final[Decimal] = Decimal("0.005") # r-grid of hazard_from_table (0.005 r = 288 blocks at I_eff 57,600)
DEFAULT_TARGET_GRID_BLOCKS: Final[int] = 10          # section 3.3: the moving-bottom time-to-target grid


def default_lambda_floor() -> Decimal:
    """ln2 / 7,200 per block (>= 50%/day)."""
    return DEC.divide(DEC.ln(Decimal(2)), Decimal(BLOCKS_PER_DAY))


# ---------------------------------------------------------------- ladder
def immunity_end(s: SubnetState, glob: ChainGlobals) -> Block:
    """First block at which the generation is no longer immune (reg_at + NetworkImmunityPeriod)."""
    return Block(s.key.reg_at + glob.immunity_period)


def is_immune(s: SubnetState, glob: ChainGlobals, block: Block) -> bool:
    return block < immunity_end(s, glob)


def ladder(snap: ChainSnapshot) -> tuple[SubnetKey, ...]:
    """Non-immune (block >= reg_at + NetworkImmunityPeriod), non-root, ascending (moving_price, reg_at). [0] = target."""
    cands = [s for s in snap.subnets if s.key.netuid != 0 and not is_immune(s, snap.glob, snap.block)]
    cands.sort(key=lambda s: (s.moving_price, s.key.reg_at, s.key.netuid))
    return tuple(s.key for s in cands)


def prune_target(snap: ChainSnapshot) -> SubnetKey | None:
    """ladder(snap)[0], or None when every subnet is immune."""
    lad = ladder(snap)
    return lad[0] if lad else None


def prune_rank(snap: ChainSnapshot, key: SubnetKey) -> int | None:
    """1-based position in the ladder (1 = target); None if immune or absent."""
    lad = ladder(snap)
    return lad.index(key) + 1 if key in lad else None


def prune_possible(glob: ChainGlobals) -> bool:
    """n_nonroot_networks + cleanup_queue_len >= SubnetLimit; otherwise a registration does not prune."""
    return glob.n_nonroot_networks + glob.cleanup_queue_len >= glob.subnet_limit


def _pool_spot(s: SubnetState) -> Decimal:
    return s.pool.spot() if s.pool.px_tao > 0 and s.pool.px_alpha > 0 else ZERO


def time_to_target(snap: ChainSnapshot, key: SubnetKey, spot_k: Decimal, horizon_blocks: int,
                   step_blocks: int = DEFAULT_TARGET_GRID_BLOCKS) -> int | None:
    """Stressed time-to-target with a moving bottom (section 3.3): the FIRST offset t in [0, horizon_blocks] at which
    `key` is ladder()[0], if its spot holds at `spot_k` (the stressed spot, e.g. (1 - D_STRESS) * spot) while every
    other subnet's spot holds at its current value. Every EMA follows the flat-spot closed form with its own a(b)
    (ema.project_ema; frozen EMAs stay flat); subnets whose immunity ends inside the horizon are admitted at their
    expiry block; ties are broken by (reg_at, netuid) as in ladder(). 0 if `key` is already the target; None if it
    does not become the target within the horizon, is absent, or stays immune throughout.

    Search: the section 3.3 grid 0, step, 2*step, ... plus the horizon block itself, k's immunity expiry and every
    rival's admission block (and the block before it), so neither the horizon nor a target window bounded by an
    admission is stepped over. At the first check point where k is the target, the preceding interval is scanned
    block by block, so the result is exact, never rounded UP to the grid (a Tier A trigger compares it with U + M_A;
    a late t* delays the emergency exit).

    Only competitors whose EMA path can reach k's path are evaluated (each flat-spot path is monotone, so a subnet
    whose lowest value over the horizon exceeds k's highest never blocks k)."""
    if horizon_blocks < 0 or step_blocks <= 0:
        raise ValueError("horizon must be >= 0 and the grid step > 0")
    glob = snap.glob
    sk = snap.get(key)
    if sk is None or int(key.netuid) == 0:
        return None
    end = snap.block + horizon_blocks
    k_open = immunity_end(sk, glob)
    if k_open > end:
        return None

    def path(s: SubnetState, t: int) -> Decimal:
        return project_ema(glob, s, snap.block, t, spot_k if s.key == key else _pool_spot(s))

    k_hi = max(sk.moving_price, path(sk, horizon_blocks))
    rivals: list[SubnetState] = []
    for s in snap.subnets:
        if s.key == key or int(s.key.netuid) == 0 or immunity_end(s, glob) > end:
            continue
        if min(s.moving_price, path(s, horizon_blocks)) <= k_hi:
            rivals.append(s)

    def is_target(t: int) -> bool:
        blk = snap.block + t
        if blk < k_open:
            return False
        mine = (path(sk, t), sk.key.reg_at, sk.key.netuid)
        return not any(blk >= immunity_end(s, glob) and (path(s, t), s.key.reg_at, s.key.netuid) < mine for s in rivals)

    points = set(range(0, horizon_blocks + 1, step_blocks))
    points.add(horizon_blocks)
    for opens in (k_open, *(immunity_end(s, glob) for s in rivals)):
        off = int(opens) - int(snap.block)
        points.update(o for o in (off - 1, off) if 0 <= o <= horizon_blocks)
    last = -1
    for t in sorted(points):
        if is_target(t):
            return next((u for u in range(last + 1, t) if is_target(u)), t)
        last = t
    return None


# ---------------------------------------------------------------- registration cost
def lock_reduction_interval_eff(glob: ChainGlobals) -> int:
    """I_eff = NetworkLockReductionInterval * block_emission / 1e9 (57,600 blocks at 0.5 TAO/block)."""
    return glob.lock_reduction_interval * glob.block_emission // RAO_PER_TAO


def registration_cost(glob: ChainGlobals, block: Block) -> Rao:
    """max(NetworkMinLockCost, 2L - (L / I_eff)*(block - last_reg_block)), I_eff = NetworkLockReductionInterval*block_emission/1e9."""
    last = glob.last_lock_cost
    mult = LOCK_COST_MULT if glob.last_reg_block != 0 else 1
    i_eff = lock_reduction_interval_eff(glob)
    per_block = last // i_eff if i_eff > 0 else 0
    elapsed = max(block - glob.last_reg_block, 0)
    cost = max(mult * last - per_block * elapsed, 0)
    return Rao(max(cost, glob.min_lock_cost))


def cost_ratio(glob: ChainGlobals, block: Block) -> Decimal:
    """r = registration_cost / NetworkLastLockCost (1.75 at window opening, 1.0 after ~8 days)."""
    if glob.last_lock_cost <= 0:
        return ZERO
    return DEC.divide(Decimal(registration_cost(glob, block)), Decimal(glob.last_lock_cost))


def window_open_block(glob: ChainGlobals) -> Block:
    """First block at which a registration is allowed again (last_reg_block + NetworkRateLimit)."""
    return Block(glob.last_reg_block + glob.network_rate_limit)


# ---------------------------------------------------------------- hazard
@dataclass(frozen=True, slots=True)
class HazardModel:
    cdf_by_r: tuple[tuple[Decimal, Decimal], ...]   # (r, F) points, r descending; see section 3.3
    p_open: Decimal                                  # mass at window opening (0.0625; 0.5 in a hot market)
    n0: int                                          # prior pseudo-count (4)
    lambda_floor_per_block: Decimal                  # ln2/7200 once r <= 1.045
    valid: bool                                      # False -> fall back to window rule + constant floor hazard


def _check_cdf(points: Sequence[tuple[Decimal, Decimal]]) -> None:
    if not points:
        raise ValueError("empty hazard CDF")
    for (r0, f0), (r1, f1) in pairwise(points):
        if not (r1 < r0 and f1 >= f0):
            raise ValueError("hazard CDF must have strictly descending r and non-decreasing F")
    if points[0][1] < 0 or points[-1][1] > ONE:
        raise ValueError("hazard CDF values must lie in [0, 1]")


def interp_cdf(points: Sequence[tuple[Decimal, Decimal]], r: Decimal) -> Decimal:
    """Piecewise-linear F(r) on r-descending points; flat beyond both ends."""
    if r >= points[0][0]:
        return points[0][1]
    if r <= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1                     # invariant: points[lo].r > r > points[hi].r
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] >= r:
            lo = mid
        else:
            hi = mid
    (r0, f0), (r1, f1) = points[lo], points[hi]
    if r == r0:
        return f0
    w = DEC.divide(DEC.subtract(r0, r), DEC.subtract(r0, r1))
    return DEC.add(f0, DEC.multiply(w, DEC.subtract(f1, f0)))


def model_cdf(model: HazardModel, r: Decimal) -> Decimal:
    """F(r) of the model, with the opening mass raised to p_open when p_open exceeds the CDF's first point: the
    remaining mass follows the base model's post-opening distribution."""
    base = interp_cdf(model.cdf_by_r, r)
    f_open = model.cdf_by_r[0][1]
    if model.p_open > f_open and f_open < ONE:
        tail = DEC.divide(DEC.subtract(base, f_open), DEC.subtract(ONE, f_open))
        return DEC.add(model.p_open, DEC.multiply(DEC.subtract(ONE, model.p_open), tail))
    return base


def _cdf_at_block(glob: ChainGlobals, model: HazardModel, t: int) -> Decimal:
    if t < window_open_block(glob):
        return ZERO
    return model_cdf(model, cost_ratio(glob, Block(t)))


def first_floor_block(glob: ChainGlobals) -> Block | None:
    """First block >= window opening with cost_ratio <= TAIL_FLOOR_R (start of the tail-floor region); None if the
    ratio never falls that low."""
    lo = int(window_open_block(glob))
    if cost_ratio(glob, Block(lo)) <= TAIL_FLOOR_R:
        return Block(lo)
    i_eff = lock_reduction_interval_eff(glob)
    per_block = glob.last_lock_cost // i_eff if i_eff > 0 else 0
    if per_block <= 0:
        return None
    hi = glob.last_reg_block + (LOCK_COST_MULT * glob.last_lock_cost) // per_block + 1   # cost has hit the floor
    if cost_ratio(glob, Block(hi)) > TAIL_FLOOR_R:
        return None
    while hi - lo > 1:                              # r(lo) > floor >= r(hi); r is non-increasing in t
        mid = (lo + hi) // 2
        if cost_ratio(glob, Block(mid)) <= TAIL_FLOOR_R:
            hi = mid
        else:
            lo = mid
    return Block(hi)


def p_registration(glob: ChainGlobals, model: HazardModel, block: Block, horizon_blocks: int) -> Decimal:
    """P(a registration lands in (block, block+horizon] | none since last_reg_block). 0 inside the rate limit
    or when not prune_possible.

    F is 0 before the window opens; the opening mass sits at the opening block itself. Once r <= TAIL_FLOOR_R the
    hazard is floored PER BLOCK: every block step (t, t+1] with t+1 >= first_floor_block survives with
    min(S(t+1)/S(t), exp(-lambda_floor_per_block)). This is exact (see _floored_stretch), so the result does not
    depend on where the query starts: P(t0, t2) = 1 - (1 - P(t0, t1))(1 - P(t1, t2)). An invalid model gives the
    window rule plus the constant floor hazard."""
    if horizon_blocks <= 0 or not prune_possible(glob):
        return ZERO
    t0, t1 = int(block), int(block) + horizon_blocks
    opening = int(window_open_block(glob))
    if t1 < opening:
        return ZERO
    lam = model.lambda_floor_per_block
    if not model.valid:
        start = max(t0, opening - 1)
        return DEC.subtract(ONE, DEC.exp(DEC.minus(DEC.multiply(lam, Decimal(t1 - start)))))

    cache: dict[int, Decimal] = {}

    def surv(t: int) -> Decimal:
        s = cache.get(t)
        if s is None:
            s = cache[t] = DEC.subtract(ONE, _cdf_at_block(glob, model, t))
        return s

    s0 = surv(t0)
    if s0 <= 0:
        return ONE
    tf = first_floor_block(glob)
    if tf is None or t1 < tf or lam <= 0:
        return _clamp01(DEC.subtract(ONE, DEC.divide(surv(t1), s0)))
    a = max(t0, int(tf) - 1)
    total = DEC.divide(surv(a), s0) if a > t0 else ONE
    cuts = {a, t1}
    for kb in _knot_blocks(glob, model):
        cuts.update(c for c in (kb, kb + 1) if a < c < t1)
    step_floor = DEC.exp(DEC.minus(lam))
    for b, e in pairwise(sorted(cuts)):
        if total <= 0:
            break
        total = DEC.multiply(total, _floored_stretch(surv, b, e, lam, step_floor))
    return _clamp01(DEC.subtract(ONE, total))


def _knot_blocks(glob: ChainGlobals, model: HazardModel) -> list[int]:
    """floor of every block at which S(t) = 1 - F(r(t)) can change slope: the window opening, each r point of the
    model's CDF mapped through the (linear) lock-cost line, and the min-lock kink. Between two consecutive knots S is
    linear in t (F is piecewise-linear in r, r linear in t, and the hot-market p_open transform is affine)."""
    out = [int(window_open_block(glob))]
    i_eff = lock_reduction_interval_eff(glob)
    per_block = glob.last_lock_cost // i_eff if i_eff > 0 else 0
    if per_block <= 0 or glob.last_lock_cost <= 0:
        return out
    mult = LOCK_COST_MULT if glob.last_reg_block != 0 else 1
    top = Decimal(mult * glob.last_lock_cost)
    costs = [DEC.multiply(r, Decimal(glob.last_lock_cost)) for r, _ in model.cdf_by_r]
    costs += [Decimal(glob.min_lock_cost), ZERO]
    for c in costs:
        elapsed = DEC.divide(DEC.subtract(top, c), Decimal(per_block))
        if elapsed >= 0:
            out.append(int(glob.last_reg_block) + floor_int(elapsed))
    return out


def _floored_stretch(surv: Callable[[int], Decimal], b: int, e: int, lam: Decimal, step_floor: Decimal) -> Decimal:
    """prod over t in [b, e) of min(S(t+1)/S(t), step_floor = exp(-lam)), exact when S is linear on [b, e] (no knot
    strictly inside a stretch longer than one block; see _knot_blocks). On a linear stretch S(t+1)/S(t) = 1 - k/S(t) is
    non-increasing, so the floor binds on a prefix [b, c) and the model on [c, e), with c the first block where
    S(t) <= k/(1 - step_floor)."""
    s_b = surv(b)
    if s_b <= 0:
        return ZERO
    if e - b == 1:
        return min(DEC.divide(surv(e), s_b), step_floor)
    k = DEC.subtract(s_b, surv(b + 1))
    if k <= 0:
        c = e                                       # flat survival: the floor binds on every block
    else:
        need = DEC.divide(DEC.subtract(s_b, DEC.divide(k, DEC.subtract(ONE, step_floor))), k)
        c = min(e, b + max(0, int(need.to_integral_value(rounding=ROUND_CEILING))))
    head = ONE if c == b else DEC.exp(DEC.minus(DEC.multiply(lam, Decimal(c - b))))
    if c == e:
        return head
    s_c = surv(c)
    return DEC.multiply(head, DEC.divide(surv(e), s_c)) if s_c > 0 else ZERO


def _clamp01(x: Decimal) -> Decimal:
    return min(max(x, ZERO), ONE)


@dataclass(frozen=True, slots=True)
class RegistrationRow:
    """One registration (lake `registration` table)."""
    queued_block: Block
    victim_netuid: NetUid | None
    cost_ratio: Decimal                 # r = cost / NetworkLastLockCost at the registration block
    blocks_since_prev: int


def delta_prior_cdf(r: Decimal, *, rate_limit_blocks: int, i_eff_blocks: int, prior_scale_blocks: int) -> Decimal:
    """Section 3.3 parametric prior F_prior = 1 - exp(-(Delta - NetworkRateLimit)/scale), expressed in r through
    Delta = (2 - r) * I_eff (the calibration period's lock-cost line)."""
    delta = DEC.multiply(DEC.subtract(Decimal(LOCK_COST_MULT), r), Decimal(i_eff_blocks))
    excess = DEC.subtract(delta, Decimal(rate_limit_blocks))
    if excess <= 0:
        return ZERO
    return DEC.subtract(ONE, DEC.exp(DEC.minus(DEC.divide(excess, Decimal(prior_scale_blocks)))))


def hazard_from_table(cdf_r: Sequence[Decimal], cdf_f: Sequence[Decimal], n_obs: int, *, n0: int,
                      rate_limit_blocks: int, i_eff_blocks: int, prior_scale_blocks: int,
                      lambda_floor_per_block: Decimal | None = None, grid_step: Decimal = DEFAULT_GRID_STEP,
                      valid: bool = True) -> HazardModel:
    """The frozen section-3.3 model (paper and live only; backtests use fit_hazard as-of): F = (n*F_emp + n0*F_prior)
    / (n + n0) with the table as F_emp (piecewise-linear in r) and the Delta-prior, baked onto an r grid of
    `grid_step` plus the table points. p_open is the smoothed opening mass (the table's 0.0625 smoothed to 0.0556);
    the hot-market override is apply_hot_market. Inputs come from config/preregistration.toml [prune.hazard]
    (cdf_r, cdf_f, n_registrations, n0_prior, prior_offset_blocks = NetworkRateLimit, prior_scale_blocks) and the
    calibration period's I_eff (57,600)."""
    if len(cdf_r) != len(cdf_f) or n_obs <= 0 or n0 < 0 or grid_step <= 0:
        raise ValueError("bad hazard table")
    emp = tuple(zip(cdf_r, cdf_f, strict=True))
    _check_cdf(emp)
    grid: set[Decimal] = set(cdf_r)
    r = emp[0][0]
    while r > emp[-1][0]:
        grid.add(r)
        r = DEC.subtract(r, grid_step)
    points: list[tuple[Decimal, Decimal]] = []
    for g in sorted(grid, reverse=True):
        prior = delta_prior_cdf(g, rate_limit_blocks=rate_limit_blocks, i_eff_blocks=i_eff_blocks,
                                prior_scale_blocks=prior_scale_blocks)
        f = DEC.divide(DEC.add(DEC.multiply(Decimal(n_obs), interp_cdf(emp, g)), DEC.multiply(Decimal(n0), prior)),
                       Decimal(n_obs + n0))
        if points and f < points[-1][1]:
            f = points[-1][1]                        # keep the CDF monotone through rounding
        points.append((g, min(f, ONE)))
    lam = lambda_floor_per_block if lambda_floor_per_block is not None else default_lambda_floor()
    return HazardModel(cdf_by_r=tuple(points), p_open=points[0][1], n0=n0, lambda_floor_per_block=lam, valid=valid)


def apply_hot_market(model: HazardModel, recent_cost_ratios: Sequence[Decimal]) -> HazardModel:
    """Hot market (section 3.3): if the last HOT_MARKET_LAST_N registrations (oldest first) all paid
    r >= HOT_MARKET_R_MIN, P_OPEN = 0.5; otherwise P_OPEN returns to the CDF's opening mass."""
    last = list(recent_cost_ratios)[-HOT_MARKET_LAST_N:]
    hot = len(last) == HOT_MARKET_LAST_N and all(r >= HOT_MARKET_R_MIN for r in last)
    base = model.cdf_by_r[0][1]
    return replace(model, p_open=max(HOT_MARKET_P_OPEN, base) if hot else base)


def fit_hazard(regs: Sequence[RegistrationRow], asof: Block, prior: HazardModel) -> HazardModel:
    """As-of refit of the section 3.3 CDF: uses ONLY rows with queued_block < asof (asserted), smoothed toward
    `prior` with n0 pseudo-counts; returns `prior` unchanged with fewer than 8 rows. Hot-market and validity rules
    as in section 3.3. Deterministic.

    F_new(r) = (n*F_emp(r) + n0*F_prior(r)) / (n + n0), F_emp(r) = share of rows with cost_ratio >= r, evaluated on
    the prior's r points plus every row's r. A refit model is valid; when the prior was invalidated (registration
    economics changed), the CALLER passes only registrations after the invalidation, so the model becomes valid again
    only after 8 new registrations."""
    rows = sorted((g for g in regs if g.queued_block < asof),
                  key=lambda g: (g.queued_block, g.cost_ratio, g.blocks_since_prev))
    if len(rows) < REFIT_MIN_ROWS:
        return prior
    assert all(g.queued_block < asof for g in rows)
    n = len(rows)
    ratios = sorted((g.cost_ratio for g in rows), reverse=True)
    grid = sorted({p[0] for p in prior.cdf_by_r} | set(ratios), reverse=True)
    points: list[tuple[Decimal, Decimal]] = []
    for r in grid:
        emp = DEC.divide(Decimal(sum(1 for x in ratios if x >= r)), Decimal(n))
        f = DEC.divide(DEC.add(DEC.multiply(Decimal(n), emp), DEC.multiply(Decimal(prior.n0), interp_cdf(prior.cdf_by_r, r))),
                       Decimal(n + prior.n0))
        if points and f < points[-1][1]:
            f = points[-1][1]
        points.append((r, min(f, ONE)))
    fitted = HazardModel(cdf_by_r=tuple(points), p_open=points[0][1], n0=prior.n0,
                         lambda_floor_per_block=prior.lambda_floor_per_block, valid=True)
    return apply_hot_market(fitted, [g.cost_ratio for g in rows])


# ---------------------------------------------------------------- recovery
def staker_base(s: SubnetState) -> int:
    """S of the recovery formula: max(TotalAlphaStaked, AlphaOut - ProtocolAlpha), the larger (conservative) of the
    two readings of section 3.3 (see recovery_ratio). TotalAlphaStaked absent (before spec 448) -> the fallback."""
    fallback = s.alpha_out - s.protocol_alpha
    if s.total_alpha_staked is None:
        return fallback
    return max(int(s.total_alpha_staked), fallback)


def recovery_ratio(s: SubnetState, glob: ChainGlobals, r_default: Decimal) -> Decimal:
    """Dissolution payout per alpha / spot. Baskets sold first: T_after = T*(x/(x+E))**(w_b/w_q);
    denom = (S - E) + P + ((x + E) if reg_at > TaoInRefundDeploymentBlock else 0), S = TotalAlphaStaked
    (fallback alpha_out - protocol_alpha); clamp to [0,1]. SN92 golden: ~0.368.

    T and x include the balancer reservoirs (dissolution phase 0 folds them into the reserves). The protocol term
    applies only when TaoInRefundDeploymentBlock is set (> 0) and reg_at is after it (legacy rule otherwise).
    r_default is returned when the formula is undefined (empty staker base or no price); an empty TAO pot gives 0.

    Staker base S (FAIL-CLOSED, pending FT10 / section 13 Q10): S = max(TotalAlphaStaked, AlphaOut - ProtocolAlpha).
    The section 3.3 / 10.1 golden (SN92 0.368, 0.41 before the basket sale) reproduces only with AlphaOut -
    ProtocolAlpha; TotalAlphaStaked on the same SN92 state gives 0.469 (0.52 before the sale), because AlphaOut also
    counts alpha no staker owns. The larger base gives the LOWER recovery, which is conservative for every consumer
    (expected prune loss, s_emerg, modelled DeregSettled payouts). See staker_base()."""
    tao = s.pool.tao + s.reservoir_tao
    x = s.pool.alpha + s.reservoir_alpha
    if tao <= 0:
        return ZERO
    if x <= 0 or s.pool.px_alpha <= 0 or s.pool.px_tao <= 0:
        return r_default
    spot = s.pool.spot()
    e = max(s.escrow_alpha or 0, 0)
    staked = staker_base(s)
    expo = DEC.divide(Decimal(s.pool.w_base_e18), Decimal(s.pool.w_quote_e18))
    t_after = DEC.multiply(Decimal(tao), DEC.power(DEC.divide(Decimal(x), Decimal(x + e)), expo)) if e > 0 else Decimal(tao)
    refund_rule = glob.tao_in_refund_block > 0 and s.key.reg_at > glob.tao_in_refund_block
    denom = (staked - e) + s.protocol_alpha + ((x + e) if refund_rule else 0)
    if denom <= 0 or spot <= 0:
        return r_default
    ratio = DEC.divide(DEC.divide(t_after, Decimal(denom)), spot)
    return _clamp01(ratio)


def blocks_to_ratio(glob: ChainGlobals, r: Decimal) -> int | None:
    """Delta (blocks since the last registration) at which cost_ratio first falls to <= r; None if never."""
    i_eff = lock_reduction_interval_eff(glob)
    per_block = glob.last_lock_cost // i_eff if i_eff > 0 else 0
    if glob.last_lock_cost <= 0 or per_block <= 0:
        return None
    mult = LOCK_COST_MULT if glob.last_reg_block != 0 else 1
    need = DEC.subtract(Decimal(mult * glob.last_lock_cost), DEC.multiply(r, Decimal(glob.last_lock_cost)))
    if need <= 0:
        return 0
    d = int(DEC.divide(need, Decimal(per_block)).to_integral_value(rounding=ROUND_CEILING))
    if cost_ratio(glob, Block(glob.last_reg_block + d)) > r:
        return None
    return d
