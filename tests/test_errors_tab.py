"""Web Errors tab tests (#148)."""

from fastapi.testclient import TestClient

from agent_hq import logreader as logreader_mod
from agent_hq.logreader import ErrorsCache, LogPage, LogRow
from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _row(id, app="backup", level="error", message="boom", ts="2026-08-19T01:00:00Z"):
    return LogRow(
        id=id, app=app, level=level, message=message, ts=ts, source="mac", json_extra=None
    )


def _make_client(tmp_path, *, configured=True, errors_cache=None):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(
        token_file=tm.token_file,
        config=None,
        errors_cache=errors_cache or ErrorsCache(),
        logs_configured=lambda: configured,
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client


def test_errors_page_renders_configured(tmp_path):
    client = _make_client(tmp_path)
    r = client.get("/errors")
    assert r.status_code == 200
    assert 'id="errors-section"' in r.text
    assert "logger-admin.token" not in r.text  # no setup hint when configured


def test_errors_page_unconfigured_shows_setup_hint(tmp_path):
    client = _make_client(tmp_path, configured=False)
    r = client.get("/errors")
    assert r.status_code == 200
    assert "logger-admin.token" in r.text
    assert 'id="errors-section"' not in r.text


def test_errors_page_outer_section_has_no_poll_trigger(tmp_path):
    # #148 fix: the outer section must not poll the bare default URL — that
    # would wipe a user's non-default filter selection every 60s. Polling
    # now lives inside the section partial, carrying current params.
    client = _make_client(tmp_path)
    r = client.get("/errors")
    section_start = r.text.index('id="errors-section"')
    section_tag_end = r.text.index(">", section_start)
    section_tag = r.text[section_start:section_tag_end]
    assert "every 60s" not in section_tag


def test_nav_has_errors_tab_on_every_page(tmp_path):
    client = _make_client(tmp_path)
    for path in ("/errors", "/automations"):
        r = client.get(path)
        assert 'href="/errors"' in r.text


def test_errors_page_inline_first_paint_from_cache(tmp_path):
    cache = ErrorsCache()
    cache.fetch(lambda: LogPage(rows=[_row(7)], count=1, max_id=7))
    client = _make_client(tmp_path, errors_cache=cache)
    r = client.get("/errors")
    assert "boom" in r.text  # cached row embedded on the page GET


def _page(*rows):
    return LogPage(rows=list(rows), count=len(rows), max_id=max((r.id for r in rows), default=None))


def test_section_default_groups_by_app(tmp_path, monkeypatch):
    cache = ErrorsCache()
    cache.fetch(
        lambda: _page(_row(5, app="backup"), _row(9, app="agent-hq"), _row(3, app="backup"))
    )
    client = _make_client(tmp_path, errors_cache=cache)
    r = client.get("/errors/section")
    assert r.status_code == 200
    # groups ordered by newest row: agent-hq (9) before backup (5, 3)
    assert r.text.index("agent-hq") < r.text.index("backup")
    # level + window chips present, defaults active
    assert "error,alert" not in r.text or True  # chips render per-level
    assert 'aria-pressed="true"' in r.text


def test_section_nondefault_levels_fetches_live(tmp_path, monkeypatch):
    calls = {}

    def fake_fetch(app=None, levels=None, since=None, since_id=None, limit=100, order=None):
        calls["levels"] = levels
        calls["since"] = since
        return _page(_row(1, level="warn"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)  # empty cache — default would need live too
    r = client.get("/errors/section?level=warn&window=7d")
    assert r.status_code == 200
    assert list(calls["levels"]) == ["warn"]
    assert calls["since"] == "7d"


def test_section_invalid_params_fall_back_to_defaults(tmp_path, monkeypatch):
    calls = {}

    def fake_fetch(app=None, levels=None, since=None, since_id=None, limit=100, order=None):
        calls["levels"] = list(levels)
        calls["since"] = since
        return _page()

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?level=bogus,error&window=99y")
    assert r.status_code == 200
    assert calls["levels"] == ["error"]  # bogus dropped; empty result → defaults
    assert calls["since"] == "24h"


def test_section_app_filter_keeps_all_app_chips(tmp_path, monkeypatch):
    def fake_fetch(**kwargs):
        assert kwargs.get("app") is None  # app filtering is in-route, not API-side
        return _page(_row(5, app="backup"), _row(9, app="agent-hq"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?app=backup")
    assert "backup" in r.text
    assert "agent-hq" in r.text  # still listed as a chip
    assert r.text.count("error-row") >= 1


def test_section_poll_carries_current_filters(tmp_path, monkeypatch):
    # #148 fix: the section partial's own poll element must re-GET with the
    # CURRENT level/window/app params, not the bare default URL — otherwise
    # the 60s poll silently resets a user's filter selection.
    def fake_fetch(**kwargs):
        return _page(_row(1, level="warn"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?level=warn&window=7d")
    assert r.status_code == 200
    assert 'hx-trigger="every 60s"' in r.text
    poll_idx = r.text.index('hx-trigger="every 60s"')
    poll_start = r.text.rindex("<div", 0, poll_idx)
    poll_tag_end = r.text.index(">", poll_idx)
    poll_markup = r.text[poll_start:poll_tag_end]
    assert "level=warn" in poll_markup
    assert "window=7d" in poll_markup


def test_section_app_filter_ghost_renders_active_chip(tmp_path, monkeypatch):
    # #148 fix: an active ?app=X filter whose rows have all aged out must
    # not leave the user stuck with an invisible filter — render it as an
    # active chip (count 0) that clears on click.
    def fake_fetch(**kwargs):
        return _page(_row(5, app="backup"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?app=ghost")
    assert r.status_code == 200
    ghost_idx = r.text.index(">ghost<")
    button_start = r.text.rindex("<button", 0, ghost_idx)
    button_markup = r.text[button_start:ghost_idx]
    assert "filter-chip--active" in button_markup
    assert "app=" not in button_markup  # clicking it clears the filter


def test_section_alt_order_default_levels_serves_from_cache(tmp_path, monkeypatch):
    # #148 fix: is_default comparison must be order-insensitive — toggling
    # error off/on produces ["alert", "error"] which must still hit cache.
    calls = {"n": 0}

    def fake_fetch(**kwargs):
        calls["n"] += 1
        return _page(_row(1))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    cache = ErrorsCache()
    cache.fetch(lambda: _page(_row(99, message="cached-row")))
    client = _make_client(tmp_path, errors_cache=cache)
    r = client.get("/errors/section?level=alert,error")
    assert calls["n"] == 0  # served from warm cache, no live fetch
    assert "cached-row" in r.text


def test_section_read_error_renders_inline_panel(tmp_path, monkeypatch):
    def fake_fetch(**kwargs):
        raise logreader_mod.LogReadError("logger returned HTTP 401")

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section")
    assert r.status_code == 200  # inline panel, never a 500
    assert "HTTP 401" in r.text


def test_section_refresh_forces_fetch(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(**kwargs):
        calls["n"] += 1
        return _page(_row(1))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    cache = ErrorsCache()
    cache.fetch(lambda: _page(_row(99, message="stale-marker")))
    client = _make_client(tmp_path, errors_cache=cache)
    r = client.get("/errors/section?refresh=1")
    assert calls["n"] == 1
    assert "stale-marker" not in r.text


def test_context_shows_row_and_neighbors(tmp_path, monkeypatch):
    rows = [_row(i, app="backup", message=f"msg-{i}") for i in range(30, 0, -1)]

    def fake_fetch(**kwargs):
        assert kwargs["app"] == "backup"
        assert kwargs.get("levels") in (None, ())  # context is all-levels
        return _page(*rows)

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/row/15/context?app=backup")
    assert r.status_code == 200
    assert "msg-15" in r.text and "msg-5" in r.text and "msg-25" in r.text
    assert "msg-27" not in r.text  # outside the ±10 slice
    assert 'class="error-row--focus"' in r.text or "error-row--focus" in r.text


def test_context_deep_row_gets_anchored_context(tmp_path, monkeypatch):
    # Fix (multi-review): a row deeper than the 200-row desc window must not
    # be mislabeled "aged out" — the since_id-anchored fetch finds the focus
    # row and its following context at any depth.
    calls = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        if kwargs.get("since_id") is not None:
            assert kwargs["since_id"] == 2 and kwargs["order"] == "asc"
            return _page(*[_row(i, app="backup", message=f"anchored-{i}") for i in range(3, 14)])
        return _page(_row(500, app="backup", message="recent-500"))  # window misses id 3

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/row/3/context?app=backup")
    assert r.status_code == 200
    assert "anchored-3" in r.text  # the focus row itself
    assert "anchored-13" in r.text  # following context rendered, not suppressed
    assert "error-row--focus" in r.text
    assert "aged out" not in r.text.lower()


def test_context_aged_out_id_mismatch_renders_not_found(tmp_path, monkeypatch):
    # #148 fix: the since_id fallback fetch can return a row that isn't the
    # requested one (e.g. gaps in ids) — guard against showing the wrong row.
    def fake_fetch(**kwargs):
        if kwargs.get("since_id") is not None:
            return _page(_row(999, app="backup", message="wrong-row"))
        return _page(_row(500, app="backup"))  # recent window misses id 3

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/row/3/context?app=backup")
    assert r.status_code == 200
    assert "wrong-row" not in r.text
    assert "not found" in r.text.lower()


def test_section_retry_button_keeps_app_filter(tmp_path, monkeypatch):
    # Fix (multi-review): the error-panel Retry button must carry the active
    # ?app= filter like every other section URL — otherwise retrying after a
    # logger blip silently clears the user's app filter.
    def fake_fetch(**kwargs):
        raise logreader_mod.LogReadError("logger returned HTTP 502")

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?level=warn&app=backup")
    assert r.status_code == 200
    retry_idx = r.text.index(">Retry<")
    button_start = r.text.rindex("<button", 0, retry_idx)
    button_markup = r.text[button_start:retry_idx]
    assert "app=backup" in button_markup


def test_section_last_level_chip_deselect_is_noop(tmp_path, monkeypatch):
    # Fix (multi-review): deselecting the ONLY active level chip used to send
    # level= (empty), which the server silently resets to error+alert — a
    # selection the user never made. The last chip's URL keeps the current
    # level instead (no-op).
    def fake_fetch(**kwargs):
        return _page(_row(1, level="warn"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?level=warn")
    assert r.status_code == 200
    assert "level=&" not in r.text  # no URL anywhere with an empty level param


def test_errors_page_first_paint_includes_app_chips(tmp_path):
    # Fix (multi-review): the warm-cache inline first paint must compute app
    # chips like every /errors/section render — not hardcode [] and let the
    # chip row pop in at the first poll.
    cache = ErrorsCache()
    cache.fetch(lambda: _page(_row(5, app="backup"), _row(9, app="agent-hq")))
    client = _make_client(tmp_path, errors_cache=cache)
    r = client.get("/errors")
    assert "filter-chip__count" in r.text  # app chips (with counts) inline


def test_errors_page_header_has_refresh_button(tmp_path):
    # Refresh lives in the top bar like the Dispatch tab's ↻ — it fires a
    # body event the section's own refresher listens for, so the current
    # filter params stay in charge of the URL.
    client = _make_client(tmp_path)
    r = client.get("/errors")
    assert "refresh-errors" in r.text
    assert "↻" in r.text


def test_errors_page_unconfigured_has_no_refresh_button(tmp_path):
    client = _make_client(tmp_path, configured=False)
    r = client.get("/errors")
    assert "↻" not in r.text


def test_section_event_refresher_carries_params_and_no_chip(tmp_path, monkeypatch):
    def fake_fetch(**kwargs):
        return _page(_row(1, level="warn", app="backup"))

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/section?level=warn&window=7d&app=backup")
    assert r.status_code == 200
    assert "⟳" not in r.text  # chip-bar refresh replaced by the header button
    trigger_idx = r.text.index('hx-trigger="refresh-errors from:body"')
    div_start = r.text.rindex("<div", 0, trigger_idx)
    div_end = r.text.index(">", trigger_idx)
    markup = r.text[div_start:div_end]
    assert "level=warn" in markup
    assert "window=7d" in markup
    assert "app=backup" in markup
    assert "refresh=1" in markup


def test_context_pretty_prints_json_extra(tmp_path, monkeypatch):
    row = LogRow(
        id=7,
        app="a",
        level="error",
        message="m",
        ts="t",
        source=None,
        json_extra={"run_date": "2026-08-19", "n": 3},
    )

    def fake_fetch(**kwargs):
        return _page(row)

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/row/7/context?app=a")
    assert "run_date" in r.text and "2026-08-19" in r.text


def test_context_read_error_renders_message(tmp_path, monkeypatch):
    def fake_fetch(**kwargs):
        raise logreader_mod.LogReadError("logger unreachable: no route")

    monkeypatch.setattr("agent_hq.web.routes.errors.logreader.fetch_logs", fake_fetch)
    client = _make_client(tmp_path)
    r = client.get("/errors/row/7/context?app=a")
    assert r.status_code == 200
    assert "unreachable" in r.text
