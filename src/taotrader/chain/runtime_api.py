"""taotrader/chain/runtime_api.py - state_call encoders and decoders (DESIGN.md section 6.6).

Arguments are SCALE little-endian, hex-encoded "0x...". Every decoder checks the exact length (all-or-nothing).
VERIFY results against the spec-475 golden fixtures (block 9,240,388):

- SwapRuntimeApi_current_alpha_price_all: Vec<(u16 netuid, u64 rao/alpha)>, compact length prefix, 10 bytes per entry,
  129 entries including root (root = 1e9).
- SwapRuntimeApi_sim_swap_*: 48 bytes = 6 x u64 (spec >= 391); 32 bytes = 4 x u64 (specs 302-377, slippage = 0).
  All-zero = failure (the runtime returns zeros instead of an error).
- SubnetInfoRuntimeApi_get_subnet_to_prune: Option<u16> (0x01 5c00 = Some(92)).
- SubnetInfoRuntimeApi_get_next_epoch_start_block: Option<u64> (9 bytes when Some).
- SubnetInfoRuntimeApi_get_block_emission: plain u64 (8 bytes), not an Option.
- SubnetRegistrationRuntimeApi_get_network_registration_cost: u64 rao.
- StakeInfoRuntimeApi_get_stake_info_for_coldkey(AccountId32): Vec<StakeInfo> with StakeInfo = {hotkey: AccountId32,
  coldkey: AccountId32, netuid: Compact<u16>, stake: Compact<u64>, locked: Compact<u64>, emission: Compact<u64>,
  tao_emission: Compact<u64>, drain: Compact<u64>, is_registered: bool}. Decoded by hand (no scalecodec needed on
  Windows): the escrow coldkey's 7,296 rows parse to exactly the 573,461 returned bytes, SN92 escrow 48,922 alpha.
- BetaBasketRuntimeApi_get_all_validator_baskets is NOT decoded (complex nested structs that need the V15 runtime-API
  type metadata); escrow per subnet comes from the escrow coldkey's StakeInfo rows (the section 6.6 fallback).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from taotrader.core.errors import DecodeError
from taotrader.core.protocols import SwapSim

from .hashing import account, le16, le64, to_hex
from .scale import d_account, d_compact, d_u16, d_u64

M_PRICE: Final[str] = "SwapRuntimeApi_current_alpha_price"
M_PRICE_ALL: Final[str] = "SwapRuntimeApi_current_alpha_price_all"
M_SIM_BUY: Final[str] = "SwapRuntimeApi_sim_swap_tao_for_alpha"
M_SIM_SELL: Final[str] = "SwapRuntimeApi_sim_swap_alpha_for_tao"
M_PRUNE: Final[str] = "SubnetInfoRuntimeApi_get_subnet_to_prune"
M_NEXT_EPOCH: Final[str] = "SubnetInfoRuntimeApi_get_next_epoch_start_block"
M_BLOCK_EMISSION: Final[str] = "SubnetInfoRuntimeApi_get_block_emission"
M_REG_COST: Final[str] = "SubnetRegistrationRuntimeApi_get_network_registration_cost"
M_STAKE_INFO_COLDKEY: Final[str] = "StakeInfoRuntimeApi_get_stake_info_for_coldkey"
M_BASKETS: Final[str] = "BetaBasketRuntimeApi_get_all_validator_baskets"

# The basket escrow account: "modl" ++ PalletId(b"subtensr") ++ b"beta/esc", zero-padded to 32 bytes (brief 3.7).
ESCROW_ACCOUNT: Final[str] = to_hex((b"modl" + b"subtensr" + b"beta/esc").ljust(32, b"\x00"))


def _b(hexstr: object) -> bytes:
    if not isinstance(hexstr, str) or not hexstr.startswith("0x"):
        raise DecodeError(f"runtime API result is not 0x-hex: {str(hexstr)[:60]}")
    try:
        return bytes.fromhex(hexstr[2:])
    except ValueError as e:
        raise DecodeError(f"runtime API result is not hex: {hexstr[:60]}") from e


# ------------------------------------------------------------------------------------------------ encoders
def args_netuid(netuid: int) -> str:
    return to_hex(le16(netuid))


def args_netuid_amount(netuid: int, amount: int) -> str:
    if amount < 0 or amount >= 2**64:
        raise ValueError(f"amount out of u64 range: {amount}")
    return to_hex(le16(netuid) + le64(amount))


def args_account(acct_hex: str) -> str:
    return to_hex(account(acct_hex))


NO_ARGS: Final[str] = "0x"


# ------------------------------------------------------------------------------------------------ decoders
def dec_u64(result: object) -> int:
    return d_u64(_b(result))


def dec_price_all(result: object) -> dict[int, int]:
    b = _b(result)
    n, used = d_compact(b)
    if used + 10 * n != len(b):
        raise DecodeError(f"price_all: {n} entries x 10 B + {used} != {len(b)} B")
    out: dict[int, int] = {}
    for i in range(n):
        o = used + 10 * i
        net = d_u16(b[o:o + 2])
        if net in out:
            raise DecodeError(f"price_all: duplicate netuid {net}")
        out[net] = d_u64(b[o + 2:o + 10])
    return out


def dec_sim_swap(result: object) -> SwapSim:
    """48-byte (6 x u64) or legacy 32-byte (4 x u64, specs 302-377) SimSwapResult. All-zero is returned as is: callers
    must treat it as failure (`sim_failed`)."""
    b = _b(result)
    if len(b) == 48:
        v = [d_u64(b[i:i + 8]) for i in range(0, 48, 8)]
        return SwapSim(*v)
    if len(b) == 32:
        v = [d_u64(b[i:i + 8]) for i in range(0, 32, 8)]
        return SwapSim(v[0], v[1], v[2], v[3], 0, 0)
    raise DecodeError(f"sim_swap: expected 48 or 32 bytes, got {len(b)}")


def sim_failed(s: SwapSim) -> bool:
    """The runtime returns zeros on any swap error: alpha_amount == 0 or tao_amount == 0 is a failure."""
    return s.tao_amount == 0 or s.alpha_amount == 0


def _strict_option(b: bytes, width: int) -> bytes | None:
    if b == b"\x00":
        return None
    if len(b) == width + 1 and b[0] == 1:
        return b[1:]
    raise DecodeError(f"Option: expected 0x00 or 0x01 ++ {width} bytes, got {len(b)}: 0x{b.hex()[:40]}")


def dec_prune_target(result: object) -> int | None:
    inner = _strict_option(_b(result), 2)
    return None if inner is None else d_u16(inner)


def dec_next_epoch(result: object) -> int | None:
    inner = _strict_option(_b(result), 8)
    return None if inner is None else d_u64(inner)


@dataclass(frozen=True, slots=True)
class StakeInfo:
    hotkey: str
    coldkey: str
    netuid: int
    stake: int            # alpha rao (value of the position)
    locked: int
    emission: int
    tao_emission: int
    drain: int
    is_registered: bool


def dec_stake_info_vec(result: object) -> tuple[StakeInfo, ...]:
    b = _b(result)
    n, o = d_compact(b)
    out: list[StakeInfo] = []
    for _ in range(n):
        if o + 64 > len(b):
            raise DecodeError("StakeInfo: truncated account ids")
        hk = d_account(b[o:o + 32])
        ck = d_account(b[o + 32:o + 64])
        o += 64
        vals: list[int] = []
        for _f in range(6):
            v, used = d_compact(b, o)
            vals.append(v)
            o += used
        if o >= len(b) or b[o] > 1:
            raise DecodeError("StakeInfo: bad is_registered byte")
        reg = b[o] == 1
        o += 1
        if vals[0] >= 2**16:
            raise DecodeError(f"StakeInfo: netuid {vals[0]} out of u16 range")
        out.append(StakeInfo(hk, ck, vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], reg))
    if o != len(b):
        raise DecodeError(f"StakeInfo: {len(b) - o} trailing bytes after {n} rows")
    return tuple(out)


def escrow_by_subnet(rows: tuple[StakeInfo, ...], escrow: str = ESCROW_ACCOUNT) -> dict[int, int]:
    """Escrow alpha E per netuid (root excluded): the sum of the escrow coldkey's stake over all validator hotkeys."""
    out: dict[int, int] = {}
    for r in rows:
        if r.coldkey != escrow:
            raise DecodeError(f"StakeInfo row for coldkey {r.coldkey} in the escrow listing")
        if r.netuid == 0:
            continue
        out[r.netuid] = out.get(r.netuid, 0) + r.stake
    return dict(sorted(out.items()))


def method_args(method: str, args: Mapping[str, int] | None = None) -> str:
    """Hex args for the methods above (convenience for tests and tools)."""
    a = dict(args or {})
    if method in (M_PRICE, M_NEXT_EPOCH):
        return args_netuid(a["netuid"])
    if method == M_SIM_BUY:
        return args_netuid_amount(a["netuid"], a["tao_rao"])
    if method == M_SIM_SELL:
        return args_netuid_amount(a["netuid"], a["alpha_rao"])
    return NO_ARGS
