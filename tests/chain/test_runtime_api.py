"""chain.runtime_api: state_call encoders/decoders against the captured runtime results (section 6.6, 10.1)."""
from __future__ import annotations

from typing import Any

import pytest

from taotrader.chain import runtime_api as rt
from taotrader.core.errors import DecodeError
from taotrader.core.protocols import SwapSim


def _rt(golden: Any, name: str, method: str, args: dict[str, Any] | None = None, snap: int = 0) -> dict[str, Any]:
    for r in golden(name)["snapshots"][snap]["runtime_api"]:
        if r["method"] == method and (args is None or r["args"] == args):
            return dict(r)
    raise KeyError(method)


def test_args_encoding_matches_capture(golden: Any) -> None:
    for name in ("sn92_9240388", "erab_7000020", "sn1_quote_9240388"):
        for r in golden(name)["snapshots"][0]["runtime_api"]:
            if r["method"] in (rt.M_SIM_BUY, rt.M_SIM_SELL, rt.M_PRICE, rt.M_NEXT_EPOCH, rt.M_PRUNE, rt.M_PRICE_ALL):
                assert rt.method_args(r["method"], r["args"]) == r["args_hex"], r
    assert rt.args_account(rt.ESCROW_ACCOUNT) == rt.ESCROW_ACCOUNT
    with pytest.raises(ValueError):
        rt.args_netuid_amount(1, 2**64)


def test_escrow_account_constant(golden: Any) -> None:
    r = _rt(golden, "escrow_9240388", rt.M_STAKE_INFO_COLDKEY)
    assert r["args"]["coldkey"] == rt.ESCROW_ACCOUNT
    assert bytes.fromhex(rt.ESCROW_ACCOUNT[2:]).rstrip(b"\x00") == b"modlsubtensrbeta/esc"


def test_price_all_layout(golden: Any) -> None:
    prices = rt.dec_price_all(_rt(golden, "sn92_9240388", rt.M_PRICE_ALL)["result"])
    assert len(prices) == 129                                  # 128 subnets + root
    assert prices[0] == 10**9                                  # root priced at 1.0
    assert prices[1] == 6_562_800
    assert prices[92] == rt.dec_u64(_rt(golden, "sn92_9240388", rt.M_PRICE)["result"]) == 1_348_210
    with pytest.raises(DecodeError):
        rt.dec_price_all("0x0400000000")                       # 1 entry announced, 4 bytes present
    with pytest.raises(DecodeError):
        rt.dec_price_all("0x08" + "0100" + "00" * 8 + "0100" + "00" * 8)    # duplicate netuid


def test_sim_swap_48_bytes(golden: Any) -> None:
    s = rt.dec_sim_swap(_rt(golden, "sn92_9240388", rt.M_SIM_BUY, {"netuid": 92, "tao_rao": 10_000_000_000})["result"])
    assert s.tao_amount == 9_994_964_523 and s.tao_fee == 5_035_477
    assert s.alpha_amount == 7_289_425_629_146
    assert s.alpha_slippage == 127_811_335_036
    s1 = rt.dec_sim_swap(_rt(golden, "sn1_quote_9240388", rt.M_SIM_BUY)["result"])
    assert (s1.tao_amount, s1.tao_fee, s1.alpha_amount) == (999_496_453, 503_547, 152_290_647_774)
    assert not rt.sim_failed(s1)


def test_sim_swap_legacy_32_bytes(golden: Any) -> None:
    r = _rt(golden, "erab_7000020", rt.M_SIM_SELL, {"netuid": 1, "alpha_rao": 107_272_764_827})
    s = rt.dec_sim_swap(r["result"])
    assert len(r["result"]) == 2 + 64
    assert s.tao_amount == 999_453_341 and s.alpha_amount == 107_218_747_871 and s.alpha_fee == 54_016_956
    assert (s.tao_slippage, s.alpha_slippage) == (0, 0)


def test_sim_swap_failure_and_bad_length() -> None:
    assert rt.sim_failed(rt.dec_sim_swap("0x" + "00" * 48))
    assert rt.sim_failed(SwapSim(5, 0, 0, 0, 0, 0))
    with pytest.raises(DecodeError):
        rt.dec_sim_swap("0x" + "00" * 40)
    with pytest.raises(DecodeError):
        rt.dec_sim_swap("zz")


def test_options_and_scalars(golden: Any) -> None:
    assert rt.dec_prune_target(_rt(golden, "sn92_9240388", rt.M_PRUNE)["result"]) == 92
    assert rt.dec_prune_target("0x00") is None
    with pytest.raises(DecodeError):
        rt.dec_prune_target("0x5c00")                          # a bare u16 is not an Option<u16>
    assert rt.dec_next_epoch(_rt(golden, "sn92_9240388", rt.M_NEXT_EPOCH)["result"]) == 9_240_495
    assert rt.dec_next_epoch("0x00") is None
    assert rt.dec_u64(_rt(golden, "sn92_9240388", rt.M_BLOCK_EMISSION)["result"]) == 500_000_000   # u64, not Option
    assert rt.dec_u64(_rt(golden, "globals_9240878", rt.M_REG_COST)["result"]) == 962_887_016_780
    with pytest.raises(DecodeError):
        rt.dec_u64("0x01" + "00" * 8)


def test_escrow_stake_info_decodes_exactly(golden: Any) -> None:
    r = _rt(golden, "escrow_9240388", rt.M_STAKE_INFO_COLDKEY)
    rows = rt.dec_stake_info_vec(r["result"])
    assert len(rows) == 7_296                                    # parses to the last of 573,461 bytes
    assert all(x.coldkey == rt.ESCROW_ACCOUNT for x in rows)
    esc = rt.escrow_by_subnet(rows)
    assert 0 not in esc and len(esc) == 125
    sn92 = esc[92] / 10**9
    assert abs(sn92 - 49_063) / 49_063 < 0.01                    # brief 3.7: SN92 E = 49,063 alpha
    assert esc[92] == 48_922_040_753_313
    with pytest.raises(DecodeError):
        rt.dec_stake_info_vec(r["result"][:-2])                  # truncated: last bool missing
    with pytest.raises(DecodeError):
        rt.dec_stake_info_vec(r["result"] + "00")                # trailing byte


def test_escrow_rejects_foreign_rows(golden: Any) -> None:
    rows = rt.dec_stake_info_vec(_rt(golden, "escrow_9240388", rt.M_STAKE_INFO_COLDKEY)["result"])
    with pytest.raises(DecodeError):
        rt.escrow_by_subnet(rows[:3], escrow="0x" + "11" * 32)
