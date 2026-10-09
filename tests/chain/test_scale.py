"""chain.scale: exact decoders (DESIGN.md sections 6.4 and 10.1).

Golden values (WP0 fixtures) cover SN92, the globals and the era-B pools; every captured storage value of every
registry item decodes with the registry decoder.
"""
from __future__ import annotations

import glob
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.chain import items as it
from taotrader.chain import scale as sc
from taotrader.core.errors import DecodeError
from taotrader.core.fixed import EXACT

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"


def _hex(s: str) -> bytes:
    return bytes.fromhex(s[2:])


def _glob_value(fixture: dict[str, Any], item: str, args: list[Any] | None = None) -> bytes | None:
    for e in fixture["snapshots"][0]["storage"]:
        if e["item"] == item and (args is None or e["args"] == args):
            return None if e["value"] is None else _hex(e["value"])
    raise KeyError(item)


def test_subnet_moving_alpha_exact(golden: Any) -> None:
    raw = _glob_value(golden("globals_9240878"), "SubtensorModule.SubnetMovingAlpha")
    assert raw is not None
    v = sc.d_i96f32(raw)
    assert int.from_bytes(raw, "little") == 1_288_490
    assert v == Decimal(1_288_490) / Decimal(2**32)                   # exact: 2**-32 terminates
    assert EXACT.multiply(v, Decimal(2**32)) == 1_288_490
    assert v.quantize(Decimal("0.0000001")) == Decimal("0.0003000")


def test_gate_bar_and_tao_weight(golden: Any) -> None:
    g = golden("sn92_9240388")                   # theta is recomputed every 360 blocks: 0.0082624 at 9,240,388
    raw = _glob_value(g, "SubtensorModule.EmissionGateBar") or b""
    bar = sc.d_u64f64(raw)
    assert bar.quantize(Decimal("0.0000001")) == Decimal("0.0082624")
    assert EXACT.multiply(bar, Decimal(2**64)) == int.from_bytes(raw, "little")
    tw = sc.d_tao_weight(_glob_value(g, "SubtensorModule.TaoWeight") or b"")
    assert tw.quantize(Decimal("0.000001")) == Decimal("0.180000")
    q = sc.d_u64f64(_glob_value(g, "SubtensorModule.EmissionBarQuantile") or b"")
    assert q == Decimal("0.75")


def test_sn92_values(golden: Any) -> None:
    f = golden("sn92_9240388")
    assert sc.d_u64(_glob_value(f, "SubtensorModule.SubnetTAO", [92]) or b"") == 587_199_047_950
    assert sc.d_u64(_glob_value(f, "SubtensorModule.SubnetAlphaIn", [92]) or b"") == 435_539_509_978_376
    assert sc.d_perquintill_raw(_glob_value(f, "Swap.SwapBalancer", [92]) or b"") == 499_999_964_641_764_870
    mp = sc.d_i96f32(_glob_value(f, "SubtensorModule.SubnetMovingPrice", [92]) or b"")
    assert mp == Decimal(int.from_bytes(_glob_value(f, "SubtensorModule.SubnetMovingPrice", [92]) or b"", "little")) / 2**32
    rp = sc.d_u96f32(_glob_value(f, "SubtensorModule.RootProp", [92]) or b"")
    assert rp.quantize(Decimal("0.001")) == Decimal("0.479")
    flow = sc.d_i64(_glob_value(f, "SubtensorModule.SubnetTaoFlow", [92]) or b"")
    assert flow < 0                                                   # 0x...ffffff: negative running total
    assert sc.d_u16(_glob_value(f, "SubtensorModule.MaxAllowedValidators", [92]) or b"") == 64
    assert sc.d_account(_glob_value(f, "SubtensorModule.SubnetOwnerHotkey", [92]) or b"").startswith("0xe22a71b4")


def test_safefloat_shares(golden: Any) -> None:
    f = golden("sn92_9240388")
    for e in f["snapshots"][0]["storage"]:
        if e["item"] == "SubtensorModule.TotalHotkeySharesV2" and e["value"]:
            raw = _hex(e["value"])
            m, ex = sc.safefloat_parts(raw)
            v = sc.d_safefloat(raw)
            assert v == Decimal(m).scaleb(ex, EXACT)
            assert len(raw) == 24 and -30 < ex < 30


@given(st.integers(min_value=0, max_value=2**128 - 1), st.integers(min_value=-10_000, max_value=10_000))
def test_safefloat_roundtrip(m: int, e: int) -> None:
    raw = m.to_bytes(16, "little") + e.to_bytes(8, "little", signed=True)
    v = sc.d_safefloat(raw)
    assert v == Decimal(m).scaleb(e, EXACT)
    assert sc.safefloat_parts(raw) == (m, e)
    assert v.as_tuple().exponent == e                                  # exact: no rounding to a context


@given(st.integers(min_value=-(2**127), max_value=2**127 - 1))
def test_i96f32_exact(raw: int) -> None:
    v = sc.d_i96f32(raw.to_bytes(16, "little", signed=True))
    assert EXACT.multiply(v, Decimal(2**32)) == raw


@given(st.integers(min_value=0, max_value=2**128 - 1))
def test_u64f64_and_u96f32_exact(raw: int) -> None:
    b = raw.to_bytes(16, "little")
    assert EXACT.multiply(sc.d_u64f64(b), Decimal(2**64)) == raw
    assert EXACT.multiply(sc.d_u96f32(b), Decimal(2**32)) == raw


@pytest.mark.parametrize(("fn", "width"), [(sc.d_u8, 1), (sc.d_u16, 2), (sc.d_u32, 4), (sc.d_u64, 8), (sc.d_u128, 16),
                                           (sc.d_i64, 8), (sc.d_i96f32, 16), (sc.d_u64f64, 16), (sc.d_safefloat, 24),
                                           (sc.d_account, 32), (sc.d_bool, 1), (sc.d_perquintill_raw, 8)])
def test_strict_widths(fn: Any, width: int) -> None:
    fn(b"\x00" * width)
    for bad in (width - 1, width + 1):
        with pytest.raises(DecodeError):
            fn(b"\x00" * bad)


def test_bool_and_enum() -> None:
    assert sc.d_bool(b"\x01") is True and sc.d_bool(b"\x00") is False
    with pytest.raises(DecodeError):
        sc.d_bool(b"\x02")
    dec = sc.d_enum_index(2)
    assert dec(b"\x01") == 1
    with pytest.raises(DecodeError):
        dec(b"\x02")


def test_option_by_width() -> None:
    opt = sc.d_option(sc.d_u64, 8)
    assert opt(None) is None
    assert opt(b"\x00") is None
    assert opt((9_240_495).to_bytes(8, "little")) == 9_240_495                      # OptionQuery: raw T
    assert opt(b"\x01" + (9_240_495).to_bytes(8, "little")) == 9_240_495            # ValueQuery<Option<T>>
    with pytest.raises(DecodeError):
        opt(b"\x01" + b"\x00" * 5)                                                    # wrong width
    with pytest.raises(DecodeError):
        opt(b"\x02" + b"\x00" * 8)
    bn = sc.d_option(sc.d_blocknum, sc.BLOCKNUM.widths)                             # u32 or u64 by length
    assert bn((5).to_bytes(4, "little")) == 5 and bn((5).to_bytes(8, "little")) == 5
    assert bn(b"\x01" + (7).to_bytes(4, "little")) == 7
    var = sc.d_option(sc.d_vec_u16, None)
    assert var(b"\x01\x04\x05\x00") == (5,)
    with pytest.raises(DecodeError):
        sc.d_blocknum(b"\x00" * 6)


@given(st.integers(min_value=0, max_value=2**100))
def test_compact_roundtrip(n: int) -> None:
    from taotrader.chain.head import encode_compact
    b = encode_compact(n)
    assert sc.d_compact(b) == (n, len(b))
    assert sc.d_compact(b"\xff" + b, 1) == (n, len(b))


def test_compact_modes_and_truncation() -> None:
    assert sc.d_compact(b"\x00") == (0, 1)
    assert sc.d_compact(b"\xfc") == (63, 1)
    assert sc.d_compact(b"\x01\x01") == (64, 2)
    assert sc.d_compact(b"\x02\x00\x01\x00") == (2**14, 4)
    assert sc.d_compact(b"\x03\x00\x00\x00\x40") == (2**30, 5)
    for bad in (b"", b"\x01", b"\x02\x00", b"\x07\x00\x00\x00"):
        with pytest.raises(DecodeError):
            sc.d_compact(bad)


def test_vectors() -> None:
    assert sc.d_vec_len(b"\x00") == 0
    assert sc.d_vec_len(b"\x08\x05\x00\x5c\x00") == 2
    assert sc.d_vec_u16(b"\x08\x05\x00\x5c\x00") == (5, 92)
    assert sc.d_vec_bool(b"\x0c\x01\x00\x01") == (True, False, True)
    with pytest.raises(DecodeError):
        sc.d_vec_u16(b"\x08\x05\x00\x5c")


def test_gate_exponent_integral_or_fail_closed() -> None:
    dec = it.GLOBAL["gate_exponent"].decoder
    assert dec(bytes.fromhex("00000000000000000300000000000000")) == 3                # the spec-475 default (3.0)
    with pytest.raises(DecodeError):
        dec((3 * 2**64 + 2**63).to_bytes(16, "little"))                              # 3.5 is not an int


def _all_golden_storage() -> list[dict[str, Any]]:
    out = []
    files = sorted(glob.glob(str(GOLDEN / "*.json"))) + sorted(glob.glob(str(GOLDEN / "emission_parity" / "*.json")))
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for s in d.get("snapshots", []):
            out.extend(s.get("storage", []))
            for item, blk in (s.get("storage_by_netuid") or {}).items():
                out.extend({"item": item, "args": [n], "value": v} for n, v in enumerate(blk["values"]))
    return out


def test_every_golden_value_decodes_with_its_registry_row() -> None:
    rows: dict[str, list[it.Row]] = {}
    for r in it.ALL_ROWS:
        rows.setdefault(r.name, []).append(r)
    n = 0
    for e in _all_golden_storage():
        if e["value"] is None or e["item"] not in rows:
            continue
        for row in rows[e["item"]]:
            if row.scope is it.Scope.FIXED_KEY and e["args"] not in ([0], ["NetworkLastRegistered"]):
                continue
            row.decode(_hex(e["value"]))
            n += 1
    assert n > 10_000
