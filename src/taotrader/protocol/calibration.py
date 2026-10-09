"""taotrader/protocol/calibration.py - the as-of Calibration bundle and the CalibrationProvider seam (WP2; DESIGN.md
sections 5.12, 5.13 note 9, 8.10).

Every input fitted on history (hazard CDF and P_OPEN, kappa_p, R / the FT10 choice, Tier B jump parameters, phi)
reaches decisions only through `CalibrationProvider.asof(block)`, fit only on events with block < asof. WP2 owns the
fit functions (prune.fit_hazard) and this bundle; WP4 owns the lake-backed provider and FrozenCalibration.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol

from ..core.codec import digest, encode
from ..core.errors import LookaheadError
from ..core.units import Block
from .prune import HazardModel
from .sellload import SellLoadParams


@dataclass(frozen=True, slots=True)
class Calibration:
    """Every calibrated decision input, fit only on events with block < asof (section 8.10)."""
    asof: Block
    hazard: HazardModel                 # fit_hazard on registrations before asof
    kappa_p: Decimal                    # FT1 logistic on prunes before asof (prior 4)
    r_default: Decimal                  # FT10 outcome on dissolutions before asof: 0.35, or min(formula, 0.35) flag
    r_cap_formula: bool                 # True -> R = min(formula, r_default)
    tier_b_jump_p_day: Decimal          # Tier B jump probability per day (prior 0.03)
    tier_b_jump_size: Decimal           # ln(1 - 0.5) prior
    phi: SellLoadParams                 # T3-measured phi on data before asof (priors until measured)
    digest: str                         # journaled in DecisionTrace.calib_digest


class CalibrationProvider(Protocol):
    """Injected into the FeatureEngine and RiskOverlay constructors. Backtests: data.calibration.LakeCalibrationProvider
    (as-of fits from the lake registration and generation tables, cached per refit point). Paper/live: the same
    provider on recorded history, or FrozenCalibration (the preregistered section 3.3 values; allowed ONLY in
    paper and live)."""
    def asof(self, block: Block) -> Calibration: ...


def calibration_digest(c: Calibration) -> str:
    """blake2b-128 of the canonical encoding of every field except `digest` itself."""
    payload: dict[str, Any] = encode(c)
    payload.pop("digest", None)
    return digest(payload)


def sealed(c: Calibration) -> Calibration:
    """`c` with its digest field set to calibration_digest(c) (providers return sealed calibrations)."""
    return replace(c, digest=calibration_digest(c))


def effective_recovery(formula_r: Decimal, c: Calibration) -> Decimal:
    """R used by decisions: min(formula, r_default) when the FT10 outcome capped the formula, else the formula."""
    return min(formula_r, c.r_default) if c.r_cap_formula else formula_r


def check_asof(c: Calibration, block: Block) -> None:
    """Raise LookaheadError if `c` was fitted as of a block after `block`: it could contain events at or after the
    decision block (as-of rule, section 8.10). A calibration as of `block` itself uses only events before it."""
    if c.asof > block:
        raise LookaheadError(f"calibration as of {c.asof} used at block {block}")
