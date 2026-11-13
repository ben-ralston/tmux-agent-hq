"""#302: the autopilot flag is set from the detail sheet and the Triage card.

Client + issue helpers copied from ``tests/test_web_routes_issues.py``.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent_hq.config import Config, DispatchConfig, ProjectConfig, RepoConfig, TriageConfig
from agent_hq.hygiene_store import HygieneStore
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageStore
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


class _StubCache:
    """Stand-in for ``GitHubCache`` — the real cache is fine, but tests
    don't want to touch ~/.config/agent-hq/cache/github.json.

    SWR knobs (#226): ``fresh`` drives what ``get_issues_cached`` reports;
    ``cold`` simulates a never-fetched cache (``None`` from the cached
    read). A forced ``get_issues`` marks the stub fresh again unless
    ``stuck_stale`` — mirroring the real cache, whose timestamps only move
    on a successful fetch.
    """

    def __init__(
        self,
        issues: list[Issue],
        *,
        fresh: bool = True,
        cold: bool = False,
        stuck_stale: bool = False,
        stale_projects: set[int] | None = None,
        closed_cold: bool = False,
    ):
        self._issues = issues
        self._fresh = fresh
        self._cold = cold
        self._stuck_stale = stuck_stale
        # Per-project staleness (overrides ``fresh`` for listed numbers) —
        # lets tests assert the background refresh targets only stale ones.
        self._stale_projects = stale_projects if stale_projects is not None else set()
        self._closed_cold = closed_cold
        # #254: what ``fetch_status()`` reports. Default healthy; tests set
        # a failed FetchStatus to exercise the stale banner.
        self.status = None
        self.get_issues_calls: list[bool] = []  # force flags, in call order
        self.forced_projects: list[int] = []
        self.fetched_projects: list[int] = []  # regardless of force
        self.get_closed_keys_calls: list[bool] = []

    def get_issues(self, project_number: int, owner: str, force: bool = False) -> list[Issue]:
        self.get_issues_calls.append(force)
        self.fetched_projects.append(project_number)
        if force:
            self.forced_projects.append(project_number)
        # Any SUCCESSFUL fetch freshens, forced or not — the background
        # sweep stopped forcing (#255 review), and keying freshness on
        # ``force`` would leave the stub permanently stale.
        if not self._stuck_stale:
            self._fresh = True
            self._cold = False
            self._stale_projects.discard(project_number)
        return list(self._issues)

    def get_issues_cached(self, project_number: int, owner: str):
        if self._cold:
            return None, False
        if project_number in self._stale_projects:
            return list(self._issues), False
        return list(self._issues), self._fresh

    def get_closed_keys(self, repos, force: bool = False) -> set[str]:
        self.__dict__.setdefault("closed_forces", []).append(force)
        self.get_closed_keys_calls.append(force)
        self._closed_cold = False
        return set(getattr(self, "_closed_keys", set()))

    def get_closed_keys_cached(self, repos):
        if self._closed_cold:
            return set(), False
        return set(getattr(self, "_closed_keys", set())), self._fresh and not self._cold

    def has_closed_entries(self, repos) -> bool:
        return not self._closed_cold

    def fetch_status(self):
        from agent_hq.github import FetchStatus

        return self.status if self.status is not None else FetchStatus()

    # Per-key failure verdict for the strict pre-Execute refetch (round 4).
    failed: set[str] = set()

    def failed_keys(self) -> set[str]:
        return set(self.failed)

    def mark_closed(self, repo: str, number: int) -> None:
        import asyncio

        self.__dict__.setdefault("_marked_closed", []).append((repo, number))
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        self.__dict__.setdefault("_marked_on_loop", []).append(on_loop)


def _mk_issue(
    number: int,
    *,
    repo: str = "ben-ralston/agent-hq",
    title: str = "",
    body: str = "",
    priority: Priority = Priority.NEED,
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


def _mk_config(
    repos: dict[str, RepoConfig] | None = None,
    triage: TriageConfig | None = None,
) -> Config:
    return Config(
        repos=repos or {"ben-ralston/agent-hq": RepoConfig("ben-ralston/agent-hq", Path("/tmp/x"))},
        projects=[ProjectConfig(name="Agent HQ", number=1, owner="ben-ralston")],
        worktree_root=Path("/tmp/wt"),
        dispatch=DispatchConfig(),
        triage=triage,
    )


def _make_authed_client(issues, tmp_path, monkeypatch):
    """Build an authed TestClient backed by ``issues`` — for tests that need
    custom issue data (e.g. timestamps) the shared fixture doesn't provide."""
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
        hygiene_store=HygieneStore(tmp_path / "triage-data"),
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, app, tm


@pytest.fixture
def client_and_store(tmp_path, monkeypatch):
    client, app, _tm = _make_authed_client(
        [_mk_issue(67, title="dispatch view")], tmp_path, monkeypatch
    )
    return client, app.state.triage_store, "ben-ralston/agent-hq#67"


def test_sheet_toggle_sets_autopilot(client_and_store):
    client, store, key = client_and_store
    r = client.post(
        f"/issues/issue/{quote(key, safe='')}/triage", data={"dim": "autopilot", "value": "true"}
    )
    assert r.status_code == 200
    assert store.get(key).autopilot is True
    assert "Autopilot" in r.text


def test_sheet_rejects_bad_autopilot_value(client_and_store):
    client, store, key = client_and_store
    r = client.post(
        f"/issues/issue/{quote(key, safe='')}/triage", data={"dim": "autopilot", "value": "maybe"}
    )
    assert r.status_code == 400
    assert store.get(key) is None or store.get(key).autopilot is None


def test_triage_card_adjust_sets_autopilot(client_and_store):
    client, store, key = client_and_store
    r = client.post(
        f"/issues/triage/{quote(key, safe='')}/adjust?mode=triage",
        data={"dim": "autopilot", "value": "true"},
    )
    assert r.status_code == 200
    assert store.get(key).autopilot is True


# ---- issue card layout (#302 follow-up: Option A) ----


def _card(html: str, number: int) -> str:
    """The rendered issue_card article for ``#number``."""
    start = html.index(f'data-issue-key="ben-ralston/agent-hq#{number}"')
    return html[start : html.index("</article>", start)]


@pytest.mark.parametrize("mode", ["queue", "browse"])
def test_flagged_card_shows_autopilot_chip(tmp_path, monkeypatch, mode):
    client, app, _ = _make_authed_client([_mk_issue(1), _mk_issue(2)], tmp_path, monkeypatch)
    app.state.triage_store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#1", autopilot=True), actor="human"
    )
    html = client.get(f"/issues/container?mode={mode}").text
    assert "issue-ap" in _card(html, 1)
    assert "issue-ap" not in _card(html, 2)


def test_in_progress_status_is_abbreviated(tmp_path, monkeypatch):
    client, _app, _ = _make_authed_client(
        [_mk_issue(1, status="In Progress"), _mk_issue(2, status="Todo")], tmp_path, monkeypatch
    )
    html = client.get("/issues/container?mode=browse").text
    assert ">In Prog<" in _card(html, 1)
    assert "In Progress" not in _card(html, 1)
    assert ">Todo<" in _card(html, 2)


def test_card_rows_are_fixed_slots(tmp_path, monkeypatch):
    """Identity line, title, footer — every card, so cards line up."""
    client, app, _ = _make_authed_client([_mk_issue(1), _mk_issue(2)], tmp_path, monkeypatch)
    app.state.triage_store.upsert(
        TriageRecord(issue_key="ben-ralston/agent-hq#1", autopilot=True), actor="human"
    )
    html = client.get("/issues/container?mode=queue").text
    card = _card(html, 1)
    assert card.index("issue-line") < card.index("issue-title") < card.index("issue-foot")
    # A card with nothing for the footer renders no empty footer row.
    assert "issue-foot" not in _card(html, 2)


def test_ai_proposed_tag_sits_rightmost_after_sentinel(tmp_path, monkeypatch):
    client, app, _ = _make_authed_client([_mk_issue(1, labels=["sentinel"])], tmp_path, monkeypatch)
    app.state.triage_store.upsert(
        TriageRecord(
            issue_key="ben-ralston/agent-hq#1",
            value=3,
            effort=Effort.S,
            agent_fitness=AgentFitness.SOLO,
            updated_at="t",
        ),
        actor="ai",
    )
    card = _card(client.get("/issues/container?mode=queue").text, 1)
    tags = card[card.index("issue-tags") :]
    assert tags.index("sentinel-badge") < tags.index("proposed-badge")
