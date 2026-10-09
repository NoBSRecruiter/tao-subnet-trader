"""taotrader/backtest/stats.py - inference for backtest series (WP10; DESIGN.md sections 8.9, 10.4).

- `newey_west_t(x, lag=5)`: t statistic of the mean with a Bartlett-kernel HAC variance (lag 5 by default).
- `effective_n(x, lag)`: n / (1 + 2 sum_k (1 - k/(lag+1)) rho_k), floored at 1.
- `stationary_bootstrap_ci(x, mean_block=7, ...)`: Politis-Romano stationary block bootstrap (geometric block lengths
  with mean `mean_block`, circular wrap) percentile CI of the mean. Seeded deterministically from (seed, label) with
  blake2b, so reports are reproducible (section 4.6).
- `sharpe(x)`, `deflated_sharpe(...)`: Bailey & Lopez de Prado deflated Sharpe ratio - the probability that the true
  Sharpe exceeds the expected maximum of `n_trials` independent null Sharpes, with skew/kurtosis correction.
- `cscv_pbo(matrix, n_splits=16)`: combinatorially symmetric cross-validation probability of backtest overfitting
  (Bailey, Borwein, Lopez de Prado, Zhu): over all C(S, S/2) splits, the share in which the in-sample best
  configuration ranks below the out-of-sample median (logit <= 0).
- `spearman(x, y)` (rank IC), `brier(p, y)` / `brier_skill(p, y, p_ref)`, `wilson_ci(k, n)`, `quantile(x, q)`.

Pure numpy / math; no clock; deterministic.
"""
from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

__all__ = [
    "BootstrapCI", "SeriesStats", "brier", "brier_skill", "cscv_pbo", "deflated_sharpe", "effective_n", "expected_max_sharpe",
    "newey_west_t", "norm_cdf", "quantile", "series_stats", "sharpe", "spearman", "stationary_bootstrap_ci", "wilson_ci",
]

EULER_GAMMA: Final[float] = 0.5772156649015329


Arr = npt.NDArray[np.float64]


def _arr(x: Sequence[float] | Arr) -> Arr:
    return np.asarray(list(x), dtype=np.float64)


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02,
         -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01,
         -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00,
         4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ------------------------------------------------------------------------------------------------ HAC and effective n
def _autocov(x: Arr, k: int) -> float:
    n = len(x)
    if k >= n:
        return 0.0
    d = x - x.mean()
    return float(np.dot(d[: n - k], d[k:]) / n)


def newey_west_t(x: Sequence[float], lag: int = 5) -> float | None:
    """t = mean / sqrt(HAC variance / n) with Bartlett weights 1 - k/(lag+1); None for fewer than 3 points or a zero
    variance."""
    a = _arr(x)
    n = len(a)
    if n < 3:
        return None
    lag = max(0, min(lag, n - 1))
    s = _autocov(a, 0)
    for k in range(1, lag + 1):
        s += 2.0 * (1.0 - k / (lag + 1)) * _autocov(a, k)
    if s <= 0:
        return None
    return float(a.mean() / math.sqrt(s / n))


def effective_n(x: Sequence[float], lag: int = 5) -> float:
    a = _arr(x)
    n = len(a)
    if n < 3:
        return float(n)
    g0 = _autocov(a, 0)
    if g0 <= 0:
        return float(n)
    s = 1.0
    for k in range(1, min(lag, n - 1) + 1):
        s += 2.0 * (1.0 - k / (lag + 1)) * _autocov(a, k) / g0
    return max(1.0, n / s) if s > 0 else float(n)


# ------------------------------------------------------------------------------------------------ bootstrap
@dataclass(frozen=True, slots=True)
class BootstrapCI:
    mean: float
    lo: float
    hi: float
    n_boot: int
    mean_block: float


def _seed(seed: int, label: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{seed}|{label}".encode(), digest_size=8).digest(), "little")


def stationary_bootstrap_ci(x: Sequence[float], *, mean_block: float = 7.0, n_boot: int = 2_000, alpha: float = 0.05,
                            seed: int = 0, label: str = "") -> BootstrapCI | None:
    """Percentile CI of the mean under the stationary block bootstrap (Politis & Romano 1994)."""
    a = _arr(x)
    n = len(a)
    if n < 2:
        return None
    rng = np.random.default_rng(_seed(seed, label))
    p = 1.0 / max(mean_block, 1.0)
    means = np.empty(n_boot, dtype=np.float64)
    for i in range(n_boot):
        idx = np.empty(n, dtype=np.int64)
        j = int(rng.integers(n))
        for t in range(n):
            if t > 0:
                j = int(rng.integers(n)) if rng.random() < p else (j + 1) % n
            idx[t] = j
        means[i] = a[idx].mean()
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return BootstrapCI(float(a.mean()), float(lo), float(hi), n_boot, mean_block)


# ------------------------------------------------------------------------------------------------ Sharpe
def sharpe(x: Sequence[float] | Arr) -> float | None:
    a = _arr(x)
    if len(a) < 2:
        return None
    sd = float(a.std(ddof=1))
    return None if sd <= 0 else float(a.mean() / sd)


def _moments(a: Arr) -> tuple[float, float]:
    d = a - a.mean()
    s2 = float((d ** 2).mean())
    if s2 <= 0:
        return 0.0, 3.0
    return float((d ** 3).mean() / s2 ** 1.5), float((d ** 4).mean() / s2 ** 2)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max of n_trials null Sharpes] (Bailey & Lopez de Prado), per-period units."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    n = float(n_trials)
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * norm_ppf(1 - 1 / n) + EULER_GAMMA * norm_ppf(1 - 1 / (n * math.e)))


def deflated_sharpe(x: Sequence[float], n_trials: int, var_sr_trials: float | None = None) -> float | None:
    """P(true SR > SR_0), SR_0 = expected max Sharpe of n_trials nulls (variance of the trial Sharpes; defaults to the
    sampling variance 1/(n-1)). None for fewer than 3 observations."""
    a = _arr(x)
    n = len(a)
    sr = sharpe(a)
    if sr is None or n < 3:
        return None
    skew, kurt = _moments(a)
    var = var_sr_trials if var_sr_trials is not None else 1.0 / (n - 1)
    sr0 = expected_max_sharpe(max(n_trials, 1), var)
    den = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if den <= 0:
        return None
    return norm_cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den))


# ------------------------------------------------------------------------------------------------ CSCV / PBO
def cscv_pbo(matrix: Sequence[Sequence[float]], n_splits: int = 16) -> float | None:
    """Probability of backtest overfitting. `matrix` is T x N (rows = periods, columns = configurations); rows are cut
    into n_splits contiguous blocks; for every half/half combination the IS-best column's OOS relative rank w gives
    logit(w / (1 - w)); PBO = share of logits <= 0. None if T < n_splits or N < 2."""
    m = np.asarray([list(r) for r in matrix], dtype=np.float64)
    if m.ndim != 2:
        return None
    t, n = m.shape
    s = n_splits - (n_splits % 2)
    if n < 2 or s < 2 or t < s:
        return None
    blocks = np.array_split(np.arange(t), s)
    logits: list[float] = []
    for combo in itertools.combinations(range(s), s // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(s) if i not in combo])
        is_perf = m[is_idx].mean(axis=0)
        oos_perf = m[oos_idx].mean(axis=0)
        best = int(np.argmax(is_perf))
        rank = float((oos_perf < oos_perf[best]).sum() + 0.5 * ((oos_perf == oos_perf[best]).sum() - 1) + 1)
        w = rank / (n + 1)
        logits.append(math.log(w / (1 - w)))
    return float(sum(1 for v in logits if v <= 0) / len(logits))


# ------------------------------------------------------------------------------------------------ misc
def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties); None for < 3 pairs or a constant input."""
    a, b = _arr(x), _arr(y)
    if len(a) != len(b) or len(a) < 3:
        return None

    def ranks(v: Arr) -> Arr:
        order = np.argsort(v, kind="mergesort")
        r = np.empty(len(v), dtype=np.float64)
        sv = v[order]
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and sv[j + 1] == sv[i]:
                j += 1
            r[order[i:j + 1]] = (i + j) / 2.0
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def brier(p: Sequence[float], y: Sequence[int]) -> float:
    a, b = _arr(p), _arr([float(v) for v in y])
    return float(((a - b) ** 2).mean()) if len(a) else 0.0


def brier_skill(p: Sequence[float], y: Sequence[int], p_ref: Sequence[float]) -> float | None:
    ref = brier(p_ref, y)
    return None if ref <= 0 else 1.0 - brier(p, y) / ref


def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    z = norm_ppf(1 - alpha / 2)
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def quantile(x: Sequence[float], q: float) -> float | None:
    a = _arr(x)
    return None if len(a) == 0 else float(np.quantile(a, q))


@dataclass(frozen=True, slots=True)
class SeriesStats:
    n: int
    mean: float
    nw_t: float | None
    n_eff: float
    ci: BootstrapCI | None
    sharpe: float | None
    deflated_sharpe: float | None


def series_stats(x: Sequence[float], *, n_trials: int = 1, seed: int = 0, label: str = "", n_boot: int = 2_000,
                 mean_block: float = 7.0) -> SeriesStats:
    a = list(x)
    return SeriesStats(n=len(a), mean=float(np.mean(a)) if a else 0.0, nw_t=newey_west_t(a, 5), n_eff=effective_n(a, 5),
                       ci=stationary_bootstrap_ci(a, mean_block=mean_block, n_boot=n_boot, seed=seed, label=label),
                       sharpe=sharpe(a), deflated_sharpe=deflated_sharpe(a, n_trials))
