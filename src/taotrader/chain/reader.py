"""taotrader/chain/reader.py - ChainReader over JSON-RPC (DESIGN.md sections 6.6-6.8). Windows-native, no bittensor.

Snapshot assembly is all-or-nothing and pinned to one block hash:

1. (block, hash) from the caller; spec/tx version from `state_getRuntimeVersion` (cached by monotone bracketing:
   spec versions never decrease, so two equal known points pin every block between them).
2. Keys for the plan from the registry (chain/items.py), filtered by that spec's layout (chain/metadata.py).
3. `state_queryStorageAt` in chunks of <= keys_per_call, concurrently up to max_concurrency, under the pool's
   per-endpoint token bucket. Every asked key must come back (null = absent); a short answer fails the snapshot.
4. Decode every key; absent keys take that spec's runtime default (Quality.DEFAULT_FILLED when the layout is not an
   exact validated one, or when no layout is known at all).
5. HEAD plans carry FULL-only fields from `prev` (Quality.CARRIED); a generation not in `prev` is read in full.
6. Era-correct PoolState: >= 8,486,594 BALANCER(SubnetTAO, SubnetAlphaIn, quote); era B with AlphaSqrtPrice set
   CP_V3_VIRTUAL(px_tao = L*sqrtP, px_alpha = L/sqrtP); otherwise CP_REAL (TA_PRICE after 6,205,195).
7. SEED_FALLBACK within 60 blocks of 8,486,594 if quote == 0.5 exactly and the price jumped > 1% vs prev.
8. Validate: `subnets` is exactly the non-root netuids with NetworksAdded true; reserves > 0; Balancer quote in
   [0.01, 0.99]; one generation per netuid; System.Number == block. Any violation -> SnapshotDecodeError (a DecodeError
   carrying the raw key/value hex so the collector can keep it in its fetch ledger).
9. digest = blake2b-128(canonical_bytes(snapshot with digest "")).

The reader stores RAW chain values only (no rp before 7,135,420, no emission, no fallbacks such as
AlphaOut - ProtocolAlpha). The documented unit conversions are NominatorMinRequiredStake factor -> rao and the owner
position value (shares x owner-hotkey index, section 6.5). Legacy rows (LastMechansimStepBlock, NetworkLastRegistered)
stand in for their primary item only in runtimes where the primary does not exist.

`pull()` prefetches the prev-independent first read of the next blocks concurrently (bounded by max_concurrency and
the endpoint bucket), then finishes each snapshot in block order; this is what makes per-block historical pulls reach
~1.5 snapshots/s on a 3 req/s public endpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final

from taotrader.core.codec import digest as codec_digest
from taotrader.core.errors import DecodeError
from taotrader.core.events import ModelDriftObserved
from taotrader.core.fixed import DEC
from taotrader.core.protocols import SwapSim
from taotrader.core.state import (
    ChainGlobals,
    ChainSnapshot,
    HotkeyIdx,
    MetagraphLite,
    PoolKind,
    PoolState,
    Quality,
    ReadPlan,
    SubnetState,
)
from taotrader.core.units import (
    HALF_E18,
    MIN_STAKE_RAO,
    PERQUINTILL,
    PPM,
    AlphaRao,
    Block,
    BlockHash,
    Coldkey,
    Hotkey,
    NetUid,
    Rao,
    SubnetKey,
)
from taotrader.protocol.regimes import fee_rate_default

from . import items as it
from . import runtime_api as rt
from .hashing import from_hex, prefix, split_key, to_hex
from .hashing import identity as h_identity
from .hashing import le16 as h_le16
from .items import Plan, Query, Row
from .metadata import SpecLayout, SpecLayouts
from .rpc import Role, RpcError, RpcFatal, RpcPool
from .scale import d_account

ZERO_ACCOUNT: Final[str] = "0x" + "00" * 32
ESCROW_FIRST_BLOCK: Final[int] = 8_765_684            # baskets / escrow exist from v441
MAX_UIDS: Final[int] = 4096
TOTAL_SUPPLY_RAO: Final[int] = 21_000_000 * 10**9     # emission-curve cap (brief 3.1)
NOMINAL_BLOCK_MS: Final[int] = 12_000
STALL_EXTRA_MS: Final[int] = 1_800_000                # > 30 min beyond nominal block time between snapshots = stall
KEYS_PAGE: Final[int] = 1000


class SnapshotDecodeError(DecodeError):
    """A snapshot could not be decoded or failed validation. Carries the raw values for the fetch ledger."""

    def __init__(self, msg: str, block: int, block_hash: str, raw: Mapping[str, str | None] | None = None) -> None:
        super().__init__(f"block {block} ({block_hash}): {msg}")
        self.block = block
        self.block_hash = block_hash
        self.raw: dict[str, str | None] = dict(raw or {})


def snapshot_digest(snap: ChainSnapshot) -> str:
    """blake2b-128 of the canonical bytes of the snapshot with an empty digest field (section 6.8 step 9)."""
    return codec_digest(replace(snap, digest=""), 16)


def with_digest(snap: ChainSnapshot) -> ChainSnapshot:
    return replace(snap, digest=snapshot_digest(snap))


# ------------------------------------------------------------------------------------------------ helpers
def issuance_bracket(issuance_rao: int) -> int | None:
    """k = floor(log2(1 / (1 - I/21e15))) in exact integers; None at or above the cap (emission 0)."""
    if issuance_rao >= TOTAL_SUPPLY_RAO:
        return None
    rest = TOTAL_SUPPLY_RAO - issuance_rao
    k = 0
    while (rest << (k + 1)) <= TOTAL_SUPPLY_RAO:
        k += 1
    return k


def curve_block_emission(issuance_rao: int) -> int:
    """floor(1e9 * 2**-k): the brief 3.1 curve (fallback when the runtime API does not exist at that block)."""
    k = issuance_bracket(issuance_rao)
    return 0 if k is None else 10**9 >> k


class SpecCache:
    """Known (block -> (spec, tx)) points. Spec and tx versions never decrease along the chain, so a block between two
    known points with equal versions has that version too."""

    def __init__(self) -> None:
        self._pts: dict[int, tuple[int, int]] = {}

    def add(self, block: int, spec: int, tx: int) -> None:
        self._pts[block] = (spec, tx)

    def lookup(self, block: int) -> tuple[int, int] | None:
        v = self._pts.get(block)
        if v is not None:
            return v
        lo = max((b for b in self._pts if b < block), default=None)
        hi = min((b for b in self._pts if b > block), default=None)
        if lo is not None and hi is not None and self._pts[lo] == self._pts[hi]:
            return self._pts[lo]
        return None


@dataclass(frozen=True, slots=True)
class PriceParity:
    n: int
    max_rel_ppm: int
    worst_netuid: int | None
    mismatches: tuple[tuple[int, int, int], ...]     # (netuid, local spot rao, runtime price rao)

    @property
    def ok(self) -> bool:
        return not self.mismatches


@dataclass
class _Read:
    """Raw values of one snapshot read (key bytes -> value bytes | None), plus what was asked for."""
    values: dict[bytes, bytes | None] = field(default_factory=dict)

    def get(self, key: bytes) -> bytes | None:
        return self.values.get(key)

    def asked(self, key: bytes) -> bool:
        return key in self.values


# ------------------------------------------------------------------------------------------------ reader
class JsonRpcChainReader:
    """WP1 ChainReader (core.protocols.ChainReader) over an RpcPool."""

    def __init__(self, pool: RpcPool, *, layouts: SpecLayouts | None = None, role: Role = Role.ARCHIVE,
                 keys_per_call: int = 2_000, max_concurrency: int = 3, max_netuid: int = it.DEFAULT_MAX_NETUID,
                 prune_check_every: int | None = None, escrow_every: int | None = None,
                 provider_check_every: int | None = 200, on_drift: Callable[[ModelDriftObserved], None] | None = None,
                 on_raw: Callable[[int, str, Mapping[str, str | None]], None] | None = None) -> None:
        if keys_per_call < 1 or max_concurrency < 1:
            raise ValueError("keys_per_call and max_concurrency must be >= 1")
        self.pool = pool
        self.layouts = layouts or SpecLayouts()
        self.role = role
        self.keys_per_call = keys_per_call
        self.max_concurrency = max_concurrency
        self.max_netuid = max_netuid
        self.prune_check_every = prune_check_every
        self.escrow_every = escrow_every
        self.provider_check_every = provider_check_every
        self.on_drift = on_drift
        self.on_raw = on_raw
        self.specs = SpecCache()
        self._emission: dict[tuple[int, int | None], int] = {}
        self._n_snapshots = 0
        self._sem = asyncio.Semaphore(max_concurrency)

    # ------------------------------------------------------------------ simple reads
    async def block_hash(self, block: Block) -> BlockHash:
        h = await self.pool.call("chain_getBlockHash", [int(block)], self.role)
        return _check_hash(block, h)

    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]:
        """Hashes for many blocks: one chain_getBlockHash call with a list (Substrate ListOrValue), falling back to
        one call per block if the provider does not accept lists."""
        out: dict[int, BlockHash] = {}
        if not blocks:
            return out
        try:
            res = await self.pool.call("chain_getBlockHash", [list(blocks)], self.role)
        except RpcFatal:
            res = None
        if isinstance(res, list) and len(res) == len(blocks):
            for b, h in zip(blocks, res, strict=True):
                out[b] = _check_hash(b, h)
            return out
        for b in blocks:
            out[b] = await self.block_hash(Block(b))
        return out

    async def finalized_head(self) -> tuple[Block, BlockHash]:
        h = await self.pool.call("chain_getFinalizedHead", [], Role.HEAD)
        hdr = await self.pool.call("chain_getHeader", [h], Role.HEAD)
        return Block(int(hdr["number"], 16)), BlockHash(str(h).lower())

    async def header_number(self, block_hash: str, role: Role | None = None) -> int:
        hdr = await self.pool.call("chain_getHeader", [block_hash], role or self.role)
        return int(hdr["number"], 16)

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        rv = await self.pool.call("state_getRuntimeVersion", [block_hash], self.role)
        return int(rv["specVersion"]), int(rv["transactionVersion"])

    async def runtime_version(self, block: int, block_hash: BlockHash) -> tuple[int, int]:
        v = self.specs.lookup(block)
        if v is None:
            v = await self.spec_version(block_hash)
            self.specs.add(block, *v)
        return v

    async def _state_call(self, method: str, args_hex: str, block_hash: str) -> object:
        return await self.pool.call("state_call", [method, args_hex, block_hash], self.role)

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        return rt.dec_sim_swap(await self._state_call(rt.M_SIM_BUY, rt.args_netuid_amount(netuid, tao_rao), block_hash))

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        return rt.dec_sim_swap(await self._state_call(rt.M_SIM_SELL, rt.args_netuid_amount(netuid, alpha_rao), block_hash))

    async def current_price(self, netuid: NetUid, block_hash: BlockHash) -> int:
        return rt.dec_u64(await self._state_call(rt.M_PRICE, rt.args_netuid(netuid), block_hash))

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        return rt.dec_price_all(await self._state_call(rt.M_PRICE_ALL, rt.NO_ARGS, block_hash))

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        v = rt.dec_prune_target(await self._state_call(rt.M_PRUNE, rt.NO_ARGS, block_hash))
        return None if v is None else NetUid(v)

    async def next_epoch_start(self, netuid: NetUid, block_hash: BlockHash) -> Block | None:
        v = rt.dec_next_epoch(await self._state_call(rt.M_NEXT_EPOCH, rt.args_netuid(netuid), block_hash))
        return None if v is None else Block(v)

    async def registration_cost(self, block_hash: BlockHash) -> Rao:
        return Rao(rt.dec_u64(await self._state_call(rt.M_REG_COST, rt.NO_ARGS, block_hash)))

    async def runtime_block_emission(self, block_hash: BlockHash) -> Rao:
        return Rao(rt.dec_u64(await self._state_call(rt.M_BLOCK_EMISSION, rt.NO_ARGS, block_hash)))

    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]:
        """Basket escrow alpha E per netuid: the escrow coldkey's StakeInfo rows summed over validator hotkeys (the
        section 6.6 StakeInfo route, decoded by hand; no scalecodec needed on Windows)."""
        rows = rt.dec_stake_info_vec(await self._state_call(
            rt.M_STAKE_INFO_COLDKEY, rt.args_account(rt.ESCROW_ACCOUNT), block_hash))
        return {k: AlphaRao(v) for k, v in rt.escrow_by_subnet(rows).items()}

    async def dividend_keys(self, netuid: NetUid, block_hash: BlockHash) -> tuple[Hotkey, ...]:
        """Hotkeys h with a key AlphaDividendsPerSubnet(netuid, h) at this block (state_getKeysPaged, 1,000 per page)."""
        row = it.HOTKEY["last_dividend"]
        pre = prefix(row.pallet, row.item) + h_identity(h_le16(int(netuid)))
        out: list[Hotkey] = []
        start: str | None = None
        while True:
            page = await self.pool.call("state_getKeysPaged", [to_hex(pre), KEYS_PAGE, start, block_hash], self.role)
            if not isinstance(page, list):
                raise DecodeError(f"state_getKeysPaged returned {type(page).__name__}")
            for k in page:
                try:
                    parts = split_key(from_hex(str(k)), row.pallet, row.item, row.hashers, (2, 32))
                except ValueError as e:
                    raise DecodeError(f"dividend key {str(k)[:80]}: {e}") from e
                if int.from_bytes(parts[0], "little") != int(netuid):
                    raise DecodeError(f"dividend key for netuid {int.from_bytes(parts[0], 'little')} under {netuid}")
                out.append(Hotkey(to_hex(parts[1])))
            if len(page) < KEYS_PAGE:
                break
            start = str(page[-1])
        return tuple(sorted(set(out)))

    async def block_emission(self, spec: int, issuance: int, block_hash: BlockHash) -> Rao:
        """Runtime get_block_emission, cached per (spec, issuance bracket): it is a step function of TotalIssuance.
        Falls back to the exact integer curve only where the runtime API does not exist (a non-retryable error);
        transient exhaustion propagates (the snapshot fails rather than silently using the fallback)."""
        key = (spec, issuance_bracket(issuance))
        v = self._emission.get(key)
        if v is None:
            try:
                v = int(await self.runtime_block_emission(block_hash))
            except RpcFatal:
                v = curve_block_emission(issuance)
            self._emission[key] = v
        return Rao(v)

    # ------------------------------------------------------------------ storage
    async def query(self, keys: Sequence[bytes], block_hash: str, role: Role | None = None) -> dict[bytes, bytes | None]:
        """state_queryStorageAt in chunks; returns every requested key (None = absent)."""
        uniq = list(dict.fromkeys(keys))
        n_chunks = -(-len(uniq) // self.keys_per_call)                 # balanced chunks of <= keys_per_call
        size = -(-len(uniq) // n_chunks) if n_chunks else 1
        chunks = [uniq[i:i + size] for i in range(0, len(uniq), size)]
        r = role or self.role

        async def one(chunk: list[bytes]) -> dict[bytes, bytes | None]:
            async with self._sem:
                res = await self.pool.call("state_queryStorageAt", [[to_hex(k) for k in chunk], block_hash], r)
            return _parse_query(res, chunk, block_hash)

        out: dict[bytes, bytes | None] = {}
        for part in await asyncio.gather(*(one(c) for c in chunks)):
            out.update(part)
        return out

    # ------------------------------------------------------------------ snapshot
    async def snapshot(self, block: Block, block_hash: BlockHash, plan: ReadPlan, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]], *, held: Iterable[int] = (),
                       metagraph_for: Iterable[int] = (), role: Role | None = None) -> ChainSnapshot:
        """One all-or-nothing snapshot at (block, block_hash). HEAD without `prev` is read as FULL; on HEAD, generations
        not in `prev` are read in full. `held` netuids also read SubnetTaoInEmission/SubnetExcessTao on HEAD plans;
        `metagraph_for` netuids get MetagraphLite (LCW only)."""
        r = role or self.role
        ctx = await self._begin(Block(int(block)), BlockHash(block_hash.lower()), plan, prev is not None, held, r)
        return await self._finish(ctx, prev, tracked, metagraph_for, r)

    async def pull(self, blocks: Sequence[int],
                   tracked: Callable[[ChainSnapshot | None], Sequence[tuple[SubnetKey, Hotkey]]] = lambda _p: (), *,
                   full_every: int = 60, prev: ChainSnapshot | None = None, role: Role | None = None,
                   held: Iterable[int] = (), prefetch: int | None = None) -> AsyncGenerator[ChainSnapshot, None]:
        """Historical pull of the given blocks in ascending order: one batched chain_getBlockHash, runtime versions by
        bracketing, then FULL at block % full_every == 0 (and the first block without `prev`) and HEAD in between.
        The first (prev-independent) read of up to `prefetch` (default max_concurrency) later blocks runs ahead."""
        bs = sorted({int(b) for b in blocks})
        if not bs:
            return
        r = role or self.role
        held_t = tuple(int(n) for n in held)
        hashes = await self.block_hashes(bs)
        await self._bracket_versions(bs, hashes)
        ahead = max(1, self.max_concurrency if prefetch is None else prefetch)
        plans: list[ReadPlan] = []
        has_prev = prev is not None
        for b in bs:
            plans.append(ReadPlan.FULL if (not has_prev or b % full_every == 0) else ReadPlan.HEAD)
            has_prev = True
        tasks: dict[int, asyncio.Task[_Ctx]] = {}

        def launch(i: int) -> None:
            if i < len(bs) and i not in tasks:
                tasks[i] = asyncio.create_task(self._begin(Block(bs[i]), hashes[bs[i]], plans[i], plans[i] is ReadPlan.HEAD,
                                                           held_t, r))

        try:
            for i in range(min(ahead, len(bs))):
                launch(i)
            for i, _b in enumerate(bs):
                launch(i)
                ctx = await tasks.pop(i)
                launch(i + ahead)
                snap = await self._finish(ctx, prev, tracked(prev), (), r)
                prev = snap
                yield snap
        finally:
            for t in tasks.values():
                t.cancel()
            for t in tasks.values():
                with contextlib.suppress(asyncio.CancelledError, Exception):   # cancelled prefetch of an abandoned pull
                    await t

    async def _bracket_versions(self, bs: Sequence[int], hashes: Mapping[int, BlockHash]) -> None:
        if not bs:
            return

        async def ver(b: int) -> tuple[int, int]:
            v = self.specs.lookup(b)
            if v is None:
                v = await self.spec_version(hashes[b])
                self.specs.add(b, *v)
            return v

        async def bisect(lo: int, hi: int) -> None:     # indexes into bs
            if hi - lo <= 1 or await ver(bs[lo]) == await ver(bs[hi]):
                return
            mid = (lo + hi) // 2
            await ver(bs[mid])
            await bisect(lo, mid)
            await bisect(mid, hi)

        a, z = await asyncio.gather(ver(bs[0]), ver(bs[-1]))
        if a != z:
            await bisect(0, len(bs) - 1)

    # ------------------------------------------------------------------ read phases
    async def _begin(self, block: Block, block_hash: BlockHash, plan: ReadPlan, has_prev: bool, held: Iterable[int],
                     role: Role) -> _Ctx:
        """Runtime version + the prev-independent first read (globals and per-subnet rows of the plan)."""
        spec, tx = await self.runtime_version(int(block), block_hash)
        ctx = _Ctx(block=block, block_hash=block_hash, spec=spec, tx=tx, layout=self.layouts.for_spec(spec),
                   plan=plan if (plan is ReadPlan.FULL or has_prev) else ReadPlan.FULL, held={int(n) for n in held})
        try:
            await self._phase1(ctx, role)
        except DecodeError as e:
            raise _wrap(e, ctx) from e
        return ctx

    async def _finish(self, ctx: _Ctx, prev: ChainSnapshot | None, tracked: Sequence[tuple[SubnetKey, Hotkey]],
                      metagraph_for: Iterable[int], role: Role) -> ChainSnapshot:
        if ctx.plan is ReadPlan.HEAD and prev is None:
            raise ValueError("a HEAD read needs the previous snapshot")
        try:
            issuance = ctx.decode_global(it.GLOBAL["total_issuance"], ctx.read)
            if issuance is None:
                raise DecodeError("SubtensorModule.TotalIssuance missing")
        except DecodeError as e:
            raise _wrap(e, ctx) from e
        # runtime-API cross-checks need only phase-1 values: run them alongside the second read
        checks = asyncio.ensure_future(self._runtime_checks(ctx, int(issuance)))
        try:
            await self._phase2(ctx, prev, tracked, {int(n) for n in metagraph_for}, role)
            snap = self._assemble(ctx, prev)
        except BaseException as e:
            checks.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await checks
            if isinstance(e, DecodeError):
                raise _wrap(e, ctx) from e
            raise
        snap = _apply_checks(snap, *(await checks))
        if self.on_raw is not None:
            self.on_raw(int(ctx.block), ctx.block_hash, ctx.raw_hex())
        self._n_snapshots += 1
        if self.provider_check_every and self._n_snapshots % self.provider_check_every == 0:
            await self.check_providers(ctx, role)
        return with_digest(snap)

    async def _phase1(self, ctx: _Ctx, role: Role) -> None:
        raw = ctx.read
        full = ctx.plan is ReadPlan.FULL
        lo, hi = 1, self.max_netuid
        keys = [row.key() for row in it.GLOBAL_ROWS if ctx.wants(row)]
        while True:
            for n in range(lo, hi + 1):
                for row in it.SUBNET_ROWS:
                    if ctx.wants(row) and (full or row.plan is Plan.HEAD or (row.plan is Plan.HELD and n in ctx.held)):
                        keys.append(row.key(netuid=n))
            raw.values.update(await self.query(keys, ctx.block_hash, role))
            keys = []
            upper = [it.SUBNET[it.NETWORKS_ADDED].key(netuid=hi), it.SUBNET["reg_at"].key(netuid=hi)]
            limit = ctx.decode_global(it.GLOBAL["subnet_limit"], raw)
            want_hi = max(hi, int(limit) + it.NETUID_EXTENSION) if isinstance(limit, int) else hi
            if any(raw.get(k) is not None for k in upper) or want_hi > hi:
                lo, hi = hi + 1, max(hi + it.NETUID_EXTENSION, want_hi)
                self.max_netuid = max(self.max_netuid, hi)
                continue
            break
        ctx.max_netuid = hi

    async def _phase2(self, ctx: _Ctx, prev: ChainSnapshot | None, tracked: Sequence[tuple[SubnetKey, Hotkey]],
                      mg: set[int], role: Role) -> None:
        raw = ctx.read
        added = ctx.added_netuids(raw)
        # generations not in prev need their FULL rows on a HEAD plan
        if ctx.plan is ReadPlan.HEAD:
            keys: list[bytes] = []
            for n in added:
                ps = prev.by_netuid(n) if prev is not None else None
                if ps is None or int(ps.key.reg_at) != ctx.reg_at(raw, n):
                    ctx.full_netuids.add(n)
                    keys += [row.key(netuid=n) for row in it.SUBNET_ROWS
                             if ctx.wants(row) and row.plan in (Plan.FULL, Plan.HELD)]
            if keys:
                raw.values.update(await self.query(keys, ctx.block_hash, role))
        else:
            ctx.full_netuids.update(added)
        # hotkey panel (+ owner position on FULL reads) and the LCW metagraph
        pairs = self._panel_pairs(ctx, raw, added, prev, tracked)
        keys = []
        for n, hk in sorted(pairs):
            keys += [row.key(netuid=n, hotkey=hk) for row in it.HOTKEY_ROWS if ctx.wants(row)]
        owners: dict[int, tuple[str, str]] = {}
        for n in sorted(ctx.full_netuids):
            ock = ctx.account(raw, it.SUBNET["owner_coldkey"], n)
            ohk = ctx.account(raw, it.SUBNET["owner_hotkey"], n)
            if ock is not None and ohk is not None:
                owners[n] = (ohk, ock)
                keys += [row.key(netuid=n, hotkey=ohk, coldkey=ock) for row in it.OWNER_ROWS if ctx.wants(row)]
        ctx.panel_pairs = pairs
        ctx.owners = owners
        mg_nets = sorted(mg & set(added))
        if mg_nets:
            keys += [row.key(netuid=n) for n in mg_nets
                     for row in (it.METAGRAPH["n_uids"], it.METAGRAPH["incentive"], it.METAGRAPH["validator_permit"])
                     if ctx.wants(row)]
        if keys:
            raw.values.update(await self.query(keys, ctx.block_hash, role))
        if mg_nets:
            await self._read_metagraph(ctx, raw, mg_nets, role)

    def _panel_pairs(self, ctx: _Ctx, raw: _Read, added: Sequence[int], prev: ChainSnapshot | None,
                     tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> set[tuple[int, str]]:
        """(netuid, hotkey) pairs read at this block: tracked pairs of live generations plus each FULL-read subnet's
        owner hotkey. On HEAD plans only pairs that are new, belong to a newly read generation or to a subnet whose
        epoch drained since prev are re-read; the rest are carried."""
        gen = {n: ctx.reg_at(raw, n) for n in added}
        want: set[tuple[int, str]] = set()
        for key, thk in tracked:
            n = int(key.netuid)
            if n in gen and gen[n] == int(key.reg_at):
                want.add((n, str(thk).lower()))
        for n in sorted(ctx.full_netuids):
            ohk = ctx.account(raw, it.SUBNET["owner_hotkey"], n)
            if ohk is not None:
                want.add((n, ohk))
        if ctx.plan is ReadPlan.FULL or prev is None:
            return want
        out: set[tuple[int, str]] = set()
        drained = {n for n in added if n not in ctx.full_netuids and (ps := prev.by_netuid(n)) is not None
                   and int(ps.last_epoch_block) != ctx.last_epoch(raw, n)}
        for n, hk in want:
            ps = prev.by_netuid(n)
            if n in ctx.full_netuids or n in drained or ps is None or ps.hotkey(Hotkey(hk)) is None:
                out.add((n, hk))
        for n in drained:                                            # re-read every carried pair of drained subnets
            ps = prev.by_netuid(n)
            if ps is not None:
                out.update((n, str(h.hotkey)) for h in ps.hotkeys)
        return out

    async def _read_metagraph(self, ctx: _Ctx, raw: _Read, netuids: Sequence[int], role: Role) -> None:
        keys: list[bytes] = []
        for n in netuids:
            keys += [it.METAGRAPH["uid_hotkey"].key(netuid=n, uid=u) for u in ctx.metagraph_uids(raw, n) if u < MAX_UIDS]
        if keys:
            raw.values.update(await self.query(keys, ctx.block_hash, role))
        hks: set[str] = set()
        for n in netuids:
            for u in ctx.metagraph_uids(raw, n):
                b = raw.get(it.METAGRAPH["uid_hotkey"].key(netuid=n, uid=u))
                if b is not None:
                    hks.add(d_account(b))
        if hks:
            raw.values.update(await self.query([it.METAGRAPH["hotkey_owner"].key(hotkey=h) for h in sorted(hks)],
                                               ctx.block_hash, role))
        ctx.metagraph_netuids = set(netuids)

    # ------------------------------------------------------------------ assembly (pure on the raw values)
    def _assemble(self, ctx: _Ctx, prev: ChainSnapshot | None) -> ChainSnapshot:
        raw = ctx.read
        block = ctx.block
        number = ctx.decode_global(it.GLOBAL["system_number"], raw)
        if number is not None and int(number) != int(block):
            raise DecodeError(f"System.Number {number} != block {block}: wrong hash for this block")
        added = ctx.added_netuids(raw)
        ts = int(ctx.decode_global(it.GLOBAL["timestamp_ms"], raw) or 0)
        stalled = _stall_gap(prev, int(block), ts)
        glob = ctx.build_globals(raw, n_nonroot=len(added))
        subnets = tuple(ctx.build_subnet(raw, n, prev, stalled) for n in added)
        got = [int(s.key.netuid) for s in subnets]
        if got != sorted(set(got)) or got != added or 0 in got:
            raise DecodeError("membership assertion failed: subnets != sorted non-root NetworksAdded netuids")
        for s in subnets:
            raw_added = raw.get(it.SUBNET[it.NETWORKS_ADDED].key(netuid=int(s.key.netuid)))
            if raw_added is None or not it.SUBNET[it.NETWORKS_ADDED].decode(raw_added):
                raise DecodeError(f"membership assertion failed: netuid {int(s.key.netuid)} is not NetworksAdded")
        return ChainSnapshot(block=block, block_hash=ctx.block_hash, timestamp_ms=ts, plan=ctx.plan, glob=glob,
                             subnets=subnets)

    async def _runtime_checks(self, ctx: _Ctx, issuance: int) -> tuple[Rao, NetUid | None, dict[int, AlphaRao] | None]:
        """(block emission, runtime prune target or None, escrow per netuid or None) at the snapshot hash."""
        block = int(ctx.block)
        want_prune = bool(self.prune_check_every) and block % int(self.prune_check_every or 1) == 0
        want_escrow = bool(self.escrow_every) and block >= ESCROW_FIRST_BLOCK and block % int(self.escrow_every or 1) == 0

        async def none() -> None:
            return None

        emission, prune, esc = await asyncio.gather(
            self.block_emission(ctx.spec, issuance, ctx.block_hash),
            self.subnet_to_prune(ctx.block_hash) if want_prune else none(),
            self.escrow_by_subnet(ctx.block_hash) if want_escrow else none())
        return emission, prune, esc

    async def check_providers(self, ctx: _Ctx, role: Role) -> list[str]:
        """Compare 3 deterministic keys of this snapshot on two providers at the same hash; a mismatch quarantines the
        second provider and reports ModelDriftObserved(probe="provider")."""
        eps = [ep for ep in self.pool.endpoints(role) if not ep.quarantined]
        if len(eps) < 2:
            eps = [ep for ep in self.pool.endpoints(Role.ARCHIVE) if not ep.quarantined]
        keys = sorted(k for k in ctx.read.values if ctx.read.values[k] is not None)
        if len(eps) < 2 or not keys:
            return []
        seed = int.from_bytes(hashlib.blake2b(bytes.fromhex(ctx.block_hash[2:]), digest_size=8).digest(), "little")
        pick = list(dict.fromkeys(keys[(seed // (7919 ** i)) % len(keys)] for i in range(3)))
        a, b = eps[0], eps[1]
        params = [[to_hex(k) for k in pick], ctx.block_hash]
        try:
            ra = _parse_query(await self.pool.call_on(a.label, "state_queryStorageAt", params), pick, ctx.block_hash)
            rb = _parse_query(await self.pool.call_on(b.label, "state_queryStorageAt", params), pick, ctx.block_hash)
        except (RpcError, DecodeError):
            return []
        bad = [to_hex(k) for k in pick if ra.get(k) != rb.get(k)]
        if bad:
            self.pool.quarantine(b.label)
            if self.on_drift is not None:
                self.on_drift(ModelDriftObserved(block=ctx.block, probe="provider", netuid=None, err_ppm=PPM))
        return bad

    async def price_parity(self, snap: ChainSnapshot, rel_ppm: int = 1) -> PriceParity:
        """Local spot (floor rao) vs SwapRuntimeApi_current_alpha_price_all at the snapshot hash. A netuid fails when
        |local - runtime| > max(1 rao, rel_ppm * runtime / 1e6) (the 1-rao floor absorbs both sides' flooring)."""
        chain = await self.prices_all(snap.block_hash)
        return compare_prices(snap, chain, rel_ppm)

    async def metagraph(self, netuid: NetUid, block: Block, block_hash: BlockHash) -> MetagraphLite | None:
        """MetagraphLite of one subnet from storage (Incentive, ValidatorPermit, Keys, Owner); LCW only."""
        spec, tx = await self.runtime_version(int(block), block_hash)
        ctx = _Ctx(block=Block(int(block)), block_hash=block_hash, spec=spec, tx=tx, layout=self.layouts.for_spec(spec),
                   plan=ReadPlan.FULL)
        keys = [row.key(netuid=int(netuid)) for row in (it.METAGRAPH["n_uids"], it.METAGRAPH["incentive"],
                                                        it.METAGRAPH["validator_permit"])]
        ctx.read.values.update(await self.query(keys, block_hash))
        await self._read_metagraph(ctx, ctx.read, [int(netuid)], self.role)
        return ctx.build_metagraph(ctx.read, int(netuid))


def _apply_checks(snap: ChainSnapshot, emission: Rao, prune: NetUid | None,
                  esc: Mapping[int, AlphaRao] | None) -> ChainSnapshot:
    glob = replace(snap.glob, block_emission=emission, runtime_prune_target=prune)
    subnets = snap.subnets
    if esc is not None:
        subnets = tuple(replace(s, escrow_alpha=AlphaRao(esc.get(int(s.key.netuid), 0))) for s in subnets)
    return replace(snap, glob=glob, subnets=subnets)


def _check_hash(block: int, h: object) -> BlockHash:
    if not isinstance(h, str) or not h.startswith("0x") or len(h) != 66:
        raise DecodeError(f"chain_getBlockHash({block}) returned {h!r}")
    return BlockHash(h.lower())


def _wrap(e: DecodeError, ctx: _Ctx) -> SnapshotDecodeError:
    if isinstance(e, SnapshotDecodeError):
        return e
    return SnapshotDecodeError(str(e), int(ctx.block), ctx.block_hash, ctx.raw_hex())


def _stall_gap(prev: ChainSnapshot | None, block: int, ts_ms: int) -> bool:
    """The 2025-05-20 freeze window, or any interval since prev whose wall time exceeds nominal by > 30 min."""
    lo, hi = it.CHAIN_STALL_BLOCKS
    if block in (lo, hi):
        return True
    if prev is None:
        return False
    if int(prev.block) <= lo < block:
        return True
    if prev.timestamp_ms and ts_ms:
        return ts_ms - prev.timestamp_ms > (block - int(prev.block)) * NOMINAL_BLOCK_MS + STALL_EXTRA_MS
    return False


def compare_prices(snap: ChainSnapshot, chain: Mapping[int, int], rel_ppm: int = 1) -> PriceParity:
    worst: tuple[int, int | None] = (0, None)
    bad: list[tuple[int, int, int]] = []
    n = 0
    for s in snap.subnets:
        net = int(s.key.netuid)
        if net not in chain:
            continue
        n += 1
        local = int(s.pool.spot_rao())
        price = chain[net]
        diff = abs(local - price)
        rel = 0 if price == 0 else diff * PPM // price
        if rel > worst[0]:
            worst = (rel, net)
        if diff > max(1, rel_ppm * price // PPM):
            bad.append((net, local, price))
    return PriceParity(n=n, max_rel_ppm=worst[0], worst_netuid=worst[1], mismatches=tuple(bad))


def _parse_query(res: Any, asked: Sequence[bytes], block_hash: str) -> dict[bytes, bytes | None]:
    """Strict: the answer is for `block_hash` and contains every asked key exactly (null = absent)."""
    if not isinstance(res, list):
        raise DecodeError(f"state_queryStorageAt returned {type(res).__name__}")
    out: dict[bytes, bytes | None] = {}
    want = set(asked)
    for change_set in res:
        if not isinstance(change_set, Mapping):
            raise DecodeError("state_queryStorageAt: malformed change set")
        blk = change_set.get("block")
        if blk is not None and str(blk).lower() != block_hash.lower():
            raise DecodeError(f"state_queryStorageAt answered for {blk}, asked {block_hash}")
        for kv in change_set.get("changes", []):
            try:
                k = from_hex(str(kv[0]))
                v = None if kv[1] is None else from_hex(str(kv[1]))
            except (ValueError, IndexError, TypeError) as e:
                raise DecodeError(f"state_queryStorageAt: bad change entry {str(kv)[:80]}") from e
            if k in want:
                out[k] = v
    if len(out) != len(want):
        raise DecodeError(f"state_queryStorageAt answered {len(out)} of {len(want)} keys")
    return out


# ------------------------------------------------------------------------------------------------ decode context
@dataclass
class _Ctx:
    block: Block
    block_hash: BlockHash
    spec: int
    tx: int
    layout: SpecLayout | None
    plan: ReadPlan
    held: set[int] = field(default_factory=set)
    read: _Read = field(default_factory=_Read)
    max_netuid: int = it.DEFAULT_MAX_NETUID
    full_netuids: set[int] = field(default_factory=set)
    panel_pairs: set[tuple[int, str]] = field(default_factory=set)
    owners: dict[int, tuple[str, str]] = field(default_factory=dict)
    metagraph_netuids: set[int] = field(default_factory=set)
    filled_unvalidated: set[int] = field(default_factory=set)       # netuids (0 = globals) default-filled from a
                                                                     # layout that is not an exact validated one

    @property
    def trusted(self) -> bool:
        return self.layout is not None and self.layout.exact and self.layout.validated

    def raw_hex(self) -> dict[str, str | None]:
        return {to_hex(k): (None if v is None else to_hex(v)) for k, v in sorted(self.read.values.items())}

    def exists(self, row: Row) -> bool | None:
        """True/False when an exact layout says whether the item exists in this runtime; None when unknown."""
        lay = self.layout
        if lay is not None and lay.exact:
            return lay.has(row)
        if lay is not None and lay.has(row):
            return True
        return None

    def wants(self, row: Row) -> bool:
        """Read this row's keys at this block. Exact layout: the item exists (a legacy row only where its primary does
        not). Unknown spec (fallback or no layout): every row the fallback layout has or whose block bounds admit
        the block, so new items of an unknown spec are still read."""
        ex = self.exists(row)
        if ex is False:
            return False
        primary = it.primary_of(row)
        if primary is not None and self.exists(primary) is True:
            return False
        return ex is True or row.live_at(int(self.block))

    # ---------------------------------------------------------------- value resolution
    def resolve(self, row: Row, raw: bytes | None, unit: int) -> tuple[Any, bool]:
        """(value, present). Absent -> layout default (ValueQuery) / None (OptionQuery) / registry fallback."""
        if raw is not None:
            return row.decode(raw), True
        lay = self.layout
        e = lay.entry(row) if lay is not None else None
        if e is None:
            if self.exists(row) is None and row.query is Query.VALUE and self.wants(row):
                self.filled_unvalidated.add(unit)          # unknown runtime: the fallback is a guess
            return row.fallback, False
        if e.modifier == Query.OPTION.value:
            return None, False
        if not self.trusted:
            self.filled_unvalidated.add(unit)
        return row.decode(e.default_bytes), False

    def value(self, raw: _Read, row: Row, netuid: int) -> tuple[Any, bool]:
        return self.resolve(row, raw.get(row.key(netuid=netuid)), netuid)

    def decode_global(self, row: Row, raw: _Read) -> Any:
        k = row.key()
        if not raw.asked(k) and not self.wants(row):
            return row.fallback
        return self.resolve(row, raw.get(k), 0)[0]

    def with_legacy(self, primary: Row, legacy: Row, get: Callable[[Row], tuple[Any, bool]]) -> Any:
        """The primary item where it exists (or is present); otherwise the legacy item where present; else the
        primary's resolved value (its fallback/default)."""
        pv, pp = get(primary)
        if pp or self.exists(primary) is True:
            return pv
        lv, lp = get(legacy)
        return lv if lp else pv

    def account(self, raw: _Read, row: Row, netuid: int) -> str | None:
        v = self.value(raw, row, netuid)[0]
        return None if v is None or v == ZERO_ACCOUNT else str(v)

    def reg_at(self, raw: _Read, n: int) -> int:
        return int(self.value(raw, it.SUBNET["reg_at"], n)[0])

    def last_epoch(self, raw: _Read, n: int) -> int:
        return int(self.with_legacy(it.SUBNET["last_epoch_block"], it.SUBNET["last_epoch_block_legacy"],
                                    lambda r: self.value(raw, r, n) if raw.asked(r.key(netuid=n)) else (r.fallback, False)))

    def added_netuids(self, raw: _Read) -> list[int]:
        row = it.SUBNET[it.NETWORKS_ADDED]
        out: list[int] = []
        for n in range(1, self.max_netuid + 1):
            b = raw.get(row.key(netuid=n))
            if b is not None and bool(row.decode(b)):
                out.append(n)
        return out

    # ---------------------------------------------------------------- globals
    def build_globals(self, raw: _Read, n_nonroot: int) -> ChainGlobals:
        g = {f: self.decode_global(row, raw) for f, row in it.GLOBAL.items()}
        if g["total_issuance"] is None:
            raise DecodeError("SubtensorModule.TotalIssuance missing")
        last_reg = max(int(g["last_reg_block"] or 0), int(g["last_reg_block_legacy"] or 0))
        nominator_min = int(g["nominator_min_factor"]) * MIN_STAKE_RAO // PPM
        return ChainGlobals(
            spec_version=self.spec, tx_version=self.tx, total_issuance=Rao(int(g["total_issuance"])),
            block_emission=Rao(0), moving_alpha=g["moving_alpha"], gate_bar=g["gate_bar"], gate_rank=int(g["gate_rank"]),
            gate_exponent=int(g["gate_exponent"]), tao_weight=g["tao_weight"], root_tao=Rao(int(g["root_tao"])),
            owner_cut_u16=int(g["owner_cut_u16"]), subnet_limit=int(g["subnet_limit"]),
            immunity_period=int(g["immunity_period"]), network_rate_limit=int(g["network_rate_limit"]),
            last_reg_block=Block(last_reg), last_lock_cost=Rao(int(g["last_lock_cost"])),
            min_lock_cost=Rao(int(g["min_lock_cost"])), lock_reduction_interval=int(g["lock_reduction_interval"]),
            tao_in_refund_block=Block(int(g["tao_in_refund_block"])), nominator_min_stake=Rao(nominator_min),
            cleanup_queue_len=int(g["cleanup_queue_len"]), n_nonroot_networks=n_nonroot,
            safe_mode_until=None if g["safe_mode_until"] is None else Block(int(g["safe_mode_until"])))

    # ---------------------------------------------------------------- subnets
    def build_subnet(self, raw: _Read, n: int, prev: ChainSnapshot | None, stalled: bool) -> SubnetState:
        block = int(self.block)
        full = n in self.full_netuids
        reg_at = self.reg_at(raw, n)
        key = SubnetKey(NetUid(n), Block(reg_at))
        ps = prev.get(key) if prev is not None else None
        f: dict[str, Any] = {}
        for name, row in it.SUBNET.items():
            k = row.key(netuid=n)
            if raw.asked(k) or not self.wants(row):
                f[name] = self.value(raw, row, n)[0]
            else:
                f[name] = row.fallback
        f["last_epoch_block"] = self.last_epoch(raw, n)
        q = Quality.OK
        if not full:
            if ps is None:
                raise DecodeError(f"netuid {n}: HEAD read without a FULL source")
            q |= Quality.CARRIED
        fee = f["fee_rate"]
        if fee is None:
            fee = fee_rate_default(self.spec)
            q |= Quality.DEFAULT_FILLED
        pool, pq = self._pool(f, int(fee), n)
        q |= pq
        if 0 in self.filled_unvalidated or n in self.filled_unvalidated:
            q |= Quality.DEFAULT_FILLED
        if f["first_emission_block"] is None and self.exists(it.SUBNET["first_emission_block"]) is not False:
            q |= Quality.NOT_STARTED                    # (runtimes before FirstEmissionBlockNumber had no start_call)
        if stalled:
            q |= Quality.CHAIN_STALL_GAP
        if block < it.DTAO_LAUNCH_BLOCK + it.EARLY_TINY_POOL_BLOCKS and int(f["tao"]) < it.EARLY_TINY_POOL_RAO:
            q |= Quality.EARLY_TINY_POOL
        if abs(block - it.BALANCER_FIRST_BLOCK) <= it.SEED_WINDOW_BLOCKS:
            q |= Quality.BALANCER_MIGRATION
            if (pool.kind is PoolKind.BALANCER and pool.w_quote_e18 == HALF_E18 and ps is not None
                    and _jumped(ps.pool, pool)):
                q |= Quality.SEED_FALLBACK
        hotkeys = self._hotkeys(raw, n, ps, full)
        if not any(h.earns for h in hotkeys):
            q |= Quality.NO_YIELD_IDX
        common: dict[str, Any] = {
            "key": key, "pool": pool, "moving_price": f["moving_price"], "emission_enabled": bool(f["emission_enabled"]),
            "subtoken_enabled": bool(f["subtoken_enabled"]), "reg_allowed": bool(f["reg_allowed"]),
            "first_emission_block": None if f["first_emission_block"] is None else Block(int(f["first_emission_block"])),
            "last_epoch_block": Block(int(f["last_epoch_block"])), "tao_flow_cum": f["tao_flow_cum"], "hotkeys": hotkeys,
        }
        if full:
            owner_ck = self.account(raw, it.SUBNET["owner_coldkey"], n)
            owner_hk = self.account(raw, it.SUBNET["owner_hotkey"], n)
            return SubnetState(
                alpha_out=AlphaRao(int(f["alpha_out"])), protocol_alpha=AlphaRao(int(f["protocol_alpha"])),
                root_prop=f["root_prop"], miner_burned=f["miner_burned"], tempo=int(f["tempo"]),
                ema_halving_blocks=int(f["ema_halving_blocks"]), tao_in_emission=Rao(int(f["tao_in_emission"])),
                excess_tao=Rao(int(f["excess_tao"])), alpha_out_emission=AlphaRao(int(f["alpha_out_emission"])),
                alpha_in_emission=AlphaRao(int(f["alpha_in_emission"])), reservoir_tao=Rao(int(f["reservoir_tao"])),
                reservoir_alpha=AlphaRao(int(f["reservoir_alpha"])),
                volume_cum=None if f["volume_cum"] is None else int(f["volume_cum"]),
                fast_moving_price=f["fast_moving_price"],
                owner_coldkey=None if owner_ck is None else Coldkey(owner_ck),
                owner_hotkey=None if owner_hk is None else Hotkey(owner_hk),
                owner_cut_enabled=None if f["owner_cut_enabled"] is None else bool(f["owner_cut_enabled"]),
                owner_cut_autolock=None if f["owner_cut_autolock"] is None else bool(f["owner_cut_autolock"]),
                total_alpha_staked=None if f["total_alpha_staked"] is None else AlphaRao(int(f["total_alpha_staked"])),
                escrow_alpha=ps.escrow_alpha if ps is not None else None,
                owner_alpha=self._owner_alpha(raw, n, hotkeys),
                max_allowed_validators=None if f["max_allowed_validators"] is None else int(f["max_allowed_validators"]),
                consensus_mode=None if f["consensus_mode"] is None else int(f["consensus_mode"]),
                metagraph=self.build_metagraph(raw, n) if n in self.metagraph_netuids else None,
                quality=q, **common)
        assert ps is not None
        upd: dict[str, Any] = dict(common)
        for name in ("tao_in_emission", "excess_tao"):
            if raw.asked(it.SUBNET[name].key(netuid=n)):
                upd[name] = Rao(int(f[name]))
        if n in self.metagraph_netuids:
            upd["metagraph"] = self.build_metagraph(raw, n)
        return replace(ps, quality=q, **upd)

    def _pool(self, f: Mapping[str, Any], fee: int, n: int) -> tuple[PoolState, Quality]:
        block = int(self.block)
        tao, alpha = int(f["tao"]), int(f["alpha_in"])
        q = Quality.OK
        if block >= it.BALANCER_FIRST_BLOCK:
            wq = int(f["w_quote_e18"])
            if not (PERQUINTILL // 100 <= wq <= 99 * PERQUINTILL // 100):
                raise DecodeError(f"netuid {n}: Balancer quote {wq} outside [0.01, 0.99]")
            pool = PoolState(PoolKind.BALANCER, Rao(tao), AlphaRao(alpha), tao, alpha, wq, fee)
        else:
            sqrt_p = f.get("v3_sqrt_price")
            liq = f.get("v3_liquidity")
            if block >= it.ERA_B_FIRST_BLOCK and isinstance(sqrt_p, int) and sqrt_p > 0 and isinstance(liq, int) and liq > 0:
                px_tao = liq * sqrt_p >> 64                    # L * sqrt(P); sqrt_p is the raw U64F64 (sqrt(P) * 2**64)
                px_alpha = (liq << 64) // sqrt_p               # L / sqrt(P)
                pool = PoolState(PoolKind.CP_V3_VIRTUAL, Rao(tao), AlphaRao(alpha), px_tao, px_alpha, HALF_E18, fee)
            else:
                pool = PoolState(PoolKind.CP_REAL, Rao(tao), AlphaRao(alpha), tao, alpha, HALF_E18, fee)
                if block > it.TA_DIVERGENCE_BLOCK:
                    q |= Quality.TA_PRICE
        if min(pool.tao, pool.alpha, pool.px_tao, pool.px_alpha) <= 0:
            raise DecodeError(f"netuid {n}: non-positive reserves {pool}")
        return pool, q

    def _hotkeys(self, raw: _Read, n: int, ps: SubnetState | None, full: bool) -> tuple[HotkeyIdx, ...]:
        fresh: dict[str, HotkeyIdx] = {}
        for nn, hk in sorted(self.panel_pairs):
            if nn == n:
                fresh[hk] = self._hotkey(raw, n, hk)
        if full or ps is None:
            return tuple(fresh[h] for h in sorted(fresh))
        merged = {str(h.hotkey): h for h in ps.hotkeys}
        merged.update(fresh)
        return tuple(merged[h] for h in sorted(merged))

    def _hotkey(self, raw: _Read, n: int, hk: str) -> HotkeyIdx:
        vals: dict[str, tuple[Any, bool]] = {}
        for name, row in it.HOTKEY.items():
            k = row.key(netuid=n, hotkey=hk)
            vals[name] = self.resolve(row, raw.get(k), n) if (raw.asked(k) or not self.wants(row)) else (row.fallback, False)
        v1, v1_present = vals["shares_v1"]
        v2 = vals["shares_v2"][0]
        shares = v1 if v1_present else (v2 if v2 is not None else Decimal(0))
        div, earns = vals["last_dividend"]
        return HotkeyIdx(hotkey=Hotkey(hk), total_alpha=AlphaRao(int(vals["total_alpha"][0])), total_shares=shares,
                         take_u16=int(vals["take_u16"][0]), childkey_take_u16=int(vals["childkey_take_u16"][0]),
                         earns=earns, last_dividend=AlphaRao(int(div) if earns and div is not None else 0))

    def _owner_alpha(self, raw: _Read, n: int, hotkeys: Sequence[HotkeyIdx]) -> AlphaRao | None:
        own = self.owners.get(n)
        if own is None:
            return None
        ohk, ock = own
        legacy_row, v2_row = it.OWNER["owner_shares_legacy"], it.OWNER["owner_shares_v2"]
        if not (self.wants(legacy_row) or self.wants(v2_row)):
            return None
        legacy = self.resolve(legacy_row, raw.get(legacy_row.key(netuid=n, hotkey=ohk, coldkey=ock)), n)
        v2 = self.resolve(v2_row, raw.get(v2_row.key(netuid=n, hotkey=ohk, coldkey=ock)), n)
        if legacy[1]:
            shares = legacy[0]
        elif v2[1]:
            shares = v2[0]
        else:
            shares = Decimal(0)
        idx = next((h for h in hotkeys if str(h.hotkey) == ohk), None)
        if idx is None:
            return None
        return idx.value_of(shares)

    def metagraph_uids(self, raw: _Read, n: int) -> list[int]:
        inc = self.value(raw, it.METAGRAPH["incentive"], n)[0] or ()
        per = self.value(raw, it.METAGRAPH["validator_permit"], n)[0] or ()
        return sorted({u for u, v in enumerate(inc) if v > 0} | {u for u, p in enumerate(per) if p})

    def build_metagraph(self, raw: _Read, n: int) -> MetagraphLite | None:
        inc = tuple(self.value(raw, it.METAGRAPH["incentive"], n)[0] or ())
        per = tuple(self.value(raw, it.METAGRAPH["validator_permit"], n)[0] or ())
        if not inc and not per:
            return None
        cold_of: dict[int, str | None] = {}
        for u in self.metagraph_uids(raw, n):
            hk_b = raw.get(it.METAGRAPH["uid_hotkey"].key(netuid=n, uid=u))
            if hk_b is None:
                cold_of[u] = None
                continue
            ck_b = raw.get(it.METAGRAPH["hotkey_owner"].key(hotkey=d_account(hk_b)))
            cold_of[u] = None if ck_b is None else d_account(ck_b)
        miners = [u for u, v in enumerate(inc) if v > 0]
        share: dict[str, int] = {}
        for u in miners:
            ck = cold_of.get(u) or f"uid:{u}"
            share[ck] = share.get(ck, 0) + inc[u]
        total = sum(inc[u] for u in miners)
        top = max(share.values(), default=0)
        permit_cks = {cold_of.get(u) or f"uid:{u}" for u, p in enumerate(per) if p}
        return MetagraphLite(n_miners=len(miners), n_miner_coldkeys=len(share),
                             top1_coldkey_share_ppm=0 if total == 0 else top * PPM // total,
                             n_permit_coldkeys=len(permit_cks))


def _jumped(before: PoolState, after: PoolState) -> bool:
    """|spot_after / spot_before - 1| > 1%."""
    a, b = before.spot(), after.spot()
    if a == 0:
        return b != 0
    return abs(DEC.divide(b, a) - 1) > Decimal("0.01")
