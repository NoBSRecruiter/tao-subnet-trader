"""taotrader/engine/reducer.py - EngineState, reduce(), ledger postings, invariant checks, the BookView projection (WP7).

`reduce(state, event)` is the ONLY state transition of a book (DESIGN.md sections 4.1, 4.3, 4.5, 5.7, 5.10). It is
pure (no clock, I/O, randomness or unsorted-set iteration), deterministic and TOTAL on valid journals: a fact about an
unknown or closed order, an illegal FSM transition or a money fact that cannot be posted is QUARANTINED (counted in
`orphans`, described in `quarantine`, never applied and never raised). Orphans halt entries until QuarantineCleared.
The one expected fact on a closed order, CarrierFeeSettled on an EXPIRED order, is accepted.

Folding (`fold_batch`) applies one committed journal batch and then cross-checks the typed portfolio against the
independent double-entry ledger (core.portfolio.check_invariants). A breach is recorded in `breaches` (entries halt,
the Runner alerts); it never raises, so a breach can never crash-loop the process.

Accounting conventions (all exact: integer rao and Decimal shares):
- Ledger postings come only from the core.portfolio helpers (fill_txn, yield_txn, dereg_txn, fail_txn,
  carrier_fee_txn, capital_txn); ReconAdjusted (live) posts against the extra account "adj:recon".
- Position valuation for invariant 2 uses a per-position `mark` index: value = floor(shares * mark). YieldAccrued sets
  mark = index_after, and the Engine computes delta_alpha = floor(shares * index_now) - ledger, so after ACCOUNT the
  ledger equals the valued position exactly (share-pool rounding shows up as a -1 rao yield, section 8.7). A fill
  re-marks the position at ledger / shares, i.e. the index implied by the fill itself.
- Sleeves are virtual (section 3.10): CapitalChanged splits cash over the book's sleeves by budget_ppm (equal split
  when no sleeve has a budget; pseudo-sleeve "book" when the book has no sleeves); a buy credits shares and debits
  sleeve cash by the intent's attribution; a sell takes shares from the attributed sleeves first, then pro rata from
  the other holders; a MOVE_STAKE rescales every holding on the key; SleeveTransfer moves shares from -> to and
  sleeve cash to -> from with NO ledger postings. Invariant 3 (sleeve shares sum to the position, sleeve cash sums
  to cash) holds by construction: every split assigns its exact remainder to one member.
- Contract for DecisionTrace.actions (the only channel from the pure pipeline back into book state):
  * rule "engine.forced_exit" (Engine-written, one per final ForcedExit): detail "rule=<rule>;urgency=<int>" ->
    recent_forced_exits (latest block per (key, rule), 21,600-block window);
  * rule "engine.order_spot" (Engine-written, one per emitted swap intent, buy or sell): detail "spot_rao=<int>;..."
    -> the order's decision spot (OrderEntry.decision_spot: the reference of the modelled cost in the sleeve cost
    ratio) and, for a buy, the decision spot of a new chase episode;
  * any action whose detail carries "cooldown_until=<block>" and a key -> cooldown (key, action.rule, until);
  * any action whose detail carries "halt_until=<block>" -> entries halted book-wide until that block.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from typing import Any, Final

from ..core import codec
from ..core.config import BookCfg, RiskCfg, SleeveCfg
from ..core.events import (
    CapitalChanged,
    CarrierFeeSettled,
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
from ..core.fixed import DEC, floor_int
from ..core.orders import TERMINAL, FailReason, Fill, IllegalTransition, OrderIntent, OrderKind, OrderRecord, OrderState, Urgency
from ..core.portfolio import (
    TAO_UNIT,
    LedgerTxn,
    Portfolio,
    Position,
    Posting,
    SleeveHolding,
    alpha_unit,
    apply_txn,
    capital_txn,
    carrier_fee_txn,
    check_invariants,
    dereg_txn,
    fail_txn,
    fill_txn,
    pos_account,
    yield_txn,
)
from ..core.protocols import BookView, RouterState, SleeveStats
from ..core.signals import Signal
from ..core.units import (
    BLOCKS_PER_DAY,
    PPM,
    Block,
    BookId,
    Mode,
    NetUid,
    PositionKey,
    Ppm,
    PpmPerDay,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from ..protocol.regimes import touches_econ

__all__ = [
    "BOOK_SLEEVE",
    "BURN_IN_PROBES",
    "ENGINE_FORCED_EXIT",
    "ENGINE_ORDER_SPOT",
    "RECON_ACCOUNT",
    "ROUTER_MEMORY_ID",
    "BookSpec",
    "ChaseEpisode",
    "EngineState",
    "OrderEntry",
    "SleeveSpec",
    "SleeveTrack",
    "book_view",
    "check_state",
    "event_book",
    "fold",
    "fold_batch",
    "holding_attribution",
    "initial_state",
    "ledger_balance",
    "ledger_dict",
    "money_digest",
    "parse_detail",
    "reduce",
    "sleeve_ids",
    "sleeve_stats",
    "state_hash",
    "value_at",
]

# ------------------------------------------------------------------------------------------------- constants
ROUTER_MEMORY_ID: Final[StrategyId] = StrategyId("risk.router")   # DecisionTrace.memories pseudo-id (section 5.10)
BOOK_SLEEVE: Final[StrategyId] = StrategyId("book")               # pseudo-sleeve: book-level context / sleeve-less books
ENGINE_FORCED_EXIT: Final[str] = "engine.forced_exit"
ENGINE_ORDER_SPOT: Final[str] = "engine.order_spot"
RECON_ACCOUNT: Final[str] = "adj:recon"                            # ReconAdjusted counter-account (live only)
BURN_IN_PROBES: Final[tuple[str, ...]] = ("emission_parity", "yield_parity", "hazard_validity")   # section 3.10 step 2

ORDERS_WINDOW_BLOCKS: Final[int] = BLOCKS_PER_DAY          # BookView.orders: terminal records of the last 7,200 blocks
FILLS_WINDOW_BLOCKS: Final[int] = 1_800                    # BookView.recent_fills / own_fill_blocks
FAIL_WINDOW_BLOCKS: Final[int] = 600                       # BookView.fail_counts_600
FORCED_EXIT_WINDOW_BLOCKS: Final[int] = 3 * BLOCKS_PER_DAY # BookView.recent_forced_exits (carry C-U9)
CHASE_STALE_BLOCKS: Final[int] = 1_800                     # an entry episode with no intent for 6 h is closed
NAV_DAYS_KEPT: Final[int] = 60                             # >= 45 daily NAV_liq samples (DD30, daily loss)
FAIL_BURST_COOLDOWN_BLOCKS: Final[int] = BLOCKS_PER_DAY    # >= 3 failures on a netuid in 600 blocks -> 7,200 blocks
FAIL_BURST_HALT_BLOCKS: Final[int] = 300                   # >= 5 book-wide in 600 blocks -> 300-block CAUTION (no entries)
WAVE_DISABLES: Final[int] = 3                              # >= 3 emission disables in one block -> book-wide entry halt
WAVE_HALT_BLOCKS: Final[int] = BLOCKS_PER_DAY
DRIFT_CAUTION_BLOCKS: Final[int] = BLOCKS_PER_DAY          # a model-drift alarm keeps CAUTION for one day
QUARANTINE_KEPT: Final[int] = 32
SHIELD_ERA_BLOCKS: Final[int] = 8                          # carrier eras (section 9.6), used only when era_end is unknown
UNSHIELDED_ERA_BLOCKS: Final[int] = 16
ERA_MARGIN_BLOCKS: Final[int] = 2
LOCK_MARGIN_BLOCKS: Final[int] = 2                         # a missed delegate is locked through era_end + 2
STATS_RETURNS_KEPT: Final[int] = 45
STATS_TRADES_KEPT: Final[int] = 20
STATS_TURNOVER_DAYS: Final[int] = 30
_INDEX_ONE: Final[int] = 10**9

_EXACT: Final[Context] = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)   # add/subtract/multiply only (exact)
_ZERO: Final[Decimal] = Decimal(0)
_PPM_D: Final[Decimal] = Decimal(PPM)
_CHAIN_FAILURES_EXCLUDED: Final[tuple[FailReason, ...]] = (FailReason.VENUE_REJECT, FailReason.NOT_PLACED)
_OPEN: Final[tuple[OrderState, ...]] = (OrderState.SUBMITTING, OrderState.SUBMITTED, OrderState.UNKNOWN)

Ledger = tuple[tuple[str, str, int], ...]


# ------------------------------------------------------------------------------------------------- state types
@dataclass(frozen=True, slots=True)
class SleeveSpec:
    """The reducer's view of one SleeveCfg: identity, budget and the section 3.11 kill-switch references.

    Optional SleeveCfg.params keys (integers): mean_45d_p5_ppm_day (preregistered 5th percentile of the backtest
    bootstrap), turnover_ref_ppm (30-day turnover / NAV reference) and the thresholds below."""
    strategy: StrategyId
    stage: Stage
    budget_ppm: Ppm
    mean_45d_p5_ppm_day: int | None = None
    turnover_ref_ppm: int | None = None
    reduce_dd_ppm: int = 80_000
    suspend_dd_ppm: int = 150_000
    reduce_cost_ppm: int = 1_500_000
    suspend_cost_ppm: int = 2_500_000
    reduce_turnover_ppm: int = 2_000_000
    promote_days: int = 30
    repromote_dd_ppm: int = 50_000          # SUSPENDED -> REDUCED: 30-day DD below 5% and 30-day mean > 0
    reactivate_dd_ppm: int = 40_000         # REDUCED -> ACTIVE: 30-day DD below 4%

    @staticmethod
    def from_cfg(s: SleeveCfg) -> SleeveSpec:
        def opt(name: str) -> int | None:
            v = s.params.get(name)
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"sleeve {s.strategy}: params.{name} must be an integer")
            return v

        d = SleeveSpec(s.strategy, s.stage, s.budget_ppm)

        def num(name: str, default: int) -> int:
            v = opt(name)
            return default if v is None else v

        return SleeveSpec(s.strategy, s.stage, s.budget_ppm, opt("mean_45d_p5_ppm_day"), opt("turnover_ref_ppm"),
                          reduce_dd_ppm=num("reduce_dd_ppm", d.reduce_dd_ppm),
                          suspend_dd_ppm=num("suspend_dd_ppm", d.suspend_dd_ppm),
                          reduce_cost_ppm=num("reduce_cost_ppm", d.reduce_cost_ppm),
                          suspend_cost_ppm=num("suspend_cost_ppm", d.suspend_cost_ppm),
                          reduce_turnover_ppm=num("reduce_turnover_ppm", d.reduce_turnover_ppm),
                          promote_days=num("promote_days", d.promote_days),
                          repromote_dd_ppm=num("repromote_dd_ppm", d.repromote_dd_ppm),
                          reactivate_dd_ppm=num("reactivate_dd_ppm", d.reactivate_dd_ppm))


@dataclass(frozen=True, slots=True)
class BookSpec:
    """Static per-book configuration the reducer needs. Part of EngineState, so a checkpoint carries it and recovery
    refuses a checkpoint written under a different spec."""
    book: BookId
    run_mode: RunMode
    sleeves: tuple[SleeveSpec, ...]          # sorted by strategy
    delegates: tuple[str, ...]               # configured fee-paying delegates (sim: "sim0".."sim<n-1>")
    risk: RiskCfg
    finality_lag_blocks: int
    latency_blocks: int

    @staticmethod
    def from_cfg(cfg: BookCfg, run_mode: RunMode, delegates: Sequence[str] | None = None) -> BookSpec:
        dels = tuple(delegates) if delegates is not None else tuple(f"sim{i}" for i in range(cfg.exec.n_delegates))
        sleeves = tuple(sorted((SleeveSpec.from_cfg(s) for s in cfg.sleeves), key=lambda s: s.strategy))
        if len({s.strategy for s in sleeves}) != len(sleeves):
            raise ValueError(f"book {cfg.book}: duplicate sleeve strategies")
        return BookSpec(cfg.book, run_mode, sleeves, dels, cfg.risk, cfg.exec.finality_lag_blocks, cfg.exec.latency_blocks)

    def sleeve(self, sid: StrategyId) -> SleeveSpec | None:
        for s in self.sleeves:
            if s.strategy == sid:
                return s
        return None


@dataclass(frozen=True, slots=True)
class OrderEntry:
    """One order (order_id, attempt) with the write-ahead bracket facts the BookView and the outbox need."""
    record: OrderRecord
    seq: int                                  # fold order of the OrderIntended (outbox priority)
    delegate: str | None = None
    nonce: int | None = None
    era_end: Block | None = None
    submit_block: Block | None = None
    expected_fill_block: Block | None = None
    terminal_block: Block | None = None
    fail_reason: FailReason | None = None
    exact_block: bool = True
    carrier_fee_settled: bool = False
    decision_spot: PriceRao = PriceRao(0)     # view spot at the decision ("engine.order_spot"); 0 = not recorded

    @property
    def intent(self) -> OrderIntent:
        return self.record.intent

    @property
    def state(self) -> OrderState:
        return self.record.state


@dataclass(frozen=True, slots=True)
class ChaseEpisode:
    """An open entry episode (section 3.12 chase rules)."""
    key: SubnetKey
    requotes: int
    spot: PriceRao                            # decision spot of the episode's first intent (Engine "engine.order_spot")
    last_block: Block


@dataclass(frozen=True, slots=True)
class SleeveTrack:
    """Un-netted stand-alone history of one sleeve (section 3.11 kill switches), sampled once per 7,200-block day."""
    strategy: StrategyId
    state: str = "ACTIVE"                     # "ACTIVE" | "REDUCED" | "SUSPENDED"
    state_since_day: int = 0
    cum_flow: int = 0                         # capital credited to the sleeve (CapitalChanged / ReconAdjusted cash)
    last_day: int | None = None
    last_nav: int = 0
    last_flow: int = 0                        # cum_flow at the last sample
    index_e9: int = _INDEX_ONE                # time-weighted return index (1e9 = 1.0)
    peak_e9: int = _INDEX_ONE
    index_hist: tuple[int, ...] = ()          # last 45 daily index values
    returns: tuple[int, ...] = ()             # last 45 daily returns, ppm
    navs: tuple[int, ...] = ()                # last 30 daily NAVs (turnover denominator)
    trades: tuple[tuple[int, int], ...] = ()  # last 20 (realised cost ppm, modelled cost ppm)
    turnover: tuple[tuple[int, int], ...] = ()   # (day, traded TAO) of the last 30 days


@dataclass(frozen=True, slots=True)
class EngineState:
    """Everything a book knows. A pure fold of the journal; codec-encodable (checkpoints, state hashes)."""
    spec: BookSpec
    clock: Block = Block(-1)                  # block of the last SnapshotObserved
    last_hash: str = ""
    health: HealthObs | None = None
    config_hash: str = ""
    funded: bool = False                      # a CapitalChanged has been folded
    portfolio: Portfolio = Portfolio(cash=Rao(0), fee_float=Rao(0))
    ledger: Ledger = ()                       # sorted (account, unit, balance), zero balances dropped
    marks: tuple[tuple[PositionKey, Decimal], ...] = ()
    dissolving: tuple[SubnetKey, ...] = ()
    orders: tuple[OrderEntry, ...] = ()       # open + terminal within ORDERS_WINDOW_BLOCKS, by seq
    order_seq: int = 0
    fills: tuple[Fill, ...] = ()              # fills with block > clock - 1,800
    fail_events: tuple[tuple[Block, NetUid], ...] = ()   # exact-block chain failures within 600 blocks
    chase: tuple[ChaseEpisode, ...] = ()
    decision_spots: tuple[tuple[SubnetKey, PriceRao], ...] = ()   # spots of the latest DecisionTrace's buys
    locks: tuple[tuple[str, Block], ...] = ()                     # delegate -> locked through (after a shield miss)
    cooldowns: tuple[tuple[SubnetKey, str, Block], ...] = ()
    entries_halted_until: Block | None = None
    disables: tuple[Block, int] = (Block(-1), 0)                  # (block, emission disables seen in it): wave halt
    forced_exits: tuple[tuple[Block, SubnetKey, str, Urgency], ...] = ()
    nav_daily: tuple[tuple[Block, Rao], ...] = ()
    sleeve_tracks: tuple[SleeveTrack, ...] = ()
    router: RouterState = RouterState()
    memories: tuple[tuple[StrategyId, bytes], ...] = ()
    last_calls: tuple[tuple[StrategyId, Block], ...] = ()
    standing: tuple[Signal, ...] = ()
    mode: Mode = Mode.NORMAL
    halted: bool = False                      # operator "halt" (or the kill file)
    exits_only: bool = False                  # operator "exits_only"
    flatten: tuple[NetUid, ...] = ()          # operator "flatten:<netuid>" on held netuids
    resume_count: int = 0
    drift_until: Block | None = None          # ModelDriftObserved alarm: CAUTION floor
    last_spec_change: Block | None = None
    burn_in_until: Block | None = None        # post-spec burn-in end (section 3.10 step 2)
    orphans: int = 0
    quarantine: tuple[str, ...] = ()
    recon_halt: bool = False                  # ReconAdjusted: entries halt until QuarantineCleared
    anomalies: int = 0                        # tolerated irregularities (unknown operator command, clamped transfer, ...)
    breaches: tuple[str, ...] = ()            # invariant violations after the last folded batch


def initial_state(spec: BookSpec) -> EngineState:
    ids = sleeve_ids(spec)
    return EngineState(spec=spec, portfolio=Portfolio(cash=Rao(0), fee_float=Rao(0),
                                                      sleeve_cash=tuple((sid, Rao(0)) for sid in ids)),
                       sleeve_tracks=tuple(SleeveTrack(sid) for sid in ids))


def sleeve_ids(spec: BookSpec) -> tuple[StrategyId, ...]:
    return tuple(s.strategy for s in spec.sleeves) or (BOOK_SLEEVE,)


# ------------------------------------------------------------------------------------------------- small helpers
def value_at(shares: Decimal, index: Decimal) -> int:
    """Position value (alpha rao) of `shares` at hotkey index `index` (floored). The Engine's YieldAccrued and the
    reducer's invariant valuation both use this one formula."""
    return floor_int(DEC.multiply(shares, index))


def event_book(ev: JournalEvent) -> BookId | None:
    """The book an event names; None for run-level events (snapshots, chain events, operator, config, drift)."""
    if isinstance(ev, OrderIntended):
        return ev.intent.book
    if isinstance(ev, FillReported):
        return ev.fill.book
    b = getattr(ev, "book", None)
    return BookId(b) if isinstance(b, str) else None


def parse_detail(detail: str) -> dict[str, str]:
    """RiskAction.detail canonical "k=v;k=v" text -> dict (malformed parts are ignored)."""
    out: dict[str, str] = {}
    for part in detail.split(";"):
        k, sep, v = part.partition("=")
        if sep and k:
            out[k.strip()] = v.strip()
    return out


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    t = s.strip()
    body = t[1:] if t.startswith("-") else t
    return int(t) if body.isdigit() else None


def ledger_dict(state: EngineState) -> dict[tuple[str, str], int]:
    return {(a, u): v for a, u, v in state.ledger}


def ledger_balance(state: EngineState, account: str, unit: str) -> int:
    for a, u, v in state.ledger:
        if a == account and u == unit:
            return v
    return 0


def _post(ledger: Ledger, txn: LedgerTxn) -> Ledger:
    d = {(a, u): v for a, u, v in ledger}
    apply_txn(d, txn)
    return tuple(sorted((a, u, v) for (a, u), v in d.items() if v != 0))


def _upsert(items: tuple[tuple[Any, ...], ...], key: Callable[[tuple[Any, ...]], Any], new: tuple[Any, ...],
            merge: Callable[[tuple[Any, ...], tuple[Any, ...]], tuple[Any, ...]] | None = None) -> tuple[Any, ...]:
    k = key(new)
    out: list[tuple[Any, ...]] = []
    found = False
    for it in items:
        if key(it) == k:
            out.append(merge(it, new) if merge is not None else new)
            found = True
        else:
            out.append(it)
    if not found:
        out.append(new)
    return tuple(out)


def _orphan(state: EngineState, desc: str) -> EngineState:
    return replace(state, orphans=state.orphans + 1, quarantine=(state.quarantine + (desc,))[-QUARANTINE_KEPT:])


def _anomaly(state: EngineState) -> EngineState:
    return replace(state, anomalies=state.anomalies + 1)


def _find_order(state: EngineState, order_id: str, attempt: int) -> int | None:
    for i, o in enumerate(state.orders):
        if o.intent.order_id == order_id and o.intent.attempt == attempt:
            return i
    return None


def _set_order(state: EngineState, i: int, entry: OrderEntry) -> EngineState:
    orders = list(state.orders)
    orders[i] = entry
    return replace(state, orders=tuple(orders))


def _split_int(amount: int, weights: Sequence[tuple[StrategyId, int]]) -> list[tuple[StrategyId, int]]:
    """Split an integer amount by non-negative integer weights (floor), the exact remainder to the largest weight
    (first in order on ties). Weights must not all be zero."""
    total = sum(w for _, w in weights)
    if total <= 0:
        raise ValueError("split needs a positive weight")
    parts = [(k, amount * w // total) for k, w in weights]
    big = max(range(len(weights)), key=lambda i: (weights[i][1], -i))
    rem = amount - sum(p for _, p in parts)
    parts[big] = (parts[big][0], parts[big][1] + rem)
    return parts


def _split_dec(total: Decimal, weights: Sequence[tuple[StrategyId, Decimal]]) -> list[tuple[StrategyId, Decimal]]:
    """Split a Decimal by non-negative Decimal weights (DEC, floor); the exact remainder goes to the largest weight."""
    wsum = _ZERO
    for _, w in weights:
        wsum = _EXACT.add(wsum, w)
    if wsum <= 0:
        raise ValueError("split needs a positive weight")
    big = max(range(len(weights)), key=lambda i: (weights[i][1], -i))
    parts: list[tuple[StrategyId, Decimal]] = []
    acc = _ZERO
    for i, (k, w) in enumerate(weights):
        if i == big:
            parts.append((k, _ZERO))
            continue
        p = DEC.divide(DEC.multiply(total, w), wsum)
        parts.append((k, p))
        acc = _EXACT.add(acc, p)
    parts[big] = (parts[big][0], _EXACT.subtract(total, acc))
    return parts


def _split_dec_int(amount: int, weights: Sequence[tuple[StrategyId, Decimal]]) -> list[tuple[StrategyId, int]]:
    """Split an integer amount by Decimal weights (floored parts, the exact remainder to the largest weight)."""
    total = _ZERO
    for _, w in weights:
        total = _EXACT.add(total, w)
    if total <= 0:
        raise ValueError("split needs a positive weight")
    parts = [(k, floor_int(DEC.divide(DEC.multiply(Decimal(amount), w), total))) for k, w in weights]
    big = max(range(len(weights)), key=lambda i: (weights[i][1], -i))
    parts[big] = (parts[big][0], parts[big][1] + amount - sum(x for _, x in parts))
    return parts


def holding_attribution(p: Portfolio, key: SubnetKey) -> tuple[tuple[StrategyId, Ppm], ...]:
    """Attribution (ppm, sums to 1e6) of the physical position on `key` by sleeve holdings; the pseudo-sleeve when
    no sleeve holds it."""
    holders = [(h.strategy, h.shares) for h in p.sleeves if h.key == key and h.shares > 0]
    if not holders:
        return ((BOOK_SLEEVE, Ppm(PPM)),)
    return tuple((sid, Ppm(x)) for sid, x in _split_dec_int(PPM, holders) if x > 0)


def _budget_weights(state: EngineState) -> list[tuple[StrategyId, int]]:
    specs = state.spec.sleeves
    if not specs:
        return [(BOOK_SLEEVE, 1)]
    if sum(s.budget_ppm for s in specs) > 0:
        return [(s.strategy, int(s.budget_ppm)) for s in specs]
    return [(s.strategy, 1) for s in specs]


def _attribution_weights(intent: OrderIntent | None) -> list[tuple[StrategyId, int]]:
    if intent is None:
        return []
    merged: dict[StrategyId, int] = {}
    for sid, ppm in intent.attribution:
        if ppm > 0:
            merged[sid] = merged.get(sid, 0) + int(ppm)
    return sorted(merged.items())


# ------------------------------------------------------------------------------------------------- portfolio edits
def _with_sleeve_cash(p: Portfolio, deltas: Iterable[tuple[StrategyId, int]]) -> Portfolio:
    d = {sid: int(c) for sid, c in p.sleeve_cash}
    for sid, x in deltas:
        d[sid] = d.get(sid, 0) + x
    return replace(p, sleeve_cash=tuple((sid, Rao(c)) for sid, c in sorted(d.items())))


def _holdings_on(p: Portfolio, key: SubnetKey) -> list[SleeveHolding]:
    return [h for h in p.sleeves if h.key == key]


def _set_holdings(p: Portfolio, key: SubnetKey, holdings: Iterable[SleeveHolding]) -> Portfolio:
    kept = [h for h in p.sleeves if h.key != key]
    kept += [h for h in holdings if h.shares > 0]
    return replace(p, sleeves=tuple(sorted(kept, key=lambda h: (h.strategy, h.key))))


def _position(p: Portfolio, pkey: PositionKey) -> Position | None:
    for pos in p.positions:
        if pos.key == pkey.subnet and pos.hotkey == pkey.hotkey:
            return pos
    return None


def _set_position(p: Portfolio, pkey: PositionKey, pos: Position | None) -> Portfolio:
    kept = [x for x in p.positions if not (x.key == pkey.subnet and x.hotkey == pkey.hotkey)]
    if pos is not None and pos.shares > 0:
        kept.append(pos)
    return replace(p, positions=tuple(sorted(kept, key=lambda x: (x.key, x.hotkey))))


def _set_mark(marks: tuple[tuple[PositionKey, Decimal], ...], pkey: PositionKey,
              mark: Decimal | None) -> tuple[tuple[PositionKey, Decimal], ...]:
    kept = [(k, m) for k, m in marks if k != pkey]
    if mark is not None:
        kept.append((pkey, mark))
    return tuple(sorted(kept, key=lambda km: km[0]))


def _remark(state: EngineState, pkey: PositionKey) -> EngineState:
    """Re-mark a position at ledger alpha / shares (the index implied by the facts just posted)."""
    pos = _position(state.portfolio, pkey)
    if pos is None:
        return replace(state, marks=_set_mark(state.marks, pkey, None))
    led = ledger_balance(state, pos_account(pkey.subnet, pkey.hotkey), alpha_unit(pkey.subnet))
    mark = DEC.divide(Decimal(max(led, 0)), pos.shares)
    return replace(state, marks=_set_mark(state.marks, pkey, mark))


def _close_flatten(state: EngineState) -> EngineState:
    held = {int(p.key.netuid) for p in state.portfolio.positions}
    keep = tuple(n for n in state.flatten if int(n) in held)
    dis = tuple(k for k in state.dissolving if any(p.key == k for p in state.portfolio.positions))
    if keep == state.flatten and dis == state.dissolving:
        return state
    return replace(state, flatten=keep, dissolving=dis)


def _track(state: EngineState, sid: StrategyId) -> SleeveTrack:
    for t in state.sleeve_tracks:
        if t.strategy == sid:
            return t
    return SleeveTrack(sid)


def _set_track(state: EngineState, t: SleeveTrack) -> EngineState:
    kept = [x for x in state.sleeve_tracks if x.strategy != t.strategy] + [t]
    return replace(state, sleeve_tracks=tuple(sorted(kept, key=lambda x: x.strategy)))


def _credit_flows(state: EngineState, shares: Iterable[tuple[StrategyId, int]]) -> EngineState:
    for sid, x in shares:
        t = _track(state, sid)
        state = _set_track(state, replace(t, cum_flow=t.cum_flow + x))
    return state


# ------------------------------------------------------------------------------------------------- fills
def _apply_fill(state: EngineState, f: Fill, intent: OrderIntent) -> EngineState:
    """Ledger + typed portfolio + sleeves for one fill leg. Raises ValueError on a fact that cannot be posted."""
    txn = fill_txn(f)                                  # raises ValueError for MOVE_STAKE_LIMIT (disabled in v1)
    pkey = PositionKey(f.key, f.hotkey)
    p = state.portfolio
    pos = _position(p, pkey)
    weights = _attribution_weights(intent)
    if f.kind is OrderKind.ADD_STAKE_LIMIT:
        if f.shares <= 0:
            raise ValueError(f"buy fill {f.fill_id} credits no shares")
        if pos is None:
            pos = Position(f.key, f.hotkey, f.shares, Rao(f.tao + f.tx_fee), f.block)
        else:
            pos = replace(pos, shares=_EXACT.add(pos.shares, f.shares), cost_tao=Rao(pos.cost_tao + f.tao + f.tx_fee))
        p = _set_position(replace(p, cash=Rao(p.cash - f.tao), fee_float=Rao(p.fee_float - f.tx_fee)), pkey, pos)
        if not weights:
            weights = [(BOOK_SLEEVE, 1)]
        share_parts = _split_dec(f.shares, [(sid, Decimal(w)) for sid, w in weights])
        tao_parts = dict(_split_int(f.tao, weights))
        cost_parts = dict(_split_int(f.tao + f.tx_fee, weights))
        holdings = {h.strategy: h for h in _holdings_on(p, f.key)}
        for sid, sh in share_parts:
            h = holdings.get(sid, SleeveHolding(sid, f.key, _ZERO, Rao(0)))
            holdings[sid] = replace(h, shares=_EXACT.add(h.shares, sh), cost_tao=Rao(h.cost_tao + cost_parts[sid]))
        p = _set_holdings(p, f.key, holdings.values())
        p = _with_sleeve_cash(p, ((sid, -x) for sid, x in tao_parts.items()))
    elif f.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        if pos is None:
            raise ValueError(f"sell fill {f.fill_id} on a position that is not held")
        take = min(f.shares, pos.shares)
        rest = _EXACT.subtract(pos.shares, take)
        cost_out = pos.cost_tao if rest <= 0 else floor_int(DEC.divide(DEC.multiply(Decimal(pos.cost_tao), take), pos.shares))
        pos = replace(pos, shares=rest, cost_tao=Rao(pos.cost_tao - cost_out))
        p = _set_position(replace(p, cash=Rao(p.cash + f.tao), fee_float=Rao(p.fee_float - f.tx_fee)), pkey, pos)
        takes = _sleeve_takes(_holdings_on(p, f.key), take, weights)
        new_holdings: list[SleeveHolding] = []
        tao_w: list[tuple[StrategyId, Decimal]] = []
        for h in _holdings_on(p, f.key):
            t = takes.get(h.strategy, _ZERO)
            if t > 0:
                tao_w.append((h.strategy, t))
                left = _EXACT.subtract(h.shares, t)
                c_out = h.cost_tao if left <= 0 else floor_int(DEC.divide(DEC.multiply(Decimal(h.cost_tao), t), h.shares))
                new_holdings.append(replace(h, shares=left, cost_tao=Rao(h.cost_tao - c_out)))
            else:
                new_holdings.append(h)
        p = _set_holdings(p, f.key, new_holdings)
        if tao_w:
            p = _with_sleeve_cash(p, _split_dec_int(int(f.tao), tao_w))
        else:
            p = _with_sleeve_cash(p, ((weights[0][0] if weights else BOOK_SLEEVE, f.tao),))
    elif f.kind is OrderKind.MOVE_STAKE:
        if pos is None or f.dest_hotkey is None:
            raise ValueError(f"move fill {f.fill_id} without an origin position or destination")
        take = min(f.shares, pos.shares)
        rest = _EXACT.subtract(pos.shares, take)
        moved_cost = pos.cost_tao if rest <= 0 else floor_int(DEC.divide(DEC.multiply(Decimal(pos.cost_tao), take), pos.shares))
        p = _set_position(replace(p, fee_float=Rao(p.fee_float - f.tx_fee)), pkey,
                          replace(pos, shares=rest, cost_tao=Rao(pos.cost_tao - moved_cost)))
        dkey = PositionKey(f.dest_key if f.dest_key is not None else f.key, f.dest_hotkey)
        dshares = f.dest_shares if f.dest_shares is not None else take
        dpos = _position(p, dkey)
        if dpos is None:
            dpos = Position(dkey.subnet, dkey.hotkey, dshares, Rao(moved_cost + f.tx_fee), f.block)
        else:
            dpos = replace(dpos, shares=_EXACT.add(dpos.shares, dshares), cost_tao=Rao(dpos.cost_tao + moved_cost + f.tx_fee))
        p = _set_position(p, dkey, dpos)
        p = _rescale_holdings(p, f.key, weights)
    else:
        raise ValueError(f"fill kind {f.kind.value} is not supported")
    state = replace(state, ledger=_post(state.ledger, txn), portfolio=p)
    state = _remark(state, pkey)
    if f.kind is OrderKind.MOVE_STAKE and f.dest_hotkey is not None:
        state = _remark(state, PositionKey(f.dest_key if f.dest_key is not None else f.key, f.dest_hotkey))
    return state


def _sleeve_takes(holdings: Sequence[SleeveHolding], shares: Decimal,
                  weights: Sequence[tuple[StrategyId, int]]) -> dict[StrategyId, Decimal]:
    """Shares each sleeve gives up in a sell: attributed sleeves first (up to their holding), the rest pro rata."""
    hold = {h.strategy: h.shares for h in holdings if h.shares > 0}
    total = _ZERO
    for v in hold.values():
        total = _EXACT.add(total, v)
    if shares >= total:
        return dict(hold)
    takes: dict[StrategyId, Decimal] = {}
    remaining = shares
    for sid, w in weights:
        avail = hold.get(sid, _ZERO)
        if avail <= 0 or remaining <= 0:
            continue
        want = DEC.divide(DEC.multiply(shares, Decimal(w)), _PPM_D)
        t = min(want, avail, remaining)
        if t > 0:
            takes[sid] = t
            remaining = _EXACT.subtract(remaining, t)
    if remaining > 0:
        avail_rest = [(sid, _EXACT.subtract(v, takes.get(sid, _ZERO))) for sid, v in sorted(hold.items())]
        avail_rest = [(sid, v) for sid, v in avail_rest if v > 0]
        rest_total = _ZERO
        for _, v in avail_rest:
            rest_total = _EXACT.add(rest_total, v)
        if remaining >= rest_total:
            parts = avail_rest
        else:
            parts = _split_dec(remaining, avail_rest)
        for sid, t in parts:
            takes[sid] = _EXACT.add(takes.get(sid, _ZERO), t)
    return takes


def _rescale_holdings(p: Portfolio, key: SubnetKey, weights: Sequence[tuple[StrategyId, int]]) -> Portfolio:
    """After a share-count change on `key` (MOVE_STAKE, ReconAdjusted), rescale the sleeve holdings so they sum to the
    new physical share count exactly."""
    phys = _ZERO
    for pos in p.positions:
        if pos.key == key:
            phys = _EXACT.add(phys, pos.shares)
    holdings = [h for h in _holdings_on(p, key) if h.shares > 0]
    if phys <= 0:
        return _set_holdings(p, key, [])
    if not holdings:
        sid = weights[0][0] if weights else BOOK_SLEEVE
        return _set_holdings(p, key, [SleeveHolding(sid, key, phys, Rao(0))])
    parts = dict(_split_dec(phys, [(h.strategy, h.shares) for h in holdings]))
    return _set_holdings(p, key, [replace(h, shares=parts[h.strategy]) for h in holdings])


# ------------------------------------------------------------------------------------------------- handlers
def _on_snapshot(state: EngineState, ev: SnapshotObserved) -> EngineState:
    b = int(ev.block)
    orders = tuple(o for o in state.orders if o.state not in TERMINAL
                   or (o.terminal_block is not None and o.terminal_block > b - ORDERS_WINDOW_BLOCKS))
    standing = tuple(s for s in state.standing if not (s.horizon_blocks > 0 and s.asof + s.horizon_blocks <= b))
    return replace(
        state, clock=ev.block, last_hash=str(ev.block_hash), health=ev.health, orders=orders,
        fills=tuple(f for f in state.fills if f.block > b - FILLS_WINDOW_BLOCKS),
        fail_events=tuple(x for x in state.fail_events if x[0] > b - FAIL_WINDOW_BLOCKS),
        forced_exits=tuple(x for x in state.forced_exits if x[0] > b - FORCED_EXIT_WINDOW_BLOCKS),
        cooldowns=tuple(c for c in state.cooldowns if c[2] >= b),
        locks=tuple(lk for lk in state.locks if lk[1] >= b),
        chase=tuple(c for c in state.chase if c.last_block > b - CHASE_STALE_BLOCKS),
        standing=standing,
        entries_halted_until=state.entries_halted_until if state.entries_halted_until is not None
        and state.entries_halted_until >= b else None,
        drift_until=state.drift_until if state.drift_until is not None and state.drift_until >= b else None,
        decision_spots=())


def _on_operator(state: EngineState, ev: OperatorCommand) -> EngineState:
    cmd = ev.command.strip()
    if cmd == "halt":
        return replace(state, halted=True)
    if cmd == "resume":
        return replace(state, halted=False, exits_only=False, flatten=(), resume_count=state.resume_count + 1)
    if cmd == "exits_only":
        return replace(state, exits_only=True)
    if cmd.startswith("flatten:"):
        n = _int_or_none(cmd.partition(":")[2])
        if n is None or n < 0:
            return _anomaly(state)
        if any(int(p.key.netuid) == n for p in state.portfolio.positions) and NetUid(n) not in state.flatten:
            return replace(state, flatten=tuple(sorted(state.flatten + (NetUid(n),))))
        return state
    return _anomaly(state)


def _on_capital(state: EngineState, ev: CapitalChanged) -> EngineState:
    try:
        txn = capital_txn(ev)
    except ValueError:
        return _orphan(state, f"capital {ev.memo}: unbalanced")
    p = state.portfolio
    p = replace(p, cash=Rao(p.cash + ev.cash_delta), fee_float=Rao(p.fee_float + ev.fee_float_delta))
    shares = _split_int(ev.cash_delta, _budget_weights(state))
    p = _with_sleeve_cash(p, shares)
    state = replace(state, ledger=_post(state.ledger, txn), portfolio=p, funded=True)
    return _credit_flows(state, shares)


def _on_config(state: EngineState, ev: ConfigApplied) -> EngineState:
    return replace(state, config_hash=ev.config_hash)


def _on_drift(state: EngineState, ev: ModelDriftObserved) -> EngineState:
    b = int(ev.block)
    if ev.probe in BURN_IN_PROBES:
        last = state.last_spec_change
        if last is not None and b - last <= state.spec.risk.spec_burn_in_blocks:
            until = Block(last + state.spec.risk.spec_burn_in_blocks)
            cur = state.burn_in_until
            return replace(state, burn_in_until=until if cur is None else Block(max(cur, until)))
        return state
    until = Block(b + DRIFT_CAUTION_BLOCKS)
    cur = state.drift_until
    return replace(state, drift_until=until if cur is None else Block(max(cur, until)))


def _add_cooldown(state: EngineState, key: SubnetKey, rule: str, until: int) -> EngineState:
    new = (key, rule, Block(until))
    merged = _upsert(state.cooldowns, lambda c: (c[0], c[1]), new, lambda old, nw: (old[0], old[1], max(old[2], nw[2])))
    return replace(state, cooldowns=tuple(sorted(merged, key=lambda c: (c[0], c[1]))))


def _halt_until(state: EngineState, until: int) -> EngineState:
    cur = state.entries_halted_until
    return replace(state, entries_halted_until=Block(until if cur is None else max(int(cur), until)))


def _on_chain_event(state: EngineState, ev: ChainEventObserved) -> EngineState:
    e = ev.event
    b = int(e.block)
    risk = state.spec.risk
    k = e.kind
    if k is ChainEventKind.DEREGISTERED and e.key is not None:
        if any(p.key == e.key for p in state.portfolio.positions) and e.key not in state.dissolving:
            return replace(state, dissolving=tuple(sorted(state.dissolving + (e.key,))))
        return state
    if k is ChainEventKind.EMISSION_TOGGLED and e.key is not None:
        if e.flag is False:
            state = _add_cooldown(state, e.key, "emission_off", b + risk.emission_ban_blocks)
            blk, n = state.disables
            n = n + 1 if blk == b else 1
            state = replace(state, disables=(Block(b), n))
            if n >= WAVE_DISABLES:
                state = _halt_until(state, b + WAVE_HALT_BLOCKS)
            return state
        if e.flag is True:
            return _add_cooldown(state, e.key, "emission_reenable", b + risk.reenable_wait_blocks)
        return state
    if k is ChainEventKind.OWNER_CHANGED and e.key is not None:
        return _add_cooldown(state, e.key, "owner_changed", b + risk.owner_cooldown_blocks)
    if k is ChainEventKind.AUTOLOCK_TOGGLED and e.key is not None and e.flag is False:
        return _add_cooldown(state, e.key, "owner_autolock_off", b + risk.owner_cooldown_blocks)
    if k is ChainEventKind.SPEC_CHANGED and e.name == "spec_version":
        state = replace(state, last_spec_change=Block(b))
        spec = _int_or_none(e.new)
        if spec is not None and touches_econ(spec):
            until = Block(b + risk.spec_burn_in_blocks)
            cur = state.burn_in_until
            state = replace(state, burn_in_until=until if cur is None else Block(max(cur, until)))
        return state
    return state


def _on_intended(state: EngineState, ev: OrderIntended) -> EngineState:
    i = ev.intent
    if any(o.intent.order_id == i.order_id for o in state.orders):
        return _orphan(state, f"duplicate OrderIntended {i.order_id}:{i.attempt}")
    spot = next((s for k, s in state.decision_spots if k == i.key), PriceRao(0))
    entry = OrderEntry(OrderRecord(i), seq=state.order_seq, decision_spot=spot)
    state = replace(state, orders=state.orders + (entry,), order_seq=state.order_seq + 1)
    episodes = [c for c in state.chase if c.key != i.key]
    cur = next((c for c in state.chase if c.key == i.key), None)
    if i.kind is OrderKind.ADD_STAKE_LIMIT:
        if cur is not None:
            episodes.append(replace(cur, requotes=cur.requotes + 1, last_block=i.created_block))
        else:
            episodes.append(ChaseEpisode(i.key, 0, spot, i.created_block))
    return replace(state, chase=tuple(sorted(episodes, key=lambda c: c.key)))


def _transition(state: EngineState, order_id: str, attempt: int, new: OrderState, what: str,
                **fields: Any) -> EngineState:
    i = _find_order(state, order_id, attempt)
    if i is None:
        return _orphan(state, f"{what} for unknown order {order_id}:{attempt}")
    entry = state.orders[i]
    try:
        rec = entry.record.to(new)
    except IllegalTransition:
        return _orphan(state, f"{what} on {entry.state.value} order {order_id}:{attempt}")
    return _set_order(state, i, replace(entry, record=rec, **fields))


def _on_cancelled(state: EngineState, ev: OrderCancelled) -> EngineState:
    return _transition(state, ev.order_id, ev.attempt, OrderState.CANCELLED, "OrderCancelled", terminal_block=ev.block)


def _on_submit_started(state: EngineState, ev: SubmitStarted) -> EngineState:
    return _transition(state, ev.order_id, ev.attempt, OrderState.SUBMITTING, "SubmitStarted", delegate=ev.delegate,
                       nonce=ev.nonce, era_end=ev.era_end)


def _on_ack(state: EngineState, ev: VenueAck) -> EngineState:
    return _transition(state, ev.order_id, ev.attempt, OrderState.SUBMITTED, "VenueAck", submit_block=ev.submit_block,
                       expected_fill_block=ev.expected_fill_block)


def _on_unknown(state: EngineState, ev: SubmitUnknown) -> EngineState:
    return _transition(state, ev.order_id, ev.attempt, OrderState.UNKNOWN, "SubmitUnknown")


def _on_fill(state: EngineState, ev: FillReported) -> EngineState:
    f = ev.fill
    i = _find_order(state, f.order_id, f.attempt)
    if i is None:
        return _orphan(state, f"fill {f.fill_id} for unknown order")
    entry = state.orders[i]
    if entry.state is OrderState.FILLED:
        if f.fill_id in entry.record.fill_ids:
            return _orphan(state, f"duplicate fill {f.fill_id}")
        rec = entry.record                                  # a further leg of a FILLED order
    else:
        try:
            rec = entry.record.to(OrderState.FILLED)
        except IllegalTransition:
            return _orphan(state, f"fill {f.fill_id} on {entry.state.value} order")
    if f.kind is not entry.intent.kind or f.key != entry.intent.key or f.hotkey != entry.intent.hotkey:
        return _orphan(state, f"fill {f.fill_id} does not match its intent")
    bad = _malformed_fill(f)
    if bad is not None:
        return _orphan(state, f"fill {f.fill_id} not posted: {bad}")
    try:
        state = _apply_fill(state, f, entry.intent)
    except (ValueError, ArithmeticError) as e:
        return _orphan(state, f"fill {f.fill_id} not posted: {e}")
    rec = replace(rec, fill_ids=rec.fill_ids + (f.fill_id,))
    state = _set_order(state, i, replace(entry, record=rec, terminal_block=f.block, exact_block=f.exact_block))
    state = replace(state, fills=state.fills + (f,))
    if f.kind is OrderKind.ADD_STAKE_LIMIT:
        state = replace(state, chase=tuple(c for c in state.chase if c.key != f.key))
    state = _record_trade(state, f, entry.intent, int(entry.decision_spot))
    return _close_flatten(state)


def _malformed_fill(f: Fill) -> str | None:
    """Why a fill fact cannot be posted (None = well-formed). Amounts are unsigned magnitudes (Fill docs: shares
    credited for a buy, debited for a sell or move); a signed share delta or a negative fee would otherwise post a
    balanced but wrong ledger that no invariant catches (a sell growing the position, a fee minting fee float)."""
    if f.shares <= 0:
        return f"shares {f.shares} <= 0"
    for name, v in (("tao", f.tao), ("alpha", f.alpha), ("swap_fee", f.swap_fee), ("author_fee_tao", f.author_fee_tao),
                    ("tx_fee", f.tx_fee)):
        if v < 0:
            return f"{name} {v} < 0"
    if f.kind is OrderKind.MOVE_STAKE:
        if f.dest_hotkey is None or f.dest_hotkey == f.hotkey:
            return "move without a destination hotkey"
        if f.dest_key is not None and f.dest_key != f.key:
            return "move_stake across generations"
        if f.dest_shares is not None and f.dest_shares <= 0:
            return f"dest_shares {f.dest_shares} <= 0"
    return None


def _on_failed(state: EngineState, ev: OrderFailed) -> EngineState:
    i = _find_order(state, ev.order_id, ev.attempt)
    if i is None:
        return _orphan(state, f"OrderFailed for unknown order {ev.order_id}:{ev.attempt}")
    if ev.tx_fee < 0:
        return _orphan(state, f"OrderFailed {ev.order_id}:{ev.attempt}: negative fee {ev.tx_fee}")
    entry = state.orders[i]
    new = OrderState.EXPIRED if ev.expired else OrderState.FAILED
    try:
        rec = entry.record.to(new)
        txn = fail_txn(ev)
    except (IllegalTransition, ValueError):
        return _orphan(state, f"OrderFailed({ev.reason.value}) on {entry.state.value} order {ev.order_id}:{ev.attempt}")
    p = state.portfolio
    state = replace(state, ledger=_post(state.ledger, txn), portfolio=replace(p, fee_float=Rao(p.fee_float - ev.tx_fee)))
    state = _set_order(state, i, replace(entry, record=rec, terminal_block=ev.block, fail_reason=ev.reason,
                                         exact_block=ev.exact_block))
    if ev.reason is FailReason.SHIELD_MISSED and entry.delegate is not None:
        if entry.era_end is not None:
            era_end = int(entry.era_end)
        else:
            era = SHIELD_ERA_BLOCKS if entry.intent.shielded else UNSHIELDED_ERA_BLOCKS
            anchor = int(entry.submit_block) if entry.submit_block is not None else int(ev.block)
            era_end = anchor + era + ERA_MARGIN_BLOCKS
        until = Block(era_end + LOCK_MARGIN_BLOCKS)
        merged = _upsert(state.locks, lambda lk: lk[0], (entry.delegate, until), lambda o, n: (o[0], max(o[1], n[1])))
        state = replace(state, locks=tuple(sorted(merged)))
    if ev.exact_block and ev.reason not in _CHAIN_FAILURES_EXCLUDED:
        b = int(ev.block)
        netuid = entry.intent.key.netuid
        state = replace(state, fail_events=state.fail_events + ((ev.block, netuid),))
        risk = state.spec.risk
        on_netuid = sum(1 for blk, n in state.fail_events if n == netuid and blk > b - FAIL_WINDOW_BLOCKS)
        if on_netuid >= risk.fail_burst_netuid:
            state = _add_cooldown(state, entry.intent.key, "fail_burst", b + FAIL_BURST_COOLDOWN_BLOCKS)
        book_wide = sum(1 for blk, _ in state.fail_events if blk > b - FAIL_WINDOW_BLOCKS)
        if book_wide >= risk.fail_burst_global:
            state = _halt_until(state, b + FAIL_BURST_HALT_BLOCKS)
    return state                                            # a failed buy keeps its chase episode open (re-quote)


def _on_carrier(state: EngineState, ev: CarrierFeeSettled) -> EngineState:
    i = _find_order(state, ev.order_id, ev.attempt)
    if i is None:
        return _orphan(state, f"CarrierFeeSettled for unknown order {ev.order_id}:{ev.attempt}")
    entry = state.orders[i]
    if entry.state is not OrderState.EXPIRED or entry.carrier_fee_settled:
        return _orphan(state, f"CarrierFeeSettled on {entry.state.value} order {ev.order_id}:{ev.attempt}")
    if ev.fee_rao < 0:
        return _orphan(state, f"CarrierFeeSettled {ev.order_id}:{ev.attempt}: negative fee {ev.fee_rao}")
    try:
        txn = carrier_fee_txn(ev)
    except ValueError:
        return _orphan(state, f"CarrierFeeSettled {ev.order_id}: unbalanced")
    p = state.portfolio
    state = replace(state, ledger=_post(state.ledger, txn), portfolio=replace(p, fee_float=Rao(p.fee_float - ev.fee_rao)))
    return _set_order(state, i, replace(entry, carrier_fee_settled=True))


def _on_dereg(state: EngineState, ev: DeregSettled) -> EngineState:
    """Settle a dissolved generation: EVERY position on ev.key (any hotkey) is written off to loss:dereg and the payout
    is credited once. DeregSettled is idempotent per generation (dereg:{book}:{netuid}:{reg_at}) and the observed live
    payout is the coldkey's whole free-TAO credit, so one record must close the whole generation; a second hotkey on
    it (an invariant-5 anomaly) must not survive as an unsettled zombie."""
    held = [x for x in state.portfolio.positions if x.key == ev.key]
    if not held:
        return _orphan(state, f"DeregSettled for {ev.key.netuid}:{ev.key.reg_at}:{ev.hotkey} not held")
    if ev.payout_tao < 0:
        return _orphan(state, f"DeregSettled {ev.key.netuid}:{ev.key.reg_at}: negative payout {ev.payout_tao}")
    au = alpha_unit(ev.key)
    primary = ev.hotkey if any(x.hotkey == ev.hotkey for x in held) else held[0].hotkey
    try:
        base = dereg_txn(replace(ev, hotkey=primary), pos_alpha=ledger_balance(state, pos_account(ev.key, primary), au))
        extra: list[Posting] = []
        for x in held:
            if x.hotkey != primary:
                led = ledger_balance(state, pos_account(ev.key, x.hotkey), au)
                extra += [Posting(pos_account(ev.key, x.hotkey), au, -led), Posting("loss:dereg", au, led)]
        txn = LedgerTxn(base.txn_id, base.block, base.postings + tuple(q for q in extra if q.amount != 0))
        ledger = _post(state.ledger, txn)
    except ValueError:
        return _orphan(state, f"DeregSettled {ev.key.netuid}:{ev.key.reg_at}: unbalanced")
    p = state.portfolio
    holders = [(h.strategy, h.shares) for h in _holdings_on(p, ev.key) if h.shares > 0]
    p = replace(p, cash=Rao(p.cash + ev.payout_tao))
    marks = state.marks
    for x in held:
        p = _set_position(p, x.pkey, None)
        marks = _set_mark(marks, x.pkey, None)
    p = _set_holdings(p, ev.key, [])
    if holders:
        p = _with_sleeve_cash(p, _split_dec_int(ev.payout_tao, holders))
    else:                                                   # no sleeve owned it: credit the payout by budget
        p = _with_sleeve_cash(p, _split_int(ev.payout_tao, _budget_weights(state)))
    state = replace(state, ledger=ledger, portfolio=p, marks=marks,
                    dissolving=tuple(k for k in state.dissolving if k != ev.key))
    return _close_flatten(state)


def _on_trace(state: EngineState, ev: DecisionTrace) -> EngineState:
    b = int(ev.block)
    memories = dict(state.memories)
    router = state.router
    for sid, raw in ev.memories:
        if sid == ROUTER_MEMORY_ID:
            try:
                router = codec.decode_bytes(RouterState, raw)
            except (codec.CodecError, ValueError):
                state = _anomaly(state)
        else:
            memories[sid] = raw
    calls = dict(state.last_calls)
    ran = set(ev.strategies_run)
    for sid in ev.strategies_run:
        calls[sid] = ev.block
    standing = [s for s in state.standing if s.strategy not in ran]
    standing += [s for s in ev.signals if s.strategy in ran]
    spots: list[tuple[SubnetKey, PriceRao]] = []
    for a in ev.actions:
        kv = parse_detail(a.detail)
        if a.rule == ENGINE_FORCED_EXIT and a.key is not None:
            urg = _int_or_none(kv.get("urgency"))
            urgency = Urgency(urg) if urg is not None and urg in {u.value for u in Urgency} else Urgency.URGENT
            entry = (ev.block, a.key, kv.get("rule", ""), urgency)
            merged = _upsert(state.forced_exits, lambda x: (x[1], x[2]), entry)
            state = replace(state, forced_exits=tuple(sorted(merged, key=lambda x: (x[0], x[1], x[2]))))
        elif a.rule == ENGINE_ORDER_SPOT and a.key is not None:
            spot = _int_or_none(kv.get("spot_rao"))
            if spot is not None:
                spots = [s for s in spots if s[0] != a.key] + [(a.key, PriceRao(spot))]
        until = _int_or_none(kv.get("cooldown_until"))
        if until is not None and a.key is not None:
            state = _add_cooldown(state, a.key, a.rule, until)
        halt = _int_or_none(kv.get("halt_until"))
        if halt is not None:
            state = _halt_until(state, halt)
    state = replace(state, memories=tuple(sorted(memories.items())), router=router,
                    last_calls=tuple(sorted(calls.items())),
                    standing=tuple(sorted(standing, key=lambda s: (s.strategy, s.key, s.kind.value))),
                    decision_spots=tuple(sorted(spots)))
    day = b // BLOCKS_PER_DAY
    if not state.nav_daily or int(state.nav_daily[-1][0]) // BLOCKS_PER_DAY < day:
        state = replace(state, nav_daily=(state.nav_daily + ((ev.block, ev.nav_liq),))[-NAV_DAYS_KEPT:])
        navs = dict(ev.sleeve_nav)
        for sid in sorted(set(navs) | {t.strategy for t in state.sleeve_tracks}):
            state = _set_track(state, _sample(_track(state, sid), day, int(navs.get(sid, 0)), state.spec.sleeve(sid)))
    return state


def _on_mode(state: EngineState, ev: ModeChanged) -> EngineState:
    return replace(state, mode=ev.mode)


def _on_xfer(state: EngineState, ev: SleeveTransfer) -> EngineState:
    if ev.from_strategy == ev.to_strategy or ev.shares <= 0:
        return _anomaly(state)
    p = state.portfolio
    holdings = {h.strategy: h for h in _holdings_on(p, ev.key)}
    src = holdings.get(ev.from_strategy)
    if src is None or src.shares <= 0:
        return _anomaly(state)
    shares, tao = ev.shares, int(ev.tao)
    if shares > src.shares:                                 # clamp to the holding; the TAO leg scales with it
        tao = floor_int(DEC.divide(DEC.multiply(Decimal(tao), src.shares), shares))
        shares = src.shares
        state = _anomaly(state)
    left = _EXACT.subtract(src.shares, shares)
    c_out = src.cost_tao if left <= 0 else floor_int(DEC.divide(DEC.multiply(Decimal(src.cost_tao), shares), src.shares))
    holdings[ev.from_strategy] = replace(src, shares=left, cost_tao=Rao(src.cost_tao - c_out))
    dst = holdings.get(ev.to_strategy, SleeveHolding(ev.to_strategy, ev.key, _ZERO, Rao(0)))
    holdings[ev.to_strategy] = replace(dst, shares=_EXACT.add(dst.shares, shares), cost_tao=Rao(dst.cost_tao + tao))
    p = _set_holdings(p, ev.key, holdings.values())
    p = _with_sleeve_cash(p, ((ev.to_strategy, -tao), (ev.from_strategy, tao)))
    return replace(state, portfolio=p)


def _on_recon(state: EngineState, ev: ReconAdjusted) -> EngineState:
    postings = [Posting("cash", TAO_UNIT, ev.cash_delta), Posting("fee_float", TAO_UNIT, ev.fee_float_delta),
                Posting(RECON_ACCOUNT, TAO_UNIT, -(ev.cash_delta + ev.fee_float_delta))]
    p = state.portfolio
    p = replace(p, cash=Rao(p.cash + ev.cash_delta), fee_float=Rao(p.fee_float + ev.fee_float_delta))
    touched: list[PositionKey] = []
    for pkey, delta in ev.share_deltas:
        mark = next((m for k, m in state.marks if k == pkey), Decimal(1))
        alpha = value_at(delta, mark) if delta >= 0 else -value_at(_EXACT.minus(delta), mark)
        au = alpha_unit(pkey.subnet)
        postings += [Posting(pos_account(pkey.subnet, pkey.hotkey), au, alpha), Posting(RECON_ACCOUNT, au, -alpha)]
        pos = _position(p, pkey)
        if pos is None:
            pos = Position(pkey.subnet, pkey.hotkey, _ZERO, Rao(0), ev.block)
        p = _set_position(p, pkey, replace(pos, shares=_EXACT.add(pos.shares, delta)))
        touched.append(pkey)
    for key in sorted({k.subnet for k in touched}):
        p = _rescale_holdings(p, key, ())
    cash_parts = _split_int(ev.cash_delta, _budget_weights(state))
    p = _with_sleeve_cash(p, cash_parts)
    txn = LedgerTxn(f"recon:{ev.book}:{ev.block}", ev.block, tuple(x for x in postings if x.amount != 0))
    try:
        ledger = _post(state.ledger, txn)
    except ValueError:
        return _orphan(state, f"ReconAdjusted at {ev.block}: unbalanced")
    state = replace(state, ledger=ledger, portfolio=p, recon_halt=True)
    state = _credit_flows(state, cash_parts)
    for pkey in touched:
        state = _remark(state, pkey)
    return _close_flatten(state)


def _on_qclear(state: EngineState, ev: QuarantineCleared) -> EngineState:
    return replace(state, orphans=0, quarantine=(), recon_halt=False)


def _on_yield(state: EngineState, ev: YieldAccrued) -> EngineState:
    pkey = PositionKey(ev.key, ev.hotkey)
    if _position(state.portfolio, pkey) is None:
        return _orphan(state, f"YieldAccrued for {ev.key.netuid}:{ev.key.reg_at}:{ev.hotkey} not held")
    try:
        txn = yield_txn(ev)
    except ValueError:
        return _orphan(state, f"YieldAccrued {ev.key.netuid}: unbalanced")
    return replace(state, ledger=_post(state.ledger, txn), marks=_set_mark(state.marks, pkey, ev.index_after))


_HANDLERS: Final[dict[type[JournalEvent], Callable[[EngineState, Any], EngineState]]] = {
    SnapshotObserved: _on_snapshot,
    OperatorCommand: _on_operator,
    CapitalChanged: _on_capital,
    ConfigApplied: _on_config,
    ModelDriftObserved: _on_drift,
    ChainEventObserved: _on_chain_event,
    YieldAccrued: _on_yield,
    DeregSettled: _on_dereg,
    DecisionTrace: _on_trace,
    ModeChanged: _on_mode,
    SleeveTransfer: _on_xfer,
    OrderIntended: _on_intended,
    OrderCancelled: _on_cancelled,
    SubmitStarted: _on_submit_started,
    VenueAck: _on_ack,
    SubmitUnknown: _on_unknown,
    FillReported: _on_fill,
    OrderFailed: _on_failed,
    CarrierFeeSettled: _on_carrier,
    ReconAdjusted: _on_recon,
    QuarantineCleared: _on_qclear,
}


# ------------------------------------------------------------------------------------------------- sleeve statistics
def _trade_costs(f: Fill, intent: OrderIntent, decision_spot: int = 0) -> tuple[int, int] | None:
    """(realised cost ppm, modelled cost ppm) of a swap fill; None when the intent carries no model output.

    Realised = fill.shortfall_ppm (vs the spot at the fill). Modelled = the cost implied by intent.expected_out at the
    spot it was MODELLED on, the decision spot. Evaluating expected_out at the fill's spot_before instead mixes the
    decision-to-fill price drift into the denominator (a stride fill lands ~60 blocks later): a 2% move turns the
    modelled cost negative and the 20-trade ratio explodes (or collapses), triggering false sleeve kills. The fill's
    spot is only a fallback when no decision spot was recorded."""
    spot = decision_spot if decision_spot > 0 else int(f.spot_before)
    if intent.expected_out <= 0 or spot <= 0:
        return None
    if f.kind is OrderKind.ADD_STAKE_LIMIT:
        if intent.tao_in <= 0:
            return None
        modelled = PPM - intent.expected_out * spot * PPM // (int(intent.tao_in) * 10**9)
    elif f.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        alpha_in = int(intent.alpha_in) if not intent.full_position and intent.alpha_in > 0 else int(f.alpha)
        if alpha_in <= 0:
            return None
        modelled = PPM - intent.expected_out * 10**9 * PPM // (alpha_in * spot)
    else:
        return None
    return (int(f.shortfall_ppm), modelled)


def _record_trade(state: EngineState, f: Fill, intent: OrderIntent, decision_spot: int = 0) -> EngineState:
    weights = _attribution_weights(intent)
    if not weights or f.kind is OrderKind.MOVE_STAKE:
        return state
    costs = _trade_costs(f, intent, decision_spot)
    day = int(f.block) // BLOCKS_PER_DAY
    for sid, tao in _split_int(int(f.tao), weights):
        t = _track(state, sid)
        trades = (t.trades + (costs,))[-STATS_TRADES_KEPT:] if costs is not None else t.trades
        turnover = [x for x in t.turnover if x[0] > day - STATS_TURNOVER_DAYS and x[0] != day]
        today = next((x[1] for x in t.turnover if x[0] == day), 0)
        turnover.append((day, today + abs(tao)))
        state = _set_track(state, replace(t, trades=trades, turnover=tuple(sorted(turnover))))
    return state


def _measures(t: SleeveTrack, spec: SleeveSpec | None) -> tuple[int, int, int, int | None, int | None]:
    """(dd_ppm, cost_ratio_20_ppm, turnover_ratio_ppm, mean_45d_ppm_day, p5) of a track. dd_ppm is the drawdown from
    the trailing 30-day peak of the time-weighted index (DD30, as the book-level governor), so a recovered sleeve can be
    re-promoted without its all-time peak re-triggering the kill rule."""
    window = t.index_hist[-STATS_TURNOVER_DAYS:]
    peak = max(window) if window else t.index_e9
    dd = (peak - t.index_e9) * PPM // peak if peak > 0 else 0
    if t.trades:
        realised = sum(r for r, _ in t.trades)
        modelled = max(sum(m for _, m in t.trades), 1)
        cost = max(realised, 0) * PPM // modelled
    else:
        cost = PPM
    turnover = 0
    if spec is not None and spec.turnover_ref_ppm and t.navs:
        avg_nav = sum(t.navs) // len(t.navs)
        if avg_nav > 0:
            traded = sum(x for d, x in t.turnover if t.last_day is None or d > t.last_day - STATS_TURNOVER_DAYS)
            turnover = (traded * PPM // avg_nav) * PPM // spec.turnover_ref_ppm
    mean45 = sum(t.returns) // len(t.returns) if len(t.returns) >= STATS_RETURNS_KEPT else None
    p5 = spec.mean_45d_p5_ppm_day if spec is not None else None
    return dd, cost, turnover, mean45, p5


def _sample(t: SleeveTrack, day: int, nav: int, spec: SleeveSpec | None) -> SleeveTrack:
    """One daily sample: return, index, drawdown and the section 3.11 kill-state transition."""
    if t.last_day is None:
        return replace(t, last_day=day, last_nav=nav, last_flow=t.cum_flow, state_since_day=day,
                       index_hist=(t.index_e9,), navs=(nav,))
    flow = t.cum_flow - t.last_flow
    r = (nav - flow - t.last_nav) * PPM // t.last_nav if t.last_nav > 0 else 0
    index = max(t.index_e9 * (PPM + r) // PPM, 0)
    t = replace(t, last_day=day, last_nav=nav, last_flow=t.cum_flow, index_e9=index, peak_e9=max(t.peak_e9, index),
                index_hist=(t.index_hist + (index,))[-STATS_RETURNS_KEPT:], returns=(t.returns + (r,))[-STATS_RETURNS_KEPT:],
                navs=(t.navs + (nav,))[-STATS_TURNOVER_DAYS:])
    s = spec if spec is not None else SleeveSpec(t.strategy, Stage.RESEARCH, Ppm(0))
    dd, cost, turnover, mean45, p5 = _measures(t, s)
    suspend = dd >= s.suspend_dd_ppm or cost > s.suspend_cost_ppm or (mean45 is not None and p5 is not None and mean45 < p5)
    reduce_ = dd >= s.reduce_dd_ppm or cost > s.reduce_cost_ppm or (s.turnover_ref_ppm is not None
                                                                     and turnover > s.reduce_turnover_ppm)
    days = day - t.state_since_day
    window = t.index_hist[-s.promote_days:]
    w_peak = max(window) if window else index
    w_dd = (w_peak - index) * PPM // w_peak if w_peak > 0 else 0
    recent = t.returns[-s.promote_days:]
    w_mean = sum(recent) // len(recent) if recent else 0
    new = t.state
    if t.state == "ACTIVE":
        new = "SUSPENDED" if suspend else "REDUCED" if reduce_ else "ACTIVE"
    elif t.state == "REDUCED":
        if suspend:
            new = "SUSPENDED"
        elif days >= s.promote_days and not reduce_ and w_dd < s.reactivate_dd_ppm:
            new = "ACTIVE"
    elif t.state == "SUSPENDED" and days >= s.promote_days and w_mean > 0 and w_dd < s.repromote_dd_ppm and not suspend:
        new = "REDUCED"
    if new != t.state:
        t = replace(t, state=new, state_since_day=day)
    return t


def sleeve_stats(state: EngineState) -> tuple[SleeveStats, ...]:
    out: list[SleeveStats] = []
    for t in state.sleeve_tracks:
        spec = state.spec.sleeve(t.strategy)
        dd, cost, turnover, mean45, p5 = _measures(t, spec)
        days = (t.last_day - t.state_since_day) if t.last_day is not None else 0
        out.append(SleeveStats(strategy=t.strategy, state=t.state, dd_ppm=Ppm(dd), cost_ratio_20_ppm=Ppm(cost),
                               turnover_ratio_ppm=Ppm(turnover),
                               mean_45d_ppm_day=PpmPerDay(mean45) if mean45 is not None else None,
                               mean_45d_p5_ppm_day=PpmPerDay(p5) if p5 is not None else None, days_in_state=days))
    return tuple(out)


# ------------------------------------------------------------------------------------------------- public API
def reduce(state: EngineState, ev: JournalEvent) -> EngineState:
    """The ONLY state transition: fold one journaled event. Events naming another book are ignored."""
    book = event_book(ev)
    if book is not None and book != state.spec.book:
        return state
    handler = _HANDLERS.get(type(ev))
    if handler is None:
        return state
    return handler(state, ev)


def check_state(state: EngineState) -> list[str]:
    """core.portfolio.check_invariants on the state, valuing every position at its mark."""
    marks = dict(state.marks)
    position_alpha: dict[PositionKey, int] = {}
    for p in state.portfolio.positions:
        m = marks.get(p.pkey)
        if m is not None:
            position_alpha[p.pkey] = value_at(p.shares, m)
    return check_invariants(state.portfolio, ledger_dict(state), position_alpha)


def fold_batch(state: EngineState, events: Iterable[JournalEvent]) -> EngineState:
    """Fold one committed batch, then cross-check portfolio and ledger (breaches are recorded, never raised)."""
    for ev in events:
        state = reduce(state, ev)
    breaches = tuple(check_state(state))
    return state if breaches == state.breaches else replace(state, breaches=breaches)


def fold(state: EngineState, batches: Iterable[Sequence[JournalEvent]]) -> EngineState:
    for batch in batches:
        state = fold_batch(state, batch)
    return state


def book_view(state: EngineState, block: Block | None = None) -> BookView:
    """The frozen, bounded projection of `state` the pipeline reads at `block` (default: the state's clock)."""
    b = int(state.clock if block is None else block)
    orders = tuple(o.record for o in state.orders
                   if o.state not in TERMINAL or (o.terminal_block is not None and o.terminal_block > b - ORDERS_WINDOW_BLOCKS))
    fills = tuple(f for f in state.fills if f.block > b - FILLS_WINDOW_BLOCKS)
    chase = tuple((c.key, c.requotes, c.spot) for c in state.chase if c.last_block > b - CHASE_STALE_BLOCKS)
    in_flight: dict[str, int] = {}
    for o in state.orders:
        if o.state in _OPEN and o.delegate is not None:
            lock = int(o.era_end) + LOCK_MARGIN_BLOCKS if o.era_end is not None else b
            in_flight[o.delegate] = max(in_flight.get(o.delegate, b), lock)
    locked: dict[str, int] = {d: int(u) for d, u in state.locks}
    for d, u in in_flight.items():
        locked[d] = max(locked.get(d, u), u)
    delegates = sorted(set(state.spec.delegates) | set(locked) | set(in_flight))
    free = tuple(d for d in delegates if d not in in_flight and locked.get(d, -1) < b)
    locked_until = tuple((d, Block(u)) for d, u in sorted(locked.items()) if u >= b)
    counts: dict[NetUid, int] = {}
    for blk, n in state.fail_events:
        if blk > b - FAIL_WINDOW_BLOCKS:
            counts[n] = counts.get(n, 0) + 1
    halted = state.entries_halted_until if state.entries_halted_until is not None and state.entries_halted_until >= b else None
    return BookView(
        orders=orders, recent_fills=fills, chase=chase, delegates_free=free, delegate_locked_until=locked_until,
        fail_counts_600=tuple(sorted(counts.items())), fail_count_600_book=sum(counts.values()),
        cooldowns=tuple(c for c in state.cooldowns if c[2] >= b), entries_halted_until=halted,
        recent_forced_exits=tuple(x for x in state.forced_exits if x[0] > b - FORCED_EXIT_WINDOW_BLOCKS),
        nav_liq_daily=state.nav_daily, sleeve_stats=sleeve_stats(state), router=state.router,
        dissolving=state.dissolving)


def money_digest(state: EngineState) -> str:
    """Digest of the money state: portfolio, ledger and every known order's FSM state and fills."""
    orders = tuple((o.intent.order_id, o.intent.attempt, o.state.value, o.record.fill_ids) for o in state.orders)
    return codec.digest((state.portfolio, state.ledger, tuple(sorted(orders))))


def state_hash(state: EngineState) -> str:
    """blake2b-256 of the canonical encoding of the whole state (checkpoints)."""
    return codec.digest(state, size=32)
