"""Adversarial-review regression tests for risk/overlay.py (WP8). Each test failed before its fix."""
from __future__ import annotations

from types import ModuleType

from taotrader.core.config import RiskCfg
from taotrader.core.orders import Urgency
from taotrader.core.units import Ppm
from taotrader.risk.liquidity import holdings
from taotrader.risk.overlay import StandardOverlay

TAO = 10**9


def test_forced_exits_are_applied_before_the_section_i_aggregates(kit: ModuleType) -> None:
    """DESIGN 3.1 fixed rule order: prune (3) and emission (4) exits come before the section I aggregates (7). The
    overlay zeroed forced-exit targets only when building the result, AFTER apply_aggregates, so the gross cap (and the
    buckets / exit budget) still counted a position that is being force-sold this tick and cut a healthy holding to
    make room: an unnecessary NORMAL sale (impact + fees, and a re-buy later)."""
    snap = kit.big_market()
    healthy = kit.key(25)
    small_pool = kit.subnet(25, tao=1_000 * TAO)                    # higher exit shortfall: cut first by the gross rule
    subs = [s if s.key != healthy else small_pool for s in snap.subnets]
    snap = kit.snapshot(subnets=subs, subnet_limit=30, n_nonroot_networks=30)
    target = kit.key(1)                                             # the prune target: EMERGENCY exit
    pos = [kit.position(target, kit.vhk(1), 7_000 * TAO),        # ~14 TAO at 0.002
           kit.position(healthy, kit.vhk(25), 280 * TAO)]        # ~14 TAO at 0.05 on a 1,000-TAO pool
    pf = kit.portfolio(cash=50 * TAO, positions=pos)
    t = kit.tick(snap, portfolio=pf)
    cur = {k: int(h.value) for k, h in holdings(t).items()}
    cfg = RiskCfg(g_max_ppm=Ppm(200_000))                           # gross cap ~15.6 TAO: binds only with the target held
    assert cur[healthy] < int(t.nav_liq) * 200_000 // 1_000_000 < cur[healthy] + cur[target]
    proposal = kit.target_book([kit.target(target, kit.vhk(1), cur[target]), kit.target(healthy, kit.vhk(25), cur[healthy])])
    d = StandardOverlay().review(proposal, kit.risk_ctx(t, cfg=cfg))
    fe = {f.key: f for f in d.targets.forced}
    assert fe[target].urgency is Urgency.EMERGENCY
    assert d.targets.get(target).value_rao == 0                    # type: ignore[union-attr]
    assert d.targets.get(healthy).value_rao == cur[healthy]        # type: ignore[union-attr]   # no collateral cut
    assert not any(a.rule == "liquidity.gross" for a in d.actions)
