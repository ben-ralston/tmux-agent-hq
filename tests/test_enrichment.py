"""Tests for agent enrichment module."""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from agent_hq.enrichment import (
    _clear_duplicate_codex_thread_ids,
    compute_content_fingerprint,
    detect_claude_state,
    detect_codex_state,
    enrich_agent,
    get_agent_state,
    get_claude_native_name,
    get_claude_session_ids,
    get_claude_session_slug,
    get_codex_thread_id,
    get_git_branch,
    get_git_context,
    get_git_repo_root,
    get_pane_content,
    get_pane_cwd,
    get_repo_name,
    get_worktree_info,
    strip_ansi,
)
from agent_hq.models import Agent, AgentState, AgentType


def _init_git_repo(path, *, base_branch="main", remote_url=None):
    """Initialize a git repo with one commit. Optionally configure origin URL."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", base_branch], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if remote_url is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", remote_url],
            cwd=path,
            check=True,
            capture_output=True,
        )
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def _git(path, *args):
    """Shorthand for running a git command in ``path``."""
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


class TestGetPaneCwd:
    """Tests for get_pane_cwd function."""

    def test_gets_pane_cwd(self):
        """Should get current working directory of pane."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "/Users/test/project\n"
            mock_run.return_value.returncode = 0

            cwd = get_pane_cwd("%5")

        assert cwd == "/Users/test/project"
        mock_run.assert_called_once()

    def test_returns_empty_on_error(self):
        """Should return empty string on error."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            cwd = get_pane_cwd("%5")

        assert cwd == ""


class TestGetGitRepoRoot:
    """Tests for get_git_repo_root function."""

    def test_gets_repo_root(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        sub = repo / "src"
        sub.mkdir()

        # Resolve to handle macOS /private/tmp vs /tmp differences
        assert get_git_repo_root(str(sub)) == str(repo.resolve())

    def test_returns_none_for_non_git(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        assert get_git_repo_root(str(not_a_repo)) is None


class TestGetGitBranch:
    """Tests for get_git_branch function."""

    def test_gets_branch_name(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        _git(repo, "checkout", "-b", "feature/test")
        assert get_git_branch(str(repo)) == "feature/test"

    def test_returns_none_for_non_git(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        assert get_git_branch(str(not_a_repo)) is None


class TestGetRepoName:
    """Tests for get_repo_name function."""

    def test_parses_https_url(self, tmp_path):
        repo = _init_git_repo(
            tmp_path / "project", remote_url="https://github.com/user/my-repo.git"
        )
        assert get_repo_name(str(repo)) == "my-repo"

    def test_parses_ssh_url(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project", remote_url="git@github.com:user/my-repo.git")
        assert get_repo_name(str(repo)) == "my-repo"

    def test_parses_url_without_git_suffix(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project", remote_url="https://github.com/user/my-repo")
        assert get_repo_name(str(repo)) == "my-repo"

    def test_fallback_to_directory_name_when_no_remote(self, tmp_path):
        repo = _init_git_repo(tmp_path / "my-project")
        assert get_repo_name(str(repo)) == "my-project"

    def test_fallback_to_directory_name_for_non_git(self, tmp_path):
        not_a_repo = tmp_path / "my-project"
        not_a_repo.mkdir()
        assert get_repo_name(str(not_a_repo)) == "my-project"


class TestGetWorktreeInfo:
    """Tests for get_worktree_info function."""

    def test_main_worktree(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        main_repo, wt_name, is_linked = get_worktree_info(str(repo))
        assert main_repo == str(repo.resolve())
        assert wt_name is None
        assert is_linked is False

    def test_main_worktree_subdir(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        sub = repo / "src"
        sub.mkdir()
        main_repo, wt_name, is_linked = get_worktree_info(str(sub))
        assert main_repo == str(repo.resolve())
        assert wt_name is None
        assert is_linked is False

    def test_linked_worktree(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        # Create a second branch and a linked worktree on it
        _git(repo, "branch", "feature")
        wt_path = tmp_path / "project-feature"
        _git(repo, "worktree", "add", str(wt_path), "feature")

        main_repo, wt_name, is_linked = get_worktree_info(str(wt_path))
        assert main_repo == str(repo.resolve())
        assert wt_name == "project-feature"
        assert is_linked is True

    def test_returns_none_for_non_git(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        main_repo, wt_name, is_linked = get_worktree_info(str(not_a_repo))
        assert main_repo is None
        assert wt_name is None
        assert is_linked is False


class TestGetGitContext:
    """Tests for get_git_context function."""

    def test_returns_context_for_git_dir(self):
        """Should return full git context for git directory."""
        with (
            patch("agent_hq.enrichment.get_git_repo_root") as mock_root,
            patch("agent_hq.enrichment.get_git_branch") as mock_branch,
            patch("agent_hq.enrichment.get_worktree_info") as mock_worktree,
        ):
            mock_root.return_value = "/Users/test/project"
            mock_branch.return_value = "main"
            # New signature: (main_repo_root, worktree_name, is_linked)
            mock_worktree.return_value = ("/Users/test/project", None, False)

            repo_root, branch, worktree_name = get_git_context("/Users/test/project/src")

        assert repo_root == "/Users/test/project"
        assert branch == "main"
        assert worktree_name is None

    def test_returns_none_for_non_git(self):
        """Should return None tuple for non-git directory."""
        with patch("agent_hq.enrichment.get_git_repo_root") as mock_root:
            mock_root.return_value = None

            repo_root, branch, worktree_name = get_git_context("/tmp")

        assert repo_root is None
        assert branch is None
        assert worktree_name is None

    def test_returns_none_for_empty_cwd(self):
        """Should return None tuple for empty cwd."""
        repo_root, branch, worktree_name = get_git_context("")

        assert repo_root is None
        assert branch is None
        assert worktree_name is None


class TestEnrichAgent:
    """Tests for enrich_agent function."""

    def test_enriches_raw_agent(self):
        """Should enrich raw agent data with cwd and git context."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
        }

        with (
            patch("agent_hq.enrichment.get_pane_cwd") as mock_cwd,
            patch("agent_hq.enrichment.get_git_context") as mock_context,
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
        ):
            mock_cwd.return_value = "/Users/test/project"
            mock_context.return_value = ("/Users/test/project", "main", None)
            mock_state.return_value = AgentState.IDLE

            agent = enrich_agent(raw)

        assert agent.pane_id == "%5"
        assert agent.session_name == "main"
        assert agent.window_index == 1
        assert agent.pane_index == 0
        assert agent.agent_type == AgentType.CLAUDE
        assert agent.cwd == "/Users/test/project"
        assert agent.repo_root == "/Users/test/project"
        assert agent.branch == "main"
        assert agent.worktree_name is None
        assert agent.raw_command == "claude"
        assert agent.state == AgentState.UNKNOWN

    def test_sets_session_slug_for_claude(self):
        """Should populate session_slug for Claude agents."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
            "agent_pid": 5678,
        }

        with (
            patch("agent_hq.enrichment.get_pane_cwd") as mock_cwd,
            patch("agent_hq.enrichment.get_git_context") as mock_context,
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_slug") as mock_slug,
        ):
            mock_cwd.return_value = "/Users/test/project"
            mock_context.return_value = ("/Users/test/project", "main", None)
            mock_state.return_value = AgentState.IDLE
            mock_slug.return_value = "my-session-name"

            agent = enrich_agent(raw)

        assert agent.session_slug == "my-session-name"
        mock_slug.assert_called_once_with(5678, "/Users/test/project")

    def test_sets_claude_session_id_for_claude(self):
        """Should populate claude_session_id (bridgeSessionId) for Claude agents."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
            "agent_pid": 5678,
        }

        with (
            patch("agent_hq.enrichment.get_pane_cwd") as mock_cwd,
            patch("agent_hq.enrichment.get_git_context") as mock_context,
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_slug") as mock_slug,
            patch("agent_hq.enrichment.get_claude_session_ids") as mock_ids,
        ):
            mock_cwd.return_value = "/Users/test/project"
            mock_context.return_value = ("/Users/test/project", "main", None)
            mock_state.return_value = AgentState.IDLE
            mock_slug.return_value = None
            mock_ids.return_value = ("session_01Tk4ztYgtWAhHHvE7uh5ghh", "transcript-abc-123")

            agent = enrich_agent(raw)

        assert agent.claude_session_id == "session_01Tk4ztYgtWAhHHvE7uh5ghh"
        mock_ids.assert_called_once_with(5678)

    def test_skips_claude_session_id_for_non_claude(self):
        """Should not call get_claude_session_ids for non-Claude agents."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "aider",
            "agent_pid": 5678,
        }

        with (
            patch("agent_hq.enrichment.get_pane_cwd") as mock_cwd,
            patch("agent_hq.enrichment.get_git_context") as mock_context,
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_ids") as mock_ids,
        ):
            mock_cwd.return_value = "/Users/test/project"
            mock_context.return_value = ("/Users/test/project", "main", None)
            mock_state.return_value = AgentState.UNKNOWN

            agent = enrich_agent(raw)

        assert agent.claude_session_id is None
        mock_ids.assert_not_called()

    def test_skips_session_slug_for_non_claude(self):
        """Should not call get_claude_session_slug for non-Claude agents."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "aider",
            "agent_pid": 5678,
        }

        with (
            patch("agent_hq.enrichment.get_pane_cwd") as mock_cwd,
            patch("agent_hq.enrichment.get_git_context") as mock_context,
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_slug") as mock_slug,
        ):
            mock_cwd.return_value = "/Users/test/project"
            mock_context.return_value = ("/Users/test/project", "main", None)
            mock_state.return_value = AgentState.UNKNOWN

            agent = enrich_agent(raw)

        assert agent.session_slug is None
        mock_slug.assert_not_called()


class TestEnrichAgentPhase:
    """enrich_agent attaches file_stats when repo context is present.

    Phase resolution moved to ``agent_hq.resolver.resolve_phase_snapshot``
    in #73 — enrichment no longer detects phase; the daemon, watcher,
    and live-fallback all route through the resolver to build
    ``phase_snapshot``. ``enrich_agent`` leaves ``phase_snapshot=None``."""

    def test_leaves_phase_snapshot_none_for_resolver_to_fill(self):
        from unittest.mock import patch

        from agent_hq.enrichment import enrich_agent
        from agent_hq.models import FileStats

        raw = {
            "pane_id": "%7",
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "command": "claude",
            "agent_pid": 123,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/repo/wt"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/repo", "feat/1", "wt"),
            ),
            patch("agent_hq.enrichment.get_repo_name", return_value="repo"),
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
            patch(
                "agent_hq.enrichment.git_diff_shortstat",
                return_value=FileStats(2, 15, 3),
            ),
            patch("agent_hq.enrichment.get_pane_content", return_value=""),
        ):
            from agent_hq.models import AgentState

            mock_state.return_value = AgentState.RUNNING

            agent = enrich_agent(raw)

        # Resolver-built; enrichment doesn't populate phase any more.
        assert agent.phase_snapshot is None
        assert agent.phase is None
        # file_stats still produced by enrichment (it has the repo + branch).
        assert agent.file_stats == FileStats(2, 15, 3)

    def test_phase_is_none_without_repo(self):
        from unittest.mock import patch

        from agent_hq.enrichment import enrich_agent

        raw = {
            "pane_id": "%7",
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "command": "claude",
            "agent_pid": 123,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/not-a-repo"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
        ):
            from agent_hq.models import AgentState

            mock_state.return_value = AgentState.IDLE
            agent = enrich_agent(raw)

        assert agent.phase is None
        assert agent.file_stats is None

    def test_enrich_agent_does_not_fetch_pane_content_for_phase(self):
        """Phase detection (and its pane-content QA probe) moved to the
        resolver in #73 — ``enrich_agent`` no longer touches pane
        content for phase. ``get_agent_state`` handles state via the
        hook file / regex; with it mocked, ``enrich_agent`` makes zero
        ``get_pane_content`` calls."""
        from unittest.mock import patch

        from agent_hq.enrichment import enrich_agent
        from agent_hq.models import AgentState, FileStats

        raw = {
            "pane_id": "%7",
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "command": "claude",
            "agent_pid": 123,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/repo/wt"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/repo", "feat/1", "wt"),
            ),
            patch("agent_hq.enrichment.get_repo_name", return_value="repo"),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.RUNNING),
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
            patch(
                "agent_hq.enrichment.git_diff_shortstat",
                return_value=FileStats(1, 5, 2),
            ),
            patch("agent_hq.enrichment.get_pane_content") as mock_pane,
        ):
            enrich_agent(raw)

        mock_pane.assert_not_called()


class TestGetClaudeSessionSlug:
    """Tests for get_claude_session_slug function."""

    def test_returns_name_from_session_file(self):
        """Should return name field from session JSON when present."""
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project",'
            '"startedAt":1000,"name":"worktrunk-evaluation-spec"}'
        )

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file_session = create_mock_open(read_data=session_json)()
            mock_open.side_effect = [mock_file_session]

            slug = get_claude_session_slug(5678, "/Users/test/project")

        assert slug == "worktrunk-evaluation-spec"

    def test_prefers_agent_name_over_slug(self):
        """Should return agentName when available (user-visible session name)."""
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project","startedAt":1000}'
        )
        jsonl_lines = (
            '{"type":"progress"}\n'
            '{"type":"user","slug":"tingly-cooking-hearth","agentName":"my-feature","sessionId":"abc-123"}\n'
        )

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file_session = create_mock_open(read_data=session_json)()
            mock_file_jsonl = create_mock_open(read_data=jsonl_lines)()
            mock_open.side_effect = [mock_file_session, mock_file_jsonl]

            slug = get_claude_session_slug(5678, "/Users/test/project")

        assert slug == "my-feature"

    def test_returns_none_when_no_agent_name(self):
        """Should return None for sessions without agentName (ignores slug)."""
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project","startedAt":1000}'
        )
        jsonl_lines = (
            '{"type":"progress"}\n'
            '{"type":"user","slug":"tingly-cooking-hearth","sessionId":"abc-123"}\n'
        )

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file_session = create_mock_open(read_data=session_json)()
            mock_file_jsonl = create_mock_open(read_data=jsonl_lines)()
            mock_open.side_effect = [mock_file_session, mock_file_jsonl]

            slug = get_claude_session_slug(5678, "/Users/test/project")

        assert slug is None

    def test_returns_none_when_session_file_missing(self):
        """Should return None when session file doesn't exist."""
        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = False

            slug = get_claude_session_slug(5678, "/Users/test/project")

        assert slug is None

    def test_returns_none_when_no_slug_in_jsonl(self):
        """Should return None when JSONL has no agentName field."""
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project","startedAt":1000}'
        )
        jsonl_lines = '{"type":"progress"}\n{"type":"progress"}\n'

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file_session = create_mock_open(read_data=session_json)()
            mock_file_jsonl = create_mock_open(read_data=jsonl_lines)()
            mock_open.side_effect = [mock_file_session, mock_file_jsonl]

            slug = get_claude_session_slug(5678, "/Users/test/project")

        assert slug is None

    def test_returns_none_when_agent_pid_is_none(self):
        """Should return None when agent_pid is None."""
        slug = get_claude_session_slug(None, "/Users/test/project")
        assert slug is None

    def test_falls_back_to_legacy_transcript_path_for_underscore_cwd(self, tmp_path):
        """#180: a transcript directory created before Claude Code started
        replacing ``_`` keeps its underscore-preserving name on disk
        forever. Enrichment counterpart to the resolver's equivalent
        legacy-fallback regression test — real files on disk (rather
        than a blanket ``Path.exists`` mock, which can't distinguish
        between the two candidate paths) so the fallback loop is
        exercised end-to-end."""
        sessions_dir = tmp_path / "sessions"
        projects_dir = tmp_path / "projects"
        sessions_dir.mkdir()
        projects_dir.mkdir()

        session_cwd = "/Users/x/agent_hq"
        session_id = "sid-legacy"
        session_file = sessions_dir / "5678.json"
        session_file.write_text(
            json.dumps(
                {"pid": 5678, "sessionId": session_id, "cwd": session_cwd, "startedAt": 1000}
            )
        )

        # Only the legacy (underscore-preserving) transcript dir exists —
        # the current-scheme dir was never created for this session.
        legacy_dir = projects_dir / "-Users-x-agent_hq"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / f"{session_id}.jsonl").write_text(
            json.dumps(
                {"type": "user", "slug": "s", "agentName": "legacy-name", "sessionId": session_id}
            )
        )

        with (
            patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions_dir),
            patch("agent_hq.transcript.CLAUDE_PROJECTS_DIR", projects_dir),
        ):
            slug = get_claude_session_slug(5678, session_cwd)

        assert slug == "legacy-name"


class TestGetClaudeSessionIds:
    """Tests for ``get_claude_session_ids`` — returns ``(bridge_id, transcript_id)``.

    Bridge id powers override session-scoping; transcript id powers
    hook-state session-scoping. Older Claude Code versions may write
    only one — callers tolerate ``None`` on either slot.
    """

    def test_returns_both_ids_when_present(self):
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project",'
            '"startedAt":1000,"bridgeSessionId":"session_01Tk4ztYgtWAhHHvE7uh5ghh"}'
        )

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file = create_mock_open(read_data=session_json)()
            mock_open.return_value = mock_file

            bridge, transcript = get_claude_session_ids(5678)

        assert bridge == "session_01Tk4ztYgtWAhHHvE7uh5ghh"
        assert transcript == "abc-123"

    def test_returns_transcript_only_when_no_bridge(self):
        """Older Claude Code writes ``sessionId`` but not ``bridgeSessionId``.

        Bridge slot is None (no override-session-scoping possible);
        transcript slot still flows through to hook-state scoping."""
        session_json = (
            '{"pid":5678,"sessionId":"abc-123","cwd":"/Users/test/project","startedAt":1000}'
        )

        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file = create_mock_open(read_data=session_json)()
            mock_open.return_value = mock_file

            bridge, transcript = get_claude_session_ids(5678)

        assert bridge is None
        assert transcript == "abc-123"

    def test_returns_none_pair_when_session_file_missing(self):
        with patch("pathlib.Path.exists") as mock_exists:
            mock_exists.return_value = False

            result = get_claude_session_ids(5678)

        assert result == (None, None)

    def test_returns_none_pair_when_pid_is_none(self):
        assert get_claude_session_ids(None) == (None, None)

    def test_returns_none_pair_on_invalid_json(self):
        with (
            patch("builtins.open") as mock_open,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_exists.return_value = True

            from unittest.mock import mock_open as create_mock_open

            mock_file = create_mock_open(read_data="not json")()
            mock_open.return_value = mock_file

            result = get_claude_session_ids(5678)

        assert result == (None, None)


class TestEnrichmentReadOrder:
    """The enrichment pass MUST read Claude session ids BEFORE calling
    ``get_agent_state`` — without this order, the state read can't
    session-scope its hook file lookup, and a stale state file from a
    prior session in the same pane number could surface as live state.
    """

    def test_session_ids_read_before_get_agent_state(self):
        """Capture the call order and assert ids land before the state read."""

        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
            "agent_pid": 5678,
        }
        call_order: list[str] = []

        def fake_state(pane_id, agent_type, *, current_session_id=None, agent_pid=None):
            call_order.append(f"state(current_session_id={current_session_id!r})")
            return AgentState.IDLE

        def fake_ids(_pid):
            call_order.append("ids")
            return ("bridge-X", "transcript-Y")

        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/p"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/p", "main", None),
            ),
            patch("agent_hq.enrichment.get_agent_state", side_effect=fake_state),
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
            patch("agent_hq.enrichment.get_claude_session_ids", side_effect=fake_ids),
        ):
            agent = enrich_agent(raw)

        # ``ids`` must come before ``state(...)`` so the latter receives
        # the transcript id for session scoping.
        assert call_order == ["ids", "state(current_session_id='transcript-Y')"]
        assert agent.claude_session_id == "bridge-X"
        assert agent.claude_transcript_session_id == "transcript-Y"


class TestGetPaneContent:
    """Tests for get_pane_content function."""

    def test_gets_pane_content(self):
        """Should capture content from tmux pane."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "line1\nline2\nline3\nline4\nline5\n"
            mock_run.return_value.returncode = 0

            content = get_pane_content("%5", lines=3)

        # Last 3 lines of split output: line4, line5, and empty string after final \n
        assert "line4" in content
        assert "line5" in content

    def test_returns_empty_on_error(self):
        """Should return empty string on error."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            content = get_pane_content("%5")

        assert content == ""

    def test_ansi_false_excludes_escape_flag(self):
        """Default ansi=False should NOT pass -e to tmux capture-pane."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content\n"
            mock_run.return_value.returncode = 0

            get_pane_content("%5")

        cmd = mock_run.call_args[0][0]
        assert "-e" not in cmd

    def test_default_excludes_scrollback_flag(self):
        """Default ansi=False should NOT pass -S flag (captures visible viewport only)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content\n"
            mock_run.return_value.returncode = 0

            get_pane_content("%5", lines=500)

        cmd = mock_run.call_args[0][0]
        assert "-S" not in cmd

    def test_ansi_true_captures_scrollback_via_start_line(self):
        """ansi=True should pass -S flag to capture tmux scrollback history."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content\n"
            mock_run.return_value.returncode = 0

            get_pane_content("%5", lines=500, ansi=True)

        cmd = mock_run.call_args[0][0]
        assert "-S" in cmd
        assert "-500" in cmd

    def test_default_preserves_trailing_whitespace(self):
        """Default ansi=False should NOT strip trailing whitespace (fingerprint stability)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content   \nmore   \n"
            mock_run.return_value.returncode = 0

            result = get_pane_content("%5", lines=5)

        assert "content   " in result

    def test_ansi_true_strips_trailing_whitespace(self):
        """ansi=True should strip trailing whitespace (tmux pads lines)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content   \nmore   \n"
            mock_run.return_value.returncode = 0

            result = get_pane_content("%5", lines=5, ansi=True)

        assert "content   " not in result
        assert "content" in result

    def test_ansi_true_includes_escape_flag(self):
        """ansi=True should pass -e to tmux capture-pane before -t flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "content\n"
            mock_run.return_value.returncode = 0

            get_pane_content("%5", ansi=True)

        cmd = mock_run.call_args[0][0]
        assert "-e" in cmd
        # -e must come before -t, not between -t and pane_id
        assert cmd.index("-e") < cmd.index("-t")

    def test_ansi_true_preserves_escape_sequences_in_output(self):
        """ansi=True should return content with ANSI codes intact."""
        ansi_content = "\x1b[32mgreen text\x1b[0m\n\x1b[1mbold\x1b[0m\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = ansi_content
            mock_run.return_value.returncode = 0

            content = get_pane_content("%5", lines=5, ansi=True)

        assert "\x1b[32m" in content
        assert "\x1b[1m" in content


class TestStripAnsi:
    """Tests for strip_ansi function."""

    def test_strips_color_codes(self):
        """Should remove ANSI color codes."""
        text = "\x1b[32mgreen\x1b[0m text"
        result = strip_ansi(text)
        assert result == "green text"

    def test_strips_cursor_codes(self):
        """Should remove cursor movement codes."""
        text = "\x1b[2Jcleared screen"
        result = strip_ansi(text)
        assert result == "cleared screen"

    def test_preserves_plain_text(self):
        """Should preserve text without ANSI codes."""
        text = "plain text without codes"
        result = strip_ansi(text)
        assert result == text


class TestDetectClaudeState:
    """Tests for detect_claude_state function."""

    def test_detects_idle_state(self):
        """Should detect idle state from prompt."""
        content = "Some output\n> \nmore stuff"
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_with_insert_mode(self):
        """Should detect idle from INSERT mode indicator."""
        content = "Some output\n> \n-- INSERT --\n"
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_with_leading_prompt(self):
        """Should detect idle when line starts with >."""
        content = "Previous output\n> waiting for input\nother lines"
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_detects_running_with_new_status_line_format(self):
        """Should detect running from new Claude Code UI without 'esc to interrupt'."""
        content = """
✶ Meandering… (43s · ↓ 543 tokens)

───────────────
❯
───────────────
  █████░░░░░ 54% | widgets-feature-31-richtext-format
  -- INSERT -- ⏵⏵ accept edits on (shift+tab to cycle)
"""
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_status_line_no_timing(self):
        """Should detect running when status line has no timing info."""
        content = "Some output\n✶ Meandering…\n>\n-- INSERT --"
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_does_not_false_positive_on_star_in_output(self):
        """Should NOT detect running when star characters appear without ellipsis."""
        content = """Here is a list:
✶ Item one
✶ Item two
>
-- INSERT --"""
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_detects_running_with_all_status_char_variants(self):
        """Should detect running for all known Claude Code status characters."""
        for char in "·✢✳✶✻✽":
            content = f"{char} Thinking… (10s · ↑ 100 tokens)\n>\n-- INSERT --"
            state = detect_claude_state(content)
            assert state == AgentState.RUNNING, f"Failed for status char: {char}"

    def test_detects_running_with_multi_word_status_line(self):
        """Should detect running when status line has multi-word task description."""
        content = "✻ Fixing serializer NS.data index… (1m 17s · ↓ 2.4k tokens)\n>\n-- INSERT --"
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_esc_to_interrupt(self):
        """Should detect running state from legacy 'Esc to interrupt' text."""
        # Old status line: "✶ Wandering… (esc to interrupt · 50s · ↑ 938 tokens)"
        content = "Working on task...\n✶ Thinking… (esc to interrupt · 10s)\n"
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_does_not_false_positive_on_esc_to_interrupt_in_output(self):
        """Should NOT detect running when 'esc to interrupt' appears in agent output text."""
        # This tests against false positives when agent output mentions the phrase
        content = """The fix should ensure esc to interrupt is captured
>
-- INSERT --"""
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_running_with_conversation_containing_yes_no(self):
        """Should detect running state even with 'yes' and 'no' in conversation text."""
        # Ensures conversation text doesn't trigger false WAITING state
        content = """
> yes please, that sounds great :) Once it is all working, let me know
✻ Creating texture generation function… (esc to interrupt · ctrl+t to show todos · thinking)
>
-- INSERT --"""
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_spinner(self):
        """Should detect running state from spinner characters."""
        content = "Processing ⠋ Loading..."
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_detects_waiting_for_permission(self):
        """Should detect waiting state from permission prompt."""
        content = "Do you want to allow this tool?\nAllow once  Allow always  Deny"
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_with_yes_no(self):
        """Should detect waiting state from yes/no prompt."""
        content = "Execute this command? [y/n]"
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_with_question_ui(self):
        """Should detect waiting state from question UI."""
        content = """
What option do you want?
❯ 1. Option A
  2. Option B
Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_with_multiselect(self):
        """Should detect waiting state from multi-select question UI."""
        content = """
Select options:
[x] Option A
[ ] Option B
Enter to select · Esc to cancel
"""
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_with_numbered_selection(self):
        """Should detect waiting state from numbered selection UI without footer."""
        content = """
Exit plan mode?

  Claude wants to exit plan mode

  ❯ 1. Yes
    2. No
"""
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_waiting_takes_priority_over_running(self):
        """Should detect waiting even when running indicator is also present.

        This handles the case where Claude Code is running but also showing
        a permission prompt (e.g., sandbox network request). User input is
        needed, so WAITING takes priority.
        """
        content = """
✽ Building project… (esc to interrupt · 17m 51s · ↑ 5.3k tokens)

────────────────────────────────────────────────────────────────────
 Network request outside of sandbox

   Host: fonts.googleapis.com

   Do you want to allow this connection?
   ❯ 1. Yes
     2. Yes, and don't ask again for fonts.googleapis.com
     3. No, and tell Claude what to do differently (esc)
"""
        state = detect_claude_state(content)
        assert state == AgentState.WAITING

    def test_detects_running_with_subagent_progress(self):
        """Should detect running when subagent progress tree is visible but no status line."""
        content = """\
⏺ Running 2 agents… (ctrl+o to expand)
   ├─ Explore · 19 tool uses · 65.2k tokens
   │  ⎿  Searching for patterns…
   └─ Explore · 22 tool uses · 67.3k tokens
      ⎿  Bash: Search for references
     ctrl+b ctrl+b (twice) to run in background

───────────────────────────────────────────────────
❯
───────────────────────────────────────────────────
  ██░░░░░░░░ 21%
  -- INSERT -- ⏸ plan mode on (shift+tab to cycle)
"""
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_single_subagent(self):
        """Should detect running when a single subagent is active."""
        content = """\
⏺ Running 1 agent… (ctrl+o to expand)
   └─ Explore · 5 tool uses · 12.1k tokens
      ⎿  Reading file…
     ctrl+b ctrl+b (twice) to run in background

───────────────────────────────────────────────────
❯
───────────────────────────────────────────────────
  ████░░░░░░ 40%
  -- INSERT --
"""
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_no_false_running_from_old_subagent_text(self):
        """Should NOT detect running from completed subagent output in scrollback."""
        content = """\
2 Explore agents finished with results.
Some other output here.
>
-- INSERT --
"""
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_no_false_waiting_from_markdown_checklists(self):
        """Should NOT detect waiting when markdown checklists appear in output.

        Regression test: markdown checklists like '- [ ] Item' were matching
        the multi-select checkbox pattern and causing false WAITING detection.
        """
        content = """\
- [ ] All tools return dicts/lists, not JSON strings (issue #25)
- [ ] `list_notes` has no `folder` parameter (issue #22)
- [ ] UUIDs hidden by default in output (issue #26)
- [ ] No errors or exceptions

· Generating… (45s · ↓ 1.1k tokens · thinking)

───────────────────────────────────────────────────
❯
───────────────────────────────────────────────────
  █████░░░░░ 47%
  -- INSERT -- ⏵⏵ accept edits on (shift+tab to cycle)
"""
        state = detect_claude_state(content)
        assert state == AgentState.RUNNING

    def test_no_false_waiting_from_deny_in_help_text(self):
        """Should NOT detect waiting when 'deny' appears in help text.

        Regression test: the standalone 'deny' pattern matched occurrences
        like 'tool names to deny' in --disallowedTools help text, causing
        false WAITING detection on idle/completed sessions.
        """
        content = """\
✻ Brewed for 9m 6s

  --disallowedTools  tool names to deny
  --help             Show help

❯
"""
        state = detect_claude_state(content)
        assert state == AgentState.IDLE

    def test_returns_unknown_for_empty_content(self):
        """Should return unknown for empty content."""
        state = detect_claude_state("")
        assert state == AgentState.UNKNOWN

    def test_defaults_to_idle(self):
        """Should default to idle if no clear state detected."""
        content = "Some random output that doesn't match any pattern"
        state = detect_claude_state(content)
        assert state == AgentState.IDLE


class TestDetectCodexState:
    """Tests for detect_codex_state function."""

    # --- IDLE state tests ---

    def test_detects_idle_with_prompt_and_context_left(self):
        """Should detect idle when › prompt and context indicator visible."""
        content = """\u203a Write tests for @filename

  ? for shortcuts                            78% context left"""
        state = detect_codex_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_from_context_left_indicator(self):
        """Should detect idle from '% context left' indicator alone."""
        content = "Some output\n  73% context left"
        state = detect_codex_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_from_shortcuts_hint(self):
        """Should detect idle from '? for shortcuts' indicator."""
        content = "\u203a Implement feature\n\n  ? for shortcuts"
        state = detect_codex_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_with_bare_prompt(self):
        """Should detect idle when just the \u203a prompt is visible."""
        content = "Previous output\n\u203a\nsome trailing"
        state = detect_codex_state(content)
        assert state == AgentState.IDLE

    def test_detects_idle_with_prompt_and_text(self):
        """Should detect idle when \u203a prompt has text after it."""
        content = "\u203a Implement {feature}\n\n  ? for shortcuts"
        state = detect_codex_state(content)
        assert state == AgentState.IDLE

    # --- RUNNING state tests ---

    def test_detects_running_with_esc_to_interrupt(self):
        """Should detect running from 'esc to interrupt' indicator."""
        content = """\u2022 Planning cleanup script and tests (51s \u2022 esc to interrupt)

\u203a Implement {feature}

  ? for shortcuts                            73% context left"""
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_working_status(self):
        """Should detect running from 'Working' status line."""
        content = "Working (23s . esc to interrupt)\nSome output below"
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    def test_detects_running_with_command_execution(self):
        """Should detect running when executing a command."""
        content = "Running npm test (5s . esc to interrupt)"
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    def test_running_takes_priority_over_idle_indicators(self):
        """Should detect RUNNING even when idle indicators also present.

        Real Codex panes show both '? for shortcuts' and 'esc to interrupt'
        simultaneously. RUNNING must take priority.
        """
        content = """\u2022 Planning cleanup script and tests (51s \u2022 esc to interrupt)

\u203a Implement {feature}

  ? for shortcuts                            73% context left"""
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    # --- WAITING state tests ---

    def test_detects_waiting_plan_confirmation(self):
        """Should detect waiting from plan confirmation dialog."""
        content = """  Implement this plan?

\u203a 1. Yes, implement this plan  Switch to Default and start coding.
  2. No, stay in Plan mode     Continue planning with the model.

  Press enter to confirm or esc to go back"""
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_command_approval(self):
        """Should detect waiting from command approval prompt."""
        content = """Allow command?
  npm test

  Yes  Always  No, provide feedback"""
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_approve_and_run(self):
        """Should detect waiting from 'approve and run' prompt."""
        content = "Approve and run the command\nSome command here"
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    def test_waiting_takes_priority_over_running(self):
        """Should detect WAITING even when running indicators present.

        If a permission prompt appears while something was running,
        user input is needed so WAITING takes priority.
        """
        content = """Working (30s . esc to interrupt)

Allow command?
  rm -rf node_modules

  Yes  Always  No, provide feedback"""
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    # --- Disambiguation tests ---

    def test_arrow_in_selection_not_detected_as_idle(self):
        """Should NOT detect idle when \u203a is used as selection cursor in plan dialog."""
        content = """  Implement this plan?

\u203a 1. Yes, implement this plan  Switch to Default and start coding.
  2. No, stay in Plan mode     Continue planning with the model.

  Press enter to confirm or esc to go back"""
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    # --- Question UI tests (issue #25) ---

    def test_detects_waiting_with_question_ui(self):
        """Should detect waiting when Codex question screen is visible."""
        content = """\
  Question 1/3 (3 unanswered)
  Which dashboard codepath should the plan target?

  › 1. Patch current (Recommended)  Keep the current `--dashboard` implementation...
    2. Promote screen app           Switch `--dashboard` to the newer two-tab...
    3. Hybrid follow-up             Patch the current dashboard now...
    4. None of the above            Optionally, add details in notes (tab).

  tab to add notes | enter to submit answer | ←/→ to navigate questions | esc to interrupt"""
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    def test_detects_waiting_with_question_footer_only(self):
        """Should detect waiting from just the question footer line."""
        content = (
            "  tab to add notes | enter to submit answer"
            " | \u2190/\u2192 to navigate questions | esc to interrupt"
        )
        state = detect_codex_state(content)
        assert state == AgentState.WAITING

    def test_running_not_triggered_by_question_footer(self):
        """Should NOT detect running from 'esc to interrupt' in question footer.

        The question footer contains 'esc to interrupt' but should be
        classified as WAITING, not RUNNING.
        """
        content = """\
  Question 2/3 (2 unanswered)
  Some question here?

  › 1. Option A
    2. Option B

  tab to add notes | enter to submit answer | ←/→ to navigate questions | esc to interrupt"""
        state = detect_codex_state(content)
        assert state != AgentState.RUNNING

    def test_running_still_works_with_timer_prefix(self):
        """Should still detect running when 'esc to interrupt' has timer prefix."""
        content = "\u2022 Planning cleanup script and tests (51s \u2022 esc to interrupt)"
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    def test_running_works_with_dot_separator_no_paren(self):
        """Should detect running with dot separator even without closing paren."""
        content = "Working 23s . esc to interrupt"
        state = detect_codex_state(content)
        assert state == AgentState.RUNNING

    # --- Edge case tests ---

    def test_returns_unknown_for_empty_content(self):
        """Should return unknown for empty content."""
        state = detect_codex_state("")
        assert state == AgentState.UNKNOWN

    def test_returns_unknown_for_whitespace_only(self):
        """Should return unknown for whitespace-only content."""
        state = detect_codex_state("   \n   \n   ")
        assert state == AgentState.UNKNOWN

    def test_defaults_to_idle(self):
        """Should default to idle if no clear state detected."""
        content = "Some random output that doesn't match any pattern"
        state = detect_codex_state(content)
        assert state == AgentState.IDLE


class TestGetAgentState:
    """Tests for get_agent_state function."""

    def test_returns_unknown_for_non_claude(self):
        """Should return unknown for non-Claude agents."""
        state = get_agent_state("%5", AgentType.AIDER)
        assert state == AgentState.UNKNOWN

    def test_detects_claude_state(self):
        """Should detect state for Claude agents via regex fallback."""
        with (
            # New read_hook_state signature returns (state, validity); a
            # ``missing`` validity with state=None triggers regex fallback.
            patch(
                "agent_hq.hooks.read_hook_state",
                return_value=(None, "missing"),
            ),
            patch("agent_hq.enrichment.get_pane_content") as mock_content,
        ):
            mock_content.return_value = "> "

            state = get_agent_state("%5", AgentType.CLAUDE)

        assert state == AgentState.UNKNOWN

    def test_detects_codex_idle_state(self):
        """Should detect idle state for Codex agents."""
        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = "\u203a Write tests\n\n  ? for shortcuts  78% context left"

            state = get_agent_state("%5", AgentType.CODEX)

        assert state == AgentState.UNKNOWN

    def test_detects_codex_running_state(self):
        """Should detect running state for Codex agents."""
        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = "\u2022 Planning (51s \u2022 esc to interrupt)"

            state = get_agent_state("%5", AgentType.CODEX)

        assert state == AgentState.UNKNOWN


class TestComputeContentFingerprint:
    """Tests for compute_content_fingerprint function."""

    def test_returns_fingerprint_for_content(self):
        """Should return a fingerprint hash for valid content."""
        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = "This is some test content"

            fingerprint = compute_content_fingerprint("%5")

        assert fingerprint  # Non-empty
        assert len(fingerprint) == 16  # MD5 hex prefix

    def test_returns_empty_for_empty_content(self):
        """Should return empty string for empty content."""
        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = "   \n   "

            fingerprint = compute_content_fingerprint("%5")

        assert fingerprint == ""

    def test_stable_across_reflow(self):
        """Same content with different line breaks should hash the same."""
        content_single_line = "This is a test of the fingerprint function"
        content_multi_line = "This is a test\nof the fingerprint\nfunction"

        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = content_single_line
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = content_multi_line
            fingerprint2 = compute_content_fingerprint("%5")

        assert fingerprint1 == fingerprint2

    def test_different_for_different_content(self):
        """Different content should produce different fingerprints."""
        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = "First version of content"
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = "Second version with new content"
            fingerprint2 = compute_content_fingerprint("%5")

        assert fingerprint1 != fingerprint2

    def test_ignores_spinner_characters(self):
        """Spinner character changes should not affect fingerprint."""
        content_with_spinner1 = "Loading ⠋ content"
        content_with_spinner2 = "Loading ⠙ content"

        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = content_with_spinner1
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = content_with_spinner2
            fingerprint2 = compute_content_fingerprint("%5")

        assert fingerprint1 == fingerprint2

    def test_ignores_codex_spinner_characters(self):
        """Codex spinner character changes should not affect fingerprint."""
        content_frame1 = "Loading \u25cf content here \u25cb more"
        content_frame2 = "Loading \u25cb content here \u25cf more"

        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = content_frame1
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = content_frame2
            fingerprint2 = compute_content_fingerprint("%5")

        assert fingerprint1 == fingerprint2

    def test_ignores_ansi_codes(self):
        """ANSI escape codes should not affect fingerprint."""
        plain_content = "This is colored text"
        ansi_content = "\x1b[32mThis is colored text\x1b[0m"

        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = plain_content
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = ansi_content
            fingerprint2 = compute_content_fingerprint("%5")

        assert fingerprint1 == fingerprint2

    def test_stable_across_resize_with_more_visible_content(self):
        """Zoom/resize showing more content should not change fingerprint.

        When you zoom a pane, more lines become visible. The fingerprint
        should stay the same because we only hash the tail words (last 150),
        not everything visible.
        """
        # Create tail content with enough words to fill the tail window
        # Each "word word word word" line = 4 words, need ~200 words for tail
        tail_content = "word1 word2 word3 word4\n" * 50  # 200 words

        # Simulates narrow pane - just the tail content
        narrow_content = tail_content

        # Simulates zoomed pane - extra content at beginning + same tail
        extra_old_content = "extra older stuff here\n" * 100  # 400 extra words
        wide_content = extra_old_content + tail_content

        with patch("agent_hq.enrichment.get_pane_content") as mock_content:
            mock_content.return_value = narrow_content
            fingerprint1 = compute_content_fingerprint("%5")

            mock_content.return_value = wide_content
            fingerprint2 = compute_content_fingerprint("%5")

        # Should be same because the last 150 words are identical
        assert fingerprint1 == fingerprint2


class TestGitDiffShortstat:
    """Diff stats for base..branch via real git repos."""

    def _make_diff_repo(self, tmp_path, *, insertions=42, deletions=8, files=3):
        """Build a repo with a `feat/1` branch that differs from `main` by exactly
        ``files`` files, ``insertions`` lines added, and ``deletions`` lines removed.

        Strategy: the first file on the branch carries the deletions plus its
        share of insertions; the remaining files only add insertions.
        """
        assert files >= 1, "need at least one file"
        repo = _init_git_repo(tmp_path / "repo")

        # Seed: on main, file-0 starts with `deletions` lines that the branch
        # will remove. The remaining `files - 1` files don't exist yet.
        if deletions:
            seed = "\n".join(f"seed-{j}" for j in range(deletions))
            (repo / "file-0.txt").write_text(seed + "\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "seed")

        _git(repo, "checkout", "-b", "feat/1")

        # Distribute insertions across `files` files; remainder onto file-0 so
        # file-0 always has both deletions and insertions in one diff entry.
        per_file = insertions // files if files else 0
        remainder = insertions - per_file * (files - 1)
        # file-0 = remainder lines (replaces seed if any); file-1..file-N = per_file lines each
        (repo / "file-0.txt").write_text("\n".join("x" for _ in range(remainder)) + "\n")
        for i in range(1, files):
            (repo / f"file-{i}.txt").write_text("\n".join("x" for _ in range(per_file)) + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "branch work")
        return repo

    def test_parses_standard_shortstat(self, tmp_path):
        from agent_hq.enrichment import git_diff_shortstat

        repo = self._make_diff_repo(tmp_path, insertions=42, deletions=8, files=3)
        stats = git_diff_shortstat(str(repo), "feat/1")
        assert stats is not None
        assert stats.files == 3
        assert stats.insertions == 42
        assert stats.deletions == 8

    def test_parses_insertions_only(self, tmp_path):
        from agent_hq.enrichment import git_diff_shortstat

        repo = self._make_diff_repo(tmp_path, insertions=10, deletions=0, files=1)
        stats = git_diff_shortstat(str(repo), "feat/1")
        assert stats is not None
        assert stats.files == 1
        assert stats.insertions == 10
        assert stats.deletions == 0

    def test_returns_none_on_empty_diff(self, tmp_path):
        from agent_hq.enrichment import git_diff_shortstat

        repo = _init_git_repo(tmp_path / "repo")
        # Same branch on both sides — empty diff
        assert git_diff_shortstat(str(repo), "main") is None

    def test_returns_none_on_missing_branch(self, tmp_path):
        from agent_hq.enrichment import git_diff_shortstat

        repo = _init_git_repo(tmp_path / "repo")
        assert git_diff_shortstat(str(repo), "nonexistent") is None

    def test_returns_none_for_non_git(self, tmp_path):
        from agent_hq.enrichment import git_diff_shortstat

        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        assert git_diff_shortstat(str(not_a_repo), "feat/1") is None

    def test_resolves_detached_head(self, tmp_path):
        """During a rebase, get_git_branch returns "HEAD". The diff against
        main must still resolve to real stats via revparse_single."""
        from agent_hq.enrichment import git_diff_shortstat

        repo = self._make_diff_repo(tmp_path, insertions=10, deletions=0, files=1)
        # Detach HEAD on the latest feat/1 commit
        _git(repo, "checkout", "HEAD")

        stats = git_diff_shortstat(str(repo), "HEAD", base_ref="main")
        assert stats is not None
        assert stats.files == 1
        assert stats.insertions == 10
        assert stats.deletions == 0


def _write_rollout(root, day, name, thread_id, cwd, age_s=0, originator="codex-tui"):
    """Build a rollout file under root/YYYY/MM/day/name with a session_meta
    first line pointing at (thread_id, cwd), optionally backdated by age_s.

    ``originator`` defaults to ``"codex-tui"`` (interactive TUI) since
    that's the only value ``_read_rollout_meta`` accepts (Fix 1); pass
    ``originator=None`` to omit the field entirely, or another string to
    simulate a non-interactive originator (e.g. ``"codex-exec"``)."""
    d = root / "2026" / "07" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    payload = {"id": thread_id, "cwd": cwd}
    if originator is not None:
        payload["originator"] = originator
    meta = {"type": "session_meta", "payload": payload}
    p.write_text(json.dumps(meta) + "\n" + '{"type":"other"}\n')
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def _codex_agent(*, pane_id, cwd):
    return Agent(
        pane_id=pane_id,
        session_name="main",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CODEX,
        cwd=cwd,
    )


class TestGetCodexThreadId:
    """Newest cwd-matching rollout within max_age_days wins."""

    def test_matches_cwd_newest_wins(self, tmp_path):
        old_id = "11111111-1111-1111-1111-111111111111"
        new_id = "22222222-2222-2222-2222-222222222222"
        _write_rollout(tmp_path, "16", "rollout-a.jsonl", old_id, "/w", age_s=600)
        _write_rollout(tmp_path, "17", "rollout-b.jsonl", new_id, "/w")
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) == new_id

    def test_no_cwd_match_returns_none(self, tmp_path):
        _write_rollout(tmp_path, "17", "rollout-a.jsonl", "u1", "/other")
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) is None

    def test_old_rollouts_ignored(self, tmp_path):
        _write_rollout(tmp_path, "10", "rollout-a.jsonl", "u1", "/w", age_s=10 * 86400)
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) is None

    def test_malformed_first_line_skipped(self, tmp_path):
        p = tmp_path / "2026" / "07" / "17"
        p.mkdir(parents=True)
        (p / "rollout-bad.jsonl").write_text("not json\n")
        ok_id = "33333333-3333-3333-3333-333333333333"
        _write_rollout(tmp_path, "17", "rollout-ok.jsonl", ok_id, "/w")
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) == ok_id

    def test_real_fixture_first_line_parses(self, tmp_path):
        fixture = Path("tests/fixtures/codex_rollouts/plain.jsonl").read_text()
        d = tmp_path / "2026" / "07" / "17"
        d.mkdir(parents=True)
        (d / "rollout-fix.jsonl").write_text(fixture)
        meta = json.loads(fixture.splitlines()[0])
        got = get_codex_thread_id(meta["payload"]["cwd"], sessions_dir=tmp_path)
        assert got == meta["payload"]["id"]

    def test_non_uuid_id_shape_rejected(self, tmp_path):
        """A captured id that doesn't fullmatch the UUID shape (e.g.
        starting with '-') must not be treated as a thread id -- an
        argv/flag-injection class, since a leading '-' would reach
        ("codex", "resume", id) as a flag rather than a positional arg."""
        _write_rollout(tmp_path, "17", "rollout-bad-id.jsonl", "-not-a-uuid", "/w")
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) is None

    def test_codex_exec_originator_does_not_displace_tui(self, tmp_path):
        """A newer rollout from a non-interactive ``codex-exec`` run must
        not steal a pane's identity: only ``originator == "codex-tui"``
        rollouts are eligible (Fix 1)."""
        tui_id = "11111111-1111-1111-1111-111111111111"
        exec_id = "22222222-2222-2222-2222-222222222222"
        _write_rollout(tmp_path, "16", "rollout-tui.jsonl", tui_id, "/w", originator="codex-tui")
        _write_rollout(tmp_path, "17", "rollout-exec.jsonl", exec_id, "/w", originator="codex-exec")
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) == tui_id

    def test_missing_originator_does_not_displace_tui(self, tmp_path):
        """A rollout with no ``originator`` field at all (very old codex,
        or a shape we don't recognize) is rejected, not treated as
        interactive -- missing must fail closed."""
        tui_id = "11111111-1111-1111-1111-111111111111"
        no_orig_id = "33333333-3333-3333-3333-333333333333"
        _write_rollout(tmp_path, "16", "rollout-tui.jsonl", tui_id, "/w", originator="codex-tui")
        _write_rollout(tmp_path, "17", "rollout-none.jsonl", no_orig_id, "/w", originator=None)
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) == tui_id

    def test_missing_originator_alone_yields_no_match(self, tmp_path):
        """When the ONLY rollout in a cwd has no originator at all, there's
        nothing to bind to -- returns None rather than accepting it."""
        _write_rollout(tmp_path, "17", "rollout-none.jsonl", "u1", "/w", originator=None)
        assert get_codex_thread_id("/w", sessions_dir=tmp_path) is None

    def test_subagent_fixtures_rejected_non_tui_originator(self, tmp_path):
        """The subagent-parent/subagent-child fixtures carry
        ``originator: "codex_exec"`` (underscore) -- not the interactive
        TUI -- and must never bind to a pane."""
        for name in ("subagent-parent.jsonl", "subagent-child.jsonl"):
            fixture = Path(f"tests/fixtures/codex_rollouts/{name}").read_text()
            d = tmp_path / "2026" / "07" / "17"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"rollout-{name}").write_text(fixture)
            meta = json.loads(fixture.splitlines()[0])
            got = get_codex_thread_id(meta["payload"]["cwd"], sessions_dir=tmp_path)
            assert got is None


class TestCodexThreadDedupe:
    """Two live codex agents must never share a thread id."""

    def test_duplicate_thread_id_cleared_on_all(self):
        a1 = _codex_agent(pane_id="%1", cwd="/w")
        a2 = _codex_agent(pane_id="%2", cwd="/w")
        a1.codex_thread_id = a2.codex_thread_id = "uuid-7"
        _clear_duplicate_codex_thread_ids([a1, a2])
        assert a1.codex_thread_id is None
        assert a2.codex_thread_id is None

    def test_unique_ids_kept(self):
        a1 = _codex_agent(pane_id="%1", cwd="/w1")
        a2 = _codex_agent(pane_id="%2", cwd="/w2")
        a1.codex_thread_id = "u1"
        a2.codex_thread_id = "u2"
        _clear_duplicate_codex_thread_ids([a1, a2])
        assert a1.codex_thread_id == "u1"
        assert a2.codex_thread_id == "u2"


CODEX_UUID_A = "019f0000-0000-7000-8000-00000000aaaa"
CODEX_UUID_B = "019fffff-0000-7000-8000-00000000bbbb"


def _codex_raw(pane="%5"):
    return {
        "session_name": "main",
        "window_index": 1,
        "pane_index": 0,
        "pane_id": pane,
        "pane_pid": 1234,
        "command": "codex",
    }


class TestGetCodexRollout:
    """``get_codex_rollout`` returns (thread_id, path); the thread-id
    wrapper keeps its old shape."""

    def test_returns_id_and_path(self, tmp_path):
        from agent_hq.enrichment import get_codex_rollout

        p = _write_rollout(tmp_path, "17", "rollout-a.jsonl", CODEX_UUID_A, "/w")
        assert get_codex_rollout("/w", sessions_dir=tmp_path) == (CODEX_UUID_A, str(p))

    def test_none_when_no_match(self, tmp_path):
        from agent_hq.enrichment import get_codex_rollout

        assert get_codex_rollout("/w", sessions_dir=tmp_path) is None


class TestCodexHookIdentityAssociation:
    """enrich_agent prefers hook-sourced identity over the cwd heuristic (#206)."""

    def _identity(self, **kw):
        from agent_hq.hooks import HookIdentity

        defaults = dict(
            harness="codex",
            session_id=CODEX_UUID_A,
            transcript_path="/r/hook-rollout.jsonl",
            cwd="/w",
            model="gpt-5.6-sol",
        )
        defaults.update(kw)
        return HookIdentity(**defaults)

    def _enrich(self, identity, heuristic=(CODEX_UUID_B, "/r/heuristic.jsonl")):
        # os.path.exists is consulted for the accepted-identity path; the
        # fixture paths here are symbolic, so treat them all as present.
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.read_hook_identity", return_value=identity),
            patch("agent_hq.enrichment.get_codex_rollout", return_value=heuristic),
            patch("agent_hq.enrichment.os.path.exists", return_value=True),
        ):
            return enrich_agent(_codex_raw())

    def test_hook_identity_wins(self):
        agent = self._enrich(self._identity())
        assert agent.codex_thread_id == CODEX_UUID_A
        assert agent.codex_rollout_path == "/r/hook-rollout.jsonl"

    def test_cwd_mismatch_falls_back_to_heuristic(self):
        agent = self._enrich(self._identity(cwd="/elsewhere"))
        assert agent.codex_thread_id == CODEX_UUID_B
        assert agent.codex_rollout_path == "/r/heuristic.jsonl"

    def test_non_uuid_session_falls_back(self):
        agent = self._enrich(self._identity(session_id="-not-a-uuid"))
        assert agent.codex_thread_id == CODEX_UUID_B

    def test_claude_harness_identity_falls_back(self):
        agent = self._enrich(self._identity(harness="claude"))
        assert agent.codex_thread_id == CODEX_UUID_B

    def test_no_identity_no_heuristic(self):
        agent = self._enrich(None, heuristic=None)
        assert agent.codex_thread_id is None
        assert agent.codex_rollout_path is None

    def test_state_read_scoped_to_thread_id(self):
        """The codex thread id must reach get_agent_state as
        current_session_id so hook-state reads are session-scoped."""
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state") as mock_state,
            patch("agent_hq.enrichment.read_hook_identity", return_value=self._identity()),
            patch("agent_hq.enrichment.get_codex_rollout", return_value=None),
        ):
            mock_state.return_value = AgentState.IDLE
            enrich_agent(_codex_raw())
        assert mock_state.call_args.kwargs["current_session_id"] == CODEX_UUID_A


class TestCodexHookState:
    """get_agent_state for codex tries hook state first, regex second."""

    def test_hook_state_cannot_supply_live_status(self):
        from agent_hq.enrichment import get_agent_state

        with (
            patch(
                "agent_hq.hooks.read_hook_state",
                return_value=(AgentState.RUNNING, "valid"),
            ) as mock_read,
            patch("agent_hq.enrichment.read_hook_identity", return_value=object()),
            patch("agent_hq.enrichment.get_pane_content") as mock_content,
        ):
            state = get_agent_state("%5", AgentType.CODEX, current_session_id=CODEX_UUID_A)
        assert state == AgentState.UNKNOWN
        mock_content.assert_not_called()
        mock_read.assert_not_called()

    def test_hook_miss_does_not_use_regex(self):
        from agent_hq.enrichment import get_agent_state

        with (
            patch("agent_hq.hooks.read_hook_state", return_value=(None, "missing")),
            patch("agent_hq.enrichment.read_hook_identity", return_value=object()),
            patch(
                "agent_hq.enrichment.get_pane_content",
                return_value="(51s • esc to interrupt)",
            ),
        ):
            state = get_agent_state("%5", AgentType.CODEX, current_session_id=None)
        assert state == AgentState.UNKNOWN


class TestCodexSummaryFallback:
    """latest_summary falls back to the rollout tail when the hook wrote none."""

    def _enrich(self, summary_side_effect):
        from agent_hq import enrichment as enr

        enr._codex_summary_cache.clear()
        adapter = type("FakeAdapter", (), {"transcript_summary": staticmethod(summary_side_effect)})
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.read_hook_identity", return_value=None),
            patch("agent_hq.enrichment.read_hook_summary", return_value=None),
            patch("agent_hq.enrichment.get_adapter", return_value=adapter),
        ):
            return enrich_agent(_codex_raw())

    def test_fallback_used(self, tmp_path):
        rollout = tmp_path / "r.jsonl"
        rollout.write_text("{}\n")
        with patch(
            "agent_hq.enrichment.get_codex_rollout",
            return_value=(CODEX_UUID_A, str(rollout)),
        ):
            agent = self._enrich(lambda path, sid: "tail summary")
        assert agent.latest_summary == "tail summary"

    def test_fallback_cached_by_mtime(self, tmp_path):
        rollout = tmp_path / "r.jsonl"
        rollout.write_text("{}\n")
        calls = []

        def counting(path, sid):
            calls.append(path)
            return "tail summary"

        with patch(
            "agent_hq.enrichment.get_codex_rollout",
            return_value=(CODEX_UUID_A, str(rollout)),
        ):
            self._enrich(counting)
            self._enrich_again_without_clear(counting, rollout)
        assert len(calls) == 1

    def _enrich_again_without_clear(self, summary_side_effect, rollout):
        adapter = type("FakeAdapter", (), {"transcript_summary": staticmethod(summary_side_effect)})
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.read_hook_identity", return_value=None),
            patch("agent_hq.enrichment.read_hook_summary", return_value=None),
            patch("agent_hq.enrichment.get_adapter", return_value=adapter),
            patch(
                "agent_hq.enrichment.get_codex_rollout",
                return_value=(CODEX_UUID_A, str(rollout)),
            ),
        ):
            return enrich_agent(_codex_raw())

    def test_adapter_exception_degrades_to_none(self, tmp_path):
        rollout = tmp_path / "r.jsonl"
        rollout.write_text("{}\n")

        def boom(path, sid):
            raise RuntimeError("parser bug")

        with patch(
            "agent_hq.enrichment.get_codex_rollout",
            return_value=(CODEX_UUID_A, str(rollout)),
        ):
            agent = self._enrich(boom)
        assert agent.latest_summary is None  # summary lost, agent survives


class TestCodexDedupeHookSourcedSurvives:
    def test_hook_sourced_claimant_survives(self, tmp_path):
        from agent_hq.hooks import HookIdentity

        hooked = _codex_agent(pane_id="%1", cwd="/w")
        strayed = _codex_agent(pane_id="%2", cwd="/w")
        hooked.codex_thread_id = strayed.codex_thread_id = CODEX_UUID_A

        def fake_identity(pane_id, *, agent_pid=None):
            if pane_id == "%1":
                return HookIdentity(
                    harness="codex",
                    session_id=CODEX_UUID_A,
                    transcript_path=None,
                    cwd="/w",
                    model=None,
                )
            return None

        with patch("agent_hq.enrichment.read_hook_identity", side_effect=fake_identity):
            _clear_duplicate_codex_thread_ids([hooked, strayed])
        assert hooked.codex_thread_id == CODEX_UUID_A
        assert strayed.codex_thread_id is None


class TestCodexDedupeClearsRolloutSummary:
    """Codex review P2: an ambiguity-cleared claimant must not keep a
    rollout-derived latest_summary from the session it was just deemed
    not to own. Hook-written summaries (per-pane) survive."""

    def test_rollout_summary_cleared_on_ambiguous_claimants(self):
        a1 = _codex_agent(pane_id="%1", cwd="/w")
        a2 = _codex_agent(pane_id="%2", cwd="/w")
        a1.codex_thread_id = a2.codex_thread_id = CODEX_UUID_A
        a1.latest_summary = a2.latest_summary = "someone else's reply"
        with (
            patch("agent_hq.enrichment.read_hook_identity", return_value=None),
            patch("agent_hq.enrichment.read_hook_summary", return_value=None),
        ):
            _clear_duplicate_codex_thread_ids([a1, a2])
        assert a1.latest_summary is None
        assert a2.latest_summary is None

    def test_hook_written_summary_survives_clear(self):
        a1 = _codex_agent(pane_id="%1", cwd="/w")
        a2 = _codex_agent(pane_id="%2", cwd="/w")
        a1.codex_thread_id = a2.codex_thread_id = CODEX_UUID_A
        a1.latest_summary = a2.latest_summary = "hook summary"
        with (
            patch("agent_hq.enrichment.read_hook_identity", return_value=None),
            patch("agent_hq.enrichment.read_hook_summary", return_value="hook summary"),
        ):
            _clear_duplicate_codex_thread_ids([a1, a2])
        assert a1.latest_summary == "hook summary"
        assert a2.latest_summary == "hook summary"


class TestCodexIdentityRolloutPathFallback:
    """Workflow review: an accepted identity without a usable
    transcript_path must still get a rollout path (from the heuristic,
    when it names the same thread) — otherwise codex phase silently
    degrades to the branch rule for the whole session."""

    def _identity(self, transcript_path):
        from agent_hq.hooks import HookIdentity

        return HookIdentity(
            harness="codex",
            session_id=CODEX_UUID_A,
            transcript_path=transcript_path,
            cwd="/w",
            model="gpt-5.6-sol",
        )

    def _enrich(self, identity, heuristic):
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.read_hook_identity", return_value=identity),
            patch("agent_hq.enrichment.read_hook_summary", return_value=None),
            patch("agent_hq.enrichment.get_codex_rollout", return_value=heuristic),
        ):
            return enrich_agent(_codex_raw())

    def test_missing_path_falls_back_to_matching_heuristic(self):
        agent = self._enrich(self._identity(None), (CODEX_UUID_A, "/r/found.jsonl"))
        assert agent.codex_thread_id == CODEX_UUID_A  # hook id kept
        assert agent.codex_rollout_path == "/r/found.jsonl"

    def test_missing_path_ignores_mismatched_heuristic(self):
        agent = self._enrich(self._identity(None), (CODEX_UUID_B, "/r/other.jsonl"))
        assert agent.codex_thread_id == CODEX_UUID_A
        assert agent.codex_rollout_path is None  # never another thread's rollout

    def test_nonexistent_path_falls_back(self, tmp_path):
        real = tmp_path / "real.jsonl"
        real.write_text("{}\n")
        agent = self._enrich(
            self._identity(str(tmp_path / "gone.jsonl")), (CODEX_UUID_A, str(real))
        )
        assert agent.codex_rollout_path == str(real)

    def test_existing_path_used_as_is(self, tmp_path):
        real = tmp_path / "real.jsonl"
        real.write_text("{}\n")
        agent = self._enrich(self._identity(str(real)), (CODEX_UUID_A, "/r/other.jsonl"))
        assert agent.codex_rollout_path == str(real)


class TestCodexHookStateHarnessGate:
    """Workflow review: codex reading hook state must not inherit a
    Claude-written record. A claude file whose session_id is null would
    otherwise classify 'valid' against a codex agent with no thread id."""

    def test_claude_written_state_not_used_for_codex(self, tmp_path):
        from agent_hq.enrichment import get_agent_state

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 2,
                    "state": "running",
                    "session_id": None,
                    "timestamp": time.time(),
                }
            )
        )
        with (
            patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path),
            patch("agent_hq.enrichment.get_pane_content", return_value="›"),
        ):
            state = get_agent_state("%5", AgentType.CODEX, current_session_id=None)
        assert state == AgentState.UNKNOWN  # Neither hooks nor terminal text supply live status

    def test_codex_hook_state_is_not_native_status(self, tmp_path):
        from agent_hq.enrichment import get_agent_state

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 2,
                    "state": "running",
                    "harness": "codex",
                    "session_id": CODEX_UUID_A,
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state = get_agent_state("%5", AgentType.CODEX, current_session_id=CODEX_UUID_A)
        assert state == AgentState.UNKNOWN

    def test_claude_hook_state_is_not_native_status(self, tmp_path):
        from agent_hq.enrichment import get_agent_state

        (tmp_path / "5.json").write_text(
            json.dumps(
                {
                    "revision": 2,
                    "state": "running",
                    "session_id": "sess-1",
                    "timestamp": time.time(),
                }
            )
        )
        with patch("agent_hq.hooks.HOOK_STATE_DIR", tmp_path):
            state = get_agent_state("%5", AgentType.CLAUDE, current_session_id="sess-1")
        assert state == AgentState.UNKNOWN


class TestDedupePreservesPidValidatedOwner:
    """Final review P2: the dedupe tiebreak must validate identity with the
    claimant's pid, exactly as enrich_agent did — otherwise a stale-but-live
    hook owner is rejected and BOTH agents lose their identity."""

    def test_stale_but_live_owner_survives(self):
        from agent_hq.hooks import HookIdentity

        owner = _codex_agent(pane_id="%1", cwd="/w")
        stray = _codex_agent(pane_id="%2", cwd="/w")
        owner.codex_thread_id = stray.codex_thread_id = CODEX_UUID_A

        seen_pids = {}

        def fake_identity(pane_id, *, agent_pid=None):
            seen_pids[pane_id] = agent_pid
            if pane_id == "%1" and agent_pid == 4242:
                return HookIdentity(
                    harness="codex",
                    session_id=CODEX_UUID_A,
                    transcript_path=None,
                    cwd="/w",
                    model=None,
                )
            return None

        with (
            patch("agent_hq.enrichment.read_hook_identity", side_effect=fake_identity),
            patch("agent_hq.enrichment.read_hook_summary", return_value=None),
        ):
            _clear_duplicate_codex_thread_ids([owner, stray], pids={"%1": 4242, "%2": 77})

        assert seen_pids["%1"] == 4242  # pid threaded through
        assert owner.codex_thread_id == CODEX_UUID_A
        assert stray.codex_thread_id is None


class TestStaleHookSummaryDoesNotMaskRollout:
    """Final review P2: once codex hooks stop firing, a stale
    latest_summary must not suppress the rollout-tail fallback forever."""

    def _enrich(self, *, summary_age_s, rollout_summary="fresh from rollout", rollout_path=None):
        from agent_hq import enrichment as enr

        enr._codex_summary_cache.clear()
        adapter = type(
            "FakeAdapter",
            (),
            {"transcript_summary": staticmethod(lambda path, sid: rollout_summary)},
        )
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/w"),
            patch("agent_hq.enrichment.get_git_context", return_value=(None, None, None)),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.read_hook_identity", return_value=None),
            patch(
                "agent_hq.enrichment.read_hook_summary",
                side_effect=lambda pane_id, max_age=None: (
                    None if max_age is not None and summary_age_s > max_age else "stale hook text"
                ),
            ),
            patch("agent_hq.enrichment.get_adapter", return_value=adapter),
            patch("agent_hq.enrichment.os.path.exists", return_value=True),
            patch(
                "agent_hq.enrichment.get_codex_rollout",
                return_value=(CODEX_UUID_A, rollout_path),
            ),
        ):
            return enrich_agent(_codex_raw())

    def test_stale_summary_yields_to_rollout(self, tmp_path):
        rollout = tmp_path / "r.jsonl"
        rollout.write_text("{}\n")
        agent = self._enrich(summary_age_s=100000, rollout_path=str(rollout))
        assert agent.latest_summary == "fresh from rollout"

    def test_fresh_summary_wins(self, tmp_path):
        rollout = tmp_path / "r.jsonl"
        rollout.write_text("{}\n")
        agent = self._enrich(summary_age_s=5, rollout_path=str(rollout))
        assert agent.latest_summary == "stale hook text"


class _OSErrorRepo:
    """Fake pygit2.Repository whose every libgit2-backed access fails at the OS layer.

    pygit2's C extension maps libgit2's ``GIT_ERROR_OS`` class straight to a
    bare ``OSError`` (not ``GitError``) — e.g. ``failed to read descriptor:
    Resource deadlock avoided`` (macOS EDEADLK on a
    repo file read).
    """

    workdir = "/tmp/fake/"
    path = "/tmp/fake/.git/"

    @property
    def head(self):
        raise OSError("failed to read descriptor: Resource deadlock avoided")

    @property
    def remotes(self):
        raise OSError("failed to read descriptor: Resource deadlock avoided")

    def revparse_single(self, _spec):
        raise OSError("failed to read descriptor: Resource deadlock avoided")


class TestPygit2OSErrorDegrades:
    """A libgit2 OS-class failure in ONE agent's repo
    must degrade that agent's git context, never escape ``enrich_all_agents``
    and abort the whole daemon tick."""

    def test_get_git_branch_returns_none_on_oserror(self):
        with patch("agent_hq.enrichment._open_repo", return_value=_OSErrorRepo()):
            assert get_git_branch("/tmp/fake") is None

    def test_get_git_branch_drops_cache_only_for_oserror(self):
        """OSError is treated as transient (evict + retry next tick); a
        GitError such as an unborn HEAD is permanent and keeps the handle."""

        class UnbornRepo:
            @property
            def head(self):
                import pygit2

                raise pygit2.GitError("reference 'refs/heads/main' not found")

        with (
            patch("agent_hq.enrichment._open_repo", return_value=_OSErrorRepo()),
            patch("agent_hq.enrichment._drop_cached_repo") as drop,
        ):
            assert get_git_branch("/tmp/fake") is None
        drop.assert_called_once_with("/tmp/fake")

        with (
            patch("agent_hq.enrichment._open_repo", return_value=UnbornRepo()),
            patch("agent_hq.enrichment._drop_cached_repo") as drop,
        ):
            assert get_git_branch("/tmp/fake") is None
        drop.assert_not_called()

    def test_get_repo_name_falls_back_on_oserror(self):
        with patch("agent_hq.enrichment._open_repo", return_value=_OSErrorRepo()):
            assert get_repo_name("/tmp/fake") == "fake"

    def test_git_diff_shortstat_returns_none_and_drops_cache_on_oserror(self):
        from agent_hq.enrichment import git_diff_shortstat

        with (
            patch("agent_hq.enrichment._open_repo", return_value=_OSErrorRepo()),
            patch("agent_hq.enrichment._drop_cached_repo") as drop,
        ):
            assert git_diff_shortstat("/tmp/fake", "feat/1") is None
        drop.assert_called_once_with("/tmp/fake")

    def test_get_worktree_info_survives_unreadable_commondir(self, tmp_path):
        """Linked worktree whose ``commondir`` file the OS refuses to read.
        libgit2 usually trips first (``_open_repo`` then degrades to None),
        so the Python-side ``read_text`` is exercised through a stub handle:
        it must fall back to the parent-directory derivation, never raise
        (review round 2)."""
        if os.geteuid() == 0:
            import pytest

            pytest.skip("root ignores file modes")

        repo = _init_git_repo(tmp_path / "project")
        _git(repo, "branch", "feature")
        wt_path = tmp_path / "project-feature"
        _git(repo, "worktree", "add", str(wt_path), "feature")
        git_path = repo / ".git" / "worktrees" / "project-feature"

        class Handle:
            path = str(git_path) + "/"
            workdir = str(wt_path) + "/"

        commondir = git_path / "commondir"
        commondir.chmod(0o000)
        try:
            with patch("agent_hq.enrichment._open_repo", return_value=Handle()):
                main_repo, wt_name, is_linked = get_worktree_info(str(wt_path))
        finally:
            commondir.chmod(0o644)
        assert main_repo == str(repo.resolve())
        assert wt_name == "project-feature"
        assert is_linked is True

    def test_enrich_all_agents_survives_unreadable_repo_file(self, tmp_path):
        """Real reproduction: an unreadable packed-refs makes libgit2 fail at
        the OS layer on ``repo.head``. The tick-level contract is that the
        agent still comes back, with the branch degraded to None."""
        if os.geteuid() == 0:
            import pytest

            pytest.skip("root ignores file modes")
        from agent_hq.enrichment import enrich_all_agents
        from agent_hq.phase import _clear_repo_cache

        repo = _init_git_repo(tmp_path / "project")
        _git(repo, "pack-refs", "--all")
        packed = repo / ".git" / "packed-refs"
        packed.chmod(0o000)
        _clear_repo_cache()
        raw = {
            "session_name": "s",
            "window_index": 0,
            "pane_index": 0,
            "pane_id": "%9",
            "pane_pid": 1,
            "agent_pid": None,
            "command": "claude",
        }
        try:
            with patch("agent_hq.enrichment.get_pane_cwd", return_value=str(repo)):
                agents = enrich_all_agents([raw])
        finally:
            packed.chmod(0o644)
            _clear_repo_cache()

        assert len(agents) == 1
        assert agents[0].branch is None
        assert agents[0].repo_root == str(repo.resolve())


class TestGetClaudeNativeName:
    """#287: the registry ``name`` + ``nameSource`` (what ``@name`` addresses)."""

    def _write(self, tmp_path, pid, payload):
        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / f"{pid}.json").write_text(payload)
        return sessions

    def test_returns_name_and_source(self, tmp_path):
        sessions = self._write(
            tmp_path,
            4242,
            json.dumps({"name": "agent-hq-c3", "nameSource": "derived", "sessionId": "s"}),
        )
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions):
            assert get_claude_native_name(4242) == ("agent-hq-c3", "derived", "s")

    def test_name_without_source(self, tmp_path):
        sessions = self._write(tmp_path, 4242, json.dumps({"name": "memory-updates"}))
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions):
            assert get_claude_native_name(4242) == ("memory-updates", None, None)

    def test_no_name_ignores_jsonl_fallback(self, tmp_path):
        """Only the registry name is addressable — no transcript agentName fallback."""
        sessions = self._write(
            tmp_path, 4242, json.dumps({"sessionId": "s", "nameSource": "derived"})
        )
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions):
            assert get_claude_native_name(4242) == (None, None, None)

    def test_missing_file(self, tmp_path):
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", tmp_path):
            assert get_claude_native_name(4242) == (None, None, None)

    def test_invalid_json(self, tmp_path):
        sessions = self._write(tmp_path, 4242, "{not json")
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions):
            assert get_claude_native_name(4242) == (None, None, None)

    def test_non_dict_and_non_string_values(self, tmp_path):
        sessions = self._write(tmp_path, 1, json.dumps(["x"]))
        self._write(tmp_path, 2, json.dumps({"name": 7, "nameSource": "user"}))
        with patch("agent_hq.enrichment.CLAUDE_SESSIONS_DIR", sessions):
            assert get_claude_native_name(1) == (None, None, None)
            assert get_claude_native_name(2) == (None, None, None)

    def test_pid_none(self):
        assert get_claude_native_name(None) == (None, None, None)

    def test_enrich_agent_populates_native_name(self):
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
            "agent_pid": 5678,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/Users/test/project"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/Users/test/project", "main", None),
            ),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
            patch("agent_hq.enrichment.get_claude_session_ids", return_value=(None, "t-1")),
            patch(
                "agent_hq.enrichment.get_claude_native_name",
                return_value=("agent-hq-c3", "derived", "t-1"),
            ) as mock_name,
        ):
            agent = enrich_agent(raw)
        assert agent.native_name == "agent-hq-c3"
        assert agent.native_name_source == "derived"
        mock_name.assert_called_once_with(5678)

    def test_enrich_agent_skips_native_name_for_non_claude(self):
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "aider",
            "agent_pid": 5678,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/Users/test/project"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/Users/test/project", "main", None),
            ),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.UNKNOWN),
            patch("agent_hq.enrichment.get_claude_native_name") as mock_name,
        ):
            agent = enrich_agent(raw)
        assert agent.native_name is None
        mock_name.assert_not_called()

    def test_enrich_agent_drops_name_from_a_different_session(self):
        """#287 review: name and transcript sid come from separate reads; a
        registry rewrite in between (/clear) must not pair them."""
        raw = {
            "session_name": "main",
            "window_index": 1,
            "pane_index": 0,
            "pane_id": "%5",
            "pane_pid": 1234,
            "command": "claude",
            "agent_pid": 5678,
        }
        with (
            patch("agent_hq.enrichment.get_pane_cwd", return_value="/Users/test/project"),
            patch(
                "agent_hq.enrichment.get_git_context",
                return_value=("/Users/test/project", "main", None),
            ),
            patch("agent_hq.enrichment.get_agent_state", return_value=AgentState.IDLE),
            patch("agent_hq.enrichment.get_claude_session_slug", return_value=None),
            patch("agent_hq.enrichment.get_claude_session_ids", return_value=(None, "t-1")),
            patch(
                "agent_hq.enrichment.get_claude_native_name",
                return_value=("renamed", "user", "t-2"),
            ),
        ):
            agent = enrich_agent(raw)
        assert agent.claude_transcript_session_id == "t-1"
        assert agent.native_name is None
        assert agent.native_name_source is None
