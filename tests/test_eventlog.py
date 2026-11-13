"""Tests for the agent-hq event-emit wrapper (#231)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from agent_hq import eventlog


@pytest.fixture(autouse=True)
def _reset_eventlog():
    eventlog.set_process_role("cli")
    eventlog._last_emit.clear()
    yield
    eventlog.set_process_role("cli")
    eventlog._last_emit.clear()


def test_inline_role_calls_log_directly():
    eventlog.set_process_role("cli")
    with patch("agent_hq.logclient.log", return_value=True) as log:
        eventlog.emit("error", "dispatch failed", {"repo": "a/b"})
    log.assert_called_once()
    app, level, message = log.call_args[0][:3]
    assert (app, level, message) == ("agent-hq", "error", "dispatch failed")
    assert log.call_args[0][3] == {"repo": "a/b", "process": "cli"}


def test_hook_role_enqueues_without_network():
    eventlog.set_process_role("hook")
    with (
        patch("agent_hq.logclient.enqueue", return_value=True) as enqueue,
        patch("agent_hq.logclient.log") as log,
    ):
        eventlog.emit("error", "hook state write failed", {"pane": "%1"})
    log.assert_not_called()
    assert enqueue.call_args[0][3] == {"pane": "%1", "process": "hook"}


def test_thread_role_delivers_off_caller_thread():
    eventlog.set_process_role("web")
    done = threading.Event()
    seen = {}

    def fake_log(app, level, message, extra):
        seen["thread"] = threading.current_thread()
        seen["extra"] = extra
        done.set()
        return True

    with patch("agent_hq.logclient.log", side_effect=fake_log):
        eventlog.emit("warn", "background refresh failed", {"name": "issues"})
        assert done.wait(timeout=5)
    assert seen["thread"] is not threading.main_thread()
    assert seen["extra"]["process"] == "web"


def test_dedupe_suppresses_repeat_within_window():
    with patch("agent_hq.logclient.log", return_value=True) as log:
        eventlog.emit("error", "web cache poll loop failing", min_interval_s=900)
        eventlog.emit("error", "web cache poll loop failing", min_interval_s=900)
    assert log.call_count == 1


def test_dedupe_is_per_message():
    with patch("agent_hq.logclient.log", return_value=True) as log:
        eventlog.emit("error", "message one")
        eventlog.emit("error", "message two")
    assert log.call_count == 2


def test_dedupe_expires_after_window(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(eventlog.time, "monotonic", lambda: clock["now"])
    with patch("agent_hq.logclient.log", return_value=True) as log:
        eventlog.emit("error", "repeat me", min_interval_s=60)
        clock["now"] += 61
        eventlog.emit("error", "repeat me", min_interval_s=60)
    assert log.call_count == 2


def test_emit_never_raises():
    with patch("agent_hq.logclient.log", side_effect=RuntimeError("bug")):
        eventlog.emit("error", "boom")  # must not raise


def test_flush_runs_off_caller_thread():
    done = threading.Event()
    with patch("agent_hq.logclient.flush", side_effect=lambda: done.set() or True):
        eventlog.flush()
        assert done.wait(timeout=5)


def test_importing_eventlog_does_not_load_the_transport():
    """#237: hooks.py imports eventlog on every harness hook; the transport
    (urllib.request + ssl, ~12 ms cold) loads only when an event is sent."""
    import subprocess
    import sys

    code = (
        "import sys, agent_hq.hooks, agent_hq.eventlog;"
        "print('agent_hq.logclient' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["False"]


def test_reported_marker_round_trips():
    exc = RuntimeError("x")
    assert eventlog.was_reported(exc) is False
    eventlog.mark_reported(exc)
    assert eventlog.was_reported(exc) is True


def test_thread_role_loads_the_transport_off_caller_thread():
    """Review #237: the lazy import must not stall the web event loop or the
    daemon tick on the first event — it runs in the spawned thread too."""
    eventlog.set_process_role("web")
    done = threading.Event()
    seen = {}
    real = eventlog._transport

    def recording_transport():
        seen["thread"] = threading.current_thread()
        done.set()
        return real()

    with (
        patch.object(eventlog, "_transport", side_effect=recording_transport),
        patch("agent_hq.logclient.log", return_value=True),
    ):
        eventlog.emit("warn", "lazy import off thread")
        assert done.wait(timeout=5)
    assert seen["thread"] is not threading.main_thread()


def test_emit_reports_whether_it_passed_the_dedupe():
    with patch("agent_hq.logclient.log", return_value=True):
        assert eventlog.emit("error", "once only") is True
        assert eventlog.emit("error", "once only") is False
