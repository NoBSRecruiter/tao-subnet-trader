"""Shared WP11 test builders (tests/live/conftest.py loads this module as the `lk` fixture; importlib mode keeps test
modules from importing each other).

Default world: the real coldkey REAL holds 100 TAO free; three Staking delegates ops0..ops2 hold 0.3 TAO each at
nonce 10; SN92 (reg 8,000,000) has a 587.2-TAO / 435,000-alpha Balancer pool (spot ~ 0.00135 TAO/alpha) and SN64
(reg 7,000,000) a 1,000-TAO / 500,000-alpha pool; validator hotkeys HK_A / HK_B start with index 1 (1,000,000 alpha
and shares each). The decision block is B0; the fake chain's best head and the reader's finalized head are set by
`Kit.set_head`. Synthetic block hashes are "0x" + 64-hex block number (sdk_port.block_hash_of).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Context, Decimal
from pathlib import Path
from typing import Any

from taotrader.core.config import BookCfg, ExecCfg, LiveCfg, RiskCfg, SleeveCfg
from taotrader.core.events import (
    CapitalChanged,
    HealthObs,
    JournalEvent,
    OrderIntended,
    SnapshotObserved,
    SubmitStarted,
    SubmitUnknown,
    VenueAck,
)
from taotrader.core.fixed import DEC, floor_int
from taotrader.core.orders import OrderIntent, OrderKind, Urgency, make_order_id
from taotrader.core.protocols import SwapSim
from taotrader.core.state import ChainGlobals, ChainSnapshot, HotkeyIdx, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import (
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    BookId,
    Hotkey,
    NetUid,
    Ppm,
    PriceRao,
    Rao,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.live.gate import Arming, GateDecision, LiveState
from taotrader.live.nonce import MemorySubmissions
from taotrader.live.sdk_port import FakeSdkPort, block_hash_of, ss58_encode
from taotrader.live.venue import LiveVenue
from taotrader.protocol.amm import marginal_after_buy, marginal_after_sell, quote_buy, quote_sell
from taotrader.protocol.prune import prune_target

TAO = RAO_PER_TAO
X = Context(prec=200)                       # exact share arithmetic in the scripted chain
BOOK = BookId("live")
RUN = "run"
REAL = ss58_encode("0x" + "aa" * 32)
DELEGATES = {"ops0": ss58_encode("0x" + "d0" * 32), "ops1": ss58_encode("0x" + "d1" * 32),
             "ops2": ss58_encode("0x" + "d2" * 32)}
D0, D1, D2 = DELEGATES["ops0"], DELEGATES["ops1"], DELEGATES["ops2"]
HK_A = Hotkey("0x" + "a1" * 32)
HK_B = Hotkey("0x" + "b2" * 32)
HK_C = Hotkey("0x" + "c3" * 32)
SA, SB, SC = ss58_encode(HK_A), ss58_encode(HK_B), ss58_encode(HK_C)
SN, REG = 92, 8_000_000
SN2, REG2 = 64, 7_000_000
KEY = SubnetKey(NetUid(SN), Block(REG))
KEY2 = SubnetKey(NetUid(SN2), Block(REG2))
B0 = 9_240_400
SPEC = 475
NOW = 1_800_000_000
START_NONCE = 10
DELEGATE_FREE = 300_000_000
REAL_FREE = 100 * TAO
HK_ALPHA0 = 1_000_000 * TAO


def pool(tao: int = 587_200_000_000, alpha: int = 435_000_000_000_000) -> PoolState:
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao, px_alpha=alpha,
                     w_quote_e18=5 * 10**17, fee_rate=33)


DEFAULT_POOLS: dict[int, PoolState] = {SN: pool(), SN2: pool(1_000 * TAO, 500_000 * TAO)}


def subnet(netuid: int, reg_at: int, p: PoolState, *, moving_price: str = "0.0013482", **over: Any) -> SubnetState:
    hk = (HotkeyIdx(HK_A, AlphaRao(HK_ALPHA0), Decimal(HK_ALPHA0), take_u16=0, earns=True,
                    last_dividend=AlphaRao(1_000 * TAO)),
          HotkeyIdx(HK_B, AlphaRao(HK_ALPHA0), Decimal(HK_ALPHA0), take_u16=0, earns=True,
                    last_dividend=AlphaRao(1_000 * TAO)))
    base = SubnetState(
        key=SubnetKey(NetUid(netuid), Block(reg_at)), pool=p, alpha_out=AlphaRao(630_980 * TAO),
        protocol_alpha=AlphaRao(41_000 * TAO), moving_price=Decimal(moving_price), root_prop=Decimal("0.479"),
        miner_burned=Decimal(0), emission_enabled=True, subtoken_enabled=True, reg_allowed=True,
        first_emission_block=Block(reg_at + 600), tempo=360, last_epoch_block=Block(reg_at + 720),
        ema_halving_blocks=201_600, tao_in_emission=Rao(4_468), excess_tao=Rao(0), alpha_out_emission=AlphaRao(TAO),
        alpha_in_emission=AlphaRao(3_314_091), owner_hotkey=HK_A, hotkeys=tuple(sorted(hk, key=lambda h: h.hotkey)))
    return replace(base, **over) if over else base


def glob(**over: Any) -> ChainGlobals:
    base = ChainGlobals(
        spec_version=SPEC, tx_version=1, total_issuance=Rao(11_597_600 * TAO), block_emission=Rao(500_000_000),
        moving_alpha=Decimal(1_288_490) / Decimal(2**32), gate_bar=Decimal("0.0082624"), gate_rank=32, gate_exponent=3,
        tao_weight=Decimal("0.18"), root_tao=Rao(5_454_000 * TAO), owner_cut_u16=11_796, subnet_limit=128,
        immunity_period=864_000, network_rate_limit=14_400, last_reg_block=Block(9_210_610),
        last_lock_cost=Rao(653_020_000_000), min_lock_cost=Rao(TAO), lock_reduction_interval=115_200,
        tao_in_refund_block=Block(8_334_450), nominator_min_stake=Rao(20_000_000), cleanup_queue_len=0,
        n_nonroot_networks=128, safe_mode_until=None)
    return replace(base, **over) if over else base


def snapshot(block: int, subnets: Sequence[SubnetState] | None = None, **glob_over: Any) -> ChainSnapshot:
    subs = list(subnets) if subnets is not None else [subnet(SN2, REG2, DEFAULT_POOLS[SN2], moving_price="0.002"),
                                                      subnet(SN, REG, DEFAULT_POOLS[SN])]
    return ChainSnapshot(block=Block(block), block_hash=block_hash_of(block), timestamp_ms=1_759_900_000_000 + block,
                         plan=ReadPlan.FULL, glob=glob(**glob_over),
                         subnets=tuple(sorted(subs, key=lambda s: int(s.key.netuid))))


class FakeReader:
    """LiveReader over synthetic hashes; sim_swap parity with protocol.amm unless overridden."""

    def __init__(self, pools: dict[int, PoolState] | None = None) -> None:
        self.fin = B0
        self.pools = dict(DEFAULT_POOLS if pools is None else pools)
        self.prices: dict[int, int] = {}
        self.prune: int | None = None
        self.sim_bias = 0
        self.calls: list[str] = []

    async def block_hash(self, block: Block) -> BlockHash:
        self.calls.append("block_hash")
        return block_hash_of(int(block))

    async def finalized_head(self) -> tuple[Block, BlockHash]:
        self.calls.append("finalized_head")
        return Block(self.fin), block_hash_of(self.fin)

    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]:
        self.calls.append("prices_all")
        return {n: self.prices.get(n, int(p.spot_rao())) for n, p in self.pools.items()}

    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim:
        q = quote_buy(self.pools[int(netuid)], Rao(tao_rao))
        return SwapSim(tao_amount=tao_rao, alpha_amount=q.amount_out + self.sim_bias, tao_fee=q.fee, alpha_fee=0,
                       tao_slippage=0, alpha_slippage=0)

    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim:
        q = quote_sell(self.pools[int(netuid)], AlphaRao(alpha_rao))
        return SwapSim(tao_amount=q.amount_out, alpha_amount=alpha_rao, tao_fee=0, alpha_fee=q.fee, tao_slippage=0,
                       alpha_slippage=0)

    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None:
        if self.prune is not None:
            return NetUid(self.prune)
        t = prune_target(snapshot(block_of(block_hash)))
        return None if t is None else t.netuid


def block_of(h: str) -> int:
    return int(h, 16)


class Guard:
    """RiskExitGuard stub (V2/V3/V6 verdict)."""

    def __init__(self, ok: bool = True) -> None:
        self.verdict = ok
        self.calls = 0

    async def ok(self, snap: ChainSnapshot) -> bool:
        self.calls += 1
        return self.verdict


def live_cfg(**over: Any) -> LiveCfg:
    base = LiveCfg(enabled=True, mode="submit", network="test", real_coldkey_ss58=REAL,
                   delegate_wallets=("ops0", "ops1", "ops2"), sleeves=(StrategyId("carry"),), accepted_specs=(SPEC,))
    return replace(base, **over) if over else base


def book_cfg(stage: Stage = Stage.LIVE_ELIGIBLE) -> BookCfg:
    return BookCfg(book=BOOK, capital_rao=Rao(REAL_FREE), fee_float_rao=Rao(3 * DELEGATE_FREE),
                   sleeves=(SleeveCfg(StrategyId("carry"), stage, Ppm(500_000)),))


def buy_limit(p: PoolState, tao_in: int, beta_ppm: int = 10_000) -> PriceRao:
    m = int(marginal_after_buy(p, Rao(tao_in)))
    return PriceRao(-(-m * (PPM + beta_ppm) // PPM) + 1)


def sell_limit(p: PoolState, alpha: int, beta_ppm: int = 10_000) -> PriceRao:
    m = int(marginal_after_sell(p, AlphaRao(alpha)))
    return PriceRao(m * (PPM - beta_ppm) // PPM)


def intent(kind: OrderKind, *, block: int = B0, key: SubnetKey = KEY, hotkey: Hotkey = HK_A, tao_in: int = 0,
           alpha_in: int = 0, full: bool = False, limit: int | None = None, partial: bool = False, shielded: bool = True,
           urgency: Urgency = Urgency.NORMAL, attempt: int = 0, dest: Hotkey | None = None,
           valid_until: int | None = None) -> OrderIntent:
    p = DEFAULT_POOLS[int(key.netuid)]
    if limit is None:
        if kind is OrderKind.ADD_STAKE_LIMIT:
            limit = int(buy_limit(p, tao_in))
        elif kind is OrderKind.MOVE_STAKE:
            limit = 0
        else:
            limit = int(sell_limit(p, alpha_in if alpha_in > 0 else 5_000 * TAO, 30_000 if urgency >= Urgency.URGENT
                                   else 10_000))
    vu = valid_until if valid_until is not None else block + (5 if shielded else 3 + 16)
    return OrderIntent(order_id=make_order_id(RUN, BOOK, Block(block), key, hotkey, kind, attempt), attempt=attempt,
                       book=BOOK, created_block=Block(block), kind=kind, key=key, hotkey=hotkey, tao_in=Rao(tao_in),
                       alpha_in=AlphaRao(alpha_in), full_position=full, limit_price=PriceRao(limit), allow_partial=partial,
                       shielded=shielded, valid_until=Block(vu), expected_out=0, urgency=urgency,
                       attribution=((StrategyId("carry"), Ppm(PPM)),), reason="test", dest_hotkey=dest)


class Kit:
    """One LiveVenue over FakeSdkPort + FakeReader, with a journal list that every event is observed from."""

    def __init__(self, tmp: Path | None = None, *, live: LiveCfg | None = None, risk: RiskCfg | None = None,
                 state: LiveState = LiveState.ARMED, expiry: int | None = NOW + 3_600, guard_ok: bool = True) -> None:
        self.live = live or live_cfg()
        self.risk = risk or RiskCfg()
        self.sdk = FakeSdkPort(real=REAL, delegates=DELEGATES, max_fee_tao=self.live.max_fee_tao)
        self.sdk.pools = dict(DEFAULT_POOLS)
        self.reader = FakeReader()
        self.clock = [NOW]
        exp = None if state is LiveState.PLAN_ONLY else expiry
        self.arming = Arming(GateDecision(state, (), exp, self.live.network, "cfg"), self.live, lambda: self.clock[0])
        self.guard = Guard(guard_ok)
        self.subs = MemorySubmissions()
        self.kill = None if tmp is None else tmp / "KILL"
        self.venue = self.new_venue()
        self.journal: list[JournalEvent] = []
        for d in DELEGATES.values():
            self.sdk.set_account(0, d, free=DELEGATE_FREE, nonce=START_NONCE)
        self.sdk.set_account(0, REAL, free=REAL_FREE)
        for hk in (SA, SB, SC):
            for n in (SN, SN2):
                self.sdk.set_stake(0, hk, n, hk_alpha=HK_ALPHA0, hk_shares=Decimal(HK_ALPHA0))
        self.feed(SnapshotObserved(Block(B0), block_hash_of(B0), "d", ReadPlan.FULL, 0, HealthObs.nominal()))
        self.feed(CapitalChanged(BOOK, Block(B0), REAL_FREE, 3 * DELEGATE_FREE, "initial"))
        self.set_head(B0)

    def new_venue(self) -> LiveVenue:
        return LiveVenue(BOOK, sdk=self.sdk, reader=self.reader, live=self.live, risk=self.risk, exec_cfg=ExecCfg(),
                         arming=self.arming, spec_checks=self.guard, submissions=self.subs, kill_file=self.kill)

    def rebuilt(self) -> LiveVenue:
        v = self.new_venue()
        for ev in self.journal:
            v.observe(ev)
        return v

    # ---------------------------------------------------------------- world
    def snap(self, block: int | None = None, **glob_over: Any) -> ChainSnapshot:
        return snapshot(B0 if block is None else block, **glob_over)

    def set_head(self, best: int, fin: int | None = None) -> None:
        self.sdk.head = best
        self.reader.fin = best if fin is None else fin

    def feed(self, ev: JournalEvent) -> JournalEvent:
        self.journal.append(ev)
        self.venue.observe(ev)
        return ev

    def tick(self, block: int, health: HealthObs | None = None) -> None:
        self.feed(SnapshotObserved(Block(block), block_hash_of(block), "d", ReadPlan.FULL, 0, health or HealthObs.nominal()))

    def hold(self, hotkey: str, netuid: int, alpha: int, block: int = 0) -> None:
        """Give the real coldkey `alpha` worth of shares on (hotkey, netuid) at index 1 (chain side)."""
        self.sdk.set_stake(block, hotkey, netuid, shares=Decimal(alpha), hk_alpha=HK_ALPHA0 + alpha,
                           hk_shares=Decimal(HK_ALPHA0 + alpha))

    # ---------------------------------------------------------------- the Runner's outbox flow
    async def place(self, it: OrderIntent, snap: ChainSnapshot | None = None) -> JournalEvent:
        s = snap or self.snap()
        self.feed(OrderIntended(it))
        d, n, era_end = await self.venue.reserve(it, s)
        self.feed(SubmitStarted(BOOK, it.order_id, it.attempt, d, n, era_end))
        try:
            ev = await self.venue.submit(it, s)
        except Exception as e:
            ev = SubmitUnknown(BOOK, it.order_id, it.attempt, f"{type(e).__name__}: {e}")
        return self.feed(ev)

    async def drain(self, view_block: int) -> list[JournalEvent]:
        out: list[JournalEvent] = []
        view = self.snap(view_block)
        for _ in range(50):
            ev = await self.venue.advance(view)
            if ev is None:
                return out
            out.append(self.feed(ev))
        raise AssertionError("venue.advance did not run dry")

    def started(self, it: OrderIntent) -> SubmitStarted:
        return next(e for e in self.journal if isinstance(e, SubmitStarted) and e.order_id == it.order_id
                    and e.attempt == it.attempt)

    # ---------------------------------------------------------------- scripted inclusions
    def land_buy(self, ack: VenueAck, it: OrderIntent, *, hotkey: str = SA, netuid: int = SN, drain: int = 0,
                 alpha_out: int | None = None, tao: int | None = None, block: int | None = None,
                 extra_events: Sequence[tuple[str, str, dict[str, object]]] = ()) -> tuple[int, Decimal]:
        """Include the acked buy at N + 2: optional epoch drain first (index up), then shares issued at the post-drain
        index; real free -= tao. Returns (alpha_out, shares issued)."""
        b = int(ack.expected_fill_block) if block is None else block
        tao_in = int(it.tao_in) if tao is None else tao
        out = quote_buy(DEFAULT_POOLS[netuid], Rao(tao_in)).amount_out if alpha_out is None else alpha_out
        st = self.started(it)
        addr = DELEGATES[st.delegate]
        n = int(st.nonce or 0)
        hk_alpha = self._at("hk_alpha", hotkey, netuid, b - 1, HK_ALPHA0) + drain
        hk_shares = Decimal(self._at("hk_shares", hotkey, netuid, b - 1, Decimal(HK_ALPHA0)))
        new_shares = DEC.divide(DEC.multiply(Decimal(out), hk_shares), Decimal(hk_alpha))
        mine = self.sdk.shares_at(b - 1, hotkey, netuid)
        self.sdk.set_stake(b, hotkey, netuid, shares=X.add(mine, new_shares), hk_alpha=hk_alpha + out,
                           hk_shares=X.add(hk_shares, new_shares))
        self.sdk.set_account(b, REAL, free=self.sdk.free_at(b - 1, REAL) - tao_in)
        evs: list[tuple[str, str, dict[str, object]]] = [
               ("Proxy", "ProxyExecuted", {"result": "Ok"}),
               ("SubtensorModule", "StakeAdded", {"coldkey": REAL, "hotkey": hotkey, "tao": tao_in, "alpha": out,
                                                 "netuid": netuid}),
               ("System", "ExtrinsicSuccess", {}), *extra_events]
        self.sdk.include_shielded(b, ack.carrier_hash, ack.inner_hash, addr, n, inner_events=evs)
        return out, new_shares

    def land_sell(self, ack: VenueAck, it: OrderIntent, *, amount: int, hotkey: str = SA, netuid: int = SN,
                  tao: int | None = None, block: int | None = None, remainder: int = 0) -> int:
        """Include the acked sell of `amount` alpha at N + 2 (index 1 assumed); real free += tao."""
        b = int(ack.expected_fill_block) if block is None else block
        tao_out = quote_sell(DEFAULT_POOLS[netuid], AlphaRao(amount)).amount_out if tao is None else tao
        st = self.started(it)
        addr = DELEGATES[st.delegate]
        hk_alpha = self._at("hk_alpha", hotkey, netuid, b - 1, HK_ALPHA0)
        hk_shares = Decimal(self._at("hk_shares", hotkey, netuid, b - 1, Decimal(HK_ALPHA0)))
        burn = DEC.divide(DEC.multiply(Decimal(amount), hk_shares), Decimal(hk_alpha))
        mine = self.sdk.shares_at(b - 1, hotkey, netuid)
        left = X.add(X.subtract(mine, burn), Decimal(remainder))
        self.sdk.set_stake(b, hotkey, netuid, shares=left, hk_alpha=hk_alpha - amount + remainder,
                           hk_shares=X.add(X.subtract(hk_shares, burn), Decimal(remainder)))
        self.sdk.set_account(b, REAL, free=self.sdk.free_at(b - 1, REAL) + tao_out)
        evs: list[tuple[str, str, dict[str, object]]] = [
               ("Proxy", "ProxyExecuted", {"result": "Ok"}),
               ("SubtensorModule", "StakeRemoved", {"coldkey": REAL, "hotkey": hotkey, "tao": tao_out, "alpha": amount,
                                                   "netuid": netuid}),
               ("System", "ExtrinsicSuccess", {})]
        self.sdk.include_shielded(b, ack.carrier_hash, ack.inner_hash, addr, int(st.nonce or 0), inner_events=evs)
        return tao_out

    def _at(self, what: str, hotkey: str, netuid: int, block: int, default: Any) -> Any:
        return self.sdk._line(what, hotkey, netuid).at(block, default)


def alpha_value(shares: Decimal, hk_alpha: int, hk_shares: Decimal) -> int:
    return floor_int(DEC.divide(DEC.multiply(shares, Decimal(hk_alpha)), hk_shares))
