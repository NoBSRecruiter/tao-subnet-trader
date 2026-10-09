"""WP9 LCW (DESIGN.md section 2.4): behind lcw.enabled = False; every gate rejects with its reason code; AVOID signals
carry the codes; two tranches; exits; memory round trip."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.orders import Urgency
from taotrader.core.protocols import Strategy
from taotrader.core.signals import SignalKind
from taotrader.core.units import AlphaRao, Block
from taotrader.features.gatekeeper import EMA_WARMING, SEED_ANOMALY
from taotrader.strategies.base import History
from taotrader.strategies.launch_lcw import LcwKeyState, LcwMemory, LcwParams, LcwStrategy

ON = {"enabled": True}


def _spec(sx: SimpleNamespace, **over: Any) -> Any:
    state: dict[str, Any] = {"alpha_out_emission": AlphaRao(10**8), "root_prop": Decimal(0), "owner_cut_autolock": True,
                             "metagraph": sx.MetagraphLite(n_miners=20, n_miner_coldkeys=10, top1_coldkey_share_ppm=200_000,
                                                           n_permit_coldkeys=5)}
    state.update(over.pop("state", {}))
    feat: dict[str, Any] = {"a_earn_alpha": 10_000.0, "rp": 0.0, "pool_tao": 1_000.0, "launch_flags": frozenset({EMA_WARMING}),
                            "router_candidates": (sx.candidate(score_ppm_day=20_000),)}
    feat.update(over.pop("feat", {}))
    kw: dict[str, Any] = {"reg_at": sx.B - 20_000, "tao_tao": 1_000, "flow_frac_day": 0.01, "hotkey_alpha_tao": 10_000}
    kw.update(over)
    return sx.Spec(60, state=state, feat=feat, **kw)


def _ctx(sx: SimpleNamespace, spec: Any, **kw: Any) -> Any:
    m = sx.market([spec])
    kw.setdefault("history_blocks", 20_100)
    return m, m.ctx(kw.pop("blk", sx.B), sid="lcw", budget_ppm=50_000, **kw)


def _gates(sx: SimpleNamespace, spec: Any, params: dict[str, Any] | None = None, **kw: Any) -> Any:
    n_pos = kw.pop("n_positions", 0)
    _, ctx = _ctx(sx, spec, **kw)
    strat = LcwStrategy({**ON, **(params or {})})
    return strat.gates(ctx, spec.key, History(ctx), None, n_positions=n_pos)


def test_disabled_by_default_emits_nothing(sx: SimpleNamespace) -> None:
    strat: Strategy = LcwStrategy()
    assert LcwParams().enabled is False
    spec = _spec(sx)
    _, ctx = _ctx(sx, spec)
    out = strat.on_tick(ctx, strat.initial_memory())
    assert out.signals == ()
    # a leftover holding is exited
    _, ctx2 = _ctx(sx, spec, portfolio=sx.holding("lcw", spec.key, alpha_tao=500, cost_tao=5.0))
    (sig,) = strat.on_tick(ctx2, strat.initial_memory()).signals
    assert sig.kind is SignalKind.EXIT and sig.reasons == ("lcw.disabled",)


def test_a_qualifying_launch_passes_every_gate(sx: SimpleNamespace) -> None:
    row = _gates(sx, _spec(sx))
    assert row.failed == (), row.failed
    assert row.phi_ewma == pytest.approx(0.01, rel=1e-6)
    assert row.phi_6h == pytest.approx(0.01, rel=1e-3)
    assert row.mu_hat >= 0.008 and row.g_gross - 2 * 33 / 65_535 >= 0.03
    # V = min(T (G - 2f)/4, V_max(1.5 %), 1 % T, 0.5 x organic buying, 2.5 % NAV): organic buying binds (5 TAO)
    assert row.v_rao == 5 * sx.TAO


GATE_CASES: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], str]] = [
    # (case, spec overrides, params, gates kwargs, expected code)
    ("watch", {"feat": {"since_start_blocks": 80_000}}, {}, {}, "lcw.U1.watch"),
    ("age", {"feat": {"age_reg_blocks": 101 * 7_200}}, {}, {}, "lcw.U1.watch"),
    ("gatekeeper", {"feat": {"launch_flags": frozenset({SEED_ANOMALY})}}, {}, {}, f"lcw.U1.gatekeeper.{SEED_ANOMALY}"),
    ("pool", {"tao_tao": 7_000, "feat": {"pool_tao": 7_000.0}}, {}, {}, "lcw.U2.pool"),
    ("spot", {"feat": {"spot": 0.02}}, {}, {}, "lcw.U3.spot_ratio"),
    ("burn", {"state": {"miner_burned": Decimal("0.5")}}, {}, {}, "lcw.U4.burn"),
    ("metagraph", {"state": {"metagraph": None}}, {}, {}, "lcw.U5.metagraph_missing"),
    ("miners", {"state": {"metagraph": SimpleNamespace(m=3)}}, {}, {}, "lcw.U5.miners"),
    ("coldkeys", {"state": {"metagraph": SimpleNamespace(c=4)}}, {}, {}, "lcw.U5.coldkeys"),
    ("top1", {"state": {"metagraph": SimpleNamespace(t=600_000)}}, {}, {}, "lcw.U5.top1"),
    ("permits", {"state": {"metagraph": SimpleNamespace(p=1)}}, {}, {}, "lcw.U5.permits"),
    ("owner unknown", {"state": {"owner_cut_autolock": False}}, {}, {}, "lcw.U6.owner_unknown"),
    ("owner sells", {"state": {"owner_cut_autolock": False}, "owner_alpha_tao": 1_000, "owner_growth_per_block": -10**7},
     {}, {}, "lcw.U6.owner"),
    ("hotkey", {"feat": {"router_candidates": (), "best_candidate": None}}, {}, {}, "lcw.U7.hotkey"),
    ("epochs", {"growth_ppm": 0}, {}, {}, "lcw.U7.positive_epochs"),
    ("marginal", {"feat": {"router_candidates": (SimpleNamespace(score=5_000),)}}, {}, {}, "lcw.U7.marginal_yield"),
    ("phi_ewma", {"flow_frac_day": 0.002}, {}, {}, "lcw.U8.phi_ewma"),
    ("phi_6h", {}, {"phi_6h_min": 0.05}, {}, "lcw.U8.phi_6h"),
    ("mu_hat", {}, {"mu_hat_min_pct_day": 10.0}, {}, "lcw.U9.mu_hat"),
    ("g_minus_2f", {}, {"g_minus_2f_min": 1.0}, {}, "lcw.U9.g_minus_2f"),
    ("positions", {}, {}, {"n_positions": 2}, "lcw.U10.positions"),
    ("cooldown", {}, {}, {"cooldown": True}, "lcw.book.cooldown.owner"),
]


def _materialise(sx: SimpleNamespace, over: dict[str, Any]) -> dict[str, Any]:
    over = {k: (dict(v) if isinstance(v, dict) else v) for k, v in over.items()}
    st = over.get("state", {})
    mg = st.get("metagraph")
    if isinstance(mg, SimpleNamespace):
        st["metagraph"] = sx.MetagraphLite(n_miners=getattr(mg, "m", 20), n_miner_coldkeys=getattr(mg, "c", 10),
                                           top1_coldkey_share_ppm=getattr(mg, "t", 200_000),
                                           n_permit_coldkeys=getattr(mg, "p", 5))
    ft = over.get("feat", {})
    rc = ft.get("router_candidates")
    if rc and isinstance(rc[0], SimpleNamespace):
        ft["router_candidates"] = (sx.candidate(score_ppm_day=rc[0].score),)
    return over


@pytest.mark.parametrize(("case", "over", "params", "gkw", "code"), GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_each_failed_gate_rejects_with_its_reason_code(sx: SimpleNamespace, case: str, over: dict[str, Any],
                                                       params: dict[str, Any], gkw: dict[str, Any], code: str) -> None:
    spec = _spec(sx, **_materialise(sx, over))
    kw: dict[str, Any] = {}
    if gkw.get("n_positions"):
        kw["n_positions"] = gkw["n_positions"]
    if gkw.get("cooldown"):
        kw["book_view"] = sx.book_view(cooldowns=((spec.key, "owner", Block(sx.B + 100)),))
    row = _gates(sx, spec, params, **kw)
    assert code in row.failed, (case, row.failed)
    assert not row.qualifies


def test_rejections_are_journaled_as_avoid_signals_with_reason_codes(sx: SimpleNamespace) -> None:
    spec = _spec(sx, state={"metagraph": None}, feat={"launch_flags": frozenset({SEED_ANOMALY})})
    _, ctx = _ctx(sx, spec)
    strat = LcwStrategy(ON)
    out = strat.on_tick(ctx, strat.initial_memory())
    (sig,) = out.signals
    assert sig.kind is SignalKind.AVOID and sig.key == spec.key
    assert sig.reasons[0] == "lcw.reject"
    assert "lcw.U5.metagraph_missing" in sig.reasons and f"lcw.U1.gatekeeper.{SEED_ANOMALY}" in sig.reasons


def test_entry_in_two_tranches(sx: SimpleNamespace) -> None:
    spec = _spec(sx)
    m, ctx = _ctx(sx, spec)
    strat = LcwStrategy(ON)
    out = strat.on_tick(ctx, strat.initial_memory())
    (sig,) = out.signals
    assert sig.kind is SignalKind.TARGET and sig.max_size_rao == 5 * sx.TAO // 2
    assert sig.reasons[:2] == ("lcw.entry", "lcw.tranche1") and sig.declares_dilution is True
    assert sig.horizon_blocks == 72_000
    mem = out.memory
    assert isinstance(mem, LcwMemory) and mem.keys[0].tranche1_block == sx.B
    # filled at half size; 360 blocks later the second tranche takes the target to the full V
    port = sx.holding("lcw", spec.key, alpha_tao=250, cost_tao=2.5, opened=sx.B + 5)
    ctx2 = m.ctx(sx.B + 360, sid="lcw", budget_ppm=50_000, portfolio=port, history_blocks=20_500)
    out2 = strat.on_tick(ctx2, mem)
    (sig2,) = out2.signals
    assert sig2.kind is SignalKind.TARGET and sig2.max_size_rao is not None and sig2.max_size_rao >= 5 * sx.TAO * 9 // 10
    assert isinstance(out2.memory, LcwMemory) and out2.memory.keys[0].tranche1_block is None


def test_at_most_two_launch_positions(sx: SimpleNamespace) -> None:
    specs = [_spec(sx), _spec(sx), _spec(sx)]
    for i, sp in enumerate(specs):
        sp.netuid = 60 + i
        sp.reg_at = sx.B - 20_000 + i
    m = sx.market(specs)
    ctx = m.ctx(sid="lcw", budget_ppm=50_000, history_blocks=20_100)
    out = LcwStrategy(ON).on_tick(ctx, LcwMemory())
    kinds = [(int(s.key.netuid), s.kind) for s in out.signals]
    assert kinds == [(60, SignalKind.TARGET), (61, SignalKind.TARGET), (62, SignalKind.AVOID)]
    assert "lcw.U10.positions" in out.signals[2].reasons


@pytest.mark.parametrize(("over", "mem_kw", "events", "code", "urgency"), [
    ({"state": {"emission_enabled": False}}, {"was_enabled": True}, (), "lcw.X1.emission_off", Urgency.HIGH),
    ({}, {}, (ChainEventKind.OWNER_CHANGED,), "lcw.X2.owner_changed", Urgency.HIGH),
    ({"state": {"owner_cut_autolock": False}, "owner_alpha_tao": 1_000, "owner_growth_per_block": -10**8}, {}, (),
     "lcw.X2.owner_dump", Urgency.HIGH),
    ({"state": {"metagraph": "few"}}, {}, (), "lcw.X3.miners", Urgency.NORMAL),
    ({"flow_frac_day": -0.05}, {}, (), "lcw.X5.phi_6h", Urgency.NORMAL),
    ({"feat": {"age_reg_blocks": 864_000 - 144_000}}, {}, (), "lcw.X7.time", Urgency.NORMAL),
])
def test_exits(sx: SimpleNamespace, over: dict[str, Any], mem_kw: dict[str, Any], events: tuple[ChainEventKind, ...],
               code: str, urgency: Urgency) -> None:
    over = _materialise(sx, over)
    if over.get("state", {}).get("metagraph") == "few":
        over["state"]["metagraph"] = sx.MetagraphLite(n_miners=3, n_miner_coldkeys=3, top1_coldkey_share_ppm=300_000,
                                                      n_permit_coldkeys=2)
    spec = _spec(sx, **over)
    evs = tuple(ChainEvent(k, Block(sx.B), key=spec.key, flag=False) for k in events)
    _, ctx = _ctx(sx, spec, events=evs, portfolio=sx.holding("lcw", spec.key, alpha_tao=500, cost_tao=5.0, opened=sx.B - 3_000))
    mem = LcwMemory(keys=(LcwKeyState(key=spec.key, **mem_kw),)) if mem_kw else LcwMemory()
    out = LcwStrategy(ON).on_tick(ctx, mem)
    sig = next(s for s in out.signals if s.key == spec.key)
    assert sig.kind is SignalKind.EXIT and code in sig.reasons and sig.urgency is urgency


def test_stop_from_cost_and_hold_limit(sx: SimpleNamespace) -> None:
    spec = _spec(sx)
    _, ctx = _ctx(sx, spec, portfolio=sx.holding("lcw", spec.key, alpha_tao=500, cost_tao=6.0, opened=sx.B - 3_000))
    sig = next(s for s in LcwStrategy(ON).on_tick(ctx, LcwMemory()).signals if s.key == spec.key)
    assert "lcw.X4.stop_from_cost" in sig.reasons and sig.urgency is Urgency.HIGH
    _, ctx2 = _ctx(sx, spec, portfolio=sx.holding("lcw", spec.key, alpha_tao=500, cost_tao=5.0, opened=sx.B - 8_000))
    sig2 = next(s for s in LcwStrategy({**ON, "x7_hold_days": 1.0}).on_tick(ctx2, LcwMemory()).signals)
    assert "lcw.X7.time" in sig2.reasons


def test_memory_round_trips_through_the_codec(sx: SimpleNamespace) -> None:
    spec = _spec(sx)
    _, ctx = _ctx(sx, spec)
    strat = LcwStrategy(ON)
    out = strat.on_tick(ctx, strat.initial_memory())
    mem = out.memory
    assert isinstance(mem, LcwMemory) and mem.keys and mem.last_signals
    raw = codec.canonical_bytes(mem)
    assert codec.decode_bytes(type(strat.initial_memory()), raw) == mem


def test_attributes() -> None:
    s: Strategy = LcwStrategy()
    assert s.id == "lcw" and s.decide_every_blocks == 360 and s.min_cadence_blocks == 60
    assert s.valid_from_block == 8_466_531 and s.declares_dilution is True
    assert ChainEventKind.EPOCH_DRAIN in s.wake_on
