"""risk.liquidity: the CapsFn (V_cap of DESIGN.md 3.5), section I aggregates and the valuation helpers."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

from taotrader.core.config import RiskCfg
from taotrader.core.portfolio import SleeveHolding
from taotrader.core.units import PERQUINTILL, PPM, AlphaRao, Block, Coldkey, Rao, StrategyId
from taotrader.protocol.amm import liq_value, quote_sell, v_max
from taotrader.risk.liquidity import (
    aggregate_limits,
    apply_aggregates,
    attribution_from_weights,
    caps,
    exit_shortfall_ppm,
    holding_attribution,
    holdings,
    sleeve_values,
    t_history,
    vcap_detail,
)

B = 9_240_388
TAO = 10**9


def test_vcap_uses_the_3_day_minimum_and_the_stress_haircut(kit: ModuleType) -> None:
    hist = [kit.snapshot(B - 300 * i, subnets=[kit.subnet(5, tao=t * TAO)]) for i, t in ((1, 900), (50, 800), (73, 700))]
    old = kit.snapshot(B - 21_700, subnets=[kit.subnet(5, tao=100 * TAO)])        # older than 3 days: ignored
    raw = kit.snapshot(B, subnets=[kit.subnet(5)])
    store = kit.MemStore(hist + [old, raw], clock=B)
    snaps = t_history(store, raw)
    assert all(B - 21_600 < int(s.block) < B for s in snaps)
    s = raw.get(kit.key(5))
    assert s is not None
    d = vcap_detail(s, 10**15, kit.book_view(), B, None, RiskCfg(), snaps)
    assert d.t_now == 1_000 * TAO and d.t_min_3d == 800 * TAO       # the 700-TAO point is 21,900 blocks old
    assert d.t_st == 640 * TAO
    assert d.base == v_max(Rao(640 * TAO), 15_000) and d.cap == d.base
    assert abs(d.base - 9_746_192_893) <= 1                         # 1.52% of T_st


def test_vcap_escrow_haircut_and_nu_max(kit: ModuleType) -> None:
    cfg = RiskCfg()
    base = kit.subnet(5)
    x = int(base.pool.alpha)
    for e_frac, m in (("0.2", 800_000), ("0.6", 500_000), ("0", PPM)):
        s = kit.subnet(5, escrow_alpha=AlphaRao(int(Decimal(e_frac) * x)))
        d = vcap_detail(s, 10**15, kit.book_view(), B, None, cfg, ())
        assert d.m_esc_ppm == m
        assert d.cap == d.base * m // PPM
    d = vcap_detail(base, 10 * TAO, kit.book_view(), B, None, cfg, ())
    assert d.nu_cap == 1_500_000_000 and d.cap == 1_500_000_000


def test_skewed_weights_solve_the_shortfall_by_bisection(kit: ModuleType) -> None:
    s = kit.subnet(5, w_quote_e18=6 * PERQUINTILL // 10)
    d = vcap_detail(s, 10**15, kit.book_view(), B, None, RiskCfg(), ())
    t_st = 800 * TAO
    scaled = s.pool.__class__(kind=s.pool.kind, tao=Rao(t_st), alpha=AlphaRao(int(s.pool.alpha) * 4 // 5),
                              px_tao=t_st, px_alpha=s.pool.px_alpha * 4 // 5, w_quote_e18=s.pool.w_quote_e18,
                              fee_rate=s.pool.fee_rate)
    alpha = int(Decimal(d.base) / scaled.spot())
    assert quote_sell(scaled, AlphaRao(alpha)).shortfall_ppm <= 15_000 + 1
    assert quote_sell(scaled, AlphaRao(alpha * 102 // 100)).shortfall_ppm > 15_000
    cp = vcap_detail(kit.subnet(5), 10**15, kit.book_view(), B, None, RiskCfg(), ())
    assert d.base != cp.base


def test_caps_function_covers_the_view_and_zeroes_dissolved_holdings(kit: ModuleType) -> None:
    snap = kit.snapshot()
    gone = kit.key(40)
    pf = kit.portfolio(positions=[kit.position(gone, kit.vhk(40), 10 * TAO)])
    t = kit.tick(snap, portfolio=pf, nav=10**15)
    c = caps(t, RiskCfg())
    assert set(c) == {s.key for s in snap.subnets} | {gone}
    assert c[gone] == 0 and all(v > 0 for k, v in c.items() if k != gone)


def test_valuation_helpers(kit: ModuleType) -> None:
    snap = kit.snapshot()
    k = kit.key(3)
    s = snap.get(k)
    assert s is not None
    pos = kit.position(k, kit.vhk(3), 1_000 * TAO)
    pf = kit.portfolio(positions=[pos], sleeves=[SleeveHolding(StrategyId("a"), k, Decimal(750 * TAO), Rao(0)),
                                                 SleeveHolding(StrategyId("b"), k, Decimal(250 * TAO), Rao(0))])
    t = kit.tick(snap, portfolio=pf)
    h = holdings(t)[k]
    assert h.indexed and h.alpha == 1_000 * TAO and h.value == liq_value(s.pool, AlphaRao(1_000 * TAO))
    sv = sleeve_values(pf, holdings(t))
    assert sv[(StrategyId("a"), k)] + sv[(StrategyId("b"), k)] in (h.value, h.value - 1)
    assert holding_attribution(pf, k) == ((StrategyId("a"), 750_000), (StrategyId("b"), 250_000))
    assert attribution_from_weights([(StrategyId("x"), 1), (StrategyId("y"), 2)]) == ((StrategyId("x"), 333_333),
                                                                                       (StrategyId("y"), 666_667))
    assert attribution_from_weights([]) == ((StrategyId("book"), PPM),)
    untracked = kit.tick(snap, portfolio=kit.portfolio(positions=[kit.position(k, kit.hk(5), 7 * TAO)]))
    hu = holdings(untracked)[k]
    assert not hu.indexed and hu.alpha == 7 * TAO                 # floor(shares): a lower bound


def _limits(nav: int, cfg: RiskCfg | None = None):
    return aggregate_limits(nav, 800_000, cfg or RiskCfg())


def test_gross_cap_cuts_increases_before_holdings(kit: ModuleType) -> None:
    snap = kit.big_market()
    nav = 100 * TAO                      # gross cap 80 TAO
    k1, k2, k3 = kit.key(20), kit.key(21), kit.key(22)
    vals = {k1: 50 * TAO, k2: 30 * TAO, k3: 20 * TAO}
    cur = {k1: 50 * TAO, k2: 10 * TAO}
    out, acts = apply_aggregates(vals, cur, snap, snap, RiskCfg(), _limits(nav))
    assert sum(out.values()) == 80 * TAO
    assert out[k1] == 50 * TAO and out[k2] >= 10 * TAO              # holdings untouched, increases cut pro rata
    assert out[k2] - 10 * TAO == out[k3] == 10 * TAO                # the 20-TAO excess split pro rata over increases
    assert any(a.rule == "liquidity.gross" for a in acts)
    # holdings are trimmed (largest exit shortfall first) only when increases are not enough
    held = {k1: 60 * TAO, k2: 40 * TAO}
    out2, _ = apply_aggregates(dict(held), held, snap, snap, RiskCfg(), _limits(nav))
    assert sum(out2.values()) == 80 * TAO


def test_buckets(kit: ModuleType) -> None:
    owner = Coldkey("0x" + "77" * 32)
    young_fe = Block(B - 100_000)
    deep = 100_000 * TAO
    subs = [kit.subnet(i, tao=deep) for i in range(1, 31)]
    subs[16] = kit.subnet(17, tao=deep, owner_coldkey=owner)
    subs[17] = kit.subnet(18, tao=deep, owner_coldkey=owner)
    subs[18] = kit.subnet(19, tao=deep, first_emission_block=young_fe)
    snap = kit.snapshot(subnets=subs, subnet_limit=30, n_nonroot_networks=30)
    nav = 1_000 * TAO
    cfg = RiskCfg(n_max=20)
    # ladder bucket: ranks 1..15 at most 15% of NAV
    out, acts = apply_aggregates({kit.key(14): 100 * TAO, kit.key(15): 100 * TAO, kit.key(16): 100 * TAO}, {}, snap, snap,
                                 cfg, _limits(nav, cfg))
    assert out[kit.key(14)] + out[kit.key(15)] <= 150 * TAO and out[kit.key(16)] == 100 * TAO
    assert any(a.rule == "liquidity.ladder_bucket" for a in acts)
    # owner cluster <= 20%
    out, acts = apply_aggregates({kit.key(17): 150 * TAO, kit.key(18): 150 * TAO}, {}, snap, snap, cfg, _limits(nav, cfg))
    assert out[kit.key(17)] + out[kit.key(18)] <= 200 * TAO and any(a.rule == "liquidity.owner_cluster" for a in acts)
    # young (since_start < 30 d) <= 10%
    out, _ = apply_aggregates({kit.key(19): 150 * TAO}, {}, snap, snap, cfg, _limits(nav, cfg))
    assert out[kit.key(19)] == 100 * TAO
    # LCW <= 5% (keys whose attribution is mostly LCW)
    out, _ = apply_aggregates({kit.key(20): 80 * TAO}, {}, snap, snap, cfg, _limits(nav, cfg), {kit.key(20): PPM})
    assert out[kit.key(20)] == 50 * TAO


def test_n_eff_drops_new_entries_only(kit: ModuleType) -> None:
    snap = kit.big_market()
    nav = 5 * TAO                                    # gross 4 TAO -> N_eff = floor(4 / (4 * 0.5)) = 2
    lim = _limits(nav)
    assert lim.n_eff == 2
    vals = {kit.key(20): TAO, kit.key(21): TAO, kit.key(22): TAO // 2}
    cur = {kit.key(20): TAO}
    out, acts = apply_aggregates(vals, cur, snap, snap, RiskCfg(), lim)
    assert out[kit.key(22)] == 0 and out[kit.key(20)] == TAO and out[kit.key(21)] == TAO
    assert any(a.rule == "liquidity.n_eff" for a in acts)


def test_hold_budget_trims_large_exit_shortfalls(kit: ModuleType) -> None:
    snap = kit.snapshot()
    s = snap.get(kit.key(12))
    assert s is not None
    nav = 10_000 * TAO
    big = 60 * TAO                                   # 6% of a 1,000-TAO pool: ES ~ 5.7% > 2.5%
    assert exit_shortfall_ppm(s.pool, big) > 25_000
    out, acts = apply_aggregates({kit.key(12): big}, {kit.key(12): big}, snap, snap, RiskCfg(), _limits(nav))
    assert out[kit.key(12)] == v_max(s.pool.tao, 15_000)
    assert any(a.rule == "liquidity.hold_es" for a in acts)
    # exit budget: sum ES * V <= 2% of NAV; the largest ES is trimmed first
    small_nav = 30 * TAO                               # exit budget: 2% * 30 TAO = 0.6 TAO of expected shortfall
    vals = {kit.key(i): 13 * TAO for i in (10, 11, 12)}  # ES ~1.33% each: 3 x 0.173 TAO = 0.52 TAO
    lim = aggregate_limits(small_nav, 10_000_000, RiskCfg())   # gross / buckets out of the way
    lim = lim.__class__(**{**{f: getattr(lim, f) for f in lim.__slots__}, "ladder_cap": 10**15, "young_cap": 10**15,
                           "owner_cap": 10**15, "n_eff": 12})
    out2, _ = apply_aggregates(vals, dict(vals), snap, snap, RiskCfg(), lim)
    assert out2 == vals
    vals3 = {kit.key(i): 15 * TAO for i in (10, 11, 12)}
    out3, acts3 = apply_aggregates(vals3, dict(vals3), snap, snap, RiskCfg(), lim)
    used = sum(exit_shortfall_ppm(snap.get(k).pool, v) * v // PPM for k, v in out3.items() if v > 0)  # type: ignore[union-attr]
    assert used <= lim.exit_budget and any(a.rule == "liquidity.exit_budget" for a in acts3)


def test_skewed_solver_matches_quote_sell_across_weights(kit: ModuleType) -> None:
    """The size-free Newton solve gives the largest one-shot exit within S_EXIT_ENTRY on the T_st-scaled pool."""
    for wq in (30, 35, 42, 48, 52, 58, 65, 70):
        for fee in (0, 33, 196, 330):
            s = kit.subnet(5, w_quote_e18=wq * PERQUINTILL // 100, fee_rate=fee)
            d = vcap_detail(s, 10**18, kit.book_view(), B, None, RiskCfg(), ())
            if abs(wq - 50) <= 1:
                assert d.base == v_max(Rao(800 * TAO), 15_000)
                continue
            st = s.pool.__class__(kind=s.pool.kind, tao=Rao(800 * TAO), alpha=AlphaRao(int(s.pool.alpha) * 4 // 5),
                                  px_tao=800 * TAO, px_alpha=s.pool.px_alpha * 4 // 5, w_quote_e18=s.pool.w_quote_e18,
                                  fee_rate=fee)
            alpha = int(Decimal(d.base) / st.spot())
            assert quote_sell(st, AlphaRao(alpha)).shortfall_ppm <= 15_001, (wq, fee)
            assert quote_sell(st, AlphaRao(alpha * 1_001 // 1_000)).shortfall_ppm >= 15_000, (wq, fee)
