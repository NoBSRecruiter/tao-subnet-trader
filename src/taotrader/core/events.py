"""taotrader/core/events.py - (1) chain events derived by diffing snapshots, (2) the closed, versioned journal vocabulary.

Chain events are a pure function of two consecutive snapshots (protocol.derive.derive_events), so they are
identical in backtest (60-block stride), paper and live (per finalized block). Journal events are the ONLY way
engine state changes (engine.reducer.reduce).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, TypeVar

from .orders import FailReason, Fill, OrderIntent
from .signals import RiskAction, Signal
from .state import ReadPlan
from .units import (
    AlphaRao, Block, BlockHash, BookId, Hotkey, Mode, NetUid, OrderId, PositionKey, Ppm, PriceRao, Rao, StrategyId,
    SubnetKey,
)


# ----------------------------------------------------------------------------------------------- chain events
class ChainEventKind(StrEnum):
    REGISTERED = "registered"                    # a new generation (netuid, reg_at) appeared
    DEREGISTERED = "deregistered"                # a generation vanished (prune/dissolve); reuse => DEREGISTERED + REGISTERED
    START_CALLED = "start_called"                # FirstEmissionBlockNumber None -> Some
    EMISSION_TOGGLED = "emission_toggled"        # SubnetEmissionEnabled flip (flag = new value)
    REG_ALLOWED_TOGGLED = "reg_allowed_toggled"  # NetworkRegistrationAllowed flip
    EPOCH_DRAIN = "epoch_drain"                  # LastEpochBlock changed
    LARGE_FLOW = "large_flow"                    # |dSubnetTaoFlow| >= large_flow_frac * SubnetTAO between snapshots
    OWNER_POSITION_CHANGED = "owner_position"    # owner coldkey alpha changed beyond accrual tolerance (amount signed)
    OWNER_CHANGED = "owner_changed"              # SubnetOwner / SubnetOwnerHotkey diff
    AUTOLOCK_TOGGLED = "autolock_toggled"        # OwnerCutAutoLockEnabled flip
    TAKE_CHANGED = "take_changed"                # Delegates[h] diff for a tracked hotkey (old/new u16 text)
    DIVIDEND_MEMBERSHIP = "dividend_membership"  # tracked hotkey entered (flag True) / left AlphaDividendsPerSubnet
    REGISTRATION_SEEN = "registration_seen"      # LastRateLimitedBlock(0x02) advanced
    REG_WINDOW_OPENED = "reg_window_opened"      # block crossed last_reg_block + NetworkRateLimit
    IMMUNITY_EXPIRED = "immunity_expired"        # generation entered the prune candidate set
    PRUNE_TARGET_CHANGED = "prune_target_changed"
    GATE_BAR_UPDATED = "gate_bar_updated"
    SPEC_CHANGED = "spec_changed"                # spec_version or transaction_version changed
    PARAM_CHANGED = "param_changed"              # a freeze-list global or per-subnet value changed (name/old/new; key if per subnet)
    SAFE_MODE = "safe_mode"                      # SafeMode.EnteredUntil set or cleared (flag = active)


@dataclass(frozen=True, slots=True)
class ChainEvent:
    kind: ChainEventKind
    block: Block
    key: SubnetKey | None = None
    hotkey: Hotkey | None = None
    flag: bool | None = None          # toggles / membership / safe-mode active
    amount: int | None = None         # LARGE_FLOW: signed rao; OWNER_POSITION_CHANGED: signed alpha rao
    frac_ppm: Ppm | None = None       # LARGE_FLOW: amount / SubnetTAO
    name: str | None = None           # PARAM_CHANGED: storage item name
    old: str | None = None            # canonical text of the old value
    new: str | None = None


@dataclass(frozen=True, slots=True)
class HealthObs:
    """Wall-clock-derived observations are INPUTS: journaled with each snapshot so replays are exact.
    Backtests use HealthObs.nominal()."""
    finality_lag_blocks: int
    secs_since_block: int
    healthy_endpoints: int
    head_lag_blocks: int              # submit node vs best known head
    feed_gap_blocks: int              # blocks skipped since the previous snapshot beyond the cadence

    @staticmethod
    def nominal() -> HealthObs:
        return HealthObs(finality_lag_blocks=3, secs_since_block=12, healthy_endpoints=2, head_lag_blocks=0,
                         feed_gap_blocks=0)                      # finality lag == ExecCfg.finality_lag_blocks


# ----------------------------------------------------------------------------------------------- journal events
class JournalEvent:
    __slots__ = ()
    KIND: ClassVar[str] = ""
    VERSION: ClassVar[int] = 1

    def idem(self) -> str | None:
        """Journal-level UNIQUE idempotency key; None = not deduplicated."""
        return None


REGISTRY: dict[str, type[JournalEvent]] = {}
E = TypeVar("E", bound=type[JournalEvent])


def journal_event(kind: str, version: int = 1) -> Callable[[E], E]:
    def deco(cls: E) -> E:
        if kind in REGISTRY:
            raise ValueError(f"duplicate journal kind {kind}")
        cls.KIND = kind
        cls.VERSION = version
        REGISTRY[kind] = cls
        return cls
    return deco


# --- inputs from the world (payloads of snapshots live in the lake; the digest pins their exact content)
@journal_event("snapshot_observed")
@dataclass(frozen=True, slots=True)
class SnapshotObserved(JournalEvent):
    block: Block
    block_hash: BlockHash
    digest: str
    plan: ReadPlan
    ts_ms: int
    health: HealthObs

    def idem(self) -> str:
        return f"snap:{self.block_hash}"


@journal_event("operator_command")
@dataclass(frozen=True, slots=True)
class OperatorCommand(JournalEvent):
    block: Block
    command: str                      # "halt" | "resume" | "exits_only" | "flatten:<netuid>"
    reason: str
    nonce: str                        # from the control file; makes re-delivery idempotent

    def idem(self) -> str:
        return f"op:{self.nonce}"


@journal_event("capital_changed")
@dataclass(frozen=True, slots=True)
class CapitalChanged(JournalEvent):
    book: BookId
    block: Block
    cash_delta: int
    fee_float_delta: int
    memo: str

    def idem(self) -> str:
        return f"capital:{self.book}:{self.memo}"


@journal_event("config_applied")
@dataclass(frozen=True, slots=True)
class ConfigApplied(JournalEvent):
    block: Block
    config_hash: str
    code_hash: str
    prereg_hash: str                  # hash of config/preregistration.toml in force


@journal_event("model_drift_observed")
@dataclass(frozen=True, slots=True)
class ModelDriftObserved(JournalEvent):
    block: Block
    probe: str                        # "sim_swap_buy" | "sim_swap_sell" | "price_all" | "prune_target" | "provider"
                                      # | "emission_parity" | "yield_parity" | "hazard_validity" (these three may start
                                      # a post-spec burn-in, section 3.10 step 2)
    netuid: NetUid | None
    err_ppm: int


# --- derived chain facts (deterministic; no idem)
@journal_event("chain_event")
@dataclass(frozen=True, slots=True)
class ChainEventObserved(JournalEvent):
    event: ChainEvent


# --- accounting facts emitted by the engine
@journal_event("yield_accrued")
@dataclass(frozen=True, slots=True)
class YieldAccrued(JournalEvent):
    book: BookId
    key: SubnetKey
    hotkey: Hotkey
    block: Block
    index_before: Decimal
    index_after: Decimal
    delta_alpha: int                  # value change of OUR shares; may be -1 rao from share-pool rounding


@journal_event("dereg_settled")
@dataclass(frozen=True, slots=True)
class DeregSettled(JournalEvent):
    book: BookId
    key: SubnetKey
    hotkey: Hotkey
    block: Block
    alpha_value: AlphaRao             # our alpha at the last good snapshot
    payout_tao: Rao                   # modelled (backtest/paper/live-dry, Engine ACCOUNT) or OBSERVED free-TAO credit
                                      # (live: reconciliation only; the Engine never emits it in RunMode.LIVE)
    model: str                        # "formula" | "fixed:0.35" | "observed"

    def idem(self) -> str:
        return f"dereg:{self.book}:{self.key.netuid}:{self.key.reg_at}"


@journal_event("decision_trace")
@dataclass(frozen=True, slots=True)
class DecisionTrace(JournalEvent):
    book: BookId
    block: Block
    strategies_run: tuple[StrategyId, ...]
    signals: tuple[Signal, ...]
    actions: tuple[RiskAction, ...]
    mode: Mode
    memories: tuple[tuple[StrategyId, bytes], ...]   # canonical bytes of each strategy's new Memory, plus the
                                                     # pseudo-id "risk.router" -> RouterState (section 5.10)
    features_digest: str
    n_intents: int
    calib_digest: str = ""                           # digest of the Calibration in force (section 5.12)
    nav_liq: Rao = Rao(0)                            # book NAV_liq at this tick (reducer samples one per 7,200 blocks)
    sleeve_nav: tuple[tuple[StrategyId, Rao], ...] = ()   # un-netted stand-alone value per sleeve (kill switches)


@journal_event("mode_changed")
@dataclass(frozen=True, slots=True)
class ModeChanged(JournalEvent):
    book: BookId
    block: Block
    mode: Mode
    reason: str


@journal_event("sleeve_transfer")
@dataclass(frozen=True, slots=True)
class SleeveTransfer(JournalEvent):
    """Netting (section 3.10 step 6): one sleeve sells to another at the decision spot. Virtual: reduce moves
    SleeveHolding shares from -> to and sleeve_cash to -> from, and posts NO ledger entries."""
    book: BookId
    block: Block
    key: SubnetKey
    from_strategy: StrategyId
    to_strategy: StrategyId
    shares: Decimal
    tao: Rao
    price: PriceRao

    def idem(self) -> str:
        return (f"xfer:{self.book}:{self.block}:{self.key.netuid}:{self.key.reg_at}:"
                f"{self.from_strategy}:{self.to_strategy}")


@journal_event("order_intended")
@dataclass(frozen=True, slots=True)
class OrderIntended(JournalEvent):
    intent: OrderIntent

    def idem(self) -> str:
        return f"intent:{self.intent.order_id}"


@journal_event("order_cancelled")
@dataclass(frozen=True, slots=True)
class OrderCancelled(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    reason: str

    def idem(self) -> str:
        return f"cancel:{self.order_id}:{self.attempt}"


# --- execution-side facts (the write-ahead bracket)
@journal_event("submit_started")
@dataclass(frozen=True, slots=True)
class SubmitStarted(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    delegate: str                     # fee-paying delegate id ("sim0".."sim2" for simulators)
    nonce: int | None                 # live: carrier nonce n reserved by venue.reserve() (pool-aware
                                      # system_accountNextIndex read immediately before this record); inner = n + 1
    era_end: Block | None = None      # last block of the carrier's mortal era (finalized anchor + 8, or + 16 unshielded,
                                      # + 2 margin): the delegate's nonce lock and the carrier-fee settlement point

    def idem(self) -> str:
        return f"submit:{self.order_id}:{self.attempt}"


@journal_event("venue_ack")
@dataclass(frozen=True, slots=True)
class VenueAck(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    submit_block: Block               # best head at submit
    expected_fill_block: Block        # submit_block + shield latency (+ sim latency)
    carrier_hash: str                 # live: carrier extrinsic hash; sim: ""
    inner_hash: str                   # live: inner (proxied) extrinsic hash; sim: ""

    def idem(self) -> str:
        return f"ack:{self.order_id}:{self.attempt}"


@journal_event("submit_unknown")
@dataclass(frozen=True, slots=True)
class SubmitUnknown(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    detail: str                       # exception text | "recovered_submitting" | "nonce_mismatch:<used nonce>"


@journal_event("fill_reported")
@dataclass(frozen=True, slots=True)
class FillReported(JournalEvent):
    fill: Fill

    def idem(self) -> str:
        return f"fill:{self.fill.fill_id}"


@journal_event("order_failed")
@dataclass(frozen=True, slots=True)
class OrderFailed(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    reason: FailReason
    tx_fee: Rao                       # a failed inner call still pays; SHIELD_MISSED: carrier fee if the carrier is in
                                      # N+2, else 0 (live then settles it with CarrierFeeSettled)
    expired: bool = False             # True -> EXPIRED terminal state, else FAILED
    exact_block: bool = True          # False: evaluated on a later stride snapshot (stride replays only)
    detail: str = ""                  # decoded chain error name, e.g. a Proxy.ProxyExecuted Err (section 9.6)

    def idem(self) -> str:
        return f"fail:{self.order_id}:{self.attempt}"


@journal_event("carrier_fee_settled")
@dataclass(frozen=True, slots=True)
class CarrierFeeSettled(JournalEvent):
    """Live only. After a shield miss with the carrier ABSENT from N+2, the carrier fee is settled from the
    delegate's nonce once era_end has passed:
    nonce n -> never included (fee 0); n + 1 -> carrier included, inner dropped (fee = observed delegate balance
    change); >= n + 2 -> the inner was included after all: LiveVenue reads its dispatch result and reconciliation
    raises a key alarm. Valid on an EXPIRED order; posts fee_float -fee, fees:tx +fee."""
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    fee_rao: Rao
    outcome: str                      # "never_included" | "carrier_only" | "inner_included"

    def idem(self) -> str:
        return f"carrier:{self.order_id}:{self.attempt}"


@journal_event("recon_adjusted")
@dataclass(frozen=True, slots=True)
class ReconAdjusted(JournalEvent):
    """Live only: chain truth wins. Entries halt until QuarantineCleared."""
    book: BookId
    block: Block
    cash_delta: int
    fee_float_delta: int
    share_deltas: tuple[tuple[PositionKey, Decimal], ...]
    evidence: str


@journal_event("quarantine_cleared")
@dataclass(frozen=True, slots=True)
class QuarantineCleared(JournalEvent):
    book: BookId
    block: Block
    reason: str
