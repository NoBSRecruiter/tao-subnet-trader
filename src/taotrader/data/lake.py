"""taotrader/data/lake.py - the Parquet market-data lake (DESIGN.md section 7.1).

Layout: <root>/<table>/era=<A|B|C>/part-<first>-<last>[-<series>].parquet for block-keyed tables and
<root>/<table>/part-<first>-<last>[-<series>].parquet for the dimension tables (generation, registration,
spec_boundary, ext_crosscheck), with the manifest in the shared `data/state.sqlite` (section 7.3).

Atomic chunk write (section 11 WP3): the rows are COPYed by an in-memory DuckDB into `<final>.tmp-<pid>-<tid>`
(ZSTD Parquet, deterministic bytes), fsynced, then under `BEGIN IMMEDIATE` on state.sqlite each temp file is renamed
onto its final path and its manifest row is inserted; COMMIT ends the write. A crash before COMMIT leaves at most a
final-named orphan file without a manifest row. Readers only ever open manifest-listed files, so an orphan is
invisible; a retry overwrites it, and `gc()` removes stale ones. Chunks are immutable: re-writing an existing path
with the same content is a no-op (kill-and-resume; an identical rewrite also restores a listed file that went missing
or was damaged), different content raises ChunkConflict. Dimension tables are replaced by writing a new chunk with
`replaces=(old_path,)` (one transaction).

A snapshot chunk is a triple (snap_global, snap_subnet, snap_hotkey) sharing one relative stem, committed together,
and verified before commit: the temp files are read back and every snapshot must rebuild digest-identically.

Readers: `connect()` returns an in-memory DuckDB connection whose views (v_global, v_subnet, v_hotkey, v_<table>)
read exactly the manifest's files (no file locks; any number of readers and processes). HUGEINT columns are stored
as DECIMAL(38,0) and cast back by the views. `refresh()` picks up chunks committed by other processes.
`load_snapshots` checks each chunk's sha256 against its manifest row once per (path, size, mtime) (check_files) and,
with verify=True, recomputes every snapshot digest.

Throughput on Windows (128 subnets x 4 tracked hotkeys per snapshot, one core): write ~47 ms/snapshot including
the read-back verification; read ~10 ms/snapshot, ~18 ms with digest verification.
"""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import numpy as np

from ..core.errors import DecodeError
from ..core.state import ChainSnapshot
from . import schema
from .schema import LAKE_TABLES, SNAPSHOT_TABLES, TableSpec

SERIES_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]{1,32}")
_PART_RE: Final[re.Pattern[str]] = re.compile(r"part-(\d+)-(\d+)(?:-([a-z0-9_]{1,32}))?\.parquet")
_DELETE_RETRIES: Final[int] = 8


class LakeError(Exception):
    """The lake or its manifest is inconsistent, or a request cannot be served."""


class ChunkConflict(LakeError):
    """A chunk path already exists in the manifest with different content (chunks are never edited)."""


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    """One manifest row (section 7.3)."""
    path: str                 # relative posix path under the lake root
    tbl: str
    first_block: int
    last_block: int
    rows: int
    sha256: str
    schema_version: int
    decoder_version: int
    created_wall_ts: int


@dataclass(frozen=True, slots=True)
class LakeSnapRef:
    """A stored snapshot: one snap_global row, located by its chunk stem (shared by the three snapshot tables)."""
    block: int
    digest: str
    quality_or: int
    stem: str                 # e.g. "era=C/part-8486640-8492580.parquet"
    series: str               # "" = base collector series; "live" = recorder; others (e.g. "refine") by writer


@dataclass(slots=True)
class _Pending:
    spec: TableSpec
    path: str
    rows: Sequence[Mapping[str, Any]]
    first: int
    last: int
    tmp: Path | None = None
    sha256: str = ""
    skip: bool = False                 # identical chunk already listed and intact: nothing to do
    repair: bool = False               # identical chunk listed but its file is missing/damaged: restore the file


def series_of(path: str) -> str:
    m = _PART_RE.fullmatch(path.rsplit("/", 1)[-1])
    if m is None:
        raise LakeError(f"not a chunk path: {path}")
    return m.group(3) or ""


def chunk_path(table: str, first: int, last: int, series: str = "") -> str:
    """Relative path of a chunk (section 7.1 layout)."""
    spec = LAKE_TABLES[table]
    if series and SERIES_RE.fullmatch(series) is None:
        raise ValueError(f"bad series name {series!r} (expected [a-z0-9_]{{1,32}})")
    if first < 0 or last < first:
        raise ValueError(f"bad chunk range {first}..{last}")
    name = f"part-{first}-{last}{'-' + series if series else ''}.parquet"
    if spec.block_col is None:
        return f"{table}/{name}"
    era = schema.era_of(first)
    if schema.era_of(last) != era:
        raise ValueError(f"{table} chunk {first}..{last} spans eras; split it at the era boundary")
    return f"{table}/era={era}/{name}"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_list(paths: Iterable[str]) -> str:
    return "[" + ", ".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"


def select_list(spec: TableSpec) -> str:
    """Logical-type select list over a Parquet chunk (HUGEINT cast back from DECIMAL(38,0))."""
    return ", ".join(f"CAST({_q(c.name)} AS HUGEINT) AS {_q(c.name)}" if c.type == "HUGEINT" else _q(c.name)
                     for c in spec.cols)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":            # Windows cannot open a directory for fsync; NTFS journals the rename itself
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unlink_with_retry(path: Path, retries: int = _DELETE_RETRIES) -> bool:
    """Delete a file; on Windows a reader holding it open makes unlink fail, so retry briefly, then give up (False)."""
    for i in range(retries):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            time.sleep(0.02 * (i + 1))
    return False


def open_state_db(path: str | Path, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """data/state.sqlite (section 7.3 shared tables: fetch_ledger, manifest, endpoint_health), WAL + synchronous=FULL."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, isolation_level=None, check_same_thread=False, timeout=busy_timeout_ms / 1000)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    stmts = schema.create_table_statements(schema.STATE_DDL)
    for name in schema.SHARED_STATE_TABLES:
        db.execute(schema.if_not_exists(stmts[name]))
    return db


_MANIFEST_COLS: Final[str] = ("path, tbl, first_block, last_block, rows, sha256, schema_version, decoder_version, "
                              "created_wall_ts")


class Lake:
    """Parquet chunks + manifest + in-memory DuckDB views. Thread-safe; one instance per process is enough."""

    def __init__(self, root: str | Path, state_db: str | Path | None = None, *, busy_timeout_ms: int = 30_000,
                 duckdb_threads: int = 1, check_files: bool = True) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_db = Path(state_db).resolve() if state_db is not None else self.root.parent / "state.sqlite"
        self._threads = duckdb_threads
        self._lock = threading.RLock()
        self._db = open_state_db(self.state_db, busy_timeout_ms)
        self._duck: duckdb.DuckDBPyConnection | None = None
        self._manifest: dict[str, ChunkInfo] = {}
        self._refs: dict[str, list[LakeSnapRef]] = {}       # snap_global chunk path -> its snapshot refs
        self.index_errors: dict[str, str] = {}               # snap_global chunk path -> why it could not be indexed
        self._refs_sorted: tuple[int, list[LakeSnapRef]] | None = None
        self.check_files = check_files                       # sha256 of each chunk vs its manifest row, once per path
        self._sha_ok: dict[str, tuple[str, int, int]] = {}   # path -> (sha256, size, mtime_ns) verified by this instance
        self.version = 0                                     # bumps whenever the manifest content changes
        self.refresh()

    # ------------------------------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            if self._duck is not None:
                self._duck.close()
                self._duck = None
            self._db.close()

    def __enter__(self) -> Lake:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _reader(self) -> duckdb.DuckDBPyConnection:
        if self._duck is None:
            self._duck = duckdb.connect(":memory:", config={"threads": self._threads})
        return self._duck

    def abspath(self, rel: str) -> str:
        return (self.root / rel).as_posix()

    # ------------------------------------------------------------------------------------------ manifest
    def refresh(self) -> bool:
        """Re-read the manifest (other processes may have committed chunks). Returns True if it changed."""
        with self._lock:
            rows = self._db.execute(f"SELECT {_MANIFEST_COLS} FROM manifest").fetchall()
            new = {r[0]: ChunkInfo(*r) for r in rows}
            if new == self._manifest:
                return False
            self._manifest = new
            self.version += 1
            g_paths = {p for p, c in new.items() if c.tbl == "snap_global"}
            for p in [p for p in self._refs if p not in g_paths]:
                del self._refs[p]
                self.index_errors.pop(p, None)
            missing = sorted(p for p in g_paths if p not in self._refs)
            if missing:
                self._index_snapshot_chunks(missing)
            return True

    def manifest(self, tbl: str | None = None) -> list[ChunkInfo]:
        with self._lock:
            return sorted((c for c in self._manifest.values() if tbl is None or c.tbl == tbl), key=lambda c: c.path)

    def manifest_hash(self) -> str:
        """sha256 over the sorted (path, sha256) pairs: the run identity's data manifest hash (section 8)."""
        h = hashlib.sha256()
        for c in self.manifest():
            h.update(f"{c.path}\t{c.sha256}\n".encode())
        return h.hexdigest()

    # ------------------------------------------------------------------------------------------ writing
    def write_rows(self, table: str, rows: Sequence[Mapping[str, Any]], *, first_block: int | None = None,
                   last_block: int | None = None, series: str = "", decoder_version: int = 0,
                   replaces: Sequence[str] = ()) -> ChunkInfo:
        """Write one immutable chunk of a non-snapshot table. Block-keyed tables take first/last from the rows
        (explicit values may widen the range); dimension tables need explicit first_block/last_block."""
        if table not in LAKE_TABLES:
            raise ValueError(f"unknown lake table {table!r}")
        if table in SNAPSHOT_TABLES:
            raise ValueError("snapshot tables are written as a triple by write_snapshots()")
        spec = LAKE_TABLES[table]
        names = set(spec.names)
        for i, r in enumerate(rows):
            unknown = sorted(k for k in r if k not in names)
            if unknown:
                raise ValueError(f"{table} row {i}: unknown column(s) {unknown}")
        _check_unique(spec, rows)
        if spec.block_col is not None:
            blocks = [r.get(spec.block_col) for r in rows]
            if any(not isinstance(b, int) or isinstance(b, bool) for b in blocks):
                raise ValueError(f"{table}: every row needs an integer {spec.block_col}")
            ints = [int(b) for b in blocks if isinstance(b, int)]
            lo = min(ints) if ints else first_block
            hi = max(ints) if ints else last_block
            if lo is None or hi is None:
                raise ValueError(f"{table}: an empty chunk needs first_block and last_block")
            first = lo if first_block is None else first_block
            last = hi if last_block is None else last_block
            if first > lo or last < hi:
                raise ValueError(f"{table}: rows span {lo}..{hi}, outside first/last {first}..{last}")
        else:
            if first_block is None or last_block is None:
                raise ValueError(f"{table} is not block-keyed: pass first_block and last_block (e.g. a key range)")
            first, last = first_block, last_block
        pending = [_Pending(spec, chunk_path(table, first, last, series), rows, first, last)]
        return self._commit(pending, decoder_version=decoder_version, replaces=replaces)[0]

    def write_snapshots(self, snaps: Sequence[ChainSnapshot], *, series: str = "",
                        decoder_version: int = schema.DEFAULT_DECODER_VERSION,
                        escrow_block: Mapping[tuple[int, int], int] | None = None,
                        shares_src: Mapping[tuple[int, int, str], int] | None = None,
                        verify: bool = True, replaces: Sequence[str] = ()) -> list[ChunkInfo]:
        """Write snapshots (strictly increasing blocks) as one snapshot-chunk triple per era. escrow_block is keyed
        (block, netuid) and shares_src (block, netuid, hotkey). verify=True reads the temp files back before COMMIT
        and requires every snapshot to rebuild digest-identically. replaces= retires chunks in the same transaction
        (e.g. a decoder-fix rebuild written under a new series); a snapshot path stands for its whole triple, and the
        snapshots must then fit in one era."""
        if not snaps:
            if replaces:
                raise ValueError("replaces= needs snapshots to write (use retire())")
            return []
        blocks = [int(s.block) for s in snaps]
        if any(b2 <= b1 for b1, b2 in itertools.pairwise(blocks)):
            raise ValueError("write_snapshots needs strictly increasing blocks")
        groups: list[list[ChainSnapshot]] = []
        for s in snaps:
            if groups and schema.era_of(int(groups[-1][0].block)) == schema.era_of(int(s.block)):
                groups[-1].append(s)
            else:
                groups.append([s])
        if replaces and len(groups) > 1:
            raise ValueError("replaces= needs snapshots of a single era (one chunk triple)")
        esc_by_block: dict[int, dict[int, int]] = {}
        for (bb, n), v in (escrow_block or {}).items():
            esc_by_block.setdefault(bb, {})[n] = v
        src_by_block: dict[int, dict[tuple[int, str], int]] = {}
        for (bb, n, h), v in (shares_src or {}).items():
            src_by_block.setdefault(bb, {})[(n, h)] = v
        out: list[ChunkInfo] = []
        for grp in groups:
            g_rows: list[dict[str, Any]] = []
            s_rows: list[dict[str, Any]] = []
            h_rows: list[dict[str, Any]] = []
            digests: dict[int, str] = {}
            for s in grp:
                b = int(s.block)
                rows = schema.snapshot_to_rows(s, decoder_version=decoder_version,
                                               escrow_block=None if escrow_block is None else esc_by_block.get(b, {}),
                                               shares_src=None if shares_src is None else src_by_block.get(b, {}))
                g_rows.append(rows.glob)
                s_rows.extend(rows.subnets)
                h_rows.extend(rows.hotkeys)
                digests[b] = rows.glob["digest"]
            first, last = int(grp[0].block), int(grp[-1].block)
            stem = chunk_path("snap_global", first, last, series).split("/", 1)[1]
            pending = [_Pending(LAKE_TABLES[t], f"{t}/{stem}", rws, first, last)
                       for t, rws in (("snap_global", g_rows), ("snap_subnet", s_rows), ("snap_hotkey", h_rows))]
            check: Callable[[list[_Pending]], None] | None = None
            if verify:
                def check(ps: list[_Pending], expect: dict[int, str] = digests) -> None:
                    self._verify_tmp_triple(ps, expect)
            out.extend(self._commit(pending, decoder_version=decoder_version, verify=check,
                                    replaces=_with_triples(replaces)))
        return out

    def retire(self, paths: Sequence[str]) -> None:
        """Remove chunks from the manifest (one transaction), then delete their files (best effort on Windows). A
        snapshot-table path stands for its whole triple."""
        self._commit([], decoder_version=0, replaces=_with_triples(paths))

    # ------------------------------------------------------------------------------------------ commit protocol
    def _write_tmp(self, p: _Pending) -> None:
        final = self.root / p.path
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        _rows_to_parquet(p.spec, p.rows, tmp)
        p.tmp = tmp
        p.sha256 = _sha256(tmp)

    def _same_content(self, a: Path, b: Path) -> bool:
        con = duckdb.connect(":memory:", config={"threads": 1})
        try:
            la, lb = _sql_list([a.as_posix()]), _sql_list([b.as_posix()])
            row = con.execute(f"SELECT (SELECT count(*) FROM read_parquet({la})), (SELECT count(*) FROM read_parquet({lb})), "
                              f"(SELECT count(*) FROM (SELECT * FROM read_parquet({la}) EXCEPT ALL "
                              f"SELECT * FROM read_parquet({lb})))").fetchone()
        finally:
            con.close()
        return row is not None and row[0] == row[1] and row[2] == 0

    def _commit(self, pending: list[_Pending], *, decoder_version: int, replaces: Sequence[str] = (),
                verify: Callable[[list[_Pending]], None] | None = None) -> list[ChunkInfo]:
        paths = [p.path for p in pending]
        if len(set(paths)) != len(paths) or set(paths) & set(replaces):
            raise ValueError("duplicate or self-replacing chunk paths")
        try:
            for p in pending:
                self._write_tmp(p)
            if verify is not None:
                verify(pending)
            with self._lock:
                infos = self._commit_manifest(pending, decoder_version, replaces)
        finally:
            for p in pending:
                if p.tmp is not None:
                    unlink_with_retry(p.tmp)
        for rel in replaces:
            unlink_with_retry(self.root / rel)
        self.refresh()
        return infos

    def _existing_ok(self, p: _Pending, existing: ChunkInfo) -> bool:
        """An already-listed path: True if the new temp file has the same content (idempotent rewrite; p.repair is set
        when the listed file is missing or damaged and the identical new file must restore it). False = conflict."""
        assert p.tmp is not None
        final = self.root / p.path
        intact = final.is_file() and _sha256(final) == existing.sha256
        if existing.sha256 == p.sha256:
            p.repair = not intact
            return True
        if not intact:
            return False                         # nothing trustworthy to compare with
        try:
            return self._same_content(final, p.tmp)
        except duckdb.Error:
            return False

    def _commit_manifest(self, pending: list[_Pending], decoder_version: int, replaces: Sequence[str]) -> list[ChunkInfo]:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            infos: list[ChunkInfo] = []
            for p in pending:
                row = db.execute(f"SELECT {_MANIFEST_COLS} FROM manifest WHERE path = ?", (p.path,)).fetchone()
                if row is not None:
                    existing = ChunkInfo(*row)
                    if not self._existing_ok(p, existing):
                        raise ChunkConflict(f"{p.path} exists with different content (chunks are never edited)")
                    p.skip = not p.repair
                    infos.append(existing)
                    continue
                infos.append(ChunkInfo(p.path, p.spec.name, p.first, p.last, len(p.rows), p.sha256, schema.SCHEMA_VERSION,
                                       decoder_version, int(time.time())))
            for rel in replaces:
                if db.execute("SELECT 1 FROM manifest WHERE path = ?", (rel,)).fetchone() is None:
                    raise LakeError(f"cannot replace {rel}: not in the manifest")
            dirs: set[Path] = set()
            for p, info in zip(pending, infos, strict=True):
                if p.skip:
                    continue
                assert p.tmp is not None
                final = self.root / p.path
                os.replace(p.tmp, final)
                p.tmp = None
                dirs.add(final.parent)
                if p.repair:                     # same sha256 as the listed row: the manifest stays as it is
                    continue
                db.execute(f"INSERT INTO manifest ({_MANIFEST_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (info.path, info.tbl, info.first_block, info.last_block, info.rows, info.sha256,
                            info.schema_version, info.decoder_version, info.created_wall_ts))
            for d in sorted(dirs):
                _fsync_dir(d)
            for rel in replaces:
                db.execute("DELETE FROM manifest WHERE path = ?", (rel,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return infos

    def _verify_tmp_triple(self, ps: list[_Pending], expect: dict[int, str]) -> None:
        tmp = {p.spec.name: p.tmp for p in ps}
        rows: dict[str, list[dict[str, Any]]] = {}
        for t in SNAPSHOT_TABLES:
            f = tmp.get(t)
            if f is None:
                raise LakeError(f"snapshot chunk: no temp file for {t}")
            rows[t] = self._fetch(t, [f.as_posix()], None, None)
        snaps = _assemble(rows["snap_global"], rows["snap_subnet"], rows["snap_hotkey"], verify=True)
        got = {int(s.block): s.digest for s in snaps}
        if got != expect:
            raise LakeError("snapshot chunk does not round-trip digest-identically; nothing was committed")

    # ------------------------------------------------------------------------------------------ reading
    def _fetch(self, table: str, files: Sequence[str], lo: int | None, hi: int | None) -> list[dict[str, Any]]:
        spec = LAKE_TABLES[table]
        if not files:
            return []
        where = ""
        params: list[Any] = [list(files)]
        if lo is not None and hi is not None and spec.block_col is not None:
            where = f" WHERE {_q(spec.block_col)} BETWEEN ? AND ?"
            params += [lo, hi]
        order = ", ".join(_q(c) for c in spec.sort)
        with self._lock:
            try:
                cur = self._reader().execute(f"SELECT {select_list(spec)} FROM read_parquet(?){where} ORDER BY {order}",
                                             params)
                names = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall()
            except duckdb.Error as e:                # missing/unreadable file (retired or deleted meanwhile)
                raise LakeError(f"cannot read {table} chunk(s) {list(files)[:3]}: {e}") from e
            return [dict(zip(names, r, strict=True)) for r in rows]

    def _index_snapshot_chunks(self, g_paths: Sequence[str]) -> None:
        """Index snap_global chunks one by one; an unreadable chunk is recorded in index_errors (verify() reports
        it) and contributes no snapshots, so one bad file never makes the whole lake unopenable."""
        with self._lock:
            for rel in g_paths:
                try:
                    rows = self._reader().execute(
                        "SELECT block, digest, quality_or FROM read_parquet(?) ORDER BY block", [[self.abspath(rel)]]
                    ).fetchall()
                except duckdb.Error as e:
                    self.index_errors[rel] = str(e)
                    self._refs[rel] = []
                    continue
                self.index_errors.pop(rel, None)
                stem, series = rel.split("/", 1)[1], series_of(rel)
                self._refs[rel] = [LakeSnapRef(int(b), str(d), int(q or 0), stem, series) for b, d, q in rows]

    def snapshot_refs(self) -> list[LakeSnapRef]:
        """Every stored snapshot (duplicates across chunks included), sorted by (block, stem). Cached per manifest
        version; callers must not mutate the returned list."""
        with self._lock:
            if self._refs_sorted is None or self._refs_sorted[0] != self.version:
                refs = sorted((r for rs in self._refs.values() for r in rs), key=lambda r: (r.block, r.stem))
                self._refs_sorted = (self.version, refs)
            return self._refs_sorted[1]

    def load_snapshots(self, refs: Sequence[LakeSnapRef], *, verify: bool = True) -> list[ChainSnapshot]:
        """Decode the given refs (in their order). verify=True recomputes every digest (DecodeError on mismatch)."""
        by_stem: dict[str, set[int]] = {}
        for r in refs:
            by_stem.setdefault(r.stem, set()).add(r.block)
        built: dict[tuple[str, int], ChainSnapshot] = {}
        for stem, blocks in sorted(by_stem.items()):
            with self._lock:
                listed = all(f"{t}/{stem}" in self._manifest for t in SNAPSHOT_TABLES)
            if not listed:
                raise LakeError(f"snapshot chunk {stem} is not (or no longer) in the manifest")
            for t in SNAPSHOT_TABLES:
                self._check_file(f"{t}/{stem}")
            lo, hi = min(blocks), max(blocks)
            rows = {t: [r for r in self._fetch(t, [self.abspath(f"{t}/{stem}")], lo, hi) if r["block"] in blocks]
                    for t in SNAPSHOT_TABLES}
            for s in _assemble(rows["snap_global"], rows["snap_subnet"], rows["snap_hotkey"], verify=verify):
                built[(stem, int(s.block))] = s
        out: list[ChainSnapshot] = []
        for r in refs:
            got = built.get((r.stem, r.block))
            if got is None:
                raise LakeError(f"snapshot {r.block} missing from chunk {r.stem}")
            if got.digest != r.digest:
                raise DecodeError(f"snapshot {r.block} in {r.stem}: digest {got.digest} != indexed {r.digest}")
            out.append(got)
        return out

    def _check_file(self, rel: str) -> None:
        """Integrity of a committed chunk: its sha256 must equal the manifest row. Hashed once per (path, sha, size,
        mtime) by this instance, so a long-lived reader still notices a file changed after its first check (a cheap
        guard against corruption or tampering after commit, independent of the digest check of verify=)."""
        if not self.check_files:
            return
        with self._lock:
            info = self._manifest.get(rel)
        if info is None:
            raise LakeError(f"{rel} is not in the manifest")
        path = self.root / rel
        try:
            st = path.stat()
            key = (info.sha256, st.st_size, st.st_mtime_ns)
            with self._lock:
                if self._sha_ok.get(rel) == key:
                    return
            got = _sha256(path)
        except OSError as e:
            raise LakeError(f"{rel}: unreadable: {e}") from e
        if got != info.sha256:
            raise LakeError(f"{rel}: sha256 mismatch with the manifest (changed after commit)")
        with self._lock:
            self._sha_ok[rel] = key

    def connect(self, *, threads: int | None = None) -> duckdb.DuckDBPyConnection:
        """A new in-memory DuckDB connection with one view per lake table over the manifest's files (caller closes)."""
        con = duckdb.connect(":memory:", config={"threads": threads if threads is not None else max(1, self._threads)})
        for sql in self.view_sql():
            con.execute(sql)
        return con

    def view_sql(self) -> list[str]:
        out: list[str] = []
        for spec in LAKE_TABLES.values():
            files = [self.abspath(c.path) for c in self.manifest(spec.name)]
            part = spec.block_col is not None
            if files:
                src = f"read_parquet({_sql_list(files)}, hive_partitioning = {'true' if part else 'false'})"
                sel = select_list(spec) + (", era" if part else "")
                out.append(f"CREATE OR REPLACE VIEW {spec.view} AS SELECT {sel} FROM {src}")
            else:
                sel = ", ".join(f"CAST(NULL AS {c.type}) AS {_q(c.name)}" for c in spec.cols)
                sel += ", CAST(NULL AS VARCHAR) AS era" if part else ""
                out.append(f"CREATE OR REPLACE VIEW {spec.view} AS SELECT {sel} WHERE false")
        return out

    # ------------------------------------------------------------------------------------------ maintenance
    def verify(self, *, deep: bool = False) -> list[str]:
        """Problems found (empty = healthy): missing files, sha256 or row-count mismatches, incomplete snapshot
        triples; deep=True also decodes every snapshot and checks its digest."""
        self.refresh()
        problems: list[str] = [f"{p}: not indexable: {e}" for p, e in sorted(self.index_errors.items())]
        chunks = self.manifest()
        for c in chunks:
            f = self.root / c.path
            if not f.is_file():
                problems.append(f"{c.path}: missing")
                continue
            if _sha256(f) != c.sha256:
                problems.append(f"{c.path}: sha256 mismatch")
                continue
            with self._lock:
                row = self._reader().execute("SELECT count(*) FROM read_parquet(?)", [[f.as_posix()]]).fetchone()
            if row is None or int(row[0]) != c.rows:
                problems.append(f"{c.path}: row count {None if row is None else row[0]} != manifest {c.rows}")
        stems: dict[str, set[str]] = {}
        for c in chunks:
            if c.tbl in SNAPSHOT_TABLES:
                stems.setdefault(c.path.split("/", 1)[1], set()).add(c.tbl)
        for stem, tbls in sorted(stems.items()):
            if tbls != set(SNAPSHOT_TABLES):
                problems.append(f"snapshot chunk {stem}: incomplete triple {sorted(tbls)}")
        if deep and not problems:
            for stem in sorted(stems):
                refs = [r for r in self.snapshot_refs() if r.stem == stem]
                try:
                    self.load_snapshots(refs, verify=True)
                except (DecodeError, LakeError) as e:
                    problems.append(f"snapshot chunk {stem}: {e}")
        return problems

    def gc(self, *, min_age_s: float = 3600.0) -> list[str]:
        """Delete files under the lake root that no manifest row lists (crash orphans, stale temp files) and that are
        older than min_age_s (so an in-flight write of another process is never touched). Returns deleted paths."""
        self.refresh()
        listed = {self.abspath(c.path) for c in self.manifest()}
        now = time.time()
        deleted: list[str] = []
        for f in sorted(self.root.rglob("*")):
            if not f.is_file() or ".parquet" not in f.name:
                continue
            if f.as_posix() in listed:
                continue
            with contextlib.suppress(FileNotFoundError):
                if now - f.stat().st_mtime >= min_age_s and unlink_with_retry(f):
                    deleted.append(f.relative_to(self.root).as_posix())
        return deleted


def _rows_to_parquet(spec: TableSpec, rows: Sequence[Mapping[str, Any]], dest: Path) -> None:
    """COPY rows (validated by schema.to_sql_text, cast exactly in DuckDB) into a ZSTD Parquet file sorted by
    spec.sort and then every other column, and fsync it. Single-threaded DuckDB, so the bytes are deterministic."""
    n = len(rows)
    arrays: dict[str, Any] = {}
    for c in spec.cols:
        a = np.empty(n, dtype=object)
        a[:] = [schema.to_sql_text(c, r.get(c.name)) for r in rows]
        arrays[c.name] = a
    exprs = ", ".join(f"{c.cast_from_text(_q(c.name))} AS {_q(c.name)}" for c in spec.cols)
    order = [*spec.sort, *(c for c in spec.names if c not in spec.sort)]
    order_sql = ", ".join(f"{_q(c)} ASC NULLS FIRST" for c in order)
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.register("src_rows", arrays)
        dest_sql = dest.as_posix().replace("'", "''")
        con.execute(f"COPY (SELECT {exprs} FROM src_rows ORDER BY {order_sql}) TO '{dest_sql}' "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()
    _fsync_file(dest)


def write_parquet_atomic(path: str | Path, columns: Sequence[tuple[str, str]], rows: Sequence[Mapping[str, Any]], *,
                         sort: Sequence[str] = ()) -> str:
    """Atomically (temp + fsync + rename) write one ZSTD Parquet file outside the lake, e.g. the section 7.4 run
    outputs under data/runs/<run_id>/out/. columns are (name, type) with the lake's types (UBIGINT ... HUGEINT,
    VARCHAR, BOOLEAN, DOUBLE, BLOB, DATE); a row value that does not fit its type raises before anything is written.
    Returns the file's sha256. No manifest: run outputs are re-exportable from the journal."""
    cols = []
    for name, typ in columns:
        if typ not in schema.INT_RANGES and typ not in schema.OTHER_TYPES:
            raise ValueError(f"unsupported column type {typ} for {name}")
        cols.append(schema.Col(name, typ))
    names = {c.name for c in cols}
    if len(names) != len(cols) or not set(sort) <= names:
        raise ValueError("duplicate column names or unknown sort columns")
    for i, r in enumerate(rows):
        unknown = sorted(k for k in r if k not in names)
        if unknown:
            raise ValueError(f"row {i}: unknown column(s) {unknown}")
    spec = TableSpec(f"out:{Path(path).name}", tuple(cols), (), tuple(sort), None, "")
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        _rows_to_parquet(spec, rows, tmp)
        digest = _sha256(tmp)
        os.replace(tmp, final)
        _fsync_dir(final.parent)
    finally:
        unlink_with_retry(tmp)
    return digest


def _with_triples(paths: Sequence[str]) -> list[str]:
    """paths, with every snapshot-table chunk expanded to its (snap_global, snap_subnet, snap_hotkey) triple."""
    out: list[str] = []
    for p in paths:
        tbl, _, stem = p.partition("/")
        group = [f"{t}/{stem}" for t in SNAPSHOT_TABLES] if tbl in SNAPSHOT_TABLES else [p]
        out.extend(x for x in group if x not in out)
    return out


def _check_unique(spec: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
    if not spec.key:
        return
    seen: set[tuple[Any, ...]] = set()
    for r in rows:
        k = tuple(r.get(c) for c in spec.key)
        if k in seen:
            raise ValueError(f"{spec.name}: duplicate key {k} in one chunk")
        seen.add(k)


def _assemble(g_rows: Sequence[Mapping[str, Any]], s_rows: Sequence[Mapping[str, Any]],
              h_rows: Sequence[Mapping[str, Any]], *, verify: bool) -> list[ChainSnapshot]:
    subnets: dict[int, list[Mapping[str, Any]]] = {}
    for r in s_rows:
        subnets.setdefault(int(r["block"]), []).append(r)
    hotkeys: dict[int, list[Mapping[str, Any]]] = {}
    for r in h_rows:
        hotkeys.setdefault(int(r["block"]), []).append(r)
    out: list[ChainSnapshot] = []
    for g in g_rows:
        b = int(g["block"])
        out.append(schema.snapshot_from_rows(g, subnets.pop(b, []), hotkeys.pop(b, []), verify=verify))
    if subnets or hotkeys:
        raise DecodeError(f"snapshot rows without a snap_global row at blocks {sorted(set(subnets) | set(hotkeys))}")
    return out
