"""Golden fixtures (tests/fixtures/golden, captured by tools/capture_golden.py): provenance, key layouts, and the
brief's verified vectors re-checked against the captured raw chain data (DESIGN.md sections 6.4, 10.1)."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import xxhash

from taotrader.core.fixed import EXACT

HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
Golden = Callable[[str], dict[str, Any]]


def twox128(b: bytes) -> bytes:
    return xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + xxhash.xxh64_intdigest(b, seed=1).to_bytes(8, "little")


HASHERS: dict[str, Callable[[bytes], bytes]] = {
    "Identity": lambda b: b,
    "Twox64Concat": lambda b: xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + b,
    "Blake2_128Concat": lambda b: hashlib.blake2b(b, digest_size=16).digest() + b,
}


def encode_arg(a: Any) -> bytes:
    if isinstance(a, int):
        return a.to_bytes(2, "little")                                   # netuid u16
    if isinstance(a, str) and a.startswith("0x"):
        return bytes.fromhex(a[2:])                                      # AccountId32
    return {"NetworkLastRegistered": b"\x02"}[a]                         # RateLimitKey variant index


def le(hexval: str | None) -> int:
    assert hexval is not None
    return int.from_bytes(bytes.fromhex(hexval[2:]), "little")


def storage(snap: dict[str, Any], item: str, *args: Any) -> str | None:
    for e in snap["storage"]:
        if e["item"] == item and e["args"] == list(args):
            return e["value"]                                            # type: ignore[no-any-return]
    if len(args) == 1 and item in snap.get("storage_by_netuid", {}):
        return snap["storage_by_netuid"][item]["values"][args[0]]        # type: ignore[no-any-return]
    raise KeyError((item, args))


def runtime(snap: dict[str, Any], method: str, **args: Any) -> str | None:
    for r in snap["runtime_api"]:
        if r["method"] == method and all(r["args"].get(k) == v for k, v in args.items()):
            return r["result"]                                           # type: ignore[no-any-return]
    raise KeyError(method)


def sim(hexval: str | None) -> list[int]:
    assert hexval is not None
    b = bytes.fromhex(hexval[2:])
    return [int.from_bytes(b[i:i + 8], "little") for i in range(0, len(b), 8)]


def all_fixtures(golden_dir: Path) -> list[Path]:
    return sorted(p for p in golden_dir.rglob("*.json") if p.name != "manifest.json")


# ------------------------------------------------------------------------------------------------- provenance
def test_manifest_and_readme_cover_every_fixture(golden_dir: Path) -> None:
    manifest = json.loads((golden_dir / "manifest.json").read_text(encoding="utf-8"))
    readme = (golden_dir / "README.md").read_text(encoding="utf-8")
    files = {e["file"] for e in manifest["fixtures"]}
    assert files == {p.relative_to(golden_dir).as_posix() for p in all_fixtures(golden_dir)}
    for e in manifest["fixtures"]:
        lf = (golden_dir / e["file"]).read_bytes().replace(bytes([13, 10]), bytes([10]))   # git autocrlf safe
        assert hashlib.sha256(lf).hexdigest() == e["sha256"], e["file"]
        assert e["file"] in readme
        for s in e["snapshots"]:
            assert HASH_RE.match(s["block_hash"]) and s["block_hash"] in readme
            assert isinstance(s["spec_version"], int) and s["spec_version"] > 0


def test_required_captures_exist(golden: Golden, golden_dir: Path) -> None:
    sn92 = golden("sn92_9240388")["snapshots"][0]
    assert sn92["block"] == 9_240_388
    assert golden("globals_9240878")["snapshots"][0]["block"] == 9_240_878
    assert {s["block"] for s in golden("erab_7000020")["snapshots"]} == {7_000_020}
    assert [s["block"] for s in golden("sn51_emission_9240382")["snapshots"]] == [9_240_381, 9_240_382]
    assert golden("sn1_quote_9240388")["snapshots"][0]["block"] == 9_240_388
    assert golden("yield_inputs_9240388")["snapshots"][0]["block"] == 9_240_388
    parity = sorted((golden_dir / "emission_parity").glob("b*.json"))
    assert len(parity) == 20
    for p in parity:
        pre, at = json.loads(p.read_text(encoding="utf-8"))["snapshots"]
        assert at["block"] >= 8_765_684 and pre["block"] == at["block"] - 1
        assert pre["spec_version"] == at["spec_version"]
        assert len(at["storage_by_netuid"]["SubtensorModule.SubnetTaoInEmission"]["values"]) == 145


def test_storage_keys_follow_the_documented_layout(golden_dir: Path) -> None:
    assert twox128(b"System").hex() == "26aa394eea5630e07c48ae0c9558cef7"
    n = 0
    for p in all_fixtures(golden_dir):
        for snap in json.loads(p.read_text(encoding="utf-8")).get("snapshots", []):
            for e in snap["storage"]:
                pallet, item = e["item"].split(".")
                key = twox128(pallet.encode()) + twox128(item.encode())
                key += b"".join(HASHERS[h](encode_arg(a)) for h, a in zip(e["hashers"], e["args"], strict=True))
                assert "0x" + key.hex() == e["key"], (p.name, e["item"], e["args"])
                n += 1
    assert n > 1_000


# ------------------------------------------------------------------------------------------------- decoders (6.4)
def test_fixed_point_decoder_vectors(golden: Golden) -> None:
    g = golden("globals_9240878")["snapshots"][0]
    raw_alpha = le(storage(g, "SubtensorModule.SubnetMovingAlpha"))
    assert raw_alpha == 1_288_490                                       # I96F32 raw -> 0.0003
    assert abs(EXACT.divide(Decimal(raw_alpha), Decimal(2**32)) - Decimal("0.0003")) < Decimal("1E-9")
    tao_weight = EXACT.divide(Decimal(le(storage(g, "SubtensorModule.TaoWeight"))), Decimal(2**64 - 1))
    assert abs(tao_weight - Decimal("0.18")) < Decimal("1E-9")
    gate_bar = EXACT.divide(Decimal(le(storage(g, "SubtensorModule.EmissionGateBar"))), Decimal(2**64))
    assert Decimal("0.001") < gate_bar < Decimal("0.05")                 # live 0.0082624 at the brief's block


# ------------------------------------------------------------------------------------------------- verified vectors
def test_sn92_ten_tao_buy_vector(golden: Golden) -> None:
    s = golden("sn92_9240388")["snapshots"][0]
    tao_amount, alpha_amount, tao_fee, *_ = sim(runtime(s, "SwapRuntimeApi_sim_swap_tao_for_alpha", tao_rao=10 * 10**9))
    assert (tao_amount, tao_fee) == (9_994_964_523, 5_035_477)
    assert abs(alpha_amount - 7_289_425_629_000) / 7_289_425_629_000 <= 1e-7
    _, alpha100, _, _, _, slip100 = sim(runtime(s, "SwapRuntimeApi_sim_swap_tao_for_alpha", tao_rao=100 * 10**9))
    assert round(alpha100 / 1e9, 2) == 63_351.67 and round(slip100 / 1e9, 2) == 10_820.70
    assert runtime(s, "SubnetInfoRuntimeApi_get_subnet_to_prune") == "0x015c00"   # Some(92): the live prune target


def test_sn92_pool_reproduces_the_quote_with_the_closed_form(golden: Golden) -> None:
    """Balancer at w ~ 0.5: alpha_out = x * (1 - (y / (y + dy))**(w_q / w_b)) within 1e-7 of the chain."""
    s = golden("sn92_9240388")["snapshots"][0]
    y, x = le(storage(s, "SubtensorModule.SubnetTAO", 92)), le(storage(s, "SubtensorModule.SubnetAlphaIn", 92))
    balancer = storage(s, "Swap.SwapBalancer", 92)
    assert balancer is not None
    q = le(balancer[:18])                                                # Perquintill: first 8 bytes
    dy = 10 * 10**9 - 10 * 10**9 * 33 // 65_535
    alpha = x * (1 - (y / (y + dy)) ** (q / (10**18 - q)))
    chain = sim(runtime(s, "SwapRuntimeApi_sim_swap_tao_for_alpha", tao_rao=10 * 10**9))[1]
    assert abs(alpha - chain) / chain < 1e-7


def test_sn1_one_tao_vector(golden: Golden) -> None:
    s = golden("sn1_quote_9240388")["snapshots"][0]
    tao_amount, alpha_amount, tao_fee, *_ = sim(runtime(s, "SwapRuntimeApi_sim_swap_tao_for_alpha", tao_rao=10**9))
    assert (tao_amount, tao_fee) == (999_496_453, 503_547)
    assert round(alpha_amount / 1e9, 2) == 152.29
    assert le(runtime(s, "SwapRuntimeApi_current_alpha_price", netuid=1)) == 6_562_800


def test_registration_cost_vector(golden: Golden) -> None:
    g = golden("globals_9240878")["snapshots"][0]
    assert round(le(runtime(g, "SubnetRegistrationRuntimeApi_get_network_registration_cost")) / 1e9, 2) == 962.89
    assert round(le(storage(g, "SubtensorModule.NetworkLastLockCost")) / 1e9, 2) == 653.02
    assert le(storage(g, "SubtensorModule.LastRateLimitedBlock", "NetworkLastRegistered")) == 9_210_610


def test_era_b_pools_have_v3_state(golden: Golden) -> None:
    s = golden("erab_7000020")["snapshots"][0]
    for n in (1, 19, 64):
        assert storage(s, "Swap.AlphaSqrtPrice", n) is not None and storage(s, "Swap.CurrentLiquidity", n) is not None
        res = runtime(s, "SwapRuntimeApi_sim_swap_tao_for_alpha", netuid=n, tao_rao=10**9)
        assert res is not None and len(bytes.fromhex(res[2:])) == 32     # 4 x u64 before spec 391


def test_sn70_index_jumps_at_the_epoch_drain(golden: Golden) -> None:
    fx = golden("sn70_index_9240222_9240582")
    a, b, c = fx["snapshots"][:3]
    hk = next(e["args"][0] for e in c["storage"] if e["item"] == "SubtensorModule.TotalHotkeyAlpha" and e["args"][0][2:6] == "56a9")
    assert le(storage(c, "SubtensorModule.LastEpochBlock", 70)) == 9_240_582

    def index(s: dict[str, Any]) -> Decimal:
        alpha = Decimal(le(storage(s, "SubtensorModule.TotalHotkeyAlpha", hk, 70)))
        raw = storage(s, "SubtensorModule.TotalHotkeySharesV2", hk, 70)
        assert raw is not None
        v2 = bytes.fromhex(raw[2:])
        shares = Decimal(f"{int.from_bytes(v2[:16], 'little')}E{int.from_bytes(v2[16:24], 'little', signed=True)}")
        return EXACT.divide(alpha, shares)

    ia, ib, ic = index(a), index(b), index(c)
    assert abs(ib / ia - 1) < Decimal("0.00001")                          # flat 9,240,222 -> 9,240,581
    assert abs((ic / ib - 1) * 100 - Decimal("0.0279")) < Decimal("0.0005")   # +0.0279% at the drain


def test_sn51_injection_split_inputs(golden: Golden) -> None:
    _pre, at = golden("sn51_emission_9240382")["snapshots"]
    tao_in = le(storage(at, "SubtensorModule.SubnetTaoInEmission", 51)) / 1e9
    excess = le(storage(at, "SubtensorModule.SubnetExcessTao", 51)) / 1e9
    assert abs(tao_in - 0.01397) < 0.0005 and abs(excess - 0.0515) < 0.002
    total = sum(le(v) for k in ("SubnetTaoInEmission", "SubnetExcessTao")
                for v in at["storage_by_netuid"][f"SubtensorModule.{k}"]["values"] if v is not None)
    assert abs(total / 1e9 - 0.5) < 0.01                                 # network sum ~ 0.5 TAO/block


@pytest.mark.parametrize("spec", [475])
def test_metadata_storage_summary_resolves_verify_items(golden: Golden, spec: int) -> None:
    md = golden(f"metadata_storage_spec{spec}")["pallets"]
    st = md["SubtensorModule"]
    assert st["SubnetOwnerHotkey"]["hashers"] == ["Identity"]
    assert st["OwnerCutEnabled"]["kind"] == "Map" and st["OwnerCutAutoLockEnabled"]["kind"] == "Map"
    assert st["Delegates"]["hashers"] == ["Blake2_128Concat"]
    assert st["ChildkeyTake"]["hashers"] == ["Blake2_128Concat", "Identity"]
    assert st["AlphaV2"]["hashers"] == ["Blake2_128Concat", "Blake2_128Concat", "Identity"]
    assert st["EmissionBarRank"]["value"] == "u16"
    assert "ShortsEnabled" not in st
    assert md["SafeMode"]["EnteredUntil"]["value"] == "u32"
    assert st["SubnetEpochConsensus"]["hashers"] == ["Identity"]


# ------------------------------------------------------------------------------------------------- network
@pytest.mark.network
def test_fixture_provenance_matches_the_archive(golden: Golden) -> None:
    """Re-reads block hashes and a few raw values from the public archive (read-only, ~0.5 s between calls)."""
    import time

    import httpx

    url = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "manifest.json")
                     .read_text(encoding="utf-8"))["endpoint"]
    with httpx.Client(timeout=60) as client:
        def rpc(method: str, params: list[Any]) -> Any:
            time.sleep(0.5)
            body = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()
            assert "error" not in body, body
            return body["result"]

        for name in ("sn92_9240388", "globals_9240878", "erab_7000020"):
            snap = golden(name)["snapshots"][0]
            assert rpc("chain_getBlockHash", [snap["block"]]) == snap["block_hash"]
            entries = [e for e in snap["storage"] if e["value"] is not None][:5]
            got = rpc("state_queryStorageAt", [[e["key"] for e in entries], snap["block_hash"]])
            values = {k.lower(): v for change_set in got for k, v in change_set["changes"]}
            for e in entries:
                assert values[e["key"]] == e["value"], (name, e["item"], e["args"])
