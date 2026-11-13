"""Daemon unified-logger event emission (#231)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_hq import daemon


class _StubRuntimeStatusCollector:
    """``run_poll_loop`` starts a real ``RuntimeStatusCollector`` thread whose
    ``ps``/``claude`` subprocesses poll with ``time.sleep`` — the same global
    these tests patch to stop the loop. On a slow CI runner that thread ate
    the tick counter (or took the KeyboardInterrupt itself), cutting the
    daemon loop short. The collector has its own suite; stub it here."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def set_claude_present(self, present):
        pass


@pytest.fixture(autouse=True)
def _stub_runtime_status_collector(monkeypatch):
    monkeypatch.setattr(
        "agent_hq.status_collector.RuntimeStatusCollector", _StubRuntimeStatusCollector
    )


def _run_loop_ticks(n_ticks, tick_side_effect=None):
    """Run run_poll_loop for n ticks by making time.sleep raise after n calls."""
    calls = {"n": 0}

    def fake_sleep(_interval):
        calls["n"] += 1
        if calls["n"] >= n_ticks:
            raise KeyboardInterrupt

    with (
        patch("agent_hq.daemon.get_all_panes", side_effect=tick_side_effect or (lambda: [])),
        patch("agent_hq.daemon.discover_agents", return_value=[]),
        patch("agent_hq.daemon.enrich_all_agents", return_value=[]),
        patch("agent_hq.daemon.time.sleep", side_effect=fake_sleep),
    ):
        daemon.run_poll_loop(interval=0.01)


def test_daemon_started_event_and_role():
    with (
        patch("agent_hq.daemon.eventlog.set_process_role") as set_role,
        patch("agent_hq.daemon.eventlog.emit") as emit,
        patch("agent_hq.daemon.eventlog.flush"),
    ):
        _run_loop_ticks(1)
    set_role.assert_called_once_with("daemon")
    assert emit.call_args_list[0][0][:2] == ("info", "daemon started")


def test_poll_loop_failing_emits_error_at_threshold():
    with (
        patch("agent_hq.daemon.eventlog.set_process_role"),
        patch("agent_hq.daemon.eventlog.emit") as emit,
        patch("agent_hq.daemon.eventlog.flush"),
    ):
        _run_loop_ticks(4, tick_side_effect=RuntimeError("tmux exploded"))
    failing = [c for c in emit.call_args_list if c[0][1] == "daemon poll loop failing"]
    # threshold is consecutive_errors >= 3: ticks 3 and 4 qualify (dedupe
    # is eventlog's job, not the loop's)
    assert len(failing) == 2
    level, _message = failing[0][0][:2]
    assert level == "error"
    extra = failing[0][0][2]
    assert extra["consecutive_errors"] == 3
    assert "tmux exploded" in extra["error"]
    assert failing[0][1] == {"min_interval_s": 900}


def test_poll_loop_flushes_periodically(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock["now"])
    with (
        patch("agent_hq.daemon.eventlog.set_process_role"),
        patch("agent_hq.daemon.eventlog.emit"),
        patch("agent_hq.daemon.eventlog.flush") as flush,
    ):

        def advance():
            clock["now"] += 200.0
            return []

        _run_loop_ticks(4, tick_side_effect=advance)
    # 4 ticks x 200s advance crosses the 300s flush interval at least twice.
    assert flush.call_count >= 2


def test_orphan_kill_emits_info():
    with (
        patch("agent_hq.daemon.find_orphan_daemon_pids", return_value=[111, 222]),
        patch("agent_hq.daemon.os.kill"),
        patch("agent_hq.daemon._process_alive", return_value=False),
        patch("agent_hq.daemon.eventlog.emit") as emit,
    ):
        daemon.kill_orphan_daemons(silent=True)
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("info", "orphan daemons killed")
    assert extra == {"count": 2, "pids": [111, 222]}
