"""taotrader/data/journal.py - SqliteJournal (append-only, hash-chained, atomic batches) + run-state projections.

Implements core.protocols.Journal on the section 7.2 DDL (schema.JOURNAL_DDL):
- append_batch: BEGIN IMMEDIATE; the head is re-read and checked against the anchor; ordering is asserted; the hash
  chain is extended and every row inserted; the anchor is updated; COMMIT. Any failure (a duplicate idempotency key
  included) rolls the whole batch back and raises. Paper/live use WAL + synchronous=FULL (durable=True, the default
  for files); backtests use ':memory:' or durable=False (synchronous=OFF).
- hash = blake2b-256(prev_hash || f"{block}|{phase}|{sub}|{book}|{kind}|{version}|".encode() || payload), with
  prev_hash = 32 zero bytes for seq 1 (GENESIS_HASH). payload = core.codec canonical JSON of the event; kind and
  version are stored separately and decoded back with core.codec.decode_event (decode_record).
- ordering ("non-decreasing (block, phase)", section 7.2), made precise: blocks never decrease across the whole
  journal, and (block, phase) never decreases within one book's stream (run-level records use book ""). A global
  (block, phase) rule would reject section 4.4's own flow (book 1's OUTBOX records precede book 2's VENUE fills in
  the same block). Recovery-time facts must therefore use the block of the last journaled tick and a phase not
  below the book's last record (e.g. Phase.OUTBOX).
- an event that names a book (CapitalChanged.book, OrderIntended.intent.book, FillReported.fill.book, ...) must be
  journaled under that book.
- UPDATE/DELETE are blocked by triggers; verify_chain() re-derives every hash, so tampering is detected even with
  the triggers dropped, and also reports missing triggers, sequence gaps, broken batches, ordering violations,
  duplicate idempotency keys and an anchor mismatch. `expected_head` (the off-host heartbeat copy of head()) detects a
  whole-file rollback.

Run-state projection (section 7.3 orders_proj in data/runs/<run_id>/state.sqlite): OrdersProjection folds the order
bracket (OrderIntended ... FillReported/OrderFailed/OrderCancelled) using core.orders' FSM; it is rebuildable from
the journal at any time and never raises on orphan or illegal facts (they are counted in `anomalies`).
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final

from ..core import codec
from ..core.events import (
    CarrierFeeSettled,
    FillReported,
    JournalEvent,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from ..core.orders import IllegalTransition, OrderIntent, OrderKind, OrderRecord, OrderState, Urgency
from ..core.protocols import Journal, JournalRecord
from ..core.units import (
    PPM,
    AlphaRao,
    Block,
    BookId,
    Hotkey,
    LogicalTime,
    NetUid,
    OrderId,
    Phase,
    Ppm,
    PriceRao,
    Rao,
    StrategyId,
    SubnetKey,
)
from . import schema

GENESIS_HASH: Final[bytes] = bytes(32)
_PAGE: Final[int] = 1_000
_COLS: Final[str] = "seq, batch, block, phase, sub, book, kind, version, payload, idem, prev_hash, hash"


class JournalError(Exception):
    """A journal write or read was refused."""


class JournalIntegrityError(JournalError):
    """The hash chain, anchor, ordering or append-only protection is broken."""


class JournalOrderError(JournalError):
    """A batch would move logical time backwards (blocks globally, (block, phase) within a book)."""


class DuplicateIdempotencyKey(JournalError):
    """An idempotency key already exists (or repeats inside the batch); the whole batch was rolled back."""

    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate idempotency key {key!r}")
        self.key = key


def record_hash(prev_hash: bytes, block: int, phase: int, sub: int, book: str, kind: str, version: int,
                payload: bytes) -> bytes:
    """blake2b-256(prev_hash || block|phase|sub|book|kind|version| || payload) (section 7.2)."""
    h = hashlib.blake2b(digest_size=32)
    h.update(prev_hash)
    h.update(f"{block}|{phase}|{sub}|{book}|{kind}|{version}|".encode())
    h.update(payload)
    return h.digest()


def decode_record(rec: JournalRecord) -> JournalEvent:
    """The typed event of a journal record (upcasting older versions through core.codec)."""
    return codec.decode_event(rec.kind, rec.version, rec.payload)


def event_book(ev: JournalEvent) -> str | None:
    """The book an event names, if any (None for run-level events)."""
    if isinstance(ev, OrderIntended):
        return str(ev.intent.book)
    if isinstance(ev, FillReported):
        return str(ev.fill.book)
    b = getattr(ev, "book", None)
    return b if isinstance(b, str) else None


def split_sql(script: str) -> list[str]:
    """Complete SQL statements of a script (trigger bodies kept whole), so DDL can run inside one transaction."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                out.append(stmt)
            buf = ""
    if buf.strip() and not all(ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()):
        raise ValueError("incomplete SQL statement at the end of the script")
    return out


def _row_to_record(r: Sequence[Any]) -> JournalRecord:
    seq, batch, block, phase, sub, book, kind, version, payload, idem, prev_hash, h = r
    try:
        ph = Phase(phase)
    except ValueError as e:
        raise JournalIntegrityError(f"seq {seq}: invalid phase {phase}") from e
    return JournalRecord(seq=seq, batch=batch, time=LogicalTime(Block(block), ph, sub), book=BookId(book), kind=kind,
                         version=version, payload=bytes(payload), idem=idem, prev_hash=bytes(prev_hash), hash=bytes(h))


class SqliteJournal:
    """core.protocols.Journal on SQLite (section 7.2). Thread-safe (the Runner commits from a worker thread)."""

    def __init__(self, path: str | Path = ":memory:", *, durable: bool | None = None, readonly: bool = False,
                 busy_timeout_ms: int = 30_000) -> None:
        self.path = str(path)
        memory = self.path == ":memory:"
        self.readonly = readonly
        self.durable = (not memory) if durable is None else durable
        if memory and self.durable:
            raise ValueError("an in-memory journal cannot be durable")
        timeout = busy_timeout_ms / 1000
        if readonly:
            if memory:
                raise ValueError("a read-only journal needs a file")
            if not Path(self.path).is_file():
                raise JournalError(f"{self.path}: no such journal")
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=timeout)
        else:
            if not memory:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=timeout)
        self._lock = threading.RLock()
        if not readonly:
            if self.durable:
                for p in schema.JOURNAL_PRAGMAS_DURABLE:
                    self._db.execute(p)
            else:
                if not memory:
                    self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=OFF")
            self._create_schema()
        elif not self._has_table("journal"):
            raise JournalError(f"{self.path} has no journal table")
        self._head_seq = 0
        self._head_hash = GENESIS_HASH
        self._head_block: int | None = None
        self._last_by_book: dict[str, tuple[int, int]] = {}
        self._load_state()

    # ------------------------------------------------------------------------------------------ setup
    def _has_table(self, name: str) -> bool:
        return self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None

    def _create_schema(self) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if not self._has_table("journal"):
                    for stmt in split_sql(schema.JOURNAL_SCHEMA_SQL):
                        self._db.execute(stmt)
                elif not self._has_table("anchor"):
                    raise JournalIntegrityError("journal table without its anchor table")
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    def _load_state(self) -> None:
        last = self._db.execute("SELECT seq, hash, block FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        if last is None:
            self._head_seq, self._head_hash, self._head_block = 0, GENESIS_HASH, None
            self._last_by_book = {}
            return
        self._head_seq, self._head_hash, self._head_block = int(last[0]), bytes(last[1]), int(last[2])
        rows = self._db.execute("SELECT book, block, phase FROM journal WHERE seq IN "
                                "(SELECT max(seq) FROM journal GROUP BY book)").fetchall()
        self._last_by_book = {str(b): (int(bl), int(ph)) for b, bl, ph in rows}

    def _missing_triggers(self) -> list[str]:
        have = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
        return [t for t in schema.JOURNAL_TRIGGERS if t not in have]

    def _head_locked(self) -> tuple[int, bytes, int | None]:
        """(seq, hash, block) of the last record, checked against the anchor row."""
        last = self._db.execute("SELECT seq, hash, block FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        anchor = self._db.execute("SELECT head_seq, head_hash FROM anchor WHERE id = 1").fetchone()
        if last is None:
            if anchor is not None and anchor[0] not in (0, None):
                raise JournalIntegrityError(f"anchor points at seq {anchor[0]} of an empty journal")
            return 0, GENESIS_HASH, None
        if anchor is None or int(anchor[0]) != int(last[0]) or bytes(anchor[1]) != bytes(last[1]):
            raise JournalIntegrityError(f"anchor {None if anchor is None else anchor[0]} does not match head seq {last[0]}")
        return int(last[0]), bytes(last[1]), int(last[2])

    # ------------------------------------------------------------------------------------------ Journal protocol
    def append_batch(self, items: Sequence[tuple[LogicalTime, BookId, JournalEvent]]) -> list[JournalRecord]:
        """Atomically append one batch; returns its records. Raises JournalOrderError, DuplicateIdempotencyKey,
        JournalIntegrityError or JournalError (nothing is written in every case)."""
        if self.readonly:
            raise JournalError("read-only journal")
        if not items:
            return []
        encoded: list[tuple[LogicalTime, str, str, int, bytes, str | None]] = []
        seen: set[str] = set()
        for t, book, ev in items:
            if not isinstance(t, LogicalTime) or not isinstance(ev, JournalEvent) or not isinstance(book, str):
                raise TypeError("append_batch items are (LogicalTime, BookId, JournalEvent)")
            if "|" in book or int(t.block) < 0 or t.sub < 0:
                raise JournalError(f"invalid record header (book {book!r}, time {t})")
            eb = event_book(ev)
            if eb is not None and eb != book:
                raise JournalError(f"{type(ev).__name__} names book {eb!r} but is journaled under {book!r}")
            kind, version, payload = codec.encode_event(ev)
            idem = ev.idem()
            if idem is not None:
                if idem in seen:
                    raise DuplicateIdempotencyKey(idem)
                seen.add(idem)
            encoded.append((t, str(book), kind, version, payload, idem))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                missing = self._missing_triggers()
                if missing:
                    raise JournalIntegrityError(f"append-only triggers missing: {missing}")
                head_seq, head_hash, head_block = self._head_locked()
                if head_seq != self._head_seq or head_hash != self._head_hash:
                    self._load_state()                      # another connection appended meanwhile
                last_block = head_block
                per_book = dict(self._last_by_book)
                rows: list[tuple[Any, ...]] = []
                records: list[JournalRecord] = []
                seq, prev, batch = head_seq, head_hash, head_seq + 1
                for t, bk, kind, version, payload, idem in encoded:
                    blk, ph = int(t.block), int(t.phase)
                    if last_block is not None and blk < last_block:
                        raise JournalOrderError(f"block {blk} after block {last_block}")
                    pb = per_book.get(bk)
                    if pb is not None and (blk, ph) < pb:
                        raise JournalOrderError(f"book {bk!r}: (block, phase) {(blk, ph)} after {pb}")
                    if idem is not None and self._db.execute("SELECT 1 FROM journal WHERE idem = ?", (idem,)).fetchone():
                        raise DuplicateIdempotencyKey(idem)
                    seq += 1
                    h = record_hash(prev, blk, ph, t.sub, bk, kind, version, payload)
                    rows.append((seq, batch, blk, ph, t.sub, bk, kind, version, payload, idem, prev, h))
                    records.append(JournalRecord(seq=seq, batch=batch, time=t, book=BookId(bk), kind=kind, version=version,
                                                 payload=payload, idem=idem, prev_hash=prev, hash=h))
                    prev, last_block = h, blk
                    per_book[bk] = (blk, ph)
                self._db.executemany(f"INSERT INTO journal ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                self._db.execute("INSERT INTO anchor (id, head_seq, head_hash) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE "
                                 "SET head_seq = excluded.head_seq, head_hash = excluded.head_hash", (seq, prev))
                self._db.execute("COMMIT")
            except sqlite3.IntegrityError as e:
                self._rollback()
                if "idem" in str(e):
                    raise DuplicateIdempotencyKey("<unique constraint>") from e
                raise JournalError(str(e)) from e
            except BaseException:
                self._rollback()
                raise
            self._head_seq, self._head_hash, self._head_block = seq, prev, last_block
            self._last_by_book = per_book
            return records

    def read(self, from_seq: int = 1) -> Iterator[JournalRecord]:
        """Records with seq >= from_seq in order (paged, so appends may interleave with a long read)."""
        nxt = max(1, from_seq)
        while True:
            with self._lock:
                rows = self._db.execute(f"SELECT {_COLS} FROM journal WHERE seq >= ? ORDER BY seq LIMIT ?",
                                        (nxt, _PAGE)).fetchall()
            if not rows:
                return
            for r in rows:
                yield _row_to_record(r)
            nxt = int(rows[-1][0]) + 1

    def read_batches(self, from_seq: int = 1) -> Iterator[list[JournalRecord]]:
        """Whole atomic batches in order, starting with the batch that contains from_seq (recovery folds and
        re-verifies batch by batch, section 4.5)."""
        start = max(1, from_seq)
        with self._lock:
            r = self._db.execute("SELECT batch FROM journal WHERE seq = ?", (start,)).fetchone()
        if r is not None:
            start = int(r[0])
        cur: list[JournalRecord] = []
        for rec in self.read(start):
            if cur and rec.batch != cur[0].batch:
                yield cur
                cur = []
            cur.append(rec)
        if cur:
            yield cur

    def has_idem(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM journal WHERE idem = ?", (key,)).fetchone() is not None

    def head(self) -> tuple[int, bytes]:
        """(seq, hash) of the last record; (0, GENESIS_HASH) when empty. The heartbeat publishes this off-host."""
        with self._lock:
            r = self._db.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, GENESIS_HASH) if r is None else (int(r[0]), bytes(r[1]))

    def head_block(self) -> int | None:
        """Block of the last record (the Recorder's committed-block bound); None when empty."""
        with self._lock:
            r = self._db.execute("SELECT block FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return None if r is None else int(r[0])

    def anchor(self) -> tuple[int, bytes]:
        """The anchor row (head_seq, head_hash), updated in the same transaction as every append."""
        with self._lock:
            r = self._db.execute("SELECT head_seq, head_hash FROM anchor WHERE id = 1").fetchone()
        return (0, GENESIS_HASH) if r is None or r[0] is None else (int(r[0]), bytes(r[1]))

    def verify_chain(self, *, expected_head: tuple[int, bytes] | None = None, deep: bool = False,
                     check_triggers: bool = True) -> int:
        """Re-derive the whole chain; returns the number of records verified, raises JournalIntegrityError.

        expected_head: an off-host copy of head() (heartbeat); the journal must still contain that record with that
        hash (detects whole-file rollback). deep=True also decodes every payload and checks its kind, idempotency key
        and canonical form."""
        with self._lock:
            began = not self._db.in_transaction
            if began:
                self._db.execute("BEGIN")                # one read snapshot for the rows AND the anchor (WAL)
            try:
                return self._verify_locked(expected_head, deep, check_triggers)
            finally:
                if began and self._db.in_transaction:
                    self._db.execute("COMMIT")

    def _verify_locked(self, expected_head: tuple[int, bytes] | None, deep: bool, check_triggers: bool) -> int:
        if check_triggers:
            missing = self._missing_triggers()
            if missing:
                raise JournalIntegrityError(f"append-only triggers missing: {missing}")
        n, prev, batch_id = 0, GENESIS_HASH, 0
        last_block: int | None = None
        per_book: dict[str, tuple[int, int]] = {}
        idems: set[str] = set()
        at_expected: bytes | None = None
        for r in self._db.execute(f"SELECT {_COLS} FROM journal ORDER BY seq"):
            rec = _row_to_record(r)
            if rec.seq != n + 1:
                raise JournalIntegrityError(f"sequence gap: expected seq {n + 1}, found {rec.seq}")
            if rec.batch == rec.seq:
                batch_id = rec.seq
            elif rec.batch != batch_id or batch_id == 0:
                raise JournalIntegrityError(f"seq {rec.seq}: batch {rec.batch} is not the open batch {batch_id}")
            if rec.prev_hash != prev:
                raise JournalIntegrityError(f"seq {rec.seq}: prev_hash does not link to seq {n}")
            blk, ph = int(rec.time.block), int(rec.time.phase)
            h = record_hash(prev, blk, ph, rec.time.sub, rec.book, rec.kind, rec.version, rec.payload)
            if h != rec.hash:
                raise JournalIntegrityError(f"seq {rec.seq}: hash mismatch (record altered)")
            if last_block is not None and blk < last_block:
                raise JournalIntegrityError(f"seq {rec.seq}: block {blk} after {last_block}")
            pb = per_book.get(rec.book)
            if pb is not None and (blk, ph) < pb:
                raise JournalIntegrityError(f"seq {rec.seq}: book {rec.book!r} time {(blk, ph)} after {pb}")
            if rec.idem is not None:
                if rec.idem in idems:
                    raise JournalIntegrityError(f"seq {rec.seq}: duplicate idempotency key {rec.idem!r}")
                idems.add(rec.idem)
            if deep:
                self._deep_check(rec)
            if expected_head is not None and rec.seq == expected_head[0]:
                at_expected = rec.hash
            prev, last_block, n = h, blk, n + 1
            per_book[str(rec.book)] = (blk, ph)
        a = self._db.execute("SELECT head_seq, head_hash FROM anchor WHERE id = 1").fetchone()
        a_seq, a_hash = (0, GENESIS_HASH) if a is None or a[0] is None else (int(a[0]), bytes(a[1]))
        if (a_seq, a_hash) != (n, prev):
            raise JournalIntegrityError(f"anchor (seq {a_seq}) does not match the verified head (seq {n})")
        if expected_head is not None:
            e_seq, e_hash = expected_head
            if e_seq > n:
                raise JournalIntegrityError(f"journal rolled back: expected head seq {e_seq}, journal ends at {n}")
            if e_seq == 0:
                if e_hash != GENESIS_HASH:
                    raise JournalIntegrityError("expected head (0, ...) must carry the genesis hash")
            elif at_expected != e_hash:
                raise JournalIntegrityError(f"record {e_seq} differs from the expected head hash")
        return n

    @staticmethod
    def _deep_check(rec: JournalRecord) -> None:
        try:
            ev = decode_record(rec)
        except (codec.CodecError, ValueError) as e:
            raise JournalIntegrityError(f"seq {rec.seq}: payload does not decode as {rec.kind} v{rec.version}: {e}") from e
        if rec.kind != type(ev).KIND:
            raise JournalIntegrityError(f"seq {rec.seq}: kind {rec.kind} decodes to {type(ev).KIND}")
        if ev.idem() != rec.idem:
            raise JournalIntegrityError(f"seq {rec.seq}: idem column {rec.idem!r} != event key {ev.idem()!r}")
        if rec.version == type(ev).VERSION and codec.canonical_bytes(ev) != rec.payload:
            raise JournalIntegrityError(f"seq {rec.seq}: payload is not in canonical form")
        eb = event_book(ev)
        if eb is not None and eb != rec.book:
            raise JournalIntegrityError(f"seq {rec.seq}: event book {eb!r} journaled under {rec.book!r}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> SqliteJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------------------------------------ run-state projections
def open_run_state(path: str | Path = ":memory:", *, durable: bool = True, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """data/runs/<run_id>/state.sqlite with the per-run section 7.3 tables (run_meta, orders_proj, live_submissions,
    checkpoint, trial). Rebuildable projections; the journal stays the source of truth."""
    p = str(path)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, isolation_level=None, check_same_thread=False, timeout=busy_timeout_ms / 1000)
    if p != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL" if durable else "PRAGMA synchronous=OFF")
    stmts = schema.create_table_statements(schema.STATE_DDL)
    for name in schema.RUN_STATE_TABLES:
        db.execute(schema.if_not_exists(stmts[name]))
    return db


_PROBE_INTENT: Final[OrderIntent] = OrderIntent(
    order_id=OrderId("0" * 24), attempt=0, book=BookId("fsm-probe"), created_block=Block(0), kind=OrderKind.ADD_STAKE_LIMIT,
    key=SubnetKey(NetUid(0), Block(0)), hotkey=Hotkey("0x" + "0" * 64), tao_in=Rao(1), alpha_in=AlphaRao(0),
    full_position=False, limit_price=PriceRao(1), allow_partial=False, shielded=True, valid_until=Block(0),
    expected_out=0, urgency=Urgency.NORMAL, attribution=((StrategyId("fsm-probe"), Ppm(PPM)),), reason="fsm-probe")


def legal_transition(cur: OrderState, new: OrderState) -> bool:
    """The core.orders FSM, asked through OrderRecord.to (no copy of the transition table)."""
    try:
        OrderRecord(_PROBE_INTENT, cur).to(new)
    except IllegalTransition:
        return False
    return True


ORDERS_PROJ_COLS: Final[tuple[str, ...]] = (
    "book", "order_id", "attempt", "state", "kind", "netuid", "reg_at", "hotkey", "tao_in", "alpha_in", "limit_price",
    "urgency", "created_block", "terminal_block", "reason")


class OrdersProjection:
    """orders_proj (section 7.3): one row per (book, order_id, attempt), folded from the journal.

    reason = the intent's reason code until a FAILED/EXPIRED/CANCELLED fact replaces it with the terminal reason
    (FailReason value, ":detail" appended when present; or the cancel reason). terminal_block = the fact's block.
    Orphan facts (unknown order) and transitions the FSM forbids are skipped and counted in `anomalies`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.anomalies = 0
        self._lock = threading.RLock()

    def apply(self, records: Iterable[JournalRecord]) -> int:
        """Fold records (one transaction). Returns the number of anomalies they produced."""
        with self._lock:
            before = self.anomalies
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for rec in records:
                    self._apply_one(rec)
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                self.anomalies = before
                raise
            return self.anomalies - before

    def rebuild(self, journal: Journal) -> int:
        """Drop and re-fold the whole projection from the journal (one transaction). Returns the anomaly count."""
        with self._lock:
            self.anomalies = 0
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM orders_proj")
                for rec in journal.read(1):
                    self._apply_one(rec)
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            return self.anomalies

    def rows(self) -> list[tuple[Any, ...]]:
        with self._lock:
            return self.conn.execute(f"SELECT {', '.join(ORDERS_PROJ_COLS)} FROM orders_proj "
                                     "ORDER BY book, order_id, attempt").fetchall()

    def _apply_one(self, rec: JournalRecord) -> None:
        if rec.kind not in _ORDER_KINDS:
            return
        ev = decode_record(rec)
        if isinstance(ev, OrderIntended):
            i = ev.intent
            exists = self.conn.execute("SELECT 1 FROM orders_proj WHERE book = ? AND order_id = ? AND attempt = ?",
                                       (i.book, i.order_id, i.attempt)).fetchone()
            if exists is not None:
                self.anomalies += 1
                return
            self.conn.execute(
                f"INSERT INTO orders_proj ({', '.join(ORDERS_PROJ_COLS)}) VALUES ({', '.join('?' * len(ORDERS_PROJ_COLS))})",
                (i.book, i.order_id, i.attempt, OrderState.INTENDED.value, i.kind.value, int(i.key.netuid),
                 int(i.key.reg_at), i.hotkey, int(i.tao_in), int(i.alpha_in), int(i.limit_price), int(i.urgency),
                 int(i.created_block), None, i.reason))
        elif isinstance(ev, OrderCancelled):
            self._to(ev.book, ev.order_id, ev.attempt, OrderState.CANCELLED, ev.block, ev.reason)
        elif isinstance(ev, SubmitStarted):
            self._to(ev.book, ev.order_id, ev.attempt, OrderState.SUBMITTING)
        elif isinstance(ev, VenueAck):
            self._to(ev.book, ev.order_id, ev.attempt, OrderState.SUBMITTED)
        elif isinstance(ev, SubmitUnknown):
            self._to(ev.book, ev.order_id, ev.attempt, OrderState.UNKNOWN)
        elif isinstance(ev, FillReported):
            f = ev.fill
            self._to(f.book, f.order_id, f.attempt, OrderState.FILLED, f.block, None, allow_same=True)
        elif isinstance(ev, OrderFailed):
            reason = ev.reason.value + (f":{ev.detail}" if ev.detail else "")
            self._to(ev.book, ev.order_id, ev.attempt, OrderState.EXPIRED if ev.expired else OrderState.FAILED, ev.block,
                     reason)
        elif isinstance(ev, CarrierFeeSettled):
            row = self.conn.execute("SELECT 1 FROM orders_proj WHERE book = ? AND order_id = ? AND attempt = ?",
                                    (ev.book, ev.order_id, ev.attempt)).fetchone()
            if row is None:
                self.anomalies += 1

    def _to(self, book: str, order_id: str, attempt: int, new: OrderState, terminal_block: int | None = None,
            reason: str | None = None, *, allow_same: bool = False) -> None:
        row = self.conn.execute("SELECT state FROM orders_proj WHERE book = ? AND order_id = ? AND attempt = ?",
                                (book, order_id, attempt)).fetchone()
        if row is None:
            self.anomalies += 1
            return
        cur = OrderState(row[0])
        if cur is new and allow_same:
            return                                   # a further leg of an already FILLED order
        if not legal_transition(cur, new):
            self.anomalies += 1
            return
        self.conn.execute("UPDATE orders_proj SET state = ?, terminal_block = coalesce(?, terminal_block), "
                          "reason = coalesce(?, reason) WHERE book = ? AND order_id = ? AND attempt = ?",
                          (new.value, terminal_block, reason, book, order_id, attempt))


_ORDER_KINDS: Final[frozenset[str]] = frozenset(
    c.KIND for c in (OrderIntended, OrderCancelled, SubmitStarted, VenueAck, SubmitUnknown, FillReported, OrderFailed,
                     CarrierFeeSettled))


def _conforms(j: SqliteJournal) -> Journal:
    """Static check (mypy): SqliteJournal satisfies core.protocols.Journal."""
    return j
