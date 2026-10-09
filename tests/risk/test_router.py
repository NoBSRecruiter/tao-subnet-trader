"""risk.router: the per-book YieldRouter choice (DESIGN.md 3.8) - hysteresis on synthetic epoch sequences, take jumps,
Q_MAX against own shares (books of different sizes choose different hotkeys), memory lifecycle."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

from taotrader.core.config import ExecCfg, RiskCfg
from taotrader.core.events import ChainEventKind
from taotrader.core.protocols import RouterState
from taotrader.core.units import Hotkey
from taotrader.risk.router import Router, entry_hotkey, q_max_ok, take_jumped

TAO = 10**9
B = 9_240_388


def _market(kit: ModuleType, *, h1_total: int = 10_000_000 * TAO, scores=(3_000, 6_000), eligible=(True, True)):
    h1, h2 = kit.vhk(5, 1), kit.vhk(5, 2)
    s = kit.subnet(5, price=Decimal("0.01"),
                   hotkeys=[kit.hidx(h1, h1_total), kit.hidx(h2, 10_000_000 * TAO)])
    snap = kit.snapshot(subnets=[kit.subnet(1), s])
    cands = sorted([kit.candidate(h1, score=scores[0], eligible=eligible[0]),
                    kit.candidate(h2, score=scores[1], eligible=eligible[1])],
                   key=lambda c: (not c.eligible, -c.score_ppm_day, c.hotkey))
    fr = kit.frame(snap, feat_over={5: {"router_candidates": tuple(cands)}})
    return snap, fr, h1, h2


def _step(kit: ModuleType, router: Router, snap, fr, state: RouterState, alpha: int, hotkey: Hotkey, *, drain: bool,
          events=(), nav: int | None = None) -> RouterState:
    pf = kit.portfolio(positions=[kit.position(kit.key(5), hotkey, alpha)])
    ev = list(events) + ([kit.event(ChainEventKind.EPOCH_DRAIN, key=kit.key(5))] if drain else [])
    t = kit.tick(snap, fr=fr, portfolio=pf, bv=kit.book_view(router=state), events=ev, nav=nav)
    return router(t, RiskCfg())


def test_new_holding_adopts_the_physical_hotkey_and_unheld_is_forgotten(kit: ModuleType) -> None:
    snap, fr, h1, _ = _market(kit)
    r = Router()
    st = _step(kit, r, snap, fr, RouterState(), 1_000 * TAO, h1, drain=True)
    assert st == RouterState(choice=((kit.key(5), h1),))
    empty = kit.tick(snap, fr=fr, portfolio=kit.portfolio(), bv=kit.book_view(router=st))
    assert r(empty, RiskCfg()) == RouterState()


def test_beat_needs_two_epochs_and_a_gain_above_three_move_fees(kit: ModuleType) -> None:
    snap, fr, h1, h2 = _market(kit)
    r = Router(ExecCfg())
    st0 = RouterState(choice=((kit.key(5), h1),))
    same = _step(kit, r, snap, fr, st0, 1_000 * TAO, h1, drain=False)
    assert same == st0                                                   # no drain: nothing re-evaluated
    st1 = _step(kit, r, snap, fr, st0, 1_000 * TAO, h1, drain=True)
    assert st1.choice == ((kit.key(5), h1),) and st1.beat_epochs == ((kit.key(5), h2, 1),)
    assert _step(kit, r, snap, fr, st1, 1_000 * TAO, h1, drain=False) == st1
    st2 = _step(kit, r, snap, fr, st1, 1_000 * TAO, h1, drain=True)
    # value ~ 1,000 alpha * 0.01 = ~10 TAO; gain = 10 TAO * 0.3%/day * 30 d = 0.9 TAO > 3 * 0.001 TAO
    assert st2 == RouterState(choice=((kit.key(5), h2),))
    tiny = _step(kit, r, snap, fr, st1, TAO // 100, h1, drain=True)      # 0.0001 TAO: gain far below 3 fees
    assert tiny.choice == ((kit.key(5), h1),) and tiny.beat_epochs == ((kit.key(5), h2, 2),)


def test_small_beats_do_not_count(kit: ModuleType) -> None:
    snap, fr, h1, _ = _market(kit, scores=(3_000, 3_150))               # +150 ppm/day < max(200, 300)
    st = _step(kit, Router(), snap, fr, RouterState(choice=((kit.key(5), h1),)), 1_000 * TAO, h1, drain=True)
    assert st == RouterState(choice=((kit.key(5), h1),))


def test_current_failing_filters_for_two_epochs_switches(kit: ModuleType) -> None:
    snap, fr, h1, h2 = _market(kit, scores=(6_000, 3_000), eligible=(False, True))
    r = Router()
    st0 = RouterState(choice=((kit.key(5), h1),))
    st1 = _step(kit, r, snap, fr, st0, 1_000 * TAO, h1, drain=True)
    assert st1.choice == ((kit.key(5), h1),) and st1.fail_epochs == ((kit.key(5), 1),)
    st2 = _step(kit, r, snap, fr, st1, 1_000 * TAO, h1, drain=True)
    assert st2 == RouterState(choice=((kit.key(5), h2),))
    # a recovery in between resets the counter
    ok_snap, ok_fr, _, _ = _market(kit, scores=(6_000, 3_000))
    reset = _step(kit, r, ok_snap, ok_fr, st1, 1_000 * TAO, h1, drain=True)
    assert reset == RouterState(choice=((kit.key(5), h1),))


def test_take_jump_switches_at_once(kit: ModuleType) -> None:
    snap, fr, h1, h2 = _market(kit, scores=(6_000, 3_000))
    jump = kit.event(ChainEventKind.TAKE_CHANGED, key=kit.key(5), hotkey=h1, old="0", new="3277")
    small = kit.event(ChainEventKind.TAKE_CHANGED, key=kit.key(5), hotkey=h1, old="0", new="3276")
    assert take_jumped([jump], kit.key(5), h1) and not take_jumped([small], kit.key(5), h1)
    st = _step(kit, Router(), snap, fr, RouterState(choice=((kit.key(5), h1),)), 1_000 * TAO, h1, drain=False,
               events=[jump])
    assert st.choice == ((kit.key(5), h2),)


def test_q_max_makes_books_of_different_sizes_choose_different_hotkeys(kit: ModuleType) -> None:
    # h1 is the best scorer but small: 10,000 alpha of stake
    snap, fr, h1, h2 = _market(kit, h1_total=10_000 * TAO, scores=(6_000, 3_000))
    small = kit.tick(snap, fr=fr, nav=10 * TAO)          # planned 1.5 TAO -> 150 alpha: 1.5% of h1
    large = kit.tick(snap, fr=fr, nav=10_000 * TAO)      # planned 15.2 TAO (V at 1.5%) -> 1,522 alpha: 13% of h1
    assert entry_hotkey(small, kit.key(5), RiskCfg()) == h1
    assert entry_hotkey(large, kit.key(5), RiskCfg()) == h2
    s = snap.get(kit.key(5))
    assert s is not None
    assert q_max_ok(s, h1, 0, 500 * TAO, RiskCfg()) and not q_max_ok(s, h1, 0, 600 * TAO, RiskCfg())
    assert q_max_ok(s, h1, 400 * TAO, 500 * TAO, RiskCfg())     # our current stake is replaced, not added
    assert not q_max_ok(s, kit.hk(1), 0, TAO, RiskCfg())        # stake unknown: fail closed
    # a held large position on h1 fails Q_MAX for two epochs and moves to h2
    r = Router()
    st = RouterState(choice=((kit.key(5), h1),))
    for _ in range(2):
        st = _step(kit, r, snap, fr, st, 1_500 * TAO, h1, drain=True, nav=10_000 * TAO)
    assert st.choice == ((kit.key(5), h2),)
    st_small = RouterState(choice=((kit.key(5), h1),))
    for _ in range(2):
        st_small = _step(kit, r, snap, fr, st_small, 100 * TAO, h1, drain=True, nav=10 * TAO)
    assert st_small.choice == ((kit.key(5), h1),)
