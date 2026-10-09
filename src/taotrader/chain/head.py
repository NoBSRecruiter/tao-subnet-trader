"""taotrader/chain/head.py - LiveChainFeed: the paper/live DataSource over finalized heads (DESIGN.md section 6.9).

- Input: `chain_subscribeFinalizedHeads` on the head role (no reorg handling needed). A parallel
  `chain_subscribeNewHeads` gives the best head: HealthObs.finality_lag_blocks = best - finalized.
- Fallback: if no finalized head arrives for 30 s or the WebSocket drops, poll `chain_getFinalizedHead` over the
  archive/HTTP role every 3 s (the pool rotates endpoints) and retry the subscription in the background.
- Gap fill: gaps <= 280 blocks are read per block from the head role (lite nodes keep ~300 blocks of state); a larger
  gap is crossed on the archive at stride 10 until the feed is within 280 blocks of the head, then per block. The first
  item after a gap carries `feed_gap_blocks`.
- Plans: FULL at block % 60 == 0 (and for the first snapshot), HEAD otherwise (FULL-only fields carried).
- Queue bounded at 64: when full, intermediate blocks are skipped (not read, not delivered) and the next delivered item
  carries `feed_gap_blocks` > 0. Skipped blocks coarsen events but cannot hide them (diffs span the gap). A FULL read
  is forced when the last one is >= 60 blocks old.
- Read failures: a transient RPC failure retries the same block after a pause; an undecodable block is reported via
  `on_error(block, exc)` and skipped (never a partial snapshot). More than `max_consecutive_failures` in a row ends
  the stream with the last error.
- Stall detector: `health().secs_since_block` grows while no finalized head arrives; `stalled` after 3 x 12 s.
- Price parity vs `current_alpha_price_all` every 100 blocks; a breach is reported through the reader's on_drift.
- Windows: default Proactor loop; `install_signal_handlers` uses signal.signal(SIGINT/SIGBREAK) to set a shutdown
  event (no loop.add_signal_handler).

The SnapshotStore and the recorder callback (`on_snapshot`, hot staging with fsync) are injected, so chain/ never
imports data/. `on_snapshot` runs BEFORE the item is yielded (the journal may reference the snapshot only after it is
durable).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import signal
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from taotrader.core.errors import DecodeError
from taotrader.core.events import HealthObs, ModelDriftObserved
from taotrader.core.protocols import SnapshotStore, SourceItem
from taotrader.core.state import ChainSnapshot, ReadPlan
from taotrader.core.units import BLOCK_SECONDS, PPM, Block, BlockHash, Hotkey, SubnetKey

from .hashing import from_hex
from .reader import JsonRpcChainReader
from .rpc import Role, RpcError, Subscription
from .scale import d_compact

LITE_WINDOW_BLOCKS: Final[int] = 280
ARCHIVE_STRIDE_BLOCKS: Final[int] = 10
QUEUE_MAX: Final[int] = 64
FALLBACK_AFTER_S: Final[float] = 30.0
POLL_S: Final[float] = 3.0
STALL_S: Final[float] = 3.0 * BLOCK_SECONDS
FULL_EVERY_BLOCKS: Final[int] = 60
PRICE_CHECK_EVERY_BLOCKS: Final[int] = 100
MAX_CONSECUTIVE_FAILURES: Final[int] = 10


def encode_compact(n: int) -> bytes:
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    if n < 1 << 30:
        return ((n << 2) | 2).to_bytes(4, "little")
    raw = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([((len(raw) - 4) << 2) | 3]) + raw


def header_hash(hdr: Mapping[str, Any]) -> BlockHash:
    """blake2b-256 of the SCALE header: parentHash ++ Compact(number) ++ stateRoot ++ extrinsicsRoot ++ Vec<DigestItem>
    (each digest log is already SCALE hex in the RPC header). Saves a chain_getBlockHash per finalized head."""
    logs = [from_hex(str(x)) for x in (hdr.get("digest") or {}).get("logs", [])]
    enc = (from_hex(str(hdr["parentHash"])) + encode_compact(int(str(hdr["number"]), 16)) + from_hex(str(hdr["stateRoot"]))
           + from_hex(str(hdr["extrinsicsRoot"])) + encode_compact(len(logs)) + b"".join(logs))
    return BlockHash("0x" + hashlib.blake2b(enc, digest_size=32).hexdigest())


def _check_compact_roundtrip(n: int) -> bool:   # used by tests
    return d_compact(encode_compact(n))[0] == n


@dataclass
class _Heads:
    finalized: int = -1
    finalized_hash: str = ""
    best: int = -1
    last_finalized_at: float | None = None
    via_ws: bool = False


class LiveChainFeed:
    """core.protocols.DataSource over finalized heads (paper / live_dry / live)."""

    cadence_blocks = 1

    def __init__(self, reader: JsonRpcChainReader, store: SnapshotStore,
                 on_snapshot: Callable[[ChainSnapshot], Awaitable[None] | None], *,
                 tracked: Callable[[ChainSnapshot | None], Sequence[tuple[SubnetKey, Hotkey]]] = lambda _p: (),
                 held: Callable[[], Iterable[int]] = lambda: (),
                 metagraph_for: Callable[[], Iterable[int]] = lambda: (),
                 heads: Callable[[], AsyncIterator[tuple[int, str, int | None]]] | None = None,
                 full_every: int = FULL_EVERY_BLOCKS, lite_window: int = LITE_WINDOW_BLOCKS,
                 archive_stride: int = ARCHIVE_STRIDE_BLOCKS, queue_max: int = QUEUE_MAX,
                 fallback_after_s: float = FALLBACK_AFTER_S, poll_s: float = POLL_S, stall_s: float = STALL_S,
                 price_check_every: int | None = PRICE_CHECK_EVERY_BLOCKS,
                 on_error: Callable[[int, BaseException], None] | None = None,
                 max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.reader = reader
        self.pool = reader.pool
        self.store = store
        self._on_snapshot = on_snapshot
        self._tracked = tracked
        self._held = held
        self._mg = metagraph_for
        self._heads_src = heads
        self.full_every = full_every
        self.lite_window = lite_window
        self.archive_stride = archive_stride
        self.queue_max = queue_max
        self.fallback_after_s = fallback_after_s
        self.poll_s = poll_s
        self.stall_s = stall_s
        self.price_check_every = price_check_every
        self._clock = clock
        self._sleep = sleep
        self._h = _Heads()
        self._head_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._prev: ChainSnapshot | None = None
        self._last_delivered: int | None = None
        self._last_full: int | None = None
        self._on_error = on_error
        self.max_consecutive_failures = max_consecutive_failures
        self.skipped_blocks = 0
        self.failed_blocks: list[int] = []
        self.errors: list[str] = []

    # ------------------------------------------------------------------ health
    def health(self, gap: int = 0) -> HealthObs:
        now = self._clock()
        since = 0 if self._h.last_finalized_at is None else int(now - self._h.last_finalized_at)
        lag = max(0, self._h.best - self._h.finalized) if self._h.best >= 0 and self._h.finalized >= 0 else 0
        return HealthObs(finality_lag_blocks=lag, secs_since_block=since, healthy_endpoints=self.pool.healthy(),
                         head_lag_blocks=0, feed_gap_blocks=gap)

    @property
    def stalled(self) -> bool:
        t = self._h.last_finalized_at
        return t is not None and self._clock() - t >= self.stall_s

    def _note_head(self, number: int, block_hash: str, best: int | None) -> None:
        if number > self._h.finalized:
            self._h.finalized, self._h.finalized_hash = number, block_hash
            self._h.last_finalized_at = self._clock()
            self._head_event.set()
        if best is not None and best > self._h.best:
            self._h.best = best
        if self._h.best < self._h.finalized:
            self._h.best = self._h.finalized

    # ------------------------------------------------------------------ head watchers
    async def _watch_injected(self) -> None:
        assert self._heads_src is not None
        async for number, h, best in self._heads_src():
            self._note_head(number, h, best)
            if self._stop.is_set():
                return

    async def _watch_ws(self) -> None:
        """Finalized-head subscription with HTTP polling fallback after `fallback_after_s` of silence or a drop."""
        while not self._stop.is_set():
            sub: Subscription | None = None
            try:
                _ep, sub = await self.pool.subscribe("chain_subscribeFinalizedHeads", [], "chain_unsubscribeFinalizedHeads",
                                                     Role.HEAD)
                self._h.via_ws = True
                while not self._stop.is_set():
                    hdr = await asyncio.wait_for(sub.__anext__(), self.fallback_after_s)
                    self._note_head(int(str(hdr["number"]), 16), header_hash(hdr), None)
            except (RpcError, TimeoutError, StopAsyncIteration, KeyError, ValueError) as e:
                self.errors.append(f"finalized subscription: {type(e).__name__}: {e}"[:300])
            finally:
                self._h.via_ws = False
                if sub is not None:
                    with contextlib.suppress(Exception):
                        await sub.aclose()
            await self._poll_until_ws()

    async def _poll_until_ws(self) -> None:
        """Poll chain_getFinalizedHead every `poll_s` for one fallback period, then let the caller retry the WS."""
        deadline = self._clock() + self.fallback_after_s
        while not self._stop.is_set() and self._clock() < deadline:
            try:
                h = await self.pool.call("chain_getFinalizedHead", [], Role.ARCHIVE)
                hdr = await self.pool.call("chain_getHeader", [h], Role.ARCHIVE)
                self._note_head(int(str(hdr["number"]), 16), str(h).lower(), None)
            except RpcError as e:
                self.errors.append(f"poll: {e}"[:300])
            await self._sleep(self.poll_s)

    async def _watch_best(self) -> None:
        while not self._stop.is_set():
            try:
                _ep, sub = await self.pool.subscribe("chain_subscribeNewHeads", [], "chain_unsubscribeNewHeads", Role.HEAD)
                async for hdr in sub:
                    self._note_head(-1, "", int(str(hdr["number"]), 16))
                    if self._stop.is_set():
                        break
            except RpcError as e:
                self.errors.append(f"best-head subscription: {e}"[:300])
            await self._sleep(self.fallback_after_s)

    # ------------------------------------------------------------------ stream
    async def stream(self, after: Block | None) -> AsyncGenerator[SourceItem, None]:
        """Items strictly after `after` (the last journaled block); None = start at the current finalized head."""
        queue: asyncio.Queue[SourceItem | BaseException] = asyncio.Queue(self.queue_max)
        if self._heads_src is not None:
            self._tasks.append(asyncio.create_task(self._watch_injected()))
        else:
            self._tasks.append(asyncio.create_task(self._watch_ws()))
            self._tasks.append(asyncio.create_task(self._watch_best()))
        self._tasks.append(asyncio.create_task(self._produce(queue, after)))
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self.aclose()

    async def _produce(self, queue: asyncio.Queue[SourceItem | BaseException], after: Block | None) -> None:
        try:
            await self._head_event.wait()
            nxt = (self._h.finalized if after is None else int(after) + 1)
            failures = 0
            while not self._stop.is_set():
                target = self._h.finalized
                if nxt > target:
                    self._head_event.clear()
                    await self._head_event.wait()
                    continue
                if queue.full():
                    # consumer is behind: skip this block without reading it; the next delivered item carries
                    # feed_gap_blocks > 0 (diffs span the gap, so events are coarsened, never hidden)
                    self.skipped_blocks += 1
                    nxt += 1
                    if nxt > self._h.finalized:
                        await self._sleep(min(self.poll_s, float(BLOCK_SECONDS)))
                    continue
                gap_to_head = target - nxt
                if gap_to_head > self.lite_window:
                    block = nxt + self.archive_stride - 1 if self._prev is not None else nxt
                    block = min(block, target - self.lite_window)
                    role = Role.ARCHIVE
                else:
                    block, role = nxt, Role.HEAD
                try:
                    await self._one(queue, block, role)
                    failures = 0
                except (RpcError, DecodeError) as e:
                    failures += 1
                    self.failed_blocks.append(block)
                    self.errors.append(f"block {block}: {type(e).__name__}: {e}"[:300])
                    if self._on_error is not None:
                        self._on_error(block, e)
                    if failures > self.max_consecutive_failures:
                        raise
                    if isinstance(e, RpcError):
                        await self._sleep(min(self.poll_s * failures, 60.0))
                        continue                        # transient: retry the same block
                    # undecodable at this hash: skip it (the collector keeps the raw bytes via the error)
                nxt = block + 1
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            await queue.put(e)

    def _plan_for(self, block: int) -> ReadPlan:
        if self._prev is None or block % self.full_every == 0:
            return ReadPlan.FULL
        if self._last_full is not None and block - self._last_full >= self.full_every:
            return ReadPlan.FULL                        # a skipped or failed block hid the scheduled FULL read
        return ReadPlan.HEAD

    async def _one(self, queue: asyncio.Queue[SourceItem | BaseException], block: int, role: Role) -> None:
        if block == self._h.finalized and self._h.finalized_hash:
            bh = BlockHash(self._h.finalized_hash)
        else:
            bh = BlockHash((await self.reader.block_hashes([block]))[block])
        prev = self._prev
        plan = self._plan_for(block)
        snap = await self.reader.snapshot(Block(block), bh, plan, prev, self._tracked(prev), held=self._held(),
                                          metagraph_for=self._mg(), role=role)
        self._prev = snap
        if snap.plan is ReadPlan.FULL:
            self._last_full = block
        if self.price_check_every and block % self.price_check_every == 0:
            await self._price_check(snap)
        gap = 0 if self._last_delivered is None else max(0, block - self._last_delivered - 1)
        res = self._on_snapshot(snap)
        if inspect.isawaitable(res):
            await res
        self._last_delivered = block
        await queue.put(SourceItem(snapshot=snap, health=self.health(gap)))

    async def _price_check(self, snap: ChainSnapshot) -> None:
        try:
            pp = await self.reader.price_parity(snap)
        except RpcError as e:
            self.errors.append(f"price parity: {e}"[:300])
            return
        if not pp.ok and self.reader.on_drift is not None:
            self.reader.on_drift(ModelDriftObserved(block=snap.block, probe="price_all", netuid=None,
                                                    err_ppm=min(pp.max_rel_ppm, PPM)))

    async def aclose(self) -> None:
        self._stop.set()
        self._head_event.set()
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGINT (and SIGBREAK on Windows) set `stop` thread-safely. Works with the default Proactor loop (no
    loop.add_signal_handler, which Windows does not support)."""
    def handler(_sig: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handler)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, handler)
