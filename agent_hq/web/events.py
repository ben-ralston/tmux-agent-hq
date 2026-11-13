"""In-process SSE event broker with topic globbing, dedupe, and replay buffer.

Single-process by design — the broker state (subscribers, buffer, last-seen
fingerprints) lives in memory. ``agent-hq web`` runs with ``workers=1`` so
all subscribers share one broker instance.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class Event:
    topic: str
    data: dict
    event_id: int = 0  # assigned by broker on publish
    fingerprint: str | None = None  # for dedupe; None disables dedupe for this event


class Broker:
    """Publishes events to all matching subscribers. Keeps a small replay
    buffer so reconnecting clients can send Last-Event-ID and catch up
    without polling."""

    def __init__(self, buffer_size: int = 100):
        self._id_counter = itertools.count(start=1)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._buffer: deque[Event] = deque(maxlen=buffer_size)
        # Per-key fingerprint dedupe: prevents the hooks watcher and the
        # discovery tick from both firing for the same state change.
        self._last_fingerprint: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> bool:
        """Return True if published, False if deduped."""
        async with self._lock:
            if event.fingerprint is not None:
                # Prefer the renumber-stable ``key`` so dedupe tracks one
                # logical agent across a tmux pane renumber; fall back to
                # ``pane_id`` for events that don't carry a key (#132).
                key = str(event.data.get("key") or event.data.get("pane_id") or event.topic)
                if self._last_fingerprint.get(key) == event.fingerprint:
                    return False
                self._last_fingerprint[key] = event.fingerprint

            event.event_id = next(self._id_counter)
            self._buffer.append(event)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Slow subscriber loses this update; reconnect will
                    # pick it up from the replay buffer.
                    pass
            return True

    async def subscribe(self, topics: list[str], after_id: int = 0) -> AsyncIterator[Event]:
        """Yield buffered events matching topics (with id > after_id), then live events."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)
        async with self._lock:
            for e in self._buffer:
                if e.event_id > after_id and _match_any(e.topic, topics):
                    queue.put_nowait(e)
            self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                if _match_any(event.topic, topics):
                    yield event
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def replay_since(self, after_id: int) -> list[Event]:
        return [e for e in self._buffer if e.event_id > after_id]


def _match_any(topic: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(topic, p) for p in patterns)
