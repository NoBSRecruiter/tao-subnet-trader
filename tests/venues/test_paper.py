"""PaperVenue (DESIGN.md 9.1, WP6): the sim_swap drift probe (> 5 bp -> ModelDriftObserved, fake reader), exact N+2
settlement on recorded state (store, then reader, else MISSED), the measured submit head, and a golden-fixture probe
against the SN92 sim_swap results recorded at block 9,240,388."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from taotrader.chain.runtime_api import dec_sim_swap
from taotrader.core.config import ExecCfg
from taotrader.core.events import FillReported, ModelDriftObserved, OrderFailed, VenueAck
from taotrader.core.orders import FailReason, OrderKind
from taotrader.core.protocols import SwapSim
from taotrader.core.state import ChainSnapshot, PoolKind, PoolState, ReadPlan
from taotrader.core.units import AlphaRao, Block, BlockHash, Hotkey, NetUid, Rao, SubnetKey
from taotrader.protocol.amm import quote_buy, quote_sell
from taotrader.venues.paper import DRIFT_THRESHOLD_PPM, PROBE_BUY, PROBE_SELL, PaperVenue, drift_ppm

TAO = 10**9
CFG = ExecCfg(shield_miss_ppm=0)


class FakeReader:
    """ChainReader double: snapshots by block, and a 'chain' whose sim_swap = local AMM x (1 + skew_ppm / 1e6) unless
    an exact result is registered for (netuid, kind, amount, block_hash)."""

    def __init__(self, snaps: Sequence[ChainSnapshot] = (), skew_ppm: int = 0) -> None:
        self.snaps = {int(s.block): s for s in snaps}
        self.skew_ppm = skew_ppm
        self.exact: dict[tuple[int, str, int, str], SwapSim] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail_sim = False
        self.zero_sim = False

    async def finalized_head(self) -> tuple[Block, BlockHash]:
        raise NotImplementedError

    async def dividend_keys(self, netuid: NetUid, block_hash: BlockHash) -> tuple[Hotkey, ...]:
        raise NotImplementedError

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        raise NotImplementedError

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        raise NotImplementedError

    async def registration_cost(self, block_hash: BlockHash) -> Rao:
        raise NotImplementedError

    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]:
        raise NotImplementedError

    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]:
        raise NotImplementedError

    async def block_hash(self, block: Block) -> BlockHash:
        self.calls.append(("block_hash", block))
        if int(block) not in self.snaps:
            raise KeyError(block)
        return self.snaps[int(block)].block_hash

    async def snapshot(self, block: Block, block_hash: BlockHash, plan: ReadPlan, prev: ChainSnapshot | None,
                       tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot:
        self.calls.append(("snapshot", (block, plan, tuple(tracked))))
        return self.snaps[int(block)]

    def _pool(self, netuid: NetUid, block_hash: BlockHash) -> PoolState:
        for s in self.snaps.values():
            if s.block_hash == block_hash:
                sub = s.by_netuid(int(netuid))
                assert sub is not None
                return sub.pool
        raise KeyError(block_hash)

    def _skew(self, x: int) -> int:
        return x + x * self.skew_ppm // 1_000_000

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        self.calls.append(("sim_buy", (netuid, tao_rao, block_hash)))
        if self.fail_sim:
            raise ConnectionError("rpc down")
        if self.zero_sim:
            return SwapSim(0, 0, 0, 0, 0, 0)
        hit = self.exact.get((int(netuid), "buy", tao_rao, block_hash))
        if hit is not None:
            return hit
        q = quote_buy(self._pool(netuid, block_hash), Rao(tao_rao))
        return SwapSim(q.amount_in - q.fee, self._skew(q.amount_out), q.fee, 0, 0, 0)

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        self.calls.append(("sim_sell", (netuid, alpha_rao, block_hash)))
        if self.fail_sim:
            raise ConnectionError("rpc down")
        if self.zero_sim:
            return SwapSim(0, 0, 0, 0, 0, 0)
        hit = self.exact.get((int(netuid), "sell", alpha_rao, block_hash))
        if hit is not None:
            return hit
        q = quote_sell(self._pool(netuid, block_hash), AlphaRao(alpha_rao))
        return SwapSim(self._skew(q.amount_out), alpha_rao - q.fee, 0, q.fee, 0, 0)


class FakeStore:
    def __init__(self, snaps: Sequence[ChainSnapshot] = ()) -> None:
        self.snaps = {int(s.block): s for s in snaps}
        self.clock = Block(0)

    def at(self, block: Block) -> ChainSnapshot:
        return self.snaps[int(block)]


def _paper(consts: dict[str, Any], reader: Any, **kw: Any) -> PaperVenue:
    return PaperVenue(consts["BOOK"], kw.pop("cfg", CFG), reader=reader, seed=1, **kw)


# ------------------------------------------------------------------------------------------------- drift probe
def test_drift_ppm() -> None:
    assert drift_ppm(100, 100) == 0 and drift_ppm(0, 0) == 0 and drift_ppm(5, 0) == 1_000_000
    assert drift_ppm(10_005, 10_000) == 500 and drift_ppm(9_995, 10_000) == 500 and drift_ppm(10_006, 10_000) == 600


@pytest.mark.parametrize(("skew_ppm", "drift"), [(0, False), (490, False), (-490, False), (510, True), (-510, True)])
def test_buy_probe_threshold_is_5_bp(consts, harness, snap, buy, skew_ppm, drift) -> None:
    reader = FakeReader([snap(1_005)], skew_ppm=skew_ppm)
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.place(buy(1_000, 3 * TAO), snap(1_000))
    evs = h.tick(snap(1_005))
    if drift:
        d, f = evs
        assert isinstance(d, ModelDriftObserved) and isinstance(f, FillReported)
        assert d.probe == PROBE_BUY and d.netuid == consts["NETUID"] and d.block == 1_005
        assert d.err_ppm > DRIFT_THRESHOLD_PPM and abs(d.err_ppm - abs(skew_ppm)) <= 2
    else:
        (f,) = evs
        assert isinstance(f, FillReported)
    assert f.fill.block == 1_005 and f.fill.exact_block
    assert sum(1 for c, _ in reader.calls if c == "sim_buy") == 1          # one probe per fill
    h.check_ledger()


def test_sell_probe_uses_the_unoverlaid_pool(consts, harness, snap, buy, sell) -> None:
    """PERSISTENT footprint: the fill trades on raw + footprint, the probe compares on the raw pool (as the chain)."""
    s1, s2 = snap(1_005), snap(1_015)
    reader = FakeReader([s1, s2], skew_ppm=0)
    h = harness(_paper(consts, reader, cfg=ExecCfg(shield_miss_ppm=0, impact_half_life_blocks=None)))
    h.capital(100 * TAO)
    h.place(buy(1_000, 20 * TAO), snap(1_000))
    h.tick(s1)
    h.place(sell(1_010, full=True), snap(1_010))
    (fs,) = h.tick(s2)                                   # no drift although the venue's own view is shifted
    assert isinstance(fs, FillReported) and fs.fill.kind is OrderKind.REMOVE_STAKE_LIMIT
    (call,) = [a for c, a in reader.calls if c == "sim_sell"]
    assert call == (consts["NETUID"], fs.fill.alpha, s2.block_hash)
    assert fs.fill.tao != quote_sell(s2.get(consts["KEY"]).pool, AlphaRao(fs.fill.alpha)).amount_out   # view != raw


def test_sell_probe_drift_is_reported_before_the_fill(consts, harness, snap, buy, sell) -> None:
    s1, s2 = snap(1_005), snap(1_065)
    reader = FakeReader([s1, s2], skew_ppm=0)
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.place(buy(1_000, 5 * TAO), snap(1_000))
    h.tick(s1)
    reader.skew_ppm = -800                                # the chain pays 8 bp less than the local model
    h.place(sell(1_060, full=True), snap(1_060))
    d, f = h.tick(s2)
    assert isinstance(d, ModelDriftObserved) and d.probe == PROBE_SELL and 790 <= d.err_ppm <= 810
    assert isinstance(f, FillReported) and f.fill.block == d.block == 1_065
    h.check_ledger()


def test_all_zero_sim_counts_as_failure(consts, harness, snap, buy) -> None:
    reader = FakeReader([snap(1_005)])
    reader.zero_sim = True
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    d, f = h.tick(snap(1_005))
    assert isinstance(d, ModelDriftObserved) and d.err_ppm == 1_000_000 and isinstance(f, FillReported)


def test_probe_rpc_error_is_skipped_not_drift(consts, harness, snap, buy) -> None:
    reader = FakeReader([snap(1_005)])
    reader.fail_sim = True
    v = _paper(consts, reader)
    h = harness(v)
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    (f,) = h.tick(snap(1_005))
    assert isinstance(f, FillReported) and v.probe_errors >= 1


def test_journaled_drift_is_not_re_probed_after_a_crash(consts, harness, snap, buy, arun) -> None:
    """Crash after ModelDriftObserved is committed and before the fill: the rebuilt venue returns the fill directly."""
    reader = FakeReader([snap(1_005)], skew_ppm=2_000)
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    h.observe_snapshot(snap(1_005))
    drift = arun(h.venue.advance(h.venue.mark_to(snap(1_005))))
    assert isinstance(drift, ModelDriftObserved)
    h.commit(drift)
    fresh = harness(_paper(consts, reader))
    for ev in h.journal:
        fresh.commit(ev)
    n_probes = len(reader.calls)
    (f,) = fresh.drain(snap(1_005))
    assert isinstance(f, FillReported) and len(reader.calls) == n_probes


def test_moves_and_failures_are_not_probed(consts, harness, snap, buy, move) -> None:
    reader = FakeReader([snap(1_005), snap(1_065)], skew_ppm=5_000)
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO, limit=1), snap(1_000))         # fails: crossed limit
    (ev,) = h.tick(snap(1_005))
    assert isinstance(ev, OrderFailed)
    h.place(move(1_060), snap(1_060))                     # fails: nothing to move
    (ev2,) = h.tick(snap(1_065))
    assert isinstance(ev2, OrderFailed) and not [c for c, _ in reader.calls if c.startswith("sim_")]


# ------------------------------------------------------------------------------------------------- submit head / N+2
def test_measured_finality_lag_sets_the_submit_head(consts, harness, snap, buy) -> None:
    reader = FakeReader([snap(1_006)])
    h = harness(_paper(consts, reader))
    h.capital(100 * TAO)
    h.observe_snapshot(snap(1_000), lag=4)
    ack = h.place(buy(1_000, TAO), snap(1_000))
    assert isinstance(ack, VenueAck) and (ack.submit_block, ack.expected_fill_block) == (1_004, 1_006)
    assert h.tick(snap(1_005), lag=4) == []
    (f,) = h.tick(snap(1_006), lag=4)
    assert isinstance(f, FillReported) and f.fill.block == 1_006


def test_shield_era_stale_when_finality_lags(consts, harness, snap, buy) -> None:
    h = harness(_paper(consts, FakeReader()))
    h.capital(100 * TAO)
    h.observe_snapshot(snap(1_000), lag=7)                # N+2 = 1,009 > the era's last valid block 1,007
    ev = h.place(buy(1_000, TAO), snap(1_000))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.VENUE_REJECT and ev.tx_fee == 0
    assert ev.detail.startswith("shield_era_stale")
    unshielded = h.place(buy(1_000, TAO, shielded=False, hotkey=consts["HK_B"]), snap(1_000))
    assert isinstance(unshielded, VenueAck) and unshielded.expected_fill_block == 1_008


def test_feed_gap_settles_on_the_recorded_n_plus_2_state(consts, harness, snap, subnet, pool, buy) -> None:
    c = consts
    rec = snap(1_005, subnet(pool(1_200)))                # the recorded N+2 differs from the late view
    reader = FakeReader([rec])
    h = harness(_paper(c, reader, store=FakeStore([rec])))
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO, rec.get(c["KEY"]).pool), snap(1_000))
    (f,) = h.tick(snap(1_009))
    assert isinstance(f, FillReported) and f.fill.block == 1_005 and f.fill.exact_block
    assert f.fill.alpha == quote_buy(rec.get(c["KEY"]).pool, Rao(TAO)).amount_out
    assert not [x for x, _ in reader.calls if x == "snapshot"]                # the store had it


def test_feed_gap_falls_back_to_the_reader(consts, harness, snap, subnet, pool, buy) -> None:
    c = consts
    rec = snap(1_005, subnet(pool(1_200)))
    reader = FakeReader([rec])
    v = _paper(c, reader, store=FakeStore([]))
    h = harness(v)
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO, rec.get(c["KEY"]).pool), snap(1_000))
    (f,) = h.tick(snap(1_009))
    assert isinstance(f, FillReported) and f.fill.block == 1_005
    ((blk, plan, tracked),) = [a for x, a in reader.calls if x == "snapshot"]
    assert (blk, plan) == (1_005, ReadPlan.FULL) and tracked == ((c["KEY"], c["HK_A"]),)
    assert v.state_errors == 1                            # the store miss


def test_feed_gap_without_any_state_is_a_miss(consts, harness, snap, buy) -> None:
    v = _paper(consts, FakeReader([]))
    h = harness(v)
    h.capital(100 * TAO)
    h.place(buy(1_000, TAO), snap(1_000))
    (ev,) = h.tick(snap(1_009))
    assert isinstance(ev, OrderFailed) and ev.reason is FailReason.SHIELD_MISSED and ev.expired
    assert ev.tx_fee == CFG.carrier_fee_rao and ev.detail.startswith("fill_state_unavailable")
    assert v.caps.kind == "paper"


# ------------------------------------------------------------------------------------------------- golden SN92 probe
def _le(hex_value: str) -> int:
    return int.from_bytes(bytes.fromhex(hex_value[2:]), "little")


@pytest.fixture(scope="module")
def sn92(golden: Any, make_subnet: Any, hk_idx: Any, consts: dict[str, Any]) -> dict[str, Any]:
    g = golden("sn92_9240388")
    s0 = g["snapshots"][0]
    st = {(e["item"], tuple(e["args"])): e["value"] for e in s0["storage"]}
    tao, alpha = _le(st[("SubtensorModule.SubnetTAO", (92,))]), _le(st[("SubtensorModule.SubnetAlphaIn", (92,))])
    w_quote = _le(st[("Swap.SwapBalancer", (92,))])
    fee_raw = st[("Swap.FeeRate", (92,))]
    pool = PoolState(PoolKind.BALANCER, Rao(tao), AlphaRao(alpha), tao, alpha, w_quote, 33 if fee_raw is None else _le(fee_raw))
    reg_at = _le(st[("SubtensorModule.NetworkRegisteredAt", (92,))])
    sub = make_subnet(92, reg_at=reg_at, pool=pool, hotkeys=(hk_idx(consts["HK_A"]),))
    sims = {}
    for r in s0["runtime_api"]:
        if r["method"] == "SwapRuntimeApi_sim_swap_tao_for_alpha":
            sims[("buy", r["args"]["tao_rao"])] = dec_sim_swap(r["result"])
        elif r["method"] == "SwapRuntimeApi_sim_swap_alpha_for_tao":
            sims[("sell", r["args"]["alpha_rao"])] = dec_sim_swap(r["result"])
    return {"block": int(s0["block"]), "hash": BlockHash(s0["block_hash"]), "subnet": sub, "sims": sims}


def test_golden_sn92_probe_has_zero_drift(consts, harness, snap, buy, sell, sn92) -> None:
    c = consts
    b, sub = sn92["block"], sn92["subnet"]
    key = sub.key

    def at(block: int, s: Any = sub) -> ChainSnapshot:
        x: ChainSnapshot = snap(block, s)
        return replace(x, block_hash=sn92["hash"]) if block == b else x

    reader = FakeReader([at(b - 8), at(b)])
    for (kind, amount), sim in sn92["sims"].items():
        reader.exact[(92, kind, amount, sn92["hash"])] = sim
    h = harness(_paper(c, reader))
    h.capital(200 * TAO)
    # a 10-TAO buy settling exactly at 9,240,388: compared with the recorded sim_swap_tao_for_alpha(92, 10 TAO)
    h.place(buy(b - 5, 10 * TAO, sub.pool, key=key), at(b - 5))
    (f,) = h.tick(at(b))
    assert isinstance(f, FillReported) and f.fill.alpha == sn92["sims"][("buy", 10 * TAO)].alpha_amount == 7_289_425_629_146
    # a 1,000-alpha partial sell settling at the same block from a second book's position
    h2 = harness(PaperVenue(c["BOOK"], CFG, reader=reader, seed=2))
    h2.capital(200 * TAO)
    h2.place(buy(b - 13, 10 * TAO, sub.pool, key=key), at(b - 13))
    h2.tick(at(b - 8))
    h2.place(sell(b - 5, 1_000 * TAO, sub.pool, key=key), at(b - 5))
    (fs,) = h2.tick(at(b))
    assert isinstance(fs, FillReported) and fs.fill.alpha == 1_000 * TAO
    assert fs.fill.tao == sn92["sims"][("sell", 1_000 * TAO)].tao_amount == 1_344_446_749
    probes = [a for x, a in reader.calls if x in ("sim_buy", "sim_sell") and a[2] == sn92["hash"]]
    assert len(probes) == 2


def test_golden_sn92_probe_catches_a_fee_change(consts, harness, snap, buy, sn92) -> None:
    """The recorded pool says FeeRate 196 while the chain still simulates 33 (a decoder or fee-change bug): flagged."""
    c = consts
    b, sub = sn92["block"], sn92["subnet"]
    stale = replace(sub, pool=replace(sub.pool, fee_rate=196))
    at_b = replace(snap(b, stale), block_hash=sn92["hash"])
    reader = FakeReader([at_b])
    reader.exact[(92, "buy", 10 * TAO, sn92["hash"])] = sn92["sims"][("buy", 10 * TAO)]
    h = harness(_paper(c, reader))
    h.capital(200 * TAO)
    h.place(buy(b - 5, 10 * TAO, stale.pool, key=sub.key), snap(b - 5, stale))
    d, f = h.tick(at_b)
    assert isinstance(d, ModelDriftObserved) and d.probe == PROBE_BUY and d.netuid == 92
    assert 2_400 < d.err_ppm < 2_600                      # (196 - 33) / 65535 = 0.249%
    assert isinstance(f, FillReported)
