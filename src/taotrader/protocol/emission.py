"""taotrader/protocol/emission.py - block-emission curve, root proportion, the get_shares replica and the per-subnet
injection / chain-buy split (WP2; DESIGN.md section 5.12, brief sections 3.1-3.5).

All functions are pure and read parameters from ChainGlobals/SubnetState. Money values are Decimal (core.fixed.DEC)
until the final floor to integer rao.

Timing: the coinbase of block n runs BEFORE that block's EMA update, so emission in block n uses the state at the
end of block n - 1 (EMA_{n-1}, reserves, weights, RootProp, the stored gate bar). `emission_vector(snap)` therefore
models the coinbase of block snap.block + 1, and `refresh_theta` should be ((snap.block + 1) % 360 == 0) when the
caller wants the chain's periodic rank-mode refresh (the bar is also recomputed whenever the stored bar is 0).
Golden parity (tests/protocol/test_emission.py): per-subnet |E_model - (SubnetTaoInEmission + SubnetExcessTao)|
<= 1 rao/block (acceptance: 1e-6 TAO/block) on all 20 fixture pairs at specs 441-473 and on every subnet at
9,240,382 (spec 475).

Share rule by regime of the modelled block (section 8.6; exact only from gate_rank32 on):
- >= gate_rank32 (8,765,684): rank-mode Hill gate (exact replica; theta = stored bar or the gate_rank-th largest
  positive burn-adjusted share on refresh);
- gate_qmass (8,713,794 .. 8,765,683): Hill gate with the STORED bar only (the q-mass refresh rule is not
  replicated: approximation, gated by parity_ok);
- price_ema (8,636,191 .. 8,713,793): burn-adjusted EMA shares, no gate;
- price_ema_rp (8,466,531 .. 8,636,190): EMA x RootProp x (1 - MinerBurned) shares, no gate;
- earlier (taoflow era and before): EMA shares, no gate - NOT the chain rule (flow-EMA shares are not replicated);
  only parity_ok-gated diagnostics may use it.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.fixed import DEC, ONE, floor_int
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import RAO_PER_TAO, AlphaRao, Rao, SubnetKey
from .regimes import INITIAL_BLOCK_EMISSION_RAO, TOTAL_SUPPLY_RAO, regime

ZERO: Final[Decimal] = Decimal(0)
PARITY_MEDIAN_MAX: Final[Decimal] = Decimal("0.01")      # median |rel err| must be < 1%
PARITY_BAND: Final[Decimal] = Decimal("0.05")            # "within 5%"
PARITY_BAND_SHARE_NUM: Final[int] = 9                    # >= 90% of subnets within the band
PARITY_BAND_SHARE_DEN: Final[int] = 10


def dsum(values: Iterable[Decimal]) -> Decimal:
    """Sum in the fixed DEC context (Python's sum() would use the thread's default 28-digit context)."""
    acc = ZERO
    for v in values:
        acc = DEC.add(acc, v)
    return acc


# ---------------------------------------------------------------- emission.py
def block_emission_for_issuance(issuance_rao: int) -> Rao:
    """floor(1e9 * 2**(-floor(log2(1/(1 - I/21e15))))); 0 at/above the cap. Used for TAO and per-subnet alpha.

    Exact integer form: k = max{k : 2**k * (cap - I) <= cap}, emission = 1e9 >> k."""
    issued = max(issuance_rao, 0)
    if issued >= TOTAL_SUPPLY_RAO:
        return Rao(0)
    remaining = TOTAL_SUPPLY_RAO - issued
    k = 0
    while (remaining << (k + 1)) <= TOTAL_SUPPLY_RAO:
        k += 1
    return Rao(INITIAL_BLOCK_EMISSION_RAO >> k)


def root_prop(glob: ChainGlobals, alpha_issuance: AlphaRao) -> Decimal:
    """rp = (SubnetTAO[0]*TaoWeight)/(SubnetTAO[0]*TaoWeight + alpha_issuance). Stored RootProp wins when present."""
    num = DEC.multiply(Decimal(glob.root_tao), glob.tao_weight)
    den = DEC.add(num, Decimal(max(alpha_issuance, 0)))
    return DEC.divide(num, den) if den > 0 else ZERO


def alpha_issuance(s: SubnetState) -> AlphaRao:
    """SubnetAlphaIn + SubnetAlphaOut + the alpha reservoir: the issuance that drives the subnet's alpha curve and rp."""
    return AlphaRao(s.pool.alpha + s.alpha_out + s.reservoir_alpha)


def emit_eligible(s: SubnetState) -> bool:
    """get_subnets_to_emit_to: FirstEmissionBlockNumber set AND SubtokenEnabled AND NetworkRegistrationAllowed
    (non-root and NetworksAdded hold by ChainSnapshot construction). Not eligible -> no share and a frozen EMA."""
    return s.first_emission_block is not None and s.subtoken_enabled and s.reg_allowed and s.key.netuid != 0


@dataclass(frozen=True, slots=True)
class EmissionShare:
    key: SubnetKey
    b: Decimal                    # burn-adjusted EMA share (renormalised)
    keep: Decimal                 # g/b = 1/(1+(theta/b)^h)
    final: Decimal                # gated share among ENABLED subnets
    tao_per_block: Rao            # E_i
    tao_in_per_block: Rao         # min(E_i, rp*alpha_emission*spot)
    chain_buy_per_block: Rao      # E_i - tao_in (the only price-moving protocol bid)


def _share_mode(block: int) -> str:
    if block >= regime("gate_rank32").first_block:
        return "rank"
    if block >= regime("gate_qmass").first_block:
        return "stored_bar"
    if block >= regime("price_ema").first_block:
        return "no_gate"
    if block >= regime("price_ema_rp").first_block:
        return "rp_weighted"
    return "no_gate"


def gate_theta(b_values: Iterable[Decimal], rank: int) -> Decimal:
    """Rank-mode bar: the rank-th largest POSITIVE burn-adjusted share (the smallest positive one when fewer than
    `rank` are positive - UNVERIFIED corner; 0 when none is positive)."""
    pos = sorted((v for v in b_values if v > 0), reverse=True)
    if not pos:
        return ZERO
    return pos[min(max(rank, 1), len(pos)) - 1]


def gate_keep(b: Decimal, theta: Decimal, exponent: int) -> Decimal:
    """g/b = 1/(1+(theta/b)^h); 0 for b <= 0; 1 when theta <= 0 (no gate)."""
    if b <= 0:
        return ZERO
    if theta <= 0:
        return ONE
    return DEC.divide(ONE, DEC.add(ONE, DEC.power(DEC.divide(theta, b), exponent)))


def injection_split(s: SubnetState, e_exact: Decimal) -> tuple[Rao, Rao, Rao]:
    """(E_i, tao_in, chain_buy) in rao for an exact per-block TAO emission e_exact (get_subnet_terms):
    alpha_in = E/spot capped at rp*alpha_emission; tao_in = alpha_in*spot; excess = E - tao_in is swapped into the
    pool fee-free (chain buy). alpha_emission follows the curve on the subnet's alpha issuance."""
    if e_exact <= 0:
        return Rao(0), Rao(0), Rao(0)
    alpha_em = block_emission_for_issuance(alpha_issuance(s))
    cap_alpha = DEC.multiply(s.root_prop, Decimal(alpha_em))
    spot = s.pool.spot() if s.pool.px_alpha > 0 and s.pool.px_tao > 0 else ZERO
    cap_tao = DEC.multiply(cap_alpha, spot)
    tao_in = min(e_exact, cap_tao)
    return Rao(floor_int(e_exact)), Rao(floor_int(tao_in)), Rao(floor_int(DEC.subtract(e_exact, tao_in)))


def emission_vector(snap: ChainSnapshot, ema_override: dict[SubnetKey, Decimal] | None = None,
                    enabled_override: dict[SubnetKey, bool] | None = None,
                    refresh_theta: bool = False) -> dict[SubnetKey, EmissionShare]:
    """Exact replica of get_shares (rank mode): eligible = FirstEmissionBlockNumber set & SubtokenEnabled &
    NetworkRegistrationAllowed & non-root; s = EMA/sum; b = s*(1-min(MB,1)) renormalised (fallback s);
    theta = EmissionGateBar (or the Nth-largest positive b when refresh_theta or theta == 0; N = gate_rank);
    g = b/(1+(theta/b)^h); gate runs BEFORE disabled subnets are zeroed; renormalise over enabled;
    E_i = block_emission * final_i. Then the injection split per subnet.

    Returns a share for EVERY subnet of the snapshot (ineligible ones are all zero). ema_override replaces
    SubnetMovingPrice per key (EMA forecasts); enabled_override replaces SubnetEmissionEnabled per key."""
    glob = snap.glob
    mode = _share_mode(snap.block + 1)
    eligible = [s for s in snap.subnets if emit_eligible(s)]
    ema: dict[SubnetKey, Decimal] = {}
    for s in eligible:
        v = ema_override.get(s.key, s.moving_price) if ema_override else s.moving_price
        ema[s.key] = v if v > 0 else ZERO
    total = dsum(ema.values())

    # Sum EMA == 0 (no eligible subnet has a price history): every share is 0, so nothing is emitted. The chain's
    # behaviour in this state is UNVERIFIED (it never occurs on mainnet); zero is the fail-closed reading.
    b: dict[SubnetKey, Decimal] = dict.fromkeys(ema, ZERO)
    if total > 0:
        share = {k: DEC.divide(v, total) for k, v in ema.items()}
        weight: dict[SubnetKey, Decimal] = {}
        for s in eligible:
            w = DEC.multiply(share[s.key], DEC.subtract(ONE, min(max(s.miner_burned, ZERO), ONE)))
            if mode == "rp_weighted":
                w = DEC.multiply(w, s.root_prop)
            weight[s.key] = w
        tw = dsum(weight.values())
        b = {k: DEC.divide(v, tw) for k, v in weight.items()} if tw > 0 else share

    keep: dict[SubnetKey, Decimal] = {k: (ONE if v > 0 else ZERO) for k, v in b.items()}
    pre: dict[SubnetKey, Decimal] = dict(b)
    if mode in ("rank", "stored_bar") and b:
        theta = glob.gate_bar
        if mode == "rank" and (refresh_theta or theta <= 0):
            theta = gate_theta(b.values(), glob.gate_rank)
        keep = {k: gate_keep(v, theta, glob.gate_exponent) for k, v in b.items()}
        g = {k: DEC.multiply(v, keep[k]) for k, v in b.items()}
        tg = dsum(g.values())
        pre = {k: DEC.divide(v, tg) for k, v in g.items()} if tg > 0 else dict(b)

    def enabled(s: SubnetState) -> bool:
        return enabled_override.get(s.key, s.emission_enabled) if enabled_override else s.emission_enabled

    te = dsum(pre[s.key] for s in eligible if enabled(s))
    out: dict[SubnetKey, EmissionShare] = {}
    for s in snap.subnets:
        if s.key not in pre:
            out[s.key] = EmissionShare(key=s.key, b=ZERO, keep=ZERO, final=ZERO, tao_per_block=Rao(0),
                                       tao_in_per_block=Rao(0), chain_buy_per_block=Rao(0))
            continue
        final = DEC.divide(pre[s.key], te) if enabled(s) and te > 0 else ZERO
        e_i, tao_in, chain_buy = injection_split(s, DEC.multiply(Decimal(glob.block_emission), final))
        out[s.key] = EmissionShare(key=s.key, b=b[s.key], keep=keep[s.key], final=final, tao_per_block=e_i,
                                   tao_in_per_block=tao_in, chain_buy_per_block=chain_buy)
    return out


def sum_ema(snap: ChainSnapshot) -> Decimal:
    """Sum of SubnetMovingPrice over emit-eligible subnets; root dividends accrue (root sell flag) iff > 1."""
    return dsum(s.moving_price for s in snap.subnets if emit_eligible(s) and s.moving_price > 0)


def observed_block_emission(cur: SubnetState, prev: SubnetState | None = None) -> Rao:
    """Observed TAO emission of one block: SubnetTaoInEmission + SubnetExcessTao (+ the TAO reservoir delta since
    `prev` when it is the same generation: parked injection shows up there, not in SubnetTaoInEmission)."""
    d_res = cur.reservoir_tao - prev.reservoir_tao if prev is not None and prev.key == cur.key else 0
    return Rao(cur.tao_in_emission + cur.excess_tao + d_res)


def parity_rel_errors(pairs: Iterable[tuple[int, int]], min_obs_rao_day: int = RAO_PER_TAO) -> tuple[Decimal, ...]:
    """|E_model - E_obs| / E_obs for (model, observed) TAO-per-day pairs (rao) of ENABLED subnets, keeping only those
    with E_obs > min_obs_rao_day (1 TAO/day). Input to parity_ok."""
    out: list[Decimal] = []
    for model, obs in pairs:
        if obs > min_obs_rao_day:
            out.append(DEC.divide(Decimal(abs(model - obs)), Decimal(obs)))
    return tuple(out)


def parity_ok(rel_errors: Sequence[Decimal]) -> bool:
    """THE emission-parity gate (EmissionView.model_ok AND carry T2a). rel_errors = |E_model - E_obs|/E_obs over
    enabled subnets with E > 1 TAO/day, E_obs = trailing-300-block 7200*(SubnetTaoInEmission + SubnetExcessTao
    + reservoir delta). True iff median < 1% AND >= 90% of them are within 5%. Empty input -> False."""
    errs = sorted(rel_errors)
    n = len(errs)
    if n == 0:
        return False
    mid = n // 2
    median = errs[mid] if n % 2 == 1 else DEC.divide(DEC.add(errs[mid - 1], errs[mid]), Decimal(2))
    within = sum(1 for e in errs if e <= PARITY_BAND)
    return median < PARITY_MEDIAN_MAX and within * PARITY_BAND_SHARE_DEN >= PARITY_BAND_SHARE_NUM * n
