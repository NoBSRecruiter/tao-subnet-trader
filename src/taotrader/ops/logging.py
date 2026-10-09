"""taotrader/ops/logging.py - JSON-lines logging with secret redaction (WP12; DESIGN.md sections 4.7, 12.4, 12.5).

`setup_logging(...)` installs, on the root logger:
- a JSON-lines file handler (`<log_dir>/<name>.jsonl`, size-rotated) when `log_dir` is given;
- a console handler on stderr (human-readable text, or JSON lines with `console_json=True`).

Every handler carries ops.secrets.RedactingFilter (the loaded secret values in the formatted message are replaced by
"***" and the args are dropped) AND formats through `redact_line`, which redacts the WHOLE output line - including
exception tracebacks and `extra` fields, which the filter alone never sees - and masks credential-looking URL
parameters (`apikey=`, `token=`, `password=`, `user:pass@` userinfo), even for values this process never loaded.

A JSON line is one object: {"ts": ISO-8601 UTC with ms, "level", "logger", "msg", "pid", optional "exc", optional
"extra": {...}} with sorted keys. Timestamps are wall-clock metadata of the log line only (never a decision input).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, TextIO

from .secrets import REDACTED, RedactingFilter, redact

__all__ = [
    "HANDLER_TAG",
    "JsonFormatter",
    "TextFormatter",
    "quiet_libraries",
    "redact_line",
    "setup_logging",
    "teardown_logging",
]

HANDLER_TAG: Final[str] = "_taotrader_handler"
DEFAULT_MAX_BYTES: Final[int] = 20 * 1024 * 1024
DEFAULT_BACKUPS: Final[int] = 10
_STD_ATTRS: Final[frozenset[str]] = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process",
    "taskName", "message", "asctime"})
# Credential-looking fragments that are masked even when the value was never loaded through ops.secrets.
_URL_PARAM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|auth|authorization)(=|:\s*|\"\s*:\s*\")([^\s&\"',;}]+)")
_USERINFO_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")


def redact_line(text: str) -> str:
    """Redact loaded secret values (ops.secrets.redact) and credential-looking URL fragments from a whole line."""
    out = redact(text)
    out = _URL_PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return _USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", out)


def _iso(created: float) -> str:
    t = time.gmtime(created)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + f".{int((created % 1) * 1000):03d}Z"


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return str(v)


class JsonFormatter(logging.Formatter):
    """One JSON object per line (sorted keys); the whole line is passed through `redact_line`."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        doc: dict[str, Any] = {"ts": _iso(record.created), "level": record.levelname, "logger": record.name, "msg": msg,
                               "pid": record.process}
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            doc["exc"] = record.exc_text
        if record.stack_info:
            doc["stack"] = self.formatStack(record.stack_info)
        extra = {k: _jsonable(v) for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        if extra:
            doc["extra"] = extra
        return redact_line(json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str))


class TextFormatter(logging.Formatter):
    """Human-readable console lines; redacted like the JSON lines."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact_line(super().format(record))


def _tag(h: logging.Handler) -> logging.Handler:
    setattr(h, HANDLER_TAG, True)
    h.addFilter(RedactingFilter())
    return h


def teardown_logging(root: logging.Logger | None = None) -> None:
    """Remove (and close) the handlers a previous setup_logging installed."""
    lg = root if root is not None else logging.getLogger()
    for h in list(lg.handlers):
        if getattr(h, HANDLER_TAG, False):
            lg.removeHandler(h)
            h.close()


def quiet_libraries(level: int = logging.WARNING) -> None:
    """httpx/httpcore/websockets log every request at INFO; keep them at WARNING (their URLs are redacted anyway)."""
    for name in ("httpx", "httpcore", "websockets", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(level)


def setup_logging(*, log_dir: str | Path | None = None, name: str = "taotrader", level: int | str = logging.INFO,
                  console: bool = True, console_json: bool = False, stream: TextIO | None = None,
                  max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> Path | None:
    """Install the redacting handlers on the root logger (idempotent: earlier taotrader handlers are replaced).
    Returns the JSON-lines file path (None without `log_dir`)."""
    root = logging.getLogger()
    teardown_logging(root)
    lvl = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    if not isinstance(lvl, int):
        raise ValueError(f"unknown log level {level!r}")
    root.setLevel(lvl)
    path: Path | None = None
    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError(f"bad log name {name!r}")
        path = d / f"{name}.jsonl"
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
                                                  delay=False)
        fh.setFormatter(JsonFormatter())
        root.addHandler(_tag(fh))
    if console:
        ch = logging.StreamHandler(stream if stream is not None else sys.stderr)
        ch.setFormatter(JsonFormatter() if console_json else TextFormatter())
        root.addHandler(_tag(ch))
    quiet_libraries()
    if os.environ.get("TAOTRADER_LOG_DEBUG_LIBS"):
        quiet_libraries(logging.DEBUG)
    return path


@contextmanager
def logging_to(log_dir: str | Path | None, **kwargs: Any) -> Iterator[Path | None]:
    """setup_logging for the duration of a block (the CLI and tests)."""
    path = setup_logging(log_dir=log_dir, **kwargs)
    try:
        yield path
    finally:
        teardown_logging()
