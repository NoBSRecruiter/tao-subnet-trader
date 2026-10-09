"""WP3 lake tests (DESIGN.md section 7.1; section 11 WP3 acceptance).

Snapshot -> Parquet -> snapshot is digest-identical (exact I96F32 / U64F64 / SafeFloat / TaoWeight, and the lossless
exact_json fallback); chunk writes are atomic (temp + rename + manifest row in one SQLite transaction), idempotent on
kill-and-resume and immutable; a process killed between the rename and the COMMIT leaves an invisible orphan; DuckDB
views read exactly the manifest's files.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from taotrader.core.errors import DecodeError
from taotrader.core.state import ChainSnapshot, HotkeyIdx, Quality, SubnetState
from taotrader.core.units import U64_MAX, AlphaRao, Hotkey
from taotrader.data import lake as lake_mod
from taotrader.data import schema
from taotrader.data.lake import ChunkConflict, Lake, LakeError, chunk_path, open_state_db, series_of
from taotrader.data.schema import raw_to_fixed, raw_to_ratio, snapshot_digest, with_digest

U128 = 2**128 - 1


# ------------------------------------------------------------------------------------------------ helpers
@pytest.fixture(scope="session")
def build(make_snapshot: Callable[..., ChainSnapshot], make_subnet: Callable[..., SubnetState],
          hk: Callable[[int], Hotkey]) -> Callable[..., ChainSnapshot]:
    """build(block, exact=True, quality=Quality.OK) -> a 3-subnet snapshot whose values vary with the block."""

    def make(block: int, *, exact: bool = True, quality: Quality = Quality.OK, ts: int | None = None) -> ChainSnapshot:
        hotkeys = (HotkeyIdx(hk(1), AlphaRao(5 * 10**14 + block), Decimal(f"{U128 - block}E-21"), earns=True),   # SafeFloat
                   HotkeyIdx(hk(2), AlphaRao(10**12), raw_to_fixed(18_446_744_073_709_551_617 * block, 64)))       # V1
        subs = [make_subnet(n, 8_000_000 + n, hotkeys=hotkeys if n == 92 else (), tao_flow_cum=block * (-1) ** n,
                            volume_cum=block * 10**25, quality=quality,
                            moving_price=raw_to_fixed(-(5_790_474 + block) if n == 3 else 5_790_474 + block, 32)
                            if exact else Decimal("0.0013482"),
                            root_prop=raw_to_fixed(2_057_289_908, 32) if exact else Decimal("0.479"))
                for n in (3, 51, 92)]
        g = {"moving_alpha": raw_to_fixed(1_288_490, 32), "gate_bar": raw_to_fixed(152_412_335_548_014_784 + block, 64),
             "tao_weight": raw_to_ratio(3_320_413_933_267_719_290, U64_MAX)} if exact else {}
        return with_digest(make_snapshot(block, subs, timestamp_ms=ts if ts is not None else 1_700_000_000_000 + 12_000 * block,
                                         **g))

    return make


def lake_at(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "data" / "lake")


# ------------------------------------------------------------------------------------------------ round trips
@pytest.mark.parametrize("exact", [True, False])
def test_snapshot_parquet_snapshot_round_trip_is_digest_identical(tmp_path: Path, build: Any, exact: bool) -> None:
    snaps = [build(9_240_000 + 60 * k, exact=exact) for k in range(20)]
    with lake_at(tmp_path) as lk:
        infos = lk.write_snapshots(snaps)
        assert [i.tbl for i in infos] == ["snap_global", "snap_subnet", "snap_hotkey"]
        assert [i.rows for i in infos] == [20, 60, 40]
        assert infos[0].path == "snap_global/era=C/part-9240000-9241140.parquet"
        assert (lk.root / infos[0].path).is_file() and lk.root == (tmp_path / "data" / "lake").resolve()
        assert lk.state_db == (tmp_path / "data" / "state.sqlite").resolve()      # section 7.3: data/state.sqlite
        back = lk.load_snapshots(lk.snapshot_refs())
        assert back == snaps
        assert [b.digest for b in back] == [snapshot_digest(s) for s in snaps]
        # exact Decimals survive Parquet: SafeFloat with a 39-digit mantissa, V1 U64F64, negative I96F32
        s92 = back[0].subnets[2]
        assert s92.hotkeys[0].total_shares == Decimal(f"{U128 - 9_240_000}E-21")
        assert s92.hotkeys[1].total_shares == snaps[0].subnets[2].hotkeys[1].total_shares
        assert back[0].subnets[0].moving_price == snaps[0].subnets[0].moving_price == (
            raw_to_fixed(-(5_790_474 + 9_240_000), 32) if exact else Decimal("0.0013482"))
    with lake_at(tmp_path) as lk2:                                                 # a fresh process view
        assert lk2.load_snapshots(lk2.snapshot_refs()) == snaps
        assert lk2.verify(deep=True) == []


@settings(max_examples=15)
@given(data=st.data())
def test_property_random_snapshots_round_trip_through_parquet(tmp_path_factory: pytest.TempPathFactory, build: Any,
                                                              data: st.DataObject) -> None:
    blocks = sorted(set(data.draw(st.lists(st.integers(8_486_594, 9_500_000), min_size=1, max_size=4))))
    snaps = []
    for b in blocks:
        s = build(b, exact=data.draw(st.booleans()))
        mp = data.draw(st.one_of(st.integers(-(2**127), 2**127 - 1).map(lambda r: raw_to_fixed(r, 32)),
                                 st.decimals(min_value=-(10**45), max_value=10**45, places=60)))
        shares = data.draw(st.builds(lambda m, e: Decimal(f"{m}E{e}"), st.integers(0, U128), st.integers(-60, 30)))
        s92 = s.subnets[2]
        s92 = replace(s92, moving_price=mp, hotkeys=(replace(s92.hotkeys[0], total_shares=shares), s92.hotkeys[1]))
        snaps.append(with_digest(replace(s, subnets=(*s.subnets[:2], s92), digest="")))
    with Lake(tmp_path_factory.mktemp("prop") / "lake") as lk:      # own state.sqlite per example
        lk.write_snapshots(snaps)
        assert lk.load_snapshots(lk.snapshot_refs()) == snaps


def test_snapshots_spanning_an_era_boundary_are_split(tmp_path: Path, build: Any) -> None:
    snaps = [build(b) for b in (8_486_400, 8_486_593, 8_486_594, 8_486_700)]
    with lake_at(tmp_path) as lk:
        infos = lk.write_snapshots(snaps)
        assert sorted(i.path for i in infos if i.tbl == "snap_global") == [
            "snap_global/era=B/part-8486400-8486593.parquet", "snap_global/era=C/part-8486594-8486700.parquet"]
        assert lk.load_snapshots(lk.snapshot_refs()) == snaps
        con = lk.connect()
        assert con.execute("SELECT block, era FROM v_global ORDER BY block").fetchall() == [
            (8_486_400, "B"), (8_486_593, "B"), (8_486_594, "C"), (8_486_700, "C")]
        con.close()


def test_write_snapshots_validates_input(tmp_path: Path, build: Any) -> None:
    with lake_at(tmp_path) as lk:
        assert lk.write_snapshots([]) == []
        with pytest.raises(ValueError, match="strictly increasing"):
            lk.write_snapshots([build(9_240_060), build(9_240_000)])
        with pytest.raises(ValueError, match="does not match"):
            lk.write_snapshots([replace(build(9_240_000), digest="0" * 32)])
        with pytest.raises(ValueError, match="series"):
            lk.write_snapshots([build(9_240_000)], series="Bad-Name")
        assert lk.manifest() == []


# ------------------------------------------------------------------------------------------------ immutability
def test_rewrite_is_idempotent_and_conflicts_are_refused(tmp_path: Path, build: Any) -> None:
    snaps = [build(9_240_000 + 60 * k) for k in range(5)]
    with lake_at(tmp_path) as lk:
        first = lk.write_snapshots(snaps)
        h = lk.manifest_hash()
        again = lk.write_snapshots(snaps)                           # kill-and-resume: same chunk, same content
        assert again == first and lk.manifest_hash() == h and len(lk.manifest()) == 3
        changed = [*snaps[:4], build(9_240_240, exact=False)]
        with pytest.raises(ChunkConflict):
            lk.write_snapshots(changed)
        assert lk.manifest_hash() == h
        assert lk.load_snapshots(lk.snapshot_refs()) == snaps
        # a different series is a different chunk; both stay addressable, the store picks per block
        lk.write_snapshots(changed, series="refine")
        assert len(lk.snapshot_refs()) == 10 and {r.series for r in lk.snapshot_refs()} == {"", "refine"}


def test_rewrite_restores_a_missing_or_damaged_listed_chunk(tmp_path: Path, build: Any) -> None:
    snaps = [build(9_240_000), build(9_240_060)]
    with lake_at(tmp_path) as lk:
        infos = lk.write_snapshots(snaps)
        h = lk.manifest_hash()
        (lk.root / infos[1].path).unlink()
        (lk.root / infos[2].path).write_bytes(b"garbage")
        assert len(lk.verify()) == 2
        assert lk.write_snapshots(snaps) == infos                     # same content: the files are restored
        assert lk.verify(deep=True) == [] and lk.manifest_hash() == h
        (lk.root / infos[1].path).unlink()
        with pytest.raises(ChunkConflict):                            # different content and nothing to compare with
            lk.write_snapshots([snaps[0], build(9_240_060, exact=False)])


def test_manifest_rows_follow_section_7_3(tmp_path: Path, build: Any) -> None:
    with lake_at(tmp_path) as lk:
        info = lk.write_snapshots([build(9_240_000)], decoder_version=3)[0]
        db = open_state_db(lk.state_db)
        row = db.execute("SELECT path, tbl, first_block, last_block, rows, sha256, schema_version, decoder_version, "
                         "created_wall_ts FROM manifest WHERE path = ?", (info.path,)).fetchone()
        assert row[:5] == (info.path, "snap_global", 9_240_000, 9_240_000, 1)
        assert len(row[5]) == 64 and row[6] == schema.SCHEMA_VERSION and row[7] == 3 and row[8] > 1_600_000_000
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == set(schema.SHARED_STATE_TABLES)
        db.close()


# ------------------------------------------------------------------------------------------------ atomicity / crashes
def test_failure_between_rename_and_commit_leaves_an_invisible_orphan(tmp_path: Path, build: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    snaps = [build(9_240_000), build(9_240_060)]
    with lake_at(tmp_path) as lk:
        real = os.replace
        calls: list[str] = []

        def flaky(src: Any, dst: Any) -> None:
            real(src, dst)
            calls.append(str(dst))
            if len(calls) == 2:
                raise OSError("disk vanished")

        monkeypatch.setattr(os, "replace", flaky)          # the lake module's os
        with pytest.raises(OSError, match="disk vanished"):
            lk.write_snapshots(snaps)
        monkeypatch.setattr(os, "replace", real)
        assert lk.manifest() == [] and lk.snapshot_refs() == []                # nothing visible
        orphans = sorted(p.relative_to(lk.root).as_posix() for p in lk.root.rglob("*.parquet"))
        assert len(orphans) == 2                                                # final-named, unlisted
        assert not list(lk.root.rglob("*.tmp-*"))                               # temp files cleaned
        con = lk.connect()
        assert con.execute("SELECT count(*) FROM v_global").fetchone() == (0,)  # views list manifest files only
        con.close()
        lk.write_snapshots(snaps)                                               # retry overwrites the orphans
        assert lk.load_snapshots(lk.snapshot_refs()) == snaps and lk.verify(deep=True) == []
        assert lk.gc(min_age_s=0) == []


def test_process_killed_between_rename_and_commit(tmp_path: Path, build: Any) -> None:
    snaps = [build(9_240_000 + 60 * k) for k in range(3)]
    root = tmp_path / "data" / "lake"
    blob = tmp_path / "snaps.json"
    from taotrader.core import codec
    blob.write_bytes(codec.canonical_bytes(snaps))
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent("""
        import os, sys
        from taotrader.core import codec
        from taotrader.core.state import ChainSnapshot
        from taotrader.data import lake as lake_mod
        snaps = codec.decode_bytes(list[ChainSnapshot], open(sys.argv[2], "rb").read())
        real, n = os.replace, [0]
        def killer(a, b):
            real(a, b); n[0] += 1
            if n[0] == 2: os._exit(17)                  # hard crash after two renames, before COMMIT
        lake_mod.os.replace = killer
        lake_mod.Lake(sys.argv[1]).write_snapshots(snaps)
    """), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(root), str(blob)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 17, proc.stderr
    with Lake(root) as lk:
        assert lk.manifest() == [] and lk.snapshot_refs() == [] and lk.verify() == []
        assert len(list(lk.root.rglob("*.parquet"))) == 2
        assert lk.gc(min_age_s=3600) == []                                      # young orphans are left alone
        n_tmp = len(list(lk.root.rglob("*.tmp-*")))                            # the killed writer's third temp file
        assert len(lk.gc(min_age_s=0)) == 2 + n_tmp
        assert not list(lk.root.rglob("*.parquet*"))
        lk.write_snapshots(snaps)
        assert lk.load_snapshots(lk.snapshot_refs()) == snaps


def test_a_triple_that_does_not_read_back_is_never_committed(tmp_path: Path, build: Any,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    with lake_at(tmp_path) as lk:
        real = lake_mod._assemble

        def corrupt(*a: Any, **kw: Any) -> list[ChainSnapshot]:
            return [replace(s, digest="f" * 32) for s in real(*a, **kw)]

        monkeypatch.setattr(lake_mod, "_assemble", corrupt)
        with pytest.raises(LakeError, match="round-trip"):
            lk.write_snapshots([build(9_240_000)])
        assert lk.manifest() == [] and not list(lk.root.rglob("*.parquet")) and not list(lk.root.rglob("*.tmp-*"))


# ------------------------------------------------------------------------------------------------ verify / gc / retire
def test_verify_detects_damage(tmp_path: Path, build: Any) -> None:
    with lake_at(tmp_path) as lk:
        infos = lk.write_snapshots([build(9_240_000), build(9_240_060)])
        assert lk.verify(deep=True) == []
        _g, s, h = (lk.root / i.path for i in infos)
        # replace the subnet chunk by a well-formed Parquet with one value changed (digest column untouched)
        con = duckdb.connect()
        tmp = s.with_name("x.parquet")
        con.execute(f"COPY (SELECT * REPLACE (CASE WHEN netuid = 51 THEN tempo + 1 ELSE tempo END AS tempo) "
                    f"FROM read_parquet('{s.as_posix()}')) TO '{tmp.as_posix()}' (FORMAT PARQUET)")
        con.close()
        os.replace(tmp, s)
        problems = lk.verify()
        assert problems == [f"{infos[1].path}: sha256 mismatch"]
        with pytest.raises(LakeError, match="sha256 mismatch"):                  # the cheap per-chunk file check
            lk.load_snapshots(lk.snapshot_refs())
        with Lake(lk.root, check_files=False) as raw, pytest.raises(DecodeError, match="stored digest"):
            raw.load_snapshots(raw.snapshot_refs())                             # and, independently, the digest
        h.unlink()
        assert f"{infos[2].path}: missing" in lk.verify()
        with pytest.raises(LakeError):
            lk.load_snapshots(lk.snapshot_refs())


def test_incomplete_triple_is_reported(tmp_path: Path, build: Any) -> None:
    with lake_at(tmp_path) as lk:
        infos = lk.write_snapshots([build(9_240_000)])
        db = open_state_db(lk.state_db)
        db.execute("DELETE FROM manifest WHERE tbl = 'snap_hotkey'")
        db.close()
        lk.refresh()
        assert any("incomplete triple" in p for p in lk.verify())
        with pytest.raises(LakeError, match=r"not .*in the manifest"):
            lk.load_snapshots(lk.snapshot_refs())
        assert infos[2].path in lk.gc(min_age_s=0)


def test_retire_and_replace_dimension_chunks(tmp_path: Path) -> None:
    gen: list[dict[str, Any]] = [{"netuid": 92, "reg_at": 8_355_590, "first_seen": 8_355_600, "end_block": None, "end_kind": "open"},
           {"netuid": 51, "reg_at": 7_000_000, "first_seen": 7_000_010, "end_block": 8_000_000, "end_kind": "pruned",
            "end_refined": True, "observed_payout_ratio": 0.42}]
    with lake_at(tmp_path) as lk:
        v1 = lk.write_rows("generation", gen[:1], first_block=0, last_block=9_000_000)
        assert v1.path == "generation/part-0-9000000.parquet"
        v2 = lk.write_rows("generation", gen, first_block=0, last_block=9_240_000, replaces=[v1.path])
        assert [c.path for c in lk.manifest("generation")] == [v2.path] and not (lk.root / v1.path).exists()
        con = lk.connect()
        q = "SELECT reg_at FROM v_generation WHERE netuid = ? AND first_seen <= ? AND coalesce(end_block, 1e18) > ?"
        assert con.execute(q, [51, 7_500_000, 7_500_000]).fetchall() == [(7_000_000,)]
        assert con.execute(q, [51, 8_100_000, 8_100_000]).fetchall() == []
        assert con.execute("SELECT observed_payout_ratio FROM v_generation WHERE netuid = 51").fetchone() == (0.42,)
        con.close()
        with pytest.raises(LakeError, match="not in the manifest"):
            lk.write_rows("generation", gen, first_block=1, last_block=2, replaces=["generation/part-9-9.parquet"])
        lk.retire([v2.path])
        assert lk.manifest() == [] and not (lk.root / v2.path).exists()


def test_snapshot_chunks_are_swapped_atomically_and_retired_as_triples(tmp_path: Path, build: Any) -> None:
    """A decoder-fix rebuild (section 7.1) is written under a new series and replaces the old triple in one commit."""
    old = [build(9_240_000 + 60 * k, exact=False) for k in range(3)]
    new = [build(9_240_000 + 60 * k) for k in range(3)]
    with lake_at(tmp_path) as lk:
        g_old = lk.write_snapshots(old)[0].path
        infos = lk.write_snapshots(new, series="dv2", replaces=[g_old])          # the global path stands for the triple
        assert {c.path for c in lk.manifest()} == {i.path for i in infos}
        assert not any((lk.root / f"{t}/{g_old.split('/', 1)[1]}").exists() for t in schema.SNAPSHOT_TABLES)
        assert lk.load_snapshots(lk.snapshot_refs()) == new and lk.verify(deep=True) == []
        with pytest.raises(ValueError, match="single era"):
            lk.write_snapshots([build(8_486_500), build(8_486_600)], replaces=[infos[0].path])
        lk.retire([infos[1].path])                                                 # the subnet path retires all three
        assert lk.manifest() == [] and lk.verify() == []


# ------------------------------------------------------------------------------------------------ other tables + views
def test_every_non_snapshot_table_round_trips_through_views(tmp_path: Path) -> None:
    rows: dict[str, list[dict[str, Any]]] = {
        "dividend_keys": [{"block": 9_240_000, "netuid": 92, "reg_at": 8_355_590, "hotkey": "0x" + "ab" * 32}],
        "chain_event": [{"block": 9_240_000, "kind": "large_flow", "netuid": 92, "reg_at": 8_355_590, "flag": None,
                         "amount": -(10**30), "frac_ppm": -25_000, "name": None, "old": None, "new": None}],
        "registration": [{"queued_block": 9_210_610, "victim_netuid": 51, "victim_reg_at": 7_000_000,
                          "new_reg_at": 9_210_611, "cost_ratio": 1.25, "lock_amount": 653_020_000_000,
                          "blocks_since_prev": 20_000, "shielded": False}],
        "spec_boundary": [{"spec_version": 475, "setcode_block": 9_100_000, "first_logic_block": 9_100_001}],
        "calib": [{"block": 9_240_000, "probe": "price", "netuid": 92, "model": 1.0, "chain": 1.0000001, "rel_err": 1e-7}],
        "raw_rpc": [{"block": 9_240_000, "call": "state_getStorage", "request_sha": "ab" * 32,
                     "response_zstd": b"\x28\xb5\x2f\xfd\x00"}],
        "ext_trades": [{"block": 9_240_000, "netuid": 92, "reg_at": 8_355_590, "side": "buy", "coldkey": "0x" + "cd" * 32,
                        "tao_rao": 10**9, "alpha_rao": 7 * 10**8, "extrinsic_id": "9240000-0012", "seq": 1}],
        "ext_crosscheck": [{"day": dt.date(2026, 10, 8), "netuid": 92, "reg_at": 8_355_590, "chain_price_rao": 1_400_000,
                            "ts_price_rao": 1_400_100, "rel_diff": 7e-5}],
    }
    dims = {"registration", "spec_boundary", "ext_crosscheck"}
    with lake_at(tmp_path) as lk:
        con0 = lk.connect()
        for spec in schema.LAKE_TABLES.values():                               # empty views have the right columns
            names = [d[0] for d in con0.execute(f"SELECT * FROM {spec.view}").description or []]
            assert names == list(spec.names) + (["era"] if spec.block_col else [])
        con0.close()
        for t, rs in rows.items():
            kw: dict[str, Any] = {"first_block": 0, "last_block": 10} if t in dims else {}
            info = lk.write_rows(t, rs, **kw)
            assert info.tbl == t and info.rows == 1
            assert ("era=C" in info.path) == (t not in dims)
        con = lk.connect()
        for t, rs in rows.items():
            spec = schema.LAKE_TABLES[t]
            got = con.execute(f"SELECT {', '.join(spec.names)} FROM {spec.view}").fetchall()
            assert got == [tuple(rs[0].get(c) for c in spec.names)], t
        assert con.execute("SELECT typeof(amount) FROM v_chain_event").fetchone() == ("HUGEINT",)
        con.close()


def test_write_rows_validation(tmp_path: Path) -> None:
    with lake_at(tmp_path) as lk:
        with pytest.raises(ValueError, match="unknown column"):
            lk.write_rows("calib", [{"block": 1, "bogus": 2}])
        with pytest.raises(ValueError, match="duplicate key"):
            lk.write_rows("spec_boundary", [{"spec_version": 1}, {"spec_version": 1}], first_block=0, last_block=1)
        with pytest.raises(ValueError, match="first_block"):
            lk.write_rows("generation", [{"netuid": 1, "reg_at": 2}])
        with pytest.raises(ValueError, match="spans eras"):
            lk.write_rows("calib", [{"block": 8_486_000}, {"block": 8_486_600}])
        with pytest.raises(ValueError, match="triple"):
            lk.write_rows("snap_global", [])
        with pytest.raises(ValueError, match="integer block"):
            lk.write_rows("calib", [{"probe": "x"}])
        with pytest.raises(TypeError):
            lk.write_rows("calib", [{"block": 1, "model": "x"}])
        assert lk.manifest() == []
        info = lk.write_rows("calib", [], first_block=9_000_000, last_block=9_000_060)     # empty chunk is fine
        assert info.rows == 0


def test_chunk_paths_and_series() -> None:
    assert chunk_path("snap_subnet", 9_240_000, 9_243_540, "live") == "snap_subnet/era=C/part-9240000-9243540-live.parquet"
    assert chunk_path("spec_boundary", 0, 475) == "spec_boundary/part-0-475.parquet"
    assert series_of("snap_subnet/era=C/part-1-2-live.parquet") == "live" and series_of("x/part-1-2.parquet") == ""
    with pytest.raises(ValueError):
        chunk_path("calib", 5, 4)
    with pytest.raises(LakeError):
        series_of("x/other.parquet")


def test_views_show_new_chunks_only_after_refresh_in_another_instance(tmp_path: Path, build: Any) -> None:
    with lake_at(tmp_path) as writer, lake_at(tmp_path) as reader:
        writer.write_snapshots([build(9_240_000)])
        assert reader.snapshot_refs() == []
        assert reader.refresh() is True and len(reader.snapshot_refs()) == 1
        assert reader.refresh() is False
        con = reader.connect()
        assert con.execute("SELECT count(*), min(typeof(px_tao)) FROM v_subnet").fetchone() == (3, "HUGEINT")
        assert con.execute("SELECT count(*) FROM v_hotkey WHERE exact_json IS NOT NULL").fetchone() == (2,)
        con.close()
        assert reader.manifest_hash() == writer.manifest_hash()


def test_atomic_run_output_writer(tmp_path: Path) -> None:
    """Section 7.4 run outputs (data/runs/<run_id>/out/*.parquet) are written atomically with exact integers."""
    out = tmp_path / "runs" / "r1" / "out" / "nav.parquet"
    cols = [("book", "VARCHAR"), ("block", "UBIGINT"), ("cash", "HUGEINT"), ("nav_liq", "HUGEINT"), ("ok", "BOOLEAN")]
    rows = [{"book": "carry", "block": 9_240_060, "cash": -(10**30), "nav_liq": 10**37, "ok": True},
            {"book": "carry", "block": 9_240_000, "cash": 5, "nav_liq": 6, "ok": None}]
    sha = lake_mod.write_parquet_atomic(out, cols, rows, sort=("book", "block"))
    assert len(sha) == 64 and not list(out.parent.glob("*.tmp-*"))
    con = duckdb.connect()
    got = con.execute(f"SELECT book, block, CAST(cash AS HUGEINT), CAST(nav_liq AS HUGEINT), ok "
                      f"FROM read_parquet('{out.as_posix()}')").fetchall()
    con.close()
    assert got == [("carry", 9_240_000, 5, 6, None), ("carry", 9_240_060, -(10**30), 10**37, True)]
    before = out.read_bytes()
    with pytest.raises(ValueError):
        lake_mod.write_parquet_atomic(out, cols, [{"book": "x", "block": -1}])          # nothing replaced on error
    with pytest.raises(ValueError, match="unknown column"):
        lake_mod.write_parquet_atomic(out, cols, [{"bogus": 1}])
    with pytest.raises(ValueError, match="unsupported"):
        lake_mod.write_parquet_atomic(out, [("x", "FLOAT")], [])
    assert out.read_bytes() == before and not list(out.parent.glob("*.tmp-*"))


# ------------------------------------------------------------------------------------------------ real chain values
def _le(h: str | None, signed: bool = False) -> int:
    assert h is not None
    return int.from_bytes(bytes.fromhex(h[2:]), "little", signed=signed)


def _safefloat(h: str) -> Decimal:
    b = bytes.fromhex(h[2:])
    assert len(b) == 24
    return Decimal(f"{int.from_bytes(b[:16], 'little')}E{int.from_bytes(b[16:], 'little', signed=True)}")


def test_golden_sn92_values_round_trip_exactly_without_fallback(tmp_path: Path, golden: Any, make_snapshot: Any,
                                                                make_globals: Any) -> None:
    """Section 11 WP3: Decimal exactness of I96F32 / U96F32 / U64F64 / SafeFloat / TaoWeight, with the raw storage
    values captured at block 9,240,388 (tests/fixtures/golden/sn92_9240388.json): every one fits its section 7.1
    column exactly (no exact_json fallback) and survives Parquet bit-for-bit."""
    from taotrader.core.state import PoolKind, PoolState
    from taotrader.core.units import Block, NetUid, Rao, SubnetKey
    from taotrader.data.schema import snapshot_to_rows
    g = golden("sn92_9240388")["snapshots"][0]
    v: dict[str, Any] = {}
    for e in g["storage"]:
        v.setdefault(e["item"].split(".", 1)[1], e["value"])
    hk_alpha, hk_shares = _le(v["TotalHotkeyAlpha"]), _safefloat(v["TotalHotkeySharesV2"])
    owner_hk = Hotkey("0x" + next(e for e in g["storage"] if e["item"].endswith("TotalHotkeyAlpha"))["args"][0][2:66])
    tao, alpha_in = _le(v["SubnetTAO"]), _le(v["SubnetAlphaIn"])
    s92 = SubnetState(
        key=SubnetKey(NetUid(92), Block(_le(v["NetworkRegisteredAt"]))),
        pool=PoolState(PoolKind.BALANCER, Rao(tao), AlphaRao(alpha_in), tao, alpha_in, _le(v["SwapBalancer"]), 33),
        alpha_out=AlphaRao(_le(v["SubnetAlphaOut"])), protocol_alpha=AlphaRao(_le(v["SubnetProtocolAlpha"])),
        moving_price=raw_to_fixed(_le(v["SubnetMovingPrice"], signed=True), 32),
        root_prop=raw_to_fixed(_le(v["RootProp"]), 32), miner_burned=raw_to_fixed(_le(v["MinerBurned"]), 32),
        emission_enabled=True, subtoken_enabled=True, reg_allowed=True,
        first_emission_block=Block(_le(v["FirstEmissionBlockNumber"])), tempo=_le(v["Tempo"]),
        last_epoch_block=Block(_le(v["LastEpochBlock"])), ema_halving_blocks=201_600,
        tao_in_emission=Rao(_le(v["SubnetTaoInEmission"])), excess_tao=Rao(_le(v["SubnetExcessTao"])),
        alpha_out_emission=AlphaRao(_le(v["SubnetAlphaOutEmission"])), alpha_in_emission=AlphaRao(_le(v["SubnetAlphaInEmission"])),
        tao_flow_cum=_le(v["SubnetTaoFlow"], signed=True), volume_cum=_le(v["SubnetVolume"]),
        fast_moving_price=raw_to_fixed(_le(v["SubnetFastMovingPrice"]), 64),
        total_alpha_staked=AlphaRao(_le(v["TotalAlphaStaked"])), max_allowed_validators=_le(v["MaxAllowedValidators"]),
        hotkeys=(HotkeyIdx(owner_hk, AlphaRao(hk_alpha), hk_shares, take_u16=_le(v["Delegates"]), earns=True,
                           last_dividend=AlphaRao(_le(v["AlphaDividendsPerSubnet"]))),))
    glob = make_globals(moving_alpha=raw_to_fixed(_le(v["SubnetMovingAlpha"], signed=True), 32),
                        gate_bar=raw_to_fixed(_le(v["EmissionGateBar"]), 64),
                        tao_weight=raw_to_ratio(_le(v["TaoWeight"]), U64_MAX), total_issuance=Rao(_le(v["TotalIssuance"])))
    snap = with_digest(make_snapshot(g["block"], [s92], glob=glob, timestamp_ms=_le(v["Now"])))
    assert hk_shares == Decimal(_le(v["TotalHotkeySharesV2"][:34])) * Decimal(10) ** -8
    assert s92.tao_flow_cum is not None and s92.tao_flow_cum < 0                  # a negative i64 survives too
    rows = snapshot_to_rows(snap)
    assert rows.glob["exact_json"] is None and rows.subnets[0]["exact_json"] is None and rows.hotkeys[0]["exact_json"] is None
    assert rows.subnets[0]["moving_price_raw"] == _le(v["SubnetMovingPrice"], signed=True)
    assert rows.glob["tao_weight_raw"] == _le(v["TaoWeight"]) and rows.glob["gate_bar_raw"] == _le(v["EmissionGateBar"])
    with lake_at(tmp_path) as lk:
        lk.write_snapshots([snap])
        back = lk.load_snapshots(lk.snapshot_refs())[0]
        assert back == snap and back.digest == snapshot_digest(snap)
        assert back.subnets[0].hotkeys[0].total_shares == hk_shares
        assert back.subnets[0].moving_price == s92.moving_price and back.glob.tao_weight == glob.tao_weight
