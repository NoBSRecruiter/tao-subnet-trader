"""risk.modes: the section 3.11 mode table, driven by HealthObs / ChainEvent fixtures, and the FT11 drill hooks."""
from __future__ import annotations

from dataclasses import replace
from types import ModuleType

import pytest

from taotrader.core.config import RiskCfg
from taotrader.core.events import ChainEventKind, HealthObs
from taotrader.core.orders import OrderKind, Urgency
from taotrader.core.units import BLOCKS_PER_DAY, PPM, Block, Mode, Rao, RunMode
from taotrader.portfolio.planner import StandardPlanner
from taotrader.risk import modes
from taotrader.risk.modes import ModeSignals
from taotrader.risk.overlay import StandardOverlay

B = 9_240_388


def _eval(kit: ModuleType, *, events=(), run_mode=RunMode.BACKTEST, signals=None, cfg=None, **tick_kw) -> modes.ModeVerdict:
    snap = tick_kw.pop("snap", None) or kit.snapshot()
    t = kit.tick(snap, events=events, **tick_kw)
    return modes.evaluate(kit.risk_ctx(t, cfg=cfg), run_mode=run_mode, signals=signals)


def test_nominal_tick_is_normal(kit: ModuleType) -> None:
    v = _eval(kit)
    assert v.mode is Mode.NORMAL and not v.halt_entries and v.actions == ()
    assert v.g_max_eff_ppm == RiskCfg().g_max_ppm


def test_engine_floor_is_never_lowered(kit: ModuleType) -> None:
    v = _eval(kit, mode=Mode.EXITS_ONLY)
    assert v.mode is Mode.EXITS_ONLY and v.halt_entries
    assert not any(a.action == "MODE" for a in v.actions)        # nothing raised above the floor


@pytest.mark.parametrize(("name", "run_mode", "expected"), [
    ("spec_version", RunMode.BACKTEST, Mode.CAUTION),
    ("spec_version", RunMode.LIVE, Mode.CAUTION),
    ("transaction_version", RunMode.PAPER, Mode.CAUTION),
    ("transaction_version", RunMode.BACKTEST, Mode.CAUTION),
    ("transaction_version", RunMode.LIVE, Mode.FROZEN),
    ("transaction_version", RunMode.LIVE_DRY, Mode.FROZEN),
])
def test_spec_and_tx_version_changes(kit: ModuleType, name: str, run_mode: RunMode, expected: Mode) -> None:
    ev = kit.event(ChainEventKind.SPEC_CHANGED, name=name, old="474", new="475")
    v = _eval(kit, events=[ev], run_mode=run_mode)
    assert v.mode is expected
    assert v.actions[0].rule == "mode.raise" and v.actions[0].action == "MODE"


def test_safe_mode_state_and_event(kit: ModuleType) -> None:
    assert _eval(kit, snap=kit.snapshot(safe_mode_until=Block(B + 10))).mode is Mode.FROZEN
    assert _eval(kit, snap=kit.snapshot(safe_mode_until=Block(B))).mode is Mode.FROZEN
    assert _eval(kit, snap=kit.snapshot(safe_mode_until=Block(B - 1))).mode is Mode.NORMAL
    assert _eval(kit, events=[kit.event(ChainEventKind.SAFE_MODE, flag=True)]).mode is Mode.FROZEN
    assert _eval(kit, events=[kit.event(ChainEventKind.SAFE_MODE, flag=False)]).mode is Mode.NORMAL


@pytest.mark.parametrize(("health", "expected", "why"), [
    (HealthObs(3, 12, 2, 0, 0), Mode.NORMAL, None),
    (HealthObs(3, 37, 2, 0, 0), Mode.CAUTION, "stall_warn"),
    (HealthObs(3, 36, 2, 0, 0), Mode.NORMAL, None),
    (HealthObs(3, 121, 2, 0, 0), Mode.EXITS_ONLY, "stall_halt"),
    (HealthObs(31, 12, 2, 0, 0), Mode.CAUTION, "finality_lag"),
    (HealthObs(30, 12, 2, 0, 0), Mode.NORMAL, None),
    (HealthObs(3, 12, 1, 0, 0), Mode.CAUTION, "healthy_endpoints"),
    (HealthObs(3, 12, 0, 0, 0), Mode.FROZEN, "no_healthy_endpoint"),
    (HealthObs(3, 12, 2, 0, 26), Mode.CAUTION, "stale_prune_inputs"),
    (HealthObs(3, 12, 2, 0, 25), Mode.NORMAL, None),
])
def test_health_rows(kit: ModuleType, health: HealthObs, expected: Mode, why: str | None) -> None:
    v = _eval(kit, signals=ModeSignals(health=health))
    assert v.mode is expected
    if why is not None:
        assert why in v.reasons


def test_stale_prune_inputs_from_the_snapshot_gap_outside_backtests(kit: ModuleType) -> None:
    prev = kit.snapshot(B - 26)
    assert _eval(kit, prev=prev, run_mode=RunMode.PAPER).mode is Mode.CAUTION
    assert _eval(kit, prev=kit.snapshot(B - 25), run_mode=RunMode.PAPER).mode is Mode.NORMAL
    assert _eval(kit, prev=kit.snapshot(B - 60), run_mode=RunMode.BACKTEST).mode is Mode.NORMAL     # stride replay


def test_emission_model_and_drift_probe(kit: ModuleType) -> None:
    snap = kit.snapshot()
    v = _eval(kit, snap=snap, fr=kit.frame(snap, model_ok=False))
    assert v.mode is Mode.CAUTION and "emission_model" in v.reasons
    assert _eval(kit, signals=ModeSignals(sim_swap_drift_bp=6)).mode is Mode.CAUTION
    assert _eval(kit, signals=ModeSignals(sim_swap_drift_bp=5)).mode is Mode.NORMAL


def test_key_alarm_freezes(kit: ModuleType) -> None:
    v = _eval(kit, signals=ModeSignals(key_alarm=True))
    assert v.mode is Mode.FROZEN and "key_alarm" in v.reasons


def test_orphans_halt_entries_without_raising_the_mode(kit: ModuleType) -> None:
    snap = kit.snapshot()
    t = kit.tick(snap)
    v = modes.evaluate(kit.risk_ctx(t, orphans=2))
    assert v.mode is Mode.NORMAL and v.halt_entries
    assert any(a.rule == "mode.orphans" and a.action == "HALT_ENTRIES" for a in v.actions)


def test_operator_halt_is_frozen(kit: ModuleType) -> None:
    t = kit.tick(kit.snapshot())
    assert modes.evaluate(kit.risk_ctx(t, halted=True)).mode is Mode.FROZEN


@pytest.mark.parametrize(("fee_float", "run_mode", "expected", "alert"), [
    (40_000_000, RunMode.LIVE, Mode.EXITS_ONLY, False),
    (100_000_000, RunMode.LIVE, Mode.CAUTION, False),
    (200_000_000, RunMode.LIVE, Mode.NORMAL, True),
    (300_000_000, RunMode.LIVE, Mode.NORMAL, False),
    (40_000_000, RunMode.PAPER, Mode.NORMAL, False),        # the fee-float rows are live-only
    (40_000_000, RunMode.LIVE_DRY, Mode.EXITS_ONLY, False),
])
def test_fee_float_rows(kit: ModuleType, fee_float: int, run_mode: RunMode, expected: Mode, alert: bool) -> None:
    pf = kit.portfolio(fee_float=fee_float)
    v = _eval(kit, portfolio=pf, run_mode=run_mode)
    assert v.mode is expected
    assert any(a.rule == "mode.fee_float_alert" for a in v.actions) is alert


def test_book_wide_failure_burst_is_caution(kit: ModuleType) -> None:
    assert _eval(kit, bv=kit.book_view(fail_count_600_book=5)).mode is Mode.CAUTION
    assert _eval(kit, bv=kit.book_view(fail_count_600_book=4)).mode is Mode.NORMAL


def test_entries_halted_until_halts(kit: ModuleType) -> None:
    v = _eval(kit, bv=kit.book_view(entries_halted_until=Block(B)))
    assert v.mode is Mode.NORMAL and v.halt_entries
    assert not _eval(kit, bv=kit.book_view(entries_halted_until=Block(B - 1))).halt_entries


@pytest.mark.parametrize(("nav_now", "mult"), [(100, PPM), (86, PPM), (85, 500_000), (76, 500_000), (75, 250_000), (40, 250_000)])
def test_dd30_governor(kit: ModuleType, nav_now: int, mult: int) -> None:
    tao = 10**9
    daily = ((Block(B - 20 * BLOCKS_PER_DAY), Rao(100 * tao)), (Block(B - 5 * BLOCKS_PER_DAY), Rao(95 * tao)))
    v = _eval(kit, bv=kit.book_view(nav_liq_daily=daily), nav=nav_now * tao)
    assert v.g_max_eff_ppm == RiskCfg().g_max_ppm * mult // PPM
    assert any(a.rule == "mode.dd30" for a in v.actions) is (mult < PPM)


def test_dd30_ignores_samples_older_than_30_days(kit: ModuleType) -> None:
    tao = 10**9
    daily = ((Block(B - 31 * BLOCKS_PER_DAY), Rao(200 * tao)), (Block(B - 2 * BLOCKS_PER_DAY), Rao(100 * tao)))
    assert modes.dd30_ppm(daily, 100 * tao, B) == 0
    assert modes.dd30_ppm(daily, 90 * tao, B) == 100_000


def test_daily_loss_is_caution_and_halts_for_a_day(kit: ModuleType) -> None:
    tao = 10**9
    daily = ((Block(B - BLOCKS_PER_DAY - 5), Rao(100 * tao)),)
    v = _eval(kit, bv=kit.book_view(nav_liq_daily=daily), nav=92 * tao)
    assert v.mode is Mode.CAUTION and v.halt_entries
    act = next(a for a in v.actions if a.rule == "mode.daily_loss")
    assert act.action == "HALT_ENTRIES" and f"halt_until={B + BLOCKS_PER_DAY}" in act.detail
    v2 = _eval(kit, bv=kit.book_view(nav_liq_daily=daily), nav=93 * tao)
    assert v2.mode is Mode.NORMAL
    assert modes.daily_loss_ppm(((Block(B - 10), Rao(100)),), 50, B) is None        # no sample a day old


def test_sleeve_kill_states_are_journaled(kit: ModuleType) -> None:
    bv = kit.book_view(sleeve_stats=(kit.sleeve_stats("carry", "REDUCED", 90_000), kit.sleeve_stats("mom", "ACTIVE")))
    v = _eval(kit, bv=bv)
    acts = [a for a in v.actions if a.rule == "mode.sleeve_kill"]
    assert len(acts) == 1 and "strategy=carry" in acts[0].detail and "budget_mult_ppm=500000" in acts[0].detail
    assert modes.sleeve_budget_mult_ppm("SUSPENDED") == 0
    assert modes.sleeve_budget_mult_ppm("bogus") == 0


def test_regime_throttle_multiplies_g_max_only_when_active(kit: ModuleType) -> None:
    bv = kit.book_view()
    assert modes.g_max_eff_ppm(RiskCfg(), bv, 10**12, B, 500_000) == 800_000
    assert modes.g_max_eff_ppm(RiskCfg(regime_throttle_active=True), bv, 10**12, B, 500_000) == 400_000


# ------------------------------------------------------------------------------------------------- FT11 drill hooks
DRILLS = [
    ("spec_change", {"events": [("SPEC_CHANGED", {"name": "spec_version", "old": "475", "new": "476"})]}, None, Mode.CAUTION),
    ("safe_mode", {"glob": {"safe_mode_until": B + 50}}, None, Mode.FROZEN),
    ("stall_2min", {}, HealthObs(3, 125, 2, 0, 0), Mode.EXITS_ONLY),
    ("finality_lag_10", {}, HealthObs(10, 12, 2, 0, 0), Mode.NORMAL),
    ("rpc_errors_30pct", {}, HealthObs(3, 12, 1, 0, 0), Mode.CAUTION),
    ("stale_ladder", {}, HealthObs(3, 12, 2, 0, 40), Mode.CAUTION),
    ("unexplained_stake_delta", {"key_alarm": True}, None, Mode.FROZEN),
    ("nonce_jump", {"key_alarm": True}, None, Mode.FROZEN),
]


@pytest.mark.parametrize(("name", "setup", "health", "expected"), DRILLS, ids=[d[0] for d in DRILLS])
def test_ft11_drills_produce_the_mode_within_one_tick(kit: ModuleType, name: str, setup: dict, health: HealthObs | None,
                                                      expected: Mode) -> None:
    """Inject health and events into ONE overlay review: the right mode, and the held prune target's EMERGENCY exit
    queued regardless (exits are evaluated every tick)."""
    snap = kit.snapshot(**setup.get("glob", {}))
    target_key = kit.key(1)
    pf = kit.portfolio(positions=[kit.position(target_key, kit.vhk(1), 1_000 * 10**9)])
    events = [kit.event(getattr(ChainEventKind, k), **kw) for k, kw in setup.get("events", [])]
    t = kit.tick(snap, portfolio=pf, events=events)
    proposal = kit.target_book([kit.target(target_key, kit.vhk(1), 0)])
    sig = ModeSignals(health=health, key_alarm=setup.get("key_alarm", False))
    d = StandardOverlay().review(proposal, kit.risk_ctx(t), sig)
    assert d.mode is expected, name
    fe = {f.key: f for f in d.targets.forced}
    assert fe[target_key].urgency is Urgency.EMERGENCY and fe[target_key].rule == "prune_target"
    intents = StandardPlanner(kit.book_cfg()).__call__(d, replace(t, mode=d.mode), frozenset(), kit.RUN, kit.BOOK,
                                                       finality_lag_blocks=health.finality_lag_blocks if health else None)
    if name == "safe_mode":
        assert intents == ()                                      # precomputed only: SafeMode accepts no staking call
        return
    assert len(intents) == 1 and intents[0].kind is OrderKind.REMOVE_STAKE_LIMIT
    if expected is Mode.FROZEN:
        assert not intents[0].allow_partial                       # fill-or-kill emergency exception
    if name == "finality_lag_10":
        assert not intents[0].shielded and intents[0].valid_until == B + 3 + 16
