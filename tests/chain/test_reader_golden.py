"""chain.reader on WP0's golden captures (real chain bytes, specs 348 and 441-475), served by the fake node.

- Era B (7,000,020, spec 348): the CP_V3_VIRTUAL pool built from AlphaSqrtPrice / CurrentLiquidity reproduces every
  captured sim_swap (buys and sells of 1/10/100 TAO on SN1/SN19/SN64) within 3e-7 (exactly, in practice), and the spot
  matches current_alpha_price.
- Emission-parity pairs (40 blocks, specs 441-473) and the SN51 pair (spec 475): every captured per-subnet and global
  value decodes into the snapshot exactly; membership equals NetworksAdded; block emission equals the runtime API.
"""
from __future__ import annotations

import glob
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain import runtime_api as rt
from taotrader.chain.hashing import from_hex
from taotrader.chain.metadata import SpecLayouts
from taotrader.core.state import ChainSnapshot, PoolKind, Quality, ReadPlan
from taotrader.core.units import Block, BlockHash

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
PARITY = sorted(glob.glob(str(GOLDEN / "emission_parity" / "b*.json")))
LAYOUTS = SpecLayouts()

# snapshot attribute for each captured SUBNET row (pool fields are checked separately)
ATTR = {"alpha_out": "alpha_out", "protocol_alpha": "protocol_alpha", "moving_price": "moving_price",
        "root_prop": "root_prop", "miner_burned": "miner_burned", "emission_enabled": "emission_enabled",
        "subtoken_enabled": "subtoken_enabled", "reg_allowed": "reg_allowed", "first_emission_block": "first_emission_block",
        "tao_in_emission": "tao_in_emission", "excess_tao": "excess_tao", "alpha_out_emission": "alpha_out_emission",
        "alpha_in_emission": "alpha_in_emission", "reservoir_tao": "reservoir_tao", "reservoir_alpha": "reservoir_alpha"}
POOL = {"tao": "tao", "alpha_in": "alpha", "w_quote_e18": "w_quote_e18"}
GLOB = {"moving_alpha": "moving_alpha", "gate_bar": "gate_bar", "gate_rank": "gate_rank", "gate_exponent": "gate_exponent",
        "tao_weight": "tao_weight", "root_tao": "root_tao", "owner_cut_u16": "owner_cut_u16", "subnet_limit": "subnet_limit",
        "immunity_period": "immunity_period", "network_rate_limit": "network_rate_limit",
        "last_lock_cost": "last_lock_cost", "min_lock_cost": "min_lock_cost",
        "lock_reduction_interval": "lock_reduction_interval", "tao_in_refund_block": "tao_in_refund_block",
        "total_issuance": "total_issuance", "cleanup_queue_len": "cleanup_queue_len", "last_reg_block": "last_reg_block",
        "safe_mode_until": "safe_mode_until"}


def expected(row: it.Row, spec: int, raw_hex: str | None) -> Any:
    """What the reader must produce for a captured value: the decoded bytes, else that spec's absent-key value."""
    if raw_hex is not None:
        return row.decode(from_hex(raw_hex))
    e = LAYOUTS.exact(spec).entry(row)  # type: ignore[union-attr]
    if e is None:
        return row.fallback
    if e.modifier == "Optional":
        return None
    return row.decode(e.default_bytes)


def read_full(arun: Any, make_reader: Any, chain: Any, snap: dict[str, Any], **kw: Any) -> ChainSnapshot:
    reader = make_reader(chain, **kw)
    return arun(reader.snapshot(Block(int(snap["block"])), BlockHash(snap["block_hash"]), ReadPlan.FULL, None, ()))  # type: ignore[no-any-return]


def check_against_capture(out: ChainSnapshot, snap: dict[str, Any]) -> int:
    spec = int(snap["spec_version"])
    assert out.glob.spec_version == spec and LAYOUTS.accepted(spec)
    by = snap["storage_by_netuid"]
    added = [n for n, v in enumerate(by["SubtensorModule.NetworksAdded"]["values"]) if n and v is not None
             and from_hex(v) == b"\x01"]
    assert [int(s.key.netuid) for s in out.subnets] == added
    rows = {r.name: r for r in it.SUBNET_ROWS}
    checked = 0
    for item, d in by.items():
        row = rows[item]
        for s in out.subnets:
            n = int(s.key.netuid)
            want = expected(row, spec, d["values"][n])
            if row.field == "reg_at":
                got: Any = int(s.key.reg_at)
            elif row.field in POOL:
                got = getattr(s.pool, POOL[row.field])
            elif row.field in ATTR:
                got = getattr(s, ATTR[row.field])
            else:
                continue
            assert got == want, (snap["block"], item, n, got, want)
            checked += 1
    globals_by_item = {(r.pallet, r.item, r.fixed_key): r for r in it.GLOBAL_ROWS}
    for e in snap["storage"]:
        pallet, item = e["item"].split(".")
        fixed = b"\x02" if item == "LastRateLimitedBlock" else (b"\x00\x00" if item == "SubnetTAO" else b"")
        grow = globals_by_item.get((pallet, item, fixed))
        if grow is None or grow.field not in GLOB:
            continue
        if item == "SubnetTAO" and e["args"] != [0]:
            continue
        assert getattr(out.glob, GLOB[grow.field]) == expected(grow, spec, e["value"]), (snap["block"], e["item"])
        checked += 1
    for r in snap.get("runtime_api", []):
        if r["method"] == rt.M_BLOCK_EMISSION:
            assert out.glob.block_emission == rt.dec_u64(r["result"])
    return checked


@pytest.mark.parametrize("path", PARITY, ids=[Path(p).stem for p in PARITY])
def test_emission_parity_blocks_decode_exactly(arun: Any, fake_chain_cls: Any, make_reader: Any, golden_node: Any,
                                               path: str) -> None:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(d["snapshots"]) == 2
    for snap in d["snapshots"]:
        chain = fake_chain_cls()
        golden_node(chain, snap)
        out = read_full(arun, make_reader, chain, snap)
        assert check_against_capture(out, snap) > 2_000
        assert all(s.pool.kind is PoolKind.BALANCER for s in out.subnets)
        assert not any(s.quality & Quality.DEFAULT_FILLED for s in out.subnets)
        assert out.digest


def test_sn51_pair_decodes_exactly(arun: Any, fake_chain_cls: Any, make_reader: Any, golden_node: Any,
                                   golden: Any) -> None:
    d = golden("sn51_emission_9240382")
    outs = []
    for snap in d["snapshots"]:
        chain = fake_chain_cls()
        golden_node(chain, snap)
        out = read_full(arun, make_reader, chain, snap)
        assert check_against_capture(out, snap) > 2_000
        outs.append(out)
    s51 = outs[1].by_netuid(51)
    assert s51 is not None
    exp = d["expected"]["SN51"]
    assert abs(Decimal(s51.tao_in_emission) / 10**9 - Decimal(str(exp["tao_in_per_block"]))) < Decimal("0.0001")
    assert abs(Decimal(s51.alpha_in_emission) / 10**9 - Decimal(str(exp["alpha_in_per_block"]))) < Decimal("0.001")
    total = sum(s.tao_in_emission + s.excess_tao for s in outs[1].subnets)
    assert abs(Decimal(total) / 10**9 - Decimal("0.5")) < Decimal("0.001")       # network sum = block emission


def test_era_b_virtual_pool_reproduces_sim_swap(arun: Any, fake_chain_cls: Any, make_reader: Any, golden_node: Any,
                                               golden: Any, enc: Any) -> None:
    d = golden("erab_7000020")
    snap = d["snapshots"][0]
    assert int(snap["spec_version"]) == 348
    chain = fake_chain_cls()
    h = golden_node(chain, snap)
    st = chain.storage[h]
    st["0x" + it.GLOBAL["system_number"].key().hex()] = "0x" + enc["le"](7_000_020, 4).hex()   # not in this capture
    st["0x" + it.GLOBAL["timestamp_ms"].key().hex()] = "0x" + enc["le"](1_747_000_000_000, 8).hex()
    out = read_full(arun, make_reader, chain, snap)
    assert [int(s.key.netuid) for s in out.subnets] == [1, 19, 64]
    tol = Decimal(d["expected"]["virtual_reserve_parity_rel"])
    n = 0
    for s in out.subnets:
        net = int(s.key.netuid)
        p = s.pool
        assert p.kind is PoolKind.CP_V3_VIRTUAL and p.fee_rate == 33 and p.w_quote_e18 == 5 * 10**17
        assert not s.quality & (Quality.TA_PRICE | Quality.DEFAULT_FILLED)
        for r in snap["runtime_api"]:
            if r["args"].get("netuid") != net or r.get("result") is None:
                continue
            if r["method"] == rt.M_PRICE:
                price = rt.dec_u64(r["result"])
                assert abs(int(p.spot_rao()) - price) <= max(1, price // 1_000_000)
                continue
            sim = rt.dec_sim_swap(r["result"])
            if r["method"] == rt.M_SIM_BUY:
                x = int(r["args"]["tao_rao"])
                fee = x * p.fee_rate // 65_535
                model, chain_out = p.px_alpha * (x - fee) // (p.px_tao + x - fee), sim.alpha_amount
                assert fee == sim.tao_fee
            elif r["method"] == rt.M_SIM_SELL:
                x = int(r["args"]["alpha_rao"])
                fee = x * p.fee_rate // 65_535
                model, chain_out = p.px_tao * (x - fee) // (p.px_alpha + x - fee), sim.tao_amount
                assert fee == sim.alpha_fee
            else:
                continue
            assert abs(Decimal(model) / Decimal(chain_out) - 1) <= tol, (net, r["method"], model, chain_out)
            n += 1
    assert n >= 18
    lay = LAYOUTS.exact(348)
    assert lay is not None and lay.entry("SubtensorModule.LastEpochBlock") is None
