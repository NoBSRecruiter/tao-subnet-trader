"""taotrader/protocol/amm.py - era-correct swap math (WP2). Integer rao in/out; Decimal (core.fixed.DEC) inside.

Exact formulas (brief 2.3-2.7):
  BUY : fee = floor(tao_in*f/65535); dy = tao_in - fee; alpha_out = x*(1-(y/(y+dy))**(w_q/w_b))  [w=0.5: x*dy//(y+dy)]
  SELL: fee_a = floor(a*f/65535); dx = a - fee_a; tao_out = y*(1-(x/(x+dx))**(w_b/w_q));
        then fee_a is sold fee-free into the same pool: tao_fee = y1*(1-(x1/(x1+fee_a))**(w_b/w_q)); pool absorbs all of a.
  Guards: MinimumReserve 1,000,000 rao; input <= 1000*reserve; gross >= 0.002 TAO + fee and dy >= 0.002 TAO;
          partial sells need tao_out >= 0.002 TAO; full exits exempt.
  Limits: buy max net dy = y*((p'/p)**w_b - 1); sell max net dx = x*((p/p')**w_q - 1); gross = net*65535/(65535-f).
          Strict: buy needs spot < limit, sell needs spot > limit, else PriceLimitExceeded.
  Era B: PoolKind.CP_V3_VIRTUAL uses px_* = (L*sqrtP, L/sqrtP) at w = 0.5 (exact absent tick crossings).

Implementation notes (WP2):
- The simulator never branches on era: swap math runs on the pricing reserves (px_tao, px_alpha) with the pool's
  weights; guards (MinimumReserve, 1000x, output <= reserve) use the REAL reserves (tao, alpha).
- w_quote == 0.5 exactly (every CP pool, and Balancer pools seeded at 0.5) takes the exact integer path; any other
  weight takes the DEC fractional power, floored. Both reproduce sim_swap to the rao on the golden fixtures
  (SN92/SN1 at 9,240,388, era-B SN1/SN19/SN64 at 7,000,020).
- The limit price bounds the pool's marginal spot after the user's swap step; on a sell the subsequent fee-alpha
  sale pushes the final price slightly lower (brief 2.7). SwapQuote.marginal_after is the post-step spot;
  `p.shifted(q.d_tao, q.d_alpha)` is the pool after the whole fill.
- No float, float() or true division in this module (static gate): integers use //, Decimals use DEC methods.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ..core.fixed import DEC, ONE, floor_int
from ..core.orders import FailReason
from ..core.state import PoolState
from ..core.units import FEE_DEN, MIN_STAKE_RAO, PERQUINTILL, PPM, RAO_PER_TAO, AlphaRao, PriceRao, Rao
from .fees import gross_for_net, swap_fee
from .regimes import MAX_SWAP_INPUT_RESERVE_MULT, MINIMUM_RESERVE_RAO


class SwapError(Exception):
    def __init__(self, reason: FailReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SwapQuote:
    amount_in: int                # gross input (rao for buys, alpha rao for sells)
    fee: int                      # input-side swap fee
    amount_out: int               # alpha out (buy) / TAO out (sell)
    author_fee_tao: int           # sells: TAO paid to the author for the fee alpha
    d_tao: int                    # real AND pricing reserve change (own-impact overlay input)
    d_alpha: int
    spot_before: PriceRao
    marginal_after: PriceRao      # pool spot after the fill (what the chain's limit bounds)
    shortfall_ppm: int            # 1 - executed / spot, incl. fee


class ImpactBound(StrEnum):
    TEMPORARY = "temporary"       # other flow heals our footprint: impact paid on BOTH legs (headline, gating)
    PERSISTENT = "persistent"     # our footprint persists: an immediate round trip costs only fees (optimistic bound)


# ------------------------------------------------------------------------------------------------- internals
_RAO = Decimal(RAO_PER_TAO)
_E18 = Decimal(PERQUINTILL)
_MAX_FIXUP_STEPS = 64


def _check_pool(p: PoolState) -> None:
    """Weights and fee rate out of range are programming errors (ValueError); an empty pool is the chain's
    ReservesTooLow, so marks and liq_value treat it as an unsellable position."""
    if not 0 < p.w_quote_e18 < PERQUINTILL or not 0 <= p.fee_rate < FEE_DEN:
        raise ValueError(f"degenerate pool weights/fee {p!r}")
    if p.px_tao <= 0 or p.px_alpha <= 0 or p.tao < 0 or p.alpha < 0:
        raise SwapError(FailReason.RESERVES_TOO_LOW)


def _out_given_in(reserve_in: int, reserve_out: int, d_in: int, w_in_e18: int, w_out_e18: int) -> int:
    """floor(reserve_out * (1 - (reserve_in / (reserve_in + d_in)) ** (w_in / w_out))); exact integer at equal weights."""
    if d_in <= 0:
        return 0
    if w_in_e18 == w_out_e18:
        return reserve_out * d_in // (reserve_in + d_in)
    base = DEC.divide(Decimal(reserve_in), Decimal(reserve_in + d_in))
    expo = DEC.divide(Decimal(w_in_e18), Decimal(w_out_e18))
    return floor_int(DEC.multiply(Decimal(reserve_out), DEC.subtract(ONE, DEC.power(base, expo))))


def _spot_rao_exact(w_quote_e18: int, px_tao: int, px_alpha: int) -> Decimal:
    """rao per alpha (exact to 60 digits) = (w_base / w_quote) * y / x * 1e9."""
    num = DEC.multiply(Decimal(PERQUINTILL - w_quote_e18), Decimal(px_tao))
    den = DEC.multiply(Decimal(w_quote_e18), Decimal(px_alpha))
    return DEC.multiply(DEC.divide(num, den), _RAO)


def _spot_rao(w_quote_e18: int, px_tao: int, px_alpha: int) -> PriceRao:
    return PriceRao(floor_int(_spot_rao_exact(w_quote_e18, px_tao, px_alpha)))


def _ceil_div(num: int, den: int) -> int:
    return -((-num) // den)


def _ceil_dec(d: Decimal) -> int:
    return -floor_int(DEC.minus(d))


# ------------------------------------------------------------------------------------------------- quotes
def quote_buy(p: PoolState, tao_in: Rao) -> SwapQuote:
    """Stake `tao_in` gross TAO (fee included) into the pool. Raises SwapError with the chain's error name."""
    _check_pool(p)
    fee = swap_fee(max(tao_in, 0), p.fee_rate)
    dy = tao_in - fee
    if tao_in < MIN_STAKE_RAO + fee or dy < MIN_STAKE_RAO:
        raise SwapError(FailReason.AMOUNT_TOO_LOW)
    if tao_in > MAX_SWAP_INPUT_RESERVE_MULT * p.tao:
        raise SwapError(FailReason.INSUFFICIENT_LIQUIDITY)
    if p.alpha < MINIMUM_RESERVE_RAO:
        raise SwapError(FailReason.RESERVES_TOO_LOW)
    if dy > MAX_SWAP_INPUT_RESERVE_MULT * p.tao:
        raise SwapError(FailReason.SWAP_INPUT_TOO_LARGE)
    alpha_out = _out_given_in(p.px_tao, p.px_alpha, dy, p.w_quote_e18, p.w_base_e18)
    if alpha_out <= 0 or alpha_out > p.alpha:
        raise SwapError(FailReason.INSUFFICIENT_LIQUIDITY)
    num = tao_in * p.w_quote_e18 * p.px_alpha - alpha_out * p.w_base_e18 * p.px_tao
    den = tao_in * p.w_quote_e18 * p.px_alpha
    return SwapQuote(amount_in=tao_in, fee=fee, amount_out=alpha_out, author_fee_tao=0, d_tao=dy, d_alpha=-alpha_out,
                     spot_before=p.spot_rao(),
                     marginal_after=_spot_rao(p.w_quote_e18, p.px_tao + dy, p.px_alpha - alpha_out),
                     shortfall_ppm=_ceil_div(num * PPM, den))


def quote_sell(p: PoolState, alpha_in: AlphaRao, *, partial_remaining: bool = False) -> SwapQuote:
    """Unstake `alpha_in` alpha (fee included). partial_remaining=True when a position remains afterwards: the TAO
    out must then be >= 0.002 TAO (full exits are exempt). Raises SwapError with the chain's error name."""
    _check_pool(p)
    if alpha_in <= 0:
        raise SwapError(FailReason.AMOUNT_TOO_LOW)
    fee_a = swap_fee(alpha_in, p.fee_rate)
    dx = alpha_in - fee_a
    if alpha_in > MAX_SWAP_INPUT_RESERVE_MULT * p.alpha:
        raise SwapError(FailReason.INSUFFICIENT_LIQUIDITY)
    if p.tao < MINIMUM_RESERVE_RAO:
        raise SwapError(FailReason.RESERVES_TOO_LOW)
    if dx > MAX_SWAP_INPUT_RESERVE_MULT * p.alpha:
        raise SwapError(FailReason.SWAP_INPUT_TOO_LARGE)
    tao_out = _out_given_in(p.px_alpha, p.px_tao, dx, p.w_base_e18, p.w_quote_e18)
    if tao_out <= 0 or tao_out > p.tao:
        raise SwapError(FailReason.INSUFFICIENT_LIQUIDITY)
    if partial_remaining and tao_out < MIN_STAKE_RAO:
        raise SwapError(FailReason.AMOUNT_TOO_LOW)
    x1, y1 = p.px_alpha + dx, p.px_tao - tao_out
    tao_fee = _out_given_in(x1, y1, fee_a, p.w_base_e18, p.w_quote_e18)     # fee alpha sold fee-free to the author
    num = alpha_in * p.w_base_e18 * p.px_tao - tao_out * p.w_quote_e18 * p.px_alpha
    den = alpha_in * p.w_base_e18 * p.px_tao
    return SwapQuote(amount_in=alpha_in, fee=fee_a, amount_out=tao_out, author_fee_tao=tao_fee,
                     d_tao=-(tao_out + tao_fee), d_alpha=alpha_in, spot_before=p.spot_rao(),
                     marginal_after=_spot_rao(p.w_quote_e18, y1, x1), shortfall_ppm=_ceil_div(num * PPM, den))


# ------------------------------------------------------------------------------------------------- limits
def marginal_after_buy(p: PoolState, tao_in: Rao) -> PriceRao:
    """spot*(1 + dy/y)**(1/w_b), dy = tao_in net of the swap fee; rounded UP to rao per alpha, so a buy limit derived
    from it never undercuts the fill's exact post-trade price (and marginal_after_buy(g) <= L <=> exact <= L)."""
    _check_pool(p)
    dy = max(tao_in - swap_fee(max(tao_in, 0), p.fee_rate), 0)
    ratio = DEC.divide(Decimal(p.px_tao + dy), Decimal(p.px_tao))
    expo = DEC.divide(_E18, Decimal(p.w_base_e18))
    spot = _spot_rao_exact(p.w_quote_e18, p.px_tao, p.px_alpha)
    return PriceRao(_ceil_dec(DEC.multiply(spot, DEC.power(ratio, expo))))


def marginal_after_sell(p: PoolState, alpha_in: AlphaRao) -> PriceRao:
    """spot*(1 + dx/x)**(-1/w_q), dx = alpha_in net of the swap fee (the step the limit bounds); rounded DOWN, so a
    sell floor derived from it never exceeds the fill's exact post-step price."""
    _check_pool(p)
    dx = max(alpha_in - swap_fee(max(alpha_in, 0), p.fee_rate), 0)
    ratio = DEC.divide(Decimal(p.px_alpha + dx), Decimal(p.px_alpha))
    expo = DEC.divide(_E18, Decimal(p.w_quote_e18))
    spot = _spot_rao_exact(p.w_quote_e18, p.px_tao, p.px_alpha)
    return PriceRao(floor_int(DEC.divide(spot, DEC.power(ratio, expo))))


def max_buy_to_limit(p: PoolState, limit: PriceRao) -> Rao:
    """Largest gross TAO whose post-fill marginal spot stays <= limit (the allow_partial cap; above it a fill-or-kill
    order fails with SlippageTooHigh). Raises SwapError(PRICE_LIMIT_EXCEEDED) if limit <= spot (strict).
    Consistent with marginal_after_buy: marginal_after_buy(result) <= limit, and any tao_in with
    marginal_after_buy(tao_in) <= limit satisfies tao_in <= result up to Decimal rounding at 60 digits."""
    _check_pool(p)
    spot = _spot_rao_exact(p.w_quote_e18, p.px_tao, p.px_alpha)
    if Decimal(limit) <= spot:
        raise SwapError(FailReason.PRICE_LIMIT_EXCEEDED)
    w_b = DEC.divide(Decimal(p.w_base_e18), _E18)
    growth = DEC.subtract(DEC.power(DEC.divide(Decimal(limit), spot), w_b), ONE)
    net = floor_int(DEC.multiply(Decimal(p.px_tao), growth))
    gross = gross_for_net(net, p.fee_rate)
    for _ in range(_MAX_FIXUP_STEPS):          # guarantee marginal_after_buy(result) <= limit despite rounding
        if gross <= 0 or marginal_after_buy(p, Rao(gross)) <= limit:
            break
        gross -= 1
    return Rao(max(gross, 0))


def max_sell_to_limit(p: PoolState, limit: PriceRao) -> AlphaRao:
    """Largest gross alpha whose post-step marginal spot stays >= limit. Raises SwapError(PRICE_LIMIT_EXCEEDED) if
    limit >= spot (strict); ValueError for limit <= 0 (no floor is not a limit)."""
    _check_pool(p)
    if limit <= 0:
        raise ValueError("sell limit must be > 0")
    spot = _spot_rao_exact(p.w_quote_e18, p.px_tao, p.px_alpha)
    if Decimal(limit) >= spot:
        raise SwapError(FailReason.PRICE_LIMIT_EXCEEDED)
    w_q = DEC.divide(Decimal(p.w_quote_e18), _E18)
    growth = DEC.subtract(DEC.power(DEC.divide(spot, Decimal(limit)), w_q), ONE)
    net = floor_int(DEC.multiply(Decimal(p.px_alpha), growth))
    gross = gross_for_net(net, p.fee_rate)
    for _ in range(_MAX_FIXUP_STEPS):          # guarantee marginal_after_sell(result) >= limit despite rounding
        if gross <= 0 or marginal_after_sell(p, AlphaRao(gross)) >= limit:
            break
        gross -= 1
    return AlphaRao(max(gross, 0))


# ------------------------------------------------------------------------------------------------- sizing
def v_max(pool_tao: Rao, slip_ppm: int) -> Rao:
    """T*s/(1-s): the largest position whose one-shot exit slips at most s against spot (floored)."""
    if pool_tao < 0 or not 0 <= slip_ppm < PPM:
        raise ValueError(f"bad v_max inputs pool_tao={pool_tao} slip_ppm={slip_ppm}")
    return Rao(pool_tao * slip_ppm // (PPM - slip_ppm))


def liq_value(p: PoolState, alpha: AlphaRao) -> Rao:
    """One-shot sell value (TAO to the seller, after fees and impact) of `alpha`; 0 if the sell would fail."""
    if alpha <= 0:
        return Rao(0)
    try:
        return Rao(quote_sell(p, alpha).amount_out)
    except SwapError:
        return Rao(0)


def round_trip_cost_ppm(p: PoolState, size: Rao, bound: ImpactBound, tx_fees_rao: int) -> int:
    """TEMPORARY: buy on p, then sell the alpha on the ORIGINAL p (healed) -> ~2f + 2V/(T+V) + tx/V.
    PERSISTENT: sell into the post-buy pool -> ~2f + tx/V. Momentum's original gate used PERSISTENT by mistake."""
    if size <= 0:
        raise ValueError("round trip size must be > 0")
    qb = quote_buy(p, size)                    # a failing buy propagates its SwapError
    sell_pool = p if bound is ImpactBound.TEMPORARY else p.shifted(qb.d_tao, qb.d_alpha)
    back = liq_value(sell_pool, AlphaRao(qb.amount_out))
    return _ceil_div((size + tx_fees_rao - back) * PPM, size)


def v_star(p: PoolState, alpha_h_ppm: int, shrink_ppm: int = 1_000_000) -> Rao:
    """Impact-optimal size maximising alpha_h*V - 2f*V - 2V^2/T: V* = T*(alpha_h - 2f)/4, times shrink (lambda^-1).

    T is the pricing TAO reserve (the depth the impact is paid on; equal to SubnetTAO except in era B), floored."""
    edge = alpha_h_ppm * FEE_DEN - 2 * p.fee_rate * PPM          # (alpha_h - 2f) * PPM * FEE_DEN
    if edge <= 0 or shrink_ppm <= 0 or p.px_tao <= 0:
        return Rao(0)
    return Rao(p.px_tao * edge * shrink_ppm // (4 * PPM * FEE_DEN * PPM))


def spot_rao_exact(p: PoolState) -> Decimal:
    """The pool's spot in rao per alpha without flooring (limit comparisons are strict against this value)."""
    _check_pool(p)
    return _spot_rao_exact(p.w_quote_e18, p.px_tao, p.px_alpha)

