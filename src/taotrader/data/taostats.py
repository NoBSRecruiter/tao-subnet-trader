"""taotrader/data/taostats.py - optional Taostats REST enrichment and cross-checks (WP4; DESIGN.md sections 7.1, 8.10,
12.3; brief section 6.1). NEVER on the decision path: it only writes the lake's ext_* tables.

- Auth: the raw key in the `Authorization` header (no "Bearer"), read through ops.secrets ("taostats": keyring
  taotrader/taostats, env TAOSTATS_API_KEY, or %USERPROFILE%\\.taotrader\\secrets.env). The key lives in a Secret and
  is revealed only while building the request header; it is never logged, never put in a URL and never part of an
  exception message.
- Credit-aware limiter: a token bucket in credits (free tier 5 credits/min; per-endpoint costs are unpublished, so
  every endpoint costs 1 unless `costs` says otherwise) with a running credit counter per endpoint (section 13 Q11:
  probe the real costs with the first key) and the last rate-limit headers seen.
- Errors: 401/403 -> TaostatsAuthError (no retry; unknown /v1 paths also answer 401); 429 -> wait Retry-After, retry;
  5xx and transport errors -> exponential backoff, retry; other 4xx -> TaostatsError.
- Paging: `page` is 1-indexed, limit <= 200, (page - 1) * limit must stay below 1,000,000: `paged()` refuses deeper
  pages (TaostatsDeepPaging) and `walk_blocks()` walks block ranges instead (deep history).
- Rows: Taostats pool history splices netuid generations, so ext rows get their reg_at by BLOCK from the generation
  table (`generation_at`), never by netuid alone (section 8.10). Trade and pool-history field names are UNVERIFIED
  until the first key (brief 6.1 lists the endpoints, not every field), so the parsers accept the documented
  alternatives and skip rows they cannot place.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import httpx

from ..core.units import RAO_PER_TAO
from ..ops.secrets import Secret, get_secret
from .collector import write_block_rows
from .lake import Lake

log = logging.getLogger(__name__)

BASE_URL: Final[str] = "https://api.taostats.io"
FREE_CREDITS_PER_MIN: Final[float] = 5.0
MAX_LIMIT: Final[int] = 200
MAX_OFFSET: Final[int] = 1_000_000
RATE_HEADERS: Final[tuple[str, ...]] = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after",
                                        "x-credits-remaining", "x-credits-used")
P_POOL_HISTORY: Final[str] = "/v1/subnets/pools/history"
P_TRADES: Final[str] = "/v1/subnets/trades"
P_STAKE_EVENTS: Final[str] = "/v1/subnets/stake-events"


class TaostatsError(Exception):
    """A Taostats request failed. Messages carry the status and path, never the key."""


class TaostatsUnavailable(TaostatsError):
    """No Taostats key is configured (the client is optional)."""


class TaostatsAuthError(TaostatsError):
    """401/403: bad or missing key, or an unknown /v1 path (Taostats answers 401, not 404)."""


class TaostatsDeepPaging(TaostatsError):
    """(page - 1) * limit would reach 1,000,000: walk block ranges instead."""


@dataclass(slots=True)
class CreditLimiter:
    """Token bucket in credits plus a credit counter (per endpoint path)."""
    credits_per_min: float = FREE_CREDITS_PER_MIN
    burst: float = FREE_CREDITS_PER_MIN
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    tokens: float = -1.0
    last: float = 0.0
    used: int = 0
    by_path: dict[str, int] = field(default_factory=dict)
    waited_s: float = 0.0

    def __post_init__(self) -> None:
        if self.credits_per_min <= 0 or self.burst <= 0:
            raise ValueError("credit rate and burst must be positive")
        self.tokens = self.burst
        self.last = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.credits_per_min / 60.0)
        self.last = now

    async def acquire(self, path: str, cost: int) -> None:
        if cost > self.burst:
            raise ValueError(f"{path}: cost {cost} exceeds the bucket ({self.burst})")
        self._refill()
        while self.tokens < cost:
            wait = (cost - self.tokens) * 60.0 / self.credits_per_min
            self.waited_s += wait
            await self.sleep(wait)
            self._refill()
        self.tokens -= cost
        self.used += cost
        self.by_path[path] = self.by_path.get(path, 0) + cost


class TaostatsClient:
    """Async Taostats REST client (see the module docstring)."""

    def __init__(self, key: Secret, *, base_url: str = BASE_URL, credits_per_min: float = FREE_CREDITS_PER_MIN,
                 costs: Mapping[str, int] | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_retries: int = 5, timeout_s: float = 30.0, backoff_cap_s: float = 60.0) -> None:
        if not isinstance(key, Secret):
            raise TypeError("the Taostats key must be an ops.secrets.Secret")
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.costs = dict(costs or {})
        self.limiter = CreditLimiter(credits_per_min, credits_per_min, clock, sleep)
        self._sleep = sleep
        self.max_retries = max_retries
        self.backoff_cap_s = backoff_cap_s
        self.last_rate_headers: dict[str, str] = {}
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout_s,
                                       headers={"Accept": "application/json"})

    @classmethod
    def from_secrets(cls, *, env: Mapping[str, str] | None = None, secrets_file: Path | None = None,
                     use_keyring: bool = True, **kw: Any) -> TaostatsClient:
        """The client with the key from ops.secrets; TaostatsUnavailable when no key is configured."""
        key = get_secret("taostats", env=env, secrets_file=secrets_file, use_keyring=use_keyring)
        if key is None:
            raise TaostatsUnavailable("no Taostats key: add it to the keyring (taotrader/taostats) or TAOSTATS_API_KEY")
        return cls(key, **kw)

    def __repr__(self) -> str:
        return f"TaostatsClient(base_url={self.base_url!r}, credits_used={self.limiter.used})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> TaostatsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ requests
    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """One GET with limiter, retries and error mapping; returns the decoded JSON."""
        q = {k: v for k, v in (params or {}).items() if v is not None}
        if any(k.lower() == "authorization" for k in q):
            raise ValueError("the key goes in the Authorization header only, never in the query")
        attempt = 0
        while True:
            await self.limiter.acquire(path, self.costs.get(path, 1))
            try:
                resp = await self._http.get(path, params=q, headers={"Authorization": self._key.reveal()})
            except httpx.TransportError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise TaostatsError(f"GET {path}: transport error after {attempt} attempts ({type(e).__name__})") from None
                await self._sleep(self._backoff(attempt))
                continue
            self.last_rate_headers = {h: resp.headers[h] for h in RATE_HEADERS if h in resp.headers}
            st = resp.status_code
            if st in (401, 403):
                raise TaostatsAuthError(f"GET {path}: HTTP {st} (bad key, no access, or unknown /v1 path)")
            if st == 429 or st >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise TaostatsError(f"GET {path}: HTTP {st} after {attempt} attempts")
                wait = self._retry_after(resp) if st == 429 else None
                log.info("taostats %s: HTTP %d, retry %d", path, st, attempt)
                await self._sleep(wait if wait is not None else self._backoff(attempt))
                continue
            if st >= 400:
                raise TaostatsError(f"GET {path}: HTTP {st}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as e:
                raise TaostatsError(f"GET {path}: response is not JSON") from e

    def _backoff(self, attempt: int) -> float:
        return float(min(self.backoff_cap_s, 2.0 ** attempt))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        v = resp.headers.get("retry-after")
        if v is None:
            return None
        with contextlib.suppress(ValueError):
            return max(0.0, float(v))
        return None

    async def paged(self, path: str, params: Mapping[str, Any] | None = None, *, limit: int = MAX_LIMIT,
                    max_pages: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Every row of a paged endpoint ({data, pagination}). Refuses pages at or beyond the 1,000,000 offset."""
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be in 1..{MAX_LIMIT}")
        page = 1
        while True:
            if (page - 1) * limit >= MAX_OFFSET:
                raise TaostatsDeepPaging(f"{path}: page {page} x limit {limit} reaches the 1,000,000 offset; walk block ranges")
            body = await self.get(path, {**(params or {}), "page": page, "limit": limit})
            data = body.get("data") if isinstance(body, Mapping) else None
            if not isinstance(data, list):
                raise TaostatsError(f"GET {path}: no data array in the response envelope")
            for row in data:
                if isinstance(row, dict):
                    yield row
            pag = body.get("pagination") if isinstance(body, Mapping) else None
            nxt = pag.get("next_page") if isinstance(pag, Mapping) else None
            total = pag.get("total_pages") if isinstance(pag, Mapping) else None
            if not data or (nxt is None and (total is None or page >= int(total))):
                return
            page = int(nxt) if nxt is not None else page + 1
            if max_pages is not None and page > max_pages:
                return

    async def walk_blocks(self, path: str, params: Mapping[str, Any] | None, block_start: int, block_end: int, *,
                          span: int = 7_200, limit: int = MAX_LIMIT) -> AsyncIterator[dict[str, Any]]:
        """Deep history: consecutive inclusive block windows of `span` blocks, each paged."""
        if span <= 0 or block_end < block_start:
            raise ValueError("bad block walk")
        a = block_start
        while a <= block_end:
            z = min(block_end, a + span - 1)
            async for row in self.paged(path, {**(params or {}), "block_start": a, "block_end": z}, limit=limit):
                yield row
            a = z + 1

    # ------------------------------------------------------------------ endpoints
    async def pool_history(self, netuid: int, block_start: int, block_end: int, *, frequency: str = "by_block",
                           span: int = 7_200) -> list[dict[str, Any]]:
        return [r async for r in self.walk_blocks(P_POOL_HISTORY, {"netuid": netuid, "frequency": frequency},
                                                  block_start, block_end, span=span)]

    async def trades(self, netuid: int, block_start: int, block_end: int, *, span: int = 7_200) -> list[dict[str, Any]]:
        """Buys (TAO -> SN{n}) and sells (SN{n} -> TAO) of one subnet (the endpoint has no netuid filter)."""
        out: list[dict[str, Any]] = []
        for frm, to in (("TAO", f"SN{netuid}"), (f"SN{netuid}", "TAO")):
            out += [r async for r in self.walk_blocks(P_TRADES, {"from_name": frm, "to_name": to}, block_start, block_end,
                                                      span=span)]
        return out

    async def stake_events(self, netuid: int, block_start: int, block_end: int, *, trades_only: bool = True,
                           span: int = 7_200) -> list[dict[str, Any]]:
        return [r async for r in self.walk_blocks(P_STAKE_EVENTS, {"netuid": netuid, "action": "all",
                                                                   "trades_only": str(trades_only).lower()},
                                                  block_start, block_end, span=span)]


# ------------------------------------------------------------------------------------------------ ext_* rows
@dataclass(frozen=True, slots=True)
class GenerationSpan:
    netuid: int
    reg_at: int
    start: int              # first block of the generation (reg_at, or first_seen when earlier)
    end: int | None         # removal block (exclusive); None = open


def load_generations(lake: Lake) -> list[GenerationSpan]:
    con = lake.connect()
    try:
        rows = con.execute("SELECT netuid, reg_at, first_seen, end_block FROM v_generation ORDER BY netuid, reg_at").fetchall()
    finally:
        con.close()
    return [GenerationSpan(int(n), int(r), min(int(r), int(f)) if f is not None else int(r), None if e is None else int(e))
            for n, r, f, e in rows]


def generation_at(gens: Sequence[GenerationSpan], netuid: int, block: int) -> int | None:
    """reg_at of the generation of `netuid` alive at `block` (join by block, never by netuid alone)."""
    best: GenerationSpan | None = None
    for g in gens:
        if g.netuid == netuid and g.start <= block and (g.end is None or block < g.end) and (best is None or g.start > best.start):
            best = g
    return None if best is None else best.reg_at


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _account(v: Any) -> str | None:
    if isinstance(v, Mapping):
        x = _first(v, "ss58", "hex")
        return None if x is None else str(x)
    return None if v is None else str(v)


def trade_rows(trades: Sequence[Mapping[str, Any]], netuid: int, gens: Sequence[GenerationSpan]) -> list[dict[str, Any]]:
    """ext_trades rows (VERIFY field names with the first key: block_number|block, from_name/to_name,
    from_amount/to_amount in rao, coldkey {ss58} | str, extrinsic_id). Rows whose block or generation is unknown are
    skipped. seq numbers repeated extrinsic ids within a block (extrinsic_id is not unique)."""
    out: list[dict[str, Any]] = []
    seen: dict[tuple[int, str], int] = {}
    sn = f"SN{netuid}"
    for t in sorted(trades, key=lambda r: (_int(_first(r, "block_number", "block")) or 0, str(_first(r, "extrinsic_id", "id")))):
        block = _int(_first(t, "block_number", "block"))
        frm, to = _first(t, "from_name"), _first(t, "to_name")
        if block is None or {frm, to} != {"TAO", sn}:
            continue
        reg_at = generation_at(gens, netuid, block)
        if reg_at is None:
            continue
        buy = frm == "TAO"
        fa, ta = _int(_first(t, "from_amount")), _int(_first(t, "to_amount"))
        ext = str(_first(t, "extrinsic_id", "id") or "")
        k = (block, ext)
        seq = seen.get(k, 0)
        seen[k] = seq + 1
        out.append({"block": block, "netuid": netuid, "reg_at": reg_at, "side": "buy" if buy else "sell",
                    "coldkey": _account(_first(t, "coldkey", "signer")),
                    "tao_rao": fa if buy else ta, "alpha_rao": ta if buy else fa, "extrinsic_id": ext, "seq": seq})
    return out


def crosscheck_rows(history: Sequence[Mapping[str, Any]], netuid: int, gens: Sequence[GenerationSpan],
                    chain_price_rao: Callable[[int, int], int | None]) -> list[dict[str, Any]]:
    """ext_crosscheck rows: Taostats pool-history price (TAO per alpha, decimal text) vs the chain price at the same
    block (`chain_price_rao(block, netuid)`, e.g. the lake snapshot's spot), per UTC day of the row's timestamp."""
    out: list[dict[str, Any]] = []
    done: set[tuple[dt.date, int]] = set()
    for h in sorted(history, key=lambda r: _int(_first(r, "block_number", "block")) or 0):
        block = _int(_first(h, "block_number", "block"))
        price = _first(h, "price")
        ts = _first(h, "timestamp")
        if block is None or price is None or ts is None:
            continue
        reg_at = generation_at(gens, netuid, block)
        chain = chain_price_rao(block, netuid)
        try:
            ts_rao = int(Decimal(str(price)) * RAO_PER_TAO)
            day = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.UTC).date()
        except (InvalidOperation, ValueError):
            continue
        if reg_at is None or chain is None or chain <= 0 or (day, reg_at) in done:
            continue
        done.add((day, reg_at))
        out.append({"day": day, "netuid": netuid, "reg_at": reg_at, "chain_price_rao": chain, "ts_price_rao": ts_rao,
                    "rel_diff": abs(ts_rao - chain) / chain})
    return out


async def enrich_trades(client: TaostatsClient, lake: Lake, netuids: Sequence[int], block_start: int, block_end: int, *,
                        span: int = 7_200) -> int:
    """Fetch trades of the given subnets into ext_trades (one chunk per era, series 'taostats'). Returns rows."""
    gens = load_generations(lake)
    rows: list[dict[str, Any]] = []
    for n in sorted(set(netuids)):
        rows += trade_rows(await client.trades(n, block_start, block_end, span=span), n, gens)
    if rows:
        write_block_rows(lake, "ext_trades", rows, series="taostats")
    return len(rows)


def write_crosscheck(lake: Lake, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """ext_crosscheck is not block-keyed: one chunk per call, first/last = the day ordinals, series = a content hash
    (calls accumulate; an identical call is a no-op)."""
    if not rows:
        return None
    days = [int(r["day"].toordinal()) for r in rows]
    canon = json.dumps([sorted((k, str(v)) for k, v in r.items()) for r in rows], separators=(",", ":"))
    series = "ts" + hashlib.sha256(canon.encode()).hexdigest()[:20]
    return lake.write_rows("ext_crosscheck", rows, first_block=min(days), last_block=max(days), series=series).path
