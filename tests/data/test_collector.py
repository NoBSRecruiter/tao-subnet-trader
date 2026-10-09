"""WP4 collector tests (DESIGN.md section 11 WP4): resumable coarse-to-fine schedules, fetch_ledger-driven resume,
kill-and-resume manifest identity, the point-in-time sticky hotkey panel (no gaps), escrow on its grid, calibration
probes and drift abort. Offline: an in-memory FakeArchive implements data.collector.ArchiveSource.

Test modules cannot import each other (importlib mode), so the fake lives here (test_refine.py has its own)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain.hashing import account, to_hex
from taotrader.chain.reader import JsonRpcChainReader, SnapshotDecodeError
from taotrader.chain.rpc import Endpoint, EndpointStats, Role, RpcExhausted, RpcFatal, RpcPool, TokenBucket
from taotrader.core.protocols import SwapSim
from taotrader.core.state import ChainSnapshot, HotkeyIdx, Quality, SubnetState
from taotrader.core.units import AlphaRao, Block, BlockHash, Hotkey, NetUid, Rao, SubnetKey
from taotrader.data import collector as col
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore
from taotrader.protocol.amm import quote_buy, quote_sell
from taotrader.protocol.prune import prune_target

BASE = 9_000_000                       # era C, escrow era
MEMBER_EVERY = 1_200                   # small membership grid for tests (multiple of the 600 level)
CHUNK = 1_800


class KillSwitch(BaseException):
    """Simulates the process dying mid-chunk (not an Exception: nothing catches it)."""


class FakeArchive:
    """Deterministic synthetic chain: netuids 1..4; netuid 3's generation (reg_at 8,000,003) is pruned at
    `removal` and re-registered 20 blocks later. Each generation has 8 validators whose TotalHotkeyAlpha ranking
    rotates every membership period, so the top-5 selection changes and stickiness matters."""

    def __init__(self, make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot],
                 hk: Callable[[int], Hotkey], *, removal: int = BASE + 2_000) -> None:
        self.make_subnet = make_subnet
        self.make_snapshot = make_snapshot
        self.hk = hk
        self.removal = removal
        self.calls = 0
        self.snapshot_calls = 0
        self.invalid: set[int] = set()
        self.fail_at_call: int | None = None
        self.kill_at_snapshot: int | None = None
        self.price_bias = Decimal(1)
        self.era_b_runtime = False
        self.listing_calls: list[int] = []

    # ---------------------------------------------------------------- world
    def gens(self, block: int) -> dict[int, int]:
        out = {1: 8_000_001, 2: 8_000_002, 4: 8_000_004}
        if block < self.removal:
            out[3] = 8_000_003
        elif block >= self.removal + 20:
            out[3] = self.removal + 20
        return dict(sorted(out.items()))

    def validators(self, n: int) -> list[Hotkey]:
        return [self.hk(n * 100 + i) for i in range(8)]

    def owner(self, n: int) -> Hotkey:
        return self.hk(n * 100 + 50)

    def alpha(self, n: int, i: int, block: int) -> int:
        return 10**12 * ((i + block // MEMBER_EVERY) % 8 + 1) + n

    def listing(self, block: int) -> dict[int, tuple[Hotkey, ...]]:
        return {n: tuple(sorted(self.validators(n))[: 6 + (block // MEMBER_EVERY) % 3]) for n in self.gens(block)}

    def idx(self, n: int, h: Hotkey, block: int) -> HotkeyIdx:
        vals = self.validators(n)
        if h in vals:
            i = vals.index(h)
            listed = h in self.listing(block)[n]
            a = self.alpha(n, i, block)
            return HotkeyIdx(hotkey=h, total_alpha=AlphaRao(a), total_shares=Decimal(a) - Decimal(n), take_u16=0 if i == 7 else 11_796,
                             earns=listed, last_dividend=AlphaRao(1_000 if listed else 0))
        return HotkeyIdx(hotkey=h, total_alpha=AlphaRao(5 * 10**12), total_shares=Decimal(5 * 10**12))

    def subnet(self, n: int, reg_at: int, block: int, tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> SubnetState:
        key = SubnetKey(NetUid(n), Block(reg_at))
        hks = {self.owner(n)} | {h for k, h in tracked if k == key}
        tao = 500 * 10**9 + n * 10**9 + (block % 7_200) * 1_000
        s = self.make_subnet(n, reg_at=reg_at, moving_price=Decimal(n) / Decimal(1000), owner_hotkey=self.owner(n),
                             hotkeys=tuple(self.idx(n, h, block) for h in sorted(hks)))
        return replace(s, pool=replace(s.pool, tao=Rao(tao), px_tao=tao))

    def snap(self, block: int, tracked: Sequence[tuple[SubnetKey, Hotkey]] = ()) -> ChainSnapshot:
        subs = [self.subnet(n, r, block, tracked) for n, r in self.gens(block).items()]
        return self.make_snapshot(block, subs, timestamp_ms=1_700_000_000_000 + block * 12_000)

    # ---------------------------------------------------------------- ArchiveSource
    def _tick(self) -> None:
        self.calls += 1
        if self.fail_at_call is not None and self.calls == self.fail_at_call:
            raise RpcExhausted("fake: retries exhausted")

    async def block_hash(self, block: int) -> BlockHash:
        self._tick()
        return BlockHash("0x" + f"{block:064x}")

    async def block_hashes(self, blocks: Sequence[int]) -> dict[int, BlockHash]:
        self._tick()
        return {b: BlockHash("0x" + f"{b:064x}") for b in blocks}

    async def prime_versions(self, blocks: Sequence[int], hashes: Mapping[int, BlockHash]) -> None:
        return None

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        self._tick()
        return 475, 1

    @staticmethod
    def num(block_hash: str) -> int:
        return int(block_hash, 16)

    async def snapshot(self, block: int, block_hash: BlockHash, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot:
        self._tick()
        self.snapshot_calls += 1
        if self.kill_at_snapshot is not None and self.snapshot_calls == self.kill_at_snapshot:
            raise KillSwitch()
        await asyncio.sleep(0)
        if block in self.invalid:
            raise SnapshotDecodeError("fake undecodable value", block, block_hash, {"0xdead": "0xbeef", "0xabsent": None})
        return self.snap(block, tracked)

    async def generations(self, block_hash: BlockHash) -> dict[int, int]:
        self._tick()
        return self.gens(self.num(block_hash))

    async def dividend_keys_all(self, block_hash: BlockHash) -> dict[int, tuple[Hotkey, ...]]:
        self._tick()
        self.listing_calls.append(self.num(block_hash))
        return self.listing(self.num(block_hash))

    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]:
        self._tick()
        b = self.num(block_hash)
        return {n: AlphaRao(1_000 * n + b % 1_000) for n in self.gens(b)}

    def _sub(self, netuid: int, block_hash: str) -> SubnetState:
        s = self.snap(self.num(block_hash)).by_netuid(netuid)
        assert s is not None
        return s

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        self._tick()
        if self.era_b_runtime:
            raise RpcFatal("fake: method not found")
        s = self.snap(self.num(block_hash))
        return {int(x.key.netuid): int(Decimal(int(x.pool.spot_rao())) * self.price_bias) for x in s.subnets}

    async def current_price(self, netuid: NetUid, block_hash: BlockHash) -> int:
        self._tick()
        raise RpcFatal("fake: method not found")

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        self._tick()
        if self.era_b_runtime:
            raise RpcFatal("fake: method not found")
        q = quote_buy(self._sub(int(netuid), block_hash).pool, tao_rao)   # type: ignore[arg-type]
        return SwapSim(tao_rao, q.amount_out, q.fee, 0, 0, 0)

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        self._tick()
        q = quote_sell(self._sub(int(netuid), block_hash).pool, alpha_rao)   # type: ignore[arg-type]
        return SwapSim(q.amount_out, alpha_rao, 0, q.fee, 0, 0)

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        self._tick()
        t = prune_target(self.snap(self.num(block_hash)))
        return None if t is None else t.netuid

    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]:
        self._tick()
        raise AssertionError("the collector does not query raw storage")

    def providers(self) -> dict[str, int]:
        return {"fake": self.calls}


@pytest.fixture
def world(make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot],
          hk: Callable[[int], Hotkey]) -> Callable[..., FakeArchive]:
    return lambda **kw: FakeArchive(make_subnet, make_snapshot, hk, **kw)


def cfg(**kw: Any) -> col.CollectorCfg:
    base: dict[str, Any] = {"chunk_blocks": CHUNK, "panel_from": BASE, "membership_every": MEMBER_EVERY,
                            "price_every_blocks": 600, "amm_every_blocks": 1_200, "prune_every_blocks": 600}
    base.update(kw)
    return col.CollectorCfg(**base)


SCHED = col.ScheduleSpec("c60", BASE, BASE + 3_600, col.LEVELS_C60)


def run(src: FakeArchive, root: Path, *, c: col.CollectorCfg | None = None, scheds: Sequence[col.ScheduleSpec] = (SCHED,),
        **kw: Any) -> col.RunSummary:
    lake = Lake(root)
    collector = col.Collector(src, lake, c or cfg())
    try:
        return asyncio.run(collector.run(scheds, **kw))
    finally:
        collector.close()
        lake.close()


def manifest(root: Path) -> list[tuple[str, str, int]]:
    """Each lake gets its own directory: Lake(root) keeps its manifest in root/../state.sqlite."""
    with Lake(root) as lake:
        return [(c.path, c.sha256, c.rows) for c in lake.manifest()]


def stored_blocks(root: Path) -> list[int]:
    with Lake(root) as lake:
        return [r.block for r in lake.snapshot_refs()]


# ------------------------------------------------------------------------------------------------ schedules
def test_schedule_levels_partition_the_grid_coarse_to_fine() -> None:
    s = col.ScheduleSpec("c60", 9_000_030, 9_006_000, col.LEVELS_C60)
    l0, l1, l2 = (s.level_blocks(i) for i in range(3))
    assert all(b % 600 == 0 for b in l0) and all(b % 300 == 0 and b % 600 for b in l1)
    assert all(b % 60 == 0 and b % 300 for b in l2)
    assert sorted(l0 + l1 + l2) == list(range(9_000_060, 9_006_001, 60)) == s.blocks()
    assert col.schedule_c60(9_000_000).start == 8_486_594 and col.schedule_c60(9_000_000).levels == (600, 300, 60)
    assert col.schedule_h300(5_000_000).start == 4_920_351 and col.schedule_h300(5_000_000).step == 300
    with pytest.raises(ValueError):
        col.ScheduleSpec("bad", 0, 10, (600, 400))
    with pytest.raises(ValueError):
        col.ScheduleSpec("bad", 10, 0, (60,))


def test_membership_points_grid() -> None:
    assert col.membership_points(8_466_531, 8_490_000, 7_200, 600) == [8_466_600, 8_467_200, 8_474_400, 8_481_600, 8_488_800]
    assert col.membership_points(9_000_000, 9_003_000, 1_200, 600) == [9_000_000, 9_001_200, 9_002_400]
    assert col.membership_points(9_000_001, 8_000_000, 1_200, 600) == []
    with pytest.raises(ValueError):
        col.membership_points(0, 10, 1_000, 600)


def test_plan_orders_membership_first_then_levels(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    with Lake(tmp_path / "lake") as lake:
        c = col.Collector(world(), lake, cfg())
        try:
            plans = c.plan([SCHED])
        finally:
            c.close()
    kinds = [(p.kind, p.level) for p in plans]
    assert kinds[0][0] == col.MEMBERSHIP
    order = [k for k in kinds if k[0] != col.MEMBERSHIP]
    assert order == sorted(order, key=lambda k: k[1])                      # 600 -> 300 -> 60
    mem = [b for p in plans if p.kind == col.MEMBERSHIP for b in p.blocks]
    assert mem == [9_000_000, 9_001_200, 9_002_400, 9_003_600]
    every = sorted(b for p in plans for b in p.blocks)
    assert every == sorted(set(every)) == SCHED.blocks()                    # each block planned exactly once
    assert all(p.blocks[0] // CHUNK == p.blocks[-1] // CHUNK == p.bucket for p in plans)


# ------------------------------------------------------------------------------------------------ end to end
def test_collect_end_to_end(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    s = run(src, tmp_path / "lake")
    assert s.snapshots == len(SCHED.blocks()) and s.invalid == 0 and s.chunks_failed == 0
    assert s.membership_points == 4
    assert s.calib_rows > 0 and max(s.calib_max.values()) == 0.0
    assert set(s.calib_max) == {col.PROBE_PRICE, col.PROBE_SIM_BUY, col.PROBE_SIM_SELL, col.PROBE_PRUNE}
    assert stored_blocks(tmp_path / "lake") == SCHED.blocks()
    with Lake(tmp_path / "lake") as lake:
        led = col.FetchLedger(lake.state_db)
        try:
            rows = led.load()
            assert {r.status for r in rows.values()} == {col.STATUS_OK} and sorted(rows) == SCHED.blocks()
            assert led.meta()["panel_from"] == str(BASE)
        finally:
            led.close()
        con = lake.connect()
        try:
            div = con.execute("SELECT DISTINCT block FROM v_dividend_keys ORDER BY block").fetchall()
            esc = con.execute("SELECT block, netuid, escrow_alpha, escrow_block FROM v_subnet ORDER BY block, netuid").fetchall()
            calib = con.execute("SELECT probe, count(*), max(rel_err) FROM v_calib GROUP BY probe ORDER BY probe").fetchall()
        finally:
            con.close()
        assert [int(r[0]) for r in div] == [9_000_000, 9_001_200, 9_002_400, 9_003_600]
        for b, n, e, eb in esc:
            assert eb == b // 360 * 360 and e == 1_000 * n + eb % 1_000      # past escrow grid value, source block kept
        assert all(r[2] == 0.0 for r in calib)
        snap = LakeSnapshotStore(lake, clock=BASE + 3_600).at(Block(BASE + 60))
        assert snap.subnets[0].escrow_alpha == 1_000 + (BASE // 360 * 360) % 1_000
    # a second run has nothing to do
    s2 = run(src, tmp_path / "lake")
    assert s2.snapshots == 0 and s2.chunks_committed == 0 and s2.chunks_skipped > 0


def test_kill_and_resume_gives_identical_manifest_and_no_duplicates(world: Callable[..., FakeArchive],
                                                                    tmp_path: Path) -> None:
    run(world(), tmp_path / "a" / "lake")
    want = manifest(tmp_path / "a" / "lake")
    # (1) killed mid-chunk (the in-flight chunk is lost), resumed by a new process
    src = world()
    src.kill_at_snapshot = 23
    with pytest.raises(KillSwitch):
        run(src, tmp_path / "b" / "lake")
    assert 0 < len(stored_blocks(tmp_path / "b" / "lake")) < len(SCHED.blocks())
    src.kill_at_snapshot = None
    run(src, tmp_path / "b" / "lake")
    assert manifest(tmp_path / "b" / "lake") == want
    blocks = stored_blocks(tmp_path / "b" / "lake")
    assert blocks == sorted(set(blocks)) == SCHED.blocks()                 # no duplicate snapshot anywhere
    # (2) killed between the lake commit and the ledger commit: the lake is reconciled, nothing is rewritten
    src3 = world()
    calls = {"n": 0}
    orig = col.FetchLedger.mark

    def flaky(self: col.FetchLedger, rows: Sequence[tuple[int, str, str, str]]) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise KillSwitch()
        orig(self, rows)

    col.FetchLedger.mark = flaky                                            # type: ignore[method-assign]
    try:
        with pytest.raises(KillSwitch):
            run(src3, tmp_path / "c" / "lake")
    finally:
        col.FetchLedger.mark = orig                                         # type: ignore[method-assign]
    run(src3, tmp_path / "c" / "lake")
    assert manifest(tmp_path / "c" / "lake") == want


def test_transient_failure_marks_failed_then_resume_completes(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    run(world(), tmp_path / "a" / "lake")
    src = world()
    src.fail_at_call = 60                                                   # inside a schedule chunk
    s = run(src, tmp_path / "b" / "lake")
    assert s.chunks_failed == 1 and s.failed_blocks
    with Lake(tmp_path / "b" / "lake") as lake:
        led = col.FetchLedger(lake.state_db)
        try:
            failed = [r for r in led.load().values() if r.status == col.STATUS_FAILED]
        finally:
            led.close()
    assert failed and all("RpcExhausted" in r.last_error and r.attempts == 1 for r in failed)
    assert not set(s.failed_blocks) & set(stored_blocks(tmp_path / "b" / "lake"))   # nothing of the failed chunk was written
    src.fail_at_call = None
    run(src, tmp_path / "b" / "lake")
    assert manifest(tmp_path / "b" / "lake") == manifest(tmp_path / "a" / "lake")


def test_failed_membership_chunk_stops_the_run(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    src.fail_at_call = 3                                                    # first membership chunk
    s = run(src, tmp_path / "lake")
    assert s.chunks_failed == 1 and s.snapshots == 0 and s.chunks_committed == 0


def test_invalid_block_raw_kept_skipped_and_retryable(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    bad = BASE + 660
    src.invalid = {bad}
    s = run(src, tmp_path / "lake")
    assert s.invalid == 1 and bad not in stored_blocks(tmp_path / "lake")
    with Lake(tmp_path / "lake") as lake:
        con = lake.connect()
        try:
            raw = con.execute("SELECT block, call, request_sha, response_zstd FROM v_raw_rpc").fetchall()
        finally:
            con.close()
        led = col.FetchLedger(lake.state_db)
        try:
            row = led.load()[bad]
        finally:
            led.close()
    assert [(int(r[0]), r[1]) for r in raw] == [(bad, "snapshot")]
    import json

    import zstandard
    payload = json.loads(zstandard.ZstdDecompressor().decompress(bytes(raw[0][3])))
    assert payload["raw"] == {"0xdead": "0xbeef", "0xabsent": None} and "undecodable" in payload["error"]
    assert row.status == col.STATUS_INVALID and "undecodable" in row.last_error
    assert run(src, tmp_path / "lake").snapshots == 0                      # INVALID is skipped by default
    src.invalid = set()
    assert run(src, tmp_path / "lake", retry_invalid=True).snapshots == 1
    assert bad in stored_blocks(tmp_path / "lake")


# ------------------------------------------------------------------------------------------------ panel
def test_panel_is_point_in_time_sticky_and_has_no_gaps(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    run(src, tmp_path / "lake")
    with Lake(tmp_path / "lake") as lake:
        assert col.panel_gaps(lake, cfg()) == []
        tr = col.rebuild_tracker(lake, cfg(), BASE + 3_600)
        store = LakeSnapshotStore(lake, clock=BASE + 3_600)
        t0, t1 = set(tr.tracked_at(BASE)), set(tr.tracked_at(BASE + 1_200))
        # sticky: everything tracked at the first membership block that is still alive stays tracked
        assert {(k, h) for k, h in t0 if k.netuid != 3} <= t1
        assert len(t1) > len(t0)                                            # the ranking rotated: new earners joined
        # point in time: a pair first selected at 9,001,200 is absent from snapshots before it
        new = sorted(t1 - t0)
        assert new
        k, h = new[0]
        early = store.at(Block(BASE + 600)).get(k)
        late = store.at(Block(BASE + 1_260)).get(k)
        assert early is not None and early.hotkey(h) is None
        assert late is not None and late.hotkey(h) is not None
        # every generation's owner hotkey is tracked; the pruned generation's pairs end with it
        snap = store.at(Block(BASE + 2_400))
        for s in snap.subnets:
            assert s.owner_hotkey is not None and s.hotkey(s.owner_hotkey) is not None
        old3 = SubnetKey(NetUid(3), Block(8_000_003))
        assert all(k != old3 for k, _ in tr.tracked_at(BASE + 2_400))
        # the take-0 earner (validator index 7) is always tracked while listed
        take0 = Hotkey("0x" + f"{107:064x}")
        assert (SubnetKey(NetUid(1), Block(8_000_001)), take0) in set(tr.tracked_at(BASE + 3_600))
    assert src.listing_calls == [9_000_000, 9_001_200, 9_002_400, 9_003_600]   # listed at each historical hash


def test_panel_gap_detector_flags_a_missing_pair(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    orig = src.snapshot
    target = BASE + 1_860

    async def drop(block: int, block_hash: BlockHash, prev: ChainSnapshot | None,
                   tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot:
        if block == target:
            tracked = tracked[1:]                                            # the collector "forgot" one pair
        return await orig(block, block_hash, prev, tracked)

    src.snapshot = drop                                                      # type: ignore[method-assign]
    run(src, tmp_path / "lake")
    with Lake(tmp_path / "lake") as lake:
        gaps = col.panel_gaps(lake, cfg())
    assert gaps and {g.missing_block for g in gaps} == {target}


# ------------------------------------------------------------------------------------------------ probes and drift
def test_calibration_drift_aborts_before_writing(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    src.price_bias = Decimal("1.01")
    with pytest.raises(col.CalibrationDrift) as ei:
        run(src, tmp_path / "lake")
    assert ei.value.rows and max(r.rel_err for r in ei.value.rows) > 0.009
    assert stored_blocks(tmp_path / "lake") == []                          # the first chunk was not written


def test_probes_skip_missing_runtime_apis(world: Callable[..., FakeArchive]) -> None:
    src = world()
    src.era_b_runtime = True
    snap = src.snap(BASE)
    assert asyncio.run(col.price_probe(src, snap)) == []
    assert asyncio.run(col.amm_probe(src, snap, 10**9)) == []
    rows = asyncio.run(col.prune_probe(src, snap))
    assert len(rows) == 1 and rows[0].rel_err == 0.0


def test_price_probe_tolerates_the_rao_quantum_and_skips_ta_price(world: Callable[..., FakeArchive]) -> None:
    src = world()
    snap = src.snap(BASE)
    s0 = snap.subnets[0]
    snap = replace(snap, subnets=(replace(s0, quality=Quality.TA_PRICE),) + snap.subnets[1:])

    async def off_by_one(block_hash: BlockHash) -> dict[int, int]:
        return {int(s.key.netuid): int(s.pool.spot_rao()) + 1 for s in snap.subnets}

    src.prices_all = off_by_one                                              # type: ignore[method-assign]
    rows = asyncio.run(col.price_probe(src, snap))
    assert {r.netuid for r in rows} == {int(s.key.netuid) for s in snap.subnets[1:]}
    assert all(r.rel_err == 0.0 for r in rows)


def test_config_mismatch_fails_closed(world: Callable[..., FakeArchive], tmp_path: Path) -> None:
    src = world()
    run(src, tmp_path / "lake", scheds=(col.ScheduleSpec("c60", BASE, BASE + 600, col.LEVELS_C60),))
    with pytest.raises(col.ConfigMismatch):
        run(src, tmp_path / "lake", c=cfg(top_n=3))


def test_finalize_snapshot_flags_escrow_and_digest(world: Callable[..., FakeArchive]) -> None:
    src = world()
    snap = src.snap(5_611_800)
    out = col.finalize_snapshot(snap, escrow={1: 7}, stall=col.stall_between(5_611_500, 5_611_800), refined=True)
    assert all(s.quality & Quality.CHAIN_STALL_GAP and s.quality & Quality.REFINED for s in out.subnets)
    assert out.by_netuid(1) is not None and out.by_netuid(1).escrow_alpha == 7   # type: ignore[union-attr]
    assert out.by_netuid(2).escrow_alpha == 0                                     # type: ignore[union-attr]
    assert out.digest and out.digest != snap.digest
    assert col.stall_between(5_611_658, 5_611_900) and not col.stall_between(5_611_659, 5_611_900)
    assert not col.stall_between(5_611_300, 5_611_600)
    assert col.finalize_snapshot(snap).subnets[0].escrow_alpha is None


def test_escrow_grid_source_blocks(world: Callable[..., FakeArchive]) -> None:
    e = col.EscrowCache(world())
    assert e.source_block(8_765_683) is None
    assert e.source_block(8_765_700) == 8_765_684                          # first escrow block, not the earlier grid point
    assert e.source_block(9_000_100) == 9_000_000 and e.source_block(9_000_360) == 9_000_360


def test_escrow_failure_leaves_escrow_unknown(world: Callable[..., FakeArchive]) -> None:
    src = world()

    async def broken(block_hash: BlockHash) -> dict[int, AlphaRao]:
        raise RpcFatal("fake: no StakeInfo api")

    src.escrow_by_subnet = broken                                            # type: ignore[method-assign]
    e = col.EscrowCache(src)
    asyncio.run(e.fill([9_000_100]))
    assert e.at(9_000_100) == (9_000_000, None)


# ------------------------------------------------------------------------------------------------ ReaderSource
class _Node:
    """Minimal JSON-RPC transport for ReaderSource: getKeysPaged over AlphaDividendsPerSubnet and queryStorageAt."""

    url = "fake://node"

    def __init__(self, keys: list[str], storage: dict[str, str]) -> None:
        self.keys = sorted(keys)
        self.storage = storage
        self.pages = 0

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        if method == "state_getKeysPaged":
            pre, count, start, _h = params
            self.pages += 1
            sel = [k for k in self.keys if k.startswith(pre) and (start is None or k > start)]
            return sel[:count]
        if method == "state_queryStorageAt":
            keys, h = params
            return [{"block": h, "changes": [[k, self.storage.get(k)] for k in keys]}]
        raise AssertionError(method)

    async def aclose(self) -> None:
        return None


def _reader(node: _Node) -> JsonRpcChainReader:
    async def nosleep(_s: float) -> None:
        return None

    ep = Endpoint(label="node", role=Role.ARCHIVE, transport=node,
                  bucket=TokenBucket(1e9, 10**6, lambda: 0.0, nosleep), sem=asyncio.Semaphore(3),
                  stats=EndpointStats(label="node", role=Role.ARCHIVE))
    return JsonRpcChainReader(RpcPool([ep], sleep=nosleep), provider_check_every=None)


def test_reader_source_lists_dividend_keys_in_pages_and_generations(hk: Callable[[int], Hotkey]) -> None:
    row = it.HOTKEY["last_dividend"]
    keys = [to_hex(row.key(netuid=n, hotkey=hk(n * 10_000 + i))) for n in (1, 2, 3) for i in range(700)]
    added, reg = it.SUBNET[it.NETWORKS_ADDED], it.SUBNET["reg_at"]
    storage = {to_hex(added.key(netuid=1)): "0x01", to_hex(reg.key(netuid=1)): "0x" + (11).to_bytes(8, "little").hex(),
               to_hex(added.key(netuid=2)): "0x00", to_hex(reg.key(netuid=2)): "0x" + (22).to_bytes(8, "little").hex(),
               to_hex(added.key(netuid=3)): "0x01"}
    node = _Node(keys, storage)
    src = col.ReaderSource(_reader(node))
    h = BlockHash("0x" + "11" * 32)
    listing = asyncio.run(src.dividend_keys_all(h))
    assert sorted(listing) == [1, 2, 3] and all(len(v) == 700 for v in listing.values())
    assert listing[2] == tuple(sorted(Hotkey(to_hex(account(hk(20_000 + i)))) for i in range(700)))
    assert node.pages == 3                                                   # 2,100 keys in pages of 1,000
    assert asyncio.run(src.generations(h)) == {1: 11, 3: 0}
    assert src.providers() == {"node": 4}


# ------------------------------------------------------------------------------------------------ network
ARCHIVE = "https://bittensor-finney.api.onfinality.io/public"


@pytest.mark.network
def test_network_short_era_c_backfill_end_to_end(tmp_path: Path) -> None:
    """Reduced section 11 WP4 acceptance (the multi-hour era-C backfill is run by the lead): 61 era-C 60-block
    snapshots across the spec-475 setCode (9,233,781), with a point-in-time membership listing, escrow on its grid,
    price / AMM / prune probes (calibration error <= 1e-6) and a gap-free panel."""
    from taotrader.chain.rpc import RpcPool as Pool

    start, end = 9_230_400, 9_234_000
    c = col.CollectorCfg(panel_from=start, price_every_blocks=600, amm_every_blocks=1_200, prune_every_blocks=600)

    async def go() -> col.RunSummary:
        pool = Pool.from_urls(archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
        src = col.ReaderSource(JsonRpcChainReader(pool, keys_per_call=2_500, provider_check_every=None))
        lake = Lake(tmp_path / "lake")
        collector = col.Collector(src, lake, c)
        try:
            return await collector.run([col.ScheduleSpec("c60", start, end, col.LEVELS_C60)])
        finally:
            collector.close()
            lake.close()
            await pool.aclose()

    s = asyncio.run(go())
    print("summary:", s)
    assert s.snapshots == 61 and s.invalid == 0 and s.chunks_failed == 0 and s.membership_points == 1
    assert s.calib_max.get(col.PROBE_PRICE) is not None and max(s.calib_max.values()) <= 1e-6
    with Lake(tmp_path / "lake") as lake:
        assert col.panel_gaps(lake, c) == []
        store = LakeSnapshotStore(lake, clock=end)
        first, last = store.at(Block(start)), store.at(Block(end))
        print("specs:", first.glob.spec_version, last.glob.spec_version, "subnets:", len(last.subnets),
              "hotkeys at m:", sum(len(x.hotkeys) for x in first.subnets), "after:", sum(len(x.hotkeys) for x in last.subnets))
        assert first.glob.spec_version == 473 and last.glob.spec_version == 475
        assert all(x.escrow_alpha is not None for x in last.subnets)
        assert all(x.owner_hotkey is None or x.hotkey(x.owner_hotkey) is not None for x in last.subnets)
        con = lake.connect()
        try:
            n_div = con.execute("SELECT count(*) FROM v_dividend_keys").fetchone()
        finally:
            con.close()
        assert n_div is not None and n_div[0] > 100
