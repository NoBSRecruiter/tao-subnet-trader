"""WP10 unit-test fixtures over the committed mini-lake (tests/fixtures/minilake; real chain data, gate rank-32 era).

- `minilake`: (lake dir, state db) of the committed fixture.
- `short_plan`: the committed books file with a short window at the start of the mini-lake (8,765,400 -> 8,776,000;
  frames warm after one day, when the yield router has the ~20 recorded epochs every entry needs) - real snapshots,
  ~177 ticks, books trade in the last ~3,000 blocks.
- `short_pass`: ONE session-scoped pass of the short window with base-cash, base-ew-total, base-ew-price and carry,
  plus the generation-keyed panel (studies.PanelRecorder) - shared by the metrics, studies and reports tests.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from taotrader.backtest.books import BacktestPlan, load_backtest_plan
from taotrader.backtest.runner import PassResult, run_pass
from taotrader.backtest.studies import PanelRecorder
from taotrader.data.lake import Lake

MINILAKE = Path(__file__).resolve().parents[1] / "fixtures" / "minilake"
SHORT_START = 8_765_400
SHORT_END = 8_776_000
SHORT_WARM = 7_200        # the router needs ~20 recorded epochs before any entry (universe section G)
SHORT_BOOKS = ("base-cash", "base-ew-price", "base-ew-total", "carry")


@pytest.fixture(scope="session")
def minilake() -> tuple[Path, Path]:
    lake_dir, state = MINILAKE / "lake", MINILAKE / "state.sqlite"
    if not (lake_dir.is_dir() and state.is_file()):
        pytest.fail("the committed mini-lake fixture is missing (tests/fixtures/minilake/build_minilake.py)")
    return lake_dir, state


@pytest.fixture()
def open_lake(minilake: tuple[Path, Path]) -> Iterator[Lake]:
    lake = Lake(*minilake)
    try:
        yield lake
    finally:
        lake.close()


def make_short_plan(minilake: tuple[Path, Path], *extra: str) -> BacktestPlan:
    return load_backtest_plan(env={}, cli=[f'backtest.lake="{minilake[0].as_posix()}"',
                                           f"backtest.start_block={SHORT_START}", f"backtest.end_block={SHORT_END}",
                                           "backtest.warmup_blocks=0", f"backtest.feature_warm_blocks={SHORT_WARM}", *extra])


@pytest.fixture(scope="session")
def short_plan(minilake: tuple[Path, Path]) -> BacktestPlan:
    return make_short_plan(minilake)


@pytest.fixture(scope="session")
def short_pass(minilake: tuple[Path, Path], short_plan: BacktestPlan) -> tuple[PassResult, PanelRecorder]:
    lake = Lake(*minilake)
    rec = PanelRecorder()
    try:
        res = run_pass(short_plan, lake, books=list(SHORT_BOOKS), on_tick=rec)
    finally:
        lake.close()
    return res, rec


@pytest.fixture(scope="session")
def short_plan_factory(minilake: tuple[Path, Path]) -> Any:
    return lambda *extra: make_short_plan(minilake, *extra)
