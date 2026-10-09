"""LiveVenue submit path (DESIGN.md 9.3, 9.4, 9.6 step 2, 9.8, 10.5): FakeSdkPort only, no network, no bittensor."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from taotrader.core.config import RiskCfg
from taotrader.core.events import FillReported, OrderFailed, QuarantineCleared, ReconAdjusted, SubmitUnknown, VenueAck
from taotrader.core.orders import FailReason, Fill, OrderKind, Urgency
from taotrader.core.units import U64_MAX, AlphaRao, Block, NetUid, OrderId, Ppm, PriceRao, Rao, SubnetKey
from taotrader.live.gate import LiveState
from taotrader.live.nonce import NoFreeDelegate


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def reject(ev: Any, prefix: str) -> None:
    assert isinstance(ev, OrderFailed), ev
    assert ev.reason is FailReason.VENUE_REJECT and ev.tx_fee == 0
    assert ev.detail.startswith(prefix), ev.detail


def buy(lk: ModuleType, tao: int, **kw: Any) -> Any:
    return lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=tao, **kw)


def buy_fill(lk: ModuleType, tao: int, block: int, n: int) -> FillReported:
    return FillReported(Fill(fill_id=f"x{n}:0:0", order_id=OrderId(f"{n:024x}"), attempt=0, book=lk.BOOK,
                             block=Block(block), kind=OrderKind.ADD_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, tao=Rao(tao),
                             alpha=AlphaRao(1), shares=Decimal(1), swap_fee=0, author_fee_tao=Rao(0), tx_fee=Rao(0),
                             d_pool_tao=0, d_pool_alpha=0, spot_before=PriceRao(1), shortfall_ppm=Ppm(0), complete=True))


# ------------------------------------------------------------------------------------------------- happy path
def test_buy_is_acked_with_exact_amount_per_call_buy_policy_and_sidecar(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    ev = run(k.place(it))
    assert isinstance(ev, VenueAck)
    assert ev.submit_block == lk.B0 and ev.expected_fill_block == lk.B0 + 2 and ev.carrier_hash and ev.inner_hash
    kind, call, delegate = k.sdk.submitted[-1]
    assert kind == "submit_shielded" and delegate == "ops0"
    assert call.kind is OrderKind.ADD_STAKE_LIMIT and call.amount == lk.TAO // 2 and type(call.amount) is int
    assert call.max_spend_tao == k.live.max_order_tao and call.allowed_netuids is None
    assert call.limit_price_rao == int(it.limit_price) and call.hotkey_ss58 == lk.SA
    st = k.started(it)
    assert st.nonce == lk.START_NONCE and st.era_end == lk.B0 + 8 + 2
    row = k.subs.get(it.order_id, it.attempt)
    assert row.state == "sent" and row.delegate_free_before == lk.DELEGATE_FREE and row.used_nonce == lk.START_NONCE
    assert row.carrier_hash == ev.carrier_hash and row.submit_head == lk.B0 and row.expected_fill_block == lk.B0 + 2
    assert "plan" in k.sdk.calls and "quote" in k.sdk.calls


def test_submit_is_idempotent_and_never_resends(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    ack = run(k.place(it))
    assert run(k.venue.submit(it, k.snap())) == ack
    fresh = k.rebuilt()
    assert run(fresh.submit(it, k.snap())) == ack
    assert len(k.sdk.submitted) == 1


def test_already_sent_without_ack_is_unknown_not_resent(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    k.sdk.submit_script = [RuntimeError("socket closed")]
    ev = run(k.place(it))
    assert isinstance(ev, SubmitUnknown) and "socket closed" in ev.detail
    assert k.subs.get(it.order_id, 0).state == "raised"
    again = run(k.venue.submit(it, k.snap()))
    assert isinstance(again, SubmitUnknown) and again.detail == "already_sent"
    assert len(k.sdk.submitted) == 1


# ------------------------------------------------------------------------------------------------- pre-submit rejects
def test_crossed_limit_at_submit_is_rejected(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    k.sdk.quote_override[lk.SN] = (10**12, int(it.limit_price) + 1)       # runtime spot above the buy limit
    reject(run(k.place(it)), "limit_crossed")
    assert k.sdk.submitted == []


def test_sell_limit_crossed_by_a_falling_runtime_spot(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO)
    k.sdk.quote_override[lk.SN] = (10**9, int(it.limit_price))              # runtime spot fell to the floor
    reject(run(k.place(it)), "limit_crossed")


def test_limit_is_tightened_from_the_runtime_spot_never_loosened(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    spot = int(lk.DEFAULT_POOLS[lk.SN].spot_rao())
    k.sdk.quote_override[lk.SN] = (10**12, spot * 99 // 100)              # price fell 1%: tighter buy limit
    assert isinstance(run(k.place(it)), VenueAck)
    call = k.sdk.submitted[-1][1]
    assert call.limit_price_rao < int(it.limit_price)
    assert call.limit_price_rao > spot * 99 // 100


def test_plan_violations_reject(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.plan_violations = ["policy: something"]
    reject(run(k.place(buy(lk, lk.TAO // 2))), "plan:policy: something")
    k.sdk.plan_violations = []
    k.sdk.plan_fee_rao = 6_000_000                                           # above max_fee_tao 0.005
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=1))), "plan:")
    assert k.sdk.submitted == []


def test_all_zero_quote_is_rejected(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.quote_override[lk.SN] = (0, 0)
    reject(run(k.place(buy(lk, lk.TAO // 2))), "quote_zero")


def test_kill_file_stops_every_submission(lk: ModuleType, tmp_path: Path) -> None:
    k = lk.Kit(tmp_path)
    (tmp_path / "KILL").write_text("halt", encoding="utf-8")
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    reject(run(k.place(buy(lk, lk.TAO // 2))), "kill_file")
    exit_it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True)
    reject(run(k.place(exit_it)), "kill_file")
    assert k.sdk.submitted == []


def test_generation_gone_safe_mode_subtoken_and_stale_rejects(lk: ModuleType) -> None:
    k = lk.Kit()
    gone = buy(lk, lk.TAO // 2, key=SubnetKey(NetUid(lk.SN), Block(lk.REG + 1)))
    reject(run(k.place(gone)), "generation_gone")
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=1), k.snap(safe_mode_until=Block(lk.B0 + 10)))), "safe_mode")
    snap = k.snap()
    off = replace(snap, subnets=tuple(replace(s, subtoken_enabled=False) for s in snap.subnets))
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=2), off)), "subtoken_disabled")
    k.set_head(lk.B0 + 30, fin=lk.B0 + 20)                                   # a re-drive after an outage
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=3))), "stale_intent")
    assert k.sdk.submitted == []


def test_head_lag_and_stale_shield_era(lk: ModuleType) -> None:
    from taotrader.core.events import HealthObs

    k = lk.Kit()
    k.tick(lk.B0, HealthObs(3, 12, 2, 2, 0))
    reject(run(k.place(buy(lk, lk.TAO // 2))), "head_lagging")
    k.tick(lk.B0, HealthObs(6, 12, 2, 0, 0))
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=1))), "shield_era_stale")


def test_move_stake_limit_disabled_and_unshielded_only_for_risk_exits(lk: ModuleType) -> None:
    from taotrader.core.orders import OrderIntent

    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    base = lk.intent(OrderKind.MOVE_STAKE, alpha_in=1_000 * lk.TAO, dest=lk.HK_B)
    rot = OrderIntent(**{f: getattr(base, f) for f in base.__slots__ if f not in ("kind", "dest_key")},
                      kind=OrderKind.MOVE_STAKE_LIMIT, dest_key=lk.KEY2)
    reject(run(k.place(rot)), "rotation_disabled")
    plain = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO, shielded=False)
    reject(run(k.place(plain)), "unshielded_not_risk_exit")


def test_long_only_sell_never_exceeds_the_position(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 500 * lk.TAO)
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO))), "exceeds_position")
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO, hotkey=lk.HK_B))), "no_position")


# ------------------------------------------------------------------------------------------------- buy-only caps
def test_caps_exceeded_on_a_buy(lk: ModuleType) -> None:
    k = lk.Kit()
    reject(run(k.place(buy(lk, 3 * lk.TAO // 2))), "cap:max_order_tao")
    for i in range(10):
        k.feed(buy_fill(lk, lk.TAO * 48 // 100, lk.B0 - 100 + i, i))          # 4.8 TAO bought in the window
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=1))), "cap:max_daily_turnover_tao")
    k2 = lk.Kit()
    spot = int(lk.DEFAULT_POOLS[lk.SN].spot_rao())
    k2.hold(lk.SA, lk.SN, 49 * lk.TAO * lk.TAO // (10 * spot))                # ~4.9 TAO executable already held
    reject(run(k2.place(buy(lk, lk.TAO // 2))), "cap:max_position_tao")
    k3 = lk.Kit(live=lk.live_cfg(allowed_netuids=(lk.SN2,)))
    reject(run(k3.place(buy(lk, lk.TAO // 2))), "cap:allowed_netuids")
    k4 = lk.Kit()
    k4.sdk.set_account(0, lk.REAL, free=lk.TAO // 2 + 10)
    reject(run(k4.place(buy(lk, lk.TAO // 2))), "min_free_real")


def test_turnover_window_expires_after_7200_blocks(lk: ModuleType) -> None:
    k = lk.Kit()
    for i in range(10):
        k.feed(buy_fill(lk, lk.TAO * 48 // 100, lk.B0 - 7_200 - i, i))
    assert isinstance(run(k.place(buy(lk, lk.TAO // 2))), VenueAck)


def test_tier_a_5_tao_exit_with_max_order_1_and_turnover_used_up_is_submitted(lk: ModuleType) -> None:
    k = lk.Kit(live=lk.live_cfg(max_order_tao=1.0, max_daily_turnover_tao=5.0))
    for i in range(10):
        k.feed(buy_fill(lk, lk.TAO // 2, lk.B0 - 50 + i, i))                  # daily buy turnover used up
    spot = int(lk.DEFAULT_POOLS[lk.SN].spot_rao())
    alpha = 5 * lk.TAO * lk.TAO // spot                                       # ~5 TAO of alpha
    k.hold(lk.SA, lk.SN, alpha)
    it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, alpha_in=0, urgency=Urgency.EMERGENCY, partial=True)
    ev = run(k.place(it))
    assert isinstance(ev, VenueAck), ev
    call = k.sdk.submitted[-1][1]
    assert call.kind is OrderKind.REMOVE_STAKE_LIMIT and call.amount == alpha and call.amount < U64_MAX
    assert call.max_spend_tao is None and lk.SN in (call.allowed_netuids or ())


# ------------------------------------------------------------------------------------------------- exact amounts
def test_full_exit_and_move_send_the_exact_alpha_value_never_all(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 1_234_567_891_234)
    full = lk.intent(OrderKind.REMOVE_STAKE_FULL_LIMIT, full=True, alpha_in=0)
    assert isinstance(run(k.place(full)), VenueAck)
    call = k.sdk.submitted[-1][1]
    assert call.kind is OrderKind.REMOVE_STAKE_LIMIT                           # call 103 is never sent live
    assert call.amount == 1_234_567_891_234 and isinstance(call.amount, int)
    k2 = lk.Kit()
    k2.hold(lk.SA, lk.SN, 987_654_321_000)
    mv = lk.intent(OrderKind.MOVE_STAKE, full=True, dest=lk.HK_B)
    assert isinstance(run(k2.place(mv)), VenueAck)
    call = k2.sdk.submitted[-1][1]
    assert call.kind is OrderKind.MOVE_STAKE and call.amount == 987_654_321_000 and call.limit_price_rao == 0
    assert call.dest_hotkey_ss58 == lk.SB and call.max_spend_tao is None


def test_fee_float_low_allows_risk_exits_only_and_exhausted_blocks_all(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.set_account(0, lk.D0, free=10_000_000)                              # 0.01 TAO < fee_float_exits 0.05 TAO
    k.hold(lk.SA, lk.SN, 1_000 * lk.TAO)
    reject(run(k.place(buy(lk, lk.TAO // 2))), "fee_float_low")
    ex = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.URGENT, partial=True)
    assert isinstance(run(k.place(ex)), VenueAck)
    k2 = lk.Kit()
    for d in (lk.D0, lk.D1, lk.D2):
        k2.sdk.set_account(0, d, free=1_000_000)                              # below max_fee: the alpha-fee trap
    k2.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    reject(run(k2.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True))),
           "fee_float_exhausted")


# ------------------------------------------------------------------------------------------------- arming
def test_unarmed_expired_token_refuses_everything_without_the_flag(lk: ModuleType) -> None:
    k = lk.Kit()
    k.clock[0] = lk.NOW + 3_601
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    reject(run(k.place(buy(lk, lk.TAO // 2))), "unarmed")
    reject(run(k.place(lk.intent(OrderKind.MOVE_STAKE, full=True, dest=lk.HK_B))), "unarmed")
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True))),
           "unarmed")
    assert k.sdk.submitted == []


@pytest.mark.parametrize("cause", ["expired", "spec"])
def test_unarmed_risk_exit_exception(lk: ModuleType, cause: str) -> None:
    k = lk.Kit(live=lk.live_cfg(risk_exits_when_unarmed=True))
    snap = k.snap()
    if cause == "expired":
        k.clock[0] = lk.NOW + 10_000
    else:
        snap = k.snap(spec_version=lk.SPEC + 1)                               # SPEC_CHANGED: not yet accepted
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    k.hold(lk.SB, lk.SN2, 5_000 * lk.TAO)
    reject(run(k.place(buy(lk, lk.TAO // 2), snap)), "unarmed")                 # never a buy
    reject(run(k.place(lk.intent(OrderKind.MOVE_STAKE, full=True, dest=lk.HK_B), snap)), "unarmed")   # never a move
    for urg in (Urgency.HIGH, Urgency.NORMAL):
        reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=urg, partial=True), snap)),
               "unarmed")
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO, urgency=Urgency.EMERGENCY,
                                 partial=True), snap)), "unarmed")          # partial sells are not full exits
    k.guard.verdict = False                                                   # V2/V3/V6 failing on the new spec
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True,
                                 attempt=1), snap)), "unarmed:v2_v3_v6")
    assert k.sdk.submitted == []
    k.guard.verdict = True
    em = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True, attempt=2)
    assert isinstance(run(k.place(em, snap)), VenueAck)
    ur = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, full=True, urgency=Urgency.URGENT,
                   partial=True)
    assert isinstance(run(k.place(ur, snap)), VenueAck)
    assert [c.kind for _, c, _ in k.sdk.submitted] == [OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_LIMIT]


def test_plan_only_runs_plan_and_never_writes(lk: ModuleType) -> None:
    k = lk.Kit(state=LiveState.PLAN_ONLY)
    k.sdk.forbid_writes = True
    assert k.venue.caps.kind == "live_dry"
    it = buy(lk, lk.TAO // 2)
    ev = run(k.place(it))
    reject(ev, "plan_only:accepted:fee=")
    assert k.started(it).nonce is None and "plan" in k.sdk.calls and "next_index" not in k.sdk.calls
    assert k.sdk.submitted == []
    k.sdk.plan_violations = ["policy: x"]
    reject(run(k.place(buy(lk, lk.TAO // 2, attempt=1))), "plan_only:plan:policy: x")
    status, evs = run(k.venue.resolve(it, k.snap()))
    assert evs == () and status.value == "LANDED"


def test_key_alarm_freezes_all_but_emergency_fill_or_kill_sells(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    k.feed(ReconAdjusted(lk.BOOK, Block(lk.B0), 0, 0, (), "key_alarm:proxies_changed"))
    assert k.venue.frozen
    reject(run(k.place(buy(lk, lk.TAO // 2))), "frozen_key_alarm")
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.URGENT))), "frozen_key_alarm")
    reject(run(k.place(lk.intent(OrderKind.REMOVE_STAKE_LIMIT, full=True, urgency=Urgency.EMERGENCY, partial=True))),
           "frozen_key_alarm")
    em = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, alpha_in=1_000 * lk.TAO, urgency=Urgency.EMERGENCY, attempt=1)
    assert isinstance(run(k.place(em)), VenueAck)
    k2 = lk.Kit(risk=RiskCfg(allow_emergency_exits_when_frozen=False))
    k2.hold(lk.SA, lk.SN, 5_000 * lk.TAO)
    k2.feed(ReconAdjusted(lk.BOOK, Block(lk.B0), 0, 0, (), "key_alarm:x"))
    reject(run(k2.place(em)), "frozen_key_alarm")
    k2.feed(QuarantineCleared(lk.BOOK, Block(lk.B0), "operator"))
    assert not k2.venue.frozen


# ------------------------------------------------------------------------------------------------- delegates
def test_reserve_rotates_and_raises_when_all_delegates_are_busy(lk: ModuleType) -> None:
    k = lk.Kit(live=lk.live_cfg(delegate_wallets=("ops0", "ops1")))
    a = buy(lk, lk.TAO // 4, key=lk.KEY)
    b = buy(lk, lk.TAO // 4, key=lk.KEY2, hotkey=lk.HK_B)
    assert isinstance(run(k.place(a)), VenueAck) and isinstance(run(k.place(b)), VenueAck)
    assert (k.started(a).delegate, k.started(b).delegate) == ("ops0", "ops1")
    with pytest.raises(NoFreeDelegate):
        run(k.venue.reserve(buy(lk, lk.TAO // 4, attempt=9), k.snap()))


def test_one_in_flight_order_per_netuid(lk: ModuleType) -> None:
    k = lk.Kit()
    assert isinstance(run(k.place(buy(lk, lk.TAO // 4))), VenueAck)
    reject(run(k.place(buy(lk, lk.TAO // 4, attempt=1))), "inflight_netuid")
    assert len(k.sdk.submitted) == 1


def test_reserve_skips_a_delegate_whose_nonce_moved_unexpectedly(lk: ModuleType) -> None:
    k = lk.Kit()
    it = buy(lk, lk.TAO // 2)
    ack = run(k.place(it))
    k.land_buy(ack, it)
    k.set_head(lk.B0 + 2)
    run(k.drain(lk.B0 + 2))
    k.sdk.settle_pool(lk.D0)
    assert k.venue.delegates.expected_nonce("ops0") == lk.START_NONCE + 2
    k.sdk.set_account(lk.B0 + 2, lk.D0, nonce=lk.START_NONCE + 3)            # someone else used the ops0 key
    d, n, _ = run(k.venue.reserve(buy(lk, lk.TAO // 2, attempt=1), k.snap()))
    assert d == "ops1" and n == lk.START_NONCE


def test_a_read_error_before_the_send_is_a_reject_not_an_unknown(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.fail["quote"] = ConnectionError("head node gone")
    it = buy(lk, lk.TAO // 2)
    reject(run(k.place(it)), "pre_submit_error:ConnectionError")
    assert k.sdk.submitted == [] and k.subs.get(it.order_id, 0) is None
    assert "ops0" in k.venue.delegates.free(lk.B0)                          # freed at once (nonce never used)
