"""taotrader/live/nonce.py - delegate rotation, nonce locks and the live_submissions sidecar (WP11; DESIGN.md 9.6).

Delegates (section 9.6 step 6): 2-3 funded ProxyType::Staking delegates, ONE in-flight carrier each (carrier nonce n,
inner n + 1). A delegate is busy from SubmitStarted until the order's terminal event. After a shield miss the stale
carrier may hold nonce n until its era expires, so the delegate stays locked THROUGH era_end + 2 (free when
block > era_end + 2; the same boundary as venues.sim and engine.reducer's BookView.delegates_free) and the retry rotates
to another free delegate.

`DelegateLedger` is a pure fold of journaled events (LiveVenue.observe feeds it), so a restarted venue rebuilds the same
locks. It also tracks the next on-chain nonce we EXPECT for each delegate once its outcomes are settled; reconciliation
compares it with System.Account(delegate).nonce (a mismatch is the "delegate nonce jump" key alarm). While an outcome is
pending (in flight, or a carrier-absent miss awaiting carrier-fee settlement) the expectation is unknown and not checked.

Nonce consumption per outcome (n = the carrier nonce): fill or inner failure -> n + 2; carrier present, inner absent
-> n + 1; carrier absent -> settled later from the nonce at era_end + 1 (never_included n, carrier_only n + 1,
inner_included n + 2); NOT_PLACED / pre-submit reject -> n; unshielded plain extrinsic included -> n + 1, era expired -> n.

The sidecar (`live_submissions` in data/runs/<run_id>/state.sqlite, section 7.3) is written BEFORE the SDK write and
updated from its result; it carries what the journal cannot (the delegate balance before the send, the used nonce, the
submit head and the carrier/inner hashes) for resolve() and carrier-fee settlement after a crash.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from typing import Final, Protocol

from ..core.units import Block, OrderId

__all__ = [
    "ERA_MARGIN_BLOCKS", "LOCK_MARGIN_BLOCKS", "NOT_PLACED_GRACE_BLOCKS", "DelegateLedger", "MemorySubmissions",
    "NoFreeDelegate", "OrderKey", "SqliteSubmissions", "SubmissionRow", "SubmissionStore", "era_anchor", "era_end_for",
]

SHIELD_ERA_BLOCKS: Final[int] = 8
PLAIN_ERA_BLOCKS: Final[int] = 16
ERA_MARGIN_BLOCKS: Final[int] = 2           # era_end = finalized anchor + period + 2 (the SDK's own anchor read)
LOCK_MARGIN_BLOCKS: Final[int] = 2          # after a miss the delegate is locked through era_end + 2
NOT_PLACED_GRACE_BLOCKS: Final[int] = 8     # resolve: nonce still n once the finalized head > era_end + 8 -> NOT_PLACED

OrderKey = tuple[OrderId, int]


def era_end_for(anchor: int, shielded: bool) -> Block:
    """Last block of the carrier's mortal era plus the SDK anchor margin (section 9.6 step 1)."""
    return Block(anchor + (SHIELD_ERA_BLOCKS if shielded else PLAIN_ERA_BLOCKS) + ERA_MARGIN_BLOCKS)


def era_anchor(era_end: int, shielded: bool) -> Block:
    """Inverse of era_end_for: the finalized anchor the era was computed from."""
    return Block(era_end - (SHIELD_ERA_BLOCKS if shielded else PLAIN_ERA_BLOCKS) - ERA_MARGIN_BLOCKS)


class NoFreeDelegate(RuntimeError):
    """reserve() found every delegate busy (carrier in flight) or nonce-locked after a miss. The Runner leaves the
    intent INTENDED and retries on a later tick (the planner respects BookView.delegates_free, so this is rare)."""


@dataclass(frozen=True, slots=True)
class DelegateUse:
    order: OrderKey
    delegate: str
    nonce: int | None                      # reserved carrier nonce n (None in plan-only mode)
    era_end: Block | None
    shielded: bool


class DelegateLedger:
    """Delegate availability and nonce expectations, folded from journaled events (deterministic)."""

    def __init__(self, delegates: Sequence[str]) -> None:
        if not delegates or len(set(delegates)) != len(delegates):
            raise ValueError("the live adapter needs one or more distinct delegates")
        self.delegates: tuple[str, ...] = tuple(delegates)
        self.uses: dict[OrderKey, DelegateUse] = {}
        self.in_flight: dict[str, OrderKey] = {}
        self.locked_until: dict[str, int] = {}
        self.expected: dict[str, int] = {}
        self.awaiting_settlement: dict[str, OrderKey] = {}

    # ---------------------------------------------------------------- fold
    def started(self, order: OrderKey, delegate: str, nonce: int | None, era_end: Block | None, shielded: bool) -> None:
        self.uses[order] = DelegateUse(order, delegate, nonce, era_end, shielded)
        self.in_flight[delegate] = order

    def used_nonce(self, order: OrderKey, nonce: int) -> None:
        """The SDK used another nonce than the reserved one (SubmitUnknown "nonce_mismatch:<used>")."""
        use = self.uses.get(order)
        if use is not None:
            self.uses[order] = replace(use, nonce=nonce)

    def finished(self, order: OrderKey, *, consumed: int | None, missed: bool) -> None:
        """Terminal outcome. consumed = nonces used from n (0, 1, 2), None = unknown until carrier-fee settlement."""
        use = self.uses.get(order)
        if use is None:
            return
        if self.in_flight.get(use.delegate) == order:
            del self.in_flight[use.delegate]
        if missed and use.era_end is not None:
            self.locked_until[use.delegate] = max(self.locked_until.get(use.delegate, -1),
                                                  int(use.era_end) + LOCK_MARGIN_BLOCKS)
        if use.nonce is None:
            return
        if consumed is None:
            self.awaiting_settlement[use.delegate] = order
            self.expected.pop(use.delegate, None)
        else:
            self.awaiting_settlement.pop(use.delegate, None)
            self.expected[use.delegate] = use.nonce + consumed

    def settled(self, order: OrderKey, consumed: int) -> None:
        use = self.uses.get(order)
        if use is None or use.nonce is None:
            return
        if self.awaiting_settlement.get(use.delegate) == order:
            del self.awaiting_settlement[use.delegate]
        self.expected[use.delegate] = use.nonce + consumed

    # ---------------------------------------------------------------- queries
    def busy(self, delegate: str) -> bool:
        return delegate in self.in_flight

    def free(self, block: int) -> tuple[str, ...]:
        return tuple(d for d in self.delegates if d not in self.in_flight and self.locked_until.get(d, -1) < block)

    def pick(self, block: int, avoid: Sequence[str] = ()) -> str:
        """The first free delegate in configured order, preferring ones not in `avoid` (the previous attempt's)."""
        free = self.free(block)
        if not free:
            raise NoFreeDelegate(f"no free delegate at block {block}: busy {sorted(self.in_flight)}, "
                                 f"locked {sorted((d, u) for d, u in self.locked_until.items() if u >= block)}")
        fresh = [d for d in free if d not in avoid]
        return (fresh or list(free))[0]

    def expected_nonce(self, delegate: str) -> int | None:
        """Next on-chain nonce once every own outcome is settled; None while one is pending or nothing is known."""
        if delegate in self.in_flight or delegate in self.awaiting_settlement:
            return None
        return self.expected.get(delegate)

    def use(self, order: OrderKey) -> DelegateUse | None:
        return self.uses.get(order)


# ------------------------------------------------------------------------------------------------- sidecar
@dataclass(frozen=True, slots=True)
class SubmissionRow:
    """One row of state.sqlite live_submissions (section 7.3); written BEFORE any send."""
    order_id: str
    attempt: int
    delegate: str
    nonce: int | None = None
    era_start: int | None = None
    era_end: int | None = None
    submit_head: int | None = None
    expected_fill_block: int | None = None
    used_nonce: int | None = None
    delegate_free_before: int | None = None
    carrier_hash: str | None = None
    inner_hash: str | None = None
    carrier_fee_settled: int | None = None
    state: str = "sending"                 # sending | sent | nonce_mismatch | raised | plan_only


class SubmissionStore(Protocol):
    def put(self, row: SubmissionRow) -> None: ...
    def get(self, order_id: str, attempt: int) -> SubmissionRow | None: ...


class MemorySubmissions:
    """In-process sidecar (tests, live-dry)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], SubmissionRow] = {}

    def put(self, row: SubmissionRow) -> None:
        self.rows[(row.order_id, row.attempt)] = row

    def get(self, order_id: str, attempt: int) -> SubmissionRow | None:
        return self.rows.get((order_id, attempt))


_COLS: Final[tuple[str, ...]] = tuple(f.name for f in fields(SubmissionRow))


class SqliteSubmissions:
    """The live_submissions table of a run-state connection (taotrader.data.journal.open_run_state, which creates it
    with WAL + synchronous=FULL in autocommit mode, so every put is durable before the send)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def put(self, row: SubmissionRow) -> None:
        sql = (f"INSERT OR REPLACE INTO live_submissions ({', '.join(_COLS)}) "
               f"VALUES ({', '.join('?' for _ in _COLS)})")
        with self.conn:
            self.conn.execute(sql, tuple(getattr(row, c) for c in _COLS))

    def get(self, order_id: str, attempt: int) -> SubmissionRow | None:
        cur = self.conn.execute(f"SELECT {', '.join(_COLS)} FROM live_submissions WHERE order_id = ? AND attempt = ?",
                                (order_id, attempt))
        r = cur.fetchone()
        if r is None:
            return None
        return SubmissionRow(**dict(zip(_COLS, r, strict=True)))
