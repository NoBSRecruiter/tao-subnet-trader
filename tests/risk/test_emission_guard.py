"""risk.emission_guard: emission-off exits, the wave halt, entry bans, MinerBurned exits, LCW stops (DESIGN.md 3.4)."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

from taotrader.core.config import RiskCfg
from taotrader.core.events import ChainEventKind
from taotrader.core.orders import Urgency
from taotrader.core.units import Block
from taotrader.risk.emission_guard import (
    ban_cooldowns,
    burn_exits,
    emission_ban_until,
    emission_exits,
    entry_bans,
    lcw_exits,
    lcw_only_keys,
    wave_halt,
)

B = 9_240_388


def test_held_disabled_subnet_exits_urgently(kit: ModuleType) -> None:
    subs = [kit.subnet(i) for i in range(1, 6)]
    subs[3] = kit.subnet(4, emission_enabled=False)
    snap = kit.snapshot(subnets=subs)
    ex = emission_exits(snap, [kit.key(2), kit.key(4)], RiskCfg())
    assert [(e.key, e.urgency, e.rule, e.slip_ppm) for e in ex] == [(kit.key(4), Urgency.URGENT, "emission_off", 30_000)]


def test_wave_halt_needs_three_disables_in_one_block(kit: ModuleType) -> None:
    off = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(i), flag=False) for i in (1, 2, 3)]
    assert wave_halt(off, B) == (3, Block(B + 7_200))
    assert wave_halt(off[:2], B) is None
    on = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(i), flag=True) for i in (1, 2, 3)]
    assert wave_halt(on, B) is None
    stale = [kit.event(ChainEventKind.EMISSION_TOGGLED, B - 1, key=kit.key(i), flag=False) for i in (1, 2, 3)]
    assert wave_halt(stale, B) is None


def test_ban_windows(kit: ModuleType) -> None:
    cfg = RiskCfg()
    evs = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(1), flag=False),
           kit.event(ChainEventKind.EMISSION_TOGGLED, key=kit.key(2), flag=True)]
    assert ban_cooldowns(evs, B, cfg) == [(kit.key(1), "emission_off", Block(B + 100_800)),
                                          (kit.key(2), "emission_reenable", Block(B + 360))]
    bv = kit.book_view(cooldowns=((kit.key(3), "emission_off", Block(B + 10)), (kit.key(3), "emission_reenable", Block(B + 5)),
                                  (kit.key(4), "owner_changed", Block(B + 99))))
    bans = emission_ban_until(bv, evs, B, cfg)
    assert bans == {kit.key(1): B + 100_800, kit.key(2): B + 360, kit.key(3): B + 10}
    snap = kit.snapshot()
    v = entry_bans(snap, [kit.key(i) for i in (1, 2, 3, 4, 5)], bv, evs, cfg)
    assert sorted(v) == [kit.key(1), kit.key(2), kit.key(3)]
    assert v[kit.key(3)] == ("emission.ban", f"until={B + 10}")
    expired = kit.book_view(cooldowns=((kit.key(3), "emission_off", Block(B - 1)),))
    assert entry_bans(snap, [kit.key(3)], expired, [], cfg) == {}
    disabled = kit.snapshot(subnets=[kit.subnet(5, emission_enabled=False)])
    assert entry_bans(disabled, [kit.key(5)], kit.book_view(), [], cfg)[kit.key(5)][0] == "emission.disabled"


def test_burn_exit_needs_two_consecutive_epochs(kit: ModuleType) -> None:
    cfg = RiskCfg()
    k = kit.key(3)
    cur = kit.snapshot(subnets=[kit.subnet(3, miner_burned=Decimal("0.95"), last_epoch_block=Block(B - 100))])
    old_hi = kit.snapshot(B - 200, subnets=[kit.subnet(3, miner_burned=Decimal("0.92"), last_epoch_block=Block(B - 460))])
    old_lo = kit.snapshot(B - 200, subnets=[kit.subnet(3, miner_burned=Decimal("0.50"), last_epoch_block=Block(B - 460))])
    ex = burn_exits(cur, None, kit.MemStore([old_hi, cur], clock=B), [k], cfg)
    assert [(e.rule, e.urgency) for e in ex] == [("burn", Urgency.NORMAL)]
    assert burn_exits(cur, None, kit.MemStore([old_lo, cur], clock=B), [k], cfg) == []
    assert burn_exits(cur, old_hi, None, [k], cfg) != []                    # previous tick as the fallback
    same_epoch = kit.snapshot(B - 50, subnets=[kit.subnet(3, miner_burned=Decimal("0.95"), last_epoch_block=Block(B - 100))])
    assert burn_exits(cur, same_epoch, None, [k], cfg) == []                 # one epoch is not enough
    low_now = kit.snapshot(subnets=[kit.subnet(3, miner_burned=Decimal("0.89"))])
    assert burn_exits(low_now, old_hi, None, [k], cfg) == []


def test_lcw_only_positions_stop(kit: ModuleType) -> None:
    cfg = RiskCfg()
    young_reg = B - 200_000
    late = B - (864_000 - 144_000)
    subs = [kit.subnet(1, reg_at=young_reg, emission_enabled=False, first_emission_block=Block(B - 151_200 + 1)),
            kit.subnet(2, reg_at=late),
            kit.subnet(3, reg_at=young_reg, emission_enabled=False, first_emission_block=Block(B - 100_000))]
    snap = kit.snapshot(subnets=subs)
    ex = lcw_exits(snap, [kit.key(1, young_reg), kit.key(2, late), kit.key(3, young_reg)], cfg)
    assert [(e.key.netuid, e.rule) for e in ex] == [(1, "launch_stop"), (2, "launch_stop")]
    assert lcw_only_keys({kit.key(1): ["lcw"], kit.key(2): ["lcw", "carry"], kit.key(3): ["lcw.paper"]}) == [kit.key(1), kit.key(3)]


def test_lcw_paper_positions_exit_only_on_a_disable_after_enabling(kit: ModuleType) -> None:
    cfg = RiskCfg()
    snap = kit.snapshot(subnets=[kit.subnet(4, emission_enabled=False)])
    k = kit.key(4)
    assert emission_exits(snap, [k], cfg, lcw_only=[k]) == []                   # never enabled yet: the 21-day rule
    ev = [kit.event(ChainEventKind.EMISSION_TOGGLED, key=k, flag=False)]
    assert [e.rule for e in emission_exits(snap, [k], cfg, lcw_only=[k], events=ev)] == ["emission_off"]
    bv = kit.book_view(cooldowns=((k, "emission_off", Block(B + 50_000)),))
    assert [e.rule for e in emission_exits(snap, [k], cfg, lcw_only=[k], book_view=bv)] == ["emission_off"]
    assert [e.rule for e in emission_exits(snap, [k], cfg)] == ["emission_off"]  # every other sleeve: state-based
