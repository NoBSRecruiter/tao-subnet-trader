"""Property tests (hypothesis): random order streams through SimVenue keep the books straight.

After every committed event: the double-entry ledger balances per unit, cash never goes negative, the venue's cash
equals the ledger's, every position's share value matches its ledger alpha within 2 rao, terminal states follow the
order FSM (enforced by OrderRecord.to in the harness), and a venue rebuilt from the journal has the same state digest.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from hypothesis import event, given, settings
from hypothesis import strategies as st

from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported
from taotrader.core.portfolio import alpha_unit, pos_account
from taotrader.core.units import Block
from taotrader.venues.sim import NoFreeDelegate, SimVenue

TAO = 10**9

ops = st.lists(st.tuples(st.sampled_from(["buy", "sell_part", "sell_full", "move"]),
                         st.integers(min_value=1, max_value=400),        # size in 1/100 TAO or % of position
                         st.sampled_from([1, 2, 5, 6, 60, 61]),           # blocks until the next tick
                         st.booleans()),                                  # allow_partial
               min_size=1, max_size=14)


@settings(max_examples=60)
@given(ops=ops,
       pool_tao=st.integers(min_value=50, max_value=5_000),
       price_milli=st.integers(min_value=1, max_value=900),
       half_life=st.sampled_from([0, None, 50]),
       miss_ppm=st.sampled_from([0, 300_000]),
       exact=st.booleans(),
       seed=st.integers(min_value=0, max_value=3))
def test_random_streams_keep_the_books(consts, harness, snap, subnet, pool, hk_idx, buy, sell, move, ops, pool_tao,
                                       price_milli, half_life, miss_ppm, exact, seed) -> None:
    c = consts
    p = pool(pool_tao, Decimal(price_milli) / 1_000)
    hka = hk_idx(c["HK_A"], total=60_000 * TAO, shares=Decimal(40_000 * TAO))
    hkb = hk_idx(c["HK_B"], total=30_000 * TAO, shares=Decimal(31_000 * TAO))
    market = subnet(p, hotkeys=(hka, hkb))
    cfg = ExecCfg(shield_miss_ppm=miss_ppm, impact_half_life_blocks=half_life)
    h = harness(SimVenue(c["BOOK"], cfg, seed=seed, exact_fills=exact))
    h.capital(20 * TAO, fee_float=TAO)
    block = 50_000
    holder = c["HK_A"]

    def check() -> None:
        h.check_ledger()
        for hk, idx in ((c["HK_A"], hka), (c["HK_B"], hkb)):
            shares = h.venue.shares(c["KEY"], hk)
            led = h.bal(pos_account(c["KEY"], hk), alpha_unit(c["KEY"]))
            assert shares >= 0
            assert abs(idx.value_of(shares) - led) <= 2, (hk, shares, led)

    for i, (kind, size, gap, partial) in enumerate(ops):
        s = snap(block, market)
        view = h.venue.mark_to(s)
        vp = view.get(c["KEY"]).pool
        held = h.venue.shares(c["KEY"], holder)
        value = (hka if holder == c["HK_A"] else hkb).value_of(held)
        it: Any = None
        if kind == "buy":
            it = buy(block, size * TAO // 100, vp, attempt=i, allow_partial=partial, hotkey=holder)
        elif kind == "sell_part" and value > 0:
            amt = max(value * min(size, 99) // 100, 1)
            it = sell(block, amt, vp, attempt=i, allow_partial=partial, hotkey=holder)
        elif kind == "sell_full" and value > 0:
            it = sell(block, full=True, attempt=i, allow_partial=partial, hotkey=holder, limit=1)
        elif kind == "move" and value > 0:
            dest = c["HK_B"] if holder == c["HK_A"] else c["HK_A"]
            it = move(block, dest, attempt=i, hotkey=holder)
        if it is not None:
            try:
                ev = h.place(it, s)
            except NoFreeDelegate:
                ev = None
            check()
            event(f"submit: {type(ev).__name__}")
        block += gap
        for e in h.tick(snap(block, market)):
            check()
            event(f"settle: {e.fill.kind.value if isinstance(e, FillReported) else getattr(e, 'reason', '?')}")
            if isinstance(e, FillReported) and e.fill.dest_hotkey is not None:
                holder = e.fill.dest_hotkey
        block += 1
    h.tick(snap(block + 100, market))
    check()
    assert not h.venue.pending()
    fresh = SimVenue(c["BOOK"], cfg, seed=seed, exact_fills=exact)
    for e in h.journal:
        fresh.observe(e)
    assert fresh.state_digest() == h.venue.state_digest()
    assert fresh.delegates_free(Block(block + 100)) == h.venue.delegates_free(Block(block + 100))
