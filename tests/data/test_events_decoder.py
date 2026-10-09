"""WP4 optional System.Events decoder tests (scalecodec; DESIGN.md section 11 WP4, section 13 Q21/Q23): decoding with
the runtime's own metadata (golden spec-475 metadata), the stake / perpetual-lock extractors, owner lock
perpetual -> decaying detection, and the era-A fee measurement maths. Event blobs are SCALE-encoded here from the same
metadata, so no network is needed; the network tests decode real blocks."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.core.units import BlockHash
from taotrader.data import events_decoder as ed

scalecodec = pytest.importorskip("scalecodec")

A1 = "0x" + "11" * 32
A2 = "0x" + "22" * 32
OWNER = "0x" + "33" * 32


@pytest.fixture(scope="module")
def decoder(golden: Callable[[str], dict[str, Any]]) -> ed.EventsDecoder:
    return ed.EventsDecoder(golden("metadata_spec475_9240388")["metadata"])


def encode(dec: ed.EventsDecoder, records: Sequence[tuple[Any, str, str, Any]]) -> str:
    """(phase, pallet, event, attributes) -> System.Events hex, through the decoder's own scalecodec registry."""
    value = [{"phase": ph, "event": {pallet: {name: attrs}}, "topics": []} for ph, pallet, name, attrs in records]
    obj = dec._rc.create_scale_object(dec.events_type, metadata=dec._md)
    return str(obj.encode(value).to_hex())


RECORDS = [
    ("Initialization", "Balances", "Issued", {"amount": 500_000_000}),
    ({"ApplyExtrinsic": 2}, "SubtensorModule", "StakeAdded", (A1, A2, 649_900_000, 21_944_987_252, 8, 327_255)),
    ({"ApplyExtrinsic": 3}, "SubtensorModule", "StakeRemoved", (A2, A1, 1_000, 2_000, 9, 0)),
    ({"ApplyExtrinsic": 4}, "SubtensorModule", "PerpetualLockUpdated", {"coldkey": OWNER, "netuid": 92, "enabled": False}),
    ({"ApplyExtrinsic": 5}, "SubtensorModule", "PerpetualLockUpdated", {"coldkey": A1, "netuid": 92, "enabled": False}),
    ({"ApplyExtrinsic": 6}, "SubtensorModule", "PerpetualLockUpdated", {"coldkey": OWNER, "netuid": 47, "enabled": True}),
    ({"ApplyExtrinsic": 7}, "SubtensorModule", "RootClaimed", {"coldkey": A1, "tao": 123_456}),
    ({"ApplyExtrinsic": 8}, "SubtensorModule", "BasketClaimed", {"hotkey": A2, "coldkey": A1, "tao": 7_890}),
]


def test_decoder_knows_the_runtime_events(decoder: ed.EventsDecoder) -> None:
    assert decoder.events_type.startswith("scale_info::")
    for name in ("StakeAdded", "StakeRemoved", "PerpetualLockUpdated", "NetworkRemoved", "NetworkRegistrationQueued"):
        assert ("SubtensorModule", name) in decoder.event_names
    assert decoder.decode(None, 1) == () and decoder.decode("0x00", 1) == ()


def test_decode_and_extract(decoder: ed.EventsDecoder) -> None:
    evs = decoder.decode(encode(decoder, RECORDS), 9_240_388)
    assert [(e.phase, e.extrinsic_idx, e.pallet, e.name) for e in evs] == [
        ("Initialization", None, "Balances", "Issued"), ("ApplyExtrinsic", 2, "SubtensorModule", "StakeAdded"),
        ("ApplyExtrinsic", 3, "SubtensorModule", "StakeRemoved"),
        ("ApplyExtrinsic", 4, "SubtensorModule", "PerpetualLockUpdated"),
        ("ApplyExtrinsic", 5, "SubtensorModule", "PerpetualLockUpdated"),
        ("ApplyExtrinsic", 6, "SubtensorModule", "PerpetualLockUpdated"),
        ("ApplyExtrinsic", 7, "SubtensorModule", "RootClaimed"), ("ApplyExtrinsic", 8, "SubtensorModule", "BasketClaimed")]
    assert ed.claim_events(evs) == [ed.ClaimEvent(9_240_388, "root", A1, None, 123_456),
                                    ed.ClaimEvent(9_240_388, "basket", A1, A2, 7_890)]
    st = ed.stake_events(evs)
    assert st == [ed.StakeEvent(9_240_388, 2, "add", A1, A2, 649_900_000, 21_944_987_252, 8, 327_255),
                  ed.StakeEvent(9_240_388, 3, "remove", A2, A1, 1_000, 2_000, 9, 0)]
    locks = ed.perpetual_lock_updates(evs)
    assert [(c.coldkey, c.netuid, c.enabled) for c in locks] == [(OWNER, 92, False), (A1, 92, False), (OWNER, 47, True)]
    alerts = ed.owner_lock_alerts(locks, {92: OWNER.upper().replace("0X", "0x"), 47: OWNER})
    assert alerts == [ed.OwnerLockAlert(9_240_388, 92, OWNER)]               # only the owner switching to decaying


def test_fee_sample_maths_on_era_a_observations() -> None:
    """Values read at block 5,500,002 (spec 261): StakeAdded on SN19 and StakeRemoved on SN1, flat fee 50,000 rao."""
    add = ed.StakeEvent(5_500_002, 1, "add", A1, A2, 109_950_000, 1_667_375_359, 19, 50_000)
    s = ed.fee_sample(add, 261, 23_507_534_031_585, 356_486_581_230_454)
    assert s is not None and s.residual_rel < Decimal("1e-8")                 # the pool took the event's TAO: no % fee
    assert abs(s.implied_fee_rao - 50_000) < 1
    rem = ed.StakeEvent(5_500_002, 1, "remove", A1, A2, 97_398_505, 1_623_515_312, 1, 50_000)
    r = ed.fee_sample(rem, 261, 18_530_934_051_308, 308_731_397_441_530)
    assert r is not None and r.residual_rel < Decimal("1e-6") and abs(r.implied_fee_rao - 50_000) < 100
    summary = ed.summarize_fee([s, r])
    assert summary["fee_fields"] == [50_000] and summary["specs"] == [261]
    assert Decimal(summary["max_proportional_rate_u16"]) < Decimal("0.1")       # < 0.1 / 65535: no proportional fee
    assert ed.fee_sample(ed.StakeEvent(1, 1, "remove", A1, A2, 1, 10, 1, 0), 261, 5, 10) is None
    assert ed.summarize_fee([]) == {"samples": 0}


class FakeEvents:
    def __init__(self, metadata: str, blobs: dict[int, str], storage: dict[tuple[int, bytes], bytes]) -> None:
        self.metadata_hex = metadata
        self.blobs = blobs
        self.storage = storage
        self.metadata_calls = 0

    async def block_hash(self, block: int) -> BlockHash:
        return BlockHash("0x" + f"{block:064x}")

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        return 475, 1

    async def metadata(self, block_hash: BlockHash) -> str:
        self.metadata_calls += 1
        return self.metadata_hex

    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]:
        b = int(block_hash, 16)
        out: dict[bytes, bytes | None] = {}
        for k in keys:
            if k == ed.EVENTS_KEY:
                blob = self.blobs.get(b)
                out[k] = None if blob is None else bytes.fromhex(blob[2:])
            else:
                out[k] = self.storage.get((b, k))
        return out


def test_measure_era_a_fee_with_a_fake_archive(decoder: ed.EventsDecoder, golden: Callable[[str], dict[str, Any]]) -> None:
    two_on_19 = [({"ApplyExtrinsic": 1}, "SubtensorModule", "StakeAdded", (A1, A2, 109_950_000, 1_667_375_359, 19, 50_000)),
                 ({"ApplyExtrinsic": 2}, "SubtensorModule", "StakeAdded", (A1, A2, 5, 5, 19, 50_000))]
    clean = [({"ApplyExtrinsic": 1}, "SubtensorModule", "StakeAdded", (A1, A2, 109_950_000, 1_667_375_359, 19, 50_000)),
             ({"ApplyExtrinsic": 2}, "SubtensorModule", "StakeRemoved", (A1, A2, 4_999_950_000, 5_000_000_000, 0, 50_000))]
    t, a = it.SUBNET["tao"].key(netuid=19), it.SUBNET["alpha_in"].key(netuid=19)

    def le(v: int) -> bytes:
        return v.to_bytes(8, "little")

    src = FakeEvents(golden("metadata_spec475_9240388")["metadata"],
                     {100: encode(decoder, two_on_19), 101: encode(decoder, clean)},
                     {(101, t): le(23_507_534_031_585), (101, a): le(356_486_581_230_454)})
    samples = asyncio.run(ed.measure_era_a_fee(src, [100], max_scan=5))
    assert [(s.block, s.netuid, s.kind) for s in samples] == [(101, 19, "add")]  # root and multi-op subnets skipped
    assert samples[0].residual_rel < Decimal("1e-8") and src.metadata_calls == 1


# ------------------------------------------------------------------------------------------------ network
ARCHIVE = "https://bittensor-finney.api.onfinality.io/public"


@pytest.mark.network
def test_network_decode_real_block_and_era_a_fee() -> None:
    from taotrader.chain.reader import JsonRpcChainReader
    from taotrader.chain.rpc import RpcPool

    async def go() -> tuple[list[ed.StakeEvent], list[ed.FeeSample]]:
        pool = RpcPool.from_urls(archive=(ARCHIVE,), rate_per_s=3.0, burst=3, max_concurrency=3)
        src = ed.PoolEventsSource(JsonRpcChainReader(pool, provider_check_every=None))
        try:
            cache = ed.MetadataCache(src)
            evs = await ed.events_at(src, cache, 9_240_388)
            fees = await ed.measure_era_a_fee(src, (5_500_000, 5_800_000), max_scan=20, per_start=1, cache=cache)
            return ed.stake_events(evs), fees
        finally:
            await pool.aclose()

    stakes, fees = asyncio.run(go())
    print("9,240,388 stake events:", stakes)
    print("era-A fee samples:", fees, ed.summarize_fee(fees))
    add = [s for s in stakes if s.kind == "add" and s.netuid == 8]
    assert add and add[0].tao == 649_900_000 and add[0].fee == add[0].tao * 33 // 65_535   # era C: FeeRate 33 / 65535
    assert fees and all(f.fee_field == 50_000 and f.residual_rel < Decimal("1e-4") for f in fees)   # era A: flat fee
