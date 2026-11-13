"""Singleflight background refresh for stale-while-revalidate routes (#226).

One ``BackgroundRefresher`` per data source lives on ``app.state``
(``issues_refresher``, ``automations_refresher``, ...).
Routes that just served stale cache data call :meth:`kick` to ensure exactly
one refresh coroutine is running; concurrent stale serves (multiple tabs,
htmx re-renders) coalesce onto the in-flight task instead of stacking
fetches. A failed refresh trips a cooldown so a persistently failing backend
can't loop stale-serve -> refresh -> SSE -> refetch -> refresh forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from agent_hq import eventlog

log = logging.getLogger(__name__)


class BackgroundRefresher:
    """Singleflight background refresh with a failure cooldown.

    ``kick()`` must be called from the event loop (route handlers) — the
    task runs on the app's running loop, NOT via ``run_in_threadpool``
    (whose cancellation shield would detach it from the loop lifecycle).
    """

    def __init__(self, name: str, cooldown: float = 30.0) -> None:
        self._name = name
        self._cooldown = cooldown
        self._task: asyncio.Task | None = None
        self._blocked_until = 0.0  # time.monotonic() deadline after a failure

    @property
    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()

    def kick(
        self, factory: Callable[[], Coroutine[Any, Any, None]], *, force: bool = False
    ) -> bool:
        """Ensure a refresh is running; return whether one is now in flight.

        True means "started here or already running" — the caller uses it as
        the UI's "updating…" flag. False means we are inside the post-failure
        cooldown: serve stale without the indicator and retry later.

        ``force`` clears the cooldown for a deliberate human retry (the
        Triage tab's "Run now", #248) so the button is never a silent
        no-op. Singleflight still applies — a forced kick coalesces onto an
        in-flight run rather than starting a second one.
        """
        if self.in_flight:
            return True
        if force:
            self._blocked_until = 0.0
        if time.monotonic() < self._blocked_until:
            return False
        self._task = asyncio.create_task(self._run(factory()))
        return True

    def cancel(self) -> None:
        """Cancel an in-flight refresh (app shutdown). CancelledError is not
        an Exception, so ``_run`` neither swallows it nor trips the cooldown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _run(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except Exception as exc:
            log.exception("background refresh failed: %s", self._name)
            eventlog.emit(
                "warn",
                "background refresh failed",
                {"name": self._name, "error": repr(exc)},
                min_interval_s=600,
            )
            self._blocked_until = time.monotonic() + self._cooldown
