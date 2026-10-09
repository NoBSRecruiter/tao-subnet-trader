"""taotrader/engine/runner.py - Runner: tick, commit, drain, outbox, recover - the only side-effect loop (WP7).

Identical in backtest, paper, live-dry and live; only the injected adapters differ (DESIGN.md 4.1, 4.4, 4.5, 4.7):
DataSource (ParquetReplay / LiveChainFeed), Journal (SqliteJournal), FeatureEngine, and per book an Engine (pure) and an
ExecutionVenue (SimVenue / PaperVenue / LiveVenue). The engine package never imports data/, venues/ or features/.

Runner.tick(item):
    store.clock = block; events = derive_events(prev, snap); own = union of every book's fill blocks in the last 1,800
    blocks; frame = features.update(snap, events, own_fill_blocks=own)
    batch = [ConfigApplied (first tick / after accepted drift)] + [SnapshotObserved(..., health)]
            + [ChainEventObserved(e)...] + [OperatorCommand (control files)...]                         run-level, INGEST
    for book: batch += [CapitalChanged (initial capital, queued capital)]                               book, INGEST
              batch += book.engine.decide(state, snap, prev, events, frame, venue.mark_to(snap), health, inputs=...)
    journal.append_batch(batch)        === THE COMMIT POINT (atomic + fsync) ===
    fold the batch into every book state (reducer.fold_batch: invariants checked) and venue.observe every event
    for book: drain (venue.advance one event at a time, each its own batch, Phase.VENUE)
              outbox (Phase.OUTBOX): UNKNOWN -> venue.resolve (never re-sent blindly);
                                     INTENDED -> venue.reserve -> commit SubmitStarted -> venue.submit
                                                 -> commit VenueAck | OrderFailed | SubmitUnknown (submit raised)
    optional reconcile hook (live), checkpoint every `checkpoint_every_blocks`.

Runner.recover() (also run first by run()): verify the hash chain, replay/verify the journal (engine.recovery), journal
ConfigApplied if drift was accepted, SubmitUnknown("recovered_submitting") for every SUBMITTING order (one batch, BEFORE
any resolve), the operator control files (a KILL or halt placed while the process was down stops the re-drive), then
drain, resolve UNKNOWN orders and re-drive the outbox at the last snapshot (all Phase.OUTBOX at the last journaled
block), and resume the stream strictly after that block. A restart on a complete journal writes nothing (unless a
control file is waiting).

Side effects strictly follow the commit that records them; a crash at any point leaves a journal from which recovery
reproduces the crash-free money state (crash matrix, tests/engine/test_recovery.py). Any exception inside a tick
poisons the Runner (it refuses further ticks): the process must restart and recover.

Fault hooks (`fault`, tests only): "pre_commit", "post_commit", "drain_post_commit", "after_submit_started",
"after_venue_submit", "outbox_post_commit", "resolve_post_commit", "recovery_post_commit".
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final, Protocol, cast

from ..core.errors import DataContractError
from ..core.events import (
    CapitalChanged,
    ChainEventObserved,
    ConfigApplied,
    JournalEvent,
    OperatorCommand,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
)
from ..core.orders import OrderState
from ..core.protocols import DataSource, FeatureEngine, Journal, SourceItem
from ..core.state import ChainSnapshot
from ..core.units import Block, BookId, LogicalTime, Phase, Ppm, RunMode
from ..protocol.derive import derive_events
from .control import ControlWatcher
from .engine import Item
from .recovery import (
    BookRuntime,
    Checkpoint,
    CheckpointStore,
    ReplayResult,
    encode_state,
    recovery_time,
    replay,
    snapshot_digest,
)
from .reducer import FILLS_WINDOW_BLOCKS, event_book, fold_batch

__all__ = ["AlertHook", "BookRuntime", "FaultHook", "Reconciler", "RunSummary", "Runner", "RunnerPoisoned"]

log = logging.getLogger("taotrader.engine.runner")

FaultHook = Callable[[str], None]
AlertHook = Callable[[str, str], None]                  # (kind, message): "orphan" | "invariant" | "venue"
Reconciler = Callable[[BookRuntime, ChainSnapshot], Awaitable[Sequence[JournalEvent]]]
MAX_DRAIN_EVENTS: Final[int] = 10_000                   # per book and tick: a venue that never runs dry is a bug
MAX_DETAIL: Final[int] = 200
RUN_BOOK: Final[BookId] = BookId("")
LARGE_FLOW_FRAC_PPM: Final[Ppm] = Ppm(20_000)           # section 4.3 LARGE_FLOW: |dflow| >= 2% of SubnetTAO


class RunnerPoisoned(RuntimeError):
    """A tick failed part-way; this Runner refuses further work. Restart the process (recovery replays the journal)."""


class _FullVerify(Protocol):
    def verify_chain(self, *, expected_head: tuple[int, bytes] | None = ..., deep: bool = ...) -> int: ...


@dataclass(frozen=True, slots=True)
class RunSummary:
    ticks: int
    last_block: Block | None
    recovery: ReplayResult | None


class Runner:
    """The one side-effect loop. One instance per (mode, run_id); single-task (commits may run on a worker thread)."""

    def __init__(self, *, run_id: str, mode: RunMode, source: DataSource, journal: Journal, features: FeatureEngine,
                 books: Sequence[BookRuntime], features_factory: Callable[[], FeatureEngine] | None = None,
                 control: ControlWatcher | None = None, checkpoints: CheckpointStore | None = None,
                 checkpoint_every_blocks: int = 7_200, config_hash: str = "", code_hash: str = "", prereg_hash: str = "",
                 accept_drift: bool = False, expected_head: tuple[int, bytes] | None = None, deep_verify: bool = False,
                 commit_in_thread: bool = False, large_flow_frac_ppm: Ppm = LARGE_FLOW_FRAC_PPM,
                 feature_warm_blocks: int = 230_400, fault: FaultHook | None = None, on_alert: AlertHook | None = None,
                 reconcile: Reconciler | None = None) -> None:
        ids = [rt.book for rt in books]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate book ids {sorted(ids)}")
        for rt in books:
            if rt.engine.mode is not mode:
                raise ValueError(f"book {rt.book}: engine mode {rt.engine.mode} != runner mode {mode}")
            if rt.engine.run_id != run_id:
                raise ValueError(f"book {rt.book}: engine run_id {rt.engine.run_id!r} != {run_id!r}")
            for s in rt.engine.strategies:
                if s.min_cadence_blocks < source.cadence_blocks:
                    raise DataContractError(f"book {rt.book}: strategy {s.id} needs data every {s.min_cadence_blocks} blocks, "
                                            f"the source delivers every {source.cadence_blocks}")
        self.run_id = run_id
        self.mode = mode
        self.source = source
        self.journal = journal
        self.features = features
        self.features_factory = features_factory
        self.books: tuple[BookRuntime, ...] = tuple(sorted(books, key=lambda rt: rt.book))
        self.control = control
        self.checkpoints = checkpoints
        self.checkpoint_every_blocks = checkpoint_every_blocks
        self.cfg_hashes: tuple[str, str, str] = (config_hash, code_hash, prereg_hash)
        self.accept_drift = accept_drift
        self.expected_head = expected_head
        self.deep_verify = deep_verify
        self.commit_in_thread = commit_in_thread
        self.large_flow_frac_ppm = large_flow_frac_ppm
        self.feature_warm_blocks = feature_warm_blocks
        self._fault_hook = fault
        self.on_alert = on_alert
        self.reconcile = reconcile
        self.ticks = 0
        self.recovery: ReplayResult | None = None
        self._recovered = False
        self._poisoned = False
        self._pending_config = False
        self._prev: ChainSnapshot | None = None
        self._last_block: Block | None = None
        self._last_cp_block: Block | None = None
        self._sub = 0
        self._capital: dict[str, list[tuple[int, int, str]]] = {}
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------ public
    @property
    def last_block(self) -> Block | None:
        return self._last_block

    def book(self, book: str) -> BookRuntime:
        for rt in self.books:
            if rt.book == book:
                return rt
        raise KeyError(book)

    def queue_capital(self, book: str, cash_delta: int, fee_float_delta: int, memo: str) -> None:
        """A CapitalChanged input for `book`, journaled in the next tick batch. The memo must be unique per book."""
        rt = self.book(book)
        if memo == "initial" or self.journal.has_idem(f"capital:{rt.book}:{memo}") or any(
                m == memo for _, _, m in self._capital.get(str(rt.book), [])):
            raise ValueError(f"capital memo {memo!r} already used for book {book}")
        self._capital.setdefault(str(rt.book), []).append((cash_delta, fee_float_delta, memo))

    async def run(self, *, until: Block | None = None, max_ticks: int | None = None) -> RunSummary:
        """recover(), then tick every DataSource item strictly after the last journaled block."""
        await self.recover()
        n = 0
        stream = self.source.stream(self._last_block)
        try:
            async for item in stream:
                if until is not None and item.snapshot.block > until:
                    break
                await self.tick(item)
                n += 1
                if max_ticks is not None and n >= max_ticks:
                    break
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
        return RunSummary(n, self._last_block, self.recovery)

    async def recover(self) -> ReplayResult:
        """Verify and fold the journal, then finish whatever the last tick left open (section 4.5)."""
        if self._recovered and self.recovery is not None:
            return self.recovery
        self._check_alive()
        try:
            if self.expected_head is not None or self.deep_verify:
                cast(_FullVerify, self.journal).verify_chain(expected_head=self.expected_head, deep=self.deep_verify)
            else:
                self.journal.verify_chain()
            res = replay(self.journal, self.books, self.features, self.source.store, current_cfg=self.cfg_hashes,
                         accept_drift=self.accept_drift, large_flow_frac_ppm=self.large_flow_frac_ppm, fold=self._fold,
                         own_fill_blocks=self._own_fill_blocks, checkpoints=self.checkpoints,
                         features_factory=self.features_factory, feature_warm_blocks=self.feature_warm_blocks)
            if res.features is not None:
                self.features = res.features
            self.recovery = res
            if res.records == 0:
                self._pending_config = True
                self._recovered = True
                return res
            self._prev, self._last_block = res.last_snapshot, res.last_block
            self._last_cp_block = res.last_block
            assert res.last_block is not None
            b = res.last_block
            if res.drift:
                await self._commit_fold([(recovery_time(res, RUN_BOOK, Phase.OUTBOX), RUN_BOOK,
                                          ConfigApplied(b, *self.cfg_hashes))], "recovery_post_commit")
            stuck: list[Item] = []
            for rt in self.books:
                for o in sorted(rt.state.orders, key=lambda x: x.seq):
                    if o.state is OrderState.SUBMITTING:
                        stuck.append((LogicalTime(b, Phase.OUTBOX, len(stuck)), rt.book,
                                      SubmitUnknown(rt.book, o.intent.order_id, o.intent.attempt, "recovered_submitting")))
            if stuck:
                await self._commit_fold(stuck, "recovery_post_commit")
            # Operator control BEFORE the outbox is re-driven: a KILL file (or halt) placed while the process was down
            # must stop the re-drive of the last tick's INTENDED orders (section 9.8 #14), not only the next tick.
            ops = self._poll_control(b)
            if ops:
                t0 = recovery_time(res, RUN_BOOK, Phase.OUTBOX)
                await self._commit_fold([(LogicalTime(b, t0.phase, i), RUN_BOOK, op) for i, op in enumerate(ops)],
                                        "recovery_post_commit")
                if self.control is not None:
                    self.control.ack(ops)
            snap = res.last_snapshot
            if snap is not None:
                self.source.store.clock = snap.block
                self._sub = 0
                for rt in self.books:
                    await self._drain(rt, snap, Phase.OUTBOX)
                    await self._flush_outbox(rt, snap, Phase.OUTBOX)
                    await self._reconcile(rt, snap)
            self._recovered = True
            log.info("recovered %d records (%d ticks, %d verified, checkpoint %s) through block %s",
                     res.records, res.ticks, res.verified_ticks, res.checkpoint_seq, res.last_block)
            return res
        except BaseException:
            self._poisoned = True
            raise

    async def tick(self, item: SourceItem) -> None:
        """Process one DataSource item (one finalized block or stride snapshot)."""
        if not self._recovered:
            await self.recover()
        self._check_alive()
        snap = item.snapshot
        b = snap.block
        if self._last_block is not None and b <= self._last_block:
            return                                       # already journaled (re-delivery after a restart)
        try:
            await self._tick(item, snap, b)
        except BaseException:
            self._poisoned = True
            raise

    # ------------------------------------------------------------------ the tick
    async def _tick(self, item: SourceItem, snap: ChainSnapshot, b: Block) -> None:
        store = self.source.store
        store.clock = b
        prev = self._prev
        events = derive_events(prev, snap, self.large_flow_frac_ppm)
        frame = self.features.update(snap, events, own_fill_blocks=self._own_fill_blocks(b))
        run_events: list[JournalEvent] = []
        if self._pending_config:
            run_events.append(ConfigApplied(b, *self.cfg_hashes))
        run_events.append(SnapshotObserved(block=b, block_hash=snap.block_hash, digest=snapshot_digest(snap), plan=snap.plan,
                                           ts_ms=snap.timestamp_ms, health=item.health))
        run_events.extend(ChainEventObserved(e) for e in events)
        ops = self._poll_control(b)
        run_events.extend(ops)
        run_items: list[Item] = [(LogicalTime(b, Phase.INGEST, i), RUN_BOOK, e) for i, e in enumerate(run_events)]
        batch: list[Item] = list(run_items)
        for rt in self.books:
            book_events: list[JournalEvent] = []
            cfg = rt.engine.cfg
            if not rt.state.funded:
                book_events.append(CapitalChanged(rt.book, b, cfg.capital_rao, cfg.fee_float_rao, "initial"))
            for cash, fee_float, memo in self._capital.pop(str(rt.book), []):
                book_events.append(CapitalChanged(rt.book, b, cash, fee_float, memo))
            book_items: list[Item] = [(LogicalTime(b, Phase.INGEST, j), rt.book, e) for j, e in enumerate(book_events)]
            view = rt.venue.mark_to(snap)
            outs = rt.engine.decide(rt.state, snap, prev, events, frame, view, item.health, inputs=run_items + book_items,
                                    store=store)
            for t, bk, e in outs:
                if bk != rt.book or t.block != b:
                    raise RuntimeError(f"book {rt.book}: engine emitted a record for {bk!r} at {t}")
                if event_book(e) not in (None, rt.book):
                    raise RuntimeError(f"book {rt.book}: engine emitted {type(e).__name__} naming another book")
            batch += book_items + outs
        self._fault("pre_commit")
        await self._commit(batch)
        self._pending_config = False
        self._fault("post_commit")
        self._fold(batch)
        self._prev, self._last_block = snap, b
        if self.control is not None and ops:
            self.control.ack(ops)
        self._sub = 0
        for rt in self.books:
            await self._drain(rt, snap, Phase.VENUE)
            await self._flush_outbox(rt, snap, Phase.OUTBOX)
            await self._reconcile(rt, snap)
        self._checkpoint(b)
        self.ticks += 1

    # ------------------------------------------------------------------ venue side
    async def _drain(self, rt: BookRuntime, snap: ChainSnapshot, phase: Phase) -> None:
        """Commit the venue's due events (fills, failures, carrier fees, drift probes) one at a time."""
        for _ in range(MAX_DRAIN_EVENTS):
            ev = await rt.venue.advance(rt.venue.mark_to(snap))
            if ev is None:
                return
            items = self._venue_items(rt, snap.block, phase, [ev])
            if not items:
                raise RuntimeError(f"book {rt.book}: venue.advance returned an already journaled event {ev.idem()}")
            await self._commit_fold(items, "drain_post_commit")
        raise RuntimeError(f"book {rt.book}: venue.advance did not run dry after {MAX_DRAIN_EVENTS} events")

    async def _flush_outbox(self, rt: BookRuntime, snap: ChainSnapshot, phase: Phase) -> None:
        b = snap.block
        view = rt.venue.mark_to(snap)
        for o in sorted((o for o in rt.state.orders if o.state is OrderState.UNKNOWN), key=lambda x: x.seq):
            try:
                _, evs = await rt.venue.resolve(o.intent, view)
            except Exception as e:                       # provider trouble: ask again next tick; nothing is re-sent
                self._alert("venue", f"book {rt.book}: resolve({o.intent.order_id}) raised {type(e).__name__}: {e}")
                continue
            items = self._venue_items(rt, b, phase, list(evs))
            if items:
                await self._commit_fold(items, "resolve_post_commit")
        if rt.state.halted:
            return
        for o in sorted((o for o in rt.state.orders if o.state is OrderState.INTENDED), key=lambda x: x.seq):
            cur = next((x for x in rt.state.orders if x.intent.order_id == o.intent.order_id
                        and x.intent.attempt == o.intent.attempt), None)
            if cur is None or cur.state is not OrderState.INTENDED or rt.state.halted:
                continue
            intent = o.intent
            try:
                delegate, nonce, era_end = await rt.venue.reserve(intent, view)
            except Exception as e:                       # no free delegate (or provider trouble): retry next tick
                log.info("book %s: reserve(%s) refused: %s", rt.book, intent.order_id, e)
                break
            started: list[Item] = [(self._t(b, phase), rt.book,
                                    SubmitStarted(rt.book, intent.order_id, intent.attempt, delegate, nonce, era_end))]
            await self._commit(started)
            self._fault("after_submit_started")
            self._fold(started)
            ev: JournalEvent
            try:
                ev = await rt.venue.submit(intent, view)
            except Exception as e:                       # never OrderFailed: the outcome is unknown, resolve() decides
                ev = SubmitUnknown(rt.book, intent.order_id, intent.attempt, f"{type(e).__name__}: {e}"[:MAX_DETAIL])
            self._fault("after_venue_submit")
            if (event_book(ev) != rt.book or getattr(ev, "order_id", None) != intent.order_id
                    or getattr(ev, "attempt", None) != intent.attempt):
                ev = SubmitUnknown(rt.book, intent.order_id, intent.attempt,
                                   f"venue returned {type(ev).__name__} for another order"[:MAX_DETAIL])
            items = self._venue_items(rt, b, phase, [ev])
            if items:
                await self._commit_fold(items, "outbox_post_commit")

    async def _reconcile(self, rt: BookRuntime, snap: ChainSnapshot) -> None:
        if self.reconcile is None:
            return
        evs = await self.reconcile(rt, snap)
        items = self._venue_items(rt, snap.block, Phase.OUTBOX, list(evs))
        if items:
            await self._commit_fold(items, "recovery_post_commit")

    def _venue_items(self, rt: BookRuntime, b: Block, phase: Phase, evs: Sequence[JournalEvent]) -> list[Item]:
        """Journal items for venue-produced events: run-level or this book only; already journaled ones are skipped, and
        so is a repeat of the same idempotency key within this answer (it would fail the whole batch on the journal's
        UNIQUE key on every restart: a crash loop)."""
        items: list[Item] = []
        batch_idem: set[str] = set()
        for ev in evs:
            bk = event_book(ev)
            if bk is not None and bk != rt.book:
                raise RuntimeError(f"book {rt.book}: venue produced {type(ev).__name__} for book {bk!r}")
            idem = ev.idem()
            if idem is not None and (idem in batch_idem or self.journal.has_idem(idem)):
                self._alert("venue", f"book {rt.book}: venue repeated journaled event {idem}; skipped")
                continue
            if idem is not None:
                batch_idem.add(idem)
            items.append((self._t(b, phase), bk if bk is not None else RUN_BOOK, ev))
        return items

    # ------------------------------------------------------------------ commit and fold
    def _t(self, b: Block, phase: Phase) -> LogicalTime:
        t = LogicalTime(b, phase, self._sub)
        self._sub += 1
        return t

    async def _commit(self, items: Sequence[Item]) -> None:
        """THE commit point. Paper/live commit on one dedicated worker thread (section 4.7: fsync never blocks the
        event loop); commits are awaited one at a time, so journal order is the call order."""
        if self.commit_in_thread:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal-commit")
            await asyncio.get_running_loop().run_in_executor(self._executor, self.journal.append_batch, list(items))
        else:
            self.journal.append_batch(list(items))

    async def _commit_fold(self, items: Sequence[Item], fault_point: str) -> None:
        await self._commit(items)
        self._fault(fault_point)
        self._fold(items)

    def _fold(self, items: Sequence[Item]) -> None:
        """Fold one committed batch into every book state, then let every venue observe every event."""
        for rt in self.books:
            evs = [e for _, bk, e in items if bk in ("", rt.book)]
            if not evs:
                continue
            before = rt.state
            rt.state = fold_batch(rt.state, evs)
            if rt.state.orphans > before.orphans:
                self._alert("orphan", f"book {rt.book}: quarantined {rt.state.quarantine[-1:]}; entries halted")
            if rt.state.breaches and rt.state.breaches != before.breaches:
                self._alert("invariant", f"book {rt.book}: {list(rt.state.breaches)}; entries halted")
        for _, _, e in items:
            for rt in self.books:
                rt.venue.observe(e)

    def _own_fill_blocks(self, b: Block) -> frozenset[Block]:
        return frozenset(f.block for rt in self.books for f in rt.state.fills if f.block > b - FILLS_WINDOW_BLOCKS)

    def _poll_control(self, b: Block) -> list[OperatorCommand]:
        if self.control is None:
            return []
        first = self.books[0].state if self.books else None
        return self.control.poll(b, halted=first.halted if first is not None else False,
                                 resume_count=first.resume_count if first is not None else 0,
                                 is_new=lambda idem: not self.journal.has_idem(idem))

    def _checkpoint(self, b: Block) -> None:
        if self.checkpoints is None or self.checkpoint_every_blocks <= 0 or not self.books:
            return
        if self._last_cp_block is not None and b - self._last_cp_block < self.checkpoint_every_blocks:
            return
        seq = self.journal.head()[0]
        fd = self.features.state_digest()
        for rt in self.books:
            h, blob = encode_state(rt.state)
            self.checkpoints.save(Checkpoint(rt.book, seq, h, blob, fd))
        self._last_cp_block = b

    def close(self) -> None:
        """Release the commit thread (the journal and the data source belong to the caller)."""
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # ------------------------------------------------------------------ misc
    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    def _alert(self, kind: str, msg: str) -> None:
        log.warning("%s: %s", kind, msg)
        if self.on_alert is not None:
            self.on_alert(kind, msg)

    def _check_alive(self) -> None:
        if self._poisoned:
            raise RunnerPoisoned("this Runner failed part-way through a tick; restart and recover")
