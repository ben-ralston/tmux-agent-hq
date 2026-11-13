"""Metered ``gh`` invocation — the single chokepoint for GitHub CLI calls (#255).

Every ``gh`` call agent-hq makes goes through :func:`run_gh`, which owns the
timeout, classifies the call against the REST or GraphQL bucket, and appends a
row to a rolling counter file. That counter is what makes "calls per hour"
measurable instead of estimated — #255's acceptance criterion is a real
before/after number, and there was previously no way to produce one.

Counting *calls* rather than GraphQL *points* is a deliberate limit. Points are
what GitHub actually bills, and they vary wildly by call: a
``gh project item-list --limit 200`` costs ~101 points where a 500-row
``gh issue list`` costs 2 (measured 2026-08-26). But points
are not reported per-request anywhere — the only source is a
``rate_limit`` delta, which is both eventually consistent and polluted by every
other process on the machine. Recording calls *by kind* keeps the one thing we
can observe honestly, and the per-kind point costs are applied afterwards from
the measurement table.

The counter is deliberately dumb: append-only JSONL, one short line per call,
trimmed to a cap. Concurrent agent-hq processes all append to the same file;
``O_APPEND`` writes well under ``PIPE_BUF`` are atomic on POSIX, so the common
path needs no lock. Only the periodic trim takes one.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._locking import file_lock

# Hard cap on every gh invocation, read-only and mutating alike. A hung gh
# would otherwise stall a ``run_in_threadpool`` worker forever — blocking the
# dispatch tab for reads (#90), or leaking a wedged thread per hung mutation
# until the whole threadpool starves (#182).
GH_TIMEOUT = 10

_DEFAULT_COUNTER_FILE = Path.home() / ".cache" / "agent-hq" / "gh-calls.jsonl"

# Rolling cap, mirroring logclient's queue cap: enough history for an
# hours-long before/after comparison, bounded so an unattended service can't
# grow the file without limit.
_MAX_RECORDS = 5000
# Trimming rewrites the whole file, so amortize it rather than doing it per call.
_TRIM_EVERY = 500

_appends_since_trim = 0
_trim_lock = threading.Lock()


def classify_api(argv: list[str]) -> str:
    """Which GitHub rate-limit bucket ``argv`` spends.

    The buckets are separate 5,000/hr budgets, and the whole point of #255 is
    moving work off the exhausted GraphQL one — so miscounting here would hide
    the very thing we are measuring. ``gh project`` and ``gh issue`` are
    GraphQL-only. ``gh api`` is REST *unless* its endpoint is literally
    ``graphql``.
    """
    if len(argv) >= 2 and argv[1] == "api":
        return "graphql" if len(argv) >= 3 and argv[2] == "graphql" else "rest"
    return "graphql"


def record(kind: str, api: str) -> None:
    """Append one call row. Best-effort — metering never breaks a fetch."""
    global _appends_since_trim
    line = json.dumps({"t": time.time(), "kind": kind, "api": api}) + "\n"
    path = _DEFAULT_COUNTER_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND: concurrent agent-hq processes interleave whole lines
        # rather than clobbering each other, with no lock on the hot path.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        return
    with _trim_lock:
        _appends_since_trim += 1
        due = _appends_since_trim >= _TRIM_EVERY
        if due:
            _appends_since_trim = 0
    if due:
        _trim(path)


def _trim(path: Path) -> None:
    """Drop the oldest rows beyond ``_MAX_RECORDS``. Best-effort.

    Known, accepted loss: a peer's append landing between the read and the
    ``os.replace`` here goes with the old inode. Appends are lockless by
    design, the counter is diagnostics, and the window is a few rows per
    ``_TRIM_EVERY`` — not worth a lock on every gh call.
    """
    try:
        with file_lock(path.with_suffix(".jsonl.lock")):
            lines = [ln for ln in path.read_text().splitlines() if ln]
            if len(lines) <= _MAX_RECORDS:
                return
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines[-_MAX_RECORDS:]) + "\n")
            os.replace(tmp, path)
    except OSError:
        return


def clear_counter() -> None:
    """Drop all recorded calls (tests, and before a measurement run)."""
    global _appends_since_trim
    with _trim_lock:
        _appends_since_trim = 0
    try:
        _DEFAULT_COUNTER_FILE.unlink()
    except OSError:
        pass


def run_gh(
    argv: list[str],
    *,
    kind: str,
    timeout: int = GH_TIMEOUT,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` (a ``gh …`` command line), counting the call.

    Raises exactly what ``subprocess.run(check=True)`` raises — callers keep
    translating those into ``GitHubFetchError`` via ``_fetch_failure``.

    A ``FileNotFoundError`` is NOT counted: gh never launched, so no API budget
    was spent. Every other failure IS counted — a rate-limit rejection or a
    timeout still put the request on the wire, and a meter that only counted
    successes would under-report exactly the storms worth seeing.
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=True, timeout=timeout, cwd=cwd
        )
    except FileNotFoundError:
        raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        record(kind, classify_api(argv))
        raise
    record(kind, classify_api(argv))
    return result


@dataclass(frozen=True)
class MeterSummary:
    """Call counts over a trailing window."""

    since: float
    hours: float
    total: int
    by_api: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    # Kind counts among GraphQL rows only. The bucket is on every row, so
    # the point estimate is a direct sum — no reconstructing the pairing
    # from the two histograms above (#255 review).
    graphql_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def per_hour(self) -> float:
        return self.total / self.hours if self.hours else 0.0


def summarize(hours: float = 1.0) -> MeterSummary:
    """Summarize recorded calls over the trailing ``hours``.

    Malformed lines are skipped rather than fatal — the counter is
    diagnostics, and a torn line from a killed process must not break the
    report it exists to produce.
    """
    since = time.time() - hours * 3600
    by_api: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    graphql_by_kind: dict[str, int] = {}
    total = 0
    try:
        raw = _DEFAULT_COUNTER_FILE.read_text()
    except OSError:
        raw = ""
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            stamp = float(row["t"])
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            continue
        if stamp < since:
            continue
        total += 1
        api = str(row.get("api", "unknown"))
        kind = str(row.get("kind", "unknown"))
        by_api[api] = by_api.get(api, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if api == "graphql":
            graphql_by_kind[kind] = graphql_by_kind.get(kind, 0) + 1
    return MeterSummary(
        since=since,
        hours=hours,
        total=total,
        by_api=by_api,
        by_kind=by_kind,
        graphql_by_kind=graphql_by_kind,
    )
