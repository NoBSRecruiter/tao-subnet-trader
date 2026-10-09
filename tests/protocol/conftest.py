"""WP2 test helpers: decode the WP0 golden fixtures (raw SCALE hex) into core state types.

The chain reader (WP1) is built in parallel, so these tests carry a minimal, test-only decoder for the storage
items they need. Absent keys take the ValueQuery default of the nearest captured runtime metadata
(metadata_storage_spec{348,441,475}.json). Helpers are exposed as fixtures because test modules cannot import each
other (pytest --import-mode=importlib).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.core.fixed import EXACT
from taotrader.core.state import (
    ChainGlobals,
    ChainSnapshot,
    HotkeyIdx,
    PoolKind,
    PoolState,
    ReadPlan,
    SubnetState,
)
from taotrader.core.units import (
    PERQUINTILL,
    AlphaRao,
    Block,
    BlockHash,
    Coldkey,
    Hotkey,
    NetUid,
    Rao,
    SubnetKey,
)
from taotrader.protocol.emission import alpha_issuance, block_emission_for_issuance, root_prop
from taotrader.protocol.regimes import fee_rate_default

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
U64_MAX = 2**64 - 1


# ------------------------------------------------------------------------------------------------- SCALE
def le(h: str | None, signed: bool = False) -> int:
    assert h is not None
    return int.from_bytes(bytes.fromhex(h[2:]), "little", signed=signed)


def fixed(h: str, frac_bits: int, signed: bool = False) -> Decimal:
    """I96F32 / U96F32 / U64F64 exactly (2**-k terminates)."""
    return EXACT.divide(Decimal(le(h, signed)), Decimal(2**frac_bits))


def safefloat(h: str) -> Decimal:
    """share_pool::SafeFloat {mantissa u128, exponent i64} = m * 10**e, exact."""
    b = bytes.fromhex(h[2:])
    m = int.from_bytes(b[:16], "little")
    e = int.from_bytes(b[16:24], "little", signed=True)
    return Decimal(f"{m}E{e}")


def compact_len(h: str) -> int:
    b = bytes.fromhex(h[2:])
    mode = b[0] & 3
    if mode == 0:
        return b[0] >> 2
    if mode == 1:
        return int.from_bytes(b[:2], "little") >> 2
    if mode == 2:
        return int.from_bytes(b[:4], "little") >> 2
    raise ValueError("big compact")


def account(h: str) -> str:
    return "0x" + h[2:].lower()


# ------------------------------------------------------------------------------------------------- metadata defaults
_META: dict[int, dict[str, Any]] = {}


def meta_default(item: str, spec: int) -> str | None:
    """ValueQuery default hex for 'Pallet.Item' at the nearest captured spec <= spec (348, 441, 475)."""
    specs = (348, 441, 475)
    use = max((s for s in specs if s <= spec), default=348)
    if use not in _META:
        _META[use] = json.loads((GOLDEN / f"metadata_storage_spec{use}.json").read_text(encoding="utf-8"))
    pallet, name = item.split(".", 1)
    entry = _META[use]["pallets"].get(pallet, {}).get(name)
    if entry is None or entry.get("modifier") != "Default":
        return None
    return str(entry["default"])


# ------------------------------------------------------------------------------------------------- golden snapshot
@dataclass
class GoldenSnap:
    raw: dict[str, Any]
    _storage: dict[tuple[str, str], str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for it in self.raw["storage"]:
            self._storage[(it["item"], json.dumps(it["args"]))] = it["value"]

    @property
    def block(self) -> int:
        return int(self.raw["block"])

    @property
    def spec(self) -> int:
        return int(self.raw["spec_version"])

    @property
    def tx(self) -> int:
        return int(self.raw["transaction_version"])

    def has(self, item: str, *args: Any) -> bool:
        return (item, json.dumps(list(args))) in self._storage

    def get(self, item: str, *args: Any) -> str | None:
        """Raw value (None if absent) from the per-key rows, falling back to the bulk per-netuid table."""
        k = (item, json.dumps(list(args)))
        if k in self._storage:
            return self._storage[k]
        bulk = self.raw.get("storage_by_netuid") or {}
        if item in bulk and len(args) == 1 and isinstance(args[0], int):
            vals = bulk[item]["values"]
            return vals[args[0]] if args[0] < len(vals) else None
        raise KeyError(f"{item}{list(args)} not captured at {self.block}")

    def value(self, item: str, *args: Any) -> str | None:
        """Raw value or the metadata default when absent."""
        v = self.get(item, *args)
        return v if v is not None else meta_default(item, self.spec)

    def runtime(self, method: str, **args: Any) -> str | None:
        for r in self.raw.get("runtime_api", []):
            if r["method"] == method and r["args"] == args:
                return r.get("result")
        raise KeyError(method)

    def bulk_netuids(self) -> list[int]:
        bulk = self.raw.get("storage_by_netuid") or {}
        added = bulk.get("SubtensorModule.NetworksAdded")
        if added is None:
            return []
        return [n for n, v in enumerate(added["values"]) if n != 0 and v is not None and le(v) == 1]

    def dividend_hotkeys(self, netuid: int) -> list[str]:
        return sorted({it["args"][1] for it in self.raw["storage"]
                       if it["item"] == "SubtensorModule.AlphaDividendsPerSubnet" and it["args"][0] == netuid})


def _u(gs: GoldenSnap, item: str, *args: Any, default: int = 0, signed: bool = False) -> int:
    v = gs.value(item, *args)
    return default if v is None else le(v, signed)


def build_globals(gs: GoldenSnap, n_nonroot: int | None = None) -> ChainGlobals:
    sm = "SubtensorModule."
    try:
        be_hex = gs.runtime("SubnetInfoRuntimeApi_get_block_emission")
        block_emission = le(be_hex) if be_hex else None
    except KeyError:
        block_emission = None
    issuance = _u(gs, sm + "TotalIssuance")
    if block_emission is None:
        block_emission = block_emission_for_issuance(issuance)
    expo_raw = _u(gs, sm + "EmissionGateExponent", default=3 << 64)
    assert expo_raw % (1 << 64) == 0, "non-integral EmissionGateExponent"
    rank_hex = gs.value(sm + "EmissionBarRank")
    lrb = gs.get(sm + "LastRateLimitedBlock", "NetworkLastRegistered")
    cq = gs.value(sm + "DissolveCleanupQueue")
    sm_until = gs.get("SafeMode.EnteredUntil")
    if n_nonroot is None:
        nets = gs.bulk_netuids()
        n_nonroot = len(nets) if nets else 128
    bar = gs.value(sm + "EmissionGateBar")
    tw = gs.value(sm + "TaoWeight")
    assert tw is not None
    ma = gs.value(sm + "SubnetMovingAlpha")
    assert ma is not None
    return ChainGlobals(
        spec_version=gs.spec, tx_version=gs.tx, total_issuance=Rao(issuance), block_emission=Rao(block_emission),
        moving_alpha=fixed(ma, 32, signed=True), gate_bar=fixed(bar, 64) if bar else Decimal(0),
        gate_rank=le(rank_hex) if rank_hex else 32, gate_exponent=expo_raw >> 64,
        tao_weight=EXACT.divide(Decimal(le(tw)), Decimal(U64_MAX)),
        root_tao=Rao(_u(gs, sm + "SubnetTAO", 0)), owner_cut_u16=_u(gs, sm + "SubnetOwnerCut", default=11_796),
        subnet_limit=_u(gs, sm + "SubnetLimit", default=128),
        immunity_period=_u(gs, sm + "NetworkImmunityPeriod"), network_rate_limit=_u(gs, sm + "NetworkRateLimit"),
        last_reg_block=Block(le(lrb) if lrb else 0), last_lock_cost=Rao(_u(gs, sm + "NetworkLastLockCost")),
        min_lock_cost=Rao(_u(gs, sm + "NetworkMinLockCost")),
        lock_reduction_interval=_u(gs, sm + "NetworkLockReductionInterval"),
        tao_in_refund_block=Block(_u(gs, sm + "TaoInRefundDeploymentBlock")),
        nominator_min_stake=Rao(_u(gs, sm + "NominatorMinRequiredStake") * 2_000_000 // 1_000_000),
        cleanup_queue_len=compact_len(cq) if cq else 0, n_nonroot_networks=n_nonroot,
        safe_mode_until=Block(le(sm_until)) if sm_until else None)


def _opt(gs: GoldenSnap, item: str, n: int) -> str | None:
    try:
        return gs.get(item, n)
    except KeyError:
        return None


def build_hotkeys(gs: GoldenSnap, netuid: int, extra: Sequence[str] = ()) -> tuple[HotkeyIdx, ...]:
    """HotkeyIdx for every captured dividend recipient of `netuid` (plus `extra` hotkeys if captured)."""
    sm = "SubtensorModule."
    out: list[HotkeyIdx] = []
    for hk in sorted(set(gs.dividend_hotkeys(netuid)) | set(extra)):
        if not gs.has(sm + "TotalHotkeyAlpha", hk, netuid):
            continue
        v1 = gs.get(sm + "TotalHotkeyShares", hk, netuid) if gs.has(sm + "TotalHotkeyShares", hk, netuid) else None
        v2 = gs.get(sm + "TotalHotkeySharesV2", hk, netuid) if gs.has(sm + "TotalHotkeySharesV2", hk, netuid) else None
        shares = fixed(v1, 64) if v1 else (safefloat(v2) if v2 else Decimal(0))
        div = gs.get(sm + "AlphaDividendsPerSubnet", netuid, hk) if gs.has(sm + "AlphaDividendsPerSubnet", netuid, hk) else None
        take = gs.get(sm + "Delegates", hk) if gs.has(sm + "Delegates", hk) else None
        ck = gs.get(sm + "ChildkeyTake", hk, netuid) if gs.has(sm + "ChildkeyTake", hk, netuid) else None
        out.append(HotkeyIdx(hotkey=Hotkey(hk), total_alpha=AlphaRao(le(gs.get(sm + "TotalHotkeyAlpha", hk, netuid) or "0x00")),
                             total_shares=shares, take_u16=le(take) if take else 11_796,
                             childkey_take_u16=le(ck) if ck else 0, earns=div is not None,
                             last_dividend=AlphaRao(le(div) if div else 0)))
    return tuple(out)


def build_subnet(gs: GoldenSnap, n: int, glob: ChainGlobals, *, with_hotkeys: bool = False,
                 escrow_alpha: int | None = None) -> SubnetState:
    sm, sw = "SubtensorModule.", "Swap."

    def u(item: str, default: int = 0, signed: bool = False) -> int:
        v = _opt(gs, item, n)
        if v is None:
            v = meta_default(item, gs.spec)
        return default if v is None else le(v, signed)

    def fx(item: str, bits: int, signed: bool = False) -> Decimal:
        v = _opt(gs, item, n)
        if v is None:
            v = meta_default(item, gs.spec)
        return fixed(v, bits, signed) if v else Decimal(0)

    tao, alpha = u(sm + "SubnetTAO"), u(sm + "SubnetAlphaIn")
    sqrt_p, liq = _opt(gs, sw + "AlphaSqrtPrice", n), _opt(gs, sw + "CurrentLiquidity", n)
    bal = _opt(gs, sw + "SwapBalancer", n)
    fee = _opt(gs, sw + "FeeRate", n)
    if sqrt_p is not None and liq is not None and gs.block < 8_486_594:
        sp, lq = le(sqrt_p), le(liq)
        pool = PoolState(kind=PoolKind.CP_V3_VIRTUAL, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=lq * sp >> 64,
                         px_alpha=(lq << 64) // sp, w_quote_e18=5 * 10**17,
                         fee_rate=le(fee) if fee else fee_rate_default(gs.spec))
    else:
        pool = PoolState(kind=PoolKind.BALANCER, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao, px_alpha=alpha,
                         w_quote_e18=le(bal[:18]) if bal else 5 * 10**17,
                         fee_rate=le(fee) if fee else fee_rate_default(gs.spec))
    feb = _opt(gs, sm + "FirstEmissionBlockNumber", n)
    flow = _opt(gs, sm + "SubnetTaoFlow", n)
    owner = _opt(gs, sm + "SubnetOwner", n)
    owner_hk = _opt(gs, sm + "SubnetOwnerHotkey", n)
    tas = _opt(gs, sm + "TotalAlphaStaked", n)
    rp_hex = _opt(gs, sm + "RootProp", n)
    ema_hb = u(sm + "EMAPriceHalvingBlocks", 201_600)
    base = SubnetState(
        key=SubnetKey(NetUid(n), Block(u(sm + "NetworkRegisteredAt"))), pool=pool,
        alpha_out=AlphaRao(u(sm + "SubnetAlphaOut")), protocol_alpha=AlphaRao(u(sm + "SubnetProtocolAlpha")),
        moving_price=fx(sm + "SubnetMovingPrice", 32, signed=True), root_prop=Decimal(0),
        miner_burned=fx(sm + "MinerBurned", 32), emission_enabled=bool(u(sm + "SubnetEmissionEnabled", 1)),
        subtoken_enabled=bool(u(sm + "SubtokenEnabled", 0)), reg_allowed=bool(u(sm + "NetworkRegistrationAllowed", 1)),
        first_emission_block=Block(le(feb)) if feb else None, tempo=u(sm + "Tempo", 360),
        last_epoch_block=Block(u(sm + "LastEpochBlock")), ema_halving_blocks=ema_hb,
        tao_in_emission=Rao(u(sm + "SubnetTaoInEmission")), excess_tao=Rao(u(sm + "SubnetExcessTao")),
        alpha_out_emission=AlphaRao(u(sm + "SubnetAlphaOutEmission")),
        alpha_in_emission=AlphaRao(u(sm + "SubnetAlphaInEmission")),
        reservoir_tao=Rao(u(sw + "BalancerTaoReservoir")), reservoir_alpha=AlphaRao(u(sw + "BalancerAlphaReservoir")),
        tao_flow_cum=le(flow, signed=True) if flow else None,
        owner_coldkey=Coldkey(account(owner)) if owner else None,
        owner_hotkey=Hotkey(account(owner_hk)) if owner_hk else None,
        total_alpha_staked=AlphaRao(le(tas)) if tas else None, escrow_alpha=AlphaRao(escrow_alpha) if escrow_alpha else None,
        hotkeys=build_hotkeys(gs, n, extra=[account(owner_hk)] if owner_hk else []) if with_hotkeys else (),
    )
    rp = fixed(rp_hex, 32) if rp_hex else root_prop(glob, alpha_issuance(base))
    return replace(base, root_prop=rp)


def build_snapshot(gs: GoldenSnap, netuids: Sequence[int] | None = None, *, with_hotkeys: bool = False,
                   plan: ReadPlan = ReadPlan.FULL) -> ChainSnapshot:
    nets = list(netuids) if netuids is not None else gs.bulk_netuids()
    glob = build_globals(gs)
    subnets = tuple(build_subnet(gs, n, glob, with_hotkeys=with_hotkeys) for n in sorted(nets))
    ts = gs.get("Timestamp.Now") if gs.has("Timestamp.Now") else None
    return ChainSnapshot(block=Block(gs.block), block_hash=BlockHash(gs.raw["block_hash"]),
                         timestamp_ms=le(ts) if ts else 0, plan=plan, glob=glob, subnets=subnets)


@pytest.fixture(scope="session")
def gsnap() -> Callable[[str, int], GoldenSnap]:
    """gsnap("sn92_9240388", 0) -> GoldenSnap of that fixture's i-th snapshot."""
    cache: dict[str, dict[str, Any]] = {}

    def load(name: str, i: int = 0) -> GoldenSnap:
        if name not in cache:
            path = GOLDEN / (name if name.endswith(".json") else f"{name}.json")
            cache[name] = json.loads(path.read_text(encoding="utf-8"))
        return GoldenSnap(cache[name]["snapshots"][i])

    return load


@dataclass(frozen=True)
class Decoders:
    le: Callable[..., int]
    fixed: Callable[..., Decimal]
    safefloat: Callable[[str], Decimal]
    build_globals: Callable[..., ChainGlobals]
    build_subnet: Callable[..., SubnetState]
    build_snapshot: Callable[..., ChainSnapshot]
    build_hotkeys: Callable[..., tuple[HotkeyIdx, ...]]


@pytest.fixture(scope="session")
def dec() -> Decoders:
    return Decoders(le=le, fixed=fixed, safefloat=safefloat, build_globals=build_globals, build_subnet=build_subnet,
                    build_snapshot=build_snapshot, build_hotkeys=build_hotkeys)


@pytest.fixture(scope="session")
def sim_fields() -> Callable[[str], list[int]]:
    """SimSwapResult hex -> its u64 fields (6 at spec >= 391, 4 before)."""
    def parse(h: str) -> list[int]:
        b = bytes.fromhex(h[2:])
        return [int.from_bytes(b[i:i + 8], "little") for i in range(0, len(b), 8)]
    return parse


@pytest.fixture(scope="session")
def balancer_pool() -> Callable[..., PoolState]:
    """balancer_pool(tao_rao, alpha_rao, w_quote_e18=5e17, fee_rate=33) -> BALANCER PoolState on real reserves."""
    def build(tao: int, alpha: int, w_quote_e18: int = PERQUINTILL // 2, fee_rate: int = 33) -> PoolState:
        return PoolState(kind=PoolKind.BALANCER, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao, px_alpha=alpha,
                         w_quote_e18=w_quote_e18, fee_rate=fee_rate)
    return build


# ------------------------------------------------------------------------------------------------- archive (network)
class Archive:
    """Minimal READ-ONLY JSON-RPC client for @pytest.mark.network tests: chain_getBlockHash, state_queryStorageAt,
    state_call. Paced at <= 2.5 req/s with exponential backoff on 429 / 5xx / transient JSON-RPC errors. No signing,
    no extrinsics, no keys."""

    ALLOWED = frozenset({"chain_getBlockHash", "state_queryStorageAt", "state_call", "chain_getHeader",
                         "state_getRuntimeVersion"})
    TRANSIENT = frozenset({-32029, -32005, -32603, -32004, -32000})

    def __init__(self, url: str, rate_per_s: float = 2.5) -> None:
        import httpx

        self.url = url
        self.min_interval = 1.0 / rate_per_s
        self.client = httpx.Client(timeout=60.0, headers={"Content-Type": "application/json"})
        self.calls = 0
        self._last = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        import time

        import httpx

        if method not in self.ALLOWED:
            raise ValueError(f"{method} is not an allowed read-only method")
        delay = 1.0
        for _ in range(8):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params})
                if r.status_code != 429 and r.status_code < 500:
                    body = r.json()
                    err = body.get("error")
                    if not err:
                        return body["result"]
                    if int(err.get("code", 0)) not in self.TRANSIENT:
                        raise RuntimeError(f"{method}: {err}")
            except (httpx.TransportError, json.JSONDecodeError):
                pass
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise RuntimeError(f"{method}: gave up")

    # ---- keys
    @staticmethod
    def twox128(b: bytes) -> bytes:
        import xxhash

        return (xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little")
                + xxhash.xxh64_intdigest(b, seed=1).to_bytes(8, "little"))

    def key(self, item: str, *parts: bytes) -> str:
        pallet, name = item.split(".", 1)
        return "0x" + (self.twox128(pallet.encode()) + self.twox128(name.encode()) + b"".join(parts)).hex()

    def netuid_key(self, item: str, netuid: int) -> str:
        """SubtensorModule per-netuid maps use Identity(u16); Swap maps use Twox64Concat(u16)."""
        import xxhash

        raw = netuid.to_bytes(2, "little")
        if item.startswith("Swap."):
            return self.key(item, xxhash.xxh64_intdigest(raw, seed=0).to_bytes(8, "little") + raw)
        return self.key(item, raw)

    # ---- reads
    def block_hash(self, block: int) -> str:
        h = self.call("chain_getBlockHash", [block])
        assert isinstance(h, str) and h.startswith("0x")
        return h

    def query(self, keys: Sequence[str], at: str) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for i in range(0, len(keys), 900):
            res = self.call("state_queryStorageAt", [list(keys[i:i + 900]), at])
            for change_set in res:
                for k, v in change_set["changes"]:
                    out[k] = v
        for k in keys:
            out.setdefault(k, None)
        return out

    def runtime(self, method: str, args_hex: str, at: str) -> str:
        res = self.call("state_call", [method, args_hex, at])
        assert isinstance(res, str)
        return res

    def golden_like(self, template: GoldenSnap, block: int) -> GoldenSnap:
        """Re-read every key of `template` (its per-key storage rows and its bulk per-netuid tables, by their exact
        storage keys) at `block`, returning a GoldenSnap with the same layout - so the test decoders above apply."""
        at = self.block_hash(block)
        rv = self.call("state_getRuntimeVersion", [at])
        rows = [it for it in template.raw["storage"] if it.get("key")]
        bulk_in = template.raw.get("storage_by_netuid") or {}
        bulk_keys: dict[str, list[str]] = {}
        for item in bulk_in:
            bulk_keys[item] = [self.netuid_key(item, n) for n in range(len(bulk_in[item]["values"]))]
        vals = self.query([r["key"] for r in rows] + [k for ks in bulk_keys.values() for k in ks], at)
        raw: dict[str, Any] = {
            "block": block, "block_hash": at, "spec_version": int(rv["specVersion"]),
            "transaction_version": int(rv["transactionVersion"]),
            "storage": [{**r, "value": vals[r["key"]]} for r in rows],
            "storage_by_netuid": {item: {"hashers": bulk_in[item].get("hashers"), "values": [vals[k] for k in ks]}
                                  for item, ks in bulk_keys.items()},
            "runtime_api": [],
        }
        return GoldenSnap(raw)

    def finalized_head(self) -> int:
        head = self.call("chain_getHeader", [])
        return int(head["number"], 16)


@pytest.fixture(scope="session")
def archive() -> Archive:
    """Public archive endpoint (DESIGN section 6.1; read-only, <= 3 req/s)."""
    return Archive("https://bittensor-finney.api.onfinality.io/public")
