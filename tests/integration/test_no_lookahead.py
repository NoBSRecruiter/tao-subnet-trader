"""Section 10.3 item 6: no lookahead (section 8.10), through the production pipeline on real mini-lake data.

- future truncation: runs on data[:k] and data[:k+m] agree on every decision before k - latency;
- the peeking canary raises LookaheadError on every evaluation (journaled engine.strategy_error) while the other books
  of the same pass are unaffected;
- as-of calibration: perturbing or deleting registrations, prunes and dissolutions after block t leaves every decision
  - and every DecisionTrace.calib_digest - before t byte-identical (and the perturbation does change decisions'
  calibration after t, so the test can see a difference).
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from taotrader.backtest import bias
from taotrader.backtest.books import BookVariant, brief_log_registrations
from taotrader.backtest.runner import run_pass
from taotrader.core.config import SleeveCfg
from taotrader.core.events import DecisionTrace
from taotrader.core.units import PPM, Block, BookId, NetUid, Ppm, Stage, StrategyId
from taotrader.data.calibration import CalibrationInputs, DissolutionObs, LakeCalibrationProvider, PruneObs
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.protocol.prune import RegistrationRow

T = 8_774_000


def test_future_truncation(itx: Any, short_plan: Any, lake: Lake) -> None:
    k, m = 8_775_000, 900
    ja, jb = SqliteJournal(":memory:", durable=False), SqliteJournal(":memory:", durable=False)
    itx.run_short(short_plan, lake, end=k, journal=ja, observe=False)
    itx.run_short(short_plan, lake, end=k + m, journal=jb, observe=False)
    ex = short_plan.book("carry").exec
    ok, n = bias.truncation_agrees(ja, jb, k, ex.finality_lag_blocks + ex.latency_blocks)
    assert ok and n > 50


def test_peeking_canary_raises_and_does_not_leak(itx: Any, short_plan: Any, lake: Lake) -> None:
    base = short_plan.book("base-cash")
    cb = replace(base, book=BookId("canary"), sleeves=(SleeveCfg(StrategyId("canary.peek"), Stage.RESEARCH, Ppm(PPM)),))
    vs = dict(short_plan.variants)
    vs[cb.book] = BookVariant(cb.book, cb.book, "temporary", cb.dereg_model, (StrategyId("canary.peek"),))
    plan = replace(short_plan, run=replace(short_plan.run, books=short_plan.run.books + (cb,)), variants=MappingProxyType(vs))
    canary = bias.PeekingCanary()
    j = SqliteJournal(":memory:", durable=False)
    with_canary = run_pass(plan, lake, books=["canary", "base-ew-total"], journal=j, extra_strategies={"canary": [canary]},
                           observe=False)
    errs = bias.peek_errors(j)
    assert canary.peeks > 10 and len(errs) == canary.peeks and all("LookaheadError" in d for _, d in errs)
    alone = run_pass(short_plan, lake, books=["base-ew-total"], observe=False)
    assert with_canary.books["base-ew-total"].money_digest == alone.books["base-ew-total"].money_digest


def _provider(extra: bool) -> LakeCalibrationProvider:
    regs = list(brief_log_registrations())
    prunes: list[PruneObs] = []
    diss: list[DissolutionObs] = []
    if extra:                                          # events strictly after T: synthetic additions ...
        regs += [RegistrationRow(Block(T + 100), NetUid(99), Decimal("1.1"), 30_000),
                 RegistrationRow(Block(T + 400), NetUid(7), Decimal("1.3"), 20_000)]
        prunes += [PruneObs(T + 100, (Decimal(1), Decimal("1.2"), Decimal("1.5")), Decimal(1), 1)]
        diss += [DissolutionObs(T + 100, Decimal("0.36"), Decimal("0.30"))]
    else:                                              # ... and deletions/perturbations of real rows after T
        regs = [r if r.queued_block <= T else replace(r, cost_ratio=r.cost_ratio + Decimal("0.2"))
                for r in regs if r.queued_block != 8_825_550]
    return LakeCalibrationProvider(CalibrationInputs(registrations=tuple(sorted(regs, key=lambda r: r.queued_block)),
                                                     prunes=tuple(prunes), dissolutions=tuple(diss)))


def _traces(j: SqliteJournal) -> list[tuple[int, str, bytes, str]]:
    out = []
    for rec in j.read(1):
        if rec.kind == "decision_trace":
            ev = decode_record(rec)
            assert isinstance(ev, DecisionTrace)
            out.append((int(ev.block), str(ev.book), rec.payload, ev.calib_digest))
    return out


def test_asof_calibration_perturbation_after_t_leaves_earlier_decisions_identical(itx: Any, short_plan: Any,
                                                                                    lake: Lake) -> None:
    ja, jb = SqliteJournal(":memory:", durable=False), SqliteJournal(":memory:", durable=False)
    itx.run_short(short_plan, lake, journal=ja, calibration=_provider(True), observe=False)
    itx.run_short(short_plan, lake, journal=jb, calibration=_provider(False), observe=False)
    before_a = bias.decision_records(ja, before=T)
    before_b = bias.decision_records(jb, before=T)
    assert before_a and before_a == before_b                      # decisions AND calib digests before T identical
    ta, tb = _traces(ja), _traces(jb)
    assert [x for x in ta if x[0] < T] == [x for x in tb if x[0] < T]
    after_a = {x[3] for x in ta if x[0] > T + 100}
    after_b = {x[3] for x in tb if x[0] > T + 100}
    assert after_a and after_b and after_a != after_b             # the perturbation is visible after T
