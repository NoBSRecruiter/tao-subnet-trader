"""taotrader/core/fixed.py - exact arithmetic for the money path.

Decimal with fixed contexts is bit-identical on Windows and Linux (libm pow is not),
so journals replay identically across OSes. Floats are allowed only in feature math
and must cross into the decision path through to_ppm().
"""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from typing import Final

from .units import PPM, Ppm

DEC: Final[Context] = Context(prec=60, rounding=ROUND_FLOOR)   # AMM fractional powers, spot, limits
EXACT: Final[Context] = Context(prec=160)                      # fixed-point decoding: 2**-64 terminates in 64 digits

ONE: Final[Decimal] = Decimal(1)


def floor_int(d: Decimal) -> int:
    return int(d.to_integral_value(rounding=ROUND_FLOOR))


def mul_ppm(x: int, p: int) -> int:
    """x * p / 1e6, floored. x is any integer amount, p a Ppm."""
    return x * p // PPM


def frac_ppm(num: int, den: int) -> Ppm:
    return Ppm(0 if den == 0 else num * PPM // den)


def to_ppm(f: float) -> Ppm:
    """The ONLY float -> int bridge on the decision path (used at the Signal boundary).

    Round half-even on the exact decimal repr; raises on NaN/inf so a bad feature fails loudly.
    """
    if not math.isfinite(f):
        raise ValueError(f"non-finite value {f!r}")
    return Ppm(int((Decimal(repr(f)) * PPM).to_integral_value(rounding=ROUND_HALF_EVEN)))
