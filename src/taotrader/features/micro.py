"""taotrader/features/micro.py - price-path microstructure helpers (WP5; DESIGN.md sections 2.2, 2.3, 3.12, 5.8).

Pure and deterministic. Floats are allowed here (feature math, section 5.8); every transcendental function that feeds
a published feature is computed in Decimal and rounded once to float, so feature values are bit-identical across
operating systems (section 4.6).

Building blocks used by features.engine:

- `GridBuffer`: the FIRST observation in each absolute `bucket`-block cell ([k*bucket, (k+1)*bucket)), oldest first.
  A stride-60 replay keeps every snapshot; a per-block stream keeps one per cell. Lookups `at(x, max_stale)` return
  the latest kept point with x - max_stale <= block <= x, so the "price at t - 60" of a stride replay is exact.
- `median3`: the median-of-3 60-block price pbar_t = median(p_t, p_{t-60}, p_{t-120}) (section 2.2), in log space.
- `DenseBuffer` + `beta_quantiles`: the section 3.12 beta sample |ln p_s - ln p_{s-h}| for s in (t - 1,800, t] with
  p_{s-h} observed exactly. A sample is EXCLUDED when an own-fill block f lies in its return window (s - h, s]
  (a fill at f moves every return that spans it, so a book never widens its limits from its own impact; this
  covers "blocks with own fills are excluded" and the returns that straddle them). Quantiles are nearest-rank:
  q_p = x_(ceil(p*n)) of the ascending sample (q99 of 30 stride points is the maximum).
- `FastEma`: the local 600-block-half-life EMA of spot (section 2.3 spike guard), made window-deterministic: two
  copies restart at absolute anchors k*R and k*R + R/2 (R = 24,000 blocks = 40 half-lives) and the older copy is
  published, so its value depends only on the last R blocks (initial-condition weight <= 2**-20).
- `robust_z`: (x - median) / (1.4826 * MAD) over a trailing history (section 2.1 flow_z_1d).
- `ewma_recent_first`: the truncated EWMA of section 3.8 (half-life in observations, at most K observations,
  weights renormalised over the observations available).
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal
from functools import lru_cache
from statistics import median
from typing import Final

# ------------------------------------------------------------------------------------------------- constants
PRICE_GRID_BLOCKS: Final[int] = 60           # the 60-block price grid of the median-of-3 price (section 2.2)
MEDIAN_OFFSETS: Final[tuple[int, int, int]] = (0, 60, 120)
BETA_WINDOW_BLOCKS: Final[int] = 1_800       # section 3.12: 1,800 blocks (30 stride points)
BETA_MIN_SAMPLES: Final[int] = 10            # fewer samples -> beta unknown (published as the widest value)
BETA_Q_ENTRY: Final[tuple[int, int]] = (95, 100)
BETA_Q_EXIT: Final[tuple[int, int]] = (99, 100)
FAST_EMA_HALF_LIFE: Final[int] = 600         # section 2.3: local 600-block-half-life EMA of spot
FAST_EMA_ANCHOR: Final[int] = 24_000         # restart period R of the window-deterministic EMA (40 half-lives)
MAD_SCALE: Final[float] = 1.4826             # MAD -> SD of a normal (section 2.1 "median/1.4826*MAD")

_LN_CTX: Final[Context] = Context(prec=20)   # ln rounded to 20 digits, then once to float: identical on every OS
_POW_CTX: Final[Context] = Context(prec=30)


def ln(x: Decimal) -> float | None:
    """Natural log of a positive Decimal as a float (Decimal-exact, then one rounding); None for x <= 0."""
    if not x > 0:
        return None
    return float(_LN_CTX.ln(x))


@lru_cache(maxsize=4_096)
def ema_gain(dt_blocks: int, half_life: int) -> float:
    """1 - 2**(-dt/half_life): the EMA weight of a new sample dt blocks after the previous one (Decimal-exact)."""
    if dt_blocks <= 0:
        return 0.0
    keep = _POW_CTX.power(Decimal(2), _POW_CTX.divide(Decimal(-dt_blocks), Decimal(half_life)))
    return float(_POW_CTX.subtract(Decimal(1), keep))


@lru_cache(maxsize=64)
def ewma_weights(half_life: int, k: int) -> tuple[float, ...]:
    """w_j = 2**(-j/half_life) for j = 0 .. k-1 (j = 0 is the most recent observation), Decimal-exact."""
    return tuple(float(_POW_CTX.power(Decimal(2), _POW_CTX.divide(Decimal(-j), Decimal(half_life)))) for j in range(k))


def ewma_recent_first(values: Sequence[float], half_life: int, k: int) -> float | None:
    """Truncated EWMA of `values` (most recent first): sum w_j x_j / sum w_j over the first min(len, k) values."""
    n = min(len(values), k)
    if n == 0:
        return None
    w = ewma_weights(half_life, k)
    num = 0.0
    den = 0.0
    for j in range(n):
        num += w[j] * values[j]
        den += w[j]
    return num / den


def median3(a: float | None, b: float | None, c: float | None) -> float | None:
    """Median of three values; None if any is missing."""
    if a is None or b is None or c is None:
        return None
    return sorted((a, b, c))[1]


def nearest_rank(sorted_values: Sequence[float], num: int, den: int) -> float:
    """Nearest-rank quantile num/den of an ascending, non-empty sample: x_(ceil(n*num/den)) (1-based)."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("empty sample")
    k = max(1, -(-n * num // den))
    return sorted_values[min(k, n) - 1]


def robust_z(x: float, history: Sequence[float], min_samples: int) -> float | None:
    """(x - median(H)) / (1.4826 * MAD(H)); None with fewer than min_samples values or a zero MAD."""
    if len(history) < max(min_samples, 1):
        return None
    med = median(history)
    mad = median([abs(h - med) for h in history])
    if not mad > 0:
        return None
    return (x - med) / (MAD_SCALE * mad)


# ------------------------------------------------------------------------------------------------- grid buffer
@dataclass(frozen=True, slots=True)
class GridPoint:
    block: int
    ln_p: float | None              # ln(spot) of the era-correct pool; None when the pool has no price
    flow_cum: int | None            # SubnetTaoFlow running total at this block (None where the item is absent)


class GridBuffer:
    """First observation per absolute `bucket`-block cell, kept for `retention` blocks (oldest first)."""

    __slots__ = ("_blocks", "_bucket", "_last_seen", "_points", "_retention", "_start")

    def __init__(self, bucket: int, retention: int) -> None:
        if bucket <= 0 or retention <= 0:
            raise ValueError("bucket and retention must be positive")
        self._bucket = bucket
        self._retention = retention
        self._blocks: list[int] = []
        self._points: list[GridPoint] = []
        self._start = 0
        self._last_seen: int | None = None

    def add(self, p: GridPoint) -> bool:
        """Keep p if it is the first observation of its cell; returns True if kept."""
        if self._last_seen is not None and p.block <= self._last_seen:
            raise ValueError(f"grid points must be strictly increasing ({p.block} after {self._last_seen})")
        self._last_seen = p.block
        if self._start < len(self._blocks) and p.block // self._bucket == self._blocks[-1] // self._bucket:
            return False
        self._blocks.append(p.block)
        self._points.append(p)
        return True

    def evict(self, now: int) -> None:
        """Drop points older than now - retention."""
        cut = bisect_left(self._blocks, now - self._retention, self._start)
        self._start = cut
        if self._start > 256 and self._start * 2 > len(self._blocks):
            del self._blocks[:self._start]
            del self._points[:self._start]
            self._start = 0

    def at(self, x: int, max_stale: int) -> GridPoint | None:
        """Latest kept point with x - max_stale <= block <= x."""
        i = bisect_right(self._blocks, x, self._start)
        if i <= self._start:
            return None
        p = self._points[i - 1]
        return p if p.block >= x - max_stale else None

    def points(self) -> tuple[GridPoint, ...]:
        return tuple(self._points[self._start:])

    def __len__(self) -> int:
        return len(self._blocks) - self._start


# ------------------------------------------------------------------------------------------------- dense buffer
class DenseBuffer:
    """Every observation (block -> ln p) within `retention` blocks, for the beta sample."""

    __slots__ = ("_blocks", "_ln", "_retention", "_start")

    def __init__(self, retention: int) -> None:
        if retention <= 0:
            raise ValueError("retention must be positive")
        self._retention = retention
        self._blocks: list[int] = []
        self._ln: dict[int, float | None] = {}
        self._start = 0

    def add(self, block: int, ln_p: float | None) -> None:
        if self._start < len(self._blocks) and block <= self._blocks[-1]:
            raise ValueError(f"dense points must be strictly increasing ({block} after {self._blocks[-1]})")
        self._blocks.append(block)
        self._ln[block] = ln_p

    def evict(self, now: int) -> None:
        cut = bisect_left(self._blocks, now - self._retention, self._start)
        for b in self._blocks[self._start:cut]:
            del self._ln[b]
        self._start = cut
        if self._start > 256 and self._start * 2 > len(self._blocks):
            del self._blocks[:self._start]
            self._start = 0

    def get(self, block: int) -> float | None:
        return self._ln.get(block)

    def since(self, after: int) -> Iterator[tuple[int, float | None]]:
        """(block, ln p) for every kept block > after, ascending."""
        i = bisect_right(self._blocks, after, self._start)
        for b in self._blocks[i:]:
            yield b, self._ln[b]

    def items(self) -> tuple[tuple[int, float | None], ...]:
        return tuple((b, self._ln[b]) for b in self._blocks[self._start:])

    def __len__(self) -> int:
        return len(self._blocks) - self._start


@dataclass(frozen=True, slots=True)
class BetaStats:
    q95: float | None               # None: fewer than BETA_MIN_SAMPLES usable samples
    q99: float | None
    n: int


def _own_fill_in(own_sorted: Sequence[int], lo_exclusive: int, hi_inclusive: int) -> bool:
    i = bisect_right(own_sorted, hi_inclusive)
    return i > 0 and own_sorted[i - 1] > lo_exclusive


def beta_samples(dense: DenseBuffer, now: int, h: int, own_sorted: Sequence[int],
                 window: int = BETA_WINDOW_BLOCKS) -> list[float]:
    """|ln p_s - ln p_{s-h}| for every observed s in (now - window, now] with p_{s-h} observed, skipping samples
    whose return window (s - h, s] contains an own-fill block (own_sorted ascending)."""
    out: list[float] = []
    for s, lp in dense.since(now - window):
        if s > now:
            break
        if lp is None:
            continue
        prev = dense.get(s - h)
        if prev is None:
            continue
        if own_sorted and _own_fill_in(own_sorted, s - h, s):
            continue
        out.append(abs(lp - prev))
    return out


def beta_quantiles(samples: Sequence[float], min_samples: int = BETA_MIN_SAMPLES) -> BetaStats:
    """Nearest-rank q95 / q99 of the beta sample."""
    if len(samples) < min_samples:
        return BetaStats(q95=None, q99=None, n=len(samples))
    xs = sorted(samples)
    return BetaStats(q95=nearest_rank(xs, *BETA_Q_ENTRY), q99=nearest_rank(xs, *BETA_Q_EXIT), n=len(xs))


def beta_horizon(gap_blocks: int | None, latency_horizon: int) -> int:
    """h of section 3.12: finality_lag + latency_blocks when snapshots are per-block, else the stride (the gap to the
    previous snapshot). Generalised to h = max(gap, finality_lag + latency): a short feed skip in a per-block stream
    keeps the full decision-to-fill horizon instead of shrinking it."""
    if gap_blocks is None:
        return latency_horizon
    return max(gap_blocks, latency_horizon)


# ------------------------------------------------------------------------------------------------- fast EMA
class FastEma:
    """Window-deterministic local EMA of a series (half-life `half_life` blocks).

    Two copies restart at absolute anchors k*R and k*R + R/2; `value(block)` returns the copy whose anchor is older,
    so the published value depends only on observations of the last R blocks."""

    __slots__ = ("_anchor", "_half_life", "_last", "_v")

    def __init__(self, half_life: int = FAST_EMA_HALF_LIFE, anchor: int = FAST_EMA_ANCHOR) -> None:
        if half_life <= 0 or anchor <= 0 or anchor % 2:
            raise ValueError("half_life > 0 and an even anchor period are required")
        self._half_life = half_life
        self._anchor = anchor
        self._last: int | None = None
        self._v: list[float] = [0.0, 0.0]

    def _cell(self, phase: int, block: int) -> int:
        return (block - phase * (self._anchor // 2)) // self._anchor

    def update(self, block: int, x: float) -> None:
        last = self._last
        if last is not None and block <= last:
            raise ValueError(f"EMA updates must be strictly increasing ({block} after {last})")
        for phase in (0, 1):
            if last is None or self._cell(phase, block) != self._cell(phase, last):
                self._v[phase] = x
            else:
                self._v[phase] += ema_gain(block - last, self._half_life) * (x - self._v[phase])
        self._last = block

    def value(self) -> float | None:
        """The EMA at the last update block (the copy restarted longer ago)."""
        if self._last is None:
            return None
        b = self._last
        half = self._anchor // 2
        age0 = b - self._cell(0, b) * self._anchor
        age1 = (b - half) - self._cell(1, b) * self._anchor
        return self._v[0] if age0 >= age1 else self._v[1]

    def state(self) -> tuple[int | None, float, float]:
        return (self._last, self._v[0], self._v[1])


def finite(x: float, what: str) -> float:
    """Guard: published float features must be finite (the codec rejects NaN/inf; fail loudly here instead)."""
    if not math.isfinite(x):
        raise ValueError(f"non-finite feature {what}: {x!r}")
    return x
