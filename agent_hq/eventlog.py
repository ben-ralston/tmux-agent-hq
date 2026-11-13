"""agent-hq-facing emit API over the vendored logger transport (#231).

``emit()`` pins ``app="agent-hq"``, stamps the process role into
``json_extra["process"]``, dedupes identical (level, message) pairs
inside ``min_interval_s``, and picks a delivery mode per role so no hot
path ever blocks:

    thread      daemon / web / tui / mcp — fire-and-forget daemon thread
                (the transport can hold its flock ~15 s worst case)
    queue_only  hook — local append, no network; the daemon's periodic
                ``flush()`` drains it
    inline      cli — short-lived processes where a daemon thread would
                die before delivering (3 s network timeout is acceptable)

Message discipline (sentinel contract): messages are STABLE literals —
the sentinel fingerprints on the first 48 normalized chars — so all
variable detail goes in ``json_extra``. Levels: error = broke (auto-files
an issue), warn = degraded, info = lifecycle.
"""

from __future__ import annotations

import threading
import time

APP = "agent-hq"
_QUEUE_ONLY_ROLES = frozenset({"hook"})
_INLINE_ROLES = frozenset({"cli"})

_role = "cli"
_dedupe_lock = threading.Lock()
_last_emit: dict[tuple[str, str], float] = {}


_REPORTED_ATTR = "_agent_hq_reported"


def _transport():
    # Imported on first send, not at import time (#237): hooks.py imports
    # this module on every harness hook, and the transport pulls in
    # urllib.request + ssl (~12 ms cold) that only a failure branch needs.
    from agent_hq import logclient

    return logclient


def mark_reported(exc: BaseException) -> None:
    """Tag an exception whose failure was already emitted, so an outer error
    boundary re-catching it does not file a second event (#237)."""
    try:
        setattr(exc, _REPORTED_ATTR, True)
    except Exception:
        pass


def was_reported(exc: BaseException) -> bool:
    return getattr(exc, _REPORTED_ATTR, False) is True


def set_process_role(role: str) -> None:
    """Record which process is emitting (daemon/web/tui/hook/mcp/cli).

    Called once at each entry point; picks the delivery mode and lands in
    every event's ``json_extra["process"]``.
    """
    global _role
    _role = role


def emit(
    level: str,
    message: str,
    json_extra: dict | None = None,
    *,
    min_interval_s: float = 60.0,
    sync: bool = False,
) -> bool:
    """Best-effort remote event. Never raises, never blocks a hot path.

    Returns False when the dedupe window swallowed the event (or handing it
    off failed), so a caller can tell whether this failure was reported.
    ``sync`` delivers inline whatever the role: for a long-running process
    about to exit, whose daemon thread would die before delivering.
    """
    try:
        now = time.monotonic()
        key = (level, message)
        with _dedupe_lock:
            last = _last_emit.get(key)
            if last is not None and now - last < min_interval_s:
                return False
            _last_emit[key] = now
        extra = dict(json_extra) if json_extra else {}
        extra["process"] = _role
        if _role in _QUEUE_ONLY_ROLES:
            _transport().enqueue(APP, level, message, extra)
        elif sync or _role in _INLINE_ROLES:
            _transport().log(APP, level, message, extra)
        else:
            # The import happens in the thread too: the first event in a web
            # or daemon process must not stall the event loop or poll tick.
            threading.Thread(
                target=lambda: _transport().log(APP, level, message, extra),
                daemon=True,
            ).start()
        return True
    except Exception:
        return False


def flush() -> None:
    """Drain the local queue in the background. Never raises."""
    try:
        threading.Thread(target=lambda: _transport().flush(), daemon=True).start()
    except Exception:
        pass
