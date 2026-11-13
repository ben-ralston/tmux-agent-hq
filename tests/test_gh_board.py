"""Tests for the narrow GraphQL board query (#255).

`gh project item-list --limit 200` costs 203 GraphQL points; this query costs 2
per 100-item page (measured 2026-08-26). Since board queries are 96% of a
refresh cycle, this is the single largest saving in the issue.
"""

import json
import subprocess

import pytest

from agent_hq import gh_board


def _item(number, title="t", repo="o/r", priority="Need", status="Ready", labels=(), body=""):
    return {
        "id": f"PVTI_{number}",
        "content": {
            "number": number,
            "title": title,
            "body": body,
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": n} for n in labels]},
        },
        "fieldValues": {
            "nodes": [
                {"name": priority, "field": {"name": "Priority"}},
                {"name": status, "field": {"name": "Status"}},
            ]
        },
    }


def _page(items, has_next=False, cursor=None):
    return {
        "data": {
            "viewer": {
                "projectV2": {
                    "items": {
                        "nodes": items,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    }
                }
            }
        }
    }


def _serve(*pages):
    bodies = list(pages)
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    _run.calls = calls
    return _run


def test_returns_items_in_item_list_shape(monkeypatch):
    """The output must slot into fetch_project_issues unchanged."""
    fake = _serve(_page([_item(7, title="Fix it", labels=["bug"])]))
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    items = gh_board.fetch_board_items(12, "@me")
    assert items == [
        {
            "id": "PVTI_7",
            "content": {
                "type": "Issue",
                "number": 7,
                "title": "Fix it",
                "body": "",
                "repository": "o/r",
            },
            "labels": ["bug"],
            "priority": "Need",
            "status": "Ready",
        }
    ]


def test_paginates_until_has_next_page_is_false(monkeypatch):
    fake = _serve(
        _page([_item(n) for n in range(1, 101)], has_next=True, cursor="CUR1"),
        _page([_item(101)], has_next=False),
    )
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    items = gh_board.fetch_board_items(12, "@me")
    assert len(items) == 101
    assert len(fake.calls) == 2


def test_second_page_sends_the_cursor(monkeypatch):
    fake = _serve(
        _page([_item(1)], has_next=True, cursor="CUR1"),
        _page([_item(2)], has_next=False),
    )
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    gh_board.fetch_board_items(12, "@me")
    assert any("CUR1" in str(arg) for arg in fake.calls[1])


def test_first_page_sends_no_cursor(monkeypatch):
    """No `-f after=` argument on page one.

    Checked as an argv pair rather than a substring: the query text itself
    contains `after: $after`, so a naive substring test always matches.
    """
    fake = _serve(_page([_item(1)]))
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    gh_board.fetch_board_items(12, "@me")
    assert not any(str(arg).startswith("after=") for arg in fake.calls[0])


def test_page_size_never_exceeds_the_connection_limit():
    """`items(first: 200)` is rejected outright with EXCESSIVE_PAGINATION."""
    assert gh_board._PAGE_SIZE <= 100
    assert "first: 100" in gh_board.BOARD_QUERY


def test_draft_and_pull_request_items_are_skipped(monkeypatch):
    """Only Issues become backlog rows; item-list filtered on content.type."""
    draft = {"id": "PVTI_d", "content": {}, "fieldValues": {"nodes": []}}
    fake = _serve(_page([draft, _item(3)]))
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    items = gh_board.fetch_board_items(12, "@me")
    assert [i["content"]["number"] for i in items] == [3]


def test_missing_field_values_become_none(monkeypatch):
    bare = _item(4)
    bare["fieldValues"] = {"nodes": []}
    fake = _serve(_page([bare]))
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    item = gh_board.fetch_board_items(12, "@me")[0]
    assert item["priority"] is None and item["status"] is None


def test_graphql_errors_raise_with_stderr_for_rate_limit_detection(monkeypatch):
    """A 200 response carrying `errors` is a failure, not an empty board.

    The raised exception exposes `.stderr` so #254's `_fetch_failure` picks up
    the rate-limit wording and populates reset_at.
    """
    body = {
        "data": {"viewer": {"projectV2": None}},
        "errors": [
            {"type": "RATE_LIMIT", "message": "API rate limit already exceeded for user ID 1."}
        ],
    }
    monkeypatch.setattr(
        gh_board.gh_meter,
        "run_gh",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr=""),
    )
    with pytest.raises(gh_board.BoardQueryError) as caught:
        gh_board.fetch_board_items(12, "@me")
    assert "rate limit" in caught.value.stderr.lower()


def test_a_missing_project_raises_rather_than_returning_empty(monkeypatch):
    body = {"data": {"viewer": {"projectV2": None}}}
    monkeypatch.setattr(
        gh_board.gh_meter,
        "run_gh",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr=""),
    )
    with pytest.raises(gh_board.BoardQueryError):
        gh_board.fetch_board_items(12, "@me")


def test_an_empty_board_returns_empty_not_an_error(monkeypatch):
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", _serve(_page([])))
    assert gh_board.fetch_board_items(12, "@me") == []


def test_subprocess_failures_propagate_untouched(monkeypatch):
    """#254's _fetch_failure already maps these; don't re-wrap them."""

    def _boom(argv, **k):
        raise subprocess.CalledProcessError(1, argv, stderr="boom")

    monkeypatch.setattr(gh_board.gh_meter, "run_gh", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        gh_board.fetch_board_items(12, "@me")


def test_pagination_is_bounded(monkeypatch):
    """A server always claiming hasNextPage must not loop forever.

    It must also not return the truncated prefix as if it were the whole
    board: GitHubCache would store that over the previous good data and
    stamp it fresh (#255 review). Hitting the cap is a failure.
    """
    monkeypatch.setattr(
        gh_board.gh_meter,
        "run_gh",
        _serve(_page([_item(1)], has_next=True, cursor="C")),
    )
    with pytest.raises(gh_board.BoardQueryError, match="did not finish paginating"):
        gh_board.fetch_board_items(12, "@me")


def _user_page(items, has_next=False, cursor=None):
    """A `user(login:)`-rooted response, as a non-@me owner returns."""
    body = _page(items, has_next, cursor)
    body["data"] = {"user": body["data"]["viewer"]}
    return body


def test_non_me_owner_uses_the_user_query(monkeypatch):
    fake = _serve(_user_page([_item(1)]))
    monkeypatch.setattr(gh_board.gh_meter, "run_gh", fake)
    items = gh_board.fetch_board_items(12, "someone-else")
    argv = " ".join(str(a) for a in fake.calls[0])
    assert "user(login:" in argv
    assert "login=someone-else" in argv
    assert len(items) == 1


def test_calls_are_metered_as_graphql(monkeypatch):
    from agent_hq import gh_meter

    monkeypatch.setattr(subprocess, "run", _serve(_page([_item(1)])))
    gh_board.fetch_board_items(12, "@me")
    summary = gh_meter.summarize(hours=1)
    assert summary.by_api.get("graphql") == 1
    assert "project-board-query" in summary.by_kind


def _root_page(root, items, has_next=False, cursor=None, total_fields=None, total_labels=None):
    """A page rooted at ``viewer`` / ``user`` / ``organization``."""
    nodes = []
    for it in items:
        node = json.loads(json.dumps(it))
        if total_fields is not None:
            node["fieldValues"]["totalCount"] = total_fields
        if total_labels is not None:
            node["content"]["labels"]["totalCount"] = total_labels
        nodes.append(node)
    return {
        "data": {
            root: {
                "projectV2": {
                    "items": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    }
                }
            }
        }
    }


def _error_page(message):
    return {"data": {"user": None}, "errors": [{"message": message}]}


class TestOrganizationOwnedBoards:
    """`gh project item-list --owner` resolved users AND organizations.

    Routing every explicit owner through `user(login:)` made an org-owned
    board fail on every refresh, permanently — config.py documents the owner
    as '"@me" or org name' (#255 review).
    """

    def setup_method(self):
        gh_board.clear_owner_kind_cache()

    def test_org_owner_falls_back_to_the_organization_query(self, monkeypatch):
        fake = _serve(
            _error_page("Could not resolve to a User with the login of 'acme'."),
            _root_page("organization", [_item(7)]),
        )
        monkeypatch.setattr(subprocess, "run", fake)

        items = gh_board.fetch_board_items(3, "acme")

        assert [i["content"]["number"] for i in items] == [7]
        queries = ["".join(a) for a in fake.calls]
        assert "user(login:" in queries[0]
        assert "organization(login:" in queries[1]

    def test_resolved_owner_kind_is_reused(self, monkeypatch):
        fake = _serve(
            _error_page("Could not resolve to a User with the login of 'acme'."),
            _root_page("organization", [_item(7)]),
        )
        monkeypatch.setattr(subprocess, "run", fake)
        gh_board.fetch_board_items(3, "acme")

        fake2 = _serve(_root_page("organization", [_item(8)]))
        monkeypatch.setattr(subprocess, "run", fake2)
        items = gh_board.fetch_board_items(3, "acme")

        assert [i["content"]["number"] for i in items] == [8]
        # No wasted `user` probe the second time round.
        assert "organization(login:" in "".join(fake2.calls[0])
        assert len(fake2.calls) == 1

    def test_user_owner_still_uses_the_user_query(self, monkeypatch):
        fake = _serve(_root_page("user", [_item(9)]))
        monkeypatch.setattr(subprocess, "run", fake)
        assert len(gh_board.fetch_board_items(3, "someuser")) == 1
        assert "user(login:" in "".join(fake.calls[0])


class TestIncompleteBoardsAreFailures:
    """A partial board must never replace a good one and be marked fresh."""

    def setup_method(self):
        gh_board.clear_owner_kind_cache()

    def test_exhausting_the_page_cap_raises(self, monkeypatch):
        # Every page claims another one follows, forever.
        monkeypatch.setattr(
            subprocess, "run", _serve(_root_page("viewer", [_item(1)], has_next=True, cursor="c"))
        )
        with pytest.raises(gh_board.BoardQueryError, match="did not finish paginating"):
            gh_board.fetch_board_items(3, "@me")

    def test_missing_cursor_mid_board_raises(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            _serve(_root_page("viewer", [_item(1)], has_next=True, cursor=None)),
        )
        with pytest.raises(gh_board.BoardQueryError, match="no cursor"):
            gh_board.fetch_board_items(3, "@me")


class TestNestedTruncationIsReported:
    """Labels stop at 5 and field values at 8; overflow is invisible.

    A Priority past the 8th field silently becomes None (losing the backlog's
    sort) and a 6th label silently disappears (losing sentinel detection), with
    no error of any kind. Measured 2026-09-07: real items use 5 of the 8 field
    slots, so this is a live margin, not a hypothetical (#255 review).
    """

    def setup_method(self):
        gh_board.clear_owner_kind_cache()

    def test_truncated_field_values_emit_a_warn(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            gh_board.eventlog, "emit", lambda lvl, msg, extra=None: seen.append((lvl, msg, extra))
        )
        monkeypatch.setattr(
            subprocess, "run", _serve(_root_page("viewer", [_item(1)], total_fields=12))
        )
        gh_board.fetch_board_items(3, "@me")

        assert seen, "silent truncation — no warn emitted"
        level, message, extra = seen[0]
        assert level == "warn"
        assert extra["total"] == 12

    def test_untruncated_items_stay_quiet(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            gh_board.eventlog, "emit", lambda lvl, msg, extra=None: seen.append(msg)
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            _serve(_root_page("viewer", [_item(1)], total_fields=2, total_labels=0)),
        )
        gh_board.fetch_board_items(3, "@me")
        assert seen == []


class TestOwnerKindFallbackKeepsTheRealError:
    """When BOTH the user and organization queries fail, the first error is
    the one that describes what happened (#255 review).

    The fallback exists to discover org logins, so it fires on any error from
    the ``user`` query. But when the owner really is a user and the *board*
    is the problem — wrong number, token without the ``project`` scope — the
    organization attempt fails too, and surfacing *its* "Could not resolve to
    an Organization" would send the user chasing the wrong thing entirely.
    """

    def setup_method(self):
        gh_board.clear_owner_kind_cache()

    def test_double_failure_raises_the_user_querys_error(self, monkeypatch):
        fake = _serve(
            _error_page("board query returned no project someuser/3"),
            _error_page("Could not resolve to an Organization with the login of 'someuser'."),
        )
        monkeypatch.setattr(subprocess, "run", fake)

        with pytest.raises(gh_board.BoardQueryError) as info:
            gh_board.fetch_board_items(3, "someuser")

        assert "no project someuser/3" in str(info.value)
        assert "Organization" not in str(info.value)
        # Nothing was learned about the owner, so the next call probes again.
        assert gh_board._owner_kind_cache == {}


class TestOrganizationFallbackSurvivesGhsExitCode:
    """``gh api graphql`` exits 1 when the response carries ``errors``.

    Verified live 2026-09-10: ``user(login: "github")`` prints the envelope
    to stdout, ``gh: Could not resolve to a User ...`` to stderr, and exits
    1. ``run_gh`` runs with ``check=True``, so production sees a
    ``CalledProcessError`` — never the exit-0 envelope the earlier org tests
    fed in. Catching only ``BoardQueryError`` meant the organization query
    was unreachable and every org-owned board failed on every refresh
    (#255 review, round 2).
    """

    def setup_method(self):
        gh_board.clear_owner_kind_cache()

    @staticmethod
    def _gh_like(*outcomes):
        """Serve each outcome in turn: a dict is a 200 body (exit 0); a
        string is a GraphQL error the way gh really reports it (exit 1)."""
        calls = []

        def _run(argv, **kwargs):
            calls.append(argv)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, str):
                raise subprocess.CalledProcessError(1, argv, output="", stderr=f"gh: {outcome}\n")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(outcome), stderr="")

        _run.calls = calls
        return _run

    def test_org_owner_falls_back_when_gh_exits_nonzero(self, monkeypatch):
        fake = self._gh_like(
            "Could not resolve to a User with the login of 'acme'.",
            _root_page("organization", [_item(7)]),
        )
        monkeypatch.setattr(subprocess, "run", fake)

        items = gh_board.fetch_board_items(3, "acme")

        assert [i["content"]["number"] for i in items] == [7]
        assert "organization(login:" in "".join(fake.calls[1])
        assert gh_board._owner_kind_cache == {"acme": "organization"}

    def test_double_nonzero_failure_raises_the_first_error(self, monkeypatch):
        fake = self._gh_like(
            "API rate limit already exceeded for user ID 1.",
            "Could not resolve to an Organization with the login of 'someuser'.",
        )
        monkeypatch.setattr(subprocess, "run", fake)

        with pytest.raises(subprocess.CalledProcessError) as info:
            gh_board.fetch_board_items(3, "someuser")

        # The rate-limit wording is what _fetch_failure needs to see.
        assert "rate limit" in info.value.stderr
        assert gh_board._owner_kind_cache == {}

    def test_viewer_boards_do_not_retry(self, monkeypatch):
        fake = self._gh_like("API rate limit already exceeded for user ID 1.")
        monkeypatch.setattr(subprocess, "run", fake)
        with pytest.raises(subprocess.CalledProcessError):
            gh_board.fetch_board_items(3, "@me")
        assert len(fake.calls) == 1
