"""features.micro: median-of-3 grid price, nearest-rank beta quantiles (h = 5 per block, h = stride on stride data,
own-fill exclusion), window-deterministic fast EMA, robust z, truncated EWMA, Decimal-exact logs."""
from __future__ import annotations

import math
from decimal import Context, Decimal

import pytest

from taotrader.features.micro import (
    BETA_WINDOW_BLOCKS,
    DenseBuffer,
    FastEma,
    GridBuffer,
    GridPoint,
    beta_horizon,
    beta_quantiles,
    beta_samples,
    ema_gain,
    ewma_recent_first,
    ewma_weights,
    finite,
    ln,
    median3,
    nearest_rank,
    robust_z,
)


# ------------------------------------------------------------------------------------------------- basics
def test_ln_is_decimal_exact_and_rejects_non_positive() -> None:
    x = Decimal("0.0013482123456789")
    assert ln(x) == float(Context(prec=20).ln(x))
    assert ln(Decimal(0)) is None and ln(Decimal(-1)) is None
    assert abs(ln(Decimal(2)) - math.log(2)) < 1e-15                      # type: ignore[operator]


def test_median3_and_nearest_rank() -> None:
    assert median3(3.0, 1.0, 2.0) == 2.0
    assert median3(1.0, None, 2.0) is None
    xs = [float(i) for i in range(1, 31)]                                  # 30 stride points
    assert nearest_rank(xs, 95, 100) == 29.0 and nearest_rank(xs, 99, 100) == 30.0
    ys = [float(i) for i in range(1, 1801)]                                # 1,800 per-block points
    assert nearest_rank(ys, 95, 100) == 1710.0 and nearest_rank(ys, 99, 100) == 1782.0
    assert nearest_rank([5.0], 95, 100) == 5.0
    with pytest.raises(ValueError):
        nearest_rank([], 95, 100)


def test_robust_z() -> None:
    hist = [0.0, 1.0, 2.0, 3.0, 4.0]                                       # median 2, MAD 1
    assert robust_z(5.0, hist, 5) == pytest.approx(3.0 / 1.4826)
    assert robust_z(5.0, hist, 6) is None                                  # not enough history
    assert robust_z(1.0, [1.0] * 10, 5) is None                            # MAD 0


def test_ewma_truncated_weights() -> None:
    w = ewma_weights(20, 40)
    assert len(w) == 40 and w[0] == 1.0 and w[20] == pytest.approx(0.5)
    assert ewma_recent_first([0.01] * 50, 20, 40) == pytest.approx(0.01)
    assert ewma_recent_first([], 20, 40) is None
    v = [1.0, 0.0]
    assert ewma_recent_first(v, 20, 40) == pytest.approx(1.0 / (1.0 + w[1]))
    assert ewma_recent_first([1.0] + [0.0] * 39 + [100.0], 20, 40) == ewma_recent_first([1.0] + [0.0] * 39, 20, 40)


def test_finite_guard() -> None:
    assert finite(1.5, "x") == 1.5
    with pytest.raises(ValueError):
        finite(float("inf"), "x")


# ------------------------------------------------------------------------------------------------- grid
def test_grid_keeps_first_point_per_cell_and_bounds_staleness() -> None:
    g = GridBuffer(60, 10_000)
    for b in range(1_000, 1_200):                                          # per-block stream
        g.add(GridPoint(b, float(b), b))
    kept = [p.block for p in g.points()]
    assert kept == [1_000, 1_020, 1_080, 1_140]                            # 1_000 // 60 = 16 -> next cell starts at 1_020
    assert g.at(1_100, 60).block == 1_080                                  # type: ignore[union-attr]
    assert g.at(1_019, 60).block == 1_000                                  # type: ignore[union-attr]
    assert g.at(999, 60) is None
    assert g.at(1_079, 10) is None                                         # latest <= 1_079 is 1_020: too stale
    with pytest.raises(ValueError):
        g.add(GridPoint(1_150, 0.0, None))
    g.evict(1_000 + 10_000 + 25)
    assert [p.block for p in g.points()] == [1_080, 1_140]


def test_grid_keeps_every_stride_point() -> None:
    g = GridBuffer(60, 10_000)
    for b in range(1_014, 3_000, 60):
        assert g.add(GridPoint(b, 0.0, None))
    assert len(g) == len(range(1_014, 3_000, 60))
    assert g.at(2_934 - 60, 60).block == 2_874                             # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------- beta
def _brute_beta(lnp: dict[int, float], now: int, h: int, own: list[int]) -> list[float]:
    out = []
    for s in range(now - BETA_WINDOW_BLOCKS + 1, now + 1):
        if s in lnp and (s - h) in lnp and not any(s - h < f <= s for f in own):
            out.append(abs(lnp[s] - lnp[s - h]))
    return out


def test_beta_stride_points_hand_computed() -> None:
    """30 stride points with |d ln p| = 0.001 .. 0.030: q95 = 29th = 0.029, q99 = 30th = 0.030 (nearest rank).
    An own fill at the block of the 0.030 move removes that sample: q95 = 28th of 29 = 0.028, q99 = 29th = 0.029."""
    d = DenseBuffer(2_401)
    start, h = 100_014, 60
    x = 0.0
    d.add(start, x)
    blocks = [start]
    for k in range(1, 31):
        x += (0.001 * k) * (1 if k % 2 else -1)
        b = start + k * h
        d.add(b, x)
        blocks.append(b)
    now = blocks[-1]
    st = beta_quantiles(beta_samples(d, now, h, []))
    assert st.n == 30
    assert st.q95 == pytest.approx(0.029) and st.q99 == pytest.approx(0.030)
    st2 = beta_quantiles(beta_samples(d, now, h, [blocks[-1]]))
    assert st2.n == 29 and st2.q95 == pytest.approx(0.028) and st2.q99 == pytest.approx(0.029)
    st3 = beta_quantiles(beta_samples(d, now, h, [blocks[-1] - 30]))      # inside (s - h, s] of the last sample
    assert st3.n == 29 and st3.q99 == pytest.approx(0.029)
    st4 = beta_quantiles(beta_samples(d, now, h, [blocks[-2]]))            # end of the previous window, start of the last
    assert st4.n == 29 and st4.q99 == pytest.approx(0.030)


def test_beta_per_block_h5_matches_brute_force() -> None:
    lnp = {s: 0.0001 * ((s * s) % 101) for s in range(50_000, 52_200)}
    d = DenseBuffer(2_401)
    for s in sorted(lnp):
        d.add(s, lnp[s])
        d.evict(s)
    now = 52_199
    own = [51_000, 51_003, 52_100]
    got = beta_samples(d, now, 5, own)
    want = _brute_beta(lnp, now, 5, own)
    assert got == want and len(want) == 1_800 - 8 - 5                    # windows [51,000, 51,007] and [52,100, 52,104]
    st = beta_quantiles(got)
    srt = sorted(want)
    assert st.q95 == srt[math.ceil(0.95 * len(srt)) - 1] and st.q99 == srt[math.ceil(0.99 * len(srt)) - 1]


def test_beta_needs_ten_samples_and_horizon_rule() -> None:
    d = DenseBuffer(2_401)
    for b in range(0, 600, 60):
        d.add(b, b / 1e5)
    assert beta_quantiles(beta_samples(d, 540, 60, [])).q95 is None       # 9 samples
    assert beta_horizon(1, 5) == 5 and beta_horizon(60, 5) == 60 and beta_horizon(None, 5) == 5
    assert beta_horizon(3, 5) == 5                                         # short feed skip keeps the latency horizon


# ------------------------------------------------------------------------------------------------- fast EMA
def test_fast_ema_tracks_a_plain_ema_and_is_window_deterministic() -> None:
    xs = {b: 0.003 * (1 + 0.01 * math.sin(b / 50)) for b in range(0, 80_000, 60)}
    long_run, late = FastEma(), FastEma()
    plain = None
    for b, x in xs.items():
        long_run.update(b, x)
        if b >= 30_000:
            late.update(b, x)
        plain = x if plain is None else plain + ema_gain(60, 600) * (x - plain)
        if b >= 30_000 + 24_000:
            assert late.value() == long_run.value()                         # bit-identical after one anchor period
    assert long_run.value() == pytest.approx(plain, rel=1e-9)
    assert ema_gain(600, 600) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        long_run.update(0, 1.0)
