"""WP4 refinement tests (DESIGN.md section 11 WP4, brief 4.4): spec-boundary search (reproduces 8,466,530/531 for
spec 421 offline; the network test runs it on the archive), removal-block bisection, the 52-prune generation and
registration tables, prune-log verification, removal-1 REFINED capture and refinement windows.

The fakes model a chain from a small timeline (test modules cannot share helpers in importlib mode)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.core.protocols import SwapSim
from taotrader.core.state import ChainSnapshot, Quality, SubnetState
from taotrader.core.units import AlphaRao, Block, BlockHash, Hotkey, NetUid, SubnetKey
from taotrader.data import refine as rf
from taotrader.data.collector import CollectorCfg
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore

pytestmark: list[Any] = []


def h(b: int) -> BlockHash:
    return BlockHash("0x" + f"{b:064x}")


def num(bh: str) -> int:
    return int(bh, 16)


def le(v: int, n: int) -> bytes:
    return v.to_bytes(n, "little")


# ------------------------------------------------------------------------------------------------ chain model
class Chain:
    """Generations per netuid as segments [(from_block, reg_at | None)], registrations (block, lock after)."""

    def __init__(self) -> None:
        self.segments: dict[int, list[tuple[int, int | None]]] = {}
        self.regs: list[tuple[int, int]] = []          # (registration block, NetworkLastLockCost after it)
        self.queries = 0
        self.legacy_until = 0                          # blocks < this report the last registration via the legacy item

    def gen(self, netuid: int, block: int) -> int | None:
        cur: int | None = None
        for start, reg in self.segments.get(netuid, []):
            if start <= block:
                cur = reg
        return cur

    def gens(self, block: int) -> dict[int, int]:
        return {n: g for n in sorted(self.segments) if (g := self.gen(n, block)) is not None}

    def last_reg(self, block: int) -> tuple[int, int]:
        prev = [(b, lock) for b, lock in self.regs if b <= block]
        return max(prev) if prev else (0, 0)

    def storage(self, key: bytes, block: int) -> bytes | None:
        for n in self.segments:
            if key == it.SUBNET[it.NETWORKS_ADDED].key(netuid=n):
                return b"\x01" if self.gen(n, block) is not None else None
            if key == it.SUBNET["reg_at"].key(netuid=n):
                g = self.gen(n, block)
                return None if g is None else le(g, 8)
            if key == it.SUBNET["tao"].key(netuid=n):
                g = self.gen(n, block)
                return None if g is None else le(500 * 10**9, 8)
            if key == it.SUBNET["alpha_in"].key(netuid=n):
                g = self.gen(n, block)
                return None if g is None else le(250_000 * 10**9, 8)
        last = self.last_reg(block)[0]
        if key == it.GLOBAL["last_reg_block"].key():
            return None if last == 0 or block < self.legacy_until else le(last, 8)
        if key == it.GLOBAL["last_reg_block_legacy"].key():
            return le(last, 8) if last and block < self.legacy_until else None
        return None


class StorageFake:
    def __init__(self, chain: Chain) -> None:
        self.chain = chain

    async def block_hash(self, block: int) -> BlockHash:
        return h(block)

    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]:
        return {b: h(b) for b in blocks}

    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]:
        self.chain.queries += 1
        return {k: self.chain.storage(k, num(block_hash)) for k in keys}


LATE_WINDOW = (8_693_261, 8_938_751)     # measured: these registrations were recorded at their add block


def prune_chain(log_rows: Sequence[tuple[int, int, int]] = rf.BRIEF_PRUNE_LOG, lag: int = 20,
                late: tuple[int, int] = LATE_WINDOW) -> Chain:
    """A chain consistent with the brief's prune log: each victim generation is removed at P and the netuid is
    re-registered at P + lag; the registration before the first prune adds netuid 127. Registrations with P inside
    `late` write LastRateLimitedBlock at the add block P + lag (as mainnet did for 8,693,261-8,938,751)."""
    c = Chain()
    first_p, _n, first_d = log_rows[0]
    c.regs.append((first_p - first_d, 400 * 10**9))
    c.segments[127] = [(first_p - first_d + lag, first_p - first_d + lag)]
    for i, (p, n, _d) in enumerate(log_rows):
        seg = c.segments.setdefault(n, [(0, 1_000_000 + n)])
        seg.append((p, None))
        seg.append((p + lag, p + lag))
        c.regs.append((p + lag if late[0] <= p <= late[1] else p, (500 + i) * 10**9))
    return c


# ------------------------------------------------------------------------------------------------ spec search
class Versions:
    def __init__(self, setcodes: Mapping[int, int]) -> None:
        self.setcodes = sorted((b, s) for s, b in setcodes.items())
        self.calls = 0

    def spec(self, block: int) -> int:
        cur = self.setcodes[0][1]
        for b, s in self.setcodes:
            if b <= block:
                cur = s
        return cur

    async def block_hash(self, block: int) -> BlockHash:
        self.calls += 1
        return h(block)

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        self.calls += 1
        return self.spec(num(block_hash)), 1


SETCODES = {233: 4_920_351, 326: 6_900_000, 338: 7_000_123, 361: 7_250_000, 362: 7_260_511, 420: 8_400_000,
            421: 8_466_530, 423: 8_486_593, 432: 8_636_190, 440: 8_713_793, 441: 8_765_683, 445: 8_831_003,
            473: 9_200_000, 475: 9_225_777}


def test_spec_search_reproduces_421_and_the_section_8_6_rows() -> None:
    v = Versions(SETCODES)
    search = rf.SpecSearch(v)
    found = asyncio.run(rf.spec_boundaries(v, hi=9_240_388, search=search))
    by_target = {b.target: b for b in found}
    b421 = by_target[421]
    assert (b421.setcode_block, b421.first_logic_block, b421.spec_version, b421.prev_spec) == (8_466_530, 8_466_531, 421, 420)
    assert rf.verify_boundaries(found) == []
    assert by_target[334].spec_version == 338 and by_target[334].setcode_block == 7_000_123   # 334 never ran: 338 replaced it
    assert by_target[362].setcode_block == 7_260_511 and by_target[475].setcode_block == 9_225_777
    rows = rf.spec_boundary_rows(found)
    assert [r["spec_version"] for r in rows] == [338, 362, 421, 432, 440, 441, 475]
    assert all(r["first_logic_block"] == r["setcode_block"] + 1 for r in rows)
    # memoised: ~log2(4.3M) probes per target, fewer as the cache narrows later searches
    assert search.probes <= 8 * 24 and v.calls == 2 * search.probes


def test_spec_search_out_of_range_and_mismatch_report() -> None:
    v = Versions(SETCODES)
    s = rf.SpecSearch(v)
    assert asyncio.run(s.find(475, 4_920_351, 9_000_000)) is None          # not reached by hi
    assert asyncio.run(s.find(233, 4_920_351, 9_000_000)) is None          # already in force at lo
    with pytest.raises(ValueError):
        asyncio.run(s.find(421, 10, 10))
    wrong = [rf.SpecBoundary(421, 421, 8_466_529, 8_466_530, 420)]
    probs = rf.verify_boundaries(wrong)
    assert any("8466529 != expected 8466530" in p for p in probs) and any("spec 432: not found" in p for p in probs)


def test_spec_boundary_table_merge_is_idempotent(tmp_path: Any) -> None:
    v = Versions(SETCODES)
    found = asyncio.run(rf.spec_boundaries(v, (421, 432), hi=9_240_388))
    with Lake(tmp_path / "lake") as lake:
        p1 = rf.merge_spec_boundaries(lake, found)
        assert rf.merge_spec_boundaries(lake, found) == p1                   # identical rebuild: no-op
        more = asyncio.run(rf.spec_boundaries(v, (475,), hi=9_240_388))
        p2 = rf.merge_spec_boundaries(lake, more)
        assert p2 != p1 and [c.path for c in lake.manifest("spec_boundary")] == [p2]
        con = lake.connect()
        try:
            rows = con.execute("SELECT spec_version, setcode_block, first_logic_block FROM v_spec_boundary "
                               "ORDER BY spec_version").fetchall()
        finally:
            con.close()
    assert rows == [(421, 8_466_530, 8_466_531), (432, 8_636_190, 8_636_191), (475, 9_225_777, 9_225_778)]


# ------------------------------------------------------------------------------------------------ bisection, prune log
def test_removal_and_add_bisection() -> None:
    c = prune_chain()
    src = StorageFake(c)
    key = SubnetKey(NetUid(116), Block(1_000_116))
    p = asyncio.run(rf.find_removal_block(src, key, 8_294_000, 8_295_000))
    assert p == 8_294_730
    assert c.queries <= 2 * 11
    new = SubnetKey(NetUid(116), Block(8_294_750))
    assert asyncio.run(rf.find_added_block(src, new, 8_294_000, 8_295_000)) == 8_294_750
    with pytest.raises(rf.RefineError):
        asyncio.run(rf.find_removal_block(src, key, 8_295_000, 8_296_000))      # not present at lo


def test_verify_prune_log_all_52_and_a_corrupted_row() -> None:
    c = prune_chain()
    c.legacy_until = 7_000_000                         # early prunes read the legacy NetworkLastRegistered item
    checks = asyncio.run(rf.verify_prune_log(StorageFake(c)))
    assert len(checks) == 52 and all(x.ok for x in checks)
    late = [x for x in checks if x.recorded_late is not None]
    assert [x.block for x in late] == [p for p, _n, _d in rf.BRIEF_PRUNE_LOG if LATE_WINDOW[0] <= p <= LATE_WINDOW[1]]
    assert all(x.recorded_late == 20 for x in late)
    assert [x.prev_lag for x in checks if x.prev_lag] == [20] * 5                 # the rows after a late recording
    bad = list(rf.BRIEF_PRUNE_LOG)
    bad[10] = (bad[10][0], bad[10][1], bad[10][2] + 1)                        # wrong Delta-reg
    bad[20] = (bad[20][0], 5, bad[20][2])                                      # wrong netuid
    checks = asyncio.run(rf.verify_prune_log(StorageFake(c), bad))
    assert [x.block for x in checks if not x.ok] == [bad[10][0], bad[20][0]]


def test_52_prune_generation_table_matches_the_brief() -> None:
    """Section 11 WP4 acceptance, offline: snapshots every 3,000 blocks over the whole prune era, removal blocks by
    bisection, registrations from LastRateLimitedBlock: blocks, netuids and Delta-reg all reproduce the brief."""
    c = prune_chain()
    obs = [rf.Obs(b, c.last_reg(b)[0], c.last_reg(b)[1], c.gens(b)) for b in range(6_600_000, 9_225_000, 3_000)]
    lc = rf.scan_lifecycle(obs)
    ends, n_bis = asyncio.run(rf.resolve_ends(lc, StorageFake(c)))
    assert n_bis == 52 and sorted(ends.values()) == [p for p, _n, _d in rf.BRIEF_PRUNE_LOG]
    gens, regs = rf.lifecycle_rows(lc, ends)
    assert rf.compare_prune_log(gens, regs) == []
    pruned = [g for g in gens if g["end_kind"] == "pruned"]
    assert len(pruned) == 52
    late = next(r for r in regs if r["queued_block"] == 8_762_355)               # recorded at 8,762,375, anchored at P
    assert late["victim_netuid"] == 103 and late["blocks_since_prev"] == 69_094
    reg116 = next(r for r in regs if r["queued_block"] == 9_210_610)
    assert reg116["victim_netuid"] == 116 and reg116["new_reg_at"] == 9_210_630 and reg116["blocks_since_prev"] == 55_373
    assert reg116["cost_ratio"] == pytest.approx(551 / 550)
    g = next(x for x in gens if x["netuid"] == 116 and x["reg_at"] == 9_210_630)
    assert g["queued_block"] == 9_210_610 and g["end_kind"] == "open" and g["lock_amount"] == 551 * 10**9
    # a corrupted table is caught
    regs2 = [dict(r, blocks_since_prev=1) if r["queued_block"] == 9_155_237 else r for r in regs]
    gens2 = [dict(x, end_kind="dissolved") if x.get("end_block") == 9_046_671 else x for x in gens]
    mism = rf.compare_prune_log(gens2, regs2)
    assert {(m.block, m.what.split()[0]) for m in mism} == {(9_155_237, "blocks_since_prev"), (9_046_671, "end_kind")}
    # reuse of earlier refined ends: no second bisection
    _ends2, n2 = asyncio.run(rf.resolve_ends(lc, StorageFake(c), {(int(k.netuid), int(k.reg_at)): v for k, v in ends.items()}))
    assert n2 == 0


def test_scan_lifecycle_rejects_duplicate_blocks() -> None:
    with pytest.raises(rf.RefineError):
        rf.scan_lifecycle([rf.Obs(10, 0, 0, {}), rf.Obs(10, 0, 0, {})])


def test_refinement_windows_merge() -> None:
    gens = [{"end_kind": "pruned", "end_block": p} for p, _n, _d in rf.BRIEF_PRUNE_LOG] + [{"end_kind": "open", "end_block": None}]
    w = rf.refinement_windows(gens, last_n_prunes=2, impulses=(9_210_000,))
    assert w == [(8_462_344, 8_464_744), (9_028_689, 9_031_089), (9_151_637, 9_155_236), (9_207_010, 9_210_609)]
    assert rf.refinement_windows([], events=()) == []


# ------------------------------------------------------------------------------------------------ lake integration
class Archive(StorageFake):
    """ArchiveSource + pull over the Chain model (snapshots built with the conftest factories)."""

    def __init__(self, chain: Chain, make_subnet: Callable[..., SubnetState],
                 make_snapshot: Callable[..., ChainSnapshot]) -> None:
        super().__init__(chain)
        self.make_subnet = make_subnet
        self.make_snapshot = make_snapshot
        self.snapshots: list[int] = []

    def snap(self, block: int) -> ChainSnapshot:
        subs = [self.make_subnet(n, reg_at=r, hotkeys=()) for n, r in self.chain.gens(block).items()]
        last, lock = self.chain.last_reg(block)
        return self.make_snapshot(block, subs, last_reg_block=last, last_lock_cost=lock)

    async def prime_versions(self, blocks: Sequence[int], hashes: Mapping[int, BlockHash]) -> None:
        return None

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        return 475, 1

    async def snapshot(self, block: int, block_hash: BlockHash, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot:
        self.snapshots.append(block)
        return self.snap(block)

    async def generations(self, block_hash: BlockHash) -> dict[int, int]:
        return self.chain.gens(num(block_hash))

    async def dividend_keys_all(self, block_hash: BlockHash) -> dict[int, tuple[Hotkey, ...]]:
        return {}

    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]:
        return {n: AlphaRao(77) for n in self.chain.gens(num(block_hash))}

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        return {}

    async def current_price(self, netuid: NetUid, block_hash: BlockHash) -> int:
        return 0

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        return SwapSim(0, 0, 0, 0, 0, 0)

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        return SwapSim(0, 0, 0, 0, 0, 0)

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        return None

    def providers(self) -> dict[str, int]:
        return {"fake": self.chain.queries}

    async def _pull(self, blocks: Sequence[int]) -> AsyncIterator[ChainSnapshot]:
        for b in blocks:
            self.snapshots.append(b)
            yield self.snap(b)

    def pull(self, blocks: Sequence[int],
             tracked: Callable[[ChainSnapshot | None], Sequence[tuple[SubnetKey, Hotkey]]]) -> AsyncIterator[ChainSnapshot]:
        assert tracked(None) == ()
        return self._pull(blocks)


def test_build_lifecycle_tables_captures_removal_minus_one(make_subnet: Callable[..., SubnetState],
                                                          make_snapshot: Callable[..., ChainSnapshot], tmp_path: Any) -> None:
    log_rows = [(9_155_237, 82, 44_008), (9_210_610, 116, 55_373)]
    c = prune_chain(log_rows)
    src = Archive(c, make_subnet, make_snapshot)
    cfg = CollectorCfg(panel_from=9_150_000)
    with Lake(tmp_path / "lake") as lake:
        snaps = [src.snap(b) for b in range(9_150_000, 9_216_000, 1_200)]
        lake.write_snapshots(snaps)
        t = asyncio.run(rf.build_lifecycle_tables(lake, src, cfg=cfg))
        assert t.bisections == 2 and t.refined_snapshots == 2
        assert rf.compare_prune_log(t.generations, t.registrations, log_rows) == []
        ended = {g["netuid"]: g for g in t.generations if g["end_block"] is not None}
        assert ended[116]["end_refined"] and ended[116]["pre_end_tao"] == 587_200_000_000 and ended[116]["pre_end_escrow"] == 77
        new116 = next(g for g in t.generations if g["netuid"] == 116 and g["reg_at"] == 9_210_630)
        assert new116["seed_price_rao"] == 500 * 10**9 * 10**9 // (250_000 * 10**9) and new116["seed_anomaly"] is True
        refined = [r for r in lake.snapshot_refs() if r.series == rf.REFINE_SERIES]
        assert [r.block for r in refined] == [9_155_236, 9_210_609] and all(r.quality_or & Quality.REFINED for r in refined)
        s = LakeSnapshotStore(lake, clock=9_300_000).at(Block(9_210_609))
        assert s.by_netuid(116) is not None and s.subnets[0].quality & Quality.REFINED
        before = [(x.path, x.sha256) for x in lake.manifest()]
        n_snap = len(src.snapshots)
        t2 = asyncio.run(rf.build_lifecycle_tables(lake, src, cfg=cfg))         # rebuild: refined ends reused
        assert t2.bisections == 0 and t2.generations == t.generations
        assert [(x.path, x.sha256) for x in lake.manifest()] == before
        assert len(src.snapshots) == n_snap + 2                                  # re-read but not rewritten
        con = lake.connect()
        try:
            n_gen = con.execute("SELECT count(*) FROM v_generation").fetchone()
            n_reg = con.execute("SELECT count(*), max(blocks_since_prev) FROM v_registration").fetchone()
        finally:
            con.close()
        assert n_gen is not None and n_gen[0] == len(t.generations)
        assert n_reg is not None and n_reg[0] == len(t.registrations) and n_reg[1] == 55_373


def test_collect_windows_writes_refined_cells_once(make_subnet: Callable[..., SubnetState],
                                                    make_snapshot: Callable[..., ChainSnapshot], tmp_path: Any) -> None:
    c = prune_chain([(9_210_610, 116, 55_373)])
    src = Archive(c, make_subnet, make_snapshot)
    with Lake(tmp_path / "lake") as lake:
        n = asyncio.run(rf.collect_windows(lake, src, [(9_210_590, 9_210_609)], cfg=CollectorCfg(panel_from=9_300_000)))
        assert n == 20
        refs = [r for r in lake.snapshot_refs() if r.series == rf.REFINE_SERIES]
        assert [r.block for r in refs] == list(range(9_210_590, 9_210_610)) and all(r.quality_or & Quality.REFINED for r in refs)
        assert asyncio.run(rf.collect_windows(lake, src, [(9_210_590, 9_210_609)])) == 0
        # replay merges refined blocks unthinned
        store = LakeSnapshotStore(lake, clock=9_300_000, stride=60)
        assert store.selected_blocks(9_210_590, 9_210_609) == list(range(9_210_590, 9_210_610))
        s = store.at(Block(9_210_600))
        assert s.subnets[0].escrow_alpha == 77


def test_write_dimension_content_hash_series(tmp_path: Any) -> None:
    rows = [{"queued_block": 5, "victim_netuid": 1, "victim_reg_at": 2, "new_reg_at": 6, "cost_ratio": 1.5,
             "lock_amount": 10, "blocks_since_prev": 3, "shielded": None}]
    with Lake(tmp_path / "lake") as lake:
        p1 = rf.write_dimension(lake, "registration", rows, "queued_block")
        assert p1 is not None and rf.write_dimension(lake, "registration", rows, "queued_block") == p1
        p2 = rf.write_dimension(lake, "registration", [replace_row(rows[0], cost_ratio=1.25)], "queued_block")
        assert p2 != p1 and [x.path for x in lake.manifest("registration")] == [p2]
        assert rf.write_dimension(lake, "registration", [], "queued_block") is None


def replace_row(r: Mapping[str, Any], **kw: Any) -> dict[str, Any]:
    return {**r, **kw}


def test_snapshot_quality_untouched_by_lifecycle_scan(make_subnet: Callable[..., SubnetState],
                                                       make_snapshot: Callable[..., ChainSnapshot]) -> None:
    s = make_snapshot(9_000_000, [replace(make_subnet(1), quality=Quality.OK)])
    assert rf.Obs(9_000_000, 0, 0, {1: int(s.subnets[0].key.reg_at)}).gens == {1: 8_000_000}


# ------------------------------------------------------------------------------------------------ network
ARCHIVE = "https://bittensor-finney.api.onfinality.io/public"


def _net_source() -> Any:
    from taotrader.chain.reader import JsonRpcChainReader
    from taotrader.chain.rpc import RpcPool
    from taotrader.data.collector import ReaderSource

    pool = RpcPool.from_urls(archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
    return ReaderSource(JsonRpcChainReader(pool, keys_per_call=2_500, provider_check_every=None))


@pytest.mark.network
def test_network_spec_boundary_search_reproduces_421() -> None:
    """Section 11 WP4 acceptance: 8,466,530 (setCode, reports 421) / 8,466,531 (first block of the new logic); the
    other section 8.6 rows are re-verified in the same search."""
    async def go() -> list[rf.SpecBoundary]:
        src = _net_source()
        try:
            return await rf.spec_boundaries(src, (421, 432, 440, 441), lo=8_400_000, hi=8_800_000)
        finally:
            await src.reader.pool.aclose()

    found = asyncio.run(go())
    print(found)
    b = {x.target: x for x in found}
    assert (b[421].setcode_block, b[421].first_logic_block, b[421].spec_version) == (8_466_530, 8_466_531, 421)
    assert rf.verify_boundaries(found) == []


@pytest.mark.network
def test_network_brief_prune_log_reproduces() -> None:
    async def go() -> list[rf.PruneCheck]:
        src = _net_source()
        try:
            return await rf.verify_prune_log(src)
        finally:
            await src.reader.pool.aclose()

    checks = asyncio.run(go())
    bad = [c for c in checks if not c.ok]
    print(f"{len(checks) - len(bad)}/{len(checks)} reproduce;", bad)
    assert len(checks) == 52 and bad == []


@pytest.mark.network
def test_network_lifecycle_around_the_sn116_prune(tmp_path: Any) -> None:
    """Collector snapshots every 1,200 blocks around the 9,210,610 prune, then the lifecycle builder: the removal
    block by bisection, the REFINED removal-1 snapshot, the registration row (victim 116, Delta-reg 55,373) and the
    new generation."""
    from taotrader.data import collector as col

    async def go() -> rf.LifecycleTables:
        src = _net_source()
        lake = Lake(tmp_path / "lake")
        cfg = col.CollectorCfg(panel_from=9_300_000, price_every_blocks=0, amm_every_blocks=0, prune_every_blocks=0)
        c = col.Collector(src, lake, cfg)
        try:
            await c.run([col.ScheduleSpec("c60", 9_208_800, 9_212_400, (1_200,))])
            return await rf.build_lifecycle_tables(lake, src, cfg=cfg)
        finally:
            c.close()
            lake.close()
            await src.reader.pool.aclose()

    t = asyncio.run(go())
    ended = [g for g in t.generations if g["end_block"] is not None]
    print("ended:", ended)
    print("registrations:", t.registrations)
    assert [(g["netuid"], g["end_block"], g["end_kind"], g["end_refined"]) for g in ended] == [(116, 9_210_610, "pruned", True)]
    reg = [r for r in t.registrations if r["queued_block"] == 9_210_610]
    assert reg and reg[0]["victim_netuid"] == 116 and reg[0]["blocks_since_prev"] == 55_373
    new = [g for g in t.generations if g["netuid"] == 116 and g["reg_at"] == reg[0]["new_reg_at"]]
    assert new and 9_210_610 < new[0]["reg_at"] <= 9_210_610 + rf.MAX_QUEUE_LAG_BLOCKS and new[0]["seed_price_rao"]
    assert ended[0]["pre_end_tao"] and ended[0]["pre_end_alpha_out"]
