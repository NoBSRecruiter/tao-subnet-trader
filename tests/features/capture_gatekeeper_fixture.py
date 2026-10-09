"""Capture tests/features/fixtures/gatekeeper_registrations.json: the offline Gatekeeper fixture of WP5.

For a few real registrations (queued block Q from the brief's section 4.4 prune log, victim netuid V) this reads the
Added block A (NetworkRegisteredAt[V] at Q + 40, checked against NetworksAdded at A - 1 / A) and then the minimal
chain state the Gatekeeper and the universe floor read at Q - 1, Q, A - 1 and A:

- per netuid 0..144: NetworksAdded, NetworkRegisteredAt, SubnetTAO, SubnetAlphaIn, SubnetMovingPrice,
  FirstEmissionBlockNumber, SubnetEmissionEnabled, SubtokenEnabled, NetworkRegistrationAllowed, MinerBurned,
  Swap.SwapBalancer, Swap.FeeRate;
- globals: LastRateLimitedBlock(NetworkLastRegistered), NetworkLastLockCost, DissolveCleanupQueue (length),
  NetworkImmunityPeriod, SubnetLimit, NetworkRateLimit, NetworkMinLockCost, NetworkLockReductionInterval,
  TotalIssuance, Timestamp.Now.

Values are decoded with the WP1 item registry (taotrader.chain.items); an absent key takes the runtime default of that
block's spec layout (taotrader.chain.metadata), exactly as the reader does. Raw bytes are not kept (the decoders are
WP1's and tested there); only netuids that are added (or are the victim) are stored.

Strictly read-only JSON-RPC (chain_getBlockHash, state_getRuntimeVersion, state_queryStorageAt), <= 2.5 req/s with
exponential backoff. Not collected by pytest (no test_ prefix). Usage:

    .venv/Scripts/python.exe tests/features/capture_gatekeeper_fixture.py [--endpoint URL]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from taotrader.chain import items as it
from taotrader.chain.hashing import from_hex, to_hex
from taotrader.chain.metadata import SpecLayouts

ENDPOINT = "https://bittensor-finney.api.onfinality.io/public"
OUT = Path(__file__).resolve().parent / "fixtures" / "gatekeeper_registrations.json"
MAX_NETUID = 144
KEYS_PER_CALL = 1_000
MIN_INTERVAL_S = 0.4                       # 2.5 req/s
ADDED_SEARCH_BLOCKS = 40

# (queued block Q, victim netuid V): the last three logged prunes (brief section 4.4) plus SN70 at 8,825,550, whose
# new generation's NetworkRegisteredAt (8,825,571) is also in the golden yield_inputs fixture.
REGISTRATIONS: tuple[tuple[int, int], ...] = ((9_210_610, 116), (9_155_237, 82), (9_111_229, 108), (8_825_550, 70))

SUBNET_FIELDS: tuple[str, ...] = ("added", "reg_at", "tao", "alpha_in", "moving_price", "first_emission_block",
                                  "emission_enabled", "subtoken_enabled", "reg_allowed", "miner_burned", "w_quote_e18",
                                  "fee_rate")
GLOBAL_FIELDS: tuple[str, ...] = ("last_reg_block", "last_lock_cost", "cleanup_queue_len", "immunity_period", "subnet_limit",
                                  "network_rate_limit", "min_lock_cost", "lock_reduction_interval", "total_issuance",
                                  "timestamp_ms")


class Rpc:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=60.0)
        self.last = 0.0
        self.calls = 0
        self.next_id = 1

    def call(self, method: str, params: list[Any]) -> Any:
        delay = 2.0
        for attempt in range(1, 8):
            wait = MIN_INTERVAL_S - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            body = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
            self.next_id += 1
            try:
                r = self.client.post(self.endpoint, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"RPC error {data['error']}")
                return data["result"]
            except (httpx.HTTPError, RuntimeError, ValueError) as e:
                print(f"  retry {attempt} {method}: {e}; sleeping {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise RuntimeError(f"{method} failed after retries")


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    return v


class Capture:
    def __init__(self, rpc: Rpc) -> None:
        self.rpc = rpc
        self.layouts = SpecLayouts()

    def block_hash(self, block: int) -> str:
        h = self.rpc.call("chain_getBlockHash", [block])
        if not isinstance(h, str):
            raise RuntimeError(f"no hash for block {block}")
        return h

    def spec(self, block_hash: str) -> int:
        rv = self.rpc.call("state_getRuntimeVersion", [block_hash])
        return int(rv["specVersion"])

    def query(self, keys: list[bytes], block_hash: str) -> dict[bytes, bytes | None]:
        out: dict[bytes, bytes | None] = dict.fromkeys(keys)
        for i in range(0, len(keys), KEYS_PER_CALL):
            chunk = [to_hex(k) for k in keys[i:i + KEYS_PER_CALL]]
            res = self.rpc.call("state_queryStorageAt", [chunk, block_hash])
            for change_set in res:
                for k_hex, v_hex in change_set["changes"]:
                    out[from_hex(k_hex)] = None if v_hex is None else from_hex(v_hex)
        return out

    def decode(self, row: it.Row, raw: bytes | None, spec: int) -> Any:
        if raw is not None:
            return row.decode(raw)
        layout = self.layouts.for_spec(spec)
        entry = layout.entry(row) if layout is not None else None
        if entry is None:
            return row.fallback
        if entry.modifier == "Optional":
            return None
        return row.decode(entry.default_bytes)

    def reg_at_of(self, netuid: int, block: int) -> tuple[bool, int]:
        h = self.block_hash(block)
        ka, kr = it.SUBNET["added"].key(netuid=netuid), it.SUBNET["reg_at"].key(netuid=netuid)
        vals = self.query([ka, kr], h)
        spec = self.spec(h)
        return bool(self.decode(it.SUBNET["added"], vals[ka], spec)), int(self.decode(it.SUBNET["reg_at"], vals[kr], spec))

    def find_added(self, q: int, victim: int) -> int:
        added, reg_at = self.reg_at_of(victim, q + ADDED_SEARCH_BLOCKS)
        if not added or not q < reg_at <= q + ADDED_SEARCH_BLOCKS:
            raise RuntimeError(f"netuid {victim}: no new generation within {ADDED_SEARCH_BLOCKS} blocks of {q}")
        before, _ = self.reg_at_of(victim, reg_at - 1)
        at, reg2 = self.reg_at_of(victim, reg_at)
        if before or not at or reg2 != reg_at:
            raise RuntimeError(f"netuid {victim}: NetworksAdded does not flip at NetworkRegisteredAt {reg_at}")
        return reg_at

    def snapshot(self, block: int, victim: int) -> dict[str, Any]:
        h = self.block_hash(block)
        spec = self.spec(h)
        sub_rows = [it.SUBNET[f] for f in SUBNET_FIELDS]
        glob_rows = [it.GLOBAL[f] for f in GLOBAL_FIELDS]
        keys: list[bytes] = [r.key() for r in glob_rows]
        for n in range(MAX_NETUID + 1):
            keys.extend(r.key(netuid=n) for r in sub_rows)
        vals = self.query(keys, h)
        glob = {f: _json_value(self.decode(r, vals[r.key()], spec)) for f, r in zip(GLOBAL_FIELDS, glob_rows, strict=True)}
        subnets: dict[str, list[Any]] = {}
        for n in range(1, MAX_NETUID + 1):
            row = [_json_value(self.decode(r, vals[r.key(netuid=n)], spec)) for r in sub_rows]
            if row[0] or n == victim:
                subnets[str(n)] = row
        return {"block": block, "block_hash": h, "spec_version": spec, "globals": glob, "subnets": subnets}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", default=ENDPOINT)
    args = ap.parse_args(argv)
    cap = Capture(Rpc(args.endpoint))
    regs: list[dict[str, Any]] = []
    for q, victim in REGISTRATIONS:
        a = cap.find_added(q, victim)
        print(f"Q {q} victim SN{victim}: Added at {a} (lag {a - q})")
        snaps = [cap.snapshot(b, victim) for b in (q - 1, q, a - 1, a)]
        regs.append({"queued_block": q, "victim_netuid": victim, "added_block": a, "lag": a - q, "snapshots": snaps})
    doc = {
        "fixture": "gatekeeper_registrations",
        "purpose": "Real registration sequences (Q-1, Q, A-1, A) for the WP5 Gatekeeper: Queued->Added lag and netuid = victim",
        "endpoint": args.endpoint,
        "captured_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rpc_calls": cap.rpc.calls,
        "subnet_fields": list(SUBNET_FIELDS),
        "global_fields": list(GLOBAL_FIELDS),
        "registrations": regs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=None, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {cap.rpc.calls} RPC calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
