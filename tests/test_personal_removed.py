"""The Personal tab is gone (#345).

Pins the removal contract: no /personal route, nav entry, TUI screen or
personal dispatch path; the Idea Inbox routes only to repos; the issue
sheet has no "→ Personal" reroute. Since #399 a leftover ``personal:``
config block is ignored like any unknown key, with no warning, and
``personal.dispatch_cwd`` no longer sets the scratch session's cwd
(``dispatch.scratch_cwd`` does).
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_hq import config as config_mod
from agent_hq.config import Config, DispatchConfig, load_config
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value

STATIC = Path(__file__).resolve().parent.parent / "agent_hq" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "agent_hq" / "web" / "templates"


@pytest.fixture
def client(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    cfg = Config(repos={}, projects=[], worktree_root=tmp_path, dispatch=DispatchConfig())
    app = create_app(token_file=tm.token_file, config=cfg)
    c = TestClient(app)
    c.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return c, app


@pytest.mark.parametrize(
    "module",
    [
        "agent_hq.personal",
        "agent_hq.personal_store",
        "agent_hq.personal_filters",
        "agent_hq.web.personal_state",
        "agent_hq.web.routes.personal",
        "agent_hq.screens.personal_dispatch_screen",
        # Only the Notes fetcher used these two (checked #345).
        "agent_hq.disclaim",
        "agent_hq.mcp_client",
    ],
)
def test_personal_modules_are_gone(module):
    assert importlib.util.find_spec(module) is None


def test_reroute_to_personal_helper_is_gone():
    # fetch_issue_details only served IdeaHandler.reroute_to_personal.
    from agent_hq import github

    assert not hasattr(github, "fetch_issue_details")


class TestWeb:
    @pytest.mark.parametrize(
        "path", ["/personal", "/personal/container", "/personal/list", "/personal/refresh"]
    )
    def test_personal_routes_404(self, client, path):
        c, _ = client
        assert c.get(path).status_code == 404

    def test_nav_has_no_personal_tab(self, client):
        c, _ = client
        r = c.get("/automations")
        assert r.status_code == 200
        assert 'href="/personal"' not in r.text
        assert "Personal</a>" not in r.text

    def test_no_route_mentions_personal(self, client):
        _, app = client
        paths = [getattr(r, "path", "") for r in app.routes]
        assert [p for p in paths if "personal" in p] == []

    def test_app_state_has_no_personal_wiring(self, client):
        _, app = client
        for attr in ("personal_cache", "personal_inbox_store", "personal_refresher"):
            assert not hasattr(app.state, attr), attr

    def test_create_app_takes_no_personal_injection(self):
        params = inspect.signature(create_app).parameters
        assert "personal_cache" not in params
        assert "personal_inbox_store" not in params
        assert "confirmed_store" not in params

    def test_templates_have_no_personal_affordances(self):
        assert not list(TEMPLATES.rglob("personal*.html"))
        detail = (TEMPLATES / "partials" / "issue_detail.html").read_text()
        assert "send-to-personal" not in detail
        for name in ("inbox.html", "partials/inbox_recent.html"):
            text = (TEMPLATES / name).read_text()
            assert "/personal" not in text, name
            assert "__personal__" not in text, name

    def test_static_has_no_personal_css_or_js(self):
        js = (STATIC / "agent_hq.js").read_text()
        for needle in ("personal-container", "personal.refreshed", "/personal/", "personal.*"):
            assert needle not in js, needle
        css = (STATIC / "components.css").read_text() + (STATIC / "layout.css").read_text()
        for needle in (".personal-", ".recent-item--personal"):
            assert needle not in css, needle

    def test_sw_version_bumped(self):
        sw = (STATIC / "sw.js").read_text()
        assert 'const VERSION = "v55"' not in sw


class TestConfig:
    def _write(self, tmp_path, body: str) -> Path:
        p = tmp_path / "projects.yaml"
        p.write_text(body)
        return p

    def test_config_has_no_personal_section(self):
        assert "personal" not in Config.__dataclass_fields__
        assert not hasattr(config_mod, "PersonalConfig")

    def test_leftover_personal_keys_are_ignored_silently(self, tmp_path, monkeypatch):
        # #399: the #345 shim (ignored_personal_keys + a startup warning) is gone.
        from agent_hq import eventlog
        from agent_hq.web import app as app_mod

        calls = []
        monkeypatch.setattr(eventlog, "emit", lambda *a, **k: calls.append(a[:2]))
        monkeypatch.setattr(app_mod.eventlog, "emit", lambda *a, **k: calls.append(a[:2]))
        p = self._write(
            tmp_path,
            "personal:\n  reminders_lists: [Reminders]\n  dispatch_cwd: ~/scratch\n"
            "idea_inbox:\n  personal_routing_description: errands\n",
        )
        cfg = load_config(p)
        assert "ignored_personal_keys" not in Config.__dataclass_fields__
        assert not hasattr(config_mod, "_legacy_personal")
        create_app(token_file=TokenManager(tmp_path / "token").token_file, config=cfg)
        assert ("warn", "personal config block ignored") not in calls

    def test_malformed_personal_block_does_not_raise(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "personal: 5\n"))
        assert cfg.dispatch.scratch_cwd == Path.home()

    def test_scratch_cwd_defaults_home(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "repos: {}\n"))
        assert cfg.dispatch.scratch_cwd == Path.home()

    def test_scratch_cwd_parsed_and_expanded(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "dispatch:\n  scratch_cwd: ~/scratch\n"))
        assert cfg.dispatch.scratch_cwd == Path.home() / "scratch"

    def test_legacy_personal_dispatch_cwd_is_no_longer_read(self, tmp_path):
        # #399: the #345 fallback is gone; dispatch.scratch_cwd is the one key.
        cfg = load_config(self._write(tmp_path, "personal:\n  dispatch_cwd: ~/scratch\n"))
        assert cfg.dispatch.scratch_cwd == Path.home()

    def test_scratch_cwd_must_be_a_string(self, tmp_path):
        with pytest.raises(config_mod.ConfigError, match="scratch_cwd"):
            load_config(self._write(tmp_path, "dispatch:\n  scratch_cwd: 5\n"))

    def test_personal_routing_description_is_ignored(self, tmp_path):
        cfg = load_config(
            self._write(tmp_path, "idea_inbox:\n  personal_routing_description: errands\n")
        )
        assert cfg is not None
        assert not hasattr(cfg.idea_inbox, "personal_routing_description")


class TestIdeaInbox:
    def test_classifier_has_no_personal_target(self):
        from agent_hq.idea import classifier

        assert not hasattr(classifier, "PERSONAL_TARGET")
        assert "personal_description" not in inspect.signature(classifier.classify).parameters
        assert "destination" not in classifier.Classification.__dataclass_fields__

    def test_handler_has_no_personal_store(self):
        from agent_hq.idea import handler

        assert "personal_store" not in inspect.signature(handler.IdeaHandler).parameters
        assert not hasattr(handler.IdeaHandler, "reroute_to_personal")


class TestDispatchAndTui:
    def test_personal_dispatch_is_gone(self):
        from agent_hq import dispatch

        assert not hasattr(dispatch, "dispatch_personal_items")
        assert not hasattr(dispatch, "PERSONAL_SESSION_NAME")

    def test_agents_screen_has_no_personal_binding(self):
        from agent_hq.dashboard import AgentDashboard

        actions = {b.action for b in AgentDashboard.BINDINGS}
        assert "switch_to_personal" not in actions
        assert not hasattr(AgentDashboard, "action_switch_to_personal")

    def test_dispatch_screen_l_returns_to_agents(self):
        from agent_hq.screens.dispatch_screen import DispatchScreen

        tabs = {b.key: b for b in DispatchScreen.BINDINGS if b.key in ("l", "L")}
        assert {k: (b.action, b.description) for k, b in tabs.items()} == {
            "l": ("switch_tab", "Agents"),
            "L": ("switch_tab", "Agents"),
        }
        # Two tabs: one footer entry is enough.
        assert tabs["l"].show and not tabs["L"].show
        assert not hasattr(DispatchScreen, "action_switch_tab_back")
