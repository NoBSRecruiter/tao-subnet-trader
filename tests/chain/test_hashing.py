"""chain.hashing: storage-key vectors (DESIGN.md sections 6.3 and 10.1).

Every storage key captured in the golden fixtures (WP0, plain httpx, independent of this code) is rebuilt from its
item name, decoded key parts and hasher list.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.chain import hashing as h
from taotrader.chain import items as it
from taotrader.chain.hashing import Hasher

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
ENUM_PARTS = {"NetworkLastRegistered": b"\x02"}


def _golden_entries() -> list[tuple[str, dict[str, Any]]]:
    out = []
    files = sorted(glob.glob(str(GOLDEN / "*.json"))) + sorted(glob.glob(str(GOLDEN / "emission_parity" / "*.json")))
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for s in d.get("snapshots", []):
            for e in s.get("storage", []):
                out.append((Path(f).name, e))
    return out


def _encode_part(arg: Any) -> bytes:
    if isinstance(arg, int):
        return h.le16(arg)
    if isinstance(arg, str) and arg in ENUM_PARTS:
        return ENUM_PARTS[arg]
    return h.account(arg)


def test_twox128_check_value() -> None:
    assert h.twox128(b"System").hex() == "26aa394eea5630e07c48ae0c9558cef7"


def test_well_known_prefixes() -> None:
    # System.Account and Timestamp.Now full prefixes (Substrate well-known keys)
    assert h.prefix("System", "Account").hex() == "26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9"
    assert h.prefix("Timestamp", "Now").hex() == "f0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb"
    assert h.prefix("System", "Number").hex() == "26aa394eea5630e07c48ae0c9558cef702a5c1b19ab7a04f536c519aca4983ac"


def test_hasher_definitions() -> None:
    b = h.le16(92)
    assert h.twox64_concat(b)[8:] == b and len(h.twox64_concat(b)) == 10
    assert h.twox64_concat(b)[:8] == h.twox64(b)
    assert h.blake2_128_concat(b)[16:] == b and h.blake2_128_concat(b)[:16] == h.blake2_128(b)
    assert h.identity(b) == b
    assert len(h.twox256(b)) == 32 and h.twox256(b)[:16] == h.twox128(b)
    assert len(h.blake2_256(b)) == 32
    assert Hasher("Twox64Concat").apply(b) == h.twox64_concat(b)


def test_named_builders_match_registry() -> None:
    hk = "0x" + "ab" * 32
    assert h.k_subnet_tao(92) == it.SUBNET["tao"].key(netuid=92)
    assert h.k_balancer(92) == it.SUBNET["w_quote_e18"].key(netuid=92)
    assert h.k_hk_alpha(h.account(hk), 92) == it.HOTKEY["total_alpha"].key(netuid=92, hotkey=hk)
    assert h.k_alpha_divs(92, h.account(hk)) == it.HOTKEY["last_dividend"].key(netuid=92, hotkey=hk)
    assert h.k_last_reg() == it.GLOBAL["last_reg_block"].key()
    assert h.k_last_reg().hex().endswith("02")


def test_every_golden_key_is_rebuilt() -> None:
    entries = _golden_entries()
    assert len(entries) > 2_000
    for fname, e in entries:
        pallet, item = e["item"].split(".")
        parts = [(Hasher(hs), _encode_part(a)) for hs, a in zip(e["hashers"], e["args"], strict=True)]
        assert h.to_hex(h.storage_key(pallet, item, parts)) == e["key"], (fname, e["item"], e["args"])


def test_registry_rows_rebuild_golden_keys() -> None:
    """The registry's own key builders (not just the generic one) agree with the captured keys."""
    rows = {r.name: r for r in it.ALL_ROWS}
    seen = 0
    for _f, e in _golden_entries():
        row = rows.get(e["item"])
        if row is None:
            continue
        a = e["args"]
        s = row.scope
        if s is it.Scope.GLOBAL:
            key = row.key()
        elif s is it.Scope.FIXED_KEY:
            if (row.item == "SubnetTAO" and a != [0]) or (row.item == "LastRateLimitedBlock" and a != ["NetworkLastRegistered"]):
                continue
            key = row.key()
        elif s in (it.Scope.SUBNET, it.Scope.SWAP):
            key = row.key(netuid=a[0])
        elif s is it.Scope.HOTKEY_SUBNET:
            key = row.key(hotkey=a[0], netuid=a[1])
        elif s is it.Scope.SUBNET_HOTKEY:
            key = row.key(netuid=a[0], hotkey=a[1])
        elif s is it.Scope.HOTKEY:
            key = row.key(hotkey=a[0])
        elif s is it.Scope.HOT_COLD_SUBNET:
            key = row.key(hotkey=a[0], coldkey=a[1], netuid=a[2])
        else:
            continue
        assert h.to_hex(key) == e["key"], e["item"]
        seen += 1
    assert seen > 1_500


def test_storage_by_netuid_keys(golden: Any) -> None:
    snap = golden("sn51_emission_9240382")["snapshots"][0]
    rows = {r.name: r for r in it.SUBNET_ROWS}
    for item in snap["storage_by_netuid"]:
        row = rows[item]
        for n in (0, 1, 51, 144):
            key = row.key(netuid=n)
            assert key.startswith(h.prefix(row.pallet, row.item))
    assert len(snap["storage_by_netuid"]["SubtensorModule.SubnetTAO"]["values"]) == 145


def test_split_key_recovers_dividend_hotkeys(golden: Any) -> None:
    kp = golden("sn92_9240388")["snapshots"][0]["keys_paged"][0]
    row = it.HOTKEY["last_dividend"]
    hks = []
    for k in kp["keys"]:
        net, hk = h.split_key(h.from_hex(k), row.pallet, row.item, row.hashers, (2, 32))
        assert int.from_bytes(net, "little") == 92
        hks.append(h.to_hex(hk))
        assert h.to_hex(row.key(netuid=92, hotkey=h.to_hex(hk))) == k
    assert len(set(hks)) == 52


def test_split_key_rejects_bad_keys() -> None:
    row = it.HOTKEY["total_alpha"]
    good = row.key(netuid=7, hotkey="0x" + "01" * 32)
    assert h.split_key(good, row.pallet, row.item, row.hashers, (32, 2))[1] == h.le16(7)
    with pytest.raises(ValueError):
        h.split_key(good[:-1], row.pallet, row.item, row.hashers, (32, 2))           # truncated
    with pytest.raises(ValueError):
        h.split_key(good + b"\x00", row.pallet, row.item, row.hashers, (32, 2))      # trailing byte
    tampered = bytearray(good)
    tampered[33] ^= 0xFF                                                             # corrupt the blake2 prefix
    with pytest.raises(ValueError):
        h.split_key(bytes(tampered), row.pallet, row.item, row.hashers, (32, 2))
    with pytest.raises(ValueError):
        h.split_key(good, "Swap", "FeeRate", row.hashers, (32, 2))                   # wrong prefix
    with pytest.raises(ValueError):
        h.split_key(good, row.pallet, row.item, (Hasher.BLAKE2_128, Hasher.IDENTITY), (32, 2))


@given(st.binary(min_size=0, max_size=40), st.integers(min_value=0, max_value=65_535))
def test_concat_hashers_roundtrip(b: bytes, n: int) -> None:
    for hs in (Hasher.TWOX64_CONCAT, Hasher.BLAKE2_128_CONCAT, Hasher.IDENTITY):
        k = h.storage_key("SubtensorModule", "X", [(hs, b), (Hasher.IDENTITY, h.le16(n))])
        parts = h.split_key(k, "SubtensorModule", "X", (hs, Hasher.IDENTITY), (len(b), 2))
        assert parts == (b, h.le16(n))


def test_account_validation() -> None:
    with pytest.raises(ValueError):
        h.account("0x1234")
    with pytest.raises(ValueError):
        h.from_hex("1234")
