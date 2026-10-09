"""taotrader/live/sdk_port.py - the SDK seam of the live adapter (WP11; DESIGN.md sections 9.2, 9.4).

`SdkPort` hides the bittensor 11.3.0 client shape (blocking vs async, brief open question 1) behind one async Protocol,
so `LiveVenue`, preflight and reconciliation are written and tested against `FakeSdkPort` without the SDK. `RealSdk` is
the ONLY code that imports bittensor (`load_bittensor()`, called lazily from its constructor): importing
`taotrader.live.*` on Windows, where bittensor cannot be installed, works, and every non-RealSdk test runs there.

Amounts are always exact integers (rao for TAO, alpha rao for alpha). `LiveCall` refuses 'all', u64::MAX,
REMOVE_STAKE_FULL_LIMIT (call 103: LiveVenue maps it to RemoveStakeLimit with the exact alpha read at the submit head)
and MOVE_STAKE_LIMIT (disabled in v1). The only intents RealSdk constructs are AddStakeLimit, RemoveStakeLimit and
MoveStake; each under its own per-call Policy (a per-call policy REPLACES the client policy, brief 5.7):
- buys: Policy(max_fee_tao, max_spend_tao=LiveCfg.max_order_tao, allowed_netuids=LiveCfg.allowed_netuids or None,
  allow_raw_calls=False);
- sells and same-subnet moves: Policy(max_fee_tao, max_spend_tao=None, allowed_netuids=allowed + held, allow_raw_calls=False)
  (a sell spends alpha, not TAO; a full exit is never blocked by a buy-size cap).

Additions to the section 9.4 surface (WP11-owned; recorded in the WP report):
- `quote(call)` returns (amount_out, spot_rao): the runtime quote's output (0 = the all-zero failure) and the head
  spot in rao per alpha (`client.prices.alpha_price`), from which LiveVenue tightens limits and checks crossing;
- `sdk_version()`, `delegate_ss58(name)`, `free_balance(ss58)` and `metadata_indices()` (V6 call indices and the
  ProxyType::Staking index) serve preflight; `account(block_hash, ss58)` (System.Account free and nonce at a block)
  serves reconciliation;
- event fields are normalised: an error is fields["error"] (decoded name, pallet prefix allowed), a proxied result is
  ProxyExecuted fields["result"] in {"Ok", "Err"} plus fields["error"], fees are TransactionFeePaid fields["actual_fee"]
  (rao), stake events carry "tao"/"alpha"/"netuid"/"hotkey"/"coldkey", and account_events add "extrinsic_hash" (the
  hash of the extrinsic that emitted the event, "" for non-extrinsic phases) so reconciliation can tell own events from
  foreign ones.

Every RealSdk call below follows brief 5.2-5.9 and is UNEXECUTED on this host: the Linux contract test
(tests/live/test_sdk_contract.py, skipped where bittensor is absent) and the user-run test.finney procedure VERIFY it.
"""
from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Context, Decimal
from typing import Any, Final, Protocol

from ..chain.hashing import Hasher, account, storage_key, to_hex
from ..core.config import LiveCfg
from ..core.orders import FailReason, OrderKind
from ..core.protocols import SwapSim
from ..core.state import PoolState
from ..core.units import RAO_PER_TAO, U64_MAX, AlphaRao, Block, BlockHash, NetUid, Rao
from ..protocol.amm import SwapError, quote_buy, quote_sell

__all__ = [
    "ALLOWED_KINDS", "CHAIN_ERRORS", "COLDKEY_SWAP_ITEMS", "EXPECTED_INDICES", "PLAIN_PERIOD", "PROXY_TYPE_STAKING",
    "REAL_PAYS_FEE_ITEM", "SDK_VERSION", "SHIELD_PERIOD",
    "FakeSdkPort", "LiveCall", "LiveCallError", "LiveReader", "PolicySpec", "PostState", "RealSdk", "SdkPort",
    "SdkResultError", "block_hash_of", "block_of_hash", "check_policy", "coldkey_swap_keys", "error_reason", "intent_spec",
    "load_bittensor", "policy_for", "real_pays_fee_keys", "ss58_decode", "ss58_encode",
]

SDK_VERSION: Final[str] = "11.3.0"
SS58_FORMAT: Final[int] = 42                     # Bittensor / generic Substrate address format
SHIELD_PERIOD: Final[int] = 8                    # MEV_SHIELD_ERA_PERIOD (CheckMortality <= 8; brief 5.9)
PLAIN_PERIOD: Final[int] = 16                    # unshielded era-16 risk-exit fallback (section 3.12)
PROXY_TYPE_STAKING: Final[str] = "Staking"       # ProxyType::Staking, index 8 (brief 5.8)
ALLOWED_KINDS: Final[frozenset[OrderKind]] = frozenset(
    {OrderKind.ADD_STAKE_LIMIT, OrderKind.REMOVE_STAKE_LIMIT, OrderKind.MOVE_STAKE})
# V6 (section 9.5): call indices 88/89/103/85/149/90 and ProxyType::Staking = 8 must be unchanged (metadata).
EXPECTED_INDICES: Final[Mapping[str, int]] = {
    "SubtensorModule.add_stake_limit": 88,
    "SubtensorModule.remove_stake_limit": 89,
    "SubtensorModule.remove_stake_full_limit": 103,
    "SubtensorModule.move_stake": 85,
    "SubtensorModule.move_stake_limit": 149,
    "SubtensorModule.swap_stake_limit": 90,
    "ProxyType.Staking": 8,
}
# Chain dispatch-error names that map one-to-one onto a FailReason (anything else: OTHER, or PROXY_ERROR when proxied).
_NOT_CHAIN: Final[frozenset[FailReason]] = frozenset({FailReason.PROXY_ERROR, FailReason.SHIELD_MISSED, FailReason.ERA_EXPIRED,
                                                      FailReason.NOT_PLACED, FailReason.VENUE_REJECT, FailReason.OTHER})
CHAIN_ERRORS: Final[Mapping[str, FailReason]] = {r.value: r for r in FailReason if r not in _NOT_CHAIN}
_EXACT: Final[Context] = Context(prec=200)
# Storage read by key PRESENCE (section 13 Q19, resolved from the committed spec-475 metadata,
# tests/fixtures/golden/metadata_storage_spec475.json; the live host re-checks with verify-metadata):
# - Proxy.RealPaysFeeConsentV1: (Twox64Concat AccountId32, Twox64Concat AccountId32) -> (), OptionQuery: a present key is
#   the consent set by Proxy.set_real_pays_fee. The key order (real, delegate) is unverified, so BOTH orders are read
#   and either one counts (fail closed).
# - SubtensorModule.ColdkeySwapAnnouncements (Twox64Concat AccountId32 -> (u32, H256)) and ColdkeySwapDisputes
#   (-> u32), both OptionQuery: an announced (scheduled) or disputed swap of the real coldkey. ColdkeySwapScheduled is the
#   pre-475 name (present at spec 348), read as well so older runtimes are covered.
REAL_PAYS_FEE_ITEM: Final[tuple[str, str]] = ("Proxy", "RealPaysFeeConsentV1")
COLDKEY_SWAP_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("SubtensorModule", "ColdkeySwapAnnouncements"), ("SubtensorModule", "ColdkeySwapDisputes"),
    ("SubtensorModule", "ColdkeySwapScheduled"))


class LiveCallError(ValueError):
    """A LiveCall that live must never send ('all', u64::MAX, call 103, a disabled kind, a malformed amount)."""


class SdkResultError(RuntimeError):
    """The SDK returned a result LiveVenue cannot interpret (e.g. no carrier nonce). Raised from a write, it makes the
    Runner journal SubmitUnknown, and resolve() decides from chain truth."""


# ------------------------------------------------------------------------------------------------- ss58
_B58: Final[str] = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def _ss58_checksum(data: bytes) -> bytes:
    return hashlib.blake2b(b"SS58PRE" + data, digest_size=64).digest()[:2]


def ss58_encode(pubkey_hex: str, ss58_format: int = SS58_FORMAT) -> str:
    """"0x" + 64 hex (core Hotkey/Coldkey encoding) -> SS58 address. Only formats < 64 (one prefix byte)."""
    raw = bytes.fromhex(pubkey_hex[2:] if pubkey_hex.startswith("0x") else pubkey_hex)
    if len(raw) != 32 or not 0 <= ss58_format < 64:
        raise ValueError("ss58_encode needs a 32-byte public key and a format < 64")
    data = bytes([ss58_format]) + raw
    return _b58encode(data + _ss58_checksum(data))


def ss58_decode(address: str, ss58_format: int | None = SS58_FORMAT) -> str:
    """SS58 address -> "0x" + 64 hex. Verifies the checksum (and the format unless ss58_format is None)."""
    raw = _b58decode(address)
    if len(raw) != 35 or raw[0] >= 64:
        raise ValueError(f"not a one-byte-prefix SS58 account address: {address!r}")
    data, check = raw[:33], raw[33:]
    if _ss58_checksum(data) != check:
        raise ValueError(f"bad SS58 checksum: {address!r}")
    if ss58_format is not None and raw[0] != ss58_format:
        raise ValueError(f"SS58 format {raw[0]} != {ss58_format}: {address!r}")
    return "0x" + data[1:].hex()


def real_pays_fee_keys(real_ss58: str, delegate_ss58: str) -> tuple[str, str]:
    """Raw storage keys of RealPaysFeeConsentV1 for (real, delegate) and (delegate, real)."""
    r, d = account(ss58_decode(real_ss58)), account(ss58_decode(delegate_ss58))
    t = Hasher.TWOX64_CONCAT
    return (to_hex(storage_key(*REAL_PAYS_FEE_ITEM, [(t, r), (t, d)])),
            to_hex(storage_key(*REAL_PAYS_FEE_ITEM, [(t, d), (t, r)])))


def coldkey_swap_keys(real_ss58: str) -> tuple[str, ...]:
    """Raw storage keys whose presence means a scheduled (announced) or disputed swap of the real coldkey."""
    r = account(ss58_decode(real_ss58))
    return tuple(to_hex(storage_key(p, i, [(Hasher.TWOX64_CONCAT, r)])) for p, i in COLDKEY_SWAP_ITEMS)


def block_hash_of(block: int) -> BlockHash:
    """The synthetic block hash used by FakeSdkPort and the tests ("0x" + 64-hex block number, as tests/conftest)."""
    return BlockHash("0x" + f"{block:064x}")


def block_of_hash(block_hash: str) -> int:
    return int(block_hash, 16)


# ------------------------------------------------------------------------------------------------- calls and policy
@dataclass(frozen=True, slots=True)
class LiveCall:
    """An OrderIntent resolved by LiveVenue to exact amounts at the submit head. Never 'all' or u64::MAX."""
    kind: OrderKind                       # REMOVE_STAKE_FULL_LIMIT is mapped to REMOVE_STAKE_LIMIT before this point
    hotkey_ss58: str                      # origin hotkey
    netuid: int
    amount: int                           # buy: rao; sell/move: exact alpha rao (full exits: PostState.alpha_value)
    limit_price_rao: int                  # 0 for MOVE_STAKE
    allow_partial: bool
    dest_hotkey_ss58: str | None
    max_spend_tao: float | None           # buy: LiveCfg.max_order_tao; sell/move: None
    allowed_netuids: tuple[int, ...] | None   # buy: LiveCfg.allowed_netuids (None if empty); sell/move: that + held netuids

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise LiveCallError(f"{self.kind.value} is never sent live (only add_stake_limit, remove_stake_limit, move_stake)")
        if type(self.amount) is not int or not 0 < self.amount < U64_MAX:
            raise LiveCallError(f"live amounts are exact integers in (0, u64::MAX): got {self.amount!r}")
        if type(self.limit_price_rao) is not int or not 0 <= self.limit_price_rao < U64_MAX:
            raise LiveCallError(f"bad limit price {self.limit_price_rao!r}")
        if not 0 < self.netuid < 2**16:
            raise LiveCallError(f"bad netuid {self.netuid}")
        if self.kind is OrderKind.MOVE_STAKE:
            if self.limit_price_rao != 0 or not self.dest_hotkey_ss58 or self.dest_hotkey_ss58 == self.hotkey_ss58:
                raise LiveCallError("MOVE_STAKE needs limit 0 and a different destination hotkey")
            if self.max_spend_tao is not None:
                raise LiveCallError("moves spend alpha: max_spend_tao must be None")
        elif self.kind is OrderKind.ADD_STAKE_LIMIT:
            if self.limit_price_rao <= 0 or self.dest_hotkey_ss58 is not None:
                raise LiveCallError("buys need a limit > 0 and no destination")
            if self.max_spend_tao is None or not self.max_spend_tao > 0:
                raise LiveCallError("buys run under a bounded max_spend_tao")
        else:
            if self.limit_price_rao <= 0 or self.dest_hotkey_ss58 is not None:
                raise LiveCallError("sells need a limit > 0 and no destination")
            if self.max_spend_tao is not None:
                raise LiveCallError("sells spend alpha: max_spend_tao must be None")

    @property
    def is_buy(self) -> bool:
        return self.kind is OrderKind.ADD_STAKE_LIMIT


@dataclass(frozen=True, slots=True)
class PostState:
    """Chain state at one block (client.at(block)); fills are built from share deltas, never value deltas."""
    block: int
    real_free_rao: int                    # free TAO of the real coldkey
    shares: Decimal                       # real coldkey's shares on (hotkey, netuid): AlphaV2 SafeFloat (legacy Alpha wins)
    hk_total_alpha: int                   # TotalHotkeyAlpha(hotkey, netuid)
    hk_total_shares: Decimal              # TotalHotkeyShares V1 if present, else V2
    alpha_value: int                      # chain-computed stake value (sizes exact full exits; fill cross-check only)
    delegate_free_rao: int
    delegate_nonce: int                   # System.Account(delegate).nonce at this block


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """The per-call bt.Policy fields (brief 5.7). Built only by policy_for()."""
    max_fee_tao: float | None
    max_spend_tao: float | None
    allowed_netuids: tuple[int, ...] | None
    allow_raw_calls: bool = False


def policy_for(call: LiveCall, max_fee_tao: float) -> PolicySpec:
    """The per-call Policy: buys bounded by max_spend_tao; sells and moves with max_spend_tao=None (section 9.4)."""
    return PolicySpec(max_fee_tao=max_fee_tao, max_spend_tao=call.max_spend_tao if call.is_buy else None,
                      allowed_netuids=call.allowed_netuids, allow_raw_calls=False)


def check_policy(spec: PolicySpec, call: LiveCall, fee_rao: int | None) -> list[str]:
    """Local replica of the client-side Policy.check semantics (brief 5.7): per intent, not cumulative; an unknown fee
    with max_fee_tao set fails closed; a TAO spend above max_spend_tao and a netuid outside allowed_netuids violate.
    FakeSdkPort's plan() uses it; LiveVenue never relies on it alone (it enforces its own caps first)."""
    out: list[str] = []
    if spec.allow_raw_calls:
        out.append("policy: raw calls must stay disabled")
    if spec.max_fee_tao is not None:
        if fee_rao is None:
            out.append("policy: fee estimate unavailable with max_fee_tao set")
        elif Decimal(fee_rao) > _EXACT.multiply(Decimal(repr(spec.max_fee_tao)), Decimal(RAO_PER_TAO)):
            out.append(f"policy: fee {fee_rao} rao > max_fee_tao {spec.max_fee_tao}")
    if call.is_buy and spec.max_spend_tao is not None and Decimal(call.amount) > _EXACT.multiply(
            Decimal(repr(spec.max_spend_tao)), Decimal(RAO_PER_TAO)):
        out.append(f"policy: spend {call.amount} rao > max_spend_tao {spec.max_spend_tao}")
    if spec.allowed_netuids is not None and call.netuid not in spec.allowed_netuids:
        out.append(f"policy: netuid {call.netuid} not in allowed_netuids")
    return out


def error_reason(name: str, *, proxied: bool) -> FailReason:
    """Map a decoded dispatch-error name ("SlippageTooHigh", "SubtensorModule.SlippageTooHigh", "Swap::PriceLimitExceeded")
    to the matching FailReason; unknown names give PROXY_ERROR (proxied inner error) or OTHER."""
    short = name.replace("::", ".").rsplit(".", 1)[-1].strip()
    hit = CHAIN_ERRORS.get(short)
    if hit is not None:
        return hit
    return FailReason.PROXY_ERROR if proxied else FailReason.OTHER


# ------------------------------------------------------------------------------------------------- the seams
class LiveReader(Protocol):
    """The part of the WP1 ChainReader (core.protocols.ChainReader) the live adapter reads; JsonRpcChainReader conforms."""
    async def block_hash(self, block: Block) -> BlockHash: ...
    async def finalized_head(self) -> tuple[Block, BlockHash]: ...
    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]: ...
    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None: ...


class SdkPort(Protocol):
    # --- reads
    async def proxies(self, real_ss58: str) -> list[tuple[str, str, int]]: ...        # (delegate, proxy type name, delay)
    async def proxy_announcements(self, real_ss58: str) -> list[tuple[str, str, int]]: ...   # (delegate, call_hash, height)
    async def coldkey_swap_scheduled(self, real_ss58: str) -> bool: ...
    async def real_pays_fee(self, real_ss58: str, delegate_ss58: str) -> bool: ...
    async def locked_alpha(self, real_ss58: str, netuid: int) -> int: ...
    async def next_index(self, delegate_ss58: str) -> int: ...                        # pool-aware system_accountNextIndex
    async def quote(self, call: LiveCall) -> tuple[int, int]: ...                      # (amount_out, head spot rao/alpha)
    async def plan(self, call: LiveCall, delegate: str) -> tuple[list[str], int]: ...  # (violations, fee_rao)
    async def post_state(self, block_hash: str, real_ss58: str, hotkey_ss58: str, netuid: int,
                         delegate_ss58: str) -> PostState: ...
    async def block_extrinsics(self, block_hash: str) -> list[tuple[int, str, str | None, int | None]]: ...
    async def extrinsic_events(self, block_hash: str, index: int) -> list[tuple[str, str, dict[str, object]]]: ...
    async def account_events(self, block_hash: str, coldkey_ss58: str) -> list[tuple[str, str, dict[str, object]]]: ...
    async def free_balance(self, ss58: str) -> int: ...                               # free TAO at the head
    async def account(self, block_hash: str, ss58: str) -> tuple[int, int]: ...       # (free rao, nonce) at a block
    async def metadata_indices(self) -> dict[str, int]: ...                            # V6: see EXPECTED_INDICES
    def sdk_version(self) -> str: ...
    def delegate_ss58(self, delegate: str) -> str: ...                                # delegate wallet name -> address
    # --- writes (LiveVenue in submit mode only)
    async def submit_shielded(self, call: LiveCall, delegate: str) -> tuple[str, str, int, int]: ...
        # -> (carrier_hash, inner_hash, submit_head_block, carrier_nonce)
    async def submit_plain(self, call: LiveCall, delegate: str) -> tuple[str, int, int]: ...
        # risk-exit fallback ONLY -> (ext_hash, submit_head_block, nonce)


# ------------------------------------------------------------------------------------------------- FakeSdkPort
Event = tuple[str, str, dict[str, object]]


@dataclass
class _Timeline:
    points: list[tuple[int, Any]] = field(default_factory=list)

    def set(self, block: int, value: Any) -> None:
        self.points = sorted([p for p in self.points if p[0] != block] + [(block, value)], key=lambda p: p[0])

    def at(self, block: int, default: Any) -> Any:
        out = default
        for b, v in self.points:
            if b > block:
                break
            out = v
        return out


class FakeSdkPort:
    """An in-memory chain behind the SdkPort protocol (section 10.5: drives every live test without the SDK).

    State is kept as per-key timelines (the latest value at or before a block wins), so post_state at any block is
    well defined. Block hashes are block_hash_of(block). Writes are recorded in `submitted`; `submit_script` (results or
    exceptions, consumed in order) overrides the default result, whose carrier nonce is the pool-aware next index.
    `include_shielded()` / `include_plain()` script an inclusion with its events, fees and nonce advance. Set
    `forbid_writes` to make any write raise (dev/CI processes)."""

    def __init__(self, *, real: str, delegates: Mapping[str, str], version: str = SDK_VERSION, max_fee_tao: float = 0.005,
                 indices: Mapping[str, int] | None = None) -> None:
        self.real = real
        self.delegate_addr: dict[str, str] = dict(delegates)
        self.version = version
        self.max_fee_tao = max_fee_tao
        self.indices: dict[str, int] = dict(EXPECTED_INDICES if indices is None else indices)
        self.head = 0                                          # best head (submit head of the next submission)
        self.calls: list[str] = []
        self.submitted: list[tuple[str, LiveCall, str]] = []
        self.submit_script: list[tuple[str, str, int, int] | tuple[str, int, int] | BaseException] = []
        self.forbid_writes = False
        self.fail: dict[str, BaseException] = {}               # method name -> exception raised on every call
        self.proxy_list: list[tuple[str, str, int]] = [(addr, PROXY_TYPE_STAKING, 0) for addr in self.delegate_addr.values()]
        self.announcements: list[tuple[str, str, int]] = []
        self.swap_scheduled = False
        self.pays_fee: dict[str, bool] = {}
        self.locks: dict[int, int] = {}
        self.pools: dict[int, PoolState] = {}
        self.quote_override: dict[int, tuple[int, int]] = {}
        self.plan_violations: list[str] = []
        self.plan_fee_rao = 1_028_000
        self.extrinsics: dict[int, list[tuple[int, str, str | None, int | None]]] = {}
        self.events: dict[tuple[int, int], list[Event]] = {}
        self.acct_events: dict[int, list[Event]] = {}
        self.pool_extra: dict[str, int] = {}                   # pending (pool) nonces above the on-chain nonce
        self._tl: dict[tuple[object, ...], _Timeline] = {}
        self._seq = 0

    # ---------------------------------------------------------------- scripting helpers
    def _line(self, *key: object) -> _Timeline:
        return self._tl.setdefault(key, _Timeline())

    def set_account(self, block: int, ss58: str, *, free: int | None = None, nonce: int | None = None) -> None:
        if free is not None:
            self._line("free", ss58).set(block, free)
        if nonce is not None:
            self._line("nonce", ss58).set(block, nonce)

    def set_stake(self, block: int, hotkey: str, netuid: int, *, shares: Decimal | None = None,
                  hk_alpha: int | None = None, hk_shares: Decimal | None = None, coldkey: str | None = None) -> None:
        if shares is not None:
            self._line("shares", coldkey or self.real, hotkey, netuid).set(block, shares)
        if hk_alpha is not None:
            self._line("hk_alpha", hotkey, netuid).set(block, hk_alpha)
        if hk_shares is not None:
            self._line("hk_shares", hotkey, netuid).set(block, hk_shares)

    def free_at(self, block: int, ss58: str) -> int:
        return int(self._line("free", ss58).at(block, 0))

    def nonce_at(self, block: int, ss58: str) -> int:
        return int(self._line("nonce", ss58).at(block, 0))

    def shares_at(self, block: int, hotkey: str, netuid: int, coldkey: str | None = None) -> Decimal:
        return Decimal(self._line("shares", coldkey or self.real, hotkey, netuid).at(block, Decimal(0)))

    def new_hash(self, tag: str) -> str:
        self._seq += 1
        return "0x" + hashlib.blake2b(f"{tag}:{self._seq}".encode(), digest_size=32).hexdigest()

    def add_extrinsic(self, block: int, ext_hash: str, signer: str | None, nonce: int | None,
                      events: Sequence[Event] = ()) -> int:
        lst = self.extrinsics.setdefault(block, [])
        idx = len(lst)
        lst.append((idx, ext_hash, signer, nonce))
        self.events[(block, idx)] = list(events)
        return idx

    def include_shielded(self, block: int, carrier_hash: str, inner_hash: str, delegate_ss58: str, nonce: int, *,
                         inner: bool = True, inner_events: Sequence[Event] = (), carrier_fee: int = 94_560,
                         inner_fee: int = 933_081, filler: int = 0) -> tuple[int, int | None]:
        """Script the carrier (nonce n) at `block`, followed by the decrypted inner (nonce n + 1) unless `inner` is False.
        Advances the delegate's on-chain nonce and debits its fees at `block`. Returns (carrier index, inner index)."""
        for _ in range(filler):
            self.add_extrinsic(block, self.new_hash("foreign"), None, None, [("System", "ExtrinsicSuccess", {})])
        c = self.add_extrinsic(block, carrier_hash, delegate_ss58, nonce, [
            ("MevShield", "EncryptedSubmitted", {"who": delegate_ss58}),
            ("TransactionPayment", "TransactionFeePaid", {"who": delegate_ss58, "actual_fee": carrier_fee, "tip": 0}),
            ("System", "ExtrinsicSuccess", {})])
        fee = carrier_fee
        i: int | None = None
        if inner:
            evs: list[Event] = list(inner_events) or [("Proxy", "ProxyExecuted", {"result": "Ok"}),
                                                     ("System", "ExtrinsicSuccess", {})]
            evs.append(("TransactionPayment", "TransactionFeePaid", {"who": delegate_ss58, "actual_fee": inner_fee, "tip": 0}))
            i = self.add_extrinsic(block, inner_hash, delegate_ss58, nonce + 1, evs)
            fee += inner_fee
        self.set_account(block, delegate_ss58, nonce=nonce + (2 if inner else 1),
                         free=self.free_at(block - 1, delegate_ss58) - fee)
        return c, i

    def include_plain(self, block: int, ext_hash: str, delegate_ss58: str, nonce: int, *, events: Sequence[Event] = (),
                      fee: int = 837_000) -> int:
        evs: list[Event] = list(events) or [("Proxy", "ProxyExecuted", {"result": "Ok"}), ("System", "ExtrinsicSuccess", {})]
        evs.append(("TransactionPayment", "TransactionFeePaid", {"who": delegate_ss58, "actual_fee": fee, "tip": 0}))
        idx = self.add_extrinsic(block, ext_hash, delegate_ss58, nonce, evs)
        self.set_account(block, delegate_ss58, nonce=nonce + 1, free=self.free_at(block - 1, delegate_ss58) - fee)
        return idx

    def _check(self, name: str) -> None:
        self.calls.append(name)
        exc = self.fail.get(name)
        if exc is not None:
            raise exc

    # ---------------------------------------------------------------- reads
    async def proxies(self, real_ss58: str) -> list[tuple[str, str, int]]:
        self._check("proxies")
        return list(self.proxy_list)

    async def proxy_announcements(self, real_ss58: str) -> list[tuple[str, str, int]]:
        self._check("proxy_announcements")
        return list(self.announcements)

    async def coldkey_swap_scheduled(self, real_ss58: str) -> bool:
        self._check("coldkey_swap_scheduled")
        return self.swap_scheduled

    async def real_pays_fee(self, real_ss58: str, delegate_ss58: str) -> bool:
        self._check("real_pays_fee")
        return self.pays_fee.get(delegate_ss58, False)

    async def locked_alpha(self, real_ss58: str, netuid: int) -> int:
        self._check("locked_alpha")
        return self.locks.get(netuid, 0)

    async def next_index(self, delegate_ss58: str) -> int:
        self._check("next_index")
        return self.nonce_at(self.head, delegate_ss58) + self.pool_extra.get(delegate_ss58, 0)

    async def quote(self, call: LiveCall) -> tuple[int, int]:
        self._check("quote")
        if call.netuid in self.quote_override:
            return self.quote_override[call.netuid]
        pool = self.pools.get(call.netuid)
        if pool is None:
            return (0, 0)
        spot = int(pool.spot_rao())
        if call.kind is OrderKind.MOVE_STAKE:
            return (call.amount, spot)
        try:
            q = quote_buy(pool, Rao(call.amount)) if call.is_buy else quote_sell(pool, AlphaRao(call.amount))
        except SwapError:
            return (0, spot)
        return (q.amount_out, spot)

    async def plan(self, call: LiveCall, delegate: str) -> tuple[list[str], int]:
        self._check("plan")
        out = check_policy(policy_for(call, self.max_fee_tao), call, self.plan_fee_rao)
        return (out + list(self.plan_violations), self.plan_fee_rao)

    async def post_state(self, block_hash: str, real_ss58: str, hotkey_ss58: str, netuid: int,
                         delegate_ss58: str) -> PostState:
        self._check("post_state")
        b = block_of_hash(block_hash)
        shares = self.shares_at(b, hotkey_ss58, netuid, real_ss58)
        hk_alpha = int(self._line("hk_alpha", hotkey_ss58, netuid).at(b, 0))
        hk_shares = Decimal(self._line("hk_shares", hotkey_ss58, netuid).at(b, Decimal(0)))
        value = 0 if hk_shares == 0 else int(_EXACT.divide(_EXACT.multiply(shares, Decimal(hk_alpha)), hk_shares))
        return PostState(block=b, real_free_rao=self.free_at(b, real_ss58), shares=shares, hk_total_alpha=hk_alpha,
                         hk_total_shares=hk_shares, alpha_value=value, delegate_free_rao=self.free_at(b, delegate_ss58),
                         delegate_nonce=self.nonce_at(b, delegate_ss58))

    async def block_extrinsics(self, block_hash: str) -> list[tuple[int, str, str | None, int | None]]:
        self._check("block_extrinsics")
        return list(self.extrinsics.get(block_of_hash(block_hash), []))

    async def extrinsic_events(self, block_hash: str, index: int) -> list[Event]:
        self._check("extrinsic_events")
        return list(self.events.get((block_of_hash(block_hash), index), []))

    async def account_events(self, block_hash: str, coldkey_ss58: str) -> list[Event]:
        self._check("account_events")
        return list(self.acct_events.get(block_of_hash(block_hash), []))

    async def free_balance(self, ss58: str) -> int:
        self._check("free_balance")
        return self.free_at(self.head, ss58)

    async def account(self, block_hash: str, ss58: str) -> tuple[int, int]:
        self._check("account")
        b = block_of_hash(block_hash)
        return self.free_at(b, ss58), self.nonce_at(b, ss58)

    async def metadata_indices(self) -> dict[str, int]:
        self._check("metadata_indices")
        return dict(self.indices)

    def sdk_version(self) -> str:
        return self.version

    def delegate_ss58(self, delegate: str) -> str:
        return self.delegate_addr[delegate]

    # ---------------------------------------------------------------- writes
    def _write(self, kind: str, call: LiveCall, delegate: str) -> object | None:
        self._check(kind)
        if self.forbid_writes:
            raise RuntimeError("FakeSdkPort: writes are forbidden in this process")
        self.submitted.append((kind, call, delegate))
        return self.submit_script.pop(0) if self.submit_script else None

    async def submit_shielded(self, call: LiveCall, delegate: str) -> tuple[str, str, int, int]:
        scripted = self._write("submit_shielded", call, delegate)
        addr = self.delegate_addr[delegate]
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            res = scripted
            assert isinstance(res, tuple) and len(res) == 4
            self.pool_extra[addr] = self.pool_extra.get(addr, 0) + 2
            return (str(res[0]), str(res[1]), int(res[2]), int(res[3]))
        n = await self.next_index(addr)
        self.pool_extra[addr] = self.pool_extra.get(addr, 0) + 2
        return (self.new_hash("carrier"), self.new_hash("inner"), self.head, n)

    async def submit_plain(self, call: LiveCall, delegate: str) -> tuple[str, int, int]:
        scripted = self._write("submit_plain", call, delegate)
        addr = self.delegate_addr[delegate]
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            res = scripted
            assert isinstance(res, tuple) and len(res) == 3
            self.pool_extra[addr] = self.pool_extra.get(addr, 0) + 1
            return (str(res[0]), int(res[1]), int(res[2]))
        n = await self.next_index(addr)
        self.pool_extra[addr] = self.pool_extra.get(addr, 0) + 1
        return (self.new_hash("plain"), self.head, n)

    def settle_pool(self, delegate_ss58: str) -> None:
        """Forget pending pool nonces (the scripted inclusions moved the on-chain nonce)."""
        self.pool_extra.pop(delegate_ss58, None)


# ------------------------------------------------------------------------------------------------- RealSdk
def load_bittensor() -> Any:
    """The ONE `import bittensor` of the code base (section 9.8 #2). Raises ImportError where it is not installed
    (native Windows): live submit runs on Linux/WSL only."""
    import bittensor  # deliberately lazy: Windows processes must never import it

    return bittensor


def intent_spec(call: LiveCall) -> tuple[str, dict[str, Any]]:
    """(bt intent class name, keyword arguments) for a LiveCall (section 9.4 intent mapping). Only three classes are
    ever named; amounts are exact rao integers, wrapped as Balances by RealSdk."""
    if call.kind is OrderKind.ADD_STAKE_LIMIT:
        return ("AddStakeLimit", {"hotkey_ss58": call.hotkey_ss58, "netuid": call.netuid, "amount_tao": call.amount,
                                  "limit_price_rao": call.limit_price_rao, "allow_partial": call.allow_partial})
    if call.kind is OrderKind.REMOVE_STAKE_LIMIT:
        return ("RemoveStakeLimit", {"hotkey_ss58": call.hotkey_ss58, "netuid": call.netuid, "amount_alpha": call.amount,
                                     "limit_price_rao": call.limit_price_rao, "allow_partial": call.allow_partial})
    if call.kind is OrderKind.MOVE_STAKE:
        return ("MoveStake", {"origin_hotkey_ss58": call.hotkey_ss58, "origin_netuid": call.netuid,
                              "dest_hotkey_ss58": call.dest_hotkey_ss58, "dest_netuid": call.netuid,
                              "amount_alpha": call.amount})
    raise LiveCallError(f"{call.kind.value} has no live intent")      # unreachable: LiveCall validates the kind


_INTENT_CLASSES: Final[frozenset[str]] = frozenset({"AddStakeLimit", "RemoveStakeLimit", "MoveStake"})


async def _aw(x: Any) -> Any:
    """Await `x` if the SDK returned an awaitable (the async client), else return it (a blocking shape)."""
    return await x if inspect.isawaitable(x) else x


def _rao(x: Any) -> int:
    """A Balance (.rao) or an integer-like value -> int rao."""
    if x is None:
        return 0
    r = getattr(x, "rao", None)
    return int(r if r is not None else x)


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, Mapping) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _value(x: Any) -> Any:
    """Unwrap a substrate ScaleType (.value) or return the plain value."""
    return getattr(x, "value", x)


def _safe_float(v: Any) -> Decimal:
    """SafeFloat {mantissa, exponent} = m * 10^e exactly; U64F64 raw bits; or a plain number."""
    v = _value(v)
    if v is None:
        return Decimal(0)
    if isinstance(v, Mapping) and "mantissa" in v:
        return _EXACT.scaleb(Decimal(int(v["mantissa"])), int(v.get("exponent", 0)))
    if isinstance(v, Mapping) and "bits" in v:
        return _EXACT.divide(Decimal(int(v["bits"])), Decimal(2**64))
    if isinstance(v, int):
        return Decimal(v)
    return Decimal(str(v))


class RealSdk:
    """SdkPort over the bittensor 11.3.0 client (async form `bt.Client`; blocking results are tolerated via _aw).

    Construct on the live host only; `connect()` opens the client. Wallets are the delegate wallets named in
    LiveCfg.delegate_wallets; their keys are loaded and used by the SDK itself (BT_WALLET_PASSWORD_FILE), never read
    by this code. No method sets allow_raw_calls, uses 'all', u64::MAX or call 103, or names any intent class other than
    AddStakeLimit, RemoveStakeLimit and MoveStake (tests/live/test_static_live.py)."""

    def __init__(self, live: LiveCfg, *, network: str | None = None, bt_module: Any | None = None,
                 endpoint: str | None = None) -> None:
        self._bt = load_bittensor() if bt_module is None else bt_module
        self.live = live
        self.network = network or live.network
        self.endpoint = endpoint
        self.real = live.real_coldkey_ss58
        self._client: Any = None
        self._wallets: dict[str, Any] = {}

    # ---------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        bt = self._bt
        net = self.endpoint or self.network
        self._client = await _aw(bt.Client(network=net, policy=None, fallback_endpoints=[], archive_endpoints=None))

    async def close(self) -> None:
        if self._client is not None:
            closer = getattr(self._client, "close", None)
            if closer is not None:
                await _aw(closer())
            self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("RealSdk.connect() first")
        return self._client

    @property
    def substrate(self) -> Any:
        return _get(self.client, "substrate", "_substrate")

    def _wallet(self, delegate: str) -> Any:
        if delegate not in self.live.delegate_wallets:
            raise ValueError(f"unknown delegate wallet {delegate!r}")
        w = self._wallets.get(delegate)
        if w is None:
            w = self._wallets[delegate] = self._bt.Wallet(name=delegate)
        return w

    def _balance(self, rao: int, netuid: int) -> Any:
        return self._bt.rao(rao, netuid)

    def _intent(self, call: LiveCall) -> Any:
        name, kwargs = intent_spec(call)
        if name not in _INTENT_CLASSES:
            raise LiveCallError(f"refusing intent {name}")
        if call.kind is OrderKind.ADD_STAKE_LIMIT:
            kwargs["amount_tao"] = self._balance(call.amount, 0)
        else:
            kwargs["amount_alpha"] = self._balance(call.amount, call.netuid)
        return getattr(self._bt, name)(**kwargs)

    def _policy(self, call: LiveCall) -> Any:
        spec = policy_for(call, self.live.max_fee_tao)
        return self._bt.Policy(max_fee_tao=spec.max_fee_tao, max_spend_tao=spec.max_spend_tao,
                               allowed_netuids=list(spec.allowed_netuids) if spec.allowed_netuids is not None else None,
                               allow_raw_calls=False)

    async def _query(self, pallet: str, item: str, params: Sequence[Any], block_hash: str | None = None) -> Any:
        storage = getattr(getattr(self._bt.storage, pallet), item)
        if block_hash is None:
            return _value(await _aw(self.client.query(storage, list(params))))
        at = await _aw(self.client.at(block_hash))
        return _value(await _aw(at.query(storage, list(params))))

    # ---------------------------------------------------------------- reads
    async def proxies(self, real_ss58: str) -> list[tuple[str, str, int]]:
        res = await _aw(self.client.balances.proxies(coldkey_ss58=real_ss58))
        rows = _get(res, "proxies", default=res) or []
        out: list[tuple[str, str, int]] = []
        for p in rows:
            p = _value(p)
            out.append((str(_get(p, "delegate", "delegate_ss58")), str(_get(p, "proxy_type", "type")),
                        int(_get(p, "delay", default=0))))
        return out

    async def proxy_announcements(self, real_ss58: str) -> list[tuple[str, str, int]]:
        out: list[tuple[str, str, int]] = []
        res = await _aw(self.client.query_map(self._bt.storage.Proxy.Announcements))
        for key, val in (res.items() if isinstance(res, Mapping) else res):
            delegate = str(_value(key))
            anns = _value(val)
            items = anns[0] if isinstance(anns, (list, tuple)) and anns and isinstance(anns[0], (list, tuple)) else anns
            for a in items or []:
                a = _value(a)
                if str(_get(a, "real")) == real_ss58:
                    out.append((delegate, str(_get(a, "call_hash")), int(_get(a, "height", default=0))))
        return out

    async def _present(self, key_hex: str) -> bool:
        """state_getStorage at the head: a present key (even a unit value, "0x") is True; absent (null) is False."""
        res = await _aw(self.substrate.rpc_request("state_getStorage", [key_hex]))
        return _get(res, "result", default=res) is not None

    async def coldkey_swap_scheduled(self, real_ss58: str) -> bool:
        return any([await self._present(k) for k in coldkey_swap_keys(real_ss58)])

    async def real_pays_fee(self, real_ss58: str, delegate_ss58: str) -> bool:
        return any([await self._present(k) for k in real_pays_fee_keys(real_ss58, delegate_ss58)])

    async def locked_alpha(self, real_ss58: str, netuid: int) -> int:
        res = await _aw(self.client.staking.stake_availability(real_ss58, netuid))
        return _rao(_get(res, "locked", default=0))

    async def next_index(self, delegate_ss58: str) -> int:
        res = await _aw(self.substrate.rpc_request("system_accountNextIndex", [delegate_ss58]))
        return int(_get(res, "result", default=res))

    async def quote(self, call: LiveCall) -> tuple[int, int]:
        px = await _aw(self.client.prices.alpha_price(call.netuid))
        spot = int(_get(px, "price_rao", default=0))
        if call.kind is OrderKind.MOVE_STAKE:
            return (call.amount, spot)
        if call.is_buy:
            q = await _aw(self.client.prices.quote_stake(call.netuid, self._balance(call.amount, 0)))
            return (_rao(_get(q, "alpha")), spot)
        q = await _aw(self.client.prices.quote_unstake(call.netuid, self._balance(call.amount, call.netuid)))
        return (_rao(_get(q, "tao")), spot)

    async def plan(self, call: LiveCall, delegate: str) -> tuple[list[str], int]:
        p = await _aw(self.client.plan(self._intent(call), self._wallet(delegate), policy=self._policy(call),
                                       proxy_for=self.real, proxy_type=PROXY_TYPE_STAKING))
        return ([str(v) for v in (_get(p, "violations", default=[]) or [])], _rao(_get(p, "fee")))

    async def post_state(self, block_hash: str, real_ss58: str, hotkey_ss58: str, netuid: int,
                         delegate_ss58: str) -> PostState:
        real_acct = await self._query("System", "Account", [real_ss58], block_hash)
        del_acct = await self._query("System", "Account", [delegate_ss58], block_hash)
        legacy = await self._query("SubtensorModule", "Alpha", [hotkey_ss58, real_ss58, netuid], block_hash)
        shares = _safe_float(legacy) if legacy not in (None, 0) else _safe_float(
            await self._query("SubtensorModule", "AlphaV2", [hotkey_ss58, real_ss58, netuid], block_hash))
        hk_alpha = int(_value(await self._query("SubtensorModule", "TotalHotkeyAlpha", [hotkey_ss58, netuid], block_hash))
                       or 0)
        v1 = await self._query("SubtensorModule", "TotalHotkeyShares", [hotkey_ss58, netuid], block_hash)
        hk_shares = _safe_float(v1) if v1 not in (None, 0) else _safe_float(
            await self._query("SubtensorModule", "TotalHotkeySharesV2", [hotkey_ss58, netuid], block_hash))
        at = await _aw(self.client.at(block_hash))
        value = _rao(await _aw(at.staking.get(real_ss58, hotkey_ss58, netuid)))
        return PostState(block=int(_get(at, "block", "block_number", default=0) or 0),
                         real_free_rao=int(_get(_get(real_acct, "data", default={}), "free", default=0)),
                         shares=shares, hk_total_alpha=hk_alpha, hk_total_shares=hk_shares, alpha_value=value,
                         delegate_free_rao=int(_get(_get(del_acct, "data", default={}), "free", default=0)),
                         delegate_nonce=int(_get(del_acct, "nonce", default=0)))

    async def block_extrinsics(self, block_hash: str) -> list[tuple[int, str, str | None, int | None]]:
        blk = await _aw(self.substrate.get_block(block_hash=block_hash))
        out: list[tuple[int, str, str | None, int | None]] = []
        for i, ext in enumerate(_get(blk, "extrinsics", default=[]) or []):
            v = _value(ext) or {}
            h = _get(ext, "extrinsic_hash", default=None) or _get(v, "extrinsic_hash", default="")
            h = h.hex() if isinstance(h, bytes) else str(h)
            addr = _get(v, "address", default=None)
            nonce = _get(v, "nonce", default=None)
            out.append((i, h if h.startswith("0x") else "0x" + h, None if addr is None else str(addr),
                        None if nonce is None else int(nonce)))
        return out

    async def _events(self, block_hash: str) -> list[tuple[int | None, Event]]:
        raw = await _aw(self.substrate.get_events(block_hash=block_hash))
        out: list[tuple[int | None, Event]] = []
        for e in raw or []:
            v = _value(e) or {}
            idx = _get(v, "extrinsic_idx", default=None)
            ev = _get(v, "event", default=v)
            attrs = _get(ev, "attributes", default={}) or {}
            name = str(_get(ev, "event_id", default=""))
            fields = _event_fields(name, attrs)
            if name == "ExtrinsicFailed" or (name == "ProxyExecuted" and "result" in fields):
                fields = _normalise_dispatch(name, fields)
            if name == "TransactionFeePaid":
                fields["actual_fee"] = int(_get(fields, "actual_fee", default=0) or 0)
            out.append((None if idx is None else int(idx), (str(_get(ev, "module_id", default="")), name, fields)))
        return out

    async def extrinsic_events(self, block_hash: str, index: int) -> list[Event]:
        return [ev for i, ev in await self._events(block_hash) if i == index]

    async def account_events(self, block_hash: str, coldkey_ss58: str) -> list[Event]:
        exts = {i: h for i, h, _, _ in await self.block_extrinsics(block_hash)}
        pub = bytes.fromhex(ss58_decode(coldkey_ss58)[2:])
        out: list[Event] = []
        for i, (pallet, name, fields) in await self._events(block_hash):
            if _mentions(fields, coldkey_ss58, pub):
                out.append((pallet, name, {**fields, "extrinsic_hash": exts.get(i, "") if i is not None else ""}))
        return out

    async def free_balance(self, ss58: str) -> int:
        acct = await self._query("System", "Account", [ss58])
        return int(_get(_get(acct, "data", default={}), "free", default=0))

    async def account(self, block_hash: str, ss58: str) -> tuple[int, int]:
        acct = await self._query("System", "Account", [ss58], block_hash)
        return int(_get(_get(acct, "data", default={}), "free", default=0)), int(_get(acct, "nonce", default=0))

    async def metadata_indices(self) -> dict[str, int]:
        md = await _aw(self.substrate.get_metadata())
        out: dict[str, int] = {}
        for name in EXPECTED_INDICES:
            pallet, item = name.split(".", 1)
            if pallet == "ProxyType":
                out[name] = int(_proxy_type_index(md, item))
            else:
                call = await _aw(self.substrate.get_metadata_call_function(pallet, item))
                out[name] = int(_get(call, "index", default=_get(_value(call), "index", default=-1)))
        return out

    def sdk_version(self) -> str:
        return str(getattr(self._bt, "__version__", ""))

    def delegate_ss58(self, delegate: str) -> str:
        return str(self._wallet(delegate).coldkeypub.ss58_address)

    # ---------------------------------------------------------------- writes
    async def submit_shielded(self, call: LiveCall, delegate: str) -> tuple[str, str, int, int]:
        res = await _aw(self.client.submit_shielded(self._intent(call), self._wallet(delegate), policy=self._policy(call),
                                                    proxy_for=self.real, proxy_type=PROXY_TYPE_STAKING,
                                                    period=SHIELD_PERIOD, wait_for_inclusion=False,
                                                    wait_for_finalization=False))
        data = _get(res, "data", default={}) or {}
        carrier = _get(res, "extrinsic_hash", "extrinsic_id", default=None) or _get(data, "carrier_extrinsic_hash")
        inner = _get(data, "inner_extrinsic_hash", default=None)
        head = _get(data, "submit_block", "block_number", default=None)
        nonce = _get(data, "nonce", "carrier_nonce", default=None)
        if carrier is None or inner is None or head is None or nonce is None:
            raise SdkResultError("submit_shielded result lacks carrier hash, inner hash, submit head or nonce "
                                 "(section 13 Q4): resolve() decides from chain truth")
        return (str(carrier), str(inner), int(head), int(nonce))

    async def submit_plain(self, call: LiveCall, delegate: str) -> tuple[str, int, int]:
        res = await _aw(self.client.execute(self._intent(call), self._wallet(delegate), policy=self._policy(call),
                                            proxy_for=self.real, proxy_type=PROXY_TYPE_STAKING, period=PLAIN_PERIOD,
                                            wait_for_inclusion=False, wait_for_finalization=False, retries=0))
        data = _get(res, "data", default={}) or {}
        ext = _get(res, "extrinsic_hash", "extrinsic_id", default=None)
        head = _get(data, "submit_block", "block_number", default=None)
        nonce = _get(data, "nonce", default=None)
        if ext is None or head is None or nonce is None:
            raise SdkResultError("execute result lacks extrinsic hash, submit head or nonce: resolve() decides")
        return (str(ext), int(head), int(nonce))


# Subtensor stake events are POSITIONAL tuples (brief 6.4: StakeAdded/StakeRemoved(cold, hot, tao, alpha, netuid, fee),
# StakeMoved(cold, hot_o, netuid_o, hot_d, netuid_d, tao)); async-substrate-interface decodes them as a tuple/list of
# attributes. They are named here so LiveVenue reads "tao"/"alpha" (same-block apportioning) and reconciliation sees the
# coldkey. Other positional events keep only "args" (account_events still matches the coldkey inside them).
_POSITIONAL_EVENTS: Final[Mapping[str, tuple[str, ...]]] = {
    "StakeAdded": ("coldkey", "hotkey", "tao", "alpha", "netuid", "fee"),
    "StakeRemoved": ("coldkey", "hotkey", "tao", "alpha", "netuid", "fee"),
    "StakeMoved": ("coldkey", "hotkey", "netuid", "dest_hotkey", "dest_netuid", "tao"),
}
_INT_FIELDS: Final[frozenset[str]] = frozenset({"tao", "alpha", "netuid", "fee", "dest_netuid"})


def _as_int(x: Any) -> Any:
    v = _value(x)
    r = getattr(v, "rao", None)
    try:
        return int(r if r is not None else v)
    except (TypeError, ValueError):
        return v


def _event_fields(name: str, attrs: Any) -> dict[str, object]:
    """Event attributes as a field dict: named attributes as they are; positional ones as {"args": [...]} plus, for the
    stake events above, their names."""
    if isinstance(attrs, Mapping):
        return dict(attrs)
    if isinstance(attrs, (list, tuple)):
        out: dict[str, object] = {"args": list(attrs)}
        names = _POSITIONAL_EVENTS.get(name)
        if names is not None and len(attrs) == len(names):
            for k, v in zip(names, attrs, strict=True):
                out[k] = _as_int(v) if k in _INT_FIELDS else _value(v)
        return out
    return {"args": attrs}


def _mentions(x: Any, ss58: str, pub: bytes, depth: int = 0) -> bool:
    """Whether a decoded event value names the account: as SS58, as "0x" hex, or as raw 32 bytes, at any nesting."""
    if depth > 8:
        return False
    x = _value(x)
    if isinstance(x, str):
        return x == ss58 or x.lower() == "0x" + pub.hex()
    if isinstance(x, (bytes, bytearray)):
        return bytes(x) == pub
    if isinstance(x, Mapping):
        return any(_mentions(v, ss58, pub, depth + 1) for v in x.values())
    if isinstance(x, (list, tuple)):
        if len(x) == len(pub) and all(isinstance(b, int) and 0 <= b < 256 for b in x):
            return bytes(x) == pub
        return any(_mentions(v, ss58, pub, depth + 1) for v in x)
    return False


def _normalise_dispatch(name: str, fields: dict[str, object]) -> dict[str, object]:
    """ExtrinsicFailed{dispatch_error} / ProxyExecuted{result: Ok|Err(e)} -> fields with "result" and a decoded "error"."""
    out = dict(fields)
    raw = fields.get("dispatch_error") if name == "ExtrinsicFailed" else fields.get("result")
    if isinstance(raw, Mapping) and ("Err" in raw or "Ok" in raw):
        if "Ok" in raw:
            out["result"] = "Ok"
            return out
        raw = raw["Err"]
        out["result"] = "Err"
    elif raw == "Ok":
        out["result"] = "Ok"
        return out
    else:
        out["result"] = "Err"
    out["error"] = _error_text(raw)
    return out


def _error_text(raw: object) -> str:
    if isinstance(raw, Mapping):
        mod = raw.get("Module")
        if isinstance(mod, Mapping):
            return str(mod.get("name") or mod.get("error") or mod)
        for k, v in raw.items():
            return f"{k}.{_error_text(v)}" if isinstance(v, Mapping) else str(k if v is None else v)
    return str(raw)


def _proxy_type_index(metadata: Any, variant: str) -> int:
    """Index of `variant` in the runtime's ProxyType enum (V6). VERIFY the metadata walk on the live host."""
    finder = getattr(metadata, "get_type_variant_index", None)
    if finder is not None:
        return int(finder("ProxyType", variant))
    raise SdkResultError("cannot read the ProxyType enum from the metadata: V6 fails closed")


def with_amount(call: LiveCall, amount: int) -> LiveCall:
    """The same call with another exact amount (used by tests and the contract test)."""
    return replace(call, amount=amount)
