"""protocol.sellload: the ONE structural sell-load model (pre- and post-v441 basket terms, autolock, burn, pushes)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from taotrader.core.fixed import DEC
from taotrader.core.units import FEE_DEN, AlphaRao, Block, Ppm, Rao
from taotrader.protocol.emission import EmissionShare
from taotrader.protocol.sellload import SellLoadParams, sell_load

TAO = 10**9
POST_V441 = Block(9_000_000)
PRE_V441 = Block(8_700_000)
C_O = Decimal(11_796) / FEE_DEN


def _share(key, chain_buy_rao_block: int) -> EmissionShare:
    return EmissionShare(key=key, b=Decimal("0.01"), keep=Decimal("0.5"), final=Decimal("0.01"), tao_per_block=Rao(5 * 10**6),
                         tao_in_per_block=Rao(5 * 10**6 - chain_buy_rao_block), chain_buy_per_block=Rao(chain_buy_rao_block))


def _subnet(make_subnet, make_pool, **kw):
    """Spot 0.001 TAO/alpha on a 600-TAO pool, 1 alpha/block, MinerBurned 0.1, escrow 50,000 alpha, rp 0.4."""
    base = make_subnet(30, pool=make_pool(600 * TAO, 600_000 * TAO), alpha_out_emission=AlphaRao(TAO),
                       miner_burned=Decimal("0.1"), escrow_alpha=AlphaRao(50_000 * TAO), root_prop=Decimal("0.4"),
                       owner_cut_autolock=False)
    return replace(base, **kw) if kw else base


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal("1e-24") * max(abs(b), Decimal(1))


def test_post_v441_components(make_subnet, make_pool, make_globals) -> None:
    glob = make_globals()
    s = _subnet(make_subnet, make_pool)
    sl = sell_load(s, glob, _share(s.key, 10**7), SellLoadParams(), POST_V441, root_flag=True)
    ae_tao = Decimal(7_200) * Decimal("0.001")                                      # AE * p = 7.2 TAO/day
    assert _close(sl.owner_tao_day, Decimal("0.8") * C_O * ae_tao)
    assert _close(sl.miner_tao_day, Decimal("0.6") * Decimal("0.5") * (1 - C_O) * Decimal("0.9") * ae_tao)
    assert _close(sl.basket_tao_day, Decimal("0.02") * 50_000 * Decimal("0.001"))  # kappa_b * E * p = 1 TAO/day
    assert _close(sl.total_tao_day, sl.owner_tao_day + sl.miner_tao_day + sl.basket_tao_day)
    assert _close(sl.sell_push_day, 2 * sl.total_tao_day / 600)                     # k_w = 1/w_base = 2
    assert _close(sl.cb_push_day, Decimal(2 * 72) / 600)                            # 0.01 TAO/block chain buy = 72/day
    assert _close(sl.coverage, Decimal(72) / sl.total_tao_day)
    # root_flag does not matter after v441: root dividends are escrowed, released by claims at kappa_b
    assert sell_load(s, glob, _share(s.key, 10**7), SellLoadParams(), POST_V441, root_flag=False) == sl


def test_pre_v441_root_dividends_sold_every_block(make_subnet, make_pool, make_globals) -> None:
    glob = make_globals()
    s = _subnet(make_subnet, make_pool)
    on = sell_load(s, glob, _share(s.key, 0), SellLoadParams(), PRE_V441, root_flag=True)
    off = sell_load(s, glob, _share(s.key, 0), SellLoadParams(), PRE_V441, root_flag=False)
    assert _close(on.basket_tao_day, Decimal("0.4") * Decimal("0.5") * (1 - C_O) * Decimal("7.2"))
    assert off.basket_tao_day == 0                                                   # sum EMA <= 1: recycled, not sold
    assert on.coverage == 0 and on.cb_push_day == 0
    first = sell_load(s, glob, _share(s.key, 0), SellLoadParams(), Block(8_765_684), root_flag=True)
    assert _close(first.basket_tao_day, Decimal(1))                                 # gate_rank32 regime: escrow model


def test_owner_liquidity_burn_and_params(make_subnet, make_pool, make_globals) -> None:
    glob = make_globals()
    s = _subnet(make_subnet, make_pool)
    share = _share(s.key, 0)
    locked = sell_load(replace(s, owner_cut_autolock=True), glob, share, SellLoadParams(), POST_V441, True)
    assert locked.owner_tao_day == 0                                                 # (1 - AL): auto-locked cut is not liquid
    no_cut = sell_load(replace(s, owner_cut_enabled=False), glob, share, SellLoadParams(), POST_V441, True)
    assert no_cut.owner_tao_day == 0
    assert no_cut.miner_tao_day > sell_load(s, glob, share, SellLoadParams(), POST_V441, True).miner_tao_day
    burned = sell_load(replace(s, miner_burned=Decimal("1.5")), glob, share, SellLoadParams(), POST_V441, True)
    assert burned.miner_tao_day == 0                                                 # min(MB, 1)
    zero = SellLoadParams(phi_owner_ppm=Ppm(0), phi_miner_ppm=Ppm(0), kappa_basket_ppm_day=Ppm(0))
    nothing = sell_load(s, glob, share, zero, POST_V441, True)
    assert nothing.total_tao_day == 0 and nothing.coverage == 0 and nothing.sell_push_day == 0   # T3 kill: phi = 0
    no_escrow = sell_load(replace(s, escrow_alpha=None), glob, share, SellLoadParams(), POST_V441, True)
    assert no_escrow.basket_tao_day == 0


def test_skewed_weights_scale_the_push(make_subnet, make_pool, make_globals) -> None:
    glob = make_globals()
    w_q = 400_000_000_000_000_000                                                    # w_base 0.6 -> k_w = 1/0.6
    s = _subnet(make_subnet, make_pool, pool=make_pool(600 * TAO, 900_000 * TAO, w_quote_e18=w_q))
    sl = sell_load(s, glob, _share(s.key, 10**7), SellLoadParams(), POST_V441, True)
    k_w = DEC.divide(Decimal(1), Decimal("0.6"))
    assert _close(sl.sell_push_day, k_w * sl.total_tao_day / 600)
    assert _close(sl.cb_push_day, k_w * 72 / 600)
    assert abs(s.pool.spot() - Decimal("0.001")) < Decimal("1e-40")                 # (w_b/w_q)*y/x
