"""taotrader/portfolio/planner.py - StandardPlanner: RiskDecision -> ordered OrderIntents (WP8; DESIGN.md 3.12, 3.9).

MONEY MODULE: integer rao / alpha rao and Decimal (core.fixed.DEC) only; no float, float() or true division (static
gate). Pure: book history comes only from ctx.book_view; pools, spots and values from ctx.view.

Per generation (sorted), at most ONE order, and none while any order on the same netuid is open (`inflight` plus the
non-terminal orders of the BookView); no order on a dissolved generation or a pool that is gone.

- Forced exits (TargetBook.forced; highest urgency per key, then the widest slippage budget):
  * EMERGENCY / URGENT: REMOVE_STAKE_LIMIT of the whole position (full_position = True; sim sells all shares, live
    the exact alpha read at the submit head), allow_partial = True, limit = floor(spot * (1 - s)^(1/w_quote)), s the
    exit's budget (S_URGENT, s_emerg). Re-quoted after each terminal outcome, at most once per L_exec blocks (an order
    the Engine CANCELLED before submission has no outcome and does not count), widening s x1.5 per unsuccessful attempt
    up to max(s, s_emerg(R)) (R from protocol.prune.recovery_ratio with RiskCfg's R_DEFAULT). Never tranched. FROZEN:
    only EMERGENCY exits when allow_emergency_exits_when_frozen, fill-or-kill at s <= 5% (the amount is cut to
    max_sell_to_limit, so the order can fill). Shield fallback: when the caller passes a
    finality lag above finality_shield_pause_blocks, urgent exits go unshielded (era 16), same limit.
  * NORMAL / HIGH (burn, launch stop, trims): a normal sell to the exit's trim_to (0 = full), ignoring the band.
- Hotkey switch: MOVE_STAKE of the whole position when the target's (router) hotkey differs from the held one and the
  target is positive; no limit, NORMAL priority; it takes the netuid's slot for this tick.
- Normal sell (REMOVE_STAKE_LIMIT, allow_partial = False): Delta = current executable value - target, skipped inside the
  no-trade band max(V_MIN, 20% of target); full exit when the target is 0 or below REMAINDER_MIN, or when the remainder
  would be nominator dust; a partial sell must output >= 0.002 TAO; alpha = position alpha * Delta / value.
  limit = floor(marginal_after_sell(pool, alpha) * (1 - beta_exit)) < spot, beta_exit = clamp(q99, 0.25%, 5%), and
  alpha <= max_sell_to_limit(limit) by construction. After a FAILED sell on the key the retry uses allow_partial with
  the same limit rule (the SlippageTooHigh fallback; the BookView carries no failure reason). NORMAL sells tranche only
  with chain-buy refill (SubnetExcessTao > 0): V_tr = T * 1%/(1 - 1%), spaced clamp(ceil(V_tr / SubnetExcessTao), 10,
  600) blocks; and they are deferred when 0 < next_drain - (b + finality_lag + latency) <= 30 blocks.
- Buy (ADD_STAKE_LIMIT, allow_partial = False), only in NORMAL mode with entries not halted: Delta = target - current,
  band as above; benefit alpha_h * Delta >= Delta * (Delta/T + f) + buy fee when the allocator supplied alpha_h
  ("alpha_h_ppm=" reason); tao_in = min(Delta, cash - MIN_FREE_REAL - TAO of open buys - earlier buys this tick); limit
  = ceil(marginal_after_buy(pool, tao_in) * (1 + beta_entry)) > spot, beta_entry = clamp(q95, 0.1%, 2%); tao_in <=
  max_buy_to_limit(limit). Chase: an open entry episode with >= K_CHASE re-quotes, or a drift >= CHI_MAX from its
  decision spot, abandons the entry.
- Priority: EMERGENCY > URGENT > HIGH > NORMAL sells (and moves) > NORMAL buys; within a tier by expected-loss rate
  (1 - R) * V for forced exits, then key. At most len(BookView.delegates_free) intents per tick (one carrier per free
  delegate).
- Validity: shielded valid_until = b + finality_lag + latency; unshielded = b + finality_lag + 16. Attempt = the number
  of consecutive unsuccessful orders of the same side on the key; ids via core.orders.make_order_id.
- Long-only: sells never exceed the held position.
- SafeMode (no staking call is whitelisted; 9.8 #15): no order whose earliest inclusion block falls at or before
  SafeMode.EnteredUntil is emitted, whatever the mode; forced exits stay precomputed in the TargetBook.
- Buy-only caps (live and live-dry books, section 9.8 #12, enforced again by the LiveVenue): LiveCfg max_order_tao,
  max_daily_turnover_tao (buy TAO intended over 7,200 blocks), max_position_tao (executable value after the buy) and
  allowed_netuids bound BUYS only. Sells and moves are never capped, and a risk exit is never refused for a cap.
  A live or live-dry planner built without a LiveCfg uses the LiveCfg defaults (fail closed).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from ..core.config import BookCfg, LiveCfg
from ..core.fixed import DEC, ONE, floor_int
from ..core.orders import (
    TERMINAL,
    Attribution,
    OrderIntent,
    OrderKind,
    OrderState,
    Urgency,
    make_order_id,
)
from ..core.protocols import BookView, TickContext
from ..core.signals import ForcedExit, RiskDecision, TargetPosition
from ..core.state import PoolState, SubnetState
from ..core.units import (
    BLOCKS_PER_DAY,
    FEE_DEN,
    MIN_STAKE_RAO,
    PERQUINTILL,
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BookId,
    Hotkey,
    Mode,
    PriceRao,
    Rao,
    RunMode,
    SubnetKey,
)
from ..protocol.amm import (
    SwapError,
    marginal_after_buy,
    marginal_after_sell,
    max_buy_to_limit,
    max_sell_to_limit,
    quote_buy,
    quote_sell,
    spot_rao_exact,
    v_max,
)
from ..protocol.fees import meets_min_stake, nominator_dust
from ..protocol.prune import recovery_ratio
from ..risk.liquidity import Holding, holding_attribution, holdings
from ..risk.prune_guard import s_emerg_ppm

__all__ = [
    "DRAIN_DEFER_BLOCKS",
    "FROZEN_FOK_MAX_PPM",
    "TRANCHE_FRAC_PPM",
    "UNSHIELDED_ERA_BLOCKS",
    "StandardPlanner",
    "next_drain_block",
    "urgent_limit",
]

FROZEN_FOK_MAX_PPM: Final[int] = 50_000          # FROZEN exception: fill-or-kill at s <= 5%
DRAIN_DEFER_BLOCKS: Final[int] = 30
TRANCHE_FRAC_PPM: Final[int] = 10_000            # V_tr = T * 1% / (1 - 1%)
TRANCHE_SPACING_MIN: Final[int] = 10
TRANCHE_SPACING_MAX: Final[int] = 600
UNSHIELDED_ERA_BLOCKS: Final[int] = 16
WIDEN_NUM: Final[int] = 3                        # x1.5 per unsuccessful urgent attempt
WIDEN_DEN: Final[int] = 2
ALPHA_H_REASON: Final[str] = "alpha_h_ppm="
_SELL_KINDS: Final[tuple[OrderKind, ...]] = (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT)
_TIER_BUY: Final[int] = 4


def urgent_limit(pool: PoolState, s_ppm: int) -> int:
    """floor(spot * (1 - s)^(1/w_quote)) in rao per alpha (0 when the pool is empty)."""
    if pool.px_tao <= 0 or pool.px_alpha <= 0 or pool.w_quote_e18 <= 0:
        return 0
    keep = DEC.divide(Decimal(PPM - max(0, min(int(s_ppm), PPM))), Decimal(PPM))
    expo = DEC.divide(Decimal(PERQUINTILL), Decimal(pool.w_quote_e18))
    return floor_int(DEC.multiply(spot_rao_exact(pool), DEC.power(keep, expo)))


def _tao_to_rao(tao: float) -> Rao:
    """A TAO amount from configuration (LiveCfg floats) to rao, exactly from its shortest decimal text, floored."""
    return Rao(max(0, floor_int(DEC.multiply(Decimal(repr(tao)), Decimal(RAO_PER_TAO)))))


def next_drain_block(s: SubnetState, block: int) -> int | None:
    """The first epoch drain after `block` (LastEpochBlock advances by Tempo per epoch)."""
    tempo = int(s.tempo)
    if tempo <= 0:
        return None
    last = int(s.last_epoch_block)
    if last > block:
        return last
    return last + ((block - last) // tempo + 1) * tempo


def _alpha_h(t: TargetPosition | None) -> int:
    if t is None:
        return 0
    for r in t.reasons:
        if r.startswith(ALPHA_H_REASON):
            text = r[len(ALPHA_H_REASON):]
            if text.isdigit():
                return int(text)
    return 0


@dataclass(frozen=True, slots=True)
class _Plan:
    tier: int
    loss: int                  # expected-loss rate proxy (higher first)
    key: SubnetKey
    intent: OrderIntent


class StandardPlanner:
    """The Planner of section 3.12 (pure; constructed per book with its RiskCfg/ExecCfg)."""

    def __init__(self, book: BookCfg, *, run_mode: RunMode = RunMode.BACKTEST, live: LiveCfg | None = None) -> None:
        self.book = book
        self.risk = book.risk
        self.exec = book.exec
        self.run_mode = run_mode
        # Live and live-dry books always run with buy caps (section 9.8 #12): without an explicit LiveCfg the LiveCfg
        # defaults apply (fail closed), never "no cap".
        self.live = (live if live is not None else LiveCfg()) if run_mode in (RunMode.LIVE, RunMode.LIVE_DRY) else None
        lv = self.live
        self._max_order = _tao_to_rao(lv.max_order_tao) if lv is not None else None
        self._max_turnover = _tao_to_rao(lv.max_daily_turnover_tao) if lv is not None else None
        self._max_position = _tao_to_rao(lv.max_position_tao) if lv is not None else None

    def buy_cap(self, key: SubnetKey, current_value: int, ctx: TickContext, planned_today: int) -> int | None:
        """Live/live-dry BUY caps (section 9.8 #12; the LiveVenue enforces the same independently): None = no cap,
        else the largest tao_in allowed (0 = refused). max_order_tao per buy; max_daily_turnover_tao over the buys
        intended in the last 7,200 blocks (BookView.orders, intended TAO, so conservative) plus this tick's; a buy may
        not take the position's executable value above max_position_tao; allowed_netuids (when set) whitelists buys.
        Sells and moves are never capped (bounded only by the held position)."""
        if self.live is None:
            return None
        if self.live.allowed_netuids and int(key.netuid) not in self.live.allowed_netuids:
            return 0
        b = int(ctx.block)
        used = sum(int(o.intent.tao_in) for o in ctx.book_view.orders
                   if o.intent.kind is OrderKind.ADD_STAKE_LIMIT and int(o.intent.created_block) > b - BLOCKS_PER_DAY
                   and o.state not in (OrderState.CANCELLED, OrderState.EXPIRED))
        caps = [int(self._max_order or 0), int(self._max_turnover or 0) - used - planned_today,
                int(self._max_position or 0) - int(current_value)]
        return max(0, min(caps))

    # ------------------------------------------------------------------ book history
    @staticmethod
    def _orders_on(bv: BookView, key: SubnetKey, kinds: Sequence[OrderKind]) -> list[tuple[int, OrderState, str]]:
        """(created_block, state, order_id) of the key's orders of `kinds`, newest first."""
        rows = [(int(o.intent.created_block), o.state, str(o.intent.order_id), int(o.intent.attempt))
                for o in bv.orders if o.intent.key == key and o.intent.kind in kinds]
        rows.sort(key=lambda r: (-r[0], -r[3], r[2]))
        return [(r[0], r[1], r[2]) for r in rows]

    def _unsuccessful(self, bv: BookView, key: SubnetKey, kinds: Sequence[OrderKind]) -> int:
        """Consecutive terminal orders of `kinds` on `key` that did not complete (newest first): FAILED, EXPIRED, or
        FILLED with an incomplete fill (allow_partial stopped at the limit). CANCELLED orders were never submitted (no
        terminal outcome): they are skipped, so they neither widen an urgent limit nor break the run."""
        incomplete = {f.order_id for f in bv.recent_fills if not f.complete}
        n = 0
        for _, state, oid in self._orders_on(bv, key, kinds):
            if state not in TERMINAL:
                break
            if state is OrderState.CANCELLED:
                continue
            if state is OrderState.FILLED and oid not in incomplete:
                break
            n += 1
        return n

    def _last_failed(self, bv: BookView, key: SubnetKey, kinds: Sequence[OrderKind]) -> bool:
        """The key's newest submitted order of `kinds` FAILED (never-submitted CANCELLED orders are skipped)."""
        rows = [r for r in self._orders_on(bv, key, kinds) if r[1] is not OrderState.CANCELLED]
        return bool(rows) and rows[0][1] is OrderState.FAILED

    @staticmethod
    def _last_created(bv: BookView, key: SubnetKey, kinds: Sequence[OrderKind] | None = None) -> int | None:
        """Creation block of the key's newest order of `kinds` that may have reached the chain. CANCELLED orders were
        never submitted (the Engine cancels INTENDED orders only), so they have no outcome to wait for: they neither
        rate-limit a risk-exit re-quote (L_exec) nor space tranches."""
        blocks = [int(o.intent.created_block) for o in bv.orders
                  if o.intent.key == key and o.state is not OrderState.CANCELLED
                  and (kinds is None or o.intent.kind in kinds)]
        return max(blocks) if blocks else None

    def _lands_in_safe_mode(self, ctx: TickContext, intent: OrderIntent) -> bool:
        """SafeMode whitelists no staking call (brief: no exits are possible while it is active; DESIGN 9.8 #15): an
        order whose earliest inclusion block (b + finality_lag + latency shielded, + 1 unshielded) is at or before
        SafeMode.EnteredUntil would fail on chain and pay its fee. Such orders are not emitted; the overlay keeps the
        forced exits precomputed, so they go out once their inclusion falls after EnteredUntil."""
        until = ctx.raw.glob.safe_mode_until
        if until is None:
            return False
        step = int(self.exec.latency_blocks) if intent.shielded else 1
        return int(until) >= int(ctx.block) + int(self.exec.finality_lag_blocks) + step

    # ------------------------------------------------------------------ the planner
    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey], run_id: str,
                 book: BookId, *, finality_lag_blocks: int | None = None) -> tuple[OrderIntent, ...]:
        b = int(ctx.block)
        bv = ctx.book_view
        mode = max(ctx.mode, decision.mode)
        tb = decision.targets
        targets = {t.key: t for t in tb.items}
        forced: dict[SubnetKey, ForcedExit] = {}
        for x in tb.forced:
            old = forced.get(x.key)
            if old is None or (int(x.urgency), int(x.exit_slip_ppm)) > (int(old.urgency), int(old.exit_slip_ppm)):
                forced[x.key] = x
        hold = holdings(ctx)
        busy = {int(k.netuid) for k in inflight} | {int(o.intent.key.netuid) for o in bv.orders if o.state not in TERMINAL}
        halted = tb.halt_entries or (bv.entries_halted_until is not None and int(bv.entries_halted_until) >= b)
        entries_ok = mode is Mode.NORMAL and not halted
        cash = int(ctx.portfolio.cash) - int(self.risk.min_free_real_rao)
        cash -= sum(int(o.intent.tao_in) for o in bv.orders
                    if o.state not in TERMINAL and o.intent.kind is OrderKind.ADD_STAKE_LIMIT)
        plans: list[_Plan] = []
        buys: list[tuple[SubnetKey, SubnetState, Holding | None, TargetPosition]] = []
        for key in sorted(set(targets) | set(forced) | set(hold)):
            if int(key.netuid) in busy or key in bv.dissolving:
                continue
            s = ctx.view.get(key)
            if s is None:
                continue
            h = hold.get(key)
            t = targets.get(key)
            fe = forced.get(key)
            if fe is not None:
                if h is not None:
                    p = self._forced(key, s, h, fe, ctx, mode, run_id, book, finality_lag_blocks)
                    if p is not None:
                        plans.append(p)
                continue
            if mode >= Mode.EXITS_ONLY:
                continue
            if h is not None:
                if t is None:
                    continue                                   # no target and no forced exit: hold (exits are explicit)
                tgt = int(t.value_rao)
                if tgt > 0 and t.hotkey != h.hotkey:
                    p = self._move(key, s, h, t, ctx, run_id, book)
                elif tgt < int(h.value) or tgt <= 0:
                    p = self._sell(key, s, h, tgt, t.urgency, t.attribution, "sell", False, ctx, run_id, book)
                else:
                    p = None
                    if entries_ok and tgt > int(h.value):
                        buys.append((key, s, h, t))
                if p is not None:
                    plans.append(p)
            elif t is not None and entries_ok and int(t.value_rao) > 0:
                buys.append((key, s, None, t))
        bought = 0
        for key, s, h, t in sorted(buys, key=lambda x: (-int(x[3].urgency), x[0])):
            res = self._buy(key, s, h, t, cash, ctx, run_id, book, bought)
            if res is not None:
                plans.append(res)
                cash -= int(res.intent.tao_in)
                bought += int(res.intent.tao_in)
        plans = [p for p in plans if not self._lands_in_safe_mode(ctx, p.intent)]
        plans.sort(key=lambda p: (p.tier, -p.loss, p.key))
        free = len(bv.delegates_free)
        return tuple(p.intent for p in plans[:free])

    # ------------------------------------------------------------------ order builders
    def _tier(self, urgency: Urgency, buy: bool) -> int:
        if urgency is Urgency.EMERGENCY:
            return 0
        if urgency is Urgency.URGENT:
            return 1
        if urgency is Urgency.HIGH:
            return 2
        return _TIER_BUY if buy else 3

    def _valid_until(self, b: int, shielded: bool) -> Block:
        lag = int(self.exec.finality_lag_blocks)
        return Block(b + lag + (int(self.exec.latency_blocks) if shielded else UNSHIELDED_ERA_BLOCKS))

    @staticmethod
    def _intent(ctx: TickContext, run_id: str, book: BookId, key: SubnetKey, hotkey: Hotkey, kind: OrderKind,
                attempt: int, *, tao_in: Rao, alpha_in: AlphaRao, full_position: bool, limit_price: PriceRao,
                allow_partial: bool, shielded: bool, valid_until: Block, expected_out: int, urgency: Urgency,
                attribution: Attribution, reason: str, dest_hotkey: Hotkey | None = None) -> OrderIntent:
        return OrderIntent(order_id=make_order_id(run_id, book, ctx.block, key, hotkey, kind, attempt), attempt=attempt,
                           book=book, created_block=ctx.block, kind=kind, key=key, hotkey=hotkey, tao_in=tao_in,
                           alpha_in=alpha_in, full_position=full_position, limit_price=limit_price,
                           allow_partial=allow_partial, shielded=shielded, valid_until=valid_until,
                           expected_out=expected_out, urgency=urgency, attribution=attribution, reason=reason,
                           dest_hotkey=dest_hotkey)

    def _recovery_loss(self, s: SubnetState, value: int, ctx: TickContext) -> tuple[int, Decimal]:
        r_default = DEC.divide(Decimal(int(self.risk.r_default_ppm)), Decimal(PPM))
        raw_s = ctx.raw.get(s.key) or s
        r = recovery_ratio(raw_s, ctx.raw.glob, r_default)
        keep = DEC.subtract(ONE, min(max(r, Decimal(0)), ONE))
        return floor_int(DEC.multiply(Decimal(value), keep)), r

    def _forced(self, key: SubnetKey, s: SubnetState, h: Holding, fe: ForcedExit, ctx: TickContext, mode: Mode,
                run_id: str, book: BookId, lag: int | None) -> _Plan | None:
        risk = self.risk
        if fe.urgency < Urgency.URGENT:
            if mode is Mode.FROZEN:
                return None
            tgt = int(fe.trim_to_rao) if fe.trim_to_rao is not None else 0
            return self._sell(key, s, h, tgt, fe.urgency, holding_attribution(ctx.portfolio, key), f"forced:{fe.rule}", True,
                              ctx, run_id, book)
        b = int(ctx.block)
        bv = ctx.book_view
        last = self._last_created(bv, key)
        if last is not None and b - last < int(risk.unwind_exec_blocks):
            return None
        pool = s.pool
        alpha = int(h.alpha)
        if alpha <= 0 or pool.px_tao <= 0 or pool.px_alpha <= 0:
            return None
        loss, r = self._recovery_loss(s, int(h.value), ctx)
        slip = int(fe.exit_slip_ppm)
        cap = slip if fe.urgency is Urgency.EMERGENCY else max(slip, int(s_emerg_ppm(r)))
        n = self._unsuccessful(bv, key, _SELL_KINDS)
        for _ in range(n):
            slip = min(cap, slip * WIDEN_NUM // WIDEN_DEN)
        allow_partial = True
        full = True
        if mode is Mode.FROZEN:
            if not (fe.urgency is Urgency.EMERGENCY and risk.allow_emergency_exits_when_frozen):
                return None
            slip = min(slip, FROZEN_FOK_MAX_PPM)
            allow_partial = False
        limit = urgent_limit(pool, slip)
        spot = spot_rao_exact(pool)
        if limit <= 0 or Decimal(limit) >= spot:
            return None
        if not allow_partial:
            try:
                room = int(max_sell_to_limit(pool, PriceRao(limit)))
            except SwapError:
                return None
            if alpha > room:
                alpha, full = room, False
                if alpha <= 0:
                    return None
        shielded = not (lag is not None and lag > int(risk.finality_shield_pause_blocks))
        try:
            expected = int(quote_sell(pool, AlphaRao(alpha), partial_remaining=not full).amount_out)
        except SwapError:
            if not full:
                return None
            expected = 0
        intent = self._intent(ctx, run_id, book, key, h.hotkey, OrderKind.REMOVE_STAKE_LIMIT, n,
                              tao_in=Rao(0), alpha_in=AlphaRao(alpha), full_position=full, limit_price=PriceRao(limit),
                              allow_partial=allow_partial, shielded=shielded, valid_until=self._valid_until(b, shielded),
                              expected_out=expected, urgency=fe.urgency,
                              attribution=holding_attribution(ctx.portfolio, key), reason=f"forced:{fe.rule}")
        return _Plan(self._tier(fe.urgency, False), loss, key, intent)

    def _move(self, key: SubnetKey, s: SubnetState, h: Holding, t: TargetPosition, ctx: TickContext, run_id: str,
              book: BookId) -> _Plan | None:
        alpha = int(h.alpha)
        if alpha <= 0 or s.pool.px_tao <= 0 or s.pool.px_alpha <= 0 or s.hotkey(t.hotkey) is None:
            return None                                            # the destination must be tracked (its index)
        if floor_int(DEC.multiply(Decimal(alpha), s.pool.spot())) < MIN_STAKE_RAO:
            return None
        attempt = self._unsuccessful(ctx.book_view, key, (OrderKind.MOVE_STAKE,))
        intent = self._intent(ctx, run_id, book, key, h.hotkey, OrderKind.MOVE_STAKE, attempt,
                              tao_in=Rao(0), alpha_in=AlphaRao(alpha), full_position=True, limit_price=PriceRao(0),
                              allow_partial=False, shielded=True, valid_until=self._valid_until(int(ctx.block), True),
                              expected_out=alpha, urgency=Urgency.NORMAL, attribution=holding_attribution(ctx.portfolio, key),
                              reason="move", dest_hotkey=t.hotkey)
        return _Plan(3, 0, key, intent)

    @staticmethod
    def _leaves_dust(pool: PoolState, held: int, alpha: int, ctx: TickContext) -> bool:
        """The remainder after selling `alpha` would be a nominator position below NominatorMinRequiredStake at the
        POST-trade spot (the chain then force-sells it with no limit; never leave one, section 3.9)."""
        rest = held - alpha
        if rest <= 0:
            return False
        try:
            q = quote_sell(pool, AlphaRao(alpha), partial_remaining=True)
        except SwapError:
            return nominator_dust(rest, pool, ctx.view.glob)
        return nominator_dust(rest, pool.shifted(q.d_tao, q.d_alpha), ctx.view.glob)

    def _sell(self, key: SubnetKey, s: SubnetState, h: Holding, tgt: int, urgency: Urgency,
              attribution: Attribution | None, reason: str, forced: bool, ctx: TickContext, run_id: str,
              book: BookId) -> _Plan | None:
        risk, ex = self.risk, self.exec
        b = int(ctx.block)
        bv = ctx.book_view
        pool = s.pool
        cur = int(h.value)
        held = int(h.alpha)
        if held <= 0 or pool.px_tao <= 0 or pool.px_alpha <= 0:
            return None
        tgt = max(int(tgt), 0)
        delta = cur - tgt
        if tgt > 0 and delta <= 0:
            return None
        v_min = int(risk.v_min_rao)
        full = tgt <= 0 or tgt < max(int(risk.remainder_min_rao), v_min) or cur <= 0
        band = max(v_min, int(risk.band_ppm) * tgt // PPM)
        if not full and not forced and delta < band:
            return None
        alpha = held if full else held * delta // cur
        if not full and (alpha <= 0 or self._leaves_dust(pool, held, alpha, ctx)):
            full, alpha = True, held
        remainder_min = max(int(risk.remainder_min_rao), v_min)
        if urgency <= Urgency.NORMAL and s.excess_tao > 0:          # tranche only with chain-buy refill
            v_tr = int(v_max(pool.tao, TRANCHE_FRAC_PPM))
            selling = cur if full else delta
            if 0 < v_tr < selling and cur - v_tr >= remainder_min:
                spacing = min(TRANCHE_SPACING_MAX, max(TRANCHE_SPACING_MIN, -(-v_tr // int(s.excess_tao))))
                last = self._last_created(bv, key, _SELL_KINDS)
                if last is not None and b - last < spacing:
                    return None
                t_alpha = held * v_tr // max(cur, 1)
                if 0 < t_alpha < held and not self._leaves_dust(pool, held, t_alpha, ctx):
                    alpha, full = t_alpha, False
        if urgency <= Urgency.NORMAL:                              # drain timing
            nd = next_drain_block(s, b)
            lat = int(ex.finality_lag_blocks) + int(ex.latency_blocks)
            if nd is not None and 0 < nd - (b + lat) <= DRAIN_DEFER_BLOCKS:
                return None
        try:
            q = quote_sell(pool, AlphaRao(alpha), partial_remaining=not full)
        except SwapError:
            return None
        feat = ctx.frame.feats.get(key)
        beta = int(risk.beta_exit_cap_ppm) if feat is None else max(int(risk.beta_exit_floor_ppm),
                                                                       min(int(risk.beta_exit_cap_ppm), int(feat.beta_exit_ppm)))
        m = int(marginal_after_sell(pool, AlphaRao(alpha)))
        limit = m * (PPM - beta) // PPM
        if limit <= 0 or Decimal(limit) >= spot_rao_exact(pool):
            return None
        try:
            room = int(max_sell_to_limit(pool, PriceRao(limit)))
        except SwapError:
            return None
        if alpha > room:
            return None                                            # cannot happen: limit < marginal_after_sell(alpha)
        allow_partial = self._last_failed(bv, key, _SELL_KINDS)
        attempt = self._unsuccessful(bv, key, _SELL_KINDS)
        attr = attribution if attribution is not None else holding_attribution(ctx.portfolio, key)
        intent = self._intent(ctx, run_id, book, key, h.hotkey, OrderKind.REMOVE_STAKE_LIMIT, attempt,
                              tao_in=Rao(0), alpha_in=AlphaRao(alpha), full_position=full, limit_price=PriceRao(limit),
                              allow_partial=allow_partial, shielded=True, valid_until=self._valid_until(b, True),
                              expected_out=int(q.amount_out), urgency=urgency, attribution=attr, reason=reason)
        return _Plan(self._tier(urgency, False), 0, key, intent)

    def _buy(self, key: SubnetKey, s: SubnetState, h: Holding | None, t: TargetPosition, cash: int, ctx: TickContext,
             run_id: str, book: BookId, planned_today: int = 0) -> _Plan | None:
        risk, ex = self.risk, self.exec
        pool = s.pool
        if not s.subtoken_enabled or pool.px_tao <= 0 or pool.px_alpha <= 0:
            return None
        if h is not None and h.hotkey != t.hotkey:
            return None                                            # the move goes first
        if s.hotkey(t.hotkey) is None:
            return None                                            # untracked staking target: no index, no entry
        cur = int(h.value) if h is not None else 0
        tgt = int(t.value_rao)
        delta = tgt - cur
        v_min = int(risk.v_min_rao)
        if delta < max(v_min, int(risk.band_ppm) * tgt // PPM):
            return None
        bv = ctx.book_view
        spot = spot_rao_exact(pool)
        for k, requotes, dspot in bv.chase:
            if k != key:
                continue
            if requotes >= risk.k_chase:
                return None
            if int(dspot) > 0:
                drift = abs(DEC.subtract(spot, Decimal(int(dspot))))
                if DEC.multiply(drift, Decimal(PPM)) >= DEC.multiply(Decimal(int(risk.chi_max_ppm)), Decimal(int(dspot))):
                    return None
        tao_in = min(delta, cash)
        cap = self.buy_cap(key, cur, ctx, planned_today)
        if cap is not None:
            tao_in = min(tao_in, cap)
        if tao_in < v_min or not meets_min_stake(tao_in, pool.fee_rate):
            return None
        a_h = _alpha_h(t)
        if a_h > 0:
            y = int(pool.px_tao)
            lhs = a_h * FEE_DEN * y * tao_in
            rhs = tao_in * tao_in * PPM * FEE_DEN + tao_in * int(pool.fee_rate) * PPM * y + int(ex.buy_tx_fee_rao) * PPM * FEE_DEN * y
            if lhs < rhs:
                return None
        feat = ctx.frame.feats.get(key)
        beta = int(risk.beta_entry_cap_ppm) if feat is None else max(int(risk.beta_entry_floor_ppm),
                                                                        min(int(risk.beta_entry_cap_ppm), int(feat.beta_entry_ppm)))
        m = int(marginal_after_buy(pool, Rao(tao_in)))
        limit = -((-m * (PPM + beta)) // PPM)
        if Decimal(limit) <= spot:
            limit = floor_int(spot) + 1
        try:
            room = int(max_buy_to_limit(pool, PriceRao(limit)))
        except SwapError:
            return None
        if tao_in > room:
            return None                                            # cannot happen: limit > marginal_after_buy(tao_in)
        try:
            q = quote_buy(pool, Rao(tao_in))
        except SwapError:
            return None
        attempt = self._unsuccessful(bv, key, (OrderKind.ADD_STAKE_LIMIT,))
        intent = self._intent(ctx, run_id, book, key, t.hotkey, OrderKind.ADD_STAKE_LIMIT, attempt,
                              tao_in=Rao(tao_in), alpha_in=AlphaRao(0), full_position=False, limit_price=PriceRao(limit),
                              allow_partial=False, shielded=True, valid_until=self._valid_until(int(ctx.block), True),
                              expected_out=int(q.amount_out), urgency=t.urgency, attribution=t.attribution, reason="buy")
        return _Plan(self._tier(t.urgency, True), 0, key, intent)


if TYPE_CHECKING:
    from ..core.protocols import Planner

    def _conforms(p: StandardPlanner) -> Planner:          # mypy: structural conformance to core.protocols.Planner
        return p
