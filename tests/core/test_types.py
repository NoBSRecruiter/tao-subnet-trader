"""core units/fixed/state/signals/events/config: arithmetic helpers, pool and share math, generation matching,
monotone TargetBook.reduced, the journal registry and the config defaults the design relies on."""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.core.config import BookCfg, ExecCfg, LiveCfg, RiskCfg
from taotrader.core.events import REGISTRY, CapitalChanged, ChainEvent, ChainEventKind, HealthObs, journal_event
from taotrader.core.fixed import DEC, EXACT, floor_int, frac_ppm, mul_ppm, to_ppm
from taotrader.core.orders import Urgency
from taotrader.core.signals import TargetBook, TargetPosition
from taotrader.core.state import ChainSnapshot, HotkeyIdx, PoolKind, PoolState
from taotrader.core.units import (
    BLOCKS_PER_DAY,
    DEFAULT_TAKE_U16,
    FEE_DEN,
    PPM,
    AlphaRao,
    Block,
    BookId,
    Hotkey,
    LogicalTime,
    Mode,
    NetUid,
    Phase,
    Ppm,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)


# ------------------------------------------------------------------------------------------------- fixed
@pytest.mark.parametrize(("f", "ppm"), [(0.1, 100_000), (0.0000015, 2), (0.0000025, 2), (-0.5, -500_000),
                                        (1e-7, 0), (0.33, 330_000), (1.5, 1_500_000), (0.0, 0)])
def test_to_ppm_half_even_on_exact_repr(f: float, ppm: int) -> None:
    assert to_ppm(f) == ppm


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_to_ppm_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        to_ppm(bad)


def test_integer_helpers() -> None:
    assert floor_int(Decimal("-1.5")) == -2 and floor_int(Decimal("2.999")) == 2
    assert mul_ppm(10**9, 33_000) == 33_000_000 and mul_ppm(7, 500_000) == 3
    assert frac_ppm(1, 3) == 333_333 and frac_ppm(5, 0) == 0
    assert DEC.prec == 60 and DEC.rounding == ROUND_FLOOR
    assert EXACT.divide(Decimal(1), Decimal(2**64)) == Decimal("5.42101086242752217003726400434970855712890625E-20")
    assert EXACT.divide(Decimal(1_288_490), Decimal(2**32)) == Decimal("0.0002999999560415744781494140625")


def test_unit_constants() -> None:
    assert (BLOCKS_PER_DAY, FEE_DEN, DEFAULT_TAKE_U16, PPM) == (7_200, 65_535, 11_796, 1_000_000)
    assert 10 * 10**9 * 33 // FEE_DEN == 5_035_477                         # brief 2.2 fee vector


def test_ordering_of_keys_and_time() -> None:
    a, b = SubnetKey(NetUid(5), Block(10)), SubnetKey(NetUid(5), Block(11))
    assert a < b and sorted([b, a]) == [a, b] and len({a, SubnetKey(NetUid(5), Block(10))}) == 1
    assert LogicalTime(Block(5), Phase.EMIT) < LogicalTime(Block(5), Phase.VENUE) < LogicalTime(Block(6))
    assert Mode.FROZEN > Mode.EXITS_ONLY > Mode.CAUTION > Mode.NORMAL
    assert Stage.LIVE_ELIGIBLE > Stage.PAPER and RunMode("live_dry") is RunMode.LIVE_DRY


# ------------------------------------------------------------------------------------------------- state
def test_pool_spot_balanced_and_skewed(make_pool: object) -> None:
    p = PoolState(PoolKind.BALANCER, Rao(600 * 10**9), AlphaRao(300 * 10**9), 600 * 10**9, 300 * 10**9, 5 * 10**17, 33)
    assert p.spot() == Decimal(2) and p.spot_rao() == 2 * 10**9 and p.w_base_e18 == 5 * 10**17
    q = PoolState(PoolKind.BALANCER, Rao(600 * 10**9), AlphaRao(300 * 10**9), 600 * 10**9, 300 * 10**9, 4 * 10**17, 33)
    assert q.spot() == Decimal(3)                                           # (0.6 / 0.4) * 2: never hard-code 0.5
    s = p.shifted(10**9, -(10**8))
    assert (s.tao, s.alpha, s.px_tao, s.px_alpha, s.w_quote_e18) == (601 * 10**9, 299_900_000_000, 601 * 10**9,
                                                                       299_900_000_000, 5 * 10**17)


def test_pool_spot_rao_floors() -> None:
    p = PoolState(PoolKind.CP_REAL, Rao(10**9), AlphaRao(3 * 10**9), 10**9, 3 * 10**9, 5 * 10**17, 33)
    assert p.spot_rao() == 333_333_333


def test_hotkey_index_share_math(hk: object) -> None:
    h = HotkeyIdx(Hotkey("0x" + "56" * 32), AlphaRao(1_631_360), Decimal(1_000_000))
    assert h.index() == Decimal("1.63136")
    assert h.value_of(Decimal(500_000)) == 815_680
    assert h.shares_for(815_680) == Decimal(500_000)
    z = HotkeyIdx(Hotkey("0x" + "56" * 32), AlphaRao(0), Decimal(0))
    assert z.index() == 1 and z.value_of(Decimal("7.9")) == 7 and z.shares_for(5) == 5
    assert z.take_u16 == DEFAULT_TAKE_U16 and z.earns is False


def test_snapshot_generation_matching(make_subnet: object, make_snapshot: object) -> None:
    snap: ChainSnapshot = make_snapshot(9_000_000, (make_subnet(92, 100), make_subnet(1, 5)))  # type: ignore[operator]
    assert [int(s.key.netuid) for s in snap.subnets] == [1, 92]
    assert snap.by_netuid(92) is not None and snap.by_netuid(7) is None
    assert snap.get(SubnetKey(NetUid(92), Block(100))) is snap.by_netuid(92)
    assert snap.get(SubnetKey(NetUid(92), Block(99))) is None              # netuid reused: our asset is gone
    s = snap.by_netuid(92)
    assert s is not None and s.hotkey(Hotkey("0x" + "00" * 32)) is None
    assert "_idx" not in repr(snap)


# ------------------------------------------------------------------------------------------------- signals
def _book(values: list[int]) -> TargetBook:
    items = tuple(TargetPosition(SubnetKey(NetUid(i), Block(1)), Hotkey("0x" + "aa" * 32), Rao(v), Urgency.NORMAL,
                                 ((StrategyId("carry"), Ppm(PPM)),)) for i, v in enumerate(values))
    return TargetBook(Block(1), items)


@given(st.lists(st.integers(0, 10**13), min_size=1, max_size=6), st.data())
def test_target_book_reduced_is_monotone(values: list[int], data: st.DataObject) -> None:
    book = _book(values)
    i = data.draw(st.integers(0, len(values) - 1))
    key = book.items[i].key
    lower = data.draw(st.integers(0, values[i]))
    out = book.reduced(key, Rao(lower), "liquidity.vcap", Urgency.URGENT)
    t = out.get(key)
    assert t is not None and t.value_rao == lower and t.reasons[-1] == "liquidity.vcap" and t.urgency is Urgency.URGENT
    assert [x.value_rao for j, x in enumerate(out.items) if j != i] == [v for j, v in enumerate(values) if j != i]
    with pytest.raises(ValueError, match="increase"):
        book.reduced(key, Rao(values[i] + 1), "nope")


def test_target_book_reduced_keeps_urgency_by_default() -> None:
    book = _book([100])
    out = book.reduced(book.items[0].key, Rao(50), "r")
    assert out.items[0].urgency is Urgency.NORMAL and book.items[0].value_rao == 100   # immutable original
    assert book.get(SubnetKey(NetUid(9), Block(9))) is None


# ------------------------------------------------------------------------------------------------- events
def test_journal_registry_and_idempotency_keys() -> None:
    kinds = {"snapshot_observed", "operator_command", "capital_changed", "config_applied", "model_drift_observed",
             "chain_event", "yield_accrued", "dereg_settled", "decision_trace", "mode_changed", "sleeve_transfer",
             "order_intended", "order_cancelled", "submit_started", "venue_ack", "submit_unknown", "fill_reported",
             "order_failed", "carrier_fee_settled", "recon_adjusted", "quarantine_cleared"}
    assert set(REGISTRY) == kinds and len(REGISTRY) == 21
    for kind, cls in REGISTRY.items():
        assert kind == cls.KIND and cls.VERSION == 1
    assert CapitalChanged(BookId("b"), Block(1), 1, 0, "m").idem() == "capital:b:m"
    assert ChainEvent(ChainEventKind.REGISTERED, Block(1)).flag is None


def test_duplicate_journal_kind_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        @journal_event("capital_changed")
        class Dup(CapitalChanged):
            pass


def test_nominal_health_matches_exec_defaults() -> None:
    assert HealthObs.nominal().finality_lag_blocks == ExecCfg().finality_lag_blocks


# ------------------------------------------------------------------------------------------------- config
def test_config_defaults_satisfy_the_cross_field_rule() -> None:
    r, e = RiskCfg(), ExecCfg()
    assert r.unwind_exec_blocks == e.finality_lag_blocks + e.latency_blocks == 5
    assert r.unwind_exec_blocks * (1 + r.unwind_retries) == 15                 # U = 15 (section 3.3)
    assert LiveCfg().enabled is False and LiveCfg().mode == "plan_only" and LiveCfg().risk_exits_when_unarmed is False
    b = BookCfg(BookId("b"), Rao(10**11), Rao(10**9), ())
    assert b.risk == RiskCfg() and b.exec == ExecCfg() and b.dereg_model == "formula"
