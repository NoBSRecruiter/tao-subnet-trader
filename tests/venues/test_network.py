"""Network acceptance for PaperVenue (WP6; DESIGN.md 9.1, section 13 Q14). Read-only JSON-RPC against the public
OnFinality archive through the WP1 reader (its token bucket keeps us at <= 3 req/s). Deselected by default; run with
`-m network`.

One live paper order end to end: FULL snapshot at b0, a 1-TAO shielded buy on a real subnet, settlement on the recorded
state of exactly N+2 = b0 + 5 with the sim_swap drift probe at that block hash (must stay <= 5 bp: no
ModelDriftObserved), the same order settled through the feed-gap path (the venue reads N+2 itself), and a sell-side
probe on the bought alpha. Nothing is signed or submitted: sim_swap is a read-only runtime API.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from taotrader.chain.reader import JsonRpcChainReader
from taotrader.chain.rpc import RpcPool
from taotrader.core.config import ExecCfg
from taotrader.core.events import (
    CapitalChanged,
    FillReported,
    JournalEvent,
    OrderIntended,
    SubmitStarted,
    VenueAck,
)
from taotrader.core.orders import OrderKind
from taotrader.core.state import ChainSnapshot, PoolKind, ReadPlan, SubnetState
from taotrader.core.units import PPM, Block, BookId, Rao
from taotrader.protocol.amm import marginal_after_buy, quote_buy
from taotrader.venues.paper import PaperVenue

pytestmark = pytest.mark.network

ARCHIVE = "https://bittensor-finney.api.onfinality.io/public"
TAO = 10**9
_POOL: list[RpcPool] = []


def _pick(s: ChainSnapshot) -> SubnetState:
    """A tradable Balancer subnet with >= 300 TAO and an owner-hotkey share index (largest pool first)."""
    ok = [x for x in s.subnets if x.subtoken_enabled and x.pool.kind is PoolKind.BALANCER and x.pool.tao >= 300 * TAO
          and x.owner_hotkey is not None and (h := x.hotkey(x.owner_hotkey)) is not None and h.total_shares > 0]
    assert ok, "no tradable subnet with an owner hotkey index"
    return max(ok, key=lambda x: (int(x.pool.tao), -int(x.key.netuid)))


async def _place(venue: PaperVenue, intent: Any, now: ChainSnapshot) -> JournalEvent:
    venue.observe(OrderIntended(intent))
    d, n, era = await venue.reserve(intent, now)
    venue.observe(SubmitStarted(book=intent.book, order_id=intent.order_id, attempt=intent.attempt, delegate=d, nonce=n,
                                era_end=era))
    ev = await venue.submit(intent, now)
    venue.observe(ev)
    return ev


def test_live_paper_fill_has_no_model_drift(consts: dict[str, Any], buy: Any) -> None:
    async def go() -> dict[str, Any]:
        pool = RpcPool.from_urls(archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
        _POOL[:] = [pool]
        reader = JsonRpcChainReader(pool, provider_check_every=None)
        head, _ = await reader.finalized_head()
        b0 = int(head) - 400
        s0 = await reader.snapshot(Block(b0), await reader.block_hash(Block(b0)), ReadPlan.FULL, None, ())
        sub = _pick(s0)
        assert sub.owner_hotkey is not None
        hk = sub.owner_hotkey
        m = marginal_after_buy(sub.pool, Rao(TAO))
        intent = buy(b0, TAO, limit=m * (PPM + 50_000) // PPM, key=sub.key, hotkey=hk)   # 5% for the 5-block wait
        cfg = ExecCfg(shield_miss_ppm=0)
        out: dict[str, Any] = {"netuid": int(sub.key.netuid), "b0": b0}

        # (1) the normal path: the Runner ticks N+2 and the venue settles on that recorded state
        v1 = PaperVenue(BookId(consts["BOOK"]), cfg, reader=reader)
        v1.observe(CapitalChanged(book=v1.book, block=Block(b0), cash_delta=100 * TAO, fee_float_delta=TAO, memo="init"))
        ack = await _place(v1, intent, s0)
        assert isinstance(ack, VenueAck) and ack.expected_fill_block == b0 + 5
        b5 = b0 + 5
        s5 = await reader.snapshot(Block(b5), await reader.block_hash(Block(b5)), ReadPlan.FULL, s0, ((sub.key, hk),))
        ev1 = await v1.advance(v1.mark_to(s5))
        assert isinstance(ev1, FillReported), ev1                    # a ModelDriftObserved here = > 5 bp drift
        f1 = ev1.fill
        s5sub = s5.get(sub.key)
        assert s5sub is not None
        assert f1.alpha == quote_buy(s5sub.pool, Rao(TAO)).amount_out and f1.block == b5 and f1.exact_block
        chain = await reader.sim_swap_buy(sub.key.netuid, TAO, s5.block_hash)
        out["buy_local"], out["buy_chain"] = f1.alpha, chain.alpha_amount
        v1.observe(ev1)

        # (2) the feed-gap path: the venue reads N+2 through the reader itself and settles identically
        v2 = PaperVenue(BookId("b2"), cfg, reader=reader)
        v2.observe(CapitalChanged(book=v2.book, block=Block(b0), cash_delta=100 * TAO, fee_float_delta=TAO, memo="init"))
        intent2 = replace(intent, book=BookId("b2"))
        await _place(v2, intent2, s0)
        late = replace(s5, block=Block(b5 + 4))                     # the feed skipped N+2
        ev2 = await v2.advance(v2.mark_to(late))
        assert isinstance(ev2, FillReported), ev2
        assert (ev2.fill.block, ev2.fill.alpha, ev2.fill.exact_block) == (b5, f1.alpha, True)
        assert v2.state_errors == 0

        # (3) a sell-side probe on the bought alpha at the same block (read-only sim_swap_alpha_for_tao)
        sell_fill = replace(f1, kind=OrderKind.REMOVE_STAKE_LIMIT, alpha=f1.alpha)
        drift = await v1._probe(sell_fill, s5sub.pool, s5)
        sim = await reader.sim_swap_sell(sub.key.netuid, f1.alpha, s5.block_hash)
        out["sell_drift"], out["sell_chain"] = drift, sim.tao_amount
        out["probe_errors"] = v1.probe_errors + v2.probe_errors
        return out

    async def guarded() -> dict[str, Any]:
        try:
            return await go()
        finally:
            await _POOL[0].aclose() if _POOL else None

    out = asyncio.run(guarded())
    print(f"WP6 network probe: {out}")
    assert out["probe_errors"] == 0
    assert abs(out["buy_local"] - out["buy_chain"]) * 1_000_000 <= 500 * out["buy_chain"], out
    assert out["sell_drift"] is None and out["sell_chain"] > 0, out
