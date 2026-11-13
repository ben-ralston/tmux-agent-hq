"""Tests for the system surface (#215): /healthz boot id + POST /system/restart.

Route tests ALWAYS monkeypatch the agent_hq.restart boundary functions —
the isolation conftest redirects real paths but does not intercept
launchctl or the agent-hq CLI, so an unmocked POST here would bounce the
live web service (#212 class).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_hq import restart
from agent_hq.restart import RestartStep
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value
from agent_hq.web.routes import system as system_routes


def _make_app(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    return create_app(token_file=tm.token_file, config=None), tm


def _make_client(tmp_path):
    app, tm = _make_app(tmp_path)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


@pytest.fixture(autouse=True)
def stub_restart(monkeypatch):
    """Neutralize every real-world side effect of the restart route.

    Autouse across this file so no test — present or future — can reach
    launchctl or the agent-hq CLI.
    """
    calls = {"kick": 0}
    monkeypatch.setattr(
        restart,
        "perform_daemon_restart",
        lambda: [
            RestartStep("daemon stop", True, "Daemon stopped (PID 11)"),
            RestartStep("clear snapshot cache", True, "/tmp/x/cache.json"),
            RestartStep("daemon start", True, "Daemon started (PID 12)"),
        ],
    )
    monkeypatch.setattr(restart, "web_service_owns_process", lambda pid: False)
    monkeypatch.setattr(
        restart, "kick_web_service", lambda: calls.__setitem__("kick", calls["kick"] + 1)
    )
    # The post-flush delay is real-time sleep; skip it in tests.
    monkeypatch.setattr(system_routes.time, "sleep", lambda s: None)
    return calls


class TestHealthzBootId:
    def test_healthz_is_ok_and_has_stable_boot_id(self, tmp_path):
        client = _make_client(tmp_path)
        first = client.get("/healthz").json()
        second = client.get("/healthz").json()
        assert first["ok"] is True
        assert first["boot_id"]
        assert first["boot_id"] == second["boot_id"]

    def test_boot_id_differs_across_app_instances(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        app_a, _ = _make_app(tmp_path / "a")
        app_b, _ = _make_app(tmp_path / "b")
        assert app_a.state.boot_id != app_b.state.boot_id

    def test_healthz_needs_no_auth(self, tmp_path):
        app, _tm = _make_app(tmp_path)
        client = TestClient(app)  # no cookie
        r = client.get("/healthz")
        assert r.status_code == 200
        assert "boot_id" in r.json()


class TestSystemRestart:
    def test_requires_auth(self, tmp_path):
        app, _tm = _make_app(tmp_path)
        client = TestClient(app)  # no cookie
        r = client.post("/system/restart", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_renders_steps_and_skipped_note_when_not_launchd(self, tmp_path, stub_restart):
        client = _make_client(tmp_path)
        r = client.post("/system/restart")
        assert r.status_code == 200
        assert "daemon stop" in r.text
        assert "daemon start" in r.text
        assert "not managed by launchd" in r.text
        assert stub_restart["kick"] == 0

    def test_kicks_web_service_when_launchd_owned(self, tmp_path, stub_restart, monkeypatch):
        monkeypatch.setattr(restart, "web_service_owns_process", lambda pid: True)
        client = _make_client(tmp_path)
        r = client.post("/system/restart")
        assert r.status_code == 200
        assert "Restarting web service" in r.text
        # TestClient runs the response's background task before returning.
        assert stub_restart["kick"] == 1

    def test_page_embeds_current_boot_id_for_polling(self, tmp_path, stub_restart, monkeypatch):
        monkeypatch.setattr(restart, "web_service_owns_process", lambda pid: True)
        app, tm = _make_app(tmp_path)
        client = TestClient(app)
        client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
        r = client.post("/system/restart")
        assert app.state.boot_id in r.text
        assert "/healthz" in r.text

    def test_failed_steps_still_render_and_still_kick(self, tmp_path, stub_restart, monkeypatch):
        monkeypatch.setattr(
            restart,
            "perform_daemon_restart",
            lambda: [RestartStep("daemon stop", False, "timed out")],
        )
        monkeypatch.setattr(restart, "web_service_owns_process", lambda pid: True)
        client = _make_client(tmp_path)
        r = client.post("/system/restart")
        assert r.status_code == 200
        assert "timed out" in r.text
        assert stub_restart["kick"] == 1


class TestConcurrentRestarts:
    async def test_overlapping_posts_are_serialized(self, tmp_path, stub_restart, monkeypatch):
        """Two simultaneous POSTs must not interleave their daemon steps
        (Codex review P2): the route holds a lock across the restart work.

        Real concurrency needs one event loop with two in-flight requests
        (TestClient's portal serializes, which would vacuously pass), so
        this drives the ASGI app directly with asyncio.gather.
        """
        import asyncio
        import threading

        from httpx import ASGITransport, AsyncClient

        state = {"active": 0, "max_active": 0}
        gauge = threading.Lock()

        def slow_restart():
            with gauge:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            # NOT time.sleep — the autouse stub no-ops it (kick delay), which
            # would silently erase this overlap window and pass vacuously.
            threading.Event().wait(0.05)
            with gauge:
                state["active"] -= 1
            return [RestartStep("daemon stop", True, "")]

        monkeypatch.setattr(restart, "perform_daemon_restart", slow_restart)
        app, tm = _make_app(tmp_path)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
            r1, r2 = await asyncio.gather(ac.post("/system/restart"), ac.post("/system/restart"))

        assert (r1.status_code, r2.status_code) == (200, 200)
        assert state["max_active"] == 1


class TestSettingsMenuRestartControl:
    def test_settings_menu_offers_two_step_restart(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents")
        assert r.status_code == 200
        # First tap arms the confirm step; the form is what actually posts.
        assert "confirmRestart" in r.text
        assert 'action="/system/restart"' in r.text
        assert "Confirm restart" in r.text
