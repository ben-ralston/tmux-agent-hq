"""Tests for /login and /logout routes via the app factory."""

from fastapi.testclient import TestClient

from agent_hq.web.app import create_app
from agent_hq.web.auth import COOKIE_NAME, TokenManager, derive_cookie_value


def _app(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = create_app(token_file=tm.token_file)
    return app, tm


def test_get_login_renders(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert "token" in r.text.lower()


def test_post_login_success_sets_cookie_and_redirects(tmp_path):
    app, tm = _app(tmp_path)
    client = TestClient(app)
    correct = tm.token_file.read_text()
    r = client.post("/login", data={"token": correct}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/agents"
    assert COOKIE_NAME in r.cookies


def test_post_login_wrong_token_rerenders_with_error(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert r.status_code == 200
    assert "invalid" in r.text.lower()


def test_post_logout_clears_cookie_and_redirects(tmp_path):
    app, tm = _app(tmp_path)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_get_login_with_token_query_prefills_form(tmp_path):
    """``GET /login?token=<value>`` is the QR-scan flow — server pre-fills
    the input so the page's bootstrap JS can auto-submit.

    The cookie isn't set on this GET; that happens on the POST that the
    page's JS triggers immediately after load. (Setting the cookie on
    GET would echo the token in the URL bar — undesirable.)
    """
    app, tm = _app(tmp_path)
    client = TestClient(app)
    correct = tm.token_file.read_text()

    r = client.get(f"/login?token={correct}", follow_redirects=False)
    assert r.status_code == 200
    # The token is rendered into the form's value attribute
    assert correct in r.text
    # Page contains the auto-submit bootstrap
    assert "submit()" in r.text or ".submit()" in r.text


def test_get_login_without_token_query_does_not_prefill(tmp_path):
    """No ``?token=`` → no prefill; the field should be empty (no value attr,
    or an empty value)."""
    app, _ = _app(tmp_path)
    client = TestClient(app)

    r = client.get("/login")
    assert r.status_code == 200
    # No token value should appear in the rendered form input.
    # The input either has no value attribute or value="" — either is fine.
    assert 'value=""' in r.text or "value=" not in r.text.split('name="token"')[1].split(">")[0]


def test_get_login_with_invalid_token_query_still_renders_form(tmp_path):
    """Even a bogus ``?token=`` value just pre-fills the form; the POST that
    the bootstrap JS triggers will get the standard "Invalid token." rerender.
    The GET handler is intentionally not validating — that's the POST's job."""
    app, _ = _app(tmp_path)
    client = TestClient(app)

    r = client.get("/login?token=not-a-real-token", follow_redirects=False)
    assert r.status_code == 200
    assert "not-a-real-token" in r.text
