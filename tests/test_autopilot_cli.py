"""`agent-hq autopilot run` (#302 Task 7): reservations, per-issue
revalidation, strict GitHub loading and failure reporting."""

import fcntl
import subprocess

import pytest

from agent_hq import autopilot_cli
from agent_hq.autopilot import NO_PR, SKIP_GITHUB, PrInfo, load_issues_strict
from agent_hq.autopilot_store import AutopilotStore, Outcome, RunRecord
from agent_hq.config import AutopilotConfig, Config, ProjectConfig, RepoConfig
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageStore

REPO = "ben-ralston/agent-hq"
TEMPLATE = (
    "<!-- see /goal <this> -->\n"
    "/goal Deliver #<ISSUE> on <BRANCH>. Or stop after <TURN_BOUND> turns."
)


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    tpl = tmp_path / "tpl.md"
    tpl.write_text(TEMPLATE)
    cfg = tmp_path / "projects.yaml"

    def write_cfg(enabled=True, base_branch=None, extra_repo=None):
        repos = f"repos:\n  {REPO}:\n    local_path: {repo_dir}\n"
        if base_branch:
            repos += f"    base_branch: {base_branch}\n"
        allow = [REPO]
        if extra_repo:
            repos += f"  {extra_repo}:\n    local_path: {repo_dir}\n"
            allow.append(extra_repo)
        cfg.write_text(
            repos + f"projects: []\nworktree_root: {tmp_path}\n"
            f"triage:\n  data_dir: {data}\n"
            f"autopilot:\n  enabled: {str(enabled).lower()}\n  repos: [{', '.join(allow)}]\n"
        )

    write_cfg()
    triage = TriageStore(data)
    for n, v in ((1, 3), (2, 5), (3, 4)):
        triage.upsert(
            TriageRecord(
                issue_key=f"{REPO}#{n}",
                value=v,
                effort=Effort.S,
                agent_fitness=AgentFitness.SOLO,
                autopilot=True,
                updated_at="t",
            ),
            actor="human",
        )
        triage.confirm(f"{REPO}#{n}", now="t")
    issues = [
        Issue(number=n, title=f"issue {n}", repo=REPO, status="Todo", priority=Priority.NO_PRI)
        for n in (1, 2, 3)
    ]
    calls, hooks = [], {}

    def launcher(argv, stdin_text, cwd):
        calls.append((argv, stdin_text))
        if "during_launch" in hooks:
            hooks.pop("during_launch")()
        branch = argv[argv.index("--branch") + 1]
        return subprocess.CompletedProcess(
            argv, 0, f"Launched session 's-{branch}' in /wt/{branch} (agent 'claude' x)\n", ""
        )

    def run(**kw):
        defaults = dict(
            dry_run=False,
            config_path=cfg,
            launcher=launcher,
            pr_lookup=lambda repo, branch: NO_PR,
            session_probe=lambda name: True,
            issues_loader=lambda c: (issues, {}),
            branch_probe=lambda path: set(),
            base_lookup=lambda repo: "main",
            cleanup=lambda path, branch: "nothing to clean",
            template_path=tpl,
        )
        defaults.update(kw)
        return autopilot_cli.run_autopilot_cli(**defaults)

    return dict(
        run=run,
        calls=calls,
        hooks=hooks,
        data=data,
        write_cfg=write_cfg,
        triage=triage,
        issues=issues,
        store=AutopilotStore(data),
    )


def _branches(calls):
    return [argv[argv.index("--branch") + 1] for argv, _ in calls]


def _nums(calls):
    return [int(b.split("/")[1].split("-")[0]) for b in _branches(calls)]


def test_launches_top_two_and_records(env):
    assert env["run"]() == 0
    assert _nums(env["calls"]) == [2, 3]
    b = _branches(env["calls"])[0]
    assert b.startswith("feature/2-issue-2-ap") and len(b) == len("feature/2-issue-2-ap") + 6
    assert env["calls"][0][1].startswith(f"/goal Deliver #2 on {b}")
    assert env["store"].snapshot().attempts[f"{REPO}#2"].branch == b
    state = env["store"].snapshot()
    assert {k: a.outcome for k, a in state.attempts.items()} == {
        f"{REPO}#2": Outcome.RUNNING,
        f"{REPO}#3": Outcome.RUNNING,
    }
    assert state.runs[-1].error is None and (f"{REPO}#1", "throttled") in state.runs[-1].skipped


def test_second_night_throttled_by_in_flight(env):
    env["run"]()
    env["calls"].clear()
    assert env["run"]() == 0
    assert _nums(env["calls"]) == [1]


def test_dry_run_writes_nothing(env, capsys):
    assert env["run"](dry_run=True) == 0
    assert env["calls"] == [] and not env["store"].path.exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "/goal Deliver #2" in out


def test_disabled_and_paused_exit_zero(env):
    env["write_cfg"](enabled=False)
    assert env["run"]() == 0 and env["calls"] == []
    env["write_cfg"](enabled=True)
    env["store"].set_paused(True)
    assert env["run"]() == 0 and env["calls"] == []


def test_lock_held_exits_zero_without_launching(env):
    lock = env["data"] / "autopilot" / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env["run"]() == 0
    assert env["calls"] == []


def test_pause_during_first_launch_stops_second(env):
    env["hooks"]["during_launch"] = lambda: env["store"].set_paused(True)
    assert env["run"]() == 0
    assert _nums(env["calls"]) == [2]
    assert (f"{REPO}#3", "paused during run") in env["store"].snapshot().runs[-1].skipped


def test_opt_out_during_first_launch_skips_it(env):
    def opt_out():
        env["triage"].upsert(
            TriageRecord(issue_key=f"{REPO}#3", autopilot=False, updated_at="t9"), actor="human"
        )

    env["hooks"]["during_launch"] = opt_out
    env["run"]()
    assert _nums(env["calls"]) == [2]
    assert f"{REPO}#3" not in env["store"].snapshot().attempts


def test_reservation_written_before_launch(env):
    seen = []
    env["hooks"]["during_launch"] = lambda: seen.append(
        env["store"].snapshot().attempts[f"{REPO}#2"].outcome
    )
    env["run"]()
    assert seen == [Outcome.LAUNCHING]


def test_launcher_failure_stops_night_and_exits_one(env, capsys):
    def bad(argv, stdin_text, cwd):
        env["calls"].append((argv, stdin_text))
        return subprocess.CompletedProcess(argv, 1, "", "Error: launcher:deps: wt not found")

    assert env["run"](launcher=bad) == 1
    assert len(env["calls"]) == 1
    state = env["store"].snapshot()
    assert state.attempts[f"{REPO}#2"].outcome is Outcome.LAUNCH_FAILED
    assert "wt not found" in state.runs[-1].error
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_uncertain_launch_holds_capacity_and_stops(env):
    def timeout(argv, stdin_text, cwd):
        env["calls"].append((argv, stdin_text))
        raise subprocess.TimeoutExpired(argv, 300)

    assert env["run"](launcher=timeout) == 1
    assert len(env["calls"]) == 1
    state = env["store"].snapshot()
    assert state.attempts[f"{REPO}#2"].outcome is Outcome.UNKNOWN and state.in_flight == 1


def test_bad_template_exits_one_without_reserving(env, tmp_path, capsys):
    bad = tmp_path / "bad.md"
    bad.write_text("no goal")
    assert env["run"](template_path=bad) == 1
    assert env["calls"] == [] and env["store"].snapshot().attempts == {}
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_github_failure_skips_repo(env, capsys):
    # Spec rule 9: an opted-in candidate skipped for a failed fetch is a
    # failed night — exit 1, error recorded, FAILED last line.
    assert env["run"](issues_loader=lambda c: (env["issues"], {REPO: "github fetch failed"})) == 1
    assert env["calls"] == []
    run = env["store"].snapshot().runs[-1]
    assert {r for _, r in run.skipped} == {"github fetch failed"}
    assert run.error == f"github fetch failed: {REPO}"
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_cold_failed_board_fails_night(env, capsys):
    # A cold board failure loads NO issues for the repo, so flagged records
    # are absent from the open set. They must still report the fetch
    # failure (rule 9), not "not open", or the night reads green.
    assert env["run"](issues_loader=lambda c: ([], {REPO: "github fetch failed"})) == 1
    assert env["calls"] == []
    run = env["store"].snapshot().runs[-1]
    assert {r for _, r in run.skipped} == {"github fetch failed"}
    assert run.error == f"github fetch failed: {REPO}"
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_worktree_probe_failure_skips_repo(env, capsys):
    assert env["run"](branch_probe=lambda path: None) == 1
    assert env["calls"] == []
    run = env["store"].snapshot().runs[-1]
    assert {r for _, r in run.skipped} == {"worktree check failed"}
    assert run.error == f"worktree check failed: {REPO}"
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_unsafe_repo_without_candidates_is_not_a_failure(env):
    # The failure only counts when it actually skipped a flagged candidate.
    other = "ben-ralston/other"
    assert env["run"](issues_loader=lambda c: (env["issues"], {other: "github fetch failed"})) == 0
    assert env["store"].snapshot().runs[-1].error is None


def test_mid_run_worktree_probe_failure_fails_night(env):
    probes = iter([set(), set(), None])  # pre-select ok, #2 ok, #3 re-probe fails
    assert env["run"](branch_probe=lambda path: next(probes)) == 1
    assert _nums(env["calls"]) == [2]
    run = env["store"].snapshot().runs[-1]
    assert run.launched == [f"{REPO}#2"]
    assert run.error == f"worktree check failed: {REPO}"


def test_existing_bugfix_branch_blocks_issue(env):
    env["run"](branch_probe=lambda path: {2})
    assert _nums(env["calls"]) == [3, 1]


def test_branch_appearing_during_first_launch_blocks_second(env):
    made = {}
    env["hooks"]["during_launch"] = lambda: made.setdefault("bugfix", {3})
    env["run"](branch_probe=lambda path: made.get("bugfix", set()))
    assert _nums(env["calls"]) == [2]
    assert (f"{REPO}#3", "worktree or branch exists") in env["store"].snapshot().runs[-1].skipped


def test_corrupt_state_exits_one(env, capsys):
    env["store"].path.parent.mkdir(parents=True, exist_ok=True)
    env["store"].path.write_text("{nope")
    assert env["run"]() == 1 and env["calls"] == []
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_refresh_marks_merged_before_selecting(env):
    env["run"]()
    env["calls"].clear()
    first = env["store"].snapshot().attempts[f"{REPO}#2"].branch
    prs = {first: PrInfo("MERGED", "u2")}
    assert env["run"](pr_lookup=lambda repo, branch: prs.get(branch, NO_PR)) == 0
    state = env["store"].snapshot()
    assert state.attempts[f"{REPO}#2"].outcome is Outcome.MERGED
    assert _nums(env["calls"]) == [1]


# --- load_issues_strict against a fake cache --------------------------------


class FakeCache:
    def __init__(self, boards, failed, closed=frozenset()):
        self.boards, self.failed, self.closed = boards, set(failed), set(closed)

    def get_issues(self, number, owner, force=False):
        assert force is True  # fail closed: the strict loader always forces
        return list(self.boards[(owner, number)])

    def get_closed_keys(self, repos, force=False):
        assert force is True
        return set(self.closed)

    def failed_keys(self):
        return set(self.failed)


def _strict_cfg(tmp_path):
    return Config(
        repos={REPO: RepoConfig(REPO, tmp_path)},
        worktree_root=tmp_path,
        projects=[ProjectConfig("healthy", 1, "@me"), ProjectConfig("broken", 2, "@me")],
        autopilot=AutopilotConfig(enabled=True, repos=(REPO,)),
    )


def _iss(n, project, status="Todo"):
    return Issue(
        number=n,
        title=f"i{n}",
        repo=REPO,
        status=status,
        priority=Priority.NO_PRI,
        project_number=project,
        project_owner="@me",
    )


def test_failed_board_with_healthy_duplicate_still_unsafe(tmp_path):
    cache = FakeCache({("@me", 1): [_iss(1, 1)], ("@me", 2): [_iss(1, 2)]}, failed={"@me/2"})
    _issues, unsafe = load_issues_strict(_strict_cfg(tmp_path), cache)
    assert unsafe == {REPO: SKIP_GITHUB}


def test_failed_board_done_item_still_marks_repo(tmp_path):
    cache = FakeCache(
        {("@me", 1): [_iss(1, 1)], ("@me", 2): [_iss(9, 2, status="Done")]}, failed={"@me/2"}
    )
    assert load_issues_strict(_strict_cfg(tmp_path), cache)[1] == {REPO: SKIP_GITHUB}


def test_closed_keys_failure_marks_repo_and_healthy_is_safe(tmp_path):
    healthy = FakeCache(
        {("@me", 1): [_iss(1, 1)], ("@me", 2): []}, failed=set(), closed={f"{REPO}#1"}
    )
    issues, unsafe = load_issues_strict(_strict_cfg(tmp_path), healthy)
    assert issues == [] and unsafe == {}
    broken = FakeCache({("@me", 1): [_iss(1, 1)], ("@me", 2): []}, failed={f"closed:{REPO}"})
    assert load_issues_strict(_strict_cfg(tmp_path), broken)[1] == {REPO: SKIP_GITHUB}


def test_cold_failed_board_marks_every_allowlisted_repo(tmp_path):
    # Board 2 failed with nothing cached: it may hold this repo's issues
    # (e.g. as Done), so the healthy board's copy is not enough to launch.
    cache = FakeCache({("@me", 1): [_iss(1, 1)], ("@me", 2): []}, failed={"@me/2"})
    assert load_issues_strict(_strict_cfg(tmp_path), cache)[1] == {REPO: SKIP_GITHUB}


def test_unexpected_error_exits_one_with_failed_last_line(env, capsys):
    def boom(cfg):
        raise RuntimeError("board parser exploded")

    assert env["run"](issues_loader=boom) == 1
    assert env["calls"] == []
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_crash_mid_run_still_records_the_night(env, capsys):
    # A raise after the first launch must not leave the previous night green.
    def flaky(argv, stdin_text, cwd):
        if env["calls"]:
            raise RuntimeError("launcher exploded")
        env["calls"].append((argv, stdin_text))
        branch = argv[argv.index("--branch") + 1]
        return subprocess.CompletedProcess(
            argv, 0, f"Launched session 's-{branch}' in /wt/{branch} (agent 'claude' x)\n", ""
        )

    env["store"].record_run(RunRecord("prev", "p", "p", [], [], False, error=None))
    assert env["run"](launcher=flaky) == 1
    run = env["store"].snapshot().runs[-1]
    assert run.run_id != "prev"
    assert run.error and "launcher exploded" in run.error
    assert run.launched == [f"{REPO}#2"]
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_crash_in_issue_loader_records_the_night(env):
    def boom(cfg):
        raise RuntimeError("board parser exploded")

    assert env["run"](issues_loader=boom) == 1
    run = env["store"].snapshot().runs[-1]
    assert "board parser exploded" in run.error and run.launched == []


def test_disabled_beats_missing_triage_block(env, tmp_path, capsys):
    cfg = tmp_path / "no-triage.yaml"
    cfg.write_text(
        f"repos: {{}}\nprojects: []\nworktree_root: {tmp_path}\nautopilot:\n  enabled: false\n"
    )
    assert env["run"](config_path=cfg) == 0
    assert "skipped (disabled)" in capsys.readouterr().out
    cfg.write_text(f"repos: {{}}\nprojects: []\nworktree_root: {tmp_path}\n")
    assert env["run"](config_path=cfg) == 0
    assert "skipped (disabled)" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["<TURN_BOUND>", "<BRANCH>"])
def test_template_missing_placeholder_exits_one_without_reserving(env, tmp_path, missing):
    bad = tmp_path / "missing.md"
    bad.write_text(TEMPLATE.replace(missing, "x"))
    assert env["run"](template_path=bad) == 1
    assert env["calls"] == [] and env["store"].snapshot().attempts == {}
    assert "goal template" in env["store"].snapshot().runs[-1].error


def test_missing_config_file_fails(env, tmp_path, capsys):
    # Only a LOADED config with autopilot absent/disabled is a quiet skip.
    assert env["run"](config_path=tmp_path / "nope.yaml") == 1
    assert "FAILED" in capsys.readouterr().out.strip().splitlines()[-1]


def test_dry_run_says_no_state_written(env, capsys):
    assert env["run"](dry_run=True) == 0
    assert "DRY RUN — no state written" in capsys.readouterr().out


def _bases(calls):
    return [argv[argv.index("--base") + 1] for argv, _ in calls]


def test_launch_uses_github_default_branch(env):
    looked = []

    def lookup(repo):
        looked.append(repo)
        return "dev"

    assert env["run"](base_lookup=lookup) == 0
    assert _bases(env["calls"]) == ["dev", "dev"]
    assert looked == [REPO]  # two launches, one lookup per repo per run


def test_override_skips_lookup(env):
    env["write_cfg"](base_branch="trunk")

    def lookup(repo):
        raise AssertionError("override must not ask GitHub")

    assert env["run"](base_lookup=lookup) == 0
    assert _bases(env["calls"]) == ["trunk", "trunk"]


def test_base_unknown_skips_and_fails_night(env):
    assert env["run"](base_lookup=lambda repo: None) == 1
    assert env["calls"] == []
    state = env["store"].snapshot()
    assert state.attempts == {}  # no reservation left behind
    run = state.runs[-1]
    assert (f"{REPO}#2", "base branch unknown") in run.skipped
    assert f"base branch unknown: {REPO}" in run.error


def test_base_lookup_is_lazy(env):
    """The other repo's base is looked up only when its launch is reached —
    a failed first launch stops the night before it."""
    other = "ben-ralston/other"
    env["write_cfg"](extra_repo=other)
    env["triage"].upsert(
        TriageRecord(
            issue_key=f"{other}#9",
            value=5,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            autopilot=True,
            updated_at="t",
        ),
        actor="human",
    )
    env["triage"].confirm(f"{other}#9", now="t")
    issues = env["issues"] + [
        Issue(number=9, title="o", repo=other, status="Todo", priority=Priority.NO_PRI)
    ]
    looked = []

    def lookup(repo):
        looked.append(repo)
        return "main"

    def failing(argv, stdin_text, cwd):
        env["calls"].append((argv, stdin_text))
        return subprocess.CompletedProcess(argv, 1, "", "Error: launcher:worktree: boom")

    rc = env["run"](
        base_lookup=lookup,
        launcher=failing,
        issues_loader=lambda c: (issues, {}),
    )
    assert rc == 1 and len(env["calls"]) == 1
    assert looked == [REPO]


def test_dry_run_prints_base(env, capsys):
    assert env["run"](dry_run=True, base_lookup=lambda repo: "dev") == 0
    assert "(base dev)" in capsys.readouterr().out
    assert env["run"](dry_run=True, base_lookup=lambda repo: None) == 0
    out = capsys.readouterr().out
    assert f"skip {REPO}#2: base branch unknown" in out


def _failing_launcher(env, rc, stderr):
    def launcher(argv, stdin_text, cwd):
        env["calls"].append((argv, stdin_text))
        return subprocess.CompletedProcess(argv, rc, "", stderr)

    return launcher


def test_launcher_failure_runs_cleanup_and_notes_detail(env, monkeypatch, capsys):
    emitted = []
    monkeypatch.setattr(
        autopilot_cli.eventlog, "emit", lambda level, msg, extra=None: emitted.append(extra)
    )
    cleaned = []

    def cleanup(path, branch):
        cleaned.append((path, branch))
        return "cleaned up worktree and branch"

    stderr = "wt: hook install failed: ./run: No such file\n" + (
        "Error: launcher:worktree: wt switch failed\n"
    )
    rc = env["run"](
        launcher=_failing_launcher(env, 1, stderr),
        base_lookup=lambda repo: "dev",
        cleanup=cleanup,
    )
    assert rc == 1
    branch = _branches(env["calls"])[0]
    assert cleaned == [(env["data"].parent / "repo", branch)]
    attempt = env["store"].snapshot().attempts[f"{REPO}#2"]
    assert attempt.outcome is Outcome.LAUNCH_FAILED
    assert attempt.detail == (
        "Error: launcher:worktree: wt switch failed (cleaned up worktree and branch)"
    )
    failure = next(e for e in emitted if e and e.get("status") == "failed")
    assert failure["cleanup"] == "cleaned up worktree and branch"
    assert "hook install failed" in failure["stderr_tail"]
    assert "hook install failed" in capsys.readouterr().out  # → autopilot.log


def test_uncertain_launch_skips_cleanup(env):
    def cleanup(path, branch):
        raise AssertionError("a session may be running")

    stderr = "Error: target:verify: no harness (tmux session 's'; attach: tmux attach -t s)\n"
    assert env["run"](launcher=_failing_launcher(env, 2, stderr), cleanup=cleanup) == 1
    assert env["store"].snapshot().attempts[f"{REPO}#2"].outcome is Outcome.UNKNOWN


def test_cleanup_exception_still_records_the_outcome(env):
    def cleanup(path, branch):
        raise ValueError("bad bytes")

    stderr = "Error: launcher:worktree: wt switch failed\n"
    assert env["run"](launcher=_failing_launcher(env, 1, stderr), cleanup=cleanup) == 1
    attempt = env["store"].snapshot().attempts[f"{REPO}#2"]
    assert attempt.outcome is Outcome.LAUNCH_FAILED
    assert "cleanup failed" in attempt.detail


def test_dry_run_count_excludes_base_unknown(env, capsys):
    assert env["run"](dry_run=True, base_lookup=lambda repo: None) == 0
    assert "would launch 0" in capsys.readouterr().out
