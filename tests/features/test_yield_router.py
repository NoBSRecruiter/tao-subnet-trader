"""features.yield_router (WP5 acceptance): on the golden SN70 dividend panel (49 recipients, the 9,240,581 -> 9,240,582
drain) the candidate panel ranks the take-0 earner 0x56a9 first and flags a take increase, a permit-rank breach and
a ratio breach; membership, consensus-mode re-validation, take history and panel bookkeeping."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core.config import RiskCfg
from taotrader.core.events import ChainEventKind
from taotrader.core.fixed import DEC
from taotrader.core.state import ChainSnapshot, HotkeyIdx, SubnetState
from taotrader.core.units import AlphaRao, Block, Hotkey
from taotrader.features.engine import FeatureEngine
from taotrader.features.yield_router import RouterParams, TakeBook, YieldPanel, take_ok
from taotrader.protocol.derive import derive_events
from taotrader.protocol.yield_model import closed_form_yield_gross

N_EPOCHS = 26
TEMPO = 360


def sn70_history(sn70: Any, *, half_growth: Sequence[Hotkey] = (), take_raise: tuple[Hotkey, int, int] | None = None,
                 stop_earning: tuple[Hotkey, int] | None = None, mav: int | None = None) -> list[SubnetState]:
    """N_EPOCHS + 1 drains ending at 9,240,582: every recipient keeps the per-epoch index growth it showed at the
    captured 9,240,581 -> 9,240,582 drain (shares fixed, TotalHotkeyAlpha scaled), so the last two epochs ARE the
    captured panel. Perturbations: halve a hotkey's growth, raise a take from epoch k, stop earning from epoch k."""
    pre = {h.hotkey: h for h in sn70.pre}
    growth = {h.hotkey: DEC.divide(h.index(), pre[h.hotkey].index()) for h in sn70.post}
    out = []
    for m in range(N_EPOCHS + 1):
        back = N_EPOCHS - m
        hks: list[HotkeyIdx] = []
        for h in sn70.post:
            g = growth[h.hotkey]
            if h.hotkey in half_growth:
                g = DEC.add(Decimal(1), DEC.divide(DEC.subtract(g, Decimal(1)), Decimal(2)))
            if back == 0:
                x = h
            elif back == 1 and h.hotkey not in half_growth:
                x = pre[h.hotkey]
            else:
                x = replace(h, total_alpha=AlphaRao(int(DEC.divide(Decimal(h.total_alpha), DEC.power(g, back)))))
            if take_raise is not None and h.hotkey == take_raise[0] and m >= take_raise[1]:
                x = replace(x, take_u16=take_raise[2])
            if stop_earning is not None and h.hotkey == stop_earning[0] and m >= stop_earning[1]:
                x = replace(x, earns=False, last_dividend=AlphaRao(0))
            hks.append(x)
        s = replace(sn70.base, last_epoch_block=Block(sn70.drain_block - TEMPO * back), hotkeys=tuple(hks))
        if mav is not None:
            s = replace(s, max_allowed_validators=mav)
        out.append(s)
    return out


def run_panel(make_engine: Callable[..., FeatureEngine], snap_of: Callable[..., ChainSnapshot],
              states: list[SubnetState]) -> tuple[Any, list[ChainSnapshot]]:
    eng = make_engine()
    snaps = [snap_of(int(s.last_epoch_block), [s], last_reg_block=9_210_610) for s in states]
    frame = None
    prev = None
    for snap in snaps:
        frame = eng.update(snap, derive_events(prev, snap))
        prev = snap
    assert frame is not None
    return frame.feats[states[-1].key], snaps


def by_prefix(cands: Sequence[Any], prefix: str) -> Any:
    return next(c for c in cands if c.hotkey.startswith(prefix))


# ------------------------------------------------------------------------------------------------- SN70 acceptance
def test_sn70_ranks_the_take0_earner_first(sn70: Any, make_engine: Callable[..., FeatureEngine],
                                           snap_of: Callable[..., ChainSnapshot]) -> None:
    f, _ = run_panel(make_engine, snap_of, sn70_history(sn70))
    cands = f.router_candidates
    assert len(cands) == 49
    top = cands[0]
    assert top.hotkey == sn70.take0_top and top.take_u16 == 0 and top.eligible
    assert f.best_candidate == sn70.take0_top
    assert top.permit_rank == 1 and top.member_frac_ppm == 1_000_000 and top.member_last2 and top.ratio_ok
    # the realised net yield reproduces the brief's SN70 0.557 %/day (0.0279 % per drain x 20 drains/day)
    assert f.yield_net_day == pytest.approx(0.00557, rel=0.01)
    assert f.yield_cf_gross_day == pytest.approx(0.00557, rel=0.01)
    assert f.a_earn_alpha == pytest.approx(182_839, rel=1e-3)
    # best first: eligible before ineligible, then score descending
    elig = [c for c in cands if c.eligible]
    assert list(cands[:len(elig)]) == elig
    assert [c.score_ppm_day for c in elig] == sorted((c.score_ppm_day for c in elig), reverse=True)
    # 18% take hotkeys are filtered (take > 5%); take-0 earners pass on real data
    assert all(not c.eligible for c in cands if c.take_u16 > 3_276)
    assert all(c.eligible for c in cands if c.take_u16 == 0)


def test_sn70_flags_take_increase_permit_rank_and_ratio_breaches(sn70: Any, make_engine: Callable[..., FeatureEngine],
                                                                 snap_of: Callable[..., ChainSnapshot]) -> None:
    post = sorted(sn70.post, key=lambda h: (-h.total_alpha, h.hotkey))
    take0 = [h.hotkey for h in post if h.take_u16 == 0]
    smallest = post[-1].hotkey                                           # permit rank 49 of 49, take 0
    raised, halved = take0[2], take0[3]
    assert smallest in take0 and len({raised, halved, smallest, sn70.take0_top}) == 4
    states = sn70_history(sn70, half_growth=(halved,), take_raise=(raised, N_EPOCHS - 5, 3_000), mav=40)
    f, snaps = run_panel(make_engine, snap_of, states)
    cands = f.router_candidates

    c_raise = by_prefix(cands, raised[:12])
    assert c_raise.take_u16 == 3_000 and take_ok(3_000, 50_000)            # still within TAKE_MAX ...
    assert c_raise.take_increase_recent and not c_raise.eligible         # ... but the increase is flagged
    events = derive_events(snaps[N_EPOCHS - 6], snaps[N_EPOCHS - 5])
    assert any(e.kind is ChainEventKind.TAKE_CHANGED and e.hotkey == raised and e.old == "0" and e.new == "3000" for e in events)

    c_small = by_prefix(cands, smallest[:12])
    assert c_small.permit_rank == 49 and 49 * 1_000_000 > 800_000 * 40       # rank > 0.8 * MaxAllowedValidators
    assert c_small.ratio_ok and c_small.member_frac_ppm == 1_000_000 and not c_small.take_increase_recent
    assert not c_small.eligible

    c_half = by_prefix(cands, halved[:12])
    assert not c_half.ratio_ok and not c_half.eligible
    assert c_half.member_frac_ppm == 1_000_000 and c_half.take_u16 == 0

    assert f.best_candidate == sn70.take0_top                            # the top earner is unaffected
    # without the MaxAllowedValidators override the smallest earner passes (64 validators: rank <= 51)
    g, _ = run_panel(make_engine, snap_of, sn70_history(sn70))
    assert by_prefix(g.router_candidates, smallest[:12]).eligible


def test_membership_rule(sn70: Any, make_engine: Callable[..., FeatureEngine], snap_of: Callable[..., ChainSnapshot]) -> None:
    hk = sn70.take0_top
    f, _ = run_panel(make_engine, snap_of, sn70_history(sn70, stop_earning=(hk, N_EPOCHS)))
    c = by_prefix(f.router_candidates, hk[:12])
    assert c.member_frac_ppm == 950_000 and not c.member_last2 and c.permit_rank is None and not c.eligible
    assert f.best_candidate != hk


def test_too_little_history_is_ineligible(sn70: Any, make_engine: Callable[..., FeatureEngine],
                                          snap_of: Callable[..., ChainSnapshot]) -> None:
    states = sn70_history(sn70)[-10:]
    f, _ = run_panel(make_engine, snap_of, states)
    top = by_prefix(f.router_candidates, sn70.take0_top[:12])
    assert top.member_frac_ppm == 500_000 and not top.eligible and f.best_candidate is None and f.yield_net_day is None
    one, _ = run_panel(make_engine, snap_of, states[-1:])
    assert all(c.score_ppm_day == 0 and not c.eligible for c in one.router_candidates)


# ------------------------------------------------------------------------------------------------- consensus mode
def test_consensus_mode_change_revalidates_the_closed_form(spec_cls: Any, synth: Callable[..., SubnetState],
                                                           snap_of: Callable[..., ChainSnapshot],
                                                           make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(8, reg_at=8_000_000, tempo=TEMPO)
    eng = make_engine()
    ratio_ok: list[bool] = []
    change_at = 30
    for m in range(60):
        b = 9_000_000 + TEMPO * m + 8
        s = replace(synth(sp, b), consensus_mode=0 if m < change_at else 1)
        fr = eng.update(snap_of(b, [s], last_reg_block=8_990_000), ())
        ratio_ok.append(fr.feats[sp.key()].router_candidates[0].ratio_ok)
    assert all(ratio_ok[25:change_at])
    assert not any(ratio_ok[change_at:change_at + 20])                   # blocked for 20 recorded epochs
    assert all(ratio_ok[change_at + 21:])


# ------------------------------------------------------------------------------------------------- bookkeeping
def test_take_book() -> None:
    tb = TakeBook(window=216_000, max_gap=7_200)
    tb.observe("h", 0, 1_000)
    tb.observe("h", 100, 2_000)
    assert tb.increased_since("h", 1_999) and not tb.increased_since("h", 2_000)
    tb.observe("g", 0, 1_000)
    tb.observe("g", 500, 1_000 + 7_201)                                  # previous observation too old: unknown
    assert not tb.increased_since("g", 0)
    tb.observe("h", 50, 3_000)                                           # a decrease is not an increase
    tb.evict(2_000 + 216_000)
    assert not tb.increased_since("h", 0)
    assert tb.state() == ((), ())


def test_yield_panel_bridges_missing_epochs(make_subnet: Callable[..., SubnetState], hkey: Callable[[int], Hotkey]) -> None:
    panel = YieldPanel(41)
    h = HotkeyIdx(hotkey=hkey(1), total_alpha=AlphaRao(10**15), total_shares=Decimal(10**15), take_u16=0, earns=True)
    for k, e in enumerate((1_000, 1_360, 2_080)):                         # the 1,720 drain was not observed
        x = replace(h, total_alpha=AlphaRao(int(10**15 * (1.001 ** (k if e < 2_080 else 3)))))
        panel.observe(make_subnet(5, 0, last_epoch_block=Block(e), hotkeys=(x,), tempo=360), e + 5)
    rets = panel.returns(h.hotkey, 360, 40)
    assert len(rets) == 2 and rets[0] == pytest.approx(0.0009995, rel=1e-3) and rets[1] == pytest.approx(0.0009995, rel=1e-3)
    assert panel.membership(h.hotkey, 20) == (3, True)


def test_router_params_from_risk_and_take_ok() -> None:
    p = RouterParams.from_risk(RiskCfg(take_max_ppm=20_000, permit_rank_frac_ppm=700_000, k_epochs=10))  # type: ignore[arg-type]
    assert (p.take_max_ppm, p.permit_rank_frac_ppm, p.member_window_epochs) == (20_000, 700_000, 10)
    assert take_ok(3_276, 50_000) and not take_ok(3_277, 50_000)          # 3,276 / 65,535 = 4.9989%


def test_closed_form_of_the_fixture(sn70: Any) -> None:
    s = replace(sn70.base, hotkeys=sn70.post)
    assert float(closed_form_yield_gross(s, _glob())) == pytest.approx(0.00557, rel=0.01)


def _glob() -> Any:
    from taotrader.core.state import ChainGlobals
    from taotrader.core.units import Rao
    return ChainGlobals(spec_version=475, tx_version=1, total_issuance=Rao(11_597_600 * 10**9), block_emission=Rao(500_000_000),
                        moving_alpha=Decimal(1_288_490) / Decimal(2**32), gate_bar=Decimal("0.0082624"), gate_rank=32,
                        gate_exponent=3, tao_weight=Decimal("0.18"), root_tao=Rao(5_454_000 * 10**9), owner_cut_u16=11_796,
                        subnet_limit=128, immunity_period=864_000, network_rate_limit=14_400, last_reg_block=Block(9_210_610),
                        last_lock_cost=Rao(653_020_000_000), min_lock_cost=Rao(10**9), lock_reduction_interval=115_200,
                        tao_in_refund_block=Block(8_334_450), nominator_min_stake=Rao(20_000_000), cleanup_queue_len=0,
                        n_nonroot_networks=128, safe_mode_until=None)
