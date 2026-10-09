"""Build (or resume) the committed mini-lake fixture (WP10; DESIGN.md sections 10.3 item 1 and 11 WP10).

The mini-lake is the WP4 collector's c60 schedule over the rank-32 gate era window 8,765,684 -> 8,830,000 (~1,072
snapshots at 60-block stride plus the membership points), collected from the PUBLIC archive endpoints only (no keyed
provider, read-only JSON-RPC, the reader's 3 req/s token bucket). Escrow is read on a daily grid (ESCROW_GRID;
collector_meta pins it) - the one deviation from the collector defaults, see ESCROW_GRID. The hotkey panel starts at
8,765,400 (the 600-grid membership block just before the window), so every snapshot of the window carries the tracked
panel.

Layout: tests/fixtures/minilake/lake/<table>/era=C/part-*.parquet + tests/fixtures/minilake/state.sqlite (manifest,
fetch_ledger, collector_meta). The run is resumable: re-running the command continues where it stopped and an
identical rewrite is a no-op. After it finishes, `python tests/fixtures/minilake/build_minilake.py --digest` prints
and writes MANIFEST.json (lake manifest hash + per-chunk sha256 + snapshot count) which tests check.

Usage (from the repo root):
  .venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py            # collect / resume
  .venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py --endpoint https://archive.chain.opentensor.ai:443
  # the public endpoints enforce historical-work budgets (-32004): two disjoint sub-ranges can run at the same time,
  # one per endpoint (chunk buckets are absolute, so 8,802,000 = 1,467 x 6,000 splits cleanly):
  ... build_minilake.py --endpoint https://bittensor-finney.api.onfinality.io/public --end 8801999
  ... build_minilake.py --endpoint https://archive.chain.opentensor.ai:443 --start 8802000
  .venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py --digest   # write MANIFEST.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAKE_DIR = HERE / "lake"
STATE_DB = HERE / "state.sqlite"
MANIFEST_JSON = HERE / "MANIFEST.json"
START = 8_765_684
END = 8_830_000
PANEL_FROM = 8_765_400
KEYS_PER_CALL = 2_500
IN_FLIGHT = 8              # requests in flight per endpoint; the pool's token bucket still caps the RATE
RATE_PER_S = 2.0           # <= 2 req/s, so build_prune_window.py (1 req/s per endpoint) can share an endpoint
SNAPSHOTS_IN_FLIGHT = 6
CHUNK_BLOCKS = 6_000       # smaller resumable chunks (progress survives an interrupted run)
ESCROW_GRID = 7_200        # escrow read once per day instead of every 360 blocks: one StakeInfo(escrow coldkey) runtime
                           # call costs ~160 s of public-archive work at these blocks (measured 2026-10-09), so the
                           # section 6.7 360-block grid alone would take ~8 h for this window. Escrow (basket alpha)
                           # moves slowly; the escrow_block column records the source block of every value.


async def collect(max_chunks: int | None, endpoint: str | None = None, start: int = START, end: int = END,
                  rate: float = RATE_PER_S) -> int:
    from taotrader.chain.reader import JsonRpcChainReader
    from taotrader.chain.rpc import RpcPool
    from taotrader.data.collector import ChunkPlan, Collector, CollectorCfg, ReaderSource, schedule_c60
    from taotrader.data.lake import Lake
    from taotrader.ops.config_load import load_run_config

    cfg = load_run_config()
    rpc = replace(cfg.rpc, keys_per_call=KEYS_PER_CALL, max_concurrency=IN_FLIGHT, rate_per_s=rate, burst=2)
    if endpoint is not None:                                       # one public archive endpoint only
        rpc = replace(rpc, archive_endpoints=(endpoint,))
    assert rpc.rate_per_s <= 3.0, "public endpoints: stay at or below 3 req/s"
    pool = RpcPool.from_cfg(rpc, keyed_archive_url=None)          # public endpoints only
    reader = JsonRpcChainReader(pool, keys_per_call=KEYS_PER_CALL, max_concurrency=rpc.max_concurrency,
                                provider_check_every=200)
    src = ReaderSource(reader)
    LAKE_DIR.mkdir(parents=True, exist_ok=True)
    lake = Lake(LAKE_DIR, STATE_DB)
    log = logging.getLogger("minilake")

    def progress(p: ChunkPlan, msg: str) -> None:
        log.info("%s [%d..%d]: %s", p.label, p.blocks[0], p.blocks[-1], msg)

    try:
        col = Collector(src, lake, CollectorCfg(panel_from=PANEL_FROM, chunk_blocks=CHUNK_BLOCKS,
                                                snapshot_concurrency=SNAPSHOTS_IN_FLIGHT, escrow_grid=ESCROW_GRID),
                        on_chunk=progress)
        try:
            summary = await col.run([schedule_c60(end, start=start)], max_chunks=max_chunks)
        finally:
            col.close()
        print(json.dumps({"snapshots": summary.snapshots, "invalid": summary.invalid, "chunks": summary.chunks_committed,
                          "skipped": summary.chunks_skipped, "failed": summary.chunks_failed,
                          "membership_points": summary.membership_points, "calib_max": summary.calib_max,
                          "calls": summary.calls}, indent=1))
        return 1 if summary.chunks_failed else 0
    finally:
        lake.close()
        await pool.aclose()


def write_digest() -> int:
    from taotrader.data.lake import Lake

    lake = Lake(LAKE_DIR, STATE_DB)
    try:
        refs = lake.snapshot_refs()
        chunks = {m.path: m.sha256 for m in lake.manifest()}
        out = {"start": START, "end": END, "panel_from": PANEL_FROM, "snapshots": len(refs),
               "first_block": min(r.block for r in refs), "last_block": max(r.block for r in refs),
               "manifest_hash": lake.manifest_hash(), "chunks": dict(sorted(chunks.items()))}
    finally:
        lake.close()
    # checkpoint the WAL so the committed state.sqlite is self-contained
    con = sqlite3.connect(STATE_DB)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    MANIFEST_JSON.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "chunks"}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true", help="write MANIFEST.json from the collected lake")
    ap.add_argument("--max-chunks", type=int, default=None)
    ap.add_argument("--endpoint", default=None, help="use only this public archive endpoint (default: [rpc] order)")
    ap.add_argument("--start", type=int, default=START, help="sub-range start (split the window over two endpoints)")
    ap.add_argument("--end", type=int, default=END, help="sub-range end (inclusive)")
    ap.add_argument("--retries", type=int, default=40, help="resumed runs after failed chunks")
    ap.add_argument("--rate", type=float, default=RATE_PER_S, help="req/s on the endpoint (<= 3.0)")
    ap.add_argument("--retry-wait", type=float, default=60.0, help="seconds between resumed runs")
    a = ap.parse_args(argv)
    if a.digest:
        return write_digest()
    if not START <= a.start <= a.end <= END:
        ap.error(f"sub-range must lie within [{START}, {END}]")
    import time

    from taotrader.chain.rpc import RpcError
    for attempt in range(a.retries):               # the public archives answer -32004 (historical work budget):
        try:                                       # failed chunks are retried on the next (resumable) run
            if asyncio.run(collect(a.max_chunks, a.endpoint, a.start, a.end, min(a.rate, 3.0))) == 0:
                return 0
        except RpcError as e:
            logging.getLogger("minilake").warning("attempt %d: %s", attempt, e)
        time.sleep(a.retry_wait)
    return 1


if __name__ == "__main__":
    sys.exit(main())
