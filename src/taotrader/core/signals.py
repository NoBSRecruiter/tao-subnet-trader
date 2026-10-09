"""taotrader/core/signals.py - the declarative decision layer: strategies emit Signals, never orders.

Floats stop here: every field is an integer (ppm / rao / blocks). Strategies convert with core.fixed.to_ppm.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from .orders import Attribution, Urgency
from .units import Block, Hotkey, Mode, Ppm, PpmPerDay, PriceRao, Rao, StrategyId, SubnetKey


class SignalKind(StrEnum):
    TARGET = "target"   # "I want to hold this, sized at weight_ppm of my sleeve (subject to caps)"
    EXIT = "exit"       # "my sleeve's share must go to 0" (sleeve-level exit; never touches other sleeves)
    AVOID = "avoid"     # "do not open new exposure in this name for my sleeve"


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: StrategyId
    key: SubnetKey
    asof: Block
    kind: SignalKind
    weight_ppm: Ppm = Ppm(0)                 # TARGET: share of the strategy's sleeve budget
    edge_ppm_day: PpmPerDay = PpmPerDay(0)   # expected TAO return per day held, net of take & dilution, BEFORE trading costs
    alpha_h_ppm: Ppm = Ppm(0)                # expected gross return over horizon_blocks; >0 lets the allocator apply V*
    max_size_rao: Rao | None = None          # sleeve's own size cap (e.g. carry V*); applied before overlay caps
    horizon_blocks: int = 0                  # opinion time-to-live; 0 = until the strategy's next evaluation
    urgency: Urgency = Urgency.NORMAL        # EXIT may be HIGH (sleeve thesis stop); risk urgencies belong to the overlay
    hotkey_pref: Hotkey | None = None        # advisory only; YieldRouter picks the one hotkey per subnet
    declares_dilution: bool = False          # True: sleeve already netted structural sell load; overlay must not re-haircut
    reasons: tuple[str, ...] = ()            # machine-readable reason codes


@dataclass(frozen=True, slots=True)
class StrategyOutput:
    signals: tuple[Signal, ...]
    memory: object                           # the strategy's own frozen Memory dataclass (codec-encodable)


@dataclass(frozen=True, slots=True)
class ForcedExit:
    key: SubnetKey
    urgency: Urgency                         # EMERGENCY / URGENT (risk) or NORMAL (liquidity trim, launch stop)
    rule: str                                # "prune_A" | "prune_B" | "prune_backstop" | "prune_target" | "emission_off" |
                                             # "owner" | "burn" | "liquidity_trim" | "dissolved" | "operator" | ...
    exit_slip_ppm: Ppm                       # average-slippage budget for the marginal limit
    trim_to_rao: Rao | None = None           # None = full exit; else target executable value after the trim


@dataclass(frozen=True, slots=True)
class TargetPosition:
    key: SubnetKey
    hotkey: Hotkey                           # the book's router choice (risk/router.py; one hotkey per coldkey and subnet)
    value_rao: Rao                           # target executable (sim_sell) value in TAO after allocation and caps
    urgency: Urgency
    attribution: Attribution
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SleeveXfer:
    """One netting transfer planned by the allocator (section 3.10 step 6); the Engine journals it as SleeveTransfer."""
    key: SubnetKey
    from_strategy: StrategyId
    to_strategy: StrategyId
    shares: Decimal                          # share-pool shares moved from -> to (exact)
    tao: Rao                                 # virtual TAO paid to -> from at the decision spot
    price: PriceRao                          # decision spot on the book's view


@dataclass(frozen=True, slots=True)
class TargetBook:
    asof: Block
    items: tuple[TargetPosition, ...]        # sorted by key
    forced: tuple[ForcedExit, ...] = ()
    halt_entries: bool = False
    transfers: tuple[SleeveXfer, ...] = ()   # netting between sleeves; sorted by (key, from, to); never touches the pool

    def get(self, key: SubnetKey) -> TargetPosition | None:
        for t in self.items:
            if t.key == key:
                return t
        return None

    def reduced(self, key: SubnetKey, value_rao: Rao, reason: str, urgency: Urgency | None = None) -> TargetBook:
        """Monotone by construction: a risk overlay may lower a target, never raise it."""
        out: list[TargetPosition] = []
        for t in self.items:
            if t.key == key:
                if value_rao > t.value_rao:
                    raise ValueError("risk overlay tried to increase a target")
                t = replace(t, value_rao=value_rao, reasons=t.reasons + (reason,),
                            urgency=urgency if urgency is not None else t.urgency)
            out.append(t)
        return replace(self, items=tuple(out))


@dataclass(frozen=True, slots=True)
class RiskAction:
    rule: str                 # e.g. "prune.entry_rank", "liquidity.vcap", "mode.caution", "owner.cooldown"
    key: SubnetKey | None
    action: str               # "CLAMP" | "FORCE_EXIT" | "VETO_ENTRY" | "HALT_ENTRIES" | "MODE" | "MONITOR"
    detail: str               # canonical "k=v;k=v" text (journaled, used by reports)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    targets: TargetBook
    actions: tuple[RiskAction, ...]
    mode: Mode
