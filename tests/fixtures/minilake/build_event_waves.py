"""Build the event-wave fixture (WP10; DESIGN.md section 10.3 item 3): FULL snapshots around the two emission waves.

- 8,463,543 -> 8,463,544: the 2026-06-22 purge (54 subnets emission-disabled in one block);
- 9,029,888 -> 9,029,889: the 2026-09-09 re-enable (47 subnets emission-enabled in one block).

Each block is one FULL read with the WP1 reader (no hotkey panel: the wave tests read subnet-level fields only),
written to tests/fixtures/minilake/waves/lake (+ state.sqlite) through the WP3 lake. Read-only JSON-RPC, the reader's
3 req/s token bucket, one public archive endpoint (default OnFinality; --endpoint to change). About 20 calls.

Usage (from the repo root):
  .venv/Scripts/python.exe tests/fixtures/minilake/build_event_waves.py [--endpoint URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "waves"
LAKE_DIR = ROOT / "lake"
STATE_DB = ROOT / "state.sqlite"
BLOCKS = (8_463_543, 8_463_544, 9_029_888, 9_029_889)
DEFAULT_ENDPOINT = "https://bittensor-finney.api.onfinality.io/public"


async def collect(endpoint: str) -> int:
    from taotrader.chain.reader import JsonRpcChainReader
    from taotrader.chain.rpc import RpcPool
    from taotrader.core.state import ReadPlan
    from taotrader.core.units import Block
    from taotrader.data.collector import finalize_snapshot
    from taotrader.data.lake import Lake
    from taotrader.ops.config_load import load_run_config

    cfg = load_run_config()
    rpc = replace(cfg.rpc, archive_endpoints=(endpoint,), keys_per_call=2_500)
    assert rpc.rate_per_s <= 3.0
    pool = RpcPool.from_cfg(rpc, keyed_archive_url=None)
    reader = JsonRpcChainReader(pool, keys_per_call=2_500, max_concurrency=rpc.max_concurrency, provider_check_every=None)
    LAKE_DIR.mkdir(parents=True, exist_ok=True)
    lake = Lake(LAKE_DIR, STATE_DB)
    try:
        have = {int(r.block) for r in lake.snapshot_refs()}
        snaps = []
        for b in BLOCKS:
            if b in have:
                continue
            h = await reader.block_hash(Block(b))
            snaps.append(finalize_snapshot(await reader.snapshot(Block(b), h, ReadPlan.FULL, None, ())))
        if snaps:
            lake.write_snapshots(snaps)
        refs = lake.snapshot_refs()
        out = {"blocks": sorted(int(r.block) for r in refs), "manifest_hash": lake.manifest_hash()}
    finally:
        lake.close()
        await pool.aclose()
    con = sqlite3.connect(STATE_DB)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    print(json.dumps(out, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    a = ap.parse_args(argv)
    return asyncio.run(collect(a.endpoint))


if __name__ == "__main__":
    sys.exit(main())
