"""protocol.calibration: the as-of Calibration bundle, its digest, the lookahead guard and the provider seam."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core.codec import decode, encode
from taotrader.core.errors import LookaheadError
from taotrader.core.units import Block, Ppm
from taotrader.protocol.calibration import (
    Calibration,
    CalibrationProvider,
    calibration_digest,
    check_asof,
    effective_recovery,
    sealed,
)
from taotrader.protocol.prune import hazard_from_table
from taotrader.protocol.sellload import SellLoadParams

CDF_R = [Decimal(x) for x in ("1.75", "1.462", "1.253", "1.132", "1.045", "0.958", "0.872", "0.767", "0.266")]
CDF_F = [Decimal(x) for x in ("0.0625", "0.09", "0.19", "0.31", "0.50", "0.72", "0.84", "0.94", "1.0")]


def _calib(asof: int = 9_000_000) -> Calibration:
    hazard = hazard_from_table(CDF_R, CDF_F, 32, n0=4, rate_limit_blocks=14_400, i_eff_blocks=57_600,
                               prior_scale_blocks=43_200)
    return Calibration(asof=Block(asof), hazard=hazard, kappa_p=Decimal(4), r_default=Decimal("0.35"), r_cap_formula=False,
                       tier_b_jump_p_day=Decimal("0.03"), tier_b_jump_size=Decimal("-0.693147180559945309417232121458"),
                       phi=SellLoadParams(), digest="")


def test_digest_is_stable_and_covers_every_field() -> None:
    c = _calib()
    s = sealed(c)
    assert s.digest == calibration_digest(c) == calibration_digest(s)              # the digest field is excluded
    assert len(s.digest) == 32 and s.digest == sealed(_calib()).digest              # deterministic
    for changed in (replace(c, kappa_p=Decimal(5)), replace(c, asof=Block(9_000_001)), replace(c, r_cap_formula=True),
                    replace(c, phi=SellLoadParams(phi_owner_ppm=Ppm(0))),
                    replace(c, hazard=replace(c.hazard, valid=False))):
        assert calibration_digest(changed) != s.digest


def test_codec_round_trip() -> None:
    s = sealed(_calib())
    assert decode(Calibration, encode(s)) == s


def test_check_asof_rejects_lookahead() -> None:
    c = _calib(9_000_000)
    check_asof(c, Block(9_000_000))                                                 # fit on events < 9,000,000: fine
    check_asof(c, Block(9_100_000))
    with pytest.raises(LookaheadError):
        check_asof(c, Block(8_999_999))


def test_effective_recovery() -> None:
    c = _calib()
    assert effective_recovery(Decimal("0.42"), c) == Decimal("0.42")
    capped = replace(c, r_cap_formula=True)
    assert effective_recovery(Decimal("0.42"), capped) == Decimal("0.35")           # FT10 failed: min(formula, 0.35)
    assert effective_recovery(Decimal("0.2"), capped) == Decimal("0.2")


class _Fixed:
    """A minimal provider (WP4 owns the real ones): the same frozen bundle at every block."""

    def __init__(self, c: Calibration) -> None:
        self._c = c

    def asof(self, block: Block) -> Calibration:
        return replace(self._c, asof=block)


def test_provider_protocol_shape() -> None:
    p: CalibrationProvider = _Fixed(sealed(_calib()))
    c = p.asof(Block(9_200_000))
    check_asof(c, Block(9_200_000))
    assert c.asof == 9_200_000
