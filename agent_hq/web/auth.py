"""Token management + cookie middleware for the web UI.

Threat model: the server runs behind Tailscale. The token file at
``~/.config/agent-hq/web-token`` is the source of truth for who can log in.
The cookie is an HMAC of the token bytes keyed by the file's mtime_ns — so
rotating (or even ``touch``ing) the file invalidates every outstanding cookie
without needing a server-side session store.

A stolen cookie stays valid until the file is rotated; this is acceptable
under the tailnet-only threat model.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "agent_hq_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days

EXEMPT_PREFIXES = ("/login", "/logout", "/static/", "/healthz", "/sw.js", "/offline")


class TokenManager:
    """Loads / creates the shared token file, exposes verify helpers."""

    def __init__(self, token_file: Path):
        self.token_file = token_file

    def ensure(self) -> None:
        """Create the token file with a fresh 32-byte secret if missing."""
        if not self.token_file.exists():
            self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Created 0600 in one step: a write-then-chmod leaves the secret
            # at umask permissions for a moment (#263 F3).
            try:
                fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            with os.fdopen(fd, "w") as f:
                # 32 random bytes, hex-encoded → 64 chars
                f.write(secrets.token_hex(32))

    def _token_bytes(self) -> bytes:
        return self.token_file.read_bytes().strip()

    def _key(self) -> bytes:
        """Per-file-version key: derived from mtime so rotation invalidates cookies."""
        mtime_ns = self.token_file.stat().st_mtime_ns
        return hashlib.sha256(f"agent-hq:{mtime_ns}".encode()).digest()

    def signing_key(self) -> bytes:
        """Key for signing user-state cookies (e.g. ``dispatch_state``).

        A deliberately separate contract from ``_key()`` (#82): ``_key()``
        is the *rotation* key — mtime-only, its one job is "touch the file
        → invalidate outstanding auth cookies". This key additionally binds
        signatures to the token *bytes*, so state-cookie validity can't be
        silently changed by a future tweak to ``_key()``'s semantics.
        Rotation still invalidates state cookies because the mtime is
        mixed in too.
        """
        mtime_ns = self.token_file.stat().st_mtime_ns
        material = b"agent-hq:state-sign:" + self._token_bytes() + f":{mtime_ns}".encode()
        return hashlib.sha256(material).digest()

    def verify_user_token(self, supplied: str) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(supplied.strip().encode(), self._token_bytes())


def derive_cookie_value(tm: TokenManager) -> str:
    """Cookie value = HMAC(mtime_key, token_bytes) as hex."""
    return hmac.new(tm._key(), tm._token_bytes(), hashlib.sha256).hexdigest()


def verify_cookie_value(cookie: str, tm: TokenManager) -> bool:
    if not cookie:
        return False
    expected = derive_cookie_value(tm)
    # bytes, not str: compare_digest raises TypeError on non-ASCII str,
    # which would 500 pre-auth in AuthMiddleware for a crafted cookie.
    return hmac.compare_digest(cookie.encode(), expected.encode())


def is_secure_request(request: Request) -> bool:
    """Detect HTTPS so the `Secure` cookie attribute is only set when honored.

    If we set ``Secure`` unconditionally, browsers silently drop the cookie
    on plain-HTTP connections (local dev at 127.0.0.1, or Tailscale-without-serve).
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _extract_bearer(header_value: str) -> str:
    """Return the raw token from an `Authorization: Bearer <token>` header, or ""."""
    if not header_value:
        return ""
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _same_origin_or_unknown(request: Request) -> bool:
    """Is a cookie-authenticated request free of cross-origin forgery?

    The cookie is ``SameSite=lax``, which still rides along from a
    *same-site* origin (another port on the same host, a sibling tailnet
    name), so unsafe methods must prove they came from this app (#263 F5).
    Browsers send ``Sec-Fetch-Site``; when present it decides alone. Older
    browsers fall back to ``Origin`` against ``Host``. A request carrying
    neither header is not from a browser and cannot ride an ambient cookie.
    """
    if request.method in _SAFE_METHODS:
        return True
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()


def _forbidden(path: str):
    if path.startswith("/api/"):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    return PlainTextResponse("cross-origin request refused", status_code=403)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, tm: TokenManager):
        super().__init__(app)
        self.tm = tm

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/login" or any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME, "")
        cookie_ok = verify_cookie_value(cookie, self.tm)
        if cookie_ok and _same_origin_or_unknown(request):
            return await call_next(request)
        # A bearer token is never sent ambiently, so it is not subject to the
        # cookie's origin check — try it before refusing (#263 review).
        bearer = _extract_bearer(request.headers.get("authorization", ""))
        if bearer and self.tm.verify_user_token(bearer):
            return await call_next(request)
        if cookie_ok:
            return _forbidden(path)
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)


def install_auth_middleware(app: FastAPI, tm: TokenManager) -> None:
    app.add_middleware(AuthMiddleware, tm=tm)
