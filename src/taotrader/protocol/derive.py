"""taotrader/protocol/derive.py - chain events as a pure diff of two snapshots, and tracked-hotkey selection (WP2;
DESIGN.md sections 4.2, 4.3, 5.12, 3.6, 3.8).

`derive_events` is the ONLY chain-event source in v1. Detection rules (section 4.3), all evaluated prev -> cur:
- REGISTERED / DEREGISTERED: generation key only in cur / only in prev (presence = NetworksAdded, so DEREGISTERED
  fires at the removal block even while NetworkRegisteredAt and the pool keys await cleanup); reuse of a netuid
  gives DEREGISTERED(old) + REGISTERED(new), never a price jump.
- START_CALLED, EMISSION_TOGGLED, REG_ALLOWED_TOGGLED, EPOCH_DRAIN, LARGE_FLOW, PRUNE_TARGET_CHANGED,
  GATE_BAR_UPDATED, REGISTRATION_SEEN, REG_WINDOW_OPENED, IMMUNITY_EXPIRED, SPEC_CHANGED, SAFE_MODE, the global
  PARAM_CHANGED rows and FeeRate use hot-path (HEAD) fields and fire on every snapshot pair.
- FULL-only fields (owner coldkey/hotkey, autolock, owner position, the hotkey panel, Tempo, EMAPriceHalvingBlocks,
  consensus mode) fire only when cur is a FULL snapshot and both snapshots carry the value (not None). A HEAD
  snapshot's carried copies are the last FULL read, so a FULL-after-HEAD diff spans the whole FULL interval.
Events are sorted by (kind, key, hotkey, name, ...) and the function is idempotent: identical snapshots give ().
`prev is None` (the first snapshot of a stream) gives () - there is nothing to diff.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from ..core.codec import encode
from ..core.events import ChainEvent, ChainEventKind
from ..core.fixed import DEC, floor_int
from ..core.state import ChainGlobals, ChainSnapshot, HotkeyIdx, ReadPlan, SubnetState
from ..core.units import FEE_DEN, PPM, Block, Hotkey, Ppm, SubnetKey
from .prune import ladder
from .regimes import regime
from .yield_model import owner_cut_frac

OWNER_CHANGE_FRAC_PPM: Final[int] = 2_500       # OWNER_POSITION_CHANGED threshold: 0.25% of pool alpha (section 4.3)
TAKE_CREDIT_TO_OWNER: Final[bool] = True        # VERIFY (section 13 Q22): validator-take credits land on the owner
                                                # coldkey's position; fail-closed default (more expected growth ->
                                                # larger sold estimate)

K = ChainEventKind

# Freeze-list globals (section 4.3): storage item name -> ChainGlobals attribute.
GLOBAL_PARAMS: Final[tuple[tuple[str, str], ...]] = (
    ("SubnetMovingAlpha", "moving_alpha"),
    ("EmissionBarRank", "gate_rank"),
    ("EmissionGateExponent", "gate_exponent"),
    ("TaoWeight", "tao_weight"),
    ("SubnetLimit", "subnet_limit"),
    ("NetworkImmunityPeriod", "immunity_period"),
    ("NetworkRateLimit", "network_rate_limit"),
    ("NetworkLockReductionInterval", "lock_reduction_interval"),
    ("NetworkMinLockCost", "min_lock_cost"),
    ("SubnetOwnerCut", "owner_cut_u16"),
    ("ShortsEnabled", "shorts_enabled"),
)


def _text(v: object) -> str:
    """Canonical text of a value for ChainEvent.old/new (codec text for Decimals, 'netuid:reg_at' for keys)."""
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, SubnetKey):
        return f"{int(v.netuid)}:{int(v.reg_at)}"
    return str(encode(v))


def safe_mode_active(glob: ChainGlobals, block: Block) -> bool:
    return glob.safe_mode_until is not None and glob.safe_mode_until >= block


def flow_valid_from() -> Block:
    """SubnetTaoFlow is a valid running total from the price_ema_rp regime on (8,466,531)."""
    return regime("price_ema_rp").first_block


def owner_position_delta(prev_s: SubnetState, cur_s: SubnetState, glob: ChainGlobals, prev_block: Block,
                         cur_block: Block) -> int | None:
    """Owner coldkey position on the owner hotkey: A1 - (A0 * I1/I0 + accrual), signed alpha rao (negative = sold;
    section 3.6 owner_sold = max(0, -delta)). accrual = c_o * alpha_out_emission * blocks (0 when OwnerCutEnabled
    is false; NO (1 - AutoLock) factor) + the owner hotkey's take credit last_dividend*t/(1-t) when a drain fell in
    the interval and it earns. None when an input is missing or the owner changed (OWNER_CHANGED covers that).

    The owner-cut accrual counts (prev_block, cur_block]. When prev is a HEAD snapshot carrying the last FULL panel,
    the cut accrued between that FULL read and prev_block (c_o * SubnetAlphaOutEmission per block, about 11 alpha per
    60 blocks today) is not counted: a bias toward "bought" of about 1% of the 0.25%-of-pool event threshold."""
    if prev_s.key != cur_s.key or prev_s.owner_alpha is None or cur_s.owner_alpha is None:
        return None
    if prev_s.owner_hotkey is None or prev_s.owner_hotkey != cur_s.owner_hotkey or prev_s.owner_coldkey != cur_s.owner_coldkey:
        return None
    h0, h1 = prev_s.hotkey(prev_s.owner_hotkey), cur_s.hotkey(cur_s.owner_hotkey)
    if h0 is None or h1 is None:
        return None
    i0, i1 = h0.index(), h1.index()
    if i0 <= 0:
        return None
    grown = DEC.divide(DEC.multiply(Decimal(prev_s.owner_alpha), i1), i0)
    blocks = max(cur_block - prev_block, 0)
    accrual = DEC.multiply(owner_cut_frac(cur_s, glob), Decimal(prev_s.alpha_out_emission * blocks))
    # A drain fell in the interval if LastEpochBlock moved or the owner hotkey's AlphaDividendsPerSubnet value changed
    # (the latter also catches a drain between a HEAD snapshot's carried panel and the next FULL read).
    drained = cur_s.last_epoch_block != prev_s.last_epoch_block or h1.last_dividend != h0.last_dividend
    if TAKE_CREDIT_TO_OWNER and drained and h1.earns and h1.take_u16 < FEE_DEN:
        credit = DEC.divide(DEC.multiply(Decimal(h1.last_dividend), Decimal(h1.take_u16)), Decimal(FEE_DEN - h1.take_u16))
        accrual = DEC.add(accrual, credit)
    expected = floor_int(DEC.add(grown, accrual))
    return cur_s.owner_alpha - expected


def _sort_key(e: ChainEvent) -> tuple[str, int, int, str, str, str, str, int, int]:
    k = e.key
    return (e.kind.value, -1 if k is None else int(k.netuid), -1 if k is None else int(k.reg_at), e.hotkey or "",
            e.name or "", e.old or "", e.new or "", -1 if e.flag is None else int(e.flag),
            0 if e.amount is None else e.amount)


def _subnet_events(p: SubnetState, c: SubnetState, prev: ChainSnapshot, cur: ChainSnapshot, full: bool,
                   large_flow_frac_ppm: int) -> list[ChainEvent]:
    b, key = cur.block, c.key
    out: list[ChainEvent] = []
    if p.first_emission_block is None and c.first_emission_block is not None:
        out.append(ChainEvent(K.START_CALLED, b, key=key))
    if p.emission_enabled != c.emission_enabled:
        out.append(ChainEvent(K.EMISSION_TOGGLED, b, key=key, flag=c.emission_enabled))
    if p.reg_allowed != c.reg_allowed:
        out.append(ChainEvent(K.REG_ALLOWED_TOGGLED, b, key=key, flag=c.reg_allowed))
    if p.last_epoch_block != c.last_epoch_block:
        out.append(ChainEvent(K.EPOCH_DRAIN, b, key=key, old=_text(p.last_epoch_block), new=_text(c.last_epoch_block)))
    if (p.tao_flow_cum is not None and c.tao_flow_cum is not None and prev.block >= flow_valid_from()
            and c.pool.tao > 0):
        delta = c.tao_flow_cum - p.tao_flow_cum
        if abs(delta) * PPM >= large_flow_frac_ppm * c.pool.tao:
            frac = abs(delta) * PPM // c.pool.tao
            out.append(ChainEvent(K.LARGE_FLOW, b, key=key, amount=delta, frac_ppm=Ppm(frac if delta >= 0 else -frac)))
    if p.pool.fee_rate != c.pool.fee_rate:
        out.append(ChainEvent(K.PARAM_CHANGED, b, key=key, name="FeeRate", old=_text(p.pool.fee_rate), new=_text(c.pool.fee_rate)))
    if not full:
        return out
    # ---- FULL-only fields (both values present)
    if p.owner_cut_autolock is not None and c.owner_cut_autolock is not None and p.owner_cut_autolock != c.owner_cut_autolock:
        out.append(ChainEvent(K.AUTOLOCK_TOGGLED, b, key=key, flag=c.owner_cut_autolock))
    if p.owner_coldkey is not None and c.owner_coldkey is not None and p.owner_coldkey != c.owner_coldkey:
        out.append(ChainEvent(K.OWNER_CHANGED, b, key=key, name="SubnetOwner", old=p.owner_coldkey, new=c.owner_coldkey))
    if p.owner_hotkey is not None and c.owner_hotkey is not None and p.owner_hotkey != c.owner_hotkey:
        out.append(ChainEvent(K.OWNER_CHANGED, b, key=key, hotkey=c.owner_hotkey, name="SubnetOwnerHotkey",
                       old=p.owner_hotkey, new=c.owner_hotkey))
    delta_owner = owner_position_delta(p, c, cur.glob, prev.block, cur.block)
    if delta_owner is not None and abs(delta_owner) * PPM >= OWNER_CHANGE_FRAC_PPM * c.pool.alpha and delta_owner != 0:
        out.append(ChainEvent(K.OWNER_POSITION_CHANGED, b, key=key, hotkey=c.owner_hotkey, amount=delta_owner))
    prev_hk: dict[str, HotkeyIdx] = {h.hotkey: h for h in p.hotkeys}
    for h in c.hotkeys:
        h0 = prev_hk.get(h.hotkey)
        if h0 is None:
            continue
        if h0.take_u16 != h.take_u16:
            out.append(ChainEvent(K.TAKE_CHANGED, b, key=key, hotkey=h.hotkey, name="Delegates", old=_text(h0.take_u16),
                           new=_text(h.take_u16)))
        if h0.earns != h.earns:
            out.append(ChainEvent(K.DIVIDEND_MEMBERSHIP, b, key=key, hotkey=h.hotkey, flag=h.earns))
    if p.tempo != c.tempo:
        out.append(ChainEvent(K.PARAM_CHANGED, b, key=key, name="Tempo", old=_text(p.tempo), new=_text(c.tempo)))
    if p.ema_halving_blocks != c.ema_halving_blocks:
        out.append(ChainEvent(K.PARAM_CHANGED, b, key=key, name="EMAPriceHalvingBlocks", old=_text(p.ema_halving_blocks),
                       new=_text(c.ema_halving_blocks)))
    if p.consensus_mode is not None and c.consensus_mode is not None and p.consensus_mode != c.consensus_mode:
        out.append(ChainEvent(K.PARAM_CHANGED, b, key=key, name="SubnetEpochConsensus", old=_text(p.consensus_mode),
                       new=_text(c.consensus_mode)))
    return out


def derive_events(prev: ChainSnapshot | None, cur: ChainSnapshot, large_flow_frac_ppm: Ppm = Ppm(20_000)  # noqa: B008
                  ) -> tuple[ChainEvent, ...]:
    """The ONLY chain-event source in v1 (section 4.2). Pure; sorted by (kind, key); idempotent on identical snapshots."""
    if prev is None:
        return ()
    if cur.block < prev.block:
        raise ValueError(f"snapshots out of order: {prev.block} -> {cur.block}")
    b = cur.block
    full = cur.plan == ReadPlan.FULL
    out: list[ChainEvent] = []
    pg, cg = prev.glob, cur.glob

    prev_keys = {s.key for s in prev.subnets}
    cur_keys = {s.key for s in cur.subnets}
    for s in prev.subnets:
        if s.key not in cur_keys:
            out.append(ChainEvent(K.DEREGISTERED, b, key=s.key))
    for s in cur.subnets:
        if s.key not in prev_keys:
            out.append(ChainEvent(K.REGISTERED, b, key=s.key))
            continue
        p = prev.get(s.key)
        if p is not None:
            out.extend(_subnet_events(p, s, prev, cur, full, large_flow_frac_ppm))
        end = s.key.reg_at + cg.immunity_period
        if prev.block < end <= cur.block:
            out.append(ChainEvent(K.IMMUNITY_EXPIRED, b, key=s.key))

    if cg.last_reg_block > pg.last_reg_block:
        out.append(ChainEvent(K.REGISTRATION_SEEN, b, old=_text(pg.last_reg_block), new=_text(cg.last_reg_block)))
    opening = cg.last_reg_block + cg.network_rate_limit
    if prev.block < opening <= cur.block:
        out.append(ChainEvent(K.REG_WINDOW_OPENED, b))
    lp, lc = ladder(prev), ladder(cur)
    t_prev, t_cur = (lp[0] if lp else None), (lc[0] if lc else None)
    if t_prev != t_cur:
        out.append(ChainEvent(K.PRUNE_TARGET_CHANGED, b, key=t_cur, old=_text(t_prev), new=_text(t_cur)))
    if pg.gate_bar != cg.gate_bar:
        out.append(ChainEvent(K.GATE_BAR_UPDATED, b, old=_text(pg.gate_bar), new=_text(cg.gate_bar)))
    if pg.spec_version != cg.spec_version:
        out.append(ChainEvent(K.SPEC_CHANGED, b, name="spec_version", old=_text(pg.spec_version), new=_text(cg.spec_version)))
    if pg.tx_version != cg.tx_version:
        out.append(ChainEvent(K.SPEC_CHANGED, b, name="transaction_version", old=_text(pg.tx_version), new=_text(cg.tx_version)))
    for name, attr in GLOBAL_PARAMS:
        old, new = getattr(pg, attr), getattr(cg, attr)
        if old != new:
            out.append(ChainEvent(K.PARAM_CHANGED, b, name=name, old=_text(old), new=_text(new)))
    was, now = safe_mode_active(pg, prev.block), safe_mode_active(cg, cur.block)
    if was != now:
        out.append(ChainEvent(K.SAFE_MODE, b, flag=now))
    return tuple(sorted(out, key=_sort_key))


def track_hotkeys(snap: ChainSnapshot, dividend_keys: dict[int, Sequence[str]],
                  held: Sequence[tuple[SubnetKey, str]], prev_tracked: Sequence[tuple[SubnetKey, str]] = (),
                  top_n: int = 5) -> tuple[tuple[SubnetKey, str], ...]:
    """Tracked set per subnet: every (key, hotkey) held or chosen by any book + top-N earning by TotalHotkeyAlpha
    + every take-0 earner + (owner_hotkey, netuid) for every subnet with owner_hotkey set. Sticky: every pair in
    prev_tracked whose generation is still in snap stays tracked until the generation ends. dividend_keys must be
    listed point-in-time at snap.block_hash (historical collection included).

    Earners are the HotkeyIdx entries of snap whose hotkey is listed in dividend_keys[netuid]; ranking and the
    take-0 test read their TotalHotkeyAlpha and Delegates take (ties: lexicographic hotkey). Held, chosen and sticky
    pairs whose generation is gone are dropped. Sorted by (key, hotkey)."""
    out: set[tuple[SubnetKey, str]] = set()
    for s in snap.subnets:
        listed = set(dividend_keys.get(int(s.key.netuid), ()))
        earners = sorted((h for h in s.hotkeys if h.hotkey in listed), key=lambda h: (-h.total_alpha, h.hotkey))
        for h in earners[:max(top_n, 0)]:
            out.add((s.key, h.hotkey))
        for h in earners:
            if h.take_u16 == 0:
                out.add((s.key, h.hotkey))
        if s.owner_hotkey is not None:
            out.add((s.key, s.owner_hotkey))
    for key, hk in list(held) + list(prev_tracked):
        if snap.get(key) is not None:
            out.add((key, Hotkey(hk)))
    return tuple(sorted(out, key=lambda kh: (kh[0], kh[1])))
