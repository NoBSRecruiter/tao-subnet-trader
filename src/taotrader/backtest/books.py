"""taotrader/backtest/books.py - backtest book expansion and wiring (WP10; DESIGN.md sections 2.5, 8, 8.9, 11 WP10).

`config/books.backtest.toml` is loaded THROUGH ops.config_load (same merge order and override rules as every other run
config: config/default.toml < books file(s) < TAOTRADER_CFG_* environment < CLI "path=value" overrides). Its extra
`[backtest]` table (the replay window and the expansion axes) is removed before the strict RunCfg build and validated
here, key by key (unknown keys rejected):

    [backtest]
    start_block = 8_765_684          # first traded block (a warm-up of `warmup_blocks` before it is ingested, warm=False)
    end_block = 0                    # 0 = the lake's last stored block
    stride_blocks = 60               # ParquetReplay stride (== RunCfg.cadence_blocks)
    warmup_blocks = 216_000          # ParquetReplay warm-up (30 d)
    feature_warm_blocks = 216_000    # FeatureParams.warm_blocks (frame.warm after this much ingested history)
    lake = "data/lake"
    hazard_invalid_from = 0          # 0 = none; else LakeCalibrationProvider(hazard_invalid_from=...)
    impact_books = ["carry", ...]    # base books crossed with every [[backtest.impact]] variant
    dereg_books = ["carry", ...]     # base books crossed with every [[backtest.dereg]] variant
    [[backtest.impact]]  suffix = "", half_life_blocks = 0          # first row = the headline (TEMPORARY)
    [[backtest.dereg]]   suffix = "", model = "formula"             # first row = the headline

Expansion (section 8): every base book runs at the headline impact and dereg model; books listed in `impact_books`
also run once per further impact row (ExecCfg.impact_half_life_blocks replaced; id = base + suffix), books listed in
`dereg_books` once per further dereg row (BookCfg.dereg_model replaced). Variants never cross each other (the
sensitivity grid in backtest.runner does the full product when asked).

Wiring (`wire_books`): one Engine + SimVenue per book; strategies through strategies.base.build_strategy; Router(exec),
risk.liquidity.caps, StandardAllocator, StandardPlanner per book; ONE StandardOverlay shared by every book; the SAME
CalibrationProvider (the as-of LakeCalibrationProvider) is given to the FeatureEngine, the overlay, every Engine and
the carry/LCW strategies. Book-specific conventions from WP9: `baseline.random_entry` gets seed = RunCfg.seed when its
params do not set one; a book whose sleeves are all `baseline.prune_blind` runs behind PruneBlindOverlay (the overlay's
prune rules - forced exits "prune_*" and the "prune.*" entry vetoes - are switched off for that book only).

Calibration (`calibration_provider`): LakeCalibrationProvider.from_lake(lake). When the lake has no registration table
(a lake that has not been through `refine lifecycle`, e.g. the committed mini-lake), the brief section 4.4 prune log
(refine.BRIEF_PRUNE_LOG: prune block, victim, blocks since the previous registration) supplies the registration rows,
with r = 2 - Delta / 57,600 (the brief's cost-ratio line). These are historical facts; the provider still fits only
rows with block < asof, so the as-of rule holds.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from ..core.config import BookCfg, RunCfg, SleeveCfg
from ..core.fixed import DEC
from ..core.protocols import RiskContext, RiskOverlay, Strategy
from ..core.signals import RiskDecision, TargetBook, TargetPosition
from ..core.units import BLOCKS_PER_DAY, Block, BookId, NetUid, RunMode, StrategyId
from ..data.calibration import CalibrationInputs, LakeCalibrationProvider, PreregCalib
from ..data.lake import Lake
from ..data.refine import BRIEF_PRUNE_LOG
from ..engine.engine import Engine
from ..engine.recovery import BookRuntime
from ..features.engine import WARM_BLOCKS, FeatureEngine, FeatureParams
from ..ops.config_load import (
    CONFIG_DIR,
    DEFAULT_CONFIG,
    ConfigError,
    apply_overrides,
    build_run_config,
    cli_overrides,
    deep_merge,
    env_overrides,
    read_toml,
)
from ..portfolio.allocator import StandardAllocator
from ..portfolio.planner import StandardPlanner
from ..protocol.calibration import CalibrationProvider
from ..protocol.prune import RegistrationRow
from ..risk.liquidity import caps
from ..risk.overlay import StandardOverlay
from ..risk.router import Router
from ..strategies.base import build_strategy
from ..venues.sim import SimVenue

__all__ = [
    "BACKTEST_TABLE", "BOOKS_BACKTEST", "BacktestPlan", "BookVariant", "ExpandSpec", "PruneBlindOverlay",
    "brief_log_registrations", "build_engine", "calibration_provider", "expand_books", "feature_engine",
    "load_backtest_plan", "plan_from_raw", "wire_books",
]

BOOKS_BACKTEST: Final[Path] = CONFIG_DIR / "books.backtest.toml"
BACKTEST_TABLE: Final[str] = "backtest"
BRIEF_I_EFF_BLOCKS: Final[int] = 57_600             # the brief's cost-ratio line r = 2 - Delta / 57,600
DEFAULT_START: Final[int] = 8_765_684               # gate_rank32 (spec 441): carry valid_from, emission replica exact
PRUNE_BLIND_ID: Final[str] = "baseline.prune_blind"
RANDOM_ENTRY_ID: Final[str] = "baseline.random_entry"
_BACKTEST_KEYS: Final[frozenset[str]] = frozenset({
    "start_block", "end_block", "stride_blocks", "warmup_blocks", "feature_warm_blocks", "lake", "hazard_invalid_from",
    "impact_books", "dereg_books", "impact", "dereg"})


@dataclass(frozen=True, slots=True)
class ExpandSpec:
    """The expansion axes. Row 0 of each axis is the headline (applied to every base book as written)."""
    impact: tuple[tuple[str, int | None], ...] = (("", 0), ("-hl14400", 2 * BLOCKS_PER_DAY), ("-persist", None))
    dereg: tuple[tuple[str, str], ...] = (("", "formula"), ("-d35", "fixed:350000"), ("-d65", "fixed:650000"))
    impact_books: tuple[str, ...] = ()
    dereg_books: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BookVariant:
    """Report labels of one expanded book."""
    book: BookId
    base: BookId
    impact: str                 # "temporary" | "hl<blocks>" | "persistent"
    dereg: str                  # the BookCfg.dereg_model text
    sleeves: tuple[StrategyId, ...]


@dataclass(frozen=True, slots=True)
class BacktestPlan:
    run: RunCfg                                  # books = the expanded set (base books first, then variants)
    base_books: tuple[BookId, ...]
    variants: Mapping[str, BookVariant]
    spec: ExpandSpec
    start_block: int = DEFAULT_START
    end_block: int = 0                           # 0 = the lake's last block
    stride_blocks: int = 60
    warmup_blocks: int = WARM_BLOCKS
    feature_warm_blocks: int = WARM_BLOCKS
    lake: str = "data/lake"
    hazard_invalid_from: int | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)

    def book(self, book: str) -> BookCfg:
        for b in self.run.books:
            if b.book == book:
                return b
        raise KeyError(book)

    def subset(self, books: Sequence[str]) -> BacktestPlan:
        """The same plan restricted to `books` (order of the plan kept)."""
        want = set(books)
        missing = sorted(want - {str(b.book) for b in self.run.books})
        if missing:
            raise KeyError(f"unknown book(s) {missing}")
        kept = tuple(b for b in self.run.books if b.book in want)
        return replace(self, run=replace(self.run, books=kept),
                       base_books=tuple(b for b in self.base_books if b in want),
                       variants=MappingProxyType({k: v for k, v in self.variants.items() if k in want}))


def impact_label(half_life: int | None) -> str:
    if half_life is None:
        return "persistent"
    return "temporary" if half_life == 0 else f"hl{half_life}"


# ------------------------------------------------------------------------------------------------ loading
def load_backtest_plan(paths: Sequence[str | Path] = (DEFAULT_CONFIG, BOOKS_BACKTEST), *,
                       env: Mapping[str, str] | None = None, cli: Sequence[str] = ()) -> BacktestPlan:
    """Read and merge `paths` (ops.config_load rules), apply TAOTRADER_CFG_* and CLI overrides, split off [backtest],
    build and validate the RunCfg, and expand the books."""
    raw: dict[str, Any] = {}
    for p in paths:
        raw = deep_merge(raw, read_toml(p))
    raw = apply_overrides(raw, env_overrides(os.environ if env is None else env), case_insensitive=True)
    raw = apply_overrides(raw, cli_overrides(cli), case_insensitive=False)
    return plan_from_raw(raw, sources=tuple(str(p) for p in paths))


def _int(v: Any, path: str, *, allow_none: bool = False) -> int | None:
    if allow_none and (v is None or v == "none"):
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ConfigError(f"{path}: expected a non-negative integer")
    return v


def _str_list(v: Any, path: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{path}: expected an array of strings")
    return tuple(v)


def _rows(v: Any, path: str, keys: tuple[str, str]) -> list[dict[str, Any]]:
    if not isinstance(v, list) or not v or not all(isinstance(x, dict) for x in v):
        raise ConfigError(f"{path}: expected a non-empty array of tables")
    for i, r in enumerate(v):
        unknown = sorted(set(r) - set(keys))
        if unknown or set(keys) - set(r):
            raise ConfigError(f"{path}[{i}]: keys must be exactly {list(keys)} (got {sorted(r)})")
        if not isinstance(r[keys[0]], str):
            raise ConfigError(f"{path}[{i}].{keys[0]}: expected a string")
    return v


def plan_from_raw(raw: Mapping[str, Any], *, sources: tuple[str, ...] = ()) -> BacktestPlan:
    data = dict(raw)
    bt = data.pop(BACKTEST_TABLE, {})
    if not isinstance(bt, dict):
        raise ConfigError("backtest: expected a table")
    unknown = sorted(set(bt) - _BACKTEST_KEYS)
    if unknown:
        raise ConfigError(f"backtest: unknown key(s) {unknown}")
    run = build_run_config(data)
    if run.mode is not RunMode.BACKTEST:
        raise ConfigError(f"mode: a backtest plan needs mode = 'backtest' (got {run.mode.value!r})")
    impact = ExpandSpec().impact
    if "impact" in bt:
        impact = tuple((str(r["suffix"]), _int(r["half_life_blocks"], f"backtest.impact[{i}].half_life_blocks",
                                               allow_none=True))
                       for i, r in enumerate(_rows(bt["impact"], "backtest.impact", ("suffix", "half_life_blocks"))))
    dereg = ExpandSpec().dereg
    if "dereg" in bt:
        dereg = tuple((str(r["suffix"]), str(r["model"]))
                      for r in _rows(bt["dereg"], "backtest.dereg", ("suffix", "model")))
    spec = ExpandSpec(impact=impact, dereg=dereg,
                      impact_books=_str_list(bt.get("impact_books", []), "backtest.impact_books"),
                      dereg_books=_str_list(bt.get("dereg_books", []), "backtest.dereg_books"))
    stride = _int(bt.get("stride_blocks", run.cadence_blocks), "backtest.stride_blocks")
    assert stride is not None
    if stride < 1:
        raise ConfigError("backtest.stride_blocks must be >= 1")
    if stride != run.cadence_blocks:
        run = replace(run, cadence_blocks=stride)
    hif = _int(bt.get("hazard_invalid_from", 0), "backtest.hazard_invalid_from")
    books, variants = expand_books(run.books, spec)
    run = replace(run, books=books)
    from ..ops.config_load import validate_run_config
    validate_run_config(run)
    lake = bt.get("lake", "data/lake")
    if not isinstance(lake, str) or not lake:
        raise ConfigError("backtest.lake: expected a path string")
    return BacktestPlan(
        run=run, base_books=tuple(b.book for b in books if variants[b.book].base == b.book), variants=variants,
        spec=spec, start_block=int(_int(bt.get("start_block", DEFAULT_START), "backtest.start_block") or 0),
        end_block=int(_int(bt.get("end_block", 0), "backtest.end_block") or 0), stride_blocks=stride,
        warmup_blocks=int(_int(bt.get("warmup_blocks", WARM_BLOCKS), "backtest.warmup_blocks") or 0),
        feature_warm_blocks=int(_int(bt.get("feature_warm_blocks", WARM_BLOCKS), "backtest.feature_warm_blocks") or 0),
        lake=lake, hazard_invalid_from=hif or None, sources=sources)


def expand_books(base: Sequence[BookCfg], spec: ExpandSpec) -> tuple[tuple[BookCfg, ...], Mapping[str, BookVariant]]:
    """Base books (headline impact and dereg as configured) + impact variants + dereg variants (section 8)."""
    ids = {str(b.book) for b in base}
    for name, listed in (("impact_books", spec.impact_books), ("dereg_books", spec.dereg_books)):
        missing = sorted(set(listed) - ids)
        if missing:
            raise ConfigError(f"backtest.{name}: unknown base book(s) {missing}")
    for axis, rows in (("impact", [s for s, _ in spec.impact]), ("dereg", [s for s, _ in spec.dereg])):
        if len(set(rows)) != len(rows) or rows[0] != "":
            raise ConfigError(f"backtest.{axis}: suffixes must be unique and the first (headline) row's must be ''")
    out: list[BookCfg] = []
    variants: dict[str, BookVariant] = {}

    def add(b: BookCfg, base_id: BookId) -> None:
        if b.book in variants:
            raise ConfigError(f"expanded book id {b.book!r} collides with another book")
        out.append(b)
        variants[b.book] = BookVariant(b.book, base_id, impact_label(b.exec.impact_half_life_blocks), b.dereg_model,
                                       tuple(s.strategy for s in b.sleeves))

    for b in base:
        add(b, b.book)
    for b in base:
        if b.book in spec.impact_books:
            for suffix, hl in spec.impact[1:]:
                add(replace(b, book=BookId(f"{b.book}{suffix}"), exec=replace(b.exec, impact_half_life_blocks=hl)), b.book)
        if b.book in spec.dereg_books:
            for suffix, model in spec.dereg[1:]:
                add(replace(b, book=BookId(f"{b.book}{suffix}"), dereg_model=model), b.book)
    return tuple(out), MappingProxyType(variants)


# ------------------------------------------------------------------------------------------------ calibration
def brief_log_registrations(log_rows: Sequence[tuple[int, int, int]] = BRIEF_PRUNE_LOG) -> tuple[RegistrationRow, ...]:
    """Registration rows from the brief section 4.4 prune log: r = 2 - Delta / 57,600 (exact Decimal)."""
    out: list[RegistrationRow] = []
    for p, victim, delta in sorted(log_rows):
        r = DEC.subtract(Decimal(2), DEC.divide(Decimal(delta), Decimal(BRIEF_I_EFF_BLOCKS)))
        out.append(RegistrationRow(Block(p), NetUid(victim), r, int(delta)))
    return tuple(out)


def calibration_provider(lake: Lake | None, *, hazard_invalid_from: int | None = None,
                         prereg: PreregCalib | None = None,
                         registrations: Sequence[RegistrationRow] | None = None) -> LakeCalibrationProvider:
    """The as-of LakeCalibrationProvider (see the module docstring for the brief-log fallback)."""
    pre = prereg or PreregCalib.load()
    inputs = (CalibrationInputs.from_lake(lake, r_default=pre.r_default) if lake is not None else CalibrationInputs())
    if registrations is not None:
        inputs = replace(inputs, registrations=tuple(registrations))
    elif not inputs.registrations:
        inputs = replace(inputs, registrations=brief_log_registrations())
    return LakeCalibrationProvider(inputs, prereg=pre, hazard_invalid_from=hazard_invalid_from)


def feature_engine(calibration: CalibrationProvider, plan: BacktestPlan | None = None, *,
                   warm_blocks: int | None = None) -> FeatureEngine:
    """The shared FeatureEngine (thresholds from the default RiskCfg/ExecCfg, as WP5 specifies)."""
    wb = warm_blocks if warm_blocks is not None else (plan.feature_warm_blocks if plan is not None else WARM_BLOCKS)
    return FeatureEngine(calibration, replace(FeatureParams.from_config(), warm_blocks=wb))


# ------------------------------------------------------------------------------------------------ overlays
_PRUNE_PREFIXES: Final[tuple[str, ...]] = ("prune", "exit.prune")


def _is_prune_rule(rule: str) -> bool:
    return rule.startswith(_PRUNE_PREFIXES)


class PruneBlindOverlay:
    """The prune_blind baseline's overlay (WP9 note): the wrapped overlay's prune rules are switched off - prune forced
    exits are dropped and every key whose only vetoes/exits came from prune rules gets its proposal target back.
    Everything else (modes, emission, owner, liquidity, aggregates) is unchanged."""

    def __init__(self, inner: RiskOverlay) -> None:
        self.inner = inner

    def review(self, proposal: TargetBook, ctx: RiskContext, *args: Any, **kwargs: Any) -> RiskDecision:
        dec = self.inner.review(proposal, ctx, *args, **kwargs)
        prune_keys = {f.key for f in dec.targets.forced if _is_prune_rule(f.rule)}
        prune_keys |= {a.key for a in dec.actions if a.key is not None and _is_prune_rule(a.rule)
                       and a.action in ("FORCE_EXIT", "VETO_ENTRY")}
        other = {f.key for f in dec.targets.forced if not _is_prune_rule(f.rule)}
        other |= {a.key for a in dec.actions if a.key is not None and not _is_prune_rule(a.rule)
                  and a.action in ("FORCE_EXIT", "VETO_ENTRY")}
        restore = prune_keys - other
        if not restore:
            return dec
        items: dict[Any, TargetPosition] = {t.key: t for t in dec.targets.items}
        for key in sorted(restore, key=lambda k: (k.netuid, k.reg_at)):
            prop = proposal.get(key)
            if prop is not None:
                items[key] = prop
            else:
                items.pop(key, None)
        forced = tuple(f for f in dec.targets.forced if not _is_prune_rule(f.rule))
        actions = tuple(a for a in dec.actions if not (_is_prune_rule(a.rule) and a.key in restore
                                                       and a.action in ("FORCE_EXIT", "VETO_ENTRY")))
        tb = replace(dec.targets, items=tuple(items[k] for k in sorted(items, key=lambda k: (k.netuid, k.reg_at))),
                     forced=forced)
        return replace(dec, targets=tb, actions=actions)


# ------------------------------------------------------------------------------------------------ wiring
def _sleeves_for(book: BookCfg, run: RunCfg) -> tuple[SleeveCfg, ...]:
    out: list[SleeveCfg] = []
    for s in book.sleeves:
        if str(s.strategy).startswith(RANDOM_ENTRY_ID) and "seed" not in s.params:
            s = replace(s, params=MappingProxyType({**dict(s.params), "seed": int(run.seed)}))
        out.append(s)
    return tuple(out)


def build_engine(book: BookCfg, run: RunCfg, calibration: CalibrationProvider, overlay: RiskOverlay, *,
                 extra_strategies: Sequence[Strategy] = ()) -> Engine:
    """One book's Engine with the WP8/WP9 implementations (extra_strategies: canaries and placebos for bias tests)."""
    sleeves = _sleeves_for(book, run)
    book = replace(book, sleeves=sleeves)
    given = {s.id for s in extra_strategies}
    strategies: list[Strategy] = list(extra_strategies)
    for s in sleeves:
        if s.strategy not in given:
            strategies.append(build_strategy(s, exec_cfg=book.exec, risk=book.risk, calibration=calibration))
    ov: RiskOverlay = overlay
    if sleeves and all(str(s.strategy).startswith(PRUNE_BLIND_ID) for s in sleeves):
        ov = PruneBlindOverlay(overlay)
    return Engine(run_id=run.run_id, cfg=book, mode=run.mode, strategies=strategies, router=Router(book.exec), caps=caps,
                  allocator=StandardAllocator(book, run_mode=run.mode, live_sleeves=run.live.sleeves), overlay=ov,
                  planner=StandardPlanner(book, run_mode=run.mode, live=run.live), calibration=calibration)


def wire_books(run: RunCfg, calibration: CalibrationProvider, *, books: Sequence[str] | None = None,
               overlay: RiskOverlay | None = None,
               extra_strategies: Mapping[str, Sequence[Strategy]] | None = None,
               overlays: Mapping[str, RiskOverlay] | None = None) -> list[BookRuntime]:
    """BookRuntimes (Engine + SimVenue) for `books` (default: all) of the run config. `overlays` replaces the shared
    overlay for the named books (tests and ablations, e.g. an overlay-off book for FT5)."""
    ov: RiskOverlay = overlay if overlay is not None else StandardOverlay(calibration, run_mode=run.mode, seed=run.seed)
    want = None if books is None else set(books)
    out: list[BookRuntime] = []
    for b in run.books:
        if want is not None and b.book not in want:
            continue
        extra = (extra_strategies or {}).get(str(b.book), ())
        eng = build_engine(b, run, calibration, (overlays or {}).get(str(b.book), ov), extra_strategies=extra)
        out.append(BookRuntime(engine=eng, venue=SimVenue(b.book, b.exec, seed=run.seed)))
    if want is not None:
        missing = sorted(want - {str(rt.book) for rt in out})
        if missing:
            raise KeyError(f"unknown book(s) {missing}")
    return out
