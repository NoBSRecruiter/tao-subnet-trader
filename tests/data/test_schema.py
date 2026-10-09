"""WP3 schema tests (DESIGN.md section 7): DDL verbatim and executable, Parquet column specs, fixed-point conversions,
and the snapshot <-> rows mapping (digest-identical round trip, exactness of I96F32 / U64F64 / SafeFloat / TaoWeight,
and the lossless exact_json fallback for values a section 7.1 column cannot hold)."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given
from hypothesis import strategies as st

from taotrader.core import codec
from taotrader.core.errors import DecodeError
from taotrader.core.fixed import EXACT
from taotrader.core.state import ChainSnapshot, HotkeyIdx, MetagraphLite, PoolKind, PoolState, Quality, SubnetState
from taotrader.core.units import AlphaRao, Block, Coldkey, Hotkey, NetUid, Rao, SubnetKey
from taotrader.data import schema
from taotrader.data.schema import (
    LAKE_TABLES,
    SNAPSHOT_TABLES,
    decimal_to_mant_exp,
    fixed_to_raw,
    mant_exp_to_decimal,
    ratio_to_raw,
    raw_to_fixed,
    raw_to_ratio,
    snapshot_digest,
    snapshot_from_rows,
    snapshot_to_rows,
    with_digest,
)

ROOT = Path(__file__).resolve().parents[2]
U64 = 2**64 - 1
U128 = 2**128 - 1
I128 = 2**127 - 1


def design_sql_blocks() -> list[str]:
    d = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = d[d.index("## 7. Storage schema"):d.index("## 8. Backtest engine")]
    return re.findall(r"```sql\n(.*?)```", sec, flags=re.S)


# ------------------------------------------------------------------------------------------------ DDL
def test_ddl_is_verbatim_from_design_section_7() -> None:
    lake, journal, state = design_sql_blocks()
    assert lake == schema.LAKE_DDL
    assert journal == schema.JOURNAL_DDL
    assert state == schema.STATE_DDL


def test_lake_ddl_runs_in_duckdb_and_matches_the_column_specs() -> None:
    con = duckdb.connect(":memory:")
    con.execute(schema.LAKE_DDL)
    for name, spec in LAKE_TABLES.items():
        got = [(r[0], r[1]) for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
            [name]).fetchall()]
        want = [(c.name, c.type) for c in spec.cols if not c.extra]
        assert got == want, name
        extras = [c.name for c in spec.cols if c.extra]
        assert extras == (["exact_json"] if name in SNAPSHOT_TABLES else [])
        assert spec.view.startswith("v_")
        assert set(spec.key) <= set(spec.names) and set(spec.sort) <= set(spec.names)
    assert LAKE_TABLES["snap_subnet"].view == "v_subnet"
    con.close()


def test_journal_and_state_ddl_run_in_sqlite() -> None:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(schema.JOURNAL_DDL)
    db.executescript(schema.STATE_DDL)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"journal", "anchor", *schema.SHARED_STATE_TABLES, *schema.RUN_STATE_TABLES}
    triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert triggers == set(schema.JOURNAL_TRIGGERS)
    stmts = schema.create_table_statements(schema.STATE_DDL)
    assert sorted(stmts) == sorted([*schema.SHARED_STATE_TABLES, *schema.RUN_STATE_TABLES])
    assert schema.if_not_exists(stmts["manifest"]).startswith("CREATE TABLE IF NOT EXISTS manifest (")


def test_era_partition_boundaries() -> None:
    assert schema.era_of(4_920_351) == "A"
    assert schema.era_of(5_947_548) == "A" and schema.era_of(5_947_549) == "B"
    assert schema.era_of(8_486_593) == "B" and schema.era_of(8_486_594) == "C"


# ------------------------------------------------------------------------------------------------ fixed point
@given(st.integers(-(2**127), I128))
def test_i96f32_raw_round_trip_is_exact(raw: int) -> None:
    d = raw_to_fixed(raw, 32)
    assert d == EXACT.divide(Decimal(raw), Decimal(2**32))      # the WP1 decoder's formula
    assert fixed_to_raw(d, 32) == raw


@given(st.integers(0, U128))
def test_u64f64_raw_round_trip_is_exact(raw: int) -> None:
    assert fixed_to_raw(raw_to_fixed(raw, 64), 64) == raw


@given(st.integers(0, U64))
def test_tao_weight_ratio_round_trip_is_exact(raw: int) -> None:
    d = raw_to_ratio(raw, U64)
    assert d == EXACT.divide(Decimal(raw), Decimal(U64))
    assert ratio_to_raw(d, U64) == raw


def test_inexact_values_have_no_raw() -> None:
    assert fixed_to_raw(Decimal("0.0013482"), 32) is None
    assert ratio_to_raw(Decimal("0.18"), U64) is None
    assert fixed_to_raw(Decimal("NaN"), 32) is None


@given(st.decimals(allow_nan=False, allow_infinity=False))
def test_mantissa_exponent_is_exact(d: Decimal) -> None:
    m, e = decimal_to_mant_exp(d)
    assert mant_exp_to_decimal(m, e) == d
    assert m == 0 or m % 10 != 0                                   # trailing zeros stripped
    if m == 0:
        assert e == 0


# ------------------------------------------------------------------------------------------------ snapshot rows
def exactify(snap: ChainSnapshot) -> ChainSnapshot:
    """Replace the factory's decimal-literal parameters by chain-exact values (what WP1's decoders produce)."""
    g = replace(snap.glob, moving_alpha=raw_to_fixed(1_288_490, 32), gate_bar=raw_to_fixed(152_412_335_548_014_784, 64),
                tao_weight=raw_to_ratio(3_320_413_933_267_719_290, U64))
    subs = tuple(replace(s, moving_price=raw_to_fixed(5_790_474 + int(s.key.netuid), 32),
                         root_prop=raw_to_fixed(2_057_289_908, 32), miner_burned=raw_to_fixed(0, 32))
                 for s in snap.subnets)
    return replace(snap, glob=g, subnets=subs, digest="")


@pytest.fixture
def snap(make_snapshot: Callable[..., ChainSnapshot], make_subnet: Callable[..., SubnetState],
         hk: Callable[[int], Hotkey]) -> ChainSnapshot:
    hotkeys = (HotkeyIdx(hk(1), AlphaRao(5 * 10**14), Decimal(123456789123) * Decimal(10) ** -3, take_u16=11_796,
                         childkey_take_u16=0, earns=True, last_dividend=AlphaRao(77)),
               HotkeyIdx(hk(2), AlphaRao(10**12), raw_to_fixed(18_446_744_073_709_551_617_123, 64)),   # V1 U64F64
               HotkeyIdx(hk(3), AlphaRao(0), Decimal(0)))
    s92 = make_subnet(92, 8_355_590, hotkeys=hotkeys, owner_coldkey=Coldkey("0x" + "c1" * 32), owner_hotkey=hk(1),
                      fast_moving_price=raw_to_fixed(25_000_000_000_000_000, 64), tao_flow_cum=-123_456_789,
                      volume_cum=10**30, owner_cut_enabled=True, owner_cut_autolock=False,
                      total_alpha_staked=AlphaRao(10**15), escrow_alpha=AlphaRao(12_345), owner_alpha=AlphaRao(9),
                      max_allowed_validators=64, consensus_mode=1,
                      metagraph=MetagraphLite(n_miners=200, n_miner_coldkeys=40, top1_coldkey_share_ppm=310_000,
                                              n_permit_coldkeys=12),
                      quality=Quality.CARRIED | Quality.NO_YIELD_IDX)
    s1 = make_subnet(1, 100, first_emission_block=None, quality=Quality.NOT_STARTED)
    s70 = make_subnet(70, 9_000_000, pool=replace(s92.pool, kind=PoolKind.CP_V3_VIRTUAL, px_tao=10**13, px_alpha=3 * 10**15))
    return exactify(make_snapshot(9_240_388, [s92, s1, s70], safe_mode_until=Block(9_300_000),
                                  runtime_prune_target=NetUid(70)))


def round_trip(snap: ChainSnapshot, **kw: Any) -> tuple[ChainSnapshot, schema.SnapshotRows]:
    rows = snapshot_to_rows(snap, **kw)
    return snapshot_from_rows(rows.glob, rows.subnets, rows.hotkeys), rows


def test_exact_snapshot_round_trips_without_any_fallback(snap: ChainSnapshot) -> None:
    back, rows = round_trip(snap)
    full = with_digest(snap)
    assert back == full and back.digest == full.digest == snapshot_digest(snap)
    assert codec.canonical_bytes(back) == codec.canonical_bytes(full)
    assert rows.glob["exact_json"] is None
    assert all(r["exact_json"] is None for r in rows.subnets)
    hk_json = [r["exact_json"] for r in rows.hotkeys]
    assert hk_json[0] is None and hk_json[2] is None
    # V1 U64F64 shares need an ~84-digit mantissa: the column keeps a 38-digit approximation, exact_json the exact text
    assert json.loads(hk_json[1]) == {"shares_mantissa": codec.encode(snap.subnets[2].hotkeys[1].total_shares)}
    g = rows.glob
    assert g["moving_alpha_raw"] == 1_288_490 and g["gate_bar_raw"] == 152_412_335_548_014_784
    assert g["tao_weight_raw"] == 3_320_413_933_267_719_290 and g["digest"] == full.digest
    assert g["quality_or"] == int(Quality.CARRIED | Quality.NO_YIELD_IDX | Quality.NOT_STARTED)
    r92 = next(r for r in rows.subnets if r["netuid"] == 92)
    assert r92["moving_price_raw"] == 5_790_474 + 92 and r92["fast_moving_raw"] == 25_000_000_000_000_000
    assert (r92["mg_n_miners"], r92["mg_top1_coldkey_ppm"], r92["consensus_mode"]) == (200, 310_000, 1)
    h1 = rows.hotkeys[0]
    assert (h1["shares_mantissa"], h1["shares_exp"]) == (123456789123, -3)
    assert [r["netuid"] for r in rows.subnets] == [1, 70, 92]


def test_informational_columns_are_written_but_not_part_of_the_snapshot(snap: ChainSnapshot, hk: Any) -> None:
    back, rows = round_trip(snap, decoder_version=7, escrow_block={92: 9_240_000}, shares_src={(92, str(hk(1))): 2})
    assert back == with_digest(snap)
    assert rows.glob["decoder_version"] == 7
    assert next(r for r in rows.subnets if r["netuid"] == 92)["escrow_block"] == 9_240_000
    assert rows.hotkeys[0]["shares_src"] == 2 and rows.hotkeys[1]["shares_src"] is None


def test_factory_literals_use_the_lossless_fallback(make_snapshot: Any, make_subnet: Any) -> None:
    s = make_snapshot(9_240_388, [make_subnet(92)])          # moving_price 0.0013482, tao_weight 0.18: not raw/2^k
    back, rows = round_trip(s)
    assert back.digest == snapshot_digest(s) and back == with_digest(s)
    ov = json.loads(rows.glob["exact_json"])
    assert set(ov) == {"gate_bar_raw", "tao_weight_raw"} and ov["tao_weight_raw"] == "0.18"   # 1,288,490/2^32 is exact
    assert rows.glob["tao_weight_raw"] == int(Decimal("0.18") * U64)            # nearest value kept for analytics
    assert set(json.loads(rows.subnets[0]["exact_json"])) == {"moving_price_raw", "root_prop_raw"}


def test_out_of_range_integers_round_trip(snap: ChainSnapshot) -> None:
    s92 = snap.subnets[2]
    big = replace(s92, volume_cum=U128, tao_flow_cum=-(2**70), alpha_out=AlphaRao(2**64 + 5),
                  hotkeys=(replace(s92.hotkeys[0], total_shares=Decimal(U128) * Decimal(10) ** -18,
                                   last_dividend=AlphaRao(-1)),))
    s = replace(snap, subnets=(snap.subnets[0], snap.subnets[1], big), glob=replace(snap.glob, total_issuance=Rao(2**65)))
    back, rows = round_trip(s)
    assert back == with_digest(s)
    r92 = rows.subnets[2]
    assert r92["volume_cum"] is None and r92["tao_flow_cum"] is None and r92["alpha_out"] is None
    assert set(json.loads(r92["exact_json"])) >= {"volume_cum", "tao_flow_cum", "alpha_out"}
    assert rows.hotkeys[0]["shares_mantissa"] is not None and rows.hotkeys[0]["exact_json"] is not None


# hypothesis: any decimal / integer values the dataclasses admit come back digest-identically
fixed32 = st.one_of(st.integers(-(2**127), I128).map(lambda r: raw_to_fixed(r, 32)),
                    st.decimals(min_value=-(10**45), max_value=10**45, places=40))
fixed64 = st.one_of(st.integers(0, U128).map(lambda r: raw_to_fixed(r, 64)),
                    st.decimals(min_value=0, max_value=10**45, places=80))
safefloat = st.builds(lambda m, e: Decimal(f"{m}E{e}"), st.integers(0, U128), st.integers(-40, 20))
shares = st.one_of(safefloat, st.integers(0, U128).map(lambda r: raw_to_fixed(r, 64)))
u64 = st.integers(0, U64)


@st.composite
def subnet_states(draw: st.DrawFn, netuid: int) -> SubnetState:
    hks = sorted(set(draw(st.lists(st.integers(1, 50), max_size=3))))
    hotkeys = tuple(HotkeyIdx(Hotkey("0x" + f"{h:064x}"), AlphaRao(draw(u64)), draw(shares), take_u16=draw(st.integers(0, 65535)),
                              childkey_take_u16=draw(st.integers(0, 65535)), earns=draw(st.booleans()),
                              last_dividend=AlphaRao(draw(u64)))
                    for h in hks)
    pool_tao = draw(u64)
    return SubnetState(
        key=SubnetKey(NetUid(netuid), Block(draw(st.integers(0, 10**7)))),
        pool=PoolState(draw(st.sampled_from(list(PoolKind))), Rao(pool_tao), AlphaRao(draw(u64)), draw(st.integers(0, 2**100)),
                       draw(st.integers(0, 2**100)), draw(st.integers(0, 10**18)), draw(st.integers(0, 65535))),
        alpha_out=AlphaRao(draw(u64)), protocol_alpha=AlphaRao(draw(u64)), moving_price=draw(fixed32),
        root_prop=draw(fixed32), miner_burned=draw(fixed32), emission_enabled=draw(st.booleans()),
        subtoken_enabled=draw(st.booleans()), reg_allowed=draw(st.booleans()),
        first_emission_block=draw(st.none() | u64.map(Block)), tempo=draw(st.integers(0, 65535)),
        last_epoch_block=Block(draw(u64)), ema_halving_blocks=draw(st.integers(0, 2**32 - 1)),
        tao_in_emission=Rao(draw(u64)), excess_tao=Rao(draw(u64)), alpha_out_emission=AlphaRao(draw(u64)),
        alpha_in_emission=AlphaRao(draw(u64)), reservoir_tao=Rao(draw(u64)), reservoir_alpha=AlphaRao(draw(u64)),
        tao_flow_cum=draw(st.none() | st.integers(-(2**63), 2**63 - 1)), volume_cum=draw(st.none() | st.integers(0, U128)),
        fast_moving_price=draw(st.none() | fixed64), owner_coldkey=draw(st.none() | st.just(Coldkey("0x" + "11" * 32))),
        owner_hotkey=draw(st.none() | st.just(Hotkey("0x" + "22" * 32))), owner_cut_enabled=draw(st.none() | st.booleans()),
        owner_cut_autolock=draw(st.none() | st.booleans()), total_alpha_staked=draw(st.none() | u64.map(AlphaRao)),
        escrow_alpha=draw(st.none() | u64.map(AlphaRao)), owner_alpha=draw(st.none() | u64.map(AlphaRao)),
        max_allowed_validators=draw(st.none() | st.integers(0, 65535)), consensus_mode=draw(st.none() | st.integers(0, 255)),
        metagraph=draw(st.none() | st.builds(MetagraphLite, st.integers(0, 65535), st.integers(0, 65535),
                                             st.integers(0, 10**6), st.integers(0, 65535))),
        hotkeys=hotkeys, quality=Quality(draw(st.integers(0, 1023))))


def draw_snapshot(data: st.DataObject, make_snapshot: Any, make_globals: Any) -> ChainSnapshot:
    netuids = sorted(set(data.draw(st.lists(st.integers(1, 200), max_size=4))))
    subs = [data.draw(subnet_states(n)) for n in netuids]
    g = make_globals(moving_alpha=data.draw(fixed32), gate_bar=data.draw(fixed64),
                     tao_weight=data.draw(st.one_of(u64.map(lambda r: raw_to_ratio(r, U64)), st.decimals(0, 1, places=20))),
                     gate_exponent=data.draw(st.integers(0, 255)), safe_mode_until=data.draw(st.none() | u64.map(Block)),
                     shorts_enabled=data.draw(st.booleans()))
    s: ChainSnapshot = make_snapshot(data.draw(st.integers(0, 2**63)), subs, glob=g, timestamp_ms=data.draw(u64))
    return s


@given(data=st.data())
def test_any_snapshot_round_trips_digest_identically(data: st.DataObject, make_snapshot: Any, make_globals: Any) -> None:
    s = draw_snapshot(data, make_snapshot, make_globals)
    back, _ = round_trip(s)
    assert back == with_digest(s)
    assert back.digest == snapshot_digest(s)


@given(data=st.data())
def test_fast_canonical_encoding_is_byte_identical_to_the_codec(data: st.DataObject, make_snapshot: Any,
                                                                make_globals: Any) -> None:
    s = draw_snapshot(data, make_snapshot, make_globals)
    assert schema.fast_encode(s) == codec.encode(s)
    assert schema.canonical_snapshot_bytes(s) == codec.canonical_bytes(s)
    assert snapshot_digest(s) == codec.digest(replace(s, digest=""))
    sealed, raw = schema.sealed_snapshot_bytes(s)
    assert sealed == with_digest(s) and raw == codec.canonical_bytes(sealed)
    assert schema.sealed_snapshot_bytes(sealed) == (sealed, raw)
    with pytest.raises(ValueError):
        schema.sealed_snapshot_bytes(replace(sealed, digest="f" * 32))


def test_fast_encoding_matches_the_codec_on_fixtures(snap: ChainSnapshot, make_snapshot: Any, make_subnet: Any) -> None:
    for s in (snap, make_snapshot(9_240_388, [make_subnet(92), make_subnet(1)])):
        assert schema.canonical_snapshot_bytes(s) == codec.canonical_bytes(s)
        assert snapshot_digest(s) == codec.digest(replace(s, digest=""))


@given(data=st.data())
def test_fast_row_decoder_agrees_with_the_validating_decoder(data: st.DataObject, make_snapshot: Any,
                                                             make_globals: Any) -> None:
    s = draw_snapshot(data, make_snapshot, make_globals)
    rows = snapshot_to_rows(s)
    for row in rows.subnets:
        hk = [h for h in rows.hotkeys if h["netuid"] == row["netuid"]]
        fast = schema._subnet_fast(row, hk)
        slow = schema._subnet_from_row(row, hk, "t")
        if row["exact_json"] is None and all(h["exact_json"] is None for h in hk):
            assert fast is not None
        if fast is not None:
            assert fast == slow and codec.encode(fast) == codec.encode(slow)


# ------------------------------------------------------------------------------------------------ validation
@pytest.mark.parametrize("value", ["1E-5000", "-1E+5000", "0E-7000", "123.456E-300", "1E+39"])
def test_pathological_decimals_take_the_fallback_without_blowing_up(snap: ChainSnapshot, value: str) -> None:
    d = Decimal(value)
    assert ratio_to_raw(d, U64) in (None, 0)          # (1E+39 has a raw value; it just does not fit the column)
    s92 = replace(snap.subnets[2], moving_price=d, fast_moving_price=d,
                  hotkeys=(replace(snap.subnets[2].hotkeys[0], total_shares=d),))
    s = replace(snap, subnets=(*snap.subnets[:2], s92), glob=replace(snap.glob, tao_weight=d), digest="")
    back, _ = round_trip(s)
    assert back == with_digest(s)


def test_with_digest_fills_and_rejects_a_wrong_digest(snap: ChainSnapshot) -> None:
    s = with_digest(snap)
    assert len(s.digest) == 32 and s.digest == codec.digest(replace(s, digest=""))
    assert with_digest(s) is s
    with pytest.raises(ValueError, match="does not match"):
        with_digest(replace(s, digest="0" * 32))


def test_unsorted_or_duplicate_members_are_rejected(snap: ChainSnapshot) -> None:
    subs = snap.subnets
    with pytest.raises(ValueError, match="sorted"):
        snapshot_to_rows(replace(snap, subnets=(subs[1], subs[0], subs[2])))
    with pytest.raises(ValueError, match="sorted"):
        snapshot_to_rows(replace(snap, subnets=(subs[0], subs[0])))
    s92 = subs[2]
    with pytest.raises(ValueError, match="hotkeys"):
        snapshot_to_rows(replace(snap, subnets=(subs[0], subs[1], replace(s92, hotkeys=s92.hotkeys[::-1]))))


def test_decoding_fails_closed(snap: ChainSnapshot) -> None:
    rows = snapshot_to_rows(snap)
    with pytest.raises(DecodeError, match="stored digest"):                    # any altered value
        snapshot_from_rows({**rows.glob, "root_tao": rows.glob["root_tao"] + 1}, rows.subnets, rows.hotkeys)
    snapshot_from_rows({**rows.glob, "root_tao": rows.glob["root_tao"] + 1}, rows.subnets, rows.hotkeys, verify=False)
    with pytest.raises(DecodeError, match="NULL"):
        snapshot_from_rows({**rows.glob, "spec_version": None}, rows.subnets, rows.hotkeys)
    with pytest.raises(DecodeError, match="exact_json"):
        snapshot_from_rows({**rows.glob, "exact_json": "[1]"}, rows.subnets, rows.hotkeys)
    with pytest.raises(DecodeError, match="partial metagraph"):
        sub = [dict(r) for r in rows.subnets]
        sub[2]["mg_n_miners"] = None
        snapshot_from_rows(rows.glob, sub, rows.hotkeys)
    with pytest.raises(DecodeError, match="without a subnet row"):
        snapshot_from_rows(rows.glob, rows.subnets[:2], rows.hotkeys)
    with pytest.raises(DecodeError, match="reg_at"):
        hks = [dict(r) for r in rows.hotkeys]
        hks[0]["reg_at"] = 1
        snapshot_from_rows(rows.glob, rows.subnets, hks)
    with pytest.raises(DecodeError, match="PoolKind"):
        sub = [dict(r) for r in rows.subnets]
        sub[0]["pool_kind"] = 9
        snapshot_from_rows(rows.glob, sub, rows.hotkeys)
    with pytest.raises(DecodeError, match="expected bool"):
        snapshot_from_rows({**rows.glob, "shorts_enabled": 1}, rows.subnets, rows.hotkeys)


def test_to_sql_text_validates_values() -> None:
    spec = LAKE_TABLES["calib"]
    assert schema.to_sql_text(spec.col("block"), 5) == "5"
    assert schema.to_sql_text(spec.col("model"), 0.25) == "0.25"
    with pytest.raises(ValueError):
        schema.to_sql_text(spec.col("block"), -1)
    with pytest.raises(TypeError):
        schema.to_sql_text(spec.col("block"), True)
    with pytest.raises(ValueError):
        schema.to_sql_text(spec.col("model"), float("nan"))
    with pytest.raises(TypeError):
        schema.to_sql_text(LAKE_TABLES["raw_rpc"].col("response_zstd"), "abc")
    assert schema.to_sql_text(LAKE_TABLES["raw_rpc"].col("response_zstd"), b"\x01\xff") == "01ff"
