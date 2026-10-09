"""taotrader/ops/secrets.py - secrets from the OS keyring (Windows Credential Manager) with env fallbacks (WP0).

Leaf module: imports only core, the stdlib and keyring (DESIGN.md section 4.2).

Rules (section 12.4):
- Secrets never live in the repo, in config files or in argv. A value that also appears on the process command
  line is refused (SecretLeak): it has already leaked into the process list and shell history.
- Values are never logged. A loaded value is wrapped in Secret, whose repr/str/format print "***"; call
  .reveal() only at the point of use (an HTTP header, an HMAC key). The module logs names and sources only.
- redact(text) replaces every value loaded in this process with "***"; RedactingFilter applies it to log
  records (WP12's logging installs it on its handlers).

Lookup order for get_secret(name):
1. keyring: service "taotrader", username <name> (Windows Credential Manager target "<name>@taotrader");
2. the process environment variable of the secret (e.g. TAOSTATS_API_KEY);
3. the same variable in %USERPROFILE%\\.taotrader\\secrets.env (KEY=VALUE lines; restrict it with icacls).
Secrets with no env variable (the live arm-token HMAC key) come from the keyring only.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn

SERVICE: Final[str] = "taotrader"
MIN_LEAK_CHECK_LEN: Final[int] = 6          # shorter values are not searched for in argv/log text
REDACTED: Final[str] = "***"

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str              # keyring username under SERVICE
    env_var: str           # environment fallback ("" = keyring only)
    description: str


KNOWN: Final[Mapping[str, SecretSpec]] = MappingProxyType({
    "taostats": SecretSpec("taostats", "TAOSTATS_API_KEY", "Taostats REST API key (raw Authorization header)"),
    "onfinality": SecretSpec("onfinality", "TAOTRADER_ONFINALITY_URL",
                             "keyed OnFinality RPC URL (https://.../rpc?apikey=...)"),
    "alert_webhook": SecretSpec("alert_webhook", "TAOTRADER_ALERT_WEBHOOK", "alert webhook URL (Discord/Telegram/generic)"),
    "healthcheck_url": SecretSpec("healthcheck_url", "TAOTRADER_HEALTHCHECK_URL", "dead-man ping URL"),
    "live_arm_hmac": SecretSpec("live_arm_hmac", "", "live arm-token HMAC key (live host keyring only)"),
})


class SecretError(Exception):
    """Base class. Messages name the secret and the source, never the value."""


class SecretMissing(SecretError):
    pass


class SecretLeak(SecretError):
    pass


class Secret:
    """An opaque secret value. Printing, formatting, repr and pickling never reveal it."""

    __slots__ = ("_name", "_source", "_value")

    def __init__(self, name: str, value: str, source: str) -> None:
        self._name = name
        self._value = value
        self._source = source

    @property
    def name(self) -> str:
        return self._name

    @property
    def source(self) -> str:
        return self._source

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret(name={self._name!r}, source={self._source!r}, value={REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __reduce__(self) -> NoReturn:
        raise TypeError("Secret values cannot be pickled or serialized")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._name == self._name and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._name))


_LOADED: dict[str, str] = {}                 # name -> value, for redact(); never logged


def default_secrets_file() -> Path:
    """%USERPROFILE%\\.taotrader\\secrets.env (Windows) or ~/.taotrader/secrets.env."""
    return Path.home() / ".taotrader" / "secrets.env"


def parse_secrets_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and '#' comments skipped; optional 'export ' prefix; matching quotes stripped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _spec(name: str) -> SecretSpec:
    spec = KNOWN.get(name)
    if spec is None:
        raise SecretError(f"unknown secret {name!r}; known: {', '.join(sorted(KNOWN))}")
    return spec


def _from_keyring(name: str) -> str | None:
    try:
        import keyring
        import keyring.errors
    except ImportError:                                        # pragma: no cover - keyring is a runtime dependency
        log.warning("secret %s: keyring is not installed; skipping the keyring source", name)
        return None
    try:
        value = keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError as e:
        log.warning("secret %s: keyring lookup failed (%s); trying fallbacks", name, type(e).__name__)
        return None
    return value or None


def _from_file(env_var: str, path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("secrets file %s unreadable (%s)", path, type(e).__name__)
        return None
    return parse_secrets_env(text).get(env_var) or None


def check_argv(name: str, value: str, argv: Sequence[str]) -> None:
    """Raise SecretLeak if the value appears in any command-line argument."""
    if len(value) >= MIN_LEAK_CHECK_LEN and any(value in a for a in argv):
        raise SecretLeak(f"secret {name!r} appears on the process command line; pass secrets through the keyring "
                         "or the environment, never argv")


def get_secret(name: str, *, env: Mapping[str, str] | None = None, secrets_file: Path | None = None,
               use_keyring: bool = True, argv: Sequence[str] | None = None) -> Secret | None:
    """Look up a known secret (keyring -> env -> secrets.env). None if absent everywhere."""
    spec = _spec(name)
    environ = os.environ if env is None else env
    value: str | None = None
    source = ""
    if use_keyring:
        value, source = _from_keyring(name), "keyring"
    if value is None and spec.env_var:
        value, source = environ.get(spec.env_var) or None, "env"
    if value is None and spec.env_var:
        path = default_secrets_file() if secrets_file is None else secrets_file
        value, source = _from_file(spec.env_var, path), "secrets_file"
    if value is None:
        log.debug("secret %s not found (keyring%s)", name, ", env, secrets file" if spec.env_var else " only")
        return None
    check_argv(name, value, sys.argv if argv is None else argv)
    _LOADED[name] = value
    log.debug("secret %s loaded from %s", name, source)
    return Secret(name, value, source)


def require_secret(name: str, *, env: Mapping[str, str] | None = None, secrets_file: Path | None = None,
                   use_keyring: bool = True, argv: Sequence[str] | None = None) -> Secret:
    """get_secret or raise SecretMissing (the message says where to put it, never a value)."""
    s = get_secret(name, env=env, secrets_file=secrets_file, use_keyring=use_keyring, argv=argv)
    if s is None:
        spec = _spec(name)
        where = f"keyring ({SERVICE}/{name})" + (f", env {spec.env_var} or the secrets.env file" if spec.env_var else "")
        raise SecretMissing(f"secret {name!r} ({spec.description}) is not set: add it to the {where}")
    return s


def set_secret(name: str, value: str) -> None:
    """Store a known secret in the OS keyring. The CLI reads the value with getpass, never from argv."""
    _spec(name)
    if not value:
        raise SecretError(f"refusing to store an empty value for {name!r}")
    import keyring

    keyring.set_password(SERVICE, name, value)
    _LOADED[name] = value
    log.info("secret %s stored in the keyring", name)


def redact(text: str) -> str:
    """Replace every secret value loaded in this process with ***."""
    for value in sorted(_LOADED.values(), key=len, reverse=True):
        if len(value) >= MIN_LEAK_CHECK_LEN and value in text:
            text = text.replace(value, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts loaded secret values from the formatted message (and drops args)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        clean = redact(msg)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True
