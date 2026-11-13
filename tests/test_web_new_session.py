"""Tests for the web "+ New session" flow (#169).

``GET /agents/new`` renders the target picker (Scratch + configured
repos) into the overlay slot; ``POST /agents/new`` find-or-creates the
target tmux session, opens a claude window via
``dispatch.open_chat_window``, and jumps to it via ``jump.perform_jump``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_hq.cache import SCHEMA_VERSION
from agent_hq.capabilities import CLAUDE_ONLY, LauncherCapabilities
from agent_hq.config import Config, DispatchConfig, RepoConfig
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


def _mk_config(tmp_path, dispatch: DispatchConfig | None = None) -> Config:
    return Config(
        repos={
            "ben-ralston/agent-hq": RepoConfig(
                owner_name="ben-ralston/agent-hq",
                local_path=Path("/Users/dev/agent-hq"),
            ),
            "ben-ralston/widgets": RepoConfig(
                owner_name="ben-ralston/widgets",
                local_path=Path("/Users/dev/widgets"),
            ),
        },
        projects=[],
        worktree_root=tmp_path,
        dispatch=dispatch or DispatchConfig(),
    )


def _make_authed_client(
    tmp_path,
    monkeypatch,
    config=None,
    capabilities_probe=None,
):
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
    monkeypatch.setattr("agent_hq.overrides.OVERRIDES_FILE", tmp_path / "overrides.json")
    monkeypatch.setattr("agent_hq.state.INACTIVE_FILE", tmp_path / "inactive.json")

    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    kwargs = {}
    if capabilities_probe is not None:
        kwargs["capabilities_probe"] = capabilities_probe
    app = create_app(token_file=tm.token_file, config=config, **kwargs)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


@pytest.fixture
def stub_tmux(monkeypatch):
    """Stub the dispatch/jump seams and record every call."""
    calls: dict[str, list] = {"find_by_name": [], "find_repo": [], "open": [], "jump": []}

    def find_by_name(name):
        calls["find_by_name"].append(name)
        return None

    def find_repo(path):
        calls["find_repo"].append(path)
        return None

    def open_chat_window(*, existing_session, new_session_name, cwd, harness="claude"):
        calls["open"].append(
            {
                "existing_session": existing_session,
                "new_session_name": new_session_name,
                "cwd": cwd,
                "harness": harness,
            }
        )
        return "%9"

    def perform_jump(pane_id, focus_terminal=True):
        calls["jump"].append({"pane_id": pane_id, "focus_terminal": focus_terminal})
        return True

    monkeypatch.setattr("agent_hq.dispatch._find_session_by_name", find_by_name)
    monkeypatch.setattr("agent_hq.dispatch._find_repo_session", find_repo)
    monkeypatch.setattr("agent_hq.dispatch.open_chat_window", open_chat_window)
    monkeypatch.setattr("agent_hq.web.jump.perform_jump", perform_jump)
    return calls


class TestNewSessionPicker:
    def test_picker_lists_scratch_and_repos(self, tmp_path, monkeypatch):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.get("/agents/new")
        assert r.status_code == 200
        assert "Scratch" in r.text
        assert "agent-hq" in r.text
        assert "widgets" in r.text
        assert 'hx-post="/agents/new"' in r.text
        assert "repo:ben-ralston/agent-hq" in r.text

    def test_picker_without_config_lists_only_scratch(self, tmp_path, monkeypatch):
        client = _make_authed_client(tmp_path, monkeypatch, config=None)
        r = client.get("/agents/new")
        assert r.status_code == 200
        assert "Scratch" in r.text
        assert "repo:" not in r.text

    def test_options_carry_ios_focus_and_deep_link_attr(self, tmp_path, monkeypatch):
        """Each option posts the same iOS-aware ``focus`` value the jump
        button uses, and carries ``data-jump-terminal`` so the existing
        shortcuts:// deep-link JS fires on iOS (#137)."""
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.get("/agents/new")
        assert "agentHQIsIOS" in r.text
        assert "data-jump-terminal" in r.text


class TestNewSessionPost:
    def test_scratch_reuses_existing_session(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        monkeypatch.setattr("agent_hq.dispatch._find_session_by_name", lambda name: "scratch")
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert stub_tmux["open"] == [
            {
                "existing_session": "scratch",
                "new_session_name": "scratch",
                "cwd": Path.home(),
                "harness": "claude",
            }
        ]
        assert "scratch" in r.text

    def test_scratch_uses_dispatch_scratch_cwd(self, tmp_path, monkeypatch, stub_tmux):
        cfg = _mk_config(tmp_path, dispatch=DispatchConfig(scratch_cwd=Path("/Users/dev/scratch")))
        client = _make_authed_client(tmp_path, monkeypatch, config=cfg)
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert stub_tmux["open"][0]["cwd"] == Path("/Users/dev/scratch")
        assert stub_tmux["open"][0]["existing_session"] is None
        assert stub_tmux["open"][0]["new_session_name"] == "scratch"

    def test_scratch_without_config_falls_back_to_home(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=None)
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert stub_tmux["open"][0]["cwd"] == Path.home()

    def test_repo_target_uses_repo_session_and_cwd(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        monkeypatch.setattr("agent_hq.dispatch._find_repo_session", lambda path: "agent-hq")
        r = client.post("/agents/new", data={"target": "repo:ben-ralston/agent-hq"})
        assert r.status_code == 200
        assert stub_tmux["open"] == [
            {
                "existing_session": "agent-hq",
                "new_session_name": "agent-hq",
                "cwd": Path("/Users/dev/agent-hq"),
                "harness": "claude",
            }
        ]

    def test_repo_target_creates_session_when_absent(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.post("/agents/new", data={"target": "repo:ben-ralston/widgets"})
        assert r.status_code == 200
        assert stub_tmux["find_repo"] == [Path("/Users/dev/widgets")]
        assert stub_tmux["open"][0]["existing_session"] is None
        assert stub_tmux["open"][0]["new_session_name"] == "widgets"

    def test_unknown_repo_404(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.post("/agents/new", data={"target": "repo:nobody/nothing"})
        assert r.status_code == 404
        assert stub_tmux["open"] == []

    def test_garbage_target_404(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.post("/agents/new", data={"target": "lolwut"})
        assert r.status_code == 404
        assert stub_tmux["open"] == []

    def test_create_failure_returns_error_toast(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        monkeypatch.setattr(
            "agent_hq.dispatch.open_chat_window",
            lambda **kwargs: None,
        )
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert "could not" in r.text.lower()
        assert stub_tmux["jump"] == []

    def test_jump_failure_still_reports_created(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        monkeypatch.setattr(
            "agent_hq.web.jump.perform_jump", lambda pane_id, focus_terminal=True: False
        )
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert "could not jump" in r.text.lower()

    def test_focus_zero_skips_terminal_focus(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        r = client.post("/agents/new", data={"target": "scratch", "focus": "0"})
        assert r.status_code == 200
        assert stub_tmux["jump"] == [{"pane_id": "%9", "focus_terminal": False}]

    def test_unauthenticated_redirects_to_login(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(tmp_path, monkeypatch, config=_mk_config(tmp_path))
        client.cookies.delete(COOKIE_NAME)
        r = client.post("/agents/new", data={"target": "scratch"}, follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert "/login" in r.headers["location"]
        assert stub_tmux["open"] == []


class TestToolbarButton:
    def test_agents_page_has_new_session_button(self, tmp_path, monkeypatch):
        client = _make_authed_client(tmp_path, monkeypatch)
        r = client.get("/agents")
        assert 'hx-get="/agents/new"' in r.text
        assert 'hx-target="#overlay-slot"' in r.text


_CODEX_CAPS = LauncherCapabilities(agents=frozenset({"claude", "codex"}), flags={})


class TestNewSessionHarnessPicker:
    """#205 Stage 1B Task 15: harness-only picker (no model/effort — a bare
    chat REPL has no such concept). ``capabilities_probe`` is injected via
    ``create_app`` so these tests never shell out to the real launch.sh."""

    def test_picker_hides_codex_when_unadvertised(self, tmp_path, monkeypatch):
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=_mk_config(tmp_path),
            capabilities_probe=lambda: CLAUDE_ONLY,
        )
        r = client.get("/agents/new")
        assert 'value="codex"' not in r.text

    def test_picker_shows_codex_when_advertised(self, tmp_path, monkeypatch):
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=_mk_config(tmp_path),
            capabilities_probe=lambda: _CODEX_CAPS,
        )
        r = client.get("/agents/new")
        assert 'value="codex"' in r.text

    def test_new_session_codex(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=_mk_config(tmp_path),
            capabilities_probe=lambda: _CODEX_CAPS,
        )
        r = client.post("/agents/new", data={"target": "scratch", "harness": "codex"})
        assert r.status_code == 200
        assert stub_tmux["open"][0]["harness"] == "codex"

    def test_new_session_rejects_unadvertised_codex(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=_mk_config(tmp_path),
            capabilities_probe=lambda: CLAUDE_ONLY,
        )
        r = client.post("/agents/new", data={"target": "scratch", "harness": "codex"})
        assert r.status_code == 400
        assert stub_tmux["open"] == []

    def test_new_session_rejects_invalid_harness_value(self, tmp_path, monkeypatch, stub_tmux):
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=_mk_config(tmp_path),
        )
        r = client.post("/agents/new", data={"target": "scratch", "harness": "bogus"})
        assert r.status_code == 400
        assert stub_tmux["open"] == []

    def test_probe_runs_off_the_event_loop_on_get(self, tmp_path, monkeypatch):
        """Fix 7 (multi-review wave): the GET /agents/new form-context probe
        call must run via run_in_threadpool, not synchronously on the event
        loop -- a hung/slow launch.sh must not stall the whole server. A
        probe called with no running event loop in its own thread proves it
        executed off-loop (run_in_threadpool workers have no loop of their
        own); a probe called directly on the route's async def would still
        see the request's running loop."""
        import asyncio

        saw_running_loop = []

        def probe():
            try:
                asyncio.get_running_loop()
                saw_running_loop.append(True)
            except RuntimeError:
                saw_running_loop.append(False)
            return CLAUDE_ONLY

        client = _make_authed_client(
            tmp_path, monkeypatch, config=_mk_config(tmp_path), capabilities_probe=probe
        )
        r = client.get("/agents/new")
        assert r.status_code == 200
        assert saw_running_loop == [False]

    def test_probe_runs_off_the_event_loop_on_post(self, tmp_path, monkeypatch, stub_tmux):
        import asyncio

        saw_running_loop = []

        def probe():
            try:
                asyncio.get_running_loop()
                saw_running_loop.append(True)
            except RuntimeError:
                saw_running_loop.append(False)
            return CLAUDE_ONLY

        client = _make_authed_client(
            tmp_path, monkeypatch, config=_mk_config(tmp_path), capabilities_probe=probe
        )
        r = client.post("/agents/new", data={"target": "scratch"})
        assert r.status_code == 200
        assert saw_running_loop == [False]

    def test_new_session_ignores_bad_configured_model(self, tmp_path, monkeypatch, stub_tmux):
        """Fix 6 (multi-review wave): a bare chat REPL has no model/effort
        concept, so /agents/new must not resolve/validate a configured model
        at all -- a bad model elsewhere in config (constructed directly
        here, bypassing load_config's own validation, to simulate an
        already-loaded bad config) must not 400 this model-less flow."""
        from agent_hq.config import DispatchConfig, HarnessDefaults

        cfg = _mk_config(tmp_path)
        cfg.dispatch = DispatchConfig(
            default_harness="claude",
            harnesses={"claude": HarnessDefaults(model="a b")},
        )
        client = _make_authed_client(
            tmp_path,
            monkeypatch,
            config=cfg,
            capabilities_probe=lambda: CLAUDE_ONLY,
        )
        r = client.post("/agents/new", data={"target": "scratch", "harness": ""})
        assert r.status_code == 200
        assert stub_tmux["open"][0]["harness"] == "claude"
