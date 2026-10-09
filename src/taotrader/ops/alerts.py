"""taotrader/ops/alerts.py - alert sinks, dedupe with cooldown, and the dead-man ping (WP12; DESIGN.md section 12.5).

Sinks (any combination; a failing sink never raises into the caller and never blocks the trading loop for long):
- `LogSink`: a WARNING/ERROR log record on logger "taotrader.alert" (always on: the JSON log is the audit trail);
- `WebhookSink`: HTTP POST of a small JSON body to the URL in ops.secrets "alert_webhook" (Discord: {"content"},
  Telegram bot API sendMessage URL: {"text"}, anything else: {"text", "kind", "severity", "source"});
- `ToastSink`: a Windows toast (PowerShell Windows.UI.Notifications, fire-and-forget); a no-op elsewhere.

`AlertManager.alert(kind, message, *, key=None, severity=...)` fans an alert out to every sink unless the same
(kind, key) was sent within `cooldown_s` (default 15 min; per-kind overrides). Suppressed repeats are counted and the
count is appended to the next delivered alert of that key. The clock is injected (tests use a fake clock). Messages
pass through ops.logging.redact_line before any sink sees them, so a webhook never carries a loaded secret.

`DeadManPinger` GETs the healthcheck URL (ops.secrets "healthcheck_url", e.g. healthchecks.io) every
`interval_s` (60 s) while the process is healthy; the monitoring side alerts when the pings stop. `ping_once()` is the
unit; `run(stop)` loops until the stop event. `is_healthy()` (injected) gates each ping, so a stalled feed stops the
pings even though the process is alive.

The wall clock enters only here (cooldowns, ping cadence): alerts are side effects, never decision inputs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Final, Protocol

import httpx

from .logging import redact_line
from .secrets import Secret, get_secret

__all__ = [
    "DEFAULT_COOLDOWN_S", "Alert", "AlertManager", "AlertSink", "DeadManPinger", "LogSink", "MemorySink", "Severity",
    "ToastSink", "WebhookSink", "build_alert_manager", "webhook_body",
]

log = logging.getLogger("taotrader.alert")

DEFAULT_COOLDOWN_S: Final[float] = 15 * 60.0
WEBHOOK_TIMEOUT_S: Final[float] = 10.0
MAX_MESSAGE: Final[int] = 1_800                 # Discord content limit is 2,000 characters
# Kinds whose repeats matter more often (a live order outcome is never deduplicated across orders: callers key it).
KIND_COOLDOWN_S: Final[Mapping[str, float]] = {
    "key_alarm": 5 * 60.0, "invariant": 5 * 60.0, "orphan": 5 * 60.0, "heartbeat": 60 * 60.0, "daily_summary": 0.0,
    "idle_proxy": 12 * 3600.0, "live_unarmed": 30 * 60.0,
}
CRITICAL_KINDS: Final[frozenset[str]] = frozenset({"key_alarm", "invariant", "orphan", "idle_proxy", "recon", "reconcile"})


class Severity(IntEnum):
    INFO = 20
    WARNING = 30
    CRITICAL = 50


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    message: str
    severity: Severity = Severity.WARNING
    key: str = ""
    source: str = "taotrader"
    ts: float = 0.0                 # wall-clock seconds (metadata)
    suppressed: int = 0             # repeats suppressed since the last delivery of this (kind, key)

    def text(self) -> str:
        head = f"[taotrader {self.severity.name}] {self.kind}"
        tail = f" (+{self.suppressed} similar suppressed)" if self.suppressed else ""
        return f"{head}: {self.message}{tail}"[:MAX_MESSAGE]


class AlertSink(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class LogSink:
    name = "log"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or log

    def send(self, alert: Alert) -> None:
        lvl = logging.ERROR if alert.severity >= Severity.CRITICAL else (
            logging.WARNING if alert.severity >= Severity.WARNING else logging.INFO)
        self.logger.log(lvl, "ALERT %s: %s", alert.kind, alert.text(), extra={"alert_kind": alert.kind,
                                                                              "alert_key": alert.key})


class MemorySink:
    """Collects alerts (tests, and `doctor` output)."""
    name = "memory"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def webhook_body(url: str, alert: Alert) -> dict[str, Any]:
    """The JSON body for the webhook flavour the URL points at."""
    text = alert.text()
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        return {"content": text}
    if "api.telegram.org" in url:
        return {"text": text, "disable_web_page_preview": True}
    return {"text": text, "kind": alert.kind, "severity": alert.severity.name, "source": alert.source,
            "key": alert.key, "ts": int(alert.ts)}


class WebhookSink:
    """POST to the alert webhook. The URL is a Secret (never logged); failures are logged by type only."""
    name = "webhook"

    def __init__(self, url: Secret, *, timeout_s: float = WEBHOOK_TIMEOUT_S,
                 post: Callable[[str, Mapping[str, Any], float], int] | None = None) -> None:
        self._url = url
        self.timeout_s = timeout_s
        self._post = post or _http_post
        self.failures = 0

    def send(self, alert: Alert) -> None:
        url = self._url.reveal()
        try:
            status = self._post(url, webhook_body(url, alert), self.timeout_s)
            if status >= 300:
                self.failures += 1
                log.warning("alert webhook answered HTTP %d", status)
        except Exception as e:
            self.failures += 1
            log.warning("alert webhook failed (%s)", type(e).__name__)


def _http_post(url: str, body: Mapping[str, Any], timeout_s: float) -> int:
    with httpx.Client(timeout=timeout_s) as c:
        return c.post(url, json=dict(body)).status_code


def _http_get(url: str, timeout_s: float) -> int:
    with httpx.Client(timeout=timeout_s) as c:
        return c.get(url).status_code


_TOAST_PS: Final[str] = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$n = $t.GetElementsByTagName('text'); $n.Item(0).AppendChild($t.CreateTextNode($env:TT_TITLE)) | Out-Null;"
    "$n.Item(1).AppendChild($t.CreateTextNode($env:TT_BODY)) | Out-Null;"
    "$x = [Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('taotrader').Show($x)")


class ToastSink:
    """Windows toast via PowerShell (no extra dependency). Title and body travel in environment variables, never on the
    command line. Fire-and-forget; a no-op on other platforms."""
    name = "toast"

    def __init__(self, *, enabled: bool | None = None,
                 spawn: Callable[[list[str], dict[str, str]], object] | None = None) -> None:
        self.enabled = (sys.platform == "win32") if enabled is None else enabled
        self._spawn = spawn or _spawn_detached

    def send(self, alert: Alert) -> None:
        if not self.enabled:
            return
        import os
        env = dict(os.environ)
        env["TT_TITLE"] = f"taotrader {alert.severity.name}: {alert.kind}"[:120]
        env["TT_BODY"] = alert.text()[:240]
        try:
            self._spawn(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS], env)
        except Exception as e:
            log.debug("toast failed (%s)", type(e).__name__)


def _spawn_detached(cmd: list[str], env: dict[str, str]) -> object:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)


@dataclass
class _KeyState:
    last_sent: float
    suppressed: int = 0


@dataclass
class AlertManager:
    """Dedupe (same kind + key within the cooldown) and fan-out. Thread-safe (the Runner commits on a worker thread)."""
    sinks: Sequence[AlertSink]
    clock: Callable[[], float] = time.time
    cooldown_s: float = DEFAULT_COOLDOWN_S
    kind_cooldown_s: Mapping[str, float] = field(default_factory=lambda: dict(KIND_COOLDOWN_S))
    source: str = "taotrader"
    _state: dict[tuple[str, str], _KeyState] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    sent: int = field(default=0, init=False)
    suppressed: int = field(default=0, init=False)

    def cooldown_for(self, kind: str) -> float:
        return float(self.kind_cooldown_s.get(kind, self.cooldown_s))

    def alert(self, kind: str, message: str, *, key: str | None = None, severity: Severity | None = None) -> bool:
        """Deliver unless deduplicated. Returns True when delivered."""
        sev = severity if severity is not None else (Severity.CRITICAL if kind in CRITICAL_KINDS else Severity.WARNING)
        k = (kind, message[:120] if key is None else key)
        now = float(self.clock())
        with self._lock:
            st = self._state.get(k)
            if st is not None and now - st.last_sent < self.cooldown_for(kind):
                st.suppressed += 1
                self.suppressed += 1
                return False
            n_supp = 0 if st is None else st.suppressed
            self._state[k] = _KeyState(last_sent=now)
            self.sent += 1
        a = Alert(kind=kind, message=redact_line(message), severity=sev, key=k[1], source=self.source, ts=now,
                  suppressed=n_supp)
        for sink in self.sinks:
            try:
                sink.send(a)
            except Exception as e:
                log.warning("alert sink %s failed (%s)", getattr(sink, "name", "?"), type(e).__name__)
        return True

    def hook(self) -> Callable[[str, str], None]:
        """An `on_alert(kind, message)` callable for the Runner, LiveReconciler and ArmingMonitor."""
        def on_alert(kind: str, message: str) -> None:
            self.alert(kind, message)
        return on_alert


def build_alert_manager(*, toast: bool = True, webhook: bool = True, clock: Callable[[], float] = time.time,
                        extra_sinks: Sequence[AlertSink] = (), source: str = "taotrader") -> AlertManager:
    """Log sink always; webhook when ops.secrets "alert_webhook" is set; Windows toast when asked and on Windows."""
    sinks: list[AlertSink] = [LogSink()]
    if webhook:
        url = get_secret("alert_webhook")
        if url is not None:
            sinks.append(WebhookSink(url))
    if toast and sys.platform == "win32":
        sinks.append(ToastSink())
    sinks.extend(extra_sinks)
    return AlertManager(sinks=sinks, clock=clock, source=source)


class DeadManPinger:
    """Ping a healthcheck URL every `interval_s` while `is_healthy()` holds (DESIGN.md 12.5)."""

    def __init__(self, url: Secret | None, *, interval_s: float = 60.0, is_healthy: Callable[[], bool] = lambda: True,
                 get: Callable[[str, float], int] | None = None, clock: Callable[[], float] = time.monotonic,
                 timeout_s: float = 10.0) -> None:
        self._url = url
        self.interval_s = interval_s
        self.is_healthy = is_healthy
        self._get = get or _http_get
        self.clock = clock
        self.timeout_s = timeout_s
        self.pings = 0
        self.failures = 0
        self.skipped = 0
        self.last_ping: float | None = None

    @property
    def enabled(self) -> bool:
        return self._url is not None

    def due(self) -> bool:
        return self.enabled and (self.last_ping is None or self.clock() - self.last_ping >= self.interval_s)

    def ping_once(self) -> bool:
        """One ping (if enabled, due and healthy). Returns True when a ping was sent successfully."""
        if self._url is None or not self.due():
            return False
        self.last_ping = self.clock()
        if not self.is_healthy():
            self.skipped += 1
            return False
        try:
            status = self._get(self._url.reveal(), self.timeout_s)
        except Exception as e:
            self.failures += 1
            log.warning("dead-man ping failed (%s)", type(e).__name__)
            return False
        if status >= 300:
            self.failures += 1
            log.warning("dead-man ping answered HTTP %d", status)
            return False
        self.pings += 1
        return True

    async def run(self, stop: asyncio.Event, *, poll_s: float = 5.0) -> None:
        """Loop until `stop` is set; the blocking GET runs in a worker thread."""
        if not self.enabled:
            return
        while not stop.is_set():
            if self.due():
                await asyncio.to_thread(self.ping_once)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_s)


def alert_payload_json(alert: Alert) -> str:
    """Canonical JSON of an alert (status files and tests)."""
    return json.dumps({"kind": alert.kind, "message": alert.message, "severity": alert.severity.name, "key": alert.key,
                       "suppressed": alert.suppressed}, sort_keys=True)
