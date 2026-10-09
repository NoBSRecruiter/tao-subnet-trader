"""taotrader/core/orders.py - order intents, fills, the order FSM, deterministic ids."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import IntEnum, StrEnum

from .units import PPM, AlphaRao, Block, BookId, Hotkey, OrderId, Ppm, PriceRao, Rao, StrategyId, SubnetKey

Attribution = tuple[tuple[StrategyId, Ppm], ...]   # parts-per-million split across originating sleeves; sums to PPM


class OrderKind(StrEnum):
    ADD_STAKE_LIMIT = "add_stake_limit"                  # call 88: buy alpha with TAO
    REMOVE_STAKE_LIMIT = "remove_stake_limit"            # call 89: sell alpha (partial, or the whole position when full_position)
    REMOVE_STAKE_FULL_LIMIT = "remove_stake_full_limit"  # call 103: whole position, Option limit (sim only; live maps it to 89)
    MOVE_STAKE = "move_stake"                            # call 85, same netuid: hotkey switch, no swap fee
    MOVE_STAKE_LIMIT = "move_stake_limit"                # call 149, cross-subnet rotation: DISABLED until FT-M5c passes


class Urgency(IntEnum):
    """Planner priority. Execution order: EMERGENCY > URGENT > HIGH > NORMAL > LOW."""
    LOW = 0
    NORMAL = 1       # rebalances, soft exits, trims
    HIGH = 2         # time-critical entries, sleeve thesis stops
    URGENT = 3       # emission-off, prune backstop / Tier B, owner exits
    EMERGENCY = 4    # prune Tier A, held subnet is the prune target


class OrderState(StrEnum):
    INTENDED = "INTENDED"      # journaled, nothing sent (the outbox)
    SUBMITTING = "SUBMITTING"  # SubmitStarted journaled BEFORE any I/O
    SUBMITTED = "SUBMITTED"    # venue acked; in flight
    UNKNOWN = "UNKNOWN"        # I/O raised or crashed mid-submit: resolve from chain truth, NEVER blind-resend
    FILLED = "FILLED"          # terminal (full or partial-final)
    FAILED = "FAILED"          # terminal (chain error or venue reject; tx fee may have been paid)
    EXPIRED = "EXPIRED"        # terminal (shield miss / era expiry; provably not executed)
    CANCELLED = "CANCELLED"    # terminal (never submitted: mode change, pool gone)


TERMINAL: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED, OrderState.CANCELLED})

# SUBMITTING never goes straight to FILLED or EXPIRED. A crash leaves an order in SUBMITTING, and the Runner
# journals SubmitUnknown(detail="recovered_submitting") (SUBMITTING -> UNKNOWN) BEFORE any venue.resolve(), so a
# resolved fill or miss always applies to UNKNOWN (section 4.5). Reducer test: SUBMITTING + crash + resolve(LANDED)
# ends FILLED with no orphan.
_NEXT: dict[OrderState, frozenset[OrderState]] = {
    OrderState.INTENDED: frozenset({OrderState.SUBMITTING, OrderState.CANCELLED}),
    OrderState.SUBMITTING: frozenset({OrderState.SUBMITTED, OrderState.FAILED, OrderState.UNKNOWN}),
    OrderState.UNKNOWN: frozenset({OrderState.SUBMITTED, OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED}),
    OrderState.SUBMITTED: frozenset({OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED}),
}


class FailReason(StrEnum):
    PRICE_LIMIT_EXCEEDED = "PriceLimitExceeded"     # limit already crossed at execution (fee paid)
    SLIPPAGE_TOO_HIGH = "SlippageTooHigh"           # fill-or-kill amount above max_amount_to_limit (fee paid)
    AMOUNT_TOO_LOW = "AmountTooLow"
    INSUFFICIENT_LIQUIDITY = "InsufficientLiquidity"
    RESERVES_TOO_LOW = "ReservesTooLow"
    SWAP_INPUT_TOO_LARGE = "SwapInputTooLarge"
    SUBTOKEN_DISABLED = "SubtokenDisabled"
    SUBNET_NOT_EXISTS = "SubnetNotExists"           # generation dissolved before the order landed
    NOT_ENOUGH_STAKE = "NotEnoughStakeToWithdraw"
    STAKE_UNAVAILABLE = "StakeUnavailable"          # locks / collateral
    PROXY_ERROR = "ProxyExecutedErr"                # Proxy.ProxyExecuted{result: Err} under ExtrinsicSuccess
    CALL_FILTERED = "CallFiltered"
    SHIELD_MISSED = "ShieldMissed"                  # inner not executed in N+2 (final once N+2 is finalized). Carrier in
                                                    # N+2: tx_fee = carrier fee. Carrier absent: tx_fee 0, and live books
                                                    # any later inclusion's fee with CarrierFeeSettled
    ERA_EXPIRED = "EraExpired"
    NOT_PLACED = "NotPlaced"                        # provably never reached the chain (resolve())
    SAFE_MODE = "SafeMode"
    VENUE_REJECT = "VenueReject"                    # pre-submit check (caps, crossed limit, plan() violation)
    OTHER = "Other"


def make_order_id(run_id: str, book: BookId, block: Block, key: SubnetKey, hotkey: Hotkey,
                  kind: OrderKind, attempt: int) -> OrderId:
    """Deterministic: a replay reproduces the same id, so a duplicate submission is structurally impossible."""
    raw = f"{run_id}|{book}|{block}|{key.netuid}|{key.reg_at}|{hotkey}|{kind.value}|{attempt}".encode()
    return OrderId(hashlib.blake2b(raw, digest_size=12).hexdigest())


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: OrderId
    attempt: int                     # 0 for the first decision; re-decisions after a terminal failure increment it
    book: BookId
    created_block: Block
    kind: OrderKind
    key: SubnetKey                   # origin generation
    hotkey: Hotkey                   # origin hotkey (staking target for buys)
    tao_in: Rao                      # ADD_STAKE_LIMIT: gross TAO incl. swap fee; else 0
    alpha_in: AlphaRao               # REMOVE_*/MOVE_*: alpha to sell/move; ignored when full_position
    full_position: bool              # whole position on (key, hotkey); exempt from the partial-sell minimum. Sim: all
                                     # shares; live: the exact alpha read at the submit head, NEVER 'all'/u64::MAX
    limit_price: PriceRao            # buy: post-fill MARGINAL spot <= limit and limit > spot; sell: >= limit and limit < spot;
                                     # MOVE_STAKE_LIMIT: min dest alpha per origin alpha * 1e9; MOVE_STAKE: 0
    allow_partial: bool
    shielded: bool                   # submit_shielded (era 8). False only for risk exits when finality lag > 5 blocks
    valid_until: Block               # shielded: planned inclusion block (created + finality_lag + latency); the venue's
                                     # VenueAck.expected_fill_block (submit head + 2) is the ONLY legal inclusion block.
                                     # unshielded (era 16): created + finality_lag + 16. Nonce locks use SubmitStarted.era_end
    expected_out: int                # model output at decision (alpha rao for buys, rao for sells)
    urgency: Urgency
    attribution: Attribution
    reason: str                      # machine-readable rule/reason code
    dest_key: SubnetKey | None = None
    dest_hotkey: Hotkey | None = None

    def __post_init__(self) -> None:
        if sum(p for _, p in self.attribution) != PPM:
            raise ValueError("attribution must sum to 1e6 ppm")
        if self.kind is OrderKind.ADD_STAKE_LIMIT:
            if self.tao_in <= 0 or self.alpha_in != 0 or self.full_position:
                raise ValueError("ADD_STAKE_LIMIT needs tao_in > 0, alpha_in == 0, full_position False")
        elif self.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
            if self.tao_in != 0 or (self.alpha_in <= 0 and not self.full_position):
                raise ValueError("REMOVE needs tao_in == 0 and alpha_in > 0 or full_position")
        else:
            if self.dest_hotkey is None or (self.kind is OrderKind.MOVE_STAKE_LIMIT and self.dest_key is None):
                raise ValueError("MOVE orders need a destination")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str                     # f"{order_id}:{attempt}:{leg}" - idempotency key of the accounting effect
    order_id: OrderId
    attempt: int
    book: BookId
    block: Block                     # inclusion block
    kind: OrderKind
    key: SubnetKey
    hotkey: Hotkey
    tao: Rao                         # BUY: gross TAO debited incl. swap fee; SELL: TAO credited; MOVE: 0
    alpha: AlphaRao                  # BUY: alpha received; SELL: alpha sold (pool absorbs all); MOVE: alpha moved
    shares: Decimal                  # shares credited (BUY) / debited (SELL, MOVE origin); exact
    swap_fee: int                    # input-side units: rao (BUY) or alpha rao (SELL); 0 for same-subnet MOVE
    author_fee_tao: Rao              # SELL: TAO paid out of the pool to the block author for the fee alpha
    tx_fee: Rao                      # carrier + inner extrinsic fee paid by the fee payer
    d_pool_tao: int                  # reserve change caused by this fill (own-impact overlay input)
    d_pool_alpha: int
    spot_before: PriceRao
    shortfall_ppm: Ppm               # 1 - executed / spot, incl. swap fee
    complete: bool                   # False: allow_partial stopped at the limit
    exact_block: bool = True         # False: evaluated on a later stride snapshot (stride replays only); excluded from
                                     # failure-burst counters and reported separately (section 3.12)
    dest_key: SubnetKey | None = None
    dest_hotkey: Hotkey | None = None
    dest_shares: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderRecord:
    intent: OrderIntent
    state: OrderState = OrderState.INTENDED
    fill_ids: tuple[str, ...] = ()

    def to(self, new: OrderState) -> OrderRecord:
        if new not in _NEXT.get(self.state, frozenset()):
            raise IllegalTransition(f"{self.intent.order_id}: {self.state.value} -> {new.value}")
        return replace(self, state=new)


class IllegalTransition(Exception):
    pass


class Resolution(StrEnum):
    NOT_PLACED = "NOT_PLACED"              # provably never on chain (era expired, nonce unconsumed): terminal fail, may re-decide
    PLACED = "PLACED"                      # in flight, awaiting finalized N+2 (sim: re-derived VenueAck attached)
    LANDED = "LANDED"                      # outcome final (fill, inner failure, or shield miss at N+2); facts attached
    UNRESOLVABLE_YET = "UNRESOLVABLE_YET"  # ask again next block; NOTHING is re-sent meanwhile


@dataclass(frozen=True, slots=True)
class VenueCaps:
    kind: str                        # "sim" | "paper" | "live_dry" | "live"
    shield_latency_blocks: int       # 2 (N+2)
    era_blocks: int                  # 8 for shielded carriers
    supports_rotation: bool          # MOVE_STAKE_LIMIT allowed (False in v1)
    max_inflight_per_netuid: int     # 1
    n_delegates: int                 # funded Staking-proxy delegates (3); sim/paper model the same count (ExecCfg.n_delegates)
