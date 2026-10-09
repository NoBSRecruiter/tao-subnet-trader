"""Preflight and the V2/V3/V6 risk-exit checks (DESIGN.md 9.3, 9.5, 10.5)."""
from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any

import pytest

from taotrader.core.errors import GateError
from taotrader.core.events import HealthObs
from taotrader.live.preflight import PreflightReport, RiskExitChecks, require_preflight, run_preflight, tao_to_rao


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def pf(lk: ModuleType, k: Any, *, held: Any = None, health: HealthObs | None = None, snap: Any = None,
       clean: bool = True) -> PreflightReport:
    k.hold(lk.SA, lk.SN, 1_000 * lk.TAO)
    hh = [(lk.KEY, lk.HK_A)] if held is None else held
    out: PreflightReport = run(run_preflight(k.sdk, live=k.live, risk=k.risk, snap=snap or k.snap(),
                                             health=health or HealthObs.nominal(), held=hh, recon_clean=clean))
    return out


def failed(report: PreflightReport) -> set[str]:
    return {c.name for c in report.checks if not c.ok}


def test_a_clean_staking_only_setup_passes(lk: ModuleType) -> None:
    r = pf(lk, lk.Kit())
    assert r.ok and r.spec_accepted, r.failures
    names = {c.name for c in r.checks}
    assert {"proxy_set", "sdk_version", "fee_float", "min_free_real", "plan_buy", "plan_partial_sell", "plan_full_exit",
            "plan_move", "v6_indices", "reconciliation", "locks[92]", "real_pays_fee[ops0]"} <= names
    require_preflight(r)


@pytest.mark.parametrize("ptype", ["Any", "NonTransfer", "Transfer", "Governance"])
def test_any_other_proxy_type_for_an_ops_delegate_fails(lk: ModuleType, ptype: str) -> None:
    k = lk.Kit()
    k.sdk.proxy_list.append((lk.D1, ptype, 0))
    r = pf(lk, k)
    assert failed(r) == {"proxy_set"} and ptype in r.failures[0]
    with pytest.raises(GateError, match="proxy_set"):
        require_preflight(r)


def test_missing_or_delayed_staking_proxy_fails_but_foreign_proxies_are_ignored(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.proxy_list = [p for p in k.sdk.proxy_list if p[0] != lk.D2]
    assert failed(pf(lk, k)) == {"proxy_set"}
    k2 = lk.Kit()
    k2.sdk.proxy_list = [(d, t, 5 if d == lk.D0 else 0) for d, t, _ in k2.sdk.proxy_list]
    assert failed(pf(lk, k2)) == {"proxy_set"}
    k3 = lk.Kit()
    k3.sdk.proxy_list.append((lk.ss58_encode("0x" + "ff" * 32), "Any", 0))   # the user's own hardware-wallet proxy
    assert pf(lk, k3).ok


def test_real_pays_fee_locks_and_balances(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.pays_fee[lk.D1] = True
    assert failed(pf(lk, k)) == {"real_pays_fee[ops1]"}
    k2 = lk.Kit()
    k2.sdk.locks[lk.SN] = 5
    assert failed(pf(lk, k2)) == {"locks[92]"}
    k3 = lk.Kit()
    k3.sdk.locks[lk.SN2] = 5                                                 # not held: irrelevant
    assert pf(lk, k3).ok
    k4 = lk.Kit()
    k4.sdk.set_account(0, lk.D2, free=tao_to_rao(0.5) + 1)
    assert failed(pf(lk, k4)) == {"delegate_balance[ops2]"}
    k5 = lk.Kit()
    for d in (lk.D0, lk.D1, lk.D2):
        k5.sdk.set_account(0, d, free=40_000_000)                             # 0.12 TAO < min_fee_float 0.15
    assert failed(pf(lk, k5)) == {"fee_float"}
    k6 = lk.Kit()
    k6.sdk.set_account(0, lk.REAL, free=1_000)
    assert failed(pf(lk, k6)) == {"min_free_real"}


def test_delegate_equal_to_real_fails(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.delegate_addr["ops1"] = lk.REAL
    k.sdk.proxy_list.append((lk.REAL, "Staking", 0))
    assert "delegate_not_real[ops1]" in failed(pf(lk, k))


def test_version_health_and_safe_mode(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.version = "11.2.0"
    assert failed(pf(lk, k)) == {"sdk_version"}
    assert failed(pf(lk, lk.Kit(), health=HealthObs(3, 12, 2, 2, 0))) == {"head_lag"}
    assert failed(pf(lk, lk.Kit(), health=HealthObs(6, 12, 2, 0, 0))) == {"finality_lag"}
    k2 = lk.Kit()
    assert failed(pf(lk, k2, snap=k2.snap(safe_mode_until=lk.B0 + 5))) == {"safe_mode"}
    r = pf(lk, lk.Kit(), snap=lk.Kit().snap(spec_version=476))
    assert r.ok and not r.spec_accepted                                      # the gate decides UNARMED
    assert failed(pf(lk, lk.Kit(), clean=False)) == {"reconciliation"}


def test_plan_shapes_and_v6(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.plan_violations = ["policy: x"]
    assert failed(pf(lk, k)) == {"plan_buy", "plan_partial_sell", "plan_full_exit", "plan_move"}
    k2 = lk.Kit()
    k2.sdk.plan_fee_rao = 6_000_000
    assert "plan_buy" in failed(pf(lk, k2))
    k3 = lk.Kit()
    k3.sdk.indices["SubtensorModule.remove_stake_limit"] = 91
    assert failed(pf(lk, k3)) == {"v6_indices"}
    k4 = lk.Kit()
    calls = []
    orig = k4.sdk.plan

    async def spy(call: Any, delegate: str) -> Any:
        calls.append(call)
        return await orig(call, delegate)

    k4.sdk.plan = spy
    assert pf(lk, k4).ok
    kinds = [(c.kind.value, c.max_spend_tao, c.amount) for c in calls]
    assert kinds[0][0] == "add_stake_limit" and kinds[0][1] == k4.live.max_order_tao
    assert all(spend is None for kind, spend, _ in kinds[1:]) and all(isinstance(a, int) for _, _, a in kinds)


def test_checks_fail_closed_on_exceptions(lk: ModuleType) -> None:
    k = lk.Kit()
    k.sdk.fail["proxies"] = ConnectionError("rpc down")
    r = pf(lk, k)
    assert "proxy_set" in failed(r) and "rpc down" in " ".join(r.failures)
    k2 = lk.Kit()
    k2.sdk.fail["real_pays_fee"] = KeyError("RealPaysFee")                   # storage name not found: VERIFY item
    assert "real_pays_fee" in failed(pf(lk, k2))


# ------------------------------------------------------------------------------------------------- V2 / V3 / V6
def test_risk_exit_checks(lk: ModuleType) -> None:
    k = lk.Kit()
    chk = RiskExitChecks(k.reader, k.sdk)
    res = run(chk.run(k.snap()))
    assert res.ok and res.v2 and res.v3 and res.v6, res.detail
    k.reader.sim_bias = 10_000_000                                            # ~1e-5: a weight drift or superellipse pool
    assert run(chk.ok(k.snap())) is True                                      # cached for this spec
    assert run(chk.ok(k.snap(lk.B0 + 7_200))) is False                        # refreshed: V2 fails
    k.reader.sim_bias = 0
    k.reader.prune = lk.SN2
    r3 = run(RiskExitChecks(k.reader, k.sdk).run(k.snap()))
    assert not r3.v3 and r3.v2 and "V3" in r3.detail
    k.reader.prune = None
    k.sdk.indices["ProxyType.Staking"] = 9
    r6 = run(RiskExitChecks(k.reader, k.sdk).run(k.snap(spec_version=476)))
    assert not r6.v6 and not r6.ok
    k.sdk.indices["ProxyType.Staking"] = 8
    k.sdk.fail["metadata_indices"] = RuntimeError("no metadata")
    r7 = run(RiskExitChecks(k.reader, k.sdk).run(k.snap()))
    assert not r7.v6 and "could not run" in r7.detail
