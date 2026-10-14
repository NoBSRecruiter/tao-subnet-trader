"""Record the generation-keyed study panel once and pickle it (features only; no trading books by default).

Usage (from the repo root):
    # current-era panel (60-block lake, record every 300 blocks)
    .venv/Scripts/python.exe research/factor_scan/build_panel.py
    # full-history panel (h300 lake, 600-block pass complete)
    .venv/Scripts/python.exe research/factor_scan/build_panel.py --lake data_h300/lake --stride 600 \
        --record-every 600 --start 5140200 --out data/research/panel_h300.pkl
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
    ap.add_argument("--lake", default="data/lake")
    ap.add_argument("--stride", type=int, default=None, help="replay stride in blocks (plan default if unset)")
    ap.add_argument("--start", type=int, default=None, help="first replayed block (plan default if unset)")
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--books", default="", help="comma-separated books to run alongside (default: none)")
    ap.add_argument("--record-every", type=int, default=300, help="record panel rows only at blocks divisible by this")
    ap.add_argument("--size-tao", type=float, default=5.0, help="position size used for the round-trip cost column")
    ap.add_argument("--out", default="data/research/panel.pkl")
    a = ap.parse_args()
    plan = load_backtest_plan((DEFAULT_CONFIG, BOOKS_BACKTEST))
    lake = Lake(a.lake)
    rec = PanelRecorder(study_size_tao=a.size_tao)
    every = a.record_every

    def on_tick(runner, snap):  # type: ignore[no-untyped-def]
        if int(snap.block) % every == 0:
            rec(runner, snap)

    books = [b.strip() for b in a.books.split(",") if b.strip()]
    t0 = time.time()
    try:
        res = run_pass(plan, lake, books=books, start=a.start, end=a.end, stride=a.stride, on_tick=on_tick)
    finally:
        lake.close()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        pickle.dump({"rows": rec.rows, "dereg": rec.dereg_blocks, "emission_off": rec.emission_off,
                     "blocks": rec.blocks, "record_every": every, "size_tao": a.size_tao, "lake": a.lake,
                     "first_block": res.first_block, "last_block": res.last_block}, fh)
    print(f"{len(rec.rows)} rows, {len(rec.blocks)} ticks, {len(rec.dereg_blocks)} deregs "
          f"in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
