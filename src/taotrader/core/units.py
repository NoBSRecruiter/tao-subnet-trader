"""taotrader/core/units.py - units, identities, logical time, mode enums.

Stdlib only. No I/O, no clock, no randomness. Every on-chain amount is an integer
in its smallest unit; the NewType names carry the unit so mypy --strict catches mix-ups.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final, NewType

NetUid = NewType("NetUid", int)          # u16; 0 = root
Block = NewType("Block", int)            # block number
BlockHash = NewType("BlockHash", str)    # "0x" + 64 lowercase hex
Rao = NewType("Rao", int)                # TAO amount in rao (1 TAO = 1e9 rao)
AlphaRao = NewType("AlphaRao", int)      # alpha amount of ONE subnet generation, 1e-9 alpha units
PriceRao = NewType("PriceRao", int)      # rao of TAO per 1 whole alpha (= TAO/alpha * 1e9): the chain's limit_price unit
Ppm = NewType("Ppm", int)                # parts per million (fractions, weights, slippage budgets)
PpmPerDay = NewType("PpmPerDay", int)    # rate: ppm per 7,200 blocks (0.10 %/day = 1_000)
Hotkey = NewType("Hotkey", str)          # "0x" + 64 hex public key; ss58 only inside taotrader.live
Coldkey = NewType("Coldkey", str)        # same encoding as Hotkey
StrategyId = NewType("StrategyId", str)  # "carry", "momentum", "lcw", "baseline.ew_total", ...
BookId = NewType("BookId", str)          # one simulated/paper/live portfolio inside a run
OrderId = NewType("OrderId", str)        # deterministic blake2b-96 hex, never random

RAO_PER_TAO: Final[int] = 10**9
PPM: Final[int] = 1_000_000
BLOCKS_PER_DAY: Final[int] = 7_200
BLOCK_SECONDS: Final[int] = 12
FEE_DEN: Final[int] = 65_535             # FeeRate, Delegates take, ChildkeyTake, SubnetOwnerCut are /65535
PERQUINTILL: Final[int] = 10**18         # Swap.SwapBalancer.quote raw scale
HALF_E18: Final[int] = 5 * 10**17
U64_MAX: Final[int] = 2**64 - 1          # chain "whole position" sentinel (spec >= 469); NEVER sent live (Policy: unbounded)
DEFAULT_TAKE_U16: Final[int] = 11_796    # 18%: take of any hotkey that never set Delegates
MIN_STAKE_RAO: Final[int] = 2_000_000    # DefaultMinStake (0.002 TAO)


@dataclass(frozen=True, slots=True, order=True)
class SubnetKey:
    """Asset identity = one GENERATION of a netuid. netuids are reused about weekly.

    Never key anything (features, positions, history, P&L) by netuid alone.
    """
    netuid: NetUid
    reg_at: Block          # SubtensorModule.NetworkRegisteredAt[netuid] at observation time


@dataclass(frozen=True, slots=True, order=True)
class PositionKey:
    subnet: SubnetKey
    hotkey: Hotkey


class Phase(IntEnum):
    """Intra-block ordering, identical in every mode.

    The snapshot batch (INGEST..EMIT) commits first; fills due at this block (VENUE)
    commit after it; outbox submissions (OUTBOX) last.
    """
    INGEST = 0
    ACCOUNT = 1
    DECIDE = 2
    EMIT = 3
    VENUE = 4
    OUTBOX = 5


@dataclass(frozen=True, slots=True, order=True)
class LogicalTime:
    """Chain time only. No wall clock ever enters a decision."""
    block: Block
    phase: Phase = Phase.INGEST
    sub: int = 0


class Mode(IntEnum):
    """Overlay operating mode (risk/modes.py). Higher = more restrictive."""
    NORMAL = 0       # everything allowed
    CAUTION = 1      # no increases; exits and trims allowed
    EXITS_ONLY = 2   # only forced risk exits (via runtime-API state if local models are unvalidated)
    FROZEN = 3       # nothing is submitted (SafeMode, key alarm, no healthy head endpoint)


class Stage(IntEnum):
    """Evidence stage of a sleeve. Budgets are funded by stage, never by backtest P&L."""
    RESEARCH = 0       # backtest/offline only
    SHADOW = 1         # runs live on paper feed, signals journaled, zero budget
    PAPER = 2          # paper budget
    LIVE_ELIGIBLE = 3  # passed every gate; the USER decides whether to list it in [live].sleeves


class RunMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE_DRY = "live_dry"   # real reads, real plan(), never submits
    LIVE = "live"           # gated; user-run on Linux/WSL only
