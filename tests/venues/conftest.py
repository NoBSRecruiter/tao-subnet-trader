"""Shared fixtures for the venue tests (WP6).

- `pool(tao_tao=..., price=...)`, `subnet(...)`, `snap(block, *subnets, **glob)`: a 0.5/0.5 Balancer test market
  (default: SN7 generation reg_at 1,000, 1,000 TAO at 0.01 TAO/alpha, fee 33) with tracked hotkeys at index 1.
- `buy(...)`, `sell(...)`, `move(...)`: OrderIntent factories with deterministic ids and planner-style limits.
- `Harness(venue)`: a minimal Runner for one book. It journals every event, folds the order FSM (OrderRecord.to) and
  an independent double-entry ledger (core.portfolio helpers), and observes every committed event on the venue.
  place() = OrderIntended -> reserve -> SubmitStarted -> submit -> commit; tick() = SnapshotObserved + drain.
- `arun(coro)`: asyncio.run.
Test modules cannot import each other (importlib mode), so everything is a fixture.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from decimal import Decimal
from typing import Any, TypeVar

import pytest

from taotrader.core.events import (
    CapitalChanged,
    FillReported,
    HealthObs,
    JournalEvent,
    OrderCancelled,
    OrderFailed,
    OrderIntended,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from taotrader.core.orders import OrderIntent, OrderKind, OrderRecord, OrderState, Urgency, make_order_id
from taotrader.core.portfolio import TAO_UNIT, apply_txn, capital_txn, fail_txn, fill_txn
from taotrader.core.state import ChainSnapshot, HotkeyIdx, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import (
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    NetUid,
    Ppm,
    PriceRao,
    Rao,
    StrategyId,
    SubnetKey,
)
from taotrader.protocol.amm import marginal_after_buy, marginal_after_sell

T = TypeVar("T")
TAO = RAO_PER_TAO
BOOK = BookId("b1")
RUN = "run-wp6"
NETUID = 7
REG_AT = 1_000
KEY = SubnetKey(NetUid(NETUID), Block(REG_AT))
HK_A = Hotkey("0x" + "a" * 64)
HK_B = Hotkey("0x" + "b" * 64)
HK_C = Hotkey("0x" + "c" * 64)


def _run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def arun() -> Callable[[Coroutine[Any, Any, Any]], Any]:
    return _run


@pytest.fixture(scope="session")
def consts() -> dict[str, Any]:
    return {"TAO": TAO, "BOOK": BOOK, "RUN": RUN, "NETUID": NETUID, "REG_AT": REG_AT, "KEY": KEY,
            "HK_A": HK_A, "HK_B": HK_B, "HK_C": HK_C}


def _pool(tao_tao: Decimal | int = 1_000, price: Decimal | str = "0.01", *, fee_rate: int = 33,
          w_quote_e18: int = 5 * 10**17, tao: int | None = None, alpha: int | None = None) -> PoolState:
    t = int(Decimal(tao_tao) * TAO) if tao is None else tao
    a = int(Decimal(t) / Decimal(price)) if alpha is None else alpha
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(t), alpha=AlphaRao(a), px_tao=t, px_alpha=a, w_quote_e18=w_quote_e18,
                     fee_rate=fee_rate)


@pytest.fixture(scope="session")
def pool() -> Callable[..., PoolState]:
    return _pool


def _hk_idx(hotkey: Hotkey, total: int = 50_000 * TAO, shares: Decimal | None = None) -> HotkeyIdx:
    return HotkeyIdx(hotkey=hotkey, total_alpha=AlphaRao(total), total_shares=Decimal(total) if shares is None else shares,
                     earns=True)


@pytest.fixture(scope="session")
def hk_idx() -> Callable[..., HotkeyIdx]:
    return _hk_idx


@pytest.fixture(scope="session")
def subnet(make_subnet: Callable[..., SubnetState]) -> Callable[..., SubnetState]:
    def build(p: PoolState | None = None, *, netuid: int = NETUID, reg_at: int = REG_AT,
              hotkeys: tuple[HotkeyIdx, ...] | None = None, **overrides: Any) -> SubnetState:
        hks = hotkeys if hotkeys is not None else (_hk_idx(HK_A), _hk_idx(HK_B))
        return make_subnet(netuid, reg_at=reg_at, pool=p if p is not None else _pool(),
                           hotkeys=tuple(sorted(hks, key=lambda h: h.hotkey)), **overrides)

    return build


@pytest.fixture(scope="session")
def snap(make_snapshot: Callable[..., ChainSnapshot], subnet: Callable[..., SubnetState]) -> Callable[..., ChainSnapshot]:
    """snap(block) -> the default market; snap(block, s1, s2, ...) -> those subnets; glob overrides as kwargs."""

    def build(block: int, *subnets: SubnetState, **glob: Any) -> ChainSnapshot:
        subs = subnets if subnets else (subnet(),)
        s = make_snapshot(block, subs, **glob)
        return replace(s, block_hash=BlockHash("0x" + f"{block:064x}"), digest=f"d{block}")

    return build


def _intent(kind: OrderKind, block: int, *, key: SubnetKey = KEY, hotkey: Hotkey = HK_A, tao_in: int = 0,
            alpha_in: int = 0, full: bool = False, limit: int = 0, allow_partial: bool = False, shielded: bool = True,
            attempt: int = 0, valid_until: int | None = None, dest_hotkey: Hotkey | None = None,
            dest_key: SubnetKey | None = None, urgency: Urgency = Urgency.NORMAL, book: BookId = BOOK,
            lag: int = 3, latency: int = 2) -> OrderIntent:
    oid = make_order_id(RUN, book, Block(block), key, hotkey, kind, attempt)
    vu = valid_until if valid_until is not None else block + lag + (latency if shielded else 16)
    return OrderIntent(order_id=oid, attempt=attempt, book=book, created_block=Block(block), kind=kind, key=key,
                       hotkey=hotkey, tao_in=Rao(tao_in), alpha_in=AlphaRao(alpha_in), full_position=full,
                       limit_price=PriceRao(limit), allow_partial=allow_partial, shielded=shielded,
                       valid_until=Block(vu), expected_out=0, urgency=urgency,
                       attribution=((StrategyId("carry"), Ppm(PPM)),), reason="test", dest_key=dest_key,
                       dest_hotkey=dest_hotkey)


@pytest.fixture(scope="session")
def buy() -> Callable[..., OrderIntent]:
    """buy(block, tao_in, pool=None, beta_ppm=20_000, limit=None, **kw): limit = ceil(marginal_after_buy * (1 + beta))
    on `pool` unless given explicitly."""

    def build(block: int, tao_in: int, p: PoolState | None = None, *, beta_ppm: int = 20_000, limit: int | None = None,
              **kw: Any) -> OrderIntent:
        if limit is None:
            m = marginal_after_buy(p if p is not None else _pool(), Rao(tao_in))
            limit = -((-m * (PPM + beta_ppm)) // PPM)
        return _intent(OrderKind.ADD_STAKE_LIMIT, block, tao_in=tao_in, limit=limit, **kw)

    return build


@pytest.fixture(scope="session")
def sell() -> Callable[..., OrderIntent]:
    """sell(block, alpha_in=0, pool=None, full=False, beta_ppm=50_000, limit=None, **kw): limit =
    floor(marginal_after_sell * (1 - beta)) on `pool` (needs alpha_in > 0) unless given; full exits default to 0."""

    def build(block: int, alpha_in: int = 0, p: PoolState | None = None, *, full: bool = False, beta_ppm: int = 50_000,
              limit: int | None = None, kind: OrderKind = OrderKind.REMOVE_STAKE_LIMIT, **kw: Any) -> OrderIntent:
        if limit is None:
            if alpha_in > 0:
                m = marginal_after_sell(p if p is not None else _pool(), AlphaRao(alpha_in))
                limit = m * (PPM - beta_ppm) // PPM
            else:
                limit = 0
        return _intent(kind, block, alpha_in=alpha_in, full=full, limit=limit, **kw)

    return build


@pytest.fixture(scope="session")
def move() -> Callable[..., OrderIntent]:
    def build(block: int, dest: Hotkey = HK_B, *, alpha_in: int = 0, full: bool = True, **kw: Any) -> OrderIntent:
        return _intent(OrderKind.MOVE_STAKE, block, alpha_in=alpha_in, full=full, dest_hotkey=dest, **kw)

    return build


class Harness:
    """A minimal single-book Runner: journal + order FSM + ledger + venue.observe on every commit."""

    def __init__(self, venue: Any, book: BookId = BOOK) -> None:
        self.venue = venue
        self.book = book
        self.journal: list[JournalEvent] = []
        self.records: dict[tuple[str, int], OrderRecord] = {}
        self.balances: dict[tuple[str, str], int] = {}
        self.max_drain = 50

    # ---- the commit point
    def commit(self, ev: JournalEvent) -> None:
        idem = ev.idem()
        if idem is not None and any(e.idem() == idem for e in self.journal):
            raise AssertionError(f"duplicate idempotency key {idem}")
        self._fold(ev)
        self.journal.append(ev)
        self.venue.observe(ev)

    def _to(self, oid: str, attempt: int, state: OrderState) -> None:
        k = (oid, attempt)
        self.records[k] = self.records[k].to(state)

    def _fold(self, ev: JournalEvent) -> None:
        if isinstance(ev, OrderIntended):
            self.records[(ev.intent.order_id, ev.intent.attempt)] = OrderRecord(ev.intent)
        elif isinstance(ev, OrderCancelled):
            self._to(ev.order_id, ev.attempt, OrderState.CANCELLED)
        elif isinstance(ev, SubmitStarted):
            self._to(ev.order_id, ev.attempt, OrderState.SUBMITTING)
        elif isinstance(ev, VenueAck):
            self._to(ev.order_id, ev.attempt, OrderState.SUBMITTED)
        elif isinstance(ev, SubmitUnknown):
            self._to(ev.order_id, ev.attempt, OrderState.UNKNOWN)
        elif isinstance(ev, FillReported):
            self._to(ev.fill.order_id, ev.fill.attempt, OrderState.FILLED)
            apply_txn(self.balances, fill_txn(ev.fill))
        elif isinstance(ev, OrderFailed):
            self._to(ev.order_id, ev.attempt, OrderState.EXPIRED if ev.expired else OrderState.FAILED)
            apply_txn(self.balances, fail_txn(ev))
        elif isinstance(ev, CapitalChanged):
            apply_txn(self.balances, capital_txn(ev))

    # ---- helpers
    def bal(self, account: str, unit: str = TAO_UNIT) -> int:
        return self.balances.get((account, unit), 0)

    def capital(self, cash: int, fee_float: int = 1 * TAO, block: int = 0, memo: str = "initial") -> None:
        self.commit(CapitalChanged(book=self.book, block=Block(block), cash_delta=cash, fee_float_delta=fee_float, memo=memo))

    def observe_snapshot(self, s: ChainSnapshot, lag: int = 3) -> None:
        health = replace(HealthObs.nominal(), finality_lag_blocks=lag)
        self.commit(SnapshotObserved(block=s.block, block_hash=s.block_hash, digest=s.digest, plan=ReadPlan.FULL,
                                     ts_ms=s.timestamp_ms, health=health))

    def place(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent:
        """OrderIntended -> reserve -> SubmitStarted -> submit -> commit the result (returned)."""
        self.commit(OrderIntended(intent))
        delegate, nonce, era_end = _run(self.venue.reserve(intent, now))
        self.commit(SubmitStarted(book=self.book, order_id=intent.order_id, attempt=intent.attempt, delegate=delegate,
                                  nonce=nonce, era_end=era_end))
        ev: JournalEvent = _run(self.venue.submit(intent, now))
        self.commit(ev)
        return ev

    def drain(self, s: ChainSnapshot) -> list[JournalEvent]:
        out: list[JournalEvent] = []
        for _ in range(self.max_drain):
            ev = _run(self.venue.advance(self.venue.mark_to(s)))
            if ev is None:
                return out
            self.commit(ev)
            out.append(ev)
        raise AssertionError("advance() did not run dry")

    def tick(self, s: ChainSnapshot, lag: int = 3) -> list[JournalEvent]:
        self.observe_snapshot(s, lag)
        return self.drain(s)

    def fills(self) -> list[FillReported]:
        return [e for e in self.journal if isinstance(e, FillReported)]

    def state(self, intent: OrderIntent) -> OrderState:
        return self.records[(intent.order_id, intent.attempt)].state

    def check_ledger(self) -> None:
        """Every unit sums to zero; venue cash == ledger cash >= 0."""
        sums: dict[str, int] = {}
        for (_, unit), amount in self.balances.items():
            sums[unit] = sums.get(unit, 0) + amount
        assert all(v == 0 for v in sums.values()), sums
        assert self.venue.cash == self.bal("cash") >= 0


@pytest.fixture(scope="session")
def harness() -> type[Harness]:
    return Harness
