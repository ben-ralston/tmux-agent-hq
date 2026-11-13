"""The GitHub headroom row in the settings menu (#255)."""

import pytest
from fastapi.testclient import TestClient

from agent_hq import gh_budget
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _budget(remaining):
    return gh_budget.RateBudget(
        graphql_remaining=remaining,
        graphql_limit=5000,
        graphql_reset=1787624635.0,
        core_remaining=4999,
        core_limit=5000,
        core_reset=None,
        exhausted_reset=None,
    )


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    """A logged-in client whose budget snapshot is whatever the test says."""

    def _make(snapshot):
        # The render path deliberately uses the non-blocking accessor
        # (#255 review) — patch that, not the probing form.
        monkeypatch.setattr(gh_budget, "snapshot_cached", lambda: snapshot)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=None, github_cache=None)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        return client

    return _make


def _body(client):
    response = client.get("/agents", follow_redirects=True)
    assert response.status_code == 200
    return response.text


def test_headroom_is_rendered(client_factory):
    body = _body(client_factory(_budget(4150)))
    assert "GitHub API" in body
    assert "4150" in body
    assert "5000" in body


def test_headroom_row_is_hidden_when_unknown(client_factory):
    """An absent number beats a wrong one — rate_limit lies under exhaustion."""
    body = _body(client_factory(None))
    assert "settings-menu__budget" not in body


def test_low_headroom_is_flagged(client_factory):
    body = _body(client_factory(_budget(120)))
    assert "settings-menu__budget--low" in body


def test_healthy_headroom_is_not_flagged(client_factory):
    body = _body(client_factory(_budget(4150)))
    assert "settings-menu__budget--low" not in body
    assert "settings-menu__budget" in body


def test_exhausted_budget_renders_zero_not_a_blank(client_factory):
    body = _body(client_factory(_budget(0)))
    assert "settings-menu__budget--low" in body
    assert "0/5000" in body.replace("\n", "").replace(" ", "")


class TestProbeNeverBlocksTheEventLoop:
    """#255 review: the ⚙ row must not run subprocesses during render.

    ``settings_menu.html`` calls ``app.state.gh_budget_snapshot()`` inside
    Jinja rendering, and the pages that include it are ``async def`` routes
    rendering ``TemplateResponse`` on the event loop. Wiring the blocking
    ``snapshot`` there meant that on every memo expiry (once a minute) one
    render ran ``_SAMPLES`` sequential ``gh api rate_limit`` subprocesses —
    up to ~50 s — stalling every other request and all background async work.
    The plan called for a threadpool-guarded accessor; this pins it.
    """

    def test_render_never_calls_the_blocking_snapshot(self, tmp_path, monkeypatch):
        def _boom():  # pragma: no cover - only runs if the wiring regresses
            raise AssertionError("blocking snapshot() called during template render")

        monkeypatch.setattr(gh_budget, "snapshot", _boom)
        monkeypatch.setattr(gh_budget, "snapshot_cached", lambda: _budget(4150))

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=None, github_cache=None)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        response = client.get("/agents", follow_redirects=True)
        assert response.status_code == 200
        assert "4150" in response.text


class TestFloorComesFromConfig:
    """The ⚙ row must colour on the SAME floor the gate enforces (#255 review).

    `create_app` threaded `config.github.graphql_floor` into `GitHubCache`
    but pinned `app.state.gh_budget_floor` to the module default, so with a
    configured floor of 2000 the row stayed calm down to 500 while background
    refreshes had already stood down — the indicator disagreeing with the
    behaviour it exists to explain.
    """

    def _app(self, tmp_path, monkeypatch, floor, remaining):
        from pathlib import Path

        from agent_hq.config import Config, GithubConfig

        monkeypatch.setattr(gh_budget, "snapshot_cached", lambda: _budget(remaining))
        cfg = Config(
            repos={},
            projects=[],
            worktree_root=Path(tmp_path / "wt"),
            github=GithubConfig(graphql_floor=floor),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=cfg, github_cache=None)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        return client.get("/agents", follow_redirects=True).text

    def test_configured_floor_flags_a_low_reading(self, tmp_path, monkeypatch):
        body = self._app(tmp_path, monkeypatch, floor=2000, remaining=1500)
        assert "settings-menu__budget--low" in body

    def test_above_the_configured_floor_is_not_flagged(self, tmp_path, monkeypatch):
        body = self._app(tmp_path, monkeypatch, floor=2000, remaining=2500)
        assert "settings-menu__budget--low" not in body
