"""Scheduled-automation health from launchd + plists + wrapper logs (#54).

Read-only: nothing here mutates launchd state or the automations themselves.
All filesystem/subprocess access stays behind small functions so tests mock
at one seam, and time is always passed in explicitly.
"""

from __future__ import annotations

import plistlib
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from xml.parsers.expat import ExpatError

from agent_hq.config import AutomationConfig

_LAST_EXIT_CODE_RE = re.compile(r"last exit code = (\d+)")
_STATE_RE = re.compile(r"^\s*state = (.+?)\s*$", re.MULTILINE)
_PID_RE = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)
_KEEPALIVE_RE = re.compile(r"^\s*properties = .*\bkeepalive\b", re.MULTILINE)
# launchd prints e.g. ``last terminating signal = Killed: 9`` (#427).
_SIGNAL_RE = re.compile(r"^\s*last terminating signal = .*?(\d+)\s*$", re.MULTILINE)
# How launchd stops a job to restart it, seen as either exit 15 or the signal.
_SIGTERM = 15
# launchctl print's exit status for a job it hasn't loaded ("Could not find service").
_LAUNCHCTL_NOT_FOUND = 113

#: How long after a scheduled fire time we wait before calling a run missed.
MISSED_GRACE = timedelta(minutes=15)

DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# v1 supports daily schedules only — see parse_calendar_intervals.
_SUPPORTED_INTERVAL_KEYS = {"Hour", "Minute"}


class AutomationStatus(Enum):
    OK = "ok"
    ERROR = "error"
    MISSED = "missed"
    UNKNOWN = "unknown"
    # Daemons only (#298): loaded+running but the log went quiet / not running.
    STALE = "stale"
    STOPPED = "stopped"


@dataclass(frozen=True)
class LaunchdJob:
    """What ``launchctl print`` says about a loaded job (#298)."""

    running: bool
    pid: int | None
    exit_code: int | None
    keepalive: bool = False
    # The signal that ended the last run (#427); such a run has no exit code.
    signal: int | None = None
    # False when launchctl says it has no such job (#426): positively not loaded,
    # unlike a None job, which means launchctl couldn't be read.
    loaded: bool = True


@dataclass(frozen=True)
class AutomationHealth:
    """Point-in-time health of one scheduled automation."""

    name: str
    label: str
    log_file: Path
    status: AutomationStatus
    schedule_desc: str | None = None
    last_run: datetime | None = None
    last_exit_code: int | None = None
    last_log_line: str | None = None
    next_run: datetime | None = None
    # Daemons only (#298): whether launchd reports the job running; None for
    # scheduled jobs, whose rows show the next fire time instead.
    running: bool | None = None
    # Scheduled jobs: the most recent fire time at or before the collection.
    last_fire: datetime | None = None
    # The signal that ended the last run, when it counts against the job (#427).
    last_signal: int | None = None

    @property
    def signal_name(self) -> str | None:
        """``last_signal`` as ``SIGKILL``, or ``signal 250`` for a number Python doesn't name."""
        if self.last_signal is None:
            return None
        try:
            return signal.Signals(self.last_signal).name
        except ValueError:
            return f"signal {self.last_signal}"


def parse_calendar_intervals(plist: dict) -> list[dict]:
    """Normalize ``StartCalendarInterval`` to a list of interval dicts.

    launchd accepts either a single dict or a list of dicts. Only intervals
    expressing a plain daily Hour/Minute schedule with in-range integer
    values survive. Everything else is dropped so health degrades to
    exit-code-only instead of misfiring or crashing the section route:
    an interval carrying any other launchd calendar key (Weekday/Day/Month)
    is weekly/monthly — treating it as daily would false-flag MISSED — and
    malformed types or ranges would raise inside ``datetime.replace``.
    """
    raw = plist.get("StartCalendarInterval")
    if isinstance(raw, dict):
        intervals = [raw]
    elif isinstance(raw, list):
        intervals = raw
    else:
        return []
    valid = []
    for interval in intervals:
        if not isinstance(interval, dict) or not set(interval) <= _SUPPORTED_INTERVAL_KEYS:
            continue
        hour = interval.get("Hour")
        minute = interval.get("Minute", 0)
        if not isinstance(hour, int) or not 0 <= hour <= 23:
            continue
        if not isinstance(minute, int) or not 0 <= minute <= 59:
            continue
        valid.append(interval)
    return valid


def _daily_candidate(interval: dict, now: datetime) -> datetime:
    return now.replace(
        hour=interval["Hour"],
        minute=interval.get("Minute", 0),
        second=0,
        microsecond=0,
    )


def next_fire_time(intervals: list[dict], now: datetime) -> datetime | None:
    """Earliest upcoming fire time strictly after ``now``."""
    candidates = []
    for interval in intervals:
        candidate = _daily_candidate(interval, now)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates, default=None)


def most_recent_fire_time(intervals: list[dict], now: datetime) -> datetime | None:
    """Latest fire time at or before ``now``."""
    candidates = []
    for interval in intervals:
        candidate = _daily_candidate(interval, now)
        if candidate > now:
            candidate -= timedelta(days=1)
        candidates.append(candidate)
    return max(candidates, default=None)


def read_launchd_job(label: str, uid: int) -> LaunchdJob | None:
    """Running state, pid, last exit code and KeepAlive of a loaded job, or None (#298).

    A job launchd hasn't loaded comes back with ``loaded=False`` (#426). None
    means launchctl couldn't be read (missing, timed out, any other failure),
    which says nothing about the job. ``exit_code`` is None
    when the job has never exited (launchd prints ``last exit code = (never
    exited)``) or a signal ended it (``last terminating signal = Killed: 9``,
    read into ``signal``). A KeepAlive job
    restarted after a crash keeps the previous run's ``last exit code`` while
    the new process runs — callers judging daemons must not treat it as live.
    """
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == _LAUNCHCTL_NOT_FOUND:
        return LaunchdJob(running=False, pid=None, exit_code=None, loaded=False)
    if result.returncode != 0:
        return None
    state = _STATE_RE.search(result.stdout)
    pid = _PID_RE.search(result.stdout)
    exit_code = _LAST_EXIT_CODE_RE.search(result.stdout)
    term_signal = _SIGNAL_RE.search(result.stdout)
    return LaunchdJob(
        running=bool(state and state.group(1) == "running"),
        pid=int(pid.group(1)) if pid else None,
        exit_code=int(exit_code.group(1)) if exit_code else None,
        keepalive=bool(_KEEPALIVE_RE.search(result.stdout)),
        signal=int(term_signal.group(1)) if term_signal else None,
    )


def read_log_status(path: Path) -> tuple[datetime | None, str | None]:
    """(mtime, last non-empty line) of a log file; (None, None) if missing."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None, None
    last_line = None
    for line in reversed(_read_tail_lines(path)):
        if line.strip():
            last_line = line
            break
    return mtime, last_line


def read_tail(path: Path) -> tuple[list[str], bool]:
    """Every whole line in the end of a log file, and whether the file holds more
    before them. Reads at most the last 64 KiB; a missing file has no lines."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_READ_BYTES))
            chunk = f.read()
    except OSError:
        return [], False
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    more_before = size > _TAIL_READ_BYTES
    # Drop a partial first line when the read started mid-file.
    return (lines[1:] if more_before else lines), more_before


def tail_log(path: Path, n: int = 50) -> str:
    """Last ``n`` lines of a log file; empty string if missing."""
    return "\n".join(_read_tail_lines(path)[-n:])


class AutomationsCache:
    """In-memory TTL cache for collected health.

    Collection shells out to ``launchctl`` once per automation, so the
    /agents page (htmx-refreshed every 30 s, possibly by several clients)
    goes through this instead of collecting per request. In-memory only —
    health is cheap to recompute and meaningless across restarts. Injected
    into ``create_app`` so route tests never touch launchctl or real paths.
    """

    def __init__(self, ttl: float = 30.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, list[AutomationHealth]] | None = None

    def get_cached(self) -> tuple[list[AutomationHealth], bool] | None:
        """Return ``(healths, fresh)`` ignoring the TTL; never collects.

        ``None`` only before the first fetch. Lets the section route serve
        stale health instantly and refresh in the background (#226).
        """
        with self._lock:
            if self._cached is None:
                return None
            timestamp, healths = self._cached
            return healths, time.time() - timestamp < self._ttl

    def fetched_at(self) -> float | None:
        """When the loader last ran (``time.time()``), or None before the first fetch."""
        with self._lock:
            return None if self._cached is None else self._cached[0]

    def fetch(self, loader, force: bool = False) -> list[AutomationHealth]:
        with self._lock:
            if not force and self._cached is not None:
                timestamp, healths = self._cached
                if time.time() - timestamp < self._ttl:
                    return healths
            healths = loader()
            self._cached = (time.time(), healths)
            return healths


# Generous upper bound for 50 log lines, and as far back as any tail reads.
_TAIL_READ_BYTES = 64 * 1024


def _read_plist(path: Path) -> dict:
    """A plist's top-level dict; empty when missing, unreadable or not a dict."""
    try:
        with open(path, "rb") as f:
            plist = plistlib.load(f)
    # plistlib raises ExpatError and plain ValueErrors for malformed XML/values.
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
        return {}
    return plist if isinstance(plist, dict) else {}


def _plist_keepalive(plist: dict, label: str, launch_agents_dir: Path) -> bool | None:
    """Whether the job's plist says ``KeepAlive: true`` (#426); None with no plist.

    The live plist decides when there is one. Without it, a ``.plist.disabled``
    does: ``agent-hq remote kill`` boots a job out and renames its plist, and a
    job killed on purpose is still stopped. A conditional KeepAlive (a dict)
    doesn't count, since launchd rightly leaves such a job down at times.
    """
    if not plist:
        plist = _read_plist(launch_agents_dir / f"{label}.plist.disabled")
    return plist.get("KeepAlive") is True if plist else None


def _has_schedule(plist: dict) -> bool:
    """Whether launchd starts the job on a schedule, daily or not."""
    return "StartCalendarInterval" in plist or "StartInterval" in plist


def _schedule_desc(intervals: list[dict]) -> str | None:
    if not intervals:
        return None
    times = sorted(f"{iv['Hour']:02d}:{iv.get('Minute', 0):02d}" for iv in intervals)
    return "daily " + ", ".join(times)


def collect_health(
    configs: list[AutomationConfig],
    now: datetime,
    uid: int,
    launch_agents_dir: Path = DEFAULT_LAUNCH_AGENTS_DIR,
) -> list[AutomationHealth]:
    """Health of each configured automation, in config order.

    Status rules (first match wins):

    * ``ERROR`` — launchd recorded a non-zero last exit code or a terminating
      signal (#427), OR the last log line matches ``failure_pattern`` /
      misses ``success_pattern`` (catches wrappers that exit 0 on logical
      failure, and launchd forgetting a non-zero exit after a reboot). On a
      KeepAlive job, exit 15 and SIGTERM are restarts and don't count.
    * ``STOPPED`` — a KeepAlive job with no schedule (no ``StartCalendarInterval``
      or ``StartInterval``) that launchd doesn't report running, or hasn't
      loaded (#426). The gap after a
      restart doesn't count: launchd brings the job back by itself.
    * ``MISSED`` — the most recent scheduled fire time (+ grace) has passed
      and the log was last written before it (or never)
    * ``UNKNOWN`` — nothing observable: no log file and no launchd record
    * ``OK`` — otherwise

    Entries with ``stale_after`` are KeepAlive daemons and use
    :func:`_daemon_health` instead (#298).
    """
    healths = []
    for config in configs:
        job = read_launchd_job(config.label, uid)
        if config.stale_after is not None:
            healths.append(_daemon_health(config, job, config.stale_after, now))
            continue
        exit_code = _meaningful_exit_code(job)
        last_signal = _meaningful_signal(job)
        last_run, last_log_line = read_log_status(config.log_file)
        plist = _read_plist(launch_agents_dir / f"{config.label}.plist")
        intervals = parse_calendar_intervals(plist)
        most_recent = most_recent_fire_time(intervals, now)

        if (
            exit_code not in (0, None)
            or last_signal is not None
            or _log_verdict_is_failure(config, last_log_line)
        ):
            status = AutomationStatus.ERROR
        elif not _has_schedule(plist) and _keepalive_stopped(
            job, plist, config.label, launch_agents_dir
        ):
            status = AutomationStatus.STOPPED
        elif (
            most_recent is not None
            and now > most_recent + MISSED_GRACE
            and (last_run is None or last_run < most_recent)
        ):
            status = AutomationStatus.MISSED
        elif last_run is None and exit_code is None:
            status = AutomationStatus.UNKNOWN
        else:
            status = AutomationStatus.OK

        healths.append(
            AutomationHealth(
                name=config.name,
                label=config.label,
                log_file=config.log_file,
                status=status,
                schedule_desc=_schedule_desc(intervals),
                last_run=last_run,
                last_exit_code=exit_code,
                last_log_line=last_log_line,
                next_run=next_fire_time(intervals, now),
                last_fire=most_recent,
                last_signal=last_signal,
            )
        )
    return healths


def _meaningful_exit_code(job: LaunchdJob | None) -> int | None:
    """A job's last exit code, unless it only records a restart.

    launchd stops a job with SIGTERM to restart it, as a deploy does, and keeps that
    code while a KeepAlive job runs again. Any other code stays: without a heartbeat
    to check, it may be the only sign of a crash loop.
    """
    if job is None or (job.keepalive and job.exit_code == _SIGTERM):
        return None
    return job.exit_code


def _meaningful_signal(job: LaunchdJob | None) -> int | None:
    """The signal that ended a job's last run, judged like its exit code (#427).

    SIGTERM on a KeepAlive job is a restart. Any other signal, an out-of-memory
    SIGKILL included, stays even once the job runs again: a job killed over and
    over is restarted between kills, so the signal may be all that shows it.
    """
    if job is None or (job.keepalive and job.signal == _SIGTERM):
        return None
    return job.signal


def _keepalive_stopped(
    job: LaunchdJob | None, plist: dict, label: str, launch_agents_dir: Path
) -> bool:
    """Whether a KeepAlive job isn't running when it should be (#426).

    KeepAlive comes from the plist, else from launchctl. A job launchd hasn't
    loaded counts. A loaded job that isn't running counts unless its last run
    ended in a restart, which launchd follows with a new run once its throttle
    interval passes. A launchctl that couldn't be read (None) never counts.
    """
    if job is None or job.running:
        return False
    keepalive = _plist_keepalive(plist, label, launch_agents_dir)
    if not (job.keepalive if keepalive is None else keepalive):
        return False
    return not job.loaded or _SIGTERM not in (job.exit_code, job.signal)


def _daemon_health(
    config: AutomationConfig, job: LaunchdJob | None, stale_after: timedelta, now: datetime
) -> AutomationHealth:
    """Health of a KeepAlive daemon (#298). Rules, first match wins:

    * ``STOPPED`` — launchd has no loaded job, or it isn't running
    * ``ERROR`` — the last log line matches ``failure_pattern`` / misses
      ``success_pattern``. launchd's ``last exit code`` is ignored while the
      job runs: KeepAlive keeps a previous crash's code across restarts.
    * ``STALE`` — running, but the log is older than ``stale_after`` (or
      absent): a hung loop stops writing its heartbeat
    * ``OK`` — otherwise
    """
    last_run, last_log_line = read_log_status(config.log_file)
    running = bool(job and job.running)
    if not running:
        status = AutomationStatus.STOPPED
    elif _log_verdict_is_failure(config, last_log_line):
        status = AutomationStatus.ERROR
    elif last_run is None or now - last_run > stale_after:
        status = AutomationStatus.STALE
    else:
        status = AutomationStatus.OK
    return AutomationHealth(
        name=config.name,
        label=config.label,
        log_file=config.log_file,
        status=status,
        last_run=last_run,
        last_exit_code=job.exit_code if job else None,
        last_log_line=last_log_line,
        running=running,
    )


def _log_verdict_is_failure(config: AutomationConfig, last_log_line: str | None) -> bool:
    """Judge the wrapper's final log line against the configured patterns.

    Only the LAST line is consulted — the wrappers append their verdict
    after any interleaved agent output, and earlier lines belong to prior
    runs. With no log line there is nothing to judge (the missed/unknown
    rules decide instead), so an unmatched ``success_pattern`` is only a
    failure when a line exists to miss it. Note ``success_pattern`` reads
    as ERROR while a run is mid-flight (the last line is interim output
    until the verdict lands) — ``failure_pattern`` doesn't.
    """
    if not last_log_line:
        return False
    if config.failure_pattern and re.search(config.failure_pattern, last_log_line):
        return True
    return bool(config.success_pattern and not re.search(config.success_pattern, last_log_line))


def _read_tail_lines(path: Path) -> list[str]:
    return read_tail(path)[0]
