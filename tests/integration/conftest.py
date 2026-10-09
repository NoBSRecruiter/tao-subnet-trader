"""Section 10.3 integration suite fixtures (WP10): the committed mini-lake and the SN116 prune-window lake.

- GOLDEN: blocks 8,765,400 -> 8,830,000 of the mini-lake (gate rank-32 era; ~1,073 snapshots at 60-block stride),
  every section 2.5 baseline book + the carry MVP, run ONCE per session through the production Runner
  (`golden` fixture, journal in a temporary SQLite file). Mini-lake warm-up deviation: the fixture holds no data
  before 8,765,400, so the 30-day warm-up of section 8.1 is replaced by feature_warm_blocks = 7,200 (frames turn warm
  one day into the window; nothing trades before) - recorded in the WP10 report.
- SHORT window 8,765,400 -> 8,776,000 (frames warm after one day, when the yield router has the ~20 recorded epochs
  every entry needs; books trade in its last ~3,000 blocks) for the multi-run tests (determinism, crash matrix, as-of
  perturbation, truncation, chaos), ~30 s per run.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from taotrader.backtest.books import BacktestPlan, load_backtest_plan
from taotrader.backtest.runner import PassResult, run_pass
from taotrader.backtest.studies import PanelRecorder
from taotrader.data.journal import SqliteJournal
from taotrader.data.lake import Lake

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "minilake"
MINILAKE = (FIXTURES / "lake", FIXTURES / "state.sqlite")
PRUNE_LAKE = (FIXTURES / "prune116" / "lake", FIXTURES / "prune116" / "state.sqlite")
GOLDEN_DIGESTS = Path(__file__).resolve().parent / "golden_minilake_digests.json"
GOLDEN_START = 8_765_400
GOLDEN_END = 8_830_000
GOLDEN_WARM = 7_200
GOLDEN_BOOKS = ("base-cash", "base-ew-price", "base-ew-total", "base-yield-size", "base-prune-blind", "base-random",
                "carry")
SHORT_START = 8_765_400
SHORT_END = 8_776_000
SHORT_WARM = 7_200


def plan_for(lake_dir: Path, start: int, end: int, warm: int, *extra: str) -> BacktestPlan:
    return load_backtest_plan(env={}, cli=[f'backtest.lake="{lake_dir.as_posix()}"', f"backtest.start_block={start}",
                                           f"backtest.end_block={end}", "backtest.warmup_blocks=0",
                                           f"backtest.feature_warm_blocks={warm}", *extra])


@pytest.fixture(scope="session")
def minilake() -> tuple[Path, Path]:
    if not (MINILAKE[0].is_dir() and MINILAKE[1].is_file()):
        pytest.fail("the committed mini-lake is missing (tests/fixtures/minilake/build_minilake.py)")
    return MINILAKE


@pytest.fixture()
def lake(minilake: tuple[Path, Path]) -> Iterator[Lake]:
    lk = Lake(*minilake)
    try:
        yield lk
    finally:
        lk.close()


@pytest.fixture(scope="session")
def golden_plan(minilake: tuple[Path, Path]) -> BacktestPlan:
    return plan_for(minilake[0], GOLDEN_START, GOLDEN_END, GOLDEN_WARM)


@pytest.fixture(scope="session")
def short_plan(minilake: tuple[Path, Path]) -> BacktestPlan:
    return plan_for(minilake[0], SHORT_START, SHORT_END, SHORT_WARM)


@pytest.fixture(scope="session")
def golden(minilake: tuple[Path, Path], golden_plan: BacktestPlan,
           tmp_path_factory: pytest.TempPathFactory) -> tuple[PassResult, PanelRecorder, Path]:
    """The golden replay (run once per session): all baseline books + carry over the whole mini-lake."""
    path = tmp_path_factory.mktemp("golden") / "journal.sqlite"
    journal = SqliteJournal(str(path), durable=False)
    lk = Lake(*minilake)
    rec = PanelRecorder()
    try:
        res = run_pass(golden_plan, lk, books=list(GOLDEN_BOOKS), journal=journal, on_tick=rec)
    finally:
        lk.close()
        journal.close()
    return res, rec, path


def short_books() -> list[str]:
    return ["base-ew-total", "base-random", "carry"]


def run_short(plan: BacktestPlan, lake_: Lake, **kw: Any) -> PassResult:
    kw.setdefault("books", short_books())
    return run_pass(plan, lake_, **kw)


@pytest.fixture(scope="session")
def itx() -> Any:
    """Helpers and constants for the integration modules (importlib mode: test modules cannot import conftest)."""
    from types import SimpleNamespace
    return SimpleNamespace(plan_for=plan_for, run_short=run_short, short_books=short_books, MINILAKE=MINILAKE,
                           PRUNE_LAKE=PRUNE_LAKE, GOLDEN_DIGESTS=GOLDEN_DIGESTS, GOLDEN_START=GOLDEN_START,
                           GOLDEN_END=GOLDEN_END, GOLDEN_WARM=GOLDEN_WARM, GOLDEN_BOOKS=GOLDEN_BOOKS,
                           SHORT_START=SHORT_START, SHORT_END=SHORT_END, SHORT_WARM=SHORT_WARM)
