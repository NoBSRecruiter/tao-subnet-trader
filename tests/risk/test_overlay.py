"""risk.overlay.StandardOverlay: the fixed rule order of DESIGN.md 3.1-3.11 on hand-built BookViews - universe floor
(A-G per book, H cooldowns), prune / emission / owner exits, caps, aggregates, modes, monotonicity."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from taotrader.core.config import RiskCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEventKind
from taotrader.core.fixed import DEC
from taotrader.core.orders import OrderKind, OrderState, Urgency
from taotrader.core.signals import SleeveXfer
from taotrader.core.state import Quality
from taotrader.core.units import BLOCKS_PER_DAY, Block, Mode, NetUid, PriceRao, Rao, RunMode, StrategyId
from taotrader.protocol.calibration import Calibration, sealed
from taotrader.protocol.prune import hazard_from_table
from taotrader.protocol.sellload import SellLoadParams
from taotrader.risk.liquidity import holdings
from taotrader.risk.modes import ModeSignals
from taotrader.risk.overlay import StandardOverlay

B = 9_240_388
TAO = 10**9


def _entry(kit: ModuleType, value: int = 10 * TAO, *, snap=None, fr=None, bv=None, events=(), pf=None, netuid: int = 20,
           cfg: RiskCfg | None = None, overlay: StandardOverlay | None = None, **ctx_kw):
    snap = snap if snap is not None else kit.big_market()
    k = kit.key(netuid)
    s = snap.get(k)
    hot = s.hotkeys[0].hotkey if s is not None and s.hotkeys else kit.vhk(netuid)
    t = kit.tick(snap, fr=fr if fr is not None else kit.frame(snap), bv=bv, events=events, portfolio=pf)
    proposal = kit.target_book([kit.target(k, hot, value)])
    d = (overlay or StandardOverlay()).review(proposal, kit.risk_ctx(t, cfg=cfg, **ctx_kw))
    return d, k


def test_clean_entry_passes_untouched(kit: ModuleType) -> None:
    d, k = _entry(kit)
    assert d.mode is Mode.NORMAL and not d.targets.halt_entries
    assert d.targets.get(k).value_rao == 10 * TAO                  # type: ignore[union-attr]
    assert not any(a.action in ("VETO_ENTRY", "CLAMP", "FORCE_EXIT") for a in d.actions)


@pytest.mark.parametrize(("over", "rule"), [
    ({"subtoken_enabled": False}, "universe.A"),
    ({"reg_allowed": False}, "universe.A"),
    ({"tao": 150 * TAO}, "universe.B"),
    ({"fee_rate": 400}, "universe.B"),
    ({"emission_enabled": False}, "universe.C"),
    ({"first_emission_block": Block(B - 50_000)}, "universe.E"),
    ({"miner_burned": Decimal("0.6")}, "universe.F"),
])
def test_universe_sections_veto_entries(kit: ModuleType, over: dict, rule: str) -> None:
    snap = kit.big_market()
    subs = [s if s.key.netuid != 20 else kit.subnet(20, **{"tao": 100_000 * TAO, **over}) for s in snap.subnets]
    snap2 = kit.snapshot(subnets=subs, subnet_limit=30, n_nonroot_networks=30)
    d, k = _entry(kit, snap=snap2)
    assert d.targets.get(k).value_rao == 0                         # type: ignore[union-attr]
    assert any(a.rule.startswith(rule) and a.action == "VETO_ENTRY" for a in d.actions), [a.rule for a in d.actions]


def test_prune_floor_and_validator_sections(kit: ModuleType) -> None:
    d, _ = _entry(kit, netuid=4)                                   # rank 4 < 6
    assert any(a.rule.startswith("universe.") and "D" in a.rule for a in d.actions)
    snap = kit.big_market()
    fr = kit.frame(snap, feat_over={20: {"router_candidates": (kit.candidate(kit.vhk(20), eligible=False),)}})
    d2, k = _entry(kit, snap=snap, fr=fr)
    assert d2.targets.get(k).value_rao == 0                        # type: ignore[union-attr]
    assert any(a.rule == "universe.G" for a in d2.actions)
    fr3 = kit.frame(snap, feat_over={20: {"launch_flags": frozenset({"SEED_ANOMALY"})}})
    d3, _ = _entry(kit, snap=snap, fr=fr3)
    assert any(a.rule == "universe.E" for a in d3.actions)


def test_section_h_cooldowns_failures_and_abnormal_fills(kit: ModuleType) -> None:
    k = kit.key(20)
    d, _ = _entry(kit, bv=kit.book_view(cooldowns=((k, "owner.sold", Block(B + 5)),)))
    assert any(a.rule == "cooldown.owner.sold" for a in d.actions) and d.targets.get(k).value_rao == 0  # type: ignore[union-attr]
    d2, _ = _entry(kit, bv=kit.book_view(fail_counts_600=((NetUid(20), 3),), fail_count_600_book=3))
    start = [a for a in d2.actions if a.rule == "fail_burst"]
    assert start and f"cooldown_until={B + BLOCKS_PER_DAY}" in start[0].detail
    assert any(a.rule == "cooldown.fail_burst" for a in d2.actions)
    # an abnormal fill: realised shortfall 2% where 0.5% was modelled
    rec = kit.order(k, kit.vhk(20), OrderKind.ADD_STAKE_LIMIT, OrderState.FILLED, tao_in=TAO, created=B - 300,
                    expected_out=24_875_000_000)                  # 24.875 alpha at 0.04 -> modelled 0.5%
    f = kit.fill(rec, block=B - 295, shortfall_ppm=20_000, spot_before=40_000_000)
    d3, _ = _entry(kit, bv=kit.book_view(orders=(rec,), recent_fills=(f,)))
    ab = [a for a in d3.actions if a.rule == "abnormal_fill"]
    assert ab and f"cooldown_until={B - 295 + BLOCKS_PER_DAY}" in ab[0].detail
    f_ok = kit.fill(rec, block=B - 295, shortfall_ppm=5_500, spot_before=40_000_000)
    d4, _ = _entry(kit, bv=kit.book_view(orders=(rec,), recent_fills=(f_ok,)))
    assert not any(a.rule == "abnormal_fill" for a in d4.actions)


def test_halts_and_caution_cap_increases_but_keep_holdings(kit: ModuleType) -> None:
    snap = kit.big_market()
    held, new = kit.key(21), kit.key(22)
    pf = kit.portfolio(positions=[kit.position(held, kit.vhk(21), 500 * TAO)])
    t = kit.tick(snap, fr=kit.frame(snap, model_ok=False), portfolio=pf)
    cur = int(holdings(t)[held].value)
    proposal = kit.target_book([kit.target(held, kit.vhk(21), cur + 5 * TAO), kit.target(new, kit.vhk(22), 5 * TAO)])
    d = StandardOverlay().review(proposal, kit.risk_ctx(t))
    assert d.mode is Mode.CAUTION and d.targets.halt_entries
    assert d.targets.get(held).value_rao == cur and d.targets.get(new).value_rao == 0   # type: ignore[union-attr]
    t2 = kit.tick(snap, portfolio=pf)
    d2 = StandardOverlay().review(proposal, kit.risk_ctx(t2, orphans=1))
    assert d2.mode is Mode.NORMAL and d2.targets.halt_entries and d2.targets.get(new).value_rao == 0  # type: ignore[union-attr]
    wave = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(i), flag=False) for i in (1, 2, 3)]
    d3 = StandardOverlay().review(proposal, kit.risk_ctx(kit.tick(snap, portfolio=pf, events=wave)))
    w = [a for a in d3.actions if a.rule == "emission.wave"]
    assert w and f"halt_until={B + BLOCKS_PER_DAY}" in w[0].detail and d3.targets.halt_entries
    bans = [a for a in d3.actions if a.rule == "emission_off"]
    assert len(bans) == 3 and f"cooldown_until={B + 100_800}" in bans[0].detail


def test_forced_exits_and_precedence(kit: ModuleType) -> None:
    subs = [kit.subnet(i, emission_enabled=(i not in (1, 7))) for i in range(1, 13)]
    snap = kit.snapshot(subnets=subs)
    pos = [kit.position(kit.key(i), kit.vhk(i), 100 * TAO) for i in (1, 7, 9)]
    gone = kit.key(40)
    pf = kit.portfolio(positions=pos + [kit.position(gone, kit.vhk(40), TAO)])
    t = kit.tick(snap, portfolio=pf)
    proposal = kit.target_book([kit.target(kit.key(i), kit.vhk(i), 300 * TAO) for i in (1, 7)])
    d = StandardOverlay().review(proposal, kit.risk_ctx(t))
    fe = {f.key: f for f in d.targets.forced}
    assert fe[kit.key(1)].urgency is Urgency.EMERGENCY and fe[kit.key(1)].rule == "prune_target"   # beats emission_off
    assert fe[kit.key(7)].urgency is Urgency.URGENT and fe[kit.key(7)].rule == "emission_off"
    assert fe[gone].rule == "dissolved"
    assert kit.key(9) not in fe
    for k in (kit.key(1), kit.key(7)):
        item = d.targets.get(k)
        assert item is not None and item.value_rao == 0 and item.urgency is fe[k].urgency
    assert any(a.rule == "exit.prune_target" and a.action == "FORCE_EXIT" for a in d.actions)


def test_vcap_limits_increases_only(kit: ModuleType) -> None:
    snap = kit.big_market()
    k = kit.key(25)
    pf = kit.portfolio(cash=100 * TAO)                             # NU_MAX * NAV = 15 TAO
    t = kit.tick(snap, portfolio=pf)
    d = StandardOverlay().review(kit.target_book([kit.target(k, kit.vhk(25), 40 * TAO)]), kit.risk_ctx(t))
    assert d.targets.get(k).value_rao == 15 * TAO                  # type: ignore[union-attr]
    assert any(a.rule == "liquidity.vcap" for a in d.actions)


def test_transfers_kept_only_on_untouched_keys(kit: ModuleType) -> None:
    snap = kit.big_market()
    a, b_ = kit.key(20), kit.key(4)
    pf = kit.portfolio(positions=[kit.position(a, kit.vhk(20), 100 * TAO), kit.position(b_, kit.vhk(4), 100 * TAO)])
    t = kit.tick(snap, portfolio=pf)
    hv = {k: int(h.value) for k, h in holdings(t).items()}
    x = [SleeveXfer(a, StrategyId("carry"), StrategyId("mom"), Decimal(TAO), Rao(1), PriceRao(1)),
         SleeveXfer(b_, StrategyId("carry"), StrategyId("mom"), Decimal(TAO), Rao(1), PriceRao(1))]
    proposal = kit.target_book([kit.target(a, kit.vhk(20), hv[a]), kit.target(b_, kit.vhk(4), hv[b_] + 10 * TAO)],
                               transfers=tuple(x))
    d = StandardOverlay().review(proposal, kit.risk_ctx(t))
    assert [y.key for y in d.targets.transfers] == [a]
    assert any(z.rule == "overlay.netting_dropped" for z in d.actions)


def test_burn_in_halves_targets(kit: ModuleType) -> None:
    d, k = _entry(kit, burn_in_until=B + 10)
    assert d.targets.get(k).value_rao == 5 * TAO                   # type: ignore[union-attr]
    assert any(a.rule == "allocator.burn_in" for a in d.actions)
    d2, _ = _entry(kit, burn_in_until=B - 1)
    assert d2.targets.get(k).value_rao == 10 * TAO                 # type: ignore[union-attr]


def test_runtime_prune_target_mismatch_halts_entries(kit: ModuleType) -> None:
    snap = kit.big_market()
    snap = kit.snapshot(subnets=snap.subnets, subnet_limit=30, n_nonroot_networks=30, runtime_prune_target=NetUid(9))
    d, k = _entry(kit, snap=snap)
    assert d.targets.halt_entries and d.targets.get(k).value_rao == 0        # type: ignore[union-attr]
    assert any(a.rule == "prune.runtime_mismatch" for a in d.actions)


class _Provider:
    def __init__(self, shift: int = 0) -> None:
        r = [Decimal(x) for x in ("1.75", "1.462", "1.253", "1.132", "1.045", "0.958", "0.872", "0.767", "0.266")]
        f = [Decimal(x) for x in ("0.0625", "0.09", "0.19", "0.31", "0.50", "0.72", "0.84", "0.94", "1.0")]
        self.model = hazard_from_table(r, f, 32, n0=4, rate_limit_blocks=14_400, i_eff_blocks=57_600,
                                       prior_scale_blocks=43_200)
        self.shift = shift

    def asof(self, block: Block) -> Calibration:
        return sealed(Calibration(asof=Block(block + self.shift), hazard=self.model, kappa_p=Decimal(4),
                                  r_default=Decimal("0.35"), r_cap_formula=True, tier_b_jump_p_day=Decimal("0.03"),
                                  tier_b_jump_size=DEC.ln(Decimal("0.5")), phi=SellLoadParams(), digest=""))


def test_tier_b_and_mc_entry_rule_through_the_overlay(kit: ModuleType) -> None:
    cfg = RiskCfg(tier_b_enabled=True, mc_paths=300)
    subs = [kit.subnet(1, price=Decimal("0.010")), kit.subnet(2, price=Decimal("0.0104"))]
    subs += [kit.subnet(i, price=Decimal("0.01") * i) for i in range(3, 11)]
    snap = kit.snapshot(subnets=subs, subnet_limit=10, n_nonroot_networks=10, last_reg_block=Block(B - 55_000))
    pf = kit.portfolio(positions=[kit.position(kit.key(2), kit.vhk(2), 10 * TAO)])
    t = kit.tick(snap, portfolio=pf)
    ov = StandardOverlay(_Provider(), seed=7)
    proposal = kit.target_book([kit.target(kit.key(10), kit.vhk(10), 5 * TAO)])
    d = ov.review(proposal, kit.risk_ctx(t, cfg=cfg))
    p = [a for a in d.actions if a.rule == "prune.p_prune"]
    assert {a.detail.split(";")[0] for a in p} == {"horizon=7200", "horizon=50400"}
    fe = {f.key: f for f in d.targets.forced}
    assert fe[kit.key(2)].rule in ("prune_A", "prune_B", "prune_backstop")
    assert d == ov.review(proposal, kit.risk_ctx(t, cfg=cfg))     # deterministic (seeded MC, memo)
    with pytest.raises(ValueError):
        StandardOverlay().review(proposal, kit.risk_ctx(t, cfg=cfg))   # tier B needs a calibration provider
    with pytest.raises(LookaheadError):
        StandardOverlay(_Provider(shift=1)).review(proposal, kit.risk_ctx(t))


def test_expected_loss_monitor_rows(kit: ModuleType) -> None:
    snap = kit.snapshot()
    pf = kit.portfolio(positions=[kit.position(kit.key(8), kit.vhk(8), 100 * TAO)])
    d = StandardOverlay(_Provider()).review(kit.target_book([]), kit.risk_ctx(kit.tick(snap, portfolio=pf)))
    rows = [a for a in d.actions if a.rule == "prune.expected_loss"]
    assert len(rows) == 1 and rows[0].key == kit.key(8) and rows[0].action == "MONITOR"


@settings(max_examples=40)
@given(st.lists(st.tuples(st.integers(1, 30), st.integers(0, 400), st.integers(0, 300)), min_size=1, max_size=8,
                unique_by=lambda x: x[0]),
       st.sampled_from([Mode.NORMAL, Mode.CAUTION, Mode.EXITS_ONLY, Mode.FROZEN]),
       st.booleans())
def test_review_is_monotone(kit: ModuleType, rows: list[tuple[int, int, int]], floor: Mode, cool: bool) -> None:
    snap = kit.big_market()
    pos = [kit.position(kit.key(n), kit.vhk(n), held * 25 * TAO) for n, _, held in rows if held > 0]
    pf = kit.portfolio(cash=500 * TAO, positions=pos)
    bv = kit.book_view(cooldowns=tuple((kit.key(n), "x", Block(B + 1)) for n, _, _ in rows[:1]) if cool else ())
    t = kit.tick(snap, portfolio=pf, bv=bv, mode=floor)
    proposal = kit.target_book([kit.target(kit.key(n), kit.vhk(n), v * TAO) for n, v, _ in rows])
    d = StandardOverlay().review(proposal, kit.risk_ctx(t), ModeSignals())
    assert d.mode >= floor
    assert [i.key for i in d.targets.items] == [i.key for i in proposal.items]
    for new, old in zip(d.targets.items, proposal.items, strict=True):
        assert new.value_rao <= old.value_rao
    for f in d.targets.forced:
        item = d.targets.get(f.key)
        assert item is None or item.value_rao == 0 or f.trim_to_rao is not None
    if d.mode >= Mode.CAUTION:
        cur = {k: int(h.value) for k, h in holdings(t).items()}
        assert all(i.value_rao <= cur.get(i.key, 0) for i in d.targets.items)


def test_owner_event_vetoes_and_starts_a_cooldown(kit: ModuleType) -> None:
    ev = [kit.event(ChainEventKind.OWNER_CHANGED, key=kit.key(20))]
    d, k = _entry(kit, events=ev)
    assert d.targets.get(k).value_rao == 0                         # type: ignore[union-attr]
    cd = [a for a in d.actions if a.rule == "owner_changed"]
    assert cd and f"cooldown_until={B + 7_200}" in cd[0].detail     # folded by the reducer into BookView.cooldowns


def test_stale_data_and_missing_features_veto(kit: ModuleType) -> None:
    snap = kit.big_market()
    fr = kit.frame(snap)
    fr = replace(fr, feats={k: v for k, v in fr.feats.items() if k != kit.key(20)})
    d, _ = _entry(kit, snap=snap, fr=fr)
    assert any(a.rule == "cooldown.stale_data" for a in d.actions)
    subs = [s if s.key.netuid != 20 else replace(s, quality=Quality.CHAIN_STALL_GAP) for s in snap.subnets]
    d2, _ = _entry(kit, snap=kit.snapshot(subnets=subs, subnet_limit=30, n_nonroot_networks=30))
    assert any(a.rule == "cooldown.stale_data" for a in d2.actions)


def test_lcw_exceptions(kit: ModuleType) -> None:
    snap = kit.big_market()
    subs = [s if s.key.netuid != 20 else kit.subnet(20, tao=100_000 * TAO, first_emission_block=Block(B - 5_000),
                                                    reg_at=B - 10_000, emission_enabled=False)
            for s in snap.subnets]
    young = kit.snapshot(subnets=subs, subnet_limit=30, n_nonroot_networks=30)
    k = kit.key(20, B - 10_000)
    fr = kit.frame(young, feat_over={20: {"launch_flags": frozenset({"EMA_WARMING", "YOUNG_IMMUNE", "EMISSION_OFF"})}})
    t = kit.tick(young, fr=fr)
    for sid, paper_ok in (("lcw", True), ("carry", False)):
        prop = kit.target_book([kit.target(k, kit.vhk(20), 2 * TAO, sid=sid)])
        d = StandardOverlay(run_mode=RunMode.PAPER).review(prop, kit.risk_ctx(t))
        assert (d.targets.get(k).value_rao > 0) is paper_ok, [a.rule for a in d.actions]   # type: ignore[union-attr]
    live = StandardOverlay(run_mode=RunMode.LIVE).review(kit.target_book([kit.target(k, kit.vhk(20), 2 * TAO, sid="lcw")]),
                                                         kit.risk_ctx(t))
    assert live.targets.get(k).value_rao == 0                      # type: ignore[union-attr]   # LCW-paper only


def test_monitor_rows(kit: ModuleType) -> None:
    snap = kit.big_market()
    off = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(9), flag=False)]
    pf = kit.portfolio(positions=[kit.position(kit.key(20), kit.vhk(20), 100 * TAO)])
    fr = kit.frame(snap, feat_over={20: {"gate_keep": 0.05, "owner_liquid_frac": 0.2}})
    t = kit.tick(snap, fr=fr, portfolio=pf, events=off)
    d = StandardOverlay().review(kit.target_book([]), kit.risk_ctx(t))
    rules = {a.rule: a for a in d.actions}
    assert rules["regime.throttle"].action == "MONITOR"             # first tick of a day (prev is None)
    assert rules["emission.prune_hazard"].key == kit.key(9)
    mon = rules["liquidity.monitor"]
    assert "m_gate_ppm=250000" in mon.detail and "m_owner_ppm=500000" in mon.detail and "gate_active=0" in mon.detail
    same_day = kit.tick(snap, fr=fr, portfolio=pf, prev=kit.snapshot(B - 60))
    assert not any(a.rule == "regime.throttle" for a in StandardOverlay().review(kit.target_book([]),
                                                                                 kit.risk_ctx(same_day)).actions)


def test_reg_clock_hot_tightens_the_prune_floor(kit: ModuleType) -> None:
    snap = kit.big_market()
    hot = frozenset({"REG_CLOCK_HOT"})
    for netuid, flags, ok in ((6, frozenset(), True), (6, hot, False), (8, hot, True), (5, frozenset(), False)):
        fr = kit.frame(snap, feat_over={netuid: {"launch_flags": flags}})
        d, k = _entry(kit, snap=snap, fr=fr, netuid=netuid)
        assert (d.targets.get(k).value_rao > 0) is ok, (netuid, flags)   # type: ignore[union-attr]
