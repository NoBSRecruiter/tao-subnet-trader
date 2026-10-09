"""WP9 momentum (DESIGN.md section 2.2): the TEMPORARY cost gate rejects a trade the PERSISTENT bound would admit;
rank-gauss S; entries, caps and the universe; exits; deferred flags; memory round trip."""
from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal
from statistics import NormalDist
from types import SimpleNamespace
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.orders import OrderIntent, OrderKind, OrderRecord, OrderState, Urgency, make_order_id
from taotrader.core.protocols import Strategy
from taotrader.core.signals import SignalKind
from taotrader.core.units import PPM, AlphaRao, Block, BookId, Ppm, PriceRao, Rao, StrategyId
from taotrader.protocol.amm import ImpactBound
from taotrader.strategies.base import rank_gauss
from taotrader.strategies.momentum import (
    MomentumMemory,
    MomentumParams,
    MomentumStrategy,
    MomKeyState,
    alpha_days,
    net_edge,
)

TX = 1_028_000 + 837_000
Z95 = NormalDist().inv_cdf(0.95)


def _specs(sx: SimpleNamespace, top_tao: float = 2_500, n: int = 10, **top_kw: Any) -> list[Any]:
    """n names (netuid 11..10+n) with ascending r_24h / r_7d / nf_24h; the last (netuid 10+n) ranks first."""
    out = []
    for i in range(n):
        netuid = 11 + i
        feat = {"ret_1d": 0.001 * (i + 1), "ret_7d": 0.01 * (i + 1), "flow_1d": 0.001 * (i + 1),
                "router_candidates": (sx.candidate(score_ppm_day=2_000),)}
        kw: dict[str, Any] = {"state": {"alpha_out_emission": AlphaRao(10**9)}}
        if i == n - 1:
            feat["pool_tao"] = float(top_tao)
            kw["tao_tao"] = top_tao
            for k, v in top_kw.items():
                if k == "feat":
                    feat.update(v)
                else:
                    kw[k] = v
        out.append(sx.Spec(netuid, reg_at=8_000_000 + netuid, feat=feat, **kw))
    return out


def _key(sx: SimpleNamespace, netuid: int) -> Any:
    return sx.Spec(netuid, reg_at=8_000_000 + netuid).key


# ------------------------------------------------------------------------------------------------- the cost gate
def test_temporary_gate_rejects_a_trade_the_persistent_bound_would_admit(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    strat = MomentumStrategy({"b_s_per_z_day": 0.0011})
    ev = strat.evaluate(m.ctx(sid="momentum"))
    top = _key(sx, 20)
    row = ev.rows[top]
    p = strat.params
    # S of the top name: rank-gauss of the sum of three top rank-gauss scores = Phi^-1(0.95)
    assert row.rank == 1 and row.s_score == pytest.approx(Z95)
    assert ev.m_u == 0.0                                          # no 14-day history in the store
    # EH = H_e (m_u + y_n) + b_S S alpha_days(H_e), y_n = min(closed form, realised 0.2 %/day)
    eh = 2.0 * (0.0 + 0.002) + 0.0011 * Z95 * alpha_days(2.0, 0.25)
    assert row.eh == pytest.approx(eh, rel=1e-12)
    phi = 2 * 33 / 65_535 + 2 * 0.0005
    edge_ppm = round((eh - phi) * 1e6)
    t = 2_500 * sx.TAO
    assert abs(row.v_rao - t * edge_ppm // (4 * 1_500_000)) <= t // (4 * 1_500_000)
    assert row.v_rao >= 1.5 * sx.TAO
    pool = m.snap(sx.B).get(top).pool
    temporary = net_edge(row.eh, pool, row.v_rao, ImpactBound.TEMPORARY, TX, p.lat_frac)
    persistent = net_edge(row.eh, pool, row.v_rao, ImpactBound.PERSISTENT, TX, p.lat_frac)
    assert temporary is not None and persistent is not None
    assert row.net_edge == pytest.approx(temporary)
    assert persistent >= p.net_margin_frac > temporary            # PERSISTENT would admit; the gate rejects
    # every other entry condition holds: only the cost gate refuses the trade
    assert row.in_universe and (row.s_score or 0.0) >= p.s_in and (row.r_24h or 0) > 0 and row.rank <= p.k + p.k_buf
    assert strat._entry_ok(row, p.s_in) == ("net_edge<margin",)
    assert not [s for s in ev.signals if s.kind is SignalKind.TARGET]


def test_a_trade_clearing_the_temporary_gate_is_entered(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    ev = MomentumStrategy({"b_s_per_z_day": 0.004}).evaluate(m.ctx(sid="momentum"))
    targets = sorted(int(s.key.netuid) for s in ev.signals if s.kind is SignalKind.TARGET)
    assert targets == [19, 20]                                   # S >= S_in = 1 for ranks 1 and 2 only; <= 2 per cycle
    sig = next(s for s in ev.signals if int(s.key.netuid) == 20)
    row = ev.rows[_key(sx, 20)]
    assert sig.max_size_rao == row.v_rao and sig.alpha_h_ppm == round(row.eh * 1e6)
    assert sig.horizon_blocks == 14_400 and sig.declares_dilution is False and sig.urgency is Urgency.NORMAL
    assert sig.reasons == ("momentum.entry",)


def test_max_new_entries_and_turnover_cap(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    one = MomentumStrategy({"b_s_per_z_day": 0.004, "max_new_entries": 1}).evaluate(m.ctx(sid="momentum"))
    assert [int(s.key.netuid) for s in one.signals if s.kind is SignalKind.TARGET] == [20]
    # 239 TAO of FILLED buys attributed to the sleeve in the last day: the 30 % x 800 TAO cap leaves < 1 position
    key = _key(sx, 11)
    intent = OrderIntent(order_id=make_order_id("r", BookId("b"), Block(sx.B - 100), key, sx.HK1, OrderKind.ADD_STAKE_LIMIT, 0),
                         attempt=0, book=BookId("b"), created_block=Block(sx.B - 100), kind=OrderKind.ADD_STAKE_LIMIT,
                         key=key, hotkey=sx.HK1, tao_in=Rao(239 * sx.TAO), alpha_in=AlphaRao(0), full_position=False,
                         limit_price=PriceRao(10**8), allow_partial=False, shielded=True, valid_until=Block(sx.B),
                         expected_out=1, urgency=Urgency.NORMAL, attribution=((StrategyId("momentum"), Ppm(PPM)),),
                         reason="test")
    bv = sx.book_view(orders=(OrderRecord(intent=intent, state=OrderState.FILLED),))
    capped = MomentumStrategy({"b_s_per_z_day": 0.004}).evaluate(m.ctx(sid="momentum", book_view=bv))
    assert capped.signals == ()
    assert MomentumStrategy()._turnover_24h(m.ctx(sid="momentum", book_view=bv)) == 239 * sx.TAO


def test_rank_gauss_is_the_z_transform() -> None:
    z = rank_gauss([3.0, 1.0, 2.0, 2.0])
    nd = NormalDist()
    assert z[1] == pytest.approx(nd.inv_cdf(0.5 / 4))
    assert z[2] == z[3] == pytest.approx(nd.inv_cdf(2.0 / 4))
    assert z[0] == pytest.approx(nd.inv_cdf(3.5 / 4))
    assert rank_gauss([5.0]) == [0.0] and rank_gauss([]) == []


# ------------------------------------------------------------------------------------------------- universe
@pytest.mark.parametrize(("over", "code"), [
    ({"tao_tao": 500, "feat": {"pool_tao": 500.0}}, "U.pool"),
    ({"tao_tao": 11_000, "feat": {"pool_tao": 11_000.0}}, "U.pool"),
    ({"feat": {"age_reg_blocks": 20 * 7_200}}, "U.age"),
    ({"feat": {"age_reg_blocks": 60 * 7_200, "ema_rank_desc": 41}}, "U.age"),
    ({"state": {"miner_burned": Decimal("0.6"), "alpha_out_emission": AlphaRao(10**9)}}, "U.burn"),
    ({"wq": 6 * 10**17}, "U.quote"),
    ({"feat": {"router_candidates": (SimpleNamespace(take=2_000),)}}, "U.take"),
    ({"feat": {"router_candidates": (SimpleNamespace(ck=10),)}}, "U.childkey_take"),
    ({"feat": {"ret_7d": None}}, "U.history"),
    ({"feat": {"flow_7d": None}}, "U.history"),
    ({"feat": {"prune_rank": 6}}, "U.prune"),
])
def test_universe_reason_codes(sx: SimpleNamespace, over: dict[str, Any], code: str) -> None:
    over = dict(over)
    if "feat" in over:
        f = dict(over["feat"])
        rc = f.get("router_candidates")
        if rc and isinstance(rc[0], SimpleNamespace):
            ns = rc[0]
            f["router_candidates"] = (sx.candidate(score_ppm_day=2_000, take_u16=getattr(ns, "take", 0),
                                                   childkey_take_u16=getattr(ns, "ck", 0)),)
        over["feat"] = f
    m = sx.market(_specs(sx, **over))
    row = MomentumStrategy().evaluate(m.ctx(sid="momentum")).rows[_key(sx, 20)]
    assert code in row.universe_failed
    assert row.s_score is None and row.rank is None


# ------------------------------------------------------------------------------------------------- holdings and exits
def _held(sx: SimpleNamespace, m: Any, netuid: int, *, opened: int, mem: MomentumMemory | None = None,
          blk: int | None = None, params: dict[str, Any] | None = None, **kw: Any) -> Any:
    key = _key(sx, netuid)
    ctx = m.ctx(blk if blk is not None else sx.B, sid="momentum",
                portfolio=sx.holding("momentum", key, alpha_tao=500, cost_tao=5.0 * 1.03, opened=opened), **kw)
    return MomentumStrategy({"b_s_per_z_day": 0.004, **(params or {})}).evaluate(ctx, mem)


def test_trailing_stop_from_peak_executable_value(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    key = _key(sx, 20)
    ev0 = _held(sx, m, 20, opened=sx.B - 7_200)
    st = next(s for s in ev0.memory.keys if s.key == key)
    assert [s.kind for s in ev0.signals if s.key == key] == [SignalKind.TARGET]
    # a peak 20 % above today's value per share: stop = clip(2.5 x 5 %, 10 %, 25 %) = 12.5 % -> HIGH exit
    mem = MomentumMemory(keys=(MomKeyState(key=key, entry_block=sx.B - 7_200, min_hold_blocks=7_200,
                                           peak_e12=st.peak_e12 * 6 // 5),))
    ev = _held(sx, m, 20, opened=sx.B - 7_200, mem=mem)
    sig = next(s for s in ev.signals if s.key == key)
    assert sig.kind is SignalKind.EXIT and sig.urgency is Urgency.HIGH and "stop.trailing" in sig.reasons
    # 10 % below the peak: inside the 12.5 % stop
    mem2 = MomentumMemory(keys=(replace_peak(mem.keys[0], st.peak_e12 * 10 // 9),))
    ev2 = _held(sx, m, 20, opened=sx.B - 7_200, mem=mem2)
    assert next(s for s in ev2.signals if s.key == key).kind is SignalKind.TARGET


def replace_peak(st: MomKeyState, peak: int) -> MomKeyState:
    return replace(st, peak_e12=peak)


@pytest.mark.parametrize(("over", "code"), [
    ({"feat": {"flow_1h": -0.03}}, "exit.flow_reversal"),
    ({"flow_frac_day": -0.4}, "exit.flow_reversal"),
])
def test_flow_reversal_exits(sx: SimpleNamespace, over: dict[str, Any], code: str) -> None:
    m = sx.market(_specs(sx, **over))
    ev = _held(sx, m, 20, opened=sx.B - 7_200)
    sig = next(s for s in ev.signals if s.key == _key(sx, 20))
    assert sig.kind is SignalKind.EXIT and sig.urgency is Urgency.HIGH and code in sig.reasons


def test_decay_exit_on_two_cycles_after_min_hold(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx, top_tao=4_000, feat={"ret_1d": -0.01, "ret_7d": -0.1, "flow_1d": -0.01}))
    key = _key(sx, 20)
    opened = sx.B - 32_400                                    # 4.5 d: past the 4-d max min-hold, inside the 5-d time exit
    ev1 = _held(sx, m, 20, opened=opened)
    assert ev1.rows[key].s_score is not None and ev1.rows[key].s_score < -0.25
    assert next(s for s in ev1.signals if s.key == key).kind is SignalKind.TARGET
    ev2 = _held(sx, m, 20, opened=opened, mem=ev1.memory, blk=sx.B + 300)
    sig = next(s for s in ev2.signals if s.key == key)
    assert sig.kind is SignalKind.EXIT and sig.urgency is Urgency.NORMAL and "exit.decay" in sig.reasons


def test_universe_failure_exit_on_two_cycles(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx, top_tao=500))
    key = _key(sx, 20)
    ev1 = _held(sx, m, 20, opened=sx.B - 7_200)
    assert next(s for s in ev1.signals if s.key == key).kind is SignalKind.TARGET
    ev2 = _held(sx, m, 20, opened=sx.B - 7_200, mem=ev1.memory, blk=sx.B + 300)
    assert "exit.universe" in next(s for s in ev2.signals if s.key == key).reasons


def test_time_exit_reunderwrites_at_most_twice(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    key = _key(sx, 20)
    opened = sx.B - 3 * 7_200 - 100                           # micro name: 3-d time exit
    ev1 = _held(sx, m, 20, opened=opened)
    sig = next(s for s in ev1.signals if s.key == key)
    assert sig.kind is SignalKind.TARGET and "exit.time.reunderwritten" in sig.reasons
    st = next(s for s in ev1.memory.keys if s.key == key)
    assert st.underwrites == 1 and st.entry_block == sx.B
    mem = MomentumMemory(keys=(MomKeyState(key=key, entry_block=opened, min_hold_blocks=7_200, underwrites=2,
                                           peak_e12=st.peak_e12),))
    ev2 = _held(sx, m, 20, opened=opened, mem=mem)
    assert "exit.time" in next(s for s in ev2.signals if s.key == key).reasons


def test_wake_without_held_event_returns_last_signals(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    strat: Strategy = MomentumStrategy({"b_s_per_z_day": 0.004})
    out1 = strat.on_tick(m.ctx(sid="momentum"), strat.initial_memory())
    ev = ChainEvent(ChainEventKind.LARGE_FLOW, Block(sx.B + 60), key=_key(sx, 12), amount=-10, frac_ppm=Ppm(30_000))
    store = m.store(sx.B, 600)
    out2 = strat.on_tick(m.ctx(sx.B + 60, sid="momentum", events=(ev,), store=store), out1.memory)
    assert out2.signals == out1.signals and out2.memory == out1.memory and store.calls == 0


# ------------------------------------------------------------------------------------------------- deferred flags
def test_deferred_flags_default_off() -> None:
    p = MomentumParams()
    assert not (p.p1_impulse or p.ema_gap_struct or p.rotation or p.breadth_gate or p.weekly_reversal_guard
                or p.blowoff_trim)


def test_p1_impulse_admits_an_inflow_name_between_scheduled_cycles(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    key = _key(sx, 19)
    ev = ChainEvent(ChainEventKind.LARGE_FLOW, Block(sx.B + 60), key=key, amount=50 * sx.TAO, frac_ppm=Ppm(25_000))
    mem = MomentumMemory(last_sched=sx.B)
    off = MomentumStrategy({"b_s_per_z_day": 0.004}).on_tick(m.ctx(sx.B + 60, sid="momentum", events=(ev,)), mem)
    assert off.signals == ()
    on = MomentumStrategy({"b_s_per_z_day": 0.004, "p1_impulse": True}).on_tick(
        m.ctx(sx.B + 60, sid="momentum", events=(ev,)), mem)
    (sig,) = on.signals
    assert sig.key == key and sig.urgency is Urgency.HIGH and "p1_impulse" in sig.reasons


def test_breadth_gate_and_weekly_reversal_guard(sx: SimpleNamespace) -> None:
    specs = _specs(sx)
    for sp in specs[:7]:
        sp.feat["ret_1d"] = -0.001
    m = sx.market(specs)
    base = MomentumStrategy({"b_s_per_z_day": 0.004}).evaluate(m.ctx(sid="momentum"))
    assert base.breadth == pytest.approx(0.3) and [s for s in base.signals if s.kind is SignalKind.TARGET]
    gated = MomentumStrategy({"b_s_per_z_day": 0.004, "breadth_gate": True}).evaluate(m.ctx(sid="momentum"))
    assert gated.signals == ()
    m2 = sx.market(_specs(sx, feat={"ret_7d": 0.5}))
    guard = MomentumStrategy({"b_s_per_z_day": 0.004, "weekly_reversal_guard": True}).evaluate(m2.ctx(sid="momentum"))
    assert _key(sx, 20) not in {s.key for s in guard.signals}


def test_blowoff_trim_and_rotation_annotation(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx, feat={"fast_ema_gap": 0.1}))
    key = _key(sx, 20)
    ev = _held(sx, m, 20, opened=sx.B - 7_200, params={"blowoff_trim": True})
    sig = next(s for s in ev.signals if s.key == key)
    assert "blowoff_trim" in sig.reasons and sig.max_size_rao is not None
    held = int(Decimal(500 * sx.TAO) * Decimal("0.01"))
    assert sig.max_size_rao <= held * 0.55
    # rotation: an exit and an entry in the same cycle are paired on the entry's reasons
    m2 = sx.market(_specs(sx) + [sx.Spec(40, reg_at=8_000_040, feat={"flow_1h": -0.05})])
    k40 = sx.Spec(40, reg_at=8_000_040).key
    ctx = m2.ctx(sid="momentum", portfolio=sx.holding("momentum", k40, alpha_tao=500, cost_tao=5.2, opened=sx.B - 7_200))
    ev2 = MomentumStrategy({"b_s_per_z_day": 0.004, "rotation": True}).evaluate(ctx)
    entries = [s for s in ev2.signals if s.kind is SignalKind.TARGET]
    assert entries and all(f"rotation_from:40:{8_000_040}" in s.reasons for s in entries)


def test_ema_gap_struct_terms_change_the_score(sx: SimpleNamespace) -> None:
    specs = _specs(sx)
    for i, sp in enumerate(specs):
        sp.feat["ema_gap"] = -0.01 * i
    m = sx.market(specs)
    a = MomentumStrategy().evaluate(m.ctx(sid="momentum")).rows[_key(sx, 20)].s_score
    b = MomentumStrategy({"ema_gap_struct": True}).evaluate(m.ctx(sid="momentum")).rows[_key(sx, 20)].s_score
    assert a is not None and b is not None and a != b


# ------------------------------------------------------------------------------------------------- memory, params
def test_memory_round_trips_through_the_codec(sx: SimpleNamespace) -> None:
    m = sx.market(_specs(sx))
    ev = _held(sx, m, 20, opened=sx.B - 7_200)
    mem = ev.memory
    assert mem.keys and mem.last_signals and mem.last_sched == sx.B
    raw = codec.canonical_bytes(mem)
    assert codec.decode_bytes(type(MomentumStrategy().initial_memory()), raw) == mem


def test_attributes_and_params() -> None:
    s: Strategy = MomentumStrategy()
    assert s.id == "momentum" and s.decide_every_blocks == 300 and s.min_cadence_blocks == 60
    assert s.valid_from_block == 8_466_531 and s.declares_dilution is False
    assert s.wake_on == frozenset({ChainEventKind.LARGE_FLOW, ChainEventKind.EMISSION_TOGGLED,
                                   ChainEventKind.DEREGISTERED})
    assert alpha_days(1.0, 0.25) == 1.0 and alpha_days(3.0, 0.25) == pytest.approx(1.5)
    with pytest.raises(ValueError, match="s_out"):
        MomentumStrategy({"s_out": 2.0})
    assert math.isfinite(Z95)


def test_rotation_cost_applies_only_to_entries_paired_with_an_exit(sx: SimpleNamespace) -> None:
    # the gate case (TEMPORARY net edge ~0.18 % < 0.25 %): one swap fee and (buy + sell - rotate) tx / V saved by the
    # rotation lift it over the margin, but only when an exit funds it in the same cycle
    k40 = sx.Spec(40, reg_at=8_000_040).key
    m = sx.market(_specs(sx) + [sx.Spec(40, reg_at=8_000_040, feat={"flow_1h": -0.05})])
    held = sx.holding("momentum", k40, alpha_tao=500, cost_tao=5.2, opened=sx.B - 7_200)
    top = _key(sx, 20)
    params = {"b_s_per_z_day": 0.0011, "rotation": True}
    paired = MomentumStrategy(params).evaluate(m.ctx(sid="momentum", portfolio=held))
    sig = next(s for s in paired.signals if s.key == top)
    assert sig.kind is SignalKind.TARGET and f"rotation_from:40:{8_000_040}" in sig.reasons
    unpaired = MomentumStrategy(params).evaluate(m.ctx(sid="momentum"))
    assert top not in {s.key for s in unpaired.signals}
    plain = MomentumStrategy({"b_s_per_z_day": 0.0011}).evaluate(m.ctx(sid="momentum", portfolio=held))
    assert top not in {s.key for s in plain.signals if s.kind is SignalKind.TARGET}
