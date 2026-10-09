"""taotrader/ops/config_load.py - TOML -> core.config dataclasses with strict validation (WP0).

Leaf module: imports only core and the stdlib, so every shell package (chain, data, venues, backtest, live, cli)
may use it (DESIGN.md section 4.2). Nobody writes a private loader.

Layout of a run-config TOML file (mirrors core.config.RunCfg; units are in the key names):

    run_id = "paper-main"            # [A-Za-z0-9][A-Za-z0-9_-]*
    mode = "paper"                   # RunMode value
    data_dir = "data"
    cadence_blocks = 60
    seed = 0
    [rpc]                            # RpcCfg
    [live]                           # LiveCfg
    [book_defaults]                  # optional: dereg_model, [book_defaults.risk], [book_defaults.exec]
    [[books]]                        # BookCfg: book, capital_rao, fee_float_rao, dereg_model
    [books.risk]                     #   partial RiskCfg override (on top of book_defaults.risk)
    [books.exec]                     #   partial ExecCfg override (on top of book_defaults.exec)
    [[books.sleeves]]                # SleeveCfg: strategy, stage ("PAPER" or 2), budget_ppm
    [books.sleeves.params]           #   free-form; validated by the strategy's own Params

Precedence (lowest first): core.config defaults < book_defaults < files (in the order given; tables merge
key by key, arrays and scalars replace) < TAOTRADER_CFG_* environment < CLI --set overrides.

- Environment: TAOTRADER_CFG_<PATH> with "__" between path segments, case-insensitive (Windows upper-cases
  environment names), e.g. TAOTRADER_CFG_RPC__RATE_PER_S=2.5, TAOTRADER_CFG_SEED=7,
  TAOTRADER_CFG_BOOKS__PAPER-CARRY__RISK__MARGIN_A_BLOCKS=600 (books by id, sleeves by strategy id).
  Other TAOTRADER_* variables (TAOTRADER_LIVE_ARMED, TAOTRADER_LIVE_NETWORK_CONFIRM, ...) are not config.
- CLI: "path.to.key=value" strings, e.g. "rpc.rate_per_s=2.5", "books.paper-carry.risk.margin_a_blocks=600".
- Values are parsed as TOML values (2.5, true, "x", [1, 2]); anything that is not valid TOML is a plain string.
- TOML has no null: an optional field (ExecCfg.impact_half_life_blocks) takes the string "none".

Strict validation: unknown keys are rejected at every level; ints must be TOML integers (no floats, no bools);
floats accept integers; every number must be finite and >= 0; enums by value (RunMode) or name (Stage).
Cross-field rules (validate_run_config): unwind_exec_blocks == finality_lag_blocks + latency_blocks per book,
unique book ids and sleeve strategies, sleeve budgets sum <= 1e6 ppm, dereg_model syntax, live mode/network
values, endpoint schemes, and no credentials in endpoint URLs (secrets belong in ops.secrets / the keyring).

config_hash(cfg) = blake2b-256 of core.codec.canonical_bytes(cfg): any edit changes it (the live arm token is
an HMAC over it). prereg_hash() = blake2b-256 of the canonical JSON of config/preregistration.toml's parsed
content, so it is identical on Windows and Linux whatever the line endings.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import tomllib
import types
import typing
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from ..core import codec
from ..core.config import BookCfg, ExecCfg, RiskCfg, RunCfg

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
CONFIG_DIR: Final[Path] = REPO_ROOT / "config"
DEFAULT_CONFIG: Final[Path] = CONFIG_DIR / "default.toml"
PREREGISTRATION: Final[Path] = CONFIG_DIR / "preregistration.toml"

ENV_PREFIX: Final[str] = "TAOTRADER_CFG_"
ENV_SEP: Final[str] = "__"
NONE_TEXT: Final[str] = "none"
BOOK_DEFAULT_KEYS: Final[frozenset[str]] = frozenset({"risk", "exec", "dereg_model"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")            # run_id, book: used in paths, '|' and ':' keys
_STRATEGY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")     # "carry", "baseline.ew_total"
_DEREG_RE = re.compile(r"^(formula|fixed:(\d+))$")
_SECRET_URL_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd)=|//[^/@]+@", re.IGNORECASE)


class ConfigError(ValueError):
    """Invalid configuration. The message names the offending key path; it never echoes secrets."""


# ------------------------------------------------------------------------------------------------- file I/O
def read_toml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: invalid TOML: {e}") from e


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Tables merge key by key; arrays and scalars in `over` replace those in `base`. Inputs are not mutated."""
    out: dict[str, Any] = {k: _copy(v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = _copy(v)
    return out


def _copy(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


# ------------------------------------------------------------------------------------------------- overrides
Override = tuple[tuple[str, ...], Any]


def parse_override_value(text: str) -> Any:
    """A TOML value if `text` parses as one ("2.5", "true", '"x"', "[1, 2]"), else the raw string."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def env_overrides(env: Mapping[str, str]) -> list[Override]:
    """TAOTRADER_CFG_A__B=value -> (("a", "b"), parsed value), sorted by variable name."""
    out: list[Override] = []
    for name in sorted(env):
        if not name.upper().startswith(ENV_PREFIX):
            continue
        rest = name[len(ENV_PREFIX):]
        path = tuple(seg.lower() for seg in rest.split(ENV_SEP))
        if not rest or any(not seg for seg in path):
            raise ConfigError(f"malformed config environment variable {name}")
        out.append((path, parse_override_value(env[name])))
    return out


def cli_overrides(items: Sequence[str]) -> list[Override]:
    """["rpc.rate_per_s=2.5", ...] -> ((("rpc", "rate_per_s"), 2.5), ...)."""
    out: list[Override] = []
    for item in items:
        key, sep, value = item.partition("=")
        path = tuple(key.strip().split("."))
        if not sep or not key.strip() or any(not seg for seg in path):
            raise ConfigError(f"malformed override {key.strip() or item!r}: expected path.to.key=value")
        out.append((path, parse_override_value(value.strip())))
    return out


def apply_overrides(raw: Mapping[str, Any], overrides: Sequence[Override], *, case_insensitive: bool) -> dict[str, Any]:
    """Apply (path, value) overrides to a copy of the merged raw config. Books are addressed by id and sleeves
    by strategy id (a strategy id may contain dots: the longest matching run of segments wins)."""
    out = _copy(raw)
    for path, value in overrides:
        _set_path(out, path, value, case_insensitive)
    return typing.cast(dict[str, Any], out)


def _eq(a: str, b: str, ci: bool) -> bool:
    return a.lower() == b.lower() if ci else a == b


def _set_path(root: dict[str, Any], path: tuple[str, ...], value: Any, ci: bool) -> None:
    dotted = ".".join(path)
    node: dict[str, Any] = root
    i = 0
    while i < len(path) - 1:
        seg = path[i]
        if node is root and seg == "books":
            books = root.get("books", [])
            match = [b for b in books if isinstance(b, dict) and _eq(str(b.get("book", "")), path[i + 1], ci)]
            if len(match) != 1 or i + 2 >= len(path):
                raise ConfigError(f"override {dotted}: no unique book {path[i + 1]!r}")
            node, i = match[0], i + 2
            continue
        if seg == "sleeves" and "book" in node:
            sleeves = [s for s in node.get("sleeves", []) if isinstance(s, dict)]
            found = None
            for j in range(len(path) - 1, i + 1, -1):                    # longest strategy id first
                sid = ".".join(path[i + 1:j])
                hit = [s for s in sleeves if _eq(str(s.get("strategy", "")), sid, ci)]
                if len(hit) == 1:
                    found = (hit[0], j)
                    break
            if found is None:
                raise ConfigError(f"override {dotted}: no unique sleeve in book {node['book']!r}")
            node, i = found
            continue
        nxt = node.get(seg)
        if nxt is None:
            nxt = node[seg] = {}
        if not isinstance(nxt, dict):
            raise ConfigError(f"override {dotted}: {seg!r} is not a table")
        node, i = nxt, i + 1
    node[path[-1]] = value


# ------------------------------------------------------------------------------------------------- building
def load_run_config(paths: Sequence[str | Path] = (DEFAULT_CONFIG,), *, env: Mapping[str, str] | None = None,
                    cli: Sequence[str] = ()) -> RunCfg:
    """Read and merge `paths` in order, apply environment then CLI overrides, build and validate a RunCfg."""
    raw: dict[str, Any] = {}
    for p in paths:
        raw = deep_merge(raw, read_toml(p))
    raw = apply_overrides(raw, env_overrides(os.environ if env is None else env), case_insensitive=True)
    raw = apply_overrides(raw, cli_overrides(cli), case_insensitive=False)
    return build_run_config(raw)


def build_run_config(raw: Mapping[str, Any]) -> RunCfg:
    """Strictly build and validate a RunCfg from an already merged raw mapping."""
    data = _copy(raw)
    defaults = data.pop("book_defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("book_defaults: expected a table")
    unknown = sorted(set(defaults) - BOOK_DEFAULT_KEYS)
    if unknown:
        raise ConfigError(f"book_defaults: unknown key(s) {unknown}")
    for sub, sub_cls in (("risk", RiskCfg), ("exec", ExecCfg)):          # validated even when no book uses them
        if sub in defaults:
            _build(sub_cls, defaults[sub], f"book_defaults.{sub}")
    if "dereg_model" in defaults:
        _coerce(str, defaults["dereg_model"], "book_defaults.dereg_model")
    books = data.get("books", [])
    if not isinstance(books, list):
        raise ConfigError("books: expected an array of tables")
    merged_books: list[Any] = []
    for i, b in enumerate(books):
        if not isinstance(b, dict):
            raise ConfigError(f"books[{i}]: expected a table")
        nb = dict(b)
        for sub in ("risk", "exec"):
            base, own = defaults.get(sub, {}), b.get(sub, {})
            if not isinstance(base, dict) or not isinstance(own, dict):
                raise ConfigError(f"books[{i}].{sub}: expected a table")
            nb[sub] = deep_merge(base, own)
        if "dereg_model" not in b and "dereg_model" in defaults:
            nb["dereg_model"] = defaults["dereg_model"]
        merged_books.append(nb)
    data["books"] = merged_books
    cfg = typing.cast(RunCfg, _build(RunCfg, data, ""))
    validate_run_config(cfg)
    return cfg


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _build(cls: type[Any], raw: Any, path: str) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path or cls.__name__}: expected a table")
    hints = typing.get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls) if f.init}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ConfigError(f"{path or '<top>'}: unknown key(s) {unknown} for {cls.__name__}")
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name in raw:
            kwargs[name] = _coerce(hints[name], raw[name], _join(path, name))
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ConfigError(f"{_join(path, name)}: required key missing")
    return cls(**kwargs)


def _coerce(tp: Any, v: Any, path: str) -> Any:
    while hasattr(tp, "__supertype__"):                                   # NewType -> runtime type
        tp = tp.__supertype__
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union or origin is types.UnionType:
        if isinstance(v, str) and v.lower() == NONE_TEXT and type(None) in args:
            return None
        arms = [a for a in args if a is not type(None)]
        if len(arms) != 1:
            raise ConfigError(f"{path}: unsupported union type")
        return _coerce(arms[0], v, path)
    if origin is tuple:
        if not isinstance(v, list):
            raise ConfigError(f"{path}: expected an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(args[0], x, f"{path}[{i}]") for i, x in enumerate(v))
        raise ConfigError(f"{path}: unsupported tuple type")
    if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
        if not isinstance(v, dict):
            raise ConfigError(f"{path}: expected a table")
        return _freeze(v, path)
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _build(tp, v, path)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return _coerce_enum(tp, v, path)
    if tp is bool:
        if isinstance(v, bool):
            return v
        raise ConfigError(f"{path}: expected true/false, got {type(v).__name__}")
    if tp is int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{path}: expected an integer, got {type(v).__name__}")
        if v < 0:
            raise ConfigError(f"{path}: must be >= 0")
        return v
    if tp is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"{path}: expected a number, got {type(v).__name__}")
        f = float(v)
        if not math.isfinite(f) or f < 0:
            raise ConfigError(f"{path}: must be finite and >= 0")
        return f
    if tp is str:
        if not isinstance(v, str):
            raise ConfigError(f"{path}: expected a string, got {type(v).__name__}")
        return v
    raise ConfigError(f"{path}: unsupported field type {tp!r}")


def _coerce_enum(tp: type[Enum], v: Any, path: str) -> Enum:
    if isinstance(v, str):
        for m in tp:
            if m.value == v or m.name.lower() == v.lower():
                return m
    elif isinstance(v, int) and not isinstance(v, bool):
        for m in tp:
            if m.value == v:
                return m
    names = ", ".join(f"{m.name}" for m in tp)
    raise ConfigError(f"{path}: {v!r} is not one of {names}")


def _freeze(v: Any, path: str) -> Any:
    """Free-form params: JSON-like values only; tables become read-only mappings and arrays tuples."""
    if isinstance(v, dict):
        return MappingProxyType({str(k): _freeze(x, f"{path}.{k}") for k, x in v.items()})
    if isinstance(v, list):
        return tuple(_freeze(x, f"{path}[{i}]") for i, x in enumerate(v))
    if isinstance(v, float) and not math.isfinite(v):
        raise ConfigError(f"{path}: must be finite")
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    raise ConfigError(f"{path}: unsupported value type {type(v).__name__}")


# ------------------------------------------------------------------------------------------------- validation
def validate_run_config(cfg: RunCfg) -> None:
    """Cross-field rules. Raises ConfigError listing every problem found."""
    errs: list[str] = []
    if not _ID_RE.match(cfg.run_id):
        errs.append(f"run_id {cfg.run_id!r} must match {_ID_RE.pattern}")
    if cfg.cadence_blocks < 1:
        errs.append("cadence_blocks must be >= 1")
    r = cfg.rpc
    if r.rate_per_s <= 0 or r.timeout_s <= 0:
        errs.append("rpc.rate_per_s and rpc.timeout_s must be > 0")
    if r.burst < 1 or r.max_concurrency < 1 or r.keys_per_call < 1:
        errs.append("rpc.burst, rpc.max_concurrency and rpc.keys_per_call must be >= 1")
    for i, u in enumerate(r.head_endpoints):
        if not u.startswith(("wss://", "ws://")):
            errs.append(f"rpc.head_endpoints[{i}] must be a ws:// or wss:// URL")
        if _SECRET_URL_RE.search(u):
            errs.append(f"rpc.head_endpoints[{i}] carries a credential: keep keyed URLs in the keyring (ops.secrets)")
    for i, u in enumerate(r.archive_endpoints):
        if not u.startswith(("https://", "http://")):
            errs.append(f"rpc.archive_endpoints[{i}] must be an http:// or https:// URL")
        if _SECRET_URL_RE.search(u):
            errs.append(f"rpc.archive_endpoints[{i}] carries a credential: keep keyed URLs in the keyring (ops.secrets)")
    lv = cfg.live
    if lv.mode not in ("plan_only", "submit"):
        errs.append(f"live.mode {lv.mode!r} must be 'plan_only' or 'submit'")
    if lv.network not in ("test", "finney"):
        errs.append(f"live.network {lv.network!r} must be 'test' or 'finney'")
    for s in lv.sleeves:
        if not _STRATEGY_RE.match(s):
            errs.append(f"live.sleeves entry {s!r} must match {_STRATEGY_RE.pattern}")
    seen_books: set[str] = set()
    for b in cfg.books:
        errs.extend(_validate_book(b, seen_books))
    if errs:
        raise ConfigError("; ".join(errs))


def _validate_book(b: BookCfg, seen: set[str]) -> list[str]:
    errs: list[str] = []
    tag = f"books[{b.book}]"
    if not _ID_RE.match(b.book):
        errs.append(f"{tag}: book id must match {_ID_RE.pattern}")
    if b.book in seen:
        errs.append(f"{tag}: duplicate book id")
    seen.add(b.book)
    want = b.exec.finality_lag_blocks + b.exec.latency_blocks
    if b.risk.unwind_exec_blocks != want:
        errs.append(f"{tag}: risk.unwind_exec_blocks ({b.risk.unwind_exec_blocks}) must equal exec.finality_lag_blocks"
                    f" + exec.latency_blocks ({want})")
    if b.exec.n_delegates < 1:
        errs.append(f"{tag}: exec.n_delegates must be >= 1")
    m = _DEREG_RE.match(b.dereg_model)
    if m is None or (m.group(2) is not None and int(m.group(2)) > 1_000_000):
        errs.append(f"{tag}: dereg_model {b.dereg_model!r} must be 'formula' or 'fixed:<ppm 0..1000000>'")
    strategies: set[str] = set()
    total = 0
    for s in b.sleeves:
        if not _STRATEGY_RE.match(s.strategy):
            errs.append(f"{tag}: sleeve strategy {s.strategy!r} must match {_STRATEGY_RE.pattern}")
        if s.strategy in strategies:
            errs.append(f"{tag}: duplicate sleeve {s.strategy!r}")
        strategies.add(s.strategy)
        total += s.budget_ppm
    if total > 1_000_000:
        errs.append(f"{tag}: sleeve budgets sum to {total} ppm > 1,000,000")
    return errs


# ------------------------------------------------------------------------------------------------- hashing
def config_hash(cfg: RunCfg) -> str:
    """blake2b-256 hex of the canonical JSON of the validated RunCfg (journaled by ConfigApplied)."""
    return hashlib.blake2b(codec.canonical_bytes(cfg), digest_size=32).hexdigest()


def load_preregistration(path: str | Path = PREREGISTRATION) -> dict[str, Any]:
    return read_toml(path)


def prereg_hash(path: str | Path = PREREGISTRATION) -> str:
    """blake2b-256 hex of the canonical JSON of the parsed preregistration file (ConfigApplied.prereg_hash)."""
    return hashlib.blake2b(codec.canonical_bytes(load_preregistration(path)), digest_size=32).hexdigest()


def check_preregistration(prereg: Mapping[str, Any]) -> list[str]:
    """Differences between the preregistered [risk]/[exec] tables and the core.config defaults (empty = frozen)."""
    out: list[str] = []
    for table, cls in (("risk", RiskCfg), ("exec", ExecCfg)):
        want = run_config_value(cls())
        got = prereg.get(table)
        if not isinstance(got, Mapping):
            out.append(f"preregistration: missing [{table}]")
            continue
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append(f"preregistration [{table}] lacks {k}")
            elif k not in want:
                out.append(f"preregistration [{table}] has unknown key {k}")
            elif got[k] != want[k] or type(got[k]) is not type(want[k]):
                out.append(f"preregistration [{table}].{k} = {got[k]!r} != core default {want[k]!r}")
    return out


# ------------------------------------------------------------------------------------------------- dumping
def run_config_value(obj: Any) -> Any:
    """TOML-shaped plain value of a config object: dataclasses -> dicts, enums -> RunMode value / Stage name,
    tuples -> lists, None -> "none"."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: run_config_value(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.init}
    if isinstance(obj, Enum):
        return obj.value if isinstance(obj.value, str) else obj.name
    if isinstance(obj, Mapping):
        return {str(k): run_config_value(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [run_config_value(x) for x in obj]
    if obj is None:
        return NONE_TEXT
    return obj


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _scalar(v: Any, path: str) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ConfigError(f"{path}: cannot write a non-finite float")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)                       # a JSON string literal is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x, path) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{" + ", ".join(f"{_key(str(k))} = {_scalar(x, path)}" for k, x in v.items()) + "}"
    raise ConfigError(f"{path}: cannot write {type(v).__name__}")


def _is_table_array(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, Mapping) for x in v)


def dumps_toml(raw: Mapping[str, Any]) -> str:
    """Minimal TOML writer for config-shaped data (tables, arrays of tables, scalars, arrays of scalars)."""
    lines: list[str] = []
    _emit_table(lines, raw, ())
    return "\n".join(lines).strip() + "\n"


def _emit_table(lines: list[str], t: Mapping[str, Any], path: tuple[str, ...]) -> None:
    for k, v in t.items():
        if not isinstance(v, Mapping) and not _is_table_array(v):
            lines.append(f"{_key(k)} = {_scalar(v, '.'.join((*path, k)))}")
    for k, v in t.items():
        if isinstance(v, Mapping):
            sub = (*path, k)
            lines.extend(["", "[" + ".".join(_key(x) for x in sub) + "]"])
            _emit_table(lines, v, sub)
    for k, v in t.items():
        if _is_table_array(v):
            sub = (*path, k)
            for item in v:
                lines.extend(["", "[[" + ".".join(_key(x) for x in sub) + "]]"])
                _emit_table(lines, item, sub)


def dump_run_config(cfg: RunCfg) -> str:
    """TOML text that load_run_config reads back to an equal RunCfg (same config_hash)."""
    return dumps_toml(run_config_value(cfg))
