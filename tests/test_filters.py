"""Tests for ``agent_hq.filters`` — pure issue filtering helpers used by
both the TUI dispatch screen (eventual extraction) and the web dispatch
view. Behavior must match the TUI's existing search semantics in
``dispatch_screen.py:_rebuild_table`` so users get the same results
across surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_hq.filters import (
    SORT_MODES,
    available_filter_options,
    filter_issues,
    format_relative_age,
    format_stale_notice,
    sort_issues,
)
from agent_hq.models import Issue, Priority


def _mk(
    number: int,
    *,
    repo: str = "ben-ralston/agent-hq",
    title: str = "",
    body: str = "",
    priority: Priority = Priority.NO_PRI,
    status: str | None = "Todo",
    updated_at: str | None = None,
    created_at: str | None = None,
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title or f"issue {number}",
        repo=repo,
        status=status,
        priority=priority,
        body=body,
        updated_at=updated_at,
        created_at=created_at,
        labels=labels or [],
    )


class TestSentinelDimension:
    """Include/exclude machine-filed (sentinel-labeled) issues (#235 follow-up)."""

    def test_filter_only_sentinel(self):
        issues = [_mk(1, labels=["sentinel"]), _mk(2), _mk(3, labels=["bug", "sentinel"])]
        out = filter_issues(issues, sentinels=["sentinel"])
        assert [i.number for i in out] == [1, 3]

    def test_filter_exclude_sentinel(self):
        issues = [_mk(1, labels=["sentinel"]), _mk(2)]
        out = filter_issues(issues, sentinels=["other"])
        assert [i.number for i in out] == [2]

    def test_filter_both_or_unset_means_no_constraint(self):
        issues = [_mk(1, labels=["sentinel"]), _mk(2)]
        assert len(filter_issues(issues, sentinels=["sentinel", "other"])) == 2
        assert len(filter_issues(issues, sentinels=None)) == 2
        assert len(filter_issues(issues, sentinels=[])) == 2

    def test_options_group_only_when_mixed(self):
        mixed = [_mk(1, labels=["sentinel"]), _mk(2), _mk(3)]
        opts = available_filter_options(mixed)
        assert opts["sentinels"] == [
            {"id": "sentinel", "label": "⚑ Sentinel", "count": 1},
            {"id": "other", "label": "Manual", "count": 2},
        ]
        # All-sentinel or no-sentinel lists → the chip group would be a
        # no-op filter, so it isn't rendered at all (#40 dynamic chips).
        assert available_filter_options([_mk(1, labels=["sentinel"])])["sentinels"] == []
        assert available_filter_options([_mk(1)])["sentinels"] == []
        assert available_filter_options([])["sentinels"] == []


class TestSortIssues:
    """Sort by (priority order, repo, number) — same as
    ``dispatch_screen.build_issue_rows``."""

    def test_priority_order_need_want_idea_none(self):
        issues = [
            _mk(1, priority=Priority.IDEA),
            _mk(2, priority=Priority.NO_PRI),
            _mk(3, priority=Priority.NEED),
            _mk(4, priority=Priority.WANT),
        ]
        ordered = sort_issues(issues)
        assert [i.number for i in ordered] == [3, 4, 1, 2]

    def test_within_priority_sort_by_repo_then_number(self):
        issues = [
            _mk(7, repo="z/zlib", priority=Priority.NEED),
            _mk(3, repo="a/alpha", priority=Priority.NEED),
            _mk(1, repo="a/alpha", priority=Priority.NEED),
        ]
        ordered = sort_issues(issues)
        assert [i.number for i in ordered] == [1, 3, 7]

    def test_does_not_mutate_input(self):
        issues = [_mk(2, priority=Priority.IDEA), _mk(1, priority=Priority.NEED)]
        original = list(issues)
        sort_issues(issues)
        assert issues == original

    def test_default_sort_by_matches_omitted(self):
        issues = [
            _mk(1, priority=Priority.IDEA),
            _mk(2, priority=Priority.NEED),
        ]
        assert sort_issues(issues, sort_by="default") == sort_issues(issues)


class TestSortIssuesByDate:
    """``created`` / ``updated`` order by the matching timestamp; ``descending``
    chooses newest-first (True) or oldest-first (False). None timestamps sort
    last; equal timestamps fall back to default (priority, repo, number) order,
    and that tie-break is never reversed by the sort direction (#156)."""

    def test_sort_modes_exposes_the_contract(self):
        assert SORT_MODES == ("default", "created", "updated")

    def test_created_descending(self):
        issues = [
            _mk(1, created_at="2026-05-10T00:00:00Z"),
            _mk(2, created_at="2026-05-20T00:00:00Z"),
            _mk(3, created_at="2026-05-01T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=True)
        assert [i.number for i in ordered] == [2, 1, 3]

    def test_created_ascending(self):
        issues = [
            _mk(1, created_at="2026-05-10T00:00:00Z"),
            _mk(2, created_at="2026-05-20T00:00:00Z"),
            _mk(3, created_at="2026-05-01T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=False)
        assert [i.number for i in ordered] == [3, 1, 2]

    def test_updated_uses_updated_at_not_created_at(self):
        # created_at order would be [1, 2]; updated_at order is [2, 1].
        issues = [
            _mk(1, created_at="2026-05-01T00:00:00Z", updated_at="2026-05-20T00:00:00Z"),
            _mk(2, created_at="2026-05-10T00:00:00Z", updated_at="2026-05-25T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="updated", descending=True)
        assert [i.number for i in ordered] == [2, 1]

    def test_none_timestamps_last_descending(self):
        issues = [
            _mk(1, created_at="2026-05-10T00:00:00Z"),
            _mk(2, created_at=None),
            _mk(3, created_at="2026-05-20T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=True)
        assert [i.number for i in ordered] == [3, 1, 2]

    def test_none_timestamps_last_ascending(self):
        # The case a naive ``reverse=True`` gets wrong: None must stay
        # last even when sorting ascending.
        issues = [
            _mk(1, created_at="2026-05-10T00:00:00Z"),
            _mk(2, created_at=None),
            _mk(3, created_at="2026-05-20T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=False)
        assert [i.number for i in ordered] == [1, 3, 2]

    def test_tiebreak_by_priority_repo_number_not_reversed(self):
        # Equal timestamps → fall back to (priority, repo, number) ascending,
        # in BOTH directions (proves the fallback isn't flipped).
        issues = [
            _mk(5, created_at="2026-05-15T00:00:00Z", priority=Priority.WANT, repo="b/b"),
            _mk(6, created_at="2026-05-15T00:00:00Z", priority=Priority.NEED, repo="a/a"),
        ]
        assert [i.number for i in sort_issues(issues, sort_by="created", descending=True)] == [6, 5]
        assert [i.number for i in sort_issues(issues, sort_by="created", descending=False)] == [
            6,
            5,
        ]

    def test_parses_z_suffix_timestamp(self):
        # Python 3.10's datetime.fromisoformat rejects a trailing 'Z';
        # the sort must still order these correctly.
        issues = [
            _mk(1, created_at="2026-05-01T12:00:00Z"),
            _mk(2, created_at="2026-05-01T13:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=True)
        assert [i.number for i in ordered] == [2, 1]

    def test_malformed_timestamp_treated_as_none(self):
        issues = [
            _mk(1, created_at="not-a-date"),
            _mk(2, created_at="2026-05-20T00:00:00Z"),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=True)
        assert [i.number for i in ordered] == [2, 1]  # garbage sorts last

    def test_all_none_falls_back_to_default_order(self):
        issues = [
            _mk(1, priority=Priority.IDEA, created_at=None),
            _mk(2, priority=Priority.NEED, created_at=None),
        ]
        ordered = sort_issues(issues, sort_by="created", descending=True)
        assert [i.number for i in ordered] == [2, 1]  # NEED before IDEA

    def test_unknown_sort_by_falls_back_to_default(self):
        issues = [_mk(1, priority=Priority.IDEA), _mk(2, priority=Priority.NEED)]
        assert sort_issues(issues, sort_by="bogus") == sort_issues(issues)

    def test_does_not_mutate_input(self):
        issues = [
            _mk(1, created_at="2026-05-01T00:00:00Z"),
            _mk(2, created_at="2026-05-20T00:00:00Z"),
        ]
        original = list(issues)
        sort_issues(issues, sort_by="created", descending=True)
        assert issues == original


class TestFormatRelativeAge:
    """Issue #101 follow-up: compact relative age shown next to issues when
    sorting by recency. ``now`` is injected so buckets are deterministic."""

    # A fixed reference instant (epoch seconds for 2026-05-25T12:00:00Z).
    NOW = 1779710400.0

    def _iso(self, *, seconds_ago: float) -> str:
        from datetime import datetime, timezone

        ts = self.NOW - seconds_ago
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_none_is_empty(self):
        assert format_relative_age(None, now=self.NOW) == ""

    def test_empty_string_is_empty(self):
        assert format_relative_age("", now=self.NOW) == ""

    def test_garbage_is_empty(self):
        assert format_relative_age("not-a-date", now=self.NOW) == ""

    def test_sub_minute_reads_now(self):
        assert format_relative_age(self._iso(seconds_ago=10), now=self.NOW) == "now"

    def test_minutes(self):
        assert format_relative_age(self._iso(seconds_ago=5 * 60), now=self.NOW) == "5m"

    def test_hours(self):
        assert format_relative_age(self._iso(seconds_ago=3 * 3600), now=self.NOW) == "3h"

    def test_days(self):
        assert format_relative_age(self._iso(seconds_ago=2 * 86400), now=self.NOW) == "2d"

    def test_weeks(self):
        assert format_relative_age(self._iso(seconds_ago=14 * 86400), now=self.NOW) == "2w"

    def test_months(self):
        assert format_relative_age(self._iso(seconds_ago=60 * 86400), now=self.NOW) == "2mo"

    def test_years(self):
        assert format_relative_age(self._iso(seconds_ago=800 * 86400), now=self.NOW) == "2y"

    def test_future_clamps_to_now(self):
        # Clock skew shouldn't render a negative/garbage age.
        future = self._iso(seconds_ago=-3600)
        assert format_relative_age(future, now=self.NOW) == "now"

    def test_z_suffix_parses(self):
        assert format_relative_age("2026-05-24T12:00:00Z", now=self.NOW) == "1d"

    def test_now_defaults_to_wall_clock(self):
        # A very old timestamp is stable regardless of the real clock.
        assert format_relative_age("2000-01-01T00:00:00Z").endswith("y")


class TestFilterByRepo:
    def test_no_repos_returns_all(self):
        issues = [_mk(1, repo="a/x"), _mk(2, repo="b/y")]
        assert filter_issues(issues, repos=None) == issues
        assert filter_issues(issues, repos=[]) == issues

    def test_filter_to_single_repo(self):
        issues = [_mk(1, repo="a/x"), _mk(2, repo="b/y")]
        out = filter_issues(issues, repos=["a/x"])
        assert [i.number for i in out] == [1]

    def test_filter_to_multiple_repos_ors_within_dimension(self):
        issues = [_mk(1, repo="a/x"), _mk(2, repo="b/y"), _mk(3, repo="c/z")]
        out = filter_issues(issues, repos=["a/x", "c/z"])
        assert [i.number for i in out] == [1, 3]




class TestFilterByStatus:
    def test_filter_to_single_status(self):
        issues = [_mk(1, status="Todo"), _mk(2, status="In Progress")]
        out = filter_issues(issues, statuses=["Todo"])
        assert [i.number for i in out] == [1]

    def test_status_match_is_case_insensitive(self):
        issues = [_mk(1, status="Todo"), _mk(2, status="todo")]
        out = filter_issues(issues, statuses=["TODO"])
        assert [i.number for i in out] == [1, 2]

    def test_filter_no_status_via_none_id(self):
        """Issues with ``status=None`` get the literal ``"none"`` filter ID."""
        issues = [_mk(1, status=None), _mk(2, status="Todo")]
        out = filter_issues(issues, statuses=["none"])
        assert [i.number for i in out] == [1]

    def test_empty_string_status_treated_as_none(self):
        issues = [_mk(1, status=""), _mk(2, status="Todo")]
        out = filter_issues(issues, statuses=["none"])
        assert [i.number for i in out] == [1]


class TestFilterByQuery:
    def test_substring_match_on_title(self):
        issues = [
            _mk(1, title="dispatch view"),
            _mk(2, title="kanban polish"),
        ]
        assert [i.number for i in filter_issues(issues, query="dispatch")] == [1]

    def test_substring_match_on_body(self):
        issues = [
            _mk(1, title="x", body="this is about cookies"),
            _mk(2, title="y", body="something else"),
        ]
        assert [i.number for i in filter_issues(issues, query="cookie")] == [1]

    def test_query_is_case_insensitive(self):
        issues = [_mk(1, title="Dispatch")]
        assert filter_issues(issues, query="dispatch") == issues
        assert filter_issues(issues, query="DISPATCH") == issues

    def test_query_matches_issue_number(self):
        """The TUI's search matches against ``str(number)`` — keep parity."""
        issues = [_mk(67, title="aaaa"), _mk(40, title="bbbb")]
        assert [i.number for i in filter_issues(issues, query="67")] == [67]

    def test_query_matches_repo_short(self):
        issues = [
            _mk(1, repo="ben-ralston/widgets", title="x"),
            _mk(2, repo="ben-ralston/agent-hq", title="y"),
        ]
        assert [i.number for i in filter_issues(issues, query="widg")] == [1]

    def test_empty_query_returns_all(self):
        issues = [_mk(1), _mk(2)]
        assert filter_issues(issues, query="") == issues
        assert filter_issues(issues, query=None) == issues

    def test_query_whitespace_only_returns_all(self):
        """Trim whitespace so an accidental leading space doesn't filter
        everything out."""
        issues = [_mk(1, title="dispatch")]
        assert filter_issues(issues, query="   ") == issues


class TestFilterIntersection:
    """When multiple filters are set, an issue must satisfy ALL of them
    (AND across dimensions, OR within a dimension)."""

    def test_chips_and_query_intersect(self):
        """#44 acceptance: search intersects with chip filters."""
        issues = [
            _mk(1, repo="a/x", priority=Priority.NEED, title="dispatch view"),
            _mk(2, repo="a/x", priority=Priority.WANT, title="dispatch refactor"),
            _mk(3, repo="b/y", priority=Priority.NEED, title="kanban polish"),
        ]
        out = filter_issues(
            issues,
            repos=["a/x"],
            query="dispatch",
        )
        assert [i.number for i in out] == [1, 2]

    def test_no_filters_returns_input_order(self):
        issues = [_mk(2), _mk(1)]
        assert filter_issues(issues) == issues  # NOTE: filter_issues does NOT sort


class TestAvailableFilterOptions:
    """Inputs to the chip rendering. Only options that appear at least
    once in the loaded issues are returned (#40 decision: dynamic chips)."""

    def test_repos_returned_as_id_label_count(self):
        issues = [
            _mk(1, repo="ben-ralston/agent-hq"),
            _mk(2, repo="ben-ralston/agent-hq"),
            _mk(3, repo="ben-ralston/widgets"),
        ]
        opts = available_filter_options(issues)
        assert opts["repos"] == [
            {"id": "ben-ralston/agent-hq", "label": "agent-hq", "count": 2},
            {"id": "ben-ralston/widgets", "label": "widgets", "count": 1},
        ]



    def test_statuses_returned_with_counts(self):
        issues = [
            _mk(1, status="Todo"),
            _mk(2, status="Todo"),
            _mk(3, status="In Progress"),
            _mk(4, status=None),
        ]
        opts = available_filter_options(issues)
        # IDs are lowercase for cookie-friendly identity; label preserves
        # the original casing from gh.
        ids = {s["id"] for s in opts["statuses"]}
        assert ids == {"todo", "in progress", "none"}
        by_id = {s["id"]: s for s in opts["statuses"]}
        assert by_id["todo"]["count"] == 2
        assert by_id["todo"]["label"] == "Todo"
        assert by_id["in progress"]["label"] == "In Progress"
        assert by_id["none"]["count"] == 1

    def test_empty_input_returns_empty_lists(self):
        opts = available_filter_options([])
        assert opts == {"repos": [], "statuses": [], "sentinels": []}


@pytest.mark.parametrize(
    "filter_kwargs,expected",
    [
        ({}, [1, 2, 3]),
        ({"query": "agent"}, [1]),
        ({"repos": ["b/y"]}, [3]),
        ({"repos": ["a/x"], "query": "first"}, [1]),
    ],
)
def test_filter_issues_smoke(filter_kwargs, expected):
    issues = [
        _mk(1, repo="a/x", title="agent first", priority=Priority.NEED),
        _mk(2, repo="a/x", title="cleanup", priority=Priority.NEED),
        _mk(3, repo="b/y", title="other", priority=Priority.WANT),
    ]
    out = filter_issues(issues, **filter_kwargs)
    assert [i.number for i in out] == expected


class TestFormatStaleNotice:
    """Shared wording for a GitHub fetch failure — the TUI and the web
    dispatch banner must say the same thing (#254)."""

    def test_reports_cached_age(self):
        status = SimpleNamespace(
            ok=False,
            message="project @me/4: could not resolve host",
            rate_limited=False,
            reset_at=None,
            stale_since=1000.0,
        )
        text = format_stale_notice(status, now=1000.0 + 7200)
        assert "GitHub unreachable" in text
        assert "2h" in text

    def test_rate_limit_includes_reset_wait(self):
        status = SimpleNamespace(
            ok=False,
            message="project @me/4: GitHub API rate limit exceeded",
            rate_limited=True,
            reset_at=5000.0 + 1800,
            stale_since=5000.0 - 60,
        )
        text = format_stale_notice(status, now=5000.0)
        assert "rate limit" in text.lower()
        assert "30m" in text

    def test_a_budget_skip_does_not_claim_a_rate_limit(self):
        """Nothing failed — we chose not to spend the last of the budget
        (#255). "rate limit exceeded" would misdescribe both what happened
        and what the user should do about it."""
        status = SimpleNamespace(
            ok=False,
            message="GraphQL headroom low — background refresh paused",
            rate_limited=False,
            budget_skipped=True,
            reset_at=5000.0 + 900,
            stale_since=5000.0 - 600,
        )
        text = format_stale_notice(status, now=5000.0)
        assert text.startswith("GitHub budget low — showing cached")
        assert "rate limit" not in text.lower()
        assert "unreachable" not in text.lower()
        assert "15m" in text

    def test_no_cached_data_says_so(self):
        status = SimpleNamespace(
            ok=False,
            message="project @me/4: boom",
            rate_limited=False,
            reset_at=None,
            stale_since=None,
        )
        text = format_stale_notice(status, now=1000.0)
        assert "no cached" in text.lower()

    def test_ok_status_has_no_notice(self):
        status = SimpleNamespace(
            ok=True, message=None, rate_limited=False, reset_at=None, stale_since=None
        )
        assert format_stale_notice(status) == ""

    def test_none_status_has_no_notice(self):
        assert format_stale_notice(None) == ""


def test_parse_ts_treats_a_naive_timestamp_as_utc():
    """Pinned (review round 2): the shared parser reads a tz-less ISO string
    as UTC — GitHub always sends ``Z``, internal stamps carry ``+00:00``, so
    this only governs hand-edited data-repo values, deterministically."""
    from agent_hq.filters import parse_ts

    assert parse_ts("2026-09-12T10:00:00") == parse_ts("2026-09-12T10:00:00Z")
    assert parse_ts("2026-09-12T10:00:00") == parse_ts("2026-09-12T10:00:00+00:00")


def test_filter_dimensions_exclude_legacy_priority():
    import inspect

    assert set(inspect.signature(filter_issues).parameters) == {
        "issues", "repos", "statuses", "sentinels", "query",
    }
    assert set(available_filter_options([_mk(1)])) == {"repos", "statuses", "sentinels"}
