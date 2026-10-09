"""Shared pytest configuration and fixtures (WP0).

- Hypothesis profile "taotrader": no deadline (Windows timer jitter), no example database, caches under
  .pytest_cache/hypothesis (nothing written into the repo root), deterministic per test (derandomize) so CI runs
  are reproducible. HYPOTHESIS_PROFILE=thorough runs more examples with random seeds.
- Network tests carry @pytest.mark.network and are deselected by default (pyproject addopts); run `-m network`.
- Golden fixtures (tests/fixtures/golden, captured by tools/capture_golden.py): `golden_dir`, `golden(name)`.
- Factories for core types with realistic defaults, for every WP's unit tests: `hk`, `make_pool`, `make_subnet`,
  `make_globals`, `make_snapshot`. Each returns a callable; keyword arguments override any dataclass field.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.configuration import set_hypothesis_home_dir

from taotrader.core.state import ChainGlobals, ChainSnapshot, PoolKind, PoolState, ReadPlan, SubnetState
from taotrader.core.units import AlphaRao, Block, BlockHash, Hotkey, NetUid, Rao, SubnetKey

# Hypothesis keeps caches under the git-ignored .pytest_cache instead of creating .hypothesis/ in the repo root.
set_hypothesis_home_dir(str(Path(__file__).resolve().parents[1] / ".pytest_cache" / "hypothesis"))
settings.register_profile("taotrader", deadline=None, database=None, derandomize=True, print_blob=True,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("thorough", deadline=None, database=None, max_examples=2_000, print_blob=True,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "taotrader"))

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    return GOLDEN_DIR


@pytest.fixture(scope="session")
def golden() -> Callable[[str], dict[str, Any]]:
    """golden("sn92_9240388") -> parsed fixture JSON (fails if the committed fixture is missing)."""
    cache: dict[str, dict[str, Any]] = {}

    def load(name: str) -> dict[str, Any]:
        if name not in cache:
            path = GOLDEN_DIR / (name if name.endswith(".json") else f"{name}.json")
            cache[name] = json.loads(path.read_text(encoding="utf-8"))
        return cache[name]

    return load


@pytest.fixture(scope="session")
def hk() -> Callable[[int], Hotkey]:
    """hk(7) -> Hotkey("0x000...0007"), a deterministic 32-byte hex account id."""
    return lambda i: Hotkey("0x" + f"{i:064x}")


@pytest.fixture(scope="session")
def make_pool() -> Callable[..., PoolState]:
    """make_pool(tao_tao=600.0, alpha_tao=...) or raw rao fields; Balancer 0.5/0.5, fee 33 by default."""

    def build(tao: int = 587_200_000_000, alpha: int = 435_000_000_000_000, *, w_quote_e18: int = 5 * 10**17,
              fee_rate: int = 33, kind: PoolKind = PoolKind.BALANCER, px_tao: int | None = None,
              px_alpha: int | None = None) -> PoolState:
        return PoolState(kind=kind, tao=Rao(tao), alpha=AlphaRao(alpha), px_tao=tao if px_tao is None else px_tao,
                         px_alpha=alpha if px_alpha is None else px_alpha, w_quote_e18=w_quote_e18, fee_rate=fee_rate)

    return build


@pytest.fixture(scope="session")
def make_subnet(make_pool: Callable[..., PoolState]) -> Callable[..., SubnetState]:
    """make_subnet(92, reg_at=8_355_590, **field_overrides) -> a started, emitting, tradable SubnetState."""

    def build(netuid: int = 92, reg_at: int = 8_000_000, **overrides: Any) -> SubnetState:
        base = SubnetState(
            key=SubnetKey(NetUid(netuid), Block(reg_at)),
            pool=make_pool(),
            alpha_out=AlphaRao(630_980_000_000_000),
            protocol_alpha=AlphaRao(41_000_000_000_000),
            moving_price=Decimal("0.0013482"),
            root_prop=Decimal("0.479"),
            miner_burned=Decimal(0),
            emission_enabled=True,
            subtoken_enabled=True,
            reg_allowed=True,
            first_emission_block=Block(reg_at + 600),
            tempo=360,
            last_epoch_block=Block(reg_at + 720),
            ema_halving_blocks=201_600,
            tao_in_emission=Rao(4_468),
            excess_tao=Rao(0),
            alpha_out_emission=AlphaRao(1_000_000_000),
            alpha_in_emission=AlphaRao(3_314_091),
        )
        return replace(base, **overrides) if overrides else base

    return build


@pytest.fixture(scope="session")
def make_globals() -> Callable[..., ChainGlobals]:
    """make_globals(**overrides) -> ChainGlobals with the live (spec-475) parameter values of the brief."""

    def build(**overrides: Any) -> ChainGlobals:
        base = ChainGlobals(
            spec_version=475, tx_version=1, total_issuance=Rao(11_597_600 * 10**9), block_emission=Rao(500_000_000),
            moving_alpha=Decimal(1_288_490) / Decimal(2**32), gate_bar=Decimal("0.0082624"), gate_rank=32,
            gate_exponent=3, tao_weight=Decimal("0.18"), root_tao=Rao(5_454_000 * 10**9), owner_cut_u16=11_796,
            subnet_limit=128, immunity_period=864_000, network_rate_limit=14_400, last_reg_block=Block(9_210_610),
            last_lock_cost=Rao(653_020_000_000), min_lock_cost=Rao(10**9), lock_reduction_interval=115_200,
            tao_in_refund_block=Block(8_334_450), nominator_min_stake=Rao(20_000_000), cleanup_queue_len=0,
            n_nonroot_networks=128, safe_mode_until=None)
        return replace(base, **overrides) if overrides else base

    return build


@pytest.fixture(scope="session")
def make_snapshot(make_globals: Callable[..., ChainGlobals]) -> Callable[..., ChainSnapshot]:
    """make_snapshot(block, subnets=(...), plan=ReadPlan.FULL, glob=None, **glob_overrides) -> ChainSnapshot
    (subnets sorted by netuid; the block hash is derived from the block number)."""

    def build(block: int = 9_240_388, subnets: Sequence[SubnetState] = (), *, plan: ReadPlan = ReadPlan.FULL,
              glob: ChainGlobals | None = None, timestamp_ms: int = 1_759_900_000_000, **glob_overrides: Any
              ) -> ChainSnapshot:
        g = glob if glob is not None else make_globals(**glob_overrides)
        return ChainSnapshot(block=Block(block), block_hash=BlockHash("0x" + f"{block:064x}"), timestamp_ms=timestamp_ms,
                             plan=plan, glob=g, subnets=tuple(sorted(subnets, key=lambda s: int(s.key.netuid))))

    return build
