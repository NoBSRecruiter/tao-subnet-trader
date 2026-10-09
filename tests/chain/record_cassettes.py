"""Record the WP1 reader cassettes (tests/fixtures/cassettes) from a public archive. Read-only JSON-RPC, <= 3 req/s.

    .venv/Scripts/python.exe tests/chain/record_cassettes.py [--endpoint URL]

Writes:
- full_9240388.jsonl / head_9240389.jsonl: every JSON-RPC exchange of a FULL snapshot at 9,240,388 (the SN92 golden
  block; tracked = the SN92 dividend recipients, prune cross-check on) and of the HEAD snapshot that follows it;
- reader_9240388.json: the expected digests and a few decoded values, so the replay test can assert digest equality.

The replay test (test_cassette.py) cross-checks the decoded values against WP0's independently captured golden fixtures
at the same block hash, so a recording made by a buggy reader cannot pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from taotrader.chain.reader import JsonRpcChainReader
from taotrader.chain.rpc import HttpTransport, RecordingTransport, Role, RpcPool
from taotrader.core.state import ReadPlan
from taotrader.core.units import Block, BlockHash, Hotkey, NetUid, SubnetKey

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"
BLOCK = 9_240_388
HASH = BlockHash("0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524")


async def record(endpoint: str) -> dict[str, object]:
    rec_full = RecordingTransport(HttpTransport(endpoint))
    pool = RpcPool([RpcPool.make_endpoint(endpoint, Role.ARCHIVE, transport=rec_full, rate_per_s=2.5, burst=2,
                                          label="archive")])
    reader = JsonRpcChainReader(pool, prune_check_every=1, provider_check_every=None)
    hks = await reader.dividend_keys(NetUid(92), HASH)
    key92 = SubnetKey(NetUid(92), Block(8_352_006))
    tracked = [(key92, Hotkey(h)) for h in hks]
    full = await reader.snapshot(Block(BLOCK), HASH, ReadPlan.FULL, None, tracked)
    n_full = rec_full.dump(OUT / f"full_{BLOCK}.jsonl")
    rec_head = RecordingTransport(rec_full.inner)
    pool_head = RpcPool([RpcPool.make_endpoint(endpoint, Role.ARCHIVE, transport=rec_head, rate_per_s=2.5, burst=2,
                                               label="archive")])
    reader_head = JsonRpcChainReader(pool_head, prune_check_every=None, provider_check_every=None)
    h1 = await reader_head.block_hash(Block(BLOCK + 1))
    head = await reader_head.snapshot(Block(BLOCK + 1), h1, ReadPlan.HEAD, full, tracked)
    n_head = rec_head.dump(OUT / f"head_{BLOCK + 1}.jsonl")
    await pool.aclose()
    s92 = full.by_netuid(92)
    assert s92 is not None
    expected: dict[str, object] = {
        "block": BLOCK, "block_hash": HASH, "head_block": BLOCK + 1, "head_block_hash": h1,
        "full_digest": full.digest, "head_digest": head.digest, "records": {"full": n_full, "head": n_head},
        "tracked_hotkeys": list(hks), "n_subnets": len(full.subnets), "prune_target": full.glob.runtime_prune_target,
        "sn92": {"tao": s92.pool.tao, "alpha_in": s92.pool.alpha, "moving_price": str(s92.moving_price),
                 "owner_alpha": s92.owner_alpha},
    }
    (OUT / f"reader_{BLOCK}.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                                              newline="\n")
    return expected


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="https://bittensor-finney.api.onfinality.io/public")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(record(a.endpoint)), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
