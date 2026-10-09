"""taotrader/venues/paper.py - PaperVenue: SimVenue on the live feed + the sim_swap drift probe (WP6; DESIGN.md 9.1).

PaperVenue = SimVenue(exact_fills=True) with three additions:
- Submit head from the measured finality lag. The Runner journals HealthObs with every SnapshotObserved; the venue
  folds `finality_lag_blocks` per block, so an order decided on finalized block b is acked with
  submit_block = b + lag(b) and expected_fill_block = submit_block + latency (N+2), "submit best-head + 2". The TTL
  still uses the configured lag (the planner's valid_until). A shielded order whose N+2 would fall outside its
  8-block carrier era (valid in anchor .. anchor + 7, so lag + latency > 7, i.e. finality lag > 5 at latency 2;
  section 3.12) is rejected before submission (`shield_era_stale`, fee 0).
- Recorded fill-block state. The Runner ticks every finalized block, so a shielded order normally settles on the
  recorded state of exactly N+2. When the feed skipped N+2 (feed_gap_blocks > 0), the venue reads that block from the
  injected SnapshotStore (the recorder's hot buffer / lake), else from the injected ChainReader (FULL read at N+2 with
  the order's hotkeys tracked). If neither has it, the order is MISSED (a fill at N+3..N+8 is never legal).
- Model drift probe. At every simulated swap fill, call sim_swap_* at the fill block hash for the filled amount and
  compare its output with protocol.amm on the UN-overlaid pool of that block. If |local - chain| / chain > 5 bp
  (all-zero chain results count as failures, err = 1e6 ppm), advance() returns
  ModelDriftObserved(probe="sim_swap_buy"|"sim_swap_sell") BEFORE the fill; the fill follows on the next call. The
  engine sets CAUTION from it (section 3.11). A probe whose RPC raises is skipped (counted in `probe_errors`), and
  a probe already journaled for (netuid, fill block) is not repeated (rebuilt by observe()).

Paper cannot know whether a carrier would have been included: the configured miss rate applies (optimistic, 9.1).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ..core.config import ExecCfg
from ..core.events import FillReported, JournalEvent, ModelDriftObserved, SnapshotObserved
from ..core.orders import Fill, OrderIntent, OrderKind
from ..core.protocols import ChainReader, SnapshotStore
from ..core.state import ChainSnapshot, PoolState, ReadPlan
from ..core.units import PPM, AlphaRao, Block, BookId, Hotkey, Rao, SubnetKey
from ..protocol.amm import SwapError, quote_buy, quote_sell
from .sim import SHIELD_ERA_BLOCKS, SimVenue

if TYPE_CHECKING:
    from ..core.protocols import ExecutionVenue

__all__ = ["DRIFT_THRESHOLD_PPM", "PROBE_BUY", "PROBE_SELL", "PaperVenue", "drift_ppm"]

DRIFT_THRESHOLD_PPM: Final[int] = 500        # 5 bp (section 9.1)
PROBE_BUY: Final[str] = "sim_swap_buy"
PROBE_SELL: Final[str] = "sim_swap_sell"
_LAG_WINDOW_BLOCKS: Final[int] = 7_200       # measured finality lags kept per block (one day)


def drift_ppm(local_out: int, chain_out: int) -> int:
    """|local - chain| / chain in ppm (rounded up). A failed chain simulation (0) against a local fill is 1e6."""
    if chain_out <= 0:
        return PPM if local_out > 0 else 0
    return -((-abs(local_out - chain_out) * PPM) // chain_out)


class PaperVenue(SimVenue):
    """ExecutionVenue for `taotrader paper` (native Windows): exact N+2 fills on recorded finalized blocks."""

    KIND: str = "paper"

    def __init__(self, book: BookId, cfg: ExecCfg | None = None, *, reader: ChainReader, seed: int = 0,
                 store: SnapshotStore | None = None, drift_threshold_ppm: int = DRIFT_THRESHOLD_PPM) -> None:
        super().__init__(book, cfg, seed=seed, exact_fills=True)
        if drift_threshold_ppm < 0:
            raise ValueError("drift_threshold_ppm must be >= 0")
        self.reader = reader
        self.store = store
        self.drift_threshold_ppm = drift_threshold_ppm
        self._lag_at: dict[int, int] = {}                 # journal-derived: block -> measured finality lag
        self._probed: set[tuple[int, int]] = set()        # journal-derived: (netuid, block) with a drift event
        self.probe_errors = 0                             # diagnostics only (never on a decision path)
        self.state_errors = 0

    # ------------------------------------------------------------------ journal fold
    def observe(self, ev: JournalEvent) -> None:
        if isinstance(ev, SnapshotObserved):
            self._lag_at[int(ev.block)] = ev.health.finality_lag_blocks
            floor = int(ev.block) - _LAG_WINDOW_BLOCKS
            while self._lag_at:
                oldest = next(iter(self._lag_at))
                if oldest >= floor:
                    break
                del self._lag_at[oldest]
        elif isinstance(ev, ModelDriftObserved) and ev.probe in (PROBE_BUY, PROBE_SELL) and ev.netuid is not None:
            self._probed.add((int(ev.netuid), int(ev.block)))
        super().observe(ev)

    # ------------------------------------------------------------------ SimVenue hooks
    def _finality_lag(self, anchor: Block) -> int:
        return self._lag_at.get(int(anchor), self.cfg.finality_lag_blocks)

    def _extra_submit_check(self, intent: OrderIntent, anchor: Block, expected: Block) -> str | None:
        # A mortal era of period 8 born at the finalized anchor is valid in blocks anchor .. anchor + 7 (death =
        # birth + period, exclusive; brief 5.9: the SDK anchors at the finalized head). N+2 must be one of them, i.e.
        # finality lag <= 5 at latency 2 (section 3.12: "finality lag > 5 blocks (the shield era is stale)").
        last_valid = int(anchor) + SHIELD_ERA_BLOCKS - 1
        if intent.shielded and int(expected) > last_valid:
            return (f"shield_era_stale: N+2 = {expected} is past the carrier era's last block {last_valid} "
                    f"(finality lag {self._finality_lag(anchor)})")
        return None

    async def _recorded_state(self, block: Block, intent: OrderIntent) -> ChainSnapshot | None:
        if self.store is not None:
            try:
                snap = self.store.at(block)
                if snap.block == block:
                    return snap
            except Exception:            # absent from the hot buffer / lake: fall back to the reader
                self.state_errors += 1
        tracked: list[tuple[SubnetKey, Hotkey]] = [(intent.key, intent.hotkey)]
        if intent.dest_hotkey is not None:
            tracked.append((intent.key, intent.dest_hotkey))
        try:
            block_hash = await self.reader.block_hash(block)
            snap = await self.reader.snapshot(block, block_hash, ReadPlan.FULL, None, tuple(sorted(tracked)))
        except Exception:                # unreadable: the caller declares the order missed (fail closed)
            self.state_errors += 1
            return None
        return snap if snap.block == block else None

    async def _after_settle(self, ev: JournalEvent, raw: ChainSnapshot | None, state: ChainSnapshot) -> JournalEvent:
        if not isinstance(ev, FillReported) or ev.fill.kind not in (
                OrderKind.ADD_STAKE_LIMIT, OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
            return ev
        f = ev.fill
        if (int(f.key.netuid), int(f.block)) in self._probed:
            return ev
        base = raw if raw is not None else self.unmark(state)
        s = base.get(f.key)
        if s is None:
            return ev
        drift = await self._probe(f, s.pool, base)
        return drift if drift is not None else ev

    # ------------------------------------------------------------------ the probe
    async def _probe(self, f: Fill, pool: PoolState, base: ChainSnapshot) -> ModelDriftObserved | None:
        buy = f.kind is OrderKind.ADD_STAKE_LIMIT
        try:
            if buy:
                sim = await self.reader.sim_swap_buy(f.key.netuid, int(f.tao), base.block_hash)
            else:
                sim = await self.reader.sim_swap_sell(f.key.netuid, int(f.alpha), base.block_hash)
        except Exception:                # provider trouble is not model drift
            self.probe_errors += 1
            return None
        failed = sim.tao_amount == 0 or sim.alpha_amount == 0
        try:
            local = quote_buy(pool, Rao(f.tao)).amount_out if buy else quote_sell(pool, AlphaRao(f.alpha)).amount_out
        except SwapError:
            local = 0
        chain = 0 if failed else (sim.alpha_amount if buy else sim.tao_amount)
        err = PPM if failed else drift_ppm(local, chain)
        if err <= self.drift_threshold_ppm:
            return None
        return ModelDriftObserved(block=f.block, probe=PROBE_BUY if buy else PROBE_SELL, netuid=f.key.netuid, err_ppm=err)


if TYPE_CHECKING:
    def _conforms(v: PaperVenue) -> ExecutionVenue:     # mypy: PaperVenue satisfies the section-5.10 protocol
        return v
