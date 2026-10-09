"""Read-only network checks for WP11 (pytest -m network): the live runtime's metadata at the finalized head still has
the storage items RealSdk reads (section 13 Q19) and the call indices / ProxyType::Staking index V6 pins.

Three JSON-RPC reads (chain_getBlockHash, chain_getHeader, state_getRuntimeVersion + state_getMetadata through the
WP1 pool at <= 3 req/s) against the public OnFinality archive. Nothing is signed or submitted. Needs scalecodec (the
collector extra) to decode the metadata."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from taotrader.live.sdk_port import COLDKEY_SWAP_ITEMS, EXPECTED_INDICES, REAL_PAYS_FEE_ITEM

pytestmark = pytest.mark.network


def _metadata() -> tuple[dict[str, Any], int]:
    from scalecodec.base import RuntimeConfiguration, ScaleBytes
    from scalecodec.type_registry import load_type_registry_preset

    from taotrader.chain.metadata import PUBLIC_ARCHIVE, fetch_metadata

    raw, spec, _tx, _block, _hash = asyncio.run(fetch_metadata(PUBLIC_ARCHIVE, None, None))
    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    rc.update_type_registry(load_type_registry_preset("legacy"))
    md = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(raw))
    md.decode()
    versioned = md.value[1]
    m: dict[str, Any] = versioned[next(iter(versioned))]
    return m, spec


def test_live_metadata_has_the_q19_items_and_the_v6_indices() -> None:
    pytest.importorskip("scalecodec")
    m, spec = _metadata()
    types = {t["id"]: t["type"] for t in m["types"]["types"]}
    storage: dict[str, dict[str, Any]] = {}
    calls: dict[str, int] = {}
    for p in m["pallets"]:
        for ent in (p.get("storage") or {}).get("entries", []):
            storage[f"{p['name']}.{ent['name']}"] = ent
        if p.get("calls"):
            for v in types[p["calls"]["ty"]]["def"]["variant"]["variants"]:
                calls[f"{p['name']}.{v['name']}"] = int(v["index"])
    proxy_type = next(t for t in types.values() if t.get("path") and t["path"][-1] == "ProxyType" and "variant" in t["def"])
    staking = next(int(v["index"]) for v in proxy_type["def"]["variant"]["variants"] if v["name"] == "Staking")
    got = {k: (staking if k == "ProxyType.Staking" else calls.get(k)) for k in EXPECTED_INDICES}
    assert got == dict(EXPECTED_INDICES), (spec, got)
    rp = storage[".".join(REAL_PAYS_FEE_ITEM)]
    assert rp["modifier"] == "Optional" and list(rp["type"]["Map"]["hashers"]) == ["Twox64Concat", "Twox64Concat"], spec
    present = [".".join(i) for i in COLDKEY_SWAP_ITEMS if ".".join(i) in storage]
    assert "SubtensorModule.ColdkeySwapAnnouncements" in present, (spec, present)
    for name in ("Proxy.Proxies", "Proxy.Announcements", "System.Account", "SubtensorModule.AlphaV2",
                 "SubtensorModule.TotalHotkeyAlpha", "SubtensorModule.TotalHotkeySharesV2"):
        assert name in storage, (spec, name)
