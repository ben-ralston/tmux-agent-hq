"""Web-route unified-logger events (#231): GitHub writes and idea
classification failures.

Each test drives an existing route/function down its failure branch and
asserts on the (level, message, json_extra) triple passed to
``eventlog.emit`` — reusing the fixture patterns from
``test_web_routes_issues.py`` and ``test_web_routes_idea.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hq.config import (
    Config,
    DispatchConfig,
    IdeaInboxConfig,
    ProjectConfig,
    RepoConfig,
)
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import TriageStore
from agent_hq.web.app import create_app
from agent_hq.web.auth import (
    COOKIE_NAME,
    TokenManager,
    derive_cookie_value,
    install_auth_middleware,
)


def _quote(key: str) -> str:
    import urllib.parse

    return urllib.parse.quote(key, safe="")


def _mk_issue(
    number: int,
    *,
    repo: str = "ben-ralston/agent-hq",
    title: str = "",
    priority: Priority = Priority.NEED,
    status: str | None = "Todo",
) -> Issue:
    return Issue(
        number=number,
        title=title or f"issue {number}",
        repo=repo,
        status=status,
        priority=priority,
        body="",
    )


class _StubCache:
    """Minimal GitHubCache stand-in — just enough for the dispatch write
    routes under test. Mirrors ``test_web_routes_issues._StubCache``."""

    def __init__(self, issues):
        self._issues = issues

    def get_issues(self, project_number, owner, force=False):
        return list(self._issues)

    def get_issues_cached(self, project_number, owner):
        return list(self._issues), True

    def get_closed_keys(self, repos, force=False):
        return set()

    def get_closed_keys_cached(self, repos):
        return set(), True

    def has_closed_entries(self, repos):
        return True

    def mark_closed(self, repo, number):
        self.__dict__.setdefault("_marked_closed", []).append((repo, number))


def _mk_config():
    return Config(
        repos={"ben-ralston/agent-hq": RepoConfig("ben-ralston/agent-hq", Path("/tmp/x"))},
        projects=[ProjectConfig(name="Agent HQ", number=1, owner="ben-ralston")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
    )


def _dispatch_client(tmp_path, monkeypatch, issues=None):
    issues = issues if issues is not None else [_mk_issue(67, priority=Priority.NEED)]
    cache = _StubCache(issues)
    from agent_hq.web.routes import issues as issues_routes

    monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(
        token_file=tm.token_file,
        config=_mk_config(),
        github_cache=cache,
        triage_store=TriageStore(tmp_path / "triage-data"),
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, app


# --- Case 1: GitHub write failures (status / close-not-planned) -------------


def test_set_status_failure_emits_warn(tmp_path, monkeypatch):
    client, app = _dispatch_client(tmp_path, monkeypatch)
    app.state.github_cache.set_status = lambda issue, owner, label: False
    key = "ben-ralston/agent-hq#67"

    with patch("agent_hq.web.routes.issues.eventlog.emit") as emit:
        response = client.post(
            f"/issues/issue/{_quote(key)}/status", data={"value": "in-progress"}
        )

    assert response.status_code == 200
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "github write failed")
    assert extra["op"] == "set_status"
    assert extra["issue"] == key


def test_close_not_planned_failure_emits_warn(tmp_path, monkeypatch):
    client, _app = _dispatch_client(tmp_path, monkeypatch)
    from agent_hq.web.routes import issues as issues_routes

    monkeypatch.setattr(issues_routes, "close_issue_not_planned", lambda repo, number: False)
    key = "ben-ralston/agent-hq#67"

    with patch("agent_hq.web.routes.issues.eventlog.emit") as emit:
        response = client.post(f"/issues/issue/{_quote(key)}/close-not-planned")

    assert response.status_code == 200
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "github write failed")
    assert extra["op"] == "close_not_planned"
    assert extra["issue"] == key


# --- Case 2: idea classification failures -----------------------------------


def _build_idea_app(tmp_path, *, handler=None):
    from agent_hq.web.routes import idea as idea_routes

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = FastAPI()
    install_auth_middleware(app, tm)
    handler = handler or MagicMock()
    config = Config(
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
    idea_routes.register(app, handler=handler, config=config)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, handler


def test_idea_classification_failed_auth_emits_warn(tmp_path):
    from agent_hq.idea.classifier import ClassifierAuthError

    client, handler = _build_idea_app(tmp_path)
    handler.handle.side_effect = ClassifierAuthError("MY_KEY missing")

    with patch("agent_hq.web.routes.idea.eventlog.emit") as emit:
        response = client.post("/api/idea", json={"text": "x"})

    assert response.status_code == 503
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "idea classification failed")
    assert extra["kind"] == "auth"


def test_idea_classification_failed_classifier_emits_warn(tmp_path):
    from agent_hq.idea.classifier import ClassifierError

    client, handler = _build_idea_app(tmp_path)
    handler.handle.side_effect = ClassifierError("upstream 500")

    with patch("agent_hq.web.routes.idea.eventlog.emit") as emit:
        response = client.post("/api/idea", json={"text": "x"})

    assert response.status_code == 502
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "idea classification failed")
    assert extra["kind"] == "classifier"


def test_idea_classification_failed_github_emits_warn(tmp_path):
    from agent_hq.github import GHError

    client, handler = _build_idea_app(tmp_path)
    handler.handle.side_effect = GHError("gh issue create failed: rate limited")

    with patch("agent_hq.web.routes.idea.eventlog.emit") as emit:
        response = client.post("/api/idea", json={"text": "x"})

    assert response.status_code == 502
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "idea classification failed")
    assert extra["kind"] == "github"


def test_idea_user_input_errors_do_not_emit(tmp_path):
    """EmptyTextError / NoCandidatesError / IdeaError are user-input
    problems, not infrastructure failures — they must not emit."""
    from agent_hq.idea.handler import EmptyTextError, IdeaError, NoCandidatesError

    for exc in (EmptyTextError("empty"), NoCandidatesError("no project"), IdeaError("bad")):
        client, handler = _build_idea_app(tmp_path)
        handler.handle.side_effect = exc

        with patch("agent_hq.web.routes.idea.eventlog.emit") as emit:
            client.post("/api/idea", json={"text": "x"})

        emit.assert_not_called()
