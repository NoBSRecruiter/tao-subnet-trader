"""Linux CI contract test against the REAL bittensor 11.3.0 (DESIGN.md 9.4, 10.5; section 13 Q4). Skipped wherever
bittensor is not importable (native Windows). Offline: it introspects the installed package and evaluates
Policy.check() client-side; it never connects, signs or submits anything.

It fails if the pin or the intent / Policy / Client signatures LiveVenue relies on change, and it asserts that
Policy.check() accepts the exact RemoveStakeLimit (partial and full-exit amounts) and MoveStake objects RealSdk builds
under the per-call sell/move Policy (max_spend_tao=None), while the same intents with 'all' are rejected under the buy
Policy (why live never sends 'all')."""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from taotrader.core.config import LiveCfg
from taotrader.core.orders import OrderKind
from taotrader.live.sdk_port import SDK_VERSION, LiveCall, RealSdk, ss58_encode

bt = pytest.importorskip("bittensor", reason="bittensor 11.3.0 is installed only on the Linux live host / CI")

HK = ss58_encode("0x" + "a1" * 32)
DEST = ss58_encode("0x" + "b2" * 32)
REAL = ss58_encode("0x" + "aa" * 32)


def params(obj: Any) -> set[str]:
    target = obj.__init__ if inspect.isclass(obj) else obj
    return set(inspect.signature(target).parameters)


def sdk() -> RealSdk:
    return RealSdk(LiveCfg(enabled=True, mode="submit", real_coldkey_ss58=REAL, delegate_wallets=("ops0",),
                           max_order_tao=1.0, max_fee_tao=0.005), bt_module=bt)


def violations(policy: Any, intent: Any, fee_tao: float = 0.001) -> list[str]:
    """Evaluate Policy.check client-side whatever its exact shape (list of violations, or raising)."""
    check = policy.check
    kwargs: dict[str, Any] = {}
    sig = inspect.signature(check)
    for name in ("fee_tao", "fee", "estimated_fee"):
        if name in sig.parameters:
            kwargs[name] = bt.Balance.from_tao(fee_tao) if name == "fee" and hasattr(bt, "Balance") else fee_tao
    try:
        out = check(intent, **kwargs)
    except Exception as e:                                   # a raising check is a violation
        return [f"{type(e).__name__}: {e}"]
    if out is None or out is True:
        return []
    if out is False:
        return ["rejected"]
    return [str(v) for v in out]


def test_pin() -> None:
    assert bt.__version__ == SDK_VERSION


def test_intent_policy_and_client_signatures() -> None:
    assert {"hotkey_ss58", "netuid", "amount_tao", "limit_price_rao", "allow_partial"} <= params(bt.AddStakeLimit)
    assert {"hotkey_ss58", "netuid", "amount_alpha", "limit_price_rao", "allow_partial"} <= params(bt.RemoveStakeLimit)
    assert {"origin_hotkey_ss58", "origin_netuid", "dest_hotkey_ss58", "dest_netuid", "amount_alpha"} <= params(bt.MoveStake)
    assert {"max_fee_tao", "max_spend_tao", "allowed_netuids", "allow_raw_calls"} <= params(bt.Policy)
    assert hasattr(bt.Policy, "check")
    client = bt.Client
    assert {"policy", "proxy_for", "proxy_type"} <= params(client.plan)
    assert {"policy", "proxy_for", "proxy_type", "period", "wait_for_inclusion"} <= params(client.submit_shielded)
    assert {"policy", "proxy_for", "proxy_type", "period", "wait_for_inclusion"} <= params(client.execute)
    prices = [c for c in vars(bt).values() if inspect.isclass(c) and hasattr(c, "quote_stake")]
    assert prices or hasattr(client, "prices"), "client.prices.quote_stake is gone"


def test_policy_accepts_the_exact_sell_and_move_intents_live_builds() -> None:
    s = sdk()
    calls = [
        LiveCall(OrderKind.REMOVE_STAKE_LIMIT, HK, 92, 1_234_567_890, 1_000_000, False, None, None, (92,)),        # partial
        LiveCall(OrderKind.REMOVE_STAKE_LIMIT, HK, 92, 987_654_321_000_000, 1_000_000, True, None, None, (92,)),   # full exit
        LiveCall(OrderKind.MOVE_STAKE, HK, 92, 987_654_321_000_000, 0, False, DEST, None, (92,)),
    ]
    for call in calls:
        intent = s._intent(call)
        assert violations(s._policy(call), intent) == [], call
    buy = LiveCall(OrderKind.ADD_STAKE_LIMIT, HK, 92, 500_000_000, 1_400_000, False, None, 1.0, None)
    assert violations(s._policy(buy), s._intent(buy)) == []


def test_all_is_rejected_under_the_buy_policy() -> None:
    s = sdk()
    buy_policy = bt.Policy(max_fee_tao=0.005, max_spend_tao=1.0, allowed_netuids=None, allow_raw_calls=False)
    for intent in (bt.RemoveStakeLimit(hotkey_ss58=HK, netuid=92, amount_alpha="all", limit_price_rao=1_000_000),
                   bt.MoveStake(HK, 92, DEST, 92, "all"),
                   bt.AddStakeLimit(hotkey_ss58=HK, netuid=92, amount_tao="all", limit_price_rao=1_400_000)):
        assert violations(buy_policy, intent), intent
    assert s.sdk_version() == SDK_VERSION
