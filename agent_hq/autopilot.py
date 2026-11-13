"""Autopilot (#302): pick opted-in issues and launch goal-mode agents.

The selection half is pure. No LLM decides what runs: the owner's ``autopilot``
flag plus a CONFIRMED triage record replace /worktree-create's tier
classification. Every uncertain input fails CLOSED (Codex plan review):
a repo whose GitHub fetch or worktree probe failed is skipped wholesale.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from agent_hq import gh_meter
from agent_hq.autopilot_store import (
    Attempt,
    AutopilotState,
    AutopilotStore,
    Outcome,
    OutcomeUpdate,
)
from agent_hq.config import AutopilotConfig, Config
from agent_hq.dispatch import generate_branch_name
from agent_hq.github import GitHubCache
from agent_hq.issues_hub import merge_issue_lists
from agent_hq.models import Issue
from agent_hq.triage_store import TriageRecord, TriageState, rank_score

SKIP_NOT_CONFIRMED = "not confirmed"
SKIP_NOT_OPEN = "not open"
SKIP_REPO = "repo not allowlisted"
SKIP_ATTEMPTED = "already attempted"
SKIP_WORKTREE = "worktree or branch exists"
SKIP_GITHUB = "github fetch failed"
SKIP_WORKTREE_UNKNOWN = "worktree check failed"
SKIP_THROTTLED = "throttled"
SKIP_CHANGED = "opted out or unconfirmed during run"
SKIP_PAUSED = "paused during run"
SKIP_BASE_UNKNOWN = "base branch unknown"


@dataclass
class Selection:
    launch: list[Issue] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    throttled: bool = False


@dataclass(frozen=True)
class PrInfo:
    state: str  # gh: OPEN | MERGED | CLOSED
    url: str


NO_PR = PrInfo("NONE", "")
PrLookup = Callable[[str, str], "PrInfo | None"]


def record_still_eligible(rec: TriageRecord | None) -> bool:
    return (
        rec is not None
        and rec.autopilot is True
        and rec.tombstoned_at is None
        and rec.state is TriageState.CONFIRMED
    )


def select(
    issues: Iterable[Issue],
    records: Mapping[str, TriageRecord],
    blocked_keys: set[str],
    *,
    in_flight: int,
    active_issue_keys: set[str],
    unsafe_repos: Mapping[str, str],
    cfg: AutopilotConfig,
    weights: dict | None = None,
) -> Selection:
    open_by_key = {i.key: i for i in issues}
    sel = Selection()
    eligible: list[tuple[Issue, TriageRecord]] = []
    for key in sorted(records):
        rec = records[key]
        if rec.autopilot is not True:
            continue
        repo = key.rsplit("#", 1)[0]
        if rec.state is not TriageState.CONFIRMED:
            reason = SKIP_NOT_CONFIRMED
        elif rec.tombstoned_at is not None:
            reason = SKIP_NOT_OPEN
        elif key not in open_by_key:
            # A cold board failure loads nothing for the repo; absence is
            # then the fetch failure, not proof the issue closed (rule 9).
            # Only for allowlisted repos: autopilot never launches elsewhere,
            # so a failure there must not fail the night.
            reason = unsafe_repos.get(repo, SKIP_NOT_OPEN) if repo in cfg.repos else SKIP_NOT_OPEN
        elif repo not in cfg.repos:
            reason = SKIP_REPO
        elif key in blocked_keys:
            reason = SKIP_ATTEMPTED
        elif key in active_issue_keys:
            # Per-key evidence beats the repo-wide failure reason; both skip.
            reason = SKIP_WORKTREE
        elif repo in unsafe_repos:
            reason = unsafe_repos[repo]
        else:
            eligible.append((open_by_key[key], rec))
            continue
        sel.skipped.append((key, reason))

    eligible.sort(key=lambda p: (-(rank_score(p[1], weights) or 0), p[0].number))
    n = max(0, min(cfg.per_night, cfg.max_open - in_flight))
    sel.throttled = n == 0 and bool(eligible)
    sel.launch = [issue for issue, _ in eligible[:n]]
    sel.skipped.extend((issue.key, SKIP_THROTTLED) for issue, _ in eligible[n:])
    return sel


_PR_OUTCOME = {"OPEN": Outcome.PR_OPEN, "MERGED": Outcome.MERGED, "CLOSED": Outcome.CLOSED}


def compute_outcomes(
    attempts: Mapping[str, Attempt],
    pr_lookup: PrLookup,
    session_alive: Callable[[str], bool | None],
) -> dict[str, OutcomeUpdate]:
    """Compare-and-set updates for RUNNING / PR_OPEN / UNKNOWN attempts.

    A failed lookup (None) or an undecidable session probe (None) changes
    nothing — failure never frees capacity. LAUNCHING is left alone: only
    the run that reserved it (finish_launch) or the owner (Retry, once stale)
    resolves it.

    UNKNOWN (an uncertain launch) needs the owner's Retry (spec rule 7): a live
    session is NOT evidence the launch worked, so it stays UNKNOWN — in
    flight and in Needs attention. It moves only on positive evidence: a
    real PR for its branch, or its session positively gone.
    """
    out: dict[str, OutcomeUpdate] = {}
    for key, a in attempts.items():
        if a.outcome not in (Outcome.RUNNING, Outcome.PR_OPEN, Outcome.UNKNOWN):
            continue
        pr = pr_lookup(a.repo, a.branch)
        if pr is None:
            continue
        if pr is not NO_PR:
            new = _PR_OUTCOME.get(pr.state)
            if new is not None and (new is not a.outcome or pr.url != a.pr_url):
                out[key] = OutcomeUpdate(a.attempt_id, a.outcome, new, pr_url=pr.url)
            continue
        if a.outcome is Outcome.PR_OPEN or not a.session_name:
            continue
        alive = session_alive(a.session_name)
        if alive is None:
            continue
        if not alive:
            out[key] = OutcomeUpdate(a.attempt_id, a.outcome, Outcome.ENDED_WITHOUT_PR)
    return out


# --- Launch, goal condition and strict probes (Task 6) -----------------------

LAUNCH_SCRIPT = Path.home() / ".claude/skills/worktree/scripts/launch.sh"
GOAL_TEMPLATE = Path.home() / ".claude/skills/goal-mode/templates/dispatch-condition.md"
# launch.sh runs the repo's wt pre-start hook synchronously (e.g. magic-eye-gen's
# `./run initialize`: npm install + vercel env pull) before the harness starts.
LAUNCH_TIMEOUT_S = 900
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)
_PLACEHOLDER = re.compile(r"<[A-Z_]+>")
_REQUIRED_PLACEHOLDERS = ("<ISSUE>", "<BRANCH>", "<TURN_BOUND>")
_LAUNCHED = re.compile(r"Launched session '([^']+)' in (.+?) \(agent ")
_TARGET_SESSION = re.compile(r"tmux session '([^']+)'")
_ISSUE_BRANCH = re.compile(r"^(?:feature|bugfix|chore|docs)/(\d+(?:-\d+)*)-")
_SESSION_ABSENT = re.compile(
    r"can't find session|no server running|error connecting to .*No such file or directory"
)


class GoalTemplateError(Exception):
    pass


def fill_goal_condition(
    template_text: str, *, issue_number: int, branch: str, turn_bound: int
) -> str:
    """Strip the leading HTML comment explicitly (the real header itself
    mentions ``/goal <this>``), require the remainder to START with the
    executable /goal line, require every placeholder (``<ISSUE>``,
    ``<BRANCH>``, ``<TURN_BOUND>``) in that remainder — a goal without its
    turn bound or branch would run unbounded or on the wrong branch —
    substitute, and refuse any leftover placeholder."""
    text = template_text
    if text.lstrip().startswith("<!--"):
        m = _LEADING_COMMENT.match(text)
        if m is None:
            raise GoalTemplateError("goal template header comment is not closed")
        text = text[m.end() :]
    text = text.strip()
    if not text.startswith("/goal "):
        raise GoalTemplateError("goal template does not start with a /goal line")
    missing = [p for p in _REQUIRED_PLACEHOLDERS if p not in text]
    if missing:
        raise GoalTemplateError(f"goal template is missing required placeholders: {missing}")
    text = (
        text.replace("<ISSUE>", str(issue_number))
        .replace("<BRANCH>", branch)
        .replace("<TURN_BOUND>", str(turn_bound))
    )
    leftover = _PLACEHOLDER.findall(text)
    if leftover:
        raise GoalTemplateError(f"goal template has unfilled placeholders: {sorted(set(leftover))}")
    return text


Launcher = Callable[[list[str], str, Path], subprocess.CompletedProcess]


def default_launcher(argv: list[str], stdin_text: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=stdin_text, cwd=cwd, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S
    )


class LaunchStatus(Enum):
    OK = "ok"
    FAILED = "failed"  # nothing is running (launcher-stage)
    UNCERTAIN = "uncertain"  # a session may be running (exit 2 / timeout)


@dataclass
class LaunchResult:
    status: LaunchStatus
    launcher_failure: bool = False
    session_name: str | None = None
    worktree_path: str | None = None
    detail: str | None = None
    # Last STDERR_TAIL_LINES of launch.sh stderr on a non-zero exit (#312):
    # wt's own error lands here, not in the one-line detail.
    stderr_tail: str | None = None


STDERR_TAIL_LINES = 15
STDERR_TAIL_CHARS = 2000


def _last_line(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return (lines[-1] if lines else f"launch.sh exit {proc.returncode}")[:500]


def _stderr_tail(proc: subprocess.CompletedProcess) -> str | None:
    lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
    tail = "\n".join(lines[-STDERR_TAIL_LINES:])
    return tail[-STDERR_TAIL_CHARS:] or None


def run_launcher(
    *,
    branch: str,
    prompt: str,
    cfg: AutopilotConfig,
    repo_path: Path,
    launcher: Launcher,
    base: str,
    script: Path = LAUNCH_SCRIPT,
) -> LaunchResult:
    """launch.sh contract: 0 = launched + harness verified; 1 = launcher-stage
    failure (no session); 2 = target-stage failure (session LEFT RUNNING).
    Exit 2, any other non-zero exit, and a timeout are UNCERTAIN — they keep
    holding capacity."""
    argv = [str(script), "--branch", branch, "--base", base, "--auto", "--model", cfg.model]
    try:
        proc = launcher(argv, prompt, repo_path)
    except subprocess.TimeoutExpired:
        return LaunchResult(
            LaunchStatus.UNCERTAIN, detail=f"launch.sh timed out after {LAUNCH_TIMEOUT_S}s"
        )
    except OSError as exc:
        return LaunchResult(LaunchStatus.FAILED, launcher_failure=True, detail=f"launch.sh: {exc}")
    if proc.returncode == 0:
        m = _LAUNCHED.search(proc.stdout or "")
        return LaunchResult(
            LaunchStatus.OK,
            session_name=m.group(1) if m else None,
            worktree_path=m.group(2) if m else None,
        )
    if proc.returncode == 1:
        return LaunchResult(
            LaunchStatus.FAILED,
            launcher_failure=True,
            detail=_last_line(proc),
            stderr_tail=_stderr_tail(proc),
        )
    m = _TARGET_SESSION.search(proc.stderr or "")
    return LaunchResult(
        LaunchStatus.UNCERTAIN,
        session_name=m.group(1) if m else None,
        detail=_last_line(proc),
        stderr_tail=_stderr_tail(proc),
    )


_ATTEMPT_SUFFIX = re.compile(r"-ap[0-9a-f]{6}$")


def cleanup_failed_launch(repo_path: Path, branch: str, runner=None) -> str:
    """Remove what a launcher-stage failure left behind (#312): the worktree
    checked out on this attempt's own ``-ap<id>`` branch, then the branch —
    otherwise the strict branch probe skips the issue forever, even after
    Retry. Refuses a branch without the attempt suffix or with any commit no
    other branch, remote or tag reaches (no work is ever destroyed) — judged
    without the base name, which may exist only as ``origin/<base>`` locally.
    Plain git, never ``wt remove`` (its hooks kill tmux sessions and touch the
    remote). Returns a short note for
    the attempt detail; never raises."""
    run = runner or subprocess.run

    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", "-C", str(repo_path), *args], capture_output=True, text=True, timeout=30)

    if not _ATTEMPT_SUFFIX.search(branch):
        return "cleanup refused: not an autopilot attempt branch"
    try:
        listing = git("worktree", "list", "--porcelain")
        if listing.returncode != 0:
            return "cleanup failed: git worktree list"
        worktree = None
        current = None
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                current = line.removeprefix("worktree ")
            elif line == f"branch refs/heads/{branch}":
                worktree = current
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return "nothing to clean" if worktree is None else "cleanup failed: no branch ref"
        # --exclude patterns for --branches are relative to refs/heads/.
        ahead = git(
            "rev-list", "--count", branch, "--not", f"--exclude={branch}",
            "--branches", "--remotes", "--tags",
        )  # fmt: skip
        if ahead.returncode != 0:
            return "cleanup failed: git rev-list"
        if ahead.stdout.strip() != "0":
            return "cleanup refused: branch has commits"
        if worktree is not None:
            if git("worktree", "remove", "--force", worktree).returncode != 0:
                return "cleanup failed: git worktree remove"
        if git("branch", "-D", branch).returncode != 0:
            return "cleanup failed: git branch -D"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"cleanup failed: {exc}"
    return "cleaned up worktree and branch" if worktree is not None else "cleaned up branch"


def fetch_default_branch(repo: str) -> str | None:
    """The repo's GitHub default branch (#312: magic-eye-gen's ``main`` is a
    stub; real work is on ``dev``). REST, so 0 GraphQL points. None on any
    failure — the caller skips the issue rather than guess ``main``."""
    try:
        proc = gh_meter.run_gh(
            ["gh", "api", f"repos/{repo}", "-q", ".default_branch"], kind="autopilot-base"
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def issue_branch_numbers(repo_path: Path, runner=None) -> set[int] | None:
    """Issue numbers that already have a worktree OR a local branch under any
    supported convention. None on any git failure — callers skip the repo.

    ``runner`` defaults to ``subprocess.run`` resolved at CALL time (not bound
    at import), so the test suite's subprocess guards still see the default."""
    run = runner or subprocess.run
    nums: set[int] = set()
    try:
        wt = run(
            ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        br = run(
            [
                "git",
                "-C",
                str(repo_path),
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if wt.returncode != 0 or br.returncode != 0:
        return None
    names = [
        ln.removeprefix("branch refs/heads/")
        for ln in wt.stdout.splitlines()
        if ln.startswith("branch refs/heads/")
    ]
    names += [ln.strip() for ln in br.stdout.splitlines() if ln.strip()]
    for name in names:
        m = _ISSUE_BRANCH.match(name)
        if m:
            nums.update(int(n) for n in m.group(1).split("-"))
    return nums


def fetch_pr(repo: str, branch: str) -> PrInfo | None:
    """The PR for exactly this head branch (any state). ``NO_PR`` itself (the
    singleton — ``compute_outcomes`` matches it by identity) when GitHub says
    there is none; None when the lookup failed or the answer is malformed."""
    try:
        proc = gh_meter.run_gh(
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "state,url",
                "--limit",
                "1",
            ],
            kind="autopilot-pr",
        )
        if proc.returncode != 0:
            return None
        rows = json.loads(proc.stdout or "")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    if not rows:
        return NO_PR
    row = rows[0]
    if not isinstance(row, dict) or not isinstance(row.get("state"), str):
        return None
    return PrInfo(row["state"], str(row.get("url") or ""))


def tmux_session_alive(name: str, runner=None) -> bool | None:
    """True/False only when tmux positively says so. The ``=name`` target is
    an exact match — a bare name would prefix-match another session. A socket permission or
    connection error is None (unknown) — never "the agent ended", which
    would release capacity (Codex round 2). ``runner`` defaults to
    ``subprocess.run`` resolved at call time."""
    run = runner or subprocess.run
    try:
        proc = run(
            ["tmux", "has-session", "-t", f"={name}"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode == 0:
        return True
    return False if _SESSION_ABSENT.search(proc.stderr or "") else None


def attempt_branch(issue: Issue, attempt_id: str) -> str:
    """Unique per attempt (Codex round 2): a retry must never reuse a branch
    whose historical PR would be mistaken for this attempt's outcome."""
    return f"{generate_branch_name([issue])}-ap{attempt_id[:6]}"


# --- Run-time wiring (Task 7) ------------------------------------------------


def sync_outcomes(
    store: AutopilotStore,
    *,
    now: str,
    pr_lookup: PrLookup = fetch_pr,
    session_probe: Callable[[str], bool | None] = tmux_session_alive,
    write: bool = True,
) -> AutopilotState:
    """Observe outside the lock, apply compare-and-set inside it — a slow
    observation can never rewrite a replacement attempt. ``write=False``
    (dry run) applies the updates to a snapshot copy only."""
    state = store.snapshot()
    updates = compute_outcomes(state.attempts, pr_lookup, session_probe)
    if write:
        store.apply_outcomes(updates, now=now)
        return store.snapshot()
    for key, u in updates.items():
        state.attempts[key].outcome = u.new
    return state


def load_issues_strict(
    cfg: Config, cache: GitHubCache | None = None
) -> tuple[list[Issue], dict[str, str]]:
    """Force-fetch every board and the closed keys; return the open issues
    plus ``unsafe_repos`` (repo -> SKIP_GITHUB).

    The cache serves last-good data on failure, so the verdict comes from
    ``failed_keys()`` (``owner/number`` for boards, ``closed:<repo>`` for
    closed keys). A repo is unsafe when ANY issue of it appears in a failed
    board's raw (pre-merge) list — judged before ``merge_issue_lists`` drops
    Done items and de-dups, so a healthy duplicate can't mask the failure.
    A failed board with NOTHING cached says nothing about which repos it
    covers, so it marks every allowlisted repo unsafe (fail closed: the
    same repo may also sit on a healthy board). ``RepoConfig.project`` is
    an Idea-Inbox name, not a board link — deliberately not used.
    """
    cache = cache or GitHubCache()
    allowlisted = set(cfg.autopilot.repos) if cfg.autopilot else set()
    per_board = [(p, cache.get_issues(p.number, p.owner, force=True)) for p in cfg.projects]
    failed = cache.failed_keys()
    unsafe: dict[str, str] = {}
    for p, board in per_board:
        if f"{p.owner}/{p.number}" not in failed:
            continue
        if not board:
            unsafe.update(dict.fromkeys(allowlisted, SKIP_GITHUB))
        for issue in board:
            unsafe[issue.repo] = SKIP_GITHUB
    issues = merge_issue_lists([board for _, board in per_board])
    repos = {i.repo for i in issues} | allowlisted
    closed = cache.get_closed_keys(repos, force=True) if repos else set()
    failed = cache.failed_keys()
    for repo in repos:
        if f"closed:{repo}" in failed:
            unsafe[repo] = SKIP_GITHUB
    return [i for i in issues if i.key not in closed], unsafe
