"""Issues hub routes (#247) — Queue + Browse modes over the issue
backlog: chip filters, search, multi-select, group, dispatch execution,
and the triage dimension editor. Browse mirrors the TUI's
``DispatchScreen`` (``agent_hq/screens/dispatch_screen.py``) so a phone
user can do everything the desktop TUI offers.

Architecture:

* ``GitHubCache`` and ``Config`` live on ``app.state`` (set in
  ``create_app``); tests inject overrides via the ``config=`` /
  ``github_cache=`` kwargs.
* User state (filters + selection + groups) lives in a single signed
  cookie (``agent_hq.web.state``).
* Issue list rendering is a pure pipeline:
    cached issues → drop Done → de-dup by key → apply filters → sort.
* Long-running operations (``dispatch_via_claude`` blocks for ~15 s
  waiting on Claude readiness) run as ``BackgroundTasks`` so the
  request returns immediately and the SSE broker streams progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from agent_hq import eventlog
from agent_hq.capabilities import LauncherCapabilities, supports_harness
from agent_hq.config import EFFORT_ORDER
from agent_hq.dispatch import (
    dispatch_via_claude,
    generate_branch_name,
    resolve_and_validate_harness,
)
from agent_hq.filters import (
    SORT_MODES,
    available_filter_options,
    filter_issues,
    format_relative_age,
    format_stale_notice,
    parse_ts,
    sort_issues,
)
from agent_hq.github import (
    close_issue_not_planned,
    detect_active_worktrees,
    fetch_issue_summary,
    invalidate_worktree_cache,
    refresh_issue_timestamps,
)
from agent_hq.hygiene_actions import execute as execute_hygiene
from agent_hq.hygiene_actions import execution_facts, plan_steps
from agent_hq.hygiene_store import FindingStatus, FindingType
from agent_hq.hygiene_worker import collect_offboard_inputs, run_sweep
from agent_hq.issues_hub import (
    IssueAnnotation,
    annotate,
    dimension_filter_options,
    ensure_triage_records,
    filter_by_dimensions,
    issue_is_triaged,
    merge_issue_lists,
    queue_partition,
)
from agent_hq.models import Issue
from agent_hq.triage_store import (
    AgentFitness,
    Effort,
    TriageRecord,
    TriageState,
    load_rank_weights,
    rank_breakdown,
    rank_score,
)
from agent_hq.triage_worker import run_triage, select_targets
from agent_hq.ui_prefs import WEB_ISSUES_MODES
from agent_hq.web.events import Event
from agent_hq.web.state import (
    DispatchState,
    read_dispatch_state,
    write_dispatch_state,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_md = MarkdownIt("default", {"breaks": False, "linkify": True})
templates.env.filters["markdown"] = lambda s: Markup(_md.render(s or ""))
# Relative age (#101) for the updated-at field shown when sorting by recency.
templates.env.filters["relative_age"] = format_relative_age
# One Python definition of "triaged" for the sentinel badge/count (#247
# review — the rule was drifting across three template copies).
templates.env.globals["issue_is_triaged"] = issue_is_triaged

log = logging.getLogger(__name__)


# Mapping for picker form values → GitHub Project labels. Lowercase IDs
# travel over the wire (matches the ``filters`` module's chip identity);
# the human label is what ``GitHubCache.set_status`` expects to look up
# against the cached field options. The priority tables left with the GH
# priority picker (#247) — the dimension editor writes the triage store.
# ``Blocked`` is intentionally absent — tracked separately in #76.
_STATUS_VALUE_TO_LABEL = {
    "todo": "Todo",
    "in-progress": "In Progress",
    "done": "Done",
}


# Accepted form values per triage dimension. The detail sheet's editor
# (#247) writes all five; the Triage inbox's tap-a-chip adjust (#248) is
# restricted to _TRIAGE_INBOX_DIMS. The two flags joined the inbox in #267
# so they can be set on the row before Confirm; the inbox's pin tap writes
# ``pin_staged`` (inert until ``TriageStore.confirm`` folds it into ``pin``),
# never ``pin`` itself — that stays the Queue/sheet toggle's direct write.
_TRIAGE_DIM_VALUES = {
    "value": {"1", "2", "3", "4", "5"},
    "effort": {"S", "M", "L"},
    "fitness": {"solo", "pair", "needs_you"},
    "interest": {"true", "false"},
    "pin": {"true", "false"},
    "autopilot": {"true", "false"},  # #302 nightly opt-in, human-only
}
_TRIAGE_INBOX_DIMS = frozenset({"value", "effort", "fitness", "interest", "pin", "autopilot"})


# ----- Data plumbing ------------------------------------------------------


async def _load_issues_fresh(app: FastAPI, *, strict: bool = True) -> list[Issue]:
    """Forced refetch of every board + closed keys for a destructive action
    (Codex round 3): the SWR cache can be arbitrarily old behind an open tab.
    ``strict`` raises on any failure instead of degrading to a partial or
    last-good list — a partial list reads as "closed". The cache never
    raises on a failed fetch (#254 serves last-good and records the failure
    per key), so the verdict is read afterwards from ``failed_keys()`` for
    exactly the boards and repos this refetch touched — ``fetch_status`` is
    process-global and would refuse on a lingering failure for a project
    the refetch never fetched (built-in review round 4)."""
    issues = await _load_issues(app, force=True)
    if strict:
        cache = app.state.github_cache
        cfg = app.state.config
        touched = {f"{p.owner}/{p.number}" for p in (cfg.projects if cfg else [])}
        touched |= {f"closed:{i.repo}" for i in issues}
        failed = cache.failed_keys() if hasattr(cache, "failed_keys") else set()
        hit = sorted(failed & touched)
        if hit:
            status = cache.fetch_status() if hasattr(cache, "fetch_status") else None
            detail = (status.message if status is not None and status.message else None) or (
                "GitHub fetch failed for " + ", ".join(hit)
            )
            raise RuntimeError(detail)
        # The board fetch dated the issues behind the index-sync debounce;
        # the forced closed-keys sync above refreshed the index afterwards
        # (Codex round 4) — read the dates the plan will be judged on.
        await run_in_threadpool(refresh_issue_timestamps, issues)
    return issues


async def _load_issues(app: FastAPI, force: bool = False) -> list[Issue]:
    """Fetch issues from every configured project, drop Done, de-dup by key.

    Same shape as the TUI's ``DispatchScreen._refresh_data`` but projects
    are fetched **concurrently** via ``asyncio.gather``. On cold cache,
    sequential latency was the sum of every ``gh project item-list``
    invocation (~2s each); now it's max-of-N (#90).

    ``force=True`` bypasses the cache TTL (used by /issues/refresh —
    threaded through here so refresh fetches each project exactly once
    instead of once-forced + once-from-cache).
    """
    cfg = app.state.config
    cache = app.state.github_cache
    if cfg is None or not cfg.projects:
        return []

    async def _fetch(proj):
        try:
            return await run_in_threadpool(cache.get_issues, proj.number, proj.owner, force)
        except Exception:
            log.warning(
                "github cache fetch failed for %s/%s", proj.owner, proj.number, exc_info=True
            )
            return []

    fetched = await asyncio.gather(*(_fetch(p) for p in cfg.projects))
    all_issues = merge_issue_lists(fetched)

    # Hide closed issues — the project ``item-list`` JSON does not expose
    # issue state, so closed keys come from a separate TTL-cached lookup.
    # Same rule as the TUI's ``_refresh_data``.
    try:
        # `force` rides through to the closed-keys lookup too: an explicit
        # refresh that re-fetched every board but left a closed issue on
        # screen for the closed-keys TTL was not the refresh the user asked
        # for (#255 review).
        closed = await run_in_threadpool(cache.get_closed_keys, {i.repo for i in all_issues}, force)
    except Exception:
        log.exception("closed-issue lookup failed")
        closed = set()
    return [i for i in all_issues if i.key not in closed]


async def _load_issues_swr(app: FastAPI) -> tuple[list[Issue], bool]:
    """Serve whatever the cache holds — even TTL-expired — without fetching.

    Returns ``(issues, needs_refresh)``. Stale-while-revalidate (#226): the
    cached reads are in-memory dict lookups — run in the threadpool anyway
    because they take the cache lock, which a concurrent background refresh
    holds across its (large) disk write. Two blocking exceptions:

    - truly cold cache (NO project has any cached entry): fall back to the
      blocking ``_load_issues`` so the first-ever load keeps today's
      skeleton-then-data behavior — ``needs_refresh`` is then False.
    - cold closed-keys cache (in-memory only, gone after a service restart
      while issues persist on disk): fetched blocking once, TTL-cached, so
      closed/Won't-Do issues never render from the stale serve.
    """
    cfg = app.state.config
    cache = app.state.github_cache
    if cfg is None or not cfg.projects:
        return [], False

    def _read_cached():
        cached = [cache.get_issues_cached(p.number, p.owner) for p in cfg.projects]
        if all(issues is None for issues, _ in cached):
            return None  # cold — caller falls back to the blocking load
        needs = any(issues is None or not fresh for issues, fresh in cached)
        merged = merge_issue_lists(issues for issues, _ in cached if issues is not None)
        repos = {i.repo for i in merged}
        closed_cold = bool(repos) and not cache.has_closed_entries(repos)
        closed, closed_fresh = cache.get_closed_keys_cached(repos)
        return merged, repos, closed, closed_fresh, needs, closed_cold

    result = await run_in_threadpool(_read_cached)
    if result is None:
        return await _load_issues(app), False
    merged, repos, closed, closed_fresh, needs_refresh, closed_cold = result
    if closed_cold:
        try:
            closed = await run_in_threadpool(cache.get_closed_keys, repos)
            closed_fresh = True
        except Exception:
            log.exception("closed-issue lookup failed")
            closed, closed_fresh = set(), False
    return [i for i in merged if i.key not in closed], needs_refresh or not closed_fresh


async def _refresh_issues_background(app: FastAPI) -> None:
    """Background refresh behind a stale serve; publishes ``dispatch.refreshed``.

    Only stale/missing projects are fetched (review fix, #226): a
    full forced sweep would turn one permanently-broken project into an
    every-cooldown refetch of ALL projects, burning API rate limit. Closed
    keys refresh through their own TTL (non-forced).

    ``get_issues`` swallows most gh errors (a failed project loads ``[]``),
    so success is judged by re-checking freshness afterwards. A still-stale
    cache raises, tripping the refresher's cooldown — without this, a
    persistently failing gh would loop stale-serve → refresh → SSE →
    refetch → refresh forever. The event fires either way (status
    ``ok``/``error``) so open tabs refetch exactly once; a
    cooldown-suppressed re-kick then renders stale data without the
    indicator.
    """
    status = "ok"
    try:
        cfg = app.state.config
        cache = app.state.github_cache
        if cfg is not None and cfg.projects:

            def _stale_project_list():
                out = []
                for proj in cfg.projects:
                    issues, fresh = cache.get_issues_cached(proj.number, proj.owner)
                    if issues is None or not fresh:
                        out.append(proj)
                return out

            async def _fetch_stale(proj):
                try:
                    # Deliberately NOT force=True (#255 review). The list is
                    # already filtered to stale projects, so a plain call
                    # fetches just the same — while still honouring the
                    # budget gate and the "a peer just fetched this" recheck
                    # inside the fetch lock. Forcing skipped both, which made
                    # the budget gate inert on the normal web refresh path
                    # and left concurrent processes serializing and then
                    # each fetching anyway.
                    await run_in_threadpool(cache.get_issues, proj.number, proj.owner)
                except Exception:
                    log.warning(
                        "github cache fetch failed for %s/%s",
                        proj.owner,
                        proj.number,
                        exc_info=True,
                    )

            stale = await run_in_threadpool(_stale_project_list)
            await asyncio.gather(*(_fetch_stale(p) for p in stale))

            def _cached_repos():
                repos: set[str] = set()
                for proj in cfg.projects:
                    issues, _ = cache.get_issues_cached(proj.number, proj.owner)
                    repos.update(i.repo for i in issues or [])
                return repos

            repos = await run_in_threadpool(_cached_repos)
            if repos:
                try:
                    await run_in_threadpool(cache.get_closed_keys, repos)
                except Exception:
                    log.exception("closed-issue lookup failed")
        _, still_stale = await _load_issues_swr(app)
        if still_stale:
            status = "error"
    except Exception:
        status = "error"
        raise
    finally:
        await _publish(app.state.broker, "dispatch.refreshed", {"status": status})
    if status == "error":
        raise RuntimeError("issue refresh did not freshen the cache")


def _kick_issues_refresh(app: FastAPI, needs_refresh: bool) -> bool:
    """Start (or coalesce onto) the background refresh; returns the
    "updating…" indicator flag. False during the post-failure cooldown."""
    if not needs_refresh:
        return False
    return app.state.issues_refresher.kick(lambda: _refresh_issues_background(app))


async def _load_active_worktrees(app: FastAPI) -> set[str]:
    cfg = app.state.config
    if cfg is None or not cfg.repos:
        return set()
    try:
        return await run_in_threadpool(detect_active_worktrees, cfg.repos)
    except Exception:
        log.exception("active-worktree detection failed")
        return set()


def _prune_stale_filters(
    state: DispatchState, issues: list[Issue], opts: dict, *, fetch_failed: bool = False
) -> bool:
    """Drop cookie filter values whose chip no longer exists (#80).

    Skipped when the issue list is empty: a transient gh failure loads
    ``[]``, and an empty option set must not read as "every chip
    vanished" and wipe the user's filters (cf. #118's transient-empty
    lesson). With zero issues the stale filter can't hide anything
    anyway, so deferring the prune to the next successful load is safe.

    Also skipped when ``fetch_failed`` (#179): a board that failed cold
    contributes no issues, so its repo's chips are missing from an
    otherwise non-empty list — unknown, not gone.
    """
    if not issues or fetch_failed:
        return False
    return state.prune_filters(opts)


async def _github_fetch_failed(app: FastAPI) -> bool:
    """True while a configured board has an unexpired fetch failure (#179).

    A failed board serves last-good when it has one and ``[]`` when cold
    (#254), so an issue or chip missing from the loaded list is unknown
    rather than gone — callers skip destructive pruning on it. Scoped to the
    configured boards' ``owner/number`` keys, like ``_load_issues_fresh``: a
    ``closed:`` lookup failure never removes an issue from the list, and a
    lingering failure for a board no longer configured is irrelevant.
    Off the event loop: ``failed_keys`` takes the cache lock a background
    refresh holds across its disk write. Duck-typed like ``_github_notice``.
    """
    cfg = app.state.config
    probe = getattr(app.state.github_cache, "failed_keys", None)
    if probe is None or cfg is None:
        return False
    boards = {f"{p.owner}/{p.number}" for p in cfg.projects}
    try:
        return bool(await run_in_threadpool(probe) & boards)
    except Exception:  # pragma: no cover - defensive
        log.warning("github failed-keys probe failed", exc_info=True)
        return False


def _dispatch_form_context(app: FastAPI, caps: LauncherCapabilities) -> dict:
    """Harness/effort/model picker context shared by every render that
    embeds the action bar (#205 Stage 1B Task 15).

    ``codex_available`` guards the codex ``<option>`` — the picker must
    only ever offer a harness the installed launch.sh actually advertises.
    ``default_harness`` pre-selects the harness select — a genuine choice
    of harness. ``default_effort`` / ``default_model`` come from
    ``config.dispatch.harnesses[default_harness]`` but must NOT pre-fill
    the effort select / model input as explicit form values (multi-review
    fix wave, Fix 5): a value submitted as an explicit override defeats the
    server-side per-harness default resolution the moment the user switches
    harness in the picker (the blank-field path already resolves against
    the CHOSEN harness's config at dispatch time — pre-filling just pins
    the page-load harness's default onto every submission regardless of
    what's actually selected). ``default_model`` is instead surfaced only
    via the model input's ``placeholder`` text; ``default_effort`` is kept
    in this dict for parity/future use but the effort select's blank
    "effort" option stays selected.
    ``effort_options`` is ``EFFORT_ORDER`` (config.py) so the template loop
    can never drift from the allowlist.

    ``caps`` is the already-resolved probe result (Fix 7, multi-review
    wave): this helper (and ``_render_view``, its caller) run in a sync
    context, so the probe itself must be run via ``run_in_threadpool`` by
    an async route handler further up and threaded down here — calling
    ``app.state.capabilities_probe()`` directly from this sync context
    would shell out to launch.sh on the event loop thread.
    """
    codex_available = supports_harness(caps, "codex")
    cfg = app.state.config
    default_harness = cfg.dispatch.default_harness if cfg is not None else "claude"
    harness_defaults = cfg.dispatch.harnesses.get(default_harness) if cfg is not None else None
    return {
        "codex_available": codex_available,
        "default_harness": default_harness,
        "default_effort": harness_defaults.effort if harness_defaults else None,
        "default_model": harness_defaults.model if harness_defaults else None,
        "effort_options": EFFORT_ORDER,
    }


def _filter_options_with_dimensions(
    issues: list[Issue], annotations: dict[str, IssueAnnotation]
) -> dict:
    """available_filter_options + the effort/fitness groups (#247).

    Merged before ``_prune_stale_filters`` runs so a transient store
    failure (empty annotations) can't wipe the user's effort/fitness
    selections (#118 class); with no annotations every issue buckets as
    "none", which still keeps explicitly-chosen "none" chips alive, and
    prune drops the rest exactly as it does for a vanished repo chip.
    """
    opts = available_filter_options(issues)
    opts.update(dimension_filter_options(issues, annotations))
    return opts


def _sync_triage_pass(store, issues: list[Issue]) -> dict[str, IssueAnnotation]:
    """Blocking half of the per-load triage pass (#247): idempotent
    auto-seeding (records + priority hints, tombstone/revive) then the
    Issue↔record join. Runs in the threadpool — flock + file I/O must
    stay off the event loop.

    The store snapshot is read once and shared by both halves in the
    steady state (zero writes). When the ensure pass DID write — a seed,
    tombstone, or revive — the snapshot is stale and gets re-read before
    annotating, or the first load after an issue reopens would render its
    record tombstoned/scoreless for one request (#247 Codex review).
    Write failures (read-only data repo, disk full) must degrade the
    annotations, never 500 the page — the store's read paths already
    log-and-skip, and this is the write-side twin of that contract.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = store.all_records(include_tombstoned=True)
    try:
        if ensure_triage_records(store, issues, now=now, records=records):
            records = store.all_records(include_tombstoned=True)
    except OSError as exc:
        log.warning("triage seeding pass failed: %s", exc)
        eventlog.emit("warn", "triage seeding failed", {"error": str(exc)}, min_interval_s=300)
    return annotate(issues, store, load_rank_weights(store.data_dir), records=records)


async def _annotate_issues(app: FastAPI, issues: list[Issue]) -> dict[str, IssueAnnotation]:
    store = app.state.triage_store
    if store is None:
        return {}
    return await run_in_threadpool(_sync_triage_pass, store, issues)


def _proposed_count(store) -> int:
    """Badge count for the mode switch (#248): store-only, no gh fetch —
    the page shell must stay skeleton-fast (#95).

    Deliberately store-GLOBAL: it counts every PROPOSED record, while the
    inbox itself renders the loaded-issues ∩ repo-filter join. The two can
    legitimately differ (a repo chip is active, or a record outlives the
    cached issue list), and the badge is a "there is work waiting" signal,
    not a row count for the current view.
    """
    if store is None:
        return 0
    return sum(1 for r in store.all_records() if r.state is TriageState.PROPOSED)


# How long a resolved ``claude`` PATH lookup is trusted. The predicates below
# run on every container render (finder round 4: two synchronous PATH sweeps
# per request); a binary appearing or vanishing shows up within this window.
_CLAUDE_PATH_TTL_S = 60.0


def _claude_on_path(app: FastAPI) -> bool:
    """``shutil.which("claude")`` memoized per app for ``_CLAUDE_PATH_TTL_S``."""
    now = time.time()
    cached = getattr(app.state, "claude_path_probe", None)
    if cached is not None and now - cached[0] < _CLAUDE_PATH_TTL_S:
        return cached[1]
    found = shutil.which("claude") is not None
    app.state.claude_path_probe = (now, found)
    return found


def _ai_available(app: FastAPI, *, flag: str, stores: tuple[str, ...]) -> bool:
    """Shared "an AI worker can run" predicate: the named stores are
    configured, the ``triage.<flag>`` switch is on, and claude is on PATH."""
    cfg = app.state.config
    return (
        all(getattr(app.state, store, None) is not None for store in stores)
        and cfg is not None
        and cfg.triage is not None
        and bool(getattr(cfg.triage, flag))
        and _claude_on_path(app)
    )


def _triage_ai_available(app: FastAPI) -> bool:
    """Worker can run: store + ``triage.ai_enabled`` + a claude on PATH."""
    return _ai_available(app, flag="ai_enabled", stores=("triage_store",))


def _maybe_kick_triage_worker(
    app: FastAPI,
    issues: list[Issue],
    annotations: dict[str, IssueAnnotation],
    *,
    available: bool | None = None,
) -> bool:
    """After the seeding/annotate pass (#248): if any issue still needs an
    AI proposal, ensure one worker run is in flight. Singleflight + the
    refresher's 600 s failure cooldown keep a broken claude from looping.
    Returns the "AI triage running…" indicator flag.

    ``available`` lets a caller that already ran ``_triage_ai_available``
    (the container routes, which also pass it into the render) share the
    result — it costs a ``shutil.which`` PATH sweep on the event loop.
    """
    if not (_triage_ai_available(app) if available is None else available):
        return False
    records = [a.record for a in annotations.values() if a.record is not None]
    cap = app.state.config.triage.ai_batch_cap
    if not select_targets(issues, records, cap=cap):
        return app.state.triage_refresher.in_flight
    return app.state.triage_refresher.kick(lambda: _run_triage_background(app, issues))


async def _run_triage_background(app: FastAPI, issues: list[Issue]) -> None:
    """One worker run off the event loop, then tell open tabs about it.

    A run that targeted nothing did nothing: it publishes nothing (every
    open tab would refetch the container for no change) and leaves the
    refresher free for a later real run.

    Otherwise the publish happens before the cooldown-tripping raise so a
    run that only produced failures still refetches the container (the
    Triage tab count / "AI triage running…" pill must clear either way).

    Any run that targeted issues and proposed none is zero progress —
    errored or not — and must trip the cooldown: the next container load
    selects the same targets, and the publish above makes open tabs refetch,
    so without the block a broken batch becomes a paid ``claude -p`` call
    every refresh.
    """
    cfg = app.state.config
    report = await run_in_threadpool(
        run_triage, issues, app.state.triage_store, cfg.triage, cfg.repos
    )
    if not report.targeted:
        return
    await _publish(
        app.state.broker,
        "triage.proposed",
        {"proposed": report.proposed, "failed": report.failed},
    )
    if not report.proposed:
        # Trip the refresher cooldown; run_triage already emitted the detail.
        raise RuntimeError("triage worker run produced no proposals")


HYGIENE_SWEEP_INTERVAL_S = 24 * 3600

# Off-board first (#273: an issue nobody can see beats everything else),
# then Clusters (hand-test round 2: the owner works them first), then Duplicates.
_HYGIENE_SECTIONS = (
    (FindingType.OFFBOARD, "Not on board"),
    (FindingType.CLUSTER, "Clusters"),
    (FindingType.DUPLICATE, "Duplicates"),
    (FindingType.MISMATCH, "Mismatches"),
    (FindingType.STALE, "Stale"),
)


def _hygiene_open_count(store) -> int:
    """Badge count (#249): store-only, like ``_proposed_count``."""
    return 0 if store is None else store.open_count()


def _hygiene_ai_available(app: FastAPI) -> bool:
    return _ai_available(app, flag="hygiene_enabled", stores=("hygiene_store", "triage_store"))


@dataclass(frozen=True)
class HygieneSnapshot:
    """One store read per request (review round): the sweep kick, the
    Hygiene-mode render and the bar all read from this, taken off the event
    loop once instead of three tolerant file reads on the loop."""

    findings: tuple
    last_swept_at: str | None


async def _hygiene_snapshot(app: FastAPI) -> HygieneSnapshot | None:
    store = app.state.hygiene_store
    if store is None:
        return None

    def read() -> HygieneSnapshot:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        findings, swept = store.snapshot(now=now)
        return HygieneSnapshot(tuple(findings), swept)

    return await run_in_threadpool(read)


def _swept_label(iso: str | None) -> str:
    """ "never" / "just now" / "3h ago" for the bar, on the shared formatter."""
    if not iso:
        return "never"
    age = format_relative_age(iso)
    if not age:
        return "unknown"
    return "just now" if age == "now" else f"{age} ago"


def _sweep_is_due(last_swept_at: str | None, now: float | None = None) -> bool:
    ts = parse_ts(last_swept_at)
    if ts is None:
        return True
    return (time.time() if now is None else now) - ts >= HYGIENE_SWEEP_INTERVAL_S


def _offboard_configured(app: FastAPI) -> bool:
    cfg = app.state.config
    return bool(cfg and any(rc.project for rc in cfg.repos.values()))


def _maybe_kick_hygiene_sweep(
    app: FastAPI,
    issues: list[Issue],
    *,
    snapshot: HygieneSnapshot | None,
    available: bool | None = None,
) -> bool:
    """Use-triggered daily sweep (#249): kick when never swept or the stamp
    is over 24 h old. Returns the "Sweeping…" indicator flag. The stamp
    comes from the request's snapshot — no store read on the loop."""
    if snapshot is None:
        return False
    if not issues and not _offboard_configured(app):
        # An empty list is a cold cache or a failed fetch, not a clean backlog
        # (review round 3): nothing to sweep, and a run would either stamp
        # (24 h of silence) or churn. With a board configured it is ALSO the
        # off-board headline case — an empty board with every issue off it
        # (#273 review round 5) — so the sweep runs; ``run_sweep`` reports
        # ``skipped`` (no stamp) when the detector found no board to check,
        # and the background runner trips the cooldown on that.
        return False
    if not (_hygiene_ai_available(app) if available is None else available):
        return False
    if app.state.hygiene_refresher.in_flight:
        return True
    if not _sweep_is_due(snapshot.last_swept_at):
        return False
    return app.state.hygiene_refresher.kick(lambda: _run_sweep_background(app, issues))


async def _run_sweep_background(app: FastAPI, issues: list[Issue]) -> None:
    """One sweep off the event loop. Always publishes ``hygiene.swept`` on
    completion — the container has no poll, so a clean or failed sweep that
    stayed silent left "Sweeping…" on screen until the next load (Codex
    round); raises (tripping the cooldown) only on an errored run —
    ``run_sweep`` stamps on failure, so the cooldown is belt-and-braces for a
    run that could not even stamp."""
    cfg = app.state.config
    # The open set decides what resolves (finder round 4): refetch the boards
    # rather than trust the SWR list an open tab happened to hold — a few
    # GraphQL points once a day next to the paid claude call. A refetch that
    # yields nothing (cold + failed) keeps the list we were handed.
    fresh = await _load_issues(app, force=True)
    if fresh:
        issues = fresh

    def sweep():
        # Off-board detection (#273) reads the boards just refetched (the
        # cache's never-fetching read) and syncs the REST issue index — a
        # subprocess, so it stays off the loop with the sweep itself.
        offboard = collect_offboard_inputs(cfg, app.state.github_cache)
        return run_sweep(
            issues,
            app.state.triage_store,
            app.state.hygiene_store,
            cfg.triage,
            cfg.repos,
            offboard=offboard,
            describe=fetch_issue_summary,
        )

    report = await run_in_threadpool(sweep)
    await _publish(
        app.state.broker,
        "hygiene.swept",
        {"written": report.written, "resolved": report.resolved, "error": report.error},
    )
    if report.error:
        raise RuntimeError(f"hygiene sweep failed: {report.error}")
    if report.skipped:
        # Nothing to check and nothing stamped: without the cooldown the next
        # load would kick another forced board refetch (#273 review round 5).
        raise RuntimeError("hygiene sweep skipped: nothing to sweep")


def _progress_rows(finding, *, final: bool) -> list[dict]:
    """Checklist rows ``{label, state, detail}`` for the Execute modal.
    States: pending / running / done / failed / skipped."""
    ex = finding.execution if isinstance(finding.execution, dict) else {}
    if final:
        rows = [
            {
                "label": s.get("label", ""),
                "state": "done" if s.get("ok") else "failed",
                "detail": s.get("detail", ""),
            }
            for s in ex.get("steps", [])
            if isinstance(s, dict)
        ]
        # A plan cut short by a failure still lists what did not run.
        rows += [
            {"label": label, "state": "pending", "detail": ""}
            for label in ex.get("plan", [])[len(rows) :]
        ]
        return rows
    plan = ex.get("plan") if ex.get("in_flight") else None
    if not isinstance(plan, list):
        return []
    done = [s for s in ex.get("progress", []) if isinstance(s, dict)]
    rows = []
    for i, label in enumerate(plan):
        if i < len(done):
            s = done[i]
            state = "done" if s.get("ok") else "failed"
            rows.append({"label": label, "state": state, "detail": s.get("detail", "")})
        elif i == len(done) and all(s.get("ok") for s in done):
            rows.append({"label": label, "state": "running", "detail": ""})
        else:
            rows.append({"label": label, "state": "pending", "detail": ""})
    return rows


def preview_rows(steps) -> list[dict]:
    """Checklist rows for the preview, before anything runs."""
    return [
        {"label": s.label, "state": "pending" if s.actionable else "skipped", "detail": ""}
        for s in steps
    ]


def _hygiene_sections(snapshot: HygieneSnapshot | None, state: DispatchState) -> list[dict]:
    """Open findings grouped by type in display order; the repo chip filter
    applies on ``issue_keys`` (a finding shows when ANY of its issues is in
    a selected repo)."""
    if snapshot is None:
        return []
    repos = {r.lower() for r in state.repos}  # chip vs finding key case (#273 round 10)
    grouped: dict[FindingType, list] = {t: [] for t, _ in _HYGIENE_SECTIONS}
    for f in snapshot.findings:
        if f.status is not FindingStatus.OPEN:
            continue
        if repos and not any(k.rsplit("#", 1)[0].lower() in repos for k in f.issue_keys):
            continue
        grouped[f.type].append(f)
    out = []
    for ftype, label in _HYGIENE_SECTIONS:
        items = sorted(grouped[ftype], key=lambda f: (f.found_at, f.id), reverse=True)
        if not items:
            continue
        section = {"type": ftype.value, "label": label, "findings": items}
        if ftype is FindingType.OFFBOARD:
            # Per-repo groups carry the "Add all" button (#273); within a repo
            # the cards run by issue number so the list reads like the repo.
            by_repo: dict[str, list] = {}
            for f in items:
                by_repo.setdefault(f.issue_keys[0].rsplit("#", 1)[0], []).append(f)
            section["groups"] = [
                {
                    "repo": repo,
                    "findings": sorted(fs, key=lambda f: int(f.issue_keys[0].rsplit("#", 1)[1])),
                }
                for repo, fs in sorted(by_repo.items())
            ]
        out.append(section)
    return out


async def _resolve_mode(app: FastAPI, mode: str | None) -> str:
    """Validated ?mode= wins; else the persisted pref; else "queue".

    Mirrors /agents?view= (#176): partial and POST routes carry the mode
    they were rendered under, so the pref read is the rare fallback path.
    """
    if mode in WEB_ISSUES_MODES:
        return mode
    stored = await run_in_threadpool(app.state.ui_prefs_store.get_web_issues_mode)
    return stored or "queue"


def _filter_visible(
    issues: list[Issue],
    state: DispatchState,
    annotations: dict[str, IssueAnnotation] | None,
    *,
    dimensions: bool,
    query: str = "",
) -> list[Issue]:
    """The one chip-filter rule for every /issues mode (#248 hand-test):
    repo, sentinel and status chips always apply; effort/fitness only where
    the bar renders them (``dimensions`` — Queue and Browse, not Triage);
    the search box is Browse-only (``query``). Confirm-all reuses this so
    "Confirm all (N)" acts on exactly the cards the user can see."""
    filtered = filter_issues(
        issues,
        repos=state.repos,
        statuses=state.statuses,
        sentinels=state.sentinels,
        query=query,
    )
    if dimensions:
        filtered = filter_by_dimensions(
            filtered,
            annotations or {},
            efforts=state.efforts,
            fitnesses=state.fitnesses,
            autopilots=state.autopilots,
        )
    return filtered


def _github_notice(app: FastAPI) -> str:
    """Stale-data banner text for the dispatch view, ``""`` when healthy.

    Reads ``GitHubCache.fetch_status()`` (#254) so a failed refresh renders
    as "GitHub unreachable — showing cached issues from 2h ago" instead of
    the benign empty state. Duck-typed and exception-swallowing: the cache is
    injectable (``create_app(github_cache=…)``) and a status probe must never
    be the reason the backlog fails to render.

    Blocking — ``fetch_status`` takes the cache lock, which a background
    refresh holds across its (large) disk write. Callers must reach it through
    ``_render_inputs``, never inline on the event loop.
    """
    cache = getattr(app.state, "github_cache", None)
    probe = getattr(cache, "fetch_status", None)
    if probe is None:
        return ""
    try:
        return format_stale_notice(probe())
    except Exception:  # pragma: no cover - defensive
        log.warning("github fetch-status probe failed", exc_info=True)
        return ""


async def _render_inputs(app: FastAPI) -> tuple[LauncherCapabilities, str]:
    """The two blocking reads every dispatch render needs, in ONE threadpool
    hop: the launcher-capabilities probe (subprocess) and the GitHub
    fetch-status notice (cache lock). ``_render_view`` is sync and called
    straight from the async handlers, so neither may be read inside it.
    """

    def _both():
        return app.state.capabilities_probe(), _github_notice(app)

    return await run_in_threadpool(_both)


def _render_view(
    request: Request,
    app: FastAPI,
    template_name: str,
    issues: list[Issue],
    state: DispatchState,
    active_worktrees: set[str],
    filter_options: dict,
    inflight_keys: set[str] | None = None,
    *,
    caps: LauncherCapabilities,
    refreshing: bool = False,
    mode: str = "browse",
    annotations: dict[str, IssueAnnotation] | None = None,
    triage_running: bool = False,
    triage_ai_available: bool | None = None,
    hygiene_running: bool = False,
    hygiene_snapshot: HygieneSnapshot | None = None,
    hygiene_ai_available: bool | None = None,
    github_notice: str = "",
):
    """Build the template context shared between the full page and the
    list / action bar partials. The same dict satisfies all three
    templates so a partial render can borrow whatever it needs.

    ``inflight_keys`` flags just-dispatched issues so their cards render
    in the in-flight state (spinner + greyed) on the very next paint —
    no waiting for an SSE round trip (#95).
    """
    queue_sections: dict[str, list[Issue]] = {}
    if mode == "queue":
        # Queue shares Browse's chip groups (repo/sentinel/status/effort/
        # fitness — #248 hand-test) but not its search box or sort: query
        # is ignored and order comes from queue_partition, not sort_issues.
        filtered = _filter_visible(issues, state, annotations, dimensions=True)
        pinned_q, ranked_q, scoreless_q = queue_partition(filtered, annotations or {})
        queue_sections = {
            "pinned_issues": pinned_q,
            "ranked_issues": ranked_q,
            "untriaged_issues": scoreless_q,
        }
        sorted_issues = pinned_q + ranked_q + scoreless_q
    elif mode == "triage":
        # Inbox of AI proposals (#248): repo/sentinel/status chips (the
        # shared set minus effort/fitness — those would filter on the very
        # dimensions awaiting confirmation); newest proposal first.
        # Confirmed / untriaged records are deliberately absent — the inbox
        # is "what the worker wants a human decision on".
        filtered = _filter_visible(issues, state, annotations, dimensions=False)
        ann = annotations or {}
        proposed = [
            i
            for i in filtered
            if (a := ann.get(i.key)) is not None
            and a.record is not None
            and a.record.state is TriageState.PROPOSED
        ]
        proposed.sort(key=lambda i: ann[i.key].record.proposed_at or "", reverse=True)
        queue_sections = {"proposed_issues": proposed}
        sorted_issues = proposed
    elif mode == "hygiene":
        # Findings, not issue cards (#249): the repo chip applies on the
        # finding's issue keys; issue titles resolve from the loaded list.
        sorted_issues = []
        snap = hygiene_snapshot
        queue_sections = {
            "hygiene_sections": _hygiene_sections(snap, state),
            "hygiene_last_swept": _swept_label(snap.last_swept_at) if snap else "never",
            "issues_by_key": {i.key: i for i in issues},
        }
    else:
        filtered = _filter_visible(issues, state, annotations, dimensions=True, query=state.query)
        field, descending = state.sort_params()
        sorted_issues = sort_issues(filtered, sort_by=field, descending=descending)
    ungrouped = state.ungrouped_keys()
    worktree_count = len(state.groups) + len(ungrouped)
    # Group button enable rule mirrors the server-side guard in
    # ``dispatch_group``: 2+ ungrouped selections, all from the same
    # repo. Computed here so the template stays declarative.
    ungrouped_repos = {k.rsplit("#", 1)[0] for k in ungrouped}
    can_group = len(ungrouped) >= 2 and len(ungrouped_repos) == 1
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "issues": sorted_issues,
            "all_issue_count": len(issues),
            "filter_options": filter_options,
            "active_worktrees": active_worktrees,
            "state": state,
            "ungrouped": ungrouped,
            "worktree_count": worktree_count,
            "can_group": can_group,
            "configured": True,
            "inflight_keys": inflight_keys or set(),
            "refreshing": refreshing,
            "mode": mode,
            "annotations": annotations or {},
            "triage_configured": app.state.triage_store is not None,
            "triage_running": triage_running,
            "triage_ai_available": (
                _triage_ai_available(app) if triage_ai_available is None else triage_ai_available
            ),
            "github_notice": github_notice,
            "hygiene_configured": app.state.hygiene_store is not None,
            "hygiene_running": hygiene_running,
            "hygiene_ai_available": (
                _hygiene_ai_available(app) if hygiene_ai_available is None else hygiene_ai_available
            ),
            **queue_sections,
            **_dispatch_form_context(app, caps),
        },
    )


# ----- Dispatch execution -------------------------------------------------


def _build_batches(state: DispatchState, issue_index: dict[str, Issue]) -> list[list[Issue]]:
    """Translate cookie selection state into ``dispatch_via_claude``'s
    expected ``list[list[Issue]]`` shape:

    * Each explicit group becomes one inner list (one worktree, multiple
      issues slash-joined into a single ``/worktree-create`` arg).
    * Each ungrouped selection becomes its own one-issue inner list
      (one worktree per issue). Mirrors the TUI's
      ``_build_dispatch_batches``.
    """
    batches: list[list[Issue]] = []
    for group in state.groups:
        members = [issue_index[k] for k in group if k in issue_index]
        if members:
            batches.append(members)
    for key in state.ungrouped_keys():
        if key in issue_index:
            batches.append([issue_index[key]])
    return batches


async def _publish(broker, topic: str, data: dict) -> None:
    if broker is None:
        return
    await broker.publish(Event(topic=topic, data=data, fingerprint=None))


# ----- Single-issue lookup ------------------------------------------------


def _status_picker_value(status: str | None) -> str:
    """Convert a GitHub Status value to the picker's lowercase-hyphen ID.

    Mirrors the prototype's identifiers (``todo``, ``in-progress``,
    ``done``). Statuses outside that set (``Backlog``, custom values,
    or the placeholder ``Blocked`` we'll eventually add via #76)
    collapse to ``none`` so the picker still renders without an
    active selection.
    """
    if not status:
        return "none"
    canonical = status.lower().replace(" ", "-")
    return canonical if canonical in _STATUS_VALUE_TO_LABEL else "none"


async def _resolve_issue_and_owner(app: FastAPI, key: str) -> tuple[Issue, str] | None:
    """Find an Issue by its ``owner/repo#number`` key plus the GitHub
    Project owner used to write Priority/Status fields against it.

    Returns ``None`` when the key isn't in the cached issue list. The
    owner is the one the issue's project was actually fetched under
    (``issue.project_owner``) — project numbers are owner-scoped, so
    matching config projects by number alone can pick the wrong owner
    when two owners share a number (#81). Issues from pre-#81 disk
    caches lack ``project_owner`` and keep the old number-match
    resolution; the final fallback is the repo owner.
    """
    # SWR read (#226): opening a detail sheet must not block on a TTL-expired
    # gh refetch — the stale list is the one the user just clicked in. NO
    # background kick here (review fix): the priority/status POSTs share this
    # resolver and mutate the cached Issue in place; a refresh kicked from
    # the resolve could complete after the write and overwrite it with
    # pre-edit data. Freshness converges via the container routes instead.
    issues, _needs_refresh = await _load_issues_swr(app)
    issue = next((i for i in issues if i.key == key), None)
    if issue is None:
        return None
    owner: str | None = issue.project_owner or None
    cfg = app.state.config
    if owner is None and cfg and cfg.projects and issue.project_number:
        for proj in cfg.projects:
            if proj.number == issue.project_number:
                owner = proj.owner
                break
    if owner is None:
        owner = issue.repo.split("/", 1)[0] if "/" in issue.repo else "@me"
    return issue, owner


async def _render_issue_detail(
    request: Request,
    app: FastAPI,
    issue: Issue,
    active_worktrees: set[str],
) -> HTMLResponse:
    store = app.state.triage_store
    record = None
    weights = None
    if store is not None:
        record, weights = await run_in_threadpool(
            lambda: (store.get(issue.key), load_rank_weights(store.data_dir))
        )
    confirmed = record is not None and record.state is TriageState.CONFIRMED
    return templates.TemplateResponse(
        request,
        "partials/issue_detail.html",
        {
            "issue": issue,
            "active_worktrees": active_worktrees,
            "current_status": _status_picker_value(issue.status),
            "triage_configured": store is not None,
            "record": record,
            # Score only once confirmed: a complete-but-unconfirmed record
            # (AI proposal, or a hand triage awaiting Confirm) would otherwise
            # show a rank chip it doesn't hold in the Queue yet.
            "score": rank_score(record, weights) if confirmed else None,
            "breakdown": rank_breakdown(record, weights) if confirmed else [],
        },
    )


def _parse_revisions(raw: str) -> dict[str, str]:
    """The Confirm-all button's ``revisions`` form field (Codex P2): a JSON
    object of ``issue_key → proposed_at`` for the cards it rendered. Empty
    (legacy markup) → no expectations; anything that isn't an object of
    strings is a malformed request, not a reason to confirm blind."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="revisions is not valid JSON") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise HTTPException(status_code=400, detail="revisions must map keys to proposed_at")
    return parsed


async def _proposal_is_stale(app: FastAPI, issue_key: str, proposed_at: str | None) -> bool:
    """True when the caller acted on a proposal revision the worker has
    since replaced: the stored record is still a PROPOSED one, but its
    ``proposed_at`` isn't the one the card was rendered with. No revision
    posted (legacy markup) → never stale."""
    if proposed_at is None:
        return False
    rec = await run_in_threadpool(app.state.triage_store.get, issue_key)
    return (
        rec is not None
        and rec.tombstoned_at is None
        and rec.state is TriageState.PROPOSED
        and rec.proposed_at != proposed_at
    )


def _record_for_dim(
    issue_key: str, dim: str, value: str, now: str, *, stage_pin: bool = False
) -> TriageRecord:
    """Build the single-dimension write behind a triage POST.

    Shared by the detail sheet's dimension editor (#247) and the Triage
    inbox's tap-a-chip adjust (#248) so both surfaces write exactly the
    same record shape. Pure builder — callers validate ``dim``/``value``
    against ``_TRIAGE_DIM_VALUES`` first; an unknown ``dim`` reaching here
    would fall through to the ``pin`` branch.

    ``stage_pin`` (#267, the inbox row) routes a ``pin`` tap to
    ``pin_staged`` — stored now, committed by Confirm — instead of the
    effective ``pin`` the sheet and Queue toggles write directly.
    """
    record = TriageRecord(issue_key=issue_key, updated_at=now)
    if dim == "value":
        record.value = int(value)
    elif dim == "effort":
        record.effort = Effort(value)
    elif dim == "fitness":
        record.agent_fitness = AgentFitness(value)
    elif dim == "interest":
        record.interest = value == "true"
    elif dim == "autopilot":
        record.autopilot = value == "true"
    elif stage_pin:
        record.pin_staged = value == "true"
    else:
        record.pin = value == "true"
    return record


def _group_batches_by_repo(batches: list[list[Issue]]) -> list[list[list[Issue]]]:
    """``dispatch_via_claude`` takes one repo's worth of batches at a
    time (single Claude session per call); split a mixed-repo batch
    list along repo boundaries while preserving order.
    """
    groups: dict[str, list[list[Issue]]] = {}
    for batch in batches:
        if not batch:
            continue
        repo = batch[0].repo
        groups.setdefault(repo, []).append(batch)
    return list(groups.values())


async def _run_dispatch_bg(
    batches_per_repo: list[list[list[Issue]]],
    config,
    broker,
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    capabilities_probe=None,
) -> None:
    """Async background task: one ``dispatch_via_claude`` call per repo.

    The blocking tmux work happens inside ``run_in_threadpool`` so the
    event loop stays free; SSE publish happens directly via ``await``
    since we're already on the loop. Mirrors the TUI's
    ``_execute_all_dispatches`` (which calls ``dispatch_via_claude``
    once per repo group and ignores per-repo failures so the rest of
    the dispatches still run).

    ``harness``/``model``/``effort`` (#205 Stage 1B Task 15) are the raw,
    already-validated selection from the request (``dispatch_execute``
    validates via ``resolve_and_validate_harness`` before scheduling this
    task) — forwarded straight through to ``dispatch_via_claude``, which
    re-resolves them against the config default itself (single source of
    truth for that resolution stays in ``resolve_dispatch_selection``).

    ``capabilities_probe`` (final-review fix, #205 Stage 1B) is forwarded
    straight through to ``dispatch_via_claude`` too, so its own inner
    capability gate reuses the same cached ``app.state.capabilities_probe``
    instead of re-shelling to launch.sh — one probe path, deterministic
    tests. Callers with no probe of their own (e.g. the inbox-dispatch
    surface, which has no boundary-level gate at all) can still omit it;
    ``dispatch_via_claude`` falls back to the real probe.
    """
    for batches in batches_per_repo:
        if not batches:
            continue
        repo = batches[0][0].repo
        branch = generate_branch_name(batches[0])
        # issue_keys lets the client transition the right cards via
        # data-issue-key without inferring from branch name (#95).
        issue_keys = [issue.key for batch in batches for issue in batch]
        await _publish(
            broker,
            "dispatch.started",
            {"repo": repo, "branch": branch, "issue_keys": issue_keys},
        )
        try:
            result = await run_in_threadpool(
                dispatch_via_claude,
                batches,
                config,
                harness=harness,
                model=model,
                effort=effort,
                capabilities_probe=capabilities_probe,
            )
        except Exception as exc:
            log.exception("dispatch_via_claude raised")
            await _publish(
                broker,
                "dispatch.failed",
                {"repo": repo, "branch": branch, "issue_keys": issue_keys, "error": str(exc)},
            )
            continue
        if result.success:
            # A worktree was just created — bust the detection cache so its
            # "active" badge shows on the next render, not after the TTL.
            invalidate_worktree_cache()
            await _publish(
                broker,
                "dispatch.completed",
                {
                    "repo": repo,
                    "branch": result.branch,
                    "issue_keys": issue_keys,
                    "warning": result.warning,
                },
            )
        else:
            # The SSE event flips the card to FAILED, but a failed dispatch
            # otherwise leaves no server-side trail of *why* (#153). Log it
            # alongside the log.exception above for raised errors.
            log.warning("dispatch failed for %s: %s", repo, result.error)
            await _publish(
                broker,
                "dispatch.failed",
                {
                    "repo": repo,
                    "branch": branch,
                    "issue_keys": issue_keys,
                    "error": result.error,
                },
            )


# ----- Routes -------------------------------------------------------------


def register(app: FastAPI) -> None:
    # /dispatch → /issues (#247). GET-only: bookmarks/links get the 301;
    # the two partial GETs keep a tab opened pre-deploy alive (its SSE
    # listeners refetch the container and follow the 307 transparently).
    # Old-markup POSTs 404 until reload — accepted.
    @app.get("/dispatch")
    async def dispatch_redirect():
        return RedirectResponse("/issues", status_code=301)

    @app.get("/dispatch/container")
    async def dispatch_container_redirect():
        return RedirectResponse("/issues/container", status_code=307)

    @app.get("/dispatch/list")
    async def dispatch_list_redirect():
        return RedirectResponse("/issues/list", status_code=307)

    @app.get("/dispatch/issue/{key:path}")
    async def dispatch_issue_redirect(key: str):
        # Pre-deploy tabs' cards still hx-get the old detail URL.
        return RedirectResponse(f"/issues/issue/{urlquote(key, safe='')}", status_code=307)

    @app.get("/issues", response_class=HTMLResponse)
    async def dispatch_page(request: Request, mode: str | None = None, keep: str | None = None):
        """First paint: shell + skeleton only — no gh fetch (#95).

        The skeleton's hx-trigger="load" immediately fetches
        /issues/container which renders the real issue list. Pre-#95
        this handler blocked on _load_issues, which on cold cache was
        7-8s with 4 projects (sum-of-sequential gh subprocesses).
        """
        cfg = app.state.config
        # Explicit valid ?mode= is persisted (#176 pattern); anything else
        # restores the stored pref, defaulting to Queue (spec Section 3).
        if mode in WEB_ISSUES_MODES:
            await run_in_threadpool(app.state.ui_prefs_store.set_web_issues_mode, mode)
        else:
            mode = await _resolve_mode(app, None)
        state = read_dispatch_state(request, app.state.token_manager)
        # Selection + groups are transient workflow state — the user's
        # mental model for a hard page reload is "fresh start". Filters
        # are preserved because they're closer to user preferences (the
        # TUI also keeps chip state across navigations). The cleared
        # state is written back so the next htmx swap doesn't resurrect
        # the old selection from the cookie. The Queue|Browse mode links
        # carry keep=1 (#247 review): hopping modes is mid-workflow
        # navigation, not a fresh start, so picks survive it.
        if keep != "1":
            state.clear_selection()

        configured = cfg is not None and bool(cfg.projects)
        # run_in_threadpool (Fix 7, multi-review wave): the probe shells out
        # to launch.sh; a hung/slow launcher must not stall the event loop.
        caps = await run_in_threadpool(app.state.capabilities_probe)
        # Store-only read (#248): the mode-switch Triage badge must paint with
        # the shell, and a gh fetch here would undo #95's skeleton-fast rule.
        proposed_count = await run_in_threadpool(_proposed_count, app.state.triage_store)
        hygiene_open_count = await run_in_threadpool(_hygiene_open_count, app.state.hygiene_store)
        response = templates.TemplateResponse(
            request,
            "issues.html",
            {
                "configured": configured,
                "mode": mode,
                "state": state,
                "proposed_count": proposed_count,
                "hygiene_open_count": hygiene_open_count,
                # The skeleton embeds the real (empty-state) action bar so it
                # doesn't pop in on swap (#144). Selection is cleared above, so
                # these neutral defaults render "No selections" + disabled
                # buttons — byte-identical to the post-load action bar.
                "ungrouped": [],
                "can_group": False,
                "worktree_count": 0,
                **_dispatch_form_context(app, caps),
            },
        )
        write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    @app.get("/issues/container", response_class=HTMLResponse)
    async def dispatch_container(request: Request, mode: str | None = None):
        """Container partial — re-renders filter bar + list + action bar
        from the current cache without forcing a GitHub refetch and
        without clearing selection. Used by the SSE
        ``issue.invalidated`` listener so a priority/status edit can
        update chip counts in the filter bar (a list-only swap left
        ``Need 9`` showing after a Need→Want change).
        """
        state = read_dispatch_state(request, app.state.token_manager)
        cfg = app.state.config
        resolved_mode = await _resolve_mode(app, mode)
        if cfg is None or not cfg.projects:
            # run_in_threadpool (Fix 7, multi-review wave): see dispatch_page.
            caps = await run_in_threadpool(app.state.capabilities_probe)
            return templates.TemplateResponse(
                request,
                "partials/dispatch_container.html",
                {
                    "issues": [],
                    "filter_options": {"repos": [], "efforts": [], "fitnesses": [], "statuses": []},
                    "active_worktrees": set(),
                    "state": state,
                    "ungrouped": [],
                    "worktree_count": 0,
                    "can_group": False,
                    "configured": False,
                    "mode": resolved_mode,
                    "triage_configured": app.state.triage_store is not None,
                    "hygiene_configured": app.state.hygiene_store is not None,
                    **_dispatch_form_context(app, caps),
                },
            )
        issues, needs_refresh = await _load_issues_swr(app)
        refreshing = _kick_issues_refresh(app, needs_refresh)
        annotations = await _annotate_issues(app, issues)
        triage_available = _triage_ai_available(app)
        triage_running = _maybe_kick_triage_worker(
            app, issues, annotations, available=triage_available
        )
        hygiene_snapshot = await _hygiene_snapshot(app)
        hygiene_available = _hygiene_ai_available(app)
        hygiene_running = _maybe_kick_hygiene_sweep(
            app, issues, snapshot=hygiene_snapshot, available=hygiene_available
        )
        active = await _load_active_worktrees(app)
        opts = _filter_options_with_dimensions(issues, annotations)
        pruned = _prune_stale_filters(
            state, issues, opts, fetch_failed=await _github_fetch_failed(app)
        )
        caps, notice = await _render_inputs(app)
        response = _render_view(
            request,
            app,
            "partials/dispatch_container.html",
            issues,
            state,
            active,
            opts,
            caps=caps,
            github_notice=notice,
            refreshing=refreshing,
            mode=resolved_mode,
            annotations=annotations,
            triage_running=triage_running,
            triage_ai_available=triage_available,
            hygiene_running=hygiene_running,
            hygiene_snapshot=hygiene_snapshot,
            hygiene_ai_available=hygiene_available,
        )
        if pruned:
            write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    @app.get("/issues/list", response_class=HTMLResponse)
    async def dispatch_list(request: Request, mode: str | None = None):
        """List partial — used as the htmx swap target after chip /
        search updates. Returns the issue card list only (no chrome)."""
        state = read_dispatch_state(request, app.state.token_manager)
        cfg = app.state.config
        if cfg is None or not cfg.projects:
            return templates.TemplateResponse(
                request,
                "partials/dispatch_list.html",
                {
                    "issues": [],
                    "active_worktrees": set(),
                    "state": state,
                    "configured": False,
                },
            )
        resolved_mode = await _resolve_mode(app, mode)
        issues, needs_refresh = await _load_issues_swr(app)
        _kick_issues_refresh(app, needs_refresh)
        annotations = await _annotate_issues(app, issues)
        active = await _load_active_worktrees(app)
        opts = _filter_options_with_dimensions(issues, annotations)
        pruned = _prune_stale_filters(
            state, issues, opts, fetch_failed=await _github_fetch_failed(app)
        )
        caps, notice = await _render_inputs(app)
        response = _render_view(
            request,
            app,
            "partials/dispatch_list.html",
            issues,
            state,
            active,
            opts,
            caps=caps,
            github_notice=notice,
            mode=resolved_mode,
            annotations=annotations,
        )
        if pruned:
            write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    # ---- mutating endpoints --------------------------------------------

    async def _list_response(request: Request, state: DispatchState, mode: str) -> HTMLResponse:
        """Render the list partial, bake the cookie onto it. Used by
        chip-toggle and search routes."""
        issues, needs_refresh = await _load_issues_swr(app)
        _kick_issues_refresh(app, needs_refresh)
        annotations = await _annotate_issues(app, issues)
        active = await _load_active_worktrees(app)
        opts = _filter_options_with_dimensions(issues, annotations)
        _prune_stale_filters(state, issues, opts, fetch_failed=await _github_fetch_failed(app))
        caps, notice = await _render_inputs(app)
        response = _render_view(
            request,
            app,
            "partials/dispatch_list.html",
            issues,
            state,
            active,
            opts,
            caps=caps,
            github_notice=notice,
            mode=mode,
            annotations=annotations,
        )
        write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    async def _container_response(
        request: Request, state: DispatchState, mode: str, *, force: bool = False
    ) -> HTMLResponse:
        """Render the full dispatch container — used by /group,
        /ungroup, /issues, /refresh because they affect markup in
        multiple slots (card group labels, action bar count, etc.) and
        a single outerHTML swap is simpler than orchestrating OOB
        swaps. ``force=True`` bypasses cache TTL for /refresh — the
        explicit refresh button stays blocking on purpose: returning the
        same stale list instantly would make the button feel broken."""
        if force:
            issues = await _load_issues(app, force=True)
            refreshing = False
        else:
            issues, needs_refresh = await _load_issues_swr(app)
            refreshing = _kick_issues_refresh(app, needs_refresh)
        annotations = await _annotate_issues(app, issues)
        triage_available = _triage_ai_available(app)
        triage_running = _maybe_kick_triage_worker(
            app, issues, annotations, available=triage_available
        )
        hygiene_snapshot = await _hygiene_snapshot(app)
        hygiene_available = _hygiene_ai_available(app)
        hygiene_running = _maybe_kick_hygiene_sweep(
            app, issues, snapshot=hygiene_snapshot, available=hygiene_available
        )
        active = await _load_active_worktrees(app)
        opts = _filter_options_with_dimensions(issues, annotations)
        _prune_stale_filters(state, issues, opts, fetch_failed=await _github_fetch_failed(app))
        caps, notice = await _render_inputs(app)
        response = _render_view(
            request,
            app,
            "partials/dispatch_container.html",
            issues,
            state,
            active,
            opts,
            caps=caps,
            github_notice=notice,
            refreshing=refreshing,
            mode=mode,
            annotations=annotations,
            triage_running=triage_running,
            triage_ai_available=triage_available,
            hygiene_running=hygiene_running,
            hygiene_snapshot=hygiene_snapshot,
            hygiene_ai_available=hygiene_available,
        )
        write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    @app.post("/issues/filters/toggle", response_class=HTMLResponse)
    async def dispatch_filter_toggle(
        request: Request, type: str, value: str, mode: str | None = None
    ):
        # Toggle returns the whole container so the clicked chip's
        # ``filter-chip--active`` class updates alongside the filtered list. A
        # list-only swap left the chip looking inactive even after the
        # filter took effect, since the chip's class lives in the
        # filter bar (outside ``#issue-list``).
        state = read_dispatch_state(request, app.state.token_manager)
        state.toggle_filter(type, value)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/search", response_class=HTMLResponse)
    async def dispatch_search(request: Request, q: str = Form(default=""), mode: str | None = None):
        state = read_dispatch_state(request, app.state.token_manager)
        state.query = q.strip()
        return await _list_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/sort", response_class=HTMLResponse)
    async def dispatch_sort(request: Request, value: str, mode: str | None = None):
        # One button = one POST; the server decides activate-vs-flip
        # (re-clicking the active date button flips its own direction, #156).
        # Return the whole container so the clicked button's active
        # class + arrow re-render alongside the reordered list. Unknown values
        # are ignored (mirrors ``toggle_filter``) so a junk param can't 500.
        state = read_dispatch_state(request, app.state.token_manager)
        if value in SORT_MODES:
            state.apply_sort(value)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/select", response_class=HTMLResponse)
    async def dispatch_select(request: Request, key: str, mode: str | None = None):
        # Return the whole container so the toggled card's
        # ``issue-card--selected`` class re-renders alongside the action bar's
        # ``N selected`` count. An action-bar-only swap left the card
        # check missing its green fill until the next list re-render.
        state = read_dispatch_state(request, app.state.token_manager)
        state.toggle_selection(key)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/group", response_class=HTMLResponse)
    async def dispatch_group(request: Request, mode: str | None = None):
        """Bundle ungrouped selections into one batch.

        Validation mirrors the TUI's ``DispatchScreen.group_selected``:
        require 2+ ungrouped selections, all from the same repo.
        Failures are silent (the cookie just doesn't gain a group);
        the action bar's Group button is what tells the user whether
        the action is even available, so re-rendering the same state
        is the right "rejection" UX here.
        """
        state = read_dispatch_state(request, app.state.token_manager)
        ungrouped = state.ungrouped_keys()
        if len(ungrouped) >= 2:
            # All selected ungrouped keys must share a repo. Issue keys
            # are ``owner/repo#number``; everything before the first
            # ``#`` is the repo.
            repos = {k.rsplit("#", 1)[0] for k in ungrouped}
            if len(repos) == 1:
                state.group_selected(ungrouped)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/ungroup", response_class=HTMLResponse)
    async def dispatch_ungroup(request: Request, index: int, mode: str | None = None):
        state = read_dispatch_state(request, app.state.token_manager)
        state.ungroup(index)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/dispatch", response_class=HTMLResponse)
    async def dispatch_execute(
        request: Request,
        background_tasks: BackgroundTasks,
        harness: str = Form(default=""),
        model: str = Form(default=""),
        effort: str = Form(default=""),
        mode: str | None = None,
    ):
        """Build batches from the cookie's selection state, schedule a
        background task to actually start the Claude sessions, clear
        the selection (so the user can immediately stage the next
        batch), and return the refreshed container with the just-
        dispatched cards rendered in the in-flight state (#95) so the
        user gets immediate visual feedback. The background task's
        SSE dispatch.completed flips them to the terminal state.

        ``harness``/``model``/``effort`` (#205 Stage 1B Task 15): blank form
        fields become ``None``; the resolved selection is validated —
        harness/effort against the allowlists, and a non-claude harness
        against the installed launcher's advertised capabilities — before
        anything is scheduled, so a bad request 400s instead of silently
        launching claude anyway.
        """
        harness_val = harness or None
        model_val = model or None
        effort_val = effort or None
        # One threadpool hop for both blocking reads: see _render_inputs.
        caps, notice = await _render_inputs(app)
        try:
            resolve_and_validate_harness(
                app.state.config,
                caps,
                harness_val,
                model_val,
                effort_val,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        state = read_dispatch_state(request, app.state.token_manager)
        resolved_mode = await _resolve_mode(app, mode)
        if not state.selected:
            return await _container_response(request, state, resolved_mode)

        # Resolve issue keys → Issue objects against the current cache.
        issues = await _load_issues(app)
        index = {issue.key: issue for issue in issues}
        # #179: a selected key missing from the list while GitHub is failing
        # is unknown, not closed — dispatching the rest and clearing the
        # selection would silently drop it. Defer the whole dispatch and keep
        # the selection; the container's stale banner says why.
        unresolved = [k for k in state.selected if k not in index]
        if unresolved and await _github_fetch_failed(app):
            eventlog.emit(
                "warn",
                "dispatch deferred: github fetch failed",
                {"unresolved": unresolved},
            )
            return await _container_response(request, state, resolved_mode)
        batches = _build_batches(state, index)
        per_repo = _group_batches_by_repo(batches)
        inflight_keys = {
            issue.key for repo_batches in per_repo for batch in repo_batches for issue in batch
        }
        if per_repo:
            background_tasks.add_task(
                _run_dispatch_bg,
                per_repo,
                app.state.config,
                app.state.broker,
                harness_val,
                model_val,
                effort_val,
                capabilities_probe=app.state.capabilities_probe,
            )

        state.clear_selection()
        annotations = await _annotate_issues(app, issues)
        active = await _load_active_worktrees(app)
        opts = _filter_options_with_dimensions(issues, annotations)
        _prune_stale_filters(state, issues, opts, fetch_failed=await _github_fetch_failed(app))
        response = _render_view(
            request,
            app,
            "partials/dispatch_container.html",
            issues,
            state,
            active,
            opts,
            inflight_keys=inflight_keys,
            caps=caps,
            github_notice=notice,
            mode=resolved_mode,
            annotations=annotations,
        )
        write_dispatch_state(response, state, app.state.token_manager, request)
        return response

    # ---- triage inbox (#248) -------------------------------------------

    # Registered ahead of the ``{key:path}`` routes below so the literal
    # /badge segment can never be swallowed by a greedy key match.
    @app.get("/issues/triage/badge", response_class=HTMLResponse)
    async def triage_badge(request: Request):
        """Just the mode-switch count pill — the SSE ``triage.proposed`` /
        ``issue.invalidated`` listeners swap this outerHTML so the badge
        tracks the record set without a full page nav."""
        count = await run_in_threadpool(_proposed_count, app.state.triage_store)
        return templates.TemplateResponse(
            request, "partials/triage_badge.html", {"proposed_count": count}
        )

    @app.get("/issues/hygiene/badge", response_class=HTMLResponse)
    async def hygiene_badge(request: Request):
        count = await run_in_threadpool(_hygiene_open_count, app.state.hygiene_store)
        return templates.TemplateResponse(
            request, "partials/hygiene_badge.html", {"hygiene_open_count": count}
        )

    def _require_hygiene_store() -> None:
        if app.state.hygiene_store is None:
            raise HTTPException(status_code=404, detail="hygiene store not configured")

    @app.post("/issues/hygiene/run", response_class=HTMLResponse)
    async def hygiene_run_now(request: Request, mode: str | None = None):
        """Manual sweep (#249); ``force=True`` clears the failure cooldown and
        bypasses the 24 h stamp — a deliberate tap is never a silent no-op."""
        _require_hygiene_store()
        issues, _ = await _load_issues_swr(app)
        if _hygiene_ai_available(app):
            app.state.hygiene_refresher.kick(lambda: _run_sweep_background(app, issues), force=True)
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    async def _open_finding(fid: str):
        _require_hygiene_store()
        finding = await run_in_threadpool(app.state.hygiene_store.get, fid)
        if finding is None or finding.status is not FindingStatus.OPEN:
            raise HTTPException(status_code=404, detail="finding not found or already decided")
        return finding

    async def _hygiene_plan(finding, now: str, issues: list[Issue] | None = None):
        if issues is None:
            issues, _ = await _load_issues_swr(app)
        issues_by_key = {i.key: i for i in issues}
        # plan_steps reads the triage store (a file) — off the loop.
        return await run_in_threadpool(
            plan_steps,
            finding,
            issues_by_key=issues_by_key,
            github_cache=app.state.github_cache,
            triage_store=app.state.triage_store,
            now=now,
            prior_execution=finding.execution,
        )

    async def _run_claimed_plan(
        fid: str, finding, now: str, issues: list[Issue], steps=None
    ) -> dict:
        """The execute half of Accept, for a finding whose claim is held:
        plan → record the labels → execute with live progress → mark
        accepted / failed → invalidate its issues. Anything raised between
        the claim and the log release re-releases the claim (review round 2:
        the card must never stay "Executing…" forever). Shared by the
        per-finding Accept and the per-repo Add all (#273)."""
        store = app.state.hygiene_store
        prior = finding.execution
        try:
            if steps is None:
                steps = await _hygiene_plan(finding, now, issues)
            plan = [s.label for s in steps]
            await run_in_threadpool(store.record_progress, fid, plan=plan, done=[], now=now)
            execution = await run_in_threadpool(
                execute_hygiene,
                finding,
                steps,
                now=now,
                prior_execution=prior,
                on_step=lambda log, facts: store.record_progress(
                    fid, plan=plan, done=log, now=now, facts=facts
                ),
            )
        except Exception as exc:
            failed = {
                **execution_facts(prior),
                "ok": False,
                "at": now,
                "plan": ["Prepare the plan"],
                "steps": [
                    {
                        "label": "Prepare the plan",
                        "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                ],
            }
            await run_in_threadpool(store.mark_failed, fid, execution=failed)
            raise
        if execution["ok"]:
            written = await run_in_threadpool(
                store.mark_accepted, fid, now=now, execution=execution
            )
        else:
            written = await run_in_threadpool(store.mark_failed, fid, execution=execution)
        if not written:
            # The steps already ran; a finding left claimed shows as
            # "Executing…" until the file is readable again.
            eventlog.emit("warn", "hygiene write failed", {"finding": fid, "op": "accept"})
        for key in finding.issue_keys:
            await _publish(app.state.broker, "issue.invalidated", {"key": key})
        return execution

    async def _refresh_boards_after_add(findings_and_logs: list[tuple]) -> None:
        """A landed item-add (#273) is invisible until its board is refetched
        — the SWR cache would show the issue only after the TTL. One forced
        fetch per distinct board, never per issue."""
        boards: set[tuple[int, str]] = set()
        for finding, execution in findings_and_logs:
            a = finding.action
            if a.get("kind") == "add_to_board" and execution.get("item_id"):
                boards.add((int(a["project_number"]), str(a["project_owner"])))
        for number, owner in sorted(boards):
            try:
                await run_in_threadpool(app.state.github_cache.get_issues, number, owner, True)
            except Exception:
                log.warning(
                    "board refetch after item-add failed for %s/%s", owner, number, exc_info=True
                )

    def _refetch_failed_log(prior: dict | None, now: str, exc: Exception) -> dict:
        label = "Confirm the current issue state on GitHub"
        return {
            **execution_facts(prior),
            "ok": False,
            "at": now,
            # This attempt's own plan — never the previous attempt's
            # (round 4: the final checklist would pad with its steps).
            "plan": [label],
            "steps": [{"label": label, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}],
        }

    @app.post("/issues/hygiene/offboard/add-all", response_class=HTMLResponse)
    async def hygiene_offboard_add_all(
        request: Request, mode: str | None = None, repo: str = Form(...)
    ):
        """Add every open off-board issue of one repo to its board (#273),
        best-effort per finding: ONE forced refetch, then each finding is
        planned, claimed only when the plan has something to do (review
        round 1: an issue added by hand must not be stored as a failed
        attempt — the next sweep resolves it) and run; a failure on one is
        stored on that card and the loop moves on. Registered before the
        ``{fid:path}`` routes."""
        _require_hygiene_store()
        store = app.state.hygiene_store
        state = read_dispatch_state(request, app.state.token_manager)
        resolved_mode = await _resolve_mode(app, mode)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snapshot = await _hygiene_snapshot(app)
        targets = [
            f
            for f in (snapshot.findings if snapshot else ())
            if f.type is FindingType.OFFBOARD
            and f.status is FindingStatus.OPEN
            and f.issue_keys[0].rsplit("#", 1)[0] == repo
        ]
        if targets:
            try:
                issues = await _load_issues_fresh(app, strict=True)
            except Exception as exc:
                # The refetch failed for everyone: claim + release each so
                # the cards say so, exactly as the per-finding Accept does.
                for f in targets:
                    if not await run_in_threadpool(
                        store.claim, f.id, now=now, expected_found_at=f.found_at
                    ):
                        continue
                    failed = _refetch_failed_log(f.execution, now, exc)
                    await run_in_threadpool(store.mark_failed, f.id, execution=failed)
                eventlog.emit(
                    "warn",
                    "hygiene write failed",
                    {"op": "add-all-refetch", "repo": repo, "error": str(exc)},
                )
                return await _container_response(request, state, resolved_mode)
            done = []
            for stale in targets:
                try:
                    # Re-read right before claiming (review round 2): a sweep
                    # finishing mid-loop refreshes found_at on every unclaimed
                    # off-board finding, and a claim pinned to the snapshot's
                    # revision would refuse the rest silently. The action
                    # itself (key + board) is stable across a refresh.
                    f = await run_in_threadpool(store.get, stale.id)
                    if f is None or f.status is not FindingStatus.OPEN:
                        continue
                    steps = await _hygiene_plan(f, now, issues)
                    if not any(s.actionable for s in steps):
                        continue
                    if not await run_in_threadpool(
                        store.claim, f.id, now=now, expected_found_at=f.found_at
                    ):
                        continue
                    done.append((f, await _run_claimed_plan(f.id, f, now, issues, steps)))
                except Exception:
                    # One finding's failure (planning included — round 5)
                    # must not skip the board refresh the others earned.
                    log.warning("add-all: %s failed", stale.id, exc_info=True)
            await _refresh_boards_after_add(done)
        return await _container_response(request, state, resolved_mode)

    @app.get("/issues/hygiene/{fid:path}/preview", response_class=HTMLResponse)
    async def hygiene_preview(request: Request, fid: str):
        """Accept step 1 (#249): the exact steps Execute will run, from the
        same ``plan_steps`` that execution uses."""
        finding = await _open_finding(fid)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        steps = await _hygiene_plan(finding, now)
        return templates.TemplateResponse(
            request,
            "partials/hygiene_preview.html",
            {"finding": finding, "steps": steps, "rows": preview_rows(steps)},
        )

    @app.get("/issues/hygiene/{fid:path}/progress", response_class=HTMLResponse)
    async def hygiene_progress(request: Request, fid: str, final: int = 0):
        """The Execute checklist (hand-test round). While the claim is held
        the rows come from the live ``plan`` / ``progress`` the accept route
        records; ``?final=1`` (loaded when the POST returns) renders the
        released step log, for an accepted finding too. Before the claim
        lands every row is pending — never "done"."""
        _require_hygiene_store()
        finding = await run_in_threadpool(app.state.hygiene_store.get, fid)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        rows = _progress_rows(finding, final=bool(final))
        if not rows and not final:
            # Nothing to show yet (claim not landed / plan not recorded) or a
            # late poll after release: 204 = htmx swaps nothing, so the
            # checklist on screen is never blanked (review round 3).
            return Response(status_code=204)
        return templates.TemplateResponse(request, "partials/hygiene_progress.html", {"rows": rows})

    @app.post("/issues/hygiene/{fid:path}/accept", response_class=HTMLResponse)
    async def hygiene_accept(
        request: Request, fid: str, mode: str | None = None, found_at: str | None = Form(None)
    ):
        """Accept step 2: execute sequentially off the loop, stop at the first
        failure, store the step log either way. A stale ``found_at`` (the sweep
        refreshed the finding after the preview was painted) re-renders
        without acting — "execute what I saw"."""
        finding = await _open_finding(fid)
        state = read_dispatch_state(request, app.state.token_manager)
        resolved_mode = await _resolve_mode(app, mode)
        # X-Hygiene-Ran tells the modal whether anything executed: a stale
        # found_at or a refused claim re-renders the container and the
        # checklist must not present an older attempt's log as this one's.
        if found_at is not None and found_at != finding.found_at:
            response = await _container_response(request, state, resolved_mode)
            response.headers["X-Hygiene-Ran"] = "0"
            return response
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store = app.state.hygiene_store
        # Claim under the flock BEFORE any GitHub write (review P1): a double
        # tap or a second tab re-renders instead of replaying the plan. The
        # card shows "Executing…" while the claim is held.
        claimed = await run_in_threadpool(store.claim, fid, now=now, expected_found_at=found_at)
        if not claimed:
            response = await _container_response(request, state, resolved_mode)
            response.headers["X-Hygiene-Ran"] = "0"
            return response
        # Plan against a forced refetch, never the SWR cache (Codex round
        # 3): the tab may have sat open for hours. A failed refetch is a
        # failed attempt — the card says so — not a guess.
        try:
            issues = await _load_issues_fresh(app, strict=True)
        except Exception as exc:
            await run_in_threadpool(
                store.mark_failed, fid, execution=_refetch_failed_log(finding.execution, now, exc)
            )
            eventlog.emit(
                "warn",
                "hygiene write failed",
                {"finding": fid, "op": "accept-refetch", "error": str(exc)},
            )
            response = await _container_response(request, state, resolved_mode)
            response.headers["X-Hygiene-Ran"] = "1"
            return response
        execution = await _run_claimed_plan(fid, finding, now, issues)
        await _refresh_boards_after_add([(finding, execution)])
        response = await _container_response(request, state, resolved_mode)
        response.headers["X-Hygiene-Ran"] = "1"
        return response

    @app.post("/issues/hygiene/{fid:path}/remove", response_class=HTMLResponse)
    async def hygiene_remove_child(
        request: Request,
        fid: str,
        mode: str | None = None,
        key: str = Form(...),
        found_at: str | None = Form(None),
    ):
        """Drop one issue from a cluster (hand-test round). The store applies
        the same revision guard as dismiss; a stale or refused edit simply
        re-renders."""
        _require_hygiene_store()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        finding = await run_in_threadpool(app.state.hygiene_store.get, fid)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        await run_in_threadpool(
            app.state.hygiene_store.remove_child,
            fid,
            key,
            now=now,
            expected_found_at=found_at,
        )
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/hygiene/{fid:path}/dismiss", response_class=HTMLResponse)
    async def hygiene_dismiss(
        request: Request, fid: str, mode: str | None = None, found_at: str | None = Form(None)
    ):
        """Permanent fingerprint suppression; the store refuses a stale
        revision (re-render, no write)."""
        _require_hygiene_store()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        finding = await run_in_threadpool(app.state.hygiene_store.get, fid)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        dismissed = await run_in_threadpool(
            app.state.hygiene_store.dismiss, fid, now=now, expected_found_at=found_at
        )
        if dismissed:
            # The badge sits outside the swapped container (smoke-test find):
            # publish so open tabs refetch it, exactly as accept does.
            for key in finding.issue_keys:
                await _publish(app.state.broker, "issue.invalidated", {"key": key})
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    def _require_store() -> None:
        if app.state.triage_store is None:
            raise HTTPException(status_code=404, detail="triage store not configured")

    # The two fixed-path POSTs are registered BEFORE the ``{key:path}``
    # pair below for the same reason as /badge above: rendered issue keys
    # carry a raw "/" (Jinja's urlencode leaves it alone), so the key
    # routes must use the greedy path converter and would otherwise be
    # able to swallow a literal segment.
    @app.post("/issues/triage/confirm-all", response_class=HTMLResponse)
    async def triage_confirm_all(
        request: Request,
        mode: str | None = None,
        keys: str = Form(""),
        revisions: str = Form(""),
    ):
        """Confirm the PROPOSED issues the user actually saw.

        ``keys`` is the comma-separated list of proposal keys the button
        rendered with (``hx-vals`` in ``issues_triage_bar.html``); the
        confirm set is that list INTERSECTED with what is PROPOSED and
        repo-filtered right now. Without the intersection a proposal the
        worker landed behind an open tab between paint and tap would be
        confirmed unseen — the count in the label is the safeguard that
        replaces a blocking ``hx-confirm``, and it can only speak for the
        cards that were on screen.

        An empty ``keys`` (markup from before this shipped, still open in a
        tab) falls back to the old confirm-everything-visible behaviour.

        ``revisions`` (Codex P2) is a JSON object mapping each rendered key
        to the ``proposed_at`` its card showed. The intersection above only
        catches a proposal that APPEARED after the paint; a proposal the
        worker REPLACED after the paint (the issue's body changed, so it was
        re-targeted) keeps its key and state, and only the revision tells it
        apart. The store skips a key whose stored revision differs, so the
        user never confirms dimensions they didn't look at; the re-rendered
        container then shows the replacement card.
        """
        _require_store()
        expected = _parse_revisions(revisions)
        state = read_dispatch_state(request, app.state.token_manager)
        issues, _ = await _load_issues_swr(app)
        annotations = await _annotate_issues(app, issues)
        visible = _filter_visible(issues, state, annotations, dimensions=False)
        posted = {part for part in (p.strip() for p in keys.split(",")) if part}
        confirm_keys = []
        for issue in visible:
            ann = annotations.get(issue.key)
            if ann is not None and ann.record is not None:
                if ann.record.state is TriageState.PROPOSED:
                    if not posted or issue.key in posted:
                        confirm_keys.append(issue.key)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        flipped = await run_in_threadpool(
            app.state.triage_store.confirm_many, confirm_keys, now=now, expected=expected
        )
        if flipped:
            # No ``key``: ``confirm_many`` skips incomplete/tombstoned records,
            # so any single key we could name might be one it didn't touch. The
            # SSE listener refetches the container regardless of the payload.
            await _publish(app.state.broker, "issue.invalidated", {"count": flipped})
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/triage/run", response_class=HTMLResponse)
    async def triage_run_now(request: Request, mode: str | None = None):
        """Manual worker kick (#248); coalesces onto an in-flight run.

        The rendered container picks the "AI triage running…" pill up from
        ``_container_response``'s own ``_maybe_kick_triage_worker`` call,
        which sees this kick's in-flight run.

        ``force=True``: an explicit tap must not be a silent no-op inside
        the automatic trigger's failure cooldown.
        """
        _require_store()
        issues, _ = await _load_issues_swr(app)
        if _triage_ai_available(app):
            app.state.triage_refresher.kick(lambda: _run_triage_background(app, issues), force=True)
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/triage/{key:path}/confirm", response_class=HTMLResponse)
    async def triage_confirm(
        request: Request,
        key: str,
        mode: str | None = None,
        proposed_at: str | None = Form(None),
    ):
        """One-tap confirm (#248): state → CONFIRMED, provenance stays "ai"
        so the audit trail reads "AI proposed, human confirmed". A record
        that isn't a complete proposal 400s rather than silently no-opping.

        ``proposed_at`` is the revision the card was rendered with (Codex
        P2): if the worker replaced the proposal after that paint, the store
        refuses the flip and the container is re-rendered so the user sees
        the replacement card instead of having approved it blind. A stale
        confirm is a 200, not an error — the user did nothing wrong.
        """
        _require_store()
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stored = await run_in_threadpool(
            app.state.triage_store.confirm, issue.key, now=now, expected_proposed_at=proposed_at
        )
        state = read_dispatch_state(request, app.state.token_manager)
        if stored is None:
            if await _proposal_is_stale(app, issue.key, proposed_at):
                return await _container_response(request, state, await _resolve_mode(app, mode))
            raise HTTPException(status_code=400, detail="record is not a complete proposal")
        await _publish(app.state.broker, "issue.invalidated", {"key": issue.key})
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/triage/{key:path}/adjust", response_class=HTMLResponse)
    async def triage_adjust(
        request: Request,
        key: str,
        dim: str = Form(...),
        value: str = Form(...),
        mode: str | None = None,
        proposed_at: str | None = Form(None),
    ):
        """Tap-a-chip adjust (#248): the same human write as the detail
        editor, re-rendering the container. The record stays PROPOSED —
        the card remains in the inbox with the new chip active until
        Confirm (hand-test follow-up: a tap is a correction, not an
        approval). Interest and pin ride the same route (#267): a flag tap
        is stored on the still-PROPOSED record — tap again to clear. Interest
        is a plain human write; pin goes to ``pin_staged`` (the Queue keeps
        reading ``pin``) and Confirm / Confirm-all is what commits it.

        Carries the card's ``proposed_at`` like ``confirm`` does (Codex P2):
        on a card the worker replaced after the paint, the tap would land a
        value chosen against dims the user never saw. Stale → no write,
        re-render.
        """
        _require_store()
        allowed = _TRIAGE_DIM_VALUES.get(dim) if dim in _TRIAGE_INBOX_DIMS else None
        if allowed is None or value not in allowed:
            raise HTTPException(status_code=400, detail="invalid triage dimension or value")
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        state = read_dispatch_state(request, app.state.token_manager)
        if await _proposal_is_stale(app, issue.key, proposed_at):
            return await _container_response(request, state, await _resolve_mode(app, mode))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # A pin tap that lands after the row was confirmed elsewhere
        # (Confirm-all, another tab) passes the revision guard above — the
        # record isn't PROPOSED any more, so it isn't "stale". The store
        # demotes that staged write to a direct pin under its own lock
        # (``_merge``: ``pin_staged`` only lives on a PROPOSED record), so
        # no read-then-write race here can orphan it (#267 review).
        record = _record_for_dim(issue.key, dim, value, now, stage_pin=True)
        await _write_triage_record(issue, record, op="adjust", dim=dim)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    # ---- single-issue detail / inline edits ----------------------------

    @app.get("/issues/issue/{key:path}", response_class=HTMLResponse)
    async def dispatch_issue_detail(request: Request, key: str):
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        active = await _load_active_worktrees(app)
        return await _render_issue_detail(request, app, issue, active)

    async def _write_triage_record(issue, record, *, op: str, dim: str | None = None) -> bool:
        """Shared human-write contract for the pin + dimension routes:
        upsert, and on a skipped write (unreadable/corrupt repo file)
        emit an eventlog warn and do NOT publish — never render a
        dropped write as success (#247 review). Returns True on success."""
        stored = await run_in_threadpool(app.state.triage_store.upsert, record, actor="human")
        if stored is None:
            extra = {"op": op, "issue": issue.key}
            if dim is not None:
                extra["dim"] = dim
            eventlog.emit("warn", "triage write failed", extra)
            return False
        await _publish(app.state.broker, "issue.invalidated", {"key": issue.key})
        return True

    @app.post("/issues/issue/{key:path}/pin", response_class=HTMLResponse)
    async def dispatch_issue_pin(request: Request, key: str, on: str, mode: str | None = None):
        """Toggle the manual pin (#247) — floats the issue above all
        scores in the Queue. Human-only; a pin-only upsert supplies no
        dimensions, so the record's state must not flip to confirmed.
        The button renders its target value (on=true/false) at render
        time, so the handler is a plain write, no read-modify-write."""
        _require_store()
        if on not in ("true", "false"):
            raise HTTPException(status_code=400, detail="invalid pin value")
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        record = TriageRecord(
            issue_key=issue.key,
            pin=(on == "true"),
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        await _write_triage_record(issue, record, op="pin")
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode))

    @app.post("/issues/issue/{key:path}/triage", response_class=HTMLResponse)
    async def dispatch_issue_triage(
        request: Request,
        key: str,
        dim: str = Form(...),
        value: str = Form(...),
    ):
        """Dimension editor write route (#247): one POST per tap, form
        fields ``dim`` + ``value``. Human actor — a write never changes the
        record's state (Confirm is the explicit ``/confirm`` POST below,
        #248 hand-test); interest/pin are flags. Replaces the GH priority
        picker: the local store is authoritative, the GH Priority field
        goes stale until the Phase-5 mirror."""
        _require_store()
        allowed = _TRIAGE_DIM_VALUES.get(dim)
        if allowed is None or value not in allowed:
            raise HTTPException(status_code=400, detail="invalid triage dimension or value")
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = _record_for_dim(issue.key, dim, value, now)
        await _write_triage_record(issue, record, op="triage", dim=dim)
        active = await _load_active_worktrees(app)
        return await _render_issue_detail(request, app, issue, active)

    @app.post("/issues/issue/{key:path}/confirm", response_class=HTMLResponse)
    async def dispatch_issue_confirm(
        request: Request,
        key: str,
        proposed_at: str | None = Form(None),
    ):
        """Detail-sheet Confirm (#248 hand-test): the same store flip as the
        inbox's ``/issues/triage/{key}/confirm`` but re-renders the sheet,
        so Browse/Queue users confirm from the popup they edited in. Works
        on a hand-triaged UNTRIAGED record too — the store only needs all
        three dims. Stale ``proposed_at`` → no write, re-render (the sheet
        then shows the replacement); incomplete → 400."""
        _require_store()
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stored = await run_in_threadpool(
            app.state.triage_store.confirm, issue.key, now=now, expected_proposed_at=proposed_at
        )
        if stored is None and not await _proposal_is_stale(app, issue.key, proposed_at):
            raise HTTPException(status_code=400, detail="record is not a complete triage")
        if stored is not None:
            await _publish(app.state.broker, "issue.invalidated", {"key": issue.key})
        active = await _load_active_worktrees(app)
        return await _render_issue_detail(request, app, issue, active)

    @app.post("/issues/issue/{key:path}/status", response_class=HTMLResponse)
    async def dispatch_issue_set_status(
        request: Request,
        key: str,
        value: str = Form(...),
    ):
        if value not in _STATUS_VALUE_TO_LABEL:
            raise HTTPException(status_code=400, detail="invalid status value")
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, owner = resolved
        label = _STATUS_VALUE_TO_LABEL[value]
        # See the priority endpoint above — same optimistic-write rule.
        ok = await run_in_threadpool(app.state.github_cache.set_status, issue, owner, label)
        if ok:
            issue.status = label
            await _publish(
                app.state.broker,
                "issue.invalidated",
                {"key": issue.key},
            )
        else:
            log.warning("set_status failed for %s -> %r", issue.key, label)
            eventlog.emit("warn", "github write failed", {"op": "set_status", "issue": issue.key})
        active = await _load_active_worktrees(app)
        return await _render_issue_detail(request, app, issue, active)

    @app.post(
        "/issues/issue/{key:path}/close-not-planned",
        response_class=HTMLResponse,
    )
    async def dispatch_issue_close_not_planned(request: Request, key: str):
        """Close the underlying GitHub issue with the ``not planned``
        reason — distinct from ``d``/Done, which only flips the project
        Status field. On success, prime the closed-keys cache so the
        list drops it immediately and SSE-invalidate other tabs.
        """
        resolved = await _resolve_issue_and_owner(app, key)
        if resolved is None:
            raise HTTPException(status_code=404, detail="issue not found")
        issue, _owner = resolved
        ok = await run_in_threadpool(close_issue_not_planned, issue.repo, issue.number)
        if ok:
            # Off the loop: mark_closed now takes a flock and rewrites the
            # durable issue index (#255), so inline it would stall every
            # request behind a competing writer.
            await run_in_threadpool(app.state.github_cache.mark_closed, issue.repo, issue.number)
            await _publish(
                app.state.broker,
                "issue.invalidated",
                {"key": issue.key},
            )
        else:
            log.warning("close_issue_not_planned failed for %s", issue.key)
            eventlog.emit(
                "warn", "github write failed", {"op": "close_not_planned", "issue": issue.key}
            )
        active = await _load_active_worktrees(app)
        return await _render_issue_detail(request, app, issue, active)

    @app.post("/issues/refresh", response_class=HTMLResponse)
    async def dispatch_refresh(request: Request, mode: str | None = None):
        """Bypass the GitHubCache TTL — same as the TUI's ``r`` keybind.

        Threads ``force=True`` through ``_container_response`` →
        ``_load_issues`` so each project is fetched exactly once. The
        prior shape ran an explicit force-fetch loop then re-rendered,
        which re-fetched everything a second time and (worse) paid the
        10 s gh timeout twice on a slow ``gh``.
        """
        state = read_dispatch_state(request, app.state.token_manager)
        return await _container_response(request, state, await _resolve_mode(app, mode), force=True)
