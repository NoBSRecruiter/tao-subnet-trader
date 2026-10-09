"""features.universe: overlay floor sections A-G evaluated per generation (each section toggled on its own), the
section 3.4 emission ban, projected ladder rank for soon-expiring immune subnets, and the published count."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from taotrader.core.state import ChainSnapshot, SubnetState
from taotrader.core.units import AlphaRao, Block, Rao, SubnetKey
from taotrader.features import gatekeeper as gk
from taotrader.features import universe as uni
from taotrader.features.engine import FeatureEngine
from taotrader.protocol.ema import project_ema
from taotrader.protocol.prune import cost_ratio, immunity_end, ladder

BLOCK = 9_240_388
K = 10                                         # the subnet under test: rank 10 of 10, EMA 0.004 = 4x the target


@pytest.fixture()
def base(spec_cls: Any, synth: Callable[..., SubnetState], snap_of: Callable[..., ChainSnapshot]) -> Callable[..., ChainSnapshot]:
    def build(block: int = BLOCK, k_over: dict[str, Any] | None = None, **glob: Any) -> ChainSnapshot:
        subs = []
        for n in range(1, 11):
            ema = Decimal("0.001") + Decimal("0.0001") * (n - 1) if n != K else Decimal("0.004")
            sp = spec_cls(n, reg_at=8_000_000 + n, p0=float(ema), moving_price=str(ema), escrow=10**12)
            s = synth(sp, block)
            if n == K and k_over:
                s = replace(s, **k_over)
            subs.append(s)
        kw = {"last_reg_block": block - 2_000}
        kw.update(glob)
        return snap_of(block, subs, **kw)
    return build


def evaluate(snap: ChainSnapshot, *, flags: dict[SubnetKey, frozenset[str]] | None = None,
             t_star: dict[SubnetKey, int | None] | None = None, validator_ok: bool = True, ban: int | None = None,
             params: uni.UniverseParams | None = None) -> uni.UniverseRow:
    lad = ladder(snap)
    key = snap.by_netuid(K).key                                          # type: ignore[union-attr]
    target = lad[0] if lad else None
    inp = uni.UniverseInputs(
        snap=snap, prune_rank={k: i + 1 for i, k in enumerate(lad)}, target=target,
        bottom_ema=snap.get(target).moving_price if target is not None else None,  # type: ignore[union-attr]
        t_star=t_star or {}, flags=flags or {}, validator_ok={key: validator_ok}, emission_ban_until={key: ban},
        cost_ratio=cost_ratio(snap.glob, snap.block))
    rows = {r.key: r for r in uni.evaluate(inp, params or uni.UniverseParams())}
    return rows[key]


def test_baseline_is_eligible(base: Callable[..., ChainSnapshot]) -> None:
    row = evaluate(base())
    assert row.eligible and row.failed == () and all((row.a_hold, row.a_enter, row.b, row.c, row.d, row.e, row.f, row.g))


@pytest.mark.parametrize(("over", "section"), [
    ({"first_emission_block": None}, "A"),
    ({"reg_allowed": False}, "A"),
    ({"subtoken_enabled": False}, "A"),
    ({"emission_enabled": False}, "C"),
    ({"miner_burned": Decimal("0.51")}, "F"),
])
def test_single_field_sections(base: Callable[..., ChainSnapshot], over: dict[str, Any], section: str) -> None:
    row = evaluate(base(k_over=over))
    assert section in row.failed
    if section != "A":
        assert row.failed == (section,)


def test_section_b_liquidity(base: Callable[..., ChainSnapshot]) -> None:
    s = base().by_netuid(K)
    assert s is not None
    p = s.pool
    small = replace(p, tao=Rao(199 * 10**9), px_tao=199 * 10**9)
    assert evaluate(base(k_over={"pool": small})).failed == ("B",)
    assert evaluate(base(k_over={"pool": replace(p, w_quote_e18=29 * 10**16)})).failed == ("B",)
    assert evaluate(base(k_over={"pool": replace(p, w_quote_e18=30 * 10**16)})).failed == ()
    assert evaluate(base(k_over={"pool": replace(p, fee_rate=331)})).failed == ("B",)
    assert evaluate(base(k_over={"escrow_alpha": AlphaRao(p.alpha // 2 + 1)})).failed == ("B",)
    assert evaluate(base(k_over={"escrow_alpha": AlphaRao(p.alpha // 2)})).failed == ()
    assert evaluate(base(k_over={"escrow_alpha": None})).failed == ()          # unread escrow counts as 0 (WP4 decode)


def test_section_c_emission_ban() -> None:
    p = uni.UniverseParams()
    assert uni.emission_ban_until(None, None, p) is None
    assert uni.emission_ban_until(1_000, None, p) == 101_800
    assert uni.emission_ban_until(1_000, 200_000, p) == 200_360
    assert uni.emission_ban_until(None, 5_000, p) == 5_360


def test_section_c_ban_window(base: Callable[..., ChainSnapshot]) -> None:
    assert evaluate(base(), ban=BLOCK + 1).failed == ("C",)
    assert evaluate(base(), ban=BLOCK).failed == ()


def test_section_d_prune_floor(base: Callable[..., ChainSnapshot]) -> None:
    low = base(k_over={"moving_price": Decimal("0.00145")})            # rank 6, but EMA 1.45x the target
    assert evaluate(low).failed == ("D",)
    ok = base(k_over={"moving_price": Decimal("0.0015")})              # rank 6 and exactly 1.5x
    assert evaluate(ok).failed == ()
    rank5 = base(k_over={"moving_price": Decimal("0.00135")})
    assert evaluate(rank5).failed == ("D",)
    hot = {base().by_netuid(K).key: frozenset({gk.REG_CLOCK_HOT})}     # type: ignore[union-attr]
    assert evaluate(base(k_over={"moving_price": Decimal("0.0016")}), flags=hot).failed == ("D",)   # rank 7 < 8
    assert evaluate(base(), flags=hot).failed == ()                    # rank 10, 4x


def test_section_d_tier_a_and_backstop_zones(base: Callable[..., ChainSnapshot]) -> None:
    snap = base()
    key = snap.by_netuid(K).key                                         # type: ignore[union-attr]
    opening = {"last_reg_block": BLOCK - 14_400 + 100}                  # window opens in 100 blocks < U + M_A
    assert evaluate(base(**opening), t_star={key: 315}).failed == ("D",)
    assert evaluate(base(**opening), t_star={key: 316}).failed == ()
    closed = {"last_reg_block": BLOCK - 14_400 + 400}                   # opens beyond U + M_A
    assert evaluate(base(**closed), t_star={key: 10}).failed == ()
    assert evaluate(base(n_nonroot_networks=100, **opening), t_star={key: 10}).failed == ()   # no prune possible
    # backstop needs rank <= 3: K never is; check the rule on a backstop-zone variant with r <= 1.2
    p = uni.UniverseParams(entry_min_rank=1, entry_min_rho_ppm=0, k_bottom=10)
    late = {"last_reg_block": BLOCK - 50_000}                           # r = 2 - 50,000/57,600 = 1.13
    assert evaluate(base(**late), params=p).failed == ("D",)
    assert evaluate(base(last_reg_block=BLOCK - 40_000), params=p).failed == ()   # r = 1.31


def test_section_d_immune_projection(base: Callable[..., ChainSnapshot]) -> None:
    soon = BLOCK - 864_000 + 10_000                                     # expires in 10,000 blocks (< 7 d)
    snap = base(k_over={"key": SubnetKey(K, Block(soon))})               # type: ignore[arg-type]
    row = evaluate(snap)
    assert row.d                                                        # projected 4x EMA: rank 10 at expiry
    pool = base().by_netuid(K).pool                                     # type: ignore[union-attr]
    cheap = replace(pool, alpha=AlphaRao(pool.alpha * 10 // 3), px_alpha=pool.alpha * 10 // 3)   # spot 0.0012
    low = base(k_over={"key": SubnetKey(K, Block(soon)), "moving_price": Decimal("0.0012"), "pool": cheap})  # type: ignore[arg-type]
    assert evaluate(low).failed == ("D",)
    drifting_up = base(k_over={"key": SubnetKey(K, Block(soon)), "moving_price": Decimal("0.0012")})  # type: ignore[arg-type]
    assert evaluate(drifting_up).d                                      # spot 0.004 lifts the flat-spot EMA forecast
    far = BLOCK - 864_000 + 60_000                                      # expiry beyond 7 d: no prune constraint
    assert evaluate(base(k_over={"key": SubnetKey(K, Block(far)), "moving_price": Decimal("0.0012")})).d  # type: ignore[arg-type]


def test_projected_rank_matches_full_projection(base: Callable[..., ChainSnapshot]) -> None:
    soon = BLOCK - 864_000 + 10_000
    snap = base(k_over={"key": SubnetKey(K, Block(soon)), "moving_price": Decimal("0.00125")})   # type: ignore[arg-type]
    key = SubnetKey(K, Block(soon))                                     # type: ignore[arg-type]
    expiry = int(immunity_end(snap.get(key), snap.glob))                # type: ignore[arg-type]
    dn = expiry - BLOCK
    proj = sorted((project_ema(snap.glob, s, snap.block, dn, s.pool.spot()), s.key.reg_at, s.key.netuid, s.key)
                  for s in snap.subnets if expiry >= int(immunity_end(s, snap.glob)))
    want = [x[3] for x in proj].index(key) + 1
    assert uni.projected_rank(snap, key, expiry) == want
    assert uni.projected_rank(snap, key, expiry - 1) is None             # still immune


def test_section_e_launch_age_and_vetoes(base: Callable[..., ChainSnapshot]) -> None:
    key = base().by_netuid(K).key                                       # type: ignore[union-attr]
    young_start = base(k_over={"first_emission_block": Block(BLOCK - 100_800 + 2)})
    assert evaluate(young_start).failed == ("E",)
    assert evaluate(base(k_over={"first_emission_block": Block(BLOCK - 100_800 + 1)})).failed == ()
    for flag in sorted(gk.NON_LCW_VETOES):
        assert evaluate(base(), flags={key: frozenset({flag})}).failed == ("E",), flag
    for flag in (gk.YOUNG_IMMUNE, gk.GATE_STARVED):
        assert evaluate(base(), flags={key: frozenset({flag})}).failed == ()


def test_section_e_age_reg(spec_cls: Any, synth: Callable[..., SubnetState], snap_of: Callable[..., ChainSnapshot]) -> None:
    p = uni.UniverseParams(min_since_start_blocks=0)
    for age, ok in ((215_999, False), (216_000, True)):
        sp = spec_cls(K, reg_at=BLOCK - age)
        snap = snap_of(BLOCK, [synth(sp, BLOCK)], last_reg_block=BLOCK - 2_000, n_nonroot_networks=100)
        inp = uni.UniverseInputs(snap=snap, prune_rank={}, target=None, bottom_ema=None, t_star={}, flags={},
                                 validator_ok={sp.key(): True}, emission_ban_until={}, cost_ratio=Decimal(1))
        row = uni.evaluate(inp, replace(p, immune_lookahead_blocks=0))[0]
        assert row.e is ok, age


def test_section_g_validator(base: Callable[..., ChainSnapshot]) -> None:
    assert evaluate(base(), validator_ok=False).failed == ("G",)


def test_published_count_matches_rows(spec_cls: Any, synth: Callable[..., SubnetState], snap_of: Callable[..., ChainSnapshot],
                                      make_engine: Callable[..., FeatureEngine]) -> None:
    specs = [spec_cls(n, reg_at=8_000_000 + n, p0=0.001 * n, moving_price=str(Decimal("0.001") * n)) for n in range(1, 11)]
    eng = make_engine()
    fr = None
    for m in range(25):
        b = 9_000_000 + 360 * m
        fr = eng.update(snap_of(b, [synth(sp, b) for sp in specs], last_reg_block=8_990_000), ())
    assert fr is not None
    rows = eng.last_universe
    assert fr.universe_eligible == sum(1 for r in rows if r.eligible) and len(rows) == 10
    eligible = {int(r.key.netuid) for r in rows if r.eligible}
    assert eligible == set(range(6, 11))                                 # ranks 1-5 fail the prune floor
    assert all(r.failed == ("D",) for r in rows if int(r.key.netuid) < 6)
    assert all(fr.feats[r.key].best_candidate is not None for r in rows)
