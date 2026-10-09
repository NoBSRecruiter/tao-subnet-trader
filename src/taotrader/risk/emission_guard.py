"""taotrader/risk/emission_guard.py - emission, burn and launch-age policy (WP8; DESIGN.md 3.4, the single source for
every sleeve).

- Held generation with SubnetEmissionEnabled false -> URGENT single-shot exit at S_URGENT ("emission_off"). The rule
  is state-based (the EMISSION_TOGGLED event is only its leading edge), so an unfinished exit is re-planned on every
  tick until flat. No tranching: a disabled subnet has no chain-buy refill.
- Entry ban: until max(disable + 100,800 blocks, re-enable + 360 blocks). engine.reducer folds the EMISSION_TOGGLED
  events into BookView.cooldowns under the rules "emission_off" / "emission_reenable"; this module reads them and
  also treats the current tick's events (and a currently disabled subnet) as banned, so a hand-built or pre-fold view
  gives the same answer. The overlay journals the same cooldowns (same rule names, so they merge).
- Wave: >= 3 disables in one block -> entries halted book-wide for 7,200 blocks ("halt_until" action).
- MinerBurned >= burn_exit_ppm (0.90) at the current epoch AND at the previous epoch (the latest snapshot before the
  current LastEpochBlock, from the bounded store or the previous tick) while held -> NORMAL exit ("burn").
- LCW-only positions (every sleeve holding it is an LCW sleeve): NORMAL exit ("launch_stop") if the subnet is not
  emission-enabled 21 days after start_call, or once age_reg >= NetworkImmunityPeriod - launch_stop_before_immunity_end.
- NetworkRegistrationAllowed false (EMA frozen) and the launch-age floor are entry rules of sections A and E
  (features.universe, re-evaluated per book by the overlay).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import RiskCfg
from ..core.errors import LookaheadError
from ..core.events import ChainEvent, ChainEventKind
from ..core.fixed import DEC
from ..core.orders import Urgency
from ..core.protocols import BookView, SnapshotStore
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import BLOCKS_PER_DAY, PPM, Block, Ppm, SubnetKey

__all__ = [
    "BURN_RULE",
    "EMISSION_OFF_RULE",
    "EMISSION_REENABLE_RULE",
    "LAUNCH_STOP_RULE",
    "LCW_ENABLE_DEADLINE_BLOCKS",
    "WAVE_DISABLES",
    "WAVE_HALT_BLOCKS",
    "EmissionExit",
    "ban_cooldowns",
    "burn_exits",
    "emission_ban_until",
    "emission_exits",
    "entry_bans",
    "lcw_exits",
    "lcw_only_keys",
    "wave_halt",
]

EMISSION_OFF_RULE: Final[str] = "emission_off"               # == engine.reducer cooldown rule
EMISSION_REENABLE_RULE: Final[str] = "emission_reenable"     # == engine.reducer cooldown rule
BURN_RULE: Final[str] = "burn"
LAUNCH_STOP_RULE: Final[str] = "launch_stop"
WAVE_DISABLES: Final[int] = 3
WAVE_HALT_BLOCKS: Final[int] = BLOCKS_PER_DAY
LCW_ENABLE_DEADLINE_BLOCKS: Final[int] = 21 * BLOCKS_PER_DAY


@dataclass(frozen=True, slots=True)
class EmissionExit:
    key: SubnetKey
    urgency: Urgency
    rule: str
    slip_ppm: Ppm
    detail: str


def emission_exits(raw: ChainSnapshot, held: Sequence[SubnetKey], cfg: RiskCfg, *, lcw_only: Sequence[SubnetKey] = (),
                   book_view: BookView | None = None, events: Sequence[ChainEvent] = ()) -> list[EmissionExit]:
    """URGENT exit of every held generation whose emission is disabled. LCW-only positions (paper) may hold a
    launch that was never enabled (their own 21-day rule covers it); they exit only on a disable AFTER enabling (LCW
    X1): a disable event this tick or an active "emission_off" cooldown (which only a disable event starts)."""
    out: list[EmissionExit] = []
    b = int(raw.block)
    lcw = set(lcw_only)
    disabled_now = {e.key for e in _toggles(events, b) if e.flag is False}
    for key in sorted(held):
        s = raw.get(key)
        if s is None or s.emission_enabled:
            continue
        if key in lcw:
            was_enabled = key in disabled_now or (book_view is not None and any(
                k == key and r == EMISSION_OFF_RULE and int(u) >= b for k, r, u in book_view.cooldowns))
            if not was_enabled:
                continue
        out.append(EmissionExit(key, Urgency.URGENT, EMISSION_OFF_RULE, Ppm(int(cfg.s_urgent_ppm)), "emission_enabled=0"))
    return out


def _toggles(events: Sequence[ChainEvent], block: int) -> list[ChainEvent]:
    return [e for e in events if e.kind is ChainEventKind.EMISSION_TOGGLED and e.key is not None and int(e.block) == block]


def wave_halt(events: Sequence[ChainEvent], block: int) -> tuple[int, Block] | None:
    """(disables, halt_until) when >= 3 generations were disabled in this block, else None."""
    n = sum(1 for e in _toggles(events, block) if e.flag is False)
    if n >= WAVE_DISABLES:
        return n, Block(block + WAVE_HALT_BLOCKS)
    return None


def ban_cooldowns(events: Sequence[ChainEvent], block: int, cfg: RiskCfg) -> list[tuple[SubnetKey, str, Block]]:
    """The section 3.4 ban cooldowns started by this tick's EMISSION_TOGGLED events (key, rule, until)."""
    out: list[tuple[SubnetKey, str, Block]] = []
    for e in _toggles(events, block):
        assert e.key is not None
        if e.flag is False:
            out.append((e.key, EMISSION_OFF_RULE, Block(block + cfg.emission_ban_blocks)))
        elif e.flag is True:
            out.append((e.key, EMISSION_REENABLE_RULE, Block(block + cfg.reenable_wait_blocks)))
    return sorted(out, key=lambda c: (c[0], c[1]))


def emission_ban_until(book_view: BookView, events: Sequence[ChainEvent], block: int,
                       cfg: RiskCfg) -> dict[SubnetKey, int]:
    """Latest ban end per generation from the BookView cooldowns and this tick's toggles."""
    out: dict[SubnetKey, int] = {}
    for k, rule, until in book_view.cooldowns:
        if rule in (EMISSION_OFF_RULE, EMISSION_REENABLE_RULE):
            out[k] = max(out.get(k, 0), int(until))
    for k, _, until in ban_cooldowns(events, block, cfg):
        out[k] = max(out.get(k, 0), int(until))
    return out


def entry_bans(raw: ChainSnapshot, keys: Sequence[SubnetKey], book_view: BookView, events: Sequence[ChainEvent],
               cfg: RiskCfg) -> dict[SubnetKey, tuple[str, str]]:
    """Entry vetoes of section 3.4: disabled now, or inside a ban window."""
    b = int(raw.block)
    bans = emission_ban_until(book_view, events, b, cfg)
    out: dict[SubnetKey, tuple[str, str]] = {}
    for key in sorted(keys):
        s = raw.get(key)
        if s is not None and not s.emission_enabled:
            out[key] = ("emission.disabled", "emission_enabled=0")
        elif bans.get(key, -1) >= b:
            out[key] = ("emission.ban", f"until={bans[key]}")
    return out


def _previous_epoch_state(store: SnapshotStore | None, prev: ChainSnapshot | None, s: SubnetState) -> SubnetState | None:
    """The generation's state in the epoch before its current LastEpochBlock (store first, then the previous tick)."""
    target = int(s.last_epoch_block) - 1
    if store is not None and target > 0:
        try:
            old = store.at_or_before(Block(target)).get(s.key)
        except (KeyError, LookaheadError, ValueError):
            old = None
        if old is not None and int(old.last_epoch_block) < int(s.last_epoch_block):
            return old
    if prev is not None:
        p = prev.get(s.key)
        if p is not None and int(p.last_epoch_block) < int(s.last_epoch_block):
            return p
    return None


def _burned_ppm(s: SubnetState) -> Decimal:
    return DEC.multiply(s.miner_burned, Decimal(PPM))


def burn_exits(raw: ChainSnapshot, prev: ChainSnapshot | None, store: SnapshotStore | None, held: Sequence[SubnetKey],
               cfg: RiskCfg) -> list[EmissionExit]:
    out: list[EmissionExit] = []
    limit = Decimal(int(cfg.burn_exit_ppm))
    for key in sorted(held):
        s = raw.get(key)
        if s is None or _burned_ppm(s) < limit:
            continue
        old = _previous_epoch_state(store, prev, s)
        if old is not None and _burned_ppm(old) >= limit:
            out.append(EmissionExit(key, Urgency.NORMAL, BURN_RULE, Ppm(int(cfg.s_exit_entry_ppm)),
                                    f"miner_burned={s.miner_burned};previous_epoch={old.miner_burned}"))
    return out


def lcw_exits(raw: ChainSnapshot, lcw_only: Sequence[SubnetKey], cfg: RiskCfg) -> list[EmissionExit]:
    out: list[EmissionExit] = []
    b = int(raw.block)
    for key in sorted(lcw_only):
        s = raw.get(key)
        if s is None:
            continue
        age = b - int(key.reg_at)
        stop_at = raw.glob.immunity_period - cfg.launch_stop_before_immunity_end
        if age >= stop_at:
            out.append(EmissionExit(key, Urgency.NORMAL, LAUNCH_STOP_RULE, Ppm(int(cfg.s_exit_entry_ppm)),
                                    f"age_reg={age};stop_at={stop_at}"))
            continue
        fe = s.first_emission_block
        if fe is not None and not s.emission_enabled and b - (int(fe) - 1) >= LCW_ENABLE_DEADLINE_BLOCKS:
            out.append(EmissionExit(key, Urgency.NORMAL, LAUNCH_STOP_RULE, Ppm(int(cfg.s_exit_entry_ppm)),
                                    f"since_start={b - (int(fe) - 1)};emission_enabled=0"))
    return out


def lcw_only_keys(sleeves: Mapping[SubnetKey, Sequence[str]]) -> list[SubnetKey]:
    """Generations whose every holding sleeve is an LCW sleeve (ids "lcw", "lcw.*", "lcw_*")."""
    out: list[SubnetKey] = []
    for key in sorted(sleeves):
        ids = list(sleeves[key])
        if ids and all(i == "lcw" or i.startswith(("lcw.", "lcw_")) for i in ids):
            out.append(key)
    return out
