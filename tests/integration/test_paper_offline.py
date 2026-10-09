"""Section 10.3 item 7: paper <-> offline equality - the replay of a recorded paper session reproduces its decisions.

The paper side is the production paper wiring: Runner(RunMode.PAPER) + PaperVenue (exact N+2 fills on recorded
finalized blocks, sim_swap drift probe) + a durable SqliteJournal + data.recorder.Recorder (hot staging, fsync before
the journal may reference a snapshot; compaction to the lake at the end). The "live feed" replays real recorded chain
data - the SN116 prune-window fixture: 60-block FULL snapshots, then every block of the per-block window (HEAD reads
with a FULL every 60 blocks) - through a feed that records each snapshot before yielding it, exactly as
LiveChainFeed(on_snapshot=recorder.aon_snapshot) does. The drift probe's reader answers sim_swap from the recorded
pools (no drift). Books: the EW baseline (paper stage) and a holder of SN116 whose ~1% position is journaled before
the session (as in test_prune_replay: the 1-hour fixture has no router history, so no fresh entry passes the
universe floor); the overlay's Tier A exit then drives real paper orders - stride-gap N+2 blocks missed, per-block
N+2 blocks filled exactly.

Offline: FRESH engines, venues, feature engine and a store built only from the recorded lake recover the paper
journal: every batch is re-decided in VERIFY mode (ReplayDivergence on any byte difference), nothing new is written,
and the money state is identical.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

from taotrader.backtest.books import build_engine, calibration_provider, feature_engine, load_backtest_plan
from taotrader.core.config import RunCfg, SleeveCfg
from taotrader.core.events import (
    CapitalChanged,
    ChainEventKind,
    ConfigApplied,
    FillReported,
    HealthObs,
    JournalEvent,
    OrderIntended,
    SubmitStarted,
    VenueAck,
)
from taotrader.core.fixed import DEC
from taotrader.core.orders import Fill, OrderIntent, OrderKind, Urgency, make_order_id
from taotrader.core.protocols import ChainReader, SnapshotStore, SourceItem, SwapSim, TickContext
from taotrader.core.signals import Signal, SignalKind, StrategyOutput
from taotrader.core.state import ChainSnapshot
from taotrader.core.units import (
    PPM,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    LogicalTime,
    Phase,
    Ppm,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
)
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.data.recorder import HotStaging, Recorder
from taotrader.data.store import LakeSnapshotStore
from taotrader.engine.recovery import BookRuntime
from taotrader.engine.reducer import money_digest
from taotrader.engine.runner import Runner
from taotrader.protocol.amm import quote_buy, quote_sell
from taotrader.risk.overlay import StandardOverlay
from taotrader.venues.paper import PaperVenue

SESSION = (9_207_000, 9_210_600)
B0 = 9_206_400
VICTIM = 116
HASHES = ("paper-eq-config", "paper-eq-code", "paper-eq-prereg")


class Holder:
    """Keeps a TARGET on SN116 at 1% of its pool TAO."""

    def __init__(self) -> None:
        self.id = StrategyId("test.holder")
        self.decide_every_blocks = 300
        self.wake_on: frozenset[ChainEventKind] = frozenset()
        self.min_cadence_blocks = 1
        self.valid_from_block = Block(0)
        self.declares_dilution = False

    def initial_memory(self) -> object:
        return 0

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        s = ctx.raw.by_netuid(VICTIM)
        if s is None:
            return StrategyOutput((), memory)
        return StrategyOutput((Signal(self.id, s.key, ctx.block, SignalKind.TARGET, weight_ppm=Ppm(PPM),
                                      max_size_rao=Rao(int(s.pool.tao) // 100)),), memory)


class RecordedFeed:
    """A finalized-head feed over recorded snapshots that records every snapshot (fsync) before yielding it."""

    def __init__(self, src: LakeSnapshotStore, blocks: list[int], recorder: Recorder | None, store: SnapshotStore) -> None:
        self.src, self.blocks, self.recorder, self.store = src, blocks, recorder, store
        self.cadence_blocks = 1
        self.by_hash: dict[str, ChainSnapshot] = {}

    async def stream(self, after: Block | None) -> AsyncIterator[SourceItem]:
        for b in self.blocks:
            if after is not None and b <= after:
                continue
            (snap,) = self.src.fetch([b])
            if self.recorder is not None:
                await self.recorder.aon_snapshot(snap)
            self.by_hash[str(snap.block_hash)] = snap
            yield SourceItem(snapshot=snap, health=HealthObs.nominal())

    async def aclose(self) -> None:
        return None


class RecordedReader:
    """ChainReader stand-in for the paper drift probe: sim_swap answered from the recorded pool at that block hash."""

    def __init__(self, feed: RecordedFeed) -> None:
        self.feed = feed

    async def sim_swap_buy(self, netuid: int, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        s = self.feed.by_hash[str(block_hash)].by_netuid(netuid)
        assert s is not None
        q = quote_buy(s.pool, Rao(tao_rao))
        return SwapSim(tao_rao, int(q.amount_out), int(q.fee), 0, 0, 0)

    async def sim_swap_sell(self, netuid: int, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        s = self.feed.by_hash[str(block_hash)].by_netuid(netuid)
        assert s is not None
        q = quote_sell(s.pool, AlphaRao(alpha_rao))
        return SwapSim(int(q.amount_out), alpha_rao, 0, int(q.fee), 0, 0)

    async def block_hash(self, block: Block) -> BlockHash:
        raise RuntimeError("no network in this test")

    async def snapshot(self, *a: Any, **k: Any) -> ChainSnapshot:
        raise RuntimeError("no network in this test")


def _paper_run(itx: Any) -> RunCfg:
    plan = load_backtest_plan(env={})
    ew = plan.book("base-ew-total")
    ew = replace(ew, sleeves=tuple(replace(s, stage=Stage.PAPER) for s in ew.sleeves))
    holder = replace(ew, book=BookId("paper-holder"), capital_rao=Rao(10_000 * 10**9),
                     sleeves=(SleeveCfg(StrategyId("test.holder"), Stage.PAPER, Ppm(PPM)),))
    return replace(plan.run, run_id="paper-eq", mode=RunMode.PAPER, books=(ew, holder))


def _seed(journal: SqliteJournal, run: RunCfg, snap: ChainSnapshot) -> None:
    """ConfigApplied + the holder's ~1% SN116 position, journaled before the session (B0, fill at B0 + 5)."""
    book = next(b for b in run.books if b.book == "paper-holder")
    s = snap.by_netuid(VICTIM)
    assert s is not None and s.hotkeys
    h = max(s.hotkeys, key=lambda x: (x.total_alpha, x.hotkey))
    tao = Rao(int(s.pool.tao) // 100)
    q = quote_buy(s.pool, tao)
    oid = make_order_id(run.run_id, book.book, Block(B0), s.key, h.hotkey, OrderKind.ADD_STAKE_LIMIT, 0)
    intent = OrderIntent(oid, 0, book.book, Block(B0), OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao, AlphaRao(0), False,
                         PriceRao(s.pool.spot_rao() * 2), False, True, Block(B0 + 5), int(q.amount_out), Urgency.NORMAL,
                         ((StrategyId("test.holder"), Ppm(PPM)),), "test.synthetic_holding")
    fill = Fill(f"{oid}:0:0", oid, 0, book.book, Block(B0 + 5), OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao,
                AlphaRao(int(q.amount_out)), DEC.divide(Decimal(int(q.amount_out)), h.index()), int(q.fee), Rao(0),
                Rao(book.exec.buy_tx_fee_rao), int(q.d_tao), int(q.d_alpha), s.pool.spot_rao(), Ppm(0), True)
    ew = next(b for b in run.books if b.book != "paper-holder")
    first: list[tuple[LogicalTime, BookId, JournalEvent]] = [
        (LogicalTime(Block(B0), Phase.INGEST, 0), BookId(""), ConfigApplied(Block(B0), *HASHES)),
        (LogicalTime(Block(B0), Phase.INGEST, 0), ew.book,
         CapitalChanged(ew.book, Block(B0), int(ew.capital_rao), int(ew.fee_float_rao), "initial")),
        (LogicalTime(Block(B0), Phase.INGEST, 0), book.book,
         CapitalChanged(book.book, Block(B0), int(book.capital_rao), int(book.fee_float_rao), "initial")),
        (LogicalTime(Block(B0), Phase.EMIT, 0), book.book, OrderIntended(intent)),
        (LogicalTime(Block(B0), Phase.OUTBOX, 0), book.book, SubmitStarted(book.book, oid, 0, "sim0", None, Block(B0 + 13))),
        (LogicalTime(Block(B0), Phase.OUTBOX, 1), book.book, VenueAck(book.book, oid, 0, Block(B0), Block(B0 + 5), "", ""))]
    journal.append_batch(first)
    journal.append_batch([(LogicalTime(Block(B0 + 5), Phase.VENUE, 0), book.book, FillReported(fill))])


def _runner(run: RunCfg, feed: RecordedFeed, journal: SqliteJournal, store: SnapshotStore) -> Runner:
    cal = calibration_provider(None)
    ov = StandardOverlay(cal, run_mode=run.mode, seed=run.seed)
    rts = [BookRuntime(engine=build_engine(b, run, cal, ov,
                                           extra_strategies=[Holder()] if b.book == "paper-holder" else ()),
                       venue=PaperVenue(b.book, b.exec, reader=cast(ChainReader, RecordedReader(feed)), seed=run.seed,
                                        store=store))
           for b in run.books]
    return Runner(run_id=run.run_id, mode=run.mode, source=feed, journal=journal, features=feature_engine(cal, warm_blocks=0),
                  books=rts, features_factory=lambda: feature_engine(cal, warm_blocks=0), config_hash=HASHES[0],
                  code_hash=HASHES[1], prereg_hash=HASHES[2])


@pytest.fixture(scope="module")
def prune_store(itx: Any) -> Any:
    lake_dir, state = itx.PRUNE_LAKE
    if not (lake_dir.is_dir() and state.is_file()):
        pytest.fail("the prune-window fixture is missing (tests/fixtures/minilake/build_prune_window.py)")
    lk = Lake(lake_dir, state)
    st = LakeSnapshotStore(lk, None, merge_refined=True)
    yield st
    lk.close()


def test_replay_of_a_recorded_paper_session_reproduces_its_decisions(itx: Any, prune_store: Any, tmp_path: Path) -> None:
    blocks = prune_store.selected_blocks(*SESSION)
    assert len(blocks) > 150 and any(b2 - b1 == 1 for b1, b2 in pairwise(blocks))
    run = _paper_run(itx)
    # ---- the paper session (recording)
    rec_lake = Lake(tmp_path / "data" / "lake")
    hot = HotStaging(tmp_path / "data" / "hot", writable=True)
    journal = SqliteJournal(str(tmp_path / "journal.sqlite"))           # WAL + synchronous=FULL (paper)
    _seed(journal, run, prune_store.fetch([B0])[0])
    recorder = Recorder(tmp_path / "data" / "hot", rec_lake, committed_block=journal.head_block, hot=hot)
    run_store = LakeSnapshotStore(rec_lake, hot)
    feed = RecordedFeed(prune_store, blocks, recorder, run_store)
    paper = _runner(run, feed, journal, run_store)
    summary = asyncio.run(paper.run())
    paper.close()
    assert summary.ticks == len(blocks)
    money = {str(rt.book): money_digest(rt.state) for rt in paper.books}
    fills = sum(len(rt.state.fills) for rt in paper.books)
    kinds = [r.kind for r in journal.read(1)]
    assert kinds.count("order_intended") > 1 and kinds.count("submit_started") > 1, "the session must place orders"
    paper_fills = [decode_record(r) for r in journal.read(1) if r.kind == "fill_reported"]
    assert any(isinstance(e, FillReported) and e.fill.exact_block and int(e.fill.block) > B0 + 5 for e in paper_fills), \
        "an exact N+2 paper fill inside the per-block window"
    head = journal.head()
    recorder.compact(final=True)
    recorder.close()
    journal.close()
    rec_lake.close()
    # ---- offline: fresh objects, the recorded lake only (hot staging compacted away)
    lake2 = Lake(tmp_path / "data" / "lake")
    store2 = LakeSnapshotStore(lake2, None)
    assert {int(r.block) for r in lake2.snapshot_refs()} == set(blocks)
    j2 = SqliteJournal(str(tmp_path / "journal.sqlite"))
    feed2 = RecordedFeed(prune_store, blocks, None, store2)
    offline = _runner(run, feed2, j2, store2)
    res = asyncio.run(offline.recover())
    assert res.records == head[0] and res.verified_ticks == res.ticks == len(blocks) and not res.drift
    again = asyncio.run(offline.run())
    offline.close()
    assert again.ticks == 0 and j2.head() == head                   # nothing new: every decision reproduced
    assert {str(rt.book): money_digest(rt.state) for rt in offline.books} == money
    assert sum(len(rt.state.fills) for rt in offline.books) == fills
    j2.close()
    lake2.close()
