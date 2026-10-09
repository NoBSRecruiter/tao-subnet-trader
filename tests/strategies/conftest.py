"""Fixtures for the WP9 strategy tests: a deterministic synthetic market (snapshots, features, frames, a history store)
and TickContext / BookView / Portfolio builders.

Test modules cannot import each other (importlib mode), so every helper is exposed through the `sx` fixture (a
namespace). Market conventions:
- `Spec` describes one generation. Its SubnetState at any block is a pure function of the block: the hotkey index grows
  by `growth_ppm` per epoch (LastEpochBlock steps every `tempo` blocks from REF), SubnetTaoFlow grows by
  `flow_frac_day` of the pool per day, and the spot is constant (pool reserves fixed).
- `Market.ctx(block, ...)` builds the TickContext with a FakeStore holding the 60-block history before `block`
  (history_blocks long), the raw = view snapshot, and the features of each Spec (Feat defaults overridable per Spec).
- The ladder bottom (target) is BOTTOM: netuid 5, a 300-TAO pool with EMA 0.001; every other spec defaults to EMA
  0.003 (rho = 3) and prune rank 20.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from taotrader.core.config import SleeveCfg
from taotrader.core.errors import LookaheadError
from taotrader.core.events import ChainEvent
from taotrader.core.portfolio import Portfolio, Position, SleeveHolding
from taotrader.core.protocols import BookView, RouterState, TickContext
from taotrader.core.state import ChainSnapshot, HotkeyIdx, MetagraphLite, PoolKind, PoolState, SubnetState
from taotrader.core.units import (
    PPM,
    RAO_PER_TAO,
    AlphaRao,
    Block,
    BlockHash,
    Hotkey,
    Mode,
    NetUid,
    Ppm,
    Rao,
    Stage,
    StrategyId,
    SubnetKey,
)
from taotrader.core.views import EmissionView, Feat, FeatureFrame, PruneView, RouterCandidate

TAO = RAO_PER_TAO
B = 9_300_000                    # default decision block (multiple of 300 and 60)
REF = B - 100 * 360              # epoch / flow anchor
HK1 = Hotkey("0x" + "1" * 64)
HK2 = Hotkey("0x" + "2" * 64)
HK_OWNER = Hotkey("0x" + "0e" * 32)
OWNER_CK = "0x" + "0c" * 32
BOTTOM = SubnetKey(NetUid(5), Block(7_000_000))


# ------------------------------------------------------------------------------------------------- store
class FakeStore:
    """SnapshotStore over a dict with the lookahead guard."""

    def __init__(self, snaps: Sequence[ChainSnapshot] = (), clock: int = 0) -> None:
        self.clock: Block = Block(clock)
        self._by_block: dict[int, ChainSnapshot] = {int(s.block): s for s in snaps}
        self.calls = 0

    def add(self, s: ChainSnapshot) -> None:
        self._by_block[int(s.block)] = s

    def at(self, block: Block) -> ChainSnapshot:
        if block > self.clock:
            raise LookaheadError(f"{block} > clock {self.clock}")
        return self._by_block[int(block)]

    def at_or_before(self, block: Block) -> ChainSnapshot:
        self.calls += 1
        if block > self.clock:
            raise LookaheadError(f"{block} > clock {self.clock}")
        best = max((b for b in self._by_block if b <= block), default=None)
        if best is None:
            raise KeyError(block)
        return self._by_block[best]

    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]:
        if until > self.clock:
            raise LookaheadError(f"{until} > clock {self.clock}")
        return [self._by_block[b] for b in sorted(self._by_block) if until - span_blocks < b <= until]


# ------------------------------------------------------------------------------------------------- builders
def make_pool(tao_tao: float | int = 2_000, spot: str = "0.01", wq: int = 5 * 10**17, fee: int = 33) -> PoolState:
    t = int(Decimal(str(tao_tao)) * TAO)
    w_b = Decimal(10**18 - wq)
    a = int(Decimal(t) * w_b / (Decimal(wq) * Decimal(spot)))
    return PoolState(kind=PoolKind.BALANCER, tao=Rao(t), alpha=AlphaRao(a), px_tao=t, px_alpha=a, w_quote_e18=wq,
                     fee_rate=fee)


def candidate(h: Hotkey = HK1, **over: Any) -> RouterCandidate:
    base = RouterCandidate(hotkey=h, score_ppm_day=6_000, take_u16=0, childkey_take_u16=0, member_frac_ppm=Ppm(PPM),
                           member_last2=True, permit_rank=1, ratio_ok=True, take_increase_recent=False, eligible=True)
    return replace(base, **over) if over else base


def feat(key: SubnetKey, **over: Any) -> Feat:
    base = Feat(
        key=key, spot=0.01, pool_tao=2_000.0, k_w=2.0, ret_1h=0.0, ret_1d=0.01, ret_7d=0.05, sigma_d=0.05,
        fast_ema_gap=0.0, ema_gap=0.0, flow_1h=0.0, flow_1d=0.0, flow_7d=0.0, flow_z_1d=0.0, emis_tao_day=10.0,
        chain_buy_day=0.0, obs_emis_tao_day=10.0, gate_keep=1.0, burn_adj_rank=20, ema_rank_desc=30, rp=0.4,
        sell_push_day=0.0, cb_push_day=0.001, escrow_frac=0.0, a_earn_alpha=100_000.0, yield_cf_gross_day=0.005,
        router_candidates=(candidate(),), best_candidate=HK1, yield_net_day=0.004, a_earn_growth_day=0.01,
        age_reg_blocks=B - int(key.reg_at), since_start_blocks=B - int(key.reg_at) - 599, immune=False,
        immune_until=Block(int(key.reg_at) + 864_000), prune_rank=20, rho=3.0, t_star_stress_blocks=None,
        launch_flags=frozenset(), beta_entry_ppm=Ppm(5_000), beta_exit_ppm=Ppm(10_000), owner_sold_6h_frac=0.0,
        owner_liquid_frac=0.0, top_holder_frac=0.1)
    return replace(base, **over) if over else base


@dataclass
class Spec:
    netuid: int
    reg_at: int = 8_000_000
    tao_tao: float = 2_000
    spot: str = "0.01"
    wq: int = 5 * 10**17
    moving_price: str = "0.003"
    growth_ppm: int = 300                 # hotkey index growth per epoch
    flow_frac_day: float = 0.0            # SubnetTaoFlow growth per day as a fraction of the pool
    tempo: int = 360
    hotkey_alpha_tao: int = 100_000
    hotkeys: tuple[Hotkey, ...] = (HK1,)
    take_u16: int = 0
    first_emission_offset: int = 600
    owner_alpha_tao: int | None = None    # owner coldkey alpha on HK_OWNER (None = not read)
    owner_growth_per_block: int = 0       # owner alpha change per block (rao)
    state: dict[str, Any] = field(default_factory=dict)
    feat: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> SubnetKey:
        return SubnetKey(NetUid(self.netuid), Block(self.reg_at))


class Market:
    def __init__(self, make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot],
                 specs: Sequence[Spec], *, bottom: bool = True, glob: Mapping[str, Any] | None = None) -> None:
        self.make_subnet = make_subnet
        self.make_snapshot = make_snapshot
        self.specs = list(specs)
        if bottom:
            self.specs.append(Spec(5, reg_at=7_000_000, tao_tao=300, spot="0.002", moving_price="0.001",
                                   feat={"prune_rank": 1, "rho": 1.0, "pool_tao": 300.0, "spot": 0.002}))
        self.glob = dict(glob or {})

    def spec(self, key: SubnetKey) -> Spec:
        return next(s for s in self.specs if s.key == key)

    @staticmethod
    def epochs(blk: int, tempo: int) -> int:
        return max(0, (blk - REF) // tempo)

    def state(self, sp: Spec, blk: int) -> SubnetState | None:
        if blk < sp.reg_at:
            return None
        n = self.epochs(blk, sp.tempo)
        hks = []
        for h in sp.hotkeys:
            total = sp.hotkey_alpha_tao * TAO
            for _ in range(n):
                total = total * (PPM + sp.growth_ppm) // PPM
            hks.append(HotkeyIdx(hotkey=h, total_alpha=AlphaRao(total), total_shares=Decimal(sp.hotkey_alpha_tao * TAO),
                                 take_u16=sp.take_u16, earns=True))
        owner: dict[str, Any] = {}
        if sp.owner_alpha_tao is not None:
            hks.append(HotkeyIdx(hotkey=HK_OWNER, total_alpha=AlphaRao(10**15), total_shares=Decimal(10**15),
                                 take_u16=0, earns=False))
            owner = {"owner_hotkey": HK_OWNER, "owner_coldkey": OWNER_CK,
                     "owner_alpha": AlphaRao(sp.owner_alpha_tao * TAO + sp.owner_growth_per_block * (blk - REF))}
        p = make_pool(sp.tao_tao, sp.spot, sp.wq)
        flow = int(sp.flow_frac_day * int(p.tao) * (blk - REF) / 7_200)
        last_epoch = REF + ((blk - REF) // sp.tempo) * sp.tempo if blk >= REF else REF
        kw: dict[str, Any] = dict(
            pool=p, alpha_out=AlphaRao(1_000_000 * TAO), protocol_alpha=AlphaRao(0), moving_price=Decimal(sp.moving_price),
            root_prop=Decimal("0.4"), alpha_out_emission=AlphaRao(10_000_000), escrow_alpha=AlphaRao(0),
            first_emission_block=Block(sp.reg_at + sp.first_emission_offset), tempo=sp.tempo,
            last_epoch_block=Block(last_epoch), tao_flow_cum=flow, hotkeys=tuple(sorted(hks, key=lambda x: x.hotkey)),
            max_allowed_validators=64, **owner)
        kw.update(sp.state)
        return self.make_subnet(sp.netuid, reg_at=sp.reg_at, **kw)

    def snap(self, blk: int) -> ChainSnapshot:
        subs = [s for s in (self.state(sp, blk) for sp in self.specs) if s is not None]
        s = self.make_snapshot(blk, subs, **self.glob)
        return replace(s, digest=f"d{blk}")

    def store(self, until: int, span: int, step: int = 60) -> FakeStore:
        first = (until - span) // step * step + step
        snaps = [self.snap(b) for b in range(max(first, 0), until + 1, step)]
        return FakeStore(snaps, clock=until)

    def feats(self, blk: int) -> dict[SubnetKey, Feat]:
        out: dict[SubnetKey, Feat] = {}
        for sp in self.specs:
            if blk < sp.reg_at:
                continue
            ss = blk - (sp.reg_at + sp.first_emission_offset - 1)
            base = feat(sp.key, spot=float(Decimal(sp.spot)), pool_tao=float(sp.tao_tao), age_reg_blocks=blk - sp.reg_at,
                        since_start_blocks=ss, immune_until=Block(sp.reg_at + 864_000),
                        immune=blk < sp.reg_at + 864_000,
                        prune_rank=None if blk < sp.reg_at + 864_000 else 20)
            out[sp.key] = replace(base, **sp.feat) if sp.feat else base
        return out

    def frame(self, blk: int, feats: Mapping[SubnetKey, Feat] | None = None, *, warm: bool = True,
              prune: Mapping[str, Any] | None = None, emission: Mapping[str, Any] | None = None) -> FeatureFrame:
        pv = PruneView(prune_possible=True, target=BOTTOM, runtime_agrees=True, ladder=(BOTTOM,), bottom_ema=0.001,
                       blocks_since_reg=30_000, window_open=True, blocks_to_window=0, cost_ratio=1.3,
                       p_reg_ppm=((1_800, Ppm(5_000)), (7_200, Ppm(20_000)), (36_000, Ppm(100_000)), (50_400, Ppm(130_000))),
                       hazard_valid=True, immunity_calendar=())
        ev = EmissionView(theta=0.008, gate_rank=32, sum_ema=1.2, root_flag=True, parity_err_max_tao_day=0.0, model_ok=True)
        if prune:
            pv = replace(pv, **prune)
        if emission:
            ev = replace(ev, **emission)
        fs = dict(feats) if feats is not None else self.feats(blk)
        return FeatureFrame(block=Block(blk), warm=warm, feats=MappingProxyType(fs), prune=pv, emission=ev,
                            regime_id="test", universe_eligible=len(fs), beta_horizon_blocks=60, digest="x")

    def ctx(self, blk: int = B, *, sid: str = "carry", portfolio: Portfolio | None = None, budget_ppm: int = PPM,
            nav_tao: int = 1_000, book_view: BookView | None = None, events: Sequence[ChainEvent] = (),
            feats: Mapping[SubnetKey, Feat] | None = None, history_blocks: int = 8_400, mode: Mode = Mode.NORMAL,
            params: Mapping[str, object] | None = None, store: FakeStore | None = None,
            prune: Mapping[str, Any] | None = None, emission: Mapping[str, Any] | None = None,
            raw: ChainSnapshot | None = None) -> TickContext:
        snap = raw if raw is not None else self.snap(blk)
        st = store if store is not None else self.store(blk - 60, history_blocks)
        st.clock = Block(blk)
        st.add(snap)
        port = portfolio if portfolio is not None else Portfolio(cash=Rao(nav_tao * TAO), fee_float=Rao(TAO))
        return TickContext(block=Block(blk), raw=snap, view=snap, prev=None, events=tuple(events),
                           frame=self.frame(blk, feats, prune=prune, emission=emission), portfolio=port,
                           nav_liq=Rao(nav_tao * TAO),
                           sleeve=SleeveCfg(strategy=StrategyId(sid), stage=Stage.PAPER, budget_ppm=Ppm(budget_ppm),
                                            params=MappingProxyType(dict(params or {}))),
                           sleeve_value=Rao(0), mode=mode, store=st, book_view=book_view or make_book_view())


def make_book_view(**over: Any) -> BookView:
    base = BookView(orders=(), recent_fills=(), chase=(), delegates_free=("sim0", "sim1", "sim2"), delegate_locked_until=(),
                    fail_counts_600=(), fail_count_600_book=0, cooldowns=(), entries_halted_until=None,
                    recent_forced_exits=(), nav_liq_daily=(), sleeve_stats=(), router=RouterState())
    return replace(base, **over) if over else base


def holding(sid: str, key: SubnetKey, hotkey: Hotkey = HK1, alpha_tao: int = 1_000, cost_tao: float = 10.0,
            opened: int = B - 7_200, cash_tao: int = 990) -> Portfolio:
    """A portfolio where sleeve `sid` holds `alpha_tao` worth of shares (shares == alpha at index 1) on key/hotkey."""
    shares = Decimal(alpha_tao * TAO)
    cost = Rao(int(Decimal(str(cost_tao)) * TAO))
    return Portfolio(cash=Rao(cash_tao * TAO), fee_float=Rao(TAO),
                     positions=(Position(key=key, hotkey=hotkey, shares=shares, cost_tao=cost, opened_block=Block(opened)),),
                     sleeves=(SleeveHolding(strategy=StrategyId(sid), key=key, shares=shares, cost_tao=cost),),
                     sleeve_cash=((StrategyId(sid), Rao(cash_tao * TAO)),))


@pytest.fixture(scope="session")
def sx(make_subnet: Callable[..., SubnetState], make_snapshot: Callable[..., ChainSnapshot]) -> SimpleNamespace:
    def market(specs: Sequence[Spec], **kw: Any) -> Market:
        return Market(make_subnet, make_snapshot, specs, **kw)

    return SimpleNamespace(
        B=B, REF=REF, TAO=TAO, HK1=HK1, HK2=HK2, HK_OWNER=HK_OWNER, BOTTOM=BOTTOM, Spec=Spec, Market=Market,
        FakeStore=FakeStore, market=market, make_pool=make_pool, candidate=candidate, feat=feat,
        book_view=make_book_view, holding=holding, MetagraphLite=MetagraphLite, BlockHash=BlockHash,
    )
