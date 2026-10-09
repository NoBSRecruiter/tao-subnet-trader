"""taotrader/chain/metadata.py - per-spec storage layouts and ValueQuery defaults (DESIGN.md sections 6.4, 6.12).

Runtime: only the committed JSON in chain/spec_defaults/<spec>.json is read (`SpecLayouts`). Each file holds, for
every registry item (chain/items.py) that exists in that runtime: modifier (Default = ValueQuery, Optional =
OptionQuery), hashers, key and value type names, the fixed encoded width of the value (null = variable) and the
default bytes. `validated` is true when every registry row matched the layout at extraction time.

An unknown spec falls back to the nearest LOWER known layout with `exact = False`: absent keys filled from it make
the snapshot `Quality.DEFAULT_FILLED`, and `SpecLayouts.accepted(spec)` is False so live goes FROZEN until a layout
for the spec is extracted and committed (section 6.12).

Committed layouts: 87 specs (233 ... 475), one per runtime seen when sampling dTAO history once per day from block
4,920,351 (plus spec 441 at 8,765,720; spec 475 from the golden metadata at 9,240,388). Every one validates against the
registry. A spec that ran for less than a day may be missing; it falls back as above (DEFAULT_FILLED) until added.

Offline (collector extra, scalecodec): `extract_layout(metadata_hex, ...)` decodes V14 metadata, and the command

    python -m taotrader.chain.metadata extract --block 9240388 [--endpoint URL]   # fetch + write spec_defaults/<spec>.json
    python -m taotrader.chain.metadata extract --hex FILE --spec 475 --block N --hash 0x..
    python -m taotrader.chain.metadata verify [--block N]                         # = taotrader verify-metadata

asserts every registry row's hasher, query kind and decoder width against the live spec, flags watched items such as
ShortsEnabled (ADR-0001 #3) and compares the committed layout of that spec (exit 1 on a mismatch). WP12's CLI calls
`verify_metadata(block=..., endpoint=...) -> (layout, problems)`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Final

from . import items as it
from .hashing import from_hex
from .items import Query, Row

SPEC_DIR = Path(__file__).resolve().parent / "spec_defaults"
LAYOUT_FORMAT = 1


@dataclass(frozen=True, slots=True)
class Entry:
    """One storage entry of one runtime."""
    modifier: str                     # "Default" (ValueQuery) | "Optional" (OptionQuery)
    hashers: tuple[str, ...]          # () for a plain value
    key: str | None                   # key type name (tuple text for multi-key maps)
    value: str                        # canonical value type name
    width: int | None                 # fixed encoded width of the value; None = variable
    default: str                      # 0x-hex default bytes

    @property
    def default_bytes(self) -> bytes:
        return from_hex(self.default)

    def to_json(self) -> dict[str, Any]:
        return {"modifier": self.modifier, "hashers": list(self.hashers), "key": self.key, "value": self.value,
                "width": self.width, "default": self.default}

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> Entry:
        return Entry(modifier=str(d["modifier"]), hashers=tuple(str(h) for h in d.get("hashers") or ()),
                     key=None if d.get("key") is None else str(d["key"]), value=str(d["value"]),
                     width=None if d.get("width") is None else int(d["width"]), default=str(d["default"]))


@dataclass(frozen=True)
class SpecLayout:
    spec_version: int
    transaction_version: int | None
    block: int | None
    block_hash: str | None
    validated: bool
    entries: dict[str, Entry]                       # "Pallet.Item" -> Entry
    exact: bool = True                              # False when used as the fallback for another (unknown) spec
    source: str = ""
    notes: tuple[str, ...] = field(default=())

    def entry(self, row_or_name: Row | str) -> Entry | None:
        name = row_or_name.name if isinstance(row_or_name, Row) else row_or_name
        return self.entries.get(name)

    def has(self, row: Row) -> bool:
        return row.name in self.entries

    def to_json(self) -> dict[str, Any]:
        return {"format": LAYOUT_FORMAT, "spec_version": self.spec_version, "transaction_version": self.transaction_version,
                "block": self.block, "block_hash": self.block_hash, "validated": self.validated, "source": self.source,
                "notes": list(self.notes), "entries": {k: self.entries[k].to_json() for k in sorted(self.entries)}}

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> SpecLayout:
        if int(d.get("format", 0)) != LAYOUT_FORMAT:
            raise ValueError(f"unsupported spec layout format {d.get('format')!r}")
        return SpecLayout(spec_version=int(d["spec_version"]),
                          transaction_version=None if d.get("transaction_version") is None else int(d["transaction_version"]),
                          block=None if d.get("block") is None else int(d["block"]),
                          block_hash=None if d.get("block_hash") is None else str(d["block_hash"]),
                          validated=bool(d["validated"]), source=str(d.get("source", "")),
                          notes=tuple(str(n) for n in d.get("notes", ())),
                          entries={str(k): Entry.from_json(v) for k, v in dict(d["entries"]).items()})


# ------------------------------------------------------------------------------------------------ runtime loading
class SpecLayouts:
    """The committed per-spec layouts. Lookup is pure and cached; nothing is fetched at runtime."""

    def __init__(self, directory: Path = SPEC_DIR, extra: Iterable[SpecLayout] = ()) -> None:
        self._dir = directory
        self._extra = {lay.spec_version: lay for lay in extra}

    @cached_property
    def _all(self) -> dict[int, SpecLayout]:
        out: dict[int, SpecLayout] = {}
        if self._dir.is_dir():
            for p in sorted(self._dir.glob("*.json")):
                if not p.stem.isdigit():
                    continue
                lay = SpecLayout.from_json(json.loads(p.read_text(encoding="utf-8")))
                if lay.spec_version != int(p.stem):
                    raise ValueError(f"{p.name} holds spec {lay.spec_version}")
                out[lay.spec_version] = lay
        out.update(self._extra)
        return out

    def known(self) -> tuple[int, ...]:
        return tuple(sorted(self._all))

    def exact(self, spec: int) -> SpecLayout | None:
        return self._all.get(spec)

    def for_spec(self, spec: int) -> SpecLayout | None:
        """The layout of `spec`, or the nearest lower known layout marked exact=False; None if no lower layout exists."""
        lay = self._all.get(spec)
        if lay is not None:
            return lay
        lower = [s for s in self._all if s < spec]
        if not lower:
            return None
        base = self._all[max(lower)]
        return SpecLayout(spec_version=spec, transaction_version=None, block=None, block_hash=None, validated=False,
                          entries=base.entries, exact=False, source=f"fallback from spec {base.spec_version}",
                          notes=(f"unknown spec {spec}: layout of spec {base.spec_version} used",))

    def accepted(self, spec: int) -> bool:
        """True iff a validated layout extracted from this exact spec is committed (live may run on it)."""
        lay = self._all.get(spec)
        return lay is not None and lay.validated


# ------------------------------------------------------------------------------------------------ verification
def verify_layout(layout: SpecLayout, rows: Sequence[Row] = it.ALL_ROWS) -> list[str]:
    """Mismatches between the registry and a spec layout (empty list = OK). Checked per row present in the layout:
    hashers, query kind (ValueQuery/OptionQuery), value width (or type name for variable-width values) and that the
    default decodes. A `required` row missing from the layout is a mismatch."""
    out: list[str] = []
    seen: set[str] = set()
    for name in it.WATCH_ITEMS:
        if name in layout.entries:
            out.append(f"{name}: watched item present in spec {layout.spec_version}; it must be registered "
                       f"deliberately (ADR-0001 #3) before this spec is accepted")
    for row in rows:
        e = layout.entry(row)
        if e is None:
            if row.required:
                out.append(f"{row.name}: required item missing from spec {layout.spec_version}")
            continue
        tag = f"{row.name} (spec {layout.spec_version})"
        if tuple(h.value for h in row.hashers) != e.hashers:
            out.append(f"{tag}: hashers {list(e.hashers)} != registry {[h.value for h in row.hashers]}")
        if e.modifier != row.query.value:
            out.append(f"{tag}: modifier {e.modifier} != registry {row.query.value}")
        w = row.decoder.width
        if w is not None:
            if e.width != w:
                out.append(f"{tag}: value {e.value} width {e.width} != decoder {row.decoder.name} width {w}")
        elif e.value not in row.decoder.types:
            out.append(f"{tag}: variable-width value {e.value} not in {list(row.decoder.types)}")
        if e.width is not None and w is not None and e.value not in row.decoder.types:
            out.append(f"{tag}: value type {e.value} not accepted by decoder {row.decoder.name} {list(row.decoder.types)}")
        if row.name not in seen and e.modifier == Query.VALUE.value:
            try:
                row.decode(e.default_bytes)
            except Exception as ex:
                out.append(f"{tag}: default {e.default} does not decode: {ex}")
        seen.add(row.name)
    return out


# ------------------------------------------------------------------------------------------------ extraction (offline)
def _hex_default(v: Any) -> str:
    """scalecodec returns a storage default as text when its bytes happen to be valid UTF-8; always store 0x-hex."""
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, str):
        body = v[2:]
        if v.startswith("0x") and len(body) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in body):
            return v.lower()
        return "0x" + v.encode("utf-8").hex()
    raise ValueError(f"unexpected default {v!r}")


def _typenum(s: str) -> int:
    """Value of a typenum UInt<UInt<...<UTerm, B1>, B0>...> type name (fixed-point fractional bits)."""
    val = 0
    for b in s.replace("UTerm", "").split("B")[1:]:
        if b and b[0] in "01":
            val = val * 2 + int(b[0])
    return val


_PRIM_WIDTH = {"bool": 1, "u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "char": 4, "u64": 8, "i64": 8,
               "u128": 16, "i128": 16, "u256": 32, "i256": 32}


class _TypeTable:
    def __init__(self, types: Mapping[int, Mapping[str, Any]]) -> None:
        self.types = types

    def name(self, i: int) -> str:
        t = self.types[i]
        d = t["def"]
        if "primitive" in d:
            return str(d["primitive"])
        if "compact" in d:
            return f"Compact<{self.name(d['compact']['type'])}>"
        if "sequence" in d:
            return f"Vec<{self.name(d['sequence']['type'])}>"
        if "array" in d:
            return f"[{self.name(d['array']['type'])}; {d['array']['len']}]"
        if "tuple" in d:
            return "(" + ", ".join(self.name(x) for x in d["tuple"]) + ")"
        path = t.get("path") or ["?"]
        params = [self.name(p["type"]) for p in t.get("params", []) if p.get("type") is not None]
        nm = str(path[-1])
        if nm in ("FixedU128", "FixedI128", "FixedU64", "FixedI64") and params:
            return f"{nm}<frac_bits={_typenum(params[0])}>"
        if nm in ("BoundedVec", "WeakBoundedVec") and params:
            return f"Vec<{params[0]}>"
        return nm + (f"<{', '.join(params)}>" if params else "")

    def width(self, i: int) -> int | None:
        d = self.types[i]["def"]
        if "primitive" in d:
            return _PRIM_WIDTH.get(str(d["primitive"]))
        if "composite" in d:
            total = 0
            for f in d["composite"]["fields"]:
                w = self.width(f["type"])
                if w is None:
                    return None
                total += w
            return total
        if "array" in d:
            w = self.width(d["array"]["type"])
            return None if w is None else w * int(d["array"]["len"])
        if "tuple" in d:
            total = 0
            for x in d["tuple"]:
                w = self.width(x)
                if w is None:
                    return None
                total += w
            return total
        if "variant" in d:
            variants = d["variant"]["variants"]
            return 1 if all(not v["fields"] for v in variants) else None
        return None


def extract_layout(metadata_hex: str, *, spec_version: int, transaction_version: int | None = None,
                   block: int | None = None, block_hash: str | None = None,
                   names: Iterable[str] | None = None) -> SpecLayout:
    """Decode runtime metadata (V14+) and keep the registry items. Needs scalecodec (the collector extra)."""
    try:
        from scalecodec.base import RuntimeConfiguration, ScaleBytes
        from scalecodec.type_registry import load_type_registry_preset
    except ImportError as e:  # pragma: no cover - exercised only without the collector extra
        raise RuntimeError("chain.metadata extraction needs the collector extra (scalecodec)") from e
    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    rc.update_type_registry(load_type_registry_preset("legacy"))
    md = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(metadata_hex))
    md.decode()
    versioned = md.value[1]
    version = next(iter(versioned))
    m = versioned[version]
    tt = _TypeTable({t["id"]: t["type"] for t in m["types"]["types"]})
    wanted = set(names if names is not None else it.item_names())
    entries: dict[str, Entry] = {}
    for p in m["pallets"]:
        if not p.get("storage"):
            continue
        for ent in p["storage"]["entries"]:
            name = f"{p['name']}.{ent['name']}"
            if name not in wanted:
                continue
            ty = ent["type"]
            if "Plain" in ty:
                vid = ty["Plain"]
                entries[name] = Entry(modifier=str(ent["modifier"]), hashers=(), key=None, value=tt.name(vid),
                                      width=tt.width(vid), default=_hex_default(ent["default"]))
            else:
                mp = ty["Map"]
                vid = mp["value"]
                entries[name] = Entry(modifier=str(ent["modifier"]), hashers=tuple(str(h) for h in mp["hashers"]),
                                      key=tt.name(mp["key"]), value=tt.name(vid), width=tt.width(vid),
                                      default=_hex_default(ent["default"]))
    draft = SpecLayout(spec_version=spec_version, transaction_version=transaction_version, block=block,
                       block_hash=block_hash, validated=False, entries=entries, source=f"state_getMetadata {version}")
    problems = verify_layout(draft)
    return SpecLayout(spec_version=spec_version, transaction_version=transaction_version, block=block,
                      block_hash=block_hash, validated=not problems, entries=entries, source=draft.source,
                      notes=tuple(problems))


def dumps_layout(layout: SpecLayout) -> str:
    """Deterministic JSON text: header fields one per line, then one storage entry per line (small, diff-friendly)."""
    d = layout.to_json()
    entries: dict[str, Any] = d.pop("entries")
    lines = ["{"]
    lines += [f" {json.dumps(k)}: {json.dumps(v)}," for k, v in d.items()]
    lines.append(' "entries": {')
    items = sorted(entries.items())
    lines += [f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}" + ("," if i + 1 < len(items) else "")
              for i, (k, v) in enumerate(items)]
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def write_layout(layout: SpecLayout, directory: Path = SPEC_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{layout.spec_version}.json"
    path.write_text(dumps_layout(layout), encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------------------------------------------ CLI (offline)
PUBLIC_ARCHIVE: Final[str] = "https://bittensor-finney.api.onfinality.io/public"


async def fetch_metadata(endpoint: str, block: int | None, block_hash: str | None) -> tuple[str, int, int, int, str]:
    """(metadata hex, spec, tx, block, hash) over plain read-only JSON-RPC (chain.rpc pool, 3 req/s)."""
    from .rpc import Role, RpcPool

    pool = RpcPool.from_urls(archive=(endpoint,), head=())
    try:
        h = block_hash if block_hash is not None else str(await pool.call("chain_getBlockHash", [block], Role.ARCHIVE))
        if block is None:
            hdr = await pool.call("chain_getHeader", [h], Role.ARCHIVE)
            block = int(hdr["number"], 16)
        rv = await pool.call("state_getRuntimeVersion", [h], Role.ARCHIVE)
        raw = str(await pool.call("state_getMetadata", [h], Role.ARCHIVE))
        return raw, int(rv["specVersion"]), int(rv["transactionVersion"]), block, h
    finally:
        await pool.aclose()


def check_against_committed(layout: SpecLayout, directory: Path = SPEC_DIR) -> list[str]:
    """verify-metadata: the registry against the live layout (`layout.notes`) plus the committed layout of the same
    spec against the live one. Empty = OK."""
    problems = list(layout.notes)
    committed = SpecLayouts(directory).exact(layout.spec_version)
    if committed is None:
        problems.append(f"spec {layout.spec_version}: no committed layout in {directory}")
    elif committed.entries != layout.entries:
        diff = sorted(k for k in set(committed.entries) | set(layout.entries)
                      if committed.entries.get(k) != layout.entries.get(k))
        problems.append(f"spec {layout.spec_version}: committed layout differs from the live metadata: {diff}")
    return problems


async def verify_metadata(*, block: int | None = None, block_hash: str | None = None, endpoint: str = PUBLIC_ARCHIVE,
                          directory: Path = SPEC_DIR) -> tuple[SpecLayout, list[str]]:
    """`taotrader verify-metadata` (WP12 CLI calls this): fetch the runtime metadata at the block (default: the
    finalized head of the endpoint), extract the layout and check it. Needs the collector extra (scalecodec)."""
    if block is None and block_hash is None:
        from .rpc import Role, RpcPool

        pool = RpcPool.from_urls(archive=(endpoint,), head=())
        try:
            block_hash = str(await pool.call("chain_getFinalizedHead", [], Role.ARCHIVE))
        finally:
            await pool.aclose()
    raw, spec, tx, blk, bh = await fetch_metadata(endpoint, block, block_hash)
    layout = extract_layout(raw, spec_version=spec, transaction_version=tx, block=blk, block_hash=bh)
    return layout, check_against_committed(layout, directory)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taotrader.chain.metadata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extract", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--block", type=int)
        sp.add_argument("--hash", dest="block_hash")
        sp.add_argument("--endpoint", default=PUBLIC_ARCHIVE)
        sp.add_argument("--hex", dest="hex_file", help="read raw metadata hex (or a capture JSON with a 'metadata' field)")
        sp.add_argument("--spec", type=int, help="spec version (with --hex)")
        sp.add_argument("--tx", type=int, help="transaction version (with --hex)")
        sp.add_argument("--out", type=Path, default=SPEC_DIR)
    a = ap.parse_args(argv)
    if a.hex_file:
        if a.spec is None:
            ap.error("--hex needs --spec")
        text = Path(a.hex_file).read_text(encoding="utf-8").strip()
        raw = str(json.loads(text)["metadata"]) if text.startswith("{") else text
        spec, tx, block, bh = int(a.spec), a.tx, a.block, a.block_hash
    else:
        if a.cmd == "extract" and a.block is None and a.block_hash is None:
            ap.error("--block or --hash is required")
        if a.cmd == "verify":
            layout, problems = asyncio.run(verify_metadata(block=a.block, block_hash=a.block_hash, endpoint=a.endpoint,
                                                           directory=a.out))
            print(f"spec {layout.spec_version}: {len(layout.entries)} entries checked against {len(it.ALL_ROWS)} "
                  f"registry rows")
            for p in problems:
                print("MISMATCH", p)
            return 1 if problems else 0
        raw, spec, tx, block, bh = asyncio.run(fetch_metadata(a.endpoint, a.block, a.block_hash))
    layout = extract_layout(raw, spec_version=spec, transaction_version=tx, block=block, block_hash=bh)
    if a.cmd == "extract":
        path = write_layout(layout, a.out)
        print(f"spec {spec}: {len(layout.entries)} entries, validated={layout.validated} -> {path}")
        problems = list(layout.notes)
    else:
        problems = check_against_committed(layout, a.out)
        print(f"spec {spec}: {len(layout.entries)} entries checked against {len(it.ALL_ROWS)} registry rows")
    for p in problems:
        print("MISMATCH", p)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
