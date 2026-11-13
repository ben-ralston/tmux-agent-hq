"""Tests for MCP server tool wrappers."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_hq import mcp_server
from agent_hq.config import Config, ProjectConfig, RepoConfig
from agent_hq.models import Agent, AgentState, AgentType, Issue, Priority
from agent_hq.state import InactiveRecord

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


def _make_issue(
    number=1,
    repo="owner/repo",
    priority=Priority.NEED,
    status="Todo",
    project_number=1,
):
    return Issue(
        number=number,
        title=f"Issue {number}",
        repo=repo,
        status=status,
        priority=priority,
        project_number=project_number,
    )


def _make_config():
    return Config(
        repos={"owner/repo": RepoConfig(owner_name="owner/repo", local_path=Path("/tmp/repo"))},
        projects=[ProjectConfig(name="Test", number=1, owner="@me")],
        worktree_root=Path("/tmp/worktrees"),
    )


@pytest.fixture(autouse=True)
def _reset_cached_state():
    """Reset module-level cached state between tests."""
    mcp_server._config = None
    mcp_server._config_loaded = False
    mcp_server._github_cache = None
    mcp_server._triage_store = None
    mcp_server._triage_store_warned = False
    yield
    mcp_server._config = None
    mcp_server._config_loaded = False
    mcp_server._github_cache = None
    mcp_server._triage_store = None
    mcp_server._triage_store_warned = False


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    """Tests for the list_agents MCP wrapper."""

    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[
            _make_agent(pane_id="%1", repo_name="agent-hq", state=AgentState.IDLE),
            _make_agent(pane_id="%2", repo_name="other", state=AgentState.RUNNING),
        ],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_formats_output(self, _disc, _enrich, _cfg, _inactive):
        result = mcp_server.list_agents()
        assert "2 agent(s)" in result
        assert "%1" in result
        assert "%2" in result

    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(mcp_server, "enrich_all_agents", return_value=[])
    @patch.object(mcp_server, "discover_agents", return_value=[])
    def test_empty(self, _disc, _enrich, _cfg, _inactive):
        result = mcp_server.list_agents()
        assert "No agents match" in result

    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[_make_agent(pane_id="%1", repo_name="agent-hq")],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_json_format_passes_through(self, _disc, _enrich, _cfg, _inactive):
        import json

        result = mcp_server.list_agents(format="json")
        assert json.loads(result)[0]["pane_id"] == "%1"


# ---------------------------------------------------------------------------
# kill_agent
# ---------------------------------------------------------------------------


class TestKillAgent:
    """Tests for the kill_agent MCP wrapper."""

    @patch("agent_hq.actions.kill_agent")
    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[_make_agent(pane_id="%5", repo_name="agent-hq")],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_no_confirm_previews_only(self, _disc, _enrich, _cfg, _inactive, mock_kill):
        result = mcp_server.kill_agent(identifier="%5")
        assert "Would kill" in result
        assert "%5" in result
        mock_kill.assert_not_called()

    @patch("agent_hq.actions.verify_agent_killed", return_value=(True, AgentType.UNKNOWN))
    @patch("agent_hq.actions.kill_agent", return_value=True)
    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[_make_agent(pane_id="%5", repo_name="agent-hq")],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_confirmed_kill_reports_verified_outcome(
        self, _disc, _enrich, _cfg, _inactive, mock_kill, _mock_verify
    ):
        result = mcp_server.kill_agent(identifier="%5", confirm=True)
        assert "Killed" in result
        assert "verified" in result
        mock_kill.assert_called_once_with("%5")

    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(mcp_server, "enrich_all_agents", return_value=[])
    @patch.object(mcp_server, "discover_agents", return_value=[])
    def test_not_found(self, _disc, _enrich, _cfg, _inactive):
        result = mcp_server.kill_agent(identifier="%99")
        assert "Error" in result
        assert "No agent found" in result

    @patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.RUNNING)
    @patch("agent_hq.actions.verify_agent_killed", return_value=(False, AgentType.CLAUDE))
    @patch("agent_hq.actions.kill_agent", return_value=False)
    @patch.object(mcp_server, "load_inactive_state", return_value={})
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[_make_agent(pane_id="%5", repo_name="agent-hq")],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_execute_fails(self, _disc, _enrich, _cfg, _inactive, _mock_kill, _mock_verify, _st):
        result = mcp_server.kill_agent(identifier="%5", confirm=True)
        assert "Ctrl-C" in result or "Failed" in result


# ---------------------------------------------------------------------------
# list_issues
# ---------------------------------------------------------------------------


class TestListIssues:
    """Tests for the list_issues MCP wrapper."""

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_formats_output(self, _cfg):
        issues = [_make_issue(1), _make_issue(2)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mcp_server._github_cache = mock_cache

        result = mcp_server.list_issues()
        assert "2 issue(s)" in result
        assert "owner/repo#1" in result

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_no_issues(self, _cfg):
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = []
        mcp_server._github_cache = mock_cache

        result = mcp_server.list_issues()
        assert "No issues match" in result


# ---------------------------------------------------------------------------
# dispatch_issues
# ---------------------------------------------------------------------------


class TestDispatchIssues:
    """Tests for the dispatch_issues MCP wrapper."""

    @patch("agent_hq.tools.dispatch_issues")
    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_success(self, _cfg, mock_dispatch):
        from agent_hq.tools import ToolResult

        mock_dispatch.return_value = ToolResult(
            success=True,
            preview="Will send /worktree-create #1 to Claude in repo session",
            execute=lambda: True,
        )

        issues = [_make_issue(1)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mcp_server._github_cache = mock_cache

        result = mcp_server.dispatch_issues(issue_keys=["owner/repo#1"])
        assert "worktree-create" in result or "Will send" in result

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_no_keys_error(self, _cfg):
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = []
        mcp_server._github_cache = mock_cache

        result = mcp_server.dispatch_issues()
        assert "Error" in result

    @patch("agent_hq.tools.dispatch_issues")
    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_mcp_tool_signature_accepts_harness(self, _cfg, mock_dispatch):
        from agent_hq.tools import ToolResult

        mock_dispatch.return_value = ToolResult(
            success=True,
            preview="Will send /worktree-create --harness codex #1 to Claude in repo session",
            execute=lambda: True,
        )

        issues = [_make_issue(1)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mcp_server._github_cache = mock_cache

        result = mcp_server.dispatch_issues(
            issue_keys=["owner/repo#1"], harness="codex", model="o3", effort="high"
        )
        assert "Will send" in result
        _ctx, kwargs = mock_dispatch.call_args
        assert kwargs["harness"] == "codex"
        assert kwargs["model"] == "o3"
        assert kwargs["effort"] == "high"

    @patch("agent_hq.tools.dispatch_issues")
    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_execute_failure_surfaces_real_error(self, _cfg, mock_dispatch):
        """Fix 8 (multi-review wave): when execute() fails, the MCP reply
        must surface tools.dispatch_issues's ``result.outcome`` (the real
        dispatch error) rather than the generic, uninformative
        'Failed: <preview>' fallback."""
        from agent_hq.tools import ToolResult

        def _execute() -> bool:
            tr.outcome = "Failed: worktree already exists at /tmp/x"
            return False

        tr = ToolResult(
            success=True,
            preview="Will send /worktree-create #1 to Claude in repo session",
            execute=_execute,
        )
        mock_dispatch.return_value = tr

        issues = [_make_issue(1)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mcp_server._github_cache = mock_cache

        result = mcp_server.dispatch_issues(issue_keys=["owner/repo#1"])
        assert result == "Failed: worktree already exists at /tmp/x"


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


class TestSetStatusMCP:
    """Tests for the set_status MCP wrapper."""

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_sets_status(self, _cfg):
        issues = [_make_issue(1, project_number=1)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mock_cache.set_status.return_value = True
        mcp_server._github_cache = mock_cache

        result = mcp_server.set_status(issue_key="owner/repo#1", status="In Progress")
        assert "In Progress" in result
        mock_cache.set_status.assert_called_once()

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_invalid_status(self, _cfg):
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = [_make_issue(1)]
        mcp_server._github_cache = mock_cache

        result = mcp_server.set_status(issue_key="owner/repo#1", status="Invalid")
        assert "Error" in result
        assert "Invalid status" in result


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


class TestContextBuilders:
    """Tests for _discover_agents_context and _issues_context."""

    @patch.object(
        mcp_server,
        "load_inactive_state",
        return_value={"%5": InactiveRecord(marked_at=0.0, fingerprint="abc")},
    )
    @patch.object(mcp_server, "load_config", return_value=None)
    @patch.object(
        mcp_server,
        "enrich_all_agents",
        return_value=[_make_agent(pane_id="%5")],
    )
    @patch.object(mcp_server, "discover_agents", return_value=[{"mock": True}])
    def test_discover_agents_context(self, _disc, _enrich, _cfg, _inactive):
        ctx = mcp_server._discover_agents_context()
        assert len(ctx.agents) == 1
        assert ctx.agents[0].pane_id == "%5"
        assert "%5" in ctx.inactive_pane_ids
        assert ctx.active_screen == "agents"

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_issues_context(self, _cfg):
        issues = [_make_issue(1), _make_issue(2)]
        mock_cache = MagicMock()
        mock_cache.get_issues.return_value = issues
        mcp_server._github_cache = mock_cache

        ctx = mcp_server._issues_context()
        assert len(ctx.issues) == 2
        assert ctx.agents == []
        assert ctx.active_screen == "dispatch"
        assert ctx.config is not None


# ---------------------------------------------------------------------------
# Cached state helpers
# ---------------------------------------------------------------------------


class TestCachedState:
    """Tests for lazy-initialized cached state."""

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_config_loaded_once(self, mock_load):
        mcp_server._get_config()
        mcp_server._get_config()
        # Should only call load_config once
        mock_load.assert_called_once()

    def test_github_cache_created_once(self):
        cache1 = mcp_server._get_github_cache()
        cache2 = mcp_server._get_github_cache()
        assert cache1 is cache2


class TestTriageContext:
    def test_store_memoized_and_passed_to_context(self, tmp_path):
        from agent_hq.config import TriageConfig
        from agent_hq.triage_store import TriageStore

        cfg = _make_config()
        cfg.triage = TriageConfig(data_dir=tmp_path)
        with patch.object(mcp_server, "load_config", return_value=cfg):
            mcp_server._github_cache = MagicMock()
            mcp_server._github_cache.get_issues.return_value = []
            store = mcp_server._get_triage_store()
            assert isinstance(store, TriageStore)
            assert store.data_dir == tmp_path
            assert mcp_server._get_triage_store() is store
            assert mcp_server._issues_context().triage_store is store
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("configured", [False, True])
    def test_storeless(self, configured):
        cfg = _make_config() if configured else None
        with patch.object(mcp_server, "load_config", return_value=cfg):
            assert mcp_server._get_triage_store() is None

    @pytest.mark.parametrize("is_file", [False, True])
    def test_missing_directory_degrades(self, tmp_path, is_file):
        from agent_hq.config import TriageConfig
        from agent_hq.tools import ToolResult

        cfg = _make_config()
        cfg.triage = TriageConfig(data_dir=tmp_path / "missing")
        if is_file:
            cfg.triage.data_dir.write_text("not a directory")
        cache = MagicMock()
        cache.get_issues.return_value = [_make_issue()]
        cache.get_closed_keys.return_value = set()
        mcp_server._github_cache = cache
        with (
            patch.object(mcp_server, "load_config", return_value=cfg),
            patch.object(mcp_server.eventlog, "emit") as emit,
        ):
            assert mcp_server._get_triage_store() is None
            assert mcp_server._issues_context().triage_store is None
            assert "Issue 1" in mcp_server.list_issues()
            assert mcp_server.refresh_issues() == "Refreshed: 1 issues"
            for filters in ({"effort": "S"}, {"fitness": "solo"}, {"state": "confirmed"}):
                assert "triage store not configured" in mcp_server.list_issues(**filters)
            assert "triage store not configured" in mcp_server.set_triage("owner/repo#1", value=3)
            assert "triage store not configured" in mcp_server.confirm_triage("owner/repo#1")
            for name, kwargs in (
                ("dispatch_issues", {"issue_keys": ["owner/repo#1"]}),
                ("set_status", {"issue_key": "owner/repo#1", "status": "Todo"}),
            ):
                with patch.object(
                    mcp_server.tools, name, return_value=ToolResult(True, "ok", lambda: True)
                ) as tool:
                    assert getattr(mcp_server, name)(**kwargs) == "ok"
                    assert tool.call_args.args[0].triage_store is None
            emit.assert_called_once_with("warn", "mcp triage store unavailable", {})
        assert cfg.triage.data_dir.is_file() if is_file else not cfg.triage.data_dir.exists()

    @pytest.mark.parametrize("closed", [set(), {"owner/repo#2"}])
    def test_shared_open_set(self, closed):
        cfg = _make_config()
        cfg.projects.append(ProjectConfig(name="Other", number=2, owner="@me"))
        cache = MagicMock()
        cache.get_issues.side_effect = [
            [_make_issue(1), _make_issue(2), _make_issue(3, status="Done")],
            [_make_issue(1)],
        ]
        cache.get_closed_keys.return_value = closed
        mcp_server._github_cache = cache
        with patch.object(mcp_server, "load_config", return_value=cfg):
            assert [i.number for i in mcp_server._get_all_issues()] == ([1] if closed else [1, 2])
        cache.get_closed_keys.assert_called_once_with({"owner/repo"}, force=False)


def test_list_issues_dimension_schema_and_forwarding():
    import inspect

    from agent_hq.tools import ToolResult

    args = dict(
        repo="repo", status="Todo", effort="S", fitness="solo", state="confirmed", format="json"
    )
    assert set(inspect.signature(mcp_server.list_issues).parameters) == set(args)
    with (
        patch.object(mcp_server, "_issues_context") as context,
        patch.object(
            mcp_server.tools, "list_issues", return_value=ToolResult(True, "[]", lambda: True)
        ) as listing,
    ):
        assert mcp_server.list_issues(**args) == "[]"
        listing.assert_called_once_with(context.return_value, **args)


@pytest.mark.parametrize(
    "name,args",
    [
        ("set_triage", {"issue_key": "owner/repo#1", "value": 3}),
        ("confirm_triage", {"issue_key": "owner/repo#1"}),
    ],
)
def test_triage_write_wrappers(name, args):
    from agent_hq.tools import ToolResult

    result = ToolResult(True, "preview", MagicMock(return_value=True), outcome="stored summary")
    with (
        patch.object(mcp_server, "_issues_context"),
        patch.object(mcp_server.tools, name, return_value=result),
    ):
        assert getattr(mcp_server, name)(**args) == "stored summary"
        result.execute.assert_called_once()
        result.success = False
        result.error = "invalid"
        assert getattr(mcp_server, name)(**args) == "Error: invalid"
        result.success = True
        result.execute.return_value = False
        result.outcome = "Error: write failed"
        assert getattr(mcp_server, name)(**args) == "Error: write failed"


def test_refresh_forces_boards_and_closed_keys():
    from unittest.mock import call

    cfg = _make_config()
    cfg.projects.append(ProjectConfig(name="Other", number=2, owner="other"))
    cache = MagicMock()
    cache.get_issues.return_value = [_make_issue(1), _make_issue(2)]
    cache.get_closed_keys.side_effect = (
        lambda repos, force=False: {"owner/repo#2"} if force else set()
    )
    mcp_server._github_cache = cache
    with patch.object(mcp_server, "load_config", return_value=cfg):
        assert len(mcp_server._get_all_issues()) == 2
        assert not cache.get_closed_keys.call_args.kwargs.get("force", False)
        assert all(not c.kwargs.get("force", False) for c in cache.get_issues.call_args_list)
        cache.reset_mock()
        assert mcp_server.refresh_issues() == "Refreshed: 1 issues"
    assert cache.get_issues.call_args_list == [
        call(1, "@me", force=True),
        call(2, "other", force=True),
    ]
    cache.get_closed_keys.assert_called_once_with({"owner/repo"}, force=True)


# ---------------------------------------------------------------------------
# open_chat_session (#290)
# ---------------------------------------------------------------------------


class TestOpenChatSession:
    @patch("agent_hq.tools.open_chat_session")
    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_forwards_args_and_returns_outcome(self, _cfg, mock_open):
        from agent_hq.tools import ToolResult

        tr = ToolResult(success=True, preview="Will open", execute=lambda: True)
        tr.outcome = '{"pane_id": "%7", "session_name": "repo"}'
        mock_open.return_value = tr

        result = mcp_server.open_chat_session(
            target="repo:owner/repo", model="opus", prompt="/clean-gone --all"
        )
        assert result == '{"pane_id": "%7", "session_name": "repo"}'
        ctx, kwargs = mock_open.call_args.args[0], mock_open.call_args.kwargs
        assert ctx.config is not None
        assert kwargs == {
            "target": "repo:owner/repo",
            "harness": None,
            "model": "opus",
            "effort": None,
            "prompt": "/clean-gone --all",
        }

    @patch.object(mcp_server, "load_config", return_value=_make_config())
    def test_unknown_target_is_an_error(self, _cfg):
        result = mcp_server.open_chat_session(target="repo:owner/nope")
        assert result.startswith("Error:")
        assert "Unknown target" in result

    def test_docstring_steers_away_from_dispatch(self):
        doc = mcp_server.open_chat_session.__doc__
        assert "No issue" in doc and "worktree" in doc
