"""Adversarial-review regressions for WP6 (venues.sim / venues.paper).

Each test pins a defect found in review (DESIGN.md sections 3.12, 8.4, 8.5, 9.1; brief 5.9):
- the paper shield-era staleness boundary (a mortal era of 8 born at the finalized anchor covers anchor..anchor+7, so
  N+2 = anchor + 8 can never be included; design: "finality lag > 5 blocks (the shield era is stale)");
- a MOVE_STAKE on an empty pool must fail like the chain, never raise out of advance() (a raise there is a crash loop:
  the rebuilt venue re-raises on the same pending order at every restart).
"""
from __future__ import annotations

from typing import Any

import pytest

from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported, OrderFailed, VenueAck
from taotrader.core.orders import FailReason, OrderState
from taotrader.core.state import ChainSnapshot, ReadPlan
from taotrader.core.units import Block, BlockHash, Hotkey, NetUid, SubnetKey
from taotrader.venues.paper import PaperVenue
from taotrader.venues.sim import SimVenue

TAO = 10**9
CFG = ExecCfg(shield_miss_ppm=0)


class _NoReader:
    """ChainReader double that is never expected to be called on these paths."""

    async def block_hash(self, block: Block) -> BlockHash:
        raise KeyError(block)

    async def snapshot(self, block: Block, block_hash: BlockHash, plan: ReadPlan, prev: ChainSnapshot | None,
                       tracked: Any) -> ChainSnapshot:
        raise KeyError(block)

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> Any:
        raise ConnectionError("not wired")

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> Any:
        raise ConnectionError("not wired")


# ------------------------------------------------------------------------------------------------- paper shield era
@pytest.mark.parametrize(("lag", "stale"), [(3, False), (5, False), (6, True), (7, True)])
def test_paper_shield_era_is_stale_when_finality_lag_exceeds_5(consts, harness, snap, buy, lag, stale) -> None:
    """Era 8 anchored at finalized b is valid in blocks b..b+7 (death = birth + period). N+2 = b + lag + 2 must be <= b+7,
    i.e. lag <= 5 (DESIGN 3.12 'finality lag > 5 blocks (the shield era is stale)'). Lag 6 puts N+2 at b+8: the carrier
    can never be included there, so paper must not model a fill (it is a fee-free pre-submit reject)."""
    h = harness(PaperVenue(consts["BOOK"], CFG, reader=_NoReader(), seed=1))
    h.capital(100 * TAO)
    h.observe_snapshot(snap(1_000), lag=lag)
    ev = h.place(buy(1_000, TAO), snap(1_000))
    if stale:
        assert isinstance(ev, OrderFailed) and ev.reason is FailReason.VENUE_REJECT and ev.tx_fee == 0, ev
        assert ev.detail.startswith("shield_era_stale")
    else:
        assert isinstance(ev, VenueAck), ev
        assert ev.expected_fill_block == 1_000 + lag + 2 <= 1_000 + 7


def test_paper_resolve_re_derives_the_same_stale_reject(consts, harness, snap, buy, arun) -> None:
    """resolve() after a crash must reproduce the submit-time verdict at lag 6 (NOT_PLACED + the stale reject)."""
    from taotrader.core.events import OrderIntended, SubmitStarted, SubmitUnknown
    from taotrader.core.orders import Resolution

    c = consts
    v = PaperVenue(c["BOOK"], CFG, reader=_NoReader(), seed=1)
    h = harness(v)
    h.capital(100 * TAO)
    h.observe_snapshot(snap(1_000), lag=6)
    it = buy(1_000, TAO)
    h.commit(OrderIntended(it))
    d, n, era = arun(v.reserve(it, snap(1_000)))
    h.commit(SubmitStarted(book=c["BOOK"], order_id=it.order_id, attempt=0, delegate=d, nonce=n, era_end=era))
    h.commit(SubmitUnknown(book=c["BOOK"], order_id=it.order_id, attempt=0, detail="recovered_submitting"))
    res, evs = arun(v.resolve(it, snap(1_003)))
    assert res is Resolution.NOT_PLACED
    (ev,) = evs
    assert isinstance(ev, OrderFailed) and ev.tx_fee == 0 and ev.detail.startswith("shield_era_stale")


# ------------------------------------------------------------------------------------------------- fail-closed AMM edges
def test_move_on_an_empty_pool_fails_closed_instead_of_raising(consts, harness, snap, subnet, pool, buy, move) -> None:
    """A same-subnet move values the moved alpha at spot. On a pool with no alpha reserve the spot is undefined; the
    venue must return a fee-paying chain failure (the chain's price is 0 there, so the move is below DefaultMinStake),
    never let decimal.DivisionByZero escape advance()."""
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    assert isinstance(fb, FillReported)
    h.place(move(1_060), snap(1_060))
    empty = subnet(pool(tao=1_000 * TAO, alpha=0))
    (ev,) = h.tick(snap(1_065, empty))
    assert isinstance(ev, OrderFailed) and ev.tx_fee == CFG.move_tx_fee_rao, ev
    assert ev.reason in (FailReason.AMOUNT_TOO_LOW, FailReason.RESERVES_TOO_LOW)
    assert h.state(move(1_060)) is OrderState.FAILED
    assert h.venue.shares(c["KEY"], c["HK_A"]) == fb.fill.shares            # nothing moved
    h.check_ledger()


def test_sell_and_buy_on_an_empty_pool_fail_closed(consts, harness, snap, subnet, pool, buy, sell) -> None:
    """The swap paths already map an empty pool to ReservesTooLow; pinned here next to the move case."""
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO), snap(1_000))
    h.tick(snap(1_005))
    h.place(sell(1_060, full=True), snap(1_060))
    (ev,) = h.tick(snap(1_065, subnet(pool(tao=1_000 * TAO, alpha=0))))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.RESERVES_TOO_LOW
    h.place(buy(1_120, TAO, hotkey=Hotkey(c["HK_A"])), snap(1_120))
    (ev2,) = h.tick(snap(1_125, subnet(pool(tao=1_000 * TAO, alpha=0))))
    assert isinstance(ev2, OrderFailed) and ev2.reason is FailReason.RESERVES_TOO_LOW
    assert SubnetKey(NetUid(c["NETUID"]), Block(c["REG_AT"])) == c["KEY"]
    h.check_ledger()
