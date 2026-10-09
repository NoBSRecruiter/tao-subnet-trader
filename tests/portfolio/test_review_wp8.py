"""Adversarial-review regression tests for portfolio/planner.py (WP8). Each test failed before its fix."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

from taotrader.core.config import LiveCfg
from taotrader.core.orders import OrderKind, OrderState, Urgency
from taotrader.core.signals import ForcedExit, RiskDecision, TargetBook
from taotrader.core.units import Block, Mode, Ppm, Rao, RunMode
from taotrader.portfolio.planner import StandardPlanner

B = 9_240_388
TAO = 10**9


def _held(kit: ModuleType, netuid: int, value_tao: int):
    alpha = int(Decimal(value_tao * TAO) / (Decimal("0.002") * netuid))
    return kit.portfolio(positions=[kit.position(kit.key(netuid), kit.vhk(netuid), alpha)])


def _plan(kit: ModuleType, items=(), forced=(), *, pf=None, snap=None, bv=None, mode: Mode = Mode.NORMAL,
          run_mode: RunMode = RunMode.BACKTEST, live: LiveCfg | None = None):
    snap = snap if snap is not None else kit.snapshot()
    t = kit.tick(snap, portfolio=pf, bv=bv, mode=mode)
    tb = TargetBook(asof=Block(B), items=tuple(sorted(items, key=lambda x: x.key)),
                    forced=tuple(sorted(forced, key=lambda f: f.key)))
    planner = StandardPlanner(kit.book_cfg(), run_mode=run_mode, live=live)
    return planner(RiskDecision(tb, (), mode), t, frozenset(), kit.RUN, kit.BOOK)


def test_a_cancelled_order_does_not_delay_the_emergency_exit(kit: ModuleType) -> None:
    """Section 3.3 / 3.12: risk exits re-quote at each TERMINAL OUTCOME, at most once per L_exec blocks, and U = 15
    blocks budgets exactly three attempts. An order the Engine cancelled before it was ever submitted (reserve() found
    no free delegate, TTL) has no chain outcome to wait for. The L_exec throttle counted it, so the EMERGENCY exit
    re-decision waited up to 4 more blocks in per-block paper/live (the Engine frees the netuid in the same tick,
    review:WP7)."""
    k = kit.key(8)
    pf = _held(kit, 8, 100)
    fe = ForcedExit(k, Urgency.EMERGENCY, "prune_A", Ppm(200_000))
    cancelled = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.CANCELLED, created=B - 1, full=True,
                          urgency=Urgency.EMERGENCY)
    out = _plan(kit, (), [fe], pf=pf, bv=kit.book_view(orders=(cancelled,)))
    assert len(out) == 1 and out[0].urgency is Urgency.EMERGENCY and out[0].full_position
    # an order that did reach the chain still rate-limits the re-quote (L_exec = 5 blocks)
    failed = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FAILED, created=B - 4, full=True)
    assert _plan(kit, (), [fe], pf=pf, bv=kit.book_view(orders=(failed,))) == ()
    # a cancelled (never executed) tranche does not hold back the next tranche either
    refill = kit.snapshot(subnets=[kit.subnet(8, excess_tao=Rao(10_000_000))])
    tranche = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.CANCELLED, created=B - 1)
    out2 = _plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=_held(kit, 8, 60), snap=refill,
                 bv=kit.book_view(orders=(tranche,)))
    assert len(out2) == 1 and out2[0].kind is OrderKind.REMOVE_STAKE_LIMIT


def test_a_cancelled_order_does_not_widen_the_urgent_limit(kit: ModuleType) -> None:
    """Section 3.12: an urgent exit widens s x1.5 per re-quote after a terminal OUTCOME (fill remainder, failure or
    miss). A cancelled, never-submitted order is no such outcome, but it counted as one: every Engine cancellation
    loosened the next limit by x1.5 (up to s_emerg) although the market gave no evidence for it."""
    from taotrader.portfolio.planner import urgent_limit

    k = kit.key(8)
    pf = _held(kit, 8, 100)
    fe = ForcedExit(k, Urgency.URGENT, "prune_backstop", Ppm(30_000))
    failed = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.FAILED, created=B - 20, full=True)
    cancelled = kit.order(k, kit.vhk(8), OrderKind.REMOVE_STAKE_LIMIT, OrderState.CANCELLED, created=B - 10, full=True)
    snap = kit.snapshot()
    sk = snap.get(k)
    assert sk is not None
    pool = sk.pool
    out = _plan(kit, (), [fe], pf=pf, snap=snap, bv=kit.book_view(orders=(failed, cancelled)))
    assert len(out) == 1 and out[0].limit_price == urgent_limit(pool, 45_000)       # one real failure: 3% x 1.5
    only_cancelled = _plan(kit, (), [fe], pf=pf, snap=snap, bv=kit.book_view(orders=(cancelled,)))
    assert only_cancelled[0].limit_price == urgent_limit(pool, 30_000)
    # the SlippageTooHigh fallback (allow_partial after a FAILED sell) also looks past a never-submitted order
    sell = _plan(kit, [kit.target(k, kit.vhk(8), 0)], pf=pf, snap=snap, bv=kit.book_view(orders=(failed, cancelled)))
    assert len(sell) == 1 and sell[0].allow_partial and sell[0].attempt == 1


def test_no_order_lands_inside_safe_mode(kit: ModuleType) -> None:
    """Section 9.8 #15 / 3.11 and brief 4.x: SafeMode whitelists no staking call, so no exit is possible while it is
    active; an order landing inside it fails on chain and pays its tx fee (the sim charges it too). The planner applied
    the FROZEN emergency exception to SafeMode as well and emitted a fill-or-kill EMERGENCY sell every L_exec blocks.
    The exit stays precomputed (TargetBook.forced) and goes out once its inclusion block is past EnteredUntil."""
    k = kit.key(8)
    pf = _held(kit, 8, 100)
    fe = ForcedExit(k, Urgency.EMERGENCY, "prune_target", Ppm(250_000))
    inside = kit.snapshot(safe_mode_until=Block(B + 1_000))
    assert _plan(kit, (), [fe], pf=pf, snap=inside, mode=Mode.FROZEN) == ()
    assert _plan(kit, (), [fe], pf=pf, snap=inside, mode=Mode.NORMAL) == ()       # whatever the mode says
    # the planned inclusion block (b + finality_lag + latency = b + 5) is the first block after SafeMode: allowed
    edge = kit.snapshot(safe_mode_until=Block(B + 4))
    out = _plan(kit, (), [fe], pf=pf, snap=edge, mode=Mode.FROZEN)
    assert len(out) == 1 and out[0].valid_until == B + 5
    assert _plan(kit, (), [fe], pf=pf, snap=kit.snapshot(safe_mode_until=Block(B + 5)), mode=Mode.FROZEN) == ()


def test_live_books_without_a_live_config_keep_the_buy_caps(kit: ModuleType) -> None:
    """Section 9.8 #12: buy caps are enforced in two independent places, the planner and LiveVenue. A LIVE or LIVE_DRY
    planner built without a LiveCfg ran with no buy cap at all (fail-open); it now falls back to the LiveCfg defaults
    (1 TAO per buy, 5 TAO per day, 5 TAO per position)."""
    buy = kit.target(kit.key(8), kit.vhk(8), 50 * TAO)
    for run_mode in (RunMode.LIVE, RunMode.LIVE_DRY):
        out = _plan(kit, [buy], run_mode=run_mode)
        assert len(out) == 1 and out[0].tao_in == TAO, run_mode
    assert _plan(kit, [buy])[0].tao_in == 50 * TAO                                 # backtest/paper books: no live caps
