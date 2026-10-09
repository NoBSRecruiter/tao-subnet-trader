"""ops.alerts: dedupe with cooldown (fake clock), fan-out that survives broken sinks, webhook bodies without secrets,
and the dead-man ping."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from taotrader.ops import secrets as sec
from taotrader.ops.alerts import (
    AlertManager,
    DeadManPinger,
    LogSink,
    MemorySink,
    Severity,
    ToastSink,
    WebhookSink,
    build_alert_manager,
    webhook_body,
)


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Boom:
    name = "boom"

    def send(self, alert: Any) -> None:
        raise RuntimeError("sink down")


def test_dedupe_within_cooldown_then_deliver_with_the_suppressed_count() -> None:
    clk, mem = Clock(), MemorySink()
    am = AlertManager(sinks=[mem], clock=clk, cooldown_s=600.0, kind_cooldown_s={})
    assert am.alert("mode", "book a: NORMAL -> CAUTION", key="mode:a")
    clk.t += 10
    assert not am.alert("mode", "book a: NORMAL -> CAUTION again", key="mode:a")
    assert not am.alert("mode", "and again", key="mode:a")
    assert am.alert("mode", "other book", key="mode:b")              # another key is independent
    clk.t += 600
    assert am.alert("mode", "after the cooldown", key="mode:a")
    assert [a.key for a in mem.alerts] == ["mode:a", "mode:b", "mode:a"]
    assert mem.alerts[-1].suppressed == 2 and "(+2 similar suppressed)" in mem.alerts[-1].text()
    assert am.sent == 3 and am.suppressed == 2


def test_default_key_is_the_message_and_kinds_have_their_own_cooldown() -> None:
    clk, mem = Clock(), MemorySink()
    am = AlertManager(sinks=[mem], clock=clk, cooldown_s=900.0, kind_cooldown_s={"daily_summary": 0.0})
    assert am.alert("feed", "x") and not am.alert("feed", "x") and am.alert("feed", "y")
    assert am.alert("daily_summary", "s") and am.alert("daily_summary", "s")   # never deduplicated
    assert mem.alerts[0].severity is Severity.WARNING
    am.alert("key_alarm", "revoke the Staking proxy from the coldkey")
    assert mem.alerts[-1].severity is Severity.CRITICAL


def test_a_broken_sink_never_silences_the_others_or_raises() -> None:
    mem = MemorySink()
    am = AlertManager(sinks=[Boom(), mem, LogSink()], clock=Clock())
    assert am.alert("invariant", "inv1: ledger does not balance")
    assert len(mem.alerts) == 1


def test_webhook_bodies_by_flavour() -> None:
    from taotrader.ops.alerts import Alert

    a = Alert("orphan", "book x quarantined", Severity.CRITICAL, key="k")
    assert webhook_body("https://discord.com/api/webhooks/1/abc", a) == {"content": a.text()}
    assert webhook_body("https://api.telegram.org/botX/sendMessage?chat_id=1", a)["text"] == a.text()
    generic = webhook_body("https://hooks.example.org/x", a)
    assert generic["kind"] == "orphan" and generic["severity"] == "CRITICAL"


def test_webhook_never_carries_a_loaded_secret(kr: object, monkeypatch: pytest.MonkeyPatch) -> None:
    taostats = "tsk_live_0011223344556677"
    monkeypatch.setenv("TAOSTATS_API_KEY", taostats)
    assert sec.get_secret("taostats", use_keyring=False, argv=[]) is not None
    posted: list[tuple[str, Mapping[str, Any]]] = []

    def post(url: str, body: Mapping[str, Any], timeout: float) -> int:
        posted.append((url, body))
        return 204

    url = sec.Secret("alert_webhook", "https://discord.com/api/webhooks/9/tok", "test")
    am = AlertManager(sinks=[WebhookSink(url, post=post)], clock=Clock())
    am.alert("feed", f"request with key {taostats} failed")
    ((u, body),) = posted
    assert u.endswith("/tok") and taostats not in str(body) and "***" in str(body)
    assert "tok" not in repr(url)


def test_webhook_failures_are_counted_not_raised() -> None:
    def post(url: str, body: Mapping[str, Any], timeout: float) -> int:
        raise ConnectionError("down")

    sink = WebhookSink(sec.Secret("alert_webhook", "https://h.example/x", "t"), post=post)
    AlertManager(sinks=[sink], clock=Clock()).alert("feed", "x")
    sink2 = WebhookSink(sec.Secret("alert_webhook", "https://h.example/x", "t"), post=lambda u, b, t: 500)
    AlertManager(sinks=[sink2], clock=Clock()).alert("feed", "x")
    assert sink.failures == 1 and sink2.failures == 1


def test_toast_sink_passes_text_through_the_environment_not_argv() -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    t = ToastSink(enabled=True, spawn=lambda cmd, env: calls.append((cmd, env)))
    am = AlertManager(sinks=[t], clock=Clock())
    am.alert("stall", "feed stalled for 40 s")
    ((cmd, env),) = calls
    assert "feed stalled" not in " ".join(cmd) and "feed stalled" in env["TT_BODY"]
    ToastSink(enabled=False, spawn=lambda c, e: calls.append((c, e))).send(Boom())  # type: ignore[arg-type]
    assert len(calls) == 1


def test_build_alert_manager_uses_the_webhook_secret_when_set(kr: object, monkeypatch: pytest.MonkeyPatch) -> None:
    assert [s.name for s in build_alert_manager(toast=False).sinks] == ["log"]
    monkeypatch.setenv("TAOTRADER_ALERT_WEBHOOK", "https://hooks.example.org/abcdef")
    assert [s.name for s in build_alert_manager(toast=False).sinks] == ["log", "webhook"]


def _recorder(got: list[str]) -> Any:
    def get(url: str, timeout_s: float) -> int:
        got.append(url)
        return 200
    return get


def test_dead_man_ping_cadence_and_health_gate() -> None:
    clk = Clock(0.0)
    got: list[str] = []
    healthy = {"v": True}
    p = DeadManPinger(sec.Secret("healthcheck_url", "https://hc.example/ping/uuid", "t"), interval_s=60.0,
                      is_healthy=lambda: healthy["v"], get=_recorder(got), clock=clk)
    assert p.ping_once() and not p.ping_once()                       # not due again within 60 s
    clk.t = 61.0
    healthy["v"] = False
    assert not p.ping_once() and p.skipped == 1                      # unhealthy: the ping is withheld
    clk.t = 125.0
    healthy["v"] = True
    assert p.ping_once()
    assert p.pings == 2 and len(got) == 2
    off = DeadManPinger(None)
    assert not off.enabled and not off.ping_once()


def test_dead_man_run_loop_stops_on_the_event() -> None:
    got: list[str] = []
    p = DeadManPinger(sec.Secret("healthcheck_url", "https://hc.example/p", "t"), interval_s=0.01,
                      get=_recorder(got))

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.ensure_future(p.run(stop, poll_s=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 2.0)

    asyncio.run(go())
    assert p.pings >= 2
