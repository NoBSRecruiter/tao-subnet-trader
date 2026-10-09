"""taotrader/protocol/yield_model.py - nominator-yield closed form, A_earn and its deterministic growth (WP2;
DESIGN.md sections 2.1 and 5.12, brief 3.5-3.6).

alpha_out (SubnetAlphaOutEmission, 1 alpha/block today) splits into the owner cut c_o = SubnetOwnerCut/65535 (a
GLOBAL value, only while OwnerCutEnabled), miners 0.5*(1 - c_o) and validators 0.5*(1 - c_o), of which rp goes to
root (escrow baskets since v441) and (1 - rp) compounds into the dividend hotkeys' share pools. Stake earns iff its
hotkey is a key of AlphaDividendsPerSubnet(n, .). Golden at 9,240,388: SN70 0.557 %/day, SN92 0.448, SN64 0.0918.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Final

from ..core.fixed import DEC, ONE
from ..core.state import ChainGlobals, HotkeyIdx, SubnetState
from ..core.units import BLOCKS_PER_DAY, FEE_DEN, AlphaRao
from .regimes import VALIDATOR_SHARE

ZERO: Final[Decimal] = Decimal(0)


def owner_cut_frac(s: SubnetState, glob: ChainGlobals) -> Decimal:
    """c_o = SubnetOwnerCut / 65535, or 0 when OwnerCutEnabled is false (absent/None = enabled, the chain default)."""
    if s.owner_cut_enabled is False:
        return ZERO
    return DEC.divide(Decimal(glob.owner_cut_u16), Decimal(FEE_DEN))


def a_earn(s: SubnetState) -> AlphaRao:
    """Sum of TotalHotkeyAlpha over tracked hotkeys with earns=True (flag NO_YIELD_IDX if coverage < 95%).

    The coverage flag needs the full dividend-key panel and is set by the snapshot builder (SubnetState.quality);
    this function sums what the snapshot tracks."""
    return AlphaRao(sum(h.total_alpha for h in s.hotkeys if h.earns))


def _nominator_alpha_per_day(s: SubnetState, glob: ChainGlobals) -> Decimal:
    """7200 * alpha_out_emission * (1 - c_o) * 0.5: the validator half of the non-owner emission, alpha rao/day."""
    per_day = DEC.multiply(Decimal(BLOCKS_PER_DAY), Decimal(s.alpha_out_emission))
    return DEC.multiply(DEC.multiply(per_day, DEC.subtract(ONE, owner_cut_frac(s, glob))), VALIDATOR_SHARE)


def closed_form_yield_gross(s: SubnetState, glob: ChainGlobals) -> Decimal:
    """Per alpha per day: 7200*alpha_out_emission*(1 - c_o)*0.5*(1 - rp)/A_earn, c_o = SubnetOwnerCut/65535
    (if OwnerCutEnabled). = 2952*(1-rp)/A_earn today. Golden: SN70 0.557%, SN92 0.448%, SN64 0.0918%.

    Returns 0 when A_earn is 0 (no earning stake tracked: NO_YIELD_IDX)."""
    ae = a_earn(s)
    if ae <= 0:
        return ZERO
    to_nominators = DEC.multiply(_nominator_alpha_per_day(s, glob), DEC.subtract(ONE, s.root_prop))
    return DEC.divide(to_nominators, Decimal(ae))


def closed_form_yield_net(s: SubnetState, glob: ChainGlobals, h: HotkeyIdx) -> Decimal:
    """Gross closed form x (1 - take) x (1 - childkey take) for staking on hotkey h."""
    take = DEC.divide(Decimal(h.take_u16), Decimal(FEE_DEN))
    ck = DEC.divide(Decimal(h.childkey_take_u16), Decimal(FEE_DEN))
    return DEC.multiply(DEC.multiply(closed_form_yield_gross(s, glob), DEC.subtract(ONE, take)), DEC.subtract(ONE, ck))


def a_earn_growth_per_day(s: SubnetState, glob: ChainGlobals, root_flag: bool) -> Decimal:
    """Deterministic A_earn growth in alpha/day: nominator compounding 7200*ae*(1-c_o)*0.5*(1-rp)
    + escrow deposits 7200*ae*(1-c_o)*0.5*rp while root_flag (sum EMA > 1). Net user flow is added by callers.

    Units: alpha RAO per day (the unit of a_earn); ae = SubnetAlphaOutEmission per block."""
    base = _nominator_alpha_per_day(s, glob)
    growth = DEC.multiply(base, DEC.subtract(ONE, s.root_prop))
    if root_flag:
        growth = DEC.add(growth, DEC.multiply(base, s.root_prop))
    return growth


def index_growth(prev: HotkeyIdx, cur: HotkeyIdx) -> Decimal:
    """I_cur / I_prev - 1 for one hotkey share pool (the realised nominator yield between two snapshots)."""
    i0 = prev.index()
    if i0 <= 0:
        return ZERO
    return DEC.subtract(DEC.divide(cur.index(), i0), ONE)
