"""WP9 baselines (DESIGN.md section 2.5): expected weights for cash / ew_price / ew_total / yield_x_size / prune_blind,
the seeded random-entry placebo, exits of names that left the target set, memory round trips, and the factory."""
from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from taotrader.core import codec
from taotrader.core.config import ExecCfg, RiskCfg, SleeveCfg
from taotrader.core.protocols import Strategy
from taotrader.core.signals import SignalKind
from taotrader.core.units import PPM, Block, BlockHash, Ppm, Stage, StrategyId
from taotrader.strategies.base import ParamsError, build_strategy, det_hash
from taotrader.strategies.baselines import (
    BASELINES,
    BaselineMemory,
    CashBaseline,
    EwPriceBaseline,
    EwTotalBaseline,
    PruneBlindBaseline,
    RandomEntryBaseline,
    RandomEntryMemory,
    YieldXSizeBaseline,
)
from taotrader.strategies.carry import CarryStrategy
from taotrader.strategies.launch_lcw import LcwStrategy
from taotrader.strategies.momentum import MomentumStrategy


def _universe(sx: SimpleNamespace, n: int = 6, **per: Any) -> Any:
    specs = [sx.Spec(10 + i, reg_at=8_000_000 + i) for i in range(n)]
    for netuid, over in per.items():
        sp = specs[int(netuid.removeprefix("n")) - 10]
        for k, v in over.items():
            setattr(sp, k, v)
    return sx.market(specs)


def _targets(out: Any) -> dict[int, int]:
    return {int(s.key.netuid): int(s.weight_ppm) for s in out.signals if s.kind is SignalKind.TARGET}


def test_cash_holds_tao(sx: SimpleNamespace) -> None:
    m = _universe(sx)
    s: Strategy = CashBaseline()
    assert s.on_tick(m.ctx(sid="baseline.cash"), s.initial_memory()).signals == ()
    k = sx.Spec(10, reg_at=8_000_000).key
    out = s.on_tick(m.ctx(sid="baseline.cash", portfolio=sx.holding("baseline.cash", k)), s.initial_memory())
    (sig,) = out.signals
    assert sig.kind is SignalKind.EXIT and sig.key == k


@pytest.mark.parametrize("cls", [EwPriceBaseline, EwTotalBaseline])
def test_equal_weight_over_the_eligible_universe(sx: SimpleNamespace, cls: Any) -> None:
    # SN12 fails the floor (burn F), SN13 is in an owner cooldown: 4 of 6 names remain, weight 1e6 // 4 each
    from decimal import Decimal
    m = _universe(sx, n12={"state": {"miner_burned": Decimal("0.6")}})
    k13 = sx.Spec(13, reg_at=8_000_003).key
    bv = sx.book_view(cooldowns=((k13, "owner", Block(sx.B + 100)),))
    s = cls()
    out = s.on_tick(m.ctx(sid=str(s.id), book_view=bv), s.initial_memory())
    assert _targets(out) == {10: 250_000, 11: 250_000, 14: 250_000, 15: 250_000}
    assert all(sig.hotkey_pref == sx.HK1 for sig in out.signals)
    if cls is EwPriceBaseline:
        assert all("price_only" in sig.reasons for sig in out.signals)
    assert s.decide_every_blocks == 7_200 and s.wake_on == frozenset() and s.declares_dilution is False


def test_names_that_leave_the_universe_are_exited(sx: SimpleNamespace) -> None:
    from decimal import Decimal
    m = _universe(sx, n12={"state": {"miner_burned": Decimal("0.6")}})
    k12 = sx.Spec(12, reg_at=8_000_002).key
    s = EwTotalBaseline()
    out = s.on_tick(m.ctx(sid="baseline.ew_total", portfolio=sx.holding("baseline.ew_total", k12)), s.initial_memory())
    ex = [sig for sig in out.signals if sig.kind is SignalKind.EXIT]
    assert [sig.key for sig in ex] == [k12] and "baseline.left_universe" in ex[0].reasons
    assert len(_targets(out)) == 5


def test_prune_blind_ignores_the_prune_floor(sx: SimpleNamespace) -> None:
    # SN11 sits at prune rank 4 (floor D fails); prune_blind keeps it, ew_total drops it
    m = _universe(sx, n11={"feat": {"prune_rank": 4}})
    ew = EwTotalBaseline().on_tick(m.ctx(sid="baseline.ew_total"), BaselineMemory())
    pb = PruneBlindBaseline().on_tick(m.ctx(sid="baseline.prune_blind"), BaselineMemory())
    assert 11 not in _targets(ew) and len(_targets(ew)) == 5
    # prune_blind also holds the ladder bottom itself (SN5, the prune target: it fails only section D)
    assert _targets(pb) == dict.fromkeys((5, *range(10, 16)), PPM // 7)
    assert all("prune_rules_off" in sig.reasons for sig in pb.signals)


def test_yield_x_size_holds_the_high_yield_large_pool_cell(sx: SimpleNamespace) -> None:
    # 9 names: closed-form net yield falls with take (0..8 %), pool size rises 1,000..9,000 TAO by netuid
    specs = []
    for i in range(9):
        specs.append(sx.Spec(10 + i, reg_at=8_000_000 + i, tao_tao=1_000 + 1_000 * ((i * 4) % 9), take_u16=[0, 600, 1_200][i % 3],
                             feat={"pool_tao": float(1_000 + 1_000 * ((i * 4) % 9))}))
    m = sx.market(specs)
    out = YieldXSizeBaseline().on_tick(m.ctx(sid="baseline.yield_x_size"), BaselineMemory())
    sizes = {10 + i: 1_000 + 1_000 * ((i * 4) % 9) for i in range(9)}
    takes = {10 + i: [0, 600, 1_200][i % 3] for i in range(9)}
    top_size = {n for n, t in sizes.items() if t >= 7_000}
    top_yield = {n for n, t in takes.items() if t == 0}              # closed form is the same gross yield everywhere
    want = sorted(top_size & top_yield)
    assert sorted(_targets(out)) == want and want
    assert set(_targets(out).values()) == {PPM // len(want)}


# ------------------------------------------------------------------------------------------------- random entry
def test_random_entry_is_seeded_from_seed_and_block_hash(sx: SimpleNamespace) -> None:
    m = _universe(sx, n=8)
    params = {"seed": 7, "n_positions": 3, "entries_per_cycle": 3, "hold_days": 1.0}
    a = RandomEntryBaseline(params).on_tick(m.ctx(sid="baseline.random_entry"), RandomEntryMemory())
    b = RandomEntryBaseline(params).on_tick(m.ctx(sid="baseline.random_entry"), RandomEntryMemory())
    assert a.signals == b.signals and a.memory == b.memory
    picks = sorted(_targets(a))
    keys = [sx.Spec(n, reg_at=8_000_000 + n - 10).key for n in range(10, 18)]
    h = str(m.snap(sx.B).block_hash)
    expect = sorted(int(k.netuid) for k in sorted(keys, key=lambda k: (det_hash(7, h, int(k.netuid), int(k.reg_at)), k))[:3])
    assert picks == expect
    assert set(_targets(a).values()) == {PPM // 3}
    # a different seed draws a different sample (for this block hash)
    c = RandomEntryBaseline({**params, "seed": 8}).on_tick(m.ctx(sid="baseline.random_entry"), RandomEntryMemory())
    assert sorted(_targets(c)) != picks


def test_random_entry_turnover_hold_and_exit(sx: SimpleNamespace) -> None:
    m = _universe(sx, n=8)
    s = RandomEntryBaseline({"seed": 1, "n_positions": 4, "entries_per_cycle": 2, "hold_days": 1.0, "max_size_tao": 3.0})
    out1 = s.on_tick(m.ctx(sid="baseline.random_entry"), s.initial_memory())
    assert len(_targets(out1)) == 2                                     # at most 2 new picks per cycle
    assert all(sig.max_size_rao == 3 * sx.TAO for sig in out1.signals)
    out2 = s.on_tick(m.ctx(sx.B + 300, sid="baseline.random_entry"), out1.memory)
    assert len(_targets(out2)) == 4 and set(_targets(out1)) <= set(_targets(out2))
    # one day later the first two picks expire; held expired names are exited
    mem2 = out2.memory
    assert isinstance(mem2, RandomEntryMemory)
    first = sorted(k for k, blk in mem2.picks if blk == sx.B)
    port = sx.holding("baseline.random_entry", first[0])
    out3 = s.on_tick(m.ctx(sx.B + 7_200, sid="baseline.random_entry", portfolio=port), mem2)
    ex = [sig for sig in out3.signals if sig.kind is SignalKind.EXIT]
    assert [sig.key for sig in ex] == [first[0]]
    assert isinstance(out3.memory, RandomEntryMemory)
    assert all(blk > sx.B for _, blk in out3.memory.picks)


def test_baseline_memories_round_trip(sx: SimpleNamespace) -> None:
    m = _universe(sx)
    for name, cls in sorted(BASELINES.items()):
        s = cls(None, strategy_id=StrategyId(f"baseline.{name}"))
        out = s.on_tick(m.ctx(sid=f"baseline.{name}"), s.initial_memory())
        raw = codec.canonical_bytes(out.memory)
        assert codec.decode_bytes(type(s.initial_memory()), raw) == out.memory


# ------------------------------------------------------------------------------------------------- factory
def _sleeve(sid: str, params: dict[str, object] | None = None) -> SleeveCfg:
    return SleeveCfg(strategy=StrategyId(sid), stage=Stage.RESEARCH, budget_ppm=Ppm(PPM),
                     params=MappingProxyType(dict(params or {})))


@pytest.mark.parametrize(("sid", "cls"), [
    ("carry", CarryStrategy), ("carry.v2", CarryStrategy), ("momentum", MomentumStrategy), ("lcw", LcwStrategy),
    ("baseline.cash", CashBaseline), ("baseline.ew_price", EwPriceBaseline), ("baseline.ew_total", EwTotalBaseline),
    ("baseline.yield_x_size", YieldXSizeBaseline), ("baseline.prune_blind", PruneBlindBaseline),
    ("baseline.random_entry", RandomEntryBaseline), ("baseline.random_entry.carry", RandomEntryBaseline),
])
def test_build_strategy(sid: str, cls: type) -> None:
    s = build_strategy(_sleeve(sid), exec_cfg=ExecCfg(), risk=RiskCfg())
    assert isinstance(s, cls) and s.id == sid


def test_build_strategy_validates_params_and_ids() -> None:
    s = build_strategy(_sleeve("baseline.random_entry", {"seed": 3, "n_positions": 2}))
    assert isinstance(s, RandomEntryBaseline) and s.params.seed == 3
    with pytest.raises(ParamsError):
        build_strategy(_sleeve("baseline.random_entry", {"seed": "x"}))
    with pytest.raises(ParamsError):
        build_strategy(_sleeve("baseline.ew_total", {"n_max": 3}))
    with pytest.raises(ParamsError, match="unknown strategy"):
        build_strategy(_sleeve("meanrev"))
    with pytest.raises(ParamsError, match="unknown strategy"):
        build_strategy(_sleeve("baseline.nope"))
    lcw = build_strategy(_sleeve("lcw", {"enabled": True}))
    assert isinstance(lcw, LcwStrategy) and lcw.params.enabled
    assert BlockHash("0x00")
