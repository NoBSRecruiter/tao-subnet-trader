"""taotrader/ops/health.py - the status.json heartbeat (WP12; DESIGN.md sections 4.7, 7.2, 11 WP12, 12.5).

`data/runs/<run_id>/status.json` is rewritten atomically (temp file + os.replace) after every tick, at most every
`min_interval_s`. It carries what an operator (and `taotrader doctor`) needs:

    {"schema": 1, "run_id", "mode", "pid", "host", "ts_unix", "started_unix", "ticks",
     "block", "block_hash", "lags": {"finality_lag_blocks", "secs_since_block", "head_lag_blocks", "feed_gap_blocks",
                                     "healthy_endpoints"},
     "journal": {"seq", "head_hash", "head_block"},
     "books": {"<book>": {"mode", "halted", "exits_only", "open_orders", "nav_rao", "cash_rao", "fee_float_rao",
                          "positions", "orphans", "breaches"}},
     "recorder": {"last_compact_error"}, "feed": {"skipped_blocks", "failed_blocks", "stalled"}, "note"}

The journal head (seq, hash) is the off-host heartbeat copy that recovery feeds back as `expected_head`
(SqliteJournal.verify_chain: the journal must still contain that record with that hash - a whole-file rollback is
detected). `expected_head_from(status)` parses it back; a status file that is missing, unreadable or for another run
gives None (recovery then verifies the chain without it).

NAV in the status file is an operator figure (cash + fee float + positions at the book's view spot, integer rao); it
is never a decision input. Wall-clock fields are metadata.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "STATUS_FILE", "STATUS_SCHEMA", "BookStatus", "HealthWriter", "Status", "expected_head_from", "read_status",
    "status_age_s", "write_json_atomic",
]

STATUS_FILE: Final[str] = "status.json"
STATUS_SCHEMA: Final[int] = 1


@dataclass(frozen=True, slots=True)
class BookStatus:
    mode: str
    halted: bool
    exits_only: bool
    open_orders: int
    nav_rao: int
    cash_rao: int
    fee_float_rao: int
    positions: int
    orphans: int
    breaches: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "halted": self.halted, "exits_only": self.exits_only, "open_orders": self.open_orders,
                "nav_rao": self.nav_rao, "cash_rao": self.cash_rao, "fee_float_rao": self.fee_float_rao,
                "positions": self.positions, "orphans": self.orphans, "breaches": list(self.breaches)}


@dataclass(frozen=True, slots=True)
class Status:
    run_id: str
    mode: str
    ticks: int
    block: int | None
    block_hash: str
    lags: Mapping[str, int]
    journal_seq: int
    journal_head_hash: str            # hex
    journal_head_block: int | None
    books: Mapping[str, BookStatus]
    recorder_error: str | None = None
    feed: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_json(self, *, ts_unix: float, started_unix: float, pid: int, host: str) -> dict[str, Any]:
        return {"schema": STATUS_SCHEMA, "run_id": self.run_id, "mode": self.mode, "pid": pid, "host": host,
                "ts_unix": round(ts_unix, 3), "started_unix": round(started_unix, 3), "ticks": self.ticks,
                "block": self.block, "block_hash": self.block_hash, "lags": dict(sorted(self.lags.items())),
                "journal": {"seq": self.journal_seq, "head_hash": self.journal_head_hash,
                            "head_block": self.journal_head_block},
                "books": {k: v.to_json() for k, v in sorted(self.books.items())},
                "recorder": {"last_compact_error": self.recorder_error}, "feed": dict(self.feed), "note": self.note}


def write_json_atomic(path: str | Path, doc: Mapping[str, Any]) -> None:
    """Write JSON to a temp file in the same directory, fsync, then os.replace (atomic on Windows and POSIX)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:          # Windows: a reader holds the target open for a moment
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class HealthWriter:
    """Rate-limited atomic writer of status.json. `write(status, force=False)` returns True when the file was written."""

    def __init__(self, path: str | Path, *, min_interval_s: float = 5.0, clock: Callable[[], float] = time.time,
                 pid: int | None = None, host: str | None = None) -> None:
        self.path = Path(path)
        self.min_interval_s = min_interval_s
        self.clock = clock
        self.pid = os.getpid() if pid is None else pid
        self.host = socket.gethostname() if host is None else host
        self.started = clock()
        self.last_write: float | None = None
        self.writes = 0
        self.errors = 0

    def write(self, status: Status, *, force: bool = False) -> bool:
        now = self.clock()
        if not force and self.last_write is not None and now - self.last_write < self.min_interval_s:
            return False
        doc = status.to_json(ts_unix=now, started_unix=self.started, pid=self.pid, host=self.host)
        try:
            write_json_atomic(self.path, doc)
        except OSError:
            self.errors += 1
            return False
        self.last_write = now
        self.writes += 1
        return True


def read_status(path: str | Path) -> dict[str, Any] | None:
    """The parsed status.json, or None if missing/unreadable/not a schema-1 object."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != STATUS_SCHEMA:
        return None
    return doc


def expected_head_from(doc: Mapping[str, Any] | None, *, run_id: str | None = None) -> tuple[int, bytes] | None:
    """(seq, hash bytes) of the heartbeat's journal head, for Runner(expected_head=...). None when unusable."""
    if doc is None or (run_id is not None and doc.get("run_id") != run_id):
        return None
    j = doc.get("journal")
    if not isinstance(j, Mapping):
        return None
    seq, h = j.get("seq"), j.get("head_hash")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 or not isinstance(h, str):
        return None
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    return (seq, raw) if len(raw) == 32 else None


def status_age_s(doc: Mapping[str, Any] | None, now_unix: float) -> float | None:
    """Seconds since the heartbeat was written (None without a usable status)."""
    if doc is None:
        return None
    ts = doc.get("ts_unix")
    return None if not isinstance(ts, int | float) else max(0.0, now_unix - float(ts))
