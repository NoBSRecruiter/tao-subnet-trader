"""features.gatekeeper (WP5 acceptance, offline): on the captured registration sequences (Q-1, Q, A-1, A for four real
registrations, tests/features/fixtures/gatekeeper_registrations.json) the Gatekeeper reproduces the Queued -> Added lag
(17-25 blocks) and netuid == victim, and the seed assertions hold; stride diffs, seed anomalies, states, flags,
recorder cadence and retention."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core.state import ChainSnapshot, SubnetState
from taotrader.core.units import AlphaRao, Block, NetUid, SubnetKey
from taotrader.features import gatekeeper as gk
from taotrader.features.engine import FeatureEngine
from taotrader.protocol.derive import derive_events


def feed(g: gk.Gatekeeper, snaps: tuple[ChainSnapshot, ...] | list[ChainSnapshot]) -> list[gk.LaunchRecord]:
    created: list[gk.LaunchRecord] = []
    prev = None
    for s in snaps:
        created.extend(g.observe(prev, s))
        prev = s
    return created


def new_key(reg: Any) -> SubnetKey:
    return SubnetKey(NetUid(reg.victim_netuid), Block(reg.added_block))


# ------------------------------------------------------------------------------------------------- acceptance
def test_fixture_covers_both_runtime_behaviours(registrations: Any) -> None:
    assert len(registrations) == 4
    for r in registrations:
        q1, q, _, a = r.snapshots
        assert int(a.block) - int(q.block) == r.lag
        if r.spec_version >= 467:                    # LastRateLimitedBlock and the lock are written at Q
            assert int(q.glob.last_reg_block) == r.queued_block and q.glob.last_lock_cost != q1.glob.last_lock_cost
        else:                                        # spec 443: written at NetworkAdded
            assert int(q.glob.last_reg_block) < r.queued_block and int(a.glob.last_reg_block) == r.added_block
    assert {r.spec_version >= 467 for r in registrations} == {True, False}


def test_per_block_sequences_reproduce_lag_and_victim(registrations: Any) -> None:
    p = gk.GatekeeperParams()
    for r in registrations:
        g = gk.Gatekeeper()
        created = feed(g, r.snapshots)
        assert [rec.key for rec in created] == [new_key(r)]
        rec = created[0]
        assert rec.queued_block == r.queued_block and rec.lag_blocks == r.lag
        assert 17 <= rec.lag_blocks <= 25 and rec.lag_ok(p)                  # type: ignore[operator]
        assert rec.victim is not None and int(rec.victim.netuid) == r.victim_netuid and rec.netuid_ok
        victim_state = r.snapshots[0].by_netuid(r.victim_netuid)
        assert victim_state is not None and rec.victim == victim_state.key
        assert rec.seed_checked and rec.seed_failures == () and not rec.seed_anomaly
        new_state = r.snapshots[3].get(new_key(r))
        assert new_state is not None and rec.lock_rao == new_state.pool.tao
        assert rec.seed_price_rel_err is not None and rec.seed_price_rel_err < Decimal("1e-12")
        assert g.pending() == ()


def test_stride_diff_spanning_queue_and_add(registrations: Any) -> None:
    """One diff Q-1 -> A: exact lag where the runtime records LastRateLimitedBlock at Q (specs 467+); at spec 443 only
    the bounds are known (they contain Q)."""
    for r in registrations:
        g = gk.Gatekeeper()
        q1, _, _, a = r.snapshots
        rec = feed(g, [q1, a])[0]
        assert rec.netuid_ok and rec.seed_failures == ()
        if r.spec_version >= 467:
            assert rec.lag_blocks == r.lag
        else:
            assert rec.lag_blocks is None and rec.queued_lo == int(q1.block) + 1 and rec.queued_hi == r.added_block - 1
            assert rec.queued_lo is not None and rec.queued_hi is not None
            assert rec.queued_lo <= r.queued_block <= rec.queued_hi


def test_queued_state_between_removal_and_add(registrations: Any) -> None:
    r = registrations[0]
    g = gk.Gatekeeper()
    feed(g, r.snapshots[:3])
    (pend,) = g.pending()
    assert pend.queued_block == r.queued_block and int(pend.victim.netuid) == r.victim_netuid  # type: ignore[union-attr]
    assert g.records() == ()


def test_seed_anomalies(registrations: Any) -> None:
    r = registrations[0]
    key = new_key(r)
    a = r.snapshots[3]

    def with_new(**kw: Any) -> ChainSnapshot:
        s = a.get(key)
        assert s is not None
        out: ChainSnapshot = replace(a, subnets=tuple(replace(x, **kw) if x.key == key else x for x in a.subnets))
        return out

    s_new = a.get(key)
    assert s_new is not None
    cases = {
        "weight": with_new(pool=replace(s_new.pool, w_quote_e18=4 * 10**17)),
        "alpha": with_new(pool=replace(s_new.pool, alpha=AlphaRao(s_new.pool.alpha * 2), px_alpha=s_new.pool.alpha * 2)),
        "tao": with_new(pool=replace(s_new.pool, tao=s_new.pool.tao + 10**9, px_tao=s_new.pool.tao + 10**9)),
    }
    for failure, snap in cases.items():
        g = gk.Gatekeeper()
        rec = feed(g, [*r.snapshots[:3], snap])[0]
        assert failure in rec.seed_failures and rec.seed_anomaly, failure
    started = with_new(first_emission_block=Block(int(a.block)))
    rec = feed(gk.Gatekeeper(), [*r.snapshots[:3], started])[0]
    assert not rec.seed_checked and not rec.seed_anomaly                 # first seen after start_call: not checked


def test_engine_flags_on_the_fixture(registrations: Any, make_engine: Callable[..., FeatureEngine]) -> None:
    r = registrations[1]
    eng = make_engine()
    prev = None
    fr = None
    for s in r.snapshots:
        fr = eng.update(s, derive_events(prev, s))
        prev = s
    assert fr is not None
    f = fr.feats[new_key(r)]
    assert {gk.UNSTARTED, gk.EMA_WARMING, gk.EMISSION_OFF, gk.YOUNG_IMMUNE} <= f.launch_flags
    assert gk.SEED_ANOMALY not in f.launch_flags
    assert eng.gatekeeper.record(new_key(r)).lag_blocks == r.lag    # type: ignore[union-attr]
    assert new_key(r) in [k for _, k in fr.prune.immunity_calendar]
    assert fr.universe_eligible <= len(fr.feats) and f.since_start_blocks is None


# ------------------------------------------------------------------------------------------------- non-registrations
def test_dissolve_without_prune_pressure_opens_nothing(spec_cls: Any, synth: Callable[..., SubnetState],
                                                       snap_of: Callable[..., ChainSnapshot]) -> None:
    specs = [spec_cls(n, reg_at=8_000_000 + n) for n in (3, 4, 5)]
    s0 = snap_of(9_000_000, [synth(sp, 9_000_000) for sp in specs], n_nonroot_networks=100)
    s1 = snap_of(9_000_060, [synth(sp, 9_000_060) for sp in specs[:2]], n_nonroot_networks=99)
    g = gk.Gatekeeper()
    assert feed(g, [s0, s1]) == [] and g.pending() == ()


def test_unmatched_pending_expires_and_records_are_bounded(spec_cls: Any, synth: Callable[..., SubnetState],
                                                           snap_of: Callable[..., ChainSnapshot]) -> None:
    old = [spec_cls(n, reg_at=8_000_000 + n) for n in (3, 4, 5)]
    g = gk.Gatekeeper()
    g.observe(None, snap_of(9_000_000, [synth(sp, 9_000_000) for sp in old]))
    g.observe(snap_of(9_000_000, [synth(sp, 9_000_000) for sp in old]),
              snap_of(9_000_060, [synth(sp, 9_000_060) for sp in old[:2]]))
    assert len(g.pending()) == 1
    g.observe(None, snap_of(9_000_060 + 7_201, [synth(sp, 9_007_261) for sp in old[:2]]))
    assert g.pending() == ()
    new = spec_cls(5, reg_at=9_100_000)
    s_new = replace(synth(new, 9_100_010), first_emission_block=None)
    a = snap_of(9_100_000, [synth(sp, 9_100_000) for sp in old[:2]])
    b = snap_of(9_100_010, [synth(sp, 9_100_010) for sp in old[:2]] + [s_new])
    (rec,) = g.observe(a, b)
    assert rec.queued_block is None and rec.victim is None and rec.netuid_ok is None
    assert g.record(new.key()) is not None
    g.observe(b, snap_of(9_100_000 + 216_001, [synth(sp, 9_316_001) for sp in old[:2]] + [synth(new, 9_316_001)]))
    assert g.record(new.key()) is None                                   # retained 216,000 blocks after registration


# ------------------------------------------------------------------------------------------------- states and flags
def test_states_flags_and_recorder_cadence(spec_cls: Any, synth: Callable[..., SubnetState], make_globals: Any) -> None:
    p = gk.GatekeeperParams()
    glob = make_globals()
    sp = spec_cls(6, reg_at=9_000_000)
    s = synth(sp, 9_000_700)                                             # started at reg_at + 600
    start = 9_000_600
    assert gk.launch_state(replace(s, first_emission_block=None), 9_000_700, p) is gk.LaunchState.WAIT_START
    assert gk.launch_state(s, start - 1 + 100_800, p) is gk.LaunchState.WATCH
    assert gk.launch_state(s, start - 1 + 100_801, p) is gk.LaunchState.MATURE
    assert gk.recorder_cadence(s, start + 10, p) == 1 and gk.recorder_cadence(s, start + 1_000, p) == 10
    assert gk.recorder_cadence(s, start + 8_000, p) is None
    assert gk.recorder_cadence(replace(s, first_emission_block=None), start, p) is None

    def flags(x: SubnetState, block: int, **kw: Any) -> frozenset[str]:
        return gk.launch_flags(x, glob, block, kw.pop("record", None), p_reg_24h=kw.pop("p24", None),
                               b_over_theta=kw.pop("bt", None), params=p)

    young = flags(s, start + 1_000)
    assert young == {gk.EMA_WARMING, gk.YOUNG_IMMUNE}
    mature_block = 9_000_000 + 864_000
    assert flags(s, mature_block) == frozenset()
    assert gk.BURNING in flags(replace(s, miner_burned=Decimal("0.61")), mature_block)
    assert gk.BURNING not in flags(replace(s, miner_burned=Decimal("0.60")), mature_block)
    assert gk.EMISSION_OFF in flags(replace(s, emission_enabled=False), mature_block)
    assert gk.REG_CLOCK_HOT in flags(s, mature_block, p24=Decimal("0.26"))
    assert gk.REG_CLOCK_HOT not in flags(s, mature_block, p24=Decimal("0.25"))
    assert gk.GATE_STARVED in flags(s, mature_block, bt=Decimal("0.39"))
    assert flags(replace(s, first_emission_block=None), mature_block) == {gk.UNSTARTED, gk.EMA_WARMING}
    assert gk.EMA_WARMING not in flags(s, start - 1 + 72_000)
    assert gk.EMA_WARMING in flags(s, start - 2 + 72_000)
    assert {gk.UNSTARTED, gk.SEED_ANOMALY, gk.EMA_WARMING, gk.BURNING, gk.EMISSION_OFF} == gk.NON_LCW_VETOES
    assert gk.GATE_STARVED in gk.MONITOR_ONLY and young <= gk.ALL_FLAGS


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_victim_was_the_prune_target_before_removal(registrations: Any, idx: int) -> None:
    from taotrader.protocol.prune import ladder
    r = registrations[idx]
    q1 = r.snapshots[0]
    assert int(ladder(q1)[0].netuid) == r.victim_netuid               # the brief's 52/52 prune rule on the fixture


def test_dissolve_before_registration_does_not_capture_the_added_generation(registrations: Any) -> None:
    """A dissolve (another netuid) shortly before the registration leaves a stale pending entry; the new generation
    is matched to the registration whose victim has its netuid."""
    r = registrations[0]
    q1, q, a1, a = r.snapshots
    other = next(s for s in q1.subnets if int(s.key.netuid) not in (r.victim_netuid, 0))
    early = replace(q1, block=Block(int(q1.block) - 100))                # at the subnet limit: removals look like prunes
    gone = tuple(s for s in early.subnets if s.key != other.key)
    dissolved = replace(early, block=Block(int(q1.block) - 50), subnets=gone)

    def without(s: ChainSnapshot) -> ChainSnapshot:
        return replace(s, subnets=tuple(x for x in s.subnets if x.key != other.key))

    g = gk.Gatekeeper()
    created = feed(g, [early, dissolved, without(q1), without(q), without(a1), without(a)])
    (rec,) = created
    assert rec.lag_blocks == r.lag and rec.netuid_ok
    assert [int(p.victim.netuid) for p in g.pending() if p.victim is not None] == [int(other.key.netuid)]
