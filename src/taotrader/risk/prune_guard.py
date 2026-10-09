"""taotrader/risk/prune_guard.py - the prune engine of the overlay (WP8; DESIGN.md 3.3).

Everything is evaluated on the RAW snapshot (the market; the frame's ladder is computed from it too) with the exact
protocol replicas, every tick, whatever the strategies' cadence:

- prune_possible = n_nonroot + len(DissolveCleanupQueue) >= SubnetLimit; when false nothing here fires (hazard 0).
- Target: ladder()[0] (local rule). When the runtime cross-check (ChainGlobals.runtime_prune_target) names another
  netuid, the runtime value wins: that generation is treated as a target too, and entries halt (data alarm).
- NEVER HOLD THE TARGET: a held target -> EMERGENCY exit ("prune_target"), whatever the window state.
- TIER A (EMERGENCY, "prune_A"): window open or opening within U + M_A (block + U + M_A >= W_open) AND the stressed
  time-to-target t*_k <= U + M_A. t* is protocol.prune.time_to_target (moving bottom, other subnets at their spot,
  subnets admitted at their immunity expiry, exact first block) with k's spot at (1 - D_STRESS) * spot. It therefore
  also fires when a held IMMUNE subnet's expiry falls inside U + M_A and it would be the target. Fail-closed: the
  frame's flat-bottom closed form Feat.t_star_stress_blocks <= U + M_A fires it as well.
  U = unwind_exec_blocks * (1 + unwind_retries) = 5 * 3 = 15 blocks. Exit slippage budget
  s_emerg = clamp(0.5 * (1 - R_k), 5%, 25%), R_k = recovery ratio (protocol.prune.recovery_ratio, capped through
  calibration.effective_recovery as of the decision block).
- BACKSTOP (URGENT, model-free, "prune_backstop"): non-immune prune rank <= K_BOTTOM (3) AND cost ratio
  r = cost / NetworkLastLockCost <= R_BACKSTOP (1.2), i.e. Delta >= 46,080 blocks since the last registration at
  I_eff = 57,600. Single-shot exit at S_URGENT (3%).
- TIER B (URGENT, "prune_B", only when tier_b_enabled): block + H_B >= W_open AND P_prune_24h(k) * (1 - R_k) >= PI_B,
  with P_prune from risk.hazard_mc.
- ENTRY floor (on top of sections A-G, which the overlay re-evaluates per book with features.universe): no entry on a
  target (local or runtime), inside the Tier A zone (t* <= U + M_A on the frame) or the backstop zone; with the MC
  enabled, P_prune_7d * (1 - R) <= 1% (fail-closed: no MC result -> no entry); no increase on a ladder-bucket name
  (prune rank <= 15) while the book's ladder bucket is >= 15% of NAV_liq.
- Expected prune loss per held name (MONITOR): lambda/day = P(reg within 7,200) * exp(-kappa_p (rho - 1)) with
  kappa_p from the calibration (prior 4), loss/day = lambda * (1 - R).

Coverage of Tier A (section 3.3 table, regenerated for U = 15): with a = 0.000286 and the bottom flat, the largest
EMA gap g = E_k / E_1 - 1 that a crash of k's spot from its EMA level still crosses within U + M_A blocks is
`tier_a_gap_covered`: with x = (U + M_A) * -ln(1 - a), q = e^x and c = 1 - crash, g = q / (1 - c + q c) - 1
(spot -> 0: q - 1; 50% crash: tanh(x/2)).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import RiskCfg
from ..core.fixed import DEC, ONE, floor_int
from ..core.orders import Urgency
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import BLOCKS_PER_DAY, PPM, Ppm, SubnetKey
from ..core.views import Feat, FeatureFrame
from ..protocol.calibration import Calibration, effective_recovery
from ..protocol.ema import project_ema
from ..protocol.prune import (
    cost_ratio,
    is_immune,
    ladder,
    prune_possible,
    recovery_ratio,
    time_to_target,
    window_open_block,
)
from .hazard_mc import McResult

__all__ = [
    "KAPPA_P_PRIOR",
    "S_EMERG_MAX_PPM",
    "S_EMERG_MIN_PPM",
    "PruneContext",
    "PruneExit",
    "backstop_ratio_hit",
    "entry_vetoes",
    "expected_loss_ppm_day",
    "held_exits",
    "prune_context",
    "ratio_ppm",
    "recovery",
    "s_emerg_ppm",
    "stressed_spot",
    "t_star_moving",
    "tier_a_gap_covered",
    "unwind_blocks",
]

S_EMERG_MIN_PPM: Final[int] = 50_000
S_EMERG_MAX_PPM: Final[int] = 250_000
KAPPA_P_PRIOR: Final[Decimal] = Decimal(4)
LADDER_BUCKET_RANK: Final[int] = 15
P_REG_DAY_HORIZON: Final[int] = BLOCKS_PER_DAY


def unwind_blocks(cfg: RiskCfg) -> int:
    """U = L_exec * (1 + retries) (15 blocks at the defaults)."""
    return cfg.unwind_exec_blocks * (1 + cfg.unwind_retries)


@dataclass(frozen=True, slots=True)
class PruneContext:
    block: int
    possible: bool
    ladder: tuple[SubnetKey, ...]
    ranks: Mapping[SubnetKey, int]
    target: SubnetKey | None              # local rule: ladder()[0]
    runtime_target: SubnetKey | None      # the runtime cross-check's generation when it differs from the local target
    window_open_block: int
    cost_ratio: Decimal
    horizon: int                          # U + M_A
    zone_open: bool                       # block + U + M_A >= W_open
    hb_open: bool                         # block + H_B >= W_open

    @property
    def targets(self) -> tuple[SubnetKey, ...]:
        return tuple(k for k in (self.target, self.runtime_target) if k is not None)


def prune_context(raw: ChainSnapshot, cfg: RiskCfg) -> PruneContext:
    glob = raw.glob
    b = int(raw.block)
    lad = ladder(raw)
    target = lad[0] if lad else None
    rt: SubnetKey | None = None
    if glob.runtime_prune_target is not None and (target is None or int(target.netuid) != int(glob.runtime_prune_target)):
        s = raw.by_netuid(int(glob.runtime_prune_target))
        rt = s.key if s is not None else None
    w_open = int(window_open_block(glob))
    horizon = unwind_blocks(cfg) + cfg.margin_a_blocks
    return PruneContext(block=b, possible=prune_possible(glob), ladder=lad, ranks={k: i + 1 for i, k in enumerate(lad)},
                        target=target, runtime_target=rt, window_open_block=w_open, cost_ratio=cost_ratio(glob, raw.block),
                        horizon=horizon, zone_open=b + horizon >= w_open, hb_open=b + cfg.h_b_blocks >= w_open)


def s_emerg_ppm(r: Decimal) -> Ppm:
    """clamp(0.5 * (1 - R), 5%, 25%) in ppm (R in [0, 1])."""
    return Ppm(max(S_EMERG_MIN_PPM, min(S_EMERG_MAX_PPM, (PPM - ratio_ppm(r)) // 2)))


def ratio_ppm(r: Decimal) -> int:
    """floor(clamp(r, 0, 1) * 1e6)."""
    return floor_int(DEC.multiply(min(max(r, Decimal(0)), ONE), Decimal(PPM)))


def backstop_ratio_hit(r: Decimal, cfg: RiskCfg) -> bool:
    """cost ratio r <= R_BACKSTOP (exact Decimal comparison)."""
    return r <= DEC.divide(Decimal(int(cfg.r_backstop_ppm)), Decimal(PPM))


def recovery(s: SubnetState, glob: ChainGlobals, cal: Calibration | None, cfg: RiskCfg) -> Decimal:
    """R used by decisions: the dissolution formula, capped per the as-of FT10 outcome (calibration)."""
    r_default = cal.r_default if cal is not None else DEC.divide(Decimal(int(cfg.r_default_ppm)), Decimal(PPM))
    r = recovery_ratio(s, glob, r_default)
    return effective_recovery(r, cal) if cal is not None else r


def stressed_spot(s: SubnetState, cfg: RiskCfg) -> Decimal:
    """(1 - D_STRESS) * spot."""
    if s.pool.px_tao <= 0 or s.pool.px_alpha <= 0:
        return Decimal(0)
    keep = DEC.divide(Decimal(PPM - int(cfg.d_stress_ppm)), Decimal(PPM))
    return DEC.multiply(s.pool.spot(), keep)


def t_star_moving(raw: ChainSnapshot, key: SubnetKey, cfg: RiskCfg, horizon: int) -> int | None:
    """Stressed time-to-target with a moving bottom (protocol.prune.time_to_target), None beyond `horizon`."""
    s = raw.get(key)
    if s is None:
        return None
    return time_to_target(raw, key, stressed_spot(s, cfg), horizon)


def _never_target(raw: ChainSnapshot, pc: PruneContext, key: SubnetKey, cfg: RiskCfg) -> bool:
    """Exact cheap pre-filter for t_star_moving: every flat-spot EMA path is monotone, so if k's lowest stressed value
    over the horizon stays above the current target's highest value (the target is non-immune, hence admitted
    throughout), k is never ladder()[0] within the horizon."""
    if pc.target is None or pc.target == key:
        return False
    s, j = raw.get(key), raw.get(pc.target)
    if s is None or j is None:
        return False
    k_low = min(s.moving_price, project_ema(raw.glob, s, raw.block, pc.horizon, stressed_spot(s, cfg)))
    j_high = max(j.moving_price, project_ema(raw.glob, j, raw.block, pc.horizon))
    return k_low > j_high


def tier_a_gap_covered(a: Decimal, horizon_blocks: int, crash_ppm: int) -> Decimal:
    """Largest EMA gap g = E_k/E_1 - 1 that a crash of k's spot (from its EMA level, by crash_ppm) still crosses
    within horizon_blocks with the bottom flat: the inverse of t* = ln((E_k - s)/(E_1 - s)) / -ln(1 - a)."""
    x = DEC.multiply(Decimal(horizon_blocks), DEC.minus(DEC.ln(DEC.subtract(ONE, a))))
    q = DEC.exp(x)
    c = DEC.divide(Decimal(PPM - crash_ppm), Decimal(PPM))
    return DEC.subtract(DEC.divide(q, DEC.add(DEC.subtract(ONE, c), DEC.multiply(q, c))), ONE)


@dataclass(frozen=True, slots=True)
class PruneExit:
    key: SubnetKey
    urgency: Urgency
    rule: str                  # "prune_target" | "prune_A" | "prune_backstop" | "prune_B"
    slip_ppm: Ppm
    detail: str


def held_exits(raw: ChainSnapshot, pc: PruneContext, held: Sequence[SubnetKey], cfg: RiskCfg,
               recoveries: Mapping[SubnetKey, Decimal], feats: Mapping[SubnetKey, Feat],
               p24: McResult | None = None) -> list[PruneExit]:
    """Forced prune exits for the held generations (one per key: the most urgent rule)."""
    out: list[PruneExit] = []
    if not pc.possible:
        return out
    glob = raw.glob
    for key in sorted(held):
        s = raw.get(key)
        if s is None:
            continue
        r = recoveries.get(key, DEC.divide(Decimal(int(cfg.r_default_ppm)), Decimal(PPM)))
        se = s_emerg_ppm(r)
        r_ppm = ratio_ppm(r)
        rank = pc.ranks.get(key)
        if key in pc.targets:
            which = "runtime" if key == pc.runtime_target else "local"
            out.append(PruneExit(key, Urgency.EMERGENCY, "prune_target", se,
                                 f"target={which};rank={rank};s_emerg_ppm={se};recovery_ppm={r_ppm}"))
            continue
        if pc.zone_open:
            t_star = None if _never_target(raw, pc, key, cfg) else t_star_moving(raw, key, cfg, pc.horizon)
            feat = feats.get(key)
            t_feat = feat.t_star_stress_blocks if feat is not None else None
            closed = t_feat is not None and not is_immune(s, glob, raw.block) and t_feat <= pc.horizon
            if t_star is not None or closed:
                out.append(PruneExit(key, Urgency.EMERGENCY, "prune_A", se,
                                     f"t_star={t_star if t_star is not None else 'none'};"
                                     f"t_star_closed_form={'none' if t_feat is None else int(t_feat)};"
                                     f"horizon={pc.horizon};w_open={pc.window_open_block};s_emerg_ppm={se};"
                                     f"recovery_ppm={r_ppm}"))
                continue
        if rank is not None and rank <= cfg.k_bottom and backstop_ratio_hit(pc.cost_ratio, cfg):
            out.append(PruneExit(key, Urgency.URGENT, "prune_backstop", Ppm(int(cfg.s_urgent_ppm)),
                                 f"rank={rank};cost_ratio_ppm={floor_int(DEC.multiply(pc.cost_ratio, Decimal(PPM)))};"
                                 f"blocks_since_reg={pc.block - int(glob.last_reg_block)}"))
            continue
        if cfg.tier_b_enabled and p24 is not None and pc.hb_open:
            p = int(p24.get(key))
            loss = p * (PPM - r_ppm) // PPM
            if loss >= cfg.pi_b_ppm:
                out.append(PruneExit(key, Urgency.URGENT, "prune_B", Ppm(int(cfg.s_urgent_ppm)),
                                     f"p_prune_24h_ppm={p};recovery_ppm={r_ppm};loss_ppm={loss};paths={p24.paths}"))
    return out


def expected_loss_ppm_day(frame: FeatureFrame, raw: ChainSnapshot, key: SubnetKey, r: Decimal,
                          cal: Calibration | None) -> tuple[int, int, int] | None:
    """(p_reg_day_ppm, lambda_ppm_day, loss_ppm_day) for a non-immune held name: lambda = P(reg within 7,200) *
    exp(-kappa_p (rho - 1)), loss = lambda * (1 - R). None when rho is unknown (immune or no bottom)."""
    s = raw.get(key)
    lad = ladder(raw)
    if s is None or not lad or key not in lad:
        return None
    bottom = raw.get(lad[0])
    if bottom is None or bottom.moving_price <= 0:
        return None
    p_day = next((int(p) for h, p in frame.prune.p_reg_ppm if h == P_REG_DAY_HORIZON), None)
    if p_day is None:
        return None
    rho = DEC.divide(s.moving_price, bottom.moving_price)
    kappa = cal.kappa_p if cal is not None else KAPPA_P_PRIOR
    w = DEC.exp(DEC.minus(DEC.multiply(kappa, DEC.subtract(rho, ONE))))
    lam = floor_int(DEC.multiply(Decimal(p_day), min(w, ONE)))
    loss = lam * (PPM - ratio_ppm(r)) // PPM
    return p_day, lam, loss


def entry_vetoes(raw: ChainSnapshot, pc: PruneContext, keys: Sequence[SubnetKey], cfg: RiskCfg,
                 feats: Mapping[SubnetKey, Feat], recoveries: Mapping[SubnetKey, Decimal],
                 current: Mapping[SubnetKey, int], nav_liq: int, p7: McResult | None) -> dict[SubnetKey, tuple[str, str]]:
    """Prune-specific entry vetoes for the generations in `keys` (increase candidates): rule and detail per key."""
    out: dict[SubnetKey, tuple[str, str]] = {}
    bucket = sum(v for k, v in current.items() if pc.ranks.get(k, LADDER_BUCKET_RANK + 1) <= LADDER_BUCKET_RANK)
    bucket_full = nav_liq > 0 and bucket * PPM >= int(cfg.ladder_bucket_ppm) * nav_liq
    for key in sorted(keys):
        rank = pc.ranks.get(key)
        if pc.possible and key in pc.targets:
            out[key] = ("prune.entry_target", f"rank={rank}")
            continue
        if pc.possible and rank is not None:
            feat = feats.get(key)
            t_feat = feat.t_star_stress_blocks if feat is not None else None
            if pc.zone_open and t_feat is not None and t_feat <= pc.horizon:
                out[key] = ("prune.entry_tier_a_zone", f"t_star={int(t_feat)};horizon={pc.horizon}")
                continue
            if rank <= cfg.k_bottom and backstop_ratio_hit(pc.cost_ratio, cfg):
                out[key] = ("prune.entry_backstop_zone", f"rank={rank}")
                continue
        if bucket_full and rank is not None and rank <= LADDER_BUCKET_RANK:
            out[key] = ("prune.ladder_bucket", f"rank={rank};bucket_rao={bucket};nav_rao={nav_liq}")
            continue
        if cfg.tier_b_enabled and pc.possible:
            if p7 is None:
                out[key] = ("prune.entry_mc", "p_prune_7d=unknown")
                continue
            r = recoveries.get(key, DEC.divide(Decimal(int(cfg.r_default_ppm)), Decimal(PPM)))
            r_ppm = ratio_ppm(r)
            p = int(p7.get(key))
            loss = p * (PPM - r_ppm) // PPM
            if loss > cfg.pi_entry_7d_ppm:
                out[key] = ("prune.entry_mc", f"p_prune_7d_ppm={p};recovery_ppm={r_ppm};loss_ppm={loss}")
    return out
