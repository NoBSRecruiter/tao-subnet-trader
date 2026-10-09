"""WP10 backtest.books: config/books.backtest.toml through ops.config_load, expansion, wiring, calibration."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taotrader.backtest import books as bk
from taotrader.core.config import BookCfg, SleeveCfg
from taotrader.core.fixed import DEC
from taotrader.core.orders import Urgency
from taotrader.core.signals import ForcedExit, RiskAction, RiskDecision, TargetBook, TargetPosition
from taotrader.core.units import PPM, Block, BookId, Hotkey, Mode, NetUid, Ppm, Rao, Stage, StrategyId, SubnetKey
from taotrader.data.refine import BRIEF_PRUNE_LOG
from taotrader.ops.config_load import DEFAULT_CONFIG, ConfigError
from taotrader.risk.overlay import StandardOverlay
from taotrader.venues.sim import SimVenue


def test_committed_books_file_loads_and_expands_to_21_books() -> None:
    plan = bk.load_backtest_plan(env={})
    ids = [str(b.book) for b in plan.run.books]
    assert len(ids) == 21 and len(set(ids)) == 21
    assert plan.base_books == ("carry", "momentum", "blend", "base-cash", "base-ew-price", "base-ew-total",
                               "base-yield-size", "base-prune-blind", "base-random")
    assert plan.variants["carry-persist"].impact == "persistent"
    assert plan.book("carry-persist").exec.impact_half_life_blocks is None
    assert plan.book("carry-hl14400").exec.impact_half_life_blocks == 14_400
    assert plan.book("blend-d35").dereg_model == "fixed:350000"
    assert plan.book("carry").exec.impact_half_life_blocks == 0          # headline TEMPORARY
    assert plan.variants["base-ew-total"].base == "base-ew-total"
    assert plan.start_block == 8_765_684 and plan.stride_blocks == 60 and plan.warmup_blocks == 216_000
    # every baseline of section 2.5 is present
    sleeves = {str(s.strategy) for b in plan.run.books for s in b.sleeves}
    for name in ("cash", "ew_price", "ew_total", "yield_x_size", "prune_blind", "random_entry"):
        assert f"baseline.{name}" in sleeves


def test_overrides_reach_the_backtest_table_and_books() -> None:
    plan = bk.load_backtest_plan(env={"TAOTRADER_CFG_SEED": "7"},
                                 cli=["backtest.start_block=8800000", "books.carry.risk.margin_a_blocks=600"])
    assert plan.run.seed == 7 and plan.start_block == 8_800_000
    assert plan.book("carry").risk.margin_a_blocks == 600
    assert plan.book("carry-persist").risk.margin_a_blocks == 600       # variants inherit the base book


@pytest.mark.parametrize("override, msg", [
    ("backtest.bogus=1", "unknown key"),
    ("backtest.impact_books=[\"nope\"]", "unknown base book"),
    ("mode=\"paper\"", "mode"),
    ("backtest.stride_blocks=0", "stride_blocks"),
])
def test_strict_validation(override: str, msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        bk.load_backtest_plan(env={}, cli=[override])


def test_strict_validation_of_book_keys(tmp_path: Path) -> None:
    p = tmp_path / "b.toml"
    p.write_text('mode = "backtest"\n[[books]]\nbook = "x"\ncapital_rao = 1\nfee_float_rao = 1\nsleeves = []\nbogus = 1\n',
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        bk.load_backtest_plan([DEFAULT_CONFIG, p], env={})


def test_expand_books_rejects_id_collisions() -> None:
    a = BookCfg(BookId("a"), Rao(1), Rao(1), ())
    b = BookCfg(BookId("a-persist"), Rao(1), Rao(1), ())
    with pytest.raises(ConfigError, match="collides"):
        bk.expand_books([a, b], bk.ExpandSpec(impact_books=("a",)))


def test_subset_keeps_order_and_rejects_unknown() -> None:
    plan = bk.load_backtest_plan(env={})
    sub = plan.subset(["base-cash", "carry"])
    assert [str(b.book) for b in sub.run.books] == ["carry", "base-cash"]
    assert set(sub.variants) == {"carry", "base-cash"}
    with pytest.raises(KeyError):
        plan.subset(["nope"])


def test_brief_log_registrations_cost_ratio_line() -> None:
    rows = bk.brief_log_registrations()
    assert len(rows) == len(BRIEF_PRUNE_LOG) == 52
    r = next(x for x in rows if x.queued_block == 8_618_670)
    assert r.blocks_since_prev == 46_614 and r.victim_netuid == 90
    assert r.cost_ratio == DEC.subtract(Decimal(2), DEC.divide(Decimal(46_614), Decimal(57_600)))
    assert abs(r.cost_ratio - Decimal("1.1907291666")) < Decimal("1e-9")


def test_calibration_provider_falls_back_to_the_brief_log_and_stays_as_of() -> None:
    cal = bk.calibration_provider(None)
    assert len(cal.inputs.registrations) == 52
    early = cal.asof(Block(7_000_000))
    late = cal.asof(Block(9_240_000))
    assert early.asof <= 7_000_000 and late.asof <= 9_240_000
    assert early.digest != late.digest
    # as-of: registrations after t cannot change asof(t)
    perturbed = bk.calibration_provider(None, registrations=[r for r in bk.brief_log_registrations()
                                                             if r.queued_block < 8_000_000])
    assert perturbed.asof(Block(7_999_000)).digest == cal.asof(Block(7_999_000)).digest


def test_wire_books_builds_engines_and_venues() -> None:
    plan = bk.load_backtest_plan(env={})
    cal = bk.calibration_provider(None)
    rts = bk.wire_books(plan.run, cal, books=["carry", "base-random", "base-prune-blind"])
    by = {str(rt.book): rt for rt in rts}
    assert set(by) == {"carry", "base-random", "base-prune-blind"}
    assert isinstance(by["carry"].venue, SimVenue)
    assert isinstance(by["base-prune-blind"].engine.overlay, bk.PruneBlindOverlay)
    assert isinstance(by["carry"].engine.overlay, StandardOverlay)
    # random entry gets seed = RunCfg.seed unless its params set one
    (sl,) = by["base-random"].engine.cfg.sleeves
    assert sl.params["seed"] == plan.run.seed and sl.params["matched_to"] == "carry"
    with pytest.raises(KeyError):
        bk.wire_books(plan.run, cal, books=["nope"])


def test_feature_engine_uses_the_plan_warm_blocks() -> None:
    plan = bk.load_backtest_plan(env={}, cli=["backtest.feature_warm_blocks=600"])
    fe = bk.feature_engine(bk.calibration_provider(None), plan)
    assert fe.params.warm_blocks == 600


class _FakeOverlay:
    def __init__(self, dec: RiskDecision) -> None:
        self.dec = dec

    def review(self, proposal: TargetBook, ctx: object) -> RiskDecision:
        return self.dec


def _tp(n: int, v: int) -> TargetPosition:
    return TargetPosition(SubnetKey(NetUid(n), Block(1)), Hotkey("0x" + "1" * 64), Rao(v), Urgency.NORMAL,
                          ((StrategyId("baseline.prune_blind"), Ppm(PPM)),))


def test_prune_blind_overlay_drops_only_prune_rules() -> None:
    k1, k2, k3 = (SubnetKey(NetUid(n), Block(1)) for n in (1, 2, 3))
    proposal = TargetBook(Block(10), (_tp(1, 100), _tp(2, 200), _tp(3, 300)))
    reduced = TargetBook(Block(10), (_tp(1, 0), _tp(2, 0), _tp(3, 150)),
                         forced=(ForcedExit(k1, Urgency.EMERGENCY, "prune_A", Ppm(1)),
                                 ForcedExit(k2, Urgency.URGENT, "emission_off", Ppm(1))))
    acts = (RiskAction("prune.entry_rank", k3, "VETO_ENTRY", ""), RiskAction("liquidity.vcap", k3, "CLAMP", ""),
            RiskAction("exit.prune_A", k1, "FORCE_EXIT", ""), RiskAction("exit.emission_off", k2, "FORCE_EXIT", ""))
    ov = bk.PruneBlindOverlay(_FakeOverlay(RiskDecision(reduced, acts, Mode.NORMAL)))
    out = ov.review(proposal, object())  # type: ignore[arg-type]
    vals = {t.key: t.value_rao for t in out.targets.items}
    assert vals[k1] == 100                       # prune exit removed: the proposal target is back
    assert vals[k2] == 0                         # emission exit stays
    assert vals[k3] == 300                       # prune entry veto removed (the clamp alone does not lower targets here)
    assert [f.rule for f in out.targets.forced] == ["emission_off"]
    assert {a.rule for a in out.actions} == {"liquidity.vcap", "exit.emission_off"}


def test_sleeve_cfg_params_are_read_only_after_injection() -> None:
    plan = bk.load_backtest_plan(env={})
    run = plan.run
    sl = bk._sleeves_for(plan.book("base-random"), run)[0]
    assert isinstance(sl, SleeveCfg) and sl.stage is Stage.RESEARCH
    with pytest.raises(TypeError):
        sl.params["seed"] = 1  # type: ignore[index]
