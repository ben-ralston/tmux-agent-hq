# agent_hq/hygiene_worker.py
"""Hygiene sweep worker (#249, Phase 4 of #245).

Stale detection is pure code; duplicates, sentinel clusters and value
mismatches come from ONE cross-repo ``claude -p`` call over the whole open
backlog. Findings are written through ``HygieneStore.record_sweep`` — the
store owns suppression (dismissed ids never come back) and resolution.
The subprocess boundary is ``triage_worker.Runner`` — injected, never real
in tests.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import eventlog
from .config import Config, RepoConfig, TriageConfig
from .filters import parse_ts
from .github import open_issue_rows
from .hygiene_store import Finding, FindingType, HygieneStore, load_hygiene_thresholds
from .models import Issue
from .triage_store import TriageRecord, TriageState, TriageStore
from .triage_worker import MAX_REASONING_CHARS, Runner, TriageInvokeError, invoke_claude

MAX_BODY_CHARS = 300
# Off-board findings (#273) fetch a title/state per issue over REST (0 GraphQL
# points); past this many in one sweep the rest are reported title-less.
MAX_OFFBOARD_LOOKUPS = 100
# An issue created within this long before its board snapshot (or after it)
# is not judged off-board (review round 3): the board is a snapshot — up to a
# TTL old from the CLI, or a failed refetch inside the TTL — while the index
# is synced now, and GitHub's own Auto-add takes a moment after filing.
OFFBOARD_MIN_AGE_S = 600
MIN_MISMATCH_DELTA = 2  # the rubric's "clearly wrong" bar; the parser enforces it

HYGIENE_RUBRIC = (
    "You are doing a hygiene pass over the open GitHub issues of a personal "
    "AI-automation ecosystem owned by one person. Every open issue across every "
    "repository is listed below with its key (owner/repo#number), title, labels, "
    "whether it was filed by an automated sentinel, its current triage value "
    "(1-5, or unscored) and the start of its body.\n"
    "Report three kinds of finding, each with ONE sentence of reasoning under 200 "
    "characters:\n"
    "- duplicates: two issues that ask for the same thing. `keep` is the OLDER "
    "issue (lower number, or the one with more discussion), `close` is the other. "
    "Near-identical titles count; issues that merely touch the same area do not.\n"
    "- clusters: two or more SENTINEL-filed issues in the SAME repository that share "
    "one root cause and should be tracked by a single umbrella issue. Propose a short "
    "umbrella_title. Never mix repositories, never include non-sentinel issues.\n"
    "- mismatches: an issue whose current value is clearly wrong by at least 2 points. "
    "The canonical case is a security, data-loss or broken-automation issue scored 1-2. "
    "Do not report unscored issues, and do not nudge values by one point.\n"
    "Only use the issue keys listed above, exactly as written. Return empty lists "
    "when nothing qualifies — an empty result is a good result."
)

_REASONING = {"type": "string", "minLength": 1, "maxLength": MAX_REASONING_CHARS}

HYGIENE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["duplicates", "clusters", "mismatches"],
    "properties": {
        "duplicates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["keep", "close", "reasoning"],
                "properties": {
                    "keep": {"type": "string"},
                    "close": {"type": "string"},
                    "reasoning": _REASONING,
                },
            },
        },
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repo", "umbrella_title", "children", "reasoning"],
                "properties": {
                    "repo": {"type": "string"},
                    "umbrella_title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "children": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    "reasoning": _REASONING,
                },
            },
        },
        "mismatches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "proposed_value", "reasoning"],
                "properties": {
                    "key": {"type": "string"},
                    "proposed_value": {"type": "integer", "minimum": 1, "maximum": 5},
                    "reasoning": _REASONING,
                },
            },
        },
    },
}


class HygieneParseError(Exception):
    """Model output violated the contract — the WHOLE batch is rejected."""


# ---- stale (pure code) ----


def _parse_iso(value: str | None) -> datetime | None:
    ts = parse_ts(value)
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)


def detect_stale(
    issues: list[Issue],
    records: dict[str, TriageRecord],
    thresholds: dict,
    *,
    now: datetime,
) -> list[Finding]:
    """Untouched for >= stale_days AND value <= stale_max_value. Unknown
    dates, unscored or tombstoned records are skipped — a rule can't fire
    on data it doesn't have."""
    cutoff = now - timedelta(days=int(thresholds["stale_days"]))
    max_value = int(thresholds["stale_max_value"])
    out: list[Finding] = []
    for issue in issues:
        rec = records.get(issue.key)
        if rec is None or rec.tombstoned_at is not None or rec.value is None:
            continue
        if rec.state is not TriageState.CONFIRMED:
            # A PROPOSED value is "awaiting a human" (review round 3): a close
            # must never rest on a number nobody approved.
            continue
        if rec.value > max_value:
            continue
        updated = _parse_iso(issue.updated_at)
        if updated is None or updated > cutoff:
            continue
        date = updated.date().isoformat()
        out.append(
            Finding.new(
                FindingType.STALE,
                [issue.key],
                action={
                    "kind": "close_stale",
                    "close": issue.key,
                    "last_activity": issue.updated_at,
                    "value": rec.value,
                },
                reasoning=f"No activity since {date} and value {rec.value} ≤ {max_value}.",
            )
        )
    return out


# ---- off-board (pure code, #273) ----


@dataclass(frozen=True)
class BoardTarget:
    """The project board a repo's issues belong on, per projects.yaml."""

    repo: str
    project_name: str
    project_number: int
    project_owner: str


@dataclass
class OffboardInputs:
    """What the detector diffs: the index's open issues per checked repo, the
    membership of each repo's OWN target board (Done included — a Done-but-
    open issue IS on the board; an issue parked on a different configured
    board is off its repo's — round 7), and each repo's target. A repo
    absent from ``open_by_repo`` was not checked (no ``project:``, cold board
    cache, unseeded index) and the store leaves its findings alone."""

    open_by_repo: dict[str, set[str]] = field(default_factory=dict)
    board_by_repo: dict[str, set[str]] = field(default_factory=dict)
    targets: dict[str, BoardTarget] = field(default_factory=dict)
    # Board items with no Status — an item-add whose Todo write failed; the
    # store keeps such a finding's card alive only while this holds it.
    statusless_keys: set[str] = field(default_factory=set)


Describe = Callable[[str, int], dict | None]


def collect_offboard_inputs(cfg: Config, cache) -> OffboardInputs:
    """Board membership from the cache's never-fetching read (the sweep
    already refetched the boards) and open issues from the durable index
    (a debounced REST sync). Only repos with a ``project:`` are checked."""
    projects = {p.name: p for p in cfg.projects}
    inputs = OffboardInputs()
    # Read BEFORE the cached reads (review round 3): serving a TTL-fresh entry
    # clears its failure record, so a failed forced refetch inside the TTL
    # would otherwise pass as a verified board.
    failed = _failed_keys(cache)
    boards: dict[tuple[int, str], tuple[set[str], float] | None] = {}
    cases: dict[tuple[int, str], dict[str, str]] = {}  # lower repo -> board's spelling
    for repo, rc in cfg.repos.items():
        proj = projects.get(rc.project) if rc.project else None
        if proj is None:
            continue
        board_key = (proj.number, proj.owner)
        if board_key not in boards:
            issues, fresh = cache.get_issues_cached(proj.number, proj.owner)
            cases[board_key] = {i.repo.lower(): i.repo for i in issues or []}
            if issues is not None and (not fresh or f"{proj.owner}/{proj.number}" in failed):
                # Last-good data behind a failed or budget-skipped refresh
                # (hand-test 2026-09-14, review round 1): an issue filed
                # since would read as off-board. The sweep just refetched the
                # boards, so a TTL-expired entry is one that did not refresh.
                issues = None
            if issues is None:
                boards[board_key] = None
            else:
                statusless = {i.key for i in issues if not i.status}
                # A closed status-less item is no Todo write anyone still has
                # to finish (round 7): it must not keep a partial finding alive.
                inputs.statusless_keys |= statusless - _closed_keys(cache, statusless)
                boards[board_key] = ({i.key for i in issues}, _snapshot_time(cache, proj))
        board = boards[board_key]
        if board is None:
            continue  # never fetched / refresh failed: membership unknown, do not guess
        keys, fetched_at = board
        # Canonical spelling (round 10): every other consumer — the index,
        # closed keys, the chip filter, the fresh list — keys on the board's
        # ``Issue.repo`` (GitHub's nameWithOwner); a projects.yaml key that
        # differs in case would otherwise seed a second index entry and file
        # findings no chip can match. An empty board keeps the config's.
        repo = cases[board_key].get(repo.lower(), repo)
        inputs.board_by_repo[repo] = set(keys)
        try:
            rows = open_issue_rows(repo)
        except Exception as exc:  # noqa: BLE001 — review round 2: the collector
            # runs outside run_sweep's never-raises boundary, and a raise here
            # would leave the bar on "Sweeping…"; one repo's index is skipped.
            eventlog.emit(
                "warn", "github issue index sync failed", {"repo": repo, "error": str(exc)}
            )
            continue
        if rows is None:
            continue  # index never seeded for this repo
        cutoff = fetched_at - OFFBOARD_MIN_AGE_S
        inputs.open_by_repo[repo] = {
            key
            for key, created in rows.items()
            # Newer than the snapshot (minus the Auto-add grace): can't be
            # judged against it. Unknown dates are judged — the index has
            # a date for every row it synced itself.
            if (ts := parse_ts(created)) is None or ts <= cutoff
        }
        inputs.targets[repo] = BoardTarget(
            repo=repo,
            project_name=proj.name,
            project_number=proj.number,
            project_owner=proj.owner,
        )
    return inputs


def _failed_keys(cache) -> set[str]:
    lookup = getattr(cache, "failed_keys", None)
    return set(lookup()) if callable(lookup) else set()


def _closed_keys(cache, keys: set[str]) -> set[str]:
    """Keys the cache's closed-keys index lists (never fetches)."""
    lookup = getattr(cache, "get_closed_keys_cached", None)
    if not callable(lookup) or not keys:
        return set()
    closed, _fresh = lookup({k.rsplit("#", 1)[0] for k in keys})
    return set(closed) & keys


def _own_board_keys(inputs: OffboardInputs) -> set[str]:
    """Keys sitting on their OWN repo's target board — what "landed" means."""
    return {
        key
        for repo, keys in inputs.board_by_repo.items()
        for key in keys
        if key.rsplit("#", 1)[0].lower() == repo.lower()
    }


def _snapshot_time(cache, proj) -> float:
    """When the board entry was fetched; "now" when the cache can't say."""
    lookup = getattr(cache, "cached_at", None)
    stamp = lookup(proj.number, proj.owner) if callable(lookup) else None
    return float(stamp) if stamp is not None else time.time()


def detect_offboard(inputs: OffboardInputs, *, describe: Describe | None = None) -> list[Finding]:
    """Open issues (index) not on their board (cache) → one finding each.
    ``describe`` (REST title/state) is optional: a closed answer drops the
    candidate (the index can lag a close), a failed one reports it
    title-less. Capped so a huge drift can't turn one sweep into hundreds
    of calls."""
    out: list[Finding] = []
    lookups = 0
    for repo in sorted(inputs.open_by_repo):
        target = inputs.targets.get(repo)
        if target is None:
            continue
        # Case-insensitive (round 9): the projects.yaml key and GitHub's
        # nameWithOwner may differ in case, and an exact miss would report
        # the whole repo off-board — and Add all would reset every Status.
        on_board = {k.lower() for k in inputs.board_by_repo.get(repo, set())}
        candidates = sorted(
            (k for k in inputs.open_by_repo[repo] if k.lower() not in on_board),
            key=lambda k: int(k.rsplit("#", 1)[1]),
        )
        for key in candidates:
            title = None
            if describe is not None and lookups < MAX_OFFBOARD_LOOKUPS:
                lookups += 1
                summary = describe(repo, int(key.rsplit("#", 1)[1]))
                if summary is not None:
                    if summary.get("state") in ("closed", "missing"):
                        continue
                    title = summary.get("title") or None
            out.append(
                Finding.new(
                    FindingType.OFFBOARD,
                    [key],
                    action={
                        "kind": "add_to_board",
                        "key": key,
                        "project_number": target.project_number,
                        "project_owner": target.project_owner,
                        "project_name": target.project_name,
                        "title": title,
                    },
                    reasoning=(
                        f"Open in {repo} but not an item on the {target.project_name} board "
                        "— invisible to Queue, Triage and dispatch."
                    ),
                )
            )
    return out


# ---- prompt ----


def build_hygiene_prompt(
    issues: list[Issue],
    records: dict[str, TriageRecord],
    repos_cfg: dict[str, RepoConfig],
) -> str:
    by_repo: dict[str, list[Issue]] = {}
    for issue in issues:
        by_repo.setdefault(issue.repo, []).append(issue)
    parts = [HYGIENE_RUBRIC, "", f"Open issues: {len(issues)} across {len(by_repo)} repositories."]
    for repo in sorted(by_repo):
        rc = repos_cfg.get(repo)
        parts += ["", f"Repository: {repo}"]
        if rc is not None and rc.description:
            parts.append(f"Repository description: {rc.description}")
        for issue in sorted(by_repo[repo], key=lambda i: i.number):
            rec = records.get(issue.key)
            value = "unscored" if rec is None or rec.value is None else str(rec.value)
            parts.append(f"### {issue.key}: {issue.title}")
            if issue.labels:
                parts.append(f"labels: {', '.join(issue.labels)}")
            parts.append(f"sentinel: {'yes' if issue.is_sentinel else 'no'}")
            parts.append(f"value: {value}")
            body = (issue.body or "").strip()
            if body:
                parts.append(
                    body[:MAX_BODY_CHARS] + (" …[truncated]" if len(body) > MAX_BODY_CHARS else "")
                )
            else:
                parts.append("(no body)")
    parts += ["", "Only use the issue keys listed above. Return JSON matching the schema."]
    return "\n".join(parts)


# ---- strict parser ----


def _reasoning(raw: object, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HygieneParseError(f"{what}: missing reasoning")
    clean = raw.strip()
    if len(clean) > MAX_REASONING_CHARS:
        raise HygieneParseError(f"{what}: reasoning over {MAX_REASONING_CHARS} chars")
    return clean


def _key(raw: object, issues_by_key: dict[str, Issue], what: str) -> str:
    if not isinstance(raw, str) or raw not in issues_by_key:
        raise HygieneParseError(f"{what}: unknown issue key {raw!r}")
    return raw


def _list(structured: dict, name: str) -> list:
    raw = structured.get(name)
    if not isinstance(raw, list):
        raise HygieneParseError(f"missing or non-list {name}")
    return raw


def parse_findings(
    structured: object,
    *,
    issues_by_key: dict[str, Issue],
    records: dict[str, TriageRecord],
) -> list[Finding]:
    """Strict: any violation rejects the whole batch (never partial writes).
    Empty lists are a valid, good result."""
    if not isinstance(structured, dict):
        raise HygieneParseError("output is not an object")
    out: list[Finding] = []
    seen: set[str] = set()

    clustered: set[str] = set()

    def add(f: Finding) -> None:
        if f.id in seen:
            raise HygieneParseError(f"duplicate finding {f.id}")
        if f.type is FindingType.CLUSTER:
            # Two clusters sharing a child would insert-then-supersede each
            # other inside one record_sweep (review round 3).
            if clustered & set(f.issue_keys):
                raise HygieneParseError(f"{f.id}: clusters overlap on a child")
            clustered.update(f.issue_keys)
        seen.add(f.id)
        out.append(f)

    for i, raw in enumerate(_list(structured, "duplicates")):
        what = f"duplicates[{i}]"
        if not isinstance(raw, dict):
            raise HygieneParseError(f"{what}: not an object")
        keep = _key(raw.get("keep"), issues_by_key, what)
        close = _key(raw.get("close"), issues_by_key, what)
        if keep == close:
            raise HygieneParseError(f"{what}: keep and close are the same issue")
        add(
            Finding.new(
                FindingType.DUPLICATE,
                [keep, close],
                action={"kind": "close_as_duplicate", "close": close, "of": keep},
                reasoning=_reasoning(raw.get("reasoning"), what),
            )
        )

    for i, raw in enumerate(_list(structured, "clusters")):
        what = f"clusters[{i}]"
        if not isinstance(raw, dict):
            raise HygieneParseError(f"{what}: not an object")
        children_raw = raw.get("children")
        if not isinstance(children_raw, list) or len(set(children_raw)) < 2:
            raise HygieneParseError(f"{what}: a cluster needs at least 2 distinct children")
        children = [_key(c, issues_by_key, what) for c in children_raw]
        repo = raw.get("repo")
        repos = {issues_by_key[c].repo for c in children}
        if len(repos) != 1 or repo not in repos:
            raise HygieneParseError(f"{what}: children must all belong to repo {repo!r}")
        for c in children:
            if not issues_by_key[c].is_sentinel:
                raise HygieneParseError(f"{what}: child {c} is not a sentinel issue")
        title = raw.get("umbrella_title")
        if not isinstance(title, str) or not title.strip():
            raise HygieneParseError(f"{what}: missing umbrella_title")
        add(
            Finding.new(
                FindingType.CLUSTER,
                children,
                action={
                    "kind": "merge_cluster",
                    "repo": repo,
                    "umbrella_title": title.strip(),
                    "children": sorted(set(children)),
                },
                reasoning=_reasoning(raw.get("reasoning"), what),
            )
        )

    for i, raw in enumerate(_list(structured, "mismatches")):
        what = f"mismatches[{i}]"
        if not isinstance(raw, dict):
            raise HygieneParseError(f"{what}: not an object")
        key = _key(raw.get("key"), issues_by_key, what)
        proposed = raw.get("proposed_value")
        if isinstance(proposed, bool) or not isinstance(proposed, int) or not 1 <= proposed <= 5:
            raise HygieneParseError(f"{what}: proposed_value must be an integer 1-5")
        rec = records.get(key)
        current = rec.value if rec is not None else None
        if current is None:
            raise HygieneParseError(f"{what}: {key} is unscored — mismatches need a current value")
        if abs(proposed - current) < MIN_MISMATCH_DELTA:
            raise HygieneParseError(
                f"{what}: proposed_value must differ from the current value by at least "
                f"{MIN_MISMATCH_DELTA}"
            )
        add(
            Finding.new(
                FindingType.MISMATCH,
                [key],
                action={"kind": "bump_value", "key": key, "current": current, "proposed": proposed},
                reasoning=_reasoning(raw.get("reasoning"), what),
            )
        )
    return out


# ---- one sweep ----


@dataclass
class SweepReport:
    stale: int = 0
    duplicates: int = 0
    clusters: int = 0
    mismatches: int = 0
    offboard: int = 0
    written: int = 0  # inserted + refreshed
    resolved: int = 0
    error: str | None = None
    dry_run: bool = False
    # Nothing to sweep at all (empty open set, no board checked): nothing was
    # written or stamped. The web trigger treats it like a failure for its
    # cooldown, so a cold cache cannot re-kick a forced refetch on every load.
    skipped: bool = False
    findings: list[Finding] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.findings)


def _count(report: SweepReport, findings: list[Finding]) -> None:
    report.findings = list(findings)
    for f in findings:
        if f.type is FindingType.STALE:
            report.stale += 1
        elif f.type is FindingType.DUPLICATE:
            report.duplicates += 1
        elif f.type is FindingType.CLUSTER:
            report.clusters += 1
        elif f.type is FindingType.OFFBOARD:
            report.offboard += 1
        else:
            report.mismatches += 1


def run_sweep(
    issues: list[Issue],
    triage_store: TriageStore,
    hygiene_store: HygieneStore,
    triage_cfg: TriageConfig,
    repos_cfg: dict[str, RepoConfig],
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
    now: datetime | None = None,
    offboard: OffboardInputs | None = None,
    describe: Describe | None = None,
) -> SweepReport:
    """One sweep: stale (code) → off-board (code, #273, when ``offboard``
    inputs are given) → prompt → invoke → parse (retry once with the parser
    error appended) → record_sweep. Never raises.

    On an invoke or parse failure the AI-derived findings are simply absent:
    the code-detected stale findings are still written (review round 2 — a
    claude outage must not drop what needed no model), the premise-gone
    resolve still runs off the real open set, ``last_swept_at`` IS stamped
    so the web's 24 h auto-trigger doesn't re-fire a paid call on every page
    load, and ``report.error`` still reports the failure (the web trigger's
    cooldown). "Run now" bypasses the stamp. Dry run writes nothing at all,
    stamp included.
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    model = triage_cfg.hygiene_model or triage_cfg.ai_model
    report = SweepReport(dry_run=dry_run)
    records = {r.issue_key: r for r in triage_store.all_records(include_tombstoned=True)}
    issues_by_key = {i.key: i for i in issues}
    thresholds = load_hygiene_thresholds(hygiene_store.data_dir)
    findings: list[Finding] = detect_stale(issues, records, thresholds, now=now)
    if offboard is not None:
        findings += detect_offboard(offboard, describe=describe)

    ai_findings: list[Finding] | None = [] if not issues else None
    if issues:
        prompt = build_hygiene_prompt(issues, records, repos_cfg)
        attempt_prompt = prompt
        for attempt in (1, 2):
            try:
                structured = invoke_claude(
                    attempt_prompt,
                    model=model,
                    timeout_s=triage_cfg.hygiene_timeout_s,
                    cwd=hygiene_store.data_dir,
                    runner=runner,
                    schema=HYGIENE_OUTPUT_SCHEMA,
                )
                ai_findings = parse_findings(
                    structured, issues_by_key=issues_by_key, records=records
                )
                report.error = None  # a successful retry clears attempt 1's parse error
                break
            except TriageInvokeError as exc:
                report.error = f"invoke: {exc}"
                eventlog.emit(
                    "warn",
                    "hygiene sweep invoke failed",
                    {"error": str(exc)},
                    min_interval_s=900,
                )
                break
            except HygieneParseError as exc:
                report.error = f"parse: {exc}"
                if attempt == 1:
                    attempt_prompt = (
                        prompt
                        + "\n\nYour previous output was rejected: "
                        + str(exc)
                        + "\nReturn ONLY JSON matching the schema, using only the listed keys."
                    )
                else:
                    eventlog.emit(
                        "error",
                        "hygiene sweep output rejected",
                        {"error": str(exc), "issues": len(issues)},
                        min_interval_s=900,
                    )

    if not issues and not (offboard is not None and offboard.open_by_repo):
        # Nothing to sweep (cold cache / failed fetch — review round): do
        # nothing. record_sweep must never see an empty open set (it would
        # read as "every issue closed" and resolve the inbox), and stamping
        # would silently disable the automatic sweep for 24 h (round 3) — the
        # web trigger skips an empty list, so this is only reachable from the
        # CLI, where "0 open issues" is the honest report. Off-board inputs
        # are the exception (#273 review round 1): a freshly configured board
        # with nothing on it IS the headline case, the detector read real
        # boards, and every resolve rule is scoped to the repos it saw.
        report.skipped = True
        return report

    if ai_findings is None:
        ai_findings = []  # AI failed: write what code found; report.error stays set
    findings += ai_findings
    _count(report, findings)
    if dry_run:
        return report
    sweep_id = uuid.uuid4().hex[:12]
    try:
        write = hygiene_store.record_sweep(
            findings,
            now=now_iso,
            sweep_id=sweep_id,
            model=model,
            open_keys=set(issues_by_key),
            offboard_repos=set(offboard.open_by_repo) if offboard is not None else None,
            offboard_statusless=offboard.statusless_keys if offboard is not None else None,
            offboard_board_keys=_own_board_keys(offboard) if offboard is not None else None,
        )
    except OSError as exc:
        report.error = f"write: {exc}"
        eventlog.emit("warn", "hygiene write failed", {"op": "record_sweep", "error": str(exc)})
    else:
        report.written = write.inserted + write.refreshed
        report.resolved = write.resolved
    eventlog.emit(
        "info",
        "hygiene sweep run",
        {
            "issues": len(issues),
            "stale": report.stale,
            "duplicates": report.duplicates,
            "clusters": report.clusters,
            "mismatches": report.mismatches,
            "offboard": report.offboard,
            "written": report.written,
            "resolved": report.resolved,
            "error": report.error,
            "dry_run": dry_run,
        },
    )
    return report
