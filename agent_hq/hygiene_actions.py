"""Accepting a hygiene finding (#249): the same ``plan_steps`` output renders
the preview modal and drives execution, so what the user reads is exactly
what runs. Execution is sequential and stops at the first failure; the step
log is stored on the finding (``execution``) either way.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import eventlog
from .filters import parse_ts
from .github import (
    GHError,
    add_to_project,
    close_issue_not_planned,
    comment_issue,
    create_issue,
    fetch_issue_summary,
    set_field_value,
)
from .hygiene_store import EXECUTION_BOOKKEEPING, Finding
from .models import Issue
from .triage_store import TriageRecord

_ISSUE_NUMBER_RE = re.compile(r"/issues/(\d+)\s*$")
_SUFFIX = "agent-hq hygiene sweep"


@dataclass
class StepResult:
    ok: bool
    detail: str = ""
    # Facts a later retry needs (e.g. {"umbrella": 900}); ``execute`` merges
    # them into the stored execution log so a re-run after a partial failure
    # can reuse what already landed on GitHub instead of creating it twice.
    data: dict | None = None


@dataclass
class Step:
    label: str
    run: Callable[[], StepResult]
    # False for a step that touches nothing (a gap, an already-closed child):
    # the preview disables Execute when no step in the plan is actionable.
    actionable: bool = True


def split_key(key: str) -> tuple[str, int]:
    repo, num = key.rsplit("#", 1)
    return repo, int(num)


def _short(key: str) -> str:
    repo, num = split_key(key)
    return f"{repo.split('/')[-1]}#{num}"


def _gap(key: str) -> list[Step]:
    return [
        Step(
            label=f"{_short(key)} is no longer open — nothing to do",
            run=lambda: StepResult(False, f"{key} is not in the current open issue set"),
            actionable=False,
        )
    ]


def _stale_recommendation(reason: str) -> list[Step]:
    """The finding no longer describes the issue (Codex round 2): offer
    nothing rather than act on the state the sweep saw."""
    return [
        Step(
            label=f"{reason} — nothing to do",
            run=lambda: StepResult(False, reason),
            actionable=False,
        )
    ]


def _skip(key: str) -> Step:
    return Step(
        label=f"{_short(key)} is already closed — skipping",
        run=lambda: StepResult(True, "already closed"),
        actionable=False,
    )


# How long after a failed attempt's ``at`` an issue's ``updated_at`` is still
# read as that attempt's own comment rather than someone else's activity.
OWN_ACTIVITY_GRACE_S = 15 * 60


def _comment_step(key: str, body: str, *, prior: dict | None = None, now: str) -> Step:
    """Comment, recording ``commented_at`` on success so a retry after a
    failed close does not post twice (Codex rounds 3-4). The fact is the
    comment's own timestamp: later attempts carry it unchanged, so the
    activity anchor never advances without a new comment."""
    repo, num = split_key(key)
    if prior and prior.get("commented_at"):
        return Step(
            label=f"Comment on {_short(key)} already posted by the previous attempt",
            run=lambda: StepResult(True, "already commented"),
            actionable=False,
        )

    def run() -> StepResult:
        ok = comment_issue(repo, num, body)
        if not ok:
            return StepResult(False, "gh issue comment failed")
        return StepResult(True, "", {"commented_at": now})

    return Step(label=f"Comment on {_short(key)}: {body}", run=run)


def _known_closed(github_cache, key: str) -> bool:
    """True when the cache's closed-keys index lists ``key`` (never fetches)."""
    lookup = getattr(github_cache, "get_closed_keys_cached", None)
    if lookup is None:
        return False
    closed, _fresh = lookup([split_key(key)[0]])
    return key in closed


def _close_step(key: str, github_cache) -> Step:
    repo, num = split_key(key)

    def run() -> StepResult:
        if not close_issue_not_planned(repo, num):
            return StepResult(False, "gh issue close failed")
        if github_cache is not None:
            github_cache.mark_closed(repo, num)
        return StepResult(True)

    return Step(label=f"Close {_short(key)} as not planned", run=run)


def plan_steps(
    finding: Finding,
    *,
    issues_by_key: dict[str, Issue],
    github_cache,
    triage_store,
    now: str,
    prior_execution: dict | None = None,
) -> list[Step]:
    """The steps Accept will run — the same list renders the preview.

    ``prior_execution`` is the finding's stored step log from an earlier
    attempt (review round): a cluster whose umbrella was created before a
    later step failed reuses that umbrella number instead of opening a
    second one.
    """
    action = finding.action
    kind = action.get("kind")
    prior = prior_execution if isinstance(prior_execution, dict) else {}

    if kind in ("close_as_duplicate", "close_stale"):
        close = action.get("close", "")
        if close not in issues_by_key:
            return _gap(close)
        if kind == "close_as_duplicate":
            # Both members must still be open: closing the survivor of a pair
            # whose keeper went away deletes the tracking issue (Codex round 2).
            if action.get("of") not in issues_by_key:
                return _gap(action.get("of", ""))
            keep_repo, keep_num = split_key(action["of"])
            # A bare #N only resolves inside the closing issue's own repo
            # (Codex review): a cross-repo pair needs the full reference.
            ref = f"#{keep_num}" if keep_repo == split_key(close)[0] else action["of"]
            body = f"Duplicate of {ref} (closed by the {_SUFFIX})"
        else:
            # Revalidate against the current issue + record: new activity or a
            # raised value since detection means the close no longer applies
            # (Codex round 2). The next sweep resolves it; until then, no-op.
            issue = issues_by_key[close]
            seen = str(action.get("last_activity") or "")
            seen_ts, live_ts = parse_ts(seen), parse_ts(issue.updated_at)
            own_ts = parse_ts(prior.get("commented_at"))
            if own_ts is not None:
                # Our own comment advanced updated_at (Codex round 3): judge
                # activity against that comment, with a grace window. Never
                # against the latest attempt's start (round 4) — a refused
                # retry would otherwise move the cutoff past human activity.
                seen_ts = own_ts + OWN_ACTIVITY_GRACE_S
            if seen_ts is not None and live_ts is not None and live_ts > seen_ts:
                return _stale_recommendation(
                    f"{_short(close)} has new activity since {seen[:10]} "
                    f"(last {str(issue.updated_at)[:10]})"
                )
            record = triage_store.get(close) if triage_store is not None else None
            value_then = action.get("value")
            if record is not None and isinstance(value_then, int) and (
                record.value is None or record.value > value_then
            ):
                return _stale_recommendation(
                    f"{_short(close)}'s value moved from {value_then} to "
                    f"{record.value if record.value is not None else 'unscored'}"
                )
            date = str(action.get("last_activity") or "")[:10]
            body = (
                f"Closing as stale: no activity since {date}, "
                f"value {action.get('value')} ({_SUFFIX})"
            )
        return [
            _comment_step(close, body, prior=prior, now=now),
            _close_step(close, github_cache),
        ]

    if kind == "merge_cluster":
        repo = action["repo"]
        children = list(action.get("children", []))
        # Children closed since the sweep (by hand, by an earlier duplicate
        # accept, or by this cluster's own previous attempt) are skipped; the
        # umbrella covers the ones still open. Fewer than two left and there
        # is nothing to cluster (hand-test + Codex round).
        open_children = [c for c in children if c in issues_by_key]
        existing = prior.get("umbrella")
        if not isinstance(existing, int) or isinstance(existing, bool):
            existing = None
        if existing is not None and _known_closed(github_cache, f"{repo}#{existing}"):
            # Codex round 5: closing the remaining children under an umbrella
            # someone closed between attempts would leave nothing tracking
            # them. The closed-keys index was just force-synced by the route.
            return _stale_recommendation(
                f"umbrella #{existing} from the previous attempt was closed since"
            )
        # With an umbrella already on GitHub the retry finishes whatever is
        # left, even one child; a fresh plan needs two to be worth an umbrella.
        if len(open_children) < (1 if existing is not None else 2):
            return [
                Step(
                    label="fewer than two of the children are still open — nothing to do",
                    run=lambda: StepResult(False, "cluster has fewer than two open children"),
                    actionable=False,
                )
            ]
        title = action["umbrella_title"]
        numbers = [split_key(c)[1] for c in open_children]
        body_lines = [
            f"Umbrella for {len(open_children)} sentinel-filed issues sharing one root cause:",
            "",
        ]
        body_lines += [f"- #{split_key(c)[1]} {issues_by_key[c].title}" for c in open_children]
        body_lines += ["", f"Opened by the {_SUFFIX}."]
        body = "\n".join(body_lines)
        ctx: dict[str, int] = {}
        if existing is not None:
            ctx["umbrella"] = existing

        def create() -> StepResult:
            try:
                url = create_issue(repo, title, body, labels=["sentinel"])
            except GHError as exc:
                return StepResult(False, str(exc))
            match = _ISSUE_NUMBER_RE.search(url)
            if not match:
                return StepResult(False, f"could not read the issue number from {url!r}")
            ctx["umbrella"] = int(match.group(1))
            return StepResult(
                True, f"created #{ctx['umbrella']}: {url}", {"umbrella": ctx["umbrella"]}
            )

        if "umbrella" in ctx:
            steps = [
                Step(
                    label=f"Reuse umbrella #{ctx['umbrella']} created by the previous attempt",
                    run=lambda: StepResult(True, f"reusing #{ctx['umbrella']}"),
                )
            ]
        else:
            steps = [
                Step(
                    label=(
                        f"Create issue in {repo}: {title} (label sentinel, body lists "
                        + ", ".join(f"#{n}" for n in numbers)
                        + ")"
                    ),
                    run=create,
                )
            ]
        for child in children:
            if child not in issues_by_key:
                steps.append(_skip(child))
                continue
            c_repo, c_num = split_key(child)

            def comment(c_repo=c_repo, c_num=c_num) -> StepResult:
                umbrella = ctx.get("umbrella")
                if umbrella is None:
                    return StepResult(False, "umbrella was not created")
                ok = comment_issue(c_repo, c_num, f"Tracked by #{umbrella} ({_SUFFIX})")
                return StepResult(ok, "" if ok else "gh issue comment failed")

            steps.append(
                Step(
                    label=(
                        f"Comment on {_short(child)}: "
                        "Tracked by the umbrella issue created above"
                    ),
                    run=comment,
                )
            )
            steps.append(_close_step(child, github_cache))
        return steps

    if kind == "bump_value":
        key = action["key"]
        proposed = action["proposed"]
        if key not in issues_by_key:
            # Codex round 5: the forced refetch dropped it — a human-provenance
            # value on a closed (soon tombstoned) record helps no one.
            return _gap(key)
        current = action.get("current")
        was = f"was {current}" if current is not None else "was unscored"
        record = triage_store.get(key) if triage_store is not None else None
        live = record.value if record is not None else None
        if live != current:
            return _stale_recommendation(
                f"{_short(key)}'s value changed since the sweep ({was}, now "
                f"{live if live is not None else 'unscored'})"
            )

        def bump() -> StepResult:
            if triage_store is None:
                return StepResult(False, "triage store not configured")
            record = TriageRecord(issue_key=key, value=proposed, updated_at=now)
            try:
                stored = triage_store.upsert(record, actor="human")
            except OSError as exc:
                return StepResult(False, str(exc))
            return StepResult(stored is not None, "" if stored else "repo file unreadable")

        return [Step(label=f"Set value {proposed} on {_short(key)} ({was})", run=bump)]

    if kind == "add_to_board":
        return _add_to_board_steps(action, issues_by_key, github_cache, prior)

    return [Step(label=f"Unknown action {kind!r}", run=lambda: StepResult(False, "unknown action"))]


def _board_member(github_cache, number: int, owner: str, key: str) -> Issue | None:
    """The issue as the cached board holds it, Done included (never fetches)."""
    lookup = getattr(github_cache, "get_issues_cached", None)
    if lookup is None:
        return None
    issues, _fresh = lookup(number, owner)
    for issue in issues or []:
        if issue.key.lower() == key.lower():  # config key vs nameWithOwner case (round 9)
            return issue
    return None


def _add_to_board_steps(action: dict, issues_by_key: dict[str, Issue], github_cache, prior: dict):
    """#273: ``gh project item-add`` then Status→Todo through the cache's
    field-id resolution. The item id lands in the facts so a retry after a
    failed status write reuses it (item-add is idempotent anyway — GitHub
    returns the existing item)."""
    key = action["key"]
    repo, num = split_key(key)
    number, owner = int(action["project_number"]), str(action["project_owner"])
    name = action.get("project_name") or f"#{number}"
    if _known_closed(github_cache, key):
        return _gap(key)
    # The closed-keys cache is only filled for repos with an on-board issue —
    # the headline case (an empty board) has none — so ask GitHub directly
    # (REST, 0 points; review round 4). A failed lookup is not a verdict.
    summary = fetch_issue_summary(repo, num)
    if summary is not None and summary.get("state") in ("closed", "missing"):
        return _gap(key)
    ctx: dict[str, str] = {}
    if isinstance(prior.get("item_id"), str):
        ctx["item_id"] = prior["item_id"]
    on_board = _board_member(github_cache, number, owner, key)
    if on_board is None:
        # The fresh list spans every configured board (round 8): only an
        # Issue fetched from THIS board counts — one parked on another repo's
        # board is still off its own, and its item belongs to that board.
        # Owner too (Codex): two owners can share a board number.
        listed = issues_by_key.get(key) or next(
            (i for k, i in issues_by_key.items() if k.lower() == key.lower()), None
        )
        if (
            listed is not None
            and listed.project_number == number
            and listed.project_owner == owner
        ):
            on_board = listed
    if on_board is not None:
        # The forced refetch the route plans against shows it on the board
        # already — added by hand, or by the previous attempt's item-add
        # before its Todo write failed (review round 1). With a Status it is
        # done (a hand-parked Done item must not be pulled back to Todo —
        # round 2, hence the RAW board, not the Done-dropped list); without
        # one, finish the Todo write on the board's item.
        if on_board.status:
            return _stale_recommendation(f"{_short(key)} is already on the board")
        if on_board.item_id:
            ctx["item_id"] = on_board.item_id
        if not ctx.get("item_id"):
            return _stale_recommendation(f"{_short(key)} is already on the board")
        finish_only = True
    else:
        finish_only = False

    def add() -> StepResult:
        try:
            item_id = add_to_project(number, owner, f"https://github.com/{repo}/issues/{num}")
        except GHError as exc:
            return StepResult(False, str(exc))
        ctx["item_id"] = item_id
        return StepResult(True, f"item {item_id}", {"item_id": item_id})

    def set_todo() -> StepResult:
        item_id = ctx.get("item_id")
        if not item_id:
            return StepResult(False, "item was not added")
        if github_cache is None:
            return StepResult(False, "github cache not configured")
        # The item id rides on every result, failures included (round 7,
        # Codex: the early exits too): on the finish-only path there is no
        # add step to have recorded it, and without it the partial-attempt
        # guard never sees the attempt.
        facts = {"item_id": item_id}
        info = github_cache.get_field_info(number, owner, "Status")
        if info is None:
            return StepResult(False, f"the {name} board has no Status field", facts)
        option = info.options.get("Todo")
        if not option:
            return StepResult(False, f"the {name} board's Status has no Todo option", facts)
        ok = set_field_value(info.project_node_id, item_id, info.field_id, option)
        return StepResult(ok, "" if ok else "gh project item-edit failed", facts)

    todo = Step(label=f"Set Status to Todo on the {name} board", run=set_todo)
    if finish_only:
        return [todo]
    return [Step(label=f"Add {_short(key)} to the {name} board", run=add), todo]


def execution_facts(execution: dict | None) -> dict:
    """The retry-reusable facts (e.g. ``umbrella``) out of a stored log."""
    prior = execution if isinstance(execution, dict) else {}
    return {k: v for k, v in prior.items() if k not in EXECUTION_BOOKKEEPING}


def execute(
    finding: Finding,
    steps: list[Step],
    *,
    now: str,
    prior_execution: dict | None = None,
    on_step: Callable[[list[dict], dict], None] | None = None,
) -> dict:
    """Run the plan in order, stop at the first failure. The returned log
    carries forward any ``StepResult.data`` facts (and the prior attempt's)
    so a retry can pick up where this one stopped. ``on_step`` receives the
    cumulative log AND the facts so far after every step (the live checklist
    + durable retry facts — an umbrella number must outlive a process death,
    Codex round 4); its failures are swallowed — progress display must never
    abort the GitHub writes."""
    facts = execution_facts(prior_execution)
    log: list[dict] = []
    ok = True
    for step in steps:
        try:
            result = step.run()
        except Exception as exc:  # a step must never take the route down
            result = StepResult(False, f"{type(exc).__name__}: {exc}")
        if result.data:
            facts.update(result.data)
        log.append({"label": step.label, "ok": result.ok, "detail": result.detail})
        if on_step is not None:
            try:
                on_step(list(log), dict(facts))
            except Exception:
                pass
        if not result.ok:
            ok = False
            eventlog.emit(
                "warn",
                "github write failed",
                {"op": "hygiene-accept", "finding": finding.id, "step": step.label,
                 "detail": result.detail},
            )
            break
    return {**facts, "ok": ok, "at": now, "steps": log}
