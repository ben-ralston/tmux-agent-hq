"""Tests for tools module: agent resolution, tool execution, and individual tools."""

from unittest.mock import MagicMock, patch

import pytest

from agent_hq.config import Config, DispatchConfig, HarnessDefaults, ProjectConfig
from agent_hq.models import Agent, AgentState, AgentType, Issue, Priority
from agent_hq.tools import (
    ToolContext,
    _owner_for_project,
    _resolve_agent,
    dispatch_issues,
    execute_tool_call,
    kill_agent,
    list_agents,
    list_issues,
    set_status,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    pane_id="%1",
    repo_name="agent-hq",
    agent_type=AgentType.CLAUDE,
    state=AgentState.IDLE,
):
    return Agent(
        pane_id=pane_id,
        session_name="test",
        window_index=0,
        pane_index=0,
        agent_type=agent_type,
        cwd="/tmp",
        repo_name=repo_name,
        state=state,
    )


def _make_issue(number=1, repo="owner/repo", priority=Priority.NEED, status="Todo"):
    return Issue(
        number=number,
        title=f"Issue {number}",
        repo=repo,
        status=status,
        priority=priority,
    )


def _make_context(agents=None, issues=None, selected_keys=None, config=None, triage_store=None):
    return ToolContext(
        config=config,
        triage_store=triage_store,
        github_cache=None,
        issues=issues or [],
        agents=agents or [],
        active_screen="agents",
        selected_keys=selected_keys or set(),
        inactive_pane_ids=set(),
    )


def _make_dispatch_config(default_harness="claude", harnesses=None):
    """A real Config with dispatch settings, for harness/effort resolution tests.

    Deliberately a real Config (not a MagicMock) -- resolve_dispatch_selection
    reads config.dispatch.harnesses[...] and compares the result against
    VALID_EFFORTS, and a MagicMock's auto-generated attributes are truthy but
    aren't strings, which corrupts that comparison in ways a mock would mask.
    """
    from pathlib import Path

    return Config(
        repos={},
        projects=[],
        worktree_root=Path("/tmp/worktrees"),
        dispatch=DispatchConfig(default_harness=default_harness, harnesses=harnesses or {}),
    )


# ---------------------------------------------------------------------------
# _resolve_agent
# ---------------------------------------------------------------------------


class TestResolveAgent:
    """Tests for fuzzy agent resolution."""

    def test_exact_pane_id_match(self):
        agents = [_make_agent(pane_id="%5"), _make_agent(pane_id="%6")]
        result = _resolve_agent("%5", agents)
        assert len(result) == 1
        assert result[0].pane_id == "%5"

    def test_repo_name_match(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq"),
            _make_agent(pane_id="%2", repo_name="other-repo"),
        ]
        result = _resolve_agent("agent-hq", agents)
        assert len(result) == 1
        assert result[0].pane_id == "%1"

    def test_type_plus_repo_match(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq", agent_type=AgentType.CLAUDE),
            _make_agent(pane_id="%2", repo_name="agent-hq", agent_type=AgentType.CODEX),
        ]
        result = _resolve_agent("claude in agent-hq", agents)
        assert len(result) == 1
        assert result[0].pane_id == "%1"

    def test_state_plus_repo_match(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq", state=AgentState.IDLE),
            _make_agent(pane_id="%2", repo_name="agent-hq", state=AgentState.RUNNING),
        ]
        result = _resolve_agent("idle in agent-hq", agents)
        assert len(result) == 1
        assert result[0].pane_id == "%1"

    def test_ambiguous_multiple_matches(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq"),
            _make_agent(pane_id="%2", repo_name="agent-hq-v2"),
        ]
        result = _resolve_agent("agent-hq", agents)
        assert len(result) == 2

    def test_no_match(self):
        agents = [_make_agent(pane_id="%1", repo_name="agent-hq")]
        result = _resolve_agent("nonexistent", agents)
        assert len(result) == 0

    def test_empty_identifier(self):
        agents = [_make_agent()]
        result = _resolve_agent("", agents)
        assert len(result) == 0

    def test_empty_agents(self):
        result = _resolve_agent("%1", [])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# execute_tool_call
# ---------------------------------------------------------------------------


class TestExecuteToolCall:
    """Tests for the tool call dispatcher."""

    def test_known_tool_dispatch(self):
        ctx = _make_context(issues=[_make_issue()])
        result = execute_tool_call("list_issues", {}, ctx)
        assert result.success is True
        assert "Found 1 issue(s)" in result.preview

    def test_unknown_tool_error(self):
        ctx = _make_context()
        result = execute_tool_call("does_not_exist", {}, ctx)
        assert result.success is False
        assert "Unknown tool: does_not_exist" in result.error


# ---------------------------------------------------------------------------
# dispatch_issues
# ---------------------------------------------------------------------------


class TestDispatchIssuesTool:
    """Tests for the dispatch_issues tool."""

    def test_same_repo_validation_passes(self):
        issues = [_make_issue(1, "owner/repo"), _make_issue(2, "owner/repo")]
        ctx = _make_context(issues=issues)
        result = dispatch_issues(ctx, issue_keys=["owner/repo#1", "owner/repo#2"])
        assert result.success is True
        assert "#1" in result.preview
        assert "#2" in result.preview

    def test_cross_repo_validation_fails(self):
        issues = [_make_issue(1, "owner/repo-a"), _make_issue(2, "owner/repo-b")]
        ctx = _make_context(issues=issues)
        result = dispatch_issues(ctx, issue_keys=["owner/repo-a#1", "owner/repo-b#2"])
        assert result.success is False
        assert "multiple repos" in result.error.lower()

    def test_empty_keys_uses_selected_keys(self):
        issues = [_make_issue(1, "owner/repo")]
        ctx = _make_context(issues=issues, selected_keys={"owner/repo#1"})
        result = dispatch_issues(ctx, issue_keys=[])
        assert result.success is True
        assert "#1" in result.preview

    def test_no_keys_and_no_selection_errors(self):
        ctx = _make_context()
        result = dispatch_issues(ctx)
        assert result.success is False
        assert "No issues specified" in result.error

    def test_preview_text_format(self):
        issues = [_make_issue(42, "owner/my-repo")]
        ctx = _make_context(issues=issues)
        result = dispatch_issues(ctx, issue_keys=["owner/my-repo#42"])
        assert result.success is True
        assert "Will send /worktree-create #42 to Claude in my-repo session" in result.preview

    def test_missing_issue_key_errors(self):
        ctx = _make_context(issues=[_make_issue(1, "owner/repo")])
        result = dispatch_issues(ctx, issue_keys=["owner/repo#999"])
        assert result.success is False
        assert "not found" in result.error.lower()

    @patch("agent_hq.tools.supports_harness", return_value=True)
    @patch("agent_hq.tools.probe_launcher_capabilities")
    @patch("agent_hq.dispatch.dispatch_via_claude")
    def test_dispatch_issues_threads_harness(self, mock_dispatch, _probe, _supports):
        from agent_hq.tools import ToolResult as _TR

        mock_dispatch.return_value = MagicMock(success=True)
        issues = [_make_issue(1, "owner/repo")]
        config = _make_dispatch_config()
        ctx = _make_context(issues=issues, config=config)

        result = dispatch_issues(
            ctx, issue_keys=["owner/repo#1"], harness="codex", model="o3", effort="high"
        )
        assert result.success is True
        assert "--harness codex" in result.preview
        assert "--model o3" in result.preview
        assert "--effort high" in result.preview

        ok = result.execute()
        assert ok is True
        mock_dispatch.assert_called_once_with(
            [issues], config, harness="codex", model="o3", effort="high"
        )
        assert isinstance(result, _TR)

    @patch("agent_hq.tools.probe_launcher_capabilities")
    @patch("agent_hq.dispatch.dispatch_via_claude")
    def test_dispatch_issues_rejects_unadvertised_codex(self, mock_dispatch, mock_probe):
        from agent_hq.capabilities import CLAUDE_ONLY

        mock_probe.return_value = CLAUDE_ONLY
        issues = [_make_issue(1, "owner/repo")]
        config = _make_dispatch_config()
        ctx = _make_context(issues=issues, config=config)

        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"], harness="codex")
        assert result.success is False
        assert "codex" in result.error.lower()
        mock_dispatch.assert_not_called()

    def test_dispatch_issues_rejects_invalid_harness(self):
        issues = [_make_issue(1, "owner/repo")]
        ctx = _make_context(issues=issues)
        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"], harness="not-a-harness")
        assert result.success is False
        assert "harness" in result.error.lower()

    def test_dispatch_issues_rejects_invalid_effort(self):
        issues = [_make_issue(1, "owner/repo")]
        ctx = _make_context(issues=issues)
        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"], effort="ludicrous")
        assert result.success is False
        assert "effort" in result.error.lower()

    @patch("agent_hq.tools.supports_harness", return_value=True)
    @patch("agent_hq.tools.probe_launcher_capabilities")
    @patch("agent_hq.dispatch.dispatch_via_claude")
    def test_preview_reflects_config_default_harness(self, mock_dispatch, _probe, _supports):
        """No explicit harness/model/effort -- the config's default_harness
        and per-harness defaults must still show up in the preview, matching
        what dispatch_via_claude will actually resolve and send (#205 Stage
        1B preview-drift fix)."""
        mock_dispatch.return_value = MagicMock(success=True)
        issues = [_make_issue(1, "owner/repo")]
        config = _make_dispatch_config(
            default_harness="codex", harnesses={"codex": HarnessDefaults(effort="high")}
        )
        ctx = _make_context(issues=issues, config=config)

        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"])
        assert result.success is True
        assert "--harness codex" in result.preview
        assert "--effort high" in result.preview

        ok = result.execute()
        assert ok is True
        # The ORIGINAL (unresolved) kwargs are forwarded -- dispatch_via_claude
        # re-resolves identically via the same helper, so there's no fork.
        mock_dispatch.assert_called_once_with(
            [issues], config, harness=None, model=None, effort=None
        )

    @patch("agent_hq.tools.supports_harness", return_value=True)
    @patch("agent_hq.tools.probe_launcher_capabilities")
    @patch("agent_hq.dispatch.dispatch_via_claude")
    def test_execute_failure_surfaces_dispatch_error_in_outcome(
        self, mock_dispatch, _probe, _supports
    ):
        """Fix 8 (multi-review wave): dispatch_issues's _execute() must not
        discard DispatchResult.error -- it belongs in result.outcome
        (mirroring kill_agent's pattern) so the MCP reply can surface the
        real failure reason instead of a generic 'Failed: <preview>'."""
        mock_dispatch.return_value = MagicMock(
            success=False, error="worktree already exists at /tmp/x"
        )
        issues = [_make_issue(1, "owner/repo")]
        config = _make_dispatch_config()
        ctx = _make_context(issues=issues, config=config)

        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"])
        assert result.success is True  # preview succeeded; execute is what fails
        ok = result.execute()
        assert ok is False
        assert "worktree already exists at /tmp/x" in result.outcome

    @patch("agent_hq.tools.probe_launcher_capabilities")
    @patch("agent_hq.dispatch.dispatch_via_claude")
    def test_rejects_config_default_codex_when_unadvertised(self, mock_dispatch, mock_probe):
        """Config defaults to codex but the installed launcher only advertises
        claude -- the capability gate must apply to the RESOLVED harness, not
        just an explicitly-passed one."""
        from agent_hq.capabilities import CLAUDE_ONLY

        mock_probe.return_value = CLAUDE_ONLY
        issues = [_make_issue(1, "owner/repo")]
        config = _make_dispatch_config(
            default_harness="codex", harnesses={"codex": HarnessDefaults(effort="high")}
        )
        ctx = _make_context(issues=issues, config=config)

        result = dispatch_issues(ctx, issue_keys=["owner/repo#1"])
        assert result.success is False
        assert "codex" in result.error.lower()
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# kill_agent
# ---------------------------------------------------------------------------


class TestKillAgentTool:
    """Tests for the kill_agent tool."""

    def test_single_match_preview(self):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5")
        assert result.success is True
        assert "%5" in result.preview
        assert "kill" in result.preview.lower()

    def test_ambiguous_match_error(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq"),
            _make_agent(pane_id="%2", repo_name="agent-hq-v2"),
        ]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="agent-hq")
        assert result.success is False
        assert "ambiguous" in result.error.lower()

    def test_no_match_error(self):
        ctx = _make_context(agents=[_make_agent()])
        result = kill_agent(ctx, identifier="nonexistent")
        assert result.success is False
        assert "No agent found" in result.error

    @patch("agent_hq.actions.verify_agent_killed", return_value=(True, AgentType.UNKNOWN))
    @patch("agent_hq.actions.kill_agent", return_value=True)
    def test_execute_calls_actions(self, mock_kill, _mock_verify):
        agents = [_make_agent(pane_id="%5")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.success is True
        assert result.execute() is True
        mock_kill.assert_called_once_with("%5")


class TestKillAgentConfirm:
    """Tests for the kill_agent confirm handshake and verified outcome (#185)."""

    @patch("agent_hq.actions.kill_agent")
    def test_no_confirm_previews_without_executing(self, mock_kill):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="agent-hq")
        assert result.success is True
        assert "Would kill" in result.preview
        assert "%5" in result.preview
        assert "Not executed" in result.preview
        assert 'kill_agent(identifier="%5", confirm=true)' in result.preview
        assert result.execute() is True
        mock_kill.assert_not_called()

    def test_preview_labels_muted_target(self):
        agents = [_make_agent(pane_id="%5")]
        ctx = _make_context(agents=agents)
        ctx.inactive_pane_ids = {"%5"}
        result = kill_agent(ctx, identifier="%5")
        assert result.success is True
        assert "muted" in result.preview

    def test_preview_omits_muted_label_when_visible(self):
        agents = [_make_agent(pane_id="%5")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5")
        assert "muted" not in result.preview

    def test_confirm_requires_exact_pane_id(self):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="agent-hq", confirm=True)
        assert result.success is False
        assert "exact pane id" in result.error

    @patch("agent_hq.actions.verify_agent_killed", return_value=(True, AgentType.UNKNOWN))
    @patch("agent_hq.actions.kill_agent", return_value=True)
    def test_confirm_kill_verified_dead(self, mock_kill, mock_verify):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.success is True
        assert result.execute() is True
        mock_kill.assert_called_once_with("%5")
        mock_verify.assert_called_once_with("%5")
        assert "Killed" in result.outcome
        assert "%5" in result.outcome
        assert "verified" in result.outcome

    @patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.WAITING)
    @patch("agent_hq.actions.verify_agent_killed", return_value=(False, AgentType.CLAUDE))
    @patch("agent_hq.actions.kill_agent", return_value=True)
    def test_confirm_kill_still_alive(self, _mock_kill, _mock_verify, _mock_state):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.execute() is False
        assert "still running" in result.outcome
        assert "waiting" in result.outcome

    @patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.RUNNING)
    @patch("agent_hq.actions.verify_agent_killed", return_value=(False, AgentType.CLAUDE))
    @patch("agent_hq.actions.kill_agent", return_value=False)
    def test_confirm_send_keys_failure_agent_alive(self, _mock_kill, _mock_verify, _mock_state):
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.execute() is False
        assert "Ctrl-C" in result.outcome
        assert "%5" in result.outcome

    @patch("agent_hq.actions.verify_agent_killed", return_value=(False, AgentType.UNKNOWN))
    @patch("agent_hq.actions.kill_agent", return_value=True)
    def test_unverifiable_outcome_reported_honestly(self, _mock_kill, _mock_verify):
        """When verification itself failed (remaining type UNKNOWN), report
        'could not verify' rather than claiming the agent is still running."""
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.execute() is False
        assert "could not verify" in result.outcome

    @patch("agent_hq.actions.verify_agent_killed", return_value=(True, AgentType.UNKNOWN))
    @patch("agent_hq.actions.kill_agent", return_value=False)
    def test_send_keys_failure_but_agent_died_is_success(self, _mock_kill, _mock_verify):
        """First Ctrl-C can kill an agent whose pane then closes, making the
        second send-keys fail -- verification, not send-keys exit status,
        decides the outcome."""
        agents = [_make_agent(pane_id="%5", repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = kill_agent(ctx, identifier="%5", confirm=True)
        assert result.execute() is True
        assert "Killed" in result.outcome
        assert "verified" in result.outcome

    def test_outcome_empty_for_other_tools(self):
        ctx = _make_context(agents=[_make_agent()])
        result = list_agents(ctx)
        assert result.outcome == ""


# ---------------------------------------------------------------------------
# list_issues
# ---------------------------------------------------------------------------


class TestListIssuesTool:
    """Tests for the list_issues tool."""

    def test_filter_by_repo(self):
        issues = [
            _make_issue(1, "owner/agent-hq"),
            _make_issue(2, "owner/other-repo"),
        ]
        ctx = _make_context(issues=issues)
        result = list_issues(ctx, repo="agent-hq")
        assert result.success is True
        assert "1 issue(s)" in result.preview
        assert "agent-hq#1" in result.preview

    def test_no_filter_returns_all(self):
        issues = [_make_issue(1), _make_issue(2), _make_issue(3)]
        ctx = _make_context(issues=issues)
        result = list_issues(ctx)
        assert result.success is True
        assert "3 issue(s)" in result.preview

    def test_no_matches(self):
        issues = [_make_issue(1, "owner/repo")]
        ctx = _make_context(issues=issues)
        result = list_issues(ctx, repo="nonexistent")
        assert result.success is True
        assert "No issues match" in result.preview

    def test_filter_by_status(self):
        issues = [
            _make_issue(1, status="Todo"),
            _make_issue(2, status="In Progress"),
        ]
        ctx = _make_context(issues=issues)
        result = list_issues(ctx, status="Todo")
        assert result.success is True
        assert "1 issue(s)" in result.preview


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgentsTool:
    """Tests for the list_agents tool."""

    def test_filter_by_state(self):
        agents = [
            _make_agent(pane_id="%1", state=AgentState.IDLE),
            _make_agent(pane_id="%2", state=AgentState.RUNNING),
        ]
        ctx = _make_context(agents=agents)
        result = list_agents(ctx, state="idle")
        assert result.success is True
        assert "1 agent(s)" in result.preview
        assert "%1" in result.preview

    def test_filter_by_repo(self):
        agents = [
            _make_agent(pane_id="%1", repo_name="agent-hq"),
            _make_agent(pane_id="%2", repo_name="other-repo"),
        ]
        ctx = _make_context(agents=agents)
        result = list_agents(ctx, repo="agent-hq")
        assert result.success is True
        assert "1 agent(s)" in result.preview

    def test_filter_by_agent_type(self):
        agents = [
            _make_agent(pane_id="%1", agent_type=AgentType.CLAUDE),
            _make_agent(pane_id="%2", agent_type=AgentType.CODEX),
        ]
        ctx = _make_context(agents=agents)
        result = list_agents(ctx, agent_type="claude")
        assert result.success is True
        assert "1 agent(s)" in result.preview

    def test_no_filter_returns_all(self):
        agents = [_make_agent(pane_id="%1"), _make_agent(pane_id="%2")]
        ctx = _make_context(agents=agents)
        result = list_agents(ctx)
        assert result.success is True
        assert "2 agent(s)" in result.preview

    def test_no_matches(self):
        agents = [_make_agent(repo_name="agent-hq")]
        ctx = _make_context(agents=agents)
        result = list_agents(ctx, repo="nonexistent")
        assert result.success is True
        assert "No agents match" in result.preview

    def test_invalid_state_error(self):
        ctx = _make_context(agents=[_make_agent()])
        result = list_agents(ctx, state="InvalidState")
        assert result.success is False
        assert "Unknown state" in result.error

    def test_invalid_agent_type_error(self):
        ctx = _make_context(agents=[_make_agent()])
        result = list_agents(ctx, agent_type="InvalidType")
        assert result.success is False
        assert "Unknown agent type" in result.error


def _named_agent(pane_id="%1", *, native="busy", reason="", source="derived"):
    """#287: a claude agent with a registry name and a fresh live status."""
    from agent_hq.status_types import LiveStatus

    agent = _make_agent(pane_id=pane_id)
    agent.native_name = "agent-hq-c3"
    agent.native_name_source = source
    agent.claude_transcript_session_id = "sid-1"
    agent.branch = "feature/287-x"
    agent.worktree_name = "agent-hq.feature-287-x"
    state = {"busy": "running", "waiting": "waiting", "idle": "idle"}[native]
    agent.status_snapshot = LiveStatus(
        state, native, state.title(), reason, "claude", "sid-1", 4242, 1.0, 1
    )
    return agent


class TestListAgentsNativeIdentity:
    """#287: native session name + live status for @-addressing."""

    def test_text_shows_name_source_and_status(self):
        ctx = _make_context(agents=[_named_agent(native="busy")])
        result = list_agents(ctx)
        assert "@agent-hq-c3 (derived) · busy" in result.preview

    def test_text_shows_waiting_reason(self):
        agent = _named_agent(native="waiting", reason="approve Bash", source="user")
        result = list_agents(_make_context(agents=[agent]))
        assert "@agent-hq-c3 · waiting: approve Bash" in result.preview
        assert "(user)" not in result.preview

    def test_text_unchanged_without_name(self):
        result = list_agents(_make_context(agents=[_make_agent(pane_id="%1")]))
        line = result.preview.splitlines()[1]
        assert line.rstrip().endswith("claude  agent-hq")
        assert "@" not in line

    def test_json_fields(self):
        import json

        agent = _named_agent(native="waiting", reason="approve Bash")
        result = list_agents(_make_context(agents=[agent]), format="json")
        assert result.success is True
        [record] = json.loads(result.preview)
        assert record == {
            "pane_id": "%1",
            "agent_type": "claude",
            "state": "idle",
            "repo": "agent-hq",
            "branch": "feature/287-x",
            "worktree": "agent-hq.feature-287-x",
            "native_name": "agent-hq-c3",
            "native_name_source": "derived",
            "native_status": "waiting",
            "waiting_for": "approve Bash",
            "waiting_since": None,
            "session_id": "sid-1",
        }

    def test_json_nulls_for_codex_and_unknown_status(self):
        import json

        from agent_hq.status_types import unknown_status

        codex = _make_agent(pane_id="%2", agent_type=AgentType.CODEX)
        stale = _named_agent(pane_id="%3")
        stale.status_snapshot = unknown_status("expired", source="claude", session_id="sid-1")
        result = list_agents(_make_context(agents=[codex, stale]), format="JSON")
        by_pane = {r["pane_id"]: r for r in json.loads(result.preview)}
        assert by_pane["%2"]["native_name"] is None
        assert by_pane["%2"]["native_status"] is None
        assert by_pane["%2"]["session_id"] is None
        assert by_pane["%3"]["native_name"] == "agent-hq-c3"
        assert by_pane["%3"]["native_status"] is None
        assert by_pane["%3"]["waiting_for"] is None

    def test_json_empty_is_empty_list(self):
        result = list_agents(_make_context(agents=[]), format="json")
        assert result.preview == "[]"

    def test_invalid_format(self):
        result = list_agents(_make_context(agents=[_make_agent()]), format="yaml")
        assert result.success is False
        assert "Invalid format" in result.error

    def test_format_none_defaults_to_text(self):
        result = list_agents(_make_context(agents=[_make_agent()]), format=None)
        assert result.success is True
        assert "1 agent(s)" in result.preview

    def test_synthesized_waiting_reason_is_not_reported(self):
        """#287 review: the collector's fallback text is not native waitingFor."""
        import json

        from agent_hq.runtime_status import CLAUDE_WAITING_FALLBACK

        agent = _named_agent(native="waiting", reason=CLAUDE_WAITING_FALLBACK)
        ctx = _make_context(agents=[agent])
        [record] = json.loads(list_agents(ctx, format="json").preview)
        assert record["native_status"] == "waiting"
        assert record["waiting_for"] is None
        assert list_agents(ctx).preview.splitlines()[1].endswith("· waiting")


class TestListAgentsWaitingSince:
    """#415: how long a session has been waiting on the owner."""

    @staticmethod
    def _waiting(age_s, *, source="claude", reason="approve Bash", **kwargs):
        import time

        from agent_hq.status_types import LiveStatus

        agent = _make_agent(pane_id="%1", agent_type=AgentType(source), **kwargs)
        now = time.time()
        agent.state = AgentState.WAITING
        agent.status_snapshot = LiveStatus(
            "waiting",
            "waiting" if source == "claude" else "active",
            "Waiting",
            reason,
            source,
            "sid-1",
            42,
            now,
            1,
            now - age_s,
        )
        return agent

    def test_text_shows_the_wait_and_the_reason(self):
        agent = self._waiting(3 * 3600 + 12 * 60 + 5)
        line = list_agents(_make_context(agents=[agent])).preview.splitlines()[1]
        assert line.endswith("· waiting 3h12m: approve Bash")

    def test_text_shows_the_wait_for_codex_too(self):
        agent = self._waiting(300, source="codex", reason="Waiting for approval")
        line = list_agents(_make_context(agents=[agent])).preview.splitlines()[1]
        assert line.endswith("· waiting 5m")

    def test_json_has_an_iso_8601_time_with_an_offset(self):
        import json
        from datetime import datetime

        agent = self._waiting(120)
        [record] = json.loads(list_agents(_make_context(agents=[agent]), format="json").preview)
        parsed = datetime.fromisoformat(record["waiting_since"])
        assert parsed.tzinfo is not None
        assert parsed.timestamp() == pytest.approx(agent.status_snapshot.waiting_since, abs=1)

    def test_json_has_it_for_codex_too(self):
        import json

        agent = self._waiting(120, source="codex")
        [record] = json.loads(list_agents(_make_context(agents=[agent]), format="json").preview)
        assert record["native_status"] is None
        assert record["waiting_since"] is not None

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (-5, "<1m"),
            (0, "<1m"),
            (59, "<1m"),
            (60, "1m"),
            (59 * 60 + 59, "59m"),
            (3600, "1h0m"),
            (3 * 3600 + 12 * 60, "3h12m"),
            (24 * 3600 - 1, "23h59m"),
            (24 * 3600, "1d0h"),
            (2 * 86400 + 3 * 3600 + 59 * 60, "2d3h"),
        ],
    )
    def test_format_waited(self, seconds, expected):
        from agent_hq.tools import format_waited

        assert format_waited(1000.0, now=1000.0 + seconds) == expected


# ---------------------------------------------------------------------------
# _owner_for_project
# ---------------------------------------------------------------------------


class TestOwnerForProject:
    """Tests for project owner resolution helper."""

    def test_matches_project_number(self):
        from pathlib import Path

        config = Config(
            repos={},
            projects=[
                ProjectConfig(name="My Project", number=12, owner="my-org"),
                ProjectConfig(name="Other", number=99, owner="@me"),
            ],
            worktree_root=Path("/tmp"),
        )
        assert _owner_for_project(config, 12) == "my-org"
        assert _owner_for_project(config, 99) == "@me"

    def test_no_match_falls_back_to_at_me(self):
        from pathlib import Path

        config = Config(
            repos={},
            projects=[ProjectConfig(name="X", number=1, owner="org")],
            worktree_root=Path("/tmp"),
        )
        assert _owner_for_project(config, 999) == "@me"

    def test_none_config_falls_back_to_at_me(self):
        assert _owner_for_project(None, 12) == "@me"


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


class TestSetStatusTool:
    """Tests for the set_status tool."""

    def _make_config_with_project(self, project_number=12, owner="my-org"):
        from pathlib import Path

        return Config(
            repos={},
            projects=[ProjectConfig(name="Proj", number=project_number, owner=owner)],
            worktree_root=Path("/tmp"),
        )

    def test_issue_not_found_error(self):
        ctx = _make_context(issues=[_make_issue(1)])
        result = set_status(ctx, issue_key="owner/repo#999", status="Todo")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_invalid_status_error(self):
        ctx = _make_context(issues=[_make_issue(1)])
        result = set_status(ctx, issue_key="owner/repo#1", status="Invalid")
        assert result.success is False
        assert "Invalid status" in result.error

    def test_preview_text(self):
        issue = _make_issue(42, repo="owner/my-repo")
        ctx = _make_context(issues=[issue])
        result = set_status(ctx, issue_key="owner/my-repo#42", status="In Progress")
        assert result.success is True
        assert "In Progress" in result.preview
        assert "my-repo#42" in result.preview

    def test_execute_calls_cache_set_status(self):
        issue = Issue(
            number=42,
            title="Test",
            repo="owner/repo",
            status="Todo",
            priority=Priority.NEED,
            project_number=12,
        )
        config = self._make_config_with_project(project_number=12, owner="my-org")
        mock_cache = MagicMock()
        mock_cache.set_status.return_value = True
        ctx = ToolContext(
            config=config,
            github_cache=mock_cache,
            issues=[issue],
            agents=[],
            active_screen="dispatch",
            selected_keys=set(),
        )
        result = set_status(ctx, issue_key="owner/repo#42", status="Done")
        assert result.success is True
        assert result.execute() is True
        mock_cache.set_status.assert_called_once_with(issue, "my-org", "Done")

    def test_execute_returns_false_when_no_github_cache(self):
        issue = _make_issue(42)
        ctx = _make_context(issues=[issue])
        result = set_status(ctx, issue_key="owner/repo#42", status="Todo")
        assert result.execute() is False


class TestRankedIssues:
    def context(self, tmp_path):
        from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageStore

        store = TriageStore(tmp_path)
        issues = [_make_issue(n) for n in range(1, 6)]
        issues[0].labels = ["sentinel"]
        for n, value in [(1, 3), (5, 1)]:
            store.upsert(
                TriageRecord(
                    issue_key=f"owner/repo#{n}",
                    value=value,
                    effort=Effort.S,
                    agent_fitness=AgentFitness.SOLO,
                    interest=True,
                ),
                actor="human",
            )
            store.confirm(f"owner/repo#{n}", now="2026-09-13T00:00:00+00:00")
        store.upsert(
            TriageRecord(
                issue_key="owner/repo#2",
                effort=Effort.M,
                reasoning={"effort": "A\x1b\nreason"},
                model="fixture",
            ),
            actor="ai",
        )
        store.upsert(TriageRecord(issue_key="owner/repo#3", pin=True), actor="human")
        return _make_context(issues=issues, triage_store=store)

    def test_order_text_and_json(self, tmp_path):
        import json

        ctx = self.context(tmp_path)
        result = list_issues(ctx)
        lines = result.preview.splitlines()
        assert [line.split()[0] for line in lines[1:]] == [
            "owner/repo#3",
            "owner/repo#1",
            "owner/repo#5",
            "owner/repo#2",
            "owner/repo#4",
        ]
        assert lines[2] == "  owner/repo#1  12 · V3 · S · solo · ★ [confirmed] [Todo] Issue 1 ⚑"
        assert "⌖" in lines[1]
        data = json.loads(list_issues(ctx, format="json").preview)
        assert [row["number"] for row in data] == [3, 1, 5, 2, 4]
        assert set(data[0]) == {
            "key",
            "repo",
            "number",
            "title",
            "status",
            "labels",
            "sentinel",
            "state",
            "value",
            "effort",
            "agent_fitness",
            "interest",
            "pin",
            "score",
            "breakdown",
            "reasoning",
            "model",
            "proposed_at",
            "updated_at",
        }
        assert data[0]["value"] is None
        assert data[1]["score"] == 12
        assert data[1]["breakdown"] == [["V3", 6], ["S", 2], ["solo", 2], ["★", 2]]
        assert data[3]["reasoning"] == {"effort": "Areason"}

    def test_dimension_filters(self, tmp_path):
        import json

        ctx = self.context(tmp_path)
        for filters, numbers in [
            ({"effort": "S"}, [1, 5]),
            ({"effort": "none"}, [3, 4]),
            ({"fitness": "SOLO"}, [1, 5]),
            ({"fitness": "none"}, [3, 2, 4]),
            ({"state": "PROPOSED"}, [2]),
            ({"state": "untriaged"}, [3, 4]),
            ({"state": "confirmed"}, [1, 5]),
        ]:
            data = json.loads(list_issues(ctx, format="json", **filters).preview)
            assert [row["number"] for row in data] == numbers
        for filters, allowed in [
            ({"effort": "XL"}, "S, M, L, none"),
            ({"fitness": "invalid"}, "solo, pair, needs_you, none"),
            ({"state": "invalid"}, "untriaged, proposed, confirmed"),
            ({"format": "xml"}, "text, json"),
        ]:
            result = list_issues(ctx, **filters)
            assert not result.success
            assert allowed in result.error

    def test_storeless(self):
        import json

        ctx = _make_context(issues=[_make_issue()])
        assert "— [untriaged]" in list_issues(ctx).preview
        assert json.loads(list_issues(ctx, format="json").preview)[0]["value"] is None
        for filters in [{"effort": "none"}, {"fitness": "none"}, {"state": "untriaged"}]:
            result = list_issues(ctx, **filters)
            assert not result.success
            assert "triage store not configured" in result.error
        assert list_issues(_make_context(), format="json").preview == "[]"


def test_json_listing_skips_malformed_reasoning(tmp_path):
    import json

    from agent_hq.triage_store import TriageRecord, TriageStore

    store = TriageStore(tmp_path)
    store.upsert(TriageRecord(issue_key="owner/repo#1", reasoning={"value": None}), actor="ai")
    result = list_issues(_make_context(issues=[_make_issue()], triage_store=store), format="json")
    assert json.loads(result.preview)[0]["reasoning"] == {}


class TestTriageWrites:
    def context(self, tmp_path):
        from agent_hq.triage_store import TriageStore

        return _make_context(issues=[_make_issue()], triage_store=TriageStore(tmp_path))

    def test_partial_writes_and_explicit_confirm(self, tmp_path):
        from agent_hq.tools import confirm_triage, set_triage
        from agent_hq.triage_store import TriageState

        ctx = self.context(tmp_path)
        result = set_triage(ctx, "owner/repo#1", value=3)
        assert ctx.triage_store.get("owner/repo#1") is None
        assert result.execute()
        rec = ctx.triage_store.get("owner/repo#1")
        assert rec.value == 3 and rec.effort is None and rec.agent_fitness is None
        assert rec.provenance == {"value": "human"}
        assert rec.state is TriageState.UNTRIAGED
        failed = confirm_triage(ctx, rec.issue_key)
        assert not failed.execute()
        assert "value, effort and fitness" in failed.error
        assert set_triage(ctx, rec.issue_key, effort="m", fitness="needs-you").execute()
        assert set_triage(ctx, rec.issue_key, interest=True, pin=True).execute()
        rec = ctx.triage_store.get(rec.issue_key)
        assert rec.value == 3 and rec.effort.value == "M"
        assert rec.agent_fitness.value == "needs_you"
        assert rec.interest and rec.pin and rec.state is TriageState.UNTRIAGED
        result = confirm_triage(ctx, rec.issue_key)
        assert result.execute() and "[confirmed]" in result.outcome
        assert set_triage(ctx, rec.issue_key, value=5, interest=False, pin=False).execute()
        rec = ctx.triage_store.get(rec.issue_key)
        assert rec.state is TriageState.CONFIRMED and rec.value == 5
        assert rec.interest is False and rec.pin is False

    def test_proposed_metadata_preserved(self, tmp_path):
        from agent_hq.tools import confirm_triage, set_triage
        from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageState

        ctx = self.context(tmp_path)
        key = "owner/repo#1"
        ctx.triage_store.upsert(
            TriageRecord(
                issue_key=key,
                value=2,
                effort=Effort.S,
                agent_fitness=AgentFitness.SOLO,
                model="fixture",
                reasoning={"effort": "Small"},
                proposed_at="2026-09-13",
            ),
            actor="ai",
        )
        assert set_triage(ctx, key, value=4).execute()
        rec = ctx.triage_store.get(key)
        assert rec.state is TriageState.PROPOSED
        assert rec.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}
        assert rec.reasoning == {"effort": "Small"} and rec.model == "fixture"
        assert confirm_triage(ctx, key).execute()
        assert ctx.triage_store.get(key).provenance == rec.provenance

    def test_validation_and_failures(self, tmp_path, monkeypatch):
        from agent_hq.tools import confirm_triage, set_triage

        ctx = self.context(tmp_path)
        key = "owner/repo#1"
        for args in [
            {},
            {"value": 7},
            {"value": True},
            {"value": 2.5},
            {"effort": "XL"},
            {"fitness": "no"},
            {"interest": "yes"},
        ]:
            assert not set_triage(ctx, key, **args).success
        assert not set_triage(ctx, "owner/repo#9", value=3).success
        assert not confirm_triage(ctx, "owner/repo#9").success
        assert not set_triage(_make_context(), key, value=3).success
        assert not confirm_triage(_make_context(), key).success
        assert ctx.triage_store.all_records() == []
        monkeypatch.setattr(ctx.triage_store, "upsert", lambda *a, **kw: None)
        result = set_triage(ctx, key, value=3)
        assert not result.execute()
        assert not result.success and "unreadable" in result.error


def test_confirm_ai_proposal_keeps_all_ai_provenance(tmp_path):
    from agent_hq.tools import confirm_triage
    from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageStore

    store = TriageStore(tmp_path)
    key = "owner/repo#1"
    store.upsert(
        TriageRecord(key, value=3, effort=Effort.S, agent_fitness=AgentFitness.SOLO), actor="ai"
    )
    ctx = _make_context(issues=[_make_issue()], triage_store=store)
    assert confirm_triage(ctx, key).execute()
    assert store.get(key).provenance == {
        "value": "ai",
        "effort": "ai",
        "agent_fitness": "ai",
    }
    store.apply_open_issue_set("owner/repo", set(), now="2026-09-13T00:00:00+00:00")
    assert not confirm_triage(ctx, key).execute()


@pytest.mark.parametrize("fitness", ["needs_you", "needs-you", "Needs You", "NEEDS_YOU"])
@pytest.mark.parametrize("effort", ["m", "M"])
def test_triage_write_and_listing_share_dimension_spellings(tmp_path, fitness, effort):
    import json

    from agent_hq.tools import set_triage
    from agent_hq.triage_store import TriageStore

    ctx = _make_context(issues=[_make_issue()])
    ctx.triage_store = TriageStore(tmp_path)
    key = ctx.issues[0].key
    write = set_triage(ctx, key, effort=effort, fitness=fitness)
    assert write.success and write.execute()
    listing = list_issues(ctx, effort=effort, fitness=fitness, format="json")
    assert listing.success, listing.error
    rows = json.loads(listing.preview)
    assert [row["key"] for row in rows] == [key]
    assert rows[0]["effort"] == "M"
    assert rows[0]["agent_fitness"] == "needs_you"


# ---------------------------------------------------------------------------
# open_chat_session (#290)
# ---------------------------------------------------------------------------


class TestOpenChatSessionTool:
    """#290: a bare REPL in scratch / a repo's main session -- no issue,
    no worktree. Validation happens before anything launches."""

    @staticmethod
    def _config(tmp_path):
        from agent_hq.config import RepoConfig

        cfg = _make_dispatch_config()
        cfg.repos = {
            "owner/repo": RepoConfig(owner_name="owner/repo", local_path=tmp_path / "repo")
        }
        cfg.dispatch.scratch_cwd = tmp_path / "scratch"
        return cfg

    @staticmethod
    def _caps(*harnesses):
        from agent_hq.capabilities import LauncherCapabilities

        return LauncherCapabilities(agents=frozenset({"claude", *harnesses}), flags={})

    def _run(self, ctx, caps=None, **kwargs):
        from agent_hq.tools import open_chat_session

        with patch("agent_hq.tools.probe_launcher_capabilities", return_value=caps or self._caps()):
            return open_chat_session(ctx, **kwargs)

    def test_unknown_target_errors_before_launch(self, tmp_path):
        ctx = _make_context(config=self._config(tmp_path))
        with patch("agent_hq.dispatch.open_chat_window") as opener:
            for target in ("repo:owner/nope", "elsewhere", ""):
                result = self._run(ctx, target=target)
                assert result.success is False
                assert "Unknown target" in result.error
        opener.assert_not_called()

    def test_unsupported_harness_errors_before_launch(self, tmp_path):
        ctx = _make_context(config=self._config(tmp_path))
        with patch("agent_hq.dispatch.open_chat_window") as opener:
            result = self._run(ctx, target="scratch", harness="codex")
        assert result.success is False
        assert "not supported" in result.error
        opener.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "needle"),
        [({"effort": "huge"}, "Invalid effort"), ({"model": "op us"}, "Invalid model")],
    )
    def test_bad_model_or_effort_errors(self, tmp_path, kwargs, needle):
        ctx = _make_context(config=self._config(tmp_path))
        result = self._run(ctx, target="scratch", **kwargs)
        assert result.success is False
        assert needle in result.error

    def test_config_harness_model_default_is_not_applied(self, tmp_path):
        """Omitted model/effort means the harness's own default, not the
        dispatch config's per-harness pins (those are for worktree agents)."""
        from agent_hq.config import HarnessDefaults

        cfg = self._config(tmp_path)
        cfg.dispatch.harnesses = {"claude": HarnessDefaults(model="sonnet", effort="low")}
        ctx = _make_context(config=cfg)
        with (
            patch("agent_hq.dispatch._find_session_by_name", return_value="scratch"),
            patch("agent_hq.dispatch.open_chat_window", return_value="%7") as opener,
            patch("agent_hq.dispatch._wait_for_harness_process", return_value=True),
        ):
            result = self._run(ctx, target="scratch")
            assert result.execute() is True
        kwargs = opener.call_args.kwargs
        assert kwargs["model"] is None and kwargs["effort"] is None

    def test_repo_target_reuses_main_session_and_reports_pane(self, tmp_path):
        import json

        ctx = _make_context(config=self._config(tmp_path))
        with (
            patch("agent_hq.dispatch._find_repo_session", return_value="repo") as find,
            patch("agent_hq.dispatch.open_chat_window", return_value="%7") as opener,
            patch("agent_hq.dispatch._wait_for_harness_process", return_value=True) as wait,
        ):
            result = self._run(
                ctx, target="repo:owner/repo", model="opus", prompt="/clean-gone --all"
            )
            assert result.success is True
            assert result.preview == (
                "Will open in owner/repo: claude --model opus -- '/clean-gone --all'"
            )
            opener.assert_not_called()  # nothing launches before execute()
            assert result.execute() is True
        find.assert_called_once_with(tmp_path / "repo")
        assert opener.call_args.kwargs == {
            "existing_session": "repo",
            "new_session_name": "repo",
            "cwd": tmp_path / "repo",
            "harness": "claude",
            "model": "opus",
            "effort": None,
            "prompt": "/clean-gone --all",
        }
        wait.assert_called_once_with("%7", "claude")
        payload = json.loads(result.outcome)
        assert payload["pane_id"] == "%7"
        assert payload["session_name"] == "repo"
        assert payload["started"] is True
        assert payload["prompt_delivered"] is True

    def test_scratch_creates_session_in_scratch_cwd(self, tmp_path):
        import json

        ctx = _make_context(config=self._config(tmp_path))
        with (
            patch("agent_hq.dispatch._find_session_by_name", return_value=None),
            patch("agent_hq.dispatch.open_chat_window", return_value="%3") as opener,
            patch("agent_hq.dispatch._wait_for_harness_process", return_value=True),
        ):
            result = self._run(ctx, target="scratch")
            assert result.execute() is True
        assert opener.call_args.kwargs["existing_session"] is None
        assert opener.call_args.kwargs["new_session_name"] == "scratch"
        assert opener.call_args.kwargs["cwd"] == tmp_path / "scratch"
        payload = json.loads(result.outcome)
        assert payload["session_name"] == "scratch"
        assert payload["prompt_delivered"] is None  # no prompt given

    def test_harness_never_seen_reports_unconfirmed(self, tmp_path):
        import json

        ctx = _make_context(config=self._config(tmp_path))
        with (
            patch("agent_hq.dispatch._find_session_by_name", return_value="scratch"),
            patch("agent_hq.dispatch.open_chat_window", return_value="%3"),
            patch("agent_hq.dispatch._wait_for_harness_process", return_value=False),
        ):
            result = self._run(ctx, target="scratch", prompt="hi")
            assert result.execute() is False
        payload = json.loads(result.outcome)
        assert payload["pane_id"] == "%3"  # left open for inspection
        assert payload["started"] is False
        assert payload["prompt_delivered"] is False
        assert "could not be confirmed" in payload["message"]

    def test_window_creation_failure(self, tmp_path):
        ctx = _make_context(config=self._config(tmp_path))
        with (
            patch("agent_hq.dispatch._find_session_by_name", return_value="scratch"),
            patch("agent_hq.dispatch.open_chat_window", return_value=None),
            patch("agent_hq.dispatch._wait_for_harness_process") as wait,
        ):
            result = self._run(ctx, target="scratch")
            assert result.execute() is False
        assert result.outcome.startswith("Failed:")
        wait.assert_not_called()


class TestOpenChatSessionInputGuards:
    """Review round 1 (#290): inputs typed into a shell via send-keys."""

    def _run(self, tmp_path, caps_harnesses=(), **kwargs):
        return TestOpenChatSessionTool()._run(
            _make_context(config=TestOpenChatSessionTool._config(tmp_path)),
            caps=TestOpenChatSessionTool._caps(*caps_harnesses),
            **kwargs,
        )

    def test_effort_validated_against_the_resolved_harness(self, tmp_path):
        result = self._run(tmp_path, target="scratch", effort="minimal")
        assert result.success is False
        assert "Invalid effort for claude" in result.error
        ok = self._run(tmp_path, ("codex",), target="scratch", harness="codex", effort="minimal")
        assert ok.success is True

    @pytest.mark.parametrize("prompt", ["hi\x03rm -rf ~", "tab\there", "esc\x1b[A", "nul\x00"])
    def test_control_characters_in_prompt_rejected(self, tmp_path, prompt):
        """A typed Ctrl-C would cancel the launch line and run the rest of
        the prompt as a shell command; Tab triggers completion."""
        result = self._run(tmp_path, target="scratch", prompt=prompt)
        assert result.success is False
        assert "control characters" in result.error

    def test_multiline_prompt_allowed(self, tmp_path):
        result = self._run(tmp_path, target="scratch", prompt="line one\nline two")
        assert result.success is True

    def test_option_like_model_rejected(self, tmp_path):
        result = self._run(tmp_path, target="scratch", model="--dangerously-skip-permissions")
        assert result.success is False
        assert "Invalid model" in result.error


def test_open_chat_session_preview_matches_codex_launch_line(tmp_path):
    """The preview is built from the adapter's own argv, so codex's -c
    overrides show as sent (review round 1, #290)."""
    result = TestOpenChatSessionTool()._run(
        _make_context(config=TestOpenChatSessionTool._config(tmp_path)),
        caps=TestOpenChatSessionTool._caps("codex"),
        target="scratch",
        harness="codex",
        effort="high",
    )
    assert result.preview == (
        "Will open in scratch: agent-hq codex -- -c model_reasoning_effort=high"
    )


def test_open_chat_session_is_in_the_tool_registry(tmp_path):
    ctx = _make_context(config=TestOpenChatSessionTool._config(tmp_path))
    with patch(
        "agent_hq.tools.probe_launcher_capabilities",
        return_value=TestOpenChatSessionTool._caps(),
    ):
        result = execute_tool_call("open_chat_session", {"target": "scratch"}, ctx)
    assert result.success is True


@pytest.mark.parametrize("model", ["opus\x15ls", "opus\x03", "opus\x1b", "opus\x7f"])
def test_open_chat_session_rejects_control_characters_in_model(tmp_path, model):
    """Review round 2 (#290): the model is typed into the shell too, so a
    Ctrl-U/Ctrl-C there could erase the launch line and run the rest."""
    result = TestOpenChatSessionTool()._run(
        _make_context(config=TestOpenChatSessionTool._config(tmp_path)),
        target="scratch",
        model=model,
    )
    assert result.success is False
    assert "Invalid model" in result.error


@pytest.fixture
def context_with_store(tmp_path):
    from agent_hq.triage_store import TriageStore

    store = TriageStore(tmp_path)
    return _make_context(issues=[_make_issue()], triage_store=store), store, "owner/repo#1"


def test_set_triage_autopilot_flag(context_with_store):
    from agent_hq import tools

    ctx, store, key = context_with_store
    result = tools.set_triage(ctx, key, autopilot=True)
    assert result.success and result.execute()
    assert store.get(key).autopilot is True


def test_set_triage_autopilot_must_be_bool(context_with_store):
    from agent_hq import tools

    ctx, _store, key = context_with_store
    result = tools.set_triage(ctx, key, autopilot="yes")
    assert not result.success and "autopilot must be a boolean" in result.error


def test_set_triage_actor_and_reasoning_reach_the_store(context_with_store):
    """#397: the remote connector writes as "duet" with its own reasoning."""
    from agent_hq import tools

    ctx, store, key = context_with_store
    result = tools.set_triage(ctx, key, value=4, effort="S", reasoning="Small fix", actor="duet")
    assert result.success and result.execute()
    rec = store.get(key)
    assert rec.provenance == {"value": "duet", "effort": "duet"}
    assert rec.reasoning == {"value": "Small fix", "effort": "Small fix"}


def test_set_triage_defaults_to_human_without_reasoning(context_with_store):
    from agent_hq import tools

    ctx, store, key = context_with_store
    assert tools.set_triage(ctx, key, value=2).execute()
    rec = store.get(key)
    assert rec.provenance == {"value": "human"}
    assert rec.reasoning == {}


@pytest.mark.parametrize("bad", ["", "x" * 201, "line\nbreak", "esc\x1b[2J", 5])
def test_set_triage_rejects_bad_reasoning(context_with_store, bad):
    from agent_hq import tools

    ctx, _store, key = context_with_store
    result = tools.set_triage(ctx, key, value=3, reasoning=bad)
    assert not result.success and "reasoning" in result.error
