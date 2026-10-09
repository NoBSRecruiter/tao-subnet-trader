"""Section 10.3 item 3: event waves on real chain data (tests/fixtures/minilake/waves, built by build_event_waves.py).

- 9,029,888 -> 9,029,889: the re-enable wave - one EMISSION_TOGGLED(true) per subnet whose SubnetEmissionEnabled
  flipped (the brief's "47"; the archive shows 49 flips, 45 of them started subnets and 4 never-started ones - the
  count is asserted against the raw flags and must reach the brief's figure; see the WP10 VERIFY items);
- 8,463,543 -> 8,463,544: the purge - one EMISSION_TOGGLED(false) per flipped subnet (brief "54"; archive 57), which
  leave the overlay EXITS-ready:
  a book holding three of the disabled names (positions built through the production reducer from journal facts at
  8,463,543) gets URGENT "emission_off" forced exits for every held disabled name, the wave halt (entries halted
  until block + 7,200, from the reducer and the overlay's "emission.wave" action), no buy intent, and sell intents for
  the held names - all through the production Engine.decide on the real snapshots.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.backtest.books import build_engine, calibration_provider, feature_engine, load_backtest_plan
from taotrader.core.events import (
    CapitalChanged,
    ChainEventKind,
    ChainEventObserved,
    DecisionTrace,
    FillReported,
    HealthObs,
    OrderIntended,
    SnapshotObserved,
    SubmitStarted,
    VenueAck,
)
from taotrader.core.fixed import DEC
from taotrader.core.orders import Fill, OrderIntent, OrderKind, Urgency, make_order_id
from taotrader.core.units import PPM, AlphaRao, Block, BookId, LogicalTime, Phase, Ppm, PriceRao, Rao, StrategyId
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore
from taotrader.engine.recovery import snapshot_digest
from taotrader.engine.reducer import fold_batch
from taotrader.protocol.amm import quote_buy
from taotrader.protocol.derive import derive_events
from taotrader.risk.overlay import StandardOverlay

WAVES = Path(__file__).resolve().parents[1] / "fixtures" / "minilake" / "waves"


@pytest.fixture(scope="module")
def store() -> Iterator[LakeSnapshotStore]:
    if not (WAVES / "lake").is_dir():
        pytest.fail("the event-wave fixture is missing (tests/fixtures/minilake/build_event_waves.py)")
    lake = Lake(WAVES / "lake", WAVES / "state.sqlite")
    st = LakeSnapshotStore(lake, clock=9_029_889)
    yield st
    lake.close()


def _toggles(store: LakeSnapshotStore, b: int) -> list[Any]:
    ev = derive_events(store.at(Block(b - 1)), store.at(Block(b)))
    return [e for e in ev if e.kind is ChainEventKind.EMISSION_TOGGLED]


def _flips(store: LakeSnapshotStore, b: int) -> set[Any]:
    prev, cur = store.at(Block(b - 1)), store.at(Block(b))
    return {s.key for s in cur.subnets if prev.get(s.key) is not None
            and prev.get(s.key).emission_enabled != s.emission_enabled}  # type: ignore[union-attr]


def test_reenable_wave_at_9029889(store: LakeSnapshotStore) -> None:
    t = _toggles(store, 9_029_889)
    assert all(e.flag is True for e in t)
    assert {e.key for e in t} == _flips(store, 9_029_889)            # one event per raw flip, nothing hidden
    assert len(t) >= 47                                               # the brief's re-enable count
    cur = store.at(Block(9_029_889))
    assert all(s.emission_enabled for s in cur.subnets)               # every subnet emits after the wave


def test_54_disables_at_8463544_leave_the_overlay_exits_ready(store: LakeSnapshotStore) -> None:
    prev, cur = store.at(Block(8_463_543)), store.at(Block(8_463_544))
    events = derive_events(prev, cur)
    off = [e for e in events if e.kind is ChainEventKind.EMISSION_TOGGLED and e.flag is False]
    assert {e.key for e in off} == _flips(store, 8_463_544) and len(off) >= 54
    plan = load_backtest_plan(env={})
    cfg = replace(plan.book("base-ew-total"), book=BookId("wave"))
    run = replace(plan.run, books=(cfg,))
    cal = calibration_provider(None)
    eng = build_engine(cfg, run, cal, StandardOverlay(cal, run_mode=run.mode, seed=run.seed))
    # --- three held disabled names, built through the reducer from journal facts at 8,463,543
    held = []
    keys = [e.key for e in off if e.key is not None]
    for key in sorted(keys, key=lambda k: -int(prev.get(k).pool.tao))[:3]:   # type: ignore[union-attr]
        s = prev.get(key)
        assert s is not None and s.hotkeys
        held.append((s, max(s.hotkeys, key=lambda h: (h.total_alpha, h.hotkey))))
    b0 = Block(8_463_543)
    state = eng.initial_state()
    snap0 = SnapshotObserved(b0, prev.block_hash, snapshot_digest(prev), prev.plan, prev.timestamp_ms, HealthObs.nominal())
    facts: list[Any] = [snap0, CapitalChanged(cfg.book, b0, int(cfg.capital_rao), int(cfg.fee_float_rao), "initial")]
    sid = StrategyId("baseline.ew_total")
    t0 = Block(b0 - 600)                                  # the holdings were bought earlier (outside the re-quote spacing)
    for s, h in held:
        tao = Rao(10 * 10**9)
        q = quote_buy(s.pool, tao)
        oid = make_order_id(run.run_id, cfg.book, t0, s.key, h.hotkey, OrderKind.ADD_STAKE_LIMIT, 0)
        intent = OrderIntent(oid, 0, cfg.book, t0, OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao, AlphaRao(0), False,
                             PriceRao(s.pool.spot_rao() * 2), False, True, Block(t0 + 5), int(q.amount_out), Urgency.NORMAL,
                             ((sid, Ppm(PPM)),), "test.wave")
        shares = DEC.divide(Decimal(int(q.amount_out)), h.index())
        fill = Fill(f"{oid}:0:0", oid, 0, cfg.book, Block(t0 + 5), OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao,
                    AlphaRao(int(q.amount_out)), shares, int(q.fee), Rao(0), Rao(cfg.exec.buy_tx_fee_rao), int(q.d_tao),
                    int(q.d_alpha), s.pool.spot_rao(), Ppm(0), True, exact_block=False)
        facts += [OrderIntended(intent), SubmitStarted(cfg.book, oid, 0, "sim0", None, Block(t0 + 13)),
                  VenueAck(cfg.book, oid, 0, t0, Block(t0 + 5), "", ""), FillReported(fill)]
    state = fold_batch(state, facts)
    assert len(state.portfolio.positions) == 3 and not state.breaches, state.breaches
    # --- the wave tick through the production Engine
    fe = feature_engine(cal, warm_blocks=0)
    fe.update(prev, ())
    frame = fe.update(cur, events)
    snap1 = SnapshotObserved(cur.block, cur.block_hash, snapshot_digest(cur), cur.plan, cur.timestamp_ms, HealthObs.nominal())
    inputs = [(LogicalTime(cur.block, Phase.INGEST, i), BookId(""), e)
              for i, e in enumerate([snap1, *[ChainEventObserved(x) for x in events]])]
    st = LakeSnapshotStore(store.lake, clock=int(cur.block))
    out = eng.decide(state, cur, prev, events, frame, cur, HealthObs.nominal(), inputs=inputs, store=st)
    traces = [e for _, _, e in out if isinstance(e, DecisionTrace)]
    (trace,) = traces
    for s, _ in held:
        acts = [a for a in trace.actions if a.action == "FORCE_EXIT" and a.key == s.key]
        assert any(a.rule == "exit.emission_off" and "urgency=3" in a.detail for a in acts), (s.key, acts)
        assert any(a.rule == "engine.forced_exit" and "rule=emission_off" in a.detail for a in acts), (s.key, acts)
    assert any(a.rule == "emission.wave" and "halt_until" in a.detail for a in trace.actions)
    intents = [e.intent for _, _, e in out if isinstance(e, OrderIntended)]
    dbg = "; ".join(f"{a.rule}|{a.key and a.key.netuid}|{a.detail}" for a in trace.actions
                    if a.key is None or a.key in {s.key for s, _ in held})
    assert intents and all(i.kind is not OrderKind.ADD_STAKE_LIMIT for i in intents), dbg
    assert {i.key for i in intents} <= {s.key for s, _ in held}
    assert all(i.urgency >= Urgency.URGENT for i in intents)
    # the reducer folds the wave: entries halted for a day
    after = fold_batch(state, [e for _, _, e in [*inputs, *out]])
    assert after.entries_halted_until is not None and after.entries_halted_until >= int(cur.block) + 7_200
