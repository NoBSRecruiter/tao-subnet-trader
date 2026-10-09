"""WP10 backtest.runner: run identity, per-book digests, resume, trial registry, sensitivity/capacity variants, grid."""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

from taotrader.backtest import runner as rn
from taotrader.backtest.books import BacktestPlan
from taotrader.core.config import RunCfg
from taotrader.core.units import RAO_PER_TAO
from taotrader.data.journal import SqliteJournal
from taotrader.data.lake import Lake
from taotrader.ops.config_load import validate_run_config

BOOKS = ["base-ew-total", "carry"]
END = 8_775_600


def test_code_hash_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\r\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
    h1 = rn.code_hash(tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\n")                      # line endings do not matter
    assert rn.code_hash(tmp_path) == h1
    (tmp_path / "sub" / "b.py").write_text("y = 3\n", encoding="utf-8")
    assert rn.code_hash(tmp_path) != h1
    assert len(rn.code_hash()) == 64


def test_identity_run_key_covers_every_part() -> None:
    a = rn.RunIdentity("c", "f", "p", "m", 0)
    keys = {a.run_key(), rn.RunIdentity("x", "f", "p", "m", 0).run_key(), rn.RunIdentity("c", "x", "p", "m", 0).run_key(),
            rn.RunIdentity("c", "f", "x", "m", 0).run_key(), rn.RunIdentity("c", "f", "p", "x", 0).run_key(),
            rn.RunIdentity("c", "f", "p", "m", 1).run_key()}
    assert len(keys) == 6


def test_a_resumed_pass_equals_an_uninterrupted_one_and_a_restart_writes_nothing(short_plan: BacktestPlan, open_lake: Lake,
                                                                          tmp_path: Path) -> None:
    j1 = SqliteJournal(":memory:", durable=False)
    a = rn.run_pass(short_plan, open_lake, books=BOOKS, end=END, journal=j1)
    assert a.books["base-ew-total"].metrics.trades.fills > 0
    # stop after 10 ticks, restart with fresh objects on the same journal file, finish: same chain as one run
    path = tmp_path / "j.sqlite"
    jf = SqliteJournal(str(path), durable=False)
    part = rn.run_pass(short_plan, open_lake, books=BOOKS, end=END, journal=jf, max_ticks=10)
    jf.close()
    assert part.ticks == 10
    jf2 = SqliteJournal(str(path), durable=False)
    rest = rn.run_pass(short_plan, open_lake, books=BOOKS, end=END, journal=jf2)
    assert rest.journal_head == a.journal_head and rest.digests() == a.digests()     # == an uninterrupted run
    assert {k: v.money_digest for k, v in rest.books.items()} == {k: v.money_digest for k, v in a.books.items()}
    assert rest.ticks == a.ticks - 10
    # a restart on the complete journal writes nothing
    again = rn.run_pass(short_plan, open_lake, books=BOOKS, end=END, journal=jf2)
    assert again.ticks == 0 and again.journal_head == a.journal_head
    jf2.close()


def test_book_digests_cover_only_book_records(short_plan: BacktestPlan, open_lake: Lake) -> None:
    j = SqliteJournal(":memory:", durable=False)
    rn.run_pass(short_plan, open_lake, books=["base-cash"], end=8_766_000, journal=j, observe=False)
    d = rn.book_digests(j)
    assert set(d) == {"base-cash"}
    assert all(len(v) == 32 for v in d.values())


def test_trial_registry(tmp_path: Path) -> None:
    reg = rn.TrialRegistry(tmp_path / "state.sqlite")
    t1 = reg.record(cfg_hash="h", strategy="carry", data_range="1-2", purpose="s0", identity="k")
    t2 = reg.record(cfg_hash="h", strategy="carry", data_range="1-2", purpose="s0", identity="k")   # idempotent
    reg.record(cfg_hash="h", strategy="momentum", data_range="1-2", purpose="s0", identity="k")
    assert t1 == t2
    assert reg.count() == 2 and reg.count("carry") == 1
    reg.close()
    reopened = rn.TrialRegistry(tmp_path / "state.sqlite")
    assert reopened.count() == 2
    reopened.close()


def test_sensitivity_and_capacity_variants_validate(short_plan: BacktestPlan) -> None:
    base = short_plan.book("carry")
    sens = rn.sensitivity_books(base)
    assert len(sens) == 16 and len({b.book for b in sens}) == 16
    lat2 = next(b for b in sens if b.book.endswith("-f2-l2"))
    assert lat2.exec.latency_blocks == 2 * base.exec.latency_blocks
    assert lat2.exec.buy_tx_fee_rao == 2 * base.exec.buy_tx_fee_rao
    assert lat2.risk.unwind_exec_blocks == lat2.exec.finality_lag_blocks + lat2.exec.latency_blocks
    caps = rn.capacity_books(base)
    assert [b.capital_rao // RAO_PER_TAO for b in caps] == list(rn.CAPACITY_TAO)
    assert all(b.fee_float_rao >= RAO_PER_TAO for b in caps)
    run: RunCfg = short_plan.run
    from dataclasses import replace
    validate_run_config(replace(run, books=tuple(sens) + tuple(caps)))     # cross-field rules hold


def test_grid_in_process_registers_one_trial_per_book(short_plan: BacktestPlan, minilake: tuple[Path, Path],
                                                      tmp_path: Path) -> None:
    lake_dir = minilake[0]
    cli = (f'backtest.lake="{lake_dir.as_posix()}"', "backtest.warmup_blocks=0", "backtest.feature_warm_blocks=600")
    jobs = [rn.GridJob("a", str(lake_dir), cli=cli, books=("base-cash",), start=8_765_400, end=8_766_200, purpose="t"),
            rn.GridJob("b", str(lake_dir), cli=cli, books=(), start=8_765_400, end=8_766_200, purpose="t",
                       variants=(("capacity", "base-ew-total", 1), ("capacity", "base-ew-total", 10)))]
    reg = rn.TrialRegistry(tmp_path / "t.sqlite")
    out = rn.run_grid(jobs, registry=reg, in_process=True)
    pickle.dumps(jobs)                                          # jobs travel to worker processes
    assert [r.label for r in out] == ["a", "b"]
    assert set(out[0].metrics) == {"base-cash"}
    assert set(out[1].metrics) == {"base-ew-total-cap1", "base-ew-total-cap10"}
    assert reg.count() == 3 and all(len(r.trials) == len(r.metrics) for r in out)
    m: Any = out[1].metrics["base-ew-total-cap1"]
    assert m.start_nav_tao > 0
    reg.close()


def test_sample_fault_points_covers_every_kind() -> None:
    pts = [("pre_commit", i) for i in range(100)] + [("after_submit_started", i) for i in range(40)] + \
          [("drain_post_commit", i) for i in range(3)] + [("post_commit", i) for i in range(100)]
    got = rn.sample_fault_points(pts, 20)
    kinds = {p[0] for p in got}
    assert kinds == {"pre_commit", "after_submit_started", "drain_post_commit", "post_commit"}
    assert len(got) == 20 and len(set(got)) == 20
    assert ("drain_post_commit", 0) in got and ("drain_post_commit", 2) in got
    assert ("pre_commit", 0) in got and ("pre_commit", 99) in got           # first and last hits
    assert rn.sample_fault_points(pts, None) == pts
    assert rn.sample_fault_points(pts, 20) == got                           # deterministic
