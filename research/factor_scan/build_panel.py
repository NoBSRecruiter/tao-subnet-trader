"""Record the generation-keyed study panel once (base-cash book only, 300-block stride) and pickle it.

Usage (from the repo root):
    .venv/Scripts/python.exe research/factor_scan/build_panel.py [--record-every 300] [--size-tao 5] [--out data/research/panel.pkl]
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
from taotrader.backtest.books import BOOKS_BACKTEST, DEFAULT_CONFIG, load_backtest_plan  # noqa: E402
from taotrader.backtest.runner import run_pass  # noqa: E402
from taotrader.backtest.studies import PanelRecorder  # noqa: E402
from taotrader.data.lake import Lake  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record-every", type=int, default=300, help="record panel rows only at blocks divisible by this")
    ap.add_argument("--size-tao", type=float, default=5.0, help="position size used for the round-trip cost column")
    ap.add_argument("--out", default="data/research/panel.pkl")
    a = ap.parse_args()
    plan = load_backtest_plan((DEFAULT_CONFIG, BOOKS_BACKTEST))
    lake = Lake("data/lake")
    rec = PanelRecorder(study_size_tao=a.size_tao)
    every = a.record_every

    def on_tick(runner, snap):  # type: ignore[no-untyped-def]
        if int(snap.block) % every == 0:
            rec(runner, snap)

    t0 = time.time()
    try:
        res = run_pass(plan, lake, books=["base-cash"], on_tick=on_tick)
    finally:
        lake.close()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        pickle.dump({"rows": rec.rows, "dereg": rec.dereg_blocks, "emission_off": rec.emission_off,
                     "blocks": rec.blocks, "record_every": every, "size_tao": a.size_tao,
                     "first_block": res.first_block, "last_block": res.last_block}, fh)
    print(f"{len(rec.rows)} rows, {len(rec.blocks)} ticks, {len(rec.dereg_blocks)} deregs "
          f"in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
