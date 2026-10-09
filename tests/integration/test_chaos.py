"""Section 10.3 item 9: chaos - reader faults, lake integrity, gap flagging and disk-full on the journal commit.

- Reader: a fake JSON-RPC archive (the recorded FULL-snapshot cassette of block 9,240,388) behind a chaotic endpoint
  injecting -32029, -32004 (historical budget), HTTP 429 with Retry-After and truncated bodies, with a clean second
  endpoint. The WP1 reader must return the snapshot with the recorded digest (no partial snapshot), with bounded
  retries and a rotation to the clean endpoint. (WS drops and stale finalized heads are WP1's LiveChainFeed chaos
  tests, tests/chain/test_head.py.)
- Lake: a chunk damaged on disk is refused against the manifest's sha256 (no snapshot from a damaged chunk), and the
  Runner fails closed on it; nothing in the journal changes.
- Gap flagging: a feed that skips blocks journals HealthObs.feed_gap_blocks with the snapshot, and the chain-event diff
  across the gap still reports every flip (skipped blocks coarsen events, they cannot hide them).
- Disk full on the journal commit: the Runner is poisoned, the journal is unchanged and verifies (no corruption, no
  partial batch), and a restart once the disk has room finishes with the money state of an uninterrupted run.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from taotrader.backtest.runner import run_pass
from taotrader.chain.reader import JsonRpcChainReader
from taotrader.chain.rpc import CassetteTransport, Role, RpcPool, RpcTransient, classify_error
from taotrader.core.events import ChainEventKind, HealthObs, SnapshotObserved
from taotrader.core.state import ReadPlan
from taotrader.core.units import Block, BlockHash, NetUid, SubnetKey
from taotrader.data.journal import SqliteJournal, decode_record
from taotrader.data.lake import Lake
from taotrader.data.store import LakeSnapshotStore
from taotrader.engine.runner import RunnerPoisoned
from taotrader.protocol.derive import derive_events

CASSETTES = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"
WAVES = Path(__file__).resolve().parents[1] / "fixtures" / "minilake" / "waves"


class ChaosTransport:
    """Cassette replay that fails the first calls of each kind with a scripted sequence of provider faults."""

    def __init__(self, inner: CassetteTransport, script: Sequence[str]) -> None:
        self.inner = inner
        self.url = "chaos://archive"
        self.script = list(script)
        self.calls = 0
        self.injected: list[str] = []

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        self.calls += 1
        if self.script:
            fault = self.script.pop(0)
            self.injected.append(fault)
            if fault == "-32029":
                raise classify_error({"code": -32029, "message": "Too many requests"})
            if fault == "-32004":
                raise classify_error({"code": -32004, "message": "Historical work rate limit exceeded"})
            if fault == "429":
                raise RpcTransient("HTTP 429", retry_after=2.0)
            if fault == "truncated":
                raise RpcTransient('malformed JSON-RPC response: {"jsonrpc":"2.0","res')
        return await self.inner.request(method, params, timeout)

    async def aclose(self) -> None:
        return None


def test_reader_survives_provider_chaos_without_partial_snapshots() -> None:
    expected = json.loads((CASSETTES / "reader_9240388.json").read_text(encoding="utf-8"))
    chaos = ChaosTransport(CassetteTransport.load(CASSETTES / "full_9240388.jsonl"), ["-32029", "429", "-32004", "truncated"])
    clean = ChaosTransport(CassetteTransport.load(CASSETTES / "full_9240388.jsonl"), ["truncated", "-32029", "429"])
    slept: list[float] = []
    now = [0.0]

    async def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    def clock() -> float:
        return now[0]

    eps = [RpcPool.make_endpoint(chaos.url, Role.ARCHIVE, transport=chaos, rate_per_s=1e6, burst=10**6, clock=clock,
                                 sleep=sleep, label="chaos"),
           RpcPool.make_endpoint(clean.url, Role.ARCHIVE, transport=clean, rate_per_s=1e6, burst=10**6, clock=clock,
                                 sleep=sleep, label="clean")]
    pool = RpcPool(eps, clock=clock, sleep=sleep)
    reader = JsonRpcChainReader(pool, provider_check_every=None, prune_check_every=1)   # as recorded (WP1)

    async def go() -> Any:
        h = BlockHash(expected["block_hash"])
        hks = await reader.dividend_keys(NetUid(92), h)
        tracked = [(SubnetKey(NetUid(92), Block(8_352_006)), x) for x in hks]
        return await reader.snapshot(Block(expected["block"]), h, ReadPlan.FULL, None, tracked)

    snap = asyncio.run(go())
    assert snap.digest == expected["full_digest"]               # complete and identical: no partial snapshot
    stats = pool.stats()
    injected = chaos.injected + clean.injected
    assert {"-32029", "429", "truncated"} <= set(injected) and len(injected) >= 5
    assert stats["chaos"].transient >= 2 and stats["clean"].transient >= 2
    assert stats["chaos"].rotations + stats["clean"].rotations >= 1           # rotated between endpoints
    assert chaos.calls + clean.calls <= expected["records"]["full"] + len(injected) + 4   # bounded retries
    assert slept and max(slept) <= pool.backoff_cap_s + 300      # backoff honoured (fake clock), never unbounded


def test_damaged_chunk_is_refused_and_the_runner_fails_closed(itx: Any, short_plan: Any, tmp_path: Path) -> None:
    lake_dir, state = itx.MINILAKE
    copy = tmp_path / "copy"
    shutil.copytree(lake_dir, copy / "lake")
    shutil.copy2(state, copy / "state.sqlite")
    victim = sorted((copy / "lake" / "snap_subnet").rglob("*.parquet"))[0]
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    victim.write_bytes(bytes(raw))
    lake = Lake(copy / "lake", copy / "state.sqlite")
    try:
        assert lake.verify(deep=True)                           # the manifest sha256 catches the damage
        j = SqliteJournal(":memory:", durable=False)
        with pytest.raises(Exception):                          # noqa: B017 - any refusal; never a partial snapshot
            run_pass(short_plan, lake, books=["base-cash"], end=8_766_000, journal=j, observe=False)
        assert j.head()[0] == 0 or j.verify_chain() == j.head()[0]
    finally:
        lake.close()


def test_feed_gap_is_flagged_and_events_span_the_gap(itx: Any, short_plan: Any, lake: Lake) -> None:
    wl = Lake(WAVES / "lake", WAVES / "state.sqlite")
    try:
        st = LakeSnapshotStore(wl, clock=9_029_889)
        a, z = st.at(Block(8_463_544)), st.at(Block(9_029_888))
        ev = derive_events(a, z)
        toggled = {e.key for e in ev if e.kind is ChainEventKind.EMISSION_TOGGLED}
        raw_flips = {s.key for s in z.subnets if a.get(s.key) is not None
                     and a.get(s.key).emission_enabled != s.emission_enabled}  # type: ignore[union-attr]
        assert toggled == raw_flips and toggled
        assert {e.key for e in ev if e.kind is ChainEventKind.DEREGISTERED} == {s.key for s in a.subnets} - {
            s.key for s in z.subnets}
    finally:
        wl.close()
    gap = HealthObs(finality_lag_blocks=3, secs_since_block=12, healthy_endpoints=2, head_lag_blocks=0, feed_gap_blocks=540)
    j = SqliteJournal(":memory:", durable=False)
    run_pass(short_plan, lake, books=["base-cash"], end=8_766_000, journal=j, observe=False, health=gap)
    obs = [decode_record(r) for r in j.read(1) if r.kind == "snapshot_observed"]
    assert obs and all(isinstance(o, SnapshotObserved) and o.health.feed_gap_blocks == 540 for o in obs)


class _DiskFullJournal(SqliteJournal):
    def __init__(self, path: str, fail_at: int) -> None:
        super().__init__(path, durable=False)
        self.fail_at = fail_at
        self.n = 0

    def append_batch(self, items: Any) -> Any:
        self.n += 1
        if self.n == self.fail_at:
            raise sqlite3.OperationalError("database or disk is full")
        return super().append_batch(items)


def test_disk_full_on_commit_halts_without_corruption_and_resumes(itx: Any, short_plan: Any, lake: Lake,
                                                                  tmp_path: Path) -> None:
    books = itx.short_books()
    counting = _DiskFullJournal(str(tmp_path / "count.sqlite"), fail_at=-1)
    ref = run_pass(short_plan, lake, books=books, end=8_776_000, journal=counting, observe=False)
    assert counting.n > 10
    counting.close()
    path = str(tmp_path / "j.sqlite")
    j = _DiskFullJournal(path, fail_at=counting.n // 2)            # the disk fills half-way through the run
    with pytest.raises((sqlite3.OperationalError, RunnerPoisoned)):
        run_pass(short_plan, lake, books=books, end=8_776_000, journal=j, observe=False)
    head = j.head()
    assert head[0] > 0 and j.verify_chain(deep=True) == head[0]   # intact, no partial batch
    j.close()
    j2 = SqliteJournal(path, durable=False)
    res = run_pass(short_plan, lake, books=books, end=8_776_000, journal=j2, observe=False)
    assert {k: v.money_digest for k, v in res.books.items()} == {k: v.money_digest for k, v in ref.books.items()}
    j2.close()
