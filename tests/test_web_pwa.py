"""PWA service worker + offline shell (issue #42)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _app(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    return create_app(token_file=tm.token_file)


def test_offline_route_returns_no_connection_shell(tmp_path):
    """Unauthenticated GET /offline returns the offline shell so the SW
    can precache it without auth, and so it works regardless of cookie state."""
    client = TestClient(_app(tmp_path))
    r = client.get("/offline", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text.lower()
    assert "no connection" in body or "offline" in body


def test_sw_js_served_at_root_with_js_content_type(tmp_path):
    """The SW must be served at scope root (/sw.js, not /static/sw.js)
    so its controlled scope is '/'."""
    client = TestClient(_app(tmp_path))
    r = client.get("/sw.js", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "no-cache" in r.headers.get("cache-control", "").lower()


def test_sw_js_skips_events_and_api(tmp_path):
    """The SW source must explicitly skip /events (SSE) and /api/* so
    real-time + mutation traffic always hits the live server."""
    client = TestClient(_app(tmp_path))
    body = client.get("/sw.js").text
    assert "/events" in body
    assert "/api/" in body


def test_sw_js_precaches_offline_and_static(tmp_path):
    """The SW must precache /offline and the core static assets."""
    client = TestClient(_app(tmp_path))
    body = client.get("/sw.js").text
    assert "/offline" in body
    for asset in (
        "/static/tokens.css",
        "/static/layout.css",
        "/static/components.css",
        "/static/agent_hq.js",
    ):
        assert asset in body, f"missing precache entry: {asset}"


def test_base_html_registers_service_worker(tmp_path):
    """Rendered pages from base.html must contain SW-registration JS that
    points at /sw.js (root scope)."""
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text
    assert "serviceWorker" in body
    assert "/sw.js" in body


def test_base_html_sets_hq_favicon(tmp_path):
    """The browser tab favicon must point at the existing HQ logo SVG so the
    tab matches the installed PWA icon (#115). base.html previously only had
    apple-touch-icon (iOS home screen) + manifest, leaving the tab blank."""
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text
    assert 'rel="icon"' in body
    assert "/static/icons/icon-192.svg" in body


def test_sw_js_static_lookup_is_exact_match_with_ignoresearch_fallback(tmp_path):
    """Regression for multi-review finding #1: the primary /static/* cache
    lookup must respect the ?v= cache-buster (i.e. exact match, NOT
    ignoreSearch). ignoreSearch is only acceptable as a stale fallback
    inside a network-failure ``.catch(...)`` for offline support.

    If someone reintroduces ``caches.match(request, { ignoreSearch: true })``
    as the primary lookup, the server's static_version cache-busting in
    base.html stops working for PWA-installed users — they keep getting
    stale CSS/JS forever.
    """
    client = TestClient(_app(tmp_path))
    body = client.get("/sw.js").text
    # Primary lookup must be exact-match.
    assert "caches.match(request)" in body, (
        "SW must use exact-match caches.match(request) as the primary "
        "/static/* lookup so ?v=NEW doesn't satisfy a cached ?v=OLD."
    )
    # ignoreSearch is allowed ONLY as a fallback inside .catch(...).
    # Heuristic: any occurrence of ignoreSearch must be preceded by ".catch("
    # within a small window — i.e. it's the offline fallback, not the primary.
    if "ignoreSearch" in body:
        # Strip whitespace to make the search tolerant of formatting.
        compact = "".join(body.split())
        assert ".catch(()=>caches.match(request,{ignoreSearch:true}))" in compact, (
            "ignoreSearch is only acceptable as a network-failure fallback "
            "inside .catch(() => caches.match(request, { ignoreSearch: true }))."
        )


def test_sw_js_splits_required_and_optional_precache(tmp_path):
    """Regression for multi-review finding #2: ``cache.addAll(EVERYTHING)``
    is atomic — a single 404 (transient blip, future asset rename) fails
    the whole install and the user gets NO offline shell.

    Split into REQUIRED (shell-critical: /offline + the 4 CSS files) and
    OPTIONAL (icons, vendored JS — fetched on first online use anyway).
    REQUIRED uses addAll (must succeed); OPTIONAL uses Promise.allSettled
    (best-effort).
    """
    client = TestClient(_app(tmp_path))
    body = client.get("/sw.js").text
    # Two distinct precache lists.
    assert "REQUIRED" in body and "OPTIONAL" in body, (
        "SW must split precache URLs into REQUIRED (shell-critical) and "
        "OPTIONAL (best-effort) lists so one bad URL doesn't kill the shell."
    )
    # OPTIONAL must use allSettled so individual failures don't reject the install.
    assert "allSettled" in body, (
        "OPTIONAL precache must use Promise.allSettled so a 404 on one "
        "asset doesn't fail the whole install."
    )
