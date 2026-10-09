"""Gate (DESIGN.md 9.3, 10.5): the four locks, the HMAC arm token, config-edit invalidation, the UNARMED start rule,
explicit network, LIVE_ELIGIBLE sleeves, runtime arming and the UNARMED alerts."""
from __future__ import annotations

import itertools
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from taotrader.core.config import RiskCfg, RpcCfg, RunCfg
from taotrader.core.errors import GateError
from taotrader.core.units import RunMode, Stage
from taotrader.live.gate import (
    ARM_ENV,
    CONFIRM_ENV,
    MAX_ARM_TTL_S,
    Arming,
    ArmingMonitor,
    GateDecision,
    LiveState,
    TokenStatus,
    arm_mac,
    check_arm_token,
    evaluate_gate,
    explicit_live_keys,
    ladder_exposure,
    make_arm_token,
    new_arm_secret,
)
from taotrader.ops import config_load

SECRET = b"0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000
REPO = Path(__file__).resolve().parents[2]


def run_cfg(lk: ModuleType, *, stage: Stage = Stage.LIVE_ELIGIBLE, **live_over: Any) -> RunCfg:
    return RunCfg(run_id="live", mode=RunMode.LIVE, books=(lk.book_cfg(stage),), rpc=RpcCfg((), ()),
                  live=lk.live_cfg(**live_over))


def gate(cfg: RunCfg, *, live: bool = True, submit: bool = True, env: dict[str, str] | None = None,
         secret: bytes | None = SECRET, spec: int = 475, keys: frozenset[str] = frozenset({"network"}),
         now: int = NOW, chash: str | None = None) -> GateDecision:
    h = config_load.config_hash(cfg) if chash is None else chash
    if env is None:
        env = {ARM_ENV: make_arm_token(SECRET, config_load.config_hash(cfg), NOW + 3_600, now_unix=NOW)}
    return evaluate_gate(cfg, config_hash=h, cli_live=live, cli_submit=submit, env=env, secret=secret, now_unix=now,
                         spec_version=spec, explicit_keys=keys)


# ------------------------------------------------------------------------------------------------- the four locks
def test_all_four_locks_arm(lk: ModuleType) -> None:
    d = gate(run_cfg(lk))
    assert d.state is LiveState.ARMED and d.submit and d.token_expiry == NOW + 3_600 and d.reasons == ()


@pytest.mark.parametrize("locks", [c for c in itertools.product((True, False), repeat=4) if not all(c)])
def test_every_combination_short_of_all_four_never_submits(lk: ModuleType, locks: tuple[bool, ...]) -> None:
    enabled, confirmed, cli_submit, armed = locks
    cfg = run_cfg(lk, enabled=enabled, mode="submit" if confirmed else "plan_only")
    good = make_arm_token(SECRET, config_load.config_hash(cfg), NOW + 3_600, now_unix=NOW)
    env = {ARM_ENV: good} if armed else {}
    if enabled and not cli_submit:
        assert gate(cfg, submit=False, env=env).state is LiveState.PLAN_ONLY      # lock 1 + --live: plan-only
        return
    with pytest.raises(GateError):
        gate(cfg, submit=cli_submit, env=env)


def test_plan_only_needs_lock_1_and_live(lk: ModuleType) -> None:
    cfg = run_cfg(lk, mode="plan_only")
    assert gate(cfg, submit=False, env={}).state is LiveState.PLAN_ONLY
    with pytest.raises(GateError, match="--live"):
        gate(cfg, live=False, submit=False, env={})
    with pytest.raises(GateError, match="lock 1"):
        gate(run_cfg(lk, enabled=False), submit=False, env={})


def test_any_config_edit_invalidates_the_arm_token(lk: ModuleType) -> None:
    cfg = run_cfg(lk)
    token = make_arm_token(SECRET, config_load.config_hash(cfg), NOW + 3_600, now_unix=NOW)
    edited = replace(cfg, live=replace(cfg.live, max_order_tao=1.01))
    assert config_load.config_hash(edited) != config_load.config_hash(cfg)
    with pytest.raises(GateError, match="forged"):
        gate(edited, env={ARM_ENV: token})


def test_token_checks(lk: ModuleType) -> None:
    h = "ab" * 32
    tok = make_arm_token(SECRET, h, NOW + 100, now_unix=NOW)
    assert check_arm_token(tok, SECRET, h, NOW) == (TokenStatus.VALID, NOW + 100)
    assert check_arm_token(tok, SECRET, h, NOW + 100)[0] is TokenStatus.EXPIRED
    assert check_arm_token(tok, b"another-secret-0123456789", h, NOW)[0] is TokenStatus.FORGED
    assert check_arm_token(tok, SECRET, "cd" * 32, NOW)[0] is TokenStatus.FORGED
    assert check_arm_token(None, SECRET, h, NOW)[0] is TokenStatus.MISSING
    assert check_arm_token(tok, None, h, NOW)[0] is TokenStatus.NO_SECRET
    assert check_arm_token("abc", SECRET, h, NOW)[0] is TokenStatus.MALFORMED
    far = NOW + MAX_ARM_TTL_S + 10
    assert check_arm_token(f"{far}:{arm_mac(SECRET, h, far)}", SECRET, h, NOW)[0] is TokenStatus.TOO_LONG
    with pytest.raises(GateError):
        make_arm_token(SECRET, h, NOW + MAX_ARM_TTL_S + 1, now_unix=NOW)
    with pytest.raises(GateError):
        make_arm_token(b"short", h, NOW + 10, now_unix=NOW)
    assert len(new_arm_secret()) == 64


def test_expired_token_refuses_unless_risk_exits_when_unarmed(lk: ModuleType) -> None:
    cfg = run_cfg(lk)
    expired = make_arm_token(SECRET, config_load.config_hash(cfg), NOW + 60, now_unix=NOW)
    with pytest.raises(GateError, match="risk_exits_when_unarmed"):
        gate(cfg, env={ARM_ENV: expired}, now=NOW + 61)
    on = run_cfg(lk, risk_exits_when_unarmed=True)
    tok = make_arm_token(SECRET, config_load.config_hash(on), NOW + 60, now_unix=NOW)
    d = gate(on, env={ARM_ENV: tok}, now=NOW + 61)
    assert d.state is LiveState.UNARMED and d.reasons == ("arm token expired",)
    forged = make_arm_token(b"x" * 32, config_load.config_hash(on), NOW + 60, now_unix=NOW)
    with pytest.raises(GateError):                                              # never for an inauthentic token
        gate(on, env={ARM_ENV: forged}, now=NOW + 61)


def test_spec_not_accepted_is_unarmed_only_with_the_flag(lk: ModuleType) -> None:
    with pytest.raises(GateError, match="accepted_specs"):
        gate(run_cfg(lk), spec=476)
    d = gate(run_cfg(lk, risk_exits_when_unarmed=True), spec=476)
    assert d.state is LiveState.UNARMED and "476" in d.reasons[0]


def test_lock_2_needs_explicit_network_eligible_sleeves_and_identities(lk: ModuleType) -> None:
    with pytest.raises(GateError, match="network must be set explicitly"):
        gate(run_cfg(lk), keys=frozenset())
    with pytest.raises(GateError, match="LIVE_ELIGIBLE"):
        gate(run_cfg(lk, stage=Stage.PAPER))
    with pytest.raises(GateError, match="no book defines"):
        gate(run_cfg(lk, sleeves=("momentum",)))
    with pytest.raises(GateError, match="SS58"):
        gate(run_cfg(lk, real_coldkey_ss58="not-an-address"))
    with pytest.raises(GateError, match="delegate_wallets"):
        gate(run_cfg(lk, delegate_wallets=()))
    with pytest.raises(GateError, match="real coldkey"):
        gate(run_cfg(lk, delegate_wallets=(lk.REAL,)))


def test_mainnet_needs_the_network_confirmation(lk: ModuleType) -> None:
    cfg = run_cfg(lk, network="finney")
    tok = make_arm_token(SECRET, config_load.config_hash(cfg), NOW + 3_600, now_unix=NOW)
    with pytest.raises(GateError, match=CONFIRM_ENV):
        gate(cfg, env={ARM_ENV: tok})
    with pytest.raises(GateError, match=CONFIRM_ENV):
        gate(cfg, env={ARM_ENV: tok, CONFIRM_ENV: "test"})
    assert gate(cfg, env={ARM_ENV: tok, CONFIRM_ENV: "finney"}).state is LiveState.ARMED


def test_gate_error_lists_every_missing_lock(lk: ModuleType) -> None:
    with pytest.raises(GateError) as e:
        gate(run_cfg(lk, mode="plan_only", network="finney"), env={}, keys=frozenset())
    msg = str(e.value)
    assert "lock 2" in msg and "network" in msg and CONFIRM_ENV in msg and ARM_ENV in msg


def test_explicit_live_keys(tmp_path: Path) -> None:
    example = REPO / "config" / "live.example.toml"
    assert "network" in explicit_live_keys([config_load.DEFAULT_CONFIG, example])
    assert explicit_live_keys([config_load.DEFAULT_CONFIG]) == frozenset()          # default.toml never counts
    assert explicit_live_keys([], env={"TAOTRADER_CFG_LIVE__NETWORK": "finney"}) == {"network"}
    assert explicit_live_keys([], cli=["live.network=finney"]) == {"network"}
    p = tmp_path / "x.toml"
    p.write_text("[live]\nenabled = true\n", encoding="utf-8")
    assert explicit_live_keys([p]) == {"enabled"}


# ------------------------------------------------------------------------------------------------- runtime arming
def test_arming_state_follows_the_clock_and_the_spec(lk: ModuleType) -> None:
    clock = [NOW]
    live = lk.live_cfg()
    a = Arming(GateDecision(LiveState.ARMED, (), NOW + 100, "test", "h"), live, lambda: clock[0])
    assert a.state(475) is LiveState.ARMED and a.seconds_to_expiry() == 100
    assert a.state(476) is LiveState.UNARMED
    clock[0] = NOW + 100
    assert a.state(475) is LiveState.UNARMED
    p = Arming(GateDecision(LiveState.PLAN_ONLY, (), None, "test", "h"), live, lambda: clock[0])
    assert p.plan_only and p.state(476) is LiveState.PLAN_ONLY and p.seconds_to_expiry() is None


def test_unarmed_alerts_fire_with_ladder_exposure_when_the_flag_is_off(lk: ModuleType) -> None:
    clock = [NOW]
    live = lk.live_cfg()
    a = Arming(GateDecision(LiveState.ARMED, (), NOW + 3 * 3_600, "test", "h"), live, lambda: clock[0])
    alerts: list[tuple[str, str]] = []
    m = ArmingMonitor(a, RiskCfg(), lambda kind, msg: alerts.append((kind, msg)))
    snap = lk.snapshot(lk.B0)
    exposure = ladder_exposure(snap, [lk.KEY], RiskCfg())
    assert exposure and exposure[0][0] == lk.KEY and exposure[0][1] == 1 and exposure[0][2] == 0
    assert m.check(snap, [lk.KEY]) == []                                      # 3 h left: nothing yet
    clock[0] = NOW + 3_600 + 1                                                # T - 2 h
    (msg,) = m.check(snap, [lk.KEY])
    assert "expires" in msg and f"SN{lk.SN}" in msg and "rank 1" in msg and "t*=0 blocks" in msg
    assert m.check(snap, [lk.KEY]) == []                                      # once per token
    changed = lk.snapshot(lk.B0 + 1, spec_version=476)
    msgs = m.check(changed, [lk.KEY])
    assert any("spec changed to 476" in x for x in msgs) and alerts and alerts[-1][0] == "live_unarmed"
    assert m.check(lk.snapshot(lk.B0 + 2, spec_version=477), []) == []        # no exposure: no alert
    on = Arming(GateDecision(LiveState.ARMED, (), NOW + 60, "test", "h"), lk.live_cfg(risk_exits_when_unarmed=True),
                lambda: clock[0])
    m2 = ArmingMonitor(on, RiskCfg())
    m2.check(snap, [lk.KEY])
    assert m2.check(changed, [lk.KEY]) == []                                  # the flag on: exits still possible


def test_ladder_exposure_ignores_names_outside_rank_15(lk: ModuleType) -> None:
    subs = [lk.subnet(n, 7_000_000 + n, lk.DEFAULT_POOLS[lk.SN], moving_price=str(0.001 * n)) for n in range(1, 20)]
    snap = lk.snapshot(lk.B0, subs)
    keys = [s.key for s in subs]
    got = ladder_exposure(snap, keys, RiskCfg())
    assert [int(k.netuid) for k, _, _ in got] == list(range(1, 16))
