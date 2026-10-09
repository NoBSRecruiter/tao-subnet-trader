"""taotrader/risk/modes.py - overlay operating modes, kill switches and drawdown governors (WP8; DESIGN.md 3.11).

`evaluate(ctx, run_mode=..., signals=...)` returns the overlay's mode for one tick, whether entries are halted, the
effective gross budget G_MAX_EFF and the journaled RiskActions. It is pure: every input is the RiskContext (snapshot,
derived chain events, frame, book_view, portfolio) plus the optional `ModeSignals`, the inputs the RiskContext does
not carry (HealthObs, live key alarms, the sim_swap drift probe). The Engine (WP7) already floors the mode from the
journaled HealthObs, operator commands and model-drift alarms and passes that floor as `ctx.tick.mode`; this module
only RAISES it. Drills (FT11) inject HealthObs and events through `ModeSignals` / `ctx.tick.events` and get the mode
within the same tick.

Section 3.11 table, as implemented here (row -> mode or action):
- SPEC_CHANGED(spec_version) at this tick -> CAUTION. The validation suite V1-V7 (paper/live tooling) reports a failure
  as a journaled ModelDriftObserved, which the Engine turns into its CAUTION floor; a pass returns to NORMAL.
- SPEC_CHANGED(transaction_version) -> FROZEN in live and live-dry, CAUTION otherwise.
- SafeMode.EnteredUntil >= block (or a SAFE_MODE(active) event) -> FROZEN. Forced exits are still computed by the
  overlay, so the post-SafeMode exits are ready and go first at EnteredUntil + 1.
- HealthObs (signals.health): secs_since_block > stall_halt_s -> EXITS_ONLY, > stall_warn_s -> CAUTION;
  finality lag > finality_caution_blocks -> CAUTION; healthy endpoints 0 -> FROZEN, < 2 -> CAUTION;
  feed_gap_blocks > stale_prune_blocks -> CAUTION (stale prune inputs).
- Stale prune inputs without HealthObs: outside backtests, a gap to the previous snapshot > stale_prune_blocks ->
  CAUTION (backtests run at a 60-block stride by construction).
- EmissionView.model_ok false -> CAUTION; sim_swap drift > 5 bp (signals) -> CAUTION.
- Orphan facts > 0 -> entries halted (until QuarantineCleared resets the count).
- Live key alarm (signals.key_alarm) -> FROZEN (the emergency exception is applied by the Engine/planner).
- Fee float (live and live-dry): < fee_float_exits_rao -> EXITS_ONLY, < fee_float_caution_rao -> CAUTION,
  < fee_float_alert_rao -> MONITOR alert.
- Failures: >= fail_burst_global exact-block failures book-wide within 600 blocks -> CAUTION (the reducer also halts
  entries for 300 blocks; the count itself stays in the 600-block window, so CAUTION lasts at least as long).
  Per-netuid bursts are cooldowns (overlay section H).
- DD30 >= DD_SOFT / DD_HARD on NAV_liq -> G_MAX_EFF x0.5 / x0.25 (the excess is unwound by the section I gross cap
  in NORMAL urgency, largest exit slippage first).
- Daily NAV_liq loss >= DAILY_LOSS -> CAUTION now and an entry halt for 7,200 blocks ("halt_until" action, folded by
  the reducer into BookView.entries_halted_until).
- Operator commands are the Engine's (halt -> FROZEN, exits_only -> EXITS_ONLY); `halted_by_operator` -> FROZEN here
  as well. Sleeve kill states (ACTIVE / REDUCED / SUSPENDED) are maintained by the reducer (SleeveStats) and applied
  as budget multipliers by the allocator; this module journals a MONITOR action per non-active sleeve.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..core.config import RiskCfg
from ..core.events import ChainEventKind, HealthObs
from ..core.protocols import BookView, RiskContext
from ..core.signals import RiskAction
from ..core.units import BLOCKS_PER_DAY, PPM, Block, Mode, Rao, RunMode

__all__ = [
    "DAILY_LOSS_HALT_BLOCKS",
    "DD_HARD_MULT_PPM",
    "DD_SOFT_MULT_PPM",
    "DD_WINDOW_BLOCKS",
    "SIM_SWAP_DRIFT_CAUTION_BP",
    "ModeSignals",
    "ModeVerdict",
    "daily_loss_ppm",
    "dd30_ppm",
    "dd_multiplier_ppm",
    "detail",
    "evaluate",
    "g_max_eff_ppm",
    "health_floors",
    "sleeve_budget_mult_ppm",
]

DD_WINDOW_BLOCKS: Final[int] = 30 * BLOCKS_PER_DAY          # DD30
DD_SOFT_MULT_PPM: Final[int] = 500_000                      # G_MAX_EFF x0.5 at DD_SOFT
DD_HARD_MULT_PPM: Final[int] = 250_000                      # x0.25 at DD_HARD
DAILY_LOSS_HALT_BLOCKS: Final[int] = BLOCKS_PER_DAY         # CAUTION for 7,200 blocks
SIM_SWAP_DRIFT_CAUTION_BP: Final[int] = 5
SPEC_VERSION: Final[str] = "spec_version"
TX_VERSION: Final[str] = "transaction_version"
_LIVE_MODES: Final[tuple[RunMode, ...]] = (RunMode.LIVE, RunMode.LIVE_DRY)
_KILL_MULT_PPM: Final[tuple[tuple[str, int], ...]] = (("ACTIVE", PPM), ("REDUCED", 500_000), ("SUSPENDED", 0))


def detail(**kv: object) -> str:
    """Canonical "k=v;k=v" text for RiskAction.detail (keys in the given order; bools as 0/1)."""
    parts: list[str] = []
    for k, v in kv.items():
        text = str(int(v)) if isinstance(v, bool) else str(v)
        parts.append(f"{k}={text}")
    return ";".join(parts)


@dataclass(frozen=True, slots=True)
class ModeSignals:
    """Inputs the RiskContext does not carry. The Engine floors the journaled HealthObs itself; drills and the live
    runner may inject them here (FT11 drill hooks)."""
    health: HealthObs | None = None
    key_alarm: bool = False                 # live reconciliation: unexplained stake delta, nonce jump, proxy change, ...
    sim_swap_drift_bp: int | None = None    # paper/live drift probe |local/chain - 1| in basis points


@dataclass(frozen=True, slots=True)
class ModeVerdict:
    mode: Mode
    halt_entries: bool
    g_max_eff_ppm: int                      # G_MAX x DD governor x (m_regime when active)
    dd30_ppm: int
    actions: tuple[RiskAction, ...]
    reasons: tuple[str, ...]                # why the mode is above the Engine floor (sorted, unique)


def health_floors(h: HealthObs, cfg: RiskCfg) -> list[tuple[Mode, str]]:
    """The HealthObs rows of section 3.11 (identical to the Engine's floors; used by drills and the live runner)."""
    out: list[tuple[Mode, str]] = []
    if h.healthy_endpoints <= 0:
        out.append((Mode.FROZEN, "no_healthy_endpoint"))
    elif h.healthy_endpoints < 2:
        out.append((Mode.CAUTION, "healthy_endpoints"))
    if h.secs_since_block > cfg.stall_halt_s:
        out.append((Mode.EXITS_ONLY, "stall_halt"))
    elif h.secs_since_block > cfg.stall_warn_s:
        out.append((Mode.CAUTION, "stall_warn"))
    if h.finality_lag_blocks > cfg.finality_caution_blocks:
        out.append((Mode.CAUTION, "finality_lag"))
    if h.feed_gap_blocks > cfg.stale_prune_blocks:
        out.append((Mode.CAUTION, "stale_prune_inputs"))
    return out


def dd30_ppm(nav_daily: Sequence[tuple[Block, Rao]], nav_now: int, block: int) -> int:
    """Drawdown of NAV_liq now from its peak over the last 30 days of daily samples (0 when nav_now is the peak)."""
    peak = max([int(v) for b, v in nav_daily if int(b) > block - DD_WINDOW_BLOCKS] + [int(nav_now)])
    if peak <= 0:
        return 0
    return max(0, (peak - int(nav_now)) * PPM // peak)


def daily_loss_ppm(nav_daily: Sequence[tuple[Block, Rao]], nav_now: int, block: int) -> int | None:
    """Loss of NAV_liq now against the latest daily sample at least 7,200 blocks old (None without one)."""
    ref: int | None = None
    for b, v in nav_daily:
        if int(b) <= block - BLOCKS_PER_DAY:
            ref = int(v)
    if ref is None or ref <= 0:
        return None
    return (ref - int(nav_now)) * PPM // ref


def dd_multiplier_ppm(dd_ppm: int, cfg: RiskCfg) -> int:
    if dd_ppm >= cfg.dd_hard_ppm:
        return DD_HARD_MULT_PPM
    if dd_ppm >= cfg.dd_soft_ppm:
        return DD_SOFT_MULT_PPM
    return PPM


def g_max_eff_ppm(cfg: RiskCfg, book_view: BookView, nav_now: int, block: int, regime_ppm: int | None = None) -> int:
    """G_MAX_EFF = G_MAX x the DD30 governor x m_regime (only when regime_throttle_active and a reading exists)."""
    g = int(cfg.g_max_ppm) * dd_multiplier_ppm(dd30_ppm(book_view.nav_liq_daily, nav_now, block), cfg) // PPM
    if cfg.regime_throttle_active and regime_ppm is not None:
        g = g * max(0, min(int(regime_ppm), PPM)) // PPM
    return g


def sleeve_budget_mult_ppm(state: str) -> int:
    """Sleeve kill state -> budget multiplier (ACTIVE 1, REDUCED 0.5, SUSPENDED 0; unknown states fail closed to 0)."""
    for name, mult in _KILL_MULT_PPM:
        if name == state:
            return mult
    return 0


def evaluate(ctx: RiskContext, *, run_mode: RunMode = RunMode.BACKTEST, signals: ModeSignals | None = None,
             regime_ppm: int | None = None) -> ModeVerdict:
    """The section 3.11 mode table for one tick (see the module docstring)."""
    tick, cfg = ctx.tick, ctx.cfg
    b = int(tick.block)
    sig = signals if signals is not None else ModeSignals()
    rows: list[tuple[Mode, str]] = []
    actions: list[RiskAction] = []
    halt = False

    if ctx.halted_by_operator:
        rows.append((Mode.FROZEN, "operator_halt"))
    for e in tick.events:
        if e.kind is ChainEventKind.SPEC_CHANGED:
            if e.name == TX_VERSION:
                rows.append((Mode.FROZEN if run_mode in _LIVE_MODES else Mode.CAUTION, "tx_version_changed"))
            else:
                rows.append((Mode.CAUTION, "spec_changed"))
        elif e.kind is ChainEventKind.SAFE_MODE and e.flag:
            rows.append((Mode.FROZEN, "safe_mode"))
    smu = tick.raw.glob.safe_mode_until
    if smu is not None and int(smu) >= b:
        rows.append((Mode.FROZEN, "safe_mode"))
    if sig.health is not None:
        rows.extend(health_floors(sig.health, cfg))
    if run_mode is not RunMode.BACKTEST and tick.prev is not None and b - int(tick.prev.block) > cfg.stale_prune_blocks:
        rows.append((Mode.CAUTION, "stale_prune_inputs"))
    if not tick.frame.emission.model_ok:
        rows.append((Mode.CAUTION, "emission_model"))
    if sig.sim_swap_drift_bp is not None and sig.sim_swap_drift_bp > SIM_SWAP_DRIFT_CAUTION_BP:
        rows.append((Mode.CAUTION, "sim_swap_drift"))
    if sig.key_alarm:
        rows.append((Mode.FROZEN, "key_alarm"))
    if run_mode in _LIVE_MODES:
        ff = int(tick.portfolio.fee_float)
        if ff < cfg.fee_float_exits_rao:
            rows.append((Mode.EXITS_ONLY, "fee_float_exits"))
        elif ff < cfg.fee_float_caution_rao:
            rows.append((Mode.CAUTION, "fee_float_caution"))
        elif ff < cfg.fee_float_alert_rao:
            actions.append(RiskAction("mode.fee_float_alert", None, "MONITOR", detail(fee_float_rao=ff)))
    bv = tick.book_view
    if bv.fail_count_600_book >= cfg.fail_burst_global:
        rows.append((Mode.CAUTION, "fail_burst"))
    if ctx.orphans > 0:
        halt = True
        actions.append(RiskAction("mode.orphans", None, "HALT_ENTRIES", detail(orphans=ctx.orphans)))
    if bv.entries_halted_until is not None and int(bv.entries_halted_until) >= b:
        halt = True

    nav = int(tick.nav_liq)
    dd = dd30_ppm(bv.nav_liq_daily, nav, b)
    mult = dd_multiplier_ppm(dd, cfg)
    if mult < PPM:
        actions.append(RiskAction("mode.dd30", None, "CLAMP", detail(dd30_ppm=dd, g_max_mult_ppm=mult)))
    loss = daily_loss_ppm(bv.nav_liq_daily, nav, b)
    if loss is not None and loss >= cfg.daily_loss_ppm:
        rows.append((Mode.CAUTION, "daily_loss"))
        halt = True
        actions.append(RiskAction("mode.daily_loss", None, "HALT_ENTRIES",
                                  detail(loss_ppm=loss, halt_until=b + DAILY_LOSS_HALT_BLOCKS)))
    for st in sorted(bv.sleeve_stats, key=lambda x: x.strategy):
        if st.state != "ACTIVE":
            actions.append(RiskAction("mode.sleeve_kill", None, "MONITOR",
                                      detail(strategy=st.strategy, state=st.state, dd_ppm=st.dd_ppm,
                                             budget_mult_ppm=sleeve_budget_mult_ppm(st.state))))

    floor = tick.mode
    mode = max([floor] + [m for m, _ in rows])
    reasons = tuple(sorted({w for m, w in rows if m > floor or m == mode}))
    if mode > floor:
        actions.insert(0, RiskAction("mode.raise", None, "MODE", detail(mode=mode.name, why=",".join(reasons))))
    g = g_max_eff_ppm(cfg, bv, nav, b, regime_ppm)
    return ModeVerdict(mode=mode, halt_entries=halt or mode >= Mode.CAUTION, g_max_eff_ppm=g, dd30_ppm=dd,
                       actions=tuple(actions), reasons=reasons)
