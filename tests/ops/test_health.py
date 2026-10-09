"""ops.health: the atomic, rate-limited status.json heartbeat and the expected_head round trip into the journal's
rollback check (SqliteJournal.verify_chain(expected_head=...))."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from taotrader.core.events import ConfigApplied
from taotrader.core.units import Block, BookId, LogicalTime, Phase
from taotrader.data.journal import JournalIntegrityError, SqliteJournal
from taotrader.ops.health import (
    STATUS_FILE,
    BookStatus,
    HealthWriter,
    Status,
    expected_head_from,
    read_status,
    status_age_s,
    write_json_atomic,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _status(seq: int, h: bytes, block: int = 9_240_000, run_id: str = "paper-main") -> Status:
    bs = BookStatus(mode="NORMAL", halted=False, exits_only=False, open_orders=1, nav_rao=12 * 10**9, cash_rao=10**10,
                    fee_float_rao=2 * 10**9, positions=0, orphans=0)
    return Status(run_id=run_id, mode="paper", ticks=7, block=block, block_hash="0x" + "ab" * 32,
                  lags={"finality_lag_blocks": 2, "secs_since_block": 12, "head_lag_blocks": 0, "feed_gap_blocks": 0,
                        "healthy_endpoints": 4},
                  journal_seq=seq, journal_head_hash=h.hex(), journal_head_block=block, books={"paper-carry": bs})


def _journal(path: Path, n: int) -> SqliteJournal:
    j = SqliteJournal(path)
    for i in range(n):
        b = Block(9_240_000 + i)
        j.append_batch([(LogicalTime(b, Phase.INGEST, 0), BookId(""), ConfigApplied(b, f"c{i}", "code", "prereg"))])
    return j


def test_status_document_has_every_documented_field(tmp_path: Path) -> None:
    clk = Clock()
    hw = HealthWriter(tmp_path / STATUS_FILE, clock=clk, pid=4242, host="h")
    assert hw.write(_status(5, bytes(range(32))))
    doc = read_status(tmp_path / STATUS_FILE)
    assert doc is not None
    assert set(doc) >= {"schema", "run_id", "mode", "pid", "host", "ts_unix", "started_unix", "ticks", "block",
                        "block_hash", "lags", "journal", "books", "recorder", "feed", "note"}
    assert doc["journal"] == {"seq": 5, "head_hash": bytes(range(32)).hex(), "head_block": 9_240_000}
    assert doc["books"]["paper-carry"]["nav_rao"] == 12 * 10**9 and doc["books"]["paper-carry"]["open_orders"] == 1
    assert doc["lags"]["finality_lag_blocks"] == 2 and doc["pid"] == 4242
    assert status_age_s(doc, clk.t + 30) == pytest.approx(30.0)


def test_writes_are_rate_limited_unless_forced(tmp_path: Path) -> None:
    clk = Clock()
    hw = HealthWriter(tmp_path / STATUS_FILE, min_interval_s=5.0, clock=clk)
    assert hw.write(_status(1, bytes(32)))
    clk.t += 1
    assert not hw.write(_status(2, bytes(32)))
    assert hw.write(_status(3, bytes(32)), force=True)
    clk.t += 5
    assert hw.write(_status(4, bytes(32)))
    assert hw.writes == 3
    doc = read_status(tmp_path / STATUS_FILE)
    assert doc is not None and doc["journal"]["seq"] == 4
    assert not list(tmp_path.glob(".status.json.*"))                 # no temp files left behind


def test_heartbeat_head_feeds_the_journal_rollback_check(tmp_path: Path) -> None:
    j = _journal(tmp_path / "journal.sqlite", 4)
    seq, h = j.head()
    HealthWriter(tmp_path / STATUS_FILE).write(_status(seq, h))
    exp = expected_head_from(read_status(tmp_path / STATUS_FILE), run_id="paper-main")
    assert exp == (seq, h)
    assert j.verify_chain(expected_head=exp) == 4
    j.append_batch([(LogicalTime(Block(9_240_010), Phase.INGEST, 0), BookId(""),
                     ConfigApplied(Block(9_240_010), "c", "code", "prereg"))])
    assert j.verify_chain(expected_head=exp) == 5                    # a lagging heartbeat is fine
    j.close()
    rolled = _journal(tmp_path / "rolled.sqlite", 2)                 # a journal that lost records since the heartbeat
    with pytest.raises(JournalIntegrityError, match="rolled back"):
        rolled.verify_chain(expected_head=exp)
    rolled.close()


def test_unusable_status_gives_no_expected_head(tmp_path: Path) -> None:
    p = tmp_path / STATUS_FILE
    assert expected_head_from(read_status(p)) is None                 # missing
    p.write_text("{not json", encoding="utf-8")
    assert read_status(p) is None
    write_json_atomic(p, {"schema": 1, "run_id": "other", "journal": {"seq": 3, "head_hash": "00" * 32}})
    assert expected_head_from(read_status(p), run_id="paper-main") is None   # another run's heartbeat
    assert expected_head_from(read_status(p)) == (3, bytes(32))
    for bad in ({"seq": -1, "head_hash": "00" * 32}, {"seq": 3, "head_hash": "zz"}, {"seq": True, "head_hash": "00" * 32},
                {"seq": 3, "head_hash": "00" * 16}):
        write_json_atomic(p, {"schema": 1, "journal": bad})
        assert expected_head_from(read_status(p)) is None
    write_json_atomic(p, {"schema": 2})
    assert read_status(p) is None


def test_atomic_write_replaces_whole_documents(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "x.json"
    for i in range(20):
        write_json_atomic(p, {"i": i, "pad": "x" * (i * 100)})
        assert json.loads(p.read_text(encoding="utf-8"))["i"] == i
