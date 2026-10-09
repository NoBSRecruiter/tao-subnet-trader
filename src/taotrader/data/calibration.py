"""taotrader/data/calibration.py - calibration providers (WP4; DESIGN.md sections 3.3, 5.12, 8.10, 11 WP4).

Two implementations of protocol.calibration.CalibrationProvider:

- `LakeCalibrationProvider` (backtests; paper/live on recorded history): every calibrated input is fit AS OF the
  requested block from the lake's registration and generation tables (data.refine), using only events with
  block < asof:
  * hazard: protocol.prune.fit_hazard(registrations before asof, asof, prior) with a lookahead-free prior - the
    section 3.3 parametric Delta-prior F = 1 - exp(-(Delta - NetworkRateLimit)/scale) mapped to the cost ratio r
    through the I_eff in force before asof (NetworkRateLimit and I_eff read from the stored globals), n0 from the
    preregistration. Fewer than 8 rows return the prior unchanged (fit_hazard). After `hazard_invalid_from`
    (spec-475 PoW registration, set by the lead) only later registrations count and the model is valid=False until 8
    of them exist (section 3.3 invalidation path).
  * kappa_p (P_bottom = exp(-kappa_p (rho - 1))): conditional-logit maximum likelihood over the prunes before asof -
    for each prune P the non-immune ladder one day before P (rho_k = EMA_k / EMA_bottom, the 15 lowest plus the
    victim); the score is monotone in kappa, solved by bisection on the preregistered range [2, 8] in Decimal;
    the prior (4) until 10 prunes exist, then shrunk toward it with 4 pseudo-observations and quantized to 0.001.
  * R / FT10: r_default = 0.35 (preregistered); r_cap_formula = False only when every dissolution before asof with
    an observed payout ratio (generation.observed_payout_ratio, FT10) is within 0.05 of the formula at removal-1;
    no evidence keeps the cap (fail closed).
  * Tier B jumps: p_day from the share f of prunes whose victim ranked outside the bottom 3 two days before P
    (p = -ln(1 - f) / 2, clipped to [0.005, 0.2]); the prior (0.03/day) until 10 prunes; size stays ln(1 - 0.5).
  * phi: injected T3 measurements (block, SellLoadParams) - the latest one before asof - else the preregistered
    priors.
  Calibrations are cached per refit point: asof(t) returns the calibration of the last event-or-parameter change
  block + 1 <= t, sealed with that block as its `asof` (so check_asof(c, t) holds and the digest only changes when an
  input does). Perturbing or deleting any event after t cannot change asof(t) (section 8.10 bias test).
- `FrozenCalibration` (paper and live only; refuses RunMode.BACKTEST): the preregistered section 3.3 values -
  protocol.prune.hazard_from_table on [prune.hazard] with the hot-market rule on the recent registrations supplied by
  the caller, kappa_p = 4, R_DEFAULT 0.35 with the formula capped until FT10 passes, Tier B priors, phi priors.
"""
from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from ..core.fixed import DEC
from ..core.state import ChainSnapshot
from ..core.units import BLOCKS_PER_DAY, PPM, RAO_PER_TAO, Block, NetUid, Ppm, RunMode, SubnetKey
from ..ops.config_load import PREREGISTRATION, load_preregistration
from ..protocol.calibration import Calibration, sealed
from ..protocol.prune import (
    REFIT_MIN_ROWS,
    HazardModel,
    RegistrationRow,
    apply_hot_market,
    default_lambda_floor,
    delta_prior_cdf,
    fit_hazard,
    hazard_from_table,
    ladder,
    prune_rank,
    recovery_ratio,
)
from ..protocol.sellload import SellLoadParams
from .lake import Lake
from .store import LakeSnapshotStore

ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)
FROZEN_I_EFF_BLOCKS: Final[int] = 57_600        # I_eff of the hazard table's calibration period (brief 4.1)
PRIOR_GRID_STEP: Final[Decimal] = Decimal("0.01")
KAPPA_LADDER_K: Final[int] = 15                 # candidates per prune in the kappa_p likelihood (plus the victim)
KAPPA_N0: Final[int] = 4                        # pseudo-observations of the kappa_p prior
KAPPA_TOL: Final[Decimal] = Decimal("0.0005")
KAPPA_QUANTUM: Final[Decimal] = Decimal("0.001")
TIER_B_BOTTOM: Final[int] = 3                   # "outside the bottom 3 two days before the prune"
TIER_B_LOOKBACK_DAYS: Final[int] = 2
TIER_B_P_CLIP: Final[tuple[Decimal, Decimal]] = (Decimal("0.005"), Decimal("0.2"))
TIER_B_QUANTUM: Final[Decimal] = Decimal("0.0001")
FT10_TOLERANCE: Final[Decimal] = Decimal("0.05")


def _dec(v: Any) -> Decimal:
    """TOML numbers -> exact decimal text (floats through their shortest repr)."""
    if isinstance(v, bool):
        raise TypeError("boolean where a number was expected")
    if isinstance(v, (int, float, str)):
        return Decimal(str(v))
    raise TypeError(f"not a number: {v!r}")


def _ppm(d: Decimal) -> Ppm:
    return Ppm(int(DEC.multiply(d, Decimal(PPM)).to_integral_value()))


def _ln_one_minus(text: str) -> Decimal:
    """'ln(1 - 0.5)' -> ln(0.5) (the preregistered Tier B jump size)."""
    t = text.replace(" ", "")
    if not (t.startswith("ln(1-") and t.endswith(")")):
        raise ValueError(f"unsupported jump_log_size {text!r}")
    return DEC.ln(DEC.subtract(ONE, Decimal(t[len("ln(1-"):-1])))


# ------------------------------------------------------------------------------------------------ preregistered values
@dataclass(frozen=True, slots=True)
class PreregCalib:
    """The calibration-relevant preregistered values (config/preregistration.toml)."""
    cdf_r: tuple[Decimal, ...]
    cdf_f: tuple[Decimal, ...]
    n_registrations: int
    n0: int
    prior_offset_blocks: int
    prior_scale_blocks: int
    refit_min_rows: int
    kappa_p: Decimal
    kappa_p_range: tuple[Decimal, Decimal]
    r_default: Decimal
    tier_b_jump_p_day: Decimal
    tier_b_jump_size: Decimal
    tier_b_prior_until_prunes: int
    phi: SellLoadParams

    @classmethod
    def from_prereg(cls, pre: Mapping[str, Any]) -> PreregCalib:
        hz = pre["prune"]["hazard"]
        tb = pre["prune"]["tier_b"]
        cp = pre["carry"]["params"]
        lo, hi = cp["kappa_p"]["range"]
        return cls(
            cdf_r=tuple(_dec(x) for x in hz["cdf_r"]), cdf_f=tuple(_dec(x) for x in hz["cdf_f"]),
            n_registrations=int(hz["n_registrations"]), n0=int(hz["n0_prior"]),
            prior_offset_blocks=int(hz["prior_offset_blocks"]), prior_scale_blocks=int(hz["prior_scale_blocks"]),
            refit_min_rows=int(hz["refit_min_rows"]),
            kappa_p=_dec(cp["kappa_p"]["default"]), kappa_p_range=(_dec(lo), _dec(hi)),
            r_default=_dec(pre["prune"]["r_default"]),
            tier_b_jump_p_day=_dec(tb["jump_p_per_day"]), tier_b_jump_size=_ln_one_minus(str(tb["jump_log_size"])),
            tier_b_prior_until_prunes=int(tb["asof_prior_until_prunes"]),
            phi=SellLoadParams(phi_owner_ppm=_ppm(_dec(cp["phi_owner"]["default"])),
                               phi_miner_ppm=_ppm(_dec(cp["phi_miner"]["default"])),
                               kappa_basket_ppm_day=_ppm(_dec(cp["kappa_basket_per_day"]["default"]))))

    @classmethod
    def load(cls, path: str | Path = PREREGISTRATION) -> PreregCalib:
        return cls.from_prereg(load_preregistration(path))


def frozen_hazard(pc: PreregCalib, i_eff_blocks: int = FROZEN_I_EFF_BLOCKS) -> HazardModel:
    """The section 3.3 frozen table model (paper and live)."""
    return hazard_from_table(pc.cdf_r, pc.cdf_f, pc.n_registrations, n0=pc.n0, rate_limit_blocks=pc.prior_offset_blocks,
                             i_eff_blocks=i_eff_blocks, prior_scale_blocks=pc.prior_scale_blocks)


def prior_hazard(rate_limit_blocks: int, i_eff_blocks: int, scale_blocks: int, n0: int,
                 step: Decimal = PRIOR_GRID_STEP) -> HazardModel:
    """Lookahead-free hazard prior: the parametric Delta-prior alone, on an r grid from the window opening
    (r_open = 2 - NetworkRateLimit/I_eff) down to the r where it reaches ~1 (or 0)."""
    if i_eff_blocks <= 0 or scale_blocks <= 0:
        raise ValueError("I_eff and the prior scale must be positive")
    r_open = DEC.subtract(Decimal(2), DEC.divide(Decimal(rate_limit_blocks), Decimal(i_eff_blocks)))
    r_end = max(DEC.subtract(r_open, DEC.divide(Decimal(12 * scale_blocks), Decimal(i_eff_blocks))), ZERO)
    points: list[tuple[Decimal, Decimal]] = []
    r = r_open
    while r > r_end:
        f = delta_prior_cdf(r, rate_limit_blocks=rate_limit_blocks, i_eff_blocks=i_eff_blocks, prior_scale_blocks=scale_blocks)
        if points and f < points[-1][1]:
            f = points[-1][1]
        points.append((r, min(f, ONE)))
        r = DEC.subtract(r, step)
    points.append((r_end, points[-1][1] if points else ZERO))
    if len(points) < 2 or points[-1][0] >= points[-2][0]:
        points = [(r_open, ZERO), (DEC.subtract(r_open, step), ZERO)]
    return HazardModel(cdf_by_r=tuple(points), p_open=points[0][1], n0=n0, lambda_floor_per_block=default_lambda_floor(),
                       valid=True)


# ------------------------------------------------------------------------------------------------ inputs (events)
@dataclass(frozen=True, slots=True)
class ParamPoint:
    block: int               # stored snapshot block where the value was (first) observed
    rate_limit: int          # NetworkRateLimit
    i_eff: int               # NetworkLockReductionInterval * block_emission / 1e9


@dataclass(frozen=True, slots=True)
class PruneObs:
    block: int                       # removal block P
    rhos: tuple[Decimal, ...]        # rho of the KAPPA_LADDER_K lowest candidates one day before P (ascending)
    victim_rho: Decimal | None       # None: the victim was not a ranked candidate one day before (excluded from kappa)
    rank_2d: int | None              # victim's prune rank two days before P (None: immune or absent then)


@dataclass(frozen=True, slots=True)
class DissolutionObs:
    block: int                       # removal block
    predicted: Decimal               # recovery_ratio at removal-1
    observed: Decimal                # FT10 observed payout ratio


@dataclass(frozen=True, slots=True)
class CalibrationInputs:
    registrations: tuple[RegistrationRow, ...] = ()
    params: tuple[ParamPoint, ...] = ()
    prunes: tuple[PruneObs, ...] = ()
    dissolutions: tuple[DissolutionObs, ...] = ()
    phi: tuple[tuple[int, SellLoadParams], ...] = ()

    @classmethod
    def from_lake(cls, lake: Lake, *, r_default: Decimal, phi: Sequence[tuple[int, SellLoadParams]] = ()) -> CalibrationInputs:
        """Read the registration and generation tables, the parameter history and the ladder states one and two
        days before each prune (stored snapshots) from the lake."""
        con = lake.connect()
        try:
            regs = con.execute("SELECT queued_block, victim_netuid, cost_ratio, blocks_since_prev FROM v_registration "
                               "WHERE cost_ratio IS NOT NULL ORDER BY queued_block").fetchall()
            gl = con.execute("SELECT block, any_value(network_rate_limit), any_value(lock_reduction_interval), "
                             "any_value(block_emission) FROM v_global GROUP BY block ORDER BY block").fetchall()
            gens = con.execute("SELECT netuid, reg_at, end_block, end_kind, observed_payout_ratio FROM v_generation "
                               "WHERE end_block IS NOT NULL ORDER BY end_block, netuid").fetchall()
        finally:
            con.close()
        rows = tuple(RegistrationRow(Block(int(q)), None if v is None else NetUid(int(v)), Decimal(repr(float(c))),
                                     int(d or 0)) for q, v, c, d in regs)
        params: list[ParamPoint] = []
        for b, rl, lri, em in gl:
            p = ParamPoint(int(b), int(rl or 0), int(lri or 0) * int(em or 0) // RAO_PER_TAO)
            if not params or (params[-1].rate_limit, params[-1].i_eff) != (p.rate_limit, p.i_eff):
                params.append(p)
        prunes: list[PruneObs] = []
        diss: list[DissolutionObs] = []
        if gens:
            store = LakeSnapshotStore(lake, clock=max(int(g[2]) for g in gens))
            for n, r, e, kind, obs in gens:
                key = SubnetKey(NetUid(int(n)), Block(int(r)))
                if kind == "pruned":
                    po = prune_obs(store, key, int(e))
                    if po is not None:
                        prunes.append(po)
                if obs is not None:
                    d = dissolution_obs(store, key, int(e), Decimal(repr(float(obs))), r_default)
                    if d is not None:
                        diss.append(d)
        return cls(rows, tuple(params), tuple(prunes), tuple(diss), tuple(sorted(phi, key=lambda x: x[0])))


def _snap_before(store: LakeSnapshotStore, block: int) -> ChainSnapshot | None:
    try:
        return store.at_or_before(Block(block))
    except KeyError:
        return None


def prune_obs(store: LakeSnapshotStore, victim: SubnetKey, block: int) -> PruneObs | None:
    """Ladder state one day (kappa_p) and two days (Tier B) before a prune, from stored snapshots before it."""
    s1 = _snap_before(store, block - BLOCKS_PER_DAY)
    if s1 is None:
        return None
    lad = ladder(s1)
    if not lad:
        return None
    bottom = s1.get(lad[0])
    if bottom is None or bottom.moving_price <= 0:
        return None
    rho: dict[SubnetKey, Decimal] = {}
    for k in lad:
        s = s1.get(k)
        if s is not None:
            rho[k] = DEC.divide(s.moving_price, bottom.moving_price)
    rhos = [rho[k] for k in lad[:KAPPA_LADDER_K] if k in rho]
    vr = rho.get(victim)
    if vr is not None and victim not in lad[:KAPPA_LADDER_K]:
        rhos.append(vr)
    s2 = _snap_before(store, block - TIER_B_LOOKBACK_DAYS * BLOCKS_PER_DAY)
    rank = None if s2 is None else prune_rank(s2, victim)
    return PruneObs(block, tuple(sorted(rhos)), vr, rank)


def dissolution_obs(store: LakeSnapshotStore, key: SubnetKey, block: int, observed: Decimal,
                    r_default: Decimal) -> DissolutionObs | None:
    s = _snap_before(store, block - 1)
    if s is None:
        return None
    sub = s.get(key)
    if sub is None:
        return None
    return DissolutionObs(block, recovery_ratio(sub, s.glob, r_default), observed)


# ------------------------------------------------------------------------------------------------ fits (pure)
def _score(prunes: Sequence[PruneObs], kappa: Decimal) -> Decimal:
    """d logL / d kappa of the conditional logit P(victim) ~ exp(-kappa (rho - 1)): sum(E_kappa[rho] - rho_victim)."""
    total = ZERO
    for p in prunes:
        assert p.victim_rho is not None
        ws = [DEC.exp(DEC.minus(DEC.multiply(kappa, DEC.subtract(r, ONE)))) for r in p.rhos]
        z = DEC.add(ZERO, sum(ws, ZERO))
        mean = DEC.divide(sum((DEC.multiply(w, r) for w, r in zip(ws, p.rhos, strict=True)), ZERO), z)
        total = DEC.add(total, DEC.subtract(mean, p.victim_rho))
    return total


def fit_kappa(prunes: Sequence[PruneObs], prior: Decimal, rng: tuple[Decimal, Decimal], min_prunes: int) -> Decimal:
    """kappa_p as of a block (see the module docstring); `prunes` must already be restricted to block < asof."""
    usable = [p for p in prunes if p.victim_rho is not None and len(p.rhos) >= 2]
    if len(usable) < min_prunes:
        return prior
    lo, hi = rng
    if _score(usable, lo) <= 0:
        mle = lo
    elif _score(usable, hi) >= 0:
        mle = hi
    else:
        a, z = lo, hi
        while DEC.subtract(z, a) > KAPPA_TOL:
            mid = DEC.divide(DEC.add(a, z), Decimal(2))
            if _score(usable, mid) > 0:
                a = mid
            else:
                z = mid
        mle = DEC.divide(DEC.add(a, z), Decimal(2))
    n = Decimal(len(usable))
    k = DEC.divide(DEC.add(DEC.multiply(n, mle), DEC.multiply(Decimal(KAPPA_N0), prior)), DEC.add(n, Decimal(KAPPA_N0)))
    return min(max(k, lo), hi).quantize(KAPPA_QUANTUM, context=DEC)


def fit_tier_b_jump(prunes: Sequence[PruneObs], prior: Decimal, min_prunes: int) -> Decimal:
    ranked = [p for p in prunes if p.rank_2d is not None]
    if len(ranked) < min_prunes:
        return prior
    f = DEC.divide(Decimal(sum(1 for p in ranked if p.rank_2d is not None and p.rank_2d > TIER_B_BOTTOM)), Decimal(len(ranked)))
    if f >= ONE:
        return TIER_B_P_CLIP[1]
    p_day = DEC.divide(DEC.minus(DEC.ln(DEC.subtract(ONE, f))), Decimal(TIER_B_LOOKBACK_DAYS))
    lo, hi = TIER_B_P_CLIP
    return min(max(p_day, lo), hi).quantize(TIER_B_QUANTUM, context=DEC)


def ft10_cap(dissolutions: Sequence[DissolutionObs]) -> bool:
    """r_cap_formula: False only when every observed dissolution is within FT10_TOLERANCE of the formula."""
    if not dissolutions:
        return True
    return any(abs(DEC.subtract(d.predicted, d.observed)) > FT10_TOLERANCE for d in dissolutions)


# ------------------------------------------------------------------------------------------------ providers
class LakeCalibrationProvider:
    """As-of calibration from the lake (see the module docstring)."""

    def __init__(self, inputs: CalibrationInputs, *, prereg: PreregCalib | None = None,
                 hazard_invalid_from: int | None = None) -> None:
        self.pre = prereg or PreregCalib.load()
        self.inputs = inputs
        self.hazard_invalid_from = hazard_invalid_from
        pts = {r.queued_block + 1 for r in inputs.registrations}
        pts |= {p.block + 1 for p in inputs.prunes}
        pts |= {d.block + 1 for d in inputs.dissolutions}
        pts |= {p.block + 1 for p in inputs.params}
        pts |= {b + 1 for b, _ in inputs.phi}
        if hazard_invalid_from is not None:
            pts.add(hazard_invalid_from)
        self._points: list[int] = sorted(pts)
        self._cache: dict[int, Calibration] = {}
        self._hazard_memo: dict[tuple[Any, ...], HazardModel] = {}
        self._kappa_memo: dict[int, Decimal] = {}

    @classmethod
    def from_lake(cls, lake: Lake, *, prereg: PreregCalib | None = None, hazard_invalid_from: int | None = None,
                  phi: Sequence[tuple[int, SellLoadParams]] = ()) -> LakeCalibrationProvider:
        pre = prereg or PreregCalib.load()
        return cls(CalibrationInputs.from_lake(lake, r_default=pre.r_default, phi=phi), prereg=pre,
                   hazard_invalid_from=hazard_invalid_from)

    def refit_point(self, block: int) -> int:
        i = bisect.bisect_right(self._points, block)
        return self._points[i - 1] if i > 0 else 0

    def asof(self, block: Block) -> Calibration:
        r = self.refit_point(int(block))
        c = self._cache.get(r)
        if c is None:
            c = self._cache[r] = self._fit(r)
        return c

    # ---------------------------------------------------------------- fits as of a refit point
    def _fit(self, t: int) -> Calibration:
        inp, pre = self.inputs, self.pre
        prunes = [p for p in inp.prunes if p.block < t]
        diss = [d for d in inp.dissolutions if d.block < t]
        phi = pre.phi
        for b, v in inp.phi:
            if b < t:
                phi = v
        kappa = self._kappa_memo.get(len(prunes))
        if kappa is None:
            kappa = self._kappa_memo[len(prunes)] = fit_kappa(prunes, pre.kappa_p, pre.kappa_p_range,
                                                              pre.tier_b_prior_until_prunes)
        return sealed(Calibration(asof=Block(t), hazard=self._hazard(t), kappa_p=kappa, r_default=pre.r_default,
                                  r_cap_formula=ft10_cap(diss),
                                  tier_b_jump_p_day=fit_tier_b_jump(prunes, pre.tier_b_jump_p_day,
                                                                    pre.tier_b_prior_until_prunes),
                                  tier_b_jump_size=pre.tier_b_jump_size, phi=phi, digest=""))

    def _hazard(self, t: int) -> HazardModel:
        pre = self.pre
        rate, i_eff = pre.prior_offset_blocks, FROZEN_I_EFF_BLOCKS
        for p in self.inputs.params:
            if p.block < t and p.i_eff > 0:
                rate, i_eff = p.rate_limit, p.i_eff
        regs = [r for r in self.inputs.registrations if r.queued_block < t]
        invalid = self.hazard_invalid_from is not None and t > self.hazard_invalid_from
        if invalid:
            regs = [r for r in regs if r.queued_block >= int(self.hazard_invalid_from or 0)]
        sig = (rate, i_eff, invalid, tuple((int(r.queued_block), r.cost_ratio) for r in regs))
        m = self._hazard_memo.get(sig)
        if m is None:
            prior = prior_hazard(rate, i_eff, pre.prior_scale_blocks, pre.n0)
            if invalid and len(regs) < max(pre.refit_min_rows, REFIT_MIN_ROWS):
                m = replace(prior, valid=False)
            else:
                m = fit_hazard(regs, Block(t), prior)
            self._hazard_memo[sig] = m
        return m


@dataclass(slots=True)
class FrozenCalibration:
    """The preregistered section 3.3 calibration; paper and live only."""
    mode: RunMode
    prereg: PreregCalib | None = None
    recent_cost_ratios: Callable[[Block], Sequence[Decimal]] | None = None   # last registrations' r, oldest first
    ft10_passed: bool = False
    i_eff_blocks: int = FROZEN_I_EFF_BLOCKS
    _base: HazardModel | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.mode is RunMode.BACKTEST:
            raise ValueError("FrozenCalibration is allowed only in paper and live (section 8.10 as-of rule); "
                             "backtests use LakeCalibrationProvider")
        if self.prereg is None:
            self.prereg = PreregCalib.load()

    def asof(self, block: Block) -> Calibration:
        pre = self.prereg
        assert pre is not None
        if self._base is None:
            self._base = frozen_hazard(pre, self.i_eff_blocks)
        hazard = self._base
        if self.recent_cost_ratios is not None:
            hazard = apply_hot_market(hazard, list(self.recent_cost_ratios(block)))
        return sealed(Calibration(asof=block, hazard=hazard, kappa_p=pre.kappa_p, r_default=pre.r_default,
                                  r_cap_formula=not self.ft10_passed, tier_b_jump_p_day=pre.tier_b_jump_p_day,
                                  tier_b_jump_size=pre.tier_b_jump_size, phi=pre.phi, digest=""))
