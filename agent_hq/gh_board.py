"""Narrow GraphQL board query — the largest single saving in #255.

``gh project item-list`` is a generic tool: it asks for every field value and
the full content object of every item, with generous internal page sizes. That
generality is expensive. Measured 2026-08-26 against board #12 (151 issues):

    gh project item-list --limit 200          203 GraphQL points
    this query, items(first: 100)               2 GraphQL points per page

Two pages cover that board, so **~4 points against 203** — roughly 50x. Board
queries are 96% of a refresh cycle, which makes this worth more than
everything else in the issue combined.

Why it matters beyond "cheaper": at 203 points x 6 projects, a *single*
agent-hq refreshing on a 300 s TTL costs ~14,600 points/hour against a
5,000/hour budget. Cross-process deduplication — the original centrepiece of
the #255 plan — would still have left one process ~3x over budget. The
exhaustion was not survivable without this change.

Output is deliberately shaped exactly like ``gh project item-list``'s JSON
items, so ``fetch_project_issues`` swaps its data source without touching its
parsing. Equivalence was verified field-by-field over the 100 overlapping
issues of board #12: zero mismatches on title, repository, priority, status,
labels, item id and body.

Failure handling composes with #254 rather than duplicating it: raw
subprocess/JSON exceptions propagate untouched (``_fetch_failure`` already
maps them), and a GraphQL error envelope — a 200 response carrying ``errors``,
which is how rate limiting arrives here — is raised as :class:`BoardQueryError`
carrying a ``.stderr`` attribute, the field ``_fetch_failure`` inspects for the
rate-limit wording.
"""

from __future__ import annotations

import json
import subprocess

from . import eventlog, gh_meter

# GitHub rejects `first:` above 100 on this connection outright, with
# EXCESSIVE_PAGINATION — `items(first: 200)` does not merely get truncated.
_PAGE_SIZE = 100

# Backstop against a server that always claims hasNextPage. 30 pages is 3,000
# board items, far past any real project.
_MAX_PAGES = 30

# Inner page sizes are chosen to cover real usage while keeping the node count
# (and therefore the point cost) down: the cost model multiplies `first:` args
# along each path, so these are the numbers that actually set the price.
_LABELS_PER_ITEM = 5
_FIELDS_PER_ITEM = 8

_ITEM_FIELDS = f"""
      items(first: {_PAGE_SIZE}, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          content {{
            ... on Issue {{
              number
              title
              body
              repository {{ nameWithOwner }}
              labels(first: {_LABELS_PER_ITEM}) {{ totalCount nodes {{ name }} }}
            }}
          }}
          fieldValues(first: {_FIELDS_PER_ITEM}) {{
            totalCount
            nodes {{
              ... on ProjectV2ItemFieldSingleSelectValue {{
                name
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
            }}
          }}
        }}
      }}
"""

BOARD_QUERY = f"""
query($num: Int!, $after: String) {{
  viewer {{
    projectV2(number: $num) {{{_ITEM_FIELDS}    }}
  }}
}}
"""

_BOARD_QUERY_USER = f"""
query($num: Int!, $login: String!, $after: String) {{
  user(login: $login) {{
    projectV2(number: $num) {{{_ITEM_FIELDS}    }}
  }}
}}
"""

# `gh project item-list --owner` resolved users AND organizations. GraphQL
# does not: `user(login:)` on an org login resolves to null with a "Could not
# resolve to a User" error, so routing every explicit owner through it broke
# org-owned boards permanently (#255 review). There is no top-level field
# returning a ProjectV2Owner by login, so the owner's type is discovered by
# trying `user` and falling back to `organization` — once per owner.
_BOARD_QUERY_ORG = f"""
query($num: Int!, $login: String!, $after: String) {{
  organization(login: $login) {{
    projectV2(number: $num) {{{_ITEM_FIELDS}    }}
  }}
}}
"""

_QUERY_FOR_KIND = {
    "viewer": BOARD_QUERY,
    "user": _BOARD_QUERY_USER,
    "organization": _BOARD_QUERY_ORG,
}

# owner login -> "user" | "organization". An owner's type never changes, so
# one probe per process is enough.
_owner_kind_cache: dict[str, str] = {}


def clear_owner_kind_cache() -> None:
    """Forget resolved owner types (tests)."""
    _owner_kind_cache.clear()


# What a failed owner-type probe looks like. A wrong owner *type* is a
# GraphQL error, and gh reports GraphQL errors with a non-zero exit.
_OWNER_PROBE_FAILURES: tuple[type[BaseException], ...]


class BoardQueryError(RuntimeError):
    """A GraphQL error envelope came back instead of board data.

    Carries ``stderr`` so #254's ``_fetch_failure`` finds the rate-limit
    wording and populates ``reset_at`` — the same path a non-zero ``gh`` exit
    takes.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.stderr = message


_OWNER_PROBE_FAILURES = (BoardQueryError, subprocess.CalledProcessError)


def _run_page(project_number: int, owner: str, after: str | None, kind: str = "viewer") -> dict:
    """One page of board items. Raises on any failure."""
    use_viewer = kind == "viewer"
    argv = [
        "gh",
        "api",
        "graphql",
        "-F",
        f"num={project_number}",
        "-f",
        f"query={_QUERY_FOR_KIND[kind]}",
    ]
    if not use_viewer:
        argv.extend(["-f", f"login={owner}"])
    if after:
        argv.extend(["-f", f"after={after}"])

    # Raw subprocess/JSON exceptions propagate: _fetch_failure maps them.
    result = gh_meter.run_gh(argv, kind="project-board-query")
    payload = json.loads(result.stdout)

    errors = payload.get("errors")
    if errors:
        messages = "; ".join(
            str(e.get("message", e)) for e in errors if isinstance(e, dict)
        ) or str(errors)
        raise BoardQueryError(f"board query failed: {messages}")

    data = payload.get("data") or {}
    root = data.get(kind)
    project = (root or {}).get("projectV2")
    if project is None:
        # A null project with no error envelope means the board is gone or
        # invisible to this token — a failure, never "the board is empty".
        raise BoardQueryError(f"board query returned no project {owner}/{project_number}")
    return project.get("items") or {}


def _warn_if_truncated(number, repo, connection: dict, requested: int, what: str) -> None:
    """Emit one warn when a capped inner connection dropped values.

    The message is a stable literal (eventlog dedupes on it), so a whole
    board's worth of truncated items yields one warn, not one per item.
    """
    total = connection.get("totalCount")
    if not isinstance(total, int) or total <= requested:
        return
    eventlog.emit(
        "warn",
        "github board item fields truncated",
        {
            "connection": what,
            "requested": requested,
            "total": total,
            "issue": number,
            "repo": repo,
        },
    )


def _normalize(node: dict) -> dict | None:
    """Reshape one GraphQL node into a ``gh project item-list`` JSON item.

    ``None`` for anything that is not an Issue — draft items and pull requests
    have no ``number``, and the caller already filters on ``content.type``.
    """
    content = node.get("content") or {}
    number = content.get("number")
    if number is None:
        return None

    # Both inner connections are capped to keep the node count (and so the
    # point cost) down, and GraphQL truncates them in silence: a Priority
    # past the 8th field just becomes None, losing the backlog's sort, and a
    # 6th label just vanishes, losing sentinel detection. `totalCount` is the
    # only way to tell, so say so rather than degrade quietly (#255 review).
    _warn_if_truncated(
        number,
        content.get("repository", {}).get("nameWithOwner") if content.get("repository") else None,
        node.get("fieldValues") or {},
        _FIELDS_PER_ITEM,
        "fieldValues",
    )
    _warn_if_truncated(
        number,
        content.get("repository", {}).get("nameWithOwner") if content.get("repository") else None,
        content.get("labels") or {},
        _LABELS_PER_ITEM,
        "labels",
    )

    fields: dict[str, str | None] = {}
    for value in (node.get("fieldValues") or {}).get("nodes") or []:
        if not isinstance(value, dict):
            continue
        name = (value.get("field") or {}).get("name")
        if name:
            fields[name] = value.get("name")

    labels = [
        label["name"]
        for label in ((content.get("labels") or {}).get("nodes") or [])
        if isinstance(label, dict) and label.get("name")
    ]

    return {
        "id": node.get("id", ""),
        "content": {
            # item-list emits this discriminator and fetch_project_issues
            # filters on it; the typed GraphQL fragment already guarantees it.
            "type": "Issue",
            "number": number,
            "title": content.get("title", ""),
            "body": content.get("body") or "",
            "repository": ((content.get("repository") or {}).get("nameWithOwner") or ""),
        },
        "labels": labels,
        "priority": fields.get("Priority"),
        "status": fields.get("Status"),
    }


def fetch_board_items(project_number: int, owner: str) -> list[dict]:
    """All Issue items on a project board, in ``gh project item-list`` shape.

    Paginates until the board is exhausted. An empty board returns ``[]``; a
    board that cannot be read raises.
    """
    items: list[dict] = []
    after: str | None = None
    kind = "viewer" if owner in ("@me", "", None) else _owner_kind_cache.get(owner)
    complete = False
    for _ in range(_MAX_PAGES):
        if kind is None:
            # First page for an owner we have not classified: GraphQL splits
            # users and organizations, so try one and fall back to the other.
            # `gh api graphql` exits 1 when the response carries `errors`
            # (verified live 2026-09-10), so with run_gh's check=True the
            # user-not-found envelope arrives as CalledProcessError, NOT as
            # BoardQueryError. Catching only the latter left the org query
            # unreachable in production while the exit-0 fixtures passed
            # (#255 review, round 2). Both are caught; the envelope branch
            # stays for a gh that reports errors with a zero exit.
            try:
                connection = _run_page(project_number, owner, after, "user")
                kind = "user"
            except _OWNER_PROBE_FAILURES as user_error:
                try:
                    connection = _run_page(project_number, owner, after, "organization")
                except _OWNER_PROBE_FAILURES as org_error:
                    # Both failed, so the owner's type is still unknown and
                    # the FIRST error is the one that describes the board
                    # (wrong number, missing `project` scope, rate limit).
                    # Surfacing the org attempt's "Could not resolve to an
                    # Organization" would point a real user at the wrong
                    # problem (#255 review).
                    raise user_error from org_error
                kind = "organization"
            _owner_kind_cache[owner] = kind
        else:
            connection = _run_page(project_number, owner, after, kind)
        for node in connection.get("nodes") or []:
            normalized = _normalize(node) if isinstance(node, dict) else None
            if normalized is not None:
                items.append(normalized)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            complete = True
            break
        after = page_info.get("endCursor")
        if not after:
            # More pages exist but we cannot ask for them. Returning what we
            # have would hand GitHubCache a partial board, which it would
            # store over the previous good one and stamp fresh.
            raise BoardQueryError(
                f"board {owner}/{project_number} reports more pages but gave no cursor"
            )
    if not complete:
        raise BoardQueryError(
            f"board {owner}/{project_number} did not finish paginating within {_MAX_PAGES} pages"
        )
    return items
