"""taotrader/core/codec.py - canonical, type-hint-driven JSON codec and digests (WP0 implements fully).

Rules (property-tested):
- int stays int (arbitrary size); bool stays bool; None -> null.
- Decimal -> exact text via format(d, "f") with trailing zeros stripped. NEVER Decimal.normalize(): it rounds
  to the 28-digit context precision and silently corrupts share counts.
- Enums by value; tuples/lists -> arrays; frozenset -> sorted array; Mapping -> object with sorted keys.
- dataclasses -> objects with sorted field names (fields with metadata {"codec": False} or leading "_" skipped).
- bytes -> "0x"-hex. float allowed only in non-money fields and must be finite (allow_nan=False).
- decode(cls, data) rebuilds by type hints (NewType-aware); unknown KIND or a newer VERSION raises.

WP0 implementation notes (the rules above are binding; these fill the gaps they leave):
- Decimal text is a JSON string ("-0" is written "0"); NaN/Infinity raise. A Decimal field also decodes a JSON
  int exactly; a JSON float is rejected (it is already inexact).
- A Mapping whose keys are all str (incl. StrEnum) is a JSON object. JSON object keys must be strings, so a
  Mapping with any other key type (e.g. FeatureFrame.feats: Mapping[SubnetKey, Feat]) is an array of [key, value]
  pairs sorted by the canonical text of the encoded key. frozenset/set elements sort the same way.
- decode is strict: unknown fields, missing fields without a default, bool-for-int, float-for-int and wrong
  container shapes all raise CodecError (a ValueError). Fields with init=False are never encoded or decoded.
- `object`/Any fields decode to the raw JSON value (strategy Memory objects are journaled as canonical bytes and
  decoded by their owner with decode_bytes(MemoryCls, raw)).
- Journal payloads: encode_event(ev) -> (KIND, VERSION, canonical bytes); decode_event(kind, version, payload)
  raises on an unknown KIND or a VERSION newer than the class, and upcasts older versions through registered
  upcasters (register_upcaster) one version at a time.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import types
import typing
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, TypeVar, Union, cast

from .events import REGISTRY, JournalEvent

T = TypeVar("T")


class CodecError(ValueError):
    """Data does not match the type it is decoded into, or a value cannot be encoded canonically."""


def encode(obj: Any) -> Any:
    """Canonical JSON-ready value (dict/list/str/int/float/bool/None) for obj."""
    return _enc(obj)


def decode(cls: type[T], data: Any) -> T:
    """Rebuild a value of type `cls` (a class or a typing alias such as tuple[Signal, ...]) from encode() output."""
    return cast(T, _dec(cls, data, getattr(cls, "__name__", str(cls))))


def canonical_bytes(obj: Any) -> bytes:
    """json.dumps(encode(obj), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()"""
    return json.dumps(encode(obj), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(obj: Any, size: int = 16) -> str:
    return hashlib.blake2b(canonical_bytes(obj), digest_size=size).hexdigest()


# ------------------------------------------------------------------------------------------------- helpers (WP0)
def decode_bytes(cls: type[T], raw: bytes | str) -> T:
    """decode(cls, json.loads(raw)) for canonical bytes (journal payloads, DecisionTrace.memories entries)."""
    return decode(cls, json.loads(raw))


Upcaster = Callable[[dict[str, Any]], dict[str, Any]]
UPCASTERS: dict[tuple[str, int], Upcaster] = {}


def register_upcaster(kind: str, from_version: int, fn: Upcaster) -> None:
    """Register fn to turn a `kind` payload at `from_version` into one at from_version + 1."""
    key = (kind, from_version)
    if key in UPCASTERS:
        raise ValueError(f"duplicate upcaster {kind} v{from_version}")
    UPCASTERS[key] = fn


def encode_event(ev: JournalEvent) -> tuple[str, int, bytes]:
    """(KIND, VERSION, canonical payload) of a registered journal event."""
    cls = type(ev)
    if not cls.KIND or REGISTRY.get(cls.KIND) is not cls:
        raise CodecError(f"{cls.__name__} is not a registered journal event")
    return cls.KIND, cls.VERSION, canonical_bytes(ev)


def decode_event(kind: str, version: int, payload: bytes | str | Mapping[str, Any]) -> JournalEvent:
    """Inverse of encode_event. Unknown kind or a version newer than the code raises CodecError."""
    cls = REGISTRY.get(kind)
    if cls is None:
        raise CodecError(f"unknown journal kind {kind!r}")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CodecError(f"{kind}: invalid version {version!r}")
    if version > cls.VERSION:
        raise CodecError(f"{kind}: journal version {version} is newer than code version {cls.VERSION}")
    data: Any = json.loads(payload) if isinstance(payload, (bytes, str)) else dict(payload)
    v = version
    while v < cls.VERSION:
        up = UPCASTERS.get((kind, v))
        if up is None:
            raise CodecError(f"{kind}: no upcaster from version {v}")
        data = up(dict(data))
        v += 1
    return decode(cls, data)


# ------------------------------------------------------------------------------------------------- encoding
def _dec_text(d: Decimal) -> str:
    if not d.is_finite():
        raise CodecError(f"non-finite Decimal {d!r}")
    s = format(d, "f")                       # exact: no context rounding without an explicit precision
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _sort_key(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _skip(f: dataclasses.Field[Any]) -> bool:
    return f.name.startswith("_") or f.metadata.get("codec", True) is False


def _enc(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        return _enc(obj.value)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise CodecError(f"non-finite float {obj!r}")
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, Decimal):
        return _dec_text(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return "0x" + bytes(obj).hex()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = sorted((f for f in dataclasses.fields(obj) if not _skip(f)), key=lambda f: f.name)
        return {f.name: _enc(getattr(obj, f.name)) for f in fields}
    if isinstance(obj, Mapping):
        if all(isinstance(k, str) for k in obj):
            return {str(_enc(k)): _enc(obj[k]) for k in sorted(obj, key=lambda k: str(_enc(k)))}
        pairs = [[_enc(k), _enc(v)] for k, v in obj.items()]
        return sorted(pairs, key=lambda kv: _sort_key(kv[0]))
    if isinstance(obj, (frozenset, set)):
        return sorted((_enc(x) for x in obj), key=_sort_key)
    if isinstance(obj, (tuple, list)):
        return [_enc(x) for x in obj]
    raise CodecError(f"cannot encode {type(obj).__name__}")


# ------------------------------------------------------------------------------------------------- decoding
_HINTS: dict[type[Any], dict[str, Any]] = {}


def _hints(cls: type[Any]) -> dict[str, Any]:
    h = _HINTS.get(cls)
    if h is None:
        h = typing.get_type_hints(cls)
        _HINTS[cls] = h
    return h


def _strip_newtype(tp: Any) -> Any:
    while hasattr(tp, "__supertype__"):
        tp = tp.__supertype__
    return tp


def _is_str_key(tp: Any) -> bool:
    tp = _strip_newtype(tp)
    return isinstance(tp, type) and issubclass(tp, str)


def _fail(path: str, msg: str) -> CodecError:
    return CodecError(f"{path}: {msg}")


def _dec(tp: Any, data: Any, path: str) -> Any:
    tp = _strip_newtype(tp)
    if tp is Any or tp is object:
        return data
    if tp is None or tp is type(None):
        if data is None:
            return None
        raise _fail(path, f"expected null, got {type(data).__name__}")
    origin = typing.get_origin(tp)
    if origin is Union or origin is types.UnionType:
        return _dec_union(tp, data, path)
    if origin is tuple:
        return _dec_tuple(tp, data, path)
    if origin is list:
        (arg,) = typing.get_args(tp) or (Any,)
        return [_dec(arg, x, f"{path}[{i}]") for i, x in enumerate(_expect_list(data, path))]
    if origin in (frozenset, set):
        (arg,) = typing.get_args(tp) or (Any,)
        items = [_dec(arg, x, f"{path}[{i}]") for i, x in enumerate(_expect_list(data, path))]
        return frozenset(items) if origin is frozenset else set(items)
    if origin is dict or (isinstance(origin, type) and issubclass(origin, Mapping)):
        return _dec_mapping(tp, data, path)
    if origin is not None:
        raise _fail(path, f"unsupported type {tp!r}")
    if not isinstance(tp, type):
        raise _fail(path, f"unsupported type {tp!r}")
    if issubclass(tp, Enum):
        return _dec_enum(tp, data, path)
    if tp is bool:
        if isinstance(data, bool):
            return data
        raise _fail(path, f"expected bool, got {type(data).__name__}")
    if tp is int:
        if isinstance(data, int) and not isinstance(data, bool):
            return data
        raise _fail(path, f"expected int, got {type(data).__name__}")
    if tp is float:
        if isinstance(data, (int, float)) and not isinstance(data, bool) and math.isfinite(data):
            return float(data)
        raise _fail(path, f"expected finite float, got {data!r}")
    if tp is str:
        if isinstance(data, str):
            return data
        raise _fail(path, f"expected str, got {type(data).__name__}")
    if tp is bytes:
        if isinstance(data, str) and data.startswith("0x"):
            try:
                return bytes.fromhex(data[2:])
            except ValueError as e:
                raise _fail(path, "bad hex") from e
        raise _fail(path, "expected 0x-hex string")
    if tp is Decimal:
        return _dec_decimal(data, path)
    if dataclasses.is_dataclass(tp):
        return _dec_dataclass(tp, data, path)
    raise _fail(path, f"unsupported type {tp!r}")


def _expect_list(data: Any, path: str) -> list[Any]:
    if not isinstance(data, list):
        raise _fail(path, f"expected array, got {type(data).__name__}")
    return data


def _dec_union(tp: Any, data: Any, path: str) -> Any:
    args = typing.get_args(tp)
    if data is None:
        if type(None) in args:
            return None
        raise _fail(path, "null for a non-optional union")
    errors: list[str] = []
    for arm in args:
        if arm is type(None):
            continue
        try:
            return _dec(arm, data, path)
        except CodecError as e:
            errors.append(str(e))
    raise _fail(path, "no union arm matched: " + "; ".join(errors))


def _dec_tuple(tp: Any, data: Any, path: str) -> tuple[Any, ...]:
    items = _expect_list(data, path)
    args = typing.get_args(tp)
    if len(args) == 2 and args[1] is Ellipsis:
        return tuple(_dec(args[0], x, f"{path}[{i}]") for i, x in enumerate(items))
    if args == ((),):                        # tuple[()] on some Python versions
        args = ()
    if len(items) != len(args):
        raise _fail(path, f"expected {len(args)} items, got {len(items)}")
    return tuple(_dec(a, x, f"{path}[{i}]") for i, (a, x) in enumerate(zip(args, items, strict=True)))


def _dec_mapping(tp: Any, data: Any, path: str) -> dict[Any, Any]:
    args = typing.get_args(tp)
    k_t, v_t = args if len(args) == 2 else (Any, Any)
    out: dict[Any, Any] = {}
    if isinstance(data, dict):
        if data and not _is_str_key(k_t) and k_t is not Any:
            raise _fail(path, "object form is only valid for str keys")
        for k, v in data.items():
            out[_dec(k_t, k, f"{path}.key")] = _dec(v_t, v, f"{path}[{k!r}]")
        return out
    if _is_str_key(k_t):
        raise _fail(path, "expected object for a str-keyed mapping")
    for i, pair in enumerate(_expect_list(data, path)):
        if not isinstance(pair, list) or len(pair) != 2:
            raise _fail(f"{path}[{i}]", "expected a [key, value] pair")
        out[_dec(k_t, pair[0], f"{path}[{i}].key")] = _dec(v_t, pair[1], f"{path}[{i}].value")
    return out


def _dec_enum(tp: type[Enum], data: Any, path: str) -> Enum:
    if isinstance(data, bool) or not isinstance(data, (int, str)):
        raise _fail(path, f"expected {tp.__name__} value, got {type(data).__name__}")
    if issubclass(tp, int) and not isinstance(data, int):
        raise _fail(path, f"expected int value for {tp.__name__}")
    if issubclass(tp, str) and not isinstance(data, str):
        raise _fail(path, f"expected str value for {tp.__name__}")
    try:
        return tp(data)
    except ValueError as e:
        raise _fail(path, f"{data!r} is not a valid {tp.__name__}") from e


def _dec_decimal(data: Any, path: str) -> Decimal:
    if isinstance(data, bool) or not isinstance(data, (str, int)):
        raise _fail(path, f"expected Decimal text, got {type(data).__name__}")
    try:
        d = Decimal(data)
    except InvalidOperation as e:
        raise _fail(path, f"bad Decimal {data!r}") from e
    if not d.is_finite():
        raise _fail(path, f"non-finite Decimal {data!r}")
    return d


def _dec_dataclass(tp: type[Any], data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise _fail(path, f"expected object for {tp.__name__}, got {type(data).__name__}")
    hints = _hints(tp)
    fields = {f.name: f for f in dataclasses.fields(tp) if f.init and not _skip(f)}
    unknown = sorted(k for k in data if k not in fields)
    if unknown:
        raise _fail(path, f"unknown field(s) {unknown} for {tp.__name__}")
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name in data:
            kwargs[name] = _dec(hints[name], data[name], f"{path}.{name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise _fail(path, f"missing field {name!r} for {tp.__name__}")
    try:
        return tp(**kwargs)
    except (TypeError, ValueError) as e:
        raise _fail(path, f"{tp.__name__} rejected the data: {e}") from e
