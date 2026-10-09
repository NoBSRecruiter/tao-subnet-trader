"""portfolio.planner: limits with marginal-price semantics (tao_in first, then the limit), beta, dust and remainder
rules, drain timing, tranching, chase, urgent/emergency exits, FROZEN fill-or-kill, shield fallback, moves, one order
per netuid, delegates, priority, and the section 10.2 planner properties (DESIGN.md 3.9, 3.12)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from taotrader.core.config import RiskCfg
from taotrader.core.fixed import DEC, floor_int
from taotrader.core.orders import OrderKind, OrderState, Urgency, make_order_id
from taotrader.core.signals import ForcedExit, RiskDecision, TargetBook
from taotrader.core.units import PERQUINTILL, PPM, AlphaRao, Block, Mode, Ppm, PriceRao, Rao
from taotrader.portfolio.planner import StandardPlanner, next_drain_block, urgent_limit
from taotrader.protocol.amm import (
    liq_value,
    marginal_after_buy,
    marginal_after_sell,
    max_buy_to_limit,
    max_sell_to_limit,
    quote_buy,
    quote_sell,
    spot_rao_exact,
)
from taotrader.protocol.fees import meets_min_stake, nominator_dust
from taotrader.risk.liquidity import holdings

B = 9_240_388
TAO = 10**9


def plan(kit: ModuleType, items=(), forced=(), *, pf=None, snap=None, bv=None, mode: Mode = Mode.NORMAL, cfg=None,
         halt: bool = False, inflight=frozenset(), lag: int | None = None, fr=None):
    snap = snap if snap is not None else kit.snapshot()
    t = kit.tick(snap, portfolio=pf, bv=bv, mode=mode, fr=fr)
    tb = TargetBook(asof=Block(B), items=tuple(sorted(items, key=lambda x: x.key)),
                    forced=tuple(sorted(forced, key=lambda f: f.key)), halt_entries=halt)
    planner = StandardPlanner(kit.book_cfg(risk=cfg))
    out = planner(RiskDecision(tb, (), mode), t, frozenset(inflight), kit.RUN, kit.BOOK, finality_lag_blocks=lag)
    return out, t


def held(kit: ModuleType, netuid: int, value_tao: int, **pf_kw):
    """A portfolio holding ~value_tao TAO (at spot) of netuid on its validator hotkey."""
    price = Decimal("0.002") * netuid
    alpha = int(Decimal(value_tao * TAO) / price)
    return kit.portfolio(positions=[kit.position(kit.key(netuid), kit.vhk(netuid), alpha)], **pf_kw)


# ------------------------------------------------------------------------------------------------- buys
def test_buy_limit_from_the_marginal_after_tao_in(kit: ModuleType) -> None:
    out, t = plan(kit, [kit.target(kit.key(8), kit.vhk(8), 10 * TAO)])
    assert len(out) == 1
    i = out[0]
    pool = t.view.get(kit.key(8)).pool                              # type: ignore[union-attr]
    assert i.kind is OrderKind.ADD_STAKE_LIMIT and i.tao_in == 10 * TAO and not i.allow_partial and i.shielded
    m = marginal_after_buy(pool, Rao(10 * TAO))
    assert i.limit_price == -((-m * (PPM + 5_000)) // PPM)          # beta_entry = q95 5,000 ppm
    assert Decimal(i.limit_price) > spot_rao_exact(pool)
    assert i.tao_in <= max_buy_to_limit(pool, i.limit_price)
    assert i.expected_out == quote_buy(pool, Rao(10 * TAO)).amount_out
    assert i.valid_until == B + 3 + 2 and i.attempt == 0
    assert i.order_id == make_order_id(kit.RUN, kit.BOOK, Block(B), kit.key(8), kit.vhk(8), OrderKind.ADD_STAKE_LIMIT, 0)


@pytest.mark.parametrize(("q95", "beta"), [(500, 1_000), (5_000, 5_000), (50_000, 20_000), (None, 20_000)])
def test_beta_entry_is_clamped(kit: ModuleType, q95: int | None, beta: int) -> None:
    snap = kit.snapshot()
    fr = kit.frame(snap, feat_over={8: {"beta_entry_ppm": Ppm(q95)}} if q95 is not None else None)
    if q95 is None:
        fr = replace(fr, feats={k: v for k, v in fr.feats.items() if k != kit.key(8)})
    out, t = plan(kit, [kit.target(kit.key(8), kit.vhk(8), 10 * TAO)], fr=fr)
    pool = t.view.get(kit.key(8)).pool                              # type: ignore[union-attr]
    assert out[0].limit_price == -((-marginal_after_buy(pool, Rao(10 * TAO)) * (PPM + beta)) // PPM)


def test_buy_cash_band_and_benefit(kit: ModuleType) -> None:
    k = kit.key(8)
    out, _ = plan(kit, [kit.target(k, kit.vhk(8), 10 * TAO)], pf=kit.portfolio(cash=5 * TAO))
    assert out[0].tao_in == 5 * TAO - RiskCfg().min_free_real_rao      # MIN_FREE_REAL stays untouched
    open_buy = kit.order(kit.key(3), kit.vhk(3), OrderKind.ADD_STAKE_LIMIT, OrderState.SUBMITTED, tao_in=2 * TAO)
    out2, _ = plan(kit, [kit.target(k, kit.vhk(8), 10 * TAO)], pf=kit.portfolio(cash=5 * TAO),
                   bv=kit.book_view(orders=(open_buy,)))
    assert out2[0].tao_in == 3 * TAO - RiskCfg().min_free_real_rao     # open buys reserve their TAO
    assert plan(kit, [kit.target(k, kit.vhk(8), 10 * TAO)], pf=kit.portfolio(cash=TAO // 2))[0] == ()   # < V_MIN
    pf = held(kit, 8, 9)
    t = kit.tick(kit.snapshot(), portfolio=pf)
    cur = int(holdings(t)[k].value)
    assert plan(kit, [kit.target(k, kit.vhk(8), cur + TAO)], pf=pf)[0] == ()   # inside max(V_MIN, 20% target)
    # benefit: alpha_h * D >= D * (D/T + f) + buy fee (T = 1,000 TAO, D = 10 TAO: needs alpha_h >~ 1.06%)
    low = kit.target(k, kit.vhk(8), 10 * TAO, reasons=("alloc", "alpha_h_ppm=10000"))
    high = kit.target(k, kit.vhk(8), 10 * TAO, reasons=("alloc", "alpha_h_ppm=11000"))
    assert plan(kit, [low])[0] == () and len(plan(kit, [high])[0]) == 1


def test_no_buys_unless_normal_and_not_halted(kit: ModuleType) -> None:
    item = kit.target(kit.key(8), kit.vhk(8), 10 * TAO)
    assert plan(kit, [item], halt=True)[0] == ()
    assert plan(kit, [item], mode=Mode.CAUTION)[0] == ()
    assert plan(kit, [item], bv=kit.book_view(entries_halted_until=Block(B)))[0] == ()
    subtoken_off = kit.snapshot(subnets=[kit.subnet(8, subtoken_enabled=False)])
    assert plan(kit, [item], snap=subtoken_off)[0] == ()


def test_chase_rules(kit: ModuleType) -> None:
    k = kit.key(8)
    item = kit.target(k, kit.vhk(8), 10 * TAO)
    spot = 16_000_000                                               # 0.016 TAO / alpha
    assert len(plan(kit, [item], bv=kit.book_view(chase=((k, 1, PriceRao(spot)),)))[0]) == 1
    assert plan(kit, [item], bv=kit.book_view(chase=((k, 2, PriceRao(spot)),)))[0] == ()           # K_CHASE re-quotes
    assert plan(kit, [item], bv=kit.book_view(chase=((k, 0, PriceRao(spot * 1_000 // 1_016)),)))[0] == ()   # drift 1.6%
    assert len(plan(kit, [item], bv=kit.book_view(chase=((k, 0, PriceRao(spot * 1_000 // 1_014)),)))[0]) == 1


# ------------------------------------------------------------------------------------------------- normal sells
def test_partial_sell_limit_and_amount(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    t = kit.tick(kit.snapshot(), portfolio=pf)
    h = holdings(t)[k]
    out, _ = plan(kit, [kit.target(k, kit.vhk(8), int(h.value) // 2)], pf=pf)
    i = out[0]
    pool = t.view.get(k).pool                                       # type: ignore[union-attr]
    assert i.kind is OrderKind.REMOVE_STAKE_LIMIT and not i.full_position and not i.allow_partial
    assert i.alpha_in == int(h.alpha) * (int(h.value) - int(h.value) // 2) // int(h.value)
    assert i.limit_price == marginal_after_sell(pool, AlphaRao(i.alpha_in)) * (PPM - 10_000) // PPM   # beta_exit q99
    assert Decimal(i.limit_price) < spot_rao_exact(pool) and i.alpha_in <= max_sell_to_limit(pool, i.limit_price)
    assert i.expected_out == quote_sell(pool, i.alpha_in, partial_remaining=True).amount_out


def test_full_exits_remainder_and_dust(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    h = holdings(kit.tick(kit.snapshot(), portfolio=pf))[k]
    for tgt in (0, TAO // 4):                                       # 0, or a remainder below REMAINDER_MIN (0.5 TAO)
        out, _ = plan(kit, [kit.target(k, kit.vhk(8), tgt)], pf=pf)
        assert out[0].full_position and out[0].alpha_in == h.alpha
    # remainder worth less than NominatorMinRequiredStake (0.02 TAO) becomes a full exit
    cfg = RiskCfg(v_min_rao=Rao(TAO // 1_000), remainder_min_rao=Rao(TAO // 1_000), band_ppm=Ppm(0))
    small = held(kit, 8, 1)
    out2, t2 = plan(kit, [kit.target(k, kit.vhk(8), TAO // 100)], pf=small, cfg=cfg)
    assert out2[0].full_position
    # a partial sell must output >= 0.002 TAO
    hs = holdings(t2)[k]
    assert plan(kit, [kit.target(k, kit.vhk(8), int(hs.value) - TAO // 1_000)], pf=small, cfg=cfg)[0] == ()
    # no target for a held key (and no forced exit): hold
    assert plan(kit, [], pf=pf)[0] == ()


def test_drain_deferral(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    for nd_offset, deferred in ((5 + 10, True), (5 + 30, True), (5 + 31, False), (5, False)):
        last = B + nd_offset - 360
        snap = kit.snapshot(subnets=[kit.subnet(8, last_epoch_block=Block(last))])
        assert next_drain_block(snap.get(k), B) == B + nd_offset        # type: ignore[arg-type]
        out, _ = plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, snap=snap)
        assert (out == ()) is deferred, nd_offset
    # HIGH sells (thesis stops) are not deferred
    snap = kit.snapshot(subnets=[kit.subnet(8, last_epoch_block=Block(B + 15 - 360))])
    out, _ = plan(kit, [kit.target(k, kit.vhk(8), 0, urgency=Urgency.HIGH)], pf=pf, snap=snap)
    assert len(out) == 1 and out[0].urgency is Urgency.HIGH


def test_tranching_only_with_chain_buy_refill(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 60)
    plain, _ = plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf)
    assert plain[0].full_position                                   # no refill: one shot
    refill = kit.snapshot(subnets=[kit.subnet(8, excess_tao=Rao(10_000_000))])
    out, t = plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, snap=refill)
    h = holdings(t)[k]
    v_tr = 1_000 * TAO * 10_000 // 990_000                          # T * 1% / (1 - 1%)
    assert not out[0].full_position and out[0].alpha_in == int(h.alpha) * v_tr // int(h.value)
    recent = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FILLED, created=B - 100)
    assert plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, snap=refill, bv=kit.book_view(orders=(recent,)))[0] == ()
    old = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FILLED, created=B - 601)
    assert len(plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, snap=refill, bv=kit.book_view(orders=(old,)))[0]) == 1


def test_slippage_fallback_after_a_failed_sell(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    failed = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FAILED, created=B - 50)
    out, _ = plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, bv=kit.book_view(orders=(failed,)))
    assert out[0].allow_partial and out[0].attempt == 1


# ------------------------------------------------------------------------------------------------- forced exits
def test_emergency_and_urgent_exit_limits(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    out, t = plan(kit, (), [ForcedExit(k, Urgency.EMERGENCY, "prune_A", Ppm(200_000))], pf=pf)
    i = out[0]
    pool = t.view.get(k).pool                                       # type: ignore[union-attr]
    expect = floor_int(DEC.multiply(spot_rao_exact(pool), DEC.power(Decimal("0.8"), Decimal(2))))
    assert i.limit_price == expect == urgent_limit(pool, 200_000)
    assert i.full_position and i.allow_partial and i.urgency is Urgency.EMERGENCY and i.reason == "forced:prune_A"
    assert i.alpha_in == holdings(t)[k].alpha and i.expected_out == liq_value(pool, holdings(t)[k].alpha)
    skew = kit.snapshot(subnets=[kit.subnet(8, w_quote_e18=6 * PERQUINTILL // 10)])
    p2 = skew.get(k).pool                                           # type: ignore[union-attr]
    w_exp = DEC.divide(Decimal(PERQUINTILL), Decimal(p2.w_quote_e18))
    assert urgent_limit(p2, 30_000) == floor_int(DEC.multiply(spot_rao_exact(p2), DEC.power(Decimal("0.97"), w_exp)))


def test_urgent_requote_widens_and_respects_l_exec(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    fe = ForcedExit(k, Urgency.URGENT, "prune_backstop", Ppm(30_000))
    f1 = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.EXPIRED, created=B - 20, full=True)
    f2 = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FAILED, created=B - 10, full=True)
    out, t = plan(kit, (), [fe], pf=pf, bv=kit.book_view(orders=(f1, f2)))
    pool = t.view.get(k).pool                                       # type: ignore[union-attr]
    assert out[0].attempt == 2 and out[0].limit_price == urgent_limit(pool, 67_500)      # 3% x 1.5 x 1.5
    many = tuple(kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.EXPIRED, created=B - 100 + j,
                           full=True, oid=f"x{j}") for j in range(8))
    out2, _ = plan(kit, (), [fe], pf=pf, bv=kit.book_view(orders=many))
    assert out2[0].limit_price == urgent_limit(pool, 250_000)        # capped at s_emerg(R) (R ~ 0.35 -> 25%)
    emer = ForcedExit(k, Urgency.EMERGENCY, "prune_A", Ppm(120_000))
    out3, _ = plan(kit, (), [emer], pf=pf, bv=kit.book_view(orders=many))
    assert out3[0].limit_price == urgent_limit(pool, 120_000)        # EMERGENCY already at s_emerg: no widening
    recent = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FAILED, created=B - 4, full=True)
    assert plan(kit, (), [fe], pf=pf, bv=kit.book_view(orders=(recent,)))[0] == ()   # at most once per L_exec


def test_frozen_allows_only_emergency_fill_or_kill(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    emer = ForcedExit(k, Urgency.EMERGENCY, "prune_target", Ppm(250_000))
    out, t = plan(kit, (), [emer], pf=pf, mode=Mode.FROZEN)
    i = out[0]
    pool = t.view.get(k).pool                                       # type: ignore[union-attr]
    assert not i.allow_partial and i.limit_price == urgent_limit(pool, 50_000)
    assert i.alpha_in <= max_sell_to_limit(pool, i.limit_price)
    assert plan(kit, (), [ForcedExit(k, Urgency.URGENT, "emission_off", Ppm(30_000))], pf=pf, mode=Mode.FROZEN)[0] == ()
    off = RiskCfg(allow_emergency_exits_when_frozen=False)
    assert plan(kit, (), [emer], pf=pf, mode=Mode.FROZEN, cfg=off)[0] == ()


def test_shield_fallback_and_validity(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    fe = [ForcedExit(k, Urgency.URGENT, "prune_backstop", Ppm(30_000))]
    assert plan(kit, (), fe, pf=pf, lag=5)[0][0].shielded
    out, _ = plan(kit, (), fe, pf=pf, lag=6)
    assert not out[0].shielded and out[0].valid_until == B + 3 + 16
    normal, _ = plan(kit, [kit.target(k, kit.vhk(8), 0, urgency=Urgency.HIGH)], pf=pf, lag=9)
    assert normal[0].shielded                                       # only risk exits go unshielded


def test_normal_forced_exit_is_a_normal_sell(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    out, t = plan(kit, (), [ForcedExit(k, Urgency.NORMAL, "burn", Ppm(15_000))], pf=pf, mode=Mode.EXITS_ONLY)
    i = out[0]
    assert i.full_position and not i.allow_partial and i.reason == "forced:burn"
    pool = t.view.get(k).pool                                       # type: ignore[union-attr]
    assert i.limit_price == marginal_after_sell(pool, i.alpha_in) * (PPM - 10_000) // PPM
    assert plan(kit, (), [ForcedExit(k, Urgency.NORMAL, "burn", Ppm(15_000))], pf=pf, mode=Mode.FROZEN)[0] == ()


# ------------------------------------------------------------------------------------------------- moves, concurrency, priority
def test_hotkey_switch_moves_the_whole_position_first(kit: ModuleType) -> None:
    k = kit.key(8)
    pf = held(kit, 8, 100)
    snap = kit.snapshot(subnets=[kit.subnet(8, hotkeys=[kit.hidx(kit.vhk(8), 10**15), kit.hidx(kit.vhk(8, 2), 10**15)])])
    out, _t = plan(kit, [kit.target(k, kit.vhk(8, 2), 500 * TAO)], pf=pf, snap=snap)
    assert len(out) == 1
    i = out[0]
    assert i.kind is OrderKind.MOVE_STAKE and i.full_position and i.dest_hotkey == kit.vhk(8, 2)
    assert i.hotkey == kit.vhk(8) and i.limit_price == 0 and i.urgency is Urgency.NORMAL
    assert plan(kit, [kit.target(k, kit.vhk(8, 2), 500 * TAO)], pf=pf, snap=snap, mode=Mode.EXITS_ONLY)[0] == ()


def test_one_order_per_netuid(kit: ModuleType) -> None:
    k = kit.key(8)
    item = kit.target(k, kit.vhk(8), 10 * TAO)
    assert plan(kit, [item], inflight={k})[0] == ()
    other_gen = kit.order(kit.key(8, 7_777_777), kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.SUBMITTED)
    assert plan(kit, [item], bv=kit.book_view(orders=(other_gen,)))[0] == ()
    done = kit.order(k, kit.vhk(8), OrderKind.ADD_STAKE_LIMIT, OrderState.FILLED, created=B - 50)
    assert len(plan(kit, [item], bv=kit.book_view(orders=(done,)))[0]) == 1
    assert plan(kit, [item], bv=kit.book_view(dissolving=(k,)))[0] == ()
    gone = kit.snapshot(subnets=[kit.subnet(9)])
    assert plan(kit, [item], snap=gone)[0] == ()


def test_priority_and_delegates(kit: ModuleType) -> None:
    pos = [kit.position(kit.key(n), kit.vhk(n), int(Decimal(50 * TAO) / (Decimal("0.002") * n))) for n in (2, 3, 4, 5)]
    pf = kit.portfolio(cash=500 * TAO, positions=pos)
    items = [kit.target(kit.key(4), kit.vhk(4), 0, urgency=Urgency.HIGH), kit.target(kit.key(5), kit.vhk(5), 0),
             kit.target(kit.key(9), kit.vhk(9), 10 * TAO)]
    forced = [ForcedExit(kit.key(2), Urgency.URGENT, "emission_off", Ppm(30_000)),
              ForcedExit(kit.key(3), Urgency.EMERGENCY, "prune_target", Ppm(250_000))]
    out, _ = plan(kit, items, forced, pf=pf, bv=kit.book_view(delegates_free=("a", "b", "c", "d", "e")))
    assert [(int(i.key.netuid), i.urgency) for i in out] == [(3, Urgency.EMERGENCY), (2, Urgency.URGENT), (4, Urgency.HIGH),
                                                             (5, Urgency.NORMAL), (9, Urgency.NORMAL)]
    assert out[-1].kind is OrderKind.ADD_STAKE_LIMIT
    two, _ = plan(kit, items, forced, pf=pf, bv=kit.book_view(delegates_free=("a", "b")))
    assert [int(i.key.netuid) for i in two] == [3, 2]
    none, _ = plan(kit, items, forced, pf=pf, bv=kit.book_view(delegates_free=()))
    assert none == ()


def test_within_a_tier_larger_expected_loss_first(kit: ModuleType) -> None:
    pos = [kit.position(kit.key(4), kit.vhk(4), 10_000 * TAO), kit.position(kit.key(6), kit.vhk(6), 30_000 * TAO)]
    pf = kit.portfolio(positions=pos)
    forced = [ForcedExit(kit.key(4), Urgency.URGENT, "emission_off", Ppm(30_000)),
              ForcedExit(kit.key(6), Urgency.URGENT, "emission_off", Ppm(30_000))]
    out, _ = plan(kit, (), forced, pf=pf)
    assert [int(i.key.netuid) for i in out] == [6, 4]


# ------------------------------------------------------------------------------------------------- properties
@settings(max_examples=80)
@given(st.lists(st.tuples(st.integers(1, 12), st.integers(0, 300), st.integers(0, 300),
                          st.sampled_from([None, Urgency.NORMAL, Urgency.URGENT, Urgency.EMERGENCY]),
                          st.sampled_from([Urgency.NORMAL, Urgency.HIGH])), min_size=1, max_size=8,
                unique_by=lambda x: x[0]),
       st.integers(3, 7), st.integers(1, 2_000), st.integers(0, 3), st.sampled_from([Mode.NORMAL, Mode.CAUTION,
                                                                                     Mode.EXITS_ONLY, Mode.FROZEN]))
def test_planner_properties(kit: ModuleType, rows, wq_tenths: int, cash_tao: int, excess: int, mode: Mode) -> None:
    w = wq_tenths * PERQUINTILL // 10
    subs = [kit.subnet(n, w_quote_e18=w, excess_tao=Rao(excess * 5_000_000),
                       last_epoch_block=Block(B - 100 - 7 * n)) for n in range(1, 13)]
    snap = kit.snapshot(subnets=subs)
    positions = [kit.position(kit.key(n), kit.vhk(n), int(Decimal(h * TAO) / (Decimal("0.002") * n)))
                 for n, h, _, _, _ in rows if h > 0]
    pf = kit.portfolio(cash=cash_tao * TAO, positions=positions)
    items = [kit.target(kit.key(n), kit.vhk(n), tg * TAO // 2, urgency=u) for n, _, tg, _, u in rows]
    forced = [ForcedExit(kit.key(n), fu, "test", Ppm(30_000 if fu is not Urgency.EMERGENCY else 200_000))
              for n, _, _, fu, _ in rows if fu is not None]
    out, t = plan(kit, items, forced, pf=pf, snap=snap, mode=mode)
    hold = holdings(t)
    cfg = RiskCfg()
    assert len({int(i.key.netuid) for i in out}) == len(out) <= 3            # one per netuid, <= free delegates
    tiers = []
    for i in out:
        pool = t.view.get(i.key).pool                                         # type: ignore[union-attr]
        spot = spot_rao_exact(pool)
        if i.kind is OrderKind.ADD_STAKE_LIMIT:
            assert mode is Mode.NORMAL
            assert i.tao_in >= cfg.v_min_rao and meets_min_stake(int(i.tao_in), pool.fee_rate)
            assert Decimal(i.limit_price) > spot and i.tao_in <= max_buy_to_limit(pool, i.limit_price)
            assert i.tao_in <= pf.cash - cfg.min_free_real_rao
            tiers.append(4 if i.urgency <= Urgency.NORMAL else 2)
        elif i.kind is OrderKind.REMOVE_STAKE_LIMIT:
            h = hold[i.key]
            assert 0 < Decimal(i.limit_price) < spot
            assert 0 < i.alpha_in <= h.alpha                                  # long-only
            if not i.allow_partial:
                assert i.alpha_in <= max_sell_to_limit(pool, i.limit_price)
            if not i.full_position:
                rest = int(h.alpha) - int(i.alpha_in)
                q = quote_sell(pool, i.alpha_in, partial_remaining=True)
                assert q.amount_out >= 2_000_000
                if mode is not Mode.FROZEN:                               # FROZEN fill-or-kill may leave a remainder
                    assert not nominator_dust(rest, pool.shifted(q.d_tao, q.d_alpha), snap.glob)
            else:
                assert i.alpha_in == h.alpha
            if mode is Mode.FROZEN:
                assert i.urgency is Urgency.EMERGENCY and not i.allow_partial
            tiers.append({Urgency.EMERGENCY: 0, Urgency.URGENT: 1, Urgency.HIGH: 2}.get(i.urgency, 3))
        else:
            assert i.kind is OrderKind.MOVE_STAKE and mode < Mode.EXITS_ONLY
            tiers.append(3)
    assert tiers == sorted(tiers)


POOLS = st.tuples(st.integers(50, 50_000), st.integers(1, 1_000), st.integers(3, 7), st.integers(0, 330))


def _pool_market(kit: ModuleType, spec) -> object:
    tao_tao, price_milli, wq, fee = spec
    s = kit.subnet(8, tao=tao_tao * TAO, price=Decimal(price_milli) / 1_000, w_quote_e18=wq * PERQUINTILL // 10,
                   fee_rate=fee)
    return kit.snapshot(subnets=[s])


@settings(max_examples=150)
@given(POOLS, st.integers(0, 2_000), st.integers(0, 3_000), st.integers(0, 100_000))
def test_buy_properties(kit: ModuleType, spec, target_tenths: int, cash_tenths: int, q95: int) -> None:
    snap = _pool_market(kit, spec)
    fr = kit.frame(snap, feat_over={8: {"beta_entry_ppm": Ppm(q95)}})
    pf = kit.portfolio(cash=cash_tenths * TAO // 10)
    out, t = plan(kit, [kit.target(kit.key(8), kit.vhk(8), target_tenths * TAO // 10)], pf=pf, snap=snap, fr=fr)
    cfg = RiskCfg()
    for i in out:
        pool = t.view.get(i.key).pool                                         # type: ignore[union-attr]
        assert i.kind is OrderKind.ADD_STAKE_LIMIT
        assert i.tao_in >= cfg.v_min_rao and meets_min_stake(int(i.tao_in), pool.fee_rate)
        assert i.tao_in <= pf.cash - cfg.min_free_real_rao
        assert Decimal(i.limit_price) > spot_rao_exact(pool)
        assert i.tao_in <= max_buy_to_limit(pool, i.limit_price)
        assert marginal_after_buy(pool, i.tao_in) <= i.limit_price
        assert i.expected_out == quote_buy(pool, i.tao_in).amount_out > 0


@settings(max_examples=150)
@given(POOLS, st.integers(1, 5_000), st.integers(0, 6_000), st.integers(0, 100_000), st.booleans())
def test_sell_properties(kit: ModuleType, spec, held_hundredths: int, target_hundredths: int, q99: int,
                         refill: bool) -> None:
    snap = _pool_market(kit, spec)
    s = snap.get(kit.key(8))
    assert s is not None
    if refill:
        snap = kit.snapshot(subnets=[replace(s, excess_tao=Rao(20_000_000))])
    fr = kit.frame(snap, feat_over={8: {"beta_exit_ppm": Ppm(q99)}})
    alpha = int(Decimal(held_hundredths * TAO // 100) / s.pool.spot())
    if alpha <= 0:
        return
    pf = kit.portfolio(positions=[kit.position(kit.key(8), kit.vhk(8), alpha)])
    out, t = plan(kit, [kit.target(kit.key(8), kit.vhk(8), target_hundredths * TAO // 100)], pf=pf, snap=snap, fr=fr)
    h = holdings(t)[kit.key(8)]
    cfg = RiskCfg()
    for i in out:
        pool = t.view.get(i.key).pool                                         # type: ignore[union-attr]
        if i.kind is OrderKind.ADD_STAKE_LIMIT:                               # target above the holding
            assert target_hundredths * TAO // 100 > h.value
            continue
        assert i.kind is OrderKind.REMOVE_STAKE_LIMIT and not i.allow_partial
        assert 0 < Decimal(i.limit_price) < spot_rao_exact(pool)
        assert 0 < i.alpha_in <= h.alpha and i.alpha_in <= max_sell_to_limit(pool, i.limit_price)
        if i.full_position:
            assert i.alpha_in == h.alpha
        else:
            rest = int(h.alpha) - int(i.alpha_in)
            q = quote_sell(pool, i.alpha_in, partial_remaining=True)
            assert q.amount_out >= 2_000_000
            assert not nominator_dust(rest, pool.shifted(q.d_tao, q.d_alpha), snap.glob)   # post-trade, as the chain
            assert liq_value(pool, AlphaRao(rest)) > 0
        target = target_hundredths * TAO // 100
        if target <= 0 or target < max(cfg.remainder_min_rao, cfg.v_min_rao):
            assert i.full_position or refill          # tranching only with chain-buy refill


def test_live_caps_bound_buys_only(kit: ModuleType) -> None:
    from taotrader.core.config import LiveCfg
    from taotrader.core.units import RunMode

    live = LiveCfg(max_order_tao=1.0, max_daily_turnover_tao=5.0, max_position_tao=5.0, allowed_netuids=(8, 9))
    snap = kit.snapshot()

    def run(items, forced=(), *, pf=None, bv=None, run_mode=RunMode.LIVE):
        t = kit.tick(snap, portfolio=pf, bv=bv)
        tb = TargetBook(asof=Block(B), items=tuple(items), forced=tuple(forced))
        return StandardPlanner(kit.book_cfg(), run_mode=run_mode, live=live)(RiskDecision(tb, (), Mode.NORMAL), t,
                                                                             frozenset(), kit.RUN, kit.BOOK)

    buy = kit.target(kit.key(8), kit.vhk(8), 10 * TAO)
    assert run([buy])[0].tao_in == TAO                                       # max_order_tao
    assert run([buy], run_mode=RunMode.BACKTEST)[0].tao_in == 10 * TAO       # backtest/paper books: no live caps
    assert run([buy], run_mode=RunMode.LIVE_DRY)[0].tao_in == TAO
    assert run([kit.target(kit.key(7), kit.vhk(7), 10 * TAO)]) == ()         # not in allowed_netuids
    spent = tuple(kit.order(kit.key(3), kit.vhk(3), OrderKind.ADD_STAKE_LIMIT, OrderState.FILLED, tao_in=TAO,
                            created=B - 100 * (j + 1), oid=f"s{j}") for j in range(4))
    assert run([buy], bv=kit.book_view(orders=spent))[0].tao_in == TAO       # 4 of 5 TAO used today: 1 left
    five = spent + (kit.order(kit.key(3), kit.vhk(3), OrderKind.ADD_STAKE_LIMIT, OrderState.FILLED, tao_in=TAO,
                              created=B - 500, oid="s9"),)
    assert run([buy], bv=kit.book_view(orders=five)) == ()                   # daily turnover used up
    near = held(kit, 8, 4)                                                   # position ~4 TAO: < 1 TAO of room
    out = run([kit.target(kit.key(8), kit.vhk(8), 10 * TAO)], pf=near)
    assert out == () or out[0].tao_in <= TAO
    # sells are never capped: a 5-TAO emergency exit with max_order 1 TAO and the turnover used up goes out in full
    pf = held(kit, 8, 5)
    exit_ = run([], [ForcedExit(kit.key(8), Urgency.EMERGENCY, "prune_A", Ppm(200_000))], pf=pf,
                bv=kit.book_view(orders=five))
    assert len(exit_) == 1 and exit_[0].full_position and exit_[0].alpha_in == holdings(kit.tick(snap, portfolio=pf))[kit.key(8)].alpha
    normal = run([kit.target(kit.key(8), kit.vhk(8), 0)], pf=pf, bv=kit.book_view(orders=five))
    assert len(normal) == 1 and normal[0].kind is OrderKind.REMOVE_STAKE_LIMIT
