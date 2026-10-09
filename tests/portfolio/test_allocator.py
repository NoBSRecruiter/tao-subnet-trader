"""portfolio.allocator: budgets by stage, kill states, sleeve targets, sum-then-cap, N_eff, netting, router hotkeys,
and the section 10.2 allocator properties (DESIGN.md 3.10)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from taotrader.core.config import RiskCfg, SleeveCfg
from taotrader.core.orders import Urgency
from taotrader.core.portfolio import SleeveHolding
from taotrader.core.protocols import RouterState
from taotrader.core.signals import Signal, SignalKind, StrategyOutput
from taotrader.core.units import BLOCKS_PER_DAY, PPM, Block, Ppm, PpmPerDay, Rao, RunMode, Stage, StrategyId
from taotrader.portfolio.allocator import StandardAllocator
from taotrader.protocol.amm import v_star
from taotrader.risk.liquidity import holdings
from taotrader.risk.modes import g_max_eff_ppm

B = 9_240_388
TAO = 10**9


def sig(kit: ModuleType, sid: str, netuid: int, kind: SignalKind = SignalKind.TARGET, *, weight: int = PPM, **kw) -> Signal:
    return Signal(strategy=StrategyId(sid), key=kit.key(netuid), asof=Block(B), kind=kind, weight_ppm=Ppm(weight), **kw)


def sleeve(sid: str, stage: Stage = Stage.PAPER, budget: int = PPM) -> SleeveCfg:
    return SleeveCfg(StrategyId(sid), stage, Ppm(budget))


def run(kit: ModuleType, rows, *, snap=None, pf=None, bv=None, run_mode=RunMode.BACKTEST, live=(), caps=None,
        cfg: RiskCfg | None = None, nav=None):
    snap = snap if snap is not None else kit.big_market()
    sleeves = tuple(sl for sl, _ in rows)
    book = kit.book_cfg(sleeves, risk=cfg)
    t = kit.tick(snap, portfolio=pf, bv=bv, nav=nav)
    c = caps if caps is not None else {s.key: Rao(10**18) for s in snap.subnets}
    out = StandardAllocator(book, run_mode=run_mode, live_sleeves=live)(
        [(sl, StrategyOutput(tuple(sigs), None)) for sl, sigs in rows], t, c)
    return out, t


def val(tb, kit: ModuleType, netuid: int) -> int:
    t = tb.get(kit.key(netuid))
    return 0 if t is None else int(t.value_rao)


@pytest.mark.parametrize(("stage", "run_mode", "live", "budget", "expected_frac"), [
    (Stage.RESEARCH, RunMode.BACKTEST, (), PPM, PPM),
    (Stage.RESEARCH, RunMode.PAPER, (), PPM, 0),
    (Stage.SHADOW, RunMode.PAPER, (), PPM, 0),
    (Stage.PAPER, RunMode.PAPER, (), PPM, 400_000),          # B_MAX 40% outside backtests
    (Stage.PAPER, RunMode.PAPER, (), 300_000, 300_000),
    (Stage.PAPER, RunMode.LIVE, ("carry",), PPM, 0),
    (Stage.LIVE_ELIGIBLE, RunMode.LIVE, ("carry",), PPM, 400_000),
    (Stage.LIVE_ELIGIBLE, RunMode.LIVE, (), PPM, 0),
    (Stage.LIVE_ELIGIBLE, RunMode.PAPER, (), PPM, 400_000),
])
def test_budgets_by_stage(kit: ModuleType, stage: Stage, run_mode: RunMode, live: tuple, budget: int,
                          expected_frac: int) -> None:
    tb, t = run(kit, [(sleeve("carry", stage, budget), [sig(kit, "carry", 20)])], run_mode=run_mode, live=live)
    gross = int(t.nav_liq) * 800_000 // PPM
    assert val(tb, kit, 20) == gross * expected_frac // PPM


def test_lcw_budget_cap_and_kill_states(kit: ModuleType) -> None:
    lcw_pf = kit.portfolio(sleeve_cash=[("lcw", 1_000 * TAO)])
    tb, t = run(kit, [(sleeve("lcw"), [sig(kit, "lcw", 20)])], pf=lcw_pf)
    assert val(tb, kit, 20) == int(t.nav_liq) * 800_000 // PPM * 50_000 // PPM
    broke = kit.portfolio(sleeve_cash=[("carry", 1_000 * TAO), ("lcw", 0)])
    tb0, _ = run(kit, [(sleeve("lcw"), [sig(kit, "lcw", 20)])], pf=broke)
    assert val(tb0, kit, 20) == 0                                   # a sleeve's increase is limited to its own cash
    for state, mult in (("REDUCED", 500_000), ("SUSPENDED", 0), ("ACTIVE", PPM)):
        bv = kit.book_view(sleeve_stats=(kit.sleeve_stats("carry", state),))
        tb2, t2 = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20)])], bv=bv)
        assert val(tb2, kit, 20) == int(t2.nav_liq) * 800_000 // PPM * mult // PPM


def test_dd_governor_lowers_the_budget(kit: ModuleType) -> None:
    daily = ((Block(B - 3 * BLOCKS_PER_DAY), Rao(2_000 * TAO)),)          # NAV now 1,000: DD 50% -> x0.25
    tb, t = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20)])], bv=kit.book_view(nav_liq_daily=daily))
    assert g_max_eff_ppm(RiskCfg(), kit.book_view(nav_liq_daily=daily), int(t.nav_liq), B) == 200_000
    assert val(tb, kit, 20) == int(t.nav_liq) * 200_000 // PPM


def test_sleeve_target_terms(kit: ModuleType) -> None:
    snap = kit.big_market()
    s = snap.get(kit.key(20))
    assert s is not None
    tb, _t = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20, weight=500_000, max_size_rao=Rao(100 * TAO))])])
    assert val(tb, kit, 20) == 100 * TAO
    tb2, _ = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20, alpha_h_ppm=Ppm(2_000))])])
    assert val(tb2, kit, 20) == v_star(s.pool, 2_000)               # T (alpha_h - 2f) / 4 ~ 24.8 TAO
    assert 24 * TAO < val(tb2, kit, 20) < 25 * TAO
    expired = replace(sig(kit, "carry", 20, horizon_blocks=10), asof=Block(B - 20))
    tb3, _ = run(kit, [(sleeve("carry"), [expired])])
    assert val(tb3, kit, 20) == 0
    reason = [r for r in tb2.get(kit.key(20)).reasons if r.startswith("alpha_h_ppm=")]   # type: ignore[union-attr]
    assert reason == ["alpha_h_ppm=2000"]


def test_exit_and_avoid(kit: ModuleType) -> None:
    snap = kit.big_market()
    k = kit.key(20)
    pf = kit.portfolio(cash=1_000 * TAO, positions=[kit.position(k, kit.vhk(20), 2_500 * TAO)])   # ~100 TAO
    t = kit.tick(snap, portfolio=pf)
    cur = int(holdings(t)[k].value)
    tb, _ = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20), sig(kit, "carry", 20, SignalKind.EXIT,
                                                                  urgency=Urgency.HIGH)])], pf=pf)
    assert val(tb, kit, 20) == 0 and tb.get(k).urgency is Urgency.HIGH          # type: ignore[union-attr]
    tb2, _ = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20), sig(kit, "carry", 20, SignalKind.AVOID)])], pf=pf)
    assert val(tb2, kit, 20) == cur                                 # no increase beyond the holding
    tb3, _ = run(kit, [(sleeve("carry"), [])], pf=pf)
    assert val(tb3, kit, 20) == 0                                   # held without a live TARGET -> 0


def test_sum_then_cap_priority(kit: ModuleType) -> None:
    snap = kit.big_market()
    k = kit.key(20)
    pf = kit.portfolio(cash=5_000 * TAO, positions=[kit.position(k, kit.vhk(20), 1_250 * TAO)],
                       sleeves=[SleeveHolding(StrategyId("a"), k, Decimal(1_250 * TAO), Rao(0))],
                       sleeve_cash=[("a", 2_500 * TAO), ("b", 2_500 * TAO)])
    t = kit.tick(snap, portfolio=pf)
    cur = int(holdings(t)[k].value)
    cap = cur + 20 * TAO
    rows = [(sleeve("a", Stage.PAPER, 500_000), [sig(kit, "a", 20, max_size_rao=Rao(cur + 30 * TAO))]),
            (sleeve("b", Stage.LIVE_ELIGIBLE, 500_000), [sig(kit, "b", 20, max_size_rao=Rao(30 * TAO))])]
    tb, _ = run(kit, rows, pf=pf, caps={k: Rao(cap)})
    assert val(tb, kit, 20) == cap
    # the holding of "a" first, then the higher stage ("b" LIVE_ELIGIBLE) gets the 20 TAO of room
    assert tb.transfers == ()
    item = tb.get(k)
    assert item is not None and dict(item.attribution) == {StrategyId("b"): PPM}


def test_n_eff_drops_the_lowest_edge_value(kit: ModuleType) -> None:
    pf = kit.portfolio(cash=5 * TAO)                                 # gross 4 TAO -> N_eff 2
    rows = [(sleeve("carry"), [sig(kit, "carry", 20, weight=330_000, edge_ppm_day=PpmPerDay(100)),
                               sig(kit, "carry", 21, weight=330_000, edge_ppm_day=PpmPerDay(50)),
                               sig(kit, "carry", 22, weight=330_000, edge_ppm_day=PpmPerDay(300))])]
    tb, _ = run(kit, rows, pf=pf)
    assert val(tb, kit, 21) == 0 and val(tb, kit, 20) > 0 and val(tb, kit, 22) > 0


def test_netting_transfers_and_attribution(kit: ModuleType) -> None:
    snap = kit.big_market()
    k = kit.key(20)
    pf = kit.portfolio(cash=1_000 * TAO, positions=[kit.position(k, kit.vhk(20), 2_500 * TAO)],
                       sleeves=[SleeveHolding(StrategyId("a"), k, Decimal(2_500 * TAO), Rao(0))],
                       sleeve_cash=[("a", 500 * TAO), ("b", 500 * TAO)])
    t = kit.tick(snap, portfolio=pf)
    cur = int(holdings(t)[k].value)
    rows = [(sleeve("a", budget=500_000), [sig(kit, "a", 20, SignalKind.EXIT)]),
            (sleeve("b", budget=500_000), [sig(kit, "b", 20, max_size_rao=Rao(cur // 2))])]
    tb, _ = run(kit, rows, pf=pf)
    assert val(tb, kit, 20) == cur // 2                              # only the net leaves the book
    assert len(tb.transfers) == 1
    x = tb.transfers[0]
    assert (x.from_strategy, x.to_strategy) == ("a", "b") and 0 < x.shares <= Decimal(2_500 * TAO)
    s = snap.get(k)
    assert s is not None
    assert x.price == s.pool.spot_rao() and x.tao == int(s.hotkey(kit.vhk(20)).value_of(x.shares)) * x.price // TAO  # type: ignore[union-attr]
    item = tb.get(k)
    assert item is not None and dict(item.attribution) == {StrategyId("a"): PPM}    # the residual seller


def test_router_hotkeys_and_no_validator(kit: ModuleType) -> None:
    snap = kit.big_market()
    k = kit.key(20)
    pf = kit.portfolio(positions=[kit.position(k, kit.vhk(20), 2_500 * TAO)])
    bv = kit.book_view(router=RouterState(choice=((k, kit.hk(777)),)))
    tb, _ = run(kit, [(sleeve("carry"), [sig(kit, "carry", 20)])], pf=pf, bv=bv)
    assert tb.get(k).hotkey == kit.hk(777)                           # type: ignore[union-attr]
    tb2, _ = run(kit, [(sleeve("carry"), [sig(kit, "carry", 21)])])
    assert tb2.get(kit.key(21)).hotkey == kit.vhk(21)                # type: ignore[union-attr]
    fr = kit.frame(snap, feat_over={22: {"router_candidates": ()}})
    book = kit.book_cfg((sleeve("carry"),))
    t = kit.tick(snap, fr=fr)
    tb3 = StandardAllocator(book)([(sleeve("carry"), StrategyOutput((sig(kit, "carry", 22),), None))], t,
                                  {s.key: Rao(10**18) for s in snap.subnets})
    assert tb3.get(kit.key(22)) is None


@settings(max_examples=60)
@given(st.lists(st.tuples(st.sampled_from(["a", "b", "c"]), st.integers(16, 30),
                          st.sampled_from([SignalKind.TARGET, SignalKind.TARGET, SignalKind.EXIT, SignalKind.AVOID]),
                          st.integers(0, PPM), st.integers(0, 500)), max_size=12),
       st.lists(st.tuples(st.sampled_from(["a", "b", "c"]), st.integers(16, 30), st.integers(1, 400)), max_size=5,
                unique_by=lambda x: x[1]),
       st.integers(1, 3_000), st.integers(0, 600))
def test_allocator_properties(kit: ModuleType, sigs, held, cash_tao: int, cap_tao: int) -> None:
    snap = kit.big_market()
    positions = [kit.position(kit.key(n), kit.vhk(n), q * 25 * TAO) for _, n, q in held]
    sleeves_h = [SleeveHolding(StrategyId(sid), kit.key(n), Decimal(q * 25 * TAO), Rao(0)) for sid, n, q in held]
    third = cash_tao * TAO // 3
    pf = kit.portfolio(cash=3 * third, positions=positions, sleeves=sleeves_h,
                       sleeve_cash=[("a", third), ("b", third), ("c", third)])
    t = kit.tick(snap, portfolio=pf)
    by: dict[str, list[Signal]] = {"a": [], "b": [], "c": []}
    for sid, n, kind, w, edge in sigs:
        by[sid].append(sig(kit, sid, n, kind, weight=w, edge_ppm_day=PpmPerDay(edge)))
    rows = [(sleeve(sid, Stage.PAPER, 333_333), by[sid]) for sid in ("a", "b", "c")]
    caps = {s.key: Rao(cap_tao * TAO) for s in snap.subnets}
    book = kit.book_cfg(tuple(sl for sl, _ in rows))
    tb = StandardAllocator(book)([(sl, StrategyOutput(tuple(ss), None)) for sl, ss in rows], t, caps)
    gross_cap = g_max_eff_ppm(RiskCfg(), t.book_view, int(t.nav_liq), B) * int(t.nav_liq) // PPM
    assert sum(int(i.value_rao) for i in tb.items) <= gross_cap
    assert all(int(i.value_rao) <= int(caps[i.key]) for i in tb.items)
    assert all(sum(p for _, p in i.attribution) == PPM for i in tb.items)
    # transfers move shares between sleeves within their holdings: sleeve shares still sum to the position
    moved: dict[tuple[str, object], Decimal] = {}
    for x in tb.transfers:
        moved[(x.from_strategy, x.key)] = moved.get((x.from_strategy, x.key), Decimal(0)) + x.shares
        assert x.from_strategy != x.to_strategy and x.shares > 0 and x.tao > 0
    for (sid, key), sh in moved.items():
        h = next(h for h in pf.sleeves if h.strategy == sid and h.key == key)
        assert sh <= h.shares
    # EXIT honoured: a key where every holder exits and nobody targets it goes to 0
    hv = {k: int(h.value) for k, h in holdings(t).items()}
    for item in tb.items:
        kinds = {(s_.strategy, s_.kind) for ss in by.values() for s_ in ss if s_.key == item.key}
        if kinds and all(kd is SignalKind.EXIT for _, kd in kinds):
            assert item.value_rao == 0
        if kinds and all(kd is SignalKind.AVOID for _, kd in kinds):
            assert item.value_rao <= hv.get(item.key, 0)


def test_exiting_seller_hands_over_all_its_shares(kit: ModuleType) -> None:

    k = kit.key(20)
    pf = kit.portfolio(cash=1_000 * TAO, positions=[kit.position(k, kit.vhk(20), 2_500 * TAO)],
                       sleeves=[SleeveHolding(StrategyId("a"), k, Decimal(1_000 * TAO), Rao(0)),
                                SleeveHolding(StrategyId("b"), k, Decimal(1_500 * TAO), Rao(0))],
                       sleeve_cash=[("a", 500 * TAO), ("b", 500 * TAO)])
    rows = [(sleeve("a", budget=500_000), [sig(kit, "a", 20, SignalKind.EXIT)]),
            (sleeve("b", budget=500_000), [sig(kit, "b", 20, max_size_rao=Rao(200 * TAO))])]
    tb, _ = run(kit, rows, pf=pf)
    assert len(tb.transfers) == 1 and tb.transfers[0].shares == Decimal(1_000 * TAO)   # no residue left with "a"
