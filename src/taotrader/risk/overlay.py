"""taotrader/risk/overlay.py - StandardOverlay: the RiskOverlay of DESIGN.md section 3 (WP8). Final authority on the
physical book; monotone (it may lower targets, add forced exits, veto entries and raise the mode, never more).

`review(proposal, ctx)` applies the rules in this FIXED order, each emitting RiskActions when it binds:
1. modes (risk.modes, section 3.11): the mode, entry halts and G_MAX_EFF (DD governor, regime throttle when active);
2. universe floor: sections A-G re-evaluated PER BOOK with features.universe (the book's RiskCfg thresholds, Gatekeeper
   flags and router candidates from the frame, emission bans from BookView cooldowns), the book's router hotkey for
   the target (section G: eligible candidate passing Q_MAX at the target size), and section H cooldowns from
   ctx.book_view (every active cooldown, >= fail_burst_netuid exact-block failures on the netuid within 600 blocks,
   abnormal fills, stale data, the runtime prune-target alarm);
3. prune (risk.prune_guard; section 3.3): never hold the target, Tier A, backstop, Tier B (MC, when enabled), entry
   floor (target, zones, MC 7-day rule, ladder bucket), expected-loss and P_prune MONITOR rows;
4. emission, burn and launch age (risk.emission_guard; section 3.4);
5. owner (risk.owner_guard; section 3.6), incl. the MONITOR m_owner;
6. liquidity and caps (risk.liquidity; section 3.5): an increase may not exceed V_cap; MONITOR m_gate / m_trd;
7. section I aggregates (risk.liquidity.apply_aggregates): buckets, gross <= G_MAX_EFF * NAV_liq, N_eff (new names
   only), hold budget (exit shortfall). The forced exits of steps 3-5 (and dissolved generations) already set their
   targets to 0 (or trim_to) here, so a position being force-sold never pushes these cuts onto healthy holdings.
Then: CAUTION and above, or any entry halt, cap every target at its current executable value; forced exits set the
target to 0 with the exit's urgency. A forced exit is listed even when its generation is not in the proposal (the
planner reads TargetBook.forced). Forced-exit precedence per generation: highest urgency, then the widest exit
slippage budget, then the rule name.

Netting transfers of the proposal are kept only for generations the overlay left untouched.

Post-spec burn-in (section 3.10 step 2): while RiskContext.burn_in_until >= block every target is halved (m_burnin =
0.5). This is the allocator's rule, applied here because only the RiskContext carries burn_in_until.

Calibration: constructed with a CalibrationProvider; `asof(block)` (check_asof enforced) supplies R (FT10 cap),
kappa_p and the hazard / Tier B jump parameters. Without one, R uses RiskCfg.r_default_ppm, kappa_p its prior 4, and
the MC cannot run (tier_b_enabled then requires a provider).
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from ..core.config import RiskCfg
from ..core.orders import OrderKind, OrderRecord, Urgency
from ..core.protocols import RiskContext, TickContext
from ..core.signals import ForcedExit, RiskAction, RiskDecision, TargetBook, TargetPosition
from ..core.state import Quality
from ..core.units import BLOCKS_PER_DAY, PPM, RAO_PER_TAO, Block, Mode, Ppm, Rao, RunMode, SubnetKey
from ..features.gatekeeper import VETO_ALL
from ..features.universe import UniverseInputs, UniverseParams, UniverseRow
from ..features.universe import evaluate as universe_evaluate
from ..protocol.calibration import Calibration, CalibrationProvider, check_asof
from .emission_guard import (
    ban_cooldowns,
    burn_exits,
    emission_ban_until,
    emission_exits,
    entry_bans,
    lcw_exits,
    lcw_only_keys,
    wave_halt,
)
from .hazard_mc import H_7D_BLOCKS, McParams, McResult, QCache, mc_seed, run_mc, sigma_inputs
from .liquidity import (
    Caps,
    aggregate_limits,
    apply_aggregates,
    exit_shortfall_ppm,
    holdings,
    lcw_share_ppm,
    monitored,
    value_to_alpha,
    vcap_detail,
)
from .modes import ModeSignals, detail, evaluate
from .owner_guard import owner_triggers
from .prune_guard import (
    PruneContext,
    entry_vetoes,
    expected_loss_ppm_day,
    held_exits,
    prune_context,
    ratio_ppm,
    recovery,
)
from .regime_throttle import RegimeThrottle
from .router import q_max_ok

__all__ = ["ABNORMAL_FILL_RULE", "FAIL_BURST_RULE", "StandardOverlay"]

ABNORMAL_FILL_RULE: Final[str] = "abnormal_fill"
FAIL_BURST_RULE: Final[str] = "fail_burst"              # == engine.reducer's rule for the same cooldown
ABNORMAL_COOLDOWN_BLOCKS: Final[int] = BLOCKS_PER_DAY
FAIL_COOLDOWN_BLOCKS: Final[int] = BLOCKS_PER_DAY
ABNORMAL_ABS_PPM: Final[int] = 1_000                    # FT7 tolerance: 0.1% + 25% of the modelled shortfall
ABNORMAL_REL_PPM: Final[int] = 250_000
STALE_QUALITY: Final[Quality] = Quality.CHAIN_STALL_GAP
_MEMO_MAX: Final[int] = 8
BURN_IN_MULT_PPM: Final[int] = 500_000                  # post-spec burn-in halves budgets (section 3.10 step 2)

_Veto = Callable[[SubnetKey, str, str], None]


@dataclass(frozen=True, slots=True)
class _Exit:
    key: SubnetKey
    urgency: Urgency
    rule: str
    slip_ppm: int
    trim_to: int | None


class StandardOverlay:
    """The RiskOverlay (core.protocols.RiskOverlay). One instance may serve every book of a run; it keeps no book
    state (only memo caches of pure functions)."""

    def __init__(self, calibration: CalibrationProvider | None = None, *, run_mode: RunMode = RunMode.BACKTEST,
                 seed: int = 0, mc_paths: int | None = None, regime: RegimeThrottle | None = None) -> None:
        self.calibration = calibration
        self.run_mode = run_mode
        self.seed = int(seed)
        self.mc_paths = mc_paths
        self.regime = regime if regime is not None else RegimeThrottle()
        self._cal_memo: dict[int, Calibration] = {}
        self._mc_memo: dict[tuple[str, int, str, int], McResult] = {}
        self._q_cache: QCache = {}

    # ------------------------------------------------------------------ inputs
    def _cal(self, block: int) -> Calibration | None:
        if self.calibration is None:
            return None
        c = self._cal_memo.get(block)
        if c is None:
            c = self.calibration.asof(Block(block))
            check_asof(c, Block(block))
            if len(self._cal_memo) >= _MEMO_MAX:
                self._cal_memo.clear()
            self._cal_memo[block] = c
        return c

    def _mc(self, tick: TickContext, cfg: RiskCfg, cal: Calibration | None, horizon: int) -> McResult | None:
        if cal is None:
            return None
        paths = self.mc_paths if self.mc_paths is not None else int(cfg.mc_paths)
        key = (str(tick.raw.block_hash), horizon, cal.digest, paths)
        hit = self._mc_memo.get(key)
        if hit is not None:
            return hit
        params = McParams.from_calibration(cal, paths)
        res = run_mc(tick.raw, cal.hazard, horizon, params, mc_seed(self.seed, str(tick.raw.block_hash)),
                     sigma_inputs(tick.store, tick.raw, tick.frame.feats), self._q_cache)
        if len(self._mc_memo) >= _MEMO_MAX:
            self._mc_memo.clear()
        self._mc_memo[key] = res
        return res

    # ------------------------------------------------------------------ review
    def review(self, proposal: TargetBook, ctx: RiskContext, signals: ModeSignals | None = None) -> RiskDecision:
        tick, cfg = ctx.tick, ctx.cfg
        if cfg.tier_b_enabled and self.calibration is None:
            raise ValueError("tier_b_enabled needs a CalibrationProvider (hazard and Tier B jump parameters)")
        b = int(tick.block)
        raw, view, bv = tick.raw, tick.view, tick.book_view
        cal = self._cal(b)
        hold = holdings(tick)
        current = {k: int(h.value) for k, h in hold.items()}
        items = {t.key: t for t in proposal.items}
        val = {k: int(t.value_rao) for k, t in items.items()}
        acts: list[RiskAction] = []
        exits: dict[SubnetKey, _Exit] = {}
        vetoed: set[SubnetKey] = set()

        def veto(key: SubnetKey, rule: str, text: str) -> None:
            if key in val and val[key] > current.get(key, 0):
                val[key] = current.get(key, 0)
                vetoed.add(key)
                acts.append(RiskAction(rule, key, "VETO_ENTRY", text))

        def force(key: SubnetKey, urgency: Urgency, rule: str, slip: int, text: str, trim_to: int | None = None) -> None:
            acts.append(RiskAction(f"exit.{rule}", key, "FORCE_EXIT", f"urgency={int(urgency)};slip_ppm={slip};{text}"))
            new = _Exit(key, urgency, rule, int(slip), trim_to)
            old = exits.get(key)
            if old is None or (int(new.urgency), new.slip_ppm, new.rule) > (int(old.urgency), old.slip_ppm, old.rule):
                exits[key] = new

        # ---- 1. modes (and the regime throttle reading)
        reading = self.regime.reading(tick.store, raw, cfg)
        if tick.prev is None or int(tick.prev.block) // BLOCKS_PER_DAY < b // BLOCKS_PER_DAY:
            acts.append(RiskAction("regime.throttle", None, "MONITOR",
                                   detail(alpha0_ppm_day="none" if reading.alpha0_ppm_day is None else reading.alpha0_ppm_day,
                                          m_regime_ppm=reading.m_regime_ppm, days=reading.n_days,
                                          active=cfg.regime_throttle_active)))
        verdict = evaluate(ctx, run_mode=self.run_mode, signals=signals, regime_ppm=reading.m_regime_ppm)
        acts.extend(verdict.actions)
        mode = verdict.mode
        halt = verdict.halt_entries
        if ctx.burn_in_until is not None and int(ctx.burn_in_until) >= b:     # section 3.10 step 2 (m_burnin)
            for k in sorted(val):
                val[k] = val[k] * BURN_IN_MULT_PPM // PPM
            acts.append(RiskAction("allocator.burn_in", None, "CLAMP",
                                   detail(until=int(ctx.burn_in_until), mult_ppm=BURN_IN_MULT_PPM)))

        increase = [k for k in sorted(val) if val[k] > current.get(k, 0)]
        feats = dict(tick.frame.feats)
        pc = prune_context(raw, cfg)
        lcw_ppm = {k: lcw_share_ppm(t.attribution) for k, t in items.items()}

        # ---- 2. universe floor A-G (per book) and section H
        if pc.runtime_target is not None or not tick.frame.prune.runtime_agrees:
            halt = True
            acts.append(RiskAction("prune.runtime_mismatch", pc.runtime_target, "HALT_ENTRIES",
                                   detail(local="none" if pc.target is None else int(pc.target.netuid),
                                          runtime="none" if raw.glob.runtime_prune_target is None
                                          else int(raw.glob.runtime_prune_target))))
        if increase:
            rows = self._universe(tick, cfg, pc)
            for k in increase:
                s = raw.get(k)
                feat = feats.get(k)
                if s is None or feat is None or (s.quality & STALE_QUALITY):
                    veto(k, "cooldown.stale_data", detail(feat=feat is not None, snapshot=s is not None))
                    continue
                row = rows.get(k)
                failed = self._failed_sections(row, lcw_ppm.get(k, 0), feat.launch_flags)
                if failed:
                    veto(k, "universe." + "".join(failed), detail(failed=",".join(failed)))
                    continue
                t = items[k]
                cand = next((c for c in feat.router_candidates if c.hotkey == t.hotkey), None)
                planned = value_to_alpha(s.pool, val[k])
                held = hold.get(k)
                ours = int(held.alpha) if held is not None and held.hotkey == t.hotkey else 0
                if cand is None or not cand.eligible or not q_max_ok(s, t.hotkey, ours, planned, cfg):
                    veto(k, "router.no_validator", detail(hotkey=t.hotkey, candidate=cand is not None,
                                                          eligible=cand is not None and cand.eligible))
        acts.extend(self._section_h(tick, cfg, val, veto))

        # ---- 3. prune
        held_keys = sorted(hold)
        recov: dict[SubnetKey, Decimal] = {}
        for k in sorted(set(held_keys) | set(val)):
            sk = raw.get(k)
            if sk is not None:
                recov[k] = recovery(sk, raw.glob, cal, cfg)
        p24: McResult | None = None
        if cfg.tier_b_enabled and pc.possible and pc.hb_open and held_keys:
            p24 = self._mc(tick, cfg, cal, cfg.h_b_blocks)
        for ex in held_exits(raw, pc, held_keys, cfg, recov, feats, p24):
            force(ex.key, ex.urgency, ex.rule, int(ex.slip_ppm), ex.detail)
        increase = [k for k in sorted(val) if val[k] > current.get(k, 0)]
        p7: McResult | None = None
        if cfg.tier_b_enabled and pc.possible and increase:
            p7 = self._mc(tick, cfg, cal, H_7D_BLOCKS)
        for k, (rule, text) in sorted(entry_vetoes(raw, pc, increase, cfg, feats, recov, current, int(tick.nav_liq),
                                                   p7).items()):
            veto(k, rule, text)
        for k in held_keys:
            el = expected_loss_ppm_day(tick.frame, raw, k, recov.get(k, Decimal(0)), cal)
            if el is not None:
                acts.append(RiskAction("prune.expected_loss", k, "MONITOR",
                                       detail(p_reg_day_ppm=el[0], lambda_ppm_day=el[1], loss_ppm_day=el[2],
                                              recovery_ppm=ratio_ppm(recov.get(k, Decimal(0))))))
            for res in (p24, p7):
                if res is not None:
                    acts.append(RiskAction("prune.p_prune", k, "MONITOR",
                                           detail(horizon=res.horizon_blocks, p_ppm=int(res.get(k)), paths=res.paths)))

        # ---- 4. emission, burn, launch age
        sleeves_by_key: dict[SubnetKey, list[str]] = {}
        for sh in tick.portfolio.sleeves:
            if sh.shares > 0:
                sleeves_by_key.setdefault(sh.key, []).append(str(sh.strategy))
        lcw_held = [k for k in lcw_only_keys(sleeves_by_key) if k in hold]
        lcw_paper_held = lcw_held if self.run_mode not in (RunMode.LIVE, RunMode.LIVE_DRY) else []
        for ex2 in emission_exits(raw, held_keys, cfg, lcw_only=lcw_paper_held, book_view=bv, events=tick.events):
            force(ex2.key, ex2.urgency, ex2.rule, int(ex2.slip_ppm), ex2.detail)
        wave = wave_halt(tick.events, b)
        if wave is not None:
            halt = True
            acts.append(RiskAction("emission.wave", None, "HALT_ENTRIES", detail(disables=wave[0], halt_until=int(wave[1]))))
        for k, rule, until in ban_cooldowns(tick.events, b, cfg):
            acts.append(RiskAction(rule, k, "VETO_ENTRY", detail(cooldown_until=int(until))))
            if rule == "emission_off":                          # leading indicator of a later prune (section 3.4)
                acts.append(RiskAction("emission.prune_hazard", k, "MONITOR", detail(disabled_at=b)))
        increase = [k for k in sorted(val) if val[k] > current.get(k, 0)]
        for k, (rule, text) in sorted(entry_bans(raw, increase, bv, tick.events, cfg).items()):
            if not self._lcw_paper(lcw_ppm.get(k, 0)):              # LCW-paper exception (sections 2.4, 3.2 C)
                veto(k, rule, text)
        for ex2 in burn_exits(raw, tick.prev, tick.store, held_keys, cfg):
            force(ex2.key, ex2.urgency, ex2.rule, int(ex2.slip_ppm), ex2.detail)
        for ex2 in lcw_exits(raw, lcw_held, cfg):
            force(ex2.key, ex2.urgency, ex2.rule, int(ex2.slip_ppm), ex2.detail)

        # ---- 5. owner
        owner_keys = sorted(set(held_keys) | {k for k in val if val[k] > current.get(k, 0)})
        for trig in owner_triggers(raw, tick.store, tick.events, feats, owner_keys, bv, cfg):
            acts.append(RiskAction(trig.rule, trig.key, "VETO_ENTRY", trig.detail))
            veto(trig.key, f"owner.{trig.rule}", f"until={int(trig.until)}")

        # ---- 6. liquidity and caps
        caps_now = Caps.details(tick, cfg, sorted(set(val) | set(hold)))
        for k in sorted(val):
            d = caps_now.get(k)
            cap = int(d.cap) if d is not None else 0
            cur = current.get(k, 0)
            if val[k] > cur and val[k] > cap:
                new = max(cur, cap)
                acts.append(RiskAction("liquidity.vcap", k, "CLAMP", detail(from_rao=val[k], to_rao=new, cap_rao=cap)))
                val[k] = new
        for k in held_keys:
            mon = monitored(feats.get(k), cfg)
            if min(mon.m_gate_ppm, mon.m_trd_ppm, mon.m_owner_ppm) < PPM:
                acts.append(RiskAction("liquidity.monitor", k, "MONITOR",
                                       detail(m_gate_ppm=mon.m_gate_ppm, m_trd_ppm=mon.m_trd_ppm, m_owner_ppm=mon.m_owner_ppm,
                                              gate_active=cfg.gate_haircuts_active, owner_active=cfg.owner_haircut_active)))

        # ---- held generations that dissolved on chain
        for k in held_keys:
            if raw.get(k) is None or k in bv.dissolving:
                force(k, Urgency.URGENT, "dissolved", int(cfg.s_urgent_ppm), "pool_gone=1")

        # ---- forced exits take effect BEFORE the section I aggregates (fixed rule order, section 3.1): a position
        # being force-sold this tick must not use the gross, bucket or exit-shortfall budgets and push the cuts onto
        # healthy holdings.
        for k in sorted(exits):
            if k in val:
                fx = exits[k]
                val[k] = 0 if fx.trim_to is None else min(val[k], max(int(fx.trim_to), 0))

        # ---- 7. section I aggregates
        limits = aggregate_limits(int(tick.nav_liq), verdict.g_max_eff_ppm, cfg)
        vcap_now: dict[SubnetKey, int] = {}
        for k in sorted(val):                                   # only where the hold-ES trim can bind
            sv = view.get(k)
            if sv is not None and val[k] > 0 and exit_shortfall_ppm(sv.pool, val[k]) > cfg.s_exit_hold_max_ppm:
                vcap_now[k] = self._vcap_now(tick, k, cfg)
        val, agg = apply_aggregates(val, current, view, raw, cfg, limits, lcw_ppm, vcap_now=vcap_now)
        acts.extend(agg)

        # ---- mode and halts: no increases
        if halt or mode >= Mode.CAUTION:
            capped = [k for k in sorted(val) if val[k] > current.get(k, 0)]
            for k in capped:
                val[k] = current.get(k, 0)
            if capped:
                acts.append(RiskAction("mode.no_increase", None, "VETO_ENTRY", detail(mode=mode.name, halt=halt, names=len(capped))))

        return self._build(proposal, items, val, exits, vetoed, acts, mode, halt)

    # ------------------------------------------------------------------ helpers
    def _lcw_paper(self, lcw_share: int) -> bool:
        """LCW-paper exception (sections 2.4, 3.2 C): an LCW-majority target outside live books."""
        return lcw_share * 2 >= PPM and self.run_mode not in (RunMode.LIVE, RunMode.LIVE_DRY)

    def _failed_sections(self, row: UniverseRow | None, lcw_share: int, flags: frozenset[str]) -> tuple[str, ...]:
        if row is None:
            return ("A",)
        failed = list(row.failed)
        if lcw_share * 2 >= PPM:                         # LCW exceptions: launch age (no hard veto flag), C in paper
            if "E" in failed and not (flags & VETO_ALL):
                failed.remove("E")
            if "C" in failed and self._lcw_paper(lcw_share):
                failed.remove("C")
        return tuple(failed)

    @staticmethod
    def _universe(tick: TickContext, cfg: RiskCfg, pc: PruneContext) -> dict[SubnetKey, UniverseRow]:
        raw = tick.raw
        feats = tick.frame.feats
        bottom_s = raw.get(pc.target) if pc.target is not None else None
        bottom = bottom_s.moving_price if bottom_s is not None else None
        t_star: dict[SubnetKey, int | None] = {}
        for k, f in feats.items():
            v = f.t_star_stress_blocks
            t_star[k] = math.ceil(v) if v is not None and math.isfinite(v) else None
        inp = UniverseInputs(
            snap=raw, prune_rank=dict(pc.ranks), target=pc.target, bottom_ema=bottom, t_star=t_star,
            flags={k: f.launch_flags for k, f in feats.items()},
            validator_ok={k: any(c.eligible for c in f.router_candidates) for k, f in feats.items()},
            emission_ban_until=dict(emission_ban_until(tick.book_view, tick.events, int(tick.block), cfg)),
            cost_ratio=pc.cost_ratio)
        return {r.key: r for r in universe_evaluate(inp, UniverseParams.from_risk(cfg))}

    def _section_h(self, tick: TickContext, cfg: RiskCfg, val: Mapping[SubnetKey, int],
                   veto: _Veto) -> list[RiskAction]:
        """Section H: active cooldowns veto entries; failure bursts and abnormal fills start cooldowns."""
        b = int(tick.block)
        bv = tick.book_view
        out: list[RiskAction] = []
        for k, rule, until in sorted(bv.cooldowns, key=lambda c: (c[0], c[1], int(c[2]))):
            if int(until) >= b and k in val:
                veto(k, f"cooldown.{rule}", detail(until=int(until)))
        active = {(k, r): int(u) for k, r, u in bv.cooldowns if int(u) >= b}
        fails = dict(bv.fail_counts_600)
        for k in sorted(val):
            n = fails.get(k.netuid, 0)
            if n >= cfg.fail_burst_netuid:
                if active.get((k, FAIL_BURST_RULE), -1) < b:
                    out.append(RiskAction(FAIL_BURST_RULE, k, "VETO_ENTRY",
                                          detail(failures_600=n, cooldown_until=b + FAIL_COOLDOWN_BLOCKS)))
                veto(k, f"cooldown.{FAIL_BURST_RULE}", detail(failures_600=n))
        records = {o.intent.order_id: o for o in bv.orders}
        for f in sorted(bv.recent_fills, key=lambda x: (int(x.block), x.fill_id)):
            rec = records.get(f.order_id)
            if rec is None or not f.exact_block:
                continue
            modelled = _modelled_shortfall_ppm(rec, int(f.alpha), int(f.spot_before))
            if modelled is None:
                continue
            tol = ABNORMAL_ABS_PPM + abs(modelled) * ABNORMAL_REL_PPM // PPM
            if int(f.shortfall_ppm) - modelled <= tol:
                continue
            cd_until = int(f.block) + ABNORMAL_COOLDOWN_BLOCKS
            if cd_until < b:
                continue
            if active.get((f.key, ABNORMAL_FILL_RULE), -1) < cd_until:
                out.append(RiskAction(ABNORMAL_FILL_RULE, f.key, "VETO_ENTRY",
                                      detail(fill_id=f.fill_id, shortfall_ppm=int(f.shortfall_ppm), modelled_ppm=modelled,
                                             cooldown_until=cd_until)))
                active[(f.key, ABNORMAL_FILL_RULE)] = cd_until
            veto(f.key, f"cooldown.{ABNORMAL_FILL_RULE}", detail(fill_id=f.fill_id))
        return out

    @staticmethod
    def _vcap_now(tick: TickContext, key: SubnetKey, cfg: RiskCfg) -> int:
        """V_cap at T_now (no 3-day minimum, no stress haircut): the hold-budget trim level of section 3.5."""
        s = tick.view.get(key)
        if s is None:
            return 0
        d = vcap_detail(s, int(tick.nav_liq), tick.book_view, int(tick.block), tick.frame.feats.get(key),
                        replace(cfg, d_t_ppm=Ppm(0)), ())
        return int(d.cap)

    @staticmethod
    def _build(proposal: TargetBook, items: Mapping[SubnetKey, TargetPosition], val: Mapping[SubnetKey, int],
               exits: Mapping[SubnetKey, _Exit], vetoed: set[SubnetKey],
               acts: Sequence[RiskAction], mode: Mode, halt: bool) -> RiskDecision:
        out: list[TargetPosition] = []
        touched: set[SubnetKey] = set(vetoed) | set(exits)
        for k in sorted(items):
            t = items[k]
            v = min(int(t.value_rao), max(int(val.get(k, 0)), 0))
            reasons = t.reasons
            urgency = t.urgency
            ex = exits.get(k)
            if ex is not None:
                v = 0 if ex.trim_to is None else min(v, ex.trim_to)
                urgency = max(urgency, ex.urgency)
                reasons = reasons + (f"exit.{ex.rule}",)
            if v != int(t.value_rao):
                touched.add(k)
                if ex is None:
                    reasons = reasons + ("overlay",)
            out.append(replace(t, value_rao=Rao(v), urgency=urgency, reasons=reasons))
        forced = tuple(ForcedExit(key=e.key, urgency=e.urgency, rule=e.rule, exit_slip_ppm=Ppm(e.slip_ppm),
                                  trim_to_rao=None if e.trim_to is None else Rao(e.trim_to))
                       for e in (exits[k] for k in sorted(exits)))
        kept = tuple(x for x in proposal.transfers if x.key not in touched)
        actions = list(acts)
        dropped = len(proposal.transfers) - len(kept)
        if dropped:
            actions.append(RiskAction("overlay.netting_dropped", None, "MONITOR", detail(transfers=dropped)))
        book = TargetBook(asof=proposal.asof, items=tuple(out), forced=forced, halt_entries=halt or proposal.halt_entries,
                          transfers=kept)
        return RiskDecision(targets=book, actions=tuple(actions), mode=mode)


def _modelled_shortfall_ppm(rec: OrderRecord, alpha: int, spot: int) -> int | None:
    """The intent's modelled shortfall (ppm) at the fill's spot_before, as engine.reducer computes cost ratios."""
    i = rec.intent
    if i.expected_out <= 0 or spot <= 0:
        return None
    if i.kind is OrderKind.ADD_STAKE_LIMIT:
        if i.tao_in <= 0:
            return None
        return PPM - i.expected_out * spot * PPM // (int(i.tao_in) * RAO_PER_TAO)
    if i.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        alpha_in = int(i.alpha_in) if not i.full_position and i.alpha_in > 0 else alpha
        if alpha_in <= 0:
            return None
        return PPM - i.expected_out * RAO_PER_TAO * PPM // (alpha_in * spot)
    return None


if TYPE_CHECKING:
    from ..core.protocols import RiskOverlay

    def _conforms(o: StandardOverlay) -> RiskOverlay:      # mypy: structural conformance to core.protocols.RiskOverlay
        return o
