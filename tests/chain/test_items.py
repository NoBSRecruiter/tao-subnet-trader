"""chain.items: the storage registry against independent metadata captures (DESIGN.md sections 6.5 and 13 Q1/Q13/Q20).

The WP0 golden `metadata_storage_spec{348,441,475}.json` summaries were decoded by tools/capture_golden.py with scalecodec,
independently of chain/metadata.py, so they cross-check the registry rows (hashers, query kind, value types).
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from taotrader.chain import hashing as h
from taotrader.chain import items as it
from taotrader.chain import scale as sc
from taotrader.chain.items import Query, Scope
from taotrader.core.errors import DecodeError
from taotrader.core.state import ChainGlobals, SubnetState

SPECS = (348, 441, 475)


def _summary(golden: Any, spec: int) -> dict[str, dict[str, Any]]:
    d = golden(f"metadata_storage_spec{spec}")
    out: dict[str, dict[str, Any]] = {}
    for pallet, items in d["pallets"].items():
        for item, e in items.items():
            out[f"{pallet}.{item}"] = e
    return out


def test_fields_are_unique_and_map_to_core_types() -> None:
    subnet_fields = {f.name for f in dataclasses.fields(SubnetState)}
    glob_fields = {f.name for f in dataclasses.fields(ChainGlobals)}
    internal = {it.NETWORKS_ADDED, "reg_at", "tao", "alpha_in", "w_quote_e18", "fee_rate", "v3_sqrt_price", "v3_liquidity",
                "last_epoch_block_legacy"}
    for row in it.SUBNET_ROWS:
        assert row.field in subnet_fields or row.field in internal, row.field
    internal_g = {"gate_quantile", "last_reg_block_legacy", "nominator_min_factor", "balances_total_issuance",
                  "timestamp_ms", "system_number", "safe_mode_until"}
    for row in it.GLOBAL_ROWS:
        assert row.field in glob_fields or row.field in internal_g, row.field
    names = [(r.name, r.scope, r.fixed_key) for r in it.ALL_ROWS]
    assert len(names) == len(set(names))


def test_shorts_enabled_is_watched_not_registered() -> None:
    assert all(r.item != "ShortsEnabled" for r in it.ALL_ROWS)
    assert "SubtensorModule.ShortsEnabled" in it.WATCH_ITEMS
    assert "SubtensorModule.ShortsEnabled" in it.item_names()


@pytest.mark.parametrize("spec", SPECS)
def test_registry_hashers_and_query_kind_match_metadata(golden: Any, spec: int) -> None:
    md = _summary(golden, spec)
    checked = 0
    for row in it.ALL_ROWS:
        e = md.get(row.name)
        if e is None:
            assert not row.required or row.name == "SubtensorModule.TotalIssuance", (spec, row.name)
            continue
        assert tuple(e.get("hashers") or ()) == tuple(x.value for x in row.hashers), (spec, row.name)
        assert e["modifier"] == row.query.value, (spec, row.name)
        checked += 1
    assert checked >= 50


@pytest.mark.parametrize("spec", SPECS)
def test_registry_value_types_are_accepted_by_decoders(golden: Any, spec: int) -> None:
    md = _summary(golden, spec)
    for row in it.ALL_ROWS:
        e = md.get(row.name)
        if e is None:
            continue
        assert e["value"] in row.decoder.types, (spec, row.name, e["value"], row.decoder.types)


@pytest.mark.parametrize("spec", SPECS)
def test_registry_defaults_decode(golden: Any, spec: int) -> None:
    """Every ValueQuery default of every registered item decodes with its row (WP0 stores defaults as hex; scalecodec
    shortens all-zero AccountId defaults, which are compared by value instead)."""
    md = _summary(golden, spec)
    for row in it.ALL_ROWS:
        e = md.get(row.name)
        if e is None or e["modifier"] != "Default":
            continue
        raw = h.from_hex(e["default"])
        if row.decoder is sc.ACCOUNT and len(raw) != 32:
            assert set(raw) <= {0}
            continue
        row.decode(raw)


def test_verify_items_against_spec475(golden: Any) -> None:
    """Section 13 Q1 / Q13 / Q20 resolutions (spec 475, block 9,240,388)."""
    md = _summary(golden, 475)
    assert md["SubtensorModule.SubnetOwnerHotkey"]["hashers"] == ["Identity"]
    assert md["SubtensorModule.SubnetOwnerHotkey"]["value"] == "AccountId32"
    assert md["SubtensorModule.OwnerCutEnabled"]["kind"] == "Map" and md["SubtensorModule.OwnerCutEnabled"]["default"] == "0x01"
    assert md["SubtensorModule.OwnerCutAutoLockEnabled"]["kind"] == "Map"
    assert md["SubtensorModule.OwnerCutAutoLockEnabled"]["default"] == "0x00"
    assert md["SubtensorModule.SubnetOwnerCut"]["kind"] == "Plain" and md["SubtensorModule.SubnetOwnerCut"]["default"] == "0x142e"
    assert md["SubtensorModule.Delegates"]["hashers"] == ["Blake2_128Concat"]
    assert md["SubtensorModule.ChildkeyTake"]["hashers"] == ["Blake2_128Concat", "Identity"]
    assert md["SubtensorModule.AlphaV2"]["hashers"] == ["Blake2_128Concat", "Blake2_128Concat", "Identity"]
    assert "SubtensorModule.Alpha" not in md and "SubtensorModule.TotalHotkeyShares" not in md
    assert md["SubtensorModule.EmissionBarRank"]["value"] == "u16"
    assert md["SubtensorModule.EmissionGateExponent"]["value"] == "FixedU128<frac_bits=64>"
    assert md["SafeMode.EnteredUntil"]["modifier"] == "Optional" and md["SafeMode.EnteredUntil"]["value"] == "u32"
    assert "SubtensorModule.ShortsEnabled" not in md
    assert md["SubtensorModule.TotalIssuance"]["value"] == "TaoBalance"
    assert md["SubtensorModule.DissolveCleanupQueue"]["value"] == "Vec<NetUid>"
    # Q20: per-subnet consensus mode
    ec = md["SubtensorModule.SubnetEpochConsensus"]
    assert (ec["hashers"], ec["key"], ec["value"], ec["default"]) == (["Identity"], "NetUid", "EpochConsensus", "0x00")
    variants = golden("metadata_storage_spec475")["types"]["pallet_subtensor::pallet::EpochConsensus"]
    assert [(v["index"], v["name"], v["fields"]) for v in variants] == [(0, "Yuma", []), (1, "Null", [])]
    assert it.SUBNET["consensus_mode"].item == "SubnetEpochConsensus"
    # Q13: metagraph storage for MetagraphLite
    assert md["SubtensorModule.Incentive"]["key"] == "NetUidStorageIndex"
    assert md["SubtensorModule.ValidatorPermit"]["value"] == "Vec<bool>"
    assert md["SubtensorModule.Keys"]["hashers"] == ["Identity", "Identity"]
    assert md["SubtensorModule.Owner"]["hashers"] == ["Blake2_128Concat"]


@pytest.mark.parametrize("spec", SPECS)
def test_rate_limit_key_variant(golden: Any, spec: int) -> None:
    variants = golden(f"metadata_storage_spec{spec}")["types"]["pallet_subtensor::RateLimitKey"]
    assert {v["name"]: v["index"] for v in variants}["NetworkLastRegistered"] == 2
    assert it.GLOBAL["last_reg_block"].key().endswith(b"\x02")


def test_legacy_rows_and_primaries(golden: Any) -> None:
    legacy = [r for r in it.ALL_ROWS if r.legacy_of is not None]
    assert {r.item for r in legacy} == {"LastMechansimStepBlock"}
    for r in legacy:
        p = it.primary_of(r)
        assert p is not None and p.field == r.legacy_of and p.plan == r.plan and p.decoder is r.decoder
    assert it.primary_of(it.SUBNET["tao"]) is None
    md348 = _summary(golden, 348)
    assert "SubtensorModule.LastEpochBlock" not in md348 and "SubtensorModule.LastMechansimStepBlock" in md348
    bad = dataclasses.replace(it.SUBNET["tao"], legacy_of="nope")
    with pytest.raises(KeyError):
        it.primary_of(bad)


def test_row_keys_by_scope() -> None:
    hk = "0x" + "ab" * 32
    ck = "0x" + "cd" * 32
    assert it.GLOBAL["tao_weight"].key() == h.prefix("SubtensorModule", "TaoWeight")
    assert it.GLOBAL["root_tao"].key() == h.k_subnet_tao(0)
    assert it.SUBNET["fee_rate"].key(netuid=5) == h.prefix("Swap", "FeeRate") + h.twox64_concat(h.le16(5))
    assert it.HOTKEY["take_u16"].key(hotkey=hk) == h.prefix("SubtensorModule", "Delegates") + h.blake2_128_concat(h.account(hk))
    assert it.HOTKEY["childkey_take_u16"].key(hotkey=hk, netuid=3) == (
        h.prefix("SubtensorModule", "ChildkeyTake") + h.blake2_128_concat(h.account(hk)) + h.le16(3))
    assert it.OWNER["owner_shares_v2"].key(hotkey=hk, coldkey=ck, netuid=7) == (
        h.prefix("SubtensorModule", "AlphaV2") + h.blake2_128_concat(h.account(hk)) + h.blake2_128_concat(h.account(ck))
        + h.le16(7))
    assert it.METAGRAPH["uid_hotkey"].key(netuid=4, uid=9) == h.prefix("SubtensorModule", "Keys") + h.le16(4) + h.le16(9)
    with pytest.raises(ValueError):
        it.SUBNET["tao"].key()
    with pytest.raises(ValueError):
        it.HOTKEY["take_u16"].key(hotkey="0x1234")


def test_option_rows_and_gate_exponent() -> None:
    feb = it.SUBNET["first_emission_block"]
    assert feb.query is Query.OPTION
    assert feb.decode(b"\x00") is None
    assert feb.decode((8_000_600).to_bytes(8, "little")) == 8_000_600
    assert feb.decode(b"\x01" + (8_000_600).to_bytes(8, "little")) == 8_000_600
    with pytest.raises(DecodeError):
        feb.decode(b"\x01\x02\x03")
    fmp = it.SUBNET["fast_moving_price"]
    assert fmp.decode((1 << 63).to_bytes(16, "little")) == Decimal("0.5")
    smu = it.GLOBAL["safe_mode_until"]
    assert smu.decode((123).to_bytes(4, "little")) == 123 and smu.decode((123).to_bytes(8, "little")) == 123
    ge = it.GLOBAL["gate_exponent"]
    assert ge.decode((3 << 64).to_bytes(16, "little")) == 3
    with pytest.raises(DecodeError):
        ge.decode(((3 << 64) + (1 << 63)).to_bytes(16, "little"))       # 3.5: fail closed (ADR-0001 #2)


def test_live_at_bounds_and_scopes() -> None:
    sp = it.SUBNET["v3_sqrt_price"]
    assert not sp.live_at(it.ERA_B_FIRST_BLOCK - 1) and sp.live_at(it.ERA_B_FIRST_BLOCK)
    assert sp.live_at(it.BALANCER_FIRST_BLOCK - 1) and not sp.live_at(it.BALANCER_FIRST_BLOCK)
    assert it.SCOPE_HASHERS[Scope.HOT_COLD_SUBNET] == (h.Hasher.BLAKE2_128_CONCAT, h.Hasher.BLAKE2_128_CONCAT,
                                                       h.Hasher.IDENTITY)
    assert it.plan_rows(it.SUBNET_ROWS, lambda r: r.plan is it.Plan.HEAD)
    with pytest.raises(ValueError):
        it.rows_by_field((it.SUBNET["tao"], it.SUBNET["tao"]))


def test_head_plan_fits_one_call() -> None:
    """HEAD plan at spec 475: hot-path per-subnet rows x 144 + globals fit one state_queryStorageAt of 2,000 keys."""
    head_rows = [r for r in it.SUBNET_ROWS if r.plan is it.Plan.HEAD and r.until_block is None and r.legacy_of is None]
    n_glob = len([r for r in it.GLOBAL_ROWS if r.legacy_of is None])
    assert len(head_rows) * it.DEFAULT_MAX_NETUID + n_glob <= 2_000
