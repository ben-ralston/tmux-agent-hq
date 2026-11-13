"""The companion is gone (#399): Claude Duet replaced it.

Pins the removal contract: no ``agent_hq.companion`` package, /companion
route, nav tab, ``companion`` CLI subcommand or test-instance flag; a
leftover ``companion:`` config block is ignored with one warning per web
process.
"""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_hq import cli
from agent_hq.config import Config, DispatchConfig, load_config
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value

TEMPLATES = Path(__file__).resolve().parent.parent / "agent_hq" / "web" / "templates"


@pytest.fixture
def client(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    cfg = Config(
        repos={},
        projects=[],
        worktree_root=tmp_path,
        dispatch=DispatchConfig(),
        retired_keys=("companion",),
    )
    app = create_app(token_file=tm.token_file, config=cfg)
    c = TestClient(app)
    c.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return c, app


@pytest.mark.parametrize(
    "module",
    [
        "agent_hq.companion",
        "agent_hq.web.routes.companion",
        "agent_hq.web.static_files",
    ],
)
def test_companion_modules_are_gone(module):
    spec = importlib.util.find_spec(module)
    # A checkout that had the package keeps an empty dir behind (__pycache__),
    # which imports as a namespace package with no source: still gone.
    assert spec is None or spec.origin is None


class TestWeb:
    @pytest.mark.parametrize(
        "path",
        ["/companion", "/companion/thread.json", "/companion/activity", "/companion/threads"],
    )
    def test_companion_routes_404(self, client, path):
        c, _ = client
        assert c.get(path).status_code == 404

    def test_nav_has_no_companion_tab(self, client):
        c, _ = client
        r = c.get("/automations")
        assert r.status_code == 200
        assert 'href="/companion"' not in r.text

    def test_no_route_mentions_companion(self, client):
        _, app = client
        paths = [getattr(r, "path", "") for r in app.routes]
        assert [p for p in paths if "companion" in p] == []

    def test_create_app_takes_no_companion_injection(self):
        params = inspect.signature(create_app).parameters
        assert [p for p in params if p.startswith("companion")] == []

    def test_no_companion_templates(self):
        assert not list(TEMPLATES.rglob("companion*.html"))


class TestCli:
    def test_companion_subcommand_is_gone(self):
        with pytest.raises(SystemExit) as exc:
            cli.create_parser().parse_args(["companion", "wake"])
        assert exc.value.code == 2

    def test_web_has_no_companion_test_root(self):
        with pytest.raises(SystemExit) as exc:
            cli.create_parser().parse_args(["web", "--companion-test-root", "/tmp/x"])
        assert exc.value.code == 2


class TestConfig:
    def _write(self, tmp_path, body: str) -> Path:
        p = tmp_path / "projects.yaml"
        p.write_text(body)
        return p

    def test_config_has_no_companion_section(self):
        from agent_hq import config as config_mod

        assert "companion" not in Config.__dataclass_fields__
        for name in ("CompanionConfig", "CompanionPushConfig", "SuperviseConfig"):
            assert not hasattr(config_mod, name), name

    def test_leftover_companion_block_is_recorded_not_logged(self, tmp_path, monkeypatch):
        # CLIs and MCP servers load the config too, so the loader never logs.
        from agent_hq import eventlog

        calls = []
        monkeypatch.setattr(eventlog, "emit", lambda *a, **k: calls.append(a))
        p = self._write(
            tmp_path, "companion:\n  enabled: true\n  name: Pip\n  push:\n    team_id: X\n"
        )
        cfg = load_config(p)
        assert cfg.retired_keys == ("companion",)
        assert calls == []

    def test_malformed_companion_block_does_not_raise(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "companion: 5\n"))
        assert cfg.retired_keys == ("companion",)

    def test_nothing_recorded_without_companion_block(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "repos: {}\n"))
        assert cfg.retired_keys == ()

    def test_web_app_warns_once_at_startup(self, tmp_path, monkeypatch):
        from agent_hq.web import app as app_mod

        calls = []
        monkeypatch.setattr(app_mod.eventlog, "emit", lambda *a, **k: calls.append(a[:2]))
        tm = TokenManager(tmp_path / "token")
        cfg = Config(repos={}, projects=[], worktree_root=tmp_path, retired_keys=("companion",))
        create_app(token_file=tm.token_file, config=cfg)
        assert calls.count(("warn", "companion config block ignored")) == 1

    def test_web_app_silent_without_leftovers(self, tmp_path, monkeypatch):
        from agent_hq.web import app as app_mod

        calls = []
        monkeypatch.setattr(app_mod.eventlog, "emit", lambda *a, **k: calls.append(a[:2]))
        tm = TokenManager(tmp_path / "token")
        create_app(
            token_file=tm.token_file,
            config=Config(repos={}, projects=[], worktree_root=tmp_path),
        )
        assert ("warn", "companion config block ignored") not in calls


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "agent_hq" / "web" / "static"


def _tracked(*paths: str) -> list[str]:
    # Tracked files, not the folders: an older checkout keeps their ignored
    # build output (node_modules, the iOS app's generated config) behind.
    out = subprocess.run(
        ["git", "ls-files", "--", *paths], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


class TestFrontEnd:
    def test_island_and_its_source_are_gone(self):
        assert _tracked("agent_hq/web/static/companion-island", "web-ui") == []

    def test_js_has_no_companion_or_native_bridge(self):
        js = (STATIC / "agent_hq.js").read_text()
        for needle in ("companion", "agentHqNative", "kb-open"):
            assert needle not in js, needle

    def test_css_has_no_companion_rules_or_chat_tokens(self):
        sheets = [STATIC / "components.css", STATIC / "layout.css", STATIC / "tokens.css"]
        sheets += sorted((STATIC / "themes").glob("*.css"))
        for sheet in sheets:
            text = sheet.read_text()
            for needle in (".companion", "--chat-", "kb-open"):
                assert needle not in text, (sheet.name, needle)

    def test_sw_no_longer_precaches_the_island(self):
        sw = (STATIC / "sw.js").read_text()
        assert "companion-island" not in sw.split("const VERSION")[1]
        assert 'const VERSION = "v70"' not in sw

    def test_ci_has_no_island_job(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        assert "island" not in ci
        assert "web-ui" not in ci

    def test_run_has_no_build_ui_or_ios_sync(self):
        run = (ROOT / "run").read_text() + (ROOT / "scripts" / "_cli.sh").read_text()
        for needle in ("build-ui", "ios-sync", "ios-app"):
            assert needle not in run, needle

    def test_ios_app_is_gone(self):
        # The owner's call on #399 step 6: its native features only served the companion.
        assert _tracked("ios-app") == []

    def test_leftover_build_output_stays_ignored(self):
        # The deleted ios-app/.gitignore kept the generated config (it holds
        # the web token) out of commits; the root .gitignore covers it now.
        for path in (
            "ios-app/ios/App/App/capacitor.config.json",
            "web-ui/companion/node_modules/x",
        ):
            r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            assert r.returncode == 0, path
