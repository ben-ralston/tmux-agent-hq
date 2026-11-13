"""Unified-logger events for remote mode (#396).

The vendored notes-graph ``auth.py`` and ``request_log.py`` call into this
module (notes-graph #180): ``TokenExchangeMonitor`` and
``UnhandledExceptionHandler``. This is agent-hq's implementation of that
interface, over ``agent_hq.eventlog`` instead of notes-graph's unified_log.
Every event has a stable literal message and metadata only - exception
TYPES, logger names, HTTP status codes, GitHub's fixed error codes - never
session names, issue titles, tokens, codes or the client secret. Nothing
here raises.

- ``report_config_failure`` / ``report_server_failure``: the ``serve``
  process is about to exit. Delivered synchronously, and throttled across
  launchd's 10 s restarts by a small stamp file.
- ``UnhandledExceptionHandler``: a root logging handler that turns ERROR
  records carrying a real fault into one event.
- ``TokenExchangeMonitor``: one event for a streak of failed GitHub
  code-for-token exchanges, at most once an hour.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from agent_hq import eventlog

CONFIG_FAILED = "remote config or secret failed to load"
SERVER_FAILED = "remote server exited with an error"
UNHANDLED = "remote server: unhandled exception"
EXCHANGE_FAILING = "remote GitHub token exchange failing repeatedly"

RESTART_WINDOW_S = 3600
STAMP_FILE = Path.home() / ".config" / "agent-hq" / "remote-logger-stamps.json"

UNHANDLED_WINDOW_S = 600
_MAX_UNHANDLED_KEYS = 256

EXCHANGE_FAILURE_THRESHOLD = 3
EXCHANGE_WINDOW_S = 3600
_GITHUB_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


def _due(key: str, clock: Callable[[], float]) -> bool:
    """True (and stamp ``key``) unless it was reported within the window."""
    now = clock()
    try:
        stamps = json.loads(STAMP_FILE.read_text())
        if not isinstance(stamps, dict):
            stamps = {}
    except (OSError, ValueError):
        stamps = {}
    last = stamps.get(key)
    if isinstance(last, (int, float)) and 0 <= now - last < RESTART_WINDOW_S:
        return False
    stamps[key] = now
    try:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STAMP_FILE.with_name(STAMP_FILE.name + ".tmp")
        tmp.write_text(json.dumps(stamps))
        os.replace(tmp, STAMP_FILE)
    except OSError:
        pass  # an unwritable stamp costs throttling, not the event
    return True


def _report_exit(key: str, message: str, extra: dict, clock) -> None:
    try:
        if _due(key, clock):
            eventlog.emit("error", message, extra, sync=True)
    except Exception:
        pass


def report_config_failure(exc: BaseException, clock: Callable[[], float] = time.time) -> None:
    """The remote: block or the Keychain secret failed to load at ``serve``.
    The error text can name paths or config values, so only its type goes out."""
    _report_exit("config", CONFIG_FAILED, {"error_type": type(exc).__name__}, clock)


def report_server_failure(
    phase: str, exc: BaseException, clock: Callable[[], float] = time.time
) -> None:
    """Building or running the server raised (``phase`` = build | serve).
    uvicorn answers a bind failure with ``SystemExit(1)``; a clean
    ``SystemExit(0)`` or Ctrl-C is not a fault."""
    if isinstance(exc, KeyboardInterrupt):
        return
    if isinstance(exc, SystemExit) and exc.code in (0, None):
        return
    _report_exit(
        f"server:{phase}",
        SERVER_FAILED,
        {"phase": phase, "error_type": type(exc).__name__},
        clock,
    )


def _root_error(exc: BaseException) -> BaseException:
    """The innermost cause/context of ``exc`` (cycle-safe)."""
    seen = {id(exc)}
    while True:
        nxt = exc.__cause__ or exc.__context__
        if nxt is None or id(nxt) in seen:
            return exc
        seen.add(id(nxt))
        exc = nxt


class UnhandledExceptionHandler(logging.Handler):
    """ERROR records with a fault attached become one event: logger name,
    level and the root exception type only.

    Must sit BEFORE the redacting stderr handler on the root logger - that
    handler's filter strips ``exc_info`` from the shared record. Faults an
    agent-hq boundary already emitted (``eventlog.mark_reported``) are skipped.
    """

    def __init__(
        self,
        window_s: float = UNHANDLED_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(level=logging.ERROR)
        self._window = window_s
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = record.exc_info[1] if record.exc_info else None
            if exc is None:
                return
            root = _root_error(exc)
            if eventlog.was_reported(exc) or eventlog.was_reported(root):
                return
            error_type = type(root).__name__
            key = (record.name, error_type)
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                return
            if len(self._last) >= _MAX_UNHANDLED_KEYS:
                self._last.clear()
            self._last[key] = now
            eventlog.mark_reported(root)
            eventlog.emit(
                "error",
                UNHANDLED,
                {"logger": record.name, "level": record.levelname, "error_type": error_type},
            )
        except Exception:
            pass


class TokenExchangeMonitor:
    """Counts consecutive failed GitHub exchanges; reports when a streak
    reaches ``threshold``, at most once per ``EXCHANGE_WINDOW_S``. A streak
    usually means a rotated or wrong client secret, or GitHub being down.
    ``bad_verification_code`` (a tampered, replayed or expired code) is client
    input, not a fault, and never counts."""

    def __init__(
        self,
        threshold: int = EXCHANGE_FAILURE_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = threshold
        self._clock = clock
        self._streak = 0
        self._last_report: float | None = None

    def success(self) -> None:
        self._streak = 0

    def failure(self, exc: BaseException) -> None:
        try:
            if getattr(exc, "code", None) == "bad_verification_code":
                return
            self._streak += 1
            if self._streak < self.threshold:
                return
            now = self._clock()
            if self._last_report is not None and now - self._last_report < EXCHANGE_WINDOW_S:
                return
            self._last_report = now
            extra: dict = {"failures": self._streak, "error_type": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                extra["status"] = exc.response.status_code
            code = getattr(exc, "code", None)
            if isinstance(code, str) and _GITHUB_ERROR_CODE.fullmatch(code):
                extra["upstream_error"] = code
            eventlog.emit("error", EXCHANGE_FAILING, extra)
        except Exception:
            pass
