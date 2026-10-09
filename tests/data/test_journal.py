"""WP3 journal tests (DESIGN.md sections 4.4, 4.5, 7.2, 7.3; section 11 WP3 acceptance).

Acceptance covered here: atomic batch rollback on a duplicate idem; non-monotone blocks rejected; UPDATE/DELETE
blocked; tampering detected with triggers dropped; plus the hash-chain formula, anchor, verify_chain (gaps, batches,
forged re-hash + heartbeat, whole-file rollback), crash consistency (a process killed inside and after the commit),
concurrent writers/readers across processes on Windows, and the orders_proj projection rebuild.
"""
from __future__ import annotations

import dataclasses
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.events import (
    REGISTRY,
    CapitalChanged,
    CarrierFeeSettled,
    ChainEvent,
    ChainEventKind,
    ChainEventObserved,
    ConfigApplied,
    DecisionTrace,
    DeregSettled,
    FillReported,
    HealthObs,
    JournalEvent,
    ModeChanged,
    ModelDriftObserved,
    OperatorCommand,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    QuarantineCleared,
    ReconAdjusted,
    SleeveTransfer,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
    YieldAccrued,
)
from taotrader.core.orders import FailReason, Fill, OrderIntent, OrderKind, OrderState, Urgency, make_order_id
from taotrader.core.signals import RiskAction, Signal, SignalKind
from taotrader.core.state import ReadPlan
from taotrader.core.units import (
    PPM,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    LogicalTime,
    Mode,
    NetUid,
    OrderId,
    Phase,
    PositionKey,
    Ppm,
    PriceRao,
    Rao,
    StrategyId,
    SubnetKey,
)
from taotrader.data import schema
from taotrader.data.journal import (
    GENESIS_HASH,
    DuplicateIdempotencyKey,
    JournalError,
    JournalIntegrityError,
    JournalOrderError,
    OrdersProjection,
    SqliteJournal,
    decode_record,
    open_run_state,
    record_hash,
)

B0 = 9_240_000
BOOK = BookId("carry")
KEY = SubnetKey(NetUid(92), Block(8_355_590))
HK = Hotkey("0x" + "ab" * 32)


# ------------------------------------------------------------------------------------------------ event samples
def snap_obs(block: int) -> SnapshotObserved:
    return SnapshotObserved(block=Block(block), block_hash=BlockHash("0x" + f"{block:064x}"), digest=f"{block:032x}",
                            plan=ReadPlan.FULL, ts_ms=1_759_900_000_000 + block, health=HealthObs.nominal())


def intent(block: int, attempt: int = 0, book: BookId = BOOK, kind: OrderKind = OrderKind.ADD_STAKE_LIMIT) -> OrderIntent:
    oid = make_order_id("run1", book, Block(block), KEY, HK, kind, attempt)
    buy = kind is OrderKind.ADD_STAKE_LIMIT
    return OrderIntent(order_id=oid, attempt=attempt, book=book, created_block=Block(block), kind=kind, key=KEY,
                       hotkey=HK, tao_in=Rao(10**9 if buy else 0), alpha_in=AlphaRao(0 if buy else 5 * 10**8),
                       full_position=False, limit_price=PriceRao(1_400_000), allow_partial=False, shielded=True,
                       valid_until=Block(block + 5), expected_out=700_000_000, urgency=Urgency.NORMAL,
                       attribution=((StrategyId("carry"), Ppm(PPM)),), reason="carry.entry")


def fill_for(i: OrderIntent, block: int, leg: int = 0) -> Fill:
    return Fill(fill_id=f"{i.order_id}:{i.attempt}:{leg}", order_id=i.order_id, attempt=i.attempt, book=i.book,
                block=Block(block), kind=i.kind, key=i.key, hotkey=i.hotkey, tao=Rao(10**9), alpha=AlphaRao(700_000_000),
                shares=Decimal("699999999.123456789012345678901234567890"), swap_fee=503_547, author_fee_tao=Rao(0),
                tx_fee=Rao(1_028_000), d_pool_tao=10**9, d_pool_alpha=-700_000_000, spot_before=PriceRao(1_398_000),
                shortfall_ppm=Ppm(1_234), complete=True)


def all_event_samples() -> list[tuple[BookId, JournalEvent]]:
    """One instance of every registered journal event kind (book they are journaled under)."""
    i = intent(B0)
    ev = ChainEvent(kind=ChainEventKind.PARAM_CHANGED, block=Block(B0), key=KEY, name="FeeRate", old="33", new="40")
    sig = Signal(strategy=StrategyId("carry"), key=KEY, asof=Block(B0), kind=SignalKind.TARGET, weight_ppm=Ppm(100_000),
                 reasons=("carry.t1",))
    run = BookId("")
    out: list[tuple[BookId, JournalEvent]] = [
        (run, snap_obs(B0)),
        (run, OperatorCommand(block=Block(B0), command="halt", reason="test", nonce="n1")),
        (BOOK, CapitalChanged(book=BOOK, block=Block(B0), cash_delta=10**12, fee_float_delta=10**9, memo="seed")),
        (run, ConfigApplied(block=Block(B0), config_hash="c" * 64, code_hash="d" * 40, prereg_hash="e" * 64)),
        (run, ModelDriftObserved(block=Block(B0), probe="price_all", netuid=None, err_ppm=12)),
        (run, ChainEventObserved(event=ev)),
        (BOOK, YieldAccrued(book=BOOK, key=KEY, hotkey=HK, block=Block(B0), index_before=Decimal("1.000000000000000001"),
                            index_after=Decimal("1.0000000002"), delta_alpha=-1)),
        (BOOK, DeregSettled(book=BOOK, key=KEY, hotkey=HK, block=Block(B0), alpha_value=AlphaRao(5),
                            payout_tao=Rao(3), model="formula")),
        (BOOK, DecisionTrace(book=BOOK, block=Block(B0), strategies_run=(StrategyId("carry"),), signals=(sig,),
                             actions=(RiskAction(rule="liquidity.vcap", key=KEY, action="CLAMP", detail="v=1"),),
                             mode=Mode.NORMAL, memories=((StrategyId("carry"), b'{"x":1}'),), features_digest="f" * 32,
                             n_intents=1, calib_digest="a" * 32, nav_liq=Rao(10**12),
                             sleeve_nav=((StrategyId("carry"), Rao(10**12)),))),
        (BOOK, ModeChanged(book=BOOK, block=Block(B0), mode=Mode.CAUTION, reason="dd")),
        (BOOK, SleeveTransfer(book=BOOK, block=Block(B0), key=KEY, from_strategy=StrategyId("carry"),
                              to_strategy=StrategyId("momentum"), shares=Decimal("12.5"), tao=Rao(17), price=PriceRao(9))),
        (BOOK, OrderIntended(intent=i)),
        (BOOK, OrderCancelled(book=BOOK, order_id=OrderId("c" * 24), attempt=0, block=Block(B0), reason="mode")),
        (BOOK, SubmitStarted(book=BOOK, order_id=i.order_id, attempt=0, delegate="sim0", nonce=None,
                             era_end=Block(B0 + 13))),
        (BOOK, VenueAck(book=BOOK, order_id=i.order_id, attempt=0, submit_block=Block(B0), expected_fill_block=Block(B0 + 5),
                        carrier_hash="", inner_hash="")),
        (BOOK, SubmitUnknown(book=BOOK, order_id=OrderId("d" * 24), attempt=1, detail="recovered_submitting")),
        (BOOK, FillReported(fill=fill_for(i, B0 + 5))),
        (BOOK, OrderFailed(book=BOOK, order_id=OrderId("e" * 24), attempt=0, block=Block(B0), reason=FailReason.SHIELD_MISSED,
                           tx_fee=Rao(98_000), expired=True)),
        (BOOK, CarrierFeeSettled(book=BOOK, order_id=OrderId("e" * 24), attempt=0, block=Block(B0), fee_rao=Rao(0),
                                 outcome="never_included")),
        (BOOK, ReconAdjusted(book=BOOK, block=Block(B0), cash_delta=-5, fee_float_delta=0,
                             share_deltas=((PositionKey(KEY, HK), Decimal("-0.5")),), evidence="chain")),
        (BOOK, QuarantineCleared(book=BOOK, block=Block(B0), reason="operator")),
    ]
    return out


def at(block: int, phase: Phase = Phase.INGEST, sub: int = 0) -> LogicalTime:
    return LogicalTime(Block(block), phase, sub)


def raw_conn(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


# ------------------------------------------------------------------------------------------------ basics
def test_every_event_kind_round_trips_through_the_journal(tmp_path: Path) -> None:
    samples = all_event_samples()
    assert {type(e) for _, e in samples} == set(REGISTRY.values())          # covers the closed vocabulary
    with SqliteJournal(tmp_path / "journal.sqlite") as j:
        recs = j.append_batch([(at(B0, Phase.INGEST, k), b, e) for k, (b, e) in enumerate(samples)])
        assert [r.seq for r in recs] == list(range(1, len(samples) + 1))
        assert {r.batch for r in recs} == {1}
        back = list(j.read())
        assert back == recs
        for (_, e), r in zip(samples, back, strict=True):
            assert decode_record(r) == e
            assert r.kind == type(e).KIND and r.version == type(e).VERSION
            assert r.payload == codec.canonical_bytes(e)
            assert r.idem == e.idem()
        assert j.verify_chain(deep=True) == len(samples)


def test_hash_chain_formula_genesis_and_anchor(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        assert j.head() == (0, GENESIS_HASH) and j.anchor() == (0, GENESIS_HASH) and j.verify_chain() == 0
        r1 = j.append_batch([(at(B0), BookId(""), snap_obs(B0))])[0]
        r2, r3 = j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1)),
                                 (at(B0 + 1, Phase.EMIT, 2), BOOK, ModeChanged(BOOK, Block(B0 + 1), Mode.CAUTION, "x"))])
        assert bytes(32) == GENESIS_HASH and r1.prev_hash == GENESIS_HASH
        for prev, r in ((GENESIS_HASH, r1), (r1.hash, r2), (r2.hash, r3)):
            pre = (f"{int(r.time.block)}|{int(r.time.phase)}|{r.time.sub}|{r.book}|{r.kind}|{r.version}|".encode()
                   + r.payload)
            assert r.hash == hashlib.blake2b(prev + pre, digest_size=32).digest()      # section 7.2, independently
            assert r.hash == record_hash(prev, int(r.time.block), int(r.time.phase), r.time.sub, r.book, r.kind,
                                         r.version, r.payload)
            assert r.prev_hash == prev
        assert (r2.batch, r3.batch) == (2, 2)
        assert j.head() == (3, r3.hash) == j.anchor()
        assert j.head_block() == B0 + 1
        assert j.verify_chain(expected_head=(1, r1.hash)) == 3
        assert j.verify_chain(expected_head=(3, r3.hash)) == 3


def test_pragmas_durable_file_memory_and_fast_backtest(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "durable.sqlite") as j:
        assert j._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert j._db.execute("PRAGMA synchronous").fetchone()[0] == 2          # FULL
    with SqliteJournal(tmp_path / "fast.sqlite", durable=False) as j:
        assert j._db.execute("PRAGMA synchronous").fetchone()[0] == 0          # OFF (backtests)
    with SqliteJournal() as j:                                                 # ':memory:'
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
        assert j.verify_chain() == 1
    with pytest.raises(ValueError):
        SqliteJournal(":memory:", durable=True)


def test_reopen_continues_the_chain(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    with SqliteJournal(p) as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
    with SqliteJournal(p) as j:
        r = j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1))])[0]
        assert r.seq == 2 and r.prev_hash == next(iter(j.read(1))).hash
        assert j.verify_chain() == 2


def test_read_from_seq_read_batches_and_has_idem(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0)),
                        (at(B0, Phase.INGEST, 1), BookId(""), OperatorCommand(Block(B0), "halt", "r", "n1"))])
        j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1))])
        j.append_batch([(at(B0 + 2), BookId(""), snap_obs(B0 + 2)),
                        (at(B0 + 2, Phase.EMIT), BOOK, ModeChanged(BOOK, Block(B0 + 2), Mode.NORMAL, "x")),
                        (at(B0 + 2, Phase.EMIT, 1), BOOK, ModeChanged(BOOK, Block(B0 + 2), Mode.CAUTION, "y"))])
        assert [r.seq for r in j.read(4)] == [4, 5, 6]
        assert [r.seq for r in j.read(0)] == [1, 2, 3, 4, 5, 6]
        assert list(j.read(99)) == []
        assert [[r.seq for r in b] for b in j.read_batches()] == [[1, 2], [3], [4, 5, 6]]
        assert [[r.seq for r in b] for b in j.read_batches(5)] == [[4, 5, 6]]          # whole batch containing seq 5
        assert j.has_idem(f"snap:0x{B0:064x}") and j.has_idem("op:n1") and not j.has_idem("op:n2")


def test_long_read_is_paged_and_survives_interleaved_appends(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite", durable=False) as j:
        j.append_batch([(at(B0 + k), BookId(""), snap_obs(B0 + k)) for k in range(2_500)])
        it = j.read()
        first = [next(it) for _ in range(1_500)]
        j.append_batch([(at(B0 + 2_500), BookId(""), snap_obs(B0 + 2_500))])
        rest = list(it)
        assert [r.seq for r in first + rest] == list(range(1, 2_502))


# ------------------------------------------------------------------------------------------------ atomic batches
def test_duplicate_idem_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
        head = j.head()
        batch = [(at(B0 + 1), BookId(""), snap_obs(B0 + 1)),
                 (at(B0 + 1, Phase.EMIT), BOOK, ModeChanged(BOOK, Block(B0 + 1), Mode.CAUTION, "x")),
                 (at(B0 + 1, Phase.EMIT, 1), BookId(""), snap_obs(B0))]               # idem snap:<B0> already journaled
        with pytest.raises(DuplicateIdempotencyKey) as ei:
            j.append_batch(batch)
        assert ei.value.key == f"snap:0x{B0:064x}"
        assert j.head() == head == j.anchor()
        assert [r.seq for r in j.read()] == [1]
        assert not j.has_idem(f"snap:0x{B0 + 1:064x}")                                # nothing of the batch landed
        # duplicate inside one batch
        with pytest.raises(DuplicateIdempotencyKey):
            j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1)), (at(B0 + 1, Phase.INGEST, 1), BookId(""),
                                                                           snap_obs(B0 + 1))])
        assert j.head() == head
        # the journal is still usable and the chain continues from the old head
        r = j.append_batch(batch[:2])
        assert r[0].seq == 2 and r[0].prev_hash == head[1] and j.verify_chain() == 3


def test_unique_constraint_backstop_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the pre-check missed it (another connection), the UNIQUE(idem) constraint aborts the whole batch."""
    p = tmp_path / "j.sqlite"
    with SqliteJournal(p) as a, SqliteJournal(p) as b:
        a.append_batch([(at(B0), BookId(""), snap_obs(B0))])
        real = b._db.execute

        def no_precheck(sql: str, *args: Any) -> Any:
            if sql.startswith("SELECT 1 FROM journal WHERE idem"):
                return real("SELECT 1 WHERE 0")
            return real(sql, *args)

        monkeypatch.setattr(b, "_db", _ConnProxy(b._db, no_precheck))
        with pytest.raises(DuplicateIdempotencyKey):
            b.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1)), (at(B0 + 1, Phase.INGEST, 1), BookId(""),
                                                                           snap_obs(B0))])
        assert a.head()[0] == 1 and a.verify_chain() == 1


class _ConnProxy:
    def __init__(self, conn: sqlite3.Connection, execute: Any) -> None:
        self._conn = conn
        self.execute = execute

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_empty_batch_and_bad_items(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        assert j.append_batch([]) == []
        with pytest.raises(TypeError):
            j.append_batch([(B0, BookId(""), snap_obs(B0))])  # type: ignore[list-item]
        with pytest.raises(JournalError):
            j.append_batch([(at(B0), BookId("a|b"), snap_obs(B0))])
        assert j.head()[0] == 0


# ------------------------------------------------------------------------------------------------ ordering
def test_non_monotone_blocks_are_rejected(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        j.append_batch([(at(B0 + 10), BookId(""), snap_obs(B0 + 10))])
        with pytest.raises(JournalOrderError):                                   # across batches
            j.append_batch([(at(B0 + 9), BookId(""), snap_obs(B0 + 9))])
        with pytest.raises(JournalOrderError):                                   # inside one batch
            j.append_batch([(at(B0 + 12), BookId(""), snap_obs(B0 + 12)), (at(B0 + 11), BookId(""), snap_obs(B0 + 11))])
        with pytest.raises(JournalOrderError):                                   # another book cannot go back either
            j.append_batch([(at(B0 + 5, Phase.EMIT), BOOK, ModeChanged(BOOK, Block(B0 + 5), Mode.NORMAL, "x"))])
        assert j.head()[0] == 1
        j.append_batch([(at(B0 + 11), BookId(""), snap_obs(B0 + 11))])
        assert j.verify_chain() == 2


def test_phase_order_is_per_book_and_matches_the_section_4_4_flow(tmp_path: Path) -> None:
    """One block of section 4.4: snapshot batch (run INGEST, then each book's ACCOUNT..EMIT), then each book's VENUE
    fills and OUTBOX records. Globally (block, phase) goes back between books; per book it never does."""
    b2 = BookId("momentum")
    i1, i2 = intent(B0, book=BOOK), intent(B0, book=b2)
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        j.append_batch([
            (at(B0), BookId(""), snap_obs(B0)),
            (at(B0, Phase.ACCOUNT), BOOK, YieldAccrued(BOOK, KEY, HK, Block(B0), Decimal(1), Decimal(1), 0)),
            (at(B0, Phase.DECIDE), BOOK, ModeChanged(BOOK, Block(B0), Mode.NORMAL, "x")),
            (at(B0, Phase.EMIT), BOOK, OrderIntended(i1)),
            (at(B0, Phase.ACCOUNT), b2, YieldAccrued(b2, KEY, HK, Block(B0), Decimal(1), Decimal(1), 0)),
            (at(B0, Phase.EMIT), b2, OrderIntended(i2)),
        ])
        j.append_batch([(at(B0, Phase.VENUE), BOOK, FillReported(fill_for(intent(B0 - 5, book=BOOK), B0)))])
        j.append_batch([(at(B0, Phase.OUTBOX), BOOK, SubmitStarted(BOOK, i1.order_id, 0, "sim0", None, Block(B0 + 13)))])
        j.append_batch([(at(B0, Phase.VENUE), b2, FillReported(fill_for(intent(B0 - 5, book=b2), B0)))])   # b2 VENUE after BOOK OUTBOX
        j.append_batch([(at(B0, Phase.OUTBOX), b2, SubmitStarted(b2, i2.order_id, 0, "sim0", None, Block(B0 + 13)))])
        with pytest.raises(JournalOrderError):                                    # BOOK cannot go OUTBOX -> VENUE
            j.append_batch([(at(B0, Phase.VENUE), BOOK, FillReported(fill_for(intent(B0 - 6, book=BOOK), B0)))])
        # recovery facts reuse the last block at a phase not below the book's last record
        j.append_batch([(at(B0, Phase.OUTBOX, 1), BOOK, SubmitUnknown(BOOK, i1.order_id, 0, "recovered_submitting"))])
        assert j.verify_chain(deep=True) == 11


def test_event_must_be_journaled_under_the_book_it_names(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        with pytest.raises(JournalError):
            j.append_batch([(at(B0), BookId("other"), CapitalChanged(BOOK, Block(B0), 1, 0, "m"))])
        with pytest.raises(JournalError):
            j.append_batch([(at(B0, Phase.EMIT), BookId(""), OrderIntended(intent(B0)))])
        with pytest.raises(JournalError):
            j.append_batch([(at(B0, Phase.VENUE), BookId("x"), FillReported(fill_for(intent(B0), B0)))])
        assert j.head()[0] == 0


# ------------------------------------------------------------------------------------------------ append-only + tampering
def _populated(path: Path, n: int = 6) -> list[Any]:
    with SqliteJournal(path) as j:
        recs = []
        for k in range(n):
            recs += j.append_batch([(at(B0 + k), BookId(""), snap_obs(B0 + k)),
                                    (at(B0 + k, Phase.EMIT), BOOK, ModeChanged(BOOK, Block(B0 + k), Mode.NORMAL, f"t{k}"))])
        return recs


def test_update_and_delete_are_blocked_by_triggers(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    _populated(p)
    c = raw_conn(p)
    for sql in ("UPDATE journal SET payload = x'00' WHERE seq = 3", "UPDATE journal SET idem = NULL",
                "DELETE FROM journal WHERE seq = 12", "DELETE FROM journal"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(sql)
    c.close()
    with SqliteJournal(p) as j:
        assert j.verify_chain(deep=True) == 12


def test_tampering_is_detected_with_triggers_dropped(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    _populated(p)
    c = raw_conn(p)
    c.execute("DROP TRIGGER journal_no_update")
    c.execute("DROP TRIGGER journal_no_delete")
    tampered = codec.canonical_bytes(ModeChanged(BOOK, Block(B0 + 2), Mode.FROZEN, "t2"))
    c.execute("UPDATE journal SET payload = ? WHERE seq = 6", (tampered,))
    c.close()
    with SqliteJournal(p) as j:
        with pytest.raises(JournalIntegrityError, match="triggers missing"):
            j.verify_chain()
        with pytest.raises(JournalIntegrityError, match="seq 6: hash mismatch"):
            j.verify_chain(check_triggers=False)
        with pytest.raises(JournalIntegrityError, match="triggers missing"):     # appends refuse a de-protected file
            j.append_batch([(at(B0 + 99), BookId(""), snap_obs(B0 + 99))])


@pytest.mark.parametrize("attack", ["header", "delete_tail", "delete_middle", "rebatch", "idem", "anchor"])
def test_every_kind_of_tampering_is_detected(tmp_path: Path, attack: str) -> None:
    p = tmp_path / "j.sqlite"
    _populated(p)
    c = raw_conn(p)
    c.execute("DROP TRIGGER journal_no_update")
    c.execute("DROP TRIGGER journal_no_delete")
    sql = {"header": "UPDATE journal SET block = block + 0, sub = 7 WHERE seq = 4",
           "delete_tail": "DELETE FROM journal WHERE seq = 12",
           "delete_middle": "DELETE FROM journal WHERE seq = 5",
           "rebatch": "UPDATE journal SET batch = 5 WHERE seq = 8",
           "idem": "UPDATE journal SET idem = 'snap:forged' WHERE seq = 3",
           "anchor": "UPDATE anchor SET head_seq = 11"}[attack]
    c.execute(sql)
    c.close()
    with SqliteJournal(p, readonly=True) as j, pytest.raises(JournalIntegrityError):
        j.verify_chain(check_triggers=False, deep=True)


def test_forged_rehash_is_caught_only_by_the_heartbeat_head(tmp_path: Path) -> None:
    """An attacker who rewrites a payload AND re-hashes the rest of the chain AND the anchor produces a self-consistent
    file. The off-host heartbeat copy of head() (section 7.2) still detects it."""
    p = tmp_path / "j.sqlite"
    recs = _populated(p)
    heartbeat = (recs[-1].seq, recs[-1].hash)
    c = raw_conn(p)
    c.execute("DROP TRIGGER journal_no_update")
    rows = c.execute("SELECT seq, block, phase, sub, book, kind, version, payload FROM journal ORDER BY seq").fetchall()
    prev = GENESIS_HASH
    for seq, block, phase, sub, book, kind, version, payload in rows:
        if seq == 6:
            payload = codec.canonical_bytes(ModeChanged(BOOK, Block(B0 + 2), Mode.FROZEN, "t2"))
        h = record_hash(prev, block, phase, sub, book, kind, version, payload)
        c.execute("UPDATE journal SET payload = ?, prev_hash = ?, hash = ? WHERE seq = ?", (payload, prev, h, seq))
        prev = h
    c.execute("UPDATE anchor SET head_hash = ?", (prev,))
    c.execute("CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'append-only'); END")
    c.close()
    with SqliteJournal(p) as j:
        assert j.verify_chain() == 12                                          # self-consistent forgery
        with pytest.raises(JournalIntegrityError, match="differs from the expected head"):
            j.verify_chain(expected_head=heartbeat)


def test_whole_file_rollback_is_caught_by_the_heartbeat_head(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    with SqliteJournal(p) as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
    backup = tmp_path / "backup.sqlite"
    shutil.copyfile(p, backup)                                                 # WAL was checkpointed on close
    with SqliteJournal(p) as j:
        j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1))])
        heartbeat = j.head()
    shutil.copyfile(backup, p)
    with SqliteJournal(p) as j:
        assert j.verify_chain() == 1
        with pytest.raises(JournalIntegrityError, match="rolled back"):
            j.verify_chain(expected_head=heartbeat)


def test_readonly_journal_reads_and_refuses_writes(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    _populated(p, 2)
    with SqliteJournal(p, readonly=True) as j:
        assert j.verify_chain() == 4 and len(list(j.read())) == 4
        with pytest.raises(JournalError):
            j.append_batch([(at(B0 + 9), BookId(""), snap_obs(B0 + 9))])
    with pytest.raises(JournalError):
        SqliteJournal(tmp_path / "empty.sqlite", readonly=True)


# ------------------------------------------------------------------------------------------------ crash consistency
_CHILD_PRELUDE = """
import os, sys
from taotrader.core.units import Block, BlockHash, BookId, LogicalTime, Phase
from taotrader.core.events import HealthObs, SnapshotObserved
from taotrader.core.state import ReadPlan
from taotrader.data.journal import SqliteJournal

def snap(b):
    return SnapshotObserved(block=Block(b), block_hash=BlockHash("0x" + f"{b:064x}"), digest=f"{b:032x}",
                            plan=ReadPlan.FULL, ts_ms=1_759_900_000_000 + b, health=HealthObs.nominal())
"""


def run_child(tmp_path: Path, body: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    script = tmp_path / f"child_{abs(hash(body)) % 10**8}.py"
    script.write_text(_CHILD_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("crash_point", ["before_insert", "after_insert", "after_anchor"])
def test_process_killed_inside_the_transaction_leaves_no_partial_batch(tmp_path: Path, crash_point: str) -> None:
    p = tmp_path / "j.sqlite"
    with SqliteJournal(p) as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
    proc = run_child(tmp_path, f"""
        j = SqliteJournal(sys.argv[1])
        real = j._db
        class Killer:
            def __getattr__(self, n): return getattr(real, n)
            def executemany(self, sql, rows):
                if {crash_point!r} == "before_insert": os._exit(17)
                r = real.executemany(sql, rows)
                if {crash_point!r} == "after_insert": os._exit(17)
                return r
            def execute(self, sql, *a):
                if sql == "COMMIT" and {crash_point!r} == "after_anchor": os._exit(17)
                return real.execute(sql, *a)
        j._db = Killer()
        j.append_batch([(LogicalTime(Block({B0 + 1})), BookId(""), snap({B0 + 1})),
                        (LogicalTime(Block({B0 + 1}), Phase.INGEST, 1), BookId(""), snap({B0 + 2}))])
        print("not killed")
    """, str(p))
    assert proc.returncode == 17, proc.stdout + proc.stderr
    with SqliteJournal(p) as j:
        assert j.verify_chain() == 1                                          # the batch is entirely absent
        assert not j.has_idem(f"snap:0x{B0 + 1:064x}")
        r = j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1))])      # the source re-delivers; it commits
        assert r[0].seq == 2 and j.verify_chain() == 2


def test_process_killed_right_after_commit_keeps_the_batch(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    proc = run_child(tmp_path, f"""
        j = SqliteJournal(sys.argv[1])
        j.append_batch([(LogicalTime(Block({B0})), BookId(""), snap({B0})),
                        (LogicalTime(Block({B0}), Phase.INGEST, 1), BookId(""), snap({B0 + 1}))])
        os._exit(17)                                                          # no close, no checkpoint
    """, str(p))
    assert proc.returncode == 17, proc.stderr
    with SqliteJournal(p) as j:
        assert j.verify_chain() == 2 and j.anchor()[0] == 2


# ------------------------------------------------------------------------------------------------ concurrency
def test_concurrent_writer_processes_and_reader_keep_one_valid_chain(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE serializes writers across processes (each re-reads the head under the lock); a read-only
    process verifying in a loop never sees a broken chain (WAL snapshot covers rows and anchor)."""
    p = tmp_path / "j.sqlite"
    with SqliteJournal(p) as j:
        j.append_batch([(at(B0), BookId(""), snap_obs(B0))])
    writer = f"""
        name, n = sys.argv[2], int(sys.argv[3])
        j = SqliteJournal(sys.argv[1])
        from taotrader.core.events import ModeChanged
        from taotrader.core.units import Mode
        for k in range(n):
            j.append_batch([(LogicalTime(Block({B0}), Phase.EMIT, k), BookId(name),
                             ModeChanged(BookId(name), Block({B0}), Mode.NORMAL, f"{{name}}-{{k}}")),
                            (LogicalTime(Block({B0}), Phase.EMIT, k), BookId(name),
                             ModeChanged(BookId(name), Block({B0}), Mode.CAUTION, f"{{name}}-{{k}}b"))])
        print("done")
    """
    reader = """
        import time
        j = SqliteJournal(sys.argv[1], readonly=True)
        t, k = time.time(), 0
        while time.time() - t < float(sys.argv[2]):
            j.verify_chain(); k += 1
        print(k)
    """
    procs = [subprocess.Popen([sys.executable, str(_script(tmp_path, f"w{w}", writer)), str(p), f"book{w}", "60"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for w in range(3)]
    rd = subprocess.Popen([sys.executable, str(_script(tmp_path, "r", reader)), str(p), "4"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for pr in [*procs, rd]:
        out, err = pr.communicate(timeout=180)
        assert pr.returncode == 0, out + err
    with SqliteJournal(p) as j:
        assert j.verify_chain(deep=True) == 1 + 3 * 60 * 2
        batches = list(j.read_batches())
        assert len(batches) == 1 + 3 * 60 and all(len({r.book for r in b}) == 1 for b in batches)


def _script(tmp_path: Path, name: str, body: str) -> Path:
    s = tmp_path / f"{name}.py"
    s.write_text(_CHILD_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    return s


def test_threads_share_one_instance_safely(tmp_path: Path) -> None:
    """The Runner commits from a worker thread while other threads read (section 4.7)."""
    with SqliteJournal(tmp_path / "j.sqlite") as j:
        errors: list[BaseException] = []

        def write(name: str) -> None:
            try:
                for k in range(40):
                    j.append_batch([(at(B0, Phase.EMIT, k), BookId(name), ModeChanged(BookId(name), Block(B0),
                                                                                      Mode.NORMAL, f"{name}{k}"))])
            except BaseException as e:   # pragma: no cover - reported below
                errors.append(e)

        def read() -> None:
            try:
                for _ in range(20):
                    j.verify_chain()
                    list(j.read())
            except BaseException as e:   # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=write, args=(f"b{i}",)) for i in range(4)] + [threading.Thread(target=read)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
        assert j.verify_chain(deep=True) == 160


# ------------------------------------------------------------------------------------------------ projection
def _order_flow(j: SqliteJournal) -> dict[str, OrderIntent]:
    """A mix of order lifecycles across books, including a crash-recovered SUBMITTING order and orphan facts."""
    a, b, c, d, e = (intent(B0, attempt=k) for k in range(5))
    m = intent(B0, book=BookId("momentum"))
    j.append_batch([(at(B0), BookId(""), snap_obs(B0)),
                    *[(at(B0, Phase.EMIT, k), BOOK, OrderIntended(x)) for k, x in enumerate((a, b, c, d, e))],
                    (at(B0, Phase.EMIT), BookId("momentum"), OrderIntended(m))])
    j.append_batch([(at(B0, Phase.OUTBOX, 0), BOOK, SubmitStarted(BOOK, a.order_id, a.attempt, "sim0", None, None)),
                    (at(B0, Phase.OUTBOX, 1), BOOK, VenueAck(BOOK, a.order_id, a.attempt, Block(B0), Block(B0 + 5), "", "")),
                    (at(B0, Phase.OUTBOX, 2), BOOK, SubmitStarted(BOOK, b.order_id, b.attempt, "sim1", None, None)),
                    (at(B0, Phase.OUTBOX, 3), BOOK, OrderCancelled(BOOK, c.order_id, c.attempt, Block(B0), "mode")),
                    (at(B0, Phase.OUTBOX, 4), BOOK, SubmitStarted(BOOK, d.order_id, d.attempt, "sim2", None, None)),
                    (at(B0, Phase.OUTBOX, 5), BOOK, VenueAck(BOOK, d.order_id, d.attempt, Block(B0), Block(B0 + 5), "", ""))])
    # crash: b stays SUBMITTING; recovery journals SubmitUnknown then resolve() lands a fill (section 4.5)
    j.append_batch([(at(B0 + 1), BookId(""), snap_obs(B0 + 1))])
    j.append_batch([(at(B0 + 1, Phase.OUTBOX), BOOK, SubmitUnknown(BOOK, b.order_id, b.attempt, "recovered_submitting"))])
    j.append_batch([(at(B0 + 5, Phase.VENUE, 0), BOOK, FillReported(fill_for(a, B0 + 5))),
                    (at(B0 + 5, Phase.VENUE, 1), BOOK, FillReported(fill_for(a, B0 + 5, leg=1))),     # second leg
                    (at(B0 + 5, Phase.VENUE, 2), BOOK, FillReported(fill_for(b, B0 + 5))),
                    (at(B0 + 5, Phase.VENUE, 3), BOOK, OrderFailed(BOOK, d.order_id, d.attempt, Block(B0 + 5),
                                                                   FailReason.SHIELD_MISSED, Rao(0), expired=True,
                                                                   detail="carrier_absent")),
                    (at(B0 + 5, Phase.VENUE, 4), BOOK, CarrierFeeSettled(BOOK, d.order_id, d.attempt, Block(B0 + 5),
                                                                         Rao(0), "never_included")),
                    # orphans / illegal: unknown order, SUBMITTING-less fill on INTENDED e, cancel of a FILLED order
                    (at(B0 + 5, Phase.VENUE, 5), BOOK, VenueAck(BOOK, OrderId("f" * 24), 0, Block(B0), Block(B0 + 5), "", "")),
                    (at(B0 + 5, Phase.VENUE, 6), BOOK, FillReported(fill_for(e, B0 + 5))),
                    (at(B0 + 5, Phase.VENUE, 7), BOOK, OrderCancelled(BOOK, a.order_id, a.attempt, Block(B0 + 5), "late"))])
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "m": m}


def test_orders_projection_folds_the_fsm_and_rebuilds_identically(tmp_path: Path) -> None:
    with SqliteJournal(tmp_path / "journal.sqlite") as j:
        st = open_run_state(tmp_path / "state.sqlite")
        proj = OrdersProjection(st)
        orders = _order_flow(j)
        # incremental fold, batch by batch (as the Runner would after each commit)
        anomalies = sum(proj.apply(b) for b in j.read_batches())
        assert anomalies == proj.anomalies == 3
        rows = {(r[0], r[1], r[2]): r for r in proj.rows()}
        state = {k: rows[(o.book, o.order_id, o.attempt)][3] for k, o in orders.items()}
        assert state == {"a": "FILLED", "b": "FILLED", "c": "CANCELLED", "d": "EXPIRED", "e": "INTENDED", "m": "INTENDED"}
        ra = rows[(BOOK, orders["a"].order_id, 0)]
        assert ra[4:] == ("add_stake_limit", 92, 8_355_590, HK, 10**9, 0, 1_400_000, int(Urgency.NORMAL), B0, B0 + 5,
                          "carry.entry")
        assert rows[(BOOK, orders["d"].order_id, 3)][13:] == (B0 + 5, "ShieldMissed:carrier_absent")
        assert rows[(BOOK, orders["c"].order_id, 2)][13:] == (B0, "mode")
        incremental = proj.rows()
        # rebuild from scratch (projection file lost) gives the identical table
        st.close()
        (tmp_path / "state.sqlite").unlink()
        for suffix in ("-wal", "-shm"):
            (tmp_path / f"state.sqlite{suffix}").unlink(missing_ok=True)
        st2 = open_run_state(tmp_path / "state.sqlite")
        proj2 = OrdersProjection(st2)
        assert proj2.rebuild(j) == 3
        assert proj2.rows() == incremental
        assert proj2.rebuild(j) == 3 and proj2.rows() == incremental             # idempotent
        st2.close()


def test_projection_apply_is_atomic(tmp_path: Path) -> None:
    with SqliteJournal() as j:
        st = open_run_state(":memory:")
        proj = OrdersProjection(st)
        i = intent(B0)
        j.append_batch([(at(B0, Phase.EMIT), BOOK, OrderIntended(i))])
        recs = list(j.read())
        bad = dataclasses.replace(recs[0], payload=b"{not json")
        with pytest.raises(ValueError):
            proj.apply([recs[0], bad])
        assert proj.rows() == [] and proj.anomalies == 0
        proj.apply(recs)
        assert [r[3] for r in proj.rows()] == [OrderState.INTENDED.value]


def test_run_state_tables_follow_section_7_3(tmp_path: Path) -> None:
    st = open_run_state(tmp_path / "runs" / "r1" / "state.sqlite")
    names = {r[0] for r in st.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == set(schema.RUN_STATE_TABLES)
    cols = [r[1] for r in st.execute("PRAGMA table_info(orders_proj)")]
    assert cols == ["book", "order_id", "attempt", "state", "kind", "netuid", "reg_at", "hotkey", "tao_in", "alpha_in",
                    "limit_price", "urgency", "created_block", "terminal_block", "reason"]
    assert st.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    st.close()
