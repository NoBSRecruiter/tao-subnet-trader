"""SdkPort seam (DESIGN.md 9.4): ss58, LiveCall refusals ('all', u64::MAX, call 103, MOVE_STAKE_LIMIT), per-call
policies, error mapping, FakeSdkPort, RealSdk wiring against a stub module, and import isolation from bittensor."""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from taotrader.core.config import LiveCfg
from taotrader.core.orders import FailReason, OrderKind
from taotrader.core.units import U64_MAX
from taotrader.live.sdk_port import (
    EXPECTED_INDICES,
    FakeSdkPort,
    LiveCall,
    LiveCallError,
    PolicySpec,
    RealSdk,
    SdkResultError,
    check_policy,
    error_reason,
    intent_spec,
    policy_for,
    ss58_decode,
    ss58_encode,
)

ALICE_HEX = "0xd43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d"
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
REPO = Path(__file__).resolve().parents[2]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def sell(amount: int = 10**12, **kw: Any) -> LiveCall:
    base: dict[str, Any] = {"kind": OrderKind.REMOVE_STAKE_LIMIT, "hotkey_ss58": ALICE, "netuid": 92, "amount": amount,
                            "limit_price_rao": 1_000_000, "allow_partial": False, "dest_hotkey_ss58": None,
                            "max_spend_tao": None, "allowed_netuids": (92,)}
    base.update(kw)
    return LiveCall(**base)


def buy(amount: int = 500_000_000, **kw: Any) -> LiveCall:
    base: dict[str, Any] = {"kind": OrderKind.ADD_STAKE_LIMIT, "hotkey_ss58": ALICE, "netuid": 92, "amount": amount,
                            "limit_price_rao": 1_400_000, "allow_partial": False, "dest_hotkey_ss58": None,
                            "max_spend_tao": 1.0, "allowed_netuids": None}
    base.update(kw)
    return LiveCall(**base)


# ------------------------------------------------------------------------------------------------- ss58
def test_ss58_known_vector_and_round_trip() -> None:
    assert ss58_encode(ALICE_HEX) == ALICE
    assert ss58_decode(ALICE) == ALICE_HEX
    for i in (0, 1, 2**255, 2**256 - 1):
        h = "0x" + f"{i:064x}"
        assert ss58_decode(ss58_encode(h)) == h
    with pytest.raises(ValueError, match="checksum"):
        ss58_decode(ALICE[:-1] + ("Z" if ALICE[-1] != "Z" else "Y"))
    with pytest.raises(ValueError):
        ss58_decode(ss58_encode(ALICE_HEX, 0))                                # another network format
    with pytest.raises(ValueError):
        ss58_encode("0x1234")


# ------------------------------------------------------------------------------------------------- LiveCall
@pytest.mark.parametrize("bad", [
    {"kind": OrderKind.REMOVE_STAKE_FULL_LIMIT},
    {"kind": OrderKind.MOVE_STAKE_LIMIT},
    {"amount": U64_MAX},
    {"amount": U64_MAX + 5},
    {"amount": 0},
    {"amount": -1},
    {"amount": True},
    {"amount": "all"},
    {"amount": 1.5},
    {"limit_price_rao": 0},
    {"max_spend_tao": 1.0},
    {"netuid": 0},
])
def test_live_call_refuses_what_live_never_sends(bad: dict[str, Any]) -> None:
    with pytest.raises(LiveCallError):
        sell(**bad)


def test_live_call_kind_specific_rules() -> None:
    with pytest.raises(LiveCallError):
        buy(max_spend_tao=None)                                               # buys always run bounded
    with pytest.raises(LiveCallError):
        buy(dest_hotkey_ss58=ALICE)
    with pytest.raises(LiveCallError):
        sell(kind=OrderKind.MOVE_STAKE, limit_price_rao=5, dest_hotkey_ss58="x")
    with pytest.raises(LiveCallError):
        sell(kind=OrderKind.MOVE_STAKE, limit_price_rao=0, dest_hotkey_ss58=ALICE)   # same hotkey
    mv = sell(kind=OrderKind.MOVE_STAKE, limit_price_rao=0, dest_hotkey_ss58=ss58_encode("0x" + "11" * 32))
    assert mv.kind is OrderKind.MOVE_STAKE and not mv.is_buy


def test_per_call_policies() -> None:
    b, s = buy(), sell(amount=10**15)
    assert policy_for(b, 0.005) == PolicySpec(0.005, 1.0, None, False)
    assert policy_for(s, 0.005) == PolicySpec(0.005, None, (92,), False)
    assert check_policy(policy_for(s, 0.005), s, 837_000) == []              # exact huge exit, no spend cap
    assert check_policy(policy_for(b, 0.005), replace(b, amount=2 * 10**9), 1_028_000)[0].startswith("policy: spend")
    assert check_policy(policy_for(b, 0.005), b, None) == ["policy: fee estimate unavailable with max_fee_tao set"]
    assert any("fee" in v for v in check_policy(policy_for(b, 0.001), b, 1_028_000))
    assert any("netuid" in v for v in check_policy(policy_for(s, 0.005), replace(s, netuid=7), 0))


def test_error_reason_mapping() -> None:
    assert error_reason("SlippageTooHigh", proxied=True) is FailReason.SLIPPAGE_TOO_HIGH
    assert error_reason("SubtensorModule.NotEnoughStakeToWithdraw", proxied=True) is FailReason.NOT_ENOUGH_STAKE
    assert error_reason("Swap::PriceLimitExceeded", proxied=False) is FailReason.PRICE_LIMIT_EXCEEDED
    assert error_reason("CallFiltered", proxied=False) is FailReason.CALL_FILTERED
    assert error_reason("Mystery", proxied=True) is FailReason.PROXY_ERROR
    assert error_reason("Mystery", proxied=False) is FailReason.OTHER
    assert error_reason("ShieldMissed", proxied=False) is FailReason.OTHER        # venue-only reasons never map


def test_intent_spec_names_only_three_intents_with_exact_amounts() -> None:
    assert intent_spec(buy())[0] == "AddStakeLimit" and intent_spec(buy())[1]["amount_tao"] == 500_000_000
    name, kw = intent_spec(sell(amount=123_456_789_012))
    assert name == "RemoveStakeLimit" and kw["amount_alpha"] == 123_456_789_012 and kw["limit_price_rao"] == 1_000_000
    mv = sell(kind=OrderKind.MOVE_STAKE, limit_price_rao=0, dest_hotkey_ss58=ss58_encode("0x" + "11" * 32))
    name, kw = intent_spec(mv)
    assert name == "MoveStake" and kw["origin_netuid"] == kw["dest_netuid"] == 92


def test_fake_sdk_forbid_writes_and_timelines() -> None:
    f = FakeSdkPort(real=ALICE, delegates={"ops0": ss58_encode("0x" + "d0" * 32)})
    f.set_account(10, ALICE, free=5)
    f.set_account(20, ALICE, free=7)
    assert (f.free_at(9, ALICE), f.free_at(10, ALICE), f.free_at(19, ALICE), f.free_at(25, ALICE)) == (0, 5, 5, 7)
    f.forbid_writes = True
    with pytest.raises(RuntimeError, match="forbidden"):
        run(f.submit_shielded(buy(), "ops0"))
    assert run(f.metadata_indices()) == dict(EXPECTED_INDICES)


# ------------------------------------------------------------------------------------------------- RealSdk wiring
class _Bal:
    def __init__(self, rao: int) -> None:
        self.rao = rao


class _Client:
    def __init__(self, recorder: list[tuple[str, Any, dict[str, Any]]], result: Any) -> None:
        self.rec = recorder
        self.result = result
        self.prices = SimpleNamespace(alpha_price=self._price, quote_stake=self._qs, quote_unstake=self._qu)

    async def _price(self, netuid: int) -> dict[str, int]:
        return {"price_rao": 1_349_885, "netuid": netuid}

    async def _qs(self, netuid: int, amount: Any) -> Any:
        self.rec.append(("quote_stake", amount, {}))
        return SimpleNamespace(alpha=_Bal(370_000_000_000), tao=_Bal(0))

    async def _qu(self, netuid: int, amount: Any) -> Any:
        self.rec.append(("quote_unstake", amount, {}))
        return SimpleNamespace(tao=_Bal(1_300_000_000), alpha=_Bal(0))

    async def plan(self, intent: Any, wallet: Any, **kw: Any) -> Any:
        self.rec.append(("plan", intent, kw))
        return SimpleNamespace(violations=[], fee=_Bal(1_028_000))

    async def submit_shielded(self, intent: Any, wallet: Any, **kw: Any) -> Any:
        self.rec.append(("submit_shielded", intent, kw))
        return self.result

    async def execute(self, intent: Any, wallet: Any, **kw: Any) -> Any:
        self.rec.append(("execute", intent, kw))
        return self.result


def _stub_bt(rec: list[tuple[str, Any, dict[str, Any]]], result: Any) -> SimpleNamespace:
    def intent(name: str) -> Any:
        return lambda **kw: (name, kw)

    return SimpleNamespace(
        __version__="11.3.0", rao=lambda n, netuid=0: ("rao", n, netuid),
        Policy=lambda **kw: ("Policy", kw), AddStakeLimit=intent("AddStakeLimit"),
        RemoveStakeLimit=intent("RemoveStakeLimit"), MoveStake=intent("MoveStake"),
        Wallet=lambda name: SimpleNamespace(name=name, coldkeypub=SimpleNamespace(ss58_address=f"ss58-of-{name}")),
        Client=lambda **kw: _Client(rec, result))


def _live() -> LiveCfg:
    return LiveCfg(enabled=True, mode="submit", real_coldkey_ss58=ALICE, delegate_wallets=("ops0",), max_order_tao=1.0)


def test_real_sdk_builds_exact_intents_under_per_call_policies() -> None:
    rec: list[tuple[str, Any, dict[str, Any]]] = []
    result = SimpleNamespace(extrinsic_hash="0xcar", data={"inner_extrinsic_hash": "0xinn", "submit_block": 77, "nonce": 9})
    sdk = RealSdk(_live(), bt_module=_stub_bt(rec, result))
    run(sdk.connect())
    assert sdk.sdk_version() == "11.3.0" and sdk.delegate_ss58("ops0") == "ss58-of-ops0"
    v, fee = run(sdk.plan(buy(), "ops0"))
    assert v == [] and fee == 1_028_000
    _, intent, kw = rec[-1]
    assert intent == ("AddStakeLimit", {"hotkey_ss58": ALICE, "netuid": 92, "amount_tao": ("rao", 500_000_000, 0),
                                        "limit_price_rao": 1_400_000, "allow_partial": False})
    assert kw["policy"] == ("Policy", {"max_fee_tao": 0.005, "max_spend_tao": 1.0, "allowed_netuids": None,
                                       "allow_raw_calls": False})
    assert kw["proxy_for"] == ALICE and kw["proxy_type"] == "Staking"
    assert run(sdk.submit_shielded(sell(amount=10**15), "ops0")) == ("0xcar", "0xinn", 77, 9)
    _, intent, kw = rec[-1]
    assert intent[0] == "RemoveStakeLimit" and intent[1]["amount_alpha"] == ("rao", 10**15, 92)
    assert kw["policy"][1]["max_spend_tao"] is None and kw["policy"][1]["allowed_netuids"] == [92]
    assert kw["period"] == 8 and kw["wait_for_inclusion"] is False and kw["proxy_type"] == "Staking"
    assert run(sdk.quote(buy())) == (370_000_000_000, 1_349_885)
    assert run(sdk.quote(sell())) == (1_300_000_000, 1_349_885)
    assert run(sdk.submit_plain(sell(), "ops0")) == ("0xcar", 77, 9)
    assert rec[-1][0] == "execute" and rec[-1][2]["period"] == 16 and rec[-1][2]["retries"] == 0
    with pytest.raises(ValueError):
        run(sdk.plan(buy(), "not-a-delegate"))
    intents = [i for name, i, _ in rec if name in ("plan", "submit_shielded", "execute")]
    assert intents and all("all" not in i[1].values() and U64_MAX not in i[1].values() for i in intents)


def test_real_sdk_without_a_carrier_nonce_raises_for_resolve() -> None:
    rec: list[tuple[str, Any, dict[str, Any]]] = []
    sdk = RealSdk(_live(), bt_module=_stub_bt(rec, SimpleNamespace(extrinsic_hash="0xcar", data={})))
    run(sdk.connect())
    with pytest.raises(SdkResultError):
        run(sdk.submit_shielded(buy(), "ops0"))


# ------------------------------------------------------------------------------------------------- isolation
def test_importing_taotrader_live_never_imports_bittensor() -> None:
    code = ("import sys, taotrader.live, taotrader.live.gate, taotrader.live.preflight, taotrader.live.sdk_port, "
            "taotrader.live.venue, taotrader.live.reconcile, taotrader.live.nonce; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'bittensor'); print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(importlib.util.find_spec("bittensor") is not None, reason="bittensor is installed here")
def test_real_sdk_needs_bittensor_where_it_is_absent() -> None:
    with pytest.raises(ImportError):
        RealSdk(_live())


# ------------------------------------------------------------------------------------------------- section 13 Q19
def test_q19_storage_items_exist_in_the_spec_475_metadata() -> None:
    import json

    from taotrader.chain.hashing import prefix, to_hex
    from taotrader.live.sdk_port import COLDKEY_SWAP_ITEMS, REAL_PAYS_FEE_ITEM, coldkey_swap_keys, real_pays_fee_keys

    golden = REPO / "tests" / "fixtures" / "golden"
    md475 = json.loads((golden / "metadata_storage_spec475.json").read_text(encoding="utf-8"))["pallets"]
    md348 = json.loads((golden / "metadata_storage_spec348.json").read_text(encoding="utf-8"))["pallets"]
    rp = md475[REAL_PAYS_FEE_ITEM[0]][REAL_PAYS_FEE_ITEM[1]]
    assert rp["modifier"] == "Optional" and rp["hashers"] == ["Twox64Concat", "Twox64Concat"] and rp["value"] == "()"
    for pallet, item in COLDKEY_SWAP_ITEMS[:2]:
        row = md475[pallet][item]
        assert row["modifier"] == "Optional" and row["hashers"] == ["Twox64Concat"] and row["key"] == "AccountId32"
    assert "ColdkeySwapScheduled" not in md475["SubtensorModule"] and "ColdkeySwapScheduled" in md348["SubtensorModule"]
    for item in ("Proxies", "Announcements"):
        assert md475["Proxy"][item]["hashers"] == ["Twox64Concat"]
    a, b = real_pays_fee_keys(ALICE, ss58_encode("0x" + "d0" * 32))
    pre = to_hex(prefix(*REAL_PAYS_FEE_ITEM))
    assert a.startswith(pre) and b.startswith(pre) and a != b and len(a) == len(b) == 2 + 2 * (32 + 2 * (8 + 32))
    assert ALICE_HEX[2:] in a and "d0" * 32 in a
    keys = coldkey_swap_keys(ALICE)
    assert len(keys) == 3 and all(ALICE_HEX[2:] in k for k in keys)


def test_real_sdk_reads_q19_items_by_key_presence() -> None:
    from taotrader.live.sdk_port import coldkey_swap_keys, real_pays_fee_keys

    present: set[str] = set()

    async def rpc_request(method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "state_getStorage"
        return {"result": "0x" if params[0] in present else None}

    rec: list[tuple[str, Any, dict[str, Any]]] = []
    sdk = RealSdk(_live(), bt_module=_stub_bt(rec, None))
    run(sdk.connect())
    sdk.client.substrate = SimpleNamespace(rpc_request=rpc_request)
    delegate = ss58_encode("0x" + "d0" * 32)
    assert run(sdk.real_pays_fee(ALICE, delegate)) is False
    present.add(real_pays_fee_keys(ALICE, delegate)[1])                      # either key order counts (fail closed)
    assert run(sdk.real_pays_fee(ALICE, delegate)) is True
    assert run(sdk.coldkey_swap_scheduled(ALICE)) is False
    present.add(coldkey_swap_keys(ALICE)[0])
    assert run(sdk.coldkey_swap_scheduled(ALICE)) is True
