"""Request log and process logging for remote mode (issue #167, brief §2).

``RequestLog``: one JSON object per line - time, event, and only metadata:
HTTP method, path (truncated, never the query string, which carries OAuth codes
and state), status, tool name, decision, GitHub login. Never arguments,
results, note contents, headers or tokens. Size-capped with one rotated copy.

``configure_remote_logging``: everything else the process logs (the MCP SDK,
uvicorn, the agent_hq library) goes to stderr - launchd's server.log -
at WARNING and above, with message text and tracebacks redacted for every
library logger, since exception messages can carry session names and issue titles.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_PATH_CHARS = 200


class RequestLog:
    def __init__(self, path: Optional[Path], max_bytes: int = MAX_LOG_BYTES):
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming > self.max_bytes:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))

    def event(self, event: str, **fields) -> None:
        if not self.path:
            return
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        entry.update(fields)
        if isinstance(entry.get("path"), str):
            entry["path"] = entry["path"][:MAX_PATH_CHARS]
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self._rotate_if_needed(len(line.encode()))
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)


class RequestLogMiddleware:
    """ASGI middleware: one ``http`` event per request (method, path, status)."""

    def __init__(self, app, log: RequestLog):
        self.app = app
        self.log = log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.log.event(
                "http",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status["code"],
                ms=round((time.monotonic() - started) * 1000),
            )


class _RedactLibraryRecords(logging.Filter):
    """Keep only logger name, level and exception type for library records.

    The remote package's own records pass through. uvicorn records without an
    exception pass too (startup/shutdown lines carry no note data).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("agent_hq.remote"):
            return True
        if record.name.startswith("uvicorn") and not record.exc_info:
            return True
        exc = (
            record.exc_info[0].__name__
            if record.exc_info and record.exc_info[0]
            else None
        )
        record.msg = (
            f"{record.levelname} from {record.name}"
            + (f" ({exc})" if exc else "")
            + " - details redacted in remote mode"
        )
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_remote_logging(stream: Optional[TextIO] = None) -> None:
    """Route all process logging through one redacting stderr handler at WARNING.

    Unhandled exceptions also go to the unified logger (#170); that handler
    runs first, since the redacting filter strips ``exc_info`` from the record.
    """
    from .logger_events import UnhandledExceptionHandler

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(_RedactLibraryRecords())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [UnhandledExceptionHandler(), handler]
    root.setLevel(logging.WARNING)
    for name in (
        "mcp",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "agent_hq",
        "httpx",
    ):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
