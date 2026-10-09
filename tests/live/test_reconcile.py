"""LiveReconciler (DESIGN.md 9.7, 10.5): share-delta reconciliation against the real WP7 reducer state folded from the
same journal LiveVenue observed, ReconAdjusted, key-alarm sources, observed dissolution payouts, fail-closed errors."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from types import ModuleType
from typing import Any, cast

from taotrader.core.events import (
    ChainEvent,
    ChainEventKind,
    ChainEventObserved,
    DeregSettled,
    FillReported,
    JournalEvent,
    OrderFailed,
    QuarantineCleared,
    ReconAdjusted,
    VenueAck,
)
from taotrader.core.orders import OrderKind
from taotrader.core.units import Block, BookId, RunMode
from taotrader.engine.recovery import BookRuntime
from taotrader.engine.reducer import BookSpec, EngineState, fold_batch, initial_state
from taotrader.live.reconcile import LiveReconciler
from taotrader.live.venue import LiveVenue


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@dataclass
class RT:
    venue: LiveVenue
    state: EngineState
    alerts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def book(self) -> BookId:
        return self.venue.book


def runtime(lk: ModuleType, k: Any) -> RT:
    spec = BookSpec.from_cfg(lk.book_cfg(), RunMode.LIVE, ("ops0", "ops1", "ops2"))
    return RT(k.venue, fold_batch(initial_state(spec), k.journal))


def recon(lk: ModuleType, k: Any, **kw: Any) -> tuple[LiveReconciler, list[tuple[str, str]]]:
    alerts: list[tuple[str, str]] = []
    r = LiveReconciler(k.sdk, k.reader, live=k.live, risk=k.risk, on_alert=lambda a, b: alerts.append((a, b)), **kw)
    return r, alerts


def call(r: LiveReconciler, rt: RT, k: Any, block: int) -> list[Any]:
    out: list[JournalEvent] = list(run(r(cast(BookRuntime, rt), k.snap(block))))
    for ev in out:
        k.feed(ev)
        rt.state = fold_batch(rt.state, [ev])
    return out


def filled_buy(lk: ModuleType, k: Any) -> tuple[Any, VenueAck]:
    it = lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=lk.TAO // 2)
    ack = run(k.place(it))
    k.land_buy(ack, it)
    k.set_head(lk.B0 + 2)
    evs = run(k.drain(lk.B0 + 2))
    assert isinstance(evs[0], FillReported)
    return it, ack


# ------------------------------------------------------------------------------------------------- clean books
def test_clean_after_a_fill_writes_nothing(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    assert rt.state.orphans == 0 and not rt.state.breaches
    r, alerts = recon(lk, k)
    assert call(r, rt, k, lk.B0 + 2) == []
    assert call(r, rt, k, lk.B0 + 30) == [] and alerts == []


def test_skips_keys_in_flight_and_balances_while_orders_are_open(lk: ModuleType) -> None:
    k = lk.Kit()
    it = lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=lk.TAO // 2)
    ack = run(k.place(it))
    k.land_buy(ack, it)                                                       # landed on chain, not drained yet
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    assert call(r, rt, k, lk.B0 + 2) == []                                     # no false ReconAdjusted mid-flight


# ------------------------------------------------------------------------------------------------- mismatches
def test_unexplained_negative_share_delta_is_adjusted_and_a_key_alarm(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    r, alerts = recon(lk, k)
    call(r, rt, k, lk.B0 + 2)
    mine = k.sdk.shares_at(lk.B0 + 2, lk.SA, lk.SN)
    k.sdk.set_stake(lk.B0 + 10, lk.SA, lk.SN, shares=mine / 2)               # someone sold half of our position
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert isinstance(ev, ReconAdjusted) and ev.evidence.startswith("key_alarm:unexplained_negative_delta")
    (pk, delta), = ev.share_deltas
    assert pk.subnet == lk.KEY and delta < 0
    assert rt.state.recon_halt and k.venue.frozen
    assert any(kind == "key_alarm" and "revoke the Staking proxy" in m for kind, m in alerts)
    pos = rt.state.portfolio.positions[0]
    assert pos.shares == k.sdk.shares_at(lk.B0 + 30, lk.SA, lk.SN)             # chain truth wins


def test_small_positive_delta_is_adjusted_without_alarm_and_large_one_alarms(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    call(r, rt, k, lk.B0 + 2)
    mine = k.sdk.shares_at(lk.B0 + 2, lk.SA, lk.SN)
    assert call(r, rt, k, lk.B0 + 20) == []
    k.sdk.set_stake(lk.B0 + 5, lk.SA, lk.SN, shares=mine + Decimal(1_000))      # within 1e-6: nothing
    assert call(r, rt, k, lk.B0 + 25) == []
    k.sdk.set_stake(lk.B0 + 10, lk.SA, lk.SN, shares=mine + Decimal(10_000_000))  # 0.01 alpha < 3x epoch yield
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert ev.evidence.startswith("recon:shares") and not k.venue.frozen and rt.state.recon_halt
    k.sdk.set_stake(lk.B0 + 40, lk.SA, lk.SN, shares=mine + Decimal(10_000 * lk.TAO))
    (ev2,) = call(r, rt, k, lk.B0 + 60)
    assert ev2.evidence.startswith("key_alarm:delta_above_yield")


def test_cash_mismatch_is_adjusted_chain_wins(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    call(r, rt, k, lk.B0 + 2)
    k.sdk.set_account(lk.B0 + 10, lk.REAL, free=k.sdk.free_at(lk.B0 + 2, lk.REAL) + 3 * lk.TAO)
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert ev.cash_delta == 3 * lk.TAO and ev.share_deltas == () and ev.evidence.startswith("recon:cash")
    assert rt.state.portfolio.cash == k.sdk.free_at(lk.B0 + 30, lk.REAL)
    k.feed(QuarantineCleared(lk.BOOK, Block(lk.B0 + 31), "operator"))
    rt.state = fold_batch(rt.state, [QuarantineCleared(lk.BOOK, Block(lk.B0 + 31), "operator")])
    assert call(r, rt, k, lk.B0 + 60) == [] and not rt.state.recon_halt


def test_unknown_position_on_a_traded_pair_alarms(lk: ModuleType) -> None:
    k = lk.Kit()
    it = lk.intent(OrderKind.ADD_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, tao_in=2 * lk.TAO)   # rejected (cap)
    assert isinstance(run(k.place(it)), OrderFailed)
    k.sdk.set_stake(lk.B0 + 5, lk.SB, lk.SN2, shares=Decimal(100 * lk.TAO))
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert ev.evidence.startswith("key_alarm:unknown_position")


# ------------------------------------------------------------------------------------------------- key alarms
def test_key_alarm_sources(lk: ModuleType) -> None:
    cases: dict[str, Callable[[Any], object]] = {
        "proxy_announcements_changed": lambda k: setattr(k.sdk, "announcements", [(lk.D0, "0x" + "ee" * 32, 5)]),
        "coldkey_swap_scheduled": lambda k: setattr(k.sdk, "swap_scheduled", True),
        "proxies_changed": lambda k: k.sdk.proxy_list.append((lk.D1, "Any", 0)),
        "foreign_StakeMoved": lambda k: k.sdk.acct_events.__setitem__(lk.B0 + 30, [
            ("SubtensorModule", "StakeMoved", {"coldkey": lk.REAL, "extrinsic_hash": "0x" + "ab" * 32})]),
        "TransactionFeePaidWithAlpha": lambda k: k.sdk.acct_events.__setitem__(lk.B0 + 30, [
            ("SubtensorModule", "TransactionFeePaidWithAlpha", {"who": lk.REAL})]),
    }
    for want, setup in cases.items():
        k = lk.Kit()
        rt = runtime(lk, k)
        r, _ = recon(lk, k)
        assert call(r, rt, k, lk.B0) == []                                    # baseline
        setup(k)
        (ev,) = call(r, rt, k, lk.B0 + 30)
        assert isinstance(ev, ReconAdjusted) and ev.evidence.startswith("key_alarm:") and want in ev.evidence, want
        assert k.venue.frozen and rt.state.recon_halt
        assert call(r, rt, k, lk.B0 + 60) == []                               # not re-journaled every tick
        k.feed(QuarantineCleared(lk.BOOK, Block(lk.B0 + 61), "operator"))
        if want in ("coldkey_swap_scheduled",):
            (again,) = call(r, rt, k, lk.B0 + 90)                             # a persisting condition re-alarms
            assert want in again.evidence


def test_own_stake_events_are_not_foreign(lk: ModuleType) -> None:
    k = lk.Kit()
    _, ack = filled_buy(lk, k)
    k.sdk.acct_events[lk.B0 + 2] = [("SubtensorModule", "StakeAdded", {"coldkey": lk.REAL, "extrinsic_hash": ack.inner_hash})]
    k.sdk.acct_events[lk.B0 + 3] = [("SubtensorModule", "StakeAdded", {"coldkey": lk.REAL,
                                                                        "extrinsic_hash": "0x" + "77" * 32})]
    k.sdk.add_extrinsic(lk.B0 + 3, "0x" + "77" * 32, lk.D0, lk.START_NONCE + 1)   # own (delegate, own nonce)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    assert call(r, rt, k, lk.B0 + 2) == []
    assert call(r, rt, k, lk.B0 + 3) == []


def test_delegate_nonce_jump_alarms(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    call(r, rt, k, lk.B0 + 2)
    k.sdk.set_account(lk.B0 + 10, lk.D0, nonce=lk.START_NONCE + 5)
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert "delegate_nonce:ops0:15!=12" in ev.evidence


def test_inner_included_after_a_miss_is_journaled_as_a_key_alarm(lk: ModuleType) -> None:
    k = lk.Kit()
    it = lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=lk.TAO // 2)
    ack = run(k.place(it))
    k.set_head(lk.B0 + 2)
    run(k.drain(lk.B0 + 2))                                                   # carrier absent: declared miss
    era_end = int(k.started(it).era_end)
    k.sdk.include_shielded(lk.B0 + 4, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE)
    k.set_head(era_end + 1)
    run(k.drain(era_end + 1))
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    out = call(r, rt, k, era_end + 1)
    assert any(isinstance(e, ReconAdjusted) and "inner_included_after_miss" in e.evidence for e in out)
    assert k.venue.pending_alarms == [] and k.venue.frozen


def test_periodic_preflight_failure_is_a_key_alarm(lk: ModuleType) -> None:
    k = lk.Kit()
    rt = runtime(lk, k)
    r, _ = recon(lk, k, preflight_every=100)
    assert call(r, rt, k, lk.B0) == []
    k.sdk.pays_fee[lk.D1] = True
    (ev,) = call(r, rt, k, lk.B0 + 100)
    assert "preflight:real_pays_fee[ops1]" in ev.evidence and k.venue.frozen


def test_errors_never_escape_and_fail_closed_after_a_long_outage(lk: ModuleType) -> None:
    k = lk.Kit()
    rt = runtime(lk, k)
    r, alerts = recon(lk, k)
    k.sdk.fail["account_events"] = ConnectionError("down")
    for i in range(49):
        assert call(r, rt, k, lk.B0 + i) == []
    assert alerts and alerts[-1][0] == "reconcile"
    (ev,) = call(r, rt, k, lk.B0 + 49)
    assert ev.evidence == "key_alarm:reconcile_unavailable" and k.venue.frozen


def test_non_live_venues_are_ignored(lk: ModuleType) -> None:
    k = lk.Kit()
    r, _ = recon(lk, k)

    class Other:
        book = lk.BOOK
        venue = object()
        state = None

    assert run(r(cast(BookRuntime, Other()), k.snap())) == ()


# ------------------------------------------------------------------------------------------------- dissolution
def test_observed_dissolution_payout_is_settled_once(lk: ModuleType) -> None:
    k = lk.Kit()
    filled_buy(lk, k)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    dereg = ChainEventObserved(ChainEvent(ChainEventKind.DEREGISTERED, Block(lk.B0 + 40), key=lk.KEY))
    k.feed(dereg)
    rt.state = fold_batch(rt.state, [dereg])
    assert rt.state.dissolving == (lk.KEY,)
    gone = [s for s in k.snap().subnets if s.key != lk.KEY]
    snap_gone = lk.snapshot(lk.B0 + 50, gone)
    assert list(run(r(cast(BookRuntime, rt), snap_gone))) == []              # baseline at the first DISSOLVING tick
    payout = 123_456_789
    k.sdk.set_account(lk.B0 + 60, lk.REAL, free=k.sdk.free_at(lk.B0 + 59, lk.REAL) + payout)
    out = list(run(r(cast(BookRuntime, rt), lk.snapshot(lk.B0 + 80, gone))))
    (ev,) = [e for e in out if isinstance(e, DeregSettled)]
    assert ev.model == "observed" and ev.payout_tao == payout and ev.key == lk.KEY and ev.alpha_value > 0
    assert ev.idem() == f"dereg:{lk.BOOK}:{lk.SN}:{lk.REG}"
    rt.state = fold_batch(rt.state, [ev])
    assert rt.state.dissolving == () and rt.state.portfolio.positions == ()
    assert [e for e in run(r(cast(BookRuntime, rt), lk.snapshot(lk.B0 + 120, gone)))
            if isinstance(e, DeregSettled)] == []


def test_live_dry_skips_ledger_reconciliation_but_keeps_key_alarms(lk: ModuleType) -> None:
    from taotrader.live.gate import LiveState

    k = lk.Kit(state=LiveState.PLAN_ONLY)
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)                                      # real stake the dry journal never saw
    run(k.place(lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=lk.TAO // 2)))
    k.sdk.set_account(lk.B0, lk.REAL, free=lk.REAL_FREE + 7 * lk.TAO)
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    assert call(r, rt, k, lk.B0) == []
    k.sdk.swap_scheduled = True
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert ev.evidence == "key_alarm:coldkey_swap_scheduled" and ev.share_deltas == () and ev.cash_delta == 0


def test_a_persisting_proxy_change_re_alarms_after_a_clear(lk: ModuleType) -> None:
    k = lk.Kit()
    rt = runtime(lk, k)
    r, _ = recon(lk, k)
    call(r, rt, k, lk.B0)
    k.sdk.proxy_list.append((lk.D1, "Any", 0))
    (ev,) = call(r, rt, k, lk.B0 + 30)
    assert "proxies_changed" in ev.evidence
    k.feed(QuarantineCleared(lk.BOOK, Block(lk.B0 + 31), "operator"))
    (again,) = call(r, rt, k, lk.B0 + 60)
    assert "proxies_changed" in again.evidence and k.venue.frozen
