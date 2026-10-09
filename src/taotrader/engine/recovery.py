"""taotrader/engine/recovery.py - replay verification, checkpoints, journal batches (WP7; DESIGN.md 4.5, 7.3).

Recovery (driven by Runner.recover):
1. `journal.verify_chain()` (with the heartbeat's expected head and deep decoding when the Runner has them).
2. Drift check: the latest ConfigApplied must carry the current (config, code, prereg) hashes, else ReplayDivergence
   unless `--accept-drift` (the Runner then journals a new ConfigApplied and VERIFY is off for earlier batches).
3. Optional checkpoint: the latest journal seq for which EVERY book has a checkpoint whose blob hashes to its
   state_hash and whose BookSpec equals the current one. The FeatureEngine (a fresh instance from the factory) is
   re-warmed on the journaled snapshots of the window before it and must reproduce the checkpoint's features digest;
   otherwise the full replay runs. Venues always observe the whole journal (they hold no other state).
4. `replay()` folds every later batch. A tick batch (one containing a run-level SnapshotObserved) loads its snapshot by
   block/digest from the store, re-derives the chain events, feeds the FeatureEngine and - in VERIFY mode, i.e. when
   the ConfigApplied in force equals the current hashes - re-runs every book's Engine.decide on the journaled inputs
   and compares the canonical outputs (ReplayDivergence on the first difference).
5. The Runner then journals SubmitUnknown("recovered_submitting") for every SUBMITTING order (one batch), drains the
   venues at the last snapshot, resolves UNKNOWN orders and re-drives the outbox, all at the last journaled block in
   Phase.OUTBOX, and resumes the stream strictly after that block.

Checkpoints live in the per-run state.sqlite `checkpoint` table (section 7.3): (book, seq, state_hash, blob_zstd,
features_digest), blob = zstd(canonical bytes of EngineState), state_hash = blake2b-256 of the same bytes.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Protocol

import zstandard

from ..core import codec
from ..core.errors import LookaheadError, ReplayDivergence
from ..core.events import (
    ChainEvent,
    ChainEventObserved,
    ConfigApplied,
    FillReported,
    JournalEvent,
    SnapshotObserved,
)
from ..core.protocols import ExecutionVenue, FeatureEngine, Journal, JournalRecord, SnapshotStore
from ..core.state import ChainSnapshot
from ..core.units import Block, BookId, LogicalTime, Phase, Ppm
from ..protocol.derive import derive_events
from .engine import Engine, Item
from .reducer import FILLS_WINDOW_BLOCKS, EngineState, state_hash

__all__ = [
    "CHECKPOINT_DDL",
    "BookRuntime",
    "Checkpoint",
    "CheckpointStore",
    "JournalBatch",
    "MemoryCheckpointStore",
    "RecoveryError",
    "ReplayResult",
    "SqliteCheckpointStore",
    "compare_outputs",
    "decode_state",
    "encode_state",
    "load_snapshot",
    "read_batches",
    "recovery_time",
    "replay",
    "snapshot_digest",
]

CHECKPOINT_DDL: Final[str] = ("CREATE TABLE IF NOT EXISTS checkpoint (book TEXT, seq INTEGER, state_hash TEXT, "
                              "blob_zstd BLOB, features_digest TEXT, PRIMARY KEY (book, seq))")
_DECIDE_PHASES: Final[tuple[Phase, ...]] = (Phase.ACCOUNT, Phase.DECIDE, Phase.EMIT)
_ZSTD_LEVEL: Final[int] = 3


class RecoveryError(Exception):
    """Recovery cannot proceed safely (missing snapshot, corrupt checkpoint, ...)."""


@dataclass
class BookRuntime:
    """One book inside a Runner: its pure Engine, its venue and its current (journal-folded) state."""
    engine: Engine
    venue: ExecutionVenue
    state: EngineState = field(init=False)

    def __post_init__(self) -> None:
        self.state = self.engine.initial_state()

    @property
    def book(self) -> BookId:
        return self.engine.book


# ------------------------------------------------------------------------------------------------- snapshots
def snapshot_digest(snap: ChainSnapshot) -> str:
    """The snapshot's digest (builder-computed), else blake2b-128 of its canonical bytes with digest="" (the WP1/WP3
    convention)."""
    return snap.digest or codec.digest(replace(snap, digest=""))


def load_snapshot(store: SnapshotStore, block: Block, digest: str) -> ChainSnapshot:
    """The journaled snapshot (by block, then by digest when the store supports it); RecoveryError if absent."""
    store.clock = block
    snap: ChainSnapshot | None = None
    try:
        snap = store.at(block)
    except (LookupError, LookaheadError):
        snap = None
    if snap is None or snapshot_digest(snap) != digest:
        by_digest = getattr(store, "by_digest", None)
        if callable(by_digest):
            try:
                cand = by_digest(digest)
                snap = cand if isinstance(cand, ChainSnapshot) else snap
            except (LookupError, LookaheadError):
                pass
    if snap is None or snap.block != block or snapshot_digest(snap) != digest:
        raise RecoveryError(f"snapshot {block} with digest {digest} is not available in the store")
    return snap


# ------------------------------------------------------------------------------------------------- checkpoints
@dataclass(frozen=True, slots=True)
class Checkpoint:
    book: BookId
    seq: int                       # journal head seq the state is folded through
    state_hash: str
    blob: bytes                    # zstd(canonical bytes of EngineState)
    features_digest: str           # FeatureEngine.state_digest() at that point


class CheckpointStore(Protocol):
    def save(self, cp: Checkpoint) -> None: ...
    def load(self, book: BookId, seq: int) -> Checkpoint | None: ...
    def seqs(self, book: BookId) -> list[int]: ...


def encode_state(state: EngineState) -> tuple[str, bytes]:
    """(state_hash, zstd blob) of a state."""
    raw = codec.canonical_bytes(state)
    return state_hash(state), zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(raw)


def decode_state(blob: bytes, expected_hash: str) -> EngineState:
    """Inverse of encode_state; RecoveryError when the blob does not decode or does not hash to `expected_hash`."""
    try:
        raw = zstandard.ZstdDecompressor().decompress(blob)
        state = codec.decode_bytes(EngineState, raw)
    except (zstandard.ZstdError, codec.CodecError, ValueError) as e:
        raise RecoveryError(f"checkpoint blob does not decode: {e}") from e
    if state_hash(state) != expected_hash:
        raise RecoveryError("checkpoint state hash mismatch")
    return state


class MemoryCheckpointStore:
    """In-process checkpoint store (backtests, tests)."""

    def __init__(self) -> None:
        self._cps: dict[tuple[str, int], Checkpoint] = {}

    def save(self, cp: Checkpoint) -> None:
        self._cps[(str(cp.book), cp.seq)] = cp

    def load(self, book: BookId, seq: int) -> Checkpoint | None:
        return self._cps.get((str(book), seq))

    def seqs(self, book: BookId) -> list[int]:
        return sorted(s for b, s in self._cps if b == book)


class SqliteCheckpointStore:
    """The section 7.3 `checkpoint` table on a run's state.sqlite connection (created if absent)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute(CHECKPOINT_DDL)

    def save(self, cp: Checkpoint) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO checkpoint (book, seq, state_hash, blob_zstd, features_digest) "
                              "VALUES (?, ?, ?, ?, ?)", (str(cp.book), cp.seq, cp.state_hash, cp.blob, cp.features_digest))
            if self.conn.in_transaction:
                self.conn.commit()

    def load(self, book: BookId, seq: int) -> Checkpoint | None:
        with self._lock:
            r = self.conn.execute("SELECT state_hash, blob_zstd, features_digest FROM checkpoint WHERE book = ? AND seq = ?",
                                  (str(book), seq)).fetchone()
        if r is None:
            return None
        return Checkpoint(book, seq, str(r[0]), bytes(r[1]), str(r[2] or ""))

    def seqs(self, book: BookId) -> list[int]:
        with self._lock:
            rows = self.conn.execute("SELECT seq FROM checkpoint WHERE book = ? ORDER BY seq", (str(book),)).fetchall()
        return [int(r[0]) for r in rows]


# ------------------------------------------------------------------------------------------------- journal batches
@dataclass(frozen=True, slots=True)
class JournalBatch:
    """One atomic journal batch, decoded."""
    seq: int                                 # seq of its first record (= JournalRecord.batch)
    records: tuple[JournalRecord, ...]
    events: tuple[JournalEvent, ...]

    @property
    def items(self) -> list[Item]:
        return [(r.time, r.book, e) for r, e in zip(self.records, self.events, strict=True)]

    @property
    def last_seq(self) -> int:
        return self.records[-1].seq

    def snapshot(self) -> SnapshotObserved | None:
        for r, e in zip(self.records, self.events, strict=True):
            if r.book == "" and isinstance(e, SnapshotObserved):
                return e
        return None


def read_batches(journal: Journal, from_seq: int = 1) -> Iterator[JournalBatch]:
    """Whole atomic batches in journal order, decoded (core.codec upcasts older versions)."""
    cur: list[JournalRecord] = []
    for rec in journal.read(from_seq):
        if cur and rec.batch != cur[0].batch:
            yield _decode_batch(cur)
            cur = []
        cur.append(rec)
    if cur:
        yield _decode_batch(cur)


def _decode_batch(recs: list[JournalRecord]) -> JournalBatch:
    try:
        events = tuple(codec.decode_event(r.kind, r.version, r.payload) for r in recs)
    except codec.CodecError as e:
        raise RecoveryError(f"journal batch {recs[0].batch} does not decode: {e}") from e
    return JournalBatch(recs[0].batch, tuple(recs), events)


def compare_outputs(expected: Sequence[Item], got: Sequence[Item]) -> str | None:
    """None if both record lists are canonically identical (time, book, kind, version, payload), else the first
    difference."""
    for n, (e, g) in enumerate(zip(expected, got, strict=False)):
        ek, gk = codec.encode_event(e[2]), codec.encode_event(g[2])
        if (e[0], e[1], ek) != (g[0], g[1], gk):
            return (f"record {n}: journal {e[0]} {e[1]!r} {ek[0]} {ek[2][:300]!r} != recomputed {g[0]} {g[1]!r} {gk[0]} "
                    f"{gk[2][:300]!r}")
    if len(expected) != len(got):
        return f"journal has {len(expected)} decision records, recomputed {len(got)}"
    return None


# ------------------------------------------------------------------------------------------------- replay
@dataclass
class ReplayResult:
    records: int = 0
    batches: int = 0
    ticks: int = 0
    verified_ticks: int = 0
    last_block: Block | None = None
    last_snapshot: ChainSnapshot | None = None
    last_phase_by_book: dict[str, tuple[int, int]] = field(default_factory=dict)
    journal_cfg: tuple[str, str, str] | None = None
    drift: bool = False
    checkpoint_seq: int | None = None
    features: FeatureEngine | None = None


@dataclass(frozen=True, slots=True)
class _Scan:
    records: int
    last_cfg: tuple[str, str, str] | None
    ticks: tuple[tuple[int, Block], ...]          # (batch seq, block) of every tick batch


def _scan(journal: Journal) -> _Scan:
    n = 0
    last_cfg: tuple[str, str, str] | None = None
    ticks: list[tuple[int, Block]] = []
    for rec in journal.read(1):
        n += 1
        if rec.kind == ConfigApplied.KIND:
            ev = codec.decode_event(rec.kind, rec.version, rec.payload)
            if isinstance(ev, ConfigApplied):
                last_cfg = (ev.config_hash, ev.code_hash, ev.prereg_hash)
        elif rec.kind == SnapshotObserved.KIND and rec.book == "" and (not ticks or ticks[-1][0] != rec.batch):
            ticks.append((rec.batch, rec.time.block))
    return _Scan(n, last_cfg, tuple(ticks))


def _cfg_of(batch: JournalBatch, cur: tuple[str, str, str] | None) -> tuple[str, str, str] | None:
    for e in batch.events:
        if isinstance(e, ConfigApplied):
            cur = (e.config_hash, e.code_hash, e.prereg_hash)
    return cur


def _chain_events(batch: JournalBatch) -> tuple[ChainEvent, ...]:
    return tuple(e.event for r, e in zip(batch.records, batch.events, strict=True)
                 if r.book == "" and isinstance(e, ChainEventObserved))


def _try_checkpoint(journal: Journal, books: Sequence[BookRuntime], store: SnapshotStore, scan: _Scan,
                    checkpoints: CheckpointStore, features_factory: Callable[[], FeatureEngine], warm_blocks: int
                    ) -> tuple[int, dict[str, EngineState], FeatureEngine, Block] | None:
    """(seq, states, re-warmed features, last tick block) for the newest usable common checkpoint, else None."""
    common: set[int] | None = None
    for rt in books:
        s = set(checkpoints.seqs(rt.book))
        common = s if common is None else common & s
    if not common:
        return None
    seq = max(common)
    states: dict[str, EngineState] = {}
    digests: set[str] = set()
    for rt in books:
        cp = checkpoints.load(rt.book, seq)
        if cp is None:
            return None
        try:
            st = decode_state(cp.blob, cp.state_hash)
        except RecoveryError:
            return None
        if st.spec != rt.engine.spec:
            return None
        states[str(rt.book)] = st
        digests.add(cp.features_digest)
    if len(digests) != 1:                                    # every book was checkpointed at the same feature state
        return None
    (digest,) = digests
    ticks = [(s, b) for s, b in scan.ticks if s <= seq]
    if not ticks:
        return None
    last_block = ticks[-1][1]
    lo = int(last_block) - warm_blocks
    features = features_factory()
    fills: list[int] = []
    for batch in read_batches(journal):
        if batch.seq > seq:
            break
        obs = batch.snapshot()
        if obs is not None and int(obs.block) > lo:
            snap = load_snapshot(store, obs.block, obs.digest)
            b = int(obs.block)
            own = frozenset(Block(x) for x in fills if x > b - FILLS_WINDOW_BLOCKS)
            features.update(snap, _chain_events(batch), own_fill_blocks=own)
        for e in batch.events:
            if isinstance(e, FillReported):
                fills.append(int(e.fill.block))
    if features.state_digest() != digest:
        return None
    return seq, states, features, last_block


def replay(journal: Journal, books: Sequence[BookRuntime], features: FeatureEngine, store: SnapshotStore, *,
           current_cfg: tuple[str, str, str], accept_drift: bool, large_flow_frac_ppm: Ppm,
           fold: Callable[[Sequence[Item]], None], own_fill_blocks: Callable[[Block], frozenset[Block]],
           checkpoints: CheckpointStore | None = None, features_factory: Callable[[], FeatureEngine] | None = None,
           feature_warm_blocks: int = 230_400) -> ReplayResult:
    """Fold (and verify) the whole journal into `books` and `features`. See the module docstring."""
    scan = _scan(journal)
    res = ReplayResult(records=scan.records, journal_cfg=scan.last_cfg, features=features)
    if scan.records == 0:
        return res
    res.drift = scan.last_cfg != current_cfg
    if res.drift and not accept_drift:
        raise ReplayDivergence(f"the journal was written under ConfigApplied {scan.last_cfg}, the current run is "
                               f"{current_cfg}: resuming with changed code or config needs --accept-drift")
    cp = None
    if checkpoints is not None and features_factory is not None and books:
        cp = _try_checkpoint(journal, books, store, scan, checkpoints, features_factory, feature_warm_blocks)
    cp_seq = 0
    cp_tick_seq = -1
    prev: ChainSnapshot | None = None
    if cp is not None:
        cp_seq, states, features, _ = cp
        for rt in books:
            rt.state = states[str(rt.book)]
        res.checkpoint_seq, res.features = cp_seq, features
        cp_tick_seq = max(s for s, _ in scan.ticks if s <= cp_seq)
    cfg_in_force: tuple[str, str, str] | None = None
    for batch in read_batches(journal):
        res.batches += 1
        for r in batch.records:
            res.last_phase_by_book[str(r.book)] = (int(r.time.block), int(r.time.phase))
        res.last_block = batch.records[-1].time.block
        cfg_in_force = _cfg_of(batch, cfg_in_force)
        obs = batch.snapshot()
        if batch.seq <= cp_seq:                              # covered by the checkpoint: venues only
            for _, _, e in batch.items:
                for rt in books:
                    rt.venue.observe(e)
            if obs is not None:
                res.ticks += 1
                if batch.seq == cp_tick_seq:
                    prev = load_snapshot(store, obs.block, obs.digest)
            continue
        if obs is None:
            fold(batch.items)
            continue
        res.ticks += 1
        snap = load_snapshot(store, obs.block, obs.digest)
        journaled = _chain_events(batch)
        verify = cfg_in_force == current_cfg
        if verify:
            derived = derive_events(prev, snap, large_flow_frac_ppm)
            if derived != journaled:
                raise ReplayDivergence(f"block {snap.block}: derive_events differs from the journaled chain events")
        frame = features.update(snap, journaled, own_fill_blocks=own_fill_blocks(snap.block))
        if verify:
            run_inputs = [(t, bk, e) for t, bk, e in batch.items if bk == ""]
            for rt in books:
                inputs = run_inputs + [(t, bk, e) for t, bk, e in batch.items if bk == rt.book and t.phase is Phase.INGEST]
                expected = [(t, bk, e) for t, bk, e in batch.items if bk == rt.book and t.phase in _DECIDE_PHASES]
                view = rt.venue.mark_to(snap)
                got = rt.engine.decide(rt.state, snap, prev, journaled, frame, view, obs.health, inputs=inputs, store=store)
                diff = compare_outputs(expected, got)
                if diff is not None:
                    raise ReplayDivergence(f"block {snap.block} book {rt.book!r}: {diff}")
            res.verified_ticks += 1
        fold(batch.items)
        prev = snap
    res.last_snapshot = prev
    return res


def recovery_time(res: ReplayResult, book: BookId, phase: Phase) -> LogicalTime:
    """LogicalTime for a recovery-time fact of `book`: the last journaled block, at a phase not below the book's last
    record (the journal requires non-decreasing (block, phase) per book)."""
    assert res.last_block is not None
    last = res.last_phase_by_book.get(str(book))
    ph = phase
    if last is not None and last[0] == int(res.last_block) and last[1] > int(ph):
        ph = Phase(last[1])
    return LogicalTime(res.last_block, ph, 0)

