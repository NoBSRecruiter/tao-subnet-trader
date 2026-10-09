"""Network (read-only JSON-RPC, <= 2.5 req/s): the Gatekeeper on live archive data.

- the committed fixture's Added blocks are re-read from the archive (the fixture is real chain data);
- the most recent registration on chain: its minimal state at Q-1, Q, A-1, A is captured with the fixture's capture
  tool and the Gatekeeper reproduces lag = A - Q in [17, 25] and netuid == victim.

The full "last 10 registrations" check is WP10's (it has the WP1 reader and the WP4 collector in scope).
Run with: .venv/Scripts/python.exe -m pytest tests/features/test_gatekeeper_network.py -m network
"""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.features import gatekeeper as gk

pytestmark = pytest.mark.network
TOOL = Path(__file__).resolve().parent / "capture_gatekeeper_fixture.py"
MAX_NETUID = 144


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp5_capture_gatekeeper_fixture", TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cap(tool: ModuleType) -> Any:
    return tool.Capture(tool.Rpc(tool.ENDPOINT))


def test_fixture_added_blocks_match_the_archive(cap: Any, registrations: Any) -> None:
    for r in registrations[:2]:
        assert cap.find_added(r.queued_block, r.victim_netuid) == r.added_block


def _latest_registration(cap: Any) -> tuple[int, int, int] | None:
    """(Q, netuid, A) of the most recent registration that has been added, from LastRateLimitedBlock and
    NetworkRegisteredAt at the finalized head."""
    head_hash = cap.rpc.call("chain_getFinalizedHead", [])
    spec = cap.spec(head_hash)
    row_last = it.GLOBAL["last_reg_block"]
    keys = [row_last.key()] + [it.SUBNET["reg_at"].key(netuid=n) for n in range(1, MAX_NETUID + 1)]
    vals = cap.query(keys, head_hash)
    last = int(cap.decode(row_last, vals[row_last.key()], spec))
    reg_ats = {n: int(cap.decode(it.SUBNET["reg_at"], vals[it.SUBNET["reg_at"].key(netuid=n)], spec))
               for n in range(1, MAX_NETUID + 1)}
    newest = max(reg_ats.items(), key=lambda kv: kv[1])
    n, a = newest
    if a == 0 or a < last:
        return None                                    # the latest registration is still queued
    if a > last:                                       # runtime writes LastRateLimitedBlock at Q
        return last, n, a
    lo, hi = a - 60, a                                 # runtime writes it at NetworkAdded: search the removal block
    while hi - lo > 1:
        mid = (lo + hi) // 2
        added, reg = cap.reg_at_of(n, mid)
        if added and reg != a:
            lo = mid
        else:
            hi = mid
    return hi, n, a


def test_latest_registration_lag_and_victim(cap: Any, tool: ModuleType,
                                            registration_loader: Callable[[dict[str, Any]], Any]) -> None:
    found = _latest_registration(cap)
    if found is None:
        pytest.skip("the latest registration has not been added yet")
    q, n, a = found
    snaps = [cap.snapshot(b, n) for b in (q - 1, q, a - 1, a)]
    doc = {"subnet_fields": list(tool.SUBNET_FIELDS), "registrations": [
        {"queued_block": q, "victim_netuid": n, "added_block": a, "lag": a - q, "snapshots": snaps}]}
    (reg,) = registration_loader(doc)
    g = gk.Gatekeeper()
    prev = None
    created: list[gk.LaunchRecord] = []
    for s in reg.snapshots:
        created.extend(g.observe(prev, s))
        prev = s
    (rec,) = created
    assert rec.lag_blocks == a - q and 17 <= a - q <= 25
    assert rec.netuid_ok and rec.victim is not None and int(rec.victim.netuid) == n
    assert not rec.seed_anomaly
