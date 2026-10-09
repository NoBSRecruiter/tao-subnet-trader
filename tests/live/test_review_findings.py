"""Adversarial-review regressions for WP11 (each test failed before its fix; DESIGN.md 9.3-9.8, brief 5.9, 6.4).

1. Buy turnover counted the order being submitted twice (its own SubmitStarted plus call.amount), so a buy was refused
   at about half the remaining max_daily_turnover_tao headroom (9.6 step 2, 9.8 #12).
2. RealSdk passed positional event attributes through as {"args": [...]}. Subtensor's stake events are positional
   (brief: StakeAdded/StakeRemoved(cold, hot, tao, alpha, netuid, fee), StakeMoved(cold, hot_o, netuid_o, hot_d,
   netuid_d, tao)), so account_events never matched the coldkey: the foreign-StakeMoved key alarm (9.7) was silently
   off on the live host, and same-block free-TAO apportioning read 0 TAO from StakeAdded/StakeRemoved.
3. Free-TAO apportioning (9.6 step 4) only looked at acked SHIELDED orders, so an unshielded risk exit (or an order
   resolved after SubmitUnknown) landing in the same block left the other fill with the whole block's free-TAO change.
4. A full exit (or move) that took the last shares of a hotkey (hk_total_shares == 0 at N+2) was reported as
   OrderFailed("success_without_share_delta") although the chain executed it: a phantom position, a false key alarm.
5. V2 (sim_swap parity) passed when no probe could be compared at all, which would admit UNARMED risk exits on a spec
   whose AMM was never checked (9.3).
6. A venue that had not yet observed any SnapshotObserved skipped the head-lag and finality-lag checks (fail open).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Any

from taotrader.core.config import LiveCfg
from taotrader.core.events import FillReported, OrderFailed, VenueAck
from taotrader.core.orders import FailReason, Fill, OrderKind, Urgency
from taotrader.core.units import AlphaRao, Block, OrderId, Ppm, PriceRao, Rao
from taotrader.live.preflight import RiskExitChecks
from taotrader.live.sdk_port import RealSdk, ss58_decode


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def buy(lk: ModuleType, tao: int, **kw: Any) -> Any:
    return lk.intent(OrderKind.ADD_STAKE_LIMIT, tao_in=tao, **kw)


def buy_fill(lk: ModuleType, tao: int, block: int, n: int) -> FillReported:
    return FillReported(Fill(fill_id=f"x{n}:0:0", order_id=OrderId(f"{n:024x}"), attempt=0, book=lk.BOOK,
                             block=Block(block), kind=OrderKind.ADD_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, tao=Rao(tao),
                             alpha=AlphaRao(1), shares=Decimal(1), swap_fee=0, author_fee_tao=Rao(0), tx_fee=Rao(0),
                             d_pool_tao=0, d_pool_alpha=0, spot_before=PriceRao(1), shortfall_ppm=Ppm(0), complete=True))


# ------------------------------------------------------------------------------------------------- 1. turnover
def test_buy_turnover_counts_the_order_being_submitted_once(lk: ModuleType) -> None:
    k = lk.Kit(live=lk.live_cfg(max_daily_turnover_tao=5.0))
    for i in range(7):
        k.feed(buy_fill(lk, lk.TAO * 6 // 10, lk.B0 - 100 + i, i))           # 4.2 TAO bought in the window
    ev = run(k.place(buy(lk, lk.TAO // 2)))                                   # 4.2 + 0.5 = 4.7 <= 5.0
    assert isinstance(ev, VenueAck), ev
    other = buy(lk, lk.TAO // 2, key=lk.KEY2, hotkey=lk.HK_B)                 # in flight 0.5 + 4.2 + 0.5 = 5.2 > 5
    ev2 = run(k.place(other))
    assert isinstance(ev2, OrderFailed) and ev2.detail.startswith("cap:max_daily_turnover_tao"), ev2


# ------------------------------------------------------------------------------------------------- 2. RealSdk events
def _real_sdk(lk: ModuleType, events: list[dict[str, Any]], extrinsics: list[dict[str, Any]]) -> RealSdk:
    async def get_events(block_hash: str) -> list[dict[str, Any]]:
        return events

    async def get_block(block_hash: str) -> dict[str, Any]:
        return {"extrinsics": extrinsics}

    bt = SimpleNamespace(__version__="11.3.0", Client=lambda **kw: SimpleNamespace(),
                         Wallet=lambda name: SimpleNamespace(coldkeypub=SimpleNamespace(ss58_address=lk.D0)))
    sdk = RealSdk(LiveCfg(enabled=True, real_coldkey_ss58=lk.REAL, delegate_wallets=("ops0",)), bt_module=bt)
    run(sdk.connect())
    sdk.client.substrate = SimpleNamespace(get_events=get_events, get_block=get_block)
    return sdk


def _ev(idx: int, name: str, attrs: Any, module: str = "SubtensorModule") -> dict[str, Any]:
    return {"phase": {"ApplyExtrinsic": idx}, "extrinsic_idx": idx,
            "event": {"module_id": module, "event_id": name, "attributes": attrs}}


def test_real_sdk_names_positional_stake_events_and_finds_the_coldkey(lk: ModuleType) -> None:
    real_hex = ss58_decode(lk.REAL)
    events = [
        _ev(0, "StakeMoved", (lk.REAL, lk.SA, 92, lk.SB, 92, 1_234)),                     # foreign move of our stake
        _ev(1, "StakeAdded", [real_hex, lk.SA, 500_000_000, 370_000_000_000, 92, 165_000]),  # hex AccountId form
        _ev(1, "TransactionFeePaid", {"who": lk.D0, "actual_fee": 933_081, "tip": 0}, "TransactionPayment"),
        _ev(2, "StakeRemoved", (lk.SC, lk.SA, 1, 2, 92, 0)),                                # someone else's coldkey
    ]
    exts = [{"extrinsic_hash": "0x" + "01" * 32, "address": None, "nonce": None},
            {"extrinsic_hash": "0x" + "02" * 32, "address": lk.D0, "nonce": 11},
            {"extrinsic_hash": "0x" + "03" * 32, "address": lk.SC, "nonce": 4}]
    sdk = _real_sdk(lk, events, exts)
    mine = run(sdk.account_events("0xblock", lk.REAL))
    names = [(n, f.get("extrinsic_hash")) for _, n, f in mine]
    assert ("StakeMoved", "0x" + "01" * 32) in names                         # the key-alarm source is visible
    assert ("StakeAdded", "0x" + "02" * 32) in names
    assert all(n != "StakeRemoved" for n, _ in names)                         # another coldkey's event is not ours
    moved = next(f for _, n, f in mine if n == "StakeMoved")
    assert moved["coldkey"] == lk.REAL and moved["tao"] == 1_234 and moved["dest_hotkey"] == lk.SB
    inner = run(sdk.extrinsic_events("0xblock", 1))
    added = next(f for _, n, f in inner if n == "StakeAdded")
    assert added["tao"] == 500_000_000 and added["alpha"] == 370_000_000_000 and added["netuid"] == 92
    assert next(f for _, n, f in inner if n == "TransactionFeePaid")["actual_fee"] == 933_081


# ------------------------------------------------------------------------------------------------- 3. apportioning
def test_shielded_buy_and_unshielded_exit_in_one_block_apportion_free_tao(lk: ModuleType) -> None:
    k = lk.Kit()
    k.hold(lk.SB, lk.SN2, 10_000 * lk.TAO)
    b_it = buy(lk, lk.TAO // 2)
    b_ack = run(k.place(b_it))
    assert isinstance(b_ack, VenueAck)
    s_it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, key=lk.KEY2, hotkey=lk.HK_B, alpha_in=2_000 * lk.TAO,
                     urgency=Urgency.URGENT, partial=True, shielded=False)
    s_ack = run(k.place(s_it))
    assert isinstance(s_ack, VenueAck) and k.sdk.submitted[-1][0] == "submit_plain"
    b = int(b_ack.expected_fill_block)
    free0 = k.sdk.free_at(b - 1, lk.REAL)
    k.land_buy(b_ack, b_it)
    amount = 2_000 * lk.TAO
    tao_out = lk.quote_sell(lk.DEFAULT_POOLS[lk.SN2], AlphaRao(amount)).amount_out
    k.sdk.set_stake(b, lk.SB, lk.SN2, shares=Decimal(8_000 * lk.TAO), hk_alpha=lk.HK_ALPHA0 + 8_000 * lk.TAO,
                    hk_shares=Decimal(lk.HK_ALPHA0 + 8_000 * lk.TAO))
    k.sdk.include_plain(b, s_ack.carrier_hash, lk.D1, int(k.started(s_it).nonce), events=[
        ("Proxy", "ProxyExecuted", {"result": "Ok"}),
        ("SubtensorModule", "StakeRemoved", {"coldkey": lk.REAL, "hotkey": lk.SB, "tao": tao_out, "alpha": amount,
                                             "netuid": lk.SN2}),
        ("System", "ExtrinsicSuccess", {})])
    k.sdk.set_account(b, lk.REAL, free=free0 - lk.TAO // 2 + tao_out)
    k.set_head(b)
    evs = run(k.drain(b))
    fills = {e.fill.kind: e.fill for e in evs if isinstance(e, FillReported)}
    assert fills[OrderKind.ADD_STAKE_LIMIT].tao == lk.TAO // 2
    assert fills[OrderKind.REMOVE_STAKE_LIMIT].tao == tao_out


# ------------------------------------------------------------------------------------------------- 4. last staker
def test_full_exit_of_the_last_shares_on_a_hotkey_is_a_fill(lk: ModuleType) -> None:
    k = lk.Kit()
    held = 3_000 * lk.TAO
    k.sdk.set_stake(0, lk.SC, lk.SN, shares=Decimal(held), hk_alpha=held, hk_shares=Decimal(held))   # sole staker
    it = lk.intent(OrderKind.REMOVE_STAKE_LIMIT, hotkey=lk.HK_C, full=True, urgency=Urgency.URGENT, partial=True)
    ack = run(k.place(it))
    assert isinstance(ack, VenueAck) and k.sdk.submitted[-1][1].amount == held
    b = int(ack.expected_fill_block)
    tao_out = lk.quote_sell(lk.DEFAULT_POOLS[lk.SN], AlphaRao(held)).amount_out
    k.sdk.set_stake(b, lk.SC, lk.SN, shares=Decimal(0), hk_alpha=0, hk_shares=Decimal(0))
    k.sdk.set_account(b, lk.REAL, free=k.sdk.free_at(b - 1, lk.REAL) + tao_out)
    k.sdk.include_shielded(b, ack.carrier_hash, ack.inner_hash, lk.D0, int(k.started(it).nonce), inner_events=[
        ("Proxy", "ProxyExecuted", {"result": "Ok"}),
        ("SubtensorModule", "StakeRemoved", {"coldkey": lk.REAL, "hotkey": lk.SC, "tao": tao_out, "alpha": held,
                                             "netuid": lk.SN}),
        ("System", "ExtrinsicSuccess", {})])
    k.set_head(b)
    (ev,) = run(k.drain(b))
    assert isinstance(ev, FillReported), ev
    f = ev.fill
    assert f.shares == Decimal(held) and f.alpha == held and f.tao == tao_out and f.complete


# ------------------------------------------------------------------------------------------------- 5. V2 vacuous
def test_v2_fails_closed_when_no_probe_could_be_compared(lk: ModuleType) -> None:
    k = lk.Kit()
    tiny = lk.pool(tao=1_000, alpha=10**12)                                   # every probe size is infeasible locally
    snap = lk.snapshot(lk.B0, [lk.subnet(lk.SN, lk.REG, tiny), lk.subnet(lk.SN2, lk.REG2, tiny)])
    res = run(RiskExitChecks(k.reader, k.sdk).run(snap))
    assert not res.v2 and not res.ok, res.detail
    assert "V2" in res.detail


# ------------------------------------------------------------------------------------------------- 6. no health yet
def test_submit_without_any_observed_health_is_refused(lk: ModuleType) -> None:
    k = lk.Kit()
    k.venue.health = None                                                     # nothing observed yet (fresh venue)
    ev = run(k.place(buy(lk, lk.TAO // 2)))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.VENUE_REJECT and ev.detail == "no_health", ev
    assert k.sdk.submitted == []
