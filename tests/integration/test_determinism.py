"""Section 10.3 item 5: determinism of the production pipeline on real mini-lake data (SHORT window, 3 books).

- two runs give byte-identical journal hash chains;
- identical under PYTHONHASHSEED in {0, 1, 777} (separate processes);
- a restart on a complete journal re-verifies (deep hash-chain check + VERIFY replay of every batch) and writes nothing;
- a replay of a journal written by another process is money-identical (the cross-OS variant - a Linux replay of a
  Windows journal - runs on the Linux CI leg; this test pins the same property across processes on one OS).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from taotrader.backtest.runner import run_pass
from taotrader.data.journal import SqliteJournal
from taotrader.data.lake import Lake

END = 8_776_000

SCRIPT = r'''
import json, sys
from taotrader.backtest.books import load_backtest_plan
from taotrader.backtest.runner import run_pass
from taotrader.data.journal import SqliteJournal
from taotrader.data.lake import Lake
lake_dir, state, journal_path, end = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
plan = load_backtest_plan(env={}, cli=[f'backtest.lake="{lake_dir}"', "backtest.start_block=8765400",
                                       f"backtest.end_block={end}", "backtest.warmup_blocks=0",
                                       "backtest.feature_warm_blocks=7200"])
lake = Lake(lake_dir, state)
j = SqliteJournal(journal_path, durable=False)
res = run_pass(plan, lake, books=["base-ew-total", "base-random", "carry"], journal=j, observe=False)
hashes = [r.hash.hex() for r in j.read(1)]
print(json.dumps({"head": res.journal_head, "n": len(hashes), "chain": hashes[-1] if hashes else "",
                  "money": {k: v.money_digest for k, v in sorted(res.books.items())}}))
j.close(); lake.close()
'''


def _chain(j: SqliteJournal) -> list[str]:
    return [r.hash.hex() for r in j.read(1)]


def test_two_runs_identical_chains_and_a_restart_writes_nothing(itx: Any, short_plan: Any, lake: Lake, tmp_path: Path) -> None:
    books = itx.short_books()
    j1 = SqliteJournal(str(tmp_path / "a.sqlite"), durable=False)
    j2 = SqliteJournal(str(tmp_path / "b.sqlite"), durable=False)
    a = run_pass(short_plan, lake, books=books, end=END, journal=j1, observe=False)
    b = run_pass(short_plan, lake, books=books, end=END, journal=j2, observe=False)
    assert a.ticks > 30
    assert _chain(j1) == _chain(j2) and a.journal_head == b.journal_head
    assert any(o.money_digest for o in a.books.values())
    head = j1.head()
    j1.close()
    j3 = SqliteJournal(str(tmp_path / "a.sqlite"), durable=False)
    assert j3.verify_chain(deep=True) == head[0]
    again = run_pass(short_plan, lake, books=books, end=END, journal=j3, observe=False)
    assert again.ticks == 0 and j3.head() == head                 # re-verified (VERIFY replay) and nothing written
    assert {k: v.money_digest for k, v in again.books.items()} == {k: v.money_digest for k, v in a.books.items()}
    j2.close()
    j3.close()


def test_identical_under_pythonhashseed_and_across_processes(itx: Any, short_plan: Any, lake: Lake, tmp_path: Path) -> None:
    lake_dir, state = itx.MINILAKE
    script = tmp_path / "det_run.py"
    script.write_text(SCRIPT, encoding="utf-8")
    outs = []
    for seed in ("0", "1", "777"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, str(script), Path(lake_dir).as_posix(), str(state), str(tmp_path / f"j{seed}.sqlite"),
                            str(END)], capture_output=True, text=True, env=env, timeout=1_800, check=False)
        assert r.returncode == 0, r.stderr[-2000:]
        outs.append(json.loads(r.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1] == outs[2]
    # this process replays the journal another process wrote: money-identical, nothing new written
    j = SqliteJournal(str(tmp_path / "j0.sqlite"), durable=False)
    before = j.head()
    res = run_pass(short_plan, lake, books=itx.short_books(), end=END, journal=j, observe=False)
    assert res.ticks == 0 and j.head() == before
    assert {k: v.money_digest for k, v in sorted(res.books.items())} == outs[0]["money"]
    j.close()
