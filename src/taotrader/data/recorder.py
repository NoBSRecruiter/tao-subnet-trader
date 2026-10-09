"""taotrader/data/recorder.py - live hot staging (JSONL.zst, fsync per snapshot) and hourly Parquet compaction.

DESIGN.md section 7.1 "Live hot staging": every processed snapshot is appended to data/hot/<yyyymmdd>.jsonl.zst
and fsynced BEFORE the journal commit that references its digest; it is compacted to Parquet hourly, and a hot file
is deleted only after the manifest rows covering it have committed.

File format (a valid multi-frame .zst: `zstd -d` prints the JSONL). Each record is
  1. a zstd *skippable* frame (magic 0x184D2A50, ignored by every zstd decoder) carrying a 48-byte index header
     <4s tag "TTH1"><u32 data length><u64 block><i64 timestamp_ms><u32 quality OR><u32 crc32 of the data frame>
     <16 bytes digest>, so readers index a file by seeking from header to header without decompressing;
  2. one zstd data frame (with content checksum) holding core.codec.canonical_bytes(snapshot) + b"\\n".
The UTC day of the snapshot's chain timestamp (never the wall clock) names the file.

Crash consistency: the writer issues one write() per record, then flush + os.fsync. A crash can only tear the last
record; on (re)open the writer truncates a torn tail (a bad record followed by a valid one is real corruption and
raises HotStagingError instead). Readers in other processes never truncate: they stop at an incomplete tail and pick
it up on the next refresh(). A block re-delivered after a crash is appended again; the LAST record of a block wins
(only uncommitted records are ever re-delivered), and every record stays resolvable by digest until compaction.

Compaction groups the last-wins snapshots by UTC hour of their timestamp. An hour is compacted (one snapshot-chunk
triple, series "live") once a later hour has been recorded (or final=True) and, when a committed-block bound is
given, only if every block in it is <= that bound (so nothing the journal may still re-deliver is frozen into
Parquet). A hot file is deleted once its day is closed and the last-wins version of every block in it is in the lake.
On Windows a reader holding the file open makes the delete fail; it is retried at the next compaction.

Wiring (WP1/WP7/WP12): `LiveChainFeed(store, on_snapshot=recorder.aon_snapshot)` (append + fsync and any compaction
in a worker thread; the feed awaits it before yielding the item), `Recorder(..., committed_block=journal.head_block)`,
and the run's store is `LakeSnapshotStore(lake, recorder.hot)`. Costs (128 subnets): append ~10 ms incl. fsync; a
record this process appended recently is served from memory, otherwise a read decodes it (~30 ms, core.codec).
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

import zstandard

from ..core import codec
from ..core.errors import DecodeError
from ..core.state import ChainSnapshot
from . import schema
from .lake import ChunkInfo, Lake, unlink_with_retry

_LOG = logging.getLogger(__name__)

HOT_SUFFIX: Final[str] = ".jsonl.zst"
HOUR_MS: Final[int] = 3_600_000
SKIPPABLE_MAGIC: Final[int] = 0x184D2A50
_TAG: Final[bytes] = b"TTH1"
_META: Final[struct.Struct] = struct.Struct("<4sIQqII16s")       # 48 bytes
_FRAME: Final[struct.Struct] = struct.Struct("<II")               # skippable frame magic + size
HEADER_LEN: Final[int] = _FRAME.size + _META.size


class HotStagingError(Exception):
    """A hot staging file is corrupt in a way a torn write cannot explain."""


@dataclass(frozen=True, slots=True)
class HotRef:
    block: int
    digest: str
    ts_ms: int
    quality_or: int
    file: str            # file name inside the hot directory
    offset: int          # offset of the zstd data frame
    length: int
    crc: int

    @property
    def order(self) -> tuple[str, int]:
        """Append order (file names are yyyymmdd, so they sort chronologically)."""
        return self.file, self.offset


@dataclass(slots=True)
class _FileState:
    end: int = 0                                   # bytes indexed so far
    refs: list[HotRef] = field(default_factory=list)


def day_file(ts_ms: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts_ms // 1000)) + HOT_SUFFIX


def encode_record(snap: ChainSnapshot, cctx: zstandard.ZstdCompressor) -> tuple[ChainSnapshot, bytes]:
    """(the snapshot with its digest verified/filled, the record bytes: index header frame + zstd data frame)."""
    snap, payload = schema.sealed_snapshot_bytes(snap)                       # payload == core.codec.canonical_bytes(snap)
    frame = cctx.compress(payload + b"\n")
    meta = _META.pack(_TAG, len(frame), int(snap.block), snap.timestamp_ms, schema.quality_or(snap), zlib.crc32(frame),
                      bytes.fromhex(snap.digest))
    return snap, _FRAME.pack(SKIPPABLE_MAGIC, _META.size) + meta + frame


def _parse_header(hdr: bytes) -> tuple[int, int, int, int, int, str] | None:
    """(data_len, block, ts_ms, quality_or, crc, digest) or None if the bytes are not a record header."""
    if len(hdr) < HEADER_LEN:
        return None
    magic, size = _FRAME.unpack_from(hdr, 0)
    if magic != SKIPPABLE_MAGIC or size != _META.size:
        return None
    tag, dlen, block, ts, qor, crc, dg = _META.unpack_from(hdr, _FRAME.size)
    if tag != _TAG:
        return None
    return dlen, block, ts, qor, crc, dg.hex()


class HotStaging:
    """Index and access to data/hot/*.jsonl.zst. writable=True for the single recorder process (it appends and
    repairs torn tails); readers (stores in other processes) use writable=False. Thread-safe."""

    def __init__(self, hot_dir: str | Path, *, writable: bool = False, level: int = 3, recent: int = 512) -> None:
        self.dir = Path(hot_dir).resolve()
        self.writable = writable
        if writable:
            self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._files: dict[str, _FileState] = {}
        self._repaired: set[str] = set()
        self._fh: tuple[str, BinaryIO] | None = None
        self._cctx = zstandard.ZstdCompressor(level=level, write_checksum=True, write_content_size=True)
        self._dctx = zstandard.ZstdDecompressor()
        self.problems: dict[str, str] = {}
        self.version = 0
        self._recent: OrderedDict[str, ChainSnapshot] = OrderedDict()   # digest -> snapshot appended by this instance
        self._recent_max = max(0, recent)
        self.refresh()

    # ------------------------------------------------------------------------------------------ index
    def refresh(self) -> bool:
        """Index new files and new records; forget vanished files. Returns True if anything changed."""
        with self._lock:
            names = sorted(p.name for p in self.dir.glob("*" + HOT_SUFFIX)) if self.dir.is_dir() else []
            changed = False
            for gone in [n for n in self._files if n not in names]:
                del self._files[gone]
                changed = True
            for n in names:
                st = self._files.get(n)
                if st is None:
                    st = self._files[n] = _FileState()
                    changed = True
                if self.writable and n not in self._repaired:
                    self._repair(n)
                    changed = True
                    continue
                if self._scan(n, st):
                    changed = True
            if changed:
                self.version += 1
            return changed

    def _scan(self, name: str, st: _FileState) -> bool:
        """Index complete records after st.end. Returns True if new records were found."""
        path = self.dir / name
        try:
            f = path.open("rb")
        except OSError:                             # deleted, or delete-pending on Windows: forget it next refresh
            return False
        added = False
        with f:
            size = os.fstat(f.fileno()).st_size
            if size < st.end:                       # replaced or truncated underneath us: re-index from scratch
                st.end, st.refs = 0, []
                added = True
            pos = st.end
            while size - pos >= HEADER_LEN:
                f.seek(pos)
                h = _parse_header(f.read(HEADER_LEN))
                if h is None:
                    self.problems[name] = f"unparseable record header at offset {pos}"
                    break
                dlen, block, ts, qor, crc, dg = h
                if pos + HEADER_LEN + dlen > size:
                    break                           # the writer is mid-append (or a torn tail): stop for now
                st.refs.append(HotRef(block, dg, ts, qor, name, pos + HEADER_LEN, dlen, crc))
                pos += HEADER_LEN + dlen
                added = True
            st.end = pos
        return added

    def _repair(self, name: str) -> None:
        """Writer only: index the whole file, CRC-check its last record and truncate a torn tail."""
        path = self.dir / name
        st = self._files[name] = _FileState()
        self._scan(name, st)
        self.problems.pop(name, None)
        size = path.stat().st_size
        if st.refs:
            last = st.refs[-1]
            with path.open("rb") as f:
                f.seek(last.offset)
                ok = zlib.crc32(f.read(last.length)) == last.crc
            if not ok:
                st.refs.pop()
                st.end = last.offset - HEADER_LEN
        if st.end < size:
            with path.open("rb") as f:
                f.seek(st.end)
                tail = f.read()
            if self._has_valid_record(tail[1:]):
                raise HotStagingError(f"{path}: corrupt record at offset {st.end} followed by valid data")
            _LOG.warning("hot staging %s: truncating torn tail of %d bytes at offset %d", name, size - st.end, st.end)
            with path.open("rb+") as f:
                f.truncate(st.end)
                os.fsync(f.fileno())
        self._repaired.add(name)

    @staticmethod
    def _has_valid_record(buf: bytes) -> bool:
        magic = _FRAME.pack(SKIPPABLE_MAGIC, _META.size)
        i = buf.find(magic)
        while i >= 0:
            h = _parse_header(buf[i:i + HEADER_LEN])
            if h is not None:
                dlen, *_rest, crc, _dg = h
                start = i + HEADER_LEN
                if start + dlen <= len(buf) and zlib.crc32(buf[start:start + dlen]) == crc:
                    return True
            i = buf.find(magic, i + 1)
        return False

    def refs(self) -> list[HotRef]:
        """Every indexed record in append order (superseded re-deliveries included)."""
        with self._lock:
            return [r for n in sorted(self._files) for r in self._files[n].refs]

    def latest_refs(self) -> list[HotRef]:
        """One record per block, the last appended one, sorted by block."""
        best: dict[int, HotRef] = {}
        for r in self.refs():
            best[r.block] = r                        # append order: later wins
        return [best[b] for b in sorted(best)]

    def files(self) -> list[str]:
        with self._lock:
            return sorted(self._files)

    # ------------------------------------------------------------------------------------------ write / read
    def append(self, snap: ChainSnapshot) -> HotRef:
        """Append one snapshot and fsync before returning (the journal may reference it afterwards)."""
        if not self.writable:
            raise HotStagingError("read-only hot staging")
        snap, rec = encode_record(snap, self._cctx)
        name = day_file(snap.timestamp_ms)
        with self._lock:
            if name not in self._repaired and (self.dir / name).exists():
                self.refresh()
            fh = self._handle(name)
            offset = os.fstat(fh.fileno()).st_size
            fh.write(rec)
            fh.flush()
            os.fsync(fh.fileno())
            h = _parse_header(rec)
            assert h is not None
            dlen, block, ts, qor, crc, dg = h
            ref = HotRef(block, dg, ts, qor, name, offset + HEADER_LEN, dlen, crc)
            st = self._files.setdefault(name, _FileState())
            if st.end == offset:
                st.refs.append(ref)
                st.end = offset + len(rec)
            else:                                   # index out of step (should not happen): rescan the file
                st.end, st.refs = 0, []
                self._scan(name, st)
            self._repaired.add(name)
            self.version += 1
            if self._recent_max:
                self._recent[ref.digest] = snap
                while len(self._recent) > self._recent_max:
                    self._recent.popitem(last=False)
            return ref

    def _handle(self, name: str) -> BinaryIO:
        if self._fh is not None and self._fh[0] == name:
            return self._fh[1]
        self._close_handle()
        fh = (self.dir / name).open("ab")
        self._fh = (name, fh)
        return fh

    def _close_handle(self) -> None:
        if self._fh is not None:
            self._fh[1].close()
            self._fh = None

    def read(self, ref: HotRef, *, verify: bool = True) -> ChainSnapshot:
        """Decode one record. FileNotFoundError if its file was compacted away meanwhile (callers refresh). Records
        this instance appended recently are served from memory (the same immutable object that was written)."""
        with self._lock:
            mine = self._recent.get(ref.digest)
        if mine is not None and int(mine.block) == ref.block:
            return mine
        with (self.dir / ref.file).open("rb") as f:
            f.seek(ref.offset)
            data = f.read(ref.length)
        if len(data) != ref.length or zlib.crc32(data) != ref.crc:
            raise DecodeError(f"hot record {ref.file}@{ref.offset} (block {ref.block}): CRC mismatch")
        try:
            raw = self._dctx.decompress(data)
            snap = codec.decode_bytes(ChainSnapshot, raw)
        except (zstandard.ZstdError, ValueError) as e:
            raise DecodeError(f"hot record {ref.file}@{ref.offset} (block {ref.block}): {e}") from e
        if int(snap.block) != ref.block or snap.digest != ref.digest:
            raise DecodeError(f"hot record {ref.file}@{ref.offset}: header does not match its snapshot")
        if verify and schema.snapshot_digest(snap) != snap.digest:
            raise DecodeError(f"hot record {ref.file}@{ref.offset} (block {ref.block}): digest mismatch")
        return snap

    def remove(self, name: str) -> bool:
        """Delete a hot file (writer). False if Windows still has it open elsewhere (retried at the next compaction)."""
        with self._lock:
            if self._fh is not None and self._fh[0] == name:
                self._close_handle()
            ok = unlink_with_retry(self.dir / name)
            if ok:
                self._files.pop(name, None)
                self._repaired.discard(name)
                self.version += 1
            return ok

    def close(self) -> None:
        with self._lock:
            self._close_handle()


class Recorder:
    """The live recorder: LiveChainFeed's on_snapshot callback (`recorder.record` or the instance itself).

    record() returns only after the snapshot is durable in hot staging, so the Runner may then journal its digest.
    committed_block (e.g. SqliteJournal.head_block) bounds automatic compaction; without it every closed hour is
    treated as final (pure data recording without a journal)."""

    def __init__(self, hot_dir: str | Path, lake: Lake, *, series: str = "live",
                 decoder_version: int = schema.DEFAULT_DECODER_VERSION,
                 committed_block: Callable[[], int | None] | None = None, auto_compact: bool = True,
                 hot: HotStaging | None = None) -> None:
        self.hot = hot if hot is not None else HotStaging(hot_dir, writable=True)
        if not self.hot.writable:
            raise ValueError("the recorder needs a writable HotStaging")
        self.lake = lake
        self.series = series
        self.decoder_version = decoder_version
        self.committed_block = committed_block
        self.auto_compact = auto_compact
        self.last_compact_error: str | None = None
        self._lock = threading.RLock()                 # record() and compact() never interleave (any thread)
        latest = self.hot.latest_refs()
        self._last_hour: int | None = max((r.ts_ms // HOUR_MS for r in latest), default=None)

    def __call__(self, snap: ChainSnapshot) -> str:
        return self.record(snap)

    def on_snapshot(self, snap: ChainSnapshot) -> None:
        """LiveChainFeed's `on_snapshot` callback (synchronous: blocks the caller for the append + fsync)."""
        self.record(snap)

    async def aon_snapshot(self, snap: ChainSnapshot) -> None:
        """Awaitable `on_snapshot` callback: append + fsync (and any hourly compaction) run in a worker thread, so the
        event loop is never blocked. LiveChainFeed awaits it before yielding the item."""
        await asyncio.to_thread(self.record, snap)

    def record(self, snap: ChainSnapshot) -> str:
        """Append + fsync; returns the snapshot digest. Starting a new hour triggers compaction of closed hours;
        a compaction failure is logged and kept in last_compact_error (the record itself is already durable)."""
        with self._lock:
            return self._record(snap)

    def _record(self, snap: ChainSnapshot) -> str:
        ref = self.hot.append(snap)
        hour = ref.ts_ms // HOUR_MS
        if self.auto_compact and self._last_hour is not None and hour > self._last_hour:
            try:
                bound = self.committed_block() if self.committed_block is not None else None
                self.compact(upto_block=bound)
                self.last_compact_error = None
            except Exception as e:                  # recording must go on; compaction retries next hour
                self.last_compact_error = f"{type(e).__name__}: {e}"
                _LOG.exception("hot staging compaction failed")
        self._last_hour = hour if self._last_hour is None else max(self._last_hour, hour)
        return ref.digest

    def compact(self, *, upto_block: int | None = None, final: bool = False) -> list[ChunkInfo]:
        """Compact closed hours to Parquet (series self.series) and delete fully compacted, closed-day hot files.
        final=True also closes the current hour and day (end of run). Returns the chunk infos written."""
        with self._lock:
            return self._compact(upto_block, final)

    def _compact(self, upto_block: int | None, final: bool) -> list[ChunkInfo]:
        self.hot.refresh()
        self.lake.refresh()
        latest = self.hot.latest_refs()
        if not latest:
            return []
        max_hour = max(r.ts_ms // HOUR_MS for r in latest)
        buckets: dict[int, list[HotRef]] = {}
        for r in latest:
            buckets.setdefault(r.ts_ms // HOUR_MS, []).append(r)
        in_lake = {r.digest for r in self.lake.snapshot_refs()}
        written: list[ChunkInfo] = []
        for hour in sorted(buckets):
            refs = buckets[hour]
            if (not final and hour >= max_hour) or (upto_block is not None and refs[-1].block > upto_block):
                continue
            if all(r.digest in in_lake for r in refs):
                continue
            snaps = [self.hot.read(r) for r in refs]
            written.extend(self.lake.write_snapshots(snaps, series=self.series, decoder_version=self.decoder_version))
        in_lake = {r.digest for r in self.lake.snapshot_refs()}
        latest_digest = {r.block: r.digest for r in latest}
        files = self.hot.files()
        for name in files:
            if not final and name == files[-1]:
                continue                            # the current (open) day
            blocks = {r.block for r in self.hot.refs() if r.file == name}
            if all(latest_digest[b] in in_lake for b in blocks):
                self.hot.remove(name)
        return written

    def close(self) -> None:
        self.hot.close()
