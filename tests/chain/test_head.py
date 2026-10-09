"""chain.head.LiveChainFeed (DESIGN.md section 6.9): finalized-head following, gap fill (<= 280 blocks from the lite
role, larger gaps on the archive at stride 10), feed_gap_blocks, stall detector, bounded queue, failure handling,
HTTP polling fallback, price parity probe, Windows signal handling. Fake time, in-memory nodes."""
from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain.head import LiveChainFeed, encode_compact, header_hash, install_signal_handlers
from taotrader.chain.reader import JsonRpcChainReader
from taotrader.chain.rpc import Role, RpcPool
from taotrader.chain.scale import d_compact
from taotrader.core.errors import DecodeError
from taotrader.core.protocols import SourceItem
from taotrader.core.state import ChainSnapshot, ReadPlan
from taotrader.core.units import Block

B0 = 9_240_300                       # divisible by 60 and 100
CASSETTES = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"


class Store:
    """Minimal SnapshotStore stand-in (the feed only carries it)."""

    def __init__(self) -> None:
        self.clock = Block(0)

    def at(self, block: Block) -> ChainSnapshot:
        raise KeyError(block)

    def at_or_before(self, block: Block) -> ChainSnapshot:
        raise KeyError(block)

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        return ()


def build_chain(chain: Any, state: Any, first: int, last: int) -> dict[int, Any]:
    s = state(chain, first)
    s.subnet(1, reg_at=100)
    s.subnet(2, reg_at=200)
    out = {first: s}
    for b in range(first + 1, last + 1):
        s = s.clone(b)
        out[b] = s
    return out


class Heads:
    def __init__(self) -> None:
        self.q: asyncio.Queue[tuple[int, str, int | None]] = asyncio.Queue()

    def put(self, b: int, h: str, best: int | None = None) -> None:
        self.q.put_nowait((b, h, best))

    async def __call__(self) -> AsyncIterator[tuple[int, str, int | None]]:
        while True:
            yield await self.q.get()


async def settle(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def feed_for(reader: JsonRpcChainReader, clock: Any, heads: Heads | None, seen: list[int], **kw: Any) -> LiveChainFeed:
    def on_snapshot(s: ChainSnapshot) -> None:
        seen.append(int(s.block))

    kw.setdefault("price_check_every", None)
    return LiveChainFeed(reader, Store(), on_snapshot, heads=heads, clock=clock, sleep=clock.sleep, **kw)


def test_follows_finalized_heads(arun: Any, chain: Any, state: Any, make_reader: Any, clock: Any) -> None:
    states = build_chain(chain, state, B0, B0 + 3)
    heads = Heads()
    seen: list[int] = []
    feed = feed_for(make_reader(chain), clock, heads, seen)

    async def go() -> list[SourceItem]:
        agen = feed.stream(after=Block(B0 - 1))
        for b in range(B0, B0 + 3):
            heads.put(b, states[b].hash, b + 2)
        items = [await agen.__anext__() for _ in range(3)]
        await agen.aclose()
        return items

    items = arun(go())
    assert [int(i.snapshot.block) for i in items] == [B0, B0 + 1, B0 + 2]
    assert [i.snapshot.plan for i in items] == [ReadPlan.FULL, ReadPlan.HEAD, ReadPlan.HEAD]
    assert seen[:3] == [B0, B0 + 1, B0 + 2]                    # recorded before being yielded
    assert all(i.health.feed_gap_blocks == 0 for i in items)
    assert items[-1].health.finality_lag_blocks == 2
    assert items[-1].health.healthy_endpoints == 1
    assert feed.cadence_blocks == 1 and feed.store is not None


def test_starts_at_current_head_when_after_is_none(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                   clock: Any) -> None:
    states = build_chain(chain, state, B0, B0 + 1)
    heads = Heads()
    feed = feed_for(make_reader(chain), clock, heads, [])

    async def go() -> SourceItem:
        agen = feed.stream(after=None)
        heads.put(B0 + 1, states[B0 + 1].hash)
        item = await agen.__anext__()
        await agen.aclose()
        return item

    item = arun(go())
    assert int(item.snapshot.block) == B0 + 1 and item.snapshot.plan is ReadPlan.FULL


def test_gap_fill_lite_window_and_archive_stride(arun: Any, fake_chain_cls: Any, state: Any, clock: Any) -> None:
    archive = fake_chain_cls("fake://archive")
    lite = fake_chain_cls("fake://lite")
    states = build_chain(archive, state, B0, B0 + 30)
    for attr in ("hashes", "numbers", "versions", "storage", "rt", "headers"):
        setattr(lite, attr, getattr(archive, attr))
    target = B0 + 300
    archive.hashes[target] = lite.hashes[target] = "0x" + "77" * 32
    eps = [RpcPool.make_endpoint(t.url, role, transport=t, clock=clock, sleep=clock.sleep, rate_per_s=1e6, burst=10**6,
                                 label=role.value) for t, role in ((lite, Role.HEAD), (archive, Role.ARCHIVE))]
    reader = JsonRpcChainReader(RpcPool(eps, clock=clock, sleep=clock.sleep), provider_check_every=None)
    heads = Heads()
    feed = feed_for(reader, clock, heads, [], lite_window=280, archive_stride=10)

    async def go() -> list[SourceItem]:
        agen = feed.stream(after=Block(B0 - 1))
        heads.put(target, "0x" + "77" * 32)
        items = [await agen.__anext__() for _ in range(5)]
        await agen.aclose()
        return items

    items = arun(go())
    assert [int(i.snapshot.block) for i in items] == [B0, B0 + 10, B0 + 20, B0 + 21, B0 + 22]
    assert [i.health.feed_gap_blocks for i in items] == [0, 9, 9, 0, 0]
    arch_reads = {p[1] for p in archive.calls("state_queryStorageAt")}
    lite_reads = {p[1] for p in lite.calls("state_queryStorageAt")}
    assert {states[b].hash for b in (B0, B0 + 10, B0 + 20)} <= arch_reads
    assert {states[b].hash for b in (B0 + 21, B0 + 22)} <= lite_reads
    assert not {states[b].hash for b in (B0 + 21, B0 + 22)} & arch_reads


def test_stall_detector(arun: Any, chain: Any, state: Any, make_reader: Any, clock: Any) -> None:
    states = build_chain(chain, state, B0, B0)
    heads = Heads()
    feed = feed_for(make_reader(chain), clock, heads, [])

    async def go() -> None:
        agen = feed.stream(after=Block(B0 - 1))
        heads.put(B0, states[B0].hash)
        await agen.__anext__()
        assert not feed.stalled and feed.health().secs_since_block == 0
        clock.t += 37.0
        assert feed.stalled and feed.health().secs_since_block == 37
        await agen.aclose()

    arun(go())


def test_queue_overflow_skips_blocks_and_forces_full(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                     clock: Any) -> None:
    states = build_chain(chain, state, B0, B0 + 8)
    heads = Heads()
    feed = feed_for(make_reader(chain), clock, heads, [], queue_max=1, full_every=5)

    async def go() -> list[SourceItem]:
        agen = feed.stream(after=Block(B0 - 1))
        heads.put(B0, states[B0].hash)
        first = await agen.__anext__()
        for b in range(B0 + 1, B0 + 7):
            heads.put(b, states[b].hash)
        await settle(400)                         # producer fills the queue (B0+1) and skips the rest
        items = [first, await agen.__anext__()]
        heads.put(B0 + 8, states[B0 + 8].hash)
        items.append(await agen.__anext__())
        await agen.aclose()
        return items

    items = arun(go())
    blocks = [int(i.snapshot.block) for i in items]
    assert blocks[:2] == [B0, B0 + 1]
    assert blocks[2] > B0 + 2 and items[2].health.feed_gap_blocks == blocks[2] - (B0 + 1) - 1 > 0
    assert feed.skipped_blocks >= 1
    assert items[2].snapshot.plan is ReadPlan.FULL           # the scheduled FULL at B0+5 was skipped


def test_undecodable_block_is_skipped_and_reported(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                   clock: Any) -> None:
    states = build_chain(chain, state, B0, B0 + 2)
    states[B0 + 1].put(it.SUBNET[it.NETWORKS_ADDED], b"\x07", netuid=1)       # invalid bool at B0+1 only
    heads = Heads()
    errors: list[tuple[int, BaseException]] = []
    feed = feed_for(make_reader(chain), clock, heads, [], on_error=lambda b, e: errors.append((b, e)))

    async def go() -> list[SourceItem]:
        agen = feed.stream(after=Block(B0 - 1))
        for b in range(B0, B0 + 3):
            heads.put(b, states[b].hash)
        items = [await agen.__anext__() for _ in range(2)]
        await agen.aclose()
        return items

    items = arun(go())
    assert [int(i.snapshot.block) for i in items] == [B0, B0 + 2]
    assert items[1].health.feed_gap_blocks == 1
    assert [b for b, _ in errors] == [B0 + 1] and isinstance(errors[0][1], DecodeError)
    assert feed.failed_blocks == [B0 + 1]


def test_persistent_rpc_failure_ends_stream(arun: Any, chain: Any, state: Any, make_reader: Any, clock: Any) -> None:
    from taotrader.chain.rpc import RpcFatal

    states = build_chain(chain, state, B0, B0)
    chain.chaos = lambda m, p: RpcFatal("Invalid params") if m == "state_queryStorageAt" else None
    heads = Heads()
    feed = feed_for(make_reader(chain), clock, heads, [], max_consecutive_failures=3)

    async def go() -> None:
        agen = feed.stream(after=Block(B0 - 1))
        heads.put(B0, states[B0].hash)
        with pytest.raises(RpcFatal):
            await agen.__anext__()

    arun(go())
    assert feed.failed_blocks == [B0] * 4


def test_http_poll_fallback_without_ws(arun: Any, chain: Any, state: Any, make_reader: Any, clock: Any) -> None:
    states = build_chain(chain, state, B0, B0 + 1)
    chain.finalized = states[B0 + 1].hash
    feed = feed_for(make_reader(chain), clock, None, [], fallback_after_s=30.0, poll_s=3.0)

    async def go() -> SourceItem:
        agen = feed.stream(after=Block(B0))
        item = await agen.__anext__()
        await agen.aclose()
        return item

    item = arun(go())
    assert int(item.snapshot.block) == B0 + 1
    assert chain.calls("chain_getFinalizedHead")
    assert any("finalized subscription" in e for e in feed.errors)


def test_price_parity_probe_reports_drift(arun: Any, chain: Any, state: Any, clock: Any, make_reader: Any) -> None:
    states = build_chain(chain, state, B0, B0)
    bad = encode_compact(2) + b"".join(n.to_bytes(2, "little") + (1).to_bytes(8, "little") for n in (1, 2))
    states[B0].rt("SwapRuntimeApi_current_alpha_price_all", "0x", bad)
    drift: list[Any] = []
    reader = make_reader(chain, on_drift=drift.append)
    heads = Heads()
    feed = feed_for(reader, clock, heads, [], price_check_every=100)

    async def go() -> None:
        agen = feed.stream(after=Block(B0 - 1))
        heads.put(B0, states[B0].hash)
        await agen.__anext__()
        await agen.aclose()

    arun(go())
    assert drift and drift[0].probe == "price_all" and drift[0].block == B0 and drift[0].err_ppm > 0


def test_header_hash_matches_chain() -> None:
    d = json.loads((CASSETTES / "header_9240388.json").read_text(encoding="utf-8"))
    assert header_hash(d["result"]) == d["block_hash"]
    for n in (0, 1, 63, 64, 16_383, 16_384, 2**30 - 1, 2**30, 2**64 - 1, 2**100):
        assert d_compact(encode_compact(n)) == (n, len(encode_compact(n)))


def test_signal_handlers_set_stop_event(arun: Any) -> None:
    old_int = signal.getsignal(signal.SIGINT)
    sigbreak = getattr(signal, "SIGBREAK", None)
    old_break = signal.getsignal(sigbreak) if sigbreak is not None else None
    try:
        async def go() -> bool:
            stop = asyncio.Event()
            install_signal_handlers(asyncio.get_running_loop(), stop)
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
            await asyncio.wait_for(stop.wait(), 1.0)
            return stop.is_set()

        assert arun(go())
    finally:
        signal.signal(signal.SIGINT, old_int)
        if sigbreak is not None and old_break is not None:
            signal.signal(sigbreak, old_break)


def test_protocol_conformance() -> None:
    """Static (mypy --strict on tests) and runtime shape checks: the reader is a core ChainReader, the feed a core
    DataSource."""
    from taotrader.core.protocols import ChainReader, DataSource

    impl = JsonRpcChainReader(RpcPool([]))
    reader: ChainReader = impl
    feed: DataSource = LiveChainFeed(impl, Store(), lambda _s: None)
    for name in ("block_hash", "finalized_head", "snapshot", "dividend_keys", "sim_swap_buy", "sim_swap_sell",
                 "prices_all", "subnet_to_prune", "registration_cost", "escrow_by_subnet", "spec_version"):
        assert callable(getattr(reader, name))
    assert feed.cadence_blocks == 1 and callable(feed.stream) and callable(feed.aclose)
