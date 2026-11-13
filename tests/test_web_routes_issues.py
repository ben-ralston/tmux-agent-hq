"""Tests for the web Dispatch view (#67 + #40 + #44).

The dispatch route mirrors the TUI's ``DispatchScreen``: chip filters
for repo / priority / status, a free-text search input, an issue card
list with multi-select checkboxes, and a Group/Dispatch action bar.

Tests use a stub ``GitHubCache`` rather than the real disk-backed cache
so we don't shell out to ``gh``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent_hq.capabilities import CLAUDE_ONLY, LauncherCapabilities
from agent_hq.config import (
    Config,
    DispatchConfig,
    HarnessDefaults,
    ProjectConfig,
    RepoConfig,
    TriageConfig,
)
from agent_hq.hygiene_store import Finding, FindingStatus, FindingType, HygieneStore
from agent_hq.models import Issue, Priority
from agent_hq.triage_store import AgentFitness, Effort, TriageRecord, TriageState, TriageStore
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value
from agent_hq.web.state import COOKIE_NAME as STATE_COOKIE
from agent_hq.web.state import DispatchState, write_dispatch_state


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


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    """Authenticated client with a stub GitHubCache and active-worktrees
    detector that returns nothing by default. Tests that need a non-empty
    set patch ``detect_active_worktrees`` directly."""
    issues = [
        _mk_issue(67, title="dispatch view", priority=Priority.NEED),
        _mk_issue(40, title="filter bar", priority=Priority.WANT),
        _mk_issue(
            44,
            title="search",
            body="case-insensitive substring",
            priority=Priority.IDEA,
        ),
    ]
    cache = _StubCache(issues)

    # Patch detect_active_worktrees at the route's import path so it
    # doesn't shell out to git.
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


def _set_state(client: TestClient, app, tm: TokenManager, state: DispatchState) -> None:
    """Helper: set the ``dispatch_state`` cookie on the test client by
    writing to a synthetic Response and copying the encoded value over.

    ``path="/"`` is critical — the route writes the cookie with
    ``Path=/`` so any pre-set cookie without that path is treated as a
    distinct cookie by httpx and the test ends up with a CookieConflict
    after the first request.
    """
    from fastapi import Response

    response = Response()
    write_dispatch_state(response, state, tm)
    raw = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    client.cookies.set(STATE_COOKIE, raw, path="/")


class TestSentinelStyling:
    """#235: sentinel-labeled issues render as machine-filed; "untriaged"
    means no confirmed triage record yet — confirming dimensions IS the
    triage (#247 moved this off the GH priority field)."""

    def _client(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(211, title="sentinel finding", labels=["sentinel"], priority=Priority.NO_PRI),
            _mk_issue(212, title="normal work"),
            _mk_issue(213, title="triaged finding", labels=["sentinel"], priority=Priority.NEED),
        ]
        return _make_authed_client(issues, tmp_path, monkeypatch)

    def test_sentinel_cards_have_class_and_badge(self, tmp_path, monkeypatch):
        client, app, _ = self._client(tmp_path, monkeypatch)
        _triage(app, 213)  # confirming dimensions IS the triage (#247)
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        # Both sentinel cards are marked — the normal card carries neither
        # the modifier class nor the badge.
        assert r.text.count("issue-card--sentinel") == 2
        assert r.text.count('class="sentinel-badge"') == 2
        # Only the priority-less one still reads untriaged.
        assert r.text.count("sentinel · untriaged") == 1

    def test_sentinel_count_line_counts_untriaged_only(self, tmp_path, monkeypatch):
        client, app, _ = self._client(tmp_path, monkeypatch)
        _triage(app, 213)
        r = client.get("/issues/container?mode=browse")
        assert "1 machine-filed (sentinel) · untriaged" in r.text

    def test_no_count_line_when_all_sentinels_triaged(self, tmp_path, monkeypatch):
        client, app, _ = _make_authed_client(
            [_mk_issue(213, title="triaged", labels=["sentinel"], priority=Priority.NEED)],
            tmp_path,
            monkeypatch,
        )
        _triage(app, 213)
        r = client.get("/issues/container?mode=browse")
        assert "machine-filed (sentinel)" not in r.text
        assert "issue-card--sentinel" in r.text  # still visibly machine-filed

    def test_no_count_line_without_sentinel_issues(self, tmp_path, monkeypatch):
        client, _, _ = _make_authed_client(
            [_mk_issue(212, title="normal work")], tmp_path, monkeypatch
        )
        r = client.get("/issues/container?mode=browse")
        assert "machine-filed (sentinel)" not in r.text
        assert "issue-card--sentinel" not in r.text

    def test_sentinel_badge_in_issue_detail(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.get("/issues/issue/ben-ralston%2Fagent-hq%23211")
        assert r.status_code == 200
        assert "sentinel · untriaged" in r.text

    def test_triaged_sentinel_detail_drops_untriaged(self, tmp_path, monkeypatch):
        client, app, _ = self._client(tmp_path, monkeypatch)
        _triage(app, 213)
        r = client.get("/issues/issue/ben-ralston%2Fagent-hq%23213")
        assert r.status_code == 200
        assert 'class="sentinel-badge"' in r.text
        assert "sentinel · untriaged" not in r.text

    def test_no_badge_in_normal_issue_detail(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.get("/issues/issue/ben-ralston%2Fagent-hq%23212")
        assert r.status_code == 200
        assert "sentinel-badge" not in r.text


class TestSentinelFilter:
    """#235 follow-up: include/exclude machine-filed issues via a chip group."""

    def _client(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(211, title="sentinel finding", labels=["sentinel"]),
            _mk_issue(212, title="normal work"),
        ]
        return _make_authed_client(issues, tmp_path, monkeypatch)

    def test_sentinel_chips_render_when_mixed(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.get("/issues/container?mode=browse")
        assert "⚑ Sentinel" in r.text
        assert "Manual" in r.text
        assert "type=sentinel" in r.text

    def test_no_sentinel_chips_without_sentinel_issues(self, tmp_path, monkeypatch):
        client, _, _ = _make_authed_client(
            [_mk_issue(212, title="normal work")], tmp_path, monkeypatch
        )
        r = client.get("/issues/container?mode=browse")
        assert "⚑ Sentinel" not in r.text

    def test_toggle_excludes_sentinel_issues(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.post("/issues/filters/toggle?mode=browse&type=sentinel&value=other")
        assert r.status_code == 200
        assert "normal work" in r.text
        assert "sentinel finding" not in r.text

    def test_toggle_only_sentinel_issues(self, tmp_path, monkeypatch):
        client, _, _ = self._client(tmp_path, monkeypatch)
        r = client.post("/issues/filters/toggle?mode=browse&type=sentinel&value=sentinel")
        assert r.status_code == 200
        assert "sentinel finding" in r.text
        assert "normal work" not in r.text


class TestTriageStoreInjection:
    """create_app(triage_store=…) seam (#247), mirroring github_cache."""

    def _app(self, tmp_path, **kw):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        return create_app(token_file=tm.token_file, github_cache=_StubCache([]), **kw)

    def test_injected_store_lands_on_app_state(self, tmp_path):
        store = TriageStore(tmp_path / "data")
        app = self._app(tmp_path, config=_mk_config(), triage_store=store)
        assert app.state.triage_store is store

    def test_none_when_config_has_no_triage_section(self, tmp_path):
        app = self._app(tmp_path, config=_mk_config())
        assert app.state.triage_store is None
        app = self._app(tmp_path, config=None)
        assert app.state.triage_store is None

    def test_constructed_from_config_data_dir(self, tmp_path):
        cfg = _mk_config()
        cfg.triage = TriageConfig(data_dir=tmp_path / "data-repo")
        app = self._app(tmp_path, config=cfg)
        assert app.state.triage_store is not None
        app.state.triage_store.seed_missing("o/r", [(1, None)], now="t0")
        assert (tmp_path / "data-repo" / "triage" / "o__r.json").exists()


class TestDispatchRedirects:
    """/dispatch → /issues (#247). GET-only: old markup's POSTs 404 after
    a deploy, self-healing on reload; the SSE-triggered container GET from
    a stale open tab follows the 307 transparently."""

    def test_dispatch_page_redirects_permanently(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/dispatch", follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == "/issues"

    def test_partial_gets_redirect(self, authed_client):
        client, _, _ = authed_client
        for old, new in (
            ("/dispatch/container", "/issues/container"),
            ("/dispatch/list", "/issues/list"),
        ):
            r = client.get(old, follow_redirects=False)
            assert r.status_code == 307
            assert r.headers["location"] == new

    def test_nav_tab_is_issues(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues")
        assert ">Issues</a>" in r.text
        assert ">Dispatch</a>" not in r.text


async def _noop_coro() -> None:
    """Factory-compatible no-op for BackgroundRefresher.kick assertions."""


def _reset_state(client: TestClient, app, tm: TokenManager, state: DispatchState) -> None:
    """``_set_state`` for a client that has already made a request: the
    route re-issues the state cookie with a domain, and a second
    ``cookies.set`` without one would sit beside it (older wins), so clear
    the jar and re-auth first."""
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    _set_state(client, app, tm, state)


def _label_tag_order(html: str) -> list[str]:
    """The chip-group label tags of the FIRST filter bar in ``html``, in
    document order — pins the shared Repo → Src → Status → Eff → Fit order."""
    bar = html.split('class="filter-bar__chips"', 1)[-1].split("</div>", 1)[0]
    return re.findall(r'class="filter-chip__label-tag">([^<]+)</span>', bar)


def _triage(app, number, *, effort="S", fitness="solo", value=4, repo="ben-ralston/agent-hq"):
    """Confirm a full dimension set for one issue via the app's store.

    Human writes never flip state on their own (Confirm is explicit), so
    the helper writes the three dims and then confirms."""
    key = f"{repo}#{number}"
    app.state.triage_store.upsert(
        TriageRecord(
            issue_key=key,
            value=value,
            effort=Effort(effort),
            agent_fitness=AgentFitness(fitness),
            updated_at="t",
        ),
        actor="human",
    )
    app.state.triage_store.confirm(key, now="t")


def _propose(
    app,
    number,
    *,
    repo="ben-ralston/agent-hq",
    value=3,
    effort="M",
    fitness="pair",
    proposed_at="2026-09-03T00:00:00+00:00",
):
    """Write one AI-proposed (PROPOSED) record via the app's store (#248)."""
    app.state.triage_store.upsert(
        TriageRecord(
            issue_key=f"{repo}#{number}",
            value=value,
            effort=Effort(effort),
            agent_fitness=AgentFitness(fitness),
            reasoning={
                "value": f"why-v-{number}",
                "effort": f"why-e-{number}",
                "agent_fitness": f"why-f-{number}",
            },
            proposed_at=proposed_at,
            model="sonnet",
            body_hash="h",
            updated_at="t",
        ),
        actor="ai",
    )


def _confirm_all_vals(html: str) -> dict:
    """The parsed ``hx-vals`` JSON of the Confirm-all button (#248)."""
    m = re.search(r'hx-post="/issues/triage/confirm-all[^"]*"\s+hx-vals=\'([^\']*)\'', html)
    assert m, "Confirm-all button not rendered"
    return json.loads(m.group(1))


class TestBrowseDimensionSwap:
    """#247 full swap: priority chips/pill out, effort+fitness/rank in."""

    def test_filter_bar_swaps_priority_for_effort_fitness(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67)
        r = client.get("/issues/container?mode=browse")
        assert "type=effort" in r.text
        assert "type=fitness" in r.text
        assert "type=priority" not in r.text
        assert ">Eff</span>" in r.text
        assert ">Fit</span>" in r.text
        assert ">Pri</span>" not in r.text

    def test_effort_chip_filters_list(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67, effort="S")
        r = client.post("/issues/filters/toggle?mode=browse&type=effort&value=s&mode=browse")
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#40"' not in r.text

    def test_none_chip_matches_untriaged_dimensions(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67)
        r = client.post("/issues/filters/toggle?mode=browse&type=fitness&value=none&mode=browse")
        assert 'data-issue-key="ben-ralston/agent-hq#67"' not in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#40"' in r.text

    def test_card_shows_rank_chip_not_priority_pill(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67)  # 4*2 + S(2) + solo(2) = 12
        r = client.get("/issues/container?mode=browse")
        assert 'class="priority' not in r.text
        assert 'class="rank-chip"' in r.text
        assert ">12</span>" in r.text
        assert "V4 +8" in r.text  # breakdown tooltip stays legible

    def test_unranked_card_has_no_rank_chip(self, authed_client):
        client, app, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        assert "rank-chip" not in r.text


class TestQueueMode:
    """Queue rendering (#247): pinned strip → ranked rows → untriaged."""

    def test_sections_and_ordering(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 40, value=5, effort="S", fitness="solo")  # 14
        _triage(app, 67, value=2, effort="L", fitness="needs_you")  # 4
        app.state.triage_store.upsert(
            TriageRecord(issue_key="ben-ralston/agent-hq#44", pin=True, updated_at="t"),
            actor="human",
        )
        r = client.get("/issues/container?mode=queue")
        assert "queue-pinned" in r.text
        # A "Ranked" divider separates the pinned strip from the ranked
        # rows so they don't read as one section (390px visual pass).
        assert ">Ranked</p>" in r.text
        i44 = r.text.find('data-issue-key="ben-ralston/agent-hq#44"')
        i40 = r.text.find('data-issue-key="ben-ralston/agent-hq#40"')
        i67 = r.text.find('data-issue-key="ben-ralston/agent-hq#67"')
        assert -1 < i44 < i40 < i67  # pinned, then 14 before 4

    def test_breakdown_chips_render_in_queue_rows(self, authed_client):
        client, app, _ = authed_client
        app.state.triage_store.upsert(
            TriageRecord(
                issue_key="ben-ralston/agent-hq#67",
                value=3,
                effort=Effort.S,
                agent_fitness=AgentFitness.SOLO,
                interest=True,
                updated_at="t",
            ),
            actor="human",
        )
        r = client.get("/issues/container?mode=queue")
        assert 'class="rank-chip"' in r.text
        assert ">V3</span>" in r.text
        assert ">solo</span>" in r.text
        # The interest star gets its own class so it can be styled apart
        # from the muted mono breakdown (hand-test: it was too small to see).
        assert 'class="rank-breakdown__part rank-breakdown__part--interest">★</span>' in r.text
        assert 'class="rank-breakdown__part rank-breakdown__part--interest">V3' not in r.text

    def test_untriaged_section_lists_scoreless_rows(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67)
        r = client.get("/issues/container?mode=queue")
        assert "Untriaged" in r.text
        # 40 + 44 are scoreless and render after the ranked row
        i67 = r.text.find('data-issue-key="ben-ralston/agent-hq#67"')
        i_head = r.text.find("Untriaged")
        i40 = r.text.find('data-issue-key="ben-ralston/agent-hq#40"')
        assert -1 < i67 < i_head < i40

    def test_queue_bar_shares_browse_chip_groups_in_order(self, tmp_path, monkeypatch):
        """Hand-test (#248): Queue carries the same chip groups as Browse, in
        the shared order Repo → Src → Status → Eff → Fit — minus search/sort."""
        issues = [_mk_issue(67, labels=["sentinel"]), _mk_issue(40)]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        _triage(app, 67)
        r = client.get("/issues/container?mode=queue")
        assert _label_tag_order(r.text) == ["Repo", "Src", "Status", "Eff", "Fit"]
        for kind in ("repo", "sentinel", "status", "effort", "fitness"):
            assert f"type={kind}&" in r.text and "mode=queue" in r.text
        assert 'id="dispatch-search"' not in r.text
        assert "/issues/sort" not in r.text

    def test_queue_ignores_query_but_applies_status_effort_sentinel(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(67, labels=["sentinel"]),
            _mk_issue(40, status="In Progress"),
            _mk_issue(44),
        ]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _triage(app, 67, effort="L")
        _triage(app, 40, effort="S")
        _triage(app, 44, effort="S")
        _set_state(client, app, tm, DispatchState(query="zzz-no-match"))
        r = client.get("/issues/container?mode=queue")
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text  # query ignored
        _reset_state(client, app, tm, DispatchState(statuses=["in progress"]))
        r = client.get("/issues/container?mode=queue")
        assert 'data-issue-key="ben-ralston/agent-hq#40"' in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#67"' not in r.text
        _reset_state(client, app, tm, DispatchState(efforts=["s"]))
        r = client.get("/issues/container?mode=queue")
        assert 'data-issue-key="ben-ralston/agent-hq#40"' in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#67"' not in r.text
        _reset_state(client, app, tm, DispatchState(sentinels=["sentinel"]))
        r = client.get("/issues/container?mode=queue")
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#44"' not in r.text

    def test_fitness_chip_filters_queue(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67, fitness="solo")
        _triage(app, 40, fitness="needs_you")
        _set_state(client, app, tm, DispatchState(fitnesses=["solo"]))
        r = client.get("/issues/container?mode=queue")
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#40"' not in r.text

    def test_selection_and_dispatch_work_in_queue(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67)
        r = client.post(
            "/issues/select?key=ben-ralston%2Fagent-hq%2367&mode=queue",
        )
        assert r.status_code == 200
        assert 'data-mode="queue"' in r.text
        assert _read_state(client, tm).selected == ["ben-ralston/agent-hq#67"]

    def test_triage_unconfigured_hint(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(),
            github_cache=_StubCache([_mk_issue(1)]),
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=queue")
        assert "Triage store not configured" in r.text
        # Browse still works without the store
        r = client.get("/issues/container?mode=browse")
        assert "Triage store not configured" not in r.text
        assert 'data-issue-key="ben-ralston/agent-hq#1"' in r.text


class TestDimensionEditor:
    """#247: the detail sheet's dimension editor replaces the GH priority
    picker; POST /issues/issue/{key}/triage is the single write route."""

    KEY = "ben-ralston%2Fagent-hq%2367"

    def test_detail_renders_editor_not_priority_picker(self, authed_client):
        client, _, _ = authed_client
        r = client.get(f"/issues/issue/{self.KEY}")
        assert r.status_code == 200
        for picker in ("value", "effort", "fitness", "interest", "pin"):
            assert f'data-picker="{picker}"' in r.text
        assert "/priority" not in r.text
        assert 'data-picker="status"' in r.text  # status picker stays
        assert "Won't Do" in r.text
        assert "→ Personal" not in r.text

    def test_set_value_writes_human_provenance_without_confirming(self, authed_client):
        """One chip is an edit, not an approval: the record keeps its state
        and the sheet offers Confirm only once all three dims are set."""
        client, app, _ = authed_client
        r = client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "4"})
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.value == 4
        assert rec.state is TriageState.UNTRIAGED
        assert rec.provenance["value"] == "human"
        assert "picker-option--active" in r.text
        assert 'data-action="confirm-triage"' in r.text
        assert re.search(r'data-action="confirm-triage"[^>]*\bdisabled\b', r.text)

    def test_detail_confirm_button_sits_below_the_flags(self, authed_client):
        """#267 follow-up: Confirm is the last thing to tap, so it renders
        after the Flags row (interest/pin), not between Fitness and Flags."""
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.get(f"/issues/issue/{self.KEY}")
        flags = r.text.find('data-picker="pin"')
        confirm = r.text.find('data-action="confirm-triage"')
        assert flags != -1 and confirm != -1
        assert flags < confirm

    def test_detail_confirm_button_flips_state_and_disappears(self, authed_client):
        """A complete, unconfirmed record renders an enabled Confirm that
        POSTs to the detail-scoped confirm route and re-renders the sheet."""
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.get(f"/issues/issue/{self.KEY}")
        assert 'class="rank-chip"' not in r.text  # no rank until confirmed
        assert ">proposed<" in r.text
        m = re.search(r'<button[^>]*data-action="confirm-triage"[^>]*>', r.text)
        assert m and "disabled" not in m.group(0)
        assert "/issues/issue/ben-ralston/agent-hq%2367/confirm" in m.group(0)
        assert "2026-09-03T00:00:00+00:00" in m.group(0)  # proposed_at revision rides along
        r = client.post(
            f"/issues/issue/{self.KEY}/confirm", data={"proposed_at": "2026-09-03T00:00:00+00:00"}
        )
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.CONFIRMED
        assert rec.provenance["value"] == "ai"
        assert 'data-action="confirm-triage"' not in r.text
        assert 'class="rank-chip"' in r.text  # confirmed + complete → scored

    def test_detail_confirm_after_editing_a_chip_keeps_the_edit(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67, value=3)
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "5"})
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.PROPOSED
        r = client.post(f"/issues/issue/{self.KEY}/confirm")
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.CONFIRMED and rec.value == 5
        assert rec.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}

    def test_detail_confirm_stale_revision_rerenders_without_writing(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.post(f"/issues/issue/{self.KEY}/confirm", data={"proposed_at": "older"})
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.PROPOSED
        assert 'data-action="confirm-triage"' in r.text

    def test_detail_confirm_incomplete_is_400(self, authed_client):
        client, app, _ = authed_client
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "4"})
        assert client.post(f"/issues/issue/{self.KEY}/confirm").status_code == 400

    def test_set_effort_and_fitness(self, authed_client):
        client, app, _ = authed_client
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "effort", "value": "M"})
        client.post(
            f"/issues/issue/{self.KEY}/triage", data={"dim": "fitness", "value": "needs_you"}
        )
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.effort is Effort.M
        assert rec.agent_fitness is AgentFitness.NEEDS_YOU

    def test_full_dimension_set_scores_only_after_confirm(self, authed_client):
        """Hand triage: three chips complete the record (Confirm becomes
        enabled) but the rank chip appears only once Confirm flips it —
        the Queue ranks confirmed records only, so the sheet must agree."""
        client, app, _ = authed_client
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "4"})
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "effort", "value": "S"})
        r = client.post(
            f"/issues/issue/{self.KEY}/triage", data={"dim": "fitness", "value": "solo"}
        )
        assert 'class="rank-chip"' not in r.text
        assert ">untriaged<" in r.text
        m = re.search(r'<button[^>]*data-action="confirm-triage"[^>]*>', r.text)
        assert m and "disabled" not in m.group(0)
        r = client.post(f"/issues/issue/{self.KEY}/confirm")
        assert r.status_code == 200
        assert 'class="rank-chip"' in r.text
        assert ">12</span>" in r.text
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.CONFIRMED

    def test_interest_and_pin_do_not_change_state(self, authed_client):
        client, app, _ = authed_client
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "interest", "value": "true"})
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "pin", "value": "true"})
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.interest is True
        assert rec.pin is True
        assert rec.state is TriageState.UNTRIAGED

    def test_triage_publishes_issue_invalidated(self, authed_client):
        client, app, _ = authed_client
        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "3"})
        new_events = broker.replay_since(0)[before:]
        assert any(
            e.topic == "issue.invalidated" and e.data.get("key") == "ben-ralston/agent-hq#67"
            for e in new_events
        )

    def test_bad_dim_and_value_400(self, authed_client):
        client, _, _ = authed_client
        assert (
            client.post(
                f"/issues/issue/{self.KEY}/triage", data={"dim": "urgency", "value": "1"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "9"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/issues/issue/{self.KEY}/triage", data={"dim": "effort", "value": "XL"}
            ).status_code
            == 400
        )

    def test_priority_route_is_gone(self, authed_client):
        client, _, _ = authed_client
        r = client.post(f"/issues/issue/{self.KEY}/priority", data={"value": "need"})
        assert r.status_code in (404, 405)

    def test_store_none_detail_degrades_and_triage_404(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(),
            github_cache=_StubCache([_mk_issue(67)]),
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get(f"/issues/issue/{self.KEY}")
        assert r.status_code == 200
        assert 'data-picker="value"' not in r.text  # editor hidden
        assert 'data-picker="status"' in r.text  # sheet still works
        r = client.post(f"/issues/issue/{self.KEY}/triage", data={"dim": "value", "value": "3"})
        assert r.status_code == 404


class TestReviewFixes:
    """#247 review round: mode threading, failed-write honesty, keep=1,
    old detail-URL shim, storeless sentinel fallback."""

    def test_mutating_posts_carry_mode_in_rendered_markup(self, authed_client):
        client, app, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        for frag in (
            "/issues/search?mode=browse",
            "/issues/sort?value=default&mode=browse",
            "type=repo&value=ben-ralston/agent-hq&mode=browse",
            "/issues/select?key=ben-ralston/agent-hq%2367&mode=browse",
            "/issues/group?mode=browse",
            "/issues/dispatch?mode=browse",
        ):
            assert frag in r.text, frag

    def test_stored_queue_pref_does_not_flip_a_browse_interaction(self, authed_client):
        client, app, _ = authed_client
        app.state.ui_prefs_store.set_web_issues_mode("queue")
        r = client.post("/issues/select?key=ben-ralston%2Fagent-hq%2367&mode=browse")
        assert 'data-mode="browse"' in r.text

    def test_failed_triage_write_warns_and_skips_publish(self, authed_client, tmp_path):
        client, app, _ = authed_client
        repo_file = tmp_path / "triage-data" / "triage" / "ben-ralston__agent-hq.json"
        repo_file.parent.mkdir(parents=True, exist_ok=True)
        repo_file.write_text("{corrupt")
        broker = app.state.broker
        before = len(broker.replay_since(0))
        from unittest.mock import patch as _patch

        with _patch("agent_hq.web.routes.issues.eventlog.emit") as emit:
            r = client.post(
                "/issues/issue/ben-ralston%2Fagent-hq%2367/triage",
                data={"dim": "value", "value": "4"},
            )
        assert r.status_code == 200
        emit.assert_called_once()
        level, message, extra = emit.call_args[0][:3]
        assert (level, message) == ("warn", "triage write failed")
        assert extra["op"] == "triage"
        new_events = broker.replay_since(0)[before:]
        assert not any(e.topic == "issue.invalidated" for e in new_events)

    def test_failed_pin_write_warns_and_skips_publish(self, authed_client, tmp_path):
        client, app, _ = authed_client
        repo_file = tmp_path / "triage-data" / "triage" / "ben-ralston__agent-hq.json"
        repo_file.parent.mkdir(parents=True, exist_ok=True)
        repo_file.write_text("{corrupt")
        broker = app.state.broker
        before = len(broker.replay_since(0))
        from unittest.mock import patch as _patch

        with _patch("agent_hq.web.routes.issues.eventlog.emit") as emit:
            r = client.post("/issues/issue/ben-ralston%2Fagent-hq%2367/pin?on=true")
        assert r.status_code == 200
        # The container re-render may also emit an unrelated capabilities
        # warn — filter for the triage-write one.
        pin_warns = [c for c in emit.call_args_list if c[0][:2] == ("warn", "triage write failed")]
        assert len(pin_warns) == 1
        assert pin_warns[0][0][2]["op"] == "pin"
        assert not any(e.topic == "issue.invalidated" for e in broker.replay_since(0)[before:])

    def test_mode_switch_with_keep_preserves_selection(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["ben-ralston/agent-hq#67"]),
        )
        r = client.get("/issues?mode=browse&keep=1")
        assert r.status_code == 200
        assert _read_state(client, tm).selected == ["ben-ralston/agent-hq#67"]
        assert 'href="/issues?mode=queue&keep=1"' in r.text
        # A hard reload (no keep) still clears — the pre-existing contract.
        client.get("/issues")
        assert _read_state(client, tm).selected == []

    def test_old_detail_url_redirects(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/dispatch/issue/ben-ralston%2Fagent-hq%2367", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/issues/issue/ben-ralston%2Fagent-hq%2367"
        r = client.get("/dispatch/issue/ben-ralston%2Fagent-hq%2367")
        assert r.status_code == 200
        assert 'data-picker="value"' in r.text

    def test_storeless_sentinel_falls_back_to_priority_rule(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        issues = [
            _mk_issue(211, title="raw finding", labels=["sentinel"], priority=Priority.NO_PRI),
            _mk_issue(213, title="triaged finding", labels=["sentinel"], priority=Priority.NEED),
        ]
        app = create_app(
            token_file=tm.token_file, config=_mk_config(), github_cache=_StubCache(issues)
        )
        assert app.state.triage_store is None
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=browse")
        # Priority-carrying sentinel reads triaged; priority-less doesn't.
        assert r.text.count("sentinel · untriaged") == 1
        assert "1 machine-filed (sentinel) · untriaged" in r.text


class TestReviewFixesRound2:
    """#247 re-review: guarded seeding writes, queue batches."""

    def test_seeding_write_failure_does_not_500(self, authed_client, monkeypatch):
        client, app, _ = authed_client

        def _boom(*a, **kw):
            raise OSError("read-only data repo")

        monkeypatch.setattr(app.state.triage_store, "seed_missing", _boom)
        from unittest.mock import patch as _patch

        with _patch("agent_hq.web.routes.issues.eventlog.emit") as emit:
            r = client.get("/issues/container?mode=queue")
        assert r.status_code == 200
        warns = [c for c in emit.call_args_list if c[0][:2] == ("warn", "triage seeding failed")]
        assert len(warns) == 1

    def test_queue_renders_batches_with_ungroup(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"],
                groups=[["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"]],
            ),
        )
        r = client.get("/issues/container?mode=queue")
        assert "Batch A" in r.text
        assert "/issues/ungroup?index=0&mode=queue" in r.text
        # grouped cards render once (inside the batch), not again below
        assert r.text.count('data-issue-key="ben-ralston/agent-hq#67"') == 1


class TestReviewFixesRound3:
    """#247 Codex review: proposed scores don't rank; fresh annotations
    after a tombstone/revive write."""

    def test_sync_triage_pass_annotates_post_revive_state(self, tmp_path):
        from agent_hq.triage_store import TriageStore
        from agent_hq.web.routes.issues import _sync_triage_pass

        store = TriageStore(tmp_path)
        key = "ben-ralston/agent-hq#1"
        store.upsert(
            TriageRecord(
                issue_key=key,
                value=5,
                effort=Effort("S"),
                agent_fitness=AgentFitness("solo"),
                updated_at="t0",
            ),
            actor="human",
        )
        store.apply_open_issue_set("ben-ralston/agent-hq", set(), now="t1")
        assert store.get(key).tombstoned_at == "t1"
        # First load after the issue reopens: the pass revives the record
        # and must annotate from the post-write state, not the pre-write
        # snapshot (which would render it tombstoned/scoreless for a turn).
        anns = _sync_triage_pass(store, [_mk_issue(1)])
        ann = anns[key]
        assert ann.record.tombstoned_at is None
        assert ann.score == 5 * 2 + 2 + 2

    def test_queue_does_not_rank_proposed_records(self, authed_client):
        client, app, _ = authed_client
        app.state.triage_store.upsert(
            TriageRecord(
                issue_key="ben-ralston/agent-hq#67",
                value=5,
                effort=Effort("S"),
                agent_fitness=AgentFitness("solo"),
                updated_at="t",
            ),
            actor="ai",
        )
        r = client.get("/issues/container?mode=queue")
        assert r.status_code == 200
        body = r.text
        # The proposed issue sits in the Untriaged section, not Ranked.
        assert "Untriaged" in body
        untriaged_pos = body.index("Untriaged")
        assert body.index('data-issue-key="ben-ralston/agent-hq#67"') > untriaged_pos


class TestPinToggle:
    """POST /issues/issue/{key}/pin (#247): human-only float-to-top."""

    KEY = "ben-ralston%2Fagent-hq%2367"

    def test_pin_and_unpin(self, authed_client):
        client, app, _ = authed_client
        r = client.post(f"/issues/issue/{self.KEY}/pin?on=true&mode=queue")
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.pin is True
        assert rec.state is TriageState.UNTRIAGED  # pin alone must not confirm
        assert "queue-pinned" in r.text
        r = client.post(f"/issues/issue/{self.KEY}/pin?on=false&mode=queue")
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").pin is False
        assert "queue-pinned" not in r.text

    def test_pin_survives_dimension_edit(self, authed_client):
        client, app, _ = authed_client
        client.post(f"/issues/issue/{self.KEY}/pin?on=true")
        _triage(app, 67)
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").pin is True

    def test_bad_value_400(self, authed_client):
        client, _, _ = authed_client
        assert client.post(f"/issues/issue/{self.KEY}/pin?on=maybe").status_code == 400

    def test_unknown_key_404(self, authed_client):
        client, _, _ = authed_client
        r = client.post("/issues/issue/ben-ralston%2Fagent-hq%23999999/pin?on=true")
        assert r.status_code == 404

    def test_store_none_404(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(),
            github_cache=_StubCache([_mk_issue(67)]),
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.post(f"/issues/issue/{self.KEY}/pin?on=true").status_code == 404

    def test_pin_publishes_issue_invalidated(self, authed_client):
        client, app, _ = authed_client
        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post(f"/issues/issue/{self.KEY}/pin?on=true")
        new_events = broker.replay_since(0)[before:]
        assert any(
            e.topic == "issue.invalidated" and e.data.get("key") == "ben-ralston/agent-hq#67"
            for e in new_events
        )

    def test_queue_cards_render_pin_button(self, authed_client):
        client, app, _ = authed_client
        r = client.get("/issues/container?mode=queue")
        assert 'class="pin-btn"' in r.text
        assert "/pin?on=true&mode=queue" in r.text
        client.post(f"/issues/issue/{self.KEY}/pin?on=true&mode=queue")
        r = client.get("/issues/container?mode=queue")
        assert "pin-btn--active" in r.text
        assert "/pin?on=false&mode=queue" in r.text


class TestAutoSeeding:
    """Auto-ensure seeding on container/list load (#247)."""

    def test_container_load_seeds_untriaged_records_with_hints(self, authed_client):
        client, app, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        store = app.state.triage_store
        rec = store.get("ben-ralston/agent-hq#67")
        assert rec is not None
        assert rec.state is TriageState.UNTRIAGED
        assert rec.priority_hint == "Need"
        assert store.get("ben-ralston/agent-hq#40").priority_hint == "Want"
        assert store.get("ben-ralston/agent-hq#44").priority_hint == "Idea"

    def test_closed_issue_tombstoned_on_next_load(self, authed_client):
        client, app, _ = authed_client
        client.get("/issues/container?mode=browse")
        cache = app.state.github_cache
        cache._issues = [i for i in cache._issues if i.number != 44]
        client.get("/issues/container?mode=browse")
        store = app.state.triage_store
        assert store.get("ben-ralston/agent-hq#44").tombstoned_at is not None
        assert store.get("ben-ralston/agent-hq#67").tombstoned_at is None

    def test_steady_state_load_writes_nothing(self, authed_client, tmp_path):
        client, _, _ = authed_client
        client.get("/issues/container?mode=browse")
        repo_file = tmp_path / "triage-data" / "triage" / "ben-ralston__agent-hq.json"
        assert repo_file.exists()
        stamp = repo_file.stat().st_mtime_ns
        client.get("/issues/container?mode=browse")
        assert repo_file.stat().st_mtime_ns == stamp

    def test_list_partial_also_seeds(self, authed_client):
        client, app, _ = authed_client
        client.get("/issues/list?mode=browse")
        assert app.state.triage_store.get("ben-ralston/agent-hq#67") is not None

    def test_no_store_configured_no_crash(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(),
            github_cache=_StubCache([_mk_issue(1)]),
        )
        assert app.state.triage_store is None
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.get("/issues/container?mode=browse").status_code == 200
        assert client.get("/issues?mode=browse").status_code == 200


class TestIssuesMode:
    """?mode= plumbing (#247), mirroring /agents?view= (#176)."""

    def test_explicit_mode_is_persisted(self, authed_client):
        client, app, _ = authed_client
        r = client.get("/issues?mode=browse")
        assert r.status_code == 200
        assert app.state.ui_prefs_store.get_web_issues_mode() == "browse"

    def test_bare_get_restores_stored_mode(self, authed_client):
        client, app, _ = authed_client
        app.state.ui_prefs_store.set_web_issues_mode("browse")
        r = client.get("/issues")
        assert 'data-mode="browse"' in r.text

    def test_default_is_queue(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues")
        assert 'data-mode="queue"' in r.text

    def test_junk_mode_falls_back_to_stored(self, authed_client):
        client, app, _ = authed_client
        app.state.ui_prefs_store.set_web_issues_mode("browse")
        r = client.get("/issues?mode=bogus")
        assert 'data-mode="browse"' in r.text
        assert app.state.ui_prefs_store.get_web_issues_mode() == "browse"

    def test_segmented_control_marks_active_mode(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues?mode=browse")
        assert 'href="/issues?mode=queue&keep=1"' in r.text
        assert 'href="/issues?mode=browse&keep=1"' in r.text
        assert "mode-switch__btn--active" in r.text

    def test_skeleton_container_url_carries_mode(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues?mode=browse")
        assert 'hx-get="/issues/container?mode=browse"' in r.text

    def test_container_partial_carries_data_mode(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        assert 'data-mode="browse"' in r.text

    def test_segmented_control_order_is_queue_browse_triage_hygiene(self, authed_client):
        """#268: the sub-tabs read Queue → Browse → Triage → Hygiene."""
        client, _, _ = authed_client
        r = client.get("/issues?mode=browse")
        hrefs = re.findall(r'href="/issues\?mode=(queue|browse|triage|hygiene)&keep=1"', r.text)
        assert hrefs == ["queue", "browse", "triage", "hygiene"]

    def test_skeleton_and_container_opt_into_live_target(self, authed_client):
        """#268: both renderings of ``#dispatch-container`` name the
        ``live-target`` extension, so a swap whose target was already replaced
        by an earlier response is retargeted to the live container instead of
        pulling the ``hx-preserve``'d search input out of the page."""
        client, _, _ = authed_client
        for path in ("/issues?mode=browse", "/issues/container?mode=browse"):
            r = client.get(path)
            tag = re.search(r'<div id="dispatch-container"[^>]*>', r.text)
            assert tag, path
            assert 'hx-ext="live-target"' in tag.group(0), (path, tag.group(0))


class TestDispatchPage:
    def test_renders_page_shell(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues?mode=browse")
        assert r.status_code == 200
        # Top-tab nav links and active-tab marker.
        assert 'href="/agents"' in r.text
        assert 'href="/issues"' in r.text
        # Container the action bar / list both swap into.
        assert 'id="dispatch-container"' in r.text or 'id="issue-list"' in r.text

    def test_renders_search_input(self, authed_client):
        client, _, _ = authed_client
        # /issues is shell+skeleton only (#95) — the filter bar lives in
        # the container partial that hx-trigger="load" swaps in next.
        r = client.get("/issues/container?mode=browse")
        assert 'hx-post="/issues/search?mode=' in r.text
        assert "delay:200ms" in r.text

    def test_renders_issue_cards_in_priority_order(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        # Priority order: Need (#67) → Want (#40) → Idea (#44).
        idx67 = r.text.find("#67")
        idx40 = r.text.find("#40")
        idx44 = r.text.find("#44")
        assert -1 < idx67 < idx40 < idx44

    def test_done_issues_are_excluded(self, tmp_path, monkeypatch):
        """The TUI hides Done issues from the dispatch backlog. Match
        that behavior here so a phone user doesn't see completed work
        cluttering the list."""
        issues = [
            _mk_issue(1, title="open work", status="Todo"),
            _mk_issue(2, title="finished", status="Done"),
        ]
        cache = _StubCache(issues)

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=browse")
        assert "open work" in r.text
        assert "finished" not in r.text

    def test_empty_state_when_no_projects_configured(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        empty_config = Config(
            repos={},
            projects=[],
            worktree_root=Path("/tmp"),
            dispatch=DispatchConfig(),
        )
        app = create_app(token_file=tm.token_file, config=empty_config, github_cache=_StubCache([]))
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues?mode=browse")
        assert r.status_code == 200
        # Some helpful copy when there's nothing to dispatch against.
        body_lower = r.text.lower()
        assert (
            "no projects" in body_lower
            or "projects.yaml" in body_lower
            or "configure" in body_lower
        )

    def test_unauthenticated_redirects_to_login(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=_StubCache([]))
        client = TestClient(app)
        r = client.get("/issues", follow_redirects=False)
        assert r.status_code in (302, 303)


class TestFilterBar:
    """Chip rendering — repos / priorities / statuses, dynamically
    derived from the loaded issue list (#40 decision)."""

    def test_browse_chip_groups_follow_shared_order_after_sort(self, tmp_path, monkeypatch):
        issues = [_mk_issue(67, labels=["sentinel"]), _mk_issue(40)]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        _triage(app, 67)
        r = client.get("/issues/container?mode=browse")
        assert _label_tag_order(r.text) == ["Sort", "Repo", "Src", "Status", "Eff", "Fit"]

    def test_repo_chips_appear_for_each_loaded_repo(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(1, repo="ben-ralston/agent-hq"),
            _mk_issue(2, repo="ben-ralston/widgets"),
        ]
        cache = _StubCache(issues)
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=browse")
        # Both repo chips appear, labelled by short name.
        assert ">agent-hq<" in r.text
        assert ">widgets<" in r.text
        # And both wire up to the toggle endpoint.
        assert "type=repo" in r.text

    def test_dimension_chips_reflect_present_records(self, authed_client):
        """#247: effort/fitness chips (from triage records) replace the
        old Need/Want/Idea priority chips."""
        client, app, _ = authed_client
        _triage(app, 67, effort="S", fitness="solo")
        _triage(app, 40, effort="M", fitness="pair")
        r = client.get("/issues/container?mode=browse")
        for label in (">Eff</span>", ">Fit</span>", ">S</span>", ">M</span>", ">—</span>"):
            assert label in r.text
        assert ">Pri</span>" not in r.text

    def test_active_filter_chip_marked_active(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        _set_state(client, app, tm, DispatchState(efforts=["s"]))
        r = client.get("/issues/container?mode=browse")
        # The S chip should carry an "active" class so the CSS can
        # highlight it.
        assert "filter-chip--active" in r.text


class TestIssueCard:
    def test_card_has_select_checkbox_targeting_action_bar(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        # Each card's checkbox posts to /issues/select with the issue
        # key encoded in the URL.
        assert "/issues/select" in r.text
        # Selection toggles swap the entire ``#dispatch-container`` so
        # both the action bar's count AND the card's ``is-selected``
        # class re-render together.
        assert 'hx-target="#dispatch-container"' in r.text

    def test_selected_issue_renders_checked(self, authed_client):
        client, app, tm = authed_client
        # Select #67 (key = "ben-ralston/agent-hq#67"). Selection state
        # is rendered against the htmx list partial — a full page load
        # intentionally drops selection so the user starts fresh on
        # reload (see ``test_full_page_load_clears_selection``).
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))
        r = client.get("/issues/list?mode=browse")
        assert "issue-card--selected" in r.text

    def test_grouped_issue_shows_group_label(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"],
                groups=[["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"]],
            ),
        )
        r = client.get("/issues/list?mode=browse")
        # The group label is rendered as ``G1`` (1-based) so the user
        # can match the badge to the dispatch plan.
        assert "G1" in r.text

    def test_full_page_load_clears_selection(self, authed_client):
        """A hard reload of /issues drops selected + groups so the
        user starts fresh. Filters (repos / priorities / statuses /
        query) are preserved — those track user preferences."""
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                efforts=["s"],
                query="dispatch",
                selected=["ben-ralston/agent-hq#67"],
                groups=[["ben-ralston/agent-hq#67"]],
            ),
        )
        # /issues returns shell+skeleton (#95). Selection-clearing still
        # happens on this handler — the cookie is rewritten — and the
        # container fetch sees the cleared state.
        r_shell = client.get("/issues?mode=browse")
        assert r_shell.status_code == 200
        state = _read_state(client, tm)
        assert state.selected == []
        assert state.groups == []
        assert state.efforts == ["s"]
        assert state.query == "dispatch"
        # The follow-up container fetch reflects cleared selection and
        # preserved filter chip state.
        r = client.get("/issues/container?mode=browse")
        assert "issue-card--selected" not in r.text
        assert 'aria-pressed="true"' in r.text  # Need chip still active.

    def test_wt_badge_appears_for_active_worktree(self, tmp_path, monkeypatch):
        issues = [_mk_issue(67, title="active issue")]
        cache = _StubCache(issues)
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(
            issues_routes,
            "detect_active_worktrees",
            lambda repos: {"ben-ralston/agent-hq#67"},
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=browse")
        assert "wt-tag" in r.text


class TestActionBar:
    def test_no_selection_shows_disabled_buttons(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        # Dispatch button disabled when nothing is selected.
        # We look for the ``disabled`` attribute on the dispatch button
        # specifically by anchoring the search around its hx-post URL.
        dispatch_idx = r.text.find('hx-post="/issues/dispatch?mode=')
        assert dispatch_idx != -1
        button_html = r.text[max(0, dispatch_idx - 200) : dispatch_idx + 200]
        assert "disabled" in button_html

    def test_selection_count_rendered(self, authed_client):
        client, _, _ = authed_client
        # ``/issues/select`` returns just the action bar partial after
        # toggling the cookie's selection set — the natural place to read
        # the count back out. The full GET ``/issues`` path
        # intentionally clears selection (see TestDispatchPage), so we
        # can't test the count off a hard reload anymore.
        client.post("/issues/select?key=ben-ralston/agent-hq%2367")
        r = client.post("/issues/select?key=ben-ralston/agent-hq%2340")
        # Look for "2 selected" or similar — ASCII so urlencode tricks
        # don't matter.
        assert "2 selected" in r.text or "2  selected" in r.text


class TestDispatchListPartial:
    """``GET /issues/list`` — the htmx swap target. Returns just the
    issue card list (no shell), used after a chip toggle / search to
    repaint the list in place."""

    def test_returns_partial_only(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/list?mode=browse")
        assert r.status_code == 200
        # Just the cards, no full page chrome.
        assert "<!DOCTYPE" not in r.text
        assert "tab-nav__item" not in r.text
        # But cards themselves are present.
        assert "issue-card" in r.text or "muted-separator" in r.text

    def test_filtering_via_state_cookie_applies(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(query="dispatch"))
        r = client.get("/issues/list?mode=browse")
        # Only the "dispatch view" issue (#67) matches.
        assert "#67" in r.text
        assert "#40" not in r.text
        assert "#44" not in r.text

    def test_intersects_chips_and_search(self, authed_client):
        """#44 acceptance: chip + search intersect."""
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        _set_state(client, app, tm, DispatchState(efforts=["s"], query="dispatch"))
        r = client.get("/issues/list?mode=browse")
        assert "#67" in r.text
        # #40 and #44 have no effort-S record and don't match "dispatch".
        assert "#40" not in r.text
        assert "#44" not in r.text


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _read_state(client: TestClient, tm: TokenManager) -> DispatchState:
    """Decode the dispatch_state cookie set on the test client.

    Pull from the cookie jar manually rather than via
    ``client.cookies.get`` — that helper raises CookieConflict if a
    pre-set test cookie and a Set-Cookie response cookie disagree on
    path. The most-recent cookie is the one we care about.
    """
    from fastapi import Request

    from agent_hq.web.state import read_dispatch_state

    raw = ""
    for cookie in client.cookies.jar:
        if cookie.name == STATE_COOKIE:
            raw = cookie.value or ""
    headers = [(b"cookie", f"{STATE_COOKIE}={raw}".encode())] if raw else []
    request = Request(scope={"type": "http", "method": "GET", "path": "/", "headers": headers})
    return read_dispatch_state(request, tm)


class TestFilterToggle:
    def test_chip_toggle_adds_value_to_cookie(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        r = client.post("/issues/filters/toggle?mode=browse&type=effort&value=s")
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.efforts == ["s"]

    def test_second_toggle_removes_value(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        client.post("/issues/filters/toggle?mode=browse&type=effort&value=s")
        client.post("/issues/filters/toggle?mode=browse&type=effort&value=s")
        state = _read_state(client, tm)
        assert state.efforts == []

    def test_returns_filtered_container_partial(self, authed_client):
        client, app, _ = authed_client
        _triage(app, 67, effort="S")
        r = client.post("/issues/filters/toggle?mode=browse&type=effort&value=s")
        # Container partial, not a full page (no <!DOCTYPE>).
        assert "<!DOCTYPE" not in r.text
        # Filter bar comes back with the active chip so its CSS class
        # can update — list-only swaps left the chip looking inactive.
        assert 'id="dispatch-container"' in r.text
        assert "filter-chip--active" in r.text
        # The legacy dual convention (``is-active`` alongside the BEM
        # modifier) was retired in #200 — templates emit one class only.
        assert "is-active" not in r.text
        # The effort-S issue (#67) is still visible.
        assert "#67" in r.text
        # Untriaged issues bucket as "none" and are filtered out.
        assert "#40" not in r.text
        assert "#44" not in r.text

    def test_repo_chip_toggle_uses_full_owner_name(self, authed_client):
        client, _, tm = authed_client
        r = client.post(
            f"/issues/filters/toggle?mode=browse&type=repo&value={_quote('ben-ralston/agent-hq')}"
        )
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.repos == ["ben-ralston/agent-hq"]

    def test_unknown_dimension_is_noop(self, authed_client):
        """Cookie is user-controllable and the chip URLs include a type
        param — make sure a stale URL doesn't crash."""
        client, _, tm = authed_client
        r = client.post("/issues/filters/toggle?mode=browse&type=garbage&value=x")
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state == DispatchState()


class TestSearch:
    def test_search_updates_query_in_cookie(self, authed_client):
        client, _, tm = authed_client
        r = client.post("/issues/search?mode=browse", data={"q": "dispatch"})
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.query == "dispatch"

    def test_search_returns_filtered_list(self, authed_client):
        client, _, _ = authed_client
        r = client.post("/issues/search?mode=browse", data={"q": "filter"})
        # Only #40 has "filter" in its title.
        assert "#40" in r.text
        assert "#67" not in r.text
        assert "#44" not in r.text

    def test_search_intersects_with_chip_filters(self, authed_client):
        """#44 acceptance: search ∩ chips."""
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        _set_state(client, app, tm, DispatchState(efforts=["s"]))
        r = client.post("/issues/search?mode=browse", data={"q": "dispatch"})
        # effort S ∩ "dispatch" → only #67.
        assert "#67" in r.text
        assert "#40" not in r.text
        assert "#44" not in r.text

    def test_empty_search_clears_query(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(query="prior"))
        client.post("/issues/search?mode=browse", data={"q": ""})
        state = _read_state(client, tm)
        assert state.query == ""


class TestSelect:
    def test_select_adds_key_to_cookie(self, authed_client):
        client, _, tm = authed_client
        r = client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#67')}")
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.selected == ["ben-ralston/agent-hq#67"]

    def test_select_returns_container_with_card_and_count(self, authed_client):
        client, _, _ = authed_client
        r = client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#67')}")
        # Container partial, not a full page.
        assert "<!DOCTYPE" not in r.text
        assert 'id="dispatch-container"' in r.text
        # Card's selected class is applied (so the green checkmark
        # shows immediately, not on next list re-render).
        assert "issue-card--selected" in r.text
        # And the action bar's selection count is in there.
        assert "1 selected" in r.text

    def test_second_select_removes(self, authed_client):
        client, _, tm = authed_client
        client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#67')}")
        client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#67')}")
        state = _read_state(client, tm)
        assert state.selected == []


class TestGroupUngroup:
    def test_group_bundles_ungrouped_selections(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"],
            ),
        )
        r = client.post("/issues/group")
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.groups == [["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"]]

    def test_group_requires_at_least_two_ungrouped(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))
        client.post("/issues/group")
        # Successful or not, the cookie shouldn't gain a single-key group.
        state = _read_state(client, tm)
        assert state.groups == []

    def test_group_button_enabled_for_same_repo_pair(self, authed_client):
        """Two ungrouped same-repo selections → Group button is clickable."""
        client, _, _ = authed_client
        client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#67')}")
        r = client.post(f"/issues/select?key={_quote('ben-ralston/agent-hq#40')}")
        idx = r.text.find('hx-post="/issues/group?mode=')
        assert idx != -1
        button_html = r.text[max(0, idx - 200) : idx + 200]
        assert "disabled" not in button_html

    def test_group_button_disabled_for_cross_repo_pair(self, tmp_path, monkeypatch):
        """The server already rejects cross-repo grouping silently — the
        button must be disabled too so the affordance matches reality.
        Single-repo issues have shared the prefix logic with the route's
        rsplit('#', 1) check; this test pins both halves to the same
        rule so a future refactor can't drift them apart."""
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        issues = [_mk_issue(1, repo="a/x", title="x"), _mk_issue(2, repo="b/y", title="y")]
        cache = _StubCache(issues)
        cfg = Config(
            repos={
                "a/x": RepoConfig("a/x", Path("/tmp/a")),
                "b/y": RepoConfig("b/y", Path("/tmp/b")),
            },
            projects=[ProjectConfig(name="P", number=1, owner="a")],
            worktree_root=Path("/tmp"),
            dispatch=DispatchConfig(),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=cfg, github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        client.post(f"/issues/select?key={_quote('a/x#1')}")
        r = client.post(f"/issues/select?key={_quote('b/y#2')}")
        idx = r.text.find('hx-post="/issues/group?mode=')
        assert idx != -1
        button_html = r.text[max(0, idx - 200) : idx + 200]
        assert "disabled" in button_html

    def test_group_rejects_cross_repo(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        issues = [
            _mk_issue(1, repo="a/x", title="x"),
            _mk_issue(2, repo="b/y", title="y"),
        ]
        cache = _StubCache(issues)
        cfg = Config(
            repos={
                "a/x": RepoConfig("a/x", Path("/tmp/a")),
                "b/y": RepoConfig("b/y", Path("/tmp/b")),
            },
            projects=[ProjectConfig(name="P", number=1, owner="a")],
            worktree_root=Path("/tmp"),
            dispatch=DispatchConfig(),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=cfg, github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["a/x#1", "b/y#2"]),
        )
        client.post("/issues/group")
        state = _read_state(client, tm)
        assert state.groups == []  # rejected — different repos

    def test_ungroup_drops_indexed_group(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"],
                groups=[["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"]],
            ),
        )
        client.post("/issues/ungroup?index=0")
        state = _read_state(client, tm)
        assert state.groups == []
        # Selections survive — ungroup only dissolves the batch.
        assert sorted(state.selected) == [
            "ben-ralston/agent-hq#40",
            "ben-ralston/agent-hq#67",
        ]


class TestDispatchExecution:
    def test_dispatch_invokes_dispatch_via_claude_with_batches(self, authed_client, monkeypatch):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"],
            ),
        )
        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(batches)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="feature/test")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        # BackgroundTasks run after the response is sent in TestClient
        # (httpx executes them synchronously on context exit). One repo,
        # two ungrouped issues → one call with two single-issue batches.
        assert len(captured) == 1
        batches = captured[0]
        # ungrouped → one batch per issue
        assert len(batches) == 2
        nums = sorted(b[0].number for b in batches)
        assert nums == [40, 67]

    def test_dispatch_clears_selection_on_success(self, authed_client, monkeypatch):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["ben-ralston/agent-hq#67"]),
        )

        def fake_dispatch(batches, config, **kwargs):
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        client.post("/issues/dispatch?mode=browse")
        state = _read_state(client, tm)
        assert state.selected == []
        assert state.groups == []

    def test_dispatch_failure_is_logged(self, authed_client, monkeypatch, caplog):
        """A dispatch that returns success=False must leave a server-side
        log trail, not only flip the card to FAILED (#153). The
        ``result.error`` is the only clue to why the dispatch failed.
        """
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["ben-ralston/agent-hq#67"]),
        )

        def fake_dispatch(batches, config, **kwargs):
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=False, error="Could not create pane for Claude")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        with caplog.at_level("WARNING", logger="agent_hq.web.routes.issues"):
            r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        assert any("Could not create pane for Claude" in rec.getMessage() for rec in caplog.records)

    def test_dispatch_with_grouped_batch(self, authed_client, monkeypatch):
        """Groups become single multi-issue batches; ungrouped become
        their own one-issue batches."""
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(
                selected=[
                    "ben-ralston/agent-hq#67",
                    "ben-ralston/agent-hq#40",
                    "ben-ralston/agent-hq#44",
                ],
                groups=[["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"]],
            ),
        )
        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(batches)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        client.post("/issues/dispatch?mode=browse")
        assert len(captured) == 1
        batches = captured[0]
        # Two batches: one with 2 issues (the group), one with 1 (the
        # ungrouped #44).
        sizes = sorted(len(b) for b in batches)
        assert sizes == [1, 2]


class TestRefresh:
    def test_refresh_passes_force_true(self, tmp_path, monkeypatch):
        """The refresh button must bypass the GitHubCache TTL — that's
        the whole point of the button (the TUI's ``r`` keybind)."""
        forced_calls: list[bool] = []

        class _RecordingCache:
            def __init__(self):
                self._issues: list[Issue] = []

            def get_issues(self, project_number, owner, force=False):
                forced_calls.append(force)
                return list(self._issues)

        cache = _RecordingCache()
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        client.post("/issues/refresh")
        # Refresh must call get_issues with force=True.
        assert any(forced_calls)

    def test_refresh_fetches_each_project_exactly_once(self, tmp_path, monkeypatch):
        """Regression: the prior shape force-fetched each project and then
        re-rendered, which re-called get_issues for the same projects.
        That doubled the gh load and (worse) doubled the 10 s timeout
        when gh hung. After: one call per project per refresh, all forced.
        """
        fetch_calls: list[tuple[int, str, bool]] = []

        class _Cache:
            def get_issues(self, project_number, owner, force=False):
                fetch_calls.append((project_number, owner, force))
                return []

            def get_closed_keys(self, repos, force=False):
                return set()

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        config = Config(
            repos={"r": RepoConfig("r", Path("/tmp/x"))},
            projects=[
                ProjectConfig(name="P1", number=1, owner="o"),
                ProjectConfig(name="P2", number=2, owner="o"),
                ProjectConfig(name="P3", number=3, owner="o"),
            ],
            worktree_root=Path("/tmp/wt"),
            dispatch=DispatchConfig(),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=config, github_cache=_Cache())
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        client.post("/issues/refresh")
        # Exactly one fetch per project (3), all with force=True.
        assert len(fetch_calls) == 3
        assert all(force for _, _, force in fetch_calls)
        assert {num for num, _, _ in fetch_calls} == {1, 2, 3}


class TestSkeletonOnInitialLoad:
    """``GET /issues`` renders a skeleton + a single ``hx-get`` to
    ``/issues/container`` so the first paint never blocks on GitHub
    fetches (#95). Cold-cache initial load went from 7-8s wall to under
    100ms with the skeleton swap arriving when data resolves.
    """

    def test_initial_dispatch_does_not_load_issues_synchronously(self, tmp_path, monkeypatch):
        fetch_calls = []

        class _Cache:
            def get_issues(self, project_number, owner, force=False):
                fetch_calls.append((project_number, owner, force))
                return []

            def get_closed_keys(self, repos, force=False):
                return set()

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=_Cache())
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        r = client.get("/issues?mode=browse")
        assert r.status_code == 200
        # Skeleton shell + hx-trigger='load' to fetch the real container.
        assert "issue-card--skeleton" in r.text
        assert 'hx-get="/issues/container?mode=' in r.text
        assert 'hx-trigger="load"' in r.text
        # Initial paint must not hit gh — that's the whole point.
        assert fetch_calls == []

    def test_initial_dispatch_renders_full_chrome(self, tmp_path, monkeypatch):
        """#144: the skeleton paints the FULL static chrome immediately —
        search bar, sort toggle, ghost count chips, and the empty-state
        action bar — so nothing pops in when the real container swaps and
        there is no vertical layout shift. Still no gh fetch on first paint.
        """
        fetch_calls = []

        class _Cache:
            def get_issues(self, project_number, owner, force=False):
                fetch_calls.append((project_number, owner, force))
                return []

            def get_closed_keys(self, repos, force=False):
                return set()

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=_Cache())
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        r = client.get("/issues?mode=browse")
        assert r.status_code == 200
        # Static chrome present from the first paint.
        assert 'id="dispatch-search"' in r.text
        assert 'hx-post="/issues/search?mode=' in r.text
        assert 'hx-preserve="true"' in r.text
        assert 'hx-post="/issues/sort' in r.text  # sort toggle is real
        assert "skel--chip" in r.text  # ghost count chips
        assert 'id="action-bar"' in r.text
        assert "No selections" in r.text  # empty-state action bar
        # But the real (data-derived) chips must NOT appear yet (#40: their
        # set isn't known until issues load) — only ghost pills.
        assert "/issues/filters/toggle" not in r.text
        # And still a skeleton that fetches the container without hitting gh.
        assert "issue-card--skeleton" in r.text
        assert 'hx-trigger="load"' in r.text
        assert fetch_calls == []

    def test_container_renders_real_chips_not_ghost(self, authed_client):
        """The real container (post-load) renders the data-derived chips and
        never the ghost ``skel--chip`` placeholders — proving the ``skeleton``
        flag defaults to falsy outside the skeleton template."""
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        assert "/issues/filters/toggle" in r.text  # real chips
        assert "skel--chip" not in r.text


class TestStaleWhileRevalidate:
    """#226: expired-but-present cache data is served instantly with an
    "Updating…" indicator while ONE background refresh runs; its
    completion publishes ``dispatch.refreshed`` so open tabs refetch the
    container. Cold (never-fetched) cache keeps the blocking behavior.

    Background tasks need ``with TestClient(app)`` — the context manager
    keeps one portal event loop alive across requests; without it each
    request gets its own loop and the created task is orphaned.
    """

    def _build(self, tmp_path, monkeypatch, cache):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        return app, tm

    def _wait_refresh_done(self, app, timeout: float = 5.0) -> None:
        import time as _time

        deadline = _time.time() + timeout
        while app.state.issues_refresher.in_flight:
            if _time.time() > deadline:
                raise AssertionError("background refresh never finished")
            _time.sleep(0.01)

    def test_stale_cache_serves_immediately_with_indicator(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67, title="stale issue")], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=browse")
            assert r.status_code == 200
            assert "stale issue" in r.text
            assert "refresh-indicator" in r.text
            self._wait_refresh_done(app)
        # The route itself never called the blocking fetch — only the
        # background task did, and NOT forced (#255 review): the background
        # sweep stays subject to the GraphQL budget gate.
        assert cache.get_issues_calls == [False]

    def test_cold_cache_blocks_like_today_no_indicator(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67, title="first load")], cold=True)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=browse")
            assert r.status_code == 200
            assert "first load" in r.text
            assert "refresh-indicator" not in r.text
        # Cold path = today's blocking, non-forced fetch; no background kick.
        assert cache.get_issues_calls == [False]
        assert not app.state.issues_refresher.in_flight

    def test_fresh_cache_no_indicator_no_kick(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67)], fresh=True)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=browse")
            assert r.status_code == 200
            assert "refresh-indicator" not in r.text
        assert cache.get_issues_calls == []
        assert not app.state.issues_refresher.in_flight

    def test_stale_serve_triggers_single_background_refresh(self, tmp_path, monkeypatch):
        import threading

        release = threading.Event()

        class _SlowCache(_StubCache):
            def get_issues(self, project_number, owner, force=False):
                # Blocks on ANY fetch: the background sweep no longer
                # forces, so gating on ``force`` would never block.
                release.wait(timeout=5)
                return super().get_issues(project_number, owner, force)

        cache = _SlowCache([_mk_issue(67)], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r1 = client.get("/issues/container?mode=browse")
            r2 = client.get("/issues/container?mode=browse")
            assert r1.status_code == 200 and r2.status_code == 200
            # Both stale serves show the indicator; the second coalesced
            # onto the in-flight refresh instead of stacking a fetch.
            assert "refresh-indicator" in r1.text
            assert "refresh-indicator" in r2.text
            release.set()
            self._wait_refresh_done(app)
        # Exactly one fetch across both stale serves, and not forced.
        assert cache.get_issues_calls == [False]

    def test_refresh_completion_publishes_dispatch_refreshed(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67)], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=browse")
            self._wait_refresh_done(app)
        events = [e for e in app.state.broker.replay_since(0) if e.topic == "dispatch.refreshed"]
        assert len(events) == 1
        assert events[0].data == {"status": "ok"}

    def test_failed_refresh_publishes_error_and_cooldown_suppresses_indicator(
        self, tmp_path, monkeypatch
    ):
        # ``stuck_stale``: forced fetches never freshen the cache — models a
        # persistently failing gh whose errors _load_issues swallows to [].
        cache = _StubCache([_mk_issue(67)], fresh=False, stuck_stale=True)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r1 = client.get("/issues/container?mode=browse")
            assert "refresh-indicator" in r1.text
            self._wait_refresh_done(app)
            # The completion event still fires (tabs refetch once)…
            events = [
                e for e in app.state.broker.replay_since(0) if e.topic == "dispatch.refreshed"
            ]
            assert len(events) == 1
            assert events[0].data == {"status": "error"}
            # …but the refetch lands in the cooldown: stale data WITHOUT the
            # indicator, and no second forced fetch (no refresh loop).
            r2 = client.get("/issues/container?mode=browse")
            assert r2.status_code == 200
            assert "refresh-indicator" not in r2.text
        assert cache.get_issues_calls == [False]

    def test_refresh_button_still_blocks_and_forces(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67, title="forced fresh")], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.post("/issues/refresh?mode=browse")
            assert r.status_code == 200
            assert "forced fresh" in r.text
            # The explicit refresh returns fresh data — no indicator.
            assert "refresh-indicator" not in r.text
        assert cache.get_issues_calls == [True]

    def test_search_and_chip_routes_serve_stale_without_blocking(self, tmp_path, monkeypatch):
        cache = _StubCache([_mk_issue(67, title="findme")], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.post("/issues/search?mode=browse", data={"q": "findme"})
            assert r.status_code == 200
            assert "findme" in r.text
            r = client.post("/issues/filters/toggle?mode=browse&type=repo&value=x%2Fy")
            assert r.status_code == 200
            self._wait_refresh_done(app)
        # Neither route fetched synchronously: the only call is the single
        # coalesced background sweep, not one per route.
        assert cache.get_issues_calls == [False]

    def test_issue_detail_resolve_does_not_kick_refresh(self, tmp_path, monkeypatch):
        """Review fix (#226): a background refresh kicked from the detail
        resolver races the priority/status POST's in-place cache mutation
        (the in-flight fetch can overwrite the optimistic write with
        pre-edit data). Detail opens serve stale WITHOUT kicking; freshness
        converges via the container routes."""
        issue = _mk_issue(67, title="detail race")
        cache = _StubCache([issue], fresh=False)
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get(f"/issues/issue/{_quote(issue.key)}")
            assert r.status_code == 200
        assert cache.get_issues_calls == []
        assert not app.state.issues_refresher.in_flight

    def test_cold_closed_keys_fetched_blocking_when_absent(self, tmp_path, monkeypatch):
        """Review fix (#226): the closed-keys cache is in-memory only while
        issues persist to disk — after a service restart the SWR path must
        not render closed/Won't-Do issues. A completely cold closed cache is
        fetched blocking (once, TTL-cached) before the render."""
        open_issue = _mk_issue(67, title="still open")
        closed_issue = _mk_issue(68, title="wont do")
        cache = _StubCache([open_issue, closed_issue], fresh=True, closed_cold=True)
        cache._closed_keys = {closed_issue.key}
        app, tm = self._build(tmp_path, monkeypatch, cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=browse")
            assert r.status_code == 200
            assert "still open" in r.text
            assert "wont do" not in r.text
        assert cache.get_closed_keys_calls == [False]

    def test_background_refresh_only_fetches_stale_projects(self, tmp_path, monkeypatch):
        """Review fix (#226): the background refresh must not force-refetch
        already-fresh projects — otherwise one permanently-failing project
        turns every cooldown expiry into a full forced sweep of all N
        projects (rate-limit burn)."""
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        cache = _StubCache([_mk_issue(67)], fresh=True, stale_projects={2})
        config = Config(
            repos={"r": RepoConfig("r", Path("/tmp/x"))},
            projects=[
                ProjectConfig(name="P1", number=1, owner="o"),
                ProjectConfig(name="P2", number=2, owner="o"),
                ProjectConfig(name="P3", number=3, owner="o"),
            ],
            worktree_root=Path("/tmp/wt"),
            dispatch=DispatchConfig(),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=config, github_cache=cache)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=browse")
            assert r.status_code == 200
            assert "refresh-indicator" in r.text
            self._wait_refresh_done(app)
        assert cache.fetched_projects == [2]

    async def test_successful_dispatch_invalidates_worktree_cache(self, monkeypatch):
        """#226: worktree detection is now TTL-cached, so a dispatch that
        just created a worktree must bust the cache — otherwise the new
        'active worktree' badge lags up to the TTL."""
        from agent_hq.dispatch import DispatchResult
        from agent_hq.web.routes import issues as issues_routes

        invalidations: list[int] = []
        monkeypatch.setattr(
            issues_routes, "invalidate_worktree_cache", lambda: invalidations.append(1)
        )
        monkeypatch.setattr(
            issues_routes,
            "dispatch_via_claude",
            lambda batches, config, **kwargs: DispatchResult(success=True, branch="feature/1-x"),
        )
        await issues_routes._run_dispatch_bg([[[_mk_issue(1)]]], config=None, broker=None)
        assert invalidations == [1]

    async def test_failed_dispatch_does_not_invalidate_worktree_cache(self, monkeypatch):
        from agent_hq.dispatch import DispatchResult
        from agent_hq.web.routes import issues as issues_routes

        invalidations: list[int] = []
        monkeypatch.setattr(
            issues_routes, "invalidate_worktree_cache", lambda: invalidations.append(1)
        )
        monkeypatch.setattr(
            issues_routes,
            "dispatch_via_claude",
            lambda batches, config, **kwargs: DispatchResult(success=False, error="boom"),
        )
        await issues_routes._run_dispatch_bg([[[_mk_issue(1)]]], config=None, broker=None)
        assert invalidations == []

    def test_agent_hq_js_listens_for_dispatch_refreshed(self):
        js = (
            Path(__file__).parent.parent / "agent_hq" / "web" / "static" / "agent_hq.js"
        ).read_text()
        assert "dispatch.refreshed" in js

class TestTriageWorkerTrigger:
    """#248: a container load whose annotate pass finds worker targets kicks
    exactly one background triage run (singleflight); completion publishes
    ``triage.proposed``. Needs ``with TestClient(app)`` (see
    TestStaleWhileRevalidate) so the background task shares one event loop.
    """

    def _build(self, tmp_path, monkeypatch, issues, *, ai_enabled=True, gate=None, timeouts=None):
        from agent_hq.triage_worker import RepoRunResult, TriageRunReport
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        monkeypatch.setattr(issues_routes.shutil, "which", lambda name: "/usr/local/bin/claude")
        calls = []

        def fake_run_triage(issues_arg, store, triage_cfg, repos_cfg, **kw):
            calls.append([i.number for i in issues_arg])
            if gate is not None:
                # Hold the run open (it lives in a threadpool worker, so the
                # event loop stays responsive) — that's the only way a second
                # request can observe the singleflight. A bare assert here
                # would be swallowed by BackgroundRefresher._run, so record
                # the timeout for the test to assert on instead.
                if not gate.wait(timeout=15) and timeouts is not None:
                    timeouts.append(True)
            return TriageRunReport(
                repos=[
                    RepoRunResult(
                        repo="ben-ralston/agent-hq",
                        targeted=len(issues_arg),
                        proposed=len(issues_arg),
                    )
                ],
                dry_run=False,
            )

        monkeypatch.setattr(issues_routes, "run_triage", fake_run_triage)
        tmp_path.mkdir(parents=True, exist_ok=True)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        cfg = _mk_config(
            triage=TriageConfig(data_dir=tmp_path / "triage-data", ai_enabled=ai_enabled)
        )
        app = create_app(
            token_file=tm.token_file,
            config=cfg,
            github_cache=_StubCache(issues),
            triage_store=TriageStore(tmp_path / "triage-data"),
        )
        return app, tm, calls

    def _wait(self, app, timeout=5.0):
        import time as _time

        deadline = _time.time() + timeout
        while app.state.triage_refresher.in_flight:
            if _time.time() > deadline:
                raise AssertionError("triage worker never finished")
            _time.sleep(0.01)

    def test_untriaged_issues_kick_one_worker_run(self, tmp_path, monkeypatch):
        import threading

        release = threading.Event()
        timed_out: list[bool] = []
        app, tm, calls = self._build(
            tmp_path,
            monkeypatch,
            [_mk_issue(67), _mk_issue(40)],
            gate=release,
            timeouts=timed_out,
        )
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/issues/container?mode=queue")
            assert r.status_code == 200
            client.get("/issues/container?mode=queue")  # coalesces — no second run
            release.set()
            self._wait(app)
        # The gate really held the run open across both requests — otherwise
        # the singleflight assertion below would be vacuous.
        assert not any(timed_out)
        assert calls == [[67, 40]]

    def test_no_kick_when_everything_confirmed(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        _triage(app, 67)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_no_kick_when_ai_disabled(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)], ai_enabled=False)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_no_kick_when_claude_missing(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        monkeypatch.setattr(issues_routes.shutil, "which", lambda name: None)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_completion_publishes_triage_proposed(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        published = []

        async def fake_publish(event):
            published.append(event.topic)
            return True

        with TestClient(app) as client:
            monkeypatch.setattr(app.state.broker, "publish", fake_publish)
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert "triage.proposed" in published

    def test_failed_run_trips_cooldown(self, tmp_path, monkeypatch):
        from agent_hq.triage_worker import RepoRunResult, TriageRunReport
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        monkeypatch.setattr(
            issues_routes,
            "run_triage",
            lambda *a, **k: TriageRunReport(
                repos=[RepoRunResult(repo="ben-ralston/agent-hq", targeted=1, error="parse: x")],
                dry_run=False,
            ),
        )
        published = []

        async def fake_publish(event):
            published.append(event.topic)
            return True

        with TestClient(app) as client:
            monkeypatch.setattr(app.state.broker, "publish", fake_publish)
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
            # The publish happens BEFORE the cooldown-tripping raise, so a
            # failures-only run still tells open tabs to refetch.
            assert "triage.proposed" in published
            # Inside the 600 s cooldown a fresh kick is refused.
            assert app.state.triage_refresher.kick(_noop_coro) is False

    def test_zero_progress_run_trips_cooldown(self, tmp_path, monkeypatch):
        """Final-review finding 1: a run that targets issues but proposes
        nothing (model omitted every issue) is zero progress — the next
        container load selects the same targets and would kick again, a paid
        ``claude -p`` call every refetch. No proposals from a non-empty batch
        must trip the cooldown even when nothing errored."""
        from agent_hq.triage_worker import RepoRunResult, TriageRunReport
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        monkeypatch.setattr(
            issues_routes,
            "run_triage",
            lambda *a, **k: TriageRunReport(
                repos=[RepoRunResult(repo="ben-ralston/agent-hq", targeted=1, proposed=0)],
                dry_run=False,
            ),
        )
        published = []

        async def fake_publish(event):
            published.append(event.topic)
            return True

        with TestClient(app) as client:
            monkeypatch.setattr(app.state.broker, "publish", fake_publish)
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
            # Open tabs still get told to refetch (the "running…" pill clears)…
            assert "triage.proposed" in published
            # …but the refresher refuses the next kick for the cooldown window.
            assert app.state.triage_refresher.kick(_noop_coro) is False

    def test_no_op_run_publishes_nothing_and_keeps_the_refresher_free(self, tmp_path, monkeypatch):
        """Review fix 5: a run that targeted nothing did nothing. Publishing
        ``triage.proposed`` makes every open tab refetch the container for no
        reason, and tripping the cooldown would block a later real run."""
        from agent_hq.triage_worker import TriageRunReport
        from agent_hq.web.routes import issues as issues_routes

        app, tm, _ = self._build(tmp_path, monkeypatch, [_mk_issue(67)])
        runs: list[int] = []
        monkeypatch.setattr(
            issues_routes,
            "run_triage",
            lambda *a, **k: runs.append(1) or TriageRunReport(repos=[], dry_run=False),
        )
        published = []

        async def fake_publish(event):
            published.append(event.topic)
            return True

        with TestClient(app) as client:
            monkeypatch.setattr(app.state.broker, "publish", fake_publish)
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
            assert "triage.proposed" not in published
            # No cooldown was tripped, so the next load kicks a fresh run.
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert len(runs) == 2

    def test_agent_hq_js_listens_for_triage_proposed(self):
        js = (
            Path(__file__).parent.parent / "agent_hq" / "web" / "static" / "agent_hq.js"
        ).read_text()
        assert "triage.proposed" in js
        assert "triage-badge" in js


class TestDispatchInflightFeedback:
    """POST /issues/dispatch renders the just-dispatched cards in the
    ``issue-card--inflight`` state on the immediate response (#95) so
    the user gets feedback before the background task's first SSE event
    even fires. SSE ``dispatch.completed`` then transitions them via JS.
    """

    def test_dispatch_post_marks_selected_cards_inflight(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        monkeypatch.setattr(
            issues_routes,
            "dispatch_via_claude",
            lambda batches, config, **kwargs: type(
                "R", (), {"success": True, "branch": "feature/x", "warning": None}
            )(),
        )

        issues = [
            _mk_issue(67, title="dispatch view", priority=Priority.NEED),
            _mk_issue(40, title="filter bar", priority=Priority.WANT),
        ]
        cache = _StubCache(issues)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        # The dispatched card is in the inflight class so CSS can spin it.
        assert "issue-card--inflight" in r.text
        # The "Starting…" tag is rendered inline so the user sees the
        # state even before the SSE event arrives.
        assert "Starting" in r.text


class TestLoadIssuesConcurrency:
    """``_load_issues`` fetches all projects concurrently (#90).

    With N projects of d seconds each, serial latency is N*d, parallel
    is ~d. Cold-cache /issues refresh was 7-8s with 4 projects → after
    parallelization should be ~max-of-4, around 2-2.5s.
    """

    async def test_load_issues_runs_projects_in_parallel(self, tmp_path):
        import time

        from agent_hq.web.routes.issues import _load_issues

        starts: list[float] = []
        finishes: list[float] = []
        delay = 0.2  # per-project simulated fetch time

        class _SlowCache:
            def get_issues(self, project_number, owner, force=False):
                starts.append(time.monotonic())
                time.sleep(delay)
                finishes.append(time.monotonic())
                return []

            def get_closed_keys(self, repos, force=False):
                return set()

        config = Config(
            repos={"r": RepoConfig("r", Path("/tmp/x"))},
            projects=[ProjectConfig(name=f"P{i}", number=i, owner="o") for i in range(4)],
            worktree_root=Path("/tmp/wt"),
            dispatch=DispatchConfig(),
        )

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=config, github_cache=_SlowCache())

        await _load_issues(app)

        # Overlap is the load-bearing invariant: every fetch must start
        # before any fetch finishes. (Wall-clock thresholds flake under
        # threadpool contention on CI.)
        assert len(starts) == 4
        assert max(starts) < min(finishes), "Fetches were serialized, not concurrent"


class TestIssueDetail:
    """``GET /issues/issue/{key}`` — modal body for a single issue."""

    def test_returns_detail_partial(self, authed_client):
        client, _, _ = authed_client
        key = _quote("ben-ralston/agent-hq#67")
        r = client.get(f"/issues/issue/{key}")
        assert r.status_code == 200
        # Partial only — no full-page chrome.
        assert "<!DOCTYPE" not in r.text
        assert "tab-nav__item" not in r.text

    def test_renders_triage_head_and_repo_meta(self, authed_client):
        client, app, _ = authed_client
        key = _quote("ben-ralston/agent-hq#67")
        r = client.get(f"/issues/issue/{key}")
        # No triage record yet → state badge; repo · #N + status meta stay.
        assert "untriaged" in r.text
        assert "ben-ralston/agent-hq" in r.text
        assert "#67" in r.text
        # A fully-triaged record renders the rank chip + breakdown instead.
        _triage(app, 67)
        r = client.get(f"/issues/issue/{key}")
        assert 'class="rank-chip"' in r.text
        assert "rank-breakdown" in r.text
        # Status label rendered using the picker's human form
        # ("To Do" / "In Progress" / "Done") rather than the gh raw value.
        assert "To Do" in r.text
        # Title is shown in the title block.
        assert "dispatch view" in r.text

    def test_renders_status_picker_without_blocked(self, authed_client):
        client, _, _ = authed_client
        key = _quote("ben-ralston/agent-hq#67")
        r = client.get(f"/issues/issue/{key}")
        # Status picker exposes Todo / In Progress / Done — Blocked is
        # tracked separately in #76 and intentionally absent.
        assert "todo" in r.text.lower()
        assert "in-progress" in r.text.lower()
        assert "done" in r.text.lower()
        assert "blocked" not in r.text.lower()

    def test_renders_open_in_github_link(self, authed_client):
        client, _, _ = authed_client
        key = _quote("ben-ralston/agent-hq#67")
        r = client.get(f"/issues/issue/{key}")
        # Anchor goes to github.com with the right owner/repo/number.
        assert 'href="https://github.com/ben-ralston/agent-hq/issues/67"' in r.text
        assert 'target="_blank"' in r.text
        assert 'rel="noopener noreferrer"' in r.text

    def test_renders_collapsible_description(self, tmp_path, monkeypatch):
        """A non-empty body is rendered inside a closed ``<details>``."""
        issues = [
            _mk_issue(
                42,
                title="markdown body",
                body="## Heading\n\n- one\n- two\n\n**bold** and `code`",
            ),
        ]
        cache = _StubCache(issues)
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())

        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

        r = client.get(f"/issues/issue/{_quote('ben-ralston/agent-hq#42')}")
        # <details> wrapper for the body.
        assert "<details" in r.text
        # markdown-it output: heading, list, strong, code.
        assert "<h2>" in r.text
        assert "<ul>" in r.text
        assert "<strong>" in r.text
        assert "<code>" in r.text

    def test_renders_wt_tag_when_active_worktree(self, tmp_path, monkeypatch):
        issues = [_mk_issue(67, title="active issue")]
        cache = _StubCache(issues)
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(
            issues_routes,
            "detect_active_worktrees",
            lambda repos: {"ben-ralston/agent-hq#67"},
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get(f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}")
        assert "wt-tag" in r.text

    def test_unknown_key_returns_404(self, authed_client):
        client, _, _ = authed_client
        r = client.get(f"/issues/issue/{_quote('ben-ralston/agent-hq#9999')}")
        assert r.status_code == 404

    def test_body_issue_carries_split_layout_hooks(self, authed_client):
        """An issue with a body (fixture #44) renders the desktop
        split-layout hooks (#224): an ``issue-detail--has-body`` root and an
        ``issue-detail__controls`` wrapper around the pickers/actions, so CSS
        can lay the description out beside the controls on wide viewports."""
        client, _, _ = authed_client
        r = client.get(f"/issues/issue/{_quote('ben-ralston/agent-hq#44')}")
        assert "issue-detail--has-body" in r.text
        assert "issue-detail__controls" in r.text

    def test_bodyless_issue_omits_has_body_modifier(self, authed_client):
        """Fixture #67 has no body: the root renders without the has-body
        modifier so the modal keeps its compact single-column width."""
        client, _, _ = authed_client
        r = client.get(f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}")
        # Exact attribute — a bare "issue-detail" substring would also match
        # the always-rendered issue-detail__controls class.
        assert 'class="issue-detail"' in r.text
        assert "issue-detail--has-body" not in r.text


class TestIssueDetailDesktopCss:
    """The desktop split-layout CSS (#224) must reference exactly the class
    hooks the template renders — renaming either side breaks the pairing."""

    def test_components_css_scopes_wide_modal_to_has_body(self):
        import agent_hq.web as web_pkg

        css = (Path(web_pkg.__file__).parent / "static" / "components.css").read_text()
        # Widening is gated on the has-body modifier via :has(), so a
        # bodyless issue keeps today's compact modal.
        assert ".modal__panel:has(.issue-detail--has-body" in css
        # The grid rules stay scoped to the modal — the partial rendered
        # into any other host must not inherit the split columns.
        assert ".modal__panel .issue-detail--has-body" in css
        # The controls wrapper is the split layout's left column.
        assert ".issue-detail__controls" in css

    def test_agent_hq_js_references_split_layout_hook(self):
        """agent_hq.js auto-opens the description when the split layout is
        active — its selector is the third leg of the template/CSS/JS class
        pairing and must track a rename of the modifier."""
        import agent_hq.web as web_pkg

        js = (Path(web_pkg.__file__).parent / "static" / "agent_hq.js").read_text()
        assert ".issue-detail--has-body" in js


class TestSetIssueStatus:
    """``POST /issues/issue/{key}/status`` — write Status to the
    GitHub Project, optimistically update the cached Issue, and return
    the redrawn modal body."""

    def test_valid_value_calls_cache_set_status(self, authed_client):
        client, app, _ = authed_client
        captured: list[tuple] = []
        app.state.github_cache.set_status = lambda issue, owner, label: (
            captured.append((issue.number, owner, label)) or True
        )
        r = client.post(
            f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/status",
            data={"value": "in-progress"},
        )
        assert r.status_code == 200
        assert captured == [(67, "ben-ralston", "In Progress")]

    def test_blocked_status_rejected(self, authed_client):
        """``Blocked`` is tracked in #76 and not yet supported here.
        Reject with 400 so a stale client can't set a Status this view
        doesn't know how to render."""
        client, _, _ = authed_client
        r = client.post(
            f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/status",
            data={"value": "blocked"},
        )
        assert r.status_code == 400

    def test_invalid_value_returns_400(self, authed_client):
        client, _, _ = authed_client
        r = client.post(
            f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/status",
            data={"value": "bogus"},
        )
        assert r.status_code == 400

    def test_unknown_key_returns_404(self, authed_client):
        client, _, _ = authed_client
        r = client.post(
            f"/issues/issue/{_quote('ben-ralston/agent-hq#9999')}/status",
            data={"value": "todo"},
        )
        assert r.status_code == 404


class TestCloseIssueNotPlanned:
    """``POST /issues/issue/{key}/close-not-planned`` — close the
    underlying GitHub issue with the ``not planned`` reason, prime the
    closed-keys cache, and SSE-invalidate so the list drops it."""

    def test_close_calls_github_and_marks_closed(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        from agent_hq.web.routes import issues as issues_routes

        captured: list[tuple] = []
        monkeypatch.setattr(
            issues_routes,
            "close_issue_not_planned",
            lambda repo, number: captured.append((repo, number)) or True,
        )
        r = client.post(f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/close-not-planned")
        assert r.status_code == 200
        assert captured == [("ben-ralston/agent-hq", 67)]
        assert ("ben-ralston/agent-hq", 67) in app.state.github_cache.__dict__.get(
            "_marked_closed", []
        )

    def test_close_failure_does_not_mark_closed(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "close_issue_not_planned", lambda repo, number: False)
        r = client.post(f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/close-not-planned")
        assert r.status_code == 200
        assert ("ben-ralston/agent-hq", 67) not in app.state.github_cache.__dict__.get(
            "_marked_closed", []
        )

    def test_unknown_key_returns_404(self, authed_client):
        client, _, _ = authed_client
        r = client.post(f"/issues/issue/{_quote('ben-ralston/agent-hq#9999')}/close-not-planned")
        assert r.status_code == 404

    def test_closed_issues_excluded_from_backlog(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(1, title="open work", status="Todo"),
            _mk_issue(2, title="abandoned", status="Todo"),
        ]
        cache = _StubCache(issues)
        cache._closed_keys = {"ben-ralston/agent-hq#2"}

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=browse")
        assert "open work" in r.text
        assert "abandoned" not in r.text

    def test_detail_renders_close_button(self, authed_client):
        client, _, _ = authed_client
        key = _quote("ben-ralston/agent-hq#67")
        r = client.get(f"/issues/issue/{key}")
        assert "/close-not-planned" in r.text
        assert "Won't Do" in r.text or "Won&#39;t Do" in r.text


class TestMarkdownFilter:
    """The ``markdown`` Jinja filter renders issue bodies inside the
    modal's collapsible Description block. Confirm the filter is wired
    up and produces the expected HTML elements."""

    def test_filter_renders_common_markdown(self):
        from agent_hq.web.routes.issues import templates

        env = templates.env
        assert "markdown" in env.filters
        body = "## H\n\n- one\n- two\n\n**bold** and `code` and [t](u)"
        rendered = str(env.filters["markdown"](body))
        assert "<h2>" in rendered
        assert "<ul>" in rendered
        assert "<strong>" in rendered
        assert "<code>" in rendered
        assert '<a href="u"' in rendered

    def test_filter_handles_empty_body(self):
        from agent_hq.web.routes.issues import templates

        env = templates.env
        rendered = str(env.filters["markdown"](""))
        assert rendered == ""
        rendered = str(env.filters["markdown"](None))
        assert rendered == ""


class TestDispatchEndpointPublishesSSE:
    """``dispatch.started`` and ``dispatch.completed`` events flow through
    the SSE broker so connected tabs can toast / refresh the list. Without
    this, the user taps Dispatch and gets no feedback until they manually
    refresh."""

    def test_dispatch_started_event_published(self, authed_client, monkeypatch):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["ben-ralston/agent-hq#67"]),
        )

        def fake_dispatch(batches, config, **kwargs):
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)

        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/issues/dispatch?mode=browse")
        new_events = broker.replay_since(0)[before:]
        topics = {e.topic for e in new_events}
        assert "dispatch.started" in topics
        assert "dispatch.completed" in topics
        # issue_keys travels with every dispatch event so the client can
        # find the right card by data-issue-key (#95).
        for e in new_events:
            if e.topic in ("dispatch.started", "dispatch.completed", "dispatch.failed"):
                assert "ben-ralston/agent-hq#67" in e.data.get("issue_keys", [])

    def test_dispatch_failed_event_when_dispatch_returns_error(self, authed_client, monkeypatch):
        """When dispatch_via_claude returns ``DispatchResult(success=False)``,
        a ``dispatch.failed`` SSE event must fire carrying ``issue_keys`` so
        the client can flip the affected cards to the failed state (#95)."""
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        def fake_dispatch(batches, config, **kwargs):
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=False, branch="x", error="tmux session not found")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)

        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/issues/dispatch?mode=browse")
        new_events = broker.replay_since(0)[before:]
        failed = [e for e in new_events if e.topic == "dispatch.failed"]
        assert len(failed) == 1
        assert "ben-ralston/agent-hq#67" in failed[0].data.get("issue_keys", [])
        assert failed[0].data.get("error") == "tmux session not found"

    def test_dispatch_failed_event_when_dispatch_raises(self, authed_client, monkeypatch):
        """When dispatch_via_claude itself raises, the background task must
        still publish ``dispatch.failed`` with ``issue_keys`` rather than
        silently dropping the dispatch from the UI's perspective."""
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        def boom(batches, config, **kwargs):
            raise RuntimeError("worktree-create not found on PATH")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", boom)

        broker = app.state.broker
        before = len(broker.replay_since(0))
        client.post("/issues/dispatch?mode=browse")
        new_events = broker.replay_since(0)[before:]
        failed = [e for e in new_events if e.topic == "dispatch.failed"]
        assert len(failed) == 1
        assert "ben-ralston/agent-hq#67" in failed[0].data.get("issue_keys", [])
        # Exception message is preserved so the toast can surface it.
        assert "worktree-create" in failed[0].data.get("error", "")


def _sort_button(html: str, value: str) -> str:
    """Return the ``<button>`` markup for the sort control with ``value``."""
    import re

    m = re.search(
        r"<button[^>]*?/issues/sort\?value=" + re.escape(value) + r"\b[^>]*?>",
        html,
    )
    assert m, f"no sort button for value={value!r}"
    return m.group(0)


def _sort_button_label(html: str, value: str) -> str:
    """Return the full ``<button>…</button>`` (incl. inner label) for a sort
    button — used to assert the direction arrow rendered inside the label."""
    import re

    m = re.search(
        r"<button[^>]*?/issues/sort\?value=" + re.escape(value) + r"\b.*?</button>",
        html,
        re.DOTALL,
    )
    assert m, f"no sort button for value={value!r}"
    return m.group(0)


class TestSortControl:
    """Issue #156: a Default / Created / Updated sort control on the web
    dispatch view whose date buttons carry per-field direction arrows and
    compose with chip filters + search."""

    def _dated_issues(self):
        # All NEED / same repo so default order is by number; created order
        # differs, proving the sort actually reorders. updated_at mirrors
        # created_at here unless a test needs them to diverge.
        return [
            _mk_issue(
                1,
                title="alpha",
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            ),
            _mk_issue(
                2,
                title="beta",
                created_at="2026-05-20T00:00:00Z",
                updated_at="2026-05-20T00:00:00Z",
            ),
            _mk_issue(
                3,
                title="gamma",
                created_at="2026-05-10T00:00:00Z",
                updated_at="2026-05-10T00:00:00Z",
            ),
        ]

    def test_sort_route_sets_cookie(self, authed_client):
        client, _, tm = authed_client
        r = client.post("/issues/sort?mode=browse&value=created")
        assert r.status_code == 200
        assert _read_state(client, tm).sort == "created"

    def test_sort_route_unknown_value_noops(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(sort="created"))
        r = client.post("/issues/sort?mode=browse&value=bogus")
        assert r.status_code == 200
        # Junk is ignored — the prior valid mode stands.
        assert _read_state(client, tm).sort == "created"

    def test_sort_route_default_resets(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(sort="created"))
        client.post("/issues/sort?mode=browse&value=default")
        assert _read_state(client, tm).sort == "default"

    def test_reclick_active_date_button_flips_direction(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(sort="created"))
        client.post("/issues/sort?mode=browse&value=created")
        assert _read_state(client, tm).created_dir == "asc"

    def test_created_descending_orders_rendered_list(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="created"))
        r = client.get("/issues/list?mode=browse")
        # Newest created first: #2 (May20) → #3 (May10) → #1 (May01).
        i2, i3, i1 = r.text.find("#2"), r.text.find("#3"), r.text.find("#1")
        assert -1 < i2 < i3 < i1

    def test_created_ascending_orders_rendered_list(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="created", created_dir="asc"))
        r = client.get("/issues/list?mode=browse")
        # Oldest created first: #1 (May01) → #3 (May10) → #2 (May20).
        i1, i3, i2 = r.text.find("#1"), r.text.find("#3"), r.text.find("#2")
        assert -1 < i1 < i3 < i2

    def test_sort_composes_with_effort_chip(self, tmp_path, monkeypatch):
        issues = [
            _mk_issue(1, created_at="2026-05-01T00:00:00Z"),
            _mk_issue(2, created_at="2026-05-20T00:00:00Z"),
            _mk_issue(3, created_at="2026-05-30T00:00:00Z"),
        ]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _triage(app, 1, effort="S")
        _triage(app, 2, effort="S")
        _set_state(client, app, tm, DispatchState(efforts=["s"], sort="created"))
        r = client.get("/issues/list?mode=browse")
        # Untriaged #3 filtered out despite being newest; S issues newest-first.
        assert "#3" not in r.text
        i2, i1 = r.text.find("#2"), r.text.find("#1")
        assert -1 < i2 < i1

    def test_active_button_highlighted(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="created"))
        # /issues is shell+skeleton only (#95) — the sort control lives in
        # the filter bar inside the container partial.
        html = client.get("/issues/container?mode=browse").text
        assert 'aria-pressed="true"' in _sort_button(html, "created")
        assert 'aria-pressed="false"' in _sort_button(html, "default")

    def test_default_active_when_unset(self, authed_client):
        client, _, _ = authed_client
        html = client.get("/issues/container?mode=browse").text
        assert 'aria-pressed="true"' in _sort_button(html, "default")
        assert 'aria-pressed="false"' in _sort_button(html, "created")

    def test_date_buttons_render_direction_arrow(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="created", created_dir="asc"))
        html = client.get("/issues/container?mode=browse").text
        # The Created button shows its ascending (↑) arrow.
        assert "↑" in _sort_button_label(html, "created")

    def test_sort_control_renders_before_filter_chips(self, tmp_path, monkeypatch):
        # #129: the sort control leads the filter row — its first button comes
        # before any filter chip toggle (the dated issues yield Pri/Repo chips).
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        html = client.get("/issues/container?mode=browse").text
        assert html.index("/issues/sort") < html.index("/issues/filters/toggle")

    def test_sort_survives_page_reload(self, authed_client):
        client, app, tm = authed_client
        _set_state(
            client,
            app,
            tm,
            DispatchState(sort="created", selected=["ben-ralston/agent-hq#67"]),
        )
        client.get("/issues?mode=browse")  # hard reload clears selection, keeps prefs
        state = _read_state(client, tm)
        assert state.sort == "created"
        assert state.selected == []

    def test_age_field_shown_in_date_mode(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="created"))
        r = client.get("/issues/list?mode=browse")
        assert "issue-meta__age" in r.text

    def test_age_field_hidden_in_default_mode(self, tmp_path, monkeypatch):
        client, app, tm = _make_authed_client(self._dated_issues(), tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(sort="default"))
        r = client.get("/issues/list?mode=browse")
        assert "issue-meta__age" not in r.text


class TestStaleFilterPruning:
    """#80: a cookie filter whose chip vanished (repo removed from config,
    last matching issue closed/changed) must be pruned on render instead
    of silently filtering the list down to an unexplained empty state."""

    def test_stale_repo_filter_is_pruned_on_container_render(self, authed_client):
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(repos=["gone/removed-repo"]))
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        # The list renders unfiltered rather than empty…
        assert "#67" in r.text
        assert "#40" in r.text
        assert "#44" in r.text
        # …and the pruned state is persisted back to the cookie.
        assert _read_state(client, tm).repos == []

    def test_stale_status_filter_is_pruned_on_list_render(self, authed_client):
        # Every loaded issue is Todo — no "in progress" chip exists anymore.
        client, app, tm = authed_client
        _set_state(client, app, tm, DispatchState(statuses=["in progress"]))
        r = client.get("/issues/list?mode=browse")
        assert r.status_code == 200
        assert "#67" in r.text
        assert _read_state(client, tm).statuses == []

    def test_stale_effort_filter_is_pruned_on_toggle(self, authed_client):
        """The audit-note trigger: the last issue with a given effort
        disappears while the chip is active. Any later interaction (here a
        repo-chip toggle) must shed the dead value from the cookie."""
        client, app, tm = authed_client
        # No issue carries effort L in the fixture set (all untriaged).
        _set_state(client, app, tm, DispatchState(efforts=["l"]))
        r = client.post(
            f"/issues/filters/toggle?mode=browse&type=repo&value={_quote('ben-ralston/agent-hq')}"
        )
        assert r.status_code == 200
        state = _read_state(client, tm)
        assert state.efforts == []
        assert state.repos == ["ben-ralston/agent-hq"]

    def test_valid_filters_survive_pruning(self, authed_client):
        client, app, tm = authed_client
        _triage(app, 67, effort="S")
        _set_state(
            client,
            app,
            tm,
            DispatchState(efforts=["s"], repos=["ben-ralston/agent-hq"]),
        )
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        assert "#67" in r.text
        assert "#40" not in r.text
        state = _read_state(client, tm)
        assert state.efforts == ["s"]
        assert state.repos == ["ben-ralston/agent-hq"]

    def test_transient_empty_issue_list_does_not_wipe_filters(self, tmp_path, monkeypatch):
        """A gh failure returns [] — an empty option set must not read as
        'every chip vanished' and wipe the user's filters (cf. #118's
        transient-empty lesson)."""
        client, app, tm = _make_authed_client([], tmp_path, monkeypatch)
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/agent-hq"]))
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        assert _read_state(client, tm).repos == ["ben-ralston/agent-hq"]


class TestMultiOwnerProjectRouting:
    """#81: GitHub Project numbers are owner-scoped, so two configured
    projects can share a number across owners. Status writes must go to
    the owner the issue was actually fetched under — not the first config
    entry with a matching number. (Priority writes left with the GH
    picker, #247.)"""

    def _client_with_collision(self, tmp_path, monkeypatch, issue):
        cache = _StubCache([issue])
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        cfg = Config(
            repos={"org-b/tool": RepoConfig("org-b/tool", Path("/tmp/x"))},
            projects=[
                ProjectConfig(name="A", number=5, owner="org-a"),
                ProjectConfig(name="B", number=5, owner="org-b"),
            ],
            worktree_root=Path("/tmp/wt"),
            dispatch=DispatchConfig(),
        )
        app = create_app(token_file=tm.token_file, config=cfg, github_cache=cache)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        return client, cache

    def test_status_write_uses_issues_own_project_owner(self, tmp_path, monkeypatch):
        issue = Issue(
            number=7,
            title="collision",
            repo="org-b/tool",
            status="Todo",
            priority=Priority.NEED,
            project_number=5,
            project_owner="org-b",
        )
        client, cache = self._client_with_collision(tmp_path, monkeypatch, issue)
        captured: list[str] = []
        cache.set_status = lambda issue, owner, label: captured.append(owner) or True
        r = client.post(
            f"/issues/issue/{_quote('org-b/tool#7')}/status", data={"value": "in-progress"}
        )
        assert r.status_code == 200
        assert captured == ["org-b"]

    def test_falls_back_to_config_number_match_for_pre81_cache(self, tmp_path, monkeypatch):
        """Issues loaded from a disk cache written before #81 carry no
        ``project_owner`` — those keep the old number-match resolution so
        a stale cache degrades to today's behavior, not worse."""
        issue = Issue(
            number=7,
            title="old cache entry",
            repo="org-b/tool",
            status="Todo",
            priority=Priority.NEED,
            project_number=5,
        )
        client, cache = self._client_with_collision(tmp_path, monkeypatch, issue)
        captured: list[str] = []
        cache.set_status = lambda issue, owner, label: captured.append(owner) or True
        r = client.post(
            f"/issues/issue/{_quote('org-b/tool#7')}/status", data={"value": "in-progress"}
        )
        assert r.status_code == 200
        assert captured == ["org-a"]  # first number match — documented fallback


_CODEX_CAPS = LauncherCapabilities(agents=frozenset({"claude", "codex"}), flags={})


def _make_client_with_probe(
    tmp_path,
    monkeypatch,
    *,
    caps: LauncherCapabilities = CLAUDE_ONLY,
    config: Config | None = None,
):
    """Authed client with an injected ``capabilities_probe`` stub so these
    tests never shell out to the real launch.sh (#205 Stage 1B Task 15)."""
    issues = [
        _mk_issue(67, title="dispatch view", priority=Priority.NEED),
        _mk_issue(40, title="filter bar", priority=Priority.WANT),
    ]
    cache = _StubCache(issues)
    from agent_hq.web.routes import issues as issues_routes

    monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(
        token_file=tm.token_file,
        config=config or _mk_config(),
        github_cache=cache,
        capabilities_probe=lambda: caps,
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, app, tm


def _make_client_with_probe_fn(tmp_path, monkeypatch, probe_fn, *, config: Config | None = None):
    """Like ``_make_client_with_probe`` but takes a raw probe callable
    (rather than a fixed caps value) so tests can observe how/where it's
    invoked."""
    issues = [
        _mk_issue(67, title="dispatch view", priority=Priority.NEED),
        _mk_issue(40, title="filter bar", priority=Priority.WANT),
    ]
    cache = _StubCache(issues)
    from agent_hq.web.routes import issues as issues_routes

    monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(
        token_file=tm.token_file,
        config=config or _mk_config(),
        github_cache=cache,
        capabilities_probe=probe_fn,
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, app, tm


class TestHarnessPicker:
    """#205 Stage 1B Task 15: harness/model/effort pickers on the web
    dispatch form. ``capabilities_probe`` is injected via ``create_app`` so
    these tests never shell out to the real launch.sh."""

    def test_probe_runs_off_the_event_loop_on_get(self, tmp_path, monkeypatch):
        """Fix 7 (multi-review wave): every GET form-context render must run
        the probe via run_in_threadpool, not synchronously on the event
        loop -- a hung/slow launch.sh must not stall the whole server. A
        probe called with no running event loop in its own thread proves it
        executed off-loop."""
        import asyncio

        saw_running_loop = []

        def probe():
            try:
                asyncio.get_running_loop()
                saw_running_loop.append(True)
            except RuntimeError:
                saw_running_loop.append(False)
            return CLAUDE_ONLY

        client, _, _ = _make_client_with_probe_fn(tmp_path, monkeypatch, probe)
        for path in ("/issues", "/issues/container?mode=browse", "/issues/list?mode=browse"):
            r = client.get(path)
            assert r.status_code == 200
        assert saw_running_loop and all(v is False for v in saw_running_loop)

    def test_probe_runs_off_the_event_loop_on_dispatch_post(self, tmp_path, monkeypatch):
        import asyncio

        saw_running_loop = []

        def probe():
            try:
                asyncio.get_running_loop()
                saw_running_loop.append(True)
            except RuntimeError:
                saw_running_loop.append(False)
            return CLAUDE_ONLY

        client, app, tm = _make_client_with_probe_fn(tmp_path, monkeypatch, probe)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        from agent_hq.dispatch import DispatchResult
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(
            issues_routes,
            "dispatch_via_claude",
            lambda batches, config, **kwargs: DispatchResult(success=True, branch="x"),
        )
        r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        assert saw_running_loop and all(v is False for v in saw_running_loop)

    def test_dispatch_page_hides_codex_when_unadvertised(self, tmp_path, monkeypatch):
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        r = client.get("/issues?mode=browse")
        assert 'value="codex"' not in r.text

    def test_dispatch_page_shows_codex_when_advertised(self, tmp_path, monkeypatch):
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=_CODEX_CAPS)
        r = client.get("/issues?mode=browse")
        assert 'value="codex"' in r.text

    def test_dispatch_form_threads_harness(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=_CODEX_CAPS)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(kwargs)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse", data={"harness": "codex", "effort": "high"})
        assert r.status_code == 200
        assert len(captured) == 1
        assert captured[0]["harness"] == "codex"
        assert captured[0]["effort"] == "high"

    def test_dispatch_rejects_unadvertised_codex(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(kwargs)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse", data={"harness": "codex"})
        assert r.status_code == 400
        assert captured == []

    def test_dispatch_rejects_invalid_harness_value(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))
        r = client.post("/issues/dispatch?mode=browse", data={"harness": "bogus"})
        assert r.status_code == 400

    def test_dispatch_rejects_invalid_effort_value(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))
        r = client.post("/issues/dispatch?mode=browse", data={"effort": "bogus"})
        assert r.status_code == 400

    def test_dispatch_rejects_model_with_whitespace(self, tmp_path, monkeypatch):
        """A model value with embedded whitespace (e.g. "sonnet 999 --effort
        max") would splice phantom argv into /worktree-create's argument
        parsing -- rejected at the shared resolution boundary before
        anything is scheduled."""
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(kwargs)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse", data={"model": "a b"})
        assert r.status_code == 400
        assert captured == []

    def test_dispatch_form_threads_model(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(kwargs)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse", data={"model": "gpt-5-codex"})
        assert r.status_code == 200
        assert captured[0]["model"] == "gpt-5-codex"

    def test_blank_form_fields_forward_none(self, tmp_path, monkeypatch):
        client, app, tm = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))

        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(kwargs)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        # capabilities_probe (final-review fix, #205 Stage 1B) is also
        # forwarded -- checked separately below, not via full-dict equality,
        # so this assertion doesn't couple to that implementation detail.
        assert captured[0]["harness"] is None
        assert captured[0]["model"] is None
        assert captured[0]["effort"] is None
        assert captured[0]["capabilities_probe"] is app.state.capabilities_probe

    def test_dispatch_page_preselects_config_default_harness(self, tmp_path, monkeypatch):
        cfg = _mk_config()
        cfg.dispatch = DispatchConfig(default_harness="codex")
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=_CODEX_CAPS, config=cfg)
        r = client.get("/issues?mode=browse")
        idx = r.text.find('value="codex"')
        assert idx != -1
        assert "selected" in r.text[idx : idx + 40]

    def test_picker_controls_carry_id_and_hx_preserve(self, tmp_path, monkeypatch):
        """#205 Stage 1B Task 15 review (Important): every /issues/select,
        /group, /sort, etc. swap replaces the whole #dispatch-container —
        without a stable id + hx-preserve (mirroring the search input in
        dispatch_filter_bar.html, #144) the picker silently resets to
        defaults on the very next unrelated interaction."""
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=_CODEX_CAPS)
        r = client.get("/issues/container?mode=browse")
        for control_id in ("dispatch-harness", "dispatch-effort", "dispatch-model"):
            idx = r.text.find(f'id="{control_id}"')
            assert idx != -1, f"missing id={control_id!r}"
            # hx-preserve lives on the same tag — look in a small window
            # around the id rather than requiring exact attribute order.
            assert 'hx-preserve="true"' in r.text[max(0, idx - 100) : idx + 200]

    def test_effort_options_come_from_effort_order(self, tmp_path, monkeypatch):
        """#205 Stage 1B Task 15 review (Minor 2): the effort <select> must
        loop over EFFORT_ORDER, not a hardcoded tuple, so it can't drift
        from VALID_EFFORTS."""
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        r = client.get("/issues/container?mode=browse")
        from agent_hq.config import EFFORT_ORDER

        idx = r.text.find('id="dispatch-effort"')
        assert idx != -1
        select_html = r.text[idx : idx + 800]
        for effort in EFFORT_ORDER:
            assert f'value="{effort}"' in select_html

    def test_effort_and_model_show_config_default_as_placeholder_only(self, tmp_path, monkeypatch):
        """Multi-review fix wave, Fix 5: the effort select and model input
        must NOT pre-fill as explicit form values from
        config.dispatch.harnesses[default_harness] -- an explicit value
        becomes an override that survives a harness switch, fighting the
        server-side per-harness default resolution. The blank "effort
        (default)" option stays selected, the model input stays empty, and
        the configured default is surfaced only via placeholder text."""
        cfg = _mk_config()
        cfg.dispatch = DispatchConfig(
            default_harness="codex",
            harnesses={"codex": HarnessDefaults(model="gpt-5-codex", effort="high")},
        )
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=_CODEX_CAPS, config=cfg)
        r = client.get("/issues/container?mode=browse")
        effort_idx = r.text.find('id="dispatch-effort"')
        assert effort_idx != -1
        effort_html = r.text[effort_idx : effort_idx + 800]
        # No effort option carries "selected" -- the blank default stays chosen.
        assert "selected" not in effort_html
        assert 'id="dispatch-model"' in r.text
        model_idx = r.text.find('id="dispatch-model"')
        model_html = r.text[model_idx : model_idx + 300]
        assert 'value=""' in model_html
        # Short form (#205 polish): the long "model (default: X)" wording
        # overflowed the input at a 390px viewport and rendered clipped.
        assert 'placeholder="model: gpt-5-codex"' in model_html

    def test_model_placeholder_generic_when_no_config_default(self, tmp_path, monkeypatch):
        """When the configured harness has no model set, the placeholder
        falls back to the bare field name "model" (Fix 5)."""
        client, _, _ = _make_client_with_probe(tmp_path, monkeypatch, caps=CLAUDE_ONLY)
        r = client.get("/issues/container?mode=browse")
        model_idx = r.text.find('id="dispatch-model"')
        assert model_idx != -1
        model_html = r.text[model_idx : model_idx + 300]
        assert 'value=""' in model_html
        assert 'placeholder="model"' in model_html


def _find(
    app,
    ftype=FindingType.DUPLICATE,
    keys=("ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"),
    *,
    action=None,
    reasoning="looks the same",
    now="2026-09-12T10:00:00+00:00",
    open_keys=None,
):
    """Record one open hygiene finding via the app's store (#249). By default
    every issue already referenced by a stored finding stays "open", so a
    second call never resolves the first finding."""
    keys = list(keys)
    if action is None:
        action = {"kind": "close_as_duplicate", "close": keys[1], "of": keys[0]}
    f = Finding.new(ftype, keys, action=action, reasoning=reasoning)
    if open_keys is None:
        open_keys = set(keys)
        for existing in app.state.hygiene_store.all_findings():
            open_keys |= set(existing.issue_keys)
    app.state.hygiene_store.record_sweep(
        [f], now=now, sweep_id="s", model="opus", open_keys=set(open_keys)
    )
    return f.id


class TestTriageMode:
    """#248 Triage inbox mode: segmented-control badge, proposal cards, bar."""

    def test_mode_switch_has_triage_segment_with_badge_count(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        _propose(app, 40)
        r = client.get("/issues?mode=triage")
        assert 'href="/issues?mode=triage&keep=1"' in r.text
        assert 'id="triage-badge"' in r.text
        assert ">2</span>" in r.text
        assert re.search(r'mode-switch__btn--active"\s+href="/issues\?mode=triage', r.text)
        assert app.state.ui_prefs_store.get_web_issues_mode() == "triage"

    def test_badge_hidden_at_zero(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/triage/badge")
        assert 'id="triage-badge"' in r.text
        assert "mode-switch__count--hidden" in r.text

    def test_container_lists_proposed_cards_newest_first_with_reasoning(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67, proposed_at="2026-09-01T00:00:00+00:00")
        _propose(app, 40, proposed_at="2026-09-02T00:00:00+00:00")
        _triage(app, 44)  # confirmed → not in the inbox
        r = client.get("/issues/container?mode=triage")
        assert r.status_code == 200
        assert 'data-mode="triage"' in r.text
        # #40 ("filter bar") is the newer proposal, so it sorts first. Assert
        # both cards are actually present before comparing — a bare
        # ``find(a) < find(b)`` passes vacuously when a is missing (-1 < N).
        newer = r.text.find("filter bar")
        older = r.text.find("dispatch view")
        assert newer != -1, "newer proposal (#40) card missing"
        assert older != -1, "older proposal (#67) card missing"
        assert newer < older
        assert 'data-issue-key="ben-ralston/agent-hq#44"' not in r.text
        for token in (
            "why-v-67",
            "why-e-67",
            "why-f-67",
            "picker-option--active",
            "Confirm all",
            "Run now",
            # This fixture has triage=None, so the worker is unavailable and
            # Run-now renders disabled. The reason must be VISIBLE text, not
            # just a title= tooltip: no hover on a phone, and a disabled
            # button isn't focusable.
            "AI triage off",
            'aria-describedby="triage-off-hint"',
        ):
            assert token in r.text, token
        # Jinja's ``urlencode`` leaves "/" alone (matches every other
        # rendered issue-key URL in this app, cf. the select/pin buttons).
        assert "/issues/triage/ben-ralston/agent-hq%2367/confirm?mode=triage" in r.text
        assert "/issues/triage/ben-ralston/agent-hq%2367/adjust" in r.text
        # No select checkbox on triage cards — the inbox isn't a dispatch list.
        assert "issue-check" not in r.text.split('class="triage-card"', 1)[-1]

    def test_confirm_all_button_carries_the_visible_keys(self, authed_client):
        """Review fix 2: Confirm-all posts exactly the keys it rendered, so a
        proposal that lands between paint and tap isn't confirmed unseen."""
        client, app, _ = authed_client
        _propose(app, 67, proposed_at="2026-09-02T00:00:00+00:00")
        _propose(app, 40, proposed_at="2026-09-01T00:00:00+00:00")
        r = client.get("/issues/container?mode=triage")
        vals = _confirm_all_vals(r.text)
        assert vals["keys"] == "ben-ralston/agent-hq#67,ben-ralston/agent-hq#40"
        # Codex P2: each key's proposal revision rides along too, so the POST
        # can refuse a proposal the worker replaced after this paint.
        assert json.loads(vals["revisions"]) == {
            "ben-ralston/agent-hq#67": "2026-09-02T00:00:00+00:00",
            "ben-ralston/agent-hq#40": "2026-09-01T00:00:00+00:00",
        }

    def test_sentinel_proposal_card_keeps_the_sentinel_class(self, tmp_path, monkeypatch):
        """A machine-filed proposal must still read as machine-filed in the
        inbox. Rendering only — the matching CSS (``.triage-card`` sets the
        ``border`` shorthand, so the stripe is restated at the combined
        ``.triage-card.issue-card--sentinel`` specificity) isn't testable here.
        """
        issues = [_mk_issue(9, title="sentinel filed", labels=["sentinel"])]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        _propose(app, 9)
        r = client.get("/issues/container?mode=triage")
        assert 'class="triage-card issue-card--sentinel"' in r.text
        assert "sentinel filed" in r.text

    def test_repo_chip_filters_inbox(self, tmp_path, monkeypatch):
        issues = [_mk_issue(1), _mk_issue(2, repo="o/other")]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _propose(app, 1)
        _propose(app, 2, repo="o/other")
        _set_state(client, app, tm, DispatchState(repos=["o/other"]))
        r = client.get("/issues/container?mode=triage")
        assert "issue 2" in r.text
        assert "issue 1" not in r.text

    def test_triage_bar_has_repo_src_status_with_actions_first(self, tmp_path, monkeypatch):
        """Triage shares Queue's chips minus Eff/Fit (those would filter on the
        very dimensions still awaiting confirmation); Confirm all / Run now
        lead the row so they stay on-screen at phone width."""
        issues = [_mk_issue(1, labels=["sentinel"]), _mk_issue(2)]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        _propose(app, 1)
        _propose(app, 2)
        r = client.get("/issues/container?mode=triage")
        assert _label_tag_order(r.text) == ["Repo", "Src", "Status"]
        assert "type=effort" not in r.text and "type=fitness" not in r.text
        bar = r.text.split('class="filter-bar__chips"', 1)[1]
        assert bar.find("Confirm all") < bar.find('filter-chip__label-tag">Repo')

    def test_status_and_src_chips_filter_inbox_and_confirm_all(self, tmp_path, monkeypatch):
        issues = [_mk_issue(1, labels=["sentinel"]), _mk_issue(2, status="In Progress")]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _propose(app, 1)
        _propose(app, 2)
        _set_state(client, app, tm, DispatchState(statuses=["in progress"]))
        r = client.get("/issues/container?mode=triage")
        assert "issue 2" in r.text and "issue 1" not in r.text
        assert "Confirm all (1)" in r.text
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#1,ben-ralston/agent-hq#2"},
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#2").state is TriageState.CONFIRMED
        assert app.state.triage_store.get("ben-ralston/agent-hq#1").state is TriageState.PROPOSED
        _reset_state(client, app, tm, DispatchState(sentinels=["sentinel"]))
        r = client.get("/issues/container?mode=triage")
        assert "issue 1" in r.text and "issue 2" not in r.text

    def test_empty_inbox_and_unconfigured_hints(self, authed_client, tmp_path, monkeypatch):
        client, _, _ = authed_client
        r = client.get("/issues/container?mode=triage")
        assert "Inbox empty" in r.text
        cache = _StubCache([_mk_issue(67)])
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "t2")
        tm.ensure()
        app2 = create_app(token_file=tm.token_file, config=_mk_config(), github_cache=cache)
        c2 = TestClient(app2)
        c2.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r2 = c2.get("/issues/container?mode=triage")
        assert "Triage store not configured" in r2.text

    def test_skeleton_renders_triage_branch(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues?mode=triage")
        assert 'hx-get="/issues/container?mode=triage"' in r.text

    def test_detail_sheet_shows_ai_reasoning_and_model(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.get("/issues/issue/ben-ralston%2Fagent-hq%2367")
        assert "why-v-67" in r.text and "why-f-67" in r.text and "Proposed by sonnet" in r.text

    def test_detail_sheet_hides_reasoning_and_model_once_confirmed(self, authed_client):
        """#308: the AI scaffolding is for deciding; once confirmed it's noise.
        Display-only — the record keeps its reasoning/model for the audit trail."""
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.post("/issues/issue/ben-ralston%2Fagent-hq%2367/confirm")
        assert r.status_code == 200
        assert '<span class="rank-chip">' in r.text  # the confirmed sheet really rendered
        assert "Confirm triage" not in r.text
        assert "triage-dim__reason" not in r.text
        assert "why-v-67" not in r.text and "why-e-67" not in r.text and "why-f-67" not in r.text
        assert "Proposed by" not in r.text
        r = client.get("/issues/issue/ben-ralston%2Fagent-hq%2367")
        assert "triage-dim__reason" not in r.text and "Proposed by" not in r.text
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.reasoning["value"] == "why-v-67" and rec.model == "sonnet"

    def test_queue_untriaged_row_shows_ai_proposed_pill(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.get("/issues/container?mode=queue")
        untriaged_at = r.text.find("Untriaged")
        pill_at = r.text.find("proposed-badge")
        assert untriaged_at != -1 and pill_at != -1
        assert untriaged_at < pill_at  # pill sits inside the Untriaged section


class TestTriageActions:
    """#248 Triage inbox writes: confirm, confirm-all, adjust, run now."""

    KEY = "ben-ralston%2Fagent-hq%2367"

    def test_confirm_flips_state_and_removes_card(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.post(f"/issues/triage/{self.KEY}/confirm?mode=triage")
        assert r.status_code == 200
        assert 'data-mode="triage"' in r.text
        assert "dispatch view" not in r.text
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.CONFIRMED
        assert rec.provenance["value"] == "ai"

    def test_confirm_incomplete_record_is_400(self, authed_client):
        client, app, _ = authed_client
        app.state.triage_store.upsert(
            TriageRecord(issue_key="ben-ralston/agent-hq#67", value=3, updated_at="t"),
            actor="ai",
        )
        r = client.post(f"/issues/triage/{self.KEY}/confirm?mode=triage")
        assert r.status_code == 400

    def test_adjust_sets_human_dim_and_keeps_card_in_inbox(self, authed_client):
        """Tapping a chip corrects one dimension; the card stays in the inbox
        (state unchanged) until Confirm, so the other chips can be reviewed."""
        client, app, _ = authed_client
        _propose(app, 67, value=3)
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "value", "value": "5"},
        )
        assert r.status_code == 200
        assert "dispatch view" in r.text
        card = r.text.split('data-issue-key="ben-ralston/agent-hq#67"', 1)[1]
        assert re.search(r'picker-option--active"[^>]*data-picker="value"[^>]*data-value="5"', card)
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.value == 5
        assert rec.state is TriageState.PROPOSED
        assert rec.provenance == {"value": "human", "effort": "ai", "agent_fitness": "ai"}
        # AI reasoning for untouched dims survives the human write.
        assert rec.reasoning["effort"] == "why-e-67"

    def test_adjust_rejects_bad_dims_and_values(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        bad = client.post(f"/issues/triage/{self.KEY}/adjust", data={"dim": "value", "value": "9"})
        assert bad.status_code == 400
        unknown = client.post(
            f"/issues/triage/{self.KEY}/adjust", data={"dim": "status", "value": "done"}
        )
        assert unknown.status_code == 400
        flag_bad = client.post(
            f"/issues/triage/{self.KEY}/adjust", data={"dim": "pin", "value": "yes"}
        )
        assert flag_bad.status_code == 400

    # ---- #267: interest + pin from the inbox row ----

    def test_card_renders_interest_and_pin_toggles_with_target_values(self, authed_client):
        """#267: the inbox row carries the two flags next to the dimension
        chips, tap-to-set / tap-again-to-clear — each button posts the
        value that flips the current state, like the detail sheet's."""
        client, app, _ = authed_client
        _propose(app, 67, proposed_at=self.OLD)
        r = client.get("/issues/container?mode=triage")
        card = r.text.split('data-issue-key="ben-ralston/agent-hq#67"', 1)[1].split("</article>")[0]
        for flag in ("interest", "pin"):
            btn = re.search(rf'<button[^>]*data-picker="{flag}"[^>]*>', card)
            assert btn, flag
            assert 'data-value="true"' in btn.group(0)
            assert "picker-option--active" not in btn.group(0)
            assert "/issues/triage/ben-ralston/agent-hq%2367/adjust" in btn.group(0)
            assert f'"dim": "{flag}", "value": "true", "proposed_at": "{self.OLD}"' in btn.group(0)
        # Set both; the re-rendered card shows them active and offers the clear.
        client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "interest", "value": "true", "proposed_at": self.OLD},
        )
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "pin", "value": "true", "proposed_at": self.OLD},
        )
        card = r.text.split('data-issue-key="ben-ralston/agent-hq#67"', 1)[1].split("</article>")[0]
        for flag in ("interest", "pin"):
            btn = re.search(rf'<button[^>]*data-picker="{flag}"[^>]*>', card)
            assert btn and "picker-option--active" in btn.group(0), flag
            assert 'data-value="false"' in btn.group(0)

    def test_adjust_flag_stages_it_and_keeps_card_in_inbox(self, authed_client):
        """#267: a flag tap is a human write that leaves the record PROPOSED
        (the card stays until Confirm) and never touches the AI dims."""
        client, app, _ = authed_client
        _propose(app, 67, value=3)
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "interest", "value": "true"},
        )
        assert r.status_code == 200
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.interest is True and rec.pin is None
        assert rec.state is TriageState.PROPOSED
        assert rec.value == 3
        assert rec.provenance == {"value": "ai", "effort": "ai", "agent_fitness": "ai"}
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "pin", "value": "true"},
        )
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        # Pin is STAGED (pin_staged), not committed (pin) — Confirm folds it.
        assert rec.pin_staged is True and rec.pin is None and rec.interest is True
        assert rec.state is TriageState.PROPOSED
        # Tap again to clear.
        client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "pin", "value": "false"},
        )
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.pin_staged is False and rec.pin is None
        # Set-then-clear leaves the effective pin unchanged, so the card
        # must not promise an "Unpin ... on Confirm" (built-in review r3).
        card = client.get("/issues/container?mode=triage").text
        card = card.split('data-issue-key="ben-ralston/agent-hq#67"', 1)[1].split("</article>")[0]
        assert "takes effect in Queue on Confirm" not in card

    def test_staged_pin_reaches_queue_pinned_strip_only_after_confirm(self, authed_client):
        """#267 acceptance: a pinned-but-unconfirmed inbox row is absent from
        the Queue's pinned strip; the inline Confirm commits it."""
        client, app, _ = authed_client
        _propose(app, 67)
        client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage", data={"dim": "pin", "value": "true"}
        )
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.pin_staged is True and rec.pin is None
        queue = client.get("/issues/container?mode=queue")
        assert "queue-pinned" not in queue.text
        # The detail sheet (canonical view) shows the pin as not yet
        # effective, with a hint that it lands on Confirm.
        sheet = client.get(f"/issues/issue/{self.KEY}")
        assert "Pin staged from the Triage inbox" in sheet.text
        r = client.post(f"/issues/triage/{self.KEY}/confirm?mode=triage")
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.CONFIRMED and rec.pin is True and rec.pin_staged is None
        sheet = client.get(f"/issues/issue/{self.KEY}")
        assert "Pin staged from the Triage inbox" not in sheet.text
        queue = client.get("/issues/container?mode=queue")
        assert "queue-pinned" in queue.text
        strip = queue.text.split('class="queue-pinned"', 1)[1].split("</section>")[0]
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in strip

    def test_confirm_all_commits_flags_staged_per_row(self, authed_client):
        """#267 acceptance: Confirm-all respects the interest/pin staged on
        each row — #67 was pinned + starred, #40 untouched."""
        client, app, _ = authed_client
        _propose(app, 67)
        _propose(app, 40)
        for dim in ("pin", "interest"):
            client.post(
                f"/issues/triage/{self.KEY}/adjust?mode=triage", data={"dim": dim, "value": "true"}
            )
        queue = client.get("/issues/container?mode=queue")
        assert "queue-pinned" not in queue.text
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#67,ben-ralston/agent-hq#40"},
        )
        assert r.status_code == 200
        rec67 = app.state.triage_store.get("ben-ralston/agent-hq#67")
        rec40 = app.state.triage_store.get("ben-ralston/agent-hq#40")
        assert rec67.state is TriageState.CONFIRMED and rec67.pin is True
        assert rec67.pin_staged is None and rec67.interest is True
        assert rec40.state is TriageState.CONFIRMED and not rec40.pin and not rec40.interest
        queue = client.get("/issues/container?mode=queue")
        strip = queue.text.split('class="queue-pinned"', 1)[1].split("</section>")[0]
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in strip
        assert 'data-issue-key="ben-ralston/agent-hq#40"' not in strip

    def test_inbox_pin_tap_on_a_record_confirmed_meanwhile_pins_directly(self, authed_client):
        """Built-in review of #267: the card was painted PROPOSED, then the
        record was confirmed (Confirm-all / another tab) before the pin tap
        landed. ``_proposal_is_stale`` only catches a still-PROPOSED record
        with a new revision, so the write goes through — it must be a direct
        ``pin`` write, never an orphaned ``pin_staged`` that no Confirm will
        ever fold (confirm_many skips CONFIRMED records)."""
        client, app, _ = authed_client
        _propose(app, 67, proposed_at=self.OLD)
        app.state.triage_store.confirm("ben-ralston/agent-hq#67", now="n")
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "pin", "value": "true", "proposed_at": self.OLD},
        )
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.CONFIRMED
        assert rec.pin is True and rec.pin_staged is None
        queue = client.get("/issues/container?mode=queue")
        strip = queue.text.split('class="queue-pinned"', 1)[1].split("</section>")[0]
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in strip

    def test_queue_pin_on_proposed_row_still_floats_immediately(self, authed_client):
        """Codex review of #267: the Queue's inline pin (and the sheet's) is a
        direct ``pin`` write — it floats an AI-proposed row at once, exactly
        as before the inbox learned to stage one; the inbox card then reads
        the effective pin back as set."""
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.post(f"/issues/issue/{self.KEY}/pin?on=true&mode=queue")
        assert r.status_code == 200
        strip = r.text.split('class="queue-pinned"', 1)[1].split("</section>")[0]
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in strip
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.pin is True and rec.pin_staged is None and rec.state is TriageState.PROPOSED
        inbox = client.get("/issues/container?mode=triage")
        card = inbox.text.split('data-issue-key="ben-ralston/agent-hq#67"', 1)[1]
        card = card.split("</article>")[0]
        btn = re.search(r'<button[^>]*data-picker="pin"[^>]*>', card).group(0)
        assert "picker-option--active" in btn and 'data-value="false"' in btn
        assert "Pin takes effect in Queue on Confirm" not in card  # nothing staged

    def test_confirm_all_respects_repo_filter(self, tmp_path, monkeypatch):
        issues = [_mk_issue(1), _mk_issue(2, repo="o/other")]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _propose(app, 1)
        _propose(app, 2, repo="o/other")
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/agent-hq"]))
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#1,o/other#2"},
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#1").state is TriageState.CONFIRMED
        assert app.state.triage_store.get("o/other#2").state is TriageState.PROPOSED

    def test_confirm_all_only_confirms_the_posted_keys(self, authed_client):
        """Review fix 2: the button carries the keys of the cards the user
        actually saw. A proposal that appeared between render and tap (the
        worker runs behind an open tab) must not be swept up by a blind
        "confirm everything currently proposed"."""
        client, app, _ = authed_client
        _propose(app, 67)
        _propose(app, 40)
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#67"},
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.CONFIRMED
        assert app.state.triage_store.get("ben-ralston/agent-hq#40").state is TriageState.PROPOSED

    def test_confirm_all_ignores_keys_no_longer_proposed(self, authed_client):
        """A posted key whose record has since been confirmed (or was never
        proposed) intersects out — it is skipped, not an error."""
        client, app, _ = authed_client
        _propose(app, 67)
        _triage(app, 40)  # already CONFIRMED by a human edit
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#40,ben-ralston/agent-hq#44"},
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.PROPOSED
        assert app.state.triage_store.get("ben-ralston/agent-hq#40").state is TriageState.CONFIRMED

    def test_confirm_all_without_keys_falls_back_to_every_proposal(self, authed_client):
        """Legacy markup (a tab open across the deploy) posts no ``keys``;
        the old confirm-everything-visible behaviour must still work."""
        client, app, _ = authed_client
        _propose(app, 67)
        _propose(app, 40)
        r = client.post("/issues/triage/confirm-all?mode=triage")
        assert r.status_code == 200
        for number in (67, 40):
            rec = app.state.triage_store.get(f"ben-ralston/agent-hq#{number}")
            assert rec.state is TriageState.CONFIRMED

    OLD = "2026-09-01T00:00:00+00:00"
    NEW = "2026-09-02T00:00:00+00:00"

    def test_confirm_card_carries_the_proposal_revision(self, authed_client):
        """Codex P2: the card's Confirm button and its chips post the
        ``proposed_at`` they were rendered with."""
        client, app, _ = authed_client
        _propose(app, 67, proposed_at=self.OLD)
        r = client.get("/issues/container?mode=triage")
        assert f'hx-vals=\'{{"proposed_at": "{self.OLD}"}}\'' in r.text
        assert f'"value": "5", "proposed_at": "{self.OLD}"' in r.text

    def test_confirm_with_stale_revision_keeps_the_card(self, authed_client):
        """The worker re-proposed #67 (its body changed) after the card was
        painted. Confirming the old revision must not sign off on the new
        dimensions: nothing is written and the fresh card is re-rendered."""
        client, app, _ = authed_client
        _propose(app, 67, value=4, proposed_at=self.OLD)
        _propose(app, 67, value=2, proposed_at=self.NEW)  # replacement lands
        r = client.post(
            f"/issues/triage/{self.KEY}/confirm?mode=triage", data={"proposed_at": self.OLD}
        )
        assert r.status_code == 200
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text  # still in the inbox
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.PROPOSED and rec.value == 2
        r = client.post(
            f"/issues/triage/{self.KEY}/confirm?mode=triage", data={"proposed_at": self.NEW}
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.CONFIRMED

    def test_adjust_with_stale_revision_writes_nothing(self, authed_client):
        """A chip tap on a stale card would auto-confirm the two unseen AI
        dimensions alongside the human one — refused the same way."""
        client, app, _ = authed_client
        _propose(app, 67, value=4, proposed_at=self.OLD)
        _propose(app, 67, value=2, effort="S", proposed_at=self.NEW)
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "value", "value": "5", "proposed_at": self.OLD},
        )
        assert r.status_code == 200
        assert 'data-issue-key="ben-ralston/agent-hq#67"' in r.text
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.state is TriageState.PROPOSED and rec.value == 2
        assert rec.provenance == {"value": "ai", "effort": "ai", "agent_fitness": "ai"}

    def test_confirm_all_skips_stale_revisions(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67, proposed_at=self.OLD)
        _propose(app, 40, proposed_at=self.OLD)
        _propose(app, 67, value=1, proposed_at=self.NEW)  # replaced after paint
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={
                "keys": "ben-ralston/agent-hq#67,ben-ralston/agent-hq#40",
                "revisions": json.dumps(
                    {"ben-ralston/agent-hq#67": self.OLD, "ben-ralston/agent-hq#40": self.OLD}
                ),
            },
        )
        assert r.status_code == 200
        assert app.state.triage_store.get("ben-ralston/agent-hq#40").state is TriageState.CONFIRMED
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.PROPOSED

    def test_confirm_all_rejects_malformed_revisions(self, authed_client):
        client, app, _ = authed_client
        _propose(app, 67)
        r = client.post(
            "/issues/triage/confirm-all?mode=triage",
            data={"keys": "ben-ralston/agent-hq#67", "revisions": "[not an object]"},
        )
        assert r.status_code == 400
        assert app.state.triage_store.get("ben-ralston/agent-hq#67").state is TriageState.PROPOSED

    def test_confirm_publishes_invalidation(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        _propose(app, 67)
        published = []

        async def fake_publish(event):
            published.append(event.topic)
            return True

        monkeypatch.setattr(app.state.broker, "publish", fake_publish)
        client.post(f"/issues/triage/{self.KEY}/confirm?mode=triage")
        assert "issue.invalidated" in published

    def test_run_now_kicks_worker(self, tmp_path, monkeypatch):
        """Only the route may fire the kick.

        #67 is CONFIRMED first, so ``select_targets`` is empty and
        ``_container_response``'s own ``_maybe_kick_triage_worker`` returns
        the in-flight flag without kicking — deleting the route's ``kick``
        line therefore fails this test instead of passing on the render
        path's kick.
        """
        from agent_hq.triage_worker import TriageRunReport
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(
            issues_routes, "run_triage", lambda *a, **k: TriageRunReport(repos=[], dry_run=False)
        )
        monkeypatch.setattr(issues_routes.shutil, "which", lambda n: "/x/claude")
        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(triage=TriageConfig(data_dir=tmp_path / "d")),
            github_cache=_StubCache([_mk_issue(67)]),
            triage_store=TriageStore(tmp_path / "d"),
        )
        _triage(app, 67)
        kicks = []

        def spy_kick(factory, **kwargs):
            kicks.append(kwargs)
            return True

        monkeypatch.setattr(app.state.triage_refresher, "kick", spy_kick)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.post("/issues/triage/run?mode=triage")
        assert r.status_code == 200
        assert 'data-mode="triage"' in r.text
        # force=True: a deliberate tap must run even inside the automatic
        # trigger's post-failure cooldown, never a silent no-op.
        assert kicks == [{"force": True}]

    def test_run_now_does_not_kick_when_ai_unavailable(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes.shutil, "which", lambda n: None)
        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(triage=TriageConfig(data_dir=tmp_path / "d")),
            github_cache=_StubCache([_mk_issue(67)]),
            triage_store=TriageStore(tmp_path / "d"),
        )
        kicks = []
        monkeypatch.setattr(app.state.triage_refresher, "kick", lambda f: kicks.append(f) or True)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.post("/issues/triage/run?mode=triage").status_code == 200
        assert kicks == []

    def test_adjust_on_untriaged_record_confirms(self, authed_client):
        """No AI proposal in play: the auto-seeded UNTRIAGED record is
        confirmed by the human write, same as from the detail editor."""
        client, app, _ = authed_client
        client.get("/issues/container?mode=queue")  # auto-seeds untriaged records
        seeded = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert seeded is not None
        assert seeded.state is TriageState.UNTRIAGED
        r = client.post(
            f"/issues/triage/{self.KEY}/adjust?mode=triage",
            data={"dim": "value", "value": "4"},
        )
        assert r.status_code == 200
        rec = app.state.triage_store.get("ben-ralston/agent-hq#67")
        assert rec.value == 4
        assert rec.state is TriageState.UNTRIAGED  # human edits never confirm
        assert rec.provenance["value"] == "human"

    def test_confirm_and_adjust_unknown_key_404(self, authed_client):
        client, _, _ = authed_client
        missing = "ben-ralston%2Fagent-hq%23999"
        assert client.post(f"/issues/triage/{missing}/confirm").status_code == 404
        adjust = client.post(
            f"/issues/triage/{missing}/adjust", data={"dim": "value", "value": "4"}
        )
        assert adjust.status_code == 404

    def test_actions_404_without_store(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file,
            config=_mk_config(),
            github_cache=_StubCache([_mk_issue(67)]),
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.post(f"/issues/triage/{self.KEY}/confirm").status_code == 404
        assert client.post("/issues/triage/confirm-all").status_code == 404
        assert client.post("/issues/triage/run").status_code == 404
        adjust = client.post(
            f"/issues/triage/{self.KEY}/adjust", data={"dim": "value", "value": "4"}
        )
        assert adjust.status_code == 404


# --- #254: GitHub-unreachable banner --------------------------------------


def _failed_status(**kwargs):
    from agent_hq.github import FetchStatus

    defaults = dict(
        ok=False,
        message="project @me/4: could not resolve host",
        rate_limited=False,
        reset_at=None,
        stale_since=time.time() - 7200,
    )
    defaults.update(kwargs)
    return FetchStatus(**defaults)


class TestGitHubUnreachableBanner:
    def test_banner_absent_when_healthy(self, authed_client):
        client, _app, _tm = authed_client
        body = client.get("/issues/container").text
        assert "GitHub unreachable" not in body

    def test_banner_shows_stale_age(self, authed_client):
        client, app, _tm = authed_client
        app.state.github_cache.status = _failed_status()
        body = client.get("/issues/container").text
        assert "GitHub unreachable" in body
        assert "2h ago" in body

    def test_banner_shows_rate_limit_reset(self, authed_client):
        client, app, _tm = authed_client
        app.state.github_cache.status = _failed_status(
            message="project @me/4: GitHub API rate limit exceeded",
            rate_limited=True,
            reset_at=time.time() + 1800,
        )
        body = client.get("/issues/container").text
        assert "rate limit" in body.lower()
        # Exact wording of the countdown is unit-tested in test_filters with
        # an injected clock; here only that the reset rides along.
        assert "resets in" in body

    def test_empty_backlog_says_unreachable_not_empty(self, tmp_path, monkeypatch):
        """The benign "waiting for the project to populate" copy must never
        stand in for "the API is down and we have nothing cached"."""
        client, app, _tm = _make_authed_client([], tmp_path, monkeypatch)
        app.state.github_cache.status = _failed_status(stale_since=None)
        body = client.get("/issues/container").text
        assert "waiting for the GitHub Project to populate" not in body
        assert "GitHub unreachable" in body

    def test_empty_backlog_keeps_benign_copy_when_healthy(self, tmp_path, monkeypatch):
        client, _app, _tm = _make_authed_client([], tmp_path, monkeypatch)
        body = client.get("/issues/container").text
        assert "waiting for the GitHub Project to populate" in body

    @pytest.mark.parametrize("mode", ["queue", "browse"])
    def test_banner_and_empty_copy_cover_both_modes(self, tmp_path, monkeypatch, mode):
        """The hub's two modes render different list partials (#247). A
        failed fetch must read the same in both: the container banner above
        the mode branch, and the unreachable copy standing in for the benign
        empty state in each list."""
        client, app, _tm = _make_authed_client([], tmp_path, monkeypatch)
        app.state.github_cache.status = _failed_status(stale_since=None)
        body = client.get(f"/issues/container?mode={mode}").text
        assert "dispatch-stale" in body
        assert "waiting for the GitHub Project to populate" not in body
        assert "GitHub unreachable" in body

    def test_banner_reserves_room_for_updating_pill(self, tmp_path, monkeypatch):
        """The "Updating…" pill is fixed at the top-right and overlaps the
        banner's second line otherwise — the two co-occur whenever a stale
        serve kicks a refresh."""
        client, app, _tm = _make_authed_client([_mk_issue(1)], tmp_path, monkeypatch)
        app.state.github_cache._fresh = False  # stale serve → kicks a refresh
        app.state.github_cache._stuck_stale = True
        app.state.github_cache.status = _failed_status()
        body = client.get("/issues/container").text
        assert "refresh-indicator" in body
        assert "dispatch-stale--inset" in body

    def test_banner_has_no_inset_without_pill(self, tmp_path, monkeypatch):
        client, app, _tm = _make_authed_client([_mk_issue(1)], tmp_path, monkeypatch)
        app.state.github_cache.status = _failed_status()
        body = client.get("/issues/container").text
        assert "refresh-indicator" not in body
        assert "dispatch-stale--inset" not in body

    def test_status_probe_runs_off_the_event_loop(self, authed_client):
        """``_render_view`` is a sync call from every async handler, and
        ``fetch_status`` takes the cache lock — which a background refresh
        holds across its whole disk write. Probing it inline would block the
        loop; it must ride the same threadpool hop as the capabilities probe.
        """
        import threading

        client, app, _tm = authed_client
        cache = app.state.github_cache
        seen: dict[str, int] = {}
        real_status = cache.fetch_status
        real_probe = app.state.capabilities_probe

        def _status():
            seen["status"] = threading.get_ident()
            return real_status()

        def _probe():
            seen["probe"] = threading.get_ident()
            return real_probe()

        cache.fetch_status = _status
        app.state.capabilities_probe = _probe
        client.get("/issues/container")
        assert seen.get("status") is not None
        assert seen["status"] == seen["probe"]


class TestClosedKeysFollowTheExplicitRefresh:
    """↻ must bypass the closed-keys TTL as well as the board TTL (#255 review).

    ``get_closed_keys`` grew a ``force`` parameter that skips the index
    debounce, but the explicit refresh route never passed it — so an issue
    closed on GitHub stayed in the list through a deliberate refresh until
    the 300 s closed-keys TTL lapsed.
    """

    def test_refresh_forces_the_closed_keys_lookup(self, authed_client):
        client, app, _ = authed_client
        client.post("/issues/refresh")
        forces = app.state.github_cache.__dict__.get("closed_forces", [])
        assert True in forces, f"explicit refresh did not force closed keys: {forces}"

    def test_plain_container_load_does_not_force(self, authed_client):
        client, app, _ = authed_client
        client.get("/issues/container")
        forces = app.state.github_cache.__dict__.get("closed_forces", [])
        assert True not in forces


class TestMarkClosedStaysOffTheEventLoop:
    """``mark_closed`` now takes a flock and rewrites the whole issue index
    (#255); inline in an ``async def`` route that stalls every request and
    SSE tick behind a competing writer (#255 review)."""

    def test_close_not_planned_marks_closed_in_the_threadpool(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "close_issue_not_planned", lambda repo, number: True)
        r = client.post(f"/issues/issue/{_quote('ben-ralston/agent-hq#67')}/close-not-planned")
        assert r.status_code == 200
        assert app.state.github_cache.__dict__.get("_marked_on_loop") == [False]


class TestHygieneMode:
    """#249 Hygiene mode: segment + badge, grouped finding cards, bar."""

    def test_mode_switch_has_hygiene_segment_with_badge(self, authed_client):
        client, app, _ = authed_client
        _find(app)
        r = client.get("/issues?mode=hygiene")
        assert 'href="/issues?mode=hygiene&keep=1"' in r.text
        assert 'id="hygiene-badge"' in r.text and ">1</span>" in r.text
        assert re.search(r'mode-switch__btn--active"\s+href="/issues\?mode=hygiene', r.text)
        assert app.state.ui_prefs_store.get_web_issues_mode() == "hygiene"
        assert 'data-mode="hygiene"' in r.text  # skeleton carries the mode

    def test_badge_route_hidden_at_zero(self, authed_client):
        client, _, _ = authed_client
        r = client.get("/issues/hygiene/badge")
        assert 'id="hygiene-badge"' in r.text and "mode-switch__count--hidden" in r.text

    def test_container_groups_open_findings_by_type(self, authed_client):
        client, app, _ = authed_client
        _find(app)
        _find(
            app,
            FindingType.MISMATCH,
            ["ben-ralston/agent-hq#44"],
            action={
                "kind": "bump_value",
                "key": "ben-ralston/agent-hq#44",
                "current": 2,
                "proposed": 5,
            },
            reasoning="security shaped",
        )
        _find(
            app,
            FindingType.STALE,
            ["ben-ralston/agent-hq#40"],
            action={
                "kind": "close_stale",
                "close": "ben-ralston/agent-hq#40",
                "last_activity": "2026-01-01T00:00:00Z",
                "value": 1,
            },
        )
        r = client.get("/issues/container?mode=hygiene")
        assert r.status_code == 200 and 'data-mode="hygiene"' in r.text
        dup = r.text.find("Duplicates")
        mis = r.text.find("Mismatches")
        stale = r.text.find("Stale")
        assert -1 not in (dup, mis, stale) and dup < mis < stale
        assert "looks the same" in r.text and "security shaped" in r.text
        assert "dispatch view" in r.text and "filter bar" in r.text  # titles resolved
        assert "/issues/issue/ben-ralston/agent-hq%2367" in r.text  # opens the sheet
        fid = "duplicate:ben-ralston/agent-hq#40+ben-ralston/agent-hq#67"
        enc = quote(fid, safe="/")  # Jinja's urlencode: "/" kept, ":" "#" "+" escaped
        assert f"/issues/hygiene/{enc}/preview" in r.text
        assert f"/issues/hygiene/{enc}/dismiss?mode=hygiene" in r.text
        assert '"found_at": "2026-09-12T10:00:00+00:00"' in r.text
        assert "Last swept" in r.text
        # No dispatch action bar / checkboxes in Hygiene.
        assert 'id="action-bar"' not in r.text
        # This fixture has triage=None → AI unavailable → Run now disabled + hint.
        assert "Sweep off" in r.text and 'aria-describedby="hygiene-off-hint"' in r.text

    def test_accepted_and_dismissed_findings_are_not_listed(self, authed_client):
        client, app, _ = authed_client
        fid = _find(app)
        app.state.hygiene_store.dismiss(fid, now="t")
        r = client.get("/issues/container?mode=hygiene")
        assert "looks the same" not in r.text
        assert "Nothing to clean up" in r.text

    def test_unconfigured_store_renders_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            __import__("agent_hq.web.routes.issues", fromlist=["x"]),
            "detect_active_worktrees",
            lambda repos: set(),
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file, config=_mk_config(), github_cache=_StubCache([_mk_issue(1)])
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.get("/issues/container?mode=hygiene")
        assert r.status_code == 200 and "Hygiene store not configured" in r.text

    def test_repo_chip_filters_findings(self, tmp_path, monkeypatch):
        issues = [_mk_issue(1), _mk_issue(2, repo="o/other")]
        client, app, tm = _make_authed_client(issues, tmp_path, monkeypatch)
        _find(
            app,
            FindingType.STALE,
            ["ben-ralston/agent-hq#1"],
            action={
                "kind": "close_stale",
                "close": "ben-ralston/agent-hq#1",
                "last_activity": "2026-01-01T00:00:00Z",
                "value": 1,
            },
            reasoning="one-stale",
        )
        _find(
            app,
            FindingType.STALE,
            ["o/other#2"],
            action={
                "kind": "close_stale",
                "close": "o/other#2",
                "last_activity": "2026-01-01T00:00:00Z",
                "value": 1,
            },
            reasoning="two-stale",
        )
        _set_state(client, app, tm, DispatchState(repos=["o/other"]))
        r = client.get("/issues/container?mode=hygiene")
        assert "two-stale" in r.text and "one-stale" not in r.text

    def test_failed_execution_shows_last_error_on_card(self, authed_client):
        client, app, _ = authed_client
        fid = _find(app)
        app.state.hygiene_store.mark_failed(
            fid,
            execution={
                "ok": False,
                "at": "t",
                "steps": [{"label": "Close x", "ok": False, "detail": "gh exploded"}],
            },
        )
        r = client.get("/issues/container?mode=hygiene")
        assert "Last attempt failed" in r.text and "gh exploded" in r.text


class TestHygieneSweepTrigger:
    """#249: a container load kicks one sweep when never swept or > 24 h ago;
    completion publishes ``hygiene.swept``; Run now forces."""

    def _build(self, tmp_path, monkeypatch, *, hygiene_enabled=True, report=None):
        from agent_hq.hygiene_worker import SweepReport
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        monkeypatch.setattr(issues_routes.shutil, "which", lambda name: "/usr/local/bin/claude")
        calls = []

        def fake_run_sweep(issues_arg, ts, hs, tcfg, repos_cfg, **kw):
            calls.append([i.number for i in issues_arg])
            return report or SweepReport(written=1)

        monkeypatch.setattr(issues_routes, "run_sweep", fake_run_sweep)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        cfg = _mk_config(
            triage=TriageConfig(data_dir=tmp_path / "d", hygiene_enabled=hygiene_enabled)
        )
        app = create_app(
            token_file=tm.token_file,
            config=cfg,
            github_cache=_StubCache([_mk_issue(67)]),
            triage_store=TriageStore(tmp_path / "d"),
            hygiene_store=HygieneStore(tmp_path / "d"),
        )
        _triage(app, 67)  # keep the triage worker quiet
        return app, tm, calls

    def _wait(self, app, timeout=5.0):
        import time as _time

        deadline = _time.time() + timeout
        while app.state.hygiene_refresher.in_flight:
            if _time.time() > deadline:
                raise AssertionError("sweep never finished")
            _time.sleep(0.01)

    def test_background_sweep_runs_on_a_forced_board_load(self, tmp_path, monkeypatch):
        """Finder round 4: the sweep's open set must not come from the SWR
        list an open tab happened to hold — it refetches the boards (a few
        GraphQL points once a day) so cache lag cannot resolve findings."""
        app, tm, calls = self._build(tmp_path, monkeypatch)
        cache = app.state.github_cache
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == [[67]] and True in cache.get_issues_calls

    def test_claude_path_lookup_is_cached_per_app(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch)
        seen = []
        monkeypatch.setattr(
            issues_routes.shutil, "which", lambda name: seen.append(name) or "/usr/bin/claude"
        )
        assert issues_routes._hygiene_ai_available(app) and issues_routes._triage_ai_available(app)
        assert issues_routes._hygiene_ai_available(app)
        assert seen == ["claude"]  # one PATH sweep, shared by both predicates

    def test_never_swept_kicks_once_and_publishes(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch)
        published = []

        async def spy(event):
            published.append(event.topic)

        monkeypatch.setattr(app.state.broker, "publish", spy)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            assert client.get("/issues/container?mode=queue").status_code == 200
            self._wait(app)
        assert calls == [[67]]
        assert "hygiene.swept" in published

    def test_empty_issue_list_does_not_kick(self, tmp_path, monkeypatch):
        """Built-in review: a cold cache / failed fetch has nothing to sweep;
        kicking would either stamp (disabling the sweep for 24 h) or churn."""
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch)

        async def empty(app_):
            return [], False

        monkeypatch.setattr(issues_routes, "_load_issues_swr", empty)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_empty_issue_list_kicks_when_off_board_detection_has_a_board_to_check(
        self, tmp_path, monkeypatch
    ):
        """#273 review round 5: a freshly configured, empty board is the
        off-board headline case — the auto-sweep must fire for it. A run
        that then finds nothing to check (cold cache) reports ``skipped``
        and trips the refresher cooldown, so a cold cache cannot loop
        forced refetches on every load."""
        from agent_hq.hygiene_worker import SweepReport
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch, report=SweepReport(skipped=True))
        app.state.config.repos["ben-ralston/agent-hq"] = RepoConfig(
            "ben-ralston/agent-hq", Path("/tmp/x"), project="Agent HQ"
        )

        async def empty(app_):
            return [], False

        monkeypatch.setattr(issues_routes, "_load_issues_swr", empty)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
            client.get("/issues/container?mode=queue")
            self._wait(app)
        # Kicked once (the runner refetches the boards, so the stub's issue
        # rides along); the skipped run tripped the cooldown for the second load.
        assert len(calls) == 1

    def test_recent_sweep_does_not_kick(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch)
        app.state.hygiene_store.stamp_swept(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_old_sweep_kicks(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch)
        app.state.hygiene_store.stamp_swept("2026-01-01T00:00:00+00:00")
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == [[67]]

    def test_disabled_or_claudeless_does_not_kick(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch, hygiene_enabled=False)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []
        app, tm, calls = self._build(tmp_path / "b", monkeypatch)
        monkeypatch.setattr(issues_routes.shutil, "which", lambda name: None)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == []

    def test_errored_sweep_trips_cooldown_without_publishing(self, tmp_path, monkeypatch):
        from agent_hq.hygiene_worker import SweepReport

        app, tm, calls = self._build(
            tmp_path, monkeypatch, report=SweepReport(error="invoke: boom")
        )
        published = []

        async def spy(event):
            published.append(event.topic)

        monkeypatch.setattr(app.state.broker, "publish", spy)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
            # The (fake) sweep never stamped, so without the cooldown this
            # second load would kick again.
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == [[67]]
        # Codex round: completion is published even on error, so the bar's
        # "Sweeping…" pill is replaced by Run now + the failure state.
        assert published.count("hygiene.swept") == 1

    def test_no_change_sweep_still_publishes_completion(self, tmp_path, monkeypatch):
        """Codex P2: a clean sweep (nothing written or resolved) must still
        tell the page it finished — the container has no poll, so without
        this "Sweeping…" stuck until the next load."""
        from agent_hq.hygiene_worker import SweepReport

        app, tm, calls = self._build(tmp_path, monkeypatch, report=SweepReport())
        published = []

        async def spy(event):
            published.append(event.topic)

        monkeypatch.setattr(app.state.broker, "publish", spy)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            client.get("/issues/container?mode=queue")
            self._wait(app)
        assert calls == [[67]] and "hygiene.swept" in published

    def test_run_now_forces_a_kick(self, tmp_path, monkeypatch):
        app, tm, calls = self._build(tmp_path, monkeypatch)
        app.state.hygiene_store.stamp_swept(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        kicks = []

        def spy_kick(factory, **kwargs):
            kicks.append(kwargs)
            return True

        monkeypatch.setattr(app.state.hygiene_refresher, "kick", spy_kick)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.post("/issues/hygiene/run?mode=hygiene")
        assert r.status_code == 200 and 'data-mode="hygiene"' in r.text
        assert kicks == [{"force": True}]

    def test_run_now_without_ai_does_not_kick(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        app, tm, calls = self._build(tmp_path, monkeypatch)
        monkeypatch.setattr(issues_routes.shutil, "which", lambda name: None)
        kicks = []
        monkeypatch.setattr(
            app.state.hygiene_refresher, "kick", lambda f, **k: kicks.append(f) or True
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.post("/issues/hygiene/run?mode=hygiene").status_code == 200
        assert kicks == []


class TestHygieneActions:
    """#249: preview shows the plan; Execute runs it; Dismiss suppresses."""

    FID = "duplicate:ben-ralston/agent-hq#40+ben-ralston/agent-hq#67"
    ENC = quote(FID, safe="/")

    def _gh(self, monkeypatch, *, fail=None):
        from agent_hq import hygiene_actions as ha

        calls = []
        monkeypatch.setattr(
            ha,
            "comment_issue",
            lambda r, n, b: calls.append(("comment", r, n, b)) or fail != "comment",
        )
        monkeypatch.setattr(
            ha,
            "close_issue_not_planned",
            lambda r, n: calls.append(("close", r, n)) or fail != "close",
        )
        return calls

    def test_preview_lists_steps_and_execute_button(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        self._gh(monkeypatch)
        _find(app)
        r = client.get(f"/issues/hygiene/{self.ENC}/preview")
        assert r.status_code == 200
        assert "Comment on agent-hq#40: Duplicate of #67" in r.text
        assert "Close agent-hq#40 as not planned" in r.text
        assert f"/issues/hygiene/{self.ENC}/accept?mode=hygiene" in r.text
        assert '"found_at": "2026-09-12T10:00:00+00:00"' in r.text
        assert "data-modal-close" in r.text  # Cancel

    def test_preview_unknown_or_decided_is_404(self, authed_client):
        client, app, _ = authed_client
        assert client.get("/issues/hygiene/nope/preview").status_code == 404
        fid = _find(app)
        app.state.hygiene_store.dismiss(fid, now="t")
        assert client.get(f"/issues/hygiene/{self.ENC}/preview").status_code == 404

    def test_accept_executes_marks_accepted_and_invalidates(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._gh(monkeypatch)
        _find(app)
        published = []

        async def spy(event):
            published.append((event.topic, event.data))

        monkeypatch.setattr(app.state.broker, "publish", spy)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and 'data-mode="hygiene"' in r.text
        assert calls == [
            (
                "comment",
                "ben-ralston/agent-hq",
                40,
                "Duplicate of #67 (closed by the agent-hq hygiene sweep)",
            ),
            ("close", "ben-ralston/agent-hq", 40),
        ]
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.ACCEPTED and f.execution["ok"] is True
        assert "looks the same" not in r.text  # card gone
        assert ("issue.invalidated", {"key": "ben-ralston/agent-hq#40"}) in published
        assert app.state.github_cache._marked_closed == [("ben-ralston/agent-hq", 40)]

    def test_accept_failure_keeps_finding_open_with_error(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._gh(monkeypatch, fail="close")
        _find(app)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.OPEN and f.execution["ok"] is False
        assert "Last attempt failed" in r.text and "gh issue close failed" in r.text
        assert [c[0] for c in calls] == ["comment", "close"]
        assert not getattr(app.state.github_cache, "_marked_closed", [])

    def test_accept_with_stale_revision_does_nothing(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._gh(monkeypatch)
        _find(app)
        r = client.post(f"/issues/hygiene/{self.ENC}/accept?mode=hygiene", data={"found_at": "old"})
        assert r.status_code == 200 and calls == []
        assert app.state.hygiene_store.get(self.FID).status is FindingStatus.OPEN

    def test_accept_mismatch_bumps_value_as_human(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._gh(monkeypatch)
        _propose(app, 44, value=2)
        fid = _find(
            app,
            FindingType.MISMATCH,
            ["ben-ralston/agent-hq#44"],
            action={
                "kind": "bump_value",
                "key": "ben-ralston/agent-hq#44",
                "current": 2,
                "proposed": 5,
            },
        )
        r = client.post(
            f"/issues/hygiene/{quote(fid, safe='/')}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls == []
        rec = app.state.triage_store.get("ben-ralston/agent-hq#44")
        assert rec.value == 5 and rec.provenance["value"] == "human"
        assert app.state.hygiene_store.get(fid).status is FindingStatus.ACCEPTED

    def test_dismiss_suppresses_and_respects_revision(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        _find(app)
        published = []

        async def spy(event):
            published.append(event.topic)

        monkeypatch.setattr(app.state.broker, "publish", spy)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/dismiss?mode=hygiene", data={"found_at": "old"}
        )
        assert r.status_code == 200
        assert app.state.hygiene_store.get(self.FID).status is FindingStatus.OPEN
        assert published == []  # stale revision: no write, no publish
        r = client.post(
            f"/issues/hygiene/{self.ENC}/dismiss?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and "looks the same" not in r.text
        assert app.state.hygiene_store.get(self.FID).status is FindingStatus.DISMISSED
        # The mode-switch badge lives outside the swapped container, so the
        # route must publish for the SSE listener to refetch it (smoke find).
        assert published == ["issue.invalidated", "issue.invalidated"]
        assert client.post("/issues/hygiene/nope/dismiss?mode=hygiene").status_code == 404

    # ---- review round (2026-09-12): claim before execute, in-flight card ----

    def test_accept_claims_first_and_refuses_a_second_execute(self, authed_client, monkeypatch):
        """Review P1: a claim under the flock precedes any GitHub write, so a
        finding already being executed (or a double tap) never replays the
        plan. Simulated by pre-claiming through the store."""
        client, app, _ = authed_client
        calls = self._gh(monkeypatch)
        _find(app)
        assert app.state.hygiene_store.claim(self.FID, now="t") is True
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls == []
        # The card shows the in-flight state (the line is always in the DOM
        # for the JS mirror; only an unhidden one counts).
        assert "data-hygiene-card-busy>" in r.text
        assert app.state.hygiene_store.get(self.FID).status is FindingStatus.OPEN

    def test_accept_retry_reuses_umbrella_and_releases_the_claim(self, tmp_path, monkeypatch):
        from agent_hq import hygiene_actions as ha

        issues = [
            _mk_issue(3, title="[sentinel] a", labels=["sentinel"]),
            _mk_issue(4, title="[sentinel] b", labels=["sentinel"]),
        ]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        fid = _find(
            app,
            FindingType.CLUSTER,
            ["ben-ralston/agent-hq#3", "ben-ralston/agent-hq#4"],
            action={
                "kind": "merge_cluster",
                "repo": "ben-ralston/agent-hq",
                "umbrella_title": "Booms",
                "children": ["ben-ralston/agent-hq#3", "ben-ralston/agent-hq#4"],
            },
        )
        enc = quote(fid, safe="/")
        calls = []
        monkeypatch.setattr(
            ha, "create_issue", lambda *a, **k: calls.append("create") or "https://x/issues/900"
        )
        monkeypatch.setattr(
            ha, "comment_issue", lambda r, n, b: calls.append(("comment", n, b)) or False
        )
        monkeypatch.setattr(
            ha, "close_issue_not_planned", lambda r, n: calls.append(("close", n)) or True
        )
        found = {"found_at": "2026-09-12T10:00:00+00:00"}
        r = client.post(f"/issues/hygiene/{enc}/accept?mode=hygiene", data=found)
        assert r.status_code == 200
        f = app.state.hygiene_store.get(fid)
        assert f.status is FindingStatus.OPEN and f.execution["umbrella"] == 900
        assert not f.execution.get("in_flight")
        assert "Last attempt failed" in r.text
        # Retry: comments now succeed; no second create.
        monkeypatch.setattr(
            ha, "comment_issue", lambda r, n, b: calls.append(("comment", n, b)) or True
        )
        calls.clear()
        r = client.post(f"/issues/hygiene/{enc}/accept?mode=hygiene", data=found)
        assert r.status_code == 200
        assert "create" not in calls
        assert ("comment", 3, "Tracked by #900 (agent-hq hygiene sweep)") in calls
        assert app.state.hygiene_store.get(fid).status is FindingStatus.ACCEPTED

    def test_preview_marks_execute_button_as_single_shot(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        self._gh(monkeypatch)
        _find(app)
        r = client.get(f"/issues/hygiene/{self.ENC}/preview")
        assert 'hx-disabled-elt="this"' in r.text

    def test_hygiene_container_survives_a_malformed_stored_finding(self, authed_client):
        """Review: a hand-edited findings.json entry missing an action key is
        skipped by the store (preserved on disk), not rendered into a 500."""
        import json as _json

        client, app, _ = authed_client
        _find(app)
        path = app.state.hygiene_store.data_dir / "hygiene" / "findings.json"
        data = _json.loads(path.read_text())
        data["findings"]["bad"] = {
            "id": "bad",
            "type": "mismatch",
            "issue_keys": ["ben-ralston/agent-hq#44"],
            "action": {"kind": "bump_value", "key": "ben-ralston/agent-hq#44"},
            "reasoning": "x",
            "status": "open",
            "found_at": "t",
        }
        path.write_text(_json.dumps(data))
        r = client.get("/issues/container?mode=hygiene")
        assert r.status_code == 200 and "looks the same" in r.text
        assert client.get("/issues/hygiene/bad/preview").status_code == 404

    def test_hygiene_routes_404_without_store(self, tmp_path, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "detect_active_worktrees", lambda repos: set())
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file, config=_mk_config(), github_cache=_StubCache([_mk_issue(1)])
        )
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        assert client.get(f"/issues/hygiene/{self.ENC}/preview").status_code == 404
        assert client.post(f"/issues/hygiene/{self.ENC}/accept").status_code == 404
        assert client.post("/issues/hygiene/run").status_code == 404

    def test_accept_releases_the_claim_when_planning_raises(self, authed_client, monkeypatch):
        """Review round 2: an exception between claim and execute must not
        leave the finding stuck in "Executing…" forever."""
        from agent_hq.web.routes import issues as routes

        client, app, _ = authed_client
        calls = self._gh(monkeypatch)
        _find(app)

        async def fresh(app_, *, strict=False):
            return [_mk_issue(40), _mk_issue(67)]

        def boom(*args, **kwargs):
            raise RuntimeError("cache exploded")

        monkeypatch.setattr(routes, "_load_issues_fresh", fresh)
        monkeypatch.setattr(routes, "plan_steps", boom)
        with pytest.raises(RuntimeError, match="cache exploded"):  # TestClient re-raises
            client.post(
                f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
                data={"found_at": "2026-09-12T10:00:00+00:00"},
            )
        assert calls == []
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.OPEN and not f.execution.get("in_flight")
        assert f.execution["ok"] is False and "cache exploded" in f.execution["steps"][0]["detail"]


class TestHygieneProgress:
    """Hand-test round: Execute turns the preview into a live checklist.
    The accept POST stays synchronous; the modal polls ``/progress`` while
    the claim is held and loads ``?final=1`` when the POST returns."""

    FID = "duplicate:ben-ralston/agent-hq#40+ben-ralston/agent-hq#67"
    ENC = quote(FID, safe="/")

    def test_preview_carries_the_execute_hooks(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        _find(app)
        r = client.get(f"/issues/hygiene/{self.ENC}/preview")
        assert "data-hygiene-execute" in r.text
        assert f'data-progress-url="/issues/hygiene/{self.ENC}/progress"' in r.text
        assert 'id="hygiene-progress-steps"' in r.text
        assert r.text.count("hygiene-progress__step is-pending") == 2

    def test_accept_records_the_plan_and_progress_while_running(self, authed_client, monkeypatch):
        from agent_hq import hygiene_actions as ha

        client, app, _ = authed_client
        store = app.state.hygiene_store
        _find(app)
        seen = []

        def comment(r, n, b):
            ex = store.get(self.FID).execution
            seen.append(("comment", ex.get("plan"), ex.get("progress")))
            return True

        def close(r, n):
            ex = store.get(self.FID).execution
            seen.append(("close", [s["ok"] for s in ex.get("progress")]))
            return True

        monkeypatch.setattr(ha, "comment_issue", comment)
        monkeypatch.setattr(ha, "close_issue_not_planned", close)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and r.headers["X-Hygiene-Ran"] == "1"
        assert seen[0][0] == "comment" and len(seen[0][1]) == 2 and seen[0][2] == []
        assert seen[1] == ("close", [True])
        ex = store.get(self.FID).execution
        assert "progress" not in ex and len(ex["plan"]) == 2

    def test_accept_that_did_not_run_says_so(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        _find(app)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene", data={"found_at": "stale"}
        )
        assert r.status_code == 200 and r.headers["X-Hygiene-Ran"] == "0"

    def test_progress_renders_running_done_and_final_states(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        store = app.state.hygiene_store
        _find(app)
        url = f"/issues/hygiene/{self.ENC}/progress"
        # Not claimed yet (the first poll can land before the claim): no swap.
        assert client.get(url).status_code == 204
        store.claim(self.FID, now="t")
        store.record_progress(self.FID, plan=["one", "two", "three"], done=[])
        r = client.get(url)
        assert r.text.index("is-running") < r.text.index("is-pending")
        assert "one" in r.text and "three" in r.text
        store.record_progress(
            self.FID,
            plan=["one", "two", "three"],
            done=[{"label": "one", "ok": True, "detail": ""}],
        )
        r = client.get(url)
        assert r.text.count("is-done") == 1 and r.text.count("is-running") == 1
        assert r.text.count("is-pending") == 1
        store.mark_failed(
            self.FID,
            execution={
                "ok": False,
                "at": "t",
                "plan": ["one", "two", "three"],
                "steps": [
                    {"label": "one", "ok": True, "detail": ""},
                    {"label": "two", "ok": False, "detail": "gh said no"},
                ],
            },
        )
        r = client.get(url + "?final=1")
        assert r.text.count("is-done") == 1 and r.text.count("is-failed") == 1
        assert "gh said no" in r.text and "is-running" not in r.text
        store2 = app.state.hygiene_store
        store2.claim(self.FID, now="t2")
        store2.mark_accepted(
            self.FID,
            now="t2",
            execution={
                "ok": True,
                "at": "t2",
                "steps": [
                    {"label": "one", "ok": True, "detail": ""},
                    {"label": "two", "ok": True, "detail": ""},
                ],
            },
        )
        r = client.get(url + "?final=1")  # accepted findings still render
        assert r.status_code == 200 and r.text.count("is-done") == 2
        assert client.get("/issues/hygiene/nope/progress").status_code == 404

    def test_progress_has_no_rows_yet_is_a_204_and_final_lists_unrun_steps(
        self, authed_client, monkeypatch
    ):
        """Built-in review: a poll before the plan is recorded (or a late poll
        after release) must not blank the checklist — 204 = no swap. The
        final view lists steps that never ran as pending."""
        client, app, _ = authed_client
        store = app.state.hygiene_store
        _find(app)
        url = f"/issues/hygiene/{self.ENC}/progress"
        assert client.get(url).status_code == 204
        store.claim(self.FID, now="t")
        assert client.get(url).status_code == 204  # claimed, plan not recorded yet
        store.record_progress(self.FID, plan=["one", "two", "three"], done=[])
        store.mark_failed(
            self.FID,
            execution={
                "ok": False,
                "at": "t",
                "steps": [{"label": "one", "ok": False, "detail": "no"}],
            },
        )
        assert client.get(url).status_code == 204  # released: a late poll swaps nothing
        r = client.get(url + "?final=1")
        assert r.status_code == 200
        assert r.text.count("is-failed") == 1 and r.text.count("is-pending") == 2

    def test_expired_claim_renders_idle_and_accept_takes_over(self, authed_client, monkeypatch):

        client, app, _ = authed_client
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        _find(app)
        store = app.state.hygiene_store
        assert store.claim(self.FID, now="2026-01-01T00:00:00+00:00") is True
        r = client.get("/issues/container?mode=hygiene")
        assert "data-hygiene-card-busy>" not in r.text and "data-hygiene-card-busy hidden" in r.text
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and r.headers["X-Hygiene-Ran"] == "1" and calls
        assert store.get(self.FID).status is FindingStatus.ACCEPTED

    def test_poller_is_aborted_before_removal(self):
        js = (Path(__file__).parent.parent / "agent_hq/web/static/agent_hq.js").read_text()
        assert 'htmx.trigger(poller, "htmx:abort")' in js
        # Finder round 4: a POST that never got a response (no X-Hygiene-Ran
        # at all) must not load a "final" checklist as if something ran.
        assert "ran === null" in js and "Could not reach the server" in js

    def test_hx_vals_are_json_encoded(self, authed_client):
        client, app, _ = authed_client
        _find(app)
        r = client.get("/issues/container?mode=hygiene")
        assert 'hx-vals=\'{"found_at": "2026-09-12T10:00:00+00:00"}\'' in r.text
        r = client.get(f"/issues/hygiene/{self.ENC}/preview")
        assert 'hx-vals=\'{"found_at": "2026-09-12T10:00:00+00:00"}\'' in r.text


class TestHygieneRemoveChild:
    """Hand-test round: a cluster you half-disagree with — drop an issue
    from the card; the plan follows."""

    KEYS = [f"ben-ralston/agent-hq#{n}" for n in (3, 4, 5)]

    def _cluster(self, tmp_path, monkeypatch, keys=None):
        keys = keys or self.KEYS
        issues = [_mk_issue(int(k.rsplit("#", 1)[1]), labels=["sentinel"]) for k in keys]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        fid = _find(
            app,
            FindingType.CLUSTER,
            keys,
            action={
                "kind": "merge_cluster",
                "repo": "ben-ralston/agent-hq",
                "umbrella_title": "Booms",
                "children": list(keys),
            },
        )
        return client, app, fid

    def test_card_offers_remove_only_when_more_than_two_children(self, tmp_path, monkeypatch):
        client, app, fid = self._cluster(tmp_path, monkeypatch)
        r = client.get("/issues/container?mode=hygiene")
        enc = quote(fid, safe="/")
        assert r.text.count(f"/issues/hygiene/{enc}/remove") == 3
        client2, app2, fid2 = self._cluster(tmp_path / "b", monkeypatch, self.KEYS[:2])
        r = client2.get("/issues/container?mode=hygiene")
        assert "/remove" not in r.text

    def test_remove_rewrites_card_and_plan(self, tmp_path, monkeypatch):
        client, app, fid = self._cluster(tmp_path, monkeypatch)
        enc = quote(fid, safe="/")
        r = client.post(
            f"/issues/hygiene/{enc}/remove?mode=hygiene",
            data={"key": self.KEYS[2], "found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200
        num = '<span class="issue-meta__num">#%d</span>'
        assert num % 5 not in r.text and num % 3 in r.text and num % 4 in r.text
        assert "close 2 sentinel issues" in r.text and "edited" in r.text
        assert "/remove" not in r.text  # two left: no further removal offered
        f = app.state.hygiene_store.get("cluster:" + "+".join(self.KEYS[:2]))
        assert f.issue_keys == self.KEYS[:2] and f.edited_by == "human"
        enc = quote(f.id, safe="/")  # re-keyed by the edit (round 3)
        r = client.get(f"/issues/hygiene/{enc}/preview")
        assert "body lists #3, #4)" in r.text and "#5" not in r.text
        assert f'"found_at": "{f.found_at}"' in r.text

    def test_remove_with_stale_revision_is_a_no_op(self, tmp_path, monkeypatch):
        client, app, fid = self._cluster(tmp_path, monkeypatch)
        enc = quote(fid, safe="/")
        r = client.post(
            f"/issues/hygiene/{enc}/remove?mode=hygiene",
            data={"key": self.KEYS[2], "found_at": "stale"},
        )
        assert r.status_code == 200 and '<span class="issue-meta__num">#5</span>' in r.text
        assert app.state.hygiene_store.get(fid).issue_keys == self.KEYS
        assert client.post("/issues/hygiene/nope/remove", data={"key": "x"}).status_code == 404


class TestHygieneExecuteFreshState:
    """Codex round 3: Execute plans against a FORCED refetch of the boards,
    never the possibly-expired cache; if the refetch fails, nothing runs."""

    FID = "duplicate:ben-ralston/agent-hq#40+ben-ralston/agent-hq#67"
    ENC = quote(FID, safe="/")

    def test_execute_refetches_and_refuses_when_github_is_unreachable(
        self, authed_client, monkeypatch
    ):
        from agent_hq.web.routes import issues as issues_routes

        client, app, _ = authed_client
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        _find(app)
        seen = []

        async def fresh(app_, *, strict=False):
            seen.append(strict)
            raise RuntimeError("gh: rate limited")

        monkeypatch.setattr(issues_routes, "_load_issues_fresh", fresh)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and r.headers["X-Hygiene-Ran"] == "1"
        assert seen == [True] and calls == []
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.OPEN and not f.execution.get("in_flight")
        assert "current issue state" in f.execution["steps"][0]["label"]
        assert "Last attempt failed" in r.text

    def test_execute_refuses_when_the_forced_fetch_served_last_good_data(
        self, authed_client, monkeypatch
    ):
        """The real cache never raises on a failed fetch (#254 serves the
        last-good list and records the failure) — the refusal must come from
        fetch_status(), or a stubbed/failing gh plans against stale data
        (found by the scratch-HOME smoke)."""
        from agent_hq.github import FetchStatus

        client, app, _ = authed_client
        cache = app.state.github_cache
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        _find(app)
        cache.status = FetchStatus(ok=False, message="GitHub unreachable")
        cache.failed = {"ben-ralston/1"}  # the configured project's key
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls == []
        assert True in cache.get_issues_calls  # a forced fetch was attempted
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.OPEN
        assert "GitHub unreachable" in f.execution["steps"][0]["detail"]
        assert f.execution["plan"] == [f.execution["steps"][0]["label"]]  # no stale padding
        r = client.get(f"/issues/hygiene/{self.ENC}/progress?final=1")
        assert r.text.count("is-failed") == 1 and "is-pending" not in r.text

    def test_execute_ignores_failures_recorded_for_unrelated_keys(self, authed_client, monkeypatch):
        """Built-in review round 4: fetch_status() is process-global — a
        lingering failure for a project/repo this refetch never touched must
        not refuse an execute whose own fetches all succeeded."""
        from agent_hq.github import FetchStatus

        client, app, _ = authed_client
        cache = app.state.github_cache
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        _find(app)
        cache.status = FetchStatus(ok=False, message="closed:someone/else failed")
        cache.failed = {"closed:someone/else", "someone/9"}
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls
        assert app.state.hygiene_store.get(self.FID).status is FindingStatus.ACCEPTED

    def test_execute_rehydrates_timestamps_from_the_synced_index(self, authed_client, monkeypatch):
        """Codex round 4: the board fetch enriches dates through a debounced
        index sync, so a strict fetch can still carry a stale updated_at —
        the forced closed-keys sync refreshed the index afterwards, and the
        plan must read THAT."""
        from agent_hq.web.routes import issues as issues_routes

        client, app, _ = authed_client
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        fid = _find(
            app,
            FindingType.STALE,
            ["ben-ralston/agent-hq#40"],
            action={
                "kind": "close_stale",
                "close": "ben-ralston/agent-hq#40",
                "last_activity": "2026-01-01T00:00:00Z",
                "value": 1,
            },
        )

        def rehydrate(issues):
            for i in issues:
                if i.number == 40:
                    i.updated_at = "2026-09-12T09:00:00+00:00"  # commented an hour ago

        monkeypatch.setattr(issues_routes, "refresh_issue_timestamps", rehydrate)
        r = client.post(
            f"/issues/hygiene/{quote(fid, safe='/')}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls == []
        f = app.state.hygiene_store.get(fid)
        assert f.status is FindingStatus.OPEN and "new activity" in f.execution["steps"][0]["label"]

    def test_execute_uses_the_fresh_list_not_the_cache(self, authed_client, monkeypatch):
        from agent_hq.web.routes import issues as issues_routes

        client, app, _ = authed_client
        calls = TestHygieneActions._gh(TestHygieneActions(), monkeypatch)
        _find(app)

        async def fresh(app_, *, strict=False):
            return [_mk_issue(67)]  # #40 closed since the cache was filled

        monkeypatch.setattr(issues_routes, "_load_issues_fresh", fresh)
        r = client.post(
            f"/issues/hygiene/{self.ENC}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and calls == []
        f = app.state.hygiene_store.get(self.FID)
        assert f.status is FindingStatus.OPEN
        assert "no longer open" in f.execution["steps"][0]["label"]


class TestHygienePreviewGating:
    """Hand-test round: a plan with nothing actionable disables Execute."""

    def test_preview_disables_execute_when_nothing_is_actionable(self, tmp_path, monkeypatch):
        issues = [_mk_issue(3, labels=["sentinel"])]
        client, app, _ = _make_authed_client(issues, tmp_path, monkeypatch)
        fid = _find(
            app,
            FindingType.CLUSTER,
            ["ben-ralston/agent-hq#3", "ben-ralston/agent-hq#4"],
            action={
                "kind": "merge_cluster",
                "repo": "ben-ralston/agent-hq",
                "umbrella_title": "Booms",
                "children": ["ben-ralston/agent-hq#3", "ben-ralston/agent-hq#4"],
            },
        )
        r = client.get(f"/issues/hygiene/{quote(fid, safe='/')}/preview")
        assert r.status_code == 200
        assert "fewer than two" in r.text and "Nothing left to do" in r.text
        assert 'hx-disabled-elt="this" disabled' in r.text


class TestHygieneOffboard:
    """#273: open issues missing from their project board — a first section
    with per-repo "Add all", cards that link to GitHub (the issue is not in
    the board cache), and Accept = item-add + Status Todo + board refresh."""

    GADGET = "ben-ralston/gadget"

    def _find(self, app, number, *, title="sentinel misfiles"):
        key = f"{self.GADGET}#{number}"
        # open_keys is the BOARD set: the off-board key must not be in it, or
        # the store reads it as landed and resolves it on the spot — so it
        # carries the fixture board (67/40) explicitly, and any duplicate
        # finding a test wants alongside is recorded BEFORE these.
        return _find(
            app,
            FindingType.OFFBOARD,
            [key],
            action={
                "kind": "add_to_board",
                "key": key,
                "project_number": 13,
                "project_owner": "@me",
                "project_name": "Gadget",
                "title": title,
            },
            reasoning="off the Gadget board",
            open_keys={"ben-ralston/agent-hq#67", "ben-ralston/agent-hq#40"},
        )

    def _wire(self, monkeypatch, app, *, fail_numbers=()):
        from agent_hq import hygiene_actions as ha
        from agent_hq.github import FieldInfo, GHError

        calls = []

        def add(number, owner, url):
            calls.append(("add", number, owner, url))
            if any(url.endswith(f"/issues/{n}") for n in fail_numbers):
                raise GHError("item-add boom")
            return "PVTI_" + url.rsplit("/", 1)[1]

        def edit(project_node_id, item_id, field_id, option_id=None):
            calls.append(("edit", project_node_id, item_id, field_id, option_id))
            return True

        monkeypatch.setattr(ha, "add_to_project", add)
        monkeypatch.setattr(ha, "set_field_value", edit)
        monkeypatch.setattr(
            ha, "fetch_issue_summary", lambda repo, n: {"title": "t", "state": "open"}
        )
        app.state.github_cache.get_field_info = lambda number, owner, name: FieldInfo(
            field_id="F1", project_node_id="PVT_1", options={"Todo": "OPT_TODO"}
        )
        return calls

    def test_section_lists_first_with_github_links_and_add_all(self, authed_client):
        client, app, _ = authed_client
        _find(app)  # a duplicate, to order against
        fid = self._find(app, 8)
        r = client.get("/issues/container?mode=hygiene")
        assert r.status_code == 200
        off, dup = r.text.find("Not on board"), r.text.find("Duplicates")
        assert -1 not in (off, dup) and off < dup
        assert 'href="https://github.com/ben-ralston/gadget/issues/8"' in r.text
        assert "sentinel misfiles" in r.text and "off the Gadget board" in r.text
        assert "Add #8 to the Gadget board" in r.text
        assert "Add to board…" in r.text
        enc = quote(fid, safe="/")
        assert f"/issues/hygiene/{enc}/preview" in r.text
        assert "/issues/hygiene/offboard/add-all?mode=hygiene" in r.text
        assert '"repo": "ben-ralston/gadget"' in r.text
        assert "Add all (1)" in r.text

    def test_accept_adds_sets_todo_and_refreshes_the_board(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app)
        fid = self._find(app, 8)
        r = client.post(
            f"/issues/hygiene/{quote(fid, safe='/')}/accept?mode=hygiene",
            data={"found_at": "2026-09-12T10:00:00+00:00"},
        )
        assert r.status_code == 200 and r.headers["X-Hygiene-Ran"] == "1"
        assert calls == [
            ("add", 13, "@me", "https://github.com/ben-ralston/gadget/issues/8"),
            ("edit", "PVT_1", "PVTI_8", "F1", "OPT_TODO"),
        ]
        f = app.state.hygiene_store.get(fid)
        assert f.status is FindingStatus.ACCEPTED and f.execution["item_id"] == "PVTI_8"
        # The board the item landed on is refetched so it shows on the next load.
        assert app.state.github_cache.forced_projects[-1] == 13

    def test_add_all_runs_every_finding_in_the_repo_and_continues_past_a_failure(
        self, authed_client, monkeypatch
    ):
        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app, fail_numbers=(8,))
        other = _find(app)  # a duplicate in another repo: untouched
        f8 = self._find(app, 8)
        f9 = self._find(app, 9, title="second")
        r = client.post("/issues/hygiene/offboard/add-all?mode=hygiene", data={"repo": self.GADGET})
        assert r.status_code == 200 and 'data-mode="hygiene"' in r.text
        assert [c[:2] for c in calls] == [("add", 13), ("add", 13), ("edit", "PVT_1")]
        store = app.state.hygiene_store
        assert store.get(f8).status is FindingStatus.OPEN
        assert store.get(f8).execution["ok"] is False and "item-add boom" in r.text
        assert store.get(f9).status is FindingStatus.ACCEPTED
        assert store.get(other).status is FindingStatus.OPEN and store.get(other).execution is None
        assert app.state.github_cache.forced_projects.count(13) == 1
        assert "Add all (1)" in r.text  # #8 is still there to retry

    def test_add_all_skips_a_finding_with_nothing_actionable(self, authed_client, monkeypatch):
        """Review round 1: an issue already on the board (added by hand)
        plans to a non-actionable step — Add all must not run it and store
        a "failed" log on the card; the next sweep resolves it."""
        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app)
        fid = self._find(app, 8)
        # The fixture board (agent-hq) is the fresh list; put gadget#8 on it
        # WITH a status so the plan is "already on the board".
        app.state.github_cache._issues.append(
            _mk_issue(8, repo=self.GADGET, title="on board already", status="Todo")
        )
        r = client.post("/issues/hygiene/offboard/add-all?mode=hygiene", data={"repo": self.GADGET})
        assert r.status_code == 200 and calls == []
        f = app.state.hygiene_store.get(fid)
        assert f.status is FindingStatus.OPEN and f.execution is None
        assert "Last attempt failed" not in r.text

    def test_add_all_claims_against_the_findings_current_revision(self, authed_client, monkeypatch):
        """Review round 2: a sweep finishing mid-loop refreshes found_at on
        every unclaimed off-board finding; a claim pinned to the snapshot
        taken at the start would then refuse silently. Re-read before
        claiming."""
        from agent_hq.web.routes import issues as issues_routes

        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app)
        fid = self._find(app, 8)
        real_snapshot = issues_routes._hygiene_snapshot

        async def stale_snapshot(app_):
            snap = await real_snapshot(app_)
            import copy as _copy

            findings = []
            for f in snap.findings:
                f = _copy.deepcopy(f)
                f.found_at = "2020-01-01T00:00:00+00:00"  # the paint is out of date
                findings.append(f)
            return issues_routes.HygieneSnapshot(tuple(findings), snap.last_swept_at)

        monkeypatch.setattr(issues_routes, "_hygiene_snapshot", stale_snapshot)
        r = client.post("/issues/hygiene/offboard/add-all?mode=hygiene", data={"repo": self.GADGET})
        assert r.status_code == 200
        assert [c[0] for c in calls] == ["add", "edit"]
        assert app.state.hygiene_store.get(fid).status is FindingStatus.ACCEPTED

    def test_add_all_isolates_a_planning_failure(self, authed_client, monkeypatch):
        """Review round 5: a plan that raises for one finding must not 500
        the route after earlier findings were added — the board refresh
        would be skipped and the added issues invisible until the TTL."""
        from agent_hq.web.routes import issues as issues_routes

        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app)
        f8 = self._find(app, 8)
        f9 = self._find(app, 9, title="second")
        real_plan = issues_routes.plan_steps

        def flaky(finding, **kw):
            if finding.issue_keys[0].endswith("#9"):
                raise RuntimeError("malformed action")
            return real_plan(finding, **kw)

        monkeypatch.setattr(issues_routes, "plan_steps", flaky)
        r = client.post("/issues/hygiene/offboard/add-all?mode=hygiene", data={"repo": self.GADGET})
        assert r.status_code == 200
        assert [c[0] for c in calls] == ["add", "edit"]
        store = app.state.hygiene_store
        assert store.get(f8).status is FindingStatus.ACCEPTED
        assert store.get(f9).status is FindingStatus.OPEN and store.get(f9).execution is None
        assert app.state.github_cache.forced_projects.count(13) == 1

    def test_repo_chip_filter_matches_off_board_cards_case_insensitively(self, authed_client):
        """Review round 10: chip values carry GitHub's repo case; a finding
        keyed with the config's spelling must still show under its chip."""
        from agent_hq.web.routes.issues import HygieneSnapshot, _hygiene_sections
        from agent_hq.web.state import DispatchState

        _, app, _ = authed_client
        fid = self._find(app, 8)
        finding = app.state.hygiene_store.get(fid)
        finding.issue_keys = ["Ben-Ralston/Gadget#8"]
        snap = HygieneSnapshot((finding,), None)
        sections = _hygiene_sections(snap, DispatchState(repos=[self.GADGET]))
        assert [s["type"] for s in sections] == ["offboard"]

    def test_add_all_with_nothing_to_add_is_a_no_op(self, authed_client, monkeypatch):
        client, app, _ = authed_client
        calls = self._wire(monkeypatch, app)
        r = client.post("/issues/hygiene/offboard/add-all?mode=hygiene", data={"repo": self.GADGET})
        assert r.status_code == 200 and calls == []


class TestFetchFailureSkipsDestructivePruning:
    """#179 (AHQ-002/017): a recorded GitHub fetch failure means a missing
    issue or chip is unknown, not gone — the web layer must not prune the
    user's filters or silently drop their selection on it."""

    def test_failed_fetch_keeps_filter_whose_chip_vanished(self, authed_client):
        client, app, tm = authed_client
        # The configured board has a recorded failure: a repo whose issues
        # failed cold would be absent from an otherwise non-empty list.
        app.state.github_cache.failed = {"ben-ralston/1"}
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/other-repo"]))
        r = client.get("/issues/container?mode=browse")
        assert r.status_code == 200
        assert _read_state(client, tm).repos == ["ben-ralston/other-repo"]

    def test_healthy_fetch_still_prunes(self, authed_client):
        client, app, tm = authed_client
        app.state.github_cache.failed = set()
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/other-repo"]))
        client.get("/issues/container?mode=browse")
        assert _read_state(client, tm).repos == []

    def test_failed_fetch_keeps_unresolved_selection_and_defers_dispatch(
        self, authed_client, monkeypatch
    ):
        client, app, tm = authed_client
        app.state.github_cache.failed = {"ben-ralston/1"}
        selected = ["ben-ralston/agent-hq#67", "ben-ralston/other-repo#5"]
        _set_state(client, app, tm, DispatchState(selected=list(selected)))
        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(batches)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        r = client.post("/issues/dispatch?mode=browse")
        assert r.status_code == 200
        assert captured == []
        assert _read_state(client, tm).selected == selected

    def test_failed_fetch_with_all_keys_resolved_still_dispatches(self, authed_client, monkeypatch):
        client, app, tm = authed_client
        app.state.github_cache.failed = {"ben-ralston/1"}
        _set_state(client, app, tm, DispatchState(selected=["ben-ralston/agent-hq#67"]))
        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(batches)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        client.post("/issues/dispatch?mode=browse")
        assert len(captured) == 1
        assert _read_state(client, tm).selected == []

    def test_closed_keys_failure_alone_still_prunes(self, authed_client):
        """A failed ``closed:`` lookup never removes issues from the list
        (review round 1) — only a failed BOARD makes a missing chip unknown."""
        client, app, tm = authed_client
        app.state.github_cache.failed = {"closed:ben-ralston/agent-hq"}
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/other-repo"]))
        client.get("/issues/container?mode=browse")
        assert _read_state(client, tm).repos == []

    def test_closed_keys_failure_alone_does_not_defer_dispatch(self, authed_client, monkeypatch):
        client, app, tm = authed_client
        app.state.github_cache.failed = {"closed:ben-ralston/agent-hq"}
        _set_state(
            client,
            app,
            tm,
            DispatchState(selected=["ben-ralston/agent-hq#67", "ben-ralston/agent-hq#999"]),
        )
        captured: list = []

        def fake_dispatch(batches, config, **kwargs):
            captured.append(batches)
            from agent_hq.dispatch import DispatchResult

            return DispatchResult(success=True, branch="x")

        from agent_hq.web.routes import issues as issues_routes

        monkeypatch.setattr(issues_routes, "dispatch_via_claude", fake_dispatch)
        client.post("/issues/dispatch?mode=browse")
        assert len(captured) == 1
        assert _read_state(client, tm).selected == []

    def test_failure_of_unconfigured_board_is_ignored(self, authed_client):
        client, app, tm = authed_client
        # A lingering failure for a board no longer in config.
        app.state.github_cache.failed = {"someone-else/9"}
        _set_state(client, app, tm, DispatchState(repos=["ben-ralston/other-repo"]))
        client.get("/issues/container?mode=browse")
        assert _read_state(client, tm).repos == []
