"""Fakes and factories for the WP7 engine tests (strategy, router, caps, allocator, overlay, planner, venue, data source,
feature engine). WP8/WP9 do not exist yet, so every decision component is a small deterministic fake here.

Test modules cannot import each other (importlib mode), so everything is exposed through the `fx` fixture (a namespace
of classes and helpers) and a few convenience fixtures.

Market: two Balancer 0.5/0.5 subnets (SN7 and SN9, reg_at 1,000), 1,000 TAO at 0.01 TAO/alpha, fee 33, hotkeys HK_A and
HK_B tracked with an index that grows 0.03% at every 360-block epoch drain (LastEpochBlock advances, so EPOCH_DRAIN
fires). Blocks start at 9,000,000 (a multiple of 300 and 60).
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from typing import Any, TypeVar

import pytest

from taotrader.core.config import BookCfg, ExecCfg, RiskCfg, SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent, ChainEventKind, HealthObs, JournalEvent
from taotrader.core.orders import OrderIntent, OrderKind, Resolution, Urgency, VenueCaps, make_order_id
from taotrader.core.portfolio import alpha_unit, pos_account
from taotrader.core.protocols import RiskContext, RouterState, SourceItem, TickContext
from taotrader.core.signals import RiskDecision, Signal, SignalKind, StrategyOutput, TargetBook, TargetPosition
from taotrader.core.state import ChainSnapshot, HotkeyIdx, PoolKind, PoolState, SubnetState
from taotrader.core.units import (
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    Mode,
    NetUid,
    Ppm,
    PriceRao,
    Rao,
    RunMode,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.core.views import EmissionView, FeatureFrame, PruneView
from taotrader.engine.engine import Engine
from taotrader.engine.recovery import BookRuntime
from taotrader.engine.reducer import holding_attribution, ledger_balance
from taotrader.protocol.amm import liq_value, marginal_after_buy, marginal_after_sell, quote_buy
from taotrader.venues.sim import SimVenue

T = TypeVar("T")
TAO = RAO_PER_TAO
RUN = "run-wp7"
START = 9_000_000
HK_A = Hotkey("0x" + "a" * 64)
HK_B = Hotkey("0x" + "b" * 64)
K7 = SubnetKey(NetUid(7), Block(1_000))
K9 = SubnetKey(NetUid(9), Block(1_000))
EPOCH = 360
GROWTH_PPM = 300                     # +0.03% index per epoch drain


def arun(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------- market
def pool(tao_tao: int = 1_000, price: str = "0.01") -> PoolState:
    t = tao_tao * TAO
    a = int(Decimal(t) / Decimal(price))
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(t), alpha=AlphaRao(a), px_tao=t, px_alpha=a, w_quote_e18=5 * 10**17,
                     fee_rate=33)


def hk_idx(hotkey: Hotkey, block: int, base: int = 50_000 * TAO) -> HotkeyIdx:
    drains = max(0, (block - START) // EPOCH)
    total = base
    for _ in range(drains):
        total = total * (PPM + GROWTH_PPM) // PPM
    return HotkeyIdx(hotkey=hotkey, total_alpha=AlphaRao(total), total_shares=Decimal(base), earns=True)


def subnet(make_subnet: Callable[..., SubnetState], key: SubnetKey, block: int, p: PoolState | None = None,
           **overrides: Any) -> SubnetState:
    last_epoch = START + max(0, (block - START) // EPOCH) * EPOCH
    hks = (hk_idx(HK_A, block), hk_idx(HK_B, block))
    return make_subnet(int(key.netuid), reg_at=int(key.reg_at), pool=p if p is not None else pool(), hotkeys=hks,
                       last_epoch_block=Block(last_epoch), **overrides)


def snapshot(make_snapshot: Callable[..., ChainSnapshot], subnets: Sequence[SubnetState], block: int,
             **glob: Any) -> ChainSnapshot:
    s = make_snapshot(block, subnets, **glob)
    return replace(s, block_hash=BlockHash("0x" + f"{block:064x}"), digest=f"dig{block}")


# ------------------------------------------------------------------------------------------------- data source
class FakeStore:
    """SnapshotStore over an in-memory dict with the lookahead guard."""

    def __init__(self, snaps: Sequence[ChainSnapshot] = ()) -> None:
        self.clock: Block = Block(0)
        self._by_block: dict[int, ChainSnapshot] = {int(s.block): s for s in snaps}

    def add(self, s: ChainSnapshot) -> None:
        self._by_block[int(s.block)] = s

    def at(self, block: Block) -> ChainSnapshot:
        if block > self.clock:
            raise LookaheadError(f"{block} > clock {self.clock}")
        return self._by_block[int(block)]

    def at_or_before(self, block: Block) -> ChainSnapshot:
        if block > self.clock:
            raise LookaheadError(f"{block} > clock {self.clock}")
        best = max((b for b in self._by_block if b <= block), default=None)
        if best is None:
            raise KeyError(block)
        return self._by_block[best]

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        if until > self.clock:
            raise LookaheadError(f"{until} > clock {self.clock}")
        return [self._by_block[b] for b in sorted(self._by_block) if until - span_blocks < b <= until]


class FakeSource:
    """DataSource over a fixed snapshot list (strictly after `after`)."""

    def __init__(self, snaps: Sequence[ChainSnapshot], cadence_blocks: int = 60,
                 health: Callable[[int], HealthObs] | None = None) -> None:
        self.snaps = sorted(snaps, key=lambda s: s.block)
        self.cadence_blocks = cadence_blocks
        self.store = FakeStore(self.snaps)
        self.health = health

    async def _gen(self, after: Block | None) -> AsyncIterator[SourceItem]:
        for s in self.snaps:
            if after is None or s.block > after:
                h = self.health(int(s.block)) if self.health is not None else HealthObs.nominal()
                yield SourceItem(s, h)

    def stream(self, after: Block | None) -> AsyncIterator[SourceItem]:
        return self._gen(after)

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------------------------------------- features
def _prune_view() -> PruneView:
    return PruneView(prune_possible=False, target=None, runtime_agrees=True, ladder=(), bottom_ema=0.0,
                     blocks_since_reg=0, window_open=False, blocks_to_window=0, cost_ratio=1.0, p_reg_ppm=(),
                     hazard_valid=True, immunity_calendar=())


def _emission_view() -> EmissionView:
    return EmissionView(theta=0.0, gate_rank=32, sum_ema=0.0, root_flag=False, parity_err_max_tao_day=0.0, model_ok=True)


class FakeFeatures:
    """Deterministic FeatureEngine fake. Its state is the last `window` (block, digest, own-fill blocks) entries, so a
    re-warm over >= window ticks reproduces state_digest() exactly. warm after `warm_after` ingested snapshots."""

    def __init__(self, warm_after: int = 0, window: int = 8) -> None:
        self.warm = False
        self.warm_after = warm_after
        self.window = window
        self.n = 0
        self.hist: tuple[tuple[int, str, tuple[int, ...]], ...] = ()
        self.calls: list[tuple[int, frozenset[Block]]] = []

    def update(self, raw: ChainSnapshot, events: Sequence[ChainEvent], own_fill_blocks: frozenset[Block] = frozenset()
               ) -> FeatureFrame:
        self.n += 1
        self.calls.append((int(raw.block), own_fill_blocks))
        kinds = ",".join(sorted(e.kind.value for e in events))
        self.hist = (self.hist + ((int(raw.block), f"{raw.digest}|{kinds}", tuple(sorted(own_fill_blocks))),))[-self.window:]
        self.warm = self.n > self.warm_after
        return FeatureFrame(block=raw.block, warm=self.warm, feats=MappingProxyType({}), prune=_prune_view(),
                            emission=_emission_view(), regime_id="test", universe_eligible=len(raw.subnets),
                            beta_horizon_blocks=60, digest=self.state_digest())

    def state_digest(self) -> str:
        return hashlib.blake2b(repr(self.hist).encode(), digest_size=16).hexdigest()


# ------------------------------------------------------------------------------------------------- decisions
@dataclass(frozen=True, slots=True)
class Mem:
    count: int = 0
    last_block: int = 0


@dataclass
class TargetStrategy:
    """TARGET signals from a schedule: [(from_block, {key: weight_ppm})]; the latest entry at or before the block."""
    id: StrategyId
    schedule: list[tuple[int, dict[SubnetKey, int]]]
    decide_every_blocks: int = 300
    wake_on: frozenset[ChainEventKind] = frozenset()
    min_cadence_blocks: int = 60
    valid_from_block: Block = Block(0)
    declares_dilution: bool = False
    horizon_blocks: int = 0
    calls: list[int] = field(default_factory=list)

    def initial_memory(self) -> object:
        return Mem()

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        assert isinstance(memory, Mem)
        self.calls.append(int(ctx.block))
        weights: dict[SubnetKey, int] = {}
        for frm, w in self.schedule:
            if ctx.block >= frm:
                weights = w
        sigs = tuple(Signal(strategy=self.id, key=k, asof=ctx.block, kind=SignalKind.TARGET, weight_ppm=Ppm(w),
                            horizon_blocks=self.horizon_blocks) for k, w in sorted(weights.items()))
        return StrategyOutput(sigs, Mem(memory.count + 1, int(ctx.block)))


def fake_router(ctx: TickContext, risk: RiskCfg) -> RouterState:
    """Keep the previous choice; otherwise the first tracked hotkey of every subnet."""
    choice = dict(ctx.book_view.router.choice)
    for s in ctx.raw.subnets:
        if s.key not in choice and s.hotkeys:
            choice[s.key] = s.hotkeys[0].hotkey
    return RouterState(choice=tuple(sorted(choice.items())))


def fake_caps(ctx: TickContext, risk: RiskCfg) -> dict[SubnetKey, Rao]:
    return {}


def fake_allocator(signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext,
                   caps: dict[SubnetKey, Rao]) -> TargetBook:
    """value_k = sum over sleeves of NAV_liq * budget * weight; held keys without a target go to 0."""
    totals: dict[SubnetKey, dict[StrategyId, int]] = {}
    for sleeve, out in signals:
        for s in out.signals:
            if s.kind is not SignalKind.TARGET:
                continue
            v = ctx.nav_liq * sleeve.budget_ppm // PPM * s.weight_ppm // PPM
            totals.setdefault(s.key, {})[sleeve.strategy] = totals.get(s.key, {}).get(sleeve.strategy, 0) + v
    items: list[TargetPosition] = []
    for key, parts in sorted(totals.items()):
        total = sum(parts.values())
        hot = ctx.book_view.router.hotkey(key) or HK_A
        if total <= 0:
            attribution = holding_attribution(ctx.portfolio, key)
        else:
            sids = sorted(parts)
            ppms = [parts[sid] * PPM // total for sid in sids]
            ppms[0] += PPM - sum(ppms)
            attribution = tuple((sid, Ppm(p)) for sid, p in zip(sids, ppms, strict=True) if p > 0)
        items.append(TargetPosition(key, hot, Rao(total), Urgency.NORMAL, attribution))
    for p in ctx.portfolio.positions:
        if p.key not in totals:
            items.append(TargetPosition(p.key, p.hotkey, Rao(0), Urgency.NORMAL, holding_attribution(ctx.portfolio, p.key)))
    return TargetBook(asof=ctx.block, items=tuple(sorted(items, key=lambda t: t.key)))


@dataclass
class FakeOverlay:
    """Pass-through review; `forced` = {key: (from_block, urgency, rule)} adds forced exits; `mode` raises the mode."""
    forced: dict[SubnetKey, tuple[int, Urgency, str]] = field(default_factory=dict)
    mode: Mode = Mode.NORMAL
    extra_actions: tuple[Any, ...] = ()
    contexts: list[RiskContext] = field(default_factory=list)

    def review(self, proposal: TargetBook, ctx: RiskContext) -> RiskDecision:
        self.contexts.append(ctx)
        targets = proposal
        forced = []
        from taotrader.core.signals import ForcedExit
        for key, (frm, urg, rule) in sorted(self.forced.items()):
            if ctx.tick.block >= frm and ctx.tick.portfolio.position(key) is not None:
                forced.append(ForcedExit(key, urg, rule, Ppm(250_000)))
                if targets.get(key) is not None:
                    targets = targets.reduced(key, Rao(0), rule, urg)
        targets = replace(targets, forced=tuple(forced))
        return RiskDecision(targets=targets, actions=self.extra_actions, mode=max(self.mode, ctx.tick.mode))


@dataclass
class FakePlanner:
    """A small section 3.12-shaped planner: one order per netuid, buy limits above the post-fill marginal, sells and
    forced exits below it, deterministic ids. `min_trade_rao` is the no-trade band."""
    min_trade_rao: int = TAO // 2
    beta_buy_ppm: int = 20_000
    beta_sell_ppm: int = 50_000
    calls: int = 0

    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey], run_id: str,
                 book: BookId) -> tuple[OrderIntent, ...]:
        self.calls += 1
        out: list[OrderIntent] = []
        used: set[int] = set()
        lag, lat = 3, 2
        b = ctx.block
        forced = {fe.key: fe for fe in decision.targets.forced}

        def held(key: SubnetKey) -> tuple[Hotkey, int, int] | None:
            for p in ctx.portfolio.positions:
                if p.key == key:
                    s = ctx.view.get(key)
                    idx = s.hotkey(p.hotkey) if s is not None else None
                    if s is None or idx is None:
                        return None
                    alpha = idx.value_of(p.shares)
                    return p.hotkey, alpha, int(liq_value(s.pool, alpha))
            return None

        def intent(kind: OrderKind, key: SubnetKey, hot: Hotkey, *, tao_in: int = 0, alpha_in: int = 0, full: bool = False,
                   limit: int, partial: bool = False, urgency: Urgency = Urgency.NORMAL, expected: int = 0,
                   attribution: Any = None) -> OrderIntent:
            return OrderIntent(order_id=make_order_id(run_id, book, b, key, hot, kind, 0), attempt=0, book=book,
                               created_block=b, kind=kind, key=key, hotkey=hot, tao_in=Rao(tao_in),
                               alpha_in=AlphaRao(alpha_in), full_position=full, limit_price=PriceRao(limit),
                               allow_partial=partial, shielded=True, valid_until=Block(b + lag + lat), expected_out=expected,
                               urgency=urgency, attribution=attribution or holding_attribution(ctx.portfolio, key),
                               reason="fake")

        for key, fe in sorted(forced.items()):
            h = held(key)
            s = ctx.view.get(key)
            if h is None or s is None or key in inflight or int(key.netuid) in used:
                continue
            spot = s.pool.spot_rao()
            out.append(intent(OrderKind.REMOVE_STAKE_LIMIT, key, h[0], full=True, limit=spot * 3 // 4, partial=True,
                              urgency=fe.urgency))
            used.add(int(key.netuid))
        for t in decision.targets.items:
            if t.key in inflight or int(t.key.netuid) in used or t.key in forced:
                continue
            s = ctx.view.get(t.key)
            if s is None:
                continue
            h = held(t.key)
            cur = h[2] if h is not None else 0
            delta = t.value_rao - cur
            if delta >= self.min_trade_rao:
                tao_in = min(delta, ctx.portfolio.cash - 50_000_000)
                if tao_in < self.min_trade_rao:
                    continue
                m = marginal_after_buy(s.pool, Rao(tao_in))
                limit = -((-m * (PPM + self.beta_buy_ppm)) // PPM)
                exp = quote_buy(s.pool, Rao(tao_in)).amount_out
                out.append(intent(OrderKind.ADD_STAKE_LIMIT, t.key, t.hotkey, tao_in=tao_in, limit=limit, expected=exp,
                                  attribution=t.attribution))
                used.add(int(t.key.netuid))
            elif h is not None and t.value_rao == 0:
                m = marginal_after_sell(s.pool, AlphaRao(h[1]))
                out.append(intent(OrderKind.REMOVE_STAKE_LIMIT, t.key, h[0], full=True,
                                  limit=m * (PPM - self.beta_sell_ppm) // PPM, expected=h[2]))
                used.add(int(t.key.netuid))
            elif h is not None and -delta >= self.min_trade_rao and cur > 0:
                alpha = h[1] * (-delta) // cur
                m = marginal_after_sell(s.pool, AlphaRao(alpha))
                out.append(intent(OrderKind.REMOVE_STAKE_LIMIT, t.key, h[0], alpha_in=alpha,
                                  limit=m * (PPM - self.beta_sell_ppm) // PPM, expected=int(liq_value(s.pool, alpha))))
                used.add(int(t.key.netuid))
        return tuple(out)


# ------------------------------------------------------------------------------------------------- venues
@dataclass
class ScriptedVenue:
    """A venue whose answers are scripted: submit -> ack (or raise), advance -> queued events, resolve -> scripted."""
    book: BookId
    caps: VenueCaps = field(default_factory=lambda: VenueCaps("fake", 2, 8, False, 1, 3))
    raise_on_submit: bool = False
    queued: list[JournalEvent] = field(default_factory=list)
    resolution: tuple[Resolution, tuple[JournalEvent, ...]] | None = None
    observed: list[JournalEvent] = field(default_factory=list)
    submits: list[str] = field(default_factory=list)

    def mark_to(self, raw: ChainSnapshot) -> ChainSnapshot:
        return raw

    async def reserve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[str, int | None, Block | None]:
        return ("sim0", None, Block(now.block + 10))

    async def submit(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent:
        self.submits.append(intent.order_id)
        if self.raise_on_submit:
            raise ConnectionError("node dropped the connection")
        from taotrader.core.events import VenueAck
        return VenueAck(self.book, intent.order_id, intent.attempt, now.block, Block(now.block + 2), "", "")

    async def advance(self, view: ChainSnapshot) -> JournalEvent | None:
        return self.queued.pop(0) if self.queued else None

    async def resolve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[Resolution, tuple[JournalEvent, ...]]:
        if self.resolution is None:
            return (Resolution.UNRESOLVABLE_YET, ())
        return self.resolution

    def observe(self, ev: JournalEvent) -> None:
        self.observed.append(ev)


# ------------------------------------------------------------------------------------------------- assembly
def book_cfg(book: str = "b1", *, sleeves: Sequence[SleeveCfg] | None = None, capital_tao: int = 100,
             exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None, dereg_model: str = "formula") -> BookCfg:
    sl = tuple(sleeves) if sleeves is not None else (SleeveCfg(StrategyId("carry"), Stage.PAPER, Ppm(800_000)),)
    return BookCfg(book=BookId(book), capital_rao=Rao(capital_tao * TAO), fee_float_rao=Rao(TAO), sleeves=sl,
                   risk=risk if risk is not None else RiskCfg(), exec=exec_cfg if exec_cfg is not None else ExecCfg(),
                   dereg_model=dereg_model)


def make_engine(cfg: BookCfg, strategies: Sequence[Any], *, mode: RunMode = RunMode.BACKTEST, overlay: Any = None,
                planner: Any = None, router: Any = fake_router, caps: Any = fake_caps, allocator: Any = fake_allocator,
                calibration: Any = None, run_id: str = RUN) -> Engine:
    return Engine(run_id=run_id, cfg=cfg, mode=mode, strategies=strategies, router=router, caps=caps, allocator=allocator,
                  overlay=overlay if overlay is not None else FakeOverlay(),
                  planner=planner if planner is not None else FakePlanner(), calibration=calibration)


def make_runtime(engine: Engine, venue: Any = None, *, seed: int = 0) -> BookRuntime:
    v = venue if venue is not None else SimVenue(engine.book, engine.cfg.exec, seed=seed)
    return BookRuntime(engine=engine, venue=v)


def position_alpha(state: Any, key: SubnetKey, hotkey: Hotkey) -> int:
    return ledger_balance(state, pos_account(key, hotkey), alpha_unit(key))


def mk_intent(kind: OrderKind, block: int, key: SubnetKey = K7, hotkey: Hotkey = HK_A, *, tao_in: int = 0,
              alpha_in: int = 0, full: bool = False, attempt: int = 0, book: str = "b1",
              attribution: Any = None, dest_hotkey: Hotkey | None = None,
              urgency: Urgency = Urgency.NORMAL, limit: int = 1, valid_until: int | None = None,
              expected_out: int = 0) -> OrderIntent:
    """A bare OrderIntent for reducer tests (limits are irrelevant there)."""
    return OrderIntent(order_id=make_order_id(RUN, BookId(book), Block(block), key, hotkey, kind, attempt), attempt=attempt,
                       book=BookId(book), created_block=Block(block), kind=kind, key=key, hotkey=hotkey,
                       tao_in=Rao(tao_in), alpha_in=AlphaRao(alpha_in), full_position=full, limit_price=PriceRao(limit),
                       allow_partial=False, shielded=True,
                       valid_until=Block(valid_until if valid_until is not None else block + 5), expected_out=expected_out,
                       urgency=urgency, attribution=attribution if attribution is not None else ((StrategyId("carry"), Ppm(PPM)),),
                       reason="test", dest_hotkey=dest_hotkey)


def mk_fill(i: OrderIntent, block: int, *, tao: int = 0, alpha: int = 0, shares: Decimal | None = None, leg: int = 0,
            swap_fee: int = 0, tx_fee: int = 0, author_fee: int = 0, exact: bool = True,
            dest_shares: Decimal | None = None, spot_before: int = 10_000_000, shortfall_ppm: int = 0) -> Any:
    """A Fill for intent `i` (shares default to alpha, i.e. index 1)."""
    from taotrader.core.orders import Fill
    return Fill(fill_id=f"{i.order_id}:{i.attempt}:{leg}", order_id=i.order_id, attempt=i.attempt, book=i.book,
                block=Block(block), kind=i.kind, key=i.key, hotkey=i.hotkey, tao=Rao(tao), alpha=AlphaRao(alpha),
                shares=Decimal(alpha) if shares is None else shares, swap_fee=swap_fee, author_fee_tao=Rao(author_fee),
                tx_fee=Rao(tx_fee), d_pool_tao=0, d_pool_alpha=0, spot_before=PriceRao(spot_before),
                shortfall_ppm=Ppm(shortfall_ppm), complete=True, exact_block=exact,
                dest_key=i.key if i.dest_hotkey is not None else None, dest_hotkey=i.dest_hotkey, dest_shares=dest_shares)


@pytest.fixture(scope="session")
def fx() -> SimpleNamespace:
    return SimpleNamespace(
        TAO=TAO, RUN=RUN, START=START, HK_A=HK_A, HK_B=HK_B, K7=K7, K9=K9, EPOCH=EPOCH, arun=arun, pool=pool,
        hk_idx=hk_idx, subnet=subnet, snapshot=snapshot, FakeStore=FakeStore, FakeSource=FakeSource,
        FakeFeatures=FakeFeatures, Mem=Mem, TargetStrategy=TargetStrategy, fake_router=fake_router, fake_caps=fake_caps,
        fake_allocator=fake_allocator, FakeOverlay=FakeOverlay, FakePlanner=FakePlanner, ScriptedVenue=ScriptedVenue,
        book_cfg=book_cfg, make_engine=make_engine, make_runtime=make_runtime, position_alpha=position_alpha,
        mk_intent=mk_intent, mk_fill=mk_fill)


@pytest.fixture(scope="session")
def market(make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot]
           ) -> Callable[..., list[ChainSnapshot]]:
    """market(n, stride=60, start=START, keys=(K7, K9), mutate=None) -> n snapshots; mutate(block, subnets) may edit
    the subnet list (e.g. remove a key to deregister it) and return glob overrides."""

    def build(n: int, stride: int = 60, start: int = START, keys: Sequence[SubnetKey] = (K7, K9),
              mutate: Callable[[int, list[SubnetState]], dict[str, Any] | None] | None = None) -> list[ChainSnapshot]:
        out = []
        for i in range(n):
            b = start + i * stride
            subs = [subnet(make_subnet, k, b) for k in keys]
            glob = mutate(b, subs) if mutate is not None else None
            out.append(snapshot(make_snapshot, subs, b, **(glob or {})))
        return out

    return build
