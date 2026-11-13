"""Tests for GitHub data fetching via gh CLI."""

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_hq import gh_board, gh_budget, gh_meter, github
from agent_hq.config import RepoConfig
from agent_hq.github import (
    FieldInfo,
    GHError,
    GitHubCache,
    GitHubFetchError,
    _enrich_dates,
    _fetch_failure,
    _rate_limit_reset,
    _safe_timestamps_for,
    _sync_issue_index,
    add_to_project,
    close_issue_not_planned,
    create_issue,
    detect_active_worktrees,
    fetch_closed_issue_keys,
    fetch_field_info,
    fetch_project_issues,
    fetch_recent_issues_for_tone,
    invalidate_worktree_cache,
    set_field_value,
    transfer_issue,
)
from agent_hq.models import Issue, Priority

SAMPLE_ITEMS_JSON = json.dumps(
    {
        "items": [
            {
                "content": {
                    "type": "Issue",
                    "number": 42,
                    "title": "Add dashboard",
                    "repository": "ben-ralston/agent-hq",
                    "body": "Implement a TUI dashboard",
                },
                "status": {"value": "Todo"},
                "priority": {"value": "Need"},
                "labels": [{"name": "feature"}],
            },
            {
                "content": {
                    "type": "Issue",
                    "number": 10,
                    "title": "Fix typo",
                    "repository": "ben-ralston/agent-hq",
                    "body": "",
                },
                "status": {"value": "Todo"},
                "priority": None,
                "labels": [],
            },
            {
                "content": {
                    "type": "DraftIssue",
                    "title": "Draft idea",
                },
                "status": None,
                "priority": None,
                "labels": [],
            },
        ]
    }
)

# The same board, already parsed: gh_board returns items, not a JSON blob.
SAMPLE_ITEMS = json.loads(SAMPLE_ITEMS_JSON)["items"]


class TestFetchProjectIssues:
    """Parsing is unchanged by #255 — only the transport underneath it moved,
    so these mock at the ``gh_board`` boundary rather than at subprocess."""

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS)
    def test_parses_items_into_issues(self, _board, _dates):
        issues = fetch_project_issues(12, "@me")
        assert len(issues) == 2
        assert issues[0].number == 42
        assert issues[0].repo == "ben-ralston/agent-hq"
        assert issues[0].title == "Add dashboard"
        assert issues[0].priority == Priority.NEED

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS)
    def test_missing_priority_defaults_to_no_pri(self, _board, _dates):
        issues = fetch_project_issues(12, "@me")
        issue_10 = [i for i in issues if i.number == 10][0]
        assert issue_10.priority == Priority.NO_PRI

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS)
    def test_carries_project_owner(self, _board, _dates):
        """#81: GitHub Project numbers are owner-scoped, so each fetched
        issue must remember which owner it was fetched under — number
        alone is ambiguous across orgs/users."""
        issues = fetch_project_issues(12, "some-org")
        assert issues and all(i.project_owner == "some-org" for i in issues)

    # Failure modes now raise ``GitHubFetchError`` instead of returning []
    # (#254) — see ``TestFetchProjectIssuesFailureContract`` below.

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run, _dates):
        """Every gh fetch must pass a finite ``timeout=`` so a hung gh
        cannot stall the whole request pool (#90).

        The timeout moved into ``gh_meter.run_gh`` with the transport, so the
        guarantee is asserted there rather than dropped."""
        page = {
            "data": {
                "viewer": {
                    "projectV2": {
                        "items": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(page), stderr="")
        fetch_project_issues(12, "@me")
        assert "timeout" in mock_run.call_args.kwargs
        assert mock_run.call_args.kwargs["timeout"] is not None

    @patch("agent_hq.github.subprocess.run")
    def test_reraises_timeout_so_cache_can_preserve(self, mock_run):
        """A timeout is NOT swallowed — it surfaces (as ``GitHubFetchError``
        since #254; previously the raw ``TimeoutExpired``) so the cache
        wrapper keeps the previous-good value instead of overwriting with
        [] (#90)."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")


# gh CLI actual format: fields are plain strings, not nested objects
SAMPLE_ITEMS_PLAIN_STRINGS = json.dumps(
    {
        "items": [
            {
                "content": {
                    "type": "Issue",
                    "number": 42,
                    "title": "Add dashboard",
                    "repository": "ben-ralston/agent-hq",
                    "body": "Implement a TUI dashboard",
                },
                "status": "Todo",
                "priority": "Need",
                "labels": ["feature"],
            },
            {
                "content": {
                    "type": "Issue",
                    "number": 10,
                    "title": "Fix typo",
                    "repository": "ben-ralston/agent-hq",
                    "body": "",
                },
                "status": "Todo",
                "priority": "",
                "labels": [],
            },
        ]
    }
)


SAMPLE_ITEMS_PLAIN = json.loads(SAMPLE_ITEMS_PLAIN_STRINGS)["items"]


class TestFetchProjectIssuesPlainStrings:
    """Plain-string field values, as both ``gh project item-list`` and
    ``gh_board`` emit them (the query normalizes to the same shape)."""

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS_PLAIN)
    def test_parses_plain_string_fields(self, _board, _dates):
        issues = fetch_project_issues(12, "@me")
        assert len(issues) == 2
        assert issues[0].number == 42
        assert issues[0].priority == Priority.NEED
        assert issues[0].status == "Todo"
        assert issues[0].labels == ["feature"]

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS_PLAIN)
    def test_empty_priority_string_defaults_to_no_pri(self, _board, _dates):
        issues = fetch_project_issues(12, "@me")
        issue_10 = [i for i in issues if i.number == 10][0]
        assert issue_10.priority == Priority.NO_PRI


class TestFetchProjectIssuesUsesTheNarrowBoardQuery:
    """The board fetch goes through ``gh_board``, not ``gh project item-list``.

    This is the single largest saving in #255: ``item-list`` costs 203 GraphQL
    points per board, the hand-written query 2 points per 100-item page
    (measured 2026-08-26). Board queries were 96% of a refresh cycle, so this
    swap — not the shared cache — is what takes the app back under budget.
    """

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items")
    def test_delegates_to_the_board_query(self, mock_board, _dates):
        mock_board.return_value = SAMPLE_ITEMS
        issues = fetch_project_issues(12, "@me")
        mock_board.assert_called_once_with(12, "@me")
        assert [i.number for i in issues] == [42, 10]

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_never_shells_out_to_item_list(self, mock_run, _dates):
        """The expensive call must be gone from the argv, not merely unused."""
        page = {
            "data": {
                "viewer": {
                    "projectV2": {
                        "items": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(page), stderr="")
        fetch_project_issues(12, "@me")
        argv = " ".join(str(a) for a in mock_run.call_args.args[0])
        assert "item-list" not in argv
        assert "graphql" in argv

    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items")
    def test_board_query_error_becomes_a_fetch_error(self, mock_board, _dates):
        """#254's contract holds across the new transport: the cache keeps its
        previous-good value rather than overwriting it with []."""
        mock_board.side_effect = gh_board.BoardQueryError("project 12: boom")
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")

    @patch("agent_hq.github._rate_limit_reset", return_value=1234.0)
    @patch("agent_hq.github._safe_timestamps_for", return_value={})
    @patch("agent_hq.github.gh_board.fetch_board_items")
    def test_rate_limited_board_query_is_flagged(self, mock_board, _dates, _reset):
        """A GraphQL 200-with-errors rate-limit body must still light up the
        banner — BoardQueryError carries `.stderr` so `_fetch_failure` sees it."""
        mock_board.side_effect = gh_board.BoardQueryError(
            "project 12: API rate limit already exceeded for user ID 1."
        )
        with pytest.raises(GitHubFetchError) as caught:
            fetch_project_issues(12, "@me")
        assert caught.value.rate_limited is True
        assert caught.value.reset_at == 1234.0


def _rest_page(*rows):
    """A REST issues page, as the index's sync reads it."""
    return MagicMock(returncode=0, stdout=json.dumps(list(rows)), stderr="")


def _rest_row(number, state="open", created="2026-01-01T00:00:00Z", updated="2026-01-02T00:00:00Z"):
    return {"number": number, "state": state, "created_at": created, "updated_at": updated}


class TestIssueIndexTimestamps:
    """Timestamps now come from the durable REST index (#255), not a 500-row
    ``gh issue list``. ``_safe_timestamps_for`` degrades failures to ``{}``."""

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_reads_timestamps_from_the_index(self, mock_run):
        mock_run.return_value = _rest_page(
            _rest_row(42, created="2026-05-15T08:00:00Z", updated="2026-05-20T10:00:00Z")
        )
        assert _safe_timestamps_for("o/r") == {
            42: {"created_at": "2026-05-15T08:00:00Z", "updated_at": "2026-05-20T10:00:00Z"}
        }

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_uses_rest_not_the_graphql_issue_list(self, mock_run):
        """`gh issue list` bills GraphQL; the REST endpoint bills 0 points and
        answers state and timestamps in the same call."""
        mock_run.return_value = _rest_page()
        _safe_timestamps_for("o/r")
        argv = mock_run.call_args.args[0]
        assert argv[:2] == ["gh", "api"]
        assert "issue" not in argv

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_sync_failure_degrades_to_no_dates(self, mock_run):
        """Dates are a sort nicety — a broken lookup must never fail the
        issue fetch that would otherwise have succeeded (#254)."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        assert _safe_timestamps_for("o/r") == {}

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_sync_failure_raises_for_callers_that_must_preserve(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(GitHubFetchError):
            _sync_issue_index("o/r")

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_repeat_sync_within_the_window_makes_no_call(self, mock_run):
        """The date enrichment and get_closed_keys both want the same repo
        each cycle; the on-disk `synced_at` collapses them into one call —
        across processes as well as within one."""
        mock_run.return_value = _rest_page(_rest_row(1))
        _sync_issue_index("o/r")
        assert mock_run.call_count == 1
        _sync_issue_index("o/r")
        assert mock_run.call_count == 1

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_force_bypasses_the_debounce(self, mock_run):
        mock_run.return_value = _rest_page(_rest_row(1))
        _sync_issue_index("o/r")
        _sync_issue_index("o/r", force=True)
        assert mock_run.call_count == 2


class TestFetchProjectIssuesTimestamps:
    """``fetch_project_issues`` enriches each issue with ``updated_at`` from
    a per-repo ``gh issue list`` call (issue #101). Best-effort: a failed
    enrichment leaves ``updated_at`` None without dropping issues."""

    @patch("agent_hq.gh_meter.subprocess.run")
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS)
    def test_enriches_created_and_updated(self, _board, mock_run):
        mock_run.return_value = _rest_page(
            _rest_row(42, created="2026-05-15T08:00:00Z", updated="2026-05-20T10:00:00Z"),
            _rest_row(10, created="2026-04-01T08:00:00Z", updated="2026-05-01T09:00:00Z"),
        )
        by_num = {i.number: i for i in fetch_project_issues(12, "@me")}
        assert by_num[42].updated_at == "2026-05-20T10:00:00Z"
        assert by_num[42].created_at == "2026-05-15T08:00:00Z"
        assert by_num[10].created_at == "2026-04-01T08:00:00Z"

    @patch("agent_hq.github.subprocess.run")
    @patch("agent_hq.github.gh_board.fetch_board_items", return_value=SAMPLE_ITEMS)
    def test_dates_none_when_timestamp_fetch_fails(self, _board, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        issues = fetch_project_issues(12, "@me")
        assert len(issues) == 2
        assert all(i.updated_at is None for i in issues)
        assert all(i.created_at is None for i in issues)

    @patch("agent_hq.github._safe_timestamps_for")
    def test_enrich_does_not_clobber_with_none(self, mock_fetch):
        # A lookup returning a present number but null timestamps must not
        # overwrite dates a caller already populated (presence-of-value guard).
        mock_fetch.return_value = {7: {"created_at": None, "updated_at": None}}
        issue = Issue(
            number=7,
            title="T",
            repo="o/r",
            status="Todo",
            priority=Priority.NEED,
            created_at="2026-05-15T08:00:00Z",
            updated_at="2026-05-20T10:00:00Z",
        )
        _enrich_dates([issue])
        assert issue.created_at == "2026-05-15T08:00:00Z"
        assert issue.updated_at == "2026-05-20T10:00:00Z"


class TestEnrichDatesOverAFullIndex:
    """The #113 backfill is gone with the window that made it necessary.

    It existed because a board issue could fall outside ``gh issue list``'s
    500-row cap, needing a targeted ``gh issue view`` to recover its dates.
    The index is complete and cumulative, so there is no window to fall
    outside of and no second call to make (#255).
    """

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_an_issue_beyond_the_old_window_still_gets_dates(self, mock_run):
        mock_run.return_value = _rest_page(
            _rest_row(7, created="2024-01-05T08:00:00Z", updated="2026-06-01T10:00:00Z")
        )
        missed = Issue(number=7, title="B", repo="o/r", status="Todo", priority=Priority.NEED)
        _enrich_dates([missed])
        assert missed.created_at == "2024-01-05T08:00:00Z"
        assert missed.updated_at == "2026-06-01T10:00:00Z"

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_one_call_per_repo_regardless_of_issue_count(self, mock_run):
        """#113's perf guarantee, now unconditional: no per-issue fan-out."""
        mock_run.return_value = _rest_page(_rest_row(1))
        issues = [
            Issue(number=n, title="x", repo="o/r", status="Todo", priority=Priority.NEED)
            for n in range(1, 25)
        ]
        _enrich_dates(issues)
        assert mock_run.call_count == 1

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_syncs_each_distinct_repo_once(self, mock_run):
        mock_run.return_value = _rest_page(_rest_row(1))
        issues = [
            Issue(number=1, title="x", repo="o/a", status="Todo", priority=Priority.NEED),
            Issue(number=2, title="y", repo="o/a", status="Todo", priority=Priority.NEED),
            Issue(number=3, title="z", repo="o/b", status="Todo", priority=Priority.NEED),
        ]
        _enrich_dates(issues)
        assert mock_run.call_count == 2


class TestFetchFieldInfo:
    @patch("agent_hq.github._fetch_project_node_id", return_value="PVT_node123")
    @patch("agent_hq.github.subprocess.run")
    def test_finds_field_with_options(self, mock_run, _mock_node):
        field_json = json.dumps(
            {
                "fields": [
                    {
                        "name": "Priority",
                        "id": "F2",
                        "options": [
                            {"name": "Need", "id": "OPT1"},
                            {"name": "Want", "id": "OPT2"},
                            {"name": "Idea", "id": "OPT3"},
                        ],
                    },
                ]
            }
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=field_json, stderr="")
        info = fetch_field_info(12, "@me", "Priority")
        assert info is not None
        assert info.field_id == "F2"
        assert info.project_node_id == "PVT_node123"
        assert info.options == {"Need": "OPT1", "Want": "OPT2", "Idea": "OPT3"}

    @patch("agent_hq.github.subprocess.run")
    def test_returns_none_when_field_not_found(self, mock_run):
        field_json = json.dumps({"fields": [{"name": "Status", "id": "F1", "options": []}]})
        mock_run.return_value = MagicMock(returncode=0, stdout=field_json, stderr="")
        assert fetch_field_info(12, "@me", "Priority") is None

    @patch("agent_hq.github._fetch_project_node_id", return_value=None)
    @patch("agent_hq.github.subprocess.run")
    def test_returns_none_when_node_id_missing(self, mock_run, _mock_node):
        field_json = json.dumps({"fields": [{"name": "Priority", "id": "F2", "options": []}]})
        mock_run.return_value = MagicMock(returncode=0, stdout=field_json, stderr="")
        assert fetch_field_info(12, "@me", "Priority") is None

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_subprocess_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(GitHubFetchError):
            fetch_field_info(12, "@me", "Priority")


class TestSetFieldValue:
    @patch("agent_hq.github.subprocess.run")
    def test_sets_option_id(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert set_field_value("PVT_node", "ITEM_1", "FIELD_1", "OPT_1") is True
        args = mock_run.call_args[0][0]
        assert "--single-select-option-id" in args
        assert "OPT_1" in args
        assert "--clear" not in args

    @patch("agent_hq.github.subprocess.run")
    def test_clears_when_no_option_id(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert set_field_value("PVT_node", "ITEM_1", "FIELD_1", None) is True
        args = mock_run.call_args[0][0]
        assert "--clear" in args
        assert "--single-select-option-id" not in args

    @patch("agent_hq.github.subprocess.run")
    def test_clears_when_empty_string(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert set_field_value("PVT_node", "ITEM_1", "FIELD_1", "") is True
        args = mock_run.call_args[0][0]
        assert "--clear" in args

    @patch("agent_hq.github.subprocess.run")
    def test_returns_false_on_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert set_field_value("PVT_node", "ITEM_1", "FIELD_1", "OPT_1") is False

    @patch("agent_hq.github.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        set_field_value("PVT_node", "ITEM_1", "FIELD_1", "OPT_1")
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        assert set_field_value("PVT_node", "ITEM_1", "FIELD_1", "OPT_1") is False


class TestDetectActiveWorktrees:
    @patch("agent_hq.github.subprocess.run")
    def test_extracts_issue_numbers_from_branches(self, mock_run):
        worktree_output = (
            "/Users/dev/agent-hq                     abc1234 [main]\n"
            "/Users/dev/agent-hq-feature-5-7-tui     def5678 [feature/5-7-tui-dashboard]\n"
            "/Users/dev/agent-hq-feature-42-auth      ghi9012 [feature/42-add-auth]\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=worktree_output, stderr="")
        repos = {
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=Path("/Users/dev/agent-hq"),
            )
        }
        active = detect_active_worktrees(repos)
        assert "ben-ralston/agent-hq#5" in active
        assert "ben-ralston/agent-hq#7" in active
        assert "ben-ralston/agent-hq#42" in active

    @patch("agent_hq.github.subprocess.run")
    def test_returns_empty_on_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        repos = {"o/r": RepoConfig(owner_name="o/r", local_path=Path("/tmp/r"))}
        assert detect_active_worktrees(repos) == set()


class TestDetectActiveWorktreesCache:
    """#226: worktree detection is called on every dispatch keystroke/chip
    render — cache it briefly and bound the git subprocess with a timeout."""

    def setup_method(self):
        invalidate_worktree_cache()

    def _repos(self, path: str = "/tmp/wt-cache-a") -> dict:
        return {"o/r": RepoConfig(owner_name="o/r", local_path=Path(path))}

    @patch("agent_hq.github.subprocess.run")
    def test_cached_within_ttl(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        repos = self._repos()
        detect_active_worktrees(repos)
        detect_active_worktrees(repos)
        assert mock_run.call_count == 1

    @patch("agent_hq.github.subprocess.run")
    def test_expires_after_ttl(self, mock_run, monkeypatch):
        import agent_hq.github as github_mod

        clock = [1000.0]
        monkeypatch.setattr(github_mod.time, "monotonic", lambda: clock[0])
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        repos = self._repos()
        detect_active_worktrees(repos)
        clock[0] += github_mod._WORKTREE_TTL + 1
        detect_active_worktrees(repos)
        assert mock_run.call_count == 2

    @patch("agent_hq.github.subprocess.run")
    def test_distinct_repo_sets_not_shared(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        detect_active_worktrees(self._repos("/tmp/wt-cache-b"))
        detect_active_worktrees(self._repos("/tmp/wt-cache-c"))
        assert mock_run.call_count == 2

    @patch("agent_hq.github.subprocess.run")
    def test_invalidate_forces_rerun(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        repos = self._repos()
        detect_active_worktrees(repos)
        invalidate_worktree_cache()
        detect_active_worktrees(repos)
        assert mock_run.call_count == 2

    @patch("agent_hq.github.subprocess.run")
    def test_cache_timestamp_captured_after_scan(self, mock_run, monkeypatch):
        """Review fix (#226): the entry's timestamp must be taken when the
        scan FINISHES — a slow scan (repos at the 5s git timeout) otherwise
        stores an already-expired entry and every subsequent call rescans."""
        import agent_hq.github as github_mod

        clock = [1000.0]
        monkeypatch.setattr(github_mod.time, "monotonic", lambda: clock[0])

        def slow_scan(*args, **kwargs):
            clock[0] += github_mod._WORKTREE_TTL + 5  # scan outlives the TTL
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = slow_scan
        repos = self._repos()
        detect_active_worktrees(repos)
        detect_active_worktrees(repos)  # within TTL of the STORE time
        assert mock_run.call_count == 1

    @patch("agent_hq.github.subprocess.run")
    def test_git_invoked_with_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        detect_active_worktrees(self._repos())
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_timeout_skips_repo(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("git", 5)
        assert detect_active_worktrees(self._repos()) == set()


class TestIssueSerde:
    """Issue ⇄ dict serialization used by the disk cache."""

    def test_round_trips_project_owner(self):
        from agent_hq.github import _issue_from_dict, _issue_to_dict

        issue = Issue(
            number=1,
            title="t",
            repo="o/r",
            status="Todo",
            priority=Priority.NEED,
            project_number=5,
            project_owner="some-org",
        )
        assert _issue_from_dict(_issue_to_dict(issue)).project_owner == "some-org"

    def test_from_dict_tolerates_missing_project_owner(self):
        """Disk caches written before #81 have no ``project_owner`` key —
        they must load with the empty-string default, not KeyError."""
        from agent_hq.github import _issue_from_dict

        issue = _issue_from_dict({"number": 1, "title": "t", "repo": "o/r", "priority": "Need"})
        assert issue.project_owner == ""


class TestGitHubCache:
    @patch("agent_hq.github.fetch_project_issues")
    def test_returns_cached_data_within_ttl(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        result1 = cache.get_issues(12, "@me")
        assert len(result1) == 1
        assert mock_fetch.call_count == 1
        result2 = cache.get_issues(12, "@me")
        assert len(result2) == 1
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.fetch_project_issues")
    def test_force_bypasses_cache(self, mock_fetch, tmp_path):
        mock_fetch.return_value = []
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        cache.get_issues(12, "@me", force=True)
        assert mock_fetch.call_count == 2

    @patch("agent_hq.github.fetch_project_issues")
    def test_persists_to_disk_and_restores(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = [
            Issue(
                number=42,
                title="Test",
                repo="o/r",
                status="Todo",
                priority=Priority.NEED,
                labels=["bug"],
                body="desc",
            )
        ]
        # First cache instance fetches and writes to disk
        cache1 = GitHubCache(ttl=300, cache_file=cache_file)
        cache1.get_issues(12, "@me")
        assert mock_fetch.call_count == 1
        assert cache_file.exists()

        # Second cache instance loads from disk — no fetch needed
        cache2 = GitHubCache(ttl=300, cache_file=cache_file)
        result = cache2.get_issues(12, "@me")
        assert mock_fetch.call_count == 1  # Still 1 — served from disk
        assert len(result) == 1
        assert result[0].number == 42
        assert result[0].priority == Priority.NEED
        assert result[0].labels == ["bug"]
        assert result[0].body == "desc"

    @patch("agent_hq.github.fetch_project_issues")
    def test_persists_updated_at_across_disk_roundtrip(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = [
            Issue(
                number=42,
                title="T",
                repo="o/r",
                status="Todo",
                priority=Priority.NEED,
                updated_at="2026-05-20T10:00:00Z",
            )
        ]
        GitHubCache(ttl=300, cache_file=cache_file).get_issues(12, "@me")
        restored = GitHubCache(ttl=300, cache_file=cache_file).get_issues(12, "@me")
        assert restored[0].updated_at == "2026-05-20T10:00:00Z"

    @patch("agent_hq.github.fetch_project_issues")
    def test_persists_created_at_across_disk_roundtrip(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = [
            Issue(
                number=42,
                title="T",
                repo="o/r",
                status="Todo",
                priority=Priority.NEED,
                created_at="2026-05-15T08:00:00Z",
                updated_at="2026-05-20T10:00:00Z",
            )
        ]
        GitHubCache(ttl=300, cache_file=cache_file).get_issues(12, "@me")
        restored = GitHubCache(ttl=300, cache_file=cache_file).get_issues(12, "@me")
        assert restored[0].created_at == "2026-05-15T08:00:00Z"

    def test_old_cache_without_updated_at_loads_as_none(self, tmp_path):
        # Backward-compat: a cache file written before #101 has no
        # ``updated_at`` key — it must load as None, not KeyError.
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "@me/12": {
                        "timestamp": 9_999_999_999,  # far future ⇒ not expired
                        "issues": [
                            {
                                "number": 42,
                                "title": "T",
                                "repo": "o/r",
                                "status": "Todo",
                                "priority": "Need",
                                "labels": [],
                                "body": "",
                                "item_id": "",
                                "project_number": 12,
                            }
                        ],
                    }
                }
            )
        )
        issues = GitHubCache(ttl=300, cache_file=cache_file).get_issues(12, "@me")
        assert issues[0].updated_at is None
        assert issues[0].created_at is None

    @patch("agent_hq.github.fetch_project_issues")
    def test_expired_disk_cache_triggers_fetch(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        # Write cache with ttl=1, then wait for expiry
        cache1 = GitHubCache(ttl=1, cache_file=cache_file)
        cache1.get_issues(12, "@me")
        assert mock_fetch.call_count == 1

        import time as _time

        _time.sleep(1.1)

        # New instance loads expired disk cache — must re-fetch
        cache2 = GitHubCache(ttl=1, cache_file=cache_file)
        cache2.get_issues(12, "@me")
        assert mock_fetch.call_count == 2

    def test_handles_missing_cache_file(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "nonexistent.json")
        assert cache._cache == {}

    def test_handles_corrupt_cache_file(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")
        cache = GitHubCache(ttl=60, cache_file=cache_file)
        assert cache._cache == {}

    def test_ignores_legacy_status_fields_key(self, tmp_path):
        """Old _status_fields format should be silently ignored."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps({"_status_fields": {"@me/12": ["F1", "OPT1", "PVT_node"]}})
        )
        cache = GitHubCache(ttl=60, cache_file=cache_file)
        assert cache._field_infos == {}

    @patch("agent_hq.github.fetch_field_info")
    def test_get_field_info_caches_result(self, mock_fetch, tmp_path):
        mock_fetch.return_value = FieldInfo(
            field_id="F1", project_node_id="PVT_node", options={"Need": "OPT1"}
        )
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        info1 = cache.get_field_info(12, "@me", "Priority")
        info2 = cache.get_field_info(12, "@me", "Priority")
        assert info1 is not None
        assert info1.field_id == "F1"
        assert info2 is info1  # Same object from cache
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.fetch_project_issues")
    def test_preserves_cached_issues_on_fetch_timeout(self, mock_fetch, tmp_path):
        """When the underlying gh fetch raises ``TimeoutExpired``, the
        cache must keep the previous-good value rather than overwriting
        with [] (#90). A transient gh hang shouldn't blank the backlog.
        """
        cache = GitHubCache(ttl=1, cache_file=tmp_path / "c.json")
        good = [Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)]
        mock_fetch.return_value = good
        first = cache.get_issues(12, "@me")
        assert len(first) == 1

        # Make the cache stale and arrange for the next fetch to time out.
        import time as _time

        _time.sleep(1.1)
        mock_fetch.side_effect = GitHubFetchError("project @me/12: gh timed out after 10s")

        result = cache.get_issues(12, "@me")
        # Stale-but-non-empty wins over a fresh empty.
        assert len(result) == 1
        assert result[0].number == 1

    @patch("agent_hq.github.fetch_project_issues")
    def test_returns_empty_when_no_prior_cache_and_timeout(self, mock_fetch, tmp_path):
        """No cached value to preserve → return ``[]`` rather than
        propagating the fetch error up into the route handler (callers
        read ``fetch_status()`` to learn it was a failure, not an empty
        backlog — #254)."""
        mock_fetch.side_effect = GitHubFetchError("project @me/12: gh timed out after 10s")
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "c.json")
        assert cache.get_issues(12, "@me") == []

    def test_concurrent_get_issues_does_not_corrupt_cache(self, tmp_path):
        """``_load_issues`` calls ``cache.get_issues`` concurrently across
        N projects via ``asyncio.gather`` + threadpool workers. Without the
        lock, concurrent ``_save_to_disk`` calls interleave and the last
        writer truncates the others — the disk snapshot loses entries (and
        the dict iteration can raise ``RuntimeError: dictionary changed size
        during iteration``). Either way the final snapshot must contain every
        entry; this test fails (0 on reload) if the RLock is removed."""
        import threading

        cache_file = tmp_path / "c.json"
        cache = GitHubCache(ttl=60, cache_file=cache_file)

        n = 30
        barrier = threading.Barrier(n)

        def fake_fetch(num: int, owner: str) -> list[Issue]:
            barrier.wait()  # all threads enter the critical section together
            return [
                Issue(
                    number=num,
                    title=f"T{num}",
                    repo="o/r",
                    status="Todo",
                    priority=Priority.NEED,
                )
            ]

        errors: list[BaseException] = []

        def worker(num: int) -> None:
            try:
                cache.get_issues(num, "@me")
            except BaseException as e:  # noqa: BLE001 — capture for assertion
                errors.append(e)

        with patch("agent_hq.github.fetch_project_issues", side_effect=fake_fetch):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors, f"concurrent get_issues raised: {errors!r}"
        assert len(cache._cache) == n
        # Final disk snapshot must contain every entry — the last writer to
        # acquire the lock serializes the full dict.
        reloaded = GitHubCache(ttl=60, cache_file=cache_file)
        assert len(reloaded._cache) == n

    @patch("agent_hq.github.fetch_field_info")
    def test_field_info_persists_to_disk(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = FieldInfo(
            field_id="F1",
            project_node_id="PVT_node",
            options={"Need": "OPT1", "Want": "OPT2"},
        )
        cache1 = GitHubCache(ttl=60, cache_file=cache_file)
        cache1.get_field_info(12, "@me", "Priority")

        # New instance loads from disk
        cache2 = GitHubCache(ttl=60, cache_file=cache_file)
        info = cache2.get_field_info(12, "@me", "Priority")
        assert info is not None
        assert info.options == {"Need": "OPT1", "Want": "OPT2"}
        assert mock_fetch.call_count == 1  # Only fetched once


class TestGitHubCacheStaleReads:
    """get_issues_cached / get_closed_keys_cached never fetch (#226 SWR)."""

    def _issue(self, number: int = 1) -> Issue:
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    def test_get_issues_cached_none_when_never_fetched(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        issues, fresh = cache.get_issues_cached(12, "@me")
        assert issues is None
        assert fresh is False

    @patch("agent_hq.github.fetch_project_issues")
    def test_get_issues_cached_fresh_true_within_ttl(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        issues, fresh = cache.get_issues_cached(12, "@me")
        assert issues is not None and len(issues) == 1
        assert fresh is True

    def test_get_issues_cached_returns_expired_with_fresh_false(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache._cache["@me/12"] = (time.time() - 3600, [self._issue()])
        issues, fresh = cache.get_issues_cached(12, "@me")
        assert issues is not None and issues[0].number == 1
        assert fresh is False

    @patch("agent_hq.github.fetch_project_issues")
    def test_get_issues_cached_never_invokes_fetch(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = AssertionError("must not fetch")
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache._cache["@me/12"] = (time.time() - 3600, [self._issue()])
        cache.get_issues_cached(12, "@me")
        cache.get_issues_cached(99, "@me")

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_get_closed_keys_cached_union_and_missing_repo_not_fresh(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = AssertionError("must not fetch")
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache._closed["o/r"] = (time.time(), {"o/r#1"})
        keys, all_fresh = cache.get_closed_keys_cached(["o/r", "o/r2"])
        assert keys == {"o/r#1"}
        assert all_fresh is False

    def test_get_closed_keys_cached_all_fresh_when_every_repo_cached(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache._closed["o/r"] = (time.time(), {"o/r#1"})
        cache._closed["o/r2"] = (time.time(), {"o/r2#7"})
        keys, all_fresh = cache.get_closed_keys_cached(["o/r", "o/r2"])
        assert keys == {"o/r#1", "o/r2#7"}
        assert all_fresh is True

    def test_get_closed_keys_cached_expired_repo_contributes_but_not_fresh(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        cache._closed["o/r"] = (time.time() - 3600, {"o/r#1"})
        keys, all_fresh = cache.get_closed_keys_cached(["o/r"])
        assert keys == {"o/r#1"}
        assert all_fresh is False


class TestGitHubCacheSetStatus:
    @patch("agent_hq.github.set_field_value", return_value=True)
    @patch("agent_hq.github.fetch_field_info")
    def test_sets_status_value(self, mock_fetch_field, mock_set, tmp_path):
        mock_fetch_field.return_value = FieldInfo(
            field_id="F2",
            project_node_id="PVT_node",
            options={"Todo": "OPT_T", "In Progress": "OPT_IP", "Done": "OPT_D"},
        )
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        issue = Issue(
            number=42,
            title="T",
            repo="o/r",
            status="Todo",
            priority=Priority.NEED,
            item_id="ITEM_1",
            project_number=12,
        )
        assert cache.set_status(issue, "@me", "In Progress") is True
        mock_set.assert_called_once_with("PVT_node", "ITEM_1", "F2", "OPT_IP")

    @patch("agent_hq.github.fetch_field_info")
    def test_returns_false_for_unknown_status(self, mock_fetch_field, tmp_path):
        mock_fetch_field.return_value = FieldInfo(
            field_id="F2",
            project_node_id="PVT_node",
            options={"Todo": "OPT_T", "In Progress": "OPT_IP"},
        )
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        issue = Issue(
            number=42,
            title="T",
            repo="o/r",
            status="Todo",
            priority=Priority.NEED,
            item_id="ITEM_1",
            project_number=12,
        )
        assert cache.set_status(issue, "@me", "Nonexistent") is False

    def test_returns_false_when_no_item_id(self, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "cache.json")
        issue = Issue(number=42, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        assert cache.set_status(issue, "@me", "In Progress") is False


class TestCreateIssue:
    @patch("agent_hq.github.subprocess.run")
    def test_returns_url_from_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/foo/issues/42\n",
            stderr="",
        )
        url = create_issue("ben-ralston/foo", "Title here", "Body here")
        assert url == "https://github.com/ben-ralston/foo/issues/42"

    @patch("agent_hq.github.subprocess.run")
    def test_passes_labels_when_provided(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/foo/issues/1",
            stderr="",
        )
        create_issue(
            "ben-ralston/foo",
            "T",
            "B",
            labels=["idea-inbox", "needs-triage"],
        )
        call_args = mock_run.call_args[0][0]
        assert "--label" in call_args
        assert "idea-inbox" in call_args
        assert "needs-triage" in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_omits_label_flag_when_no_labels(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/foo/issues/1",
            stderr="",
        )
        create_issue("ben-ralston/foo", "T", "B")
        call_args = mock_run.call_args[0][0]
        assert "--label" not in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_raises_gh_error_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="HTTP 422: Validation failed"
        )
        with pytest.raises(GHError, match="create"):
            create_issue("ben-ralston/foo", "T", "B")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_gh_error_when_gh_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(GHError, match="gh CLI not found"):
            create_issue("ben-ralston/foo", "T", "B")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_when_stdout_lacks_url(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="weird output\n", stderr="")
        with pytest.raises(GHError, match="URL"):
            create_issue("ben-ralston/foo", "T", "B")

    @patch("agent_hq.github.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run):
        """#182: a hung mutating gh call must not wedge its thread forever."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/foo/issues/1",
            stderr="",
        )
        create_issue("ben-ralston/foo", "T", "B")
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_raises_gh_error_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with pytest.raises(GHError, match="timed out"):
            create_issue("ben-ralston/foo", "T", "B")


class TestAddToProject:
    @patch("agent_hq.github.subprocess.run")
    def test_returns_item_id(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"id": "PVTI_lADO_xyz", "title": "T"}),
            stderr="",
        )
        item_id = add_to_project(12, "@me", "https://github.com/ben-ralston/foo/issues/42")
        assert item_id == "PVTI_lADO_xyz"
        call_args = mock_run.call_args[0][0]
        assert "project" in call_args
        assert "item-add" in call_args
        assert "12" in call_args
        assert "--owner" in call_args
        assert "@me" in call_args
        assert "--url" in call_args
        assert "https://github.com/ben-ralston/foo/issues/42" in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_called_process_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        with pytest.raises(GHError, match="project-add"):
            add_to_project(12, "@me", "https://github.com/x/y/issues/1")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_when_response_lacks_id(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        with pytest.raises(GHError):
            add_to_project(12, "@me", "https://github.com/x/y/issues/1")

    @patch("agent_hq.github.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"id": "PVTI_x"}), stderr=""
        )
        add_to_project(12, "@me", "https://github.com/x/y/issues/1")
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_raises_gh_error_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with pytest.raises(GHError, match="timed out"):
            add_to_project(12, "@me", "https://github.com/x/y/issues/1")


class TestFetchRecentIssuesForTone:
    @patch("agent_hq.github.subprocess.run")
    def test_returns_title_body_pairs(self, mock_run):
        payload = json.dumps(
            [
                {"title": "Add dashboard", "body": "Need a TUI"},
                {"title": "Fix typo", "body": ""},
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
        recent = fetch_recent_issues_for_tone("ben-ralston/foo", limit=5)
        assert recent == [
            {"title": "Add dashboard", "body": "Need a TUI"},
            {"title": "Fix typo", "body": ""},
        ]
        call_args = mock_run.call_args[0][0]
        assert "issue" in call_args
        assert "list" in call_args
        assert "--repo" in call_args
        assert "ben-ralston/foo" in call_args
        assert "--limit" in call_args
        assert "5" in call_args
        assert "--json" in call_args
        assert "title,body" in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_seeds_only_from_the_owners_own_issues(self, mock_run):
        """Anyone can open issues in a public repo; only the owner's own
        issues may seed the drafting prompt (#263 F8)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        fetch_recent_issues_for_tone("o/r")
        call_args = mock_run.call_args[0][0]
        assert call_args[call_args.index("--author") + 1] == "@me"

    @patch("agent_hq.github.subprocess.run")
    def test_returns_empty_on_called_process_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert fetch_recent_issues_for_tone("o/r") == []

    @patch("agent_hq.github.subprocess.run")
    def test_returns_empty_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        assert fetch_recent_issues_for_tone("o/r") == []


class TestTransferIssue:
    @patch("agent_hq.github.subprocess.run")
    def test_returns_new_url(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/bar/issues/7\n",
            stderr="",
        )
        url = transfer_issue(42, "ben-ralston/foo", "ben-ralston/bar")
        assert url == "https://github.com/ben-ralston/bar/issues/7"
        call_args = mock_run.call_args[0][0]
        assert "issue" in call_args
        assert "transfer" in call_args
        assert "42" in call_args
        assert "ben-ralston/bar" in call_args
        # `gh issue transfer` reads source repo via -R / --repo
        assert "--repo" in call_args
        assert "ben-ralston/foo" in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="forbidden")
        with pytest.raises(GHError, match="transfer"):
            transfer_issue(42, "ben-ralston/foo", "ben-ralston/bar")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_when_stdout_lacks_url(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="moved.\n", stderr="")
        with pytest.raises(GHError, match="URL"):
            transfer_issue(42, "ben-ralston/foo", "ben-ralston/bar")

    @patch("agent_hq.github.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/ben-ralston/bar/issues/7",
            stderr="",
        )
        transfer_issue(42, "ben-ralston/foo", "ben-ralston/bar")
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_raises_gh_error_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with pytest.raises(GHError, match="timed out"):
            transfer_issue(42, "ben-ralston/foo", "ben-ralston/bar")


class TestCloseIssueNotPlanned:
    @patch("agent_hq.github.subprocess.run")
    def test_closes_with_not_planned_reason(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert close_issue_not_planned("ben-ralston/agent-hq", 70) is True
        call_args = mock_run.call_args[0][0]
        assert call_args[:3] == ["gh", "issue", "close"]
        assert "70" in call_args
        assert "--repo" in call_args
        assert "ben-ralston/agent-hq" in call_args
        assert "--reason" in call_args
        assert "not planned" in call_args

    @patch("agent_hq.github.subprocess.run")
    def test_returns_false_on_called_process_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="nope")
        assert close_issue_not_planned("o/r", 1) is False

    @patch("agent_hq.github.subprocess.run")
    def test_returns_false_when_gh_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        assert close_issue_not_planned("o/r", 1) is False

    @patch("agent_hq.github.subprocess.run")
    def test_passes_subprocess_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        close_issue_not_planned("o/r", 1)
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @patch("agent_hq.github.subprocess.run")
    def test_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        assert close_issue_not_planned("o/r", 1) is False


class TestFetchClosedIssueKeys:
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_returns_repo_scoped_keys(self, mock_run):
        """Closed state rides the same REST index row as the timestamps —
        one call now answers both questions (#255)."""
        mock_run.return_value = _rest_page(
            _rest_row(1, state="closed"),
            _rest_row(7),
            _rest_row(42, state="closed"),
        )
        keys = fetch_closed_issue_keys("ben-ralston/agent-hq")
        assert keys == {"ben-ralston/agent-hq#1", "ben-ralston/agent-hq#42"}
        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["gh", "api"]
        assert "repos/ben-ralston/agent-hq/issues" in argv[2]

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_a_closed_issue_stays_closed_beyond_the_old_500_window(self, mock_run):
        """The cumulative index retires `gh issue list --limit 500`'s caveat:
        an old closed issue pinned to a board used to fall outside the cap and
        reappear in the backlog."""
        mock_run.return_value = _rest_page(_rest_row(3, state="closed"))
        assert fetch_closed_issue_keys("o/r") == {"o/r#3"}
        # A later incremental sync that reports nothing new must not forget it.
        mock_run.return_value = _rest_page()
        assert fetch_closed_issue_keys("o/r", force=True) == {"o/r#3"}

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_raises_on_error(self, mock_run):
        # An empty set would hide nothing and make closed issues reappear —
        # a failure has to be distinguishable from "no closed issues" (#254).
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(GitHubFetchError):
            fetch_closed_issue_keys("o/r")

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_raises_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        with pytest.raises(GitHubFetchError):
            fetch_closed_issue_keys("o/r")


class TestGitHubCacheClosedKeys:
    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_fetches_and_caches_per_repo(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = lambda repo, force=False: {f"{repo}#1"}
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "c.json")
        assert cache.get_closed_keys(["o/a", "o/b"]) == {"o/a#1", "o/b#1"}
        assert mock_fetch.call_count == 2
        # Second call within TTL is a cache hit — no new fetches.
        assert cache.get_closed_keys(["o/a", "o/b"]) == {"o/a#1", "o/b#1"}
        assert mock_fetch.call_count == 2

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_force_bypasses_cache(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = lambda repo, force=False: {f"{repo}#1"}
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "c.json")
        cache.get_closed_keys(["o/a"])
        cache.get_closed_keys(["o/a"], force=True)
        assert mock_fetch.call_count == 2

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_mark_closed_primes_cache(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = lambda repo, force=False: set()
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "c.json")
        cache.get_closed_keys(["ben-ralston/agent-hq"])
        cache.mark_closed("ben-ralston/agent-hq", 70)
        # No refetch (still within TTL) but the primed key is present.
        assert "ben-ralston/agent-hq#70" in cache.get_closed_keys(["ben-ralston/agent-hq"])
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_mark_closed_without_prior_fetch(self, mock_fetch, tmp_path):
        cache = GitHubCache(ttl=60, cache_file=tmp_path / "c.json")
        cache.mark_closed("o/r", 5)
        assert "o/r#5" in cache.get_closed_keys(["o/r"])
        mock_fetch.assert_not_called()


# --- #254: fetch failures must not poison the cache -----------------------


class TestFetchProjectIssuesFailureContract:
    """``fetch_project_issues`` signals "I could not fetch" as a raised
    ``GitHubFetchError`` — never as an empty list, which is
    indistinguishable from a genuinely empty project (#254)."""

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_called_process_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(stdout="not json")
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")

    @patch("agent_hq.github.subprocess.run")
    def test_raises_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with pytest.raises(GitHubFetchError):
            fetch_project_issues(12, "@me")

    @patch("agent_hq.github.subprocess.run")
    def test_rate_limit_flagged_with_reset_time(self, mock_run):

        def _run(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "rate_limit"]:
                # `limit` is part of every real rate_limit response; the
                # sampled probe needs it to tell a bucket from a partial
                # read, so the fixture has to carry it too.
                return MagicMock(
                    stdout=json.dumps(
                        {
                            "resources": {
                                "core": {"remaining": 4999, "limit": 5000, "reset": 111},
                                "graphql": {"remaining": 0, "limit": 5000, "reset": 999},
                            }
                        }
                    )
                )
            raise subprocess.CalledProcessError(
                1, "gh", stderr="GraphQL: API rate limit already exceeded for user ID 1."
            )

        mock_run.side_effect = _run
        with pytest.raises(GitHubFetchError) as exc_info:
            fetch_project_issues(12, "@me")
        assert exc_info.value.rate_limited is True
        assert exc_info.value.reset_at == 999

    @patch("agent_hq.github.subprocess.run")
    def test_non_rate_limit_failure_not_flagged(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="could not resolve host"
        )
        with pytest.raises(GitHubFetchError) as exc_info:
            fetch_project_issues(12, "@me")
        assert exc_info.value.rate_limited is False
        assert exc_info.value.reset_at is None


class TestSiblingFetcherFailureContract:
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_closed_keys_raises_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        with pytest.raises(GitHubFetchError):
            fetch_closed_issue_keys("o/r")

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_timestamps_raise_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        with pytest.raises(GitHubFetchError):
            _sync_issue_index("o/r")

    @patch("agent_hq.github.subprocess.run")
    def test_field_info_raises_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        with pytest.raises(GitHubFetchError):
            fetch_field_info(12, "@me", "Priority")

    @patch("agent_hq.github._sync_issue_index")
    def test_enrich_dates_degrades_when_timestamps_fail(self, mock_ts):
        """A failed timestamp fetch degrades the date sort — it must not
        blow up the whole project fetch."""
        mock_ts.side_effect = GitHubFetchError("timestamps: boom")
        issues = [Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)]
        _enrich_dates(issues)
        assert issues[0].updated_at is None


class TestCacheIsCrossProcessCoordinated:
    """github.json is the coordination point, not a warm-start seed (#255).

    ``_load_from_disk`` used to run exactly once, in ``__init__``, so each
    process's in-memory dict was authoritative forever: nine live processes
    meant nine independent TTL clocks all hammering the same API, and the
    last one to write won. These tests pin the fix — mtime reload, per-key
    single-flight, and a merging atomic write.
    """

    def _issue(self, number, repo="o/r"):
        return Issue(number=number, title="T", repo=repo, status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.fetch_project_issues")
    def test_a_peers_write_is_picked_up_without_refetching(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "github.json"
        mock_fetch.return_value = [self._issue(1)]
        first = GitHubCache(ttl=300, cache_file=cache_file)
        assert len(first.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1

        # A second process, started after the first wrote.
        second = GitHubCache(ttl=300, cache_file=cache_file)
        assert len(second.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1

        # And one that was already running when the write landed.
        third = GitHubCache(ttl=300, cache_file=tmp_path / "other.json")
        third._cache_file = cache_file
        assert len(third.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.fetch_project_issues")
    def test_concurrent_instances_fetch_once(self, mock_fetch, tmp_path):
        """The acceptance criterion: N processes, one fetch."""
        cache_file = tmp_path / "github.json"
        calls = []

        def _slow(project_number, owner):
            calls.append((project_number, owner))
            time.sleep(0.05)
            return [self._issue(1)]

        mock_fetch.side_effect = _slow
        caches = [GitHubCache(ttl=300, cache_file=cache_file) for _ in range(3)]
        results = []
        threads = [
            threading.Thread(target=lambda c=c: results.append(c.get_issues(12, "@me")))
            for c in caches
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(calls) == 1
        assert all(len(r) == 1 for r in results)

    @patch("agent_hq.github.fetch_project_issues")
    def test_different_projects_still_fetch_in_parallel(self, mock_fetch, tmp_path):
        """Locks are per-key on purpose.

        A single global lock would serialize the six concurrent project
        fetches _load_issues runs via asyncio.gather, turning cold-load
        latency from max-of-N back into sum-of-N and regressing #90. The
        barrier only clears if both fetches are genuinely in flight at once.
        """
        cache_file = tmp_path / "github.json"
        barrier = threading.Barrier(2, timeout=5)

        def _fetch(project_number, owner):
            barrier.wait()
            return [self._issue(project_number)]

        mock_fetch.side_effect = _fetch
        cache = GitHubCache(ttl=300, cache_file=cache_file)
        errors = []

        def _go(number):
            try:
                cache.get_issues(number, "@me")
            except Exception as exc:  # BrokenBarrierError if serialized
                errors.append(exc)

        threads = [threading.Thread(target=_go, args=(n,)) for n in (12, 13)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"per-key locks serialized distinct projects: {errors}"

    @patch("agent_hq.github.fetch_project_issues")
    def test_a_write_does_not_clobber_a_peers_other_project(self, mock_fetch, tmp_path):
        """Whole-file writes are the reason github.json was last-writer-wins."""
        cache_file = tmp_path / "github.json"
        a = GitHubCache(ttl=300, cache_file=cache_file)
        b = GitHubCache(ttl=300, cache_file=cache_file)

        mock_fetch.return_value = [self._issue(1)]
        a.get_issues(12, "@me")
        mock_fetch.return_value = [self._issue(2)]
        b.get_issues(99, "@me")

        on_disk = json.loads(cache_file.read_text())
        assert "@me/12" in on_disk
        assert "@me/99" in on_disk

    @patch("agent_hq.github.fetch_project_issues")
    def test_save_is_atomic(self, mock_fetch, tmp_path):
        """A concurrent reader sees the old file or the new one, never a
        half-written one — the torn-read bug in the old plain write_text."""
        cache_file = tmp_path / "github.json"
        mock_fetch.return_value = [self._issue(1)]
        cache = GitHubCache(ttl=300, cache_file=cache_file)
        cache.get_issues(12, "@me")
        assert json.loads(cache_file.read_text())  # parses
        assert not list(tmp_path.glob("*.tmp")), "temp file left behind"

    @patch("agent_hq.github.fetch_project_issues")
    def test_a_stale_disk_file_does_not_roll_back_a_fresh_fetch(self, mock_fetch, tmp_path):
        """Merge is newest-timestamp-wins, so reloading can't undo our work."""
        cache_file = tmp_path / "github.json"
        mock_fetch.return_value = [self._issue(1)]
        a = GitHubCache(ttl=300, cache_file=cache_file)
        a.get_issues(12, "@me")

        b = GitHubCache(ttl=300, cache_file=cache_file)
        mock_fetch.return_value = [self._issue(1), self._issue(2)]
        assert len(b.get_issues(12, "@me", force=True)) == 2
        # a re-reads the file; b's newer entry must win over a's older one.
        assert len(a.get_issues(12, "@me")) == 2


class TestObservedRejectionsFeedTheBudget:
    """The ground-truth tier has to actually be fed (#255).

    `gh api rate_limit` cannot be trusted to report exhaustion — measured
    2026-08-26, 19 of 30 reads reported a full budget while GraphQL was
    provably dead. An observed rejection is the only fully reliable signal,
    so `_fetch_failure` — the single funnel where every rate-limit rejection
    is recognised — reports it.
    """

    def test_a_rate_limited_failure_marks_the_budget_exhausted(self):
        assert gh_budget.is_exhausted() is False
        exc = subprocess.CalledProcessError(
            1, "gh", stderr="GraphQL: API rate limit already exceeded for user ID 1."
        )
        with patch("agent_hq.github._rate_limit_reset", return_value=time.time() + 600):
            _fetch_failure("project @me/12", exc)
        assert gh_budget.is_exhausted() is True

    def test_exhaustion_then_blocks_background_refreshes(self):
        """The gate's short-circuit is unreachable unless this wiring exists."""
        exc = subprocess.CalledProcessError(1, "gh", stderr="API rate limit exceeded")
        with patch("agent_hq.github._rate_limit_reset", return_value=time.time() + 600):
            _fetch_failure("project @me/12", exc)
        assert gh_budget.allow_background_refresh(500) is False

    def test_an_ordinary_failure_does_not_mark_exhaustion(self):
        exc = subprocess.CalledProcessError(1, "gh", stderr="could not resolve host")
        _fetch_failure("project @me/12", exc)
        assert gh_budget.is_exhausted() is False


class TestRateLimitResetDelegates:
    """One rate_limit probe in the codebase, and one that survives the
    endpoint's replica inconsistency (#255; flagged on PR #257)."""

    def test_prefers_an_observed_rejections_reset(self):
        # Must be in the future — a window whose reset has passed is not
        # exhaustion any more, which is the point of exhausted_until().
        reset = time.time() + 600
        gh_budget.note_rate_limited(reset_at=reset)
        assert _rate_limit_reset() == reset

    @patch("agent_hq.github.gh_budget.snapshot")
    def test_falls_back_to_the_sampled_snapshot(self, snapshot):
        snapshot.return_value = SimpleNamespace(exhausted_reset=999.0, graphql_reset=111.0)
        assert _rate_limit_reset() == 999.0

    @patch("agent_hq.github.gh_budget.snapshot", return_value=None)
    def test_unknown_budget_is_none_not_an_error(self, _snapshot):
        assert _rate_limit_reset() is None

    @patch("agent_hq.gh_meter.subprocess.run")
    @patch("agent_hq.github.gh_budget.snapshot", return_value=None)
    def test_does_not_run_its_own_rate_limit_call(self, _snapshot, mock_run):
        _rate_limit_reset()
        assert not mock_run.called, "a second, weaker rate_limit probe survives"


class TestBudgetGating:
    """Background refreshes stop before the budget does (#255).

    The user's rule: the floor blocks TTL-driven and SWR background
    refreshes only. An explicit refresh always fetches — the user is present
    and deliberately spending budget — and writes are never gated.
    """

    def _issue(self, number=1):
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_low_budget_serves_cached_without_fetching(self, mock_fetch, _allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            assert len(cache.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1
        # TTL is 0, so this would normally refetch.
        assert len(cache.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_a_cold_start_fetches_even_when_low(self, mock_fetch, _allow, tmp_path):
        """There is nothing to degrade to. An empty backlog forever is worse
        than one more call, and it is the state a fresh process boots into."""
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        assert len(cache.get_issues(12, "@me")) == 1
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_explicit_refresh_still_fetches(self, mock_fetch, _allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        cache.get_issues(12, "@me")
        cache.get_issues(12, "@me", force=True)
        assert mock_fetch.call_count == 2

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_skip_is_reported_as_budget_not_as_a_failure(self, mock_fetch, _allow, tmp_path):
        """Nothing broke — saying "rate limit exceeded" would be a lie."""
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        cache.get_issues(12, "@me")
        status = cache.fetch_status()
        assert status.ok is False
        assert status.budget_skipped is True
        assert status.rate_limited is False

    @patch("agent_hq.github.fetch_project_issues")
    def test_a_successful_refresh_clears_the_skip(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False):
            cache.get_issues(12, "@me")
            assert cache.fetch_status().budget_skipped is True
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        assert cache.fetch_status().ok is True

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_a_real_failure_outranks_a_budget_skip(self, mock_fetch, _allow, tmp_path):
        """Something breaking is more urgent than something being conserved."""
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        cache.get_issues(12, "@me")  # budget skip on @me/12
        mock_fetch.side_effect = GitHubFetchError("project 99: boom")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(99, "@me")
        status = cache.fetch_status()
        assert status.ok is False
        assert status.budget_skipped is False
        assert "boom" in (status.message or "")

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False)
    @patch("agent_hq.github.fetch_project_issues")
    def test_skip_emits_a_warn(self, mock_fetch, _allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        with patch("agent_hq.github.eventlog.emit") as emit:
            cache.get_issues(12, "@me")
        assert emit.called
        level, message = emit.call_args.args[0], emit.call_args.args[1]
        assert level == "warn"
        assert message == "github refresh skipped, budget low"

    @patch("agent_hq.github.gh_budget.allow_background_refresh")
    @patch("agent_hq.github.fetch_project_issues")
    def test_the_configured_floor_is_passed_through(self, mock_fetch, allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        allow.return_value = True
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json", graphql_floor=1200)
        cache.get_issues(12, "@me")
        cache.get_issues(12, "@me")
        assert allow.call_args.args[0] == 1200


class TestCachePreservesOnFailure:
    @patch("agent_hq.github.fetch_project_issues")
    def test_failure_preserves_previous_issues(self, mock_fetch, tmp_path):
        good = [Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)]
        mock_fetch.return_value = good
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "cache.json")
        assert len(cache.get_issues(12, "@me")) == 1

        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        assert [i.number for i in cache.get_issues(12, "@me")] == [1]

    @patch("agent_hq.github.fetch_project_issues")
    def test_failure_keeps_original_timestamp(self, mock_fetch, tmp_path):
        """The cache entry's timestamp must NOT move on a failed refresh —
        a fresh stamp would make stale data look current and suppress the
        next refresh for a full TTL."""
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        cache = GitHubCache(ttl=0, cache_file=cache_file)
        cache.get_issues(12, "@me")
        before = json.loads(cache_file.read_text())["@me/12"]["timestamp"]

        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        cache.get_issues(12, "@me")
        after = json.loads(cache_file.read_text())["@me/12"]["timestamp"]
        assert after == before
        assert len(json.loads(cache_file.read_text())["@me/12"]["issues"]) == 1

    @patch("agent_hq.github.fetch_project_issues")
    def test_genuine_empty_response_is_cached(self, mock_fetch, tmp_path):
        cache_file = tmp_path / "cache.json"
        mock_fetch.return_value = []
        cache = GitHubCache(ttl=300, cache_file=cache_file)
        assert cache.get_issues(12, "@me") == []
        assert json.loads(cache_file.read_text())["@me/12"]["issues"] == []
        assert cache.fetch_status().ok is True

    @patch("agent_hq.github.fetch_project_issues")
    def test_failure_with_no_cache_returns_empty(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "cache.json")
        assert cache.get_issues(12, "@me") == []

    @patch("agent_hq.github.fetch_project_issues")
    def test_fetch_status_reports_rate_limit_and_reset(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = GitHubFetchError(
            "item-list: GitHub API rate limit exceeded",
            rate_limited=True,
            reset_at=time.time() + 1800,
        )
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        status = cache.fetch_status()
        assert status.ok is False
        assert status.rate_limited is True
        assert status.reset_at is not None
        assert "rate limit" in status.message.lower()

    @patch("agent_hq.github.fetch_project_issues")
    def test_fetch_status_exposes_stale_age(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        cache.get_issues(12, "@me")
        status = cache.fetch_status()
        assert status.stale_since is not None
        assert status.stale_since <= time.time()

    @patch("agent_hq.github.fetch_project_issues")
    def test_successful_refetch_clears_failure(self, mock_fetch, tmp_path):
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "cache.json")
        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        cache.get_issues(12, "@me")
        assert cache.fetch_status().ok is False

        mock_fetch.side_effect = None
        mock_fetch.return_value = []
        cache.get_issues(12, "@me")
        assert cache.fetch_status().ok is True

    @patch("agent_hq.github.eventlog.emit")
    @patch("agent_hq.github.fetch_project_issues")
    def test_failure_emits_eventlog_warn(self, mock_fetch, mock_emit, tmp_path):
        mock_fetch.side_effect = GitHubFetchError("item-list: boom")
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        assert mock_emit.called
        level, message = mock_emit.call_args[0][0], mock_emit.call_args[0][1]
        assert level == "warn"
        assert "github" in message.lower()

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_closed_keys_failure_preserves_previous(self, mock_fetch, tmp_path):
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "cache.json")
        mock_fetch.return_value = {"o/r#9"}
        assert cache.get_closed_keys(["o/r"]) == {"o/r#9"}

        mock_fetch.side_effect = GitHubFetchError("closed-list: boom")
        assert cache.get_closed_keys(["o/r"]) == {"o/r#9"}
        assert cache.fetch_status().ok is False

    @patch("agent_hq.github.fetch_closed_issue_keys")
    @patch("agent_hq.github.fetch_project_issues")
    def test_stale_age_falls_back_to_cached_issues(self, mock_fetch, mock_closed, tmp_path):
        """A closed-keys-only failure must still report the age of the data on
        screen. The failing key ("closed:o/r") has no entry in the issue cache,
        and reporting "no cached issues" while the list is full is a lie."""
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "cache.json")
        cache.get_issues(12, "@me")
        mock_closed.side_effect = GitHubFetchError("closed issues for o/r: boom")
        cache.get_closed_keys(["o/r"])
        status = cache.fetch_status()
        assert status.ok is False
        assert status.stale_since is not None


class TestFailureBookkeepingLifecycle:
    """Review follow-ups (#254): a recorded failure must not outlive its
    usefulness, and a failed refresh must still throttle the next attempt."""

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_closed_failure_still_throttles_refetch(self, mock_fetch, tmp_path):
        """Before #254 a failed closed-keys fetch cached ``set()`` with a fresh
        stamp, which suppressed refetch for a TTL. Skipping the write entirely
        would leave the repo permanently "missing" → every request re-runs a
        10s-timeout ``gh issue list`` against an already-rate-limited API."""
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        cache.get_closed_keys(["o/r"])
        cache.get_closed_keys(["o/r"])
        assert mock_fetch.call_count == 1
        assert cache.has_closed_entries(["o/r"]) is True

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_closed_failure_keeps_previous_keys(self, mock_fetch, tmp_path):
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        mock_fetch.return_value = {"o/r#9"}
        cache.get_closed_keys(["o/r"])
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        assert cache.get_closed_keys(["o/r"]) == {"o/r#9"}

    @patch("agent_hq.github.fetch_project_issues")
    def test_failure_expires_when_key_is_never_fetched_again(self, mock_fetch, tmp_path):
        """A failing key can leave the fetch set for good — a repo whose last
        board issue got closed, or a project dropped from projects.yaml. Its
        failure would otherwise pin the banner on over perfectly fresh data."""
        mock_fetch.side_effect = GitHubFetchError("project @me/12: boom")
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        cache.get_issues(12, "@me")
        assert cache.fetch_status().ok is False

        with patch("agent_hq.github.time.time", return_value=time.time() + 4000):
            assert cache.fetch_status().ok is True

    @patch("agent_hq.github.fetch_project_issues")
    def test_fresh_cached_entry_clears_a_stale_failure(self, mock_fetch, tmp_path):
        """A forced refresh that fails right after a successful one must not
        label the still-fresh data "unreachable — showing cached issues from
        under 1m ago"."""
        mock_fetch.return_value = [
            Issue(number=1, title="T", repo="o/r", status="Todo", priority=Priority.NEED)
        ]
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        cache.get_issues(12, "@me")
        mock_fetch.side_effect = GitHubFetchError("project @me/12: boom")
        cache.get_issues(12, "@me", force=True)
        assert cache.fetch_status().ok is False

        cache.get_issues(12, "@me")  # TTL-fresh serve
        assert cache.fetch_status().ok is True

    @patch("agent_hq.github.subprocess.run")
    def test_rate_limit_probe_does_not_block_concurrent_callers(self, mock_run):
        """The probe must not pin every threadpool worker for its full 10s
        timeout — only the one thread actually probing."""
        import threading

        started = threading.Event()
        release = threading.Event()

        def _run(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "rate_limit"]:
                started.set()
                release.wait(5)
                return MagicMock(stdout='{"resources":{"graphql":{"remaining":0,"reset":42}}}')
            raise subprocess.CalledProcessError(1, "gh", stderr="API rate limit exceeded")

        mock_run.side_effect = _run
        slow = threading.Thread(target=lambda: _rate_limit_reset())
        slow.start()
        assert started.wait(5)
        # Second caller must return immediately rather than queue behind the probe.
        done = threading.Event()
        threading.Thread(target=lambda: (_rate_limit_reset(), done.set())).start()
        assert done.wait(2), "second caller blocked on the in-flight probe"
        release.set()
        slow.join(5)


class TestClosedFailurePreservesAge:
    """Codex review follow-up (#254): the retry cooldown must live on the
    failure record, not on a re-stamped data entry — re-stamping made
    ``fetch_status().stale_since`` claim hours-old closed keys were fetched
    under a minute ago."""

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_closed_failure_keeps_original_timestamp(self, mock_fetch, tmp_path):
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        old_ts = time.time() - 7200
        cache._closed["o/r"] = (old_ts, {"o/r#9"})
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        assert cache.get_closed_keys(["o/r"]) == {"o/r#9"}
        assert cache._closed["o/r"][0] == old_ts
        assert cache.fetch_status().stale_since == old_ts

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_warm_closed_failure_still_throttles_refetch(self, mock_fetch, tmp_path):
        """With the data stamp left alone (and therefore TTL-expired), the
        cooldown on the failure record is all that stands between a failing
        repo and a 10s-timeout ``gh issue list`` per request."""
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        cache._closed["o/r"] = (time.time() - 7200, {"o/r#9"})
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        cache.get_closed_keys(["o/r"])
        cache.get_closed_keys(["o/r"])
        assert mock_fetch.call_count == 1

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_forced_closed_refresh_bypasses_cooldown(self, mock_fetch, tmp_path):
        """The explicit refresh button must actually retry."""
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        cache.get_closed_keys(["o/r"])
        mock_fetch.side_effect = None
        mock_fetch.return_value = {"o/r#9"}
        assert cache.get_closed_keys(["o/r"], force=True) == {"o/r#9"}

    @patch("agent_hq.github.fetch_closed_issue_keys")
    def test_fresh_closed_entry_clears_a_stale_failure(self, mock_fetch, tmp_path):
        """Mirror of the get_issues rule: a forced refresh that fails right
        after a successful one must not pin the banner on over TTL-fresh
        keys that are what is actually being served."""
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        mock_fetch.return_value = {"o/r#9"}
        cache.get_closed_keys(["o/r"])
        mock_fetch.side_effect = GitHubFetchError("closed issues for o/r: boom")
        cache.get_closed_keys(["o/r"], force=True)
        assert cache.fetch_status().ok is False
        mock_fetch.reset_mock()
        assert cache.get_closed_keys(["o/r"]) == {"o/r#9"}
        assert mock_fetch.call_count == 0
        assert cache.fetch_status().ok is True


class TestIssueIndexSyncIsSingleFlighted:
    """One index fetch per repo at a time, across threads and processes.

    ``_sync_issue_index`` read ``synced_at`` and then fetched, both unlocked.
    On a cold start the per-repo threadpool in ``_enrich_dates`` and
    ``get_closed_keys`` all saw an absent stamp and each launched a full
    seed — up to 30 REST pages apiece — and so did every other agent-hq
    process. The debounce only ever collapsed the *steady state*, which is
    not the path that costs calls (#255 review).
    """

    def test_concurrent_syncs_fetch_once(self, tmp_path, monkeypatch):
        import threading
        import time as _time

        from agent_hq import gh_index
        from agent_hq import github as github_mod

        monkeypatch.setattr(gh_index, "_DEFAULT_INDEX_FILE", tmp_path / "index.json")
        github_mod.clear_issue_index_cache()

        calls = []
        barrier_passed = threading.Event()

        def _slow_sync(repo, index, **kwargs):
            calls.append(repo)
            _time.sleep(0.3)  # hold the lock long enough for the peer to queue
            index.merge(repo, [], seeded=True, synced_at=_time.time())
            barrier_passed.set()

        monkeypatch.setattr(gh_index, "sync_repo", _slow_sync)

        threads = [
            threading.Thread(target=lambda: github_mod._sync_issue_index("o/r")) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert calls == ["o/r"], f"index seeded {len(calls)} times concurrently: {calls}"


class TestDiskReloadDoesNotMissAPeersWrite:
    """``_load_from_disk`` stat'd the file *after* reading it (#255 review).

    A peer's ``os.replace`` landing between the read and the stat stored the
    NEW mtime alongside the OLD content, so ``_reload_if_changed`` — which
    compares against exactly that mtime — never reloaded the version it had
    already skipped. The staleness was permanent, not transient: only the
    next write to the file could clear it. ``gh_index`` stats first; this
    pins that ordering here too.
    """

    def test_write_landing_mid_read_is_still_picked_up(self, tmp_path, monkeypatch):
        import os

        from agent_hq import github as github_mod

        cache_file = tmp_path / "github.json"
        cache = github_mod.GitHubCache(ttl=0, cache_file=cache_file)
        cache._cache["a/1"] = (100.0, [])
        cache._save_to_disk()

        peer = json.dumps(
            {"a/1": {"timestamp": 999.0, "issues": []}, "b/2": {"timestamp": 999.0, "issues": []}}
        )
        original_read = type(cache_file).read_text
        landed = {"done": False}

        def _read_then_peer_writes(self, *args, **kwargs):
            content = original_read(self, *args, **kwargs)
            if self == cache_file and not landed["done"]:
                landed["done"] = True
                # The peer's atomic replace lands *after* our read.
                cache_file.write_text(peer)
                os.utime(cache_file, (5_000_000.0, 5_000_000.0))
            return content

        monkeypatch.setattr(type(cache_file), "read_text", _read_then_peer_writes)
        cache._load_from_disk()
        monkeypatch.undo()

        # The peer's version must not be permanently invisible.
        cache._reload_if_changed()
        assert "b/2" in cache._cache, (
            "a peer write that landed mid-read was recorded as already-seen"
        )


class TestFreshDataClearsTheBudgetSkip:
    """A peer's successful refresh must clear our "budget low" notice (#255).

    ``_fresh_entry`` cleared the failure record but not the budget skip, so
    once a peer freshened the board we served *their* fresh data under a
    banner saying background refresh was paused — until the skip aged out on
    its own.
    """

    def test_serving_fresh_data_clears_the_skip(self, tmp_path):
        from agent_hq import github as github_mod

        cache = github_mod.GitHubCache(ttl=300, cache_file=tmp_path / "github.json")
        cache._record_budget_skip("o/12")
        assert cache.fetch_status().budget_skipped is True

        # A peer's write makes the entry genuinely fresh.
        cache._cache["o/12"] = (time.time(), [])
        assert cache._fresh_entry("o/12") == []

        assert cache.fetch_status().budget_skipped is False


class TestFetchStatusNeverProbesInline:
    """``fetch_status`` runs on the TUI's main thread (#255 review).

    ``DispatchScreen._apply_refresh`` reaches it via ``call_from_thread``, so
    anything blocking inside it freezes the whole TUI. In the budget-skipped
    state it wants the reset time for the toast — but the blocking
    ``gh_budget.snapshot`` can sit in ``_SAMPLES`` sequential ``gh``
    subprocesses when the memo has expired, which happens exactly when an
    observed rejection has been short-circuiting the meter. A status probe is
    a display nicety; it must answer from the memo and never probe inline.
    """

    def _issue(self, number=1):
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.fetch_project_issues")
    def test_budget_skipped_status_uses_the_non_blocking_accessor(self, mock_fetch, tmp_path):
        from agent_hq import gh_budget

        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            cache.get_issues(12, "@me")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False):
            cache.get_issues(12, "@me")  # records a budget skip

        def _boom():  # pragma: no cover - only runs if the wiring regresses
            raise AssertionError("blocking snapshot() called from fetch_status()")

        budget = gh_budget.RateBudget(
            graphql_remaining=100,
            graphql_limit=5000,
            graphql_reset=1_900_000_000.0,
            core_remaining=5000,
            core_limit=5000,
            core_reset=None,
            exhausted_reset=None,
        )
        with (
            patch("agent_hq.github.gh_budget.snapshot", _boom),
            patch("agent_hq.github.gh_budget.snapshot_cached", return_value=budget),
        ):
            status = cache.fetch_status()
        assert status.budget_skipped is True
        assert status.reset_at == 1_900_000_000.0


class TestQueuedFetchesRespectObservedExhaustion:
    """The in-lock recheck covers freshness but not the budget (#255 review).

    Several background callers pass the gate together and queue on one
    project's fetch lock. The first is rejected and records exhaustion —
    ground truth — yet every waiter then fetches anyway, because a failed
    fetch does not freshen the entry. The recheck is free: ``is_exhausted``
    is an in-memory read, never a probe.
    """

    def _issue(self, number=1):
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True)
    @patch("agent_hq.github.fetch_project_issues")
    def test_exhaustion_noticed_while_queued_skips_the_fetch(self, mock_fetch, _allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        cache.get_issues(12, "@me")
        assert mock_fetch.call_count == 1
        # Exhaustion lands "while we were queued" — i.e. after the gate,
        # before the lock. Simulate by making it visible only now.
        with patch("agent_hq.github.gh_budget.is_exhausted", return_value=True):
            issues = cache.get_issues(12, "@me")
        assert len(issues) == 1
        assert mock_fetch.call_count == 1
        assert cache.fetch_status().budget_skipped is True

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True)
    @patch("agent_hq.github.fetch_project_issues")
    def test_explicit_refresh_ignores_the_recheck(self, mock_fetch, _allow, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache = GitHubCache(ttl=0, cache_file=tmp_path / "c.json")
        cache.get_issues(12, "@me")
        with patch("agent_hq.github.gh_budget.is_exhausted", return_value=True):
            cache.get_issues(12, "@me", force=True)
        assert mock_fetch.call_count == 2


class TestFailedSyncFallsBackToTheDurableIndex:
    """The index IS the previous-good data; a failed sync must not hide it.

    ``_safe_timestamps_for`` returned ``{}`` and ``get_closed_keys`` seeded
    an empty set for a cold repo whenever the REST sync failed — while rows
    for that repo sat on disk. Restart while GitHub is unreachable and every
    known-closed issue reappears (#255 review).
    """

    def _seed_index(self):
        from agent_hq import github as github_mod

        index = github_mod._get_issue_index()
        index.merge(
            "o/r",
            [
                {"number": 1, "state": "closed", "created_at": "2026-01-01T00:00:00Z",
                 "updated_at": "2026-01-02T00:00:00Z"},
                {"number": 2, "state": "open", "created_at": "2026-01-03T00:00:00Z",
                 "updated_at": "2026-01-04T00:00:00Z"},
            ],
            seeded=True,
            synced_at=1.0,  # long ago, so the debounce does not mask the sync
        )

    def test_timestamps_survive_a_failed_sync(self):
        from agent_hq import github as github_mod

        self._seed_index()
        with patch(
            "agent_hq.github.gh_index.sync_repo",
            side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="boom"),
        ):
            ts = github_mod._safe_timestamps_for("o/r")
        assert ts[2]["created_at"] == "2026-01-03T00:00:00Z"

    def test_closed_keys_survive_a_failed_sync_on_a_cold_repo(self, tmp_path):
        self._seed_index()
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        with patch(
            "agent_hq.github.gh_index.sync_repo",
            side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="boom"),
        ):
            keys = cache.get_closed_keys({"o/r"})
        assert keys == {"o/r#1"}
        # Still a failure as far as the banner is concerned.
        assert cache.fetch_status().ok is False


class TestRestRejectionsDoNotStandDownGraphQL:
    """``note_rate_limited`` is ground truth *for the GraphQL bucket*.

    ``_fetch_failure`` is the funnel for every fetcher, and the REST index
    now goes through it too — so a core-bucket (or secondary) rejection on
    ``gh api repos/…`` was recorded as GraphQL exhaustion and stood every
    background board refresh down for ten minutes while that budget was
    untouched (#255 review).
    """

    def test_a_rest_rejection_is_reported_but_not_noted(self):
        from agent_hq import gh_budget
        from agent_hq import github as github_mod

        exc = subprocess.CalledProcessError(
            1, ["gh", "api", "repos/o/r/issues"], stderr="API rate limit exceeded for user"
        )
        with patch("agent_hq.github.gh_index.sync_repo", side_effect=exc):
            with pytest.raises(GitHubFetchError) as info:
                github_mod._sync_issue_index_locked("o/r", github_mod._get_issue_index())
        assert info.value.rate_limited is True
        assert gh_budget.is_exhausted() is False

    def test_a_graphql_rejection_is_noted(self):
        from agent_hq import gh_budget
        from agent_hq import github as github_mod

        exc = subprocess.CalledProcessError(
            1, ["gh", "api", "graphql"], stderr="API rate limit already exceeded"
        )
        with patch("agent_hq.github.gh_board.fetch_board_items", side_effect=exc):
            with pytest.raises(GitHubFetchError):
                github_mod.fetch_project_issues(12, "@me")
        assert gh_budget.is_exhausted() is True


class TestLockFailuresDegradeLikeDiskFailures:
    """An unwritable lock directory must not turn a fetch into a crash.

    ``main``'s ``_save_to_disk`` swallowed every OSError, so an unwritable
    cache dir degraded to in-memory. The fetch and write locks were added
    outside that handler, and the TUI worker has no handler of its own —
    its dispatch refresh died as a Worker.ERROR with no toast (#255 review).
    """

    def _issue(self, number=1):
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True)
    @patch("agent_hq.github.fetch_project_issues")
    def test_get_issues_still_answers_when_locks_cannot_be_created(
        self, mock_fetch, _allow, tmp_path
    ):
        mock_fetch.return_value = [self._issue()]
        # A FILE where the locks directory should be: mkdir raises.
        blocker = tmp_path / "locks"
        blocker.write_text("not a directory")
        cache = GitHubCache(ttl=300, cache_file=tmp_path / "c.json")
        with patch.object(cache, "_write_lock_path", return_value=blocker / "w.lock"):
            issues = cache.get_issues(12, "@me")
        assert len(issues) == 1
        assert mock_fetch.call_count == 1


class TestSwrReadsClearStaleWarnings:
    """A peer's fresh board arriving via ``get_issues_cached`` must clear the
    skip and failure recorded against that key, exactly as ``_fresh_entry``
    does — the web SWR path never calls ``get_issues`` for a fresh entry, so
    the "budget low" banner outlived the condition it described (#255 review).
    """

    def _issue(self, number=1):
        return Issue(number=number, title="T", repo="o/r", status="Todo", priority=Priority.NEED)

    @patch("agent_hq.github.fetch_project_issues")
    def test_fresh_peer_data_clears_the_budget_skip(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [self._issue()]
        cache_file = tmp_path / "c.json"
        mine = GitHubCache(ttl=0, cache_file=cache_file)
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            mine.get_issues(12, "@me")
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=False):
            mine.get_issues(12, "@me")
        assert mine.fetch_status().budget_skipped is True

        peer = GitHubCache(ttl=300, cache_file=cache_file)
        with patch("agent_hq.github.gh_budget.allow_background_refresh", return_value=True):
            peer.get_issues(12, "@me", force=True)

        mine._ttl = 300
        issues, fresh = mine.get_issues_cached(12, "@me")
        assert fresh and issues
        assert mine.fetch_status().ok is True


def test_comment_issue_routes_through_run_gh(monkeypatch):
    calls = []
    monkeypatch.setattr(gh_meter, "run_gh", lambda argv, **kw: calls.append((argv, kw)) or None)
    assert github.comment_issue("o/r", 7, "Duplicate of #3") is True
    argv, kw = calls[0]
    assert argv == ["gh", "issue", "comment", "7", "--repo", "o/r", "--body", "Duplicate of #3"]
    assert kw["kind"] == "issue-comment"


def test_comment_issue_is_best_effort(monkeypatch):
    import subprocess

    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr="nope")

    monkeypatch.setattr(gh_meter, "run_gh", boom)
    assert github.comment_issue("o/r", 7, "x") is False


class TestOpenIssueKeys:
    """#273: the off-board detector's view of the durable index."""

    def _seed(self, tmp_path, monkeypatch, rows):
        from agent_hq import gh_index

        monkeypatch.setattr(gh_index, "_DEFAULT_INDEX_FILE", tmp_path / "index.json")
        github.clear_issue_index_cache()
        if rows is not None:
            github._get_issue_index().merge("o/r", rows, seeded=True, synced_at=time.time())

    @patch("agent_hq.github._sync_issue_index")
    def test_returns_open_keys_after_a_sync(self, mock_sync, tmp_path, monkeypatch):
        self._seed(
            tmp_path,
            monkeypatch,
            [
                {"number": 1, "state": "open", "updated_at": "2026-01-02T00:00:00Z"},
                {"number": 2, "state": "closed", "updated_at": "2026-01-02T00:00:00Z"},
            ],
        )
        assert github.open_issue_keys("o/r") == {"o/r#1"}
        mock_sync.assert_called_once_with("o/r")

    @patch("agent_hq.github._sync_issue_index")
    def test_unseeded_repo_is_none(self, mock_sync, tmp_path, monkeypatch):
        """None, not an empty set: an empty set would read as "every issue is
        on the board" and resolve real findings."""
        self._seed(tmp_path, monkeypatch, None)
        mock_sync.side_effect = GitHubFetchError("index: boom")
        assert github.open_issue_keys("o/r") is None

    @patch("agent_hq.github._sync_issue_index")
    def test_failed_sync_is_unchecked(self, mock_sync, tmp_path, monkeypatch):
        """Review round 9: a failed sync (rate limit, or an auth-scoped 404
        that also makes every REST lookup read "missing") must not let the
        stale index pass as a checked repo — its findings would resolve."""
        self._seed(
            tmp_path,
            monkeypatch,
            [{"number": 5, "state": "open", "updated_at": "2026-01-02T00:00:00Z"}],
        )
        mock_sync.side_effect = GitHubFetchError("index: boom")
        assert github.open_issue_keys("o/r") is None


class TestFetchIssueSummary:
    """#273: REST title/state for an issue the board cache does not hold."""

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_parses_title_and_state(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"title": "Fix bug", "state": "open"}), stderr=""
        )
        assert github.fetch_issue_summary("o/r", 42) == {
            "title": "Fix bug",
            "state": "open",
            "is_pr": False,
        }
        argv = mock_run.call_args.args[0]
        assert argv[:2] == ["gh", "api"] and "repos/o/r/issues/42" in argv

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="boom")
        assert github.fetch_issue_summary("o/r", 42) is None


class TestOpenIssueRows:
    """#273 review round 3: the detector needs creation dates to skip issues
    newer than the board snapshot, and the snapshot's own timestamp."""

    @patch("agent_hq.github._sync_issue_index")
    def test_rows_carry_created_at(self, mock_sync, tmp_path, monkeypatch):
        from agent_hq import gh_index

        monkeypatch.setattr(gh_index, "_DEFAULT_INDEX_FILE", tmp_path / "index.json")
        github.clear_issue_index_cache()
        github._get_issue_index().merge(
            "o/r",
            [
                {"number": 1, "state": "open", "created_at": "2026-01-01T00:00:00Z",
                 "updated_at": "2026-01-02T00:00:00Z"},
                {"number": 2, "state": "closed", "created_at": "2026-01-01T00:00:00Z",
                 "updated_at": "2026-01-02T00:00:00Z"},
            ],
            seeded=True,
            synced_at=time.time(),
        )
        assert github.open_issue_rows("o/r") == {"o/r#1": "2026-01-01T00:00:00Z"}
        assert github.open_issue_keys("o/r") == {"o/r#1"}

    def test_cached_at_is_the_entry_timestamp(self, tmp_path):
        cache = GitHubCache(cache_file=tmp_path / "c.json")
        assert cache.cached_at(1, "o") is None
        with patch("agent_hq.github.fetch_project_issues", return_value=[]):
            cache.get_issues(1, "o", force=True)
        stamp = cache.cached_at(1, "o")
        assert stamp is not None and time.time() - stamp < 5


class TestFetchIssueSummaryMissing:
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_404_is_reported_as_missing(self, mock_run):
        """#273 review round 5: a transferred or deleted issue stays open in
        the index until the daily reseed; a 404 is a verdict, not a transport
        failure, so the detector can drop it."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="gh: Not Found (HTTP 404)"
        )
        assert github.fetch_issue_summary("o/r", 42) == {"title": "", "state": "missing"}


class TestFetchIssueSummary404Match:
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_a_404_elsewhere_in_stderr_is_not_a_verdict(self, mock_run):
        """Review round 10: only gh's own ``HTTP 404`` is "missing"."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="gh: request id 404abc failed: HTTP 502"
        )
        assert github.fetch_issue_summary("o/r", 42) is None


# --- #414: remote issue writes (create / comment / close) ------------------------


class TestFetchIssueSummaryIsPr:
    @patch("agent_hq.gh_meter.subprocess.run")
    def test_flags_a_pull_request(self, mock_run):
        """#414: repos/{r}/issues/{n} serves PRs too; only PRs carry pull_request."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "A PR", "state": "open", "pull_request": {"url": "x"}}),
            stderr="",
        )
        assert github.fetch_issue_summary("o/r", 7)["is_pr"] is True

    @patch("agent_hq.gh_meter.subprocess.run")
    def test_an_issue_is_not_a_pr(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"title": "Bug", "state": "open"}), stderr=""
        )
        assert github.fetch_issue_summary("o/r", 7)["is_pr"] is False


class TestFetchLabelNames:
    def test_pages_by_hand_until_a_short_page(self, monkeypatch):
        calls = []
        pages = [[{"name": f"l{i}"} for i in range(100)], [{"name": "bug"}]]

        def fake(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(stdout=json.dumps(pages[len(calls) - 1]))

        monkeypatch.setattr(gh_meter, "run_gh", fake)
        names = github.fetch_label_names("o/r")
        assert names is not None and "bug" in names and len(names) == 101
        assert calls[0][0] == ["gh", "api", "repos/o/r/labels?per_page=100&page=1"]
        assert calls[1][0][-1].endswith("page=2")
        assert calls[0][1]["kind"] == "label-list"

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.CalledProcessError(1, "gh", stderr="boom"),
            subprocess.TimeoutExpired("gh", 10),
            FileNotFoundError("gh"),
        ],
    )
    def test_none_on_failure(self, monkeypatch, exc):
        def boom(argv, **kw):
            raise exc

        monkeypatch.setattr(gh_meter, "run_gh", boom)
        assert github.fetch_label_names("o/r") is None

    def test_none_on_an_error_envelope(self, monkeypatch):
        monkeypatch.setattr(
            gh_meter, "run_gh", lambda argv, **kw: SimpleNamespace(stdout='{"message": "x"}')
        )
        assert github.fetch_label_names("o/r") is None

    def test_unmocked_gh_is_none_not_a_crash(self):
        """conftest makes a real gh raise FileNotFoundError."""
        assert github.fetch_label_names("o/r") is None


class TestPostIssueComment:
    def test_returns_the_comment_url(self, monkeypatch):
        calls = []
        url = "https://github.com/o/r/issues/7#issuecomment-123"

        def fake(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(stdout=url + "\n")

        monkeypatch.setattr(gh_meter, "run_gh", fake)
        assert github.post_issue_comment("o/r", 7, "hello") == url
        argv, kw = calls[0]
        assert argv == ["gh", "issue", "comment", "7", "--repo", "o/r", "--body", "hello"]
        assert kw["kind"] == "issue-comment"

    def test_falls_back_to_the_issue_url(self, monkeypatch):
        monkeypatch.setattr(gh_meter, "run_gh", lambda argv, **kw: SimpleNamespace(stdout=""))
        assert github.post_issue_comment("o/r", 7, "x") == "https://github.com/o/r/issues/7"

    def test_failure_raises(self, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.CalledProcessError(1, argv, stderr="nope")

        monkeypatch.setattr(gh_meter, "run_gh", boom)
        with pytest.raises(github.GHError):
            github.post_issue_comment("o/r", 7, "x")

    def test_timeout_raises_the_ambiguous_error(self, monkeypatch):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)

        monkeypatch.setattr(gh_meter, "run_gh", slow)
        with pytest.raises(github.GHTimeoutError):
            github.post_issue_comment("o/r", 7, "x")


class TestCloseIssue:
    def _record(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gh_meter, "run_gh", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace()
        )
        return calls

    @pytest.mark.parametrize(
        "reason, flag", [("completed", "completed"), ("not_planned", "not planned")]
    )
    def test_reasons(self, monkeypatch, reason, flag):
        calls = self._record(monkeypatch)
        github.close_issue("o/r", 7, reason=reason, comment="done")
        argv, kw = calls[0]
        assert argv == [
            "gh",
            "issue",
            "close",
            "7",
            "--repo",
            "o/r",
            "--comment",
            "done",
            "--reason",
            flag,
        ]
        assert kw["kind"] == "issue-close"

    def test_duplicate_passes_duplicate_of_without_reason(self, monkeypatch):
        """gh rejects --reason other than duplicate alongside --duplicate-of."""
        calls = self._record(monkeypatch)
        dup = "https://github.com/o/r/issues/3"
        github.close_issue("o/r", 7, reason="duplicate", comment="dup", duplicate_of_url=dup)
        argv = calls[0][0]
        assert argv[-2:] == ["--duplicate-of", dup]
        assert "--reason" not in argv

    def test_bad_combinations_raise_value_error(self, monkeypatch):
        self._record(monkeypatch)
        with pytest.raises(ValueError):
            github.close_issue("o/r", 7, reason="duplicate", comment="x")
        with pytest.raises(ValueError):
            github.close_issue("o/r", 7, reason="completed", comment="x", duplicate_of_url="u")
        with pytest.raises(ValueError):
            github.close_issue("o/r", 7, reason="wontfix", comment="x")

    def test_failure_and_timeout(self, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.CalledProcessError(1, argv, stderr="nope")

        monkeypatch.setattr(gh_meter, "run_gh", boom)
        with pytest.raises(github.GHError):
            github.close_issue("o/r", 7, reason="completed", comment="x")

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)

        monkeypatch.setattr(gh_meter, "run_gh", slow)
        with pytest.raises(github.GHTimeoutError):
            github.close_issue("o/r", 7, reason="completed", comment="x")


def test_create_issue_timeout_is_the_ambiguous_error(monkeypatch):
    """#414: a timed-out create may still have filed the issue."""

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(gh_meter, "run_gh", slow)
    with pytest.raises(github.GHTimeoutError):
        github.create_issue("o/r", "t", "b")


def test_add_to_project_timeout_is_the_ambiguous_error(monkeypatch):
    """#414 review: an item-add that timed out may have landed."""

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(gh_meter, "run_gh", slow)
    with pytest.raises(github.GHTimeoutError):
        github.add_to_project(1, "@me", "https://github.com/o/r/issues/1")
