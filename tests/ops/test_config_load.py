"""ops.config_load: strict TOML -> core.config, precedence, the cross-field rule, config_hash, preregistration."""
from __future__ import annotations

from pathlib import Path

import pytest

from taotrader.core.config import BookCfg, ExecCfg, LiveCfg, RiskCfg, RpcCfg, RunCfg, SleeveCfg
from taotrader.core.units import RunMode, Stage
from taotrader.ops import config_load as cl
from taotrader.ops.config_load import ConfigError

BOOKS = """
run_id = "paper-main"
mode = "paper"

[book_defaults.risk]
margin_a_blocks = 600

[[books]]
book = "paper-carry"
capital_rao = 100_000_000_000
fee_float_rao = 500_000_000
[books.exec]
shield_miss_ppm = 20_000
[[books.sleeves]]
strategy = "carry"
stage = "PAPER"
budget_ppm = 400_000
[books.sleeves.params]
h_eval_days = 5
mu_in_ppm_day = 1_000
[[books.sleeves]]
strategy = "baseline.ew_total"
stage = 0
budget_ppm = 100_000

[[books]]
book = "paper-mom"
capital_rao = 50_000_000_000
fee_float_rao = 500_000_000
dereg_model = "fixed:350000"
sleeves = []
[books.risk]
margin_a_blocks = 1_200
[books.exec]
impact_half_life_blocks = "none"
"""


@pytest.fixture
def books_file(tmp_path: Path) -> Path:
    p = tmp_path / "books.toml"
    p.write_text(BOOKS, encoding="utf-8")
    return p


def write(tmp_path: Path, text: str, name: str = "x.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def load(*paths: Path, env: dict[str, str] | None = None, cli: tuple[str, ...] = ()) -> RunCfg:
    return cl.load_run_config((cl.DEFAULT_CONFIG, *paths), env=env or {}, cli=cli)


# ------------------------------------------------------------------------------------------------- defaults
def test_default_toml_equals_core_defaults() -> None:
    cfg = cl.load_run_config(env={})
    assert cfg.run_id == "default" and cfg.mode is RunMode.BACKTEST and cfg.books == ()
    assert cfg.live == LiveCfg()
    assert cfg.rpc.head_endpoints[0] == "wss://entrypoint-finney.opentensor.ai:443"
    assert cfg.rpc.archive_endpoints[0] == "https://bittensor-finney.api.onfinality.io/public"
    assert (cfg.rpc.rate_per_s, cfg.rpc.burst, cfg.rpc.max_concurrency, cfg.rpc.keys_per_call) == (3.0, 3, 3, 2_000)
    raw = cl.read_toml(cl.DEFAULT_CONFIG)
    assert raw["book_defaults"]["risk"] == cl.run_config_value(RiskCfg())
    assert raw["book_defaults"]["exec"] == cl.run_config_value(ExecCfg())
    assert raw["live"] == cl.run_config_value(LiveCfg())


def test_books_build_with_defaults_and_overrides(books_file: Path) -> None:
    cfg = load(books_file)
    assert cfg.run_id == "paper-main" and cfg.mode is RunMode.PAPER
    carry, mom = cfg.books
    assert carry.risk == RiskCfg(margin_a_blocks=600)                     # book_defaults applied
    assert carry.exec == ExecCfg(shield_miss_ppm=20_000)
    assert carry.sleeves[0] == SleeveCfg("carry", Stage.PAPER, 400_000, {"h_eval_days": 5, "mu_in_ppm_day": 1_000})  # type: ignore[arg-type]
    assert carry.sleeves[1].stage is Stage.RESEARCH                        # by value
    assert mom.risk.margin_a_blocks == 1_200 and mom.dereg_model == "fixed:350000"
    assert mom.exec.impact_half_life_blocks is None                       # "none" -> None
    with pytest.raises(TypeError):
        carry.sleeves[0].params["h_eval_days"] = 6                         # type: ignore[index]  # read-only


def test_round_trip_through_toml(books_file: Path, tmp_path: Path) -> None:
    cfg = load(books_file, cli=("live.sleeves=[\"carry\"]", "live.accepted_specs=[475]"))
    text = cl.dump_run_config(cfg)
    again = cl.load_run_config((write(tmp_path, text, "dumped.toml"),), env={})
    assert again == cfg
    assert cl.config_hash(again) == cl.config_hash(cfg)


# ------------------------------------------------------------------------------------------------- precedence
def test_precedence_defaults_file_env_cli(books_file: Path, tmp_path: Path) -> None:
    second = write(tmp_path, "seed = 3\n[rpc]\nrate_per_s = 2.0\n", "second.toml")
    assert load(books_file).seed == 0                                       # default
    assert load(books_file, second).seed == 3                               # file
    assert load(books_file, second).rpc.burst == 3                          # tables merge key by key
    env = {"TAOTRADER_CFG_SEED": "5", "TAOTRADER_CFG_RPC__RATE_PER_S": "1.5",
           "TAOTRADER_CFG_BOOKS__PAPER-CARRY__RISK__MARGIN_A_BLOCKS": "900",
           "TAOTRADER_CFG_BOOKS__PAPER-CARRY__SLEEVES__BASELINE.EW_TOTAL__BUDGET_PPM": "50000",
           "TAOTRADER_LIVE_ARMED": "123:abc", "TAOTRADER_LIVE_NETWORK_CONFIRM": "finney"}   # not config: ignored
    cfg = load(books_file, second, env=env)
    assert (cfg.seed, cfg.rpc.rate_per_s) == (5, 1.5)
    assert cfg.books[0].risk.margin_a_blocks == 900 and cfg.books[0].sleeves[1].budget_ppm == 50_000
    cfg = load(books_file, second, env=env, cli=("seed=9", "books.paper-carry.risk.margin_a_blocks=60",
                                                 "books.paper-carry.sleeves.carry.params.h_eval_days=7"))
    assert cfg.seed == 9 and cfg.books[0].risk.margin_a_blocks == 60
    assert cfg.books[0].sleeves[0].params["h_eval_days"] == 7


def test_env_names_are_case_insensitive(books_file: Path) -> None:
    cfg = load(books_file, env={"taotrader_cfg_live__enabled": "true", "TAOTRADER_CFG_MODE": "backtest"})
    assert cfg.live.enabled is True and cfg.mode is RunMode.BACKTEST


@pytest.mark.parametrize(("env", "cli"), [
    ({"TAOTRADER_CFG_RPC__NO_SUCH_KEY": "1"}, ()),
    ({"TAOTRADER_CFG_BOOKS__MISSING__SEED": "1"}, ()),
    ({"TAOTRADER_CFG_": "1"}, ()),
    ({}, ("no_equals_sign",)),
    ({}, ("books.paper-carry.sleeves.nope.budget_ppm=1",)),
    ({}, ("rpc.rate_per_s.deeper=1",)),
])
def test_bad_overrides_rejected(books_file: Path, env: dict[str, str], cli: tuple[str, ...]) -> None:
    with pytest.raises(ConfigError):
        load(books_file, env=env, cli=cli)


# ------------------------------------------------------------------------------------------------- strictness
@pytest.mark.parametrize("snippet", [
    "unknown_top = 1",
    "[rpc]\nrate_per_sec = 2.0",
    "[live]\nenabeld = true",
    "[book_defaults.risk]\nmargin_a = 300",
    "[book_defaults]\nsleeves = []",
    "[[books]]\nbook = 'b'\ncapital_rao = 1\nfee_float_rao = 1\ncapital_tao = 1",
    "[[books]]\nbook = 'b'\ncapital_rao = 1\nfee_float_rao = 1\n[[books.sleeves]]\nstrategy = 'carry'\nstage = 'PAPER'\n"
    "budget_ppm = 1\nweight = 2",
])
def test_unknown_keys_rejected(tmp_path: Path, snippet: str) -> None:
    with pytest.raises(ConfigError, match="unknown"):
        load(write(tmp_path, snippet))


@pytest.mark.parametrize("snippet", [
    "seed = 1.0",                                   # float for int
    "seed = true",                                  # bool for int
    "seed = '1'",                                   # string for int
    "seed = -1",                                    # negative
    "[rpc]\nrate_per_s = 'fast'",
    "[rpc]\nrate_per_s = nan",
    "[live]\nenabled = 1",
    "mode = 'warp'",
    "[book_defaults.risk]\nd_stress_ppm = 0.5",     # units are in the names: ppm are integers
    "[[books]]\nbook = 'b'\ncapital_rao = 1\nfee_float_rao = 1\n[[books.sleeves]]\nstrategy = 'carry'\nstage = 'GOLD'\n"
    "budget_ppm = 1",
    "[[books]]\nbook = 'b'\nfee_float_rao = 1",     # capital_rao is required
])
def test_wrong_types_and_missing_required_rejected(tmp_path: Path, snippet: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, snippet))


def test_invalid_toml_reports_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(write(tmp_path, "seed = = 1"))
    with pytest.raises(ConfigError, match="not found"):
        cl.load_run_config((tmp_path / "missing.toml",), env={})


# ------------------------------------------------------------------------------------------------- cross-field rules
def test_cross_field_rule_rejects_mismatched_unwind_exec_blocks(books_file: Path) -> None:
    with pytest.raises(ConfigError, match="unwind_exec_blocks"):
        load(books_file, cli=("books.paper-carry.risk.unwind_exec_blocks=6",))
    with pytest.raises(ConfigError, match="unwind_exec_blocks"):
        load(books_file, cli=("book_defaults.exec.finality_lag_blocks=10",))
    cfg = load(books_file, cli=("book_defaults.exec.finality_lag_blocks=10", "book_defaults.risk.unwind_exec_blocks=12"))
    assert all(b.risk.unwind_exec_blocks == 12 for b in cfg.books)
    direct = RunCfg("r", RunMode.BACKTEST, (BookCfg("b", 1, 1, (), risk=RiskCfg(unwind_exec_blocks=4)),),  # type: ignore[arg-type]
                    RpcCfg(("wss://x",), ("https://y",)))
    with pytest.raises(ConfigError, match="unwind_exec_blocks"):
        cl.validate_run_config(direct)


@pytest.mark.parametrize("cli", [
    ("run_id=bad id",), ("run_id=a|b",), ("cadence_blocks=0",), ("live.mode=yolo",), ("live.network=mainnet",),
    ("rpc.head_endpoints=[\"https://not-ws\"]",), ("rpc.archive_endpoints=[\"wss://not-http\"]",),
    ("rpc.archive_endpoints=[\"https://x.onfinality.io/rpc?apikey=SECRET123\"]",),
    ("rpc.head_endpoints=[\"wss://user:pw@host:443\"]",), ("rpc.burst=0",), ("rpc.rate_per_s=0",),
    ("books.paper-carry.dereg_model=fixed:2000000",), ("books.paper-carry.dereg_model=half",),
    ("books.paper-carry.exec.n_delegates=0",), ("books.paper-carry.sleeves.carry.budget_ppm=950000",),
    ("books.paper-mom.book=paper-carry",),
])
def test_cross_field_validation(books_file: Path, cli: tuple[str, ...]) -> None:
    with pytest.raises(ConfigError):
        load(books_file, cli=cli)


def test_errors_never_echo_credentials(books_file: Path) -> None:
    with pytest.raises(ConfigError) as ei:
        load(books_file, cli=("rpc.archive_endpoints=[\"https://x.onfinality.io/rpc?apikey=SECRET123\"]",))
    assert "SECRET123" not in str(ei.value)


def test_duplicate_sleeve_rejected(tmp_path: Path) -> None:
    text = ("[[books]]\nbook = 'b'\ncapital_rao = 1\nfee_float_rao = 1\n"
            + "[[books.sleeves]]\nstrategy = 'carry'\nstage = 'PAPER'\nbudget_ppm = 1\n" * 2)
    with pytest.raises(ConfigError, match="duplicate sleeve"):
        load(write(tmp_path, text))


# ------------------------------------------------------------------------------------------------- hashes
def test_config_hash_stable_and_edit_sensitive(books_file: Path, tmp_path: Path) -> None:
    a, b = load(books_file), load(books_file)
    assert cl.config_hash(a) == cl.config_hash(b) and len(cl.config_hash(a)) == 64
    reformatted = write(tmp_path, BOOKS.replace("100_000_000_000", "100000000000") + "\n# comment\n", "fmt.toml")
    assert cl.config_hash(load(reformatted)) == cl.config_hash(a)          # formatting does not matter
    for edit in ("seed=1", "books.paper-carry.risk.margin_a_blocks=301", "live.max_fee_tao=0.006",
                 "books.paper-carry.sleeves.carry.params.h_eval_days=6"):
        assert cl.config_hash(load(books_file, cli=(edit,))) != cl.config_hash(a), edit


def test_preregistration_freezes_the_core_defaults() -> None:
    prereg = cl.load_preregistration()
    assert cl.check_preregistration(prereg) == []
    h = cl.prereg_hash()
    assert h == cl.prereg_hash() and len(h) == 64
    for section in ("carry", "momentum", "mean_reversion_study", "lcw", "gatekeeper", "prune", "allocator", "modes",
                    "falsification"):
        assert section in prereg, section


def test_preregistration_check_detects_drift() -> None:
    prereg = cl.load_preregistration()
    prereg["risk"] = {**prereg["risk"], "margin_a_blocks": 301}
    prereg["exec"] = {k: v for k, v in prereg["exec"].items() if k != "n_delegates"}
    out = cl.check_preregistration(prereg)
    assert any("margin_a_blocks" in o for o in out) and any("n_delegates" in o for o in out)


def test_prereg_hash_ignores_line_endings(tmp_path: Path) -> None:
    text = cl.PREREGISTRATION.read_text(encoding="utf-8")
    crlf = tmp_path / "crlf.toml"
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert cl.prereg_hash(crlf) == cl.prereg_hash()
