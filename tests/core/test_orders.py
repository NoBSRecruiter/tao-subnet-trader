"""core.orders: the order FSM (incl. the illegal SUBMITTING -> FILLED/EXPIRED), intent validation, deterministic ids."""
from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from taotrader.core.orders import (
    TERMINAL,
    IllegalTransition,
    OrderIntent,
    OrderKind,
    OrderRecord,
    OrderState,
    Urgency,
    make_order_id,
)
from taotrader.core.units import PPM, AlphaRao, Block, BookId, Hotkey, NetUid, Ppm, PriceRao, Rao, StrategyId, SubnetKey

KEY = SubnetKey(NetUid(92), Block(8_355_590))
HK = Hotkey("0x" + "e2" * 32)
S = OrderState
LEGAL = {
    (S.INTENDED, S.SUBMITTING), (S.INTENDED, S.CANCELLED),
    (S.SUBMITTING, S.SUBMITTED), (S.SUBMITTING, S.FAILED), (S.SUBMITTING, S.UNKNOWN),
    (S.UNKNOWN, S.SUBMITTED), (S.UNKNOWN, S.FILLED), (S.UNKNOWN, S.FAILED), (S.UNKNOWN, S.EXPIRED),
    (S.SUBMITTED, S.FILLED), (S.SUBMITTED, S.FAILED), (S.SUBMITTED, S.EXPIRED),
}


def intent(kind: OrderKind = OrderKind.ADD_STAKE_LIMIT, **kw: object) -> OrderIntent:
    base = {"order_id": make_order_id("run", BookId("b1"), Block(1), KEY, HK, kind, 0), "attempt": 0, "book": BookId("b1"),
                "created_block": Block(1), "kind": kind, "key": KEY, "hotkey": HK, "tao_in": Rao(10**9), "alpha_in": AlphaRao(0),
                "full_position": False, "limit_price": PriceRao(1_400_000), "allow_partial": False, "shielded": True,
                "valid_until": Block(6), "expected_out": 7 * 10**11, "urgency": Urgency.NORMAL,
                "attribution": ((StrategyId("carry"), Ppm(PPM)),), "reason": "entry"}
    base.update(kw)
    return OrderIntent(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(("src", "dst"), list(product(list(OrderState), list(OrderState))))
def test_fsm_transition_table(src: OrderState, dst: OrderState) -> None:
    rec = OrderRecord(intent(), state=src)
    if (src, dst) in LEGAL:
        assert rec.to(dst).state is dst
    else:
        with pytest.raises(IllegalTransition):
            rec.to(dst)


def test_submitting_never_goes_straight_to_filled_or_expired() -> None:
    rec = OrderRecord(intent()).to(S.SUBMITTING)
    for dst in (S.FILLED, S.EXPIRED):
        with pytest.raises(IllegalTransition, match="SUBMITTING -> "):
            rec.to(dst)
    # the recovery path: SubmitUnknown("recovered_submitting") first, then the resolved outcome
    assert rec.to(S.UNKNOWN).to(S.FILLED).state is S.FILLED
    assert rec.to(S.UNKNOWN).to(S.EXPIRED).state is S.EXPIRED
    assert rec.to(S.UNKNOWN).to(S.SUBMITTED).to(S.FILLED).state is S.FILLED


def test_terminal_states_are_final() -> None:
    assert frozenset({S.FILLED, S.FAILED, S.EXPIRED, S.CANCELLED}) == TERMINAL
    for t in TERMINAL:
        for dst in OrderState:
            with pytest.raises(IllegalTransition):
                OrderRecord(intent(), state=t).to(dst)


def test_intent_validation() -> None:
    intent()                                                     # valid buy
    with pytest.raises(ValueError, match="attribution"):
        intent(attribution=((StrategyId("carry"), Ppm(999_999)),))
    with pytest.raises(ValueError, match="ADD_STAKE_LIMIT"):
        intent(tao_in=Rao(0))
    with pytest.raises(ValueError, match="ADD_STAKE_LIMIT"):
        intent(alpha_in=AlphaRao(1))
    with pytest.raises(ValueError, match="ADD_STAKE_LIMIT"):
        intent(full_position=True)
    sell = {"tao_in": Rao(0), "alpha_in": AlphaRao(5)}
    intent(OrderKind.REMOVE_STAKE_LIMIT, **sell)
    intent(OrderKind.REMOVE_STAKE_FULL_LIMIT, tao_in=Rao(0), alpha_in=AlphaRao(0), full_position=True)
    with pytest.raises(ValueError, match="REMOVE"):
        intent(OrderKind.REMOVE_STAKE_LIMIT, tao_in=Rao(1), alpha_in=AlphaRao(5))
    with pytest.raises(ValueError, match="REMOVE"):
        intent(OrderKind.REMOVE_STAKE_LIMIT, tao_in=Rao(0), alpha_in=AlphaRao(0))
    with pytest.raises(ValueError, match="destination"):
        intent(OrderKind.MOVE_STAKE, **sell)
    intent(OrderKind.MOVE_STAKE, dest_hotkey=Hotkey("0x" + "56" * 32), **sell)
    with pytest.raises(ValueError, match="destination"):
        intent(OrderKind.MOVE_STAKE_LIMIT, dest_hotkey=Hotkey("0x" + "56" * 32), **sell)
    intent(OrderKind.MOVE_STAKE_LIMIT, dest_hotkey=Hotkey("0x" + "56" * 32), dest_key=KEY, **sell)


def test_make_order_id_is_stable_and_field_sensitive() -> None:
    oid = make_order_id("run-1", BookId("b1"), Block(9_240_388), KEY, HK, OrderKind.ADD_STAKE_LIMIT, 0)
    assert oid == "73540ab77a9043ccfa774978"                    # regression vector: blake2b-96 of the canonical text
    assert len(oid) == 24 and int(oid, 16) >= 0
    variants = {
        make_order_id("run-2", BookId("b1"), Block(9_240_388), KEY, HK, OrderKind.ADD_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b2"), Block(9_240_388), KEY, HK, OrderKind.ADD_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b1"), Block(9_240_389), KEY, HK, OrderKind.ADD_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b1"), Block(9_240_388), replace(KEY, reg_at=Block(1)), HK, OrderKind.ADD_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b1"), Block(9_240_388), KEY, Hotkey("0x" + "00" * 32), OrderKind.ADD_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b1"), Block(9_240_388), KEY, HK, OrderKind.REMOVE_STAKE_LIMIT, 0),
        make_order_id("run-1", BookId("b1"), Block(9_240_388), KEY, HK, OrderKind.ADD_STAKE_LIMIT, 1),
    }
    assert oid not in variants and len(variants) == 7


def test_urgency_order() -> None:
    assert Urgency.EMERGENCY > Urgency.URGENT > Urgency.HIGH > Urgency.NORMAL > Urgency.LOW
