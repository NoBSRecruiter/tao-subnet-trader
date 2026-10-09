"""ops.logging: JSON lines, and secrets never reach a log line (DESIGN.md 11 WP12 acceptance: "Secrets never appear in
logs, including through the redaction filter")."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from taotrader.ops import secrets as sec
from taotrader.ops.logging import HANDLER_TAG, JsonFormatter, redact_line, setup_logging, teardown_logging

WEBHOOK = "https://discord.com/api/webhooks/123/SuperSecretWebhookToken987"
TAOSTATS = "tsk_live_abcdef0123456789"
ONFINALITY = "https://bittensor-finney.api.onfinality.io/rpc?apikey=0f1e2d3c4b5a69788796"


@pytest.fixture
def loaded(kr: object, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("TAOTRADER_ALERT_WEBHOOK", WEBHOOK)
    monkeypatch.setenv("TAOSTATS_API_KEY", TAOSTATS)
    monkeypatch.setenv("TAOTRADER_ONFINALITY_URL", ONFINALITY)
    for name in ("alert_webhook", "taostats", "onfinality"):
        assert sec.get_secret(name, use_keyring=False, argv=[]) is not None
    return [WEBHOOK, TAOSTATS, ONFINALITY]


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_json_lines_have_the_documented_fields(tmp_path: Path) -> None:
    path = setup_logging(log_dir=tmp_path, name="t1", console=False)
    try:
        logging.getLogger("taotrader.test").info("hello %s", "world", extra={"block": 9_240_388})
    finally:
        teardown_logging()
    assert path is not None
    (rec,) = _lines(path)
    assert rec["msg"] == "hello world" and rec["level"] == "INFO" and rec["logger"] == "taotrader.test"
    assert rec["extra"] == {"block": 9_240_388}
    assert str(rec["ts"]).endswith("Z") and isinstance(rec["pid"], int)


def test_loaded_secrets_never_appear_in_the_log_file_or_console(tmp_path: Path, loaded: list[str]) -> None:
    console = io.StringIO()
    path = setup_logging(log_dir=tmp_path, name="t2", stream=console)
    lg = logging.getLogger("taotrader.leaky")
    try:
        lg.warning(f"f-string leak {WEBHOOK}")                       # in the message itself
        lg.warning("args leak %s and %r", TAOSTATS, ONFINALITY)       # in the args
        lg.info("extra leak", extra={"url": WEBHOOK, "nested": {"k": [TAOSTATS]}})
        try:
            raise RuntimeError(f"request to {ONFINALITY} failed with key {TAOSTATS}")
        except RuntimeError:
            lg.exception("exception leak")                           # in the traceback text
        lg.error("dict leak %s", {"Authorization": TAOSTATS})
    finally:
        teardown_logging()
    assert path is not None
    text = path.read_text(encoding="utf-8") + console.getvalue()
    for s in loaded:
        assert s not in text
    assert "0f1e2d3c4b5a69788796" not in text and "SuperSecretWebhookToken987" not in text
    assert text.count("***") >= 6
    assert len(_lines(path)) == 5


def test_unloaded_credentials_in_urls_are_masked_too() -> None:
    line = redact_line("GET https://user:hunter2pass@example.org/x?apikey=ABCDEF123456&b=2 token=zzz999 password: p4ss")
    assert "hunter2pass" not in line and "ABCDEF123456" not in line and "zzz999" not in line and "p4ss" not in line
    assert "&b=2" in line and "example.org" in line


def test_every_handler_carries_the_redacting_filter(tmp_path: Path) -> None:
    setup_logging(log_dir=tmp_path, name="t3")
    try:
        mine = [h for h in logging.getLogger().handlers if getattr(h, HANDLER_TAG, False)]
        assert len(mine) == 2
        assert all(any(isinstance(f, sec.RedactingFilter) for f in h.filters) for h in mine)
        setup_logging(log_dir=tmp_path, name="t3")                   # idempotent: replaced, not duplicated
        assert len([h for h in logging.getLogger().handlers if getattr(h, HANDLER_TAG, False)]) == 2
    finally:
        teardown_logging()
    assert not [h for h in logging.getLogger().handlers if getattr(h, HANDLER_TAG, False)]


def test_formatter_output_is_redacted_even_without_the_filter(loaded: list[str]) -> None:
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom %s", (WEBHOOK,), None)
    out = JsonFormatter().format(rec)
    assert WEBHOOK not in out and json.loads(out)["msg"] == "boom ***"


def test_bad_level_and_bad_name_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="level"):
        setup_logging(log_dir=None, level="LOUD", console=False)
    with pytest.raises(ValueError, match="log name"):
        setup_logging(log_dir=tmp_path, name="../evil", console=False)
    teardown_logging()
