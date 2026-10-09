"""taotrader/backtest/bias.py - bias canaries and placebos (WP10; DESIGN.md sections 8.10, 10.3 item 6).

Each control is a core.protocols.Strategy that runs through the production Engine like any sleeve:

- `PeekingCanary`: reads the store one stride AHEAD of ctx.block. The store's lookahead guard must raise
  LookaheadError; inside the Engine the strategy is skipped and the exception type is journaled ("engine.strategy_error"
  with detail LookaheadError) - `peek_errors` finds those actions in a journal.
- `OracleCanary`: holds the top-N names by REALISED forward return over `horizon_blocks` (a table computed from the
  lake before the run - `oracle_table`); it must show an implausible edge, proving the harness can see edges.
- `ShuffledSignals`: wraps a strategy and re-assigns its TARGET weights to a deterministic permutation (seeded by
  blake2b(seed | block_hash)) of the eligible universe; it must lose about the costs.
- `DelayedSignals`: wraps a strategy and releases each evaluation's signals `delay_blocks` later (the delayed-signal
  placebo; delays 0 < d1 < d2 must degrade monotonically).
- The random-entry placebo is strategies.baselines (baseline.random_entry).

Helpers: `truncation_agrees(journal_a, journal_b, k, latency)` (future truncation: runs on data[:k] and data[:k+m]
agree on every decision before k - latency), `decision_records(journal, before=b)`.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ..core import codec
from ..core.events import ChainEventKind, DecisionTrace, OrderIntended
from ..core.protocols import Journal, Strategy, TickContext
from ..core.signals import Signal, SignalKind, StrategyOutput
from ..core.units import PPM, Block, Ppm, StrategyId, SubnetKey
from ..data.journal import decode_record
from ..data.lake import Lake

__all__ = [
    "DelayedSignals", "OracleCanary", "PeekingCanary", "ShuffledSignals", "decision_records", "oracle_table",
    "peek_errors", "truncation_agrees",
]

DECISION_KINDS: Final[frozenset[str]] = frozenset({"decision_trace", "order_intended", "order_cancelled", "mode_changed",
                                                   "sleeve_transfer", "yield_accrued", "dereg_settled"})


class PeekingCanary:
    """A strategy that cheats: it asks the store for the snapshot one stride after ctx.block."""

    def __init__(self, strategy_id: str = "canary.peek", *, stride: int = 60) -> None:
        self.id = StrategyId(strategy_id)
        self.decide_every_blocks = stride
        self.wake_on: frozenset[ChainEventKind] = frozenset()
        self.min_cadence_blocks = stride
        self.valid_from_block = Block(0)
        self.declares_dilution = False
        self.stride = stride
        self.peeks = 0

    def initial_memory(self) -> object:
        return 0

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        self.peeks += 1
        ctx.store.at(Block(ctx.block + self.stride))          # must raise LookaheadError
        return StrategyOutput((), int(memory) + 1 if isinstance(memory, int) else 1)


def peek_errors(journal: Journal, strategy_id: str = "canary.peek") -> list[tuple[int, str]]:
    """(block, detail) of every journaled engine.strategy_error action naming `strategy_id`."""
    out: list[tuple[int, str]] = []
    for rec in journal.read(1):
        if rec.kind != "decision_trace":
            continue
        ev = decode_record(rec)
        assert isinstance(ev, DecisionTrace)
        for a in ev.actions:
            if a.rule == "engine.strategy_error" and strategy_id in a.detail:
                out.append((int(ev.block), a.detail))
    return out


# ------------------------------------------------------------------------------------------------ oracle
def oracle_table(lake: Lake, lo: int, hi: int, horizon_blocks: int) -> dict[int, dict[SubnetKey, float]]:
    """block -> {key: realised forward log price return over horizon_blocks} for every stored base-series block in
    [lo, hi] whose block + horizon is stored too (the oracle's deliberate lookahead; computed outside the store guard).
    Spot = (w_base / w_quote) * px_tao / px_alpha."""
    con = lake.connect()
    try:
        rows = con.execute(
            "SELECT block, netuid, reg_at, px_tao::DOUBLE, px_alpha::DOUBLE, w_quote_e18::DOUBLE FROM v_subnet "
            "WHERE block BETWEEN ? AND ? AND px_alpha > 0 AND w_quote_e18 > 0 ORDER BY block, netuid",
            [lo, hi + horizon_blocks]).fetchall()
    finally:
        con.close()
    import math
    px: dict[int, dict[SubnetKey, float]] = {}
    for b, n, r, pt, pa, wq in rows:
        spot = (1e18 - wq) / wq * pt / pa
        if spot > 0:
            px.setdefault(int(b), {})[SubnetKey(int(n), int(r))] = spot  # type: ignore[arg-type]
    out: dict[int, dict[SubnetKey, float]] = {}
    for b in sorted(px):
        if b > hi:
            break
        fut = px.get(b + horizon_blocks)
        if fut is None:
            continue
        out[b] = {k: math.log(fut[k] / v) for k, v in px[b].items() if k in fut}
    return out


class OracleCanary:
    """Holds the top-N names by realised forward return (see oracle_table). Equal weights, TARGET signals only."""

    def __init__(self, table: Mapping[int, Mapping[SubnetKey, float]], *, strategy_id: str = "canary.oracle",
                 every_blocks: int = 300, top_n: int = 4, stride: int = 60) -> None:
        self.id = StrategyId(strategy_id)
        self.decide_every_blocks = every_blocks
        self.wake_on: frozenset[ChainEventKind] = frozenset()
        self.min_cadence_blocks = stride
        self.valid_from_block = Block(0)
        self.declares_dilution = False
        self.table = table
        self.top_n = top_n

    def initial_memory(self) -> object:
        return 0

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        fwd = self.table.get(int(ctx.block), {})
        live = {s.key for s in ctx.raw.subnets}
        ranked = sorted(((v, k) for k, v in fwd.items() if k in live and v > 0), key=lambda t: (-t[0], t[1].netuid))
        picks = [k for _, k in ranked[: self.top_n]]
        sigs: list[Signal] = []
        held = {p.key for p in ctx.portfolio.positions}
        w = Ppm(PPM // max(len(picks), 1))
        for k in sorted(picks, key=lambda x: (x.netuid, x.reg_at)):
            sigs.append(Signal(self.id, k, ctx.block, SignalKind.TARGET, weight_ppm=w, reasons=("oracle",)))
        for k in sorted(held - set(picks), key=lambda x: (x.netuid, x.reg_at)):
            sigs.append(Signal(self.id, k, ctx.block, SignalKind.EXIT, reasons=("oracle.exit",)))
        return StrategyOutput(tuple(sigs), memory)


# ------------------------------------------------------------------------------------------------ placebo wrappers
@dataclass(frozen=True, slots=True)
class WrapMemory:
    inner: bytes = b""                                   # canonical bytes of the wrapped strategy's memory
    pending: tuple[tuple[int, tuple[Signal, ...]], ...] = ()   # DelayedSignals: (release block, signals), ascending
    current: tuple[Signal, ...] = ()                     # DelayedSignals: the last released signals (re-emitted)
    last_bucket: int = -1                                # DelayedSignals: block // inner.decide_every of the last run


def _decode_inner(inner: Strategy, raw: bytes) -> object:
    init = inner.initial_memory()
    return init if not raw else codec.decode_bytes(type(init), raw)


class _Wrapper:
    def __init__(self, inner: Strategy, strategy_id: str) -> None:
        self.inner = inner
        self.id = StrategyId(strategy_id)
        self.decide_every_blocks = inner.decide_every_blocks
        self.wake_on = inner.wake_on
        self.min_cadence_blocks = inner.min_cadence_blocks
        self.valid_from_block = inner.valid_from_block
        self.declares_dilution = inner.declares_dilution

    def initial_memory(self) -> object:
        return WrapMemory(codec.canonical_bytes(self.inner.initial_memory()))

    def _run_inner(self, ctx: TickContext, memory: object) -> tuple[tuple[Signal, ...], bytes]:
        mem = memory if isinstance(memory, WrapMemory) else WrapMemory()
        out = self.inner.on_tick(ctx, _decode_inner(self.inner, mem.inner))
        return out.signals, codec.canonical_bytes(out.memory)

    def _relabel(self, s: Signal) -> Signal:
        from dataclasses import replace
        return replace(s, strategy=self.id)


class ShuffledSignals(_Wrapper):
    """The shuffled-signal placebo: the inner strategy's TARGET weights land on a seeded permutation of the snapshot's
    started, enabled subnets (EXIT signals follow held names only)."""

    def __init__(self, inner: Strategy, *, seed: int = 0, strategy_id: str | None = None) -> None:
        super().__init__(inner, strategy_id or f"placebo.shuffled.{inner.id}")
        self.seed = seed

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        from dataclasses import replace
        sigs, inner_mem = self._run_inner(ctx, memory)
        targets = [s for s in sigs if s.kind is SignalKind.TARGET]
        pool = sorted((s.key for s in ctx.raw.subnets if s.subtoken_enabled and s.emission_enabled),
                      key=lambda k: (k.netuid, k.reg_at))
        h = hashlib.blake2b(f"{self.seed}|{ctx.raw.block_hash}".encode(), digest_size=16).digest()
        order = sorted(pool, key=lambda k: hashlib.blake2b(h + f"{k.netuid}:{k.reg_at}".encode(), digest_size=8).digest())
        held = {p.key for p in ctx.portfolio.positions}
        out: list[Signal] = []
        new_keys = order[: len(targets)]
        for s, k in zip(sorted(targets, key=lambda x: (x.key.netuid, x.key.reg_at)), new_keys, strict=False):
            out.append(replace(s, strategy=self.id, key=k, hotkey_pref=None, max_size_rao=s.max_size_rao,
                               reasons=s.reasons + ("placebo.shuffled",)))
        for k in sorted(held - set(new_keys), key=lambda x: (x.netuid, x.reg_at)):
            out.append(Signal(self.id, k, ctx.block, SignalKind.EXIT, reasons=("placebo.shuffled.exit",)))
        return StrategyOutput(tuple(out), WrapMemory(inner_mem))


class DelayedSignals(_Wrapper):
    """The delayed-signal placebo: the wrapped strategy runs on its own cadence (or a wake event), and the signals it
    computes at block b are released at the first evaluation at or after b + delay_blocks; the last released set is
    re-emitted on every evaluation until a newer one is released (so standing targets persist as for the original).
    The wrapper itself evaluates every `min_cadence_blocks` (the data cadence). delay_blocks = 0 is the identity."""

    def __init__(self, inner: Strategy, delay_blocks: int, *, strategy_id: str | None = None) -> None:
        super().__init__(inner, strategy_id or f"placebo.delay{delay_blocks}.{inner.id}")
        if delay_blocks < 0:
            raise ValueError("delay_blocks must be >= 0")
        self.delay_blocks = delay_blocks
        if delay_blocks > 0:
            self.decide_every_blocks = inner.min_cadence_blocks

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        mem = memory if isinstance(memory, WrapMemory) else WrapMemory()
        if self.delay_blocks == 0:
            sigs, inner_mem = self._run_inner(ctx, mem)
            return StrategyOutput(tuple(self._relabel(s) for s in sigs), WrapMemory(inner_mem))
        pending = list(mem.pending)
        inner_mem, bucket = mem.inner, mem.last_bucket
        due = int(ctx.block) // self.inner.decide_every_blocks
        woke = any(e.kind in self.inner.wake_on for e in ctx.events)
        if due > bucket or woke:
            sigs, inner_mem = self._run_inner(ctx, mem)
            pending.append((int(ctx.block) + self.delay_blocks, tuple(self._relabel(s) for s in sigs)))
            bucket = due
        ready = [p for p in pending if p[0] <= ctx.block]
        current = ready[-1][1] if ready else mem.current
        keep = tuple(p for p in pending if p[0] > ctx.block)
        return StrategyOutput(current, WrapMemory(inner_mem, keep, current, bucket))


# ------------------------------------------------------------------------------------------------ truncation
def decision_records(journal: Journal, *, before: int | None = None) -> list[tuple[str, str, bytes]]:
    """(book, kind, canonical payload) of every decision-side record (traces, intents, cancels, modes, transfers,
    accruals, settlements) with block < before (all if None), in journal order."""
    out: list[tuple[str, str, bytes]] = []
    for rec in journal.read(1):
        if rec.kind not in DECISION_KINDS:
            continue
        if before is not None and rec.time.block >= before:
            continue
        out.append((str(rec.book), rec.kind, rec.payload))
    return out


def truncation_agrees(a: Journal, b: Journal, k: int, latency: int) -> tuple[bool, int]:
    """Future truncation (section 8.10): decisions before k - latency are byte-identical. Returns (ok, n compared)."""
    da = decision_records(a, before=k - latency)
    db = decision_records(b, before=k - latency)
    return da == db, len(da)


def intents_of(journal: Journal) -> list[OrderIntended]:
    out: list[OrderIntended] = []
    for rec in journal.read(1):
        if rec.kind == "order_intended":
            ev = decode_record(rec)
            assert isinstance(ev, OrderIntended)
            out.append(ev)
    return out


def signal_keys(sigs: Sequence[Signal]) -> list[tuple[int, int, str]]:
    return [(int(s.key.netuid), int(s.key.reg_at), s.kind.value) for s in sigs]
