"""protocol.emission: block-emission curve, rp, the get_shares replica (20-block golden parity), injection split,
parity_ok."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from taotrader.core.fixed import DEC
from taotrader.core.units import RAO_PER_TAO, Block, Rao
from taotrader.protocol.emission import (
    alpha_issuance,
    block_emission_for_issuance,
    emission_vector,
    emit_eligible,
    gate_keep,
    gate_theta,
    observed_block_emission,
    parity_ok,
    parity_rel_errors,
    root_prop,
    sum_ema,
)

TAO = RAO_PER_TAO
PARITY_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "emission_parity"
PARITY_FILES = sorted(p.stem for p in PARITY_DIR.glob("b*.json"))


# ------------------------------------------------------------------------------------------------- block emission
@pytest.mark.parametrize(("issuance_tao", "emission_rao"), [
    (0, 10**9), (10_499_999, 10**9), (10_500_000, 5 * 10**8), (11_600_000, 5 * 10**8), (11_597_600, 5 * 10**8),
    (15_749_999, 5 * 10**8), (15_750_000, 25 * 10**7), (18_375_000, 125 * 10**6), (21_000_000, 0), (22_000_000, 0),
])
def test_block_emission_curve(issuance_tao: int, emission_rao: int) -> None:
    assert block_emission_for_issuance(issuance_tao * TAO) == emission_rao


def test_block_emission_curve_boundary_to_the_rao() -> None:
    half = 10_500_000 * TAO
    assert block_emission_for_issuance(half - 1) == 10**9 and block_emission_for_issuance(half) == 5 * 10**8


def test_block_emission_matches_runtime_api_on_fixtures(gsnap, dec) -> None:
    for name in ("sn92_9240388", "globals_9240878", "sn51_emission_9240382"):
        gs = gsnap(name, 0)
        runtime = dec.le(gs.runtime("SubnetInfoRuntimeApi_get_block_emission"))
        assert block_emission_for_issuance(dec.le(gs.get("SubtensorModule.TotalIssuance"))) == runtime == 5 * 10**8
    for name in PARITY_FILES:
        gs = gsnap(f"emission_parity/{name}", 0)
        runtime = dec.le(gs.runtime("SubnetInfoRuntimeApi_get_block_emission"))
        assert block_emission_for_issuance(dec.le(gs.get("SubtensorModule.TotalIssuance"))) == runtime


def test_alpha_curve_one_alpha_per_block(make_subnet) -> None:
    s = make_subnet()
    assert block_emission_for_issuance(alpha_issuance(s)) == 10**9


# ------------------------------------------------------------------------------------------------- root prop
def test_root_prop_formula_matches_stored_rootprop(gsnap, dec) -> None:
    gs = gsnap("sn51_emission_9240382", 0)
    glob = dec.build_globals(gs)
    s = dec.build_subnet(gs, 51, glob)
    rp = root_prop(glob, alpha_issuance(s))
    assert abs(rp - s.root_prop) / s.root_prop < Decimal("1e-8")
    assert round(rp, 3) == Decimal("0.139")
    assert root_prop(replace(glob, root_tao=Rao(0)), alpha_issuance(s)) == 0


# ------------------------------------------------------------------------------------------------- parity
def _pair(gsnap, dec, name: str):
    prev = dec.build_snapshot(gsnap(name, 0))
    cur = dec.build_snapshot(gsnap(name, 1))
    return prev, cur


@pytest.mark.parametrize("name", PARITY_FILES)
def test_emission_replica_parity_on_golden_blocks(gsnap, dec, name: str) -> None:
    """Per-subnet |E_model - (SubnetTaoInEmission + SubnetExcessTao)| <= 1e-6 TAO/block (section 10.1)."""
    prev, cur = _pair(gsnap, dec, f"emission_parity/{name}")
    assert cur.block == prev.block + 1 and cur.block >= 8_765_684
    model = emission_vector(prev, refresh_theta=cur.block % 360 == 0)
    worst = 0
    total_model = 0
    for c in cur.subnets:
        obs = c.tao_in_emission + c.excess_tao
        share = model.get(c.key)
        e = share.tao_per_block if share is not None else 0
        total_model += e
        worst = max(worst, abs(e - obs))
        if share is not None and obs > 0:
            assert abs(share.tao_in_per_block - c.tao_in_emission) <= 2_000      # split within 2e-6 TAO/block
            assert abs(share.chain_buy_per_block - c.excess_tao) <= 2_000
    assert worst <= 1_000, f"{name}: worst per-subnet error {worst} rao/block"
    assert worst <= 2, f"{name}: the replica was exact to 1 rao/block at capture; drift {worst} rao/block"
    assert abs(total_model - prev.glob.block_emission) <= len(model)          # sum = 0.5 TAO/block up to floors


def test_emission_replica_parity_at_spec_475(gsnap, dec) -> None:
    """Every subnet at 9,240,382 (spec 475, "precise emissions"): the rank-32 replica still holds to the rao."""
    prev, cur = _pair(gsnap, dec, "sn51_emission_9240382")
    assert cur.glob.spec_version == 475 and cur.block == prev.block + 1
    model = emission_vector(prev, refresh_theta=cur.block % 360 == 0)
    errs = [abs(model[c.key].tao_per_block - (c.tao_in_emission + c.excess_tao)) for c in cur.subnets]
    assert len(errs) >= 125 and max(errs) <= 2
    assert sum(1 for c in cur.subnets if c.tao_in_emission + c.excess_tao > 0) >= 90
    rel = parity_rel_errors([(7_200 * model[c.key].tao_per_block, 7_200 * (c.tao_in_emission + c.excess_tao))
                             for c in cur.subnets])
    assert len(rel) >= 45 and parity_ok(rel)                                    # the T2a / model_ok gate passes


def test_refresh_semantics_on_a_360_block(gsnap, dec) -> None:
    """At block % 360 == 0 the chain recomputes theta (rank 32) before gating; at other blocks the stored bar holds."""
    prev, cur = _pair(gsnap, dec, "emission_parity/b8766000")
    assert cur.block % 360 == 0
    refreshed = emission_vector(prev, refresh_theta=True)
    stored = emission_vector(prev, refresh_theta=False)

    def err(m) -> int:
        return max(abs(m[c.key].tao_per_block - (c.tao_in_emission + c.excess_tao)) for c in cur.subnets if c.key in m)

    assert err(refreshed) <= 1_000 < err(stored)


def test_sn51_injection_split_vector(gsnap, dec) -> None:
    """SN51 at 9,240,382: rp 0.139, alpha_in 0.139/block, tao_in 0.01397, chain buy 0.0515 TAO/block, ~472 TAO/day;
    network sum of tao_in + excess = 0.5 TAO/block."""
    prev, cur = _pair(gsnap, dec, "sn51_emission_9240382")
    model = emission_vector(prev, refresh_theta=cur.block % 360 == 0)
    s51 = prev.by_netuid(51)
    assert s51 is not None
    sh = model[s51.key]
    assert round(s51.root_prop, 3) == Decimal("0.139")
    cap_alpha = s51.root_prop * block_emission_for_issuance(alpha_issuance(s51))
    assert round(cap_alpha / TAO, 3) == Decimal("0.139")                    # alpha_in per block (capped)
    assert round(Decimal(sh.tao_in_per_block) / TAO, 5) == Decimal("0.01397")
    assert round(Decimal(sh.chain_buy_per_block) / TAO, 4) == Decimal("0.0515")
    assert abs(Decimal(sh.tao_per_block) * 7_200 / TAO - 472) < 1
    c51 = cur.by_netuid(51)
    assert c51 is not None
    assert abs(sh.tao_in_per_block - c51.tao_in_emission) <= 100
    assert abs(sh.chain_buy_per_block - c51.excess_tao) <= 1_000
    network = sum(c.tao_in_emission + c.excess_tao for c in cur.subnets)
    assert abs(network - 5 * 10**8) <= 200                                    # 0.5 TAO/block (chain floors per subnet)
    assert sum_ema(prev) > 1                                                  # root dividends accrue (brief: 1.185)


# ------------------------------------------------------------------------------------------------- gate mechanics
def test_gate_keep_table() -> None:
    theta = Decimal("0.0082624")
    assert gate_keep(theta, theta, 3) == Decimal("0.5")
    assert round(gate_keep(theta / 2, theta, 3), 2) == Decimal("0.11")
    assert round(gate_keep(theta / 3, theta, 3), 3) == Decimal("0.036")
    assert round(gate_keep(theta / 5, theta, 3), 3) == Decimal("0.008")
    assert gate_keep(Decimal(0), theta, 3) == 0 and gate_keep(theta, Decimal(0), 3) == 1


def test_gate_theta_rank() -> None:
    vals = [Decimal(i) for i in range(1, 41)] + [Decimal(0), Decimal(-1)]
    assert gate_theta(vals, 32) == Decimal(9)                                 # 32nd largest of 1..40
    assert gate_theta([Decimal(3), Decimal(2)], 32) == Decimal(2)             # fewer than N positive: smallest
    assert gate_theta([Decimal(0)], 32) == 0


def _toy_snapshot(make_subnet, make_snapshot, n: int = 40, block: int = 9_000_000, **glob):
    subs = [make_subnet(i, reg_at=1_000_000 + i, moving_price=Decimal(i) / 1_000, miner_burned=Decimal(0))
            for i in range(1, n + 1)]
    return make_snapshot(block, subs, **glob)


def test_disabled_subnets_keep_their_rank_in_theta(make_subnet, make_snapshot) -> None:
    snap = _toy_snapshot(make_subnet, make_snapshot)
    base = emission_vector(snap, refresh_theta=True)
    top = max(snap.subnets, key=lambda s: s.moving_price).key
    off = emission_vector(snap, enabled_override={top: False}, refresh_theta=True)
    assert off[top].final == 0 and off[top].tao_per_block == 0
    for s in snap.subnets:
        assert off[s.key].keep == base[s.key].keep                            # theta unchanged by the disable
        assert off[s.key].b == base[s.key].b
    assert sum(v.final for v in off.values()) == pytest.approx(1)
    assert sum(v.tao_per_block for v in off.values()) <= snap.glob.block_emission


def test_burn_adjustment_and_fallback(make_subnet, make_snapshot) -> None:
    snap = _toy_snapshot(make_subnet, make_snapshot, n=4)
    burned = make_snapshot(snap.block, [replace(s, miner_burned=Decimal(1)) for s in snap.subnets])
    v = emission_vector(burned, refresh_theta=True)
    assert all(x.b > 0 for x in v.values())                                   # all weights 0 -> fall back to s
    half = make_snapshot(snap.block, [replace(s, miner_burned=Decimal("0.5") if int(s.key.netuid) == 4 else Decimal(0))
                                      for s in snap.subnets])
    w = emission_vector(half, refresh_theta=True)
    k4 = half.by_netuid(4)
    k3 = half.by_netuid(3)
    assert k4 is not None and k3 is not None
    assert w[k4.key].b == pytest.approx(w[k3.key].b * Decimal(4) / 3 * Decimal("0.5"))


def test_ineligible_subnets_get_nothing(make_subnet, make_snapshot) -> None:
    subs = [make_subnet(1, moving_price=Decimal("0.01")),
            make_subnet(2, moving_price=Decimal("0.01"), first_emission_block=None),
            make_subnet(3, moving_price=Decimal("0.01"), subtoken_enabled=False),
            make_subnet(4, moving_price=Decimal("0.01"), reg_allowed=False)]
    snap = make_snapshot(9_000_000, subs)
    v = emission_vector(snap, refresh_theta=True)
    assert [emit_eligible(s) for s in snap.subnets] == [True, False, False, False]
    assert v[subs[0].key].final == 1
    for s in subs[1:]:
        assert v[s.key].final == 0 and v[s.key].tao_per_block == 0 and v[s.key].b == 0


def test_zero_ema_gives_zero_emission(make_subnet, make_snapshot) -> None:
    snap = make_snapshot(9_000_000, [make_subnet(1, moving_price=Decimal(0)), make_subnet(2, moving_price=Decimal(0))])
    assert all(v.tao_per_block == 0 for v in emission_vector(snap).values())


def test_ema_override_changes_shares(make_subnet, make_snapshot) -> None:
    snap = _toy_snapshot(make_subnet, make_snapshot, n=3)
    k1 = snap.subnets[0].key
    v = emission_vector(snap, ema_override={k1: Decimal("0.003")}, refresh_theta=True)
    k3 = snap.subnets[2].key
    assert v[k1].b == v[k3].b


def test_injection_cap_and_chain_buy(make_subnet, make_snapshot, make_pool) -> None:
    """E above rp*alpha_emission*spot -> the excess is a chain buy; below the cap all of E is injected."""
    s = make_subnet(1, moving_price=Decimal("0.02"), root_prop=Decimal("0.2"))
    snap = make_snapshot(9_000_000, [s])
    sh = emission_vector(snap)[s.key]
    spot = s.pool.spot()
    cap = DEC.multiply(DEC.multiply(Decimal("0.2"), Decimal(10**9)), spot)
    assert sh.tao_per_block == snap.glob.block_emission
    assert sh.tao_in_per_block == int(cap)
    assert sh.chain_buy_per_block + sh.tao_in_per_block in (sh.tao_per_block, sh.tao_per_block - 1)
    rich = replace(s, root_prop=Decimal("0.99"), pool=make_pool(1_000 * TAO, 1_000 * TAO))      # spot 1 TAO/alpha
    sh2 = emission_vector(make_snapshot(9_000_000, [rich]))[s.key]
    assert sh2.tao_in_per_block == sh2.tao_per_block and sh2.chain_buy_per_block == 0           # uncapped: all injected


@pytest.mark.parametrize(("block", "weighted_by_rp", "gated"), [
    (8_500_000, True, False), (8_700_000, False, False), (8_740_000, False, True), (9_000_000, False, True),
])
def test_share_rule_follows_the_regime(make_subnet, make_snapshot, block: int, weighted_by_rp: bool, gated: bool) -> None:
    subs = [make_subnet(i, reg_at=1_000 + i, moving_price=Decimal("0.01"), root_prop=Decimal(i) / 10) for i in (1, 2)]
    v = emission_vector(make_snapshot(block, subs, gate_bar=Decimal("0.6")))
    b1, b2 = v[subs[0].key].b, v[subs[1].key].b
    if weighted_by_rp:
        assert b2 == pytest.approx(2 * b1)
    else:
        assert b1 == b2
    assert (v[subs[0].key].keep < 1) is gated


# ------------------------------------------------------------------------------------------------- parity gate
def _errs(pcts: list[str]) -> list[Decimal]:
    return [Decimal(p) / 100 for p in pcts]


def test_parity_ok_boundaries() -> None:
    assert not parity_ok([])
    assert parity_ok(_errs(["0.99"] * 10))
    assert not parity_ok(_errs(["1.01"] * 10))
    assert not parity_ok(_errs(["1.00"] * 10))                               # median must be < 1%
    nine_of_ten = _errs(["0.1"] * 9 + ["6"])                                  # 90% within 5%
    assert parity_ok(nine_of_ten)
    within_89 = _errs(["0.1"] * 89 + ["6"] * 11)                              # 89% within 5%
    assert not parity_ok(within_89)
    assert parity_ok(_errs(["0.1"] * 90 + ["6"] * 10))
    assert parity_ok(_errs(["0.5", "0.9", "1.5", "0.2"]))                    # even count: median (0.5+0.9)/2 = 0.7%
    assert parity_ok(_errs(["0.2", "0.5", "1.2", "1.5"]))                    # median (0.5+1.2)/2 = 0.85%
    assert not parity_ok(_errs(["0.2", "0.9", "1.2", "1.5"]))                 # median (0.9+1.2)/2 = 1.05%
    assert parity_ok(_errs(["5"] * 9 + ["0"] * 11))                           # exactly 5% counts as within


def test_parity_rel_errors_and_observed_emission(make_subnet) -> None:
    errs = parity_rel_errors([(110, 100), (0, 5), (95 * TAO, 100 * TAO), (2 * TAO, 2 * TAO)], min_obs_rao_day=TAO)
    assert errs == (Decimal("0.05"), Decimal(0))
    a = make_subnet(5, tao_in_emission=Rao(100), excess_tao=Rao(50), reservoir_tao=Rao(30))
    b = replace(a, reservoir_tao=Rao(45))
    assert observed_block_emission(b, a) == 165 and observed_block_emission(a) == 150
    other = make_subnet(5, reg_at=Block(42), reservoir_tao=Rao(0))
    assert observed_block_emission(b, other) == 150                           # different generation: no delta
