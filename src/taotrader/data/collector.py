"""taotrader/data/collector.py - resumable archive collector (WP4; DESIGN.md sections 6.6-6.11, 7.1, 7.3, 8.11, 11).

What it collects, into the WP3 lake (base series ""):
- schedules: `c60` = every 60 blocks from 8,486,594 (era C) and `h300` = every 300 blocks from 4,920,351 (dTAO
  launch). Every scheduled snapshot is a FULL read (backtests need every field).
- coarse-to-fine order: each schedule is collected level by level (600 -> 300 -> 60 blocks), so a partial run already
  gives an evenly thinned history that ParquetReplay can use.
- the hotkey panel: every snapshot carries the tracked (hotkey, generation) pairs. Tracked sets are selected
  POINT-IN-TIME on a fixed membership grid (the first 600-grid block >= panel_from, then every `membership_every`
  blocks, default daily): at each membership block m the collector lists AlphaDividendsPerSubnet with
  state_getKeysPaged at m's hash (never today's membership), reads every listed recipient's share pool in the
  snapshot at m, and selects T(m) = protocol.derive.track_hotkeys(snap_m, listing_m, prev_tracked=T(previous m)):
  top-N earners by TotalHotkeyAlpha + take-0 earners + owner hotkeys, STICKY until the generation ends. Every snapshot
  at b >= m (until the next membership block) reads T(m); the reader adds each subnet's owner hotkey on its own. T(m) is
  recomputed from the lake on resume (snapshot at m + its dividend_keys rows), so tracking never depends on run
  history.
- escrow E per subnet (>= 8,765,684): read on a fixed 360-block grid (StakeInfo route of the WP1 reader) and attached
  to every snapshot from its grid block floor(b/360)*360 (escrow_block column = the source block; past values only).
- calibration probes (table `calib`): local spot vs current_alpha_price_all, AMM (protocol.amm) vs sim_swap and the
  local prune target vs get_subnet_to_prune, on fixed block grids. A probe whose relative error exceeds
  `abort_rel_err` (beyond the 1-rao integer quantum) raises CalibrationDrift BEFORE anything of that chunk is written.

Resume (`fetch_ledger` in data/state.sqlite drives it): blocks are grouped into deterministic chunks (plan kind, level,
absolute bucket of `chunk_blocks`). A chunk is read completely, then written (raw_rpc of INVALID blocks, dividend_keys,
calib, the snapshot triple), then its blocks are marked OK / INVALID in fetch_ledger. A transient failure leaves the
chunk unwritten and its blocks FAILED (retried next run). Each snapshot is read with prev=None, so its content is a
function of (block, chain state, tracked set, escrow grid) only: a killed run resumed later writes exactly the chunks
an uninterrupted run writes (identical manifest; an identical rewrite of a committed chunk is a no-op in the lake).
Blocks found in the lake but not in the ledger (crash between the lake commit and the ledger commit) are reconciled
to OK at startup.

Run identity: `collector_meta` (a small key/value table this module creates in data/state.sqlite) pins the settings
that change snapshot content (panel_from, membership grid, top_n, escrow grid, decoder version); resuming with
different values raises ConfigMismatch (fail closed).

CLI (until WP12's `taotrader collect` wraps it):
  python -m taotrader.data.collector backfill --schedule c60 [--schedule h300] [--end N] [--lake data/lake] ...
  python -m taotrader.data.collector status | panel-gaps [--lake data/lake]
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import contextlib
import hashlib
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import zstandard

from ..chain import items as it
from ..chain.hashing import from_hex, prefix, split_key, to_hex
from ..chain.reader import ESCROW_FIRST_BLOCK, KEYS_PAGE, JsonRpcChainReader, SnapshotDecodeError
from ..chain.rpc import RpcError, RpcFatal
from ..core.errors import DecodeError
from ..core.protocols import SwapSim
from ..core.state import ChainSnapshot, Quality, ReadPlan, SubnetState
from ..core.units import BLOCKS_PER_DAY, RAO_PER_TAO, AlphaRao, Block, BlockHash, Hotkey, NetUid, Rao, SubnetKey
from ..protocol.amm import SwapError, quote_buy, quote_sell, spot_rao_exact
from ..protocol.derive import track_hotkeys
from ..protocol.prune import prune_target
from ..protocol.regimes import regime
from . import schema
from .lake import Lake, open_state_db
from .store import LakeSnapshotStore

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------------ constants
DTAO_LAUNCH_BLOCK: Final[int] = it.DTAO_LAUNCH_BLOCK            # 4,920,351
ERA_C_FIRST_BLOCK: Final[int] = it.BALANCER_FIRST_BLOCK         # 8,486,594
PANEL_FROM_BLOCK: Final[int] = int(regime("price_ema_rp").first_block)   # 8,466,531 (post-June panel, section 8.11)
DEREG_ERA_FIRST_BLOCK: Final[int] = 6_573_966                   # NetworkRegistrationStartBlock (brief 4.4): prunes exist
ESCROW_GRID_BLOCKS: Final[int] = 360                            # one escrow read per 360 blocks (section 6.7 ESCROW)
LEVELS_C60: Final[tuple[int, ...]] = (600, 300, 60)
LEVELS_H300: Final[tuple[int, ...]] = (600, 300)
COLLECTOR_DECODER_VERSION: Final[int] = 1
MAX_NETUID_SCAN: Final[int] = it.DEFAULT_MAX_NETUID + it.NETUID_EXTENSION
TINY_POOL_RAO: Final[int] = 10 * RAO_PER_TAO                    # AMM probes use pools of >= 10 TAO
MEMBERSHIP: Final[str] = "membership"
DEFAULT_CHUNK_BLOCKS: Final[int] = 18_000
DEFAULT_PRICE_EVERY: Final[int] = 3_000      # 50 x 60 blocks
DEFAULT_AMM_EVERY: Final[int] = 12_000       # 200 x 60 blocks
DEFAULT_PRUNE_EVERY: Final[int] = 3_000

STATUS_OK: Final[str] = "OK"
STATUS_INVALID: Final[str] = "INVALID"
STATUS_FAILED: Final[str] = "FAILED"

PROBE_PRICE: Final[str] = "price"
PROBE_SIM_BUY: Final[str] = "sim_swap_buy"
PROBE_SIM_SELL: Final[str] = "sim_swap_sell"
PROBE_PRUNE: Final[str] = "prune_target"

_META_DDL: Final[str] = "CREATE TABLE IF NOT EXISTS collector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class CollectorError(Exception):
    """The collector cannot continue (bad configuration, inconsistent state)."""


class ConfigMismatch(CollectorError):
    """The lake was collected with different content-relevant settings (collector_meta)."""


class ChunkFailed(CollectorError):
    """A chunk could not be read completely (transient RPC failure); nothing of it was written."""


class CalibrationDrift(CollectorError):
    """A calibration probe exceeded the abort threshold; nothing of the chunk was written."""

    def __init__(self, msg: str, rows: Sequence[CalibRow]) -> None:
        super().__init__(msg)
        self.rows = tuple(rows)


# ------------------------------------------------------------------------------------------------ schedules
@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """Blocks start..end (inclusive) on the grid of the finest level, collected coarse-to-fine by `levels`."""
    name: str
    start: int
    end: int
    levels: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.levels or any(s <= 0 for s in self.levels):
            raise ValueError("levels must be positive")
        for a, b in zip(self.levels, self.levels[1:], strict=False):
            if not (b < a and a % b == 0):
                raise ValueError(f"levels must be strictly decreasing divisors: {self.levels}")
        if self.start > self.end or self.start < 0:
            raise ValueError(f"bad schedule range {self.start}..{self.end}")

    @property
    def step(self) -> int:
        return self.levels[-1]

    def level_blocks(self, i: int) -> list[int]:
        """Blocks of level i: on the level-i grid and not on any coarser grid."""
        step = self.levels[i]
        coarser = self.levels[i - 1] if i > 0 else None
        first = -(-self.start // step) * step
        return [b for b in range(first, self.end + 1, step) if coarser is None or b % coarser != 0]

    def blocks(self) -> list[int]:
        return sorted(b for i in range(len(self.levels)) for b in self.level_blocks(i))


def schedule_c60(end: int, *, start: int = ERA_C_FIRST_BLOCK) -> ScheduleSpec:
    """Every 60 blocks from era C (8,486,594), levels 600 -> 300 -> 60."""
    return ScheduleSpec("c60", start, end, LEVELS_C60)


def schedule_h300(end: int, *, start: int = DTAO_LAUNCH_BLOCK) -> ScheduleSpec:
    """Every 300 blocks from the dTAO launch (4,920,351), levels 600 -> 300."""
    return ScheduleSpec("h300", start, end, LEVELS_H300)


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    kind: str                  # "membership" or the schedule name
    level: int                 # level index (-1 for membership)
    step: int                  # cadence of the plan's grid (stall flag uses it)
    bucket: int                # absolute bucket: block // chunk_blocks
    blocks: tuple[int, ...]

    @property
    def label(self) -> str:
        return f"{self.kind}/L{self.level}/b{self.bucket}"


def membership_points(panel_from: int, end: int, every: int, level0: int) -> list[int]:
    """The membership grid: the first `level0`-grid block >= panel_from, then every multiple of `every` above it."""
    if every <= 0 or level0 <= 0 or every % level0 != 0:
        raise ValueError("membership_every must be a positive multiple of the coarsest level")
    first = -(-panel_from // level0) * level0
    if first > end:
        return []
    out = [first]
    m = (first // every + 1) * every
    while m <= end:
        out.append(m)
        m += every
    return out


# ------------------------------------------------------------------------------------------------ configuration
@dataclass(frozen=True, slots=True)
class CollectorCfg:
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS   # absolute bucket width of one chunk (multiple of every level)
    panel_from: int = PANEL_FROM_BLOCK         # membership grid start (4,920,351 = optional pre-June panel)
    membership_every: int = BLOCKS_PER_DAY     # daily point-in-time membership (section 6.7 MEMBERSHIP)
    level0: int = 600                          # coarsest grid (the membership grid starts on it)
    top_n: int = 5                             # preregistration [router].top_n_tracked
    snapshot_concurrency: int = 3              # snapshots in flight (the reader's bucket bounds calls at 3 req/s)
    escrow_grid: int = ESCROW_GRID_BLOCKS
    price_every_blocks: int = DEFAULT_PRICE_EVERY   # price parity every 50th 60-block snapshot (section 11 WP4)
    amm_every_blocks: int = DEFAULT_AMM_EVERY       # AMM vs sim_swap every 200th
    prune_every_blocks: int = DEFAULT_PRUNE_EVERY   # local ladder target vs get_subnet_to_prune
    amm_probe_tao_rao: int = RAO_PER_TAO       # 1 TAO buy, then a sell of the bought alpha
    amm_probe_subnets: int = 3
    abort_rel_err: Decimal = Decimal("1e-4")   # section 6.8: the collector aborts on > 1e-4
    prune_abort_from: int = DEREG_ERA_FIRST_BLOCK
    max_failed_chunks: int = 3
    decoder_version: int = COLLECTOR_DECODER_VERSION

    def content_meta(self) -> dict[str, str]:
        """Settings that change snapshot content; pinned in collector_meta."""
        return {"panel_from": str(self.panel_from), "membership_every": str(self.membership_every),
                "level0": str(self.level0), "top_n": str(self.top_n), "escrow_grid": str(self.escrow_grid),
                "decoder_version": str(self.decoder_version)}


@dataclass(frozen=True, slots=True)
class CalibRow:
    block: int
    probe: str
    netuid: int | None
    model: float
    chain: float
    rel_err: float

    def row(self) -> dict[str, Any]:
        return {"block": self.block, "probe": self.probe, "netuid": self.netuid, "model": self.model, "chain": self.chain,
                "rel_err": self.rel_err}


@dataclass(slots=True)
class RunSummary:
    chunks_committed: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    snapshots: int = 0
    invalid: int = 0
    membership_points: int = 0
    calib_rows: int = 0
    calib_max: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    failed_blocks: list[int] = field(default_factory=list)

    def note_calib(self, rows: Iterable[CalibRow]) -> None:
        for r in rows:
            self.calib_rows += 1
            self.calib_max[r.probe] = max(self.calib_max.get(r.probe, 0.0), r.rel_err)


# ------------------------------------------------------------------------------------------------ the source seam
class ArchiveSource(Protocol):
    """What the collector and data.refine need from the archive. ReaderSource adapts the WP1 JsonRpcChainReader;
    tests supply in-memory fakes."""

    async def block_hash(self, block: int) -> BlockHash: ...
    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]: ...
    async def prime_versions(self, blocks: Sequence[int], hashes: Mapping[int, BlockHash]) -> None: ...
    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]: ...
    async def snapshot(self, block: int, block_hash: BlockHash, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot: ...
    async def generations(self, block_hash: BlockHash) -> dict[int, int]: ...
    async def dividend_keys_all(self, block_hash: BlockHash) -> dict[int, tuple[Hotkey, ...]]: ...
    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]: ...
    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]: ...
    async def current_price(self, netuid: NetUid, block_hash: BlockHash) -> int: ...
    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None: ...
    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]: ...
    def providers(self) -> dict[str, int]: ...


class ReaderSource:
    """ArchiveSource over the WP1 JsonRpcChainReader (construct the reader with escrow_every=None and
    prune_check_every=None: the collector reads escrow and the prune target on its own grids)."""

    def __init__(self, reader: JsonRpcChainReader) -> None:
        self.reader = reader

    async def block_hash(self, block: int) -> BlockHash:
        return await self.reader.block_hash(Block(block))

    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]:
        return await self.reader.block_hashes(sorted(set(blocks)))

    async def prime_versions(self, blocks: Sequence[int], hashes: Mapping[int, BlockHash]) -> None:
        """Runtime versions by monotone bracketing (spec versions never decrease): two equal known points pin every
        block between them, so a chunk costs 2 calls plus ~2*log2(n) per spec change."""
        bs = sorted(set(blocks))
        if not bs:
            return
        specs = self.reader.specs

        async def ver(b: int) -> tuple[int, int]:
            v = specs.lookup(b)
            if v is None:
                v = await self.reader.spec_version(hashes[b])
                specs.add(b, *v)
            return v

        async def split(lo: int, hi: int) -> None:
            if hi - lo <= 1 or await ver(bs[lo]) == await ver(bs[hi]):
                return
            mid = (lo + hi) // 2
            await ver(bs[mid])
            await split(lo, mid)
            await split(mid, hi)

        await ver(bs[0])
        await ver(bs[-1])
        await split(0, len(bs) - 1)

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        return await self.reader.spec_version(block_hash)

    async def snapshot(self, block: int, block_hash: BlockHash, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot:
        return await self.reader.snapshot(Block(block), block_hash, ReadPlan.FULL, prev, tracked)

    async def generations(self, block_hash: BlockHash) -> dict[int, int]:
        """netuid -> NetworkRegisteredAt for every non-root netuid with NetworksAdded true (one storage call)."""
        added, reg = it.SUBNET[it.NETWORKS_ADDED], it.SUBNET["reg_at"]
        keys = [r.key(netuid=n) for n in range(1, MAX_NETUID_SCAN + 1) for r in (added, reg)]
        raw = await self.reader.query(keys, block_hash)
        out: dict[int, int] = {}
        for n in range(1, MAX_NETUID_SCAN + 1):
            a = raw.get(added.key(netuid=n))
            if a is not None and bool(added.decode(a)):
                r = raw.get(reg.key(netuid=n))
                out[n] = 0 if r is None else int(reg.decode(r))
        return out

    async def dividend_keys_all(self, block_hash: BlockHash) -> dict[int, tuple[Hotkey, ...]]:
        """Every key of AlphaDividendsPerSubnet at this hash, one prefix walk (pages of 1,000 keys; a few calls instead
        of one listing per subnet). netuid -> sorted hotkeys."""
        row = it.HOTKEY["last_dividend"]
        pre = to_hex(prefix(row.pallet, row.item))
        out: dict[int, set[str]] = {}
        start: str | None = None
        while True:
            page = await self.reader.pool.call("state_getKeysPaged", [pre, KEYS_PAGE, start, block_hash], self.reader.role)
            if not isinstance(page, list):
                raise DecodeError(f"state_getKeysPaged returned {type(page).__name__}")
            for k in page:
                try:
                    parts = split_key(from_hex(str(k)), row.pallet, row.item, row.hashers, (2, 32))
                except ValueError as e:
                    raise DecodeError(f"dividend key {str(k)[:80]}: {e}") from e
                out.setdefault(int.from_bytes(parts[0], "little"), set()).add(to_hex(parts[1]))
            if len(page) < KEYS_PAGE:
                break
            start = str(page[-1])
        return {n: tuple(Hotkey(h) for h in sorted(hs)) for n, hs in sorted(out.items())}

    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]:
        return await self.reader.escrow_by_subnet(block_hash)

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        return await self.reader.prices_all(block_hash)

    async def current_price(self, netuid: NetUid, block_hash: BlockHash) -> int:
        return await self.reader.current_price(netuid, block_hash)

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        return await self.reader.sim_swap_buy(netuid, tao_rao, block_hash)

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        return await self.reader.sim_swap_sell(netuid, alpha_rao, block_hash)

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        return await self.reader.subnet_to_prune(block_hash)

    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]:
        return await self.reader.query(keys, block_hash)

    def providers(self) -> dict[str, int]:
        return {label: st.calls for label, st in self.reader.pool.stats().items()}


# ------------------------------------------------------------------------------------------------ panel tracking
Pair = tuple[SubnetKey, Hotkey]


class PanelTracker:
    """Point-in-time, sticky tracked sets on the membership grid (see the module docstring)."""

    def __init__(self, panel_from: int, membership_every: int, level0: int, top_n: int) -> None:
        self.panel_from = panel_from
        self.membership_every = membership_every
        self.level0 = level0
        self.top_n = top_n
        self._blocks: list[int] = []
        self._sets: list[tuple[Pair, ...]] = []

    def points(self, end: int) -> list[int]:
        return membership_points(self.panel_from, end, self.membership_every, self.level0)

    @property
    def recorded(self) -> tuple[int, ...]:
        return tuple(self._blocks)

    def tracked_at(self, block: int) -> tuple[Pair, ...]:
        """T(m) of the last recorded membership block m <= block (() before the first)."""
        i = bisect.bisect_right(self._blocks, block)
        return self._sets[i - 1] if i > 0 else ()

    def tracked_before(self, block: int) -> tuple[Pair, ...]:
        i = bisect.bisect_left(self._blocks, block)
        return self._sets[i - 1] if i > 0 else ()

    def record(self, block: int, snap: ChainSnapshot, listing: Mapping[int, Sequence[str]]) -> tuple[Pair, ...]:
        """T(block) from the membership snapshot and its point-in-time dividend listing (blocks ascending)."""
        if self._blocks and block <= self._blocks[-1]:
            raise CollectorError(f"membership block {block} recorded out of order (last {self._blocks[-1]})")
        prev = self.tracked_before(block)
        sel = track_hotkeys(snap, {int(n): tuple(v) for n, v in listing.items()}, (), prev, self.top_n)
        out = tuple((k, Hotkey(h)) for k, h in sel)
        self._blocks.append(block)
        self._sets.append(out)
        return out


def listed_pairs(gens: Mapping[int, int], listing: Mapping[int, Sequence[Hotkey]]) -> list[Pair]:
    return [(SubnetKey(NetUid(n), Block(gens[n])), Hotkey(h)) for n in sorted(listing) if n in gens for h in listing[n]]


# ------------------------------------------------------------------------------------------------ escrow
class EscrowCache:
    """Escrow alpha per netuid on the fixed escrow grid; None where it is unavailable (before 8,765,684, or a
    runtime/decoder failure, which is logged)."""

    def __init__(self, source: ArchiveSource, grid: int = ESCROW_GRID_BLOCKS) -> None:
        self.source = source
        self.grid = grid
        self._cache: dict[int, dict[int, AlphaRao] | None] = {}

    def source_block(self, block: int) -> int | None:
        if block < ESCROW_FIRST_BLOCK:
            return None
        return max(block // self.grid * self.grid, ESCROW_FIRST_BLOCK)

    async def fill(self, blocks: Iterable[int]) -> None:
        need = sorted({src for b in blocks if (src := self.source_block(b)) is not None and src not in self._cache})
        if not need:
            return
        hashes = await self.source.block_hashes(need)
        for src in need:
            try:
                self._cache[src] = await self.source.escrow_by_subnet(hashes[src])
            except (RpcFatal, DecodeError) as e:
                log.warning("escrow at %d unavailable (%s); escrow_alpha stays unknown there", src, e)
                self._cache[src] = None

    def at(self, block: int) -> tuple[int | None, dict[int, AlphaRao] | None]:
        src = self.source_block(block)
        if src is None:
            return None, None
        return src, self._cache.get(src)


# ------------------------------------------------------------------------------------------------ snapshot finishing
def stall_between(lo_exclusive: int, block: int) -> bool:
    """The 2025-05-20 freeze (5,611,658 -> next block) lies in (lo, block]."""
    freeze = it.CHAIN_STALL_BLOCKS[0]
    return lo_exclusive <= freeze < block


def finalize_snapshot(snap: ChainSnapshot, *, escrow: Mapping[int, int] | None = None, stall: bool = False,
                      refined: bool = False) -> ChainSnapshot:
    """Attach grid escrow, OR the stall / REFINED flags into every subnet, and reseal the digest."""
    subnets: list[SubnetState] = []
    for s in snap.subnets:
        q = s.quality
        if stall:
            q |= Quality.CHAIN_STALL_GAP
        if refined:
            q |= Quality.REFINED
        esc = s.escrow_alpha if escrow is None else AlphaRao(int(escrow.get(int(s.key.netuid), 0)))
        subnets.append(replace(s, quality=q, escrow_alpha=esc))
    return schema.with_digest(replace(snap, subnets=tuple(subnets), digest=""))


def raw_rpc_row(err: SnapshotDecodeError) -> dict[str, Any]:
    """Layer-0 record of an INVALID block: the raw key/value hex of the failed read, zstd JSON."""
    payload = json.dumps({"error": str(err), "block_hash": err.block_hash, "raw": err.raw}, sort_keys=True,
                         separators=(",", ":")).encode()
    sha = hashlib.sha256(json.dumps(sorted(err.raw), separators=(",", ":")).encode()).hexdigest()
    return {"block": int(err.block), "call": "snapshot", "request_sha": sha,
            "response_zstd": zstandard.ZstdCompressor(level=10).compress(payload)}


def write_block_rows(lake: Lake, table: str, rows: Sequence[Mapping[str, Any]], *, series: str = "") -> None:
    """Write block-keyed rows as one chunk per era (chunks never span eras)."""
    by_era: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_era.setdefault(schema.era_of(int(r["block"])), []).append(r)
    for era in sorted(by_era):
        lake.write_rows(table, by_era[era], series=series)


# ------------------------------------------------------------------------------------------------ calibration probes
def _rel_excess(model: int, chain: int) -> float:
    """|model - chain| beyond the 1-rao integer quantum, relative to the chain value."""
    if chain <= 0:
        return 0.0 if model == chain else 1.0
    return max(abs(model - chain) - 1, 0) / chain


def probe_subnets(snap: ChainSnapshot, k: int) -> list[SubnetState]:
    """Deterministic AMM probe set: tradable pools (>= 10 TAO, not T/A-priced) by (tao, netuid): smallest, median,
    largest."""
    ok = sorted((s for s in snap.subnets if s.subtoken_enabled and int(s.pool.tao) >= TINY_POOL_RAO
                 and not s.quality & Quality.TA_PRICE), key=lambda s: (int(s.pool.tao), int(s.key.netuid)))
    if not ok:
        return []
    picks = [ok[0], ok[len(ok) // 2], ok[-1]][:max(k, 0)]
    seen: set[int] = set()
    out: list[SubnetState] = []
    for s in picks:
        if int(s.key.netuid) not in seen:
            seen.add(int(s.key.netuid))
            out.append(s)
    return out


async def price_probe(source: ArchiveSource, snap: ChainSnapshot, k: int = 3) -> list[CalibRow]:
    """Local spot (floor rao) vs current_alpha_price_all (spec >= 391); before it, current_alpha_price for the AMM
    probe set; nothing where neither runtime API exists. T/A-priced pools are skipped (their runtime price differs by
    construction)."""
    try:
        chain = await source.prices_all(snap.block_hash)
        subs = [s for s in snap.subnets if int(s.key.netuid) in chain]
    except RpcFatal:
        subs = probe_subnets(snap, k)
        chain = {}
        try:
            for s in subs:
                chain[int(s.key.netuid)] = await source.current_price(s.key.netuid, snap.block_hash)
        except RpcFatal:
            return []
    out: list[CalibRow] = []
    for s in subs:
        if s.quality & Quality.TA_PRICE:
            continue
        local, ch = int(s.pool.spot_rao()), int(chain[int(s.key.netuid)])
        out.append(CalibRow(int(snap.block), PROBE_PRICE, int(s.key.netuid), float(spot_rao_exact(s.pool)), float(ch),
                            _rel_excess(local, ch)))
    return out


async def amm_probe(source: ArchiveSource, snap: ChainSnapshot, tao_rao: int, k: int = 3) -> list[CalibRow]:
    """protocol.amm.quote_buy / quote_sell vs sim_swap at the snapshot hash (from 6,262,253; nothing before)."""
    out: list[CalibRow] = []
    for s in probe_subnets(snap, k):
        try:
            qb = quote_buy(s.pool, Rao(tao_rao))
            qs = quote_sell(s.pool, AlphaRao(qb.amount_out))
        except SwapError:
            continue
        try:
            sb = await source.sim_swap_buy(s.key.netuid, tao_rao, snap.block_hash)
            ss = await source.sim_swap_sell(s.key.netuid, qb.amount_out, snap.block_hash)
        except RpcFatal:
            return out
        n = int(s.key.netuid)
        out.append(CalibRow(int(snap.block), PROBE_SIM_BUY, n, float(qb.amount_out), float(sb.alpha_amount),
                            _rel_excess(qb.amount_out, sb.alpha_amount)))
        out.append(CalibRow(int(snap.block), PROBE_SIM_SELL, n, float(qs.amount_out), float(ss.tao_amount),
                            _rel_excess(qs.amount_out, ss.tao_amount)))
    return out


async def prune_probe(source: ArchiveSource, snap: ChainSnapshot) -> list[CalibRow]:
    """Local ladder target (protocol.prune.prune_target) vs SubnetInfoRuntimeApi_get_subnet_to_prune."""
    try:
        chain = await source.subnet_to_prune(snap.block_hash)
    except RpcFatal:
        return []
    local = prune_target(snap)
    ln = 0 if local is None else int(local.netuid)
    cn = 0 if chain is None else int(chain)
    return [CalibRow(int(snap.block), PROBE_PRUNE, cn or None, float(ln), float(cn), 0.0 if ln == cn else 1.0)]


# ------------------------------------------------------------------------------------------------ fetch ledger
@dataclass(frozen=True, slots=True)
class LedgerRow:
    block: int
    status: str
    attempts: int
    provider: str
    last_error: str


class FetchLedger:
    """data/state.sqlite fetch_ledger (section 7.3) plus collector_meta."""

    def __init__(self, state_db: str | Path) -> None:
        self.db: sqlite3.Connection = open_state_db(state_db)
        self.db.execute(_META_DDL)

    def close(self) -> None:
        self.db.close()

    def load(self) -> dict[int, LedgerRow]:
        rows = self.db.execute("SELECT block, status, attempts, provider, last_error FROM fetch_ledger").fetchall()
        return {int(r[0]): LedgerRow(int(r[0]), str(r[1]), int(r[2] or 0), str(r[3] or ""), str(r[4] or "")) for r in rows}

    def mark(self, rows: Sequence[tuple[int, str, str, str]]) -> None:
        """(block, status, provider, error) rows in one transaction; attempts counts every mark of a block."""
        if not rows:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany(
                "INSERT INTO fetch_ledger (block, status, attempts, provider, last_error) VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(block) DO UPDATE SET status = excluded.status, attempts = fetch_ledger.attempts + 1, "
                "provider = excluded.provider, last_error = excluded.last_error",
                [(b, st, prov, err[:500]) for b, st, prov, err in rows])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def meta(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.db.execute("SELECT key, value FROM collector_meta").fetchall()}

    def pin_meta(self, want: Mapping[str, str]) -> None:
        have = self.meta()
        diff = sorted(k for k in want if k in have and have[k] != want[k])
        if diff:
            raise ConfigMismatch("collector settings differ from the lake's collector_meta: "
                                 + ", ".join(f"{k} {have[k]} -> {want[k]}" for k in diff))
        missing = [(k, v) for k, v in sorted(want.items()) if k not in have]
        if missing:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.executemany("INSERT INTO collector_meta (key, value) VALUES (?, ?)", missing)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise


# ------------------------------------------------------------------------------------------------ the collector
@dataclass(slots=True)
class _ChunkRead:
    snaps: dict[int, ChainSnapshot] = field(default_factory=dict)
    invalid: dict[int, SnapshotDecodeError] = field(default_factory=dict)
    listings: dict[int, tuple[dict[int, int], dict[int, tuple[Hotkey, ...]]]] = field(default_factory=dict)
    pending: dict[int, tuple[Pair, ...]] = field(default_factory=dict)     # T(m) of this chunk's membership blocks


class Collector:
    """Resumable archive collector over an ArchiveSource and a WP3 Lake (see the module docstring)."""

    def __init__(self, source: ArchiveSource, lake: Lake, cfg: CollectorCfg | None = None, *,
                 on_chunk: Callable[[ChunkPlan, str], None] | None = None) -> None:
        self.source = source
        self.lake = lake
        self.cfg = cfg or CollectorCfg()
        if self.cfg.chunk_blocks <= 0 or self.cfg.chunk_blocks % self.cfg.level0 != 0:
            raise ValueError("chunk_blocks must be a positive multiple of level0")
        self.ledger = FetchLedger(lake.state_db)
        self.tracker = PanelTracker(self.cfg.panel_from, self.cfg.membership_every, self.cfg.level0, self.cfg.top_n)
        self.escrow = EscrowCache(source, self.cfg.escrow_grid)
        self.on_chunk = on_chunk
        self._state: dict[int, LedgerRow] = {}

    def close(self) -> None:
        self.ledger.close()

    # ------------------------------------------------------------------ planning
    def plan(self, schedules: Sequence[ScheduleSpec]) -> list[ChunkPlan]:
        """Membership chunks first (ascending; the sticky chain needs them in order), then each schedule
        coarse-to-fine, each level by ascending absolute bucket."""
        if not schedules:
            return []
        for sch in schedules:
            if sch.levels[0] % self.cfg.level0 != 0 and self.cfg.level0 % sch.levels[0] != 0:
                raise ValueError(f"schedule {sch.name}: coarsest level {sch.levels[0]} incompatible with level0")
        end = max(s.end for s in schedules)
        mem = self.tracker.points(end)
        mem_set = set(mem)
        plans = self._bucketed(MEMBERSHIP, -1, self.cfg.membership_every, mem)
        for sch in schedules:
            for i in range(len(sch.levels)):
                blocks = [b for b in sch.level_blocks(i) if b not in mem_set]
                plans += self._bucketed(sch.name, i, sch.step, blocks)
        return plans

    def _bucketed(self, kind: str, level: int, step: int, blocks: Sequence[int]) -> list[ChunkPlan]:
        groups: dict[int, list[int]] = {}
        for b in blocks:
            groups.setdefault(b // self.cfg.chunk_blocks, []).append(b)
        return [ChunkPlan(kind, level, step, k, tuple(sorted(v))) for k, v in sorted(groups.items())]

    # ------------------------------------------------------------------ resume state
    def _reconcile(self) -> None:
        """Blocks stored in the lake's base series but missing from the ledger (a crash between the lake commit and the
        ledger commit) are marked OK."""
        self.lake.refresh()
        stored = {r.block for r in self.lake.snapshot_refs() if r.series == ""}
        fix = [(b, STATUS_OK, "lake", "") for b in sorted(stored)
               if b not in self._state or self._state[b].status != STATUS_OK]
        if fix:
            self.ledger.mark(fix)
            self._state = self.ledger.load()

    def _rebuild_tracker(self, end: int) -> None:
        """Recompute T(m) for every committed membership block, ascending, from the lake (snapshot at m + its
        dividend_keys rows). Stops at the first membership block not yet collected."""
        self.tracker = PanelTracker(self.cfg.panel_from, self.cfg.membership_every, self.cfg.level0, self.cfg.top_n)
        pts = self.tracker.points(end)
        if not pts:
            return
        store = LakeSnapshotStore(self.lake, clock=max(pts))
        listings = load_listings(self.lake, pts)
        for m in pts:
            st = self._state.get(m)
            if st is None or st.status == STATUS_FAILED:
                break
            if st.status != STATUS_OK:
                continue                                 # INVALID: T carries over unchanged
            snap = store.at(Block(m))
            self.tracker.record(m, snap, listings.get(m, {}))

    def _needs(self, b: int, retry_invalid: bool) -> bool:
        st = self._state.get(b)
        if st is None or st.status == STATUS_FAILED:
            return True
        return st.status == STATUS_INVALID and retry_invalid

    # ------------------------------------------------------------------ run
    async def run(self, schedules: Sequence[ScheduleSpec], *, retry_invalid: bool = False,
                  max_chunks: int | None = None) -> RunSummary:
        """Collect every planned chunk that is not done yet. A failed membership chunk stops the run (the sticky chain
        must stay in order); other failures are retried next run, up to cfg.max_failed_chunks per run."""
        self.ledger.pin_meta(self.cfg.content_meta())
        self._state = self.ledger.load()
        self._reconcile()
        end = max((s.end for s in schedules), default=0)
        self._rebuild_tracker(end)
        summary = RunSummary()
        before = self.source.providers()
        done = 0
        for plan in self.plan(schedules):
            if max_chunks is not None and done >= max_chunks:
                break
            todo = [b for b in plan.blocks if self._needs(b, retry_invalid)]
            if plan.kind == MEMBERSHIP:
                todo = [b for b in todo if b > max(self.tracker.recorded, default=-1)]
            if not todo:
                summary.chunks_skipped += 1
                continue
            try:
                await self._chunk(plan, todo, summary)
                done += 1
            except ChunkFailed as e:
                summary.chunks_failed += 1
                log.warning("chunk %s failed: %s", plan.label, e)
                if self.on_chunk is not None:
                    self.on_chunk(plan, f"failed: {e}")
                if plan.kind == MEMBERSHIP or summary.chunks_failed >= self.cfg.max_failed_chunks:
                    break
        after = self.source.providers()
        summary.calls = {k: v - before.get(k, 0) for k, v in sorted(after.items()) if v - before.get(k, 0)}
        return summary

    async def _chunk(self, plan: ChunkPlan, todo: Sequence[int], summary: RunSummary) -> None:
        before = self.source.providers()
        try:
            read = await self._read_chunk(plan, todo)
            await self.escrow.fill(read.snaps)
            calib = await self._probes(read.snaps)
        except (RpcError, DecodeError) as e:
            after = self.source.providers()
            prov = ",".join(k for k in sorted(after) if after[k] != before.get(k, 0))
            self.ledger.mark([(b, STATUS_FAILED, prov, f"{type(e).__name__}: {e}") for b in todo])
            self._state = self.ledger.load()
            summary.failed_blocks += list(todo)
            raise ChunkFailed(f"{plan.label}: {type(e).__name__}: {e}") from e
        drift = [r for r in calib if Decimal(repr(r.rel_err)) > self.cfg.abort_rel_err
                 and (r.probe != PROBE_PRUNE or r.block >= self.cfg.prune_abort_from)]
        if drift:
            raise CalibrationDrift(f"{plan.label}: {len(drift)} probe(s) over {self.cfg.abort_rel_err}: "
                                   + "; ".join(f"{r.probe}@{r.block} n{r.netuid} err={r.rel_err:.3g}" for r in drift[:5]),
                                   calib)
        finals: list[ChainSnapshot] = []
        esc_blocks: dict[tuple[int, int], int] = {}
        for b in sorted(read.snaps):
            snap = read.snaps[b]
            src, esc = self.escrow.at(b)
            if src is not None and esc is not None:
                for s in snap.subnets:
                    esc_blocks[(b, int(s.key.netuid))] = src
            finals.append(finalize_snapshot(snap, escrow=esc, stall=stall_between(b - plan.step, b)))
        div_rows = [{"block": m, "netuid": n, "reg_at": gens[n], "hotkey": str(h)}
                    for m, (gens, listing) in sorted(read.listings.items()) for n in sorted(listing) for h in listing[n]]
        # ---- writes (each one atomic; an identical rewrite after a crash is a no-op)
        if read.invalid:
            write_block_rows(self.lake, "raw_rpc", [raw_rpc_row(read.invalid[b]) for b in sorted(read.invalid)])
        if div_rows:
            write_block_rows(self.lake, "dividend_keys", div_rows)
        if calib:
            write_block_rows(self.lake, "calib", [r.row() for r in calib])
        if finals:
            self.lake.write_snapshots(finals, series="", decoder_version=self.cfg.decoder_version, escrow_block=esc_blocks)
        after = self.source.providers()
        prov = ",".join(k for k in sorted(after) if after[k] != before.get(k, 0))
        marks = [(int(s.block), STATUS_OK, prov, "") for s in finals]
        marks += [(b, STATUS_INVALID, prov, str(e)) for b, e in sorted(read.invalid.items())]
        self.ledger.mark(marks)
        for b, row in self.ledger.load().items():
            self._state[b] = row
        # ---- membership: T(m) from the committed snapshot (same bytes the lake replays on resume)
        if plan.kind == MEMBERSHIP:
            by_block = {int(s.block): s for s in finals}
            for m in sorted(read.listings):
                if m in by_block:
                    self.tracker.record(m, by_block[m], read.listings[m][1])
                    summary.membership_points += 1
        summary.chunks_committed += 1
        summary.snapshots += len(finals)
        summary.invalid += len(read.invalid)
        summary.note_calib(calib)
        if self.on_chunk is not None:
            self.on_chunk(plan, f"ok: {len(finals)} snapshots, {len(read.invalid)} invalid, {len(calib)} calib rows")

    async def _read_chunk(self, plan: ChunkPlan, todo: Sequence[int]) -> _ChunkRead:
        out = _ChunkRead()
        extra = sorted({b - plan.step for b in todo if abs(b - ERA_C_FIRST_BLOCK) <= it.SEED_WINDOW_BLOCKS})
        hashes = await self.source.block_hashes(sorted(set(todo) | set(extra)))
        await self.source.prime_versions(sorted(hashes), hashes)
        if plan.kind == MEMBERSHIP:
            for b in todo:                                # sequential: T(m) depends on T(previous m)
                gens = await self.source.generations(hashes[b])
                listing_all = await self.source.dividend_keys_all(hashes[b])
                listing = {n: hks for n, hks in listing_all.items() if n in gens}
                prev_t = self._prev_tracked(out, b)
                tracked = sorted(set(prev_t) | set(listed_pairs(gens, listing)))
                try:
                    snap = await self.source.snapshot(b, hashes[b], None, tracked)
                except SnapshotDecodeError as e:
                    out.invalid[b] = e
                    continue
                out.snaps[b] = snap
                out.listings[b] = (gens, listing)
                sel = track_hotkeys(snap, {int(n): tuple(v) for n, v in listing.items()}, (), prev_t, self.cfg.top_n)
                out.pending[b] = tuple((k, Hotkey(h)) for k, h in sel)
            return out
        prevs: dict[int, ChainSnapshot] = {}
        for p in extra:                                   # SEED_FALLBACK needs the predecessor's pool
            with contextlib.suppress(SnapshotDecodeError):
                prevs[p + plan.step] = await self.source.snapshot(p, hashes[p], None, ())
        sem = asyncio.Semaphore(max(1, self.cfg.snapshot_concurrency))

        async def one(b: int) -> None:
            async with sem:
                try:
                    out.snaps[b] = await self.source.snapshot(b, hashes[b], prevs.get(b), self.tracker.tracked_at(b))
                except SnapshotDecodeError as e:
                    out.invalid[b] = e

        tasks = [asyncio.ensure_future(one(b)) for b in todo]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return out

    def _prev_tracked(self, out: _ChunkRead, b: int) -> tuple[Pair, ...]:
        """T of the previous membership block: an earlier point of this (not yet committed) chunk, else the tracker."""
        earlier = [m for m in out.pending if m < b]
        if earlier:
            return out.pending[max(earlier)]
        return self.tracker.tracked_before(b)

    async def _probes(self, snaps: Mapping[int, ChainSnapshot]) -> list[CalibRow]:
        c = self.cfg
        rows: list[CalibRow] = []
        for b in sorted(snaps):
            s = snaps[b]
            if c.price_every_blocks > 0 and b % c.price_every_blocks == 0:
                rows += await price_probe(self.source, s, c.amm_probe_subnets)
            if c.amm_every_blocks > 0 and b % c.amm_every_blocks == 0:
                rows += await amm_probe(self.source, s, c.amm_probe_tao_rao, c.amm_probe_subnets)
            if c.prune_every_blocks > 0 and b % c.prune_every_blocks == 0:
                rows += await prune_probe(self.source, s)
        return rows


# ------------------------------------------------------------------------------------------------ lake readers
def load_listings(lake: Lake, blocks: Sequence[int]) -> dict[int, dict[int, tuple[Hotkey, ...]]]:
    """dividend_keys rows of the given blocks: block -> netuid -> sorted hotkeys."""
    out: dict[int, dict[int, list[str]]] = {}
    if not blocks:
        return {}
    con = lake.connect()
    try:
        rows = con.execute("SELECT block, netuid, hotkey FROM v_dividend_keys WHERE block BETWEEN ? AND ? "
                           "ORDER BY block, netuid, hotkey", [min(blocks), max(blocks)]).fetchall()
    finally:
        con.close()
    want = set(blocks)
    for b, n, h in rows:
        if int(b) in want:
            out.setdefault(int(b), {}).setdefault(int(n), []).append(str(h))
    return {b: {n: tuple(Hotkey(h) for h in sorted(set(hs))) for n, hs in d.items()} for b, d in out.items()}


@dataclass(frozen=True, slots=True)
class PanelGap:
    netuid: int
    reg_at: int
    hotkey: str
    first_block: int
    missing_block: int


def rebuild_tracker(lake: Lake, cfg: CollectorCfg, end: int) -> PanelTracker:
    """The tracker as the collector would rebuild it from the lake (membership blocks stored in the base series)."""
    tr = PanelTracker(cfg.panel_from, cfg.membership_every, cfg.level0, cfg.top_n)
    pts = tr.points(end)
    lake.refresh()
    stored = {r.block for r in lake.snapshot_refs() if r.series == ""}
    pts = [m for m in pts if m in stored]
    if not pts:
        return tr
    store = LakeSnapshotStore(lake, clock=max(pts))
    listings = load_listings(lake, pts)
    for m in pts:
        tr.record(m, store.at(Block(m)), listings.get(m, {}))
    return tr


def panel_gaps(lake: Lake, cfg: CollectorCfg | None = None, *, limit: int = 10_000) -> list[PanelGap]:
    """Section 11 WP4 acceptance: every tracked (hotkey, generation) pair - selected at a membership block m - must
    be present in every stored snapshot at b >= m while its generation is alive at b. Returns up to `limit`
    violations. The tracked sets are rebuilt from the lake; the anti-join runs in DuckDB (a full era-C lake holds
    ~20M snap_hotkey rows)."""
    cfg = cfg or CollectorCfg()
    con = lake.connect()
    try:
        row = con.execute("SELECT max(block) FROM v_global").fetchone()
        if row is None or row[0] is None:
            return []
        tr = rebuild_tracker(lake, cfg, int(row[0]))
        first: dict[tuple[int, int, str], int] = {}
        for m in tr.recorded:
            for key, thk in tr.tracked_at(m):
                first.setdefault((int(key.netuid), int(key.reg_at), str(thk)), m)
        if not first:
            return []
        con.execute("CREATE TEMP TABLE tracked (netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR, first_block UBIGINT)")
        con.executemany("INSERT INTO tracked VALUES (?, ?, ?, ?)", [(n, r, h, m) for (n, r, h), m in sorted(first.items())])
        gaps = con.execute(
            "SELECT t.netuid, t.reg_at, t.hotkey, t.first_block, s.block "
            "FROM tracked t JOIN (SELECT DISTINCT block, netuid, reg_at FROM v_subnet) s "
            "  ON s.netuid = t.netuid AND s.reg_at = t.reg_at AND s.block >= t.first_block "
            "LEFT JOIN (SELECT DISTINCT block, netuid, reg_at, hotkey FROM v_hotkey) h "
            "  ON h.block = s.block AND h.netuid = t.netuid AND h.reg_at = t.reg_at AND h.hotkey = t.hotkey "
            "WHERE h.block IS NULL ORDER BY t.netuid, t.reg_at, t.hotkey, s.block LIMIT ?", [limit]).fetchall()
    finally:
        con.close()
    return [PanelGap(int(n), int(r), str(h), int(m), int(b)) for n, r, h, m, b in gaps]


# ------------------------------------------------------------------------------------------------ CLI
def _build_source(keys_per_call: int | None) -> ReaderSource:
    from ..chain.rpc import RpcPool
    from ..ops.config_load import load_run_config
    from ..ops.secrets import get_secret

    cfg = load_run_config()
    secret = get_secret("onfinality")
    pool = RpcPool.from_cfg(cfg.rpc, keyed_archive_url=None if secret is None else secret.reveal())
    reader = JsonRpcChainReader(pool, keys_per_call=keys_per_call or cfg.rpc.keys_per_call,
                                max_concurrency=cfg.rpc.max_concurrency, provider_check_every=200)
    return ReaderSource(reader)


async def _backfill(a: argparse.Namespace) -> int:
    src = _build_source(a.keys_per_call)
    lake = Lake(a.lake)
    try:
        end = a.end
        if end is None:
            head, _ = await src.reader.finalized_head()
            end = int(head) - a.head_margin
        scheds: list[ScheduleSpec] = []
        for name in a.schedule:
            if name == "c60":
                scheds.append(schedule_c60(end, start=a.start or ERA_C_FIRST_BLOCK))
            elif name == "h300":
                scheds.append(schedule_h300(end if a.h300_end is None else a.h300_end, start=a.start or DTAO_LAUNCH_BLOCK))
            else:
                raise SystemExit(f"unknown schedule {name}")
        cfg = CollectorCfg(chunk_blocks=a.chunk_blocks, panel_from=a.panel_from, membership_every=a.membership_every,
                           price_every_blocks=a.price_every, amm_every_blocks=a.amm_every, prune_every_blocks=a.prune_every,
                           top_n=a.top_n)

        def progress(p: ChunkPlan, msg: str) -> None:
            log.info("%s [%d..%d]: %s", p.label, p.blocks[0], p.blocks[-1], msg)

        col = Collector(src, lake, cfg, on_chunk=progress)
        try:
            summary = await col.run(scheds, retry_invalid=a.retry_invalid, max_chunks=a.max_chunks)
        finally:
            col.close()
        print(json.dumps({"snapshots": summary.snapshots, "invalid": summary.invalid, "chunks": summary.chunks_committed,
                          "skipped": summary.chunks_skipped, "failed": summary.chunks_failed,
                          "membership_points": summary.membership_points, "calib_rows": summary.calib_rows,
                          "calib_max": summary.calib_max, "calls": summary.calls}, indent=1))
        return 1 if summary.chunks_failed else 0
    finally:
        lake.close()
        await src.reader.pool.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    from ..ops.secrets import RedactingFilter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())
    ap = argparse.ArgumentParser(prog="python -m taotrader.data.collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="collect scheduled snapshots (resumable)")
    bf.add_argument("--schedule", action="append", default=[], choices=["c60", "h300"])
    bf.add_argument("--start", type=int, default=None, help="override the schedule start block")
    bf.add_argument("--end", type=int, default=None, help="last block (default: finalized head - head-margin)")
    bf.add_argument("--h300-end", type=int, default=None, help="end of the h300 schedule (default: --end)")
    bf.add_argument("--head-margin", type=int, default=100)
    bf.add_argument("--lake", default="data/lake")
    bf.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS)
    bf.add_argument("--panel-from", type=int, default=PANEL_FROM_BLOCK)
    bf.add_argument("--membership-every", type=int, default=BLOCKS_PER_DAY)
    bf.add_argument("--top-n", type=int, default=5)
    bf.add_argument("--price-every", type=int, default=DEFAULT_PRICE_EVERY)
    bf.add_argument("--amm-every", type=int, default=DEFAULT_AMM_EVERY)
    bf.add_argument("--prune-every", type=int, default=DEFAULT_PRUNE_EVERY)
    bf.add_argument("--keys-per-call", type=int, default=None)
    bf.add_argument("--max-chunks", type=int, default=None)
    bf.add_argument("--retry-invalid", action="store_true")
    for name in ("status", "panel-gaps"):
        sp = sub.add_parser(name)
        sp.add_argument("--lake", default="data/lake")
        sp.add_argument("--panel-from", type=int, default=PANEL_FROM_BLOCK)
    a = ap.parse_args(argv)
    if a.cmd == "backfill":
        if not a.schedule:
            ap.error("at least one --schedule")
        return asyncio.run(_backfill(a))
    lake = Lake(a.lake)
    try:
        if a.cmd == "status":
            led = FetchLedger(lake.state_db)
            try:
                rows = led.load()
                counts: dict[str, int] = {}
                for r in rows.values():
                    counts[r.status] = counts.get(r.status, 0) + 1
                print(json.dumps({"ledger": counts, "meta": led.meta(), "chunks": len(lake.manifest()),
                                  "snapshots": len(lake.snapshot_refs())}, indent=1))
            finally:
                led.close()
            return 0
        gaps = panel_gaps(lake, CollectorCfg(panel_from=a.panel_from))
        for g in gaps[:50]:
            print(g)
        print(f"{len(gaps)} panel gap(s)")
        return 1 if gaps else 0
    finally:
        lake.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
