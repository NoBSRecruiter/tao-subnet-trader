"""risk.owner_guard: owner sales over 7,200 blocks, owner events, cooldowns and the MONITOR m_owner (DESIGN.md 3.6)."""
from __future__ import annotations

from types import ModuleType

from taotrader.core.config import RiskCfg
from taotrader.core.events import ChainEventKind
from taotrader.core.units import AlphaRao, Block, Coldkey, Hotkey
from taotrader.risk.liquidity import vcap_detail
from taotrader.risk.owner_guard import m_owner_ppm, owner_cooldown_active, owner_sold_alpha, owner_triggers

B = 9_240_360            # on the 60-block grid
OWNER_HK = Hotkey("0x" + "ab" * 32)
OWNER_CK = Coldkey("0x" + "cd" * 32)


def _owned(kit: ModuleType, owner_alpha: int, block: int):
    s = kit.subnet(7, hotkeys=[kit.hidx(OWNER_HK, 10**15, earns=False)], owner_hotkey=OWNER_HK, owner_coldkey=OWNER_CK,
                   owner_alpha=AlphaRao(owner_alpha), owner_cut_enabled=False, alpha_out_emission=AlphaRao(0))
    return kit.snapshot(block, subnets=[s])


def _history(kit: ModuleType, alphas: list[int]):
    """FULL snapshots every 60 blocks ending at B with the given owner alpha path (oldest first)."""
    n = len(alphas)
    return [_owned(kit, a, B - 60 * (n - 1 - i)) for i, a in enumerate(alphas)]


def test_owner_sold_over_the_window(kit: ModuleType) -> None:
    hist = _history(kit, [10**15, 10**15, 9 * 10**14, 9 * 10**14, 95 * 10**13])
    store = kit.MemStore(hist, clock=B)
    sold = owner_sold_alpha(store, hist[-1], kit.key(7))
    assert sold == 10**14 - 5 * 10**13 == 5 * 10**13                 # net: -1e14 then +5e13
    assert owner_sold_alpha(None, hist[-1], kit.key(7)) is None
    assert owner_sold_alpha(kit.MemStore([hist[-1]], clock=B), hist[-1], kit.key(7)) is None


def test_sale_trigger_at_two_percent_of_subnet_tao(kit: ModuleType) -> None:
    cfg = RiskCfg()
    # pool: 1,000 TAO at 0.014 TAO/alpha -> 2% of T = 20 TAO = 1,428.6 alpha
    big = _history(kit, [10**15, 10**15 - 1_500 * 10**9])
    small = _history(kit, [10**15, 10**15 - 1_400 * 10**9])
    for hist, fires in ((big, True), (small, False)):
        raw = hist[-1]
        trig = owner_triggers(raw, kit.MemStore(hist, clock=B), [], {}, [kit.key(7)], kit.book_view(), cfg)
        assert bool(trig) is fires
        if fires:
            assert trig[0].rule == "owner.sold" and trig[0].until == B + 7_200
            assert f"cooldown_until={B + 7_200}" in trig[0].detail and "source=store" in trig[0].detail
    active = kit.book_view(cooldowns=((kit.key(7), "owner.sold", Block(B + 100)),))
    assert owner_triggers(big[-1], kit.MemStore(big, clock=B), [], {}, [kit.key(7)], active, cfg) == []


def test_sale_trigger_falls_back_to_the_6h_feature(kit: ModuleType) -> None:
    raw = _owned(kit, 10**15, B)
    s = raw.get(kit.key(7))
    assert s is not None
    feats = {kit.key(7): kit.feat(s, owner_sold_6h_frac=0.03)}           # 3% of pool alpha at spot ~ 3% of T
    trig = owner_triggers(raw, None, [], feats, [kit.key(7)], kit.book_view(), RiskCfg())
    assert trig and "source=feat_6h" in trig[0].detail


def test_event_triggers(kit: ModuleType) -> None:
    raw = kit.snapshot()
    evs = [kit.event(ChainEventKind.OWNER_CHANGED, key=kit.key(2)),
           kit.event(ChainEventKind.AUTOLOCK_TOGGLED, key=kit.key(3), flag=False),
           kit.event(ChainEventKind.AUTOLOCK_TOGGLED, key=kit.key(4), flag=True),
           kit.event(ChainEventKind.OWNER_CHANGED, key=kit.key(9))]
    trig = owner_triggers(raw, None, evs, {}, [kit.key(2), kit.key(3), kit.key(4)], kit.book_view(), RiskCfg())
    assert [(t.key.netuid, t.rule) for t in trig] == [(2, "owner_changed"), (3, "owner_autolock_off")]
    assert all(t.until == Block(B + 28 + 7_200) for t in trig)


def test_owner_cooldown_halves_v_cap_and_m_owner_is_monitor(kit: ModuleType) -> None:
    snap = kit.snapshot()
    s = snap.get(kit.key(5))
    assert s is not None
    cfg = RiskCfg()
    bv = kit.book_view(cooldowns=((kit.key(5), "owner_changed", Block(snap.block + 10)),))
    plain = vcap_detail(s, 10**15, kit.book_view(), int(snap.block), None, cfg, ())
    halved = vcap_detail(s, 10**15, bv, int(snap.block), None, cfg, ())
    assert owner_cooldown_active(bv, kit.key(5), int(snap.block))
    assert halved.cap == plain.cap // 2 and halved.m_owner_event_ppm == 500_000
    liquid = kit.feat(s, owner_liquid_frac=0.15)
    assert m_owner_ppm(liquid, cfg) == 500_000 and m_owner_ppm(kit.feat(s), cfg) == 1_000_000
    monitor = vcap_detail(s, 10**15, kit.book_view(), int(snap.block), liquid, cfg, ())
    active = vcap_detail(s, 10**15, kit.book_view(), int(snap.block), liquid, RiskCfg(owner_haircut_active=True), ())
    assert monitor.cap == plain.cap and active.cap == plain.cap // 2
