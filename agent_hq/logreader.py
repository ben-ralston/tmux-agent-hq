"""Read-side client for the unified logger (#148).

Deliberately separate from the vendored write-side ``logclient.py`` (which
must stay byte-identical to its upstream-derived state): the read side uses
the ADMIN token, and unlike ``log()`` it fails loudly — the Errors tab
decides presentation, not this module.

Endpoint/token come from the same convention as the write side: fixed
default paths plus env-var test seams (never caller-facing flags, never
projects.yaml).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_hq.logclient import DEFAULT_URL

USER_AGENT = "agent-hq-logreader/1.0"
TIMEOUT_SECONDS = 10

VALID_LEVELS = ("debug", "info", "warn", "error", "alert")
DEFAULT_LEVELS = ("error", "alert")
WINDOWS = ("1h", "24h", "7d")
DEFAULT_WINDOW = "24h"

_DEFAULT_ADMIN_TOKEN_FILE = Path(os.path.expanduser("~/.claude/secrets/logger-admin.token"))


class LogReadError(Exception):
    """A /logs read failed (missing token, network, HTTP, bad payload)."""


@dataclass
class LogRow:
    id: int
    app: str
    level: str
    message: str
    ts: str
    source: str | None
    json_extra: object | None


@dataclass
class LogPage:
    rows: list[LogRow]
    count: int
    max_id: int | None


def _url() -> str:
    return os.environ.get("LOGGER_URL", DEFAULT_URL).rstrip("/")


def _admin_token_file() -> Path:
    env = os.environ.get("LOGGER_ADMIN_TOKEN_FILE")
    return Path(env) if env else _DEFAULT_ADMIN_TOKEN_FILE


def is_configured() -> bool:
    """Whether the admin token exists on this machine (tab enabled)."""
    try:
        return _admin_token_file().is_file()
    except OSError:
        return False


def _read_token() -> str:
    path = _admin_token_file()
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise LogReadError(f"admin token unreadable at {path}: {exc}") from exc
    if not token:
        raise LogReadError(f"admin token file {path} is empty")
    return token


def _parse_row(raw: dict) -> LogRow:
    extra = raw.get("json_extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, ValueError):
            pass  # keep the raw string — display beats data loss
    return LogRow(
        id=int(raw["id"]),
        app=str(raw.get("app", "")),
        level=str(raw.get("level", "")),
        message=str(raw.get("message", "")),
        ts=str(raw.get("ts", "")),
        source=raw.get("source"),
        json_extra=extra,
    )


def fetch_logs(
    app: str | None = None,
    levels: Sequence[str] | None = None,
    since: str | None = None,
    since_id: int | None = None,
    limit: int = 100,
    order: str | None = None,
) -> LogPage:
    """Query ``GET /logs``. Raises :class:`LogReadError` on any failure."""
    params: dict[str, str] = {"limit": str(limit)}
    if app is not None:
        params["app"] = app
    if levels:
        params["level"] = ",".join(levels)
    if since is not None:
        params["since"] = since
    if since_id is not None:
        params["since_id"] = str(since_id)
    if order is not None:
        params["order"] = order
    token = _read_token()
    request = urllib.request.Request(
        _url() + "/logs?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LogReadError(f"logger returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LogReadError(f"logger unreachable: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise LogReadError(f"logger returned malformed JSON: {exc}") from exc
    try:
        rows = [_parse_row(r) for r in payload["logs"]]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LogReadError(f"logger response missing expected fields: {exc}") from exc
    # count/max_id have no consumer yet — parse leniently so a reshaped
    # field can't take down rows that parsed fine.
    try:
        count = int(payload.get("count"))
    except (TypeError, ValueError):
        count = len(rows)
    max_id = payload.get("max_id")
    if not isinstance(max_id, int):
        max_id = None
    return LogPage(rows=rows, count=count, max_id=max_id)


def group_rows_by_app(rows: list[LogRow]) -> list[tuple[str, list[LogRow]]]:
    """Rows grouped by app; groups ordered by their newest row, rows newest-first."""
    by_app: dict[str, list[LogRow]] = {}
    for row in rows:
        by_app.setdefault(row.app, []).append(row)
    for app_rows in by_app.values():
        app_rows.sort(key=lambda r: r.id, reverse=True)
    return sorted(by_app.items(), key=lambda item: item[1][0].id, reverse=True)


class ErrorsCache:
    """In-memory TTL cache for the default Errors-tab query (#148).

    Same shape as ``AutomationsCache``: ``get_cached()`` never fetches (the
    stale-serve path), ``fetch(loader)`` refreshes under the lock. Only the
    default query (error+alert / 24h) is cached — filtered views are
    explicit user actions and fetch live. A loader failure propagates and
    keeps the previous page.
    """

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, LogPage] | None = None

    def get_cached(self) -> tuple[LogPage, bool] | None:
        with self._lock:
            if self._cached is None:
                return None
            timestamp, page = self._cached
            return page, time.time() - timestamp < self._ttl

    def fetched_at(self) -> float | None:
        """When the loader last succeeded (``time.time()``), or None before that."""
        with self._lock:
            return None if self._cached is None else self._cached[0]

    def fetch(self, loader, force: bool = False) -> LogPage:
        with self._lock:
            if not force and self._cached is not None:
                timestamp, page = self._cached
                if time.time() - timestamp < self._ttl:
                    return page
        # The loader runs OUTSIDE the lock: it's a WAN fetch (up to
        # TIMEOUT_SECONDS), and holding the lock across it would park every
        # concurrent get_cached() stale-serve behind it — defeating
        # stale-while-revalidate exactly when the logger is slow. A rare
        # concurrent double-fetch is harmless (last writer wins).
        page = loader()
        with self._lock:
            self._cached = (time.time(), page)
        return page
