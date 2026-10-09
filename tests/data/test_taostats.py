"""WP4 Taostats client tests (DESIGN.md sections 11 WP4, 12.3; brief 6.1). Mocked HTTP only (httpx.MockTransport):
the user has no key yet. The key comes from ops.secrets and never appears in logs, URLs, reprs or exceptions."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from taotrader.data import taostats as ts
from taotrader.data.lake import Lake
from taotrader.data.refine import write_dimension
from taotrader.ops.secrets import Secret

KEY = "tao-0123456789abcdef:SIGNATUREsecretmaterial"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def client(handler: Callable[[httpx.Request], httpx.Response], clock: Clock | None = None, **kw: Any) -> ts.TaostatsClient:
    c = clock or Clock()
    return ts.TaostatsClient(Secret("taostats", KEY, "test"), transport=httpx.MockTransport(handler), clock=c, sleep=c.sleep, **kw)


def envelope(rows: list[dict[str, Any]], page: int, total_pages: int) -> dict[str, Any]:
    return {"data": rows, "pagination": {"current_page": page, "per_page": len(rows), "total_items": 0,
                                         "total_pages": total_pages, "next_page": page + 1 if page < total_pages else None,
                                         "prev_page": page - 1 or None}}


def test_raw_authorization_header_and_paging() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        page = int(req.url.params["page"])
        return httpx.Response(200, json=envelope([{"block_number": page * 10, "i": page}], page, 3))

    async def go() -> list[dict[str, Any]]:
        async with client(handler) as c:
            return [r async for r in c.paged("/v1/subnets/pools/history", {"netuid": 7, "x": None})]

    rows = asyncio.run(go())
    assert [r["i"] for r in rows] == [1, 2, 3]
    assert all(r.headers["authorization"] == KEY for r in seen)                 # raw key, no "Bearer"
    assert all(KEY not in str(r.url) and "x" not in r.url.params for r in seen)
    assert seen[0].url.params["limit"] == "200" and seen[0].url.params["netuid"] == "7"


def test_credit_limiter_paces_the_free_tier() -> None:
    clock = Clock()
    c = client(lambda req: httpx.Response(200, json={"data": [], "pagination": {}}), clock, credits_per_min=5,
               costs={"/v1/expensive": 2})

    async def go() -> None:
        for _ in range(7):
            await c.get("/v1/cheap")
        await c.get("/v1/expensive")
        await c.aclose()

    asyncio.run(go())
    assert c.limiter.used == 9 and c.limiter.by_path == {"/v1/cheap": 7, "/v1/expensive": 2}
    assert clock.t == pytest.approx(48.0)                                        # 5 free, then 12 s per credit
    with pytest.raises(ValueError):
        asyncio.run(c.limiter.acquire("/v1/x", 6))


def test_429_honours_retry_after_and_5xx_backs_off() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7", "x-ratelimit-remaining": "0"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True}, headers={"x-ratelimit-remaining": "4"})

    clock = Clock()
    c = client(handler, clock, credits_per_min=600)
    assert asyncio.run(c.get("/v1/x")) == {"ok": True}
    assert clock.slept[:2] == [7.0, 4.0] and c.last_rate_headers == {"x-ratelimit-remaining": "4"}
    always = client(lambda req: httpx.Response(500), Clock(), max_retries=2)
    with pytest.raises(ts.TaostatsError, match="HTTP 500 after 3 attempts"):
        asyncio.run(always.get("/v1/x"))


def test_transport_errors_are_retried_then_raised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(ts.TaostatsError, match="transport error"):
        asyncio.run(client(handler, max_retries=1).get("/v1/x"))


def test_auth_errors_are_not_retried_and_never_leak_the_key(caplog: pytest.LogCaptureFixture) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text=f"bad key {req.headers['authorization']}")   # a hostile echo

    c = client(handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ts.TaostatsAuthError) as ei:
        asyncio.run(c.get("/v1/unknown/path"))
    assert calls["n"] == 1
    assert KEY not in str(ei.value) and KEY not in repr(ei.value) and KEY not in repr(c)
    assert KEY not in caplog.text
    with pytest.raises(ValueError):
        asyncio.run(c.get("/v1/x", {"authorization": KEY}))
    with pytest.raises(TypeError):
        ts.TaostatsClient(KEY)                                                   # type: ignore[arg-type]


def test_key_read_through_ops_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    nofile = tmp_path / "none.env"
    with pytest.raises(ts.TaostatsUnavailable):
        ts.TaostatsClient.from_secrets(env={}, secrets_file=nofile, use_keyring=False)
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json={})

    c = ts.TaostatsClient.from_secrets(env={"TAOSTATS_API_KEY": KEY}, secrets_file=nofile, use_keyring=False,
                                       transport=httpx.MockTransport(handler))
    asyncio.run(c.get("/v1/x"))
    assert seen == [KEY] and KEY not in caplog.text
    f = tmp_path / "secrets.env"
    f.write_text(f"TAOSTATS_API_KEY={KEY}\n", encoding="utf-8")
    c2 = ts.TaostatsClient.from_secrets(env={}, secrets_file=f, use_keyring=False, transport=httpx.MockTransport(handler))
    asyncio.run(c2.get("/v1/x"))
    assert seen == [KEY, KEY] and KEY not in caplog.text


def test_deep_paging_refused_and_block_walk() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        page = int(p["page"])
        return httpx.Response(200, json=envelope([{"a": int(p.get("block_start", 0)), "z": int(p.get("block_end", 0))}],
                                                 page, 10**6))

    c = client(handler, credits_per_min=10**6)

    async def deep() -> int:
        n = 0
        async for _ in c.paged("/v1/x", limit=200):
            n += 1
        return n

    with pytest.raises(ts.TaostatsDeepPaging):
        asyncio.run(deep())

    def single(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        return httpx.Response(200, json=envelope([{"a": int(p["block_start"]), "z": int(p["block_end"])}], 1, 1))

    c2 = client(single, credits_per_min=10**6)

    async def walk() -> list[dict[str, Any]]:
        return [r async for r in c2.walk_blocks("/v1/x", {}, 100, 349, span=100)]

    assert [(r["a"], r["z"]) for r in asyncio.run(walk())] == [(100, 199), (200, 299), (300, 349)]
    async def bad_limit() -> None:
        async for _ in c2.paged("/v1/x", limit=500):
            pass

    with pytest.raises(ValueError):
        asyncio.run(bad_limit())


GENS = [ts.GenerationSpan(116, 8_294_750, 8_294_750, 9_210_610), ts.GenerationSpan(116, 9_210_632, 9_210_632, None)]


def test_generation_join_by_block() -> None:
    assert ts.generation_at(GENS, 116, 9_210_609) == 8_294_750
    assert ts.generation_at(GENS, 116, 9_210_620) is None                       # removed, not yet re-added
    assert ts.generation_at(GENS, 116, 9_210_632) == 9_210_632
    assert ts.generation_at(GENS, 5, 9_210_632) is None


def test_trade_rows_split_generations_and_sequence_duplicates() -> None:
    trades: list[dict[str, Any]] = [
        {"block_number": 9_210_600, "from_name": "TAO", "to_name": "SN116", "from_amount": "1000000000",
         "to_amount": "750000000000", "coldkey": {"ss58": "5Abc"}, "extrinsic_id": "9210600-0004"},
        {"block_number": 9_210_600, "from_name": "TAO", "to_name": "SN116", "from_amount": "2000000000",
         "to_amount": "1500000000000", "coldkey": "5Def", "extrinsic_id": "9210600-0004"},
        {"block_number": 9_210_700, "from_name": "SN116", "to_name": "TAO", "from_amount": "5000", "to_amount": "7",
         "coldkey": None, "extrinsic_id": "9210700-0001"},
        {"block_number": 9_210_620, "from_name": "TAO", "to_name": "SN116", "from_amount": "1", "to_amount": "1"},
        {"block_number": 9_210_700, "from_name": "TAO", "to_name": "SN5", "from_amount": "1", "to_amount": "1"},
        {"from_name": "TAO", "to_name": "SN116"},
    ]
    rows = ts.trade_rows(trades, 116, GENS)
    assert [(r["block"], r["reg_at"], r["side"], r["seq"]) for r in rows] == [
        (9_210_600, 8_294_750, "buy", 0), (9_210_600, 8_294_750, "buy", 1), (9_210_700, 9_210_632, "sell", 0)]
    assert rows[0]["tao_rao"] == 10**9 and rows[0]["alpha_rao"] == 750 * 10**9 and rows[0]["coldkey"] == "5Abc"
    assert rows[2]["tao_rao"] == 7 and rows[2]["alpha_rao"] == 5_000 and rows[2]["coldkey"] is None


def test_enrich_and_crosscheck_write_ext_tables(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        if p["from_name"] == "TAO":
            rows = [{"block_number": 9_210_600, "from_name": "TAO", "to_name": "SN116", "from_amount": "1000000000",
                     "to_amount": "5", "coldkey": "5Abc", "extrinsic_id": "e1"}]
        else:
            rows = [{"block_number": 9_210_700, "from_name": "SN116", "to_name": "TAO", "from_amount": "9",
                     "to_amount": "3", "coldkey": "5Xyz", "extrinsic_id": "e2"}]
        return httpx.Response(200, json=envelope(rows, 1, 1))

    gens = [{"netuid": 116, "reg_at": r, "queued_block": None, "added_block": r, "start_call_block": None, "first_seen": r,
             "last_seen": r, "end_block": e, "end_kind": "pruned" if e else "open", "end_refined": False, "lock_amount": None,
             "seed_price_rao": None, "seed_anomaly": None, "pre_end_tao": None, "pre_end_alpha_in": None,
             "pre_end_alpha_out": None, "pre_end_protocol": None, "pre_end_escrow": None, "pre_end_total_staked": None,
             "observed_payout_ratio": None} for r, e in ((8_294_750, 9_210_610), (9_210_632, None))]
    with Lake(tmp_path / "lake") as lake:
        write_dimension(lake, "generation", gens, "reg_at")
        assert ts.load_generations(lake) == GENS
        c = client(handler, credits_per_min=10**6)
        n = asyncio.run(ts.enrich_trades(c, lake, [116], 9_210_000, 9_211_000, span=10_000))
        assert n == 2
        hist = [{"block_number": 9_216_000, "price": "0.00135", "timestamp": "2026-10-05T10:02:36.000Z"},
                {"block_number": 9_216_300, "price": "0.00140", "timestamp": "2026-10-05T11:02:36.000Z"},
                {"block_number": 9_210_620, "price": "0.001", "timestamp": "2026-10-04T10:00:00Z"}]
        xs = ts.crosscheck_rows(hist, 116, ts.load_generations(lake), lambda b, n: 1_349_000)
        assert xs == [{"day": dt.date(2026, 10, 5), "netuid": 116, "reg_at": 9_210_632, "chain_price_rao": 1_349_000,
                       "ts_price_rao": 1_350_000, "rel_diff": 1_000 / 1_349_000}]
        p = ts.write_crosscheck(lake, xs)
        assert p is not None and ts.write_crosscheck(lake, xs) == p and ts.write_crosscheck(lake, []) is None
        con = lake.connect()
        try:
            got = con.execute("SELECT block, reg_at, side, coldkey, tao_rao, alpha_rao FROM v_ext_trades ORDER BY block").fetchall()
            cx = con.execute("SELECT day, reg_at, ts_price_rao FROM v_ext_crosscheck").fetchall()
        finally:
            con.close()
    assert got == [(9_210_600, 8_294_750, "buy", "5Abc", 10**9, 5), (9_210_700, 9_210_632, "sell", "5Xyz", 3, 9)]
    assert cx == [(dt.date(2026, 10, 5), 9_210_632, 1_350_000)]
