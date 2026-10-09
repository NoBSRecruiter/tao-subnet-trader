"""taotrader/protocol/ema.py - SubnetMovingPrice kinematics (WP2; DESIGN.md sections 3.3 and 5.12, brief 3.3).

Chain rule (stake_utils.rs::update_moving_price, every block in block_step AFTER run_coinbase, emit-eligible
subnets only):
    b    = now - (FirstEmissionBlockNumber - 1)            # blocks since start_call, not since registration
    a    = SubnetMovingAlpha * b / (b + EMAPriceHalvingBlocks)
    EMA' = a * min(spot, 1) + (1 - a) * EMA
NetworkRegistrationAllowed = false freezes the EMA (a = 0); SubnetEmissionEnabled = false does NOT.
All arithmetic is Decimal in the fixed DEC context (ln/exp are correctly rounded and identical across OSes).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Final

from ..core.fixed import DEC, ONE
from ..core.state import ChainGlobals, SubnetState
from ..core.units import Block
from .emission import emit_eligible

ZERO: Final[Decimal] = Decimal(0)
LN2: Final[Decimal] = DEC.ln(Decimal(2))


# ---------------------------------------------------------------- ema.py
def blocks_since_start(s: SubnetState, block: Block) -> int | None:
    """b = block - (FirstEmissionBlockNumber - 1); None before start_call."""
    if s.first_emission_block is None:
        return None
    return block - (s.first_emission_block - 1)


def ema_alpha(glob: ChainGlobals, s: SubnetState, block: Block) -> Decimal:
    """a = SubnetMovingAlpha * b/(b + EMAPriceHalvingBlocks), b = block - (FirstEmissionBlockNumber - 1).
    a = 0 if not emit-eligible (no start_call, SubtokenEnabled False, or NetworkRegistrationAllowed False: EMA frozen)."""
    if not emit_eligible(s):
        return ZERO
    b = blocks_since_start(s, block)
    if b is None or b <= 0:
        return ZERO
    return DEC.divide(DEC.multiply(glob.moving_alpha, Decimal(b)), Decimal(b + s.ema_halving_blocks))


def ema_step(ema: Decimal, spot: Decimal, a: Decimal) -> Decimal:
    """One block of the recursion: a * min(spot, 1) + (1 - a) * EMA."""
    return DEC.add(DEC.multiply(a, min(spot, ONE)), DEC.multiply(DEC.subtract(ONE, a), ema))


def half_life_blocks(a: Decimal) -> Decimal | None:
    """ln2 / a (the section 10.1 half-life convention); None when the EMA is frozen (a <= 0)."""
    return DEC.divide(LN2, a) if a > 0 else None


def ema_forecast(e0: Decimal, spot: Decimal, b0: int, dn: int, moving_alpha: Decimal, halving: int) -> Decimal:
    """Flat-spot closed form: S - (S - E0)*exp(-alpha*(dn - H*ln((H+b0+dn)/(H+b0)))), S = min(spot, 1)."""
    s = min(spot, ONE)
    if dn <= 0 or moving_alpha <= 0:
        return e0
    h = Decimal(halving)
    log_term = DEC.ln(DEC.divide(DEC.add(h, Decimal(b0 + dn)), DEC.add(h, Decimal(b0))))
    integral = DEC.multiply(moving_alpha, DEC.subtract(Decimal(dn), DEC.multiply(h, log_term)))
    return DEC.subtract(s, DEC.multiply(DEC.subtract(s, e0), DEC.exp(DEC.minus(integral))))


def project_ema(glob: ChainGlobals, s: SubnetState, block: Block, dn: int, spot: Decimal | None = None) -> Decimal:
    """SubnetMovingPrice of `s` dn blocks after `block` if its spot holds flat at `spot` (default: the pool spot):
    ema_forecast with the subnet's own a(b) = SubnetMovingAlpha*b/(b + EMAPriceHalvingBlocks). A frozen EMA (not
    emit-eligible: no start_call, SubtokenEnabled or NetworkRegistrationAllowed false) stays at its current value."""
    if dn <= 0 or not emit_eligible(s):
        return s.moving_price
    b0 = blocks_since_start(s, block)
    if b0 is None:
        return s.moving_price
    if spot is None:
        spot = s.pool.spot() if s.pool.px_tao > 0 and s.pool.px_alpha > 0 else ZERO
    return ema_forecast(s.moving_price, spot, max(b0, 0), dn, glob.moving_alpha, s.ema_halving_blocks)


def ema_warmup_fraction(b: int, moving_alpha: Decimal, halving: int) -> Decimal:
    """EMA/spot after b blocks from start_call at a constant spot <= 1: 1 - exp(-alpha*(b - H*ln(1 + b/H)))."""
    return ema_forecast(ZERO, ONE, 0, b, moving_alpha, halving)


def t_star_blocks(ema_k: Decimal, ema_bottom: Decimal, spot_k: Decimal, a_k: Decimal) -> int | None:
    """Blocks until EMA_k <= bottom EMA if k's spot holds at spot_k: ln((E_k - s)/(E_1 - s)) / -ln(1-a).
    0 if already at/below; None if spot_k >= E_1 (never crosses) or a == 0.

    s = min(spot_k, 1) (the chain caps the EMA input at 1); the bottom EMA is held flat; the result is rounded UP to
    whole blocks."""
    if ema_k <= ema_bottom:
        return 0
    s = min(spot_k, ONE)
    if s >= ema_bottom or a_k <= 0 or a_k >= ONE:
        return None
    ratio = DEC.divide(DEC.subtract(ema_k, s), DEC.subtract(ema_bottom, s))
    per_block = DEC.minus(DEC.ln(DEC.subtract(ONE, a_k)))
    blocks = DEC.divide(DEC.ln(ratio), per_block)
    whole = int(blocks.to_integral_value(rounding=ROUND_CEILING))
    return max(whole, 0)
