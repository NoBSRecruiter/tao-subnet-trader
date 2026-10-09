"""SimVenue fills, N+2 latency, stride vs exact settlement and the footprint overlay (DESIGN.md 8.3, 8.4, 8.7)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported, OrderFailed, VenueAck
from taotrader.core.orders import FailReason, OrderKind, OrderState
from taotrader.core.units import AlphaRao, Block, Rao
from taotrader.protocol.amm import quote_buy, quote_sell
from taotrader.protocol.fees import tx_fee_rao
from taotrader.venues.sim import SimVenue, decay_factor

NO_MISS = ExecCfg(shield_miss_ppm=0)


def _venue(c, cfg: ExecCfg = NO_MISS, **kw) -> SimVenue:
    return SimVenue(c["BOOK"], cfg, seed=1, **kw)


# ------------------------------------------------------------------------------------------------- latency
def test_ack_is_decision_block_plus_finality_lag_plus_latency(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    ack = h.place(buy(1_000, 2 * c["TAO"]), snap(1_000))
    assert isinstance(ack, VenueAck)
    assert ack.submit_block == 1_003 and ack.expected_fill_block == 1_005
    assert ack.carrier_hash == "" and ack.inner_hash == ""
    assert h.tick(snap(1_004)) == []                      # not due before N+2


def test_per_block_fill_is_exact_at_n_plus_2(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    it = buy(1_000, 2 * c["TAO"])
    h.place(it, snap(1_000))
    (ev,) = h.tick(snap(1_005))
    assert isinstance(ev, FillReported)
    f = ev.fill
    assert f.block == 1_005 and f.exact_block and f.complete
    q = quote_buy(snap(1_005).get(c["KEY"]).pool, Rao(2 * c["TAO"]))
    assert (f.tao, f.alpha, f.swap_fee) == (q.amount_in, q.amount_out, q.fee)
    assert (f.d_pool_tao, f.d_pool_alpha) == (q.d_tao, q.d_alpha)
    assert f.tx_fee == ExecCfg().buy_tx_fee_rao and f.author_fee_tao == 0
    assert f.shares == Decimal(f.alpha)                   # hotkey index 1 in the test market
    assert f.fill_id == f"{it.order_id}:0:0"
    assert h.state(it) is OrderState.FILLED
    h.check_ledger()
    assert h.venue.cash == 98 * c["TAO"]
    assert h.bal("fee_float") == c["TAO"] - ExecCfg().buy_tx_fee_rao


def test_stride_fill_uses_first_snapshot_at_or_after_n_plus_2(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_020, c["TAO"]), snap(1_020))
    (ev,) = h.tick(snap(1_080))                           # 60-block stride: next snapshot after 1,025
    assert isinstance(ev, FillReported)
    assert ev.fill.block == 1_080 and not ev.fill.exact_block


def test_shares_are_issued_at_the_fill_snapshot_index(consts, harness, snap, subnet, hk_idx, buy) -> None:
    c = consts
    rich = hk_idx(c["HK_A"], total=60_000 * c["TAO"], shares=Decimal(40_000 * c["TAO"]))   # index 1.5
    s5 = snap(1_005, subnet(hotkeys=(rich, hk_idx(c["HK_B"]))))
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 3 * c["TAO"]), snap(1_000))
    (ev,) = h.tick(s5)
    f = ev.fill
    assert f.shares == rich.shares_for(f.alpha)
    assert abs(rich.value_of(f.shares) - f.alpha) <= 2
    assert h.venue.shares(c["KEY"], c["HK_A"]) == f.shares


def test_one_event_per_advance_in_ack_order(consts, harness, snap, subnet, pool, buy) -> None:
    c = consts
    s_other = subnet(netuid=9, reg_at=2_000)
    market = (subnet(), s_other)
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    a = buy(1_000, c["TAO"])
    b = buy(1_000, c["TAO"], key=s_other.key)
    h.place(a, snap(1_000, *market))
    h.place(b, snap(1_000, *market))
    view = h.venue.mark_to(snap(1_005, *market))
    import asyncio
    first = asyncio.run(h.venue.advance(view))
    again = asyncio.run(h.venue.advance(view))            # not observed yet: the same event again (pure)
    assert first == again and isinstance(first, FillReported) and first.fill.order_id == a.order_id
    h.commit(first)
    second = asyncio.run(h.venue.advance(h.venue.mark_to(snap(1_005, *market))))
    assert isinstance(second, FillReported) and second.fill.order_id == b.order_id
    h.commit(second)
    assert asyncio.run(h.venue.advance(h.venue.mark_to(snap(1_005, *market)))) is None


# ------------------------------------------------------------------------------------------------- sells
def test_full_sell_closes_the_position(consts, harness, snap, buy, sell) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 5 * c["TAO"]), snap(1_000))
    h.tick(snap(1_005))
    s = sell(1_060, full=True)
    assert isinstance(h.place(s, snap(1_060)), VenueAck)
    (ev,) = h.tick(snap(1_065))
    f = ev.fill
    assert f.kind is OrderKind.REMOVE_STAKE_LIMIT and f.complete
    assert h.venue.shares(c["KEY"], c["HK_A"]) == 0
    q = quote_sell(snap(1_065).get(c["KEY"]).pool, AlphaRao(f.alpha))
    assert (f.tao, f.author_fee_tao, f.swap_fee) == (q.amount_out, q.author_fee_tao, q.fee)
    assert f.d_pool_tao == -(f.tao + f.author_fee_tao) and f.d_pool_alpha == f.alpha
    assert f.tx_fee == ExecCfg().sell_tx_fee_rao
    h.check_ledger()


def test_partial_sell_keeps_the_remainder(consts, harness, snap, buy, sell) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 5 * c["TAO"]), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    part = fb.fill.alpha // 2
    h.place(sell(1_060, part), snap(1_060))
    (ev,) = h.tick(snap(1_065))
    assert ev.fill.alpha == part and ev.fill.complete
    left = h.venue.shares(c["KEY"], c["HK_A"])
    assert left == fb.fill.shares - ev.fill.shares > 0
    h.check_ledger()


def test_dust_remainder_is_force_sold_in_the_same_fill(consts, harness, snap, buy, sell) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, c["TAO"]), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    held = fb.fill.alpha                                  # ~99.9 alpha at 0.01 TAO/alpha
    leave = 1 * c["TAO"]                                  # 1 alpha ~ 0.01 TAO < NominatorMinRequiredStake 0.02 TAO
    h.place(sell(1_060, held - leave), snap(1_060))
    (ev,) = h.tick(snap(1_065))
    f = ev.fill
    assert f.alpha == held and f.shares == fb.fill.shares and f.complete
    assert h.venue.shares(c["KEY"], c["HK_A"]) == 0
    p = snap(1_065).get(c["KEY"]).pool
    q1 = quote_sell(p, AlphaRao(held - leave), partial_remaining=True)
    q2 = quote_sell(p.shifted(q1.d_tao, q1.d_alpha), AlphaRao(leave))
    assert f.tao == q1.amount_out + q2.amount_out
    assert f.swap_fee == q1.fee + q2.fee and f.author_fee_tao == q1.author_fee_tao + q2.author_fee_tao
    assert f.d_pool_tao == q1.d_tao + q2.d_tao and f.d_pool_alpha == held
    assert 0 < f.shortfall_ppm < 10_000                  # fee + impact of ~1 TAO in a 1,000-TAO pool
    h.check_ledger()


def test_allow_partial_buy_stops_at_the_limit_and_refunds(consts, harness, snap, pool, buy) -> None:
    c = consts
    from taotrader.protocol.amm import max_buy_to_limit
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    p = pool()
    limit = p.spot_rao() * 1_010 // 1_000                 # +1% marginal: ~5 TAO of room in a 1,000-TAO pool
    it = buy(1_000, 20 * c["TAO"], limit=limit, allow_partial=True)
    h.place(it, snap(1_000))
    (ev,) = h.tick(snap(1_005))
    f = ev.fill
    assert not f.complete and f.tao == max_buy_to_limit(p, limit) < 20 * c["TAO"]
    assert h.venue.cash == 100 * c["TAO"] - f.tao
    h.check_ledger()


def test_allow_partial_sell_stops_at_the_limit(consts, harness, snap, pool, buy, sell) -> None:
    c = consts
    from taotrader.protocol.amm import max_sell_to_limit
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 20 * c["TAO"]), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    p = pool()
    limit = p.spot_rao() * 995 // 1_000
    h.place(sell(1_060, limit=limit, full=True, allow_partial=True, urgency=3), snap(1_060))
    (ev,) = h.tick(snap(1_065))
    f = ev.fill
    assert not f.complete and f.alpha == max_sell_to_limit(p, limit) < fb.fill.alpha
    assert h.venue.shares(c["KEY"], c["HK_A"]) > 0
    h.check_ledger()


# ------------------------------------------------------------------------------------------------- moves
def test_move_stake_reissues_shares_at_the_destination_index(consts, harness, snap, subnet, hk_idx, buy, move) -> None:
    c = consts
    dest = hk_idx(c["HK_B"], total=30_000 * c["TAO"], shares=Decimal(20_000 * c["TAO"]))   # index 1.5
    market = subnet(hotkeys=(hk_idx(c["HK_A"]), dest))
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 5 * c["TAO"]), snap(1_000, market))
    (fb,) = h.tick(snap(1_005, market))
    cash = h.venue.cash
    m = move(1_060)
    assert isinstance(h.place(m, snap(1_060, market)), VenueAck)
    (ev,) = h.tick(snap(1_065, market))
    f = ev.fill
    assert f.kind is OrderKind.MOVE_STAKE and f.tao == 0 and f.swap_fee == 0 and f.d_pool_tao == f.d_pool_alpha == 0
    assert f.tx_fee == tx_fee_rao(OrderKind.MOVE_STAKE, ExecCfg()) == ExecCfg().move_tx_fee_rao
    assert f.alpha == fb.fill.alpha and f.shares == fb.fill.shares
    assert f.dest_hotkey == c["HK_B"] and f.dest_key == c["KEY"] and f.dest_shares == dest.shares_for(f.alpha)
    assert h.venue.shares(c["KEY"], c["HK_A"]) == 0 and h.venue.shares(c["KEY"], c["HK_B"]) == f.dest_shares
    assert h.venue.cash == cash
    assert h.venue.footprint(c["KEY"]) is None          # TEMPORARY buy footprint pruned; a move adds none
    h.check_ledger()


# ------------------------------------------------------------------------------------------------- footprint
def test_decay_factor_bounds() -> None:
    assert decay_factor(10, None) == 1 and decay_factor(0, 0) == 1 and decay_factor(1, 0) == 0
    assert decay_factor(-5, 100) == 1
    assert decay_factor(100, 100) == Decimal("0.5") and decay_factor(200, 100) == Decimal("0.25")


@pytest.mark.parametrize("half_life", [0, None, 100])
def test_mark_to_shifts_touched_pools_by_the_decayed_footprint(consts, harness, snap, buy, half_life) -> None:
    c = consts
    h = harness(_venue(c, ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=half_life)))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 10 * c["TAO"]), snap(1_000))
    (ev,) = h.tick(snap(1_005))
    f = ev.fill
    raw = snap(1_005)
    same_block = h.venue.mark_to(raw).get(c["KEY"]).pool
    assert same_block == raw.get(c["KEY"]).pool.shifted(f.d_pool_tao, f.d_pool_alpha)   # 0.5**0 == 1
    later = snap(1_105)
    pool_later = h.venue.mark_to(later).get(c["KEY"]).pool
    base = later.get(c["KEY"]).pool
    if half_life == 0:
        assert pool_later == base and h.venue.mark_to(later) is later
    elif half_life is None:
        assert pool_later == base.shifted(f.d_pool_tao, f.d_pool_alpha)
    else:
        assert pool_later == base.shifted(f.d_pool_tao // 2, -((-f.d_pool_alpha) // 2))   # truncation toward zero
    assert h.venue.mark_to(later).digest != later.digest or half_life == 0


def test_footprint_is_per_generation(consts, harness, snap, subnet, buy) -> None:
    c = consts
    h = harness(_venue(c, ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=None)))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 10 * c["TAO"]), snap(1_000))
    h.tick(snap(1_005))
    reused = snap(1_100, subnet(reg_at=1_090))            # netuid 7 dissolved and re-registered
    assert h.venue.mark_to(reused) is reused


def test_temporary_footprint_is_pruned_by_the_next_snapshot(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 10 * c["TAO"]), snap(1_000))
    h.tick(snap(1_005))
    assert h.venue.footprint(c["KEY"]) is not None
    h.observe_snapshot(snap(1_006))
    assert h.venue.footprint(c["KEY"]) is None


def test_buy_then_sell_folds_footprints(consts, harness, snap, buy, sell) -> None:
    c = consts
    h = harness(_venue(c, ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=None)))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 10 * c["TAO"]), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    h.place(sell(1_010, full=True), snap(1_010))
    (fs,) = h.tick(snap(1_015))
    fp = h.venue.footprint(c["KEY"])
    assert fp is not None and fp.stamp == Block(1_015)
    assert (fp.d_tao, fp.d_alpha) == (fb.fill.d_pool_tao + fs.fill.d_pool_tao, fb.fill.d_pool_alpha + fs.fill.d_pool_alpha)
    assert fp.d_alpha == 0 and fp.d_tao > 0             # the pool keeps our swap fees and the author payout gap


def test_infeasible_overlay_on_a_drained_pool_is_skipped(consts, harness, snap, subnet, pool, buy) -> None:
    c = consts
    h = harness(_venue(c, ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=None)))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 10 * c["TAO"]), snap(1_000))
    h.tick(snap(1_005))
    tiny = snap(1_100, subnet(pool(tao=10**9, alpha=100 * c["TAO"])))   # our footprint holds more alpha than the pool
    assert h.venue.mark_to(tiny).get(c["KEY"]).pool == tiny.get(c["KEY"]).pool


def test_failed_order_does_not_move_the_footprint_or_cash(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    it = buy(1_000, 2 * c["TAO"], limit=1)               # crossed limit
    h.place(it, snap(1_000))
    (ev,) = h.tick(snap(1_005))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.PRICE_LIMIT_EXCEEDED
    assert h.venue.footprint(c["KEY"]) is None and h.venue.cash == 100 * c["TAO"]
    assert ev.tx_fee == ExecCfg().buy_tx_fee_rao and not ev.expired and ev.exact_block


def test_remove_stake_full_limit_sells_everything_without_a_floor(consts, harness, snap, buy, sell) -> None:
    c = consts
    h = harness(_venue(c))
    h.capital(100 * c["TAO"])
    h.place(buy(1_000, 5 * c["TAO"]), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    it = sell(1_060, alpha_in=1, kind=OrderKind.REMOVE_STAKE_FULL_LIMIT, limit=0)   # Option limit None
    h.place(it, snap(1_060))
    (ev,) = h.tick(snap(1_065))
    assert isinstance(ev, FillReported) and ev.fill.alpha == fb.fill.alpha and ev.fill.shares == fb.fill.shares
    assert ev.fill.kind is OrderKind.REMOVE_STAKE_FULL_LIMIT and h.venue.shares(c["KEY"], c["HK_A"]) == 0
    h.check_ledger()
