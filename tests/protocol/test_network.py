"""WP2 network tests (read-only JSON-RPC against the public archive, <= 2.5 req/s; run with `-m network`).

- Prune rule 52/52 (section 10.1): for each row of the brief section 4.4 prune log, ladder(snapshot at P-1)[0] is the
  logged netuid (SN73 at 5,145,525 is a dissolve and is not in the log).
- Live prune target SN58 at 8,500,160.
- First 0.5-TAO block 7,103,976: the curve on TotalIssuance gives 1 TAO after block 7,103,974 and 0.5 after 7,103,975.
- SN47 (legacy) recovery ratio ~0.36 at 9,240,388.
- Emission replica parity on every subnet at a recent finalized block (spec 475 or later: "re-verify all replicas",
  section 8.6), using the SN51 fixture's key set re-read at blocks n-1 and n.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from taotrader.core.fixed import EXACT
from taotrader.core.units import AlphaRao, Block, Rao
from taotrader.protocol.emission import block_emission_for_issuance, emission_vector, parity_ok, parity_rel_errors
from taotrader.protocol.prune import ladder, recovery_ratio

pytestmark = pytest.mark.network

MAX_NETUID = 144
SM = "SubtensorModule."

# brief section 4.4 compact prune log: (block P, pruned netuid)
PRUNE_LOG: tuple[tuple[int, int], ...] = (
    (6_693_448, 100), (6_783_158, 49), (6_841_399, 105), (6_914_378, 86), (6_962_737, 94), (7_013_758, 92),
    (7_063_126, 90), (7_105_263, 108), (7_119_664, 113), (7_151_800, 80), (7_173_591, 31), (7_208_725, 87),
    (7_236_936, 67), (7_257_480, 109), (7_284_230, 38), (7_312_241, 114), (7_340_355, 47), (7_366_897, 15),
    (7_415_113, 99), (7_457_580, 107), (7_525_773, 126), (7_574_784, 76), (7_633_645, 91), (7_692_872, 96),
    (7_735_450, 97), (7_787_562, 70), (7_840_965, 102), (7_894_898, 36), (7_966_145, 78), (8_026_517, 82),
    (8_057_320, 57), (8_085_297, 84), (8_123_781, 26), (8_138_182, 69), (8_238_082, 122), (8_294_730, 116),
    (8_352_006, 92), (8_409_860, 40), (8_460_646, 16), (8_511_017, 58), (8_572_056, 99), (8_618_670, 90),
    (8_693_261, 86), (8_762_355, 103), (8_825_550, 70), (8_884_341, 36), (8_938_751, 59), (9_003_827, 76),
    (9_046_671, 35), (9_111_229, 108), (9_155_237, 82), (9_210_610, 116),
)


def _le(h: str | None, signed: bool = False) -> int:
    return 0 if h is None else int.from_bytes(bytes.fromhex(h[2:]), "little", signed=signed)


def _ladder_snapshot(archive, make_subnet, make_snapshot, block: int):
    """Snapshot at `block` with exactly the NetworksAdded netuids, their NetworkRegisteredAt and SubnetMovingPrice
    (I96F32), and the block's NetworkImmunityPeriod - everything the ladder reads."""
    at = archive.block_hash(block)
    items = ("NetworksAdded", "NetworkRegisteredAt", "SubnetMovingPrice")
    keys = {(it, n): archive.netuid_key(SM + it, n) for it in items for n in range(MAX_NETUID + 1)}
    imm_key = archive.key(SM + "NetworkImmunityPeriod")
    vals = archive.query([*keys.values(), imm_key], at)
    immunity = _le(vals[imm_key])
    assert immunity > 0, f"NetworkImmunityPeriod absent at {block}"
    subs = []
    for n in range(1, MAX_NETUID + 1):
        if _le(vals[keys[("NetworksAdded", n)]]) != 1:
            continue
        ema = EXACT.divide(Decimal(_le(vals[keys[("SubnetMovingPrice", n)]], signed=True)), Decimal(2**32))
        subs.append(make_subnet(n, reg_at=_le(vals[keys[("NetworkRegisteredAt", n)]]), moving_price=ema))
    assert _le(vals[keys[("NetworksAdded", MAX_NETUID)]]) == 0                       # the range covered every netuid
    return make_snapshot(block, subs, immunity_period=immunity), at


def test_prune_rule_52_of_52(archive, make_subnet, make_snapshot) -> None:
    assert len(PRUNE_LOG) == 52
    misses = []
    for p, victim in PRUNE_LOG:
        snap, _ = _ladder_snapshot(archive, make_subnet, make_snapshot, p - 1)
        lad = ladder(snap)
        got = int(lad[0].netuid) if lad else None
        if got != victim:
            misses.append((p, victim, got))
    assert misses == [], f"prune rule missed: {misses}"


def test_live_prune_target_sn58_at_8500160(archive, make_subnet, make_snapshot) -> None:
    snap, at = _ladder_snapshot(archive, make_subnet, make_snapshot, 8_500_160)
    assert int(ladder(snap)[0].netuid) == 58
    try:
        raw = archive.runtime("SubnetInfoRuntimeApi_get_subnet_to_prune", "0x", at)
    except RuntimeError:
        return                                                                         # API absent at this spec
    b = bytes.fromhex(raw[2:])
    assert b[0] == 1 and int.from_bytes(b[1:3], "little") == 58


def test_first_half_tao_block(archive) -> None:
    key = archive.key(SM + "TotalIssuance")
    before = _le(archive.query([key], archive.block_hash(7_103_974))[key])
    after = _le(archive.query([key], archive.block_hash(7_103_975))[key])
    assert before < 10_500_000 * 10**9 <= after                                       # crossed inside 7,103,975
    assert block_emission_for_issuance(before) == 10**9                                # block 7,103,975 still 1 TAO
    assert block_emission_for_issuance(after) == 5 * 10**8                             # 7,103,976: the first 0.5-TAO block


def test_sn47_legacy_recovery_ratio(archive, make_subnet, make_pool, make_globals) -> None:
    """Brief 4.5: SN47 (legacy, registered before TaoInRefundDeploymentBlock) recovers ~0.36x spot, with the brief's
    escrow E = 25,516 alpha (escrow decoding is WP4's)."""
    at = archive.block_hash(9_240_388)
    items = ("SubnetTAO", "SubnetAlphaIn", "SubnetAlphaOut", "SubnetProtocolAlpha", "TotalAlphaStaked",
             "NetworkRegisteredAt")
    keys = {it: archive.netuid_key(SM + it, 47) for it in items}
    for it in ("Swap.SwapBalancer", "Swap.BalancerTaoReservoir", "Swap.BalancerAlphaReservoir"):
        keys[it] = archive.netuid_key(it, 47)
    refund_key = archive.key(SM + "TaoInRefundDeploymentBlock")
    v = archive.query([*keys.values(), refund_key], at)
    tao, alpha = _le(v[keys["SubnetTAO"]]), _le(v[keys["SubnetAlphaIn"]])
    bal = v[keys["Swap.SwapBalancer"]]
    w_q = _le("0x" + bal[2:18]) if bal else 5 * 10**17
    glob = make_globals(tao_in_refund_block=Block(_le(v[refund_key])))
    tas = v[keys["TotalAlphaStaked"]]
    s = make_subnet(47, reg_at=_le(v[keys["NetworkRegisteredAt"]]),
                    pool=make_pool(tao, alpha, w_quote_e18=w_q), alpha_out=AlphaRao(_le(v[keys["SubnetAlphaOut"]])),
                    protocol_alpha=AlphaRao(_le(v[keys["SubnetProtocolAlpha"]])),
                    total_alpha_staked=AlphaRao(_le(tas)) if tas else None,
                    reservoir_tao=Rao(_le(v[keys["Swap.BalancerTaoReservoir"]])),
                    reservoir_alpha=AlphaRao(_le(v[keys["Swap.BalancerAlphaReservoir"]])),
                    escrow_alpha=AlphaRao(25_516 * 10**9))
    assert s.key.reg_at <= glob.tao_in_refund_block                                    # legacy rule
    r = recovery_ratio(s, glob, Decimal("0.35"))
    assert abs(r - Decimal("0.36")) < Decimal("0.02"), r


def test_emission_replica_parity_at_a_recent_block(archive, gsnap, dec) -> None:
    """Per-subnet |E_model(n-1) - (SubnetTaoInEmission + SubnetExcessTao)(n)| <= 1e-6 TAO/block at a recent block."""
    template = gsnap("sn51_emission_9240382", 0)
    n = archive.finalized_head() - 20
    if n % 360 == 0:
        n -= 1
    prev = dec.build_snapshot(archive.golden_like(template, n - 1))
    cur = dec.build_snapshot(archive.golden_like(template, n))
    assert cur.glob.spec_version >= 475 and len(cur.subnets) >= 100
    model = emission_vector(prev, refresh_theta=False)
    errs = {int(c.key.netuid): abs((model[c.key].tao_per_block if c.key in model else 0) - (c.tao_in_emission + c.excess_tao))
            for c in cur.subnets}
    worst = max(errs.values())
    assert worst <= 1_000, f"block {n} spec {cur.glob.spec_version}: worst {worst} rao/block at {max(errs, key=errs.__getitem__)}"
    rel = parity_rel_errors([(7_200 * model[c.key].tao_per_block, 7_200 * (c.tao_in_emission + c.excess_tao))
                             for c in cur.subnets if c.key in model])
    assert parity_ok(rel)
