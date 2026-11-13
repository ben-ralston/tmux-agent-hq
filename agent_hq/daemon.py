"""Background daemon for agent discovery caching."""

import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import eventlog
from .cache import CACHE_FILE, write_cache
from .config import Config, load_config
from .discovery import discover_agents, get_all_panes
from .enrichment import enrich_all_agents
from .hooks import prune_stale_hook_state_files
from .models import Agent, AgentType, PhaseSnapshot
from .overrides import prune_stale_overrides, read_overrides
from .pinned_store import PinnedSessionsStore, migrate_legacy_pins
from .resolver import resolve_phase_snapshot
from .runtime_dir import check_trusted, ensure_dir
from .snapshots import SnapshotStore
from .state import cleanup_inactive_entries, cleanup_stale_entries
from .triage_sync import TriageSyncer

log = logging.getLogger(__name__)

PID_FILE = Path("/tmp/agent-hq/daemon.pid")
DAEMON_LOCK_FILE = Path("/tmp/agent-hq/daemon.lock")
DEFAULT_INTERVAL = 2.0
# Prune stale access/inactive entries every N poll ticks (~1 min at
# DEFAULT_INTERVAL) — often enough to bound growth, rare enough that the
# extra state-file reads don't matter (#13).
STATE_CLEANUP_TICKS = 30
# Drain hook-queued logger events even when the daemon itself emits
# nothing (#231). Frequency, not latency, is the contract here.
EVENTLOG_FLUSH_INTERVAL_S = 300.0


def write_pid(pid: int) -> None:
    """Write PID to file via a temp file + rename, so a symlink planted at
    the PID path is replaced rather than followed (#263 F4)."""
    ensure_dir(PID_FILE.parent)
    fd, tmp = tempfile.mkstemp(dir=PID_FILE.parent, prefix=".daemon.pid.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{pid}\n")
        os.replace(tmp, PID_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_pid() -> int | None:
    """Read PID from file. Returns None if missing or corrupt."""
    try:
        check_trusted(PID_FILE)
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def remove_pid() -> None:
    """Remove PID file if it exists."""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _deregister_if_owner() -> None:
    """Remove the PID file + cache, but only if this process owns them.

    A culled orphan daemon exiting gracefully must not de-register the
    live singleton — deleting the owner's PID file would let the next
    ``ensure_running`` fork a duplicate cache writer. A missing PID file
    is NOT skipped: ``stop_daemon`` unlinks it before SIGTERM lands, and
    the dying owner must still clean up the cache.
    """
    owner = read_pid()
    if owner is not None and owner != os.getpid():
        return
    remove_pid()
    from .cache import CACHE_FILE

    try:
        CACHE_FILE.unlink()
    except FileNotFoundError:
        pass


def is_daemon_running() -> bool:
    """Check if daemon process is alive."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # Signal 0: check if process exists
        return True
    except (ProcessLookupError, PermissionError):
        return False


# Wait this long after SIGTERM before escalating to SIGKILL.
_ORPHAN_TERM_WAIT_SECONDS = 2.0
_ORPHAN_POLL_INTERVAL_SECONDS = 0.1

# When sampling cache.json for orphan writers, do this many reads spaced this far apart.
# 3 samples × 0.5s = 1.5s — long enough to catch most rotating writers without
# noticeably delaying daemon start.
_CACHE_WRITER_SAMPLES = 3
_CACHE_WRITER_SAMPLE_INTERVAL_SECONDS = 0.5


def _validate_daemon_cmdline(cmdline: str) -> bool:
    """Return True iff ``cmdline`` looks like an agent-hq daemon process.

    Stricter than a pgrep regex match — requires all three:

    1. First argv token names a Python interpreter (filters
       bash/sh/zsh self-matches whose argv contains the pgrep regex).
    2. ``agent-hq`` or ``agent_hq`` appears as a script reference, not
       just a positional arg of an unrelated program. Accepted shapes:
       a path containing ``/agent-hq`` or ``/agent_hq``, or
       ``python -m agent_hq[...]``. A bare word ``agent-hq`` in the
       middle of someone else's argv does NOT qualify.
    3. argv contains a ``daemon`` or ``--dashboard`` token (matched
       as a whole token, not a substring of another arg).
    """
    if not _is_agent_hq_cmdline(cmdline):
        return False
    return any(t in ("daemon", "--dashboard") for t in cmdline.split()[1:])


def _is_agent_hq_cmdline(cmdline: str) -> bool:
    """Criteria 1 and 2 of ``_validate_daemon_cmdline``: a Python process
    running agent-hq, whatever its subcommand. The daemon is forked from the
    process that started it, so its argv may read ``web serve`` too."""
    tokens = cmdline.split()
    if not tokens:
        return False
    if "python" not in tokens[0].lower():
        return False
    for i in range(1, len(tokens)):
        t = tokens[i]
        if "/agent-hq" in t or "/agent_hq" in t:
            return True
        if tokens[i - 1] == "-m" and (t == "agent_hq" or t.startswith("agent_hq.")):
            return True
    return False


def _ps_cmdline(pid: int) -> str | None:
    """Return the full cmdline for ``pid`` via ``ps``, or None on failure."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    out = result.stdout.strip()
    return out or None


def _pgrep_daemon_pids(excluded: set[int]) -> list[int]:
    """pgrep for daemon processes (catches ``daemon start`` orphans).

    pgrep also matches shells whose argv contains the regex string
    itself (``bash -c 'pgrep -af "...agent-hq...daemon"'`` matches its
    own pattern). Each match is validated by ``_validate_daemon_cmdline``
    so only real daemon processes survive — never an unrelated python
    invocation that happens to mention these tokens.
    """
    try:
        result = subprocess.run(
            # -fl (not -af): on BSD/macOS -a means "include ancestors" and
            # prints PID-only lines; -fl prints "PID full-cmdline" on both
            # BSD and procps.
            ["pgrep", "-fl", "agent-hq.*daemon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        pid_str, cmdline = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in excluded:
            continue
        if not _validate_daemon_cmdline(cmdline):
            continue
        pids.append(pid)
    return pids


def _read_cache_writer_pid() -> int | None:
    """Read the writer PID stamped into cache.json by the last write."""
    try:
        check_trusted(CACHE_FILE)
        data = json.loads(CACHE_FILE.read_text())
        pid = data.get("pid")
        return int(pid) if pid is not None else None
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return None


def _sample_cache_writer_pids(excluded: set[int]) -> list[int]:
    """Sample cache.json's writer PID a few times to catch rotating daemon-children.

    Daemons forked from ``run_dashboard`` inherit the dashboard's argv
    (e.g. ``agent-hq --dashboard``) so pgrep can't distinguish them
    from real dashboard processes. The cache file has each writer's
    PID embedded, so sampling it over a short window surfaces these
    orphans.

    Each sampled PID is validated against ``ps`` before being included,
    so a stale cache PID belonging to a recycled (unrelated) process
    never enters the kill list.

    Fast-path: when neither the daemon PID file nor the cache file
    exists (clean-state launch), there are no orphans to sample, so
    skip the 3 × 0.5s polling loop entirely. This shaves 1.5s off
    every dashboard start in the common case.
    """
    if not PID_FILE.exists() and not CACHE_FILE.exists():
        return []
    seen: set[int] = set()
    for i in range(_CACHE_WRITER_SAMPLES):
        pid = _read_cache_writer_pid()
        if pid is not None and pid not in excluded:
            cmdline = _ps_cmdline(pid)
            if cmdline is not None and _validate_daemon_cmdline(cmdline):
                seen.add(pid)
        if i < _CACHE_WRITER_SAMPLES - 1:
            time.sleep(_CACHE_WRITER_SAMPLE_INTERVAL_SECONDS)
    return sorted(seen)


def find_orphan_daemon_pids(*, exclude_pid: int | None = None) -> list[int]:
    """Return PIDs of agent-hq daemon processes other than ourselves.

    Two signal sources:

    1. ``pgrep -af "agent-hq.*daemon"`` catches processes started via
       ``agent-hq daemon start`` from any worktree.
    2. The ``pid`` field in ``/tmp/agent-hq/cache.json`` (sampled a
       few times) catches daemon children forked from
       ``run_dashboard`` — their command line inherits ``--dashboard``
       so pgrep alone misses them.

    Excludes the current process and any pid passed in ``exclude_pid``.
    Returns ``[]`` on any error — this helper is called from a startup
    hook and must never raise.
    """
    self_pid = os.getpid()
    excluded = {self_pid}
    if exclude_pid is not None:
        excluded.add(exclude_pid)

    pids: set[int] = set(_pgrep_daemon_pids(excluded))
    pids.update(_sample_cache_writer_pids(excluded))
    return sorted(pids)


def kill_orphan_daemons(*, silent: bool = False, exclude_pid: int | None = None) -> list[int]:
    """SIGTERM (then SIGKILL stragglers) any orphan daemons. Returns killed PIDs.

    The agent-hq daemon is a singleton — there should only ever be one
    process polling tmux and writing the cache. Multiple writers race
    on ``/tmp/agent-hq/cache.json`` and produce visible flicker in the
    dashboard when their serialized ``Agent`` shapes drift. This helper
    is called from ``start_daemon`` to cull any siblings.

    ``exclude_pid`` names the legitimate daemon (e.g. the PID-file
    owner) so we don't accidentally kill it on a no-op start.
    """
    pids = find_orphan_daemon_pids(exclude_pid=exclude_pid)
    if not pids:
        return []

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            log.warning("cannot SIGTERM orphan daemon PID %s (permission denied)", pid)

    deadline = time.monotonic() + _ORPHAN_TERM_WAIT_SECONDS
    survivors = list(pids)
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _process_alive(pid)]
        if not survivors:
            break
        time.sleep(_ORPHAN_POLL_INTERVAL_SECONDS)

    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            log.warning("cannot SIGKILL orphan daemon PID %s (permission denied)", pid)

    msg = f"Killed {len(pids)} orphan daemon(s): {pids}"
    log.info(msg)
    eventlog.emit("info", "orphan daemons killed", {"count": len(pids), "pids": list(pids)})
    if not silent:
        print(msg, file=sys.stderr)
    return pids


def _process_alive(pid: int) -> bool:
    """True if ``pid`` is still a live process we can signal."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _build_triage_syncer(config: "Config | None") -> TriageSyncer | None:
    """TriageSyncer when a triage: section is configured, else None (#246)."""
    if config is None or config.triage is None:
        return None
    return TriageSyncer(config.triage)


def run_poll_loop(interval: float = DEFAULT_INTERVAL) -> None:
    """Run the discovery polling loop. Exits on KeyboardInterrupt or SIGTERM.

    Per tick:

    1. Enumerate all tmux panes (not just discovered agents — discovery
       can transiently miss-classify a pane; using all panes for the
       orphan set prevents unlinking valid hook state files).
    2. Discover + enrich agents (populates both Claude session ids).
    3. Prune stale overrides (session-mismatch + legacy entries).
    4. Prune stale hook state files (orphan unlink + mismatch tombstone).
    5. Read overrides ONCE — all panes' snapshots in this tick share
       the same ``overrides_revision``.
    6. Resolve a ``PhaseSnapshot`` for each agent and write the cache
       with this tick's ``producer_epoch`` / ``sequence`` / ``monotonic_ns``.
    """
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    eventlog.set_process_role("daemon")
    eventlog.emit("info", "daemon started", {"interval": interval})
    last_eventlog_flush = time.monotonic()

    epoch = time.time()
    sequence = 0
    consecutive_errors = 0
    # Last tick's accepted snapshots, keyed by pane_id. Lets the resolver
    # skip the transcript scan when a pane's JSONL is byte-unchanged.
    prev_snapshots: dict[str, PhaseSnapshot] = {}
    # Last tick's agents by pane: an agent whose enrichment fails this tick
    # keeps its previous record instead of aborting the tick (#358).
    prev_agents: dict[str, Agent] = {}
    # Durable rolling record of the live-agent set for crash recovery (#125).
    # Appends only on change and swallows its own I/O errors, so it never
    # disturbs this loop.
    snapshot_store = SnapshotStore()
    # Pin migration (#205 Codex review): re-key legacy-form pins on the
    # captured stable id each tick. Store methods swallow their own I/O
    # errors, so this can't wedge the loop either.
    pinned_store = PinnedSessionsStore()
    # Triage data-repo sync (#246): tick() self-throttles (one git status
    # per 15 s) and never raises, so it is safe on the 2 s loop.
    try:
        triage_syncer = _build_triage_syncer(load_config())
    except Exception as exc:
        # Remote signal too: a malformed projects.yaml (any section) silently
        # disabling sync for the daemon's lifetime deserves more than a
        # scrolled-away local warning.
        log.warning("triage sync disabled (config error): %s", exc)
        eventlog.emit(
            "warn", "triage sync disabled (config error)", {"error": repr(exc)}, min_interval_s=900
        )
        triage_syncer = None

    from .status_collector import RuntimeStatusCollector

    collector = RuntimeStatusCollector()
    collector.start()
    try:
        while running:
            try:
                panes = get_all_panes()
                raw = discover_agents()
                collector.set_claude_present(any(
                    AgentType.from_command(item["command"]) is AgentType.CLAUDE
                    for item in raw
                ))
                agents = enrich_all_agents(raw, previous=prev_agents)

                active_pane_ids = {p["pane_id"] for p in panes}
                agents_by_pane = {a.pane_id: a for a in agents}
                prev_agents = agents_by_pane

                try:
                    prune_stale_overrides(agents)
                except OSError as exc:
                    # A read failure now raises (AHQ-014) instead of silently
                    # clobbering overrides.json — but that must cost only the
                    # prune, not the rest of the tick. A persistently unreadable
                    # overrides.json would otherwise abort write_cache/
                    # snapshot_store every tick, degrading the whole dashboard
                    # to live-discovery fallback — a bigger regression than the
                    # original clobber bug.
                    log.warning("skipping overrides prune: %s", exc)
                migrate_legacy_pins(pinned_store, agents)
                prune_stale_hook_state_files(active_pane_ids, agents_by_pane)

                # Periodic hygiene for the access/inactive state files (#13):
                # only fzf mode pruned them, so the long-running modes let
                # entries for dead panes pile up. Gated to every
                # STATE_CLEANUP_TICKS ticks; a transiently empty pane set is
                # treated as suspect (never wipe everything on a tmux hiccup).
                if sequence % STATE_CLEANUP_TICKS == 0 and active_pane_ids:
                    cleanup_stale_entries(active_pane_ids)
                    cleanup_inactive_entries(active_pane_ids)

                overrides_table, overrides_rev = read_overrides()
                sequence += 1
                for agent in agents:
                    agent.phase_snapshot = resolve_phase_snapshot(
                        agent=agent,
                        overrides_table=overrides_table,
                        overrides_revision=overrides_rev,
                        producer="daemon",
                        producer_epoch=epoch,
                        sequence=sequence,
                        previous_snapshot=prev_snapshots.get(agent.pane_id),
                    )
                prev_snapshots = {
                    a.pane_id: a.phase_snapshot for a in agents if a.phase_snapshot is not None
                }
                write_cache(
                    agents,
                    producer="daemon",
                    producer_epoch=epoch,
                    sequence=sequence,
                    timestamp=time.time(),
                    monotonic_ns=time.monotonic_ns(),
                )
                snapshot_store.append_if_changed(agents, timestamp=time.time())
                if triage_syncer is not None:
                    triage_syncer.tick()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                # The loop must not die — but a tick that throws every time
                # leaves the cache stale and every dashboard request silently
                # falling back to live discovery, with zero signal at the
                # default log level. Surface it at WARNING; full traceback for
                # the first few, then just the running count so a wedged
                # daemon doesn't spam the log on every tick.
                log.warning(
                    "poll loop tick failed (consecutive=%d)",
                    consecutive_errors,
                    exc_info=consecutive_errors <= 3,
                )
                if consecutive_errors >= 3:
                    # error level: a repeatedly-failing tick means every
                    # dashboard silently falls back to live discovery.
                    # eventlog dedupes; 15 min keeps a wedged daemon to
                    # ~4 rows/hour in D1.
                    eventlog.emit(
                        "error",
                        "daemon poll loop failing",
                        {"consecutive_errors": consecutive_errors, "error": repr(exc)},
                        min_interval_s=900,
                    )
            if time.monotonic() - last_eventlog_flush >= EVENTLOG_FLUSH_INTERVAL_S:
                last_eventlog_flush = time.monotonic()
                eventlog.flush()
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                break
    finally:
        collector.stop()


def start_daemon(interval: float = DEFAULT_INTERVAL, silent: bool = False) -> int:
    """Start the daemon in the background. Returns exit code."""
    # Identify the legitimate daemon (PID-file owner, if alive) so we
    # don't kill it during orphan cleanup. Everything else is fair game:
    # other worktrees' daemons, dashboard-forked siblings, etc.
    legit_pid = read_pid() if is_daemon_running() else None
    kill_orphan_daemons(silent=silent, exclude_pid=legit_pid)

    if legit_pid is not None:
        if not silent:
            print(f"Daemon already running (PID {legit_pid})", file=sys.stderr)
        return 1

    # Clean up stale PID file if present
    remove_pid()

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent: report success and exit
        if not silent:
            print(f"Daemon started (PID {pid})")
        return 0

    # Child: detach from terminal
    os.setsid()

    # Write PID file
    write_pid(os.getpid())

    # Close stdio to fully detach
    sys.stdin.close()
    devnull = open(os.devnull, "w")
    sys.stdout = devnull
    sys.stderr = devnull

    try:
        run_poll_loop(interval)
    finally:
        _deregister_if_owner()

    os._exit(0)


def stop_daemon() -> int:
    """Stop the running daemon. Returns exit code."""
    if not is_daemon_running():
        print("Daemon is not running", file=sys.stderr)
        return 1

    pid = read_pid()
    if pid is None:
        # Daemon exited between check and read
        print("Daemon is not running", file=sys.stderr)
        return 1
    # Never signal a PID the file merely names: a reused PID (or one planted
    # in a shared /tmp) could be the tmux server (#263 F4).
    cmdline = _ps_cmdline(pid)
    if cmdline is None:
        # ps timed out or failed: that says nothing about the PID, so keep
        # the file and signal nothing rather than orphan a live daemon.
        print(f"Could not verify PID {pid} (ps failed); not stopping", file=sys.stderr)
        return 1
    if not _is_agent_hq_cmdline(cmdline):
        remove_pid()
        print(f"PID {pid} is not an agent-hq process; removed stale PID file", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    remove_pid()
    print(f"Daemon stopped (PID {pid})")
    return 0


def daemon_status() -> int:
    """Print daemon status. Returns 0 if running, 1 if stopped."""
    if is_daemon_running():
        pid = read_pid()
        print(f"Daemon is running (PID {pid})")
        return 0
    else:
        print("Daemon is not running")
        return 1


@contextlib.contextmanager
def _file_lock(path: Path):
    """Hold an advisory exclusive ``flock`` on ``path`` for the block."""
    ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def ensure_running() -> bool:
    """Idempotent daemon launch — return True iff a daemon is up afterwards.

    Used by ``agent-hq web`` to guarantee a daemon is feeding cache.json
    before the FastAPI app starts. Multiple simultaneous web launches are
    serialized through the advisory lock at ``DAEMON_LOCK_FILE`` so we
    don't race ``start_daemon`` and end up with two writers on cache.json.
    """
    if is_daemon_running():
        return True

    with _file_lock(DAEMON_LOCK_FILE):
        # A competing caller may have started the daemon while we
        # waited for the lock — recheck to avoid duplicate spawn.
        if is_daemon_running():
            return True
        return start_daemon(silent=True) == 0
