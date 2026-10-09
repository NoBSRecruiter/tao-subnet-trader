"""taotrader/data/store.py - SnapshotStore over the lake plus hot staging (DESIGN.md sections 5.10, 7.1, 8.10).

`LakeSnapshotStore` implements core.protocols.SnapshotStore:
- sources: the Parquet lake (Lake), live hot staging (HotStaging, shared with the in-process Recorder or a read-only
  view of another process's), and snapshots added in memory (`add`, for tests and small tools). Any source may be
  absent; LakeSnapshotStore() with only `add` is an in-memory store.
- lookup by block (`at`, `at_or_before`, `window`) and by digest (`by_digest`, which resolves EVERY stored version,
  so a journaled SnapshotObserved digest is always found while its snapshot exists anywhere).
- lookahead guard (section 8.10): `clock` is set by the Runner before each tick; asking for any block after it (or a
  digest whose snapshot is after it) raises core.errors.LookaheadError. `window(until, span)` returns the selected
  snapshots with until - span < block <= until, oldest first. The clock starts at 0, so a store nobody has clocked
  refuses everything (fail closed).
- one snapshot per block. When several stored snapshots share a block, the preferred one is: non-REFINED before
  REFINED (Quality.REFINED in any subnet), then the base lake series "" before named series (e.g. "live",
  "refine"), then by series name, then lake before hot before memory, then by chunk path (lake) or LAST appended
  (hot, memory). The rule depends only on what is stored, so it is identical before and after compaction.
- optional thinning (used by ParquetReplay): `stride` keeps, per absolute grid cell [k*stride, (k+1)*stride), the
  first available non-REFINED block; REFINED snapshots are merged in unthinned when merge_refined is True.

The lake part of the index is cached per lake manifest version; hot and memory entries are overlaid on it, so a live
tick (one hot append) costs O(hot + selected blocks) list/dict copies, not a rebuild of the lake index.
Reads of hot records or chunks that vanished meanwhile (compaction or retirement by another process, or a Windows
delete-pending file) are retried once after a refresh.
"""
from __future__ import annotations

import bisect
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..core.errors import LookaheadError
from ..core.protocols import SnapshotStore
from ..core.state import ChainSnapshot, Quality
from ..core.units import Block
from . import schema
from .lake import Lake, LakeError, LakeSnapRef
from .recorder import HotRef, HotStaging

_REFINED = int(Quality.REFINED)
_Rank = tuple[int, int, str, int, str, int]


@dataclass(frozen=True, slots=True)
class _Entry:
    block: int
    digest: str
    refined: bool
    rank: _Rank
    lake: LakeSnapRef | None = None
    hot: HotRef | None = None
    mem: ChainSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _Index:
    best: dict[int, _Entry]          # preferred entry per block (before thinning)
    by_digest: dict[str, _Entry]     # preferred entry per digest
    sel: dict[int, _Entry]           # selected entries (after thinning / refined filter)
    sel_blocks: list[int]            # sorted keys of sel


_EMPTY = _Index({}, {}, {}, [])


def _lake_entry(r: LakeSnapRef) -> _Entry:
    refined = bool(r.quality_or & _REFINED)
    return _Entry(r.block, r.digest, refined, (int(refined), 0 if r.series == "" else 1, r.series, 0, r.stem, 0), lake=r)


class LakeSnapshotStore:
    """SnapshotStore over lake + hot staging + memory, with the LookaheadError guard. Thread-safe."""

    def __init__(self, lake: Lake | None = None, hot: HotStaging | None = None, *, clock: int = 0,
                 stride: int | None = None, merge_refined: bool = True, hot_series: str = "live",
                 cache_size: int = 1024, verify: bool = True) -> None:
        if stride is not None and stride < 1:
            raise ValueError("stride must be >= 1")
        self.clock: Block = Block(clock)
        self.lake = lake
        self.hot = hot
        self.stride = stride
        self.merge_refined = merge_refined
        self.hot_series = hot_series
        self.verify = verify
        self._cache_size = max(1, cache_size)
        self._lock = threading.RLock()
        self._mem: dict[str, tuple[int, ChainSnapshot]] = {}     # digest -> (insertion ordinal, snapshot)
        self._mem_n = 0
        self._mem_version = 0
        self._seen: tuple[int, int, int] = (-1, -1, -1)
        self._base: tuple[int, _Index] | None = None            # (lake version, lake-only index)
        self._ix: _Index = _EMPTY
        self._cache: OrderedDict[str, ChainSnapshot] = OrderedDict()

    # ------------------------------------------------------------------------------------------ sources / index
    def add(self, snap: ChainSnapshot) -> ChainSnapshot:
        """Add a snapshot to the in-memory source (digest filled or verified). Returns it with its digest."""
        snap = schema.with_digest(snap)
        with self._lock:
            self._mem[snap.digest] = (self._mem_n, snap)
            self._mem_n += 1
            self._mem_version += 1
        return snap

    def refresh(self) -> None:
        """Pick up chunks and hot records written by other processes. Hot staging is re-read BEFORE the lake: the
        recorder commits a chunk before deleting the hot file it came from, so a record that vanished from hot staging
        is always found in the lake read afterwards (a concurrent reader never sees a block disappear)."""
        if self.hot is not None:
            self.hot.refresh()
        if self.lake is not None:
            self.lake.refresh()
        with self._lock:
            self._reindex()

    def _versions(self) -> tuple[int, int, int]:
        return (self.lake.version if self.lake is not None else 0, self.hot.version if self.hot is not None else 0,
                self._mem_version)

    def _select(self, best: dict[int, _Entry]) -> tuple[dict[int, _Entry], list[int]]:
        chosen: dict[int, _Entry] = {}
        last_cell = -1
        for b in sorted(best):
            e = best[b]
            if e.refined:
                if self.merge_refined:
                    chosen[b] = e
                continue
            if self.stride is not None:
                cell = b // self.stride
                if cell == last_cell:
                    continue
                last_cell = cell
            chosen[b] = e
        return chosen, sorted(chosen)

    def _base_index(self, lake_version: int) -> _Index:
        if self._base is not None and self._base[0] == lake_version:
            return self._base[1]
        best: dict[int, _Entry] = {}
        by_digest: dict[str, _Entry] = {}
        if self.lake is not None:
            for r in self.lake.snapshot_refs():
                e = _lake_entry(r)
                cur = best.get(e.block)
                if cur is None or e.rank < cur.rank:
                    best[e.block] = e
                prev = by_digest.get(e.digest)
                if prev is None or e.rank < prev.rank:
                    by_digest[e.digest] = e
        sel, blocks = self._select(best)
        ix = _Index(best, by_digest, sel, blocks)
        self._base = (lake_version, ix)
        return ix

    def _extra_entries(self) -> list[_Entry]:
        out: list[_Entry] = []
        if self.hot is not None:
            all_hot = self.hot.refs()
            n = len(all_hot)
            for i, h in enumerate(all_hot):
                refined = bool(h.quality_or & _REFINED)
                out.append(_Entry(h.block, h.digest, refined, (int(refined), 1, self.hot_series, 1, "", n - i), hot=h))
        for d, (i, s) in self._mem.items():
            refined = bool(schema.quality_or(s) & _REFINED)
            out.append(_Entry(int(s.block), d, refined, (int(refined), 1, "~memory", 2, "", -i), mem=s))
        return out

    def _reindex(self) -> None:
        v = self._versions()
        if v == self._seen:
            return
        base = self._base_index(v[0])
        extra = self._extra_entries()
        if not extra:
            self._ix = base
            self._seen = v
            return
        best = dict(base.best)
        by_digest = dict(base.by_digest)
        touched: set[int] = set()
        for e in extra:
            cur = best.get(e.block)
            if cur is None or e.rank < cur.rank:
                best[e.block] = e
                touched.add(e.block)
            prev = by_digest.get(e.digest)
            if prev is None or e.rank < prev.rank:
                by_digest[e.digest] = e
        if self.stride is not None:
            sel, blocks = self._select(best)           # thinning depends on neighbours: recompute (replay stores only)
        else:
            sel = dict(base.sel)
            blocks = list(base.sel_blocks)
            for b in sorted(touched):
                e = best[b]
                keep = self.merge_refined or not e.refined
                if b in sel:
                    if keep:
                        sel[b] = e
                    else:
                        del sel[b]
                        blocks.pop(bisect.bisect_left(blocks, b))
                elif keep:
                    sel[b] = e
                    if not blocks or b > blocks[-1]:
                        blocks.append(b)
                    else:
                        bisect.insort(blocks, b)
        self._ix = _Index(best, by_digest, sel, blocks)
        self._seen = v

    def _ensure(self) -> _Index:
        with self._lock:
            self._reindex()
            return self._ix

    # ------------------------------------------------------------------------------------------ guard
    def _guard(self, block: int, what: str) -> None:
        if block > self.clock:
            raise LookaheadError(f"{what}: block {block} is after the engine clock {self.clock}")

    # ------------------------------------------------------------------------------------------ SnapshotStore
    def at(self, block: Block) -> ChainSnapshot:
        """The snapshot at exactly `block`; KeyError if none is stored."""
        self._guard(int(block), "at")
        e = self._lookup(lambda ix: ix.sel.get(int(block)))
        if e is None:
            raise KeyError(f"no snapshot at block {block}")
        return self._load([e])[0]

    def at_or_before(self, block: Block) -> ChainSnapshot:
        """The latest snapshot at or before `block`; KeyError if there is none."""
        self._guard(int(block), "at_or_before")

        def find(ix: _Index) -> _Entry | None:
            i = bisect.bisect_right(ix.sel_blocks, int(block))
            return ix.sel[ix.sel_blocks[i - 1]] if i else None

        e = self._lookup(find)
        if e is None:
            raise KeyError(f"no snapshot at or before block {block}")
        return self._load([e])[0]

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        """Selected snapshots with until - span_blocks < block <= until, oldest first. LookaheadError past clock."""
        self._guard(int(until), "window")
        if span_blocks < 0:
            raise ValueError("span_blocks must be >= 0")
        ix = self._ensure()
        lo = bisect.bisect_right(ix.sel_blocks, int(until) - span_blocks)
        hi = bisect.bisect_right(ix.sel_blocks, int(until))
        return tuple(self._load([ix.sel[b] for b in ix.sel_blocks[lo:hi]]))

    def by_digest(self, digest: str) -> ChainSnapshot:
        """Any stored snapshot by its digest (superseded re-deliveries included); KeyError if unknown.
        LookaheadError if that snapshot is after the clock."""
        e = self._lookup(lambda ix: ix.by_digest.get(digest))
        if e is None:
            raise KeyError(f"no snapshot with digest {digest}")
        self._guard(e.block, "by_digest")
        return self._load([e])[0]

    def has_digest(self, digest: str) -> bool:
        """True if any source stores a snapshot with this digest (unguarded; no load)."""
        return self._lookup(lambda ix: ix.by_digest.get(digest)) is not None

    # ------------------------------------------------------------------------------------------ unguarded (data sources)
    def selected_blocks(self, lo: int | None = None, hi: int | None = None) -> list[Block]:
        """Selected blocks in [lo, hi] (inclusive, unguarded: for data sources and tools, never for strategies)."""
        ix = self._ensure()
        i = 0 if lo is None else bisect.bisect_left(ix.sel_blocks, lo)
        j = len(ix.sel_blocks) if hi is None else bisect.bisect_right(ix.sel_blocks, hi)
        return [Block(b) for b in ix.sel_blocks[i:j]]

    def fetch(self, blocks: Sequence[int]) -> list[ChainSnapshot]:
        """Load the selected snapshots at `blocks` (unguarded bulk read for ParquetReplay); KeyError if one is absent."""
        ix = self._ensure()
        missing = [b for b in blocks if b not in ix.sel]
        if missing:
            raise KeyError(f"no snapshot at blocks {missing[:5]}")
        return self._load([ix.sel[b] for b in blocks])

    def __len__(self) -> int:
        return len(self._ensure().sel_blocks)

    # ------------------------------------------------------------------------------------------ loading
    def _lookup(self, find: Callable[[_Index], _Entry | None]) -> _Entry | None:
        e = find(self._ensure())
        if e is None:                                # maybe written by another process since the last refresh
            self.refresh()
            e = find(self._ensure())
        return e

    def _load(self, entries: Sequence[_Entry]) -> list[ChainSnapshot]:
        try:
            return self._load_once(entries)
        except (OSError, LakeError):
            self.refresh()                           # compacted / retired meanwhile: resolve the digests again
            ix = self._ensure()
            again = [ix.by_digest.get(e.digest) for e in entries]
            if any(a is None for a in again):
                raise
            return self._load_once([a for a in again if a is not None])

    def _load_once(self, entries: Sequence[_Entry]) -> list[ChainSnapshot]:
        with self._lock:
            have = {e.digest: self._cache[e.digest] for e in entries if e.digest in self._cache}
            for d in have:
                self._cache.move_to_end(d)
        todo = [e for e in entries if e.digest not in have]
        lake_refs = [e.lake for e in todo if e.lake is not None]
        if lake_refs:
            if self.lake is None:
                raise LakeError("lake entry without a lake")
            for s in self.lake.load_snapshots(lake_refs, verify=self.verify):
                have[s.digest] = s
        for e in todo:
            if e.hot is not None:
                if self.hot is None:
                    raise LakeError("hot entry without hot staging")
                have[e.digest] = self.hot.read(e.hot, verify=self.verify)
            elif e.mem is not None:
                have[e.digest] = e.mem
        with self._lock:
            for e in todo:
                self._cache[e.digest] = have[e.digest]
                self._cache.move_to_end(e.digest)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return [have[e.digest] for e in entries]


def _conforms(s: LakeSnapshotStore) -> SnapshotStore:
    """Static check (mypy): LakeSnapshotStore satisfies core.protocols.SnapshotStore."""
    return s
