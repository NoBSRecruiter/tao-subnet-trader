"""taotrader/data/events_decoder.py - OPTIONAL System.Events decoder (WP4; DESIGN.md sections 3.6, 4.1, 11 WP4, 13 Q21
and Q23). Needs the `collector` extra (scalecodec); nothing on the v1 decision path depends on it.

- `EventsDecoder(metadata_hex)` decodes `System.Events` of one runtime with scalecodec and the runtime's own V14+
  metadata (the storage entry's value type from the portable registry), so every spec decodes with its exact layout.
  `MetadataCache` keeps one decoder per spec version (metadata fetched once per spec at a block hash of that spec).
- Extractors: `stake_events` (StakeAdded / StakeRemoved: coldkey, hotkey, TAO, alpha, netuid, fee; positional in
  every spec seen so far), `claim_events` (RootClaimed / BasketClaimed), `perpetual_lock_updates`
  (PerpetualLockUpdated{coldkey, netuid, enabled}) and `owner_lock_alerts`: an owner coldkey switching its lock
  from perpetual to decaying (enabled = false) is the section 13 Q23 / brief 4.8 early exit signal (backlog: no v1
  rule depends on it).
- `measure_era_a_fee` (section 13 Q21): era-A stake events (one stake operation on the subnet in the block) against
  the post-state reserves of the same block (the coinbase injection runs in on_initialize, before extrinsics, so the
  post-state minus the trade is the pre-trade pool). With x = SubnetAlphaIn and y = SubnetTAO after the block:
    StakeAdded:   the pool took d = alpha * y / (x + alpha) TAO   (pre-trade pool x + alpha, y - d)
    StakeRemoved: the pool paid g = y * alpha / (x - alpha) TAO    (pre-trade pool x - alpha, y + g)
  and the residual against "no proportional fee, flat fee = the event's fee field" (d == event TAO for adds,
  g == event TAO + fee for removes) is reported per sample.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from ..chain import items as it
from ..chain.hashing import to_hex, twox128
from ..core.fixed import DEC
from ..core.units import BlockHash

log = logging.getLogger(__name__)

EVENTS_KEY: Final[bytes] = twox128(b"System") + twox128(b"Events")
ERA_A_LAST_BLOCK: Final[int] = it.ERA_B_FIRST_BLOCK - 1
ERA_A_SAMPLE_BLOCKS: Final[tuple[int, ...]] = (4_930_000, 5_100_000, 5_300_000, 5_500_000, 5_800_000)


class EventsDecoderUnavailable(RuntimeError):
    """scalecodec (the collector extra) is not installed."""


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    block: int
    index: int                      # position in System.Events
    phase: str                      # "Initialization" | "ApplyExtrinsic" | "Finalization"
    extrinsic_idx: int | None
    pallet: str
    name: str
    attributes: Any                 # scalecodec value: tuple (positional fields) or dict (named fields)


@dataclass(frozen=True, slots=True)
class StakeEvent:
    block: int
    extrinsic_idx: int | None
    kind: str                       # "add" | "remove"
    coldkey: str
    hotkey: str
    tao: int                        # rao: StakeAdded = TAO into the pool (net of the flat fee); StakeRemoved = TAO paid out
    alpha: int                      # alpha rao out (add) / in (remove)
    netuid: int
    fee: int | None                 # the event's fee field (rao), None where the runtime has none


@dataclass(frozen=True, slots=True)
class PerpetualLockChange:
    block: int
    coldkey: str
    netuid: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    block: int
    kind: str                       # "root" (RootClaimed{coldkey, tao}) | "basket" (BasketClaimed{hotkey, coldkey, tao})
    coldkey: str
    hotkey: str | None
    tao: int                        # rao


@dataclass(frozen=True, slots=True)
class OwnerLockAlert:
    block: int
    netuid: int
    coldkey: str                    # the subnet owner coldkey that switched to a decaying lock


def _import_scalecodec() -> tuple[Any, Any, Any]:
    try:
        from scalecodec.base import RuntimeConfiguration, ScaleBytes
        from scalecodec.type_registry import load_type_registry_preset
    except ImportError as e:  # pragma: no cover - exercised only without the collector extra
        raise EventsDecoderUnavailable("events_decoder needs the collector extra (scalecodec)") from e
    return RuntimeConfiguration, ScaleBytes, load_type_registry_preset


class EventsDecoder:
    """System.Events decoder of one runtime."""

    def __init__(self, metadata_hex: str) -> None:
        rc_cls, sb, preset = _import_scalecodec()
        self._sb = sb
        rc = rc_cls()
        rc.update_type_registry(preset("core"))
        rc.update_type_registry(preset("legacy"))
        md = rc.create_scale_object("MetadataVersioned", data=sb(metadata_hex))
        md.decode()
        rc.add_portable_registry(md)
        self._rc = rc
        self._md = md
        versioned = md.value[1]
        m = versioned[next(iter(versioned))]
        tid: int | None = None
        for p in m["pallets"]:
            if p["name"] == "System" and p.get("storage"):
                for e in p["storage"]["entries"]:
                    if e["name"] == "Events":
                        tid = int(e["type"]["Plain"])
        if tid is None:
            raise ValueError("metadata has no System.Events storage entry")
        self.events_type = f"scale_info::{tid}"
        self.event_names = {(str(p["name"]), str(v["name"])) for p in m["pallets"] if p.get("event") is not None
                            for v in _variants(m, p["event"])}

    def decode(self, events_hex: str | None, block: int) -> tuple[DecodedEvent, ...]:
        if events_hex is None or events_hex in ("0x", "0x00"):
            return ()
        obj = self._rc.create_scale_object(self.events_type, data=self._sb(events_hex), metadata=self._md)
        obj.decode()
        out: list[DecodedEvent] = []
        for i, rec in enumerate(obj.value):
            phase = rec.get("phase")
            ph = phase if isinstance(phase, str) else next(iter(phase)) if isinstance(phase, Mapping) else str(phase)
            ev = rec.get("event") or rec
            out.append(DecodedEvent(block=block, index=i, phase=str(ph), extrinsic_idx=rec.get("extrinsic_idx"),
                                    pallet=str(ev.get("module_id") or rec.get("module_id")),
                                    name=str(ev.get("event_id") or rec.get("event_id")),
                                    attributes=ev.get("attributes", rec.get("attributes"))))
        return tuple(out)


def _variants(m: Mapping[str, Any], ev: Any) -> list[Mapping[str, Any]]:
    tid = ev["ty"] if isinstance(ev, Mapping) else ev
    for t in m["types"]["types"]:
        if t["id"] == tid:
            d = t["type"]["def"]
            return list(d["variant"]["variants"]) if "variant" in d else []
    return []


# ------------------------------------------------------------------------------------------------ sources
class EventsSource(Protocol):
    async def block_hash(self, block: int) -> BlockHash: ...
    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]: ...
    async def metadata(self, block_hash: BlockHash) -> str: ...
    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]: ...


class MetadataCache:
    """One EventsDecoder per spec version."""

    def __init__(self, source: EventsSource) -> None:
        self.source = source
        self.decoders: dict[int, EventsDecoder] = {}

    async def for_block(self, block_hash: BlockHash) -> EventsDecoder:
        spec = (await self.source.spec_version(block_hash))[0]
        d = self.decoders.get(spec)
        if d is None:
            d = self.decoders[spec] = EventsDecoder(await self.source.metadata(block_hash))
        return d


async def events_at(source: EventsSource, cache: MetadataCache, block: int,
                    block_hash: BlockHash | None = None) -> tuple[DecodedEvent, ...]:
    h = block_hash or await source.block_hash(block)
    dec = await cache.for_block(h)
    raw = (await source.query([EVENTS_KEY], h)).get(EVENTS_KEY)
    return dec.decode(None if raw is None else to_hex(raw), block)


# ------------------------------------------------------------------------------------------------ extractors
def _fields(attrs: Any, names: Sequence[str]) -> list[Any]:
    if isinstance(attrs, Mapping):
        return [attrs.get(n) for n in names]
    if isinstance(attrs, (list, tuple)):
        return list(attrs)
    raise ValueError(f"unexpected event attributes {attrs!r}")


def stake_events(events: Iterable[DecodedEvent]) -> list[StakeEvent]:
    out: list[StakeEvent] = []
    for e in events:
        if e.pallet != "SubtensorModule" or e.name not in ("StakeAdded", "StakeRemoved"):
            continue
        f = _fields(e.attributes, ("coldkey", "hotkey", "tao", "alpha", "netuid", "fee"))
        if len(f) < 5:
            raise ValueError(f"{e.name} at {e.block}: {len(f)} fields")
        out.append(StakeEvent(block=e.block, extrinsic_idx=e.extrinsic_idx, kind="add" if e.name == "StakeAdded" else "remove",
                              coldkey=str(f[0]), hotkey=str(f[1]), tao=int(f[2]), alpha=int(f[3]), netuid=int(f[4]),
                              fee=None if len(f) < 6 or f[5] is None else int(f[5])))
    return out


def perpetual_lock_updates(events: Iterable[DecodedEvent]) -> list[PerpetualLockChange]:
    out: list[PerpetualLockChange] = []
    for e in events:
        if e.pallet == "SubtensorModule" and e.name == "PerpetualLockUpdated":
            ck, n, en = _fields(e.attributes, ("coldkey", "netuid", "enabled"))[:3]
            out.append(PerpetualLockChange(e.block, str(ck), int(n), bool(en)))
    return out


def claim_events(events: Iterable[DecodedEvent]) -> list[ClaimEvent]:
    """Root and basket claims (claim enrichment; brief 3.7: claims release escrowed root dividends)."""
    out: list[ClaimEvent] = []
    for e in events:
        if e.pallet != "SubtensorModule":
            continue
        if e.name == "RootClaimed":
            ck, tao = _fields(e.attributes, ("coldkey", "tao"))[:2]
            out.append(ClaimEvent(e.block, "root", str(ck), None, int(tao)))
        elif e.name == "BasketClaimed":
            hk, ck, tao = _fields(e.attributes, ("hotkey", "coldkey", "tao"))[:3]
            out.append(ClaimEvent(e.block, "basket", str(ck), str(hk), int(tao)))
    return out


def owner_lock_alerts(changes: Iterable[PerpetualLockChange], owner_of: Mapping[int, str]) -> list[OwnerLockAlert]:
    """Owner coldkeys switching their subnet's lock from perpetual to decaying (enabled = false)."""
    return [OwnerLockAlert(c.block, c.netuid, c.coldkey) for c in changes
            if not c.enabled and owner_of.get(c.netuid, "").lower() == c.coldkey.lower()]


# ------------------------------------------------------------------------------------------------ era-A fee (Q21)
@dataclass(frozen=True, slots=True)
class FeeSample:
    block: int
    spec: int
    netuid: int
    kind: str
    tao_event: int
    alpha_event: int
    fee_field: int | None
    tao_post: int                    # SubnetTAO after the block
    alpha_post: int                  # SubnetAlphaIn after the block
    implied_tao: Decimal             # add: TAO the pool took; remove: gross TAO the pool paid
    residual_rel: Decimal            # vs "no proportional fee, flat fee = fee_field" (add: tao_event; remove: tao + fee)

    @property
    def implied_fee_rao(self) -> Decimal:
        """Fee implied by the reserves: add = gross (event TAO + fee field) - TAO taken; remove = gross paid - net."""
        if self.kind == "add":
            return DEC.subtract(Decimal(self.tao_event + (self.fee_field or 0)), self.implied_tao)
        return DEC.subtract(self.implied_tao, Decimal(self.tao_event))


def fee_sample(ev: StakeEvent, spec: int, tao_post: int, alpha_post: int) -> FeeSample | None:
    """Constant-product check of one stake event against the post-state reserves of its block (see module doc)."""
    x, y = Decimal(alpha_post), Decimal(tao_post)
    a = Decimal(ev.alpha)
    fee = ev.fee or 0
    if ev.kind == "add":
        den = DEC.add(x, a)
        if den <= 0:
            return None
        implied = DEC.divide(DEC.multiply(a, y), den)
        expect = Decimal(ev.tao)
    else:
        den = DEC.subtract(x, a)
        if den <= 0:
            return None
        implied = DEC.divide(DEC.multiply(y, a), den)
        expect = Decimal(ev.tao + fee)
    if expect <= 0:
        return None
    resid = DEC.divide(abs(DEC.subtract(implied, expect)), expect)
    return FeeSample(ev.block, spec, ev.netuid, ev.kind, ev.tao, ev.alpha, ev.fee, tao_post, alpha_post, implied, resid)


async def measure_era_a_fee(source: EventsSource, starts: Sequence[int] = ERA_A_SAMPLE_BLOCKS, *, max_scan: int = 40,
                            per_start: int = 2, cache: MetadataCache | None = None) -> list[FeeSample]:
    """From each start block, scan forward (<= max_scan blocks) and keep up to `per_start` clean samples: a non-root
    subnet with exactly one stake event in the block."""
    cache = cache or MetadataCache(source)
    out: list[FeeSample] = []
    tao_row, alpha_row = it.SUBNET["tao"], it.SUBNET["alpha_in"]
    for start in starts:
        got = 0
        for b in range(start, start + max_scan):
            if got >= per_start:
                break
            h = await source.block_hash(b)
            evs = stake_events(await events_at(source, cache, b, h))
            per: dict[int, list[StakeEvent]] = {}
            for e in evs:
                per.setdefault(e.netuid, []).append(e)
            clean = [es[0] for n, es in sorted(per.items()) if n != 0 and len(es) == 1]
            if not clean:
                continue
            spec = (await source.spec_version(h))[0]
            keys = [k for e in clean for k in (tao_row.key(netuid=e.netuid), alpha_row.key(netuid=e.netuid))]
            raw = await source.query(keys, h)
            for e in clean:
                t = raw.get(tao_row.key(netuid=e.netuid))
                a = raw.get(alpha_row.key(netuid=e.netuid))
                if t is None or a is None:
                    continue
                s = fee_sample(e, spec, int(tao_row.decode(t)), int(alpha_row.decode(a)))
                if s is not None and got < per_start:
                    out.append(s)
                    got += 1
    return out


def summarize_fee(samples: Sequence[FeeSample]) -> dict[str, Any]:
    """The ADR numbers: flat fee values seen, the largest residual, and the implied proportional rate (u16/65535)."""
    if not samples:
        return {"samples": 0}
    worst = max(s.residual_rel for s in samples)
    rates = [DEC.divide(DEC.multiply(abs(s.implied_fee_rao - Decimal(s.fee_field or 0)), Decimal(65_535)),
                        Decimal(s.tao_event + (s.fee_field or 0))) for s in samples]
    return {"samples": len(samples), "specs": sorted({s.spec for s in samples}),
            "fee_fields": sorted({s.fee_field for s in samples if s.fee_field is not None}),
            "max_residual_rel": str(worst), "max_proportional_rate_u16": str(max(rates))}


# ------------------------------------------------------------------------------------------------ CLI
class PoolEventsSource:
    """EventsSource over the WP1 RpcPool (archive role)."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader

    async def block_hash(self, block: int) -> BlockHash:
        return BlockHash(str(await self.reader.block_hash(block)))

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        v = await self.reader.spec_version(block_hash)
        return int(v[0]), int(v[1])

    async def metadata(self, block_hash: BlockHash) -> str:
        return str(await self.reader.pool.call("state_getMetadata", [block_hash], self.reader.role))

    async def query(self, keys: Sequence[bytes], block_hash: BlockHash) -> dict[bytes, bytes | None]:
        res: dict[bytes, bytes | None] = await self.reader.query(keys, block_hash)
        return res


async def _cli(a: argparse.Namespace) -> int:
    from ..chain.reader import JsonRpcChainReader
    from ..chain.rpc import RpcPool

    pool = RpcPool.from_urls(archive=(a.endpoint,), rate_per_s=3.0, burst=3, max_concurrency=3)
    src = PoolEventsSource(JsonRpcChainReader(pool, provider_check_every=None))
    try:
        if a.cmd == "era-a-fee":
            samples = await measure_era_a_fee(src, a.blocks or ERA_A_SAMPLE_BLOCKS, max_scan=a.max_scan)
            print(json.dumps({"summary": summarize_fee(samples),
                              "samples": [{**asdict(s), "implied_tao": str(s.implied_tao), "residual_rel": str(s.residual_rel),
                                           "implied_fee_rao": str(s.implied_fee_rao)} for s in samples]}, indent=1))
            return 0
        cache = MetadataCache(src)
        for b in a.blocks:
            for e in await events_at(src, cache, b):
                print(json.dumps({"block": e.block, "index": e.index, "phase": e.phase, "extrinsic": e.extrinsic_idx,
                                  "event": f"{e.pallet}.{e.name}", "attributes": e.attributes}, default=str))
        return 0
    finally:
        await pool.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(prog="python -m taotrader.data.events_decoder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("era-a-fee", "events"):
        sp = sub.add_parser(name)
        sp.add_argument("--blocks", type=lambda s: [int(x) for x in s.split(",")], default=None)
        sp.add_argument("--endpoint", default="https://bittensor-finney.api.onfinality.io/public")
        sp.add_argument("--max-scan", type=int, default=40)
    a = ap.parse_args(argv)
    if a.cmd == "events" and not a.blocks:
        ap.error("--blocks is required")
    return asyncio.run(_cli(a))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
