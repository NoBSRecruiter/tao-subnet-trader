"""taotrader/data/replay.py - ParquetReplay: the backtest DataSource (DESIGN.md sections 4.4, 8.1).

`ParquetReplay(lake, start, end, stride=60)` streams the lake's snapshots in strictly increasing block order with
`HealthObs.nominal()` health:
- stride thinning on absolute block grid cells: from every cell [k*stride, (k+1)*stride) the first available
  non-REFINED snapshot. The selection depends only on the lake, never on `start` or `after`, so a resumed stream
  yields exactly the tail of an uninterrupted one;
- refinement rows (snapshots carrying Quality.REFINED, written by data.refine) are merged into the stream unthinned
  (merge_refined=True), so fills and tripwires inside refinement windows are block-exact (section 8.1);
- warm-up: the stream starts `warmup_blocks` (30 days by default) before `start`. Items before `start` are the
  warm-up: `is_warmup(block)` tells the Runner to ingest them with `warm = False` (features fill, nothing trades);
- `stream(after)` yields only blocks strictly after `after` (the last journaled block on recovery);
- the snapshots come from the lake exactly as the collector built them (era-correct PoolState per row; section 8.2).

`store` is a LakeSnapshotStore over the same lake with the same selection, so `window()` returns exactly the
snapshots the stream delivers (and raises LookaheadError past the Runner's clock). cadence_blocks == stride is the
data-resolution contract the Runner checks against Strategy.min_cadence_blocks.

Costs (128 subnets x 4 tracked hotkeys, one core): ~18 ms per snapshot with digest verification (verify=True, the
default), ~10 ms without (every chunk's sha256 is still checked against the manifest). A decoded snapshot holds
~340 KiB, so the store's LRU (cache_size=1,024 snapshots, about 42 days at stride 60) costs up to ~350 MB.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

from ..core.errors import DataContractError
from ..core.events import HealthObs
from ..core.protocols import DataSource, SnapshotStore, SourceItem
from ..core.units import BLOCKS_PER_DAY, Block
from .lake import Lake
from .store import LakeSnapshotStore

WARMUP_DAYS: Final[int] = 30
WARMUP_BLOCKS: Final[int] = WARMUP_DAYS * BLOCKS_PER_DAY


class ParquetReplay:
    """core.protocols.DataSource over the Parquet lake."""

    def __init__(self, lake: Lake, start: int, end: int, stride: int = 60, *, warmup_blocks: int = WARMUP_BLOCKS,
                 merge_refined: bool = True, page_size: int = 128, verify: bool = True, cache_size: int = 1024,
                 health: HealthObs | None = None) -> None:
        if stride < 1:
            raise ValueError("stride must be >= 1")
        if end < start or start < 0 or warmup_blocks < 0 or page_size < 1:
            raise ValueError("bad replay range or parameters")
        self.cadence_blocks: int = stride
        self.start: Block = Block(start)
        self.end: Block = Block(end)
        self.warm_from: Block = Block(max(0, start - warmup_blocks))
        self.page_size = page_size
        self.health = health if health is not None else HealthObs.nominal()
        self.lake_store: LakeSnapshotStore = LakeSnapshotStore(lake, None, stride=stride, merge_refined=merge_refined,
                                                               cache_size=cache_size, verify=verify)
        self.store: SnapshotStore = self.lake_store     # the DataSource seam (Runner sets store.clock each tick)
        self._closed = False

    def is_warmup(self, block: int) -> bool:
        """True for warm-up items (block < start): ingest with warm = False, never trade."""
        return block < self.start

    def planned_blocks(self, after: Block | None = None) -> list[Block]:
        """The blocks stream(after) will yield, in order."""
        lo = int(self.warm_from) if after is None else max(int(self.warm_from), int(after) + 1)
        return self.lake_store.selected_blocks(lo, int(self.end))

    async def stream(self, after: Block | None) -> AsyncIterator[SourceItem]:
        if self._closed:
            raise RuntimeError("ParquetReplay is closed")
        blocks = self.planned_blocks(after)
        last = -1 if after is None else int(after)
        for i in range(0, len(blocks), self.page_size):
            for snap in self.lake_store.fetch(blocks[i:i + self.page_size]):
                if int(snap.block) <= last:
                    raise DataContractError(f"replay order violated: {snap.block} after {last}")
                last = int(snap.block)
                yield SourceItem(snapshot=snap, health=self.health)
                if self._closed:
                    return

    async def aclose(self) -> None:
        self._closed = True


def _conforms(r: ParquetReplay) -> DataSource:
    """Static check (mypy): ParquetReplay satisfies core.protocols.DataSource."""
    return r
