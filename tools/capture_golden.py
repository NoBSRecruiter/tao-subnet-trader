"""tools/capture_golden.py - one-off golden fixture capture (WP0; DESIGN.md sections 10.1 and 11).

Plain httpx JSON-RPC (deliberately NOT the WP1 reader) against a public archive node. It captures RAW chain data
pinned to block hashes into tests/fixtures/golden/:

- storage values exactly as returned by state_queryStorageAt (hex SCALE bytes; null = absent key), each entry
  labelled with its item name, key arguments and hasher layout so decoders can be tested without a node;
- runtime-API results exactly as returned by state_call (hex), with the hex arguments sent;
- the spec-475 runtime metadata (raw) plus, when scalecodec (collector extra) is installed, a storage-layout
  summary (hashers, key/value types, defaults) of the pallets the reader uses;
- manifest.json and README.md with block / hash / spec provenance and the brief's expected vectors.

Strictly read-only: chain_getBlockHash, state_getRuntimeVersion, state_queryStorageAt, state_getKeysPaged,
state_call, state_getMetadata. No signing, no extrinsics, no keys. Requests are rate-limited (default 2.5 req/s,
below the 3 req/s budget) with exponential backoff on 429 / -32029 / -32005 / -32603 / -32004 / transport errors.

Usage:  .venv/Scripts/python.exe tools/capture_golden.py [--only NAME ...] [--rate 2.5] [--endpoint URL]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import xxhash

ENDPOINT = "https://bittensor-finney.api.onfinality.io/public"
OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden"
MAX_NETUID = 144                       # SubnetLimit (128) + 16, as the reader's default range (section 6.5)
KEYS_PER_CALL = 1000
RAO = 10**9
ESCROW_COLDKEY = b"modlsubtensrbeta/esc".ljust(32, b"\x00")     # brief 3.7: "modl"+"subtensr"+"beta/esc"
TRANSIENT_CODES = {-32029, -32005, -32603, -32004, -32000}
CRLF, LF = bytes([13, 10]), bytes([10])           # manifest checksums are over LF-normalized bytes (git autocrlf)


# ------------------------------------------------------------------------------------------------- JSON-RPC
class RpcError(Exception):
    pass


class Rpc:
    """Rate-limited JSON-RPC over HTTP POST with exponential backoff. Read-only methods only."""

    ALLOWED = frozenset({"chain_getBlockHash", "state_getRuntimeVersion", "state_queryStorageAt", "state_getKeysPaged",
                         "state_call", "state_getMetadata", "chain_getHeader"})

    def __init__(self, url: str, rate_per_s: float, timeout_s: float = 60.0, max_attempts: int = 8) -> None:
        self.url = url
        self.min_interval = 1.0 / rate_per_s
        self.max_attempts = max_attempts
        self.client = httpx.Client(timeout=timeout_s, headers={"Content-Type": "application/json"})
        self.calls = 0
        self.retries = 0
        self._last = 0.0
        self._id = 0

    def _pace(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in self.ALLOWED:
            raise RpcError(f"{method} is not a read-only method allowed by this tool")
        delay = 1.0
        last_err = ""
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            self._id += 1
            self.calls += 1
            retry_after: float | None = None
            try:
                r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    ra = r.headers.get("Retry-After")
                    retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                else:
                    r.raise_for_status()
                    body = r.json()
                    if "error" in body and body["error"] is not None:
                        err = body["error"]
                        code = int(err.get("code", 0))
                        last_err = f"JSON-RPC {code}: {err.get('message')}"
                        if code not in TRANSIENT_CODES:
                            raise RpcError(f"{method}: {last_err}")
                        if code == -32004:
                            retry_after = 30.0
                    else:
                        return body["result"]
            except (httpx.TransportError, json.JSONDecodeError) as e:
                last_err = f"{type(e).__name__}: {e}"
            self.retries += 1
            sleep = retry_after if retry_after is not None else min(60.0, delay) * random.uniform(0.5, 1.5)
            print(f"  retry {attempt}/{self.max_attempts} {method} after {last_err}; sleeping {sleep:.1f}s", file=sys.stderr)
            time.sleep(sleep)
            delay *= 2
        raise RpcError(f"{method}: gave up after {self.max_attempts} attempts ({last_err})")


# ------------------------------------------------------------------------------------------------- storage keys
def twox128(b: bytes) -> bytes:
    return xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + xxhash.xxh64_intdigest(b, seed=1).to_bytes(8, "little")


def twox64_concat(b: bytes) -> bytes:
    return xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + b


def blake2_128_concat(b: bytes) -> bytes:
    return hashlib.blake2b(b, digest_size=16).digest() + b


def identity(b: bytes) -> bytes:
    return b


HASHERS: dict[str, Callable[[bytes], bytes]] = {"Identity": identity, "Twox64Concat": twox64_concat,
                                                "Blake2_128Concat": blake2_128_concat}


def prefix(pallet: str, item: str) -> bytes:
    return twox128(pallet.encode()) + twox128(item.encode())


def le16(n: int) -> bytes:
    return n.to_bytes(2, "little")


def le64(n: int) -> bytes:
    return n.to_bytes(8, "little")


def acct(hex32: str) -> bytes:
    b = bytes.fromhex(hex32[2:] if hex32.startswith("0x") else hex32)
    if len(b) != 32:
        raise ValueError("account id must be 32 bytes")
    return b


@dataclass(frozen=True)
class Req:
    item: str                        # "Pallet.Item"
    args: tuple[Any, ...]            # JSON-able key arguments (netuid ints, 0x account hex, enum labels)
    hashers: tuple[str, ...]         # one hasher per key part ("" for a plain value)
    key: bytes

    def to_json(self, value: str | None) -> dict[str, Any]:
        return {"item": self.item, "args": list(self.args), "hashers": list(self.hashers), "key": "0x" + self.key.hex(),
                "value": value}


def plain(pallet: str, item: str) -> Req:
    return Req(f"{pallet}.{item}", (), (), prefix(pallet, item))


def keyed(pallet: str, item: str, parts: Sequence[tuple[str, bytes, Any]]) -> Req:
    """parts: (hasher, encoded key part, JSON label of the part)."""
    key = prefix(pallet, item) + b"".join(HASHERS[h](enc) for h, enc, _ in parts)
    return Req(f"{pallet}.{item}", tuple(lbl for _, _, lbl in parts), tuple(h for h, _, _ in parts), key)


def sub(item: str, n: int) -> Req:                                   # SubtensorModule map keyed by Identity(u16)
    return keyed("SubtensorModule", item, [("Identity", le16(n), n)])


def swp(item: str, n: int) -> Req:                                   # Swap map keyed by Twox64Concat(u16)
    return keyed("Swap", item, [("Twox64Concat", le16(n), n)])


def hk_n(item: str, hk: str, n: int) -> Req:                         # (Blake2_128Concat(hk), Identity(u16))
    return keyed("SubtensorModule", item, [("Blake2_128Concat", acct(hk), hk), ("Identity", le16(n), n)])


# Per-subnet items (section 6.5 registry; hashers confirmed against the spec-475 metadata)
SUBNET_ITEMS = ("NetworksAdded", "NetworkRegisteredAt", "SubnetTAO", "SubnetAlphaIn", "SubnetAlphaOut",
                "SubnetProtocolAlpha", "SubnetMovingPrice", "SubnetFastMovingPrice", "RootProp", "MinerBurned",
                "SubnetEmissionEnabled", "SubtokenEnabled", "NetworkRegistrationAllowed", "FirstEmissionBlockNumber",
                "Tempo", "LastEpochBlock", "EMAPriceHalvingBlocks", "SubnetTaoInEmission", "SubnetExcessTao",
                "SubnetAlphaOutEmission", "SubnetAlphaInEmission", "SubnetTaoFlow", "SubnetVolume", "SubnetOwner",
                "SubnetOwnerHotkey", "OwnerCutEnabled", "OwnerCutAutoLockEnabled", "TotalAlphaStaked",
                "MaxAllowedValidators", "SubnetEpochConsensus", "LiquidAlphaConsensusMode", "PendingOwnerCut")
SWAP_ITEMS = ("SwapBalancer", "FeeRate", "BalancerTaoReservoir", "BalancerAlphaReservoir", "AlphaSqrtPrice",
              "CurrentLiquidity", "CurrentTick")
EMISSION_ITEMS = ("NetworksAdded", "NetworkRegisteredAt", "FirstEmissionBlockNumber", "SubtokenEnabled",
                  "NetworkRegistrationAllowed", "SubnetEmissionEnabled", "SubnetMovingPrice", "MinerBurned", "RootProp",
                  "SubnetTAO", "SubnetAlphaIn", "SubnetAlphaOut", "SubnetProtocolAlpha", "SubnetAlphaOutEmission",
                  "SubnetAlphaInEmission", "SubnetTaoInEmission", "SubnetExcessTao")
EMISSION_SWAP_ITEMS = ("SwapBalancer", "BalancerTaoReservoir", "BalancerAlphaReservoir")
LADDER_ITEMS = ("NetworksAdded", "NetworkRegisteredAt", "SubnetMovingPrice", "FirstEmissionBlockNumber")
GLOBAL_SUBTENSOR = ("SubnetMovingAlpha", "EmissionGateBar", "EmissionBarRank", "EmissionGateExponent",
                    "EmissionBarQuantile", "TaoWeight", "SubnetOwnerCut", "SubnetLimit", "NetworkImmunityPeriod",
                    "NetworkRateLimit", "NetworkLastLockCost", "NetworkMinLockCost", "NetworkLockReductionInterval",
                    "TaoInRefundDeploymentBlock", "NominatorMinRequiredStake", "TotalIssuance", "DissolveCleanupQueue",
                    "ShortsEnabled", "NetworkLastRegistered", "BlockEmission")


def subnet_reqs(n: int, items: Sequence[str] = SUBNET_ITEMS, swap_items: Sequence[str] = SWAP_ITEMS) -> list[Req]:
    return [sub(i, n) for i in items] + [swp(i, n) for i in swap_items]


def global_reqs() -> list[Req]:
    out = [plain("SubtensorModule", i) for i in GLOBAL_SUBTENSOR]
    out += [sub("SubnetTAO", 0),
            keyed("SubtensorModule", "LastRateLimitedBlock", [("Identity", b"\x02", "NetworkLastRegistered")]),
            plain("Balances", "TotalIssuance"), plain("SafeMode", "EnteredUntil"), plain("Timestamp", "Now"),
            plain("System", "Number")]
    return out


def all_netuid_reqs(items: Sequence[str], swap_items: Sequence[str] = (), max_netuid: int = MAX_NETUID) -> list[Req]:
    return [r for n in range(max_netuid + 1) for r in subnet_reqs(n, items, swap_items)]


# ------------------------------------------------------------------------------------------------- runtime API
@dataclass(frozen=True)
class RtReq:
    method: str
    args: dict[str, Any]
    args_hex: str


def rt(method: str, args: dict[str, Any] | None = None, enc: bytes = b"") -> RtReq:
    return RtReq(method, args or {}, "0x" + enc.hex())


def rt_price(n: int) -> RtReq:
    return rt("SwapRuntimeApi_current_alpha_price", {"netuid": n}, le16(n))


def rt_buy(n: int, tao_rao: int) -> RtReq:
    return rt("SwapRuntimeApi_sim_swap_tao_for_alpha", {"netuid": n, "tao_rao": tao_rao}, le16(n) + le64(tao_rao))


def rt_sell(n: int, alpha_rao: int) -> RtReq:
    return rt("SwapRuntimeApi_sim_swap_alpha_for_tao", {"netuid": n, "alpha_rao": alpha_rao}, le16(n) + le64(alpha_rao))


RT_PRICE_ALL = rt("SwapRuntimeApi_current_alpha_price_all")
RT_PRUNE = rt("SubnetInfoRuntimeApi_get_subnet_to_prune")
RT_BLOCK_EMISSION = rt("SubnetInfoRuntimeApi_get_block_emission")
RT_REG_COST = rt("SubnetRegistrationRuntimeApi_get_network_registration_cost")
RT_BASKETS = rt("BetaBasketRuntimeApi_get_all_validator_baskets")
RT_ESCROW_STAKE = rt("StakeInfoRuntimeApi_get_stake_info_for_coldkey", {"coldkey": "0x" + ESCROW_COLDKEY.hex()},
                     ESCROW_COLDKEY)


def rt_next_epoch(n: int) -> RtReq:
    return rt("SubnetInfoRuntimeApi_get_next_epoch_start_block", {"netuid": n}, le16(n))


# ------------------------------------------------------------------------------------------------- decoding (checks only)
def u_le(hexval: str | None, width: int | None = None, offset: int = 0) -> int | None:
    if hexval is None:
        return None
    b = bytes.fromhex(hexval[2:])
    b = b[offset:offset + width] if width is not None else b[offset:]
    return int.from_bytes(b, "little")


def sim_fields(hexval: str | None) -> dict[str, int] | None:
    """SimSwapResult: 6 x u64 (48 bytes, spec >= 391) or 4 x u64 (32 bytes, specs 302-377)."""
    if hexval is None:
        return None
    b = bytes.fromhex(hexval[2:])
    names = ["tao_amount", "alpha_amount", "tao_fee", "alpha_fee", "tao_slippage", "alpha_slippage"]
    n = len(b) // 8
    return {names[i]: int.from_bytes(b[8 * i:8 * i + 8], "little") for i in range(min(n, 6))}


# ------------------------------------------------------------------------------------------------- snapshots
@dataclass
class Snap:
    rpc: Rpc
    block: int
    block_hash: str = ""
    spec_version: int = 0
    transaction_version: int = 0
    storage: list[dict[str, Any]] = field(default_factory=list)
    runtime_api: list[dict[str, Any]] = field(default_factory=list)
    keys_paged: list[dict[str, Any]] = field(default_factory=list)
    by_netuid: dict[str, dict[str, Any]] = field(default_factory=dict)
    _values: dict[str, str | None] = field(default_factory=dict)

    def open(self) -> Snap:
        self.block_hash = str(self.rpc.call("chain_getBlockHash", [self.block]))
        rv = self.rpc.call("state_getRuntimeVersion", [self.block_hash])
        self.spec_version, self.transaction_version = int(rv["specVersion"]), int(rv["transactionVersion"])
        return self

    def read(self, reqs: Sequence[Req]) -> dict[str, str | None]:
        """Query every not-yet-read key at this block hash; returns key hex -> value for all reqs."""
        todo: list[Req] = []
        seen: set[str] = set()
        for r in reqs:
            k = "0x" + r.key.hex()
            if k not in self._values and k not in seen:
                todo.append(r)
                seen.add(k)
        for i in range(0, len(todo), KEYS_PER_CALL):
            chunk = todo[i:i + KEYS_PER_CALL]
            res = self.rpc.call("state_queryStorageAt", [["0x" + r.key.hex() for r in chunk], self.block_hash])
            got: dict[str, str | None] = {}
            for change_set in res:
                for k, v in change_set["changes"]:
                    got[k.lower()] = v
            for r in chunk:
                k = "0x" + r.key.hex()
                v = got.get(k)
                self._values[k] = v
                self.storage.append(r.to_json(v))
        return {"0x" + r.key.hex(): self._values["0x" + r.key.hex()] for r in reqs}

    def read_netuids(self, items: Sequence[str], swap_items: Sequence[str] = (), max_netuid: int = MAX_NETUID) -> None:
        """Bulk per-netuid read for netuids 0..max_netuid, stored densely (index = netuid) without per-key rows.
        Keys follow the documented layout: prefix ++ Identity(u16) for SubtensorModule, ++ Twox64Concat(u16) for Swap."""
        reqs = all_netuid_reqs(items, swap_items, max_netuid)
        todo = [r for r in reqs if "0x" + r.key.hex() not in self._values]
        for i in range(0, len(todo), KEYS_PER_CALL):
            chunk = todo[i:i + KEYS_PER_CALL]
            res = self.rpc.call("state_queryStorageAt", [["0x" + r.key.hex() for r in chunk], self.block_hash])
            got = {k.lower(): v for change_set in res for k, v in change_set["changes"]}
            for r in chunk:
                self._values["0x" + r.key.hex()] = got.get("0x" + r.key.hex())
        for r in reqs:
            slot = self.by_netuid.setdefault(r.item, {"hashers": list(r.hashers), "values": [None] * (max_netuid + 1)})
            slot["values"][int(r.args[0])] = self._values["0x" + r.key.hex()]

    def value(self, r: Req) -> str | None:
        return self.read([r])["0x" + r.key.hex()]

    def get_keys(self, pfx: bytes, label: str) -> list[str]:
        keys: list[str] = []
        start: str | None = None
        while True:
            page = self.rpc.call("state_getKeysPaged", ["0x" + pfx.hex(), 1000, start, self.block_hash])
            keys.extend(page)
            if len(page) < 1000:
                break
            start = page[-1]
        self.keys_paged.append({"label": label, "prefix": "0x" + pfx.hex(), "keys": keys})
        return keys

    def call(self, q: RtReq) -> str | None:
        try:
            res: str | None = self.rpc.call("state_call", [q.method, q.args_hex, self.block_hash])
            err = None
        except RpcError as e:
            res, err = None, str(e)
        entry: dict[str, Any] = {"method": q.method, "args": q.args, "args_hex": q.args_hex, "result": res}
        if err:
            entry["error"] = err
        self.runtime_api.append(entry)
        return res

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"block": self.block, "block_hash": self.block_hash, "spec_version": self.spec_version,
                               "transaction_version": self.transaction_version, "storage": self.storage,
                               "runtime_api": self.runtime_api}
        if self.by_netuid:
            out["storage_by_netuid"] = self.by_netuid
        if self.keys_paged:
            out["keys_paged"] = self.keys_paged
        return out


def dividend_state(s: Snap, n: int) -> list[str]:
    """AlphaDividendsPerSubnet(n, .) membership (point-in-time) and the hotkey panel of every recipient."""
    pfx = prefix("SubtensorModule", "AlphaDividendsPerSubnet") + le16(n)
    keys = s.get_keys(pfx, f"AlphaDividendsPerSubnet({n}, .)")
    hks = sorted("0x" + k[-64:] for k in keys)
    reqs: list[Req] = []
    for hk in hks:
        reqs += [keyed("SubtensorModule", "AlphaDividendsPerSubnet", [("Identity", le16(n), n), ("Blake2_128Concat", acct(hk), hk)]),
                 hk_n("TotalHotkeyAlpha", hk, n), hk_n("TotalHotkeyShares", hk, n), hk_n("TotalHotkeySharesV2", hk, n),
                 hk_n("ChildkeyTake", hk, n),
                 keyed("SubtensorModule", "Delegates", [("Blake2_128Concat", acct(hk), hk)])]
    s.read(reqs)
    return hks


def owner_state(s: Snap, n: int) -> None:
    ck, hk = s.value(sub("SubnetOwner", n)), s.value(sub("SubnetOwnerHotkey", n))
    if ck is None or hk is None:
        return
    parts = [("Blake2_128Concat", acct(hk), hk), ("Blake2_128Concat", acct(ck), ck), ("Identity", le16(n), n)]
    s.read([keyed("SubtensorModule", "AlphaV2", parts), keyed("SubtensorModule", "Alpha", parts),
            hk_n("TotalHotkeyAlpha", hk, n), hk_n("TotalHotkeyShares", hk, n), hk_n("TotalHotkeySharesV2", hk, n),
            keyed("SubtensorModule", "Delegates", [("Blake2_128Concat", acct(hk), hk)])])


# ------------------------------------------------------------------------------------------------- fixtures
@dataclass
class Fixture:
    name: str
    purpose: str
    snapshots: list[Snap]
    expected: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def cap_sn92(rpc: Rpc) -> Fixture:
    s = Snap(rpc, 9_240_388).open()
    s.read(subnet_reqs(92) + global_reqs())
    s.read_netuids(LADDER_ITEMS)
    hks = dividend_state(s, 92)
    owner_state(s, 92)
    buy10 = s.call(rt_buy(92, 10 * RAO))
    buy100 = s.call(rt_buy(92, 100 * RAO))
    s.call(rt_sell(92, 1_000 * RAO))
    price = s.call(rt_price(92))
    prune = s.call(RT_PRUNE)
    for q in (RT_PRICE_ALL, RT_BLOCK_EMISSION, RT_REG_COST, rt_next_epoch(92)):
        s.call(q)
    return Fixture(
        "sn92_9240388",
        "SN92 pool, dividend panel, owner position and prune ladder at 9,240,388 (AMM 10/100-TAO buy vectors, live "
        "prune target, recovery-ratio inputs, closed-form yield inputs)",
        [s],
        expected={"sim_swap_buy_10tao": {"tao_amount": 9_994_964_523, "alpha_amount_approx": 7_289_425_629_000,
                                         "tao_fee": 5_035_477},
                  "sim_swap_buy_100tao": {"alpha_amount_approx": 63_351_670_000_000,
                                          "alpha_slippage_approx": 10_820_700_000_000},
                  "prune_target_netuid": 92, "recovery_ratio_approx": 0.368,
                  "closed_form_yield_gross_pct_day": 0.448, "root_prop_approx": 0.479, "a_earn_alpha_approx": 343_198},
        checks={"sim_buy_10tao": sim_fields(buy10), "sim_buy_100tao": sim_fields(buy100),
                "current_alpha_price_rao": u_le(price), "subnet_to_prune_raw": prune, "n_dividend_recipients": len(hks)},
    )


def cap_escrow(rpc: Rpc) -> Fixture:
    s = Snap(rpc, 9_240_388).open()
    baskets, stake = s.call(RT_BASKETS), s.call(RT_ESCROW_STAKE)
    return Fixture("escrow_9240388",
                   "Raw validator-basket and escrow-coldkey stake results at 9,240,388 (SCALE structs decoded with "
                   "scalecodec + runtime-API metadata; DESIGN.md section 13 Q7)", [s],
                   expected={"SN92_escrow_alpha_approx": 49_063, "network_funds": 189},
                   checks={"baskets_bytes": None if baskets is None else (len(baskets) - 2) // 2,
                           "escrow_stake_info_bytes": None if stake is None else (len(stake) - 2) // 2})


SN1_BLOCK = 9_240_388


def cap_sn1(rpc: Rpc) -> Fixture:
    """SN1 1-TAO quote. The brief's spot (6,562,800 rao/alpha) holds exactly at 9,240,307-9,240,410 and
    9,240,660-9,240,700 (per-block scan of [9,238,000, 9,242,700] on 2026-10-09); 9,240,388 is also the SN92
    vector block. DESIGN.md 10.1's alpha_out of 152,285,961,807 rao is reproduced at no block in that range; the
    chain gives about 152,290,64x,xxx rao there (the brief's "152.29 alpha")."""
    s = Snap(rpc, SN1_BLOCK).open()
    s.read(subnet_reqs(1) + global_reqs())
    buy = s.call(rt_buy(1, RAO))
    price = s.call(rt_price(1))
    notes = [f"spot == 6,562,800 at this block: {u_le(price) == 6_562_800}",
             "DESIGN.md 10.1 lists alpha_out 152,285,961,807 rao; no block in [9,238,000, 9,242,700] reproduces it. "
             "At the brief's spot blocks sim_swap returns about 152,290,64x,xxx rao (152.29 alpha, as the brief says); "
             "the checks below hold the value at this block. ADR requested to correct the design figure."]
    return Fixture(f"sn1_quote_{SN1_BLOCK}", "SN1 pool and the 1-TAO sim_swap quote vector (brief 2.8)", [s],
                   expected={"sim_swap_buy_1tao": {"tao_amount": 999_496_453, "tao_fee": 503_547,
                                                   "alpha_amount_approx": 152_290_000_000,
                                                   "alpha_amount_design_unreproduced": 152_285_961_807},
                             "spot_rao": 6_562_800},
                   checks={"sim_buy_1tao": sim_fields(buy), "current_alpha_price_rao": u_le(price)}, notes=notes)


def cap_yield(rpc: Rpc) -> Fixture:
    s = Snap(rpc, 9_240_388).open()
    s.read(global_reqs())
    counts = {}
    for n in (70, 92, 64):
        s.read(subnet_reqs(n))
        counts[n] = len(dividend_state(s, n))
        owner_state(s, n)
    return Fixture("yield_inputs_9240388",
                   "Closed-form yield inputs for SN70 / SN92 / SN64 (RootProp, alpha_out emission, owner cut, A_earn = "
                   "sum of TotalHotkeyAlpha over AlphaDividendsPerSubnet recipients, takes)", [s],
                   expected={"SN70": {"rp": 0.655, "a_earn": 182_839, "gross_pct_day": 0.557},
                             "SN92": {"rp": 0.479, "a_earn": 343_198, "gross_pct_day": 0.448},
                             "SN64": {"rp": 0.133, "a_earn": 2_789_195, "gross_pct_day": 0.0918}},
                   checks={"n_dividend_recipients": counts},
                   notes=["The brief's yield table was measured around blocks 9,240,3xx; values here are at 9,240,388 "
                          "and should agree to the brief's rounding."])


def cap_sn70_index(rpc: Rpc) -> Fixture:
    snaps = []
    hk56: str | None = None
    for b in (9_240_222, 9_240_581, 9_240_582):
        s = Snap(rpc, b).open()
        s.read(subnet_reqs(70, ("NetworkRegisteredAt", "LastEpochBlock", "Tempo", "SubnetAlphaOutEmission", "RootProp"), ()))
        hks = dividend_state(s, 70)
        hk56 = hk56 or next((h for h in hks if h[2:6] == "56a9"), None)
        snaps.append(s)
    notes = []
    if hk56 is not None:
        s = Snap(rpc, 9_240_582 - 216_000).open()
        s.read([hk_n("TotalHotkeyAlpha", hk56, 70), hk_n("TotalHotkeyShares", hk56, 70),
                hk_n("TotalHotkeySharesV2", hk56, 70), sub("NetworkRegisteredAt", 70)])
        snaps.append(s)
        notes.append(f"hotkey 56a9 = {hk56}; the 4th snapshot (30 d = 216,000 blocks earlier) holds only its index inputs")
    else:
        notes.append("no SN70 dividend recipient starting with 0x56a9 was found")
    return Fixture("sn70_index_9240222_9240582",
                   "SN70 share-price index: flat 9,240,222 -> 9,240,581, then +0.0279% at 9,240,582 (= LastEpochBlock); "
                   "index I = TotalHotkeyAlpha / shares (V2 SafeFloat when V1 is absent)", snaps,
                   expected={"hotkey_prefix": "0x56a9", "jump_block": 9_240_582, "jump_pct": 0.0279,
                             "index_30d": {"from": 1.329667, "to": 1.631360}},
                   notes=notes)


def emission_snapshot(rpc: Rpc, block: int) -> Snap:
    s = Snap(rpc, block).open()
    s.read(global_reqs())
    s.read_netuids(EMISSION_ITEMS, EMISSION_SWAP_ITEMS)
    s.call(RT_BLOCK_EMISSION)
    return s


def cap_sn51(rpc: Rpc) -> Fixture:
    pre, at = emission_snapshot(rpc, 9_240_381), emission_snapshot(rpc, 9_240_382)
    return Fixture("sn51_emission_9240382",
                   "Full emission state at 9,240,381 (inputs: EMA_{n-1}) and 9,240,382 (observed per-block emission) "
                   "for the SN51 injection-split vector", [pre, at],
                   expected={"SN51": {"rp": 0.139, "alpha_in_per_block": 0.139, "tao_in_per_block": 0.01397,
                                      "chain_buy_tao_per_block": 0.0515, "tao_per_day_approx": 472},
                             "network_sum_tao_in_plus_excess_per_block": 0.5})


def cap_erab(rpc: Rpc) -> Fixture:
    s = Snap(rpc, 7_000_020).open()
    s.read(global_reqs())
    checks: dict[str, Any] = {}
    for n in (1, 19, 64):
        s.read(subnet_reqs(n))
        sqrt_p = u_le(s.value(swp("AlphaSqrtPrice", n)))
        price = s.call(rt_price(n))
        for tao in (1, 10, 100):
            s.call(rt_buy(n, tao * RAO))
        spot = (sqrt_p * sqrt_p / 2**128) if sqrt_p else None
        sells = {}
        for tao in (1, 10, 100):
            alpha = int(tao * RAO / spot) if spot else tao * RAO
            sells[tao] = sim_fields(s.call(rt_sell(n, alpha)))
        checks[f"SN{n}"] = {"current_alpha_price_rao": u_le(price), "alpha_sqrt_price_raw": sqrt_p, "sells": sells}
    return Fixture("erab_7000020",
                   "Era-B (swap v3) pools of SN1/SN19/SN64 at 7,000,020: SubnetTAO/AlphaIn plus Swap.AlphaSqrtPrice "
                   "and Swap.CurrentLiquidity (virtual reserves L*sqrtP, L/sqrtP) with sim_swap buys of 1/10/100 TAO "
                   "and sells of about 1/10/100 TAO of alpha (32-byte SimSwapResult at this spec)", [s],
                   expected={"virtual_reserve_parity_rel": 3e-7}, checks=checks)


def cap_globals(rpc: Rpc) -> Fixture:
    s = Snap(rpc, 9_240_878).open()
    s.read(global_reqs())
    s.read_netuids(LADDER_ITEMS)
    cost = s.call(RT_REG_COST)
    s.call(RT_PRUNE)
    s.call(RT_BLOCK_EMISSION)
    return Fixture("globals_9240878",
                   "Globals and prune-ladder inputs at 9,240,878 with the runtime registration cost", [s],
                   expected={"registration_cost_tao": 962.89, "network_last_lock_cost_tao": 653.02,
                             "last_registration_block": 9_210_610, "i_eff_blocks": 57_600},
                   checks={"registration_cost_rao": u_le(cost)})


PARITY_FIRST = 8_765_720
PARITY_STEP = 24_000


def parity_blocks() -> list[int]:
    """20 evaluation blocks >= 8,765,684 spread over the rank-32 gate era; even ones on theta-refresh blocks
    (block % 360 == 0), odd ones off them."""
    out = []
    for k in range(20):
        b = PARITY_FIRST + k * PARITY_STEP
        if k % 2 == 0:
            b += (-b) % 360
        elif b % 360 == 0:
            b += 7
        out.append(b)
    return out


def cap_parity(rpc: Rpc, block: int) -> Fixture:
    b = block
    for _ in range(5):
        pre, at = emission_snapshot(rpc, b - 1), emission_snapshot(rpc, b)
        if pre.spec_version == at.spec_version:
            break
        b += 10                                         # do not straddle a setCode block
    else:
        raise RpcError(f"could not find a same-spec pair near {block}")
    return Fixture(f"emission_parity/b{b}",
                   f"Emission parity pair: state at {b - 1} (inputs, EMA_(n-1)) and {b} (observed SubnetTaoInEmission"
                   f" + SubnetExcessTao, reservoirs) for every netuid 0..{MAX_NETUID}", [pre, at],
                   expected={"per_subnet_abs_err_tao_per_block_max": 1e-6})


# ------------------------------------------------------------------------------------------------- metadata
SUMMARY_PALLETS = ("System", "Timestamp", "Balances", "SubtensorModule", "Swap", "SafeMode", "Proxy", "MevShield",
                   "AlphaAssets", "LimitOrders")
SUMMARY_ENUMS = ("RateLimitKey", "EpochConsensus", "ConsensusMode", "Balancer", "SafeFloat")


def metadata_summary(raw_hex: str) -> dict[str, Any] | None:
    """Storage layout of the reader's pallets from V14 metadata (needs scalecodec, the collector extra)."""
    try:
        from scalecodec.base import RuntimeConfiguration, ScaleBytes
        from scalecodec.type_registry import load_type_registry_preset
    except ImportError:
        return None
    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    rc.update_type_registry(load_type_registry_preset("legacy"))
    md = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(raw_hex))
    md.decode()
    versioned = md.value[1]
    version = next(iter(versioned))
    m = versioned[version]
    types = {t["id"]: t["type"] for t in m["types"]["types"]}

    def tname(i: int) -> str:
        t = types[i]
        d = t["def"]
        if "primitive" in d:
            return str(d["primitive"])
        if "compact" in d:
            return f"Compact<{tname(d['compact']['type'])}>"
        if "sequence" in d:
            return f"Vec<{tname(d['sequence']['type'])}>"
        if "array" in d:
            return f"[{tname(d['array']['type'])}; {d['array']['len']}]"
        if "tuple" in d:
            return "(" + ", ".join(tname(x) for x in d["tuple"]) + ")"
        path = t.get("path") or ["?"]
        params = [tname(p["type"]) for p in t.get("params", []) if p.get("type") is not None]
        name = str(path[-1])
        if name in ("FixedU128", "FixedI128", "FixedU64", "FixedI64") and params:
            return f"{name}<frac_bits={_typenum(params[0])}>"
        return name + (f"<{', '.join(params)}>" if params else "")

    pallets: dict[str, Any] = {}
    for p in m["pallets"]:
        if p["name"] not in SUMMARY_PALLETS or not p.get("storage"):
            continue
        entries = {}
        for e in p["storage"]["entries"]:
            ty = e["type"]
            if "Plain" in ty:
                entries[e["name"]] = {"modifier": e["modifier"], "kind": "Plain", "value": tname(ty["Plain"]),
                                      "default": _hex_default(e["default"])}
            else:
                mp = ty["Map"]
                entries[e["name"]] = {"modifier": e["modifier"], "kind": "Map", "hashers": mp["hashers"],
                                      "key": tname(mp["key"]), "value": tname(mp["value"]),
                                      "default": _hex_default(e["default"])}
        pallets[p["name"]] = entries
    enums: dict[str, Any] = {}
    for t in m["types"]["types"]:
        path = t["type"].get("path") or []
        if path and path[-1] in SUMMARY_ENUMS:
            d = t["type"]["def"]
            if "variant" in d:
                enums["::".join(path)] = [{"index": v["index"], "name": v["name"],
                                           "fields": [f.get("typeName") for f in v["fields"]]} for v in d["variant"]["variants"]]
            elif "composite" in d:
                enums["::".join(path)] = [{"name": f.get("name"), "type": tname(f["type"])} for f in d["composite"]["fields"]]
    return {"metadata_version": version, "pallets": pallets, "types": enums}


def _hex_default(v: Any) -> Any:
    """scalecodec returns a storage default as text when its bytes happen to be valid UTF-8; always store 0x-hex."""
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, str):
        body = v[2:]
        if v.startswith("0x") and len(body) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in body):
            return v.lower()
        return "0x" + v.encode("utf-8").hex()
    return v


def _typenum(s: str) -> int:
    """Value of a typenum UInt<UInt<...<UTerm, B1>, B0>...> string (fixed-point fractional bits)."""
    bits = s.replace("UTerm", "").split("B")[1:]
    val = 0
    for b in bits:
        if b and b[0] in "01":
            val = val * 2 + int(b[0])
    return val


def cap_metadata(rpc: Rpc, out: Path) -> list[dict[str, Any]]:
    entries = []
    for block, raw_too in ((9_240_388, True), (8_765_720, False), (7_000_020, False)):
        h = str(rpc.call("chain_getBlockHash", [block]))
        rv = rpc.call("state_getRuntimeVersion", [h])
        spec = int(rv["specVersion"])
        raw = str(rpc.call("state_getMetadata", [h]))
        base = {"block": block, "block_hash": h, "spec_version": spec, "transaction_version": int(rv["transactionVersion"])}
        if raw_too:
            name = f"metadata_spec{spec}_{block}"
            write_json(out / f"{name}.json", {**base, "fixture": name, "purpose": "raw runtime metadata (state_getMetadata)",
                                              "metadata": raw})
            entries.append({"file": f"{name}.json", "purpose": "raw runtime metadata (state_getMetadata)",
                            "snapshots": [base]})
        summary = metadata_summary(raw)
        if summary is not None:
            name = f"metadata_storage_spec{spec}"
            purpose = "storage layout (hashers, key/value types, defaults) decoded from the metadata with scalecodec"
            write_json(out / f"{name}.json", {**base, "fixture": name, "purpose": purpose, **summary})
            entries.append({"file": f"{name}.json", "purpose": purpose, "snapshots": [base]})
    return entries


# ------------------------------------------------------------------------------------------------- output
def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8", newline="\n")


def write_fixture(out: Path, fx: Fixture, endpoint: str, captured: str) -> dict[str, Any]:
    obj = {"fixture": fx.name, "purpose": fx.purpose, "endpoint": endpoint, "captured_utc": captured,
           "snapshots": [s.to_json() for s in fx.snapshots], "expected": fx.expected, "checks": fx.checks,
           "notes": fx.notes}
    write_json(out / f"{fx.name}.json", obj)
    return {"file": f"{fx.name}.json", "purpose": fx.purpose, "notes": fx.notes,
            "snapshots": [{"block": s.block, "block_hash": s.block_hash, "spec_version": s.spec_version,
                           "transaction_version": s.transaction_version} for s in fx.snapshots]}


def write_readme(out: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# Golden fixtures (WP0)",
        "",
        "Raw chain data captured by `tools/capture_golden.py` (plain httpx JSON-RPC, read-only) for the verified",
        "vectors of DESIGN.md section 10.1. Every value is pinned to a block hash. Storage values are the exact hex",
        "SCALE bytes returned by `state_queryStorageAt` (`null` = absent key; absent ValueQuery keys take the",
        "runtime default). Runtime-API results are the exact hex returned by `state_call`. `expected` holds the",
        "brief's reference numbers; `checks` holds convenience decodes made by the capture tool (not authoritative).",
        "",
        f"- Endpoint: `{manifest['endpoint']}`",
        f"- Captured (UTC): {manifest['captured_utc']}",
        f"- JSON-RPC calls: {manifest['rpc_calls']} (retries: {manifest['rpc_retries']}), rate <= {manifest['rate_per_s']} req/s",
        "- Regenerate: `.venv/Scripts/python.exe tools/capture_golden.py` (or `--only NAME`; `--readme-only` rewrites",
        "  this file offline). manifest.json holds each file's sha256 over LF-normalized bytes.",
        "",
        "## Provenance",
        "",
        "| File | Block | Block hash | Spec | Tx | Purpose |",
        "|---|---|---|---|---|---|",
    ]
    for e in manifest["fixtures"]:
        for i, s in enumerate(e["snapshots"]):
            lines.append(f"| {e['file'] if i == 0 else ''} | {s['block']:,} | `{s['block_hash']}` | {s['spec_version']} "
                         f"| {s['transaction_version']} | {e['purpose'] if i == 0 else ''} |")
    notes = [(e["file"], n) for e in manifest["fixtures"] for n in e.get("notes", [])]
    if notes:
        lines += ["", "## Notes", ""] + [f"- `{f}`: {n}" for f, n in notes]
    checks = []
    for e in manifest["fixtures"]:
        path = out / e["file"]
        if path.exists() and not e["file"].startswith("metadata"):
            c = json.loads(path.read_text(encoding="utf-8")).get("checks")
            if c:
                text = json.dumps(c, separators=(",", ":"))
                checks.append(f"- `{e['file']}`: `{text[:400]}{'...' if len(text) > 400 else ''}`")
    if checks:
        lines += ["", "## Capture-time checks (decoded by the tool; compare with each file's `expected`)", "", *checks]
    lines += ["", "## Layout", "",
              "Each fixture: `{fixture, purpose, endpoint, captured_utc, snapshots: [{block, block_hash, spec_version,",
              "transaction_version, storage: [{item, args, hashers, key, value}], storage_by_netuid?: {item: {hashers,",
              "values[netuid]}}, runtime_api: [{method, args, args_hex, result, error?}], keys_paged?: [{label, prefix,",
              "keys}]}], expected, checks, notes}`. `hashers` lists one hasher per key part (empty for plain values);",
              "`args` are the decoded key parts (netuids, 0x account ids, enum labels). `storage_by_netuid` holds bulk",
              "per-netuid reads for netuids 0..144 densely (index = netuid); their keys are prefix ++ Identity(u16) for",
              "SubtensorModule items and prefix ++ Twox64Concat(u16) for Swap items. `metadata_storage_spec*.json` are",
              "storage layouts decoded from runtime metadata with scalecodec (hashers, key/value types, defaults).", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def readme_only(out: Path) -> None:
    write_readme(out, json.loads((out / "manifest.json").read_text(encoding="utf-8")))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--rate", type=float, default=2.5, help="requests per second (<= 3)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="subset of: sn92 sn1 yield sn70 sn51 erab globals escrow parity metadata")
    ap.add_argument("--readme-only", action="store_true", help="rewrite README.md from manifest.json (no network)")
    args = ap.parse_args(argv)
    if args.readme_only:
        readme_only(args.out)
        return 0
    if args.rate > 3.0:
        ap.error("--rate must be <= 3 req/s (public endpoint budget)")
    rpc = Rpc(args.endpoint, args.rate)
    captured = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"fixtures": []}
    entries: dict[str, dict[str, Any]] = {e["file"]: e for e in old.get("fixtures", []) if (out / e["file"]).exists()}
    jobs: dict[str, Callable[[], list[Fixture]]] = {
        "sn92": lambda: [cap_sn92(rpc)], "sn1": lambda: [cap_sn1(rpc)], "yield": lambda: [cap_yield(rpc)],
        "sn70": lambda: [cap_sn70_index(rpc)], "sn51": lambda: [cap_sn51(rpc)], "erab": lambda: [cap_erab(rpc)],
        "globals": lambda: [cap_globals(rpc)], "escrow": lambda: [cap_escrow(rpc)],
        "parity": lambda: [cap_parity(rpc, b) for b in parity_blocks()],
    }
    selected = args.only or [*jobs, "metadata"]
    for name in selected:
        t0 = time.monotonic()
        if name == "metadata":
            for e in cap_metadata(rpc, out):
                entries[e["file"]] = e
        elif name in jobs:
            for fx in jobs[name]():
                e = write_fixture(out, fx, args.endpoint, captured)
                entries[e["file"]] = e
        else:
            ap.error(f"unknown capture {name!r}")
        print(f"{name}: done in {time.monotonic() - t0:.1f}s ({rpc.calls} calls so far)")
    manifest = {"endpoint": args.endpoint, "captured_utc": old.get("captured_utc", captured) if args.only else captured,
                "rate_per_s": args.rate, "rpc_calls": rpc.calls + (int(old.get("rpc_calls", 0)) if args.only else 0),
                "rpc_retries": rpc.retries + (int(old.get("rpc_retries", 0)) if args.only else 0),
                "fixtures": [entries[k] for k in sorted(entries)]}
    for e in manifest["fixtures"]:
        p = out / e["file"]
        if p.exists():
            e["sha256"] = hashlib.sha256(p.read_bytes().replace(CRLF, LF)).hexdigest()   # LF-normalized
    write_json(manifest_path, manifest)
    write_readme(out, manifest)
    print(f"wrote {len(manifest['fixtures'])} fixtures to {out} ({rpc.calls} calls, {rpc.retries} retries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
