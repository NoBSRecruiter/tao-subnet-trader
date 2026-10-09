"""Cassette replay of a FULL snapshot (9,240,388) and the HEAD snapshot after it (DESIGN.md section 11 WP1 acceptance).

The cassettes (tests/fixtures/cassettes, recorded by tests/chain/record_cassettes.py) replay every JSON-RPC exchange;
the replayed snapshots must hash to the recorded digests. The decoded values are cross-checked against WP0's golden
fixtures, captured independently (plain httpx, tools/capture_golden.py) at the same block hash.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain import runtime_api as rt
from taotrader.chain.hashing import from_hex
from taotrader.chain.reader import JsonRpcChainReader, compare_prices
from taotrader.chain.rpc import CassetteTransport, Role, RpcPool
from taotrader.core.fixed import EXACT
from taotrader.core.state import ChainSnapshot, PoolKind, Quality, ReadPlan
from taotrader.core.units import Block, BlockHash, Hotkey, NetUid, SubnetKey

CASSETTES = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"
EXPECTED = json.loads((CASSETTES / "reader_9240388.json").read_text(encoding="utf-8"))
BLOCK = int(EXPECTED["block"])
HASH = BlockHash(EXPECTED["block_hash"])
KEY92 = SubnetKey(NetUid(92), Block(8_352_006))


def _reader(name: str, **kw: Any) -> tuple[JsonRpcChainReader, CassetteTransport]:
    t = CassetteTransport.load(CASSETTES / name)
    pool = RpcPool([RpcPool.make_endpoint(t.url, Role.ARCHIVE, transport=t, rate_per_s=1e6, burst=1_000_000,
                                          label="cassette")])
    return JsonRpcChainReader(pool, provider_check_every=None, **kw), t


async def _replay() -> tuple[ChainSnapshot, ChainSnapshot, dict[str, str | None], tuple[Hotkey, ...]]:
    raw: dict[str, str | None] = {}
    reader, t = _reader(f"full_{BLOCK}.jsonl", prune_check_every=1, on_raw=lambda _b, _h, r: raw.update(r))
    hks = await reader.dividend_keys(NetUid(92), HASH)
    tracked = [(KEY92, h) for h in hks]
    full = await reader.snapshot(Block(BLOCK), HASH, ReadPlan.FULL, None, tracked)
    assert t.misses == []
    reader_h, th = _reader(f"head_{BLOCK + 1}.jsonl")
    h1 = await reader_h.block_hash(Block(BLOCK + 1))
    head = await reader_h.snapshot(Block(BLOCK + 1), h1, ReadPlan.HEAD, full, tracked)
    assert th.misses == []
    return full, head, raw, hks


@pytest.fixture(scope="module")
def replay() -> tuple[ChainSnapshot, ChainSnapshot, dict[str, str | None], tuple[Hotkey, ...]]:
    return asyncio.run(_replay())


def test_replayed_digests_equal_recorded(replay: Any) -> None:
    full, head, _raw, hks = replay
    assert full.digest == EXPECTED["full_digest"]
    assert head.digest == EXPECTED["head_digest"]
    assert list(hks) == EXPECTED["tracked_hotkeys"]
    assert len(full.subnets) == EXPECTED["n_subnets"] == 128
    assert full.plan is ReadPlan.FULL and head.plan is ReadPlan.HEAD
    assert all(s.quality & Quality.CARRIED for s in head.subnets if head.by_netuid(int(s.key.netuid)) is not None
               and full.get(s.key) is not None)


def test_raw_values_equal_independent_golden_capture(replay: Any, golden: Any) -> None:
    """Every storage key both the reader and WP0's capture read at this hash has identical bytes."""
    _full, _head, raw, _hks = replay
    common = 0
    for name in ("sn92_9240388", "yield_inputs_9240388", "sn1_quote_9240388"):
        snap = golden(name)["snapshots"][0]
        assert snap["block_hash"] == HASH
        for e in snap["storage"]:
            if e["key"] in raw:
                assert raw[e["key"]] == e["value"], (name, e["item"], e["args"])
                common += 1
        for item, d in snap.get("storage_by_netuid", {}).items():
            pallet, name_ = item.split(".")
            row = next((r for r in it.SUBNET_ROWS if r.pallet == pallet and r.item == name_), None)
            if row is None:
                continue
            for n, v in enumerate(d["values"]):
                k = "0x" + row.key(netuid=n).hex()
                if k in raw:
                    assert raw[k] == v, (item, n)
                    common += 1
    assert common > 600


def test_membership_and_per_subnet_values_match_golden(replay: Any, golden: Any) -> None:
    full, _head, _raw, _hks = replay
    by = golden("sn92_9240388")["snapshots"][0]["storage_by_netuid"]
    added = [n for n, v in enumerate(by["SubtensorModule.NetworksAdded"]["values"]) if n != 0 and v == "0x01"]
    assert [int(s.key.netuid) for s in full.subnets] == added
    assert full.glob.n_nonroot_networks == len(added) == 128
    for s in full.subnets:
        n = int(s.key.netuid)
        assert s.key.reg_at == int.from_bytes(from_hex(by["SubtensorModule.NetworkRegisteredAt"]["values"][n]), "little")
        mp = by["SubtensorModule.SubnetMovingPrice"]["values"][n]
        assert s.moving_price == (Decimal(0) if mp is None else it.SUBNET["moving_price"].decode(from_hex(mp)))
        feb = by["SubtensorModule.FirstEmissionBlockNumber"]["values"][n]
        assert s.first_emission_block == (None if feb is None else int.from_bytes(from_hex(feb), "little"))
        assert s.pool.kind is PoolKind.BALANCER and not s.quality & Quality.DEFAULT_FILLED


def test_sn92_and_globals_match_golden(replay: Any, golden: Any) -> None:
    full, _head, _raw, hks = replay
    g = golden("sn92_9240388")["snapshots"][0]
    st = {(e["item"], json.dumps(e["args"])): e["value"] for e in g["storage"]}

    def val(item: str, *args: Any) -> bytes | None:
        v = st[(item, json.dumps(list(args)))]
        return None if v is None else from_hex(v)

    s = full.get(KEY92)
    assert s is not None
    assert s.pool.tao == int.from_bytes(val("SubtensorModule.SubnetTAO", 92) or b"", "little") == 587_199_047_950
    assert s.pool.alpha == int.from_bytes(val("SubtensorModule.SubnetAlphaIn", 92) or b"", "little")
    assert s.alpha_out == int.from_bytes(val("SubtensorModule.SubnetAlphaOut", 92) or b"", "little")
    assert s.owner_hotkey == "0x" + (val("SubtensorModule.SubnetOwnerHotkey", 92) or b"").hex()
    assert s.owner_coldkey == "0x" + (val("SubtensorModule.SubnetOwner", 92) or b"").hex()
    assert s.pool.spot_rao() == 1_348_210                          # == current_alpha_price(92) at this block
    # tracked panel: every SN92 dividend recipient, values as captured
    assert len(hks) == 52 and {str(h.hotkey) for h in s.hotkeys} == set(hks) | {str(s.owner_hotkey)}
    for h in s.hotkeys:
        ta = val("SubtensorModule.TotalHotkeyAlpha", str(h.hotkey), 92)
        assert h.total_alpha == (0 if ta is None else int.from_bytes(ta, "little"))
        dg = st.get(("SubtensorModule.Delegates", json.dumps([str(h.hotkey)])), "0x142e")
        assert h.take_u16 == int.from_bytes(from_hex(dg or "0x142e"), "little")
        assert h.earns == (str(h.hotkey) in hks)
    # owner position = AlphaV2 shares x owner-hotkey index (legacy Alpha absent at this block)
    assert val("SubtensorModule.Alpha", s.owner_hotkey, s.owner_coldkey, 92) is None
    shares = it.OWNER["owner_shares_v2"].decode(val("SubtensorModule.AlphaV2", s.owner_hotkey, s.owner_coldkey, 92) or b"")
    oh = s.hotkey(Hotkey(str(s.owner_hotkey)))
    assert oh is not None and s.owner_alpha == oh.value_of(shares) == EXPECTED["sn92"]["owner_alpha"]
    # globals
    gl = full.glob
    assert gl.gate_bar == it.GLOBAL["gate_bar"].decode(val("SubtensorModule.EmissionGateBar") or b"")
    assert abs(gl.gate_bar - Decimal("0.0082624")) < Decimal("1e-7")
    assert abs(gl.tao_weight - Decimal("0.18")) < Decimal("1e-15")
    assert abs(gl.moving_alpha - Decimal("0.0003")) < Decimal("1e-9")
    assert gl.moving_alpha == EXACT.divide(Decimal(1_288_490), Decimal(2**32))
    assert gl.owner_cut_u16 == 11_796 and val("SubtensorModule.SubnetOwnerCut") is None       # absent -> default
    assert gl.gate_exponent == 3 and gl.gate_rank == 32 and gl.subnet_limit == 128
    assert gl.total_issuance == int.from_bytes(val("SubtensorModule.TotalIssuance") or b"", "little")
    rts = {r["method"]: r for r in g["runtime_api"]}
    assert gl.block_emission == rt.dec_u64(rts[rt.M_BLOCK_EMISSION]["result"]) == 500_000_000
    assert gl.runtime_prune_target == rt.dec_prune_target(rts[rt.M_PRUNE]["result"]) == 92
    assert gl.spec_version == 475 and gl.shorts_enabled is False and gl.safe_mode_until is None


def test_offline_price_parity_at_golden_block(replay: Any, golden: Any) -> None:
    """Section 6.8 step 8 parity (<= 1e-6 relative) against the captured current_alpha_price_all at this hash."""
    full, _head, _raw, _hks = replay
    rts = {r["method"]: r for r in golden("sn92_9240388")["snapshots"][0]["runtime_api"]}
    prices = rt.dec_price_all(rts[rt.M_PRICE_ALL]["result"])
    pp = compare_prices(full, prices, rel_ppm=1)
    assert pp.n == 128 and pp.ok, pp.mismatches


def test_local_prune_ladder_matches_runtime(replay: Any) -> None:
    """Brief 4.3 rule on the reader's raw fields: lowest (moving_price, reg_at) among non-immune subnets."""
    full, _head, _raw, _hks = replay
    imm = full.glob.immunity_period
    cands = [s for s in full.subnets if int(full.block) >= int(s.key.reg_at) + imm]
    target = min(cands, key=lambda s: (s.moving_price, int(s.key.reg_at)))
    assert int(target.key.netuid) == full.glob.runtime_prune_target == 92
