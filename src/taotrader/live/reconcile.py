"""taotrader/live/reconcile.py - reconciliation, observed dissolution payouts and key alarms (WP11; DESIGN.md 9.7).

`LiveReconciler` is the Runner's `reconcile` hook (engine.runner.Reconciler: `async (BookRuntime, ChainSnapshot) ->
Sequence[JournalEvent]`), called after every tick's drain and outbox and during recovery. Reads use SdkPort only.

- Cadence: every 25 blocks and after every terminal order outcome. Expected values come from the book's journal-folded
  state: each (real coldkey, hotkey, generation) share count, `cash` (= free TAO of the dedicated real coldkey) and
  `fee_float` (= free TAO of the delegates). Share counts never change with yield (yield raises the hotkey index), so a
  share difference is never yield. Tolerance: 2 rao or 1e-6 relative of the position value (TAO: of the balance).
  A key with an order in flight is skipped (its fill is not final yet), and so are the balances while any order is in
  flight, a carrier-absent miss is unsettled, or a held generation is DISSOLVING.
- Mismatch -> ONE ReconAdjusted per call (chain wins; the reducer halts entries until QuarantineCleared, an operator
  command).
- Key alarms (-> FROZEN): a ReconAdjusted whose evidence starts with "key_alarm:" (zero deltas if nothing else is off);
  LiveVenue folds it into its frozen state (nothing but EMERGENCY fill-or-kill sells with
  allow_emergency_exits_when_frozen) and the reducer halts entries. Sources:
  * unexplained negative share delta; a positive delta worth > 3x the expected epoch yield; a position on a key the
    ledger does not hold (post_state of every known pair, account events);
  * delegate nonce != the nonce the journal implies (post_state.delegate_nonce vs DelegateLedger.expected_nonce);
  * Proxy.Proxies(real) or Proxy.Announcements changed (proxies, proxy_announcements) against the set seen at the
    first call (the one start preflight validated; never re-based, so a persisting change re-alarms after a clear);
  * a coldkey swap scheduled (coldkey_swap_scheduled);
  * foreign StakeMoved/StakeTransferred/StakeSwapped/StakeAdded/StakeRemoved on the coldkey, and any
    TransactionFeePaidWithAlpha (account_events on EVERY finalized block; own events are matched by our journaled
    carrier/inner hashes, or by (delegate signer, own nonce));
  * an inner included after a declared miss (LiveVenue CarrierFeeSettled "inner_included");
  * RealPaysFee turned on, a lock on a held netuid, or any other preflight failure in the periodic preflight
    (every 7,200 blocks); the transient health rows (head/finality lag, SafeMode) are mode inputs, not key alarms;
  * reconciliation unavailable for 50 consecutive ticks (fail closed).
  Each alarm also calls on_alert("key_alarm", "... revoke the Staking proxy from the coldkey ...").
- Dissolution (section 9.7): the Engine never settles a held dissolution in live. When a generation becomes DISSOLVING,
  the real coldkey's free balance is baselined; the first unexplained free-TAO increase (chain delta minus ledger cash
  delta) is journaled as DeregSettled(model="observed") (idempotency key dereg:{book}:{netuid}:{reg_at}: exactly once).
  With no payout within 7,200 blocks it settles at 0 and the cash reconciliation books any late credit.
- The arming monitor (gate.ArmingMonitor) runs every call (T - 2 h and spec-change alerts while UNARMED is not covered).
- Live-dry (plan-only): the key-alarm reads run, but position/balance reconciliation and observed dissolution payouts
  do not (the journal holds no real trades, and the Engine settles dissolutions itself in RunMode.LIVE_DRY).

Exceptions never escape (they would poison the Runner): they are alerted and counted.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Final

from ..core.config import LiveCfg, RiskCfg
from ..core.events import DeregSettled, JournalEvent, ReconAdjusted
from ..core.fixed import DEC, floor_int
from ..core.orders import TERMINAL
from ..core.portfolio import pos_account
from ..core.state import ChainSnapshot
from ..core.units import AlphaRao, Hotkey, PositionKey, Rao, SubnetKey
from ..engine.recovery import BookRuntime
from .gate import ArmingMonitor
from .preflight import PREFLIGHT_EVERY_BLOCKS, run_preflight
from .sdk_port import LiveReader, SdkPort, ss58_encode
from .venue import KEY_ALARM_PREFIX, LiveVenue

__all__ = ["DISSOLVE_WINDOW_BLOCKS", "RECON_EVERY_BLOCKS", "LiveReconciler"]

log = logging.getLogger("taotrader.live.reconcile")

RECON_EVERY_BLOCKS: Final[int] = 25
TOL_RAO: Final[int] = 2
TOL_PPB: Final[int] = 1_000                       # 1e-6 relative
YIELD_ALARM_MULT: Final[int] = 3
DISSOLVE_WINDOW_BLOCKS: Final[int] = 7_200
MAX_FAILED_CALLS: Final[int] = 50
RECENT_INTENT_BLOCKS: Final[int] = 7_200          # traded pairs checked for unknown positions
FOREIGN_STAKE_EVENTS: Final[frozenset[str]] = frozenset(
    {"StakeMoved", "StakeTransferred", "StakeSwapped", "StakeAdded", "StakeRemoved"})
ALWAYS_ALARM_EVENTS: Final[frozenset[str]] = frozenset(
    {"TransactionFeePaidWithAlpha", "ColdkeySwapScheduled", "ColdkeySwapAnnounced"})
PERIODIC_EXEMPT: Final[frozenset[str]] = frozenset({"head_lag", "finality_lag", "safe_mode", "reconciliation"})
REVOKE_HINT: Final[str] = "revoke the Staking proxy from the coldkey (btcli proxy remove), then reconcile"


def _tol(value: int) -> int:
    return max(TOL_RAO, abs(value) * TOL_PPB // 1_000_000_000)


class LiveReconciler:
    """The live reconcile hook. One instance serves every live book of a Runner."""

    def __init__(self, sdk: SdkPort, reader: LiveReader, *, live: LiveCfg, risk: RiskCfg,
                 monitor: ArmingMonitor | None = None, on_alert: Callable[[str, str], None] | None = None,
                 every_blocks: int = RECON_EVERY_BLOCKS, preflight_every: int = PREFLIGHT_EVERY_BLOCKS) -> None:
        self.sdk = sdk
        self.reader = reader
        self.live = live
        self.risk = risk
        self.monitor = monitor
        self.on_alert = on_alert
        self.every_blocks = every_blocks
        self.preflight_every = preflight_every
        self.real = live.real_coldkey_ss58
        self.failed_calls = 0
        self._last_recon: dict[str, int] = {}
        self._last_terminal: dict[str, int] = {}
        self._last_preflight: dict[str, int] = {}
        self._proxies: tuple[tuple[str, str, int], ...] | None = None
        self._announcements: tuple[tuple[str, str, int], ...] | None = None
        self._raised: dict[str, set[str]] = {}
        self._dissolving: dict[tuple[str, SubnetKey], tuple[int, int, int]] = {}   # (start block, free base, cash base)

    async def __call__(self, rt: BookRuntime, snap: ChainSnapshot) -> Sequence[JournalEvent]:
        venue = rt.venue
        if not isinstance(venue, LiveVenue):
            return ()
        book = str(rt.book)
        try:
            out = await self._run(rt, venue, snap)
            self.failed_calls = 0
            return out
        except Exception as e:                      # never poison the Runner; fail closed after a long outage
            self.failed_calls += 1
            msg = f"book {book}: reconciliation failed ({type(e).__name__}: {e})"[:300]
            log.warning("%s", msg)
            self._alert("reconcile", msg)
            if self.failed_calls >= MAX_FAILED_CALLS and not venue.frozen:
                return (self._alarm_event(rt, venue, snap, [], 0, 0, ["reconcile_unavailable"]),)
            return ()

    # ------------------------------------------------------------------ one call
    async def _run(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot) -> list[JournalEvent]:
        book = str(rt.book)
        b = int(snap.block)
        if self._raised.get(book) and not venue.frozen:   # journaled alarms, venue no longer frozen: the operator
            self._raised[book] = set()                    # cleared the quarantine, so persisting conditions re-alarm
        held = [p.key for p in rt.state.portfolio.positions]
        if self.monitor is not None:
            self.monitor.check(snap, held)
        alarms: list[str] = list(venue.pending_alarms)
        alarms += await self._account_events(venue, snap)
        out: list[JournalEvent] = []
        terminal = len(venue.terminal)
        due = (b - self._last_recon.get(book, -10**9) >= self.every_blocks or terminal != self._last_terminal.get(book))
        share_deltas: list[tuple[PositionKey, Decimal]] = []
        cash_delta = fee_delta = 0
        if due:
            self._last_recon[book] = b
            self._last_terminal[book] = terminal
            alarms += await self._key_reads(venue)
            alarms += await self._nonces(venue, snap)
            if not venue.arming.plan_only:          # live-dry: the journal never holds real trades (and the Engine
                share_deltas, pos_alarms = await self._positions(rt, venue, snap)   # settles dissolutions itself)
                alarms += pos_alarms
                out += await self._dissolutions(rt, venue, snap)
                if not out and venue.in_flight() == 0 and not venue.unsettled and not rt.state.dissolving:
                    cash_delta, fee_delta = await self._balances(rt, venue, snap)
        if b - self._last_preflight.get(book, b) >= self.preflight_every:
            self._last_preflight[book] = b
            alarms += await self._periodic_preflight(rt, venue, snap)
        self._last_preflight.setdefault(book, b)
        fresh = [a for a in dict.fromkeys(alarms) if a not in self._raised.setdefault(book, set())]
        if fresh or share_deltas or cash_delta or fee_delta:
            out.append(self._alarm_event(rt, venue, snap, share_deltas, cash_delta, fee_delta, fresh))
        return out

    def _alarm_event(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot,
                     share_deltas: Sequence[tuple[PositionKey, Decimal]], cash_delta: int, fee_delta: int,
                     alarms: Sequence[str]) -> ReconAdjusted:
        book = str(rt.book)
        if alarms:
            self._raised.setdefault(book, set()).update(alarms)
            msg = f"book {book} at {snap.block}: KEY ALARM {', '.join(alarms)}: live FROZEN; {REVOKE_HINT}"
            log.error("%s", msg)
            self._alert("key_alarm", msg)
            evidence = KEY_ALARM_PREFIX + ";".join(alarms)
        else:
            evidence = "recon:" + ";".join(
                [f"cash{cash_delta:+d}" if cash_delta else "", f"fee_float{fee_delta:+d}" if fee_delta else "",
                 *(f"shares[{int(k.subnet.netuid)}:{k.hotkey[:10]}]{d}" for k, d in share_deltas)]).strip(";")
            self._alert("recon", f"book {book} at {snap.block}: {evidence}; entries halted until QuarantineCleared")
        return ReconAdjusted(rt.book, snap.block, cash_delta, fee_delta, tuple(share_deltas), evidence[:500])

    def _alert(self, kind: str, msg: str) -> None:
        if self.on_alert is not None:
            self.on_alert(kind, msg)

    # ------------------------------------------------------------------ every finalized block
    async def _account_events(self, venue: LiveVenue, snap: ChainSnapshot) -> list[str]:
        events = await self.sdk.account_events(snap.block_hash, self.real)
        if not events:
            return []
        own_hashes = venue.own_hashes()
        own_nonces = {self.sdk.delegate_ss58(d): ns for d, ns in venue.own_nonces().items()}
        exts: dict[str, tuple[str | None, int | None]] | None = None
        out: list[str] = []
        for _pallet, name, fields in events:
            if name in ALWAYS_ALARM_EVENTS:
                out.append(f"{name}@{int(snap.block)}")
                continue
            if name not in FOREIGN_STAKE_EVENTS:
                continue
            h = str(fields.get("extrinsic_hash", "") or "")
            if h and h in own_hashes:
                continue
            if exts is None:
                exts = {eh: (signer, nonce) for _, eh, signer, nonce in await self.sdk.block_extrinsics(snap.block_hash)}
            signer, nonce = exts.get(h, (None, None))
            if signer is not None and nonce is not None and nonce in own_nonces.get(signer, set()):
                continue
            out.append(f"foreign_{name}@{int(snap.block)}")
        return out

    # ------------------------------------------------------------------ cadence reads
    async def _key_reads(self, venue: LiveVenue) -> list[str]:
        out: list[str] = []
        proxies = tuple(sorted(await self.sdk.proxies(self.real)))
        if self._proxies is None:                    # baseline = the set preflight validated at start; never moved,
            self._proxies = proxies                  # so a still-changed set re-alarms after an operator clear
        elif proxies != self._proxies:
            out.append("proxies_changed")
        anns = tuple(sorted(await self.sdk.proxy_announcements(self.real)))
        if self._announcements is None:
            self._announcements = anns
            if anns:
                out.append("proxy_announcements_present")
        elif anns != self._announcements:
            out.append("proxy_announcements_changed")
        if await self.sdk.coldkey_swap_scheduled(self.real):
            out.append("coldkey_swap_scheduled")
        return out

    def _delegate_addr(self) -> str:
        return self.sdk.delegate_ss58(self.live.delegate_wallets[0])

    async def _positions(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot
                         ) -> tuple[list[tuple[PositionKey, Decimal]], list[str]]:
        busy = {(o.intent.key, o.intent.hotkey) for o in rt.state.orders if o.state not in TERMINAL}
        busy |= {(o.intent.key, o.intent.dest_hotkey) for o in rt.state.orders
                 if o.state not in TERMINAL and o.intent.dest_hotkey is not None}
        ledger: dict[tuple[SubnetKey, Hotkey], Decimal] = {(p.key, p.hotkey): p.shares for p in rt.state.portfolio.positions}
        recent = [i for i in venue.intents.values() if int(i.created_block) > int(snap.block) - RECENT_INTENT_BLOCKS]
        pairs = sorted(set(ledger) | set(venue.positions) | {(i.key, i.hotkey) for i in recent}
                       | {(i.key, i.dest_hotkey) for i in recent if i.dest_hotkey is not None})
        addr = self._delegate_addr()
        deltas: list[tuple[PositionKey, Decimal]] = []
        alarms: list[str] = []
        for key, hk in pairs:
            s = snap.get(key)
            if s is None or (key, hk) in busy or key in rt.state.dissolving:
                continue
            ps = await self.sdk.post_state(snap.block_hash, self.real, ss58_encode(hk), int(key.netuid), addr)
            want = ledger.get((key, hk), Decimal(0))
            diff = DEC.subtract(ps.shares, want)
            if diff == 0:
                continue
            index = DEC.divide(Decimal(ps.hk_total_alpha), ps.hk_total_shares) if ps.hk_total_shares > 0 else Decimal(1)
            value = floor_int(DEC.multiply(abs(diff), index))
            base = floor_int(DEC.multiply(want, index))
            if value <= _tol(base):
                continue
            deltas.append((PositionKey(key, hk), diff))
            tag = f"SN{int(key.netuid)}:{hk[:10]}"
            if want == 0:
                alarms.append(f"unknown_position:{tag}")
            elif diff < 0:
                alarms.append(f"unexplained_negative_delta:{tag}")
            else:
                idx = s.hotkey(hk)
                epoch_yield = 0
                if idx is not None and idx.total_shares > 0:
                    epoch_yield = floor_int(DEC.divide(DEC.multiply(Decimal(idx.last_dividend), want), idx.total_shares))
                if value > YIELD_ALARM_MULT * epoch_yield:
                    alarms.append(f"delta_above_yield:{tag}")
        return deltas, alarms

    async def _nonces(self, venue: LiveVenue, snap: ChainSnapshot) -> list[str]:
        out: list[str] = []
        for d in self.live.delegate_wallets:
            want = venue.delegates.expected_nonce(d)
            if want is None:
                continue
            _, nonce = await self.sdk.account(snap.block_hash, self.sdk.delegate_ss58(d))
            if nonce != want:
                out.append(f"delegate_nonce:{d}:{nonce}!={want}")
        return out

    async def _balances(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot) -> tuple[int, int]:
        real_free, _ = await self.sdk.account(snap.block_hash, self.real)
        cash = int(rt.state.portfolio.cash)
        cash_delta = real_free - cash
        fee_float = 0
        for d in self.live.delegate_wallets:
            fee_float += (await self.sdk.account(snap.block_hash, self.sdk.delegate_ss58(d)))[0]
        fee_delta = fee_float - int(rt.state.portfolio.fee_float)
        return (cash_delta if abs(cash_delta) > _tol(cash) else 0,
                fee_delta if abs(fee_delta) > _tol(int(rt.state.portfolio.fee_float)) else 0)

    async def _dissolutions(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot) -> list[JournalEvent]:
        book = str(rt.book)
        out: list[JournalEvent] = []
        if not rt.state.dissolving:
            return out
        real_free, _ = await self.sdk.account(snap.block_hash, self.real)
        cash = int(rt.state.portfolio.cash)
        for key in rt.state.dissolving:
            k = (book, key)
            if k not in self._dissolving:
                self._dissolving[k] = (int(snap.block), real_free, cash)
                continue
            start, free0, cash0 = self._dissolving[k]
            payout = (real_free - free0) - (cash - cash0)
            if payout > TOL_RAO or int(snap.block) - start >= DISSOLVE_WINDOW_BLOCKS:
                hk = next((p.hotkey for p in rt.state.portfolio.positions if p.key == key), None)
                if hk is None:
                    continue
                accounts = {pos_account(key, p.hotkey) for p in rt.state.portfolio.positions if p.key == key}
                alpha = sum(v for a, _u, v in rt.state.ledger if a in accounts)
                out.append(DeregSettled(rt.book, key, hk, snap.block, AlphaRao(max(int(alpha), 0)), Rao(max(payout, 0)),
                                        "observed"))
                del self._dissolving[k]
        return out

    async def _periodic_preflight(self, rt: BookRuntime, venue: LiveVenue, snap: ChainSnapshot) -> list[str]:
        health = venue.health
        if health is None:
            return []
        held = [(p.key, p.hotkey) for p in rt.state.portfolio.positions]
        report = await run_preflight(self.sdk, live=self.live, risk=self.risk, snap=snap, health=health, held=held,
                                     recon_clean=True)
        return [f"preflight:{c.name}" for c in report.checks if not c.ok and c.name not in PERIODIC_EXEMPT]

