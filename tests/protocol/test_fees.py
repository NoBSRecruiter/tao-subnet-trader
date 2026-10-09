"""protocol.fees: section 10.1 fee vectors, fee arithmetic, minimum stake and dust."""
from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.core.config import ExecCfg
from taotrader.core.orders import OrderKind
from taotrader.core.state import PoolKind, PoolState
from taotrader.core.units import AlphaRao, Rao
from taotrader.protocol.fees import (
    MIN_STAKE_RAO,
    gross_for_net,
    meets_min_stake,
    net_of_fee,
    nominator_dust,
    swap_fee,
    tx_fee_rao,
    tx_fee_table,
)


def test_fee_vectors() -> None:
    assert swap_fee(10 * 10**9, 33) == 5_035_477          # floor(10e9 * 33 / 65535)
    assert swap_fee(10**9, 33) == 503_547
    assert tx_fee_rao(OrderKind.ADD_STAKE_LIMIT) == 1_028_000
    assert tx_fee_rao(OrderKind.REMOVE_STAKE_LIMIT) == 837_000
    assert tx_fee_rao(OrderKind.REMOVE_STAKE_FULL_LIMIT) == 837_000
    assert tx_fee_rao(OrderKind.MOVE_STAKE) == 1_000_000
    assert tx_fee_rao(OrderKind.MOVE_STAKE_LIMIT) == 1_250_000
    assert tx_fee_table().carrier_rao == 98_000
    assert tx_fee_table().buy_rao + tx_fee_table().sell_rao == 1_865_000      # the round-trip 0.001865 TAO
    cfg = replace(ExecCfg(), buy_tx_fee_rao=1_100_000)
    assert tx_fee_rao(OrderKind.ADD_STAKE_LIMIT, cfg) == 1_100_000
    with pytest.raises(ValueError):
        swap_fee(-1, 33)


@given(st.integers(min_value=1, max_value=10**16), st.sampled_from([0, 1, 33, 196, 330, 10_000]))
def test_gross_for_net_is_the_largest_gross_within_net(net: int, fee_rate: int) -> None:
    g = gross_for_net(net, fee_rate)
    assert net_of_fee(g, fee_rate) <= net
    assert net_of_fee(g + 1, fee_rate) > net


def test_min_stake() -> None:
    assert MIN_STAKE_RAO == 2_000_000
    assert not meets_min_stake(2_000_000, 33)               # gross must cover the fee too
    assert meets_min_stake(2_001_100, 33)
    assert meets_min_stake(2_000_000, 0)
    assert not meets_min_stake(1_999_999, 0)


def test_nominator_dust(make_globals) -> None:
    glob = make_globals()                                   # NominatorMinRequiredStake 0.02 TAO
    pool = PoolState(kind=PoolKind.BALANCER, tao=Rao(600 * 10**9), alpha=AlphaRao(600_000 * 10**9),
                     px_tao=600 * 10**9, px_alpha=600_000 * 10**9, w_quote_e18=5 * 10**17, fee_rate=33)
    # spot 0.001 TAO/alpha: 0.02 TAO = 20 alpha
    assert nominator_dust(19 * 10**9, pool, glob)
    assert not nominator_dust(20 * 10**9, pool, glob)
    assert not nominator_dust(0, pool, glob)
