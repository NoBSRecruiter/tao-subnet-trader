"""taotrader/risk/liquidity.py - the CapsFn (DESIGN.md 3.5 V_cap), the section I portfolio aggregates and the shared
book valuation helpers (WP8).

Valuation (every WP8 rule uses these, on `ctx.view`: the market plus this book's own footprint, == raw live):
- a position's alpha is HotkeyIdx.value_of(shares) at the view's index of its hotkey. When the hotkey is not tracked
  in the view (it always should be: held hotkeys are tracked), floor(shares) is used: the share index starts at 1 and
  only rises, so this is a lower bound (flagged `indexed=False`);
- its value is the one-shot executable value protocol.amm.liq_value (0 if the pool is gone or the sell would fail).

V_cap (per subnet; the CapsFn runs before the allocator, section 4.4):
    T_st  = min(T_now, min T over 3 d) * (1 - D_T)                 T = SubnetTAO; 3 d sampled on an absolute 300-block
                                                                   grid of the SnapshotStore (shared by consecutive ticks)
    base  = T_st * s / (1 - s), s = S_EXIT_ENTRY (1.5%)            weights within 0.5 +- 0.01; otherwise the largest V
                                                                   whose one-shot sell on the T_st-scaled pool has
                                                                   shortfall <= s incl. the fee (size-free bisection
                                                                   on the quote_sell formula, `_skewed_base`)
    V_cap = min(base * m_esc * m_owner_event * [m_owner if owner_haircut_active], NU_MAX * NAV_liq)
    m_esc = clamp(1 - E/x, 0.5, 1) (escrow E = SubnetState.escrow_alpha, None -> 0; x = SubnetAlphaIn)
    m_owner_event = 0.5 while an owner-event cooldown is active (section 3.6: halve V_cap for 1 day)
The MONITOR multipliers m_gate (1 / 0.5 / 0.25 at gate keep >= 0.5 / >= 0.1 / below) and m_trd (0.5 when the
total-return drift TRD = yield + cb_push - sell_push < -0.25%/day) are computed here (`monitored`) but applied by the
allocator, and only to sleeves with declares_dilution = False, when gate_haircuts_active (section 3.1 item 4).

Section I aggregates (per book; applied by the overlay as the final authority and by the allocator constructively):
gross alpha <= G_MAX_EFF * NAV_liq; ladder bucket (prune rank <= 15) <= 15%; owner-coldkey cluster <= 20%;
young (since_start < 30 d) <= 10%; LCW <= 5%; N_eff = min(n_max, floor(G_MAX_EFF * NAV_liq / (4 * V_MIN))) names;
hold budget: ES = one-shot exit shortfall of the target value; ES > S_EXIT_HOLD_MAX -> trim to V_cap at T_now;
sum ES * V <= exit_budget * NAV_liq, trimming the largest ES first. An over-full bucket is cut from INCREASES first
(pro rata), then from holdings with the largest exit shortfall first.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Context, Decimal
from typing import TYPE_CHECKING, Final

from ..core.config import RiskCfg
from ..core.errors import LookaheadError
from ..core.fixed import DEC, ONE, floor_int, to_ppm
from ..core.orders import Attribution
from ..core.portfolio import Portfolio
from ..core.protocols import BookView, SnapshotStore, TickContext
from ..core.signals import RiskAction
from ..core.state import ChainSnapshot, PoolState, SubnetState
from ..core.units import (
    BLOCKS_PER_DAY,
    FEE_DEN,
    PERQUINTILL,
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    Hotkey,
    Ppm,
    Rao,
    StrategyId,
    SubnetKey,
)
from ..core.views import Feat
from ..protocol.amm import SwapError, liq_value, quote_sell, spot_rao_exact, v_max
from ..protocol.prune import ladder
from .owner_guard import m_owner_ppm, owner_cooldown_active

__all__ = [
    "BOOK_SLEEVE",
    "LADDER_BUCKET_RANK",
    "LCW_PREFIX",
    "T_ST_GRID_BLOCKS",
    "T_ST_LOOKBACK_BLOCKS",
    "YOUNG_SINCE_START_BLOCKS",
    "AggregateLimits",
    "CapDetail",
    "Caps",
    "Holding",
    "Monitored",
    "aggregate_limits",
    "apply_aggregates",
    "attribution_from_weights",
    "caps",
    "exit_shortfall_ppm",
    "holding_attribution",
    "holdings",
    "is_lcw",
    "ladder_ranks",
    "monitored",
    "pool_spot_rao",
    "sleeve_values",
    "t_history",
    "value_to_alpha",
    "vcap_detail",
]

T_ST_LOOKBACK_BLOCKS: Final[int] = 3 * BLOCKS_PER_DAY       # min T over 3 d
T_ST_GRID_BLOCKS: Final[int] = 300
SKEW_BAND_E18: Final[int] = PERQUINTILL // 100              # |w_q - 0.5| > 0.01 -> bisection on quote_sell
HALF_E18: Final[int] = PERQUINTILL // 2
YOUNG_SINCE_START_BLOCKS: Final[int] = 30 * BLOCKS_PER_DAY
LADDER_BUCKET_RANK: Final[int] = 15
LCW_BUCKET_PPM: Final[int] = 50_000
LCW_PREFIX: Final[str] = "lcw"
BOOK_SLEEVE: Final[StrategyId] = StrategyId("book")         # engine.reducer's pseudo-sleeve for sleeve-less holdings
M_ESC_MIN_PPM: Final[int] = 500_000
OWNER_EVENT_CAP_PPM: Final[int] = 500_000
M_GATE_HALF_KEEP_PPM: Final[int] = 500_000
M_GATE_TENTH_KEEP_PPM: Final[int] = 100_000
TRD_FLOOR_PPM_DAY: Final[int] = -2_500                      # -0.25 %/day
_BISECT_STEPS: Final[int] = 64


# ------------------------------------------------------------------------------------------------- valuation
@dataclass(frozen=True, slots=True)
class Holding:
    key: SubnetKey
    hotkey: Hotkey
    shares: Decimal
    alpha: AlphaRao            # value_of(shares) at the view's index (floor(shares) when untracked: a lower bound)
    value: Rao                 # one-shot executable value on the view (0 when the pool is gone or unsellable)
    indexed: bool              # the hotkey index was found in the view


def holdings(ctx: TickContext) -> dict[SubnetKey, Holding]:
    """Every position of the book valued on ctx.view (one Position per generation, section 5.7 invariant 5)."""
    out: dict[SubnetKey, Holding] = {}
    for p in ctx.portfolio.positions:
        s = ctx.view.get(p.key)
        idx = s.hotkey(p.hotkey) if s is not None else None
        if idx is not None:
            alpha = idx.value_of(p.shares)
        else:
            alpha = AlphaRao(floor_int(p.shares))
        value = liq_value(s.pool, alpha) if s is not None else Rao(0)
        out[p.key] = Holding(p.key, p.hotkey, p.shares, alpha, value, idx is not None)
    return out


def sleeve_values(portfolio: Portfolio, hold: Mapping[SubnetKey, Holding]) -> dict[tuple[StrategyId, SubnetKey], int]:
    """Each sleeve's share of each position's executable value (pro rata to shares, floored)."""
    out: dict[tuple[StrategyId, SubnetKey], int] = {}
    for sh in portfolio.sleeves:
        h = hold.get(sh.key)
        if h is None or h.shares <= 0 or sh.shares <= 0:
            continue
        out[(sh.strategy, sh.key)] = floor_int(DEC.divide(DEC.multiply(Decimal(int(h.value)), sh.shares), h.shares))
    return out


def attribution_from_weights(weights: Sequence[tuple[StrategyId, int]]) -> Attribution:
    """ppm attribution (sums to 1e6 exactly) from non-negative integer weights; the remainder goes to the largest
    weight (ties: lowest id). Empty or all-zero weights -> the book pseudo-sleeve."""
    merged: dict[StrategyId, int] = {}
    for sid, w in weights:
        if w > 0:
            merged[sid] = merged.get(sid, 0) + int(w)
    total = sum(merged.values())
    if total <= 0:
        return ((BOOK_SLEEVE, Ppm(PPM)),)
    items = sorted(merged.items())
    parts = {sid: w * PPM // total for sid, w in items}
    rest = PPM - sum(parts.values())
    top = min(items, key=lambda kv: (-kv[1], kv[0]))[0]
    parts[top] += rest
    return tuple((sid, Ppm(parts[sid])) for sid, _ in items if parts[sid] > 0)


def holding_attribution(portfolio: Portfolio, key: SubnetKey) -> Attribution:
    """Attribution of the physical position on `key` by sleeve shares (the pseudo-sleeve when no sleeve holds it)."""
    total = Decimal(0)
    rows = [(h.strategy, h.shares) for h in portfolio.sleeves if h.key == key and h.shares > 0]
    for _, sh in rows:
        total = DEC.add(total, sh)
    if total <= 0:
        return ((BOOK_SLEEVE, Ppm(PPM)),)
    return attribution_from_weights([(sid, floor_int(DEC.divide(DEC.multiply(sh, Decimal(PPM)), total))) for sid, sh in rows])


def is_lcw(sid: StrategyId) -> bool:
    return sid == LCW_PREFIX or sid.startswith((LCW_PREFIX + ".", LCW_PREFIX + "_"))


def lcw_share_ppm(attribution: Attribution) -> int:
    return sum(int(p) for sid, p in attribution if is_lcw(sid))


def pool_spot_rao(pool: PoolState) -> int:
    """Floored spot in rao per alpha (0 for an empty pool)."""
    if pool.px_tao <= 0 or pool.px_alpha <= 0:
        return 0
    return int(pool.spot_rao())


def value_to_alpha(pool: PoolState, value_rao: int) -> int:
    """Alpha worth `value_rao` at spot (floored; 0 for an empty pool)."""
    if value_rao <= 0 or pool.px_tao <= 0 or pool.px_alpha <= 0:
        return 0
    return floor_int(DEC.divide(DEC.multiply(Decimal(value_rao), Decimal(RAO_PER_TAO)), spot_rao_exact(pool)))


def exit_shortfall_ppm(pool: PoolState, value_rao: int) -> int:
    """ES of a one-shot exit of `value_rao` (TAO at spot): 1 - liq/(alpha*spot), ppm; 1e6 when it cannot be sold."""
    alpha = value_to_alpha(pool, value_rao)
    if alpha <= 0:
        return 0
    try:
        return max(0, int(quote_sell(pool, AlphaRao(alpha)).shortfall_ppm))
    except SwapError:
        return PPM


def ladder_ranks(raw: ChainSnapshot) -> dict[SubnetKey, int]:
    """Non-immune prune rank of every generation (1 = target); immune generations are absent."""
    return {k: i + 1 for i, k in enumerate(ladder(raw))}


# ------------------------------------------------------------------------------------------------- V_cap
def t_history(store: SnapshotStore | None, raw: ChainSnapshot, lookback: int = T_ST_LOOKBACK_BLOCKS,
              grid: int = T_ST_GRID_BLOCKS) -> list[ChainSnapshot]:
    """Snapshots on the absolute `grid` of (block - lookback, block) from the bounded store (missing ones skipped)."""
    if store is None:
        return []
    b = int(raw.block)
    seen: dict[int, ChainSnapshot] = {}
    g = (b // grid) * grid
    if g == b:
        g -= grid
    while g > b - lookback:
        try:
            snap = store.at_or_before(Block(g))
        except (KeyError, LookaheadError, ValueError):
            break
        if int(snap.block) <= b - lookback:
            break
        seen[int(snap.block)] = snap
        g -= grid
    return [seen[k] for k in sorted(seen)]


@dataclass(frozen=True, slots=True)
class Monitored:
    """MONITOR-only multipliers (ppm) and the inputs that produced them."""
    m_gate_ppm: int
    m_trd_ppm: int
    m_owner_ppm: int
    gate_keep_ppm: int | None
    trd_ppm_day: int | None


def monitored(feat: Feat | None, cfg: RiskCfg) -> Monitored:
    if feat is None:
        return Monitored(PPM, PPM, PPM, None, None)
    keep = to_ppm(feat.gate_keep)
    m_gate = PPM if keep >= M_GATE_HALF_KEEP_PPM else 500_000 if keep >= M_GATE_TENTH_KEEP_PPM else 250_000
    y = feat.yield_net_day if feat.yield_net_day is not None else feat.yield_cf_gross_day
    trd = to_ppm(y) + to_ppm(feat.cb_push_day) - to_ppm(feat.sell_push_day)
    m_trd = 500_000 if trd < TRD_FLOOR_PPM_DAY else PPM
    return Monitored(m_gate, m_trd, m_owner_ppm(feat, cfg), keep, trd)


@dataclass(frozen=True, slots=True)
class CapDetail:
    key: SubnetKey
    t_now: Rao
    t_min_3d: Rao
    t_st: Rao
    base: Rao                     # T_st * s / (1 - s) (or the skewed-weight bisection)
    m_esc_ppm: int
    m_owner_event_ppm: int        # 0.5 while an owner-event cooldown is active
    m_owner_ppm: int              # MONITOR haircut (applied only when owner_haircut_active)
    nu_cap: Rao                   # NU_MAX * NAV_liq
    cap: Rao


_SOLVE: Final[Context] = Context(prec=34, rounding=ROUND_FLOOR)   # fixed context: identical on every OS
_SOLVE_TOL: Final[Decimal] = Decimal("1e-12")


def _sell_keep(u: Decimal, r: Decimal, c: Decimal) -> tuple[Decimal, Decimal]:
    """h(u) = executed / spot value of a one-shot sell of u = alpha / x (fee factor c = 1 - f, r = w_base / w_quote):
    (1 - (1 + u c)^(-r)) / (r u), decreasing from c (u -> 0) to 0; and its derivative h'(u)."""
    q = _SOLVE.add(ONE, _SOLVE.multiply(u, c))
    p = _SOLVE.power(q, _SOLVE.minus(r))
    one_minus = _SOLVE.subtract(ONE, p)
    h = _SOLVE.divide(one_minus, _SOLVE.multiply(r, u))
    num = _SOLVE.subtract(_SOLVE.divide(_SOLVE.multiply(_SOLVE.multiply(_SOLVE.multiply(r, c), p), u), q), one_minus)
    dh = _SOLVE.divide(num, _SOLVE.multiply(r, _SOLVE.multiply(u, u)))
    return h, dh


def _skewed_base(pool: PoolState, t_st: int, s_ppm: int) -> int:
    """Largest V (TAO at spot) whose one-shot sell on the pool scaled to T_st has shortfall <= s, incl. the swap fee.

    The shortfall of selling u = alpha / x of the reserve depends only on u, r = w_base / w_quote and the fee, and
    V = u * r * y (y = the scaled pricing TAO reserve), so the solve is size-free: a bracketed (safeguarded) Newton
    iteration on h(u) = 1 - s in a fixed 34-digit Decimal context, to a relative width of 1e-12. The returned point is
    the bracket's feasible end, floored and nudged down 1 ppb, so the cap never exceeds the budget."""
    if pool.tao <= 0 or t_st <= 0 or pool.px_tao <= 0 or pool.px_alpha <= 0:
        return 0
    y = DEC.divide(DEC.multiply(Decimal(pool.px_tao), Decimal(t_st)), Decimal(int(pool.tao)))
    r = _SOLVE.divide(Decimal(PERQUINTILL - pool.w_quote_e18), Decimal(pool.w_quote_e18))
    c = _SOLVE.subtract(ONE, _SOLVE.divide(Decimal(pool.fee_rate), Decimal(FEE_DEN)))
    want = _SOLVE.subtract(ONE, _SOLVE.divide(Decimal(s_ppm), Decimal(PPM)))
    if c <= want:
        return 0                                             # the fee alone exceeds the budget
    lo = Decimal(0)                                          # feasible: h(lo) >= want (h(0+) = c > want)
    hi = _SOLVE.divide(Decimal(s_ppm), Decimal(PPM))
    for _ in range(_BISECT_STEPS):                           # expand until infeasible
        if _sell_keep(hi, r, c)[0] < want:
            break
        lo, hi = hi, _SOLVE.multiply(hi, Decimal(2))
    u = _SOLVE.divide(_SOLVE.add(lo, hi), Decimal(2))
    for _ in range(_BISECT_STEPS * 2):
        h, dh = _sell_keep(u, r, c)
        if h >= want:
            lo = u
        else:
            hi = u
        if _SOLVE.subtract(hi, lo) <= _SOLVE.multiply(hi, _SOLVE_TOL):
            break
        nxt = _SOLVE.subtract(u, _SOLVE.divide(_SOLVE.subtract(h, want), dh)) if dh < 0 else lo
        if not lo < nxt < hi:
            nxt = _SOLVE.divide(_SOLVE.add(lo, hi), Decimal(2))
        u = nxt
    v = DEC.multiply(DEC.multiply(lo, r), y)
    return max(0, floor_int(DEC.multiply(v, DEC.subtract(ONE, Decimal("1e-9")))))


def vcap_detail(s: SubnetState, nav_liq: int, book_view: BookView, block: int, feat: Feat | None, cfg: RiskCfg,
                hist: Sequence[ChainSnapshot], *, t_now_pool: PoolState | None = None) -> CapDetail:
    """Section 3.5 V_cap of one generation (see the module docstring). `hist` are the 3-day history snapshots."""
    pool = t_now_pool if t_now_pool is not None else s.pool
    t_now = int(pool.tao)
    t_min = t_now
    for snap in hist:
        old = snap.get(s.key)
        if old is not None:
            t_min = min(t_min, int(old.pool.tao))
    t_st = t_min * (PPM - int(cfg.d_t_ppm)) // PPM
    s_ppm = int(cfg.s_exit_entry_ppm)
    if abs(pool.w_quote_e18 - HALF_E18) > SKEW_BAND_E18:
        base = _skewed_base(pool, t_st, s_ppm)
    else:
        base = int(v_max(Rao(t_st), s_ppm))
    x = int(pool.alpha)
    e = max(int(s.escrow_alpha or 0), 0)
    if e <= 0:
        m_esc = PPM
    elif x <= 0:
        m_esc = M_ESC_MIN_PPM
    else:
        m_esc = max(M_ESC_MIN_PPM, min(PPM, PPM - e * PPM // x))
    m_ev = OWNER_EVENT_CAP_PPM if owner_cooldown_active(book_view, s.key, block) else PPM
    m_own = m_owner_ppm(feat, cfg)
    cap = base * m_esc // PPM * m_ev // PPM
    if cfg.owner_haircut_active:
        cap = cap * m_own // PPM
    nu = max(int(nav_liq), 0) * int(cfg.nu_max_ppm) // PPM
    return CapDetail(key=s.key, t_now=Rao(t_now), t_min_3d=Rao(t_min), t_st=Rao(t_st), base=Rao(base), m_esc_ppm=m_esc,
                     m_owner_event_ppm=m_ev, m_owner_ppm=m_own, nu_cap=Rao(nu), cap=Rao(max(0, min(cap, nu))))


class Caps:
    """The CapsFn (core.protocols.CapsFn): per-subnet V_cap for every generation of the view, 0 for a held generation
    that is no longer in it. Pure: reads the TickContext and the bounded SnapshotStore only."""

    def __call__(self, ctx: TickContext, cfg: RiskCfg) -> dict[SubnetKey, Rao]:
        return {k: d.cap for k, d in self.details(ctx, cfg).items()} | {
            p.key: Rao(0) for p in ctx.portfolio.positions if ctx.view.get(p.key) is None}

    @staticmethod
    def details(ctx: TickContext, cfg: RiskCfg, keys: Sequence[SubnetKey] | None = None) -> dict[SubnetKey, CapDetail]:
        """CapDetail per generation of the view (only `keys` when given)."""
        hist = t_history(ctx.store, ctx.raw)
        b = int(ctx.block)
        want = set(keys) if keys is not None else None
        out: dict[SubnetKey, CapDetail] = {}
        for s in ctx.view.subnets:
            if want is None or s.key in want:
                out[s.key] = vcap_detail(s, int(ctx.nav_liq), ctx.book_view, b, ctx.frame.feats.get(s.key), cfg, hist)
        return out


caps: Final[Caps] = Caps()


# ------------------------------------------------------------------------------------------------- section I
@dataclass(frozen=True, slots=True)
class AggregateLimits:
    nav: int
    g_max_eff_ppm: int
    gross_cap: int
    n_eff: int
    ladder_cap: int
    owner_cap: int
    young_cap: int
    lcw_cap: int
    exit_budget: int


def aggregate_limits(nav_liq: int, g_max_eff_ppm: int, cfg: RiskCfg) -> AggregateLimits:
    nav = max(int(nav_liq), 0)
    gross = nav * int(g_max_eff_ppm) // PPM
    v_min = max(int(cfg.v_min_rao), 1)
    n_eff = min(int(cfg.n_max), gross // (4 * v_min))
    return AggregateLimits(nav=nav, g_max_eff_ppm=int(g_max_eff_ppm), gross_cap=gross, n_eff=n_eff,
                           ladder_cap=nav * int(cfg.ladder_bucket_ppm) // PPM,
                           owner_cap=nav * int(cfg.owner_cluster_ppm) // PPM,
                           young_cap=nav * int(cfg.young_bucket_ppm) // PPM,
                           lcw_cap=nav * LCW_BUCKET_PPM // PPM,
                           exit_budget=nav * int(cfg.exit_budget_ppm) // PPM)


def _cut(values: dict[SubnetKey, int], current: Mapping[SubnetKey, int], keys: Sequence[SubnetKey], excess: int,
         es: Mapping[SubnetKey, int]) -> int:
    """Reduce values on `keys` by `excess`: increases first (pro rata), then holdings with the largest ES first.
    Returns the amount actually cut (mutates `values`)."""
    if excess <= 0:
        return 0
    incs = [(k, values[k] - min(values[k], current.get(k, 0))) for k in sorted(keys)]
    incs = [(k, v) for k, v in incs if v > 0]
    total_inc = sum(v for _, v in incs)
    cut = 0
    if total_inc > 0:
        if total_inc <= excess:
            for k, v in incs:
                values[k] -= v
            cut = total_inc
        else:
            for k, v in incs:
                c = min(v, -((-excess * v) // total_inc))         # ceil share, never above the increase
                c = min(c, excess - cut)
                values[k] -= c
                cut += c
    rest = excess - cut
    if rest > 0:
        for k in sorted(keys, key=lambda k: (-es.get(k, 0), k)):
            if rest <= 0:
                break
            c = min(values[k], rest)
            if c > 0:
                values[k] -= c
                rest -= c
                cut += c
    return cut


def apply_aggregates(values: Mapping[SubnetKey, int], current: Mapping[SubnetKey, int], view: ChainSnapshot,
                     raw: ChainSnapshot, cfg: RiskCfg, limits: AggregateLimits,
                     lcw_ppm: Mapping[SubnetKey, int] | None = None, *, entries_only_n_eff: bool = True,
                     vcap_now: Mapping[SubnetKey, int] | None = None) -> tuple[dict[SubnetKey, int], list[RiskAction]]:
    """Section I on target values (rao, executable). Returns the new values and one CLAMP action per binding rule.
    `current` are the executable values held now; `lcw_ppm` the LCW share of each target's attribution;
    `vcap_now` the per-key V_cap at T_now for the hold-budget trims (defaults to T_now * s/(1-s))."""
    out = {k: max(int(v), 0) for k, v in values.items()}
    acts: list[RiskAction] = []
    b = int(raw.block)
    ranks = ladder_ranks(raw)
    pools: dict[SubnetKey, PoolState] = {}
    for k in sorted(out):
        sv = view.get(k)
        if sv is not None:
            pools[k] = sv.pool
    es = {k: exit_shortfall_ppm(pools[k], v) if k in pools else PPM for k, v in out.items()}

    def bucket(rule: str, keys: list[SubnetKey], cap: int, extra: str = "") -> None:
        total = sum(out[k] for k in keys)
        if total > cap:
            done = _cut(out, current, keys, total - cap, es)
            acts.append(RiskAction(rule, None, "CLAMP", f"total_rao={total};cap_rao={cap};cut_rao={done}{extra}"))

    keys = sorted(out)
    bucket("liquidity.ladder_bucket", [k for k in keys if ranks.get(k, LADDER_BUCKET_RANK + 1) <= LADDER_BUCKET_RANK],
           limits.ladder_cap)
    clusters: dict[str, list[SubnetKey]] = {}
    for k in keys:
        s = raw.get(k) or view.get(k)
        if s is not None and s.owner_coldkey is not None:
            clusters.setdefault(str(s.owner_coldkey), []).append(k)
    for owner in sorted(clusters):
        bucket("liquidity.owner_cluster", clusters[owner], limits.owner_cap, f";owner={owner}")
    young: list[SubnetKey] = []
    for k in keys:
        s = raw.get(k) or view.get(k)
        fe = s.first_emission_block if s is not None else None
        if fe is None or b - (int(fe) - 1) < YOUNG_SINCE_START_BLOCKS:
            young.append(k)
    bucket("liquidity.young_bucket", young, limits.young_cap)
    if lcw_ppm:
        bucket("liquidity.lcw_bucket", [k for k in keys if lcw_ppm.get(k, 0) * 2 >= PPM], limits.lcw_cap)
    bucket("liquidity.gross", keys, limits.gross_cap)

    # N_eff: at most n_eff names; new entries beyond it are dropped (smallest first)
    names = [k for k in keys if out[k] > 0]
    if len(names) > limits.n_eff:
        over = len(names) - limits.n_eff
        new = sorted((k for k in names if current.get(k, 0) <= 0), key=lambda k: (out[k], k))
        dropped = new[:over]
        if not entries_only_n_eff and len(dropped) < over:
            held = sorted((k for k in names if current.get(k, 0) > 0), key=lambda k: (out[k], k))
            dropped += held[:over - len(dropped)]
        for k in dropped:
            out[k] = 0
        if dropped:
            acts.append(RiskAction("liquidity.n_eff", None, "CLAMP",
                                   f"names={len(names)};n_eff={limits.n_eff};dropped={len(dropped)}"))

    # hold budget: ES > S_EXIT_HOLD_MAX -> trim to V_cap at T_now; sum ES * V <= exit budget, largest ES first
    for k in keys:
        v = out[k]
        if v <= 0 or k not in pools:
            continue
        e = exit_shortfall_ppm(pools[k], v)
        es[k] = e
        if e > cfg.s_exit_hold_max_ppm:
            cap_now = (vcap_now or {}).get(k, int(v_max(Rao(int(pools[k].tao)), int(cfg.s_exit_entry_ppm))))
            if v > cap_now:
                out[k] = cap_now
                es[k] = exit_shortfall_ppm(pools[k], cap_now)
                acts.append(RiskAction("liquidity.hold_es", k, "CLAMP", f"es_ppm={e};from_rao={v};to_rao={cap_now}"))
    used = sum(es.get(k, 0) * out[k] // PPM for k in keys if out[k] > 0)
    if used > limits.exit_budget:
        for k in sorted((k for k in keys if out[k] > 0), key=lambda k: (-es.get(k, 0), k)):
            if used <= limits.exit_budget:
                break
            own = es.get(k, 0) * out[k] // PPM
            room = limits.exit_budget - (used - own)
            lo, hi = 0, out[k]
            pool = pools.get(k)
            for _ in range(_BISECT_STEPS):
                if hi - lo <= 1 or pool is None:
                    break
                mid = (lo + hi) // 2
                if exit_shortfall_ppm(pool, mid) * mid // PPM <= room:
                    lo = mid
                else:
                    hi = mid
            new_v = lo if pool is not None and room > 0 else 0
            new_own = exit_shortfall_ppm(pool, new_v) * new_v // PPM if pool is not None else 0
            acts.append(RiskAction("liquidity.exit_budget", k, "CLAMP",
                                   f"es_ppm={es.get(k, 0)};from_rao={out[k]};to_rao={new_v};budget_rao={limits.exit_budget}"))
            used = used - own + new_own
            out[k] = new_v
    return out, acts


if TYPE_CHECKING:
    from ..core.protocols import CapsFn

    _CAPS_FN: CapsFn = caps                                # mypy: conformance to core.protocols.CapsFn
