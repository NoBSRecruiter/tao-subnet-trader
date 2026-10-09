"""WP5 test fixtures: a frozen calibration provider, deterministic synthetic snapshot series, and decoders that turn
the golden SN70 captures and the captured registration sequences (tests/features/fixtures) into ChainSnapshots.

Test modules cannot import each other (importlib mode), so every helper is exposed as a fixture.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taotrader.chain import items as it
from taotrader.chain.hashing import from_hex
from taotrader.core.state import ChainGlobals, ChainSnapshot, HotkeyIdx, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import AlphaRao, Block, BlockHash, Coldkey, Hotkey, NetUid, Rao, SubnetKey
from taotrader.features.engine import FeatureEngine, FeatureParams
from taotrader.protocol.calibration import Calibration, sealed
from taotrader.protocol.prune import HazardModel, hazard_from_table
from taotrader.protocol.sellload import SellLoadParams

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "fixtures" / "golden"
GK_FIXTURE = HERE / "fixtures" / "gatekeeper_registrations.json"
D = Decimal


# ------------------------------------------------------------------------------------------------- calibration
class FrozenProvider:
    """CalibrationProvider returning one calibration (the preregistered section 3.3 table), as of block 0."""

    def __init__(self, cal: Calibration) -> None:
        self.cal = cal
        self.calls: list[int] = []

    def asof(self, block: Block) -> Calibration:
        self.calls.append(int(block))
        return self.cal


def _prereg_hazard() -> HazardModel:
    r = [D(x) for x in ("1.75", "1.462", "1.253", "1.132", "1.045", "0.958", "0.872", "0.767", "0.266")]
    f = [D(x) for x in ("0.0625", "0.09", "0.19", "0.31", "0.50", "0.72", "0.84", "0.94", "1.0")]
    return hazard_from_table(r, f, 32, n0=4, rate_limit_blocks=14_400, i_eff_blocks=57_600, prior_scale_blocks=43_200)


@pytest.fixture(scope="session")
def calibration() -> Calibration:
    return sealed(Calibration(asof=Block(0), hazard=_prereg_hazard(), kappa_p=D(4), r_default=D("0.35"), r_cap_formula=False,
                              tier_b_jump_p_day=D("0.03"), tier_b_jump_size=D("-0.6931471805599453"), phi=SellLoadParams(),
                              digest=""))


@pytest.fixture()
def provider(calibration: Calibration) -> FrozenProvider:
    return FrozenProvider(calibration)


@pytest.fixture(scope="session")
def provider_cls() -> type[FrozenProvider]:
    return FrozenProvider


@pytest.fixture()
def make_engine(calibration: Calibration) -> Callable[..., FeatureEngine]:
    def build(params: FeatureParams | None = None) -> FeatureEngine:
        return FeatureEngine(FrozenProvider(calibration), params)
    return build


# ------------------------------------------------------------------------------------------------- synthetic series
def hk_of(i: int) -> Hotkey:
    return Hotkey("0x" + f"{i:064x}")


@dataclass(frozen=True)
class HK:
    """A tracked hotkey of a synthetic subnet: alpha grows by `growth` per epoch at constant shares."""
    ident: int
    alpha: int                       # whole alpha at epoch 0
    growth: float = 0.0              # per-epoch index growth
    take: int = 0
    childkey: int = 0
    earns: bool = True


@dataclass(frozen=True)
class Spec:
    """A deterministic synthetic subnet generation. Price p(b) = p0 * exp(vol * sin(b / period + netuid))."""
    netuid: int
    reg_at: int = 7_000_000
    tao: int = 1_000 * 10**9
    p0: float = 0.003
    vol: float = 0.0
    period: float = 977.0
    moving_price: str = "0.003"
    flow_per_block: int | None = 50_000_000          # SubnetTaoFlow increment per block; None = item absent
    flow_wave: int = 0                               # + flow_wave * sin(block / 4,999 + netuid) (rao)
    started: bool = True
    tempo: int = 360
    epoch_origin: int = 9_000_000                    # hotkey alpha = alpha * (1 + growth) ** (epochs since this block)
    burned: str = "0"
    emission_enabled: bool = True
    reg_allowed: bool = True
    subtoken: bool = True
    w_quote_e18: int = 5 * 10**17
    fee_rate: int = 33
    alpha_out_emission: int = 10**9
    root_prop: str = "0.4"
    max_validators: int | None = 64
    escrow: int | None = None
    owner_alpha: int | None = None
    owner_hotkey_ident: int | None = None
    autolock: bool | None = None
    hotkeys: tuple[HK, ...] = field(default_factory=lambda: (HK(1, 200_000, 0.00029, 0), HK(2, 100_000, 0.00023, 11_796)))

    def key(self) -> SubnetKey:
        return SubnetKey(NetUid(self.netuid), Block(self.reg_at))

    def price(self, block: int) -> float:
        return self.p0 * math.exp(self.vol * math.sin(block / self.period + self.netuid))

    def epoch_block(self, block: int) -> int:
        """LastEpochBlock: drains at blocks b with (b - netuid) % tempo == 0 (LastEpochBlock steps by Tempo)."""
        return block - (block - self.netuid) % self.tempo


def synth_subnet(spec: Spec, block: int, **overrides: Any) -> SubnetState:
    p = spec.price(block)
    alpha_in = int(spec.tao * (10**18 - spec.w_quote_e18) / (spec.w_quote_e18 * p))
    pool = PoolState(PoolKind.BALANCER, Rao(spec.tao), AlphaRao(alpha_in), spec.tao, alpha_in, spec.w_quote_e18, spec.fee_rate)
    e = spec.epoch_block(block)
    m = (e - spec.epoch_origin) // spec.tempo
    hks = []
    for h in spec.hotkeys:
        shares = D(h.alpha) * D(10**9)
        total = int(h.alpha * 10**9 * (1.0 + h.growth) ** m)
        hks.append(HotkeyIdx(hotkey=hk_of(spec.netuid * 1_000 + h.ident), total_alpha=AlphaRao(total), total_shares=shares,
                             take_u16=h.take, childkey_take_u16=h.childkey, earns=h.earns,
                             last_dividend=AlphaRao(10**9 if h.earns else 0)))
    owner_hk = hk_of(spec.netuid * 1_000 + spec.owner_hotkey_ident) if spec.owner_hotkey_ident is not None else None
    s = SubnetState(
        key=spec.key(), pool=pool, alpha_out=AlphaRao(630_980 * 10**9), protocol_alpha=AlphaRao(41_000 * 10**9),
        moving_price=D(spec.moving_price), root_prop=D(spec.root_prop), miner_burned=D(spec.burned),
        emission_enabled=spec.emission_enabled, subtoken_enabled=spec.subtoken, reg_allowed=spec.reg_allowed,
        first_emission_block=Block(spec.reg_at + 600) if spec.started else None, tempo=spec.tempo,
        last_epoch_block=Block(e), ema_halving_blocks=201_600, tao_in_emission=Rao(4_468), excess_tao=Rao(1_000),
        alpha_out_emission=AlphaRao(spec.alpha_out_emission), alpha_in_emission=AlphaRao(3_314_091),
        tao_flow_cum=None if spec.flow_per_block is None else (
            spec.flow_per_block * (block - spec.reg_at) + int(spec.flow_wave * math.sin(block / 4_999 + spec.netuid))),
        escrow_alpha=None if spec.escrow is None else AlphaRao(spec.escrow),
        owner_alpha=None if spec.owner_alpha is None else AlphaRao(spec.owner_alpha),
        owner_hotkey=owner_hk, owner_coldkey=Coldkey(hk_of(999_999)) if owner_hk is not None else None,
        owner_cut_autolock=spec.autolock,
        max_allowed_validators=spec.max_validators, hotkeys=tuple(sorted(hks, key=lambda h: h.hotkey)))
    return replace(s, **overrides) if overrides else s


@pytest.fixture(scope="session")
def synth() -> Callable[..., SubnetState]:
    return synth_subnet


@pytest.fixture(scope="session")
def spec_cls() -> type[Spec]:
    return Spec


@pytest.fixture(scope="session")
def hk_cls() -> type[HK]:
    return HK


@pytest.fixture(scope="session")
def hkey() -> Callable[[int], Hotkey]:
    return hk_of


@pytest.fixture(scope="session")
def snap_of(make_globals: Callable[..., ChainGlobals]) -> Callable[..., ChainSnapshot]:
    """snap_of(block, subnets, plan=FULL, **glob_overrides): a snapshot at `block` (n_nonroot from the subnets)."""

    def build(block: int, subnets: Sequence[SubnetState], *, plan: ReadPlan = ReadPlan.FULL, **glob_overrides: Any) -> ChainSnapshot:
        kw: dict[str, Any] = {"n_nonroot_networks": 128}
        kw.update(glob_overrides)
        return ChainSnapshot(block=Block(block), block_hash=BlockHash("0x" + f"{block:064x}"), timestamp_ms=12_000 * block,
                             plan=plan, glob=make_globals(**kw), subnets=tuple(sorted(subnets, key=lambda s: int(s.key.netuid))))

    return build


@pytest.fixture(scope="session")
def series(snap_of: Callable[..., ChainSnapshot]) -> Callable[..., list[ChainSnapshot]]:
    """series(specs, start, end, step, **glob_overrides): one snapshot every `step` blocks in [start, end)."""

    def build(specs: Sequence[Spec], start: int, end: int, step: int = 60, **glob_overrides: Any) -> list[ChainSnapshot]:
        return [snap_of(b, [synth_subnet(sp, b) for sp in specs if sp.reg_at <= b], **glob_overrides)
                for b in range(start, end, step)]

    return build


# ------------------------------------------------------------------------------------------------- golden decoders
_ROWS = {r.name: r for r in it.ALL_ROWS}


def _dec(item: str, value: str | None) -> Any:
    row = _ROWS[item]
    return None if value is None else row.decode(from_hex(value))


def _subnet_values(snap: dict[str, Any], netuid: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for e in snap["storage"]:
        if e["args"] and e["args"][0] == netuid and len(e["args"]) == 1 and e["item"] in _ROWS:
            out[e["item"].split(".")[1]] = _dec(e["item"], e["value"])
    return out


def _panel(snap: dict[str, Any], netuid: int) -> dict[str, dict[str, Any]]:
    hk: dict[str, dict[str, Any]] = {}
    takes: dict[str, int] = {}
    for e in snap["storage"]:
        item, a = e["item"], e["args"]
        name = item.split(".")[1]
        if name in ("TotalHotkeyAlpha", "TotalHotkeyShares", "TotalHotkeySharesV2", "ChildkeyTake") and a[1] == netuid:
            hk.setdefault(a[0], {})[name] = _dec(item, e["value"])
        elif name == "AlphaDividendsPerSubnet" and a[0] == netuid:
            hk.setdefault(a[1], {})["div"] = _dec(item, e["value"])
        elif name == "Delegates":
            takes[a[0]] = 11_796 if e["value"] is None else int(_dec(item, e["value"]))
    for h, v in hk.items():
        v["take"] = takes.get(h, 11_796)
    return hk


def _hotkey_idx(h: str, v: dict[str, Any]) -> HotkeyIdx:
    shares = v.get("TotalHotkeyShares") if v.get("TotalHotkeyShares") is not None else v.get("TotalHotkeySharesV2")
    assert shares is not None
    return HotkeyIdx(hotkey=Hotkey(h), total_alpha=AlphaRao(int(v["TotalHotkeyAlpha"])), total_shares=D(shares),
                     take_u16=int(v["take"]), childkey_take_u16=int(v.get("ChildkeyTake") or 0), earns=v.get("div") is not None,
                     last_dividend=AlphaRao(int(v["div"]) if v.get("div") is not None else 0))


@dataclass(frozen=True)
class SN70:
    base: SubnetState                     # SN70 at 9,240,388 (yield_inputs) without hotkeys
    pre: tuple[HotkeyIdx, ...]            # 49 recipients at 9,240,581 (before the drain)
    post: tuple[HotkeyIdx, ...]           # the same at 9,240,582 (LastEpochBlock)
    drain_block: int
    take0_top: Hotkey                     # 0x56a9...: the brief's take-0 earner


@pytest.fixture(scope="session")
def sn70(golden: Callable[[str], dict[str, Any]]) -> SN70:
    y = golden("yield_inputs_9240388")["snapshots"][0]
    v = _subnet_values(y, 70)
    pool = PoolState(PoolKind.BALANCER, Rao(int(v["SubnetTAO"])), AlphaRao(int(v["SubnetAlphaIn"])), int(v["SubnetTAO"]),
                     int(v["SubnetAlphaIn"]), int(v["SwapBalancer"]) if v.get("SwapBalancer") is not None else 5 * 10**17,
                     int(v["FeeRate"]) if v.get("FeeRate") is not None else 33)
    base = SubnetState(
        key=SubnetKey(NetUid(70), Block(int(v["NetworkRegisteredAt"]))), pool=pool, alpha_out=AlphaRao(int(v["SubnetAlphaOut"])),
        protocol_alpha=AlphaRao(int(v.get("SubnetProtocolAlpha") or 0)), moving_price=v["SubnetMovingPrice"],
        root_prop=v["RootProp"], miner_burned=v.get("MinerBurned") or D(0), emission_enabled=True,
        subtoken_enabled=True, reg_allowed=True, first_emission_block=Block(int(v["FirstEmissionBlockNumber"])),
        tempo=int(v["Tempo"]), last_epoch_block=Block(int(v["LastEpochBlock"])), ema_halving_blocks=201_600,
        tao_in_emission=Rao(int(v.get("SubnetTaoInEmission") or 0)), excess_tao=Rao(int(v.get("SubnetExcessTao") or 0)),
        alpha_out_emission=AlphaRao(int(v["SubnetAlphaOutEmission"])),
        alpha_in_emission=AlphaRao(int(v.get("SubnetAlphaInEmission") or 0)),
        max_allowed_validators=int(v["MaxAllowedValidators"]))
    d = golden("sn70_index_9240222_9240582")["snapshots"]
    pre = tuple(sorted((_hotkey_idx(h, x) for h, x in _panel(d[1], 70).items()), key=lambda h: h.hotkey))
    post = tuple(sorted((_hotkey_idx(h, x) for h, x in _panel(d[2], 70).items()), key=lambda h: h.hotkey))
    top = next(h.hotkey for h in post if h.hotkey.startswith("0x56a9"))
    return SN70(base=base, pre=pre, post=post, drain_block=int(d[2]["block"]), take0_top=top)


# ------------------------------------------------------------------------------------------------- gatekeeper fixture
@dataclass(frozen=True)
class Registration:
    queued_block: int
    victim_netuid: int
    added_block: int
    lag: int
    spec_version: int
    snapshots: tuple[ChainSnapshot, ...]   # Q-1, Q, A-1, A


@pytest.fixture(scope="session")
def registrations(registration_loader: Callable[[dict[str, Any]], tuple[Registration, ...]]) -> tuple[Registration, ...]:
    return registration_loader(json.loads(GK_FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="session")
def registration_loader(make_globals: Callable[..., ChainGlobals],
                        make_subnet: Callable[..., SubnetState]) -> Callable[[dict[str, Any]], tuple[Registration, ...]]:
    def load(doc: dict[str, Any]) -> tuple[Registration, ...]:
        return _load_registrations(doc, make_globals, make_subnet)
    return load


def _load_registrations(doc: dict[str, Any], make_globals: Callable[..., ChainGlobals],
                        make_subnet: Callable[..., SubnetState]) -> tuple[Registration, ...]:
    sf: list[str] = doc["subnet_fields"]
    out = []
    for r in doc["registrations"]:
        snaps = []
        for s in r["snapshots"]:
            g = s["globals"]
            subnets = []
            for n, row in sorted(s["subnets"].items(), key=lambda kv: int(kv[0])):
                v = dict(zip(sf, row, strict=True))
                if not v["added"]:
                    continue
                pool = PoolState(PoolKind.BALANCER, Rao(v["tao"]), AlphaRao(v["alpha_in"]), v["tao"], v["alpha_in"], v["w_quote_e18"],
                                 v["fee_rate"] if v["fee_rate"] is not None else 33)
                fe = v["first_emission_block"]
                subnets.append(make_subnet(int(n), v["reg_at"], pool=pool, moving_price=D(v["moving_price"]),
                                           first_emission_block=None if fe is None else Block(fe),
                                           emission_enabled=v["emission_enabled"], subtoken_enabled=v["subtoken_enabled"],
                                           reg_allowed=v["reg_allowed"], miner_burned=D(v["miner_burned"]),
                                           last_epoch_block=Block(max(v["reg_at"], s["block"] - 100))))
            glob = make_globals(spec_version=s["spec_version"], total_issuance=Rao(g["total_issuance"]),
                                immunity_period=g["immunity_period"], subnet_limit=g["subnet_limit"],
                                network_rate_limit=g["network_rate_limit"], last_reg_block=Block(g["last_reg_block"]),
                                last_lock_cost=Rao(g["last_lock_cost"]), min_lock_cost=Rao(g["min_lock_cost"]),
                                lock_reduction_interval=g["lock_reduction_interval"], cleanup_queue_len=g["cleanup_queue_len"],
                                n_nonroot_networks=len(subnets))
            snaps.append(ChainSnapshot(block=Block(s["block"]), block_hash=BlockHash(s["block_hash"]), timestamp_ms=g["timestamp_ms"],
                                       plan=ReadPlan.FULL, glob=glob, subnets=tuple(subnets)))
        out.append(Registration(queued_block=r["queued_block"], victim_netuid=r["victim_netuid"], added_block=r["added_block"],
                                lag=r["lag"], spec_version=r["snapshots"][0]["spec_version"], snapshots=tuple(snaps)))
    return tuple(out)
