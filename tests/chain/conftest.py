"""Shared fixtures for the chain reader tests (WP1).

- `FakeChain`: an in-memory JSON-RPC Transport serving block hashes, runtime versions, storage
  (state_queryStorageAt / state_getKeysPaged / state_getStorage), runtime-API results and headers, with a chaos hook.
- `state(chain, block, spec)`: a StateBuilder that writes registry rows (chain/items.py) as raw SCALE bytes.
- `make_reader(chain, **kw)`: a JsonRpcChainReader over one fake archive endpoint on fake time (no sleeping).
- `cassette(name)`: path of a recorded session in tests/fixtures/cassettes.
Test modules cannot import each other (importlib mode), so helpers are exposed as fixtures.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain.hashing import account, to_hex
from taotrader.chain.items import Row
from taotrader.chain.reader import JsonRpcChainReader
from taotrader.chain.rpc import Endpoint, Role, RpcPool, Transport, classify_error

CASSETTES = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s
        await asyncio.sleep(0)


class FakeChain:
    """In-memory node. `chaos(method, params) -> Exception | None` runs before every request."""

    def __init__(self, url: str = "fake://archive") -> None:
        self.url = url
        self.hashes: dict[int, str] = {}
        self.numbers: dict[str, int] = {}
        self.versions: dict[str, tuple[int, int]] = {}
        self.storage: dict[str, dict[str, str | None]] = {}
        self.rt: dict[tuple[str, str, str], str] = {}
        self.headers: dict[str, dict[str, Any]] = {}
        self.finalized: str | None = None
        self.log: list[tuple[str, list[Any]]] = []
        self.chaos: Callable[[str, list[Any]], BaseException | None] | None = None
        self.mutate: Callable[[str, list[Any], Any], Any] | None = None

    async def request(self, method: str, params: Sequence[Any], timeout: float) -> Any:
        p = list(params)
        self.log.append((method, p))
        await asyncio.sleep(0)
        if self.chaos is not None:
            err = self.chaos(method, p)
            if err is not None:
                raise err
        res = self._dispatch(method, p)
        return res if self.mutate is None else self.mutate(method, p, res)

    def calls(self, method: str) -> list[list[Any]]:
        return [p for m, p in self.log if m == method]

    def _dispatch(self, method: str, p: list[Any]) -> Any:
        if method == "chain_getBlockHash":
            arg = p[0]
            if isinstance(arg, list):
                return [self.hashes.get(int(b)) for b in arg]
            return self.hashes.get(int(arg))
        if method == "state_getRuntimeVersion":
            spec, tx = self.versions[p[0]]
            return {"specVersion": spec, "transactionVersion": tx, "specName": "node-subtensor"}
        if method == "state_queryStorageAt":
            keys, h = p[0], p[1]
            st = self.storage.get(h)
            if st is None:
                raise classify_error({"code": 4003, "message": "Client error: UnknownBlock: State already discarded"})
            return [{"block": h, "changes": [[k, st.get(k)] for k in keys]}]
        if method == "state_getStorage":
            return self.storage[p[1]].get(p[0])
        if method == "state_getKeysPaged":
            pre, count, start, h = p
            keys = sorted(k for k, v in self.storage[h].items() if k.startswith(pre) and v is not None)
            if start is not None:
                keys = [k for k in keys if k > start]
            return keys[:count]
        if method == "state_call":
            key = (p[0], p[1], p[2])
            if key not in self.rt:
                raise classify_error({"code": -32000, "message": f"Execution failed: {p[0]} not found"})
            return self.rt[key]
        if method == "chain_getHeader":
            h = p[0] if p else self.finalized
            return self.headers[str(h)]
        if method == "chain_getFinalizedHead":
            return self.finalized
        raise classify_error({"code": -32601, "message": f"Method not found: {method}"})

    async def aclose(self) -> None:
        return None


def _le(v: int, n: int, signed: bool = False) -> bytes:
    return int(v).to_bytes(n, "little", signed=signed)


def enc_fixed(v: Decimal | int, frac: int, signed: bool = False) -> bytes:
    raw = v if isinstance(v, int) else int(Decimal(v) * (2**frac))
    return _le(raw, 16, signed)


def enc_safefloat(m: int, e: int) -> bytes:
    return _le(m, 16) + _le(e, 8, True)


def block_hash_of(block: int, salt: str = "") -> str:
    return "0x" + hashlib.blake2b(f"{salt}{block}".encode(), digest_size=32).hexdigest()


class StateBuilder:
    """Raw storage of one block. Encoded values are written for registry rows by field name."""

    def __init__(self, chain: FakeChain, block: int, spec: int = 475, tx: int = 1, salt: str = "") -> None:
        self.chain = chain
        self.block = block
        self.hash = block_hash_of(block, salt)
        chain.hashes[block] = self.hash
        chain.numbers[self.hash] = block
        chain.versions[self.hash] = (spec, tx)
        self.st: dict[str, str | None] = {}
        chain.storage[self.hash] = self.st
        chain.headers[self.hash] = {"number": hex(block), "parentHash": block_hash_of(block - 1, salt),
                                    "stateRoot": "0x" + "11" * 32, "extrinsicsRoot": "0x" + "22" * 32,
                                    "digest": {"logs": []}}
        self.glob(system_number=_le(block, 4), timestamp_ms=_le(1_759_900_000_000 + 12_000 * block, 8),
                  total_issuance=_le(11_597_622_104_454_806, 8), moving_alpha=enc_fixed(1_288_490, 32, True),
                  gate_bar=enc_fixed(152_412_446_590_000_000, 64), tao_weight=_le(3_320_413_933_267_719_290, 8),
                  root_tao=_le(5_454_229_642_958_192, 8), subnet_limit=_le(128, 2), immunity_period=_le(864_000, 8),
                  network_rate_limit=_le(14_400, 8), last_lock_cost=_le(653_019_966_758, 8),
                  min_lock_cost=_le(10**9, 8), lock_reduction_interval=_le(115_200, 8),
                  last_reg_block=_le(9_210_610, 8), tao_in_refund_block=_le(8_334_450, 8),
                  nominator_min_factor=_le(10_000_000, 8), cleanup_queue_len=b"\x00")
        self.rt("SubnetInfoRuntimeApi_get_block_emission", "0x", _le(500_000_000, 8))

    def clone(self, block: int, spec: int | None = None, tx: int = 1) -> StateBuilder:
        """The state of a later block: every raw value copied, System.Number/Timestamp advanced."""
        nxt = StateBuilder(self.chain, block, spec=spec if spec is not None else self.chain.versions[self.hash][0], tx=tx)
        nxt.st.update(self.st)
        nxt.glob(system_number=_le(block, 4), timestamp_ms=_le(1_759_900_000_000 + 12_000 * block, 8))
        for key, val in list(self.chain.rt.items()):
            if key[2] == self.hash:
                self.chain.rt[(key[0], key[1], nxt.hash)] = val
        return nxt

    def put(self, row: Row, value: bytes | None, **parts: Any) -> None:
        self.st[to_hex(row.key(**parts))] = None if value is None else to_hex(value)

    def glob(self, **fields: bytes | None) -> None:
        for f, v in fields.items():
            self.put(it.GLOBAL[f], v)

    def rt(self, method: str, args_hex: str, result: bytes) -> None:
        self.chain.rt[(method, args_hex, self.hash)] = to_hex(result)

    def subnet(self, n: int, *, added: bool | None = True, reg_at: int = 8_000_000, tao: int = 587_199_047_950,
               alpha_in: int = 435_539_509_978_376, quote: int | None = 499_999_964_641_764_870,
               moving_price: Decimal | int = 5_834_416, first_emission: int | None = 8_000_600,
               owner_hk: str | None = None, owner_ck: str | None = None, **raw: bytes | None) -> None:
        S = it.SUBNET
        if added is not None:
            self.put(S["added"], b"\x01" if added else b"\x00", netuid=n)
        self.put(S["reg_at"], _le(reg_at, 8), netuid=n)
        self.put(S["tao"], _le(tao, 8), netuid=n)
        self.put(S["alpha_in"], _le(alpha_in, 8), netuid=n)
        self.put(S["moving_price"], enc_fixed(moving_price, 32, True), netuid=n)
        self.put(S["subtoken_enabled"], b"\x01", netuid=n)
        self.put(S["alpha_out"], _le(630_980_000_000_000, 8), netuid=n)
        self.put(S["last_epoch_block"], _le(reg_at + 720, 8), netuid=n)
        if quote is not None:
            self.put(S["w_quote_e18"], _le(quote, 8), netuid=n)
        if first_emission is not None:
            self.put(S["first_emission_block"], _le(first_emission, 8), netuid=n)
        if owner_hk is not None:
            self.put(S["owner_hotkey"], account(owner_hk), netuid=n)
        if owner_ck is not None:
            self.put(S["owner_coldkey"], account(owner_ck), netuid=n)
        for f, v in raw.items():
            self.put(S[f], v, netuid=n)

    def hotkey(self, n: int, hk: str, *, total_alpha: int, shares: tuple[int, int] | None = None,
               take: int | None = None, dividend: int | None = None, shares_v1: int | None = None) -> None:
        H = it.HOTKEY
        self.put(H["total_alpha"], _le(total_alpha, 8), netuid=n, hotkey=hk)
        if shares is not None:
            self.put(H["shares_v2"], enc_safefloat(*shares), netuid=n, hotkey=hk)
        if shares_v1 is not None:
            self.put(H["shares_v1"], _le(shares_v1, 16), netuid=n, hotkey=hk)
        if take is not None:
            self.put(H["take_u16"], _le(take, 2), hotkey=hk)
        if dividend is not None:
            self.put(H["last_dividend"], _le(dividend, 8), netuid=n, hotkey=hk)

    def owner_position(self, n: int, hk: str, ck: str, *, v2: tuple[int, int] | None = None,
                       legacy_raw: int | None = None) -> None:
        if v2 is not None:
            self.put(it.OWNER["owner_shares_v2"], enc_safefloat(*v2), netuid=n, hotkey=hk, coldkey=ck)
        if legacy_raw is not None:
            self.put(it.OWNER["owner_shares_legacy"], _le(legacy_raw, 16), netuid=n, hotkey=hk, coldkey=ck)


def load_golden_snapshot(chain: FakeChain, snap: dict[str, Any]) -> str:
    """Serve one golden capture (WP0 layout) from the fake node: storage, bulk per-netuid reads, runtime-API results.
    Keys the capture did not read are absent (null), so only captured fields are meaningful."""
    h = str(snap["block_hash"])
    b = int(snap["block"])
    chain.hashes[b] = h
    chain.numbers[h] = b
    chain.versions[h] = (int(snap["spec_version"]), int(snap["transaction_version"]))
    st = chain.storage.setdefault(h, {})
    for e in snap.get("storage", []):
        st[str(e["key"])] = e["value"]
    rows = {r.name: r for r in it.SUBNET_ROWS}
    for item, d in snap.get("storage_by_netuid", {}).items():
        row = rows[item]
        for n, v in enumerate(d["values"]):
            st[to_hex(row.key(netuid=n))] = v
    for r in snap.get("runtime_api", []):
        if r.get("result") is not None:
            chain.rt[(str(r["method"]), str(r["args_hex"]), h)] = str(r["result"])
    return h


def make_pool_over(transports: Sequence[tuple[Transport, Role]], clock: FakeClock, **kw: Any) -> RpcPool:
    eps: list[Endpoint] = [RpcPool.make_endpoint(t.url, role, rate_per_s=kw.pop("rate", 1000.0), burst=kw.pop("burst", 1000),
                                                 max_concurrency=3, transport=t, clock=clock, sleep=clock.sleep,
                                                 label=f"{role.value}{i}")
                           for i, (t, role) in enumerate(transports)]
    import random
    return RpcPool(eps, clock=clock, sleep=clock.sleep, rng=random.Random(7), **kw)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def chain() -> FakeChain:
    return FakeChain()


@pytest.fixture
def state() -> Callable[..., StateBuilder]:
    return StateBuilder


@pytest.fixture
def fake_chain_cls() -> type[FakeChain]:
    return FakeChain


@pytest.fixture
def make_rpc_pool(clock: FakeClock) -> Callable[..., RpcPool]:
    def build(*transports: tuple[Transport, Role], **kw: Any) -> RpcPool:
        return make_pool_over(transports, clock, **kw)
    return build


@pytest.fixture
def make_reader(clock: FakeClock) -> Callable[..., JsonRpcChainReader]:
    def build(chain: FakeChain, **kw: Any) -> JsonRpcChainReader:
        pool = make_pool_over([(chain, Role.ARCHIVE)], clock)
        kw.setdefault("provider_check_every", None)
        return JsonRpcChainReader(pool, **kw)
    return build


@pytest.fixture
def golden_node() -> Callable[[FakeChain, dict[str, Any]], str]:
    return load_golden_snapshot


@pytest.fixture
def enc() -> dict[str, Callable[..., bytes]]:
    return {"le": _le, "fixed": enc_fixed, "safefloat": enc_safefloat}


@pytest.fixture(scope="session")
def cassette_dir() -> Path:
    return CASSETTES


@pytest.fixture(scope="session")
def cassette_expected() -> Callable[[str], dict[str, Any]]:
    def load(name: str) -> dict[str, Any]:
        return dict(json.loads((CASSETTES / name).read_text(encoding="utf-8")))
    return load


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def arun() -> Callable[[Any], Any]:
    """asyncio.run (pytest-asyncio is not a dependency)."""
    return run
