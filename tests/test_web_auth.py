"""Tests for web token management + cookie middleware."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from agent_hq.web.auth import (
    COOKIE_NAME,
    TokenManager,
    derive_cookie_value,
    install_auth_middleware,
    verify_cookie_value,
)

# --- Token manager -----------------------------------------------------------


def test_token_manager_creates_file_if_missing(tmp_path):
    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    assert token_file.exists()
    assert token_file.stat().st_size == 64  # 32 bytes hex-encoded
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_token_manager_creates_file_private_from_the_start(tmp_path, monkeypatch):
    """The secret must never sit on disk at umask permissions, even briefly
    (#263 F3): neutralise any after-the-fact chmod and the file must still be
    0600, and a freshly created parent directory 0700."""
    import os
    from pathlib import Path

    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    old_umask = os.umask(0o022)
    try:
        token_file = tmp_path / "cfg" / "web-token"
        TokenManager(token_file).ensure()
    finally:
        os.umask(old_umask)
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert token_file.parent.stat().st_mode & 0o777 == 0o700
    assert len(token_file.read_text()) == 64


def test_token_manager_reuses_existing_file(tmp_path):
    token_file = tmp_path / "web-token"
    token_file.write_text("deadbeef" * 8)
    token_file.chmod(0o600)
    tm = TokenManager(token_file)
    tm.ensure()
    assert token_file.read_text() == "deadbeef" * 8


def test_derive_and_verify_cookie_round_trip(tmp_path):
    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    cookie = derive_cookie_value(tm)
    assert verify_cookie_value(cookie, tm) is True


def test_verify_cookie_rejects_wrong_value(tmp_path):
    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    assert verify_cookie_value("nonsense", tm) is False


def test_verify_cookie_rejects_empty_string(tmp_path):
    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    assert verify_cookie_value("", tm) is False


def test_verify_cookie_rejects_after_token_rotation(tmp_path):
    import time

    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    cookie = derive_cookie_value(tm)
    # Rotate — sleep to ensure mtime_ns changes
    time.sleep(0.005)
    token_file.write_text("cafebabe" * 8)
    tm_rotated = TokenManager(token_file)
    tm_rotated.ensure()
    assert verify_cookie_value(cookie, tm_rotated) is False


def test_verify_user_supplied_token(tmp_path):
    token_file = tmp_path / "web-token"
    tm = TokenManager(token_file)
    tm.ensure()
    correct = token_file.read_text()
    assert tm.verify_user_token(correct) is True
    assert tm.verify_user_token("wrong") is False
    assert tm.verify_user_token("") is False


# --- Middleware --------------------------------------------------------------


def _build_test_app(tm):
    app = FastAPI()
    install_auth_middleware(app, tm)

    @app.get("/agents")
    def agents():
        return PlainTextResponse("agents-ok")

    @app.get("/login")
    def login():
        return PlainTextResponse("login-ok")

    @app.get("/static/thing.css")
    def static_thing():
        return PlainTextResponse("css-ok")

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.post("/api/idea")
    def api_idea():
        return PlainTextResponse("api-ok")

    return app


def test_middleware_blocks_unauthenticated(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.get("/agents", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_middleware_allows_with_valid_cookie(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)
    cookie_val = derive_cookie_value(tm)

    client.cookies.set(COOKIE_NAME, cookie_val)
    r = client.get("/agents")
    assert r.status_code == 200
    assert r.text == "agents-ok"


def test_middleware_exempts_login_static_healthz(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)

    assert client.get("/login").status_code == 200
    assert client.get("/static/thing.css").status_code == 200
    assert client.get("/healthz").status_code == 200


# --- Bearer auth -------------------------------------------------------------


def test_bearer_token_grants_access_when_cookie_missing(tmp_path):
    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    raw_token = token_file.read_text()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.get("/agents", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.status_code == 200
    assert r.text == "agents-ok"


def test_bearer_token_rejected_when_wrong(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.get(
        "/agents",
        headers={"Authorization": "Bearer wrongvalue"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_api_path_returns_json_401_when_unauthenticated(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.post("/api/idea", json={"text": "hi"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "unauthorized"}


def test_api_path_accepts_valid_bearer(tmp_path):
    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    raw_token = token_file.read_text()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.post(
        "/api/idea",
        json={"text": "hi"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    assert r.text == "api-ok"


def test_api_path_accepts_valid_cookie(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))

    r = client.post("/api/idea", json={"text": "hi"})
    assert r.status_code == 200


def test_bearer_survives_touch_dies_on_rewrite(tmp_path):
    """Bearer is the *raw* token: `touch` (mtime change) must NOT invalidate it,
    but rewriting the file must.
    """
    import time

    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    raw_token = token_file.read_text()
    app = _build_test_app(tm)
    client = TestClient(app)

    # touch — only updates mtime
    time.sleep(0.005)
    token_file.touch()
    r = client.get("/agents", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.status_code == 200

    # rewrite — invalidates bearer
    time.sleep(0.005)
    token_file.write_text("cafebabe" * 8)
    r = client.get(
        "/agents",
        headers={"Authorization": f"Bearer {raw_token}"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def test_malformed_authorization_header_falls_through(tmp_path):
    """Authorization header without the 'Bearer ' prefix must not crash; just be ignored."""
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    app = _build_test_app(tm)
    client = TestClient(app)

    r = client.get(
        "/agents",
        headers={"Authorization": "Basic abcd"},
        follow_redirects=False,
    )
    assert r.status_code == 302


# --- Signing key (#82) --------------------------------------------------------


def test_signing_key_incorporates_token_bytes(tmp_path):
    """#82: ``signing_key`` must differ for different token *content* even
    when the file mtimes are identical. ``_key()`` (the rotation key) is
    mtime-only by design — this pins the two contracts apart."""
    import os

    file_a = tmp_path / "token-a"
    file_b = tmp_path / "token-b"
    file_a.write_text("a" * 64)
    file_b.write_text("b" * 64)
    stat_a = file_a.stat()
    os.utime(file_b, ns=(stat_a.st_atime_ns, stat_a.st_mtime_ns))
    tm_a, tm_b = TokenManager(file_a), TokenManager(file_b)
    # Sanity: the rotation key really is mtime-only, so it collides here.
    assert tm_a._key() == tm_b._key()
    assert tm_a.signing_key() != tm_b.signing_key()


def test_signing_key_changes_on_rotation(tmp_path):
    """Touching/rotating the token file must still invalidate the signing
    key — dispatch cookies keep the rotation-invalidates property."""
    import os

    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    before = tm.signing_key()
    st = token_file.stat()
    os.utime(token_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert tm.signing_key() != before


def test_signing_key_is_stable_across_calls(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    assert tm.signing_key() == tm.signing_key()


class TestNonAsciiCookieValue:
    """#170 audit AHQ-013: verify_cookie_value must return False for a
    non-ASCII cookie, not raise TypeError (pre-auth 500 in AuthMiddleware)."""

    def test_non_ascii_cookie_is_rejected_not_500(self, tmp_path):
        tm = TokenManager(tmp_path / "token")
        tm.ensure()
        assert verify_cookie_value("\xe9abc", tm) is False


# --- Cross-origin request forgery (#263 F5) ----------------------------------


def _cookie_client(tmp_path):
    tm = TokenManager(tmp_path / "token")
    tm.ensure()
    client = TestClient(_build_test_app(tm))
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    return client, tm


def test_cookie_post_from_another_origin_is_rejected(tmp_path):
    """SameSite=lax still sends the cookie from a same-site origin (another
    port on the same host): a cookie-authenticated unsafe request must come
    from the app's own origin."""
    client, _ = _cookie_client(tmp_path)
    r = client.post("/api/idea", headers={"Origin": "http://testserver:9999"})
    assert r.status_code == 403
    r = client.post("/api/idea", headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    r = client.post("/api/idea", headers={"Origin": "null"})
    assert r.status_code == 403


def test_cookie_post_from_own_origin_is_allowed(tmp_path):
    client, _ = _cookie_client(tmp_path)
    r = client.post(
        "/api/idea",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200
    # Non-browser clients send neither header.
    assert client.post("/api/idea").status_code == 200


def test_cookie_get_is_not_origin_checked(tmp_path):
    client, _ = _cookie_client(tmp_path)
    r = client.get("/agents", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_bearer_post_is_not_origin_checked(tmp_path):
    """A bearer token is never sent ambiently, so it cannot be forged."""
    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    client = TestClient(_build_test_app(tm))
    r = client.post(
        "/api/idea",
        headers={
            "Authorization": f"Bearer {token_file.read_text()}",
            "Origin": "http://elsewhere",
        },
    )
    assert r.status_code == 200


def test_cookie_and_bearer_cross_origin_post_uses_the_bearer(tmp_path):
    """A request carrying both a session cookie and a valid bearer token is
    not forgeable (the bearer is never ambient); the cookie's origin check
    must not 403 it before the bearer is considered (#263 review)."""
    token_file = tmp_path / "token"
    tm = TokenManager(token_file)
    tm.ensure()
    client = TestClient(_build_test_app(tm))
    client.cookies.set(COOKIE_NAME, derive_cookie_value(tm))
    r = client.post(
        "/api/idea",
        headers={
            "Authorization": f"Bearer {token_file.read_text()}",
            "Sec-Fetch-Site": "same-site",
        },
    )
    assert r.status_code == 200
