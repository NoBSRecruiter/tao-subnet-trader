"""Acceptance (WP6): deterministic miss injection, observe()-rebuild gives identical future fills, idempotent submit,
resolve() reproduces the crash-free fills, delegate rotation and locks (DESIGN.md 4.5, 5.13 note 5, 8.5, 3.12)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from taotrader.core.config import ExecCfg
from taotrader.core.events import (
    FillReported,
    JournalEvent,
    OrderFailed,
    OrderIntended,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from taotrader.core.orders import FailReason, OrderState, Resolution
from taotrader.core.units import Block
from taotrader.venues.sim import (
    ERA_MARGIN_BLOCKS,
    LOCK_MARGIN_BLOCKS,
    SHIELD_ERA_BLOCKS,
    NoFreeDelegate,
    SimVenue,
    draw_ppm,
)

TAO = 10**9


# ------------------------------------------------------------------------------------------------- miss injection
def test_draw_is_a_pinned_blake2b_function() -> None:
    import hashlib
    raw = b"7|abc|0"
    assert draw_ppm(7, "abc", 0) == int.from_bytes(hashlib.blake2b(raw, digest_size=16).digest(), "big") % 1_000_000
    assert draw_ppm(7, "abc", 0) == draw_ppm(7, "abc", 0)
    assert draw_ppm(7, "abc", 1) != draw_ppm(7, "abc", 0) and draw_ppm(8, "abc", 0) != draw_ppm(7, "abc", 0)
    assert draw_ppm(7, "abc", 0, "inner") != draw_ppm(7, "abc", 0)


def test_miss_rate_matches_shield_miss_ppm() -> None:
    hits = sum(draw_ppm(0, f"{i:024x}", 0) < 11_000 for i in range(40_000))
    assert 0.009 < hits / 40_000 < 0.013                 # 1.1% (deterministic sample)


def _run_book(consts: dict[str, Any], harness: Any, snap: Any, buy: Any, sell: Any, seed: int, n: int = 60,
              cfg: ExecCfg | None = None) -> list[JournalEvent]:
    """n buy/sell cycles at 60-block stride; returns the venue-produced outcomes."""
    h = harness(SimVenue(consts["BOOK"], cfg or ExecCfg(shield_miss_ppm=200_000), seed=seed))
    h.capital(1_000 * TAO)
    out: list[JournalEvent] = []
    b = 10_000
    for i in range(n):
        it = buy(b, TAO, attempt=i) if h.venue.shares(consts["KEY"], consts["HK_A"]) == 0 else \
            sell(b, full=True, attempt=i)
        out.append(h.place(it, snap(b)))
        b += 60
        out += h.tick(snap(b))
    h.check_ledger()
    return out


def test_miss_injection_is_deterministic_across_runs(consts, harness, snap, buy, sell) -> None:
    a = _run_book(consts, harness, snap, buy, sell, seed=11)
    b = _run_book(consts, harness, snap, buy, sell, seed=11)
    assert a == b
    misses = [e for e in a if isinstance(e, OrderFailed) and e.reason is FailReason.SHIELD_MISSED]
    assert 3 <= len(misses) <= 25                         # 20% of 60
    c = _run_book(consts, harness, snap, buy, sell, seed=12)
    assert a != c                                         # the seed matters


# ------------------------------------------------------------------------------------------------- rebuild
def test_rebuild_from_observe_gives_identical_future_fills(consts, harness, snap, subnet, buy, sell, move) -> None:
    c = consts
    cfg = ExecCfg(shield_miss_ppm=150_000, impact_half_life_blocks=200)
    other = subnet(netuid=9, reg_at=2_000)
    market = (subnet(), other)
    h = harness(SimVenue(c["BOOK"], cfg, seed=5))
    h.capital(500 * TAO)
    b = 20_000
    script: list[Any] = []
    for i in range(12):                                   # interleaved orders on two subnets, some in flight
        script.append(buy(b + 20 * i, (i + 1) * TAO // 2, key=c["KEY"] if i % 2 == 0 else other.key, attempt=i))
    cut = 6

    def step(hh: Any, i: int) -> list[JournalEvent]:
        it = script[i]
        s = snap(int(it.created_block), *market)
        res: list[JournalEvent] = [hh.place(it, s)] if hh.venue.delegates_free(s.block) else []
        res += hh.tick(snap(int(it.created_block) + 7, *market))
        return res

    for i in range(cut):
        step(h, i)
    # a fresh venue folded from the journal alone
    fresh = harness(SimVenue(c["BOOK"], cfg, seed=5))
    for ev in h.journal:
        fresh.commit(ev)
    assert fresh.venue.state_digest() == h.venue.state_digest()
    assert fresh.venue.pending() == h.venue.pending() and fresh.venue.cash == h.venue.cash
    for i in range(cut, len(script)):
        assert step(fresh, i) == step(h, i)
    # sells and a hotkey move after the rebuild, then a full drain far in the future
    for hh in (h, fresh):
        hh.place(sell(22_000, full=True), snap(22_000, *market))
        hh.place(move(22_000, key=other.key), snap(22_000, *market))
    assert h.tick(snap(22_060, *market)) == fresh.tick(snap(22_060, *market))
    assert fresh.venue.state_digest() == h.venue.state_digest()
    h.check_ledger()
    fresh.check_ledger()


def test_mark_to_after_rebuild_is_identical(consts, harness, snap, buy) -> None:
    c = consts
    cfg = ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=1_000)
    h = harness(SimVenue(c["BOOK"], cfg))
    h.capital(100 * TAO)
    h.place(buy(1_000, 10 * TAO), snap(1_000))
    h.tick(snap(1_005))
    fresh = SimVenue(c["BOOK"], cfg)
    for ev in h.journal:
        fresh.observe(ev)
    later = snap(1_500)
    assert fresh.mark_to(later) == h.venue.mark_to(later) != later


# ------------------------------------------------------------------------------------------------- idempotent submit
def test_submit_is_idempotent(consts, harness, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(1_000, TAO)
    first = h.place(it, snap(1_000))
    assert arun(v.submit(it, snap(1_000))) == first
    assert arun(v.submit(it, snap(1_003))) == first      # a later re-drive returns the journaled ack
    assert len(v.pending()) == 1
    rej = buy(990, TAO, hotkey=c["HK_B"])                 # TTL reject
    r1 = h.place(rej, snap(1_000))
    assert isinstance(r1, OrderFailed) and arun(v.submit(rej, snap(1_000))) == r1


def test_observe_ignores_duplicates_and_other_books(consts, harness, snap, buy) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    (fill,) = h.tick(snap(1_005))
    digest = v.state_digest()
    for ev in h.journal:
        v.observe(ev)                                     # replayed twice: no double counting
    assert v.state_digest() == digest
    from dataclasses import replace
    v.observe(replace(fill, fill=replace(fill.fill, book="other", fill_id="x:0:0")))
    assert v.state_digest() == digest


# ------------------------------------------------------------------------------------------------- resolve
def _crash_after_submit_started(consts: dict[str, Any], harness: Any, snap: Any, buy: Any, arun: Any,
                                resolve_block: int) -> tuple[Any, Any]:
    c = consts
    cfg = ExecCfg(shield_miss_ppm=0)
    crash = harness(SimVenue(c["BOOK"], cfg))
    crash.capital(100 * TAO)
    it = buy(1_000, 3 * TAO)
    crash.commit(OrderIntended(it))
    d, n, era = arun(crash.venue.reserve(it, snap(1_000)))
    crash.commit(SubmitStarted(book=c["BOOK"], order_id=it.order_id, attempt=0, delegate=d, nonce=n, era_end=era))
    # --- crash: fresh objects, recovery journals recovered_submitting, then resolve
    rec = harness(SimVenue(c["BOOK"], cfg))
    for ev in crash.journal:
        rec.commit(ev)
    rec.commit(SubmitUnknown(book=c["BOOK"], order_id=it.order_id, attempt=0, detail="recovered_submitting"))
    res, evs = arun(rec.venue.resolve(it, snap(resolve_block)))
    assert res is Resolution.PLACED
    for ev in evs:
        rec.commit(ev)
    # --- the crash-free reference
    ref = harness(SimVenue(c["BOOK"], cfg))
    ref.capital(100 * TAO)
    ref.place(it, snap(1_000))
    return rec, ref


@pytest.mark.parametrize("resolve_block", [1_000, 1_004, 1_200])
def test_resolve_reproduces_the_crash_free_fill(consts, harness, snap, buy, arun, resolve_block) -> None:
    rec, ref = _crash_after_submit_started(consts, harness, snap, buy, arun, resolve_block)
    ack_rec = [e for e in rec.journal if isinstance(e, VenueAck)]
    ack_ref = [e for e in ref.journal if isinstance(e, VenueAck)]
    assert ack_rec == ack_ref and ack_rec[0].expected_fill_block == 1_005
    assert rec.tick(snap(1_005)) == ref.tick(snap(1_005))
    (intended,) = [e for e in rec.journal if isinstance(e, OrderIntended)]
    assert rec.state(intended.intent) is OrderState.FILLED and len(rec.fills()) == 1
    assert rec.venue.cash == ref.venue.cash and rec.venue.shares(consts["KEY"], consts["HK_A"]) > 0


def test_resolve_on_acked_and_landed_orders(consts, harness, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(1_000, TAO)
    h.place(it, snap(1_000))
    assert arun(v.resolve(it, snap(1_001))) == (Resolution.PLACED, ())
    h.tick(snap(1_005))
    assert arun(v.resolve(it, snap(1_006))) == (Resolution.LANDED, ())


def test_resolve_re_derives_a_reject(consts, harness, snap, buy, arun) -> None:
    """A crash after SubmitStarted for an intent submit() would have rejected resolves NOT_PLACED with that reject."""
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(990, TAO)                                    # TTL already passed at 1,000
    h.commit(OrderIntended(it))
    d, n, era = arun(v.reserve(it, snap(1_000)))
    h.commit(SubmitStarted(book=c["BOOK"], order_id=it.order_id, attempt=0, delegate=d, nonce=n, era_end=era))
    expected = arun(v.submit(it, snap(1_000)))
    h.commit(SubmitUnknown(book=c["BOOK"], order_id=it.order_id, attempt=0, detail="recovered_submitting"))
    res, evs = arun(v.resolve(it, snap(1_100)))
    assert res is Resolution.NOT_PLACED and evs == (expected,)
    assert isinstance(expected, OrderFailed) and expected.reason is FailReason.VENUE_REJECT and expected.tx_fee == 0


# ------------------------------------------------------------------------------------------------- delegates
def test_reserve_rotates_over_n_delegates(consts, harness, snap, subnet, buy, arun) -> None:
    c = consts
    subs = tuple(subnet(netuid=n, reg_at=100 + n) for n in (3, 4, 5, 6))
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    s = snap(1_000, *subs)
    seen = []
    for sn in subs[:3]:
        it = buy(1_000, TAO, key=sn.key)
        h.place(it, s)
        seen.append([e for e in h.journal if isinstance(e, SubmitStarted)][-1])
    assert [e.delegate for e in seen] == ["sim0", "sim1", "sim2"]
    assert all(e.nonce is None and e.era_end == 1_000 + SHIELD_ERA_BLOCKS + ERA_MARGIN_BLOCKS for e in seen)
    with pytest.raises(NoFreeDelegate):
        arun(v.reserve(buy(1_000, TAO, key=subs[3].key), s))
    assert v.delegates_free(Block(1_000)) == ()
    h.tick(snap(1_005, *subs))                            # all three fill: delegates free again
    assert v.delegates_free(Block(1_005)) == ("sim0", "sim1", "sim2")
    unshielded = buy(1_010, TAO, key=subs[3].key, shielded=False)
    assert arun(v.reserve(unshielded, snap(1_010, *subs)))[2] == 1_010 + 16 + ERA_MARGIN_BLOCKS


def test_miss_locks_the_delegate_until_era_end_plus_two(consts, harness, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=1_000_000))
    h = harness(v)
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    (miss,) = h.tick(snap(1_005))
    assert isinstance(miss, OrderFailed) and miss.reason is FailReason.SHIELD_MISSED
    era_end = 1_000 + SHIELD_ERA_BLOCKS + ERA_MARGIN_BLOCKS
    assert v.delegate_locked_until() == (("sim0", era_end + LOCK_MARGIN_BLOCKS),)
    retry = buy(1_006, TAO, attempt=1)
    assert arun(v.reserve(retry, snap(1_006)))[0] == "sim1"          # the retry rotates
    assert v.delegates_free(Block(era_end + LOCK_MARGIN_BLOCKS)) == ("sim1", "sim2")
    assert v.delegates_free(Block(era_end + LOCK_MARGIN_BLOCKS + 1)) == ("sim0", "sim1", "sim2")


def test_reserve_is_stable_once_submit_started(consts, harness, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(1_000, TAO)
    h.place(it, snap(1_000))
    assert arun(v.reserve(it, snap(1_003))) == ("sim0", None, Block(1_010))


def test_venue_rejects_foreign_books(consts, snap, buy) -> None:
    v = SimVenue(consts["BOOK"], ExecCfg())
    with pytest.raises(ValueError, match="belongs to book"):
        asyncio.run(v.submit(buy(1_000, TAO, book="other"), snap(1_000)))


def test_caps_and_config_validation(consts) -> None:
    v = SimVenue(consts["BOOK"], ExecCfg(n_delegates=2, latency_blocks=2))
    assert (v.caps.kind, v.caps.shield_latency_blocks, v.caps.era_blocks, v.caps.n_delegates) == ("sim", 2, 8, 2)
    assert not v.caps.supports_rotation and v.caps.max_inflight_per_netuid == 1
    for bad in (ExecCfg(n_delegates=0), ExecCfg(impact_half_life_blocks=-1), ExecCfg(shield_miss_ppm=2_000_000)):
        with pytest.raises(ValueError):
            SimVenue(consts["BOOK"], bad)


def _exact(e: JournalEvent) -> bool:
    return e.fill.exact_block if isinstance(e, FillReported) else bool(getattr(e, "exact_block", False))


def test_stride_and_per_block_runs_differ_only_in_settlement_flags(consts, harness, snap, buy) -> None:
    """Same decisions at stride 1 and stride 60: identical outcomes (misses are seeded by order id), only the
    exact_block flag differs (per-block runs settle at N+2 exactly, stride runs at the next snapshot)."""
    c = consts
    cfg = ExecCfg(shield_miss_ppm=300_000)
    fine, coarse = harness(SimVenue(c["BOOK"], cfg, seed=9)), harness(SimVenue(c["BOOK"], cfg, seed=9))
    for hh in (fine, coarse):
        hh.capital(100 * TAO)
    out_fine: list[JournalEvent] = []
    out_coarse: list[JournalEvent] = []
    for i in range(10):
        b = 30_000 + 60 * i
        it = buy(b, TAO, attempt=i)
        for hh in (fine, coarse):
            hh.place(it, snap(b))
        for blk in range(b + 1, b + 60):
            out_fine += fine.tick(snap(blk))
        out_coarse += coarse.tick(snap(b + 59))
    assert [type(e).__name__ for e in out_fine] == [type(e).__name__ for e in out_coarse]
    assert any(isinstance(e, OrderFailed) for e in out_fine) and any(isinstance(e, FillReported) for e in out_fine)
    assert all(_exact(e) for e in out_fine) and not any(_exact(e) for e in out_coarse)


def test_submit_after_not_placed_returns_the_same_failure(consts, harness, snap, buy, arun) -> None:
    """SUBMITTING -> crash -> UNKNOWN -> resolve(NOT_PLACED + reject) committed; a re-driven submit gives that answer."""
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    h = harness(v)
    h.capital(100 * TAO)
    it = buy(990, TAO)                                    # TTL already passed at 1,000
    h.commit(OrderIntended(it))
    d, n, era = arun(v.reserve(it, snap(1_000)))
    h.commit(SubmitStarted(book=c["BOOK"], order_id=it.order_id, attempt=0, delegate=d, nonce=n, era_end=era))
    h.commit(SubmitUnknown(book=c["BOOK"], order_id=it.order_id, attempt=0, detail="recovered_submitting"))
    res, (np_ev,) = arun(v.resolve(it, snap(1_000)))
    assert res is Resolution.NOT_PLACED
    h.commit(np_ev)
    assert h.state(it) is OrderState.FAILED
    assert arun(v.submit(it, snap(1_200))) == np_ev      # never a second, different answer for (order_id, attempt)
    assert arun(v.resolve(it, snap(1_200))) == (Resolution.LANDED, ())
    assert v.delegates_free(Block(1_200)) == ("sim0", "sim1", "sim2")


def test_an_ack_without_its_intent_fails_loudly(consts, snap, buy, arun) -> None:
    c = consts
    v = SimVenue(c["BOOK"], ExecCfg(shield_miss_ppm=0))
    it = buy(1_000, TAO)
    v.observe(VenueAck(book=c["BOOK"], order_id=it.order_id, attempt=0, submit_block=Block(1_003),
                       expected_fill_block=Block(1_005), carrier_hash="", inner_hash=""))
    with pytest.raises(RuntimeError, match="OrderIntended was never observed"):
        arun(v.advance(snap(1_005)))
