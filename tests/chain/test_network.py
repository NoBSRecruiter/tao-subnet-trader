"""Network acceptance tests for the chain reader (DESIGN.md section 11 WP1). Read-only JSON-RPC against public
Bittensor endpoints at <= 3 req/s (the reader's own token bucket). Deselected by default; run with `-m network`.

- price parity <= 1e-6 vs SwapRuntimeApi_current_alpha_price_all at 3 blocks;
- get_subnet_to_prune parity (brief 4.3 ladder on the reader's raw fields) at the same blocks;
- era-B pool at 7,000,020 reproduces live sim_swap <= 3e-7;
- 6-block historical pull at >= 1.5 snapshots/s with a 3 req/s bucket;
- verify-metadata against the live runtime;
- the live feed over the WebSocket head role (finalized-head subscription, header hashing, finality lag sample).
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest

from taotrader.chain import metadata as md
from taotrader.chain.head import LiveChainFeed
from taotrader.chain.reader import JsonRpcChainReader, compare_prices
from taotrader.chain.rpc import Role, RpcPool
from taotrader.core.state import ChainSnapshot, PoolKind, ReadPlan
from taotrader.core.units import Block, BlockHash, NetUid

pytestmark = pytest.mark.network

ARCHIVE = "https://bittensor-finney.api.onfinality.io/public"
HEAD_WS = "wss://entrypoint-finney.opentensor.ai:443"


def archive_reader(**kw: Any) -> JsonRpcChainReader:
    pool = RpcPool.from_urls(archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
    kw.setdefault("provider_check_every", None)
    return JsonRpcChainReader(pool, **kw)


async def _full(reader: JsonRpcChainReader, block: int) -> ChainSnapshot:
    h = await reader.block_hash(Block(block))
    return await reader.snapshot(Block(block), h, ReadPlan.FULL, None, ())


def _ladder_target(s: ChainSnapshot) -> int | None:
    imm = s.glob.immunity_period
    cands = [x for x in s.subnets if int(s.block) >= int(x.key.reg_at) + imm]
    return None if not cands else int(min(cands, key=lambda x: (x.moving_price, int(x.key.reg_at))).key.netuid)


def test_price_and_prune_parity_three_blocks() -> None:
    async def go() -> list[tuple[int, int, int, int | None, int | None]]:
        reader = archive_reader(prune_check_every=1)
        try:
            head, _ = await reader.finalized_head()
            out = []
            for b in (9_240_388, 9_240_878, int(head) - 20):
                snap = await _full(reader, b)
                pp = compare_prices(snap, await reader.prices_all(snap.block_hash), rel_ppm=1)
                assert pp.ok, (b, pp.mismatches)
                assert pp.n == len(snap.subnets) >= 100
                rpt = snap.glob.runtime_prune_target
                out.append((b, pp.n, pp.max_rel_ppm, None if rpt is None else int(rpt), _ladder_target(snap)))
            return out
        finally:
            await reader.pool.aclose()

    rows = asyncio.run(go())
    print("price/prune parity:", rows)
    for _b, _n, max_rel_ppm, runtime_target, local_target in rows:
        assert max_rel_ppm <= 1
        assert runtime_target == local_target
    assert rows[0][3] == 92


def test_era_b_pool_reproduces_live_sim_swap() -> None:
    async def go() -> list[tuple[int, str, int, int, int]]:
        reader = archive_reader()
        try:
            snap = await _full(reader, 7_000_020)
            assert snap.glob.spec_version == 348 and reader.layouts.accepted(348)
            out = []
            for net in (1, 19, 64):
                s = snap.by_netuid(net)
                assert s is not None and s.pool.kind is PoolKind.CP_V3_VIRTUAL
                p = s.pool
                for tao in (10**9, 10 * 10**9, 100 * 10**9):
                    sim = await reader.sim_swap_buy(NetUid(net), tao, snap.block_hash)
                    fee = tao * p.fee_rate // 65_535
                    model = p.px_alpha * (tao - fee) // (p.px_tao + tao - fee)
                    out.append((net, "buy", tao, model, sim.alpha_amount))
                alpha = sim.alpha_amount // 100                     # ~1 TAO worth
                sell = await reader.sim_swap_sell(NetUid(net), alpha, snap.block_hash)
                fee = alpha * p.fee_rate // 65_535
                model = p.px_tao * (alpha - fee) // (p.px_alpha + alpha - fee)
                out.append((net, "sell", alpha, model, sell.tao_amount))
            return out
        finally:
            await reader.pool.aclose()

    rows = asyncio.run(go())
    print("era-B parity:", rows)
    for _net, _side, _amt, model, chain in rows:
        assert abs(Decimal(model) / Decimal(chain) - 1) <= Decimal("3e-7")


def test_six_block_pull_rate() -> None:
    """6 consecutive blocks (1 FULL + 5 HEAD) through one 3 req/s bucket: 13 calls with keys_per_call = 2,500 (the
    design's measured-OK range is 2,332-2,732), so the pull is rate-bound at ~1.7 snapshots/s. Public-endpoint latency
    has multi-second outliers, so the best of 3 attempts is asserted; every attempt is printed."""
    blocks = list(range(9_240_361, 9_240_367))

    async def attempt() -> tuple[float, list[ChainSnapshot], int]:
        reader = archive_reader(keys_per_call=2_500)
        try:
            t0 = time.monotonic()
            snaps = [s async for s in reader.pull(blocks)]
            return time.monotonic() - t0, snaps, sum(reader.pool.ru_used().values())
        finally:
            await reader.pool.aclose()

    rates = []
    for _ in range(3):
        dt, snaps, calls = asyncio.run(attempt())
        assert [int(s.block) for s in snaps] == blocks
        assert snaps[0].plan is ReadPlan.FULL and all(s.plan is ReadPlan.HEAD for s in snaps[1:])
        assert calls <= 14
        rates.append(len(snaps) / dt)
        print(f"6-block pull: {dt:.2f} s, {len(snaps) / dt:.2f} snapshots/s, {calls} calls")
        if rates[-1] >= 1.5:
            break
    assert max(rates) >= 1.5, rates


def test_verify_metadata_against_live_runtime() -> None:
    layout, problems = asyncio.run(md.verify_metadata(endpoint=ARCHIVE))
    print("live spec", layout.spec_version, "problems:", problems)
    assert layout.validated, layout.notes                     # every registry row matches the live runtime
    if md.SpecLayouts().exact(layout.spec_version) is not None:
        assert problems == []


def test_live_feed_over_websocket() -> None:
    """Three finalized heads from the WS head role; finalized hashes are computed from headers (header_hash), and the
    snapshot hash equals chain_getBlockHash of its number."""
    async def go() -> list[Any]:
        pool = RpcPool.from_urls(head=(HEAD_WS,), archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
        reader = JsonRpcChainReader(pool, provider_check_every=None)

        class Store:
            clock = Block(0)

        feed = LiveChainFeed(reader, Store(), lambda s: None, price_check_every=None)  # type: ignore[arg-type]
        items = []
        try:
            agen = feed.stream(after=None)
            for _ in range(3):
                items.append(await asyncio.wait_for(agen.__anext__(), 120))
            await agen.aclose()
            for it_ in items:
                assert await reader.block_hash(it_.snapshot.block) == BlockHash(it_.snapshot.block_hash)
        finally:
            await feed.aclose()
            await pool.aclose()
        return items

    items = asyncio.run(go())
    lags = [i.health.finality_lag_blocks for i in items]
    print("finality lag samples (blocks):", lags, "blocks:", [int(i.snapshot.block) for i in items])
    assert [int(i.snapshot.block) for i in items] == list(range(int(items[0].snapshot.block),
                                                               int(items[0].snapshot.block) + 3))
    assert all(0 <= lag <= 20 for lag in lags)
    assert items[0].snapshot.plan is ReadPlan.FULL and items[1].snapshot.plan in (ReadPlan.HEAD, ReadPlan.FULL)


def test_lite_node_state_discarded_reroutes_to_archive() -> None:
    """A read far behind the lite window on the WS head role is answered via the archive re-route."""
    async def go() -> int:
        pool = RpcPool.from_urls(head=(HEAD_WS,), archive=(ARCHIVE,), rate_per_s=3.0, burst=3)
        reader = JsonRpcChainReader(pool, role=Role.HEAD, provider_check_every=None)
        try:
            h = await reader.block_hash(Block(9_240_388))
            snap = await reader.snapshot(Block(9_240_388), h, ReadPlan.FULL, None, (), role=Role.HEAD)
            return len(snap.subnets)
        finally:
            await pool.aclose()

    assert asyncio.run(go()) == 128
