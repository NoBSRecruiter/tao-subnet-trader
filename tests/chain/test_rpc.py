"""chain.rpc chaos tests (DESIGN.md section 6.10): 429 / -32029 / -32004 / WS drop / truncated body give bounded
retries, rotation and breakers, on fake time; no partial snapshot ever escapes the reader."""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from taotrader.chain.reader import SnapshotDecodeError
from taotrader.chain.rpc import (
    CassetteTransport,
    HistoricalBudget,
    HttpTransport,
    RecordingTransport,
    Role,
    RpcExhausted,
    RpcFatal,
    RpcPool,
    RpcTransient,
    StateDiscarded,
    Subscription,
    TokenBucket,
    WsTransport,
    classify_error,
    redact_url,
)
from taotrader.core.state import ReadPlan
from taotrader.core.units import Block, BlockHash


def err(code: int, msg: str = "boom") -> Exception:
    return classify_error({"code": code, "message": msg})


def test_classify_errors() -> None:
    assert isinstance(err(-32029), RpcTransient) and not isinstance(err(-32029), HistoricalBudget)
    assert isinstance(err(-32005), RpcTransient) and isinstance(err(-32603), RpcTransient)
    assert isinstance(err(-32004, "Historical work rate limit exceeded"), HistoricalBudget)
    assert isinstance(err(4003, "UnknownBlock: State already discarded for 0xab"), StateDiscarded)
    assert isinstance(err(-32602, "Invalid params"), RpcFatal)
    e = classify_error({"code": -32000, "message": "slow down", "data": {"retry_after_seconds": 4}})
    assert isinstance(e, RpcTransient) and e.retry_after == 4.0


def test_allow_list_blocks_non_read_methods(arun: Any, chain: Any, make_rpc_pool: Any) -> None:
    pool = make_rpc_pool((chain, Role.ARCHIVE))
    for m in ("author_submitExtrinsic", "author_submitAndWatchExtrinsic", "system_addReservedPeer"):
        with pytest.raises(RpcFatal):
            arun(pool.call(m, ["0x00"]))
    assert chain.log == []


def _http_pool(handler: Callable[[httpx.Request], httpx.Response], clock: Any) -> RpcPool:
    t = HttpTransport("https://node.example/rpc", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return RpcPool([RpcPool.make_endpoint(t.url, Role.ARCHIVE, transport=t, clock=clock, sleep=clock.sleep,
                                          rate_per_s=1000, burst=1000)],
                   clock=clock, sleep=clock.sleep, rng=random.Random(1))


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def test_http_429_honours_retry_after(arun: Any, clock: Any) -> None:
    seq = [httpx.Response(429, headers={"Retry-After": "7"}), _ok("0xabc")]
    pool = _http_pool(lambda _r: seq.pop(0), clock)
    assert arun(pool.call("chain_getBlockHash", [1])) == "0xabc"
    assert clock.slept == [7.0]
    st = pool.stats()["https://node.example/rpc"]
    assert (st.calls, st.transient, st.ok) == (2, 1, 1)


def test_http_truncated_body_and_5xx_are_transient(arun: Any, clock: Any) -> None:
    seq = [httpx.Response(200, content=b'{"jsonrpc":"2.0","id":1,"resu'), httpx.Response(503), _ok(7)]
    pool = _http_pool(lambda _r: seq.pop(0), clock)
    assert arun(pool.call("chain_getBlockHash", [1])) == 7
    assert len(clock.slept) == 2 and 0.5 <= clock.slept[0] <= 1.5 and 1.0 <= clock.slept[1] <= 3.0


def test_http_transport_errors_and_4xx(arun: Any, clock: Any) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(400, text="bad request")

    pool = _http_pool(handler, clock)
    with pytest.raises(RpcFatal):
        arun(pool.call("chain_getBlockHash", [1]))
    assert calls["n"] == 2


def test_rotation_after_two_consecutive_transients(arun: Any, fake_chain_cls: Any, make_rpc_pool: Any, clock: Any) -> None:
    a, b = fake_chain_cls("fake://a"), fake_chain_cls("fake://b")
    a.hashes[5] = b.hashes[5] = "0x" + "55" * 32
    a.chaos = lambda m, p: err(-32029, "Too many requests")
    pool = make_rpc_pool((a, Role.ARCHIVE), (b, Role.ARCHIVE))
    assert arun(pool.call("chain_getBlockHash", [5])) == "0x" + "55" * 32
    assert len(a.log) == 2 and len(b.log) == 1
    st = pool.stats()
    assert st["archive0"].rotations == 1 and st["archive0"].transient == 2 and st["archive1"].ok == 1
    # the pool stays on the healthy endpoint afterwards
    arun(pool.call("chain_getBlockHash", [5]))
    assert len(a.log) == 2 and len(b.log) == 2


def test_historical_budget_opens_breaker_then_half_open_probe(arun: Any, chain: Any, make_rpc_pool: Any, clock: Any) -> None:
    chain.hashes[9] = "0x" + "99" * 32
    fails = {"n": 1}

    def chaos(m: str, p: list[Any]) -> Exception | None:
        if fails["n"] > 0:
            fails["n"] -= 1
            return err(-32004, "Historical work rate limit exceeded")
        return None

    chain.chaos = chaos
    pool = make_rpc_pool((chain, Role.ARCHIVE))
    t0 = clock.t
    assert arun(pool.call("chain_getBlockHash", [9])) == "0x" + "99" * 32
    st = pool.stats()["archive0"]
    assert st.breaker_opens == 1 and st.breaker_open_until is None          # closed again by the probe
    assert clock.t - t0 >= 300.0                                            # waited for the half-open window
    assert len(chain.log) == 2                                              # exactly one probe call
    assert pool.healthy() == 1


def test_breaker_skips_endpoint_while_open(arun: Any, fake_chain_cls: Any, make_rpc_pool: Any, clock: Any) -> None:
    a, b = fake_chain_cls("fake://a"), fake_chain_cls("fake://b")
    a.hashes[1] = b.hashes[1] = "0x" + "11" * 32
    a.chaos = lambda m, p: err(-32004)
    pool = make_rpc_pool((a, Role.ARCHIVE), (b, Role.ARCHIVE))
    for _ in range(3):
        arun(pool.call("chain_getBlockHash", [1]))
    assert len(a.log) == 1 and len(b.log) == 3
    assert pool.healthy(Role.ARCHIVE) == 1
    clock.t += 301
    assert pool.healthy(Role.ARCHIVE) == 2


def test_state_discarded_reroutes_to_archive(arun: Any, fake_chain_cls: Any, make_rpc_pool: Any) -> None:
    head, arch = fake_chain_cls("fake://lite"), fake_chain_cls("fake://archive")
    head.chaos = lambda m, p: err(4003, "State already discarded for block")
    arch.hashes[3] = "0x" + "33" * 32
    pool = make_rpc_pool((head, Role.HEAD), (arch, Role.ARCHIVE))
    assert arun(pool.call("chain_getBlockHash", [3], Role.HEAD)) == "0x" + "33" * 32
    assert pool.stats()["head0"].discarded == 1 and len(arch.log) == 1
    # on the archive itself a discarded state is final
    arch.chaos = lambda m, p: err(4003, "State already discarded")
    with pytest.raises(StateDiscarded):
        arun(pool.call("chain_getBlockHash", [3], Role.ARCHIVE))


def test_bounded_retries_then_exhausted(arun: Any, chain: Any, make_rpc_pool: Any, clock: Any) -> None:
    chain.chaos = lambda m, p: RpcTransient("ws connection dropped")
    pool = make_rpc_pool((chain, Role.ARCHIVE))
    with pytest.raises(RpcExhausted):
        arun(pool.call("chain_getBlockHash", [1]))
    assert len(chain.log) == 7                                   # 1 call + 6 retries
    assert len(clock.slept) == 6
    for k, s in enumerate(clock.slept, start=1):
        base = min(60.0, 2.0 ** (k - 1))
        assert 0.5 * base <= s <= 1.5 * base


def test_backoff_is_capped_at_60s(chain: Any, make_rpc_pool: Any) -> None:
    pool = make_rpc_pool((chain, Role.ARCHIVE))
    assert all(30.0 <= pool._backoff(k, None) <= 90.0 for k in range(7, 20))
    assert pool._backoff(3, 200.0) == 60.0


def test_fatal_errors_are_not_retried(arun: Any, chain: Any, make_rpc_pool: Any) -> None:
    chain.chaos = lambda m, p: err(-32602, "Invalid params")
    pool = make_rpc_pool((chain, Role.ARCHIVE))
    with pytest.raises(RpcFatal):
        arun(pool.call("chain_getBlockHash", [1]))
    assert len(chain.log) == 1


def test_token_bucket_paces_on_fake_time(arun: Any, clock: Any) -> None:
    bucket = TokenBucket(3.0, 3, clock, clock.sleep)

    async def go() -> None:
        for _ in range(10):
            await bucket.acquire()

    t0 = clock.t
    arun(go())
    assert abs((clock.t - t0) - 7 / 3) < 1e-9                    # burst 3 free, then 3 per second


def test_pool_rate_limit_3_per_s(arun: Any, chain: Any, clock: Any) -> None:
    chain.hashes[1] = "0x" + "11" * 32
    ep = RpcPool.make_endpoint(chain.url, Role.ARCHIVE, transport=chain, clock=clock, sleep=clock.sleep)
    pool = RpcPool([ep], clock=clock, sleep=clock.sleep)

    async def go() -> None:
        await asyncio.gather(*(pool.call("chain_getBlockHash", [1]) for _ in range(9)))

    t0 = clock.t
    arun(go())
    assert abs((clock.t - t0) - 2.0) < 1e-9 and pool.ru_used()[chain.url] == 9


def test_concurrency_cap(arun: Any, clock: Any) -> None:
    class Slow:
        url = "fake://slow"

        def __init__(self) -> None:
            self.inflight = 0
            self.peak = 0

        async def request(self, method: str, params: Any, timeout: float) -> Any:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            for _ in range(5):
                await asyncio.sleep(0)
            self.inflight -= 1
            return 1

        async def aclose(self) -> None:
            return None

    t = Slow()
    ep = RpcPool.make_endpoint(t.url, Role.ARCHIVE, transport=t, clock=clock, sleep=clock.sleep, rate_per_s=1000,
                               burst=1000, max_concurrency=3)
    pool = RpcPool([ep], clock=clock, sleep=clock.sleep)

    async def go() -> None:
        await asyncio.gather(*(pool.call("system_health") for _ in range(12)))

    arun(go())
    assert t.peak == 3


def test_redact_url() -> None:
    assert redact_url("https://bittensor-finney.api.onfinality.io/rpc?apikey=SECRET") == \
        "https://bittensor-finney.api.onfinality.io"
    assert "SECRET" not in redact_url("wss://bittensor-finney.api.onfinality.io/ws/apikey/SECRET")
    assert redact_url("wss://entrypoint-finney.opentensor.ai:443") == "wss://entrypoint-finney.opentensor.ai:443"


def test_cassette_record_and_replay(arun: Any, chain: Any, tmp_path: Path, clock: Any) -> None:
    chain.hashes[1] = "0x" + "11" * 32
    rec = RecordingTransport(chain)
    arun(rec.request("chain_getBlockHash", [1], 1.0))
    p = tmp_path / "c.jsonl"
    assert rec.dump(p) == 1
    replay = CassetteTransport.load(p)
    assert arun(replay.request("chain_getBlockHash", [1], 1.0)) == "0x" + "11" * 32
    with pytest.raises(RpcFatal):
        arun(replay.request("chain_getBlockHash", [2], 1.0))
    assert replay.misses
    err_cas = CassetteTransport([{"method": "chain_getBlockHash", "params": [3], "error": {"code": -32029, "message": "x"}}])
    with pytest.raises(RpcTransient):
        arun(err_cas.request("chain_getBlockHash", [3], 1.0))


# ------------------------------------------------------------------------------------------------ WebSocket
class FakeWs:
    """A websockets-like connection: the server side answers requests from `responder` and can push or drop."""

    def __init__(self, responder: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> None:
        self.q: asyncio.Queue[str | None] = asyncio.Queue()
        self.responder = responder
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, msg: str) -> None:
        if self.closed:
            from websockets.exceptions import ConnectionClosedError
            raise ConnectionClosedError(None, None)
        req = json.loads(msg)
        self.sent.append(req)
        for out in self.responder(req):
            self.q.put_nowait(json.dumps(out))

    def push(self, obj: dict[str, Any]) -> None:
        self.q.put_nowait(json.dumps(obj))

    def drop(self) -> None:
        self.closed = True
        self.q.put_nowait(None)

    def __aiter__(self) -> FakeWs:
        return self

    async def __anext__(self) -> str:
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def close(self) -> None:
        self.drop()


def test_ws_multiplexing_subscription_and_drop(arun: Any) -> None:
    conns: list[FakeWs] = []

    def responder(req: dict[str, Any]) -> list[dict[str, Any]]:
        if req["method"] == "chain_subscribeFinalizedHeads":
            return [{"jsonrpc": "2.0", "id": req["id"], "result": "sub1"}]
        if req["method"] == "chain_getBlockHash":
            return [{"jsonrpc": "2.0", "id": req["id"], "result": f"0x{req['params'][0]:064x}"}]
        if req["method"] == "state_call":
            return [{"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32029, "message": "busy"}}]
        return []

    async def connect(url: str, **kw: Any) -> FakeWs:
        c = FakeWs(responder)
        conns.append(c)
        return c

    async def go() -> None:
        t = WsTransport("wss://lite.example:443", connect=connect)
        a, b = await asyncio.gather(t.request("chain_getBlockHash", [1], 5), t.request("chain_getBlockHash", [2], 5))
        assert (a, b) == ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
        with pytest.raises(RpcTransient):
            await t.request("state_call", ["x", "0x", "0x00"], 5)
        sub: Subscription = await t.subscribe("chain_subscribeFinalizedHeads", [], "chain_unsubscribeFinalizedHeads", 5)
        conns[0].push({"jsonrpc": "2.0", "method": "chain_finalizedHead", "params": {"subscription": "sub1",
                                                                                     "result": {"number": "0x10"}}})
        assert (await sub.__anext__())["number"] == "0x10"
        pending = asyncio.create_task(t.request("system_health", [], 5))     # never answered
        await asyncio.sleep(0)
        conns[0].drop()
        with pytest.raises(RpcTransient):
            await sub.__anext__()
        with pytest.raises(RpcTransient):
            await pending
        # lazily reconnects on the next request
        assert await t.request("chain_getBlockHash", [3], 5) == "0x" + f"{3:064x}"
        assert len(conns) == 2
        await t.aclose()

    arun(go())


def test_ws_timeout_is_transient(arun: Any) -> None:
    async def connect(url: str, **kw: Any) -> FakeWs:
        return FakeWs(lambda req: [])

    async def go() -> None:
        t = WsTransport("wss://x", connect=connect)
        with pytest.raises(RpcTransient):
            await t.request("system_health", [], 0.01)
        await t.aclose()

    arun(go())


def test_ws_connect_failure_is_transient(arun: Any) -> None:
    async def connect(url: str, **kw: Any) -> FakeWs:
        raise OSError("connection refused")

    with pytest.raises(RpcTransient):
        arun(WsTransport("wss://x", connect=connect).request("system_health", [], 1))


# ------------------------------------------------------------------------------------------------ all-or-nothing
def test_no_partial_snapshot_on_failed_chunk(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 9_240_388)
    for n in range(1, 6):
        s.subnet(n, reg_at=8_000_000 + n)
    seen: list[Any] = []
    reader = make_reader(chain, keys_per_call=200, on_raw=lambda *a: seen.append(a))
    calls = {"n": 0}

    def chaos(m: str, p: list[Any]) -> Exception | None:
        if m == "state_queryStorageAt":
            calls["n"] += 1
            if calls["n"] == 3:
                return err(-32029)
            if calls["n"] > 3:
                return RpcTransient("connection reset")
        return None

    chain.chaos = chaos
    with pytest.raises(RpcExhausted):
        arun(reader.snapshot(Block(9_240_388), BlockHash(s.hash), ReadPlan.FULL, None, ()))
    assert seen == []


def test_undecodable_value_fails_whole_snapshot_with_raw(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 9_240_388)
    s.subnet(1, reg_at=8_000_001)
    s.subnet(2, reg_at=8_000_002, tao_in_emission=b"\x01\x02\x03")             # 3 bytes for a u64
    reader = make_reader(chain)
    with pytest.raises(SnapshotDecodeError) as ei:
        arun(reader.snapshot(Block(9_240_388), BlockHash(s.hash), ReadPlan.FULL, None, ()))
    assert ei.value.block == 9_240_388 and ei.value.block_hash == s.hash
    assert "0x010203" in ei.value.raw.values()


def test_pool_from_run_config(fake_chain_cls: Any, clock: Any) -> None:
    from taotrader.core.config import RpcCfg

    cfg = RpcCfg(head_endpoints=("wss://lite.example:443",), archive_endpoints=("https://archive.example/rpc",),
                 rate_per_s=3.0, burst=3, max_concurrency=3, keys_per_call=2_000, timeout_s=12.0)
    made: list[str] = []

    def factory(url: str) -> Any:
        made.append(url)
        return fake_chain_cls(url)

    pool = RpcPool.from_cfg(cfg, keyed_archive_url="https://node.example/rpc?apikey=SECRET", keyed_rate_per_s=10.0,
                            transport_factory=factory, clock=clock, sleep=clock.sleep)
    labels = [ep.label for ep in pool.endpoints(Role.ARCHIVE)]
    assert labels == ["archive-keyed", "https://archive.example/rpc"]          # keyed first; key never in a label
    assert [ep.label for ep in pool.endpoints(Role.HEAD)] == ["wss://lite.example:443"]
    assert all("SECRET" not in lab for lab in pool.stats())
    assert pool.timeout_s == 12.0 and pool.endpoints(Role.ARCHIVE)[0].bucket.rate == 10.0
    assert len(made) == 3 and pool.healthy() == 3 and pool.has(Role.HEAD) and not pool.has(Role.TEST)
    with pytest.raises(ValueError):
        RpcPool.from_urls(archive=("ftp://x",))
