"""Section 10.3 item 4: crash matrix with the real SimVenue on real mini-lake data.

Fault points (engine.runner fault hooks + an interruption inside the SQLite write transaction): pre_commit,
in_transaction, post_commit, drain_post_commit, after_submit_started, after_venue_submit, outbox_post_commit. A
crash-free pass enumerates every hit; each sampled hit is then crashed (InjectedCrash, a BaseException), the process
state is dropped and a FRESH plan / lake / venues / features / journal handle recovers on the same journal file and
finishes. Every case must end with the crash-free money-state digest per book, the same intents and fills, and no
duplicate intent or fill.

Window: the integration SHORT window (8,765,400 -> 8,776,000; trading in its last ~3,000 blocks) with base-ew-total,
base-random and carry - the order paths are exercised (buys, stride fills, the outbox bracket). pytest samples
TAOTRADER_CRASH_SAMPLES points (default 36, an equal quota per fault-point kind, evenly spaced over its hits) on
TAOTRADER_CRASH_WORKERS processes (default 6); TAOTRADER_FULL_MATRIX=1 runs every point (the nightly matrix).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from taotrader.backtest.runner import FAULT_POINTS, CrashSpec, run_crash_matrix

END = 8_776_000


def test_crash_matrix_reproduces_the_money_state(itx: Any, tmp_path: Path) -> None:
    lake_dir, state = itx.MINILAKE
    spec = CrashSpec(lake=(str(lake_dir), str(state)),
                     cli=(f'backtest.lake="{Path(lake_dir).as_posix()}"', f"backtest.start_block={itx.SHORT_START}",
                          f"backtest.end_block={END}", "backtest.warmup_blocks=0",
                          f"backtest.feature_warm_blocks={itx.SHORT_WARM}"),
                     books=("base-ew-total", "base-random", "carry"), start=itx.SHORT_START, end=END)
    full = os.environ.get("TAOTRADER_FULL_MATRIX") == "1"
    samples = None if full else int(os.environ.get("TAOTRADER_CRASH_SAMPLES", "36"))
    workers = int(os.environ.get("TAOTRADER_CRASH_WORKERS", "6"))
    ref, outs = run_crash_matrix(spec, tmp_path, samples=samples, workers=workers)
    assert ref.fills and ref.intents, "the window must exercise the order path"
    assert ref.duplicate_intents == 0 and ref.duplicate_fills == 0
    kinds = {o.point for o in outs}
    assert {"pre_commit", "post_commit", "in_transaction", "after_submit_started", "after_venue_submit",
            "outbox_post_commit", "drain_post_commit"} <= kinds <= set(FAULT_POINTS)
    bad = []
    for o in outs:
        if not o.crashed or o.error or o.money != ref.money or o.intents != ref.intents or o.fills != ref.fills \
                or o.duplicate_intents or o.duplicate_fills:
            bad.append((o.point, o.occurrence, o.crashed, o.error, o.money == ref.money, o.intents == ref.intents,
                        o.fills == ref.fills, o.duplicate_intents, o.duplicate_fills))
    print(f"crash matrix: {len(outs)} cases over {sorted(kinds)}; failures {len(bad)}")
    assert not bad, bad[:10]
