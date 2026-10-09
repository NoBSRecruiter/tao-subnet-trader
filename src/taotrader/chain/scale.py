"""taotrader/chain/scale.py - exact SCALE decoders for the storage items and runtime APIs the reader uses (section 6.4).

Little-endian throughout. Fixed-width values must have EXACTLY their width (a short or long value raises DecodeError:
an undecodable value fails the whole snapshot, never a silent truncation). Fixed-point values decode to Decimal
exactly (core.fixed.EXACT, 160 digits: 2**-64 terminates in 64 digits):

- I96F32 (FixedI128<32>)  i128 / 2**32         SubnetMovingPrice, SubnetMovingAlpha
- U96F32 (FixedU128<32>)  u128 / 2**32         RootProp, MinerBurned
- U64F64 (FixedU128<64>)  u128 / 2**64         EmissionGateBar, EmissionGateExponent, AlphaSqrtPrice, V1 shares,
                                               SubnetFastMovingPrice, legacy Alpha
- Perquintill             u64 (1e18-scaled)    Swap.SwapBalancer.quote: the raw int is kept
- TaoWeight               u64 / u64::MAX       (not terminating; EXACT context, deterministic)
- SafeFloat               {mantissa u128, exponent i64} = m * 10**e (24 bytes; Decimal string ctor is exact)
- Option<T>               OptionQuery stores raw T; a ValueQuery<Option<T>> stores 0x00 / 0x01 ++ T
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, TypeVar

from taotrader.core.errors import DecodeError
from taotrader.core.fixed import EXACT

T = TypeVar("T")

TWO_32 = Decimal(2**32)
TWO_64 = Decimal(2**64)
U64_MAX_DEC = Decimal(2**64 - 1)


def _exact(b: bytes, n: int, what: str) -> bytes:
    if len(b) != n:
        raise DecodeError(f"{what}: expected {n} bytes, got {len(b)}: 0x{b.hex()[:80]}")
    return b


def _u(b: bytes, n: int, what: str, signed: bool = False) -> int:
    return int.from_bytes(_exact(b, n, what), "little", signed=signed)


# ------------------------------------------------------------------------------------------------ primitives
def d_bool(b: bytes) -> bool:
    v = _u(b, 1, "bool")
    if v > 1:
        raise DecodeError(f"bool: invalid byte 0x{b.hex()}")
    return v == 1


def d_u8(b: bytes) -> int:
    return _u(b, 1, "u8")


def d_u16(b: bytes) -> int:
    return _u(b, 2, "u16")


def d_u32(b: bytes) -> int:
    return _u(b, 4, "u32")


def d_u64(b: bytes) -> int:
    return _u(b, 8, "u64")


def d_u128(b: bytes) -> int:
    return _u(b, 16, "u128")


def d_i64(b: bytes) -> int:
    return _u(b, 8, "i64", signed=True)


def d_i128(b: bytes) -> int:
    return _u(b, 16, "i128", signed=True)


# ------------------------------------------------------------------------------------------------ fixed point
def d_i96f32(b: bytes) -> Decimal:
    return EXACT.divide(Decimal(d_i128(b)), TWO_32)


def d_u96f32(b: bytes) -> Decimal:
    return EXACT.divide(Decimal(d_u128(b)), TWO_32)


def d_u64f64(b: bytes) -> Decimal:
    return EXACT.divide(Decimal(d_u128(b)), TWO_64)


def d_perquintill_raw(b: bytes) -> int:
    """Swap.SwapBalancer (struct Balancer {quote: Perquintill}): the 1e18-scaled u64, kept as an int."""
    return _u(b, 8, "Perquintill")


def d_tao_weight(b: bytes) -> Decimal:
    return EXACT.divide(Decimal(d_u64(b)), U64_MAX_DEC)


def d_safefloat(b: bytes) -> Decimal:
    """share_pool::SafeFloat {mantissa: u128, exponent: i64} = mantissa * 10**exponent, exact."""
    _exact(b, 24, "SafeFloat")
    m = int.from_bytes(b[:16], "little")
    e = int.from_bytes(b[16:], "little", signed=True)
    return Decimal(f"{m}E{e}")


def safefloat_parts(b: bytes) -> tuple[int, int]:
    _exact(b, 24, "SafeFloat")
    return int.from_bytes(b[:16], "little"), int.from_bytes(b[16:], "little", signed=True)


def d_account(b: bytes) -> str:
    return "0x" + _exact(b, 32, "AccountId32").hex()


def d_enum_index(n_variants: int) -> Callable[[bytes], int]:
    """A field-less enum (u8 variant index), e.g. EpochConsensus {Yuma = 0, Null = 1}."""
    def f(b: bytes) -> int:
        v = _u(b, 1, "enum")
        if v >= n_variants:
            raise DecodeError(f"enum: variant {v} >= {n_variants}")
        return v
    return f


def d_blocknum(b: bytes) -> int:
    """u32 or u64 block number depending on the item: decode by length."""
    if len(b) not in (4, 8):
        raise DecodeError(f"block number: expected 4 or 8 bytes, got {len(b)}")
    return int.from_bytes(b, "little")


# ------------------------------------------------------------------------------------------------ compact / vec
def d_compact(b: bytes, offset: int = 0) -> tuple[int, int]:
    """SCALE Compact<uN> at `offset` -> (value, bytes consumed)."""
    if offset >= len(b):
        raise DecodeError("compact: no bytes")
    first = b[offset]
    mode = first & 3
    if mode == 0:
        return first >> 2, 1
    if mode == 1:
        if offset + 2 > len(b):
            raise DecodeError("compact: truncated (2-byte mode)")
        return int.from_bytes(b[offset:offset + 2], "little") >> 2, 2
    if mode == 2:
        if offset + 4 > len(b):
            raise DecodeError("compact: truncated (4-byte mode)")
        return int.from_bytes(b[offset:offset + 4], "little") >> 2, 4
    n = (first >> 2) + 4
    if offset + 1 + n > len(b):
        raise DecodeError("compact: truncated (big-integer mode)")
    return int.from_bytes(b[offset + 1:offset + 1 + n], "little"), n + 1


def d_vec_len(b: bytes) -> int:
    """Length prefix of a Vec<T> (DissolveCleanupQueue)."""
    return d_compact(b)[0]


def d_vec_fixed(elem_width: int, elem: Callable[[bytes], T]) -> Callable[[bytes], tuple[T, ...]]:
    """Vec<T> of a fixed-width element; the whole value must be consumed exactly."""
    def f(b: bytes) -> tuple[T, ...]:
        n, used = d_compact(b)
        if used + n * elem_width != len(b):
            raise DecodeError(f"vec: {n} x {elem_width} B + {used} B prefix != {len(b)} B")
        return tuple(elem(b[used + i * elem_width:used + (i + 1) * elem_width]) for i in range(n))
    return f


def d_vec_u16(b: bytes) -> tuple[int, ...]:
    return d_vec_fixed(2, d_u16)(b)


def d_vec_bool(b: bytes) -> tuple[bool, ...]:
    return d_vec_fixed(1, d_bool)(b)


# ------------------------------------------------------------------------------------------------ option
def d_option(inner: Callable[[bytes], T], widths: int | tuple[int, ...] | None) -> Callable[[bytes | None], T | None]:
    """OptionQuery stores raw T (absent = None); ValueQuery<Option<T>> stores 0x00 / 0x01 ++ T.

    `widths` is T's encoded width (or the accepted widths, e.g. (4, 8) for a block number decoded by length); None for
    a variable-width T (then only the 0x00 / 0x01 ++ T form is accepted).
    """
    ws: tuple[int, ...] = () if widths is None else ((widths,) if isinstance(widths, int) else tuple(widths))

    def f(b: bytes | None) -> T | None:
        if b is None or b == b"\x00":
            return None
        if len(b) in ws:
            return inner(b)
        if b[0] == 1 and (not ws or len(b) - 1 in ws):
            return inner(b[1:])
        raise DecodeError(f"option width {ws or 'variable'}: 0x{b.hex()[:80]}")
    return f


def d_option_u64_by_len(b: bytes | None) -> int | None:
    """Option<u64> where the storage form is unknown (FirstEmissionBlockNumber): decode by length."""
    return d_option(d_u64, 8)(b)


# ------------------------------------------------------------------------------------------------ descriptor
@dataclass(frozen=True, slots=True)
class Decoder(Generic[T]):
    """A named decoder with its encoded width (None = variable) and the metadata value types it accepts.

    verify-metadata checks both: the layout's computed value width must equal `width` (fixed-width decoders) and the
    canonical type name must be one of `types`. `alt_widths` are further widths accepted when decoding by length
    (a block number stored as u32 or u64)."""
    name: str
    width: int | None
    fn: Callable[[bytes], T]
    types: tuple[str, ...]
    alt_widths: tuple[int, ...] = ()

    def __call__(self, b: bytes) -> T:
        return self.fn(b)

    @property
    def widths(self) -> tuple[int, ...] | None:
        return None if self.width is None else (self.width, *self.alt_widths)


BOOL: Decoder[bool] = Decoder("bool", 1, d_bool, ("bool",))
U16: Decoder[int] = Decoder("u16", 2, d_u16, ("u16", "NetUid", "PerU16", "NetUidStorageIndex"))
U32: Decoder[int] = Decoder("u32", 4, d_u32, ("u32", "BlockNumberFor"))
U64: Decoder[int] = Decoder("u64", 8, d_u64, ("u64", "TaoBalance", "AlphaBalance", "TaoCurrency", "AlphaCurrency"))
U128: Decoder[int] = Decoder("u128", 16, d_u128, ("u128",))
I64: Decoder[int] = Decoder("i64", 8, d_i64, ("i64",))
I96F32: Decoder[Decimal] = Decoder("I96F32", 16, d_i96f32, ("FixedI128<frac_bits=32>",))
U96F32: Decoder[Decimal] = Decoder("U96F32", 16, d_u96f32, ("FixedU128<frac_bits=32>",))
U64F64: Decoder[Decimal] = Decoder("U64F64", 16, d_u64f64, ("FixedU128<frac_bits=64>",))
U64F64_RAW: Decoder[int] = Decoder("U64F64.raw", 16, d_u128, ("FixedU128<frac_bits=64>",))
PERQUINTILL: Decoder[int] = Decoder("Perquintill", 8, d_perquintill_raw, ("Balancer", "Perquintill"))
TAO_WEIGHT: Decoder[Decimal] = Decoder("TaoWeight", 8, d_tao_weight, ("u64",))
SAFEFLOAT: Decoder[Decimal] = Decoder("SafeFloat", 24, d_safefloat, ("SafeFloat",))
ACCOUNT: Decoder[str] = Decoder("AccountId32", 32, d_account, ("AccountId32", "AccountId"))
BLOCKNUM: Decoder[int] = Decoder("BlockNumber", 4, d_blocknum, ("u32", "u64", "BlockNumberFor"), alt_widths=(8,))
EPOCH_CONSENSUS: Decoder[int] = Decoder("EpochConsensus", 1, d_enum_index(2), ("EpochConsensus",))
VEC_LEN: Decoder[int] = Decoder("Vec.len", None, d_vec_len, ("Vec<NetUid>", "Vec<u16>"))
VEC_U16: Decoder[tuple[int, ...]] = Decoder("Vec<u16>", None, d_vec_u16, ("Vec<u16>", "Vec<PerU16>"))
VEC_BOOL: Decoder[tuple[bool, ...]] = Decoder("Vec<bool>", None, d_vec_bool, ("Vec<bool>",))
