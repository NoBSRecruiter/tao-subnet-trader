"""taotrader/core/protocols.py - the seams. Everything a WP implements in parallel is typed here.

Rule: the engine/strategies/risk/portfolio packages are pure (no I/O, no clock, no randomness, no env);
chain/data/venues/live/ops are the imperative shell. import-linter enforces the direction.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .config import BookCfg, RiskCfg, SleeveCfg
from .events import ChainEvent, ChainEventKind, HealthObs, JournalEvent
from .orders import Fill, OrderIntent, OrderRecord, Resolution, Urgency, VenueCaps
from .portfolio import Portfolio
from .signals import RiskDecision, StrategyOutput, TargetBook
from .state import ChainSnapshot, ReadPlan
from .units import (
    AlphaRao, Block, BlockHash, BookId, Hotkey, LogicalTime, Mode, NetUid, Ppm, PpmPerDay, PriceRao, Rao, Stage,
    StrategyId, SubnetKey,
)
from .views import FeatureFrame


# ------------------------------------------------------------------ data
class SnapshotStore(Protocol):
    """Immutable decoded snapshots (lake + live hot buffer). Never returns data after `clock`."""
    clock: Block                                         # set by the Runner before each tick

    def at(self, block: Block) -> ChainSnapshot: ...     # exact block or KeyError
    def at_or_before(self, block: Block) -> ChainSnapshot: ...
    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]: ...   # LookaheadError if until > clock


@dataclass(frozen=True, slots=True)
class SourceItem:
    snapshot: ChainSnapshot
    health: HealthObs


class DataSource(Protocol):
    """ParquetReplay (backtest), LiveChainFeed (paper/live, finalized heads), JournalSource (recovery)."""
    cadence_blocks: int                                  # data-resolution contract (Strategy.min_cadence_blocks)
    store: SnapshotStore

    def stream(self, after: Block | None) -> AsyncIterator[SourceItem]: ...   # strictly after the last journaled block
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SwapSim:
    """SimSwapResult (48 bytes, spec >= 391). All-zero == failure."""
    tao_amount: int
    alpha_amount: int
    tao_fee: int
    alpha_fee: int
    tao_slippage: int
    alpha_slippage: int


class ChainReader(Protocol):
    """WP1. Windows-native JSON-RPC reader; every read is pinned to a block hash."""
    async def block_hash(self, block: Block) -> BlockHash: ...
    async def finalized_head(self) -> tuple[Block, BlockHash]: ...
    async def snapshot(self, block: Block, block_hash: BlockHash, plan: ReadPlan,
                       prev: ChainSnapshot | None, tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot: ...
    async def dividend_keys(self, netuid: NetUid, block_hash: BlockHash) -> tuple[Hotkey, ...]: ...
    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]: ...       # netuid -> rao/alpha
    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None: ...
    async def registration_cost(self, block_hash: BlockHash) -> Rao: ...
    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]: ...
    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]: ...    # (spec, tx_version)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    seq: int
    batch: int                     # seq of the first record of the atomic batch
    time: LogicalTime
    book: BookId                   # "" for run-level records (snapshot, chain events, operator, config)
    kind: str
    version: int                   # the event class's VERSION (upcasters key on it)
    payload: bytes                 # canonical JSON (core.codec)
    idem: str | None
    prev_hash: bytes
    hash: bytes                    # blake2b-256(prev_hash || block|phase|sub|book|kind|version|payload) (= section 7.2)


class Journal(Protocol):
    """WP3. Append-only, hash-chained, atomic batches (SQLite WAL, synchronous=FULL in paper/live)."""
    def append_batch(self, items: Sequence[tuple[LogicalTime, BookId, JournalEvent]]) -> list[JournalRecord]: ...
    def read(self, from_seq: int = 1) -> Iterator[JournalRecord]: ...
    def has_idem(self, key: str) -> bool: ...
    def head(self) -> tuple[int, bytes]: ...
    def verify_chain(self) -> int: ...       # returns records verified; raises on a broken chain


# ------------------------------------------------------------------ features
class FeatureEngine(Protocol):
    """WP5. Deterministic function of the snapshot sequence and of own_fill_blocks; rebuilt on recovery by
    re-ingesting the lake window. Book-INDEPENDENT only: per-book choices live in BookView / WP8.
    Constructed with a CalibrationProvider (protocol/calibration.py) for the hazard and kappa_p used in PruneView."""
    warm: bool

    def update(self, raw: ChainSnapshot, events: Sequence[ChainEvent],
               own_fill_blocks: frozenset[Block] = frozenset()) -> FeatureFrame: ...
        # own_fill_blocks: union over all books of blocks with own fills (Runner-supplied, journal-derived);
        # those blocks are excluded from the beta samples (section 3.12)
    def state_digest(self) -> str: ...


# ------------------------------------------------------------------ decisions
@dataclass(frozen=True, slots=True)
class SleeveStats:
    """Un-netted stand-alone statistics of one sleeve (section 3.11 kill rules). Maintained by engine.reducer
    from DecisionTrace.sleeve_nav, fills and SleeveTransfer; references come from SleeveCfg.params."""
    strategy: StrategyId
    state: str                                   # "ACTIVE" | "REDUCED" | "SUSPENDED" (sleeve kill state)
    dd_ppm: Ppm                                  # un-netted drawdown from peak
    cost_ratio_20_ppm: Ppm                       # realised / modelled cost over the last 20 trades (1e6 = parity)
    turnover_ratio_ppm: Ppm                      # trailing 30-d turnover / backtest reference
    mean_45d_ppm_day: PpmPerDay | None           # trailing 45-d mean daily un-netted return; None before 45 d
    mean_45d_p5_ppm_day: PpmPerDay | None        # 5th percentile of its backtest bootstrap (preregistered reference)
    days_in_state: int                           # re-promotion clock


@dataclass(frozen=True, slots=True)
class RouterState:
    """Per-book YieldRouter memory (WP8 risk/router.py, section 3.8). Journaled in DecisionTrace.memories under
    the pseudo-id "risk.router" and folded back by the reducer."""
    choice: tuple[tuple[SubnetKey, Hotkey], ...] = ()              # current hotkey per subnet, sorted by key
    fail_epochs: tuple[tuple[SubnetKey, int], ...] = ()            # consecutive epochs the current hotkey failed filters
    beat_epochs: tuple[tuple[SubnetKey, Hotkey, int], ...] = ()    # consecutive epochs this challenger beat the current

    def hotkey(self, key: SubnetKey) -> Hotkey | None:
        for k, h in self.choice:
            if k == key:
                return h
        return None


@dataclass(frozen=True, slots=True)
class BookView:
    """Book-specific execution and risk history: a frozen, bounded projection of engine.reducer's EngineState
    (WP7). Everything the strategies, router, caps, allocator, overlay and planner need beyond the market."""
    orders: tuple[OrderRecord, ...]                       # open records + terminal records of the last 7,200 blocks
    recent_fills: tuple[Fill, ...]                        # last 1,800 blocks (cost ratios, own-fill bookkeeping)
    chase: tuple[tuple[SubnetKey, int, PriceRao], ...]    # open entry episodes: (key, re-quotes so far, decision spot)
    delegates_free: tuple[str, ...]                       # no carrier in flight and no nonce lock
    delegate_locked_until: tuple[tuple[str, Block], ...]  # (delegate, era_end + 2) after a miss or while in flight
    fail_counts_600: tuple[tuple[NetUid, int], ...]       # terminal failures per netuid, last 600 blocks, exact_block only
    fail_count_600_book: int
    cooldowns: tuple[tuple[SubnetKey, str, Block], ...]   # (key, rule, until): section 3.2 H cooldowns, 3.4 entry bans
    entries_halted_until: Block | None                    # wave halt, fail-burst CAUTION, ...
    recent_forced_exits: tuple[tuple[Block, SubnetKey, str, Urgency], ...]   # last 21,600 blocks (carry C-U9)
    nav_liq_daily: tuple[tuple[Block, Rao], ...]          # one NAV_liq sample per 7,200-block day, >= 45 d (DD30, daily loss)
    sleeve_stats: tuple[SleeveStats, ...]
    router: RouterState
    dissolving: tuple[SubnetKey, ...] = ()                # held generations removed on chain, awaiting DeregSettled


@dataclass(frozen=True, slots=True)
class TickContext:
    block: Block
    raw: ChainSnapshot                 # the market as it is: features and signals come from here
    view: ChainSnapshot                # raw + this book's own footprint (sim/paper); == raw live. Sizing/quotes/marks use it
    prev: ChainSnapshot | None
    events: tuple[ChainEvent, ...]     # derived this tick
    frame: FeatureFrame                # book-independent
    portfolio: Portfolio
    nav_liq: Rao                       # cash + sum of one-shot sim_sell value of every position on `view`
    sleeve: SleeveCfg                  # the calling strategy's sleeve config and budget
    sleeve_value: Rao                  # current executable value of this sleeve's holdings
    mode: Mode
    store: SnapshotStore               # bounded history; cannot see beyond `block`
    book_view: BookView                # this book's orders, fills, delegates, cooldowns, NAV history, router memory


class Strategy(Protocol):
    """WP9. Pure: no clock, I/O, randomness or unsorted-set iteration. State lives in the returned Memory."""
    id: StrategyId
    decide_every_blocks: int
    wake_on: frozenset[ChainEventKind]
    min_cadence_blocks: int            # coarsest data cadence it can be honestly evaluated on
    valid_from_block: Block            # regime guard (e.g. 8,765,684 for gate-dependent logic)
    declares_dilution: bool            # True if its scores already net structural sell load

    def initial_memory(self) -> object: ...
    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput: ...


@dataclass(frozen=True, slots=True)
class RiskContext:
    tick: TickContext                  # sleeve field is the book-level pseudo-sleeve
    cfg: RiskCfg
    book: BookCfg
    stages: tuple[tuple[StrategyId, Stage], ...]
    halted_by_operator: bool
    orphans: int                       # unknown-order facts in quarantine (>0 halts entries)
    burn_in_until: Block | None        # post-spec burn-in end (reducer: SPEC_CHANGED with regimes.touches_econ, or a
                                       # post-spec ModelDriftObserved parity breach; section 3.10 step 2)


@runtime_checkable
class RiskOverlay(Protocol):
    """WP8. Final authority. Monotone: may lower targets, add forced exits, veto entries, raise the mode.
    Constructed with a CalibrationProvider (kappa_p, R, Tier B jumps as-of the decision block)."""
    def review(self, proposal: TargetBook, ctx: RiskContext) -> RiskDecision: ...


# ------------------------------------------------------------------ execution
class ExecutionVenue(Protocol):
    """WP6 (sim, paper) and WP11 (live). Venues hold NO hidden state: everything needed after a restart is
    rebuilt from journaled events via observe(). submit() must be idempotent on (order_id, attempt)."""
    caps: VenueCaps

    def mark_to(self, raw: ChainSnapshot) -> ChainSnapshot: ...      # sim/paper: + own-impact overlay; live: identity
    async def reserve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[str, int | None, Block | None]: ...
        # (delegate, carrier nonce, era_end) for SubmitStarted; sends nothing. Live: a free delegate, its pool-aware
        # system_accountNextIndex read immediately before SubmitStarted is journaled, finalized anchor + 8 (16) + 2.
        # Sim/paper: ("sim<i>", None, deterministic era_end)
    async def submit(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent: ...
        # returns VenueAck, OrderFailed(VENUE_REJECT ...) or SubmitUnknown (live: the SDK used a nonce other than the
        # reserved one); raising => runner journals SubmitUnknown
    async def advance(self, view: ChainSnapshot) -> JournalEvent | None: ...
        # next due FillReported / OrderFailed / CarrierFeeSettled (ONE per call; the runner commits it, re-marks,
        # and calls again)
    async def resolve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[Resolution, tuple[JournalEvent, ...]]: ...
    def observe(self, ev: JournalEvent) -> None: ...                 # called for EVERY journaled event, incl. recovery


# ------------------------------------------------------------------ pure pipeline functions (signatures)
# DECIDE order (section 4.4): strategies -> router -> caps -> allocator -> overlay.review -> planner.
# After the router, the Engine passes ctx' = replace(ctx, book_view=replace(ctx.book_view, router=new_state)).

RouterFn = Callable[[TickContext, RiskCfg], RouterState]
"""WP8 risk/router.py: per-book hotkey choice from frame.feats[k].router_candidates, the book's positions and
ctx.book_view.router (Q_MAX against own shares, 2-epoch hysteresis, switch triggers). Pure."""

CapsFn = Callable[[TickContext, RiskCfg], dict[SubnetKey, Rao]]
"""WP8 risk/liquidity.py: per-subnet V_cap of section 3.5 (T_st, s, m_esc, active haircuts, NU_MAX). Pure.
Runs before the allocator; its result is the allocator's `caps`."""


class Allocator(Protocol):
    """WP8 portfolio/allocator.py: sleeves -> per-subnet aggregate targets (sum-then-cap, netting transfers,
    hotkeys from ctx.book_view.router)."""
    def __call__(self, signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext,
                 caps: dict[SubnetKey, Rao]) -> TargetBook: ...


class Planner(Protocol):
    """WP8 portfolio/planner.py: TargetBook -> ordered OrderIntents (limits, dust, priority, one in flight per netuid).
    Attempts, chase state, delegates, own fills and failure counts come from ctx.book_view; `inflight` is the set of
    keys with a non-terminal order in ctx.book_view.orders."""
    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey],
                 run_id: str, book: BookId) -> tuple[OrderIntent, ...]: ...


class ShareValue(Protocol):
    def __call__(self, key: SubnetKey, hotkey: Hotkey, shares: Decimal) -> AlphaRao: ...
