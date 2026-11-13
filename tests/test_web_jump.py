"""Tests for the desktop jump route + the ``jump.py`` helper.

Mobile uses the deep-link in detail templates and never hits this route.
Desktop POSTs ``/agents/{slug}/jump`` to run ``tmux switch-client`` and
then bring Ghostty (the only terminal we use) forward via AppleScript.
AppleScript is best-effort — we always toast success once tmux
switched.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from agent_hq.cache import SCHEMA_VERSION
from agent_hq.config import Config, WebConfig
from agent_hq.models import Agent, AgentState, AgentType
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _mk_agent(pane_id: str = "%5") -> Agent:
    return Agent(
        pane_id=pane_id,
        session_name="ses",
        window_index=0,
        pane_index=0,
        agent_type=AgentType.CLAUDE,
        cwd="/tmp",
        repo_name="repo",
        branch="main",
        state=AgentState.IDLE,
    )


def _make_authed_client(tmp_path, monkeypatch, config=None):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "producer": "daemon",
                "producer_epoch": time.time(),
                "sequence": 1,
                "timestamp": time.time(),
                "monotonic_ns": time.monotonic_ns(),
                "pid": 1,
                "agents": [_mk_agent("%5").to_dict()],
            }
        )
    )
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
    overrides_path = tmp_path / "overrides.json"
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_path)
    inactive_path = tmp_path / "inactive.json"
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", inactive_path)

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file, config=config)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    return _make_authed_client(tmp_path, monkeypatch)


def _mk_config(tmp_path, web: WebConfig) -> Config:
    return Config(repos={}, projects=[], worktree_root=tmp_path, web=web)


class TestIosJumpUrl:
    """``create_app`` derives ``app.state.ios_jump_url`` from config. #137"""

    def _app(self, tmp_path, **kwargs):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        return create_app(token_file=tm.token_file, **kwargs)

    def test_default_url_without_config(self, tmp_path):
        """No projects.yaml at all still yields the default Shortcut URL."""
        app = self._app(tmp_path, config=None)
        assert app.state.ios_jump_url == "shortcuts://run-shortcut?name=Agent%20HQ%20Terminal"

    def test_custom_name_is_url_encoded(self, tmp_path):
        cfg = _mk_config(tmp_path, WebConfig(ios_jump_shortcut="Open Blink & Attach"))
        app = self._app(tmp_path, config=cfg)
        assert app.state.ios_jump_url == (
            "shortcuts://run-shortcut?name=Open%20Blink%20%26%20Attach"
        )

    def test_disabled_when_shortcut_none(self, tmp_path):
        cfg = _mk_config(tmp_path, WebConfig(ios_jump_shortcut=None))
        app = self._app(tmp_path, config=cfg)
        assert app.state.ios_jump_url is None


class TestIosJumpTemplates:
    """Markup for the iOS jump branch (#137): body data attr + button."""

    def test_body_carries_ios_jump_url(self, tmp_path, monkeypatch):
        client = _make_authed_client(tmp_path, monkeypatch)
        r = client.get("/agents")
        assert 'data-ios-jump-url="shortcuts://run-shortcut?name=Agent%20HQ%20Terminal"' in r.text

    def test_body_attr_absent_when_disabled(self, tmp_path, monkeypatch):
        cfg = _mk_config(tmp_path, WebConfig(ios_jump_shortcut=None))
        client = _make_authed_client(tmp_path, monkeypatch, config=cfg)
        r = client.get("/agents")
        assert "data-ios-jump-url" not in r.text

    def test_terminal_button_marked_and_sends_focus(self, authed_client):
        r = authed_client.get("/agents/5")
        assert r.status_code == 200
        assert "data-jump-terminal" in r.text
        assert 'js:{focus: window.agentHQIsIOS ? "0" : "1"}' in r.text


def _make_client_for_agent(tmp_path, monkeypatch, agent: Agent, config=None):
    """Like ``_make_authed_client`` but injects an arbitrary agent instead
    of the fixed Claude/pane-5 default, so native_uri variants can be
    exercised without cloning the whole harness."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "producer": "daemon",
                "producer_epoch": time.time(),
                "sequence": 1,
                "timestamp": time.time(),
                "monotonic_ns": time.monotonic_ns(),
                "pid": 1,
                "agents": [agent.to_dict()],
            }
        )
    )
    monkeypatch.setattr("agent_hq.web.watcher.CACHE_FILE", cache_path)
    overrides_path = tmp_path / "overrides.json"
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", overrides_path)
    inactive_path = tmp_path / "inactive.json"
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", inactive_path)

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file, config=config)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


class TestNativeUriJumpButton:
    """The detail sheet's native-UI button is harness-aware (#205 Task 18):
    driven by ``agent.session_metadata.native_uri`` instead of the old
    unconditional claude.ai fallback, and hidden entirely when there's no
    URI to jump to."""

    def test_claude_agent_keeps_claude_url(self, tmp_path, monkeypatch):
        agent = Agent(
            pane_id="%5",
            session_name="ses",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CLAUDE,
            cwd="/tmp",
            repo_name="repo",
            branch="main",
            state=AgentState.IDLE,
            claude_session_id="b1",
        )
        client = _make_client_for_agent(tmp_path, monkeypatch, agent)
        r = client.get("/agents/5")
        assert r.status_code == 200
        assert "https://claude.ai/code/b1" in r.text
        assert "⏎ Jump to Web" in r.text

    def test_codex_agent_gets_deep_link(self, tmp_path, monkeypatch):
        agent = Agent(
            pane_id="%5",
            session_name="ses",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/tmp",
            repo_name="repo",
            branch="main",
            state=AgentState.IDLE,
            codex_thread_id="u7",
        )
        client = _make_client_for_agent(tmp_path, monkeypatch, agent)
        r = client.get("/agents/5")
        assert r.status_code == 200
        assert "codex://threads/u7" in r.text
        assert "⏎ Open in Codex" in r.text
        assert "claude.ai" not in r.text

    def test_no_uri_hides_button(self, tmp_path, monkeypatch):
        agent = Agent(
            pane_id="%5",
            session_name="ses",
            window_index=0,
            pane_index=0,
            agent_type=AgentType.CODEX,
            cwd="/tmp",
            repo_name="repo",
            branch="main",
            state=AgentState.IDLE,
            codex_thread_id=None,
        )
        client = _make_client_for_agent(tmp_path, monkeypatch, agent)
        r = client.get("/agents/5")
        assert r.status_code == 200
        assert "claude.ai" not in r.text
        assert "codex://threads/" not in r.text
        assert "⏎ Jump to Web" not in r.text
        assert "⏎ Open in Codex" not in r.text


class TestJumpHelper:
    """``jump.perform_jump(pane_id)`` runs tmux switch-client + AppleScript."""

    def test_runs_tmux_switch(self, monkeypatch):
        from agent_hq.web import jump

        called = []

        def fake_jump(pane_id):
            called.append(pane_id)
            return True

        monkeypatch.setattr(jump, "_jump_to_pane", fake_jump)
        monkeypatch.setattr(jump, "_focus_terminal", lambda: True)
        ok = jump.perform_jump("%5")
        assert ok is True
        assert called == ["%5"]

    def test_returns_false_when_tmux_fails(self, monkeypatch):
        from agent_hq.web import jump

        monkeypatch.setattr(jump, "_jump_to_pane", lambda pid: False)
        monkeypatch.setattr(jump, "_focus_terminal", lambda: True)
        assert jump.perform_jump("%5") is False

    def test_focus_terminal_failure_does_not_fail_jump(self, monkeypatch):
        """tmux succeeded; AppleScript focus is best-effort, so a focus
        failure must NOT flip the overall return to False."""
        from agent_hq.web import jump

        monkeypatch.setattr(jump, "_jump_to_pane", lambda pid: True)
        monkeypatch.setattr(jump, "_focus_terminal", lambda: False)
        assert jump.perform_jump("%5") is True

    def test_focus_skipped_when_focus_terminal_false(self, monkeypatch):
        """iOS-triggered jumps (#137) switch the pane but must not raise
        Ghostty on the Mac."""
        from agent_hq.web import jump

        focus_calls = []
        monkeypatch.setattr(jump, "_jump_to_pane", lambda pid: True)
        monkeypatch.setattr(jump, "_focus_terminal", lambda: focus_calls.append(1) or True)
        assert jump.perform_jump("%5", focus_terminal=False) is True
        assert focus_calls == []

    def test_focus_runs_by_default(self, monkeypatch):
        from agent_hq.web import jump

        focus_calls = []
        monkeypatch.setattr(jump, "_jump_to_pane", lambda pid: True)
        monkeypatch.setattr(jump, "_focus_terminal", lambda: focus_calls.append(1) or True)
        assert jump.perform_jump("%5") is True
        assert focus_calls == [1]

    def test_focus_terminal_only_targets_ghostty(self, monkeypatch):
        """We only use Ghostty. ``_focus_terminal`` must invoke osascript
        for Ghostty and nothing else — AppleScript ``activate`` *launches*
        an app that isn't running, so a fall-through to iTerm/Terminal
        would cold-launch terminals the user never opened."""
        from agent_hq.web import jump

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(jump.subprocess, "run", fake_run)
        result = jump._focus_terminal()
        assert result is True
        assert len(calls) == 1
        joined = " ".join(calls[0])
        assert "Ghostty" in joined
        assert "iTerm" not in joined
        assert "Terminal" not in joined

    def test_focus_terminal_never_falls_back_when_ghostty_fails(self, monkeypatch):
        """Even if Ghostty's activate returns non-zero, we never fall back
        to iTerm or Terminal — no other terminal is ever osascript'd."""
        from agent_hq.web import jump

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            # Ghostty activate fails — but there must be no fallback.
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(jump.subprocess, "run", fake_run)
        result = jump._focus_terminal()
        assert result is False
        assert len(calls) == 1
        joined_calls = [" ".join(c) for c in calls]
        assert all("iTerm" not in j for j in joined_calls)
        assert all("Terminal" not in j for j in joined_calls)


class TestJumpRoute:
    def test_jump_runs_perform_jump(self, authed_client, monkeypatch):
        called = []
        from agent_hq.web import jump

        def fake_perform_jump(pane_id, focus_terminal=True):
            called.append(pane_id)
            return True

        monkeypatch.setattr(jump, "perform_jump", fake_perform_jump)
        r = authed_client.post("/agents/5/jump")
        assert r.status_code == 200
        assert called == ["%5"]
        assert "jumped" in r.text.lower() or "jump" in r.text.lower()

    def test_jump_failure_returns_error_toast(self, authed_client, monkeypatch):
        from agent_hq.web import jump

        monkeypatch.setattr(jump, "perform_jump", lambda pane_id, focus_terminal=True: False)
        r = authed_client.post("/agents/5/jump")
        assert r.status_code == 200
        # Toast indicates failure
        assert (
            "could not" in r.text.lower() or "fail" in r.text.lower() or "error" in r.text.lower()
        )

    def test_jump_404_on_unknown_slug(self, authed_client):
        r = authed_client.post("/agents/999/jump")
        assert r.status_code == 404

    def test_jump_focus_0_skips_terminal_focus(self, authed_client, monkeypatch):
        """The iOS button posts ``focus=0`` (#137): pane switch still runs,
        Ghostty focus is skipped."""
        from agent_hq.web import jump

        called = []

        def fake_perform_jump(pane_id, focus_terminal=True):
            called.append((pane_id, focus_terminal))
            return True

        monkeypatch.setattr(jump, "perform_jump", fake_perform_jump)
        r = authed_client.post("/agents/5/jump", data={"focus": "0"})
        assert r.status_code == 200
        assert called == [("%5", False)]

    def test_jump_without_focus_field_keeps_terminal_focus(self, authed_client, monkeypatch):
        """Desktop path unchanged: no ``focus`` field → focus_terminal=True."""
        from agent_hq.web import jump

        called = []

        def fake_perform_jump(pane_id, focus_terminal=True):
            called.append((pane_id, focus_terminal))
            return True

        monkeypatch.setattr(jump, "perform_jump", fake_perform_jump)
        r = authed_client.post("/agents/5/jump")
        assert r.status_code == 200
        assert called == [("%5", True)]
