"""WP10 backtest.stats: NW t, effective n, stationary bootstrap, deflated Sharpe, CSCV PBO, rank IC, Brier."""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from taotrader.backtest import stats as st


def test_newey_west_t_lag0_equals_the_plain_t() -> None:
    x = [0.01, -0.02, 0.03, 0.005, 0.0, 0.012, -0.004]
    n = len(x)
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / n                     # HAC uses the 1/n autocovariance
    assert st.newey_west_t(x, 0) == pytest.approx(m / math.sqrt(var / n))


def test_newey_west_t_hand_computed_lag2() -> None:
    x = [1.0, 2.0, 0.0, 3.0, 1.0, 2.0]
    a = np.asarray(x)
    d = a - a.mean()
    g = [float(np.dot(d[: 6 - k], d[k:]) / 6) for k in range(3)]
    s = g[0] + 2 * (1 - 1 / 3) * g[1] + 2 * (1 - 2 / 3) * g[2]
    assert st.newey_west_t(x, 2) == pytest.approx(a.mean() / math.sqrt(s / 6))


def test_newey_west_degenerate_inputs() -> None:
    assert st.newey_west_t([1.0, 2.0], 5) is None
    assert st.newey_west_t([1.0, 1.0, 1.0, 1.0], 5) is None


def test_effective_n_shrinks_with_positive_autocorrelation() -> None:
    rng = random.Random(1)
    iid = [rng.gauss(0, 1) for _ in range(400)]
    ar = [0.0]
    for _ in range(399):
        ar.append(0.8 * ar[-1] + rng.gauss(0, 1))
    assert st.effective_n(iid, 5) > 300
    assert st.effective_n(ar, 5) < 150


def test_stationary_bootstrap_is_seeded_and_covers_the_mean() -> None:
    rng = random.Random(7)
    x = [0.001 + rng.gauss(0, 0.01) for _ in range(120)]
    a = st.stationary_bootstrap_ci(x, mean_block=7, n_boot=400, seed=3, label="x")
    b = st.stationary_bootstrap_ci(x, mean_block=7, n_boot=400, seed=3, label="x")
    c = st.stationary_bootstrap_ci(x, mean_block=7, n_boot=400, seed=4, label="x")
    assert a is not None and b is not None and c is not None
    assert (a.lo, a.hi) == (b.lo, b.hi)                         # deterministic for (seed, label)
    assert (a.lo, a.hi) != (c.lo, c.hi)
    assert a.lo < a.mean < a.hi
    assert st.stationary_bootstrap_ci([1.0], n_boot=10) is None


def test_bootstrap_ci_is_wider_for_autocorrelated_series() -> None:
    rng = random.Random(11)
    iid = [rng.gauss(0, 1) for _ in range(200)]
    ar = [0.0]
    for _ in range(199):
        ar.append(0.9 * ar[-1] + rng.gauss(0, math.sqrt(1 - 0.81)))
    w_iid = st.stationary_bootstrap_ci(iid, mean_block=7, n_boot=300)
    w_ar = st.stationary_bootstrap_ci(ar, mean_block=7, n_boot=300)
    assert w_iid is not None and w_ar is not None
    assert (w_ar.hi - w_ar.lo) > (w_iid.hi - w_iid.lo)


def test_deflated_sharpe_falls_with_the_number_of_trials() -> None:
    rng = random.Random(5)
    x = [0.002 + rng.gauss(0, 0.01) for _ in range(250)]
    one = st.deflated_sharpe(x, 1)
    many = st.deflated_sharpe(x, 1_000)
    assert one is not None and many is not None
    assert 0 <= many < one <= 1
    assert st.expected_max_sharpe(1, 0.01) == 0.0
    assert st.expected_max_sharpe(100, 0.01) > st.expected_max_sharpe(10, 0.01) > 0


def test_norm_ppf_inverts_the_cdf() -> None:
    for p in (0.001, 0.025, 0.3, 0.5, 0.8, 0.975, 0.999):
        assert st.norm_cdf(st.norm_ppf(p)) == pytest.approx(p, abs=1e-8)


def test_cscv_pbo_separates_noise_from_a_real_edge() -> None:
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 1, size=(160, 20))                      # 20 configurations, no edge: PBO ~ 0.5
    pbo_noise = st.cscv_pbo(noise.tolist(), n_splits=8)
    edge = noise.copy()
    edge[:, 3] += 0.8                                             # one configuration truly better
    pbo_edge = st.cscv_pbo(edge.tolist(), n_splits=8)
    assert pbo_noise is not None and pbo_edge is not None
    assert 0.2 < pbo_noise < 0.8
    assert pbo_edge < 0.05
    assert st.cscv_pbo([[1.0, 2.0]], n_splits=8) is None


def test_spearman_ties_and_degenerate() -> None:
    assert st.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert st.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert st.spearman([1, 1, 2, 2], [1, 2, 3, 4]) == pytest.approx(0.894427, rel=1e-5)
    assert st.spearman([1, 1, 1], [1, 2, 3]) is None
    assert st.spearman([1, 2], [1, 2]) is None


def test_brier_skill_and_wilson() -> None:
    y = [1, 0, 1, 1, 0]
    assert st.brier([1, 0, 1, 1, 0], y) == 0.0
    assert st.brier_skill([0.9, 0.1, 0.8, 0.7, 0.2], y, [0.6] * 5) == pytest.approx(1 - 0.038 / 0.24)
    lo, hi = st.wilson_ci(25, 26)
    assert lo < 25 / 26 < hi <= 1.0
    assert st.wilson_ci(0, 0) == (0.0, 1.0)


def test_series_stats_bundle() -> None:
    s = st.series_stats([0.01, 0.02, -0.005, 0.0, 0.013, 0.004], n_trials=3, n_boot=200, label="b")
    assert s.n == 6 and s.ci is not None and s.sharpe is not None and s.nw_t is not None
