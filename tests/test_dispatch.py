"""Tests for dispatch: branch naming and dispatch-via-Claude."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_hq.capabilities import LauncherCapabilities
from agent_hq.config import Config, DispatchConfig, HarnessDefaults, ProjectConfig, RepoConfig
from agent_hq.dispatch import (
    _find_repo_session,
    _new_split_with_claude,
    _wait_for_harness_process,
    dispatch_via_claude,
    generate_branch_name,
    open_chat_window,
)
from agent_hq.models import Issue, Priority


def _issue(number, title="Test issue", repo="ben-ralston/agent-hq"):
    return Issue(number=number, title=title, repo=repo, status="Todo", priority=Priority.NEED)


# A launcher advertising codex + full --model/--effort flag support, for tests
# that dispatch a non-claude harness or override model/effort -- since #205
# Stage 1B's dispatch_via_claude now gates those on the installed launcher's
# advertised capabilities (defense-in-depth alongside the UI-layer gate).
_CODEX_CAPS = LauncherCapabilities(
    agents=frozenset({"claude", "codex"}),
    flags={"claude": ("--model", "--effort"), "codex": ("--model", "--effort")},
)


def _make_config():
    return Config(
        repos={
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=Path("/Users/dev/agent-hq"),
            ),
        },
        projects=[ProjectConfig(name="Agent HQ", number=12, owner="@me")],
        worktree_root=Path("/Users/dev/.claude-worktrees"),
    )


class TestGenerateBranchName:
    def _issue(self, number, title="Test issue"):
        return Issue(number=number, title=title, repo="o/r", status=None, priority=Priority.NO_PRI)

    def test_single_issue(self):
        result = generate_branch_name([self._issue(42, "Add user auth")])
        assert result == "feature/42-add-user-auth"

    def test_multiple_issues_sorted(self):
        result = generate_branch_name([self._issue(9, "Foo"), self._issue(5, "Bar")])
        assert result.startswith("feature/5-9-")

    def test_special_chars_sanitized(self):
        result = generate_branch_name([self._issue(1, "Fix @user's bug!")])
        assert "@" not in result
        assert "!" not in result
        assert "'" not in result

    def test_title_truncated(self):
        result = generate_branch_name(
            [self._issue(1, "A very long title that should be truncated to something reasonable")]
        )
        assert len(result) < 60


class TestFindRepoSession:
    @patch("agent_hq.dispatch.subprocess.run")
    def test_matches_session_by_path(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="my-session\t/Users/dev/agent-hq\n",
        )
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result == "my-session"

    @patch("agent_hq.dispatch.subprocess.run")
    def test_normalizes_trailing_slash(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="my-session\t/Users/dev/agent-hq/\n",
        )
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result == "my-session"

    @patch("agent_hq.dispatch.subprocess.run")
    def test_no_matching_session_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="other-session\t/Users/dev/other-repo\n",
        )
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result is None

    @patch("agent_hq.dispatch.subprocess.run")
    def test_tmux_not_running_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no server running")
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result is None

    @patch("agent_hq.dispatch.subprocess.run")
    def test_tmux_not_found_returns_none(self, mock_run):
        mock_run.side_effect = FileNotFoundError("tmux not found")
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result is None

    @patch("agent_hq.dispatch.subprocess.run")
    def test_multiple_sessions_returns_matching(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "session-a\t/Users/dev/project-a\n"
                "session-b\t/Users/dev/agent-hq\n"
                "session-c\t/Users/dev/project-c\n"
            ),
        )
        result = _find_repo_session(Path("/Users/dev/agent-hq"))
        assert result == "session-b"

    @patch("agent_hq.dispatch.subprocess.run")
    def test_matches_dot_repo_by_sanitized_name(self, mock_run):
        # A repo dir whose name starts with '.' is created as a tmux session
        # with the leading dot sanitized to '_' (e.g. '.claude' -> '_claude').
        # When the session's reported path has drifted away from the repo
        # path, the name-match fallback must still find it -- using the SAME
        # sanitized name that creation used, not the raw dir name.
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="_claude\t/Users/dev\n",
        )
        result = _find_repo_session(Path("/Users/dev/.claude"))
        assert result == "_claude"


class TestDispatchViaClaude:
    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_happy_path_sends_worktree_create(self, mock_find_session, mock_new_split):
        config = _make_config()
        batches = [[_issue(42, title="Add auth")]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 42"
        assert "/worktree-create 42" in result.branch
        assert result.branch == "(via Claude /worktree-create 42)"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_batched_issues_joined_with_slash(self, mock_find_session, mock_new_split):
        config = _make_config()
        batches = [[_issue(19), _issue(20)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 19/20"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_multiple_ungrouped_batches_same_repo(
        self, mock_find_session, mock_new_split
    ):
        """Two ungrouped same-repo issues become one space-joined command."""
        config = _make_config()
        batches = [[_issue(6)], [_issue(41)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 6 41"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_mixed_grouped_and_ungrouped(self, mock_find_session, mock_new_split):
        """Grouped batch + ungrouped: slash within batch, space between batches."""
        config = _make_config()
        batches = [[_issue(6), _issue(41)], [_issue(50)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 6/41 50"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_multi_batch_splits_only_once(self, mock_find_session, mock_new_split):
        """Multi-batch same-repo dispatch performs one split and one send."""
        config = _make_config()
        batches = [[_issue(6)], [_issue(41)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        mock_new_split.assert_called_once()
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 6 41"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._create_repo_session", return_value="agent-hq")
    @patch("agent_hq.dispatch._find_repo_session", return_value=None)
    def test_creates_session_when_not_found(
        self, mock_find_session, mock_create_session, mock_new_split
    ):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        mock_create_session.assert_called_once()
        mock_new_split.assert_called_once()

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=(None, False))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_returns_error_when_both_split_and_new_window_fail(
        self, mock_find_session, mock_new_split
    ):
        """The error is returned only after the new-window fallback also
        fails; both attempts must have been made (#153). Pinning the
        call count guards against a regression that returns early after
        the first failed split and never tries the fallback.
        """
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert not result.success
        assert "Could not create pane for Claude" in result.error
        assert mock_new_split.call_count == 2
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("new_window") is True

    @patch("agent_hq.dispatch._new_split_with_claude")
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_falls_back_to_new_window_when_split_has_no_space(
        self, mock_find_session, mock_new_split
    ):
        """A crowded session's active pane is too small to halve, so
        split-window fails with 'no space for new pane'. Dispatch must
        retry in a fresh full-screen window rather than failing outright
        (#153).
        """
        # First call (split-window) fails; the new-window retry succeeds.
        mock_new_split.side_effect = [(None, False), ("%1", True)]
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        assert mock_new_split.call_count == 2
        # The retry must request a fresh window so it can't run out of space.
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("new_window") is True
        assert kwargs.get("initial_prompt") == "/worktree-create 42"

    def test_unknown_repo_returns_error(self):
        config = _make_config()
        batches = [[_issue(42, repo="unknown/repo")]]
        result = dispatch_via_claude(batches, config)
        assert not result.success
        assert "No config" in result.error

    def test_empty_batches_returns_error(self):
        config = _make_config()
        result = dispatch_via_claude([], config)
        assert not result.success
        assert "No issues" in result.error

    def test_empty_batch_returns_error(self):
        config = _make_config()
        result = dispatch_via_claude([[]], config)
        assert not result.success
        assert "No issues" in result.error

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%7", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_always_splits_with_repo_root_cwd(self, mock_find_session, mock_new_split):
        """Dispatch always splits a new pane and passes the repo root as cwd.

        Regression for bug where dispatching while a claude was already
        running in the session's pane sent /worktree-create (or the literal
        word 'claude') into the active claude REPL.
        """
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        mock_new_split.assert_called_once()
        args, _kwargs = mock_new_split.call_args
        assert args[0] == "agent-hq"
        assert args[1] == Path("/Users/dev/agent-hq")
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 42"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", False))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_timeout_hard_fails(self, mock_find_session, mock_new_split):
        """No claude process within the timeout is a FAILURE, not a warning
        (#183): success=True-with-dropped-warning rendered 'Started ✓' for
        dispatches that verifiably never reached Claude. The pane is left
        open for inspection and nothing further is sent to it.
        """
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert not result.success
        assert "%1" in result.error
        assert "did not start" in result.error
        # A pane WAS created (claude just never started), so the new-window
        # fallback must NOT fire -- retrying here would orphan a duplicate
        # pane. The fallback gates on "no pane", not "not ready" (#153).
        mock_new_split.assert_called_once()

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", False))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_timeout_marks_the_result_launched(self, mock_find_session, mock_new_split):
        """A readiness timeout is a failure that may STILL create a worktree
        (#59): the ``/worktree-create`` prompt is already on claude's argv by
        the time we start waiting, so a slow boot runs it anyway. Callers
        must be able to tell this apart from "nothing happened" — restoring
        such a selection would invite a duplicate worktree on retry.
        """
        result = dispatch_via_claude([[_issue(42)]], _make_config())
        assert not result.success
        assert result.launched is True

    @patch("agent_hq.dispatch._create_repo_session", return_value=None)
    @patch("agent_hq.dispatch._find_repo_session", return_value=None)
    def test_session_creation_failure_returns_error(self, mock_find_session, mock_create_session):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert not result.success
        assert "Failed to create tmux session" in result.error
        # Nothing was ever sent to a pane, so this IS safely retryable.
        assert result.launched is False

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=(None, False))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_pane_creation_failure_is_not_launched(self, mock_find_session, mock_new_split):
        result = dispatch_via_claude([[_issue(42)]], _make_config())
        assert not result.success
        assert result.launched is False

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_default_prompt_unchanged(self, mock_find_session, mock_new_split):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config)
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create 42"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_codex_harness_in_prompt(self, mock_find_session, mock_new_split):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches, config, harness="codex", capabilities_probe=lambda: _CODEX_CAPS
        )
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create --harness codex 42"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_model_effort_from_config_defaults(self, mock_find_session, mock_new_split):
        config = _make_config()
        config.dispatch = DispatchConfig(
            harnesses={"codex": HarnessDefaults(effort="high")},
        )
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches, config, harness="codex", capabilities_probe=lambda: _CODEX_CAPS
        )
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create --harness codex --effort high 42"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_explicit_overrides_beat_config(self, mock_find_session, mock_new_split):
        config = _make_config()
        config.dispatch = DispatchConfig(
            harnesses={"codex": HarnessDefaults(effort="high")},
        )
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches,
            config,
            harness="codex",
            effort="low",
            capabilities_probe=lambda: _CODEX_CAPS,
        )
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create --harness codex --effort low 42"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_branch_reflects_harness_and_effort(self, mock_find_session, mock_new_split):
        """result.branch feeds the web SSE dispatch.completed event and the
        TUI notify -- it must carry the resolved harness/effort, not just the
        issue numbers, or a codex/high-effort dispatch reports identically to
        a plain claude one.
        """
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches,
            config,
            harness="codex",
            effort="high",
            capabilities_probe=lambda: _CODEX_CAPS,
        )
        assert result.success
        assert "--harness codex --effort high" in result.branch

    @patch("agent_hq.dispatch._find_repo_session")
    def test_invalid_effort_fails_before_any_tmux_call(self, mock_find_session):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config, effort="turbo")
        assert not result.success
        assert "effort" in result.error
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._find_repo_session")
    def test_invalid_harness_fails(self, mock_find_session):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config, harness="aider")
        assert not result.success
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._find_repo_session")
    def test_model_with_whitespace_rejected_before_any_tmux_call(self, mock_find_session):
        """A model value containing whitespace (e.g. "sonnet 999 --effort
        max") would otherwise splice phantom argv into /worktree-create's
        argument parsing. Rejected at the shared resolution boundary
        (resolve_dispatch_selection) before any tmux call is made."""
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config, model="sonnet 999 --effort max")
        assert not result.success
        assert "model" in result.error.lower()
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._find_repo_session")
    def test_model_with_tab_or_newline_rejected(self, mock_find_session):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config, model="o3\tinjected")
        assert not result.success
        assert "model" in result.error.lower()
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_model_without_whitespace_still_accepted(
        self, mock_find_session, mock_new_split
    ):
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches,
            config,
            harness="codex",
            model="o3",
            capabilities_probe=lambda: _CODEX_CAPS,
        )
        assert result.success
        _args, kwargs = mock_new_split.call_args
        assert kwargs.get("initial_prompt") == "/worktree-create --harness codex --model o3 42"


class TestDispatchViaClaudeCapabilitiesGate:
    """#205 Stage 1B final-review fix: the capability gate + flag-granularity
    check now live INSIDE dispatch_via_claude itself (not only at UI
    boundaries), so every caller -- including the inbox-dispatch surface
    that has no boundary-level gate of its own -- is protected."""

    @patch("agent_hq.dispatch._find_repo_session")
    def test_codex_harness_rejected_when_unadvertised(self, mock_find_session):
        from agent_hq.capabilities import CLAUDE_ONLY

        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches, config, harness="codex", capabilities_probe=lambda: CLAUDE_ONLY
        )
        assert not result.success
        assert "codex" in result.error.lower()
        assert "not advertised" in result.error.lower()
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._find_repo_session")
    def test_model_rejected_when_flag_unsupported(self, mock_find_session):
        caps = LauncherCapabilities(agents=frozenset({"claude", "codex"}), flags={"codex": ()})
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches,
            config,
            harness="codex",
            model="o3",
            capabilities_probe=lambda: caps,
        )
        assert not result.success
        assert "--model" in result.error
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._find_repo_session")
    def test_effort_rejected_when_flag_unsupported(self, mock_find_session):
        caps = LauncherCapabilities(
            agents=frozenset({"claude", "codex"}), flags={"codex": ("--model",)}
        )
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(
            batches,
            config,
            harness="codex",
            effort="high",
            capabilities_probe=lambda: caps,
        )
        assert not result.success
        assert "--effort" in result.error
        mock_find_session.assert_not_called()

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_plain_claude_dispatch_never_probes(self, mock_find_session, mock_new_split):
        """No harness/model/effort override -- ClaudeAdapter emits no flags,
        so the capability gate must be skipped entirely (no probe call)."""
        config = _make_config()
        batches = [[_issue(42)]]

        def _boom():
            raise AssertionError("capabilities_probe must not be called")

        result = dispatch_via_claude(batches, config, capabilities_probe=_boom)
        assert result.success

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    @patch("agent_hq.dispatch.probe_launcher_capabilities")
    def test_default_probe_used_when_not_provided(
        self, mock_probe, mock_find_session, mock_new_split
    ):
        """When capabilities_probe isn't passed, dispatch_via_claude falls
        back to the real probe_launcher_capabilities (production default) --
        exercised here via a codex dispatch, which always emits flags."""
        mock_probe.return_value = _CODEX_CAPS
        config = _make_config()
        batches = [[_issue(42)]]
        result = dispatch_via_claude(batches, config, harness="codex")
        assert result.success
        mock_probe.assert_called_once()


class TestResolveDispatchSelection:
    """Direct unit tests for the shared resolution boundary."""

    def test_model_with_whitespace_raises(self):
        from agent_hq.dispatch import resolve_dispatch_selection

        config = _make_config()
        try:
            resolve_dispatch_selection(config, None, "sonnet 999 --effort max", None)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "model" in str(e).lower()
            assert "sonnet 999 --effort max" in str(e)

    def test_model_without_whitespace_passes_through(self):
        from agent_hq.dispatch import resolve_dispatch_selection

        config = _make_config()
        harness, model, effort = resolve_dispatch_selection(config, None, "o3", None)
        assert model == "o3"

    def test_empty_string_model_raises(self):
        """An explicit empty-string model is distinct from None (no
        override) -- reject it too since it's not a usable single token."""
        from agent_hq.dispatch import resolve_dispatch_selection

        config = _make_config()
        try:
            resolve_dispatch_selection(config, None, "", None)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "model" in str(e).lower()

    def test_none_model_is_unaffected(self):
        from agent_hq.dispatch import resolve_dispatch_selection

        config = _make_config()
        harness, model, effort = resolve_dispatch_selection(config, None, None, None)
        assert model is None


class TestWaitForHarnessProcess:
    def _pane(self, pane_id="%1", pane_pid=100):
        return {
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "pane_id": pane_id,
            "pane_pid": pane_pid,
        }

    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command", return_value=("claude", 200))
    @patch("agent_hq.dispatch.build_process_tree", return_value=({100: [200]}, {200: "claude"}))
    @patch("agent_hq.dispatch.get_all_panes")
    def test_true_when_claude_process_found(self, mock_panes, mock_tree, mock_find, mock_sleep):
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1") is True
        mock_sleep.assert_called_once()
        mock_find.assert_called_once_with(100, {100: [200]}, {200: "claude"})

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command", return_value=(None, None))
    @patch("agent_hq.dispatch.build_process_tree", return_value=({}, {100: "zsh"}))
    @patch("agent_hq.dispatch.get_all_panes")
    def test_false_when_no_agent_in_tree(self, mock_panes, mock_tree, mock_find, mock_sleep):
        """A pane whose tree only ever contains the shell times out False —
        the exact false-positive case the glyph check got wrong (#183)."""
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1") is False
        assert mock_sleep.call_count == 2

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command", return_value=("codex", 300))
    @patch("agent_hq.dispatch.build_process_tree", return_value=({100: [300]}, {300: "codex"}))
    @patch("agent_hq.dispatch.get_all_panes")
    def test_false_when_other_agent_in_tree(self, mock_panes, mock_tree, mock_find, mock_sleep):
        """find_agent_command matches ANY known agent; only claude counts for
        the default harness="claude"."""
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1") is False

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.get_all_panes", return_value=[])
    def test_false_when_pane_gone(self, mock_panes, mock_sleep):
        assert _wait_for_harness_process("%1") is False

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command")
    @patch("agent_hq.dispatch.build_process_tree")
    @patch("agent_hq.dispatch.get_all_panes")
    def test_true_on_second_poll(self, mock_panes, mock_tree, mock_find, mock_sleep):
        mock_panes.return_value = [self._pane()]
        mock_tree.side_effect = [({}, {100: "zsh"}), ({100: [200]}, {200: "claude"})]
        mock_find.side_effect = [(None, None), ("claude", 200)]
        assert _wait_for_harness_process("%1") is True
        assert mock_sleep.call_count == 2

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.build_process_tree", return_value=({}, {}))
    @patch("agent_hq.dispatch.get_all_panes")
    def test_false_when_process_tree_empty(self, mock_panes, mock_tree, mock_sleep):
        """ps failure yields ({}, {}) — must keep polling, not crash."""
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1") is False

    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command", return_value=("/usr/bin/codex", 300))
    @patch(
        "agent_hq.dispatch.build_process_tree", return_value=({100: [300]}, {300: "/usr/bin/codex"})
    )
    @patch("agent_hq.dispatch.get_all_panes")
    def test_wait_accepts_codex_when_asked(self, mock_panes, mock_tree, mock_find, mock_sleep):
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1", "codex") is True

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.find_agent_command", return_value=("claude", 200))
    @patch("agent_hq.dispatch.build_process_tree", return_value=({100: [200]}, {200: "claude"}))
    @patch("agent_hq.dispatch.get_all_panes")
    def test_wait_rejects_wrong_harness(self, mock_panes, mock_tree, mock_find, mock_sleep):
        """A claude process in the tree must not satisfy a codex wait."""
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1", "codex") is False

    @patch("agent_hq.dispatch._CLAUDE_READY_TIMEOUT", 2)
    @patch("agent_hq.dispatch.time.sleep")
    @patch("agent_hq.dispatch.get_all_panes")
    def test_unknown_harness_returns_false_without_polling(self, mock_panes, mock_sleep):
        """get_adapter(None) for an unregistered harness must not crash the wait."""
        mock_panes.return_value = [self._pane()]
        assert _wait_for_harness_process("%1", "aider") is False
        mock_sleep.assert_not_called()


class TestNewSplitWithClaude:
    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_splits_session_and_starts_claude(self, mock_run, mock_wait):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),  # split-window prints new pane_id
            MagicMock(returncode=0),  # send-keys
        ]
        mock_wait.return_value = True
        pane_id, was_ready = _new_split_with_claude("my-session", Path("/tmp/repo"), "acceptEdits")
        assert pane_id == "%9"
        assert was_ready is True
        split_call = mock_run.call_args_list[0].args[0]
        assert "split-window" in split_call
        assert "-t" in split_call and "my-session" in split_call
        assert "-c" in split_call and "/tmp/repo" in split_call
        assert "-P" in split_call  # prints new pane id
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call[:3] == ["tmux", "send-keys", "-t"]
        assert "%9" in send_call
        assert "claude --permission-mode acceptEdits" in send_call
        mock_wait.assert_called_once_with("%9", "claude")

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_permission_mode_is_interpolated_into_command(self, mock_run, mock_wait):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = True
        _new_split_with_claude("my-session", Path("/tmp/repo"), "default")
        send_call = mock_run.call_args_list[1].args[0]
        assert "claude --permission-mode default" in send_call

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_permission_mode_is_shell_quoted(self, mock_run, mock_wait):
        """shlex.quote keeps the mode value a single shell token.

        load_config rejects invalid modes, but this is defense-in-depth against
        anyone constructing the call programmatically with a weird string.
        """
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = True
        _new_split_with_claude("my-session", Path("/tmp/repo"), "weird; rm -rf /")
        send_call = mock_run.call_args_list[1].args[0]
        claude_cmd = send_call[4]
        assert claude_cmd == "claude --permission-mode 'weird; rm -rf /'"

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_new_window_mode_uses_tmux_new_window(self, mock_run, mock_wait):
        """`new_window=True` creates a fresh full-screen window in the session
        instead of splitting the current one -- issue dispatch's fallback
        when the active pane has "no space for new pane" (#153).
        """
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = True
        _new_split_with_claude("agent-hq", Path("/tmp/repo"), "acceptEdits", new_window=True)
        cmd = mock_run.call_args_list[0].args[0]
        assert "new-window" in cmd
        assert "split-window" not in cmd
        assert "-t" in cmd and "agent-hq" in cmd
        assert "-c" in cmd and "/tmp/repo" in cmd
        assert "-P" in cmd  # prints new pane id

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_returns_false_ready_on_timeout(self, mock_run, mock_wait):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = False
        pane_id, was_ready = _new_split_with_claude("my-session", Path("/tmp/repo"), "acceptEdits")
        assert pane_id == "%9"
        assert was_ready is False

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_initial_prompt_is_argv_passed_and_quoted(self, mock_run, mock_wait):
        """The slash command rides claude's argv (like the worktree skill's
        claude-entry.sh) so it can never be typed into a bare shell (#183)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = True
        _new_split_with_claude(
            "my-session",
            Path("/tmp/repo"),
            "acceptEdits",
            initial_prompt="/worktree-create 6 41",
        )
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call[4] == ("claude --permission-mode acceptEdits '/worktree-create 6 41'")

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_send_keys_failure_reports_not_ready(self, mock_run, mock_wait):
        """If the launch line never reached the pane, don't wait and don't
        claim readiness — callers hard-fail with the pane left open."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=1, stderr="can't find pane"),
        ]
        pane_id, was_ready = _new_split_with_claude("s", Path("/tmp/repo"), "default")
        assert pane_id == "%9"
        assert was_ready is False
        mock_wait.assert_not_called()

    @patch("agent_hq.dispatch._wait_for_harness_process")
    @patch("agent_hq.dispatch.subprocess.run")
    def test_new_split_codex_command_line(self, mock_run, mock_wait):
        """harness="codex" launches bare `codex` — no --permission-mode flag,
        since codex's repl_command doesn't take one (#205 Stage 1B)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        mock_wait.return_value = True
        pane_id, was_ready = _new_split_with_claude(
            "my-session", Path("/tmp/repo"), "acceptEdits", harness="codex"
        )
        assert pane_id == "%9"
        assert was_ready is True
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call[4] == "agent-hq codex --"
        mock_wait.assert_called_once_with("%9", "codex")

    @patch("agent_hq.dispatch.subprocess.run")
    def test_split_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no session")
        pane_id, was_ready = _new_split_with_claude("my-session", Path("/tmp/repo"), "acceptEdits")
        assert pane_id is None
        assert was_ready is False

    @patch("agent_hq.dispatch.subprocess.run", side_effect=FileNotFoundError)
    def test_tmux_not_found_returns_none(self, mock_run):
        pane_id, was_ready = _new_split_with_claude("my-session", Path("/tmp/repo"), "acceptEdits")
        assert pane_id is None
        assert was_ready is False


class TestOpenChatWindow:
    @patch("agent_hq.dispatch.subprocess.run")
    def test_returns_none_when_send_keys_fails(self, mock_run):
        """A pane whose claude launch line was never delivered must not be
        reported as a started chat (#183 / AHQ-016)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%3\n"),  # new-window
            MagicMock(returncode=1, stderr="can't find pane"),  # send-keys
        ]
        pane = open_chat_window(
            existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp")
        )
        assert pane is None

    @patch("agent_hq.dispatch.subprocess.run")
    def test_existing_session_opens_new_window(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),  # new-window prints pane id
            MagicMock(returncode=0),  # send-keys
        ]
        pane_id = open_chat_window(
            existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        assert pane_id == "%9"
        cmd = mock_run.call_args_list[0].args[0]
        assert "new-window" in cmd
        assert "new-session" not in cmd
        assert "-t" in cmd and "scratch" in cmd
        assert "-c" in cmd and "/tmp/scratch" in cmd
        assert "-P" in cmd and "#{pane_id}" in cmd

    @patch("agent_hq.dispatch.subprocess.run")
    def test_no_session_creates_detached_session_using_initial_pane(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%3\n"),  # new-session prints initial pane id
            MagicMock(returncode=0),  # send-keys
        ]
        pane_id = open_chat_window(
            existing_session=None, new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        assert pane_id == "%3"
        cmd = mock_run.call_args_list[0].args[0]
        assert "new-session" in cmd
        assert "new-window" not in cmd
        assert "-d" in cmd
        assert "-s" in cmd and "scratch" in cmd
        assert "-c" in cmd and "/tmp/scratch" in cmd
        assert "-P" in cmd and "#{pane_id}" in cmd

    @patch("agent_hq.dispatch.subprocess.run")
    def test_starts_bare_claude_in_new_pane(self, mock_run):
        """The chat window runs plain `claude` — no permission mode, no prompt."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        open_chat_window(
            existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call == ["tmux", "send-keys", "-t", "%9", "claude", "Enter"]

    @patch("agent_hq.dispatch.subprocess.run")
    def test_open_chat_window_codex(self, mock_run):
        """harness="codex" sends bare `codex` instead of `claude`."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        pane_id = open_chat_window(
            existing_session="scratch",
            new_session_name="scratch",
            cwd=Path("/tmp/scratch"),
            harness="codex",
        )
        assert pane_id == "%9"
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call == ["tmux", "send-keys", "-t", "%9", "agent-hq codex --", "Enter"]

    @patch("agent_hq.dispatch.subprocess.run")
    def test_model_effort_prompt_ride_the_launch_line(self, mock_run):
        """#290: model/effort/prompt join the harness argv, shell-quoted, so
        the prompt is submitted by the harness itself at startup (no
        keystrokes into a booting REPL -- the #183 lesson)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        open_chat_window(
            existing_session="scratch",
            new_session_name="scratch",
            cwd=Path("/tmp/scratch"),
            model="opus",
            effort="high",
            prompt="/clean-gone --all; don't touch main",
        )
        send_call = mock_run.call_args_list[1].args[0]
        assert send_call == [
            "tmux",
            "send-keys",
            "-t",
            "%9",
            "claude --model opus --effort high -- '/clean-gone --all; don'\"'\"'t touch main'",
            "Enter",
        ]

    @patch("agent_hq.dispatch.subprocess.run")
    def test_creation_failure_returns_none_and_skips_send_keys(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        pane_id = open_chat_window(
            existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        assert pane_id is None
        assert mock_run.call_count == 1

    @patch("agent_hq.dispatch.subprocess.run")
    def test_empty_stdout_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        pane_id = open_chat_window(
            existing_session=None, new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        assert pane_id is None
        assert mock_run.call_count == 1

    @patch("agent_hq.dispatch.subprocess.run", side_effect=FileNotFoundError)
    def test_tmux_not_found_returns_none(self, mock_run):
        pane_id = open_chat_window(
            existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp/scratch")
        )
        assert pane_id is None


class TestDispatchControllerDefaults:
    """#232: the issue-dispatch controller pane pins Opus + high effort + auto.

    The controller is the claude session Agent HQ starts to run
    ``/worktree-create``. It used to inherit the user's default model and
    ``config.dispatch.permission_mode`` (``acceptEdits``). Every issue-dispatch
    surface (web, MCP, TUI, inbox background task) funnels through
    ``dispatch_via_claude``, so pinning it here covers all of them.
    """

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_controller_launches_opus_high_auto(self, mock_find_session, mock_new_split):
        result = dispatch_via_claude([[_issue(42)]], _make_config())
        assert result.success
        args, kwargs = mock_new_split.call_args
        assert args[2] == "auto"
        assert kwargs.get("model") == "opus"
        assert kwargs.get("effort") == "high"

    @patch("agent_hq.dispatch._new_split_with_claude", return_value=("%1", True))
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_configured_permission_mode_does_not_downgrade_controller(
        self, mock_find_session, mock_new_split
    ):
        """The issue-dispatch controller is pinned to auto regardless of
        dispatch.permission_mode (#232)."""
        config = _make_config()
        config.dispatch = DispatchConfig(permission_mode="acceptEdits")
        dispatch_via_claude([[_issue(42)]], config)
        args, _kwargs = mock_new_split.call_args
        assert args[2] == "auto"

    @patch(
        "agent_hq.dispatch._new_split_with_claude",
        side_effect=[(None, False), ("%1", True)],
    )
    @patch("agent_hq.dispatch._find_repo_session", return_value="agent-hq")
    def test_new_window_fallback_keeps_the_same_defaults(self, mock_find_session, mock_new_split):
        """The 'no space for new pane' fallback must not drop the flags."""
        result = dispatch_via_claude([[_issue(42)]], _make_config())
        assert result.success
        args, kwargs = mock_new_split.call_args
        assert kwargs.get("new_window") is True
        assert args[2] == "auto"
        assert kwargs.get("model") == "opus"
        assert kwargs.get("effort") == "high"

    @patch("agent_hq.dispatch._wait_for_harness_process", return_value=True)
    @patch("agent_hq.dispatch.subprocess.run")
    def test_launch_line_carries_flags(self, mock_run, mock_wait):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        _new_split_with_claude("my-session", Path("/tmp/repo"), "auto", model="opus", effort="high")
        claude_cmd = mock_run.call_args_list[1].args[0][4]
        assert claude_cmd == "claude --permission-mode auto --model opus --effort high"

    @patch("agent_hq.dispatch._wait_for_harness_process", return_value=True)
    @patch("agent_hq.dispatch.subprocess.run")
    def test_launch_line_without_model_unchanged(self, mock_run, mock_wait):
        """No model/effort passed -> the pre-#232 line."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="%9\n"),
            MagicMock(returncode=0),
        ]
        _new_split_with_claude("my-session", Path("/tmp/repo"), "acceptEdits")
        claude_cmd = mock_run.call_args_list[1].args[0][4]
        assert claude_cmd == "claude --permission-mode acceptEdits"


@patch("agent_hq.dispatch.subprocess.run")
def test_chat_window_quotes_equals_leading_words(mock_run):
    """zsh's EQUALS option expands an unquoted leading ``=word`` to a command
    path; shlex treats '=' as safe, so the launch line must force-quote it
    (review round 1, #290)."""
    mock_run.side_effect = [MagicMock(returncode=0, stdout="%9\n"), MagicMock(returncode=0)]
    open_chat_window(
        existing_session="scratch", new_session_name="scratch", cwd=Path("/tmp"), prompt="=ls"
    )
    assert mock_run.call_args_list[1].args[0][4] == "claude -- '=ls'"
