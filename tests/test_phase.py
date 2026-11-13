"""Tests for agent_hq.phase — dev-phase detection."""

from __future__ import annotations

import time

import pytest


class TestPhaseConstants:
    """Sanity checks for module constants."""

    def test_cache_ttl_is_reasonable(self):
        from agent_hq.phase import PHASE_CACHE_TTL_SECONDS

        # 60s default; short enough to feel live, long enough to dodge gh rate limits
        assert 30 <= PHASE_CACHE_TTL_SECONDS <= 120

    def test_test_command_patterns_nonempty(self):
        from agent_hq.phase import TEST_COMMAND_PATTERNS

        assert len(TEST_COMMAND_PATTERNS) >= 3
        assert any("pytest" in p for p in TEST_COMMAND_PATTERNS)


class TestHasRecentPlanFile:
    """Signal: plan file exists under docs/superpowers/plans/ with recent mtime."""

    def test_returns_true_when_recent_plan_file_exists(self, tmp_path):
        from agent_hq.phase import has_recent_plan_file

        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "2026-04-24-foo.md").write_text("plan")

        assert has_recent_plan_file(str(tmp_path)) is True

    def test_returns_false_when_no_plans_dir(self, tmp_path):
        from agent_hq.phase import has_recent_plan_file

        assert has_recent_plan_file(str(tmp_path)) is False

    def test_returns_false_when_plan_is_old(self, tmp_path):
        import os

        from agent_hq.phase import RECENT_EDIT_WINDOW_SECONDS, has_recent_plan_file

        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "2025-01-01-old.md"
        plan_file.write_text("old")
        # Backdate the plan file well outside the recency window
        old_time = time.time() - RECENT_EDIT_WINDOW_SECONDS - 60
        os.utime(plan_file, (old_time, old_time))

        assert has_recent_plan_file(str(tmp_path)) is False


class TestHasRecentSpecFile:
    def test_returns_true_when_recent_spec_file_exists(self, tmp_path):
        from agent_hq.phase import has_recent_spec_file

        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "2026-04-24-foo-design.md").write_text("spec")

        assert has_recent_spec_file(str(tmp_path)) is True

    def test_returns_false_when_no_specs_dir(self, tmp_path):
        from agent_hq.phase import has_recent_spec_file

        assert has_recent_spec_file(str(tmp_path)) is False


class TestCommitsSinceBase:
    """Signal: number of commits on branch since it diverged from base."""

    def _init_repo(self, tmp_path, base_branch="main"):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", base_branch], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True
        )
        (repo / "README.md").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        return repo

    def test_returns_zero_when_on_base_branch(self, tmp_path):
        from agent_hq.phase import commits_since_base

        repo = self._init_repo(tmp_path)
        assert commits_since_base(str(repo), "main", base_ref="main") == 0

    def test_returns_commit_count_on_feature_branch(self, tmp_path):
        import subprocess

        from agent_hq.phase import commits_since_base

        repo = self._init_repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
        )
        (repo / "a.py").write_text("a")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "a"], cwd=repo, check=True, capture_output=True)
        (repo / "b.py").write_text("b")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "b"], cwd=repo, check=True, capture_output=True)

        assert commits_since_base(str(repo), "feature", base_ref="main") == 2

    def test_returns_zero_when_base_ref_missing(self, tmp_path):
        from agent_hq.phase import commits_since_base

        repo = self._init_repo(tmp_path)
        assert commits_since_base(str(repo), "main", base_ref="nonexistent") == 0

    def test_counts_commits_with_detached_head(self, tmp_path):
        """When the agent's worktree is mid-rebase / bisect, ``branch`` is "HEAD".
        ``revparse_single`` must resolve it to the current commit so we still
        report a real count instead of 0."""
        import subprocess

        from agent_hq.phase import commits_since_base

        repo = self._init_repo(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
        )
        for letter in ("a", "b"):
            (repo / f"{letter}.py").write_text(letter)
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", letter], cwd=repo, check=True, capture_output=True
            )
        # Detach HEAD on the latest feature commit
        subprocess.run(["git", "checkout", "HEAD"], cwd=repo, check=True, capture_output=True)

        assert commits_since_base(str(repo), "HEAD", base_ref="main") == 2


class TestGetPRState:
    """Signal: cached PR state for a branch via `gh pr view`."""

    def test_returns_open_when_pr_is_open(self):
        from unittest.mock import patch

        from agent_hq.phase import get_pr_state

        mock_stdout = '{"state": "OPEN", "number": 42}'
        with patch("subprocess.run") as m:
            m.return_value.returncode = 0
            m.return_value.stdout = mock_stdout
            get_pr_state.cache_clear()  # ensure cache miss
            assert get_pr_state("/repo", "feat/42") == "open"

    def test_returns_merged_when_pr_is_merged(self):
        from unittest.mock import patch

        from agent_hq.phase import get_pr_state

        mock_stdout = '{"state": "MERGED", "number": 42}'
        with patch("subprocess.run") as m:
            m.return_value.returncode = 0
            m.return_value.stdout = mock_stdout
            get_pr_state.cache_clear()
            assert get_pr_state("/repo", "feat/42") == "merged"

    def test_returns_none_when_no_pr(self):
        from unittest.mock import patch

        from agent_hq.phase import get_pr_state

        with patch("subprocess.run") as m:
            m.return_value.returncode = 1
            m.return_value.stdout = ""
            get_pr_state.cache_clear()
            assert get_pr_state("/repo", "no-pr-here") is None

    def test_cache_avoids_second_call(self):
        from unittest.mock import patch

        from agent_hq.phase import get_pr_state

        mock_stdout = '{"state": "OPEN", "number": 7}'
        with patch("subprocess.run") as m:
            m.return_value.returncode = 0
            m.return_value.stdout = mock_stdout
            get_pr_state.cache_clear()
            get_pr_state("/repo", "feat/7")
            get_pr_state("/repo", "feat/7")
            # Second call must be served from cache
            assert m.call_count == 1


class TestPRStateIsMetered:
    """``gh pr view`` is a GraphQL call the daemon makes per branch; it must
    show up in ``gh-stats`` like every other gh call (#255 review)."""

    def test_pr_view_is_recorded_by_the_meter(self):
        from unittest.mock import patch

        from agent_hq import gh_meter
        from agent_hq.phase import get_pr_state

        with patch("subprocess.run") as m:
            m.return_value.returncode = 0
            m.return_value.stdout = '{"state": "OPEN", "number": 7}'
            get_pr_state.cache_clear()
            get_pr_state("/repo", "feat/7")
            # cwd must still reach the subprocess: gh resolves the repo from it.
            assert m.call_args.kwargs.get("cwd") == "/repo"
        summary = gh_meter.summarize(hours=1)
        assert summary.by_kind.get("pr-view") == 1
        assert summary.by_api.get("graphql") == 1


class TestHasQASignal:
    """Signal: recent pane content contains test/dev-server commands."""

    def test_returns_true_for_pytest(self):
        from agent_hq.phase import has_qa_signal

        content = "$ ./run test -k discovery\npassed"
        assert has_qa_signal(content) is True

    def test_returns_true_for_npm_test(self):
        from agent_hq.phase import has_qa_signal

        assert has_qa_signal("> npm test\n") is True

    def test_returns_false_for_unrelated_content(self):
        from agent_hq.phase import has_qa_signal

        assert has_qa_signal("just regular terminal output") is False

    def test_returns_false_for_empty(self):
        from agent_hq.phase import has_qa_signal

        assert has_qa_signal("") is False


class TestDetectBranchPhase:
    """Integration: orchestrate signals into a DevPhase."""

    def test_merged_pr_maps_to_done(self):
        """``gh pr view`` returning ``"merged"`` maps to the renamed
        ``DevPhase.DONE`` (issue #55: ``done`` covers PR-merge AND non-PR
        completion of one-shot tasks)."""
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with patch("agent_hq.phase.get_pr_state", return_value="merged"):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.DONE

    def test_review_when_open_pr(self):
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with patch("agent_hq.phase.get_pr_state", return_value="open"):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.REVIEW

    def test_implement_when_commits_no_pr(self):
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=3),
            patch("agent_hq.phase.has_qa_signal", return_value=False),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=False, pane_content=""
            )
        assert phase is DevPhase.IMPLEMENT

    def test_qa_when_commits_plus_qa_signal_and_idle(self):
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=1),
            patch("agent_hq.phase.has_qa_signal", return_value=True),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content="pytest"
            )
        assert phase is DevPhase.QA

    def test_design_when_plan_file_no_commits(self):
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=0),
            patch("agent_hq.phase.has_recent_plan_file", return_value=True),
            patch("agent_hq.phase.has_recent_spec_file", return_value=False),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.DESIGN

    def test_design_when_spec_file_no_plan_no_commits(self):
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=0),
            patch("agent_hq.phase.has_recent_plan_file", return_value=False),
            patch("agent_hq.phase.has_recent_spec_file", return_value=True),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.DESIGN

    def test_adhoc_when_no_signals(self):
        """Untyped or short-prefix branches (e.g. 'feat/1') still fall to
        ADHOC — only the canonical `feature|bugfix|chore|docs/*` prefixes
        are recognized as 'task in progress'."""
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=0),
            patch("agent_hq.phase.has_recent_plan_file", return_value=False),
            patch("agent_hq.phase.has_recent_spec_file", return_value=False),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="feat/1", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.ADHOC

    def test_adhoc_on_main_when_no_signals(self):
        """``main`` with no signals stays ADHOC — main is the 'no specific
        task underway' branch by definition."""
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=0),
            patch("agent_hq.phase.has_recent_plan_file", return_value=False),
            patch("agent_hq.phase.has_recent_spec_file", return_value=False),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch="main", agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.ADHOC

    @pytest.mark.parametrize(
        "branch",
        [
            "feature/55-phase",
            "bugfix/123-rss",
            "chore/cleanup",
            "docs/readme",
        ],
    )
    def test_typed_feature_branch_with_no_signals_defaults_to_design(self, branch):
        """Closes the gap from issue #55 Pattern A: a freshly created
        `feature/*`, `bugfix/*`, `chore/*`, or `docs/*` worktree with no
        commits, no PR, and no plan/spec file yet falls through every
        rule. Default to DESIGN (work has clearly started — just no
        artifacts yet) instead of ADHOC ('no clear task'), so the dashboard
        is never wrong about a fresh feature branch even before Claude
        emits its first <phase> tag."""
        from unittest.mock import patch

        from agent_hq.models import DevPhase
        from agent_hq.phase import detect_branch_phase

        with (
            patch("agent_hq.phase.get_pr_state", return_value=None),
            patch("agent_hq.phase.commits_since_base", return_value=0),
            patch("agent_hq.phase.has_recent_plan_file", return_value=False),
            patch("agent_hq.phase.has_recent_spec_file", return_value=False),
        ):
            phase = detect_branch_phase(
                repo_root="/r", branch=branch, agent_state_idle=True, pane_content=""
            )
        assert phase is DevPhase.DESIGN

    def test_returns_none_when_no_repo(self):
        from agent_hq.phase import detect_branch_phase

        assert (
            detect_branch_phase(repo_root=None, branch=None, agent_state_idle=True, pane_content="")
            is None
        )


class TestOpenRepoOSError:
    """pygit2 raises a bare ``OSError`` for libgit2
    ``GIT_ERROR_OS`` failures; every pygit2 call site must treat it like a
    ``GitError`` and degrade instead of propagating into the daemon tick."""

    def _oserror(self, *_a, **_k):
        raise OSError("failed to read descriptor: Resource deadlock avoided")

    def test_open_repo_returns_none_when_discover_raises_oserror(self, tmp_path):
        from agent_hq import phase

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(phase.pygit2, "discover_repository", self._oserror)
            assert phase._open_repo(str(tmp_path)) is None

    def test_open_repo_returns_none_when_repository_raises_oserror(self, tmp_path):
        from agent_hq import phase

        phase._clear_repo_cache()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(phase.pygit2, "discover_repository", lambda _c: str(tmp_path / ".git"))
            mp.setattr(phase.pygit2, "Repository", self._oserror)
            assert phase._open_repo(str(tmp_path)) is None

    def test_drop_cached_repo_tolerates_discover_oserror(self, tmp_path):
        from agent_hq import phase

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(phase.pygit2, "discover_repository", self._oserror)
            phase._drop_cached_repo(str(tmp_path))  # must not raise

    def test_commits_since_base_returns_zero_and_drops_cache_on_oserror(self):
        from agent_hq import phase

        class Repo:
            def revparse_single(self, _spec):
                raise OSError("failed to read descriptor: Resource deadlock avoided")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(phase, "_open_repo", lambda _c: Repo())
            dropped = []
            mp.setattr(phase, "_drop_cached_repo", dropped.append)
            assert phase.commits_since_base("/tmp/fake", "feat", base_ref="main") == 0
        assert dropped == ["/tmp/fake"]
