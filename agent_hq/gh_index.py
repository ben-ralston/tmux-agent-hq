"""Durable per-repo issue index, sourced from REST (#255).

The dispatch backlog needs two things the Projects ``item-list`` JSON does not
carry: issue timestamps (for the Created/Updated sort) and issue state (to hide
closed issues). Both used to come from ``gh issue list --limit 500`` — one
GraphQL query per repo per refresh, re-run by every agent-hq process on its own
clock.

Scoped honestly: those lists cost **2 GraphQL points each** (measured
2026-08-26), so ~28 points per refresh cycle across 7 repos. Against today's
~1,246-point cycle that is ~2%, because ``gh project item-list`` at 203 points
a board dominates everything. But once :mod:`agent_hq.gh_board` replaces the
board query with a 4-point equivalent, the cycle drops to ~52 points and this
index becomes **~54% of what remains** — a much better argument for it than
the one it was originally drafted with.

It also removes the #113 per-issue backfill outright, cuts refresh latency,
and moves the work to an entirely unused REST bucket.

The index is durable and cumulative: once seeded, a refresh asks only "what
changed since my cursor?", normally zero to three rows. A complete index also
has no 500-row window to fall outside of, which is what the #113 backfill
existed to paper over.

The file is a real cross-process coordination point: every read stats it and
reloads when it moved, and every write is a locked read-modify-write followed
by an atomic replace.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from pathlib import Path

from . import gh_meter
from ._locking import best_effort_file_lock

log = logging.getLogger(__name__)

_DEFAULT_INDEX_FILE = Path.home() / ".config" / "agent-hq" / "cache" / "issue-index.json"

_VERSION = 1


class IssueIndex:
    """Disk-backed ``{repo: {number: (created, updated, state)}}``.

    Rows are stored under short keys (``c``/``u``/``s``) because this file
    holds every issue of every configured repo indefinitely; the long form
    roughly doubles it for no benefit.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or _DEFAULT_INDEX_FILE
        self._lock = threading.RLock()
        self._repos: dict[str, dict] = {}
        self._mtime: float | None = None
        self._reload_if_changed()

    # --- disk -------------------------------------------------------------

    def _reload_if_changed(self) -> None:
        """Reload when another process (or another instance) wrote the file.

        This is what makes the file authoritative instead of a warm-start
        seed — the bug that let nine processes each run their own refresh
        clock against ``github.json`` (#255).
        """
        # The whole stat -> read -> install sequence runs under the instance
        # lock. Installing under the lock alone is not enough: a reader
        # preempted between parse and install would put an OLD snapshot over
        # a peer thread's just-merged repo, and a writer whose in-lock reload
        # had already seen the newer mtime would then persist that snapshot,
        # erasing the peer's repo from disk (#255 review). Stat before read,
        # for the same reason GitHubCache does: a replace landing between the
        # two must pair the new mtime with old contents, never the reverse.
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                return
            if mtime == self._mtime:
                return
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError, ValueError):
                return
            if not isinstance(data, dict) or data.get("version") != _VERSION:
                return
            repos = data.get("repos")
            if isinstance(repos, dict):
                self._repos = repos
                self._mtime = mtime

    def _write_locked(self) -> None:
        """Atomically persist. Caller holds both the file lock and ``_lock``."""
        payload = {"version": _VERSION, "repos": self._repos}
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload))
            # Atomic replace: a concurrent reader sees the old file or the new
            # one, never a half-written one.
            os.replace(tmp, self._path)
            self._mtime = self._path.stat().st_mtime
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _lock_path(self) -> Path:
        return self._path.with_suffix(".json.lock")

    # --- reads ------------------------------------------------------------

    def sync_lock_path(self, repo: str) -> Path:
        """Per-repo lock serializing index *fetches* across threads and processes.

        Per repo rather than global, for the same reason ``GitHubCache``'s
        fetch lock is per project: distinct repos must still sync in parallel,
        or a cold start goes back to sum-of-N (#90).
        """
        slug = repo.replace("/", "-")
        return self._path.parent / "locks" / f"index-{slug}.lock"

    def _entry(self, repo: str) -> dict:
        self._reload_if_changed()
        with self._lock:
            return self._repos.get(repo) or {}

    def is_seeded(self, repo: str) -> bool:
        return bool(self._entry(repo).get("seeded"))

    def synced_at(self, repo: str) -> float | None:
        value = self._entry(repo).get("synced_at")
        return float(value) if isinstance(value, (int, float)) else None

    def full_synced_at(self, repo: str) -> float | None:
        """When this repo was last seeded from scratch.

        Tracked separately from ``synced_at`` because the reseed decision must
        measure from the last FULL sync. Stamping one field on every sync makes
        ``now - last_full`` ~0 forever — a refresh cycle syncs every few
        minutes — so the daily reseed would never fire in production while
        still passing a single-sync test.

        ``None`` on an index written before this field existed, which costs one
        extra reseed on upgrade and is then self-correcting.
        """
        value = self._entry(repo).get("full_synced_at")
        return float(value) if isinstance(value, (int, float)) else None

    def cursor_for(self, repo: str) -> str | None:
        value = self._entry(repo).get("cursor")
        return value if isinstance(value, str) else None

    def timestamps_for(self, repo: str) -> dict[int, dict[str, str | None]]:
        """``{number: {"created_at": …, "updated_at": …}}``.

        Deliberately the same shape ``_enrich_dates`` already consumed from
        the deleted ``_fetch_issue_timestamps``, so swapping the data source
        is not a call-site rewrite.
        """
        issues = self._entry(repo).get("issues") or {}
        out: dict[int, dict[str, str | None]] = {}
        for key, row in issues.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            out[number] = {"created_at": row.get("c"), "updated_at": row.get("u")}
        return out

    def closed_keys_for(self, repo: str) -> set[str]:
        """``{'owner/repo#number'}`` for every closed issue the index knows.

        Cumulative, so unlike the old 500-row window an old closed issue still
        pinned to a board can no longer slip back into the backlog.
        """
        issues = self._entry(repo).get("issues") or {}
        return {
            f"{repo}#{key}"
            for key, row in issues.items()
            if isinstance(row, dict) and row.get("s") == "closed"
        }

    def open_keys_for(self, repo: str) -> set[str]:
        """``{'owner/repo#number'}`` for every OPEN issue the index knows —
        the off-board detector's side of the diff (#273)."""
        issues = self._entry(repo).get("issues") or {}
        return {
            f"{repo}#{key}"
            for key, row in issues.items()
            if str(key).isdigit() and isinstance(row, dict) and row.get("s") == "open"
        }

    # --- writes -----------------------------------------------------------

    def merge(
        self,
        repo: str,
        rows: list[dict],
        *,
        seeded: bool,
        synced_at: float,
        full_synced_at: float | None = None,
        replace: bool = False,
        seen_through: str | None = None,
    ) -> None:
        """Merge REST issue rows into ``repo``'s table and persist.

        ``rows`` are raw REST objects (``number``/``state``/``created_at``/
        ``updated_at``). The cursor advances to the newest ``updated_at`` seen,
        and only when rows actually came back — an empty incremental sync means
        nothing changed, not "rewind to the beginning".

        ``replace`` swaps the repo's whole table for ``rows`` instead of
        merging into it. Only a *complete* full sweep may set it: that is the
        one case where an absent issue means "GitHub no longer has it"
        (transferred or deleted) rather than "unchanged since the cursor", and
        clearing those rows is the entire reason the daily reseed exists. An
        incremental sync — or a full one truncated at the page cap — must
        never replace, or it would erase everything it did not happen to ask
        for (#255 review).

        ``seen_through`` is the newest ``updated_at`` the sweep *saw*,
        including the pull requests it dropped. The cursor must cover those
        too: a page made only of PRs has still been seen, and without this a
        PR-heavy prefix left no cursor at all, so every sync re-seeded from
        scratch (#255 review).
        """
        with best_effort_file_lock(self._lock_path()):
            # Re-read inside the lock: another process may have merged a
            # different repo since we last looked, and a whole-file write
            # would otherwise drop their work.
            self._reload_if_changed()
            with self._lock:
                entry = self._repos.setdefault(repo, {})
                if replace:
                    entry["issues"] = {}
                issues = entry.setdefault("issues", {})
                newest = None if replace else entry.get("cursor")
                for row in rows:
                    number = row.get("number")
                    if number is None:
                        continue
                    updated = row.get("updated_at")
                    # Never let an older read overwrite a newer one. Two syncs
                    # can race (a stalled fetch merging after a fresher one),
                    # and an unconditional write would resurrect stale state —
                    # reopening a closed issue — while the cursor kept the
                    # newer value, so no later incremental sync would repair
                    # it (#255 review).
                    existing = issues.get(str(number))
                    if (
                        not replace
                        and isinstance(existing, dict)
                        and existing.get("u")
                        and updated
                        and updated < existing["u"]
                    ):
                        continue
                    issues[str(number)] = {
                        "c": row.get("created_at"),
                        "u": updated,
                        "s": row.get("state"),
                    }
                    if updated and (newest is None or updated > newest):
                        newest = updated
                if seen_through and (newest is None or seen_through > newest):
                    newest = seen_through
                if newest is not None:
                    entry["cursor"] = newest
                entry["seeded"] = bool(seeded) or bool(entry.get("seeded"))
                entry["synced_at"] = synced_at
                # Only a full sweep restarts the reseed clock; an incremental
                # sync must leave it where it was.
                if full_synced_at is not None:
                    entry["full_synced_at"] = full_synced_at
                self._write_locked()

    def mark_closed(self, repo: str, number: int) -> None:
        """Record an issue as closed immediately after we closed it.

        Keeps the backlog from flashing the just-closed issue back before the
        next sync notices.

        The row's ``updated_at`` is stamped to *now*: a sync that fetched the
        issue open, paused while this close landed, and merged last would
        otherwise reopen it — ``merge``'s older-row guard only protects a row
        that carries a newer stamp than the stale one arriving. GitHub's own
        close bumps ``updated_at`` to about this same moment, so the next
        incremental sync corrects the second-level drift (#255 review).
        """
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with best_effort_file_lock(self._lock_path()):
            self._reload_if_changed()
            with self._lock:
                entry = self._repos.setdefault(repo, {})
                issues = entry.setdefault("issues", {})
                row = issues.get(str(number))
                if isinstance(row, dict):
                    row["s"] = "closed"
                    if not row.get("u") or row["u"] < now:
                        row["u"] = now
                else:
                    issues[str(number)] = {"c": None, "u": now, "s": "closed"}
                self._write_locked()


# --- REST sync ------------------------------------------------------------

# 100 is REST's maximum page size, so a seed costs ceil(issues/100) calls
# exactly once; steady state is a single call returning almost nothing.
_PER_PAGE = 100

# Bound the seed so a huge repo can't turn one refresh into hundreds of calls.
# 30 pages = 3,000 issues. Hitting it is logged, never silent.
_MAX_PAGES = 30

# `since=` can only report issues that were *updated*. A transfer or a delete
# leaves its row in the index forever, so periodically re-seed from scratch.
RESEED_AFTER_S = 86400.0


class IndexSyncError(RuntimeError):
    """The REST issue endpoint returned something that isn't a page of issues.

    Carries ``stderr`` so #254's ``_fetch_failure`` can inspect it for
    rate-limit wording, exactly as it does for a non-zero ``gh`` exit. Raw
    subprocess and JSON errors are deliberately NOT wrapped — ``_fetch_failure``
    already maps those, and re-wrapping would lose their type.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.stderr = message


def _issues_url(repo: str, page: int, since: str | None) -> str:
    params = {
        "state": "all",
        "per_page": str(_PER_PAGE),
        "sort": "updated",
        # Newest first, deliberately. Numbered pages over a mutable order
        # skip a row when it is updated mid-sweep and moves behind the page
        # boundary; newest-first turns that into a harmless duplicate. It
        # also means a sweep truncated at the page cap keeps the RECENT
        # rows a board actually shows, not the 3,000 oldest (#255 review).
        "direction": "desc",
        "page": str(page),
    }
    if since:
        params["since"] = since
    return f"repos/{repo}/issues?{urllib.parse.urlencode(params)}"


def _fetch_rows(repo: str, since: str | None) -> tuple[list[dict], bool, str | None]:
    """Page the REST issues endpoint, dropping PRs.

    Returns ``(rows, truncated, seen_through)``. ``truncated`` matters to the
    caller: a sweep cut off at the page cap has NOT seen the whole repo, so
    it must never be treated as authoritative about which issues no longer
    exist. ``seen_through`` is the newest ``updated_at`` in the raw pages —
    PRs included — so the cursor can cover what was seen, not only what was
    kept.

    Pagination is manual rather than ``gh --paginate`` so the page cap is
    enforceable and every page shows up as its own metered call.
    """
    rows: list[dict] = []
    truncated = True
    seen_through: str | None = None
    for page in range(1, _MAX_PAGES + 1):
        # Raw subprocess/JSON exceptions propagate: _fetch_failure maps them.
        result = gh_meter.run_gh(["gh", "api", _issues_url(repo, page, since)], kind="issue-index")
        batch = json.loads(result.stdout)
        if not isinstance(batch, list):
            # A dict here is GitHub's error envelope, not a page of issues.
            detail = ""
            if isinstance(batch, dict):
                detail = str(batch.get("message") or batch)
            raise IndexSyncError(f"issue index for {repo}: {detail or 'malformed response'}")
        for row in batch:
            updated = row.get("updated_at") if isinstance(row, dict) else None
            if isinstance(updated, str) and (seen_through is None or updated > seen_through):
                seen_through = updated
        # The endpoint returns PRs alongside issues; only PRs carry this key.
        rows.extend(row for row in batch if "pull_request" not in row)
        if len(batch) < _PER_PAGE:
            truncated = False
            break
    if truncated:
        log.warning(
            "issue index for %s truncated at the %d-page cap (%d issues kept); "
            "older issues are missing from the date sort and closed-issue filter",
            repo,
            _MAX_PAGES,
            len(rows),
        )
    return rows, truncated, seen_through


def sync_repo(repo: str, index: IssueIndex, *, now: float | None = None) -> None:
    """Bring ``repo``'s index up to date.

    Seeds from scratch when the repo is unknown or its last sync has aged past
    ``RESEED_AFTER_S``; otherwise asks only for issues updated since the stored
    cursor, which in steady state is a single REST call returning almost
    nothing. Raises on failure, leaving the existing index untouched.
    """
    import time as _time

    stamp = _time.time() if now is None else now
    last_full = index.full_synced_at(repo)
    full = not index.is_seeded(repo) or last_full is None or stamp - last_full >= RESEED_AFTER_S
    since = None if full else index.cursor_for(repo)
    rows, truncated, seen_through = _fetch_rows(repo, since)
    index.merge(
        repo,
        rows,
        seeded=True,
        synced_at=stamp,
        full_synced_at=stamp if full else None,
        seen_through=seen_through,
        # Only a full sweep that actually reached the end of the repo can say
        # which issues have gone away; a truncated one would delete every
        # issue past the page cap.
        replace=full and not truncated,
    )
