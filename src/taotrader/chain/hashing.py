"""taotrader/chain/hashing.py - Substrate storage-key construction (DESIGN.md section 6.3).

Storage key = twox128(pallet) ++ twox128(item) ++ hasher1(k1) [++ hasher2(k2) [++ hasher3(k3)]]; a plain value's
key is the 32-byte prefix only. Hashers (all little-endian):

- Twox128           xxh64(b, seed 0) LE ++ xxh64(b, seed 1) LE           (pallet and item names)
- Twox64Concat      xxh64(b, seed 0) as 8 LE bytes ++ b                   (Swap maps keyed by netuid)
- Blake2_128Concat  blake2b-128(b) ++ b                                   (hotkey / coldkey keys)
- Identity          b                                                     (u16 netuid keys of SubtensorModule)
- Blake2_128, Blake2_256, Twox256 are implemented for completeness (non-concat hashers cannot be reversed).

Check value: twox128(b"System").hex() == "26aa394eea5630e07c48ae0c9558cef7".
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache

import xxhash


class Hasher(StrEnum):
    """Metadata hasher names (StorageHasher variants) as they appear in V14 metadata."""
    IDENTITY = "Identity"
    TWOX64_CONCAT = "Twox64Concat"
    BLAKE2_128_CONCAT = "Blake2_128Concat"
    BLAKE2_128 = "Blake2_128"
    BLAKE2_256 = "Blake2_256"
    TWOX128 = "Twox128"
    TWOX256 = "Twox256"

    @property
    def concat_prefix_len(self) -> int | None:
        """Bytes of hash before the raw key in a concat hasher (0 for Identity); None if the key is not recoverable."""
        return _CONCAT_PREFIX.get(self)

    def apply(self, b: bytes) -> bytes:
        return _HASHERS[self](b)


def _xxh64(b: bytes, seed: int) -> bytes:
    return xxhash.xxh64_intdigest(b, seed=seed).to_bytes(8, "little")


def twox64(b: bytes) -> bytes:
    return _xxh64(b, 0)


def twox128(b: bytes) -> bytes:
    return _xxh64(b, 0) + _xxh64(b, 1)


def twox256(b: bytes) -> bytes:
    return _xxh64(b, 0) + _xxh64(b, 1) + _xxh64(b, 2) + _xxh64(b, 3)


def twox64_concat(b: bytes) -> bytes:
    return _xxh64(b, 0) + b


def blake2_128(b: bytes) -> bytes:
    return hashlib.blake2b(b, digest_size=16).digest()


def blake2_256(b: bytes) -> bytes:
    return hashlib.blake2b(b, digest_size=32).digest()


def blake2_128_concat(b: bytes) -> bytes:
    return blake2_128(b) + b


def identity(b: bytes) -> bytes:
    return b


_HASHERS = {
    Hasher.IDENTITY: identity,
    Hasher.TWOX64_CONCAT: twox64_concat,
    Hasher.BLAKE2_128_CONCAT: blake2_128_concat,
    Hasher.BLAKE2_128: blake2_128,
    Hasher.BLAKE2_256: blake2_256,
    Hasher.TWOX128: twox128,
    Hasher.TWOX256: twox256,
}
_CONCAT_PREFIX = {Hasher.IDENTITY: 0, Hasher.TWOX64_CONCAT: 8, Hasher.BLAKE2_128_CONCAT: 16}


# ------------------------------------------------------------------------------------------------ key encoders
def le16(n: int) -> bytes:
    return n.to_bytes(2, "little")


def le32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def le64(n: int) -> bytes:
    return n.to_bytes(8, "little")


def account(hex32: str) -> bytes:
    """A 32-byte account id from "0x" + 64 hex (core Hotkey/Coldkey encoding)."""
    raw = from_hex(hex32)
    if len(raw) != 32:
        raise ValueError(f"account id must be 32 bytes, got {len(raw)}: {hex32!r}")
    return raw


def to_hex(b: bytes) -> str:
    return "0x" + b.hex()


def from_hex(s: str) -> bytes:
    if not s.startswith("0x"):
        raise ValueError(f"hex string must start with 0x: {s[:20]!r}")
    return bytes.fromhex(s[2:])


@lru_cache(maxsize=1024)
def prefix(pallet: str, item: str) -> bytes:
    """twox128(pallet) ++ twox128(item): the 32-byte storage prefix (and the whole key of a plain value)."""
    return twox128(pallet.encode()) + twox128(item.encode())


def storage_key(pallet: str, item: str, parts: Sequence[tuple[Hasher, bytes]] = ()) -> bytes:
    """prefix ++ hasher1(k1) ++ hasher2(k2) ... with each key part already SCALE-encoded."""
    out = prefix(pallet, item)
    for h, raw in parts:
        out += h.apply(raw)
    return out


def split_key(key: bytes, pallet: str, item: str, hashers: Sequence[Hasher], widths: Sequence[int]) -> tuple[bytes, ...]:
    """Recover the encoded key parts of a full storage key (state_getKeysPaged output). Every hasher must be a concat
    hasher or Identity, and every part has a fixed encoded width. Raises ValueError on any mismatch."""
    pre = prefix(pallet, item)
    if not key.startswith(pre):
        raise ValueError(f"key does not start with the {pallet}.{item} prefix")
    if len(hashers) != len(widths):
        raise ValueError("hashers and widths differ in length")
    pos = len(pre)
    parts: list[bytes] = []
    for h, w in zip(hashers, widths, strict=True):
        skip = h.concat_prefix_len
        if skip is None:
            raise ValueError(f"hasher {h} is not reversible")
        head, raw = key[pos:pos + skip], key[pos + skip:pos + skip + w]
        if len(raw) != w or h.apply(raw) != head + raw:
            raise ValueError(f"{pallet}.{item}: key part {len(parts)} does not verify under {h}")
        parts.append(raw)
        pos += skip + w
    if pos != len(key):
        raise ValueError(f"{pallet}.{item}: {len(key) - pos} trailing key bytes")
    return tuple(parts)


# ------------------------------------------------------------------------------------------------ named builders
def k_subnet_tao(n: int) -> bytes:
    return prefix("SubtensorModule", "SubnetTAO") + identity(le16(n))


def k_balancer(n: int) -> bytes:
    return prefix("Swap", "SwapBalancer") + twox64_concat(le16(n))


def k_hk_alpha(hk32: bytes, n: int) -> bytes:
    return prefix("SubtensorModule", "TotalHotkeyAlpha") + blake2_128_concat(hk32) + identity(le16(n))


def k_alpha_divs(n: int, hk32: bytes) -> bytes:
    return prefix("SubtensorModule", "AlphaDividendsPerSubnet") + identity(le16(n)) + blake2_128_concat(hk32)


RATE_LIMIT_KEY_NETWORK_LAST_REGISTERED = b"\x02"     # RateLimitKey::NetworkLastRegistered (enum variant 2, no fields)


def k_last_reg() -> bytes:
    return prefix("SubtensorModule", "LastRateLimitedBlock") + identity(RATE_LIMIT_KEY_NETWORK_LAST_REGISTERED)
