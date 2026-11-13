"""Hook/restart/service/snapshot unified-logger events (#231)."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

from agent_hq import restart, snapshots


def _invoke_hook_with_stop_event(monkeypatch, hooks):
    """Drive handle_hook_event with a claude Stop event and a valid
    $TMUX_PANE, mirroring tests/test_hooks.py's TestHandleHookEvent
    stdin/env setup. The state dir is already redirected into pytest
    tmp by the suite's autouse isolation fixture (tests/conftest.py)."""
    stdin_data = json.dumps({"hook_event_name": "Stop", "session_id": "s1"})
    monkeypatch.setattr("sys.stdin", StringIO(stdin_data))
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setattr("sys.stdout", StringIO())
    hooks.handle_hook_event()


def test_hook_state_write_failure_emits_error(monkeypatch, tmp_path):
    """Drive handle_hook_event's state-write OSError path; the hook role
    must be set (queue-only delivery) and the event emitted."""
    from agent_hq import hooks

    # Model the stdin/env setup on tests/test_hooks.py's existing
    # state-write tests (they feed hook_input via monkeypatched stdin);
    # make _atomic_write raise OSError.
    with (
        patch("agent_hq.hooks._atomic_write", side_effect=OSError("disk full")),
        patch("agent_hq.hooks.eventlog.emit") as emit,
        patch("agent_hq.hooks.eventlog.set_process_role") as set_role,
    ):
        _invoke_hook_with_stop_event(monkeypatch, hooks)  # helper mirroring test_hooks.py
    set_role.assert_called_once_with("hook")
    calls = [c for c in emit.call_args_list if c[0][1] == "hook state write failed"]
    assert len(calls) == 1
    assert calls[0][0][0] == "error"
    assert calls[0][0][2]["kind"] == "state"
    assert "disk full" in calls[0][0][2]["error"]


_CODEX_THREAD = "019f0000-0000-7000-8000-00000000aaaa"


def _write_codex_tui_rollout(tmp_path, thread_id=_CODEX_THREAD):
    """A coherent interactive-TUI rollout head, as required by
    ``_codex_event_owns_pane`` before a SessionStart identity write is
    attempted. Modeled on tests/test_hooks.py's _write_tui_rollout."""
    rollout = tmp_path / f"rollout-{thread_id}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": "/work/tree",
            "originator": "codex-tui",
            "thread_source": "user",
        },
    }
    rollout.write_text(json.dumps(meta) + "\n")
    return rollout


def _invoke_codex_session_start(monkeypatch, hooks, tmp_path):
    """Drive handle_hook_event with a coherent codex SessionStart event so
    it reaches the identity-write try block, mirroring
    tests/test_hooks.py's TestCodexHookWrites._run_codex."""
    rollout = _write_codex_tui_rollout(tmp_path)
    payload = {
        "session_id": _CODEX_THREAD,
        "transcript_path": str(rollout),
        "cwd": "/work/tree",
        "model": "gpt-5.6-sol",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr("sys.stdout", StringIO())
    monkeypatch.setattr("sys.argv", ["agent-hq-hook", "--harness", "codex"])
    hooks.handle_hook_event()


def test_hook_identity_write_failure_emits_error(monkeypatch, tmp_path):
    """Drive handle_hook_event's codex SessionStart identity-write OSError
    path (kind="identity") — distinct from the state-write path
    (kind="state") covered by test_hook_state_write_failure_emits_error."""
    from agent_hq import hooks

    with (
        patch("agent_hq.hooks._atomic_write", side_effect=OSError("disk full")),
        patch("agent_hq.hooks.eventlog.emit") as emit,
        patch("agent_hq.hooks.eventlog.set_process_role") as set_role,
    ):
        _invoke_codex_session_start(monkeypatch, hooks, tmp_path)
    set_role.assert_called_once_with("hook")
    calls = [c for c in emit.call_args_list if c[0][1] == "hook state write failed"]
    assert len(calls) == 1
    assert calls[0][0][0] == "error"
    assert calls[0][0][2]["kind"] == "identity"
    assert calls[0][0][2]["harness"] == "codex"
    assert "disk full" in calls[0][0][2]["error"]


def test_kickstart_spawn_failure_emits_error():
    with (
        patch("agent_hq.restart.subprocess.Popen", side_effect=OSError("no launchctl")),
        patch("agent_hq.restart.eventlog.emit") as emit,
        patch("agent_hq.restart.service._domain_target", return_value="gui/501/x"),
    ):
        restart.kick_web_service()
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("error", "web service restart step failed")
    assert extra["step"] == "kickstart web service"


def test_failed_restart_step_emits_error():
    from agent_hq.restart import RestartStep
    from agent_hq.web.routes.system import _report_failed_steps

    with patch("agent_hq.web.routes.system.eventlog.emit") as emit:
        _report_failed_steps(
            [RestartStep("daemon stop", True, ""), RestartStep("daemon start", False, "exit 1")]
        )
    emit.assert_called_once()
    extra = emit.call_args[0][2]
    assert extra == {"step": "daemon start", "detail": "exit 1"}


def test_snapshot_append_failure_emits_warn(tmp_path):
    store = snapshots.SnapshotStore(path=tmp_path / "snaps.jsonl")
    agent = MagicMock()
    with (
        patch.object(store, "_read_or_raise", side_effect=OSError("disk full")),
        patch("agent_hq.snapshots._record_agents", return_value=[{"tool": "claude"}]),
        patch("agent_hq.snapshots.eventlog.emit") as emit,
    ):
        assert store.append_if_changed([agent], timestamp=1.0) is False
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "snapshot append failed")
    assert "disk full" in extra["error"]
    assert emit.call_args[1] == {"min_interval_s": 900}


def test_service_error_emits_from_cli(monkeypatch):
    from agent_hq import cli, service

    monkeypatch.setattr("sys.argv", ["agent-hq", "web", "service", "status"])
    with (
        patch(
            "agent_hq.service.service_status", side_effect=service.ServiceError("bootstrap failed")
        ),
        patch("agent_hq.cli.eventlog.emit") as emit,
    ):
        code = cli.main()
    assert code == 1
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("error", "launchctl service operation failed")
    assert extra == {"command": "status", "error": "bootstrap failed"}
