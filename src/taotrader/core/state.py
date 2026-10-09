"""taotrader/core/state.py - decoded chain state. Immutable; built only by chain.reader / data.replay."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import IntEnum, IntFlag

from .fixed import DEC, floor_int
from .units import (
    PERQUINTILL, RAO_PER_TAO, AlphaRao, Block, BlockHash, Coldkey, Hotkey, NetUid, PriceRao, Rao, SubnetKey,
)


class PoolKind(IntEnum):
    CP_REAL = 1        # Era A (blocks < per-subnet v3 init): constant product on (SubnetTAO, SubnetAlphaIn), w = 0.5
    CP_V3_VIRTUAL = 2  # Era B (5,947,549 .. 8,486,593): constant product on virtual reserves (L*sqrtP, L/sqrtP)
    BALANCER = 3       # Era C (>= 8,486,594): weighted pool, weights from Swap.SwapBalancer


class Quality(IntFlag):
    OK = 0
    TA_PRICE = 1               # price from T/A in era B (biased in v3-era micro caps; mask in factor work)
    SEED_FALLBACK = 2          # Balancer seeding fell back to q = 0.5 at 8,486,594: check for a price jump
    EARLY_TINY_POOL = 4        # first weeks of dTAO, pools < 10 TAO
    CHAIN_STALL_GAP = 8        # 2025-05-20 freeze window
    DEFAULT_FILLED = 16        # an absent ValueQuery key was filled from a spec whose defaults are not validated
    REFINED = 32               # snapshot fetched by the per-block refinement pass (e.g. removal-1)
    NOT_STARTED = 64           # FirstEmissionBlockNumber is None
    CARRIED = 128              # non-hot-path fields carried forward from the last FULL read (live HEAD plan)
    NO_YIELD_IDX = 256         # no tracked earning hotkey: nominator yield unknown
    BALANCER_MIGRATION = 512   # within 60 blocks of 8,486,594


class ReadPlan(IntEnum):
    HEAD = 1   # per-block hot-path keys (pool, EMA, flags, flow, epoch); rest carried (Quality.CARRIED)
    FULL = 2   # every subnet item + globals; every 60 blocks live, every backtest snapshot


@dataclass(frozen=True, slots=True)
class PoolState:
    kind: PoolKind
    tao: Rao                 # real SubnetTAO (sizing caps, depth, dissolution pot)
    alpha: AlphaRao          # real SubnetAlphaIn
    px_tao: int              # pricing reserve y used by swap math (== tao except CP_V3_VIRTUAL)
    px_alpha: int            # pricing reserve x
    w_quote_e18: int         # Perquintill raw TAO weight (Swap.SwapBalancer.quote); 5*10**17 for CP kinds
    fee_rate: int            # Swap.FeeRate (/65535); absent -> spec default (33; 196 for specs 290-292)

    @property
    def w_base_e18(self) -> int:
        return PERQUINTILL - self.w_quote_e18

    def spot(self) -> Decimal:
        """TAO per alpha = (w_base / w_quote) * y / x. Never hard-code 0.5."""
        return DEC.divide(DEC.multiply(Decimal(self.w_base_e18), Decimal(self.px_tao)),
                          DEC.multiply(Decimal(self.w_quote_e18), Decimal(self.px_alpha)))

    def spot_rao(self) -> PriceRao:
        return PriceRao(floor_int(DEC.multiply(self.spot(), Decimal(RAO_PER_TAO))))

    def shifted(self, d_tao: int, d_alpha: int) -> PoolState:
        """Pool after a reserve change (own-impact overlay, post-trade pool). Weights unchanged (no injection)."""
        return replace(self, tao=Rao(self.tao + d_tao), alpha=AlphaRao(self.alpha + d_alpha),
                       px_tao=self.px_tao + d_tao, px_alpha=self.px_alpha + d_alpha)


@dataclass(frozen=True, slots=True)
class HotkeyIdx:
    """Hotkey share pool on one subnet generation. Position value = shares * index; yield raises the index only."""
    hotkey: Hotkey
    total_alpha: AlphaRao            # TotalHotkeyAlpha(h, n)
    total_shares: Decimal            # TotalHotkeyShares V1 (U64F64) if present at this block, else V2 SafeFloat m*10^e; exact
    take_u16: int = 11_796           # Delegates[h]; absent -> 18%
    childkey_take_u16: int = 0       # ChildkeyTake(h, n)
    earns: bool = False              # h is a key of AlphaDividendsPerSubnet(n, .) at this block
    last_dividend: AlphaRao = AlphaRao(0)   # AlphaDividendsPerSubnet(n, h): post-take nominator alpha of last epoch

    def index(self) -> Decimal:
        if self.total_shares == 0:
            return Decimal(1)
        return DEC.divide(Decimal(self.total_alpha), self.total_shares)

    def value_of(self, shares: Decimal) -> AlphaRao:
        if self.total_shares == 0:
            return AlphaRao(floor_int(shares))
        return AlphaRao(floor_int(DEC.divide(DEC.multiply(shares, Decimal(self.total_alpha)), self.total_shares)))

    def shares_for(self, alpha: int) -> Decimal:
        if self.total_alpha == 0 or self.total_shares == 0:
            return Decimal(alpha)
        return DEC.divide(DEC.multiply(Decimal(alpha), self.total_shares), Decimal(self.total_alpha))


@dataclass(frozen=True, slots=True)
class MetagraphLite:
    """Miner-quality summary for LCW gates (risk #22). Read only when lcw.enabled (get_selective_metagraph or
    storage; VERIFY item names); None otherwise."""
    n_miners: int                    # miner UIDs with incentive > 0
    n_miner_coldkeys: int            # distinct coldkeys owning those UIDs
    top1_coldkey_share_ppm: int      # largest coldkey's share of total miner incentive
    n_permit_coldkeys: int           # distinct coldkeys holding a validator permit


@dataclass(frozen=True, slots=True)
class SubnetState:
    key: SubnetKey
    pool: PoolState
    alpha_out: AlphaRao                    # SubnetAlphaOut (includes protocol + burned alpha)
    protocol_alpha: AlphaRao               # SubnetProtocolAlpha
    moving_price: Decimal                  # SubnetMovingPrice (I96F32 exact): emission share AND prune rank
    root_prop: Decimal                     # RootProp U96F32 (computed for blocks < 7,135,420)
    miner_burned: Decimal                  # MinerBurned U96F32 (0 before 8,466,597)
    emission_enabled: bool                 # SubnetEmissionEnabled (absent -> True)
    subtoken_enabled: bool                 # SubtokenEnabled (start_call done); buys need it, sells do not
    reg_allowed: bool                      # NetworkRegistrationAllowed (False freezes the EMA, leaves emit set)
    first_emission_block: Block | None     # FirstEmissionBlockNumber; None = start_call never made
    tempo: int
    last_epoch_block: Block
    ema_halving_blocks: int                # EMAPriceHalvingBlocks (201,600)
    tao_in_emission: Rao                   # SubnetTaoInEmission (per-block value)
    excess_tao: Rao                        # SubnetExcessTao (per-block chain buy)
    alpha_out_emission: AlphaRao           # SubnetAlphaOutEmission (per block)
    alpha_in_emission: AlphaRao            # SubnetAlphaInEmission (per block)
    reservoir_tao: Rao = Rao(0)            # Swap.BalancerTaoReservoir
    reservoir_alpha: AlphaRao = AlphaRao(0)
    tao_flow_cum: int | None = None        # SubnetTaoFlow i64 running total (valid >= 8,466,531, within one generation)
    volume_cum: int | None = None          # SubnetVolume u128
    fast_moving_price: Decimal | None = None   # SubnetFastMovingPrice U64F64 (basket era; cross-check only)
    owner_coldkey: Coldkey | None = None   # SubnetOwner
    owner_hotkey: Hotkey | None = None     # SubnetOwnerHotkey (item name VERIFY at build)
    owner_cut_enabled: bool | None = None  # OwnerCutEnabled[n] (default True; VERIFY)
    owner_cut_autolock: bool | None = None # OwnerCutAutoLockEnabled[n] (default False; VERIFY)
    total_alpha_staked: AlphaRao | None = None   # TotalAlphaStaked (spec >= 448); fallback alpha_out - protocol_alpha
    escrow_alpha: AlphaRao | None = None   # basket escrow alpha E on this subnet (>= 8,765,684); forward-filled
    owner_alpha: AlphaRao | None = None    # owner coldkey's alpha on the owner hotkey (position value)
    max_allowed_validators: int | None = None   # MaxAllowedValidators[n] (router permit filter, section 3.8)
    consensus_mode: int | None = None      # per-subnet consensus mode (spec 475 Null consensus; item name VERIFY)
    metagraph: MetagraphLite | None = None # LCW miner-quality inputs; None unless lcw.enabled
    hotkeys: tuple[HotkeyIdx, ...] = ()    # TRACKED hotkeys only (incl. the owner hotkey), sorted by hotkey
    quality: Quality = Quality.OK

    def hotkey(self, hk: Hotkey) -> HotkeyIdx | None:
        for h in self.hotkeys:
            if h.hotkey == hk:
                return h
        return None


@dataclass(frozen=True, slots=True)
class ChainGlobals:
    spec_version: int
    tx_version: int
    total_issuance: Rao
    block_emission: Rao                     # runtime get_block_emission / curve(TotalIssuance); NEVER BlockEmission storage
    moving_alpha: Decimal                   # SubnetMovingAlpha (I96F32; live 0.0003)
    gate_bar: Decimal                       # EmissionGateBar theta (U64F64; live 0.0082624)
    gate_rank: int                          # EmissionBarRank (absent -> 32)
    gate_exponent: int                      # EmissionGateExponent (absent -> 3)
    tao_weight: Decimal                     # TaoWeight raw / u64::MAX (live 0.18)
    root_tao: Rao                           # SubnetTAO[0]
    owner_cut_u16: int                      # SubnetOwnerCut: a GLOBAL StorageValue (absent -> 11,796)
    subnet_limit: int                       # SubnetLimit (128)
    immunity_period: int                    # NetworkImmunityPeriod (864,000)
    network_rate_limit: int                 # NetworkRateLimit (14,400)
    last_reg_block: Block                   # LastRateLimitedBlock(RateLimitKey::NetworkLastRegistered, suffix 0x02)
    last_lock_cost: Rao                     # NetworkLastLockCost
    min_lock_cost: Rao                      # NetworkMinLockCost (1 TAO)
    lock_reduction_interval: int            # NetworkLockReductionInterval (115,200)
    tao_in_refund_block: Block              # TaoInRefundDeploymentBlock (8,334,450)
    nominator_min_stake: Rao                # NominatorMinRequiredStake factor * DefaultMinStake / 1e6
    cleanup_queue_len: int                  # len(DissolveCleanupQueue)
    n_nonroot_networks: int                 # count(NetworksAdded) - 1
    safe_mode_until: Block | None           # SafeMode.EnteredUntil
    shorts_enabled: bool = False            # monitored only (long-only is enforced by the planner)
    runtime_prune_target: NetUid | None = None   # SubnetInfoRuntimeApi_get_subnet_to_prune (cross-check)


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    block: Block
    block_hash: BlockHash
    timestamp_ms: int                       # Timestamp.Now
    plan: ReadPlan
    glob: ChainGlobals
    subnets: tuple[SubnetState, ...]        # sorted by netuid; exactly one generation per netuid; root excluded.
                                            # Contains EXACTLY the non-root netuids with NetworksAdded == true: a netuid
                                            # that is removed and in cleanup, or queued and not yet added, is excluded
                                            # even while NetworkRegisteredAt / pool storage still exist (reader asserts).
    digest: str = ""                        # blake2b-128 of canonical bytes; computed once by the builder, stored in the lake
    _idx: dict[int, SubnetState] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_idx", {int(s.key.netuid): s for s in self.subnets})

    def by_netuid(self, n: int) -> SubnetState | None:
        return self._idx.get(n)

    def get(self, key: SubnetKey) -> SubnetState | None:
        """None if the netuid is gone OR now holds a different generation (our asset was dissolved)."""
        s = self._idx.get(int(key.netuid))
        return s if s is not None and s.key == key else None
