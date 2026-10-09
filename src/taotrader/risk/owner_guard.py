"""taotrader/risk/owner_guard.py - owner events and owner sales (WP8; DESIGN.md 3.6, 3.5 m_owner, 3.2 H).

Triggers (each sets a 7,200-block entry cooldown on the generation and halves its V_cap for 1 day):
- the owner sold >= owner_unstake_frac_ppm (2%) of SubnetTAO, TAO-equivalent at spot, over the last 7,200 blocks.
  sold = max(0, -sum of protocol.derive.owner_position_delta) over consecutive FULL snapshots of the window, read from
  the bounded SnapshotStore on an absolute 60-block grid (the cache is shared by consecutive ticks). Moves to other
  coldkeys count as sales (the position simply shrinks). When the store has no usable history the 1,800-block
  feature Feat.owner_sold_6h_frac is used instead (a lower bound of the 7,200-block figure);
- SubnetOwner or SubnetOwnerHotkey changed (OWNER_CHANGED);
- the owner-cut autolock switched true -> false (AUTOLOCK_TOGGLED with flag False).
The cooldown is journaled as RiskAction(detail "cooldown_until=<block>"), which engine.reducer folds into
BookView.cooldowns under the action's rule. The event-driven rule names equal the reducer's own ("owner_changed",
"owner_autolock_off"), so the two writers merge into one cooldown. A sale cooldown is emitted only while none is
active, so a sale seen by the trailing window is not re-armed on every tick of that window.

Standing exposure: m_owner = 0.5 on V_cap when Feat.owner_liquid_frac >= owner_liquid_max_ppm (10%). MONITOR-only
until owner_haircut_active (an FT5-style ablation) is set.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import RiskCfg
from ..core.errors import LookaheadError
from ..core.events import ChainEvent, ChainEventKind
from ..core.fixed import DEC, floor_int, to_ppm
from ..core.protocols import BookView, SnapshotStore
from ..core.state import ChainSnapshot, ReadPlan
from ..core.units import BLOCKS_PER_DAY, PPM, Block, SubnetKey
from ..core.views import Feat
from ..protocol.derive import owner_position_delta

__all__ = [
    "OWNER_AUTOLOCK_RULE",
    "OWNER_CHANGED_RULE",
    "OWNER_RULES",
    "OWNER_SOLD_RULE",
    "OWNER_WINDOW_BLOCKS",
    "OwnerTrigger",
    "m_owner_ppm",
    "owner_cooldown_active",
    "owner_sold_alpha",
    "owner_triggers",
]

OWNER_CHANGED_RULE: Final[str] = "owner_changed"          # == engine.reducer's event-driven cooldown rule
OWNER_AUTOLOCK_RULE: Final[str] = "owner_autolock_off"    # == engine.reducer's event-driven cooldown rule
OWNER_SOLD_RULE: Final[str] = "owner.sold"
OWNER_RULES: Final[tuple[str, ...]] = (OWNER_AUTOLOCK_RULE, OWNER_CHANGED_RULE, OWNER_SOLD_RULE)
OWNER_WINDOW_BLOCKS: Final[int] = BLOCKS_PER_DAY
FULL_GRID_BLOCKS: Final[int] = 60                         # FULL-plan cadence (paper/live) and the backtest stride
M_OWNER_PPM: Final[int] = 500_000


@dataclass(frozen=True, slots=True)
class OwnerTrigger:
    key: SubnetKey
    rule: str
    until: Block
    detail: str


def owner_cooldown_active(book_view: BookView, key: SubnetKey, block: int) -> bool:
    """An owner-event cooldown (any OWNER_RULES rule) is active on `key` at `block`."""
    return any(k == key and r in OWNER_RULES and int(u) >= block for k, r, u in book_view.cooldowns)


def _full_history(store: SnapshotStore, raw: ChainSnapshot, window: int) -> list[ChainSnapshot]:
    """FULL snapshots on the absolute 60-block grid of (block - window, block], plus raw itself, oldest first."""
    b = int(raw.block)
    out: dict[int, ChainSnapshot] = {}
    g = (b // FULL_GRID_BLOCKS) * FULL_GRID_BLOCKS
    while g > b - window:
        try:
            snap = store.at_or_before(Block(g))
        except (KeyError, LookaheadError, ValueError):
            break
        if int(snap.block) <= b - window:
            break
        if snap.plan is ReadPlan.FULL and int(snap.block) < b:
            out[int(snap.block)] = snap
        g -= FULL_GRID_BLOCKS
    if raw.plan is ReadPlan.FULL:
        out[b] = raw
    return [out[k] for k in sorted(out)]


def owner_sold_alpha(store: SnapshotStore | None, raw: ChainSnapshot, key: SubnetKey,
                     window: int = OWNER_WINDOW_BLOCKS, *, hist: Sequence[ChainSnapshot] | None = None) -> int | None:
    """Owner alpha sold (>= 0) over (block - window, block]; None when fewer than two FULL observations exist.
    `hist` (the FULL snapshots of the window, oldest first) may be passed to share one store walk across keys."""
    if hist is None:
        if store is None:
            return None
        hist = _full_history(store, raw, window)
    total = 0
    pairs = 0
    prev: ChainSnapshot | None = None
    for snap in hist:
        cur_s = snap.get(key)
        if prev is not None and cur_s is not None:
            prev_s = prev.get(key)
            if prev_s is not None:
                d = owner_position_delta(prev_s, cur_s, snap.glob, prev.block, snap.block)
                if d is not None:
                    total += d
                    pairs += 1
        prev = snap if cur_s is not None else prev
    if pairs == 0:
        return None
    return max(0, -total)


def _sold_from_feature(raw: ChainSnapshot, key: SubnetKey, feat: Feat | None) -> int | None:
    s = raw.get(key)
    if s is None or feat is None or feat.owner_sold_6h_frac is None:
        return None
    frac = to_ppm(feat.owner_sold_6h_frac)
    return max(0, int(s.pool.alpha) * frac // PPM)


def owner_triggers(raw: ChainSnapshot, store: SnapshotStore | None, events: Sequence[ChainEvent],
                   feats: dict[SubnetKey, Feat], keys: Sequence[SubnetKey], book_view: BookView,
                   cfg: RiskCfg) -> list[OwnerTrigger]:
    """Owner triggers on `keys` (the held and proposed generations) at raw.block, sorted by (key, rule)."""
    b = int(raw.block)
    until = Block(b + cfg.owner_cooldown_blocks)
    out: list[OwnerTrigger] = []
    wanted = set(keys)
    for e in events:
        if e.key is None or e.key not in wanted:
            continue
        if e.kind is ChainEventKind.OWNER_CHANGED:
            out.append(OwnerTrigger(e.key, OWNER_CHANGED_RULE, Block(int(e.block) + cfg.owner_cooldown_blocks),
                                    f"event=owner_changed;cooldown_until={int(e.block) + cfg.owner_cooldown_blocks}"))
        elif e.kind is ChainEventKind.AUTOLOCK_TOGGLED and e.flag is False:
            out.append(OwnerTrigger(e.key, OWNER_AUTOLOCK_RULE, Block(int(e.block) + cfg.owner_cooldown_blocks),
                                    f"event=autolock_off;cooldown_until={int(e.block) + cfg.owner_cooldown_blocks}"))
    hist: list[ChainSnapshot] | None = None
    for key in sorted(wanted):
        s = raw.get(key)
        if s is None or owner_cooldown_active(book_view, key, b):
            continue
        if hist is None and store is not None:
            hist = _full_history(store, raw, OWNER_WINDOW_BLOCKS)
        sold = owner_sold_alpha(store, raw, key, hist=hist)
        source = "store"
        if sold is None:
            sold = _sold_from_feature(raw, key, feats.get(key))
            source = "feat_6h"
        if sold is None or sold <= 0 or s.pool.px_alpha <= 0 or s.pool.px_tao <= 0:
            continue
        sold_tao = floor_int(DEC.multiply(Decimal(sold), s.pool.spot()))
        if sold_tao * PPM >= int(cfg.owner_unstake_frac_ppm) * int(s.pool.tao):
            frac = sold_tao * PPM // max(int(s.pool.tao), 1)
            out.append(OwnerTrigger(key, OWNER_SOLD_RULE, until,
                                    f"sold_alpha={sold};sold_tao_rao={sold_tao};frac_ppm={frac};source={source};"
                                    f"cooldown_until={int(until)}"))
    out.sort(key=lambda t: (t.key, t.rule, int(t.until)))
    dedup: list[OwnerTrigger] = []
    for t in out:
        if dedup and dedup[-1].key == t.key and dedup[-1].rule == t.rule:
            continue
        dedup.append(t)
    return dedup


def m_owner_ppm(feat: Feat | None, cfg: RiskCfg) -> int:
    """MONITOR haircut: 0.5 when Feat.owner_liquid_frac >= owner_liquid_max_ppm, else 1 (unknown -> 1)."""
    if feat is None or feat.owner_liquid_frac is None:
        return PPM
    return M_OWNER_PPM if to_ppm(feat.owner_liquid_frac) >= cfg.owner_liquid_max_ppm else PPM
