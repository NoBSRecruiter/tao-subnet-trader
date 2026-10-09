"""taotrader/chain/rpc.py - HTTP + WebSocket JSON-RPC with rate limiting, backoff, rotation and breakers (section 6.10).

- One token bucket per endpoint per process (rate 3 req/s, burst 3) and a concurrency cap (<= 3; public OnFinality
  trips at ~8 concurrent). Every request is 1 RU on the endpoint's counter (`RpcPool.stats()`).
- Transient errors: HTTP 429 (honours Retry-After / `retry_after_seconds`), HTTP 5xx, JSON-RPC -32029, -32005,
  -32603, transport errors (connect/read timeouts, WebSocket drops) and truncated bodies. Exponential backoff
  1 -> 60 s with jitter x[0.5, 1.5]; after 2 consecutive transient errors the pool rotates to the next endpoint of
  the role; at most 6 retries per call, then `RpcExhausted`.
- -32004 "Historical work rate limit exceeded": that endpoint's breaker opens for 300 s, then one half-open probe call.
- "State already discarded" (pruned lite node): the read is re-routed to the archive role once.
- Non-retryable: invalid params / method errors (`RpcFatal`) and anything the caller fails to decode.
- Only read-only methods are allowed (an allow-list); this module never signs or submits anything.

Clock, sleep and jitter are injected so chaos tests run on fake time. `CassetteTransport` replays recorded sessions
(tests/fixtures/cassettes/*.jsonl) and `RecordingTransport` records them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from taotrader.core.config import RpcCfg

READ_ONLY_METHODS: Final[frozenset[str]] = frozenset({
    "chain_getBlockHash", "chain_getHeader", "chain_getFinalizedHead", "chain_getBlock",
    "chain_subscribeFinalizedHeads", "chain_unsubscribeFinalizedHeads", "chain_subscribeNewHeads",
    "chain_unsubscribeNewHeads", "state_getRuntimeVersion", "state_subscribeRuntimeVersion",
    "state_unsubscribeRuntimeVersion", "state_queryStorageAt", "state_queryStorage", "state_getKeysPaged",
    "state_getStorage", "state_call", "state_getMetadata", "system_health", "rpc_methods",
})
TRANSIENT_CODES: Final[frozenset[int]] = frozenset({-32029, -32005, -32603})
HISTORICAL_BUDGET_CODE: Final[int] = -32004
DISCARDED_MARKERS: Final[tuple[str, ...]] = ("state already discarded", "unknownblock")


class Role(StrEnum):
    HEAD = "head"          # WS lite/entrypoint nodes: finalized heads, recent per-block reads (~300 blocks of state)
    ARCHIVE = "archive"    # HTTP archive: history, gap fill > 280 blocks, "State already discarded" re-routes
    TEST = "test"          # test.finney (live smoke tests only)


# ------------------------------------------------------------------------------------------------ errors
class RpcError(Exception):
    """Base of every JSON-RPC failure."""


class RpcTransient(RpcError):
    def __init__(self, msg: str, retry_after: float | None = None, code: int | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after
        self.code = code


class HistoricalBudget(RpcTransient):
    """-32004: the endpoint's historical work budget is exhausted (breaker opens for 300 s)."""


class StateDiscarded(RpcError):
    """The node pruned the state of that block (lite node); re-route to the archive role."""


class RpcFatal(RpcError):
    """Non-retryable error (invalid params, unknown method, HTTP 4xx other than 429, disallowed method)."""

    def __init__(self, msg: str, code: int | None = None) -> None:
        super().__init__(msg)
        self.code = code


class RpcExhausted(RpcError):
    """Retries exhausted or no endpoint available for the role."""


def classify_error(err: Mapping[str, Any]) -> RpcError:
    code = err.get("code")
    msg = str(err.get("message", ""))
    data = err.get("data")
    text = f"{msg} {data if data is not None else ''}".lower()
    retry_after: float | None = None
    if isinstance(data, Mapping) and data.get("retry_after_seconds") is not None:
        with contextlib.suppress(TypeError, ValueError):
            retry_after = float(data["retry_after_seconds"])
    icode = int(code) if isinstance(code, int) else None
    if any(m in text for m in DISCARDED_MARKERS):
        return StateDiscarded(f"JSON-RPC {icode}: {msg} {data or ''}".strip())
    if icode == HISTORICAL_BUDGET_CODE:
        return HistoricalBudget(f"JSON-RPC {icode}: {msg}", retry_after=retry_after, code=icode)
    if icode in TRANSIENT_CODES or retry_after is not None:
        return RpcTransient(f"JSON-RPC {icode}: {msg}", retry_after=retry_after, code=icode)
    return RpcFatal(f"JSON-RPC {icode}: {msg} {data or ''}".strip(), code=icode)


def unwrap(body: Any) -> Any:
    """A JSON-RPC response object -> result, raising the classified error."""
    if not isinstance(body, Mapping) or ("result" not in body and "error" not in body):
        raise RpcTransient(f"malformed JSON-RPC response: {str(body)[:200]}")
    err = body.get("error")
    if err is not None:
        raise classify_error(err if isinstance(err, Mapping) else {"message": str(err)})
    return body.get("result")


def _retry_after_header(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def redact_url(url: str) -> str:
    """Endpoint label without query string or credentials (keyed OnFinality URLs carry the key)."""
    u = urlsplit(url)
    host = u.hostname or url
    port = f":{u.port}" if u.port else ""
    path = u.path if (u.path and not u.query and "apikey" not in u.path.lower()) else ""
    return f"{u.scheme}://{host}{port}{path}"


# ------------------------------------------------------------------------------------------------ transports
class Transport(Protocol):
    url: str

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any: ...
    async def aclose(self) -> None: ...


class HttpTransport:
    """JSON-RPC over HTTP POST (httpx). Batch arrays are not used (public OnFinality rejects them)."""

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self._client = client or httpx.AsyncClient(headers={"Content-Type": "application/json"})
        self._own = client is None
        self._id = 0

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}
        try:
            r = await self._client.post(self.url, json=payload, timeout=timeout)
        except httpx.TransportError as e:
            raise RpcTransient(f"{type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise RpcTransient("HTTP 429", retry_after=_retry_after_header(r.headers.get("Retry-After")))
        if r.status_code >= 500:
            raise RpcTransient(f"HTTP {r.status_code}", retry_after=_retry_after_header(r.headers.get("Retry-After")))
        if r.status_code >= 400:
            raise RpcFatal(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            body = json.loads(r.content)
        except ValueError as e:
            raise RpcTransient(f"truncated or invalid JSON body ({len(r.content)} bytes)") from e
        return unwrap(body)

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()


class Subscription:
    """Notifications of one WebSocket subscription. Iteration raises RpcTransient when the connection drops."""

    _END: Final = object()

    def __init__(self, transport: WsTransport, sub_id: str, unsubscribe: str) -> None:
        self._t = transport
        self.sub_id = sub_id
        self._unsub = unsubscribe
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Any:
        item = await self.queue.get()
        if item is Subscription._END:
            self.closed = True
            raise RpcTransient(f"subscription {self.sub_id} ended: connection to {redact_url(self._t.url)} dropped")
        return item

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self._t._subs.pop(self.sub_id, None)
            with contextlib.suppress(RpcError):
                await self._t.request(self._unsub, [self.sub_id], 5.0)


class WsTransport:
    """JSON-RPC over one WebSocket connection with id multiplexing and subscriptions; reconnects lazily."""

    def __init__(self, url: str, connect: Callable[..., Any] | None = None, max_size: int = 64 * 2**20) -> None:
        self.url = url
        self._connect = connect or websockets.connect
        self._max_size = max_size
        self._conn: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subs: dict[str, Subscription] = {}
        self._id = 0
        self._lock = asyncio.Lock()

    async def _ensure(self, timeout: float) -> Any:
        async with self._lock:
            if self._conn is None:
                try:
                    self._conn = await asyncio.wait_for(
                        self._connect(self.url, max_size=self._max_size, ping_interval=20, ping_timeout=20), timeout)
                except (TimeoutError, OSError, WebSocketException) as e:
                    raise RpcTransient(f"ws connect {redact_url(self.url)}: {type(e).__name__}: {e}") from e
                self._reader = asyncio.create_task(self._read_loop(self._conn))
            return self._conn

    async def _read_loop(self, conn: Any) -> None:
        try:
            async for msg in conn:
                try:
                    body = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(body, Mapping) and "id" in body and body.get("id") is not None:
                    fut = self._pending.pop(int(body["id"]), None)
                    if fut is not None and not fut.done():
                        fut.set_result(body)
                elif isinstance(body, Mapping) and "params" in body:
                    p = body["params"]
                    sub = self._subs.get(str(p.get("subscription"))) if isinstance(p, Mapping) else None
                    if sub is not None:
                        sub.queue.put_nowait(p.get("result"))
        except (ConnectionClosed, OSError, WebSocketException):
            pass
        finally:
            self._drop(conn)

    def _drop(self, conn: Any) -> None:
        if self._conn is not conn:
            return
        self._conn = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RpcTransient(f"ws connection to {redact_url(self.url)} dropped"))
        self._pending.clear()
        for sub in self._subs.values():
            sub.queue.put_nowait(Subscription._END)
        self._subs.clear()

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        conn = await self._ensure(timeout)
        self._id += 1
        rid = self._id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": list(params)}))
            body = await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            raise RpcTransient(f"ws timeout after {timeout}s: {method}") from e
        except (ConnectionClosed, OSError, WebSocketException) as e:
            self._drop(conn)
            raise RpcTransient(f"ws send failed: {type(e).__name__}: {e}") from e
        finally:
            self._pending.pop(rid, None)
        return unwrap(body)

    async def subscribe(self, method: str, params: Sequence[Any], unsubscribe: str, timeout: float) -> Subscription:
        sub_id = await self.request(method, params, timeout)
        sub = Subscription(self, str(sub_id), unsubscribe)
        self._subs[str(sub_id)] = sub
        return sub

    async def aclose(self) -> None:
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
            self._drop(conn)
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader


def _canon(method: str, params: Sequence[Any]) -> str:
    return json.dumps([method, list(params)], sort_keys=True, separators=(",", ":"))


class CassetteTransport:
    """Replays a recorded JSON-RPC session. A request not in the cassette is a fatal error (never a network call)."""

    def __init__(self, records: Iterable[Mapping[str, Any]], url: str = "cassette://replay") -> None:
        self.url = url
        self._map: dict[str, Mapping[str, Any]] = {}
        for rec in records:
            self._map[_canon(str(rec["method"]), rec.get("params") or [])] = rec
        self.calls = 0
        self.misses: list[str] = []

    @staticmethod
    def load(path: Path, url: str | None = None) -> CassetteTransport:
        recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return CassetteTransport(recs, url or f"cassette://{path.name}")

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        self.calls += 1
        rec = self._map.get(_canon(method, params))
        if rec is None:
            self.misses.append(_canon(method, params)[:200])
            raise RpcFatal(f"cassette miss: {method} {json.dumps(list(params))[:160]}")
        if rec.get("error") is not None:
            raise classify_error(rec["error"])
        return rec.get("result")

    async def aclose(self) -> None:
        return None


class RecordingTransport:
    """Wraps a transport and records every successful (method, params) -> result for a cassette."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.url = inner.url
        self.records: dict[str, dict[str, Any]] = {}

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        res = await self.inner.request(method, params, timeout)
        self.records[_canon(method, params)] = {"method": method, "params": list(params), "result": res}
        return res

    def dump(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(self.records[k], sort_keys=True, separators=(",", ":")) for k in sorted(self.records)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return len(lines)

    async def aclose(self) -> None:
        await self.inner.aclose()


# ------------------------------------------------------------------------------------------------ rate limiting
_TOKEN_EPS: Final[float] = 1e-9
_MIN_WAIT_S: Final[float] = 1e-3


class TokenBucket:
    """rate tokens/s, capacity `burst`; acquire() waits (on the injected clock/sleep) until a token is available."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("token bucket needs rate > 0 and burst >= 1")
        self.rate = rate
        self.burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._t = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._t) * self.rate)
        self._t = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0 - _TOKEN_EPS:          # float refill can land a hair below 1.0
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                await self._sleep(max((1.0 - self._tokens) / self.rate, _MIN_WAIT_S))


@dataclass
class EndpointStats:
    label: str
    role: Role
    calls: int = 0                  # requests sent = RU consumed (1 RU per call)
    ok: int = 0
    transient: int = 0
    fatal: int = 0
    discarded: int = 0
    breaker_opens: int = 0
    rotations: int = 0              # times the pool rotated away from this endpoint
    quarantined: bool = False
    breaker_open_until: float | None = None
    last_error: str = ""


@dataclass
class Endpoint:
    label: str
    role: Role
    transport: Transport
    bucket: TokenBucket
    sem: asyncio.Semaphore
    stats: EndpointStats
    breaker_until: float | None = None
    probing: bool = False
    quarantined: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def default_transport(url: str) -> Transport:
    scheme = urlsplit(url).scheme
    if scheme in ("ws", "wss"):
        return WsTransport(url)
    if scheme in ("http", "https"):
        return HttpTransport(url)
    raise ValueError(f"unsupported endpoint scheme: {redact_url(url)}")


async def _asleep(s: float) -> None:
    await asyncio.sleep(s)


class RpcPool:
    """Endpoints by role in priority order, with per-endpoint token buckets, concurrency caps, breakers and stats."""

    def __init__(self, endpoints: Sequence[Endpoint], *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = _asleep, rng: random.Random | None = None,
                 timeout_s: float = 30.0, max_retries: int = 6, backoff_base_s: float = 1.0, backoff_cap_s: float = 60.0,
                 breaker_s: float = 300.0, rotate_after: int = 2) -> None:
        self._eps: dict[Role, list[Endpoint]] = {r: [] for r in Role}
        for ep in endpoints:
            self._eps[ep.role].append(ep)
        self._pref: dict[Role, int] = dict.fromkeys(Role, 0)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.breaker_s = breaker_s
        self.rotate_after = rotate_after

    # ------------------------------------------------------------------ construction
    @staticmethod
    def make_endpoint(url: str, role: Role, *, rate_per_s: float = 3.0, burst: int = 3, max_concurrency: int = 3,
                      transport: Transport | None = None, clock: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], Awaitable[None]] = _asleep, label: str | None = None) -> Endpoint:
        lab = label or redact_url(url)
        return Endpoint(label=lab, role=role, transport=transport or default_transport(url),
                        bucket=TokenBucket(rate_per_s, burst, clock, sleep), sem=asyncio.Semaphore(max_concurrency),
                        stats=EndpointStats(label=lab, role=role))

    @classmethod
    def from_urls(cls, *, head: Sequence[str] = (), archive: Sequence[str] = (), test: Sequence[str] = (),
                  rate_per_s: float = 3.0, burst: int = 3, max_concurrency: int = 3, timeout_s: float = 30.0,
                  transport_factory: Callable[[str], Transport] = default_transport,
                  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = _asleep,
                  rng: random.Random | None = None, **kw: Any) -> RpcPool:
        eps = [cls.make_endpoint(u, role, rate_per_s=rate_per_s, burst=burst, max_concurrency=max_concurrency,
                                 transport=transport_factory(u), clock=clock, sleep=sleep)
               for role, urls in ((Role.HEAD, head), (Role.ARCHIVE, archive), (Role.TEST, test)) for u in urls]
        return cls(eps, clock=clock, sleep=sleep, rng=rng, timeout_s=timeout_s, **kw)

    @classmethod
    def from_cfg(cls, cfg: RpcCfg, *, keyed_archive_url: str | None = None, keyed_rate_per_s: float = 10.0,
                 transport_factory: Callable[[str], Transport] = default_transport,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = _asleep,
                 rng: random.Random | None = None) -> RpcPool:
        """Section 6.1 endpoint roles from `[rpc]`. A keyed OnFinality URL (ops.secrets "onfinality") goes first in
        the archive role at its own plan rate; its label never contains the key."""
        eps: list[Endpoint] = []
        if keyed_archive_url:
            eps.append(cls.make_endpoint(keyed_archive_url, Role.ARCHIVE, rate_per_s=keyed_rate_per_s,
                                         burst=max(cfg.burst, int(keyed_rate_per_s)), max_concurrency=cfg.max_concurrency,
                                         transport=transport_factory(keyed_archive_url), clock=clock, sleep=sleep,
                                         label="archive-keyed"))
        for role, urls in ((Role.HEAD, cfg.head_endpoints), (Role.ARCHIVE, cfg.archive_endpoints)):
            for u in urls:
                eps.append(cls.make_endpoint(u, role, rate_per_s=cfg.rate_per_s, burst=cfg.burst,
                                             max_concurrency=cfg.max_concurrency, transport=transport_factory(u),
                                             clock=clock, sleep=sleep))
        return cls(eps, clock=clock, sleep=sleep, rng=rng, timeout_s=cfg.timeout_s)

    # ------------------------------------------------------------------ introspection
    def endpoints(self, role: Role | None = None) -> list[Endpoint]:
        if role is None:
            return [ep for r in Role for ep in self._eps[r]]
        return list(self._eps[role])

    def has(self, role: Role) -> bool:
        return bool(self._eps[role])

    def _available(self, ep: Endpoint) -> bool:
        if ep.quarantined:
            return False
        return ep.breaker_until is None or self._clock() >= ep.breaker_until

    def healthy(self, role: Role | None = None) -> int:
        """Endpoints not quarantined and not breaker-open (HealthObs.healthy_endpoints)."""
        return sum(1 for ep in self.endpoints(role) if self._available(ep))

    def stats(self) -> dict[str, EndpointStats]:
        out: dict[str, EndpointStats] = {}
        for ep in self.endpoints():
            out[ep.label] = replace(ep.stats, quarantined=ep.quarantined, breaker_open_until=ep.breaker_until)
        return out

    def ru_used(self) -> dict[str, int]:
        return {ep.label: ep.stats.calls for ep in self.endpoints()}

    def quarantine(self, label: str, on: bool = True) -> None:
        for ep in self.endpoints():
            if ep.label == label:
                ep.quarantined = on

    # ------------------------------------------------------------------ calls
    def _backoff(self, retry: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(self.backoff_cap_s, retry_after)
        base = min(self.backoff_cap_s, self.backoff_base_s * float(1 << max(0, retry - 1)))
        return base * self._rng.uniform(0.5, 1.5)

    def _rotate(self, role: Role) -> None:
        eps = self._eps[role]
        if eps:
            eps[self._pref[role] % len(eps)].stats.rotations += 1
            self._pref[role] = (self._pref[role] + 1) % len(eps)

    def _pick(self, role: Role) -> tuple[Endpoint | None, float | None]:
        """(endpoint, None) or (None, seconds until the earliest breaker half-opens; None if nothing will)."""
        eps = self._eps[role]
        n = len(eps)
        now = self._clock()
        wait: float | None = None
        for i in range(n):
            ep = eps[(self._pref[role] + i) % n]
            if ep.quarantined:
                continue
            if ep.breaker_until is not None:
                if now < ep.breaker_until:
                    w = ep.breaker_until - now
                    wait = w if wait is None else min(wait, w)
                    continue
                if ep.probing:          # half-open: exactly one probe in flight
                    continue
                ep.probing = True
            return ep, None
        return None, wait

    async def _send(self, ep: Endpoint, method: str, params: Sequence[Any]) -> Any:
        await ep.bucket.acquire()
        async with ep.sem:
            ep.stats.calls += 1
            return await ep.transport.request(method, params, self.timeout_s)

    async def call(self, method: str, params: Sequence[Any] = (), role: Role = Role.ARCHIVE) -> Any:
        """One read with retries, rotation, breakers and the archive re-route. Raises RpcFatal / RpcExhausted /
        StateDiscarded (already on the archive)."""
        if method not in READ_ONLY_METHODS:
            raise RpcFatal(f"{method} is not an allowed read-only method")
        cur = role if self.has(role) else Role.ARCHIVE
        retries = 0
        consecutive = 0
        rerouted = False
        last: RpcError | None = None
        while True:
            ep, wait = self._pick(cur)
            if ep is None:
                if cur is Role.HEAD and self.has(Role.ARCHIVE) and not rerouted:
                    cur, rerouted, consecutive = Role.ARCHIVE, True, 0
                    continue
                retries += 1
                if wait is None or retries > self.max_retries:
                    raise RpcExhausted(f"{method}: no available {cur.value} endpoint ({last})")
                await self._sleep(wait)
                continue
            try:
                res = await self._send(ep, method, params)
            except StateDiscarded as e:
                ep.probing = False
                ep.stats.discarded += 1
                ep.stats.last_error = str(e)[:200]
                if cur is not Role.ARCHIVE and self.has(Role.ARCHIVE) and not rerouted:
                    cur, rerouted, consecutive = Role.ARCHIVE, True, 0
                    continue
                raise
            except RpcFatal as e:
                ep.probing = False
                ep.stats.fatal += 1
                ep.stats.last_error = str(e)[:200]
                raise
            except RpcTransient as e:
                last = e
                ep.stats.transient += 1
                ep.stats.last_error = str(e)[:200]
                if isinstance(e, HistoricalBudget) or ep.probing:
                    if ep.breaker_until is None or self._clock() >= ep.breaker_until:
                        ep.stats.breaker_opens += 1
                    ep.breaker_until = self._clock() + self.breaker_s
                    ep.probing = False
                    self._rotate(cur)
                    consecutive = 0
                else:
                    consecutive += 1
                    if consecutive >= self.rotate_after:
                        self._rotate(cur)
                        consecutive = 0
                retries += 1
                if retries > self.max_retries:
                    raise RpcExhausted(f"{method}: gave up after {retries} attempts ({e})") from e
                await self._sleep(self._backoff(retries, e.retry_after))
                continue
            ep.stats.ok += 1
            if ep.probing or ep.breaker_until is not None:
                ep.breaker_until = None
                ep.probing = False
            return res

    async def call_on(self, label: str, method: str, params: Sequence[Any] = ()) -> Any:
        """One read on a specific endpoint, no retries (provider comparisons)."""
        if method not in READ_ONLY_METHODS:
            raise RpcFatal(f"{method} is not an allowed read-only method")
        for ep in self.endpoints():
            if ep.label == label:
                try:
                    return await self._send(ep, method, params)
                except RpcError as e:
                    ep.stats.last_error = str(e)[:200]
                    raise
        raise RpcFatal(f"no endpoint labelled {label}")

    async def subscribe(self, method: str, params: Sequence[Any], unsubscribe: str,
                        role: Role = Role.HEAD) -> tuple[Endpoint, Subscription]:
        """Open a WS subscription on the first available WS endpoint of the role (rotating on failure)."""
        if method not in READ_ONLY_METHODS:
            raise RpcFatal(f"{method} is not an allowed read-only method")
        errors: list[str] = []
        for _ in range(len(self._eps[role])):
            ep, _wait = self._pick(role)
            if ep is None:
                break
            t = ep.transport
            if not isinstance(t, WsTransport):
                ep.probing = False
                self._rotate(role)
                continue
            try:
                await ep.bucket.acquire()
                ep.stats.calls += 1
                sub = await t.subscribe(method, params, unsubscribe, self.timeout_s)
            except RpcError as e:
                ep.probing = False
                ep.stats.transient += 1
                ep.stats.last_error = str(e)[:200]
                errors.append(f"{ep.label}: {e}")
                self._rotate(role)
                continue
            ep.probing = False
            ep.breaker_until = None
            ep.stats.ok += 1
            return ep, sub
        raise RpcExhausted(f"{method}: no {role.value} endpoint accepted the subscription ({'; '.join(errors)})")

    async def aclose(self) -> None:
        for ep in self.endpoints():
            with contextlib.suppress(Exception):
                await ep.transport.aclose()
