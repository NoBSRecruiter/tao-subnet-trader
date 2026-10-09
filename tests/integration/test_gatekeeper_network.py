"""Network acceptance test moved from WP5 to WP10 (DESIGN.md section 11 WP5/WP10): the Gatekeeper reproduces
Queued->Added lags of 17-25 blocks and netuid = victim on the LAST 10 REAL REGISTRATIONS.

The registrations are the last 10 of the brief section 4.4 prune log (data.refine.BRIEF_PRUNE_LOG: Q = the victim's
removal block, victim netuid), plus any newer registration found at the finalized head (the 10 newest are kept).
For each, WP5's read-only capture tool (tests/features/capture_gatekeeper_fixture.py, loaded with importlib) finds the
Added block A (NetworkRegisteredAt of the victim's netuid, NetworksAdded flip checked) and captures the minimal chain
state at Q-1, Q, A-1 and A; the production features.gatekeeper.Gatekeeper then observes the diffs Q-1 -> Q -> A-1 -> A.

Strictly read-only JSON-RPC (chain_getBlockHash, chain_getFinalizedHead, state_getRuntimeVersion,
state_queryStorageAt) at <= 2.5 req/s with backoff (the tool's own limiter; TAOTRADER_GK_MIN_INTERVAL_S=1.0 for
1 req/s); about 220 calls.
Endpoint: TAOTRADER_GK_ENDPOINT, default the public OnFinality archive.

Run with: .venv/Scripts/python.exe -m pytest tests/integration/test_gatekeeper_network.py -m network
"""
from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.core.state import ChainGlobals, ChainSnapshot, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import AlphaRao, Block, BlockHash, Rao
from taotrader.data.refine import BRIEF_PRUNE_LOG
from taotrader.features import gatekeeper as gk

pytestmark = pytest.mark.network
TOOL = Path(__file__).resolve().parents[1] / "features" / "capture_gatekeeper_fixture.py"
MAX_NETUID = 144
N_LAST = 10


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp10_capture_gatekeeper_fixture", TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # the tool's own limiter (module constant, 2.5 req/s); TAOTRADER_GK_MIN_INTERVAL_S slows it further when another
    # read-only job shares the endpoint (the total must stay <= 3 req/s)
    setattr(mod, "MIN_INTERVAL_S", max(float(os.environ.get("TAOTRADER_GK_MIN_INTERVAL_S", "0.4")), 0.4))  # noqa: B010
    return mod


@pytest.fixture(scope="module")
def cap(tool: ModuleType) -> Any:
    return tool.Capture(tool.Rpc(os.environ.get("TAOTRADER_GK_ENDPOINT", tool.ENDPOINT)))


def _newer_registration(cap: Any, after: int) -> tuple[int, int] | None:
    """(Q, victim netuid) of a registration newer than `after` at the finalized head, if any (WP5's head check)."""
    head_hash = cap.rpc.call("chain_getFinalizedHead", [])
    spec = cap.spec(head_hash)
    keys = [it.SUBNET["reg_at"].key(netuid=n) for n in range(1, MAX_NETUID + 1)]
    vals = cap.query(keys, head_hash)
    reg_ats = {n: int(cap.decode(it.SUBNET["reg_at"], vals[it.SUBNET["reg_at"].key(netuid=n)], spec))
               for n in range(1, MAX_NETUID + 1)}
    n, a = max(reg_ats.items(), key=lambda kv: kv[1])
    if a <= after + 25:
        return None
    lo, hi = a - 60, a                                   # the removal block Q: last block where the old generation is added
    while hi - lo > 1:
        mid = (lo + hi) // 2
        added, reg = cap.reg_at_of(n, mid)
        if added and reg != a:
            lo = mid
        else:
            hi = mid
    return hi, n


def _snapshot(doc: dict[str, Any], sf: list[str], make_globals: Callable[..., ChainGlobals],
              make_subnet: Callable[..., SubnetState]) -> ChainSnapshot:
    g = doc["globals"]
    subnets = []
    for n, row in sorted(doc["subnets"].items(), key=lambda kv: int(kv[0])):
        v = dict(zip(sf, row, strict=True))
        if not v["added"]:
            continue
        pool = PoolState(PoolKind.BALANCER, Rao(v["tao"]), AlphaRao(v["alpha_in"]), v["tao"], v["alpha_in"], v["w_quote_e18"],
                         v["fee_rate"] if v["fee_rate"] is not None else 33)
        fe = v["first_emission_block"]
        subnets.append(make_subnet(int(n), v["reg_at"], pool=pool, moving_price=Decimal(str(v["moving_price"])),
                                   first_emission_block=None if fe is None else Block(fe),
                                   emission_enabled=v["emission_enabled"], subtoken_enabled=v["subtoken_enabled"],
                                   reg_allowed=v["reg_allowed"], miner_burned=Decimal(str(v["miner_burned"])),
                                   last_epoch_block=Block(max(v["reg_at"], doc["block"] - 100))))
    glob = make_globals(spec_version=doc["spec_version"], total_issuance=Rao(g["total_issuance"]),
                        immunity_period=g["immunity_period"], subnet_limit=g["subnet_limit"],
                        network_rate_limit=g["network_rate_limit"], last_reg_block=Block(g["last_reg_block"]),
                        last_lock_cost=Rao(g["last_lock_cost"]), min_lock_cost=Rao(g["min_lock_cost"]),
                        lock_reduction_interval=g["lock_reduction_interval"], cleanup_queue_len=g["cleanup_queue_len"],
                        n_nonroot_networks=len(subnets))
    return ChainSnapshot(block=Block(doc["block"]), block_hash=BlockHash(doc["block_hash"]), timestamp_ms=g["timestamp_ms"],
                         plan=ReadPlan.FULL, glob=glob, subnets=tuple(subnets))


def test_last_10_registrations_lag_17_25_and_netuid_equals_victim(cap: Any, tool: ModuleType,
                                                                    make_globals: Callable[..., ChainGlobals],
                                                                    make_subnet: Callable[..., SubnetState]) -> None:
    regs = [(q, v) for q, v, _ in BRIEF_PRUNE_LOG]
    newer = _newer_registration(cap, regs[-1][0])
    if newer is not None:
        regs.append(newer)
    regs = regs[-N_LAST:]
    assert len(regs) == N_LAST
    sf = list(tool.SUBNET_FIELDS)
    results: list[tuple[int, int, int, int | None, bool | None, bool]] = []
    for q, victim in regs:
        a = cap.find_added(q, victim)
        snaps = [_snapshot(cap.snapshot(b, victim), sf, make_globals, make_subnet) for b in (q - 1, q, a - 1, a)]
        g = gk.Gatekeeper()
        prev: ChainSnapshot | None = None
        created: list[gk.LaunchRecord] = []
        for s in snaps:
            created.extend(g.observe(prev, s))
            prev = s
        assert len(created) == 1, (q, victim, created)
        rec = created[0]
        results.append((q, victim, a, rec.lag_blocks, rec.netuid_ok, rec.seed_anomaly))
        assert rec.lag_blocks == a - q, (q, victim, a, rec.lag_blocks)
        assert 17 <= a - q <= 25, (q, victim, a)
        assert rec.netuid_ok and rec.victim is not None and int(rec.victim.netuid) == victim
        assert not rec.seed_anomaly, (q, victim, rec.seed_failures)
    print("gatekeeper last-10:", results)
    assert cap.rpc.calls < 600
