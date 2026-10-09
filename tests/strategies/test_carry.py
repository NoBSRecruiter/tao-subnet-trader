"""WP9 carry (DESIGN.md section 2.1): hand-computed mu, V* and mu_net on a fixture frame; every entry rule's reason code;
ranking and N_max; the exits; the light wake path; calibration injection; deferred modules; memory round trip."""
from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.fixed import to_ppm
from taotrader.core.orders import Urgency
from taotrader.core.protocols import RouterState, Strategy
from taotrader.core.signals import SignalKind
from taotrader.core.units import PPM, Block, Mode, Ppm, StrategyId
from taotrader.protocol.calibration import Calibration, sealed
from taotrader.protocol.prune import HazardModel, default_lambda_floor
from taotrader.protocol.sellload import SellLoadParams
from taotrader.strategies.base import ParamsError
from taotrader.strategies.carry import CarryKeyState, CarryMemory, CarryParams, CarryStrategy

FEE = 33
TX = 1_028_000 + 837_000


def _market(sx: SimpleNamespace, **spec_kw: Any) -> Any:
    return sx.market([sx.Spec(92, **spec_kw)])


def _key(sx: SimpleNamespace) -> Any:
    return sx.Spec(92).key


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


# ------------------------------------------------------------------------------------------------- hand-computed
def test_hand_computed_mu_vstar_and_mu_net(sx: SimpleNamespace) -> None:
    m = _market(sx)
    ctx = m.ctx()
    strat = CarryStrategy()
    ev = strat.evaluate(ctx)
    k = _key(sx)
    row = ev.rows[k]

    # yield forecast: Y_real * A0 / (A0 + G * H/2) * (1 - haircut); G = 1% of A0 per day, no inflow
    y_real = 6_000 / 1e6
    a0, g, h = 100_000.0, 1_000.0, 5.0
    y_f = y_real * a0 / (a0 + g * h / 2) * 0.9
    # structural sell push: k_w * p * [phi_o c_o AE + phi_m 0.5 (1 - c_o) AE] / y, AE = 7200 * 0.01 alpha/day
    c_o = 11_796 / 65_535
    ae = 7_200 * 0.01
    sell_push = 2.0 * 0.01 * (0.8 * c_o * ae + 0.6 * 0.5 * (1 - c_o) * ae) / 2_000.0
    struct = 0.001 - sell_push
    # hazard: p_reg(36,000) = 10 %, P_bottom = exp(-4 (3 - 1)), R = (2,000 TAO / 1e6 alpha) / 0.01 = 0.2
    lam = 0.1 * math.exp(-8.0) / h
    loss = lam * (1 - 0.2)
    mu = y_f + struct - loss
    mu_lcb = mu - 0.5 * 0.003                 # sigma_model prior 0.30 %/day

    assert row.y_real == pytest.approx(y_real, rel=1e-12)
    assert row.y_f == pytest.approx(y_f, rel=1e-12)
    assert row.sell_push == pytest.approx(sell_push, rel=1e-9)
    assert row.struct == pytest.approx(struct, rel=1e-9)
    assert row.recovery == pytest.approx(0.2, rel=1e-12)
    assert row.hazard_day == pytest.approx(lam, rel=1e-9)
    assert row.mu == pytest.approx(mu, rel=1e-9)
    assert row.sigma_model == pytest.approx(0.003)
    assert row.mu_lcb == pytest.approx(mu_lcb, rel=1e-9)
    assert abs(row.alpha_h_ppm - to_ppm(mu_lcb * h)) <= 1

    # V* = y * (alpha_h - 2f - tx/V0) / 4, one fixed-point iteration, on the (view) pool's 2,000 TAO
    y = 2_000 * sx.TAO
    a_h = row.alpha_h_ppm
    v0 = y * (a_h * 65_535 - 2 * FEE * PPM) // (4 * PPM * 65_535)
    a_h1 = a_h - _ceil_div(TX * PPM, v0)
    v_star = y * (a_h1 * 65_535 - 2 * FEE * PPM) // (4 * PPM * 65_535)
    assert row.v_star_rao == v_star
    caps = [v_star, y // 100, y * 10_000 // 990_000, 800 * sx.TAO * 120_000 // PPM]
    v = min(caps)
    assert row.v_rao == v == v_star                       # V* binds at these values (about 10.6 TAO)

    # RT TEMPORARY: buy V on the pool, sell the alpha back on the ORIGINAL pool (w = 0.5 exact integer path)
    x = 200_000 * sx.TAO
    fee = v * FEE // 65_535
    dy = v - fee
    alpha = x * dy // (y + dy)
    fee_a = alpha * FEE // 65_535
    dx = alpha - fee_a
    back = y * dx // (x + dx)
    rt = _ceil_div((v + TX - back) * PPM, v)
    assert row.rt_ppm == rt
    assert row.mu_net == pytest.approx(mu_lcb - rt / 1e6 / h, rel=1e-9)

    # every entry rule passes (incl. the store-backed C-U6 positive d ln I over 20 epochs) -> one TARGET
    assert row.failed == ()
    (sig,) = ev.signals
    assert sig.kind is SignalKind.TARGET and sig.key == k
    assert sig.max_size_rao == v
    assert sig.weight_ppm == v * PPM // (800 * sx.TAO)
    assert int(sig.edge_ppm_day) == int(to_ppm(row.mu_lcb))
    assert sig.alpha_h_ppm == to_ppm(row.mu_lcb * h)
    assert sig.horizon_blocks == 36_000
    assert sig.declares_dilution is True
    assert sig.hotkey_pref == sx.HK1
    assert sig.strategy == "carry"
    assert sig.reasons[0] == "carry.entry"


def test_router_hotkey_from_book_view_wins_over_best_candidate(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92, hotkeys=(sx.HK1, sx.HK2),
                           feat={"router_candidates": (sx.candidate(sx.HK1), sx.candidate(sx.HK2, score_ppm_day=9_000))})])
    bv = sx.book_view(router=RouterState(choice=((k, sx.HK2),)))
    row = CarryStrategy().evaluate(m.ctx(book_view=bv)).rows[k]
    assert row.hotkey == sx.HK2
    assert row.y_real == pytest.approx(0.009)


# ------------------------------------------------------------------------------------------------- entry rules
ENTRY_CASES: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
    # (case, spec kwargs, ctx/params kwargs, expected reason code)
    ("C-U1", {"state": {"subtoken_enabled": False}}, {}, "C-U1"),
    ("C-U2", {"feat": {"since_start_blocks": 100_000}}, {}, "C-U2"),
    ("C-U3", {"tao_tao": 9_000, "feat": {"pool_tao": 9_000.0}}, {}, "C-U3.pool"),
    ("C-U3 micro", {"tao_tao": 4_000, "feat": {"pool_tao": 4_000.0}}, {"params": {"micro_only": True}}, "C-U3.pool"),
    ("C-U4 ladder", {"feat": {"prune_rank": 7}}, {}, "C-U4.ladder"),
    ("C-U4 rho", {"feat": {"rho": 1.9}}, {}, "C-U4.ladder"),
    ("C-U5", {"state": {"miner_burned": Decimal("0.25")}}, {}, "C-U5"),
    ("C-U6 hotkey", {"feat": {"router_candidates": (), "best_candidate": None}}, {}, "C-U6.no_hotkey"),
    ("C-U6 take", {"feat": {"router_candidates": (SimpleNamespace(),)}}, {}, "C-U6.take"),
    ("C-U6 member", {"feat": {"router_candidates": (SimpleNamespace(),)}}, {}, "C-U6.membership"),
    ("C-U6 yield", {"feat": {"router_candidates": (SimpleNamespace(),)}}, {}, "C-U6.realised_yield"),
    ("C-U6 ratio", {"feat": {"router_candidates": (SimpleNamespace(),)}}, {}, "C-U6.ratio"),
    ("C-U6 dlnI", {"growth_ppm": 0}, {}, "C-U6.positive_dlni"),
    ("C-U7 cooldown", {}, {"cooldown": "owner"}, "C-U7.owner_cooldown"),
    ("C-U7 event", {}, {"event": "owner"}, "C-U7.owner_event"),
    ("C-U8 mode", {}, {"mode": Mode.CAUTION}, "C-U8"),
    ("C-U8 model", {}, {"emission": {"model_ok": False}}, "C-U8"),
    ("C-U9", {}, {"forced_exit": True}, "C-U9"),
    ("C-U10 flow", {"feat": {"flow_1d": -0.01}}, {}, "C-U10"),
    ("C-U10 z", {"feat": {"flow_z_1d": -1.5}}, {}, "C-U10"),
    ("C-U11", {"feat": {"escrow_frac": 0.3}}, {}, "C-U11"),
    ("hurdle", {}, {"params": {"mu_in_pct_day": 0.40}}, "mu_net<mu_in"),
    ("V_min", {}, {"params": {"v_min_tao": 50.0}}, "V<V_min"),
    ("sigma_d", {"feat": {"sigma_d": None}}, {}, "rank.sigma_d"),
    ("target", {}, {"target": True}, "C-U4.target"),
]
CAND_OVERRIDES: dict[str, dict[str, Any]] = {"C-U6 take": {"take_u16": 4_000}, "C-U6 member": {"member_frac_ppm": Ppm(850_000)},
                  "C-U6 yield": {"score_ppm_day": 900}, "C-U6 ratio": {"ratio_ok": False}}


@pytest.mark.parametrize(("case", "spec_kw", "ctx_kw", "code"), ENTRY_CASES, ids=[c[0] for c in ENTRY_CASES])
def test_each_entry_rule_has_a_reason_code(sx: SimpleNamespace, case: str, spec_kw: dict[str, Any],
                                           ctx_kw: dict[str, Any], code: str) -> None:
    k = _key(sx)
    spec_kw = {kk: (dict(v) if isinstance(v, dict) else v) for kk, v in spec_kw.items()}
    if case in CAND_OVERRIDES:
        spec_kw["feat"] = {"router_candidates": (sx.candidate(**CAND_OVERRIDES[case]),)}
    m = sx.market([sx.Spec(92, **spec_kw)])
    kw: dict[str, Any] = {}
    params = ctx_kw.get("params")
    bv_kw: dict[str, Any] = {}
    mem = CarryMemory()
    if ctx_kw.get("cooldown"):
        bv_kw["cooldowns"] = ((k, "owner", Block(sx.B + 7_000)),)
    if ctx_kw.get("forced_exit"):
        bv_kw["recent_forced_exits"] = ((Block(sx.B - 600), k, "prune_A", Urgency.EMERGENCY),)
    if ctx_kw.get("event") == "owner":
        mem = CarryMemory(keys=(CarryKeyState(key=k, owner_event_block=sx.B - 1_000),))
    if "mode" in ctx_kw:
        kw["mode"] = ctx_kw["mode"]
    if "emission" in ctx_kw:
        kw["emission"] = ctx_kw["emission"]
    if ctx_kw.get("target"):
        kw["prune"] = {"target": k}
    ctx = m.ctx(book_view=sx.book_view(**bv_kw), **kw)
    row = CarryStrategy(params).evaluate(ctx, mem).rows[k]
    assert code in row.failed, (case, row.failed)
    assert not row.qualifies


def test_baseline_entry_passes_every_rule(sx: SimpleNamespace) -> None:
    row = CarryStrategy().evaluate(_market(sx).ctx()).rows[_key(sx)]
    assert row.failed == ()


def test_escrow_unknown_is_flagged_not_failed(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92, feat={"escrow_frac": None})])
    ev = CarryStrategy().evaluate(m.ctx())
    row = ev.rows[_key(sx)]
    assert row.failed == ()
    assert "C-U11.escrow_unknown" in ev.signals[0].reasons


def test_burn_bucket_research_flag_halves_size(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92, state={"miner_burned": Decimal("0.3")})])
    plain = CarryStrategy().evaluate(m.ctx()).rows[_key(sx)]
    bucket = CarryStrategy({"allow_burn_bucket": True}).evaluate(m.ctx()).rows[_key(sx)]
    assert "C-U5" in plain.failed
    assert "C-U5" not in bucket.failed and "C-U5.burn_bucket_half" in bucket.flags
    assert bucket.v_rao == min(bucket.v_star_rao, 20 * sx.TAO) // 2


# ------------------------------------------------------------------------------------------------- ranking
def test_rank_by_mu_net_over_sigma_and_n_max(sx: SimpleNamespace) -> None:
    specs = [sx.Spec(n, reg_at=8_000_000 + n, feat={"sigma_d": sd}) for n, sd in ((11, 0.10), (12, 0.04), (13, 0.02))]
    m = sx.market(specs)
    ev = CarryStrategy({"n_max": 2}).evaluate(m.ctx())
    picked = sorted(int(s.key.netuid) for s in ev.signals if s.kind is SignalKind.TARGET)
    # mu_net equal; score = mu_net / max(sigma_d, 3 %): SN13 (3 % floor) > SN12 (4 %) > SN11 (10 %)
    assert picked == [12, 13]
    scores = {int(k.netuid): r.rank_score for k, r in ev.rows.items() if r.rank_score is not None and int(k.netuid) > 10}
    assert scores[13] == pytest.approx(scores[12] * 0.04 / 0.03)


# ------------------------------------------------------------------------------------------------- holdings and exits
def _held_ctx(sx: SimpleNamespace, m: Any, *, cost_tao: float = 10.0, alpha_tao: int = 1_000, opened: int | None = None,
              **kw: Any) -> Any:
    k = _key(sx)
    op = sx.B - 7_200 if opened is None else opened
    return m.ctx(portfolio=sx.holding("carry", k, alpha_tao=alpha_tao, cost_tao=cost_tao, opened=op), **kw)


def _one(ev: Any, kind: SignalKind) -> Any:
    sigs = [s for s in ev.signals if s.kind is kind]
    assert len(sigs) == 1, ev.signals
    return sigs[0]


@pytest.mark.parametrize(("case", "spec_kw", "kw", "code"), [
    ("C-X1 sold", {"feat": {"owner_sold_6h_frac": 0.006}}, {}, "C-X1.owner_sold"),
    ("C-X1 changed", {}, {"event": ChainEventKind.OWNER_CHANGED}, "C-X1.owner_changed"),
    ("C-X1 autolock", {}, {"event": ChainEventKind.AUTOLOCK_TOGGLED}, "C-X1.autolock_off"),
    ("C-X2 large", {}, {"event": ChainEventKind.LARGE_FLOW}, "C-X2.large_outflow"),
    ("C-X2 crash", {"flow_frac_day": -0.2, "feat": {"flow_z_1d": -4.0}}, {}, "C-X2.flow_crash"),
    ("C-X4", {}, {"cost_tao": 20.0}, "C-X4.dd_stop"),
    ("C-X5 rank", {"feat": {"prune_rank": 3}}, {}, "C-X5.prune"),
    ("C-X5 rho", {"feat": {"rho": 1.25}}, {}, "C-X5.prune"),
])
def test_high_exits(sx: SimpleNamespace, case: str, spec_kw: dict[str, Any], kw: dict[str, Any], code: str) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92, **spec_kw)])
    events: tuple[ChainEvent, ...] = ()
    ev_kind = kw.pop("event", None)
    if ev_kind is ChainEventKind.LARGE_FLOW:
        events = (ChainEvent(ev_kind, Block(sx.B), key=k, amount=-50 * sx.TAO, frac_ppm=Ppm(25_000)),)
    elif ev_kind is not None:
        events = (ChainEvent(ev_kind, Block(sx.B), key=k, flag=False),)
    ev = CarryStrategy().evaluate(_held_ctx(sx, m, events=events, **kw))
    sig = _one(ev, SignalKind.EXIT)
    assert sig.urgency is Urgency.HIGH
    assert code in sig.reasons
    st = next(s for s in ev.memory.keys if s.key == k)
    assert st.exiting == int(Urgency.HIGH) and st.risk_exit_block == sx.B


def test_exit_is_sticky_until_flat_and_blocks_reentry_for_3_days(sx: SimpleNamespace) -> None:
    k = _key(sx)
    strat = CarryStrategy()
    m = sx.market([sx.Spec(92, feat={"owner_sold_6h_frac": 0.006})])
    ev1 = strat.evaluate(_held_ctx(sx, m))
    assert _one(ev1, SignalKind.EXIT).urgency is Urgency.HIGH
    # the trigger clears, the position is still held: the exit continues
    m2 = sx.market([sx.Spec(92)])
    ev2 = strat.evaluate(_held_ctx(sx, m2, blk=sx.B + 300), ev1.memory)
    assert _one(ev2, SignalKind.EXIT).urgency is Urgency.HIGH
    # flat now: no exit, and C-U9 blocks re-entry for 3 days
    ev3 = strat.evaluate(m2.ctx(sx.B + 600), ev2.memory)
    assert not [s for s in ev3.signals if s.key == k]
    assert "C-U9" in ev3.rows[k].failed
    ev4 = strat.evaluate(m2.ctx(sx.B + 3 * 7_200 + 300), ev3.memory)
    assert "C-U9" not in ev4.rows[k].failed


def test_decay_exit_after_two_scheduled_evaluations_past_min_hold(sx: SimpleNamespace) -> None:
    k = _key(sx)
    weak = {"router_candidates": (sx.candidate(score_ppm_day=0),)}
    m = sx.market([sx.Spec(92, feat=weak)])
    strat = CarryStrategy()
    op = sx.B - 20_000
    ev1 = strat.evaluate(_held_ctx(sx, m, opened=op))
    assert ev1.rows[k].mu_net < -0.0005
    assert _one(ev1, SignalKind.TARGET).key == k                          # first weak evaluation: hold
    ev_wake = strat.evaluate(_held_ctx(sx, m, opened=op, blk=sx.B + 60), ev1.memory, scheduled=False)
    assert not [s for s in ev_wake.signals if s.kind is SignalKind.EXIT]   # wake runs do not count
    ev2 = strat.evaluate(_held_ctx(sx, m, opened=op, blk=sx.B + 300), ev_wake.memory)
    sig = _one(ev2, SignalKind.EXIT)
    assert sig.urgency is Urgency.NORMAL and "C-S1.decay" in sig.reasons


def test_decay_waits_for_min_hold(sx: SimpleNamespace) -> None:
    weak = {"router_candidates": (sx.candidate(score_ppm_day=0),)}
    m = sx.market([sx.Spec(92, feat=weak)])
    strat = CarryStrategy()
    k = _key(sx)
    port = sx.holding("carry", k, opened=sx.B - 100)
    ev1 = strat.evaluate(m.ctx(portfolio=port))
    ev2 = strat.evaluate(m.ctx(sx.B + 300, portfolio=port), ev1.memory)
    assert not [s for s in ev2.signals if s.kind is SignalKind.EXIT]
    assert next(s for s in ev2.memory.keys if s.key == k).decay_count == 2


def test_hotkey_loss_exit_after_two_epochs(sx: SimpleNamespace) -> None:
    k = _key(sx)
    lost = {"router_candidates": (sx.candidate(eligible=False),), "best_candidate": None}
    m = sx.market([sx.Spec(92, feat=lost)])
    strat = CarryStrategy()
    ev1 = strat.evaluate(_held_ctx(sx, m))
    assert not [s for s in ev1.signals if s.kind is SignalKind.EXIT]
    assert next(s for s in ev1.memory.keys if s.key == k).hk_loss_epochs == 1
    ev2 = strat.evaluate(_held_ctx(sx, m, blk=sx.B + 360), ev1.memory)
    sig = _one(ev2, SignalKind.EXIT)
    assert "C-X3.hotkey_loss" in sig.reasons and sig.urgency is Urgency.NORMAL


def test_max_hold_reunderwrites_a_top_name(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92)])
    port = sx.holding("carry", k, opened=sx.B - 22 * 7_200)
    ev = CarryStrategy().evaluate(m.ctx(portfolio=port))
    sig = _one(ev, SignalKind.TARGET)
    assert "C-S3.reunderwritten" in sig.reasons
    assert next(s for s in ev.memory.keys if s.key == k).entry_block == sx.B
    # a name that no longer qualifies is exited instead
    m2 = sx.market([sx.Spec(92, feat={"flow_z_1d": -2.0})])
    ev2 = CarryStrategy().evaluate(m2.ctx(portfolio=port))
    assert "C-S3.max_hold" in _one(ev2, SignalKind.EXIT).reasons


def test_trade_band_keeps_the_held_value(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92)])
    strat = CarryStrategy()
    v = strat.evaluate(m.ctx()).rows[k].v_rao
    # holding about 90 % of V (inside max(V_min, 25 % of target)): target stays at the held executable value
    alpha_tao = int(v * 0.9 / 0.01 / sx.TAO)
    ev = strat.evaluate(_held_ctx(sx, m, alpha_tao=alpha_tao, cost_tao=alpha_tao * 0.01))
    sig = _one(ev, SignalKind.TARGET)
    held_value = ev.signals[0].max_size_rao
    assert sig.reasons[0] == "carry.hold"
    assert held_value is not None and held_value < v
    # holding a third of V: re-sized to V
    ev2 = strat.evaluate(_held_ctx(sx, m, alpha_tao=alpha_tao // 3, cost_tao=alpha_tao // 3 * 0.01))
    assert _one(ev2, SignalKind.TARGET).max_size_rao == v


# ------------------------------------------------------------------------------------------------- on_tick paths
def test_light_wake_path_returns_last_signals_and_books_events(sx: SimpleNamespace) -> None:
    k = _key(sx)
    other = sx.Spec(93, reg_at=8_000_001).key
    m = sx.market([sx.Spec(92), sx.Spec(93, reg_at=8_000_001)])
    strat: Strategy = CarryStrategy()
    out1 = strat.on_tick(m.ctx(), strat.initial_memory())
    assert out1.signals
    store = m.store(sx.B, 600)
    ev = ChainEvent(ChainEventKind.OWNER_CHANGED, Block(sx.B + 60), key=other)
    out2 = strat.on_tick(m.ctx(sx.B + 60, events=(ev,), store=store), out1.memory)
    assert out2.signals == out1.signals
    assert isinstance(out2.memory, CarryMemory)
    st = next(s for s in out2.memory.keys if s.key == other)
    assert st.owner_event_block == sx.B + 60
    assert store.calls == 0                                            # no evaluation ran
    assert k in {s.key for s in out1.signals}


def test_wake_on_a_held_generation_re_evaluates(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92)])
    strat = CarryStrategy()
    port = sx.holding("carry", k)
    out1 = strat.on_tick(m.ctx(portfolio=port), strat.initial_memory())
    ev = ChainEvent(ChainEventKind.OWNER_CHANGED, Block(sx.B + 60), key=k)
    out2 = strat.on_tick(m.ctx(sx.B + 60, portfolio=port, events=(ev,)), out1.memory)
    sig = next(s for s in out2.signals if s.key == k)
    assert sig.kind is SignalKind.EXIT and "C-X1.owner_changed" in sig.reasons


def test_strategy_attributes(sx: SimpleNamespace) -> None:
    s: Strategy = CarryStrategy()
    assert s.id == "carry"
    assert s.decide_every_blocks == 300 and s.min_cadence_blocks == 60
    assert s.valid_from_block == 8_765_684
    assert s.declares_dilution is True
    assert ChainEventKind.EPOCH_DRAIN in s.wake_on and ChainEventKind.PRUNE_TARGET_CHANGED in s.wake_on
    assert len(s.wake_on) == 10


# ------------------------------------------------------------------------------------------------- calibration
class _Provider:
    def __init__(self, asof: int, kappa_p: str = "2", r_default: str = "0.1", cap: bool = True) -> None:
        hz = HazardModel(cdf_by_r=((Decimal("1.75"), Decimal("0.0625")), (Decimal("0.266"), Decimal(1))),
                         p_open=Decimal("0.0625"), n0=4, lambda_floor_per_block=default_lambda_floor(), valid=True)
        self.c = sealed(Calibration(asof=Block(asof), hazard=hz, kappa_p=Decimal(kappa_p), r_default=Decimal(r_default),
                                    r_cap_formula=cap, tier_b_jump_p_day=Decimal("0.03"), tier_b_jump_size=Decimal("-0.69"),
                                    phi=SellLoadParams(Ppm(0), Ppm(0), Ppm(0)), digest=""))

    def asof(self, block: Block) -> Calibration:
        return self.c


def test_calibration_injection_supplies_kappa_p_r_and_phi(sx: SimpleNamespace) -> None:
    k = _key(sx)
    ctx = _market(sx).ctx()
    row = CarryStrategy(calibration=_Provider(sx.B)).evaluate(ctx).rows[k]
    assert row.p_bottom == pytest.approx(math.exp(-2.0 * 2.0))       # kappa_p = 2
    assert row.recovery == pytest.approx(0.1)                       # min(formula 0.2, r_default 0.1)
    assert row.sell_push == 0.0                                      # phi = 0 from the calibration
    with pytest.raises(LookaheadError):
        CarryStrategy(calibration=_Provider(sx.B + 1)).evaluate(ctx)


def test_sell_load_off_is_the_t3_kill(sx: SimpleNamespace) -> None:
    row = CarryStrategy({"sell_load_on": False}).evaluate(_market(sx).ctx()).rows[_key(sx)]
    assert row.sell_push == 0.0 and row.struct == pytest.approx(0.001)


def test_no_prune_possible_means_no_hazard(sx: SimpleNamespace) -> None:
    row = CarryStrategy().evaluate(_market(sx).ctx(prune={"prune_possible": False})).rows[_key(sx)]
    assert row.hazard_day == 0.0 and row.prune_loss_day == 0.0


# ------------------------------------------------------------------------------------------------- deferred modules
def test_deferred_modules_are_off_by_default(sx: SimpleNamespace) -> None:
    p = CarryParams()
    assert not p.forward_cb_sim and not p.gate_tilt and not p.emission_reenable
    assert p.flow_a1 == 0.0 and p.flow_a2 == 0.0
    assert p.forward_cb_blocks == 2 * 7_200
    row = CarryStrategy().evaluate(_market(sx).ctx()).rows[_key(sx)]
    assert row.extra_day == 0.0


def test_deferred_modules_change_the_score_when_on(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92, feat={"burn_adj_rank": 30, "flow_1d": 0.002, "flow_7d": 0.014})])
    base = CarryStrategy().evaluate(m.ctx()).rows[k]
    tilt = CarryStrategy({"gate_tilt": True}).evaluate(m.ctx()).rows[k]
    assert tilt.extra_day == pytest.approx(0.0005)
    flows = CarryStrategy({"flow_a1": 0.5, "flow_a2": 1.0}).evaluate(m.ctx()).rows[k]
    assert flows.extra_day == pytest.approx(0.5 * 0.002 + 0.014 / 7)
    mem = CarryMemory(keys=(CarryKeyState(key=k, reenable_block=sx.B - 100),))
    re = CarryStrategy({"emission_reenable": True}).evaluate(m.ctx(), mem).rows[k]
    assert re.extra_day == pytest.approx(0.0005)
    fwd = CarryStrategy({"forward_cb_sim": True}).evaluate(m.ctx()).rows[k]
    assert fwd.cb_push != base.cb_push and fwd.cb_push >= 0.0


# ------------------------------------------------------------------------------------------------- sigma_model and memory
def test_sigma_model_prior_until_30_days_then_robust_sd() -> None:
    strat = CarryStrategy()
    b = 10_000_000
    day = 7_200
    rows = tuple((b - d * day, 1, (-2_000, -1_000, 0, 1_000, 2_000)) for d in range(1, 30))
    young = CarryMemory(resid=rows, resid_first_day=(-1, b - 10 * day, -1))
    assert strat._sigma_model(young, 1, b) == pytest.approx(0.003)
    old = CarryMemory(resid=rows, resid_first_day=(-1, b - 31 * day, -1))
    assert strat._sigma_model(old, 1, b) == pytest.approx(1.4826 * 1_000 / 1e6)
    assert strat._sigma_model(old, 0, b) == pytest.approx(0.003)


def test_predictions_mature_into_residuals_and_memory_round_trips(sx: SimpleNamespace) -> None:
    k = _key(sx)
    m = sx.market([sx.Spec(92)])
    strat = CarryStrategy()
    ev1 = strat.evaluate(m.ctx())
    assert len(ev1.memory.preds) == 1 and ev1.memory.preds[0][1] == 92
    later = sx.B + 5 * 7_200 + 300
    ev2 = strat.evaluate(m.ctx(later), ev1.memory)
    assert ev2.memory.resid, "the day-1 prediction matured into a residual"
    day_block, terc, sketch = ev2.memory.resid[0]
    assert day_block == sx.B // 7_200 * 7_200 and len(sketch) == 1
    # realised = d ln(P*I) per day (index +300 ppm per epoch, flat price); residual = realised - mu
    epochs = (later - sx.REF) // 360 - (sx.B - sx.REF) // 360
    realised = epochs * math.log(1.0003) / ((later - sx.B) / 7_200)
    assert sketch[0] == pytest.approx(round((realised - ev1.rows[k].mu) * 1e6), abs=2)
    assert ev2.memory.resid_first_day[terc] == day_block
    for mem in (ev1.memory, ev2.memory):
        raw = codec.canonical_bytes(mem)
        back = codec.decode_bytes(type(strat.initial_memory()), raw)
        assert back == mem
        assert codec.canonical_bytes(back) == raw


def test_memory_with_exiting_states_round_trips(sx: SimpleNamespace) -> None:
    m = sx.market([sx.Spec(92, feat={"owner_sold_6h_frac": 0.01})])
    ev = CarryStrategy().evaluate(_held_ctx(sx, m))
    mem = ev.memory
    assert mem.keys and mem.last_signals
    assert codec.decode_bytes(CarryMemory, codec.canonical_bytes(mem)) == mem


# ------------------------------------------------------------------------------------------------- params
def test_params_validation() -> None:
    assert CarryStrategy({"h_eval_days": 7, "mu_in_pct_day": 0.2}).params.h_eval_days == 7.0
    with pytest.raises(ParamsError, match="unknown"):
        CarryStrategy({"h_eval": 5})
    with pytest.raises(ParamsError, match="expected bool"):
        CarryStrategy({"micro_only": 1})
    with pytest.raises(ParamsError, match="expected int"):
        CarryStrategy({"n_max": 12.0})
    with pytest.raises(ParamsError, match="hysteresis"):
        CarryStrategy({"mu_out_pct_day": 0.5})
    s = CarryStrategy(strategy_id=StrategyId("carry.v2"))
    assert s.id == "carry.v2"

