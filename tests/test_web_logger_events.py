"""Web-process unified-logger events (#231).

Named ``test_web_logger_events.py`` (not ``test_web_events.py``) — that
filename is already taken by the pre-existing SSE-broker test suite.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, derive_cookie_value
from agent_hq.web.refresh import BackgroundRefresher


@pytest.fixture()
def app(tmp_path) -> FastAPI:
    token_file = tmp_path / "web-token"
    return create_app(token_file=token_file, config=None, enable_watcher=False)


def _client(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(app.state.token_manager))
    return client


def test_unhandled_route_exception_emits_error(app):
    @app.get("/boom")
    async def boom():
        raise RuntimeError("route exploded")

    with patch("agent_hq.web.app.eventlog.emit") as emit:
        client = _client(app)
        response = client.get("/boom")
    assert response.status_code == 500
    calls = [c for c in emit.call_args_list if c[0][1] == "web unhandled exception"]
    assert len(calls) == 1
    level, _message, extra = calls[0][0][:3]
    assert level == "error"
    assert extra["path"] == "/boom"
    assert extra["method"] == "GET"
    assert "route exploded" in extra["error"]
    assert extra["request_id"]


def test_http_404_does_not_emit(app):
    with patch("agent_hq.web.app.eventlog.emit") as emit:
        client = _client(app)
        client.get("/definitely-not-a-route")
    assert not [c for c in emit.call_args_list if c[0][1] == "web unhandled exception"]


def test_successful_response_still_carries_request_id_header(app):
    client = _client(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers.get("x-request-id")


def test_config_load_failure_emits_warn(tmp_path):
    from agent_hq.config import ConfigError

    with (
        patch("agent_hq.web.app.load_config", side_effect=ConfigError("bad yaml")),
        patch("agent_hq.web.app.eventlog.emit") as emit,
    ):
        create_app(token_file=tmp_path / "web-token", enable_watcher=False)
    calls = [c for c in emit.call_args_list if c[0][1] == "config load failed"]
    assert len(calls) == 1
    assert calls[0][0][0] == "warn"
    assert "bad yaml" in calls[0][0][2]["error"]


def test_background_refresher_failure_emits_warn():
    async def scenario():
        refresher = BackgroundRefresher("issues")

        async def failing():
            raise RuntimeError("gh down")

        with patch("agent_hq.web.refresh.eventlog.emit") as emit:
            refresher.kick(failing)
            await asyncio.sleep(0.05)
        return emit

    emit = asyncio.run(scenario())
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "background refresh failed")
    assert extra["name"] == "issues"
    assert "gh down" in extra["error"]
    assert emit.call_args[1] == {"min_interval_s": 600}


def test_cache_schema_mismatch_emits_warn(tmp_path):
    from agent_hq.web import watcher as watcher_mod

    cache_file = tmp_path / "cache.json"
    cache_file.write_text(
        json.dumps({"schema_version": -1, "agents": [], "timestamp": time.time()})
    )
    with patch("agent_hq.web.watcher.eventlog.emit") as emit:
        agents = watcher_mod._load_agents_defensively(max_age=None, cache_file=cache_file)
    assert agents == []
    emit.assert_called_once()
    level, message, extra = emit.call_args[0][:3]
    assert (level, message) == ("warn", "agent cache schema mismatch")
    assert extra["found"] == -1


def test_cache_missing_schema_version_does_not_emit(tmp_path):
    """A v1/pre-upgrade cache file has no ``schema_version`` field at all —
    this is not a mismatch worth alerting on, just an old cache to reject
    silently (#231 fix-wave: distinguish absent from present-but-different)."""
    from agent_hq.web import watcher as watcher_mod

    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"agents": [], "timestamp": time.time()}))
    with patch("agent_hq.web.watcher.eventlog.emit") as emit:
        agents = watcher_mod._load_agents_defensively(max_age=None, cache_file=cache_file)
    assert agents == []
    emit.assert_not_called()
