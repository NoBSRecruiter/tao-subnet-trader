"""config/live.example.toml and requirements-live.txt (DESIGN.md 9.2, 11 WP11)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from taotrader.live.gate import explicit_live_keys
from taotrader.ops import config_load

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "live.example.toml"
REQS = ROOT / "requirements-live.txt"
WHEEL_SHA = "4651d9125cd29ecfda1eed0ef758fe9e29563dd81ecd9d42a9f1c9ec6603cbaa"
SDIST_SHA = "8ce05029a712866048c6cf3e3d5df1592ac896175f5b4227f63044b7e7b7edb3"


def test_live_example_is_safe_by_default_and_loads() -> None:
    cfg = config_load.load_run_config((config_load.DEFAULT_CONFIG, EXAMPLE), env={})
    lv = cfg.live
    assert lv.enabled is False and lv.mode == "plan_only" and lv.risk_exits_when_unarmed is False
    assert lv.network == "test" and lv.real_coldkey_ss58 == "" and lv.delegate_wallets == () and lv.accepted_specs == ()
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(raw) == {"live"}
    assert {"enabled", "mode", "network", "risk_exits_when_unarmed"} <= set(raw["live"])
    assert "network" in explicit_live_keys([config_load.DEFAULT_CONFIG, EXAMPLE])


def test_risk_exits_when_unarmed_is_explicit_and_commented() -> None:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    idx = [i for i, ln in enumerate(lines) if re.match(r"^\s*risk_exits_when_unarmed\s*=\s*false\s*$", ln)]
    assert len(idx) == 1, "an explicit, uncommented `risk_exits_when_unarmed = false` line"
    above = "\n".join(lines[max(0, idx[0] - 12):idx[0]])
    assert above.count("#") >= 5 and "decide" in above and "EMERGENCY" in above and "V2" in above


def test_live_example_holds_no_secrets() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    body = "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())
    assert not re.search(r"(api[_-]?key|token|secret|password|passwd|mnemonic|seed)\s*=", body, re.IGNORECASE)
    assert not re.search(r"\b0x[0-9a-fA-F]{40,}\b", text), "no raw keys or hashes"
    assert not re.search(r"\b5[1-9A-HJ-NP-Za-km-z]{47}\b", text), "no SS58 addresses"
    assert "://" not in body


def test_requirements_live_pins_bittensor_with_the_wheel_hash() -> None:
    text = REQS.read_text(encoding="utf-8")
    logical = re.sub(r"\\\n\s*", " ", text)
    reqs = [" ".join(ln.split()) for ln in logical.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert reqs == [f"bittensor==11.3.0 --hash=sha256:{WHEEL_SHA}"]
    assert f"--hash=sha256:{SDIST_SHA}" not in text
    assert "--require-hashes" in text and "bittensor-cli" in text
