"""taotrader/strategies/base.py - shared strategy plumbing (WP9; DESIGN.md sections 2.0, 3.2, 4.4, 5.5, 5.10, 5.13).

Every strategy is PURE: no clock, I/O, randomness, environment or unsorted-set iteration. Its only state is the frozen
Memory dataclass it returns (journaled in DecisionTrace.memories; the Engine decodes it with
core.codec.decode_bytes(type(initial_memory()), raw)). Book-dependent inputs come ONLY from ctx.book_view and
ctx.portfolio (section 5.13 note 8). Bounded history comes from ctx.store, never past ctx.block. Floats are confined to
feature math and cross into Signals through core.fixed.to_ppm (section 5.13 note 3); sizes are integer rao from
protocol.amm.

This module holds:
- StrategyBase: the declared attributes of core.protocols.Strategy;
- parse_params: SleeveCfg.params -> a frozen Params dataclass. It is strict: unknown keys, wrong types and failed
  sanity checks raise ParamsError;
- sleeve_positions: this sleeve's executable holdings on ctx.view, as a pro-rata share of the one-shot liq_value of the
  physical position (the same rule the Engine uses for sleeve values);
- History: cached, generation-safe lookups into ctx.store on a 60-block grid. It gives flows over h blocks, per-epoch
  hotkey index returns, owner sales over a window and subnet states N blocks ago;
- floor_rows: the overlay universe floor, sections A-G (features.universe.evaluate), rebuilt from the frame and the raw
  snapshot. entry_blocks gives the book's cooldowns, entry halts and dissolving keys (sections 3.2 H and 3.4 bans);
- small numeric helpers (rank-gauss, robust SD, deterministic hashing) and Signal builders;
- build_strategy: the strategy-id -> implementation factory for the backtest and paper wiring (WP10, WP12).
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from statistics import NormalDist
from typing import Any, Final, TypeVar

from ..core.config import ExecCfg, RiskCfg, SleeveCfg
from ..core.events import ChainEvent, ChainEventKind
from ..core.fixed import DEC, floor_int, to_ppm
from ..core.orders import Urgency
from ..core.protocols import Strategy, TickContext
from ..core.signals import Signal, SignalKind
from ..core.state import ChainSnapshot, ReadPlan, SubnetState
from ..core.units import (
    BLOCKS_PER_DAY,
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    Hotkey,
    Ppm,
    PpmPerDay,
    Rao,
    StrategyId,
    SubnetKey,
)
from ..core.views import Feat, RouterCandidate
from ..features.universe import UniverseInputs, UniverseParams, UniverseRow, evaluate
from ..protocol.amm import liq_value
from ..protocol.calibration import Calibration, CalibrationProvider, check_asof
from ..protocol.derive import flow_valid_from, owner_position_delta
from ..protocol.prune import cost_ratio, p_registration

GRID_BLOCKS: Final[int] = 60                   # history sampling grid (the FULL-plan cadence; the backtest stride)
MAD_TO_SD: Final[float] = 1.4826               # robust SD = 1.4826 * MAD
_ND: Final[NormalDist] = NormalDist()
P = TypeVar("P")


class ParamsError(ValueError):
    """SleeveCfg.params do not validate into the strategy's Params dataclass."""


# ------------------------------------------------------------------------------------------------- params
def _coerce(tp: Any, value: object, path: str) -> object:
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ParamsError(f"{path}: expected bool, got {type(value).__name__}")
    if tp is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ParamsError(f"{path}: expected int, got {type(value).__name__}")
    if tp is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        raise ParamsError(f"{path}: expected a finite number, got {value!r}")
    if tp is str:
        if isinstance(value, str):
            return value
        raise ParamsError(f"{path}: expected str, got {type(value).__name__}")
    raise ParamsError(f"{path}: unsupported Params field type {tp!r}")


def parse_params(cls: type[P], raw: Mapping[str, object] | None, *, where: str = "params") -> P:
    """Validate free-form SleeveCfg.params into the frozen dataclass `cls` (fields of type bool / int / float / str).

    Unknown keys raise, ints are accepted for float fields (never bools), and the result's `problems()` (a list of
    messages; empty = valid) is checked. Missing keys keep the dataclass defaults (= DESIGN.md section 2)."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    hints = typing.get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    data = dict(raw or {})
    unknown = sorted(k for k in data if k not in fields)
    if unknown:
        raise ParamsError(f"{where}: unknown param(s) {unknown}")
    kwargs = {name: _coerce(hints[name], data[name], f"{where}.{name}") for name in sorted(data)}
    obj: Any = cls(**kwargs)
    check = getattr(obj, "problems", None)
    if callable(check):
        bad = list(check())
        if bad:
            raise ParamsError(f"{where}: " + "; ".join(bad))
    return typing.cast(P, obj)


def tao_to_rao(tao: float) -> int:
    """A TAO amount given as a float parameter -> integer rao, exactly via its decimal repr (floored)."""
    return floor_int(DEC.multiply(Decimal(repr(tao)), Decimal(RAO_PER_TAO)))


def days_to_blocks(days: float) -> int:
    return floor_int(DEC.multiply(Decimal(repr(days)), Decimal(BLOCKS_PER_DAY)))


def frac_to_ppm(x: float) -> int:
    return int(to_ppm(x))


# ------------------------------------------------------------------------------------------------- strategy base
class StrategyBase:
    """The declared attributes of core.protocols.Strategy. Subclasses implement initial_memory and on_tick."""

    def __init__(self, *, strategy_id: StrategyId, decide_every_blocks: int, wake_on: frozenset[ChainEventKind],
                 min_cadence_blocks: int, valid_from_block: Block, declares_dilution: bool) -> None:
        if decide_every_blocks < 1 or min_cadence_blocks < 1:
            raise ValueError("decide_every_blocks and min_cadence_blocks must be >= 1")
        self.id: StrategyId = strategy_id
        self.decide_every_blocks: int = decide_every_blocks
        self.wake_on: frozenset[ChainEventKind] = wake_on
        self.min_cadence_blocks: int = min_cadence_blocks
        self.valid_from_block: Block = valid_from_block
        self.declares_dilution: bool = declares_dilution

    def scheduled(self, block: int, last_scheduled: int | None) -> bool:
        """True when `block` opens a new absolute cadence bucket (the Engine's due rule without wake events)."""
        every = self.decide_every_blocks
        return last_scheduled is None or block // every > last_scheduled // every


def calibration_at(provider: CalibrationProvider | None, block: Block) -> Calibration | None:
    """provider.asof(block) with the as-of check (LookaheadError on a calibration fitted after the block)."""
    if provider is None:
        return None
    c = provider.asof(block)
    check_asof(c, block)
    return c


# ------------------------------------------------------------------------------------------------- holdings
@dataclass(frozen=True, slots=True)
class SleevePos:
    """This sleeve's share of one physical position, valued on ctx.view."""
    key: SubnetKey
    hotkey: Hotkey                  # the physical position's hotkey
    shares: Decimal                 # this sleeve's share-pool shares
    alpha: AlphaRao                 # value_of(shares) on the view (0 if the generation or the hotkey is gone)
    value_rao: Rao                  # executable value: liq_value(position alpha) * shares / position shares
    cost_rao: Rao                   # the sleeve's remaining cost basis (fees included)
    opened_block: Block             # the physical position's opened block


def sleeve_positions(ctx: TickContext, sid: StrategyId) -> dict[SubnetKey, SleevePos]:
    """Executable holdings of sleeve `sid` (keys with shares > 0), sorted by key."""
    out: dict[SubnetKey, SleevePos] = {}
    for h in sorted(ctx.portfolio.sleeves, key=lambda x: x.key):
        if h.strategy != sid or h.shares <= 0:
            continue
        pos = ctx.portfolio.position(h.key)
        if pos is None or pos.shares <= 0:
            continue
        s = ctx.view.get(h.key)
        idx = s.hotkey(pos.hotkey) if s is not None else None
        alpha = AlphaRao(0)
        value = Rao(0)
        if s is not None and idx is not None:
            alpha = idx.value_of(h.shares)
            total = liq_value(s.pool, idx.value_of(pos.shares))
            value = Rao(floor_int(DEC.divide(DEC.multiply(Decimal(total), h.shares), pos.shares)))
        out[h.key] = SleevePos(key=h.key, hotkey=pos.hotkey, shares=h.shares, alpha=alpha, value_rao=value,
                               cost_rao=h.cost_tao, opened_block=pos.opened_block)
    return out


def sleeve_budget_rao(ctx: TickContext, g_max_ppm: int) -> int:
    """Nominal sleeve budget G_MAX * NAV_liq * budget_ppm (section 3.10 step 1 before the stage, burn-in and drawdown
    multipliers, which the allocator applies to weight_ppm). Signals size as weight_ppm = V / this budget."""
    return int(ctx.nav_liq) * int(ctx.sleeve.budget_ppm) // PPM * g_max_ppm // PPM


def candidate(feat: Feat, hotkey: Hotkey | None) -> RouterCandidate | None:
    if hotkey is None:
        return None
    for c in feat.router_candidates:
        if c.hotkey == hotkey:
            return c
    return None


def yield_hotkey(ctx: TickContext, feat: Feat) -> Hotkey | None:
    """The book's router hotkey for the generation (book_view.router), else Feat.best_candidate (sections 2.1, 3.8)."""
    h = ctx.book_view.router.hotkey(feat.key)
    return h if h is not None else feat.best_candidate


# ------------------------------------------------------------------------------------------------- history
class History:
    """Cached, generation-safe read access to ctx.store for one on_tick call.

    Lookups sample the store at the 60-block grid (at_or_before each grid block), so a per-block paper store and a
    stride-60 replay give the same samples. `now` is always ctx.raw. A missing snapshot (KeyError) is None; a
    LookaheadError is a programming error and propagates."""

    def __init__(self, ctx: TickContext, grid: int = GRID_BLOCKS) -> None:
        self._store = ctx.store
        self.now: ChainSnapshot = ctx.raw
        self.grid_blocks = grid
        self._cache: dict[int, ChainSnapshot | None] = {}

    @property
    def block(self) -> int:
        return int(self.now.block)

    def at_or_before(self, block: int) -> ChainSnapshot | None:
        if block >= self.block:
            return self.now
        if block < 0:
            return None
        if block not in self._cache:
            try:
                self._cache[block] = self._store.at_or_before(Block(block))
            except KeyError:
                self._cache[block] = None
        return self._cache[block]

    def subnet_ago(self, key: SubnetKey, blocks: int, max_stale: int = GRID_BLOCKS) -> tuple[int, SubnetState] | None:
        """(block, state) of the same generation `blocks` ago, read at most `max_stale` blocks before that point."""
        want = self.block - blocks
        snap = self.at_or_before(want)
        if snap is None or int(snap.block) < want - max_stale:
            return None
        s = snap.get(key)
        return None if s is None else (int(snap.block), s)

    def grid(self, span: int, step: int | None = None) -> tuple[ChainSnapshot, ...]:
        """Snapshots at or before each grid block (multiples of `step`, default the 60-block grid) in (now - span,
        now], deduplicated, oldest first; `now` last."""
        g = self.grid_blocks if step is None else max(int(step), 1)
        first = (self.block - span) // g * g + g
        out: list[ChainSnapshot] = []
        seen: set[int] = set()
        for b in range(first, self.block, g):
            snap = self.at_or_before(b)
            if snap is not None and int(snap.block) not in seen and int(snap.block) > self.block - span:
                seen.add(int(snap.block))
                out.append(snap)
        if self.block not in seen:
            out.append(self.now)
        return tuple(out)

    def flow_frac(self, key: SubnetKey, blocks: int) -> float | None:
        """(SubnetTaoFlow_now - SubnetTaoFlow_{now-blocks}) / SubnetTAO_now; None unless both totals exist in the same
        generation and the older one is at a block >= 8,466,531 (protocol.derive.flow_valid_from)."""
        s = self.now.get(key)
        old = self.subnet_ago(key, blocks)
        if s is None or old is None or s.tao_flow_cum is None or s.pool.tao <= 0:
            return None
        ob, os_ = old
        if os_.tao_flow_cum is None or ob < int(flow_valid_from()):
            return None
        return (s.tao_flow_cum - os_.tao_flow_cum) / int(s.pool.tao)

    def flow_ewma_per_day(self, key: SubnetKey, half_life_blocks: int, step: int) -> float | None:
        """EWMA (half-life in blocks, weights by sample age) of the per-day net user flow as a fraction of the pool:
        increments of SubnetTaoFlow between consecutive `step`-grid samples of the generation over 3 half-lives, each
        scaled by 7,200 / gap and divided by that sample's SubnetTAO. None without two valid samples."""
        span = 3 * half_life_blocks
        valid = int(flow_valid_from())
        pts: list[tuple[int, int, int]] = []
        for snap in (self.at_or_before(self.block - span), *self.grid(span, step)):
            if snap is None or int(snap.block) < valid:
                continue
            st = snap.get(key)
            if st is None or st.tao_flow_cum is None or st.pool.tao <= 0:
                continue
            if not pts or pts[-1][0] < int(snap.block):
                pts.append((int(snap.block), int(st.tao_flow_cum), int(st.pool.tao)))
        if len(pts) < 2:
            return None
        num = den = 0.0
        for (b0, f0, _t0), (b1, f1, t1) in pairwise(pts):
            w = math.pow(0.5, (self.block - b1) / half_life_blocks)
            num += w * (f1 - f0) * BLOCKS_PER_DAY / (b1 - b0) / t1
            den += w
        return num / den if den > 0 else None

    def epoch_index_returns(self, key: SubnetKey, hotkey: Hotkey, n_epochs: int) -> tuple[float, ...]:
        """Per-epoch d ln I of `hotkey` on `key`, oldest first, at most the last n_epochs. The index of an epoch is its
        LAST FULL-snapshot observation (HEAD snapshots carry a stale panel); a gap of n drains is spread evenly."""
        s = self.now.get(key)
        if s is None:
            return ()
        tempo = max(s.tempo, 1)
        span = (n_epochs + 2) * tempo + 2 * self.grid_blocks
        rec: dict[int, Decimal] = {}
        for snap in self.grid(span):
            if snap.plan is not ReadPlan.FULL:
                continue
            st = snap.get(key)
            idx = st.hotkey(hotkey) if st is not None else None
            if st is None or idx is None or idx.total_shares <= 0 or idx.total_alpha <= 0:
                continue
            rec[int(st.last_epoch_block)] = idx.index()
        epochs = sorted(rec)
        out: list[float] = []
        for e0, e1 in pairwise(epochs):
            n = max(1, round((e1 - e0) / tempo))
            r = float(DEC.ln(DEC.divide(rec[e1], rec[e0]))) / n
            out.extend([r] * n)
        return tuple(out[-n_epochs:])

    def owner_sold_alpha(self, key: SubnetKey, blocks: int, step: int | None = None) -> int | None:
        """Owner net alpha sold over the last `blocks`: max(0, -sum of protocol.derive.owner_position_delta) over
        consecutive grid snapshots of the generation (section 3.6); None when no pair has the inputs. A step longer
        than an epoch credits at most one take credit per pair (a conservative overstatement of sales)."""
        snaps = (self.at_or_before(self.block - blocks),) + self.grid(blocks, step)
        total = 0
        seen_any = False
        prev: tuple[int, SubnetState] | None = None
        for snap in snaps:
            if snap is None:
                continue
            st = snap.get(key)
            if st is None:
                continue
            if prev is not None and prev[0] < int(snap.block):
                d = owner_position_delta(prev[1], st, snap.glob, Block(prev[0]), snap.block)
                if d is not None:
                    total += d
                    seen_any = True
            prev = (int(snap.block), st)
        if not seen_any:
            return None
        return max(0, -total)


# ------------------------------------------------------------------------------------------------- universe floor
def floor_rows(ctx: TickContext, risk: RiskCfg, *, skip_prune: bool = False) -> dict[SubnetKey, UniverseRow]:
    """Overlay floor sections A-G for every generation of ctx.raw (features.universe.evaluate) from the frame's
    book-independent inputs. Section 3.4 emission bans are book state (book_view.cooldowns; see entry_blocks), so
    none are passed here. skip_prune=True drops section D (the prune_blind baseline)."""
    raw, frame = ctx.raw, ctx.frame
    keys = sorted(k for k in frame.feats if raw.get(k) is not None)
    feats = {k: frame.feats[k] for k in keys}
    target = frame.prune.target
    ts = raw.get(target) if target is not None else None
    inp = UniverseInputs(
        snap=raw,
        prune_rank={k: f.prune_rank for k, f in feats.items() if f.prune_rank is not None},
        target=target,
        bottom_ema=ts.moving_price if ts is not None else None,
        t_star={k: (None if f.t_star_stress_blocks is None else math.ceil(f.t_star_stress_blocks))
                for k, f in feats.items()},
        flags={k: f.launch_flags for k, f in feats.items()},
        validator_ok={k: any(c.eligible for c in f.router_candidates) for k, f in feats.items()},
        emission_ban_until={},
        cost_ratio=cost_ratio(raw.glob, raw.block),
    )
    out: dict[SubnetKey, UniverseRow] = {}
    for row in evaluate(inp, UniverseParams.from_risk(risk)):
        if row.key not in feats:
            continue
        if skip_prune and "D" in row.failed:
            row = dataclasses.replace(row, d=True, failed=tuple(x for x in row.failed if x != "D"))
        out[row.key] = row
    return out


def entry_blocks(ctx: TickContext, key: SubnetKey) -> tuple[str, ...]:
    """Book-level reasons that forbid NEW exposure in `key` now: active cooldowns (section 3.2 H, section 3.4 bans,
    owner cooldowns), a book-wide entry halt, a dissolving generation."""
    b = int(ctx.block)
    out = sorted({f"cooldown.{rule}" for k, rule, until in ctx.book_view.cooldowns if k == key and int(until) > b})
    halt = ctx.book_view.entries_halted_until
    if halt is not None and int(halt) > b:
        out.append("entries_halted")
    if key in ctx.book_view.dissolving:
        out.append("dissolving")
    return tuple(out)


def recent_forced_exit(ctx: TickContext, key: SubnetKey, window_blocks: int) -> bool:
    """A risk (overlay) forced exit on this generation within the last window_blocks (book_view, section 2.1 C-U9)."""
    b = int(ctx.block)
    return any(k == key and int(blk) > b - window_blocks for blk, k, _rule, _u in ctx.book_view.recent_forced_exits)


def events_by_key(events: Iterable[ChainEvent]) -> dict[SubnetKey, tuple[ChainEvent, ...]]:
    out: dict[SubnetKey, list[ChainEvent]] = {}
    for e in events:
        if e.key is not None:
            out.setdefault(e.key, []).append(e)
    return {k: tuple(v) for k, v in sorted(out.items())}


def p_reg_horizon(ctx: TickContext, cal: Calibration | None, horizon_blocks: int) -> float:
    """P(a registration lands within horizon_blocks): PruneView.p_reg_ppm when the horizon is published, else the
    as-of hazard (protocol.prune.p_registration) when a calibration is injected, else the next longer published horizon
    (conservative), else a constant-hazard extrapolation of the longest one. 0 when no prune is possible; 1 when nothing
    is known (fail closed)."""
    pv = ctx.frame.prune
    if not pv.prune_possible:
        return 0.0
    table = dict(pv.p_reg_ppm)
    if horizon_blocks in table:
        return table[horizon_blocks] / PPM
    if cal is not None:
        return float(p_registration(ctx.raw.glob, cal.hazard, ctx.raw.block, horizon_blocks))
    longer = sorted(h for h in table if h >= horizon_blocks)
    if longer:
        return table[longer[0]] / PPM
    if table:
        hmax = max(table)
        p = min(max(table[hmax] / PPM, 0.0), 1.0)
        return 1.0 - math.pow(1.0 - p, horizon_blocks / hmax)
    return 1.0


# ------------------------------------------------------------------------------------------------- numerics
def rank_gauss(values: Sequence[float]) -> list[float]:
    """Rank-gauss transform: Phi^-1((rank - 0.5) / n) with average ranks for ties; [0.0] for a single value."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    order = sorted(range(n), key=lambda i: (values[i], i))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for m in range(i, j + 1):
            ranks[order[m]] = avg
        i = j + 1
    return [_ND.inv_cdf((r - 0.5) / n) for r in ranks]


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of an empty sequence")
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2


def robust_sd(xs: Sequence[float]) -> float:
    """1.4826 * median absolute deviation from the median."""
    m = median(xs)
    return MAD_TO_SD * median([abs(x - m) for x in xs])


def det_hash(*parts: object) -> int:
    """Deterministic 64-bit hash of the parts' text (blake2b), for seeded placebos and stable tie-breaks."""
    raw = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "little")


def terciles(values: Mapping[SubnetKey, float]) -> dict[SubnetKey, int]:
    """Tercile 0 (lowest) .. 2 (highest) of each key by value (ties broken by key): rank * 3 // n."""
    keys = sorted(values, key=lambda k: (values[k], k))
    n = len(keys)
    return {k: i * 3 // n for i, k in enumerate(keys)}


def hk_prefix(h: Hotkey) -> int:
    """First 48 bits of a hotkey (compact memory reference; matched back only among the tracked hotkeys of one subnet)."""
    return int(h[2:14], 16)


def hotkey_by_prefix(s: SubnetState, prefix: int) -> Hotkey | None:
    for idx in s.hotkeys:
        if hk_prefix(idx.hotkey) == prefix:
            return idx.hotkey
    return None


# ------------------------------------------------------------------------------------------------- signals
def target_signal(sid: StrategyId, key: SubnetKey, block: Block, *, value_rao: int, budget_rao: int, edge_day: float,
                  alpha_h: float, horizon_blocks: int, declares_dilution: bool, reasons: tuple[str, ...],
                  hotkey: Hotkey | None = None, urgency: Urgency = Urgency.NORMAL, size_cap: bool = True) -> Signal:
    """TARGET at value_rao: weight_ppm = value / nominal sleeve budget (capped at 1e6), max_size_rao = value when
    size_cap. Floats cross here, once per field, through to_ppm."""
    v = max(int(value_rao), 0)
    weight = min(PPM, v * PPM // budget_rao) if budget_rao > 0 else 0
    return Signal(strategy=sid, key=key, asof=block, kind=SignalKind.TARGET, weight_ppm=Ppm(weight),
                  edge_ppm_day=PpmPerDay(to_ppm(edge_day)), alpha_h_ppm=Ppm(to_ppm(alpha_h)),
                  max_size_rao=Rao(v) if size_cap else None, horizon_blocks=horizon_blocks, urgency=urgency,
                  hotkey_pref=hotkey, declares_dilution=declares_dilution, reasons=reasons)


def weight_signal(sid: StrategyId, key: SubnetKey, block: Block, weight_ppm: int, *, reasons: tuple[str, ...],
                  hotkey: Hotkey | None = None, horizon_blocks: int = 0, max_size_rao: int | None = None) -> Signal:
    """TARGET at a fixed share of the sleeve budget (baselines)."""
    return Signal(strategy=sid, key=key, asof=block, kind=SignalKind.TARGET, weight_ppm=Ppm(max(0, min(PPM, weight_ppm))),
                  max_size_rao=None if max_size_rao is None else Rao(max_size_rao), horizon_blocks=horizon_blocks,
                  hotkey_pref=hotkey, reasons=reasons)


def exit_signal(sid: StrategyId, key: SubnetKey, block: Block, urgency: Urgency, reasons: tuple[str, ...]) -> Signal:
    """Sleeve-level EXIT (this sleeve's share -> 0). Sleeve urgency is capped at HIGH (section 3.1)."""
    u = urgency if urgency <= Urgency.HIGH else Urgency.HIGH
    return Signal(strategy=sid, key=key, asof=block, kind=SignalKind.EXIT, urgency=u, reasons=reasons)


def avoid_signal(sid: StrategyId, key: SubnetKey, block: Block, reasons: tuple[str, ...]) -> Signal:
    return Signal(strategy=sid, key=key, asof=block, kind=SignalKind.AVOID, reasons=reasons)


def sort_signals(sigs: Iterable[Signal]) -> tuple[Signal, ...]:
    return tuple(sorted(sigs, key=lambda s: (s.key, s.kind.value)))


def relevant_wake(events: Sequence[ChainEvent], keys: Iterable[SubnetKey], kinds: frozenset[ChainEventKind]) -> bool:
    """True if an event of one of `kinds` concerns one of `keys` (or has no key)."""
    ks = set(keys)
    return any(e.kind in kinds and (e.key is None or e.key in ks) for e in events)


# ------------------------------------------------------------------------------------------------- factory
StrategyFactory = Callable[..., Strategy]


def build_strategy(sleeve: SleeveCfg, *, exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None,
                   calibration: CalibrationProvider | None = None) -> Strategy:
    """The implementation of sleeve.strategy, with its Params validated from sleeve.params.

    Ids: "carry", "momentum", "lcw" (each also as "<id>.<variant>") and "baseline.<name>" for the section 2.5
    baselines (cash, ew_price, ew_total, yield_x_size, prune_blind, random_entry; also "baseline.<name>.<variant>").
    exec_cfg / risk are the book's; calibration is the as-of CalibrationProvider (backtests: the LakeCalibrationProvider
    also given to the FeatureEngine and the overlay)."""
    from .baselines import BASELINES
    from .carry import CarryStrategy
    from .launch_lcw import LcwStrategy
    from .momentum import MomentumStrategy

    ex = exec_cfg if exec_cfg is not None else ExecCfg()
    rk = risk if risk is not None else RiskCfg()
    sid = sleeve.strategy
    root = str(sid).split(".")[0]
    if root == "carry":
        return CarryStrategy(sleeve.params, strategy_id=sid, exec_cfg=ex, risk=rk, calibration=calibration)
    if root == "momentum":
        return MomentumStrategy(sleeve.params, strategy_id=sid, exec_cfg=ex, risk=rk)
    if root == "lcw":
        return LcwStrategy(sleeve.params, strategy_id=sid, exec_cfg=ex, risk=rk, calibration=calibration)
    if root == "baseline":
        parts = str(sid).split(".")
        name = parts[1] if len(parts) > 1 else ""
        cls = BASELINES.get(name)
        if cls is not None:
            return cls(sleeve.params, strategy_id=sid, exec_cfg=ex, risk=rk)
    raise ParamsError(f"unknown strategy id {sid!r}")
