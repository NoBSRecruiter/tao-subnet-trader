"""Shared fixtures for the WP12 ops/CLI tests (tests/ops). WP0's test_config_load.py and test_secrets.py keep their own.

- `kr`: an in-memory keyring backend (the real Windows Credential Manager is never touched) with ops.secrets' loaded
  values cleared before and after;
- `no_secret_env`: removes every secret environment variable, so a developer's own keys never leak into a test;
- `tmp_cfg`: a writable config that points data_dir (and the paper lake/hot) into tmp_path; returns (path, data_dir).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest

from taotrader.ops import secrets as sec


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


@pytest.fixture
def no_secret_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for spec in sec.KNOWN.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.setattr(sec, "default_secrets_file", lambda: tmp_path / "no-secrets.env")
    for name in list(os.environ):
        if name.upper().startswith("TAOTRADER_CFG_") or name.upper().startswith("TAOTRADER_LIVE"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def kr(no_secret_env: None) -> Iterator[MemoryKeyring]:
    old = keyring.get_keyring()
    k = MemoryKeyring()
    keyring.set_keyring(k)
    sec._LOADED.clear()
    try:
        yield k
    finally:
        keyring.set_keyring(old)
        sec._LOADED.clear()


@pytest.fixture
def tmp_cfg(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    p = tmp_path / "test.toml"
    p.write_text(f'data_dir = "{data.as_posix()}"\n', encoding="utf-8")
    return p, data
