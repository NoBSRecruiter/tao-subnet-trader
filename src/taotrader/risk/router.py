"""taotrader/risk/router.py - the per-book YieldRouter choice (WP8; DESIGN.md 3.8). A RouterFn: (TickContext, RiskCfg)
-> RouterState. Pure; the memory lives in RouterState (journaled as the "risk.router" memory, folded back by the
reducer into BookView.router).

Book-independent candidate data come from WP5 (Feat.router_candidates, best first; `eligible` = every
book-independent filter of section 3.8). The per-book rules here:
- Q_MAX: our alpha after the planned trade / TotalHotkeyAlpha(h, n) <= Q_MAX (5%), with TotalHotkeyAlpha after the
  trade (our current stake on h replaced by the planned one). The router runs before the allocator, so the planned
  position is the largest one the book may hold there: max(current alpha, alpha of min(NU_MAX * NAV_liq,
  T_now * s/(1 - s))) at spot. Books of different NAV therefore choose different hotkeys where Q_MAX binds. A hotkey
  whose stake is not in the snapshot fails (fail closed).
- State: only HELD generations are remembered (choice, fail and beat counters), so the journaled memory stays bounded
  by the number of positions. A generation that becomes held adopts the position's hotkey with fresh counters (the
  entry choice was this module's `entry_hotkey`, used by the allocator). A generation no longer held is forgotten.
- Re-evaluation happens only at the generation's EPOCH_DRAIN (candidate scores change only there) or on a take jump of
  the current hotkey; counters count those evaluations (epochs).
- Switch (same-subnet MOVE_STAKE of the whole position; the planner acts on TargetPosition.hotkey != the held hotkey)
  to the best candidate that is eligible and passes Q_MAX, if any of:
  * the current hotkey failed the filters (not eligible, not tracked, or Q_MAX) for 2 consecutive epochs;
  * its take jumped by more than 5 percentage points (TAKE_CHANGED with new - old > 5% of 65535);
  * the best beat it by more than max(0.02 %/day, 10% relative) for 2 consecutive epochs AND the 30-day expected gain
    (value x score difference x 30 d) exceeds 3x the move fee (ExecCfg.move_tx_fee_rao).
  With no passing alternative the position stays where it is (yield may then be zero; the overlay vetoes entries).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from ..core.config import ExecCfg, RiskCfg
from ..core.events import ChainEvent, ChainEventKind
from ..core.protocols import RouterState, TickContext
from ..core.state import SubnetState
from ..core.units import FEE_DEN, PPM, Hotkey, SubnetKey
from ..core.views import RouterCandidate
from ..protocol.amm import v_max
from .liquidity import Holding, holdings, value_to_alpha

__all__ = [
    "BEAT_EPOCHS",
    "FAIL_EPOCHS",
    "GAIN_DAYS",
    "GAIN_FEE_MULT",
    "Router",
    "entry_hotkey",
    "planned_alpha",
    "q_max_ok",
    "route",
    "take_jumped",
]

FAIL_EPOCHS: Final[int] = 2
BEAT_EPOCHS: Final[int] = 2
BEAT_RELATIVE_PPM: Final[int] = 100_000          # 10 % relative
TAKE_JUMP_PP: Final[int] = 5                     # > 5 percentage points
GAIN_DAYS: Final[int] = 30
GAIN_FEE_MULT: Final[int] = 3


def planned_alpha(ctx: TickContext, s: SubnetState, cfg: RiskCfg, held_alpha: int = 0) -> int:
    """The largest alpha the book may hold on `s`: max(current, alpha of min(NU_MAX * NAV_liq, T_now*s/(1-s)))."""
    nu = max(int(ctx.nav_liq), 0) * int(cfg.nu_max_ppm) // PPM
    v = min(nu, int(v_max(s.pool.tao, int(cfg.s_exit_entry_ppm))))
    return max(int(held_alpha), value_to_alpha(s.pool, v))


def q_max_ok(s: SubnetState, hotkey: Hotkey, ours_now_on_h: int, planned: int, cfg: RiskCfg) -> bool:
    """planned / (TotalHotkeyAlpha(h) - ours_now_on_h + planned) <= Q_MAX; False when h's stake is unknown."""
    idx = s.hotkey(hotkey)
    if idx is None:
        return False
    total_after = int(idx.total_alpha) - int(ours_now_on_h) + int(planned)
    if planned <= 0:
        return True
    return total_after > 0 and planned * PPM <= int(cfg.q_max_ppm) * total_after


def _passing(cands: Sequence[RouterCandidate], s: SubnetState, planned: int, held: Holding | None,
             cfg: RiskCfg) -> list[RouterCandidate]:
    out: list[RouterCandidate] = []
    for c in cands:
        ours = int(held.alpha) if held is not None and held.hotkey == c.hotkey else 0
        if c.eligible and q_max_ok(s, c.hotkey, ours, planned, cfg):
            out.append(c)
    return out


def entry_hotkey(ctx: TickContext, key: SubnetKey, cfg: RiskCfg) -> Hotkey | None:
    """The router's choice for a generation the book does not hold: the best eligible candidate passing Q_MAX for the
    book's planned size (candidates are published best first). None -> no validator, no entry (section 3.2 G)."""
    s = ctx.view.get(key)
    feat = ctx.frame.feats.get(key)
    if s is None or feat is None:
        return None
    planned = planned_alpha(ctx, s, cfg)
    ok = _passing(feat.router_candidates, s, planned, None, cfg)
    return ok[0].hotkey if ok else None


def take_jumped(events: Sequence[ChainEvent], key: SubnetKey, hotkey: Hotkey) -> bool:
    """TAKE_CHANGED on (key, hotkey) with new - old > 5 percentage points of 65535."""
    for e in events:
        if e.kind is ChainEventKind.TAKE_CHANGED and e.hotkey == hotkey and (e.key is None or e.key == key):
            try:
                old, new = int(e.old or "0"), int(e.new or "0")
            except ValueError:
                continue
            if (new - old) * 100 > TAKE_JUMP_PP * FEE_DEN:
                return True
    return False


class Router:
    """RouterFn with the move fee of the book's ExecCfg (the 30-day gain test)."""

    def __init__(self, exec_cfg: ExecCfg | None = None) -> None:
        self.move_fee_rao = (exec_cfg if exec_cfg is not None else ExecCfg()).move_tx_fee_rao

    def __call__(self, ctx: TickContext, cfg: RiskCfg) -> RouterState:
        prior = ctx.book_view.router
        prior_choice = dict(prior.choice)
        prior_fail = dict(prior.fail_epochs)
        prior_beat = {k: (h, n) for k, h, n in prior.beat_epochs}
        hold = holdings(ctx)
        drained = {e.key for e in ctx.events if e.kind is ChainEventKind.EPOCH_DRAIN and e.key is not None}
        choice: list[tuple[SubnetKey, Hotkey]] = []
        fails: list[tuple[SubnetKey, int]] = []
        beats: list[tuple[SubnetKey, Hotkey, int]] = []
        for key in sorted(hold):
            h = hold[key]
            cur = prior_choice.get(key)
            if cur is None:                                   # holdings changed: adopt the physical hotkey
                choice.append((key, h.hotkey))
                continue
            fail = prior_fail.get(key, 0)
            beat = prior_beat.get(key)
            s = ctx.view.get(key)
            feat = ctx.frame.feats.get(key)
            jumped = take_jumped(ctx.events, key, cur)
            if s is None or feat is None or (key not in drained and not jumped):
                choice.append((key, cur))
                if fail:
                    fails.append((key, fail))
                if beat is not None:
                    beats.append((key, beat[0], beat[1]))
                continue
            planned = planned_alpha(ctx, s, cfg, int(h.alpha))
            cands = feat.router_candidates
            cur_c = next((c for c in cands if c.hotkey == cur), None)
            ours = int(h.alpha) if h.hotkey == cur else 0
            cur_ok = cur_c is not None and cur_c.eligible and q_max_ok(s, cur, ours, planned, cfg)
            fail = 0 if cur_ok else fail + 1
            best = next((c for c in _passing(cands, s, planned, h, cfg) if c.hotkey != cur), None)
            new_beat: tuple[Hotkey, int] | None = None
            if best is not None:
                cur_score = cur_c.score_ppm_day if cur_c is not None else 0
                margin = max(int(cfg.switch_min_gain_ppm_day), max(cur_score, 0) * BEAT_RELATIVE_PPM // PPM)
                if cur_c is None or best.score_ppm_day - cur_score > margin:
                    n = beat[1] + 1 if beat is not None and beat[0] == best.hotkey else 1
                    new_beat = (best.hotkey, n)
            switch = False
            if best is not None:
                if fail >= FAIL_EPOCHS or jumped:
                    switch = True
                elif new_beat is not None and new_beat[1] >= BEAT_EPOCHS and cur_c is not None:
                    gain = int(h.value) * (best.score_ppm_day - cur_c.score_ppm_day) * GAIN_DAYS // PPM
                    switch = gain > GAIN_FEE_MULT * int(self.move_fee_rao)
            if switch and best is not None:
                choice.append((key, best.hotkey))
                continue
            choice.append((key, cur))
            if fail:
                fails.append((key, fail))
            if new_beat is not None:
                beats.append((key, new_beat[0], new_beat[1]))
        return RouterState(choice=tuple(sorted(choice)), fail_epochs=tuple(sorted(fails)),
                           beat_epochs=tuple(sorted(beats)))


route: Final[Router] = Router()


if TYPE_CHECKING:
    from ..core.protocols import RouterFn

    _ROUTER_FN: RouterFn = route                           # mypy: conformance to core.protocols.RouterFn
