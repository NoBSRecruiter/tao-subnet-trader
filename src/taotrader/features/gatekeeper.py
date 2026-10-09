"""taotrader/features/gatekeeper.py - launch registry, launch states and mechanical flags (WP5; DESIGN.md sections
2.4, 3.4, 3.7). Overlay-owned, always on, no capital; book-independent.

Generation registry, detected from storage diffs of consecutive snapshots (brief section 4.3):
- a generation that leaves NetworksAdded is a removal. When the previous snapshot could prune (n_nonroot +
  cleanup >= SubnetLimit) or a registration was recorded in the same diff, the removal is a registration's prune
  victim and opens a PENDING (QUEUED) registration;
- the queued block Q is the removal block. It is exact when the removal is seen between adjacent blocks, or when
  LastRateLimitedBlock(NetworkLastRegistered) advanced to a block inside the diff that is not a new generation's
  NetworkRegisteredAt. Captured registrations (tests/features/fixtures) show both runtime behaviours: at specs
  467-472 LastRateLimitedBlock and NetworkLastLockCost are written at Q, at spec 443 only at NetworkAdded;
- a new generation key (NetworkRegisteredAt = the NetworkAdded block A) closes the oldest pending registration;
  lag = A - Q (17-25 blocks on chain; 21-24 in the fixtures) and netuid == victim netuid is recorded (assertion only).

Seed assertions at Added (only when the new generation is first seen before start_call, i.e. its pool is still the
seed: no buys before start_call and nobody holds alpha):
- SubnetTAO == lock (NetworkLastLockCost of this registration), relative tolerance 1e-6;
- SubnetAlphaIn == lock / median subnet price: lock / alpha_in equals the median era-correct spot of the other added
  subnets (127 at the limit) within 1e-4. The median is taken on the previous snapshot (subnets that survive the
  diff) and on the current one (excluding new generations), and the smaller error is used; the fixtures reproduce
  the chain seed to about 1e-15;
- quote weight == 0.5 (SwapBalancer quote 5e17).
A failed assertion sets SEED_ANOMALY. A generation first seen after start_call is not checked (no flag). Registry
records are kept while the generation exists and for 216,000 blocks after its registration (the 30-day launch
window), so the registry is a function of a bounded history (features.engine.WARMUP_BLOCKS).

States: QUEUED (pending registration, no generation yet), WAIT_START (no FirstEmissionBlockNumber), WATCH (started,
since_start <= 14 d), MATURE.

Flags (section 2.4 table; vetoes are applied by the overlay policy of section 3.4):
UNSTARTED, SEED_ANOMALY, BURNING: veto all. EMA_WARMING (since_start < 10 d, or not started): veto all except LCW.
EMISSION_OFF: veto all except LCW-paper. YOUNG_IMMUNE, REG_CLOCK_HOT (P(registration within 24 h) > 0.25):
informational / prune-guard tightening. GATE_STARVED (b/theta < 0.4): MONITOR only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ..core.fixed import DEC
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import HALF_E18, PPM, Block, SubnetKey
from ..protocol.prune import ladder, prune_possible

# ------------------------------------------------------------------------------------------------- flags
UNSTARTED: Final[str] = "UNSTARTED"
SEED_ANOMALY: Final[str] = "SEED_ANOMALY"
EMA_WARMING: Final[str] = "EMA_WARMING"
BURNING: Final[str] = "BURNING"
EMISSION_OFF: Final[str] = "EMISSION_OFF"
YOUNG_IMMUNE: Final[str] = "YOUNG_IMMUNE"
REG_CLOCK_HOT: Final[str] = "REG_CLOCK_HOT"
GATE_STARVED: Final[str] = "GATE_STARVED"

VETO_ALL: Final[frozenset[str]] = frozenset({UNSTARTED, SEED_ANOMALY, BURNING})
VETO_ALL_EXCEPT_LCW: Final[frozenset[str]] = frozenset({EMA_WARMING})
VETO_ALL_EXCEPT_LCW_PAPER: Final[frozenset[str]] = frozenset({EMISSION_OFF})
NON_LCW_VETOES: Final[frozenset[str]] = VETO_ALL | VETO_ALL_EXCEPT_LCW | VETO_ALL_EXCEPT_LCW_PAPER
INFORMATIONAL: Final[frozenset[str]] = frozenset({YOUNG_IMMUNE, REG_CLOCK_HOT})
MONITOR_ONLY: Final[frozenset[str]] = frozenset({GATE_STARVED})
ALL_FLAGS: Final[frozenset[str]] = NON_LCW_VETOES | INFORMATIONAL | MONITOR_ONLY


class LaunchState(StrEnum):
    QUEUED = "QUEUED"
    WAIT_START = "WAIT_START"
    WATCH = "WATCH"
    MATURE = "MATURE"


@dataclass(frozen=True, slots=True)
class GatekeeperParams:
    ema_warming_blocks: int = 72_000           # EMA_WARMING: since_start < 10 d
    watch_blocks: int = 100_800                # WATCH: <= 14 d after start
    burning_ppm: int = 600_000                 # BURNING: MinerBurned > 0.60
    reg_clock_hot_ppm: int = 250_000           # REG_CLOCK_HOT: P(reg within 24 h) > 0.25
    reg_clock_horizon_blocks: int = 7_200
    gate_starved_ppm: int = 400_000            # GATE_STARVED: b/theta < 0.4 (monitor)
    seed_tao_tol_ppm: int = 1                  # SubnetTAO == lock within 1e-6
    seed_alpha_tol_ppm: int = 100              # SubnetAlphaIn == lock / median price within 1e-4
    lag_min_blocks: int = 17                   # Queued -> Added lag window (assertion, brief section 4.3)
    lag_max_blocks: int = 25
    pending_max_blocks: int = 7_200            # an unmatched pending registration is dropped after a day
    retention_blocks: int = 216_000            # registry records kept 30 d after registration
    dense_record_blocks: int = 600             # recorder: every block for 600 blocks after start_call ...
    sparse_record_blocks: int = 7_200          # ... then every 10 blocks up to 7,200
    sparse_record_every: int = 10


@dataclass(frozen=True, slots=True)
class PendingRegistration:
    """A registration seen through its prune victim (or LastRateLimitedBlock), awaiting NetworkAdded."""
    queued_lo: Block                           # Q lies in [queued_lo, queued_hi]
    queued_hi: Block
    victim: SubnetKey | None
    lock_rao: int | None                       # NetworkLastLockCost when the runtime recorded it at Q

    @property
    def queued_block(self) -> Block | None:
        return self.queued_lo if self.queued_lo == self.queued_hi else None


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    key: SubnetKey
    added_block: Block                         # = NetworkRegisteredAt
    queued_lo: Block | None
    queued_hi: Block | None
    victim: SubnetKey | None
    netuid_ok: bool | None                     # new netuid == victim netuid (None: no victim known)
    lock_rao: int | None
    seed_checked: bool
    seed_failures: tuple[str, ...]             # subset of ("tao", "alpha", "weight")
    seed_price_rel_err: Decimal | None         # |lock/alpha_in / median spot - 1| (diagnostic)

    @property
    def queued_block(self) -> Block | None:
        if self.queued_lo is not None and self.queued_lo == self.queued_hi:
            return self.queued_lo
        return None

    @property
    def lag_blocks(self) -> int | None:
        q = self.queued_block
        return None if q is None else int(self.added_block) - int(q)

    @property
    def seed_anomaly(self) -> bool:
        return self.seed_checked and bool(self.seed_failures)

    def lag_ok(self, params: GatekeeperParams) -> bool | None:
        lag = self.lag_blocks
        return None if lag is None else params.lag_min_blocks <= lag <= params.lag_max_blocks


def _median_rel_err(implied: Decimal, prices: list[Decimal]) -> Decimal | None:
    if not prices:
        return None
    prices.sort()
    n = len(prices)
    best: Decimal | None = None
    for m in sorted({(n - 1) // 2, n // 2}):
        p = prices[m]
        if p > 0:
            err = abs(DEC.subtract(DEC.divide(implied, p), Decimal(1)))
            best = err if best is None or err < best else best
    return best


def _spots(subnets: list[SubnetState]) -> list[Decimal]:
    return [s.pool.spot() for s in subnets if s.pool.px_tao > 0 and s.pool.px_alpha > 0 and s.key.netuid != 0]


class Gatekeeper:
    """Registry of launches from consecutive snapshots. Deterministic; state is bounded (see module docstring)."""

    def __init__(self, params: GatekeeperParams | None = None) -> None:
        self.params = params if params is not None else GatekeeperParams()
        self._pending: list[PendingRegistration] = []
        self._records: dict[SubnetKey, LaunchRecord] = {}

    # ------------------------------------------------------------------ ingestion
    def observe(self, prev: ChainSnapshot | None, cur: ChainSnapshot) -> tuple[LaunchRecord, ...]:
        """Ingest the diff prev -> cur; returns the launch records created by it (new generations)."""
        created: list[LaunchRecord] = []
        if prev is not None:
            pg, cg = prev.glob, cur.glob
            removed = [s for s in prev.subnets if cur.get(s.key) is None]
            added = sorted((s for s in cur.subnets if prev.get(s.key) is None),
                           key=lambda s: (s.key.reg_at, s.key.netuid))
            new_reg_ats = {int(s.key.reg_at) for s in added}
            reg_seen = cg.last_reg_block > pg.last_reg_block
            q_exact: int | None = None
            if reg_seen and prev.block < cg.last_reg_block <= cur.block and int(cg.last_reg_block) not in new_reg_ats:
                q_exact = int(cg.last_reg_block)
            if removed and (prune_possible(pg) or reg_seen):
                victim = self._victim(prev, removed)
                lo, hi = (q_exact, q_exact) if q_exact is not None else (int(prev.block) + 1, int(cur.block))
                lock = int(cg.last_lock_cost) if q_exact is not None else None
                self._pending.append(PendingRegistration(Block(lo), Block(hi), victim, lock))
            elif q_exact is not None:
                self._pending.append(PendingRegistration(Block(q_exact), Block(q_exact), None, int(cg.last_lock_cost)))
            for g in added:
                rec = self._close(g, prev, cur)
                self._records[g.key] = rec
                created.append(rec)
        self._evict(cur)
        return tuple(created)

    @staticmethod
    def _victim(prev: ChainSnapshot, removed: list[SubnetState]) -> SubnetKey:
        order = {k: i for i, k in enumerate(ladder(prev))}
        return min(removed, key=lambda s: (order.get(s.key, len(order)), s.moving_price, s.key.reg_at, s.key.netuid)).key

    def _close(self, g: SubnetState, prev: ChainSnapshot, cur: ChainSnapshot) -> LaunchRecord:
        p = self.params
        reg_at = int(g.key.reg_at)
        older = [x for x in self._pending if x.queued_lo < reg_at]
        # the new subnet takes the victim's netuid (lowest free id), so a same-netuid pending wins over an older one
        # (e.g. a root dissolve shortly before the registration)
        match = next((x for x in older if x.victim is not None and x.victim.netuid == g.key.netuid), None)
        if match is None:
            match = older[0] if older else None
        if match is not None:
            self._pending.remove(match)
        cg = cur.glob
        lock = match.lock_rao if match is not None else None
        lo = int(match.queued_lo) if match is not None else reg_at - p.lag_max_blocks * 4
        if lock is None and lo <= cg.last_reg_block <= reg_at:
            lock = int(cg.last_lock_cost)                  # the latest recorded registration is this one
        victim = match.victim if match is not None else None
        failures: list[str] = []
        checked = False
        rel_err: Decimal | None = None
        if g.first_emission_block is None:
            checked = True
            if g.pool.w_quote_e18 != HALF_E18:
                failures.append("weight")
            if lock is not None and lock > 0:
                if abs(int(g.pool.tao) - lock) * PPM > p.seed_tao_tol_ppm * lock:
                    failures.append("tao")
                if g.pool.alpha > 0:
                    implied = DEC.divide(Decimal(lock), Decimal(g.pool.alpha))
                    new_keys = {s.key for s in cur.subnets if prev.get(s.key) is None}
                    errs = [e for e in (
                        _median_rel_err(implied, _spots([s for s in prev.subnets if cur.get(s.key) is not None])),
                        _median_rel_err(implied, _spots([s for s in cur.subnets if s.key not in new_keys])),
                    ) if e is not None]
                    rel_err = min(errs) if errs else None
                    if rel_err is None or rel_err * PPM > p.seed_alpha_tol_ppm:
                        failures.append("alpha")
                else:
                    failures.append("alpha")
        return LaunchRecord(
            key=g.key, added_block=Block(reg_at),
            queued_lo=match.queued_lo if match is not None else None,
            queued_hi=Block(min(int(match.queued_hi), reg_at - 1)) if match is not None else None,
            victim=victim, netuid_ok=None if victim is None else victim.netuid == g.key.netuid, lock_rao=lock,
            seed_checked=checked, seed_failures=tuple(failures), seed_price_rel_err=rel_err)

    def _evict(self, cur: ChainSnapshot) -> None:
        p = self.params
        b = int(cur.block)
        self._pending = [x for x in self._pending if b - int(x.queued_hi) <= p.pending_max_blocks]
        for k in sorted(self._records):
            if cur.get(k) is None or int(k.reg_at) < b - p.retention_blocks:
                del self._records[k]

    # ------------------------------------------------------------------ queries
    def record(self, key: SubnetKey) -> LaunchRecord | None:
        return self._records.get(key)

    def records(self) -> tuple[LaunchRecord, ...]:
        return tuple(self._records[k] for k in sorted(self._records))

    def pending(self) -> tuple[PendingRegistration, ...]:
        return tuple(self._pending)

    def state(self) -> tuple[tuple[PendingRegistration, ...], tuple[LaunchRecord, ...]]:
        return self.pending(), self.records()


# ------------------------------------------------------------------------------------------------- pure helpers
def since_start(s: SubnetState, block: int) -> int | None:
    """block - (FirstEmissionBlockNumber - 1); None before start_call."""
    return None if s.first_emission_block is None else block - (int(s.first_emission_block) - 1)


def launch_state(s: SubnetState, block: int, params: GatekeeperParams) -> LaunchState:
    ss = since_start(s, block)
    if ss is None:
        return LaunchState.WAIT_START
    return LaunchState.WATCH if ss <= params.watch_blocks else LaunchState.MATURE


def launch_flags(s: SubnetState, glob: ChainGlobals, block: int, record: LaunchRecord | None, *,
                 p_reg_24h: Decimal | None, b_over_theta: Decimal | None, params: GatekeeperParams) -> frozenset[str]:
    """The section 2.4 mechanical flags of one generation."""
    flags: set[str] = set()
    ss = since_start(s, block)
    if s.first_emission_block is None:
        flags.add(UNSTARTED)
    if record is not None and record.seed_anomaly:
        flags.add(SEED_ANOMALY)
    if ss is None or ss < params.ema_warming_blocks:
        flags.add(EMA_WARMING)
    if s.miner_burned * PPM > params.burning_ppm:
        flags.add(BURNING)
    if not s.emission_enabled:
        flags.add(EMISSION_OFF)
    if block - int(s.key.reg_at) < glob.immunity_period:
        flags.add(YOUNG_IMMUNE)
    if p_reg_24h is not None and p_reg_24h * PPM > params.reg_clock_hot_ppm:
        flags.add(REG_CLOCK_HOT)
    if b_over_theta is not None and b_over_theta * PPM < params.gate_starved_ppm:
        flags.add(GATE_STARVED)
    return frozenset(flags)


def recorder_cadence(s: SubnetState, block: int, params: GatekeeperParams) -> int | None:
    """Dense-recorder cadence after start_call (section 2.4): 1 for 600 blocks, then 10 up to 7,200; None after."""
    ss = since_start(s, block)
    if ss is None or ss < 0:
        return None
    if ss < params.dense_record_blocks:
        return 1
    if ss < params.sparse_record_blocks:
        return params.sparse_record_every
    return None
