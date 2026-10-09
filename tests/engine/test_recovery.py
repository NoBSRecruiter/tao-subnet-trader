"""engine.recovery + Runner.recover: crash matrix, SUBMITTING recovery, restart idempotence, replay verification,
drift, checkpoints (DESIGN.md 4.5, 10.3 items 4-5, section 11 WP7 acceptance)."""
from __future__ import annotations

import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from taotrader.core.config import ExecCfg, SleeveCfg
from taotrader.core.errors import ReplayDivergence
from taotrader.core.events import ConfigApplied, FillReported, OrderFailed, OrderIntended, SubmitStarted, SubmitUnknown
from taotrader.core.orders import FailReason, OrderState, Resolution
from taotrader.core.signals import Signal, SignalKind, StrategyOutput
from taotrader.core.units import Block, BookId, Ppm, Rao, RunMode, Stage, StrategyId
from taotrader.data.journal import JournalIntegrityError, SqliteJournal, decode_record
from taotrader.engine.recovery import (
    BookRuntime,
    Checkpoint,
    MemoryCheckpointStore,
    RecoveryError,
    SqliteCheckpointStore,
    decode_state,
    encode_state,
)
from taotrader.engine.reducer import book_view, check_state, money_digest
from taotrader.engine.runner import Runner

TAO = 10**9
CARRY = StrategyId("carry")
MOM = StrategyId("momentum")
N_SNAPS = 30


class Crash(BaseException):
    """A simulated process death (BaseException, so no `except Exception` in the code under test can swallow it)."""


def strategies_for(fx: Any, book: str) -> list[Any]:
    if book == "b1":
        return [fx.TargetStrategy(CARRY, [(0, {fx.K7: 500_000, fx.K9: 300_000}), (fx.START + 900, {fx.K9: 200_000})])]
    return [fx.TargetStrategy(CARRY, [(0, {fx.K9: 600_000})], decide_every_blocks=120),
            fx.TargetStrategy(MOM, [(0, {fx.K7: 400_000}), (fx.START + 600, {})], decide_every_blocks=300)]


def build(fx: Any, snaps: list[Any], journal: Any, *, fault: Any = None, venue: Any = None, strategies: Any = None,
          **kw: Any) -> Runner:
    rts = []
    for name in ("b1", "b2"):
        sleeves = None if name == "b1" else [SleeveCfg(CARRY, Stage.PAPER, Ppm(500_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(300_000))]
        exec_cfg = ExecCfg() if name == "b1" else ExecCfg(shield_miss_ppm=250_000)      # b2 misses often (locks, retries)
        strats = strategies(fx, name) if strategies is not None else strategies_for(fx, name)
        eng = fx.make_engine(fx.book_cfg(name, sleeves=sleeves, exec_cfg=exec_cfg), strats)
        rts.append(fx.make_runtime(eng, venue(name) if venue is not None else None, seed=7))
    return Runner(run_id=fx.RUN, mode=RunMode.BACKTEST, source=fx.FakeSource(snaps), journal=journal,
                  features=fx.FakeFeatures(), books=rts, fault=fault, **kw)


def summary(r: Runner) -> dict[str, Any]:
    recs = [(rec, decode_record(rec)) for rec in r.journal.read(1)]
    intents = Counter(ev.intent.order_id for _, ev in recs if isinstance(ev, OrderIntended))
    fills = Counter(ev.fill.fill_id for _, ev in recs if isinstance(ev, FillReported))
    return {"money": {rt.book: money_digest(rt.state) for rt in r.books}, "intents": intents, "fills": fills,
            "views": {rt.book: book_view(rt.state) for rt in r.books}}


def _reference(fx: Any, snaps: list[Any], tmp: Path) -> tuple[dict[str, Any], list[str]]:
    points: list[str] = []
    j = SqliteJournal(tmp / "ref.sqlite", durable=False)
    r = build(fx, snaps, j, fault=points.append)
    fx.arun(r.run())
    s = summary(r)
    j.close()
    return s, points


def _crash_and_recover(fx: Any, snaps: list[Any], path: Path, fault: Any, journal_cls: Any = None) -> Runner:
    j = (journal_cls or (lambda p: SqliteJournal(p, durable=False)))(path)
    r = build(fx, snaps, j, fault=fault)
    with pytest.raises(Crash):
        fx.arun(r.run())
    j.close()
    j2 = SqliteJournal(path, durable=False)
    r2 = build(fx, snaps, j2)
    fx.arun(r2.run())
    return r2


def test_crash_matrix_every_fault_point_reproduces_the_money_state(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    ref, points = _reference(fx, snaps, tmp_path)
    assert {"pre_commit", "post_commit", "drain_post_commit", "after_submit_started", "after_venue_submit",
            "outbox_post_commit"} <= set(points)
    assert any(ev for ev in ref["fills"]) and max(ref["intents"].values()) == 1
    for k in range(len(points)):
        def fault(point: str, k: int = k, seen: list[int] = [0]) -> None:   # noqa: B006 - one counter per k
            if seen[0] == k:
                raise Crash(point)
            seen[0] += 1

        r2 = _crash_and_recover(fx, snaps, tmp_path / f"crash{k}.sqlite", fault)
        got = summary(r2)
        assert got["money"] == ref["money"], f"fault point {k} ({points[k]})"
        assert got["intents"] == ref["intents"] and got["fills"] == ref["fills"], f"fault point {k} ({points[k]})"
        assert all(c == 1 for c in got["intents"].values()) and all(c == 1 for c in got["fills"].values())
        assert got["views"] == ref["views"], f"fault point {k} ({points[k]})"
        assert all(check_state(rt.state) == [] and rt.state.orphans == 0 for rt in r2.books)
        r2.journal.close()


class InTxnCrashJournal(SqliteJournal):
    """Raises from INSIDE the append transaction of its n-th batch (SQLite progress handler interrupt)."""

    def __init__(self, path: Path, crash_at: int) -> None:
        super().__init__(path, durable=False)
        self.n = 0
        self.crash_at = crash_at

    def append_batch(self, items: Any) -> Any:
        self.n += 1
        if self.n != self.crash_at:
            return super().append_batch(items)
        head = self.head()
        ops = [0]

        def handler() -> int:
            ops[0] += 1
            return 1 if ops[0] > 2 else 0

        self._db.set_progress_handler(handler, 20)
        err: sqlite3.OperationalError | None = None
        try:
            super().append_batch(items)
        except sqlite3.OperationalError as e:
            err = e
        finally:
            self._db.set_progress_handler(None, 0)
        if err is None:
            raise AssertionError("the interrupt did not fire inside the transaction")
        assert self.head() == head                          # the whole batch rolled back
        raise Crash(f"in transaction: {err}")


def test_crash_inside_the_commit_transaction_rolls_back_and_recovers(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    ref, _ = _reference(fx, snaps, tmp_path)
    for crash_at in (1, 2, 5, 9, 17, 33, 60):
        r2 = _crash_and_recover(fx, snaps, tmp_path / f"txn{crash_at}.sqlite", None,
                                journal_cls=lambda p, c=crash_at: InTxnCrashJournal(p, c))
        got = summary(r2)
        assert got["money"] == ref["money"] and got["intents"] == ref["intents"] and got["fills"] == ref["fills"]
        r2.journal.close()


def test_a_restart_on_a_complete_journal_reverifies_and_writes_nothing(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    j = SqliteJournal(tmp_path / "j.sqlite", durable=False)
    fx.arun(build(fx, snaps, j).run())
    head = j.head()
    j.close()
    j2 = SqliteJournal(tmp_path / "j.sqlite", durable=False)
    r2 = build(fx, snaps, j2, deep_verify=True, expected_head=head)
    s = fx.arun(r2.run())
    assert j2.head() == head and s.ticks == 0
    assert s.recovery is not None and s.recovery.verified_ticks == N_SNAPS and s.recovery.ticks == N_SNAPS
    j2.close()


def test_two_runs_have_identical_hash_chains_and_restarts_continue_them(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    a = SqliteJournal(":memory:")
    fx.arun(build(fx, snaps, a).run())
    b = SqliteJournal(tmp_path / "b.sqlite", durable=False)
    fx.arun(build(fx, snaps[:12], b).run())                 # stop early (a clean shutdown), then resume
    b.close()
    b2 = SqliteJournal(tmp_path / "b.sqlite", durable=False)
    fx.arun(build(fx, snaps, b2).run())
    assert a.head() == b2.head()                             # a clean stop + restart is invisible in the journal


def test_journal_rollback_is_detected_with_the_heartbeat_head(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    j = SqliteJournal(tmp_path / "live.sqlite", durable=False)
    fx.arun(build(fx, snaps[:10], j).run())
    j.close()
    shutil.copy(tmp_path / "live.sqlite", tmp_path / "old.sqlite")
    j = SqliteJournal(tmp_path / "live.sqlite", durable=False)
    fx.arun(build(fx, snaps, j).run())
    head = j.head()
    j.close()
    old = SqliteJournal(tmp_path / "old.sqlite", durable=False)
    with pytest.raises(JournalIntegrityError):
        fx.arun(build(fx, snaps, old, expected_head=head).run())
    assert old.head()[0] < head[0]
    old.close()


# ------------------------------------------------------------------------------------------------- SUBMITTING recovery
def _submitting_crash(fx: Any, market: Any, tmp_path: Path, outcome: str) -> tuple[Runner, SubmitStarted]:
    snaps = market(4)
    path = tmp_path / f"sub-{outcome}.sqlite"

    def venues(name: str) -> Any:
        return fx.ScriptedVenue(BookId(name))

    def fault(point: str) -> None:
        if point == "after_submit_started":
            raise Crash(point)

    j = SqliteJournal(path, durable=False)
    r = build(fx, snaps, j, fault=fault, venue=venues)
    with pytest.raises(Crash):
        fx.arun(r.run())
    j.close()
    j = SqliteJournal(path, durable=False)
    started = [decode_record(rec) for rec in j.read(1) if rec.kind == SubmitStarted.KIND]
    assert len(started) == 1 and isinstance(started[0], SubmitStarted)
    st = started[0]
    intent = next(decode_record(rec).intent for rec in j.read(1) if rec.kind == OrderIntended.KIND
                  and decode_record(rec).intent.order_id == st.order_id)
    if outcome == "landed":
        facts: tuple[Any, ...] = (FillReported(fx.mk_fill(intent, fx.START + 5, tao=int(intent.tao_in),
                                                         alpha=int(intent.tao_in) * 90)),)
    else:
        facts = (OrderFailed(intent.book, intent.order_id, 0, Block(fx.START + 5), FailReason.SHIELD_MISSED, Rao(98_000),
                             expired=True),)

    def venues2(name: str) -> Any:
        v = fx.ScriptedVenue(BookId(name))
        if name == st.book:
            v.resolution = (Resolution.LANDED, facts)
        return v

    r2 = build(fx, snaps, j, venue=venues2)
    fx.arun(r2.recover())
    return r2, st


@pytest.mark.parametrize("outcome", ["landed", "missed"])
def test_submitting_crash_then_resolve_lands_without_an_orphan(fx, market, tmp_path, outcome) -> None:
    r2, st = _submitting_crash(fx, market, tmp_path, outcome)
    recs = [(rec, decode_record(rec)) for rec in r2.journal.read(1)]
    kinds = [type(ev).__name__ for _, ev in recs if getattr(ev, "order_id", None) == st.order_id
             or (isinstance(ev, FillReported) and ev.fill.order_id == st.order_id)]
    want_last = "FillReported" if outcome == "landed" else "OrderFailed"
    assert kinds == ["SubmitStarted", "SubmitUnknown", want_last]
    unknown = next(ev for _, ev in recs if isinstance(ev, SubmitUnknown))
    assert unknown.detail == "recovered_submitting"
    rt = r2.book(st.book)
    order = next(o for o in rt.state.orders if o.intent.order_id == st.order_id)
    assert order.state is (OrderState.FILLED if outcome == "landed" else OrderState.EXPIRED)
    assert rt.state.orphans == 0 and check_state(rt.state) == []
    venue = rt.venue
    assert st.order_id not in venue.submits                     # never re-sent after the crash
    r2.journal.close()


# ------------------------------------------------------------------------------------------------- verification
@dataclass
class NondeterministicStrategy:
    """Its weight depends on a process-global counter: a replay recomputes different signals."""
    id: StrategyId = CARRY
    decide_every_blocks: int = 60
    wake_on: frozenset[Any] = frozenset()
    min_cadence_blocks: int = 60
    valid_from_block: Block = Block(0)
    declares_dilution: bool = False
    counter: list[int] = field(default_factory=lambda: [0])

    def initial_memory(self) -> object:
        return None

    def on_tick(self, ctx: Any, memory: object) -> StrategyOutput:
        self.counter[0] += 1
        sig = Signal(self.id, ctx.raw.subnets[0].key, ctx.block, SignalKind.TARGET, Ppm(100_000 + self.counter[0]))
        return StrategyOutput((sig,), None)


def test_deliberate_nondeterminism_raises_replay_divergence(fx, market, tmp_path) -> None:
    snaps = market(6)
    shared = [0]

    def strat(fx_: Any, name: str) -> list[Any]:
        if name == "b1":
            return [NondeterministicStrategy(id=CARRY, counter=shared)]
        return [fx_.TargetStrategy(CARRY, []), fx_.TargetStrategy(MOM, [])]
    j = SqliteJournal(tmp_path / "n.sqlite", durable=False)
    fx.arun(build(fx, snaps, j, strategies=strat).run())
    j.close()
    j2 = SqliteJournal(tmp_path / "n.sqlite", durable=False)
    with pytest.raises(ReplayDivergence, match="book 'b1'"):
        fx.arun(build(fx, snaps, j2, strategies=strat).run())
    j2.close()


def test_code_or_config_drift_needs_accept_drift(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    path = tmp_path / "d.sqlite"
    j = SqliteJournal(path, durable=False)
    fx.arun(build(fx, snaps[:10], j, config_hash="cfg-1", code_hash="code-1").run())
    j.close()
    j = SqliteJournal(path, durable=False)
    with pytest.raises(ReplayDivergence, match="accept-drift"):
        fx.arun(build(fx, snaps, j, config_hash="cfg-2", code_hash="code-1").run())
    j.close()
    def changed(fx_: Any, name: str) -> list[Any]:
        return [fx_.TargetStrategy(CARRY, [(0, {fx_.K9: 100_000})])] if name == "b1" else strategies_for(fx_, name)
    j = SqliteJournal(path, durable=False)
    r = build(fx, snaps[:20], j, config_hash="cfg-2", code_hash="code-1", accept_drift=True, strategies=changed)
    s = fx.arun(r.run())
    assert s.recovery is not None and s.recovery.drift and s.recovery.verified_ticks == 0
    cfgs = [decode_record(rec) for rec in j.read(1) if rec.kind == ConfigApplied.KIND]
    assert [(c.config_hash, int(c.block)) for c in cfgs] == [("cfg-1", fx.START), ("cfg-2", fx.START + 9 * 60)]
    j.close()
    j = SqliteJournal(path, durable=False)                   # same new code: the post-drift batches verify again
    s = fx.arun(build(fx, snaps, j, config_hash="cfg-2", code_hash="code-1", strategies=changed).run())
    assert s.recovery is not None and not s.recovery.drift and s.recovery.verified_ticks == 10
    j.close()


# ------------------------------------------------------------------------------------------------- checkpoints
def test_state_codec_round_trips_and_rejects_tampering(fx, market) -> None:
    r = build(fx, market(12), SqliteJournal(":memory:"))
    fx.arun(r.run())
    st = r.book("b2").state
    h, blob = encode_state(st)
    assert decode_state(blob, h) == st
    with pytest.raises(RecoveryError):
        decode_state(blob, "0" * 64)
    with pytest.raises(RecoveryError):
        decode_state(b"not zstd", h)


def test_checkpoint_plus_tail_recovery_equals_a_full_replay(fx, market, tmp_path) -> None:
    snaps = market(N_SNAPS)
    path = tmp_path / "cp.sqlite"
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    store = SqliteCheckpointStore(conn)
    j = SqliteJournal(path, durable=False)
    fx.arun(build(fx, snaps[:20], j, checkpoints=store, checkpoint_every_blocks=300).run())
    j.close()
    assert len(store.seqs(BookId("b1"))) >= 3 and store.seqs(BookId("b1")) == store.seqs(BookId("b2"))
    shutil.copy(path, tmp_path / "full.sqlite")
    j = SqliteJournal(path, durable=False)
    r_cp = build(fx, snaps, j, checkpoints=store, checkpoint_every_blocks=300, features_factory=fx.FakeFeatures,
                 feature_warm_blocks=600)
    fx.arun(r_cp.recover())
    assert r_cp.recovery is not None and r_cp.recovery.checkpoint_seq == max(store.seqs(BookId("b1")))
    assert r_cp.recovery.verified_ticks < 20
    jf = SqliteJournal(tmp_path / "full.sqlite", durable=False)
    r_full = build(fx, snaps, jf)
    fx.arun(r_full.recover())
    assert r_full.recovery is not None and r_full.recovery.checkpoint_seq is None and r_full.recovery.verified_ticks == 20
    for a, b in zip(r_cp.books, r_full.books, strict=True):
        assert book_view(a.state) == book_view(b.state) and a.state == b.state
        assert a.venue.state_digest() == b.venue.state_digest()
    assert r_cp.features.state_digest() == r_full.features.state_digest()
    fx.arun(r_cp.run())
    fx.arun(r_full.run())
    assert r_cp.journal.head() == r_full.journal.head()
    j.close()
    jf.close()
    conn.close()


def test_an_unusable_checkpoint_falls_back_to_a_full_replay(fx, market, tmp_path) -> None:
    snaps = market(15)
    store = MemoryCheckpointStore()
    j = SqliteJournal(tmp_path / "x.sqlite", durable=False)
    fx.arun(build(fx, snaps, j, checkpoints=store, checkpoint_every_blocks=300).run())
    j.close()
    seq = max(store.seqs(BookId("b1")))
    bad = store.load(BookId("b1"), seq)
    assert bad is not None
    store.save(Checkpoint(bad.book, seq, bad.state_hash, bad.blob, "wrong-features-digest"))
    j = SqliteJournal(tmp_path / "x.sqlite", durable=False)
    r = build(fx, snaps, j, checkpoints=store, features_factory=fx.FakeFeatures, feature_warm_blocks=600)
    fx.arun(r.recover())
    assert r.recovery is not None and r.recovery.checkpoint_seq is None and r.recovery.verified_ticks == 15
    j.close()


def test_book_runtime_starts_from_the_initial_state(fx) -> None:
    eng = fx.make_engine(fx.book_cfg(), [fx.TargetStrategy(CARRY, [])])
    rt = BookRuntime(engine=eng, venue=fx.ScriptedVenue(BookId("b1")))
    assert rt.state == eng.initial_state() and rt.book == "b1"
