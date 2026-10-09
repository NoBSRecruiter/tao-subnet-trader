"""ops.secrets: keyring -> env -> secrets.env lookup; values never logged, printed, pickled or accepted from argv.

Tests install an in-memory keyring backend; the real Windows Credential Manager is never touched.
"""
from __future__ import annotations

import io
import logging
import pickle
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from taotrader.ops import secrets as sec

VALUE = "tsk_live_9f8e7d6c5b4a39281706"


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.KeyringError("backend locked")


@pytest.fixture
def mem_keyring() -> Iterator[MemoryKeyring]:
    old = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    sec._LOADED.clear()
    try:
        yield kr
    finally:
        keyring.set_keyring(old)
        sec._LOADED.clear()


@pytest.fixture
def capture_logs() -> Iterator[io.StringIO]:
    """Every record from every logger at DEBUG, formatted with its args."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def test_lookup_order(mem_keyring: MemoryKeyring, tmp_path: Path) -> None:
    f = tmp_path / "secrets.env"
    f.write_text("# taotrader\nexport TAOSTATS_API_KEY='from-file-value'\n", encoding="utf-8")
    env = {"TAOSTATS_API_KEY": "from-env-value"}
    assert sec.get_secret("taostats", env={}, secrets_file=tmp_path / "none.env", argv=[]) is None
    s = sec.get_secret("taostats", env={}, secrets_file=f, argv=[])
    assert s is not None and s.reveal() == "from-file-value" and s.source == "secrets_file"
    s = sec.get_secret("taostats", env=env, secrets_file=f, argv=[])
    assert s is not None and s.reveal() == "from-env-value" and s.source == "env"
    mem_keyring.set_password(sec.SERVICE, "taostats", VALUE)
    s = sec.get_secret("taostats", env=env, secrets_file=f, argv=[])
    assert s is not None and s.reveal() == VALUE and s.source == "keyring"


def test_keyring_only_secret_ignores_env(mem_keyring: MemoryKeyring) -> None:
    assert sec.get_secret("live_arm_hmac", env={"": "x"}, argv=[]) is None
    mem_keyring.set_password(sec.SERVICE, "live_arm_hmac", VALUE)
    s = sec.require_secret("live_arm_hmac", env={}, argv=[])
    assert s.reveal() == VALUE


def test_broken_keyring_falls_back_without_leaking(capture_logs: io.StringIO) -> None:
    old = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    try:
        s = sec.get_secret("taostats", env={"TAOSTATS_API_KEY": VALUE}, argv=[])
    finally:
        keyring.set_keyring(old)
        sec._LOADED.clear()
    assert s is not None and s.source == "env"
    assert "KeyringError" in capture_logs.getvalue() and VALUE not in capture_logs.getvalue()


def test_unknown_and_missing_secrets(mem_keyring: MemoryKeyring, tmp_path: Path) -> None:
    with pytest.raises(sec.SecretError, match="unknown secret"):
        sec.get_secret("nope", env={}, argv=[])
    with pytest.raises(sec.SecretMissing, match="TAOSTATS_API_KEY") as ei:
        sec.require_secret("taostats", env={}, secrets_file=tmp_path / "none.env", argv=[])
    assert VALUE not in str(ei.value)


def test_secret_value_never_printed_or_pickled(mem_keyring: MemoryKeyring) -> None:
    mem_keyring.set_password(sec.SERVICE, "taostats", VALUE)
    s = sec.require_secret("taostats", env={}, argv=[])
    for text in (str(s), repr(s), f"{s}", f"{s!r}", "%s" % (s,), str([s]), str({"k": s})):  # noqa: UP031
        assert VALUE not in text
    with pytest.raises(TypeError):
        pickle.dumps(s)
    assert s == sec.require_secret("taostats", env={}, argv=[])


def test_secrets_never_appear_in_logs(mem_keyring: MemoryKeyring, capture_logs: io.StringIO, tmp_path: Path) -> None:
    sec.set_secret("taostats", VALUE)
    s = sec.require_secret("taostats", env={}, argv=[])
    sec.get_secret("alert_webhook", env={"TAOTRADER_ALERT_WEBHOOK": "https://hooks.example/abcdef123456"}, argv=[])
    logging.getLogger("taotrader.test").info("using %s and %r", s, s)
    out = capture_logs.getvalue()
    assert "taostats" in out                                                  # names and sources are logged
    assert VALUE not in out and "abcdef123456" not in out


def test_redacting_filter_scrubs_accidental_reveals(mem_keyring: MemoryKeyring) -> None:
    mem_keyring.set_password(sec.SERVICE, "taostats", VALUE)
    s = sec.require_secret("taostats", env={}, argv=[])
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(sec.RedactingFilter())
    log = logging.getLogger("taotrader.test.redact")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        log.info("header Authorization: %s", s.reveal())                   # a bug elsewhere: still scrubbed
        log.info("plain message with %d args", 2)
    finally:
        log.removeHandler(handler)
    assert VALUE not in buf.getvalue() and "Authorization: ***" in buf.getvalue()
    assert "plain message with 2 args" in buf.getvalue()
    assert sec.redact(f"x {VALUE} y") == "x *** y"


def test_secret_in_argv_is_refused(mem_keyring: MemoryKeyring) -> None:
    mem_keyring.set_password(sec.SERVICE, "taostats", VALUE)
    with pytest.raises(sec.SecretLeak) as ei:
        sec.get_secret("taostats", env={}, argv=["taotrader", "collect", f"--key={VALUE}"])
    assert VALUE not in str(ei.value)
    assert sec.get_secret("taostats", env={}, argv=["taotrader", "collect"]) is not None


def test_set_secret_rejects_empty_and_unknown(mem_keyring: MemoryKeyring) -> None:
    with pytest.raises(sec.SecretError):
        sec.set_secret("taostats", "")
    with pytest.raises(sec.SecretError):
        sec.set_secret("bogus", VALUE)
    sec.set_secret("onfinality", "https://x.onfinality.io/rpc?apikey=0123456789")
    assert mem_keyring.store[(sec.SERVICE, "onfinality")].endswith("0123456789")


def test_parse_secrets_env() -> None:
    text = "\n# c\nA=1\nexport B = \"two words\"\nbad line\nC='x=y'\n=novalue\n"
    assert sec.parse_secrets_env(text) == {"A": "1", "B": "two words", "C": "x=y"}
    assert sec.default_secrets_file().parts[-2:] == (".taotrader", "secrets.env")
