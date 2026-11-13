"""ASGI pieces of the remote server (#396).

Copied from notes-graph ``src/apple_notes_mcp/remote/app.py`` (``_result_chars``,
``RemoteFastMCP``, ``_plain_error``, ``RegistrationGuard``) and ``tools.py``
(``threaded``) at ``vendor.UPSTREAM_COMMIT``. Unlike ``auth.py`` these are not
hash-pinned, because upstream ``app.py`` keeps changing for notes-only
reasons. Differences from upstream: the "too large" hint text, and
``RemoteFastMCP.call_tool`` returning a fixed error instead of the tool's
exception text (#396 review).
"""

import collections
import functools
import json
import time
from typing import Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from pydantic import ValidationError

from .request_log import RequestLog

# Claude rejects tool results over roughly 150k characters.
MAX_RESULT_CHARS = 150_000

REGISTER_MAX_BODY = 16 * 1024
REGISTER_RATE_LIMIT = 10  # registrations per window, server-wide (only Claude registers)
REGISTER_RATE_WINDOW = 60.0


def _result_chars(result: Any) -> int:
    if isinstance(result, tuple):
        content, structured = result
    elif isinstance(result, dict):
        content, structured = [], result
    else:
        content, structured = result, None
    size = sum(len(getattr(block, "text", "") or "") for block in content)
    if structured is not None:
        size = max(size, len(json.dumps(structured, default=str)))
    return size


class RemoteFastMCP(FastMCP):
    """FastMCP that logs every tool call (name + decision only) and caps result size."""

    def __init__(self, *args, request_log: RequestLog, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_log = request_log

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        token = get_access_token()
        who = {"tool": name[:100], "subject": token.subject if token else None}
        if self._tool_manager.get_tool(name) is None:
            self._request_log.event("tool", decision="deny", reason="not exposed remotely", **who)
            raise ToolError(f"Unknown tool: {name}")
        try:
            result = await super().call_tool(name, arguments)
        except Exception as e:
            # FastMCP wraps a tool's exception as ToolError("Error executing
            # tool X: <message>"), and that message goes to Claude - it can
            # carry local paths or config lines (#396 review). Send a fixed
            # message naming only the type; argument validation is the
            # caller's mistake, not a fault, so it is logged apart.
            cause = e.__cause__ if isinstance(e, ToolError) and e.__cause__ else e
            if isinstance(cause, ValidationError):
                self._request_log.event(
                    "tool", decision="allow", outcome="invalid_arguments", **who
                )
                fields = sorted({str(err["loc"][0]) for err in cause.errors() if err.get("loc")})
                raise ToolError(
                    f"Invalid arguments for {name}: {', '.join(fields) or 'see the tool schema'}"
                ) from None
            error_type = type(cause).__name__
            self._request_log.event("tool", decision="allow", outcome=f"error:{error_type}", **who)
            raise ToolError(
                f"{name} failed ({error_type}). "
                "Details are in the agent-hq server log on the owner's Mac."
            ) from None
        size = _result_chars(result)
        if size > MAX_RESULT_CHARS:
            self._request_log.event(
                "tool", decision="allow", outcome="too_large", chars=size, **who
            )
            raise ToolError(
                f"Result too large for Claude ({size} characters, limit {MAX_RESULT_CHARS}). "
                "Narrow the request: filter list_issues by repo, status or state, "
                "or list_agents by repo or state."
            )
        self._request_log.event("tool", decision="allow", outcome="ok", chars=size, **who)
        return result


async def _plain_error(send, status: int, message: str) -> None:
    body = json.dumps({"error": "invalid_request", "error_description": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RegistrationGuard:
    """Bounds open dynamic client registration: body size and a rate limit."""

    def __init__(self, app):
        self.app = app
        self._recent: collections.deque = collections.deque()

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("path") != "/register"
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        while self._recent and now - self._recent[0] > REGISTER_RATE_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= REGISTER_RATE_LIMIT:
            await _plain_error(send, 429, "too many client registrations; try again later")
            return
        self._recent.append(now)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > REGISTER_MAX_BODY:
            await _plain_error(send, 413, "registration request too large")
            return
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > REGISTER_MAX_BODY:
                await _plain_error(send, 413, "registration request too large")
                return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def threaded(tool: Tool) -> Tool:
    """A copy of a sync tool that runs in a worker thread.

    FastMCP calls plain ``def`` tools directly on the event loop, so one slow
    read (a GitHub refresh) would stall every other request - token refreshes
    and sign-in callbacks included.
    """
    if tool.is_async:
        return tool
    fn = tool.fn

    async def run_in_thread(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    return tool.model_copy(update={"fn": run_in_thread, "is_async": True})
