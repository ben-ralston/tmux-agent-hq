import subprocess
from pathlib import Path

import pytest

from agent_hq.autopilot import (
    NO_PR,
    GoalTemplateError,
    LaunchStatus,
    PrInfo,
    fill_goal_condition,
    issue_branch_numbers,
    run_launcher,
)
from agent_hq.config import AutopilotConfig

FIXTURE = Path(__file__).parent / "fixtures" / "autopilot" / "dispatch-condition.md"
CFG = AutopilotConfig(enabled=True, model="opus", turn_bound=40)


def test_real_template_fixture_header_is_stripped():
    text = FIXTURE.read_text()
    assert "/goal <this>" in text  # the trap: a /goal inside the comment
    out = fill_goal_condition(text, issue_number=42, branch="feature/42-fix", turn_bound=40)
    assert out.startswith("/goal Deliver GitHub issue #42 on feature/42-fix as an open PR.")
    assert "-->" not in out and "<this>" not in out
    assert out.rstrip().endswith("Or stop after 40 turns.")


def test_live_template_still_parses():
    live = Path.home() / ".claude/skills/goal-mode/templates/dispatch-condition.md"
    if not live.exists():
        pytest.skip("no live template on this machine")
    fill_goal_condition(live.read_text(), issue_number=1, branch="b", turn_bound=1)


@pytest.mark.parametrize(
    "text",
    [
        "no goal here",
        "<!-- c -->\nprose first\n/goal x",
        "<!-- never closed\n/goal x",
        "<!-- c -->\n/goal #<ISSUE> <NEW>",
    ],
)
def test_bad_templates_raise(text):
    with pytest.raises(GoalTemplateError):
        fill_goal_condition(text, issue_number=1, branch="b", turn_bound=1)


class FakeLauncher:
    def __init__(self, rc=0, stdout="", stderr="", exc=None):
        self.rc, self.stdout, self.stderr, self.exc = rc, stdout, stderr, exc
        self.calls = []

    def __call__(self, argv, stdin_text, cwd):
        self.calls.append((argv, stdin_text, cwd))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def _run(fake, tmp_path):
    return run_launcher(
        branch="feature/42-fix",
        prompt="/goal x",
        cfg=CFG,
        repo_path=tmp_path,
        launcher=fake,
        base="main",
        script=Path("/x/launch.sh"),
    )


def test_launch_ok_parses_session(tmp_path):
    fake = FakeLauncher(
        stdout="Launched session 'ahq_feature-42-fix' in /wt/ahq.feature-42-fix "
        "(agent 'claude' startup verified)\n"
    )
    res = _run(fake, tmp_path)
    assert res.status is LaunchStatus.OK
    assert (res.session_name, res.worktree_path) == ("ahq_feature-42-fix", "/wt/ahq.feature-42-fix")
    argv, stdin_text, cwd = fake.calls[0]
    assert argv == [
        "/x/launch.sh",
        "--branch",
        "feature/42-fix",
        "--base",
        "main",
        "--auto",
        "--model",
        "opus",
    ]
    assert stdin_text == "/goal x" and cwd == tmp_path


def test_run_launcher_passes_base(tmp_path):
    fake = FakeLauncher(stdout="")
    run_launcher(
        branch="feature/42-fix",
        prompt="/goal x",
        cfg=CFG,
        repo_path=tmp_path,
        launcher=fake,
        base="dev",
        script=Path("/x/launch.sh"),
    )
    argv = fake.calls[0][0]
    assert argv[argv.index("--base") + 1] == "dev"


def test_fetch_default_branch_ok(monkeypatch):
    from agent_hq import autopilot

    seen = []

    def fake_run_gh(argv, *, kind, **kw):
        seen.append((argv, kind))
        return subprocess.CompletedProcess(argv, 0, "dev\n", "")

    monkeypatch.setattr(autopilot.gh_meter, "run_gh", fake_run_gh)
    assert autopilot.fetch_default_branch("o/r") == "dev"
    assert seen == [(["gh", "api", "repos/o/r", "-q", ".default_branch"], "autopilot-base")]


@pytest.mark.parametrize(
    "result", [(1, "dev\n"), OSError("gh"), subprocess.TimeoutExpired(["gh"], 30), (0, "  \n")]
)
def test_fetch_default_branch_failure_is_unknown(monkeypatch, result):
    from agent_hq import autopilot

    def fake_run_gh(argv, *, kind, **kw):
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(argv, result[0], result[1], "")

    monkeypatch.setattr(autopilot.gh_meter, "run_gh", fake_run_gh)
    assert autopilot.fetch_default_branch("o/r") is None


def test_exit1_is_launcher_failure(tmp_path):
    res = _run(FakeLauncher(rc=1, stderr="Error: launcher:deps: wt not found"), tmp_path)
    assert (
        res.status is LaunchStatus.FAILED and res.launcher_failure and "wt not found" in res.detail
    )


def test_exit2_is_uncertain_with_session(tmp_path):
    res = _run(
        FakeLauncher(
            rc=2,
            stderr="Error: target:verify: no harness (tmux session "
            "'ahq_feature-42-fix'; attach: tmux attach -t x)",
        ),
        tmp_path,
    )
    assert res.status is LaunchStatus.UNCERTAIN and res.session_name == "ahq_feature-42-fix"
    assert not res.launcher_failure


def test_timeout_is_uncertain(tmp_path):
    res = _run(FakeLauncher(exc=subprocess.TimeoutExpired(["x"], 300)), tmp_path)
    assert res.status is LaunchStatus.UNCERTAIN and "timed out" in res.detail


def test_missing_launcher_is_launcher_failure(tmp_path):
    res = _run(FakeLauncher(exc=FileNotFoundError("launch.sh")), tmp_path)
    assert res.status is LaunchStatus.FAILED and res.launcher_failure


def _git_runner(worktree_out="", branch_out="", rc=0, exc=None):
    def runner(argv, **kw):
        if exc:
            raise exc
        out = worktree_out if "worktree" in argv else branch_out
        return subprocess.CompletedProcess(argv, rc, out, "")

    return runner


def test_issue_branch_numbers_covers_conventions(tmp_path):
    wt = (
        "worktree /a\nbranch refs/heads/main\n\n"
        "worktree /b\nbranch refs/heads/bugfix/12-x\n\n"
        "worktree /c\nbranch refs/heads/feature/30-31-y\n"
    )
    br = "main\ndocs/77-z\nchore/5-a\nrandom-branch\n"
    assert issue_branch_numbers(tmp_path, runner=_git_runner(wt, br)) == {12, 30, 31, 77, 5}


def test_issue_branch_numbers_fails_closed(tmp_path):
    assert issue_branch_numbers(tmp_path, runner=_git_runner(rc=128)) is None
    assert (
        issue_branch_numbers(
            tmp_path, runner=_git_runner(exc=subprocess.TimeoutExpired(["git"], 5))
        )
        is None
    )
    assert issue_branch_numbers(tmp_path, runner=_git_runner(exc=FileNotFoundError("git"))) is None


def test_fetch_pr_targets_branch(monkeypatch):
    from agent_hq import autopilot

    seen = []

    def fake_run_gh(argv, *, kind, **kw):
        seen.append(argv)
        out = '[{"state":"OPEN","url":"u"}]' if "feature/1-x" in argv else "[]"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(autopilot.gh_meter, "run_gh", fake_run_gh)
    assert autopilot.fetch_pr("o/r", "feature/1-x") == PrInfo("OPEN", "u")
    assert autopilot.fetch_pr("o/r", "feature/2-x") is NO_PR
    assert "--head" in seen[0] and "feature/1-x" in seen[0]

    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(autopilot.gh_meter, "run_gh", boom)
    assert autopilot.fetch_pr("o/r", "feature/1-x") is None


@pytest.mark.parametrize(
    "out", ['{"state":"OPEN"}', "[1]", '[{"url":"u"}]', "not json", '[{"state":"OPEN","url":"u"}']
)
def test_fetch_pr_malformed_output_is_unknown(monkeypatch, out):
    from agent_hq import autopilot

    def fake_run_gh(argv, *, kind, **kw):
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(autopilot.gh_meter, "run_gh", fake_run_gh)
    assert autopilot.fetch_pr("o/r", "feature/1-x") is None


@pytest.mark.parametrize(
    "rc, stderr, expected",
    [
        (0, "", True),
        (1, "can't find session: s", False),
        (1, "no server running on /private/tmp/tmux-501/default", False),
        (1, "error connecting to /private/tmp/tmux-501/x (No such file or directory)", False),
        (1, "error connecting to /private/tmp/tmux-501/default (Permission denied)", None),
        (1, "some other failure", None),
    ],
)
def test_tmux_session_alive_classifies_errors(rc, stderr, expected):
    from agent_hq.autopilot import tmux_session_alive

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, rc, "", stderr)

    assert tmux_session_alive("s", runner=runner) is expected


def test_tmux_session_alive_timeout_is_unknown():
    from agent_hq.autopilot import tmux_session_alive

    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    assert tmux_session_alive("s", runner=runner) is None


def test_attempt_branch_is_unique_per_attempt():
    from agent_hq.autopilot import attempt_branch
    from agent_hq.models import Issue, Priority

    issue = Issue(
        number=42, title="Fix the thing", repo="o/r", status="Todo", priority=Priority.NO_PRI
    )
    a, b = attempt_branch(issue, "abcdef123"), attempt_branch(issue, "123456abc")
    assert a == "feature/42-fix-the-thing-apabcdef" and a != b


def test_conftest_blocks_real_launcher():
    with pytest.raises(RuntimeError, match="launch.sh"):
        subprocess.run(["/Users/x/.claude/skills/worktree/scripts/launch.sh", "--branch", "b"])
    with pytest.raises(RuntimeError, match="launch.sh"):
        subprocess.Popen(["/Users/x/.claude/skills/worktree/scripts/launch.sh", "--branch", "b"])


def test_conftest_lets_capabilities_probe_through(tmp_path):
    # Read-only probe (capabilities.py) is exempt; a nonexistent path just fails to spawn.
    missing = tmp_path / "skills/worktree/scripts/launch.sh"
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(missing), "--capabilities"])


@pytest.mark.parametrize(
    "text",
    [
        "<!-- c -->\n/goal #<ISSUE> on <BRANCH>.",  # no <TURN_BOUND>
        "<!-- c -->\n/goal #<ISSUE>. Stop after <TURN_BOUND> turns.",  # no <BRANCH>
        "/goal on <BRANCH>. Stop after <TURN_BOUND> turns.",  # no <ISSUE>
        "<!-- <TURN_BOUND> only in the comment -->\n/goal #<ISSUE> on <BRANCH>.",
    ],
)
def test_template_missing_a_required_placeholder_raises(text):
    with pytest.raises(GoalTemplateError, match="placeholder"):
        fill_goal_condition(text, issue_number=1, branch="b", turn_bound=1)


def test_tmux_probe_targets_exact_session_name():
    from agent_hq.autopilot import tmux_session_alive

    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    tmux_session_alive("feature-2", runner=runner)
    assert seen == [["tmux", "has-session", "-t", "=feature-2"]]


def test_stderr_tail_kept_detail_stays_one_line(tmp_path):
    lines = [f"wt: line {i}" for i in range(20)] + ["Error: launcher:worktree: wt switch failed"]
    res = _run(FakeLauncher(rc=1, stderr="\n".join(lines) + "\n"), tmp_path)
    assert res.detail == "Error: launcher:worktree: wt switch failed"
    assert res.stderr_tail.splitlines() == lines[-15:]


def test_stderr_tail_capped_keeps_the_end(tmp_path):
    stderr = "x" * 5000 + "\nError: launcher:worktree: boom\n"
    res = _run(FakeLauncher(rc=2, stderr=stderr), tmp_path)
    assert len(res.stderr_tail) <= 2000
    assert res.stderr_tail.endswith("Error: launcher:worktree: boom")


def test_ok_launch_has_no_stderr_tail(tmp_path):
    assert _run(FakeLauncher(stdout="", stderr="noise"), tmp_path).stderr_tail is None


# --- cleanup_failed_launch (#312): real git in tmp_path, never a real repo ---

BRANCH = "feature/7-x-apabc123"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _branches_of(repo):
    return _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def test_cleanup_removes_worktree_and_branch(git_repo, tmp_path):
    from agent_hq.autopilot import cleanup_failed_launch

    wt = tmp_path / "wt"
    _git(git_repo, "worktree", "add", "-q", "-b", BRANCH, str(wt), "main")
    (wt / "partial-hook-output").write_text("x")  # untracked junk from a failed hook
    assert cleanup_failed_launch(git_repo, BRANCH) == "cleaned up worktree and branch"
    assert str(wt) not in _git(git_repo, "worktree", "list") and not wt.exists()
    assert BRANCH not in _branches_of(git_repo)


def test_cleanup_branch_without_worktree(git_repo):
    from agent_hq.autopilot import cleanup_failed_launch

    _git(git_repo, "branch", BRANCH, "main")
    assert cleanup_failed_launch(git_repo, BRANCH) == "cleaned up branch"
    assert BRANCH not in _branches_of(git_repo)


def test_cleanup_nothing_created(git_repo):
    from agent_hq.autopilot import cleanup_failed_launch

    assert cleanup_failed_launch(git_repo, BRANCH) == "nothing to clean"
    assert _branches_of(git_repo) == ["main"]


def test_cleanup_refuses_when_branch_has_commits(git_repo, tmp_path):
    from agent_hq.autopilot import cleanup_failed_launch

    wt = tmp_path / "wt"
    _git(git_repo, "worktree", "add", "-q", "-b", BRANCH, str(wt), "main")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "work")
    assert cleanup_failed_launch(git_repo, BRANCH) == "cleanup refused: branch has commits"
    assert wt.exists() and BRANCH in _branches_of(git_repo)


def test_cleanup_never_touches_the_base_branch(git_repo):
    from agent_hq.autopilot import cleanup_failed_launch

    assert cleanup_failed_launch(git_repo, "main").startswith("cleanup refused")
    assert _branches_of(git_repo) == ["main"]


def test_cleanup_git_failure_never_raises(git_repo):
    from agent_hq.autopilot import cleanup_failed_launch

    def boom(argv, **kw):
        raise OSError("git gone")

    assert cleanup_failed_launch(git_repo, BRANCH, runner=boom).startswith("cleanup failed")


def test_cleanup_branch_cut_from_a_remote_only_base(git_repo):
    """GitHub's default branch may exist only as origin/<name> locally (or the
    local copy may lag it): the commit check must not depend on the base name."""
    from agent_hq.autopilot import cleanup_failed_launch

    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "remote work")
    _git(git_repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    _git(git_repo, "reset", "-q", "--hard", "HEAD~1")  # local branches lag origin/dev
    _git(git_repo, "branch", BRANCH, "origin/dev")
    assert cleanup_failed_launch(git_repo, BRANCH) == "cleaned up branch"
    assert BRANCH not in _branches_of(git_repo)


def test_cleanup_refuses_a_former_companion_dispatch_branch(git_repo):
    # #399: -cp<6 hex> was the companion's dispatch suffix; only autopilot's
    # own -ap<6 hex> attempt branches are cleaned now.
    from agent_hq.autopilot import cleanup_failed_launch

    branch = "feature/7-x-cpabc123"
    _git(git_repo, "branch", branch, "main")
    assert cleanup_failed_launch(git_repo, branch).startswith("cleanup refused")
    assert branch in _branches_of(git_repo)
