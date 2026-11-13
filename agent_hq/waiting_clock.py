"""When each session entered native ``waiting``, across daemon restarts (#415).

Lives beside the runtime status snapshot in ``/tmp/agent-hq``: it outlives a
daemon restart, and a reboot loses nothing because it kills the processes the
entries belong to. One entry per process, ``{pid: {session_id, since}}``, so a
new process or a new session in the same process (``/clear``, ``/resume``) is
always a new wait.

A session has stopped waiting when it is seen idle or running. Unknown (a source
error, an ambiguous identity) says nothing about the wait, so a blip can't reset
an overnight one.

Claude's registry records when the status last changed (``statusUpdatedAt``,
undocumented; other writes to the file leave it alone), so when a reader supplies
it, it is the start: it also catches a wait that ended between polls or while the
daemon was down. Codex has no such record: there, a wait that ends and restarts
unseen looks continuous.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import eventlog
from .runtime_dir import check_trusted, ensure_dir
from .runtime_status import _pid_alive
from .status_types import LiveStatus


def _load(path: Path) -> dict[int, dict]:
    """Valid entries from ``path``; anything else just restarts that wait."""
    try:
        check_trusted(path)
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = {}
    now = time.time()
    for key, entry in raw.items():
        try:
            pid = int(key)
            session_id, since = entry["session_id"], entry["since"]
            valid = (
                pid > 0
                and isinstance(session_id, str)
                and session_id
                and type(since) in (int, float)
                and math.isfinite(since)
                and 0 <= since <= now
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if valid:
            entries[pid] = {"session_id": session_id, "since": since}
    return entries


class WaitingClock:
    """Stamps ``LiveStatus.waiting_since``. Single-threaded: the collector's loop."""

    def __init__(self, path: Path, *, alive: Callable[[int], bool] | None = None) -> None:
        self._path = path
        self._alive = alive or _pid_alive
        self._entries = _load(path)
        self._dirty = False

    def stamp(self, status: LiveStatus) -> LiveStatus:
        """``status`` with its wait's start: the harness's own when it gives one,
        else the tracked one, else this observation."""
        if status.pid is None or status.session_id is None:
            return status
        if status.state in ("idle", "running"):
            if self._entries.pop(status.pid, None) is not None:
                self._dirty = True
            return status
        if status.state != "waiting":
            return status
        tracked = self._entries.get(status.pid)
        since = status.waiting_since
        if since is None:
            same_session = tracked is not None and tracked["session_id"] == status.session_id
            since = tracked["since"] if same_session else status.observed_at
        entry = {"session_id": status.session_id, "since": since}
        if entry != tracked:
            self._entries[status.pid] = entry
            self._dirty = True
        return replace(status, waiting_since=since)

    def save(self) -> None:
        """Drop dead processes and write any change. Never raises: a failure here
        must not cost the status snapshot; a change stays pending for the next call."""
        for pid in [pid for pid in self._entries if not self._alive(pid)]:
            del self._entries[pid]
            self._dirty = True
        if not self._dirty:
            return
        temporary = None
        try:
            ensure_dir(self._path.parent)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._path.parent, prefix=".waiting-since-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self._entries, stream, allow_nan=False)
            os.replace(temporary, self._path)
            temporary = None
            self._dirty = False
        except (OSError, ValueError) as exc:
            eventlog.emit(
                "warn", "waiting clock save failed", {"error": repr(exc)}, min_interval_s=900
            )
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
