"""taotrader/engine/engine.py - Engine.decide: the pure per-book decision pipeline (WP7; DESIGN.md 4.4, 5.10, 5.13).

`Engine.decide(state, snap, prev, events, frame, view, health, *, inputs, store)` is a pure function of its arguments
(no clock, I/O, randomness or unsorted-set iteration). It returns the book's journal records for one tick, in phase
order, which the Runner commits in ONE batch together with the run-level inputs. Recovery re-runs it on the journaled
inputs and compares the canonical outputs byte for byte (ReplayDivergence otherwise).

Phases (core.units.Phase gives the LogicalTime ordering):
1. INGEST: the tick's journaled inputs (`inputs`: SnapshotObserved, ChainEventObserved, OperatorCommand,
   ConfigApplied, the book's CapitalChanged, ...) are folded into a working copy of the state with engine.reducer.
2. ACCOUNT: YieldAccrued per held position whose value at the snapshot's hotkey index differs from its ledger alpha
   (delta = floor(shares * index) - ledger); DeregSettled for DISSOLVING positions from the last good snapshot
   (`prev`) and the book's dereg model - backtest, paper and live-dry only, never in RunMode.LIVE (reconciliation
   journals the observed payout there). Both are folded into the working state before DECIDE.
3. DECIDE: base mode (operator, health, model drift, SafeMode) -> strategies that are due (absolute-block cadence
   buckets `block // every > last // every`, or a wake event; block >= valid_from_block; frame.warm) -> standing
   signals of the others -> router (ctx.book_view.router replaced by its result) -> caps -> allocator -> monotone
   overlay review -> operator flatten -> final mode (ModeChanged) -> cancellation of INTENDED (never submitted)
   orders the new decision invalidates (operator halt, pool gone, TTL, mode) -> planner, with those orders no longer
   counted as in flight (its intents are filtered by mode, entry halts, one order per netuid and duplicate ids).
4. EMIT: DecisionTrace (signals of the strategies that ran, overlay + engine actions, mode, memories incl. the
   "risk.router" memory when it changed, features and calibration digests, NAV_liq, sleeve NAVs), one SleeveTransfer
   per netting transfer, and OrderIntended per kept intent.

Fail-closed pipeline: an exception in a strategy skips that strategy (its standing signals remain); an exception in the
router keeps the previous router memory; an exception in caps or the allocator replaces the proposal by "hold every
position"; an exception in the overlay or the planner emits no intents and floors the mode at CAUTION. Each is
journaled as an "engine.*" action (exception type only, so replays reproduce it byte for byte).

Engine-owned mode floors (section 3.11 rows whose inputs the overlay cannot see, because HealthObs is not part of
TickContext/RiskContext): operator halt -> FROZEN (and no planner call at all); operator exits_only -> EXITS_ONLY;
model-drift alarm -> CAUTION; healthy endpoints 0 -> FROZEN, < 2 -> CAUTION; secs_since_block > stall_halt_s ->
EXITS_ONLY, > stall_warn_s -> CAUTION; finality lag > finality_caution_blocks -> CAUTION; SafeMode -> FROZEN. The
overlay receives that floor as ctx.mode and may only raise it. The FROZEN emergency exception
(RiskCfg.allow_emergency_exits_when_frozen: EMERGENCY forced sells only) applies only to a FROZEN raised by the overlay
(the key-alarm row of section 3.11); an Engine FROZEN floor (operator halt, SafeMode, no healthy head endpoint) admits
nothing, because risk exits are attempted only when the chain accepts staking calls (section 9.8 #15).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from typing import Any, Final

from ..core import codec
from ..core.config import BookCfg, SleeveCfg
from ..core.events import (
    ChainEvent,
    DecisionTrace,
    DeregSettled,
    HealthObs,
    JournalEvent,
    ModeChanged,
    OrderCancelled,
    OrderIntended,
    SleeveTransfer,
    YieldAccrued,
)
from ..core.fixed import DEC, floor_int
from ..core.orders import TERMINAL, OrderIntent, OrderKind, OrderState, Urgency
from ..core.portfolio import alpha_unit, pos_account
from ..core.protocols import (
    Allocator,
    CapsFn,
    Planner,
    RiskContext,
    RiskOverlay,
    RouterFn,
    SnapshotStore,
    Strategy,
    TickContext,
)
from ..core.signals import ForcedExit, RiskAction, RiskDecision, Signal, StrategyOutput, TargetBook, TargetPosition
from ..core.state import ChainSnapshot
from ..core.units import (
    PPM,
    AlphaRao,
    Block,
    BookId,
    LogicalTime,
    Mode,
    Phase,
    PositionKey,
    Ppm,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from ..core.views import FeatureFrame
from ..protocol.amm import liq_value
from ..protocol.calibration import Calibration, CalibrationProvider, check_asof, effective_recovery
from ..protocol.prune import recovery_ratio
from .reducer import (
    BOOK_SLEEVE,
    ENGINE_FORCED_EXIT,
    ENGINE_ORDER_SPOT,
    ROUTER_MEMORY_ID,
    BookSpec,
    EngineState,
    OrderEntry,
    book_view,
    holding_attribution,
    initial_state,
    ledger_dict,
    reduce,
    value_at,
)

__all__ = ["ENGINE_ERROR", "Engine", "Item", "parse_dereg_model"]

Item = tuple[LogicalTime, BookId, JournalEvent]

ENGINE_ERROR: Final[str] = "engine.error"
ENGINE_STRATEGY_ERROR: Final[str] = "engine.strategy_error"
ENGINE_DROP_INTENT: Final[str] = "engine.drop_intent"
ENGINE_CLAMP: Final[str] = "engine.monotone_clamp"
ENGINE_MODE_FLOOR: Final[str] = "engine.mode_floor"
_UNSHIELDED_INCLUSION_BLOCKS: Final[int] = 1        # an unshielded fallback lands at submit head + 1 (section 8.4)
_SELLS: Final[tuple[OrderKind, ...]] = (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT)
_EXACT: Final[Context] = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)   # exact share sums (add only)


def parse_dereg_model(model: str) -> tuple[str, Decimal | None]:
    """BookCfg.dereg_model -> (DeregSettled.model text, fixed recovery ratio or None for "formula").
    "formula" -> ("formula", None); "fixed:350000" (ppm of spot) -> ("fixed:0.35", Decimal("0.35"))."""
    if model == "formula":
        return "formula", None
    head, _, tail = model.partition(":")
    if head == "fixed" and tail.isdigit() and 0 <= int(tail) <= PPM:
        r = DEC.divide(Decimal(int(tail)), Decimal(PPM))
        text = format(r, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"fixed:{text}", r
    raise ValueError(f"unknown dereg model {model!r} (expected 'formula' or 'fixed:<ppm>')")


def _exc(e: BaseException) -> str:
    return type(e).__name__


class _Emitter:
    """Collects one book's records with LogicalTime (block, phase, sub); sub counts records within a phase."""

    def __init__(self, book: BookId, block: Block) -> None:
        self.book, self.block = book, block
        self.items: list[Item] = []
        self._sub: dict[Phase, int] = {}

    def add(self, phase: Phase, ev: JournalEvent) -> None:
        n = self._sub.get(phase, 0)
        self._sub[phase] = n + 1
        self.items.append((LogicalTime(self.block, phase, n), self.book, ev))


@dataclass(frozen=True, slots=True)
class _Strategies:
    """The book's strategies (one per sleeve), sorted by id."""
    by_id: tuple[tuple[StrategyId, Strategy], ...]

    def get(self, sid: StrategyId) -> Strategy | None:
        for k, s in self.by_id:
            if k == sid:
                return s
        return None


class Engine:
    """One book's pure decision pipeline. Holds configuration and the injected pure functions only; all state is
    passed in (EngineState) and comes back as journal records."""

    def __init__(self, *, run_id: str, cfg: BookCfg, mode: RunMode, strategies: Sequence[Strategy], router: RouterFn,
                 caps: CapsFn, allocator: Allocator, overlay: RiskOverlay, planner: Planner,
                 calibration: CalibrationProvider | None = None, delegates: Sequence[str] | None = None) -> None:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        by_id: dict[StrategyId, Strategy] = {}
        for s in strategies:
            if s.id in by_id:
                raise ValueError(f"book {cfg.book}: duplicate strategy {s.id}")
            by_id[s.id] = s
        for sl in cfg.sleeves:
            if sl.strategy not in by_id:
                raise ValueError(f"book {cfg.book}: sleeve {sl.strategy} has no strategy implementation")
        sleeve_set = {sl.strategy for sl in cfg.sleeves}
        self.run_id = run_id
        self.cfg = cfg
        self.mode = mode
        self.router = router
        self.caps = caps
        self.allocator = allocator
        self.overlay = overlay
        self.planner = planner
        self.calibration = calibration
        self.spec: BookSpec = BookSpec.from_cfg(cfg, mode, delegates)
        self._dereg_text, self._dereg_fixed = parse_dereg_model(cfg.dereg_model)
        self._strategies = _Strategies(tuple(sorted(((k, v) for k, v in by_id.items() if k in sleeve_set),
                                                    key=lambda kv: kv[0])))
        self._sleeves: tuple[SleeveCfg, ...] = tuple(sorted(cfg.sleeves, key=lambda s: s.strategy))
        self._stages: tuple[tuple[StrategyId, Stage], ...] = tuple((s.strategy, s.stage) for s in self._sleeves)
        top = max((s.stage for s in self._sleeves), default=Stage.RESEARCH)
        self._book_sleeve = SleeveCfg(strategy=BOOK_SLEEVE, stage=top, budget_ppm=Ppm(PPM))

    # ------------------------------------------------------------------ introspection
    @property
    def book(self) -> BookId:
        return self.cfg.book

    @property
    def strategies(self) -> tuple[Strategy, ...]:
        return tuple(s for _, s in self._strategies.by_id)

    def initial_state(self) -> EngineState:
        return initial_state(self.spec)

    # ------------------------------------------------------------------ the pipeline
    def decide(self, state: EngineState, snap: ChainSnapshot, prev: ChainSnapshot | None, events: Sequence[ChainEvent],
               frame: FeatureFrame, view: ChainSnapshot, health: HealthObs, *, inputs: Sequence[Item] = (),
               store: SnapshotStore) -> list[Item]:
        """The book's journal records for the tick at snap.block (ACCOUNT, DECIDE and EMIT phases). `inputs` are the
        records the Runner journals in the same batch before them (run-level, and this book's INGEST records); the
        tick's SnapshotObserved must be among them."""
        b = snap.block
        book = self.book
        if state.spec != self.spec:
            raise ValueError(f"book {book}: the state was built for a different BookSpec")
        ws = state
        for _, bk, ev in inputs:
            if bk in ("", book):
                ws = reduce(ws, ev)
        if ws.clock != b:
            raise ValueError(f"decide({b}): the tick's SnapshotObserved must be among the inputs (clock {ws.clock})")
        out = _Emitter(book, b)
        actions: list[RiskAction] = []
        cal: Calibration | None = None
        if self.calibration is not None:
            cal = self.calibration.asof(b)
            check_asof(cal, b)

        # ---- ACCOUNT
        for ev in self._account(ws, snap, prev, cal):
            out.add(Phase.ACCOUNT, ev)
            ws = reduce(ws, ev)

        # ---- DECIDE
        base_mode, floors = self._base_mode(ws, snap, health)
        if base_mode > Mode.NORMAL:
            actions.append(RiskAction(ENGINE_MODE_FLOOR, None, "MODE", f"mode={base_mode.name};why={','.join(floors)}"))
        entries_halted = self._entries_halted(ws)
        pos_liq, nav = self._valuation(ws, view)
        sleeve_values = self._sleeve_values(ws, pos_liq)
        ctx = TickContext(block=b, raw=snap, view=view, prev=prev, events=tuple(events), frame=frame,
                          portfolio=ws.portfolio, nav_liq=Rao(nav), sleeve=self._book_sleeve,
                          sleeve_value=Rao(sum(pos_liq.values())), mode=base_mode, store=store, book_view=book_view(ws, b))

        ran, new_signals, new_memories, outputs = self._run_strategies(ws, ctx, frame, events, sleeve_values, actions)
        signals_in: list[tuple[SleeveCfg, StrategyOutput]] = []
        for sl in self._sleeves:
            got = outputs.get(sl.strategy)
            if got is None:
                strat = self._strategies.get(sl.strategy)
                mem = self._memory(ws, strat, actions) if strat is not None else None
                got = StrategyOutput(tuple(s for s in ws.standing if s.strategy == sl.strategy), mem)
            signals_in.append((sl, got))

        risk = self.cfg.risk
        new_router = ws.router
        try:
            new_router = self.router(ctx, risk)
        except Exception as e:
            actions.append(RiskAction(ENGINE_ERROR, None, "MONITOR", f"stage=router;exc={_exc(e)}"))
        ctx = replace(ctx, book_view=replace(ctx.book_view, router=new_router))

        pipeline_ok = True
        proposal: TargetBook
        try:
            caps = self.caps(ctx, risk)
            proposal = self.allocator(signals_in, ctx, caps)
        except Exception as e:
            actions.append(RiskAction(ENGINE_ERROR, None, "MONITOR", f"stage=allocator;exc={_exc(e)}"))
            proposal = self._hold_book(ws, pos_liq, b)
        rctx = RiskContext(tick=ctx, cfg=risk, book=self.cfg, stages=self._stages, halted_by_operator=ws.halted,
                           orphans=ws.orphans,
                           burn_in_until=ws.burn_in_until if ws.burn_in_until is not None and ws.burn_in_until >= b else None)
        try:
            decision = self.overlay.review(proposal, rctx)
        except Exception as e:
            actions.append(RiskAction(ENGINE_ERROR, None, "MONITOR", f"stage=overlay;exc={_exc(e)}"))
            decision = RiskDecision(targets=self._hold_book(ws, pos_liq, b), actions=(), mode=max(base_mode, Mode.CAUTION))
            pipeline_ok = False
        targets, clamps = _monotone(proposal, decision.targets)
        actions.extend(clamps)
        targets = self._flatten(ws, targets)
        final_mode = max(decision.mode, base_mode)
        if not pipeline_ok:
            final_mode = max(final_mode, Mode.CAUTION)
        decision = RiskDecision(targets=targets, actions=decision.actions, mode=final_mode)
        if final_mode != ws.mode:
            why = ",".join(floors) if final_mode == base_mode and floors else "overlay" if pipeline_ok else "pipeline_error"
            out.add(Phase.DECIDE, ModeChanged(book, b, final_mode, why))

        # The FROZEN emergency exception (allow_emergency_exits_when_frozen) belongs to the key-alarm FROZEN row only
        # (section 3.11). An Engine floor of FROZEN (SafeMode: no staking call is whitelisted; no healthy head endpoint;
        # operator halt) admits nothing: risk exits are attempted only when the chain accepts staking calls (9.8 #15).
        emergency_ok = self.cfg.risk.allow_emergency_exits_when_frozen and base_mode < Mode.FROZEN

        # INTENDED orders the new decision invalidates are cancelled BEFORE planning, so their netuid is free again
        forced_keys = {fe.key for fe in targets.forced}
        cancelled: set[tuple[str, int]] = set()
        for o in ws.orders:
            if o.state is not OrderState.INTENDED:
                continue
            why_c = self._invalid(ws, o.intent, view, final_mode, forced_keys, entries_halted, emergency_ok)
            if why_c is not None:
                cancelled.add((o.intent.order_id, o.intent.attempt))
                out.add(Phase.DECIDE, OrderCancelled(book, o.intent.order_id, o.intent.attempt, b, why_c))
        open_orders = [o for o in ws.orders if o.state not in TERMINAL
                       and (o.intent.order_id, o.intent.attempt) not in cancelled]
        intents: tuple[OrderIntent, ...] = ()
        if pipeline_ok and not ws.halted:
            inflight = frozenset(o.intent.key for o in open_orders)
            pview = ctx.book_view
            if cancelled:                       # the planner's book history must show this tick's cancellations
                pview = replace(pview, orders=tuple(
                    r.to(OrderState.CANCELLED) if (r.intent.order_id, r.intent.attempt) in cancelled else r
                    for r in pview.orders))
            try:
                intents = tuple(self.planner(decision, replace(ctx, mode=final_mode, book_view=pview), inflight,
                                             self.run_id, book))
            except Exception as e:
                actions.append(RiskAction(ENGINE_ERROR, None, "MONITOR", f"stage=planner;exc={_exc(e)}"))
                intents = ()
        kept = self._filter_intents(ws, open_orders, intents, final_mode, forced_keys, entries_halted, emergency_ok,
                                    actions)

        # ---- EMIT
        for fe in targets.forced:
            actions.append(RiskAction(ENGINE_FORCED_EXIT, fe.key, "FORCE_EXIT", f"rule={fe.rule};urgency={int(fe.urgency)}"))
        for i in kept:                          # decision spot of every swap: chase episodes (buys) and the modelled
            if i.kind is OrderKind.ADD_STAKE_LIMIT or i.kind in _SELLS:    # cost of the sleeve cost ratio (all swaps)
                s = view.get(i.key)
                spot = s.pool.spot_rao() if s is not None else 0
                actions.append(RiskAction(ENGINE_ORDER_SPOT, i.key, "MONITOR", f"spot_rao={spot};order_id={i.order_id}"))
        memories = dict(new_memories)
        if new_router != ws.router:
            memories[ROUTER_MEMORY_ID] = codec.canonical_bytes(new_router)
        sleeve_nav = tuple((sid, Rao(cash + sleeve_values.get(sid, 0))) for sid, cash in ws.portfolio.sleeve_cash)
        out.add(Phase.EMIT, DecisionTrace(
            book=book, block=b, strategies_run=tuple(ran), signals=tuple(new_signals),
            actions=tuple(decision.actions) + tuple(actions), mode=final_mode, memories=tuple(sorted(memories.items())),
            features_digest=frame.digest, n_intents=len(kept), calib_digest=cal.digest if cal is not None else "",
            nav_liq=Rao(nav), sleeve_nav=sleeve_nav))
        seen_xfer: set[tuple[SubnetKey, StrategyId, StrategyId]] = set()
        for x in targets.transfers:
            k = (x.key, x.from_strategy, x.to_strategy)
            if k in seen_xfer:
                continue
            seen_xfer.add(k)
            out.add(Phase.EMIT, SleeveTransfer(book, b, x.key, x.from_strategy, x.to_strategy, x.shares, x.tao, x.price))
        for i in kept:
            out.add(Phase.EMIT, OrderIntended(i))
        return out.items

    # ------------------------------------------------------------------ ACCOUNT
    def _account(self, ws: EngineState, snap: ChainSnapshot, prev: ChainSnapshot | None,
                 cal: Calibration | None) -> list[JournalEvent]:
        out: list[JournalEvent] = []
        ledger = ledger_dict(ws)
        marks = dict(ws.marks)
        book, b = self.book, snap.block
        for p in ws.portfolio.positions:
            if p.key in ws.dissolving:
                continue
            s = snap.get(p.key)
            idx = s.hotkey(p.hotkey) if s is not None else None
            if idx is None:
                continue
            index = idx.index()
            led = ledger.get((pos_account(p.key, p.hotkey), alpha_unit(p.key)), 0)
            delta = value_at(p.shares, index) - led
            if delta != 0:
                out.append(YieldAccrued(book=book, key=p.key, hotkey=p.hotkey, block=b,
                                        index_before=marks.get(p.pkey, index), index_after=index, delta_alpha=delta))
        if self.mode is RunMode.LIVE:
            return out                          # live: DISSOLVING until reconciliation journals the observed payout
        for key in ws.dissolving:
            # ONE settlement per held generation: its idempotency key is dereg:{book}:{netuid}:{reg_at}, so a second
            # record for another hotkey on the same generation (an invariant-5 anomaly) would make the journal reject
            # the whole tick batch on every restart. The record covers every hotkey's alpha; the reducer closes them all.
            held = [p for p in ws.portfolio.positions if p.key == key]
            if not held:
                continue
            alpha = sum(max(ledger.get((pos_account(p.key, p.hotkey), alpha_unit(p.key)), 0), 0) for p in held)
            payout, model = self._dereg_payout(key, alpha, prev, cal)
            out.append(DeregSettled(book=book, key=key, hotkey=held[0].hotkey, block=b, alpha_value=AlphaRao(alpha),
                                    payout_tao=Rao(payout), model=model))
        return out

    def _dereg_payout(self, key: SubnetKey, alpha: int, prev: ChainSnapshot | None,
                      cal: Calibration | None) -> tuple[int, str]:
        """Modelled dissolution payout (section 8.8) from the last good snapshot: alpha x R x spot."""
        s = prev.get(key) if prev is not None else None
        if s is None or prev is None:
            return 0, f"{self._dereg_text}:no_snapshot"
        if self._dereg_fixed is not None:
            r = self._dereg_fixed
        else:
            r_default = cal.r_default if cal is not None else DEC.divide(Decimal(self.cfg.risk.r_default_ppm), Decimal(PPM))
            r = recovery_ratio(s, prev.glob, r_default)
            if cal is not None:
                r = effective_recovery(r, cal)
        payout = floor_int(DEC.multiply(DEC.multiply(Decimal(alpha), s.pool.spot()), r))
        return max(payout, 0), self._dereg_text

    # ------------------------------------------------------------------ DECIDE helpers
    def _base_mode(self, ws: EngineState, snap: ChainSnapshot, health: HealthObs) -> tuple[Mode, list[str]]:
        risk = self.cfg.risk
        b = snap.block
        floors: list[tuple[Mode, str]] = []
        if ws.halted:
            floors.append((Mode.FROZEN, "operator_halt"))
        if ws.exits_only:
            floors.append((Mode.EXITS_ONLY, "operator_exits_only"))
        if ws.drift_until is not None and ws.drift_until >= b:
            floors.append((Mode.CAUTION, "model_drift"))
        if health.healthy_endpoints <= 0:
            floors.append((Mode.FROZEN, "no_healthy_endpoint"))
        elif health.healthy_endpoints < 2:
            floors.append((Mode.CAUTION, "healthy_endpoints"))
        if health.secs_since_block > risk.stall_halt_s:
            floors.append((Mode.EXITS_ONLY, "stall_halt"))
        elif health.secs_since_block > risk.stall_warn_s:
            floors.append((Mode.CAUTION, "stall_warn"))
        if health.finality_lag_blocks > risk.finality_caution_blocks:
            floors.append((Mode.CAUTION, "finality_lag"))
        if snap.glob.safe_mode_until is not None and snap.glob.safe_mode_until >= b:
            floors.append((Mode.FROZEN, "safe_mode"))
        mode = max((m for m, _ in floors), default=Mode.NORMAL)
        return mode, [w for _, w in floors]

    @staticmethod
    def _entries_halted(ws: EngineState) -> bool:
        return (ws.halted or ws.orphans > 0 or ws.recon_halt or bool(ws.breaches)
                or (ws.entries_halted_until is not None and ws.entries_halted_until >= ws.clock))

    def _valuation(self, ws: EngineState, view: ChainSnapshot) -> tuple[dict[PositionKey, int], int]:
        """One-shot liquidation value of every position on the book's view, and NAV_liq = cash + their sum."""
        ledger = ledger_dict(ws)
        pos_liq: dict[PositionKey, int] = {}
        for p in ws.portfolio.positions:
            s = view.get(p.key)
            if s is None or p.key in ws.dissolving:
                pos_liq[p.pkey] = 0
                continue
            alpha = max(ledger.get((pos_account(p.key, p.hotkey), alpha_unit(p.key)), 0), 0)
            pos_liq[p.pkey] = int(liq_value(s.pool, AlphaRao(alpha)))
        return pos_liq, int(ws.portfolio.cash) + sum(pos_liq.values())

    @staticmethod
    def _sleeve_values(ws: EngineState, pos_liq: dict[PositionKey, int]) -> dict[StrategyId, int]:
        """Un-netted executable value of each sleeve: its share of every physical position's liquidation value."""
        phys: dict[SubnetKey, tuple[Decimal, int]] = {}
        for p in ws.portfolio.positions:
            sh, lv = phys.get(p.key, (Decimal(0), 0))
            phys[p.key] = (_EXACT.add(sh, p.shares), lv + pos_liq.get(p.pkey, 0))
        out: dict[StrategyId, int] = {}
        for h in ws.portfolio.sleeves:
            sh, lv = phys.get(h.key, (Decimal(0), 0))
            if sh <= 0 or lv <= 0:
                continue
            out[h.strategy] = out.get(h.strategy, 0) + floor_int(DEC.divide(DEC.multiply(Decimal(lv), h.shares), sh))
        return out

    def _memory(self, ws: EngineState, strat: Strategy, actions: list[RiskAction]) -> object:
        init = strat.initial_memory()
        raw = next((m for sid, m in ws.memories if sid == strat.id), None)
        if raw is None:
            return init
        cls = type(init)
        target: Any = cls if dataclasses.is_dataclass(cls) or cls in (int, str, bool, type(None)) else object
        try:
            return codec.decode_bytes(target, raw)
        except (codec.CodecError, ValueError) as e:
            actions.append(RiskAction(ENGINE_STRATEGY_ERROR, None, "MONITOR", f"strategy={strat.id};memory={_exc(e)}"))
            return init

    def _due(self, ws: EngineState, strat: Strategy, block: Block, frame: FeatureFrame, woke: set[Any]) -> bool:
        if block < strat.valid_from_block or not frame.warm:
            return False
        last = next((blk for sid, blk in ws.last_calls if sid == strat.id), None)
        every = max(int(strat.decide_every_blocks), 1)
        if last is None or block // every > last // every:
            return True
        return not woke.isdisjoint(strat.wake_on)

    def _run_strategies(self, ws: EngineState, ctx: TickContext, frame: FeatureFrame, events: Sequence[ChainEvent],
                        sleeve_values: dict[StrategyId, int], actions: list[RiskAction]
                        ) -> tuple[list[StrategyId], list[Signal], dict[StrategyId, bytes], dict[StrategyId, StrategyOutput]]:
        woke: set[Any] = {e.kind for e in events}
        ran: list[StrategyId] = []
        signals: list[Signal] = []
        memories: dict[StrategyId, bytes] = {}
        outputs: dict[StrategyId, StrategyOutput] = {}
        sleeve_cfg = {s.strategy: s for s in self._sleeves}
        for sid, strat in self._strategies.by_id:
            if not self._due(ws, strat, ctx.block, frame, woke):
                continue
            mem = self._memory(ws, strat, actions)
            sctx = replace(ctx, sleeve=sleeve_cfg[sid], sleeve_value=Rao(sleeve_values.get(sid, 0)))
            try:
                res = strat.on_tick(sctx, mem)
                raw = codec.canonical_bytes(res.memory)
            except Exception as e:
                actions.append(RiskAction(ENGINE_STRATEGY_ERROR, None, "MONITOR", f"strategy={sid};exc={_exc(e)}"))
                continue
            own = tuple(s for s in res.signals if s.strategy == sid)
            if len(own) != len(res.signals):
                actions.append(RiskAction(ENGINE_STRATEGY_ERROR, None, "MONITOR", f"strategy={sid};foreign_signals="
                                          f"{len(res.signals) - len(own)}"))
            ran.append(sid)
            signals.extend(own)
            memories[sid] = raw
            outputs[sid] = StrategyOutput(own, res.memory)
        return ran, signals, memories, outputs

    def _hold_book(self, ws: EngineState, pos_liq: dict[PositionKey, int], b: Block) -> TargetBook:
        """Fail-closed proposal: hold every position at its current value (no entries, no exits)."""
        items = tuple(TargetPosition(p.key, p.hotkey, Rao(pos_liq.get(p.pkey, 0)), Urgency.NORMAL,
                                     holding_attribution(ws.portfolio, p.key), ("engine.hold",))
                      for p in ws.portfolio.positions)
        return TargetBook(asof=b, items=tuple(sorted(items, key=lambda t: t.key)))

    def _flatten(self, ws: EngineState, targets: TargetBook) -> TargetBook:
        """Operator flatten:<netuid>: an URGENT forced exit and a zero target for every held key on that netuid."""
        if not ws.flatten:
            return targets
        forced = list(targets.forced)
        have = {fe.key for fe in forced}
        for p in ws.portfolio.positions:
            if p.key.netuid not in ws.flatten:
                continue
            if targets.get(p.key) is not None:
                targets = targets.reduced(p.key, Rao(0), "operator.flatten", Urgency.URGENT)
            if p.key not in have:
                have.add(p.key)
                forced.append(ForcedExit(p.key, Urgency.URGENT, "operator", self.cfg.risk.s_urgent_ppm))
        return replace(targets, forced=tuple(forced))

    @staticmethod
    def _allowed(intent: OrderIntent, mode: Mode, forced_keys: set[SubnetKey], entries_halted: bool,
                 emergency_ok: bool) -> str | None:
        """None if `intent` may be emitted under `mode`, else the reason (section 3.11 mode semantics). `emergency_ok`:
        the FROZEN emergency exception applies (configured, and FROZEN does not come from an Engine floor)."""
        buy = intent.kind is OrderKind.ADD_STAKE_LIMIT
        if buy and entries_halted:
            return "entries_halted"
        if buy and mode >= Mode.CAUTION:
            return f"mode:{mode.name}"
        if mode is Mode.FROZEN:
            ok = (emergency_ok and intent.kind in _SELLS and intent.urgency is Urgency.EMERGENCY
                  and intent.key in forced_keys)
            return None if ok else f"mode:{mode.name}"
        if mode is Mode.EXITS_ONLY and not (intent.kind in _SELLS and intent.key in forced_keys):
            return f"mode:{mode.name}"
        return None

    def _filter_intents(self, ws: EngineState, open_orders: Sequence[OrderEntry], intents: Sequence[OrderIntent], mode: Mode,
                        forced_keys: set[SubnetKey], entries_halted: bool, emergency_ok: bool,
                        actions: list[RiskAction]) -> list[OrderIntent]:
        known = {o.intent.order_id for o in ws.orders}
        netuids = {int(o.intent.key.netuid) for o in open_orders}
        kept: list[OrderIntent] = []
        for i in intents:
            why: str | None
            if i.book != self.book:
                why = "foreign_book"
            elif i.order_id in known:
                why = "duplicate_order_id"
            elif int(i.key.netuid) in netuids:
                why = "netuid_inflight"
            else:
                why = self._allowed(i, mode, forced_keys, entries_halted, emergency_ok)
            if why is not None:
                actions.append(RiskAction(ENGINE_DROP_INTENT, i.key, "VETO_ENTRY", f"order_id={i.order_id};why={why}"))
                continue
            known.add(i.order_id)
            netuids.add(int(i.key.netuid))
            kept.append(i)
        return kept

    def _invalid(self, ws: EngineState, i: OrderIntent, view: ChainSnapshot, mode: Mode, forced_keys: set[SubnetKey],
                 entries_halted: bool, emergency_ok: bool) -> str | None:
        """Why an INTENDED (not yet submitted) order is no longer valid at this tick, or None."""
        if ws.halted:
            return "operator_halt"
        if view.get(i.key) is None:
            return "pool_gone"
        step = self.cfg.exec.latency_blocks if i.shielded else _UNSHIELDED_INCLUSION_BLOCKS
        if int(view.block) + self.cfg.exec.finality_lag_blocks + step > int(i.valid_until):
            return "ttl_expired"
        return self._allowed(i, mode, forced_keys, entries_halted, emergency_ok)


def _monotone(proposal: TargetBook, reviewed: TargetBook) -> tuple[TargetBook, list[RiskAction]]:
    """Enforce the overlay's monotonicity (section 3.1): no reviewed target above the proposal; a target the proposal
    did not contain is clamped to zero."""
    actions: list[RiskAction] = []
    items: list[TargetPosition] = []
    for t in reviewed.items:
        p = proposal.get(t.key)
        cap = p.value_rao if p is not None else Rao(0)
        if t.value_rao > cap:
            actions.append(RiskAction(ENGINE_CLAMP, t.key, "CLAMP", f"from={t.value_rao};to={cap}"))
            t = replace(t, value_rao=cap, reasons=t.reasons + (ENGINE_CLAMP,))
        items.append(t)
    return replace(reviewed, items=tuple(items)), actions

