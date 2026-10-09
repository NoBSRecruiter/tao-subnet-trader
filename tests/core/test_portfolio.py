"""core.portfolio: ledger transactions balance per unit; check_invariants detects each of its five classes."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.core.events import CapitalChanged, CarrierFeeSettled, DeregSettled, OrderFailed, YieldAccrued
from taotrader.core.fixed import EXACT
from taotrader.core.orders import FailReason, Fill, OrderKind
from taotrader.core.portfolio import (
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
    market_account,
    parse_pos_account,
    pos_account,
    yield_txn,
)
from taotrader.core.units import (
    AlphaRao,
    Block,
    BookId,
    Hotkey,
    NetUid,
    OrderId,
    PositionKey,
    Ppm,
    PriceRao,
    Rao,
    StrategyId,
    SubnetKey,
)

KEY = SubnetKey(NetUid(92), Block(8_355_590))
HK = Hotkey("0x" + "e2" * 32)
HK2 = Hotkey("0x" + "56" * 32)
BOOK = BookId("b1")
CARRY, MOM = StrategyId("carry"), StrategyId("momentum")
BUY_ALPHA = 7_289_425_629_146


def _fill(kind: OrderKind, **kw: object) -> Fill:
    base = Fill(fill_id="o1:0:0", order_id=OrderId("o1"), attempt=0, book=BOOK, block=Block(9_240_390), kind=kind,
                key=KEY, hotkey=HK, tao=Rao(10 * 10**9), alpha=AlphaRao(BUY_ALPHA), shares=Decimal(BUY_ALPHA),
                swap_fee=5_035_477, author_fee_tao=Rao(0), tx_fee=Rao(1_028_000), d_pool_tao=9_994_964_523,
                d_pool_alpha=-BUY_ALPHA, spot_before=PriceRao(1_348_210), shortfall_ppm=Ppm(18_000), complete=True)
    return replace(base, **kw)  # type: ignore[arg-type]


def _unit_sums(txn: LedgerTxn) -> dict[str, int]:
    sums: dict[str, int] = {}
    for p in txn.postings:
        sums[p.unit] = sums.get(p.unit, 0) + p.amount
    return sums


# ------------------------------------------------------------------------------------------------- transactions
@pytest.mark.parametrize("txn", [
    fill_txn(_fill(OrderKind.ADD_STAKE_LIMIT)),
    fill_txn(_fill(OrderKind.REMOVE_STAKE_LIMIT, tao=Rao(9_000_000_000), author_fee_tao=Rao(4_500_000),
                   swap_fee=3_000_000, tx_fee=Rao(837_000))),
    fill_txn(_fill(OrderKind.REMOVE_STAKE_FULL_LIMIT, tao=Rao(9_000_000_000), author_fee_tao=Rao(4_500_000))),
    fill_txn(_fill(OrderKind.MOVE_STAKE, tao=Rao(0), swap_fee=0, dest_hotkey=HK2, dest_shares=Decimal(5))),
    fill_txn(_fill(OrderKind.ADD_STAKE_LIMIT, tx_fee=Rao(0))),
    yield_txn(YieldAccrued(BOOK, KEY, HK, Block(9_240_582), Decimal("1.0"), Decimal("1.000279"), 2_034_000)),
    yield_txn(YieldAccrued(BOOK, KEY, HK, Block(9_240_582), Decimal("1.0"), Decimal("1.0"), -1)),
    dereg_txn(DeregSettled(BOOK, KEY, HK, Block(9_300_000), AlphaRao(BUY_ALPHA), Rao(3_600_000_000), "formula")),
    dereg_txn(DeregSettled(BOOK, KEY, HK, Block(9_300_000), AlphaRao(BUY_ALPHA), Rao(0), "observed"), pos_alpha=BUY_ALPHA + 1),
    fail_txn(OrderFailed(BOOK, OrderId("o2"), 1, Block(9_240_400), FailReason.SLIPPAGE_TOO_HIGH, Rao(933_081))),
    fail_txn(OrderFailed(BOOK, OrderId("o3"), 0, Block(9_240_400), FailReason.SHIELD_MISSED, Rao(0), expired=True)),
    carrier_fee_txn(CarrierFeeSettled(BOOK, OrderId("o3"), 0, Block(9_240_420), Rao(98_000), "carrier_only")),
    capital_txn(CapitalChanged(BOOK, Block(1), 100 * 10**9, 10**9, "seed")),
    capital_txn(CapitalChanged(BOOK, Block(2), -5 * 10**9, 0, "withdraw")),
])
def test_every_ledger_txn_balances_per_unit(txn: LedgerTxn) -> None:
    txn.validate()
    assert all(v == 0 for v in _unit_sums(txn).values())


def test_txn_ids_and_postings() -> None:
    assert fill_txn(_fill(OrderKind.ADD_STAKE_LIMIT)).txn_id == "fill:o1:0:0"
    y = yield_txn(YieldAccrued(BOOK, KEY, HK, Block(7), Decimal(1), Decimal(2), 5))
    assert y.txn_id == "yield:b1:92:8355590:7"
    assert Posting(pos_account(KEY, HK), alpha_unit(KEY), 5) in y.postings
    d = dereg_txn(DeregSettled(BOOK, KEY, HK, Block(9), AlphaRao(10), Rao(3), "formula"))
    assert d.txn_id == "dereg:b1:92:8355590"
    assert Posting("cash", TAO_UNIT, 3) in d.postings and Posting(pos_account(KEY, HK), alpha_unit(KEY), -10) in d.postings
    f = fail_txn(OrderFailed(BOOK, OrderId("o9"), 2, Block(9), FailReason.OTHER, Rao(7)))
    assert f.txn_id == "fail:o9:2" and Posting("fees:tx", TAO_UNIT, 7) in f.postings
    assert fail_txn(OrderFailed(BOOK, OrderId("o9"), 3, Block(9), FailReason.OTHER, Rao(0))).postings == ()
    c = carrier_fee_txn(CarrierFeeSettled(BOOK, OrderId("o9"), 2, Block(9), Rao(98_000), "carrier_only"))
    assert c.txn_id == "carrier:o9:2" and Posting("fee_float", TAO_UNIT, -98_000) in c.postings
    k = capital_txn(CapitalChanged(BOOK, Block(1), 10, 2, "m"))
    assert k.txn_id == "capital:b1:m" and Posting("equity:capital", TAO_UNIT, -12) in k.postings
    assert yield_txn(YieldAccrued(BOOK, KEY, HK, Block(7), Decimal(1), Decimal(1), 0)).postings == ()


def test_move_stake_limit_has_no_ledger_path_in_v1() -> None:
    with pytest.raises(ValueError, match="disabled"):
        fill_txn(_fill(OrderKind.MOVE_STAKE_LIMIT, dest_key=KEY, dest_hotkey=HK2))


def test_unbalanced_txn_rejected_by_apply() -> None:
    bad = LedgerTxn("x", Block(1), (Posting("cash", TAO_UNIT, 1),))
    with pytest.raises(ValueError, match="unbalanced"):
        apply_txn({}, bad)


def test_pos_account_parse_round_trip() -> None:
    assert parse_pos_account(pos_account(KEY, HK)) == PositionKey(KEY, HK)
    assert parse_pos_account(market_account(KEY)) is None
    assert parse_pos_account("pos:x:1:h") is None


# ------------------------------------------------------------------------------------------------- invariants
def _clean_state() -> tuple[Portfolio, dict[tuple[str, str], int], dict[PositionKey, int]]:
    bal: dict[tuple[str, str], int] = {}
    apply_txn(bal, capital_txn(CapitalChanged(BOOK, Block(1), 100 * 10**9, 10**9, "seed")))
    apply_txn(bal, fill_txn(_fill(OrderKind.ADD_STAKE_LIMIT)))
    pos = Position(KEY, HK, Decimal(BUY_ALPHA), Rao(10 * 10**9 + 1_028_000), Block(9_240_390))
    half = Decimal(BUY_ALPHA) / 2
    pf = Portfolio(cash=Rao(90 * 10**9), fee_float=Rao(10**9 - 1_028_000), positions=(pos,),
                   sleeves=(SleeveHolding(CARRY, KEY, half, Rao(5 * 10**9)), SleeveHolding(MOM, KEY, half, Rao(5 * 10**9))),
                   sleeve_cash=((CARRY, Rao(45 * 10**9)), (MOM, Rao(45 * 10**9))))
    return pf, bal, {pos.pkey: BUY_ALPHA}


def _classes(violations: list[str]) -> set[str]:
    return {v.split(":", 1)[0] for v in violations}


def test_clean_state_has_no_violations() -> None:
    pf, bal, val = _clean_state()
    assert check_invariants(pf, bal, val) == []


def test_inv1_cash_and_fee_float() -> None:
    pf, bal, val = _clean_state()
    assert _classes(check_invariants(replace(pf, cash=Rao(pf.cash + 1)), bal, val)) >= {"inv1"}
    neg = dict(bal)
    neg[("fee_float", TAO_UNIT)] = -5
    neg[("equity:capital", TAO_UNIT)] += bal[("fee_float", TAO_UNIT)] + 5
    out = check_invariants(replace(pf, fee_float=Rao(-5)), neg, val)
    assert any(v.startswith("inv1: fee_float -5 < 0") for v in out)


def test_inv2_position_value_vs_ledger() -> None:
    pf, bal, val = _clean_state()
    pk = pf.positions[0].pkey
    assert check_invariants(pf, bal, {pk: BUY_ALPHA + 2}) == []           # within the 2-rao tolerance
    assert _classes(check_invariants(pf, bal, {pk: BUY_ALPHA + 3})) == {"inv2"}
    assert _classes(check_invariants(pf, bal, {})) == {"inv2"}            # no value_of supplied
    dangling = dict(bal)
    other = pos_account(KEY, HK2)
    dangling[(other, alpha_unit(KEY))] = 50
    dangling[(market_account(KEY), alpha_unit(KEY))] -= 50
    assert _classes(check_invariants(pf, dangling, val)) == {"inv2"}


def test_inv3_sleeves() -> None:
    pf, bal, val = _clean_state()
    drift = replace(pf, sleeves=(pf.sleeves[0], replace(pf.sleeves[1], shares=EXACT.add(pf.sleeves[1].shares, Decimal("1E-40")))))
    assert _classes(check_invariants(drift, bal, val)) == {"inv3"}        # exact Decimal, no tolerance
    cash = replace(pf, sleeve_cash=((CARRY, Rao(45 * 10**9)), (MOM, Rao(45 * 10**9 - 1))))
    assert _classes(check_invariants(cash, bal, val)) == {"inv3"}
    orphan = replace(pf, sleeves=(*pf.sleeves, SleeveHolding(CARRY, SubnetKey(NetUid(5), Block(1)), Decimal(3), Rao(0))))
    assert _classes(check_invariants(orphan, bal, val)) == {"inv3"}


def test_inv3_holds_under_sleeve_transfers() -> None:
    """A SleeveTransfer moves shares from -> to and sleeve cash to -> from with no ledger postings."""
    pf, bal, val = _clean_state()
    moved, paid = pf.sleeves[0].shares / 3, Rao(1_234_567)
    pf2 = replace(pf, sleeves=(replace(pf.sleeves[0], shares=pf.sleeves[0].shares - moved),
                               replace(pf.sleeves[1], shares=pf.sleeves[1].shares + moved)),
                  sleeve_cash=((CARRY, Rao(pf.sleeve_cash[0][1] + paid)), (MOM, Rao(pf.sleeve_cash[1][1] - paid))))
    assert check_invariants(pf2, bal, val) == []


def test_inv4_units_sum_to_zero() -> None:
    pf, bal, val = _clean_state()
    broken = dict(bal)
    broken[("fees:swap", TAO_UNIT)] += 1
    assert _classes(check_invariants(pf, broken, val)) == {"inv4"}


def test_inv5_positions() -> None:
    pf, bal, val = _clean_state()
    empty = replace(pf, positions=(replace(pf.positions[0], shares=Decimal(0)),), sleeves=(),
                    sleeve_cash=((CARRY, Rao(90 * 10**9)),))
    assert "inv5" in _classes(check_invariants(empty, bal, val))
    dup = replace(pf, positions=(pf.positions[0], replace(pf.positions[0], hotkey=HK2)))
    assert "inv5" in _classes(check_invariants(dup, bal, {**val, PositionKey(KEY, HK2): 0}))


@given(st.lists(st.tuples(st.sampled_from(["cap", "buy", "sell", "yield", "fail", "carrier", "dereg"]),
                          st.integers(0, 10**13), st.integers(-(10**12), 10**12)), max_size=30))
def test_random_ledger_sequences_stay_balanced(ops: list[tuple[str, int, int]]) -> None:
    bal: dict[tuple[str, str], int] = {}
    for i, (op, a, s) in enumerate(ops):
        if op == "cap":
            txn = capital_txn(CapitalChanged(BOOK, Block(i), s, a % 10**9, f"m{i}"))
        elif op == "buy":
            txn = fill_txn(_fill(OrderKind.ADD_STAKE_LIMIT, fill_id=f"f{i}", tao=Rao(a + 1), swap_fee=a // 2000,
                                 alpha=AlphaRao(a // 3)))
        elif op == "sell":
            txn = fill_txn(_fill(OrderKind.REMOVE_STAKE_LIMIT, fill_id=f"f{i}", tao=Rao(a), author_fee_tao=Rao(a // 2000),
                                 alpha=AlphaRao(a // 2)))
        elif op == "yield":
            txn = yield_txn(YieldAccrued(BOOK, KEY, HK, Block(i), Decimal(1), Decimal(1), s))
        elif op == "fail":
            txn = fail_txn(OrderFailed(BOOK, OrderId(f"o{i}"), 0, Block(i), FailReason.OTHER, Rao(a % 10**7)))
        elif op == "carrier":
            txn = carrier_fee_txn(CarrierFeeSettled(BOOK, OrderId(f"o{i}"), 0, Block(i), Rao(a % 10**6), "carrier_only"))
        else:
            txn = dereg_txn(DeregSettled(BOOK, KEY, HK, Block(i), AlphaRao(a), Rao(a // 3), "formula"))
        apply_txn(bal, txn)
    units: dict[str, int] = {}
    for (_, unit), amount in bal.items():
        units[unit] = units.get(unit, 0) + amount
    assert all(v == 0 for v in units.values())
