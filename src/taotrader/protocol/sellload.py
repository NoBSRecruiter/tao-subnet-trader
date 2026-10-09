"""taotrader/protocol/sellload.py - the ONE structural sell-load model (WP2; DESIGN.md sections 2.1 and 5.12,
brief 3.5-3.8).

Per day, in whole TAO (Decimal): emission recipients that may sell are the owner (liquid part of the cut), miners
(burn-adjusted) and root dividends - escrowed into validator baskets since v441 (released by claims at kappa_b of the
stock per day), sold every block before it while root dividends accrued (sum EMA > 1). Pushes are fractions of price
per day: k_w * flow / y with k_w = 1 / w_base and y the pricing TAO reserve.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.fixed import DEC, ONE
from ..core.state import ChainGlobals, SubnetState
from ..core.units import BLOCKS_PER_DAY, PERQUINTILL, PPM, RAO_PER_TAO, Block, Ppm
from .emission import EmissionShare
from .regimes import VALIDATOR_SHARE, regime
from .yield_model import owner_cut_frac

ZERO: Final[Decimal] = Decimal(0)


@dataclass(frozen=True, slots=True)
class SellLoadParams:
    phi_owner_ppm: Ppm = Ppm(800_000)     # fraction of liquid owner cut sold (prior; measured from owner positions in T3)
    phi_miner_ppm: Ppm = Ppm(600_000)     # fraction of miner emission sold (prior)
    kappa_basket_ppm_day: Ppm = Ppm(20_000)   # basket claim release c ~ 2%/day of escrow stock


@dataclass(frozen=True, slots=True)
class SellLoad:
    owner_tao_day: Decimal
    miner_tao_day: Decimal
    basket_tao_day: Decimal               # post-v441: kappa*E*p; pre-v441: root dividends sold every block
    total_tao_day: Decimal
    sell_push_day: Decimal                # k_w * total / y
    cb_push_day: Decimal                  # k_w * chain_buy_day / y
    coverage: Decimal                     # chain_buy_day / total (0 if total == 0)


def _ppm(v: int) -> Decimal:
    return DEC.divide(Decimal(v), Decimal(PPM))


def sell_load(s: SubnetState, glob: ChainGlobals, share: EmissionShare, params: SellLoadParams,
              block: Block, root_flag: bool) -> SellLoad:
    """S = p*[phi_o*c_o*(1-AL)*AE + phi_m*0.5*(1-c_o)*(1-min(MB,1))*AE + basket], AE = 7200*SubnetAlphaOutEmission.
    basket = kappa_b*E (>= 8,765,684) else rp*0.5*(1-c_o)*AE*root_flag (root dividends sold per block pre-v441).
    (1-AL) here is the LIQUID fraction of the owner cut; owner-sale DETECTION (section 3.6) never applies it.

    Units: TAO per day (whole TAO). AE and E are converted from alpha rao to alpha; p = spot (TAO per alpha);
    E = SubnetState.escrow_alpha (None -> 0); AL = OwnerCutAutoLockEnabled (None -> False, the chain default)."""
    rao = Decimal(RAO_PER_TAO)
    p = s.pool.spot() if s.pool.px_alpha > 0 and s.pool.px_tao > 0 else ZERO
    ae = DEC.divide(DEC.multiply(Decimal(BLOCKS_PER_DAY), Decimal(s.alpha_out_emission)), rao)   # alpha/day
    c_o = owner_cut_frac(s, glob)
    liquid = ZERO if s.owner_cut_autolock else ONE
    burn_keep = DEC.subtract(ONE, min(max(s.miner_burned, ZERO), ONE))

    owner_alpha = DEC.multiply(DEC.multiply(DEC.multiply(_ppm(params.phi_owner_ppm), c_o), liquid), ae)
    miner_alpha = DEC.multiply(DEC.multiply(DEC.multiply(DEC.multiply(_ppm(params.phi_miner_ppm), VALIDATOR_SHARE),
                                                         DEC.subtract(ONE, c_o)), burn_keep), ae)
    if block >= regime("gate_rank32").first_block:
        escrow = DEC.divide(Decimal(max(s.escrow_alpha or 0, 0)), rao)
        basket_alpha = DEC.multiply(_ppm(params.kappa_basket_ppm_day), escrow)
    elif root_flag:
        basket_alpha = DEC.multiply(DEC.multiply(DEC.multiply(s.root_prop, VALIDATOR_SHARE), DEC.subtract(ONE, c_o)), ae)
    else:
        basket_alpha = ZERO

    owner_tao = DEC.multiply(p, owner_alpha)
    miner_tao = DEC.multiply(p, miner_alpha)
    basket_tao = DEC.multiply(p, basket_alpha)
    total = DEC.add(DEC.add(owner_tao, miner_tao), basket_tao)

    chain_buy_day = DEC.divide(DEC.multiply(Decimal(BLOCKS_PER_DAY), Decimal(share.chain_buy_per_block)), rao)
    y = DEC.divide(Decimal(s.pool.px_tao), rao)
    k_w = DEC.divide(Decimal(PERQUINTILL), Decimal(s.pool.w_base_e18)) if s.pool.w_base_e18 > 0 else ZERO
    sell_push = DEC.divide(DEC.multiply(k_w, total), y) if y > 0 else ZERO
    cb_push = DEC.divide(DEC.multiply(k_w, chain_buy_day), y) if y > 0 else ZERO
    coverage = DEC.divide(chain_buy_day, total) if total > 0 else ZERO
    return SellLoad(owner_tao_day=owner_tao, miner_tao_day=miner_tao, basket_tao_day=basket_tao, total_tao_day=total,
                    sell_push_day=sell_push, cb_push_day=cb_push, coverage=coverage)
