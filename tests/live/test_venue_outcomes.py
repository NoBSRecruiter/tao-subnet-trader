"""LiveVenue outcomes (DESIGN.md 9.6 steps 3-5, 10.5): N+2 miss rule, inner failures, ProxyExecuted errors, share-delta
fills (partial, epoch drain, two orders in one block), carrier-fee settlement, nonce mismatch and crash resolution."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import ModuleType
from typing import Any

from taotrader.core.events import (
    CarrierFeeSettled,
    FillReported,
    OrderFailed,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from taotrader.core.orders import FailReason, OrderKind, Resolution, Urgency
from taotrader.live.nonce import SubmissionRow
from taotrader.live.sdk_port import block_hash_of


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def buy(lk: ModuleType, tao: int, **kw: Any) -> Any:
    return lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=tao, **kw)


def placed_buy(lk: ModuleType, k: Any, tao: int | None = None, **kw: Any) -> tuple[Any, VenueAck]:
    it = buy(lk, lk.TAO // 2 if tao is None else tao, **kw)
    ack = run(k.place(it))
    assert isinstance(ack, VenueAck), ack
    return it, ack


# ------------------------------------------------------------------------------------------------- fills
def test_fill_from_share_deltas_with_fees_from_both_extrinsics(lk: ModuleType) -> None:
    k = lk.Kit()
    it, ack = placed_buy(lk, k)
    out, shares = k.land_buy(ack, it)
    k.set_head(lk.B0 + 1)
    assert run(k.drain(lk.B0 + 1)) == []                                     # not due before N + 2 is finalized
    k.set_head(lk.B0 + 2)
    evs = run(k.drain(lk.B0 + 2))
    assert len(evs) == 1 and isinstance(evs[0], FillReported)
    f = evs[0].fill
    assert f.block == lk.B0 + 2 and f.shares == shares and f.alpha == out and f.tao == lk.TAO // 2
    assert f.tx_fee == 94_560 + 933_081 and f.complete and f.exact_block and f.kind is OrderKind.ADD_STAKE_LIMIT
    assert f.spot_before == int(lk.DEFAULT_POOLS[lk.SN].spot_rao()) and f.fill_id == f"{it.order_id}:0:0"
    assert k.venue.delegates.free(lk.B0 + 3)[0] == "ops0"
    assert k.venue.delegates.expected_nonce("ops0") == lk.START_NONCE + 2


def test_partial_fill(lk: ModuleType) -> None:
    k = lk.Kit()
    it, ack = placed_buy(lk, k, partial=True)
    k.land_buy(ack, it, tao=lk.TAO * 3 // 10)
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    assert isinstance(ev, FillReported) and not ev.fill.complete and ev.fill.tao == lk.TAO * 3 // 10


def test_fill_in_an_epoch_drain_block_excludes_the_drain_yield(lk: ModuleType) -> None:
    k = lk.Kit(live=lk.live_cfg(max_position_tao=1_000.0))
    held = 50_000 * lk.TAO
    k.hold(lk.SA, lk.SN, held)
    it, ack = placed_buy(lk, k)
    drain = 20_000 * lk.TAO                                                   # the epoch drain runs before extrinsics
    out, shares = k.land_buy(ack, it, drain=drain)
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    f = ev.fill
    pre = run(k.sdk.post_state(block_hash_of(lk.B0 + 1), lk.REAL, lk.SA, lk.SN, lk.D0))
    post = run(k.sdk.post_state(block_hash_of(lk.B0 + 2), lk.REAL, lk.SA, lk.SN, lk.D0))
    value_delta = post.alpha_value - pre.alpha_value
    assert f.shares == shares and abs(f.alpha - out) <= 1
    assert value_delta > f.alpha + 100 * lk.TAO                              # a value delta would book the drain's yield


def test_two_own_orders_in_one_block_apportion_free_tao_by_stake_events(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SB, lk.SN2, 10_000 * lk.TAO)
    b_it, b_ack = placed_buy(lk, k)
    s_it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, alpha_in=2_000 * lk.TAO)
    s_ack = run(k.place(s_it))
    assert isinstance(s_ack, VenueAck) and s_ack.expected_fill_block == b_ack.expected_fill_block
    free0 = k.sdk.free_at(lk.B0 + 1, lk.REAL)
    k.land_buy(b_ack, b_it)
    tao_out = k.land_sell(s_ack, s_it, amount=2_000 * lk.TAO, hotkey=lk.SB, netuid=lk.SN2)
    k.sdk.set_account(lk.B0 + 2, lk.REAL, free=free0 - lk.TAO // 2 + tao_out + 7)   # 7 rao residual -> reconcile
    k.set_head(lk.B0 + 2)
    evs = run(k.drain(lk.B0 + 2))
    fills = {e.fill.kind: e.fill for e in evs if isinstance(e, FillReported)}
    assert fills[OrderKind.ADD_STAKE_LIMIT].tao == lk.TAO // 2
    assert fills[OrderKind.REMOVE_STAKE_LIMIT].tao == tao_out
    assert fills[OrderKind.REMOVE_STAKE_LIMIT].shares == Decimal(2_000 * lk.TAO)


def test_full_exit_drain_remainder_is_re_exited_with_the_new_exact_amount(lk: ModuleType) -> None:
    k = lk.Kit()
    held = 3_000 * lk.TAO
    k.hold(lk.SA, lk.SN, held)
    it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.URGENT, partial=True)
    ack = run(k.place(it))
    assert k.sdk.submitted[-1][1].amount == held
    remainder = 7 * lk.TAO                                                    # yield credited between read and inclusion
    k.land_sell(ack, it, amount=held, remainder=remainder)
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    assert isinstance(ev, FillReported) and not ev.fill.complete
    k.set_head(lk.B0 + 3)
    again = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, block=lk.B0 + 3, full=True, urgency=Urgency.URGENT, partial=True,
                      attempt=1)
    assert isinstance(run(k.place(again, k.snap(lk.B0 + 3))), VenueAck)
    assert k.sdk.submitted[-1][1].amount == remainder


def test_move_fill_debits_origin_and_credits_destination_shares(lk: ModuleType) -> None:
    k = lk.Kit()
    held = 3_000 * lk.TAO
    k.hold(lk.SA, lk.SN, held)
    it = lk.intent(OrderKind.MOVE_STAKE, full=True, dest=lk.HK_B)
    ack = run(k.place(it))
    b = lk.B0 + 2
    st = k.started(it)
    k.sdk.set_stake(b, lk.SA, lk.SN, shares=Decimal(0), hk_alpha=lk.HK_ALPHA0, hk_shares=Decimal(lk.HK_ALPHA0))
    k.sdk.set_stake(b, lk.SB, lk.SN, shares=Decimal(held), hk_alpha=lk.HK_ALPHA0 + held,
                    hk_shares=Decimal(lk.HK_ALPHA0 + held))
    k.sdk.include_shielded(b, ack.carrier_hash, ack.inner_hash, lk.D0, int(st.nonce))
    k.set_head(b)
    (ev,) = run(k.drain(b))
    f = ev.fill
    assert f.kind is OrderKind.MOVE_STAKE and f.shares == Decimal(held) and f.dest_shares == Decimal(held)
    assert f.dest_hotkey == lk.HK_B and f.tao == 0 and f.swap_fee == 0 and f.complete


# ------------------------------------------------------------------------------------------------- failures
def test_carrier_absent_from_n2_is_a_final_miss_with_fee_0_and_rotation(lk: ModuleType) -> None:
    k = lk.Kit()
    it, _ = placed_buy(lk, k)
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.SHIELD_MISSED and ev.expired and ev.tx_fee == 0
    assert ev.detail == "carrier_absent" and ev.block == lk.B0 + 2
    era_end = int(k.started(it).era_end)
    assert k.venue.delegates.locked_until["ops0"] == era_end + 2
    assert "ops0" not in k.venue.delegates.free(era_end + 2) and "ops0" in k.venue.delegates.free(era_end + 3)
    retry = buy(lk, lk.TAO // 2, block=lk.B0 + 2, attempt=1)
    assert isinstance(run(k.place(retry, k.snap(lk.B0 + 2))), VenueAck)
    assert k.started(retry).delegate == "ops1"                                # re-decided at once on another delegate
    assert k.venue.unsettled == [(it.order_id, 0)]


def _miss(lk: ModuleType) -> tuple[Any, Any, VenueAck, int]:
    k = lk.Kit()
    it, ack = placed_buy(lk, k)
    k.set_head(lk.B0 + 2)
    run(k.drain(lk.B0 + 2))
    return k, it, ack, int(k.started(it).era_end)


def test_carrier_fee_settlement_never_included(lk: ModuleType) -> None:
    k, _, _, era_end = _miss(lk)
    k.set_head(era_end)
    assert run(k.drain(era_end)) == []                                       # only once finalized > era_end
    k.set_head(era_end + 1)
    (ev,) = run(k.drain(era_end + 1))
    assert isinstance(ev, CarrierFeeSettled) and ev.fee_rao == 0 and ev.outcome == "never_included"
    assert k.venue.unsettled == [] and k.venue.delegates.expected_nonce("ops0") == lk.START_NONCE


def test_carrier_fee_settlement_carrier_only_fee_is_the_balance_change(lk: ModuleType) -> None:
    k, it, ack, era_end = _miss(lk)
    k.sdk.include_shielded(lk.B0 + 4, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE, inner=False)
    k.set_head(era_end + 1)
    (ev,) = run(k.drain(era_end + 1))
    assert isinstance(ev, CarrierFeeSettled) and ev.outcome == "carrier_only"
    before = k.subs.get(it.order_id, 0).delegate_free_before
    assert ev.fee_rao == 94_560 == before - k.sdk.free_at(era_end + 1, lk.D0)
    k2, _, _, era2 = _miss(lk)                                                # no visible carrier: the balance change
    k2.sdk.set_account(lk.B0 + 5, lk.D0, nonce=lk.START_NONCE + 1, free=lk.DELEGATE_FREE - 98_000)
    k2.set_head(era2 + 1)
    (ev2,) = run(k2.drain(era2 + 1))
    assert ev2.outcome == "carrier_only" and ev2.fee_rao == 98_000
    assert k2.venue.delegates.expected_nonce("ops0") == lk.START_NONCE + 1


def test_carrier_fee_settlement_inner_included_raises_a_key_alarm(lk: ModuleType) -> None:
    k, it, ack, era_end = _miss(lk)
    k.sdk.include_shielded(lk.B0 + 4, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE)
    k.set_head(era_end + 1)
    (ev,) = run(k.drain(era_end + 1))
    assert isinstance(ev, CarrierFeeSettled) and ev.outcome == "inner_included" and ev.fee_rao == 94_560 + 933_081
    assert k.venue.pending_alarms == [f"inner_included_after_miss:{it.order_id}:0"]
    k2, _, _, era2 = _miss(lk)                                                # nonce jumped, nothing of ours found
    k2.sdk.set_account(lk.B0 + 5, lk.D0, nonce=lk.START_NONCE + 2, free=lk.DELEGATE_FREE - 500_000)
    k2.set_head(era2 + 1)
    (ev2,) = run(k2.drain(era2 + 1))
    assert ev2.outcome == "inner_included" and ev2.fee_rao == 500_000 and k2.venue.pending_alarms
    assert k2.rebuilt().pending_alarms == k2.venue.pending_alarms            # journal-derived, survives a restart


def test_carrier_present_inner_absent_is_a_miss_with_the_carrier_fee(lk: ModuleType) -> None:
    k = lk.Kit()
    _, ack = placed_buy(lk, k)
    k.sdk.include_shielded(lk.B0 + 2, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE, inner=False, filler=2)
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.SHIELD_MISSED and ev.expired
    assert ev.tx_fee == 94_560 and ev.detail == "inner_absent"
    assert k.venue.unsettled == [] and k.venue.delegates.expected_nonce("ops0") == lk.START_NONCE + 1


def test_inner_extrinsic_failed_pays_the_fees(lk: ModuleType) -> None:
    k = lk.Kit()
    _, ack = placed_buy(lk, k)
    k.sdk.include_shielded(lk.B0 + 2, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE,
                           inner_events=[("System", "ExtrinsicFailed", {"error": "SubtensorModule.NotEnoughBalanceToStake"})])
    k.set_head(lk.B0 + 2)
    (ev,) = run(k.drain(lk.B0 + 2))
    assert isinstance(ev, OrderFailed) and not ev.expired and ev.tx_fee == 94_560 + 933_081
    assert ev.reason is FailReason.OTHER and ev.detail == "SubtensorModule.NotEnoughBalanceToStake"
    k2 = lk.Kit()
    _, ack2 = placed_buy(lk, k2)
    k2.sdk.include_shielded(lk.B0 + 2, ack2.carrier_hash, ack2.inner_hash, lk.D0, lk.START_NONCE,
                            inner_events=[("System", "ExtrinsicFailed", {"error": "Swap::PriceLimitExceeded"})])
    k2.set_head(lk.B0 + 2)
    (ev2,) = run(k2.drain(lk.B0 + 2))
    assert ev2.reason is FailReason.PRICE_LIMIT_EXCEEDED


def test_proxy_executed_err_under_success_maps_the_reason(lk: ModuleType) -> None:
    for name, want in (("SlippageTooHigh", FailReason.SLIPPAGE_TOO_HIGH), ("SomethingNew", FailReason.PROXY_ERROR),
                       ("SubtensorModule.StakeUnavailable", FailReason.STAKE_UNAVAILABLE)):
        k = lk.Kit()
        _, ack = placed_buy(lk, k)
        k.sdk.include_shielded(lk.B0 + 2, ack.carrier_hash, ack.inner_hash, lk.D0, lk.START_NONCE, inner_events=[
            ("Proxy", "ProxyExecuted", {"result": "Err", "error": name}), ("System", "ExtrinsicSuccess", {})])
        k.set_head(lk.B0 + 2)
        (ev,) = run(k.drain(lk.B0 + 2))
        assert isinstance(ev, OrderFailed) and ev.reason is want and ev.detail == name
        assert ev.tx_fee == 94_560 + 933_081 and not ev.expired


def test_advance_defers_on_provider_errors(lk: ModuleType) -> None:
    k = lk.Kit()
    it, ack = placed_buy(lk, k)
    k.land_buy(ack, it)
    k.set_head(lk.B0 + 2)
    k.sdk.fail["block_extrinsics"] = ConnectionError("boom")
    assert run(k.drain(lk.B0 + 2)) == [] and "boom" in k.venue.last_error
    del k.sdk.fail["block_extrinsics"]
    assert isinstance(run(k.drain(lk.B0 + 2))[0], FillReported)


def test_unshielded_risk_exit_is_found_by_hash_or_expires(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 3_000 * lk.TAO)
    it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO, urgency=Urgency.URGENT, partial=True,
                   shielded=False)
    ack = run(k.place(it))
    assert isinstance(ack, VenueAck) and k.sdk.submitted[-1][0] == "submit_plain"
    assert ack.expected_fill_block == lk.B0 + 1 and ack.inner_hash == ""
    b = lk.B0 + 3
    k.sdk.set_stake(b, lk.SA, lk.SN, shares=Decimal(2_000 * lk.TAO), hk_alpha=lk.HK_ALPHA0 + 2_000 * lk.TAO,
                    hk_shares=Decimal(lk.HK_ALPHA0 + 2_000 * lk.TAO))
    k.sdk.set_account(b, lk.REAL, free=lk.REAL_FREE + 1_300_000_000)
    k.sdk.include_plain(b, ack.carrier_hash, lk.D0, lk.START_NONCE)
    k.set_head(b)
    (ev,) = run(k.drain(b))
    assert isinstance(ev, FillReported) and ev.fill.tao == 1_300_000_000 and ev.fill.tx_fee == 837_000
    k2 = lk.Kit()
    k2.hold(lk.SA, lk.SN, 3_000 * lk.TAO)
    run(k2.place(it))
    era_end = int(k2.started(it).era_end)
    k2.set_head(era_end + 1)
    (ev2,) = run(k2.drain(era_end + 1))
    assert ev2.reason is FailReason.ERA_EXPIRED and ev2.expired and ev2.tx_fee == 0


# ------------------------------------------------------------------------------------------------- resolve
def test_nonce_mismatch_is_unknown_then_resolved_by_the_used_nonce(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    used = lk.START_NONCE + 3
    carrier, inner = k.sdk.new_hash("c"), k.sdk.new_hash("i")
    k.sdk.submit_script = [(carrier, inner, lk.B0, used)]
    ev = run(k.place(it))
    assert isinstance(ev, SubmitUnknown) and ev.detail == f"nonce_mismatch:{used}"
    row = k.subs.get(it.order_id, 0)
    assert row.used_nonce == used and row.state == "nonce_mismatch"
    ack = VenueAck(lk.BOOK, it.order_id, 0, lk.B0, lk.B0 + 2, carrier, inner)  # what the chain did
    k.sdk.set_account(lk.B0 + 1, lk.D0, nonce=used)
    k.land_buy(ack, it)                                                       # stake and balances at N + 2
    k.sdk.extrinsics.clear()                                                  # re-script the carrier at the USED nonce
    k.sdk.include_shielded(lk.B0 + 2, carrier, inner, lk.D0, used,
                           inner_events=[("Proxy", "ProxyExecuted", {"result": "Ok"}), ("System", "ExtrinsicSuccess", {})])
    k.set_head(lk.B0 + 1)
    status, evs = run(k.venue.resolve(it, k.snap()))
    assert status is Resolution.UNRESOLVABLE_YET and evs == ()
    k.set_head(lk.B0 + 3)
    status, evs = run(k.venue.resolve(it, k.snap()))
    assert status is Resolution.LANDED and isinstance(evs[0], FillReported) and evs[0].fill.block == lk.B0 + 2
    k.feed(evs[0])
    assert k.venue.delegates.expected_nonce("ops0") == used + 2
    assert len(k.sdk.submitted) == 1                                         # nothing is ever re-sent


def test_crash_between_submit_started_and_ack_never_sent_is_not_placed(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    from taotrader.core.events import OrderIntended

    k.feed(OrderIntended(it))
    d, n, era_end = run(k.venue.reserve(it, k.snap()))
    k.feed(SubmitStarted(lk.BOOK, it.order_id, 0, d, n, era_end))
    # crash: venue.submit never ran; recovery journals SubmitUnknown("recovered_submitting") first
    k.feed(SubmitUnknown(lk.BOOK, it.order_id, 0, "recovered_submitting"))
    fresh = k.rebuilt()
    k.set_head(int(era_end) + 8)
    assert run(fresh.resolve(it, k.snap()))[0] is Resolution.UNRESOLVABLE_YET
    k.set_head(int(era_end) + 9)
    status, evs = run(fresh.resolve(it, k.snap()))
    assert status is Resolution.NOT_PLACED and isinstance(evs[0], OrderFailed)
    assert evs[0].reason is FailReason.NOT_PLACED and evs[0].tx_fee == 0
    assert k.sdk.submitted == []


def test_crash_after_the_send_is_resolved_by_delegate_nonce(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    from taotrader.core.events import OrderIntended

    k.feed(OrderIntended(it))
    d, n, era_end = run(k.venue.reserve(it, k.snap()))
    k.feed(SubmitStarted(lk.BOOK, it.order_id, 0, d, n, era_end))
    k.subs.put(SubmissionRow(it.order_id, 0, d, nonce=n, era_end=int(era_end), state="sending"))
    carrier, inner, head, used = run(k.sdk.submit_shielded(_call(lk, it), d))      # the send happened, then a crash
    assert used == n
    ack = VenueAck(lk.BOOK, it.order_id, 0, head, head + 2, carrier, inner)
    out, shares = k.land_buy(ack, it)
    k.feed(SubmitUnknown(lk.BOOK, it.order_id, 0, "recovered_submitting"))
    fresh = k.rebuilt()
    k.set_head(lk.B0 + 4)
    status, evs = run(fresh.resolve(it, k.snap()))
    assert status is Resolution.LANDED and isinstance(evs[0], FillReported)
    assert evs[0].fill.shares == shares and evs[0].fill.alpha == out


def _call(lk: ModuleType, it: Any) -> Any:
    from taotrader.live.sdk_port import LiveCall

    return LiveCall(OrderKind.ADD_STAKE_LIMIT, lk.SA, lk.SN, int(it.tao_in), int(it.limit_price), False, None, 1.0, None)


def test_resolve_of_terminal_and_acked_orders(lk: ModuleType) -> None:
    k = lk.Kit()
    it, _ = placed_buy(lk, k)
    assert run(k.venue.resolve(it, k.snap())) == (Resolution.PLACED, ())
    k.set_head(lk.B0 + 2)
    run(k.drain(lk.B0 + 2))                                                    # a miss: terminal
    assert run(k.venue.resolve(it, k.snap())) == (Resolution.LANDED, ())
    never = buy(lk, lk.TAO // 2, attempt=7)
    status, evs = run(k.venue.resolve(never, k.snap()))
    assert status is Resolution.NOT_PLACED and evs[0].detail == "no_submit_started"


def test_observe_rebuild_reproduces_the_venue_state(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SB, lk.SN2, 10_000 * lk.TAO)
    a_it, a_ack = placed_buy(lk, k)
    k.land_buy(a_ack, a_it)
    s_it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, alpha_in=2_000 * lk.TAO)
    run(k.place(s_it))
    k.set_head(lk.B0 + 2)
    run(k.drain(lk.B0 + 2))
    fresh = k.rebuilt()
    for attr in ("terminal", "unsettled", "positions", "buy_fills", "frozen", "pending_alarms", "used_nonce"):
        assert getattr(fresh, attr) == getattr(k.venue, attr), attr
    assert fresh.delegates.locked_until == k.venue.delegates.locked_until
    assert fresh.delegates.expected == k.venue.delegates.expected
    assert fresh.delegates.in_flight == k.venue.delegates.in_flight
