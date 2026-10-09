"""WP3 ParquetReplay tests (DESIGN.md sections 4.4, 8.1, 8.10): strictly increasing blocks, `after` honoured, 30-day
warm-up, stride thinning on absolute cells, refinement rows merged unthinned, nominal health, era-correct pools
delivered exactly as stored, resume == tail of an uninterrupted stream, and independence from data after `end`
(future truncation)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from taotrader.core.errors import LookaheadError
from taotrader.core.events import HealthObs
from taotrader.core.protocols import DataSource, SourceItem
from taotrader.core.state import ChainSnapshot, PoolKind, Quality, SubnetState
from taotrader.core.units import BLOCKS_PER_DAY, Block
from taotrader.data.lake import Lake
from taotrader.data.replay import WARMUP_BLOCKS, ParquetReplay
from taotrader.data.schema import with_digest

START = 8_486_594 + 60 * 400            # an era-C start; warm-up reaches back into era B


@pytest.fixture(scope="session")
def snap(make_snapshot: Callable[..., ChainSnapshot], make_subnet: Callable[..., SubnetState],
         make_pool: Callable[..., Any]) -> Callable[..., ChainSnapshot]:
    def make(block: int, *, refined: bool = False) -> ChainSnapshot:
        era_b = block < 8_486_594
        pool = make_pool(kind=PoolKind.CP_V3_VIRTUAL if era_b else PoolKind.BALANCER,
                         px_tao=10**13 if era_b else None, px_alpha=3 * 10**15 if era_b else None)
        q = Quality.REFINED if refined else Quality.OK
        subs = [make_subnet(n, 7_000_000 + n, pool=pool, tempo=360 + block % 7, quality=q) for n in (19, 64)]
        return with_digest(make_snapshot(block, subs, timestamp_ms=1_700_000_000_000 + 12_000 * block))

    return make


def collect(src: ParquetReplay, after: int | None) -> list[SourceItem]:
    async def run() -> list[SourceItem]:
        return [item async for item in src.stream(None if after is None else Block(after))]
    return asyncio.run(run())


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory, snap: Any) -> Any:
    """60-block collector snapshots (offset 14, like the 8,486,594-anchored schedule) from 31 days before START to
    START + 3,600, a sparse 300-block patch, and a per-block refinement window around START + 1,200."""
    lk = Lake(tmp_path_factory.mktemp("replay") / "lake")
    base = list(range(START - WARMUP_BLOCKS - BLOCKS_PER_DAY, START + 3_600, 60))      # all == 14 (mod 60)
    lk.write_snapshots([snap(b) for b in base if b < 8_486_594])
    lk.write_snapshots([snap(b) for b in base if b >= 8_486_594])
    lk.write_snapshots([snap(b, refined=True) for b in range(START + 1_190, START + 1_211)], series="refine")
    yield lk
    lk.close()


def test_stream_yields_strictly_increasing_blocks_with_warmup_and_nominal_health(lake: Lake) -> None:
    src = ParquetReplay(lake, START, START + 3_000, stride=60)
    items = collect(src, None)
    blocks = [int(i.snapshot.block) for i in items]
    assert blocks == sorted(set(blocks))                                   # strictly increasing
    assert blocks[0] >= START - WARMUP_BLOCKS and blocks[0] - 60 < START - WARMUP_BLOCKS
    assert blocks[-1] <= START + 3_000
    assert WARMUP_BLOCKS == 30 * BLOCKS_PER_DAY and src.warm_from == START - WARMUP_BLOCKS
    warm = [b for b in blocks if src.is_warmup(b)]
    assert warm and all(b < START for b in warm) and not any(src.is_warmup(b) for b in blocks if b >= START)
    assert all(i.health == HealthObs.nominal() for i in items)
    assert src.cadence_blocks == 60
    refined = [b for b in blocks if START + 1_190 <= b <= START + 1_210]
    assert refined == list(range(START + 1_190, START + 1_211))           # refinement rows merged, unthinned
    coarse = [b for b in blocks if not START + 1_190 <= b <= START + 1_210]
    assert all(b % 60 == 14 for b in coarse) and len({b // 60 for b in coarse}) == len(coarse)


def test_snapshots_arrive_exactly_as_stored_with_era_correct_pools(lake: Lake, snap: Any) -> None:
    items = collect(ParquetReplay(lake, START, START + 120, stride=60, warmup_blocks=60 * 500), None)
    kinds = {int(i.snapshot.block) < 8_486_594: i.snapshot.subnets[0].pool.kind for i in items}
    assert kinds == {True: PoolKind.CP_V3_VIRTUAL, False: PoolKind.BALANCER}
    for i in items[:5] + items[-5:]:
        assert i.snapshot == snap(int(i.snapshot.block))


@pytest.fixture(scope="module")
def full_blocks(lake: Lake) -> list[int]:
    return [int(i.snapshot.block) for i in collect(ParquetReplay(lake, START, START + 3_000), None)]


@pytest.mark.parametrize("after_offset", [-WARMUP_BLOCKS - 10**6, -WARMUP_BLOCKS, -1, 0, 1_195, 1_201, 2_999, 3_000, 10**6])
def test_after_is_honoured_and_resume_equals_the_tail(lake: Lake, full_blocks: list[int], after_offset: int) -> None:
    after = START + after_offset
    src = ParquetReplay(lake, START, START + 3_000, page_size=97)
    resumed = [int(i.snapshot.block) for i in collect(src, after)]
    assert resumed == [b for b in full_blocks if b > after] == src.planned_blocks(Block(after))


@pytest.mark.parametrize("stride", [1, 60, 300, 1_000])
def test_stride_selection_is_on_absolute_cells_and_independent_of_start(lake: Lake, stride: int) -> None:
    a = [int(i.snapshot.block) for i in collect(ParquetReplay(lake, START, START + 3_000, stride=stride), None)]
    b = [int(i.snapshot.block) for i in collect(ParquetReplay(lake, START + 7, START + 3_000, stride=stride,
                                                              warmup_blocks=WARMUP_BLOCKS + 7), None)]
    assert a == b                                                           # a shifted start selects the same blocks
    cells = [x // stride for x in a if not START + 1_190 <= x <= START + 1_210]
    assert len(cells) == len(set(cells))


def test_merge_refined_false_drops_refinement_rows(lake: Lake) -> None:
    blocks = [int(i.snapshot.block) for i in collect(ParquetReplay(lake, START, START + 3_000, merge_refined=False), None)]
    assert all(b % 60 == 14 for b in blocks)


def test_future_truncation_does_not_change_earlier_items(tmp_path: Path, snap: Any) -> None:
    """Section 8.10: runs on data[:k] and data[:k+m] agree before k (selection never looks at later data)."""
    blocks = list(range(START, START + 6_000, 60))
    short, long_ = Lake(tmp_path / "a" / "lake"), Lake(tmp_path / "b" / "lake")
    short.write_snapshots([snap(b) for b in blocks[:50]])
    long_.write_snapshots([snap(b) for b in blocks])
    long_.write_snapshots([snap(b, refined=True) for b in range(START + 4_000, START + 4_010)], series="refine")
    k = blocks[49]
    a = collect(ParquetReplay(short, START, START + 6_000, warmup_blocks=0), None)
    b = collect(ParquetReplay(long_, START, START + 6_000, warmup_blocks=0), None)
    assert [i.snapshot for i in a] == [i.snapshot for i in b if int(i.snapshot.block) <= k]
    short.close()
    long_.close()


def test_store_matches_the_stream_and_guards_lookahead(lake: Lake) -> None:
    src = ParquetReplay(lake, START, START + 600, stride=60)
    seen: list[ChainSnapshot] = []

    async def run() -> None:
        async for item in src.stream(None):
            snap = item.snapshot
            src.store.clock = snap.block                                    # what the Runner does each tick
            seen.append(snap)
            window = src.store.window(snap.block, 3 * 60)            # may also hold lake data from before warm_from
            assert window[-1] == snap
            assert [s for s in window if s.block >= src.warm_from] == [s for s in seen if int(s.block) > int(snap.block) - 180]
            with pytest.raises(LookaheadError):
                src.store.window(Block(int(snap.block) + 1), 60)
            with pytest.raises(LookaheadError):
                src.store.at(Block(int(snap.block) + 60))

    asyncio.run(run())
    assert len(seen) == len(src.planned_blocks())


def test_aclose_stops_the_stream_and_closed_sources_refuse(lake: Lake) -> None:
    src = ParquetReplay(lake, START, START + 3_000, page_size=3)

    async def run() -> int:
        n = 0
        async for _ in src.stream(None):
            n += 1
            if n == 5:
                await src.aclose()
        return n

    assert asyncio.run(run()) == 5

    async def again() -> AsyncIterator[SourceItem]:
        async for item in src.stream(None):
            yield item

    async def drain() -> None:
        async for _ in again():
            pass

    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(drain())


def test_parameter_validation_and_protocol_shape(lake: Lake) -> None:
    bads: list[dict[str, Any]] = [{"stride": 0}, {"end": START - 1}, {"warmup_blocks": -1}, {"page_size": 0}]
    for bad in bads:
        kw: dict[str, Any] = {"stride": 60, "end": START + 60, **bad}
        with pytest.raises(ValueError):
            ParquetReplay(lake, START, kw.pop("end"), **kw)
    src: DataSource = ParquetReplay(lake, START, START + 60)
    assert src.cadence_blocks == 60 and hasattr(src.store, "clock")
