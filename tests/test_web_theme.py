"""Tests for theme stylesheet injection (#200) and persistence.

The base template links an extra token-override stylesheet for the
active theme. Precedence: a whitelisted ``?theme=`` query param (kept
as a screenshot/dev override) wins over the persisted ``web_theme``
preference in ``ui-prefs.json``; the whitelist is the injection guard —
any value outside it must inject nothing. ``POST /prefs/theme`` sets
the persisted preference and redirects back to the page the picker was
on, so the reload links the new stylesheet.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_hq.ui_prefs import UiPrefsStore
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _make_client(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
    app = create_app(token_file=tm.token_file, config=None, ui_prefs_store=store)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


class TestThemePreview:
    def test_midnight_theme_links_override_stylesheet(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents?theme=midnight")
        assert r.status_code == 200
        assert "/static/themes/midnight.css" in r.text

    def test_warm_theme_links_override_stylesheet(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents?theme=warm")
        assert r.status_code == 200
        assert "/static/themes/warm.css" in r.text

    def test_no_param_links_no_theme(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents")
        assert r.status_code == 200
        assert "/static/themes/" not in r.text

    def test_unknown_value_links_no_theme(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents?theme=../../etc/passwd")
        assert r.status_code == 200
        assert "/static/themes/" not in r.text

    def test_theme_applies_on_other_pages_via_base_template(self, tmp_path):
        # /login extends base.html and needs neither auth nor config.
        client = _make_client(tmp_path)
        r = client.get("/login?theme=warm")
        assert r.status_code == 200
        assert "/static/themes/warm.css" in r.text


class TestThemePersistence:
    def test_post_sets_theme_and_redirects_back(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.post(
            "/prefs/theme",
            data={"theme": "midnight"},
            headers={"referer": "http://testserver/automations"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/automations"

    def test_persisted_theme_links_stylesheet_on_every_page(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "midnight"})
        for path in ("/agents", "/issues", "/login"):
            r = client.get(path)
            assert r.status_code == 200
            assert "/static/themes/midnight.css" in r.text, path

    def test_default_clears_persisted_theme(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "warm"})
        client.post("/prefs/theme", data={"theme": "default"})
        r = client.get("/agents")
        assert "/static/themes/" not in r.text

    def test_query_param_overrides_persisted_theme(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "midnight"})
        r = client.get("/agents?theme=warm")
        assert "/static/themes/warm.css" in r.text
        assert "/static/themes/midnight.css" not in r.text

    def test_invalid_theme_is_not_stored_and_returns_user_to_the_page(self, tmp_path):
        # The only caller is a native <form> POST, so answering with raw 422
        # JSON would navigate the user out of the app onto an error document.
        # Redirecting back instead leaves the theme unchanged AND reloads the
        # page — which self-heals the drift case that produces an unknown
        # theme (a stale tab offering an option the server no longer knows).
        client = _make_client(tmp_path)
        r = client.post(
            "/prefs/theme",
            data={"theme": "../../etc"},
            headers={"referer": "http://testserver/issues"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/issues"
        assert "/static/themes/" not in client.get("/agents").text

    def test_malformed_referer_does_not_error(self, tmp_path):
        # urlsplit() raises ValueError on an unclosed IPv6 authority. The
        # parse happens after the pref is already persisted, so an unhandled
        # raise means the write lands and the client still sees a 500.
        client = _make_client(tmp_path)
        r = client.post(
            "/prefs/theme",
            data={"theme": "warm"},
            headers={"referer": "http://[::1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/agents"
        assert UiPrefsStore(path=tmp_path / "ui-prefs.json").get_web_theme() == "warm"

    def test_query_param_default_overrides_persisted_theme(self, tmp_path):
        # ?theme=default must force the stock look for screenshots, exactly
        # like ?theme=midnight forces midnight — otherwise a "default" capture
        # silently shows whatever theme happens to be persisted.
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "midnight"})
        r = client.get("/agents?theme=default")
        assert "/static/themes/" not in r.text

    def test_missing_referer_redirects_to_agents(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.post("/prefs/theme", data={"theme": "warm"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/agents"

    def test_external_referer_host_is_discarded(self, tmp_path):
        # Only the referer's PATH is reused — never its scheme/host — so a
        # foreign referer can't turn this into an open redirect.
        client = _make_client(tmp_path)
        r = client.post(
            "/prefs/theme",
            data={"theme": "warm"},
            headers={"referer": "https://evil.example/phish"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/phish"  # local path, host gone

    def test_protocol_relative_referer_path_falls_back(self, tmp_path):
        # A referer whose PATH itself is protocol-relative ("//host") would
        # redirect off-site if echoed — must fall back to /agents.
        client = _make_client(tmp_path)
        r = client.post(
            "/prefs/theme",
            data={"theme": "warm"},
            headers={"referer": "https://evil.example//attacker.example"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/agents"

    def test_unauthenticated_post_is_rejected(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        store = UiPrefsStore(path=tmp_path / "ui-prefs.json")
        app = create_app(token_file=tm.token_file, config=None, ui_prefs_store=store)
        client = TestClient(app)
        r = client.post("/prefs/theme", data={"theme": "midnight"}, follow_redirects=False)
        assert r.status_code in (302, 401, 403)  # login redirect or denial
        assert store.get_web_theme() is None


class TestExpressiveThemes:
    """The five expressive themes (#220) and the template-side registry.

    ``partials/_theme_registry.html`` is the single template-side source of
    truth (ids, stylesheet-backed subset, labels, color schemes); base.html
    and the settings menu import it. Parity with the Python ``WEB_THEMES``
    allowlist is pinned here so the two homes can't drift.
    """

    NEW_THEMES = (
        "paper",
        "terminal",
        "neon",
        "brutalist",
        "console",
        # Adopted from Codex's #221 bake-off take, then reworked pink/light:
        "bubblegum",
    )

    def test_each_new_theme_links_its_stylesheet(self, tmp_path):
        client = _make_client(tmp_path)
        for theme in self.NEW_THEMES:
            r = client.get(f"/agents?theme={theme}")
            assert r.status_code == 200, theme
            assert f"/static/themes/{theme}.css" in r.text, theme

    def test_each_new_theme_persists_via_prefs_route(self, tmp_path):
        client = _make_client(tmp_path)
        for theme in self.NEW_THEMES:
            r = client.post("/prefs/theme", data={"theme": theme}, follow_redirects=False)
            assert r.status_code == 303, theme
            assert f"/static/themes/{theme}.css" in client.get("/agents").text, theme

    def test_data_theme_attribute_stamped_on_html(self, tmp_path):
        client = _make_client(tmp_path)
        for theme in ("midnight", *self.NEW_THEMES):
            r = client.get(f"/agents?theme={theme}")
            assert f'data-theme="{theme}"' in r.text, theme

    def test_data_theme_defaults_to_default(self, tmp_path):
        client = _make_client(tmp_path)
        assert 'data-theme="default"' in client.get("/agents").text

    def test_unknown_theme_param_stamps_default(self, tmp_path):
        client = _make_client(tmp_path)
        assert 'data-theme="default"' in client.get("/agents?theme=bogus").text

    def test_light_themes_flip_the_scheme(self, tmp_path):
        # Paper and Bubblegum are the light themes: the color-scheme meta
        # must flip so form controls/scrollbars render light, and the static
        # dark class must come off <html>.
        client = _make_client(tmp_path)
        for theme in ("paper", "bubblegum"):
            r = client.get(f"/agents?theme={theme}")
            assert '<meta name="color-scheme" content="light">' in r.text, theme
            assert 'class="dark"' not in r.text, theme

    def test_dark_themes_keep_dark_scheme(self, tmp_path):
        client = _make_client(tmp_path)
        for suffix in ("", "?theme=terminal", "?theme=midnight"):
            r = client.get(f"/agents{suffix}")
            assert '<meta name="color-scheme" content="dark">' in r.text, suffix
            assert 'class="dark"' in r.text, suffix

    # Picker-visible subset in display order; midnight/warm/brutalist stay
    # valid (deep links, old persisted prefs) but are hidden from the menu.
    PICKER = ("default", "console", "neon", "terminal", "paper", "bubblegum")

    def _picker_options(self, html):
        import re

        select = re.search(r'<select id="theme-select".*?</select>', html, re.S).group(0)
        return tuple(re.findall(r'value="([^"]+)"', select))

    def test_settings_menu_lists_picker_themes_in_order(self, tmp_path):
        client = _make_client(tmp_path)
        assert self._picker_options(client.get("/agents").text) == self.PICKER

    def test_hidden_themes_stay_valid_but_off_the_menu(self, tmp_path):
        # Hidden ≠ removed: the stylesheet link and the prefs route must
        # keep working for every registered theme, only the <option> goes.
        client = _make_client(tmp_path)
        for theme in ("midnight", "warm", "brutalist"):
            assert f"/static/themes/{theme}.css" in client.get(f"/agents?theme={theme}").text
            r = client.post("/prefs/theme", data={"theme": theme}, follow_redirects=False)
            assert r.status_code == 303, theme
        assert "brutalist" not in self._picker_options(client.get("/agents?theme=default").text)

    def test_persisted_hidden_theme_appears_selected_in_picker(self, tmp_path):
        # A user carrying a hidden theme (persisted before it was hidden)
        # must see it selected — otherwise the picker silently shows
        # Default and the next save switches their theme out from under them.
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "midnight"})
        html = client.get("/agents").text
        assert 'value="midnight" selected' in html
        assert self._picker_options(html) == (*self.PICKER, "midnight")

    def test_jinja_registry_matches_python_allowlist(self):
        # The template registry and WEB_THEMES are two homes of one list;
        # this is the parity pin. THEME_FILES must be every id but default
        # (default links no stylesheet), every id needs a label + scheme,
        # and the picker subset must only name registered ids.
        import jinja2

        from agent_hq.ui_prefs import WEB_THEMES
        from agent_hq.web.routes.system import TEMPLATES_DIR

        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)))
        tmpl = env.from_string(
            '{% import "partials/_theme_registry.html" as reg %}'
            "{{ reg.THEME_IDS|join(',') }}|{{ reg.THEME_FILES|join(',') }}"
            "|{{ reg.THEME_LABELS|length }}|{{ reg.THEME_SCHEMES['paper'] }}"
            "|{{ reg.THEME_SCHEMES['default'] }}|{{ reg.THEME_PICKER_IDS|join(',') }}"
        )
        ids, files, n_labels, paper_scheme, default_scheme, picker = tmpl.render().split("|")
        assert tuple(ids.split(",")) == WEB_THEMES
        assert tuple(files.split(",")) == WEB_THEMES[1:]
        assert int(n_labels) == len(WEB_THEMES)
        assert paper_scheme == "light"
        assert default_scheme == "dark"
        assert tuple(picker.split(",")) == self.PICKER
        assert set(picker.split(",")) <= set(ids.split(","))


class TestStructuralTokens:
    """The #220 structural hooks. Mostly visual, but the iOS-zoom floor is
    load-bearing enough to pin: form controls must read --text-input and the
    token must stay 16px — a theme shrinking the type scale via --text-*
    must not be able to drag inputs below the zoom threshold."""

    def _static(self):
        from agent_hq.web.routes.system import TEMPLATES_DIR

        return TEMPLATES_DIR.parent / "static"

    def test_input_floor_token_pinned_at_16px(self):
        assert "--text-input: 16px" in (self._static() / "tokens.css").read_text()

    def test_form_controls_use_the_floor_token(self):
        static = self._static()
        combined = (static / "layout.css").read_text() + (static / "components.css").read_text()
        # The four floor-guarded controls (login input, filter input,
        # settings-menu select, inbox textarea) all moved to the token.
        assert combined.count("font-size: var(--text-input)") >= 4

    def test_every_registered_theme_has_a_stylesheet_on_disk(self):
        # base.html links /static/themes/<id>.css for every non-default id;
        # a missing file would 404 silently and render the stock look.
        from agent_hq.ui_prefs import WEB_THEMES

        themes_dir = self._static() / "themes"
        for theme in WEB_THEMES[1:]:
            assert (themes_dir / f"{theme}.css").is_file(), theme

    def test_service_worker_precaches_themes_and_fonts(self):
        # sw.js hand-copies the theme list a third time (OPTIONAL precache);
        # pin it against the registry, and pin every @font-face src (plus
        # every font file on disk) into the precache so offline keeps the
        # active theme's face. VERSION must be past v5 (the pre-#220 shell).
        import re

        from agent_hq.ui_prefs import WEB_THEMES

        static = self._static()
        sw = (static / "sw.js").read_text()
        for theme in WEB_THEMES[1:]:
            assert f'"/static/themes/{theme}.css"' in sw, theme
        for font in sorted((static / "fonts").glob("*.woff2")):
            assert f'"/static/fonts/{font.name}"' in sw, font.name
        for theme_css in sorted((static / "themes").glob("*.css")):
            for src in re.findall(r'url\("(/static/fonts/[^"]+)"\)', theme_css.read_text()):
                assert f'"{src}"' in sw, (theme_css.name, src)
                assert (static / src.removeprefix("/static/")).is_file(), src
        assert 'VERSION = "v5"' not in sw


class TestSettingsMenu:
    # Pages registered without a projects.yaml (config=None). /inbox is
    # config-gated so it's exercised by the idea-route tests instead.
    PAGES = ("/agents", "/issues", "/automations")

    def test_every_page_has_settings_menu_with_logout_label(self, tmp_path):
        client = _make_client(tmp_path)
        for path in self.PAGES:
            r = client.get(path)
            assert r.status_code == 200, path
            assert "settings-menu" in r.text, path
            assert "Log Out" in r.text, path
            assert "⏻" not in r.text, path

    def test_theme_picker_posts_to_prefs_route(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents")
        assert 'action="/prefs/theme"' in r.text

    def test_theme_picker_defaults_to_default(self, tmp_path):
        client = _make_client(tmp_path)
        r = client.get("/agents")
        assert 'value="default" selected' in r.text

    def test_theme_picker_reflects_persisted_choice(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/prefs/theme", data={"theme": "warm"})
        r = client.get("/automations")
        assert 'value="warm" selected' in r.text
        assert 'value="default" selected' not in r.text
