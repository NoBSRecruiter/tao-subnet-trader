"""WP12 CLI wiring (DESIGN.md section 11 WP12 acceptance):

- every command parses, loads its configuration through WP0's ops.config_load and fails closed on bad input
  (bad arguments, bad config values, missing files, missing secrets) BEFORE any network or engine work starts;
- operator commands reach the engine's control watcher;
- a second paper instance is refused by the single-instance lock (exit 4);
- `doctor --live` raises the idle-proxy alert when the last arm is older than 72 h (fake clock);
- `live` is gated in this module and `taotrader.live` is imported only after locks 1 and 3; `live arm` prints a
  verifiable token that never reaches the log;
- secrets never appear in the CLI's log file or stderr.
The research commands are exercised end to end on a tiny window of the committed mini-lake.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from taotrader import cli
from taotrader.core.units import Block
from taotrader.engine.control import KILL_FILE, ControlWatcher
from taotrader.ops import config_load
from taotrader.ops.alerts import AlertManager, MemorySink
from taotrader.ops.lock import InstanceLock, lock_path

ROOT = Path(__file__).resolve().parents[2]
BOOKS_PAPER = ROOT / "config" / "books.paper.toml"
MINILAKE = ROOT / "tests" / "fixtures" / "minilake"
DELEGATES = '["ops-a","ops-b"]'


@pytest.fixture
def run_cli(no_secret_env: None, tmp_path: Path) -> Callable[..., int]:
    """main() with logs in tmp_path/logs (every test checks exit codes; some read the log file)."""
    def go(*argv: str) -> int:
        cmd, rest = argv[0], list(argv[1:])
        if cmd == "live" and rest and rest[0] in ("arm", "run"):
            return cli.main([cmd, rest[0], "--log-dir", str(tmp_path / "logs"), "--quiet", *rest[1:]])
        if cmd == "refine":
            return cli.main([cmd, "--log-dir", str(tmp_path / "logs"), "--quiet", *rest])
        return cli.main([cmd, "--log-dir", str(tmp_path / "logs"), "--quiet", *rest])
    return go


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any attempt to build an RPC pool or reader fails the test (fail-closed checks must come first)."""
    calls: list[str] = []

    def refuse(*_a: Any, **_k: Any) -> Any:
        calls.append("network")
        raise AssertionError("the command reached the network before validating its input")
    monkeypatch.setattr(cli, "_pool", refuse)
    monkeypatch.setattr(cli, "_reader", refuse)
    return calls


def _logs(tmp_path: Path) -> str:
    d = tmp_path / "logs"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(d.glob("*.jsonl*"))) if d.is_dir() else ""


# ================================================================================================= parsing
PARSE_CASES: list[list[str]] = [
    ["collect"], ["collect", "--catch-up", "--schedule", "c60", "--schedule", "h300", "--max-chunks", "3"],
    ["collect", "--status"], ["collect", "--enrich", "--netuids", "1,2", "--from-block", "1", "--to-block", "9"],
    ["refine", "lifecycle", "--lake", "x"],
    ["verify", "--spec"], ["verify", "--taostats", "--netuids", "3", "--from-block", "1", "--to-block", "2"],
    ["verify-metadata", "--block", "5"], ["verify-journal", "--all"], ["verify-journal", "--run", "r"],
    ["verify-lake", "--deep"],
    ["backtest", "--books", "carry,base-cash", "--start", "1", "--end", "2", "--report"],
    ["grid", "--capacity", "carry", "--capitals", "1,3", "--workers", "2"],
    ["study", "s0"], ["study", "all", "--out", "o"], ["report"], ["report", "--run", "paper-main"],
    ["paper"], ["paper", "--max-minutes", "10", "--max-ticks", "5", "--accept-drift", "--no-toast"],
    ["replay"], ["replay", "--recover", "--accept-drift"],
    ["halt", "--reason", "drill"], ["halt", "--kill"], ["resume", "--clear-kill"], ["exits-only"], ["flatten", "17"],
    ["clear-quarantine", "--book", "paper-carry", "--reason", "reconciled", "--run", "r"],
    ["live"], ["live", "--live"], ["live", "--live", "--submit"], ["live", "arm", "--ttl-hours", "12"],
    ["live", "arm", "--init-secret"], ["doctor"], ["doctor", "--live", "--network", "--max-idle-hours", "72"],
]


@pytest.mark.parametrize("argv", PARSE_CASES, ids=lambda a: " ".join(a))
def test_every_command_parses(argv: list[str]) -> None:
    a = cli.build_parser().parse_args(argv)
    assert a.command == argv[0]
    assert a.command in cli._HANDLERS


def test_the_handler_table_covers_every_documented_command() -> None:
    documented = {"collect", "refine", "verify", "verify-metadata", "verify-journal", "verify-lake", "backtest", "grid",
                  "study", "report", "paper", "replay", "halt", "resume", "exits-only", "flatten", "clear-quarantine",
                  "live", "doctor"}
    assert set(cli._HANDLERS) == documented
    for name in sorted(documented):
        assert cli.main([name, "--help"]) == 0


@pytest.mark.parametrize("argv", [
    [], ["no-such-command"], ["flatten"], ["flatten", "-1"], ["flatten", "70000"], ["flatten", "x"],
    ["grid", "--capitals", "1,x"], ["paper", "--max-ticks", "0"], ["paper", "--max-minutes", "-1"],
    ["paper", "--max-minutes", "nan"], ["live", "arm", "--ttl-hours", "0"], ["live", "launch"],
    ["collect", "--schedule", "c999"], ["verify-journal", "--all", "--run", "x"], ["study", "s9"],
    ["doctor", "--log-level", "TRACE"], ["clear-quarantine", "--book", "b"], ["clear-quarantine", "--reason", "r"],
], ids=lambda a: " ".join(a) or "<none>")
def test_bad_arguments_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == cli.EXIT_USAGE


# ================================================================================================= config, fail closed
BAD_CONFIG_CASES: list[list[str]] = [
    ["collect"], ["collect", "--status"], ["refine", "lifecycle"], ["verify", "--spec"], ["verify-metadata"],
    ["verify-journal"], ["verify-lake"], ["backtest"], ["grid", "--capacity", "carry"], ["study", "s0"], ["report"],
    ["report", "--run", "x"], ["paper"], ["replay"], ["halt"], ["resume"], ["exits-only"], ["flatten", "3"],
    ["clear-quarantine", "--book", "paper-carry", "--reason", "r"],
    ["live", "run", "--live"], ["live", "run", "--live", "--submit"], ["live", "arm"], ["doctor"],
    ["doctor", "--live"],
]


@pytest.mark.parametrize("argv", BAD_CONFIG_CASES, ids=lambda a: " ".join(a))
def test_every_command_rejects_an_unknown_config_key_before_doing_anything(
        argv: list[str], run_cli: Callable[..., int], no_network: list[str], tmp_cfg: tuple[Path, Path]) -> None:
    cfg, data = tmp_cfg
    assert run_cli(*argv, "--config", str(cfg), "--set", "no_such_key=1") == cli.EXIT_USAGE
    assert no_network == []
    assert not (data / "control").exists()                 # operator commands wrote nothing


@pytest.mark.parametrize("argv", [["halt"], ["doctor"], ["paper"], ["backtest"], ["live", "run", "--live"]],
                         ids=lambda a: " ".join(a))
def test_bad_values_missing_files_and_bad_toml_fail_closed(argv: list[str], run_cli: Callable[..., int],
                                                           no_network: list[str], tmp_path: Path) -> None:
    assert run_cli(*argv, "--config", str(tmp_path / "missing.toml")) == cli.EXIT_USAGE
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml\n", encoding="utf-8")
    assert run_cli(*argv, "--config", str(bad)) == cli.EXIT_USAGE
    assert run_cli(*argv, "--set", "rpc.rate_per_s=-1") == cli.EXIT_USAGE      # a strict RunCfg value check
    assert run_cli(*argv, "--set", "malformed-override") == cli.EXIT_USAGE
    assert no_network == []


def test_commands_load_through_ops_config_load(run_cli: Callable[..., int], monkeypatch: pytest.MonkeyPatch,
                                               tmp_cfg: tuple[Path, Path]) -> None:
    seen: list[int] = []
    real = config_load.build_run_config

    def spy(raw: Any) -> Any:
        seen.append(1)
        return real(raw)
    monkeypatch.setattr(config_load, "build_run_config", spy)
    cfg, _ = tmp_cfg
    for argv in (["halt"], ["resume"], ["exits-only"], ["flatten", "4"], ["doctor"], ["verify-journal"],
                 ["live", "run", "--live"]):
        before = len(seen)
        run_cli(*argv, "--config", str(cfg))
        assert len(seen) > before, argv


def test_environment_overrides_are_applied_and_validated(run_cli: Callable[..., int], monkeypatch: pytest.MonkeyPatch,
                                                         tmp_cfg: tuple[Path, Path]) -> None:
    cfg, _ = tmp_cfg
    monkeypatch.setenv("TAOTRADER_CFG_RPC__RATE_PER_S", "-3")
    assert run_cli("doctor", "--config", str(cfg)) == cli.EXIT_USAGE
    monkeypatch.setenv("TAOTRADER_CFG_RPC__RATE_PER_S", "2.0")
    assert run_cli("doctor", "--config", str(cfg)) == cli.EXIT_OK


def test_books_paper_toml_is_a_valid_paper_plan(no_secret_env: None) -> None:
    plan = cli.load_paper_plan([config_load.DEFAULT_CONFIG, BOOKS_PAPER], env={})
    assert plan.run.mode.value == "paper" and plan.run.run_id == "paper-main"
    books = {str(b.book): b for b in plan.run.books}
    assert {"paper-carry", "paper-carry-persist", "paper-momentum-shadow", "paper-ew-total"} <= set(books)
    assert books["paper-carry-persist"].exec.impact_half_life_blocks is None        # the PERSISTENT impact bound
    assert books["paper-carry"].exec.impact_half_life_blocks is not None
    assert {s.stage.name for s in books["paper-momentum-shadow"].sleeves} == {"SHADOW"}   # experimental: shadow only
    assert plan.feature_warm_blocks == 216_000 and plan.lake == "data/paper/lake"
    assert not plan.run.live.enabled


@pytest.mark.parametrize("override,msg", [
    ("paper.bogus=1", "unknown key"), ("paper.feature_warm_blocks=-5", "non-negative"),
    ("paper.lake=3", "path string"), ('mode="backtest"', "mode"), ("paper.top_n_tracked=0", ">= 1"),
    ("paper.status_interval_s=true", "number"),
])
def test_paper_plan_fails_closed(override: str, msg: str, no_secret_env: None) -> None:
    with pytest.raises(config_load.ConfigError, match=msg):
        cli.load_paper_plan([config_load.DEFAULT_CONFIG, BOOKS_PAPER], [override], env={})


def test_paper_needs_books(tmp_path: Path, no_secret_env: None) -> None:
    p = tmp_path / "nobooks.toml"
    p.write_text('mode = "paper"\n', encoding="utf-8")
    with pytest.raises(config_load.ConfigError, match="books"):
        cli.load_paper_plan([config_load.DEFAULT_CONFIG, p], env={})


@pytest.mark.parametrize("argv,needle", [
    (["verify"], "--spec"), (["refine"], "sub-command"), (["collect", "--enrich"], "--enrich"),
    (["verify", "--taostats"], "--taostats"), (["verify-lake", "--lake", "nowhere"], "no lake"),
    (["backtest", "--lake", "nowhere"], "no lake"), (["grid", "--lake", "nowhere", "--capacity", "carry"], "no lake"),
    (["report", "--lake", "nowhere"], "no lake"), (["report", "--run", "none"], "no journal"),
    (["replay", "--run", "none"], "no journal"),
])
def test_commands_refuse_incomplete_requests(argv: list[str], needle: str, run_cli: Callable[..., int],
                                             no_network: list[str], tmp_cfg: tuple[Path, Path],
                                             capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _ = tmp_cfg
    extra = ["--config", str(BOOKS_PAPER), "--config", str(cfg)] if argv[0] in ("replay",) or "--run" in argv else \
        ([] if argv[0] in ("backtest", "grid", "report") else ["--config", str(cfg)])
    assert run_cli(*argv, *extra) == cli.EXIT_USAGE
    assert needle in capsys.readouterr().err
    assert no_network == []


def test_grid_needs_a_known_base_book(run_cli: Callable[..., int], capsys: pytest.CaptureFixture[str]) -> None:
    lake = str(MINILAKE / "lake")
    assert run_cli("grid", "--lake", lake) == cli.EXIT_USAGE
    assert run_cli("grid", "--lake", lake, "--capacity", "no-such-book") == cli.EXIT_USAGE
    assert "unknown base book" in capsys.readouterr().err


# ================================================================================================= operator commands
def test_operator_commands_reach_the_control_watcher(run_cli: Callable[..., int], tmp_cfg: tuple[Path, Path]) -> None:
    cfg, data = tmp_cfg
    ctl = data / "control"
    assert run_cli("halt", "--config", str(cfg), "--reason", "drill") == 0
    assert run_cli("flatten", "17", "--config", str(cfg)) == 0
    assert run_cli("exits-only", "--config", str(cfg)) == 0
    cmds = ControlWatcher(ctl).poll(Block(100), halted=False, resume_count=0)
    assert sorted(c.command for c in cmds) == ["exits_only", "flatten:17", "halt"]
    assert any(c.reason == "drill" for c in cmds)


def test_halt_kill_and_resume_clear_kill(run_cli: Callable[..., int], tmp_cfg: tuple[Path, Path],
                                         capsys: pytest.CaptureFixture[str]) -> None:
    cfg, data = tmp_cfg
    kill = data / "control" / KILL_FILE
    assert run_cli("halt", "--kill", "--config", str(cfg)) == 0
    assert kill.is_file()
    assert run_cli("resume", "--config", str(cfg)) == 0
    assert "stays halted" in capsys.readouterr().err                          # the kill file still wins
    assert kill.is_file()
    assert run_cli("resume", "--clear-kill", "--config", str(cfg)) == 0
    assert not kill.exists()


# ================================================================================================= single instance
def test_a_second_paper_instance_is_refused_by_the_lock(run_cli: Callable[..., int], tmp_cfg: tuple[Path, Path],
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    cfg, data = tmp_cfg
    started: list[int] = []

    async def fake_paper(*_a: Any, **_k: Any) -> Any:
        started.append(1)
        return cli.LoopResult(ticks=0, last_block=None, breaches={}, orphans={}, stopped_by="test")
    monkeypatch.setattr(cli, "_paper", fake_paper)
    args = ("paper", "--config", str(BOOKS_PAPER), "--config", str(cfg))
    with InstanceLock(lock_path(data, "paper-main", "paper"), mode="paper", run_id="paper-main"):
        assert run_cli(*args) == cli.EXIT_LOCKED
        assert "another instance" in capsys.readouterr().err
        assert started == []
        assert run_cli("replay", "--recover", "--config", str(BOOKS_PAPER), "--config", str(cfg)) in (
            cli.EXIT_LOCKED, cli.EXIT_USAGE)                                   # no journal yet: refused either way
    assert run_cli(*args) == cli.EXIT_OK                                      # free again
    assert started == [1]


def test_a_second_paper_process_is_refused(tmp_cfg: tuple[Path, Path], tmp_path: Path) -> None:
    """Across processes: a real holder process keeps the lock; the CLI in another process exits 4."""
    cfg, data = tmp_cfg
    lp = lock_path(data, "paper-main", "paper")
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from taotrader.ops.lock import InstanceLock
        lk = InstanceLock({str(lp)!r}, mode="paper", run_id="paper-main").acquire()   # kept referenced: held
        print("LOCKED", flush=True)
        time.sleep(60)
    """)
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "LOCKED"
        dead = '["http://127.0.0.1:9"]'                     # if the lock ever failed, no real endpoint is reached
        r = subprocess.run([sys.executable, "-m", "taotrader", "paper", "--config", str(BOOKS_PAPER), "--config",
                            str(cfg), "--set", f"rpc.archive_endpoints={dead}", "--set", "rpc.head_endpoints=[]",
                            "--set", f"paper.lake={(tmp_path / 'pl').as_posix()}",
                            "--set", f"paper.hot={(tmp_path / 'ph').as_posix()}", "--max-ticks", "1",
                            "--log-dir", str(tmp_path / "logs2"), "--quiet"], capture_output=True, text=True,
                           timeout=120, cwd=tmp_path)
        assert r.returncode == cli.EXIT_LOCKED, r.stderr
        assert "another instance" in r.stderr
    finally:
        holder.kill()
        holder.wait(30)


# ================================================================================================= doctor --live
def _arm_state(data: Path, armed: int, expiry: int) -> None:
    p = data / "live" / "arm_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"armed_unix": armed, "expiry_unix": expiry, "config_hash": "x", "source": "test"}),
                 encoding="utf-8")


def _live_cfg(cfg_path: Path, *sets: str) -> Any:
    return cli.load_config([config_load.DEFAULT_CONFIG, cfg_path], [f"live.delegate_wallets={DELEGATES}", *sets],
                           env={})


def test_doctor_live_idle_proxy_alert_after_72_hours(tmp_cfg: tuple[Path, Path], no_secret_env: None) -> None:
    cfg_path, data = tmp_cfg
    cfg = _live_cfg(cfg_path)
    now = 2_000_000_000
    sink = MemorySink()
    am = AlertManager(sinks=[sink], clock=lambda: float(now))
    _arm_state(data, now - 100 * 3600, now - 73 * 3600)                     # token lapsed 73 h ago
    f = cli.doctor_live(cfg, now_unix=now, alerts=am)
    assert [x.level for x in f] == ["FAIL"]
    assert "remove Staking proxies" in f[0].detail and "btcli proxy remove" in f[0].detail
    assert "ops-a" in f[0].detail and "ops-b" in f[0].detail
    assert len(sink.alerts) == 1 and sink.alerts[0].kind == "idle_proxy"
    assert "ops-a, ops-b" in sink.alerts[0].message
    sink.alerts.clear()
    _arm_state(data, now - 90 * 3600, now - 71 * 3600)                      # 71 h: still inside the window
    f = cli.doctor_live(cfg, now_unix=now, alerts=am)
    assert [x.level for x in f] == ["OK"] and sink.alerts == []
    _arm_state(data, now - 3600, now + 3600)                                # armed now; expires within 2 h
    f = cli.doctor_live(cfg, now_unix=now, alerts=am)
    assert [x.level for x in f] == ["OK", "WARN"]


def test_doctor_live_never_armed_and_no_delegates(tmp_cfg: tuple[Path, Path], no_secret_env: None) -> None:
    cfg_path, _ = tmp_cfg
    f = cli.doctor_live(_live_cfg(cfg_path), now_unix=1_000)
    assert f[0].level == "FAIL" and "never armed" in f[0].detail
    f = cli.doctor_live(cli.load_config([config_load.DEFAULT_CONFIG, cfg_path], env={}), now_unix=1_000)
    assert f[0].level == "OK" and "no delegate" in f[0].detail


def test_doctor_live_command_with_a_fake_clock(run_cli: Callable[..., int], tmp_cfg: tuple[Path, Path],
                                               monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path, data = tmp_cfg
    now = 2_000_000_000
    import time as time_mod

    monkeypatch.setattr(time_mod, "time", lambda: float(now))           # cli reads the wall clock via time.time
    sink = MemorySink()
    monkeypatch.setattr(cli, "_alerts", lambda *, toast: AlertManager(sinks=[sink], clock=lambda: float(now)))
    _arm_state(data, now - 80 * 3600, now - (72 * 3600 + 60))
    argv = ("doctor", "--live", "--config", str(cfg_path), "--set", f"live.delegate_wallets={DELEGATES}")
    assert run_cli(*argv) == cli.EXIT_FAIL
    out = capsys.readouterr().out
    assert "remove Staking proxies" in out and "ops-a" in out
    assert [a.kind for a in sink.alerts] == ["idle_proxy"]
    _arm_state(data, now - 10 * 3600, now - 3600)
    assert run_cli(*argv) == cli.EXIT_OK


def test_doctor_reports_a_stale_heartbeat_of_a_running_instance(tmp_cfg: tuple[Path, Path],
                                                                no_secret_env: None) -> None:
    cfg_path, data = tmp_cfg
    cfg = cli.load_config([config_load.DEFAULT_CONFIG, cfg_path], env={})
    rd = data / "runs" / "paper-main"
    rd.mkdir(parents=True)
    (rd / "status.json").write_text(json.dumps({"schema": 1, "run_id": "paper-main", "ts_unix": 1_000.0, "block": 7,
                                                "books": {"b": {"breaches": [], "orphans": 0}}}), encoding="utf-8")
    with InstanceLock(lock_path(data, "paper-main", "paper"), mode="paper", run_id="paper-main"):
        f = {x.check: x for x in cli.doctor_checks(cfg, now_unix=2_000.0, stale_s=300)}
        assert f["run paper-main"].level == "FAIL" and "STALE" in f["run paper-main"].detail
    f = {x.check: x for x in cli.doctor_checks(cfg, now_unix=2_000.0, stale_s=300)}
    assert f["run paper-main"].level == "OK"                                 # not running: an old heartbeat is fine


# ================================================================================================= live gate
def test_live_refuses_without_the_cli_and_config_locks(run_cli: Callable[..., int], no_network: list[str],
                                                       tmp_cfg: tuple[Path, Path],
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    cfg, _ = tmp_cfg
    assert run_cli("live", "--config", str(cfg)) == cli.EXIT_GATE                       # no --live, enabled=false
    assert "lock 1" in capsys.readouterr().err
    assert run_cli("live", "--submit", "--config", str(cfg), "--set", "live.enabled=true") == cli.EXIT_GATE
    assert "--submit needs --live" in capsys.readouterr().err
    assert run_cli("live", "--live", "--config", str(cfg)) == cli.EXIT_GATE             # lock 1
    assert run_cli("live", "--config", str(cfg), "--set", "live.enabled=true") == cli.EXIT_GATE   # lock 3
    assert "lock 3" in capsys.readouterr().err
    assert no_network == []


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows refusal")
def test_live_never_runs_on_windows(run_cli: Callable[..., int], no_network: list[str], tmp_cfg: tuple[Path, Path],
                                    capsys: pytest.CaptureFixture[str]) -> None:
    cfg, _ = tmp_cfg
    assert run_cli("live", "--live", "--submit", "--config", str(cfg), "--set", "live.enabled=true") == cli.EXIT_GATE
    assert "Linux/WSL only" in capsys.readouterr().err
    assert no_network == []


def test_cli_never_imports_taotrader_live_statically() -> None:
    tree = ast.parse((ROOT / "src" / "taotrader" / "cli.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            assert not mod.startswith((".live", "taotrader.live")), mod
            assert not (node.level == 1 and node.module is None and any(a.name == "live" for a in node.names))
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith(("taotrader.live", "bittensor")) for a in node.names)


def test_taotrader_live_is_not_imported_before_the_gate(tmp_cfg: tuple[Path, Path], tmp_path: Path) -> None:
    """In a fresh process: refused live starts, doctor and operator commands never import taotrader.live."""
    cfg, _ = tmp_cfg
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from taotrader import cli
        logs = {str(tmp_path / 'logs3')!r}
        cfg = {str(cfg)!r}
        codes = [cli.main(["live", "--config", cfg, "--log-dir", logs, "--quiet"]),
                 cli.main(["live", "--live", "--config", cfg, "--log-dir", logs, "--quiet"]),
                 cli.main(["doctor", "--config", cfg, "--log-dir", logs, "--quiet"]),
                 cli.main(["halt", "--config", cfg, "--log-dir", logs, "--quiet"])]
        print(codes, sorted(m for m in sys.modules if m.startswith("taotrader.live") or m.startswith("bittensor")))
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "[3, 3, 0, 0] []"


def test_live_arm_prints_a_verifiable_token_that_is_never_logged(kr: Any, run_cli: Callable[..., int],
                                                                  tmp_cfg: tuple[Path, Path], tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    from taotrader.live import gate

    cfg_path, data = tmp_cfg
    assert run_cli("live", "arm", "--config", str(cfg_path)) == cli.EXIT_USAGE             # no secret yet
    capsys.readouterr()
    assert run_cli("live", "arm", "--init-secret", "--ttl-hours", "6", "--config", str(cfg_path)) == cli.EXIT_OK
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith(f"export {gate.ARM_ENV}="))
    token = line.split("=", 1)[1].strip("'")
    secret = gate.load_arm_secret()
    cfg = cli.load_config([config_load.DEFAULT_CONFIG, cfg_path], env={})
    status, expiry = gate.check_arm_token(token, secret, config_load.config_hash(cfg), int(time.time()))
    assert status is gate.TokenStatus.VALID and expiry is not None
    assert 5 * 3600 < expiry - time.time() <= 6 * 3600
    other = cli.load_config([config_load.DEFAULT_CONFIG, cfg_path], ["rpc.rate_per_s=2.0"], env={})
    assert gate.check_arm_token(token, secret, config_load.config_hash(other), int(time.time()))[0] \
        is not gate.TokenStatus.VALID                                                     # any config edit invalidates
    st = json.loads((data / "live" / "arm_state.json").read_text(encoding="utf-8"))
    assert st["expiry_unix"] == expiry
    logs = _logs(tmp_path)
    assert "token not logged" in logs
    assert token.split(":")[1] not in logs and secret.decode() not in logs
    assert run_cli("live", "arm", "--init-secret", "--config", str(cfg_path)) == cli.EXIT_USAGE   # never overwritten
    assert run_cli("live", "arm", "--ttl-hours", "25", "--config", str(cfg_path)) == cli.EXIT_USAGE
    assert cli.doctor_live(_live_cfg(cfg_path), now_unix=int(time.time()))[0].level == "OK"


# ================================================================================================= secrets in logs
def test_secrets_never_reach_the_cli_log_or_stderr(run_cli: Callable[..., int], monkeypatch: pytest.MonkeyPatch,
                                                   tmp_cfg: tuple[Path, Path], tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    hook = "https://hc-ping.example/5f0c-SECRET-PING-0001"
    key = "tsk_live_TAOSTATS-KEY-0123456789"
    monkeypatch.setenv("TAOTRADER_HEALTHCHECK_URL", hook)
    monkeypatch.setenv("TAOSTATS_API_KEY", key)
    cfg, _ = tmp_cfg
    real = cli.doctor_checks

    def leaky(*a: Any, **k: Any) -> Any:
        real(*a, **k)                                       # loads every secret through ops.secrets
        cli.log.warning("probe %s with key %s", hook, key)
        cli.log.error("context", extra={"url": hook, "nested": {"k": key}})
        raise RuntimeError(f"connection to {hook} failed, Authorization: {key}, "
                           "https://x.example/rpc?apikey=never-loaded-123")
    monkeypatch.setattr(cli, "doctor_checks", leaky)
    assert run_cli("doctor", "--config", str(cfg)) == cli.EXIT_FAIL
    err = capsys.readouterr().err
    logs = _logs(tmp_path)
    assert "RuntimeError" in logs and "probe" in logs
    for text in (logs, err):
        assert hook not in text and key not in text and "never-loaded-123" not in text
    monkeypatch.setattr(cli, "doctor_checks", real)
    assert run_cli("doctor", "--config", str(cfg)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "healthcheck_url=set" in out and "taostats=set" in out and hook not in out and key not in out


# ================================================================================================= journals and runs
def test_verify_journal_and_run_report(run_cli: Callable[..., int], tmp_cfg: tuple[Path, Path],
                                       capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from taotrader.core.events import OperatorCommand
    from taotrader.core.units import BookId, LogicalTime, Phase
    from taotrader.data.journal import SqliteJournal

    monkeypatch.chdir(tmp_path)
    cfg, data = tmp_cfg
    assert run_cli("verify-journal", "--config", str(cfg), "--run", "paper-main") == cli.EXIT_FAIL   # missing
    jp = data / "runs" / "paper-main" / "journal.sqlite"
    jp.parent.mkdir(parents=True)
    j = SqliteJournal(jp)
    try:
        j.append_batch([(LogicalTime(Block(5), Phase.INGEST, 0), BookId(""),
                         OperatorCommand(block=Block(5), command="halt", reason="t",
                                                                                     nonce="n1"))])
    finally:
        j.close()
    capsys.readouterr()
    assert run_cli("verify-journal", "--config", str(cfg), "--all") == cli.EXIT_OK
    assert "OK 1 records" in capsys.readouterr().out
    assert run_cli("report", "--run", "paper-main", "--config", str(BOOKS_PAPER), "--config", str(cfg)) == cli.EXIT_OK
    doc = json.loads((tmp_path / "reports" / "output" / "paper-main" / "summary.json").read_text(encoding="utf-8"))
    assert doc["records"] == 1 and doc["kinds"] == {"operator_command": 1}
    assert (tmp_path / "reports" / "output" / "paper-main" / "summary.html").is_file()


# ================================================================================================= research commands
@pytest.fixture(scope="module")
def lake_copy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private copy of the committed mini-lake (Lake opens its state db read-write)."""
    d = tmp_path_factory.mktemp("minilake")
    shutil.copytree(MINILAKE / "lake", d / "lake")
    shutil.copy2(MINILAKE / "state.sqlite", d / "state.sqlite")
    yield d / "lake"


TINY = ("--start", "8765400", "--end", "8767200", "--warmup-blocks", "0", "--set", "backtest.feature_warm_blocks=7200")


def test_backtest_command_end_to_end_on_the_minilake(run_cli: Callable[..., int], lake_copy: Path, tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "bt"
    rc = run_cli("backtest", "--lake", str(lake_copy), "--books", "base-cash,base-ew-total", *TINY, "--out", str(out),
                 "--journal", str(tmp_path / "bt.sqlite"), "--report")
    assert rc == cli.EXIT_OK, capsys.readouterr().err
    doc = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert set(doc["books"]) == {"base-cash", "base-ew-total"}
    assert doc["ticks"] > 0 and doc["trial_count"] >= 2
    assert all(b["orphans"] == 0 and b["breaches"] == [] for b in doc["books"].values())
    assert (out / "trials.sqlite").is_file() and (out / "index.html").is_file()
    # the persisted pass journal verifies, and a second run over it resumes to the same digests
    rc = run_cli("backtest", "--lake", str(lake_copy), "--books", "base-cash,base-ew-total", *TINY, "--out", str(out),
                 "--journal", str(tmp_path / "bt.sqlite"))
    assert rc == cli.EXIT_OK
    doc2 = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert {k: v["digest"] for k, v in doc2["books"].items()} == {k: v["digest"] for k, v in doc["books"].items()}
    assert run_cli("verify-journal", "--journal", str(tmp_path / "bt.sqlite")) == cli.EXIT_OK


def test_report_and_study_commands_on_the_minilake(run_cli: Callable[..., int], lake_copy: Path, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "rep"
    rc = run_cli("report", "--lake", str(lake_copy), "--books", "base-cash", *TINY, "--with-studies", "s0",
                 "--out", str(out))
    assert rc == cli.EXIT_OK, capsys.readouterr().err
    assert (out / "index.html").is_file()
    sout = tmp_path / "s0"
    rc = run_cli("study", "s0", "--lake", str(lake_copy), "--books", "base-cash,base-ew-total", *TINY, "--out", str(sout))
    assert rc == cli.EXIT_OK, capsys.readouterr().err
    assert (sout / "index.html").is_file()


def test_verify_lake_on_a_copy(run_cli: Callable[..., int], lake_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("verify-lake", "--lake", str(lake_copy)) == cli.EXIT_OK
    assert "0 problem(s)" in capsys.readouterr().out


def test_clear_quarantine_needs_a_stopped_run_and_appends_one_record(run_cli: Callable[..., int],
                                                                     tmp_cfg: tuple[Path, Path],
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    from taotrader.core.events import OperatorCommand, QuarantineCleared
    from taotrader.core.units import BookId, LogicalTime, Phase
    from taotrader.data.journal import SqliteJournal, decode_record

    cfg, data = tmp_cfg
    base = ("clear-quarantine", "--config", str(BOOKS_PAPER), "--config", str(cfg))
    assert run_cli(*base, "--book", "paper-carry", "--reason", "x") == cli.EXIT_USAGE          # no journal
    jp = data / "runs" / "paper-main" / "journal.sqlite"
    jp.parent.mkdir(parents=True)
    j = SqliteJournal(jp)
    try:
        j.append_batch([(LogicalTime(Block(9), Phase.INGEST, 0), BookId(""), OperatorCommand(block=Block(9), command="halt",
                                                                                     reason="t", nonce="n1"))])
    finally:
        j.close()
    assert run_cli(*base, "--book", "nope", "--reason", "x") == cli.EXIT_USAGE
    assert run_cli(*base, "--book", "paper-carry", "--reason", "  ") == cli.EXIT_USAGE
    with InstanceLock(lock_path(data, "paper-main", "live"), mode="live", run_id="paper-main"):
        assert run_cli(*base, "--book", "paper-carry", "--reason", "reconciled") == cli.EXIT_LOCKED
    capsys.readouterr()
    assert run_cli(*base, "--book", "paper-carry", "--reason", "reconciled by hand") == cli.EXIT_OK
    j = SqliteJournal(jp, readonly=True)
    try:
        assert j.verify_chain() == 2
        recs = list(j.read(1))
    finally:
        j.close()
    ev = decode_record(recs[-1])
    assert isinstance(ev, QuarantineCleared) and str(recs[-1].book) == "paper-carry" and int(ev.block) == 9
    assert "reconciled by hand" in ev.reason


# ================================================================================================= the run loop, offline
def test_run_loop_heartbeat_and_journal_on_recorded_blocks(lake_copy: Path, tmp_path: Path,
                                                                  no_secret_env: None) -> None:
    """The paper/live loop (recover, tick, heartbeat after every tick, alert watch) driven by recorded mini-lake
    snapshots instead of the live feed: status.json follows the journal head, breaches/orphans stay empty, and a
    restart resumes after the last journaled block."""
    import asyncio

    from taotrader.backtest.books import calibration_provider, feature_engine, load_backtest_plan, wire_books
    from taotrader.data.journal import SqliteJournal
    from taotrader.data.lake import Lake
    from taotrader.data.replay import ParquetReplay
    from taotrader.engine.runner import Runner
    from taotrader.ops.health import HealthWriter, expected_head_from, read_status

    plan = load_backtest_plan(env={}, cli=[f'backtest.lake="{lake_copy.as_posix()}"', "backtest.warmup_blocks=0",
                                           "backtest.feature_warm_blocks=7200"])
    run = plan.subset(["base-cash", "base-ew-total"]).run
    sink = MemorySink()
    am = AlertManager(sinks=[sink], clock=lambda: 0.0)
    status_path = tmp_path / "status.json"

    def one(max_ticks: int | None, stop_first: bool = False) -> cli.LoopResult:
        lake = Lake(lake_copy)
        journal = SqliteJournal(tmp_path / "journal.sqlite")
        try:
            cal = calibration_provider(lake)
            src = ParquetReplay(lake, 8_765_400, 8_766_600, 60, warmup_blocks=0)
            runner = Runner(run_id=run.run_id, mode=run.mode, source=src, journal=journal,
                            features=feature_engine(cal, plan), books=wire_books(run, cal),
                            features_factory=lambda: feature_engine(cal, plan),
                            expected_head=expected_head_from(read_status(status_path), run_id=run.run_id))
            stop = asyncio.Event()
            if stop_first:
                stop.set()
            try:
                return asyncio.run(cli.run_loop(runner, src, run_id=run.run_id, mode=run.mode.value, journal=journal,
                                                recorder=None, health_writer=HealthWriter(status_path, min_interval_s=0),
                                                alerts=am, stop=stop, max_ticks=max_ticks))
            finally:
                runner.close()
        finally:
            journal.close()
            lake.close()

    r1 = one(8)
    assert r1.ticks == 8 and r1.stopped_by == "max_ticks"
    assert all(v == () for v in r1.breaches.values()) and all(v == 0 for v in r1.orphans.values())
    st = read_status(status_path)
    assert st is not None and st["ticks"] == 8 and st["block"] == r1.last_block and st["note"] == "stopped (max_ticks)"
    assert set(st["books"]) == {"base-cash", "base-ew-total"}
    assert st["books"]["base-cash"]["nav_rao"] == st["books"]["base-cash"]["cash_rao"] + \
        st["books"]["base-cash"]["fee_float_rao"]
    j = SqliteJournal(tmp_path / "journal.sqlite", readonly=True)
    try:
        assert expected_head_from(st, run_id=run.run_id) == j.head()          # the heartbeat carries the journal head
    finally:
        j.close()
    r2 = one(None)                                                            # restart: resume to the end of the window
    assert r2.stopped_by == "feed_end" and r2.ticks > 0 and r2.last_block == 8_766_600
    assert read_status(status_path)["ticks"] == r2.ticks                      # type: ignore[index]
    r3 = one(None, stop_first=True)                                           # an already-set stop ends at once
    assert r3.ticks == 0 and r3.stopped_by == "stop"
    assert read_status(status_path)["note"] == "stopped (stop)"               # type: ignore[index]


def test_watch_tick_alerts_mode_changes_fee_float_daily_summary_and_stalls() -> None:
    from types import SimpleNamespace as NS  # noqa: N814

    from taotrader.ops.health import BookStatus

    sink = MemorySink()
    am = AlertManager(sinks=[sink], clock=lambda: 0.0)
    state = NS(mode=NS(name="NORMAL"), portfolio=NS(fee_float=10, positions=()), funded=True)
    rt = NS(book="b1", state=state, engine=NS(cfg=NS(risk=NS(fee_float_alert_rao=5))))
    runner = NS(books=[rt])
    w = cli._Watch(modes={"b1": "NORMAL"}, prune_alerted=set())
    st = {"b1": BookStatus(mode="NORMAL", halted=False, exits_only=False, open_orders=0, nav_rao=10**9, cash_rao=10**9,
                           fee_float_rao=10, positions=0, orphans=0)}
    feed = NS(stalled=False)
    cli._watch_tick(runner, NS(block=7_199), am, w, st, feed)                 # type: ignore[arg-type]
    assert sink.alerts == []
    state.mode = NS(name="CAUTION")
    state.portfolio = NS(fee_float=4, positions=())
    feed.stalled = True
    cli._watch_tick(runner, NS(block=7_200), am, w, st, feed)                 # type: ignore[arg-type]
    kinds = sorted(a.kind for a in sink.alerts)
    assert kinds == ["daily_summary", "fee_float", "mode", "stall"]
    assert any("NORMAL -> CAUTION" in a.message for a in sink.alerts)
    sink.alerts.clear()
    cli._watch_tick(runner, NS(block=7_201), am, w, st, feed)                 # type: ignore[arg-type]
    assert [a.kind for a in sink.alerts] == []                                # still stalled: no repeat; fee float deduped
