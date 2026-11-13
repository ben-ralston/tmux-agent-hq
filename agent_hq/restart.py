"""Runtime restart of Agent HQ processes (issue #215).

Backs the web ⚙ settings menu's Restart action. Replicates the
runtime-meaningful subset of ``./run install-global`` (``_do_install_global``
in ./run) — the mobile-recovery contract:

    _do_install_global step              at restart-time
    -----------------------------------  ------------------------------------
    agent-hq daemon stop                 replicated
    rm /tmp/agent-hq/cache.json          replicated (5s-TTL discovery
                                         snapshot; states/, overrides.json
                                         and ~/.config are preserved, same
                                         as the script)
    uv tool install -e . --reinstall     SKIPPED — install-only. The global
                                         install is editable, so restarting
                                         the processes already picks up the
                                         main worktree's current code; the
                                         reinstall only matters when deps or
                                         entry points changed.
    agent-hq daemon start                replicated
    agent-hq web service restart         replicated via ``launchctl
                                         kickstart -k``, and only when THIS
                                         process is the launchd-managed
                                         service — a dev server must not
                                         bounce the live one.
    main-worktree guard                  install-only, N/A

The kick is the caller's problem to sequence: ``kick_web_service()`` kills
the calling process, so the web route runs it in a background task after
the response has been flushed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from agent_hq import daemon, eventlog, service

_STEP_TIMEOUT = 15


@dataclass
class RestartStep:
    """Outcome of one restart step, rendered on the restarting page."""

    name: str
    ok: bool
    detail: str = ""


def _agent_hq_executable() -> str | None:
    """The agent-hq console script to restart the daemon with.

    Prefer the script serving this very process (``sys.argv[0]``): under
    launchd that is the absolute path baked into the plist, under
    ``uv run agent-hq web`` the worktree venv's script — either way the
    daemon restarts on the same code the server is running.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0) == "agent-hq":
        return argv0
    return shutil.which("agent-hq")


def _run_daemon_command(executable: str, action: str) -> RestartStep:
    name = f"daemon {action}"
    try:
        # Capture via a real file, never PIPE: `daemon start` forks a
        # long-lived child that inherits the CLI's stdout/stderr, and pipe
        # capture makes subprocess.run wait for a pipe EOF that only comes
        # when the daemon exits — the step would nondeterministically hang
        # to timeout even though the daemon started fine (same class as the
        # install-global `| tail` hang). With a file, run() waits only for
        # the CLI process itself.
        with tempfile.TemporaryFile(mode="w+") as buf:
            result = subprocess.run(
                [executable, "daemon", action],
                stdout=buf,
                stderr=buf,
                text=True,
                timeout=_STEP_TIMEOUT,
            )
            buf.seek(0)
            output = buf.read().strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RestartStep(name, False, str(exc))
    if action == "stop" and result.returncode == 1:
        # `daemon stop` exits 1 when nothing was running — fine here.
        return RestartStep(name, True, output or "daemon was not running")
    if result.returncode != 0:
        return RestartStep(name, False, output or f"exit {result.returncode}")
    return RestartStep(name, True, output)


def _clear_snapshot_cache() -> RestartStep:
    name = "clear snapshot cache"
    try:
        # Module-attribute lookup at call time so the test suite's path
        # redirection of daemon.CACHE_FILE applies.
        daemon.CACHE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        return RestartStep(name, False, str(exc))
    return RestartStep(name, True, str(daemon.CACHE_FILE))


def perform_daemon_restart() -> list[RestartStep]:
    """Stop the daemon, clear the discovery snapshot, start the daemon.

    Never raises: each step reports success/failure, and a failed stop
    doesn't block the start attempt.
    """
    executable = _agent_hq_executable()
    if executable is None:
        return [RestartStep("locate agent-hq", False, "agent-hq executable not found")]
    return [
        _run_daemon_command(executable, "stop"),
        _clear_snapshot_cache(),
        _run_daemon_command(executable, "start"),
    ]


def web_service_owns_process(pid: int) -> bool:
    """True iff ``pid`` is the process launchd manages as the web service.

    Compares against the ``pid =`` line of ``launchctl print``. This is the
    definitive "safe to kickstart myself" check — a bare ppid==1 test would
    false-positive on any orphaned process.
    """
    try:
        result = service._launchctl(["print", service._domain_target()])
    except OSError:
        # launchctl missing/broken (non-macOS dev box): not launchd-managed,
        # never a reason to fail the whole restart request.
        return False
    if result.returncode != 0:
        return False
    return service._parse_pid(result.stdout) == pid


def kick_web_service() -> None:
    """Kill-and-relaunch the launchd web service. Kills the caller.

    Popen, not run: the calling process is the kick's target, so it cannot
    wait for the result — a nonzero launchctl exit is unobservable by design,
    and the interstitial's 60s poll timeout is the user-facing backstop.
    """
    try:
        subprocess.Popen(["launchctl", "kickstart", "-k", service._domain_target()])
    except OSError as exc:
        print(f"agent-hq restart: kickstart spawn failed: {exc}", file=sys.stderr)
        # The stderr above dies with this process; the remote event is the
        # only observable record of a failed kick (#215 blind spot).
        eventlog.emit(
            "error",
            "web service restart step failed",
            {"step": "kickstart web service", "detail": str(exc)},
        )
