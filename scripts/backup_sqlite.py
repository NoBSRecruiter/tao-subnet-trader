"""Consistent online backups of SQLite files with `VACUUM INTO` (WP12; DESIGN.md section 12.1, nightly job).

    python scripts/backup_sqlite.py --dest D:/taotrader-backup/2026-10-09 data/runs/*/journal.sqlite data/state.sqlite

Each source is opened read-only (a running writer in WAL mode is not disturbed) and copied with
`VACUUM INTO '<dest>/<relative path>'`, which writes a transactionally consistent, compacted copy. The copy is then
opened and checked with `PRAGMA integrity_check`; a journal copy also has its record count printed. An existing
destination file is never overwritten (VACUUM INTO refuses it), so a re-run of the same night is a no-op for files
already backed up. Standard library only. Exit 0 when every source was backed up and checks ok, else 1.
"""
from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from pathlib import Path


def backup(src: Path, dest_root: Path, base: Path) -> str:
    try:
        rel = src.resolve().relative_to(base.resolve())
    except ValueError:
        rel = Path(src.name)
    dst = dest_root / rel
    if dst.exists():
        return f"SKIP {src} (backup exists: {dst})"
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()
    chk = sqlite3.connect(dst.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        extra = ""
        if chk.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal'").fetchone():
            extra = f", {chk.execute('SELECT COUNT(*) FROM journal').fetchone()[0]} journal records"
    finally:
        chk.close()
    if ok != "ok":
        raise sqlite3.DatabaseError(f"integrity_check of {dst}: {ok}")
    return f"OK {src} -> {dst}{extra}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", required=True, help="backup directory (a second disk)")
    ap.add_argument("--base", default=".", help="paths are kept relative to this directory (default: cwd)")
    ap.add_argument("sources", nargs="+", help="SQLite files or glob patterns")
    a = ap.parse_args(argv)
    files: list[Path] = []
    for pat in a.sources:
        hits = sorted(glob.glob(pat, recursive=True))
        files += [Path(h) for h in hits] if hits else ([Path(pat)] if Path(pat).is_file() else [])
    if not files:
        print("backup_sqlite: nothing matched", file=sys.stderr)
        return 1
    bad = 0
    for f in files:
        try:
            print(backup(f, Path(a.dest), Path(a.base)))
        except (sqlite3.Error, OSError) as e:
            bad += 1
            print(f"FAIL {f}: {type(e).__name__}: {e}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
