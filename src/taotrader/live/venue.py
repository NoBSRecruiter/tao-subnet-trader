"""taotrader/live/venue.py - LiveVenue: the gated live ExecutionVenue (WP11; DESIGN.md 9.3, 9.4, 9.6, 9.8).

`mark_to` is the identity (the chain already contains our footprint). Everything the venue needs after a restart is
folded from journaled events by `observe()` (intents, SubmitStarted, acks, outcomes, carrier-fee settlements, key-alarm
ReconAdjusted / QuarantineCleared, positions, buy turnover); what the journal cannot carry (the delegate balance before
the send, the used nonce, the submit head, the carrier and inner hashes) lives in the live_submissions sidecar, written
BEFORE the send.

reserve(): a free delegate (one in-flight carrier each; locked through era_end + 2 after a miss), its pool-aware
system_accountNextIndex read immediately before the Runner journals SubmitStarted(delegate, n, era_end), with
era_end = finalized head + 8 (16 unshielded) + 2. A delegate whose next index differs from the nonce the journal implies
is skipped (foreign use; reconciliation raises the key alarm).

submit() checks, in order (any failure -> OrderFailed(VENUE_REJECT, tx_fee 0, detail=<reason>); nothing is sent):
  idempotency (an order already sent is never re-sent: SubmitUnknown("already_sent") -> resolve()); MOVE_STAKE_LIMIT
  disabled; kill file; key alarm (FROZEN: only EMERGENCY fill-or-kill sells, allow_emergency_exits_when_frozen);
  arming (UNARMED: only EMERGENCY/URGENT full sells, only with risk_exits_when_unarmed and V2/V3/V6 passing); the
  generation still exists; SafeMode; SubtokenEnabled for buys; head lag < 2; shielded needs finality lag <= 5
  (else the shield era is stale), unshielded only for risk exits; staleness (finalized head past valid_until);
  exact amounts from post_state at the fresh finalized head (full exits and moves: PostState.alpha_value, never 'all';
  long-only: never more than the position); a fresh runtime quote (all-zero -> reject); the limit recomputed from the
  runtime spot but never looser than the planner's, and strictly on the right side of that spot; fill-or-kill size
  <= max_*_to_limit; BUY-ONLY caps (max_order_tao, max_daily_turnover_tao over 7,200 blocks, max_position_tao,
  allowed_netuids, MIN_FREE_REAL) - sells and moves are bounded only by the position, so a 5-TAO Tier A exit with
  max_order_tao = 1 is submitted; the fee payer's TAO (below max_fee -> nothing; below fee_float_exits_rao -> risk
  exits only: the alpha-fee trap); plan() under the per-call Policy shows no violations.
  Plan-only (live-dry): the same path, then OrderFailed(VENUE_REJECT, detail "plan_only:accepted:fee=<rao>" or
  "plan_only:<reason>") - real reads and plan(), journaled, never sent.
  Submit: sidecar row first, then submit_shielded (period 8) -> VenueAck(N, N + 2) if the carrier nonce is the reserved
  n, else SubmitUnknown("nonce_mismatch:<used>"); an exception propagates (the Runner journals SubmitUnknown).
  Unshielded risk-exit fallback: submit_plain (period 16), acked with expected_fill_block N + 1 and found by hash in
  N + 1 .. era_end.

advance(view), view = the finalized tick snapshot, one event per call:
  - shielded, finalized >= N + 2: carrier absent from N + 2 -> OrderFailed(SHIELD_MISSED, expired, tx_fee 0) at once
    (the delegate stays locked until era_end + 2); carrier present but inner (carrier index + 1) absent ->
    SHIELD_MISSED with the carrier fee; ExtrinsicFailed -> OrderFailed(reason, fees paid); Proxy.ProxyExecuted{Err}
    under ExtrinsicSuccess -> OrderFailed(matching FailReason or PROXY_ERROR, detail = decoded name, fees paid);
    success -> FillReported from SHARE deltas (post_state at N + 1 and N + 2; Fill.alpha = delta x post index), never
    from value deltas or StakeAdded; Fill.tao = the real coldkey's free-TAO change, apportioned by each inner's
    StakeAdded/StakeRemoved TAO when several own orders land in the same block (the residual is reconciliation's);
  - carrier-fee settlement of a carrier-absent miss once finalized > era_end, from the delegate nonce at era_end + 1:
    n -> CarrierFeeSettled(0, never_included); n + 1 -> carrier fee (event, else the balance change), carrier_only;
    >= n + 2 -> inner_included (key alarm: an inner executed after a declared miss, or foreign use of the key);
  - unshielded: found -> as above; finalized > era_end and not found -> OrderFailed(ERA_EXPIRED, expired, fee 0).
  Provider errors make advance() return None (retry next tick: finalized chain truth does not change).

resolve() (after a crash or SubmitUnknown), from SubmitStarted and the sidecar: delegate nonce at the finalized head
still n and finalized > era_end + 8 -> NOT_PLACED; nonce >= n + 1 -> the carrier found by (delegate, n) [or by the
sidecar hash] gives N + 2 and the step-4 outcome (LANDED); otherwise UNRESOLVABLE_YET. Nothing is ever re-sent.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from ..core.config import ExecCfg, LiveCfg, RiskCfg
from ..core.events import (
    CapitalChanged,
    CarrierFeeSettled,
    DeregSettled,
    FillReported,
    HealthObs,
    JournalEvent,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    QuarantineCleared,
    ReconAdjusted,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from ..core.fixed import DEC, floor_int
from ..core.orders import FailReason, Fill, OrderIntent, OrderKind, Resolution, Urgency, VenueCaps
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import (
    BLOCKS_PER_DAY,
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    Ppm,
    PriceRao,
    Rao,
    SubnetKey,
)
from ..protocol.amm import SwapError, liq_value, max_buy_to_limit, max_sell_to_limit, quote_buy
from ..protocol.fees import swap_fee
from .gate import Arming, LiveState
from .nonce import (
    NOT_PLACED_GRACE_BLOCKS,
    DelegateLedger,
    MemorySubmissions,
    NoFreeDelegate,
    OrderKey,
    SubmissionRow,
    SubmissionStore,
    era_anchor,
    era_end_for,
)
from .preflight import MAX_FINALITY_LAG_BLOCKS, MAX_HEAD_LAG_BLOCKS, tao_to_rao
from .sdk_port import LiveCall, LiveCallError, LiveReader, PostState, SdkPort, error_reason, ss58_encode

if TYPE_CHECKING:
    from ..core.protocols import ExecutionVenue

__all__ = ["KEY_ALARM_PREFIX", "LiveVenue", "RiskExitGuard", "VenueReject"]

log = logging.getLogger("taotrader.live.venue")

KEY_ALARM_PREFIX: Final[str] = "key_alarm:"         # ReconAdjusted.evidence prefix of a key alarm (FROZEN)
SHIELD_LATENCY_BLOCKS: Final[int] = 2               # the only legal inclusion block is submit head + 2
EMERGENCY_FROZEN_SLIP_PPM: Final[int] = 50_000      # FROZEN exception: fill-or-kill with s <= 5%
TURNOVER_WINDOW_BLOCKS: Final[int] = BLOCKS_PER_DAY
_EXACT: Final[Context] = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
_ZERO: Final[Decimal] = Decimal(0)

Event = tuple[str, str, dict[str, object]]


class RiskExitGuard(Protocol):
    """V2/V3/V6 on the current spec (preflight.RiskExitChecks): the precondition of the UNARMED risk-exit exception."""
    async def ok(self, snap: ChainSnapshot) -> bool: ...


class VenueReject(Exception):
    """Internal: a pre-submit check refused the order (-> OrderFailed(VENUE_REJECT, detail=reason))."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Landing:
    """Where an order's extrinsic(s) sit in the inclusion block."""
    block: int
    index: int                                  # carrier (shielded) or the plain extrinsic
    inner_index: int | None                     # shielded inner (carrier index + 1) or None
    shielded: bool


def _fees(events: Sequence[Event]) -> int:
    return sum(int(str(f.get("actual_fee", 0))) for _, e, f in events if e == "TransactionFeePaid")


def _dispatch(events: Sequence[Event]) -> tuple[str, FailReason | None, str]:
    """("ok" | "failed" | "proxy_err", reason, decoded error name) of one extrinsic's events."""
    for _, e, f in events:
        if e == "ExtrinsicFailed":
            name = str(f.get("error", "") or "")
            return "failed", error_reason(name, proxied=False), name or "ExtrinsicFailed"
    for _, e, f in events:
        if e in ("ProxyExecuted", "ItemFailed") and (e == "ItemFailed" or str(f.get("result", "Ok")) != "Ok"):
            name = str(f.get("error", "") or "")
            return "proxy_err", error_reason(name, proxied=True), name or FailReason.PROXY_ERROR.value
    return "ok", None, ""


def _stake_tao(events: Sequence[Event]) -> int | None:
    """Signed TAO of the real coldkey from the inner's stake events: StakeAdded -tao, StakeRemoved +tao."""
    for _, e, f in events:
        if e == "StakeAdded":
            return -int(str(f.get("tao", 0)))
        if e == "StakeRemoved":
            return int(str(f.get("tao", 0)))
    return None


def _stake_alpha(events: Sequence[Event]) -> int | None:
    """Alpha of the inner's StakeRemoved (None if absent)."""
    for _, e, f in events:
        if e == "StakeRemoved" and f.get("alpha") is not None:
            return int(str(f["alpha"]))
    return None


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


class LiveVenue:
    """ExecutionVenue for RunMode.LIVE / LIVE_DRY (one per book). Only `submit()` in an ARMED (or UNARMED risk-exit)
    state ever calls a SdkPort write; plan-only never does."""

    def __init__(self, book: BookId, *, sdk: SdkPort, reader: LiveReader, live: LiveCfg, risk: RiskCfg, exec_cfg: ExecCfg,
                 arming: Arming, spec_checks: RiskExitGuard | None = None, submissions: SubmissionStore | None = None,
                 kill_file: str | Path | None = None) -> None:
        self.book = book
        self.sdk = sdk
        self.reader = reader
        self.live = live
        self.risk = risk
        self.exec_cfg = exec_cfg
        self.arming = arming
        self.spec_checks = spec_checks
        self.submissions: SubmissionStore = submissions if submissions is not None else MemorySubmissions()
        self.kill_file = None if kill_file is None else Path(kill_file)
        self.real = live.real_coldkey_ss58
        self.caps = VenueCaps(kind="live_dry" if arming.plan_only else "live", shield_latency_blocks=SHIELD_LATENCY_BLOCKS,
                              era_blocks=8, supports_rotation=False, max_inflight_per_netuid=1,
                              n_delegates=len(live.delegate_wallets))
        self.delegates = DelegateLedger(live.delegate_wallets)
        # ---- folded from the journal (observe)
        self.block: int = -1
        self.health: HealthObs | None = None
        self.intents: dict[OrderKey, OrderIntent] = {}
        self.started: dict[OrderKey, SubmitStarted] = {}
        self.acks: dict[OrderKey, VenueAck] = {}
        self.ack_order: list[OrderKey] = []
        self.terminal: set[OrderKey] = set()
        self.unsettled: list[OrderKey] = []                        # carrier-absent misses awaiting CarrierFeeSettled
        self.used_nonce: dict[OrderKey, int] = {}
        self.positions: dict[tuple[SubnetKey, Hotkey], Decimal] = {}
        self.buy_fills: list[tuple[int, int]] = []                  # (block, tao) of buy fills (turnover)
        self.started_block: dict[OrderKey, int] = {}
        self.frozen: bool = False                                   # key alarm journaled, not yet QuarantineCleared
        self.pending_alarms: list[str] = []                         # derived from journaled facts; reconcile journals them
        self.last_error: str = ""

    # ================================================================== ExecutionVenue
    def mark_to(self, raw: ChainSnapshot) -> ChainSnapshot:
        return raw

    async def reserve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[str, int | None, Block | None]:
        """A free delegate and its pool-aware next index (read immediately before SubmitStarted is journaled)."""
        if self.arming.plan_only:
            return self.delegates.pick(int(now.block)), None, era_end_for(int(now.block), intent.shielded)
        fin, _ = await self.reader.finalized_head()
        skipped: list[str] = []
        for d in self.delegates.free(int(now.block)):
            n = await self.sdk.next_index(self.sdk.delegate_ss58(d))
            want = self.delegates.expected_nonce(d)
            if want is not None and n != want:
                log.warning("delegate %s next index %d != expected %d: skipped (reconciliation alarms)", d, n, want)
                skipped.append(f"{d}:{n}!={want}")
                continue
            return d, n, era_end_for(int(fin), intent.shielded)
        raise NoFreeDelegate(f"no usable delegate at {now.block} (nonce mismatches: {skipped})")

    async def submit(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent:
        key = (intent.order_id, intent.attempt)
        ack = self.acks.get(key)
        if ack is not None:
            return ack
        row = self.submissions.get(intent.order_id, intent.attempt)
        if row is not None and row.state != "plan_only":
            return SubmitUnknown(self.book, intent.order_id, intent.attempt, "already_sent")
        plan_only = self.arming.plan_only
        try:
            call, delegate, ps, fee = await self._prepare(intent, now)
        except VenueReject as r:
            return self._reject(intent, now, f"plan_only:{r.reason}" if plan_only else r.reason)
        except Exception as e:                   # reads only so far: nothing was sent, so this is a reject, not UNKNOWN
            reason = f"pre_submit_error:{type(e).__name__}: {e}"
            return self._reject(intent, now, f"plan_only:{reason}" if plan_only else reason)
        if plan_only:
            self.submissions.put(SubmissionRow(intent.order_id, intent.attempt, delegate, state="plan_only"))
            return self._reject(intent, now, f"plan_only:accepted:fee={fee}")
        return await self._send(intent, call, delegate, ps)

    async def advance(self, view: ChainSnapshot) -> JournalEvent | None:
        fin = int(view.block)
        try:
            for key in list(self.ack_order):
                if key in self.terminal:
                    continue
                ev = await self._outcome(key, fin, view)
                if ev is not None:
                    return ev
            for key in list(self.unsettled):
                ev = await self._settle(key, fin)
                if ev is not None:
                    return ev
        except Exception as e:                   # provider trouble: chain truth is final, ask again next tick
            self.last_error = f"advance: {type(e).__name__}: {e}"[:300]
            log.warning("book %s: %s", self.book, self.last_error)
        return None

    async def resolve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[Resolution, tuple[JournalEvent, ...]]:
        key = (intent.order_id, intent.attempt)
        if key in self.terminal:
            return Resolution.LANDED, ()
        if key in self.acks:
            return Resolution.PLACED, ()
        st = self.started.get(key)
        if st is None or st.nonce is None:
            return Resolution.NOT_PLACED, (self._failed(intent, int(now.block), FailReason.NOT_PLACED, 0,
                                                        detail="no_send" if st is not None else "no_submit_started"),)
        row = self.submissions.get(intent.order_id, intent.attempt)
        if row is not None and row.state == "plan_only":
            return Resolution.NOT_PLACED, (self._failed(intent, int(now.block), FailReason.NOT_PLACED, 0, detail="plan_only"),)
        n = self._nonce_of(key)
        era_end = int(st.era_end) if st.era_end is not None else int(now.block)
        fin, fin_hash = await self.reader.finalized_head()
        addr = self.sdk.delegate_ss58(st.delegate)
        ps = await self.sdk.post_state(fin_hash, self.real, ss58_encode(intent.hotkey), int(intent.key.netuid), addr)
        if ps.delegate_nonce <= n:
            if int(fin) > era_end + NOT_PLACED_GRACE_BLOCKS:
                return Resolution.NOT_PLACED, (self._failed(intent, int(fin), FailReason.NOT_PLACED, 0,
                                                            detail=f"nonce_unused:{n}"),)
            return Resolution.UNRESOLVABLE_YET, ()
        landing = await self._find_landing(intent, st.delegate, n, era_end, int(fin), row)
        if landing is None:
            if int(fin) > era_end:
                if intent.shielded:
                    return Resolution.LANDED, (self._failed(intent, era_end, FailReason.SHIELD_MISSED, 0, expired=True,
                                                            detail="carrier_absent"),)
                return Resolution.LANDED, (self._failed(intent, era_end, FailReason.ERA_EXPIRED, 0, expired=True,
                                                        detail="era_expired"),)
            return Resolution.UNRESOLVABLE_YET, ()
        ev = await self._evaluate(key, intent, st.delegate, landing, now)
        return Resolution.LANDED, (ev,)

    def observe(self, ev: JournalEvent) -> None:
        if isinstance(ev, SnapshotObserved):
            self.block = int(ev.block)
            self.health = ev.health
            return
        book = getattr(ev, "book", None)
        if isinstance(ev, OrderIntended):
            book = ev.intent.book
        elif isinstance(ev, FillReported):
            book = ev.fill.book
        if book != self.book:
            return
        if isinstance(ev, OrderIntended):
            self.intents[(ev.intent.order_id, ev.intent.attempt)] = ev.intent
        elif isinstance(ev, SubmitStarted):
            key = (ev.order_id, ev.attempt)
            intent = self.intents.get(key)
            self.started[key] = ev
            self.started_block[key] = self.block
            self.delegates.started(key, ev.delegate, ev.nonce, ev.era_end, intent.shielded if intent is not None else True)
        elif isinstance(ev, VenueAck):
            key = (ev.order_id, ev.attempt)
            if key not in self.acks:
                self.acks[key] = ev
                self.ack_order.append(key)
        elif isinstance(ev, SubmitUnknown):
            if ev.detail.startswith("nonce_mismatch:"):
                tail = ev.detail.split(":", 1)[1]
                if tail.isdigit():
                    key = (ev.order_id, ev.attempt)
                    self.used_nonce[key] = int(tail)
                    self.delegates.used_nonce(key, int(tail))
        elif isinstance(ev, FillReported):
            self._on_fill(ev.fill)
        elif isinstance(ev, OrderFailed):
            self._on_failed(ev)
        elif isinstance(ev, OrderCancelled):
            self.terminal.add((ev.order_id, ev.attempt))
        elif isinstance(ev, CarrierFeeSettled):
            key = (ev.order_id, ev.attempt)
            if key in self.unsettled:
                self.unsettled.remove(key)
            consumed = {"never_included": 0, "carrier_only": 1}.get(ev.outcome, 2)
            self.delegates.settled(key, consumed)
            if ev.outcome == "inner_included":
                self.pending_alarms.append(f"inner_included_after_miss:{ev.order_id}:{ev.attempt}")
        elif isinstance(ev, ReconAdjusted):
            for pkey, delta in ev.share_deltas:
                k = (pkey.subnet, pkey.hotkey)
                self.positions[k] = _EXACT.add(self.positions.get(k, _ZERO), delta)
                if self.positions[k] <= 0:
                    del self.positions[k]
            if ev.evidence.startswith(KEY_ALARM_PREFIX):
                self.frozen = True
                self.pending_alarms.clear()
        elif isinstance(ev, QuarantineCleared):
            self.frozen = False
        elif isinstance(ev, DeregSettled):
            for k in [k for k in self.positions if k[0] == ev.key]:
                del self.positions[k]
        elif isinstance(ev, CapitalChanged):
            pass

    # ================================================================== observe helpers
    def _on_fill(self, f: Fill) -> None:
        key = (f.order_id, f.attempt)
        self.terminal.add(key)
        intent = self.intents.get(key)
        self.delegates.finished(key, consumed=2 if (intent is None or intent.shielded) else 1, missed=False)
        pk = (f.key, f.hotkey)
        if f.kind is OrderKind.ADD_STAKE_LIMIT:
            self.positions[pk] = _EXACT.add(self.positions.get(pk, _ZERO), f.shares)
            self.buy_fills.append((int(f.block), int(f.tao)))
        else:
            left = _EXACT.subtract(self.positions.get(pk, _ZERO), f.shares)
            if left > 0:
                self.positions[pk] = left
            else:
                self.positions.pop(pk, None)
            if f.kind is OrderKind.MOVE_STAKE and f.dest_hotkey is not None and f.dest_shares is not None:
                dk = (f.dest_key or f.key, f.dest_hotkey)
                self.positions[dk] = _EXACT.add(self.positions.get(dk, _ZERO), f.dest_shares)

    def _on_failed(self, ev: OrderFailed) -> None:
        key = (ev.order_id, ev.attempt)
        self.terminal.add(key)
        intent = self.intents.get(key)
        shielded = intent.shielded if intent is not None else True
        if ev.reason in (FailReason.VENUE_REJECT, FailReason.NOT_PLACED, FailReason.ERA_EXPIRED):
            consumed: int | None = 0
        elif ev.reason is FailReason.SHIELD_MISSED:
            consumed = 1 if ev.tx_fee > 0 else None        # carrier in N+2 (inner dropped) / carrier absent: settle later
            if consumed is None and key not in self.unsettled and key in self.started:
                self.unsettled.append(key)
        else:
            consumed = 2 if shielded else 1                # inner (or plain) extrinsic included and failed
        self.delegates.finished(key, consumed=consumed, missed=ev.reason is FailReason.SHIELD_MISSED)

    # ================================================================== submit path
    def _reject(self, intent: OrderIntent, now: ChainSnapshot, reason: str) -> OrderFailed:
        log.info("book %s: %s %s rejected: %s", self.book, intent.kind.value, intent.order_id, reason)
        return OrderFailed(self.book, intent.order_id, intent.attempt, now.block, FailReason.VENUE_REJECT, Rao(0),
                           detail=reason[:200])

    def _failed(self, intent: OrderIntent, block: int, reason: FailReason, fee: int, *, expired: bool = False,
                detail: str = "") -> OrderFailed:
        return OrderFailed(self.book, intent.order_id, intent.attempt, Block(block), reason, Rao(fee), expired=expired,
                           detail=detail[:200])

    def _held_netuids(self) -> set[int]:
        return {int(k.netuid) for k, _ in self.positions}

    def _turnover(self, block: int, exclude: OrderKey) -> int:
        """Buy TAO in the window: filled buys plus OTHER in-flight buys. `exclude` is the order being checked: its own
        SubmitStarted is already folded, and the caller adds its amount, so counting it here would count it twice."""
        lo = block - TURNOVER_WINDOW_BLOCKS
        used = sum(t for b, t in self.buy_fills if b > lo)
        for key, st in self.started.items():
            intent = self.intents.get(key)
            if (key != exclude and intent is not None and intent.kind is OrderKind.ADD_STAKE_LIMIT and key not in self.terminal
                    and self.started_block.get(key, block) > lo and st.nonce is not None):
                used += int(intent.tao_in)
        return used

    async def _prepare(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[LiveCall, str, PostState, int]:
        """Every section 9.6 step-2 check; returns the exact LiveCall, the delegate, the head post-state and plan fee."""
        key = (intent.order_id, intent.attempt)
        st = self.started.get(key)
        if st is None:
            raise VenueReject("no_submit_started")
        kind = intent.kind
        is_buy = kind is OrderKind.ADD_STAKE_LIMIT
        is_sell = kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT)
        risk_exit = is_sell and intent.urgency >= Urgency.URGENT
        if kind is OrderKind.MOVE_STAKE_LIMIT:
            raise VenueReject("rotation_disabled")
        if any(k != key and k not in self.terminal and (o := self.intents.get(k)) is not None
               and o.key.netuid == intent.key.netuid and self.started[k].nonce is not None for k in self.started):
            raise VenueReject("inflight_netuid")                       # one in-flight order per netuid (9.8 #7)
        if self.kill_file is not None and self.kill_file.exists():
            raise VenueReject("kill_file")
        if self.frozen and not (self.risk.allow_emergency_exits_when_frozen and is_sell
                                and intent.urgency is Urgency.EMERGENCY and not intent.allow_partial):
            raise VenueReject("frozen_key_alarm")
        state = self.arming.state(now.glob.spec_version)
        if state is LiveState.UNARMED:
            if not (self.live.risk_exits_when_unarmed and risk_exit and intent.full_position):
                raise VenueReject("unarmed")
            if self.spec_checks is None or not await self.spec_checks.ok(now):
                raise VenueReject("unarmed:v2_v3_v6_not_passing")
        s = now.get(intent.key)
        if s is None:
            raise VenueReject("generation_gone")
        if now.glob.safe_mode_until is not None and now.glob.safe_mode_until >= now.block:
            raise VenueReject("safe_mode")
        if is_buy and not s.subtoken_enabled:
            raise VenueReject("subtoken_disabled")
        health = self.health
        if health is None:                       # no SnapshotObserved folded yet: lags unknown, fail closed
            raise VenueReject("no_health")
        if health.head_lag_blocks > MAX_HEAD_LAG_BLOCKS:
            raise VenueReject("head_lagging")
        if intent.shielded:
            if health.finality_lag_blocks > MAX_FINALITY_LAG_BLOCKS:
                raise VenueReject("shield_era_stale")
        elif not risk_exit:
            raise VenueReject("unshielded_not_risk_exit")
        fin, fin_hash = await self.reader.finalized_head()
        if int(fin) > int(intent.valid_until):
            raise VenueReject(f"stale_intent:finalized {int(fin)} > valid_until {int(intent.valid_until)}")
        delegate = st.delegate
        addr = self.sdk.delegate_ss58(delegate)
        hk = ss58_encode(intent.hotkey)
        n = int(intent.key.netuid)
        ps = await self.sdk.post_state(fin_hash, self.real, hk, n, addr)
        call = self._resolve_call(intent, s, hk, n, ps)
        out, spot_rt = await self.sdk.quote(call)
        if out <= 0 or spot_rt <= 0:
            raise VenueReject("quote_zero")
        if kind is not OrderKind.MOVE_STAKE:
            call = self._tighten(intent, call, s, spot_rt)
        if is_buy:
            self._buy_caps(intent, call, s, ps, now)
        max_fee = tao_to_rao(self.live.max_fee_tao)
        if ps.delegate_free_rao < max_fee:
            raise VenueReject(f"fee_float_exhausted:{ps.delegate_free_rao}")
        if ps.delegate_free_rao < self.risk.fee_float_exits_rao and not risk_exit:
            raise VenueReject(f"fee_float_low:{ps.delegate_free_rao}")
        violations, fee = await self.sdk.plan(call, delegate)
        if violations:
            raise VenueReject("plan:" + "|".join(violations))
        if fee > max_fee:
            raise VenueReject(f"plan:fee {fee} > max_fee")
        return call, delegate, ps, fee

    def _resolve_call(self, intent: OrderIntent, s: SubnetState, hk: str, n: int, ps: PostState) -> LiveCall:
        allowed = tuple(sorted(set(self.live.allowed_netuids)))
        sell_allowed = tuple(sorted(set(allowed) | self._held_netuids() | {n}))
        try:
            if intent.kind is OrderKind.ADD_STAKE_LIMIT:
                return LiveCall(OrderKind.ADD_STAKE_LIMIT, hk, n, int(intent.tao_in), int(intent.limit_price),
                                intent.allow_partial, None, self.live.max_order_tao, allowed or None)
            if intent.kind is OrderKind.MOVE_STAKE:
                if intent.dest_hotkey is None:
                    raise VenueReject("move_without_destination")
                amount = ps.alpha_value if intent.full_position else int(intent.alpha_in)
                self._check_position(amount, ps)
                return LiveCall(OrderKind.MOVE_STAKE, hk, n, amount, 0, False, ss58_encode(intent.dest_hotkey), None,
                                sell_allowed)
            # REMOVE_STAKE_LIMIT and REMOVE_STAKE_FULL_LIMIT (never sent live: mapped to 89 with the exact alpha)
            amount = ps.alpha_value if intent.full_position or intent.kind is OrderKind.REMOVE_STAKE_FULL_LIMIT \
                else int(intent.alpha_in)
            self._check_position(amount, ps)
            if intent.limit_price <= 0:
                raise VenueReject("sell_without_limit")
            return LiveCall(OrderKind.REMOVE_STAKE_LIMIT, hk, n, amount, int(intent.limit_price), intent.allow_partial,
                            None, None, sell_allowed)
        except LiveCallError as e:
            raise VenueReject(f"bad_call:{e}") from e

    @staticmethod
    def _check_position(amount: int, ps: PostState) -> None:
        if ps.alpha_value <= 0:
            raise VenueReject("no_position")
        if amount > ps.alpha_value:
            raise VenueReject(f"exceeds_position:{amount}>{ps.alpha_value}")      # long-only (section 9.8 #13)

    def _tighten(self, intent: OrderIntent, call: LiveCall, s: SubnetState, spot_rt: int) -> LiveCall:
        """Recompute the limit from the runtime head spot, never looser than the planner's, strictly on the right side."""
        dec_spot = int(s.pool.spot_rao())
        lim = int(call.limit_price_rao)
        if dec_spot <= 0:
            raise VenueReject("no_decision_spot")
        if call.is_buy:
            if spot_rt < dec_spot:
                lim = min(lim, _ceil_div(lim * spot_rt, dec_spot))
            if lim <= spot_rt:
                raise VenueReject(f"limit_crossed:buy limit {lim} <= spot {spot_rt}")
            if not intent.allow_partial:                 # fill-or-kill: size <= max_buy_to_limit at the relative limit
                try:
                    cap = int(max_buy_to_limit(s.pool, PriceRao(_ceil_div(lim * dec_spot, spot_rt))))
                except SwapError as e:
                    raise VenueReject(f"limit_crossed:{e.reason.value}") from e
                if call.amount > cap:
                    raise VenueReject(f"size_above_limit:{call.amount}>{cap}")
        else:
            if spot_rt > dec_spot:
                lim = max(lim, (lim * spot_rt) // dec_spot)
            if lim >= spot_rt:
                raise VenueReject(f"limit_crossed:sell limit {lim} >= spot {spot_rt}")
            if self.frozen and intent.urgency is Urgency.EMERGENCY:
                floor_px = spot_rt * (PPM - EMERGENCY_FROZEN_SLIP_PPM) // PPM
                lim = max(lim, floor_px)
            if not intent.allow_partial:                 # fill-or-kill: size <= max_sell_to_limit at the relative limit
                try:
                    cap = int(max_sell_to_limit(s.pool, PriceRao(max(lim * dec_spot // spot_rt, 1))))
                except (SwapError, ValueError) as e:
                    raise VenueReject("limit_crossed:sell") from e
                if call.amount > cap:
                    raise VenueReject(f"size_above_limit:{call.amount}>{cap}")
        return LiveCall(call.kind, call.hotkey_ss58, call.netuid, call.amount, lim, call.allow_partial,
                        call.dest_hotkey_ss58, call.max_spend_tao, call.allowed_netuids)

    def _buy_caps(self, intent: OrderIntent, call: LiveCall, s: SubnetState, ps: PostState, now: ChainSnapshot) -> None:
        """Caps bound BUYS ONLY (section 9.6 step 2, 9.8 #12)."""
        n = call.netuid
        if self.live.allowed_netuids and n not in self.live.allowed_netuids:
            raise VenueReject("cap:allowed_netuids")
        if call.amount > tao_to_rao(self.live.max_order_tao):
            raise VenueReject(f"cap:max_order_tao:{call.amount}")
        used = self._turnover(int(now.block), (intent.order_id, intent.attempt))
        if used + call.amount > tao_to_rao(self.live.max_daily_turnover_tao):
            raise VenueReject(f"cap:max_daily_turnover_tao:{used}+{call.amount}")
        try:
            q = quote_buy(s.pool, Rao(call.amount))
            after = liq_value(s.pool.shifted(q.d_tao, q.d_alpha), AlphaRao(ps.alpha_value + q.amount_out))
        except SwapError as e:
            raise VenueReject(f"buy_infeasible:{e.reason.value}") from e
        if after > tao_to_rao(self.live.max_position_tao):
            raise VenueReject(f"cap:max_position_tao:{after}")
        if ps.real_free_rao - call.amount < self.risk.min_free_real_rao:
            raise VenueReject(f"min_free_real:{ps.real_free_rao}")

    async def _send(self, intent: OrderIntent, call: LiveCall, delegate: str, ps: PostState) -> JournalEvent:
        key = (intent.order_id, intent.attempt)
        st = self.started[key]
        era_end = int(st.era_end) if st.era_end is not None else None
        row = SubmissionRow(intent.order_id, intent.attempt, delegate, nonce=st.nonce,
                            era_start=None if era_end is None else int(era_anchor(era_end, intent.shielded)),
                            era_end=era_end, delegate_free_before=ps.delegate_free_rao, state="sending")
        self.submissions.put(row)                                   # BEFORE the send (section 9.6 step 1)
        try:
            if intent.shielded:
                carrier, inner, head, used = await self.sdk.submit_shielded(call, delegate)
                expected = head + SHIELD_LATENCY_BLOCKS
            else:
                carrier, head, used = await self.sdk.submit_plain(call, delegate)
                inner, expected = "", head + 1
        except BaseException:
            self.submissions.put(replace(row, state="raised"))
            raise
        mismatch = st.nonce is not None and used != st.nonce
        self.submissions.put(replace(row, submit_head=head, expected_fill_block=expected, used_nonce=used,
                                     carrier_hash=carrier, inner_hash=inner, state="nonce_mismatch" if mismatch else "sent"))
        if mismatch:
            log.warning("book %s: %s used nonce %d, reserved %s", self.book, intent.order_id, used, st.nonce)
            return SubmitUnknown(self.book, intent.order_id, intent.attempt, f"nonce_mismatch:{used}")
        return VenueAck(self.book, intent.order_id, intent.attempt, Block(head), Block(expected), carrier, inner)

    # ================================================================== outcomes
    def _nonce_of(self, key: OrderKey) -> int:
        if key in self.used_nonce:
            return self.used_nonce[key]
        row = self.submissions.get(*key)
        if row is not None and row.used_nonce is not None:
            return int(row.used_nonce)
        st = self.started[key]
        return int(st.nonce) if st.nonce is not None else -1

    async def _hash(self, block: int) -> BlockHash:
        return await self.reader.block_hash(Block(block))

    async def _outcome(self, key: OrderKey, fin: int, view: ChainSnapshot) -> JournalEvent | None:
        ack = self.acks[key]
        intent = self.intents.get(key)
        st = self.started.get(key)
        if intent is None or st is None:
            return None
        if intent.shielded:
            n2 = int(ack.expected_fill_block)
            if fin < n2:
                return None
            exts = await self.sdk.block_extrinsics(await self._hash(n2))
            idx = next((i for i, h, _, _ in exts if h == ack.carrier_hash), None)
            if idx is None:
                return self._failed(intent, n2, FailReason.SHIELD_MISSED, 0, expired=True, detail="carrier_absent")
            return await self._evaluate(key, intent, st.delegate, _Landing(n2, idx, idx + 1, True), view)
        era_end = int(st.era_end) if st.era_end is not None else int(ack.submit_block) + 18
        hi = min(fin, era_end)
        for b in range(int(ack.submit_block) + 1, hi + 1):
            exts = await self.sdk.block_extrinsics(await self._hash(b))
            idx = next((i for i, h, _, _ in exts if h == ack.carrier_hash), None)
            if idx is not None:
                return await self._evaluate(key, intent, st.delegate, _Landing(b, idx, None, False), view)
        if fin > era_end:
            return self._failed(intent, era_end, FailReason.ERA_EXPIRED, 0, expired=True, detail="era_expired")
        return None

    async def _find_landing(self, intent: OrderIntent, delegate: str, n: int, era_end: int, fin: int,
                            row: SubmissionRow | None) -> _Landing | None:
        """Find the carrier (or plain extrinsic) by the sidecar hash or by (signer = delegate, nonce = n) in the era."""
        addr = self.sdk.delegate_ss58(delegate)
        lo = int(era_anchor(era_end, intent.shielded)) + 1
        if row is not None and row.submit_head is not None:
            lo = max(lo, int(row.submit_head) + 1)
        for b in range(lo, min(fin, era_end) + 1):
            exts = await self.sdk.block_extrinsics(await self._hash(b))
            for i, h, signer, nonce in exts:
                if (row is not None and row.carrier_hash and h == row.carrier_hash) or (signer == addr and nonce == n):
                    return _Landing(b, i, i + 1 if intent.shielded else None, intent.shielded)
        return None

    async def _evaluate(self, key: OrderKey, intent: OrderIntent, delegate: str, landing: _Landing,
                        view: ChainSnapshot) -> JournalEvent:
        """The step-4 outcome of an order whose carrier (or plain extrinsic) sits at `landing`."""
        bh = await self._hash(landing.block)
        outer = await self.sdk.extrinsic_events(bh, landing.index)
        outer_fee = _fees(outer)
        if landing.shielded:
            exts = await self.sdk.block_extrinsics(bh)
            addr = self.sdk.delegate_ss58(delegate)
            n = self._nonce_of(key)
            ack = self.acks.get(key)
            inner_ok = any(i == landing.inner_index and (
                (ack is not None and ack.inner_hash and h == ack.inner_hash) or (signer == addr and nonce == n + 1))
                for i, h, signer, nonce in exts)
            if not inner_ok or landing.inner_index is None:
                return self._failed(intent, landing.block, FailReason.SHIELD_MISSED, outer_fee, expired=True,
                                    detail="inner_absent")
            inner = await self.sdk.extrinsic_events(bh, landing.inner_index)
        else:
            inner, outer_fee = outer, 0
        fee = outer_fee + _fees(inner)
        status, reason, name = _dispatch(inner)
        if status != "ok" or reason is not None:
            return self._failed(intent, landing.block, reason or FailReason.OTHER, fee, detail=name)
        return await self._fill(key, intent, delegate, landing, inner, fee, view)

    async def _fill(self, key: OrderKey, intent: OrderIntent, delegate: str, landing: _Landing, inner: Sequence[Event],
                    fee: int, view: ChainSnapshot) -> JournalEvent:
        b = landing.block
        addr = self.sdk.delegate_ss58(delegate)
        hk = ss58_encode(intent.hotkey)
        n = int(intent.key.netuid)
        h_pre, h_post = await self._hash(b - 1), await self._hash(b)
        pre = await self.sdk.post_state(h_pre, self.real, hk, n, addr)
        post = await self.sdk.post_state(h_post, self.real, hk, n, addr)
        d_shares = _EXACT.subtract(post.shares, pre.shares)
        is_buy = intent.kind is OrderKind.ADD_STAKE_LIMIT
        if (is_buy and d_shares <= 0) or (not is_buy and d_shares >= 0):
            return self._failed(intent, b, FailReason.OTHER, fee, detail="success_without_share_delta")
        shares = d_shares if is_buy else _EXACT.minus(d_shares)
        if post.hk_total_shares > 0:
            alpha = floor_int(DEC.divide(DEC.multiply(shares, Decimal(post.hk_total_alpha)), post.hk_total_shares))
        elif not is_buy and pre.hk_total_shares > 0:
            # Our exit took the LAST shares of the hotkey: the post index is undefined (0/0). The chain executed it, so
            # this is a fill: the alpha is the inner's StakeRemoved amount, else the pre-block index (excludes a drain
            # in N + 2, which YieldAccrued books).
            ev_alpha = _stake_alpha(inner)
            alpha = ev_alpha if ev_alpha is not None else floor_int(
                DEC.divide(DEC.multiply(shares, Decimal(pre.hk_total_alpha)), pre.hk_total_shares))
        else:
            return self._failed(intent, b, FailReason.OTHER, fee, detail="success_without_hotkey_index")
        tao = 0
        if intent.kind is not OrderKind.MOVE_STAKE:
            delta_free = post.real_free_rao - pre.real_free_rao          # buy: negative, sell: positive
            own = _stake_tao(inner)
            own_index = landing.inner_index if landing.shielded and landing.inner_index is not None else landing.index
            if own is not None and await self._other_own_stakes(b, own_index):
                tao = abs(own)                                            # apportioned; reconcile books the residual
            else:
                tao = max(0, -delta_free if is_buy else delta_free)
        s = view.get(intent.key)
        fee_rate = s.pool.fee_rate if s is not None else 0
        prices = await self.reader.prices_all(h_pre)
        spot_before = int(prices.get(n, 0))
        dest_shares: Decimal | None = None
        if intent.kind is OrderKind.MOVE_STAKE:
            assert intent.dest_hotkey is not None
            dhk = ss58_encode(intent.dest_hotkey)
            dpre = await self.sdk.post_state(h_pre, self.real, dhk, n, addr)
            dpost = await self.sdk.post_state(h_post, self.real, dhk, n, addr)
            dest_shares = _EXACT.subtract(dpost.shares, dpre.shares)
            if dest_shares <= 0:
                return self._failed(intent, b, FailReason.OTHER, fee, detail="move_without_dest_delta")
            sw, d_tao, d_alpha, shortfall, complete = 0, 0, 0, 0, intent.full_position or alpha + 2 >= int(intent.alpha_in)
        elif is_buy:
            sw = swap_fee(tao, fee_rate)
            d_tao, d_alpha = tao - sw, -alpha
            shortfall = _ceil_div((tao * RAO_PER_TAO - alpha * spot_before) * PPM, tao * RAO_PER_TAO) if tao > 0 else 0
            complete = not intent.allow_partial or tao >= int(intent.tao_in)
        else:
            sw = swap_fee(alpha, fee_rate)
            d_tao, d_alpha = -tao, alpha
            gross = alpha * spot_before
            shortfall = _ceil_div((gross - tao * RAO_PER_TAO) * PPM, gross) if gross > 0 else 0
            complete = (not intent.allow_partial) or intent.full_position or alpha + 2 >= int(intent.alpha_in)
            if intent.full_position and post.shares > 0:
                complete = False                     # a drain remainder stays open; the planner re-exits it (3.9)
        fill = Fill(fill_id=f"{intent.order_id}:{intent.attempt}:0", order_id=intent.order_id, attempt=intent.attempt,
                    book=self.book, block=Block(b), kind=intent.kind, key=intent.key, hotkey=intent.hotkey, tao=Rao(tao),
                    alpha=AlphaRao(alpha), shares=shares, swap_fee=sw, author_fee_tao=Rao(0), tx_fee=Rao(fee),
                    d_pool_tao=d_tao, d_pool_alpha=d_alpha, spot_before=PriceRao(spot_before), shortfall_ppm=Ppm(shortfall),
                    complete=complete, exact_block=True,
                    dest_key=intent.key if intent.kind is OrderKind.MOVE_STAKE else None,
                    dest_hotkey=intent.dest_hotkey if intent.kind is OrderKind.MOVE_STAKE else None,
                    dest_shares=dest_shares)
        return FillReported(fill)

    async def _other_own_stakes(self, block: int, own_index: int) -> bool:
        """Whether another extrinsic of `block` signed by one of our delegates swapped TAO for the real coldkey
        (StakeAdded/StakeRemoved, dispatched ok): then the block's free-TAO change is shared and is apportioned by each
        inner's stake-event TAO (section 9.6 step 4). Found from the block itself, so shielded inners, unshielded
        risk-exit fallbacks and orders resolved after SubmitUnknown all count."""
        bh = await self._hash(block)
        addrs = {self.sdk.delegate_ss58(d) for d in self.live.delegate_wallets}
        for i, _, signer, _ in await self.sdk.block_extrinsics(bh):
            if i == own_index or signer not in addrs:
                continue
            evs = await self.sdk.extrinsic_events(bh, i)
            if _stake_tao(evs) is not None and _dispatch(evs)[0] == "ok":
                return True
        return False

    # ================================================================== carrier-fee settlement
    async def _settle(self, key: OrderKey, fin: int) -> JournalEvent | None:
        st = self.started.get(key)
        intent = self.intents.get(key)
        if st is None or intent is None or st.era_end is None:
            return None
        era_end = int(st.era_end)
        if fin <= era_end:
            return None
        n = self._nonce_of(key)
        addr = self.sdk.delegate_ss58(st.delegate)
        hk = ss58_encode(intent.hotkey)
        net = int(intent.key.netuid)
        after = await self.sdk.post_state(await self._hash(era_end + 1), self.real, hk, net, addr)
        row = self.submissions.get(*key)
        settle_block = Block(era_end + 1)
        if after.delegate_nonce <= n:
            return CarrierFeeSettled(self.book, intent.order_id, intent.attempt, settle_block, Rao(0), "never_included")
        before = row.delegate_free_before if row is not None and row.delegate_free_before is not None else None
        lo = int(era_anchor(era_end, intent.shielded)) + 1
        if row is not None and row.submit_head is not None:
            lo = max(lo, int(row.submit_head) + 1)
        if before is None:
            before = (await self.sdk.post_state(await self._hash(lo - 1), self.real, hk, net, addr)).delegate_free_rao
        balance_fee = max(0, int(before) - after.delegate_free_rao)
        if after.delegate_nonce == n + 1:
            fee = await self._fee_of(addr, n, lo, era_end)
            return CarrierFeeSettled(self.book, intent.order_id, intent.attempt, settle_block,
                                     Rao(balance_fee if fee is None else fee), "carrier_only")
        inner_fee = await self._fee_of(addr, n + 1, lo, era_end)
        carrier_fee = await self._fee_of(addr, n, lo, era_end)
        if inner_fee is None:
            log.error("book %s: delegate %s nonce advanced to %d after a declared miss with no own extrinsic at n+1: "
                      "foreign use of the delegate key", self.book, st.delegate, after.delegate_nonce)
            fee = balance_fee
        else:
            fee = inner_fee + (carrier_fee or 0)
        return CarrierFeeSettled(self.book, intent.order_id, intent.attempt, settle_block, Rao(fee), "inner_included")

    async def _fee_of(self, addr: str, nonce: int, lo: int, hi: int) -> int | None:
        """The fee of the delegate's extrinsic at `nonce` within blocks lo..hi (None if not found)."""
        for b in range(lo, hi + 1):
            bh = await self._hash(b)
            for i, _, signer, nn in await self.sdk.block_extrinsics(bh):
                if signer == addr and nn == nonce:
                    return _fees(await self.sdk.extrinsic_events(bh, i))
        return None

    # ================================================================== introspection (reconcile, tests)
    def own_hashes(self) -> set[str]:
        out: set[str] = set()
        for a in self.acks.values():
            out.update(h for h in (a.carrier_hash, a.inner_hash) if h)
        return out

    def own_nonces(self) -> dict[str, set[int]]:
        """delegate id -> every carrier/inner nonce of our journaled submissions."""
        out: dict[str, set[int]] = {}
        for key, st in self.started.items():
            n = self.used_nonce.get(key, st.nonce)
            if n is not None:
                out.setdefault(st.delegate, set()).update({n, n + 1})
        return out

    def in_flight(self) -> int:
        return sum(1 for k in self.started if k not in self.terminal)


if TYPE_CHECKING:
    def _conforms(v: LiveVenue) -> ExecutionVenue:
        return v
