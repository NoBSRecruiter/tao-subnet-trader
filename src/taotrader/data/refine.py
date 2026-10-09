"""taotrader/data/refine.py - refinement passes over the collected lake (WP4; DESIGN.md sections 7.1, 8.1, 8.6, 8.8,
11 WP4; brief sections 3.9, 4.3-4.4).

1. `spec_boundaries()` - binary search on `state_getRuntimeVersion` for the setCode block of each target spec (the
   block containing setCode already reports the new spec; the new logic runs from the next block, so
   first_logic_block = setcode_block + 1). Targets default to 334/338 (taoflow), 362 (chain buys unrecorded) and 475,
   and the section 8.6 rows 421/432/440/441 are re-verified. The output is the `spec_boundary` lake table plus the ADR
   draft in docs/adr/; protocol/regimes.py is edited by the lead only.
2. Generation and registration tables from the collected snapshots: generations appear / vanish between consecutive
   stored snapshots (presence = NetworksAdded, generation = (netuid, NetworkRegisteredAt)); the exact removal block (the
   NetworksAdded flip) is found by bisection on two storage keys, and the removal-1 snapshot is captured as a REFINED
   snapshot (series "refine") whose state fills the generation's pre_end_* columns (section 8.8). Registrations come
   from LastRateLimitedBlock(0x02) increases (exact block, no bisection): blocks_since_prev = queued_block - previous
   registration block (the brief's Delta-reg), cost_ratio = lock paid / previous NetworkLastLockCost.
3. `verify_prune_log()` - the brief section 4.4 prune log (52 events) checked against the archive with 4 storage keys at
   P-1 and P each (removal flip, netuid, Delta-reg); `compare_prune_log()` checks the built tables against the same log.
4. Refinement windows (section 8.1): per-block snapshots (series "refine", Quality.REFINED) for the 3,600 blocks before
   the last prunes, +-4 h around the 2026-06-22 purge and the 47-subnet re-enable, and caller-given impulse windows.

CLI: python -m taotrader.data.refine spec-boundaries | verify-prune-log | lifecycle | windows (see main()).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from ..chain import items as it
from ..chain.reader import SnapshotDecodeError
from ..core.state import ChainSnapshot
from ..core.units import RAO_PER_TAO, Block, BlockHash, Hotkey, NetUid, SubnetKey
from .collector import (
    DTAO_LAUNCH_BLOCK,
    ArchiveSource,
    CollectorCfg,
    EscrowCache,
    PanelTracker,
    ReaderSource,
    finalize_snapshot,
    rebuild_tracker,
)
from .lake import Lake

log = logging.getLogger(__name__)

REFINE_SERIES: Final[str] = "refine"
REG_RECORD_LAG: Final[int] = 30              # read offset for registrations recorded at their add block
REG_ADD_LAG: Final[tuple[int, int]] = (17, 25)   # queued -> added (brief 4.3); measured 18-25 on mainnet
MAX_QUEUE_LAG_BLOCKS: Final[int] = 600       # a queued registration is added 17-25 blocks later (brief 4.3); generous bound
WINDOW_CHUNK_BLOCKS: Final[int] = 600        # refinement windows are collected and written in absolute 600-block cells

# Section 8.6 setCode blocks re-verified by the search (regime first block = setCode + 1). Informational rows from
# brief 3.9 / 6.5 are verified the same way when they are searched.
EXPECTED_SETCODE: Final[dict[int, int]] = {
    421: 8_466_530, 432: 8_636_190, 440: 8_713_793, 441: 8_765_683,
}
INFORMATIONAL_SETCODE: Final[dict[int, int]] = {423: 8_486_593, 443: 8_772_666, 445: 8_831_003, 450: 8_938_465, 464: 9_088_597}
DEFAULT_TARGETS: Final[tuple[int, ...]] = (334, 338, 362, 421, 432, 440, 441, 475)

PURGE_BLOCK: Final[int] = 8_463_544          # 2026-06-22: 54 subnets emission-disabled in one block
REENABLE_BLOCK: Final[int] = 9_029_889       # 2026-09-09: 47 subnets re-enabled in one block
EVENT_HALF_WIDTH: Final[int] = 1_200         # +-4 h
PRUNE_WINDOW_BLOCKS: Final[int] = 3_600      # the 3,600 blocks before each prune (FT1b)
IMPULSE_HALF_WIDTH: Final[int] = 150         # +-150 blocks around momentum impulses (F14)

# Brief section 4.4 compact prune log: (prune block P, victim netuid, blocks since the previous registration).
BRIEF_PRUNE_LOG: Final[tuple[tuple[int, int, int], ...]] = (
    (6_693_448, 100, 85_220), (6_783_158, 49, 89_710), (6_841_399, 105, 58_241), (6_914_378, 86, 72_979),
    (6_962_737, 94, 48_359), (7_013_758, 92, 51_021), (7_063_126, 90, 49_368), (7_105_263, 108, 42_137),
    (7_119_664, 113, 14_401), (7_151_800, 80, 32_136), (7_173_591, 31, 21_791), (7_208_725, 87, 35_134),
    (7_236_936, 67, 28_211), (7_257_480, 109, 20_544), (7_284_230, 38, 26_750), (7_312_241, 114, 28_011),
    (7_340_355, 47, 28_114), (7_366_897, 15, 26_542), (7_415_113, 99, 48_216), (7_457_580, 107, 42_467),
    (7_525_773, 126, 68_193), (7_574_784, 76, 49_011), (7_633_645, 91, 58_861), (7_692_872, 96, 59_227),
    (7_735_450, 97, 42_578), (7_787_562, 70, 52_112), (7_840_965, 102, 53_403), (7_894_898, 36, 53_933),
    (7_966_145, 78, 71_247), (8_026_517, 82, 60_372), (8_057_320, 57, 30_803), (8_085_297, 84, 27_977),
    (8_123_781, 26, 38_484), (8_138_182, 69, 14_401), (8_238_082, 122, 99_900), (8_294_730, 116, 56_648),
    (8_352_006, 92, 57_276), (8_409_860, 40, 57_854), (8_460_646, 16, 50_786), (8_511_017, 58, 50_371),
    (8_572_056, 99, 61_039), (8_618_670, 90, 46_614), (8_693_261, 86, 74_591), (8_762_355, 103, 69_094),
    (8_825_550, 70, 63_195), (8_884_341, 36, 58_791), (8_938_751, 59, 54_410), (9_003_827, 76, 65_076),
    (9_046_671, 35, 42_844), (9_111_229, 108, 64_558), (9_155_237, 82, 44_008), (9_210_610, 116, 55_373),
)


class RefineError(Exception):
    """A refinement precondition failed (bad bracket, inconsistent archive answers)."""


# ------------------------------------------------------------------------------------------------ spec boundaries
class VersionSource(Protocol):
    async def block_hash(self, block: int) -> BlockHash: ...
    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class SpecBoundary:
    target: int                 # the spec searched for
    spec_version: int           # the spec reported at setcode_block (> target if the target never ran on mainnet)
    setcode_block: int          # first block reporting spec >= target (it contains the setCode extrinsic)
    first_logic_block: int      # setcode_block + 1: the first block executing the new runtime
    prev_spec: int              # spec reported at setcode_block - 1

    def row(self) -> dict[str, Any]:
        return {"spec_version": self.spec_version, "setcode_block": self.setcode_block,
                "first_logic_block": self.first_logic_block}


class SpecSearch:
    """Binary search over a monotone block -> spec_version map, memoising every probe (2 calls each)."""

    def __init__(self, source: VersionSource) -> None:
        self.source = source
        self.cache: dict[int, int] = {}
        self.probes = 0

    async def spec_at(self, block: int) -> int:
        v = self.cache.get(block)
        if v is None:
            h = await self.source.block_hash(block)
            v = (await self.source.spec_version(h))[0]
            self.cache[block] = v
            self.probes += 1
        return v

    async def find(self, target: int, lo: int, hi: int) -> SpecBoundary | None:
        """Minimal block in (lo, hi] whose spec is >= target, or None when spec(hi) < target or spec(lo) >= target
        (the boundary lies outside the range)."""
        if lo >= hi:
            raise ValueError("need lo < hi")
        below = [b for b, v in self.cache.items() if v < target and lo <= b <= hi]
        above = [b for b, v in self.cache.items() if v >= target and lo <= b <= hi]
        a = max(below, default=lo)
        z = min(above, default=hi)
        if await self.spec_at(a) >= target or await self.spec_at(z) < target:
            return None
        while z - a > 1:
            mid = (a + z) // 2
            if await self.spec_at(mid) >= target:
                z = mid
            else:
                a = mid
        return SpecBoundary(target, await self.spec_at(z), z, z + 1, await self.spec_at(a))


async def spec_boundaries(source: VersionSource, targets: Sequence[int] = DEFAULT_TARGETS, *,
                          lo: int = DTAO_LAUNCH_BLOCK, hi: int, search: SpecSearch | None = None) -> list[SpecBoundary]:
    """setCode block of every target spec within (lo, hi] (see SpecSearch.find)."""
    s = search or SpecSearch(source)
    out: list[SpecBoundary] = []
    for t in sorted(set(targets)):
        b = await s.find(t, lo, hi)
        if b is None:
            log.warning("spec %d: no boundary in (%d, %d]", t, lo, hi)
        else:
            out.append(b)
    return out


def verify_boundaries(found: Sequence[SpecBoundary], expected: Mapping[int, int] = EXPECTED_SETCODE) -> list[str]:
    """Problems against the section 8.6 setCode blocks (empty = all reproduced)."""
    got = {b.spec_version: b.setcode_block for b in found}
    out: list[str] = []
    for spec, blk in sorted(expected.items()):
        if spec not in got:
            out.append(f"spec {spec}: not found (expected setCode {blk})")
        elif got[spec] != blk:
            out.append(f"spec {spec}: setCode {got[spec]} != expected {blk}")
    return out


def spec_boundary_rows(found: Sequence[SpecBoundary]) -> list[dict[str, Any]]:
    """One row per spec actually reported at a boundary (a skipped target maps onto the spec that replaced it)."""
    best: dict[int, SpecBoundary] = {}
    for b in found:
        cur = best.get(b.spec_version)
        if cur is None or b.setcode_block < cur.setcode_block:
            best[b.spec_version] = b
    return [best[k].row() for k in sorted(best)]


# ------------------------------------------------------------------------------------------------ dimension tables
def write_dimension(lake: Lake, table: str, rows: Sequence[Mapping[str, Any]], key_col: str) -> str | None:
    """Replace a dimension table (generation, registration, spec_boundary) by one chunk whose series is a content
    hash, so a rebuild with new content never collides with the old chunk and an identical rebuild is a no-op."""
    if not rows:
        return None
    keys = [int(r[key_col]) for r in rows]
    canon = json.dumps([sorted((k, _jsonable(v)) for k, v in r.items()) for r in rows], separators=(",", ":"))
    series = "v" + hashlib.sha256(canon.encode()).hexdigest()[:20]
    old = [c.path for c in lake.manifest(table)]
    info = lake.write_rows(table, rows, first_block=min(keys), last_block=max(keys), series=series,
                           replaces=[p for p in old if not p.endswith(f"-{series}.parquet")])
    return info.path


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def merge_spec_boundaries(lake: Lake, found: Sequence[SpecBoundary]) -> str | None:
    """Merge newly found boundaries into the lake's spec_boundary table (new rows win) and rewrite it."""
    con = lake.connect()
    try:
        old = con.execute("SELECT spec_version, setcode_block, first_logic_block FROM v_spec_boundary").fetchall()
    finally:
        con.close()
    rows = {int(r[0]): {"spec_version": int(r[0]), "setcode_block": int(r[1]), "first_logic_block": int(r[2])} for r in old}
    for r in spec_boundary_rows(found):
        rows[int(r["spec_version"])] = r
    return write_dimension(lake, "spec_boundary", [rows[k] for k in sorted(rows)], "spec_version")


# ------------------------------------------------------------------------------------------------ storage probes
_ADDED = it.SUBNET[it.NETWORKS_ADDED]
_REG_AT = it.SUBNET["reg_at"]
_LAST_REG = it.GLOBAL["last_reg_block"]
_LAST_REG_LEGACY = it.GLOBAL["last_reg_block_legacy"]


class StorageSource(Protocol):
    async def block_hash(self, block: int) -> BlockHash: ...
    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]: ...
    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]: ...


@dataclass(frozen=True, slots=True)
class NetState:
    added: bool
    reg_at: int
    last_reg: int        # max(LastRateLimitedBlock(0x02), NetworkLastRegistered); 0 when absent


def _net_keys(netuid: int) -> list[bytes]:
    return [_ADDED.key(netuid=netuid), _REG_AT.key(netuid=netuid), _LAST_REG.key(), _LAST_REG_LEGACY.key()]


def _net_state(raw: Mapping[bytes, bytes | None], netuid: int) -> NetState:
    a = raw.get(_ADDED.key(netuid=netuid))
    r = raw.get(_REG_AT.key(netuid=netuid))
    lr = raw.get(_LAST_REG.key())
    ll = raw.get(_LAST_REG_LEGACY.key())
    last = max(0 if lr is None else int(_LAST_REG.decode(lr)), 0 if ll is None else int(_LAST_REG_LEGACY.decode(ll)))
    return NetState(added=a is not None and bool(_ADDED.decode(a)), reg_at=0 if r is None else int(_REG_AT.decode(r)),
                    last_reg=last)


async def net_state(source: StorageSource, netuid: int, block: int, block_hash: BlockHash | None = None) -> NetState:
    h = block_hash or await source.block_hash(block)
    return _net_state(await source.query(_net_keys(netuid), h), netuid)


async def _present(source: StorageSource, key: SubnetKey, block: int) -> bool:
    st = await net_state(source, int(key.netuid), block)
    return st.added and st.reg_at == int(key.reg_at)


async def find_removal_block(source: StorageSource, key: SubnetKey, lo: int, hi: int) -> int:
    """First block in (lo, hi] at which the generation is gone (NetworksAdded false, or NetworkRegisteredAt of
    another generation). Precondition: present at lo, absent at hi (checked)."""
    if not await _present(source, key, lo) or await _present(source, key, hi):
        raise RefineError(f"{key}: bad removal bracket ({lo}, {hi}]")
    a, z = lo, hi
    while z - a > 1:
        mid = (a + z) // 2
        if await _present(source, key, mid):
            a = mid
        else:
            z = mid
    return z


async def find_added_block(source: StorageSource, key: SubnetKey, lo: int, hi: int) -> int:
    """First block in (lo, hi] at which the generation is present. Precondition: absent at lo, present at hi."""
    if await _present(source, key, lo) or not await _present(source, key, hi):
        raise RefineError(f"{key}: bad add bracket ({lo}, {hi}]")
    a, z = lo, hi
    while z - a > 1:
        mid = (a + z) // 2
        if await _present(source, key, mid):
            z = mid
        else:
            a = mid
    return z


# ------------------------------------------------------------------------------------------------ prune log
@dataclass(frozen=True, slots=True)
class PruneCheck:
    block: int
    netuid: int
    delta_reg: int                # brief value (P - previous prune block)
    added_before: bool            # NetworksAdded at P-1
    present_after: bool           # the P-1 generation is still there at P
    last_reg_at: int              # LastRateLimitedBlock(0x02) (or the legacy item) read at P
    recorded_late: int | None     # registration recorded at the add block P + k (k), read at P + REG_RECORD_LAG
    delta_obs: int                # P - registration block read at P-1

    @property
    def recorded(self) -> bool:
        """The registration of P is recorded at P, or at its add block (P + 17..25) - see REG_RECORD_LAG."""
        return self.last_reg_at == self.block or self.recorded_late is not None

    @property
    def prev_lag(self) -> int:
        """delta_reg - delta_obs: 0 when the previous registration was recorded at its queue block, its add lag
        (17-25) when it was recorded at its add block."""
        return self.delta_reg - self.delta_obs

    @property
    def ok(self) -> bool:
        return (self.added_before and not self.present_after and self.recorded
                and (self.prev_lag == 0 or REG_ADD_LAG[0] <= self.prev_lag <= REG_ADD_LAG[1]))


async def verify_prune_log(source: StorageSource, log_rows: Sequence[tuple[int, int, int]] = BRIEF_PRUNE_LOG) -> list[PruneCheck]:
    """For every logged prune P: the victim is added at P-1 and removed at P (exact block, logged netuid), the
    registration is recorded at P or at its add block, and Delta-reg = P - previous registration, up to the add lag
    of a previous registration recorded at its add block (specs of 8,693,261-8,938,751 wrote LastRateLimitedBlock
    when the queued registration completed, not at P). 1 hash call + up to 3 storage calls per prune."""
    blocks = sorted({b for p, _n, _d in log_rows for b in (p - 1, p, p + REG_RECORD_LAG)})
    hashes = await source.block_hashes(blocks)
    out: list[PruneCheck] = []
    for p, n, d in log_rows:
        before = await net_state(source, n, p - 1, hashes[p - 1])
        after = await net_state(source, n, p, hashes[p])
        late: int | None = None
        if after.last_reg != p:
            later = await net_state(source, n, p + REG_RECORD_LAG, hashes[p + REG_RECORD_LAG])
            if REG_ADD_LAG[0] <= later.last_reg - p <= REG_ADD_LAG[1]:
                late = later.last_reg - p
        out.append(PruneCheck(block=p, netuid=n, delta_reg=d, added_before=before.added,
                              present_after=after.added and after.reg_at == before.reg_at, last_reg_at=after.last_reg,
                              recorded_late=late, delta_obs=p - before.last_reg))
    return out


# ------------------------------------------------------------------------------------------------ lifecycle scan (pure)
@dataclass(frozen=True, slots=True)
class Obs:
    """What one stored snapshot says about generations and registrations."""
    block: int
    last_reg_block: int
    last_lock_cost: int
    gens: Mapping[int, int]                              # netuid -> reg_at (NetworksAdded netuids only)
    first_emission: Mapping[int, int | None] = field(default_factory=dict)


@dataclass(slots=True)
class GenSpan:
    key: SubnetKey
    first_seen: int
    last_seen: int
    first_absent: int | None = None                      # first stored block after last_seen (the removal bracket end)
    absent_before: int | None = None                     # last stored block before first_seen without it
    first_emission_block: int | None = None


@dataclass(frozen=True, slots=True)
class RegObs:
    recorded_block: int        # LastRateLimitedBlock(0x02) value: the queue block P, or (some specs) the add block
    prev_reg_block: int
    prev_lock_cost: int
    lock_after: int
    seen_at: int


@dataclass(slots=True)
class Lifecycle:
    spans: dict[SubnetKey, GenSpan]
    regs: list[RegObs]
    last_block: int


def scan_lifecycle(obs: Sequence[Obs]) -> Lifecycle:
    """Generation spans and registrations from stored snapshots (blocks ascending, one per block)."""
    spans: dict[SubnetKey, GenSpan] = {}
    regs: list[RegObs] = []
    prev: Obs | None = None
    for o in sorted(obs, key=lambda x: x.block):
        if prev is not None and o.block <= prev.block:
            raise RefineError(f"duplicate observation at {o.block}")
        cur_keys = {SubnetKey(NetUid(n), Block(r)) for n, r in o.gens.items()}
        for k in sorted(cur_keys):
            sp = spans.get(k)
            if sp is None:
                sp = spans[k] = GenSpan(k, o.block, o.block, absent_before=None if prev is None else prev.block)
            sp.last_seen = o.block
            fe = o.first_emission.get(int(k.netuid))
            if fe is not None and sp.first_emission_block is None:
                sp.first_emission_block = fe
        if prev is not None:
            for n, r in prev.gens.items():
                k = SubnetKey(NetUid(n), Block(r))
                if k not in cur_keys and spans[k].first_absent is None:
                    spans[k].first_absent = o.block
            if o.last_reg_block > prev.last_reg_block and o.last_reg_block > 0:
                regs.append(RegObs(o.last_reg_block, prev.last_reg_block, prev.last_lock_cost, o.last_lock_cost, o.block))
        prev = o
    return Lifecycle(spans=spans, regs=regs, last_block=prev.block if prev is not None else 0)


def load_observations(lake: Lake, series: Sequence[str] = ("",)) -> list[Obs]:
    """One Obs per stored snapshot block of the given lake series (default: the collector's base series, so the
    tables do not depend on which refined snapshots exist yet; duplicates of a block carry the same chain state)."""
    lake.refresh()
    keep = {r.block for r in lake.snapshot_refs() if r.series in set(series)}
    con = lake.connect()
    try:
        g = con.execute("SELECT block, any_value(last_reg_block), any_value(last_lock_cost) FROM v_global "
                        "GROUP BY block ORDER BY block").fetchall()
        s = con.execute("SELECT DISTINCT block, netuid, reg_at, first_emission_block FROM v_subnet ORDER BY block, netuid").fetchall()
    finally:
        con.close()
    gens: dict[int, dict[int, int]] = {}
    fe: dict[int, dict[int, int | None]] = {}
    for b, n, r, f in s:
        gens.setdefault(int(b), {})[int(n)] = int(r)
        fe.setdefault(int(b), {})[int(n)] = None if f is None else int(f)
    return [Obs(int(b), int(lr or 0), int(lc or 0), gens.get(int(b), {}), fe.get(int(b), {})) for b, lr, lc in g
            if int(b) in keep]


# ------------------------------------------------------------------------------------------------ table builder
@dataclass(slots=True)
class LifecycleTables:
    generations: list[dict[str, Any]]
    registrations: list[dict[str, Any]]
    refined_snapshots: int = 0
    bisections: int = 0


async def resolve_ends(lc: Lifecycle, source: StorageSource, prior: Mapping[tuple[int, int], int] | None = None
                       ) -> tuple[dict[SubnetKey, int], int]:
    """Exact removal block of every ended generation: a refined end of an earlier build when it lies in the bracket,
    the bracket end when the bracket is one block, else bisection. Returns (end blocks, bisections run)."""
    prior = prior or {}
    ends: dict[SubnetKey, int] = {}
    n = 0
    for key, sp in sorted(lc.spans.items()):
        if sp.first_absent is None:
            continue
        p = prior.get((int(key.netuid), int(key.reg_at)))
        if p is not None and sp.last_seen < p <= sp.first_absent:
            ends[key] = p
        elif sp.first_absent - sp.last_seen == 1:
            ends[key] = sp.first_absent
        else:
            ends[key] = await find_removal_block(source, key, sp.last_seen, sp.first_absent)
            n += 1
    return ends, n


def lifecycle_rows(lc: Lifecycle, end_block: Mapping[SubnetKey, int],
                   pre_end: Mapping[SubnetKey, Mapping[str, int | None]] | None = None,
                   seeds: Mapping[SubnetKey, tuple[int | None, bool | None]] | None = None,
                   added: Mapping[SubnetKey, int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(generation rows, registration rows) of section 7.1. Pure. Registrations come from LastRateLimitedBlock(0x02)
    increases (exact blocks): the victim is the generation removed in the registration block, the new generation the
    one whose reg_at follows within MAX_QUEUE_LAG_BLOCKS (the victim's netuid first)."""
    pre_end = pre_end or {}
    seeds = seeds or {}
    added = added or {}
    removals = sorted((b, k) for k, b in end_block.items())
    used: set[SubnetKey] = set()
    taken: set[SubnetKey] = set()
    reg_rows: list[dict[str, Any]] = []
    reg_of: dict[SubnetKey, dict[str, Any]] = {}
    prev_queued: int | None = None
    for r in lc.regs:
        victim = _victim(removals, r.recorded_block, used)
        queued = r.recorded_block if victim is None else end_block[victim]
        if victim is not None:
            used.add(victim)
        new = _new_generation(lc, queued, victim, taken)
        if new is not None:
            taken.add(new)
        prev = prev_queued if prev_queued is not None else (r.prev_reg_block if r.prev_reg_block > 0 else None)
        row = {"queued_block": queued,
               "victim_netuid": None if victim is None else int(victim.netuid),
               "victim_reg_at": None if victim is None else int(victim.reg_at),
               "new_reg_at": None if new is None else int(new.reg_at),
               "cost_ratio": None if r.prev_lock_cost <= 0 else r.lock_after / r.prev_lock_cost,
               "lock_amount": r.lock_after,
               "blocks_since_prev": None if prev is None else queued - prev,
               "shielded": None}
        reg_rows.append(row)
        prev_queued = queued
        if new is not None:
            reg_of[new] = row
    queued_at = {int(r["queued_block"]) for r in reg_rows}
    gen_rows: list[dict[str, Any]] = []
    for key, sp in sorted(lc.spans.items()):
        add = added.get(key, int(key.reg_at))
        eb = end_block.get(key)
        reg = reg_of.get(key)
        seed_price, seed_anomaly = seeds.get(key, (None, None))
        gen_rows.append({
            "netuid": int(key.netuid), "reg_at": int(key.reg_at),
            "queued_block": None if reg is None else int(reg["queued_block"]),
            "added_block": add if add > 0 else None,
            "start_call_block": None if sp.first_emission_block is None else sp.first_emission_block - 1,
            "first_seen": sp.first_seen, "last_seen": sp.last_seen,
            "end_block": eb, "end_kind": "open" if eb is None else ("pruned" if eb in queued_at else "dissolved"),
            "end_refined": eb is not None and key in pre_end,
            "lock_amount": None if reg is None else int(reg["lock_amount"]),
            "seed_price_rao": seed_price, "seed_anomaly": seed_anomaly,
            "pre_end_tao": None, "pre_end_alpha_in": None, "pre_end_alpha_out": None, "pre_end_protocol": None,
            "pre_end_escrow": None, "pre_end_total_staked": None, "observed_payout_ratio": None,
            **pre_end.get(key, {}),
        })
    return gen_rows, reg_rows


async def build_lifecycle_tables(lake: Lake, source: ArchiveSource, *, cfg: CollectorCfg | None = None,
                                 capture_pre_end: bool = True, seed_prices: bool = True, verify_added: bool = False,
                                 write: bool = True) -> LifecycleTables:
    """Generation and registration tables (section 7.1) from the lake's snapshots, refined against the archive:
    exact removal blocks, the REFINED removal-1 snapshots (pre_end_* columns), seed prices at the add block, and
    optionally a bisection check of the add block against NetworkRegisteredAt."""
    cfg = cfg or CollectorCfg()
    lc = scan_lifecycle(load_observations(lake))
    out = LifecycleTables([], [])
    end_block, out.bisections = await resolve_ends(lc, source, _prior_ends(lake))
    pre_end: dict[SubnetKey, dict[str, int | None]] = {}
    if capture_pre_end:
        tracker = rebuild_tracker(lake, cfg, lc.last_block)
        escrow = EscrowCache(source, cfg.escrow_grid)
        stored_refined = {r.block for r in lake.snapshot_refs() if r.series == REFINE_SERIES}
        for key, eb in sorted(end_block.items()):
            pb = eb - 1
            snap = await _refined_snapshot(lake, source, pb, tracker, escrow, write=write and pb not in stored_refined)
            if snap is None:
                continue
            stored_refined.add(pb)
            out.refined_snapshots += 1
            s = snap.get(key)
            if s is not None:
                pre_end[key] = {"pre_end_tao": int(s.pool.tao), "pre_end_alpha_in": int(s.pool.alpha),
                                "pre_end_alpha_out": int(s.alpha_out), "pre_end_protocol": int(s.protocol_alpha),
                                "pre_end_escrow": None if s.escrow_alpha is None else int(s.escrow_alpha),
                                "pre_end_total_staked": None if s.total_alpha_staked is None else int(s.total_alpha_staked)}
    added: dict[SubnetKey, int] = {}
    seeds: dict[SubnetKey, tuple[int | None, bool | None]] = {}
    locks = {int(r.recorded_block): r.lock_after for r in lc.regs}
    for key, sp in sorted(lc.spans.items()):
        if sp.absent_before is None or int(key.reg_at) <= 0:
            continue                                      # alive at the first stored snapshot: no add bracket
        if verify_added and not (sp.absent_before < int(key.reg_at) <= sp.first_seen):
            added[key] = await find_added_block(source, key, sp.absent_before, sp.first_seen)
            out.bisections += 1
        if seed_prices:
            q = max((b for b in locks if b - REG_RECORD_LAG <= int(key.reg_at) <= b + MAX_QUEUE_LAG_BLOCKS), default=None)
            seeds[key] = await _seed(source, key, None if q is None else locks[q])
    out.generations, out.registrations = lifecycle_rows(lc, end_block, pre_end, seeds, added)
    if write:
        write_dimension(lake, "generation", out.generations, "reg_at")
        write_dimension(lake, "registration", out.registrations, "queued_block")
    return out


def _victim(removals: Sequence[tuple[int, SubnetKey]], recorded: int, used: set[SubnetKey]) -> SubnetKey | None:
    """The generation removed by the registration recorded at `recorded`: removed in that block, or (registrations
    recorded at their add block) up to REG_RECORD_LAG blocks earlier; the latest such removal not yet used."""
    best: SubnetKey | None = None
    for b, k in removals:
        if recorded - REG_RECORD_LAG <= b <= recorded and k not in used:
            best = k
    return best


def _new_generation(lc: Lifecycle, queued: int, victim: SubnetKey | None, taken: set[SubnetKey]) -> SubnetKey | None:
    """The generation a registration queued at `queued` created: reg_at in [queued, queued + MAX_QUEUE_LAG_BLOCKS],
    the victim's netuid first, then the earliest."""
    cands = [k for k in lc.spans if queued <= int(k.reg_at) <= queued + MAX_QUEUE_LAG_BLOCKS and k not in taken]
    if not cands:
        return None
    cands.sort(key=lambda k: (victim is None or k.netuid != victim.netuid, int(k.reg_at), int(k.netuid)))
    return cands[0]


def _prior_ends(lake: Lake) -> dict[tuple[int, int], int]:
    """Refined end blocks of an earlier build (reused: no second bisection)."""
    con = lake.connect()
    try:
        rows = con.execute("SELECT netuid, reg_at, end_block FROM v_generation WHERE end_block IS NOT NULL").fetchall()
    finally:
        con.close()
    return {(int(n), int(r)): int(e) for n, r, e in rows}


async def _seed(source: ArchiveSource, key: SubnetKey, lock: int | None) -> tuple[int | None, bool | None]:
    """Seed price (rao per alpha, w = 0.5) at the add block; anomaly when SubnetTAO is not the full lock."""
    h = await source.block_hash(int(key.reg_at))
    tao_k, alpha_k = it.SUBNET["tao"].key(netuid=int(key.netuid)), it.SUBNET["alpha_in"].key(netuid=int(key.netuid))
    raw = await source.query([tao_k, alpha_k], h)
    t = raw.get(tao_k)
    a = raw.get(alpha_k)
    tao = 0 if t is None else int(it.SUBNET["tao"].decode(t))
    alpha = 0 if a is None else int(it.SUBNET["alpha_in"].decode(a))
    if alpha <= 0:
        return None, True
    return tao * RAO_PER_TAO // alpha, None if lock is None else tao != lock


async def _refined_snapshot(lake: Lake, source: ArchiveSource, block: int, tracker: PanelTracker, escrow: EscrowCache, *,
                            write: bool) -> ChainSnapshot | None:
    """The removal-1 snapshot (FULL, REFINED, tracked set of that block, grid escrow); stored under series refine."""
    h = (await source.block_hashes([block]))[block]
    try:
        snap = await source.snapshot(block, h, None, tracker.tracked_at(block))
    except SnapshotDecodeError as e:
        log.warning("removal-1 snapshot %d undecodable: %s", block, e)
        return None
    await escrow.fill([block])
    src, esc = escrow.at(block)
    final = finalize_snapshot(snap, escrow=esc, refined=True)
    if write:
        eb = {(block, int(s.key.netuid)): src for s in final.subnets} if src is not None and esc is not None else None
        lake.write_snapshots([final], series=REFINE_SERIES, escrow_block=eb)
    return final


# ------------------------------------------------------------------------------------------------ prune-log comparison
@dataclass(frozen=True, slots=True)
class PruneLogMismatch:
    block: int
    netuid: int
    what: str


def compare_prune_log(generations: Sequence[Mapping[str, Any]], registrations: Sequence[Mapping[str, Any]],
                      log_rows: Sequence[tuple[int, int, int]] = BRIEF_PRUNE_LOG) -> list[PruneLogMismatch]:
    """The built tables against the brief section 4.4 log: a 'pruned' generation of the netuid ending at P, a
    registration queued at P with that victim, and blocks_since_prev == Delta-reg. Also flags pruned generations
    absent from the log inside the log's block range."""
    ends = {(int(g["netuid"]), int(g["end_block"])): g for g in generations if g.get("end_block") is not None}
    regs = {int(r["queued_block"]): r for r in registrations}
    out: list[PruneLogMismatch] = []
    for p, n, d in log_rows:
        g = ends.get((n, p))
        if g is None:
            out.append(PruneLogMismatch(p, n, "no generation of this netuid ends at P"))
        elif g.get("end_kind") != "pruned":
            out.append(PruneLogMismatch(p, n, f"end_kind {g.get('end_kind')}"))
        r = regs.get(p)
        if r is None:
            out.append(PruneLogMismatch(p, n, "no registration queued at P"))
            continue
        if r.get("victim_netuid") != n:
            out.append(PruneLogMismatch(p, n, f"victim {r.get('victim_netuid')}"))
        if r.get("blocks_since_prev") != d:
            out.append(PruneLogMismatch(p, n, f"blocks_since_prev {r.get('blocks_since_prev')} != {d}"))
    lo, hi = min(p for p, _n, _d in log_rows), max(p for p, _n, _d in log_rows)
    logged = {(n, p) for p, n, _d in log_rows}
    for g in generations:
        eb = g.get("end_block")
        if g.get("end_kind") == "pruned" and eb is not None and lo <= int(eb) <= hi and (int(g["netuid"]), int(eb)) not in logged:
            out.append(PruneLogMismatch(int(eb), int(g["netuid"]), "pruned generation missing from the log"))
    return out


# ------------------------------------------------------------------------------------------------ refinement windows
def refinement_windows(generations: Sequence[Mapping[str, Any]], *, last_n_prunes: int = 10,
                       before: int = PRUNE_WINDOW_BLOCKS, events: Sequence[int] = (PURGE_BLOCK, REENABLE_BLOCK),
                       event_half_width: int = EVENT_HALF_WIDTH, impulses: Sequence[int] = (),
                       impulse_half_width: int = IMPULSE_HALF_WIDTH) -> list[tuple[int, int]]:
    """Merged inclusive per-block windows (section 8.1)."""
    prunes = sorted(int(g["end_block"]) for g in generations if g.get("end_kind") == "pruned" and g.get("end_block"))
    raw = [(p - before, p - 1) for p in prunes[-last_n_prunes:]] if last_n_prunes > 0 else []
    raw += [(e - event_half_width, e + event_half_width) for e in events]
    raw += [(b - impulse_half_width, b + impulse_half_width) for b in impulses]
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(raw):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


class WindowSource(ArchiveSource, Protocol):
    def pull(self, blocks: Sequence[int],
             tracked: Callable[[ChainSnapshot | None], Sequence[tuple[SubnetKey, Hotkey]]]) -> AsyncIterator[ChainSnapshot]: ...


class ReaderWindowSource(ReaderSource):
    """ReaderSource plus the reader's per-block pull (FULL every 60 blocks, HEAD in between)."""

    def pull(self, blocks: Sequence[int],
             tracked: Callable[[ChainSnapshot | None], Sequence[tuple[SubnetKey, Hotkey]]]) -> AsyncIterator[ChainSnapshot]:
        return self.reader.pull(blocks, tracked)


async def collect_windows(lake: Lake, source: WindowSource, windows: Sequence[tuple[int, int]], *,
                          cfg: CollectorCfg | None = None) -> int:
    """Per-block REFINED snapshots of every window, collected and written per absolute 600-block cell (the first
    block of a cell is a FULL read without prev, so a cell's content does not depend on what was collected before).
    Cells fully stored already are skipped. Returns the number of snapshots written."""
    cfg = cfg or CollectorCfg()
    tracker = rebuild_tracker(lake, cfg, max((hi for _lo, hi in windows), default=0))
    escrow = EscrowCache(source, cfg.escrow_grid)
    have = {r.block for r in lake.snapshot_refs() if r.series == REFINE_SERIES}
    written = 0
    for lo, hi in windows:
        cell = lo // WINDOW_CHUNK_BLOCKS
        while cell * WINDOW_CHUNK_BLOCKS <= hi:
            a = max(lo, cell * WINDOW_CHUNK_BLOCKS)
            z = min(hi, (cell + 1) * WINDOW_CHUNK_BLOCKS - 1)
            cell += 1
            blocks = list(range(a, z + 1))
            if all(b in have for b in blocks):
                continue
            first = a

            def tracked(prev: ChainSnapshot | None, first: int = first) -> Sequence[tuple[SubnetKey, Hotkey]]:
                return tracker.tracked_at(first if prev is None else int(prev.block) + 1)   # consecutive blocks

            snaps: list[ChainSnapshot] = [s async for s in source.pull(blocks, tracked)]
            await escrow.fill(int(s.block) for s in snaps)
            finals: list[ChainSnapshot] = []
            eb: dict[tuple[int, int], int] = {}
            for s in snaps:
                src, esc = escrow.at(int(s.block))
                if src is not None and esc is not None:
                    eb.update({(int(s.block), int(x.key.netuid)): src for x in s.subnets})
                finals.append(finalize_snapshot(s, escrow=esc, refined=True))
            lake.write_snapshots(finals, series=REFINE_SERIES, escrow_block=eb)
            have.update(int(s.block) for s in finals)
            written += len(finals)
    return written


# ------------------------------------------------------------------------------------------------ range-call probe (Q9)
@dataclass(frozen=True, slots=True)
class RangeProbe:
    keys: int
    span_blocks: int
    ok: bool
    change_sets: int
    seconds: float
    error: str


async def probe_range_calls(source: ReaderSource, start: int, spans: Sequence[int] = (10, 100, 1_000),
                            key_counts: Sequence[int] = (10, 80), clock: Callable[[], float] | None = None) -> list[RangeProbe]:
    """Section 13 Q9: does the archive serve `state_queryStorage(keys, from, to)` change sets for these spans and key
    counts, and how long does each call take? Keys: per-subnet hot fields (SubnetTAO, SubnetAlphaIn,
    SubnetMovingPrice, NetworksAdded) of netuids 1..n. One call per (keys, span); failures are reported, not raised."""
    import time

    from ..chain.hashing import to_hex
    from ..chain.rpc import RpcError

    tick = clock or time.monotonic
    rows = (it.SUBNET["tao"], it.SUBNET["alpha_in"], it.SUBNET["moving_price"], it.SUBNET[it.NETWORKS_ADDED])
    out: list[RangeProbe] = []
    for nk in key_counts:
        keys = [to_hex(r.key(netuid=n)) for n in range(1, it.DEFAULT_MAX_NETUID + 1) for r in rows][:nk]
        for span in spans:
            hs = await source.block_hashes([start, start + span])
            t0 = tick()
            try:
                res = await source.reader.pool.call("state_queryStorage", [keys, hs[start], hs[start + span]], source.reader.role)
                n = len(res) if isinstance(res, list) else 0
                out.append(RangeProbe(nk, span, isinstance(res, list), n, tick() - t0, ""))
            except RpcError as e:
                out.append(RangeProbe(nk, span, False, 0, tick() - t0, f"{type(e).__name__}: {str(e)[:160]}"))
    return out


# ------------------------------------------------------------------------------------------------ CLI
def _reader_source(window: bool = False) -> ReaderSource:
    from ..chain.reader import JsonRpcChainReader
    from ..chain.rpc import RpcPool
    from ..ops.config_load import load_run_config
    from ..ops.secrets import get_secret

    cfg = load_run_config()
    secret = get_secret("onfinality")
    pool = RpcPool.from_cfg(cfg.rpc, keyed_archive_url=None if secret is None else secret.reveal())
    reader = JsonRpcChainReader(pool, keys_per_call=cfg.rpc.keys_per_call, max_concurrency=cfg.rpc.max_concurrency,
                                provider_check_every=None)
    return ReaderWindowSource(reader) if window else ReaderSource(reader)


async def _cli(a: argparse.Namespace) -> int:
    src = _reader_source(window=a.cmd == "windows")
    try:
        if a.cmd == "spec-boundaries":
            hi = a.hi
            if hi is None:
                head, _ = await src.reader.finalized_head()
                hi = int(head)
            search = SpecSearch(src)
            found = await spec_boundaries(src, a.targets or DEFAULT_TARGETS, lo=a.lo, hi=hi, search=search)
            problems = verify_boundaries(found)
            info = verify_boundaries(found, {k: v for k, v in INFORMATIONAL_SETCODE.items()
                                             if k in {b.spec_version for b in found}})
            print(json.dumps({"boundaries": [{"target": b.target, "spec_version": b.spec_version,
                                              "setcode_block": b.setcode_block, "first_logic_block": b.first_logic_block,
                                              "prev_spec": b.prev_spec} for b in found],
                              "section_8_6_problems": problems, "informational_problems": info,
                              "probes": search.probes}, indent=1))
            if a.write:
                with Lake(a.lake) as lake:
                    print("spec_boundary ->", merge_spec_boundaries(lake, found))
            return 1 if problems else 0
        if a.cmd == "range-probe":
            res = await probe_range_calls(src, a.start)
            print(json.dumps([{"keys": r.keys, "span": r.span_blocks, "ok": r.ok, "change_sets": r.change_sets,
                               "seconds": round(r.seconds, 3), "error": r.error} for r in res], indent=1))
            return 0
        if a.cmd == "verify-prune-log":
            checks = await verify_prune_log(src)
            bad = [c for c in checks if not c.ok]
            for c in bad:
                print("MISMATCH", c)
            print(f"{len(checks) - len(bad)}/{len(checks)} prunes reproduce block, netuid and Delta-reg")
            return 1 if bad else 0
        with Lake(a.lake) as lake:
            ccfg = CollectorCfg(panel_from=a.panel_from)
            if a.cmd == "lifecycle":
                t = await build_lifecycle_tables(lake, src, cfg=ccfg, capture_pre_end=not a.no_capture,
                                                 seed_prices=not a.no_seed)
                mism = compare_prune_log(t.generations, t.registrations)
                print(json.dumps({"generations": len(t.generations), "registrations": len(t.registrations),
                                  "refined_snapshots": t.refined_snapshots, "bisections": t.bisections,
                                  "prune_log_mismatches": [m.__repr__() for m in mism[:60]]}, indent=1))
                return 1 if mism else 0
            con = lake.connect()
            try:
                cols = [d[0] for d in con.execute("SELECT * FROM v_generation LIMIT 0").description or []]
                gens = [dict(zip(cols, r, strict=True)) for r in con.execute("SELECT * FROM v_generation").fetchall()]
            finally:
                con.close()
            wins = refinement_windows(gens, last_n_prunes=a.last_n)
            n = await collect_windows(lake, src, wins, cfg=ccfg)   # type: ignore[arg-type]
            print(json.dumps({"windows": wins, "snapshots_written": n}, indent=1))
            return 0
    finally:
        await src.reader.pool.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    from ..ops.secrets import RedactingFilter
    from .collector import PANEL_FROM_BLOCK

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())
    ap = argparse.ArgumentParser(prog="python -m taotrader.data.refine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sb = sub.add_parser("spec-boundaries")
    sb.add_argument("--targets", type=lambda s: [int(x) for x in s.split(",")], default=None)
    sb.add_argument("--lo", type=int, default=DTAO_LAUNCH_BLOCK)
    sb.add_argument("--hi", type=int, default=None)
    sb.add_argument("--write", action="store_true", help="merge into the lake's spec_boundary table")
    sb.add_argument("--lake", default="data/lake")
    sub.add_parser("verify-prune-log")
    rp = sub.add_parser("range-probe", help="section 13 Q9: state_queryStorage range-call limits")
    rp.add_argument("--start", type=int, default=9_200_000)
    for name in ("lifecycle", "windows"):
        sp = sub.add_parser(name)
        sp.add_argument("--lake", default="data/lake")
        sp.add_argument("--panel-from", type=int, default=PANEL_FROM_BLOCK)
        if name == "lifecycle":
            sp.add_argument("--no-capture", action="store_true", help="skip the removal-1 snapshots")
            sp.add_argument("--no-seed", action="store_true", help="skip the seed-price reads")
        else:
            sp.add_argument("--last-n", type=int, default=10)
    return asyncio.run(_cli(ap.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
