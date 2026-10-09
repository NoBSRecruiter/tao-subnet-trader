"""core.codec: canonical JSON, exact Decimal, type-driven decode, journal event round trips (DESIGN.md 5.11, 10.1/10.2)."""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.core import codec
from taotrader.core.config import BookCfg, RpcCfg, RunCfg, SleeveCfg
from taotrader.core.events import (
    REGISTRY,
    CapitalChanged,
    CarrierFeeSettled,
    ChainEvent,
    ChainEventKind,
    ChainEventObserved,
    ConfigApplied,
    DecisionTrace,
    DeregSettled,
    FillReported,
    HealthObs,
    JournalEvent,
    ModeChanged,
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
from taotrader.core.orders import FailReason, Fill, OrderIntent, OrderKind, Urgency, make_order_id
from taotrader.core.protocols import BookView, RouterState, SleeveStats
from taotrader.core.signals import RiskAction, Signal, SignalKind
from taotrader.core.state import ChainSnapshot, HotkeyIdx, Quality, ReadPlan
from taotrader.core.units import (
    PPM,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    Mode,
    NetUid,
    OrderId,
    PositionKey,
    Ppm,
    PpmPerDay,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.core.views import EmissionView, Feat, FeatureFrame, PruneView, RouterCandidate

# ------------------------------------------------------------------------------------------------- strategies
U64 = 2**64 - 1
blocks = st.integers(0, 20_000_000).map(Block)
netuids = st.integers(0, 65_535).map(NetUid)
hotkeys = st.binary(min_size=32, max_size=32).map(lambda b: Hotkey("0x" + b.hex()))
hashes = st.binary(min_size=32, max_size=32).map(lambda b: BlockHash("0x" + b.hex()))
books = st.sampled_from(["b1", "paper-carry", "bt.ew_total"]).map(BookId)
strategy_ids = st.sampled_from(["carry", "momentum", "lcw", "baseline.ew_total", "risk.router"]).map(StrategyId)
order_ids = st.binary(min_size=12, max_size=12).map(lambda b: OrderId(b.hex()))
amounts = st.integers(0, 10**22)
signed = st.integers(-(10**22), 10**22)
texts = st.text(max_size=40)
# exact decimals well beyond the 28-digit default context, including tiny and huge exponents
decimals = st.decimals(allow_nan=False, allow_infinity=False, places=None).filter(lambda d: d.is_finite()) | st.builds(
    lambda m, e: Decimal(m).scaleb(e), st.integers(-(10**45), 10**45), st.integers(-70, 30))
keys = st.builds(SubnetKey, netuids, blocks)


@st.composite
def attributions(draw: st.DrawFn) -> tuple[tuple[StrategyId, Ppm], ...]:
    n = draw(st.integers(1, 3))
    ids = draw(st.lists(strategy_ids, min_size=n, max_size=n, unique=True))
    cuts = sorted(draw(st.lists(st.integers(0, PPM), min_size=n - 1, max_size=n - 1)))
    parts = [b - a for a, b in zip([0, *cuts], [*cuts, PPM], strict=True)]
    return tuple((i, Ppm(p)) for i, p in zip(ids, parts, strict=True))


@st.composite
def intents(draw: st.DrawFn) -> OrderIntent:
    kind = draw(st.sampled_from(list(OrderKind)))
    key, hotkey, book, block = draw(keys), draw(hotkeys), draw(books), draw(blocks)
    attempt = draw(st.integers(0, 5))
    tao_in, alpha_in, full, dest_key, dest_hk = 0, 0, False, None, None
    if kind is OrderKind.ADD_STAKE_LIMIT:
        tao_in = draw(st.integers(1, 10**15))
    elif kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        full = draw(st.booleans())
        alpha_in = draw(st.integers(0 if full else 1, 10**18))
    else:
        dest_hk = draw(hotkeys)
        dest_key = draw(keys) if kind is OrderKind.MOVE_STAKE_LIMIT else draw(st.none() | keys)
        full = draw(st.booleans())
        alpha_in = draw(st.integers(0, 10**18))
    return OrderIntent(
        order_id=make_order_id("run", book, block, key, hotkey, kind, attempt), attempt=attempt, book=book,
        created_block=block, kind=kind, key=key, hotkey=hotkey, tao_in=Rao(tao_in), alpha_in=AlphaRao(alpha_in),
        full_position=full, limit_price=PriceRao(draw(st.integers(0, U64))), allow_partial=draw(st.booleans()),
        shielded=draw(st.booleans()), valid_until=draw(blocks), expected_out=draw(amounts),
        urgency=draw(st.sampled_from(list(Urgency))), attribution=draw(attributions()), reason=draw(texts),
        dest_key=dest_key, dest_hotkey=dest_hk)


fills = st.builds(
    Fill, fill_id=texts, order_id=order_ids, attempt=st.integers(0, 9), book=books, block=blocks,
    kind=st.sampled_from(list(OrderKind)), key=keys, hotkey=hotkeys, tao=amounts.map(Rao), alpha=amounts.map(AlphaRao),
    shares=decimals, swap_fee=amounts, author_fee_tao=amounts.map(Rao), tx_fee=amounts.map(Rao), d_pool_tao=signed,
    d_pool_alpha=signed, spot_before=amounts.map(PriceRao), shortfall_ppm=st.integers(-PPM, PPM).map(Ppm),
    complete=st.booleans(), exact_block=st.booleans(), dest_key=st.none() | keys, dest_hotkey=st.none() | hotkeys,
    dest_shares=st.none() | decimals)
chain_events = st.builds(
    ChainEvent, kind=st.sampled_from(list(ChainEventKind)), block=blocks, key=st.none() | keys,
    hotkey=st.none() | hotkeys, flag=st.none() | st.booleans(), amount=st.none() | signed,
    frac_ppm=st.none() | st.integers(-PPM, 10 * PPM).map(Ppm), name=st.none() | texts, old=st.none() | texts,
    new=st.none() | texts)
healths = st.builds(HealthObs, st.integers(0, 10**4), st.integers(0, 10**6), st.integers(0, 9), st.integers(0, 10**4),
                    st.integers(0, 10**4))
signals = st.builds(
    Signal, strategy=strategy_ids, key=keys, asof=blocks, kind=st.sampled_from(list(SignalKind)),
    weight_ppm=st.integers(0, PPM).map(Ppm), edge_ppm_day=st.integers(-PPM, PPM).map(PpmPerDay),
    alpha_h_ppm=st.integers(-PPM, PPM).map(Ppm), max_size_rao=st.none() | amounts.map(Rao),
    horizon_blocks=st.integers(0, 10**6), urgency=st.sampled_from(list(Urgency)), hotkey_pref=st.none() | hotkeys,
    declares_dilution=st.booleans(), reasons=st.lists(texts, max_size=3).map(tuple))
risk_actions = st.builds(RiskAction, rule=texts, key=st.none() | keys,
                         action=st.sampled_from(["CLAMP", "FORCE_EXIT", "VETO_ENTRY", "MONITOR"]), detail=texts)

EVENT_STRATEGIES: dict[type[JournalEvent], st.SearchStrategy[Any]] = {
    SnapshotObserved: st.builds(SnapshotObserved, blocks, hashes, texts, st.sampled_from(list(ReadPlan)),
                                st.integers(0, 2**63), healths),
    OperatorCommand: st.builds(OperatorCommand, blocks, texts, texts, texts),
    CapitalChanged: st.builds(CapitalChanged, books, blocks, signed, signed, texts),
    ConfigApplied: st.builds(ConfigApplied, blocks, texts, texts, texts),
    ModelDriftObserved: st.builds(ModelDriftObserved, blocks, texts, st.none() | netuids, signed),
    ChainEventObserved: st.builds(ChainEventObserved, chain_events),
    YieldAccrued: st.builds(YieldAccrued, books, keys, hotkeys, blocks, decimals, decimals, signed),
    DeregSettled: st.builds(DeregSettled, books, keys, hotkeys, blocks, amounts.map(AlphaRao), amounts.map(Rao),
                            st.sampled_from(["formula", "fixed:350000", "observed"])),
    DecisionTrace: st.builds(
        DecisionTrace, books, blocks, st.lists(strategy_ids, max_size=3).map(tuple), st.lists(signals, max_size=3).map(tuple),
        st.lists(risk_actions, max_size=3).map(tuple), st.sampled_from(list(Mode)),
        st.lists(st.tuples(strategy_ids, st.binary(max_size=64)), max_size=3).map(tuple), texts, st.integers(0, 50),
        texts, amounts.map(Rao), st.lists(st.tuples(strategy_ids, amounts.map(Rao)), max_size=3).map(tuple)),
    ModeChanged: st.builds(ModeChanged, books, blocks, st.sampled_from(list(Mode)), texts),
    SleeveTransfer: st.builds(SleeveTransfer, books, blocks, keys, strategy_ids, strategy_ids, decimals,
                              amounts.map(Rao), amounts.map(PriceRao)),
    OrderIntended: st.builds(OrderIntended, intents()),
    OrderCancelled: st.builds(OrderCancelled, books, order_ids, st.integers(0, 9), blocks, texts),
    SubmitStarted: st.builds(SubmitStarted, books, order_ids, st.integers(0, 9), texts, st.none() | st.integers(0, 2**32),
                             st.none() | blocks),
    VenueAck: st.builds(VenueAck, books, order_ids, st.integers(0, 9), blocks, blocks, texts, texts),
    SubmitUnknown: st.builds(SubmitUnknown, books, order_ids, st.integers(0, 9), texts),
    FillReported: st.builds(FillReported, fills),
    OrderFailed: st.builds(OrderFailed, books, order_ids, st.integers(0, 9), blocks, st.sampled_from(list(FailReason)),
                           amounts.map(Rao), st.booleans(), st.booleans(), texts),
    CarrierFeeSettled: st.builds(CarrierFeeSettled, books, order_ids, st.integers(0, 9), blocks, amounts.map(Rao),
                                 st.sampled_from(["never_included", "carrier_only", "inner_included"])),
    ReconAdjusted: st.builds(ReconAdjusted, books, blocks, signed, signed,
                             st.lists(st.tuples(st.builds(PositionKey, keys, hotkeys), decimals), max_size=3).map(tuple),
                             texts),
    QuarantineCleared: st.builds(QuarantineCleared, books, blocks, texts),
}
any_event = st.one_of(*EVENT_STRATEGIES.values())


# ------------------------------------------------------------------------------------------------- Decimal rules
@pytest.mark.parametrize(("value", "text"), [
    ("1.2300", "1.23"), ("100", "100"), ("1E+2", "100"), ("-0.000", "0"), ("-0", "0"), ("0.000001", "0.000001"),
    ("1E-30", "0." + "0" * 29 + "1"), ("-12.50", "-12.5"), ("0", "0"), ("5E-1", "0.5"),
])
def test_decimal_text_is_exact_and_stripped(value: str, text: str) -> None:
    assert codec.encode(Decimal(value)) == text
    assert codec.decode(Decimal, text) == Decimal(value)


def test_decimal_normalize_trap() -> None:
    """A 60-digit share count survives exactly; Decimal.normalize() would round it to the 28-digit context."""
    shares = Decimal("123456789012345678901234567890.123456789012345678901234567891")
    with localcontext() as ctx:
        ctx.prec = 28
        assert shares.normalize() != shares                     # the trap the codec must avoid
        enc = codec.encode(shares)                              # independent of the ambient context
    assert enc == "123456789012345678901234567890.123456789012345678901234567891"
    assert codec.decode(Decimal, enc) == shares
    idx = HotkeyIdx(Hotkey("0x" + "11" * 32), AlphaRao(10**18), shares)
    assert codec.decode(HotkeyIdx, json.loads(codec.canonical_bytes(idx))) == idx


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_decimals_rejected(bad: str) -> None:
    with pytest.raises(codec.CodecError):
        codec.encode(Decimal(bad))
    with pytest.raises(codec.CodecError):
        codec.decode(Decimal, bad)


@given(decimals)
def test_decimal_round_trip_property(d: Decimal) -> None:
    assert codec.decode(Decimal, json.loads(codec.canonical_bytes(d))) == d


# ------------------------------------------------------------------------------------------------- canonical form
def test_canonical_bytes_format_and_ordering() -> None:
    sig = Signal(StrategyId("carry"), SubnetKey(NetUid(5), Block(10)), Block(20), SignalKind.TARGET,
                 reasons=("b", "a"))
    raw = codec.canonical_bytes(sig)
    assert b" " not in raw
    obj = json.loads(raw)
    assert list(obj) == sorted(obj)
    assert obj["kind"] == "target" and obj["urgency"] == 1 and obj["max_size_rao"] is None
    assert obj["reasons"] == ["b", "a"]                         # tuples keep their order
    assert codec.encode(frozenset({"z", "a", "m"})) == ["a", "m", "z"]
    assert codec.encode(b"\x01\xff") == "0x01ff"
    assert codec.encode(Quality.TA_PRICE | Quality.REFINED) == 33
    assert codec.encode(10**40) == 10**40


def test_digest_regression_vector() -> None:
    """Pins the canonical byte format: any change here is a journal format change (ADR + KIND version bump)."""
    ev = CapitalChanged(BookId("b1"), Block(9_240_388), 100 * 10**9, 10**9, "seed")
    assert codec.canonical_bytes(ev) == (b'{"block":9240388,"book":"b1","cash_delta":100000000000,'
                                         b'"fee_float_delta":1000000000,"memo":"seed"}')
    assert codec.digest(ev) == codec.digest(CapitalChanged(BookId("b1"), Block(9_240_388), 100 * 10**9, 10**9, "seed"))
    assert len(codec.digest(ev)) == 32 and len(codec.digest(ev, 32)) == 64


def test_non_finite_float_rejected() -> None:
    with pytest.raises(codec.CodecError):
        codec.canonical_bytes(float("nan"))
    with pytest.raises(codec.CodecError):
        codec.encode(float("inf"))


def test_unencodable_type_rejected() -> None:
    with pytest.raises(codec.CodecError):
        codec.encode(object())


def test_mapping_with_dataclass_keys_round_trips() -> None:
    k1, k2 = SubnetKey(NetUid(9), Block(1)), SubnetKey(NetUid(2), Block(5))
    feat = _feat(k1)
    frame = FeatureFrame(
        block=Block(100), warm=True, feats={k1: feat, k2: dataclasses.replace(feat, key=k2)},
        prune=PruneView(True, k2, True, (k2, k1), 0.01, 100, False, 14_300, 1.5, ((7_200, Ppm(83_000)),), True,
                        ((Block(200), k1),)),
        emission=EmissionView(0.0082624, 32, 1.185, True, 1e-7, True), regime_id="gate_rank32",
        universe_eligible=40, beta_horizon_blocks=5, digest="d")
    enc = codec.encode(frame)
    assert isinstance(enc["feats"], list) and [p[0] for p in enc["feats"]] == [codec.encode(k2), codec.encode(k1)]
    assert codec.decode(FeatureFrame, json.loads(codec.canonical_bytes(frame))) == frame


def _feat(key: SubnetKey) -> Feat:
    cand = RouterCandidate(Hotkey("0x" + "ab" * 32), 5_500, 0, 0, Ppm(1_000_000), True, 3, True, False, True)
    return Feat(key=key, spot=0.00134, pool_tao=587.2, k_w=2.0, ret_1h=None, ret_1d=-0.01, ret_7d=0.05, sigma_d=None,
                fast_ema_gap=0.0, ema_gap=-0.02, flow_1h=None, flow_1d=0.001, flow_7d=None, flow_z_1d=-0.3,
                emis_tao_day=0.0, chain_buy_day=0.0, obs_emis_tao_day=0.0, gate_keep=0.03, burn_adj_rank=None,
                ema_rank_desc=90, rp=0.479, sell_push_day=0.016, cb_push_day=0.0, escrow_frac=0.112,
                a_earn_alpha=343_198.0, yield_cf_gross_day=0.00448, router_candidates=(cand,),
                best_candidate=cand.hotkey, yield_net_day=0.0044, a_earn_growth_day=0.008, age_reg_blocks=900_000,
                since_start_blocks=899_000, immune=False, immune_until=Block(1), prune_rank=1, rho=1.0,
                t_star_stress_blocks=None, launch_flags=frozenset({"YOUNG_IMMUNE", "BURNING"}),
                beta_entry_ppm=Ppm(3_000), beta_exit_ppm=Ppm(6_000), owner_sold_6h_frac=None,
                owner_liquid_frac=0.05, top_holder_frac=0.4)


def test_snapshot_round_trip_skips_private_index(make_subnet: Any, make_snapshot: Any, hk: Any) -> None:
    s1 = make_subnet(1, 100, hotkeys=(HotkeyIdx(hk(1), AlphaRao(5), Decimal("4.5"), earns=True),),
                     quality=Quality.CARRIED | Quality.NO_YIELD_IDX, first_emission_block=None)
    snap: ChainSnapshot = make_snapshot(9_000_000, (make_subnet(92, 200), s1))
    enc = codec.encode(snap)
    assert "_idx" not in enc
    back = codec.decode(ChainSnapshot, json.loads(codec.canonical_bytes(snap)))
    assert back == snap and back.by_netuid(92) == snap.by_netuid(92)
    assert back.get(SubnetKey(NetUid(1), Block(100))) is not None


def test_config_round_trip_with_params() -> None:
    cfg = RunCfg("r1", RunMode.PAPER, (BookCfg(BookId("b1"), Rao(10**11), Rao(10**9), (
        SleeveCfg(StrategyId("carry"), Stage.PAPER, Ppm(400_000), MappingProxyType({"h_eval_days": 5, "x": [1, 2]})),)),),
        RpcCfg(("wss://a",), ("https://b",)))
    back = codec.decode(RunCfg, json.loads(codec.canonical_bytes(cfg)))
    assert back.books[0].sleeves[0].params == {"h_eval_days": 5, "x": [1, 2]}
    assert codec.canonical_bytes(back) == codec.canonical_bytes(cfg)


def test_bookview_and_router_state_round_trip(hk: Any) -> None:
    k = SubnetKey(NetUid(3), Block(9))
    view = BookView(orders=(), recent_fills=(), chase=((k, 1, PriceRao(1_000)),), delegates_free=("sim0", "sim1"),
                    delegate_locked_until=(("sim2", Block(50)),), fail_counts_600=((NetUid(3), 2),),
                    fail_count_600_book=2, cooldowns=((k, "owner", Block(99)),), entries_halted_until=None,
                    recent_forced_exits=((Block(5), k, "prune_A", Urgency.EMERGENCY),),
                    nav_liq_daily=((Block(7_200), Rao(10**11)),),
                    sleeve_stats=(SleeveStats(StrategyId("carry"), "ACTIVE", Ppm(0), Ppm(10**6), Ppm(10**6), None, None, 3),),
                    router=RouterState(choice=((k, hk(1)),), fail_epochs=((k, 1),), beat_epochs=((k, hk(2), 1),)),
                    dissolving=(k,))
    assert codec.decode(BookView, json.loads(codec.canonical_bytes(view))) == view


# ------------------------------------------------------------------------------------------------- strict decode
def test_decode_is_strict() -> None:
    good = codec.encode(SubnetKey(NetUid(1), Block(2)))
    assert codec.decode(SubnetKey, good) == SubnetKey(NetUid(1), Block(2))
    for bad in ({"netuid": True, "reg_at": 2}, {"netuid": 1.0, "reg_at": 2}, {"netuid": "1", "reg_at": 2},
                {"netuid": 1}, {"netuid": 1, "reg_at": 2, "extra": 0}, [1, 2]):
        with pytest.raises(codec.CodecError):
            codec.decode(SubnetKey, bad)
    with pytest.raises(codec.CodecError):
        codec.decode(Mode, 9)
    with pytest.raises(codec.CodecError):
        codec.decode(SignalKind, 1)
    with pytest.raises(codec.CodecError):                       # OrderIntent.__post_init__ rejects it
        data = codec.encode(_valid_intent())
        data["attribution"] = [["carry", 1]]
        codec.decode(OrderIntent, data)


def test_decode_uses_field_defaults_for_missing_optional_fields() -> None:
    data = codec.encode(OrderFailed(BookId("b"), OrderId("o"), 0, Block(1), FailReason.OTHER, Rao(0)))
    for k in ("expired", "exact_block", "detail"):
        del data[k]
    ev = codec.decode(OrderFailed, data)
    assert ev.expired is False and ev.exact_block is True and ev.detail == ""


def _valid_intent() -> OrderIntent:
    key, hot = SubnetKey(NetUid(92), Block(8_355_590)), Hotkey("0x" + "cd" * 32)
    return OrderIntent(make_order_id("run", BookId("b"), Block(1), key, hot, OrderKind.ADD_STAKE_LIMIT, 0), 0,
                       BookId("b"), Block(1), OrderKind.ADD_STAKE_LIMIT, key, hot, Rao(10**9), AlphaRao(0), False,
                       PriceRao(1_400_000), False, True, Block(6), 7 * 10**11, Urgency.NORMAL,
                       ((StrategyId("carry"), Ppm(PPM)),), "entry")


# ------------------------------------------------------------------------------------------------- journal events
def test_every_registered_event_has_a_strategy() -> None:
    assert set(EVENT_STRATEGIES) == set(REGISTRY.values())
    assert len(REGISTRY) == 21


@given(any_event)
def test_every_journal_event_round_trips(ev: JournalEvent) -> None:
    kind, version, payload = codec.encode_event(ev)
    assert kind == type(ev).KIND and version == type(ev).VERSION == 1
    back = codec.decode_event(kind, version, payload)
    assert back == ev
    assert codec.canonical_bytes(back) == payload               # canonical: re-encoding is byte-identical
    assert codec.decode(type(ev), json.loads(payload)) == ev
    assert back.idem() == ev.idem()


def test_unknown_kind_and_newer_version_raise() -> None:
    payload = codec.canonical_bytes(QuarantineCleared(BookId("b"), Block(1), "ok"))
    with pytest.raises(codec.CodecError, match="unknown journal kind"):
        codec.decode_event("no_such_kind", 1, payload)
    with pytest.raises(codec.CodecError, match="newer"):
        codec.decode_event("quarantine_cleared", 2, payload)
    with pytest.raises(codec.CodecError):
        codec.decode_event("quarantine_cleared", 0, payload)


def test_upcaster_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A v1 payload is upcast when the class is at v2 (simulated by bumping VERSION for this test only)."""
    old = json.loads(codec.canonical_bytes(ConfigApplied(Block(1), "c", "k", "p")))
    old["prereg_hash"] = "legacy"
    monkeypatch.setattr(ConfigApplied, "VERSION", 2)
    with pytest.raises(codec.CodecError, match="no upcaster"):
        codec.decode_event("config_applied", 1, json.dumps(old))
    monkeypatch.setitem(codec.UPCASTERS, ("config_applied", 1), lambda d: {**d, "prereg_hash": "upcast:" + d["prereg_hash"]})
    ev = codec.decode_event("config_applied", 1, json.dumps(old))
    assert isinstance(ev, ConfigApplied) and ev.prereg_hash == "upcast:legacy"
    assert codec.decode_event("config_applied", 2, json.dumps(old)) == ConfigApplied(Block(1), "c", "k", "legacy")


def test_unregistered_event_cannot_be_encoded_as_event() -> None:
    class Rogue(JournalEvent):
        pass

    with pytest.raises(codec.CodecError):
        codec.encode_event(Rogue())
