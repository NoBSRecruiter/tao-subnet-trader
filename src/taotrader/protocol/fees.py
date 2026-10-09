"""taotrader/protocol/fees.py - swap-fee arithmetic, the transaction-fee table, the minimum stake and the dust
thresholds (WP2; DESIGN.md sections 2.0, 3.9, 8.5; brief sections 2.2, 2.4, 5.12).

All amounts are integer rao (TAO) or alpha rao. The swap fee is floor(amount * FeeRate / 65535) on the INPUT side
of every pool leg; it is never charged on a same-subnet move or on protocol chain buys.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.config import ExecCfg
from ..core.fixed import DEC, floor_int
from ..core.orders import OrderKind
from ..core.state import ChainGlobals, PoolState
from ..core.units import FEE_DEN, MIN_STAKE_RAO, RAO_PER_TAO, Rao

__all__ = [
    "MIN_STAKE_RAO", "TxFeeTable", "fee_tao", "gross_for_net", "meets_min_stake", "net_of_fee", "nominator_dust",
    "swap_fee", "tx_fee_rao", "tx_fee_table",
]


def swap_fee(amount: int, fee_rate: int) -> int:
    """floor(amount * FeeRate / 65535): the input-side swap fee (rao for buys, alpha rao for sells)."""
    if amount < 0 or not 0 <= fee_rate < FEE_DEN:
        raise ValueError(f"bad swap fee inputs amount={amount} fee_rate={fee_rate}")
    return amount * fee_rate // FEE_DEN


def net_of_fee(amount: int, fee_rate: int) -> int:
    """The amount that reaches the pool: amount - swap_fee(amount)."""
    return amount - swap_fee(amount, fee_rate)


def gross_for_net(net: int, fee_rate: int) -> int:
    """The LARGEST gross input whose post-fee amount does not exceed `net` (gross = net * 65535 / (65535 - f),
    floored and corrected for the chain's floor on the fee)."""
    if net <= 0:
        return 0
    gross = net * FEE_DEN // (FEE_DEN - fee_rate)
    while gross > 0 and net_of_fee(gross, fee_rate) > net:
        gross -= 1
    while net_of_fee(gross + 1, fee_rate) <= net:
        gross += 1
    return gross


def meets_min_stake(gross: int, fee_rate: int) -> bool:
    """Chain minimum for a stake: gross >= DefaultMinStake + fee AND the post-fee amount >= DefaultMinStake."""
    fee = swap_fee(gross, fee_rate)
    return gross >= MIN_STAKE_RAO + fee and gross - fee >= MIN_STAKE_RAO


def nominator_dust(remaining_alpha: int, pool: PoolState, glob: ChainGlobals) -> bool:
    """True if a remaining nominator position (> 0 alpha) is worth less than NominatorMinRequiredStake at spot: the
    chain force-sells it with no price limit after any remove-type call (brief 2.4). Never leave one behind."""
    if remaining_alpha <= 0:
        return False
    value_rao = floor_int(DEC.multiply(Decimal(remaining_alpha), pool.spot()))   # alpha rao x TAO/alpha = rao
    return value_rao < glob.nominator_min_stake


@dataclass(frozen=True, slots=True)
class TxFeeTable:
    """All-in extrinsic fees per order on the production path (Proxy + shield carrier), rao (brief 5.12)."""
    buy_rao: int
    sell_rao: int
    move_rao: int
    rotate_rao: int
    carrier_rao: int


def tx_fee_table(cfg: ExecCfg | None = None) -> TxFeeTable:
    """The measured fee table (ExecCfg defaults: buy 1,028,000, sell 837,000, move 1,000,000 (unmeasured),
    rotation 1,250,000, carrier 98,000) or the values of a configured ExecCfg."""
    c = cfg if cfg is not None else ExecCfg()
    return TxFeeTable(buy_rao=c.buy_tx_fee_rao, sell_rao=c.sell_tx_fee_rao, move_rao=c.move_tx_fee_rao,
                      rotate_rao=c.rotate_tx_fee_rao, carrier_rao=c.carrier_fee_rao)


def tx_fee_rao(kind: OrderKind, cfg: ExecCfg | None = None) -> Rao:
    """Extrinsic fee for one order of `kind` (a failed inner call pays it in full)."""
    t = tx_fee_table(cfg)
    if kind is OrderKind.ADD_STAKE_LIMIT:
        return Rao(t.buy_rao)
    if kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        return Rao(t.sell_rao)
    if kind is OrderKind.MOVE_STAKE:
        return Rao(t.move_rao)
    return Rao(t.rotate_rao)


def fee_tao(rao: int) -> Decimal:
    """rao -> TAO (exact)."""
    return DEC.divide(Decimal(rao), Decimal(RAO_PER_TAO))
