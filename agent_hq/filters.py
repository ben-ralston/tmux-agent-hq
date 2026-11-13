"""Issue filtering + sorting helpers.

Pure functions over ``list[Issue]``. Designed to be cheap on every chip
toggle / keystroke (case-insensitive substring on a small in-memory list)
and shared between the TUI and web dispatch surfaces. Kept out of
``grouping.py``, which is agent-centric.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .models import Issue, Priority

_PRIORITY_ORDER = {
    Priority.NEED: 0,
    Priority.WANT: 1,
    Priority.IDEA: 2,
    Priority.NO_PRI: 3,
}

# The sort contract shared by both dispatch surfaces (#156). The TUI cycles
# its field through these with ``S``; the web view exposes one button each and
# validates an incoming cookie value against this tuple. ``created`` /
# ``updated`` are date fields whose direction is carried separately.
SORT_MODES = ("default", "created", "updated")

def _status_id(status: str | None) -> str:
    """Cookie-friendly identity for a status value.

    ``None`` and ``""`` both collapse to ``"none"`` so users can filter
    "issues without a status" with a single chip — gh occasionally
    returns either depending on board configuration.
    """
    if not status:
        return "none"
    return status.lower()


def filter_issues(
    issues: list[Issue],
    *,
    repos: list[str] | None = None,
    statuses: list[str] | None = None,
    sentinels: list[str] | None = None,
    query: str | None = None,
) -> list[Issue]:
    """Return issues matching every set filter.

    Within a dimension, values OR; across dimensions, they AND. An empty
    or ``None`` filter for a dimension means "no constraint" — not "match
    nothing" — so an unset chip group leaves all issues through.

    ``query`` is a case-insensitive substring match against
    ``title + body + repo_short + str(number)``. Whitespace-only queries
    behave as if unset (an accidental space shouldn't blank the list).
    """
    repo_filter = set(repos) if repos else None
    status_filter = {s.lower() for s in statuses} if statuses else None
    sentinel_filter = set(sentinels) if sentinels else None
    q = query.strip().lower() if query else ""

    out: list[Issue] = []
    for issue in issues:
        if repo_filter is not None and issue.repo not in repo_filter:
            continue
        if status_filter is not None and _status_id(issue.status) not in status_filter:
            continue
        if sentinel_filter is not None:
            bucket = "sentinel" if issue.is_sentinel else "other"
            if bucket not in sentinel_filter:
                continue
        if q:
            haystack = f"{issue.title}\n{issue.body}\n{issue.repo_short}\n{issue.number}".lower()
            if q not in haystack:
                continue
        out.append(issue)
    return out


def _default_key(issue: Issue) -> tuple:
    """Canonical order: priority, then repo, then number."""
    return (_PRIORITY_ORDER.get(issue.priority, 99), issue.repo, issue.number)


def parse_ts(value: str | None) -> float | None:
    """ISO-8601 string → epoch seconds, or ``None`` when missing/garbage.
    Shared by the sort/age helpers here and the hygiene sweep's stale rule +
    24 h due-check (review round: one parser, not four).

    Normalizes a trailing ``Z`` to ``+00:00`` because
    ``datetime.fromisoformat`` rejects ``Z`` before Python 3.11. A tz-naive
    value is read as UTC (deterministic across hosts) — GitHub always sends
    ``Z`` and internal stamps carry ``+00:00``, so this only governs
    hand-edited data-repo values.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_parse_ts = parse_ts  # back-compat alias for in-module callers


def format_relative_age(value: str | None, *, now: float | None = None) -> str:
    """Compact relative age of an ISO-8601 timestamp for display.

    Returns the coarsest single unit — ``now`` / ``5m`` / ``3h`` / ``2d`` /
    ``2w`` / ``2mo`` / ``2y`` — which reads well in a narrow column and conveys
    recency at a glance. Missing or unparseable values yield ``""``. A
    future-dated value (clock skew) clamps to ``now`` rather than rendering a
    negative age. ``now`` is injectable for deterministic tests; it defaults to
    the wall clock.
    """
    ts = _parse_ts(value)
    if ts is None:
        return ""
    reference = time.time() if now is None else now
    delta = reference - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    days = int(delta // 86400)
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _compact_duration(seconds: float) -> str:
    """``90`` → ``1m``, ``5400`` → ``1h``. Coarsest single unit, floor at 1m."""
    if seconds < 60:
        return "under 1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def format_stale_notice(status, *, now: float | None = None) -> str:
    """One-line description of a ``GitHubCache.FetchStatus`` that is not ok.

    Shared by the web dispatch banner and the TUI notification so both
    surfaces say the same thing (#254): what broke, how old the data on
    screen is, and — for a rate limit — how long the wait is. Returns ``""``
    for a healthy (or absent) status so callers can render unconditionally.

    ``status`` is duck-typed rather than imported to keep this module free of
    a ``github`` dependency; it needs ``ok`` / ``rate_limited`` / ``reset_at``
    / ``stale_since``, and optionally ``budget_skipped`` (#255).
    """
    if status is None or getattr(status, "ok", True):
        return ""
    reference = time.time() if now is None else now
    if getattr(status, "budget_skipped", False):
        # Nothing failed — we chose not to spend the last of the budget
        # (#255). Saying "rate limit exceeded" here would misdescribe both
        # what happened and what the user should do about it.
        headline = "GitHub budget low"
    elif getattr(status, "rate_limited", False):
        headline = "GitHub rate limit exceeded"
    else:
        headline = "GitHub unreachable"
    stale_since = getattr(status, "stale_since", None)
    if stale_since is None:
        body = "no cached issues to show"
    else:
        # Clock skew (or a same-instant failure) clamps to "just now" rather
        # than rendering a negative age.
        age = max(reference - stale_since, 0.0)
        body = f"showing cached issues from {_compact_duration(age)} ago"
    text = f"{headline} — {body}"
    reset_at = getattr(status, "reset_at", None)
    if reset_at and reset_at > reference:
        text += f" · resets in {_compact_duration(reset_at - reference)}"
    return text


def sort_issues(
    issues: list[Issue], sort_by: str = "default", descending: bool = True
) -> list[Issue]:
    """Sort issues for display.

    ``"default"`` orders by (priority, repo, number) — mirrors
    ``dispatch_screen.build_issue_rows`` so the TUI and web surfaces match.
    ``"created"`` / ``"updated"`` order by the matching timestamp; ``descending``
    puts the most recent first (``True``) or oldest first (``False``). Issues
    without a timestamp always sort last, and ties fall back to the default
    order — that tie-break stays ascending in both directions (so it's never
    reversed). Any unrecognized ``sort_by`` falls back to ``"default"``.
    """
    if sort_by not in ("created", "updated"):
        return sorted(issues, key=_default_key)

    sign = -1.0 if descending else 1.0

    def key(issue: Issue) -> tuple:
        ts = _parse_ts(issue.timestamp_for(sort_by))
        # ``ts is None`` first ⇒ undated rows land last in both directions.
        # ``sign * ts`` bakes direction into the key so we never pass
        # ``reverse=True`` (which would also flip the tie-break below).
        return (ts is None, 0.0 if ts is None else sign * ts, *_default_key(issue))

    return sorted(issues, key=key)


def available_filter_options(issues: list[Issue]) -> dict[str, list[dict]]:
    """Compute the chip set to render for a given issue list.

    Each chip group is a list of ``{"id", "label", "count"}`` dicts. Only
    values that appear at least once are returned (#40 decision: chip
    list is dynamic, never shows a chip whose tap would yield zero
    issues). Repos sort alphabetically by short name; statuses sort
    alphabetically with ``"none"`` last.
    """
    if not issues:
        return {"repos": [], "statuses": [], "sentinels": []}

    repo_counts: dict[str, int] = {}
    for issue in issues:
        repo_counts[issue.repo] = repo_counts.get(issue.repo, 0) + 1
    repos = [
        {"id": repo, "label": repo.split("/", 1)[-1], "count": count}
        for repo, count in sorted(repo_counts.items(), key=lambda kv: kv[0].split("/")[-1])
    ]

    status_buckets: dict[str, dict] = {}
    for issue in issues:
        sid = _status_id(issue.status)
        if sid not in status_buckets:
            status_buckets[sid] = {
                "id": sid,
                "label": issue.status if issue.status else "—",
                "count": 0,
            }
        status_buckets[sid]["count"] += 1
    # Stable sort: alphabetical, but "none" pinned last so a "no status"
    # bucket doesn't break up the named statuses on the chip row.
    statuses = sorted(
        status_buckets.values(),
        key=lambda s: (1 if s["id"] == "none" else 0, s["id"]),
    )

    # Sentinel buckets (#235 follow-up): the group only renders when the
    # list actually mixes machine-filed and manual issues — with a single
    # bucket the filter would be a no-op, so no chips (per the #40 rule).
    sentinel_count = sum(1 for issue in issues if issue.is_sentinel)
    sentinels = []
    if 0 < sentinel_count < len(issues):
        sentinels = [
            {"id": "sentinel", "label": "⚑ Sentinel", "count": sentinel_count},
            {"id": "other", "label": "Manual", "count": len(issues) - sentinel_count},
        ]

    return {"repos": repos, "statuses": statuses, "sentinels": sentinels}
