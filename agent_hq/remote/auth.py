"""OAuth for remote mode: a proxy to GitHub limited to one identity (issue #167).

This server is the OAuth authorization server Claude talks to (the MCP SDK's
auth routes supply /authorize, /token, /register, /revoke, the metadata
documents, PKCE S256 checking and the bearer middleware). Sign-in itself is
handed to GitHub, after a consent page of our own:

1. ``authorize`` parks the request under a random state and sends the browser
   to ``/oauth/consent``, which names the registered client and its client_id.
   Approving needs a form POST carrying a single-use CSRF value that must match
   a ``SameSite=Strict`` cookie set by that page, so a link or auto-submitted
   form from another site cannot approve on Ben's behalf (the MCP spec's
   confused-deputy rule for proxies with a static upstream client id: GitHub
   re-approves silently).
2. Only then does the browser go to GitHub, carrying a ``SameSite=Lax`` flow
   cookie that ``github_callback`` requires, so an approval made in another
   browser can't be completed in Ben's. The callback exchanges GitHub's code
   (one overall deadline), fetches ``/user``, and issues our own authorization
   code only when the login AND numeric account id match the one allowed
   identity.
3. Trust on first use: the first client to complete sign-in is pinned. Any
   other client_id gets a refusal page (naming the id) until Ben runs
   ``agent-hq remote allow-client <id>``; ``revoke-all`` clears the pin.

Tokens are opaque random strings stored only as SHA-256 hashes, in a chmod-600
JSON file written under an ``flock`` shared with the CLI, and re-read whenever
the file changes (inode/mtime/size), so ``revoke-all`` and ``allow-client``
take effect on a running server immediately. ``revoke-all`` also bumps an
epoch: every write that grants access (the pin, a code, a token pair) checks
it under the store lock, so sign-ins and token exchanges already in flight
store nothing. Access tokens live 1h; refresh tokens rotate on every use.
Every grant belongs to a token family started by one authorization code:
replaying a used code or a rotated refresh token revokes the whole family
(OAuth 2.0 security BCP reuse detection).
"""

import copy
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode

import anyio
import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import RemoteConfig
from .logger_events import TokenExchangeMonitor
from .request_log import RequestLog

AUTH_CODE_TTL = 300
UPSTREAM_STATE_TTL = 600
UPSTREAM_TIMEOUT = 8.0  # per upstream request
UPSTREAM_DEADLINE = 8.0  # whole token exchange + /user, under Claude's ~10s budget
MAX_CLIENTS = 25  # dynamic registration is open; keep only the newest unused clients
MAX_PENDING = 50  # sign-ins in flight; the oldest is dropped beyond this
MAX_PENDING_PER_CLIENT = 5  # so one client cannot crowd out another's sign-in
CLIENT_NAME_MAX = 80
SCOPE_MAX = 200
REUSE_TOMBSTONE_TTL = (
    24 * 3600
)  # how long a used code is remembered for reuse detection
CONSENT_COOKIE = "ahq_consent"
FLOW_COOKIE = "ahq_flow"
_PAGE_HEADERS = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")


class StoreFullError(Exception):
    """Every client slot belongs to a client with live grants or a sign-in in flight."""


class RevokedError(Exception):
    """``revoke-all`` ran since the sign-in or token exchange began; nothing was stored."""


class GitHubTokenError(ValueError):
    """GitHub's token endpoint returned no access token. ``code`` is its
    ``error`` field (e.g. ``incorrect_client_credentials``), if any."""

    def __init__(self, code=None):
        super().__init__("GitHub returned no access token")
        self.code = code


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _live(entry: dict, now: float) -> bool:
    return (entry.get("expires_at") or now + 1) > now


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{title}</title>"
        f"<body style='font-family:system-ui;max-width:36em;margin:3em auto'>"
        f"<h1>{title}</h1>{body}</body>",
        status_code=status_code,
        headers=_PAGE_HEADERS,
    )


class TokenStore:
    """Hashed tokens, codes, registered clients and the client pin in ``<state_dir>/tokens.json``."""

    SECTIONS = ("clients", "codes", "access", "refresh", "used")

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "tokens.json"
        self._lock = threading.Lock()
        self._cache: Optional[tuple[tuple, dict]] = None

    @contextmanager
    def _file_lock(self):
        """Exclusive lock shared by every process using this store (server + CLI)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        fd = os.open(self.state_dir / "tokens.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _transaction(self):
        with self._lock, self._file_lock():
            yield

    @staticmethod
    def _normalize(data: dict) -> dict:
        out = {s: data.get(s) or {} for s in TokenStore.SECTIONS}
        meta = data.get("meta") or {}
        out["meta"] = {
            "epoch": int(meta.get("epoch") or 0),
            "allowed_clients": list(meta.get("allowed_clients") or []),
        }
        return out

    def _read(self) -> dict:
        """The current store, parsed only when the file changed. Treat as read-only."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return self._normalize({})
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        try:
            data = self._normalize(json.loads(self.path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            data = self._normalize({})
        self._cache = (key, data)
        return data

    def _load(self) -> dict:
        """A private, mutable copy for read-modify-write."""
        return copy.deepcopy(self._read())

    def _save(self, data: dict) -> None:
        now = time.time()
        for section in ("codes", "access", "refresh", "used"):
            data[section] = {k: v for k, v in data[section].items() if _live(v, now)}
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        finally:
            self._cache = None

    def _get(self, section: str, key: str) -> Optional[dict]:
        with self._transaction():
            entry = self._read()[section].get(key)
        if entry and not _live(entry, time.time()):
            return None
        return entry

    def _put(self, section: str, key: str, value: dict) -> None:
        with self._transaction():
            data = self._load()
            data[section][key] = value
            self._save(data)

    def _pop(self, section: str, key: str) -> Optional[dict]:
        with self._transaction():
            data = self._load()
            entry = data[section].pop(key, None)
            if entry is not None:
                self._save(data)
        return entry

    # --- meta: revocation epoch + pinned clients ----------------------
    def epoch(self) -> int:
        with self._transaction():
            return self._read()["meta"]["epoch"]

    @staticmethod
    def _check_epoch(data: dict, epoch: int) -> None:
        """Call inside a transaction, before a write that grants access."""
        if data["meta"]["epoch"] != epoch:
            raise RevokedError("revoke-all ran since this grant began")

    def client_allowed(self, client_id: str) -> bool:
        """True when no client is pinned yet, or this one is."""
        with self._transaction():
            allowed = self._read()["meta"]["allowed_clients"]
        return not allowed or client_id in allowed

    def pin_if_first(self, client_id: str, *, epoch: int) -> bool:
        """Pin ``client_id`` if nothing is pinned yet; return whether it is allowed.

        Atomic under the store lock, so of two sign-ins racing through the
        GitHub round trip exactly one wins the pin and the other is refused.
        Raise ``RevokedError`` if ``revoke-all`` ran since ``epoch`` (read when
        the sign-in began).
        """
        with self._transaction():
            data = self._load()
            self._check_epoch(data, epoch)
            if not data["meta"]["allowed_clients"]:
                data["meta"]["allowed_clients"] = [client_id]
                self._save(data)
                return True
            return client_id in data["meta"]["allowed_clients"]

    def allow_client(self, client_id: str) -> None:
        with self._transaction():
            data = self._load()
            if client_id not in data["meta"]["allowed_clients"]:
                data["meta"]["allowed_clients"].append(client_id)
                self._save(data)

    # --- clients (keyed by client_id, which is not a secret) ------------
    def get_client(self, client_id: str) -> Optional[dict]:
        return self._get("clients", client_id)

    def put_client(
        self, client_id: str, info: dict, protect: Iterable[str] = ()
    ) -> None:
        """Store a client, evicting the oldest UNUSED ones beyond MAX_CLIENTS.

        Clients holding live grants, the pinned clients, and ``protect`` (sign-ins
        in flight) are never evicted - registration is unauthenticated, so a
        flood must not log the real connector out. If every slot is protected,
        the registration is refused.
        """
        with self._transaction():
            data = self._load()
            clients = data["clients"]
            clients[client_id] = info
            if len(clients) > MAX_CLIENTS:
                now = time.time()
                keep = set(protect) | set(data["meta"]["allowed_clients"])
                keep |= {
                    v.get("client_id")
                    for section in ("codes", "access", "refresh")
                    for v in data[section].values()
                    if _live(v, now)  # expired grants protect nothing
                }
                evictable = sorted(
                    (k for k in clients if k not in keep and k != client_id),
                    key=lambda k: clients[k].get("client_id_issued_at") or 0,
                )
                while len(clients) > MAX_CLIENTS and evictable:
                    del clients[evictable.pop(0)]
                if len(clients) > MAX_CLIENTS:
                    raise StoreFullError("every client slot is in use")
            self._save(data)

    # --- codes and tokens (keyed by hash) -------------------------------
    def get_code(self, code: str) -> Optional[dict]:
        return self._get("codes", _hash(code))

    def put_code(self, code: str, value: dict, *, epoch: int) -> None:
        """Store a code, unless ``revoke-all`` ran since ``epoch`` (read when the
        sign-in began): then raise ``RevokedError``."""
        with self._transaction():
            data = self._load()
            self._check_epoch(data, epoch)
            data["codes"][_hash(code)] = value
            self._save(data)

    def consume_code(self, code: str, family: str) -> tuple[Optional[dict], int]:
        """Pop a code (single use), remembering it so a replay revokes ``family``.

        Also return the store epoch read in the same transaction, for
        ``put_tokens`` to check.
        """
        with self._transaction():
            data = self._load()
            epoch = data["meta"]["epoch"]
            entry = data["codes"].pop(_hash(code), None)
            if entry is None:
                return None, epoch
            data["used"]["c:" + _hash(code)] = {
                "family": family,
                "expires_at": time.time() + REUSE_TOMBSTONE_TTL,
            }
            self._save(data)
        return entry, epoch

    def get_access(self, token: str) -> Optional[dict]:
        return self._get("access", _hash(token))

    def put_access(self, token: str, value: dict) -> None:
        self._put("access", _hash(token), value)

    def pop_access(self, token: str) -> Optional[dict]:
        return self._pop("access", _hash(token))

    def get_refresh(self, token: str) -> Optional[dict]:
        return self._get("refresh", _hash(token))

    def put_tokens(
        self,
        access: str,
        access_value: dict,
        refresh: str,
        refresh_value: dict,
        *,
        epoch: int,
    ) -> None:
        """Store a new access + refresh pair, unless ``revoke-all`` ran since
        ``epoch`` (read when the old grant was taken): then raise ``RevokedError``."""
        with self._transaction():
            data = self._load()
            self._check_epoch(data, epoch)
            data["access"][_hash(access)] = access_value
            data["refresh"][_hash(refresh)] = {
                **refresh_value,
                "access_hash": _hash(access),
            }
            self._save(data)

    def pop_refresh(
        self, token: str, remember: bool = False
    ) -> tuple[Optional[dict], int]:
        """Remove a refresh token and the access token issued alongside it.

        With ``remember`` (rotation), keep a tombstone so a replay of this
        refresh token revokes its family. Also return the store epoch read
        in the same transaction, for ``put_tokens`` to check.
        """
        with self._transaction():
            data = self._load()
            epoch = data["meta"]["epoch"]
            entry = data["refresh"].pop(_hash(token), None)
            if entry is None:
                return None, epoch
            data["access"].pop(entry.get("access_hash", ""), None)
            if remember and entry.get("family"):
                data["used"]["r:" + _hash(token)] = {
                    "family": entry["family"],
                    "expires_at": entry.get("expires_at"),
                }
            self._save(data)
        return entry, epoch

    def revoke_reused(self, kind: str, token: str) -> Optional[str]:
        """If ``token`` was already used (kind "c" code / "r" refresh), revoke
        every grant in its family and return the family id."""
        with self._transaction():
            data = self._load()
            tomb = data["used"].get(f"{kind}:{_hash(token)}")
            if not tomb or not _live(tomb, time.time()):
                return None
            family = tomb["family"]
            for section in ("access", "refresh"):
                data[section] = {
                    k: v for k, v in data[section].items() if v.get("family") != family
                }
            self._save(data)
        return family

    def revoke_all(self) -> int:
        """Wipe every token, code and pin, and bump the epoch; return the count.

        Bumping the epoch cancels sign-ins already in flight in a running
        server. Registered clients are kept: a client_id alone grants nothing.
        """
        with self._transaction():
            data = self._load()
            count = sum(len(data[s]) for s in ("codes", "access", "refresh"))
            data.update(codes={}, access={}, refresh={}, used={})
            data["meta"]["epoch"] += 1
            data["meta"]["allowed_clients"] = []
            self._save(data)
        return count


class GitHubOAuthProvider:
    """OAuthAuthorizationServerProvider that delegates sign-in to GitHub."""

    def __init__(
        self,
        config: RemoteConfig,
        store: TokenStore,
        log: RequestLog,
        http_client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
    ):
        self.config = config
        self.store = store
        self.log = log
        self._http = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
        )
        self.exchange_monitor = TokenExchangeMonitor()  # unified logger (#170)
        # state -> {expires_at, epoch, client_id, params, csrf, consented, flow}; memory only,
        # insertion-ordered so the oldest can be dropped beyond MAX_PENDING
        self._pending: dict[str, dict] = {}

    # --- clients -------------------------------------------------------
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if str(uri) not in self.config.redirect_uri_allowlist:
                self.log.event(
                    "register", decision="deny", reason="redirect_uri not allowed"
                )
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="redirect_uri is not an allowed Claude callback",
                )
        # Keep only what the flow uses, with length caps: registration is open.
        record = {
            "client_id": client_info.client_id,
            "client_id_issued_at": client_info.client_id_issued_at,
            "client_secret": client_info.client_secret,
            "client_secret_expires_at": client_info.client_secret_expires_at,
            "redirect_uris": [str(u) for u in client_info.redirect_uris or []],
            "token_endpoint_auth_method": client_info.token_endpoint_auth_method,
            "grant_types": list(client_info.grant_types or []),
            "response_types": list(client_info.response_types or []),
            "client_name": (client_info.client_name or "")[:CLIENT_NAME_MAX] or None,
            "scope": (client_info.scope or "")[:SCOPE_MAX] or None,
        }
        try:
            self.store.put_client(
                client_info.client_id,
                record,
                protect=self._protected_clients(),
            )
        except StoreFullError:
            self.log.event("register", decision="deny", reason="client limit reached")
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="registration limit reached; run revoke-all to reset",
            )
        self.log.event(
            "register", decision="allow", client_name=record["client_name"] or ""
        )

    def _protected_clients(self) -> set:
        """Clients with a live, current-epoch, allowed sign-in in flight."""
        self._live_pending("")
        return {
            p["client_id"] for p in self._pending.values() if self.store.client_allowed(p["client_id"])
        }

    # --- sign-in -------------------------------------------------------
    def _live_pending(self, state: str) -> Optional[dict]:
        now = time.time()
        epoch = self.store.epoch()
        self._pending = {
            k: v
            for k, v in self._pending.items()
            if v["expires_at"] > now and v["epoch"] == epoch
        }
        return self._pending.get(state) if state else None

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not self.store.client_allowed(client.client_id):
            # Pinned to another client: park nothing (no eviction pressure on
            # Ben's sign-ins) and show the refusal page with this client_id.
            self.log.event("authorize", decision="deny", reason="client not pinned")
            return f"{self.config.public_url}/oauth/not-allowed?{urlencode({'client_id': client.client_id})}"
        self._live_pending("")
        mine = [k for k, v in self._pending.items() if v["client_id"] == client.client_id]
        while len(mine) >= MAX_PENDING_PER_CLIENT:
            del self._pending[mine.pop(0)]
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        state = secrets.token_urlsafe(32)
        self._pending[state] = {
            "expires_at": time.time() + UPSTREAM_STATE_TTL,
            "epoch": self.store.epoch(),
            "client_id": client.client_id,
            "params": params,
            "csrf": None,
            "consented": False,
            "flow": None,
        }
        return f"{self.config.public_url}/oauth/consent?{urlencode({'state': state})}"

    def _not_pinned_page(self, client_id: str) -> Response:
        self.log.event("consent", decision="deny", reason="client not pinned")
        if client_id == "unknown":
            return _page(
                "This connector client isn't allowed",
                "<p>This server is pinned to a different connector client. Close this tab.</p>",
                403,
            )
        cid = html.escape(client_id)
        return _page(
            "This connector client isn't allowed",
            f"<p>Client ID <code style='font-size:1.1em'>{cid}</code> is not the client "
            "this server is pinned to.</p>"
            "<p>If you just re-added the connector in claude.ai yourself, allow it with "
            f"<code>agent-hq remote allow-client {cid}</code> (or reset everything with "
            "<code>agent-hq remote revoke-all</code>) and try again. Otherwise, close this tab.</p>",
            403,
        )

    async def not_allowed_page(self, request: Request) -> Response:
        # Only name a well-formed client_id that is actually registered, so a
        # crafted link can't make this page vouch for arbitrary text.
        client_id = request.query_params.get("client_id", "")
        if not _CLIENT_ID_RE.fullmatch(client_id) or self.store.get_client(client_id) is None:
            client_id = "unknown"
        return self._not_pinned_page(client_id)

    async def consent_page(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        pending = self._live_pending(state)
        if pending is None:
            return _page("Sign-in expired", "<p>Start again from Claude.</p>", 400)
        if not self.store.client_allowed(pending["client_id"]):
            return self._not_pinned_page(pending["client_id"])
        client = await self.get_client(pending["client_id"])
        name = html.escape(
            (client.client_name if client else None) or "an unnamed client"
        )
        issued = client.client_id_issued_at if client else None
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(issued))
            if issued
            else "unknown"
        )
        redirect = html.escape(str(pending["params"].redirect_uri))
        cid = html.escape(pending["client_id"])
        pending["csrf"] = secrets.token_urlsafe(32)
        response = _page(
            "Allow access to your agent-hq sessions and backlog?",
            f"<p><b>{name}</b> (registered {when}) wants to read your agent-hq sessions and backlog through "
            f"this connector.</p>"
            f"<p>Client ID: <code style='font-size:1.1em'>{cid}</code></p>"
            f"<p>After GitHub sign-in it will be sent to <code>{redirect}</code>.</p>"
            "<p>Only approve if you just clicked <i>Connect</i> in claude.ai yourself. "
            "If you didn't, close this tab.</p>"
            "<form method=post action=/oauth/consent>"
            f'<input type="hidden" name="state" value="{html.escape(state)}">'
            f'<input type="hidden" name="csrf" value="{pending["csrf"]}">'
            '<button type="submit" name="decision" value="approve">Approve</button>'
            "</form>",
        )
        response.set_cookie(
            CONSENT_COOKIE,
            pending["csrf"],
            max_age=UPSTREAM_STATE_TTL,
            path="/oauth",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    async def consent_submit(self, request: Request) -> Response:
        form = await request.form()
        state = str(form.get("state", ""))
        pending = self._live_pending(state)
        csrf = str(form.get("csrf", ""))
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        if (
            pending is None
            or not pending["csrf"]
            or not csrf
            or not hmac.compare_digest(csrf, pending["csrf"])
            or not hmac.compare_digest(cookie, pending["csrf"])
            or form.get("decision") != "approve"
        ):
            self.log.event("consent", decision="deny", reason="missing/forged consent")
            return _page("Consent not accepted", "<p>Start again from Claude.</p>", 403)
        if not self.store.client_allowed(pending["client_id"]):
            return self._not_pinned_page(pending["client_id"])
        pending["csrf"] = None  # single use
        pending["consented"] = True
        # Ties the GitHub callback to THIS browser: an approval made elsewhere
        # can't be completed by sending Ben the GitHub URL. Lax, because the
        # GitHub -> callback redirect is a cross-site top-level navigation.
        pending["flow"] = secrets.token_urlsafe(32)
        self.log.event("consent", decision="allow")
        query = urlencode(
            {
                "client_id": self.config.github_client_id,
                "redirect_uri": self.config.github_callback_url,
                "state": state,
                "allow_signup": "false",
            }
        )
        response = RedirectResponse(
            f"{self.config.github_authorize_url}?{query}", status_code=302
        )
        response.set_cookie(
            FLOW_COOKIE,
            pending["flow"],
            max_age=UPSTREAM_STATE_TTL,
            path="/oauth",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    def _identity_allowed(self, user: dict) -> bool:
        login = user.get("login")
        if (
            not isinstance(login, str)
            or login.casefold() != self.config.allowed_github_login.casefold()
        ):
            return False
        # Fail closed: a login alone can be renamed or re-registered by someone else.
        if self.config.allowed_github_id is None:
            return False
        return user.get("id") == self.config.allowed_github_id

    async def _github_user(self, code: str) -> dict:
        async with self._http() as http:
            r = await http.post(
                self.config.github_token_url,
                data={
                    "client_id": self.config.github_client_id,
                    "client_secret": self.config.github_client_secret or "",
                    "code": code,
                    "redirect_uri": self.config.github_callback_url,
                },
                headers={"accept": "application/json"},
            )
            r.raise_for_status()
            body = r.json()
            upstream_token = body.get("access_token")
            if not upstream_token:
                raise GitHubTokenError(body.get("error"))
            r = await http.get(
                self.config.github_user_url,
                headers={
                    "authorization": f"Bearer {upstream_token}",
                    "accept": "application/vnd.github+json",
                },
            )
            r.raise_for_status()
            return r.json()

    async def github_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code")
        pending = self._live_pending(state)
        flow = request.cookies.get(FLOW_COOKIE, "")
        if (
            pending is None
            or not pending["consented"]
            or not code
            or not hmac.compare_digest(flow, pending.get("flow") or "")
        ):
            self.log.event(
                "sign_in",
                decision="deny",
                reason="unknown, expired, revoked or unconsented state, or no code",
            )
            return _page("Sign-in failed", "<p>Start again from Claude.</p>", 400)
        del self._pending[state]  # single use
        params: AuthorizationParams = pending["params"]
        client_id = pending["client_id"]
        if not self.store.client_allowed(client_id):
            return self._not_pinned_page(client_id)

        try:
            with anyio.fail_after(UPSTREAM_DEADLINE):
                user = await self._github_user(code)
        except (httpx.HTTPError, ValueError, TimeoutError) as e:
            self.exchange_monitor.failure(e)
            self.log.event(
                "sign_in", decision="deny", reason=f"upstream error: {type(e).__name__}"
            )
            return _page("Sign-in failed", "<p>GitHub did not answer in time.</p>", 502)
        self.exchange_monitor.success()

        login = user.get("login")
        if not self._identity_allowed(user):
            self.log.event(
                "sign_in", decision="deny", reason="identity not allowed", login=login
            )
            return _page("Access denied", "<p>This account is not allowed.</p>", 403)

        # The pin and the code each check the sign-in's epoch under the store
        # lock, so a revoke-all at any point since /authorize leaves neither.
        our_code = secrets.token_urlsafe(32)
        try:
            # trust on first use (atomic)
            if not self.store.pin_if_first(client_id, epoch=pending["epoch"]):
                return self._not_pinned_page(client_id)
            self.store.put_code(
                our_code,
                AuthorizationCode(
                    code=_hash(our_code),
                    scopes=params.scopes or [],
                    expires_at=time.time() + AUTH_CODE_TTL,
                    client_id=client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource,
                    subject=login,
                ).model_dump(mode="json"),
                epoch=pending["epoch"],
            )
        except RevokedError:
            self.log.event("sign_in", decision="deny", reason="revoked during sign-in")
            return _page("Sign-in cancelled", "<p>Tokens were revoked. Start again from Claude.</p>", 400)
        self.log.event("sign_in", decision="allow", login=login)
        return RedirectResponse(
            construct_redirect_uri(
                str(params.redirect_uri), code=our_code, state=params.state
            ),
            status_code=302,
        )

    # --- codes and tokens ----------------------------------------------
    def _reuse_check(self, kind: str, token: str) -> None:
        family = self.store.revoke_reused(kind, token)
        if family:
            self.log.event(
                "token", decision="deny", reason="reuse detected: token family revoked"
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        data = self.store.get_code(authorization_code)
        if not data:
            self._reuse_check("c", authorization_code)
            return None
        if data["client_id"] != client.client_id or not self.store.client_allowed(client.client_id):
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    def _issue(
        self,
        client_id: str,
        scopes: list[str],
        subject: Optional[str],
        resource: Optional[str],
        family: str,
        epoch: int,
        grant: str,
    ) -> OAuthToken:
        """Mint and store a token pair. ``epoch`` was read when the old grant was
        taken, so a ``revoke-all`` in between refuses the exchange."""
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "subject": subject,
            "resource": resource,
            "family": family,
        }
        try:
            self.store.put_tokens(
                access,
                {**common, "expires_at": now + self.config.access_token_ttl},
                refresh,
                {**common, "expires_at": now + self.config.refresh_token_ttl},
                epoch=epoch,
            )
        except RevokedError:
            self.log.event(
                "token",
                decision="deny",
                reason="revoked during the exchange",
                grant=grant,
                subject=subject,
            )
            raise TokenError(error="invalid_grant", error_description="grant revoked") from None
        self.log.event("token", decision="allow", grant=grant, subject=subject)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        family = secrets.token_urlsafe(16)
        data, epoch = self.store.consume_code(authorization_code.code, family)  # single use
        if not data:
            self._reuse_check("c", authorization_code.code)  # lost a concurrent exchange
        if not data or data["client_id"] != client.client_id:
            raise TokenError(
                error="invalid_grant",
                error_description="authorization code already used",
            )
        return self._issue(
            client.client_id,
            data["scopes"],
            data.get("subject"),
            data.get("resource"),
            family,
            epoch,
            "authorization_code",
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        data = self.store.get_refresh(refresh_token)
        if not data:
            self._reuse_check("r", refresh_token)
            return None
        if data["client_id"] != client.client_id or not self.store.client_allowed(client.client_id):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data.get("expires_at"),
            resource=data.get("resource"),
            subject=data.get("subject"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        entry, epoch = self.store.pop_refresh(refresh_token.token, remember=True)  # rotation
        if entry is None:
            self._reuse_check("r", refresh_token.token)  # lost a concurrent exchange
            raise TokenError(
                error="invalid_grant", error_description="refresh token already used"
            )
        return self._issue(
            client.client_id,
            scopes,
            refresh_token.subject,
            refresh_token.resource,
            entry.get("family") or secrets.token_urlsafe(16),
            epoch,
            "refresh_token",
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        data = self.store.get_access(token)
        if not data or not self.store.client_allowed(data["client_id"]):
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data.get("expires_at"),
            resource=data.get("resource"),
            subject=data.get("subject"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self.store.pop_refresh(token.token)
        else:
            self.store.pop_access(token.token)
        self.log.event("revoke", decision="allow", subject=token.subject)
