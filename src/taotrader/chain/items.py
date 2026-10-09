"""taotrader/chain/items.py - spec-aware storage item registry (DESIGN.md section 6.5).

One row = (pallet, item, key layout, decoder, expected query kind, fallback, read plan, block bounds). The reader builds
keys from the rows, decodes with the row decoder and fills absent keys by the rule of section 6.4:

- the key is present                        -> decode the bytes (an undecodable value fails the whole snapshot);
- absent, item exists in that spec's layout -> the runtime default of THAT spec (ValueQuery) or None (OptionQuery);
- absent, item not in that spec's layout    -> `fallback` (the item did not exist yet / any more at that block).

Hasher layouts are asserted against each spec's metadata (chain/metadata.py, `taotrader verify-metadata`). The rows
below were checked against the spec-348 (7,000,020), spec-441 (8,765,720) and spec-475 (9,240,388) metadata
(section 13 Q1 / Q13 / Q20 VERIFY results):

- SubnetOwnerHotkey: Map Identity(NetUid) -> AccountId32 (name confirmed; present at 348, 441, 475).
- OwnerCutEnabled / OwnerCutAutoLockEnabled: per-subnet MAPS Identity(NetUid) -> bool, ValueQuery defaults True / False
  (absent at 348, present from 441).
- SubnetOwnerCut: a GLOBAL plain u16, default 11,796 (the key is absent at 9,240,388, so the default applies).
- Delegates: Blake2_128Concat(hotkey) -> PerU16 (u16), default 11,796. ChildkeyTake: (Blake2_128Concat(hotkey),
  Identity(NetUid)) -> PerU16, default 0. AlphaV2 / Alpha: (Blake2_128Concat(hotkey), Blake2_128Concat(coldkey),
  Identity(NetUid)) -> SafeFloat / U64F64. The legacy Alpha map and TotalHotkeyShares (V1) are gone from the spec-475
  metadata (present at 348 and 441).
- EmissionBarRank: u16 (default 32). EmissionGateExponent: FixedU128<64> (U64F64; default raw = 3.0), decoded as U64F64
  and required to be integral (ADR-0001 #2: ChainGlobals.gate_exponent is an int; a fractional value fails closed).
- SafeMode.EnteredUntil: OptionQuery u32 (decoded by length, u32 or u64).
- ShortsEnabled: absent from specs 348, 441 and 475. NOT registered (ADR-0001 #3); `WATCH_ITEMS` makes
  verify-metadata flag it if a future spec adds it. ChainGlobals.shorts_enabled stays False.
- TotalIssuance: SubtensorModule.TotalIssuance (u64 TaoBalance) is the issuance the emission curve uses; the reader
  reads it. Balances.TotalIssuance also exists (a cross-check row only).
- DissolveCleanupQueue: Vec<NetUid> (u16 elements); only its length is used.
- Per-subnet consensus mode (spec 475 Null consensus): SubtensorModule.SubnetEpochConsensus, Identity(NetUid) ->
  EpochConsensus {Yuma = 0, Null = 1}, ValueQuery default Yuma (absent at 348 and 441). LiquidAlphaConsensusMode
  {Current, Previous, Auto} is unrelated.
- Metagraph (LCW): Incentive Identity(NetUidStorageIndex = netuid for mechanism 0) -> Vec<PerU16>; ValidatorPermit
  Identity(NetUid) -> Vec<bool>; Keys (Identity(NetUid), Identity(u16 uid)) -> AccountId32; Owner
  Blake2_128Concat(hotkey) -> AccountId32 (coldkey); SubnetworkN Identity(NetUid) -> u16.
- LastEpochBlock exists from spec 423; before it the epoch marker is LastMechansimStepBlock (sic; set at each drain),
  read as a legacy row only where LastEpochBlock does not exist. LastRateLimitedBlock(RateLimitKey::NetworkLastRegistered)
  (variant index 2 at 348, 441 and 475) exists from spec 273; the older NetworkLastRegistered value exists in specs
  233-306, so in the overlap the reader takes the larger of the two (both only ever increase).
- Item lifetimes over the 87 committed layouts (first .. last spec): FirstEmissionBlockNumber 257.., SubtokenEnabled
  273.., FeeRate 290.., AlphaSqrtPrice/CurrentLiquidity 290..422, SubnetTaoFlow 338.., RootProp 365..,
  TotalHotkeySharesV2/AlphaV2 401.., SubnetEmissionEnabled/SubnetExcessTao/OwnerCut* 411.., SubnetProtocolAlpha 413..,
  TaoInRefundDeploymentBlock 415.., MinerBurned 421.., SwapBalancer/LastEpochBlock 423.., reservoirs and
  DissolveCleanupQueue 432.., gate items 440/441.., TotalAlphaStaked 448.., SubnetFastMovingPrice 464..,
  SubnetEpochConsensus 475..; TotalHotkeyShares (V1) and Alpha (legacy) ..472.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any, Final

from taotrader.core.errors import DecodeError

from . import scale as sc
from .hashing import RATE_LIMIT_KEY_NETWORK_LAST_REGISTERED, Hasher, account, le16, storage_key

# Era and data-availability boundaries used by snapshot assembly (sections 6.8 / 8.6). They mirror the regime table
# owned by protocol/regimes.py (WP2), whose regime ids are not part of the section 5.12 contract; a test asserts that
# these blocks appear as regime starts there.
DTAO_LAUNCH_BLOCK: Final[int] = 4_920_351
ERA_B_FIRST_BLOCK: Final[int] = 5_947_549          # swap v3 (lazy per subnet)
TA_DIVERGENCE_BLOCK: Final[int] = 6_205_195         # T/A diverges from the v3 price after this block (TA_PRICE)
BALANCER_FIRST_BLOCK: Final[int] = 8_486_594        # era C
SEED_WINDOW_BLOCKS: Final[int] = 60                 # SEED_FALLBACK / BALANCER_MIGRATION window around era C start
CHAIN_STALL_BLOCKS: Final[tuple[int, int]] = (5_611_658, 5_611_659)   # 2025-05-20 freeze: no blocks between these
EARLY_TINY_POOL_BLOCKS: Final[int] = 201_600        # "first weeks of dTAO" (4 weeks) for EARLY_TINY_POOL
EARLY_TINY_POOL_RAO: Final[int] = 10 * 10**9        # pools < 10 TAO
DEFAULT_MAX_NETUID: Final[int] = 144                # SubnetLimit (128) + 16
NETUID_EXTENSION: Final[int] = 16


class Scope(StrEnum):
    """Key layout of a row. The reader supplies the key parts; hashers come from the row."""
    GLOBAL = "global"                    # plain value: key = prefix
    FIXED_KEY = "fixed_key"              # map read at one fixed key (SubnetTAO[0], LastRateLimitedBlock[0x02])
    SUBNET = "subnet"                    # Identity(u16 netuid)
    SWAP = "swap"                        # Twox64Concat(u16 netuid)
    HOTKEY_SUBNET = "hotkey_subnet"      # Blake2_128Concat(hotkey) ++ Identity(u16)
    SUBNET_HOTKEY = "subnet_hotkey"      # Identity(u16) ++ Blake2_128Concat(hotkey)
    HOTKEY = "hotkey"                    # Blake2_128Concat(hotkey)
    HOT_COLD_SUBNET = "hot_cold_subnet"  # Blake2_128Concat(hotkey) ++ Blake2_128Concat(coldkey) ++ Identity(u16)
    SUBNET_UID = "subnet_uid"            # Identity(u16 netuid) ++ Identity(u16 uid)


SCOPE_HASHERS: Final[dict[Scope, tuple[Hasher, ...]]] = {
    Scope.GLOBAL: (),
    Scope.FIXED_KEY: (Hasher.IDENTITY,),
    Scope.SUBNET: (Hasher.IDENTITY,),
    Scope.SWAP: (Hasher.TWOX64_CONCAT,),
    Scope.HOTKEY_SUBNET: (Hasher.BLAKE2_128_CONCAT, Hasher.IDENTITY),
    Scope.SUBNET_HOTKEY: (Hasher.IDENTITY, Hasher.BLAKE2_128_CONCAT),
    Scope.HOTKEY: (Hasher.BLAKE2_128_CONCAT,),
    Scope.HOT_COLD_SUBNET: (Hasher.BLAKE2_128_CONCAT, Hasher.BLAKE2_128_CONCAT, Hasher.IDENTITY),
    Scope.SUBNET_UID: (Hasher.IDENTITY, Hasher.IDENTITY),
}


class Query(StrEnum):
    VALUE = "Default"      # ValueQuery: absent key = the runtime default of that spec
    OPTION = "Optional"    # OptionQuery: absent key = None


class Plan(IntEnum):
    """Which read plan includes a row."""
    HEAD = 1        # every block (hot path) and every FULL read
    HELD = 2        # FULL, plus HEAD for held subnets
    FULL = 3        # FULL only (carried in HEAD snapshots)
    HOTKEY = 4      # tracked (hotkey, netuid) pairs
    OWNER = 5       # owner position (hotkey, coldkey, netuid)
    METAGRAPH = 6   # LCW miner quality, only on request
    CHECK = 7       # cross-check only (not assembled into the snapshot)


@dataclass(frozen=True, slots=True)
class Row:
    field: str                         # SubnetState / ChainGlobals / HotkeyIdx field (or an internal name)
    pallet: str
    item: str
    scope: Scope
    decoder: sc.Decoder[Any]
    plan: Plan
    query: Query = Query.VALUE
    fallback: object = None            # value when the item is not in that spec's layout
    required: bool = False             # must exist in every known layout (verify-metadata fails otherwise)
    fixed_key: bytes = b""             # the encoded key part of a FIXED_KEY row
    since_block: int | None = None     # documentation + key filter when a spec layout is unknown
    until_block: int | None = None
    legacy_of: str | None = None       # field of the primary row this row stands in for (read only where the primary
                                       # item does not exist in the spec layout)
    note: str = ""

    @property
    def name(self) -> str:
        return f"{self.pallet}.{self.item}"

    @property
    def hashers(self) -> tuple[Hasher, ...]:
        return SCOPE_HASHERS[self.scope]

    def live_at(self, block: int) -> bool:
        return (self.since_block is None or block >= self.since_block) and (self.until_block is None or block <= self.until_block)

    def decode(self, raw: bytes) -> Any:
        if self.query is Query.OPTION:
            return sc.d_option(self.decoder.fn, self.decoder.widths)(raw)
        return self.decoder(raw)

    # ------------------------------------------------------------------ keys
    def key(self, *, netuid: int | None = None, hotkey: str | None = None, coldkey: str | None = None,
            uid: int | None = None) -> bytes:
        s = self.scope
        h = self.hashers
        if s is Scope.GLOBAL:
            return storage_key(self.pallet, self.item)
        if s is Scope.FIXED_KEY:
            return storage_key(self.pallet, self.item, [(h[0], self.fixed_key)])
        if s in (Scope.SUBNET, Scope.SWAP):
            return storage_key(self.pallet, self.item, [(h[0], le16(_req(netuid)))])
        if s is Scope.HOTKEY_SUBNET:
            return storage_key(self.pallet, self.item, [(h[0], account(_req(hotkey))), (h[1], le16(_req(netuid)))])
        if s is Scope.SUBNET_HOTKEY:
            return storage_key(self.pallet, self.item, [(h[0], le16(_req(netuid))), (h[1], account(_req(hotkey)))])
        if s is Scope.HOTKEY:
            return storage_key(self.pallet, self.item, [(h[0], account(_req(hotkey)))])
        if s is Scope.HOT_COLD_SUBNET:
            return storage_key(self.pallet, self.item, [(h[0], account(_req(hotkey))), (h[1], account(_req(coldkey))),
                                                        (h[2], le16(_req(netuid)))])
        return storage_key(self.pallet, self.item, [(h[0], le16(_req(netuid))), (h[1], le16(_req(uid)))])


def _req(v: Any) -> Any:
    if v is None:
        raise ValueError("missing key part")
    return v


def _st(item: str, scope: Scope, dec: sc.Decoder[Any], plan: Plan, **kw: Any) -> Row:
    return Row(field=kw.pop("field", item), pallet="SubtensorModule", item=item, scope=scope, decoder=dec, plan=plan, **kw)


def _int_gate_exponent(b: bytes) -> int:
    """EmissionGateExponent is FixedU128<64> on chain while ChainGlobals.gate_exponent is an int: fail closed on a
    fractional value (WP0 ADR request 2)."""
    raw = sc.d_u128(b)
    if raw % (1 << 64):
        raise DecodeError(f"EmissionGateExponent is not integral: raw {raw} = {sc.d_u64f64(b)}")
    return raw >> 64


GATE_EXPONENT: Final[sc.Decoder[int]] = sc.Decoder("U64F64->int", 16, _int_gate_exponent, ("FixedU128<frac_bits=64>",))
NETWORKS_ADDED: Final[str] = "added"

# ------------------------------------------------------------------------------------------------ per subnet
SUBNET_ROWS: Final[tuple[Row, ...]] = (
    _st("NetworksAdded", Scope.SUBNET, sc.BOOL, Plan.HEAD, field=NETWORKS_ADDED, fallback=False, required=True),
    _st("NetworkRegisteredAt", Scope.SUBNET, sc.U64, Plan.HEAD, field="reg_at", fallback=0, required=True),
    _st("SubnetTAO", Scope.SUBNET, sc.U64, Plan.HEAD, field="tao", fallback=0, required=True),
    _st("SubnetAlphaIn", Scope.SUBNET, sc.U64, Plan.HEAD, field="alpha_in", fallback=0, required=True),
    _st("SubnetAlphaOut", Scope.SUBNET, sc.U64, Plan.FULL, field="alpha_out", fallback=0, required=True),
    _st("SubnetProtocolAlpha", Scope.SUBNET, sc.U64, Plan.FULL, field="protocol_alpha", fallback=0),
    _st("SubnetMovingPrice", Scope.SUBNET, sc.I96F32, Plan.HEAD, field="moving_price", fallback=Decimal(0), required=True),
    _st("SubnetFastMovingPrice", Scope.SUBNET, sc.U64F64, Plan.FULL, field="fast_moving_price", query=Query.OPTION,
        note="basket era"),
    _st("RootProp", Scope.SUBNET, sc.U96F32, Plan.FULL, field="root_prop", fallback=Decimal(0), since_block=7_135_420,
        note="reads 0 before 7,135,420; protocol.emission.root_prop computes it (the reader stores raw values only)"),
    _st("MinerBurned", Scope.SUBNET, sc.U96F32, Plan.FULL, field="miner_burned", fallback=Decimal(0), since_block=8_466_597),
    _st("SubnetEmissionEnabled", Scope.SUBNET, sc.BOOL, Plan.HEAD, field="emission_enabled", fallback=True,
        since_block=8_283_784),
    _st("SubtokenEnabled", Scope.SUBNET, sc.BOOL, Plan.HEAD, field="subtoken_enabled", fallback=True),
    _st("NetworkRegistrationAllowed", Scope.SUBNET, sc.BOOL, Plan.HEAD, field="reg_allowed", fallback=True),
    _st("FirstEmissionBlockNumber", Scope.SUBNET, sc.U64, Plan.HEAD, field="first_emission_block", query=Query.OPTION),
    _st("Tempo", Scope.SUBNET, sc.U16, Plan.FULL, field="tempo", fallback=360),
    _st("LastEpochBlock", Scope.SUBNET, sc.U64, Plan.HEAD, field="last_epoch_block", fallback=0),
    _st("LastMechansimStepBlock", Scope.SUBNET, sc.U64, Plan.HEAD, field="last_epoch_block_legacy", fallback=0,
        legacy_of="last_epoch_block", note="epoch marker of runtimes without LastEpochBlock (spec 348)"),
    _st("EMAPriceHalvingBlocks", Scope.SUBNET, sc.U64, Plan.FULL, field="ema_halving_blocks", fallback=201_600),
    _st("SubnetTaoInEmission", Scope.SUBNET, sc.U64, Plan.HELD, field="tao_in_emission", fallback=0),
    _st("SubnetExcessTao", Scope.SUBNET, sc.U64, Plan.HELD, field="excess_tao", fallback=0, since_block=8_283_784),
    _st("SubnetAlphaOutEmission", Scope.SUBNET, sc.U64, Plan.FULL, field="alpha_out_emission", fallback=0),
    _st("SubnetAlphaInEmission", Scope.SUBNET, sc.U64, Plan.FULL, field="alpha_in_emission", fallback=0),
    _st("SubnetTaoFlow", Scope.SUBNET, sc.I64, Plan.HEAD, field="tao_flow_cum", fallback=None,
        note="running total valid >= 8,466,531 within one generation; None where the item does not exist"),
    _st("SubnetVolume", Scope.SUBNET, sc.U128, Plan.FULL, field="volume_cum", fallback=None),
    _st("SubnetOwner", Scope.SUBNET, sc.ACCOUNT, Plan.FULL, field="owner_coldkey", fallback=None),
    _st("SubnetOwnerHotkey", Scope.SUBNET, sc.ACCOUNT, Plan.FULL, field="owner_hotkey", fallback=None),
    _st("OwnerCutEnabled", Scope.SUBNET, sc.BOOL, Plan.FULL, field="owner_cut_enabled", fallback=None),
    _st("OwnerCutAutoLockEnabled", Scope.SUBNET, sc.BOOL, Plan.FULL, field="owner_cut_autolock", fallback=None),
    _st("TotalAlphaStaked", Scope.SUBNET, sc.U64, Plan.FULL, field="total_alpha_staked", fallback=None,
        note="spec >= 448; where absent consumers use alpha_out - protocol_alpha"),
    _st("MaxAllowedValidators", Scope.SUBNET, sc.U16, Plan.FULL, field="max_allowed_validators", fallback=None),
    _st("SubnetEpochConsensus", Scope.SUBNET, sc.EPOCH_CONSENSUS, Plan.FULL, field="consensus_mode", fallback=None,
        note="spec 475 Null consensus: 0 = Yuma, 1 = Null"),
    Row("w_quote_e18", "Swap", "SwapBalancer", Scope.SWAP, sc.PERQUINTILL, Plan.HEAD, fallback=5 * 10**17,
        since_block=BALANCER_FIRST_BLOCK),
    Row("fee_rate", "Swap", "FeeRate", Scope.SWAP, sc.U16, Plan.HEAD, fallback=None,
        note="absent from the layout (era A) -> protocol fee_rate_default(spec) and Quality.DEFAULT_FILLED"),
    Row("reservoir_tao", "Swap", "BalancerTaoReservoir", Scope.SWAP, sc.U64, Plan.FULL, fallback=0,
        since_block=BALANCER_FIRST_BLOCK),
    Row("reservoir_alpha", "Swap", "BalancerAlphaReservoir", Scope.SWAP, sc.U64, Plan.FULL, fallback=0,
        since_block=BALANCER_FIRST_BLOCK),
    Row("v3_sqrt_price", "Swap", "AlphaSqrtPrice", Scope.SWAP, sc.U64F64_RAW, Plan.HEAD, fallback=None,
        since_block=ERA_B_FIRST_BLOCK, until_block=BALANCER_FIRST_BLOCK - 1, note="era B only"),
    Row("v3_liquidity", "Swap", "CurrentLiquidity", Scope.SWAP, sc.U64, Plan.HEAD, fallback=None,
        since_block=ERA_B_FIRST_BLOCK, until_block=BALANCER_FIRST_BLOCK - 1, note="era B only"),
)

# ------------------------------------------------------------------------------------------------ globals
GLOBAL_ROWS: Final[tuple[Row, ...]] = (
    _st("SubnetMovingAlpha", Scope.GLOBAL, sc.I96F32, Plan.HEAD, field="moving_alpha", fallback=Decimal(0), required=True),
    _st("EmissionGateBar", Scope.GLOBAL, sc.U64F64, Plan.HEAD, field="gate_bar", fallback=Decimal(0)),
    _st("EmissionBarRank", Scope.GLOBAL, sc.U16, Plan.HEAD, field="gate_rank", fallback=32),
    _st("EmissionGateExponent", Scope.GLOBAL, GATE_EXPONENT, Plan.HEAD, field="gate_exponent", fallback=3),
    _st("EmissionBarQuantile", Scope.GLOBAL, sc.U64F64, Plan.CHECK, field="gate_quantile", fallback=None,
        note="logged only (ignored in rank mode)"),
    _st("TaoWeight", Scope.GLOBAL, sc.TAO_WEIGHT, Plan.HEAD, field="tao_weight", fallback=Decimal(0), required=True),
    _st("SubnetOwnerCut", Scope.GLOBAL, sc.U16, Plan.HEAD, field="owner_cut_u16", fallback=11_796),
    _st("SubnetLimit", Scope.GLOBAL, sc.U16, Plan.HEAD, field="subnet_limit", fallback=128),
    _st("NetworkImmunityPeriod", Scope.GLOBAL, sc.U64, Plan.HEAD, field="immunity_period", fallback=0, required=True),
    _st("NetworkRateLimit", Scope.GLOBAL, sc.U64, Plan.HEAD, field="network_rate_limit", fallback=0, required=True),
    _st("NetworkLastLockCost", Scope.GLOBAL, sc.U64, Plan.HEAD, field="last_lock_cost", fallback=0, required=True),
    _st("NetworkMinLockCost", Scope.GLOBAL, sc.U64, Plan.HEAD, field="min_lock_cost", fallback=0, required=True),
    _st("NetworkLockReductionInterval", Scope.GLOBAL, sc.U64, Plan.HEAD, field="lock_reduction_interval", fallback=0,
        required=True),
    _st("LastRateLimitedBlock", Scope.FIXED_KEY, sc.U64, Plan.HEAD, field="last_reg_block", fallback=None,
        fixed_key=RATE_LIMIT_KEY_NETWORK_LAST_REGISTERED,
        note="RateLimitKey::NetworkLastRegistered (variant 2); older runtimes: NetworkLastRegistered"),
    _st("NetworkLastRegistered", Scope.GLOBAL, sc.U64, Plan.HEAD, field="last_reg_block_legacy", fallback=None,
        note="specs 233-306 (overlaps LastRateLimitedBlock from 273): last_reg_block = max of the two"),
    _st("TaoInRefundDeploymentBlock", Scope.GLOBAL, sc.U64, Plan.HEAD, field="tao_in_refund_block", fallback=0),
    _st("NominatorMinRequiredStake", Scope.GLOBAL, sc.U64, Plan.HEAD, field="nominator_min_factor", fallback=0),
    _st("TotalIssuance", Scope.GLOBAL, sc.U64, Plan.HEAD, field="total_issuance", fallback=None, required=True),
    _st("DissolveCleanupQueue", Scope.GLOBAL, sc.VEC_LEN, Plan.HEAD, field="cleanup_queue_len", fallback=0),
    _st("SubnetTAO", Scope.FIXED_KEY, sc.U64, Plan.HEAD, field="root_tao", fallback=0, fixed_key=le16(0), required=True),
    Row("balances_total_issuance", "Balances", "TotalIssuance", Scope.GLOBAL, sc.U64, Plan.CHECK, fallback=None),
    Row("safe_mode_until", "SafeMode", "EnteredUntil", Scope.GLOBAL, sc.BLOCKNUM, Plan.HEAD, query=Query.OPTION,
        fallback=None),
    Row("timestamp_ms", "Timestamp", "Now", Scope.GLOBAL, sc.U64, Plan.HEAD, fallback=0, required=True),
    Row("system_number", "System", "Number", Scope.GLOBAL, sc.U32, Plan.HEAD, fallback=None, required=True),
)

# ------------------------------------------------------------------------------------------------ hotkey panel
HOTKEY_ROWS: Final[tuple[Row, ...]] = (
    _st("TotalHotkeyAlpha", Scope.HOTKEY_SUBNET, sc.U64, Plan.HOTKEY, field="total_alpha", fallback=0, required=True),
    _st("TotalHotkeyShares", Scope.HOTKEY_SUBNET, sc.U64F64, Plan.HOTKEY, field="shares_v1", fallback=None,
        note="V1; removed from the metadata at v473"),
    _st("TotalHotkeySharesV2", Scope.HOTKEY_SUBNET, sc.SAFEFLOAT, Plan.HOTKEY, field="shares_v2", fallback=None,
        since_block=8_036_577),
    _st("AlphaDividendsPerSubnet", Scope.SUBNET_HOTKEY, sc.U64, Plan.HOTKEY, field="last_dividend", fallback=None,
        note="present key => earns"),
    _st("Delegates", Scope.HOTKEY, sc.U16, Plan.HOTKEY, field="take_u16", fallback=11_796),
    _st("ChildkeyTake", Scope.HOTKEY_SUBNET, sc.U16, Plan.HOTKEY, field="childkey_take_u16", fallback=0),
)

OWNER_ROWS: Final[tuple[Row, ...]] = (
    _st("AlphaV2", Scope.HOT_COLD_SUBNET, sc.SAFEFLOAT, Plan.OWNER, field="owner_shares_v2", fallback=None,
        since_block=8_036_577),
    _st("Alpha", Scope.HOT_COLD_SUBNET, sc.U64F64, Plan.OWNER, field="owner_shares_legacy", fallback=None,
        until_block=9_217_507, note="legacy wins in the overlap"),
)

METAGRAPH_ROWS: Final[tuple[Row, ...]] = (
    _st("SubnetworkN", Scope.SUBNET, sc.U16, Plan.METAGRAPH, field="n_uids", fallback=0),
    _st("Incentive", Scope.SUBNET, sc.VEC_U16, Plan.METAGRAPH, field="incentive", fallback=(),
        note="keyed by NetUidStorageIndex (= netuid for mechanism 0)"),
    _st("ValidatorPermit", Scope.SUBNET, sc.VEC_BOOL, Plan.METAGRAPH, field="validator_permit", fallback=()),
    _st("Keys", Scope.SUBNET_UID, sc.ACCOUNT, Plan.METAGRAPH, field="uid_hotkey", fallback=None),
    _st("Owner", Scope.HOTKEY, sc.ACCOUNT, Plan.METAGRAPH, field="hotkey_owner", fallback=None),
)

ALL_ROWS: Final[tuple[Row, ...]] = SUBNET_ROWS + GLOBAL_ROWS + HOTKEY_ROWS + OWNER_ROWS + METAGRAPH_ROWS


def rows_by_field(rows: tuple[Row, ...]) -> dict[str, Row]:
    out: dict[str, Row] = {}
    for r in rows:
        if r.field in out:
            raise ValueError(f"duplicate registry field {r.field}")
        out[r.field] = r
    return out


SUBNET: Final[dict[str, Row]] = rows_by_field(SUBNET_ROWS)
GLOBAL: Final[dict[str, Row]] = rows_by_field(GLOBAL_ROWS)
HOTKEY: Final[dict[str, Row]] = rows_by_field(HOTKEY_ROWS)
OWNER: Final[dict[str, Row]] = rows_by_field(OWNER_ROWS)
METAGRAPH: Final[dict[str, Row]] = rows_by_field(METAGRAPH_ROWS)


# Items deliberately NOT registered whose appearance in a future spec must be flagged by verify-metadata so they are
# added on purpose (ADR-0001 #3), not silently ignored.
WATCH_ITEMS: Final[tuple[str, ...]] = ("SubtensorModule.ShortsEnabled",)


def primary_of(row: Row) -> Row | None:
    """The primary row a legacy row stands in for (None for a primary row)."""
    if row.legacy_of is None:
        return None
    for table in (SUBNET, GLOBAL, HOTKEY, OWNER, METAGRAPH):
        if row.legacy_of in table:
            return table[row.legacy_of]
    raise KeyError(f"{row.name}: legacy_of {row.legacy_of!r} names no registry field")


def item_names() -> tuple[str, ...]:
    """Every (pallet.item) the registry touches plus the watch list, sorted (the set chain/metadata.py extracts per
    spec)."""
    return tuple(sorted({r.name for r in ALL_ROWS} | set(WATCH_ITEMS)))


def plan_rows(rows: tuple[Row, ...], include: Callable[[Row], bool]) -> tuple[Row, ...]:
    return tuple(r for r in rows if include(r))
