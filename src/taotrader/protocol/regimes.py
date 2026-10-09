"""taotrader/protocol/regimes.py - the ONE regime table, per-spec flags, swap-fee defaults by spec, and the protocol
constants compiled into the runtime (WP2; DESIGN.md sections 8.2 and 8.6, brief sections 2-4).

Regimes start at setCode block + 1, the first block that executes the new code. Strategies never read the regime
id (no hindsight): it drives `valid_from_block` guards, report slicing and the era-correct protocol replicas only.

Entries whose first block is not yet known are NOT in REGIMES (DESIGN.md section 11, WP2): WP4 finds them with
`refine.spec_boundaries` and the lead adds them by ADR. The same holds for every `touches_econ` value (SPECS) and
the era-A swap fee. Until then:
- `PENDING_REGIMES` lists the missing ids (taoflow, chainbuy_unrecorded, spec475);
- `touches_econ(spec)` is False for every spec;
- `fee_rate_default(spec < 290)` returns 33 and `fee_rate_default_verified` is False, so the loader flags era-A
  results `Quality.DEFAULT_FILLED`.

This is also the only module that may hold the protocol's frozen economic literals (static gate in
tests/core/test_static_gates.py); every value that exists in storage is read live from ChainGlobals/SubnetState.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.units import RAO_PER_TAO, Block

# ------------------------------------------------------------------ runtime constants (code, not storage)
MINIMUM_RESERVE_RAO: Final[int] = 1_000_000          # Swap MinimumReserve: the output-side reserve before a swap
MAX_SWAP_INPUT_RESERVE_MULT: Final[int] = 1_000      # MAX_SWAP_INPUT_RESERVE_MULTIPLIER: input <= 1000 x reserve
TOTAL_SUPPLY_RAO: Final[int] = 21_000_000 * RAO_PER_TAO   # emission-curve cap (TAO and every subnet's alpha)
INITIAL_BLOCK_EMISSION_RAO: Final[int] = RAO_PER_TAO      # 1 TAO (or 1 alpha) per block before the first halving
VALIDATOR_SHARE: Final[Decimal] = Decimal("0.5")     # miner / validator split of the non-owner alpha emission
LOCK_COST_MULT: Final[int] = 2                       # registration cost starts at 2 x NetworkLastLockCost
FEE_RATE_V3_LAUNCH: Final[int] = 196                 # Swap.FeeRate default for specs 290-292 (0.299%)
FEE_RATE_DEFAULT: Final[int] = 33                    # Swap.FeeRate default afterwards (0.0504%)
V3_LAUNCH_FIRST_SPEC: Final[int] = 290
V3_LAUNCH_LAST_SPEC: Final[int] = 292
ERA_A_FEE_RATE: Final[int] = FEE_RATE_DEFAULT        # placeholder until WP4 measures it (ADR; section 13 Q21)
ERA_A_FEE_VERIFIED: Final[bool] = False


@dataclass(frozen=True, slots=True)
class Regime:
    regime_id: str
    first_block: Block          # setCode block + 1
    last_block: Block | None
    note: str


REGIMES: tuple[Regime, ...] = (
    Regime("era_a", Block(4_920_351), None, "dTAO launch; CP on (SubnetTAO, SubnetAlphaIn); first weeks EARLY_TINY_POOL"),
    Regime("era_b_v3", Block(5_947_549), None,
           "swap v3 (lazy per subnet); fee 196 for specs 290-292; T/A diverges after 6,205,195; sim_swap from 6,262,253"),
    Regime("halving", Block(7_103_976), None, "first 0.5-TAO block"),
    Regime("spec411", Block(8_283_784), None, "SubnetExcessTao and SubnetEmissionEnabled exist"),
    Regime("price_ema_rp", Block(8_466_531), None,
           "setCode 8,466,530 (spec 421): price-EMA x rp x (1 - MinerBurned), no gate; SubnetTaoFlow valid"),
    Regime("balancer", Block(8_486_594), None, "era C: weighted pool, weights from Swap.SwapBalancer"),
    Regime("price_ema", Block(8_636_191), None, "spec 432: price-EMA x (1 - MinerBurned); rp removed"),
    Regime("gate_qmass", Block(8_713_794), None, "spec 440: Hill gate in q-mass mode"),
    Regime("gate_rank32", Block(8_765_684), None, "spec 441: rank-32 gate; Root Reborn; escrow baskets"),
    Regime("spec445", Block(8_831_004), None, "miner-burn scaling restored (spec 444 never ran)"),
    Regime("curated", Block(8_938_466), Block(9_088_597), "curated root weights; removed at 9,088,598"),
    Regime("basket_trading", Block(9_117_749), None, "BasketTradingEnabled"),
    Regime("v2_only", Block(9_217_508), None, "legacy Alpha drained; share pools V2 only"),
)

# Section 8.6 rows whose first block is unknown; added to REGIMES by the lead through an ADR (WP4 boundary search).
PENDING_REGIMES: tuple[str, ...] = (
    "taoflow",              # spec 334/338 setCode + 1 (2025-11-04/05): flow-EMA shares
    "chainbuy_unrecorded",  # v3.3.1-362 (2025-12-12) -> 8,283,783: sum SubnetTaoInEmission undercounts
    "spec475",              # setCode(475) + 1 (2026-10-07): Null consensus, precise emissions, PoW registration
)


@dataclass(frozen=True, slots=True)
class SpecInfo:
    spec_version: int
    touches_econ: bool          # release changes emission, dividend or registration code (lead-maintained via ADR)
    note: str


# One row per known spec. Empty until the lead's ADR lands (WP2 does not fill touches_econ values); an unknown spec
# has touches_econ False.
SPECS: tuple[SpecInfo, ...] = ()


def regime_at(block: Block) -> Regime:
    """The regime in force at `block`: the active regime (first_block <= block <= last_block) that started last."""
    best: Regime | None = None
    for r in REGIMES:
        if r.first_block <= block and (r.last_block is None or block <= r.last_block) and (
                best is None or r.first_block > best.first_block):
            best = r
    if best is None:
        raise ValueError(f"block {block} precedes the dTAO launch ({REGIMES[0].first_block})")
    return best


def regime(regime_id: str) -> Regime:
    """Lookup by id; KeyError for an unknown or still-pending id."""
    for r in REGIMES:
        if r.regime_id == regime_id:
            return r
    raise KeyError(regime_id)


def touches_econ(spec_version: int) -> bool:
    """Post-spec burn-in trigger (section 3.10 step 2). False for any spec without a lead-approved SPECS row."""
    for s in SPECS:
        if s.spec_version == spec_version:
            return s.touches_econ
    return False


def fee_rate_default(spec_version: int) -> int:
    """Swap.FeeRate when the key is absent: 196 for specs 290-292, 33 after; era A (< 290) the WP4-measured value
    (ERA_A_FEE_RATE; 33 until verified, see fee_rate_default_verified)."""
    if spec_version < V3_LAUNCH_FIRST_SPEC:
        return ERA_A_FEE_RATE
    if spec_version <= V3_LAUNCH_LAST_SPEC:
        return FEE_RATE_V3_LAUNCH
    return FEE_RATE_DEFAULT


def fee_rate_default_verified(spec_version: int) -> bool:
    """False while fee_rate_default(spec) is an unverified placeholder (era A): the loader sets DEFAULT_FILLED."""
    return spec_version >= V3_LAUNCH_FIRST_SPEC or ERA_A_FEE_VERIFIED
