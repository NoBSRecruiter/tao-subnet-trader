"""WP3 recorder tests (DESIGN.md sections 4.5, 4.7, 7.1 "Live hot staging"; section 11 WP3 acceptance).

- hot staging: JSONL.zst named by the snapshot's UTC chain day, fsync per record, a valid multi-frame zstd stream;
  torn tails are repaired by the writer and ignored by readers; corruption before valid data fails loudly;
- hourly compaction to Parquet: only closed hours whose blocks the journal committed; hot files deleted only after the
  manifest rows commit;
- acceptance: a crash between the hot fsync and the journal commit leaves a consistent store (a killed process);
- acceptance: concurrent DuckDB/store readers in other processes while the recorder appends and compacts, on Windows.
"""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import zstandard

from taotrader.core import codec
from taotrader.core.errors import DecodeError
from taotrader.core.events import HealthObs, SnapshotObserved
from taotrader.core.state import ChainSnapshot, ReadPlan, SubnetState
from taotrader.core.units import Block, BookId, LogicalTime
from taotrader.data import recorder as rec_mod
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.data.recorder import HEADER_LEN, HotStaging, HotStagingError, Recorder, day_file
from taotrader.data.schema import with_digest
from taotrader.data.store import LakeSnapshotStore

MIDNIGHT = 1_760_054_400_000            # 2025-10-10T00:00:00Z (ms)
B0 = 9_300_000


@pytest.fixture(scope="session")
def snap(make_snapshot: Callable[..., ChainSnapshot], make_subnet: Callable[..., SubnetState]) -> Callable[..., ChainSnapshot]:
    """snap(k, variant=0, plan=FULL): block B0 + 10k at chain time MIDNIGHT - 2 h + 120 s * k (crosses a UTC day)."""

    def make(k: int, variant: int = 0, plan: ReadPlan = ReadPlan.FULL) -> ChainSnapshot:
        subs = [make_subnet(n, 8_000_000 + n, tempo=360 + variant, tao_flow_cum=k) for n in (51, 92)]
        return with_digest(make_snapshot(B0 + 10 * k, subs, plan=plan, timestamp_ms=MIDNIGHT - 7_200_000 + 120_000 * k))

    return make


def obs(s: ChainSnapshot) -> SnapshotObserved:
    return SnapshotObserved(block=s.block, block_hash=s.block_hash, digest=s.digest, plan=s.plan, ts_ms=s.timestamp_ms,
                            health=HealthObs.nominal())


# ------------------------------------------------------------------------------------------------ hot staging format
def test_record_fsyncs_names_files_by_utc_chain_day_and_is_plain_jsonl_zst(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    rec = Recorder(tmp_path / "data" / "hot", lake, auto_compact=False)
    snaps = [snap(k) for k in (0, 59, 60, 61)]                     # k=60 is 00:00:00Z of the next day
    digests = [rec.record(s) for s in snaps]
    assert digests == [s.digest for s in snaps]
    assert rec.hot.files() == ["20251009.jsonl.zst", "20251010.jsonl.zst"] == [day_file(s.timestamp_ms) for s in snaps[1:3]]
    # any zstd decoder reads the file as JSONL (the index headers are skippable frames)
    lines: list[bytes] = []
    for name in rec.hot.files():
        raw = (tmp_path / "data" / "hot" / name).read_bytes()
        lines += zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True).read().splitlines()
    assert [codec.decode_bytes(ChainSnapshot, ln) for ln in lines] == snaps
    assert lines == [codec.canonical_bytes(s) for s in snaps]
    # a fresh reader (another process) indexes and decodes the same records
    reader = HotStaging(tmp_path / "data" / "hot")
    assert [reader.read(r) for r in reader.refs()] == snaps
    assert [(r.block, r.digest, r.ts_ms) for r in reader.refs()] == [(int(s.block), s.digest, s.timestamp_ms) for s in snaps]
    with pytest.raises(HotStagingError):
        reader.append(snaps[0])
    rec.close()
    lake.close()


def test_recent_records_are_served_from_memory_and_stale_files_fail_loudly(tmp_path: Path, snap: Any) -> None:
    hot = HotStaging(tmp_path / "hot", writable=True, recent=2)
    refs = [hot.append(snap(k)) for k in range(3)]
    assert hot.read(refs[2]) is hot._recent[refs[2].digest]          # no decode for own recent records
    assert hot.read(refs[0]) == snap(0) and refs[0].digest not in hot._recent
    path = tmp_path / "hot" / refs[1].file
    data = bytearray(path.read_bytes())
    data[refs[1].offset + 5] ^= 0xFF                                   # bit rot inside record 1's data frame
    path.write_bytes(bytes(data))
    with pytest.raises(DecodeError, match="CRC"):
        HotStaging(tmp_path / "hot").read(refs[1])
    hot.close()


def test_torn_tail_is_repaired_by_the_writer_and_ignored_by_readers(tmp_path: Path, snap: Any) -> None:
    hot_dir = tmp_path / "hot"
    w = HotStaging(hot_dir, writable=True)
    for k in range(3):
        w.append(snap(k))
    w.close()
    path = hot_dir / day_file(snap(0).timestamp_ms)
    good = path.stat().st_size
    _, full = rec_mod.encode_record(snap(3), zstandard.ZstdCompressor())
    for torn in (full[:10], full[:HEADER_LEN + 7]):                      # crash inside the header / inside the frame
        path.write_bytes(path.read_bytes()[:good] + torn)
        r = HotStaging(hot_dir)                                           # readers never truncate
        assert len(r.refs()) == 3 and path.stat().st_size == good + len(torn)
        w2 = HotStaging(hot_dir, writable=True)                          # the writer repairs on open
        assert len(w2.refs()) == 3 and path.stat().st_size == good
        w2.append(snap(3))
        assert [x.block for x in HotStaging(hot_dir).refs()] == [B0, B0 + 10, B0 + 20, B0 + 30]
        w2.close()
        path.write_bytes(path.read_bytes()[:good])
    # a complete last record whose CRC does not match is dropped too
    bad = bytearray(full)
    bad[-3] ^= 0x01
    path.write_bytes(path.read_bytes()[:good] + bytes(bad))
    w3 = HotStaging(hot_dir, writable=True)
    assert len(w3.refs()) == 3 and path.stat().st_size == good
    w3.close()


def test_corruption_followed_by_valid_records_raises(tmp_path: Path, snap: Any) -> None:
    hot_dir = tmp_path / "hot"
    w = HotStaging(hot_dir, writable=True)
    refs = [w.append(snap(k)) for k in range(4)]
    w.close()
    path = hot_dir / refs[0].file
    data = bytearray(path.read_bytes())
    data[refs[1].offset - HEADER_LEN] ^= 0xFF                          # smash record 1's header magic
    path.write_bytes(bytes(data))
    r = HotStaging(hot_dir)
    assert len(r.refs()) == 1 and refs[0].file in r.problems
    with pytest.raises(HotStagingError, match="followed by valid data"):
        HotStaging(hot_dir, writable=True)
    assert path.stat().st_size == len(data)                            # nothing was truncated


def test_redelivered_block_last_record_wins(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    rec = Recorder(tmp_path / "data" / "hot", lake, auto_compact=False)
    a, b = snap(5, 0, ReadPlan.HEAD), snap(5, 1, ReadPlan.FULL)
    rec.record(a)
    rec.record(b)
    assert [r.digest for r in rec.hot.latest_refs()] == [b.digest]
    store = LakeSnapshotStore(lake, rec.hot, clock=10**9)
    assert store.at(Block(B0 + 50)) == b and store.by_digest(a.digest) == a
    rec.compact(final=True)
    assert [r.digest for r in lake.snapshot_refs()] == [b.digest]      # only the last-wins version is frozen
    rec.close()
    lake.close()


# ------------------------------------------------------------------------------------------------ compaction
def test_compaction_waits_for_closed_hours_and_the_committed_bound(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    committed = {"block": B0 + 10 * 28}                                 # the journal lags inside hour 0
    rec = Recorder(tmp_path / "data" / "hot", lake, committed_block=lambda: committed["block"])
    hour = 30                                                            # 30 snapshots per chain hour
    for k in range(hour + 1):                                            # hour 0 complete + first record of hour 1
        rec.record(snap(k))
    assert lake.manifest() == []                                         # block B0+290 of hour 0 is not committed yet
    committed["block"] = B0 + 10 * hour
    for k in range(hour + 1, 2 * hour + 1):                              # finishing hour 1 starts hour 2 -> compaction
        rec.record(snap(k))
    chunks = lake.manifest("snap_global")
    assert [(c.first_block, c.last_block) for c in chunks] == [(B0, B0 + 10 * (hour - 1))]
    assert chunks[0].path.endswith("-live.parquet") and rec.last_compact_error is None
    assert len(rec.hot.files()) == 2                                      # the day of hour 0 is still open
    written = rec.compact(upto_block=B0 + 10 * 2 * hour)
    assert [w.tbl for w in written] == ["snap_global", "snap_subnet", "snap_hotkey"]
    assert rec.compact(upto_block=B0 + 10 * 2 * hour) == []              # idempotent
    # day 1 ended at k=59 (hour 1 ends 00:00Z): its file is fully compacted and closed, so it was deleted
    assert rec.hot.files() == [day_file(snap(60).timestamp_ms)]
    store = LakeSnapshotStore(lake, rec.hot, clock=10**12)
    assert [int(s.block) for s in store.window(Block(10**12), 10**13)] == [B0 + 10 * k for k in range(2 * hour + 1)]
    rec.compact(final=True)
    assert rec.hot.files() == [] and len(lake.snapshot_refs()) == 2 * hour + 1
    assert lake.verify(deep=True) == []
    rec.close()
    lake.close()


def test_a_failing_compaction_never_loses_a_record(tmp_path: Path, snap: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    rec = Recorder(tmp_path / "data" / "hot", lake)

    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(lake, "write_snapshots", boom)
    for k in range(32):
        rec.record(snap(k))
    assert rec.last_compact_error == "OSError: disk full"
    assert len(HotStaging(tmp_path / "data" / "hot").latest_refs()) == 32
    monkeypatch.undo()
    rec.compact(final=True)
    assert len(lake.snapshot_refs()) == 32 and rec.hot.files() == []
    rec.close()
    lake.close()


def test_feed_callbacks(tmp_path: Path, snap: Any) -> None:
    lake = Lake(tmp_path / "data" / "lake")
    rec = Recorder(tmp_path / "data" / "hot", lake, auto_compact=False)
    rec.on_snapshot(snap(0))
    asyncio.run(rec.aon_snapshot(snap(1)))                               # fsync in a worker thread
    assert rec(snap(2)) == snap(2).digest
    assert [r.block for r in rec.hot.refs()] == [B0, B0 + 10, B0 + 20]
    rec.close()
    lake.close()


# ------------------------------------------------------------------------------------------------ crash consistency
def test_crash_between_hot_fsync_and_journal_commit_leaves_a_consistent_store(tmp_path: Path, snap: Any) -> None:
    """Section 4.5 / 7.1: the snapshot is durable in hot staging BEFORE the journal commit that references it. A process
    killed in between leaves an unreferenced hot record; recovery re-delivers the block, the new record wins, and every
    journaled digest stays resolvable (before and after compaction)."""
    data = tmp_path / "data"
    snaps = [snap(k) for k in range(6)]
    blob = tmp_path / "snaps.json"
    blob.write_bytes(codec.canonical_bytes(snaps))
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent("""
        import os, sys
        from pathlib import Path
        from taotrader.core import codec
        from taotrader.core.events import HealthObs, SnapshotObserved
        from taotrader.core.state import ChainSnapshot
        from taotrader.core.units import BookId, LogicalTime
        from taotrader.data.journal import SqliteJournal
        from taotrader.data.lake import Lake
        from taotrader.data.recorder import Recorder
        data = Path(sys.argv[1])
        snaps = codec.decode_bytes(list[ChainSnapshot], Path(sys.argv[2]).read_bytes())
        j = SqliteJournal(data / "runs" / "r1" / "journal.sqlite")
        rec = Recorder(data / "hot", Lake(data / "lake"), committed_block=j.head_block)
        for s in snaps[:-1]:
            d = rec.record(s)                                   # hot append + fsync ...
            j.append_batch([(LogicalTime(s.block), BookId(""), SnapshotObserved(
                block=s.block, block_hash=s.block_hash, digest=d, plan=s.plan, ts_ms=s.timestamp_ms,
                health=HealthObs.nominal()))])                  # ... then the commit point
        rec.record(snaps[-1])
        os._exit(17)                                            # killed after the fsync, before the journal commit
    """), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(child), str(data), str(blob)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 17, proc.stderr

    j = SqliteJournal(data / "runs" / "r1" / "journal.sqlite")
    assert j.verify_chain() == 5 and j.head_block() == int(snaps[4].block)
    lake = Lake(data / "lake")
    rec = Recorder(data / "hot", lake, committed_block=j.head_block)
    store = LakeSnapshotStore(lake, rec.hot, clock=10**12)
    journaled = [decode_record(r) for r in j.read()]
    assert all(isinstance(e, SnapshotObserved) for e in journaled)
    for e in journaled:
        assert isinstance(e, SnapshotObserved) and store.by_digest(e.digest).block == e.block
    assert store.at(snaps[5].block) == snaps[5]                          # durable but unreferenced
    # recovery: the source re-delivers block 5 (after a restart the first read is a FULL plan with other content)
    redelivered = snap(5, variant=1)
    assert redelivered.digest != snaps[5].digest
    rec.record(redelivered)
    j.append_batch([(LogicalTime(redelivered.block), BookId(""), obs(redelivered))])
    assert store.at(redelivered.block) == redelivered and store.by_digest(snaps[5].digest) == snaps[5]
    rec.compact(final=True, upto_block=j.head_block())
    cold = LakeSnapshotStore(Lake(data / "lake"), None, clock=10**12)     # the lake alone, as a backtest would see it
    for r in j.read():
        e = decode_record(r)
        assert isinstance(e, SnapshotObserved) and cold.by_digest(e.digest).digest == e.digest
    assert cold.at(redelivered.block) == redelivered
    rec.close()
    lake.close()
    j.close()


# ------------------------------------------------------------------------------------------------ concurrency
_READER = """
import json, sys, time, traceback
from pathlib import Path
from taotrader.core.units import Block
from taotrader.data.lake import Lake
from taotrader.data.recorder import HotStaging
from taotrader.data.store import LakeSnapshotStore
data, done = Path(sys.argv[1]), Path(sys.argv[2])
try:
    lake = Lake(data / "lake")
    store = LakeSnapshotStore(lake, HotStaging(data / "hot"), clock=10**12, cache_size=8)
    seen, iters, max_rows, from_hot = set(), 0, 0, 0
    while True:
        last = done.exists()
        store.refresh()
        blocks = store.selected_blocks()
        assert seen <= set(blocks), sorted(seen - set(blocks))[:5]      # a block never disappears
        seen = set(blocks)
        if blocks:
            s = store.at(Block(blocks[-1]))                             # digest verified on load
            assert int(s.block) == blocks[-1]
            w = store.window(Block(blocks[-1]), 400)
            assert [int(x.block) for x in w] == [b for b in blocks if b > blocks[-1] - 400]
            from_hot += sum(1 for e in store._ix.sel.values() if e.hot is not None)
        con = lake.connect()
        n = con.execute("SELECT count(*) FROM v_global").fetchone()[0]
        m = con.execute("SELECT count(*) FROM v_subnet WHERE block IN (SELECT block FROM v_global)").fetchone()[0]
        con.close()
        assert m == 2 * n
        max_rows = max(max_rows, n)
        iters += 1
        if last:
            break
        time.sleep(0.01)
    print(json.dumps({"iters": iters, "blocks": len(seen), "lake_rows": max_rows, "hot_seen": from_hot}))
except Exception:
    traceback.print_exc()
    sys.exit(1)
"""


def test_concurrent_duckdb_readers_while_the_recorder_appends_and_compacts(tmp_path: Path, snap: Any) -> None:
    """Section 11 WP3 acceptance (Windows): two reader processes query the DuckDB views and the store (lake + hot
    staging) in a loop while this process records 120 snapshots over 4 chain hours across a UTC day, compacting each
    closed hour and deleting the closed day's hot file. No reader error, no block ever disappears, all data ends up
    in the lake."""
    data = tmp_path / "data"
    lake = Lake(data / "lake")
    rec = Recorder(data / "hot", lake)
    rec.record(snap(0))
    script = tmp_path / "reader.py"
    script.write_text(_READER, encoding="utf-8")
    done = tmp_path / "done.flag"
    readers = [subprocess.Popen([sys.executable, str(script), str(data), str(done)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for k in range(1, 120):
            rec.record(snap(k))
            time.sleep(0.02)
        assert rec.last_compact_error is None
        assert len(lake.manifest("snap_global")) >= 3                     # hours 0..2 compacted while readers ran
        rec.compact(final=True)
        for _ in range(50):                                               # a reader may hold a hot file open briefly
            if not rec.hot.files():
                break
            time.sleep(0.1)
            rec.compact(final=True)
        assert rec.hot.files() == []
    finally:
        done.write_text("1")
        outs = [r.communicate(timeout=120) for r in readers]
    for r, (out, err) in zip(readers, outs, strict=True):
        assert r.returncode == 0, out + err
        stats = json.loads(out.strip().splitlines()[-1])
        print("reader stats", stats)
        assert stats["iters"] > 5 and stats["blocks"] == 120 and stats["lake_rows"] == 120
        assert stats["hot_seen"] > 0                                          # it really read hot staging mid-run
    assert lake.verify(deep=True) == []
    rec.close()
    lake.close()
