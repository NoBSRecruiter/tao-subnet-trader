"""taotrader/backtest/runner.py - single backtest passes, the process-pool grid and the trial registry (WP10; DESIGN 8).

A backtest pass IS the production engine.runner.Runner with data.replay.ParquetReplay + one venues.sim.SimVenue per
book + a SqliteJournal (":memory:" or a file, synchronous=OFF) + HealthObs.nominal(). There is no separate backtest
loop: `arun_pass` calls Runner.recover() and then Runner.tick() for every replay item, exactly as Runner.run() does,
so that the metrics observer (backtest.metrics.NavRecorder) can value every book after each tick.

Run identity (section 8): (code hash, config hash, preregistration hash, data manifest hash, seed). The Runner journals
ConfigApplied(config, code, prereg); a pass is bit-reproducible (identical journal hash chains, integration test 5).
Per-book run digests (`book_digests`) hash only the records journaled under that book (kind, version, canonical
payload), so they are independent of the code hash in ConfigApplied and pin each book's decisions and money facts.

Trial registry (section 8.9): every evaluated (book, config, data range) writes one `trial` row (run-state DDL of
data.schema) - `TrialRegistry.record`; reports print `TrialRegistry.count()`.

Grid: `run_grid` fans jobs out over a ProcessPoolExecutor (<= 8 workers, section 8.12); each worker loads its plan
through ops.config_load, opens the lake read-only and returns a picklable GridResult (per-book metrics).
Sensitivity / capacity variants (section 8.9): `sensitivity_books` (impact x dereg x fees x2 x latency x2) and
`capacity_books` (capital in {1, 3, 10, 30, 100, 300} TAO).
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from ..core.config import BookCfg, RunCfg
from ..core.events import HealthObs
from ..core.protocols import Journal, RiskOverlay, Strategy
from ..core.state import ChainSnapshot
from ..core.units import RAO_PER_TAO, Block, BookId, Rao
from ..data.journal import SqliteJournal, open_run_state
from ..data.lake import Lake
from ..data.replay import ParquetReplay
from ..engine.reducer import money_digest
from ..engine.runner import AlertHook, FaultHook, Runner
from ..ops.config_load import DEFAULT_CONFIG, config_hash, prereg_hash
from ..protocol.calibration import CalibrationProvider
from .books import BOOKS_BACKTEST, BacktestPlan, calibration_provider, feature_engine, load_backtest_plan, wire_books
from .metrics import BookMetrics, NavRecorder, TickPoint, book_metrics

__all__ = [
    "CAPACITY_TAO", "BookOutcome", "GridJob", "GridResult", "PassResult", "RunIdentity", "TrialRegistry", "arun_pass",
    "book_digests", "capacity_books", "code_hash", "run_grid", "run_pass", "sensitivity_books",
]

SRC_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CAPACITY_TAO: Final[tuple[int, ...]] = (1, 3, 10, 30, 100, 300)
MAX_WORKERS: Final[int] = 8


def code_hash(root: Path = SRC_ROOT) -> str:
    """blake2b-256 over every source file of the package (sorted relative paths, LF-normalised content)."""
    h = hashlib.blake2b(digest_size=32)
    for p in sorted(root.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts or p.suffix not in (".py", ".json"):
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class RunIdentity:
    code_hash: str
    config_hash: str
    prereg_hash: str
    manifest_hash: str
    seed: int

    def run_key(self) -> str:
        text = "|".join((self.code_hash, self.config_hash, self.prereg_hash, self.manifest_hash, str(self.seed)))
        return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def book_digests(journal: Journal) -> dict[str, str]:
    """Per-book digest of the journal: blake2b-128 over (kind, version, payload) of the records journaled under each
    book, in journal order. Run-level records (book "") are not included."""
    hs: dict[str, Any] = {}
    for rec in journal.read(1):
        if not rec.book:
            continue
        h = hs.get(rec.book)
        if h is None:
            h = hs[rec.book] = hashlib.blake2b(digest_size=16)
        h.update(rec.kind.encode())
        h.update(b"|")
        h.update(str(rec.version).encode())
        h.update(b"|")
        h.update(rec.payload)
        h.update(b"\n")
    return {k: v.hexdigest() for k, v in sorted(hs.items())}


# ------------------------------------------------------------------------------------------------ trial registry
class TrialRegistry:
    """The section 8.9 trial registry: one row per evaluation (book x config x data range x purpose)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db: sqlite3.Connection = open_run_state(path, durable=False)

    def record(self, *, cfg_hash: str, strategy: str, data_range: str, purpose: str, identity: str = "") -> str:
        trial_id = hashlib.blake2b(f"{identity}|{cfg_hash}|{strategy}|{data_range}|{purpose}".encode(),
                                   digest_size=16).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO trial (trial_id, cfg_hash, strategy, data_range, purpose, wall_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (trial_id, cfg_hash, strategy, data_range, purpose, int(time.time())))
        return trial_id

    def count(self, strategy: str | None = None) -> int:
        if strategy is None:
            row = self.db.execute("SELECT count(*) FROM trial").fetchone()
        else:
            row = self.db.execute("SELECT count(*) FROM trial WHERE strategy = ?", (strategy,)).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        self.db.close()


# ------------------------------------------------------------------------------------------------ one pass
@dataclass(frozen=True, slots=True)
class BookOutcome:
    book: str
    digest: str                         # per-book run digest
    money_digest: str                   # reducer.money_digest of the final state
    points: tuple[TickPoint, ...]
    metrics: BookMetrics
    orphans: int
    breaches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PassResult:
    identity: RunIdentity
    run_id: str
    ticks: int
    first_block: int | None
    last_block: int | None
    journal_head: tuple[int, str]
    books: Mapping[str, BookOutcome]
    alerts: tuple[tuple[str, str], ...] = ()

    def digests(self) -> dict[str, str]:
        return {k: v.digest for k, v in sorted(self.books.items())}


def identity_of(run: RunCfg, lake: Lake | None) -> RunIdentity:
    return RunIdentity(code_hash=code_hash(), config_hash=config_hash(run), prereg_hash=prereg_hash(),
                       manifest_hash=lake.manifest_hash() if lake is not None else "", seed=int(run.seed))


async def arun_pass(plan: BacktestPlan, lake: Lake, *, books: Sequence[str] | None = None, start: int | None = None,
                    end: int | None = None, warmup_blocks: int | None = None, stride: int | None = None,
                    journal: Journal | None = None, calibration: CalibrationProvider | None = None,
                    overlay: RiskOverlay | None = None, extra_strategies: Mapping[str, Sequence[Strategy]] | None = None,
                    overlays: Mapping[str, RiskOverlay] | None = None,
                    fault: FaultHook | None = None, max_ticks: int | None = None, observe: bool = True,
                    health: HealthObs | None = None, identity: RunIdentity | None = None,
                    on_tick: Callable[[Runner, ChainSnapshot], None] | None = None) -> PassResult:
    """Run (or resume, when `journal` already holds records) one pass over [start, end] of the lake."""
    run = plan.run if books is None else plan.subset(books).run
    cal = calibration if calibration is not None else calibration_provider(lake, hazard_invalid_from=plan.hazard_invalid_from)
    lo = plan.start_block if start is None else start
    hi_default = plan.end_block or _lake_last_block(lake)
    hi = hi_default if end is None else end
    st = stride if stride is not None else plan.stride_blocks
    source = ParquetReplay(lake, lo, hi, st, warmup_blocks=plan.warmup_blocks if warmup_blocks is None else warmup_blocks,
                           health=health)
    jr: Journal = journal if journal is not None else SqliteJournal(":memory:", durable=False)
    ident = identity if identity is not None else identity_of(run, lake)
    rts = wire_books(run, cal, overlay=overlay, extra_strategies=extra_strategies, overlays=overlays)
    fe = feature_engine(cal, plan)
    alerts: list[tuple[str, str]] = []

    def on_alert(kind: str, msg: str) -> None:
        alerts.append((kind, msg))

    runner = Runner(run_id=run.run_id, mode=run.mode, source=source, journal=jr, features=fe, books=rts,
                    features_factory=lambda: feature_engine(cal, plan), config_hash=ident.config_hash,
                    code_hash=ident.code_hash, prereg_hash=ident.prereg_hash, fault=fault,
                    on_alert=_alert_hook(on_alert), feature_warm_blocks=plan.feature_warm_blocks + st)
    rec = NavRecorder(jr) if observe else None
    first: int | None = None
    n = 0
    try:
        await runner.recover()
        if rec is not None:
            rec._seq = int(jr.head()[0])
        stream = source.stream(runner.last_block)
        try:
            async for item in stream:
                await runner.tick(item)
                if first is None:
                    first = int(item.snapshot.block)
                n += 1
                if rec is not None:
                    snap = item.snapshot
                    rec.observe(snap, [(str(rt.book), rt.state, rt.venue.mark_to(snap)) for rt in runner.books])
                if on_tick is not None:
                    on_tick(runner, item.snapshot)
                if max_ticks is not None and n >= max_ticks:
                    break
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
    finally:
        runner.close()
        await source.aclose()
    digests = book_digests(jr)
    outcomes: dict[str, BookOutcome] = {}
    for rt in runner.books:
        bk = str(rt.book)
        pts = tuple(rec.points(bk)) if rec is not None else ()
        outcomes[bk] = BookOutcome(book=bk, digest=digests.get(bk, ""), money_digest=money_digest(rt.state), points=pts,
                                   metrics=book_metrics(bk, pts, rec.trade_stats(bk) if rec is not None else None),
                                   orphans=rt.state.orphans, breaches=tuple(rt.state.breaches))
    head = jr.head()
    return PassResult(identity=ident, run_id=run.run_id, ticks=n, first_block=first,
                      last_block=None if runner.last_block is None else int(runner.last_block),
                      journal_head=(int(head[0]), head[1].hex()), books=outcomes, alerts=tuple(alerts))


def _alert_hook(fn: Callable[[str, str], None]) -> AlertHook:
    return fn


def run_pass(plan: BacktestPlan, lake: Lake, **kwargs: Any) -> PassResult:
    """Synchronous wrapper of arun_pass (one event loop per pass)."""
    return asyncio.run(arun_pass(plan, lake, **kwargs))


def _lake_last_block(lake: Lake) -> int:
    refs = lake.snapshot_refs()
    if not refs:
        raise ValueError("the lake holds no snapshots")
    return max(int(r.block) for r in refs)


# ------------------------------------------------------------------------------------------------ variants
def sensitivity_books(book: BookCfg, *, impacts: Sequence[tuple[str, int | None]] = (("t", 0), ("p", None)),
                      deregs: Sequence[tuple[str, str]] = (("f", "formula"), ("d35", "fixed:350000")),
                      fee_mults: Sequence[int] = (1, 2), latency_mults: Sequence[int] = (1, 2)) -> tuple[BookCfg, ...]:
    """The section 8.9 sensitivity grid around one book: impact x dereg x tx fees x{1,2} x latency x{1,2}. Latency
    scaling keeps the cross-field rule unwind_exec_blocks == finality_lag + latency."""
    out: list[BookCfg] = []
    for ilab, hl in impacts:
        for dlab, model in deregs:
            for fm in fee_mults:
                for lm in latency_mults:
                    ex = replace(book.exec, impact_half_life_blocks=hl, buy_tx_fee_rao=book.exec.buy_tx_fee_rao * fm,
                                 sell_tx_fee_rao=book.exec.sell_tx_fee_rao * fm, move_tx_fee_rao=book.exec.move_tx_fee_rao * fm,
                                 rotate_tx_fee_rao=book.exec.rotate_tx_fee_rao * fm,
                                 carrier_fee_rao=book.exec.carrier_fee_rao * fm,
                                 latency_blocks=book.exec.latency_blocks * lm)
                    rk = replace(book.risk, unwind_exec_blocks=ex.finality_lag_blocks + ex.latency_blocks)
                    out.append(replace(book, book=BookId(f"{book.book}-s-{ilab}-{dlab}-f{fm}-l{lm}"), exec=ex, risk=rk,
                                       dereg_model=model))
    return tuple(out)


def capacity_books(book: BookCfg, capitals_tao: Sequence[int] = CAPACITY_TAO) -> tuple[BookCfg, ...]:
    """The capacity sweep: the same book at each capital (fee float scaled with it, at least 1 TAO)."""
    out: list[BookCfg] = []
    for c in capitals_tao:
        cap = c * RAO_PER_TAO
        ff = max(RAO_PER_TAO, book.fee_float_rao * cap // max(book.capital_rao, 1))
        out.append(replace(book, book=BookId(f"{book.book}-cap{c}"), capital_rao=Rao(cap), fee_float_rao=Rao(ff)))
    return tuple(out)


# ------------------------------------------------------------------------------------------------ the grid
@dataclass(frozen=True, slots=True)
class GridJob:
    label: str
    lake: str
    paths: tuple[str, ...] = (str(DEFAULT_CONFIG), str(BOOKS_BACKTEST))
    cli: tuple[str, ...] = ()
    books: tuple[str, ...] | None = None
    start: int | None = None
    end: int | None = None
    warmup_blocks: int | None = None
    purpose: str = "grid"
    variants: tuple[tuple[str, str, int], ...] = ()   # (kind, base book, value): ("capacity", "carry", 30) adds the
                                                      # 30-TAO variant, ("sensitivity", "carry", 0) the 16-book grid;
                                                      # built in the worker (BookCfg params are not picklable)


def job_books(plan: BacktestPlan, variants: Sequence[tuple[str, str, int]]) -> tuple[BookCfg, ...]:
    """The variant books of a grid job, built from the plan's base books."""
    out: list[BookCfg] = []
    for kind, base, value in variants:
        if kind == "capacity":
            out.extend(capacity_books(plan.book(base), (value,)))
        elif kind == "sensitivity":
            out.extend(sensitivity_books(plan.book(base)))
        else:
            raise ValueError(f"unknown grid variant kind {kind!r}")
    return tuple(out)


def with_books(plan: BacktestPlan, extra: Sequence[BookCfg]) -> BacktestPlan:
    """The plan plus extra books (variant labels derived from each book)."""
    from types import MappingProxyType

    from .books import BookVariant, impact_label
    vs = dict(plan.variants)
    for b in extra:
        vs[b.book] = BookVariant(b.book, b.book, impact_label(b.exec.impact_half_life_blocks), b.dereg_model,
                                 tuple(s.strategy for s in b.sleeves))
    return replace(plan, run=replace(plan.run, books=plan.run.books + tuple(extra)), variants=MappingProxyType(vs))


@dataclass(frozen=True, slots=True)
class GridResult:
    label: str
    identity: RunIdentity
    digests: Mapping[str, str]
    metrics: Mapping[str, BookMetrics]
    ticks: int
    data_range: str
    trials: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)   # (trial_id, cfg_hash, book)


def _grid_worker(job: GridJob) -> GridResult:
    plan = load_backtest_plan(job.paths, env={}, cli=job.cli)
    extra = job_books(plan, job.variants)
    if extra:
        plan = with_books(plan, extra)
    books = job.books
    if books is not None:
        books = tuple(books) + tuple(b.book for b in extra)
    lake = Lake(job.lake, check_files=False)
    try:
        res = run_pass(plan, lake, books=books, start=job.start, end=job.end, warmup_blocks=job.warmup_blocks)
    finally:
        lake.close()
    rng = f"{job.start or plan.start_block}-{res.last_block}"
    return GridResult(label=job.label, identity=res.identity, digests=res.digests(),
                      metrics={k: v.metrics for k, v in res.books.items()}, ticks=res.ticks, data_range=rng)


def run_grid(jobs: Sequence[GridJob], *, max_workers: int = MAX_WORKERS, registry: TrialRegistry | None = None,
             in_process: bool = False) -> list[GridResult]:
    """Run every job (process pool, <= 8 workers; `in_process` for tests) and register one trial per (job, book)."""
    workers = max(1, min(max_workers, MAX_WORKERS, len(jobs)))
    if in_process or workers == 1:
        results = [_grid_worker(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_grid_worker, jobs))
    out: list[GridResult] = []
    for job, r in zip(jobs, results, strict=True):
        trials: list[tuple[str, str, str]] = []
        if registry is not None:
            for bk in sorted(r.metrics):
                tid = registry.record(cfg_hash=r.identity.config_hash, strategy=bk, data_range=r.data_range,
                                      purpose=job.purpose, identity=r.identity.run_key())
                trials.append((tid, r.identity.config_hash, bk))
        out.append(replace(r, trials=tuple(trials)))
    return out


def register_pass(registry: TrialRegistry, res: PassResult, purpose: str) -> int:
    """One trial row per book of a pass; returns the registry's trial count."""
    rng = f"{res.first_block}-{res.last_block}"
    for bk in sorted(res.books):
        registry.record(cfg_hash=res.identity.config_hash, strategy=bk, data_range=rng, purpose=purpose,
                        identity=res.identity.run_key())
    return registry.count()


def blocks_of(res: PassResult) -> tuple[Block, ...]:
    """The blocks of the first book's points (the pass's tick blocks)."""
    for b in sorted(res.books):
        return tuple(Block(p.block) for p in res.books[b].points)
    return ()


# ------------------------------------------------------------------------------------------------ crash matrix
FAULT_POINTS: Final[tuple[str, ...]] = ("pre_commit", "in_transaction", "post_commit", "drain_post_commit",
                                        "after_submit_started", "after_venue_submit", "outbox_post_commit")


class InjectedCrash(BaseException):
    """Simulated process death at a fault point (BaseException, so no `except Exception` swallows it)."""


class _CrashJournal(SqliteJournal):
    """SqliteJournal whose transaction can be interrupted after BEGIN IMMEDIATE (the in-transaction fault point)."""

    def __init__(self, path: str, on_txn: Callable[[], None]) -> None:
        super().__init__(path, durable=False)
        self._on_txn = on_txn

    def _missing_triggers(self) -> list[str]:          # runs inside the write transaction
        self._on_txn()
        return super()._missing_triggers()


@dataclass(frozen=True, slots=True)
class CrashSpec:
    """A picklable pass description for the crash matrix (the plan is rebuilt from config paths + overrides)."""
    lake: tuple[str, str]
    cli: tuple[str, ...]
    books: tuple[str, ...]
    start: int
    end: int
    paths: tuple[str, ...] = (str(DEFAULT_CONFIG), str(BOOKS_BACKTEST))


@dataclass(frozen=True, slots=True)
class CrashOutcome:
    point: str
    occurrence: int
    crashed: bool
    money: Mapping[str, str]
    intents: tuple[tuple[str, int], ...]
    fills: tuple[str, ...]
    duplicate_intents: int
    duplicate_fills: int
    error: str = ""


def _journal_facts(journal: Journal) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...], int, int]:
    from ..data.journal import decode_record
    intents: list[tuple[str, int]] = []
    fills: list[str] = []
    for rec in journal.read(1):
        if rec.kind == "order_intended":
            ev: Any = decode_record(rec)
            intents.append((str(ev.intent.order_id), int(ev.intent.attempt)))
        elif rec.kind == "fill_reported":
            ev = decode_record(rec)
            fills.append(str(ev.fill.fill_id))
    return (tuple(sorted(intents)), tuple(sorted(fills)), len(intents) - len(set(intents)), len(fills) - len(set(fills)))


def _spec_plan(spec: CrashSpec) -> BacktestPlan:
    return load_backtest_plan(spec.paths, env={}, cli=spec.cli)


def enumerate_fault_points(spec: CrashSpec, journal_path: str) -> tuple[list[tuple[str, int]], CrashOutcome]:
    """Run the pass once crash-free, counting every fault point hit: [(point, occurrence)] and the reference outcome."""
    seen: dict[str, int] = {}
    order: list[tuple[str, int]] = []

    def hit(point: str) -> None:
        k = seen.get(point, 0)
        seen[point] = k + 1
        order.append((point, k))

    plan = _spec_plan(spec)
    jr = _CrashJournal(journal_path, lambda: hit("in_transaction"))
    lake = Lake(*spec.lake)
    try:
        res = run_pass(plan, lake, books=list(spec.books), start=spec.start, end=spec.end, journal=jr, fault=hit,
                       observe=False)
        intents, fills, di, df = _journal_facts(jr)
    finally:
        lake.close()
        jr.close()
    ref = CrashOutcome("none", 0, False, {k: v.money_digest for k, v in sorted(res.books.items())}, intents, fills, di, df)
    return order, ref


def crash_case(args: tuple[CrashSpec, str, int, str]) -> CrashOutcome:
    """Crash at the `occurrence`-th hit of `point`, then restart with FRESH objects (plan, lake, books, venues,
    features, journal handle) on the same journal file and finish the pass (section 10.3 item 4)."""
    spec, point, occurrence, journal_path = args
    count = [0]

    def maybe(p: str) -> None:
        if p == point:
            if count[0] == occurrence:
                count[0] += 1
                raise InjectedCrash(f"{point}#{occurrence}")
            count[0] += 1

    plan = _spec_plan(spec)
    crashed = False
    jr = _CrashJournal(journal_path, lambda: maybe("in_transaction"))
    lake = Lake(*spec.lake)
    try:
        run_pass(plan, lake, books=list(spec.books), start=spec.start, end=spec.end, journal=jr, fault=maybe, observe=False)
    except InjectedCrash:
        crashed = True
    finally:
        lake.close()
        jr.close()
    plan2 = _spec_plan(spec)
    jr2 = SqliteJournal(journal_path, durable=False)
    lake2 = Lake(*spec.lake)
    try:
        jr2.verify_chain()
        res = run_pass(plan2, lake2, books=list(spec.books), start=spec.start, end=spec.end, journal=jr2, observe=False)
        intents, fills, di, df = _journal_facts(jr2)
        return CrashOutcome(point, occurrence, crashed, {k: v.money_digest for k, v in sorted(res.books.items())},
                            intents, fills, di, df)
    except Exception as e:                                # reported, never raised (the matrix collects failures)
        return CrashOutcome(point, occurrence, crashed, {}, (), (), 0, 0, f"{type(e).__name__}: {e}")
    finally:
        lake2.close()
        jr2.close()


def sample_fault_points(points: Sequence[tuple[str, int]], n: int | None) -> list[tuple[str, int]]:
    """Every point when n is None; else n points spread over the fault-point KINDS: each kind gets an equal quota
    (a kind with fewer hits gives its unused quota to the others) filled with evenly spaced occurrences, so every kind
    is crashed at its first, middle and last hits as far as the quota allows. Deterministic; journal order kept."""
    if n is None or n >= len(points):
        return list(points)
    by_kind: dict[str, list[tuple[str, int]]] = {}
    for p in points:
        by_kind.setdefault(p[0], []).append(p)
    quota = dict.fromkeys(by_kind, 0)
    left = n
    while left > 0:
        open_kinds = [k for k in sorted(by_kind) if quota[k] < len(by_kind[k])]
        if not open_kinds:
            break
        share = max(1, left // len(open_kinds))
        for k in open_kinds:
            add = min(share, len(by_kind[k]) - quota[k], left)
            quota[k] += add
            left -= add
            if left == 0:
                break
    chosen: set[tuple[str, int]] = set()
    for k, q in quota.items():
        hits = by_kind[k]
        if q <= 0:
            continue
        if q == 1:
            chosen.add(hits[len(hits) // 2])
            continue
        for i in range(q):
            chosen.add(hits[round(i * (len(hits) - 1) / (q - 1))])
    return [p for p in points if p in chosen]


def run_crash_matrix(spec: CrashSpec, workdir: str | Path, *, samples: int | None = 150, workers: int = 4
                     ) -> tuple[CrashOutcome, list[CrashOutcome]]:
    """The crash matrix: enumerate the fault points of a crash-free run, crash at each sampled point (a fresh journal
    file per case), recover and finish. Returns (reference, outcomes). `samples=None` runs the full matrix."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    points, ref = enumerate_fault_points(spec, str(wd / "reference.sqlite"))
    chosen = sample_fault_points(points, samples)
    jobs = [(spec, p, k, str(wd / f"crash-{i:04d}.sqlite")) for i, (p, k) in enumerate(chosen)]
    if workers <= 1:
        outs = [crash_case(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=min(workers, MAX_WORKERS)) as ex:
            outs = list(ex.map(crash_case, jobs))
    return ref, outs
