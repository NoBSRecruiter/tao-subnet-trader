"""LiveVenue + LiveReconciler inside the real WP7 Runner / Engine / reducer with a SqliteJournal (FakeSdkPort chain).

1. A buy decided at B0+1 goes INTENDED -> SubmitStarted(delegate, nonce, era_end) -> VenueAck -> FillReported at N+2,
   the hash chain verifies, the reducer folds every live fact (no orphan, no invariant breach), and reconciliation of
   the folded ledger against the fake chain is clean (no ReconAdjusted).
2. A crash right after SubmitStarted (before the send): a fresh Runner recovers, journals
   SubmitUnknown("recovered_submitting"), resolves by the delegate nonce (never re-sending) and ends NOT_PLACED once the
   era is provably over.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Sequence
from decimal import Decimal
from types import MappingProxyType, ModuleType
from typing import Any

import pytest

from taotrader.core.codec import decode_event
from taotrader.core.config import BookCfg, RiskCfg, SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent, HealthObs
from taotrader.core.orders import OrderIntent, OrderKind, OrderState, Urgency, make_order_id
from taotrader.core.protocols import RouterState, SnapshotStore, SourceItem, TickContext
from taotrader.core.signals import RiskDecision, StrategyOutput, TargetBook
from taotrader.core.state import ChainSnapshot
from taotrader.core.units import PPM, AlphaRao, Block, BookId, Ppm, PriceRao, Rao, RunMode, StrategyId, SubnetKey
from taotrader.core.views import EmissionView, FeatureFrame, PruneView
from taotrader.data.journal import SqliteJournal
from taotrader.engine.engine import Engine
from taotrader.engine.recovery import BookRuntime
from taotrader.engine.runner import Runner
from taotrader.live.reconcile import LiveReconciler
from taotrader.live.venue import LiveVenue


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Store:
    def __init__(self, snaps: Sequence[ChainSnapshot]) -> None:
        self.clock: Block = Block(0)
        self._by = {int(s.block): s for s in snaps}

    def at(self, block: Block) -> ChainSnapshot:
        if block > self.clock:
            raise LookaheadError(str(block))
        return self._by[int(block)]

    def at_or_before(self, block: Block) -> ChainSnapshot:
        if block > self.clock:
            raise LookaheadError(str(block))
        return self._by[max(b for b in self._by if b <= block)]

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        return [self._by[b] for b in sorted(self._by) if until - span_blocks < b <= until]


class Source:
    """Per-block DataSource; `on_block(b)` runs before each item (moves the fake chain's heads, scripts inclusions)."""

    def __init__(self, snaps: Sequence[ChainSnapshot], on_block: Callable[[int], None]) -> None:
        self.snaps = list(snaps)
        self.cadence_blocks = 1
        self.store: SnapshotStore = Store(self.snaps)
        self.on_block = on_block

    async def _gen(self, after: Block | None) -> AsyncIterator[SourceItem]:
        for s in self.snaps:
            if after is None or s.block > after:
                self.on_block(int(s.block))
                yield SourceItem(s, HealthObs.nominal())

    def stream(self, after: Block | None) -> AsyncIterator[SourceItem]:
        return self._gen(after)

    async def aclose(self) -> None:
        return None


class Features:
    warm = True

    def __init__(self) -> None:
        self.hist: tuple[int, ...] = ()

    def update(self, raw: ChainSnapshot, events: Sequence[ChainEvent], own_fill_blocks: frozenset[Block] = frozenset()
               ) -> FeatureFrame:
        self.hist = (self.hist + (int(raw.block),))[-4:]
        prune = PruneView(prune_possible=False, target=None, runtime_agrees=True, ladder=(), bottom_ema=0.0,
                          blocks_since_reg=0, window_open=False, blocks_to_window=0, cost_ratio=1.0, p_reg_ppm=(),
                          hazard_valid=True, immunity_calendar=())
        em = EmissionView(theta=0.0, gate_rank=32, sum_ema=0.0, root_flag=False, parity_err_max_tao_day=0.0, model_ok=True)
        return FeatureFrame(block=raw.block, warm=True, feats=MappingProxyType({}), prune=prune, emission=em,
                            regime_id="test", universe_eligible=len(raw.subnets), beta_horizon_blocks=5,
                            digest=self.state_digest())

    def state_digest(self) -> str:
        return hashlib.blake2b(repr(self.hist).encode(), digest_size=16).hexdigest()


def router(ctx: TickContext, risk: RiskCfg) -> RouterState:
    return ctx.book_view.router


def caps(ctx: TickContext, risk: RiskCfg) -> dict[SubnetKey, Rao]:
    return {}


def allocator(signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext, caps: dict[SubnetKey, Rao]) -> TargetBook:
    return TargetBook(asof=ctx.block, items=())


class Overlay:
    def review(self, proposal: TargetBook, ctx: Any) -> RiskDecision:
        return RiskDecision(targets=proposal, actions=(), mode=ctx.tick.mode)


class Planner:
    """One 0.5-TAO shielded buy of KEY on HK_A at `at`."""

    def __init__(self, lk: ModuleType, at: int) -> None:
        self.lk = lk
        self.at = at

    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey], run_id: str,
                 book: BookId) -> tuple[OrderIntent, ...]:
        lk = self.lk
        if int(ctx.block) != self.at or lk.KEY in inflight:
            return ()
        tao = lk.TAO // 2
        return (OrderIntent(order_id=make_order_id(run_id, book, ctx.block, lk.KEY, lk.HK_A, OrderKind.ADD_STAKE_LIMIT, 0),
                            attempt=0, book=book, created_block=ctx.block, kind=OrderKind.ADD_STAKE_LIMIT, key=lk.KEY,
                            hotkey=lk.HK_A, tao_in=Rao(tao), alpha_in=AlphaRao(0), full_position=False,
                            limit_price=PriceRao(int(lk.buy_limit(lk.DEFAULT_POOLS[lk.SN], tao))), allow_partial=False,
                            shielded=True, valid_until=Block(int(ctx.block) + 5), expected_out=0, urgency=Urgency.NORMAL,
                            attribution=((StrategyId("book"), Ppm(PPM)),), reason="it"),)


def build(lk: ModuleType, k: Any, journal: SqliteJournal, snaps: Sequence[ChainSnapshot], on_block: Callable[[int], None],
          fault: Callable[[str], None] | None = None) -> tuple[Runner, BookRuntime, list[tuple[str, str]]]:
    cfg = BookCfg(book=lk.BOOK, capital_rao=Rao(lk.REAL_FREE), fee_float_rao=Rao(3 * lk.DELEGATE_FREE), sleeves=())
    eng = Engine(run_id=lk.RUN, cfg=cfg, mode=RunMode.LIVE, strategies=[], router=router, caps=caps, allocator=allocator,
                 overlay=Overlay(), planner=Planner(lk, lk.B0 + 1), delegates=("ops0", "ops1", "ops2"))
    venue = k.new_venue()
    rt = BookRuntime(engine=eng, venue=venue)
    alerts: list[tuple[str, str]] = []
    rec = LiveReconciler(k.sdk, k.reader, live=k.live, risk=k.risk, on_alert=lambda a, b: alerts.append((a, b)))
    runner = Runner(run_id=lk.RUN, mode=RunMode.LIVE, source=Source(snaps, on_block), journal=journal,
                    features=Features(), features_factory=Features, books=[rt], reconcile=rec,
                    on_alert=lambda a, b: alerts.append((a, b)), fault=fault)
    return runner, rt, alerts


def kinds(journal: SqliteJournal) -> list[tuple[str, Any]]:
    return [(r.kind, decode_event(r.kind, r.version, r.payload)) for r in list(journal.read())]


def test_buy_round_trip_through_the_runner(lk: ModuleType) -> None:
    k = lk.Kit()
    journal = SqliteJournal(":memory:")
    snaps = [k.snap(b) for b in range(lk.B0, lk.B0 + 7)]
    rt_holder: list[BookRuntime] = []

    def on_block(b: int) -> None:
        k.set_head(b)
        if b == lk.B0 + 3 and rt_holder:
            v = rt_holder[0].venue
            assert isinstance(v, LiveVenue)
            (key, ack), = v.acks.items()
            st, it = v.started[key], v.intents[key]
            out = lk.quote_buy(lk.DEFAULT_POOLS[lk.SN], Rao(int(it.tao_in))).amount_out
            k.sdk.set_stake(b, lk.SA, lk.SN, shares=Decimal(out), hk_alpha=lk.HK_ALPHA0 + out,
                            hk_shares=Decimal(lk.HK_ALPHA0 + out))
            k.sdk.set_account(b, lk.REAL, free=lk.REAL_FREE - int(it.tao_in))
            k.sdk.include_shielded(b, ack.carrier_hash, ack.inner_hash, lk.DELEGATES[st.delegate], int(st.nonce or 0))

    runner, rt, alerts = build(lk, k, journal, snaps, on_block)
    rt_holder.append(rt)
    run(runner.run())
    runner.close()
    assert journal.verify_chain() > 0
    evs = kinds(journal)
    order = [kd for kd, _ in evs if kd in ("order_intended", "submit_started", "venue_ack", "fill_reported")]
    assert order == ["order_intended", "submit_started", "venue_ack", "fill_reported"]
    started = next(e for kd, e in evs if kd == "submit_started")
    assert started.delegate == "ops0" and started.nonce == lk.START_NONCE and started.era_end == lk.B0 + 1 + 10
    fill = next(e for kd, e in evs if kd == "fill_reported").fill
    assert fill.block == lk.B0 + 3 and fill.tao == lk.TAO // 2
    assert [o.state for o in rt.state.orders] == [OrderState.FILLED]
    assert rt.state.orphans == 0 and not rt.state.breaches and not rt.state.recon_halt
    assert "recon_adjusted" not in [kd for kd, _ in evs], alerts
    assert len(k.sdk.submitted) == 1


def test_crash_after_submit_started_recovers_by_nonce_without_resending(lk: ModuleType) -> None:
    k = lk.Kit()
    journal = SqliteJournal(":memory:")
    snaps = [k.snap(b) for b in range(lk.B0, lk.B0 + 24)]

    class Crash(Exception):
        pass

    def fault(point: str) -> None:
        if point == "after_submit_started":
            raise Crash(point)

    runner, _, _ = build(lk, k, journal, snaps[:3], lambda b: k.set_head(b), fault=fault)
    with pytest.raises(Crash):
        run(runner.run())
    runner.close()
    assert k.sdk.submitted == []
    runner2, rt2, _ = build(lk, k, journal, snaps, lambda b: k.set_head(b))
    run(runner2.run())
    runner2.close()
    evs = kinds(journal)
    unknown = [e for kd, e in evs if kd == "submit_unknown"]
    assert [u.detail for u in unknown] == ["recovered_submitting"]
    failed = [e for kd, e in evs if kd == "order_failed"]
    assert len(failed) == 1 and failed[0].reason.value == "NotPlaced" and failed[0].tx_fee == 0
    assert [o.state for o in rt2.state.orders] == [OrderState.FAILED]
    assert k.sdk.submitted == [] and rt2.state.orphans == 0 and not rt2.state.breaches
    assert journal.verify_chain() > 0
