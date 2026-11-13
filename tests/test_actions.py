"""Tests for actions module."""

import subprocess
from unittest.mock import MagicMock, call, patch

from agent_hq.actions import (
    generate_session_name,
    jump_to_pane,
    rename_session,
    verify_agent_killed,
)
from agent_hq.models import AgentState, AgentType


class TestVerifyAgentKilled:
    """Tests for the post-kill verification poll (#185)."""

    @patch("agent_hq.actions._inspect_pane_agent", return_value=(AgentType.UNKNOWN, None))
    def test_dead_immediately(self, mock_info):
        died, remaining = verify_agent_killed("%5", timeout=1.0, interval=0.01)
        assert died is True
        assert remaining == AgentType.UNKNOWN
        mock_info.assert_called_once_with("%5")

    @patch(
        "agent_hq.actions._inspect_pane_agent",
        side_effect=[
            (AgentType.CLAUDE, 123),
            (AgentType.CLAUDE, 123),
            (AgentType.UNKNOWN, None),
        ],
    )
    def test_dies_after_polls(self, mock_info):
        died, remaining = verify_agent_killed("%5", timeout=1.0, interval=0.01)
        assert died is True
        assert remaining == AgentType.UNKNOWN
        assert mock_info.call_count == 3

    @patch("agent_hq.actions._inspect_pane_agent", return_value=(AgentType.CLAUDE, 123))
    def test_timeout_still_alive(self, _mock_info):
        died, remaining = verify_agent_killed("%5", timeout=0.05, interval=0.01)
        assert died is False
        assert remaining == AgentType.CLAUDE

    @patch("agent_hq.actions._inspect_pane_agent", return_value=(AgentType.CODEX, None))
    def test_missing_pid_counts_as_dead(self, _mock_info):
        died, _ = verify_agent_killed("%5", timeout=1.0, interval=0.01)
        assert died is True

    @patch("agent_hq.actions._inspect_pane_agent", return_value=None)
    def test_inspection_failure_is_not_death(self, _mock_info):
        """A failed ps/process-tree read must never be reported as a verified
        kill -- persistent inspection failure times out unverified."""
        died, remaining = verify_agent_killed("%5", timeout=0.05, interval=0.01)
        assert died is False
        assert remaining == AgentType.UNKNOWN

    @patch(
        "agent_hq.actions._inspect_pane_agent",
        side_effect=[None, (AgentType.UNKNOWN, None)],
    )
    def test_inspection_failure_then_dead(self, mock_info):
        died, _ = verify_agent_killed("%5", timeout=1.0, interval=0.01)
        assert died is True
        assert mock_info.call_count == 2


class TestInspectPaneAgent:
    """Tests for the three-way pane inspection helper (#185 review fix)."""

    @patch("agent_hq.actions.build_process_tree", return_value=({}, {}))
    @patch("subprocess.run")
    def test_ps_failure_returns_none(self, mock_run, _mock_tree):
        from agent_hq.actions import _inspect_pane_agent

        mock_run.return_value = MagicMock(stdout="1234\n")
        assert _inspect_pane_agent("%5") is None

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "tmux"))
    def test_missing_pane_is_dead(self, _mock_run):
        from agent_hq.actions import _inspect_pane_agent

        assert _inspect_pane_agent("%5") == (AgentType.UNKNOWN, None)


class TestJumpToPane:
    """Tests for jump_to_pane function."""

    @patch("agent_hq.actions.record_access")
    @patch("subprocess.run")
    def test_preserves_zoom_and_skips_rezoom(self, mock_run, mock_record):
        """Should not re-zoom when switch-client -Z already preserved zoom."""
        switch_result = MagicMock()
        zoom_check = MagicMock(stdout="1\n")

        mock_run.side_effect = [switch_result, zoom_check]

        result = jump_to_pane("%5")

        assert result is True
        assert mock_run.call_args_list == [
            call(
                ["tmux", "switch-client", "-Z", "-t", "%5"],
                check=True,
                capture_output=True,
            ),
            call(
                ["tmux", "display-message", "-t", "%5", "-p", "#{window_zoomed_flag}"],
                capture_output=True,
                text=True,
            ),
        ]

    @patch("agent_hq.actions.record_access")
    @patch("subprocess.run")
    def test_zooms_when_not_already_zoomed(self, mock_run, mock_record):
        """Should zoom target pane when it isn't already zoomed after switch."""
        switch_result = MagicMock()
        zoom_check = MagicMock(stdout="0\n")
        rezoom_result = MagicMock()

        mock_run.side_effect = [switch_result, zoom_check, rezoom_result]

        result = jump_to_pane("%5")

        assert result is True
        assert mock_run.call_args_list == [
            call(
                ["tmux", "switch-client", "-Z", "-t", "%5"],
                check=True,
                capture_output=True,
            ),
            call(
                ["tmux", "display-message", "-t", "%5", "-p", "#{window_zoomed_flag}"],
                capture_output=True,
                text=True,
            ),
            call(
                ["tmux", "resize-pane", "-Z", "-t", "%5"],
                capture_output=True,
            ),
        ]

    @patch("agent_hq.actions.record_access")
    @patch("subprocess.run")
    def test_returns_false_on_failure(self, mock_run, mock_record):
        """Should return False when switch-client fails."""
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, "tmux", stderr=b"no such pane")

        result = jump_to_pane("%99")

        assert result is False
        mock_record.assert_not_called()

    @patch("agent_hq.actions.record_access")
    @patch("subprocess.run")
    def test_returns_false_when_tmux_not_found(self, mock_run, mock_record):
        """Should return False when tmux is not installed."""
        mock_run.side_effect = FileNotFoundError()

        result = jump_to_pane("%5")

        assert result is False
        mock_record.assert_not_called()


class TestGenerateSessionName:
    """Tests for generate_session_name function."""

    @patch("agent_hq.actions.get_repo_name", return_value="my-project")
    @patch("agent_hq.actions.get_git_context", return_value=("/repo", "feature/auth", None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/repo")
    def test_repo_branch_name(self, mock_cwd, mock_ctx, mock_repo):
        """Should generate repo-branch name."""
        name = generate_session_name("%5")
        assert name == "my-project-feature/auth"

    @patch("agent_hq.actions.get_repo_name", return_value="my-project")
    @patch("agent_hq.actions.get_git_context", return_value=("/repo", "main", None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/repo")
    def test_omits_main_branch(self, mock_cwd, mock_ctx, mock_repo):
        """Should use just repo name for main branch."""
        name = generate_session_name("%5")
        assert name == "my-project"

    @patch("agent_hq.actions.get_repo_name", return_value="my-project")
    @patch("agent_hq.actions.get_git_context", return_value=("/repo", "master", None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/repo")
    def test_omits_master_branch(self, mock_cwd, mock_ctx, mock_repo):
        """Should use just repo name for master branch."""
        name = generate_session_name("%5")
        assert name == "my-project"

    @patch("agent_hq.actions.get_git_context", return_value=(None, None, None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/Users/dev/scratch")
    def test_falls_back_to_cwd(self, mock_cwd, mock_ctx):
        """Should fall back to cwd basename when not in a git repo."""
        name = generate_session_name("%5")
        assert name == "scratch"

    @patch("agent_hq.actions.get_repo_name", return_value="my.project")
    @patch("agent_hq.actions.get_git_context", return_value=("/repo", "feat:thing", None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/repo")
    def test_sanitizes_special_chars(self, mock_cwd, mock_ctx, mock_repo):
        """Should replace dots and colons with dashes."""
        name = generate_session_name("%5")
        assert "." not in name
        assert ":" not in name

    @patch("agent_hq.actions.get_repo_name", return_value="repo")
    @patch("agent_hq.actions.get_git_context", return_value=("/repo", "a" * 60, None))
    @patch("agent_hq.actions.get_pane_cwd", return_value="/repo")
    def test_truncates(self, mock_cwd, mock_ctx, mock_repo):
        """Should truncate names longer than 50 characters."""
        name = generate_session_name("%5")
        assert len(name) <= 50


class TestRenameSession:
    """Tests for rename_session function."""

    @patch("subprocess.run")
    @patch("agent_hq.actions._get_pane_agent_info", return_value=(AgentType.CLAUDE, 5678))
    @patch("agent_hq.actions.get_agent_state", return_value=AgentState.IDLE)
    def test_sends_rename_to_idle_claude(self, mock_state, mock_info, mock_run):
        """Should send /rename to idle Claude agents via send-keys."""
        mock_run.return_value = MagicMock(stdout="old-session\n")

        result = rename_session("%5")

        assert result is True
        # Should call send-keys with /rename and Enter
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "%5", "/rename", "Enter"],
            check=True,
            capture_output=True,
        )

    @patch("subprocess.run")
    @patch("agent_hq.actions._get_pane_agent_info", return_value=(AgentType.CLAUDE, 5678))
    @patch("agent_hq.actions.get_agent_state", return_value=AgentState.RUNNING)
    def test_skips_running_claude(self, mock_state, mock_info, mock_run):
        """Should return False for non-idle Claude agents."""
        result = rename_session("%5")
        assert result is False

    @patch("subprocess.run")
    @patch("agent_hq.actions._get_pane_agent_info", return_value=(AgentType.CODEX, 5678))
    @patch("agent_hq.actions.generate_session_name", return_value="my-project-main")
    def test_renames_non_claude_with_generated_name(self, mock_gen, mock_info, mock_run):
        """Should use tmux rename-session for non-Claude agents."""
        mock_run.return_value = MagicMock(stdout="old-session\n")

        result = rename_session("%5")

        assert result is True
        mock_run.assert_any_call(
            ["tmux", "rename-session", "-t", "old-session", "my-project-main"],
            check=True,
            capture_output=True,
        )

    @patch("subprocess.run")
    def test_renames_with_explicit_name(self, mock_run):
        """Should rename directly when explicit name is provided."""
        mock_run.return_value = MagicMock(stdout="old-session\n")

        result = rename_session("%5", new_name="custom-name")

        assert result is True
        mock_run.assert_any_call(
            ["tmux", "rename-session", "-t", "old-session", "custom-name"],
            check=True,
            capture_output=True,
        )

    @patch("subprocess.run")
    def test_returns_false_on_tmux_error(self, mock_run):
        """Should return False when tmux command fails."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "tmux", stderr=b"error")

        result = rename_session("%5", new_name="test")

        assert result is False
