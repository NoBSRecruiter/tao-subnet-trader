"""engine.reducer: the only state transition (DESIGN.md 4.3, 4.5, 5.7, 5.10, 10.2 reducer cases)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core import codec
from taotrader.core.config import SleeveCfg
from taotrader.core.events import (
    CapitalChanged,
    CarrierFeeSettled,
    ChainEvent,
    ChainEventKind,
    ChainEventObserved,
    DecisionTrace,
    DeregSettled,
    FillReported,
    HealthObs,
    ModelDriftObserved,
    OperatorCommand,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    QuarantineCleared,
    ReconAdjusted,
    SleeveTransfer,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
    YieldAccrued,
)
from taotrader.core.orders import FailReason, OrderKind, OrderState, Urgency
from taotrader.core.portfolio import TAO_UNIT, alpha_unit
from taotrader.core.protocols import RouterState
from taotrader.core.signals import RiskAction, Signal, SignalKind
from taotrader.core.state import ReadPlan
from taotrader.core.units import (
    Block,
    BlockHash,
    BookId,
    Mode,
    NetUid,
    PositionKey,
    Ppm,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.data.journal import DuplicateIdempotencyKey, SqliteJournal
from taotrader.engine.recovery import decode_state, encode_state
from taotrader.engine.reducer import (
    BOOK_SLEEVE,
    ENGINE_FORCED_EXIT,
    ENGINE_ORDER_SPOT,
    RECON_ACCOUNT,
    ROUTER_MEMORY_ID,
    BookSpec,
    EngineState,
    book_view,
    check_state,
    fold_batch,
    initial_state,
    ledger_balance,
    money_digest,
    reduce,
)

B0 = 9_000_000
TAO = 10**9
CARRY = StrategyId("carry")
MOM = StrategyId("momentum")


def obs(block: int) -> SnapshotObserved:
    return SnapshotObserved(Block(block), BlockHash("0x" + f"{block:064x}"), f"dig{block}", ReadPlan.FULL, 0, HealthObs.nominal())


def spec(fx, sleeves=None, mode: RunMode = RunMode.BACKTEST) -> BookSpec:
    return BookSpec.from_cfg(fx.book_cfg(sleeves=sleeves), mode)


def funded(fx, sleeves=None, cash: int = 100 * TAO, mode: RunMode = RunMode.BACKTEST) -> EngineState:
    st = initial_state(spec(fx, sleeves, mode))
    return fold_batch(st, [obs(B0), CapitalChanged(BookId("b1"), Block(B0), cash, TAO, "initial")])


def place(st: EngineState, i, *, delegate: str = "sim0", era_end: int | None = None) -> EngineState:
    b = int(i.created_block)
    return fold_batch(st, [OrderIntended(i), SubmitStarted(i.book, i.order_id, i.attempt, delegate, None,
                                                           Block(era_end if era_end is not None else b + 12)),
                           VenueAck(i.book, i.order_id, i.attempt, Block(b + 3), Block(b + 5), "", "")])


def buy(fx, st: EngineState, block: int = B0, *, key=None, tao: int = 10 * TAO, alpha: int = 990 * TAO,
        attribution=None, hotkey=None):
    key = key if key is not None else fx.K7
    kw = {"attribution": attribution} if attribution is not None else {}
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, block, key, hotkey or fx.HK_A, tao_in=tao, **kw)
    st = place(st, i)
    f = fx.mk_fill(i, block + 5, tao=tao, alpha=alpha, swap_fee=5_035_477, tx_fee=1_028_000)
    return fold_batch(st, [FillReported(f)]), i


def order_state(st: EngineState, i) -> OrderState:
    return next(o.state for o in st.orders if o.intent.order_id == i.order_id)


# ------------------------------------------------------------------------------------------------- capital & sleeves
def test_capital_splits_cash_over_sleeves_by_budget(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(600_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(200_000))]
    st = funded(fx, sleeves, cash=100 * TAO + 1)
    p = st.portfolio
    assert p.cash == 100 * TAO + 1 and p.fee_float == TAO
    assert dict(p.sleeve_cash) == {CARRY: 75 * TAO + 1, MOM: 25 * TAO}     # remainder to the largest budget
    assert ledger_balance(st, "cash", TAO_UNIT) == p.cash
    assert ledger_balance(st, "equity:capital", TAO_UNIT) == -(p.cash + p.fee_float)
    assert st.funded and check_state(st) == [] and st.breaches == ()


def test_capital_without_sleeves_uses_the_book_pseudo_sleeve(fx) -> None:
    st = funded(fx, sleeves=[])
    assert dict(st.portfolio.sleeve_cash) == {BOOK_SLEEVE: 100 * TAO}
    assert check_state(st) == []


# ------------------------------------------------------------------------------------------------- fills
def test_buy_fill_posts_ledger_portfolio_and_sleeves(fx) -> None:
    st, i = buy(fx, funded(fx))
    p = st.portfolio
    assert order_state(st, i) is OrderState.FILLED
    assert p.cash == 90 * TAO and p.fee_float == TAO - 1_028_000
    (pos,) = p.positions
    assert pos.key == fx.K7 and pos.shares == 990 * TAO and pos.cost_tao == 10 * TAO + 1_028_000 and pos.opened_block == B0 + 5
    assert fx.position_alpha(st, fx.K7, fx.HK_A) == 990 * TAO
    assert ledger_balance(st, "fees:swap", TAO_UNIT) == 5_035_477 and ledger_balance(st, "fees:tx", TAO_UNIT) == 1_028_000
    (h,) = p.sleeves
    assert (h.strategy, h.key, h.shares) == (CARRY, fx.K7, Decimal(990 * TAO))
    assert dict(p.sleeve_cash) == {CARRY: 90 * TAO}
    assert check_state(st) == [] and st.orphans == 0
    assert [f.fill_id for f in st.fills] == [f"{i.order_id}:0:0"]


def test_full_sell_closes_the_position_and_credits_the_sleeve(fx) -> None:
    st, _ = buy(fx, funded(fx))
    s = fx.mk_intent(OrderKind.REMOVE_STAKE_LIMIT, B0 + 60, full=True)
    st = place(st, s)
    st = fold_batch(st, [FillReported(fx.mk_fill(s, B0 + 65, tao=9 * TAO, alpha=990 * TAO, author_fee=4_000_000,
                                                 tx_fee=837_000))])
    p = st.portfolio
    assert p.positions == () and p.sleeves == ()
    assert p.cash == 99 * TAO and dict(p.sleeve_cash) == {CARRY: 99 * TAO}
    assert fx.position_alpha(st, fx.K7, fx.HK_A) == 0
    assert ledger_balance(st, f"market:{fx.K7.netuid}:{fx.K7.reg_at}", TAO_UNIT) == 10 * TAO - 5_035_477 - 9 * TAO - 4_000_000
    assert check_state(st) == []


def test_attribution_splits_shares_exactly_and_sells_take_from_the_attributed_sleeve(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    st, _ = buy(fx, funded(fx, sleeves), alpha=1_000 * TAO + 7, attribution=((CARRY, Ppm(700_000)), (MOM, Ppm(300_000))))
    hold = {h.strategy: h.shares for h in st.portfolio.sleeves}
    assert hold[CARRY] + hold[MOM] == Decimal(1_000 * TAO + 7)
    assert hold[MOM] == Decimal(300 * TAO) + Decimal("2.1")
    s = fx.mk_intent(OrderKind.REMOVE_STAKE_LIMIT, B0 + 60, alpha_in=200 * TAO, attribution=((MOM, Ppm(1_000_000)),))
    st = place(st, s)
    st = fold_batch(st, [FillReported(fx.mk_fill(s, B0 + 65, tao=2 * TAO, alpha=200 * TAO))])
    hold2 = {h.strategy: h.shares for h in st.portfolio.sleeves}
    assert hold2[CARRY] == hold[CARRY]                                   # momentum's sale never touches carry
    assert hold2[MOM] == hold[MOM] - 200 * TAO
    assert check_state(st) == []


def test_sell_beyond_the_attributed_holding_takes_the_rest_pro_rata(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    st, _ = buy(fx, funded(fx, sleeves), alpha=1_000 * TAO, attribution=((CARRY, Ppm(500_000)), (MOM, Ppm(500_000))))
    s = fx.mk_intent(OrderKind.REMOVE_STAKE_LIMIT, B0 + 60, alpha_in=700 * TAO, attribution=((MOM, Ppm(1_000_000)),))
    st = place(st, s)
    st = fold_batch(st, [FillReported(fx.mk_fill(s, B0 + 65, tao=7 * TAO, alpha=700 * TAO))])
    hold = {h.strategy: h.shares for h in st.portfolio.sleeves}
    assert hold == {CARRY: Decimal(300 * TAO)}                           # MOM gave all 500, CARRY the other 200
    assert check_state(st) == []


def test_move_stake_rehomes_the_position_and_rescales_sleeves(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    st, _ = buy(fx, funded(fx, sleeves), alpha=1_000 * TAO, attribution=((CARRY, Ppm(600_000)), (MOM, Ppm(400_000))))
    m = fx.mk_intent(OrderKind.MOVE_STAKE, B0 + 60, full=True, dest_hotkey=fx.HK_B)
    st = place(st, m)
    st = fold_batch(st, [FillReported(fx.mk_fill(m, B0 + 65, alpha=1_000 * TAO, shares=Decimal(1_000 * TAO),
                                                 dest_shares=Decimal(800 * TAO), tx_fee=1_000_000))])
    (pos,) = st.portfolio.positions
    assert pos.hotkey == fx.HK_B and pos.shares == 800 * TAO
    assert fx.position_alpha(st, fx.K7, fx.HK_B) == 1_000 * TAO and fx.position_alpha(st, fx.K7, fx.HK_A) == 0
    hold = {h.strategy: h.shares for h in st.portfolio.sleeves}
    assert hold[CARRY] + hold[MOM] == Decimal(800 * TAO) and hold[CARRY] == Decimal(480 * TAO)
    assert check_state(st) == []                                          # dest mark = 1,000 / 800 alpha per share


def test_yield_accrued_updates_the_ledger_and_the_mark(fx) -> None:
    st, _ = buy(fx, funded(fx), alpha=1_000 * TAO)
    y = YieldAccrued(BookId("b1"), fx.K7, fx.HK_A, Block(B0 + 360), Decimal(1), Decimal("1.0003"), 300_000_000)
    st = fold_batch(st, [obs(B0 + 360), y])
    assert fx.position_alpha(st, fx.K7, fx.HK_A) == 1_000_300_000_000
    assert ledger_balance(st, "income:yield", alpha_unit(fx.K7)) == -300_000_000
    assert dict(st.marks)[PositionKey(fx.K7, fx.HK_A)] == Decimal("1.0003")
    assert check_state(st) == []
    assert st.portfolio.positions[0].shares == 1_000 * TAO              # yield never changes the share count


# ------------------------------------------------------------------------------------------------- FSM and orphans
def test_orphan_facts_are_quarantined_without_raising(fx) -> None:
    st, i = buy(fx, funded(fx))
    money = (st.portfolio, st.ledger)
    ghost = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + 120, fx.K9, tao_in=TAO)
    facts = [
        FillReported(fx.mk_fill(ghost, B0 + 125, tao=TAO, alpha=99 * TAO)),          # unknown order
        OrderFailed(BookId("b1"), ghost.order_id, 0, Block(B0 + 125), FailReason.OTHER, Rao(1_000)),
        CarrierFeeSettled(BookId("b1"), i.order_id, 0, Block(B0 + 130), Rao(98_000), "carrier_only"),   # FILLED order
        FillReported(fx.mk_fill(i, B0 + 5, tao=10 * TAO, alpha=990 * TAO)),           # duplicate fill id
        YieldAccrued(BookId("b1"), fx.K9, fx.HK_A, Block(B0 + 360), Decimal(1), Decimal(2), 5),
        DeregSettled(BookId("b1"), fx.K9, fx.HK_A, Block(B0 + 400), 0, Rao(5), "formula"),
        SubmitUnknown(BookId("b1"), i.order_id, 0, "late"),                            # FILLED -> UNKNOWN is illegal
    ]
    st2 = fold_batch(st, facts)
    assert st2.orphans == len(facts) and len(st2.quarantine) == len(facts)
    assert (st2.portfolio, st2.ledger) == money and check_state(st2) == []
    cleared = fold_batch(st2, [QuarantineCleared(BookId("b1"), Block(B0 + 500), "operator")])
    assert cleared.orphans == 0 and cleared.quarantine == ()


def test_a_fill_on_submitting_is_an_orphan_but_after_recovered_submitting_it_fills(fx) -> None:
    st = funded(fx)
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0, tao_in=10 * TAO)
    st = fold_batch(st, [OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim0", None, Block(B0 + 12))])
    f = FillReported(fx.mk_fill(i, B0 + 5, tao=10 * TAO, alpha=990 * TAO))
    crashed = fold_batch(st, [f])                       # SUBMITTING -> FILLED is illegal: quarantined, never raised
    assert crashed.orphans == 1 and order_state(crashed, i) is OrderState.SUBMITTING
    recovered = fold_batch(st, [SubmitUnknown(i.book, i.order_id, 0, "recovered_submitting")])
    assert order_state(recovered, i) is OrderState.UNKNOWN
    landed = fold_batch(recovered, [f])                 # resolve(LANDED) facts apply to UNKNOWN
    assert order_state(landed, i) is OrderState.FILLED and landed.orphans == 0 and check_state(landed) == []


def test_recovered_submitting_then_miss_expires_and_the_carrier_fee_is_accepted_once(fx) -> None:
    st = funded(fx)
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0, tao_in=10 * TAO)
    st = fold_batch(st, [OrderIntended(i), SubmitStarted(i.book, i.order_id, 0, "sim1", 7, Block(B0 + 13)),
                         SubmitUnknown(i.book, i.order_id, 0, "recovered_submitting"),
                         OrderFailed(i.book, i.order_id, 0, Block(B0 + 5), FailReason.SHIELD_MISSED, Rao(0), expired=True)])
    assert order_state(st, i) is OrderState.EXPIRED and st.orphans == 0
    fee = CarrierFeeSettled(i.book, i.order_id, 0, Block(B0 + 20), Rao(98_000), "carrier_only")
    st2 = fold_batch(st, [fee])
    assert st2.orphans == 0 and st2.portfolio.fee_float == TAO - 98_000
    assert ledger_balance(st2, "fees:tx", TAO_UNIT) == 98_000 and check_state(st2) == []
    assert fold_batch(st2, [fee]).orphans == 1          # a second settlement is quarantined (the journal rejects it too)


def test_cancel_and_illegal_transitions(fx) -> None:
    st = funded(fx)
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0, tao_in=TAO)
    st = fold_batch(st, [OrderIntended(i), OrderCancelled(i.book, i.order_id, 0, Block(B0), "ttl_expired")])
    assert order_state(st, i) is OrderState.CANCELLED
    st = fold_batch(st, [SubmitStarted(i.book, i.order_id, 0, "sim0", None, None)])   # CANCELLED -> SUBMITTING
    assert st.orphans == 1
    st = fold_batch(st, [OrderIntended(i)])              # duplicate intent
    assert st.orphans == 2


# ------------------------------------------------------------------------------------------------- netting transfers
def test_sleeve_transfer_moves_shares_and_cash_without_ledger_postings(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    st, _ = buy(fx, funded(fx, sleeves), alpha=1_000 * TAO, attribution=((CARRY, Ppm(1_000_000)),))
    ledger, cash = st.ledger, st.portfolio.cash
    x = SleeveTransfer(BookId("b1"), Block(B0 + 60), fx.K7, CARRY, MOM, Decimal(400 * TAO), Rao(4 * TAO), PriceRao(10**7))
    st2 = fold_batch(st, [x])
    assert st2.ledger == ledger and st2.portfolio.cash == cash
    hold = {h.strategy: h.shares for h in st2.portfolio.sleeves}
    assert hold == {CARRY: Decimal(600 * TAO), MOM: Decimal(400 * TAO)}
    sc = dict(st2.portfolio.sleeve_cash)
    assert sc[CARRY] - dict(st.portfolio.sleeve_cash)[CARRY] == 4 * TAO
    assert sc[MOM] - dict(st.portfolio.sleeve_cash)[MOM] == -4 * TAO
    assert check_state(st2) == []                         # invariant 3 holds
    over = replace(x, from_strategy=MOM, to_strategy=CARRY, shares=Decimal(500 * TAO), tao=Rao(5 * TAO))
    st3 = fold_batch(st2, [over])                         # clamped to MOM's 400 shares, TAO scaled to 4
    assert {h.strategy: h.shares for h in st3.portfolio.sleeves} == {CARRY: Decimal(1_000 * TAO)}
    assert st3.anomalies == st2.anomalies + 1 and check_state(st3) == [] and st3.ledger == ledger


# ------------------------------------------------------------------------------------------------- dissolution
def test_live_deregistered_marks_dissolving_and_a_second_settlement_is_rejected(fx, tmp_path) -> None:
    st, _ = buy(fx, funded(fx, mode=RunMode.LIVE), alpha=1_000 * TAO)
    ev = ChainEventObserved(ChainEvent(ChainEventKind.DEREGISTERED, Block(B0 + 600), key=fx.K7))
    st = fold_batch(st, [obs(B0 + 600), ev])
    assert st.dissolving == (fx.K7,) and book_view(st).dissolving == (fx.K7,)
    assert st.portfolio.position(fx.K7) is not None                      # still held until the payout is observed
    d = DeregSettled(BookId("b1"), fx.K7, fx.HK_A, Block(B0 + 640), 1_000 * TAO, Rao(3 * TAO), "observed")
    st2 = fold_batch(st, [d])
    assert st2.dissolving == () and st2.portfolio.positions == () and st2.portfolio.cash == 93 * TAO
    assert fx.position_alpha(st2, fx.K7, fx.HK_A) == 0
    assert ledger_balance(st2, "loss:dereg", alpha_unit(fx.K7)) == 1_000 * TAO
    assert ledger_balance(st2, "loss:dereg", TAO_UNIT) == -3 * TAO
    assert check_state(st2) == []
    st3 = fold_batch(st2, [d])
    assert st3.orphans == 1 and st3.portfolio == st2.portfolio            # never paid twice
    with SqliteJournal(tmp_path / "j.sqlite", durable=False) as j:
        from taotrader.core.units import LogicalTime, Phase
        j.append_batch([(LogicalTime(Block(B0 + 640), Phase.OUTBOX), BookId("b1"), d)])
        with pytest.raises(DuplicateIdempotencyKey):
            j.append_batch([(LogicalTime(Block(B0 + 700), Phase.OUTBOX), BookId("b1"), d)])


# ------------------------------------------------------------------------------------------------- failures, delegates
def fail(i, block: int, reason: FailReason = FailReason.PRICE_LIMIT_EXCEEDED, *, exact: bool = True, fee: int = 1_028_000,
         expired: bool = False) -> OrderFailed:
    return OrderFailed(i.book, i.order_id, i.attempt, Block(block), reason, Rao(fee), expired=expired, exact_block=exact)


def test_failure_counters_use_exact_block_chain_failures_only(fx) -> None:
    st = funded(fx)
    for n, (exact, reason) in enumerate([(True, FailReason.PRICE_LIMIT_EXCEEDED), (False, FailReason.SLIPPAGE_TOO_HIGH),
                                         (True, FailReason.VENUE_REJECT), (True, FailReason.SLIPPAGE_TOO_HIGH)]):
        i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + n, tao_in=TAO)
        st = fold_batch(place(st, i), [fail(i, B0 + n + 5, reason, exact=exact)])
    v = book_view(st, Block(B0 + 10))
    assert v.fail_counts_600 == ((NetUid(7), 2),) and v.fail_count_600_book == 2
    assert not any(c[1] == "fail_burst" for c in v.cooldowns)
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + 20, tao_in=TAO)
    st = fold_batch(place(st, i), [fail(i, B0 + 25)])
    v = book_view(st, Block(B0 + 25))
    assert (fx.K7, "fail_burst", Block(B0 + 25 + 7_200)) in v.cooldowns
    assert book_view(st, Block(B0 + 700)).fail_counts_600 == ()        # outside the 600-block window
    assert st.portfolio.fee_float == TAO - 5 * 1_028_000                 # every failure paid its (test) fee
    assert check_state(st) == []


def test_book_wide_failure_burst_halts_entries(fx) -> None:
    st = funded(fx)
    keys = [SubnetKey(NetUid(n), Block(1_000)) for n in (3, 4, 5, 6, 8)]
    for n, k in enumerate(keys):
        i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + n, k, tao_in=TAO)
        st = fold_batch(place(st, i), [fail(i, B0 + 10)])
    assert book_view(st, Block(B0 + 10)).entries_halted_until == Block(B0 + 10 + 300)


def test_shield_miss_locks_the_delegate_until_era_end_plus_two(fx) -> None:
    st = funded(fx)
    i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0, tao_in=TAO)
    st = place(st, i, delegate="sim1", era_end=B0 + 13)
    v = book_view(st, Block(B0 + 1))
    assert "sim1" not in v.delegates_free and ("sim1", Block(B0 + 15)) in v.delegate_locked_until
    st = fold_batch(st, [fail(i, B0 + 5, FailReason.SHIELD_MISSED, fee=98_000, expired=True)])
    assert "sim1" not in book_view(st, Block(B0 + 15)).delegates_free
    assert book_view(st, Block(B0 + 16)).delegates_free == ("sim0", "sim1", "sim2")


# ------------------------------------------------------------------------------------------------- traces
def trace(block: int, *, actions=(), memories=(), ran=(), signals=(), nav: int = 0, sleeve_nav=(),
          mode: Mode = Mode.NORMAL) -> DecisionTrace:
    return DecisionTrace(BookId("b1"), Block(block), tuple(ran), tuple(signals), tuple(actions), mode, tuple(memories), "fd",
                         0, "", Rao(nav), tuple(sleeve_nav))


def test_chase_episode_from_order_spot_actions(fx) -> None:
    st = funded(fx)
    spot = RiskAction(ENGINE_ORDER_SPOT, fx.K7, "MONITOR", "spot_rao=10000000;order_id=x")
    i0 = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0, tao_in=TAO)
    st = fold_batch(st, [trace(B0, actions=[spot]), OrderIntended(i0)])
    assert book_view(st).chase == ((fx.K7, 0, PriceRao(10_000_000)),)
    st = fold_batch(st, [SubmitStarted(i0.book, i0.order_id, 0, "sim0", None, Block(B0 + 12)),
                         VenueAck(i0.book, i0.order_id, 0, Block(B0 + 3), Block(B0 + 5), "", ""),
                         fail(i0, B0 + 5)])
    i1 = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + 60, tao_in=TAO, attempt=1)
    st = fold_batch(st, [obs(B0 + 60), trace(B0 + 60, actions=[replace(spot, detail="spot_rao=10100000")]), OrderIntended(i1)])
    assert book_view(st).chase == ((fx.K7, 1, PriceRao(10_000_000)),)  # the episode keeps its decision spot
    st = place(st, i1)
    st = fold_batch(st, [FillReported(fx.mk_fill(i1, B0 + 65, tao=TAO, alpha=99 * TAO))])
    assert book_view(st).chase == ()                                    # filled: episode closed
    i2 = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + 120, fx.K9, tao_in=TAO)
    st = fold_batch(st, [obs(B0 + 120), OrderIntended(i2)])
    assert book_view(st).chase == ((fx.K9, 0, PriceRao(0)),)
    st = fold_batch(st, [obs(B0 + 120 + 1_800)])
    assert book_view(st).chase == ()                                    # stale after 1,800 blocks


def test_trace_actions_feed_forced_exits_cooldowns_and_halts(fx) -> None:
    st = funded(fx)
    acts = [RiskAction(ENGINE_FORCED_EXIT, fx.K7, "FORCE_EXIT", f"rule=prune_A;urgency={int(Urgency.EMERGENCY)}"),
            RiskAction("owner.sale", fx.K9, "VETO_ENTRY", f"cooldown_until={B0 + 7_200};sold_ppm=30000"),
            RiskAction("mode.daily_loss", None, "HALT_ENTRIES", f"halt_until={B0 + 100}")]
    st = fold_batch(st, [trace(B0, actions=acts)])
    v = book_view(st, Block(B0))
    assert v.recent_forced_exits == ((Block(B0), fx.K7, "prune_A", Urgency.EMERGENCY),)
    assert (fx.K9, "owner.sale", Block(B0 + 7_200)) in v.cooldowns
    assert v.entries_halted_until == Block(B0 + 100)
    st = fold_batch(st, [obs(B0 + 60), trace(B0 + 60, actions=acts[:1])])   # re-evaluated: latest block kept, deduped
    assert book_view(st).recent_forced_exits == ((Block(B0 + 60), fx.K7, "prune_A", Urgency.EMERGENCY),)
    st = fold_batch(st, [obs(B0 + 101)])
    assert book_view(st).entries_halted_until is None
    st = fold_batch(st, [obs(B0 + 60 + 21_600)])
    assert book_view(st).recent_forced_exits == ()


def test_memories_router_standing_signals_and_last_calls(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    st = funded(fx, sleeves)
    router = RouterState(choice=((fx.K7, fx.HK_B),))
    s_c = Signal(CARRY, fx.K7, Block(B0), SignalKind.TARGET, Ppm(500_000))
    s_m = Signal(MOM, fx.K9, Block(B0), SignalKind.TARGET, Ppm(100_000), horizon_blocks=600)
    st = fold_batch(st, [trace(B0, ran=[CARRY, MOM], signals=[s_c, s_m],
                               memories=[(CARRY, b'{"count":1}'), (MOM, b"3"), (ROUTER_MEMORY_ID, codec.canonical_bytes(router))])])
    assert st.router == router and book_view(st).router == router
    assert dict(st.memories) == {CARRY: b'{"count":1}', MOM: b"3"}
    assert dict(st.last_calls) == {CARRY: Block(B0), MOM: Block(B0)}
    assert st.standing == (s_c, s_m)
    st = fold_batch(st, [obs(B0 + 300), trace(B0 + 300, ran=[CARRY], signals=[], memories=[(CARRY, b'{"count":2}')])])
    assert st.standing == (s_m,)                                         # carry replaced its signals with none
    st = fold_batch(st, [obs(B0 + 600)])
    assert st.standing == ()                                             # momentum's 600-block horizon expired


def test_nav_daily_samples_one_per_day(fx) -> None:
    st = funded(fx)
    for k, b in enumerate([B0, B0 + 60, B0 + 7_200, B0 + 7_260, B0 + 14_400 + 1]):
        st = fold_batch(st, [obs(b), trace(b, nav=100 + k)])
    assert st.nav_daily == ((Block(B0), Rao(100)), (Block(B0 + 7_200), Rao(102)), (Block(B0 + 14_401), Rao(104)))


def test_sleeve_kill_states_follow_un_netted_drawdown(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(800_000))]
    st = funded(fx, sleeves)
    day = 7_200
    navs = [100_000, 90_000, 84_000] + [84_000 + 100 * k for k in range(1, 70)]
    states = []
    for d, nav in enumerate(navs):
        b = B0 + d * day
        st = fold_batch(st, [obs(b), trace(b, nav=nav, sleeve_nav=[(CARRY, Rao(nav))])])
        states.append(book_view(st).sleeve_stats[0].state)
    assert states[0] == "ACTIVE" and states[1] == "REDUCED" and states[2] == "SUSPENDED"
    first_reduced = states.index("REDUCED", 3)
    assert first_reduced == 2 + 30                                       # 30 days with mean > 0 and DD < 5%
    assert states[first_reduced + 30] == "ACTIVE"                        # 30 more days with DD < 4%
    s = book_view(st).sleeve_stats[0]
    assert s.strategy == CARRY and s.dd_ppm == 0 and s.mean_45d_ppm_day is not None and s.mean_45d_p5_ppm_day is None


def test_capital_flows_do_not_count_as_sleeve_returns(fx) -> None:
    st = funded(fx, [SleeveCfg(CARRY, Stage.PAPER, Ppm(800_000))], cash=100_000)
    st = fold_batch(st, [obs(B0), trace(B0, nav=100_000, sleeve_nav=[(CARRY, Rao(100_000))])])
    st = fold_batch(st, [obs(B0 + 7_200), CapitalChanged(BookId("b1"), Block(B0 + 7_200), 50_000, 0, "topup"),
                         trace(B0 + 7_200, nav=150_000, sleeve_nav=[(CARRY, Rao(150_000))])])
    (t,) = st.sleeve_tracks
    assert t.returns == (0,) and t.index_e9 == 10**9


# ------------------------------------------------------------------------------------------------- chain events, inputs
def ce(kind: ChainEventKind, block: int, key=None, **kw) -> ChainEventObserved:
    return ChainEventObserved(ChainEvent(kind, Block(block), key=key, **kw))


def test_emission_disable_wave_halts_entries_and_bans_each_key(fx) -> None:
    st = funded(fx)
    b = B0 + 60
    keys = [SubnetKey(NetUid(n), Block(1_000)) for n in (3, 4, 5)]
    st = fold_batch(st, [obs(b)] + [ce(ChainEventKind.EMISSION_TOGGLED, b, k, flag=False) for k in keys])
    v = book_view(st)
    assert v.entries_halted_until == Block(b + 7_200)
    assert {(c[0], c[1]) for c in v.cooldowns} == {(k, "emission_off") for k in keys}
    assert all(c[2] == b + 100_800 for c in v.cooldowns)
    st = fold_batch(st, [obs(b + 60), ce(ChainEventKind.EMISSION_TOGGLED, b + 60, keys[0], flag=True)])
    assert (keys[0], "emission_reenable", Block(b + 60 + 360)) in book_view(st).cooldowns


def test_two_disables_are_not_a_wave_and_owner_events_cool_down(fx) -> None:
    st = funded(fx)
    st = fold_batch(st, [obs(B0 + 60), ce(ChainEventKind.EMISSION_TOGGLED, B0 + 60, fx.K7, flag=False),
                         ce(ChainEventKind.EMISSION_TOGGLED, B0 + 60, fx.K9, flag=False),
                         ce(ChainEventKind.OWNER_CHANGED, B0 + 60, fx.K7),
                         ce(ChainEventKind.AUTOLOCK_TOGGLED, B0 + 60, fx.K9, flag=False)])
    v = book_view(st)
    assert v.entries_halted_until is None
    assert (fx.K7, "owner_changed", Block(B0 + 60 + 7_200)) in v.cooldowns
    assert (fx.K9, "owner_autolock_off", Block(B0 + 60 + 7_200)) in v.cooldowns


def test_spec_change_burn_in_and_drift_alarms(fx) -> None:
    st = funded(fx)
    b = B0 + 60
    st = fold_batch(st, [obs(b), ce(ChainEventKind.SPEC_CHANGED, b, name="spec_version", old="475", new="999")])
    assert st.last_spec_change == b and st.burn_in_until is None        # no lead-approved touches_econ row for 999
    st = fold_batch(st, [ModelDriftObserved(Block(b + 600), "emission_parity", None, 25_000)])
    assert st.burn_in_until == b + 100_800                               # post-spec parity breach starts the burn-in
    st = fold_batch(st, [ModelDriftObserved(Block(b + 700), "sim_swap_buy", NetUid(7), 900)])
    assert st.drift_until == b + 700 + 7_200
    late = fold_batch(funded(fx), [ModelDriftObserved(Block(b), "yield_parity", None, 1)])
    assert late.burn_in_until is None                                    # no spec change: no burn-in


def test_operator_commands(fx) -> None:
    st, _ = buy(fx, funded(fx))
    op = lambda c, n: OperatorCommand(Block(B0 + 60), c, "test", n)      # noqa: E731
    st = fold_batch(st, [op("halt", "1"), op("exits_only", "2"), op("flatten:7", "3"), op("flatten:9", "4"), op("bogus", "5")])
    assert st.halted and st.exits_only and st.flatten == (NetUid(7),) and st.anomalies == 1
    st = fold_batch(st, [op("resume", "6")])
    assert not st.halted and not st.exits_only and st.flatten == () and st.resume_count == 1


def test_recon_adjusted_chain_wins_and_halts_entries_until_cleared(fx) -> None:
    st, _ = buy(fx, funded(fx, mode=RunMode.LIVE), alpha=1_000 * TAO)
    r = ReconAdjusted(BookId("b1"), Block(B0 + 100), -5_000, 2_000, ((PositionKey(fx.K7, fx.HK_A), Decimal(-3 * TAO)),),
                      "share delta")
    st2 = fold_batch(st, [r])
    assert st2.recon_halt and st2.portfolio.cash == st.portfolio.cash - 5_000
    assert st2.portfolio.fee_float == st.portfolio.fee_float + 2_000
    assert st2.portfolio.positions[0].shares == 997 * TAO and fx.position_alpha(st2, fx.K7, fx.HK_A) == 997 * TAO
    assert ledger_balance(st2, RECON_ACCOUNT, TAO_UNIT) == 3_000
    assert check_state(st2) == []
    st3 = fold_batch(st2, [QuarantineCleared(BookId("b1"), Block(B0 + 200), "operator ack")])
    assert not st3.recon_halt


def test_events_of_other_books_are_ignored(fx) -> None:
    st = funded(fx)
    other = CapitalChanged(BookId("b2"), Block(B0), 5 * TAO, 0, "initial")
    assert reduce(st, other) is st


# ------------------------------------------------------------------------------------------------- projections
def test_book_view_orders_window_and_inflight_delegates(fx) -> None:
    st, i = buy(fx, funded(fx))
    open_i = fx.mk_intent(OrderKind.ADD_STAKE_LIMIT, B0 + 60, fx.K9, tao_in=TAO)
    st = fold_batch(st, [obs(B0 + 60)])
    st = place(st, open_i, delegate="sim2", era_end=B0 + 70)
    v = book_view(st)
    assert [o.intent.order_id for o in v.orders] == [i.order_id, open_i.order_id]
    assert v.delegates_free == ("sim0", "sim1") and ("sim2", Block(B0 + 72)) in v.delegate_locked_until
    st = fold_batch(st, [obs(B0 + 5 + 7_200)])
    assert [o.intent.order_id for o in book_view(st).orders] == [open_i.order_id]   # terminal order aged out
    assert book_view(st).recent_fills == ()


def test_checkpoint_plus_tail_equals_a_full_fold(fx) -> None:
    sleeves = [SleeveCfg(CARRY, Stage.PAPER, Ppm(400_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(400_000))]
    base = initial_state(spec(fx, sleeves))
    st, _ = buy(fx, funded(fx, sleeves), alpha=1_000 * TAO, attribution=((CARRY, Ppm(500_000)), (MOM, Ppm(500_000))))
    tail = [
        [obs(B0 + 60), trace(B0 + 60, actions=[RiskAction(ENGINE_FORCED_EXIT, fx.K7, "FORCE_EXIT", "rule=owner;urgency=3")],
                             nav=100 * TAO, sleeve_nav=[(CARRY, Rao(50 * TAO)), (MOM, Rao(50 * TAO))])],
        [SleeveTransfer(BookId("b1"), Block(B0 + 60), fx.K7, CARRY, MOM, Decimal(100 * TAO), Rao(TAO), PriceRao(10**7))],
        [obs(B0 + 360), YieldAccrued(BookId("b1"), fx.K7, fx.HK_A, Block(B0 + 360), Decimal(1), Decimal("1.0003"), 3 * 10**8)],
    ]
    blob_hash, blob = encode_state(st)
    restored = decode_state(blob, blob_hash)
    assert restored == st
    a = st
    b = restored
    for batch in tail:
        a, b = fold_batch(a, batch), fold_batch(b, batch)
    assert a == b and book_view(a) == book_view(b) and money_digest(a) == money_digest(b)
    assert base.spec == a.spec and check_state(a) == []
