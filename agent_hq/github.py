"""Query GitHub Projects API via gh CLI."""

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import eventlog, gh_board, gh_budget, gh_index, gh_meter
from ._locking import best_effort_file_lock
from .config import RepoConfig
from .models import Issue, Priority

_CACHE_DIR = Path.home() / ".config" / "agent-hq" / "cache"
_CACHE_FILE = _CACHE_DIR / "github.json"

# Match issue/PR URLs like https://github.com/owner/name/issues/123
_ISSUE_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+")
# `gh issue comment` prints the new comment's URL (#414).
_COMMENT_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+#issuecomment-\d+"
)

# Hard cap on every gh invocation, read-only and mutating alike. A hung gh
# would otherwise stall a `run_in_threadpool` worker forever — blocking the
# dispatch tab for reads (#90), or leaking a wedged thread per hung mutation
# (create_issue, add_to_project, etc.) until the whole threadpool starves
# (#182).
#
# Note on mutation timeouts: killing the local gh process on expiry does NOT
# undo a GitHub-side mutation gh may have already sent before the kill — a
# GHError/False from a timed-out mutation means "the local wait gave up",
# not "the mutation didn't happen". A caller that blindly retries on any
# GHError could in theory double-file. This is a deliberate tradeoff:
# threadpool starvation from an unbounded hang is worse and far more likely
# than a rare double-mutation from a >10s-but-successful gh call, but an
# idempotency-aware caller (e.g. IdeaHandler) should treat a mutation
# timeout as ambiguous rather than a clean failure to retry.
_GH_TIMEOUT = 10


class GHError(Exception):
    """Raised when a `gh` CLI invocation fails or returns malformed output.

    Used by mutating wrappers (create_issue, add_to_project, transfer_issue)
    where the caller needs to distinguish failure modes from success.
    """


class GHTimeoutError(GHError):
    """A mutating gh call timed out: the change may still have landed on
    GitHub (see ``_GH_TIMEOUT``). A ``GHError`` subclass so existing callers
    are unaffected; the remote write tools (#414) report it as uncertain."""


class GitHubFetchError(GHError):
    """A read-only fetch could not complete (#254).

    The backlog fetchers used to collapse every failure — rate limit, auth
    expiry, network, GitHub 5xx — to ``[]``/``{}``/``None``, which is
    indistinguishable from "GitHub says there is nothing here". A caller that
    cannot tell them apart caches the empty answer over its last-good data
    and renders a benign empty state while the API is down. Raising instead
    keeps "could not fetch" a distinct, cache-preserving signal.

    ``rate_limited`` / ``reset_at`` are populated when gh's stderr carries the
    rate-limit signature, so the UI can say when the wait ends.
    """

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        reset_at: float | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.rate_limited = rate_limited
        self.reset_at = reset_at


# gh surfaces both the REST ("API rate limit exceeded") and the GraphQL
# ("GraphQL: API rate limit already exceeded") wording; the shared substring
# is what we match on.
_RATE_LIMIT_RE = re.compile(r"rate limit", re.IGNORECASE)

def _rate_limit_reset() -> float | None:
    """Epoch seconds at which the exhausted GitHub budget resets.

    Delegates to :mod:`agent_hq.gh_budget` so there is exactly one
    ``rate_limit`` probe in the codebase — and one that survives the
    endpoint's replica inconsistency.

    This function used to take a **single** read and filter for
    ``remaining == 0``. Measured 2026-08-26 with GraphQL provably dead, 19 of
    30 reads reported ``used:0/remaining:5000`` — a full budget — because
    GitHub serves the endpoint from replicas with inconsistent counters and a
    replica that never saw the window mints a fresh one at request time.
    Against such a read that filter matches nothing, so the "rate limited,
    back at HH:MM" copy silently lost its reset time roughly 60% of the time,
    in exactly the situation it exists for.

    ``gh_budget`` answers from an observed rejection first (ground truth),
    then from a sampled, earliest-reset, sticky snapshot. ``None`` on any
    failure: the reset time is a nicety, never a reason to fail harder.
    """
    observed = gh_budget.exhausted_until()
    if observed is not None:
        return observed
    budget = gh_budget.snapshot()
    if budget is None:
        return None
    return budget.exhausted_reset


def _fetch_failure(what: str, exc: Exception, *, graphql: bool = True) -> GitHubFetchError:
    """Translate a raw subprocess/JSON failure into a ``GitHubFetchError``
    carrying a human-readable cause (and rate-limit detail when applicable).

    ``graphql`` says which bucket the failed call spent. Only a GraphQL
    rejection is recorded as GraphQL exhaustion: the REST index goes through
    this funnel too, and a core-bucket (or secondary) rejection there used to
    stand every background board refresh down while the GraphQL budget was
    untouched (#255 review).
    """
    if isinstance(exc, FileNotFoundError):
        return GitHubFetchError(f"{what}: gh CLI not found")
    if isinstance(exc, subprocess.TimeoutExpired):
        return GitHubFetchError(f"{what}: gh timed out after {_GH_TIMEOUT}s")
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return GitHubFetchError(f"{what}: gh returned malformed JSON")
    stderr = (getattr(exc, "stderr", "") or "").strip()
    if _RATE_LIMIT_RE.search(stderr):
        reset_at = _rate_limit_reset()
        # A call that came back rate-limited is the only fully reliable
        # exhaustion signal there is — `gh api rate_limit` reports a full
        # budget on a dead one most of the time. This is the single funnel
        # where every such rejection is recognised, so it is where the budget
        # gate learns about it (#255) — for the bucket the gate guards.
        if graphql:
            gh_budget.note_rate_limited(reset_at)
        return GitHubFetchError(
            f"{what}: GitHub API rate limit exceeded",
            rate_limited=True,
            reset_at=reset_at,
        )
    detail = stderr.splitlines()[0] if stderr else str(exc)
    return GitHubFetchError(f"{what}: {detail}")


# Priority field value to enum mapping
_PRIORITY_MAP = {
    "Need": Priority.NEED,
    "Want": Priority.WANT,
    "Idea": Priority.IDEA,
}


def fetch_project_issues(project_number: int, owner: str) -> list[Issue]:
    """Fetch issues from a GitHub Project via gh CLI.

    The board comes from :mod:`agent_hq.gh_board`'s hand-written GraphQL query
    rather than ``gh project item-list``. Both return the same JSON shape, so
    the parsing below is unchanged, but the cost is not: ``item-list`` bills
    **203 GraphQL points** per board against the query's 2 per 100-item page
    (measured 2026-08-26). Board queries were 96% of a refresh cycle, which is
    why this swap — not the shared cache — is what takes #255 back under budget.

    Raises ``GitHubFetchError`` on ANY failure (non-zero exit, missing gh,
    timeout, malformed JSON, a 200 response carrying GraphQL ``errors``) so
    ``GitHubCache.get_issues`` can keep its previous-good value (#254). An
    empty list means GitHub genuinely returned zero items — never a failure.
    """
    try:
        items = gh_board.fetch_board_items(project_number, owner)
    except (
        # BoardQueryError carries `.stderr`, so a GraphQL-level rate-limit
        # body reaches _fetch_failure's rate-limit branch just like a
        # non-zero gh exit does.
        gh_board.BoardQueryError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        ValueError,  # includes json.JSONDecodeError
    ) as exc:
        raise _fetch_failure(f"project {owner}/{project_number}", exc) from exc

    issues = []
    for item in items:
        content = item.get("content", {})
        if content.get("type") != "Issue":
            continue

        priority_raw = item.get("priority")
        if isinstance(priority_raw, dict):
            priority_value = priority_raw.get("value")
        else:
            priority_value = priority_raw  # gh returns a plain string
        priority = _PRIORITY_MAP.get(priority_value, Priority.NO_PRI)

        raw_labels = item.get("labels", [])
        if raw_labels and isinstance(raw_labels[0], dict):
            labels = [lbl["name"] for lbl in raw_labels if "name" in lbl]
        else:
            labels = [str(lbl) for lbl in raw_labels]  # gh returns plain strings

        status_raw = item.get("status")
        if isinstance(status_raw, dict):
            status = status_raw.get("value")
        else:
            status = status_raw  # gh returns a plain string

        issues.append(
            Issue(
                number=content["number"],
                title=content.get("title", ""),
                repo=content.get("repository", ""),
                status=status,
                priority=priority,
                labels=labels,
                body=content.get("body", ""),
                item_id=item.get("id", ""),
                project_number=project_number,
                # Project numbers are owner-scoped (#81) — remember which
                # owner this issue's project was fetched under so field
                # writes can't route to a same-numbered project elsewhere.
                project_owner=owner,
            )
        )

    _enrich_dates(issues)
    return issues


# Two readers want the same repo's index on every refresh cycle: the date
# enrichment inside ``fetch_project_issues``, and ``get_closed_keys``.
# Debouncing on the index's own ON-DISK ``synced_at`` collapses them into one
# REST call — and, because that stamp is shared, collapses every agent-hq
# process too, which is the same cross-process dedupe #255 is after.
_INDEX_SYNC_MIN_INTERVAL = 60.0

_issue_index: gh_index.IssueIndex | None = None
_issue_index_lock = threading.Lock()


def _get_issue_index() -> gh_index.IssueIndex:
    """The process-wide index handle, built on first use.

    Deliberately lazy rather than a module-level instance: the constructor
    captures its path, and at import time the test-isolation fixture has not
    yet redirected ``_DEFAULT_INDEX_FILE``, so an eager singleton would pin
    the real user file for the whole suite.
    """
    global _issue_index
    with _issue_index_lock:
        if _issue_index is None:
            _issue_index = gh_index.IssueIndex()
        return _issue_index


def clear_issue_index_cache() -> None:
    """Drop the process-wide index handle (test seam)."""
    global _issue_index
    with _issue_index_lock:
        _issue_index = None


def _sync_issue_index(repo: str, *, force: bool = False) -> gh_index.IssueIndex:
    """Bring ``repo`` up to date in the shared index and return it.

    Raises ``GitHubFetchError`` on failure (#254), leaving whatever the index
    already holds untouched — callers decide whether to degrade or preserve.
    """
    index = _get_issue_index()
    if not force and _index_recently_synced(index, repo):
        return index
    # Single-flight, per repo. The debounce above is an unlocked read, so a
    # cold start let the per-repo threadpool in _enrich_dates, get_closed_keys
    # and every other agent-hq process each launch their own full seed — up to
    # 30 REST pages apiece. The shared stamp only ever collapsed the steady
    # state, which is not the path that costs calls (#255 review).
    with best_effort_file_lock(index.sync_lock_path(repo)):
        # A peer may have synced this repo while we queued; ``synced_at``
        # reloads the file on mtime change, so this sees their write.
        if not force and _index_recently_synced(index, repo):
            return index
        return _sync_issue_index_locked(repo, index)


def _index_recently_synced(index: gh_index.IssueIndex, repo: str) -> bool:
    last = index.synced_at(repo)
    return last is not None and time.time() - last < _INDEX_SYNC_MIN_INTERVAL


def _sync_issue_index_locked(repo: str, index: gh_index.IssueIndex) -> gh_index.IssueIndex:
    try:
        gh_index.sync_repo(repo, index)
    except (
        gh_index.IndexSyncError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        ValueError,  # includes json.JSONDecodeError
    ) as exc:
        raise _fetch_failure(f"issue index for {repo}", exc, graphql=False) from exc
    return index


def _safe_timestamps_for(repo: str) -> dict[int, dict[str, str | None]]:
    """Index timestamps for ``repo``; a failed sync serves what the index holds.

    The index IS the previous-good data. Returning ``{}`` on a failed sync
    threw away rows already on disk and nulled every date on that repo's
    board until the next successful sync (#255 review). The warn still
    lands, so the failure is visible; the sort just keeps working.
    """
    index = _get_issue_index()
    try:
        _sync_issue_index(repo)
    except GitHubFetchError as exc:
        eventlog.emit("warn", "github timestamp fetch failed", {"error": exc.message})
    return index.timestamps_for(repo)


def refresh_issue_timestamps(issues: list[Issue]) -> None:
    """Re-read ``created_at`` / ``updated_at`` from the index as it stands now,
    WITHOUT syncing (Codex round 4): ``_enrich_dates`` runs behind the sync
    debounce, so a strict forced load can still carry a timestamp older than
    the index the forced closed-keys sync just refreshed. Callers that need
    the freshest dates call this after that sync."""
    index = _get_issue_index()
    for repo in sorted({i.repo for i in issues if i.repo}):
        rows = index.timestamps_for(repo)
        for issue in issues:
            ts = rows.get(issue.number) if issue.repo == repo else None
            if not ts:
                continue
            if ts.get("created_at"):
                issue.created_at = ts["created_at"]
            if ts.get("updated_at"):
                issue.updated_at = ts["updated_at"]


def open_issue_rows(repo: str) -> dict[str, str | None] | None:
    """``{key: created_at}`` for every open issue the durable index holds for
    ``repo`` (#273), after the debounced sync. ``None`` when the index was
    never seeded for the repo OR the sync failed (review round 9: an
    auth-scoped 404 fails the sync AND makes every REST lookup read
    "missing", so a stale index served as checked would resolve the repo's
    findings) — the detector then treats the repo as unchecked, never as
    "every issue is on the board". The creation date is what lets the
    detector skip an issue newer than its board snapshot."""
    index = _get_issue_index()
    try:
        _sync_issue_index(repo)
    except GitHubFetchError as exc:
        eventlog.emit("warn", "github issue index sync failed", {"error": exc.message})
        return None
    if not index.is_seeded(repo):
        return None
    open_keys = index.open_keys_for(repo)
    created = index.timestamps_for(repo)
    return {
        key: (created.get(int(key.rsplit("#", 1)[1])) or {}).get("created_at")
        for key in open_keys
    }


def open_issue_keys(repo: str) -> set[str] | None:
    """The keys of :func:`open_issue_rows`."""
    rows = open_issue_rows(repo)
    return None if rows is None else set(rows)


def _enrich_dates(issues: list[Issue]) -> None:
    """Populate ``issue.created_at`` / ``issue.updated_at`` in place from the
    durable REST issue index.

    The project board JSON exposes no timestamps, so the dispatch date sort
    (#156/#101) needs a second source. That used to be a 500-row
    ``gh issue list`` per repo plus a targeted ``gh issue view`` for any board
    issue outside the window (#113). Both are gone: the index answers from a
    complete local table, refreshed by one incremental REST call per repo
    (#255). The backfill goes with them — a complete index has no window to
    fall outside of.

    Best-effort: a repo whose sync fails keeps whatever the index already
    holds, and a repo with no index yet simply leaves its issues' dates
    ``None``. Dates are a sort nicety, so a broken lookup must degrade the
    Created/Updated sort, never fail an issue fetch that otherwise succeeded.
    """
    repos = sorted({i.repo for i in issues if i.repo})
    if not repos:
        return
    with ThreadPoolExecutor(max_workers=max(len(repos), 1)) as pool:
        by_repo = dict(zip(repos, pool.map(_safe_timestamps_for, repos)))
    for issue in issues:
        ts = by_repo.get(issue.repo, {}).get(issue.number)
        if not ts:
            continue
        # Only overwrite with a present value — a lookup that returns one
        # timestamp but not the other (or neither) must never null out a date
        # the caller already populated (presence-of-value guard, #156 review).
        if ts.get("created_at"):
            issue.created_at = ts["created_at"]
        if ts.get("updated_at"):
            issue.updated_at = ts["updated_at"]


@dataclass
class FieldInfo:
    """Metadata for a single-select project field."""

    field_id: str
    project_node_id: str
    options: dict[str, str] = field(default_factory=dict)  # option name -> option ID


def _fetch_project_node_id(project_number: int, owner: str) -> str | None:
    """Get the GraphQL node ID for a project (needed by item-edit)."""
    try:
        result = gh_meter.run_gh(
            ["gh", "project", "view", str(project_number), "--owner", owner, "--format", "json"],
            kind="project-view",
        )
        return json.loads(result.stdout).get("id")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
    ):
        return None


def fetch_field_info(project_number: int, owner: str, field_name: str) -> FieldInfo | None:
    """Fetch metadata for a single-select field by name.

    Returns FieldInfo with field_id, project_node_id, and option name->ID
    mapping, or ``None`` when the project genuinely has no such field.
    Raises ``GitHubFetchError`` when the lookup itself failed (#254) —
    ``GitHubCache.get_field_info`` degrades that to ``None`` after warning,
    so a rate limit doesn't get mistaken for a missing field.
    """
    try:
        result = gh_meter.run_gh(
            [
                "gh",
                "project",
                "field-list",
                str(project_number),
                "--owner",
                owner,
                "--format",
                "json",
            ],
            kind="project-field-list",
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        raise _fetch_failure(f"field-list {owner}/{project_number}", exc) from exc

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _fetch_failure(f"field-list {owner}/{project_number}", exc) from exc

    fld_id = None
    options: dict[str, str] = {}
    for fld in data.get("fields", []):
        if fld.get("name") == field_name:
            fld_id = fld.get("id")
            for opt in fld.get("options", []):
                name = opt.get("name")
                opt_id = opt.get("id")
                if name and opt_id:
                    options[name] = opt_id
            break

    if not fld_id:
        return None

    project_node_id = _fetch_project_node_id(project_number, owner)
    if not project_node_id:
        return None

    return FieldInfo(field_id=fld_id, project_node_id=project_node_id, options=options)


def fetch_issue_summary(repo: str, number: int) -> dict | None:
    """``{"title", "state"}`` for one issue over REST (0 GraphQL points) —
    the off-board card needs a title the board cache never fetched (#273).
    ``None`` on any failure; the caller reports the finding without it. A
    404 is a verdict, not a failure: ``state == "missing"`` (transferred or
    deleted — the index keeps such a row open until the daily reseed)."""
    try:
        result = gh_meter.run_gh(
            ["gh", "api", f"repos/{repo}/issues/{number}"], kind="issue-summary"
        )
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        if "HTTP 404" in str(exc.stderr or ""):  # gh's own wording, not any 404 substring
            return {"title": "", "state": "missing"}
        return None
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        ValueError,  # includes json.JSONDecodeError
    ):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title") or ""),
        "state": str(data.get("state") or ""),
        # The issues endpoint serves PRs too; only PRs carry this key (#414).
        "is_pr": "pull_request" in data,
    }


_LABEL_PAGE = 100
_LABEL_MAX_PAGES = 10


def fetch_label_names(repo: str) -> set[str] | None:
    """Every label name in ``repo`` over REST (#414), or ``None`` on any
    failure. Pages by hand: ``gh api --paginate`` prints one JSON array per
    page, which is not one JSON document."""
    names: set[str] = set()
    for page in range(1, _LABEL_MAX_PAGES + 1):
        try:
            result = gh_meter.run_gh(
                ["gh", "api", f"repos/{repo}/labels?per_page={_LABEL_PAGE}&page={page}"],
                kind="label-list",
            )
            batch = json.loads(result.stdout)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            ValueError,
        ):
            return None
        if not isinstance(batch, list):
            return None  # GitHub's error envelope
        names.update(str(row["name"]) for row in batch if isinstance(row, dict) and row.get("name"))
        if len(batch) < _LABEL_PAGE:
            return names
    return None  # past the cap: can't say a label is missing


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str:
    """Create a GitHub issue and return its URL. Raises GHError on failure
    (``GHTimeoutError`` when it timed out and may still have been filed)."""
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels or []:
        cmd.extend(["--label", label])
    result = _run_mutation(cmd, kind="issue-create", what="issue create")
    match = _ISSUE_URL_RE.search(result.stdout)
    if not match:
        raise GHError(f"issue create returned no URL: {result.stdout!r}")
    return match.group(0)


def add_to_project(project_number: int, owner: str, issue_url: str) -> str:
    """Add an issue to a GitHub Project, return the project item id. Raises
    GHError on failure (``GHTimeoutError`` when it timed out)."""
    cmd = [
        "gh",
        "project",
        "item-add",
        str(project_number),
        "--owner",
        owner,
        "--url",
        issue_url,
        "--format",
        "json",
    ]
    result = _run_mutation(cmd, kind="project-item-add", what="project-add")
    try:
        item_id = json.loads(result.stdout).get("id")
    except (json.JSONDecodeError, ValueError) as e:
        raise GHError(f"project-add returned non-JSON: {result.stdout!r}") from e
    if not item_id:
        raise GHError(f"project-add response missing item id: {result.stdout!r}")
    return item_id


def fetch_recent_issues_for_tone(repo: str, limit: int = 10) -> list[dict]:
    """Fetch recent issue title/body pairs from a repo for tone-matching.

    Returns [] on any failure (best-effort).
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--limit",
        str(limit),
        "--state",
        "all",
        # Only the owner's own issues seed the drafting prompt: anyone can
        # open issues in a public repo (#263 F8).
        "--author",
        "@me",
        "--json",
        "title,body",
    ]
    try:
        result = gh_meter.run_gh(cmd, kind="issue-list-tone")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [{"title": e.get("title", ""), "body": e.get("body", "")} for e in data]


def fetch_idea_inbox_issues(repo: str, limit: int = 10) -> list[dict]:
    """Fetch recent issues with the `idea-inbox` label. Best-effort, [] on failure."""
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        "idea-inbox",
        "--limit",
        str(limit),
        "--state",
        "all",
        "--json",
        "number,title,url,labels,createdAt,state",
    ]
    try:
        result = gh_meter.run_gh(cmd, kind="issue-list-idea")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def transfer_issue(number: int, src_repo: str, dst_repo: str) -> str:
    """Transfer an issue to a different repo, return the new URL. Raises on failure."""
    cmd = ["gh", "issue", "transfer", str(number), dst_repo, "--repo", src_repo]
    try:
        result = gh_meter.run_gh(cmd, kind="issue-transfer")
    except FileNotFoundError as e:
        raise GHError("gh CLI not found") from e
    except subprocess.CalledProcessError as e:
        raise GHError(f"issue transfer failed: {e.stderr.strip() or e}") from e
    except subprocess.TimeoutExpired as e:
        raise GHError(f"issue transfer timed out after {_GH_TIMEOUT}s") from e
    match = _ISSUE_URL_RE.search(result.stdout)
    if not match:
        raise GHError(f"issue transfer returned no URL: {result.stdout!r}")
    return match.group(0)


def close_issue_not_planned(repo: str, number: int) -> bool:
    """Close a GitHub issue with the ``not planned`` reason. Best-effort.

    Returns True on success, False on any failure — the bool contract mirrors
    ``GitHubCache.set_status`` so dispatch callers can revert an optimistic UI
    update without distinguishing failure modes.
    """
    cmd = [
        "gh",
        "issue",
        "close",
        str(number),
        "--repo",
        repo,
        "--reason",
        "not planned",
    ]
    try:
        gh_meter.run_gh(cmd, kind="issue-close")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def comment_issue(repo: str, number: int, body: str) -> bool:
    """Post a comment on an issue. Best-effort bool like ``close_issue_not_planned``
    — the hygiene sweep's accept path (#249) stops at the first failed step."""
    cmd = ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body]
    try:
        gh_meter.run_gh(cmd, kind="issue-comment")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def _run_mutation(cmd: list[str], *, kind: str, what: str):
    """Run a mutating gh call, mapping failures to ``GHError`` and a timeout
    to ``GHTimeoutError`` (the change may still have landed)."""
    try:
        return gh_meter.run_gh(cmd, kind=kind)
    except FileNotFoundError as e:
        raise GHError("gh CLI not found") from e
    except subprocess.CalledProcessError as e:
        raise GHError(f"{what} failed: {(e.stderr or '').strip() or e}") from e
    except subprocess.TimeoutExpired as e:
        raise GHTimeoutError(f"{what} timed out after {_GH_TIMEOUT}s") from e


def post_issue_comment(repo: str, number: int, body: str) -> str:
    """Post a comment and return its URL (#414; falls back to the issue URL
    when gh prints none). Raises ``GHError`` / ``GHTimeoutError``. The bool
    :func:`comment_issue` stays for the hygiene accept path."""
    cmd = ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body]
    result = _run_mutation(cmd, kind="issue-comment", what="issue comment")
    match = _COMMENT_URL_RE.search(getattr(result, "stdout", "") or "")
    return match.group(0) if match else f"https://github.com/{repo}/issues/{number}"


CLOSE_REASONS = {"completed": "completed", "not_planned": "not planned"}


def close_issue(
    repo: str,
    number: int,
    *,
    reason: str,
    comment: str,
    duplicate_of_url: str | None = None,
) -> None:
    """Close an issue with a closing comment (#414). ``reason`` is
    ``completed``, ``not_planned`` or ``duplicate`` (which needs
    ``duplicate_of_url`` and is recorded natively via ``--duplicate-of``; gh
    rejects another ``--reason`` beside it). Raises ``ValueError`` on a bad
    combination, ``GHError`` / ``GHTimeoutError`` on failure."""
    cmd = ["gh", "issue", "close", str(number), "--repo", repo, "--comment", comment]
    if reason == "duplicate":
        if not duplicate_of_url:
            raise ValueError("a duplicate close needs duplicate_of_url")
        cmd += ["--duplicate-of", duplicate_of_url]
    elif reason in CLOSE_REASONS:
        if duplicate_of_url:
            raise ValueError("duplicate_of_url only goes with reason=duplicate")
        cmd += ["--reason", CLOSE_REASONS[reason]]
    else:
        raise ValueError(f"unknown close reason: {reason}")
    _run_mutation(cmd, kind="issue-close", what="issue close")


def fetch_closed_issue_keys(repo: str, force: bool = False) -> set[str]:
    """Return ``{'owner/repo#number'}`` for closed issues in ``repo``.

    Served from the durable REST issue index (#255) rather than a 500-row
    ``gh issue list --state closed``. The index is cumulative, which retires
    that call's documented caveat: a very old closed issue still pinned to the
    project board used to fall outside the 500 cap and briefly reappear in the
    backlog. It cannot now — once the index has seen an issue close, it stays
    closed until a full reseed says otherwise.

    Raises ``GitHubFetchError`` on failure (#254) — an empty set means the
    repo genuinely has no closed issues, and mistaking a failure for that can
    only make already-closed issues *reappear* in the backlog.
    """
    return _sync_issue_index(repo, force=force).closed_keys_for(repo)


def set_field_value(
    project_node_id: str,
    item_id: str,
    field_id: str,
    option_id: str | None = None,
) -> bool:
    """Set or clear a single-select field value in a GitHub Project. Best-effort."""
    cmd = [
        "gh",
        "project",
        "item-edit",
        "--id",
        item_id,
        "--project-id",
        project_node_id,
        "--field-id",
        field_id,
    ]
    if option_id:
        cmd.extend(["--single-select-option-id", option_id])
    else:
        cmd.append("--clear")
    try:
        gh_meter.run_gh(cmd, kind="project-item-edit")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Worktree-detection cache (#226): the dispatch view calls
# detect_active_worktrees on every container/list render — including every
# debounced search keystroke — which was N uncached, timeout-less git
# spawns each time. Worktrees change on dispatch/cleanup, so a short TTL
# (plus explicit invalidation right after a dispatch creates one) keeps the
# badge honest while collapsing the spawns to one burst per TTL.
_WORKTREE_TTL = 15.0
_GIT_WORKTREE_TIMEOUT = 5
_worktree_cache: dict[frozenset[str], tuple[float, set[str]]] = {}
_worktree_lock = threading.Lock()


def invalidate_worktree_cache() -> None:
    """Drop cached worktree detections — called after a dispatch creates a
    worktree so its 'active' badge shows on the next render, not after TTL."""
    with _worktree_lock:
        _worktree_cache.clear()


def detect_active_worktrees(repos: dict[str, RepoConfig]) -> set[str]:
    """Detect which issues have active worktrees.

    Returns set of issue keys like "owner/repo#42".
    Parses branch names matching feature/<numbers>-... pattern.
    TTL-cached per repo set; each git call is bounded by
    ``_GIT_WORKTREE_TIMEOUT`` so a hung git (network mount, dead FS)
    degrades to "no badge" instead of wedging the request.
    """
    cache_key = frozenset(f"{name}:{cfg.local_path}" for name, cfg in repos.items())
    now = time.monotonic()
    with _worktree_lock:
        entry = _worktree_cache.get(cache_key)
        if entry is not None and now - entry[0] < _WORKTREE_TTL:
            return set(entry[1])

    active = set()
    for owner_name, repo_config in repos.items():
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_config.local_path), "worktree", "list"],
                capture_output=True,
                text=True,
                check=True,
                timeout=_GIT_WORKTREE_TIMEOUT,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue

        for line in result.stdout.strip().splitlines():
            match = re.search(r"\[(.+)\]", line)
            if not match:
                continue
            branch = match.group(1)
            num_match = re.match(r"feature/([\d]+(?:-[\d]+)*)-", branch)
            if num_match:
                nums_str = num_match.group(1)
                for num in nums_str.split("-"):
                    if num.isdigit():
                        active.add(f"{owner_name}#{num}")

    with _worktree_lock:
        # Timestamp at STORE time: a scan slowed by per-repo git timeouts
        # must not store an already-expired entry (review fix, #226).
        _worktree_cache[cache_key] = (time.monotonic(), active)
    return set(active)


def _issue_to_dict(issue: Issue) -> dict:
    """Serialize an Issue to a JSON-safe dict."""
    return {
        "number": issue.number,
        "title": issue.title,
        "repo": issue.repo,
        "status": issue.status,
        "priority": issue.priority.value,
        "labels": issue.labels,
        "body": issue.body,
        "item_id": issue.item_id,
        "project_number": issue.project_number,
        "project_owner": issue.project_owner,
        "updated_at": issue.updated_at,
        "created_at": issue.created_at,
    }


def _issue_from_dict(d: dict) -> Issue:
    """Deserialize an Issue from a dict."""
    pri_value = d.get("priority", "")
    priority = _PRIORITY_MAP.get(pri_value, Priority.NO_PRI)
    return Issue(
        number=d["number"],
        title=d["title"],
        repo=d["repo"],
        status=d.get("status"),
        priority=priority,
        labels=d.get("labels", []),
        body=d.get("body", ""),
        item_id=d.get("item_id", ""),
        project_number=d.get("project_number", 0),
        project_owner=d.get("project_owner", ""),
        updated_at=d.get("updated_at"),
        created_at=d.get("created_at"),
    )


@dataclass(frozen=True)
class FetchStatus:
    """Whether the cache's data is current, or a preserved last-good copy
    being served because GitHub could not be reached (#254).

    ``ok`` False means at least one project/repo refresh failed since its
    last success. ``stale_since`` is the oldest surviving cache timestamp
    among the failing keys, so the UI can say how old the data it is showing
    actually is; ``None`` means nothing was cached to fall back on.
    """

    ok: bool = True
    message: str | None = None
    rate_limited: bool = False
    reset_at: float | None = None
    stale_since: float | None = None
    # A refresh we declined to make while GraphQL headroom was low (#255).
    # Distinct from rate_limited on purpose: nothing failed, so telling the
    # user "rate limit exceeded" would be a lie about what happened.
    budget_skipped: bool = False


class GitHubCache:
    """Disk-backed cache for GitHub project issues with TTL."""

    def __init__(
        self,
        ttl: int = 300,
        cache_file: Path | None = None,
        graphql_floor: int | None = None,
    ):
        self._ttl = ttl
        self._cache_file = cache_file or _CACHE_FILE
        self._graphql_floor = (
            gh_budget.DEFAULT_GRAPHQL_FLOOR if graphql_floor is None else graphql_floor
        )
        # Background refreshes declined for low headroom, keyed like _failures.
        # Kept apart from them because a skip is not a failure — see FetchStatus.
        self._budget_skips: dict[str, float] = {}
        self._cache: dict[str, tuple[float, list[Issue]]] = {}
        self._field_infos: dict[str, FieldInfo] = {}  # keyed by "owner/number/field_name"
        # Closed-issue keys per repo, in-memory only (not disk-persisted —
        # keeps _save_to_disk untouched; cheap to refetch once per process).
        self._closed: dict[str, tuple[float, set[str]]] = {}
        # Serializes mutations to the three dicts and the disk write.
        # _load_issues fans out get_issues across projects via asyncio.gather +
        # threadpool, so without this lock _save_to_disk's iteration can race
        # with concurrent _cache writes and concurrent write_text() calls can
        # truncate each other.
        self._lock = threading.RLock()
        # Live fetch failures keyed like the data they shadow
        # ("<owner>/<number>" for projects, "closed:<repo>" for closed keys),
        # each stamped when it was recorded. An entry means "this key's data is
        # a preserved last-good copy".
        self._failures: dict[str, tuple[float, GitHubFetchError]] = {}
        # mtime of the file as of our last read, so a peer's write is noticed.
        self._disk_mtime: float | None = None
        self._load_from_disk()

    # --- cross-process coordination (#255) --------------------------------

    def _write_lock_path(self) -> Path:
        return self._cache_file.with_suffix(".json.lock")

    def _fetch_lock_path(self, cache_key: str) -> Path:
        """One lock per project, NOT one for the cache.

        A single global fetch lock would serialize the six concurrent project
        fetches ``_load_issues`` runs via ``asyncio.gather``, turning cold-load
        latency from max-of-N back into sum-of-N and regressing #90. Per-key
        locks keep that parallelism while still collapsing N processes racing
        for the *same* project into one fetch.
        """
        slug = cache_key.replace("/", "-")
        return self._cache_file.parent / "locks" / f"project-{slug}.lock"

    def _reload_if_changed(self) -> None:
        """Re-read the cache file when another process wrote it.

        This is what makes ``github.json`` authoritative rather than a
        warm-start seed. Without it ``_load_from_disk`` ran exactly once, in
        ``__init__``, so every process kept its own TTL clock forever and the
        nine live processes each refreshed independently (#255).
        """
        try:
            mtime = self._cache_file.stat().st_mtime
        except OSError:
            return
        if mtime == self._disk_mtime:
            return
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Merge the disk file into memory, newest timestamp wins.

        A merge rather than a replace: our own just-fetched entry must not be
        rolled back by an older file, and a peer's fresher entry must win over
        ours. Malformed entries are skipped individually — one bad row used to
        abort the whole load.
        """
        try:
            # stat BEFORE read, never after. A peer's os.replace landing
            # between the two would otherwise pair the new mtime with the old
            # contents, and `_reload_if_changed` compares against exactly this
            # mtime — so that version would never be re-read (#255 review).
            # This order can only err towards a redundant reload, which is
            # harmless; the other order loses the write permanently.
            mtime = self._cache_file.stat().st_mtime
            raw = self._cache_file.read_text()
        except OSError:
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            for key, entry in data.items():
                if key == "_field_infos":
                    if not isinstance(entry, dict):
                        continue
                    for k, v in entry.items():
                        try:
                            self._field_infos[k] = FieldInfo(
                                field_id=v["field_id"],
                                project_node_id=v["project_node_id"],
                                options=v.get("options", {}),
                            )
                        except (KeyError, TypeError):
                            continue
                    continue
                if key == "_status_fields":
                    continue  # Ignore legacy format
                try:
                    timestamp = entry["timestamp"]
                    issues = [_issue_from_dict(d) for d in entry["issues"]]
                except (KeyError, TypeError):
                    continue
                mine = self._cache.get(key)
                if mine is None or timestamp > mine[0]:
                    self._cache[key] = (timestamp, issues)
            self._disk_mtime = mtime

    def _save_to_disk(self) -> None:
        """Persist the cache atomically, merging with whatever a peer wrote.

        Callers already hold ``self._lock``; the file lock is taken inside it,
        so a thread holding the file lock always already holds the RLock and
        the two can't invert. The read-modify-write is what keeps a whole-file
        write from dropping a peer's *other* project, and ``os.replace`` is
        what keeps a concurrent reader from seeing a half-written file.
        """
        with best_effort_file_lock(self._write_lock_path()):
            self._load_from_disk()
            data: dict = {}
            for key, (timestamp, issues) in self._cache.items():
                data[key] = {
                    "timestamp": timestamp,
                    "issues": [_issue_to_dict(i) for i in issues],
                }
            if self._field_infos:
                data["_field_infos"] = {
                    k: {
                        "field_id": v.field_id,
                        "project_node_id": v.project_node_id,
                        "options": v.options,
                    }
                    for k, v in self._field_infos.items()
                }
            tmp = self._cache_file.with_suffix(".json.tmp")
            try:
                self._cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data))
                os.replace(tmp, self._cache_file)
                self._disk_mtime = self._cache_file.stat().st_mtime
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _record_failure(self, key: str, exc: GitHubFetchError) -> None:
        """Remember a failed refresh and warn the unified logger.

        The message is a stable literal so eventlog's fingerprint dedupe
        collapses a six-project storm into one warn per interval; the detail
        rides in ``json_extra`` (#254).
        """
        with self._lock:
            self._failures[key] = (time.time(), exc)
        eventlog.emit(
            "warn",
            "github fetch failed, serving cached data",
            {
                "target": key,
                "error": exc.message,
                "rate_limited": exc.rate_limited,
                "reset_at": exc.reset_at,
            },
        )

    def _record_budget_skip(self, key: str) -> None:
        """Remember a refresh declined for low headroom, and warn once.

        A stable literal, distinct from the fetch-failure one, so eventlog's
        fingerprint dedupe collapses a six-project sweep into a single warn
        and the two situations stay separable in the Errors tab (#255).
        """
        with self._lock:
            self._budget_skips[key] = time.time()
        eventlog.emit(
            "warn",
            "github refresh skipped, budget low",
            {"target": key, "floor": self._graphql_floor},
        )

    def _clear_budget_skip(self, key: str) -> None:
        with self._lock:
            self._budget_skips.pop(key, None)

    def _live_budget_skips(self) -> dict[str, float]:
        """Unexpired skips, pruning what has aged out — mirrors
        ``_live_failures``, including its expiry rule."""
        cutoff = time.time() - self._failure_ttl()
        with self._lock:
            for key in [k for k, ts in self._budget_skips.items() if ts < cutoff]:
                del self._budget_skips[key]
            return dict(self._budget_skips)

    def _clear_failure(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _failure_ttl(self) -> float:
        """How long a recorded failure stays believable.

        A key that is still being fetched re-records its failure every TTL, so
        anything older than a couple of refresh cycles belongs to a key that
        left the fetch set — a repo whose last board issue got closed, or a
        project dropped from projects.yaml. Without expiry that entry would pin
        the "GitHub unreachable" banner on over perfectly fresh data.
        """
        return max(self._ttl * 2, 600.0)

    def _live_failures(self) -> dict[str, GitHubFetchError]:
        """Unexpired failures, pruning what has aged out. Caller holds no lock."""
        cutoff = time.time() - self._failure_ttl()
        with self._lock:
            for key in [k for k, (ts, _) in self._failures.items() if ts < cutoff]:
                del self._failures[key]
            return {k: exc for k, (_, exc) in self._failures.items()}

    def failed_keys(self) -> set[str]:
        """Cache keys with an unexpired fetch failure — ``owner/number`` for a
        board, ``closed:owner/repo`` for closed keys. A caller judging ONE
        forced refetch checks the keys it touched (round 4): ``fetch_status``
        is process-global and would report a lingering failure for a project
        or repo the refetch never touched."""
        return set(self._live_failures())

    def fetch_status(self) -> FetchStatus:
        """Summarize whether what this cache is serving is current (#254).

        A rate-limit failure wins the ``message`` slot when several keys
        failed for different reasons — it is the one with an actionable
        wait time attached.

        ``stale_since`` prefers the failing keys' own cache stamps, then
        falls back to the oldest cached project entry: a closed-keys-only
        failure has no entry under its own key, and reporting "nothing
        cached" while the backlog renders would be plainly wrong.
        """
        failures = self._live_failures()
        if not failures:
            # A real failure outranks a skip: something breaking is more
            # urgent than something being deliberately conserved.
            skips = self._live_budget_skips()
            if not skips:
                return FetchStatus()
            with self._lock:
                stamps = [self._cache[k][0] for k in skips if k in self._cache]
            # The NON-blocking accessor, deliberately: the TUI reaches this
            # from its main thread (`_apply_refresh` via call_from_thread),
            # and `snapshot()` can sit in _SAMPLES sequential subprocesses
            # once the memo expires — which is exactly the state an observed
            # rejection leaves it in, since that short-circuits the meter.
            # The reset time is a toast nicety; answering "unknown" for a
            # beat beats freezing the UI (#255 review).
            budget = gh_budget.snapshot_cached()
            return FetchStatus(
                ok=False,
                message="GraphQL headroom low — background refresh paused",
                budget_skipped=True,
                reset_at=budget.graphql_reset if budget else None,
                stale_since=min(stamps) if stamps else None,
            )
        with self._lock:
            stamps = [self._cache[k][0] for k in failures if k in self._cache]
            stamps += [
                self._closed[r][0]
                for k in failures
                if k.startswith("closed:") and (r := k.removeprefix("closed:")) in self._closed
            ]
            if not stamps:
                stamps = [ts for ts, _ in self._cache.values()]
        rate_limited = [e for e in failures.values() if e.rate_limited]
        chosen = rate_limited[0] if rate_limited else next(iter(failures.values()))
        resets = [e.reset_at for e in failures.values() if e.reset_at]
        return FetchStatus(
            ok=False,
            message=chosen.message,
            rate_limited=bool(rate_limited),
            reset_at=max(resets) if resets else None,
            stale_since=min(stamps) if stamps else None,
        )

    def get_issues(
        self,
        project_number: int,
        owner: str,
        force: bool = False,
    ) -> list[Issue]:
        """Get issues, using cache if within TTL.

        If the underlying gh fetch fails for any reason — timeout (#90),
        rate limit, auth expiry, network, GitHub 5xx (#254) — fall back to
        the previously cached value and leave its **original timestamp**
        alone rather than writing ``[]`` with a fresh stamp. A fresh stamp
        would both destroy the last-good data and suppress the next refresh
        for a full TTL. Only a successful fetch (including a genuine
        zero-item one) updates the cache.
        """
        cache_key = f"{owner}/{project_number}"

        if not force:
            fresh = self._fresh_entry(cache_key)
            if fresh is not None:
                return fresh
            # The TTL has lapsed, so this is a background refresh. Spend
            # budget on it only if there is budget to spend — but only when
            # there is something to fall back to. A cold process has nothing
            # to degrade to, and an empty backlog forever is worse than one
            # more call. Writes and explicit refreshes are never gated.
            if not gh_budget.allow_background_refresh(self._graphql_floor):
                with self._lock:
                    cached = self._cache.get(cache_key)
                if cached is not None:
                    self._record_budget_skip(cache_key)
                    return cached[1]

        # Single-flight: whoever gets the lock fetches, everyone else waits
        # and then finds the answer already cached. Per key, so distinct
        # projects still fetch in parallel (see _fetch_lock_path).
        with best_effort_file_lock(self._fetch_lock_path(cache_key)):
            if not force:
                # A peer may have fetched this exact project while we queued.
                fresh = self._fresh_entry(cache_key)
                if fresh is not None:
                    return fresh
                # ...or been rejected for rate limiting while we queued. The
                # gate above ran before the wait; a failed fetch does not
                # freshen the entry, so without this every waiter fetched
                # into a budget already known to be dead. In-memory read,
                # never a probe (#255 review).
                if gh_budget.is_exhausted():
                    with self._lock:
                        cached = self._cache.get(cache_key)
                    if cached is not None:
                        self._record_budget_skip(cache_key)
                        return cached[1]
            try:
                issues = fetch_project_issues(project_number, owner)
            except GitHubFetchError as exc:
                self._record_failure(cache_key, exc)
                with self._lock:
                    cached = self._cache.get(cache_key)
                return cached[1] if cached else []
            self._clear_failure(cache_key)
            self._clear_budget_skip(cache_key)
            with self._lock:
                self._cache[cache_key] = (time.time(), issues)
                self._save_to_disk()
            return issues

    def _fresh_entry(self, cache_key: str) -> list[Issue] | None:
        """TTL-fresh issues for ``cache_key``, reloading a peer's write first.

        ``None`` means "not fresh, go fetch". Serving fresh data also clears
        any failure recorded against the key (e.g. a forced refresh that
        failed seconds after a good one): the banner means "this data is
        stale", not "a fetch failed once", and the eventlog warn already
        records the failure itself.
        """
        with self._lock:
            self._reload_if_changed()
            entry = self._cache.get(cache_key)
        if entry is not None and time.time() - entry[0] < self._ttl:
            self._clear_failure(cache_key)
            # Fresh data is fresh no matter who fetched it. A peer's
            # successful refresh used to leave our own budget skip standing,
            # so we served their current board under a "background refresh
            # paused" banner until the skip aged out (#255 review).
            self._clear_budget_skip(cache_key)
            return entry[1]
        return None

    def cached_at(self, project_number: int, owner: str) -> float | None:
        """When the cached entry for a board was fetched (#273): the off-board
        detector treats the board as a snapshot taken then. Never fetches."""
        with self._lock:
            self._reload_if_changed()
            entry = self._cache.get(f"{owner}/{project_number}")
        return None if entry is None else float(entry[0])

    def get_issues_cached(
        self,
        project_number: int,
        owner: str,
    ) -> tuple[list[Issue] | None, bool]:
        """Return ``(issues, fresh)`` for a project without ever fetching.

        ``issues`` is ``None`` when the project was never fetched; ``fresh``
        is False once the TTL has lapsed. The stale-while-revalidate path
        (#226) serves this instantly while a background refresh runs.

        Reloads a peer's write first (#255), so a process that has been idle
        does not report its own stale copy as needing a refresh when another
        process already did the work.
        """
        cache_key = f"{owner}/{project_number}"
        with self._lock:
            self._reload_if_changed()
            entry = self._cache.get(cache_key)
        if entry is None:
            return None, False
        timestamp, issues = entry
        fresh = time.time() - timestamp < self._ttl
        if fresh:
            # Same rule as _fresh_entry: fresh data is fresh no matter who
            # fetched it, and the SWR reader never reaches get_issues for a
            # fresh entry — so a peer's explicit refresh left our "budget
            # low" banner standing until the skip aged out (#255 review).
            self._clear_failure(cache_key)
            self._clear_budget_skip(cache_key)
        return issues, fresh

    def has_closed_entries(self, repos: Iterable[str]) -> bool:
        """True when at least one of ``repos`` has a closed-keys entry (any
        age). False means the in-memory closed cache is cold — e.g. right
        after a service restart, where issues load from disk but closed keys
        (deliberately not persisted) are gone; the SWR path must then fetch
        them blocking so closed/Won't-Do issues never render (#226)."""
        with self._lock:
            return any(r in self._closed for r in repos)

    def get_closed_keys_cached(self, repos: Iterable[str]) -> tuple[set[str], bool]:
        """Return ``(closed keys union, all_fresh)`` without ever fetching.

        A missing or expired repo contributes whatever it has (or nothing)
        and flips ``all_fresh`` False.
        """
        now = time.time()
        result: set[str] = set()
        all_fresh = True
        with self._lock:
            for repo in set(repos):
                entry = self._closed.get(repo)
                if entry is None:
                    all_fresh = False
                    continue
                timestamp, keys = entry
                result |= keys
                if now - timestamp >= self._ttl:
                    all_fresh = False
        return result, all_fresh

    def get_closed_keys(self, repos: Iterable[str], force: bool = False) -> set[str]:
        """Return the union of closed-issue keys for ``repos``, TTL-cached.

        Stale/missing repos are fetched in parallel. Used by both the TUI and
        web backlog to hide closed issues (the project ``item-list`` JSON does
        not expose issue state).

        A failed repo keeps its previous keys at their original timestamp
        (#254) — dropping them would make already-closed issues reappear in
        the backlog while GitHub is unreachable.
        """
        now = time.time()
        unique = sorted(set(repos))
        with self._lock:
            stale = []
            for r in unique:
                entry = self._closed.get(r)
                if not force and entry is not None and now - entry[0] < self._ttl:
                    # Serving TTL-fresh keys: a recorded failure (e.g. a
                    # forced refresh that failed right after a good one) no
                    # longer describes what is being served — mirror the
                    # get_issues rule.
                    self._failures.pop(f"closed:{r}", None)
                    continue
                failure = self._failures.get(f"closed:{r}")
                if not force and failure is not None and now - failure[0] < self._ttl:
                    # Retry cooldown. It lives on the failure record — NOT on
                    # a re-stamped data entry, which would corrupt the age
                    # fetch_status() reports — so an expired entry behind a
                    # recent failure is served as-is instead of re-running a
                    # 10s-timeout `gh issue list` per request against an
                    # already-failing API.
                    continue
                stale.append(r)
        if stale:

            def _fetch(repo: str):
                try:
                    # An explicit refresh bypasses the index sync debounce —
                    # the user is present and deliberately spending budget.
                    return repo, fetch_closed_issue_keys(repo, force=force), None
                except GitHubFetchError as exc:
                    return repo, None, exc

            with ThreadPoolExecutor(max_workers=max(len(stale), 1)) as pool:
                results = list(pool.map(_fetch, stale))
            for repo, keys, exc in results:
                if exc is not None:
                    self._record_failure(f"closed:{repo}", exc)
                    # Previous keys stay AT THEIR ORIGINAL STAMP — the retry
                    # cooldown rides the failure record (see the staleness
                    # pass above), so re-stamping here would only make
                    # fetch_status() report hours-old keys as fresh. A cold
                    # repo still gets an empty entry: closed keys never
                    # persist to disk, and a missing entry sends every SWR
                    # render down the blocking closed_cold path (#226).
                    # ...and that empty entry is seeded from the durable
                    # index rather than left empty: the index holds every
                    # close it has ever seen, so a restart while GitHub is
                    # unreachable must not make them all reappear (#255
                    # review). Still a failure — the banner still shows.
                    durable = _get_issue_index().closed_keys_for(repo)
                    with self._lock:
                        self._closed.setdefault(repo, (now, durable))
                    continue
                self._clear_failure(f"closed:{repo}")
                with self._lock:
                    self._closed[repo] = (now, keys)
        with self._lock:
            result: set[str] = set()
            for repo in unique:
                entry = self._closed.get(repo)
                if entry:
                    result |= entry[1]
            return result

    def mark_closed(self, repo: str, number: int) -> None:
        """Record an issue as closed so it stays hidden before the TTL lapses.

        Called after a successful close so the backlog does not flash the
        just-closed issue back until ``get_closed_keys`` next refetches.

        Also written through to the durable index (#255), so the close
        survives a restart and is immediately visible to every other agent-hq
        process — the in-memory set alone is per-process and never persisted.
        Index failure is swallowed: the close already succeeded on GitHub, and
        the in-memory entry above still hides the issue in this process.
        """
        with self._lock:
            timestamp, keys = self._closed.get(repo, (time.time(), set()))
            self._closed[repo] = (timestamp, keys | {f"{repo}#{number}"})
        try:
            _get_issue_index().mark_closed(repo, number)
        except OSError:
            pass

    def get_field_info(self, project_number: int, owner: str, field_name: str) -> FieldInfo | None:
        """Get field metadata for a single-select field. Cached permanently."""
        cache_key = f"{owner}/{project_number}/{field_name}"
        with self._lock:
            cached = self._field_infos.get(cache_key)
        if cached is not None:
            return cached

        try:
            result = fetch_field_info(project_number, owner, field_name)
        except GitHubFetchError as exc:
            # Field metadata backs the priority/status writers, not the
            # backlog itself — degrade to "unavailable" (callers already
            # treat None as a failed write) rather than surfacing it as
            # stale-backlog state (#254).
            eventlog.emit("warn", "github field lookup failed", {"error": exc.message})
            return None
        if result:
            with self._lock:
                self._field_infos[cache_key] = result
                self._save_to_disk()
        return result

    def set_status(self, issue: Issue, owner: str, status_value: str) -> bool:
        """Set an issue's Status field in its GitHub Project. Best-effort."""
        if not issue.item_id or not issue.project_number:
            return False
        info = self.get_field_info(issue.project_number, owner, "Status")
        if not info:
            return False
        option_id = info.options.get(status_value)
        if not option_id:
            return False
        return set_field_value(info.project_node_id, issue.item_id, info.field_id, option_id)
