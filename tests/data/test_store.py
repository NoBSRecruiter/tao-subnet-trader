"""WP3 SnapshotStore tests (DESIGN.md sections 5.10, 7.1, 8.10): lookups by block and digest over lake + hot staging +
memory, the LookaheadError guard (including a peeking canary), the one-snapshot-per-block preference rule, stride
thinning with refined merging, transparency across compaction, and the incremental index matching a full rebuild."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from taotrader.core.errors import LookaheadError
from taotrader.core.state import ChainSnapshot, Quality, ReadPlan, SubnetState
from taotrader.core.units import Block
from taotrader.data.lake import Lake
from taotrader.data.recorder import HotStaging, Recorder
from taotrader.data.schema import snapshot_digest, with_digest
from taotrader.data.store import LakeSnapshotStore

T0 = 1_759_900_000_000            # chain timestamp of block 0 in these tests (ms)


@pytest.fixture(scope="session")
def snap(make_snapshot: Callable[..., ChainSnapshot], make_subnet: Callable[..., SubnetState]) -> Callable[..., ChainSnapshot]:
    """snap(block, variant=0, refined=False, plan=FULL) -> a digest-filled snapshot; variant changes the content."""

    def make(block: int, variant: int = 0, *, refined: bool = False, plan: ReadPlan = ReadPlan.FULL) -> ChainSnapshot:
        q = Quality.REFINED if refined else Quality.OK
        subs = [make_subnet(n, 8_000_000 + n, tempo=360 + variant, quality=q if n == 92 else Quality.OK) for n in (51, 92)]
        return with_digest(make_snapshot(block, subs, plan=plan, timestamp_ms=T0 + 12_000 * block))

    return make


def blocks_of(snaps: Any) -> list[int]:
    return [int(s.block) for s in snaps]


# ------------------------------------------------------------------------------------------------ guard + lookups
def test_lookups_and_the_lookahead_guard(snap: Any) -> None:
    st_ = LakeSnapshotStore()
    for b in (100, 160, 220, 280):
        st_.add(snap(b))
    with pytest.raises(LookaheadError):                       # clock starts at 0: nothing is visible (fail closed)
        st_.at(Block(100))
    st_.clock = Block(220)
    assert int(st_.at(Block(160)).block) == 160
    assert int(st_.at_or_before(Block(219)).block) == 160 and int(st_.at_or_before(Block(220)).block) == 220
    assert blocks_of(st_.window(Block(220), 120)) == [160, 220]                # until - span < block <= until
    assert blocks_of(st_.window(Block(220), 121)) == [100, 160, 220]
    assert blocks_of(st_.window(Block(220), 0)) == []
    assert blocks_of(st_.window(Block(150), 10**9)) == [100]
    for call in (lambda: st_.at(Block(221)), lambda: st_.at_or_before(Block(221)), lambda: st_.window(Block(280), 60),
                 lambda: st_.by_digest(snap(280).digest)):
        with pytest.raises(LookaheadError):
            call()
    with pytest.raises(KeyError):
        st_.at(Block(200))
    with pytest.raises(KeyError):
        st_.at_or_before(Block(99))
    with pytest.raises(KeyError):
        st_.by_digest("0" * 32)
    with pytest.raises(ValueError):
        st_.window(Block(200), -1)
    assert len(st_) == 4 and st_.selected_blocks(150, 280) == [160, 220, 280]       # unguarded tools API


def test_a_peeking_canary_strategy_raises(snap: Any) -> None:
    """Section 8.10: a canary strategy that peeks must raise."""
    store = LakeSnapshotStore()
    for b in range(0, 600, 60):
        store.add(snap(b))

    def canary_on_tick(block: int) -> float:
        future = store.window(Block(block + 60), 60)          # peeks one cadence ahead
        return float(len(future))

    for b in range(0, 540, 60):
        store.clock = Block(b)
        with pytest.raises(LookaheadError):
            canary_on_tick(b)
        assert int(store.window(Block(b), 60)[-1].block) == b   # the honest call works


def test_by_digest_resolves_every_stored_version(snap: Any) -> None:
    st_ = LakeSnapshotStore(clock=10**9)
    a, b = snap(100, 0), snap(100, 1)
    st_.add(a)
    st_.add(b)                                                  # re-delivery of the same block, different content
    assert st_.at(Block(100)) == b                              # memory: last added wins
    assert st_.by_digest(a.digest) == a and st_.by_digest(b.digest) == b
    assert st_.has_digest(a.digest) and not st_.has_digest("1" * 32)
    with pytest.raises(ValueError):
        st_.add(replace(a, digest="2" * 32))


# ------------------------------------------------------------------------------------------------ sources + preference
def test_preference_lake_series_hot_memory_and_refined(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    lake.write_snapshots([snap(60, 1), snap(120, 1, refined=True)], series="live")
    lake.write_snapshots([snap(60, 0)])                                          # base series "" wins over "live"
    rec = Recorder(tmp_path / "data" / "hot", lake, auto_compact=False)
    rec.record(snap(120, 2))                                                     # hot non-refined beats lake refined
    rec.record(snap(180, 3))
    rec.record(snap(180, 4))                                                     # hot: last appended wins
    st_ = LakeSnapshotStore(lake, rec.hot, clock=10**9)
    st_.add(snap(180, 5))                                                        # memory loses to hot
    st_.add(snap(240, 6, refined=True))
    assert [s.subnets[0].tempo - 360 for s in st_.window(Block(240), 10**6)] == [0, 2, 4, 6]
    for v, b in ((1, 60), (1, 120), (3, 180), (5, 180)):
        kw = {"refined": True} if (v, b) == (1, 120) else {}
        assert st_.by_digest(snap(b, v, **kw).digest) == snap(b, v, **kw)       # every version stays addressable
    no_ref = LakeSnapshotStore(lake, rec.hot, clock=10**9, merge_refined=False)
    no_ref.add(snap(240, 6, refined=True))
    assert no_ref.selected_blocks() == [60, 120, 180]
    rec.close()
    lake.close()


def test_stride_thinning_on_absolute_cells_with_refined_merge(snap: Any) -> None:
    st_ = LakeSnapshotStore(clock=10**9, stride=60)
    for b in (14, 30, 74, 134, 150, 179, 194):
        st_.add(snap(b))
    st_.add(snap(100, refined=True))
    st_.add(snap(101, refined=True))
    assert st_.selected_blocks() == [14, 74, 100, 101, 134, 194]       # first per [k*60, (k+1)*60) + refined blocks
    st2 = LakeSnapshotStore(clock=10**9, stride=60, merge_refined=False)
    st2.add(snap(100, refined=True))
    st2.add(snap(30))
    assert st2.selected_blocks() == [30]


def test_answers_are_identical_before_and_after_compaction(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    rec = Recorder(tmp_path / "data" / "hot", lake, auto_compact=False)
    blocks = list(range(0, 1_200, 25))                                    # 4 hours of chain time
    for b in blocks:
        rec.record(snap(b))
    st_ = LakeSnapshotStore(lake, rec.hot, clock=10**9, cache_size=4)
    before = [s.digest for s in st_.window(Block(10**6), 10**7)]
    assert len(before) == len(blocks)
    written = rec.compact(final=True)
    assert written and rec.hot.files() == []                             # hot files deleted after the manifest commit
    after = [s.digest for s in st_.window(Block(10**6), 10**7)]
    assert after == before == [snapshot_digest(snap(b)) for b in blocks]
    assert all(e.lake is not None for e in st_._ix.sel.values())
    rec.close()
    lake.close()


def test_store_sees_other_instances_writes_on_a_miss(tmp_path: Path, snap: Any) -> None:
    lake_w = Lake(tmp_path / "data" / "lake")
    lake_r = Lake(tmp_path / "data" / "lake")
    hot_dir = tmp_path / "data" / "hot"
    rec = Recorder(hot_dir, lake_w, auto_compact=False)
    reader = LakeSnapshotStore(lake_r, HotStaging(hot_dir), clock=10**9)
    assert len(reader) == 0
    rec.record(snap(60))
    lake_w.write_snapshots([snap(120)])
    assert int(reader.at(Block(60)).block) == 60                         # miss -> refresh -> found in hot staging
    assert reader.by_digest(snap(120).digest) == snap(120)               # ... and in the lake
    rec.compact(final=True)                                              # hot file removed by the writer
    reader.refresh()
    assert reader.at(Block(60)) == snap(60) and len(reader) == 2         # now served from the lake
    rec.close()
    lake_w.close()
    lake_r.close()


def test_stale_hot_entry_falls_back_to_the_lake(tmp_path: Path, snap: Any) -> None:
    """A reader indexed a hot record, the writer compacted and deleted the file: the load re-resolves by digest."""
    lake = Lake(tmp_path / "data" / "lake")
    hot_dir = tmp_path / "data" / "hot"
    rec = Recorder(hot_dir, lake, auto_compact=False)
    for b in (60, 120):
        rec.record(snap(b))
    reader = LakeSnapshotStore(Lake(tmp_path / "data" / "lake"), HotStaging(hot_dir), clock=10**9, cache_size=1)
    assert reader.selected_blocks() == [60, 120]                          # indexed from hot staging
    rec.compact(final=True)
    assert not list(hot_dir.glob("*.zst"))
    assert reader.at(Block(60)) == snap(60) and reader.at(Block(120)) == snap(120)
    rec.close()
    lake.close()


# ------------------------------------------------------------------------------------------------ incremental index
@pytest.fixture(scope="module")
def shared_lake(tmp_path_factory: pytest.TempPathFactory, snap: Any) -> Any:
    """One immutable lake for every example (chunks are never edited; the examples only append to hot staging)."""
    lake = Lake(tmp_path_factory.mktemp("shared") / "lake")
    lake.write_snapshots([snap(b * 10, 9) for b in (1, 5, 9, 13)])
    lake.write_snapshots([snap(b * 10, 8, refined=True) for b in (2, 5, 30)], series="refine")
    yield lake
    lake.close()


@settings(max_examples=40)
@given(ops=st.lists(st.tuples(st.integers(0, 40), st.integers(0, 3), st.booleans(), st.sampled_from(["mem", "hot"])),
                    min_size=1, max_size=25),
       stride=st.sampled_from([None, 7]), merge=st.booleans())
def test_incremental_overlay_equals_a_full_rebuild(tmp_path_factory: pytest.TempPathFactory, snap: Any, shared_lake: Lake,
                                                   ops: list[tuple[int, int, bool, str]], stride: int | None,
                                                   merge: bool) -> None:
    hot_dir = tmp_path_factory.mktemp("inc") / "hot"
    rec = Recorder(hot_dir, shared_lake, auto_compact=False)
    live = LakeSnapshotStore(shared_lake, rec.hot, clock=10**9, stride=stride, merge_refined=merge)
    mem_snaps: list[ChainSnapshot] = []
    for b, v, refined, where in ops:
        s = snap(b * 10, v, refined=refined)
        if where == "hot":
            rec.record(s)
        else:
            live.add(s)
            mem_snaps.append(s)
        len(live)                                                        # index incrementally after every op
    fresh = LakeSnapshotStore(shared_lake, HotStaging(hot_dir), clock=10**9, stride=stride, merge_refined=merge)
    for s in mem_snaps:
        fresh.add(s)
    assert live.selected_blocks() == fresh.selected_blocks()
    assert {b: e.digest for b, e in live._ix.sel.items()} == {b: e.digest for b, e in fresh._ix.sel.items()}
    assert {k: e.digest for k, e in live._ix.by_digest.items()} == {k: e.digest for k, e in fresh._ix.by_digest.items()}
    rec.close()
