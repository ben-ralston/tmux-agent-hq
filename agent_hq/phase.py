"""Dev-phase detection for agent worktrees.

Deterministic, signal-based phase inference. No LLM calls.

Every exported helper is a pure function of (repo_root, branch) plus
process-local caches. Expensive signals (``gh pr view``, multi-file git
walks) are cached with a short TTL so the 2s discovery loop stays cheap.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import pygit2

from .models import DevPhase, normalize_phase_value
from .transcript import extract_last_phase_tag

log = logging.getLogger(__name__)

# Cache TTL for expensive signals (gh pr view, git log counts).
# 60s gives "feels live" UX while keeping gh calls under 1/min per branch.
PHASE_CACHE_TTL_SECONDS = 60

# Recency window for plan / spec file signals.
# Files touched within this window count as "active" signals for phase detection.
RECENT_EDIT_WINDOW_SECONDS = 24 * 3600  # 24 hours

# Branch-name prefixes recognized as "a task is underway" — used as a soft
# default when no other phase signal fires (issue #55 Pattern A: a fresh
# worktree's first turn hasn't emitted <phase> yet, and ADHOC is wrong for a
# branch whose name *is* the task). Only canonical prefixes — short forms
# like ``feat/`` aren't treated as a clear task.
TYPED_FEATURE_BRANCH_PREFIXES = ("feature/", "bugfix/", "chore/", "docs/")

# Substrings that, if present in recent pane content, suggest manual QA is underway.
TEST_COMMAND_PATTERNS = [
    "pytest",
    "npm test",
    "npm run dev",
    "./run test",
    "uv run pytest",
    "go test",
    "cargo test",
    "playwright",
]

# Process-local cache keyed by (repo_root, branch) → (expire_at_monotonic, value).
# Lives for the lifetime of a daemon process; cleared on restart.
_cache: dict[tuple[str, str, str], tuple[float, object]] = {}


def _cache_get(key: tuple[str, str, str]) -> object | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(
    key: tuple[str, str, str], value: object, ttl: float = PHASE_CACHE_TTL_SECONDS
) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


# Per-process Repository cache shared with enrichment.py. Keyed by the .git
# path discovered for a cwd so each linked worktree gets its own entry
# (worktrees share a common dir but expose distinct .git/worktrees/<name>/
# pointers).
_repo_cache: dict[str, pygit2.Repository] = {}

# Every pygit2 call site catches this pair. pygit2's C extension maps
# libgit2's ``GIT_ERROR_OS`` class to a bare ``OSError`` — NOT ``GitError`` —
# so a repo file the OS refuses to read (seen on macOS:
# ``failed to read descriptor: Resource deadlock avoided``) escaped every
# ``except pygit2.GitError`` and aborted the daemon's whole tick for every
# agent. Git context degrades per agent instead; the daemon tick never sees it.
_PYGIT2_ERRORS = (pygit2.GitError, OSError)


def _open_repo(cwd: str) -> pygit2.Repository | None:
    """Discover the repo containing ``cwd`` and return a cached Repository.

    Drops and re-opens the cache entry if the previously-cached gitdir is
    gone from disk (e.g. after `git worktree remove`). For `wt remove + wt
    add` recreating at the same path within one daemon run, callers should
    rely on ``_drop_cached_repo`` triggered by a downstream ``GitError``.
    """
    try:
        gitdir = pygit2.discover_repository(cwd)
    except _PYGIT2_ERRORS:
        return None
    if gitdir is None:
        return None

    repo = _repo_cache.get(gitdir)
    if repo is not None and not Path(gitdir).exists():
        _repo_cache.pop(gitdir, None)
        repo = None

    if repo is None:
        try:
            repo = pygit2.Repository(gitdir)
        except _PYGIT2_ERRORS:
            return None
        _repo_cache[gitdir] = repo
    return repo


def _drop_cached_repo(cwd: str) -> None:
    """Drop the cached Repository for the repo containing ``cwd``, if any.

    Called by consumers when a pygit2 op raises ``GitError`` / ``OSError``
    so the next call re-opens against fresh on-disk state.
    """
    try:
        gitdir = pygit2.discover_repository(cwd)
    except _PYGIT2_ERRORS:
        return
    if gitdir:
        _repo_cache.pop(gitdir, None)


def _clear_repo_cache() -> None:
    """Test hook: drop the Repository cache."""
    _repo_cache.clear()


def has_recent_plan_file(repo_root: str) -> bool:
    """True if docs/superpowers/plans/*.md was edited within the recency window."""
    plans_dir = Path(repo_root) / "docs" / "superpowers" / "plans"
    if not plans_dir.is_dir():
        return False
    now = time.time()
    try:
        for entry in plans_dir.iterdir():
            if entry.suffix != ".md":
                continue
            if now - entry.stat().st_mtime <= RECENT_EDIT_WINDOW_SECONDS:
                return True
    except OSError:
        return False
    return False


def has_recent_spec_file(repo_root: str) -> bool:
    """True if docs/superpowers/specs/*.md was edited within the recency window."""
    specs_dir = Path(repo_root) / "docs" / "superpowers" / "specs"
    if not specs_dir.is_dir():
        return False
    now = time.time()
    try:
        for entry in specs_dir.iterdir():
            if entry.suffix != ".md":
                continue
            if now - entry.stat().st_mtime <= RECENT_EDIT_WINDOW_SECONDS:
                return True
    except OSError:
        return False
    return False


def commits_since_base(repo_root: str, branch: str, *, base_ref: str = "main") -> int:
    """Count commits on ``branch`` that are not reachable from ``base_ref``.

    Equivalent to ``git rev-list --count base_ref..branch``. Returns 0 on
    any git error — phase detection should degrade silently rather than
    raise. Uses ``revparse_single`` so any rev spec works (branch, tag,
    remote ref, raw SHA, "HEAD" on detached HEAD).
    """
    repo = _open_repo(repo_root)
    if repo is None:
        return 0
    try:
        base_oid = repo.revparse_single(base_ref).peel(pygit2.Commit).id
        branch_oid = repo.revparse_single(branch).peel(pygit2.Commit).id
        walker = repo.walk(branch_oid, pygit2.GIT_SORT_NONE)
        walker.hide(base_oid)
        return sum(1 for _ in walker)
    except (KeyError, *_PYGIT2_ERRORS):
        _drop_cached_repo(repo_root)
        return 0


def get_pr_state(repo_root: str, branch: str) -> str | None:
    """Return 'open' / 'merged' / 'closed' / None for the branch's PR (cached).

    Calls ``gh pr view <branch> --json state`` under the repo_root.
    Cached per (repo_root, branch) for PHASE_CACHE_TTL_SECONDS.
    """
    import json as _json

    key = ("pr_state", repo_root, branch)
    cached = _cache_get(key)
    if cached is not None or key in _cache:
        # "not present" is also a cacheable answer — use the key-in-dict check above
        return cached  # type: ignore[return-value]

    from . import gh_meter

    try:
        # Metered like every other gh call (#255 review): this is a GraphQL
        # point per branch per memo expiry, and `gh-stats` must see it.
        result = gh_meter.run_gh(
            ["gh", "pr", "view", branch, "--json", "state"],
            kind="pr-view",
            cwd=repo_root,
        )
    except subprocess.CalledProcessError:
        # No PR for this branch (gh exits 1) — a cacheable "not present".
        _cache_set(key, None)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _cache_set(key, None)
        return None

    if result.returncode != 0 or not result.stdout:
        _cache_set(key, None)
        return None

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        _cache_set(key, None)
        return None

    state_raw = data.get("state", "").lower()
    mapped: str | None
    if state_raw == "open":
        mapped = "open"
    elif state_raw == "merged":
        mapped = "merged"
    elif state_raw == "closed":
        mapped = "closed"
    else:
        mapped = None
    _cache_set(key, mapped)
    return mapped


def _clear_pr_cache() -> None:
    """Test hook: drop all cached PR state entries."""
    for key in list(_cache):
        if key[0] == "pr_state":
            _cache.pop(key, None)


# Functools-style .cache_clear() ergonomics for test patching ease
get_pr_state.cache_clear = _clear_pr_cache  # type: ignore[attr-defined]


def has_qa_signal(pane_content: str) -> bool:
    """True if the recent pane content mentions a test or dev-server command."""
    if not pane_content:
        return False
    lowered = pane_content.lower()
    return any(p.lower() in lowered for p in TEST_COMMAND_PATTERNS)


def detect_transcript_phase(transcript_path: str) -> DevPhase | None:
    """Phase from the last ``<phase>`` tag Claude emitted in its transcript.

    The authoritative signal (a Claude agent emitting ``<phase>X</phase>``
    as the last line of its message). Reads the JSONL directly — no hook,
    no race against the flush. Returns ``None`` when the transcript has
    no recoverable tag yet, so the caller can fall back to the branch
    rule below.
    """
    tag = extract_last_phase_tag(transcript_path)
    if tag is None:
        return None
    try:
        return DevPhase(normalize_phase_value(tag))
    except ValueError:
        return None


def detect_branch_phase(
    *,
    repo_root: str | None,
    branch: str | None,
    agent_state_idle: bool,
    pane_content: str,
) -> DevPhase | None:
    """Classify which dev phase a branch is in.

    Args:
        repo_root: Main repo root (from git_context). None → no phase.
        branch: Current branch name. None → no phase.
        agent_state_idle: Whether the agent is currently IDLE (from AgentState).
            IDLE + commits + QA-signal → QA; IDLE + commits alone → Implement.
        pane_content: Recent tmux pane content for QA-signal detection.

    Returns:
        DevPhase, or None when the agent has no repo context.

    Ordering of signals matters — first match wins, "most specific" first:
        1. PR merged → DONE
        2. PR open → REVIEW
        3. commits > 0 + QA signal + IDLE → QA
        4. commits > 0 → IMPLEMENT
        5. plan file OR spec file recent → DESIGN
        6. typed feature/bugfix/chore/docs branch → DESIGN
        7. otherwise → ADHOC

    (#108 collapsed the former plan-file→PLAN and spec-file→BRAINSTORM rules,
    and the typed-branch BRAINSTORM default, into a single ``DESIGN`` phase.)
    """
    if not repo_root or not branch:
        return None

    pr_state = get_pr_state(repo_root, branch)
    if pr_state == "merged":
        return DevPhase.DONE
    if pr_state == "open":
        return DevPhase.REVIEW

    n_commits = commits_since_base(repo_root, branch)
    if n_commits > 0:
        if agent_state_idle and has_qa_signal(pane_content):
            return DevPhase.QA
        return DevPhase.IMPLEMENT

    # A recent plan or spec file means design work is underway (#108: the two
    # used to map to PLAN / BRAINSTORM respectively).
    if has_recent_plan_file(repo_root) or has_recent_spec_file(repo_root):
        return DevPhase.DESIGN

    # Soft default for typed feature branches with no other signals yet.
    # Closes the "fresh worktree shows ADHOC" gap from issue #55 Pattern A.
    if branch.startswith(TYPED_FEATURE_BRANCH_PREFIXES):
        return DevPhase.DESIGN

    return DevPhase.ADHOC
