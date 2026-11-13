#!/usr/bin/env python3
"""Check that the demo's isolation reaches the processes it starts.

    uv run python demo/check_isolation.py

Starts no daemon, no web server and no tmux session, and sends no signal to
anything it did not start itself. It asks fresh processes, started the ways
the app starts them, which paths and programs they would use, and fails if any
answer points outside the demo root. It also checks that the refused commands
are refused, that the signal guard holds, and that stopping the demo will not
signal a process that is not the demo's.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _isolate

ROOT = _isolate.isolate()
REPO = _isolate.REPO_DIR
BIN = _isolate.DEMO_DIR / "bin"
PATH_KEYS = (
    "home",
    "tmux_socket",
    "runtime_root",
    "cache_file",
    "daemon_cache_file",
    "watcher_cache_file",
    "pid_file",
    "daemon_lock",
    "hook_state_dir",
    "watcher_hooks_dir",
    "overrides_file",
    "status_file",
    "codex_runtime_dir",
)
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=REPO, **kwargs)


def check_report(label: str, result: subprocess.CompletedProcess) -> None:
    """A child's --print-isolation output must stay inside the demo root."""
    try:
        report = json.loads(result.stdout)
    except ValueError:
        check(label, False, (result.stderr or result.stdout).strip()[-300:])
        return
    outside = [key for key in PATH_KEYS if not report[key].startswith(str(ROOT))]
    check(f"{label}: every runtime path is under the demo root", not outside, str(outside))
    check(f"{label}: orphan cleanup is disabled", report["orphan_cleanup"] == "_no_orphan_cleanup")
    check(f"{label}: os.kill is guarded", report["kill_guard"] == "_guarded_kill")
    stand_ins = {
        k: v for k, v in report.items() if k.startswith("which_") or k == "restart_executable"
    }
    wrong = {k: v for k, v in stand_ins.items() if not v.startswith(str(BIN))}
    check(
        f"{label}: agent-hq, gh, launchctl, pgrep and osascript resolve to demo/bin",
        not wrong,
        str(wrong),
    )


def live_daemon() -> str | None:
    """Start time of a real Agent HQ daemon on this machine, read-only. None if there is none."""
    try:
        pid = int(Path("/tmp/agent-hq/daemon.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    started = run(["/bin/ps", "-o", "lstart=", "-p", str(pid)]).stdout.strip()
    return f"{pid} started {started}" if started else None


def main() -> int:
    before = live_daemon()

    # 1. A fresh interpreter that imports agent_hq without going through demo/hq.py.
    #    This is what a re-executed or separately installed agent-hq would be.
    probe = (
        "import json, os, sys; sys.path.insert(0, %r); import _isolate; "
        "print(json.dumps(_isolate.effective_paths()))" % str(_isolate.DEMO_DIR)
    )
    check_report("fresh python process", run([sys.executable, "-c", probe]))

    # 2. The executable the dashboard's Restart action would run.
    from agent_hq import restart

    executable = restart._agent_hq_executable()
    check(
        "restart would run demo/bin/agent-hq",
        str(executable) == str(BIN / "agent-hq"),
        str(executable),
    )
    check_report("restart executable", run([str(executable), "--print-isolation"]))

    # 3. `agent-hq` by bare name from a shell, as the fzf key bindings and the
    #    Codex wrapper run it.
    check_report("agent-hq by name from sh", run(["/bin/sh", "-c", "agent-hq --print-isolation"]))

    # 4. Commands the launcher refuses outright.
    for argv in (["remote", "serve"], ["web", "service", "install"], ["hooks", "install-codex"]):
        result = run([sys.executable, str(_isolate.DEMO_DIR / "hq.py"), *argv])
        check(f"hq.py refuses `{' '.join(argv)}`", result.returncode == 2, result.stderr.strip())
    bare = {k: v for k, v in os.environ.items() if k != "AGENT_HQ_DEMO_ROOT"}
    check(
        "demo/bin/agent-hq refuses to run outside the demo",
        run([str(BIN / "agent-hq"), "--list"], env=bare).returncode == 2,
    )

    # 5. Stand-ins for system tools.
    found = run(["pgrep", "-fl", "agent-hq.*daemon"])
    check("pgrep finds nothing", found.returncode == 1 and not found.stdout.strip(), found.stdout)
    for argv in (
        ["launchctl", "kickstart", "-k", "gui/0/x"],
        ["launchctl", "bootout", "gui/0/x"],
        ["gh", "issue", "close", "1"],
        ["pkill", "-f", "x"],
        ["security", "find-generic-password"],
        ["tailscale", "ip", "-4"],
    ):
        check(f"`{' '.join(argv[:2])}` is refused", run(argv).returncode != 0)

    # 6. The signal guard: a process that is neither the demo's nor a child of
    #    this one cannot be signalled. The victim is a sleep this check starts
    #    and then orphans, so nothing else is ever at risk.
    pid = int(run(["/bin/sh", "-c", "/bin/sleep 60 >/dev/null 2>&1 & echo $!"]).stdout.strip())
    time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGTERM)
        refused = False
    except PermissionError:
        refused = True
    still_alive = run(["/bin/ps", "-o", "pid=", "-p", str(pid)]).stdout.strip() != ""
    check("os.kill refuses a non-demo process", refused and still_alive)

    # 7. `seed.py --stop` with a stale PID file that names a non-demo process:
    #    it must refuse to signal it. Run against a scratch root so a demo that
    #    is running right now is left alone.
    with tempfile.TemporaryDirectory() as scratch:
        runtime = Path(scratch) / "runtime"
        runtime.mkdir()
        (runtime / "daemon.pid").write_text(str(pid))
        stopped = run(
            [sys.executable, str(_isolate.DEMO_DIR / "seed.py"), "--stop"],
            env={**os.environ, "AGENT_HQ_DEMO_ROOT": scratch},
        )
    still_alive = run(["/bin/ps", "-o", "pid=", "-p", str(pid)]).stdout.strip() != ""
    check(
        "seed.py --stop leaves a non-demo PID alone",
        "not signalling" in stopped.stdout and still_alive,
        (stopped.stdout + stopped.stderr).strip()[-300:],
    )
    _isolate._real_kill(pid, signal.SIGTERM)  # clean up the sleep this check started

    after = live_daemon()
    if before is None:
        print("note no real Agent HQ daemon on this machine to compare against")
    else:
        check(
            "the real daemon is the same process as before the check",
            before == after,
            f"{before} -> {after}",
        )

    print("\nFAILED: " + ", ".join(failures) if failures else "\nall isolation checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
