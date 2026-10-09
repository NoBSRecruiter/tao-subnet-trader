"""Section 10.3 item 2: prune replay - the SN116 prune at P = 9,210,610 (rank 2 -> 1 in < 1 h, 0.3% gap).

Fixture: tests/fixtures/minilake/prune116 (build_prune_window.py): 60-block FULL snapshots over [P - 3,600, P + 120]
with runtime prune-target probes on the 600-block grid, plus the per-block REFINED window [P - 120, P + 25] that
ParquetReplay merges unthinned (block-exact ticks before and at the removal).

The synthetic holding: SN116 at ~1% of its pool TAO, journaled at the window start as ordinary facts (initial
CapitalChanged, OrderIntended, SubmitStarted, VenueAck, FillReported priced on the stored pool) BEFORE the replay -
recovery folds them into the books and the SimVenues exactly like any journal, so the production Runner, Engine,
overlay and planner then manage a real position. (The universe floor would refuse a fresh entry here: SN116 sits in
the prune zone and the 1-hour fixture has no router history - that is why the holding is synthetic.)

Asserted:
1. the local ladder target (protocol.prune.prune_target on the stored snapshot) equals the runtime target
   (SubnetInfoRuntimeApi_get_subnet_to_prune, the collector's calib rows) at every sampled block;
2. the holding exits through Tier A or the backstop (overlay forced exit exit.prune_*) before P under the default
   M_A - or the miss is reported as the known fast-prune case (SN116 became the target less than U + M_A before P);
3. a holder that rides the dissolution (test overlay: no forced exit on SN116) is settled by DeregSettled at P from
   the REFINED removal-1 snapshot:
   payout = alpha x spot(P-1) x R with R = effective_recovery(recovery_ratio(P-1 state)), and |payout ratio - R| is
   within the FT10 tolerance (0.05). The OBSERVED FT10 ratio (coldkey free-balance deltas over the on_idle blocks) is
   not in the fixture; see the WP10 report.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest

from taotrader.backtest.books import BookVariant, PruneBlindOverlay, calibration_provider
from taotrader.backtest.runner import identity_of, run_pass
from taotrader.core.config import BookCfg, SleeveCfg
from taotrader.core.events import (
    CapitalChanged,
    ChainEventKind,
    ConfigApplied,
    DecisionTrace,
    DeregSettled,
    FillReported,
    JournalEvent,
    OrderIntended,
    SubmitStarted,
    VenueAck,
)
from taotrader.core.fixed import DEC, floor_int
from taotrader.core.orders import Fill, OrderIntent, OrderKind, Urgency, make_order_id
from taotrader.core.protocols import TickContext
from taotrader.core.signals import Signal, SignalKind, StrategyOutput
from taotrader.core.state import ChainSnapshot
from taotrader.core.units import PPM, AlphaRao, Block, BookId, LogicalTime, Phase, Ppm, PriceRao, Rao, Stage, StrategyId
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore
from taotrader.ops.config_load import load_preregistration
from taotrader.protocol.amm import quote_buy
from taotrader.protocol.calibration import effective_recovery
from taotrader.protocol.prune import prune_target, recovery_ratio
from taotrader.risk.overlay import StandardOverlay

P = 9_210_610
VICTIM = 116
B0 = 9_206_400                                  # first stored block: the synthetic holding is journaled here
END = P + 120


class Holder:
    """Keeps a TARGET on the victim generation at `frac_ppm` of its pool TAO (holds the synthetic position)."""

    def __init__(self, sid: str, frac_ppm: int = 10_000) -> None:
        self.id = StrategyId(sid)
        self.decide_every_blocks = 300
        self.wake_on: frozenset[ChainEventKind] = frozenset()
        self.min_cadence_blocks = 60
        self.valid_from_block = Block(0)
        self.declares_dilution = False
        self.frac_ppm = frac_ppm

    def initial_memory(self) -> object:
        return 0

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        s = ctx.raw.by_netuid(VICTIM)
        if s is None:
            return StrategyOutput((), memory)
        size = Rao(int(s.pool.tao) * self.frac_ppm // PPM)
        return StrategyOutput((Signal(self.id, s.key, ctx.block, SignalKind.TARGET, weight_ppm=Ppm(PPM), max_size_rao=size,
                                      reasons=("test.holder",)),), memory)


class HoldThrough:
    """Test overlay for the settlement check: the wrapped overlay with every forced exit on the victim dropped and its
    proposal target restored (a holder that rides the dissolution; the prune-blind baseline alone would still leave on
    the burn rule)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def review(self, proposal: Any, ctx: Any, *args: Any, **kwargs: Any) -> Any:
        dec = self.inner.review(proposal, ctx, *args, **kwargs)
        items = {t.key: t for t in dec.targets.items}
        for t in proposal.items:
            if int(t.key.netuid) == VICTIM:
                items[t.key] = t
        forced = tuple(f for f in dec.targets.forced if int(f.key.netuid) != VICTIM)
        acts = tuple(a for a in dec.actions if not (a.key is not None and int(a.key.netuid) == VICTIM
                                                     and a.action in ("FORCE_EXIT", "VETO_ENTRY")))
        tb = replace(dec.targets, items=tuple(items[k] for k in sorted(items, key=lambda k: (k.netuid, k.reg_at))),
                     forced=forced)
        return replace(dec, targets=tb, actions=acts)


@pytest.fixture(scope="module")
def prune_lake(itx: Any) -> Iterator[Lake]:
    lake_dir, state = itx.PRUNE_LAKE
    if not (lake_dir.is_dir() and state.is_file()):
        pytest.fail("the prune-window fixture is missing (tests/fixtures/minilake/build_prune_window.py)")
    lk = Lake(lake_dir, state)
    yield lk
    lk.close()


def test_local_target_equals_the_runtime_target_at_sampled_blocks(prune_lake: Lake) -> None:
    con = prune_lake.connect()
    try:
        rows = con.execute("SELECT block, model, chain FROM v_calib WHERE probe = 'prune_target' ORDER BY block").fetchall()
    finally:
        con.close()
    assert len(rows) >= 5
    store = LakeSnapshotStore(prune_lake, clock=END)
    for b, model, chain in rows:
        assert model == chain, (b, model, chain)
        local = prune_target(store.at(Block(int(b))))
        assert (0 if local is None else int(local.netuid)) == int(chain), b
    before = [int(c) for b, _m, c in rows if int(b) < P]
    assert before and before[-1] == VICTIM                        # SN116 is the runtime target just before P


def _seed(book: BookCfg, run_id: str, snap: ChainSnapshot) -> tuple[list[tuple[Phase, JournalEvent]], list[JournalEvent]]:
    """The synthetic holding's journal facts: at B0 (capital, intent, submit, ack) and at B0 + 5 (the fill)."""
    s = snap.by_netuid(VICTIM)
    assert s is not None and s.hotkeys
    h = max(s.hotkeys, key=lambda x: (x.total_alpha, x.hotkey))
    tao = Rao(int(s.pool.tao) // 100)                             # ~1% of the pool's TAO
    q = quote_buy(s.pool, tao)
    oid = make_order_id(run_id, book.book, Block(B0), s.key, h.hotkey, OrderKind.ADD_STAKE_LIMIT, 0)
    sid = book.sleeves[0].strategy
    intent = OrderIntent(oid, 0, book.book, Block(B0), OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao, AlphaRao(0), False,
                         PriceRao(s.pool.spot_rao() * 2), False, True, Block(B0 + 5), int(q.amount_out), Urgency.NORMAL,
                         ((sid, Ppm(PPM)),), "test.synthetic_holding")
    shares = DEC.divide(Decimal(int(q.amount_out)), h.index())
    fill = Fill(f"{oid}:0:0", oid, 0, book.book, Block(B0 + 5), OrderKind.ADD_STAKE_LIMIT, s.key, h.hotkey, tao,
                AlphaRao(int(q.amount_out)), shares, int(q.fee), Rao(0), Rao(book.exec.buy_tx_fee_rao), int(q.d_tao),
                int(q.d_alpha), s.pool.spot_rao(), Ppm(0), True, exact_block=True)
    first = [(Phase.INGEST, CapitalChanged(book.book, Block(B0), int(book.capital_rao), int(book.fee_float_rao), "initial")),
             (Phase.EMIT, OrderIntended(intent)),
             (Phase.OUTBOX, SubmitStarted(book.book, oid, 0, "sim0", None, Block(B0 + 13))),
             (Phase.OUTBOX, VenueAck(book.book, oid, 0, Block(B0), Block(B0 + 5), "", ""))]
    return first, [FillReported(fill)]


def test_synthetic_holding_exits_before_the_prune_and_dereg_payout_matches(itx: Any, prune_lake: Lake) -> None:
    base_plan = itx.plan_for(itx.PRUNE_LAKE[0], B0, END, 0)        # frames warm at once: the holder runs at tick 1
    base = base_plan.book("base-cash")
    books = [replace(base, book=BookId(name), capital_rao=Rao(10_000 * 10**9),
                     sleeves=(SleeveCfg(StrategyId(sid), Stage.RESEARCH, Ppm(PPM)),))
             for name, sid in (("holder", "test.holder"), ("holder-blind", "test.holder.blind"))]
    vs = dict(base_plan.variants)
    for b in books:
        vs[b.book] = BookVariant(b.book, b.book, "temporary", b.dereg_model, tuple(s.strategy for s in b.sleeves))
    plan = replace(base_plan, run=replace(base_plan.run, books=base_plan.run.books + tuple(books)),
                   variants=MappingProxyType(vs))
    names = ["holder", "holder-blind"]
    ident = identity_of(plan.subset(names).run, prune_lake)
    store = LakeSnapshotStore(prune_lake, clock=END)
    snap0 = store.at(Block(B0))
    journal = SqliteJournal(":memory:", durable=False)
    first: list[tuple[LogicalTime, BookId, JournalEvent]] = [
        (LogicalTime(Block(B0), Phase.INGEST, 0), BookId(""), ConfigApplied(Block(B0), ident.config_hash, ident.code_hash,
                                                                                ident.prereg_hash))]
    second: list[tuple[LogicalTime, BookId, JournalEvent]] = []
    for b in books:
        a, z = _seed(b, plan.run.run_id, snap0)
        first += [(LogicalTime(Block(B0), ph, i), b.book, e) for i, (ph, e) in enumerate(a)]
        second += [(LogicalTime(Block(B0 + 5), Phase.VENUE, i), b.book, e) for i, e in enumerate(z)]
    journal.append_batch(first)
    journal.append_batch(second)
    cal = calibration_provider(prune_lake)
    blind = HoldThrough(PruneBlindOverlay(StandardOverlay(cal, run_mode=plan.run.mode, seed=plan.run.seed)))
    res = run_pass(plan, prune_lake, books=names, journal=journal, identity=ident, calibration=cal,
                   overlays={"holder-blind": blind},
                   extra_strategies={"holder": [Holder("test.holder")], "holder-blind": [Holder("test.holder.blind")]})
    assert res.last_block is not None and res.last_block >= P
    fills: dict[str, list[Any]] = {"holder": [], "holder-blind": []}
    forced: list[tuple[int, str]] = []
    settled: list[DeregSettled] = []
    for rec in journal.read(1):
        if rec.kind == "fill_reported":
            ev = decode_record(rec)
            assert isinstance(ev, FillReported)
            fills[str(ev.fill.book)].append(ev.fill)
        elif rec.kind == "decision_trace" and rec.book == "holder":
            ev = decode_record(rec)
            assert isinstance(ev, DecisionTrace)
            forced += [(int(ev.block), a.rule) for a in ev.actions if a.action == "FORCE_EXIT"
                       and a.key is not None and int(a.key.netuid) == VICTIM]
        elif rec.kind == "dereg_settled":
            ev = decode_record(rec)
            assert isinstance(ev, DeregSettled)
            settled.append(ev)
    for o in res.books.values():
        assert o.orphans == 0 and not o.breaches, (o.book, o.breaches)
    # --- 2. Tier A / backstop exit before P (or the known fast-prune case)
    target_from: int | None = None
    for b in store.selected_blocks(B0, P - 1):
        t = prune_target(store.at(Block(b)))
        if t is not None and int(t.netuid) == VICTIM:
            target_from = b if target_from is None else target_from
        else:
            target_from = None
    risk = books[0].risk
    u_plus_ma = risk.unwind_exec_blocks * (1 + risk.unwind_retries) + risk.margin_a_blocks
    fast = target_from is not None and P - target_from < u_plus_ma
    sells = [f for f in fills["holder"] if f.kind is not OrderKind.ADD_STAKE_LIMIT and int(f.key.netuid) == VICTIM]
    exited = [f for f in sells if int(f.block) < P]
    prune_rules = sorted({r for _, r in forced if r.startswith("exit.prune")})
    print(f"prune replay: forced {forced[:4]}; exits {[int(f.block) for f in exited]}; prune rules {prune_rules}; "
          f"target from {target_from}; fast-prune case {fast}")
    if exited:
        assert prune_rules, "the exit before P must come from a prune rule"
        assert not any(s.book == "holder" for s in settled)
    else:
        assert fast, "no exit before P and not the known fast-prune case"
    # --- 3. the prune-blind holder is settled at P from the removal-1 snapshot
    (ds,) = [s for s in settled if s.book == "holder-blind"]
    assert int(ds.block) == P and int(ds.key.netuid) == VICTIM and ds.model == "formula"
    assert not [f for f in fills["holder-blind"] if f.kind is not OrderKind.ADD_STAKE_LIMIT]
    prev = store.at(Block(P - 1))                                  # REFINED removal-1 snapshot (per-block window)
    s = prev.get(ds.key)
    assert s is not None
    c = cal.asof(Block(P))
    r_formula = recovery_ratio(s, prev.glob, c.r_default)
    r_eff = effective_recovery(r_formula, c)
    expected = floor_int(DEC.multiply(DEC.multiply(Decimal(int(ds.alpha_value)), s.pool.spot()), r_eff))
    assert int(ds.payout_tao) == expected                         # alpha x spot(P-1) x R, R from the P-1 state
    ratio = DEC.divide(Decimal(int(ds.payout_tao)), DEC.multiply(Decimal(int(ds.alpha_value)), s.pool.spot()))
    tol = Decimal(str(load_preregistration()["falsification"]["FT10"]["abs_err_max"]))
    print(f"dereg payout ratio {ratio:.4f}; formula R at P-1 {r_formula:.4f}; effective R {r_eff:.4f} "
          f"(FT10 cap {'on' if c.r_cap_formula else 'off'})")
    assert abs(ratio - r_eff) <= tol
    assert store.at(Block(P)).get(ds.key) is None                 # the generation is gone at the removal block
