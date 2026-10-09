"""taotrader/features/engine.py - the FeatureEngine (WP5; DESIGN.md sections 4.4, 5.8, 5.10, 11).

One instance per run, shared by every book: `update(raw, events, own_fill_blocks)` turns each snapshot into a
FeatureFrame (core.views) from rolling, generation-keyed buffers. Book-INDEPENDENT only: nothing here reads a book's
orders, fills, holdings or memory; the union of own-fill blocks only REMOVES samples from the beta buffers.

Determinism.
- Pure: no clock, I/O, randomness or unsorted iteration. Every value is a function of the snapshot sequence, the
  own-fill blocks and the injected CalibrationProvider; transcendental functions are Decimal-exact (features.micro).
- Window-determinism: the engine state after ingesting the snapshots of [b0, t] depends only on those of
  [max(b0, t - WARMUP_BLOCKS), t]. Every buffer is truncated by absolute block age and nothing derived from older
  data survives (bounded retention everywhere; take increases need a FULL observation <= 1 day older; the Gatekeeper
  keeps records 30 days). Recovery therefore re-ingests [max(first ingested block, t - WARMUP_BLOCKS), t] from the
  lake and reproduces the journaled features digest (section 4.5 step 3).
- `FeatureFrame.digest` is codec.digest of the frame without its digest field (`frame_digest`); `state_digest`
  covers every buffer (not the first-ingested block, which window replays do not share; `warm` stands for it).

Feature definitions (section 5.8 field by field; numbers are DESIGN.md defaults):
- price: spot = PoolState.spot() (era-correct); pbar_t = median(p_t, p_{t-60}, p_{t-120}) from the first-per-60-block
  grid (features.micro.GridBuffer, lookups at most 60 blocks stale); ret_1h / 1d / 7d = ln pbar_t - ln pbar_{t-h}
  (h = 300 / 7,200 / 50,400); sigma_d = sample SD of the 14 daily ln pbar differences over 14 days. Buffers are keyed
  by SubnetKey, so every lookback is truncated at NetworkRegisteredAt (a reused netuid starts empty).
- fast_ema_gap = ln(min(spot, 1) / local 600-block-half-life EMA of min(spot, 1)) (micro.FastEma); ema_gap =
  ln(min(spot, 1) / SubnetMovingPrice). Both 0.0 when a price is missing.
- flows: flow_h = (SubnetTaoFlow_t - SubnetTaoFlow_{t-h}) / SubnetTAO_t; None unless both values exist in the same
  generation and the older one is at a block >= 8,466,531 (protocol.derive.flow_valid_from). flow_z_1d is the robust
  z of flow_1d against its values on the first snapshot of each 600-block cell of the trailing 30 days (>= 72 values).
- emission (protocol.emission.emission_vector, refresh_theta at (block + 1) % 360 == 0): emis_tao_day and
  chain_buy_day are the modelled E_i and chain buy of the next block; obs_emis_tao_day is the trailing-300-block mean
  of SubnetTaoInEmission + SubnetExcessTao plus the BalancerTaoReservoir delta per block. EmissionView.model_ok is
  protocol.emission.parity_ok over enabled, emit-eligible subnets, comparing the trailing-300-block MEAN of the model
  (predictions made at blocks t-300 .. t-1 for t-299 .. t) with the trailing observed emission over the same blocks.
- structure: sell_push_day and cb_push_day from protocol.sellload with the frozen prior SellLoadParams; the chain-buy
  CB fed to it is the OBSERVED trailing-300-block mean of SubnetExcessTao (section 2.1 "struct"), while chain_buy_day
  is the model. root_flag = sum of SubnetMovingPrice over emit-eligible subnets > 1. burn_adj_rank ranks
  emit-eligible subnets by burn-adjusted share b; ema_rank_desc ranks started subnets by SubnetMovingPrice
  (ties by (reg_at, netuid)).
- yield: a_earn_alpha (tracked earners), yield_cf_gross_day (closed form), a_earn_growth_day = deterministic A_earn
  growth / A_earn; router_candidates, best_candidate and yield_net_day from features.yield_router.
- prune: protocol.prune ladder, rank, rho = EMA / bottom EMA; t_star_stress_blocks = protocol.ema.t_star_blocks
  with s = (1 - D_STRESS) * spot and the subnet's own a_k (None if immune, never crossing, or frozen). PruneView
  p_reg_ppm uses the hazard of CalibrationProvider.asof(block) (check_asof enforced) at horizons 1,800 / 7,200 /
  36,000 / 50,400 blocks.
- beta: q95 / q99 of |ln p_s - ln p_{s-h}| over s in (t - 1,800, t] with own-fill windows excluded (micro), h =
  max(gap to the previous snapshot, finality_lag + latency); fewer than 10 samples -> 1,000,000 ppm (unknown: the
  widest buffer; the planner clamps to its cap).
- owner (section 3.6): owner_sold_6h_frac = max(0, -sum of protocol.derive.owner_position_delta over consecutive
  FULL snapshots ending in (t - 1,800, t]) / SubnetAlphaIn; owner_liquid_frac = owner_alpha * (1 - autolock) /
  SubnetAlphaIn (autolock unknown -> liquid); top_holder_frac = (owner_alpha + TotalHotkeyAlpha of the 5 largest
  tracked hotkeys other than the owner hotkey) / (AlphaOut - ProtocolAlpha). None when an input is missing.
- launch_flags: features.gatekeeper; universe_eligible: features.universe sections A-G.
- warm: at least 216,000 blocks (30 d) since the first ingested snapshot.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Final

from ..core.codec import digest, encode
from ..core.config import ExecCfg, RiskCfg
from ..core.events import ChainEvent
from ..core.fixed import DEC, to_ppm
from ..core.state import ChainSnapshot, ReadPlan, SubnetState
from ..core.units import BLOCKS_PER_DAY, PERQUINTILL, PPM, RAO_PER_TAO, Block, Hotkey, Ppm, Rao, SubnetKey
from ..core.views import EmissionView, Feat, FeatureFrame, PruneView
from ..protocol.calibration import Calibration, CalibrationProvider, check_asof
from ..protocol.derive import flow_valid_from, owner_position_delta
from ..protocol.ema import ema_alpha, t_star_blocks
from ..protocol.emission import (
    EmissionShare,
    emission_vector,
    emit_eligible,
    gate_theta,
    parity_ok,
    parity_rel_errors,
    sum_ema,
)
from ..protocol.prune import (
    cost_ratio,
    immunity_end,
    ladder,
    p_registration,
    prune_possible,
    window_open_block,
)
from ..protocol.regimes import regime, regime_at
from ..protocol.sellload import SellLoadParams, sell_load
from ..protocol.yield_model import a_earn, a_earn_growth_per_day, closed_form_yield_gross
from . import universe as uni
from .gatekeeper import Gatekeeper, GatekeeperParams, launch_flags, since_start
from .micro import (
    BETA_WINDOW_BLOCKS,
    PRICE_GRID_BLOCKS,
    DenseBuffer,
    FastEma,
    GridBuffer,
    GridPoint,
    beta_horizon,
    beta_quantiles,
    beta_samples,
    finite,
    ln,
    median3,
    robust_z,
)
from .yield_router import RouterParams, RouterResult, TakeBook, YieldPanel, router_candidates

# ------------------------------------------------------------------------------------------------- constants
WARM_BLOCKS: Final[int] = 216_000              # warm after 30 d of ingested history
WARMUP_BLOCKS: Final[int] = 230_400            # window that determines the state (32 d >= 30 d + 1 d + margins)
GRID_RETENTION: Final[int] = 14 * BLOCKS_PER_DAY + 240        # sigma_d reaches back 14 d + 2 grid steps
GRID_MAX_STALE: Final[int] = PRICE_GRID_BLOCKS
DENSE_RETENTION: Final[int] = BETA_WINDOW_BLOCKS + 601        # beta window + the largest supported stride h
RET_1H: Final[int] = 300
RET_1D: Final[int] = BLOCKS_PER_DAY
RET_7D: Final[int] = 7 * BLOCKS_PER_DAY
SIGMA_DAYS: Final[int] = 14
FLOW_Z_CELL: Final[int] = 600
FLOW_Z_WINDOW: Final[int] = 30 * BLOCKS_PER_DAY
FLOW_Z_MIN_SAMPLES: Final[int] = 72                          # 6 days of 600-block cells
OBS_WINDOW: Final[int] = 300                                 # trailing observed emission / chain buy
OWNER_WINDOW: Final[int] = 1_800                             # owner_sold_6h
TOP_HOLDERS: Final[int] = 5
GATE_REFRESH_BLOCKS: Final[int] = 360                        # rank-mode bar refresh: (block + 1) % 360 == 0
BETA_UNKNOWN_PPM: Final[Ppm] = Ppm(PPM)
P_REG_HORIZONS: Final[tuple[int, ...]] = (1_800, 7_200, 36_000, 50_400)

_RAO = float(RAO_PER_TAO)
_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class FeatureParams:
    """Engine-level (book-independent) parameters. Thresholds come from the RiskCfg/ExecCfg DEFAULTS (from_config);
    books with stricter values re-filter on the published raw fields (take, ranks, flags)."""
    d_stress_ppm: int = 500_000                # RiskCfg.d_stress_ppm (Tier A stressed spot = (1 - D) * spot)
    finality_lag_blocks: int = 3               # ExecCfg: beta horizon h = finality_lag + latency on per-block data
    latency_blocks: int = 2
    warm_blocks: int = WARM_BLOCKS
    p_reg_horizons: tuple[int, ...] = P_REG_HORIZONS
    sell_load: SellLoadParams = SellLoadParams()   # frozen priors (section 2.1); T3 measurements arrive via WP4
    router: RouterParams = RouterParams()
    gatekeeper: GatekeeperParams = GatekeeperParams()
    universe: uni.UniverseParams = uni.UniverseParams()

    @staticmethod
    def from_config(risk: RiskCfg | None = None, exec_cfg: ExecCfg | None = None) -> FeatureParams:
        r = risk if risk is not None else RiskCfg()
        x = exec_cfg if exec_cfg is not None else ExecCfg()
        return FeatureParams(d_stress_ppm=int(r.d_stress_ppm), finality_lag_blocks=x.finality_lag_blocks,
                             latency_blocks=x.latency_blocks, router=RouterParams.from_risk(r),
                             universe=uni.UniverseParams.from_risk(r))


_FIELD_NAMES: dict[type[Any], tuple[str, ...]] = {}


def _field_names(cls: type[Any]) -> tuple[str, ...]:
    names = _FIELD_NAMES.get(cls)
    if names is None:
        names = tuple(sorted(f.name for f in dataclasses.fields(cls)
                             if not f.name.startswith("_") and f.metadata.get("codec", True) is not False))
        _FIELD_NAMES[cls] = names
    return names


def _json_key(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_value(obj: Any) -> Any:
    """core.codec.encode(obj) for the value types of a FeatureFrame, with per-class field-name caching (the frame is
    encoded once per snapshot). Any other type is delegated to core.codec.encode; tests assert equality."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj if not isinstance(obj, Enum) else encode(obj)
    t = type(obj)
    if t is int:
        return obj
    if t is float:
        if not math.isfinite(obj):
            raise ValueError(f"non-finite float {obj!r}")
        return obj
    if t is tuple or t is list:
        return [canonical_value(x) for x in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {n: canonical_value(getattr(obj, n)) for n in _field_names(t)}
    if isinstance(obj, (frozenset, set)):
        return sorted((canonical_value(x) for x in obj), key=_json_key)
    if isinstance(obj, Mapping) and not all(isinstance(k, str) for k in obj):
        return sorted(([canonical_value(k), canonical_value(v)] for k, v in obj.items()), key=lambda kv: _json_key(kv[0]))
    return encode(obj)


def replay_start(first_ingested: int, block: int) -> int:
    """First block a recovery must re-ingest (stride as originally ingested) so that a fresh engine reproduces the
    state and frames at `block`: max(first block the run ingested, block - WARMUP_BLOCKS)."""
    return max(int(first_ingested), int(block) - WARMUP_BLOCKS)


def frame_digest(frame: FeatureFrame) -> str:
    """Canonical digest of a frame: core.codec.digest of every field except `digest` (blake2b-128 of the canonical
    JSON bytes)."""
    payload: dict[str, Any] = canonical_value(frame)
    payload.pop("digest", None)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def _pool_spot(s: SubnetState) -> Decimal:
    return s.pool.spot() if s.pool.px_tao > 0 and s.pool.px_alpha > 0 else Decimal(0)


def _gap(ln_num: float | None, den: Decimal | None) -> float:
    """ln_num - ln(den): a log gap, 0.0 when either side is undefined."""
    if ln_num is None or den is None:
        return 0.0
    ln_den = ln(den)
    return 0.0 if ln_den is None else ln_num - ln_den


def _prob_ppm(p: Decimal) -> Ppm:
    return Ppm(int(DEC.multiply(p, Decimal(PPM)).to_integral_value(rounding=ROUND_HALF_EVEN)))


# ------------------------------------------------------------------------------------------------- per generation
class _Gen:
    """Rolling buffers of one generation (netuid, reg_at)."""

    __slots__ = ("dense", "fast", "flow_cell", "flow_hist", "grid", "key", "last", "last_disable", "last_enable", "obs",
                 "owner_deltas", "owner_full", "panel")

    def __init__(self, key: SubnetKey, epochs_kept: int) -> None:
        self.key = key
        self.grid = GridBuffer(PRICE_GRID_BLOCKS, GRID_RETENTION)
        self.dense = DenseBuffer(DENSE_RETENTION)
        self.fast = FastEma()
        self.flow_hist: deque[tuple[int, float | None]] = deque()   # first snapshot of each 600-block cell
        self.flow_cell: int | None = None
        self.obs: deque[tuple[int, int, int, int, int]] = deque()   # (block, model next, tao_in+excess, reservoir, excess)
        self.owner_full: tuple[int, SubnetState] | None = None      # last FULL observation
        self.owner_deltas: deque[tuple[int, int]] = deque()         # (block, signed owner position delta)
        self.last: SubnetState | None = None
        self.last_disable: int | None = None
        self.last_enable: int | None = None
        self.panel = YieldPanel(epochs_kept)

    # ------------------------------------------------------------------ ingestion
    def ingest(self, s: SubnetState, snap: ChainSnapshot, full: bool, model_next: int, up: uni.UniverseParams) -> None:
        block = int(snap.block)
        spot = _pool_spot(s)
        lnp = ln(spot)
        self.grid.add(GridPoint(block, lnp, s.tao_flow_cum))
        self.grid.evict(block)
        self.dense.add(block, lnp)
        self.dense.evict(block)
        if spot > 0:
            self.fast.update(block, float(min(spot, _ONE)))
        self.obs.append((block, model_next, int(s.tao_in_emission) + int(s.excess_tao), int(s.reservoir_tao),
                         int(s.excess_tao)))
        while self.obs and self.obs[0][0] < block - OBS_WINDOW:
            self.obs.popleft()
        last = self.last
        if last is not None:
            if last.emission_enabled and not s.emission_enabled:
                self.last_disable = block
            elif not last.emission_enabled and s.emission_enabled:
                self.last_enable = block
        if self.last_disable is not None and block >= self.last_disable + up.emission_ban_blocks:
            self.last_disable = None
        if self.last_enable is not None and block >= self.last_enable + up.reenable_wait_blocks:
            self.last_enable = None
        if full:
            if self.owner_full is not None:
                b0, s0 = self.owner_full
                d = owner_position_delta(s0, s, snap.glob, Block(b0), Block(block))
                if d is not None:
                    self.owner_deltas.append((block, d))
            self.owner_full = (block, s)
            self.panel.observe(s, block)
        while self.owner_deltas and self.owner_deltas[0][0] <= block - OWNER_WINDOW:
            self.owner_deltas.popleft()
        self.last = s

    # ------------------------------------------------------------------ price features
    def _ln_at(self, x: int) -> float | None:
        p = self.grid.at(x, GRID_MAX_STALE)
        return None if p is None else p.ln_p

    def pbar_at(self, anchor: int) -> float | None:
        return median3(self._ln_at(anchor), self._ln_at(anchor - 60), self._ln_at(anchor - 120))

    def pbar_now(self, block: int, lnp: float | None) -> float | None:
        return median3(lnp, self._ln_at(block - 60), self._ln_at(block - 120))

    def flow(self, s: SubnetState, block: int, h: int) -> float | None:
        f_now = s.tao_flow_cum
        if f_now is None or s.pool.tao <= 0 or block - h < flow_valid_from():
            return None
        p = self.grid.at(block - h, GRID_MAX_STALE)
        if p is None or p.flow_cum is None or p.block < flow_valid_from():
            return None
        return (f_now - p.flow_cum) / s.pool.tao

    def record_flow_1d(self, block: int, flow_1d: float | None) -> float | None:
        """Keep flow_1d of the first snapshot of each 600-block cell (30 d); return the robust z of flow_1d."""
        cell = block // FLOW_Z_CELL
        if self.flow_cell is None or cell != self.flow_cell:
            self.flow_hist.append((block, flow_1d))
            self.flow_cell = cell
        while self.flow_hist and self.flow_hist[0][0] <= block - FLOW_Z_WINDOW:
            self.flow_hist.popleft()
        if flow_1d is None:
            return None
        hist = [v for _, v in self.flow_hist if v is not None]
        return robust_z(flow_1d, hist, FLOW_Z_MIN_SAMPLES)

    def sigma_d(self, block: int, pbar_now: float | None) -> float | None:
        pts = [pbar_now] + [self.pbar_at(block - k * BLOCKS_PER_DAY) for k in range(1, SIGMA_DAYS + 1)]
        vals = [p for p in pts if p is not None]
        if len(vals) != len(pts):
            return None
        rets = [vals[k] - vals[k + 1] for k in range(SIGMA_DAYS)]
        mean = math.fsum(rets) / SIGMA_DAYS
        var = math.fsum((r - mean) ** 2 for r in rets) / (SIGMA_DAYS - 1)
        return math.sqrt(var)

    # ------------------------------------------------------------------ emission windows
    def emission_window(self, block: int) -> tuple[int, int, int]:
        """(model rao/day, observed rao/day, observed excess rao/block), trailing 300 blocks."""
        model = [m for b, m, _, _, _ in self.obs if block - OBS_WINDOW <= b <= block - 1]
        if not model:
            model = [self.obs[-1][1]]
        recent = [(b, o, r, x) for b, _, o, r, x in self.obs if b >= block - OBS_WINDOW + 1]
        n = len(recent)
        obs_day = sum(o for _, o, _, _ in recent) * BLOCKS_PER_DAY // n
        older = [(b, r) for b, _, _, r, _ in self.obs if block - OBS_WINDOW <= b < block]
        if older:
            b0, r0 = older[0]
            obs_day += (self.obs[-1][3] - r0) * BLOCKS_PER_DAY // (block - b0)
        excess = sum(x for _, _, _, x in recent) // n
        return sum(model) * BLOCKS_PER_DAY // len(model), obs_day, excess

    def owner_sold_frac(self, s: SubnetState) -> float | None:
        if not self.owner_deltas or s.pool.alpha <= 0:
            return None
        net_sold = -sum(d for _, d in self.owner_deltas)
        return max(net_sold, 0) / s.pool.alpha

    def state(self) -> tuple[object, ...]:
        return (self.grid.points(), tuple(self.flow_hist), self.flow_cell, self.dense.items(), self.fast.state(),
                tuple(self.obs), None if self.owner_full is None else self.owner_full[0], tuple(self.owner_deltas),
                None if self.last is None else self.last.key, self.last_disable, self.last_enable, self.panel.state())


# ------------------------------------------------------------------------------------------------- the engine
class FeatureEngine:
    """core.protocols.FeatureEngine implementation (see module docstring)."""

    def __init__(self, calibration: CalibrationProvider, params: FeatureParams | None = None) -> None:
        self.params = params if params is not None else FeatureParams()
        self._calibration = calibration
        self._gens: dict[SubnetKey, _Gen] = {}
        self._takes = TakeBook(self.params.router.take_increase_window_blocks, self.params.router.take_obs_max_gap_blocks)
        self._gk = Gatekeeper(self.params.gatekeeper)
        self._prev: ChainSnapshot | None = None
        self._first: int | None = None
        self._last_frame: FeatureFrame | None = None
        self._last_universe: tuple[uni.UniverseRow, ...] = ()
        self._last_calibration: Calibration | None = None
        self.warm: bool = False

    # ------------------------------------------------------------------ accessors
    @property
    def gatekeeper(self) -> Gatekeeper:
        return self._gk

    @property
    def last_frame(self) -> FeatureFrame | None:
        return self._last_frame

    @property
    def last_universe(self) -> tuple[uni.UniverseRow, ...]:
        """Per-generation section A-G results of the last frame (reports, WP8 per-book counts)."""
        return self._last_universe

    @property
    def last_calibration(self) -> Calibration | None:
        return self._last_calibration

    @property
    def first_block(self) -> int | None:
        """First block ingested by this engine (replay_start input; not part of state_digest)."""
        return self._first

    def state_digest(self) -> str:
        gens = tuple((k, self._gens[k].state()) for k in sorted(self._gens))
        prev = None if self._prev is None else int(self._prev.block)
        return digest((prev, self.warm, gens, self._takes.state(), self._gk.state()))

    # ------------------------------------------------------------------ update
    def update(self, raw: ChainSnapshot, events: Sequence[ChainEvent],
               own_fill_blocks: frozenset[Block] = frozenset()) -> FeatureFrame:
        """Ingest one snapshot (strictly increasing blocks) and publish its FeatureFrame.

        `events` must be the chain events derived for this snapshot (their block is checked); the features are
        derived from the snapshot sequence itself, so a re-ingest that passes the same snapshots reproduces them."""
        p = self.params
        block = int(raw.block)
        prev = self._prev
        if prev is not None and block <= int(prev.block):
            raise ValueError(f"snapshots must be strictly increasing: {block} after {int(prev.block)}")
        for ev in events:
            if ev.block != raw.block:
                raise ValueError(f"chain event of block {ev.block} passed with snapshot {block}")
        calib = self._calibration.asof(raw.block)
        check_asof(calib, raw.block)
        self._last_calibration = calib
        if self._first is None:
            self._first = block
        glob = raw.glob
        full = raw.plan == ReadPlan.FULL
        gap = None if prev is None else block - int(prev.block)
        h = beta_horizon(gap, p.finality_lag_blocks + p.latency_blocks)
        own = sorted(int(b) for b in own_fill_blocks if block - BETA_WINDOW_BLOCKS - h < b <= block)

        # ---- shared protocol models (once per snapshot)
        refresh = (block + 1) % GATE_REFRESH_BLOCKS == 0
        shares = emission_vector(raw, refresh_theta=refresh)
        sum_e = sum_ema(raw)
        root_flag = sum_e > 1
        theta = _theta(raw, shares, refresh)
        lad = ladder(raw)
        ranks = {k: i + 1 for i, k in enumerate(lad)}
        target = lad[0] if lad else None
        target_state = raw.get(target) if target is not None else None
        bottom = target_state.moving_price if target_state is not None else None

        # ---- ingest
        epochs_kept = p.router.epochs_kept
        gens: dict[SubnetKey, _Gen] = {}
        for s in raw.subnets:
            g = self._gens.get(s.key)
            if g is None:
                g = _Gen(s.key, epochs_kept)
            g.ingest(s, raw, full, int(shares[s.key].tao_per_block), p.universe)
            gens[s.key] = g
        self._gens = gens
        if full:
            for s in raw.subnets:
                for hk in s.hotkeys:
                    self._takes.observe(hk.hotkey, hk.take_u16, block)
        self._takes.evict(block)
        self._gk.observe(prev, raw)

        # ---- prune view
        hazard = calib.hazard
        p_reg = tuple((hz, p_registration(glob, hazard, raw.block, hz)) for hz in p.p_reg_horizons)
        p_by_h = dict(p_reg)
        hot_h = p.gatekeeper.reg_clock_horizon_blocks
        p24 = p_by_h[hot_h] if hot_h in p_by_h else p_registration(glob, hazard, raw.block, hot_h)
        opens = int(window_open_block(glob))
        r_cost = cost_ratio(glob, raw.block)
        prune_view = PruneView(
            prune_possible=prune_possible(glob), target=target,
            runtime_agrees=glob.runtime_prune_target is None or (target is not None and target.netuid == glob.runtime_prune_target),
            ladder=lad, bottom_ema=float(bottom) if bottom is not None else 0.0,
            blocks_since_reg=block - int(glob.last_reg_block), window_open=block >= opens,
            blocks_to_window=max(0, opens - block), cost_ratio=float(r_cost),
            p_reg_ppm=tuple((hz, _prob_ppm(pr)) for hz, pr in p_reg), hazard_valid=hazard.valid,
            immunity_calendar=tuple(sorted(((immunity_end(s, glob), s.key) for s in raw.subnets
                                            if block < immunity_end(s, glob)), key=lambda x: (int(x[0]), x[1]))))

        # ---- cross-sectional ranks
        eligible_emit = [s for s in raw.subnets if emit_eligible(s)]
        burn_rank = {s.key: i + 1 for i, s in enumerate(sorted(
            eligible_emit, key=lambda s: (-shares[s.key].b, s.key.reg_at, s.key.netuid)))}
        ema_rank = {s.key: i + 1 for i, s in enumerate(sorted(
            (s for s in raw.subnets if s.first_emission_block is not None),
            key=lambda s: (-s.moving_price, s.key.reg_at, s.key.netuid)))}

        # ---- per generation features
        stress_keep = DEC.divide(Decimal(PPM - p.d_stress_ppm), Decimal(PPM))      # stressed spot = (1 - D) * spot
        feats: dict[SubnetKey, Feat] = {}
        t_star_map: dict[SubnetKey, int | None] = {}
        flags_map: dict[SubnetKey, frozenset[str]] = {}
        validator_ok: dict[SubnetKey, bool] = {}
        bans: dict[SubnetKey, int | None] = {}
        parity_pairs: list[tuple[int, int]] = []
        for s in sorted(raw.subnets, key=lambda x: x.key):
            g = gens[s.key]
            share = shares[s.key]
            spot = _pool_spot(s)
            lnp = g.dense.get(block)
            ln_cap = None if lnp is None else min(lnp, 0.0)        # ln(min(spot, 1))
            fast = g.fast.value()
            pbar = g.pbar_now(block, lnp)
            ret = {hz: (None if pbar is None or (old := g.pbar_at(block - hz)) is None else pbar - old)
                   for hz in (RET_1H, RET_1D, RET_7D)}
            flow_1d = g.flow(s, block, RET_1D)
            flow_z = g.record_flow_1d(block, flow_1d)
            model_day, obs_day, excess_obs = g.emission_window(block)
            if emit_eligible(s) and s.emission_enabled:
                parity_pairs.append((model_day, obs_day))
            sl = sell_load(s, glob, replace(share, chain_buy_per_block=Rao(excess_obs)), p.sell_load, raw.block, root_flag)
            ae = int(a_earn(s))
            growth = DEC.divide(a_earn_growth_per_day(s, glob, root_flag), Decimal(ae)) if ae > 0 else Decimal(0)
            rr: RouterResult = router_candidates(s, glob, g.panel, self._takes, block, p.router)
            rank = ranks.get(s.key)
            t_star: int | None = None
            if rank is not None and bottom is not None:
                t_star = t_star_blocks(s.moving_price, bottom, DEC.multiply(spot, stress_keep), ema_alpha(glob, s, raw.block))
            t_star_map[s.key] = t_star
            rec = self._gk.record(s.key)
            b_theta = DEC.divide(share.b, theta) if emit_eligible(s) and theta > 0 else None
            flags = launch_flags(s, glob, block, rec, p_reg_24h=p24, b_over_theta=b_theta, params=p.gatekeeper)
            flags_map[s.key] = flags
            validator_ok[s.key] = rr.best is not None
            bans[s.key] = uni.emission_ban_until(g.last_disable, g.last_enable, p.universe)
            beta = beta_quantiles(beta_samples(g.dense, block, h, own))
            ss = since_start(s, block)
            imm_end = immunity_end(s, glob)
            feats[s.key] = Feat(
                key=s.key,
                spot=finite(float(spot), "spot"),
                pool_tao=int(s.pool.tao) / _RAO,
                k_w=PERQUINTILL / s.pool.w_base_e18 if s.pool.w_base_e18 > 0 else 0.0,
                ret_1h=ret[RET_1H], ret_1d=ret[RET_1D], ret_7d=ret[RET_7D],
                sigma_d=g.sigma_d(block, pbar),
                fast_ema_gap=_gap(ln_cap, None if fast is None else Decimal(fast)),
                ema_gap=_gap(ln_cap, s.moving_price),
                flow_1h=g.flow(s, block, RET_1H), flow_1d=flow_1d, flow_7d=g.flow(s, block, RET_7D), flow_z_1d=flow_z,
                emis_tao_day=int(share.tao_per_block) * BLOCKS_PER_DAY / _RAO,
                chain_buy_day=int(share.chain_buy_per_block) * BLOCKS_PER_DAY / _RAO,
                obs_emis_tao_day=obs_day / _RAO,
                gate_keep=float(share.keep),
                burn_adj_rank=burn_rank.get(s.key),
                ema_rank_desc=ema_rank.get(s.key),
                rp=float(s.root_prop),
                sell_push_day=finite(float(sl.sell_push_day), "sell_push_day"),
                cb_push_day=finite(float(sl.cb_push_day), "cb_push_day"),
                escrow_frac=(int(s.escrow_alpha) / int(s.pool.alpha)
                             if s.escrow_alpha is not None and s.pool.alpha > 0 else None),
                a_earn_alpha=ae / _RAO,
                yield_cf_gross_day=float(closed_form_yield_gross(s, glob)),
                router_candidates=rr.candidates,
                best_candidate=rr.best,
                yield_net_day=rr.yield_net_day,
                a_earn_growth_day=float(growth),
                age_reg_blocks=block - int(s.key.reg_at),
                since_start_blocks=ss,
                immune=block < imm_end,
                immune_until=imm_end,
                prune_rank=rank,
                rho=float(DEC.divide(s.moving_price, bottom)) if bottom is not None and bottom > 0 else None,
                t_star_stress_blocks=None if t_star is None else float(t_star),
                launch_flags=flags,
                beta_entry_ppm=to_ppm(beta.q95) if beta.q95 is not None else BETA_UNKNOWN_PPM,
                beta_exit_ppm=to_ppm(beta.q99) if beta.q99 is not None else BETA_UNKNOWN_PPM,
                owner_sold_6h_frac=g.owner_sold_frac(s),
                owner_liquid_frac=_owner_liquid(s),
                top_holder_frac=_top_holder(s),
            )

        # ---- emission view
        errs = parity_rel_errors(parity_pairs)
        big = [abs(m - o) for m, o in parity_pairs if o > RAO_PER_TAO]
        emission_view = EmissionView(theta=float(theta), gate_rank=glob.gate_rank, sum_ema=float(sum_e),
                                     root_flag=root_flag, parity_err_max_tao_day=max(big) / _RAO if big else 0.0,
                                     model_ok=parity_ok(errs))

        # ---- universe A-G
        rows = uni.evaluate(uni.UniverseInputs(
            snap=raw, prune_rank=ranks, target=target, bottom_ema=bottom, t_star=t_star_map, flags=flags_map,
            validator_ok=validator_ok, emission_ban_until=bans, cost_ratio=r_cost), p.universe)
        self._last_universe = rows

        self.warm = block - self._first >= p.warm_blocks
        frame = FeatureFrame(block=raw.block, warm=self.warm, feats=MappingProxyType(feats), prune=prune_view,
                             emission=emission_view, regime_id=_regime_id(raw.block), universe_eligible=uni.eligible_count(rows),
                             beta_horizon_blocks=h, digest="")
        frame = replace(frame, digest=frame_digest(frame))
        self._prev = raw
        self._last_frame = frame
        return frame


# ------------------------------------------------------------------------------------------------- helpers
def _theta(snap: ChainSnapshot, shares: Mapping[SubnetKey, EmissionShare], refresh: bool) -> Decimal:
    """The gate bar emission_vector used for the next block (stored bar, or the rank-mode refresh)."""
    nxt = int(snap.block) + 1
    glob = snap.glob
    if nxt >= regime("gate_rank32").first_block:
        th = glob.gate_bar
        if refresh or th <= 0:
            th = gate_theta([sh.b for _, sh in sorted(shares.items())], glob.gate_rank)
        return th
    if nxt >= regime("gate_qmass").first_block:
        return glob.gate_bar
    return Decimal(0)


def _regime_id(block: Block) -> str:
    try:
        return regime_at(block).regime_id
    except ValueError:
        return "pre_dtao"


def _owner_liquid(s: SubnetState) -> float | None:
    if s.owner_alpha is None or s.pool.alpha <= 0:
        return None
    liquid = 0 if s.owner_cut_autolock else 1
    return int(s.owner_alpha) * liquid / int(s.pool.alpha)


def _top_holder(s: SubnetState) -> float | None:
    if s.owner_alpha is None:
        return None
    denom = int(s.alpha_out) - int(s.protocol_alpha)
    if denom <= 0:
        return None
    top = sorted((int(h.total_alpha) for h in s.hotkeys if h.hotkey != s.owner_hotkey), reverse=True)[:TOP_HOLDERS]
    return (int(s.owner_alpha) + sum(top)) / denom


def best_candidates(frame: FeatureFrame) -> dict[SubnetKey, Hotkey | None]:
    """best_candidate per generation of a frame (convenience for reports and WP8 tests)."""
    return {k: f.best_candidate for k, f in sorted(frame.feats.items())}
