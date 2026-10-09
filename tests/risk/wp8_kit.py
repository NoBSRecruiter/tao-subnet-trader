"""Shared WP8 test builders: hand-built snapshots, Feats, frames, BookViews, TickContexts and RiskContexts.

Test modules cannot import each other (pytest --import-mode=importlib), so tests/risk/conftest.py and
tests/portfolio/conftest.py load this file with importlib and expose it as the `kit` fixture.

Default market (block 9,240,388, spec 475, prune possible, window open since 9,225,010, r ~ 1.48):
netuid i in 1..12, reg_at 8,000,000 + i (all non-immune), SubnetTAO 1,000 TAO, spot = SubnetMovingPrice =
0.002 * i TAO/alpha (so netuid 1 is the prune target and rank == netuid), one tracked earning validator hotkey
vhk(i) holding 30% of the pool alpha, take 0.
"""
from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

from taotrader.core.config import BookCfg, ExecCfg, RiskCfg, SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.fixed import DEC, floor_int
from taotrader.core.orders import Attribution, Fill, OrderIntent, OrderKind, OrderRecord, OrderState, Urgency
from taotrader.core.portfolio import Portfolio, Position, SleeveHolding
from taotrader.core.protocols import BookView, RiskContext, RouterState, SleeveStats, TickContext
from taotrader.core.signals import TargetBook, TargetPosition
from taotrader.core.state import ChainGlobals, ChainSnapshot, HotkeyIdx, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import (
    PERQUINTILL,
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    Mode,
    NetUid,
    OrderId,
    Ppm,
    PriceRao,
    Rao,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.core.views import EmissionView, Feat, FeatureFrame, PruneView, RouterCandidate
from taotrader.risk.liquidity import holdings

BLOCK = 9_240_388
HALF = PERQUINTILL // 2
TAO = RAO_PER_TAO
BOOK = BookId("b1")
RUN = "run1"
DELEGATES = ("sim0", "sim1", "sim2")


def hk(i: int) -> Hotkey:
    return Hotkey("0x" + f"{i:064x}")


def vhk(netuid: int, j: int = 1) -> Hotkey:
    """Validator hotkey j of a netuid."""
    return hk(netuid * 100 + j)


def key(netuid: int, reg_at: int | None = None) -> SubnetKey:
    return SubnetKey(NetUid(netuid), Block(8_000_000 + netuid if reg_at is None else reg_at))


def pool(tao: int = 1_000 * TAO, price: Decimal = Decimal("0.01"), *, fee_rate: int = 33, w_quote_e18: int = HALF,
         alpha: int | None = None) -> PoolState:
    """A pool with SubnetTAO `tao` rao and spot `price` TAO/alpha (alpha derived for the weights unless given)."""
    if alpha is None:
        w_base = PERQUINTILL - w_quote_e18
        # spot = (w_base / w_quote) * tao / alpha  ->  alpha = w_base * tao / (w_quote * spot)
        alpha = floor_int(DEC.divide(DEC.multiply(Decimal(w_base), Decimal(tao)), DEC.multiply(Decimal(w_quote_e18), price)))
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao, px_alpha=alpha,
                     w_quote_e18=w_quote_e18, fee_rate=fee_rate)


def hidx(hotkey: Hotkey, total_alpha: int, *, earns: bool = True, take_u16: int = 0, shares: Decimal | None = None,
         last_dividend: int = 0) -> HotkeyIdx:
    return HotkeyIdx(hotkey=hotkey, total_alpha=AlphaRao(total_alpha),
                     total_shares=Decimal(total_alpha) if shares is None else shares, take_u16=take_u16,
                     childkey_take_u16=0, earns=earns, last_dividend=AlphaRao(last_dividend))


def subnet(netuid: int, *, reg_at: int | None = None, tao: int = 1_000 * TAO, price: Decimal | None = None,
           ema: Decimal | None = None, hotkeys: Sequence[HotkeyIdx] | None = None, w_quote_e18: int = HALF,
           fee_rate: int = 33, **over: Any) -> SubnetState:
    p = Decimal("0.002") * netuid if price is None else price
    pl = pool(tao, p, fee_rate=fee_rate, w_quote_e18=w_quote_e18)
    k = key(netuid, reg_at)
    hks = tuple(hotkeys) if hotkeys is not None else (hidx(vhk(netuid), int(pl.alpha) * 3 // 10),)
    base = SubnetState(
        key=k, pool=pl, alpha_out=AlphaRao(int(pl.alpha) * 2), protocol_alpha=AlphaRao(int(pl.alpha) // 10),
        moving_price=p if ema is None else ema, root_prop=Decimal("0.4"), miner_burned=Decimal(0), emission_enabled=True,
        subtoken_enabled=True, reg_allowed=True, first_emission_block=Block(int(k.reg_at) + 600), tempo=360,
        last_epoch_block=Block(BLOCK - 100), ema_halving_blocks=201_600, tao_in_emission=Rao(4_000),
        excess_tao=Rao(0), alpha_out_emission=AlphaRao(1_000_000_000), alpha_in_emission=AlphaRao(3_000_000),
        hotkeys=tuple(sorted(hks, key=lambda h: h.hotkey)))
    return replace(base, **over) if over else base


def globals_(**over: Any) -> ChainGlobals:
    base = ChainGlobals(
        spec_version=475, tx_version=1, total_issuance=Rao(11_597_600 * TAO), block_emission=Rao(500_000_000),
        moving_alpha=Decimal(1_288_490) / Decimal(2**32), gate_bar=Decimal("0.0082624"), gate_rank=32, gate_exponent=3,
        tao_weight=Decimal("0.18"), root_tao=Rao(5_454_000 * TAO), owner_cut_u16=11_796, subnet_limit=12,
        immunity_period=864_000, network_rate_limit=14_400, last_reg_block=Block(9_210_610),
        last_lock_cost=Rao(653_019_955_200), min_lock_cost=Rao(TAO), lock_reduction_interval=115_200,
        tao_in_refund_block=Block(8_334_450), nominator_min_stake=Rao(20_000_000), cleanup_queue_len=0,
        n_nonroot_networks=12, safe_mode_until=None)
    return replace(base, **over) if over else base


def snapshot(block: int = BLOCK, subnets: Sequence[SubnetState] | None = None, *, plan: ReadPlan = ReadPlan.FULL,
             glob: ChainGlobals | None = None, **glob_over: Any) -> ChainSnapshot:
    subs = list(subnets) if subnets is not None else [subnet(i) for i in range(1, 13)]
    g = glob if glob is not None else globals_(**glob_over)
    return ChainSnapshot(block=Block(block), block_hash=BlockHash("0x" + f"{block:064x}"), timestamp_ms=1_759_900_000_000,
                         plan=plan, glob=g, subnets=tuple(sorted(subs, key=lambda s: int(s.key.netuid))),
                         digest=f"d{block}")


def big_market(n: int = 30, tao: int = 100_000 * TAO, block: int = BLOCK, **over: Any) -> ChainSnapshot:
    """n subnets with deep pools (ranks 16+ are outside the ladder bucket); prune possible at SubnetLimit = n."""
    subs = [subnet(i, tao=tao, **over) for i in range(1, n + 1)]
    return snapshot(block, subnets=subs, subnet_limit=n, n_nonroot_networks=n)


def candidate(hotkey: Hotkey, *, score: int = 5_000, eligible: bool = True, take_u16: int = 0,
              permit_rank: int | None = 1) -> RouterCandidate:
    return RouterCandidate(hotkey=hotkey, score_ppm_day=score, take_u16=take_u16, childkey_take_u16=0,
                           member_frac_ppm=Ppm(PPM), member_last2=True, permit_rank=permit_rank, ratio_ok=True,
                           take_increase_recent=False, eligible=eligible)


def feat(s: SubnetState, block: int = BLOCK, **over: Any) -> Feat:
    base = Feat(
        key=s.key, spot=float(s.pool.spot()), pool_tao=s.pool.tao / TAO, k_w=2.0, ret_1h=0.0, ret_1d=0.0, ret_7d=0.0,
        sigma_d=0.05, fast_ema_gap=0.0, ema_gap=0.0, flow_1h=0.0, flow_1d=0.0, flow_7d=0.0, flow_z_1d=0.0,
        emis_tao_day=10.0, chain_buy_day=1.0, obs_emis_tao_day=10.0, gate_keep=1.0, burn_adj_rank=1, ema_rank_desc=1,
        rp=0.4, sell_push_day=0.001, cb_push_day=0.002, escrow_frac=0.0, a_earn_alpha=1.0, yield_cf_gross_day=0.004,
        router_candidates=tuple(candidate(h.hotkey) for h in s.hotkeys if h.earns),
        best_candidate=next((h.hotkey for h in s.hotkeys if h.earns), None), yield_net_day=0.004,
        a_earn_growth_day=0.0, age_reg_blocks=block - int(s.key.reg_at), since_start_blocks=500_000, immune=False,
        immune_until=Block(int(s.key.reg_at) + 864_000), prune_rank=None, rho=None, t_star_stress_blocks=None,
        launch_flags=frozenset(), beta_entry_ppm=Ppm(5_000), beta_exit_ppm=Ppm(10_000), owner_sold_6h_frac=0.0,
        owner_liquid_frac=0.0, top_holder_frac=0.1)
    return replace(base, **over) if over else base


def frame(snap: ChainSnapshot, feats: Mapping[SubnetKey, Feat] | None = None, *, model_ok: bool = True,
          p_reg_day_ppm: int = 50_000, runtime_agrees: bool = True, warm: bool = True,
          feat_over: Mapping[int, Mapping[str, Any]] | None = None) -> FeatureFrame:
    fs: dict[SubnetKey, Feat] = dict(feats) if feats is not None else {}
    if feats is None:
        for s in snap.subnets:
            fs[s.key] = feat(s, int(snap.block), **dict((feat_over or {}).get(int(s.key.netuid), {})))
    pv = PruneView(prune_possible=True, target=None, runtime_agrees=runtime_agrees, ladder=(), bottom_ema=0.0,
                   blocks_since_reg=0, window_open=True, blocks_to_window=0, cost_ratio=1.5,
                   p_reg_ppm=((1_800, Ppm(p_reg_day_ppm // 4)), (7_200, Ppm(p_reg_day_ppm))), hazard_valid=True,
                   immunity_calendar=())
    ev = EmissionView(theta=0.008, gate_rank=32, sum_ema=0.5, root_flag=False, parity_err_max_tao_day=0.0,
                      model_ok=model_ok)
    return FeatureFrame(block=snap.block, warm=warm, feats=fs, prune=pv, emission=ev, regime_id="basket_trading",
                        universe_eligible=len(fs), beta_horizon_blocks=60, digest="frame")


def book_view(**over: Any) -> BookView:
    base = BookView(orders=(), recent_fills=(), chase=(), delegates_free=DELEGATES, delegate_locked_until=(),
                    fail_counts_600=(), fail_count_600_book=0, cooldowns=(), entries_halted_until=None,
                    recent_forced_exits=(), nav_liq_daily=(), sleeve_stats=(), router=RouterState(), dissolving=())
    return replace(base, **over) if over else base


def position(k: SubnetKey, hotkey: Hotkey, alpha: int, *, cost: int = 0) -> Position:
    """A position of `alpha` alpha rao at index 1 (shares == alpha)."""
    return Position(key=k, hotkey=hotkey, shares=Decimal(alpha), cost_tao=Rao(cost), opened_block=Block(BLOCK - 10_000))


def portfolio(cash: int = 1_000 * TAO, positions: Sequence[Position] = (), *, fee_float: int = TAO,
              sleeves: Sequence[SleeveHolding] | None = None,
              sleeve_cash: Sequence[tuple[str, int]] | None = None) -> Portfolio:
    pos = tuple(sorted(positions, key=lambda p: p.key))
    if sleeves is None:
        sleeves = [SleeveHolding(StrategyId("carry"), p.key, p.shares, p.cost_tao) for p in pos]
    if sleeve_cash is None:
        sleeve_cash = [("carry", cash)]
    return Portfolio(cash=Rao(cash), fee_float=Rao(fee_float), positions=pos,
                     sleeves=tuple(sorted(sleeves, key=lambda h: (h.strategy, h.key))),
                     sleeve_cash=tuple((StrategyId(s), Rao(c)) for s, c in sorted(sleeve_cash)))


_portfolio = portfolio


class MemStore:
    """In-memory SnapshotStore with the lookahead guard."""

    def __init__(self, snaps: Iterable[ChainSnapshot] = (), clock: int | None = None) -> None:
        self._s: dict[int, ChainSnapshot] = {int(s.block): s for s in snaps}
        self._blocks = sorted(self._s)
        self.clock = Block(clock if clock is not None else (self._blocks[-1] if self._blocks else 0))

    def add(self, snap: ChainSnapshot) -> None:
        self._s[int(snap.block)] = snap
        self._blocks = sorted(self._s)

    def _guard(self, block: int) -> None:
        if block > int(self.clock):
            raise LookaheadError(f"{block} > clock {self.clock}")

    def at(self, block: Block) -> ChainSnapshot:
        self._guard(int(block))
        return self._s[int(block)]

    def at_or_before(self, block: Block) -> ChainSnapshot:
        self._guard(int(block))
        i = bisect.bisect_right(self._blocks, int(block))
        if i == 0:
            raise KeyError(block)
        return self._s[self._blocks[i - 1]]

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        self._guard(int(until))
        return tuple(self._s[b] for b in self._blocks if int(until) - span_blocks < b <= int(until))


def nav_of(snap: ChainSnapshot, pf: Portfolio) -> int:
    ctx = tick(snap, portfolio=pf, nav=0)
    return int(pf.cash) + sum(int(h.value) for h in holdings(ctx).values())


def tick(snap: ChainSnapshot, *, fr: FeatureFrame | None = None, portfolio: Portfolio | None = None,
         bv: BookView | None = None, view: ChainSnapshot | None = None, prev: ChainSnapshot | None = None,
         events: Sequence[ChainEvent] = (), nav: int | None = None, store: Any = None, mode: Mode = Mode.NORMAL,
         sleeve: SleeveCfg | None = None) -> TickContext:
    pf = portfolio if portfolio is not None else _portfolio()
    st = store if store is not None else MemStore([snap], clock=int(snap.block))
    ctx = TickContext(block=snap.block, raw=snap, view=view if view is not None else snap, prev=prev, events=tuple(events),
                      frame=fr if fr is not None else frame(snap), portfolio=pf, nav_liq=Rao(0),
                      sleeve=sleeve if sleeve is not None else SleeveCfg(StrategyId("book"), Stage.PAPER, Ppm(PPM)),
                      sleeve_value=Rao(0), mode=mode, store=st, book_view=bv if bv is not None else book_view())
    if nav is None:
        nav = int(pf.cash) + sum(int(h.value) for h in holdings(ctx).values())
    return replace(ctx, nav_liq=Rao(nav))


def book_cfg(sleeves: Sequence[SleeveCfg] | None = None, *, risk: RiskCfg | None = None,
             exec_cfg: ExecCfg | None = None, book: str = "b1") -> BookCfg:
    sl = tuple(sleeves) if sleeves is not None else (SleeveCfg(StrategyId("carry"), Stage.PAPER, Ppm(1_000_000)),)
    return BookCfg(book=BookId(book), capital_rao=Rao(1_000 * TAO), fee_float_rao=Rao(TAO), sleeves=sl,
                   risk=risk if risk is not None else RiskCfg(), exec=exec_cfg if exec_cfg is not None else ExecCfg())


def risk_ctx(t: TickContext, *, cfg: RiskCfg | None = None, book: BookCfg | None = None, orphans: int = 0,
             halted: bool = False, burn_in_until: int | None = None) -> RiskContext:
    c = cfg if cfg is not None else RiskCfg()
    bk = book if book is not None else book_cfg(risk=c)
    return RiskContext(tick=t, cfg=c, book=bk, stages=tuple((s.strategy, s.stage) for s in bk.sleeves),
                       halted_by_operator=halted, orphans=orphans,
                       burn_in_until=Block(burn_in_until) if burn_in_until is not None else None)


def attribution(sid: str = "carry") -> Attribution:
    return ((StrategyId(sid), Ppm(PPM)),)


def target(k: SubnetKey, hotkey: Hotkey, value: int, *, urgency: Urgency = Urgency.NORMAL, sid: str = "carry",
           reasons: tuple[str, ...] = ("alloc",)) -> TargetPosition:
    return TargetPosition(key=k, hotkey=hotkey, value_rao=Rao(value), urgency=urgency, attribution=attribution(sid),
                          reasons=reasons)


def target_book(items: Sequence[TargetPosition], block: int = BLOCK, **over: Any) -> TargetBook:
    tb = TargetBook(asof=Block(block), items=tuple(sorted(items, key=lambda t: t.key)))
    return replace(tb, **over) if over else tb


def event(kind: ChainEventKind, block: int = BLOCK, **over: Any) -> ChainEvent:
    return ChainEvent(kind=kind, block=Block(block), **over)


def sleeve_stats(sid: str, state: str = "ACTIVE", dd_ppm: int = 0) -> SleeveStats:
    return SleeveStats(strategy=StrategyId(sid), state=state, dd_ppm=Ppm(dd_ppm), cost_ratio_20_ppm=Ppm(PPM),
                       turnover_ratio_ppm=Ppm(0), mean_45d_ppm_day=None, mean_45d_p5_ppm_day=None, days_in_state=0)


def order(k: SubnetKey, hotkey: Hotkey, kind: OrderKind, state: OrderState, *, created: int = BLOCK - 100,
          attempt: int = 0, tao_in: int = 0, alpha_in: int = 0, full: bool = False, oid: str | None = None,
          expected_out: int = 0, urgency: Urgency = Urgency.NORMAL) -> OrderRecord:
    if kind is OrderKind.ADD_STAKE_LIMIT and tao_in <= 0:
        tao_in = TAO
    if kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT) and alpha_in <= 0 and not full:
        alpha_in = TAO
    intent = OrderIntent(order_id=OrderId(oid or f"o{int(k.netuid)}_{created}_{kind.value}_{attempt}"), attempt=attempt,
                         book=BOOK, created_block=Block(created), kind=kind, key=k, hotkey=hotkey, tao_in=Rao(tao_in),
                         alpha_in=AlphaRao(alpha_in), full_position=full, limit_price=PriceRao(1), allow_partial=False,
                         shielded=True, valid_until=Block(created + 5), expected_out=expected_out, urgency=urgency,
                         attribution=attribution(), reason="test",
                         dest_hotkey=hk(9_999) if kind is OrderKind.MOVE_STAKE else None)
    return OrderRecord(intent=intent, state=state)


def fill(rec: OrderRecord, *, block: int, shortfall_ppm: int, spot_before: int, alpha: int = TAO, tao: int = TAO,
         complete: bool = True, exact_block: bool = True) -> Fill:
    i = rec.intent
    return Fill(fill_id=f"{i.order_id}:{i.attempt}:0", order_id=i.order_id, attempt=i.attempt, book=BOOK,
                block=Block(block), kind=i.kind, key=i.key, hotkey=i.hotkey, tao=Rao(tao), alpha=AlphaRao(alpha),
                shares=Decimal(alpha), swap_fee=0, author_fee_tao=Rao(0), tx_fee=Rao(0), d_pool_tao=0, d_pool_alpha=0,
                spot_before=PriceRao(spot_before), shortfall_ppm=Ppm(shortfall_ppm), complete=complete,
                exact_block=exact_block)
