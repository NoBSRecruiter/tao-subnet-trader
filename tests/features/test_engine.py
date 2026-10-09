"""features.engine (WP5 acceptance): deterministic frames (mini-lake replayed twice; window-determinism), flow
validity, returns, beta horizons and own-fill exclusion, t* vs brute-force EMA stepping, emission parity, owner
features, warm-up, as-of calibration, book independence and the FeatureEngine protocol."""
from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Callable
from dataclasses import fields, replace
from decimal import Context, Decimal
from typing import Any

import pytest

from taotrader.core.codec import digest, encode
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent, ChainEventKind
from taotrader.core.fixed import DEC, to_ppm
from taotrader.core.protocols import FeatureEngine as FeatureEngineProtocol
from taotrader.core.state import ChainSnapshot, ReadPlan, SubnetState
from taotrader.core.units import AlphaRao, Block, Hotkey, Ppm, Rao, SubnetKey
from taotrader.features.engine import (
    BETA_UNKNOWN_PPM,
    WARM_BLOCKS,
    WARMUP_BLOCKS,
    FeatureEngine,
    FeatureParams,
    canonical_value,
    frame_digest,
    replay_start,
)
from taotrader.protocol.derive import derive_events
from taotrader.protocol.ema import ema_alpha, ema_step
from taotrader.protocol.emission import emission_vector
from taotrader.protocol.prune import ladder

LN20 = Context(prec=20)
FLOW_START = 8_466_531


def run(engine: FeatureEngine, snaps: list[ChainSnapshot], own: frozenset[Block] = frozenset()) -> list[Any]:
    out = []
    prev = None
    for s in snaps:
        out.append(engine.update(s, derive_events(prev, s), own))
        prev = s
    return out


def lnspot(s: SubnetState) -> float:
    return float(LN20.ln(s.pool.spot()))


# ------------------------------------------------------------------------------------------------- protocol
def test_engine_satisfies_the_core_protocol(make_engine: Callable[..., FeatureEngine]) -> None:
    eng: FeatureEngineProtocol = make_engine()
    assert eng.warm is False
    params = list(inspect.signature(FeatureEngine.update).parameters)
    assert params == ["self", "raw", "events", "own_fill_blocks"]        # book-independent inputs only


# ------------------------------------------------------------------------------------------------- determinism
@pytest.fixture(scope="module")
def rich_specs(spec_cls: Any, hk_cls: Any) -> list[Any]:
    Spec, HK = spec_cls, hk_cls
    return [
        Spec(5, reg_at=8_300_000, vol=0.03, period=313.0, moving_price="0.0031", escrow=2 * 10**12, owner_alpha=5 * 10**13,
             owner_hotkey_ident=1, autolock=False, flow_wave=200 * 10**9),
        Spec(9, reg_at=8_520_000, vol=0.05, period=97.0, moving_price="0.0026", flow_per_block=-20_000_000,
             hotkeys=(HK(1, 150_000, 0.00031, 0), HK(2, 90_000, 0.00024, 6_553), HK(3, 40_000, 0.0, 0, earns=False))),
        Spec(12, reg_at=8_540_000, vol=0.02, period=1_501.0, moving_price="0.0040", flow_per_block=None),
    ]


@pytest.fixture(scope="module")
def long_series(rich_specs: list[Any], series: Callable[..., list[ChainSnapshot]]) -> list[ChainSnapshot]:
    """~34 days at stride 60 (FULL), with an emission disable/re-enable and a take increase on netuid 9."""
    start = 9_000_000
    snaps = series(rich_specs, start, start + WARMUP_BLOCKS + 10 * 7_200, 60, last_reg_block=8_990_000)
    out = []
    for s in snaps:
        b = int(s.block)
        subs = []
        for x in s.subnets:
            if int(x.key.netuid) == 9 and start + 200_000 <= b < start + 200_600:
                x = replace(x, emission_enabled=False)
            if int(x.key.netuid) == 9 and b >= start + 230_000:
                x = replace(x, hotkeys=tuple(replace(h, take_u16=h.take_u16 + 500) if h.take_u16 == 6_553 else h for h in x.hotkeys))
            subs.append(x)
        out.append(replace(s, subnets=tuple(subs)))
    return out


# p_registration is a pure function of (globals, block, hazard): one horizon keeps the long runs fast without
# touching the engine state.
FAST = FeatureParams(p_reg_horizons=(7_200,))


@pytest.fixture(scope="module")
def long_run(long_series: list[ChainSnapshot], calibration: Any, provider_cls: Any) -> list[Any]:
    return run(FeatureEngine(provider_cls(calibration), FAST), long_series)


def test_window_determinism(long_series: list[ChainSnapshot], long_run: list[Any], calibration: Any, provider_cls: Any) -> None:
    """An engine started 1 day later reproduces every frame digest once WARMUP_BLOCKS have been ingested."""
    FrozenProvider = provider_cls
    late_start = 120                                                     # 1 day of stride-60 snapshots later
    late = FeatureEngine(FrozenProvider(calibration), FAST)
    full_eng = FeatureEngine(FrozenProvider(calibration), FAST)
    late_frames = run(late, long_series[late_start:])
    b1 = int(long_series[late_start].block)
    compared = 0
    for f_long, f_late in zip(long_run[late_start:], late_frames, strict=True):
        if int(f_long.block) >= b1 + WARMUP_BLOCKS:
            assert f_late.digest == f_long.digest, int(f_long.block)
            compared += 1
    assert compared >= 100
    run(full_eng, long_series)
    assert full_eng.state_digest() == late.state_digest()
    t = int(long_series[-1].block)
    first = full_eng.first_block
    assert first == int(long_series[0].block)
    assert replay_start(first, t) == t - WARMUP_BLOCKS
    assert replay_start(t - 100, t) == t - 100


def test_rich_series_fills_every_feat_field(long_run: list[Any]) -> None:
    last = long_run[-1]
    assert last.warm
    f5 = last.feats[SubnetKey(5, 8_300_000)]                            # type: ignore[arg-type]
    for fld in fields(f5):
        assert getattr(f5, fld.name) is not None, fld.name
    assert f5.best_candidate is not None and f5.router_candidates[0].eligible
    assert f5.ret_7d is not None and f5.sigma_d is not None and f5.sigma_d > 0 and f5.flow_z_1d is not None
    assert f5.owner_liquid_frac is not None and f5.owner_liquid_frac > 0 and f5.escrow_frac is not None
    f12 = last.feats[SubnetKey(12, 8_540_000)]                          # type: ignore[arg-type]
    assert f12.flow_1h is None and f12.flow_1d is None and f12.flow_z_1d is None   # SubnetTaoFlow absent
    assert last.beta_horizon_blocks == 60


def test_take_increase_and_emission_ban_are_tracked(long_series: list[ChainSnapshot], long_run: list[Any]) -> None:
    start = int(long_series[0].block)
    i_after = next(i for i, s in enumerate(long_series) if int(s.block) >= start + 230_000)
    f9 = long_run[i_after].feats[SubnetKey(9, 8_520_000)]               # type: ignore[arg-type]
    raised = [c for c in f9.router_candidates if c.take_u16 == 6_553 + 500]
    assert raised and raised[0].take_increase_recent and not raised[0].eligible
    before = long_run[i_after - 1].feats[SubnetKey(9, 8_520_000)]       # type: ignore[arg-type]
    assert not any(c.take_increase_recent for c in before.router_candidates)


def test_mini_lake_replayed_twice_gives_identical_frame_digests(tmp_path: Any, rich_specs: list[Any],
                                                                   series: Callable[..., list[ChainSnapshot]],
                                                                   make_engine: Callable[..., FeatureEngine]) -> None:
    """A Parquet mini-lake (WP3) replayed twice through fresh engines: identical frame and state digests, equal to the
    in-memory run (the lake round trip is exact)."""
    from taotrader.data.lake import Lake
    from taotrader.data.replay import ParquetReplay
    from taotrader.data.schema import with_digest

    start = 9_000_014
    snaps = [with_digest(s) for s in series(rich_specs, start - 2 * 7_200, start + 7_200, 60, last_reg_block=8_990_000)]
    lake = Lake(tmp_path / "lake")
    try:
        lake.write_snapshots(snaps)

        def replay() -> tuple[list[str], str]:
            async def collect() -> list[ChainSnapshot]:
                src = ParquetReplay(lake, start, start + 7_200 - 60, stride=60, warmup_blocks=2 * 7_200)
                return [item.snapshot async for item in src.stream(None)]
            got = asyncio.run(collect())
            eng = make_engine()
            return [f.digest for f in run(eng, got)], eng.state_digest()

        first, second = replay(), replay()
    finally:
        lake.close()
    assert first == second and len(first[0]) == len(snaps)
    eng = make_engine()
    assert [f.digest for f in run(eng, snaps)] == first[0] and eng.state_digest() == first[1]


def test_frame_digest_is_the_codec_digest(long_run: list[Any]) -> None:
    frame = long_run[-1]
    assert canonical_value(frame) == encode(frame)
    payload = encode(frame)
    payload.pop("digest")
    assert frame.digest == frame_digest(frame) == digest(payload)


# ------------------------------------------------------------------------------------------------- flows
def test_flow_validity_rules(spec_cls: Any, series: Callable[..., list[ChainSnapshot]], snap_of: Callable[..., ChainSnapshot],
                             synth: Callable[..., SubnetState], make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(7, reg_at=8_300_000, flow_per_block=40_000_000, tao=800 * 10**9)
    eng = make_engine()
    frames = run(eng, series([sp], FLOW_START - 600, FLOW_START + 7_800, 60, last_reg_block=8_300_000))
    for fr in frames:
        f = fr.feats[sp.key()]
        b = int(fr.block)
        if b - 300 < FLOW_START:
            assert f.flow_1h is None                                         # older end before 8,466,531
        else:
            assert f.flow_1h == pytest.approx(40_000_000 * 300 / (800 * 10**9))
        if b - 7_200 < FLOW_START:
            assert f.flow_1d is None
        else:
            assert f.flow_1d == pytest.approx(40_000_000 * 7_200 / (800 * 10**9))
        assert f.flow_7d is None
    # a new generation on the same netuid starts without flow history even though SubnetTaoFlow is set
    b0 = FLOW_START + 7_860
    new = spec_cls(7, reg_at=b0 - 30, flow_per_block=40_000_000, tao=800 * 10**9)
    fr = eng.update(snap_of(b0, [synth(new, b0)], last_reg_block=8_300_000), ())
    assert new.key() in fr.feats and sp.key() not in fr.feats
    assert fr.feats[new.key()].flow_1h is None and fr.feats[new.key()].ret_1h is None
    b = b0
    for _ in range(5):
        b += 60
        fr = eng.update(snap_of(b, [synth(new, b)], last_reg_block=8_300_000), ())
    assert fr.feats[new.key()].flow_1h == pytest.approx(40_000_000 * 300 / (800 * 10**9))


# ------------------------------------------------------------------------------------------------- returns / beta
def test_returns_use_the_median_of_three_grid_price(spec_cls: Any, series: Callable[..., list[ChainSnapshot]],
                                                    make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(3, reg_at=8_900_000, vol=0.04, period=211.0)
    snaps = series([sp], 9_000_000, 9_000_000 + 7_800, 60, last_reg_block=8_990_000)
    frames = run(make_engine(), snaps)
    lnp = {int(s.block): lnspot(s.subnets[0]) for s in snaps}

    def pbar(t: int) -> float:
        return sorted((lnp[t], lnp[t - 60], lnp[t - 120]))[1]

    t = int(snaps[-1].block)
    f = frames[-1].feats[sp.key()]
    assert f.ret_1h == pbar(t) - pbar(t - 300)
    assert f.ret_1d == pbar(t) - pbar(t - 7_200)
    assert f.ret_7d is None and f.sigma_d is None
    early = frames[2].feats[sp.key()]
    assert early.ret_1h is None                                          # generation-truncated: not enough history


def test_beta_h5_on_per_block_data_and_h60_on_stride_data(spec_cls: Any, series: Callable[..., list[ChainSnapshot]],
                                                          make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(4, reg_at=8_900_000, vol=0.02, period=7.3)
    per_block = series([sp], 9_100_000, 9_102_000, 1, last_reg_block=8_990_000)
    own = frozenset({Block(9_101_500), Block(9_101_990)})
    fr = run(make_engine(FAST), per_block, own)[-1]
    assert fr.beta_horizon_blocks == 5
    lnp = {int(s.block): lnspot(s.subnets[0]) for s in per_block}
    t = int(per_block[-1].block)
    sample = sorted(abs(lnp[s] - lnp[s - 5]) for s in range(t - 1_799, t + 1)
                    if not any(s - 5 < o <= s for o in own))
    assert len(sample) == 1_800 - 10
    f = fr.feats[sp.key()]
    assert f.beta_entry_ppm == to_ppm(sample[math.ceil(0.95 * len(sample)) - 1])
    assert f.beta_exit_ppm == to_ppm(sample[math.ceil(0.99 * len(sample)) - 1])

    stride = series([sp], 9_200_000, 9_200_000 + 60 * 40, 60, last_reg_block=8_990_000)
    own2 = frozenset({Block(9_200_000 + 60 * 39 - 7)})
    fr2 = run(make_engine(), stride, own2)[-1]
    assert fr2.beta_horizon_blocks == 60
    lnp2 = {int(s.block): lnspot(s.subnets[0]) for s in stride}
    t2 = int(stride[-1].block)
    sample2 = sorted(abs(lnp2[s] - lnp2[s - 60]) for s in sorted(lnp2)
                     if t2 - 1_800 < s <= t2 and s - 60 in lnp2 and not any(s - 60 < o <= s for o in own2))
    assert len(sample2) == 29                                            # 30 stride points minus the own-fill window
    f2 = fr2.feats[sp.key()]
    assert f2.beta_entry_ppm == to_ppm(sample2[27]) and f2.beta_exit_ppm == to_ppm(sample2[28])
    fresh = run(make_engine(), stride[:5])[-1].feats[sp.key()]
    assert fresh.beta_entry_ppm == BETA_UNKNOWN_PPM and fresh.beta_exit_ppm == BETA_UNKNOWN_PPM


def test_own_fill_blocks_change_only_beta(rich_specs: list[Any], series: Callable[..., list[ChainSnapshot]],
                                          make_engine: Callable[..., FeatureEngine]) -> None:
    snaps = series(rich_specs, 9_000_000, 9_000_000 + 3_600, 60, last_reg_block=8_990_000)
    a = run(make_engine(), snaps)[-1]
    b = run(make_engine(), snaps, frozenset(Block(9_000_000 + 60 * k) for k in range(40, 60)))[-1]
    assert a.digest != b.digest
    for k, fa in a.feats.items():
        fb = b.feats[k]
        assert replace(fa, beta_entry_ppm=Ppm(0), beta_exit_ppm=Ppm(0)) == replace(fb, beta_entry_ppm=Ppm(0), beta_exit_ppm=Ppm(0))


# ------------------------------------------------------------------------------------------------- prune
def test_t_star_matches_brute_force_ema_stepping(spec_cls: Any, synth: Callable[..., SubnetState],
                                                 snap_of: Callable[..., ChainSnapshot],
                                                 make_engine: Callable[..., FeatureEngine]) -> None:
    block = 9_240_388
    specs = [spec_cls(n, reg_at=8_000_000 + n, p0=float(ema) * 1.02, moving_price=ema)
             for n, ema in ((11, "0.0020"), (12, "0.0025"), (13, "0.0030"), (14, "0.0050"))]
    snap = snap_of(block, [synth(sp, block) for sp in specs], last_reg_block=9_210_610)
    fr = make_engine().update(snap, ())
    bottom = Decimal("0.0020")
    assert ladder(snap)[0] == specs[0].key()
    crossed = 0
    for sp in specs[1:]:
        s = snap.get(sp.key())
        assert s is not None
        f = fr.feats[sp.key()]
        a = ema_alpha(snap.glob, s, snap.block)
        stressed = DEC.multiply(s.pool.spot(), Decimal("0.5"))
        if stressed >= bottom:
            assert f.t_star_stress_blocks is None, sp.netuid                    # never crosses
            continue
        e, n = s.moving_price, 0
        while e > bottom:
            e = ema_step(e, stressed, a)
            n += 1
        assert f.t_star_stress_blocks == float(n), sp.netuid                # closed form == brute force (constant a)
        e2, n2, blk = s.moving_price, 0, int(snap.block)
        while e2 > bottom:                                                  # a(b) rising with b: within 0.1%
            e2 = ema_step(e2, stressed, ema_alpha(snap.glob, s, Block(blk + n2)))
            n2 += 1
        assert abs(n2 - n) <= max(2, n // 1_000)
        assert f.rho == pytest.approx(float(s.moving_price / bottom))
        assert f.prune_rank == specs.index(sp) + 1
        crossed += 1
    assert crossed == 2
    assert fr.feats[specs[0].key()].t_star_stress_blocks == 0.0
    assert fr.prune.target == specs[0].key() and fr.prune.ladder == tuple(sp.key() for sp in specs)


def test_prune_view_uses_the_calibration_as_of_the_block(spec_cls: Any, synth: Callable[..., SubnetState],
                                                          snap_of: Callable[..., ChainSnapshot], provider: Any,
                                                          calibration: Any) -> None:
    block = 9_240_388
    sp = spec_cls(11, reg_at=8_000_000)
    snap = snap_of(block, [synth(sp, block)], last_reg_block=block - 50_000)
    fr = FeatureEngine(provider).update(snap, ())
    assert provider.calls == [block]
    p = dict(fr.prune.p_reg_ppm)
    assert sorted(p) == [1_800, 7_200, 36_000, 50_400] and 0 < p[1_800] < p[7_200] < p[36_000] <= p[50_400] < 1_000_000
    assert fr.prune.window_open and fr.prune.blocks_to_window == 0 and fr.prune.hazard_valid

    class Ahead:
        def asof(self, b: Block) -> Any:
            return replace(calibration, asof=Block(int(b) + 1))

    with pytest.raises(LookaheadError):
        FeatureEngine(Ahead()).update(snap, ())


# ------------------------------------------------------------------------------------------------- emission parity
def test_emission_parity_model_ok(spec_cls: Any, hk_cls: Any, synth: Callable[..., SubnetState],
                                  snap_of: Callable[..., ChainSnapshot], make_engine: Callable[..., FeatureEngine]) -> None:
    specs = [spec_cls(n, reg_at=8_000_000 + n, p0=0.002 + 0.0004 * n, moving_price=str(Decimal("0.002") + Decimal("0.0004") * n))
             for n in range(1, 41)]

    def build(block: int, scale: Decimal) -> ChainSnapshot:
        base = snap_of(block, [synth(sp, block) for sp in specs], last_reg_block=9_100_000)
        shares = emission_vector(base, refresh_theta=False)
        subs = [replace(s, tao_in_emission=Rao(int(Decimal(int(shares[s.key].tao_in_per_block)) * scale)),
                        excess_tao=Rao(int(Decimal(int(shares[s.key].chain_buy_per_block)) * scale))) for s in base.subnets]
        return replace(base, subnets=tuple(subs))

    good = run(make_engine(), [build(9_200_000 + 60 * k, Decimal(1)) for k in range(8)])[-1]
    assert good.emission.model_ok and good.emission.parity_err_max_tao_day < 1e-3
    assert good.emission.root_flag == (good.emission.sum_ema > 1)
    bad = run(make_engine(), [build(9_200_000 + 60 * k, Decimal("1.1")) for k in range(8)])[-1]
    assert not bad.emission.model_ok and bad.emission.parity_err_max_tao_day > 0
    f = good.feats[specs[-1].key()]
    assert f.obs_emis_tao_day == pytest.approx(f.emis_tao_day, rel=1e-6) and f.emis_tao_day > 1


# ------------------------------------------------------------------------------------------------- owner
def test_owner_features(spec_cls: Any, hk_cls: Any, synth: Callable[..., SubnetState], snap_of: Callable[..., ChainSnapshot],
                        make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(21, reg_at=8_000_000, owner_alpha=10**14, owner_hotkey_ident=1, autolock=True,
                  hotkeys=(hk_cls(1, 200_000, 0.0, 0), hk_cls(2, 50_000, 0.0, 0), hk_cls(3, 40_000, 0.0, 0)))
    eng = make_engine()
    sold_per_step = 2 * 10**12
    frames = []
    for k in range(40):
        b = 9_200_000 + 60 * k
        s = synth(sp, b)
        s = replace(s, owner_alpha=AlphaRao(10**14 - sold_per_step * k), owner_cut_enabled=False)
        frames.append(eng.update(snap_of(b, [s], last_reg_block=9_100_000), ()))
    f = frames[-1].feats[sp.key()]
    s_last = synth(sp, 9_200_000 + 60 * 39)
    assert f.owner_sold_6h_frac == pytest.approx(30 * sold_per_step / s_last.pool.alpha)   # diffs ending in (t-1,800, t]
    assert f.owner_liquid_frac == 0.0                                   # auto-locked
    top = (10**14 - sold_per_step * 39 + (50_000 + 40_000) * 10**9) / (630_980 * 10**9 - 41_000 * 10**9)
    assert f.top_holder_frac == pytest.approx(top)
    unlocked = replace(s_last, owner_cut_autolock=False)
    fr = make_engine().update(snap_of(9_300_000, [unlocked], last_reg_block=9_100_000), ())
    assert fr.feats[sp.key()].owner_liquid_frac == pytest.approx(unlocked.owner_alpha / unlocked.pool.alpha)  # type: ignore[operator]
    assert fr.feats[sp.key()].owner_sold_6h_frac is None


# ------------------------------------------------------------------------------------------------- lifecycle
def test_warm_after_thirty_days_and_regime(spec_cls: Any, series: Callable[..., list[ChainSnapshot]],
                                           make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(2, reg_at=8_900_000)
    eng = make_engine()
    frames = run(eng, series([sp], 9_000_000, 9_000_000 + WARM_BLOCKS + 7_201, 7_200, last_reg_block=8_990_000))
    assert [fr.warm for fr in frames] == [int(fr.block) - 9_000_000 >= WARM_BLOCKS for fr in frames]
    assert frames[-2].warm and not frames[-3].warm and eng.warm and int(frames[-2].block) == 9_000_000 + WARM_BLOCKS
    assert frames[-1].regime_id == "v2_only"


def test_strict_order_and_event_blocks(spec_cls: Any, synth: Callable[..., SubnetState], snap_of: Callable[..., ChainSnapshot],
                                       make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(2, reg_at=8_900_000)
    eng = make_engine()
    s1 = snap_of(9_000_000, [synth(sp, 9_000_000)], last_reg_block=8_990_000)
    eng.update(s1, ())
    with pytest.raises(ValueError):
        eng.update(s1, ())
    s2 = snap_of(9_000_060, [synth(sp, 9_000_060)], last_reg_block=8_990_000)
    with pytest.raises(ValueError):
        eng.update(s2, (ChainEvent(ChainEventKind.EPOCH_DRAIN, Block(9_000_000)),))
    assert eng.update(s2, (ChainEvent(ChainEventKind.EPOCH_DRAIN, Block(9_000_060)),)).block == 9_000_060


def test_head_snapshots_do_not_record_the_carried_panel(spec_cls: Any, synth: Callable[..., SubnetState],
                                                        snap_of: Callable[..., ChainSnapshot],
                                                        make_engine: Callable[..., FeatureEngine]) -> None:
    sp = spec_cls(2, reg_at=8_900_000)
    full_eng, mixed_eng = make_engine(), make_engine()
    for k in range(30):
        b = 9_000_000 + 60 * k
        s = synth(sp, b)
        full_eng.update(snap_of(b, [s], last_reg_block=8_990_000), ())
        bumped = replace(s, hotkeys=tuple(replace(h, total_alpha=AlphaRao(h.total_alpha * 2)) for h in s.hotkeys))
        mixed_eng.update(snap_of(b, [s], last_reg_block=8_990_000), ())
        mixed_eng.update(snap_of(b + 1, [bumped], plan=ReadPlan.HEAD, last_reg_block=8_990_000), ())
    a = full_eng.last_frame.feats[sp.key()].router_candidates          # type: ignore[union-attr]
    b2 = mixed_eng.last_frame.feats[sp.key()].router_candidates         # type: ignore[union-attr]
    assert [c.score_ppm_day for c in a] == [c.score_ppm_day for c in b2]
    assert [c.member_frac_ppm for c in a] == [c.member_frac_ppm for c in b2]


def test_params_from_config() -> None:
    from taotrader.core.config import ExecCfg, RiskCfg
    p = FeatureParams.from_config(RiskCfg(take_max_ppm=20_000, d_stress_ppm=600_000), ExecCfg(finality_lag_blocks=4))  # type: ignore[arg-type]
    assert p.router.take_max_ppm == 20_000 and p.d_stress_ppm == 600_000 and p.finality_lag_blocks == 4
    assert p.universe.unwind_blocks == 15 and FeatureParams.from_config() == FeatureParams()


def test_feature_floats_are_reproducible_across_constructions(rich_specs: list[Any], series: Callable[..., list[ChainSnapshot]],
                                                              make_engine: Callable[..., FeatureEngine]) -> None:
    snaps = series(rich_specs, 9_000_000, 9_000_000 + 1_200, 60, last_reg_block=8_990_000)
    assert [f.digest for f in run(make_engine(), snaps)] == [f.digest for f in run(make_engine(), snaps)]
    hk = Hotkey("0x" + "0" * 64)
    assert hk not in {c.hotkey for f in run(make_engine(), snaps)[-1].feats.values() for c in f.router_candidates}
