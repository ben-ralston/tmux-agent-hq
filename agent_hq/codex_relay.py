"""Transparent local TUI relay; observes explicit selections, never approvals."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


def _object(frame: str | bytes) -> dict:
    try:
        value = json.loads(frame)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


class ThreadSelection:
    """Correlate selection responses from this frontend connection only."""

    def __init__(self, selected: Callable[[dict | None], None]):
        self.selected = selected
        self.pending: dict[str | int, tuple[int, str, str | None]] = {}
        self.sequence = 0
        self.accepted = 0
        self.thread_id: str | None = None

    def client(self, frame: str | bytes) -> None:
        data = _object(frame)
        method, request_id = data.get("method"), data.get("id")
        if not isinstance(method, str) or method not in {
            "thread/start",
            "thread/resume",
            "thread/fork",
            "thread/unsubscribe",
        }:
            return
        if type(request_id) not in (str, int):
            return
        self.sequence += 1
        params = data.get("params")
        # The TUI also starts ephemeral system threads for title generation.
        # Those requests share this connection but never select the visible
        # conversation. Use native provenance, never request-ID conventions.
        if method == "thread/start" and isinstance(params, dict):
            if params.get("ephemeral") is True or params.get("threadSource") not in (None, "user"):
                return
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        self.pending[request_id] = (self.sequence, method, thread_id)

    def server(self, frame: str | bytes) -> None:
        data = _object(frame)
        request_id = data.get("id")
        # Server-initiated approval requests may reuse a client request ID.
        if "method" in data or type(request_id) not in (str, int):
            return
        pending = self.pending.pop(request_id, None)
        if pending is None or "error" in data:
            return
        sequence, method, thread_id = pending
        result = data.get("result")
        if sequence < self.accepted or not isinstance(result, dict):
            return
        if method == "thread/unsubscribe":
            if thread_id == self.thread_id and result.get("status") in (
                "unsubscribed",
                "notLoaded",
            ):
                self.accepted = sequence
                self.thread_id = None
                self._notify(None)
            return
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or not thread["id"]
        ):
            return
        self.accepted = sequence
        self.thread_id = thread["id"]
        self._notify(thread)

    def _notify(self, thread):
        try:
            self.selected(thread)
        except Exception:
            # Monitoring must not swallow a TUI response when its receipt
            # cannot be persisted. Invalidate identity and keep forwarding.
            self.thread_id = None
            try:
                self.selected(None)
            except Exception:
                pass


async def relay(frontend, socket_path: str, selected: Callable[[dict | None], None]) -> None:
    """Pass both directions byte-for-byte and close together on disconnect."""
    tracker = ThreadSelection(selected)
    try:
        async with unix_connect(
            socket_path,
            uri="ws://localhost/rpc",
            compression=None,
            max_size=None,
            open_timeout=2,
            close_timeout=0.1,
        ) as backend:

            async def forward(source, destination, observe):
                async for frame in source:
                    observe(frame)
                    await destination.send(frame)

            tasks = [
                asyncio.create_task(forward(frontend, backend, tracker.client)),
                asyncio.create_task(forward(backend, frontend, tracker.server)),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake):
        pass
    finally:
        tracker._notify(None)
        await frontend.close()
