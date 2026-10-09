"""taotrader/strategies/baselines.py - the section 2.5 baselines and placebos (WP9; DESIGN.md sections 2.5, 8.10).
They run as books in every backtest pass, each under both impact bounds and the three dereg payout variants (that
crossing is WP10's book expansion).

- baseline.cash: hold TAO. It emits nothing, except an EXIT for anything the sleeve still holds.
- baseline.ew_price: equal weight over the overlay-eligible universe (floor sections A-G, minus names the book may not
  enter now), daily rebalance. The return is measured price-only, which is a WP10 report: the physical position is
  staked on the yield hotkey either way. Signals carry the reason "price_only".
- baseline.ew_total: the same universe and weights, measured with the P x I router-hotkey index.
- baseline.yield_x_size: an independent double sort of the eligible universe into terciles, by closed-form NET yield of
  the yield hotkey (protocol.yield_model.closed_form_yield_net) and by pool size (SubnetTAO). It holds the high-yield /
  large-pool cell at equal weight. This is the benchmark carry must beat.
- baseline.prune_blind: ew_total with the prune rules disabled. Universe section D is skipped here; the book's overlay
  must also run with its prune rules off (WP10 book config). It prices the prune engine.
- baseline.random_entry: the placebo. Turnover (entries_per_cycle, rebalance_blocks), holding time (hold_days) and
  sizing (n_positions, weight_ppm, max_size_tao) are parameters, matched by WP10 to the sleeve under test. Picks are
  the eligible names ranked by blake2b(f"{seed}|{block_hash}|{netuid}|{reg_at}"), so the placebo is seeded from
  (seed, block_hash) and is fully deterministic (no `random` module). A pick is held until hold_days expire or its
  generation is gone; the sleeve then exits it.

Every TARGET carries the book's yield hotkey (book_view.router, else Feat.best_candidate) as hotkey_pref.
weight_ppm is a share of the sleeve budget; the allocator, caps and overlay apply on top (sections 3.5, 3.10).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from ..core.config import ExecCfg, RiskCfg
from ..core.events import ChainEventKind
from ..core.orders import Urgency
from ..core.protocols import TickContext
from ..core.signals import Signal, StrategyOutput
from ..core.units import BLOCKS_PER_DAY, PPM, Block, StrategyId, SubnetKey
from ..protocol.yield_model import closed_form_yield_net
from .base import (
    StrategyBase,
    days_to_blocks,
    det_hash,
    entry_blocks,
    exit_signal,
    floor_rows,
    parse_params,
    sleeve_positions,
    sort_signals,
    tao_to_rao,
    terciles,
    weight_signal,
    yield_hotkey,
)

MIN_CADENCE_BLOCKS: Final[int] = 60
NO_WAKE: Final[frozenset[ChainEventKind]] = frozenset()
CASH_ID: Final[StrategyId] = StrategyId("baseline.cash")
RANDOM_ENTRY_ID: Final[StrategyId] = StrategyId("baseline.random_entry")


@dataclass(frozen=True, slots=True)
class BaselineMemory:
    last_block: int | None = None


@dataclass(frozen=True, slots=True)
class EwParams:
    rebalance_blocks: int = BLOCKS_PER_DAY        # daily rebalance

    def problems(self) -> list[str]:
        return [] if self.rebalance_blocks >= 1 else ["rebalance_blocks must be >= 1"]


@dataclass(frozen=True, slots=True)
class RandomEntryParams:
    seed: int = 0                                 # the run seed (RunCfg.seed); with the block hash it seeds every pick
    n_positions: int = 6                          # positions held (sizing match)
    hold_days: float = 5.0                        # holding time (match)
    entries_per_cycle: int = 2                    # new picks per decision (turnover match)
    rebalance_blocks: int = 300                   # decision cadence (turnover match)
    weight_ppm: int = 0                           # per-position share of the sleeve budget; 0 = 1e6 / n_positions
    max_size_tao: float = 0.0                     # per-position cap (sizing match); 0 = none
    matched_to: str = ""                          # the sleeve under test (report label only)

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.n_positions < 1 or self.entries_per_cycle < 0 or self.rebalance_blocks < 1:
            out.append("n_positions >= 1, entries_per_cycle >= 0, rebalance_blocks >= 1")
        if self.hold_days <= 0 or self.max_size_tao < 0 or not 0 <= self.weight_ppm <= PPM:
            out.append("hold_days > 0, max_size_tao >= 0, 0 <= weight_ppm <= 1e6")
        return out


@dataclass(frozen=True, slots=True)
class RandomEntryMemory:
    picks: tuple[tuple[SubnetKey, int], ...] = ()     # (generation, pick block), sorted by key
    last_block: int | None = None


def eligible_universe(ctx: TickContext, risk: RiskCfg, *, skip_prune: bool = False) -> tuple[SubnetKey, ...]:
    """Overlay-eligible generations (floor A-G; D skipped for prune_blind) the book may enter now, sorted."""
    rows = floor_rows(ctx, risk, skip_prune=skip_prune)
    return tuple(k for k in sorted(rows) if rows[k].eligible and not entry_blocks(ctx, k))


class _Baseline(StrategyBase):
    def __init__(self, *, strategy_id: StrategyId, rebalance_blocks: int, exec_cfg: ExecCfg | None,
                 risk: RiskCfg | None) -> None:
        super().__init__(strategy_id=strategy_id, decide_every_blocks=rebalance_blocks, wake_on=NO_WAKE,
                         min_cadence_blocks=MIN_CADENCE_BLOCKS, valid_from_block=Block(0), declares_dilution=False)
        self.exec_cfg = exec_cfg if exec_cfg is not None else ExecCfg()
        self.risk = risk if risk is not None else RiskCfg()

    def initial_memory(self) -> object:
        return BaselineMemory()

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        raise NotImplementedError

    def _exits(self, ctx: TickContext, keep: tuple[SubnetKey, ...], reason: str) -> list[Signal]:
        return [exit_signal(self.id, k, ctx.block, Urgency.NORMAL, (f"{self.id}", reason))
                for k in sorted(sleeve_positions(ctx, self.id)) if k not in keep]

    def _equal(self, ctx: TickContext, keys: tuple[SubnetKey, ...], reasons: tuple[str, ...]) -> list[Signal]:
        n = len(keys)
        out: list[Signal] = []
        for k in keys:
            f = ctx.frame.feats.get(k)
            out.append(weight_signal(self.id, k, ctx.block, PPM // n, reasons=reasons,
                                     hotkey=yield_hotkey(ctx, f) if f is not None else None))
        return out


class CashBaseline(_Baseline):
    """baseline.cash: hold TAO."""

    def __init__(self, params: Mapping[str, object] | None = None, *, strategy_id: StrategyId = CASH_ID,
                 exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None) -> None:
        p = parse_params(EwParams, params, where=str(strategy_id))
        super().__init__(strategy_id=strategy_id, rebalance_blocks=p.rebalance_blocks, exec_cfg=exec_cfg, risk=risk)

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        return StrategyOutput(sort_signals(self._exits(ctx, (), "baseline.cash")), BaselineMemory(int(ctx.block)))


class EwBaseline(_Baseline):
    """Equal weight over the eligible universe (ew_price, ew_total, prune_blind)."""
    KIND: str = "ew_total"
    SKIP_PRUNE: bool = False
    EXTRA_REASON: tuple[str, ...] = ()

    def __init__(self, params: Mapping[str, object] | None = None, *, strategy_id: StrategyId | None = None,
                 exec_cfg: ExecCfg | None = None, risk: RiskCfg | None = None) -> None:
        sid = strategy_id if strategy_id is not None else StrategyId(f"baseline.{self.KIND}")
        p = parse_params(EwParams, params, where=str(sid))
        super().__init__(strategy_id=sid, rebalance_blocks=p.rebalance_blocks, exec_cfg=exec_cfg, risk=risk)

    def targets(self, ctx: TickContext) -> tuple[SubnetKey, ...]:
        return eligible_universe(ctx, self.risk, skip_prune=self.SKIP_PRUNE)

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        keys = self.targets(ctx)
        sigs = self._equal(ctx, keys, (str(self.id),) + self.EXTRA_REASON)
        sigs += self._exits(ctx, keys, "baseline.left_universe")
        return StrategyOutput(sort_signals(sigs), BaselineMemory(int(ctx.block)))


class EwPriceBaseline(EwBaseline):
    KIND = "ew_price"
    EXTRA_REASON = ("price_only",)


class EwTotalBaseline(EwBaseline):
    KIND = "ew_total"


class PruneBlindBaseline(EwBaseline):
    KIND = "prune_blind"
    SKIP_PRUNE = True
    EXTRA_REASON = ("prune_rules_off",)


class YieldXSizeBaseline(EwBaseline):
    """High closed-form net-yield tercile x large pool-size tercile (independent sorts), equal weight."""
    KIND = "yield_x_size"

    def targets(self, ctx: TickContext) -> tuple[SubnetKey, ...]:
        raw = ctx.raw
        yields: dict[SubnetKey, float] = {}
        sizes: dict[SubnetKey, float] = {}
        for k in eligible_universe(ctx, self.risk):
            s = raw.get(k)
            f = ctx.frame.feats.get(k)
            if s is None or f is None:
                continue
            h = yield_hotkey(ctx, f)
            idx = s.hotkey(h) if h is not None else None
            if idx is None:
                continue
            yields[k] = float(closed_form_yield_net(s, raw.glob, idx))
            sizes[k] = float(int(s.pool.tao))
        if not yields:
            return ()
        ty, ts = terciles(yields), terciles(sizes)
        return tuple(k for k in sorted(yields) if ty[k] == 2 and ts[k] == 2)


class RandomEntryBaseline(_Baseline):
    """The random-entry placebo, seeded from (seed, block_hash)."""

    def __init__(self, params: RandomEntryParams | Mapping[str, object] | None = None, *,
                 strategy_id: StrategyId = RANDOM_ENTRY_ID, exec_cfg: ExecCfg | None = None,
                 risk: RiskCfg | None = None) -> None:
        p = params if isinstance(params, RandomEntryParams) else parse_params(RandomEntryParams, params,
                                                                               where=str(strategy_id))
        super().__init__(strategy_id=strategy_id, rebalance_blocks=p.rebalance_blocks, exec_cfg=exec_cfg, risk=risk)
        self.params = p

    def initial_memory(self) -> RandomEntryMemory:
        return RandomEntryMemory()

    def pick_order(self, ctx: TickContext, keys: tuple[SubnetKey, ...]) -> list[SubnetKey]:
        """Eligible keys in their seeded random order for this block: blake2b(seed|block_hash|netuid|reg_at)."""
        h = str(ctx.raw.block_hash)
        return sorted(keys, key=lambda k: (det_hash(self.params.seed, h, int(k.netuid), int(k.reg_at)), k))

    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput:
        p = self.params
        mem = memory if isinstance(memory, RandomEntryMemory) else self.initial_memory()
        b = int(ctx.block)
        hold_blocks = days_to_blocks(p.hold_days)
        picks = [(k, blk) for k, blk in mem.picks if b - blk < hold_blocks and ctx.raw.get(k) is not None]
        have = {k for k, _ in picks}
        eligible = eligible_universe(ctx, self.risk)
        room = min(p.entries_per_cycle, p.n_positions - len(picks))
        for k in self.pick_order(ctx, tuple(x for x in eligible if x not in have)):
            if room <= 0:
                break
            picks.append((k, b))
            have.add(k)
            room -= 1
        picks.sort()
        weight = p.weight_ppm if p.weight_ppm > 0 else PPM // p.n_positions
        cap = tao_to_rao(p.max_size_tao) if p.max_size_tao > 0 else None
        sigs: list[Signal] = []
        for k, blk in picks:
            f = ctx.frame.feats.get(k)
            sigs.append(weight_signal(self.id, k, ctx.block, weight, max_size_rao=cap,
                                      reasons=(str(self.id), f"picked:{blk}"),
                                      hotkey=yield_hotkey(ctx, f) if f is not None else None))
        keep = tuple(k for k, _ in picks)
        sigs += self._exits(ctx, keep, "baseline.random_exit")
        return StrategyOutput(sort_signals(sigs), RandomEntryMemory(picks=tuple(picks), last_block=b))


BASELINES: Final[dict[str, Callable[..., _Baseline]]] = {
    "cash": CashBaseline,
    "ew_price": EwPriceBaseline,
    "ew_total": EwTotalBaseline,
    "yield_x_size": YieldXSizeBaseline,
    "prune_blind": PruneBlindBaseline,
    "random_entry": RandomEntryBaseline,
}
