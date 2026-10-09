"""taotrader/backtest/metrics.py - NAV_liq, the exact P&L decomposition and per-book statistics (WP10; DESIGN.md 8.9).

NAV. The primary series is NAV_liq in TAO: cash + the one-shot `liq_value` of every position on the book's own view
(SimVenue.mark_to: raw snapshot + own footprint), PLUS the fee float. The fee float is added (deviation from the
section 8.9 wording, recorded in the WP10 report) because tx fees, failed-order fees and carrier fees are paid from it:
excluding it would let the fee float silently absorb every tx fee (WP7 open item 1). The Engine's DecisionTrace.nav_liq
(cash + liq, pre-fill) is kept beside it as `nav_liq_engine`. NAV_spot (cash + fee float + alpha x spot) is shown only
beside it.

Exact decomposition (per tick, per position; Decimal rao, no rounding). With A = the position's ledger alpha (exactly
what the reducer holds), p = spot (TAO per alpha) of the view pool at the tick (carried from the previous tick while
the subnet is absent from the view), L = liq_value(view pool, A) (0 while DISSOLVING / absent):

    price     = A_prev * (p - p_prev)                        holdings before the tick, valued at the new spot
    yield     = sum(YieldAccrued.delta_alpha) * p            the share-price index change (epoch drains)
    swap_fee  = -(BUY swap_fee rao + SELL author_fee_tao)    the pool fee (sell side: TAO paid for the fee alpha)
    shortfall = BUY: alpha*p - (tao - swap_fee); SELL: (tao + author_fee) - alpha*p; MOVE: alpha*(p_dest - p_orig)
                execution shortfall against the spot used for the marks (impact, latency drift)
    tx_fee    = -(fill tx fees)                              paid from the fee float
    failed    = -(OrderFailed.tx_fee + CarrierFeeSettled.fee_rao)
    dereg     = payout - alpha_value * p_last                dissolution payout vs last spot
    liquidity = -(D - D_prev), D = sum(A*p - L)              change of the one-shot liquidation discount
    capital   = CapitalChanged cash + fee float deltas       flows (excluded from returns)

and the identity  NAV(t) - NAV(t-1) = price + yield + swap_fee + shortfall + tx_fee + failed + dereg + liquidity
+ capital  holds exactly; `TickPoint.identity_error` reports |difference| and the integration test asserts <= 1 rao
per tick. Per-netuid contributions (everything except failed fees and capital) feed the per-subnet table and the
leave-top-3-subnets-out statistic.

Daily statistics: NAV_liq resampled to UTC days (last point of each day by snapshot timestamp), flow-adjusted daily
returns r_d = (NAV_d - NAV_{d-1} - F_d) / (NAV_{d-1} + F_d); max drawdown, turnover, fee drag, hit rate, average hold,
time in cash. Inference (NW t, bootstrap, deflated Sharpe, CSCV) lives in backtest.stats.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final

from ..core.events import (
    CapitalChanged,
    CarrierFeeSettled,
    DecisionTrace,
    DeregSettled,
    FillReported,
    JournalEvent,
    OrderFailed,
    OrderIntended,
    YieldAccrued,
)
from ..core.fixed import DEC
from ..core.orders import FailReason, OrderKind
from ..core.portfolio import alpha_unit, pos_account
from ..core.protocols import Journal
from ..core.state import ChainSnapshot
from ..core.units import RAO_PER_TAO, AlphaRao, PositionKey, SubnetKey
from ..data.journal import decode_record
from ..engine.reducer import EngineState, ledger_dict
from ..protocol.amm import liq_value

__all__ = [
    "COMPONENTS", "BookMetrics", "Components", "DayPoint", "NavRecorder", "TickPoint", "TradeStats", "book_metrics",
    "daily_points", "daily_returns", "leave_top_out", "max_drawdown", "per_netuid_totals",
]

ZERO: Final[Decimal] = Decimal(0)
MS_PER_DAY: Final[int] = 86_400_000
COMPONENTS: Final[tuple[str, ...]] = ("price", "yield_", "swap_fee", "shortfall", "tx_fee", "failed", "dereg",
                                      "liquidity", "capital")


def _d(x: int) -> Decimal:
    return Decimal(x)


@dataclass(frozen=True, slots=True)
class Components:
    """Signed contributions to the NAV change of one tick, in rao (exact Decimal)."""
    price: Decimal = ZERO
    yield_: Decimal = ZERO
    swap_fee: Decimal = ZERO
    shortfall: Decimal = ZERO
    tx_fee: Decimal = ZERO
    failed: Decimal = ZERO
    dereg: Decimal = ZERO
    liquidity: Decimal = ZERO
    capital: Decimal = ZERO

    def total(self) -> Decimal:
        out = ZERO
        for name in COMPONENTS:
            out = DEC.add(out, getattr(self, name))
        return out

    def plus(self, o: Components) -> Components:
        return Components(**{n: DEC.add(getattr(self, n), getattr(o, n)) for n in COMPONENTS})

    def as_dict(self) -> dict[str, Decimal]:
        return {n: getattr(self, n) for n in COMPONENTS}


@dataclass(frozen=True, slots=True)
class TickPoint:
    block: int
    ts_ms: int
    nav: int                    # NAV_liq incl. fee float (rao)
    nav_spot: Decimal           # cash + fee float + sum(alpha * spot)
    nav_liq_engine: int         # cash + liq (the Engine's definition, end of tick)
    cash: int
    fee_float: int
    gross_liq: int              # sum of liq_value of the positions
    n_positions: int
    comps: Components
    identity_error: Decimal     # |delta NAV - comps.total()|
    flows: int                  # capital flows of the tick (rao)
    traded_tao: int             # gross TAO of the tick's buy and sell fills
    per_netuid: tuple[tuple[int, Decimal], ...] = ()   # contribution per netuid (price..liquidity, tx fees of fills)
    mode: str = ""


@dataclass(frozen=True, slots=True)
class TradeStats:
    fills: int = 0
    buys: int = 0
    sells: int = 0
    moves: int = 0
    failures: int = 0
    failures_exact: int = 0     # exact_block failures (per-block outcomes; section 3.12 / FT7)
    failures_stride: int = 0    # stride-evaluated failures (exact_block = False), reported separately
    limit_failures_exact: int = 0
    limit_failures_stride: int = 0
    fills_exact: int = 0
    fills_stride: int = 0
    hold_blocks: tuple[int, ...] = ()           # completed holds (first buy -> flat)
    fill_latency_blocks: tuple[int, ...] = ()   # fill block - decision block (effective-latency distribution)


@dataclass(slots=True)
class _Pos:
    alpha: int
    price: Decimal
    liq: int


@dataclass(slots=True)
class _BookTrack:
    pos: dict[PositionKey, _Pos] = field(default_factory=dict)
    cash: int = 0
    fee_float: int = 0
    nav: int = 0
    points: list[TickPoint] = field(default_factory=list)
    opened: dict[SubnetKey, int] = field(default_factory=dict)
    holds: list[int] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    latencies: list[int] = field(default_factory=list)
    intents: dict[tuple[str, int], int] = field(default_factory=dict)
    mode: str = ""


_LIMIT_REASONS: Final[frozenset[FailReason]] = frozenset({FailReason.PRICE_LIMIT_EXCEEDED, FailReason.SLIPPAGE_TOO_HIGH})


class NavRecorder:
    """Observer for backtest.runner: after every tick it reads the journal records committed since the previous tick and
    values every book on its own view. One instance per pass (book states and views come from the Runner)."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._seq = int(journal.head()[0])
        self._books: dict[str, _BookTrack] = {}

    def track(self, book: str) -> _BookTrack:
        t = self._books.get(book)
        if t is None:
            t = self._books[book] = _BookTrack()
        return t

    def points(self, book: str) -> list[TickPoint]:
        return list(self.track(book).points)

    def trade_stats(self, book: str) -> TradeStats:
        t = self.track(book)
        s = t.stats
        return TradeStats(fills=s.get("fills", 0), buys=s.get("buys", 0), sells=s.get("sells", 0), moves=s.get("moves", 0),
                          failures=s.get("failures", 0), failures_exact=s.get("failures_exact", 0),
                          failures_stride=s.get("failures_stride", 0), limit_failures_exact=s.get("limit_exact", 0),
                          limit_failures_stride=s.get("limit_stride", 0), fills_exact=s.get("fills_exact", 0),
                          fills_stride=s.get("fills_stride", 0), hold_blocks=tuple(t.holds),
                          fill_latency_blocks=tuple(t.latencies))

    def books(self) -> tuple[str, ...]:
        return tuple(sorted(self._books))

    def observe(self, snap: ChainSnapshot, books: Iterable[tuple[str, EngineState, ChainSnapshot]]) -> None:
        """Record one tick: `books` = (book id, folded EngineState, the book's view after the tick)."""
        events: dict[str, list[JournalEvent]] = {}
        last = self._seq
        for rec in self.journal.read(self._seq + 1):
            last = rec.seq
            ev = decode_record(rec)
            events.setdefault(str(rec.book), []).append(ev)
        self._seq = last
        for book, state, view in books:
            self._observe_book(book, state, view, snap, events.get(book, []))

    # ------------------------------------------------------------------ one book, one tick
    def _observe_book(self, book: str, state: EngineState, view: ChainSnapshot, snap: ChainSnapshot,
                      evs: Sequence[JournalEvent]) -> None:
        t = self.track(book)
        prev = t.pos
        price_now: dict[SubnetKey, Decimal] = {}

        def spot(key: SubnetKey) -> Decimal:
            if key in price_now:
                return price_now[key]
            s = view.get(key)
            if s is not None:
                try:
                    p = s.pool.spot()
                except ArithmeticError:
                    p = ZERO
            else:
                p = next((v.price for pk, v in prev.items() if pk.subnet == key), ZERO)
            price_now[key] = p
            return p

        per: dict[int, Decimal] = {}

        def credit(netuid: int, v: Decimal) -> None:
            per[netuid] = DEC.add(per.get(netuid, ZERO), v)

        price = yld = swap = short = tx = failed = dereg = capital = ZERO
        flows = traded = 0
        for pk, pv in prev.items():
            d = DEC.multiply(_d(pv.alpha), DEC.subtract(spot(pk.subnet), pv.price))
            price = DEC.add(price, d)
            credit(int(pk.subnet.netuid), d)
        for ev in evs:
            if isinstance(ev, YieldAccrued):
                v = DEC.multiply(_d(ev.delta_alpha), spot(ev.key))
                yld = DEC.add(yld, v)
                credit(int(ev.key.netuid), v)
            elif isinstance(ev, FillReported):
                f = ev.fill
                p = spot(f.key)
                t.stats["fills"] = t.stats.get("fills", 0) + 1
                t.stats["fills_exact" if f.exact_block else "fills_stride"] = t.stats.get(
                    "fills_exact" if f.exact_block else "fills_stride", 0) + 1
                dec_block = t.intents.get((str(f.order_id), int(f.attempt)))
                if dec_block is not None:
                    t.latencies.append(int(f.block) - dec_block)
                if f.kind is OrderKind.ADD_STAKE_LIMIT:
                    t.stats["buys"] = t.stats.get("buys", 0) + 1
                    fee = _d(f.swap_fee)
                    s_ = DEC.subtract(DEC.multiply(_d(f.alpha), p), DEC.subtract(_d(f.tao), fee))
                    swap, short = DEC.subtract(swap, fee), DEC.add(short, s_)
                    credit(int(f.key.netuid), DEC.subtract(s_, fee))
                    traded += int(f.tao)
                    t.opened.setdefault(f.key, int(f.block))
                elif f.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
                    t.stats["sells"] = t.stats.get("sells", 0) + 1
                    fee = _d(f.author_fee_tao)
                    s_ = DEC.subtract(DEC.add(_d(f.tao), fee), DEC.multiply(_d(f.alpha), p))
                    swap, short = DEC.subtract(swap, fee), DEC.add(short, s_)
                    credit(int(f.key.netuid), DEC.subtract(s_, fee))
                    traded += int(f.tao)
                else:
                    t.stats["moves"] = t.stats.get("moves", 0) + 1
                    dest = f.dest_key if f.dest_key is not None else f.key
                    s_ = DEC.multiply(_d(f.alpha), DEC.subtract(spot(dest), p))
                    short = DEC.add(short, s_)
                    credit(int(f.key.netuid), s_)
                if f.tx_fee:
                    tx = DEC.subtract(tx, _d(f.tx_fee))
                    credit(int(f.key.netuid), -_d(f.tx_fee))
            elif isinstance(ev, OrderFailed):
                failed = DEC.subtract(failed, _d(ev.tx_fee))
                if ev.reason not in (FailReason.VENUE_REJECT, FailReason.NOT_PLACED):
                    t.stats["failures"] = t.stats.get("failures", 0) + 1
                    t.stats["failures_exact" if ev.exact_block else "failures_stride"] = t.stats.get(
                        "failures_exact" if ev.exact_block else "failures_stride", 0) + 1
                    if ev.reason in _LIMIT_REASONS:
                        k = "limit_exact" if ev.exact_block else "limit_stride"
                        t.stats[k] = t.stats.get(k, 0) + 1
            elif isinstance(ev, CarrierFeeSettled):
                failed = DEC.subtract(failed, _d(ev.fee_rao))
            elif isinstance(ev, DeregSettled):
                v = DEC.subtract(_d(ev.payout_tao), DEC.multiply(_d(ev.alpha_value), spot(ev.key)))
                dereg = DEC.add(dereg, v)
                credit(int(ev.key.netuid), v)
            elif isinstance(ev, CapitalChanged):
                capital = DEC.add(capital, _d(ev.cash_delta + ev.fee_float_delta))
                flows += ev.cash_delta + ev.fee_float_delta
            elif isinstance(ev, DecisionTrace):
                t.mode = ev.mode.name
            elif isinstance(ev, OrderIntended):
                it = ev.intent
                t.intents[(str(it.order_id), int(it.attempt))] = int(it.created_block)
        # ---- end-of-tick valuation
        ledger = ledger_dict(state)
        cur: dict[PositionKey, _Pos] = {}
        disc_prev = ZERO
        for pv in prev.values():
            disc_prev = DEC.add(disc_prev, DEC.subtract(DEC.multiply(_d(pv.alpha), pv.price), _d(pv.liq)))
        disc = ZERO
        gross_liq = 0
        held_keys: set[SubnetKey] = set()
        for pos in state.portfolio.positions:
            alpha = int(ledger.get((pos_account(pos.key, pos.hotkey), alpha_unit(pos.key)), 0))
            p = spot(pos.key)
            s = view.get(pos.key)
            liq = 0
            if s is not None and pos.key not in state.dissolving and alpha > 0:
                liq = int(liq_value(s.pool, AlphaRao(alpha)))
            cur[pos.pkey] = _Pos(alpha, p, liq)
            gross_liq += liq
            disc = DEC.add(disc, DEC.subtract(DEC.multiply(_d(alpha), p), _d(liq)))
            held_keys.add(pos.key)
        liq_c = DEC.subtract(disc_prev, disc)
        # per-netuid liquidity contribution
        by_net_prev: dict[int, Decimal] = {}
        for pk, pv in prev.items():
            n = int(pk.subnet.netuid)
            by_net_prev[n] = DEC.add(by_net_prev.get(n, ZERO), DEC.subtract(DEC.multiply(_d(pv.alpha), pv.price), _d(pv.liq)))
        by_net_cur: dict[int, Decimal] = {}
        for pk, cv in cur.items():
            n = int(pk.subnet.netuid)
            by_net_cur[n] = DEC.add(by_net_cur.get(n, ZERO), DEC.subtract(DEC.multiply(_d(cv.alpha), cv.price), _d(cv.liq)))
        for n in sorted(set(by_net_prev) | set(by_net_cur)):
            credit(n, DEC.subtract(by_net_prev.get(n, ZERO), by_net_cur.get(n, ZERO)))
        # holds: a key that was held and is now flat closes a hold
        for key in sorted({pk.subnet for pk in prev} - held_keys, key=lambda k: (k.netuid, k.reg_at)):
            start = t.opened.pop(key, None)
            if start is not None:
                t.holds.append(int(snap.block) - start)
        cash, ff = int(state.portfolio.cash), int(state.portfolio.fee_float)
        nav = cash + ff + gross_liq
        spot_val = ZERO
        for cv in cur.values():
            spot_val = DEC.add(spot_val, DEC.multiply(_d(cv.alpha), cv.price))
        comps = Components(price=price, yield_=yld, swap_fee=swap, shortfall=short, tx_fee=tx, failed=failed, dereg=dereg,
                           liquidity=liq_c, capital=capital)
        err = abs(DEC.subtract(_d(nav - t.nav), comps.total()))
        t.points.append(TickPoint(block=int(snap.block), ts_ms=int(snap.timestamp_ms), nav=nav,
                                  nav_spot=DEC.add(_d(cash + ff), spot_val), nav_liq_engine=cash + gross_liq, cash=cash,
                                  fee_float=ff, gross_liq=gross_liq, n_positions=len(cur), comps=comps,
                                  identity_error=err, flows=flows, traded_tao=traded,
                                  per_netuid=tuple(sorted(per.items())), mode=t.mode))
        t.pos, t.cash, t.fee_float, t.nav = cur, cash, ff, nav


# ------------------------------------------------------------------------------------------------ daily series
@dataclass(frozen=True, slots=True)
class DayPoint:
    day: int                  # UTC day number (ts_ms // 86,400,000)
    block: int                # last block of the day
    nav: int
    flows: int
    traded_tao: int
    gross_liq: int


def daily_points(points: Sequence[TickPoint]) -> list[DayPoint]:
    """Last point of each UTC day (by snapshot timestamp); flows and traded TAO summed over the day."""
    out: list[DayPoint] = []
    for p in points:
        day = p.ts_ms // MS_PER_DAY
        if out and out[-1].day == day:
            last = out[-1]
            out[-1] = DayPoint(day, p.block, p.nav, last.flows + p.flows, last.traded_tao + p.traded_tao, p.gross_liq)
        else:
            out.append(DayPoint(day, p.block, p.nav, p.flows, p.traded_tao, p.gross_liq))
    return out


def daily_returns(days: Sequence[DayPoint]) -> list[float]:
    """Flow-adjusted daily returns r_d = (NAV_d - NAV_{d-1} - F_d) / (NAV_{d-1} + F_d) (NAV_{-1} = 0)."""
    out: list[float] = []
    prev = 0
    for d in days:
        base = prev + d.flows
        if base > 0:
            out.append((d.nav - prev - d.flows) / base)
        prev = d.nav
    return out


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a fraction of the peak (0 for a monotone series)."""
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def per_netuid_totals(points: Sequence[TickPoint]) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for p in points:
        for n, v in p.per_netuid:
            out[n] = DEC.add(out.get(n, ZERO), v)
    return out


def leave_top_out(points: Sequence[TickPoint], k: int = 3) -> tuple[tuple[int, ...], list[float]]:
    """Daily returns with the k netuids of largest total contribution removed from every tick's NAV change."""
    tot = per_netuid_totals(points)
    top = tuple(n for n, _ in sorted(tot.items(), key=lambda kv: (-kv[1], kv[0]))[:k])
    adj: list[TickPoint] = []
    shift = ZERO
    for p in points:
        for n, v in p.per_netuid:
            if n in top:
                shift = DEC.add(shift, v)
        adj.append(TickPoint(p.block, p.ts_ms, p.nav - int(shift), p.nav_spot, p.nav_liq_engine, p.cash, p.fee_float,
                             p.gross_liq, p.n_positions, p.comps, p.identity_error, p.flows, p.traded_tao))
    return top, daily_returns(daily_points(adj))


# ------------------------------------------------------------------------------------------------ book summary
@dataclass(frozen=True, slots=True)
class BookMetrics:
    book: str
    ticks: int
    days: int
    start_nav_tao: float
    end_nav_tao: float
    mean_daily_net_pct: float
    total_return_pct: float
    max_drawdown_pct: float
    turnover_per_day_pct: float
    fee_drag_pct: float                 # fees (swap + tx + failed) / mean NAV over the run
    hit_rate_pct: float                 # positive days / days
    avg_hold_days: float | None
    time_in_cash_pct: float
    components_tao: Mapping[str, float]
    max_identity_error_rao: float
    trades: TradeStats
    daily_returns: tuple[float, ...]
    per_netuid_tao: Mapping[int, float]
    leave_top3_out_mean_pct: float | None
    top3: tuple[int, ...]
    extra: Mapping[str, Any] = field(default_factory=dict)


def _tao(x: Decimal | int) -> float:
    return float(Decimal(x) / Decimal(RAO_PER_TAO))


def book_metrics(book: str, points: Sequence[TickPoint], trades: TradeStats | None = None) -> BookMetrics:
    days = daily_points(points)
    rets = daily_returns(days)
    comps = Components()
    for p in points:
        comps = comps.plus(p.comps)
    navs = [float(p.nav) for p in points]
    mean_nav = sum(navs) / len(navs) if navs else 0.0
    fees = -(comps.swap_fee + comps.tx_fee + comps.failed)
    traded = sum(p.traded_tao for p in points)
    n_days = max(len(days), 1)
    in_cash = sum(1 for p in points if p.n_positions == 0)
    tr = trades if trades is not None else TradeStats()
    holds = tr.hold_blocks
    top, loo = leave_top_out(points, 3) if points else ((), [])
    total_ret = 1.0
    for r in rets:
        total_ret *= 1.0 + r
    return BookMetrics(
        book=book, ticks=len(points), days=len(days),
        start_nav_tao=_tao(points[0].nav) if points else 0.0, end_nav_tao=_tao(points[-1].nav) if points else 0.0,
        mean_daily_net_pct=100.0 * sum(rets) / len(rets) if rets else 0.0,
        total_return_pct=100.0 * (total_ret - 1.0),
        max_drawdown_pct=100.0 * max_drawdown(navs),
        turnover_per_day_pct=100.0 * traded / mean_nav / n_days if mean_nav > 0 else 0.0,
        fee_drag_pct=100.0 * float(fees) / mean_nav if mean_nav > 0 else 0.0,
        hit_rate_pct=100.0 * sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0,
        avg_hold_days=(sum(holds) / len(holds) / 7_200.0) if holds else None,
        time_in_cash_pct=100.0 * in_cash / len(points) if points else 0.0,
        components_tao={k: _tao(v) for k, v in comps.as_dict().items()},
        max_identity_error_rao=float(max((p.identity_error for p in points), default=ZERO)),
        trades=tr, daily_returns=tuple(rets),
        per_netuid_tao={n: _tao(v) for n, v in sorted(per_netuid_totals(points).items())},
        leave_top3_out_mean_pct=100.0 * sum(loo) / len(loo) if loo else None, top3=top)


def capacity_at_half_edge(points: Sequence[tuple[float, float]]) -> float | None:
    """Section 8.9 capacity sweep: given (capital TAO, mean net edge %/day) at increasing capital, the capital at which
    the net edge has fallen to half of the smallest-capital edge (linear interpolation between sweep points). None when
    the base edge is not positive or the edge never halves within the sweep."""
    pts = sorted(points)
    if not pts or pts[0][1] <= 0:
        return None
    half = pts[0][1] / 2
    for (c0, e0), (c1, e1) in pairwise(pts):
        if e1 <= half:
            if e0 == e1:
                return c1
            return c0 + (e0 - half) * (c1 - c0) / (e0 - e1)
    return None
