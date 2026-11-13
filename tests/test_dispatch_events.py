"""Dispatch-failure unified-logger events (#231).

Instrumentation lives inside dispatch.py (not per-consumer) so web, TUI,
and MCP dispatches are all covered by one site. Failures short-circuit
before any tmux call, so no tmux mocking is needed here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_hq.config import Config, DispatchConfig
from agent_hq.dispatch import dispatch_via_claude
from agent_hq.models import Issue, Priority


def _issue(repo="ben-ralston/agent-hq", number=231):
    return Issue(repo=repo, number=number, title="t", status="Todo", priority=Priority.NEED)


def _config():
    # No repos configured -> "No config for repo" failure path.
    return Config(
        repos={},
        projects=[],
        worktree_root=Path("/tmp/worktrees"),
        dispatch=DispatchConfig(),
    )


def test_dispatch_failure_emits_error_event():
    with patch("agent_hq.dispatch.eventlog.emit") as emit:
        result = dispatch_via_claude([[_issue()]], _config())
    assert result.success is False
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("error", "dispatch failed")
    assert extra["issues"] == ["ben-ralston/agent-hq#231"]
    assert extra["error"] == result.error
    assert extra["launched"] is False


def test_no_issues_is_benign_no_event():
    with patch("agent_hq.dispatch.eventlog.emit") as emit:
        result = dispatch_via_claude([], _config())
    assert result.success is False
    emit.assert_not_called()


def test_dispatch_crash_emits_and_reraises():
    with (
        patch("agent_hq.dispatch._dispatch_via_claude", side_effect=RuntimeError("boom")),
        patch("agent_hq.dispatch.eventlog.emit") as emit,
    ):
        try:
            dispatch_via_claude([[_issue()]], _config())
            raised = False
        except RuntimeError:
            raised = True
    assert raised
    extra = emit.call_args[0][2]
    assert extra["crashed"] is True
    assert "boom" in extra["error"]

