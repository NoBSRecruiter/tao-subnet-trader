"""taotrader/venues/sim.py - SimVenue: the ONE shared execution simulator (WP6; DESIGN.md sections 8.3-8.5, 3.12, 4.5).

A SimVenue serves one book. Its whole state is a fold of the journal: `observe()` is called for EVERY journaled event
(including recovery) and is the only method that mutates venue state. `mark_to`, `reserve`, `submit`, `advance` and
`resolve` are pure functions of that state and their arguments, so a venue rebuilt from the journal produces exactly
the same future events as the one that wrote it (section 5.13 note 5).

Model (binding numbers come from ExecCfg; the chain rules from protocol.amm / protocol.fees):
- Footprint overlay (8.3). Per generation, the cumulative reserve displacement of this book's fills
  (d_tao, d_alpha, stamp). `mark_to(raw)` shifts each touched pool by trunc(decay * d) with
  decay = 0.5 ** ((block - stamp) / impact_half_life_blocks): half-life 0 = TEMPORARY (gone from the next block on),
  None = PERSISTENT (never decays). A new fill folds into the decayed displacement and restamps it. A shift that would
  leave any reserve below 1 rao is not applied (degenerate pools only).
- Latency (8.4). An order submitted on snapshot b is acked with submit_block = b + finality_lag and
  expected_fill_block = submit_block + latency (shielded) or submit_block + 1 (unshielded era-16 fallback).
  * stride mode (exact_fills=False, backtests): the order settles at the first view whose block >= expected_fill_block;
    exact_block = (view.block == expected_fill_block), so per-block refinement windows settle block-exact for free.
  * exact mode (exact_fills=True, PaperVenue): a shielded order settles ONLY at expected_fill_block. If the view is
    already past it, `_recorded_state()` supplies the state at that block (PaperVenue: recorder/store or reader); without
    it the order is MISSED (a fill at N+3..N+8 is never legal). An unshielded order may land at any block in
    (submit, era_end]; past era_end it expires (ERA_EXPIRED, never included, fee 0).
- Chain rules (8.5), evaluated at the settlement view, in this order: injected shield miss (SHIELD_MISSED, expired,
  carrier fee), SubnetNotExists (generation gone), SafeMode, injected inner failure (fail_inject_ppm), then per kind:
  * buy:  SubtokenDisabled, hotkey index missing (sim artefact, OTHER), NotEnoughBalanceToStake (OTHER),
          PriceLimitExceeded (strict: spot < limit), SlippageTooHigh (fill-or-kill above max_buy_to_limit) or clip to it
          (allow_partial, refund the rest), then the AMM guards (AmountTooLow, InsufficientLiquidity, ReservesTooLow).
  * sell: NotEnoughStakeToWithdraw, PriceLimitExceeded (strict: spot > limit; limit 0 = no floor), SlippageTooHigh or
          clip, AMM guards (partial sells need >= 0.002 TAO out; full exits exempt). A remainder below
          NominatorMinRequiredStake is force-sold at no limit in the same fill (one combined Fill, complete=True).
  * MOVE_STAKE (same subnet): NotEnoughStakeToWithdraw, AmountTooLow (TAO value < DefaultMinStake), no swap fee, one
          tx fee, shares re-issued at the destination index.
  Every inner-call failure pays the full tx fee (protocol.fees.tx_fee_rao); a shield miss pays the carrier fee;
  venue rejects, ERA_EXPIRED and NOT_PLACED pay nothing. The check order is a modelling choice that matters only
  when several failures coincide.
- Failure injection (8.5): MISSED iff blake2b(seed|order_id|attempt) mod 1e6 < shield_miss_ppm (shielded orders only);
  an inner failure iff blake2b(seed|order_id|attempt|inner) mod 1e6 < fail_inject_ppm. Deterministic across runs.
- Delegates (3.12, 9.6). `reserve()` returns ("sim<i>", None, era_end) for the lowest-index delegate with no carrier in
  flight and no nonce lock; era_end = anchor + 8 (16 unshielded) + 2 where anchor = now.block. After a SHIELD_MISSED
  the delegate stays locked through era_end + 2 (free from era_end + 3), so retries rotate as live. When every
  delegate is busy or locked, `reserve()` raises NoFreeDelegate (the planner should never get there).
- submit() is idempotent on (order_id, attempt) and depends only on the intent, the anchor block and the open-order
  set: TTL (planned inclusion anchor + cfg.finality_lag + latency must be <= valid_until), MOVE_STAKE_LIMIT disabled,
  one open order per netuid. resolve() recomputes the anchor from SubmitStarted.era_end and re-derives exactly the
  ack (or reject) submit() would have produced, so a crash after SubmitStarted reproduces the crash-free fills.
- Positions (shares per (generation, hotkey)) and cash are folded from FillReported, CapitalChanged, DeregSettled and
  ReconAdjusted, so a buy beyond the book's cash fails like the chain (NotEnoughBalanceToStake, fee paid). The fee
  float is not modelled. Buys get shares at the hotkey index of the settlement view; the overlay never touches
  hotkey indexes (the footprint is on reserves only, section 8.3).

Runner contract (WP7): call mark_to() before advance(); observe() every committed event (including those returned by
reserve/submit/advance/resolve) before calling advance() again, or advance() returns the same event again.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from typing import TYPE_CHECKING, Final

from ..core.codec import digest
from ..core.config import ExecCfg
from ..core.events import (
    CapitalChanged,
    DeregSettled,
    FillReported,
    JournalEvent,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    ReconAdjusted,
    SnapshotObserved,
    SubmitStarted,
    VenueAck,
)
from ..core.fixed import DEC, ONE, floor_int
from ..core.orders import FailReason, Fill, OrderIntent, OrderKind, Resolution, VenueCaps
from ..core.state import ChainGlobals, ChainSnapshot, PoolState, SubnetState
from ..core.units import (
    MIN_STAKE_RAO,
    PERQUINTILL,
    PPM,
    AlphaRao,
    Block,
    BookId,
    Hotkey,
    OrderId,
    PositionKey,
    Ppm,
    PriceRao,
    Rao,
    SubnetKey,
)
from ..protocol.amm import SwapError, SwapQuote, max_buy_to_limit, max_sell_to_limit, quote_buy, quote_sell, spot_rao_exact
from ..protocol.fees import nominator_dust, tx_fee_rao

if TYPE_CHECKING:
    from ..core.protocols import ExecutionVenue

__all__ = [
    "ERA_MARGIN_BLOCKS", "LOCK_MARGIN_BLOCKS", "SHIELD_ERA_BLOCKS", "UNSHIELDED_ERA_BLOCKS", "Footprint", "NoFreeDelegate",
    "PendingOrder", "SimVenue", "decay_factor", "draw_ppm",
]

SHIELD_ERA_BLOCKS: Final[int] = 8          # shielded carrier mortal era (CheckMortality <= 8; brief 5.9)
UNSHIELDED_ERA_BLOCKS: Final[int] = 16     # era-16 unshielded risk-exit fallback (section 3.12)
ERA_MARGIN_BLOCKS: Final[int] = 2          # era_end = finalized anchor + era + 2 (section 9.6 step 1)
LOCK_MARGIN_BLOCKS: Final[int] = 2         # a delegate is locked through era_end + 2 after a miss (section 8.5)
UNSHIELDED_INCLUSION_BLOCKS: Final[int] = 1   # an unshielded extrinsic is modelled to land at submit head + 1

_HALF: Final[Decimal] = Decimal(1) / Decimal(2)
_SHARES: Final[Context] = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)   # exact share sums (as core.portfolio)
_ZERO: Final[Decimal] = Decimal(0)

OrderKey = tuple[OrderId, int]


class NoFreeDelegate(RuntimeError):
    """reserve() found every simulated delegate busy (carrier in flight) or nonce-locked after a miss. The planner keeps
    one carrier per delegate (BookView.delegates_free), so this means the caller ignored that projection: leave the
    intent INTENDED and retry on a later tick."""


@dataclass(frozen=True, slots=True)
class Footprint:
    """Cumulative reserve displacement of this book's fills on one generation, as of `stamp`."""
    d_tao: int
    d_alpha: int
    stamp: Block


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """An acked order waiting for its settlement block. `seq` = order of the VenueAck in the journal."""
    order_id: OrderId
    attempt: int
    submit_block: Block
    expected_fill_block: Block
    seq: int


@dataclass(frozen=True, slots=True)
class _Submission:
    delegate: str
    era_end: Block | None


@dataclass(frozen=True, slots=True)
class _Reject:
    reason: FailReason
    detail: str


# ------------------------------------------------------------------------------------------------- pure helpers
def draw_ppm(seed: int, order_id: str, attempt: int, salt: str = "") -> int:
    """Deterministic uniform draw in [0, 1e6): blake2b(seed|order_id|attempt[|salt]) mod 1e6 (section 8.5)."""
    raw = f"{seed}|{order_id}|{attempt}" if not salt else f"{seed}|{order_id}|{attempt}|{salt}"
    return int.from_bytes(hashlib.blake2b(raw.encode(), digest_size=16).digest(), "big") % PPM


def decay_factor(elapsed_blocks: int, half_life_blocks: int | None) -> Decimal:
    """0.5 ** (elapsed / half_life). None = PERSISTENT (1); 0 = TEMPORARY (1 in the stamp block, 0 afterwards).
    A negative elapsed (a view older than the stamp) is clamped to 0."""
    if half_life_blocks is None or elapsed_blocks <= 0:
        return ONE
    if half_life_blocks == 0:
        return _ZERO
    return DEC.power(_HALF, DEC.divide(Decimal(elapsed_blocks), Decimal(half_life_blocks)))


def _trunc(amount: int, factor: Decimal) -> int:
    """amount * factor truncated toward zero (a decayed displacement never exceeds the original in magnitude)."""
    if factor == ONE:
        return amount
    if factor == _ZERO or amount == 0:
        return 0
    return int(DEC.multiply(Decimal(amount), factor))


def _ceil_div(num: int, den: int) -> int:
    return -((-num) // den)


def _sell_shortfall_ppm(p: PoolState, alpha_in: int, tao_out: int) -> Ppm:
    """1 - executed / spot for a (possibly multi-leg) sell against the pre-fill pool p, rounded up (as protocol.amm)."""
    den = alpha_in * p.w_base_e18 * p.px_tao
    if den <= 0:
        return Ppm(0)
    num = alpha_in * p.w_base_e18 * p.px_tao - tao_out * p.w_quote_e18 * p.px_alpha
    return Ppm(_ceil_div(num * PPM, den))


def _value_at_spot_rao(alpha: int, pool: PoolState) -> int:
    """TAO value (rao) of `alpha` alpha rao at the pool's spot, floored. A pool without a pricing reserve (or with a
    degenerate weight) has no spot: its value is 0 (the chain's price there is 0), never a DivisionByZero that would
    escape advance() and crash-loop the Runner on the same pending order."""
    if pool.px_alpha <= 0 or pool.px_tao <= 0 or not 0 < pool.w_quote_e18 < PERQUINTILL:
        return 0
    return floor_int(DEC.multiply(Decimal(alpha), pool.spot()))


def _safe_mode(glob: ChainGlobals, block: Block) -> bool:
    return glob.safe_mode_until is not None and glob.safe_mode_until >= block


def _key(order_id: OrderId, attempt: int) -> OrderKey:
    return (order_id, attempt)


def _book_of(ev: JournalEvent) -> BookId | None:
    """The book an event belongs to; None for run-level events (snapshots, chain events, operator, config, drift)."""
    if isinstance(ev, OrderIntended):
        return ev.intent.book
    if isinstance(ev, FillReported):
        return ev.fill.book
    book = getattr(ev, "book", None)
    return BookId(book) if isinstance(book, str) else None


# ------------------------------------------------------------------------------------------------- the venue
class SimVenue:
    """ExecutionVenue for backtests (stride mode) and the base of PaperVenue (exact mode). One instance per book."""

    KIND: str = "sim"

    def __init__(self, book: BookId, cfg: ExecCfg | None = None, *, seed: int = 0, exact_fills: bool = False) -> None:
        c = cfg if cfg is not None else ExecCfg()
        if c.n_delegates < 1:
            raise ValueError("ExecCfg.n_delegates must be >= 1")
        if c.impact_half_life_blocks is not None and c.impact_half_life_blocks < 0:
            raise ValueError("ExecCfg.impact_half_life_blocks must be None or >= 0")
        if c.latency_blocks < 0 or c.finality_lag_blocks < 0:
            raise ValueError("ExecCfg latency and finality lag must be >= 0")
        for name, ppm in (("shield_miss_ppm", c.shield_miss_ppm), ("fail_inject_ppm", c.fail_inject_ppm)):
            if not 0 <= ppm <= PPM:
                raise ValueError(f"ExecCfg.{name} must be in [0, 1e6]")
        self.book = book
        self.cfg = c
        self.seed = seed
        self.exact_fills = exact_fills
        self.caps = VenueCaps(kind=self.KIND, shield_latency_blocks=c.latency_blocks, era_blocks=SHIELD_ERA_BLOCKS,
                              supports_rotation=False, max_inflight_per_netuid=1, n_delegates=c.n_delegates)
        self._delegates: tuple[str, ...] = tuple(f"sim{i}" for i in range(c.n_delegates))
        # ---- journal-derived state (observe() is the only writer)
        self._intents: dict[OrderKey, OrderIntent] = {}
        self._submissions: dict[OrderKey, _Submission] = {}     # every SubmitStarted seen
        self._open: dict[OrderKey, _Submission] = {}            # submitted and not yet terminal
        self._acks: dict[OrderKey, VenueAck] = {}
        self._pending: dict[OrderKey, PendingOrder] = {}
        self._outcomes: dict[OrderKey, JournalEvent] = {}       # terminal FillReported / OrderFailed
        self._locks: dict[str, Block] = {}                      # delegate -> last locked block (inclusive)
        self._shares: dict[PositionKey, Decimal] = {}
        self._fp: dict[SubnetKey, Footprint] = {}
        self._cash: int = 0
        self._seq: int = 0
        self._seen: set[str] = set()
        # ---- cache only (not state): the last (raw, view) pair produced by mark_to()
        self._last_mark: tuple[ChainSnapshot, ChainSnapshot] | None = None

    # ------------------------------------------------------------------ read-only projections (tests, reports, WP7)
    @property
    def cash(self) -> Rao:
        return Rao(self._cash)

    def shares(self, key: SubnetKey, hotkey: Hotkey) -> Decimal:
        return self._shares.get(PositionKey(key, hotkey), _ZERO)

    def footprint(self, key: SubnetKey) -> Footprint | None:
        return self._fp.get(key)

    def pending(self) -> tuple[PendingOrder, ...]:
        return tuple(sorted(self._pending.values(), key=lambda p: (p.expected_fill_block, p.seq)))

    def delegates_free(self, block: Block) -> tuple[str, ...]:
        return tuple(d for d in self._delegates if self._delegate_free(d, block))

    def delegate_locked_until(self) -> tuple[tuple[str, Block], ...]:
        return tuple(sorted(self._locks.items()))

    def state_digest(self) -> str:
        """Digest of the journal-derived state (two venues folded from the same journal have equal digests)."""
        return digest((
            self._cash, self._seq,
            tuple((k.subnet.netuid, k.subnet.reg_at, k.hotkey, v) for k, v in sorted(self._shares.items())),
            tuple((k.netuid, k.reg_at, f.d_tao, f.d_alpha, f.stamp) for k, f in sorted(self._fp.items())),
            tuple((p.order_id, p.attempt, p.submit_block, p.expected_fill_block, p.seq) for p in self.pending()),
            tuple(sorted(self._locks.items())),
            tuple((k[0], k[1], s.delegate, s.era_end) for k, s in sorted(self._submissions.items())),
            tuple(sorted(self._open)), tuple(sorted(self._acks)), tuple(sorted(self._outcomes)),
            tuple(sorted(self._intents)),
        ))

    # ------------------------------------------------------------------ ExecutionVenue
    def mark_to(self, raw: ChainSnapshot) -> ChainSnapshot:
        """raw + this book's own footprint (section 8.3). Identity when nothing is displaced."""
        view = self._overlay(raw)
        self._last_mark = (raw, view)
        return view

    async def reserve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[str, int | None, Block | None]:
        """("sim<i>", None, era_end) for the lowest-index free delegate; idempotent once SubmitStarted is observed."""
        self._check_book(intent)
        sub = self._submissions.get(_key(intent.order_id, intent.attempt))
        if sub is not None:
            return (sub.delegate, None, sub.era_end)
        era_end = Block(int(now.block) + self._era_len(intent) + ERA_MARGIN_BLOCKS)
        for d in self._delegates:
            if self._delegate_free(d, now.block):
                return (d, None, era_end)
        raise NoFreeDelegate(f"book {self.book}: no free delegate at block {now.block} "
                             f"(locked {sorted(self._locks.items())}, busy {sorted(s.delegate for s in self._open.values())})")

    async def submit(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent:
        """VenueAck or OrderFailed(VENUE_REJECT, tx_fee 0). Idempotent on (order_id, attempt)."""
        self._check_book(intent)
        k = _key(intent.order_id, intent.attempt)
        if k in self._acks:
            return self._acks[k]                # acked once: the ack stays the answer, whatever happened since
        prior = self._outcomes.get(k)
        if isinstance(prior, OrderFailed):      # rejected (or resolved NOT_PLACED) without an ack
            return prior
        return self._submit_result(intent, self._anchor(intent, now.block))

    async def advance(self, view: ChainSnapshot) -> JournalEvent | None:
        """The next due FillReported / OrderFailed (ONE per call) at `view` (= mark_to(snapshot)), or None."""
        due = [p for p in self._pending.values() if p.expected_fill_block <= view.block]
        if not due:
            return None
        p = min(due, key=lambda q: (q.expected_fill_block, q.seq))
        return await self._settle(p, view)

    async def resolve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[Resolution, tuple[JournalEvent, ...]]:
        """UNKNOWN order: re-derive the deterministic ack (PLACED + VenueAck) or reject (NOT_PLACED + OrderFailed)
        that submit() produced (or would have produced) at the SubmitStarted anchor."""
        self._check_book(intent)
        k = _key(intent.order_id, intent.attempt)
        if k in self._outcomes:
            return (Resolution.LANDED, ())
        if k in self._acks or k in self._pending:
            return (Resolution.PLACED, ())
        if k not in self._submissions:
            return (Resolution.NOT_PLACED, (self._failed(intent, FailReason.NOT_PLACED, now.block, 0,
                                                         detail="no SubmitStarted: never sent"),))
        ev = self._submit_result(intent, self._anchor(intent, now.block))
        return (Resolution.PLACED, (ev,)) if isinstance(ev, VenueAck) else (Resolution.NOT_PLACED, (ev,))

    def observe(self, ev: JournalEvent) -> None:
        """Fold one journaled event (every event, in journal order, including recovery)."""
        if isinstance(ev, SnapshotObserved):
            self._prune_footprints(ev.block)
            return
        if _book_of(ev) != self.book:
            return
        idem = ev.idem()
        if idem is not None:
            if idem in self._seen:
                return
            self._seen.add(idem)
        if isinstance(ev, OrderIntended):
            k = _key(ev.intent.order_id, ev.intent.attempt)
            if k not in self._outcomes:
                self._intents[k] = ev.intent
        elif isinstance(ev, OrderCancelled):
            self._intents.pop(_key(ev.order_id, ev.attempt), None)
        elif isinstance(ev, SubmitStarted):
            k = _key(ev.order_id, ev.attempt)
            sub = _Submission(ev.delegate, ev.era_end)
            self._submissions[k] = sub
            if k not in self._outcomes:
                self._open[k] = sub
        elif isinstance(ev, VenueAck):
            k = _key(ev.order_id, ev.attempt)
            self._acks[k] = ev
            if k not in self._outcomes:
                self._pending[k] = PendingOrder(ev.order_id, ev.attempt, ev.submit_block, ev.expected_fill_block, self._seq)
                self._seq += 1
        elif isinstance(ev, FillReported):
            self._apply_fill(ev.fill)
            self._terminal(_key(ev.fill.order_id, ev.fill.attempt), ev)
        elif isinstance(ev, OrderFailed):
            k = _key(ev.order_id, ev.attempt)
            if ev.reason is FailReason.SHIELD_MISSED:
                self._lock_after_miss(k, ev.block)
            self._terminal(k, ev)
        elif isinstance(ev, CapitalChanged):
            self._cash += ev.cash_delta
        elif isinstance(ev, DeregSettled):
            self._cash += ev.payout_tao
            self._shares.pop(PositionKey(ev.key, ev.hotkey), None)
        elif isinstance(ev, ReconAdjusted):
            self._cash += ev.cash_delta
            for pk, delta in ev.share_deltas:
                self._add_shares(pk, delta)

    # ------------------------------------------------------------------ hooks (PaperVenue overrides)
    def _finality_lag(self, anchor: Block) -> int:
        """Blocks between the decision (finalized) block and the modelled submit head."""
        return self.cfg.finality_lag_blocks

    def _extra_submit_check(self, intent: OrderIntent, anchor: Block, expected: Block) -> str | None:
        """Additional pre-submit reject reason (None = accept)."""
        return None

    async def _recorded_state(self, block: Block, intent: OrderIntent) -> ChainSnapshot | None:
        """Exact mode only: the raw state at `block` when the view is already past it. The base venue has none."""
        return None

    async def _after_settle(self, ev: JournalEvent, raw: ChainSnapshot | None, state: ChainSnapshot) -> JournalEvent:
        """Post-settlement hook (PaperVenue: the sim_swap drift probe). `state` is the overlaid settlement view and
        `raw` its un-overlaid source when known."""
        return ev

    # ------------------------------------------------------------------ submission
    def _check_book(self, intent: OrderIntent) -> None:
        if intent.book != self.book:
            raise ValueError(f"intent {intent.order_id} belongs to book {intent.book}, not {self.book}")

    def _era_len(self, intent: OrderIntent) -> int:
        return SHIELD_ERA_BLOCKS if intent.shielded else UNSHIELDED_ERA_BLOCKS

    def _anchor(self, intent: OrderIntent, fallback: Block) -> Block:
        """The finalized block the submission was anchored at: SubmitStarted.era_end - era - margin when journaled
        (so submit() and a later resolve() agree), else the caller's block."""
        sub = self._submissions.get(_key(intent.order_id, intent.attempt))
        if sub is not None and sub.era_end is not None:
            return Block(int(sub.era_end) - self._era_len(intent) - ERA_MARGIN_BLOCKS)
        return fallback

    def _submit_result(self, intent: OrderIntent, anchor: Block) -> JournalEvent:
        k = _key(intent.order_id, intent.attempt)
        step = self.cfg.latency_blocks if intent.shielded else UNSHIELDED_INCLUSION_BLOCKS
        submit_block = Block(int(anchor) + self._finality_lag(anchor))
        expected = Block(int(submit_block) + step)
        planned = int(anchor) + self.cfg.finality_lag_blocks + step
        reject: str | None = None
        if intent.kind is OrderKind.MOVE_STAKE_LIMIT:
            reject = "move_stake_limit is disabled in v1 (caps.supports_rotation = False)"
        elif intent.kind is OrderKind.MOVE_STAKE and (intent.dest_hotkey == intent.hotkey
                                                      or (intent.dest_key is not None and intent.dest_key != intent.key)):
            reject = "move_stake must change the hotkey within the same generation"
        elif planned > intent.valid_until:
            reject = f"ttl_expired: planned inclusion {planned} > valid_until {intent.valid_until}"
        else:
            other = self._open_on_netuid(int(intent.key.netuid), k)
            if other is not None:
                reject = f"inflight_netuid: order {other[0]}:{other[1]} is open on netuid {intent.key.netuid}"
            else:
                reject = self._extra_submit_check(intent, anchor, expected)
        if reject is not None:
            return self._failed(intent, FailReason.VENUE_REJECT, anchor, 0, detail=reject)
        return VenueAck(book=self.book, order_id=intent.order_id, attempt=intent.attempt, submit_block=submit_block,
                        expected_fill_block=expected, carrier_hash="", inner_hash="")

    def _open_on_netuid(self, netuid: int, me: OrderKey) -> OrderKey | None:
        for k in sorted(self._open):
            if k == me:
                continue
            it = self._intents.get(k)
            if it is not None and int(it.key.netuid) == netuid:
                return k
        return None

    # ------------------------------------------------------------------ delegates
    def _delegate_free(self, delegate: str, block: Block) -> bool:
        if any(s.delegate == delegate for s in self._open.values()):
            return False
        return block > self._locks.get(delegate, Block(-1))

    def _lock_after_miss(self, k: OrderKey, block: Block) -> None:
        sub = self._submissions.get(k)
        if sub is None:
            return
        if sub.era_end is not None:
            era_end = int(sub.era_end)
        else:                                   # no era journaled: assume the carrier era ran from its ack
            intent = self._intents.get(k)
            era = SHIELD_ERA_BLOCKS if intent is None or intent.shielded else UNSHIELDED_ERA_BLOCKS
            ack = self._acks.get(k)
            era_end = (int(ack.submit_block) if ack is not None else int(block)) + era + ERA_MARGIN_BLOCKS
        until = Block(era_end + LOCK_MARGIN_BLOCKS)
        self._locks[sub.delegate] = max(self._locks.get(sub.delegate, Block(-1)), until)

    # ------------------------------------------------------------------ settlement
    def _intent(self, k: OrderKey) -> OrderIntent:
        intent = self._intents.get(k)
        if intent is None:
            raise RuntimeError(f"book {self.book}: order {k[0]}:{k[1]} was acked but its OrderIntended was never observed")
        return intent

    def _era_end_of(self, k: OrderKey, p: PendingOrder, intent: OrderIntent) -> int:
        sub = self._submissions.get(k)
        if sub is not None and sub.era_end is not None:
            return int(sub.era_end)
        anchor = int(p.submit_block) - self.cfg.finality_lag_blocks
        return anchor + self._era_len(intent) + ERA_MARGIN_BLOCKS

    async def _settle(self, p: PendingOrder, view: ChainSnapshot) -> JournalEvent:
        k = _key(p.order_id, p.attempt)
        intent = self._intent(k)
        raw: ChainSnapshot | None = None
        state = view
        exact = view.block == p.expected_fill_block
        if self.exact_fills and not exact:
            rec = await self._recorded_state(p.expected_fill_block, intent)
            if rec is not None and rec.block == p.expected_fill_block:
                raw, state, exact = rec, self._overlay(rec), True
            elif intent.shielded:
                return self._failed(intent, FailReason.SHIELD_MISSED, view.block, self.cfg.carrier_fee_rao, expired=True,
                                    exact_block=False, detail=f"fill_state_unavailable: N+2 = {p.expected_fill_block}")
            elif view.block > self._era_end_of(k, p, intent):
                return self._failed(intent, FailReason.ERA_EXPIRED, view.block, 0, expired=True, exact_block=False,
                                    detail="unshielded extrinsic not included before its era end")
            else:
                exact = True                    # an unshielded extrinsic may land at any block in (submit, era_end]
        ev = self._evaluate(intent, state, exact)
        return await self._after_settle(ev, raw, state)

    def _failed(self, intent: OrderIntent, reason: FailReason, block: Block, tx_fee: int, *, expired: bool = False,
                exact_block: bool = True, detail: str = "") -> OrderFailed:
        return OrderFailed(book=self.book, order_id=intent.order_id, attempt=intent.attempt, block=block, reason=reason,
                           tx_fee=Rao(tx_fee), expired=expired, exact_block=exact_block, detail=detail)

    def _evaluate(self, intent: OrderIntent, state: ChainSnapshot, exact: bool) -> JournalEvent:
        """The chain's verdict on `intent` included at `state.block` (pure)."""
        block = state.block
        if intent.shielded and draw_ppm(self.seed, intent.order_id, intent.attempt) < self.cfg.shield_miss_ppm:
            return self._failed(intent, FailReason.SHIELD_MISSED, block, self.cfg.carrier_fee_rao, expired=True,
                                exact_block=exact, detail="injected: carrier included, inner not decrypted")
        fee = tx_fee_rao(intent.kind, self.cfg)
        s = state.get(intent.key)
        if s is None:
            return self._failed(intent, FailReason.SUBNET_NOT_EXISTS, block, fee, exact_block=exact, detail="pool_gone")
        if _safe_mode(state.glob, block):
            return self._failed(intent, FailReason.SAFE_MODE, block, fee, exact_block=exact,
                                detail=f"SafeMode until {state.glob.safe_mode_until}")
        if self.cfg.fail_inject_ppm and draw_ppm(self.seed, intent.order_id, intent.attempt, "inner") < self.cfg.fail_inject_ppm:
            return self._failed(intent, FailReason.OTHER, block, fee, exact_block=exact, detail="injected inner-call failure")
        res: Fill | _Reject
        if intent.kind is OrderKind.ADD_STAKE_LIMIT:
            res = self._buy(intent, s, block, fee, exact)
        elif intent.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
            res = self._sell(intent, s, state.glob, block, fee, exact)
        elif intent.kind is OrderKind.MOVE_STAKE:
            res = self._move(intent, s, block, fee, exact)
        else:
            res = _Reject(FailReason.CALL_FILTERED, "move_stake_limit is disabled in v1")
        if isinstance(res, _Reject):
            return self._failed(intent, res.reason, block, fee, exact_block=exact, detail=res.detail)
        return FillReported(res)

    def _fill(self, intent: OrderIntent, block: Block, exact: bool, *, tao: int, alpha: int, shares: Decimal,
              swap_fee: int, tx_fee: int, d_pool_tao: int, d_pool_alpha: int, spot_before: PriceRao, shortfall_ppm: int,
              complete: bool, author_fee_tao: int = 0, dest_hotkey: Hotkey | None = None,
              dest_shares: Decimal | None = None) -> Fill:
        return Fill(fill_id=f"{intent.order_id}:{intent.attempt}:0", order_id=intent.order_id, attempt=intent.attempt,
                    book=self.book, block=block, kind=intent.kind, key=intent.key, hotkey=intent.hotkey, tao=Rao(tao),
                    alpha=AlphaRao(alpha), shares=shares, swap_fee=swap_fee, author_fee_tao=Rao(author_fee_tao),
                    tx_fee=Rao(tx_fee), d_pool_tao=d_pool_tao, d_pool_alpha=d_pool_alpha, spot_before=spot_before,
                    shortfall_ppm=Ppm(shortfall_ppm), complete=complete, exact_block=exact,
                    dest_key=intent.key if dest_hotkey is not None else None, dest_hotkey=dest_hotkey,
                    dest_shares=dest_shares)

    def _buy(self, intent: OrderIntent, s: SubnetState, block: Block, fee: int, exact: bool) -> Fill | _Reject:
        pool = s.pool
        if not s.subtoken_enabled:
            return _Reject(FailReason.SUBTOKEN_DISABLED, "")
        idx = s.hotkey(intent.hotkey)
        if idx is None:
            return _Reject(FailReason.OTHER, f"sim:hotkey_untracked {intent.hotkey}")
        if intent.tao_in > self._cash:
            return _Reject(FailReason.OTHER, f"NotEnoughBalanceToStake: need {intent.tao_in}, have {self._cash}")
        amount = int(intent.tao_in)
        complete = True
        try:
            if Decimal(intent.limit_price) <= spot_rao_exact(pool):
                return _Reject(FailReason.PRICE_LIMIT_EXCEEDED, f"buy limit {intent.limit_price} <= spot {pool.spot_rao()}")
            cap = max_buy_to_limit(pool, intent.limit_price)
            if amount > cap:
                if not intent.allow_partial:
                    return _Reject(FailReason.SLIPPAGE_TOO_HIGH, f"tao_in {amount} > max_buy_to_limit {cap}")
                amount, complete = int(cap), False
            q = quote_buy(pool, Rao(amount))
        except SwapError as e:
            return _Reject(e.reason, "")
        return self._fill(intent, block, exact, tao=q.amount_in, alpha=q.amount_out, shares=idx.shares_for(q.amount_out),
                          swap_fee=q.fee, tx_fee=fee, d_pool_tao=q.d_tao, d_pool_alpha=q.d_alpha,
                          spot_before=q.spot_before, shortfall_ppm=q.shortfall_ppm, complete=complete)

    def _position(self, intent: OrderIntent, s: SubnetState) -> tuple[Decimal, int, Decimal] | _Reject:
        """(held shares, alpha to take, shares to take) for a sell or move on (key, hotkey)."""
        held = self._shares.get(PositionKey(intent.key, intent.hotkey), _ZERO)
        if held <= 0:
            return _Reject(FailReason.NOT_ENOUGH_STAKE, "no position on this hotkey")
        idx = s.hotkey(intent.hotkey)
        if idx is None:
            return _Reject(FailReason.OTHER, f"sim:hotkey_untracked {intent.hotkey}")
        value = int(idx.value_of(held))
        if intent.full_position or intent.kind is OrderKind.REMOVE_STAKE_FULL_LIMIT:
            return (held, value, held)
        alpha = int(intent.alpha_in)
        if alpha > value:
            return _Reject(FailReason.NOT_ENOUGH_STAKE, f"alpha {alpha} > position value {value}")
        take = held if alpha == value else min(held, idx.shares_for(alpha))
        return (held, alpha, take)

    def _sell(self, intent: OrderIntent, s: SubnetState, glob: ChainGlobals, block: Block, fee: int,
              exact: bool) -> Fill | _Reject:
        pos = self._position(intent, s)
        if isinstance(pos, _Reject):
            return pos
        held, alpha, take = pos
        idx = s.hotkey(intent.hotkey)
        assert idx is not None                  # checked by _position
        pool = s.pool
        complete = True
        try:
            if intent.limit_price > 0:          # limit 0 = no floor (remove_stake_full_limit None, dust force-sale)
                if Decimal(intent.limit_price) >= spot_rao_exact(pool):
                    return _Reject(FailReason.PRICE_LIMIT_EXCEEDED,
                                   f"sell limit {intent.limit_price} >= spot {pool.spot_rao()}")
                cap = int(max_sell_to_limit(pool, intent.limit_price))
                if alpha > cap:
                    if not intent.allow_partial:
                        return _Reject(FailReason.SLIPPAGE_TOO_HIGH, f"alpha {alpha} > max_sell_to_limit {cap}")
                    alpha, complete = cap, False
                    take = min(held, idx.shares_for(alpha))
            rest = _SHARES.subtract(held, take)
            if rest > 0 and idx.value_of(rest) == 0:     # zero-value share residue goes with the sale
                take, rest = held, _ZERO
            q = quote_sell(pool, AlphaRao(alpha), partial_remaining=rest > 0)
        except SwapError as e:
            return _Reject(e.reason, "")
        legs: list[SwapQuote] = [q]
        total_alpha = alpha
        if rest > 0:                            # nominator dust: the chain force-sells the remainder at no limit
            remainder = int(idx.value_of(rest))
            after = pool.shifted(q.d_tao, q.d_alpha)
            if nominator_dust(remainder, after, glob):
                try:
                    legs.append(quote_sell(after, AlphaRao(remainder)))
                    total_alpha += remainder
                    take, complete = held, True
                except SwapError:
                    pass                        # unsellable residue stays staked (re-exited by the planner)
        tao = sum(leg.amount_out for leg in legs)
        shortfall = q.shortfall_ppm if len(legs) == 1 else _sell_shortfall_ppm(pool, total_alpha, tao)
        return self._fill(intent, block, exact, tao=tao, alpha=total_alpha, shares=take,
                          swap_fee=sum(leg.fee for leg in legs), author_fee_tao=sum(leg.author_fee_tao for leg in legs),
                          tx_fee=fee, d_pool_tao=sum(leg.d_tao for leg in legs),
                          d_pool_alpha=sum(leg.d_alpha for leg in legs), spot_before=q.spot_before,
                          shortfall_ppm=shortfall, complete=complete)

    def _move(self, intent: OrderIntent, s: SubnetState, block: Block, fee: int, exact: bool) -> Fill | _Reject:
        assert intent.dest_hotkey is not None   # OrderIntent.__post_init__
        pos = self._position(intent, s)
        if isinstance(pos, _Reject):
            return pos
        _, alpha, take = pos
        dest = s.hotkey(intent.dest_hotkey)
        if dest is None:
            return _Reject(FailReason.OTHER, f"sim:hotkey_untracked {intent.dest_hotkey}")
        if alpha <= 0 or _value_at_spot_rao(alpha, s.pool) < MIN_STAKE_RAO:
            return _Reject(FailReason.AMOUNT_TOO_LOW, f"move of {alpha} alpha is below DefaultMinStake")
        return self._fill(intent, block, exact, tao=0, alpha=alpha, shares=take, swap_fee=0, tx_fee=fee, d_pool_tao=0,
                          d_pool_alpha=0, spot_before=s.pool.spot_rao(), shortfall_ppm=0, complete=True,
                          dest_hotkey=intent.dest_hotkey, dest_shares=dest.shares_for(alpha))

    # ------------------------------------------------------------------ folding
    def _terminal(self, k: OrderKey, ev: JournalEvent) -> None:
        self._outcomes[k] = ev
        self._pending.pop(k, None)
        self._open.pop(k, None)
        self._intents.pop(k, None)

    def _add_shares(self, pk: PositionKey, delta: Decimal) -> None:
        new = _SHARES.add(self._shares.get(pk, _ZERO), delta)
        if new > 0:
            self._shares[pk] = new
        else:
            self._shares.pop(pk, None)

    def _apply_fill(self, f: Fill) -> None:
        if f.kind is OrderKind.ADD_STAKE_LIMIT:
            self._cash -= f.tao
            self._add_shares(PositionKey(f.key, f.hotkey), f.shares)
        elif f.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
            self._cash += f.tao
            self._add_shares(PositionKey(f.key, f.hotkey), _SHARES.minus(f.shares))
        elif f.kind is OrderKind.MOVE_STAKE and f.dest_hotkey is not None:
            self._add_shares(PositionKey(f.key, f.hotkey), _SHARES.minus(f.shares))
            dest_key = f.dest_key if f.dest_key is not None else f.key
            self._add_shares(PositionKey(dest_key, f.dest_hotkey), f.dest_shares if f.dest_shares is not None else _ZERO)
        if f.d_pool_tao or f.d_pool_alpha:
            self._fold_footprint(f.key, f.block, f.d_pool_tao, f.d_pool_alpha)

    def _shift(self, fp: Footprint, block: Block) -> tuple[int, int]:
        factor = decay_factor(int(block) - int(fp.stamp), self.cfg.impact_half_life_blocks)
        return (_trunc(fp.d_tao, factor), _trunc(fp.d_alpha, factor))

    def _fold_footprint(self, key: SubnetKey, block: Block, d_tao: int, d_alpha: int) -> None:
        old = self._fp.get(key)
        dt, da = self._shift(old, block) if old is not None else (0, 0)
        nt, na = dt + d_tao, da + d_alpha
        if nt == 0 and na == 0:
            self._fp.pop(key, None)
        else:
            self._fp[key] = Footprint(nt, na, block)

    def _prune_footprints(self, block: Block) -> None:
        """Drop displacements that have decayed to (0, 0) by `block` (journal-driven, so rebuilds prune identically)."""
        for key in sorted(self._fp):
            fp = self._fp[key]
            if block > fp.stamp and self._shift(fp, block) == (0, 0):
                del self._fp[key]

    def _overlay(self, raw: ChainSnapshot) -> ChainSnapshot:
        if not self._fp:
            return raw
        subnets = list(raw.subnets)
        applied: list[str] = []
        for i, s in enumerate(subnets):
            fp = self._fp.get(s.key)
            if fp is None:
                continue
            dt, da = self._shift(fp, raw.block)
            p = s.pool
            if (dt == 0 and da == 0) or min(p.tao + dt, p.alpha + da, p.px_tao + dt, p.px_alpha + da) < 1:
                continue
            subnets[i] = replace(s, pool=p.shifted(dt, da))
            applied.append(f"{s.key.netuid}:{s.key.reg_at}:{dt}:{da}")
        if not applied:
            return raw
        tag = hashlib.blake2b(f"{raw.digest}|{raw.block_hash}|own-footprint|{'|'.join(applied)}".encode(),
                              digest_size=16).hexdigest()
        return replace(raw, subnets=tuple(subnets), digest=tag)

    def unmark(self, view: ChainSnapshot) -> ChainSnapshot:
        """The raw snapshot behind `view`: the cached source when `view` came from the latest mark_to(), else the
        overlay subtracted again (exact unless a shift was skipped as infeasible on a degenerate pool)."""
        if self._last_mark is not None and self._last_mark[1] is view:
            return self._last_mark[0]
        if not self._fp:
            return view
        subnets = list(view.subnets)
        for i, s in enumerate(subnets):
            fp = self._fp.get(s.key)
            if fp is None:
                continue
            dt, da = self._shift(fp, view.block)
            p = s.pool
            if (dt or da) and min(p.tao - dt, p.alpha - da, p.px_tao - dt, p.px_alpha - da) >= 1:
                subnets[i] = replace(s, pool=p.shifted(-dt, -da))
        return replace(view, subnets=tuple(subnets))


if TYPE_CHECKING:
    def _conforms(v: SimVenue) -> ExecutionVenue:     # mypy: SimVenue satisfies the section-5.10 protocol
        return v
