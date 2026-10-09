"""Reducer properties (DESIGN.md 10.2): a RuleBasedStateMachine over random but well-formed journals.

Invariants after every batch: the typed portfolio agrees with the double-entry ledger (check_invariants, all five
classes), orphan facts never raise and never move money, and a checkpoint taken at a random point and folded with the
same tail equals the live state (including its BookView). At teardown, a full fold from scratch equals the state.
"""
from __future__ import annotations

from decimal import Decimal

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from taotrader.core.config import BookCfg, ExecCfg, RiskCfg, SleeveCfg
from taotrader.core.events import (
    CapitalChanged,
    CarrierFeeSettled,
    DecisionTrace,
    FillReported,
    HealthObs,
    JournalEvent,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    QuarantineCleared,
    SleeveTransfer,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
    YieldAccrued,
)
from taotrader.core.fixed import DEC, floor_int
from taotrader.core.orders import FailReason, Fill, OrderIntent, OrderKind, OrderState, Urgency, make_order_id
from taotrader.core.portfolio import alpha_unit, pos_account
from taotrader.core.state import ReadPlan
from taotrader.core.units import (
    PPM,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    Mode,
    NetUid,
    Ppm,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.engine.recovery import decode_state, encode_state
from taotrader.engine.reducer import (
    BookSpec,
    EngineState,
    OrderEntry,
    book_view,
    check_state,
    fold,
    fold_batch,
    initial_state,
    ledger_balance,
    value_at,
)

TAO = 10**9
B0 = 9_000_000
BOOK = BookId("b1")
HK = Hotkey("0x" + "a" * 64)
KEYS = [SubnetKey(NetUid(n), Block(1_000)) for n in (7, 9, 11)]
CARRY, MOM = StrategyId("carry"), StrategyId("momentum")
CFG = BookCfg(book=BOOK, capital_rao=Rao(100 * TAO), fee_float_rao=Rao(1_000 * TAO),
              sleeves=(SleeveCfg(CARRY, Stage.PAPER, Ppm(500_000)), SleeveCfg(MOM, Stage.PAPER, Ppm(300_000))),
              risk=RiskCfg(), exec=ExecCfg())


def obs(block: int) -> SnapshotObserved:
    return SnapshotObserved(Block(block), BlockHash("0x" + f"{block:064x}"), f"d{block}", ReadPlan.FULL, 0, HealthObs.nominal())


class ReducerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.spec = BookSpec.from_cfg(CFG, RunMode.BACKTEST)
        self.state: EngineState = initial_state(self.spec)
        self.cp: EngineState | None = None
        self.batches: list[list[JournalEvent]] = []
        self.block = B0
        self.n = 0
        self.index: dict[SubnetKey, Decimal] = {k: Decimal(1) for k in KEYS}
        self.orphans_injected = 0
        self.apply([obs(B0), CapitalChanged(BOOK, Block(B0), 100 * TAO, 1_000 * TAO, "initial")])

    # ------------------------------------------------------------------ helpers
    def apply(self, batch: list[JournalEvent]) -> None:
        self.batches.append(batch)
        self.state = fold_batch(self.state, batch)
        if self.cp is not None:
            self.cp = fold_batch(self.cp, batch)

    def orders(self, *states: OrderState) -> list[OrderEntry]:
        return [o for o in self.state.orders if o.state in states]

    def intent(self, kind: OrderKind, key: SubnetKey, *, tao_in: int = 0, alpha_in: int = 0, full: bool = False,
               attribution: tuple[tuple[StrategyId, Ppm], ...] = ((CARRY, Ppm(PPM)),)) -> OrderIntent:
        self.n += 1
        return OrderIntent(order_id=make_order_id(f"run{self.n}", BOOK, Block(self.block), key, HK, kind, 0), attempt=0,
                           book=BOOK, created_block=Block(self.block), kind=kind, key=key, hotkey=HK, tao_in=Rao(tao_in),
                           alpha_in=AlphaRao(alpha_in), full_position=full, limit_price=PriceRao(1), allow_partial=False,
                           shielded=True, valid_until=Block(self.block + 5), expected_out=max(tao_in * 95, 1),
                           urgency=Urgency.NORMAL, attribution=attribution, reason="prop")

    def fill(self, i: OrderIntent, *, tao: int, alpha: int, shares: Decimal) -> Fill:
        return Fill(fill_id=f"{i.order_id}:0:0", order_id=i.order_id, attempt=0, book=BOOK, block=Block(self.block),
                    kind=i.kind, key=i.key, hotkey=HK, tao=Rao(tao), alpha=AlphaRao(alpha), shares=shares,
                    swap_fee=tao * 33 // 65_535, author_fee_tao=Rao(0), tx_fee=Rao(1_028_000), d_pool_tao=0, d_pool_alpha=0,
                    spot_before=PriceRao(10_500_000), shortfall_ppm=Ppm(600), complete=True,
                    exact_block=self.n % 2 == 0)

    # ------------------------------------------------------------------ rules
    @rule(dt=st.integers(1, 700))
    def advance(self, dt: int) -> None:
        self.block += dt
        self.apply([obs(self.block)])

    @rule(key=st.sampled_from(KEYS), tao=st.integers(TAO // 2, 30 * TAO), split=st.integers(0, PPM))
    def intend_buy(self, key: SubnetKey, tao: int, split: int) -> None:
        attribution = tuple((sid, Ppm(p)) for sid, p in ((CARRY, split), (MOM, PPM - split)) if p > 0)
        self.apply([OrderIntended(self.intent(OrderKind.ADD_STAKE_LIMIT, key, tao_in=tao, attribution=attribution))])

    @precondition(lambda self: bool(self.state.portfolio.positions))
    @rule(data=st.data(), full=st.booleans(), split=st.integers(0, PPM))
    def intend_sell(self, data: st.DataObject, full: bool, split: int) -> None:
        pos = data.draw(st.sampled_from(self.state.portfolio.positions))
        led = ledger_balance(self.state, pos_account(pos.key, pos.hotkey), alpha_unit(pos.key))
        attribution = tuple((sid, Ppm(p)) for sid, p in ((CARRY, split), (MOM, PPM - split)) if p > 0)
        alpha = max(led // 3, 1)
        self.apply([OrderIntended(self.intent(OrderKind.REMOVE_STAKE_LIMIT, pos.key, alpha_in=0 if full else alpha,
                                              full=full, attribution=attribution))])

    @precondition(lambda self: bool(self.orders(OrderState.INTENDED)))
    @rule(data=st.data(), delegate=st.sampled_from(["sim0", "sim1", "sim2"]))
    def submit(self, data: st.DataObject, delegate: str) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.INTENDED)))
        self.apply([SubmitStarted(BOOK, o.intent.order_id, 0, delegate, None, Block(self.block + 10))])

    @precondition(lambda self: bool(self.orders(OrderState.INTENDED)))
    @rule(data=st.data())
    def cancel(self, data: st.DataObject) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.INTENDED)))
        self.apply([OrderCancelled(BOOK, o.intent.order_id, 0, Block(self.block), "ttl_expired")])

    @precondition(lambda self: bool(self.orders(OrderState.SUBMITTING, OrderState.UNKNOWN)))
    @rule(data=st.data())
    def ack(self, data: st.DataObject) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.SUBMITTING, OrderState.UNKNOWN)))
        self.apply([VenueAck(BOOK, o.intent.order_id, 0, Block(self.block + 3), Block(self.block + 5), "", "")])

    @precondition(lambda self: bool(self.orders(OrderState.SUBMITTING)))
    @rule(data=st.data())
    def crash_unknown(self, data: st.DataObject) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.SUBMITTING)))
        self.apply([SubmitUnknown(BOOK, o.intent.order_id, 0, "recovered_submitting")])

    @precondition(lambda self: bool(self.orders(OrderState.SUBMITTED, OrderState.UNKNOWN)))
    @rule(data=st.data(), frac=st.integers(1, PPM))
    def land(self, data: st.DataObject, frac: int) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.SUBMITTED, OrderState.UNKNOWN)))
        i = o.intent
        p = self.state.portfolio
        if i.kind is OrderKind.ADD_STAKE_LIMIT:
            tao = min(int(i.tao_in), int(p.cash))
            if tao < TAO // 10:
                self.apply([OrderFailed(BOOK, i.order_id, 0, Block(self.block), FailReason.OTHER, Rao(1_028_000))])
                return
            alpha = tao * 95
            shares = DEC.divide(Decimal(alpha), self.index[i.key])
            self.apply([FillReported(self.fill(i, tao=tao, alpha=alpha, shares=shares))])
            return
        pos = p.position(i.key)
        if pos is None:
            self.apply([OrderFailed(BOOK, i.order_id, 0, Block(self.block), FailReason.NOT_ENOUGH_STAKE, Rao(837_000))])
            return
        led = ledger_balance(self.state, pos_account(pos.key, pos.hotkey), alpha_unit(pos.key))
        mark = dict(self.state.marks)[pos.pkey]
        if i.full_position:
            shares, alpha = pos.shares, max(led, 0)
        else:
            shares = min(pos.shares, DEC.divide(DEC.multiply(pos.shares, Decimal(frac)), Decimal(PPM)))
            alpha = min(value_at(shares, mark), max(led, 0))
        self.apply([FillReported(self.fill(i, tao=alpha // 100, alpha=alpha, shares=shares))])

    @precondition(lambda self: bool(self.orders(OrderState.SUBMITTING, OrderState.SUBMITTED, OrderState.UNKNOWN)))
    @rule(data=st.data(), expired=st.booleans(), exact=st.booleans(),
          reason=st.sampled_from([FailReason.PRICE_LIMIT_EXCEEDED, FailReason.SHIELD_MISSED, FailReason.VENUE_REJECT]))
    def fail(self, data: st.DataObject, expired: bool, exact: bool, reason: FailReason) -> None:
        o = data.draw(st.sampled_from(self.orders(OrderState.SUBMITTING, OrderState.SUBMITTED, OrderState.UNKNOWN)))
        expired = expired and o.state is not OrderState.SUBMITTING
        fee = 0 if reason is FailReason.VENUE_REJECT else 98_000
        self.apply([OrderFailed(BOOK, o.intent.order_id, 0, Block(self.block), reason, Rao(fee), expired=expired,
                                exact_block=exact)])

    @precondition(lambda self: any(not o.carrier_fee_settled for o in self.orders(OrderState.EXPIRED)))
    @rule(data=st.data(), fee=st.integers(0, 200_000))
    def carrier_fee(self, data: st.DataObject, fee: int) -> None:
        o = data.draw(st.sampled_from([o for o in self.orders(OrderState.EXPIRED) if not o.carrier_fee_settled]))
        self.apply([CarrierFeeSettled(BOOK, o.intent.order_id, 0, Block(self.block), Rao(fee), "carrier_only")])

    @precondition(lambda self: bool(self.state.portfolio.positions))
    @rule(data=st.data(), growth=st.integers(0, 3_000))
    def accrue(self, data: st.DataObject, growth: int) -> None:
        pos = data.draw(st.sampled_from(self.state.portfolio.positions))
        old = self.index[pos.key]
        new = DEC.multiply(old, DEC.divide(Decimal(PPM + growth), Decimal(PPM)))
        self.index[pos.key] = new
        led = ledger_balance(self.state, pos_account(pos.key, pos.hotkey), alpha_unit(pos.key))
        delta = value_at(pos.shares, new) - led
        if delta:
            self.apply([YieldAccrued(BOOK, pos.key, pos.hotkey, Block(self.block), old, new, delta)])

    @precondition(lambda self: bool(self.state.portfolio.sleeves))
    @rule(data=st.data(), frac=st.integers(1, 2 * PPM), tao=st.integers(0, 10 * TAO))
    def transfer(self, data: st.DataObject, frac: int, tao: int) -> None:
        h = data.draw(st.sampled_from(self.state.portfolio.sleeves))
        to = MOM if h.strategy == CARRY else CARRY
        shares = DEC.divide(DEC.multiply(h.shares, Decimal(frac)), Decimal(PPM))       # up to 2x: exercises the clamp
        if shares <= 0:
            return
        self.apply([SleeveTransfer(BOOK, Block(self.block), h.key, h.strategy, to, shares, Rao(tao), PriceRao(10**7))])

    @rule(nav=st.integers(0, 200 * TAO))
    def trace(self, nav: int) -> None:
        self.apply([DecisionTrace(BOOK, Block(self.block), (), (), (), Mode.NORMAL, (), "fd", 0, "", Rao(nav),
                                  ((CARRY, Rao(nav // 2)), (MOM, Rao(nav // 3))))])

    @rule(key=st.sampled_from(KEYS))
    def orphan_fact(self, key: SubnetKey) -> None:
        ghost = self.intent(OrderKind.ADD_STAKE_LIMIT, key, tao_in=TAO)
        before = (self.state.portfolio, self.state.ledger)
        self.apply([FillReported(self.fill(ghost, tao=TAO, alpha=95 * TAO, shares=Decimal(95 * TAO)))])
        self.orphans_injected += 1
        assert (self.state.portfolio, self.state.ledger) == before

    @precondition(lambda self: self.state.orphans > 0)
    @rule()
    def clear(self) -> None:
        self.apply([QuarantineCleared(BOOK, Block(self.block), "operator")])

    @rule()
    def checkpoint(self) -> None:
        h, blob = encode_state(self.state)
        self.cp = decode_state(blob, h)

    # ------------------------------------------------------------------ invariants
    @invariant()
    def portfolio_matches_ledger(self) -> None:
        assert check_state(self.state) == [], check_state(self.state)
        assert self.state.breaches == ()
        sleeve_cash = sum(c for _, c in self.state.portfolio.sleeve_cash)
        assert sleeve_cash == self.state.portfolio.cash

    @invariant()
    def checkpoint_plus_tail_equals_live(self) -> None:
        if self.cp is not None:
            assert self.cp == self.state
            assert book_view(self.cp) == book_view(self.state)

    @invariant()
    def stride_failures_never_count(self) -> None:
        exact = sum(1 for o in self.state.orders if o.fail_reason is not None and o.exact_block
                    and o.fail_reason is not FailReason.VENUE_REJECT and o.terminal_block is not None
                    and o.terminal_block > self.state.clock - 600)
        assert book_view(self.state).fail_count_600_book <= exact

    def teardown(self) -> None:
        assert fold(initial_state(self.spec), self.batches) == self.state


TestReducerMachine = ReducerMachine.TestCase
TestReducerMachine.settings = settings(settings.default, stateful_step_count=40, deadline=None)   # profile decides examples


def test_value_at_is_floor_of_shares_times_index() -> None:
    assert value_at(Decimal(3), Decimal("0.5")) == 1
    assert value_at(Decimal(10**12), Decimal("1.0003")) == 1_000_300_000_000
    assert floor_int(Decimal("-0.5")) == -1
