"""MCP server/client + capabilities unified-logger events (#231)."""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_hq import mcp_server
from agent_hq.capabilities import CLAUDE_ONLY, _probe_uncached
from agent_hq.config import ConfigError


def test_tool_crash_emits_error_and_returns_string():
    with (
        patch(
            "agent_hq.mcp_server._discover_agents_context", side_effect=RuntimeError("tmux gone")
        ),
        patch("agent_hq.mcp_server.eventlog.emit") as emit,
    ):
        result = mcp_server.list_agents()
    assert isinstance(result, str)
    assert result.startswith("Error:")
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("error", "mcp server tool failed")
    assert extra["tool"] == "list_agents"
    assert "tmux gone" in extra["error"]


def test_dispatch_crash_is_reported_once_not_twice():
    """#237: dispatch_via_claude reports its own crash before re-raising, so
    _guarded must not emit a second error event (two sentinel fingerprints,
    two auto-filed issues for one incident)."""
    from agent_hq import dispatch

    class _Result:
        success = True
        outcome = None
        preview = "p"

        def execute(self):
            dispatch.dispatch_via_claude([], None)
            return True

    with (
        patch("agent_hq.mcp_server._issues_context", return_value=object()),
        patch("agent_hq.mcp_server.tools.dispatch_issues", return_value=_Result()),
        patch("agent_hq.dispatch._dispatch_via_claude", side_effect=RuntimeError("boom")),
        patch("agent_hq.eventlog.emit") as emit,
    ):
        result = mcp_server.dispatch_issues(["o/r#1"])
    assert result.startswith("Error: dispatch_issues failed:")
    errors = [c for c in emit.call_args_list if c[0][0] == "error"]
    assert [c[0][1] for c in errors] == ["dispatch failed"]


def test_deduped_dispatch_crash_falls_back_to_the_guard_event():
    """Review #237: when the crash's own 'dispatch failed' emit is swallowed
    by the 60 s dedupe, the exception is NOT marked and _guarded reports it."""
    from agent_hq import dispatch, eventlog

    class _Result:
        success = True
        outcome = None
        preview = "p"

        def execute(self):
            dispatch.dispatch_via_claude([], None)
            return True

    sent = []
    eventlog._last_emit.clear()
    with (
        patch("agent_hq.logclient.log", side_effect=lambda *a: sent.append(a[1:3]) or True),
        patch.object(eventlog, "_role", "cli"),
    ):
        eventlog.emit("error", "dispatch failed", {"earlier": "ordinary failure"})
        with (
            patch("agent_hq.mcp_server._issues_context", return_value=object()),
            patch("agent_hq.mcp_server.tools.dispatch_issues", return_value=_Result()),
            patch("agent_hq.dispatch._dispatch_via_claude", side_effect=RuntimeError("boom")),
        ):
            mcp_server.dispatch_issues(["o/r#1"])
    eventlog._last_emit.clear()
    assert sent == [("error", "dispatch failed"), ("error", "mcp server tool failed")]


def test_reported_config_error_is_not_emitted_again():
    from agent_hq import eventlog

    err = ConfigError("bad yaml")
    eventlog.mark_reported(err)
    with (
        patch("agent_hq.mcp_server._issues_context", side_effect=err),
        patch("agent_hq.mcp_server.eventlog.emit") as emit,
    ):
        result = mcp_server.list_issues()
    assert result.startswith("Error: config failed to load")
    emit.assert_not_called()


def test_unreported_crash_still_emits_after_a_reported_one():
    """The skip is per exception: an ordinary crash still reaches the logger."""
    with (
        patch("agent_hq.mcp_server._issues_context", side_effect=RuntimeError("fresh")),
        patch("agent_hq.mcp_server.eventlog.emit") as emit,
    ):
        mcp_server.dispatch_issues(["o/r#1"])
    assert emit.call_args[0][:2] == ("error", "mcp server tool failed")


def test_config_error_emits_warn_not_error():
    with (
        patch("agent_hq.mcp_server._issues_context", side_effect=ConfigError("bad yaml")),
        patch("agent_hq.mcp_server.eventlog.emit") as emit,
    ):
        result = mcp_server.list_issues()
    assert result.startswith("Error: config failed to load")
    assert emit.call_args[0][:2] == ("warn", "config load failed")


def test_capabilities_probe_failure_emits_warn(tmp_path):
    missing = tmp_path / "launch.sh"
    with patch("agent_hq.capabilities.eventlog.emit") as emit:
        caps = _probe_uncached(missing)
    assert caps is CLAUDE_ONLY
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "capabilities probe degraded to claude-only")
    assert extra["launcher"] == str(missing)
    assert emit.call_args[1] == {"min_interval_s": 3600}


def test_capabilities_nonzero_exit_emits_warn(tmp_path):
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="", stderr="")
    with (
        patch("agent_hq.capabilities.subprocess.run", return_value=completed),
        patch("agent_hq.capabilities.eventlog.emit") as emit,
    ):
        caps = _probe_uncached(Path("/x/launch.sh"))
    assert caps is CLAUDE_ONLY
    assert "exit 3" in emit.call_args[0][2]["reason"]
    assert emit.call_args[0][:2] == ("warn", "capabilities probe degraded to claude-only")
    assert emit.call_args[1] == {"min_interval_s": 3600}


def test_capabilities_invalid_contract_emits_warn():
    """The third degradation path: exit 0 but the JSON contract doesn't parse."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"contract_version": 999}', stderr=""
    )
    with (
        patch("agent_hq.capabilities.subprocess.run", return_value=completed),
        patch("agent_hq.capabilities.eventlog.emit") as emit,
    ):
        caps = _probe_uncached(Path("/x/launch.sh"))
    assert caps is CLAUDE_ONLY
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "capabilities probe degraded to claude-only")
    assert extra == {"launcher": "/x/launch.sh", "reason": "invalid contract"}
    assert emit.call_args[1] == {"min_interval_s": 3600}


def _tool_decorated_functions() -> dict[str, list[str]]:
    """Map every ``@mcp.tool()``-decorated function to its decorator names."""
    tree = ast.parse(inspect.getsource(mcp_server))
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        names = []
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            names.append(ast.unparse(target))
        if "mcp.tool" in names:
            found[node.name] = names
    return found


def test_every_registered_tool_is_guarded():
    """No tool may be registered without the #231 error boundary.

    Source-level: every ``@mcp.tool()`` also carries ``@_guarded``, and
    ``_guarded`` sits UNDER ``mcp.tool`` so FastMCP registers the guarded
    callable rather than the raw one.
    """
    decorated = _tool_decorated_functions()
    assert len(decorated) == 12, f"expected 12 MCP tools, found {sorted(decorated)}"
    for name, decorators in decorated.items():
        assert "_guarded" in decorators, f"{name} is registered without @_guarded"
        assert decorators.index("mcp.tool") < decorators.index("_guarded"), (
            f"{name}: @_guarded must sit under @mcp.tool() so FastMCP registers the guarded fn"
        )


def test_registry_holds_guarded_callables_with_intact_schemas():
    """Runtime counterpart: FastMCP holds the wrapper, signatures survive."""
    tools = mcp_server.mcp._tool_manager.list_tools()
    assert {t.name for t in tools} == set(_tool_decorated_functions())
    for tool in tools:
        assert getattr(tool.fn, "__wrapped__", None) is not None, (
            f"{tool.name} is registered unguarded"
        )
    by_name = {t.name: t for t in tools}
    # functools.wraps must not have flattened the params into *args/**kwargs.
    assert set(by_name["list_agents"].parameters["properties"]) == {
        "repo",
        "state",
        "agent_type",
        "format",
    }


def test_guarded_tool_never_raises_into_fastmcp():
    """Every tool returns an "Error: ..." string rather than raising."""
    with (
        patch("agent_hq.mcp_server._issues_context", side_effect=RuntimeError("boom")),
        patch("agent_hq.mcp_server.eventlog.emit"),
    ):
        result = mcp_server.set_status("repo#1", "Todo")
    assert isinstance(result, str)
    assert result.startswith("Error: set_status failed:")


def test_triage_tool_crash_is_guarded():
    with patch.object(mcp_server, "_issues_context", side_effect=RuntimeError("boom")):
        assert mcp_server.set_triage("owner/repo#1", value=3).startswith("Error:")


def test_registered_tool_names():
    assert {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()} == {
        "list_agents",
        "jump_to_agent",
        "kill_agent",
        "mute_agent",
        "unmute_agent",
        "list_issues",
        "dispatch_issues",
        "open_chat_session",
        "set_status",
        "refresh_issues",
        "set_triage",
        "confirm_triage",
    }
