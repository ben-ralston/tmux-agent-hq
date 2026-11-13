"""Issue ↔ TriageRecord join layer for the /issues hub (#247).

Pure functions over ``list[Issue]`` + a ``TriageStore``. Lives outside
``web/`` so the TUI can reuse annotation/rank sorting later (spec Phase 5),
mirroring how ``filters.py`` is shared. ``filters.filter_issues`` stays pure
over Issue fields; dimension (effort/fitness) filtering happens here on the
annotated join instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from agent_hq.filters import _default_key
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import (
    TriageRecord,
    TriageState,
    TriageStore,
    rank_breakdown,
)

_EFFORT_ORDER = {"s": 0, "m": 1, "l": 2, "none": 3}
_FITNESS_ORDER = {"solo": 0, "pair": 1, "needs_you": 2, "none": 3}
_FITNESS_LABEL = {"solo": "solo", "pair": "pair", "needs_you": "needs you"}


def merge_issue_lists(fetched: Iterable[list[Issue]]) -> list[Issue]:
    """Flatten per-project lists: drop Done, de-dup by issue key.

    Shared so every surface that loads the boards sees the same set — the web
    routes and ``agent-hq triage run`` both seed/tombstone triage records off
    this list, and a divergence makes them fight (#248).
    """
    all_issues: list[Issue] = []
    seen: set[str] = set()
    for issues in fetched:
        for issue in issues:
            if issue.status and issue.status.lower() == "done":
                continue
            if issue.key in seen:
                continue
            all_issues.append(issue)
            seen.add(issue.key)
    return all_issues


@dataclass
class IssueAnnotation:
    """Per-issue triage view: record + derived rank/filter identities."""

    record: TriageRecord | None = None
    score: int | None = None
    breakdown: list[tuple[str, int]] = field(default_factory=list)
    pinned: bool = False
    effort_id: str = "none"
    fitness_id: str = "none"
    # #302: opted in to the nightly autopilot (``TriageRecord.autopilot``).
    autopilot: bool = False


def annotate(
    issues: list[Issue],
    store: TriageStore,
    weights: dict | None = None,
    *,
    records: list[TriageRecord] | None = None,
) -> dict[str, IssueAnnotation]:
    """Join issues to their triage records, keyed by ``Issue.key``.

    ``Issue.key`` and ``TriageRecord.issue_key`` share the
    "owner/repo#number" format, so this is a plain dict lookup.
    ``records`` lets a caller that already scanned the store (the seeding
    pass) share its snapshot instead of re-globbing every repo file.
    """
    if records is None:
        records = store.all_records(include_tombstoned=True)
    by_key = {rec.issue_key: rec for rec in records}
    out: dict[str, IssueAnnotation] = {}
    for issue in issues:
        rec = by_key.get(issue.key)
        if rec is None:
            out[issue.key] = IssueAnnotation()
            continue
        # rank_score is sum(rank_breakdown) — compute the parts once.
        parts = rank_breakdown(rec, weights)
        out[issue.key] = IssueAnnotation(
            record=rec,
            score=sum(p for _, p in parts) if parts else None,
            breakdown=parts,
            pinned=bool(rec.pin),
            effort_id=rec.effort.value.lower() if rec.effort else "none",
            fitness_id=rec.agent_fitness.value if rec.agent_fitness else "none",
            autopilot=bool(rec.autopilot),
        )
    return out


def queue_partition(
    issues: list[Issue], annotations: dict[str, IssueAnnotation]
) -> tuple[list[Issue], list[Issue], list[Issue]]:
    """(pinned, ranked, scoreless) for the Queue mode.

    Ranked needs a score AND a CONFIRMED record. Score presence alone
    decides nothing in either direction: a human who confirmed a single
    dimension has a CONFIRMED record with no score and must land in the
    untriaged section rather than vanish, while a fully-dimensioned AI
    PROPOSED record scores but must not reorder the "what next" list
    until a human confirms it (spec: AI influences rank only after
    confirmation — the suggestion stays visible in the untriaged section).

    ``pinned`` reads the record's effective ``pin`` regardless of state — a
    human's Queue/Browse pin floats immediately, PROPOSED or not, and an AI
    proposal landing on a pinned issue never moves it. What the Triage inbox
    row stages before Confirm lives in ``pin_staged`` (#267), which
    ``TriageStore.confirm`` folds into ``pin``; it is invisible here.
    """
    pinned: list[Issue] = []
    ranked: list[Issue] = []
    scoreless: list[Issue] = []
    for issue in issues:
        ann = annotations.get(issue.key) or IssueAnnotation()
        if ann.pinned:
            pinned.append(issue)
        elif (
            ann.score is not None
            and ann.record is not None
            and ann.record.state is TriageState.CONFIRMED
        ):
            ranked.append(issue)
        else:
            scoreless.append(issue)

    def _score_key(issue: Issue) -> tuple:
        ann = annotations.get(issue.key) or IssueAnnotation()
        return (ann.score is None, -(ann.score or 0), *_default_key(issue))

    pinned.sort(key=_score_key)
    ranked.sort(key=_score_key)
    scoreless.sort(key=_default_key)
    return pinned, ranked, scoreless


def filter_by_dimensions(
    issues: list[Issue],
    annotations: dict[str, IssueAnnotation],
    *,
    efforts: list[str] | None = None,
    fitnesses: list[str] | None = None,
    autopilots: list[str] | None = None,
) -> list[Issue]:
    """OR within a dimension, AND across; empty/None = no constraint.

    Unset dimensions match id ``"none"`` (mirrors ``filters._status_id``)
    so "no effort yet" is filterable with a single chip. The autopilot
    group (#302) has one chip, ``"on"``: flagged issues only.
    """
    effort_filter = set(efforts) if efforts else None
    fitness_filter = set(fitnesses) if fitnesses else None
    autopilot_only = bool(autopilots) and "on" in autopilots
    out: list[Issue] = []
    for issue in issues:
        ann = annotations.get(issue.key) or IssueAnnotation()
        if effort_filter is not None and ann.effort_id not in effort_filter:
            continue
        if fitness_filter is not None and ann.fitness_id not in fitness_filter:
            continue
        if autopilot_only and not ann.autopilot:
            continue
        out.append(issue)
    return out


def dimension_filter_options(
    issues: list[Issue], annotations: dict[str, IssueAnnotation]
) -> dict[str, list[dict]]:
    """Effort/fitness chip groups: ``{"id", "label", "count"}`` dicts.

    Only non-empty buckets are emitted (#40 rule); the "none" bucket sorts
    last, labeled ``—`` like the status group's.
    """
    effort_counts: dict[str, int] = {}
    fitness_counts: dict[str, int] = {}
    for issue in issues:
        ann = annotations.get(issue.key) or IssueAnnotation()
        effort_counts[ann.effort_id] = effort_counts.get(ann.effort_id, 0) + 1
        fitness_counts[ann.fitness_id] = fitness_counts.get(ann.fitness_id, 0) + 1
    efforts = [
        {"id": eid, "label": "—" if eid == "none" else eid.upper(), "count": count}
        for eid, count in sorted(effort_counts.items(), key=lambda kv: _EFFORT_ORDER.get(kv[0], 99))
    ]
    fitnesses = [
        {"id": fid, "label": _FITNESS_LABEL.get(fid, "—"), "count": count}
        for fid, count in sorted(
            fitness_counts.items(), key=lambda kv: _FITNESS_ORDER.get(kv[0], 99)
        )
    ]
    flagged = sum(1 for i in issues if (annotations.get(i.key) or IssueAnnotation()).autopilot)
    autopilots = [{"id": "on", "label": "⏵ Autopilot", "count": flagged}] if flagged else []
    return {"efforts": efforts, "fitnesses": fitnesses, "autopilots": autopilots}


def issue_is_triaged(record: TriageRecord | None, issue: Issue, triage_configured: bool) -> bool:
    """One definition of "this issue has been triaged" for badges/counts.

    With a store: the record is human-confirmed (#247; the explicit
    Confirm — never a dimension edit — is what flips it, #248 hand-test).
    Storeless installs fall back to the pre-#247 rule (a GH priority is
    set) so the sentinel badge stays clearable there.
    """
    if record is not None and record.state is TriageState.CONFIRMED:
        return True
    return not triage_configured and bool(issue.priority.value)


def priority_hint_for(priority: Priority) -> str | None:
    """Old Need/Want/Idea label → seeding hint; NO_PRI carries no hint."""
    return priority.value or None


def ensure_triage_records(
    store: TriageStore,
    issues: list[Issue],
    *,
    now: str,
    records: list[TriageRecord] | None = None,
) -> int:
    """Idempotent auto-seeding pass (#247): every open issue gets a record.

    Returns the number of records WRITTEN (seeds + tombstones + revives) —
    a non-zero return tells a caller holding a pre-pass ``records``
    snapshot that it went stale and must be re-read before rendering.

    Read-only diff first; locked writes only for repos that actually need
    them, so the steady-state container render costs zero flocks. Only
    repos present in ``issues`` are tombstone-checked — a repo missing
    entirely from the list is indistinguishable from a fetch failure.

    Caveat (accepted in the plan): callers pass the post-Done-filter issue
    list, so a Done-but-open issue is tombstoned like a closed one. It
    leaves every view anyway, and revive-on-reopen restores it. The same
    tolerance covers a partially-failed fetch (gh error cached as an empty
    project, #179/#254): affected records tombstone for one TTL and revive
    on the next good fetch — churn, not data loss.
    """
    by_repo: dict[str, list[Issue]] = {}
    for issue in issues:
        by_repo.setdefault(issue.repo, []).append(issue)

    if records is None:
        records = store.all_records(include_tombstoned=True)
    records_by_repo: dict[str, list[TriageRecord]] = {}
    for rec in records:
        records_by_repo.setdefault(rec.issue_key.rsplit("#", 1)[0], []).append(rec)

    written = 0
    for repo, repo_issues in by_repo.items():
        open_numbers = {i.number for i in repo_issues}
        existing = records_by_repo.get(repo, [])
        existing_keys = {rec.issue_key for rec in existing}
        missing = [
            (i.number, priority_hint_for(i.priority))
            for i in repo_issues
            if i.key not in existing_keys
        ]
        if missing:
            written += store.seed_missing(repo, missing, now=now)
        needs_tombstone_pass = False
        for rec in existing:
            try:
                number = int(rec.issue_key.rsplit("#", 1)[1])
            except (ValueError, IndexError):
                continue
            if (number in open_numbers) == (rec.tombstoned_at is not None):
                needs_tombstone_pass = True
                break
        if needs_tombstone_pass:
            written += store.apply_open_issue_set(repo, open_numbers, now=now)
    return written
