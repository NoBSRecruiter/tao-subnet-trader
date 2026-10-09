"""chain.metadata: per-spec layouts, verify-metadata, offline extraction (DESIGN.md sections 6.4, 6.12, 11 WP1)."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain import metadata as md
from taotrader.chain.metadata import Entry, SpecLayout, SpecLayouts

HASH_9240388 = "0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524"


def _layout(spec: int = 999, **entries: Entry) -> SpecLayout:
    return SpecLayout(spec_version=spec, transaction_version=1, block=1, block_hash=None, validated=True,
                      entries={k.replace("__", "."): v for k, v in entries.items()})


def test_committed_layouts_load_and_verify() -> None:
    lays = SpecLayouts()
    known = lays.known()
    assert 475 in known
    for spec in known:
        lay = lays.exact(spec)
        assert lay is not None and lay.exact and lay.spec_version == spec
        problems = md.verify_layout(lay)
        assert lay.validated == (not problems), (spec, problems)
        assert list(lay.notes) == problems, spec
    assert lays.accepted(475)


def test_spec475_layout_values() -> None:
    lay = SpecLayouts().exact(475)
    assert lay is not None and lay.validated and lay.block == 9_240_388 and lay.block_hash == HASH_9240388
    ge = lay.entry("SubtensorModule.EmissionGateExponent")
    assert ge is not None and ge.width == 16 and ge.default_bytes == (3 << 64).to_bytes(16, "little")
    assert it.GLOBAL["gate_exponent"].decode(ge.default_bytes) == 3
    sb = lay.entry("Swap.SwapBalancer")
    assert sb is not None and int.from_bytes(sb.default_bytes, "little") == 5 * 10**17
    ec = lay.entry("SubtensorModule.SubnetEpochConsensus")
    assert ec is not None and (ec.value, ec.width, ec.default) == ("EpochConsensus", 1, "0x00")
    assert lay.entry("SubtensorModule.ShortsEnabled") is None
    assert lay.entry("SubtensorModule.Alpha") is None and lay.entry("SubtensorModule.TotalHotkeyShares") is None
    dq = lay.entry("SubtensorModule.DissolveCleanupQueue")
    assert dq is not None and dq.width is None and dq.value == "Vec<NetUid>"
    oc = lay.entry("SubtensorModule.SubnetOwnerCut")
    assert oc is not None and oc.hashers == () and int.from_bytes(oc.default_bytes, "little") == 11_796


def test_extract_from_golden_metadata_matches_committed(golden: Any) -> None:
    pytest.importorskip("scalecodec")
    raw = golden("metadata_spec475_9240388")["metadata"]
    lay = md.extract_layout(raw, spec_version=475, transaction_version=1, block=9_240_388, block_hash=HASH_9240388)
    committed = SpecLayouts().exact(475)
    assert committed is not None
    assert lay.entries == committed.entries
    assert lay.validated and not lay.notes
    assert set(lay.entries) <= set(it.item_names())


def test_extract_agrees_with_independent_wp0_summary(golden: Any) -> None:
    """chain.metadata's extraction vs tools/capture_golden.py's (both scalecodec, different code): hashers, modifier,
    value type name and default bytes of every registry item."""
    pytest.importorskip("scalecodec")
    lay = md.extract_layout(golden("metadata_spec475_9240388")["metadata"], spec_version=475)
    summ = golden("metadata_storage_spec475")["pallets"]
    for name, e in lay.entries.items():
        pallet, item = name.split(".")
        s = summ[pallet][item]
        assert e.modifier == s["modifier"], name
        assert list(e.hashers) == (s.get("hashers") or []), name
        assert e.value == s["value"], name
        assert e.default == s["default"], name


def test_spec_layouts_fallback_and_acceptance(tmp_path: Path) -> None:
    base = SpecLayouts().exact(475)
    assert base is not None
    lays = SpecLayouts(tmp_path, extra=[base, dataclasses.replace(base, spec_version=480, validated=False)])
    assert lays.known() == (475, 480)
    fb = lays.for_spec(477)
    assert fb is not None and not fb.exact and not fb.validated and fb.spec_version == 477
    assert fb.entries == base.entries and "fallback from spec 475" in fb.source
    assert lays.for_spec(400) is None
    assert lays.accepted(475) and not lays.accepted(477) and not lays.accepted(480)
    assert lays.for_spec(475) is base


def test_layout_json_roundtrip_and_file_checks(tmp_path: Path) -> None:
    base = SpecLayouts().exact(475)
    assert base is not None
    path = md.write_layout(base, tmp_path)
    assert path.name == "475.json"
    again = SpecLayouts(tmp_path).exact(475)
    assert again is not None and again.entries == base.entries and again.to_json() == base.to_json()
    (tmp_path / "476.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError):
        SpecLayouts(tmp_path).known()                                  # 476.json holds spec 475
    bad = base.to_json()
    bad["format"] = 99
    with pytest.raises(ValueError):
        SpecLayout.from_json(bad)
    (tmp_path / "README.json").write_text("{}", encoding="utf-8")       # non-numeric stems are ignored
    (tmp_path / "476.json").unlink()
    assert SpecLayouts(tmp_path).known() == (475,)


def test_verify_layout_detects_mismatches() -> None:
    good = Entry(modifier="Default", hashers=("Identity",), key="NetUid", value="TaoBalance", width=8,
                 default="0x0000000000000000")
    assert md.verify_layout(_layout(SubtensorModule__SubnetTAO=good), [it.SUBNET["tao"]]) == []
    probs = md.verify_layout(_layout(SubtensorModule__SubnetTAO=dataclasses.replace(good, hashers=("Twox64Concat",))),
                             [it.SUBNET["tao"]])
    assert probs and "hashers" in probs[0]
    probs = md.verify_layout(_layout(SubtensorModule__SubnetTAO=dataclasses.replace(good, modifier="Optional")),
                             [it.SUBNET["tao"]])
    assert probs and "modifier" in probs[0]
    probs = md.verify_layout(_layout(SubtensorModule__SubnetTAO=dataclasses.replace(good, width=16, value="u128")),
                             [it.SUBNET["tao"]])
    assert any("width" in p for p in probs)
    probs = md.verify_layout(_layout(SubtensorModule__SubnetTAO=dataclasses.replace(good, value="Weird")),
                             [it.SUBNET["tao"]])
    assert any("not accepted" in p for p in probs)
    probs = md.verify_layout(_layout(SubtensorModule__SubnetTAO=dataclasses.replace(good, default="0x00")),
                             [it.SUBNET["tao"]])
    assert any("does not decode" in p for p in probs)
    assert any("required item missing" in p for p in md.verify_layout(_layout(), [it.SUBNET["tao"]]))
    vec = Entry(modifier="Default", hashers=(), key=None, value="Vec<AccountId32>", width=None, default="0x00")
    probs = md.verify_layout(_layout(SubtensorModule__DissolveCleanupQueue=vec), [it.GLOBAL["cleanup_queue_len"]])
    assert probs and "variable-width" in probs[0]


def test_verify_flags_watched_items() -> None:
    """ShortsEnabled appearing in a future spec is flagged (ADR-0001 #3): that spec is not validated / accepted."""
    base = SpecLayouts().exact(475)
    assert base is not None
    shorts = Entry(modifier="Default", hashers=(), key=None, value="bool", width=1, default="0x00")
    future = dataclasses.replace(base, spec_version=480, entries={**base.entries, "SubtensorModule.ShortsEnabled": shorts})
    probs = md.verify_layout(future)
    assert len(probs) == 1 and "ShortsEnabled" in probs[0] and "ADR-0001" in probs[0]
    assert md.verify_layout(base) == []


def test_type_table_helpers() -> None:
    assert md._typenum("UInt<UInt<UInt<UInt<UInt<UInt<UTerm, B1>, B0>, B0>, B0>, B0>, B0>") == 32
    assert md._typenum("UInt<UInt<UInt<UInt<UInt<UInt<UInt<UTerm, B1>, B0>, B0>, B0>, B0>, B0>, B0>") == 64
    tt = md._TypeTable({
        0: {"def": {"primitive": "u16"}},
        1: {"def": {"composite": {"fields": [{"type": 2}, {"type": 3}]}}, "path": ["share_pool", "SafeFloat"]},
        2: {"def": {"primitive": "u128"}},
        3: {"def": {"primitive": "i64"}},
        4: {"def": {"sequence": {"type": 0}}},
        5: {"def": {"variant": {"variants": [{"fields": []}, {"fields": []}]}}, "path": ["x", "EpochConsensus"]},
        6: {"def": {"array": {"type": 0, "len": 3}}},
        7: {"def": {"tuple": [0, 2]}},
        8: {"def": {"variant": {"variants": [{"fields": [{"type": 0}]}]}}, "path": ["x", "Data"]},
        9: {"def": {"compact": {"type": 2}}},
    })
    assert (tt.name(1), tt.width(1)) == ("SafeFloat", 24)
    assert (tt.name(4), tt.width(4)) == ("Vec<u16>", None)
    assert (tt.name(5), tt.width(5)) == ("EpochConsensus", 1)
    assert (tt.name(6), tt.width(6)) == ("[u16; 3]", 6)
    assert (tt.name(7), tt.width(7)) == ("(u16, u128)", 18)
    assert tt.width(8) is None and tt.width(9) is None and tt.name(9) == "Compact<u128>"
    assert md._hex_default("0x00ff") == "0x00ff" and md._hex_default(b"\x01") == "0x01"
    assert md._hex_default("abc") == "0x616263"
    with pytest.raises(ValueError):
        md._hex_default(3)


def test_cli_extract_and_verify_offline(golden_dir: Path, tmp_path: Path, capsys: Any) -> None:
    pytest.importorskip("scalecodec")
    src = golden_dir / "metadata_spec475_9240388.json"
    args = ["--hex", str(src), "--spec", "475", "--tx", "1", "--block", "9240388", "--hash", HASH_9240388]
    assert md.main(["verify", *args]) == 0                              # live layout == committed layout
    assert md.main(["extract", *args, "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "475.json").read_text(encoding="utf-8"))["validated"] is True
    assert md.main(["verify", *args, "--out", str(tmp_path / "empty")]) == 1   # nothing committed there
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "no committed layout" in out


def test_check_against_committed_reports_diff() -> None:
    base = SpecLayouts().exact(475)
    assert base is not None
    changed = dict(base.entries)
    k = "SubtensorModule.Tempo"
    changed[k] = dataclasses.replace(changed[k], default="0x6802")
    probs = md.check_against_committed(dataclasses.replace(base, entries=changed))
    assert len(probs) == 1 and k in probs[0]
