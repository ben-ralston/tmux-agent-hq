"""``agent-hq gh-stats`` — what we spent on the GitHub API (#255).

#255's acceptance criterion is a *measured* before/after, so this reads the
meter rather than estimating from the code. It doubles as the ongoing check
that call volume has not crept back up as new processes join.

Calls alone would mislead badly, because the costs differ by ~50x
(:data:`GRAPHQL_POINT_COSTS`), so the report converts them to points using the
measured per-call table. GitHub does not report points per request anywhere —
the only live source is a ``rate_limit`` delta, which is eventually consistent
and polluted by every other process on the machine — so a measured table
applied to an exact call count is strictly better than either alone.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import gh_budget, gh_meter

# Measured 2026-08-26, gh 2.97.0 (N=30, idle control 0 points/45 s). The
# measurement write-up is not part of this repository.
#
# The spread is the whole story: a Projects v2 board query costs ~50x a
# 500-row issue list, which is the opposite of what issue #255 assumed.
GRAPHQL_POINT_COSTS = {
    # The call agent-hq no longer makes, kept so an old counter file still
    # scores correctly. 203, not the 101.5 first reported: that reading came
    # from a run that aggregated `rate_limit` with MAX(used) while discarding
    # `used == 0` reads, a rule later shown to be unreliable.
    "project-item-list": 203.0,
    # Its replacement: 2 points per 100-item page (gh_board), so a board of
    # up to 100 issues costs 2 against item-list's 203.
    "project-board-query": 2.0,
    "project-view": 1.0,
    "project-field-list": 1.0,
    "project-item-add": 1.0,
    "project-item-edit": 1.0,
    "issue-list-tone": 2.0,
    "issue-list-idea": 2.0,
    "issue-view": 1.0,
    "issue-create": 1.0,
    "issue-close": 1.0,
    "issue-transfer": 1.0,
    # The daemon's branch-phase fallback, per branch, behind a 60 s memo.
    "pr-view": 1.0,
    # gh_budget's read of GraphQL's own `rateLimit` object: goes through the
    # GraphQL bucket of the meter, but GitHub does not bill it (verified
    # 2026-09-11 — `used` held still across consecutive reads).
    "rate-limit-graphql": 0.0,
}

# An unrecognised GraphQL call still costs something; never score it zero.
_DEFAULT_GRAPHQL_COST = 1.0


def estimate_graphql_points(graphql_by_kind: dict[str, int]) -> float:
    """Estimated GraphQL points for the GraphQL calls of a call breakdown.

    ``graphql_by_kind`` is ``MeterSummary.graphql_by_kind`` — counts of
    GraphQL-bucket rows per kind. REST calls never reach it, which is the
    point of moving work there. An unrecognised kind scores
    ``_DEFAULT_GRAPHQL_COST``, never zero.
    """
    total = 0.0
    for kind, count in graphql_by_kind.items():
        total += count * GRAPHQL_POINT_COSTS.get(kind, _DEFAULT_GRAPHQL_COST)
    return round(total, 1)


def run_gh_stats(hours: float = 1.0, as_json: bool = False) -> int:
    summary = gh_meter.summarize(hours=hours)
    budget = gh_budget.snapshot()
    points = estimate_graphql_points(summary.graphql_by_kind)

    if as_json:
        print(
            json.dumps(
                {
                    "hours": summary.hours,
                    "total": summary.total,
                    "per_hour": summary.per_hour,
                    "by_api": summary.by_api,
                    "by_kind": summary.by_kind,
                    "estimated_graphql_points": points,
                    "graphql_remaining": budget.graphql_remaining if budget else None,
                    "graphql_limit": budget.graphql_limit if budget else None,
                }
            )
        )
        return 0

    print(f"gh calls in the last {summary.hours:g}h: {summary.total} ({summary.per_hour:.0f}/hr)")
    print(f"estimated GraphQL points: {points:.0f}")
    if summary.by_api:
        print("\nBy bucket:")
        for api, count in sorted(summary.by_api.items(), key=lambda kv: -kv[1]):
            print(f"  {api:<10} {count:>6}")
    if summary.by_kind:
        print("\nBy call:")
        for kind, count in sorted(summary.by_kind.items(), key=lambda kv: -kv[1]):
            gql = summary.graphql_by_kind.get(kind, 0)
            cost = (
                f"  ~{gql * GRAPHQL_POINT_COSTS.get(kind, _DEFAULT_GRAPHQL_COST):.0f} pts"
                if gql
                else ""
            )
            print(f"  {kind:<22} {count:>6}{cost}")
    if budget is not None:
        reset = (
            datetime.fromtimestamp(budget.graphql_reset).strftime("%H:%M")
            if budget.graphql_reset
            else "?"
        )
        print(
            f"\nHeadroom: GraphQL {budget.graphql_remaining}/{budget.graphql_limit} "
            f"(resets {reset}), REST {budget.core_remaining}/{budget.core_limit}"
        )
    else:
        print("\nHeadroom: unknown (rate_limit probe unavailable)")
    return 0
