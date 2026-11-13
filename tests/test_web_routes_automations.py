"""Tests for the automations health section + log drill-down routes (#54)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent_hq.automations import AutomationHealth, AutomationsCache, AutomationStatus
from agent_hq.config import AutomationConfig, Config
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _mk_config(tmp_path, names=("nightly-backup",)):
    return Config(
        repos={},
        projects=[],
        worktree_root=tmp_path,
        automations=[
            AutomationConfig(
                name=name,
                label=f"com.claude.{name}",
                log_file=tmp_path / f"{name}.log",
            )
            for name in names
        ],
    )


def _mk_health(name, status, **kwargs):
    defaults = dict(
        label=f"com.claude.{name}",
        log_file=Path(f"/tmp/{name}.log"),
        schedule_desc="daily 06:00",
        last_run=datetime(2026, 6, 10, 6, 5),
        last_exit_code=0,
        last_log_line="run done",
        next_run=datetime(2026, 6, 11, 6, 0),
    )
    defaults.update(kwargs)
    return AutomationHealth(name=name, status=status, **defaults)


class _StubCache:
    """Pre-seeded stand-in for AutomationsCache: never calls the loader.
    Reports its data as fresh so the SWR path (#226) serves it directly."""

    def __init__(self, healths):
        self._healths = healths

    def get_cached(self):
        return list(self._healths), True

    def fetch(self, loader, force=False):
        return self._healths


def _make_client(tmp_path, config, cache):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file, config=config, automations_cache=cache)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


class TestInjection:
    def test_injected_cache_lands_on_app_state(self, tmp_path):
        cache = AutomationsCache()
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=None, automations_cache=cache)
        assert app.state.automations_cache is cache

    def test_default_cache_constructed_when_not_injected(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=None)
        assert isinstance(app.state.automations_cache, AutomationsCache)


class TestAutomationsSection:
    def test_one_row_per_automation_with_status_dots(self, tmp_path):
        config = _mk_config(tmp_path, names=("nightly-backup", "daily-digest"))
        cache = _StubCache(
            [
                _mk_health("nightly-backup", AutomationStatus.OK),
                _mk_health(
                    "daily-digest",
                    AutomationStatus.ERROR,
                    last_exit_code=78,
                    last_log_line="claude --print exit code: 78",
                ),
            ]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert r.status_code == 200
        assert "nightly-backup" in r.text
        assert "daily-digest" in r.text
        assert "automation-dot--ok" in r.text
        assert "automation-dot--error" in r.text

    def test_error_row_shows_last_log_line(self, tmp_path):
        config = _mk_config(tmp_path)
        cache = _StubCache(
            [
                _mk_health(
                    "nightly-backup",
                    AutomationStatus.ERROR,
                    last_exit_code=1,
                    last_log_line="something exploded",
                )
            ]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert "something exploded" in r.text

    def test_ok_row_omits_last_log_line(self, tmp_path):
        config = _mk_config(tmp_path)
        cache = _StubCache(
            [_mk_health("nightly-backup", AutomationStatus.OK, last_log_line="run done")]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert "run done" not in r.text

    def test_missed_row_shows_status_and_dot(self, tmp_path):
        config = _mk_config(tmp_path)
        cache = _StubCache(
            [
                _mk_health(
                    "nightly-backup",
                    AutomationStatus.MISSED,
                    last_run=datetime(2026, 6, 8, 17, 35),
                )
            ]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert "automation-dot--missed" in r.text
        assert "missed" in r.text

    def test_daemon_rows_show_running_state_not_next_run(self, tmp_path):
        config = _mk_config(tmp_path, names=("sync-agent", "worker"))
        cache = _StubCache(
            [
                _mk_health(
                    "sync-agent",
                    AutomationStatus.OK,
                    running=True,
                    next_run=None,
                    schedule_desc=None,
                ),
                _mk_health(
                    "worker",
                    AutomationStatus.STOPPED,
                    running=False,
                    next_run=None,
                    schedule_desc=None,
                    last_log_line="stopping (SIGTERM)",
                ),
            ]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert "· running" in r.text
        assert "· not running" in r.text
        assert "automation-dot--stopped" in r.text
        assert "stopping (SIGTERM)" in r.text

    def test_stale_row_shows_dot_and_last_line(self, tmp_path):
        config = _mk_config(tmp_path, names=("sync-agent",))
        cache = _StubCache(
            [
                _mk_health(
                    "sync-agent",
                    AutomationStatus.STALE,
                    running=True,
                    next_run=None,
                    last_log_line="heartbeat ok",
                )
            ]
        )
        client = _make_client(tmp_path, config, cache)
        r = client.get("/automations/section")
        assert "automation-dot--stale" in r.text
        assert "heartbeat ok" in r.text

    def test_unconfigured_returns_empty_section(self, tmp_path):
        client = _make_client(tmp_path, None, _StubCache([]))
        r = client.get("/automations/section")
        assert r.status_code == 200
        assert "automation-row" not in r.text

    def test_requires_auth(self, tmp_path):
        config = _mk_config(tmp_path)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=config, automations_cache=_StubCache([]))
        client = TestClient(app)
        r = client.get("/automations/section", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert r.headers["location"] == "/login"


class TestAgentsPageNoLongerEmbedsSection:
    """Automation health moved to its own /automations tab; the agents page
    must not embed it anymore (configured or not)."""

    def test_agents_page_omits_section_when_configured(self, tmp_path):
        config = _mk_config(tmp_path)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/agents")
        assert "/automations/section" not in r.text

    def test_agents_page_omits_section_when_unconfigured(self, tmp_path):
        client = _make_client(tmp_path, None, _StubCache([]))
        r = client.get("/agents")
        assert "/automations/section" not in r.text


class TestAutomationsPage:
    def test_page_hosts_section_when_configured(self, tmp_path):
        config = _mk_config(tmp_path)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations")
        assert r.status_code == 200
        # Loads the section partial via htmx, and hosts the drill-down slot.
        assert 'hx-get="/automations/section"' in r.text
        assert 'id="detail-slot"' in r.text

    def test_page_renders_when_unconfigured(self, tmp_path):
        client = _make_client(tmp_path, None, _StubCache([]))
        r = client.get("/automations")
        assert r.status_code == 200
        # Nothing to poll for when no automations are configured.
        assert 'hx-get="/automations/section"' not in r.text

    def test_page_marks_its_nav_tab_active(self, tmp_path):
        config = _mk_config(tmp_path)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations")
        assert 'tab-nav__item--active" href="/automations"' in r.text

    def test_page_requires_auth(self, tmp_path):
        config = _mk_config(tmp_path)
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(token_file=tm.token_file, config=config, automations_cache=_StubCache([]))
        client = TestClient(app)
        r = client.get("/automations", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert r.headers["location"] == "/login"


class TestNav:
    def test_automations_tab_present_on_other_pages(self, tmp_path):
        config = _mk_config(tmp_path)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/agents")
        assert 'href="/automations"' in r.text
        assert ">Automations<" in r.text


class TestAutomationLogs:
    def test_logs_partial_renders_ansi_tail(self, tmp_path):
        config = _mk_config(tmp_path)
        log_file = tmp_path / "nightly-backup.log"
        log_file.write_text("plain line\n\x1b[31mred line\x1b[0m\n")
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations/nightly-backup/logs")
        assert r.status_code == 200
        assert "plain line" in r.text
        assert "red line" in r.text
        assert "\x1b" not in r.text  # ANSI converted, not raw

    def test_unknown_name_404s(self, tmp_path):
        config = _mk_config(tmp_path)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations/nope/logs")
        assert r.status_code == 404

    def test_logs_strip_osc_sequences(self, tmp_path):
        config = _mk_config(tmp_path)
        log_file = tmp_path / "nightly-backup.log"
        log_file.write_text("\x1b]8;;javascript:alert(1)\x07evil link\x1b]8;;\x07\n")
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations/nightly-backup/logs")
        assert "javascript:" not in r.text


class TestLogsScrollContract:
    def test_logs_partial_uses_pane_preview_host_scroller(self, tmp_path):
        """The sheet/pane wrappers are non-scrolling flex columns whose ONLY
        scroll container is .pane-preview-host (layout.css contract). Without
        it the 50-line log overflows the fixed 80vh sheet and nothing inside
        is scrollable — page scroll appears dead (the owner's live report)."""
        config = _mk_config(tmp_path)
        (tmp_path / "nightly-backup.log").write_text("line\n" * 50)
        client = _make_client(tmp_path, config, _StubCache([]))
        r = client.get("/automations/nightly-backup/logs")
        assert 'class="pane-preview-host"' in r.text
        assert 'class="detail-body__top"' in r.text


class _SwrStubCache:
    """AutomationsCache stand-in with SWR knobs (#226): ``get_cached``
    reports (healths, fresh) or None; ``fetch`` records force flags."""

    def __init__(self, healths, *, fresh=True, cold=False):
        self._healths = healths
        self._fresh = fresh
        self._cold = cold
        self.fetch_calls: list[bool] = []

    def get_cached(self):
        if self._cold:
            return None
        return list(self._healths), self._fresh

    def fetch(self, loader, force=False):
        self.fetch_calls.append(force)
        self._fresh = True
        self._cold = False
        self._healths = loader()
        return self._healths


class TestAutomationsServeStale:
    """#226: TTL(30s) == the page's poll interval, so every poll missed the
    cache and blocked ~100ms on launchctl/plist/log work. The section now
    serves whatever the cache holds instantly and refreshes behind; the
    existing 30s poll picks up the fresh data."""

    def _wait_refresh_done(self, app, timeout: float = 5.0) -> None:
        import time as _time

        deadline = _time.time() + timeout
        while app.state.automations_refresher.in_flight:
            if _time.time() > deadline:
                raise AssertionError("background refresh never finished")
            _time.sleep(0.01)

    def _build(self, tmp_path, cache, monkeypatch, fresh_healths):
        from agent_hq.web.routes import automations as automations_routes

        monkeypatch.setattr(
            automations_routes, "collect_health", lambda configs, now, uid: fresh_healths
        )
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        app = create_app(
            token_file=tm.token_file, config=_mk_config(tmp_path), automations_cache=cache
        )
        return app, tm

    def test_section_serves_stale_instantly_and_kicks_refresh(self, tmp_path, monkeypatch):
        stale = [_mk_health("nightly-backup", AutomationStatus.OK, last_log_line="old run")]
        fresh = [_mk_health("nightly-backup", AutomationStatus.ERROR, last_log_line="new run")]
        cache = _SwrStubCache(stale, fresh=False)
        app, tm = self._build(tmp_path, cache, monkeypatch, fresh)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations/section")
            assert r.status_code == 200
            assert "nightly-backup" in r.text
            self._wait_refresh_done(app)
        # The request served the cached healths; the loader ran only in the
        # background refresh, forced.
        assert cache.fetch_calls == [True]

    def test_section_cold_blocks(self, tmp_path, monkeypatch):
        fresh = [_mk_health("nightly-backup", AutomationStatus.OK)]
        cache = _SwrStubCache([], cold=True)
        app, tm = self._build(tmp_path, cache, monkeypatch, fresh)
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations/section")
            assert r.status_code == 200
            assert "nightly-backup" in r.text
        assert cache.fetch_calls == [False]
        assert not app.state.automations_refresher.in_flight

    def test_section_stale_shows_indicator(self, tmp_path, monkeypatch):
        """Review fix (#226): a stale serve must carry the same "Updating…"
        signal the dispatch container shows — otherwise hours-old
        health renders with no staleness cue until the next 30s poll."""
        cache = _SwrStubCache([_mk_health("nightly-backup", AutomationStatus.OK)], fresh=False)
        app, tm = self._build(tmp_path, cache, monkeypatch, [])
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations/section")
            assert r.status_code == 200
            assert "refresh-indicator" in r.text
            self._wait_refresh_done(app)

    def test_section_fresh_shows_no_indicator(self, tmp_path, monkeypatch):
        cache = _SwrStubCache([_mk_health("nightly-backup", AutomationStatus.OK)], fresh=True)
        app, tm = self._build(tmp_path, cache, monkeypatch, [])
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations/section")
            assert "refresh-indicator" not in r.text

    def test_section_fresh_no_kick(self, tmp_path, monkeypatch):
        cache = _SwrStubCache([_mk_health("nightly-backup", AutomationStatus.OK)], fresh=True)
        app, tm = self._build(tmp_path, cache, monkeypatch, [])
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations/section")
            assert r.status_code == 200
        assert cache.fetch_calls == []
        assert not app.state.automations_refresher.in_flight

    def test_page_inline_when_cache_warm(self, tmp_path, monkeypatch):
        cache = _SwrStubCache([_mk_health("nightly-backup", AutomationStatus.OK)], fresh=True)
        app, tm = self._build(tmp_path, cache, monkeypatch, [])
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations")
            assert r.status_code == 200
            assert "nightly-backup" in r.text
            assert "Loading automations…" not in r.text
            assert 'hx-trigger="every 30s"' in r.text

    def test_page_shell_when_cold(self, tmp_path, monkeypatch):
        cache = _SwrStubCache([], cold=True)
        app, tm = self._build(tmp_path, cache, monkeypatch, [])
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r = client.get("/automations")
            assert r.status_code == 200
            assert "Loading automations…" in r.text
            assert 'hx-trigger="load, every 30s"' in r.text


def test_a_signal_kill_shows_the_signal_on_the_row(tmp_path):
    """#427: no exit code to show, so the row names the signal."""
    config = _mk_config(tmp_path)
    cache = _StubCache(
        [_mk_health("nightly-backup", AutomationStatus.ERROR, last_exit_code=None, last_signal=9)]
    )
    r = _make_client(tmp_path, config, cache).get("/automations/section")
    assert "error (SIGKILL)" in r.text
