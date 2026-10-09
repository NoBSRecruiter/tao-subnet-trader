"""Build (or resume) the SN116 prune-replay fixture (WP10; DESIGN.md section 10.3 item 2).

The SN116 prune at P = 9,210,610 (rank 2 -> 1 in under an hour, 0.3% gap; brief section 4.4) as a small lake:
- the WP4 collector's c60 schedule over [P - 3,600, P + 120] (60-block FULL snapshots, the tracked hotkey panel from
  the membership point at 9,206,400, escrow on the 360-block grid) with the runtime prune target probed at every
  600-grid block (`calib` rows, probe "prune_target") so the local ladder target can be compared with
  SubnetInfoRuntimeApi_get_subnet_to_prune at sampled blocks;
- the per-block refinement window [P - 120, P + 25] (the WP1 reader's per-block pull: REFINED snapshots in series
  "refine", FULL every 60 blocks and HEAD in between, written in resumable 10-block groups) covering the last 2 stride
  snapshots before P, the removal block and the re-registration (Added at P + 22).

Both public archive endpoints are used (opentensor first, OnFinality as the fallback) at 1 req/s each, so this can
run next to build_minilake.py (<= 2 req/s on its endpoint) without any endpoint seeing more than 3 req/s.
Read-only JSON-RPC; nothing is signed or submitted.

Usage (from the repo root):
  .venv/Scripts/python.exe tests/fixtures/minilake/build_prune_window.py            # collect / resume
  .venv/Scripts/python.exe tests/fixtures/minilake/build_prune_window.py --digest   # write PRUNE_MANIFEST.json
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
ROOT = HERE / "prune116"
LAKE_DIR = ROOT / "lake"
STATE_DB = ROOT / "state.sqlite"
MANIFEST_JSON = HERE / "PRUNE_MANIFEST.json"
P = 9_210_610
C60_START = P - 3_600
C60_END = P + 120
PANEL_FROM = 9_206_400
WINDOW = (P - 120, P + 25)
ENDPOINTS = ("https://archive.chain.opentensor.ai:443", "https://bittensor-finney.api.onfinality.io/public")
RATE_PER_S = 1.0            # per endpoint; the mini-lake builder may use OnFinality at the same time (<= 3 req/s total)
GROUP = 10
KEYS_PER_CALL = 2_500
IN_FLIGHT = 3
RETRIES = 30                # the public archives answer -32004 (historical work budget): wait and resume
RETRY_WAIT_S = 90


async def collect(endpoints: tuple[str, ...] = ENDPOINTS) -> int:
    from taotrader.chain.reader import JsonRpcChainReader
    from taotrader.chain.rpc import RpcPool
    from taotrader.core.units import Block
    from taotrader.data.collector import (
        ChunkPlan,
        Collector,
        CollectorCfg,
        EscrowCache,
        finalize_snapshot,
        rebuild_tracker,
        schedule_c60,
    )
    from taotrader.data.lake import Lake
    from taotrader.data.refine import REFINE_SERIES, ReaderWindowSource
    from taotrader.data.store import LakeSnapshotStore
    from taotrader.ops.config_load import load_run_config

    cfg = load_run_config()
    rpc = replace(cfg.rpc, archive_endpoints=endpoints, keys_per_call=KEYS_PER_CALL, max_concurrency=IN_FLIGHT,
                  rate_per_s=RATE_PER_S, burst=1)
    assert rpc.rate_per_s <= 3.0, "public endpoints: stay at or below 3 req/s"
    pool = RpcPool.from_cfg(rpc, keyed_archive_url=None)
    reader = JsonRpcChainReader(pool, keys_per_call=KEYS_PER_CALL, max_concurrency=IN_FLIGHT, provider_check_every=None)
    src = ReaderWindowSource(reader)
    LAKE_DIR.mkdir(parents=True, exist_ok=True)
    lake = Lake(LAKE_DIR, STATE_DB)
    log = logging.getLogger("prune116")

    def progress(p: ChunkPlan, msg: str) -> None:
        log.info("%s [%d..%d]: %s", p.label, p.blocks[0], p.blocks[-1], msg)

    ccfg = CollectorCfg(panel_from=PANEL_FROM, chunk_blocks=1_200, snapshot_concurrency=4, prune_every_blocks=600,
                        price_every_blocks=1_200, amm_every_blocks=3_600)
    try:
        col = Collector(src, lake, ccfg, on_chunk=progress)
        try:
            summary = await col.run([schedule_c60(C60_END, start=C60_START)])
        finally:
            col.close()
        log.info("c60: %s snapshots, %s failed chunks", summary.snapshots, summary.chunks_failed)
        if summary.chunks_failed:
            return 1
        # the per-block window, written in small groups so progress survives the archives' work budgets; a group
        # continues from the stored previous block (HEAD reads carry FULL-only fields from it), so the content does
        # not depend on where an interrupted run stopped
        tracker = rebuild_tracker(lake, ccfg, WINDOW[1])
        escrow = EscrowCache(src, ccfg.escrow_grid)
        for g0 in range(WINDOW[0], WINDOW[1] + 1, GROUP):
            lake.refresh()
            have = {int(r.block) for r in lake.snapshot_refs() if r.series == REFINE_SERIES}
            blocks = [b for b in range(g0, min(g0 + GROUP, WINDOW[1] + 1)) if b not in have]
            if not blocks:
                continue
            prev = None
            if blocks[0] - 1 in have:
                store = LakeSnapshotStore(lake, clock=blocks[0] - 1)
                prev = store.at(Block(blocks[0] - 1))

            def tracked(p_: object, first: int = blocks[0]) -> object:
                pb = getattr(p_, "block", None)
                return tracker.tracked_at(first if pb is None else int(pb) + 1)

            snaps = [x async for x in src.reader.pull(blocks, tracked, prev=prev)]  # type: ignore[arg-type]
            await escrow.fill(int(x.block) for x in snaps)
            finals = []
            eb: dict[tuple[int, int], int] = {}
            for x in snaps:
                srcb, esc = escrow.at(int(x.block))
                if srcb is not None and esc is not None:
                    eb.update({(int(x.block), int(sn.key.netuid)): srcb for sn in x.subnets})
                finals.append(finalize_snapshot(x, escrow=esc, refined=True))
            lake.write_snapshots(finals, series=REFINE_SERIES, escrow_block=eb)
            log.info("refined %d..%d: %d snapshots", blocks[0], blocks[-1], len(finals))
        return 0
    finally:
        lake.close()
        await pool.aclose()


def write_digest() -> int:
    from taotrader.data.lake import Lake

    lake = Lake(LAKE_DIR, STATE_DB)
    try:
        refs = lake.snapshot_refs()
        out = {"prune_block": P, "c60": [C60_START, C60_END], "window": list(WINDOW), "panel_from": PANEL_FROM,
               "snapshots": len(refs), "refined": sum(1 for r in refs if r.series == "refine"),
               "manifest_hash": lake.manifest_hash(), "chunks": dict(sorted((m.path, m.sha256) for m in lake.manifest()))}
    finally:
        lake.close()
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
    import time

    from taotrader.chain.rpc import RpcError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--endpoint", action="append", default=None, help="restrict to these archive endpoints")
    a = ap.parse_args(argv)
    if a.digest:
        return write_digest()
    for attempt in range(RETRIES):                 # resumable: every retry continues where the last one stopped
        try:
            if asyncio.run(collect(tuple(a.endpoint) if a.endpoint else ENDPOINTS)) == 0:
                return 0
        except RpcError as e:
            logging.getLogger("prune116").warning("attempt %d: %s; retrying in %d s", attempt, e, RETRY_WAIT_S)
        time.sleep(RETRY_WAIT_S)
    return 1


if __name__ == "__main__":
    sys.exit(main())
