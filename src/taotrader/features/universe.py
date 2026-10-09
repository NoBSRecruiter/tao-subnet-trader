"""taotrader/features/universe.py - overlay universe floor, sections A-G (WP5; DESIGN.md sections 3.2-3.4, 2.4, 3.8).

Sections A-G depend only on chain state, so they are evaluated once per snapshot here and their count is published
as FeatureFrame.universe_eligible. Sections H (cooldowns) and I (aggregates) depend on a book's own orders, fills and
holdings and are WP8's (risk/overlay.py, risk/liquidity.py). The count is the general (non-LCW) floor for ENTRY:

A eligibility: FirstEmissionBlockNumber set; NetworkRegistrationAllowed (hold); SubtokenEnabled (enter). Non-root and
  NetworksAdded hold by ChainSnapshot construction.
B liquidity: SubnetTAO >= 200 TAO; SwapBalancer quote in [0.30, 0.70]; FeeRate <= 330; escrow E/x <= 0.50 (an unread
  escrow, SubnetState.escrow_alpha None, counts as E = 0: the per-subnet escrow decode is WP4's, section 13 Q7).
C emission: SubnetEmissionEnabled, and no section 3.4 entry ban: after a disable observed at block d and/or a
  re-enable at block e, entries are banned until max(d + 100,800, e + 360).
D prune (section 3.3 entry floor): non-immune: prune_rank >= 6 and EMA >= 1.5 x the target's EMA (REG_CLOCK_HOT:
  rank >= 8 and 1.7 x), outside the Tier A zone (prune_possible and block + U + M_A >= window opening and (target or
  t* <= U + M_A)) and outside the backstop zone (prune_possible and rank <= K_BOTTOM and r <= R_BACKSTOP). Immune
  with expiry within 7 d: projected rank at expiry >= 6 (>= 8 when hot) under the flat-spot EMA forecast
  (protocol.ema.project_ema), subnets admitted at their own expiry; immune beyond 7 d: no prune constraint. The
  Monte-Carlo P_prune_7d rule is WP8's (hazard_mc).
E launch age: since_start >= 100,800 and age_reg >= 216,000 blocks, and no Gatekeeper veto flag (UNSTARTED,
  SEED_ANOMALY, EMA_WARMING, BURNING, EMISSION_OFF).
F burn: MinerBurned <= 0.50.
G validator: at least one RouterCandidate passes the book-independent filters (section 3.8).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import RiskCfg
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import PERQUINTILL, PPM, RAO_PER_TAO, SubnetKey
from ..protocol.ema import project_ema
from ..protocol.prune import immunity_end, prune_possible, window_open_block
from .gatekeeper import NON_LCW_VETOES, REG_CLOCK_HOT, since_start

SECTIONS: Final[tuple[str, ...]] = ("A", "B", "C", "D", "E", "F", "G")


@dataclass(frozen=True, slots=True)
class UniverseParams:
    """Section 3.2-3.4 thresholds (RiskCfg defaults; see from_risk)."""
    t_min_pool_rao: int = 200 * RAO_PER_TAO
    quote_min_e18: int = 3 * PERQUINTILL // 10
    quote_max_e18: int = 7 * PERQUINTILL // 10
    fee_rate_max: int = 330
    escrow_max_ppm: int = 500_000
    entry_min_rank: int = 6
    entry_min_rho_ppm: int = 1_500_000
    hot_min_rank: int = 8                      # REG_CLOCK_HOT raises the floor (section 3.3)
    hot_min_rho_ppm: int = 1_700_000
    immune_lookahead_blocks: int = 50_400      # "immune with expiry within 7 d"
    unwind_blocks: int = 15                    # U = unwind_exec_blocks * (1 + unwind_retries)
    margin_a_blocks: int = 300                 # M_A
    k_bottom: int = 3
    r_backstop_ppm: int = 1_200_000
    emission_ban_blocks: int = 100_800
    reenable_wait_blocks: int = 360
    min_since_start_blocks: int = 100_800
    min_age_reg_blocks: int = 216_000
    burn_entry_max_ppm: int = 500_000

    @staticmethod
    def from_risk(cfg: RiskCfg) -> UniverseParams:
        return UniverseParams(
            t_min_pool_rao=int(cfg.t_min_pool_rao), fee_rate_max=cfg.fee_rate_max, escrow_max_ppm=int(cfg.escrow_max_ppm),
            entry_min_rank=cfg.entry_min_rank, entry_min_rho_ppm=int(cfg.entry_min_rho_ppm),
            unwind_blocks=cfg.unwind_exec_blocks * (1 + cfg.unwind_retries), margin_a_blocks=cfg.margin_a_blocks,
            k_bottom=cfg.k_bottom, r_backstop_ppm=int(cfg.r_backstop_ppm), emission_ban_blocks=cfg.emission_ban_blocks,
            reenable_wait_blocks=cfg.reenable_wait_blocks, min_since_start_blocks=cfg.min_since_start_blocks,
            min_age_reg_blocks=cfg.min_age_reg_blocks, burn_entry_max_ppm=int(cfg.burn_entry_max_ppm))


@dataclass(frozen=True, slots=True)
class UniverseInputs:
    """Per-snapshot inputs the engine has already computed (shared with the Feats)."""
    snap: ChainSnapshot
    prune_rank: Mapping[SubnetKey, int]        # non-immune ladder rank (1 = target)
    target: SubnetKey | None
    bottom_ema: Decimal | None                 # SubnetMovingPrice of the target
    t_star: Mapping[SubnetKey, int | None]     # stressed time-to-target (non-immune)
    flags: Mapping[SubnetKey, frozenset[str]]  # Gatekeeper flags
    validator_ok: Mapping[SubnetKey, bool]     # any eligible RouterCandidate
    emission_ban_until: Mapping[SubnetKey, int | None]
    cost_ratio: Decimal


@dataclass(frozen=True, slots=True)
class UniverseRow:
    key: SubnetKey
    a_hold: bool
    a_enter: bool
    b: bool
    c: bool
    d: bool
    e: bool
    f: bool
    g: bool
    failed: tuple[str, ...]                    # failing sections, in order A..G

    @property
    def eligible(self) -> bool:
        return not self.failed


def _pool_spot(s: SubnetState) -> Decimal:
    return s.pool.spot() if s.pool.px_tao > 0 and s.pool.px_alpha > 0 else Decimal(0)


def projected_rank(snap: ChainSnapshot, key: SubnetKey, at_block: int) -> int | None:
    """Rank of `key` in the ladder at `at_block` under the flat-spot EMA forecast of every subnet (each at its own
    current spot; frozen EMAs flat); candidates = subnets non-immune at at_block; ties by (reg_at, netuid).
    None if `key` is absent or still immune then. Only rivals whose forecast path can cross key's are projected:
    a flat-spot path is monotone between the current EMA and min(spot, 1)."""
    s_k = snap.get(key)
    if s_k is None or at_block < immunity_end(s_k, snap.glob):
        return None
    glob = snap.glob
    dn = max(at_block - int(snap.block), 0)
    mine = (project_ema(glob, s_k, snap.block, dn, _pool_spot(s_k)), s_k.key.reg_at, s_k.key.netuid)
    below = 0
    one = Decimal(1)
    for s in snap.subnets:
        if s.key == key or int(s.key.netuid) == 0 or at_block < immunity_end(s, glob):
            continue
        lo = min(s.moving_price, min(_pool_spot(s), one))
        hi = max(s.moving_price, min(_pool_spot(s), one))
        if lo > mine[0]:
            continue
        if hi < mine[0]:
            below += 1
            continue
        theirs = (project_ema(glob, s, snap.block, dn, _pool_spot(s)), s.key.reg_at, s.key.netuid)
        if theirs < mine:
            below += 1
    return below + 1


def emission_ban_until(last_disable: int | None, last_enable: int | None, params: UniverseParams) -> int | None:
    """Section 3.4: entries banned until max(disable + 100,800, re-enable + 360); None when no toggle is recorded."""
    ends = []
    if last_disable is not None:
        ends.append(last_disable + params.emission_ban_blocks)
    if last_enable is not None:
        ends.append(last_enable + params.reenable_wait_blocks)
    return max(ends) if ends else None


def evaluate(inp: UniverseInputs, params: UniverseParams) -> tuple[UniverseRow, ...]:
    """Sections A-G for every generation of the snapshot (sorted by key)."""
    snap = inp.snap
    glob = snap.glob
    block = int(snap.block)
    possible = prune_possible(glob)
    opens = int(window_open_block(glob))
    zone = params.unwind_blocks + params.margin_a_blocks
    rows: list[UniverseRow] = []
    for s in sorted(snap.subnets, key=lambda x: x.key):
        key = s.key
        flags = inp.flags.get(key, frozenset())
        hot = REG_CLOCK_HOT in flags
        min_rank = params.hot_min_rank if hot else params.entry_min_rank
        min_rho = params.hot_min_rho_ppm if hot else params.entry_min_rho_ppm

        a_hold = s.first_emission_block is not None and s.reg_allowed
        a_enter = a_hold and s.subtoken_enabled

        wq = s.pool.w_quote_e18
        escrow = s.escrow_alpha or 0
        b = (s.pool.tao >= params.t_min_pool_rao and params.quote_min_e18 <= wq <= params.quote_max_e18
             and s.pool.fee_rate <= params.fee_rate_max
             and (s.pool.alpha > 0 and escrow * PPM <= params.escrow_max_ppm * s.pool.alpha))

        ban = inp.emission_ban_until.get(key)
        c = s.emission_enabled and (ban is None or block >= ban)

        rank = inp.prune_rank.get(key)
        if rank is not None:
            bottom = inp.bottom_ema
            rho_ok = bottom is not None and bottom > 0 and s.moving_price * PPM >= bottom * min_rho
            t_star = inp.t_star.get(key)
            tier_a = possible and block + zone >= opens and (key == inp.target or (t_star is not None and t_star <= zone))
            backstop = possible and rank <= params.k_bottom and inp.cost_ratio * PPM <= params.r_backstop_ppm
            d = rank >= min_rank and rho_ok and not tier_a and not backstop
        else:
            expiry = int(immunity_end(s, glob))
            if expiry - block <= params.immune_lookahead_blocks:
                pr = projected_rank(snap, key, expiry)
                d = pr is not None and pr >= min_rank
            else:
                d = True

        ss = since_start(s, block)
        e = (ss is not None and ss >= params.min_since_start_blocks and block - int(key.reg_at) >= params.min_age_reg_blocks
             and not (flags & NON_LCW_VETOES))
        f = s.miner_burned * PPM <= params.burn_entry_max_ppm
        g = bool(inp.validator_ok.get(key, False))
        ok = (a_hold and a_enter, b, c, d, e, f, g)
        failed = tuple(sec for sec, passed in zip(SECTIONS, ok, strict=True) if not passed)
        rows.append(UniverseRow(key=key, a_hold=a_hold, a_enter=a_enter, b=b, c=c, d=d, e=e, f=f, g=g, failed=failed))
    return tuple(rows)


def eligible_count(rows: tuple[UniverseRow, ...]) -> int:
    return sum(1 for r in rows if r.eligible)
