"""risk.prune_guard: never hold the target, Tier A (incl. the U = 15 coverage table), backstop (Delta >= 46,080),
Tier B, entry floor, s_emerg (DESIGN.md 3.3)."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

import pytest

from taotrader.core.config import RiskCfg
from taotrader.core.orders import Urgency
from taotrader.core.units import Block, NetUid, Ppm
from taotrader.protocol.ema import ema_alpha, t_star_blocks
from taotrader.risk.hazard_mc import McResult
from taotrader.risk.prune_guard import (
    entry_vetoes,
    expected_loss_ppm_day,
    held_exits,
    prune_context,
    s_emerg_ppm,
    t_star_moving,
    tier_a_gap_covered,
    unwind_blocks,
)

B = 9_240_388
R35 = Decimal("0.35")


def _exits(kit: ModuleType, snap, held, cfg: RiskCfg | None = None, feats=None, p24=None):
    c = cfg or RiskCfg()
    pc = prune_context(snap, c)
    rec = dict.fromkeys(held, R35)
    return pc, held_exits(snap, pc, held, c, rec, feats or {}, p24)


def test_unwind_time_is_15_blocks() -> None:
    assert unwind_blocks(RiskCfg()) == 15
    assert prune_context.__name__  # module import sanity


@pytest.mark.parametrize(("r", "expected"), [("1", 50_000), ("0", 250_000), ("0.6", 200_000), ("0.8", 100_000),
                                             ("0.368", 250_000), ("0.95", 50_000), ("1.5", 50_000), ("-1", 250_000)])
def test_s_emerg_clamp(r: str, expected: int) -> None:
    assert s_emerg_ppm(Decimal(r)) == expected


def test_never_hold_the_target_even_with_the_window_closed(kit: ModuleType) -> None:
    snap = kit.snapshot(last_reg_block=Block(B - 100))          # window opens in 14,300 blocks
    pc, ex = _exits(kit, snap, [kit.key(1), kit.key(5)])
    assert not pc.zone_open and pc.target == kit.key(1)
    assert [(e.key, e.urgency, e.rule) for e in ex] == [(kit.key(1), Urgency.EMERGENCY, "prune_target")]
    assert ex[0].slip_ppm == s_emerg_ppm(R35) == 250_000          # 0.5 * (1 - 0.35) = 32.5% -> clamped to 25%


def test_runtime_target_wins_on_a_mismatch(kit: ModuleType) -> None:
    snap = kit.snapshot(runtime_prune_target=NetUid(5))
    pc, ex = _exits(kit, snap, [kit.key(5)])
    assert pc.runtime_target == kit.key(5) and pc.targets == (kit.key(1), kit.key(5))
    assert ex[0].rule == "prune_target" and "target=runtime" in ex[0].detail


def test_prune_not_possible_disables_every_exit(kit: ModuleType) -> None:
    snap = kit.snapshot(n_nonroot_networks=11)
    pc, ex = _exits(kit, snap, [kit.key(1), kit.key(2)])
    assert not pc.possible and ex == []


def _near_bottom(kit: ModuleType, gap: Decimal, *, k_reg: int = 8_000_002, last_reg: int = 9_210_610,
                 k_over: dict | None = None, **glob):
    e1 = Decimal("0.01")
    ek = e1 * (1 + gap)
    subs = [kit.subnet(1, price=e1), kit.subnet(2, price=ek, reg_at=k_reg, **(k_over or {})),
            kit.subnet(3, price=Decimal("0.05"))]
    return kit.snapshot(subnets=subs, last_reg_block=Block(last_reg), **glob)


def test_tier_a_fires_inside_the_zone_only(kit: ModuleType) -> None:
    snap = _near_bottom(kit, Decimal("0.02"))
    _, ex = _exits(kit, snap, [kit.key(2)])
    assert [(e.rule, e.urgency) for e in ex] == [("prune_A", Urgency.EMERGENCY)]
    assert "horizon=315" in ex[0].detail
    far = _near_bottom(kit, Decimal("0.02"), last_reg=B - 100)
    _, ex2 = _exits(kit, far, [kit.key(2)])
    assert ex2 == []
    # window opening within U + M_A counts as open
    near = _near_bottom(kit, Decimal("0.02"), last_reg=B - 14_400 + 300)
    _, ex3 = _exits(kit, near, [kit.key(2)])
    assert [e.rule for e in ex3] == ["prune_A"]
    beyond = _near_bottom(kit, Decimal("0.02"), last_reg=B - 14_400 + 316)
    assert _exits(kit, beyond, [kit.key(2)])[1] == []


def test_tier_a_closed_form_feature_is_a_fail_closed_trigger(kit: ModuleType) -> None:
    snap = _near_bottom(kit, Decimal("0.5"))                   # moving t* never crosses
    s2 = snap.get(kit.key(2))
    assert s2 is not None
    feats = {kit.key(2): kit.feat(s2, t_star_stress_blocks=200.0)}
    _, ex = _exits(kit, snap, [kit.key(2)], feats=feats)
    assert [e.rule for e in ex] == ["prune_A"] and "t_star=none" in ex[0].detail


TABLE = [(150, "0.024", "0.048"), (300, "0.045", "0.094"), (600, "0.088", "0.192"), (1_200, "0.172", "0.416")]


@pytest.mark.parametrize(("m_a", "g50", "g0"), TABLE)
def test_tier_a_coverage_table_is_reproduced_by_the_t_star_code(kit: ModuleType, m_a: int, g50: str, g0: str) -> None:
    """Section 3.3 table (a = 0.000286, U = 15): the gap covered at a 50% crash and at spot -> 0, rounded to 0.1 pp;
    then the real Tier A path (protocol.prune.time_to_target with a moving bottom) fires just inside the covered gap
    and not just outside it, and the closed form t* agrees with brute-force stepping."""
    a = Decimal("0.000286")
    horizon = 15 + m_a
    for crash_ppm, want in ((500_000, g50), (1_000_000, g0)):
        g = tier_a_gap_covered(a, horizon, crash_ppm)
        assert abs(g - Decimal(want)) < Decimal("0.0006"), (m_a, crash_ppm, g)
        # a(b) = 0.0003 * b / (b + 201,600) == 0.000286 exactly at b = 4,118,400
        fe = B - 4_118_400 + 1
        cfg = RiskCfg(margin_a_blocks=m_a, d_stress_ppm=Ppm(crash_ppm))
        for delta, fires in ((Decimal("-0.001"), True), (Decimal("0.001"), False)):
            gap = g + delta
            snap = _near_bottom(kit, gap, k_reg=5_000_000, k_over={"first_emission_block": Block(fe)},
                                moving_alpha=Decimal("0.0003"))
            s2 = snap.get(kit.key(2, 5_000_000))
            assert s2 is not None and ema_alpha(snap.glob, s2, snap.block) == a
            pc = prune_context(snap, cfg)
            ex = held_exits(snap, pc, [s2.key], cfg, {s2.key: R35}, {})
            assert ([e.rule for e in ex] == ["prune_A"]) is fires, (m_a, crash_ppm, delta)
            t_moving = t_star_moving(snap, s2.key, cfg, 2 * horizon)
            bottom = snap.get(kit.key(1))
            assert bottom is not None
            stressed = s2.pool.spot() * (Decimal(1) - Decimal(crash_ppm) / Decimal(1_000_000))
            t_closed = t_star_blocks(s2.moving_price, bottom.moving_price, stressed, a)
            assert t_moving is not None and t_closed is not None
            assert abs(t_moving - t_closed) <= 1                  # closed form == moving-bottom search (flat bottom)
            assert (t_moving <= horizon) is fires


def test_tier_a_fires_for_a_held_immune_subnet_expiring_inside_the_horizon(kit: ModuleType) -> None:
    expiry = B + 100
    reg = expiry - 864_000
    subs = [kit.subnet(1, price=Decimal("0.01")), kit.subnet(2, price=Decimal("0.005"), reg_at=reg),
            kit.subnet(3, price=Decimal("0.05"))]
    snap = kit.snapshot(subnets=subs)
    k = kit.key(2, reg)
    pc, ex = _exits(kit, snap, [k])
    assert k not in pc.ranks                                    # immune now
    assert [e.rule for e in ex] == ["prune_A"]
    assert "t_star=100" in ex[0].detail
    later = [kit.subnet(1, price=Decimal("0.01")), kit.subnet(2, price=Decimal("0.005"), reg_at=B + 400 - 864_000),
             kit.subnet(3, price=Decimal("0.05"))]
    assert _exits(kit, kit.snapshot(subnets=later), [kit.key(2, B + 400 - 864_000)])[1] == []


@pytest.mark.parametrize(("delta", "rank", "fires"), [(46_080, 2, True), (46_079, 2, False), (50_000, 3, True),
                                                      (50_000, 4, False), (46_080, 1, True)])
def test_backstop_triggers_at_delta_46080(kit: ModuleType, delta: int, rank: int, fires: bool) -> None:
    snap = kit.snapshot(last_reg_block=Block(B - delta))
    pc, ex = _exits(kit, snap, [kit.key(rank)])
    want = "prune_target" if rank == 1 else "prune_backstop"
    if rank == 1:
        assert [e.rule for e in ex] == [want]
        return
    assert ([e.rule for e in ex] == [want]) is fires
    if fires:
        assert ex[0].urgency is Urgency.URGENT and ex[0].slip_ppm == RiskCfg().s_urgent_ppm
    assert (pc.cost_ratio <= Decimal("1.2")) is (delta >= 46_080)


def test_tier_b_only_when_enabled(kit: ModuleType) -> None:
    snap = kit.snapshot()
    k = kit.key(4)
    p24 = McResult(snap.block, 7_200, 2_000, 1, ((k, Ppm(20_000)),), Ppm(100_000))
    _, off = _exits(kit, snap, [k], p24=p24)
    assert off == []
    _, on = _exits(kit, snap, [k], cfg=RiskCfg(tier_b_enabled=True), p24=p24)
    assert [(e.rule, e.urgency) for e in on] == [("prune_B", Urgency.URGENT)]      # 2% * 0.65 = 1.3% >= 0.75%
    low = McResult(snap.block, 7_200, 2_000, 1, ((k, Ppm(10_000)),), Ppm(100_000))  # 1% * 0.65 < 0.75%
    assert _exits(kit, snap, [k], cfg=RiskCfg(tier_b_enabled=True), p24=low)[1] == []


def test_entry_vetoes(kit: ModuleType) -> None:
    cfg = RiskCfg()
    snap = kit.snapshot(last_reg_block=Block(B - 47_000))       # backstop zone (r < 1.2)
    pc = prune_context(snap, cfg)
    keys = [kit.key(i) for i in (1, 2, 3, 4, 8)]
    s4 = snap.get(kit.key(4))
    assert s4 is not None
    feats = {kit.key(4): kit.feat(s4, t_star_stress_blocks=100.0)}
    v = entry_vetoes(snap, pc, keys, cfg, feats, {}, {}, 10**12, None)
    assert v[kit.key(1)][0] == "prune.entry_target"
    assert v[kit.key(2)][0] == "prune.entry_backstop_zone" and v[kit.key(3)][0] == "prune.entry_backstop_zone"
    assert v[kit.key(4)][0] == "prune.entry_tier_a_zone"
    assert kit.key(8) not in v
    # ladder bucket full: 15% of NAV in rank <= 15 names blocks increases there
    pc2 = prune_context(kit.snapshot(), cfg)
    v2 = entry_vetoes(kit.snapshot(), pc2, [kit.key(8)], cfg, {}, {}, {kit.key(9): 150}, 1_000, None)
    assert v2[kit.key(8)][0] == "prune.ladder_bucket"
    assert entry_vetoes(kit.snapshot(), pc2, [kit.key(8)], cfg, {}, {}, {kit.key(9): 149}, 1_000, None) == {}
    # MC enabled: no result -> fail closed; a result applies P_7d * (1 - R) <= 1%
    on = RiskCfg(tier_b_enabled=True)
    assert entry_vetoes(kit.snapshot(), pc2, [kit.key(8)], on, {}, {}, {}, 10**12, None)[kit.key(8)][0] == "prune.entry_mc"
    p7 = McResult(Block(B), 50_400, 2_000, 1, ((kit.key(8), Ppm(20_000)),), Ppm(0))
    assert kit.key(8) in entry_vetoes(kit.snapshot(), pc2, [kit.key(8)], on, {}, {kit.key(8): R35}, {}, 10**12, p7)
    p7ok = McResult(Block(B), 50_400, 2_000, 1, ((kit.key(8), Ppm(15_000)),), Ppm(0))
    assert entry_vetoes(kit.snapshot(), pc2, [kit.key(8)], on, {}, {kit.key(8): R35}, {}, 10**12, p7ok) == {}


def test_expected_loss_uses_kappa_and_recovery(kit: ModuleType) -> None:
    snap = kit.snapshot()
    fr = kit.frame(snap, p_reg_day_ppm=100_000)
    p, lam, loss = expected_loss_ppm_day(fr, snap, kit.key(2), Decimal("0.5"), None) or (0, 0, 0)
    # rho = 2, exp(-4 * 1) = 0.0183 -> lambda = 1,831 ppm/day, loss = 915
    assert p == 100_000 and lam == 1_831 and loss == 915
    _p1, lam1, _ = expected_loss_ppm_day(fr, snap, kit.key(1), Decimal("0.5"), None) or (0, 0, 0)
    assert lam1 == 100_000                                          # the bottom itself: rho = 1


def test_never_target_prefilter_is_exact(kit: ModuleType) -> None:
    """Whenever the cheap monotone-path pre-filter skips a key, the full moving-bottom search agrees (None)."""
    from taotrader.risk.prune_guard import _never_target

    cfg = RiskCfg()
    skipped = 0
    for gap in ("0.001", "0.01", "0.02", "0.03", "0.05", "0.08", "0.1", "0.3", "1"):
        for crash in (300_000, 500_000, 900_000, 1_000_000):
            c = RiskCfg(d_stress_ppm=Ppm(crash))
            snap = _near_bottom(kit, Decimal(gap))
            pc = prune_context(snap, c)
            k = kit.key(2)
            if _never_target(snap, pc, k, c):
                skipped += 1
                assert t_star_moving(snap, k, c, pc.horizon) is None, (gap, crash)
    assert skipped > 0 and cfg.unwind_exec_blocks == 5
