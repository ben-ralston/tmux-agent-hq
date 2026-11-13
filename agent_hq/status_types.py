"""Immutable runtime observations, independent of discovery and model imports."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Literal

StatusState = Literal["idle", "running", "waiting", "unknown"]
StatusSource = Literal["claude", "codex", "none"]


@dataclass(frozen=True)
class LiveStatus:
    """A single harness observation, never a terminal or transcript inference."""

    state: StatusState
    native_value: str
    label: str
    reason: str
    source: StatusSource
    session_id: str | None
    pid: int | None
    observed_at: float
    observed_ns: int
    # When a waiting session entered waiting (#415): WaitingClock's value once
    # published; straight out of a reader, the harness's own record, or None.
    waiting_since: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> LiveStatus:
        """Fail closed on corrupt/old snapshots instead of reviving live state."""
        invalid = "Invalid runtime status snapshot"
        if not isinstance(data, dict):
            return unknown_status(invalid)
        state, source = data.get("state"), data.get("source")
        if state not in ("idle", "running", "waiting", "unknown"):
            return unknown_status(invalid)
        if source not in ("claude", "codex", "none"):
            return unknown_status(invalid)
        if any(not isinstance(data.get(key), str) for key in ("native_value", "label", "reason")):
            return unknown_status(invalid)
        session_id, pid = data.get("session_id"), data.get("pid")
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            return unknown_status(invalid)
        if pid is not None and (type(pid) is not int or pid <= 0):
            return unknown_status(invalid)
        observed_at, observed_ns = data.get("observed_at"), data.get("observed_ns")
        try:
            valid_time = (type(observed_at) in (int, float)
                          and math.isfinite(observed_at) and observed_at >= 0)
        except OverflowError:
            valid_time = False
        if not valid_time or type(observed_ns) is not int or observed_ns < 0:
            return unknown_status(invalid)
        if state != "unknown" and (source == "none" or session_id is None or pid is None):
            return unknown_status(invalid)
        # Optional and additive: a bad value costs this field, not the observation.
        waiting_since = data.get("waiting_since")
        try:
            valid_since = (state == "waiting" and type(waiting_since) in (int, float)
                           and math.isfinite(waiting_since)
                           and 0 <= waiting_since <= observed_at)
        except OverflowError:
            valid_since = False
        return cls(state, data["native_value"], data["label"], data["reason"], source,
                   session_id, pid, observed_at, observed_ns,
                   waiting_since if valid_since else None)


def unknown_status(
    reason: str,
    *,
    source: StatusSource = "none",
    session_id: str | None = None,
    pid: int | None = None,
    native_value: str = "",
    observed_at: float | None = None,
    observed_ns: int | None = None,
) -> LiveStatus:
    return LiveStatus(
        "unknown", native_value, "Unknown", reason, source, session_id, pid,
        time.time() if observed_at is None else observed_at,
        time.monotonic_ns() if observed_ns is None else observed_ns,
    )
