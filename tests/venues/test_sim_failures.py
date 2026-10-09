"""Acceptance (DESIGN.md 8.5, WP6): every failure reason the simulator can produce fires on a constructed case and pays
the right fee: a failed inner call pays the full tx fee, a shield miss the carrier fee, and venue rejects, era
expiry and NOT_PLACED pay nothing. The ledger (fee_float -> fees:tx) and the order FSM are checked for each case.

Not producible by the simulator, by construction:
- SWAP_INPUT_TOO_LARGE: the subtensor pre-check (input <= 1000 x reserve -> InsufficientLiquidity) runs first on the
  same reserve, so the swap pallet's own check can never trip (protocol.amm order, brief 2.4);
- STAKE_UNAVAILABLE, PROXY_ERROR, CALL_FILTERED: live-only chain outcomes (locks, proxy dispatch errors, filters).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest

from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported, OrderFailed, SubmitStarted, SubmitUnknown, VenueAck
from taotrader.core.orders import FailReason, OrderKind, OrderState, Resolution
from taotrader.core.units import Block
from taotrader.venues.sim import SimVenue

TAO = 10**9
CFG = ExecCfg(shield_miss_ppm=0)
BUY_FEE, SELL_FEE, CARRIER = CFG.buy_tx_fee_rao, CFG.sell_tx_fee_rao, CFG.carrier_fee_rao


@dataclass(frozen=True)
class Case:
    reason: FailReason
    fee: int
    expired: bool = False
    final: OrderState = OrderState.FAILED


def _settle_one(h, ev_list) -> OrderFailed:
    (ev,) = ev_list
    assert isinstance(ev, OrderFailed), ev
    return ev


def _check(h, ev: OrderFailed, case: Case, fee_float_before: int, cash_before: int) -> None:
    assert ev.reason is case.reason, (ev.reason, ev.detail)
    assert ev.tx_fee == case.fee and ev.expired is case.expired
    assert h.bal("fee_float") == fee_float_before - case.fee
    assert h.bal("fees:tx") == case.fee
    assert h.venue.cash == cash_before                    # no TAO moved
    h.check_ledger()


# ------------------------------------------------------------------------------------------------- inner-call failures
def _buy_case(consts, harness, snap, buy, *, intent_kw: dict[str, Any], fill_snap: Callable[[], Any] | None = None,
              cfg: ExecCfg = CFG, cash: int = 100 * TAO, tao_in: int = 2 * TAO) -> tuple[Any, OrderFailed]:
    h = harness(SimVenue(consts["BOOK"], cfg, seed=3))
    h.capital(cash)
    h.place(buy(1_000, tao_in, **intent_kw), snap(1_000))
    s5 = fill_snap() if fill_snap is not None else snap(1_005)
    return h, _settle_one(h, h.tick(s5))


def test_subnet_not_exists_when_the_generation_is_gone(consts, harness, snap, subnet, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, fill_snap=lambda: snap(1_005, subnet(reg_at=1_004)))
    _check(h, ev, Case(FailReason.SUBNET_NOT_EXISTS, BUY_FEE), TAO, 100 * TAO)
    assert ev.detail == "pool_gone"
    h2, ev2 = _buy_case(consts, harness, snap, buy, intent_kw={},
                        fill_snap=lambda: snap(1_005, subnet(netuid=8, reg_at=500)))      # netuid absent
    _check(h2, ev2, Case(FailReason.SUBNET_NOT_EXISTS, BUY_FEE), TAO, 100 * TAO)


def test_subtoken_disabled_blocks_buys(consts, harness, snap, subnet, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={},
                      fill_snap=lambda: snap(1_005, subnet(subtoken_enabled=False)))
    _check(h, ev, Case(FailReason.SUBTOKEN_DISABLED, BUY_FEE), TAO, 100 * TAO)


def test_price_limit_exceeded_is_strict_for_buys(consts, harness, snap, pool, buy) -> None:
    spot = pool().spot_rao()                               # 10,000,000 rao/alpha exactly
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={"limit": spot})
    _check(h, ev, Case(FailReason.PRICE_LIMIT_EXCEEDED, BUY_FEE), TAO, 100 * TAO)
    h2, ev2 = _buy_case(consts, harness, snap, buy, intent_kw={"limit": spot, "allow_partial": True})
    _check(h2, ev2, Case(FailReason.PRICE_LIMIT_EXCEEDED, BUY_FEE), TAO, 100 * TAO)   # even with allow_partial


def test_slippage_too_high_for_fill_or_kill_buys(consts, harness, snap, pool, buy) -> None:
    limit = pool().spot_rao() * 1_001 // 1_000             # +0.1%: ~0.5 TAO of room
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={"limit": limit}, tao_in=5 * TAO)
    _check(h, ev, Case(FailReason.SLIPPAGE_TOO_HIGH, BUY_FEE), TAO, 100 * TAO)


def test_amount_too_low(consts, harness, snap, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, tao_in=1_000_000)       # 0.001 TAO < 0.002 + fee
    _check(h, ev, Case(FailReason.AMOUNT_TOO_LOW, BUY_FEE), TAO, 100 * TAO)


def test_insufficient_liquidity(consts, harness, snap, subnet, pool, buy) -> None:
    thin = pool(tao=1_500_000, alpha=150_000_000)          # 0.0015 TAO: input > 1000 x reserve for a 2-TAO buy
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={"limit": thin.spot_rao() * 10**9},
                      fill_snap=lambda: snap(1_005, subnet(thin)))
    _check(h, ev, Case(FailReason.INSUFFICIENT_LIQUIDITY, BUY_FEE), TAO, 100 * TAO)


def test_reserves_too_low(consts, harness, snap, subnet, pool, buy) -> None:
    drained = pool(tao=1_000 * TAO, alpha=500_000)        # alpha reserve below MinimumReserve 1,000,000
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={"limit": drained.spot_rao() * 10},
                      fill_snap=lambda: snap(1_005, subnet(drained)))
    _check(h, ev, Case(FailReason.RESERVES_TOO_LOW, BUY_FEE), TAO, 100 * TAO)


def test_not_enough_balance_to_stake(consts, harness, snap, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, cash=TAO, tao_in=2 * TAO)
    _check(h, ev, Case(FailReason.OTHER, BUY_FEE), TAO, TAO)
    assert ev.detail.startswith("NotEnoughBalanceToStake")


def test_safe_mode_filters_staking(consts, harness, snap, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, fill_snap=lambda: snap(1_005, safe_mode_until=Block(2_000)))
    _check(h, ev, Case(FailReason.SAFE_MODE, BUY_FEE), TAO, 100 * TAO)


def test_untracked_hotkey_is_a_fee_paying_sim_failure(consts, harness, snap, subnet, hk_idx, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={},
                      fill_snap=lambda: snap(1_005, subnet(hotkeys=(hk_idx(consts["HK_B"]),))))
    _check(h, ev, Case(FailReason.OTHER, BUY_FEE), TAO, 100 * TAO)
    assert ev.detail.startswith("sim:hotkey_untracked")


def test_injected_inner_failure_pays_the_fee(consts, harness, snap, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, cfg=ExecCfg(shield_miss_ppm=0, fail_inject_ppm=1_000_000))
    _check(h, ev, Case(FailReason.OTHER, BUY_FEE), TAO, 100 * TAO)
    assert ev.detail == "injected inner-call failure"


def test_sell_failures(consts, harness, snap, pool, buy, sell) -> None:
    c = consts
    # no position at all
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(sell(1_000, 5 * TAO), snap(1_000))
    _check(h, _settle_one(h, h.tick(snap(1_005))), Case(FailReason.NOT_ENOUGH_STAKE, SELL_FEE), TAO, 100 * TAO)
    # more than held, crossed limit, fill-or-kill above max_sell_to_limit, partial output below 0.002 TAO
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(buy(1_000, 20 * TAO), snap(1_000))
    (fb,) = h.tick(snap(1_005))
    held = fb.fill.alpha
    spot = pool().spot_rao()
    float0 = h.bal("fee_float")
    cash0 = h.venue.cash
    cases = [
        (sell(1_100, held + 1, limit=1), FailReason.NOT_ENOUGH_STAKE),
        (sell(1_200, held // 2, limit=spot), FailReason.PRICE_LIMIT_EXCEEDED),
        (sell(1_300, full=True, limit=spot * 999 // 1_000), FailReason.SLIPPAGE_TOO_HIGH),
        (sell(1_400, 100_000_000, limit=1), FailReason.AMOUNT_TOO_LOW),      # 0.1 alpha ~ 0.001 TAO, position remains
    ]
    for i, (it, reason) in enumerate(cases):
        h.place(it, snap(int(it.created_block)))
        ev = _settle_one(h, h.tick(snap(int(it.created_block) + 5)))
        assert ev.reason is reason and ev.tx_fee == SELL_FEE, (reason, ev)
        assert h.bal("fee_float") == float0 - (i + 1) * SELL_FEE and h.venue.cash == cash0
    assert h.venue.shares(c["KEY"], c["HK_A"]) == fb.fill.shares
    h.check_ledger()


def test_sell_reserves_too_low(consts, harness, snap, subnet, pool, buy, sell) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO), snap(1_000))
    h.tick(snap(1_005))
    drained = subnet(pool(tao=900_000, alpha=10**12))     # TAO reserve below MinimumReserve
    h.place(sell(1_060, full=True), snap(1_060))
    ev = _settle_one(h, h.tick(snap(1_065, drained)))
    assert ev.reason is FailReason.RESERVES_TOO_LOW and ev.tx_fee == SELL_FEE


def test_move_failures(consts, harness, snap, buy, move) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    h.place(move(1_000), snap(1_000))                     # nothing to move
    ev = _settle_one(h, h.tick(snap(1_005)))
    assert ev.reason is FailReason.NOT_ENOUGH_STAKE and ev.tx_fee == CFG.move_tx_fee_rao
    h.place(buy(1_010, 2 * TAO), snap(1_010))
    h.tick(snap(1_015))
    h.place(move(1_020, full=False, alpha_in=100_000_000), snap(1_020))   # 0.1 alpha ~ 0.001 TAO < DefaultMinStake
    ev = _settle_one(h, h.tick(snap(1_025)))
    assert ev.reason is FailReason.AMOUNT_TOO_LOW and ev.tx_fee == CFG.move_tx_fee_rao
    h.check_ledger()


# ------------------------------------------------------------------------------------------------- shield / era / venue
def test_injected_shield_miss_pays_the_carrier_fee_and_expires(consts, harness, snap, buy) -> None:
    h, ev = _buy_case(consts, harness, snap, buy, intent_kw={}, cfg=ExecCfg(shield_miss_ppm=1_000_000))
    _check(h, ev, Case(FailReason.SHIELD_MISSED, CARRIER, expired=True), TAO, 100 * TAO)
    assert ev.block == 1_005 and ev.exact_block
    assert h.state(h.journal[1].intent) is OrderState.EXPIRED


def test_unshielded_orders_are_never_shield_missed(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=1_000_000)))
    h.capital(100 * TAO)
    ack = h.place(buy(1_000, 2 * TAO, shielded=False), snap(1_000))
    assert isinstance(ack, VenueAck) and ack.expected_fill_block == 1_004          # submit head + 1
    (ev,) = h.tick(snap(1_004))
    assert isinstance(ev, FillReported)


def test_era_expired_in_exact_mode(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG, exact_fills=True))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO, shielded=False), snap(1_000))   # era_end = 1,000 + 16 + 2
    (ev,) = h.tick(snap(1_030))
    _check(h, ev, Case(FailReason.ERA_EXPIRED, 0, expired=True), TAO, 100 * TAO)
    assert not ev.exact_block


def test_exact_mode_shielded_late_view_is_a_miss(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG, exact_fills=True))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO), snap(1_000))
    (ev,) = h.tick(snap(1_007))                           # N+2 = 1,005 was never observed: never legal later
    _check(h, ev, Case(FailReason.SHIELD_MISSED, CARRIER, expired=True), TAO, 100 * TAO)
    assert ev.detail.startswith("fill_state_unavailable")


def test_exact_mode_unshielded_inside_its_era_lands_late(consts, harness, snap, buy) -> None:
    c = consts
    h = harness(SimVenue(c["BOOK"], CFG, exact_fills=True))
    h.capital(100 * TAO)
    h.place(buy(1_000, 2 * TAO, shielded=False), snap(1_000))
    (ev,) = h.tick(snap(1_010))                           # inside (submit 1,003, era_end 1,018]
    assert isinstance(ev, FillReported) and ev.fill.block == 1_010 and ev.fill.exact_block


@pytest.mark.parametrize("variant", ["rotation", "ttl", "inflight", "same_hotkey"])
def test_venue_rejects_pay_nothing(consts, harness, snap, subnet, buy, move, variant) -> None:
    c = consts
    from taotrader.core.orders import OrderIntent
    h = harness(SimVenue(c["BOOK"], CFG))
    h.capital(100 * TAO)
    it: OrderIntent
    if variant == "rotation":
        base = buy(1_000, TAO)
        it = replace(base, kind=OrderKind.MOVE_STAKE_LIMIT, tao_in=0, alpha_in=TAO, dest_key=c["KEY"],
                     dest_hotkey=c["HK_B"])
    elif variant == "ttl":
        it = buy(990, TAO)                                # valid_until 995; submitted at 1,000
    elif variant == "inflight":
        h.place(buy(1_000, TAO), snap(1_000))
        it = buy(1_000, TAO, hotkey=c["HK_B"])
    else:
        it = move(1_000, c["HK_A"])
    ev = h.place(it, snap(1_000))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.VENUE_REJECT and ev.tx_fee == 0
    assert not ev.expired and ev.block == 1_000 and h.state(it) is OrderState.FAILED
    assert h.venue.delegates_free(Block(1_000)) == (("sim1", "sim2") if variant == "inflight" else ("sim0", "sim1", "sim2"))
    h.check_ledger()


def test_not_placed_when_resolve_finds_no_submission(consts, harness, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], CFG)
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(1_000, TAO)
    from taotrader.core.events import OrderIntended
    h.commit(OrderIntended(it))
    res, evs = arun(v.resolve(it, snap(1_000)))
    assert res is Resolution.NOT_PLACED
    (ev,) = evs
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.NOT_PLACED and ev.tx_fee == 0


def test_failure_after_crash_resolve_still_pays_the_fee(consts, harness, snap, buy, arun) -> None:
    """SUBMITTING -> crash -> SubmitUnknown -> resolve(PLACED + ack) -> the inner fails at N+2 with the fee."""
    c = consts
    v = SimVenue(c["BOOK"], CFG)
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(1_000, TAO, limit=1)
    from taotrader.core.events import OrderIntended
    h.commit(OrderIntended(it))
    d, n, era = arun(v.reserve(it, snap(1_000)))
    h.commit(SubmitStarted(book=c["BOOK"], order_id=it.order_id, attempt=0, delegate=d, nonce=n, era_end=era))
    h.commit(SubmitUnknown(book=c["BOOK"], order_id=it.order_id, attempt=0, detail="recovered_submitting"))
    res, evs = arun(v.resolve(it, snap(1_000)))
    assert res is Resolution.PLACED
    for e in evs:
        h.commit(e)
    ev = _settle_one(h, h.tick(snap(1_005)))
    assert ev.reason is FailReason.PRICE_LIMIT_EXCEEDED and ev.tx_fee == BUY_FEE
    assert h.state(it) is OrderState.FAILED
