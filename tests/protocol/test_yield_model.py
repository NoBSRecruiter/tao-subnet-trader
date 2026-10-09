"""protocol.yield_model: closed-form yield vectors (SN70 / SN92 / SN64), the SN70 share-price index, A_earn and its
deterministic growth."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from taotrader.core.fixed import DEC
from taotrader.core.state import HotkeyIdx
from taotrader.core.units import BLOCKS_PER_DAY, FEE_DEN, AlphaRao, Hotkey
from taotrader.protocol.yield_model import (
    a_earn,
    a_earn_growth_per_day,
    closed_form_yield_gross,
    closed_form_yield_net,
    index_growth,
    owner_cut_frac,
)

TAO = 10**9
SN70_HK = "0x56a9aee6291bd03ab6d36d4d13e2bebae7cd403518066c72fba1b417d6ddd748"

# brief 3.6 table: rp, A_earn (alpha), predicted gross %/day
CLOSED_FORM = {70: ("0.655", 182_839, "0.557"), 92: ("0.479", 343_198, "0.448"), 64: ("0.133", 2_789_195, "0.0918")}


@pytest.mark.parametrize("netuid", sorted(CLOSED_FORM))
def test_closed_form_yield_vectors(gsnap, dec, netuid: int) -> None:
    """7200*alpha_out_emission*(1 - c_o)*0.5*(1 - rp)/A_earn (= 2952*(1-rp)/A_earn today): SN70 0.557 %/day,
    SN92 0.448, SN64 0.0918. Inputs are read at 9,240,388; the brief measured a few blocks apart, so A_earn and rp
    agree to its rounding (A_earn within 0.05%) and the yield within 0.2%."""
    rp_exp, ae_exp, y_exp = CLOSED_FORM[netuid]
    gs = gsnap("yield_inputs_9240388", 0)
    glob = dec.build_globals(gs)
    s = dec.build_subnet(gs, netuid, glob, with_hotkeys=True)
    assert len(gs.dividend_hotkeys(netuid)) == len([h for h in s.hotkeys if h.earns])
    assert round(s.root_prop, 3) == Decimal(rp_exp)
    ae = Decimal(a_earn(s)) / TAO
    assert abs(ae / ae_exp - 1) < Decimal("0.0005")
    y = closed_form_yield_gross(s, glob) * 100
    assert abs(y / Decimal(y_exp) - 1) < Decimal("0.002")
    assert s.alpha_out_emission == TAO and glob.owner_cut_u16 == 11_796
    k = DEC.multiply(Decimal(BLOCKS_PER_DAY), DEC.multiply(DEC.subtract(Decimal(1), owner_cut_frac(s, glob)), Decimal("0.5")))
    assert round(k) == 2952                                                         # the "2952" of the brief
    assert abs(closed_form_yield_gross(s, glob) - k * (1 - s.root_prop) * TAO / a_earn(s)) < Decimal("1e-20")


def test_sn92_fixture_closed_form_and_alpha_out_vs_a_earn(gsnap, dec) -> None:
    """SN92: AlphaOut ~631k against A_earn ~343k (brief 3.6: A_earn is NOT SubnetAlphaOut)."""
    gs = gsnap("sn92_9240388", 0)
    glob = dec.build_globals(gs)
    s = dec.build_subnet(gs, 92, glob, with_hotkeys=True)
    assert round(closed_form_yield_gross(s, glob) * 100, 3) == Decimal("0.448")
    assert Decimal(s.alpha_out) / TAO > Decimal(630_000) and abs(Decimal(a_earn(s)) / TAO / 343_198 - 1) < Decimal("0.0005")


def _sn70_index(gsnap, dec, i: int) -> tuple[int, HotkeyIdx]:
    gs = gsnap("sn70_index_9240222_9240582", i)
    hks = [h for h in dec.build_hotkeys(gs, 70, extra=[SN70_HK]) if h.hotkey == SN70_HK]
    assert len(hks) == 1
    return gs.block, hks[0]


def test_sn70_share_price_index(gsnap, dec) -> None:
    """Hotkey 56a9 on SN70: I = TotalHotkeyAlpha / shares (V2 SafeFloat, V1 absent) went 1.329667 -> 1.631360 over
    30 days; flat from 9,240,222 to 9,240,581, then +0.0279% at 9,240,582 (= LastEpochBlock)."""
    b0, h0 = _sn70_index(gsnap, dec, 0)
    b1, h1 = _sn70_index(gsnap, dec, 1)
    b2, h2 = _sn70_index(gsnap, dec, 2)
    b3, h3 = _sn70_index(gsnap, dec, 3)
    assert (b0, b1, b2, b3) == (9_240_222, 9_240_581, 9_240_582, 9_024_582) and b2 - b3 == 216_000
    assert round(h3.index(), 6) == Decimal("1.329667")
    assert round(h2.index(), 6) == Decimal("1.631360")
    assert abs(index_growth(h0, h1)) < Decimal("1e-15")                            # flat between drains (stake moved)
    assert h1.total_alpha != h0.total_alpha                                         # ... although stake changed
    jump = index_growth(h1, h2) * 100
    assert round(jump, 4) == Decimal("0.0279")
    assert h1.total_shares == h2.total_shares                                       # the drain raises alpha, not shares
    assert round(index_growth(h3, h2) * 100, 1) == Decimal("22.7")                 # +22.7% over 30 days
    gs2 = gsnap("sn70_index_9240222_9240582", 2)
    gs1 = gsnap("sn70_index_9240222_9240582", 1)
    assert dec.le(gs2.get("SubtensorModule.LastEpochBlock", 70)) == 9_240_582
    assert dec.le(gs1.get("SubtensorModule.LastEpochBlock", 70)) < 9_240_582
    assert h2.take_u16 == 0 and h2.earns                                            # a take-0 earner


def test_a_earn_counts_only_earning_hotkeys(make_subnet, hk) -> None:
    hks = (HotkeyIdx(hotkey=hk(1), total_alpha=AlphaRao(100 * TAO), total_shares=Decimal(90 * TAO), earns=True),
           HotkeyIdx(hotkey=hk(2), total_alpha=AlphaRao(50 * TAO), total_shares=Decimal(50 * TAO), earns=False),
           HotkeyIdx(hotkey=hk(3), total_alpha=AlphaRao(25 * TAO), total_shares=Decimal(20 * TAO), earns=True))
    s = make_subnet(hotkeys=hks)
    assert a_earn(s) == 125 * TAO
    assert a_earn(make_subnet(hotkeys=())) == 0


def test_closed_form_net_and_owner_cut(make_subnet, make_globals, hk) -> None:
    glob = make_globals()
    h = HotkeyIdx(hotkey=hk(1), total_alpha=AlphaRao(300_000 * TAO), total_shares=Decimal(300_000 * TAO), take_u16=3_277,
                  childkey_take_u16=6_554, earns=True)
    s = make_subnet(hotkeys=(h,), root_prop=Decimal("0.5"))
    gross = closed_form_yield_gross(s, glob)
    want = Decimal(BLOCKS_PER_DAY) * TAO * (1 - Decimal(11_796) / FEE_DEN) / 2 * Decimal("0.5") / (300_000 * TAO)
    assert abs(gross - want) < Decimal("1e-25")
    net = closed_form_yield_net(s, glob, h)
    assert abs(net - gross * (1 - Decimal(3_277) / FEE_DEN) * (1 - Decimal(6_554) / FEE_DEN)) < Decimal("1e-25")
    off = replace(s, owner_cut_enabled=False)
    assert owner_cut_frac(off, glob) == 0
    assert closed_form_yield_gross(off, glob) > gross                               # no owner cut -> more to validators
    assert owner_cut_frac(replace(s, owner_cut_enabled=None), glob) == owner_cut_frac(s, glob)   # absent = enabled
    assert closed_form_yield_gross(make_subnet(hotkeys=()), glob) == 0              # NO_YIELD_IDX: no earning stake


def test_a_earn_growth_per_day(make_subnet, make_globals) -> None:
    """Compounding 7200*ae*(1-c_o)*0.5*(1-rp) + escrow deposits 7200*ae*(1-c_o)*0.5*rp while root_flag."""
    glob = make_globals()
    s = make_subnet(root_prop=Decimal("0.4"))
    base = Decimal(BLOCKS_PER_DAY) * TAO * (1 - Decimal(11_796) / FEE_DEN) / 2
    off = a_earn_growth_per_day(s, glob, root_flag=False)
    on = a_earn_growth_per_day(s, glob, root_flag=True)
    assert abs(off / (base * Decimal("0.6")) - 1) < Decimal("1e-25")             # base is in the 28-digit context
    assert abs(on / base - 1) < Decimal("1e-25")                                    # rp + (1 - rp) = everything
    assert index_growth(HotkeyIdx(hotkey=Hotkey("0x" + "0" * 64), total_alpha=AlphaRao(0), total_shares=Decimal(0)),
                        HotkeyIdx(hotkey=Hotkey("0x" + "0" * 64), total_alpha=AlphaRao(5), total_shares=Decimal(5))) == 0
