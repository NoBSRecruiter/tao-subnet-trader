"""protocol.derive: derive_events (section 4.3 table; netuid reuse, the removal block, idempotence, thresholds,
FULL-only fields) and track_hotkeys (owner hotkey, top-N, take-0 earners, held, sticky)."""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.state import HotkeyIdx, ReadPlan
from taotrader.core.units import AlphaRao, Block, Coldkey, Hotkey, NetUid, Rao, SubnetKey
from taotrader.protocol.derive import derive_events, owner_position_delta, track_hotkeys
from taotrader.protocol.prune import ladder

K = ChainEventKind
TAO = 10**9
BLK = 9_000_000


def _kinds(evs: tuple[ChainEvent, ...]) -> list[str]:
    return [e.kind.value for e in evs]


def _base(make_subnet, **kw):
    """Mature, non-immune subnets 1 (the prune target) and 2, with flow history and FULL-only fields set."""
    s1 = make_subnet(1, reg_at=5_000_000, moving_price=Decimal("0.001"), tao_flow_cum=0, owner_coldkey=Coldkey("0x" + "a1" * 32),
                     owner_hotkey=Hotkey("0x" + "b1" * 32), owner_cut_autolock=False, consensus_mode=0)
    s2 = make_subnet(2, reg_at=5_100_000, moving_price=Decimal("0.003"), tao_flow_cum=0)
    return s1, s2


# ------------------------------------------------------------------------------------------------- basics
def test_first_snapshot_and_identical_snapshots_give_nothing(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    snap = make_snapshot(BLK, [s1, s2])
    assert derive_events(None, snap) == ()
    assert derive_events(snap, snap) == ()
    again = make_snapshot(BLK, [s1, s2])
    assert derive_events(snap, again) == ()
    with pytest.raises(ValueError):
        derive_events(make_snapshot(BLK + 1, [s1, s2]), snap)


def test_idempotent_on_the_golden_full_snapshot(gsnap, dec) -> None:
    snap = dec.build_snapshot(gsnap("sn92_9240388", 0), with_hotkeys=True)
    s92 = snap.by_netuid(92)
    assert s92 is not None and len(s92.hotkeys) >= 50 and s92.owner_hotkey is not None
    assert derive_events(snap, snap) == ()
    assert derive_events(snap, dec.build_snapshot(gsnap("sn92_9240388", 0), with_hotkeys=True)) == ()


def test_netuid_reuse_gives_deregistered_plus_registered(make_subnet, make_snapshot, make_pool) -> None:
    """A prune followed by re-registration of the same netuid: DEREGISTERED(old) + REGISTERED(new), never a price
    jump or toggle on the new generation."""
    s1, s2 = _base(make_subnet)
    old7 = make_subnet(7, reg_at=6_000_000, moving_price=Decimal("0.002"))
    new7 = make_subnet(7, reg_at=BLK, moving_price=Decimal(0), first_emission_block=None, subtoken_enabled=False,
                       emission_enabled=False, pool=make_pool(800 * TAO, 3_000_000 * TAO), tao_flow_cum=None)
    prev = make_snapshot(BLK - 1, [s1, s2, old7])
    cur = make_snapshot(BLK, [s1, s2, new7])
    evs = derive_events(prev, cur)
    assert evs == (ChainEvent(K.DEREGISTERED, Block(BLK), key=old7.key), ChainEvent(K.REGISTERED, Block(BLK), key=new7.key))
    assert cur.get(old7.key) is None and cur.by_netuid(7) is not None


def test_removal_block_fires_deregistered_while_registered_at_persists(gsnap, dec) -> None:
    """At the removal block P, NetworksAdded is false while NetworkRegisteredAt and the pool keys are still set (they
    are cleared 17-25 blocks later): the generation leaves the snapshot and DEREGISTERED fires at P."""
    gs = gsnap("sn92_9240388", 0)
    raw = copy.deepcopy(gs.raw)
    raw["block"] = gs.block + 1
    raw["storage_by_netuid"]["SubtensorModule.NetworksAdded"]["values"][92] = "0x00"
    removed = type(gs)(raw)
    assert removed.get("SubtensorModule.NetworkRegisteredAt", 92) is not None          # still present at P
    assert removed.get("SubtensorModule.SubnetTAO", 92) is not None
    prev = dec.build_snapshot(gs)
    cur = dec.build_snapshot(removed)
    assert prev.by_netuid(92) is not None and cur.by_netuid(92) is None
    evs = derive_events(prev, cur)
    s92 = prev.by_netuid(92)
    assert s92 is not None
    assert ChainEvent(K.DEREGISTERED, Block(cur.block), key=s92.key) in evs
    assert [e for e in evs if e.kind is K.REGISTERED] == []
    tgt = [e for e in evs if e.kind is K.PRUNE_TARGET_CHANGED]                         # the ladder moves on to SN47
    assert len(tgt) == 1 and tgt[0].key is not None and int(tgt[0].key.netuid) == 47


# ------------------------------------------------------------------------------------------------- per-subnet (hot path)
def test_hot_path_toggles_fire_on_head_snapshots(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    pending = replace(s2, first_emission_block=None)
    prev = make_snapshot(BLK, [s1, pending])
    s2b = replace(s2, emission_enabled=False, reg_allowed=False, last_epoch_block=Block(BLK + 1),
                  pool=replace(s2.pool, fee_rate=40))
    cur = make_snapshot(BLK + 1, [s1, s2b], plan=ReadPlan.HEAD)
    evs = derive_events(prev, cur)
    assert _kinds(evs) == ["emission_toggled", "epoch_drain", "param_changed", "reg_allowed_toggled", "start_called"]
    by = {e.kind: e for e in evs}
    assert by[K.EMISSION_TOGGLED].flag is False and by[K.REG_ALLOWED_TOGGLED].flag is False
    assert by[K.EPOCH_DRAIN].new == str(BLK + 1)
    assert (by[K.PARAM_CHANGED].name, by[K.PARAM_CHANGED].old, by[K.PARAM_CHANGED].new) == ("FeeRate", "33", "40")
    assert all(e.key == s2.key for e in evs)


@pytest.mark.parametrize(("delta_rao", "fires"), [(12 * TAO, True), (-12 * TAO, True), (12 * TAO - 1, False),
                                                  (-(12 * TAO - 1), False)])
def test_large_flow_threshold(make_subnet, make_snapshot, make_pool, delta_rao: int, fires: bool) -> None:
    """|dSubnetTaoFlow| >= 2% of SubnetTAO (600 TAO -> 12 TAO) between snapshots of one generation."""
    s1, s2 = _base(make_subnet)
    s2 = replace(s2, pool=make_pool(600 * TAO, 200_000 * TAO), tao_flow_cum=5 * TAO)
    prev = make_snapshot(BLK, [s1, s2])
    cur = make_snapshot(BLK + 60, [s1, replace(s2, tao_flow_cum=5 * TAO + delta_rao)], plan=ReadPlan.HEAD)
    flows = [e for e in derive_events(prev, cur) if e.kind is K.LARGE_FLOW]
    if not fires:
        assert flows == []
        return
    assert len(flows) == 1
    assert flows[0].amount == delta_rao and flows[0].frac_ppm == (20_000 if delta_rao > 0 else -20_000)
    assert flows[0].key == s2.key


def test_large_flow_needs_valid_flow(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    early_prev = make_snapshot(8_466_530, [s1, s2])                                   # SubnetTaoFlow valid from 8,466,531
    early_cur = make_snapshot(8_466_590, [s1, replace(s2, tao_flow_cum=100 * TAO)])
    assert [e for e in derive_events(early_prev, early_cur) if e.kind is K.LARGE_FLOW] == []
    prev = make_snapshot(BLK, [s1, replace(s2, tao_flow_cum=None)])
    cur = make_snapshot(BLK + 60, [s1, replace(s2, tao_flow_cum=100 * TAO)])
    assert [e for e in derive_events(prev, cur) if e.kind is K.LARGE_FLOW] == []


# ------------------------------------------------------------------------------------------------- FULL-only fields
def test_full_only_fields_fire_only_on_full_snapshots(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    prev = make_snapshot(BLK, [s1, s2])
    changed = replace(s1, owner_coldkey=Coldkey("0x" + "a2" * 32), owner_hotkey=Hotkey("0x" + "b2" * 32), owner_cut_autolock=True,
                      consensus_mode=1, tempo=361, ema_halving_blocks=100_800)
    head = derive_events(prev, make_snapshot(BLK + 1, [changed, s2], plan=ReadPlan.HEAD))
    assert head == ()
    full = derive_events(prev, make_snapshot(BLK + 1, [changed, s2], plan=ReadPlan.FULL))
    names = sorted((e.kind.value, e.name or "") for e in full)
    assert names == [("autolock_toggled", ""), ("owner_changed", "SubnetOwner"), ("owner_changed", "SubnetOwnerHotkey"),
                     ("param_changed", "EMAPriceHalvingBlocks"), ("param_changed", "SubnetEpochConsensus"),
                     ("param_changed", "Tempo")]
    mode = next(e for e in full if e.name == "SubnetEpochConsensus")
    assert (mode.key, mode.old, mode.new) == (s1.key, "0", "1")                       # Null consensus switch: per subnet
    assert [e.flag for e in full if e.kind is K.AUTOLOCK_TOGGLED] == [True]


def test_full_only_fields_need_both_values(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    prev = make_snapshot(BLK, [replace(s1, owner_coldkey=None, consensus_mode=None, owner_cut_autolock=None), s2])
    cur = make_snapshot(BLK + 60, [s1, s2])
    assert derive_events(prev, cur) == ()
    prev2 = make_snapshot(BLK, [s1, s2])
    cur2 = make_snapshot(BLK + 60, [replace(s1, consensus_mode=None, owner_hotkey=None), s2])
    assert derive_events(prev2, cur2) == ()


def test_hotkey_panel_events(make_subnet, make_snapshot, hk) -> None:
    s1, s2 = _base(make_subnet)
    h1 = HotkeyIdx(hotkey=hk(1), total_alpha=AlphaRao(10 * TAO), total_shares=Decimal(10 * TAO), take_u16=0, earns=True)
    h2 = HotkeyIdx(hotkey=hk(2), total_alpha=AlphaRao(20 * TAO), total_shares=Decimal(20 * TAO), take_u16=11_796, earns=False)
    prev = make_snapshot(BLK, [replace(s2, hotkeys=(h1, h2)), s1])
    h1b = replace(h1, take_u16=6_554)
    h2b = replace(h2, earns=True)
    h3 = HotkeyIdx(hotkey=hk(3), total_alpha=AlphaRao(TAO), total_shares=Decimal(TAO), take_u16=1, earns=True)   # newly tracked
    cur = make_snapshot(BLK + 60, [replace(s2, hotkeys=(h1b, h2b, h3)), s1])
    evs = derive_events(prev, cur)
    assert [(e.kind, e.hotkey) for e in evs] == [(K.DIVIDEND_MEMBERSHIP, hk(2)), (K.TAKE_CHANGED, hk(1))]
    take = evs[1]
    assert (take.name, take.old, take.new) == ("Delegates", "0", "6554")
    assert evs[0].flag is True
    assert derive_events(prev, make_snapshot(BLK + 1, [replace(s2, hotkeys=(h1b, h2b)), s1], plan=ReadPlan.HEAD)) == ()


# ------------------------------------------------------------------------------------------------- owner position
def _owner_pair(make_subnet, make_pool, hk, sold_alpha: int, *, drain: bool = False):
    owner_hk = hk(9)
    idx0 = HotkeyIdx(hotkey=owner_hk, total_alpha=AlphaRao(200_000 * TAO), total_shares=Decimal(150_000 * TAO),
                     take_u16=11_796, earns=True, last_dividend=AlphaRao(100 * TAO))
    s = make_subnet(3, reg_at=5_000_000, moving_price=Decimal("0.004"), pool=make_pool(600 * TAO, 600_000 * TAO),
                    owner_coldkey=Coldkey("0x" + "c3" * 32), owner_hotkey=owner_hk, owner_alpha=AlphaRao(100_000 * TAO),
                    hotkeys=(idx0,), alpha_out_emission=AlphaRao(TAO), last_epoch_block=Block(BLK - 100))
    accrual = 60 * TAO * 11_796 // 65_535                                               # c_o * 1 alpha/block * 60 blocks
    idx1, credit, last_epoch = idx0, 0, s.last_epoch_block
    if drain:
        idx1 = replace(idx0, total_alpha=AlphaRao(200_100 * TAO), last_dividend=AlphaRao(120 * TAO))
        credit = 120 * TAO * 11_796 // (65_535 - 11_796)                               # take credited as new owner shares
        last_epoch = Block(BLK + 30)
    grown = 100_000 * TAO * idx1.total_alpha // idx0.total_alpha                       # same shares, higher index
    cur = replace(s, owner_alpha=AlphaRao(grown + accrual + credit - sold_alpha), hotkeys=(idx1,), last_epoch_block=last_epoch)
    return s, cur


@pytest.mark.parametrize(("sold_alpha", "fires"), [(2_000 * TAO, True), (1_000 * TAO, False), (0, False),
                                                   (-2_000 * TAO, True)])
def test_owner_position_changed(make_subnet, make_snapshot, make_pool, hk, sold_alpha: int, fires: bool) -> None:
    """Owner sold estimate (section 3.6: A0*I1/I0 + accrual - A1) >= 0.25% of pool alpha (600,000 -> 1,500 alpha);
    the amount is signed (negative = sold)."""
    s1, _ = _base(make_subnet)
    prev_s, cur_s = _owner_pair(make_subnet, make_pool, hk, sold_alpha)
    evs = derive_events(make_snapshot(BLK, [s1, prev_s]), make_snapshot(BLK + 60, [s1, cur_s]))
    owner = [e for e in evs if e.kind is K.OWNER_POSITION_CHANGED]
    if not fires:
        assert owner == []
        return
    assert len(owner) == 1 and owner[0].key == prev_s.key and owner[0].hotkey == hk(9)
    assert owner[0].amount is not None and abs(owner[0].amount + sold_alpha) <= 2


def test_owner_position_accounts_for_index_and_take_credit(make_subnet, make_pool, make_globals, hk) -> None:
    glob = make_globals()
    prev_s, cur_s = _owner_pair(make_subnet, make_pool, hk, 0, drain=True)
    delta = owner_position_delta(prev_s, cur_s, glob, Block(BLK), Block(BLK + 60))
    assert delta is not None and abs(delta) <= 2                                       # drain + cut + take credit explained
    # a drain seen only through the dividend value (HEAD-carried panel; LastEpochBlock already current at prev)
    carried_prev = replace(prev_s, last_epoch_block=cur_s.last_epoch_block)
    delta2 = owner_position_delta(carried_prev, cur_s, glob, Block(BLK), Block(BLK + 60))
    assert delta2 is not None and abs(delta2) <= 2
    assert owner_position_delta(prev_s, replace(cur_s, owner_alpha=None), glob, Block(BLK), Block(BLK + 60)) is None
    other = replace(cur_s, owner_hotkey=hk(8))
    assert owner_position_delta(prev_s, other, glob, Block(BLK), Block(BLK + 60)) is None   # OWNER_CHANGED covers it


# ------------------------------------------------------------------------------------------------- globals
def test_registration_window_immunity_and_target(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    young = make_subnet(4, reg_at=BLK + 30 - 864_000, moving_price=Decimal("0.0001"))   # leaves immunity at BLK + 30
    last = Block(BLK - 14_400 + 10)
    prev = make_snapshot(BLK, [s1, s2, young], last_reg_block=last)
    cur = make_snapshot(BLK + 60, [s1, s2, young], last_reg_block=last)
    evs = derive_events(prev, cur)
    assert _kinds(evs) == ["immunity_expired", "prune_target_changed", "reg_window_opened"]
    tgt = evs[1]
    assert tgt.key == young.key and (tgt.old, tgt.new) == (f"1:{s1.key.reg_at}", f"4:{young.key.reg_at}")
    assert ladder(cur)[0] == young.key
    seen = derive_events(cur, make_snapshot(BLK + 120, [s1, s2, young], last_reg_block=Block(BLK + 100)))
    assert [e.kind for e in seen] == [K.REGISTRATION_SEEN]
    assert (seen[0].old, seen[0].new) == (str(BLK - 14_400 + 10), str(BLK + 100))


def test_global_params_spec_gate_bar_and_safe_mode(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    prev = make_snapshot(BLK, [s1, s2])
    cur = make_snapshot(BLK + 1, [s1, s2], plan=ReadPlan.HEAD, spec_version=476, tx_version=2, gate_bar=Decimal("0.009"),
                        tao_weight=Decimal("0.2"), owner_cut_u16=10_000, gate_exponent=4, network_rate_limit=7_200,
                        safe_mode_until=Block(BLK + 7_200))
    evs = derive_events(prev, cur)
    got = [(e.kind.value, e.name) for e in evs]
    assert got == [("gate_bar_updated", None), ("param_changed", "EmissionGateExponent"),
                   ("param_changed", "NetworkRateLimit"), ("param_changed", "SubnetOwnerCut"),
                   ("param_changed", "TaoWeight"), ("safe_mode", None), ("spec_changed", "spec_version"),
                   ("spec_changed", "transaction_version")]
    tw = next(e for e in evs if e.name == "TaoWeight")
    assert (tw.old, tw.new, tw.key) == ("0.18", "0.2", None)
    assert [e.flag for e in evs if e.kind is K.SAFE_MODE] == [True]
    ended = make_snapshot(BLK + 7_201, [s1, s2], spec_version=476, tx_version=2, gate_bar=Decimal("0.009"),
                          tao_weight=Decimal("0.2"), owner_cut_u16=10_000, gate_exponent=4, network_rate_limit=7_200,
                          safe_mode_until=Block(BLK + 7_200))
    assert [(e.kind, e.flag) for e in derive_events(cur, ended)] == [(K.SAFE_MODE, False)]   # EnteredUntil passed


def test_events_are_sorted_by_kind_then_key(make_subnet, make_snapshot) -> None:
    s1, s2 = _base(make_subnet)
    prev = make_snapshot(BLK, [s1, s2])
    cur = make_snapshot(BLK + 1, [replace(s1, emission_enabled=False, last_epoch_block=Block(BLK + 1)),
                                  replace(s2, emission_enabled=False, last_epoch_block=Block(BLK + 1))], gate_bar=Decimal("0.01"))
    evs = derive_events(prev, cur)
    keys = [(e.kind.value, -1 if e.key is None else int(e.key.netuid)) for e in evs]
    assert keys == sorted(keys)
    assert derive_events(prev, cur) == evs


# ------------------------------------------------------------------------------------------------- track_hotkeys
def _panel(hk, alphas: dict[int, int], takes: dict[int, int] | None = None) -> tuple[HotkeyIdx, ...]:
    takes = takes or {}
    return tuple(HotkeyIdx(hotkey=hk(i), total_alpha=AlphaRao(a * TAO), total_shares=Decimal(a * TAO),
                           take_u16=takes.get(i, 11_796), earns=True) for i, a in sorted(alphas.items()))


def test_track_hotkeys_owner_top_n_take0_held(make_subnet, make_snapshot, hk) -> None:
    alphas = {i: 1_000 * (10 - i) for i in range(1, 9)}                               # hk1 largest ... hk8 smallest
    s1 = make_subnet(1, reg_at=5_000_000, hotkeys=_panel(hk, alphas, {8: 0}), owner_hotkey=hk(20))
    s2 = make_subnet(2, reg_at=5_100_000, owner_hotkey=hk(21))
    snap = make_snapshot(BLK, [s1, s2])
    divs = {1: [str(hk(i)) for i in range(1, 9)], 2: []}
    gone = SubnetKey(NetUid(3), Block(4_000_000))
    out = track_hotkeys(snap, divs, held=[(s1.key, hk(30)), (gone, hk(31))])
    want = {(s1.key, hk(i)) for i in (1, 2, 3, 4, 5)} | {(s1.key, hk(8)), (s1.key, hk(20)), (s1.key, hk(30)),
                                                         (s2.key, hk(21))}
    assert set(out) == want
    assert list(out) == sorted(out)
    assert track_hotkeys(snap, divs, held=[(s1.key, hk(30)), (gone, hk(31))]) == out    # deterministic
    assert (s1.key, hk(6)) not in out                                                   # rank 6, take 18%
    top3 = track_hotkeys(snap, divs, held=[], top_n=3)
    assert {(s1.key, hk(i)) for i in (1, 2, 3)} <= set(top3) and (s1.key, hk(4)) not in top3


def test_track_hotkeys_is_sticky_until_the_generation_ends(make_subnet, make_snapshot, hk) -> None:
    alphas = {i: 1_000 * (10 - i) for i in range(1, 8)}
    s1 = make_subnet(1, reg_at=5_000_000, hotkeys=_panel(hk, alphas))
    snap = make_snapshot(BLK, [s1])
    divs = {1: [str(hk(i)) for i in range(1, 8)]}
    first = track_hotkeys(snap, divs, held=[])
    assert (s1.key, hk(5)) in first
    # hk5 drops out of the top 5 and out of the dividend set: still tracked (sticky)
    s1b = replace(s1, hotkeys=_panel(hk, {**alphas, 5: 1}))
    snap_b = make_snapshot(BLK + 60, [s1b])
    later = track_hotkeys(snap_b, {1: [str(hk(i)) for i in (1, 2, 3, 4, 6, 7)]}, held=[], prev_tracked=first)
    assert (s1.key, hk(5)) in later and (s1.key, hk(6)) in later
    # the generation ends (netuid reused): every pair of the old key is dropped
    reused = make_subnet(1, reg_at=BLK + 100, hotkeys=())
    after = track_hotkeys(make_snapshot(BLK + 120, [reused]), {1: []}, held=[(s1.key, hk(1))], prev_tracked=later)
    assert after == ()
    with_owner = track_hotkeys(make_snapshot(BLK + 120, [replace(reused, owner_hotkey=hk(40))]), {}, held=[])
    assert with_owner == ((reused.key, hk(40)),)


def test_track_hotkeys_on_the_golden_panel(gsnap, dec) -> None:
    """SN92 at 9,240,388: 52 dividend recipients; the owner hotkey is always tracked."""
    gs = gsnap("sn92_9240388", 0)
    glob = dec.build_globals(gs)
    s92 = dec.build_subnet(gs, 92, glob, with_hotkeys=True)
    snap = dec.build_snapshot(gs, netuids=[92], with_hotkeys=True)
    divs = {92: gs.dividend_hotkeys(92)}
    assert len(divs[92]) == 52
    out = track_hotkeys(snap, divs, held=[])
    assert s92.owner_hotkey is not None and (s92.key, s92.owner_hotkey) in out
    earners = sorted((h for h in s92.hotkeys if h.hotkey in set(divs[92])), key=lambda h: (-h.total_alpha, h.hotkey))
    assert {(s92.key, h.hotkey) for h in earners[:5]} <= set(out)
    assert {(s92.key, h.hotkey) for h in earners if h.take_u16 == 0} <= set(out)
    assert all(k == s92.key for k, _ in out)
    assert all(isinstance(h, str) for _, h in out) and Rao(0) == 0
