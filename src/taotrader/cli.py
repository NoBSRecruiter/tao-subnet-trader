"""taotrader/cli.py - the `taotrader` command line (WP12; DESIGN.md sections 9, 11 WP12 and 12).

    taotrader <command> [options]          (python -m taotrader <command> ... is equivalent)

Data:      collect, refine, verify (--spec / --taostats), verify-metadata, verify-journal, verify-lake
Research:  backtest, grid, study, report
Running:   paper, replay
Operator:  halt, resume, exits-only, flatten <netuid>, clear-quarantine --book B --reason R (run stopped)
Live:      live arm, live --live [--submit]          (Linux/WSL only; user-run)
Health:    doctor [--live]

Every command loads its configuration through ops.config_load (config/default.toml first, then the command's own
files or the --config files in order, then TAOTRADER_CFG_* environment, then --set path=value overrides) and fails
closed: a bad argument, a bad config value or a missing secret exits non-zero before anything runs.

Exit codes: 0 ok, 1 a check found problems, 2 usage / configuration / missing secret, 3 live gate refused,
4 another instance holds the lock, 5 replay divergence, 130 interrupted.

Safety: paper is the default way to run strategies. `taotrader.live` is imported only inside `live`, and only AFTER
locks 1 and 3 (config `[live] enabled = true` and the `--live` flag) pass in this module, which never imports
`taotrader.live` otherwise; bittensor itself is imported only by live.sdk_port on the live host. Nothing in this
module signs or submits anything; `live --live --submit` hands submission to LiveVenue under the four locks.
Secrets are read through ops.secrets (keyring/env/secrets.env), never from argv, and logs pass the redaction filter.

`main(argv: Sequence[str] | None = None) -> int` returns the exit code (the console script and
`taotrader.__init__.main` call it).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar

from .ops import config_load
from .ops.config_load import CONFIG_DIR, DEFAULT_CONFIG, ConfigError
from .ops.secrets import KNOWN as KNOWN_SECRETS
from .ops.secrets import SecretError, get_secret

if TYPE_CHECKING:
    from .chain.head import LiveChainFeed
    from .chain.reader import JsonRpcChainReader
    from .chain.rpc import RpcPool
    from .core.config import BookCfg, RunCfg
    from .core.protocols import RiskOverlay, SourceItem
    from .core.state import ChainSnapshot
    from .data.journal import SqliteJournal
    from .data.recorder import Recorder
    from .engine.engine import Engine
    from .engine.recovery import BookRuntime
    from .engine.runner import Runner
    from .ops.alerts import AlertManager
    from .ops.health import BookStatus, HealthWriter
    from .protocol.calibration import CalibrationProvider

__all__ = ["EXIT_DIVERGENCE", "EXIT_FAIL", "EXIT_GATE", "EXIT_LOCKED", "EXIT_OK", "EXIT_USAGE", "PaperPlan", "build_parser",
           "load_paper_plan", "main"]

log = logging.getLogger("taotrader.cli")

EXIT_OK: Final[int] = 0
EXIT_FAIL: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_GATE: Final[int] = 3
EXIT_LOCKED: Final[int] = 4
EXIT_DIVERGENCE: Final[int] = 5
EXIT_INTERRUPTED: Final[int] = 130

BOOKS_PAPER: Final[Path] = CONFIG_DIR / "books.paper.toml"
BOOKS_BACKTEST: Final[Path] = CONFIG_DIR / "books.backtest.toml"
PAPER_TABLE: Final[str] = "paper"
EXTENSION_TABLES: Final[tuple[str, ...]] = ("paper", "backtest")
IDLE_PROXY_HOURS: Final[int] = 72
ARM_STATE_FILE: Final[str] = "arm_state.json"
MAX_ARM_TTL_HOURS: Final[int] = 24
SPEC_V2_SUBNETS: Final[int] = 3
SPEC_V4_TOL_RAO: Final[int] = 100_000            # 1e-4 TAO per block per subnet (section 9.5 V4)
TAOSTATS_TOL: Final[float] = 0.01                # 1% price cross-check tolerance (diagnostic, never a decision input)
RAO_PER_TAO: Final[int] = 10**9
FULL_CADENCE_BLOCKS: Final[int] = 60             # FULL-plan cadence of the live feed (checkpoint re-warm margin)

T = TypeVar("T")


class CliError(Exception):
    """A fail-closed refusal with an exit code; the message is printed to stderr."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# ================================================================================================= config loading
def _paths(a: argparse.Namespace, defaults: Sequence[Path]) -> list[Path]:
    """config/default.toml first, then --config files (in order) or the command's default files."""
    extra = [Path(p) for p in (a.config or [])] or [Path(p) for p in defaults]
    out = [DEFAULT_CONFIG]
    for p in extra:
        if p.resolve() != DEFAULT_CONFIG.resolve():
            out.append(p)
    return out


def _raw_config(paths: Sequence[Path], sets: Sequence[str], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for p in paths:
        raw = config_load.deep_merge(raw, config_load.read_toml(p))
    raw = config_load.apply_overrides(raw, config_load.env_overrides(os.environ if env is None else env),
                                      case_insensitive=True)
    return config_load.apply_overrides(raw, config_load.cli_overrides(sets), case_insensitive=False)


def load_config(paths: Sequence[Path], sets: Sequence[str] = (), *, env: Mapping[str, str] | None = None,
                strip: Sequence[str] = EXTENSION_TABLES) -> RunCfg:
    """A strictly validated RunCfg; the command-specific extension tables ([paper], [backtest]) are set aside."""
    raw = _raw_config(paths, sets, env)
    for t in strip:
        raw.pop(t, None)
    return config_load.build_run_config(raw)


def _cfg(a: argparse.Namespace, defaults: Sequence[Path] = ()) -> RunCfg:
    return load_config(_paths(a, defaults), a.set or ())


@dataclass(frozen=True, slots=True)
class PaperPlan:
    """config/books.paper.toml: the RunCfg (mode = "paper") plus its [paper] table (validated key by key)."""
    run: RunCfg
    lake: str = "data/paper/lake"
    hot: str = "data/paper/hot"
    feature_warm_blocks: int = 216_000
    checkpoint_every_blocks: int = 7_200
    status_interval_s: float = 5.0
    membership_every_blocks: int = 7_200
    top_n_tracked: int = 5
    dead_man_interval_s: float = 60.0
    sources: tuple[str, ...] = ()


_PAPER_KEYS: Final[Mapping[str, type]] = {
    "lake": str, "hot": str, "feature_warm_blocks": int, "checkpoint_every_blocks": int, "status_interval_s": float,
    "membership_every_blocks": int, "top_n_tracked": int, "dead_man_interval_s": float}


def load_paper_plan(paths: Sequence[Path], sets: Sequence[str] = (), *, env: Mapping[str, str] | None = None,
                    mode: str = "paper") -> PaperPlan:
    raw = _raw_config(paths, sets, env)
    table = raw.pop(PAPER_TABLE, {})
    raw.pop("backtest", None)
    if not isinstance(table, dict):
        raise ConfigError("paper: expected a table")
    unknown = sorted(set(table) - set(_PAPER_KEYS))
    if unknown:
        raise ConfigError(f"paper: unknown key(s) {unknown}")
    vals: dict[str, Any] = {}
    for k, v in table.items():
        want = _PAPER_KEYS[k]
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ConfigError(f"paper.{k}: expected a non-negative integer")
        if want is float:
            if isinstance(v, bool) or not isinstance(v, int | float) or v < 0:
                raise ConfigError(f"paper.{k}: expected a non-negative number")
            v = float(v)
        if want is str and (not isinstance(v, str) or not v):
            raise ConfigError(f"paper.{k}: expected a path string")
        vals[k] = v
    run = config_load.build_run_config(raw)
    if run.mode.value != mode:
        raise ConfigError(f"mode: this command needs mode = {mode!r} (got {run.mode.value!r})")
    if not run.books:
        raise ConfigError("books: a paper run needs at least one [[books]] entry")
    if vals.get("membership_every_blocks", 1) == 0 or vals.get("top_n_tracked", 1) == 0:
        raise ConfigError("paper.membership_every_blocks and paper.top_n_tracked must be >= 1")
    return PaperPlan(run=run, sources=tuple(str(p) for p in paths), **vals)


def _paper_plan(a: argparse.Namespace) -> PaperPlan:
    return load_paper_plan(_paths(a, (BOOKS_PAPER,)), a.set or ())


def _run_dir(cfg: RunCfg, run_id: str | None = None) -> Path:
    return Path(cfg.data_dir) / "runs" / (run_id or cfg.run_id)


def _control_dir(cfg: RunCfg) -> Path:
    return Path(cfg.data_dir) / "control"


# ================================================================================================= parser
def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("configuration and logging")
    g.add_argument("--config", action="append", metavar="TOML", help="config file(s), after config/default.toml, in order")
    g.add_argument("--set", action="append", metavar="PATH=VALUE", help="config override, e.g. rpc.rate_per_s=2.5")
    g.add_argument("--log-dir", default="logs", help="JSON-lines log directory (default: logs; '' = console only)")
    g.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    g.add_argument("--quiet", action="store_true", help="no console log lines (the JSON log is still written)")


def _int_list(text: str) -> list[int]:
    try:
        out = [int(x) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {text!r}") from e
    if not out or any(x < 0 for x in out):
        raise argparse.ArgumentTypeError(f"expected non-negative integers, got {text!r}")
    return out


def _positive_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from e
    if v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return v


def _nonneg_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from e
    if v < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}")
    return v


def _positive_float(text: str) -> float:
    try:
        v = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from e
    if not v > 0 or v != v or v == float("inf"):
        raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}")
    return v


def _netuid(text: str) -> int:
    v = _nonneg_int(text)
    if v >= 65_536:
        raise argparse.ArgumentTypeError(f"netuid out of range: {v}")
    return v


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    ap = argparse.ArgumentParser(prog="taotrader", description="taotrader: research, backtest and paper trading of "
                                 "Bittensor dTAO subnet alpha (paper by default; live is gated and user-run). "
                                 "Not financial advice; backtests do not predict returns.")
    ap.add_argument("--version", action="version", version=f"taotrader {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def cmd(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_)
        _common(p)
        return p

    # ---------------------------------------------------------------- data
    p = cmd("collect", "collect archive snapshots into the lake (resumable; --catch-up resumes to the finalized head)")
    p.add_argument("--lake", default="data/lake")
    p.add_argument("--schedule", action="append", choices=["c60", "h300"], help="default: c60 (era C, 60-block)")
    p.add_argument("--catch-up", action="store_true", help="resume the schedule(s) up to finalized head - head margin "
                   "(the default behaviour; the collector skips committed chunks)")
    p.add_argument("--start", type=_nonneg_int)
    p.add_argument("--end", type=_nonneg_int, help="last block (default: finalized head - --head-margin)")
    p.add_argument("--head-margin", type=_nonneg_int, default=100)
    p.add_argument("--max-chunks", type=_positive_int)
    p.add_argument("--keys-per-call", type=_positive_int)
    p.add_argument("--panel-from", type=_nonneg_int)
    p.add_argument("--escrow-grid", type=_positive_int, help="escrow StakeInfo grid in blocks (collector default if unset)")
    p.add_argument("--retry-invalid", action="store_true")
    p.add_argument("--status", action="store_true", help="print the fetch-ledger status and exit")
    p.add_argument("--enrich", action="store_true", help="Taostats trades into ext_trades (needs the Taostats key)")
    p.add_argument("--netuids", type=_int_list, help="--enrich: comma-separated netuids")
    p.add_argument("--from-block", type=_nonneg_int, help="--enrich: first block")
    p.add_argument("--to-block", type=_nonneg_int, help="--enrich: last block")

    p = cmd("refine", "per-block refinement passes (python -m taotrader.data.refine): lifecycle, windows, ...")
    p.add_argument("refine_args", nargs=argparse.REMAINDER, help="arguments for taotrader.data.refine")

    p = cmd("verify", "spec validation suite V1-V7 (--spec) and the Taostats price cross-check (--taostats)")
    p.add_argument("--spec", action="store_true")
    p.add_argument("--taostats", action="store_true")
    p.add_argument("--block", type=_positive_int, help="--spec: block to check (default: finalized head)")
    p.add_argument("--accept-freeze", action="store_true", help="--spec: record the current freeze-list values as the V7 "
                   "baseline after you reviewed a change")
    p.add_argument("--lake", default="data/lake", help="--taostats: lake holding the chain prices")
    p.add_argument("--netuids", type=_int_list, help="--taostats: netuids to cross-check")
    p.add_argument("--from-block", type=_nonneg_int)
    p.add_argument("--to-block", type=_nonneg_int)
    p.add_argument("--out", help="write the result as JSON here")

    p = cmd("verify-metadata", "check the live runtime metadata against the committed storage registry")
    p.add_argument("--block", type=_nonneg_int)
    p.add_argument("--hash", dest="block_hash")
    p.add_argument("--endpoint", help="archive endpoint (default: the first [rpc].archive_endpoints entry)")

    p = cmd("verify-journal", "verify a run journal's hash chain (and the heartbeat head)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--run", help="run id under <data_dir>/runs (default: the config's run_id)")
    g.add_argument("--journal", help="path of a journal.sqlite")
    g.add_argument("--all", action="store_true", help="every run under <data_dir>/runs")
    p.add_argument("--deep", action="store_true", help="also decode every payload")
    p.add_argument("--no-heartbeat", action="store_true", help="do not check against status.json's journal head")

    p = cmd("verify-lake", "verify lake chunk files against the manifest")
    p.add_argument("--lake", default="data/lake")
    p.add_argument("--deep", action="store_true", help="also decode every snapshot and check its digest")

    # ---------------------------------------------------------------- research
    def research(p: argparse.ArgumentParser) -> None:
        p.add_argument("--lake", help="lake root (default: [backtest].lake)")
        p.add_argument("--books", help="comma-separated subset of books")
        p.add_argument("--start", type=_nonneg_int)
        p.add_argument("--end", type=_nonneg_int)
        p.add_argument("--warmup-blocks", type=_nonneg_int)

    p = cmd("backtest", "one replay pass over the lake for every configured book (config/books.backtest.toml)")
    research(p)
    p.add_argument("--out", default="reports/output/backtest", help="summary.json (+ report with --report)")
    p.add_argument("--journal", help="persist the pass journal to this SQLite file (resumes if it exists)")
    p.add_argument("--trials", help="trial registry (default: <out>/trials.sqlite)")
    p.add_argument("--report", action="store_true", help="also write the HTML report")

    p = cmd("grid", "capacity and sensitivity grids (process pool, <= 8 workers)")
    research(p)
    p.add_argument("--capacity", action="append", metavar="BOOK", help="capacity sweep of this base book")
    p.add_argument("--capitals", type=_int_list, help="capacity capitals in TAO (default 1,3,10,30,100,300)")
    p.add_argument("--sensitivity", action="append", metavar="BOOK", help="sensitivity grid of this base book")
    p.add_argument("--workers", type=_positive_int, default=8)
    p.add_argument("--out", default="reports/output/grid")

    p = cmd("study", "pre-registered offline studies (S0 benchmark, or all)")
    p.add_argument("study", choices=["s0", "all"])
    research(p)
    p.add_argument("--out", default="reports/s0")
    p.add_argument("--trials")

    p = cmd("report", "HTML research report (backtest pass) or a paper/live run summary (--run)")
    research(p)
    p.add_argument("--run", help="summarise this paper/live run's journal instead of running a backtest")
    p.add_argument("--with-studies", choices=["none", "s0", "all"], default="s0")
    p.add_argument("--out", help="output directory (default: reports/output/<run or backtest>)")

    # ---------------------------------------------------------------- running
    p = cmd("paper", "paper trading on the live finalized feed (config/books.paper.toml)")
    p.add_argument("--max-minutes", type=_positive_float, help="stop cleanly after this many minutes")
    p.add_argument("--max-ticks", type=_positive_int, help="stop cleanly after this many ticks")
    p.add_argument("--accept-drift", action="store_true", help="resume a journal written under other code/config")
    p.add_argument("--no-toast", action="store_true", help="no Windows toast alerts")

    p = cmd("replay", "re-verify a run journal offline (default: on a copy) or recover it in place (--recover)")
    p.add_argument("--run", help="run id (default: the config's run_id)")
    p.add_argument("--recover", action="store_true", help="run crash recovery on the real journal and exit")
    p.add_argument("--accept-drift", action="store_true", help="accept changed code/config (journals ConfigApplied)")
    p.add_argument("--use-checkpoints", action="store_true", help="start from the newest usable checkpoint")

    # ---------------------------------------------------------------- operator
    for name, help_ in (("halt", "halt every book (no new submissions within one block)"),
                        ("resume", "resume after a halt / exits-only (remove data/control/KILL first)"),
                        ("exits-only", "allow only forced risk exits")):
        p = cmd(name, help_)
        p.add_argument("--reason", default="", help="journaled with the command")
        if name == "halt":
            p.add_argument("--kill", action="store_true", help="also create the kill file data/control/KILL")
        if name == "resume":
            p.add_argument("--clear-kill", action="store_true", help="also remove the kill file")
    p = cmd("flatten", "sell this netuid's positions in every book")
    p.add_argument("netuid", type=_netuid)
    p.add_argument("--reason", default="")
    p = cmd("clear-quarantine", "journal QuarantineCleared for one book of a STOPPED paper/live run, after you "
            "reconciled the cause (orphans, ReconAdjusted, key alarm); entries resume on the next start")
    p.add_argument("--book", required=True)
    p.add_argument("--reason", required=True, help="why it is safe to clear (journaled)")
    p.add_argument("--run", help="run id (default: the config's run_id)")

    # ---------------------------------------------------------------- live
    p = cmd("live", "live adapter (Linux/WSL, user-run): `live arm` or `live --live [--submit]`")
    p.add_argument("action", nargs="?", choices=["run", "arm"], default="run")
    p.add_argument("--live", action="store_true", help="lock 3: required to start (plan-only without --submit)")
    p.add_argument("--submit", action="store_true", help="lock 3: submit under all four locks")
    p.add_argument("--ttl-hours", type=_positive_float, default=float(MAX_ARM_TTL_HOURS), help="arm: token lifetime (<= 24)")
    p.add_argument("--init-secret", action="store_true", help="arm: create the arm HMAC secret in the OS keyring")
    p.add_argument("--max-minutes", type=_positive_float)
    p.add_argument("--accept-drift", action="store_true")

    p = cmd("doctor", "environment and run health checks (--live: idle-proxy / arm-age check on the live host)")
    p.add_argument("--live", action="store_true")
    p.add_argument("--network", action="store_true", help="also probe the configured endpoints (read-only)")
    p.add_argument("--max-idle-hours", type=_positive_int, default=IDLE_PROXY_HOURS)
    p.add_argument("--stale-heartbeat-s", type=_positive_int, default=300)
    return ap


def _split_refine_args(a: argparse.Namespace) -> None:
    """`refine <sub-command> ...` passes the rest through to taotrader.data.refine (argparse REMAINDER), so the common
    options may sit inside it: take --config/--set/--log-dir/--log-level/--quiet out and merge them into `a`."""
    p = argparse.ArgumentParser(prog="taotrader refine", add_help=False)
    p.add_argument("--config", action="append")
    p.add_argument("--set", action="append")
    p.add_argument("--log-dir")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--quiet", action="store_const", const=True)
    known, rest = p.parse_known_args([x for x in a.refine_args if x != "--"])
    a.config = (a.config or []) + (known.config or [])
    a.set = (a.set or []) + (known.set or [])
    for name in ("log_dir", "log_level", "quiet"):
        if getattr(known, name) is not None:
            setattr(a, name, getattr(known, name))
    a.refine_args = rest


# ================================================================================================= main
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        a = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        if a.command == "refine":
            _split_refine_args(a)
    except SystemExit as e:                                    # argparse: --help/--version (0) or bad input (2)
        return int(e.code) if isinstance(e.code, int) else EXIT_USAGE
    from .ops.logging import setup_logging, teardown_logging

    try:
        setup_logging(log_dir=a.log_dir or None, name=f"taotrader-{a.command}", level=a.log_level, console=not a.quiet)
    except (OSError, ValueError) as e:
        _err(f"taotrader: cannot set up logging: {e}")
        return EXIT_USAGE
    handler = _HANDLERS[a.command]
    try:
        return handler(a)
    except CliError as e:
        _err(f"taotrader {a.command}: {e}")
        log.error("%s refused: %s", a.command, e)
        return e.code
    except ConfigError as e:
        _err(f"taotrader {a.command}: configuration error: {e}")
        log.error("%s configuration error: %s", a.command, e)
        return EXIT_USAGE
    except SecretError as e:
        _err(f"taotrader {a.command}: {e}")
        log.error("%s secret error: %s", a.command, e)
        return EXIT_USAGE
    except KeyboardInterrupt:
        _err(f"taotrader {a.command}: interrupted")
        return EXIT_INTERRUPTED
    except SystemExit as e:                                    # a delegated module CLI (refine, collector, studies)
        code = e.code
        if code is None or isinstance(code, int):
            return int(code or 0)
        _err(f"taotrader {a.command}: {code}")
        return EXIT_USAGE
    except Exception as e:
        name = type(e).__name__
        if name == "GateError":
            _err(f"taotrader {a.command}: live gate refused: {e}")
            log.error("live gate refused: %s", e)
            return EXIT_GATE
        if name == "LockHeld":
            _err(f"taotrader {a.command}: {e}")
            log.error("%s", e)
            return EXIT_LOCKED
        if name == "ReplayDivergence":
            _err(f"taotrader {a.command}: replay divergence: {e}")
            log.error("replay divergence: %s", e)
            return EXIT_DIVERGENCE
        log.exception("%s failed", a.command)
        _err(f"taotrader {a.command}: failed: {name}: {e}")
        return EXIT_FAIL
    finally:
        teardown_logging()


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, sort_keys=True, default=str))


def _err(message: str) -> None:
    """A refusal or failure line on stderr, redacted like the log lines (an exception text may carry a keyed URL)."""
    from .ops.logging import redact_line

    print(redact_line(message), file=sys.stderr)


# ================================================================================================= shared runtime
def _pool(cfg: RunCfg) -> RpcPool:
    from .chain.rpc import RpcPool

    keyed = get_secret("onfinality")
    return RpcPool.from_cfg(cfg.rpc, keyed_archive_url=None if keyed is None else keyed.reveal())


def _reader(cfg: RunCfg, *, keys_per_call: int | None = None, **kw: Any) -> JsonRpcChainReader:
    from .chain.reader import JsonRpcChainReader

    return JsonRpcChainReader(_pool(cfg), keys_per_call=keys_per_call or cfg.rpc.keys_per_call,
                              max_concurrency=cfg.rpc.max_concurrency, **kw)


def _alerts(*, toast: bool) -> AlertManager:
    from .ops.alerts import build_alert_manager

    return build_alert_manager(toast=toast)


# ================================================================================================= data commands
def _cmd_collect(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    if a.status:
        from .data import collector

        return collector.main(["status", "--lake", a.lake])
    if a.enrich:
        if not a.netuids or a.from_block is None or a.to_block is None or a.from_block > a.to_block:
            raise CliError("--enrich needs --netuids and a --from-block <= --to-block range (Taostats credits are metered)")
        return asyncio.run(_enrich(a))
    return asyncio.run(_collect(a, cfg))


async def _collect(a: argparse.Namespace, cfg: RunCfg) -> int:
    from .data import collector as col
    from .data.lake import Lake

    reader = _reader(cfg, keys_per_call=a.keys_per_call, provider_check_every=200)
    lake = Lake(a.lake)
    try:
        end = a.end
        if end is None:
            head, _ = await reader.finalized_head()
            end = int(head) - a.head_margin
        scheds = []
        for name in a.schedule or ["c60"]:
            if name == "c60":
                scheds.append(col.schedule_c60(end, start=a.start or col.ERA_C_FIRST_BLOCK))
            else:
                scheds.append(col.schedule_h300(end, start=a.start or col.DTAO_LAUNCH_BLOCK))
        kw: dict[str, Any] = {}
        if a.panel_from is not None:
            kw["panel_from"] = a.panel_from
        if a.escrow_grid is not None:
            kw["escrow_grid"] = a.escrow_grid
        ccfg = col.CollectorCfg(**kw)

        def progress(p: Any, msg: str) -> None:
            log.info("%s [%d..%d]: %s", p.label, p.blocks[0], p.blocks[-1], msg)

        c = col.Collector(col.ReaderSource(reader), lake, ccfg, on_chunk=progress)
        try:
            s = await c.run(scheds, retry_invalid=a.retry_invalid, max_chunks=a.max_chunks)
        finally:
            c.close()
        _print({"end": end, "snapshots": s.snapshots, "invalid": s.invalid, "chunks": s.chunks_committed,
                "skipped": s.chunks_skipped, "failed": s.chunks_failed, "calls": s.calls})
        return EXIT_FAIL if s.chunks_failed else EXIT_OK
    finally:
        lake.close()
        await reader.pool.aclose()


async def _enrich(a: argparse.Namespace) -> int:
    from .data.lake import Lake
    from .data.taostats import TaostatsClient, TaostatsUnavailable, enrich_trades

    try:
        client = TaostatsClient.from_secrets()
    except TaostatsUnavailable as e:
        raise CliError(str(e)) from e
    lake = Lake(a.lake)
    try:
        async with client:
            n = await enrich_trades(client, lake, list(a.netuids), a.from_block, a.to_block)
        _print({"ext_trades_rows": n, "credits_used": client.limiter.used})
        return EXIT_OK
    finally:
        lake.close()


def _cmd_refine(a: argparse.Namespace) -> int:
    _cfg(a)
    from .data import refine

    rest = list(a.refine_args)
    if not rest:
        raise CliError("refine needs a sub-command: lifecycle, windows, spec-boundaries, verify-prune-log, range-probe")
    return refine.main(rest)


def _cmd_verify_metadata(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    from .chain.metadata import verify_metadata

    endpoint = a.endpoint or (cfg.rpc.archive_endpoints[0] if cfg.rpc.archive_endpoints else None)
    if not endpoint:
        raise CliError("no archive endpoint configured ([rpc].archive_endpoints)")
    try:
        layout, problems = asyncio.run(verify_metadata(block=a.block, block_hash=a.block_hash, endpoint=endpoint))
    except ImportError as e:
        raise CliError(f"verify-metadata needs the collector extra (scalecodec): {e}") from e
    print(f"spec {layout.spec_version}: {len(layout.entries)} storage entries checked")
    for p in problems:
        print("MISMATCH", p)
    return EXIT_FAIL if problems else EXIT_OK


def _journal_paths(a: argparse.Namespace, cfg: RunCfg) -> list[tuple[str, Path]]:
    if a.journal:
        return [("", Path(a.journal))]
    runs = Path(cfg.data_dir) / "runs"
    if a.all:
        return sorted((d.name, d / "journal.sqlite") for d in runs.glob("*") if (d / "journal.sqlite").is_file())
    rid = a.run or cfg.run_id
    return [(rid, runs / rid / "journal.sqlite")]


def _cmd_verify_journal(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    from .data.journal import JournalError, SqliteJournal
    from .ops.health import STATUS_FILE, expected_head_from, read_status

    targets = _journal_paths(a, cfg)
    if not targets:
        raise CliError(f"no journals under {Path(cfg.data_dir) / 'runs'}")
    bad = 0
    for rid, path in targets:
        if not path.is_file():
            print(f"{path}: MISSING")
            bad += 1
            continue
        exp = None if a.no_heartbeat else expected_head_from(read_status(path.parent / STATUS_FILE),
                                                             run_id=rid or None)
        try:
            j = SqliteJournal(path, readonly=True)
        except JournalError as e:
            print(f"{path}: CANNOT OPEN: {e}")
            bad += 1
            continue
        try:
            n = j.verify_chain(expected_head=exp, deep=a.deep)
            seq, h = j.head()
            print(f"{path}: OK {n} records, head seq {seq} {h.hex()[:16]}..."
                  + ("" if exp is None else f" (heartbeat head seq {exp[0]} present)"))
        except JournalError as e:
            print(f"{path}: INTEGRITY FAILURE: {e}")
            bad += 1
        finally:
            j.close()
    return EXIT_FAIL if bad else EXIT_OK


def _cmd_verify_lake(a: argparse.Namespace) -> int:
    _cfg(a)
    from .data.lake import Lake

    if not Path(a.lake).is_dir():
        raise CliError(f"no lake at {a.lake}")
    lake = Lake(a.lake)
    try:
        problems = lake.verify(deep=a.deep)
        n = len(lake.manifest())
    finally:
        lake.close()
    for p in problems:
        print("PROBLEM", p)
    print(f"{a.lake}: {n} chunks, {len(problems)} problem(s)")
    return EXIT_FAIL if problems else EXIT_OK


# ------------------------------------------------------------------------------------------------- verify --spec
@dataclass(frozen=True, slots=True)
class SpecCheck:
    check: str
    status: str            # PASS | FAIL | SKIPPED | BASELINE
    detail: str


def _cmd_verify(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    if not (a.spec or a.taostats):
        raise CliError("choose --spec and/or --taostats")
    out: dict[str, Any] = {}
    failed = False
    if a.spec:
        checks = asyncio.run(_verify_spec(cfg, a.block, accept_freeze=a.accept_freeze))
        out["spec"] = [{"check": c.check, "status": c.status, "detail": c.detail} for c in checks]
        for c in checks:
            print(f"{c.check}: {c.status} {c.detail}")
        failed |= any(c.status == "FAIL" for c in checks)
    if a.taostats:
        if not a.netuids or a.from_block is None or a.to_block is None or a.from_block > a.to_block:
            raise CliError("--taostats needs --netuids and a --from-block <= --to-block range")
        res = asyncio.run(_verify_taostats(a))
        out["taostats"] = res
        print(f"taostats: {res['rows']} rows, {res['outside_tolerance']} outside {TAOSTATS_TOL:.0%}, max rel diff "
              f"{res['max_rel_diff']}")
        failed |= res["outside_tolerance"] > 0
    if a.out:
        from .ops.health import write_json_atomic

        write_json_atomic(a.out, out)
    return EXIT_FAIL if failed else EXIT_OK


def _freeze_values(snap: ChainSnapshot) -> dict[str, str]:
    """Section 4.3 PARAM_CHANGED freeze list as text (globals, then per-subnet values keyed by netuid)."""
    g = snap.glob
    out: dict[str, str] = {}
    for name in ("subnet_moving_alpha", "gate_rank", "gate_exponent", "tao_weight", "subnet_limit", "immunity_period",
                 "network_rate_limit", "lock_reduction_interval", "min_lock_cost", "owner_cut", "shorts_enabled"):
        if hasattr(g, name):
            out[f"glob.{name}"] = str(getattr(g, name))
    for s in snap.subnets:
        for name in ("fee_rate", "ema_halving_blocks", "tempo", "consensus_mode"):
            if hasattr(s, name):
                out[f"sn{int(s.key.netuid)}.{name}"] = str(getattr(s, name))
    return out


async def _verify_spec(cfg: RunCfg, block: int | None, *, accept_freeze: bool) -> list[SpecCheck]:
    """V1-V4 and V7 against the configured endpoints (read-only). V5 and V6 need the SDK: the live host runs them
    (preflight and RiskExitChecks), so they are reported SKIPPED here."""
    from .core.state import ReadPlan
    from .core.units import AlphaRao, Block, BlockHash, NetUid, Rao
    from .ops.health import write_json_atomic
    from .protocol.amm import quote_buy, quote_sell
    from .protocol.emission import emission_vector, observed_block_emission
    from .protocol.prune import prune_target
    from .venues.paper import drift_ppm

    reader = _reader(cfg)
    checks: list[SpecCheck] = []
    try:
        if block is None:
            head, _ = await reader.finalized_head()
            b = int(head)
        else:
            b = block
        hashes = await reader.block_hashes([b - 1, b])
        prev = await reader.snapshot(Block(b - 1), hashes[b - 1], ReadPlan.FULL, None, ())
        cur = await reader.snapshot(Block(b), hashes[b], ReadPlan.FULL, prev, ())
        bh = BlockHash(hashes[b])
        spec = f"spec {cur.glob.spec_version} tx {cur.glob.tx_version} at {b}"
        # V1 decode parity
        pp = await reader.price_parity(cur)
        checks.append(SpecCheck("V1 price decode parity", "PASS" if pp.ok else "FAIL", f"{spec}: max {pp.max_rel_ppm} ppm"))
        # V2 AMM vs sim_swap: 3 deepest started subnets x 2 sizes x buy/sell
        subs = sorted((s for s in cur.subnets if int(s.key.netuid) != 0 and s.first_emission_block is not None
                       and s.pool.px_tao > 0 and s.pool.px_alpha > 0), key=lambda s: (-int(s.pool.tao), int(s.key.netuid)))
        worst = 0
        fails: list[str] = []
        for s in subs[:SPEC_V2_SUBNETS]:
            for tao in (RAO_PER_TAO, 10 * RAO_PER_TAO):
                qb = quote_buy(s.pool, Rao(tao))
                sim = await reader.sim_swap_buy(NetUid(int(s.key.netuid)), tao, bh)
                e1 = drift_ppm(int(qb.amount_out), sim.alpha_amount)
                fee_off = abs(int(qb.fee) - sim.tao_fee) > max(1, sim.tao_fee // 1_000_000)
                alpha = int(qb.amount_out)
                qs = quote_sell(s.pool, AlphaRao(alpha))
                sims = await reader.sim_swap_sell(NetUid(int(s.key.netuid)), alpha, bh)
                e2 = drift_ppm(int(qs.amount_out), sims.tao_amount)
                worst = max(worst, e1, e2)
                if e1 > 1 or e2 > 1 or fee_off:
                    fails.append(f"SN{int(s.key.netuid)} {tao // RAO_PER_TAO} TAO: buy {e1} ppm, sell {e2} ppm"
                                 + (", fee mismatch" if fee_off else ""))
        checks.append(SpecCheck("V2 AMM vs sim_swap", "FAIL" if fails or not subs else "PASS",
                                "; ".join(fails) or f"{min(len(subs), SPEC_V2_SUBNETS)} subnets, worst {worst} ppm"))
        # V3 prune target parity
        local = prune_target(cur)
        chain = await reader.subnet_to_prune(bh)
        ok3 = (None if local is None else int(local.netuid)) == (None if chain is None else int(chain))
        checks.append(SpecCheck("V3 prune-target parity", "PASS" if ok3 else "FAIL",
                                f"local {None if local is None else int(local.netuid)}, chain {chain}"))
        # V4 emission replica (EMA of n-1 drives emission at n)
        model = emission_vector(prev)
        worst4, n4, off = 0, 0, []
        for s in cur.subnets:
            p = prev.get(s.key)
            sh = model.get(s.key)
            if p is None or sh is None or not s.emission_enabled:
                continue
            obs = int(observed_block_emission(s, p))
            d = abs(int(sh.tao_per_block) - obs)
            n4 += 1
            worst4 = max(worst4, d)
            if d > SPEC_V4_TOL_RAO:
                off.append(f"SN{int(s.key.netuid)} {d} rao")
        checks.append(SpecCheck("V4 emission replica", "FAIL" if off else "PASS",
                                f"{n4} enabled subnets, worst {worst4} rao/block" + (f": {', '.join(off[:8])}" if off else "")))
        checks.append(SpecCheck("V5 fee parity (plan/query_info)", "SKIPPED", "needs the SDK: runs on the live host "
                                "(taotrader live preflight)"))
        checks.append(SpecCheck("V6 call indices / Staking allow-list", "SKIPPED", "needs the SDK metadata: runs on the "
                                "live host (preflight and the UNARMED risk-exit checks)"))
        # V7 freeze list against the recorded baseline
        base_path = Path(cfg.data_dir) / "verify" / "freeze_baseline.json"
        vals = _freeze_values(cur)
        try:
            base = json.loads(base_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            base = None
        if base is None or accept_freeze:
            write_json_atomic(base_path, {"block": b, "spec": cur.glob.spec_version, "values": vals})
            checks.append(SpecCheck("V7 freeze-list diff", "BASELINE", f"{len(vals)} values recorded at {b}"))
        else:
            old = base.get("values", {}) if isinstance(base, dict) else {}
            changed = sorted(k for k in set(old) | set(vals) if old.get(k) != vals.get(k)
                             and not (k.startswith("sn") and (k not in old or k not in vals)))
            checks.append(SpecCheck("V7 freeze-list diff", "FAIL" if changed else "PASS",
                                    (f"{len(changed)} changed: " + ", ".join(f"{k} {old.get(k)} -> {vals.get(k)}"
                                                                          for k in changed[:12]))
                                    if changed else f"{len(vals)} values unchanged since block {base.get('block')}"))
    finally:
        await reader.pool.aclose()
    return checks


async def _verify_taostats(a: argparse.Namespace) -> dict[str, Any]:
    from .core.units import Block
    from .data.lake import Lake
    from .data.store import LakeSnapshotStore
    from .data.taostats import TaostatsClient, TaostatsUnavailable, crosscheck_rows, load_generations, write_crosscheck

    try:
        client = TaostatsClient.from_secrets()
    except TaostatsUnavailable as e:
        raise CliError(str(e)) from e
    lake = Lake(a.lake)
    try:
        store = LakeSnapshotStore(lake, None)
        gens = load_generations(lake)

        def chain_price(block: int, netuid: int) -> int | None:
            if block % 300 != 0:
                return None
            try:
                (snap,) = store.fetch([Block(block)])
            except (KeyError, ValueError, LookupError):
                return None
            s = snap.by_netuid(netuid)
            return None if s is None else int(s.pool.spot_rao())

        rows: list[dict[str, Any]] = []
        async with client:
            for n in a.netuids:
                hist = await client.pool_history(n, a.from_block, a.to_block)
                rows += crosscheck_rows(hist, n, gens, chain_price)
        write_crosscheck(lake, rows)
    finally:
        lake.close()
    diffs = [float(r["rel_diff"]) for r in rows]
    return {"rows": len(rows), "outside_tolerance": sum(1 for d in diffs if d > TAOSTATS_TOL),
            "max_rel_diff": max(diffs, default=0.0)}


# ================================================================================================= research commands
def _plan(a: argparse.Namespace) -> Any:
    from .backtest.books import load_backtest_plan

    return load_backtest_plan(_paths(a, (BOOKS_BACKTEST,)), cli=a.set or ())


def _books_arg(a: argparse.Namespace) -> list[str] | None:
    return [b.strip() for b in a.books.split(",") if b.strip()] if a.books else None


def _metrics_json(m: Any) -> dict[str, Any]:
    import dataclasses

    if dataclasses.is_dataclass(m) and not isinstance(m, type):
        return {f.name: _plain(getattr(m, f.name)) for f in dataclasses.fields(m)}
    return {"value": str(m)}


def _plain(v: Any) -> Any:
    """JSON-ready copy: Decimal as exact text, dataclasses as objects, enums by name, non-finite floats as text."""
    import dataclasses
    import enum
    import math

    if isinstance(v, bool) or v is None or isinstance(v, int | str):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, enum.Enum):
        return v.name
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, tuple | list | set | frozenset):
        return [_plain(x) for x in (sorted(v, key=str) if isinstance(v, set | frozenset) else v)]
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def _cmd_backtest(a: argparse.Namespace) -> int:
    from .backtest.runner import TrialRegistry, register_pass, run_pass
    from .data.journal import SqliteJournal
    from .data.lake import Lake

    plan = _plan(a)
    lake_root = a.lake or plan.lake
    if not Path(lake_root).is_dir():
        raise CliError(f"no lake at {lake_root} (run `taotrader collect` first)")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lake = Lake(lake_root)
    journal = SqliteJournal(a.journal, durable=False) if a.journal else None
    try:
        res = run_pass(plan, lake, books=_books_arg(a), start=a.start, end=a.end, warmup_blocks=a.warmup_blocks,
                       journal=journal)
    finally:
        if journal is not None:
            journal.close()
        lake.close()
    reg = TrialRegistry(a.trials or out / "trials.sqlite")
    try:
        n_trials = register_pass(reg, res, "backtest")
    finally:
        reg.close()
    summary = {"run_id": res.run_id, "ticks": res.ticks, "first_block": res.first_block, "last_block": res.last_block,
               "journal_head": list(res.journal_head), "trial_count": n_trials, "identity": _plain(vars_of(res.identity)),
               "books": {k: {"digest": v.digest, "money_digest": v.money_digest, "orphans": v.orphans,
                             "breaches": list(v.breaches), "metrics": _metrics_json(v.metrics)}
                         for k, v in sorted(res.books.items())}}
    from .ops.health import write_json_atomic

    write_json_atomic(out / "summary.json", summary)
    for k, v in sorted(res.books.items()):
        print(f"{k}: orphans {v.orphans} breaches {len(v.breaches)} digest {v.digest[:16]}")
    print(f"summary: {out / 'summary.json'} (trials registered: {n_trials})")
    if a.report:
        from .reports.html import write_report

        path = write_report(out, res, [], plan=plan, trial_count=n_trials)
        print(f"report: {path}")
    bad = any(v.orphans or v.breaches for v in res.books.values())
    return EXIT_FAIL if bad else EXIT_OK


def vars_of(obj: Any) -> dict[str, Any]:
    import dataclasses

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    return dict(vars(obj))


def _cmd_grid(a: argparse.Namespace) -> int:
    from .backtest.runner import CAPACITY_TAO, GridJob, TrialRegistry, run_grid

    plan = _plan(a)
    lake_root = a.lake or plan.lake
    if not Path(lake_root).is_dir():
        raise CliError(f"no lake at {lake_root}")
    if not a.capacity and not a.sensitivity:
        raise CliError("choose --capacity BOOK and/or --sensitivity BOOK")
    known = {str(b) for b in plan.base_books}
    for b in (a.capacity or []) + (a.sensitivity or []):
        if b not in known:
            raise CliError(f"unknown base book {b!r} (known: {sorted(known)})")
    paths = tuple(str(p) for p in _paths(a, (BOOKS_BACKTEST,)))
    jobs: list[GridJob] = []
    for b in a.capacity or []:
        for tao in a.capitals or list(CAPACITY_TAO):
            jobs.append(GridJob(label=f"capacity-{b}-{tao}", lake=lake_root, paths=paths, cli=tuple(a.set or ()), books=(),
                                start=a.start, end=a.end, warmup_blocks=a.warmup_blocks, purpose="grid:capacity",
                                variants=(("capacity", b, int(tao)),)))
    for b in a.sensitivity or []:
        jobs.append(GridJob(label=f"sensitivity-{b}", lake=lake_root, paths=paths, cli=tuple(a.set or ()), books=(),
                            start=a.start, end=a.end, warmup_blocks=a.warmup_blocks, purpose="grid:sensitivity",
                            variants=(("sensitivity", b, 0),)))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    reg = TrialRegistry(out / "trials.sqlite")
    try:
        results = run_grid(jobs, max_workers=a.workers, registry=reg)
        n = reg.count()
    finally:
        reg.close()
    doc = {"trial_count": n, "jobs": [{"label": r.label, "ticks": r.ticks, "data_range": r.data_range,
                                       "metrics": {k: _metrics_json(m) for k, m in sorted(r.metrics.items())}}
                                      for r in results]}
    from .ops.health import write_json_atomic

    write_json_atomic(out / "grid.json", doc)
    print(f"grid: {len(results)} job(s), trial count {n}: {out / 'grid.json'}")
    return EXIT_OK


def _cmd_study(a: argparse.Namespace) -> int:
    _plan(a)                                   # fail closed on a bad config before the long pass starts
    from .backtest import studies

    argv = [a.study, "--out", a.out]
    for p in _paths(a, (BOOKS_BACKTEST,)):
        argv += ["--config", str(p)]
    for s in a.set or []:
        argv += ["--set", s]
    for flag, val in (("--lake", a.lake), ("--books", a.books), ("--start", a.start), ("--end", a.end),
                      ("--warmup-blocks", a.warmup_blocks), ("--trials", a.trials)):
        if val is not None:
            argv += [flag, str(val)]
    return studies.main(argv)


def _cmd_report(a: argparse.Namespace) -> int:
    if a.run:
        return _run_report(a)
    from .backtest.runner import TrialRegistry, register_pass, run_pass
    from .backtest.studies import PanelRecorder, lake_panel_from, run_all, s0
    from .data.lake import Lake
    from .reports.html import write_report

    plan = _plan(a)
    lake_root = a.lake or plan.lake
    if not Path(lake_root).is_dir():
        raise CliError(f"no lake at {lake_root}")
    out = Path(a.out or "reports/output/backtest")
    lake = Lake(lake_root)
    rec = PanelRecorder()
    try:
        res = run_pass(plan, lake, books=_books_arg(a), start=a.start, end=a.end, warmup_blocks=a.warmup_blocks, on_tick=rec)
        pf = lake_panel_from(lake)
    finally:
        lake.close()
    out.mkdir(parents=True, exist_ok=True)
    reg = TrialRegistry(out / "trials.sqlite")
    try:
        n = register_pass(reg, res, "report")
    finally:
        reg.close()
    window = (a.start or plan.start_block, int(res.last_block or 0))
    studies: list[Any] = []
    if a.with_studies == "s0":
        studies = [s0(res, rec.rows, window=window, panel_from=pf)]
    elif a.with_studies == "all":
        studies = list(run_all(res, rec, window=window, panel_from=pf))
    path = write_report(out, res, studies, plan=plan, trial_count=n, universe=rec.universe_counts)
    print(f"report: {path}")
    return EXIT_OK


def journal_summary(journal_path: Path) -> dict[str, Any]:
    """Counts and facts of a paper/live journal (weekly report: data quality, model drift, prune watch, components)."""
    from .core.events import (
        DecisionTrace,
        FillReported,
        ModeChanged,
        ModelDriftObserved,
        OrderFailed,
        SnapshotObserved,
    )
    from .data.journal import SqliteJournal, decode_record

    j = SqliteJournal(journal_path, readonly=True)
    kinds: dict[str, int] = {}
    fails: dict[str, int] = {}
    drift: list[dict[str, Any]] = []
    modes: list[dict[str, Any]] = []
    fills: dict[str, int] = {}
    nav: dict[str, int] = {}
    gaps = 0
    first = last = None
    try:
        records = j.verify_chain()
        for rec in j.read(1):
            kinds[rec.kind] = kinds.get(rec.kind, 0) + 1
            first = int(rec.time.block) if first is None else first
            last = int(rec.time.block)
            if rec.kind not in ("snapshot_observed", "fill_reported", "order_failed", "model_drift_observed", "mode_changed",
                                "decision_trace"):
                continue
            ev = decode_record(rec)
            if isinstance(ev, SnapshotObserved):
                gaps += int(ev.health.feed_gap_blocks)
            elif isinstance(ev, FillReported):
                fills[str(rec.book)] = fills.get(str(rec.book), 0) + 1
            elif isinstance(ev, OrderFailed):
                fails[ev.reason.name] = fails.get(ev.reason.name, 0) + 1
            elif isinstance(ev, ModelDriftObserved):
                drift.append({"block": int(ev.block), "probe": ev.probe, "netuid": ev.netuid, "err_ppm": ev.err_ppm})
            elif isinstance(ev, ModeChanged):
                modes.append({"block": int(rec.time.block), "book": str(rec.book), "event": str(ev)[:200]})
            elif isinstance(ev, DecisionTrace):
                nav[str(rec.book)] = int(ev.nav_liq)
    finally:
        j.close()
    return {"journal": str(journal_path), "records": records, "first_block": first, "last_block": last,
            "kinds": dict(sorted(kinds.items())), "feed_gap_blocks": gaps, "fills_by_book": dict(sorted(fills.items())),
            "failures_by_reason": dict(sorted(fails.items())), "model_drift": drift[-50:], "mode_changes": modes[-50:],
            "last_nav_liq_rao": dict(sorted(nav.items()))}


def _run_report(a: argparse.Namespace) -> int:
    cfg = _cfg(a, (BOOKS_PAPER,)) if not a.config else _cfg(a)
    run_dir = _run_dir(cfg, a.run)
    jp = run_dir / "journal.sqlite"
    if not jp.is_file():
        raise CliError(f"no journal at {jp}")
    doc = journal_summary(jp)
    from .ops.health import STATUS_FILE, read_status, write_json_atomic

    doc["status"] = read_status(run_dir / STATUS_FILE)
    out = Path(a.out or f"reports/output/{a.run}")
    write_json_atomic(out / "summary.json", doc)
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in doc["kinds"].items())
    html = (f"<!doctype html><meta charset='utf-8'><title>taotrader run {a.run}</title>"
            f"<h1>Run {a.run}</h1><p>Not financial advice. Paper results are optimistic (shield misses are modelled).</p>"
            f"<p>{doc['records']} journal records, blocks {doc['first_block']}..{doc['last_block']}, "
            f"feed gaps {doc['feed_gap_blocks']} blocks</p><h2>Records</h2><table>{rows}</table>"
            f"<h2>Fills</h2><pre>{json.dumps(doc['fills_by_book'], indent=1)}</pre>"
            f"<h2>Failures</h2><pre>{json.dumps(doc['failures_by_reason'], indent=1)}</pre>"
            f"<h2>Model drift</h2><pre>{json.dumps(doc['model_drift'], indent=1)}</pre>"
            f"<h2>Last NAV_liq (rao)</h2><pre>{json.dumps(doc['last_nav_liq_rao'], indent=1)}</pre>")
    (out / "summary.html").write_text(html, encoding="utf-8")
    print(f"run summary: {out / 'summary.json'}")
    return EXIT_OK


# ================================================================================================= operator commands
def _operator(a: argparse.Namespace, command: str) -> int:
    cfg = _cfg(a, (BOOKS_PAPER,)) if not a.config else _cfg(a)
    from .engine.control import KILL_FILE, write_command

    d = _control_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    if command == "halt" and getattr(a, "kill", False):
        (d / KILL_FILE).write_text(f"created by `taotrader halt --kill` at {int(time.time())}: {a.reason}\n",
                                   encoding="utf-8")
        print(f"kill file created: {d / KILL_FILE}")
    if command == "resume" and getattr(a, "clear_kill", False):
        with contextlib.suppress(FileNotFoundError):
            (d / KILL_FILE).unlink()
            print(f"kill file removed: {d / KILL_FILE}")
    if command == "resume" and (d / KILL_FILE).exists():
        print(f"warning: {d / KILL_FILE} exists, so every book stays halted; remove it (resume --clear-kill)",
              file=sys.stderr)
    path = write_command(d, command, a.reason)
    print(f"{command}: written {path} (picked up by the running process within one block)")
    log.warning("operator command %s written to %s (reason: %s)", command, path, a.reason)
    return EXIT_OK


def _cmd_halt(a: argparse.Namespace) -> int:
    return _operator(a, "halt")


def _cmd_resume(a: argparse.Namespace) -> int:
    return _operator(a, "resume")


def _cmd_exits_only(a: argparse.Namespace) -> int:
    return _operator(a, "exits_only")


def _cmd_flatten(a: argparse.Namespace) -> int:
    return _operator(a, f"flatten:{a.netuid}")


RUN_LOCK_MODES: Final[tuple[str, ...]] = ("paper", "live_dry", "live")


def _cmd_clear_quarantine(a: argparse.Namespace) -> int:
    """QuarantineCleared is an operator decision (DESIGN.md 4.3, 9.7): it resets the book's orphan count, quarantine
    list and recon halt. The Runner is the journal's only writer while it runs, so this command takes every instance
    lock of the run first (a running process -> exit 4) and appends one non-tick batch at the journal's last block in
    Phase.OUTBOX, which recovery folds like any reconcile batch. The chain is verified before and after."""
    from .core.events import QuarantineCleared
    from .core.units import Block, BookId, LogicalTime, Phase
    from .data.journal import SqliteJournal
    from .ops.health import STATUS_FILE, expected_head_from, read_status
    from .ops.lock import InstanceLock, lock_path

    cfg = _cfg(a, (BOOKS_PAPER,)) if not a.config else _cfg(a)
    reason = a.reason.strip()
    if not reason:
        raise CliError("--reason must say why clearing is safe (it is journaled)")
    books = sorted(str(b.book) for b in cfg.books)
    if a.book not in books:
        raise CliError(f"unknown book {a.book!r} in this config (books: {books})")
    run_id = a.run or cfg.run_id
    rd = _run_dir(cfg, run_id)
    jp = rd / "journal.sqlite"
    if not jp.is_file():
        raise CliError(f"no journal at {jp}")
    locks = [InstanceLock(lock_path(cfg.data_dir, run_id, m), mode=m, run_id=run_id) for m in RUN_LOCK_MODES]
    held: list[InstanceLock] = []
    try:
        for lk in locks:
            held.append(lk.acquire())                          # LockHeld -> exit 4: stop the running process first
        j = SqliteJournal(jp)
        try:
            j.verify_chain(expected_head=expected_head_from(read_status(rd / STATUS_FILE), run_id=run_id))
            last = j.head_block()
            if last is None:
                raise CliError(f"{jp} is empty")
            ev = QuarantineCleared(book=BookId(a.book), block=Block(last), reason=f"operator: {reason}"[:200])
            j.append_batch([(LogicalTime(Block(last), Phase.OUTBOX, 0), BookId(a.book), ev)])
            n = j.verify_chain()
            seq, h = j.head()
        finally:
            j.close()
    finally:
        for lk in held:
            lk.release()
    print(f"QuarantineCleared journaled for book {a.book} at block {last} (journal {n} records, head seq {seq} "
          f"{h.hex()[:16]}...); it takes effect when the run starts again")
    log.warning("operator QuarantineCleared for book %s of run %s at block %d: %s", a.book, run_id, last, reason)
    return EXIT_OK


# ================================================================================================= paper
class PaperTracker:
    """The live tracked-hotkey panel (protocol.derive.track_hotkeys): every held pair, the top-N earners by
    TotalHotkeyAlpha, every take-0 earner and the owner hotkeys, sticky per generation. The dividend listing is
    refreshed every `every` blocks from the snapshot's own hash (one AlphaDividendsPerSubnet prefix walk)."""

    def __init__(self, list_dividends: Callable[[Any], Awaitable[Mapping[int, Sequence[Any]]]], *, every: int, top_n: int,
                 held: Callable[[], Sequence[tuple[Any, str]]] = lambda: ()) -> None:
        self._list = list_dividends
        self.every = every
        self.top_n = top_n
        self.held = held
        self.listing: dict[int, tuple[str, ...]] = {}
        self.listing_block: int | None = None
        self.tracked_set: tuple[tuple[Any, Any], ...] = ()
        self.errors = 0

    def tracked(self, prev: ChainSnapshot | None) -> Sequence[tuple[Any, Any]]:
        from .protocol.derive import track_hotkeys

        if prev is None or not self.listing:
            return self.tracked_set
        self.tracked_set = track_hotkeys(prev, dict(self.listing), list(self.held()), self.tracked_set, self.top_n)
        return self.tracked_set

    async def maybe_refresh(self, snap: ChainSnapshot) -> None:
        b = int(snap.block)
        if self.listing_block is not None and b - self.listing_block < self.every:
            return
        try:
            listing = await self._list(snap.block_hash)
        except Exception as e:
            self.errors += 1
            log.warning("dividend listing at %d failed (%s); keeping the previous panel", b, type(e).__name__)
            self.listing_block = b - self.every + 60          # retry after ~60 blocks
            return
        self.listing = {int(n): tuple(str(h) for h in hs) for n, hs in listing.items()}
        self.listing_block = b


def _held_pairs(books: Sequence[BookRuntime]) -> list[tuple[Any, str]]:
    out: set[tuple[Any, str]] = set()
    for rt in books:
        for p in rt.state.portfolio.positions:
            out.add((p.key, str(p.hotkey)))
        for o in rt.state.orders:
            if not o.terminal_block:
                out.add((o.record.intent.key, str(o.record.intent.hotkey)))
    return sorted(out, key=lambda kh: (int(kh[0].netuid), int(kh[0].reg_at), kh[1]))


def _book_status(rt: BookRuntime, view: ChainSnapshot | None) -> BookStatus:
    from .core.fixed import DEC, floor_int
    from .core.orders import TERMINAL
    from .core.portfolio import parse_pos_account
    from .ops.health import BookStatus

    st = rt.state
    pos_value = 0
    for account, _unit, bal in st.ledger:
        pk = parse_pos_account(account)
        if pk is None or bal <= 0 or view is None:
            continue
        s = view.get(pk.subnet)
        if s is None or s.pool.px_alpha <= 0 or s.pool.px_tao <= 0:
            continue
        try:
            pos_value += floor_int(DEC.multiply(Decimal(int(bal)), s.pool.spot()))
        except ArithmeticError:
            continue
    cash, ff = int(st.portfolio.cash), int(st.portfolio.fee_float)
    open_orders = sum(1 for o in st.orders if o.state not in TERMINAL)
    return BookStatus(mode=st.mode.name, halted=st.halted, exits_only=st.exits_only, open_orders=open_orders,
                      nav_rao=cash + ff + pos_value, cash_rao=cash, fee_float_rao=ff, positions=len(st.portfolio.positions),
                      orphans=st.orphans, breaches=tuple(st.breaches))


@dataclass
class _Watch:
    """Alert bookkeeping across ticks (mode changes, prune watch, fee float, daily summary)."""
    modes: dict[str, str]
    prune_alerted: set[tuple[str, int, int]]
    last_day: int | None = None
    stall_alerted: bool = False


def _watch_tick(runner: Runner, snap: ChainSnapshot, alerts: AlertManager, w: _Watch, statuses: Mapping[str, BookStatus],
                feed: Any) -> None:
    from .protocol.prune import prune_rank

    for rt in runner.books:
        bk = str(rt.book)
        mode = rt.state.mode.name
        if w.modes.get(bk) not in (None, mode):
            alerts.alert("mode", f"book {bk}: mode {w.modes[bk]} -> {mode} at block {int(snap.block)}", key=f"mode:{bk}:{mode}")
        w.modes[bk] = mode
        if int(rt.state.portfolio.fee_float) < int(rt.engine.cfg.risk.fee_float_alert_rao) and rt.state.funded:
            alerts.alert("fee_float", f"book {bk}: fee float {int(rt.state.portfolio.fee_float)} rao is below the alert "
                         f"level {int(rt.engine.cfg.risk.fee_float_alert_rao)}", key=f"fee_float:{bk}")
        for p in rt.state.portfolio.positions:
            s = snap.get(p.key)
            reason = ""
            if s is None:
                reason = "dissolved"
            elif not s.emission_enabled:
                reason = "emission disabled"
            else:
                r = prune_rank(snap, p.key)
                if r is not None and r <= 3:
                    reason = f"prune rank {r}"
            tag = (bk, int(p.key.netuid), int(p.key.reg_at))
            if reason and tag not in w.prune_alerted:
                w.prune_alerted.add(tag)
                alerts.alert("prune_watch", f"book {bk}: held SN{int(p.key.netuid)} (reg {int(p.key.reg_at)}): {reason}",
                             key=f"prune:{bk}:{int(p.key.netuid)}:{int(p.key.reg_at)}")
            elif not reason:
                w.prune_alerted.discard(tag)
    day = int(snap.block) // 7_200
    if w.last_day is not None and day != w.last_day:
        parts = [f"{k}: NAV {v.nav_rao / RAO_PER_TAO:.4f} TAO, {v.positions} positions, mode {v.mode}"
                 for k, v in sorted(statuses.items())]
        alerts.alert("daily_summary", f"block {int(snap.block)}: " + "; ".join(parts), key=f"day:{day}")
    w.last_day = day
    stalled = bool(getattr(feed, "stalled", False))
    if stalled and not w.stall_alerted:
        alerts.alert("stall", "no finalized head for >= 36 s (feed stalled)", key="stall")
    w.stall_alerted = stalled


def _status(runner: Runner, run_id: str, mode: str, snap: ChainSnapshot | None, health: Any, ticks: int,
            journal: SqliteJournal, recorder: Recorder | None, feed: Any, statuses: Mapping[str, BookStatus],
            note: str = "") -> Any:
    from .ops.health import Status

    seq, h = journal.head()
    lags: dict[str, int] = {}
    if health is not None:
        lags = {"finality_lag_blocks": int(health.finality_lag_blocks), "secs_since_block": int(health.secs_since_block),
                "head_lag_blocks": int(health.head_lag_blocks), "feed_gap_blocks": int(health.feed_gap_blocks),
                "healthy_endpoints": int(health.healthy_endpoints)}
    fd = {"skipped_blocks": int(getattr(feed, "skipped_blocks", 0)),
          "failed_blocks": len(getattr(feed, "failed_blocks", ())), "stalled": bool(getattr(feed, "stalled", False))}
    return Status(run_id=run_id, mode=mode, ticks=ticks, block=None if snap is None else int(snap.block),
                  block_hash="" if snap is None else str(snap.block_hash), lags=lags, journal_seq=int(seq),
                  journal_head_hash=h.hex(), journal_head_block=journal.head_block(), books=dict(statuses),
                  recorder_error=None if recorder is None else recorder.last_compact_error, feed=fd, note=note)


async def _until_stop(stream: AsyncIterator[T], stop: asyncio.Event) -> AsyncIterator[T]:
    """Items of `stream` until it ends or `stop` is set (a pending read is cancelled on stop)."""
    it = stream.__aiter__()
    waiter = asyncio.ensure_future(stop.wait())
    try:
        while not stop.is_set():
            nxt = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({nxt, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if nxt not in done:
                nxt.cancel()
                with contextlib.suppress(BaseException):
                    await nxt
                return
            try:
                item = nxt.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        waiter.cancel()
        with contextlib.suppress(BaseException):
            await waiter


@dataclass
class LoopResult:
    ticks: int
    last_block: int | None
    breaches: dict[str, tuple[str, ...]]
    orphans: dict[str, int]
    stopped_by: str


async def run_loop(runner: Runner, feed: LiveChainFeed | Any, *, run_id: str, mode: str, journal: SqliteJournal,
                   recorder: Recorder | None, health_writer: HealthWriter, alerts: AlertManager, stop: asyncio.Event,
                   max_ticks: int | None = None, tracker: PaperTracker | None = None) -> LoopResult:
    """recover(), then tick every finalized item until stop / max_ticks / the feed ends; heartbeat after every tick."""
    await runner.recover()
    snap: ChainSnapshot | None = runner.recovery.last_snapshot if runner.recovery is not None else None
    statuses = {str(rt.book): _book_status(rt, snap) for rt in runner.books}
    health_writer.write(_status(runner, run_id, mode, snap, None, 0, journal, recorder, feed, statuses, "recovered"),
                        force=True)
    w = _Watch(modes={str(rt.book): rt.state.mode.name for rt in runner.books}, prune_alerted=set())
    ticks = 0
    stopped_by = "feed_end"
    async for item in _until_stop(feed.stream(runner.last_block), stop):
        await runner.tick(item)
        ticks += 1
        snap = item.snapshot
        statuses = {str(rt.book): _book_status(rt, rt.venue.mark_to(snap)) for rt in runner.books}
        health_writer.write(_status(runner, run_id, mode, snap, item.health, ticks, journal, recorder, feed, statuses))
        _watch_tick(runner, snap, alerts, w, statuses, feed)
        if max_ticks is not None and ticks >= max_ticks:
            stopped_by = "max_ticks"
            break
    else:
        if stop.is_set():
            stopped_by = "stop"
    statuses = {str(rt.book): _book_status(rt, snap) for rt in runner.books}
    health_writer.write(_status(runner, run_id, mode, snap, None, ticks, journal, recorder, feed, statuses,
                                f"stopped ({stopped_by})"), force=True)
    return LoopResult(ticks=ticks, last_block=None if runner.last_block is None else int(runner.last_block),
                      breaches={str(rt.book): tuple(rt.state.breaches) for rt in runner.books},
                      orphans={str(rt.book): rt.state.orphans for rt in runner.books}, stopped_by=stopped_by)


def _hashes(cfg: RunCfg) -> tuple[str, str, str]:
    from .backtest.runner import code_hash

    return config_load.config_hash(cfg), code_hash(), config_load.prereg_hash()


def _paper_books(plan: PaperPlan, cal: CalibrationProvider, reader: Any, store: Any) -> list[BookRuntime]:
    from .backtest.books import build_engine
    from .engine.recovery import BookRuntime
    from .risk.overlay import StandardOverlay
    from .venues.paper import PaperVenue

    run = plan.run
    ov = StandardOverlay(cal, run_mode=run.mode, seed=run.seed)
    return [BookRuntime(engine=build_engine(b, run, cal, ov),
                        venue=PaperVenue(b.book, b.exec, reader=reader, seed=run.seed, store=store)) for b in run.books]


def _cmd_paper(a: argparse.Namespace) -> int:
    plan = _paper_plan(a)
    from .ops.lock import InstanceLock, lock_path

    run = plan.run
    lock = InstanceLock(lock_path(run.data_dir, run.run_id, run.mode.value), mode=run.mode.value, run_id=run.run_id)
    lock.acquire()                                            # LockHeld -> exit 4 (a second paper instance is refused)
    try:
        res = asyncio.run(_paper(a, plan))
    finally:
        lock.release()
    _print({"ticks": res.ticks, "last_block": res.last_block, "stopped_by": res.stopped_by,
            "breaches": {k: list(v) for k, v in res.breaches.items()}, "orphans": res.orphans})
    return EXIT_FAIL if any(res.breaches.values()) or any(res.orphans.values()) else EXIT_OK


async def _paper(a: argparse.Namespace, plan: PaperPlan) -> LoopResult:
    from .backtest.books import feature_engine
    from .chain.head import LiveChainFeed, install_signal_handlers
    from .data.calibration import FrozenCalibration
    from .data.collector import ReaderSource
    from .data.journal import SqliteJournal, open_run_state
    from .data.lake import Lake
    from .data.recorder import Recorder
    from .data.store import LakeSnapshotStore
    from .engine.control import ControlWatcher
    from .engine.recovery import SqliteCheckpointStore
    from .engine.runner import Runner
    from .ops.alerts import DeadManPinger
    from .ops.health import STATUS_FILE, HealthWriter, expected_head_from, read_status

    run = plan.run
    alerts = _alerts(toast=not a.no_toast)
    rd = _run_dir(run)
    rd.mkdir(parents=True, exist_ok=True)
    exp = expected_head_from(read_status(rd / STATUS_FILE), run_id=run.run_id)
    journal = SqliteJournal(rd / "journal.sqlite")
    state_conn = open_run_state(rd / "state.sqlite")
    lake = Lake(plan.lake)
    recorder = Recorder(plan.hot, lake, committed_block=journal.head_block)
    store = LakeSnapshotStore(lake, recorder.hot)

    def on_drift(ev: Any) -> None:
        alerts.alert("model_drift", f"{ev.probe} drift {ev.err_ppm} ppm at block {int(ev.block)} (netuid {ev.netuid})",
                     key=f"drift:{ev.probe}:{ev.netuid}")

    reader = _reader(run, provider_check_every=200, on_drift=on_drift)
    src = ReaderSource(reader)
    holder: list[Runner] = []
    tracker = PaperTracker(src.dividend_keys_all, every=plan.membership_every_blocks, top_n=plan.top_n_tracked,
                           held=lambda: _held_pairs(holder[0].books) if holder else [])

    async def on_snapshot(snap: ChainSnapshot) -> None:
        await recorder.aon_snapshot(snap)
        await tracker.maybe_refresh(snap)

    def on_error(block: int, exc: BaseException) -> None:
        alerts.alert("feed", f"block {block} unreadable ({type(exc).__name__}); skipped", key=f"feed:{type(exc).__name__}")

    feed = LiveChainFeed(reader, store, on_snapshot, tracked=tracker.tracked,
                         held=lambda: sorted({int(k.netuid) for k, _ in tracker.held()}), on_error=on_error)
    cal = FrozenCalibration(run.mode)
    books = _paper_books(plan, cal, reader, store)
    hashes = _hashes(run)
    runner = Runner(run_id=run.run_id, mode=run.mode, source=feed, journal=journal,
                    features=feature_engine(cal, warm_blocks=plan.feature_warm_blocks), books=books,
                    features_factory=lambda: feature_engine(cal, warm_blocks=plan.feature_warm_blocks),
                    control=ControlWatcher(_control_dir(run)), checkpoints=SqliteCheckpointStore(state_conn),
                    checkpoint_every_blocks=plan.checkpoint_every_blocks, config_hash=hashes[0], code_hash=hashes[1],
                    prereg_hash=hashes[2], accept_drift=a.accept_drift, expected_head=exp, deep_verify=exp is not None,
                    commit_in_thread=True, on_alert=alerts.hook(),
                    feature_warm_blocks=plan.feature_warm_blocks + FULL_CADENCE_BLOCKS)
    holder.append(runner)
    hw = HealthWriter(rd / STATUS_FILE, min_interval_s=plan.status_interval_s)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(ValueError):                     # not in the main thread (tests): no signal handlers
        install_signal_handlers(loop, stop)
    if a.max_minutes:
        loop.call_later(a.max_minutes * 60.0, stop.set)
    pinger = DeadManPinger(get_secret("healthcheck_url"), interval_s=plan.dead_man_interval_s,
                           is_healthy=lambda: not feed.stalled)
    ping_task = asyncio.ensure_future(pinger.run(stop))
    log.info("paper %s: %d book(s), journal %s, lake %s, hot %s, feature warm-up %d blocks", run.run_id, len(books),
             rd / "journal.sqlite", plan.lake, plan.hot, plan.feature_warm_blocks)
    try:
        res = await run_loop(runner, feed, run_id=run.run_id, mode=run.mode.value, journal=journal, recorder=recorder,
                             health_writer=hw, alerts=alerts, stop=stop, max_ticks=a.max_ticks, tracker=tracker)
    finally:
        stop.set()
        with contextlib.suppress(BaseException):
            await ping_task
        runner.close()
        await feed.aclose()
        try:
            recorder.compact(upto_block=journal.head_block())
        except Exception as e:
            log.warning("final compaction failed (%s); hot staging keeps the records", type(e).__name__)
        recorder.close()
        await reader.pool.aclose()
        journal.close()
        state_conn.close()
        lake.close()
    log.info("paper %s stopped after %d ticks (%s)", run.run_id, res.ticks, res.stopped_by)
    return res


# ================================================================================================= replay
class _OfflineSource:
    """DataSource stand-in for offline recovery: the run store, nothing to stream."""
    cadence_blocks = 1

    def __init__(self, store: Any) -> None:
        self.store = store

    async def stream(self, after: Any) -> AsyncIterator[SourceItem]:
        return
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


class _OfflineReader:
    """ChainReader stand-in: offline verification never reaches the network (every call raises)."""

    def __getattr__(self, name: str) -> Any:
        async def refuse(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError(f"offline replay: no network ({name})")
        return refuse


def _copy_sqlite(src: Path, dst: Path) -> None:
    s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _cmd_replay(a: argparse.Namespace) -> int:
    plan = _paper_plan(a)
    run = replace(plan.run, run_id=a.run) if a.run else plan.run
    plan = replace(plan, run=run)
    rd = _run_dir(run)
    jp = rd / "journal.sqlite"
    if not jp.is_file():
        raise CliError(f"no journal at {jp}")
    if a.recover:
        from .ops.lock import InstanceLock, lock_path

        lock = InstanceLock(lock_path(run.data_dir, run.run_id, run.mode.value), mode=run.mode.value, run_id=run.run_id)
        lock.acquire()
        try:
            out = asyncio.run(_replay(a, plan, jp, in_place=True))
        finally:
            lock.release()
    else:
        with tempfile.TemporaryDirectory(prefix="taotrader-replay-") as tmp:
            copy = Path(tmp) / "journal.sqlite"
            _copy_sqlite(jp, copy)
            out = asyncio.run(_replay(a, plan, copy, in_place=False))
    _print(out)
    ok = out["verified_ticks"] == out["ticks"] and (not out["drift"] or a.accept_drift)
    if not a.recover:
        ok = ok and out["head_before"] == out["head_after"]
    return EXIT_OK if ok else EXIT_FAIL


async def _replay(a: argparse.Namespace, plan: PaperPlan, journal_path: Path, *, in_place: bool) -> dict[str, Any]:
    from .backtest.books import feature_engine
    from .data.calibration import FrozenCalibration
    from .data.journal import SqliteJournal, open_run_state
    from .data.lake import Lake
    from .data.recorder import HotStaging
    from .data.store import LakeSnapshotStore
    from .engine.control import ControlWatcher
    from .engine.recovery import SqliteCheckpointStore
    from .engine.runner import Runner
    from .ops.health import STATUS_FILE, expected_head_from, read_status

    run = plan.run
    rd = _run_dir(run)
    lake = Lake(plan.lake)
    hot = HotStaging(plan.hot, writable=False) if Path(plan.hot).is_dir() else None
    store = LakeSnapshotStore(lake, hot)
    journal = SqliteJournal(journal_path)
    exp = expected_head_from(read_status(rd / STATUS_FILE), run_id=run.run_id)
    reader: Any = _reader(run) if in_place else _OfflineReader()
    cal = FrozenCalibration(run.mode)
    books = _paper_books(plan, cal, reader, store)
    hashes = _hashes(run)
    conn = open_run_state(rd / "state.sqlite") if (in_place or a.use_checkpoints) else None
    runner = Runner(run_id=run.run_id, mode=run.mode, source=_OfflineSource(store), journal=journal,
                    features=feature_engine(cal, warm_blocks=plan.feature_warm_blocks), books=books,
                    features_factory=lambda: feature_engine(cal, warm_blocks=plan.feature_warm_blocks),
                    control=ControlWatcher(_control_dir(run)) if in_place else None,
                    checkpoints=None if conn is None else SqliteCheckpointStore(conn), config_hash=hashes[0],
                    code_hash=hashes[1], prereg_hash=hashes[2], accept_drift=a.accept_drift, expected_head=exp,
                    deep_verify=True, feature_warm_blocks=plan.feature_warm_blocks + FULL_CADENCE_BLOCKS)
    before = journal.head()
    try:
        res = await runner.recover()
    finally:
        runner.close()
        after = journal.head()
        journal.close()
        if conn is not None:
            conn.close()
        if in_place:
            await reader.pool.aclose()
        if hot is not None:
            hot.close()
        lake.close()
    return {"run_id": run.run_id, "mode": "recover" if in_place else "verify-copy", "records": res.records,
            "ticks": res.ticks, "verified_ticks": res.verified_ticks, "drift": res.drift,
            "checkpoint_seq": res.checkpoint_seq, "last_block": None if res.last_block is None else int(res.last_block),
            "head_before": [before[0], before[1].hex()], "head_after": [after[0], after[1].hex()],
            "breaches": {str(rt.book): list(rt.state.breaches) for rt in books},
            "orphans": {str(rt.book): rt.state.orphans for rt in books}}


# ================================================================================================= live
def _arm_state_path(cfg: RunCfg) -> Path:
    return Path(cfg.data_dir) / "live" / ARM_STATE_FILE


def _write_arm_state(cfg: RunCfg, *, armed_unix: int, expiry_unix: int, config_hash: str, source: str) -> None:
    from .ops.health import write_json_atomic

    write_json_atomic(_arm_state_path(cfg), {"armed_unix": armed_unix, "expiry_unix": expiry_unix,
                                             "config_hash": config_hash, "source": source})


def _live_module(name: str) -> Any:
    """Import taotrader.live.<name> at run time, only after the CLI-level locks passed (DESIGN.md 9.2: "cli.py imports
    it lazily only after the gate passes"). The import is dynamic on purpose: a static import here would put
    cli -> live.sdk_port -> bittensor (sdk_port's own lazy loader) into the import graph that the
    `bittensor-only-in-live` contract checks, and no module of this package may import taotrader.live at load time."""
    import importlib

    return importlib.import_module(f"taotrader.live.{name}")


def _pre_gate(cfg: RunCfg, *, cli_live: bool) -> None:
    """Locks 1 and 3, evaluated BEFORE taotrader.live is imported (DESIGN.md 9.2: lazy import after the gate)."""
    from .core.errors import GateError

    fails: list[str] = []
    if not cfg.live.enabled:
        fails.append("lock 1 (config): [live] enabled = true is required")
    if not cli_live:
        fails.append("lock 3 (CLI): --live is required")
    if fails:
        raise GateError("; ".join(fails))


def _cmd_live(a: argparse.Namespace) -> int:
    paths = _paths(a, ())
    cfg = load_config(paths, a.set or ())
    if a.action == "arm":
        return _live_arm(a, cfg)
    if a.submit and not a.live:
        from .core.errors import GateError

        raise GateError("lock 3 (CLI): --submit needs --live as well")
    _pre_gate(cfg, cli_live=a.live)
    if sys.platform == "win32":
        from .core.errors import GateError

        raise GateError("the live adapter runs on Linux/WSL only (DESIGN.md 9.2); use `taotrader paper` on Windows")
    return asyncio.run(_live(a, cfg, paths))


def _live_arm(a: argparse.Namespace, cfg: RunCfg) -> int:
    """`taotrader live arm`: print a TAOTRADER_LIVE_ARMED token for the current config (never logged)."""
    gate = _live_module("gate")                               # gate itself never imports bittensor
    from .ops.secrets import set_secret

    if a.init_secret:
        if get_secret(gate.ARM_SECRET_NAME) is not None:
            raise CliError("an arm secret already exists in the keyring; delete it there first to rotate it")
        set_secret(gate.ARM_SECRET_NAME, gate.new_arm_secret())
        print("arm secret created in the OS keyring (taotrader/live_arm_hmac)")
    if a.ttl_hours > MAX_ARM_TTL_HOURS:
        raise CliError(f"--ttl-hours must be <= {MAX_ARM_TTL_HOURS}")
    now = int(time.time())
    expiry = now + int(a.ttl_hours * 3600)
    h = config_load.config_hash(cfg)
    token = gate.make_arm_token(gate.load_arm_secret(), h, expiry, now_unix=now)
    _write_arm_state(cfg, armed_unix=now, expiry_unix=expiry, config_hash=h, source="live arm")
    log.warning("live arm token created for config %s..., expires at %d (token not logged)", h[:16], expiry)
    print("# set this in the live shell (any config edit invalidates it):", file=sys.stderr)
    print(f"export {gate.ARM_ENV}='{token}'")
    return EXIT_OK


def _live_engine(book: BookCfg, run: RunCfg, cal: CalibrationProvider, overlay: RiskOverlay,
                 delegates: Sequence[str]) -> Engine:
    from .backtest.books import PRUNE_BLIND_ID, PruneBlindOverlay
    from .engine.engine import Engine
    from .portfolio.allocator import StandardAllocator
    from .portfolio.planner import StandardPlanner
    from .risk.liquidity import caps
    from .risk.router import Router
    from .strategies.base import build_strategy

    strategies = [build_strategy(s, exec_cfg=book.exec, risk=book.risk, calibration=cal) for s in book.sleeves]
    ov: RiskOverlay = overlay
    if book.sleeves and all(str(s.strategy).startswith(PRUNE_BLIND_ID) for s in book.sleeves):
        ov = PruneBlindOverlay(overlay)
    return Engine(run_id=run.run_id, cfg=book, mode=run.mode, strategies=strategies, router=Router(book.exec), caps=caps,
                  allocator=StandardAllocator(book, run_mode=run.mode, live_sleeves=run.live.sleeves), overlay=ov,
                  planner=StandardPlanner(book, run_mode=run.mode, live=run.live), calibration=cal,
                  delegates=tuple(delegates))


async def _live(a: argparse.Namespace, cfg: RunCfg, paths: Sequence[Path]) -> int:
    """Wiring of DESIGN.md 9.2-9.7 (WP11 notes): gate -> runtime objects -> journal pre-fold -> preflight -> recover
    -> stream. Only reached on Linux/WSL after locks 1 and 3."""
    from .backtest.books import feature_engine
    from .chain.head import LiveChainFeed, install_signal_handlers
    from .core.events import HealthObs
    from .core.state import ReadPlan
    from .core.units import RunMode
    from .data.calibration import FrozenCalibration
    from .data.collector import ReaderSource
    from .data.journal import SqliteJournal, open_run_state
    from .data.lake import Lake
    from .data.recorder import Recorder
    from .data.store import LakeSnapshotStore
    from .engine.control import KILL_FILE, ControlWatcher
    from .engine.recovery import BookRuntime, SqliteCheckpointStore, read_batches
    from .engine.reducer import fold_batch
    from .engine.runner import Runner
    gate, nonce, preflight = _live_module("gate"), _live_module("nonce"), _live_module("preflight")
    reconcile, sdk_port, live_venue = _live_module("reconcile"), _live_module("sdk_port"), _live_module("venue")
    from .ops.health import STATUS_FILE, HealthWriter, expected_head_from, read_status
    from .ops.lock import InstanceLock, lock_path
    from .risk.overlay import StandardOverlay

    alerts = _alerts(toast=False)
    h = config_load.config_hash(cfg)
    keys = gate.explicit_live_keys(paths, os.environ, a.set or ())
    reader = _reader(cfg, provider_check_every=200)
    sdk: Any = None
    lock: InstanceLock | None = None
    closers: list[Callable[[], object]] = []                     # run in reverse in the finally (preflight may refuse)
    try:
        head, hh = await reader.finalized_head()
        spec = (await reader.spec_version(hh))[0]
        secret = gate.load_arm_secret() if a.submit else None
        decision = gate.evaluate_gate(cfg, config_hash=h, cli_live=a.live, cli_submit=a.submit, env=os.environ,
                                      secret=secret, now_unix=int(time.time()), spec_version=spec, explicit_keys=keys)
        log.warning("live gate: %s (%s) on %s", decision.state.value, "; ".join(decision.reasons) or "all locks",
                    decision.network)
        run_mode = RunMode.LIVE if decision.submit else RunMode.LIVE_DRY
        run = replace(cfg, mode=run_mode)
        if decision.state is gate.LiveState.ARMED and decision.token_expiry is not None:
            _write_arm_state(cfg, armed_unix=int(time.time()), expiry_unix=decision.token_expiry, config_hash=h,
                             source="live start")
        rd = _run_dir(run)
        rd.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(lock_path(run.data_dir, run.run_id, run_mode.value), mode=run_mode.value, run_id=run.run_id)
        lock.acquire()                                            # before the SDK connects: one live process per run
        sdk = sdk_port.RealSdk(cfg.live)
        await sdk.connect()
        arming = gate.Arming(decision, cfg.live, clock=lambda: int(time.time()))
        exp = expected_head_from(read_status(rd / STATUS_FILE), run_id=run.run_id)
        journal = SqliteJournal(rd / "journal.sqlite")
        closers.append(journal.close)
        state_conn = open_run_state(rd / "state.sqlite")
        closers.append(state_conn.close)
        lake = Lake(Path(run.data_dir) / "live" / "lake")
        closers.append(lake.close)
        recorder = Recorder(Path(run.data_dir) / "live" / "hot", lake, committed_block=journal.head_block)
        closers.append(recorder.close)
        store = LakeSnapshotStore(lake, recorder.hot)
        cal = FrozenCalibration(run_mode)
        ov = StandardOverlay(cal, run_mode=run_mode, seed=run.seed)
        kill = _control_dir(run) / KILL_FILE
        books = [BookRuntime(engine=_live_engine(b, run, cal, ov, cfg.live.delegate_wallets),
                             venue=live_venue.LiveVenue(b.book, sdk=sdk, reader=reader, live=cfg.live, risk=b.risk,
                                                        exec_cfg=b.exec, arming=arming,
                                                        spec_checks=preflight.RiskExitChecks(reader, sdk),
                                                        submissions=nonce.SqliteSubmissions(state_conn),
                                                        kill_file=kill))
                 for b in run.books]
        # journal pre-fold (no side effects): held positions and reconciliation state for preflight
        states = {str(rt.book): rt.engine.initial_state() for rt in books}
        for batch in read_batches(journal):
            for rt in books:
                evs = [e for _, bk, e in batch.items if bk in ("", rt.book)]
                if evs:
                    states[str(rt.book)] = fold_batch(states[str(rt.book)], evs)
        held = sorted({(p.key, p.hotkey) for st in states.values() for p in st.portfolio.positions},
                      key=lambda kh: (int(kh[0].netuid), int(kh[0].reg_at), str(kh[1])))
        clean = all(st.orphans == 0 and not st.recon_halt for st in states.values())
        snap = await reader.snapshot(head, hh, ReadPlan.FULL, None, list(held))
        health = HealthObs(finality_lag_blocks=0, secs_since_block=0, healthy_endpoints=reader.pool.healthy(),
                           head_lag_blocks=0, feed_gap_blocks=0)
        report = await preflight.run_preflight(sdk, live=cfg.live, risk=run.books[0].risk, snap=snap, health=health,
                                               held=held, recon_clean=clean)
        preflight.require_preflight(report)                 # GateError at start (exit 3)
        src = ReaderSource(reader)
        holder: list[Runner] = []
        tracker = PaperTracker(src.dividend_keys_all, every=7_200, top_n=5,
                               held=lambda: _held_pairs(holder[0].books) if holder else [])

        async def on_snapshot(s: ChainSnapshot) -> None:
            await recorder.aon_snapshot(s)
            await tracker.maybe_refresh(s)

        feed = LiveChainFeed(reader, store, on_snapshot, tracked=tracker.tracked,
                             held=lambda: sorted({int(k.netuid) for k, _ in tracker.held()}))
        on_alert = alerts.hook()
        recon = reconcile.LiveReconciler(sdk, reader, live=cfg.live, risk=run.books[0].risk,
                                         monitor=gate.ArmingMonitor(arming, run.books[0].risk, on_alert),
                                         on_alert=on_alert)
        hashes = _hashes(cfg)
        runner = Runner(run_id=run.run_id, mode=run_mode, source=feed, journal=journal,
                        features=feature_engine(cal), books=books, features_factory=lambda: feature_engine(cal),
                        control=ControlWatcher(_control_dir(run)), checkpoints=SqliteCheckpointStore(state_conn),
                        config_hash=hashes[0], code_hash=hashes[1], prereg_hash=hashes[2],
                        accept_drift=a.accept_drift, expected_head=exp, deep_verify=exp is not None,
                        commit_in_thread=True, on_alert=on_alert, reconcile=recon)
        holder.append(runner)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        install_signal_handlers(loop, stop)
        if a.max_minutes:
            loop.call_later(a.max_minutes * 60.0, stop.set)
        try:
            res = await run_loop(runner, feed, run_id=run.run_id, mode=run_mode.value, journal=journal,
                                 recorder=recorder, health_writer=HealthWriter(rd / STATUS_FILE), alerts=alerts,
                                 stop=stop, tracker=tracker)
        finally:
            runner.close()
            await feed.aclose()
            with contextlib.suppress(Exception):
                recorder.compact(upto_block=journal.head_block())
    finally:
        for close in reversed(closers):
            with contextlib.suppress(Exception):
                close()
        if lock is not None:
            lock.release()
        if sdk is not None:
            with contextlib.suppress(Exception):
                await sdk.close()
        await reader.pool.aclose()
    _print({"ticks": res.ticks, "last_block": res.last_block, "stopped_by": res.stopped_by, "orphans": res.orphans})
    return EXIT_FAIL if any(res.breaches.values()) else EXIT_OK


# ================================================================================================= doctor
@dataclass(frozen=True, slots=True)
class DoctorFinding:
    level: str              # OK | WARN | FAIL
    check: str
    detail: str


def doctor_live(cfg: RunCfg, *, now_unix: int, max_idle_hours: int = IDLE_PROXY_HOURS,
                alerts: AlertManager | None = None) -> list[DoctorFinding]:
    """The section 12.2 idle-proxy check: live not armed for more than `max_idle_hours` -> alert "remove Staking
    proxies (`btcli proxy remove`)" listing the configured delegates. The last arm time is the latest moment a token
    was valid (min(now, expiry) of the last `live arm` / armed start, data/live/arm_state.json)."""
    out: list[DoctorFinding] = []
    delegates = list(cfg.live.delegate_wallets)
    expiry: int | None = None
    armed: int | None = None
    try:
        doc = json.loads(_arm_state_path(cfg).read_text(encoding="utf-8"))
        expiry, armed = int(doc["expiry_unix"]), int(doc["armed_unix"])
    except (OSError, ValueError, KeyError, TypeError):
        expiry = armed = None
    if expiry is None:
        last_valid = None
        idle_h = None
    else:
        last_valid = min(now_unix, expiry)
        idle_h = (now_unix - last_valid) / 3600.0
    if not delegates:
        out.append(DoctorFinding("OK", "idle proxies", "no delegate wallets configured ([live].delegate_wallets)"))
        return out
    listing = ", ".join(delegates)
    if idle_h is None or idle_h > max_idle_hours:
        since = "never armed on this host" if idle_h is None else f"last armed {idle_h:.1f} h ago (armed at {armed})"
        msg = (f"live has not been armed for more than {max_idle_hours} h ({since}): remove Staking proxies "
               f"(`btcli proxy remove`) for the delegates {listing}. A zero-delay Staking proxy stays active on chain after "
               "the arm token expires; re-adding it later is one coldkey signature per delegate.")
        out.append(DoctorFinding("FAIL", "idle proxies", msg))
        if alerts is not None:
            alerts.alert("idle_proxy", msg, key="idle_proxy")
    else:
        out.append(DoctorFinding("OK", "idle proxies", f"armed within the last {idle_h:.1f} h (delegates {listing})"))
        exp_in = (expiry - now_unix) if expiry is not None else None
        if exp_in is not None and 0 < exp_in <= 2 * 3600:
            out.append(DoctorFinding("WARN", "arm token", f"the arm token expires in {exp_in // 60} min"))
    return out


def doctor_checks(cfg: RunCfg, *, now_unix: float, stale_s: int) -> list[DoctorFinding]:
    from .ops.health import STATUS_FILE, read_status, status_age_s
    from .ops.lock import read_lock_info

    out: list[DoctorFinding] = []
    v = sys.version_info
    out.append(DoctorFinding("OK" if (v.major, v.minor) == (3, 11) else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro}"))
    data = Path(cfg.data_dir)
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        free_gb = shutil.disk_usage(data).free / 1e9
        out.append(DoctorFinding("OK" if free_gb >= 20 else "WARN", "data dir", f"{data.resolve()} writable, "
                                 f"{free_gb:.1f} GB free"))
        if "onedrive" in str(data.resolve()).lower():
            out.append(DoctorFinding("WARN", "data dir", "data/ is inside OneDrive: move it out (SQLite WAL and Parquet)"))
    except OSError as e:
        out.append(DoctorFinding("FAIL", "data dir", f"{data}: {e}"))
    names = sorted(KNOWN_SECRETS)
    present = []
    for n in names:
        try:
            present.append(f"{n}={'set' if get_secret(n) is not None else 'unset'}")
        except SecretError as e:
            out.append(DoctorFinding("FAIL", "secrets", str(e)))
    out.append(DoctorFinding("OK", "secrets", ", ".join(present) + " (values never shown)"))
    runs = data / "runs"
    for d in sorted(runs.glob("*")) if runs.is_dir() else []:
        doc = read_status(d / STATUS_FILE)
        if doc is None:
            continue
        age = status_age_s(doc, now_unix)
        breaches = {k: v.get("breaches") for k, v in (doc.get("books") or {}).items() if v.get("breaches")}
        orphans = {k: v.get("orphans") for k, v in (doc.get("books") or {}).items() if v.get("orphans")}
        running = any(read_lock_info(p) is not None for p in d.glob("*.lock"))
        lvl = "OK"
        notes = [f"block {doc.get('block')}", f"heartbeat {age:.0f} s old" if age is not None else "no heartbeat time"]
        if running and age is not None and age > stale_s:
            lvl = "FAIL"
            notes.append(f"STALE (> {stale_s} s) while the lock is held")
        if breaches or orphans:
            lvl = "FAIL"
            notes.append(f"breaches {breaches} orphans {orphans}")
        out.append(DoctorFinding(lvl, f"run {d.name}", ", ".join(notes) + (" (running)" if running else "")))
    try:
        from .data.journal import SqliteJournal  # noqa: F401  (import check: duckdb/zstandard present)
        out.append(DoctorFinding("OK", "imports", "core runtime imports"))
    except ImportError as e:
        out.append(DoctorFinding("FAIL", "imports", str(e)))
    out.append(DoctorFinding("OK" if not cfg.live.enabled or sys.platform != "win32" else "WARN", "live config",
                             f"[live] enabled={cfg.live.enabled}, mode={cfg.live.mode}, network={cfg.live.network}"))
    return out


async def _doctor_network(cfg: RunCfg) -> list[DoctorFinding]:
    out: list[DoctorFinding] = []
    reader = _reader(cfg)
    try:
        head, _ = await reader.finalized_head()
        out.append(DoctorFinding("OK", "rpc", f"finalized head {int(head)}; healthy endpoints {reader.pool.healthy()}"))
    except Exception as e:
        out.append(DoctorFinding("FAIL", "rpc", f"{type(e).__name__}: {e}"[:300]))
    finally:
        await reader.pool.aclose()
    return out


def _cmd_doctor(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    now = time.time()
    findings = doctor_checks(cfg, now_unix=now, stale_s=a.stale_heartbeat_s)
    if a.network:
        findings += asyncio.run(_doctor_network(cfg))
    if a.live:
        alerts = _alerts(toast=False)
        findings += doctor_live(cfg, now_unix=int(now), max_idle_hours=a.max_idle_hours, alerts=alerts)
        if sys.platform != "win32":
            try:
                import importlib.util
                ok = importlib.util.find_spec("bittensor") is not None
            except (ImportError, ValueError):
                ok = False
            findings.append(DoctorFinding("OK" if ok else "WARN", "bittensor", "installed" if ok else
                                          "not importable in this venv (pip install --require-hashes -r requirements-live.txt)"))
    from .ops.logging import redact_line

    for f in findings:
        print(redact_line(f"[{f.level:4}] {f.check}: {f.detail}"))
    return EXIT_FAIL if any(f.level == "FAIL" for f in findings) else EXIT_OK


_HANDLERS: Final[Mapping[str, Callable[[argparse.Namespace], int]]] = {
    "collect": _cmd_collect, "refine": _cmd_refine, "verify": _cmd_verify, "verify-metadata": _cmd_verify_metadata,
    "verify-journal": _cmd_verify_journal, "verify-lake": _cmd_verify_lake, "backtest": _cmd_backtest, "grid": _cmd_grid,
    "study": _cmd_study, "report": _cmd_report, "paper": _cmd_paper, "replay": _cmd_replay, "halt": _cmd_halt,
    "resume": _cmd_resume, "exits-only": _cmd_exits_only, "flatten": _cmd_flatten,
    "clear-quarantine": _cmd_clear_quarantine, "live": _cmd_live,
    "doctor": _cmd_doctor,
}
