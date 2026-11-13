"""Tests for the Idea Inbox HTTP routes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hq.config import (
    Config,
    DispatchConfig,
    IdeaInboxConfig,
    ProjectConfig,
    RepoConfig,
)
from agent_hq.idea.classifier import ClassifierAuthError
from agent_hq.idea.handler import EmptyTextError, FiledIdea, NoCandidatesError, ReroutedIdea
from agent_hq.web.app import create_app
from agent_hq.web.auth import (
    COOKIE_NAME,
    TokenManager,
    derive_cookie_value,
    install_auth_middleware,
)
from agent_hq.web.routes import idea as idea_routes


def _config() -> Config:
    return Config(
        repos={
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=Path("/tmp/agent-hq"),
                project="Agent HQ",
            ),
        },
        projects=[ProjectConfig(name="Agent HQ", number=12, owner="@me")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
        idea_inbox=IdeaInboxConfig(),
    )


def _filed_idea(**overrides) -> FiledIdea:
    base = dict(
        issue_url="https://github.com/ben-ralston/agent-hq/issues/42",
        issue_number=42,
        repo="ben-ralston/agent-hq",
        project="Agent HQ",
        title="T",
        confidence=0.9,
        needs_triage=False,
        warnings=[],
    )
    base.update(overrides)
    return FiledIdea(**base)


def _build_app(tmp_path, *, handler=None, config=None):
    """Mini app: auth middleware + idea routes only. Tests inject a mock handler."""
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = FastAPI()
    install_auth_middleware(app, tm)
    handler = handler or MagicMock()
    config = config or _config()
    idea_routes.register(app, handler=handler, config=config)
    return app, tm, handler


# --- POST /api/idea ----------------------------------------------------------


def test_post_idea_requires_auth(tmp_path):
    app, _, _ = _build_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/idea", json={"text": "hi"})
    assert r.status_code == 401


def test_post_idea_accepts_bearer_and_returns_filed_idea(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.return_value = _filed_idea()
    raw = tm.token_file.read_text()
    client = TestClient(app)

    r = client.post(
        "/api/idea",
        json={"text": "track agent state better"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["issue_number"] == 42
    assert body["repo"] == "ben-ralston/agent-hq"
    assert body["needs_triage"] is False
    assert body["warnings"] == []


def test_post_idea_accepts_cookie(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.return_value = _filed_idea()
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 200


def test_post_idea_passes_idempotency_key(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.return_value = _filed_idea()
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    client.post("/api/idea", json={"text": "x", "idempotency_key": "abc-123"})
    kwargs = handler.handle.call_args.kwargs
    assert kwargs["idempotency_key"] == "abc-123"
    # token_hash is non-empty
    assert kwargs["token_hash"]


def test_post_idea_empty_text_returns_422(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = EmptyTextError("empty")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "   "})
    assert r.status_code == 422


def test_post_idea_missing_text_returns_422(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={})
    assert r.status_code == 422
    handler.handle.assert_not_called()


def test_post_idea_no_anthropic_key_returns_503(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = ClassifierAuthError("MY_KEY missing")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 503
    assert "MY_KEY" in r.json().get("error", "")


def test_post_idea_rejected_key_returns_503_auth(tmp_path):
    """#300: a key the API rejects (401) is an auth problem the user fixes on
    the Mac — 503 like a missing key, not a 502 upstream failure."""
    from agent_hq.idea.classifier import ClassifierKeyRejectedError

    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = ClassifierKeyRejectedError("Anthropic API rejected the key")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 503
    assert "rejected the key" in r.json().get("error", "")


def test_post_idea_no_candidates_returns_400(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = NoCandidatesError("no project: in any repo")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 400


def test_post_idea_classifier_error_returns_502(tmp_path):
    from agent_hq.idea.classifier import ClassifierError

    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = ClassifierError("Anthropic API call failed: connection error")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 502


def test_post_idea_github_error_returns_502(tmp_path):
    from agent_hq.github import GHError

    app, tm, handler = _build_app(tmp_path)
    handler.handle.side_effect = GHError("gh issue create failed: rate limited")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "x"})
    assert r.status_code == 502
    assert "rate limited" in r.json().get("error", "")


# --- POST /api/idea/{n}/reroute ----------------------------------------------


def test_reroute_returns_new_url_and_warnings(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.reroute.return_value = ReroutedIdea(
        issue_url="https://github.com/ben-ralston/scratch/issues/9",
        warnings=["project-add failed: boom"],
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post(
        "/api/idea/42/reroute",
        json={
            "src_repo": "ben-ralston/agent-hq",
            "dst_repo": "ben-ralston/scratch",
            "dst_project": "Scratch",
        },
    )
    assert r.status_code == 200
    assert r.json()["issue_url"] == "https://github.com/ben-ralston/scratch/issues/9"
    assert r.json()["warnings"] == ["project-add failed: boom"]
    handler.reroute.assert_called_once_with(
        issue_number=42,
        src_repo="ben-ralston/agent-hq",
        dst_repo="ben-ralston/scratch",
        dst_project="Scratch",
    )


def test_reroute_unknown_dst_returns_400(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.reroute.side_effect = NoCandidatesError("not configured")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post(
        "/api/idea/42/reroute",
        json={"src_repo": "a/b", "dst_repo": "c/d", "dst_project": "X"},
    )
    assert r.status_code == 400


def test_reroute_requires_auth(tmp_path):
    app, _, _ = _build_app(tmp_path)
    client = TestClient(app)
    r = client.post(
        "/api/idea/42/reroute",
        json={"src_repo": "a/b", "dst_repo": "c/d", "dst_project": "X"},
    )
    assert r.status_code == 401


def test_reroute_github_error_returns_502(tmp_path):
    from agent_hq.github import GHError

    app, tm, handler = _build_app(tmp_path)
    handler.reroute.side_effect = GHError("gh issue transfer failed")
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post(
        "/api/idea/42/reroute",
        json={"src_repo": "a/b", "dst_repo": "c/d", "dst_project": "X"},
    )
    assert r.status_code == 502


# --- GET /inbox + GET /inbox/recent -------------------------------------------


def test_get_inbox_returns_html(tmp_path):
    app, tm, _ = _build_app(tmp_path)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Page should contain the input area
    assert "<textarea" in r.text.lower()


def test_inbox_recent_section_listens_for_refresh_event(tmp_path):
    """htmx special-cases the `load` trigger: it fires once at init and
    registers NO event listener, so `htmx.trigger(el, "load")` is a silent
    no-op. Programmatic refreshes (Show more, just-filed, reroute) must go
    through a custom `refresh-recent` event that gets a real listener."""
    app, tm, _ = _build_app(tmp_path)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    body = client.get("/inbox").text
    assert 'hx-trigger="load, refresh-recent, every 30s"' in body
    # No call site may trigger the special-cased "load" event.
    assert '"load")' not in body


def test_get_inbox_recent_returns_html_partial(tmp_path):
    """GET /inbox/recent returns just the recent-list partial (htmx target)."""
    app, tm, handler = _build_app(tmp_path)
    handler.list_recent.return_value = []
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_get_inbox_requires_auth(tmp_path):
    app, _, _ = _build_app(tmp_path)
    client = TestClient(app)
    r = client.get("/inbox", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- POST /inbox/dispatch ---------------------------------------------------


def _mk_dispatch_config(tmp_path: Path) -> Config:
    """Config with a repo entry for dispatch tests."""
    return Config(
        repos={
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=tmp_path / "agent-hq",
                project="Agent HQ",
            ),
        },
        projects=[ProjectConfig(name="Agent HQ", number=12, owner="@me")],
        worktree_root=tmp_path / "wt",
        dispatch=DispatchConfig(permission_mode="acceptEdits"),
        idea_inbox=IdeaInboxConfig(),
    )


@pytest.fixture
def authed_idea_client(tmp_path):
    """Authenticated TestClient backed by create_app for /inbox/dispatch tests."""
    cfg = _mk_dispatch_config(tmp_path)
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file, config=cfg, github_cache=None)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, app


def test_inbox_dispatch_github_schedules_dispatch(tmp_path, monkeypatch, authed_idea_client):
    """POST /inbox/dispatch with kind=github returns 200 + 'Dispatching' text
    and calls dispatch_via_claude with the right issue."""
    client, app = authed_idea_client

    captured = []

    def _fake_dispatch(batches, config, **kwargs):
        captured.extend(issue for batch in batches for issue in batch)
        return SimpleNamespace(success=True, branch="feature/131-x", warning=None, error=None)

    from agent_hq.web.routes import issues as issues_routes

    monkeypatch.setattr(issues_routes, "dispatch_via_claude", _fake_dispatch)

    r = client.post(
        "/inbox/dispatch",
        data={"kind": "github", "repo": "ben-ralston/agent-hq", "number": "131", "title": "Fix"},
    )

    assert r.status_code == 200
    assert "Dispatching" in r.text
    assert len(captured) == 1
    assert captured[0].number == 131
    assert captured[0].repo == "ben-ralston/agent-hq"


def test_inbox_dispatch_github_unconfigured_repo_400(authed_idea_client):
    """Repo not in config.repos → 400."""
    client, _ = authed_idea_client
    r = client.post(
        "/inbox/dispatch",
        data={"kind": "github", "repo": "x/y", "number": "1"},
    )
    assert r.status_code == 400


def test_inbox_dispatch_github_zero_number_400(authed_idea_client):
    """number=0 → 400."""
    client, _ = authed_idea_client
    r = client.post(
        "/inbox/dispatch",
        data={"kind": "github", "repo": "ben-ralston/agent-hq", "number": "0"},
    )
    assert r.status_code == 400


def test_inbox_dispatch_unknown_kind_400(authed_idea_client):
    """An unrecognized kind → 400."""
    client, _ = authed_idea_client
    r = client.post("/inbox/dispatch", data={"kind": "frobnicate"})
    assert r.status_code == 400


def test_inbox_dispatch_requires_auth(tmp_path):
    """A client without an auth cookie gets a 302 redirect (non-/api/ path)."""
    cfg = _mk_dispatch_config(tmp_path)
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file, config=cfg, github_cache=None)
    client = TestClient(app)
    r = client.post(
        "/inbox/dispatch",
        data={"kind": "github", "repo": "ben-ralston/agent-hq", "number": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 302


# --- GET /inbox/recent → Re-route options --------------


def _github_recent_idea():
    from agent_hq.idea.handler import RecentIdea

    return RecentIdea(
        issue_url="https://github.com/ben-ralston/agent-hq/issues/42",
        issue_number=42,
        repo="ben-ralston/agent-hq",
        project="Agent HQ",
        title="Track agent state better",
        needs_triage=False,
        created_at="2026-06-04T00:00:00Z",
        destination="github",
        item_key="ben-ralston/agent-hq#42",
    )


def test_inbox_recent_reroute_offers_no_personal_option(tmp_path, monkeypatch):
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    app, tm, handler = _build_app(tmp_path)
    handler.list_recent.return_value = [_github_recent_idea()]
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    assert "__personal__" not in r.text


# --- GET /inbox/recent dispatch buttons (#128) ---------------------------------


def test_inbox_recent_renders_dispatch_buttons(tmp_path, monkeypatch, authed_idea_client):
    """GET /inbox/recent renders an hx-post='/inbox/dispatch' button for each
    recent item (two GitHub issues) and the one with an active
    worktree renders a disabled 'Dispatched' button instead. #128"""
    from agent_hq.idea.handler import RecentIdea
    from agent_hq.web.routes import idea as idea_routes

    client, app = authed_idea_client

    # Stub detect_active_worktrees to return an empty set — so both buttons
    # render enabled initially. Then we verify the dispatched variant separately.
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())

    # Stub list_recent on the real IdeaHandler via monkeypatch (create_app
    # registers a real handler, not a MagicMock, so .return_value doesn't work).
    fake_recent = [
        RecentIdea(
            issue_url="https://github.com/ben-ralston/agent-hq/issues/42",
            issue_number=42,
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            title="Track agent state better",
            needs_triage=False,
            created_at="2026-06-04T00:00:00Z",
            destination="github",
            item_key="ben-ralston/agent-hq#42",
        ),
        RecentIdea(
            issue_url="https://github.com/ben-ralston/agent-hq/issues/43",
            issue_number=43,
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            title="Buy milk",
            needs_triage=False,
            created_at="2026-06-04T01:00:00Z",
            destination="github",
            item_key="ben-ralston/agent-hq#43",
        ),
    ]
    from agent_hq.idea import handler as handler_module

    monkeypatch.setattr(
        handler_module.IdeaHandler,
        "list_recent",
        lambda self, **kwargs: fake_recent,
    )

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    body = r.text

    # Both items have a dispatch button.
    assert 'hx-post="/inbox/dispatch"' in body
    assert body.count("recent-item__dispatch") >= 2

    # --- Active-worktree variant: GitHub item with an active worktree renders
    # a disabled "Dispatched" button, not the hx-post one. ----
    monkeypatch.setattr(
        idea_routes,
        "detect_active_worktrees",
        lambda repos: {"ben-ralston/agent-hq#42"},
    )

    r2 = client.get("/inbox/recent")
    assert r2.status_code == 200
    body2 = r2.text
    assert "Dispatched" in body2
    # The GitHub item (active worktree) renders a static disabled button — no
    # hx-post.  Only #43 still has the live dispatch button.
    assert body2.count('hx-post="/inbox/dispatch"') == 1


def test_inbox_recent_closed_issue_is_not_dispatchable(tmp_path, monkeypatch, authed_idea_client):
    """A CLOSED/merged idea renders muted with a 'Closed' chip and NO dispatch
    button or reroute select — completed work isn't re-dispatchable. #128"""
    from agent_hq.idea import handler as handler_module
    from agent_hq.idea.handler import RecentIdea
    from agent_hq.web.routes import idea as idea_routes

    client, app = authed_idea_client
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())

    fake_recent = [
        RecentIdea(
            issue_url="https://github.com/ben-ralston/agent-hq/issues/41",
            issue_number=41,
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            title="Already merged idea",
            needs_triage=False,
            created_at="2026-06-04T00:00:00Z",
            destination="github",
            item_key="ben-ralston/agent-hq#41",
            state="CLOSED",
        ),
    ]
    monkeypatch.setattr(
        handler_module.IdeaHandler, "list_recent", lambda self, **kwargs: fake_recent
    )

    body = client.get("/inbox/recent").text
    assert "closed-chip" in body  # the "Closed" chip rendered
    assert "recent-item--closed" in body  # muted row
    assert 'hx-post="/inbox/dispatch"' not in body  # not dispatchable
    assert "reroute-select" not in body  # no reroute either


# --- GET /inbox/recent?limit=N (incremental loading, #128 follow-up) ---------


def _recent_ideas(n: int) -> list:
    from agent_hq.idea.handler import RecentIdea

    return [
        RecentIdea(
            issue_url=f"https://github.com/ben-ralston/agent-hq/issues/{i}",
            issue_number=i,
            repo="ben-ralston/agent-hq",
            project="Agent HQ",
            title=f"Idea number {i}",
            needs_triage=False,
            created_at=f"2026-06-04T{i:02d}:00:00Z",
            destination="github",
            item_key=f"ben-ralston/agent-hq#{i}",
            state="OPEN",
        )
        for i in range(1, n + 1)
    ]


def test_inbox_recent_truncates_to_limit_and_offers_show_more(tmp_path, monkeypatch):
    """With more items available than `limit`, the partial shows exactly
    `limit` rows plus a Show-more affordance. The route over-fetches by one
    to detect 'more exist'."""
    app, tm, handler = _build_app(tmp_path)
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    handler.list_recent.return_value = _recent_ideas(11)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    body = client.get("/inbox/recent").text

    handler.list_recent.assert_called_once_with(limit=11)
    assert "Idea number 10" in body
    assert "Idea number 11" not in body  # the +1 sentinel is not rendered
    assert "show-more" in body


def test_inbox_recent_no_show_more_when_list_exhausted(tmp_path, monkeypatch):
    app, tm, handler = _build_app(tmp_path)
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    handler.list_recent.return_value = _recent_ideas(3)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    body = client.get("/inbox/recent").text
    assert "Idea number 3" in body
    assert "show-more" not in body


def test_inbox_recent_respects_limit_param(tmp_path, monkeypatch):
    app, tm, handler = _build_app(tmp_path)
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    handler.list_recent.return_value = _recent_ideas(20)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    body = client.get("/inbox/recent?limit=20").text
    handler.list_recent.assert_called_once_with(limit=21)
    assert "Idea number 20" in body
    assert "show-more" not in body  # exactly 20 available → no more


def test_inbox_recent_clamps_limit_param(tmp_path, monkeypatch):
    """Hostile/garbage limits are clamped instead of 500ing or DoSing gh."""
    app, tm, handler = _build_app(tmp_path)
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    handler.list_recent.return_value = []
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    assert client.get("/inbox/recent?limit=0").status_code == 200
    assert handler.list_recent.call_args.kwargs["limit"] >= 2  # clamped up to >=1, +1
    handler.list_recent.reset_mock()
    assert client.get("/inbox/recent?limit=99999").status_code == 200
    assert handler.list_recent.call_args.kwargs["limit"] <= 201  # clamped to a sane cap


def test_inbox_recent_sent_rows_exempt_from_limit(tmp_path, monkeypatch):
    """Sent rows (#140) are guaranteed a slot: they don't count toward the
    page size, are never dropped as the over-fetch sentinel, and don't fake
    a has_more signal on their own."""
    from agent_hq.idea.handler import RecentIdea

    app, tm, handler = _build_app(tmp_path)
    monkeypatch.setattr(idea_routes, "detect_active_worktrees", lambda repos: set())
    sent = RecentIdea(
        issue_url="",
        issue_number=0,
        repo="",
        project=None,
        title="lost idea",
        needs_triage=False,
        created_at="2026-06-01T00:00:00+00:00",  # oldest row — last after sort
        destination="sent",
        sent_id="abc123",
        status="error",
        raw_text="lost idea",
        error="github: rate limited",
    )
    # 11 github rows (the over-fetch sentinel) + the sent row sorted last.
    handler.list_recent.return_value = _recent_ideas(11)[::-1] + [sent]
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    body = client.get("/inbox/recent").text
    assert "show-more" in body  # 11 non-sent rows → more exist
    assert "lost idea" in body  # the sent row survives, not sliced off
    assert ">Idea number 1</a>" not in body  # oldest github row dropped as the sentinel
    assert ">Idea number 2</a>" in body  # the other 10 github rows render

    # A sent row alone must not fake a has_more signal.
    handler.list_recent.return_value = _recent_ideas(3) + [sent]
    body = client.get("/inbox/recent").text
    assert "show-more" not in body


# --- sent-message rows + dismiss (#140) ---------------------------------------


def _sent_row(**overrides):
    from agent_hq.idea.handler import RecentIdea

    base = dict(
        issue_url="",
        issue_number=0,
        repo="",
        project=None,
        title="lost idea",
        needs_triage=False,
        created_at="2026-06-10T01:00:00+00:00",
        destination="sent",
        sent_id="abc123",
        status="error",
        raw_text="lost idea\nwith details",
        error="github: rate limited",
    )
    base.update(overrides)
    return RecentIdea(**base)


def test_get_inbox_recent_renders_failed_sent_row(tmp_path):
    """A failed send renders distinctly with its raw text recoverable and
    Restore/Dismiss controls — and never offers the GitHub Re-route select."""
    app, tm, handler = _build_app(tmp_path)
    handler.list_recent.return_value = [_sent_row()]
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    body = r.text
    assert "recent-item--failed" in body
    assert 'data-sent-id="abc123"' in body
    assert "data-raw-text" in body
    assert "github: rate limited" in body
    assert "Restore" in body
    assert "Dismiss" in body
    assert "Re-route" not in body


def test_get_inbox_recent_renders_interrupted_sent_row(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.list_recent.return_value = [_sent_row(status="interrupted", error="")]
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    assert "recent-item--interrupted" in r.text
    assert "interrupted" in r.text


def test_dismiss_sent_returns_ok(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.dismiss_sent.return_value = True
    raw = tm.token_file.read_text()
    client = TestClient(app)

    r = client.post(
        "/api/idea/sent/abc123/dismiss",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    handler.dismiss_sent.assert_called_once_with("abc123")


def test_dismiss_sent_unknown_id_returns_404(tmp_path):
    app, tm, handler = _build_app(tmp_path)
    handler.dismiss_sent.return_value = False
    raw = tm.token_file.read_text()
    client = TestClient(app)

    r = client.post(
        "/api/idea/sent/nope/dismiss",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 404


def test_dismiss_sent_requires_auth(tmp_path):
    app, _, _ = _build_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/idea/sent/abc123/dismiss")
    assert r.status_code == 401


def test_get_inbox_recent_renders_pending_sent_row(tmp_path):
    """A young in-flight send renders as a 'Sending…' row (no Restore/Dismiss,
    no Re-route) so it survives navigating away mid-classification."""
    app, tm, handler = _build_app(tmp_path)
    handler.list_recent.return_value = [_sent_row(status="pending", error="")]
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/inbox/recent")
    assert r.status_code == 200
    body = r.text
    assert "recent-item--sending" in body
    assert 'data-sent-id="abc123"' in body
    assert "Sending…" in body
    assert "Restore" not in body
    assert "Re-route" not in body
