"""chain.reader: snapshot assembly on an in-memory node (DESIGN.md sections 6.4-6.8).

Membership (NetworksAdded), era-correct pools, Quality flags, HEAD carry, hotkey panel, owner position, validation
failures (all-or-nothing), block emission, historical pulls, cross-checks.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain.hashing import account, to_hex
from taotrader.chain.head import encode_compact
from taotrader.chain.metadata import Entry, SpecLayouts
from taotrader.chain.reader import (
    JsonRpcChainReader,
    SnapshotDecodeError,
    SpecCache,
    compare_prices,
    curve_block_emission,
    issuance_bracket,
    snapshot_digest,
)
from taotrader.chain.rpc import Role
from taotrader.chain.runtime_api import ESCROW_ACCOUNT, M_STAKE_INFO_COLDKEY
from taotrader.core.errors import DecodeError
from taotrader.core.fixed import EXACT
from taotrader.core.state import ChainSnapshot, PoolKind, Quality, ReadPlan
from taotrader.core.units import Block, BlockHash, Hotkey, NetUid, SubnetKey

B = 9_240_388
HK_A = "0x" + "a1" * 32
HK_B = "0x" + "b2" * 32
CK_O = "0x" + "c3" * 32
HK_O = "0x" + "d4" * 32


def snap(arun: Any, reader: JsonRpcChainReader, s: Any, plan: ReadPlan = ReadPlan.FULL, prev: ChainSnapshot | None = None,
         tracked: Any = (), **kw: Any) -> ChainSnapshot:
    return arun(reader.snapshot(Block(s.block), BlockHash(s.hash), plan, prev, tracked, **kw))  # type: ignore[no-any-return]


def queried_keys(chain: Any) -> list[str]:
    return [k for p in chain.calls("state_queryStorageAt") for k in p[0]]


# ------------------------------------------------------------------------------------------------ FULL assembly
def test_full_snapshot_decodes_every_field(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    le = enc["le"]
    s = state(chain, B)
    s.subnet(1, reg_at=100, tao=10_000 * 10**9, alpha_in=1_500_000 * 10**9, quote=600_000_000_000_000_000,
             alpha_out=le(2_000_000 * 10**9, 8), protocol_alpha=le(7 * 10**9, 8), tempo=le(99, 2),
             tao_flow_cum=le(-5, 8, True), volume_cum=le(2**70, 16), fee_rate=le(196, 2),
             consensus_mode=b"\x01", total_alpha_staked=le(123, 8), emission_enabled=b"\x00",
             fast_moving_price=enc["fixed"](1 << 63, 64), root_prop=enc["fixed"](1 << 31, 32),
             reservoir_tao=le(5, 8), reservoir_alpha=le(6, 8), owner_cut_autolock=b"\x01")
    s.subnet(92, reg_at=8_352_006, owner_hk=HK_O, owner_ck=CK_O, first_emission=None)
    s.hotkey(92, HK_O, total_alpha=1_000 * 10**9, shares=(5, 11), take=0, dividend=77)
    s.owner_position(92, HK_O, CK_O, v2=(1, 11))
    reader = make_reader(chain)
    out = snap(arun, reader, s)
    assert [int(x.key.netuid) for x in out.subnets] == [1, 92]
    a = out.by_netuid(1)
    assert a is not None and a.key == SubnetKey(NetUid(1), Block(100))
    assert a.pool.kind is PoolKind.BALANCER and a.pool.w_quote_e18 == 600_000_000_000_000_000 and a.pool.fee_rate == 196
    assert (a.pool.tao, a.pool.alpha, a.pool.px_tao, a.pool.px_alpha) == (10**13, 15 * 10**14, 10**13, 15 * 10**14)
    assert a.alpha_out == 2 * 10**15 and a.protocol_alpha == 7 * 10**9 and a.tempo == 99
    assert a.tao_flow_cum == -5 and a.volume_cum == 2**70 and a.consensus_mode == 1 and a.total_alpha_staked == 123
    assert a.emission_enabled is False and a.subtoken_enabled is True and a.reg_allowed is True
    assert a.fast_moving_price == Decimal("0.5") and a.root_prop == Decimal("0.5") and a.miner_burned == 0
    assert (a.reservoir_tao, a.reservoir_alpha) == (5, 6)
    assert a.owner_cut_enabled is True and a.owner_cut_autolock is True          # map default True / explicit True
    assert a.max_allowed_validators == 128 and a.ema_halving_blocks == 201_600  # spec-475 ValueQuery defaults
    assert a.moving_price == Decimal(5_834_416) / Decimal(2**32)
    assert a.first_emission_block == 8_000_600 and a.last_epoch_block == 820
    assert a.owner_hotkey is None and a.owner_alpha is None and a.hotkeys == ()
    assert a.quality == Quality.NO_YIELD_IDX
    b = out.by_netuid(92)
    assert b is not None and b.owner_hotkey == HK_O and b.owner_coldkey == CK_O
    assert b.pool.fee_rate == 33                                                   # FeeRate absent -> layout default
    assert b.consensus_mode == 0 and b.first_emission_block is None
    assert b.quality == Quality.NOT_STARTED
    hk = b.hotkey(Hotkey(HK_O))
    assert hk is not None and hk.total_shares == Decimal("5E11") and hk.index() == 2
    assert hk.take_u16 == 0 and hk.earns and hk.last_dividend == 77 and hk.childkey_take_u16 == 0
    assert b.owner_alpha == 2 * 10**11                                             # 1e11 shares x index 2
    g = out.glob
    assert g.spec_version == 475 and g.tx_version == 1
    assert g.moving_alpha == Decimal(1_288_490) / Decimal(2**32)
    assert g.gate_exponent == 3 and g.gate_rank == 32 and g.owner_cut_u16 == 11_796 and g.subnet_limit == 128
    assert g.tao_weight == EXACT.divide(Decimal(3_320_413_933_267_719_290), Decimal(2**64 - 1))
    assert abs(g.tao_weight - Decimal("0.18")) < Decimal("1e-18")
    assert g.last_reg_block == 9_210_610 and g.nominator_min_stake == 20_000_000 and g.cleanup_queue_len == 0
    assert g.n_nonroot_networks == 2 and g.safe_mode_until is None and g.shorts_enabled is False
    assert g.block_emission == 500_000_000 and g.runtime_prune_target is None
    assert out.timestamp_ms == 1_759_900_000_000 + 12_000 * B and out.plan is ReadPlan.FULL
    assert out.digest == snapshot_digest(out) and len(out.digest) == 32


def test_digest_is_deterministic_and_content_sensitive(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                       enc: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    d1 = snap(arun, make_reader(chain), s).digest
    d2 = snap(arun, make_reader(chain), s).digest
    assert d1 == d2
    s.subnet(1, reg_at=100, tao=587_199_047_951)
    assert snap(arun, make_reader(chain), s).digest != d1


def test_membership_is_networks_added(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    """A removed-in-cleanup netuid (NetworksAdded false, NetworkRegisteredAt and pool still set) and a queued-not-added
    netuid (no NetworksAdded key yet) are excluded from `subnets`; root never appears."""
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    s.subnet(5, reg_at=200, added=False)          # removed at P, cleanup pending
    s.subnet(6, reg_at=B - 3, added=None)         # registration queued, NetworkAdded follows 17-25 blocks later
    s.subnet(0, reg_at=0)                         # root
    s.subnet(7, reg_at=300)
    out = snap(arun, make_reader(chain), s)
    assert [int(x.key.netuid) for x in out.subnets] == [1, 7]
    assert out.glob.n_nonroot_networks == 2 and out.by_netuid(5) is None and out.by_netuid(6) is None


def test_netuid_range_extends_past_default(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    s.subnet(144, reg_at=101)
    s.subnet(150, reg_at=102)
    reader = make_reader(chain)
    out = snap(arun, reader, s)
    assert [int(x.key.netuid) for x in out.subnets] == [1, 144, 150]
    assert reader.max_netuid == 160
    s2 = state(chain, B + 1)
    s2.glob(subnet_limit=enc["le"](256, 2))
    s2.subnet(200, reg_at=103)
    out2 = snap(arun, make_reader(chain), s2)
    assert [int(x.key.netuid) for x in out2.subnets] == [200]


# ------------------------------------------------------------------------------------------------ eras
def test_era_b_virtual_reserves(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any, tmp_path: Path) -> None:
    le = enc["le"]
    base = SpecLayouts().exact(475)
    assert base is not None
    v3 = {"Swap.AlphaSqrtPrice": Entry("Default", ("Twox64Concat",), "NetUid", "FixedU128<frac_bits=64>", 16, "0x" + "00" * 16),
          "Swap.CurrentLiquidity": Entry("Default", ("Twox64Concat",), "NetUid", "u64", 8, "0x" + "00" * 8)}
    lay = dataclasses.replace(base, entries={**{k: v for k, v in base.entries.items() if k != "Swap.SwapBalancer"}, **v3})
    s = state(chain, 7_000_020)
    sqrt_raw = 1_781_045_380_923_513_843                     # SN1 at 7,000,020 (golden erab)
    liq = 39_321_840_000_000
    s.subnet(1, reg_at=100, quote=None, v3_sqrt_price=le(sqrt_raw, 16), v3_liquidity=le(liq, 8))
    s.subnet(2, reg_at=101, quote=None)                       # v3 not initialised: T/A pricing
    out = snap(arun, make_reader(chain, layouts=SpecLayouts(tmp_path, extra=[lay])), s)
    a, b = out.by_netuid(1), out.by_netuid(2)
    assert a is not None and b is not None
    assert a.pool.kind is PoolKind.CP_V3_VIRTUAL and a.pool.w_quote_e18 == 5 * 10**17
    assert a.pool.px_tao == liq * sqrt_raw >> 64 and a.pool.px_alpha == (liq << 64) // sqrt_raw
    assert a.pool.tao == 587_199_047_950                      # real reserves kept for caps
    assert not a.quality & Quality.TA_PRICE
    assert b.pool.kind is PoolKind.CP_REAL and b.quality & Quality.TA_PRICE


def test_era_a_real_reserves_and_tiny_pool(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 5_000_000, spec=200)                     # no layout at or below spec 200: unknown runtime
    s.subnet(3, reg_at=4_950_000, quote=None, tao=5 * 10**9, alpha_in=5 * 10**9)
    out = snap(arun, make_reader(chain), s)
    x = out.by_netuid(3)
    assert x is not None and x.pool.kind is PoolKind.CP_REAL
    assert x.quality & Quality.EARLY_TINY_POOL and x.quality & Quality.DEFAULT_FILLED
    assert not x.quality & Quality.TA_PRICE
    assert x.pool.fee_rate == 33                               # protocol.regimes.fee_rate_default, flagged


def test_seed_fallback_window(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s0 = state(chain, it.BALANCER_FIRST_BLOCK - 10)
    s0.subnet(1, reg_at=100, quote=None, tao=10**12, alpha_in=10**14)
    s0.subnet(2, reg_at=101, quote=None, tao=10**12, alpha_in=10**14)
    reader = make_reader(chain)
    prev = snap(arun, reader, s0)
    s1 = state(chain, it.BALANCER_FIRST_BLOCK + 5)
    s1.subnet(1, reg_at=100, quote=5 * 10**17, tao=10**12, alpha_in=10**14 // 2)   # price doubled at q = 0.5
    s1.subnet(2, reg_at=101, quote=5 * 10**17, tao=10**12, alpha_in=10**14)        # unchanged price
    cur = snap(arun, reader, s1, prev=prev)
    a, b = cur.by_netuid(1), cur.by_netuid(2)
    assert a is not None and b is not None
    assert a.quality & Quality.SEED_FALLBACK and a.quality & Quality.BALANCER_MIGRATION
    assert not b.quality & Quality.SEED_FALLBACK and b.quality & Quality.BALANCER_MIGRATION
    s2 = state(chain, it.BALANCER_FIRST_BLOCK + 200)
    s2.subnet(1, reg_at=100, quote=5 * 10**17)
    c = snap(arun, reader, s2).by_netuid(1)
    assert c is not None and not c.quality & (Quality.BALANCER_MIGRATION | Quality.SEED_FALLBACK)


# ------------------------------------------------------------------------------------------------ fail closed
@pytest.mark.parametrize("case", ["quote_low", "quote_high", "zero_tao", "number", "gate_exp", "bad_u64", "bad_bool"])
def test_invalid_snapshots_fail_whole(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any, case: str) -> None:
    le = enc["le"]
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    if case == "quote_low":
        s.subnet(2, reg_at=101, quote=10**15)
    elif case == "quote_high":
        s.subnet(2, reg_at=101, quote=995 * 10**15)
    elif case == "zero_tao":
        s.subnet(2, reg_at=101, tao=0)
    elif case == "number":
        s.glob(system_number=le(B + 1, 4))
    elif case == "gate_exp":
        s.glob(gate_exponent=le((3 << 64) + (1 << 62), 16))     # 3.25: not an int (ADR-0001 #2, fail closed)
    elif case == "bad_u64":
        s.subnet(2, reg_at=101, alpha_out=b"\x01\x02")
    elif case == "bad_bool":
        s.subnet(2, reg_at=101, added=None)
        s.put(it.SUBNET[it.NETWORKS_ADDED], b"\x02", netuid=2)
    raws: list[Any] = []
    reader = make_reader(chain, on_raw=lambda *a: raws.append(a))
    with pytest.raises(SnapshotDecodeError) as ei:
        snap(arun, reader, s)
    assert ei.value.block == B and ei.value.block_hash == s.hash and ei.value.raw
    assert raws == []


def test_integral_gate_exponent_decodes(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    s.glob(gate_exponent=enc["le"](4 << 64, 16), gate_rank=enc["le"](16, 2), owner_cut_u16=enc["le"](0, 2))
    g = snap(arun, make_reader(chain), s).glob
    assert (g.gate_exponent, g.gate_rank, g.owner_cut_u16) == (4, 16, 0)


def test_strict_query_response(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)

    def drop_one(method: str, p: list[Any], res: Any) -> Any:
        if method == "state_queryStorageAt":
            return [{"block": res[0]["block"], "changes": res[0]["changes"][1:]}]
        return res

    chain.mutate = drop_one
    with pytest.raises(SnapshotDecodeError, match="answered"):
        snap(arun, make_reader(chain), s)

    def wrong_block(method: str, p: list[Any], res: Any) -> Any:
        if method == "state_queryStorageAt":
            return [{"block": "0x" + "00" * 32, "changes": res[0]["changes"]}]
        return res

    chain.mutate = wrong_block
    with pytest.raises(SnapshotDecodeError, match="answered for"):
        snap(arun, make_reader(chain), s)


# ------------------------------------------------------------------------------------------------ defaults
def test_unknown_spec_is_default_filled(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B, spec=999)                       # newer than any committed layout: fallback, not exact
    s.subnet(1, reg_at=100)
    reader = make_reader(chain)
    out = snap(arun, reader, s)
    x = out.by_netuid(1)
    assert x is not None and x.quality & Quality.DEFAULT_FILLED
    assert out.glob.spec_version == 999
    assert not reader.layouts.accepted(999)
    s2 = state(chain, B + 1)
    s2.subnet(1, reg_at=100)
    y = snap(arun, reader, s2).by_netuid(1)
    assert y is not None and not y.quality & Quality.DEFAULT_FILLED


def test_item_missing_from_layout_uses_fallbacks(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                 tmp_path: Path) -> None:
    base = SpecLayouts().exact(475)
    assert base is not None
    drop = ("Swap.FeeRate", "SubtensorModule.SubnetEpochConsensus", "SubtensorModule.OwnerCutEnabled",
            "SubtensorModule.TotalAlphaStaked", "SubtensorModule.SubnetTaoFlow")
    lay = dataclasses.replace(base, entries={k: v for k, v in base.entries.items() if k not in drop})
    s = state(chain, B)
    s.subnet(1, reg_at=100, fee_rate=b"\x05\x00")
    reader = make_reader(chain, layouts=SpecLayouts(tmp_path, extra=[lay]))
    out = snap(arun, reader, s)
    x = out.by_netuid(1)
    assert x is not None
    assert x.pool.fee_rate == 33 and x.quality & Quality.DEFAULT_FILLED       # not read: absent from the runtime
    assert x.consensus_mode is None and x.owner_cut_enabled is None and x.total_alpha_staked is None
    assert x.tao_flow_cum is None
    assert to_hex(it.SUBNET["fee_rate"].key(netuid=1)) not in queried_keys(chain)


# ------------------------------------------------------------------------------------------------ HEAD plan
def test_head_plan_carries_full_fields(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    le = enc["le"]
    s0 = state(chain, B)
    s0.subnet(1, reg_at=100, tempo=le(360, 2))
    s0.subnet(2, reg_at=200)
    reader = make_reader(chain)
    full = snap(arun, reader, s0)
    s1 = s0.clone(B + 1)
    s1.subnet(1, reg_at=100, tao=999 * 10**9, tempo=le(77, 2), alpha_out=le(1, 8))   # FULL-only fields change too
    s1.subnet(2, reg_at=B + 1)                                                          # a new generation of netuid 2
    n0 = len(chain.calls("state_queryStorageAt"))
    head = snap(arun, reader, s1, plan=ReadPlan.HEAD, prev=full)
    calls = chain.calls("state_queryStorageAt")[n0:]
    a, b = head.by_netuid(1), head.by_netuid(2)
    assert a is not None and b is not None
    assert head.plan is ReadPlan.HEAD
    assert a.pool.tao == 999 * 10**9 and a.tempo == 360 and a.alpha_out == 630_980_000_000_000   # carried
    assert a.quality & Quality.CARRIED
    assert b.key.reg_at == B + 1 and not b.quality & Quality.CARRIED                    # read in full
    first = calls[0][0]
    assert len(calls) == 2 and len(first) <= 2_000
    alpha_out_1 = to_hex(it.SUBNET["alpha_out"].key(netuid=1))
    alpha_out_2 = to_hex(it.SUBNET["alpha_out"].key(netuid=2))
    assert alpha_out_1 not in first and alpha_out_1 not in calls[1][0] and alpha_out_2 in calls[1][0]
    assert head.digest != full.digest
    with pytest.raises(ValueError):
        arun(reader._finish(arun(reader._begin(Block(B + 1), BlockHash(s1.hash), ReadPlan.HEAD, True, (), Role.ARCHIVE)),
                            None, (), (), Role.ARCHIVE))


def test_head_reads_held_emission_rows(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    le = enc["le"]
    s0 = state(chain, B)
    s0.subnet(1, reg_at=100, tao_in_emission=le(10, 8))
    s0.subnet(2, reg_at=101, tao_in_emission=le(20, 8))
    reader = make_reader(chain)
    full = snap(arun, reader, s0)
    s1 = s0.clone(B + 1)
    s1.subnet(1, reg_at=100, tao_in_emission=le(11, 8))
    s1.subnet(2, reg_at=101, tao_in_emission=le(21, 8))
    head = snap(arun, reader, s1, plan=ReadPlan.HEAD, prev=full, held=[1])
    a, b = head.by_netuid(1), head.by_netuid(2)
    assert a is not None and b is not None and a.tao_in_emission == 11 and b.tao_in_emission == 20


def test_head_without_prev_reads_full(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    out = snap(arun, make_reader(chain), s, plan=ReadPlan.HEAD)
    assert out.plan is ReadPlan.FULL and out.subnets[0].quality == Quality.NO_YIELD_IDX


def test_hotkey_panel_rereads_only_on_epoch_drain(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                  enc: Any) -> None:
    le = enc["le"]
    s0 = state(chain, B)
    s0.subnet(7, reg_at=100, last_epoch_block=le(B - 5, 8))
    s0.hotkey(7, HK_A, total_alpha=1_000, shares=(1_000, 0), dividend=3)
    s0.hotkey(7, HK_B, total_alpha=50, shares=(50, 0))
    key = SubnetKey(NetUid(7), Block(100))
    tracked = [(key, Hotkey(HK_A)), (key, Hotkey(HK_B)), (SubnetKey(NetUid(7), Block(99)), Hotkey(HK_O))]
    reader = make_reader(chain)
    full = snap(arun, reader, s0, tracked=tracked)
    x = full.by_netuid(7)
    assert x is not None and [str(h.hotkey) for h in x.hotkeys] == sorted([HK_A, HK_B])   # stale generation ignored
    a = x.hotkey(Hotkey(HK_A))
    b_ = x.hotkey(Hotkey(HK_B))
    assert a is not None and b_ is not None and a.earns and a.last_dividend == 3 and not b_.earns
    assert a.take_u16 == 11_796                                                            # Delegates default
    assert not x.quality & Quality.NO_YIELD_IDX
    s1 = s0.clone(B + 1)
    s1.hotkey(7, HK_A, total_alpha=1_100, shares=(1_000, 0), dividend=3)                   # changed, no drain
    h1 = snap(arun, reader, s1, plan=ReadPlan.HEAD, prev=full, tracked=tracked)
    y = h1.by_netuid(7)
    assert y is not None and (ya := y.hotkey(Hotkey(HK_A))) is not None and ya.total_alpha == 1_000   # carried
    s2 = s1.clone(B + 2)
    s2.subnet(7, reg_at=100, last_epoch_block=le(B + 2, 8))                                # epoch drain
    h2 = snap(arun, reader, s2, plan=ReadPlan.HEAD, prev=h1, tracked=tracked)
    z = h2.by_netuid(7)
    assert z is not None and (za := z.hotkey(Hotkey(HK_A))) is not None and za.total_alpha == 1_100
    assert z.last_epoch_block == B + 2


def test_dividend_keys_paginate(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(7, reg_at=100)
    hks = ["0x" + f"{i:064x}" for i in range(1, 1_201)]
    for h_ in hks:
        s.put(it.HOTKEY["last_dividend"], b"\x01" + b"\x00" * 7, netuid=7, hotkey=h_)
    s.put(it.HOTKEY["last_dividend"], b"\x01" + b"\x00" * 7, netuid=8, hotkey=HK_A)
    got = arun(make_reader(chain).dividend_keys(NetUid(7), BlockHash(s.hash)))
    assert list(got) == sorted(hks) and len(chain.calls("state_getKeysPaged")) == 2


# ------------------------------------------------------------------------------------------------ globals extras
def test_block_emission_runtime_cached_and_curve(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    s2 = s.clone(B + 1)
    reader = make_reader(chain)
    snap(arun, reader, s)
    snap(arun, reader, s2)
    calls = [p for p in chain.calls("state_call") if p[0] == "SubnetInfoRuntimeApi_get_block_emission"]
    assert len(calls) == 1
    s3 = state(chain, 5_000_000, spec=300)                     # no runtime API there: exact curve fallback
    s3.subnet(1, reg_at=100, quote=None)
    chain.rt.pop(("SubnetInfoRuntimeApi_get_block_emission", "0x", s3.hash))
    assert snap(arun, reader, s3).glob.block_emission == 500_000_000      # issuance 11.6M TAO -> bracket 1
    assert issuance_bracket(0) == 0 and curve_block_emission(0) == 10**9
    assert curve_block_emission(10_500_000 * 10**9 - 1) == 10**9 and curve_block_emission(10_500_000 * 10**9) == 5 * 10**8
    assert curve_block_emission(15_750_000 * 10**9) == 25 * 10**7 and curve_block_emission(21 * 10**15) == 0
    assert issuance_bracket(21 * 10**15) is None


def test_crosschecks_prune_and_escrow(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 9_239_400)                 # divisible by 25 (prune check) and 360 (escrow)
    s.subnet(92, reg_at=8_352_006)
    s.subnet(5, reg_at=100)
    s.rt("SubnetInfoRuntimeApi_get_subnet_to_prune", "0x", b"\x01\x5c\x00")
    rows = b""
    for hk, net, stake in ((HK_A, 92, 1_000_000), (HK_B, 92, 234), (HK_A, 0, 5)):
        rows += (account(hk) + account(ESCROW_ACCOUNT) + encode_compact(net) + encode_compact(stake)
                 + b"".join(encode_compact(0) for _ in range(4)) + b"")
    s.rt(M_STAKE_INFO_COLDKEY, to_hex(account(ESCROW_ACCOUNT)), encode_compact(3) + rows)
    reader = make_reader(chain, prune_check_every=25, escrow_every=360)
    out = snap(arun, reader, s)
    assert out.glob.runtime_prune_target == 92
    a, b = out.by_netuid(92), out.by_netuid(5)
    assert a is not None and b is not None and a.escrow_alpha == 1_000_234 and b.escrow_alpha == 0
    s2 = s.clone(9_239_401)
    h = snap(arun, reader, s2, plan=ReadPlan.HEAD, prev=out)
    c = h.by_netuid(92)
    assert c is not None and c.escrow_alpha == a.escrow_alpha and h.glob.runtime_prune_target is None


def test_chain_stall_flag(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    lo, hi = it.CHAIN_STALL_BLOCKS
    s0 = state(chain, lo - 2)
    s0.subnet(1, reg_at=100, quote=None)
    reader = make_reader(chain)
    p = snap(arun, reader, s0)
    s1 = s0.clone(hi + 1)
    q = snap(arun, reader, s1, plan=ReadPlan.HEAD, prev=p).subnets[0].quality
    assert q & Quality.CHAIN_STALL_GAP
    s2 = state(chain, B)
    s2.subnet(1, reg_at=100)
    p2 = snap(arun, reader, s2)
    s3 = s2.clone(B + 10)
    s3.glob(timestamp_ms=enc["le"](p2.timestamp_ms + 3_600_000, 8))                # an hour for 10 blocks
    assert snap(arun, reader, s3, plan=ReadPlan.HEAD, prev=p2).subnets[0].quality & Quality.CHAIN_STALL_GAP
    s4 = s2.clone(B + 20)
    assert not snap(arun, reader, s4, plan=ReadPlan.HEAD, prev=p2).subnets[0].quality & Quality.CHAIN_STALL_GAP


def test_metagraph_lite(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    le = enc["le"]
    s = state(chain, B)
    s.subnet(9, reg_at=100)
    inc = [0, 30_000, 10_000, 20_000]
    s.put(it.METAGRAPH["incentive"], bytes([len(inc) << 2]) + b"".join(le(v, 2) for v in inc), netuid=9)
    s.put(it.METAGRAPH["validator_permit"], bytes([4 << 2]) + bytes([1, 0, 0, 1]), netuid=9)
    owners = {0: CK_O, 1: CK_O, 2: "0x" + "e5" * 32, 3: CK_O}
    for uid, ck in owners.items():
        hk = "0x" + f"{uid + 1:064x}"
        s.put(it.METAGRAPH["uid_hotkey"], account(hk), netuid=9, uid=uid)
        s.put(it.METAGRAPH["hotkey_owner"], account(ck), hotkey=hk)
    reader = make_reader(chain)
    out = snap(arun, reader, s, metagraph_for=[9, 77])
    m = out.subnets[0].metagraph
    assert m is not None and (m.n_miners, m.n_miner_coldkeys, m.n_permit_coldkeys) == (3, 2, 1)
    assert m.top1_coldkey_share_ppm == 50_000 * 1_000_000 // 60_000
    assert arun(reader.metagraph(NetUid(9), Block(B), BlockHash(s.hash))) == m
    assert snap(arun, make_reader(chain), s).subnets[0].metagraph is None


def test_owner_position_legacy_wins_where_alpha_exists(arun: Any, chain: Any, state: Any, make_reader: Any,
                                                       tmp_path: Path) -> None:
    """Spec 441 still has the legacy Alpha map and TotalHotkeyShares (V1): in the overlap the legacy values win."""
    lay = SpecLayouts().exact(441)
    if lay is None:
        pytest.skip("spec-441 layout not committed")
    s = state(chain, 8_900_000, spec=441)
    s.subnet(92, reg_at=100, owner_hk=HK_O, owner_ck=CK_O)
    s.hotkey(92, HK_O, total_alpha=1_000 * 10**9, shares=(5, 11), shares_v1=(250 * 10**9) << 64)
    s.owner_position(92, HK_O, CK_O, v2=(1, 11), legacy_raw=(100 * 10**9) << 64)
    x = snap(arun, make_reader(chain), s).by_netuid(92)
    assert x is not None and (hk := x.hotkey(Hotkey(HK_O))) is not None
    assert hk.total_shares == 250 * 10**9 and hk.index() == 4
    assert x.owner_alpha == 400 * 10**9


def test_legacy_epoch_marker_at_spec_348(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any) -> None:
    lay = SpecLayouts().exact(348)
    if lay is None:
        pytest.skip("spec-348 layout not committed")
    le = enc["le"]
    s = state(chain, 7_000_020, spec=348)
    s.subnet(1, reg_at=100, quote=None, last_epoch_block_legacy=le(6_999_999, 8))
    reader = make_reader(chain)
    x = snap(arun, reader, s).by_netuid(1)
    assert x is not None and x.last_epoch_block == 6_999_999
    keys = queried_keys(chain)
    assert to_hex(it.SUBNET["last_epoch_block"].key(netuid=1)) not in keys
    assert to_hex(it.SUBNET["w_quote_e18"].key(netuid=1)) not in keys                    # no Balancer in that runtime


# ------------------------------------------------------------------------------------------------ pulls and caches
def test_pull_plans_prefetch_and_version_bracketing(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 9_240_358)
    s.subnet(1, reg_at=100)
    for b in range(9_240_359, 9_240_366):
        s = s.clone(b)
    reader = make_reader(chain)

    async def go() -> list[ChainSnapshot]:
        return [x async for x in reader.pull(list(range(9_240_358, 9_240_366)))]

    snaps = arun(go())
    assert [int(x.block) for x in snaps] == list(range(9_240_358, 9_240_366))
    assert [x.plan for x in snaps] == [ReadPlan.FULL] + [ReadPlan.HEAD] + [ReadPlan.FULL] + [ReadPlan.HEAD] * 5
    assert len(chain.calls("state_getRuntimeVersion")) == 2
    assert len(chain.calls("chain_getBlockHash")) == 1
    assert all(x.subnets[0].quality & Quality.CARRIED for x in snaps if x.plan is ReadPlan.HEAD)


def test_pull_bisects_spec_change(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, 100_000, spec=200)
    s.subnet(1, reg_at=1, quote=None)
    for b in range(100_001, 100_009):
        s = s.clone(b, spec=200 if b < 100_005 else 201)
    reader = make_reader(chain)

    async def go() -> list[ChainSnapshot]:
        return [x async for x in reader.pull(list(range(100_000, 100_009)), full_every=3)]

    snaps = arun(go())
    assert [x.glob.spec_version for x in snaps] == [200] * 5 + [201] * 4
    assert len(chain.calls("state_getRuntimeVersion")) <= 6


def test_spec_cache_bracketing() -> None:
    c = SpecCache()
    c.add(10, 400, 1)
    c.add(20, 400, 1)
    c.add(30, 401, 1)
    assert c.lookup(15) == (400, 1) and c.lookup(10) == (400, 1)
    assert c.lookup(25) is None and c.lookup(5) is None and c.lookup(35) is None


def test_compare_prices(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100, tao=6_562_800_000, alpha_in=10**12, quote=5 * 10**17)
    out = snap(arun, make_reader(chain), s)
    assert out.subnets[0].pool.spot_rao() == 6_562_800
    ok = compare_prices(out, {0: 10**9, 1: 6_562_801})
    assert ok.ok and ok.n == 1
    bad = compare_prices(out, {1: 6_562_900})
    assert not bad.ok and bad.mismatches == ((1, 6_562_800, 6_562_900),) and bad.worst_netuid == 1
    s.rt("SwapRuntimeApi_current_alpha_price_all", "0x", bytes([2 << 2]) + (0).to_bytes(2, "little")
         + (10**9).to_bytes(8, "little") + (1).to_bytes(2, "little") + (6_562_800).to_bytes(8, "little"))
    pp = arun(make_reader(chain).price_parity(out))
    assert pp.ok and pp.max_rel_ppm == 0


def test_provider_disagreement_quarantines(arun: Any, fake_chain_cls: Any, state: Any, clock: Any) -> None:
    from taotrader.chain.rpc import RpcPool

    a, b = fake_chain_cls("fake://a"), fake_chain_cls("fake://b")
    s = state(a, B)
    s.subnet(1, reg_at=100)
    b.storage[s.hash] = {k: ("0x00" if v is not None else None) for k, v in a.storage[s.hash].items()}
    eps = [RpcPool.make_endpoint(t.url, Role.ARCHIVE, transport=t, clock=clock, sleep=clock.sleep, rate_per_s=1000,
                                 burst=1000, label=lab) for t, lab in ((a, "pa"), (b, "pb"))]
    drift: list[Any] = []
    reader = JsonRpcChainReader(RpcPool(eps, clock=clock, sleep=clock.sleep), provider_check_every=1,
                                on_drift=drift.append)
    snap(arun, reader, s)
    assert reader.pool.stats()["pb"].quarantined and drift and drift[0].probe == "provider"
    assert reader.pool.healthy() == 1


def test_on_raw_receives_every_value(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    s = state(chain, B)
    s.subnet(1, reg_at=100)
    got: list[Any] = []
    out = snap(arun, make_reader(chain, on_raw=lambda *a: got.append(a)), s)
    assert len(got) == 1 and got[0][0] == B and got[0][1] == s.hash
    raw = got[0][2]
    assert raw[to_hex(it.SUBNET["tao"].key(netuid=1))] == to_hex((587_199_047_950).to_bytes(8, "little"))
    assert out.digest


def test_block_hash_validation(arun: Any, chain: Any, make_reader: Any) -> None:
    reader = make_reader(chain)
    with pytest.raises(DecodeError):
        arun(reader.block_hash(Block(5)))                        # unknown block -> null hash
    chain.hashes[5] = "0x" + "AB" * 32
    assert arun(reader.block_hash(Block(5))) == "0x" + "ab" * 32
    chain.hashes[6] = "0x" + "cd" * 32
    assert arun(reader.block_hashes([5, 6])) == {5: "0x" + "ab" * 32, 6: "0x" + "cd" * 32}
    assert arun(reader.block_hashes([])) == {}


@pytest.mark.parametrize(("primary", "legacy", "want"), [(100, 200, 200), (300, 200, 300), (None, 150, 150)])
def test_last_registration_takes_max_in_overlap(arun: Any, chain: Any, state: Any, make_reader: Any, enc: Any,
                                                primary: int | None, legacy: int | None, want: int) -> None:
    """Specs 273-306 carry both LastRateLimitedBlock(0x02) and the older NetworkLastRegistered."""
    lay = SpecLayouts().exact(302)
    assert lay is not None and lay.entry("SubtensorModule.NetworkLastRegistered") is not None
    assert lay.entry("SubtensorModule.LastRateLimitedBlock") is not None
    le = enc["le"]
    s = state(chain, 6_266_751, spec=302)
    s.subnet(1, reg_at=100, quote=None)
    s.glob(last_reg_block=None if primary is None else le(primary, 8),
           last_reg_block_legacy=None if legacy is None else le(legacy, 8))
    assert snap(arun, make_reader(chain), s).glob.last_reg_block == want


def test_not_started_only_where_start_call_exists(arun: Any, chain: Any, state: Any, make_reader: Any) -> None:
    """Spec 234 (dTAO launch) has no FirstEmissionBlockNumber: every subnet emitted, so NOT_STARTED is not set."""
    lay = SpecLayouts().exact(234)
    assert lay is not None and lay.entry("SubtensorModule.FirstEmissionBlockNumber") is None
    s = state(chain, 4_927_551, spec=234)
    s.subnet(1, reg_at=100, quote=None, first_emission=None)
    x = snap(arun, make_reader(chain), s).by_netuid(1)
    assert x is not None and x.first_emission_block is None and not x.quality & Quality.NOT_STARTED
    assert x.quality & Quality.DEFAULT_FILLED                       # era-A swap fee is unverified (fee_rate_default)
    assert x.subtoken_enabled and x.emission_enabled and x.tao_flow_cum is None and x.owner_cut_enabled is None


def test_era_boundaries_agree_with_protocol_regimes() -> None:
    """chain.items keeps its own copies of the section 8.6 boundaries; they must match WP2's single table."""
    regimes = pytest.importorskip("taotrader.protocol.regimes")
    firsts = {int(r.first_block) for r in regimes.REGIMES}
    for b in (it.DTAO_LAUNCH_BLOCK, it.ERA_B_FIRST_BLOCK, it.BALANCER_FIRST_BLOCK):
        assert b in firsts, b
